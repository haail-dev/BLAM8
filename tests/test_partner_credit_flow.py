"""Partner settlement plumbing — mode choice, withdrawal requests, operator settlement,
and the Stripe subscription credit.

The maths lives in tests/test_partner_settlement.py; this is the wiring around it. The
things most worth pinning are the ones that involve money moving: a request amount is
never trusted, the settle set is chosen at PAYMENT time (not request time) so a clawback
in between is picked up, nothing can settle twice, and the credit path refuses outright
while the feature flag is off.

Run: python -m pytest tests/test_partner_credit_flow.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.admin as admin_mod
from app import auth, db, partner_credit, partner_settlement, partners
from app.admin_auth import AdminCtx, require_admin
from app.config import settings
from app.main import app
from app.security import require_csrf

PARTNER = {"id": "p-1", "name": "Sterling Accounts", "email": "jo@sterling.co.uk",
           "ref_code": "STERLING41", "commission_rate": 0.20, "status": "active",
           "settlement_mode": "cash", "credit_tenant_id": None}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(client):
    app.dependency_overrides[require_admin] = \
        lambda: AdminCtx(user_id="a1", email="ops@blam8.com", role="owner")
    yield client
    app.dependency_overrides.clear()


def _sign_in(client, monkeypatch, email="jo@sterling.co.uk"):
    async def fake_get_user(access):
        return {"id": "u-p", "email": email} if access == "AT" else None
    monkeypatch.setattr(auth, "get_user", fake_get_user)
    client.cookies.set(auth.AT_COOKIE, "AT")


def _partner(monkeypatch, **over):
    p = {**PARTNER, **over}
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: p)
    return p


def _summary(monkeypatch, available=0.0, credit=0.0):
    """Stub referral_summary — the settlement panel's numbers are already tested."""
    monkeypatch.setattr(partners, "referral_summary", lambda p: {
        "settlement": {"available": available, "credit_applied": credit,
                       "mode": "credit" if credit else "cash"}})


def _c(cid, amount, earned="2026-06-01T00:00:00+00:00"):
    return {"id": cid, "amount": amount, "earned_at": earned, "status": "scheduled"}


# ── choosing how you get paid ─────────────────────────────────────────────────
def test_switching_to_credit_stores_the_tenant_to_credit(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    saved = {}
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t-9")
    monkeypatch.setattr(db, "set_partner_settlement",
                        lambda pid, mode, tid=None: saved.update(pid=pid, mode=mode, tid=tid))
    r = client.post("/partner/settlement", data={"mode": "credit"})
    assert r.status_code == 303
    assert saved == {"pid": "p-1", "mode": "credit", "tid": "t-9"}


def test_credit_is_refused_for_a_partner_with_no_blam8_account(client, monkeypatch):
    # A pure referrer has no bill to credit; say so rather than switching them into a
    # mode that can never pay them.
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)
    called = []
    monkeypatch.setattr(db, "set_partner_settlement", lambda *a, **k: called.append(a))
    r = client.post("/partner/settlement", data={"mode": "credit"})
    assert "err=notenant" in r.headers["location"] and called == []


def test_switching_back_to_cash_needs_no_tenant(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch, settlement_mode="credit", credit_tenant_id="t-9")
    saved = {}
    monkeypatch.setattr(db, "set_partner_settlement",
                        lambda pid, mode, tid=None: saved.update(mode=mode))
    client.post("/partner/settlement", data={"mode": "cash"})
    assert saved["mode"] == "cash"


def test_a_disabled_partner_cannot_change_settlement(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch, status="disabled")
    called = []
    monkeypatch.setattr(db, "set_partner_settlement", lambda *a, **k: called.append(a))
    r = client.post("/partner/settlement", data={"mode": "credit"})
    assert r.headers["location"] == "/partner" and called == []


