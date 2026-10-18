"""SSO sign-in (Google / Microsoft) over Supabase Auth — the passwordless flow
that replaced email/password.

Pins: PKCE challenge derivation, the default-company-name heuristic, the SSO
start redirect (+ verifier cookie), provider whitelisting, and the callback
exchanging the code for a session (rejecting a missing/expired/failed one).

Run: python -m pytest tests/ -q
"""
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import auth, auth_routes
from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    return TestClient(app, follow_redirects=False)   # no `with`: skip startup events


def _set_cookies(r) -> str:
    return " ".join(r.headers.get_list("set-cookie"))


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_pkce_pair_is_a_valid_s256_challenge():
    verifier, challenge = auth._pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge            # url-safe, unpadded


def test_authorize_url_carries_provider_and_challenge():
    url, verifier = auth.sso_authorize_url("google", "https://app/auth/callback")
    assert "/authorize?" in url
    assert "provider=google" in url
    assert "code_challenge=" in url and "code_challenge_method=s256" in url
    assert "redirect_to=https%3A%2F%2Fapp%2Fauth%2Fcallback" in url
    assert verifier   # handed back for the cookie


def test_authorize_url_forces_account_chooser():
    """Without prompt=select_account the provider silently reuses the browser's
    current session — after /logout you cannot sign in as a different account.
    GoTrue forwards the param to the provider; standard OIDC for both."""
    for provider in ("google", "azure"):
        url, _ = auth.sso_authorize_url(provider, "https://app/auth/callback")
        assert "prompt=select_account" in url


def test_azure_authorize_url_requests_email_scope():
    """GoTrue's Azure default is a bare `openid` scope; personal Microsoft
    accounts then return no email claim and EVERY sign-in dies at the callback
    with "Error getting user email from external provider". The authorize URL
    must ask for the OIDC email+profile scopes. Google keeps GoTrue's default
    (already includes email) — changing it would re-prompt existing users."""
    azure_url, _ = auth.sso_authorize_url("azure", "https://app/auth/callback")
    assert "scopes=email+profile" in azure_url
    google_url, _ = auth.sso_authorize_url("google", "https://app/auth/callback")
    assert "scopes=" not in google_url


@pytest.mark.parametrize("email,expected", [
    ("ops@acme-logistics.co.uk", "Acme Logistics"),   # work domain -> main label
    ("admin.blam8@gmail.com", "Admin Blam8"),          # consumer -> local part
    ("jane@outlook.com", "Jane"),
    (None, None),
    ("nope", None),
])
def test_default_company_name(email, expected):
    assert auth.default_company_name(email) == expected


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("provider", ["google", "azure"])
def test_sso_start_redirects_and_sets_verifier_cookie(client, provider):
    r = client.get(f"/auth/sso/{provider}")
    assert r.status_code == 303
    assert f"provider={provider}" in r.headers["location"]
    assert "bl_pkce=" in _set_cookies(r)   # verifier stashed for the callback


def test_sso_start_rejects_unknown_provider(client):
    r = client.get("/auth/sso/facebook")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=")
    assert "bl_pkce=" not in _set_cookies(r)


def test_callback_without_code_or_verifier_bounces_to_login(client):
    r = client.get("/auth/callback")   # no code, no verifier cookie
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=")


def test_callback_with_provider_error_bounces_to_login(client):
    r = client.get("/auth/callback?error=access_denied")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=")


def test_callback_exchanges_code_and_starts_session(client, monkeypatch):
    async def fake_exchange(code, verifier):
        assert code == "the-code" and verifier == "v123"
        return {"access_token": "AT", "refresh_token": "RT",
                "user": {"id": "u1", "email": "ops@acme.com"}}

    seen = {}
    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", fake_exchange)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda uid, name, email=None: seen.update(uid=uid, name=name) or "t1")

    client.cookies.set(auth.PKCE_COOKIE, "v123")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303
    assert r.headers["location"] == "/app"
    cookies = _set_cookies(r)
    assert "bl_at=" in cookies and "bl_rt=" in cookies
    assert seen == {"uid": "u1", "name": "Acme"}   # tenant ensured from the email


def test_callback_handles_failed_exchange(client, monkeypatch):
    async def boom(code, verifier):
        raise ValueError("bad code")
    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", boom)
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    r = client.get("/auth/callback?code=x")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=")


def test_callback_drops_the_stale_user_tenant_hop(client, monkeypatch):
    """Run #8 (the SSO bounce on every reseed): the user→tenant hop cached the
    tenant deleted minutes earlier, the callback minted a new one, and the next
    request resolved the ghost. Sign-in now re-establishes the hop."""
    from app import navcache

    async def fake_exchange(code, verifier):
        return {"access_token": "AT", "refresh_token": "RT",
                "user": {"id": "u1", "email": "ops@acme.com"}}

    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", fake_exchange)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda uid, name, email=None: "t-new")
    monkeypatch.setattr(navcache, "_disabled", False)      # the conftest disables it
    navcache.put("ut:u1", 300.0, "t-ghost")
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert navcache.peek("ut:u1") == (True, "t-new")
    navcache._store.clear()


def _continue_cookies(client, csrf="csrf-1"):
    from app.security import CSRF_COOKIE
    client.cookies.set(auth.AT_COOKIE, "AT")
    client.cookies.set(CSRF_COOKIE, csrf)


