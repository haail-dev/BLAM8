"""Gmail-connect invite gate (src/app/slots.py + /oauth routes).

Google's unverified-app cap is burned by the Gmail MAILBOX connect (restricted
gmail.* scopes), not by SSO sign-in (basic scopes, uncapped) — so the gate
lives server-side in /oauth/connect while sign-in is open to everyone.
GOOGLE_INVITE_CODES / GOOGLE_INVITE_GATE arm it; Microsoft is never gated, and
the gate lifts automatically once GOOGLE_OAUTH_VERIFIED=true. A passed code
sets an HMAC-signed cookie so returning users aren't re-asked.

Run: python -m pytest tests/test_google_invite.py -q
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app import db, slots
from app.config import settings


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "google_invite_codes", "Founder8, mate-rates ,X99")
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(settings, "token_encryption_key", "unit-test-key")
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    slots._cache.update(at=-1e9, used=0)     # never reuse a cached slot count
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 5)


def test_gate_active_only_when_codes_set_and_unverified(gated, monkeypatch):
    assert slots.invite_gate_active() is True
    monkeypatch.setattr(settings, "google_oauth_verified", True)
    assert slots.invite_gate_active() is False           # verification lifts the gate
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(settings, "google_invite_codes", "   ")
    assert slots.invite_gate_active() is False           # no codes = no gate


def test_codes_match_case_insensitively_and_trimmed(gated):
    assert slots.invite_code_ok("founder8")
    assert slots.invite_code_ok("  FOUNDER8  ")
    assert slots.invite_code_ok("MATE-RATES")
    assert not slots.invite_code_ok("founder")
    assert not slots.invite_code_ok("")


def test_cookie_is_signed_and_tamperproof(gated):
    good = slots.invite_cookie_value()
    assert slots.invite_cookie_valid(good)
    assert not slots.invite_cookie_valid(None)
    assert not slots.invite_cookie_valid("")
    assert not slots.invite_cookie_valid(good[:-1] + ("0" if good[-1] != "0" else "1"))


def test_single_use_db_invite_redeems_once(gated, monkeypatch):
    """A minted /admin invite passes via db.use_google_invite exactly once."""
    burned = []

    def fake_use(code):
        if code in burned:
            return False
        burned.append(code)
        return code == "BLAM8-TEST-CODE"

    monkeypatch.setattr(slots.db, "use_google_invite", fake_use)
    assert slots.redeem_invite("BLAM8-TEST-CODE") is True
    assert slots.redeem_invite("BLAM8-TEST-CODE") is False   # consumed
    assert slots.redeem_invite("BLAM8-NOPE-NOPE") is False


def test_gate_arms_on_flag_alone(monkeypatch):
    """GOOGLE_INVITE_GATE=true arms the gate even with no env channel codes."""
    monkeypatch.setattr(settings, "google_invite_codes", "")
    monkeypatch.setattr(settings, "google_invite_gate", True)
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    assert slots.invite_gate_active() is True
    monkeypatch.setattr(settings, "google_oauth_verified", True)
    assert slots.invite_gate_active() is False


def test_db_errors_fail_closed(gated, monkeypatch):
    def boom(code):
        raise RuntimeError("db down")
    monkeypatch.setattr(slots.db, "use_google_invite", boom)
    assert slots.redeem_invite("BLAM8-ANY-CODE") is False    # not a 500, not a pass
    assert slots.redeem_invite("founder8") is True           # env codes still work


# ── sign-in is OPEN — the gate no longer touches /auth ───────────────────────
def test_sso_start_is_ungated_for_google(gated, monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    monkeypatch.setattr(auth, "sso_authorize_url",
                        lambda provider, redirect: ("https://sso.example/consent", "ver"))
    client = TestClient(app)
    r = client.get("/auth/sso/google", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://sso.example/consent"


def test_callback_admits_new_google_user_without_invite(gated, monkeypatch):
    """A NEW Google SSO account signs straight in — no rejection, no user
    deletion: sign-in doesn't consume a Gmail grant."""
    from fastapi.testclient import TestClient
    from app import auth, auth_routes
    from app.main import app

    async def fake_exchange(code, verifier):
        return {"access_token": "at", "refresh_token": "rt",
                "user": {"id": "u-new", "email": "someone@gmail.com",
                         "app_metadata": {"provider": "google"}}}

    async def never_delete(uid):
        pytest.fail("sign-in is open — no auth user must be deleted")

    monkeypatch.setattr(auth, "exchange_code_for_session", fake_exchange)
    monkeypatch.setattr(auth, "admin_delete_user", never_delete)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda e: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user", lambda uid, name, email=None: "t-1")
    client = TestClient(app)
    client.cookies.set(auth.PKCE_COOKIE, "verifier")
    r = client.get("/auth/callback?code=x", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app"


# ── /oauth/connect — the real enforcement point ──────────────────────────────
@contextmanager
def _tenant_client(monkeypatch, mailboxes=(), onboarded=False):
    """A TestClient signed in as tenant t1, with the connect flow's DB touches
    mocked out. `mailboxes` seeds what list_mailboxes returns."""
    from fastapi.testclient import TestClient
    from app import oauth as oauth_mod
    from app.auth import TenantCtx, require_tenant
    from app.main import app
    from app.security import require_csrf

    monkeypatch.setattr(oauth_mod.db, "list_mailboxes", lambda tid: list(mailboxes))
    monkeypatch.setattr(oauth_mod.db, "is_onboarded", lambda tid: onboarded)
    monkeypatch.setattr(oauth_mod.db, "put_oauth_state", lambda state, tid, mode="replace": None)
    monkeypatch.setattr(oauth_mod.gmail, "build_authorize_url",
                        lambda state: "https://accounts.google.example/consent")
    monkeypatch.setattr(oauth_mod.graph, "build_authorize_url",
                        lambda state: "https://login.microsoft.example/consent")
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app, follow_redirects=False)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def tenant_client(monkeypatch):
    with _tenant_client(monkeypatch) as client:
        yield client


