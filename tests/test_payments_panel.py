"""Card payments (docs/plan-payments-panel.md) — the panel, refunds, pay-all, bank methods.

Pins the surfaces that matter:
  * the pure shaping: method/status/fee maths, today-vs-yesterday by London day,
    the 30-day summary, next payout, the bank-methods threshold
  * load(): four reads, concurrent, cached a minute, degrading per read
  * /app/payments renders the connect card or the figures
  * /app/payments/refund: refunds ON the connected account, then the shared refund
    lifecycle (invoice row, push, agent log, CRM activity) keyed for idempotency
  * db.record_invoice_refund: absolute amounts, full → 'refunded', replay → None
  * the Connect webhook: invoice_batch settles every invoice, processing marks a
    bank payment in progress, charge.refunded lands a dashboard refund by the delta,
    charge.dispute.created reads the charge and announces
  * the pay page: the pay-all offer, the in-progress state
  * /pay/{id}/checkout-all: one session, a line per invoice, the method threshold
  * /app/payments/methods saves the threshold into invoice_settings.extras

Run: python -m pytest tests/test_payments_panel.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import dashboard, db, invoice_config, payments, payments_panel, push
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.main import app
from app.security import require_csrf

TENANT = "11111111-1111-1111-1111-111111111111"
INV_ID = "22222222-2222-2222-2222-222222222222"
INV2_ID = "33333333-3333-3333-3333-333333333333"
ACCT = "acct_test1"


@pytest.fixture(autouse=True)
def _connected_account(monkeypatch):
    """Webhook events must come from the business's own connected account
    (security review 2026-09, M9) — the fake business is connected to the
    account the fake events carry. A test that needs otherwise overrides it."""
    from app import db as _db
    monkeypatch.setattr(_db, "get_stripe_account_id", lambda *a, **k: ACCT)


def _inv(**over) -> dict:
    base = {"id": INV_ID, "tenant_id": TENANT, "invoice_ref": "INV-1001",
            "customer_email": "velox@example.com", "customer_name": "Velox Velcee",
            "status": "sent", "subtotal": 180.0, "vat": 0.0, "total": 180.0,
            "amount_paid": 0, "due_date": "2026-08-11", "issue_date": "2026-07-28",
            "reminders_sent": 0, "overdue_notified_days": 0, "notes": "",
            "stripe_payment_intent_id": None, "deal_id": None, "contact_id": None,
            "viewed_at": "2026-07-29T10:00:00+00:00", "paid_at": None, "payment_method": None}
    return {**base, **over}


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)      # 11:00 London (BST)


def _charge(**over) -> dict:
    base = {"id": "ch_1", "payment_intent": "pi_1", "amount": 18000, "amount_refunded": 0,
            "status": "succeeded", "refunded": False, "disputed": False,
            "created": _ts(NOW - timedelta(hours=1)),
            "balance_transaction": {"fee": 272},
            "billing_details": {"name": "Velox Velcee", "email": "velox@example.com"},
            "payment_method_details": {"type": "card", "card": {"wallet": None}},
            "metadata": {"kind": "invoice", "tenant_id": TENANT, "invoice_id": INV_ID,
                         "invoice_ref": "INV-1001"}}
    return {**base, **over}


# ── pure shaping ──────────────────────────────────────────────────────────────
def test_method_of_prefers_the_wallet_over_card():
    assert payments_panel.method_of(_charge()) == "card"
    apple = _charge(payment_method_details={"type": "card", "card": {"wallet": {"type": "apple_pay"}}})
    assert payments_panel.method_of(apple) == "apple_pay"
    assert payments_panel.method_of(_charge(payment_method_details={"type": "bacs_debit"})) == "bacs_debit"
    assert payments_panel.method_label("bacs_debit") == "Bacs Direct Debit"
    assert payments_panel.method_label("something_new") == "Something New"


def test_status_of_reads_in_the_order_that_matters():
    assert payments_panel.status_of(_charge()) == "paid"
    assert payments_panel.status_of(_charge(amount_refunded=100)) == "partly refunded"
    assert payments_panel.status_of(_charge(refunded=True, amount_refunded=18000)) == "refunded"
    assert payments_panel.status_of(_charge(disputed=True, refunded=True)) == "disputed"
    assert payments_panel.status_of(_charge(status="pending")) == "in progress"


def test_shape_charge_carries_fee_net_and_the_invoice_link():
    row = payments_panel.shape_charge(_charge())
    assert row["amount"] == 180.0 and row["fee"] == 2.72 and row["net"] == 177.28
    assert row["invoice_id"] == INV_ID and row["invoice_refs"] == "INV-1001"
    assert row["refundable"] is True and row["stripe_url"].endswith("/payments/pi_1")
    # No balance transaction yet (a bank payment clearing): fee and net unknown, not £0.
    row = payments_panel.shape_charge(_charge(balance_transaction=None, status="pending"))
    assert row["fee"] is None and row["net"] is None and row["refundable"] is False
    batch = payments_panel.shape_charge(_charge(metadata={
        "kind": "invoice_batch", "tenant_id": TENANT, "invoice_ids": f"{INV_ID},{INV2_ID}",
        "invoice_refs": "INV-1001, INV-1002"}))
    assert batch["invoice_id"] is None and batch["invoice_ids"] == [INV_ID, INV2_ID]


def test_bucket_today_counts_by_the_london_day():
    today_early = _charge(created=_ts(datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)))  # 00:30 BST today
    yesterday = _charge(created=_ts(datetime(2026, 9, 16, 23, 30, tzinfo=timezone.utc)))    # 00:30 BST yesterday
    older = _charge(created=_ts(NOW - timedelta(days=3)))
    pending = _charge(status="pending", balance_transaction=None)
    rows = [payments_panel.shape_charge(c) for c in (today_early, yesterday, older, pending)]
    t = payments_panel.bucket_today(rows, NOW)
    assert t == {"today": 180.0, "today_count": 1, "yesterday": 180.0, "yesterday_count": 1}


def test_summarize_totals_fees_and_flags_unsettled_ones():
    rows = [payments_panel.shape_charge(c) for c in (
        _charge(), _charge(amount=5000, balance_transaction={"fee": 90}, amount_refunded=1000),
        _charge(status="pending", balance_transaction=None),          # not counted
        _charge(amount=2000, balance_transaction=None),               # counted, fee unknown
    )]
    w = payments_panel.summarize(rows)
    assert w["count"] == 3 and w["collected"] == 250.0 and w["fees"] == 3.62
    assert w["refunded"] == 10.0 and w["unfeed"] == 1 and w["blam8_cut"] == 0.0
    assert w["net"] == round(250.0 - 3.62 - 10.0, 2)


def test_next_payout_is_the_soonest_not_yet_landed():
    mk = lambda i, status, days: payments_panel.shape_payout(
        {"id": i, "amount": 1000 * i, "status": status, "created": _ts(NOW),
         "arrival_date": _ts(NOW + timedelta(days=days)), "destination": {"last4": "4321"}})
    payouts = [mk(1, "paid", -2), mk(2, "in_transit", 3), mk(3, "pending", 1), mk(4, "failed", 0)]
    nxt = payments_panel.next_payout(payouts)
    assert nxt["id"] == 3 and nxt["amount"] == 30.0 and nxt["bank"] == "4321"
    assert payments_panel.next_payout([mk(1, "paid", -2)]) is None


def test_balance_shape_sums_gbp_and_keeps_other_currencies_visible():
    bal = {"available": [{"amount": 12345, "currency": "gbp"}, {"amount": 100, "currency": "eur"}],
           "pending": [{"amount": 500, "currency": "gbp"}]}
    assert payments_panel.balance_shape(bal) == {"available": 123.45, "pending": 5.0, "other": 1.0}
    assert payments_panel.balance_shape(None) == {"available": 0.0, "pending": 0.0, "other": 0.0}


def test_checkout_payment_method_types_follows_the_threshold():
    f = payments_panel.checkout_payment_method_types
    assert f(None, 500) == ["card"]                 # blank = cards only, whatever the size
    assert f("", 500) == ["card"]
    assert f(100, 250) is None                      # at/above the threshold Stripe decides
    assert f(100, 50) == ["card"]
    assert f(0, 1) is None                          # zero = always offer bank methods
    assert f("garbage", 1000) == ["card"]


def test_refund_key_and_the_ledger_read_back():
    assert payments_panel.refund_key("ch_1", 5000) == "[ch_1@5000]"
    notes = "Refunded £50.00 on 18th Sep 26 (refunded by you) [ch_1@5000]\nRefunded £30.00 [ch_1@8000]"
    assert payments._last_refunded_pence(notes, "ch_1") == 8000
    assert payments._last_refunded_pence(notes, "ch_2") == 0
    assert payments._last_refunded_pence("", "ch_1") == 0


def test_settings_for_reads_the_threshold_from_extras():
    assert invoice_config.settings_for({"extras": {"bank_methods_min_gbp": 250}})["bank_methods_min_gbp"] == 250.0
    assert invoice_config.settings_for({"extras": {}})["bank_methods_min_gbp"] is None
    assert invoice_config.settings_for(None)["bank_methods_min_gbp"] is None
    assert invoice_config.settings_for({"extras": {"bank_methods_min_gbp": "no"}})["bank_methods_min_gbp"] is None


# ── load(): four reads, cached, degrading ────────────────────────────────────
def _fake_reads(fail: set[str] = frozenset()):
    calls: list[str] = []

    def read(acct, what):
        calls.append(what)
        if what in fail:
            raise RuntimeError("stripe down")
        if what == "balance":
            return {"available": [{"amount": 12345, "currency": "gbp"}],
                    "pending": [{"amount": 500, "currency": "gbp"}]}
        if what == "payouts":
            return {"data": [{"id": "po_1", "amount": 10000, "status": "pending",
                              "created": _ts(NOW), "arrival_date": _ts(NOW + timedelta(days=2)),
                              "destination": {"last4": "1234"}}]}
        return {"data": [_charge()]}
    return read, calls


def test_load_runs_every_read_once_then_serves_the_cache(monkeypatch):
    payments_panel._cache.clear()
    read, calls = _fake_reads()
    monkeypatch.setattr(payments_panel, "_read", read)
    d = payments_panel.load(ACCT, force=True, now=NOW)
    assert sorted(calls) == ["balance", "payouts", "recent", "window"]
    assert d["balance"]["available"] == 123.45 and d["next_payout"]["amount"] == 100.0
    assert d["charges"][0]["invoice_id"] == INV_ID and d["today"]["today"] == 180.0
    assert d["errors"] == []
    calls.clear()
    payments_panel.load(ACCT)
    assert calls == []                               # a minute's cache
    payments_panel.invalidate(ACCT)
    payments_panel.load(ACCT)
    assert calls                                     # a refund busts it


def test_load_degrades_per_read_rather_than_failing_the_page(monkeypatch):
    payments_panel._cache.clear()
    read, _ = _fake_reads(fail={"balance"})
    monkeypatch.setattr(payments_panel, "_read", read)
    d = payments_panel.load(ACCT, force=True, now=NOW)
    assert d["errors"] == ["balance"]
    assert d["balance"] == {"available": 0.0, "pending": 0.0, "other": 0.0}
    assert d["charges"] and d["payouts"]


# ── the owner's routes ───────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def _page_env(monkeypatch):
    """Enough of the app chrome for _ctx() to render a page."""
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_members", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"id": TENANT, "name": "Velox"})
    monkeypatch.setattr(dashboard.db, "is_platform_admin", lambda *a, **k: False)


def _connect_on(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_connect_client_id", "ca_x")


def test_panel_shows_the_connect_card_when_not_connected(client, monkeypatch):
    _page_env(monkeypatch)
    _connect_on(monkeypatch)
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: None)
    r = client.get("/app/payments")
    assert r.status_code == 200
    assert "Connect your Stripe account" in r.text and "BLAM8 takes no cut" in r.text


def test_panel_renders_the_figures_beside_their_invoices(client, monkeypatch):
    _page_env(monkeypatch)
    _connect_on(monkeypatch)
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: ACCT)
    payments_panel._cache.clear()
    read, _ = _fake_reads()
    monkeypatch.setattr(payments_panel, "_read", read)
    r = client.get("/app/payments")
    assert r.status_code == 200
    assert "£123.45" in r.text and "£5.00" in r.text            # available / on its way
    assert "£100.00" in r.text and "····1234" in r.text          # next payout + bank
    assert f'href="/app/invoices/{INV_ID}"' in r.text and "INV-1001" in r.text
    assert "£2.72" in r.text and "£177.28" in r.text             # fee and net per payment
    assert "BLAM8's cut" in r.text and "£0.00" in r.text
    assert 'action="/app/payments/refund"' in r.text and 'value="pi_1"' in r.text


def test_refund_route_refunds_on_the_connected_account_then_runs_the_lifecycle(client, monkeypatch):
    _connect_on(monkeypatch)
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: ACCT)
    paid = _inv(status="paid", amount_paid=180.0, stripe_payment_intent_id="pi_1",
                payment_method="card", paid_at="2026-09-10T09:00:00+00:00")
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: paid if i == INV_ID else None)
    stripe_calls = {}

    def fake_refund(**k):
        stripe_calls["refund"] = k
        return {"id": "re_1", "charge": "ch_1", "amount": 5000}

    def fake_charge(cid, **k):
        stripe_calls["charge"] = (cid, k)
        return {"id": "ch_1", "amount_refunded": 5000}
    monkeypatch.setattr(payments_panel.stripe.Refund, "create", fake_refund)
    monkeypatch.setattr(payments_panel.stripe.Charge, "retrieve", fake_charge)
    recorded = {}
    monkeypatch.setattr(db, "record_invoice_refund",
                        lambda i, t, **k: recorded.update(k) or {**paid, "amount_paid": 130.0})
    pushes, events, acts = [], [], []
    monkeypatch.setattr(push, "notify", lambda t, **k: pushes.append(k))
    monkeypatch.setattr(db, "log_agent_event", lambda t, k, s, **kw: events.append((k, s)))
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda t, **k: acts.append(k) or True)
    monkeypatch.setattr(payments.trace, "emit", lambda *a, **k: None)

    r = client.post("/app/payments/refund", data={
        "invoice_id": INV_ID, "amount": "50", "reason": "part not needed",
        "back": f"/app/invoices/{INV_ID}"})
    assert r.status_code == 303 and r.headers["location"] == f"/app/invoices/{INV_ID}?msg=refunded"
    assert stripe_calls["refund"]["stripe_account"] == ACCT
    assert stripe_calls["refund"]["payment_intent"] == "pi_1"
    assert stripe_calls["refund"]["amount"] == 5000
    assert stripe_calls["refund"]["metadata"]["note"] == "part not needed"
    assert stripe_calls["charge"] == ("ch_1", {"stripe_account": ACCT})
    assert recorded["key"] == "[ch_1@5000]" and recorded["amount_refunded_total"] == 50.0
    assert "part not needed" in recorded["note"] and "refunded by you" in recorded["note"]
    assert pushes[0]["kind"] == "invoice_refunded" and "£50.00" in pushes[0]["title"]
    assert events[0][0] == "invoice_refunded"
    assert acts[0]["activity_type"] == "invoice_refunded" and acts[0]["source_id"] == "ch_1:5000"


def test_refund_route_refuses_bad_amounts_and_invoices_without_a_card_payment(client, monkeypatch):
    _connect_on(monkeypatch)
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: ACCT)
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: _inv(
        status="paid", stripe_payment_intent_id="pi_1"))
    r = client.post("/app/payments/refund", data={"invoice_id": INV_ID, "amount": "abc"})
    assert r.headers["location"].endswith("msg=refund_badamount")
    r = client.post("/app/payments/refund", data={"invoice_id": INV_ID, "amount": "-5"})
    assert r.headers["location"].endswith("msg=refund_badamount")
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: _inv(status="paid",
                                                                           payment_method="cash"))
    r = client.post("/app/payments/refund", data={"invoice_id": INV_ID})
    assert r.headers["location"].endswith("msg=refund_nopayment")


def test_refund_followup_does_nothing_twice(monkeypatch):
    monkeypatch.setattr(db, "record_invoice_refund", lambda *a, **k: None)   # key already there
    pushes = []
    monkeypatch.setattr(push, "notify", lambda t, **k: pushes.append(k))
    ok = payments.invoice_refund_followup(TENANT, _inv(status="paid"), charge_id="ch_1",
                                          amount_refunded_pence=5000, refunded_now_pence=5000,
                                          reason="", via="refunded in Stripe")
    assert ok is False and pushes == []


class _Chain:
    """A fake supabase table chain that records the update and answers with it."""
    def __init__(self, sink):
        self.sink = sink
    def table(self, name):
        return self
    def update(self, fields):
        self.sink.append(fields)
        return self
    def eq(self, *a):
        return self
    def execute(self):
        return SimpleNamespace(data=[{"status": self.sink[-1]["status"], **self.sink[-1]}])


def test_record_invoice_refund_uses_absolute_amounts_and_the_key(monkeypatch):
    updates: list[dict] = []
    fake = _Chain(updates)
    paid = _inv(status="paid", amount_paid=180.0, notes="Imported from Xero")
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: paid)
    # partial: stays paid, amount_paid reduced, the line + key appended
    row = db.record_invoice_refund(INV_ID, TENANT, amount_refunded_total=50.0,
                                   key="[ch_1@5000]", note="Refunded £50.00", client=fake)
    assert row["status"] == "paid" and updates[-1]["amount_paid"] == 130.0
    assert updates[-1]["notes"] == "Imported from Xero\nRefunded £50.00 [ch_1@5000]"
    # full: status flips, amount_paid 0
    row = db.record_invoice_refund(INV_ID, TENANT, amount_refunded_total=180.0,
                                   key="[ch_1@18000]", note="Refunded £130.00", client=fake)
    assert row["status"] == "refunded" and updates[-1]["amount_paid"] == 0
    # replay: key already in notes → None, no update
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: {
        **paid, "notes": "x [ch_1@5000]"})
    n = len(updates)
    assert db.record_invoice_refund(INV_ID, TENANT, amount_refunded_total=50.0,
                                    key="[ch_1@5000]", note="again", client=fake) is None
    assert len(updates) == n
    # not a settled invoice → None
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: _inv(status="sent"))
    assert db.record_invoice_refund(INV_ID, TENANT, amount_refunded_total=50.0,
                                    key="[ch_1@5000]", note="x", client=fake) is None


def test_methods_route_keeps_the_threshold_in_extras(client, monkeypatch):
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {
        "extras": {"section_order": ["core"], "bank_methods_min_gbp": 100}})
    saved = []
    monkeypatch.setattr(db, "save_invoice_settings_checked", lambda t, expected, f, mode, client=None: saved.append(f))
    r = client.post("/app/payments/methods", data={"invoice_snapshot": __import__("json").dumps({"invoice": db.get_invoice_settings("t1"), "quote_auto_draft":"propose"}), "bank_methods_min_gbp": "£250"})
    assert r.headers["location"].endswith("msg=methods_saved")
    assert saved[-1] == {"extras": {"section_order": ["core"], "bank_methods_min_gbp": 250.0}}
    client.post("/app/payments/methods", data={"invoice_snapshot": __import__("json").dumps({"invoice": db.get_invoice_settings("t1"), "quote_auto_draft":"propose"}), "bank_methods_min_gbp": ""})
    assert saved[-1] == {"extras": {"section_order": ["core"]}}
    r = client.post("/app/payments/methods", data={"invoice_snapshot": __import__("json").dumps({"invoice": db.get_invoice_settings("t1"), "quote_auto_draft":"propose"}), "bank_methods_min_gbp": "lots"})
    assert r.headers["location"].endswith("msg=methods_badamount")


# ── the Connect webhook ───────────────────────────────────────────────────────
@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def hook_web(web, monkeypatch):
    monkeypatch.setattr(settings, "stripe_connect_webhook_secret", "whsec_test")
    monkeypatch.setattr(db, "stripe_record_event", lambda *a: True)
    monkeypatch.setattr(payments.trace, "emit", lambda *a, **k: None)
    return web


def _hook(event):
    return patch("app.payments.stripe.Webhook.construct_event", return_value=event)


def _post(hook_web, evt):
    with _hook(evt):
        return hook_web.post("/webhooks/stripe", content=b"{}",
                             headers={"stripe-signature": "t=0,v1=x"})


def test_webhook_batch_settles_every_invoice_on_the_one_payment(hook_web, monkeypatch):
    evt = {"id": "evt_b1", "type": "payment_intent.succeeded", "account": ACCT,
           "data": {"object": {"id": "pi_b", "payment_method_types": ["card"],
                    "metadata": {"kind": "invoice_batch", "tenant_id": TENANT,
                                 "invoice_ids": f"{INV_ID},{INV2_ID}",
                                 "invoice_refs": "INV-1001, INV-1002"}}}}
    paid, followed = [], []
    monkeypatch.setattr(db, "mark_invoice_paid",
                        lambda i, t, **k: paid.append((i, k)) or _inv(id=i, status="paid"))
    monkeypatch.setattr(payments, "invoice_paid_followup",
                        lambda t, inv, via: followed.append((inv["id"], via)))
    assert _post(hook_web, evt).status_code == 200
    assert [p[0] for p in paid] == [INV_ID, INV2_ID]
    assert all(p[1]["stripe_payment_intent_id"] == "pi_b" for p in paid)
    assert followed[0][1] == "paid online via Stripe — 2 invoices in one payment"


def test_webhook_processing_marks_a_bank_payment_in_progress(hook_web, monkeypatch):
    evt = {"id": "evt_p1", "type": "payment_intent.processing", "account": ACCT,
           "data": {"object": {"id": "pi_bacs", "payment_method_types": ["bacs_debit"],
                    "metadata": {"kind": "invoice", "tenant_id": TENANT,
                                 "invoice_id": INV_ID, "invoice_ref": "INV-1001"}}}}
    pending, pushes, acts = [], [], []
    monkeypatch.setattr(db, "mark_invoice_payment_pending",
                        lambda i, t, **k: pending.append(k) or _inv(payment_method=k["payment_method"]))
    monkeypatch.setattr(push, "notify", lambda t, **k: pushes.append(k))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda t, **k: acts.append(k) or True)
    assert _post(hook_web, evt).status_code == 200
    assert pending == [{"payment_method": "bacs_debit", "stripe_payment_intent_id": "pi_bacs"}]
    assert pushes[0]["kind"] == "invoice_payment_pending" and "Bacs Direct Debit" in pushes[0]["title"]
    assert acts[0]["activity_type"] == "invoice_payment_pending" and acts[0]["source_id"] == "pi_bacs:processing"


def test_webhook_charge_refunded_lands_a_dashboard_refund_by_the_delta(hook_web, monkeypatch):
    # First refund from Stripe's dashboard: nothing on the invoice yet → £50 now.
    evt = {"id": "evt_r1", "type": "charge.refunded", "account": ACCT,
           "data": {"object": {"id": "ch_1", "amount": 18000, "amount_refunded": 5000,
                    "metadata": {"kind": "invoice", "tenant_id": TENANT,
                                 "invoice_id": INV_ID, "invoice_ref": "INV-1001"}}}}
    paid = _inv(status="paid", amount_paid=180.0, stripe_payment_intent_id="pi_1", notes="")
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: paid)
    recorded = []
    monkeypatch.setattr(db, "record_invoice_refund",
                        lambda i, t, **k: recorded.append(k) or {**paid, "amount_paid": 130.0})
    monkeypatch.setattr(push, "notify", lambda t, **k: None)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda t, **k: True)
    assert _post(hook_web, evt).status_code == 200
    assert recorded[0]["key"] == "[ch_1@5000]" and recorded[0]["amount_refunded_total"] == 50.0
    assert "Refunded £50.00" in recorded[0]["note"] and "refunded in Stripe" in recorded[0]["note"]
    # A second, later refund: the notes carry the first key → only the £30 delta is "now".
    paid["notes"] = "Refunded £50.00 (refunded in Stripe) [ch_1@5000]"
    evt2 = {**evt, "id": "evt_r2",
            "data": {"object": {**evt["data"]["object"], "amount_refunded": 8000}}}
    assert _post(hook_web, evt2).status_code == 200
    assert recorded[1]["key"] == "[ch_1@8000]" and "Refunded £30.00" in recorded[1]["note"]


def test_webhook_dispute_reads_the_charge_and_announces(hook_web, monkeypatch):
    evt = {"id": "evt_d1", "type": "charge.dispute.created", "account": ACCT,
           "data": {"object": {"id": "dp_1", "charge": "ch_1", "amount": 18000,
                               "reason": "product_not_received"}}}
    monkeypatch.setattr(payments.stripe.Charge, "retrieve", lambda cid, **k: {
        "id": cid, "metadata": {"kind": "invoice", "tenant_id": TENANT,
                                "invoice_id": INV_ID, "invoice_ref": "INV-1001"}})
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: _inv(status="paid"))
    pushes, acts = [], []
    monkeypatch.setattr(push, "notify", lambda t, **k: pushes.append(k))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda t, **k: acts.append(k) or True)
    assert _post(hook_web, evt).status_code == 200
    assert pushes[0]["kind"] == "invoice_disputed" and "INV-1001" in pushes[0]["title"]
    assert "product not received" in pushes[0]["body"]
    assert acts[0]["source_id"] == "dp_1" and "dashboard.stripe.com/disputes/dp_1" in acts[0]["summary"]


# ── the customer's pay page + checkout-all ───────────────────────────────────
def _pay_env(monkeypatch, inv: dict, others: list[dict], extras: dict | None = None):
    _connect_on(monkeypatch)
    monkeypatch.setattr(db, "get_stripe_account_id", lambda *a, **k: ACCT)
    monkeypatch.setattr(db, "get_invoice_any_tenant", lambda i: inv)
    monkeypatch.setattr(db, "set_invoice_viewed", lambda i, t: False)
    monkeypatch.setattr(db, "get_tenant_config", lambda t: {})
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t, client=None: [])
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {
        "business_name": "Velox Vans", "extras": extras or {}})
    monkeypatch.setattr(db, "open_invoices_for_customer", lambda t, e, **k: list(others))
    monkeypatch.setattr(payments.trace, "emit", lambda *a, **k: None)


def test_pay_page_offers_to_settle_the_customers_other_invoices_too(web, monkeypatch):
    other = _inv(id=INV2_ID, invoice_ref="INV-1002", total=40.0, subtotal=40.0)
    _pay_env(monkeypatch, _inv(), [other])
    r = web.get(f"/pay/{INV_ID}")
    assert r.status_code == 200
    assert "Pay £180.00 now" in r.text
    assert "You also have 1 other unpaid invoice with Velox Vans" in r.text
    assert "INV-1002" in r.text and "Pay all 2 together — £220.00" in r.text
    assert f"action='/pay/{INV_ID}/checkout-all'" in r.text


def test_pay_page_shows_a_bank_payment_in_progress_instead_of_pay_now(web, monkeypatch):
    _pay_env(monkeypatch, _inv(payment_method="bacs_debit", stripe_payment_intent_id="pi_bacs"), [])
    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {
        "business_name": "Velox Vans", "bank_sort_code": "04-00-04",
        "bank_account_number": "12345678", "extras": {}})
    r = web.get(f"/pay/{INV_ID}")
    assert "being processed" in r.text
    assert "Pay £180.00 now" not in r.text and "Sort code" not in r.text


def _session_capture(monkeypatch):
    calls = []
    monkeypatch.setattr(payments.stripe.checkout.Session, "create",
                        lambda **k: calls.append(k) or SimpleNamespace(url="https://checkout.stripe.com/x"))
    return calls


def test_checkout_all_mints_one_session_with_a_line_per_invoice(web, monkeypatch):
    other = _inv(id=INV2_ID, invoice_ref="INV-1002", total=40.0, amount_paid=10.0)
    _pay_env(monkeypatch, _inv(), [other])
    calls = _session_capture(monkeypatch)
    r = web.post(f"/pay/{INV_ID}/checkout-all")
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/")
    k = calls[0]
    assert k["stripe_account"] == ACCT and k["mode"] == "payment"
    assert [li["price_data"]["unit_amount"] for li in k["line_items"]] == [18000, 3000]
    assert k["line_items"][1]["price_data"]["product_data"]["name"] == "INV-1002 — Velox Vans"
    assert k["metadata"] == {"kind": "invoice_batch", "tenant_id": TENANT,
                             "invoice_ids": f"{INV_ID},{INV2_ID}",
                             "invoice_refs": "INV-1001, INV-1002"}
    assert k["payment_intent_data"] == {"metadata": k["metadata"]}
    assert k["payment_method_types"] == ["card"]          # no threshold set → cards only


def test_checkout_all_with_nothing_else_open_falls_back_to_the_single_checkout(web, monkeypatch):
    _pay_env(monkeypatch, _inv(), [])
    r = web.post(f"/pay/{INV_ID}/checkout-all")
    assert r.status_code == 307 and r.headers["location"] == f"/pay/{INV_ID}/checkout"


def test_single_checkout_offers_bank_methods_only_from_the_threshold(web, monkeypatch):
    _pay_env(monkeypatch, _inv(), [], extras={"bank_methods_min_gbp": 500})
    calls = _session_capture(monkeypatch)
    web.post(f"/pay/{INV_ID}/checkout")
    assert calls[0]["payment_method_types"] == ["card"]           # £180 is under £500
    _pay_env(monkeypatch, _inv(), [], extras={"bank_methods_min_gbp": 100})
    calls = _session_capture(monkeypatch)
    web.post(f"/pay/{INV_ID}/checkout")
    assert "payment_method_types" not in calls[0]                 # Stripe offers what's switched on
    assert calls[0]["metadata"]["kind"] == "invoice"
