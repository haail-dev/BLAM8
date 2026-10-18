"""Gmail-connect slot indicator (src/app/slots.py) + the count_gmail_connections wrapper.

Pins: the verified short-circuit (no DB hit, indicator hidden), the
remaining/low/full math, the ~60s in-process cache (one DB read, not one per
visit), that the count wrapper tolerates a missing/exploding count, and that
the login page no longer carries the gate (sign-in is open — the cap meters
Gmail mailbox CONNECTS, guarded in /oauth/connect).

Run: python -m pytest tests/test_slots.py -q
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import db, slots
from app.config import settings


@pytest.fixture(autouse=True)
def _fresh_cache():
    slots._cache.update(at=-1e9, used=0)   # force a miss at the start of every test
    yield


def test_verified_hides_indicator_and_skips_db(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", True)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: (_ for _ in ()).throw(AssertionError("DB hit")))
    s = slots.google_slots()
    assert s == {"used": 0, "cap": 100, "remaining": 100,
                 "verified": True, "low": False, "full": False}


def test_unverified_reports_remaining(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 30)
    s = slots.google_slots()
    assert s["used"] == 30 and s["remaining"] == 70
    assert s["verified"] is False and s["low"] is False and s["full"] is False


def test_low_flag_at_20_remaining(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 80)   # 20 remaining
    s = slots.google_slots()
    assert s["remaining"] == 20 and s["low"] is True and s["full"] is False


def test_full_flag_and_clamp_at_cap(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 130)  # over cap
    s = slots.google_slots()
    assert s["remaining"] == 0 and s["full"] is True and s["low"] is True


def test_count_is_cached_for_60s(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return 5
    monkeypatch.setattr(db, "count_gmail_connections", counting)
    slots.google_slots()
    slots.google_slots()
    slots.google_slots()
    assert calls["n"] == 1            # one DB read, then served from cache


# ── db.count_gmail_connections wrapper ───────────────────────────────────────
def _fake_table(count):
    fake = MagicMock()
    (fake.table.return_value.select.return_value.eq.return_value
     .limit.return_value.execute.return_value) = SimpleNamespace(count=count)
    return fake


def test_count_wrapper_handles_value_none_and_failure(monkeypatch):
    monkeypatch.setattr(db, "db", lambda: _fake_table(12))
    assert db.count_gmail_connections() == 12
    monkeypatch.setattr(db, "db", lambda: _fake_table(None))   # PostgREST no-count shape
    assert db.count_gmail_connections() == 0

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "db", boom)
    assert db.count_gmail_connections() == 0     # never breaks a page


def test_count_reads_google_connections(monkeypatch):
    fake = _fake_table(3)
    monkeypatch.setattr(db, "db", lambda: fake)
    assert db.count_gmail_connections() == 3
    fake.table.assert_called_once_with("email_connections")
    fake.table.return_value.select.return_value.eq.assert_called_once_with("provider", "google")


# ── route + login-page rendering ─────────────────────────────────────────────
def test_endpoint_reports_gmail_connect_availability(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 40)
    client = TestClient(app, follow_redirects=False)   # no `with`: skip startup events

    r = client.get("/api/auth/google-slots")
    assert r.status_code == 200
    assert r.json() == {"used": 40, "cap": 100, "remaining": 60,
                        "verified": False, "low": False, "full": False}


def test_login_page_is_ungated_even_when_slots_full(monkeypatch):
    # The gate moved to the mailbox-connect step: Google SIGN-IN stays live on the
    # login page regardless of Gmail-connect slot state (basic scopes, no cap).
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "google_oauth_verified", False)
    monkeypatch.setattr(settings, "google_invite_gate", True)
    monkeypatch.setattr(db, "count_gmail_connections", lambda: 100)   # full
    client = TestClient(app, follow_redirects=False)

    r = client.get("/login")
    assert r.status_code == 200
    assert 'href="/auth/sso/google"' in r.text        # Google sign-in always live
    assert "temporarily full" not in r.text
    assert "Invite code" not in r.text                # no code box at sign-in
    # the heads-up about the CONNECT gate is present while the gate is armed
    assert "connecting a Gmail inbox is invite-only" in r.text
