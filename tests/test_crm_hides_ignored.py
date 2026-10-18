"""Ignored senders are not contacts (Kris 2026-09-01).

Telling the agent to ignore someone is the operator saying "this is not a
customer". The CRM did not hear it: on the BLAM8 tenant, 7 of 11 contacts were
addresses already on the ignore list — Cloudflare, Perplexity, smtp2go, a
mailing-list robot. A CRM whose list is more than half noise is not one anybody
opens.

The filter is applied at READ time against ignored_contacts rather than stamped
onto crm_contacts, and the two tests that matter are about what that buys:
nothing can drift out of sync, and un-ignoring restores the contact and its
whole history without a migration or a backfill.

Run: python -m pytest tests/test_crm_hides_ignored.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app

CONTACTS = [
    {"id": "1", "email": "sarah@realcustomer.co.uk", "name": "Sarah", "last_activity_at": None},
    {"id": "2", "email": "em@em1.cloudflare.com", "name": "Cloudflare", "last_activity_at": None},
    {"id": "3", "email": "team@mail.perplexity.ai", "name": "Perplexity", "last_activity_at": None},
]


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, ignored=(), contacts=CONTACTS):
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [dict(c) for c in contacts])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set(ignored))
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "module_ui_visible", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: True)


def test_an_ignored_sender_is_not_listed_as_a_contact(client, monkeypatch):
    _env(monkeypatch, ignored={"em@em1.cloudflare.com", "team@mail.perplexity.ai"})
    html = client.get("/app/crm").text
    assert "sarah@realcustomer.co.uk" in html
    assert "cloudflare" not in html.lower()
    assert "perplexity" not in html.lower()


def test_the_list_says_what_it_left_out(client, monkeypatch):
    """A list that quietly shrinks is unnerving; say why, and link to where they
    went."""
    _env(monkeypatch, ignored={"em@em1.cloudflare.com", "team@mail.perplexity.ai"})
    html = client.get("/app/crm").text
    assert "2" in html and "ignored sender" in html
    assert 'href="/app/ignored"' in html


def test_nothing_is_said_when_nothing_is_hidden(client, monkeypatch):
    _env(monkeypatch, ignored=set())
    assert "kept out of this list" not in client.get("/app/crm").text


def test_matching_is_case_insensitive(client, monkeypatch):
    """Addresses are lowercased on the way into crm_contacts but the ignore list
    takes what it is given."""
    _env(monkeypatch, ignored={"EM@EM1.Cloudflare.COM"})
    assert "cloudflare" not in client.get("/app/crm").text.lower()


def test_un_ignoring_restores_the_contact(client, monkeypatch):
    """The whole reason this filters at read time instead of stamping a column:
    taking someone off the ignore list brings them and their history straight
    back, with nothing to migrate and nothing that can drift."""
    _env(monkeypatch, ignored={"em@em1.cloudflare.com"})
    assert "cloudflare" not in client.get("/app/crm").text.lower()
    _env(monkeypatch, ignored=set())
    assert "cloudflare" in client.get("/app/crm").text.lower()


def test_an_unreadable_ignore_list_shows_every_contact(client, monkeypatch):
    """Best-effort like every other CRM read. Failing open shows a little noise;
    failing closed would show an empty CRM and look like data loss."""
    _env(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(dashboard.db, "ignored_addresses", boom)
    html = client.get("/app/crm").text
    assert "sarah@realcustomer.co.uk" in html
    assert "cloudflare" in html.lower()
