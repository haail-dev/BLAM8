"""An expired access token skips the doomed GoTrue /user call; the refresh response's user is used;
sign-in seeds the identity cache. Everything that is not a parseable expired JWT behaves as before."""
import base64
import json
import time

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app import actor, auth, db, journey, navcache


def jwt(exp):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'HS256'})}.{part({'exp': exp, 'sub': 'u1'})}.sig"


@pytest.fixture
def harness(monkeypatch):
    calls = {"get_user": [], "refresh": []}
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t1")
    monkeypatch.setattr(db, "db_user", lambda token: object())
    monkeypatch.setattr(journey, "touch", lambda *a: None)
    monkeypatch.setattr(auth, "_viewing_as", lambda *a: None)

    async def get_user(token):
        calls["get_user"].append(token)
        return {"id": "u1", "email": "Owner@Example.invalid"}

    async def refresh(rt):
        calls["refresh"].append(rt)
        return calls.get("session")
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(auth, "refresh_session", refresh)
    yield calls
    navcache._store.clear()
    actor.forget()


def request(at, rt="rt1"):
    cookies = f"bl_at={at}; bl_rt={rt}" if rt else f"bl_at={at}"
    return Request({"type": "http", "method": "GET", "path": "/app/",
                    "headers": [(b"cookie", cookies.encode())]})


@pytest.mark.asyncio
async def test_expired_jwt_skips_the_doomed_user_call_and_uses_the_refresh_user(harness):
    new = jwt(int(time.time()) + 3600)
    harness["session"] = {"access_token": new, "refresh_token": "rt2", "user": {"id": "u1", "email": "Owner@Example.invalid"}}
    ctx = await auth.require_tenant(request(jwt(int(time.time()) - 60)), Response())
    assert ctx.user_id == "u1" and ctx.tenant_id == "t1"
    assert harness["get_user"] == []             # no /user for the expired token, none for the fresh one
    assert harness["refresh"] == ["rt1"]
    # the refreshed identity is cached: the next request is free
    await auth.require_tenant(request(new), Response())
    assert harness["get_user"] == []


@pytest.mark.asyncio
async def test_refresh_response_without_a_user_falls_back_to_get_user(harness):
    new = jwt(int(time.time()) + 3600)
    harness["session"] = {"access_token": new, "refresh_token": "rt2"}
    ctx = await auth.require_tenant(request(jwt(int(time.time()) - 60)), Response())
    assert ctx.user_id == "u1"
    assert harness["get_user"] == [new]


@pytest.mark.asyncio
async def test_a_token_that_is_not_an_expired_jwt_still_asks_get_user(harness):
    for token in ("AT", "fixture", jwt(int(time.time()) + 600), "a.b.c"):
        harness["get_user"].clear()
        navcache._store.clear()
        await auth.require_tenant(request(token), Response())
        assert harness["get_user"] == [token], token


@pytest.mark.asyncio
async def test_an_expired_token_with_no_refresh_cookie_redirects_without_asking_get_user(harness):
    with pytest.raises(auth.AuthRedirect):
        await auth.require_tenant(request(jwt(int(time.time()) - 60), rt=None), Response())
    assert harness["get_user"] == []


@pytest.mark.asyncio
async def test_sign_in_seeds_the_identity_cache(harness):
    tok = jwt(int(time.time()) + 3600)
    auth.seed_identity({"access_token": tok, "user": {"id": "u1", "email": "o@example.invalid"}})
    await auth.require_tenant(request(tok), Response())
    assert harness["get_user"] == []


def test_seed_identity_ignores_unusable_sessions(harness):
    auth.seed_identity({"access_token": jwt(int(time.time()) + 3600)})            # no user
    auth.seed_identity({"access_token": jwt(int(time.time()) - 5), "user": {"id": "u1"}})   # no life left
    auth.seed_identity({"user": {"id": "u1"}})                                    # no token
    assert not navcache._store
