"""Quotes Phase 2 (docs/modules/module-quotes.md) — chase, concierge, portal, money.

Pins:
  * the chase cadence maths (day 3 / day 7 / pre-expiry, capped at 3) and the
    stop conditions: inbound reply stands the chase down, expiry flips the
    quote (with a push), the cap is never exceeded, auto-send tenants send
    while approval tenants draft — and the /q link is guaranteed into the body
  * the "keeps looking" escalation fires at day 7 with ≥2 views, exactly once
  * concierge: quote kinds are unlocked on EVERY tier, stagers validate against
    real data, convert executes through quoting.conversion_lines, manual accept
    runs the same guarded path as /q
  * the list_quotes data tool derives 'expired' and returns quote_id handles
  * portal list_my_quotes is identity-gated, hides drafts, links /q
  * money.quote_pipeline buckets awaiting vs accepted-not-invoiced

Run: python -m pytest tests/test_quotes_phase2.py -q
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import app.jobs.check_quote_chases as cqc
from app import concierge_actions, concierge_data, db, money, portal_data, push, quote_public
from app.concierge_actions import ActionError

TENANT = "11111111-1111-1111-1111-111111111111"
QID = "22222222-2222-2222-2222-222222222222"


def _today() -> date:
    # The chase job clocks in UTC (check_quote_chases.run); a local date.today()
    # disagrees with it between 23:00 and 00:00 UTC in summer (flaked 2026-09-03).
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()


def _iso_days_ago(n: int) -> str:
    return (_today() - timedelta(days=n)).isoformat()


def _quote(**over) -> dict:
    base = {"id": QID, "tenant_id": TENANT, "quote_ref": "QUO-1001",
            "customer_email": "pat@example.com", "customer_name": "Pat Verner",
            "status": "sent", "title": "Bathroom refit", "subtotal": 100.0,
            "vat": 20.0, "total": 120.0, "version": 1,
            "sent_at": _iso_days_ago(4) + "T09:00:00+00:00",
            "valid_until": (_today() + timedelta(days=14)).isoformat(),
            "viewed_at": None, "view_count": 0, "reminders_sent": 0,
            "last_reminder_at": None, "accepted_at": None, "accepted_name": None,
            "acceptance_snapshot": None, "deposit_percent": None,
            "deposit_invoice_id": None, "invoice_id": None, "booking_id": None,
            "contact_id": None, "deal_id": None, "show_item_prices": True,
            "issue_date": _iso_days_ago(4), "intro": None, "notes": None,
            "created_by": "owner", "decline_reason": None, "declined_at": None}
    return {**base, **over}


SNAPSHOT = {"version": 1, "subtotal": 100.0, "vat": 20.0, "total": 120.0,
            "lines": [{"description": "Supply and fit suite", "quantity": 1,
                       "unit_price": 100.0, "amount": 100.0, "booking_id": None,
                       "kb_entry_id": None, "is_optional": False}]}


# ── chase cadence maths ───────────────────────────────────────────────────────
def test_touches_due_cadence():
    today = _today()
    assert cqc._touches_due(_quote(sent_at=_iso_days_ago(1)), today) == 0
    assert cqc._touches_due(_quote(sent_at=_iso_days_ago(4)), today) == 1
    assert cqc._touches_due(_quote(sent_at=_iso_days_ago(8)), today) == 2
    near_expiry = _quote(sent_at=_iso_days_ago(8),
                         valid_until=(today + timedelta(days=2)).isoformat())
    assert cqc._touches_due(near_expiry, today) == 3
    # Capped even when everything has passed.
    stale = _quote(sent_at=_iso_days_ago(40),
                   valid_until=(today + timedelta(days=1)).isoformat())
    assert cqc._touches_due(stale, today) == 3


@pytest.fixture
def chase_env(monkeypatch):
    """A recording harness around one run() over a single quote."""
    state = {"drafted": [], "sent_mail": [], "reminders": [], "pushes": [],
             "expired": [], "auto": False, "quote": _quote(),
             "inbound": False, "compose_text": "Just checking you got our quote."}
    monkeypatch.setattr(db, "list_open_sent_quotes", lambda: [state["quote"]])
    monkeypatch.setattr(db, "has_inbound_from_since",
                        lambda t, e, s: state["inbound"])
    monkeypatch.setattr(db, "expire_quote",
                        lambda i, t: state["expired"].append(i) or True)
    monkeypatch.setattr(db, "record_quote_reminder",
                        lambda i, t, **k: state["reminders"].append(i))
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: state["drafted"].append((a, k)) or ("log-1", True))
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(db, "effective_config",
                        lambda t, u: {"auto_send": state["auto"]})
    monkeypatch.setattr(push, "notify", lambda *a, **k: state["pushes"].append(k))

    async def _compose(cfg, purpose, facts, **k):
        return SimpleNamespace(text=state["compose_text"], model="test-model",
                               usage=None)
    monkeypatch.setattr(cqc.agent, "compose_outbound", _compose)
    monkeypatch.setattr(cqc.usage, "record_anthropic", lambda *a, **k: None)

    async def _send(*a, **k):
        state["sent_mail"].append((a, k))
    monkeypatch.setattr(cqc.mailbox, "send_mail", _send)
    monkeypatch.setattr(cqc.disclosure, "wrap", lambda cfg, text: text)
    return state


def test_chase_drafts_at_day3_and_guarantees_the_link(chase_env):
    out = asyncio.run(cqc.run())
    assert out["drafted"] == 1 and out["sent"] == 0
    assert chase_env["reminders"] == [QID]
    (args, kwargs) = chase_env["drafted"][0]
    body = args[4]
    assert f"/q/{QID}" in body            # the link IS the quote — always present
    assert kwargs["kind"] == "quote_reminder"
    assert not chase_env["sent_mail"]     # approval tenant: queued, not emailed


def test_chase_stops_on_inbound_reply(chase_env):
    chase_env["inbound"] = True
    out = asyncio.run(cqc.run())
    assert out["drafted"] == 0 and chase_env["reminders"] == []


def test_chase_never_exceeds_counted_touches(chase_env):
    chase_env["quote"] = _quote(reminders_sent=1)          # day 4, touch 1 done
    out = asyncio.run(cqc.run())
    assert out["drafted"] == 0
    chase_env["quote"] = _quote(reminders_sent=3,          # capped forever
                                sent_at=_iso_days_ago(40))
    out = asyncio.run(cqc.run())
    assert out["drafted"] == 0


def test_chase_auto_send_tenant_sends(chase_env):
    chase_env["auto"] = True
    out = asyncio.run(cqc.run())
    assert out["sent"] == 1 and out["drafted"] == 0
    assert len(chase_env["sent_mail"]) == 1


def test_expiry_flips_and_pushes_instead_of_chasing(chase_env):
    chase_env["quote"] = _quote(valid_until=_iso_days_ago(1))
    out = asyncio.run(cqc.run())
    assert out["expired"] == 1 and out["drafted"] == 0
    assert chase_env["expired"] == [QID]
    assert any(p["kind"] == "quote_expired" for p in chase_env["pushes"])


def test_keeps_looking_escalation_fires_at_day7_with_views(chase_env):
    chase_env["quote"] = _quote(sent_at=_iso_days_ago(7), view_count=3,
                                reminders_sent=2)
    out = asyncio.run(cqc.run())
    assert out["escalated"] == 1
    assert any(p["kind"] == "quote_keeps_looking" for p in chase_env["pushes"])
    # Not at day 8 (fires exactly once), not with a single view.
    chase_env["pushes"].clear()
    chase_env["quote"] = _quote(sent_at=_iso_days_ago(8), view_count=3,
                                reminders_sent=2)
    assert asyncio.run(cqc.run())["escalated"] == 0
    chase_env["quote"] = _quote(sent_at=_iso_days_ago(7), view_count=1,
                                reminders_sent=2)
    assert asyncio.run(cqc.run())["escalated"] == 0


# ── concierge ─────────────────────────────────────────────────────────────────
def test_quote_kinds_unlocked_on_every_tier():
    starter = concierge_actions.allowed_kinds(full_qa=False, scheduling_on=False)
    for kind in ("quote_create", "quote_send", "quote_mark_accepted",
                 "quote_mark_declined", "quote_convert_invoice", "quote_cancel",
                 "quote_reminder_draft"):
        assert kind in starter


def test_stage_quote_create_validates(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    with pytest.raises(ActionError):
        concierge_actions._stage_quote_create(TENANT, None, {"contact": "not-an-email"})
    with pytest.raises(ActionError):
        concierge_actions._stage_quote_create(
            TENANT, None, {"contact": "a@b.com", "amount": "50",
                           "deposit_percent": "150"})
    summary, payload, details, _ = concierge_actions._stage_quote_create(
        TENANT, None, {"contact": "a@b.com", "title": "Boiler service",
                       "amount": "£95.50", "deposit_percent": "25"})
    assert payload["amount"] == 95.5 and payload["deposit_percent"] == 25.0
    assert "Boiler service" in summary


def test_stage_quote_send_warns_on_resend_and_refuses_terminal(monkeypatch):
    monkeypatch.setattr(db, "get_quote", lambda *a, **k: _quote(status="accepted"))
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_quote_lines", lambda *a, **k: [])
    with pytest.raises(ActionError):
        concierge_actions._stage_quote_send(TENANT, None, {"quote_id": QID})
    monkeypatch.setattr(db, "get_quote", lambda *a, **k: _quote(status="sent"))
    summary, payload, details, body = concierge_actions._stage_quote_send(
        TENANT, None, {"quote_id": QID})
    assert summary.startswith("Resend")
    assert any("AGAIN" in d for d in details)
    assert f"/q/{QID}" in body


def test_exec_quote_convert_invoice_uses_snapshot_and_credit(monkeypatch):
    q = _quote(status="accepted", acceptance_snapshot=SNAPSHOT,
               deposit_invoice_id="dep-1")
    made, linked = [], []
    monkeypatch.setattr(db, "get_quote", lambda *a, **k: q)
    monkeypatch.setattr(db, "get_invoice", lambda *a, **k: {
        "id": "dep-1", "invoice_ref": "INV-1009", "status": "paid", "subtotal": 30.0})
    monkeypatch.setattr(db, "create_invoice",
                        lambda *a, **k: made.append((a, k)) or
                        {"id": "new-1", "invoice_ref": "INV-1010", "total": 84.0})
    monkeypatch.setattr(db, "link_quote_invoice",
                        lambda i, t, inv, **k: linked.append(inv))
    out = asyncio.run(concierge_actions._exec_quote_convert_invoice(
        TENANT, None, {"quote_id": QID, "ref": "QUO-1001", "total": 120.0,
                       "who": "Pat"}, None))
    lines = made[0][0][5]
    assert [l["amount"] for l in lines] == [100.0, -30.0]
    assert linked == ["new-1"]
    assert "INV-1010" in out["summary"]


def test_exec_quote_mark_accepted_runs_guarded_path(monkeypatch):
    row = _quote(status="accepted", acceptance_snapshot=SNAPSHOT)
    effects, minted = [], []
    monkeypatch.setattr(db, "accept_quote", lambda i, t, n: row)
    monkeypatch.setattr(quote_public, "accepted_side_effects",
                        lambda t, q: effects.append(q["id"]))
    monkeypatch.setattr(quote_public, "mint_deposit_invoice",
                        lambda t, q: minted.append(q["id"]) or None)
    out = asyncio.run(concierge_actions._exec_quote_mark_accepted(
        TENANT, None, {"quote_id": QID, "ref": "QUO-1001", "total": 120.0,
                       "accepted_name": "Sam — by phone"}, None))
    assert effects == [QID] and minted == [QID]
    assert "accepted" in out["summary"]
    # A race (someone declined online first) surfaces honestly.
    monkeypatch.setattr(db, "accept_quote", lambda i, t, n: None)
    with pytest.raises(ActionError):
        asyncio.run(concierge_actions._exec_quote_mark_accepted(
            TENANT, None, {"quote_id": QID, "ref": "QUO-1001", "total": 120.0,
                           "accepted_name": "Sam"}, None))


def test_list_quotes_tool_derives_expired_and_hands_out_ids(monkeypatch):
    rows = [_quote(), _quote(id="q2", quote_ref="QUO-1002",
                             valid_until=_iso_days_ago(1)),
            _quote(id="q3", quote_ref="QUO-1003", status="draft")]
    monkeypatch.setattr(db, "list_quotes", lambda t, client=None: rows)
    out = concierge_data._list_quotes(TENANT, None, {"status": "expired"})
    assert out["count"] == 1 and out["quotes"][0]["ref"] == "QUO-1002"
    assert out["quotes"][0]["quote_id"] == "q2"
    out = concierge_data._list_quotes(TENANT, None, {})
    assert out["count"] == 3


# ── portal ────────────────────────────────────────────────────────────────────
def test_portal_list_my_quotes_identity_gated_and_filtered(monkeypatch):
    session = portal_data.PortalSession(tenant_id=TENANT, business_name="Thornbeck")
    assert portal_data._list_my_quotes(session, {}) == portal_data.NOT_IDENTIFIED
    session.contact = {"email": "pat@example.com", "name": "Pat"}
    monkeypatch.setattr(db, "quotes_for_contact", lambda t, e, client=None: [
        _quote(), _quote(id="q2", status="accepted",
                         accepted_at="2026-07-29T10:00:00+00:00")])
    out = portal_data._list_my_quotes(session, {})
    assert out["count"] == 2 and out["awaiting_answer"] == 1
    open_q = next(q for q in out["quotes"] if q["status"] == "awaiting your answer")
    assert open_q["view_url"].endswith(f"/q/{QID}")
    assert open_q["can_accept_online"] is True


# ── money ─────────────────────────────────────────────────────────────────────
def test_money_quote_pipeline_buckets():
    quotes = [_quote(total=100), _quote(id="b", total=50),
              _quote(id="c", status="accepted", total=200),
              _quote(id="d", status="invoiced", total=999),   # already billed
              _quote(id="e", status="draft", total=999)]
    p = money.quote_pipeline(quotes)
    assert p == {"awaiting_count": 2, "awaiting_value": 150.0,
                 "accepted_count": 1, "accepted_value": 200.0,
                 "accepted_deposits": 0.0}
    assert money.view([], {}, {}, date.today())["quotes"] is None


def test_money_accepted_bucket_nets_off_deposit_invoices():
    """Live 2026-09-03 (Stone Mason): QUO-1001 £1,209 and QUO-1002 £246 were
    accepted with 50% deposits already invoiced, and Money still said £1,455
    "not yet invoiced". The bucket is the BALANCE; a cancelled deposit invoice
    counts for nothing; a deposit exceeding the total never goes negative."""
    quotes = [_quote(id="a", status="accepted", total=1209.0, deposit_invoice_id="d1"),
              _quote(id="b", status="accepted", total=246.0, deposit_invoice_id="d2"),
              _quote(id="c", status="accepted", total=100.0, deposit_invoice_id="d3"),
              _quote(id="d", status="accepted", total=50.0, deposit_invoice_id="d4"),
              _quote(id="e", status="accepted", total=80.0)]
    invoices = [{"id": "d1", "total": 604.5, "status": "sent"},
                {"id": "d2", "total": 123.0, "status": "paid"},
                {"id": "d3", "total": 50.0, "status": "cancelled"},   # doesn't count
                {"id": "d4", "total": 60.0, "status": "sent"}]        # over the top → 0
    p = money.quote_pipeline(quotes, invoices)
    assert p["accepted_count"] == 5
    assert p["accepted_value"] == round((1209 - 604.5) + (246 - 123) + 100 + 0 + 80, 2)
    assert p["accepted_deposits"] == round(604.5 + 123 + 60, 2)
    # view() threads the invoices through
    v = money.view(invoices, {}, {}, date.today(), quotes=quotes)
    assert v["quotes"]["accepted_value"] == p["accepted_value"]


# ── Phase 3: the quote writes itself (src/app/quote_autodraft.py) ────────────────
from app import quote_autodraft


@pytest.fixture(autouse=True)
def _no_open_quote(monkeypatch):
    monkeypatch.setattr(db, "open_quote_for_customer", lambda *a, **k: None)


def test_autodraft_skips_when_a_quote_is_already_in_play(monkeypatch):
    """Live 2026-09-29: the start-date confirmation priced BK-1083 at the
    service's £85 and auto-drafted QUO-1024 beside the accepted £2,850 QUO-1023."""
    made = []
    monkeypatch.setattr(db, "bookings_for_log", lambda *a, **k: [
        {"id": "bk1", "job_type": "Bathroom plumbing", "quoted_amount": 85.0}])
    monkeypatch.setattr(db, "quote_for_log", lambda *a, **k: None)
    monkeypatch.setattr(db, "open_quote_for_customer",
                        lambda *a, **k: _quote(status="accepted"))
    monkeypatch.setattr(db, "create_quote", lambda *a, **k: made.append(1) or _quote())
    assert quote_autodraft.maybe_draft_for_log(TENANT, {}, "log-2", "r@e.com") is None
    assert made == []


