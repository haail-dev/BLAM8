"""The in-app Modules hub (/app/) renders the roadmap chips (WhatsApp, Telegram) and the
shared Invoicing module. Invoicing lights as Live once the tenant has a billable vertical.
Route-level via TestClient, db mocked (in-process, no network)."""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_roadmap_chips_and_invoicing_live_with_a_vertical(client, monkeypatch):
    # A billable vertical (FSM/TMS) lights invoicing as Live.
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    html = client.get("/app/modules").text
    assert "WhatsApp" in html and "Telegram" in html        # roadmap chips present
    assert "Invoicing" in html                               # the shared module card
    assert "/app/invoices" in html                           # active → Open link to it


def test_invoicing_active_for_office_tenant(client, monkeypatch):
    # Since the standalone extraction (2026-07-26): scheduling tenants get the live
    # card (office invoicing is on), never the greyed "coming soon".
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    html = client.get("/app/modules").text
    assert "Invoicing" in html
    assert "appointments &amp; stays — coming soon" not in html
    assert "/app/invoices" in html
    assert "WhatsApp" in html and "Telegram" in html


def test_invoicing_active_when_email_only(client, monkeypatch):
    # Email-only tenant: invoicing is standalone core — the LIVE card with an Open
    # link (the bundled/off note never renders because the card is never off).
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    html = client.get("/app/modules").text
    assert "Invoicing" in html and "/app/invoices" in html
    assert "Included with Field OS" not in html


# ── the CRM teaser (Julius 2026-07-25): locked card shows what it already knows ─

def test_locked_crm_card_teases_with_real_counts(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [{}] * 12)
    monkeypatch.setattr(dashboard.db, "crm_list_companies", lambda *a, **k: [{}] * 3)
    html = client.get("/app/modules").text
    assert "Already filling itself in from your inbox" in html
    assert "12 contacts" in html and "3 companies" in html
    assert "unlock on Pro" in html


def test_no_teaser_when_crm_is_active_or_book_is_empty(client, monkeypatch):
    # Active CRM → the Live card, never the teaser.
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})
    html = client.get("/app/modules").text
    assert "Already filling itself in from your inbox" not in html
    # Locked but nothing ingested yet → nothing to tease with.
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_companies", lambda *a, **k: [])
    html = client.get("/app/modules").text
    assert "Already filling itself in from your inbox" not in html
