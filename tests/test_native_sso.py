"""SSO for the iOS apps (docs/architecture/native-sign-in.md).

Pins: /auth/native/start's allow-lists and GoTrue redirect, the callback handing
a one-time code back to the app (and never starting a session itself), the
single-use, verifier-bound code, and the app-side completion behaving exactly
like the web callback (tenant, invite, partner, switched-off).

Run: python tests/run_offline.py tests/test_native_sso.py -q
"""
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import auth, auth_routes, native_auth, people_routes
from app.config import settings
from app.main import app

VERIFIER = "v" * 43
CHALLENGE = native_auth.challenge_for(VERIFIER)
SESSION = {"access_token": "AT", "refresh_token": "RT",
           "user": {"id": "u1", "email": "ops@acme.com"}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    native_auth._pending.clear()
    yield TestClient(app, follow_redirects=False, headers={'X-Blam8-Native-Auth':'1'})   # no `with`: skip startup events
    native_auth._pending.clear()


def _set_cookies(r) -> str:
    return " ".join(r.headers.get_list("set-cookie"))


def _native_cookie(scheme="blam8", challenge=CHALLENGE, next_path="/app") -> str:
    return base64.urlsafe_b64encode(json.dumps(
        {"s": scheme, "c": challenge, "n": next_path}).encode()).decode()


def _callback_params(location: str) -> tuple[str, dict]:
    parts = urlsplit(location)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qs(parts.query)


def _fake_exchange(monkeypatch, session=SESSION):
    async def fake(code, verifier):
        assert code == "the-code" and verifier == "v123"
        return session
    monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", fake)


# ── /auth/native/start ───────────────────────────────────────────────────────
def test_start_rejects_an_unknown_app_outright(client):
    r = client.get(f"/auth/native/start?provider=google&scheme=evil&next=/app&challenge={CHALLENGE}")
    assert r.status_code == 400
    assert "bl_pkce=" not in _set_cookies(r)


@pytest.mark.parametrize("query", [
    f"provider=github&scheme=blam8&next=/app&challenge={CHALLENGE}",          # provider
    f"provider=google&scheme=blam8&next=https://evil.example&challenge={CHALLENGE}",  # landing
    f"provider=google&scheme=blam8&next=/admin&challenge={CHALLENGE}",       # landing
    "provider=google&scheme=blam8&next=/app&challenge=short",                # challenge
])
def test_start_sends_an_invalid_request_back_to_the_app(client, query):
    r = client.get(f"/auth/native/start?{query}")
    assert r.status_code == 303
    target, params = _callback_params(r.headers["location"])
    assert target == "blam8://auth/complete" and params["error"]
    assert "bl_pkce=" not in _set_cookies(r)


@pytest.mark.parametrize("scheme,next_path", [("blam8", "/app"),
                                              ("blam8-concierge", "/concierge/live")])
def test_start_redirects_to_gotrue_like_the_web_and_marks_the_sheet(client, scheme, next_path):
    r = client.get(f"/auth/native/start?provider=google&scheme={scheme}"
                   f"&next={next_path}&challenge={CHALLENGE}")
    assert r.status_code == 303
    location = r.headers["location"]
    assert "/authorize?" in location and "provider=google" in location
    # Same redirect_to as the web flow: no Supabase allow-list change.
    callback = f"{settings.app_base_url}/auth/callback"
    assert parse_qs(urlsplit(location).query)["redirect_to"] == [callback]
    cookies = _set_cookies(r)
    assert "bl_pkce=" in cookies and "bl_native=" in cookies
    marker = r.cookies.get(native_auth.NATIVE_COOKIE)
    assert native_auth.parse_pending(marker) == native_auth.Pending(scheme, CHALLENGE, next_path)


# ── /auth/callback, native branch ────────────────────────────────────────────
def test_native_callback_hands_back_a_code_and_starts_no_session(client, monkeypatch):
    _fake_exchange(monkeypatch)
    monkeypatch.setattr(auth_routes, "_establish_tenant",
                        lambda *a, **k: pytest.fail("tenant work belongs to the app's web view"))
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(native_auth.NATIVE_COOKIE, _native_cookie(next_path="/app"))
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303
    target, params = _callback_params(r.headers["location"])
    assert target == "blam8://auth/complete" and params["code"][0]
    cookies = _set_cookies(r)
    assert "bl_at=" not in cookies and "bl_rt=" not in cookies
    assert 'bl_pkce="";' in cookies or "bl_pkce=;" in cookies       # cleared
    assert 'bl_native="";' in cookies or "bl_native=;" in cookies   # cleared
    # The code redeems once, with the app's verifier, for this session.
    assert native_auth.redeem(params["code"][0], VERIFIER) == (SESSION, "/app")


@pytest.mark.parametrize("query,exchange_fails", [
    ("error=access_denied", False),   # provider said no
    ("", False),                      # no code
    ("code=the-code", True),          # GoTrue refused the exchange
])
def test_native_callback_failures_return_an_error_to_the_app(client, monkeypatch, query, exchange_fails):
    async def boom(code, verifier):
        raise ValueError("bad code")
    if exchange_fails:
        monkeypatch.setattr(auth_routes.auth, "exchange_code_for_session", boom)
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(native_auth.NATIVE_COOKIE, _native_cookie(scheme="blam8-concierge",
                                                                 next_path="/concierge/live"))
    r = client.get(f"/auth/callback?{query}")
    assert r.status_code == 303
    target, params = _callback_params(r.headers["location"])
    assert target == "blam8-concierge://auth/complete"
    assert params["error"] and "code" not in params
    assert not native_auth._pending


def test_a_tampered_native_marker_is_ignored_and_dropped(client, monkeypatch):
    _fake_exchange(monkeypatch)
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t1")
    client.cookies.set(auth.PKCE_COOKIE, "v123")
    client.cookies.set(native_auth.NATIVE_COOKIE, _native_cookie(scheme="evil"))
    r = client.get("/auth/callback?code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/app"   # ordinary web sign-in
    cookies = _set_cookies(r)
    assert "bl_at=" in cookies
    assert 'bl_native="";' in cookies or "bl_native=;" in cookies


# ── /auth/native/complete ────────────────────────────────────────────────────
@pytest.mark.parametrize("landing", ["/app", "/concierge/live"])
def test_complete_signs_in_like_the_web_callback(client, monkeypatch, landing):
    seen = {}
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda uid, name, email=None: seen.update(uid=uid, name=name) or "t1")
    code = native_auth.issue(SESSION, CHALLENGE, landing)
    r = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert r.status_code == 303 and r.headers["location"] == landing
    cookies = _set_cookies(r)
    assert "bl_at=" in cookies and "bl_rt=" in cookies
    assert r.headers["cache-control"] == "no-store"
    assert seen == {"uid": "u1", "name": "Acme"}


def test_complete_with_the_wrong_verifier_fails_and_burns_the_code(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t1")
    code = native_auth.issue(SESSION, CHALLENGE, "/app")
    r = client.post("/auth/native/complete", data={"code": code, "verifier": "w" * 43})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=")
    assert "bl_at=" not in _set_cookies(r)
    r = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert r.headers["location"].startswith("/login?error=")       # no second guess


def test_complete_refuses_reused_unknown_and_expired_codes(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: "t1")
    code = native_auth.issue(SESSION, CHALLENGE, "/app")
    first = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert first.headers["location"] == "/app"
    again = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert again.headers["location"].startswith("/login?error=")
    unknown = client.post("/auth/native/complete", data={"code": "nope", "verifier": VERIFIER})
    assert unknown.headers["location"].startswith("/login?error=")
    stale = native_auth.issue(SESSION, CHALLENGE, "/app", now=0.0)
    assert native_auth.redeem(stale, VERIFIER, now=float(native_auth.CODE_TTL_S)) is None


def test_complete_binds_an_invited_user_through_the_apps_own_cookie(client, monkeypatch):
    from app import people
    accepted = {}
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "get_member_invite",
                        lambda token: {"tenant_id": "t-inviter"} if token == "join-tok" else None)
    monkeypatch.setattr(people, "usable", lambda row: True)
    monkeypatch.setattr(auth_routes.db, "accept_member_invite",
                        lambda row, uid, email, when: accepted.update(tenant=row["tenant_id"], uid=uid))
    monkeypatch.setattr(auth_routes.db, "ensure_tenant_for_user",
                        lambda *a, **k: pytest.fail("an invited user joins, not creates"))
    client.cookies.set(people_routes.JOIN_COOKIE, "join-tok")   # set earlier in the app's web view
    code = native_auth.issue(SESSION, CHALLENGE, "/app")
    r = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert r.headers["location"] == "/app"
    assert accepted == {"tenant": "t-inviter", "uid": "u1"}


def test_complete_keeps_partner_and_switched_off_outcomes(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda e: {"id": "p1"})
    code = native_auth.issue(SESSION, CHALLENGE, "/concierge/live")
    r = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert r.headers["location"] == "/partner"

    def switched_off(*a, **k):
        raise auth_routes.db.MembershipSwitchedOff()
    monkeypatch.setattr(auth_routes.db, "get_partner_by_email", lambda e: None)
    monkeypatch.setattr(auth_routes, "_establish_tenant", switched_off)
    code = native_auth.issue(SESSION, CHALLENGE, "/app")
    r = client.post("/auth/native/complete", data={"code": code, "verifier": VERIFIER})
    assert r.headers["location"].startswith("/login?error=Your%20access")


# ── the code store ───────────────────────────────────────────────────────────
def test_the_store_keeps_only_hashes_and_purges_expired_codes(client):
    old = native_auth.issue(SESSION, CHALLENGE, "/app", now=0.0)
    assert old not in native_auth._pending and len(native_auth._pending) == 1
    native_auth.issue(SESSION, CHALLENGE, "/app", now=float(native_auth.CODE_TTL_S) + 1)
    assert len(native_auth._pending) == 1                      # the expired one went
    assert native_auth.redeem(old, VERIFIER) is None


def test_complete_rejects_foreign_origin_even_with_attackers_valid_code(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db, 'get_tenant_for_user', lambda uid: 't1')
    code=native_auth.issue(SESSION,CHALLENGE,'/app')
    response=client.post('/auth/native/complete',data={'code':code,'verifier':VERIFIER},headers={'Origin':'https://untrusted.example','Sec-Fetch-Site':'cross-site'})
    assert response.status_code==403
    assert 'bl_at=' not in _set_cookies(response)
    assert native_auth.redeem(code,VERIFIER) is not None


@pytest.mark.parametrize('headers', [{}, {'Origin':'null'}, {'Origin':'https://untrusted.example'}])
def test_native_completion_requires_non_simple_header_before_consuming_code(client, headers):
    client.headers.pop('X-Blam8-Native-Auth', None)
    code=native_auth.issue(SESSION,CHALLENGE,'/app')
    response=client.post('/auth/native/complete',data={'code':code,'verifier':VERIFIER},headers=headers)
    assert response.status_code==403 and response.headers['cache-control']=='no-store'
    assert 'bl_at=' not in _set_cookies(response)
    assert native_auth.redeem(code,VERIFIER) is not None


def test_native_completion_does_not_allow_cross_origin_header_preflight(client):
    response=client.options('/auth/native/complete',headers={'Origin':'https://untrusted.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'X-Blam8-Native-Auth'})
    assert 'access-control-allow-origin' not in response.headers


def test_native_completion_allows_same_origin_app_request(client, monkeypatch):
    monkeypatch.setattr(auth_routes.db,'get_tenant_for_user',lambda uid:'t1')
    code=native_auth.issue(SESSION,CHALLENGE,'/app')
    response=client.post('/auth/native/complete',data={'code':code,'verifier':VERIFIER},headers={'Origin':settings.app_base_url.rstrip('/'),'Sec-Fetch-Site':'same-origin'})
    assert response.status_code==303 and response.headers['location']=='/app'
    assert 'bl_at=' in _set_cookies(response)
