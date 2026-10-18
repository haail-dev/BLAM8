"""Identity stale-while-revalidate: a GET page view just past the 60 s identity cache is served on the
identity validated moments ago while GoTrue is asked again in the background."""
import asyncio
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
def h(monkeypatch):
    state = {"get_user": [], "checks": [], "check_result": (200, {"id": "u1", "email": "o@example.invalid"})}
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    navcache._inflight.clear()
    auth._revalidating.clear()
    monkeypatch.setattr(auth.settings, "identity_swr_seconds", 120)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t1")
    monkeypatch.setattr(db, "db_user", lambda token: object())
    monkeypatch.setattr(journey, "touch", lambda *a: None)
    monkeypatch.setattr(auth, "_viewing_as", lambda *a: None)

    async def get_user(token):
        state["get_user"].append(token)
        return {"id": "u1", "email": "o@example.invalid"}

    async def check_user(token):
        state["checks"].append(token)
        return state["check_result"]
    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(auth, "_check_user", check_user)
    yield state
    navcache._store.clear()
    navcache._inflight.clear()
    auth._revalidating.clear()
    actor.forget()


def req(token, method="GET"):
    return Request({"type": "http", "method": method, "path": "/app/",
                    "headers": [(b"cookie", f"bl_at={token}; bl_rt=rt".encode())]})


async def settle():
    for _ in range(20):
        if not auth._bg_tasks:
            return
        await asyncio.sleep(0.01)
    await asyncio.gather(*list(auth._bg_tasks))


def lapse_fresh_cache(token):
    navcache.invalidate("u:" + auth._token_key(token))      # the 60 s key expired; the stale copy remains


@pytest.mark.asyncio
async def test_a_get_after_the_fresh_cache_lapses_is_served_stale_and_revalidated(h):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())            # cold: validates live
    assert h["get_user"] == [tok]
    lapse_fresh_cache(tok)
    ctx = await auth.require_tenant(req(tok), Response())
    assert ctx.user_id == "u1"
    assert h["get_user"] == [tok]                              # no live call on this request
    await settle()
    assert h["checks"] == [tok]                                # asked again in the background
    assert navcache.peek("u:" + auth._token_key(tok))[0]       # and the fresh cache is renewed
    await auth.require_tenant(req(tok), Response())
    assert h["checks"] == [tok]                                # next request: plain cache hit


@pytest.mark.asyncio
async def test_writes_never_use_the_stale_identity(h):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    lapse_fresh_cache(tok)
    h["get_user"].clear()
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        lapse_fresh_cache(tok)
        await auth.require_tenant(req(tok, method), Response())
    assert len(h["get_user"]) == 4
    assert h["checks"] == []


@pytest.mark.asyncio
async def test_a_rejected_session_is_dropped_at_once_and_the_next_request_validates_live(h):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    lapse_fresh_cache(tok)
    h["check_result"] = (403, None)                            # revoked elsewhere
    await auth.require_tenant(req(tok), Response())            # the one stale-served view
    await settle()
    assert not navcache.peek("us:" + auth._token_key(tok))[0]
    assert not navcache.peek("u:" + auth._token_key(tok))[0]
    h["get_user"].clear()
    await auth.require_tenant(req(tok), Response())
    assert h["get_user"] == [tok]                              # live again: GoTrue decides


@pytest.mark.asyncio
async def test_a_brown_out_does_not_drop_the_session(h):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    lapse_fresh_cache(tok)
    h["check_result"] = (503, None)
    await auth.require_tenant(req(tok), Response())
    await settle()
    assert navcache.peek("us:" + auth._token_key(tok))[0]      # stale copy kept until it runs out


@pytest.mark.asyncio
async def test_past_the_window_or_with_the_feature_off_it_validates_live(h, monkeypatch):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    lapse_fresh_cache(tok)
    navcache.invalidate("us:" + auth._token_key(tok))          # past the stale window
    h["get_user"].clear()
    await auth.require_tenant(req(tok), Response())
    assert h["get_user"] == [tok]
    lapse_fresh_cache(tok)
    monkeypatch.setattr(auth.settings, "identity_swr_seconds", 0)
    h["get_user"].clear()
    await auth.require_tenant(req(tok), Response())
    assert h["get_user"] == [tok] and h["checks"] == []


@pytest.mark.asyncio
async def test_concurrent_stale_requests_start_one_revalidation(h):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    lapse_fresh_cache(tok)
    await asyncio.gather(*[auth.require_tenant(req(tok), Response()) for _ in range(6)])
    await settle()
    assert len(h["checks"]) == 1


@pytest.mark.asyncio
async def test_sign_out_drops_the_stale_copy_too(h, monkeypatch):
    tok = jwt(int(time.time()) + 3600)
    await auth.require_tenant(req(tok), Response())
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: None)
    auth.revoke_session(tok)
    assert not navcache.peek("us:" + auth._token_key(tok))[0]


def test_the_stale_copy_never_outlives_the_token(h):
    short = jwt(int(time.time()) + 45)
    auth._remember_stale(short, {"id": "u1"})
    exp, _ = navcache._store["us:" + auth._token_key(short)]
    assert exp - time.monotonic() <= 16                         # token life minus the 30 s guard
    expired = jwt(int(time.time()) + 10)
    auth._remember_stale(expired, {"id": "u1"})
    assert "us:" + auth._token_key(expired) not in navcache._store
