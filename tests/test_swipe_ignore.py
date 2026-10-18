"""Swipe-to-ignore confirmation toast (Kris 2026-08-31).

The swipe POSTs one contact to the (still list-shaped) ignore-remove endpoint;
the redirect carries only a clamped COUNT, and the list page renders the toast
saying where the conversation went (Ignored senders) with the undo link. The
address itself never rides the URL — fixed-shape params only.

Run: python -m pytest tests/test_swipe_ignore.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "email_log_month_stats", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)


def test_ignore_redirect_carries_the_count_not_the_address(client, monkeypatch):
    ignored = []
    monkeypatch.setattr(dashboard.db, "ignore_contact",
                        lambda tid, addr, *a, **k: ignored.append(addr))
    monkeypatch.setattr(dashboard.db, "skip_pending_for_contact", lambda *a, **k: None)
    r = client.post("/app/conversations/ignore-remove", follow_redirects=False,
                    data={"contacts": "spam@x.co"})
    assert r.status_code == 303
    assert r.headers["location"] == "/app/conversations?ignored=1"
    assert ignored == ["spam@x.co"]
    assert "spam@x.co" not in r.headers["location"]


def test_empty_submit_redirects_without_a_toast(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "ignore_contact", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "skip_pending_for_contact", lambda *a, **k: None)
    r = client.post("/app/conversations/ignore-remove", follow_redirects=False, data={})
    assert r.headers["location"] == "/app/conversations"


def test_toast_names_the_destination_and_the_way_back(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations?ignored=1").text
    assert 'id="ignore-toast"' in html
    assert "moved to Ignored senders" in html
    assert 'href="/app/ignored"' in html               # the undo surface
    html2 = client.get("/app/conversations?ignored=3").text
    assert "3 conversations have moved" in html2


def test_toast_param_is_clamped_and_garbage_safe(client, monkeypatch):
    _env(monkeypatch)
    assert 'id="ignore-toast"' not in client.get("/app/conversations").text
    assert 'id="ignore-toast"' not in client.get("/app/conversations?ignored=0").text
    assert 'id="ignore-toast"' not in client.get("/app/conversations?ignored=junk").text
    assert "99 conversations" in client.get("/app/conversations?ignored=4000").text