def test_autodraft_mode_parsing():
    assert quote_autodraft.mode_for(None) == "propose"
    assert quote_autodraft.mode_for({}) == "propose"
    assert quote_autodraft.mode_for({"quote_auto_draft": "AUTO"}) == "auto"
    assert quote_autodraft.mode_for({"quote_auto_draft": "garbage"}) == "propose"
    assert quote_autodraft.mode_for({"quote_auto_draft": "off"}) == "off"


def test_autodraft_creates_once_from_priced_bookings(monkeypatch):
    made, pushes = [], []
    booking = {"id": "bk1", "job_type": "Boiler service", "booking_ref": "BK-1001",
               "quoted_amount": 95.0}
    monkeypatch.setattr(db, "bookings_for_log", lambda l, t, client=None: [
        booking, {"id": "bk2", "quoted_amount": None}])   # unpriced never quotes
    monkeypatch.setattr(db, "quote_for_log", lambda l, t, client=None: None)
    monkeypatch.setattr(db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1",
                                                                     "name": "Pat"})
    monkeypatch.setattr(db, "create_quote",
                        lambda *a, **k: made.append((a, k)) or _quote(created_by="ai"))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    out = quote_autodraft.maybe_draft_for_log(TENANT, {}, "log-1", "pat@example.com")
    assert out is not None
    (args, kwargs) = made[0]
    assert [l["amount"] for l in args[3]] == [95.0]        # only the priced one
    assert kwargs["email_log_id"] == "log-1"
    assert kwargs["created_by"] == "ai" and kwargs["contact_id"] == "c1"
    assert pushes and pushes[0]["kind"] == "quote_drafted"  # propose mode cue

    # Dedupe: a live quote for this log means no second draft, ever.
    made.clear()
    monkeypatch.setattr(db, "quote_for_log", lambda l, t, client=None: _quote())
    assert quote_autodraft.maybe_draft_for_log(TENANT, {}, "log-1", "p@e.com") is None
    assert made == []


