"""Invoicing & Payments enhancement (directive 2026-07-26) — the payment layer.

Pins the surfaces that matter:
  * Connect OAuth state is signed + expiring; the callback can't be forged
  * the public /pay/{id} page: capability lookup, viewed_at stamps once,
    bank-transfer fallback, no Pay Now without a connected account
  * /webhooks/stripe: signature-verified, deduped, routes ONLY metadata.kind
    'invoice', marks paid idempotently, runs the paid followup
  * payment-confirmation inbound → rule-based 'automated'/'fyi', linked by ref,
    never drafted; the manual-payment "mark it as paid?" surface
  * payment reminders never touch the draft-suppression counter
  * the overdue job: flip + one push per 7/14/30 milestone + the ≥3-reminders
    "hasn't paid" escalation
  * concierge invoice stagers/executors validate against real data
  * portal list_my_invoices is identity-gated and hides drafts
  * db.summarize_invoice_rows maths

Run: python -m pytest tests/test_invoice_payments.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, payments, pipeline, push
from app.config import settings
from app.main import app

TENANT = "11111111-1111-1111-1111-111111111111"
INV_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _connected_account(monkeypatch):
    """Webhook events must come from the business's own connected account
    (security review 2026-09, M9) — the fake business is connected to the
    account the fake events carry. A test that needs otherwise overrides it."""
    from app import db as _db
    monkeypatch.setattr(_db, "get_stripe_account_id", lambda *a, **k: "acct_1")


def _inv(**over) -> dict:
    base = {"id": INV_ID, "tenant_id": TENANT, "invoice_ref": "INV-1001",
            "customer_email": "velox@example.com", "customer_name": "Velox Velcee",
            "status": "sent", "subtotal": 180.0, "vat": 0.0, "total": 180.0,
            "amount_paid": 0, "due_date": "2026-08-11", "issue_date": "2026-07-28",
            "reminders_sent": 0, "overdue_notified_days": 0,
            "stripe_payment_intent_id": None, "deal_id": None, "contact_id": None,
            "viewed_at": None, "paid_at": None, "payment_method": None}
    return {**base, **over}


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    return TestClient(app, follow_redirects=False)


# ── Connect OAuth state ───────────────────────────────────────────────────────
def test_connect_state_round_trips_and_rejects_tampering(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", "test-key")
    state = payments._state_for(TENANT)
    assert payments._tenant_from_state(state) == TENANT
    # Flip the last hex digit — a fixed "ff" suffix was a no-op whenever the
    # real signature already ended in ff (1 second in 256), and the test failed.
    tampered = state[:-1] + ("0" if state[-1] != "0" else "1")
    assert payments._tenant_from_state(tampered) is None               # bad sig
    other = state.replace(TENANT, "33333333-3333-3333-3333-333333333333", 1)
    assert payments._tenant_from_state(other) is None                  # swapped tenant
    assert payments._tenant_from_state("") is None


def test_connect_state_expires(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", "test-key")
    state = payments._state_for(TENANT)
    assert payments._tenant_from_state(state, max_age_s=0) is None


def test_tenant_can_take_payments_short_circuits_without_platform_keys(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "")
    # Never touches the DB when Connect isn't configured (conftest would raise).
    assert payments.tenant_can_take_payments(TENANT) is False


# ── the public pay page ───────────────────────────────────────────────────────
def test_pay_page_stamps_viewed_once_and_shows_bank_fallback(web, monkeypatch):
    stamped = []
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: _inv())
    monkeypatch.setattr(db, "set_invoice_viewed", lambda i, t: stamped.append(i) or True)
    monkeypatch.setattr(db, "get_tenant_config", lambda t: {})
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {
        "business_name": "Thornbeck Ltd", "bank_sort_code": "01-02-03",
        "bank_account_number": "12345678"})
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "")
    r = web.get(f"/pay/{INV_ID}")
    assert r.status_code == 200
    assert "INV-1001" in r.text and "01-02-03" in r.text
    assert "Reference</span> <strong>INV-1001</strong>" in r.text   # bank ref = the invoice number
    assert "Pay £" not in r.text                  # no Stripe → no Pay Now button
    assert stamped == [INV_ID]


def test_pay_page_always_reports_the_view_and_lets_settings_decide(web, monkeypatch):
    """The opt-in used to wrap this call, so an opted-out tenant got no push AND
    no feed row — no record their customer had ever opened the invoice (Kris
    2026-09-04). The page reports the view either way now; src/app/notify_prefs.py
    decides whether it earns a buzz."""
    from app import notify_prefs

    sent = []
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: _inv())
    monkeypatch.setattr(db, "set_invoice_viewed", lambda i, t: True)
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: None)
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "")
    monkeypatch.setattr(push, "notify", lambda *a, **k: sent.append(k))

    monkeypatch.setattr(db, "get_tenant_config", lambda t: {"notify_invoice_viewed": False})
    web.get(f"/pay/{INV_ID}")
    assert len(sent) == 1 and sent[0]["kind"] == "invoice_viewed", \
        "the view is recorded whatever the setting"

    monkeypatch.setattr(db, "get_tenant_config", lambda t: {"notify_invoice_viewed": True})
    web.get(f"/pay/{INV_ID}")
    assert len(sent) == 2

    # …and the setting is what decides the interruption. Off by default here:
    # an invoice being opened rarely needs anything doing.
    assert notify_prefs.should_push({}, "invoice_viewed") is False
    assert notify_prefs.should_push({"notify_invoice_viewed": False},
                                    "invoice_viewed") is False
    assert notify_prefs.should_push({"notify_invoice_viewed": True},
                                    "invoice_viewed") is True


def test_pay_page_404s_drafts_and_unknown(web, monkeypatch):
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: None)
    assert web.get(f"/pay/{INV_ID}").status_code == 404
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: _inv(status="draft"))
    assert web.get(f"/pay/{INV_ID}").status_code == 404   # never issued → not public


def test_checkout_without_stripe_redirects_back(web, monkeypatch):
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: _inv())
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "")
    r = web.post(f"/pay/{INV_ID}/checkout")
    assert r.status_code == 303 and r.headers["location"] == f"/pay/{INV_ID}"


# ── the Connect webhook ───────────────────────────────────────────────────────
def _hook(event):
    return patch("app.payments.stripe.Webhook.construct_event", return_value=event)


@pytest.fixture
def hook_web(web, monkeypatch):
    monkeypatch.setattr(settings, "stripe_connect_webhook_secret", "whsec_test")
    return web


def test_webhook_503_when_unconfigured(web, monkeypatch):
    monkeypatch.setattr(settings, "stripe_connect_webhook_secret", "")
    assert web.post("/webhooks/stripe", content=b"{}").status_code == 503


def test_webhook_marks_invoice_paid_and_runs_followup(hook_web, monkeypatch):
    evt = {"id": "evt_pay1", "type": "payment_intent.succeeded", "account": "acct_1",
           "data": {"object": {"id": "pi_1", "payment_method_types": ["card"],
                    "metadata": {"kind": "invoice", "tenant_id": TENANT,
                                 "invoice_id": INV_ID, "invoice_ref": "INV-1001"}}}}
    paid, followed = [], []
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    monkeypatch.setattr(db, "mark_invoice_paid",
                        lambda i, t, **k: paid.append((i, t, k)) or _inv(status="paid"))
    monkeypatch.setattr(payments, "invoice_paid_followup",
                        lambda t, inv, via: followed.append(via))
    with _hook(evt):
        r = hook_web.post("/webhooks/stripe", content=b"{}",
                          headers={"stripe-signature": "t=0,v1=x"})
    assert r.status_code == 200
    assert paid and paid[0][0] == INV_ID and paid[0][2]["payment_method"] == "card"
    assert paid[0][2]["stripe_payment_intent_id"] == "pi_1"
    assert followed == ["paid online via Stripe"]


def test_webhook_replay_is_idempotent_at_the_invoice(hook_web, monkeypatch):
    """Even if the event-id wall let a duplicate through, mark_invoice_paid's
    status-guarded update returns None and the followup must NOT re-fire."""
    evt = {"id": "evt_pay2", "type": "payment_intent.succeeded",
           "data": {"object": {"id": "pi_1", "metadata": {
               "kind": "invoice", "tenant_id": TENANT, "invoice_id": INV_ID}}}}
    followed = []
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    monkeypatch.setattr(db, "mark_invoice_paid", lambda *a, **k: None)  # already paid
    monkeypatch.setattr(payments, "invoice_paid_followup",
                        lambda *a, **k: followed.append(1))
    with _hook(evt):
        r = hook_web.post("/webhooks/stripe", content=b"{}",
                          headers={"stripe-signature": "t=0,v1=x"})
    assert r.status_code == 200 and followed == []


def test_webhook_ignores_non_invoice_events(hook_web, monkeypatch):
    evt = {"id": "evt_dep", "type": "payment_intent.succeeded",
           "data": {"object": {"metadata": {"kind": "deposit", "tenant_id": TENANT}}}}
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    with _hook(evt):
        r = hook_web.post("/webhooks/stripe", content=b"{}",
                          headers={"stripe-signature": "t=0,v1=x"})
    assert r.status_code == 200 and r.text == "ignored"


def test_webhook_duplicate_event_id_short_circuits(hook_web, monkeypatch):
    evt = {"id": "evt_dup", "type": "payment_intent.succeeded",
           "data": {"object": {"metadata": {"kind": "invoice", "tenant_id": TENANT,
                                            "invoice_id": INV_ID}}}}
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: False)
    with _hook(evt):
        r = hook_web.post("/webhooks/stripe", content=b"{}",
                          headers={"stripe-signature": "t=0,v1=x"})
    assert r.text == "duplicate"


def test_webhook_payment_failed_pushes_and_logs(hook_web, monkeypatch):
    evt = {"id": "evt_fail", "type": "payment_intent.payment_failed", "account": "acct_1",
           "data": {"object": {"last_payment_error": {"message": "card declined"},
                    "metadata": {"kind": "invoice", "tenant_id": TENANT,
                                 "invoice_id": INV_ID, "invoice_ref": "INV-1001"}}}}
    events, pushes = [], []
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    monkeypatch.setattr(db, "log_agent_event",
                        lambda t, kind, s, **k: events.append((kind, s)))
    monkeypatch.setattr(db, "get_invoice", lambda i, t, **k: _inv())
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    with _hook(evt):
        r = hook_web.post("/webhooks/stripe", content=b"{}",
                          headers={"stripe-signature": "t=0,v1=x"})
    assert r.status_code == 200
    assert events and events[0][0] == "invoice_payment_failed"
    assert "card declined" in events[0][1]
    assert pushes and pushes[0]["kind"] == "invoice_payment_failed"


def test_paid_followup_pushes_and_nudges_deal(monkeypatch):
    pushes, msgs, acts = [], [], []
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity",
                        lambda *a, **k: acts.append(k) or True)
    monkeypatch.setattr(db, "add_concierge_message",
                        lambda t, role, content, **k: msgs.append(content))
    payments.invoice_paid_followup(TENANT, _inv(status="paid", deal_id="d1"),
                                   via="test")
    assert pushes and pushes[0]["kind"] == "invoice_paid"
    assert "£180.00" in pushes[0]["title"] or "£180.00" in pushes[0]["body"] \
        or "INV-1001" in pushes[0]["title"]
    assert acts and acts[0]["activity_type"] == "invoice_paid"
    assert msgs and "Won" in msgs[0]               # the confirm-first nudge


# ── payment-confirmation inbound (pipeline §5.1) ──────────────────────────────
def test_payment_confirmation_detection():
    f = pipeline.looks_like_payment_confirmation
    assert f("receipts@stripe.com", "Receipt", "…")
    assert f("service@paypal.co.uk", "anything", "")
    assert f("bank@bigbank.com", "Payment received", "")
    assert f("customer@gmail.com", "Re: quote", "payment confirmation attached")
    assert not f("customer@gmail.com", "Can you fit me in Tuesday?", "hi there")
    assert pipeline.find_invoice_ref("Payment for INV-1001", "") == "INV-1001"
    assert pipeline.find_invoice_ref("re: inv-1002 settled", "") == "INV-1002"
    assert pipeline.find_invoice_ref("no ref here", "none") is None


def _run_pipeline(email, monkeypatch, logged, events, pushes):
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: logged.append((a, k)) or ("log1", True))
    monkeypatch.setattr(db, "log_agent_event",
                        lambda t, kind, s, **k: events.append((kind, s)))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    return asyncio.run(pipeline.draft_for_email(TENANT, email))


def test_stripe_receipt_categorised_automated_and_linked(monkeypatch):
    logged, events, pushes = [], [], []
    monkeypatch.setattr(db, "get_invoice_by_ref", lambda t, r, client=None:
                        _inv() if r == "INV-1001" else None)
    out = _run_pipeline({"from_address": "receipts@stripe.com",
                         "subject": "Payment for INV-1001",
                         "body": "£180.00 received"}, monkeypatch,
                        logged, events, pushes)
    assert out is None                              # never drafts, zero tokens
    _, kw = logged[0]
    assert kw["category"] == "automated" and kw["status"] == "skipped"
    assert "INV-1001" in kw["category_reason"]
    # webhook didn't settle it (no stripe_payment_intent_id) → surface mark-as-paid
    assert events and events[0][0] == "invoice_payment_seen"
    assert pushes and pushes[0]["kind"] == "invoice_payment_seen"


def test_receipt_without_matching_invoice_files_as_fyi(monkeypatch):
    logged, events, pushes = [], [], []
    monkeypatch.setattr(db, "get_invoice_by_ref", lambda *a, **k: None)
    _run_pipeline({"from_address": "receipts@stripe.com",
                   "subject": "Your receipt", "body": "thanks"},
                  monkeypatch, logged, events, pushes)
    _, kw = logged[0]
    assert kw["category"] == "fyi" and kw["status"] == "skipped"
    assert events == [] and pushes == []            # nothing to confirm


def test_already_settled_invoice_stays_quiet(monkeypatch):
    logged, events, pushes = [], [], []
    monkeypatch.setattr(db, "get_invoice_by_ref", lambda *a, **k:
                        _inv(status="paid", stripe_payment_intent_id="pi_1"))
    _run_pipeline({"from_address": "receipts@stripe.com",
                   "subject": "Payment for INV-1001", "body": ""},
                  monkeypatch, logged, events, pushes)
    assert events == [] and pushes == []            # webhook already handled it


# ── reminders never touch the suppression counter (§5.3) ─────────────────────
def test_reminder_send_never_touches_suppression_counter(monkeypatch):
    from app import invoice_send, suppression
    counted = []
    monkeypatch.setattr(suppression, "record_draft_generated",
                        lambda *a, **k: counted.append(1))
    sent = {}

    async def fake_send(t, to, subject, body, **k):
        sent.update(to=to, subject=subject)
    monkeypatch.setattr(invoice_send.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(db, "record_invoice_reminder", lambda *a, **k: None)
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("l1", True))
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda *a, **k: True)
    asyncio.run(invoice_send.send_reminder_email(TENANT, _inv(), "Please pay!"))
    assert sent["to"] == "velox@example.com"
    assert "INV-1001" in sent["subject"]
    assert counted == []      # chasing money ≠ ignored conversation drafts


# ── the overdue job (§6.1) — now the proactive chase engine ──────────────────
def _job_run(rows, monkeypatch, outcome="drafted", auto_send=False):
    from app import invoice_chase
    from app.jobs import check_overdue_invoices as job
    stamped, events, pushes, chased = [], [], [], []
    monkeypatch.setattr(db, "list_unpaid_due_invoices", lambda today: rows)
    monkeypatch.setattr(db, "set_invoice_overdue_milestone",
                        lambda i, t, d: stamped.append((i, d)))
    monkeypatch.setattr(db, "effective_config",
                        lambda t, c: {"auto_send": auto_send})
    monkeypatch.setattr(db, "log_agent_event",
                        lambda t, kind, s, **k: events.append(s))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))

    async def fake_stage(tenant_id, cfg, inv, days, **kw):
        chased.append({"invoice": inv["id"], "days": days,
                       "label": kw.get("dedupe_label"), "auto": kw.get("auto")})
        return outcome, "log-1"
    monkeypatch.setattr(invoice_chase, "stage_chase", fake_stage)
    return asyncio.run(job.run()), stamped, events, pushes, chased


def _due(days_ago: int, **over):
    d = (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()
    return _inv(due_date=d, **over)


def test_overdue_job_flips_without_push_before_first_milestone(monkeypatch):
    out, stamped, events, pushes, chased = _job_run([_due(3)], monkeypatch)
    assert stamped == [(INV_ID, 0)] and pushes == [] and chased == []
    assert out["flipped"] == 1 and out["notified"] == 0


def test_overdue_job_pushes_each_milestone_once(monkeypatch):
    out, stamped, events, pushes, chased = _job_run([_due(8)], monkeypatch)
    assert stamped == [(INV_ID, 7)]
    assert len(pushes) == 1 and pushes[0]["kind"] == "invoice_overdue"
    # Next day: milestone 7 already notified → silent.
    out2, stamped2, _, pushes2, chased2 = _job_run(
        [_due(9, status="overdue", overdue_notified_days=7)], monkeypatch)
    assert pushes2 == [] and stamped2 == [] and chased2 == []
    # Day 30 crossing from 14 → one more.
    out3, stamped3, _, pushes3, _ = _job_run(
        [_due(31, status="overdue", overdue_notified_days=14)], monkeypatch)
    assert stamped3 == [(INV_ID, 30)] and len(pushes3) == 1


def test_overdue_job_drafts_the_chase_itself(monkeypatch):
    # Proactive doctrine: the milestone STAGES the chase — the push reports
    # what was done and points at the draft, not at work for the owner to do.
    out, _, events, pushes, chased = _job_run([_due(8)], monkeypatch)
    assert chased == [{"invoice": INV_ID, "days": 8, "label": 7, "auto": False}]
    assert out["drafted"] == 1
    assert "chase drafted" in pushes[0]["title"].lower()
    assert pushes[0]["url"].startswith("/app/conversations/view?contact=")
    assert "waiting for your yes" in events[0]


def test_overdue_job_auto_send_tenants_send_immediately(monkeypatch):
    out, _, _, pushes, chased = _job_run([_due(8)], monkeypatch,
                                         outcome="sent", auto_send=True)
    assert chased[0]["auto"] is True and out["sent"] == 1
    assert pushes[0]["title"].startswith("Chased")


def test_overdue_job_stands_down_when_the_customer_replied(monkeypatch):
    # An inbound since the last touch means the conversation owns the thread —
    # the milestone falls back to a plain overdue push, no robot nudge on top.
    out, _, _, pushes, chased = _job_run([_due(8)], monkeypatch,
                                         outcome="stood_down")
    assert len(chased) == 1 and out["drafted"] == 0 and out["sent"] == 0
    assert "been in touch" in pushes[0]["body"]


def test_overdue_job_caps_the_burst_per_tenant(monkeypatch):
    # A fresh Xero import can land dozens of overdue invoices at once — the
    # morning run touches at most MAX_TENANT_TOUCHES per tenant, OLDEST first,
    # and leaves the rest unstamped so later runs pick them up.
    rows = [_inv(id=f"i{n}", invoice_ref=f"INV-{n}",
                 due_date=(datetime.now(timezone.utc).date()
                           - timedelta(days=8 + n)).isoformat())
            for n in range(7)]
    out, stamped, _, pushes, chased = _job_run(rows, monkeypatch)
    assert out["deferred"] == 2
    assert len(chased) == 5 and len(pushes) == 5
    assert len(stamped) == 5                       # deferred rows left unstamped
    # Oldest due dates got today's touches.
    assert [c["invoice"] for c in chased] == ["i6", "i5", "i4", "i3", "i2"]


def test_overdue_job_escalates_after_three_reminders(monkeypatch):
    _, _, events, pushes, chased = _job_run([_due(14, reminders_sent=3)],
                                            monkeypatch)
    assert chased == []          # no fourth email — this is a phone call
    assert events and "hasn't responded to 3 payment reminders" in events[0]
    assert "escalate" in events[0].lower()
    assert pushes and "hasn't paid" in pushes[0]["title"]


# ── concierge stagers/executors ───────────────────────────────────────────────
def test_stage_invoice_create_validates_and_shapes(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k:
                        {"id": "c1", "name": "Velox Velcee"})
    summary, payload, details, draft = ca._stage_invoice_create(
        TENANT, None, {"contact": "velox@example.com",
                       "title": "Annual service (standard)", "amount": "£180"})
    assert "£180.00" in summary and payload["contact_id"] == "c1"
    assert payload["amount"] == 180.0
    assert draft is None
    with pytest.raises(ca.ActionError, match="email"):
        ca._stage_invoice_create(TENANT, None, {"contact": "velox", "amount": "180"})
    with pytest.raises(ca.ActionError):
        ca._stage_invoice_create(TENANT, None, {"contact": "v@x.co", "amount": "loads"})


def test_stage_invoice_send_requires_sendable_state(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: _inv(status="paid"))
    with pytest.raises(ca.ActionError, match="paid"):
        ca._stage_invoice_send(TENANT, None, {"invoice_id": INV_ID})
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None:
                        _inv(customer_email=""))
    with pytest.raises(ca.ActionError, match="email"):
        ca._stage_invoice_send(TENANT, None, {"invoice_id": INV_ID})


def test_resolve_invoice_accepts_the_human_ref(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(db, "get_invoice_by_ref", lambda t, r, client=None:
                        _inv() if r == "INV-1001" else None)
    assert ca._resolve_invoice(TENANT, None, "inv-1001")["id"] == INV_ID
    with pytest.raises(ca.ActionError):
        ca._resolve_invoice(TENANT, None, "INV-9999")


def test_exec_invoice_mark_paid_runs_followup(monkeypatch):
    from app import concierge_actions as ca
    followed = []
    monkeypatch.setattr(db, "mark_invoice_paid",
                        lambda i, t, **k: _inv(status="paid"))
    monkeypatch.setattr(payments, "invoice_paid_followup",
                        lambda t, inv, via: followed.append(via))
    out = asyncio.run(ca._exec_invoice_mark_paid(
        TENANT, None, {"invoice_id": INV_ID, "ref": "INV-1001",
                       "total": 180.0, "payment_method": "bank_transfer"}, None))
    assert "INV-1001" in out["summary"] and followed
    # Already settled → honest refusal, no followup double-fire.
    monkeypatch.setattr(db, "mark_invoice_paid", lambda *a, **k: None)
    with pytest.raises(ca.ActionError, match="already"):
        asyncio.run(ca._exec_invoice_mark_paid(
            TENANT, None, {"invoice_id": INV_ID, "ref": "INV-1001",
                           "total": 180.0, "payment_method": "cash"}, None))


def test_stage_reminder_flags_phantom_pay_now_claims(monkeypatch):
    """Live e2e 2026-07-26: with no Stripe connected the model wrote 'use the Pay
    Now link' into a reminder — the card must warn so the owner edits it."""
    from app import concierge_actions as ca
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None:
                        _inv(status="overdue"))
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    _, _, details, draft = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID,
                       "draft_text": "Hi Dave, you can settle via the Pay Now link."})
    assert any("Stripe isn't" in d for d in details)     # loud warning on the card
    assert "pay online here" not in draft.lower()        # and no phantom link appended
    # Clean text → no warning.
    _, _, details2, _ = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID,
                       "draft_text": "Hi Dave, a gentle nudge about INV-1001."})
    assert not any("Stripe" in d for d in details2)


