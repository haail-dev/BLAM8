"""Deposit on confirmation (src/app/deposits.py).

Kris, 2026-09-20, watching the filmed kennels take: the owner had to raise the
25% deposit by hand and mark it paid by hand. Now the confirmation email
carries the deposit invoice — Pay Now when Stripe is connected, bank details
otherwise — and Stripe's webhook does the marking.

Run: python -m pytest tests/test_deposit_on_confirm.py -q
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app import db, deposits, invoice_config, payments

TENANT = "t1"
LOG = "log-1"
HANNAH = "hannah@example.com"


def _bookings():
    return [
        {"id": "b1", "booking_ref": "BK-1001", "status": "proposed", "quoted_amount": 30.5,
         "deposit_amount": None, "summary": "Trial night for Biscuit", "booking_date": "2026-10-07"},
        {"id": "b2", "booking_ref": "BK-1002", "status": "proposed", "quoted_amount": 91.5,
         "deposit_amount": None, "summary": "Boarding for Biscuit, 9–12 October", "booking_date": "2026-10-09"},
        {"id": "b3", "booking_ref": "BK-0999", "status": "confirmed", "quoted_amount": 500,
         "deposit_amount": None, "summary": "Already confirmed in the diary", "booking_date": "2026-10-01"},
    ]


def _wire(monkeypatch, *, percent=25, bank=True, stripe=False, on=True, already=(), bookings=None):
    calls = {"created": [], "events": [], "status": [], "links": [], "deposits": [], "crm": []}
    row = {"default_deposit_percent": percent, "extras": {} if on else {"deposit_on_confirm": False}}
    if bank:
        row.update({"bank_account_name": "Little Paws", "bank_sort_code": "04-00-04",
                    "bank_account_number": "12345678"})
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: row)
    monkeypatch.setattr(db, "bookings_for_log", lambda *a, **k: bookings if bookings is not None else _bookings())
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: set(already))
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "name": "Hannah"})
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: stripe)
    monkeypatch.setattr(payments, "payment_url", lambda i: f"https://blam8.com/pay/{i}")
    monkeypatch.setattr(db, "log_agent_event", lambda t, kind, s, **k: calls["events"].append((kind, s)))

    def fake_create(tenant_id, email, name, pf, pt, lines, **kw):
        calls["created"].append({"email": email, "name": name, "lines": lines, **kw})
        return {"id": "inv1", "invoice_ref": "INV-1001",
                "total": round(sum(l["amount"] for l in lines), 2), "due_date": kw.get("due_date")}
    monkeypatch.setattr(db, "create_invoice", fake_create)
    monkeypatch.setattr(db, "set_invoice_status", lambda i, t, s, **k: calls["status"].append((i, s)))
    monkeypatch.setattr(db, "set_invoice_payment_link", lambda i, t, u, **k: calls["links"].append(u))
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t, **k: calls["created"][0]["lines"] if calls["created"] else [])
    monkeypatch.setattr(db, "set_booking_deposit", lambda b, t, a, **k: calls["deposits"].append((b, a)))
    from app import invoice_send
    monkeypatch.setattr(invoice_send, "_crm_invoice_activity", lambda *a, **k: calls["crm"].append(k))
    return calls


# ── the amount ────────────────────────────────────────────────────────────────
def test_the_fixed_stay_deposit_beats_the_percentage():
    assert deposits.deposit_for({"deposit_amount": 50, "quoted_amount": 400}, 25) == (50.0, "fixed")


def test_the_percentage_of_the_quoted_price_otherwise():
    assert deposits.deposit_for({"deposit_amount": None, "quoted_amount": 91.5}, 25) == (22.88, "percent")
    assert deposits.deposit_for({"quoted_amount": 30.5}, 25) == (7.63, "percent")


def test_nothing_without_a_price_or_a_deposit_setting():
    assert deposits.deposit_for({"quoted_amount": None}, 25) is None
    assert deposits.deposit_for({"quoted_amount": 91.5}, None) is None
    assert deposits.deposit_for({"quoted_amount": 0, "deposit_amount": 0}, 25) is None


# ── staging ───────────────────────────────────────────────────────────────────
def test_one_invoice_for_the_holds_the_send_confirms(monkeypatch):
    calls = _wire(monkeypatch)
    inv, block = deposits.stage_for_log(TENANT, LOG, HANNAH)
    assert inv and inv["invoice_ref"] == "INV-1001"
    made = calls["created"][0]
    # The confirmed-in-the-diary booking is not this email's to invoice.
    assert [l["booking_id"] for l in made["lines"]] == ["b1", "b2"]
    assert [l["amount"] for l in made["lines"]] == [7.63, 22.88]
    assert made["lines"][1]["description"] == "25% deposit — Boarding for Biscuit, 9–12 October (BK-1002)"
    assert made["booking_id"] == "b1" and made["contact_id"] == "c1" and made["name"] == "Hannah"
    assert made["created_by"] == "agent" and "BK-1001, BK-1002" in made["notes"]
    # No Stripe: the block carries the bank details and the reference to quote.
    assert "Deposit to secure your booking: £30.51 (invoice INV-1001" in block
    assert "Trial night for Biscuit (BK-1001) £7.63" in block
    assert "Sort code: 04-00-04" in block and "Reference: INV-1001" in block
    assert "View the invoice: https://blam8.com/pay/inv1" in block
    assert "Pay online" not in block


def test_stripe_connected_means_a_pay_now_link_and_no_bank_details(monkeypatch):
    _wire(monkeypatch, stripe=True)
    inv, block = deposits.stage_for_log(TENANT, LOG, HANNAH)
    assert inv and "Pay online: https://blam8.com/pay/inv1" in block
    assert "Sort code" not in block


def test_no_payment_route_means_no_invoice_but_the_owner_is_told(monkeypatch):
    calls = _wire(monkeypatch, bank=False, stripe=False)
    assert deposits.stage_for_log(TENANT, LOG, HANNAH) == (None, "")
    assert calls["created"] == []
    kind, text = calls["events"][0]
    assert kind == "deposit_skipped" and "no way to pay" in text and "BK-1001, BK-1002" in text


def test_the_switch_off_asks_for_nothing(monkeypatch):
    calls = _wire(monkeypatch, on=False)
    assert deposits.stage_for_log(TENANT, LOG, HANNAH) == (None, "")
    assert calls["created"] == [] and calls["events"] == []


def test_a_booking_already_on_a_live_invoice_is_skipped(monkeypatch):
    calls = _wire(monkeypatch, already={"b2"})
    inv, block = deposits.stage_for_log(TENANT, LOG, HANNAH)
    assert [l["booking_id"] for l in calls["created"][0]["lines"]] == ["b1"]
    assert "£7.63 (invoice" in block


def test_no_holds_or_no_prices_means_nothing_and_no_noise(monkeypatch):
    calls = _wire(monkeypatch, bookings=[{"id": "b9", "status": "proposed", "quoted_amount": None}])
    assert deposits.stage_for_log(TENANT, LOG, HANNAH) == (None, "")
    assert calls["created"] == [] and calls["events"] == []
    assert deposits.stage_for_log(TENANT, LOG, None) == (None, "")


def test_staging_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_invoice_settings", boom)
    assert deposits.stage_for_log(TENANT, LOG, HANNAH) == (None, "")


# ── the due date ──────────────────────────────────────────────────────────────
def test_due_on_terms_but_never_after_the_stay_starts():
    s = invoice_config.settings_for({"payment_terms": "14 days"})
    today = date(2026, 9, 20)
    assert deposits.due_date(s, _bookings()[:2], today) == "2026-10-04"      # terms win: 14 days
    soon = [{"booking_date": "2026-09-25"}]
    assert deposits.due_date(s, soon, today) == "2026-09-24"                 # eve of the stay
    tomorrow = [{"booking_date": "2026-09-21"}]
    assert deposits.due_date(s, tomorrow, today) == "2026-09-20"             # never before today


# ── after the send ────────────────────────────────────────────────────────────
def test_after_send_marks_it_sent_and_stamps_each_booking(monkeypatch):
    calls = _wire(monkeypatch, stripe=True)
    inv, _ = deposits.stage_for_log(TENANT, LOG, HANNAH)
    deposits.after_send(TENANT, inv, log_id=LOG, customer_email=HANNAH)
    assert ("inv1", "sent") in calls["status"]
    assert calls["links"] == ["https://blam8.com/pay/inv1"]
    assert calls["deposits"] == [("b1", 7.63), ("b2", 22.88)]
    assert any("Deposit invoice INV-1001 (£30.51) went out with the confirmation" in s
               for _, s in calls["events"])
    assert calls["crm"] and "Deposit invoice INV-1001" in calls["crm"][0]["subject"]


def test_a_failed_send_voids_the_staged_invoice(monkeypatch):
    calls = _wire(monkeypatch)
    inv, _ = deposits.stage_for_log(TENANT, LOG, HANNAH)
    deposits.discard(TENANT, inv)
    assert calls["status"] == [("inv1", "void")]
    deposits.after_send(TENANT, None, log_id=LOG, customer_email=HANNAH)   # a no-op, never raises


# ── the two send paths carry it ───────────────────────────────────────────────
def _read(rel: str) -> str:
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_approve_and_send_stages_before_and_finalises_after():
    src = _read("src/app/dashboard.py")
    body = src[src.index("async def send_draft("):src.index("@router.post(\"/drafts/{log_id}/skip\"")]
    assert "deposits.stage_for_log(" in body and "+ dep_block" in body
    assert body.index("deposits.stage_for_log(") < body.index("await mailbox.send_reply(")
    assert "deposits.after_send(" in body and "deposits.discard(" in body


def test_auto_send_stages_before_and_finalises_after():
    src = _read("src/app/pipeline.py")
    assert "deposits.stage_for_log(" in src and "draft_out += dep_block" in src
    assert src.index("deposits.stage_for_log(") < src.index("disclosure.wrap(cfg, draft_out")
    assert "deposits.after_send(" in src and "deposits.discard(" in src


# ── the settings switch ───────────────────────────────────────────────────────
def test_the_switch_round_trips_through_the_settings_form():
    on = invoice_config.clean_form({"deposit_on_confirm_form": "1", "deposit_on_confirm": "1"})
    assert on["extras"]["deposit_on_confirm"] is True
    off = invoice_config.clean_form({"deposit_on_confirm_form": "1"})
    assert off["extras"]["deposit_on_confirm"] is False
    untouched = invoice_config.clean_form({})
    assert "deposit_on_confirm" not in (untouched.get("extras") or {})
    assert invoice_config.settings_for(None)["deposit_on_confirm"] is True
    assert invoice_config.settings_for({"extras": {"deposit_on_confirm": False}})["deposit_on_confirm"] is False
    html = _read("src/app/templates/app/invoice_settings.html")
    assert 'name="deposit_on_confirm"' in html and 'name="deposit_on_confirm_form"' in html