def test_connect_google_blocked_without_invite(gated, tenant_client):
    """A crafted /oauth/connect URL cannot burn a grant — the gate is server-side."""
    r = tenant_client.get("/oauth/connect?provider=google")
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/mailbox?gmail=gate"


def test_connect_google_blocked_lands_on_settings_when_onboarded(gated, monkeypatch):
    with _tenant_client(monkeypatch, onboarded=True) as client:
        r = client.get("/oauth/connect?provider=google")
        assert r.headers["location"] == "/app/settings?gmail=gate"


def test_connect_google_add_mode_lands_on_mailboxes(gated, monkeypatch):
    with _tenant_client(monkeypatch, onboarded=True) as client:
        r = client.get("/oauth/connect?provider=google&mode=add")
        assert r.headers["location"] == "/app/mailboxes?gmail=gate"


def test_connect_google_passes_with_invite_cookie(gated, tenant_client):
    tenant_client.cookies.set(slots.INVITE_COOKIE, slots.invite_cookie_value())
    r = tenant_client.get("/oauth/connect?provider=google")
    assert r.status_code == 307
    assert r.headers["location"] == "https://accounts.google.example/consent"


def test_connect_google_open_for_tenant_with_existing_gmail(gated, monkeypatch):
    """The two live founding Gmail connections must keep reconnecting freely —
    re-consenting the same account doesn't burn a new grant."""
    with _tenant_client(monkeypatch, mailboxes=[{"provider": "google"}]) as client:
        r = client.get("/oauth/connect?provider=google")
        assert r.headers["location"] == "https://accounts.google.example/consent"


def test_connect_google_full_blocks_even_with_code(gated, monkeypatch):
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 100)
    slots._cache.update(at=-1e9, used=0)
    with _tenant_client(monkeypatch) as client:
        client.cookies.set(slots.INVITE_COOKIE, slots.invite_cookie_value())
        r = client.get("/oauth/connect?provider=google")
        assert r.headers["location"] == "/onboarding/mailbox?gmail=full"


def test_connect_microsoft_never_gated(gated, tenant_client):
    r = tenant_client.get("/oauth/connect?provider=microsoft")
    assert r.headers["location"] == "https://login.microsoft.example/consent"


