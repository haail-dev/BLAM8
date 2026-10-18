"""Operator console login — the password grant restored after the SSO migration.

The tenant dashboard went SSO-only and `auth.login` was deleted, but /admin/login still
called it → AttributeError → 500. The fix re-adds `auth.password_login` (admin-only) and
points the route at it. This pins: success sets the admin cookie + redirects; a wrong
password is a clean 401 (NOT a 500); a valid non-admin is 403.

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

import app.admin as admin
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


SESSION = {"access_token": "at-1", "refresh_token": "rt-1", "user": {"id": "u-1"}}


def _patch(monkeypatch, session, is_admin):
    async def fake_login(email, password):
        if session is None:
            raise ValueError("Invalid email or password")
        return session
    monkeypatch.setattr(admin.auth, "password_login", fake_login)
    monkeypatch.setattr(admin.db, "is_platform_admin", lambda uid: is_admin)


def test_login_success_sets_admin_cookie_and_redirects(client, monkeypatch):
    _patch(monkeypatch, SESSION, is_admin={"email": "haail.dev@outlook.com", "role": "owner"})
    r = client.post("/admin/login", follow_redirects=False,
                    data={"email": "haail.dev@outlook.com", "password": "pw"})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert r.cookies.get("bl_admin_at") == "at-1"


def test_login_bad_credentials_is_401_not_500(client, monkeypatch):
    _patch(monkeypatch, None, is_admin=None)
    r = client.post("/admin/login", data={"email": "x@y.co", "password": "wrong"})
    assert r.status_code == 401
    assert "Invalid email or password" in r.text


def test_login_valid_but_not_a_platform_admin_is_403(client, monkeypatch):
    _patch(monkeypatch, SESSION, is_admin=None)
    r = client.post("/admin/login", data={"email": "x@y.co", "password": "pw"})
    assert r.status_code == 403
    assert "not a platform admin" in r.text