def test_autodraft_off_and_unpriced_do_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(db, "bookings_for_log",
                        lambda *a, **k: called.append(1) or [])
    assert quote_autodraft.maybe_draft_for_log(
        TENANT, {"quote_auto_draft": "off"}, "log-1", "p@e.com") is None
    assert called == []                                    # off = zero reads
    assert quote_autodraft.maybe_draft_for_log(TENANT, {}, "log-1", "p@e.com") is None


def test_autodraft_auto_mode_skips_the_review_push(monkeypatch):
    monkeypatch.setattr(db, "list_job_parties", lambda *a, **k: [])
    pushes = []
    monkeypatch.setattr(db, "bookings_for_log", lambda *a, **k: [
        {"id": "bk1", "job_type": "Repair", "quoted_amount": 50.0}])
    monkeypatch.setattr(db, "quote_for_log", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "create_quote", lambda *a, **k: _quote(created_by="ai"))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    out = quote_autodraft.maybe_draft_for_log(
        TENANT, {"quote_auto_draft": "auto"}, "log-1", "p@e.com")
    assert out is not None and pushes == []   # the send hook is the cue instead


def test_link_for_send_auto_mode_flips_and_links(monkeypatch):
    flips = []
    monkeypatch.setattr(db, "quote_for_log",
                        lambda l, t, client=None: _quote(status="draft"))
    monkeypatch.setattr(db, "set_quote_status",
                        lambda i, t, s, client=None: flips.append(s))
    out = quote_autodraft.link_for_send(TENANT, {"quote_auto_draft": "auto"}, "log-1")
    assert f"/q/{QID}" in out and "QUO-1001" in out
    assert flips == ["sent"]
    # Propose mode never touches the reply; nor does a quote already sent.
    assert quote_autodraft.link_for_send(TENANT, {}, "log-1") == ""
    monkeypatch.setattr(db, "quote_for_log",
                        lambda l, t, client=None: _quote(status="sent"))
    assert quote_autodraft.link_for_send(
        TENANT, {"quote_auto_draft": "auto"}, "log-1") == ""