def test_stage_reminder_carries_editable_draft_with_pay_link(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None:
                        _inv(status="overdue", reminders_sent=1))
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: True)
    monkeypatch.setattr(settings, "app_base_url", "https://blam8.com")
    summary, payload, details, draft = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID,
                       "draft_text": "Hi Velox, gentle nudge about INV-1001."})
    assert draft and f"https://blam8.com/pay/{INV_ID}" in draft
    assert any("#2" in d for d in details)          # reminder count surfaced


# ── portal ────────────────────────────────────────────────────────────────────
def test_portal_invoices_identity_gated_and_hides_drafts(monkeypatch):
    from app import portal_data
    session = portal_data.PortalSession(tenant_id=TENANT, business_name="T")
    out = portal_data._list_my_invoices(session, {})
    assert out["error"] == "not_identified"
    session.contact = {"email": "velox@example.com", "name": "Velox"}
    monkeypatch.setattr(db, "invoices_for_contact", lambda t, e: [
        _inv(), _inv(id="d1", status="draft"), _inv(id="p1", status="paid",
                                                   paid_at="2026-07-20T10:00:00Z")])
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t: [
        {"description": "Annual service", "amount": 180.0}])
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: True)
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    monkeypatch.setattr(settings, "app_base_url", "https://blam8.com")
    out = portal_data._list_my_invoices(session, {})
    refs = [i["ref"] for i in out["invoices"]]
    assert len(out["invoices"]) == 2               # the draft never shows
    assert out["total_outstanding"] == 180.0
    assert "paid online" in out["payment_note"]
    payable = out["invoices"][0]
    assert payable["pay_url"] == f"https://blam8.com/pay/{INV_ID}"
    assert payable["pay_online"] is True
    paid = out["invoices"][1]
    assert paid["pay_url"] is None and paid["paid_on"] == "2026-07-20"


