"""Proactive invoice chasing (src/app/invoice_chase.py + the Money page lever).

The competitive research's sharpest money finding (2026-09-01): every app has
payment "reminders" — one template, fired blind — and none of them CHASES.
These tests pin the behaviours that make ours a chase:
  * the register ladder — tone escalates with lateness, statutory interest
    appears only at 30 days and never with an amount
  * stand-down — an inbound from the customer since the last touch silences it
  * deterministic dedupe ids — a re-run or double-click can't stage twice
  * the approval path stamps the chase trail (reminders_sent, CRM timeline)
  * the Money page shows the machinery and offers "Chase now"

Run: python -m pytest tests/test_invoice_chase.py -q
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import agent, db, invoice_chase, usage
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

TENANT = "11111111-1111-1111-1111-111111111111"
INV_ID = "22222222-2222-2222-2222-222222222222"


def _inv(**over) -> dict:
    base = {"id": INV_ID, "tenant_id": TENANT, "invoice_ref": "INV-1001",
            "customer_email": "velox@example.com", "customer_name": "Velox Velcee",
            "status": "overdue", "total": 180.0, "amount_paid": 0,
            "due_date": "2026-08-11", "reminders_sent": 0,
            "last_reminder_at": None, "sent_at": "2026-07-28T09:00:00+00:00"}
    return {**base, **over}


# ── the register ladder ───────────────────────────────────────────────────────
def test_register_escalates_with_lateness():
    assert invoice_chase.register_for(0) == 0        # chase-now, not yet a week
    assert invoice_chase.register_for(6) == 0
    assert invoice_chase.register_for(7) == 7
    assert invoice_chase.register_for(13) == 7
    assert invoice_chase.register_for(29) == 14
    assert invoice_chase.register_for(90) == 30


def test_statutory_interest_only_at_thirty_days_and_never_an_amount():
    # A legal-adjacent sentence with a wrong figure is worse than none: the
    # 30-day register may MENTION statutory interest, and must forbid amounts.
    assert "statutory interest" in invoice_chase.PURPOSES[30]
    assert "never quote an amount" in invoice_chase.PURPOSES[30]
    for milestone in (0, 7, 14):
        assert "statutory" not in invoice_chase.PURPOSES[milestone]


# ── linkage ids ───────────────────────────────────────────────────────────────
def test_source_id_round_trips_through_the_approval_parser():
    sid = invoice_chase.chase_source_id(INV_ID, 14)
    assert invoice_chase.invoice_id_from_source(sid) == INV_ID
    # Manual chases carry a per-day label; the uuid still parses out.
    sid2 = invoice_chase.chase_source_id(INV_ID, "now-2026-09-01")
    assert invoice_chase.invoice_id_from_source(sid2) == INV_ID
    assert invoice_chase.invoice_id_from_source("msg-abc-123") is None
    assert invoice_chase.invoice_id_from_source(None) is None


def test_chase_ref_reads_back_out_of_the_intent_label():
    assert invoice_chase.chase_ref_from_intent("[invoice chase] INV-1001") == "INV-1001"
    assert invoice_chase.chase_ref_from_intent("Re: your invoice") is None


# ── stage_chase ───────────────────────────────────────────────────────────────
def _wire(monkeypatch, *, inbound=False, created=True, can_pay=False,
          bank=True):
    from app import invoice_send, payments
    calls = {"inserted": [], "sent": [], "usage": [], "events": []}
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: (
        {"bank_sort_code": "20-45-45", "bank_account_number": "13371337"}
        if bank else {}))
    monkeypatch.setattr(db, "has_inbound_from_since", lambda *a, **k: inbound)
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: can_pay)
    monkeypatch.setattr(payments, "payment_url", lambda i: f"https://pay.example/{i}")

    async def fake_compose(cfg, purpose, facts, **kw):
        calls["purpose"], calls["facts"] = purpose, facts
        return SimpleNamespace(text="Please settle INV-1001 when you can.",
                               model="test-model", usage={})
    monkeypatch.setattr(agent, "compose_outbound", fake_compose)

    def fake_insert(tenant_id, frm, subject, body_in, draft, **kw):
        calls["inserted"].append({"to": frm, "subject": subject,
                                  "body_in": body_in, "draft": draft, **kw})
        return "log-1", created
    monkeypatch.setattr(db, "insert_email_log", fake_insert)
    monkeypatch.setattr(usage, "record_anthropic",
                        lambda *a, **k: calls["usage"].append(k))
    monkeypatch.setattr(db, "log_agent_event",
                        lambda t, kind, s, **k: calls["events"].append(s))

    async def fake_send(tenant_id, inv, text, **kw):
        calls["sent"].append({"text": text, **kw})
    monkeypatch.setattr(invoice_send, "send_reminder_email", fake_send)
    return calls


def test_no_customer_email_means_no_chase(monkeypatch):
    calls = _wire(monkeypatch)
    outcome, _ = asyncio.run(invoice_chase.stage_chase(
        TENANT, {}, _inv(customer_email=""), 8))
    assert outcome == "no_email" and calls["inserted"] == []


def test_an_inbound_reply_stands_the_chase_down(monkeypatch):
    # A robot nudge on top of a live conversation reads as not listening.
    calls = _wire(monkeypatch, inbound=True)
    outcome, _ = asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 8))
    assert outcome == "stood_down" and calls["inserted"] == []


def test_chase_lands_in_the_approval_queue(monkeypatch):
    calls = _wire(monkeypatch)
    outcome, log_id = asyncio.run(invoice_chase.stage_chase(
        TENANT, {}, _inv(), 8, dedupe_label=7))
    assert outcome == "drafted" and log_id == "log-1"
    row = calls["inserted"][0]
    assert row["kind"] == "invoice_reminder" and row["status"] == "pending"
    assert row["body_in"] == "[invoice chase] INV-1001"
    assert row["source_message_id"] == invoice_chase.chase_source_id(INV_ID, 7)
    assert calls["usage"] and calls["usage"][0]["operation"] == "reminder"
    assert calls["events"] and "waiting for your yes" in calls["events"][0]
    # No Stripe → the facts point at the bank details on the PDF instead.
    assert "Bank transfer details" in calls["facts"]


def test_no_payment_route_is_never_asserted(monkeypatch):
    # Live e2e 2026-09-01: no Stripe + no bank details, yet the chase claimed
    # "bank details are on the PDF". The facts must never invent a route.
    calls = _wire(monkeypatch, bank=False)
    asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 8))
    assert "Bank transfer details" not in calls["facts"]
    assert "Do not state how to pay" in calls["facts"]


def test_pay_link_rides_the_chase_when_stripe_is_connected(monkeypatch):
    calls = _wire(monkeypatch, can_pay=True)
    asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 8))
    # The composed text didn't include the link, so the guarantee appends it.
    assert f"https://pay.example/{INV_ID}" in calls["inserted"][0]["draft"]


def test_double_staging_is_physically_impossible(monkeypatch):
    calls = _wire(monkeypatch, created=False)
    outcome, _ = asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 8))
    assert outcome == "already_staged"
    assert calls["usage"] == [] and calls["events"] == []


def test_auto_send_goes_through_the_one_reminder_door(monkeypatch):
    # Auto tenants send now, via invoice_send.send_reminder_email — the single
    # place that stamps reminders_sent + the CRM timeline — with the EU AI Act
    # disclosure wrapped on.
    calls = _wire(monkeypatch)
    outcome, _ = asyncio.run(invoice_chase.stage_chase(
        TENANT, {"auto_send": True}, _inv(), 8, auto=True))
    assert outcome == "sent"
    assert calls["sent"] and calls["sent"][0]["via"] == "auto"
    assert calls["inserted"] == []      # no second email_log row


def test_escalation_register_reaches_the_composer(monkeypatch):
    calls = _wire(monkeypatch)
    asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 35))
    assert "statutory interest" in calls["purpose"]
    asyncio.run(invoice_chase.stage_chase(TENANT, {}, _inv(), 8))
    assert "oversight" in calls["purpose"]


# ── the Money page lever ──────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def _overdue_inv():
    return _inv(created_at="2026-07-28T09:00:00+00:00", issue_date="2026-07-28")


def test_money_page_states_the_chase_machinery(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [_overdue_inv()])
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases", lambda *a, **k: [])
    r = client.get("/app/money")
    assert r.status_code == 200
    assert "These chase themselves" in r.text
    assert f"/app/invoices/{INV_ID}/chase" in r.text          # the go-early lever
    assert "Chase now" in r.text


def test_money_page_points_at_a_staged_chase_instead_of_offering_one(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [_overdue_inv()])
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases", lambda *a, **k: [
        {"id": "log-1", "from_address": "velox@example.com",
         "body_in": "[invoice chase] INV-1001"}])
    r = client.get("/app/money")
    assert "review" in r.text and "/app/conversations/view?contact=velox%40example.com" in r.text
    assert f"/app/invoices/{INV_ID}/chase" not in r.text      # already staged


def _real_shaped_config(tenant_id, mailbox_id=None, client=None):
    # Signature-shaped stub: the live 500 of 2026-09-01 slipped through a loose
    # `lambda t, c` — this one rejects a client passed in the mailbox_id seat.
    assert mailbox_id is None or isinstance(mailbox_id, str), \
        "mailbox_id seat got a non-mailbox value"
    return {}


def test_chase_now_stages_a_draft_and_lands_on_it(client, monkeypatch):
    staged, stamps = [], []
    # Relative lateness, or the passage of real time walks this test across the
    # 30d milestone (the fixed-date trap this suite's docstring warns about).
    twenty_days_late = (date.today() - timedelta(days=20)).isoformat()
    monkeypatch.setattr(dashboard.db, "get_invoice",
                        lambda i, t, client=None: _inv(due_date=twenty_days_late))
    monkeypatch.setattr(dashboard.db, "effective_config", _real_shaped_config)
    monkeypatch.setattr(dashboard.db, "set_invoice_overdue_milestone",
                        lambda i, t, d, client=None: stamps.append(d))

    async def fake_stage(tenant_id, cfg, inv, days, **kw):
        staged.append(kw)
        return "drafted", "log-1"
    monkeypatch.setattr(invoice_chase, "stage_chase", fake_stage)
    r = client.post(f"/app/invoices/{INV_ID}/chase")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/app/conversations/view?contact=")
    assert staged[0]["auto"] is False                          # asked ≠ send
    assert str(staged[0]["dedupe_label"]).startswith("now-")   # one per day
    # A manual chase IS a chase (live, 2026-09-02: e2e chased at 21:22, the
    # 08:45 run re-drafted the same invoice) — it satisfies its milestone:
    # 20d late sits in the 14d register.
    assert stamps == [14]


def test_chase_now_reports_a_stand_down(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda i, t, client=None: _inv())
    monkeypatch.setattr(dashboard.db, "effective_config", _real_shaped_config)
    stamps = []
    monkeypatch.setattr(dashboard.db, "set_invoice_overdue_milestone",
                        lambda i, t, d, client=None: stamps.append(d))

    async def fake_stage(*a, **k):
        return "stood_down", None
    monkeypatch.setattr(invoice_chase, "stage_chase", fake_stage)
    r = client.post(f"/app/invoices/{INV_ID}/chase")
    assert r.status_code == 303 and "msg=chase_stood_down" in r.headers["location"]
    assert stamps == []       # nothing went out, so no milestone is satisfied


def test_chase_now_refuses_settled_invoices(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice",
                        lambda i, t, client=None: _inv(status="paid"))
    called = []

    async def fake_stage(*a, **k):
        called.append(1)
        return "drafted", "log-1"
    monkeypatch.setattr(invoice_chase, "stage_chase", fake_stage)
    r = client.post(f"/app/invoices/{INV_ID}/chase")
    assert r.status_code == 303 and called == []


# ── the approval path stamps the chase trail ──────────────────────────────────
def test_approving_a_chase_records_the_reminder(client, monkeypatch):
    from app import invoice_send
    recorded, activities = [], []
    entry = {"id": "log-1", "from_address": "velox@example.com",
             "subject": "Payment reminder — invoice INV-1001",
             "draft_reply": "Please settle up.", "kind": "invoice_reminder",
             "source_message_id": invoice_chase.chase_source_id(INV_ID, 7),
             "mailbox_id": None, "channel": "email"}
    monkeypatch.setattr(dashboard.db, "get_email_log_entry",
                        lambda i, t, client=None: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda i, t, client=None: _inv())
    monkeypatch.setattr(dashboard.db, "record_invoice_reminder",
                        lambda i, t, client=None: recorded.append(i))
    monkeypatch.setattr(invoice_send, "_crm_invoice_activity",
                        lambda t, inv, **k: activities.append(k))
    monkeypatch.setattr(dashboard.db, "set_email_log_review_seconds", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.suppression, "record_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.trace, "emit", lambda *a, **k: None)

    async def fake_send_mail(*a, **k):
        return None
    monkeypatch.setattr(dashboard.mailbox, "send_mail", fake_send_mail)

    async def fake_push(*a, **k):
        return None
    monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", fake_push)
    monkeypatch.setattr(dashboard.crm, "note_outbound_sent", lambda *a, **k: None)

    async def fake_intel(*a, **k):
        return None
    monkeypatch.setattr(dashboard.crm_intel, "note_outbound", fake_intel)
    r = client.post("/app/drafts/log-1/send",
                    data={"draft_reply": "Please settle up.", "return_to": "/app/money"})
    assert r.status_code in (200, 303)
    assert recorded == [INV_ID]
    assert activities and activities[0]["activity_type"] == "invoice_reminder"