def test_continue_uses_existing_active_tenant_without_provisioning(client, monkeypatch):
    async def fake_get_user(access):
        assert access == "AT"
        return {"id": "u1", "email": "ops@acme.com"}

    monkeypatch.setattr(auth_routes.auth, "get_user", fake_get_user)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t-existing")
    monkeypatch.setattr(auth_routes.db, "get_tenant", lambda tid: {"id": tid})
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a, **k: pytest.fail("existing tenant must not be provisioned"))
    monkeypatch.setattr(auth_routes.db, "touch_member_seen", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.db, "update_member", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.partners, "mark_leads_already_customer", lambda *a: None)
    _continue_cookies(client)

    response = client.post("/auth/continue", data={"csrf_token": "csrf-1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/app"


def test_continue_provisions_missing_tenant_and_replaces_stale_cache(client, monkeypatch):
    from app import navcache

    async def fake_get_user(access):
        return {"id": "u1", "email": "ops@acme.com"}

    calls = []
    monkeypatch.setattr(auth_routes.auth, "get_user", fake_get_user)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda email: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda uid, name, email=None: calls.append((uid, name, email)) or "t-new")
    monkeypatch.setattr(auth_routes.db, "get_tenant", lambda tid: {"id": tid})
    monkeypatch.setattr(auth_routes.db, "touch_member_seen", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.db, "update_member", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.partners, "reconcile_leads_for_signup", lambda *a: None)
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache.put("ut:u1", 300.0, "t-deleted")
    _continue_cookies(client)

    response = client.post("/auth/continue", data={"csrf_token": "csrf-1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert calls == [("u1", "Acme", "ops@acme.com")]
    assert navcache.peek("ut:u1") == (True, "t-new")


def test_continue_does_not_bypass_switched_off_membership(client, monkeypatch):
    async def fake_get_user(access):
        return {"id": "u1", "email": "ops@acme.com"}

    def switched_off(*args, **kwargs):
        raise auth_routes.db.MembershipSwitchedOff()

    monkeypatch.setattr(auth_routes.auth, "get_user", fake_get_user)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", switched_off)
    _continue_cookies(client)

    response = client.post("/auth/continue", data={"csrf_token": "csrf-1"})

    assert response.status_code == 303
    assert "switched%20off" in response.headers["location"]
    cookies = _set_cookies(response)
    assert 'bl_at=""' in cookies and "Max-Age=0" in cookies


def test_continue_blocks_unreadable_tenant_without_retry_loop(client, monkeypatch):
    from app import navcache

    async def fake_get_user(access):
        return {"id": "u1", "email": "ops@acme.com"}

    monkeypatch.setattr(auth_routes.auth, "get_user", fake_get_user)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t-ghost")
    monkeypatch.setattr(auth_routes.db, "get_tenant", lambda tid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a, **k: pytest.fail("do not replace an orphaned membership"))
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache.put("ut:u1", 300.0, "t-ghost")
    _continue_cookies(client)

    response = client.post("/auth/continue", data={"csrf_token": "csrf-1"})

    assert response.status_code == 303
    assert "continue_blocked=1" in response.headers["location"]
    assert navcache.peek("ut:u1")[0] is False


def test_continue_requires_csrf_before_reading_session_or_creating_tenant(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a, **k: pytest.fail("CSRF failure must not provision"))

    response = client.post("/auth/continue", data={})

    assert response.status_code == 403


def test_continue_refreshes_expired_access_session_and_sets_new_cookies(client, monkeypatch):
    async def expired_access(access):
        return None

    async def refresh(refresh_token):
        assert refresh_token == "RT"
        return {"access_token": "AT-new", "refresh_token": "RT-new",
                "user": {"id": "u1", "email": "ops@acme.com"}}

    monkeypatch.setattr(auth_routes.auth, "get_user", expired_access)
    monkeypatch.setattr(auth_routes.auth, "refresh_session", refresh)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t-existing")
    monkeypatch.setattr(auth_routes.db, "get_tenant", lambda tid: {"id": tid})
    monkeypatch.setattr(auth_routes.db, "touch_member_seen", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.db, "update_member", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes.partners, "mark_leads_already_customer", lambda *a: None)
    from app.security import CSRF_COOKIE
    client.cookies.set(auth.AT_COOKIE, "AT-expired")
    client.cookies.set(auth.RT_COOKIE, "RT")
    client.cookies.set(CSRF_COOKIE, "csrf-1")

    response = client.post("/auth/continue", data={"csrf_token": "csrf-1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    cookies = _set_cookies(response)
    assert "bl_at=AT-new" in cookies and "bl_rt=RT-new" in cookies


def test_login_refreshes_expired_access_token_before_showing_continue(client, monkeypatch):
    async def no_user(access):
        return None

    async def refresh(refresh_token):
        assert refresh_token == "RT"
        return {"access_token": "AT-new", "refresh_token": "RT-new",
                "user": {"id": "u1", "email": "ops@acme.com"}}

    monkeypatch.setattr(auth_routes.auth, "get_user", no_user)
    monkeypatch.setattr(auth_routes.auth, "refresh_session", refresh)
    client.cookies.set(auth.RT_COOKIE, "RT")

    response = client.get("/login")

    assert response.status_code == 200
    assert "Continue as ops@acme.com" in response.text
    assert 'action="/auth/continue"' in response.text
    assert "bl_at=AT-new" in _set_cookies(response)
    assert "bl_rt=RT-new" in _set_cookies(response)
