"""Partner referral programme — attribution, the commission ledger, leads.

Pins the promises the programme makes:
  * attribution is capture-anywhere (?ref= cookie at /login and the landing
    page consumed at the SSO callback; typed code on the onboarding interview;
    pre-registered leads matched by signup email) and FIRST-WINS (attach only
    writes an unattributed row);
  * NEW accounts only — an existing account signing in marks matching leads
    ineligible (already a customer) instead of attributing;
  * self-referral is blocked in PRODUCT (code + lead form), not just the
    agreement;
  * a losing lead is marked disputed/ineligible, never silently dropped;
  * money is EARNED only from a collected Stripe invoice (ledger row,
    'scheduled' until the payout run marks it paid), the 90-day attribution
    window enforced at write time;
  * the portal ladder: Registered → Trial → Payment pending → Active →
    Cancelled, with the window expiry shown on unconverted referrals.

Run: python -m pytest tests/ -q
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth, auth_routes, db, onboarding, partners
from app.admin_auth import AdminCtx, require_admin
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)   # no `with`: skip startup events
    app.dependency_overrides.clear()


def _set_cookies(r) -> str:
    return " ".join(r.headers.get_list("set-cookie"))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


NOW = datetime.now(timezone.utc)
PARTNER = {"id": "p-1", "name": "Sterling Accounts", "email": "jo@sterling.co.uk",
           "ref_code": "STERLING41", "commission_rate": 0.20, "status": "active"}
RIVAL = {"id": "p-2", "name": "Rival Ltd", "email": "sam@rival.co.uk",
         "ref_code": "RIVAL77", "commission_rate": 0.20, "status": "active"}


@pytest.fixture(autouse=True)
def _no_withdrawals(monkeypatch):
    """referral_summary reads the withdrawal state on EVERY call (the settlement panel
    added 2026-07-30), so default it to none — a test then only sets up what it is
    actually asserting, and can still override these two.
    """
    monkeypatch.setattr(db, "list_partner_withdrawals", lambda pid, **k: [])
    monkeypatch.setattr(db, "open_partner_withdrawal", lambda pid: None)


def _no_ledger(monkeypatch):
    """A partner with an empty ledger — referral_summary reads all three."""
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_payouts", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])


# ── code helpers ─────────────────────────────────────────────────────────────
def test_normalize_code_uppercases_and_strips():
    assert partners.normalize_code("  sterling41 ") == "STERLING41"
    assert partners.normalize_code(None) == ""


def test_generate_code_is_name_stem_plus_digits(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    code = partners.generate_code("Sterling Accounts Ltd")
    assert code.startswith("STERLING") and code[8:].isdigit()


def test_generate_code_retries_past_collisions(monkeypatch):
    rolls = iter([13, 13, 45])   # randbelow(90) → codes 23, 23, 55
    monkeypatch.setattr(partners.secrets, "randbelow", lambda n: next(rolls))
    monkeypatch.setattr(db, "get_partner_by_code",
                        lambda c: {"id": "x"} if c == "STERLING23" else None)
    assert partners.generate_code("Sterling") == "STERLING55"


# ── attribution (attach) ─────────────────────────────────────────────────────
def test_attach_rejects_unknown_and_disabled_codes(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    assert partners.attach_for_tenant("t1", "NOPE") == "invalid"

    monkeypatch.setattr(db, "get_partner_by_code",
                        lambda c: {**PARTNER, "status": "disabled"})
    assert partners.attach_for_tenant("t1", "STERLING41") == "invalid"


def test_attach_blocks_self_referral_in_product(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: PARTNER)
    assert partners.attach_for_tenant("t1", "STERLING41",
                                      signer_email="JO@sterling.co.uk ") == "self"


def test_attach_validates_then_writes_first_wins(monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: PARTNER)
    monkeypatch.setattr(db, "attach_referral",
                        lambda tid, pid, code, at: seen.update(tid=tid, pid=pid, code=code) or True)
    monkeypatch.setattr(db, "list_leads_by_email", lambda e, status="registered": [])
    assert partners.attach_for_tenant("t1", " sterling41 ",
                                      signer_email="ops@acme.com") == "attached"
    assert seen == {"tid": "t1", "pid": "p-1", "code": "STERLING41"}


def test_attach_already_attributed_reports_taken(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: PARTNER)
    monkeypatch.setattr(db, "attach_referral", lambda *a: False)   # row already set
    assert partners.attach_for_tenant("t1", "STERLING41") == "taken"


def test_attach_settles_leads_winner_converts_rival_disputed(monkeypatch):
    updates = {}
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: PARTNER)
    monkeypatch.setattr(db, "attach_referral", lambda *a: True)
    monkeypatch.setattr(db, "list_leads_by_email", lambda e, status="registered": [
        {"id": "l-mine", "partner_id": "p-1"}, {"id": "l-rival", "partner_id": "p-2"}])
    monkeypatch.setattr(db, "update_partner_lead",
                        lambda lid, fields: updates.update({lid: fields}))
    assert partners.attach_for_tenant("t1", "STERLING41",
                                      signer_email="ops@acme.com") == "attached"
    assert updates["l-mine"]["status"] == "converted"
    assert updates["l-rival"]["status"] == "disputed"
    assert updates["l-rival"]["reason"] == "other_partner"       # marked, not dropped


# ── leads: register + reconcile at signup ────────────────────────────────────
def test_register_lead_validations(monkeypatch):
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [
        {"contact_email": "dup@acme.com", "status": "registered"}])
    monkeypatch.setattr(db, "auth_user_exists", lambda e: False)
    created = {}
    monkeypatch.setattr(db, "create_partner_lead",
                        lambda pid, c, e, exp, **kw: created.update(pid=pid, c=c, e=e) or {})
    assert partners.register_lead(PARTNER, "", "ops@acme.com") == "invalid"
    assert partners.register_lead(PARTNER, "Acme", "not-an-email") == "invalid"
    assert partners.register_lead(PARTNER, "Acme", "JO@sterling.co.uk") == "self"
    assert partners.register_lead(PARTNER, "Acme", "dup@acme.com") == "duplicate"
    assert partners.register_lead(PARTNER, "Acme Haulage", "ops@acme.com") == "ok"
    assert created == {"pid": "p-1", "c": "Acme Haulage", "e": "ops@acme.com"}


def test_register_lead_for_existing_account_is_marked_ineligible_up_front(monkeypatch):
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])
    monkeypatch.setattr(db, "auth_user_exists", lambda e: e == "ops@existing.com")
    created = {}
    monkeypatch.setattr(db, "create_partner_lead",
                        lambda pid, c, e, exp, status="registered", reason=None:
                        created.update(e=e, status=status, reason=reason) or {})
    assert partners.register_lead(PARTNER, "Existing Ltd", "OPS@existing.com") == "exists"
    # recorded and marked — visible on the portal, never a silent refusal
    assert created == {"e": "ops@existing.com", "status": "ineligible",
                       "reason": "already_customer"}


def test_reconcile_earliest_live_lead_wins_when_no_code_used(monkeypatch):
    attached, updates = {}, {}
    monkeypatch.setattr(db, "list_leads_by_email", lambda e, status="registered": [
        {"id": "l-old-expired", "partner_id": "p-2",
         "expires_at": _iso(NOW - timedelta(days=1))},                  # expired → skipped
        {"id": "l-mine", "partner_id": "p-1",
         "expires_at": _iso(NOW + timedelta(days=30))},
    ])
    monkeypatch.setattr(db, "get_tenant_referral", lambda tid: {})      # unattributed
    monkeypatch.setattr(db, "get_partner",
                        lambda pid: PARTNER if pid == "p-1" else RIVAL)
    monkeypatch.setattr(db, "attach_referral",
                        lambda tid, pid, code, at: attached.update(pid=pid, code=code) or True)
    monkeypatch.setattr(db, "update_partner_lead",
                        lambda lid, fields: updates.update({lid: fields}))
    partners.reconcile_leads_for_signup("t1", "ops@acme.com")
    assert attached == {"pid": "p-1", "code": "STERLING41"}
    assert updates["l-mine"]["status"] == "converted"


def test_reconcile_when_code_already_won_marks_rival_lead_disputed(monkeypatch):
    updates = {}
    monkeypatch.setattr(db, "list_leads_by_email", lambda e, status="registered": [
        {"id": "l-rival", "partner_id": "p-2", "expires_at": _iso(NOW + timedelta(days=30))}])
    monkeypatch.setattr(db, "get_tenant_referral",
                        lambda tid: {"referred_by_partner_id": "p-1"})   # code won already
    monkeypatch.setattr(db, "update_partner_lead",
                        lambda lid, fields: updates.update({lid: fields}))
    partners.reconcile_leads_for_signup("t1", "ops@acme.com")
    assert updates["l-rival"]["status"] == "disputed"


def test_existing_account_marks_leads_already_customer(monkeypatch):
    updates = {}
    monkeypatch.setattr(db, "list_leads_by_email", lambda e, status="registered": [
        {"id": "l-1", "partner_id": "p-1"}])
    monkeypatch.setattr(db, "update_partner_lead",
                        lambda lid, fields: updates.update({lid: fields}))
    partners.mark_leads_already_customer("ops@existing.com")
    assert updates["l-1"] == {"status": "ineligible", "reason": "already_customer"}


# ── the commission ledger (earned = collected) ───────────────────────────────
def _referred(monkeypatch, referred_days_ago: int, has_commission: bool):
    monkeypatch.setattr(db, "get_tenant_referral", lambda tid: {
        "referred_by_partner_id": "p-1",
        "referred_at": _iso(NOW - timedelta(days=referred_days_ago))})
    monkeypatch.setattr(db, "get_partner", lambda pid: PARTNER)
    monkeypatch.setattr(db, "tenant_has_commission", lambda tid: has_commission)


def test_invoice_on_unreferred_tenant_earns_nothing(monkeypatch):
    monkeypatch.setattr(db, "get_tenant_referral", lambda tid: {})
    assert partners.record_commission_for_invoice("t1", {"amount_paid": 24800}) is False


def test_zero_invoice_never_earns(monkeypatch):
    _referred(monkeypatch, referred_days_ago=5, has_commission=False)
    assert partners.record_commission_for_invoice("t1", {"amount_paid": 0}) is False


def test_first_collected_payment_in_window_earns_scheduled_commission(monkeypatch):
    _referred(monkeypatch, referred_days_ago=30, has_commission=False)
    seen = {}
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at:
                        seen.update(pid=pid, inv=inv, total=total, amount=amount) or True)
    ok = partners.record_commission_for_invoice("t1", {"id": "in_1", "amount_paid": 24800})
    assert ok is True
    assert seen == {"pid": "p-1", "inv": "in_1", "total": 248.0, "amount": 49.60}


def test_first_payment_outside_90_days_earns_nothing(monkeypatch):
    _referred(monkeypatch, referred_days_ago=120, has_commission=False)
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda *a: pytest.fail("must not write outside the window"))
    assert partners.record_commission_for_invoice("t1", {"id": "in_1", "amount_paid": 9900}) is False


def test_converted_in_window_earns_for_life(monkeypatch):
    _referred(monkeypatch, referred_days_ago=400, has_commission=True)   # converted long ago
    seen = {}
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at: seen.update(amount=amount) or True)
    assert partners.record_commission_for_invoice("t1", {"id": "in_9", "amount_paid": 9900}) is True
    assert seen["amount"] == 19.80


def test_commission_uses_collected_amount_so_promos_are_exact(monkeypatch):
    # FOUNDING50: a £248 plan collects £124 — commission is 20% of the £124.
    _referred(monkeypatch, referred_days_ago=10, has_commission=False)
    seen = {}
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at:
                        seen.update(total=total, amount=amount) or True)
    partners.record_commission_for_invoice("t1", {"id": "in_2", "amount_paid": 12400})
    assert seen == {"total": 124.0, "amount": 24.80}


# ── the portal view-model ────────────────────────────────────────────────────
def test_referral_rows_follow_the_status_ladder(monkeypatch):
    referrals = [
        {"id": "t-a", "name": "Active Ltd", "plan": "professional", "industry": "logistics",
         "subscription_status": "active", "referred_at": _iso(NOW - timedelta(days=20))},
        {"id": "t-t", "name": "Trial Ltd", "plan": "starter",
         "subscription_status": "trialing", "referred_at": _iso(NOW - timedelta(days=5))},
        {"id": "t-p", "name": "Pending Ltd", "plan": "starter",
         "subscription_status": "past_due", "referred_at": _iso(NOW - timedelta(days=40))},
        {"id": "t-r", "name": "Registered Ltd", "plan": "pilot",
         "subscription_status": None, "referred_at": _iso(NOW - timedelta(days=2))},
        {"id": "t-c", "name": "Gone Ltd", "plan": "starter",
         "subscription_status": "canceled", "referred_at": _iso(NOW - timedelta(days=200))},
        {"id": "t-x", "name": "Never Ltd", "plan": "pilot",
         "subscription_status": None, "referred_at": _iso(NOW - timedelta(days=120))},
    ]
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: referrals)
    _no_ledger(monkeypatch)
    s = partners.referral_summary(PARTNER)
    by = {r["name"]: r for r in s["rows"]}
    assert by["Active Ltd"]["status"] == "Active" and by["Active Ltd"]["paying"]
    assert by["Trial Ltd"]["status"] == "Trial"
    assert by["Pending Ltd"]["status"] == "Payment pending"
    assert by["Registered Ltd"]["status"] == "Registered"
    assert by["Gone Ltd"]["status"] == "Cancelled"
    assert by["Never Ltd"]["status"] == "Expired"            # window passed, never converted
    # the 90-day window is VISIBLE on unconverted referrals
    expected_expiry = (NOW - timedelta(days=2) + timedelta(days=90)).date().isoformat()
    assert by["Registered Ltd"]["window_expires"] == expected_expiry
    assert by["Active Ltd"]["window_expires"] is None        # paying → no expiry
    assert s["active"] == 1
    # privacy: rows carry name/date/status/plan/money only
    assert set(by["Active Ltd"].keys()) <= {"name", "joined", "status", "paying", "expired",
                                            "window_expires", "plan", "monthly_gbp",
                                            "commission_gbp"}


def test_money_tiles_read_the_ledger_not_estimates(monkeypatch):
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [
        {"tenant_id": "t1", "amount": 49.60, "status": "paid",
         "earned_at": _iso(NOW - timedelta(days=40))},
        {"tenant_id": "t1", "amount": 49.60, "status": "scheduled", "earned_at": _iso(NOW)},
        {"tenant_id": "t2", "amount": 19.80, "status": "scheduled", "earned_at": _iso(NOW)},
        {"tenant_id": "t3", "amount": 5.00, "status": "void", "earned_at": _iso(NOW)},
    ])
    monkeypatch.setattr(db, "list_partner_payouts", lambda pid: [
        {"period_start": "2026-06-01", "period_end": "2026-06-30",
         "paid_at": "2026-07-01T09:00:00+00:00", "total": 49.60, "reference": "BLAM8-JUN"}])
    s = partners.referral_summary(PARTNER)
    assert s["scheduled"] == pytest.approx(69.40)
    assert s["lifetime"] == pytest.approx(119.0)             # void excluded
    assert s["this_month"] == pytest.approx(69.40)           # this month's earned (non-void)
    assert s["payouts"][0]["total"] == 49.60
    assert s["next_payout"]                                  # a date is always shown


def test_lead_views_surface_disputes_not_silence(monkeypatch):
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_payouts", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [
        {"company_name": "Acme", "contact_email": "a@a.com", "status": "disputed",
         "reason": "other_partner", "created_at": _iso(NOW), "expires_at": _iso(NOW)},
        {"company_name": "Old", "contact_email": "b@b.com", "status": "registered",
         "reason": None, "created_at": _iso(NOW - timedelta(days=100)),
         "expires_at": _iso(NOW - timedelta(days=10))},
    ])
    s = partners.referral_summary(PARTNER)
    by = {l["company"]: l for l in s["leads"]}
    assert by["Acme"]["status"] == "Disputed" and "another partner" in by["Acme"]["reason"]
    assert by["Old"]["status"] == "Expired"


# ── capture: ?ref= cookie at /login + landing, consumed at the callback ─────
def _quiet_login_page(monkeypatch):
    monkeypatch.setattr(auth_routes.slots, "google_slots",
                        lambda: {"used": 0, "cap": 100, "remaining": 100, "verified": False})
    monkeypatch.setattr(auth_routes.slots, "invite_gate_active", lambda: False)
    monkeypatch.setattr(auth_routes.slots, "invite_cookie_valid", lambda v: False)


def test_login_ref_param_sets_referral_cookie(client, monkeypatch):
    _quiet_login_page(monkeypatch)
    r = client.get("/login?ref=sterling41")
    assert r.status_code == 200
    assert "bl_ref=STERLING41" in _set_cookies(r)


def test_landing_ref_param_sets_referral_cookie(client, monkeypatch):
    import app.slots as slots
    monkeypatch.setattr(slots, "invite_gate_active", lambda: False)
    r = client.get("/?ref=STERLING41")
    assert r.status_code == 200
    assert "bl_ref=STERLING41" in _set_cookies(r)


def _fake_session(monkeypatch):
    async def fake_exchange(code, verifier):
        return {"access_token": "AT", "refresh_token": "RT",
                "user": {"id": "u1", "email": "ops@acme.com"}}
    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", fake_exchange)


def test_callback_new_account_attaches_cookie_code_then_reconciles_leads(client, monkeypatch):
    _fake_session(monkeypatch)
    calls = []
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)   # NEW
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", lambda uid, name, email=None: "t1")
    monkeypatch.setattr(auth_routes.partners, "attach_for_tenant",
                        lambda tid, code, signer_email=None:
                        calls.append(("attach", tid, code, signer_email)) or "attached")
    monkeypatch.setattr(auth_routes.partners, "reconcile_leads_for_signup",
                        lambda tid, email: calls.append(("leads", tid, email)))
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(partners.REF_COOKIE, "STERLING41")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert calls == [("attach", "t1", "STERLING41", "ops@acme.com"),
                     ("leads", "t1", "ops@acme.com")]
    assert 'bl_ref="";' in _set_cookies(r) or "bl_ref=;" in _set_cookies(r)  # consumed


def test_callback_existing_account_never_attributes_marks_leads_ineligible(client, monkeypatch):
    _fake_session(monkeypatch)
    calls = []
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t-old")  # EXISTING
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", lambda uid, name, email=None: "t-old")
    monkeypatch.setattr(auth_routes.partners, "attach_for_tenant",
                        lambda *a, **k: pytest.fail("existing accounts must not attribute"))
    monkeypatch.setattr(auth_routes.partners, "mark_leads_already_customer",
                        lambda email: calls.append(("ineligible", email)))
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(partners.REF_COOKIE, "STERLING41")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert calls == [("ineligible", "ops@acme.com")]


def test_callback_partner_first_signin_lands_on_portal_without_tenant(client, monkeypatch):
    _fake_session(monkeypatch)   # ops@acme.com — registered below as a partner email
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)   # first sign-in
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email",
                        lambda e: PARTNER if e == "ops@acme.com" else None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a: pytest.fail("a pure partner must not get a tenant"))
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(partners.REF_COOKIE, "RIVAL77")   # partners aren't referable
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/partner"
    cookies = _set_cookies(r)
    assert "bl_at=" in cookies                            # signed in
    assert 'bl_ref="";' in cookies or "bl_ref=;" in cookies   # ref discarded


def test_callback_partner_bypasses_google_invite_gate(client, monkeypatch):
    async def fake_exchange(code, verifier):
        return {"access_token": "AT", "refresh_token": "RT",
                "user": {"id": "u1", "email": "jo@sterling.co.uk",
                         "app_metadata": {"provider": "google"}}}
    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", fake_exchange)
    monkeypatch.setattr(auth_routes.slots, "invite_gate_active", lambda: True)
    monkeypatch.setattr(auth_routes.slots, "invite_cookie_valid", lambda v: False)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda e: PARTNER)

    async def never_delete(uid):
        pytest.fail("a registered partner is invited — must not be rejected")
    monkeypatch.setattr(auth_routes.auth, "admin_delete_user", never_delete)
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/partner"


def test_callback_partner_with_existing_tenant_enters_app_normally(client, monkeypatch):
    _fake_session(monkeypatch)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t-mine")
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email",
                        lambda e: pytest.fail("existing accounts skip partner recognition"))
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", lambda uid, name, email=None: "t-mine")
    monkeypatch.setattr(auth_routes.partners, "mark_leads_already_customer", lambda e: None)
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_callback_referral_failure_never_breaks_signin(client, monkeypatch):
    _fake_session(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("partners table missing")
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", lambda uid, name, email=None: "t1")
    monkeypatch.setattr(auth_routes.partners, "attach_for_tenant", boom)
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(partners.REF_COOKIE, "STERLING41")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"   # sign-in survived


# ── capture: the typed code on the onboarding interview ──────────────────────
@pytest.fixture
def tenant_client(client):
    app.dependency_overrides[require_tenant] = lambda: TenantCtx("t1", None)
    yield client
    app.dependency_overrides.clear()


def _quiet_interview_writes(monkeypatch):
    monkeypatch.setattr(onboarding.db, "set_tenant_industry", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "get_tenant_referral", lambda tid: {})


def test_interview_valid_code_attaches_and_continues(tenant_client, monkeypatch):
    _quiet_interview_writes(monkeypatch)
    seen = {}
    monkeypatch.setattr(onboarding.partners, "attach_for_tenant",
                        lambda tid, code, signer_email=None:
                        seen.update(tid=tid, code=code) or "attached")
    r = tenant_client.post("/onboarding/interview",
                           data={"industry": "logistics", "referral_code": "sterling41"})
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/mailbox"
    assert seen == {"tid": "t1", "code": "sterling41"}


def test_interview_bad_code_rerenders_with_error_not_silent_loss(tenant_client, monkeypatch):
    _quiet_interview_writes(monkeypatch)
    monkeypatch.setattr(onboarding.partners, "attach_for_tenant",
                        lambda *a, **k: "invalid")
    r = tenant_client.post("/onboarding/interview",
                           data={"industry": "logistics", "referral_code": "TYPO99"})
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/interview?ref_err=TYPO99&why=invalid"


def test_interview_own_code_is_refused_as_self_referral(tenant_client, monkeypatch):
    _quiet_interview_writes(monkeypatch)
    monkeypatch.setattr(onboarding.partners, "attach_for_tenant", lambda *a, **k: "self")
    r = tenant_client.post("/onboarding/interview",
                           data={"industry": "logistics", "referral_code": "STERLING41"})
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/interview?ref_err=STERLING41&why=self"


def test_interview_no_code_continues_untouched(tenant_client, monkeypatch):
    _quiet_interview_writes(monkeypatch)
    called = []
    monkeypatch.setattr(onboarding.partners, "attach_for_tenant",
                        lambda *a, **k: called.append(a) or "attached")
    r = tenant_client.post("/onboarding/interview", data={"industry": "logistics"})
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/mailbox"
    assert called == []


def test_interview_already_attributed_never_reattaches(tenant_client, monkeypatch):
    monkeypatch.setattr(onboarding.db, "set_tenant_industry", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "get_tenant_referral",
                        lambda tid: {"referral_code_used": "FIRST10"})
    called = []
    monkeypatch.setattr(onboarding.partners, "attach_for_tenant",
                        lambda *a, **k: called.append(a) or "attached")
    r = tenant_client.post("/onboarding/interview",
                           data={"industry": "logistics", "referral_code": "SECOND20"})
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/mailbox"
    assert called == []                                   # first code won, forever


# ── the stripe webhook hook (unit-level: the branch calls the ledger) ────────
def test_plan_monthly_gbp_context_values():
    assert partners.plan_monthly_gbp({"plan": "email"}) == 39.0
    assert partners.plan_monthly_gbp({"plan": "starter"}) == 99.0
    assert partners.plan_monthly_gbp({"plan": "professional", "industry": "wholesale"}) == 248.0
    assert partners.plan_monthly_gbp({"plan": "professional", "industry": "logistics"}) == 298.0
    assert partners.plan_monthly_gbp({"plan": "enterprise", "contract_monthly_gbp": 1200}) == 1200.0
    assert partners.plan_monthly_gbp({"plan": "pilot"}) is None


# ── the /partner portal ──────────────────────────────────────────────────────
def _sign_in(client, monkeypatch, email="jo@sterling.co.uk"):
    async def fake_get_user(access):
        return {"id": "u-p", "email": email} if access == "AT" else None
    monkeypatch.setattr(auth, "get_user", fake_get_user)
    client.cookies.set(auth.AT_COOKIE, "AT")


def test_partner_page_requires_signin(client):
    r = client.get("/partner")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_partner_page_non_partner_gets_the_enrolment_form(client, monkeypatch):
    _sign_in(client, monkeypatch, email="not-a-partner@acme.com")
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    r = client.get("/partner")
    assert r.status_code == 200
    assert "Become a BLAM8 partner" in r.text          # self-serve, not a closed door
    assert "/partner/terms" in r.text                  # click-wrap links the terms
    assert "Acme" in r.text                            # name prefilled from the email


# ── self-serve enrolment (signing the agreement) ─────────────────────────────
def test_enroll_self_serve_mints_code_and_records_the_signature(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    seen = {}
    monkeypatch.setattr(db, "create_partner",
                        lambda name, email, code, commission_rate=0.20, notes=None,
                        source="operator", agreed_at=None, terms_version=None,
                        signed_name=None:
                        seen.update(name=name, email=email, code=code, source=source,
                                    agreed=bool(agreed_at), version=terms_version,
                                    signed=signed_name)
                        or {**PARTNER, "ref_code": code})
    p = partners.enroll_self_serve("JO@sterling.co.uk", "Sterling Accounts", "Jo Sterling")
    assert p and seen["email"] == "jo@sterling.co.uk"
    assert seen["code"].startswith("STERLING")
    assert seen["source"] == "self_serve" and seen["agreed"]
    assert seen["version"] == partners.TERMS_VERSION
    assert seen["signed"] == "Jo Sterling"                    # the e-signature


def test_enroll_without_business_name_uses_the_signature_for_name_and_code(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    seen = {}
    monkeypatch.setattr(db, "create_partner",
                        lambda name, email, code, **kw:
                        seen.update(name=name, code=code) or {**PARTNER, "ref_code": code})
    p = partners.enroll_self_serve("jo@sterling.co.uk", "", "Jo Sterling")
    assert p and seen["name"] == "Jo Sterling"          # trading name optional
    assert seen["code"].startswith("JOSTERLI")          # code minted from the signature


def test_enroll_self_serve_requires_a_signature(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    monkeypatch.setattr(db, "create_partner",
                        lambda *a, **k: pytest.fail("no signature, no partner"))
    assert partners.enroll_self_serve("jo@sterling.co.uk", "Sterling", "  ") is None


def test_enroll_self_serve_is_idempotent_for_existing_partners(monkeypatch):
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: PARTNER)
    monkeypatch.setattr(db, "create_partner",
                        lambda *a, **k: pytest.fail("must not double-mint"))
    assert partners.enroll_self_serve("jo@sterling.co.uk", "Sterling", "Jo") == PARTNER


def test_enroll_route_requires_terms_acceptance(client, monkeypatch):
    _sign_in(client, monkeypatch, email="new@acme.com")
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    r = client.post("/partner/enroll",
                    data={"partner_name": "Acme", "signed_name": "Jo"})   # no agree
    assert r.status_code == 303 and r.headers["location"] == "/partner?err=agree"


def test_enroll_route_requires_the_typed_signature(client, monkeypatch):
    _sign_in(client, monkeypatch, email="new@acme.com")
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    r = client.post("/partner/enroll", data={"partner_name": "Acme", "agree": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/partner?err=sign"


def test_enroll_route_creates_partner_and_lands_on_portal(client, monkeypatch):
    _sign_in(client, monkeypatch, email="new@acme.com")
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: None)
    seen = {}
    monkeypatch.setattr(partners, "enroll_self_serve",
                        lambda email, name, signed_name:
                        seen.update(email=email, name=name, signed=signed_name) or PARTNER)
    r = client.post("/partner/enroll",
                    data={"partner_name": "Acme", "signed_name": "Jo Bloggs", "agree": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/partner"
    assert seen == {"email": "new@acme.com", "name": "Acme", "signed": "Jo Bloggs"}


def test_join_link_sets_partner_intent_and_shows_partner_login(client):
    r = client.get("/partner/join")
    assert r.status_code == 303 and r.headers["location"] == "/login?partner=1"
    assert "bl_pjoin=1" in _set_cookies(r)


def test_terms_page_is_public_and_states_the_deal(client):
    r = client.get("/partner/terms")
    assert r.status_code == 200
    assert "20%" in r.text and "90" in r.text and "self-referral" in r.text.lower()


def test_callback_partner_intent_skips_tenant_and_lands_on_enrolment(client, monkeypatch):
    _fake_session(monkeypatch)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)   # brand new
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda e: None)    # not yet enrolled
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a: pytest.fail("partner intent must not create a tenant"))
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(partners.PARTNER_INTENT_COOKIE, "1")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/partner"
    cookies = _set_cookies(r)
    assert "bl_at=" in cookies                                        # signed in
    assert 'bl_pjoin="";' in cookies or "bl_pjoin=;" in cookies       # intent consumed


def test_partner_page_shows_all_areas(client, monkeypatch):
    _sign_in(client, monkeypatch)
    monkeypatch.setattr(db, "get_partner_by_email",
                        lambda e: PARTNER if e == "jo@sterling.co.uk" else None)
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [
        {"id": "t1", "name": "Acme Haulage", "plan": "professional", "industry": "logistics",
         "subscription_status": "active", "referred_at": _iso(NOW - timedelta(days=30))}])
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [
        {"tenant_id": "t1", "amount": 59.60, "status": "scheduled", "earned_at": _iso(NOW)}])
    monkeypatch.setattr(db, "list_partner_payouts", lambda pid: [])
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])
    r = client.get("/partner")
    assert r.status_code == 200
    assert "STERLING41" in r.text                       # code
    assert "/login?ref=STERLING41" in r.text            # share link
    assert "I partner with BLAM8" in r.text             # approved share text
    assert "Acme Haulage" in r.text
    assert "£59.60" in r.text                           # scheduled tile (ledger, not estimate)
    assert "Register an introduction" in r.text         # leads
    assert "Payouts" in r.text
    assert "Promote accurately" in r.text               # assets


def test_partner_page_offers_business_setup_only_without_a_tenant(client, monkeypatch):
    _sign_in(client, monkeypatch)
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: PARTNER)
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [])
    _no_ledger(monkeypatch)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)     # pure partner
    r = client.get("/partner")
    assert "Use BLAM8 for my business" in r.text and "My dashboard" not in r.text

    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t-mine")  # also a tenant
    r = client.get("/partner")
    assert "My dashboard" in r.text and "Use BLAM8 for my business" not in r.text


def test_partner_setup_creates_tenant_and_enters_the_app(client, monkeypatch):
    _sign_in(client, monkeypatch)
    seen = {}
    monkeypatch.setattr(db, "ensure_tenant_for_user",
                        lambda uid, name, email=None: seen.update(uid=uid, name=name) or "t-new")
    r = client.post("/partner/setup")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert seen["uid"] == "u-p" and seen["name"] == "Sterling"   # from jo@sterling.co.uk


def test_partner_lead_form_posts_and_redirects(client, monkeypatch):
    _sign_in(client, monkeypatch)
    monkeypatch.setattr(db, "get_partner_by_email", lambda e: PARTNER)
    seen = {}
    monkeypatch.setattr(db, "list_partner_leads", lambda pid: [])
    monkeypatch.setattr(db, "create_partner_lead",
                        lambda pid, c, e, exp: seen.update(c=c, e=e) or {})
    r = client.post("/partner/leads",
                    data={"company_name": "Acme Haulage", "contact_email": "OPS@Acme.com"})
    assert r.status_code == 303 and r.headers["location"] == "/partner?lead=ok#leads"
    assert seen == {"c": "Acme Haulage", "e": "ops@acme.com"}


# ── operator console ─────────────────────────────────────────────────────────
@pytest.fixture
def admin_client(client):
    app.dependency_overrides[require_admin] = \
        lambda: AdminCtx(user_id="a1", email="ops@blam8.com", role="owner")
    yield client
    app.dependency_overrides.clear()


def test_admin_partners_lists_with_ledger_summaries(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_partners", lambda: [PARTNER])
    monkeypatch.setattr(db, "list_partner_referrals", lambda pid: [])
    monkeypatch.setattr(db, "list_all_partner_leads", lambda: [])
    # The page also renders the cross-partner withdrawal queue.
    monkeypatch.setattr(db, "list_open_withdrawals", lambda: [])
    _no_ledger(monkeypatch)
    r = admin_client.get("/admin/partners")
    assert r.status_code == 200
    assert "Sterling Accounts" in r.text and "STERLING41" in r.text


def test_admin_creates_partner_minting_a_code(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    monkeypatch.setattr(db, "create_partner",
                        lambda name, email, code, commission_rate=0.20, notes=None:
                        seen.update(name=name, email=email, code=code, rate=commission_rate) or PARTNER)
    r = admin_client.post("/admin/partners/new",
                          data={"name": "Sterling Accounts", "email": "JO@sterling.co.uk"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/partners"
    assert seen["name"] == "Sterling Accounts"
    assert seen["code"].startswith("STERLING") and seen["rate"] == 0.20


def test_admin_duplicate_partner_is_a_clean_error(admin_client, monkeypatch):
    def dupe(*a, **k):
        raise RuntimeError("unique violation")
    monkeypatch.setattr(db, "get_partner_by_code", lambda c: None)
    monkeypatch.setattr(db, "create_partner", dupe)
    r = admin_client.post("/admin/partners/new",
                          data={"name": "Sterling", "email": "jo@sterling.co.uk"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/partners?err=exists"


def test_admin_payout_run_pays_only_cleared_rows(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [
        {"id": "c1", "amount": 49.60, "status": "scheduled", "earned_at": "2026-06-03T10:00:00+00:00"},
        {"id": "c2", "amount": 19.80, "status": "scheduled", "earned_at": "2026-06-28T10:00:00+00:00"},
        {"id": "c3", "amount": 30.00, "status": "scheduled", "earned_at": _iso(NOW)},   # this month → still clearing
        {"id": "c4", "amount": -9.90, "status": "scheduled", "earned_at": _iso(NOW)},   # clawback → deducts NOW
        {"id": "c5", "amount": 99.00, "status": "paid", "earned_at": "2026-05-01T10:00:00+00:00"},
    ])
    created, marked = {}, {}
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda pid, total, start, end, ref:
                        created.update(pid=pid, total=total, ref=ref) or {"id": "pay-1"})
    monkeypatch.setattr(db, "mark_commission_ids_paid",
                        lambda ids, payout_id: marked.update(ids=sorted(ids), payout=payout_id))
    r = admin_client.post("/admin/partners/p-1/payout", data={"reference": "BLAM8-JUN"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/partners?msg=paid"
    assert created == {"pid": "p-1", "total": 59.50, "ref": "BLAM8-JUN"}   # 49.60+19.80-9.90
    assert marked == {"ids": ["c1", "c2", "c4"], "payout": "pay-1"}        # c3 still clearing


def test_admin_payout_under_threshold_rolls_forward(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [
        {"id": "c1", "amount": 7.80, "status": "scheduled", "earned_at": "2026-06-03T10:00:00+00:00"}])
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda *a: pytest.fail("under the £10 threshold — must roll forward"))
    r = admin_client.post("/admin/partners/p-1/payout", data={"reference": "X"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/partners?err=threshold"


def test_admin_payout_with_nothing_cleared_is_a_noop(admin_client, monkeypatch):
    monkeypatch.setattr(db, "list_partner_commissions", lambda pid: [
        {"id": "c1", "amount": 30.0, "status": "scheduled", "earned_at": _iso(NOW)}])  # clearing
    monkeypatch.setattr(db, "create_partner_payout",
                        lambda *a: pytest.fail("no payout without cleared commissions"))
    r = admin_client.post("/admin/partners/p-1/payout", data={"reference": "X"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/partners?err=nothing"


# ── conforming commission mechanics (net-of-tax base, clawback, hold) ────────
def test_commission_base_excludes_vat_when_present(monkeypatch):
    _referred(monkeypatch, referred_days_ago=10, has_commission=False)
    seen = {}
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at:
                        seen.update(total=total, amount=amount) or True)
    # £248 plan + 20% VAT: £297.60 collected, £49.60 of it is tax → base £248.
    partners.record_commission_for_invoice("t1", {"id": "in_3", "amount_paid": 29760,
                                                  "tax": 4960})
    assert seen == {"total": 297.60, "amount": 49.60}   # 20% of the ex-VAT £248


def test_full_refund_creates_a_full_clawback_row(monkeypatch):
    earned = {"id": "c1", "partner_id": "p-1", "tenant_id": "t1",
              "invoice_id": "in_1", "amount": 49.60, "status": "scheduled"}
    monkeypatch.setattr(db, "get_commission_by_invoice",
                        lambda inv: earned if inv == "in_1" else None)
    seen = {}
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at:
                        seen.update(pid=pid, inv=inv, amount=amount) or True)
    ok = partners.record_refund_for_charge(
        {"invoice": "in_1", "amount": 24800, "amount_refunded": 24800})
    assert ok is True
    assert seen == {"pid": "p-1", "inv": "in_1:clawback", "amount": -49.60}


def test_partial_refund_claws_back_proportionally_and_upserts(monkeypatch):
    earned = {"id": "c1", "partner_id": "p-1", "tenant_id": "t1",
              "invoice_id": "in_1", "amount": 49.60, "status": "scheduled"}
    clawback_row = {}
    def by_invoice(inv):
        if inv == "in_1":
            return earned
        return clawback_row or None
    monkeypatch.setattr(db, "get_commission_by_invoice", by_invoice)
    monkeypatch.setattr(db, "create_partner_commission",
                        lambda pid, tid, inv, total, amount, at:
                        clawback_row.update(id="cb1", status="scheduled", amount=amount) or True)
    updates = {}
    monkeypatch.setattr(db, "update_partner_commission",
                        lambda cid, fields: updates.update({cid: fields}))
    # first event: 50% refunded
    partners.record_refund_for_charge({"invoice": "in_1", "amount": 24800,
                                       "amount_refunded": 12400})
    assert clawback_row["amount"] == -24.80
    # second event: now fully refunded — the SAME row is updated to the cumulative value
    partners.record_refund_for_charge({"invoice": "in_1", "amount": 24800,
                                       "amount_refunded": 24800})
    assert updates == {"cb1": {"amount": -49.60}}


def test_refund_on_unreferred_or_uncommissioned_charge_is_a_noop(monkeypatch):
    monkeypatch.setattr(db, "get_commission_by_invoice", lambda inv: None)
    assert partners.record_refund_for_charge(
        {"invoice": "in_x", "amount": 9900, "amount_refunded": 9900}) is False
    assert partners.record_refund_for_charge({"amount": 9900, "amount_refunded": 9900}) is False


def test_payable_holds_current_month_but_never_holds_clawbacks():
    rows = [
        {"id": "a", "amount": 49.60, "status": "scheduled",
         "earned_at": _iso(NOW - timedelta(days=45))},        # prior month → cleared
        {"id": "b", "amount": 19.80, "status": "scheduled", "earned_at": _iso(NOW)},  # clearing
        {"id": "c", "amount": -9.90, "status": "scheduled", "earned_at": _iso(NOW)},  # clawback
        {"id": "d", "amount": 99.00, "status": "paid",
         "earned_at": _iso(NOW - timedelta(days=90))},        # already paid
    ]
    assert [c["id"] for c in partners.payable_commissions(rows)] == ["a", "c"]


def test_next_payout_is_the_last_day_of_this_month():
    d = partners.next_payout_date()
    from datetime import date, timedelta as td
    parsed = date.fromisoformat(d)
    assert parsed.month == NOW.month and parsed.year == NOW.year
    assert (parsed + td(days=1)).day == 1                     # genuinely month-end