def test_autodraft_failure_never_costs_the_reply(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "bookings_for_log", _boom)
    monkeypatch.setattr(db, "quote_for_log", _boom)
    assert quote_autodraft.maybe_draft_for_log(TENANT, {}, "log-1", "p@e.com") is None
    assert quote_autodraft.link_for_send(
        TENANT, {"quote_auto_draft": "auto"}, "log-1") == ""


def test_conversion_deposit_line_says_paid_only_when_paid():
    """Live 2026-09-29 (long-form rig run): the final invoice read 'Less deposit
    paid (INV-1002)' while INV-1002 was still unpaid. The credit is right (the
    two invoices together make the quote total) but the words must not lie."""
    from app import quoting
    quote = {"quote_ref": "QUO-1", "acceptance_snapshot": {"lines": [
        {"description": "Job", "amount": 1000.0, "quantity": 1, "unit_price": 1000.0}]}}
    base = {"invoice_ref": "INV-9", "subtotal": 250.0, "total": 250.0}
    unpaid = quoting.conversion_lines(quote, {**base, "status": "sent", "amount_paid": 0.0})
    assert unpaid[-1]["description"] == "Less deposit invoiced (INV-9)"
    assert unpaid[-1]["amount"] == -250.0
    paid = quoting.conversion_lines(quote, {**base, "status": "paid", "amount_paid": 250.0})
    assert paid[-1]["description"] == "Less deposit paid (INV-9)"
    part = quoting.conversion_lines(quote, {**base, "status": "sent", "amount_paid": 100.0})
    assert part[-1]["description"] == "Less deposit invoiced (INV-9)"
    dead = quoting.conversion_lines(quote, {**base, "status": "cancelled", "amount_paid": 0.0})
    assert len(dead) == 1


def test_chase_fourteen_day_rung_only_says_second_after_a_first():
    from app import invoice_chase
    assert "second" in invoice_chase.purpose_for(14, 1).lower()
    first = invoice_chase.purpose_for(14, 0)
    assert "first one sent" in first and "Do NOT call it a second" in first
    assert invoice_chase.purpose_for(7, 0) == invoice_chase.PURPOSES[7]