def test_portal_payment_note_never_promises_a_missing_route(monkeypatch):
    # Live e2e 2026-09-01: no Stripe + no bank details, and the portal told the
    # customer the pay page had "bank details". The tool now states the truth.
    from app import portal_data
    session = portal_data.PortalSession(tenant_id=TENANT, business_name="T")
    session.contact = {"email": "velox@example.com", "name": "Velox"}
    monkeypatch.setattr(db, "invoices_for_contact", lambda t, e: [_inv()])
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t: [])
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    monkeypatch.setattr(settings, "app_base_url", "https://blam8.com")
    out = portal_data._list_my_invoices(session, {})
    assert "No online payment or bank details" in out["payment_note"]
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {
        "bank_sort_code": "20-45-45", "bank_account_number": "13371337"})
    out2 = portal_data._list_my_invoices(session, {})
    assert "bank transfer" in out2["payment_note"]


# ── the shared sender ─────────────────────────────────────────────────────────
def test_send_invoice_email_stamps_link_only_when_stripe_connected(monkeypatch):
    from app import invoice_send
    sent, links, statuses = {}, [], []

    async def fake_send(t, to, subject, body, attachments=None, **k):
        sent.update(to=to, subject=subject, body=body, att=attachments)
    monkeypatch.setattr(invoice_send.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(db, "list_invoice_lines", lambda *a, **k: [
        {"description": "Annual service", "amount": 180.0, "date": "2026-07-20"}])
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {
        "bank_sort_code": "01-02-03", "bank_account_number": "12345678"})
    monkeypatch.setattr(db, "set_invoice_payment_link",
                        lambda i, t, u, client=None: links.append(u))
    monkeypatch.setattr(db, "set_invoice_status",
                        lambda i, t, s, client=None: statuses.append(s))
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("l1", True))
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda *a, **k: True)

    # No Stripe → bank details in the body, no payment_link stamped.
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    asyncio.run(invoice_send.send_invoice_email(TENANT, _inv()))
    assert "01-02-03" in sent["body"] and links == [] and statuses == ["sent"]
    assert sent["att"][0][0] == "INV-1001.pdf" and sent["att"][0][1][:4] == b"%PDF"
    # Customer emails read the date like a person (ui-date convention, 09-01).
    assert "due 11th Aug 26" in sent["body"]

    # Stripe connected → Pay Now link in body + stamped on the invoice.
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: True)
    monkeypatch.setattr(settings, "app_base_url", "https://blam8.com")
    asyncio.run(invoice_send.send_invoice_email(TENANT, _inv()))
    assert links == [f"https://blam8.com/pay/{INV_ID}"]
    assert f"https://blam8.com/pay/{INV_ID}" in sent["body"]


