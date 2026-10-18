"""The inbox 'Booking details' pill — the booking-requirements entry point.

Phase 1 (86bb6c9) added this pill to the legacy inbox.html, but the live inbox
is conversations.html — so it never showed (caught by Kris 2026-07-02).
2026-07-04 (Kris): booking requirements are an EMAIL-side concern for every
tier — email-only tenants gather-and-confirm, diary tenants hard-require the
fields before a hold — so the pill shows for ALL tenants and the Diary page no
longer carries a 'Booking requirements' link.

Run: python -m pytest tests/test_inbox_booking_pill.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, modules):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)


def test_email_only_tenant_sees_the_booking_details_pill(client, monkeypatch):
    _env(monkeypatch, modules=set())
    html = client.get("/app/conversations").text
    assert "Booking details" in html
    assert "/app/diary/intake" in html


@pytest.mark.parametrize("modules", [{"scheduling"}, {"tms"}, {"fsm"}, {"scheduling", "tms"}])
def test_pill_shows_for_diary_tenants_too(client, monkeypatch, modules):
    # the inbox pill is now the ONLY way a diary tenant edits booking
    # requirements — the Diary page dropped its link (email-side concern)
    _env(monkeypatch, modules=modules)
    html = client.get("/app/conversations").text
    assert "/app/diary/intake" in html


# ── the "finish scheduling setup" banner (2026-07-27, Julius) ─────────────────
# Nobody tells a new Starter tenant that picking the plan isn't enough — the AI
# can't book until the intake checklist is saved. The inbox banner is that
# announcement, persistent until configured.
def test_scheduling_unconfigured_shows_finish_setup_banner(client, monkeypatch):
    _env(monkeypatch, modules={"scheduling"})          # cfg = {} → not configured
    html = client.get("/app/conversations").text
    assert "Finish scheduling setup" in html
    assert "One step left before the AI can book" in html


def test_banner_gone_once_intake_configured(client, monkeypatch):
    _env(monkeypatch, modules={"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config",
                        lambda *a, **k: {"intake_configured_at": "2026-01-01T00:00:00+00:00"})
    assert "Finish scheduling setup" not in client.get("/app/conversations").text


def test_no_banner_without_the_scheduling_module(client, monkeypatch):
    # email-only: nothing is blocked, so no nag — the pill alone is the entry point
    _env(monkeypatch, modules=set())
    assert "Finish scheduling setup" not in client.get("/app/conversations").text