def test_connect_google_open_once_verified(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", True)
    monkeypatch.setattr(settings, "google_invite_codes", "FOUNDER8")
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with _tenant_client(monkeypatch) as client:
        r = client.get("/oauth/connect?provider=google")
        assert r.headers["location"] == "https://accounts.google.example/consent"


# ── redeeming a code at the connect step ─────────────────────────────────────
def test_invite_post_sets_cookie_and_continues_to_consent(gated, tenant_client):
    r = tenant_client.post("/oauth/google-invite",
                           data={"code": "founder8", "next": "/onboarding/mailbox"})
    assert r.status_code == 303
    assert r.headers["location"] == "/oauth/connect?provider=google&mode=replace"
    assert slots.INVITE_COOKIE in r.cookies
    assert slots.invite_cookie_valid(r.cookies[slots.INVITE_COOKIE])


def test_invite_post_rejects_bad_code_back_to_surface(gated, tenant_client, monkeypatch):
    monkeypatch.setattr(slots.db, "use_google_invite", lambda code: False)
    r = tenant_client.post("/oauth/google-invite",
                           data={"code": "wrong", "next": "/onboarding/mailbox"})
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding/mailbox?gmail=badcode"
    assert slots.INVITE_COOKIE not in r.cookies


def test_invite_post_whitelists_next(gated, tenant_client, monkeypatch):
    monkeypatch.setattr(slots.db, "use_google_invite", lambda code: False)
    r = tenant_client.post("/oauth/google-invite",
                           data={"code": "wrong", "next": "https://evil.example/phish"})
    assert r.headers["location"] == "/app/settings?gmail=badcode"


def test_invite_post_carries_add_mode(gated, tenant_client):
    r = tenant_client.post("/oauth/google-invite",
                           data={"code": "founder8", "mode": "add", "next": "/app/mailboxes"})
    assert r.headers["location"] == "/oauth/connect?provider=google&mode=add"


# ── "no code? request access" from the connect step ──────────────────────────
def test_request_access_queues_and_confirms(gated, monkeypatch):
    stored = []
    monkeypatch.setattr(settings, "platform_notify_mailbox", "")
    from app import oauth as oauth_mod
    monkeypatch.setattr(oauth_mod.db, "create_google_access_request",
                        lambda email, message: stored.append((email, message)) or {"id": "r1"})
    with _tenant_client(monkeypatch) as client:
        r = client.post("/oauth/request-google-access",
                        data={"email": "owner@somegarage.co.uk",
                              "message": "MOT garage, 2 vans", "next": "/onboarding/mailbox"})
        assert r.status_code == 303
        assert r.headers["location"] == "/onboarding/mailbox?gmail=requested"
    assert stored == [("owner@somegarage.co.uk", "MOT garage, 2 vans")]


def test_request_access_rejects_junk_email(gated, monkeypatch):
    from app import oauth as oauth_mod
    monkeypatch.setattr(oauth_mod.db, "create_google_access_request",
                        lambda email, message: (_ for _ in ()).throw(AssertionError("should not store")))
    with _tenant_client(monkeypatch) as client:
        r = client.post("/oauth/request-google-access",
                        data={"email": "not-an-email", "next": "/app/settings"})
        assert r.status_code == 303
        assert r.headers["location"] == "/app/settings?gmail=bademail"


def test_onboarding_mailbox_renders_gate_instead_of_gmail_button(gated, monkeypatch):
    """With the gate armed, the mailbox step swaps the plain "Connect Gmail"
    link for the invite box (code entry + request access + live slot count) —
    while the Outlook button stays a one-click connect."""
    from app import onboarding as onb
    monkeypatch.setattr(onb.db, "get_tenant", lambda tid, client=None: {})
    monkeypatch.setattr(onb.db, "get_connection_status", lambda tid, client=None: None)
    with _tenant_client(monkeypatch) as client:
        r = client.get("/onboarding/mailbox")
        assert r.status_code == 200
        assert 'href="/oauth/connect?provider=google"' not in r.text
        assert 'action="/oauth/google-invite"' in r.text
        assert 'action="/oauth/request-google-access"' in r.text
        assert "95 of 100 left" in r.text            # live count (5 connected)
        assert 'href="/oauth/connect?provider=microsoft"' in r.text

        # a redeemed invite cookie restores the one-click Gmail button
        client.cookies.set(slots.INVITE_COOKIE, slots.invite_cookie_value())
        r2 = client.get("/onboarding/mailbox")
        assert 'href="/oauth/connect?provider=google"' in r2.text
        assert 'action="/oauth/google-invite"' not in r2.text


def test_landing_surfaces_gotrue_errors_on_login(gated):
    """GoTrue lands OAuth errors on the Site URL (the homepage) — they must be
    forwarded to /login with a readable message, not silently swallowed."""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/?error=invalid_request&error_code=bad_oauth_state"
                   "&error_description=OAuth+state+not+found+or+expired",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=")
    assert "expired" in r.headers["location"]
    # a clean landing hit is untouched
    assert client.get("/", follow_redirects=False).status_code == 200


# ── /oauth/invite/{code} — the tappable invite (2026-09-10) ─────────────────
def test_invite_link_page_prefills_the_gate_form(gated, monkeypatch):
    """One link from the operator: the page names the inbox connected today and
    posts the ordinary gate form with the code already in (mode=replace, and
    'next' pointing back at this page so a used code lands where they are)."""
    from app import oauth as oauth_mod
    with _tenant_client(monkeypatch, mailboxes=[{"address": "old@hotmail.com", "provider": "microsoft"}],
                        onboarded=True) as client:
        monkeypatch.setattr(oauth_mod.db, "get_tenant", lambda tid: {"name": "LittleDogs"})
        r = client.get("/oauth/invite/BLAM8-TEST-CODE")
        assert r.status_code == 200
        body = r.text
        assert 'name="code" value="BLAM8-TEST-CODE"' in body
        assert 'name="mode" value="replace"' in body
        assert 'name="next" value="/oauth/invite/BLAM8-TEST-CODE"' in body
        assert "old@hotmail.com" in body and "LittleDogs" in body


def test_invite_link_rejects_a_malformed_code(gated, tenant_client):
    r = tenant_client.get("/oauth/invite/not%20a%20code%21")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/settings"


def test_invite_link_used_code_bounces_back_to_the_link(gated, tenant_client, monkeypatch):
    monkeypatch.setattr(slots.db, "use_google_invite", lambda code: False)
    r = tenant_client.post("/oauth/google-invite", data={
        "code": "BLAM8-NOPE-NOPE", "mode": "replace", "next": "/oauth/invite/BLAM8-NOPE-NOPE"})
    assert r.status_code == 303
    assert r.headers["location"] == "/oauth/invite/BLAM8-NOPE-NOPE?gmail=badcode"


def test_invite_link_good_code_goes_straight_to_google(gated, tenant_client, monkeypatch):
    monkeypatch.setattr(slots.db, "use_google_invite", lambda code: code == "BLAM8-TEST-CODE")
    r = tenant_client.post("/oauth/google-invite", data={
        "code": "BLAM8-TEST-CODE", "mode": "replace", "next": "/oauth/invite/BLAM8-TEST-CODE"})
    assert r.status_code == 303
    assert r.headers["location"] == "/oauth/connect?provider=google&mode=replace"
    assert slots.INVITE_COOKIE in r.cookies


def test_gate_next_never_open_redirects(gated, tenant_client, monkeypatch):
    monkeypatch.setattr(slots.db, "use_google_invite", lambda code: False)
    r = tenant_client.post("/oauth/google-invite", data={
        "code": "x", "mode": "replace", "next": "https://evil.example/"})
    assert r.headers["location"] == "/app/settings?gmail=badcode"
    r = tenant_client.post("/oauth/google-invite", data={
        "code": "x", "mode": "replace", "next": "/oauth/invite/../../admin"})
    assert r.headers["location"] == "/app/settings?gmail=badcode"


def test_invite_link_skips_the_code_on_a_phone_that_already_passed_the_gate(gated, monkeypatch):
    """The signed cookie from an earlier code (or a verified app) means the page
    links straight to Google — no code consumed, and a burned code in the URL
    still works on that phone."""
    from app import oauth as oauth_mod
    with _tenant_client(monkeypatch, mailboxes=[{"address": "old@hotmail.com", "provider": "microsoft"}],
                        onboarded=True) as client:
        monkeypatch.setattr(oauth_mod.db, "get_tenant", lambda tid: {"name": "LittleDogs"})
        client.cookies.set(slots.INVITE_COOKIE, slots.invite_cookie_value())
        r = client.get("/oauth/invite/BLAM8-USED-CODE")
        assert r.status_code == 200
        assert 'href="/oauth/connect?provider=google&amp;mode=replace"' in r.text
        assert 'name="code"' not in r.text