# ── reporting maths ───────────────────────────────────────────────────────────
def test_summarize_invoice_rows_buckets_correctly():
    today = datetime.now(timezone.utc).date()
    rows = [
        {"status": "paid", "total": 100, "amount_paid": 100,
         "sent_at": "2026-07-01T10:00:00+00:00", "paid_at": "2026-07-05T10:00:00+00:00"},
        {"status": "sent", "total": 50, "amount_paid": 0,
         "due_date": (today + timedelta(days=5)).isoformat()},
        {"status": "overdue", "total": 30, "amount_paid": 0,
         "due_date": (today - timedelta(days=10)).isoformat()},
        {"status": "draft", "total": 999},          # drafts don't count
        {"status": "cancelled", "total": 999},
    ]
    s = db.summarize_invoice_rows(rows)
    assert s["paid"] == 1 and s["sent"] == 2 and s["overdue"] == 1
    assert s["invoiced"] == 180.0 and s["collected"] == 100.0
    assert s["outstanding"] == 80.0 and s["avg_days_to_pay"] == 4.0


def test_pay_page_shows_the_lines_totals_and_a_labelled_bank_box(web, monkeypatch):
    """Kris 2026-09-04: it was a bare monospace bank block under one line."""
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: {**_inv(), "subtotal": 160.0, "total": 160.0,
                                                                  "issue_date": "2026-09-04"})
    monkeypatch.setattr(db, "set_invoice_viewed", lambda i, t: True)
    monkeypatch.setattr(db, "get_tenant_config", lambda t: {})
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t, client=None: [
        {"description": "Remedial bar shoeing, front feet — Murphy", "date": "2026-09-07", "amount": 160.0}])
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {
        "business_name": "Tanner & Vale Farriery", "business_address": "Unit 3, Wells",
        "bank_account_name": "Tanner & Vale Farriery", "bank_sort_code": "04-00-04",
        "bank_account_number": "12345678", "footer_note": "Registered farriers"})
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "")
    r = web.get(f"/pay/{INV_ID}")
    assert r.status_code == 200
    assert "<h1>Tanner &amp; Vale Farriery</h1>" in r.text and "Unit 3, Wells" in r.text
    assert "Remedial bar shoeing, front feet — Murphy" in r.text
    assert "Total" in r.text and "£160.00" in r.text
    assert "Pay by bank transfer" in r.text and "Sort code</span> 04-00-04" in r.text
    assert "<strong>INV-1001</strong>" in r.text        # the reference stands out
    assert "Registered farriers" in r.text
    assert "<pre>" not in r.text                        # no monospace block any more
