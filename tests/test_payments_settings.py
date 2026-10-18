"""Getting paid for bookings (Kris 2026-09-24: "how would a real kennels deal
with payments?" → deposit to secure, the balance on collection, fixed prices
need no quote, peak dates ask for more). Three Invoice settings, each
defaulting to what every business had before."""
import asyncio
from datetime import date

import pytest

from app import db, deposits, invoice_config, payments, quote_autodraft
from app.jobs import raise_balances


# ── the defaults keep every existing business exactly as it was ─────────────
def test_the_defaults_change_nothing():
    s = invoice_config.settings_for({})
    assert s["quote_booking_priced"] is True
    assert s["balance_mode"] == "off"
    assert s["peak_deposits"] == []


def test_the_form_saves_all_three_behind_its_marker():
    out = invoice_config.clean_form({"payments_form": "1", "balance_mode": "draft",
                                     "peak_deposits": "2026-12-18 to 2027-01-03: 100%\nnonsense"})
    ex = out["extras"]
    assert ex["quote_booking_priced"] is False                 # unticked box
    assert ex["balance_mode"] == "draft"
    assert ex["peak_deposits"] == [{"from": "2026-12-18", "to": "2027-01-03", "percent": 100.0}]
    assert "quote_booking_priced" not in invoice_config.clean_form({}).get("extras", {})  # another section's save
    assert invoice_config.clean_form({"payments_form": "1", "balance_mode": "evil"})["extras"]["balance_mode"] == "off"


# ── peak dates ───────────────────────────────────────────────────────────────
def test_a_booking_touching_a_peak_asks_for_the_peak_deposit(monkeypatch):
    s = {"default_deposit_percent": 25, "extras": {"deposit_on_confirm": True,
         "peak_deposits": [{"from": "2026-12-18", "to": "2027-01-03", "percent": 100}]}}
    monkeypatch.setattr(db, "get_invoice_settings", lambda tid, client=None: s)
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: [])
    monkeypatch.setattr(db, "bookings_for_log", lambda lid, tid, client=None: [
        {"id": "b1", "booking_ref": "BK-1", "status": "proposed", "quoted_amount": 61.0,
         "booking_date": "2026-12-23", "summary": "Christmas boarding"},
        {"id": "b2", "booking_ref": "BK-2", "status": "proposed", "quoted_amount": 61.0,
         "booking_date": "2026-11-10", "summary": "November boarding"}])
    _s, _held, lines = deposits._deposit_lines("t1", "L1", "c@example.com")
    by = {l["booking_id"]: l for l in lines}
    assert by["b1"]["amount"] == 61.0 and "100% deposit (peak dates)" in by["b1"]["description"]
    assert by["b2"]["amount"] == 15.25 and "25% deposit" in by["b2"]["description"]


# ── the quote that only restated a diary price ───────────────────────────────
def test_no_restating_quote_when_the_setting_is_off(monkeypatch):
    monkeypatch.setattr(db, "bookings_for_log", lambda lid, tid, client=None: [
        {"id": "b1", "quoted_amount": 61.0, "booking_ref": "BK-1075"}])
    monkeypatch.setattr(db, "get_invoice_settings", lambda tid, client=None: {"extras": {"quote_booking_priced": False}})
    monkeypatch.setattr(db, "quote_for_log", lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked")))
    assert quote_autodraft.maybe_draft_for_log("t1", "L1", {"quote_auto_draft": "propose"},
                                               contact_email="j@example.com") is None


# ── the balance, the day before it ends ──────────────────────────────────────
@pytest.fixture
def rig(monkeypatch):
    made, marked, sent, pushed = [], [], [], []
    stay = {"id": "b1", "booking_ref": "BK-1075", "summary": "Boarding for Dottie", "status": "confirmed",
            "booking_date": "2026-11-10", "start_block": 36, "length_blocks": 2 * 96 + 28,
            "quoted_amount": 61.0, "deposit_amount": 15.25, "contact": "jess@example.com"}
    day_visit = {"id": "b2", "booking_ref": "BK-2", "summary": "Daycare", "status": "confirmed",
                 "booking_date": "2026-11-12", "quoted_amount": 28.0, "contact": "x@example.com"}
    mode = {"m": "draft"}
    monkeypatch.setattr(db, "get_invoice_settings", lambda tid, client=None: {"extras": {"balance_mode": mode["m"]}})
    monkeypatch.setattr(db, "bookings_awaiting_balance", lambda tid, d, **k: [stay, day_visit])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda tid, e, client=None: {"id": "c1", "name": "Jess"})
    monkeypatch.setattr(db, "create_invoice", lambda *a, **k: made.append((a, k)) or {"id": f"i{len(made)}", "invoice_ref": f"INV-9{len(made)}"})
    monkeypatch.setattr(db, "mark_booking_balance", lambda bid, tid, iid: marked.append((bid, iid)))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    import app.push as push
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushed.append(k))
    import app.invoice_send as invoice_send

    async def fake_send(tid, inv, **k):
        sent.append(inv["id"])
    monkeypatch.setattr(invoice_send, "send_invoice_email", fake_send)
    return {"made": made, "marked": marked, "sent": sent, "pushed": pushed, "mode": mode}


def test_the_balance_is_the_price_less_the_deposit_due_on_the_last_day(rig):
    done = asyncio.run(raise_balances.raise_for_tenant("t1", date(2026, 11, 12)))
    assert [d["booking"] for d in done] == ["BK-1075", "BK-2"]      # a stay ending then + a day visit on it
    args, kw = rig["made"][0]
    line = args[5][0]
    assert line["amount"] == 45.75 and line["description"] == "Balance — Boarding for Dottie (BK-1075)"
    assert kw["due_date"] == "2026-11-12" and "£15.25 deposit already asked for is taken off" in kw["notes"]
    assert rig["marked"][0] == ("b1", "i1")
    assert rig["sent"] == [] and rig["pushed"][0]["kind"] == "invoice_balance_ready"   # draft: told, not sent


def test_send_mode_sends_it_and_a_different_day_raises_nothing(rig):
    rig["mode"]["m"] = "send"
    asyncio.run(raise_balances.raise_for_tenant("t1", date(2026, 11, 12)))
    assert rig["sent"] == ["i1", "i2"] and rig["pushed"] == []
    rig["made"].clear()
    asyncio.run(raise_balances.raise_for_tenant("t1", date(2026, 11, 11)))
    assert rig["made"] == []                                            # nothing ends on the 11th


def test_off_raises_nothing(rig):
    rig["mode"]["m"] = "off"
    assert asyncio.run(raise_balances.raise_for_tenant("t1", date(2026, 11, 12))) == []
    assert rig["made"] == []


def test_the_settings_page_carries_the_three():
    t = open("src/app/templates/app/invoice_settings.html", encoding="utf-8").read()
    assert 'name="payments_form"' in t and 'name="quote_booking_priced"' in t
    assert 'name="balance_mode"' in t and 'name="peak_deposits"' in t
    assert "raise_balances" in open("src/app/main.py", encoding="utf-8").read()