# ── requesting a withdrawal ───────────────────────────────────────────────────
def test_a_withdrawal_request_records_the_clamped_amount(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    saved = {}
    monkeypatch.setattr(db, "create_partner_withdrawal",
                        lambda pid, amt: saved.update(pid=pid, amt=amt) or {"id": "w1"})
    r = client.post("/partner/withdraw", data={"amount": "75"})
    assert "msg=wrequested" in r.headers["location"] and saved["amt"] == 75.0


def test_a_request_over_the_available_balance_is_clamped_not_refused(client, monkeypatch):
    # The UI is a slider, so an out-of-range value means a hand-built post.
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    saved = {}
    monkeypatch.setattr(db, "create_partner_withdrawal",
                        lambda pid, amt: saved.update(amt=amt) or {"id": "w1"})
    client.post("/partner/withdraw", data={"amount": "999999"})
    assert saved["amt"] == 120.0


def test_a_request_below_the_minimum_is_refused(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    called = []
    monkeypatch.setattr(db, "create_partner_withdrawal", lambda *a: called.append(a))
    r = client.post("/partner/withdraw", data={"amount": "5"})
    assert "err=wamount" in r.headers["location"] and called == []


def test_junk_amounts_are_refused(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    called = []
    monkeypatch.setattr(db, "create_partner_withdrawal", lambda *a: called.append(a))
    for junk in ("", "lots", "-90"):
        r = client.post("/partner/withdraw", data={"amount": junk})
        assert "err=wamount" in r.headers["location"]
    assert called == []


def test_only_one_request_can_be_open_at_a_time(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: {"id": "w-open"})
    called = []
    monkeypatch.setattr(db, "create_partner_withdrawal", lambda *a: called.append(a))
    r = client.post("/partner/withdraw", data={"amount": "75"})
    assert "err=wopen" in r.headers["location"] and called == []


def test_a_race_caught_by_the_unique_index_is_reported_not_crashed(client, monkeypatch):
    # create_partner_withdrawal returns None on the unique violation.
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    _summary(monkeypatch, available=120.0)
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    monkeypatch.setattr(db, "create_partner_withdrawal", lambda pid, amt: None)
    r = client.post("/partner/withdraw", data={"amount": "75"})
    assert "err=wopen" in r.headers["location"]


def test_a_partner_can_only_cancel_their_own_request(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    monkeypatch.setattr(db, "get_partner_withdrawal",
                        lambda wid: {"id": wid, "partner_id": "SOMEONE-ELSE"})
    called = []
    monkeypatch.setattr(db, "cancel_partner_withdrawal", lambda *a, **k: called.append(a))
    r = client.post("/partner/withdraw/w-1/cancel")
    assert "err=wmissing" in r.headers["location"] and called == []


def test_cancelling_your_own_request_works(client, monkeypatch):
    _sign_in(client, monkeypatch)
    _partner(monkeypatch)
    monkeypatch.setattr(db, "get_partner_withdrawal",
                        lambda wid: {"id": wid, "partner_id": "p-1"})
    called = []
    monkeypatch.setattr(db, "cancel_partner_withdrawal",
                        lambda wid, note=None: called.append(wid) or {"id": wid})
    r = client.post("/partner/withdraw/w-1/cancel")
    assert "msg=wcancelled" in r.headers["location"] and called == ["w-1"]


# ── the operator settling one ─────────────────────────────────────────────────
def test_paying_a_withdrawal_settles_a_payout_and_marks_the_commissions(admin_client, monkeypatch):
    monkeypatch.setattr(db, "get_partner_withdrawal", lambda wid: {
        "id": "w-1", "partner_id": "p-1", "status": "requested", "requested_gbp": 45.0})
    monkeypatch.setattr(db, "list_partner_commissions",
                        lambda pid: [_c("a", 19.80), _c("b", 19.80), _c("c", 19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    made = {}
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda pid, total, ps, pe, ref, method="bank":
                        made.update(total=total, method=method, ref=ref) or {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid",
                        lambda ids, poid: made.update(ids=ids, poid=poid))
    monkeypatch.setattr(db, "settle_partner_withdrawal",
                        lambda wid, poid, total: made.update(settled=total) or {"id": wid})
    r = admin_client.post("/admin/withdrawals/w-1/pay", data={"reference": "FPS-123"})
    assert "msg=wpaid" in r.headers["location"]
    # Whole rows only, so £45 asked settles at £39.60.
    assert made["total"] == 39.60 and made["settled"] == 39.60
    assert made["ids"] == ["a", "b"] and made["method"] == "bank"
    assert made["ref"] == "FPS-123"


def test_the_settle_set_is_chosen_at_payment_time_so_a_new_clawback_lands(admin_client, monkeypatch):
    # A refund arrived after the £45 request: it must reduce what gets paid, not be
    # skipped because the request was made before it existed.
    monkeypatch.setattr(db, "get_partner_withdrawal", lambda wid: {
        "id": "w-1", "partner_id": "p-1", "status": "requested", "requested_gbp": 45.0})
    monkeypatch.setattr(db, "list_partner_commissions",
                        lambda pid: [_c("a", 19.80), _c("b", 19.80), _c("claw", -19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    made = {}
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda pid, total, ps, pe, ref, method="bank":
                        made.update(total=total) or {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid",
                        lambda ids, poid: made.update(ids=ids))
    monkeypatch.setattr(db, "settle_partner_withdrawal", lambda w, p, t: {"id": w})
    admin_client.post("/admin/withdrawals/w-1/pay", data={"reference": ""})
    assert made["total"] == 19.80 and "claw" in made["ids"]


def test_paying_an_already_settled_request_is_refused(admin_client, monkeypatch):
    monkeypatch.setattr(db, "get_partner_withdrawal", lambda wid: {
        "id": "w-1", "partner_id": "p-1", "status": "paid", "requested_gbp": 45.0})
    r = admin_client.post("/admin/withdrawals/w-1/pay", data={"reference": ""})
    assert "err=wmissing" in r.headers["location"]


def test_nothing_settleable_leaves_the_request_open(admin_client, monkeypatch):
    monkeypatch.setattr(db, "get_partner_withdrawal", lambda wid: {
        "id": "w-1", "partner_id": "p-1", "status": "requested", "requested_gbp": 45.0})
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [_c("claw", -80.0)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    called = []
    monkeypatch.setattr(db, "create_partner_payout", lambda *a, **k: called.append(a))
    r = admin_client.post("/admin/withdrawals/w-1/pay", data={"reference": ""})
    assert "err=wnothing" in r.headers["location"] and called == []


def test_a_double_click_cannot_settle_the_same_request_twice(admin_client, monkeypatch):
    monkeypatch.setattr(db, "get_partner_withdrawal", lambda wid: {
        "id": "w-1", "partner_id": "p-1", "status": "requested", "requested_gbp": 45.0})
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [_c("a", 19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda *a, **k: {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid", lambda ids, poid: None)
    # The status-guarded update matched nothing — someone else got there first.
    monkeypatch.setattr(db, "settle_partner_withdrawal", lambda w, p, t: None)
    r = admin_client.post("/admin/withdrawals/w-1/pay", data={"reference": ""})
    assert "err=wraced" in r.headers["location"]


def test_the_queue_shows_on_the_admin_partners_page(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_partners", lambda: [PARTNER])
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [])
    monkeypatch.setattr(db, "list_all_partner_leads", lambda: [])
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_payouts", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_withdrawals", lambda pid, **k: [])
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)
    monkeypatch.setattr(db, "list_open_withdrawals", lambda: [
        {"id": "w-1", "partner_id": "p-1", "requested_gbp": 120.0,
         "requested_at": "2026-07-29T09:00:00+00:00"}])
    r = admin_client.get("/admin/partners")
    assert "Withdrawal requests" in r.text and "£120.00" in r.text
    assert "/admin/withdrawals/w-1/pay" in r.text


# ── the Stripe subscription credit ────────────────────────────────────────────
def test_credit_refuses_while_the_flag_is_off(monkeypatch):
    # Nothing may be credited until the VAT answer and agreement v2.3 land.
    monkeypatch.setattr(settings, "partner_credit_enabled", False)
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": "t-9"})
    assert out["ok"] is False and out["reason"] == partner_credit.SKIP_DISABLED


@pytest.fixture
def credit_on(monkeypatch):
    monkeypatch.setattr(settings, "partner_credit_enabled", True)


def test_a_cash_partner_is_never_credited(monkeypatch, credit_on):
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "cash"})
    assert out["reason"] == partner_credit.SKIP_NOT_CREDIT_MODE


def test_credit_needs_a_tenant(monkeypatch, credit_on):
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": None})
    assert out["reason"] == partner_credit.SKIP_NO_TENANT


def test_credit_needs_an_existing_stripe_customer(monkeypatch, credit_on):
    # Deliberately not creating one: a credit on a customer with no subscription would
    # sit unusable and look like the money vanished.
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: None)
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": "t-9"})
    assert out["reason"] == partner_credit.SKIP_NO_CUSTOMER


def test_nothing_cleared_means_no_stripe_call(monkeypatch, credit_on):
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: "cus_1")
    _summary(monkeypatch, credit=0.0)
    calls = []
    monkeypatch.setattr(partner_credit.stripe.Customer, "create_balance_transaction",
                        lambda *a, **k: calls.append(k))
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": "t-9"})
    assert out["reason"] == partner_credit.SKIP_NOTHING_TO_CREDIT and calls == []


def test_a_credit_is_negative_pence_and_settles_the_ledger(monkeypatch, credit_on):
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: "cus_1")
    _summary(monkeypatch, credit=39.0)
    monkeypatch.setattr(db, "list_partner_commissions",
                        lambda pid: [_c("a", 19.80), _c("b", 19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    seen = {}
    monkeypatch.setattr(partner_credit.stripe.Customer, "create_balance_transaction",
                        lambda cid, **k: seen.update(cid=cid, **k))
    made = {}
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda pid, total, ps, pe, reference=None, method="bank":
                        made.update(total=total, method=method) or {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid",
                        lambda ids, poid: made.update(ids=ids))
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": "t-9"})
    assert out["ok"] is True and out["amount"] == 39.60
    # Negative = a credit on Stripe's customer balance.
    assert seen["cid"] == "cus_1" and seen["amount"] == -3960 and seen["currency"] == "gbp"
    assert made["method"] == "credit" and made["ids"] == ["a", "b"]


def test_the_credit_carries_an_idempotency_key_tied_to_the_settled_rows(monkeypatch, credit_on):
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: "cus_1")
    _summary(monkeypatch, credit=39.0)
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [_c("a", 19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    seen = {}
    monkeypatch.setattr(partner_credit.stripe.Customer, "create_balance_transaction",
                        lambda cid, **k: seen.update(k))
    monkeypatch.setattr(db, "create_partner_payout", lambda *a, **k: {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid", lambda ids, poid: None)
    partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                         "credit_tenant_id": "t-9"})
    assert "a" in seen["idempotency_key"] and "p-1" in seen["idempotency_key"]


def test_a_stripe_failure_leaves_the_ledger_untouched(monkeypatch, credit_on):
    # Credit first, settle second: if Stripe fails we have changed nothing, which is the
    # better failure to leave behind than money given away with no record.
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: "cus_1")
    _summary(monkeypatch, credit=39.0)
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [_c("a", 19.80)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)

    def _boom(*a, **k):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(partner_credit.stripe.Customer, "create_balance_transaction", _boom)
    called = []
    monkeypatch.setattr(db, "create_partner_payout", lambda *a, **k: called.append(a))
    monkeypatch.setattr(db, "mark_commission_ids_paid", lambda *a: called.append(a))
    with pytest.raises(RuntimeError):
        partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                             "credit_tenant_id": "t-9"})
    assert called == []


def test_credit_is_bounded_by_the_bill_and_never_dumps_the_whole_balance(monkeypatch, credit_on):
    # Stripe cannot pay a customer out, so crediting the entire balance would strand
    # most of it. The credit is the bill plus at most ONE commission row that crosses
    # it (so the bill genuinely reaches £0) — here £39.60 of a £198 balance.
    monkeypatch.setattr(db, "stripe_get_customer_id", lambda tid: "cus_1")
    _summary(monkeypatch, credit=39.0)
    monkeypatch.setattr(db, "list_partner_commissions",
                        lambda pid: [_c(str(i), 19.80) for i in range(10)])
    monkeypatch.setattr(partners, "payable_commissions", lambda cs: cs)
    seen = {}
    monkeypatch.setattr(partner_credit.stripe.Customer, "create_balance_transaction",
                        lambda cid, **k: seen.update(k))
    monkeypatch.setattr(db, "create_partner_payout", lambda *a, **k: {"id": "po-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid", lambda ids, poid: None)
    out = partner_credit.apply_monthly_credit({**PARTNER, "settlement_mode": "credit",
                                               "credit_tenant_id": "t-9"})
    assert out["amount"] == 39.60          # the bill (£39) + the row that crosses it
    assert seen["amount"] == -3960         # not -19800, the whole cleared balance


# ── the agreement matches what the product does ───────────────────────────────
def test_agreement_is_v2_3(client):
    from app import partners as prt
    assert prt.TERMS_VERSION == "2.3"
    assert "Version 2.3" in client.get("/partner/terms").text


def test_agreement_covers_the_settlement_election(client):
    body = client.get("/partner/terms").text
    assert "4.3" in body and "Settlement method" in body
    assert "bank transfer" in body and "subscription" in body


def test_agreement_explains_that_credit_cannot_go_below_zero(client):
    # The Stripe limit, stated as a contractual term rather than left as a surprise.
    body = client.get("/partner/terms").text
    assert "Subscription credit" in body
    assert "cannot reduce an invoice below zero" in body


def test_agreement_permits_the_retained_cover_the_product_actually_holds(client):
    """The single most important clause in v2.3. The product holds back part of cleared
    commission against refunds; without 4.11 it would be doing something the contract
    never permitted. It must also say the money is NOT forfeited."""
    body = client.get("/partner/terms").text
    assert "Retained cover" in body
    assert "not forfeited" in body
    assert "three times" in body                 # the cap the code enforces
    assert "released" in body                    # and it comes back


def test_agreement_states_the_withdrawal_terms_the_code_enforces(client):
    body = client.get("/partner/terms").text
    assert "Withdrawal of accumulated commission" in body
    assert "£50" in body                          # partner_settlement.MIN_WITHDRAWAL_GBP
    assert "one request may be open" in body      # the unique partial index
    assert "may be less than the sum requested" in body   # whole-amount settlement


def test_agreement_covers_vat_and_invoicing_for_both_settlement_routes(client):
    body = client.get("/partner/terms").text
    assert "Invoicing and VAT" in body and "self-billing" in body
    assert "applied as subscription credit" in body


def test_agreement_says_crediting_your_own_bill_is_not_self_referral(client):
    # 3.7 bars commission on your OWN subscription; 4.9 applies OTHER people's
    # commission to it. Without this the two clauses look contradictory.
    body = client.get("/partner/terms").text
    assert "not a self-referral" in body


def test_the_thresholds_in_the_agreement_match_the_code(client):
    """A contract that drifts from the code is worse than no contract."""
    from app import partner_settlement as ps
    body = client.get("/partner/terms").text
    assert f"£{ps.MIN_WITHDRAWAL_GBP:.0f}" in body
    assert f"{int(ps.BUFFER_COMMISSION_SHARE * 100)}%" in body
    assert ps.BUFFER_CAP_MULTIPLE == 3.0 and "three times" in body
