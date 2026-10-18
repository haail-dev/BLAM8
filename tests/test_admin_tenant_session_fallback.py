"""require_admin: the tenant-session fallback (the PWA path).

The installed app signs in with the TENANT cookies (bl_at) — it has no admin
cookies until someone completes /admin/login's password form, which an SSO-only
operator account can't. The fallback lets a tenant session through require_admin
IFF its user is in platform_admins — same authentication, same authorisation
wall, different cookie name. These pin:

  * tenant cookie + platform_admins member → AdminCtx (no admin cookies minted:
    the tenant refresh token is single-use and belongs to /app);
  * tenant cookie + NON-member → redirect (the wall holds for every other account);
  * no cookies at all → redirect (unchanged);
  * a valid admin cookie still wins as before.

Run: python -m pytest tests/test_admin_tenant_session_fallback.py -q
"""
from types import SimpleNamespace

import pytest

from app import admin_auth, auth


def _request(cookies: dict) -> SimpleNamespace:
    return SimpleNamespace(cookies=cookies, state=SimpleNamespace())


class _Response:
    def __init__(self):
        self.cookies_set = []

    def set_cookie(self, key, value, **kw):
        self.cookies_set.append(key)


def _patch_users(monkeypatch, tokens: dict, admins: set):
    """tokens: access_token -> user id; admins: user ids in platform_admins."""
    async def fake_get_user(access):
        uid = tokens.get(access)
        return {"id": uid, "email": f"{uid}@x.co"} if uid else None

    async def fake_refresh(rt):
        return None

    monkeypatch.setattr(admin_auth.auth, "get_user", fake_get_user)
    monkeypatch.setattr(admin_auth.auth, "refresh_session", fake_refresh)
    monkeypatch.setattr(admin_auth.db, "is_platform_admin",
                        lambda uid: {"email": f"{uid}@x.co", "role": "owner"} if uid in admins else None)


@pytest.mark.asyncio
async def test_tenant_session_of_platform_admin_passes_without_minting_admin_cookies(monkeypatch):
    _patch_users(monkeypatch, tokens={"tenant-at": "u-admin"}, admins={"u-admin"})
    resp = _Response()
    ctx = await admin_auth.require_admin(_request({auth.AT_COOKIE: "tenant-at"}), resp)
    assert ctx.user_id == "u-admin" and ctx.role == "owner"
    assert resp.cookies_set == []   # tenant tokens must never become admin cookies


@pytest.mark.asyncio
async def test_tenant_session_of_ordinary_tenant_is_redirected(monkeypatch):
    _patch_users(monkeypatch, tokens={"tenant-at": "u-tenant"}, admins=set())
    with pytest.raises(admin_auth.AdminAuthRedirect):
        await admin_auth.require_admin(_request({auth.AT_COOKIE: "tenant-at"}), _Response())


@pytest.mark.asyncio
async def test_no_cookies_still_redirects(monkeypatch):
    _patch_users(monkeypatch, tokens={}, admins={"u-admin"})
    with pytest.raises(admin_auth.AdminAuthRedirect):
        await admin_auth.require_admin(_request({}), _Response())


@pytest.mark.asyncio
async def test_admin_cookie_path_unchanged(monkeypatch):
    _patch_users(monkeypatch, tokens={"admin-at": "u-admin"}, admins={"u-admin"})
    ctx = await admin_auth.require_admin(
        _request({admin_auth.ADMIN_AT_COOKIE: "admin-at"}), _Response())
    assert ctx.user_id == "u-admin"
