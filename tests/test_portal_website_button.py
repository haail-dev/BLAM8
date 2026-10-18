"""Invite-only portal (Kris 2026-09-24): the ?intent=book deep link and the
settings no longer advertise the portal to strangers.

?intent=book still STAGES the booking ask for a customer who arrives verified
(their ?ref= link), never auto-sends it — every portal turn is an LLM call. A
visitor without a code gets the invite gate, not a staged ask. Settings no
longer offer "Put it on your website" snippets: a public link only leads
strangers to a door they can't open.

Run: python -m pytest tests/test_portal_website_button.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.portal as portal
from app.auth import TenantCtx, require_onboarded
from app.main import app

BOOK_ASK = "I&#39;d like to book a service"   # as Jinja autoescapes it into the textarea
CONTACT = {"id": "c1", "name": "Sam Lee", "email": "sam@x.com",
           "reference_code": "ACME-7K3M"}


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _page_env(monkeypatch, modules={"scheduling"}):
    tenant = {"id": "t1", "name": "Acme Plumbing", "portal_slug": "acme-plumbing"}
    monkeypatch.setattr(portal.db, "get_tenant_by_portal_slug",
                        lambda slug: dict(tenant) if slug == "acme-plumbing" else None)
    monkeypatch.setattr(portal.db, "get_tenant_config",
                        lambda *a, **k: {"portal_enabled": True})
    monkeypatch.setattr(portal.db, "visible_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(portal.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, ref: dict(CONTACT) if ref == "ACME-7K3M" else None)
    monkeypatch.setattr(portal.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(portal.db, "job_party_memberships", lambda *a, **k: [])


# ── /c/{slug}?intent=book ────────────────────────────────────────────────────
def test_intent_book_stages_the_ask_for_a_verified_customer(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.get("/c/acme-plumbing?ref=ACME-7K3M&intent=book")
    assert res.status_code == 200
    assert BOOK_ASK in res.text


def test_intent_book_stages_nothing_for_a_stranger(client, monkeypatch):
    # Invite-only: a visitor with no code meets the gate, not a staged ask.
    _page_env(monkeypatch)
    res = client.get("/c/acme-plumbing?intent=book")
    assert res.status_code == 200
    assert BOOK_ASK not in res.text
    assert "This page is for Acme Plumbing's customers" in res.text
    assert "ACME-XXXX" in res.text


def test_without_intent_the_composer_is_empty(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.get("/c/acme-plumbing?ref=ACME-7K3M")
    assert res.status_code == 200
    assert BOOK_ASK not in res.text


def test_unknown_intent_is_ignored(client, monkeypatch):
    _page_env(monkeypatch)
    res = client.get("/c/acme-plumbing?ref=ACME-7K3M&intent=drop-tables")
    assert res.status_code == 200
    assert BOOK_ASK not in res.text


def test_intent_book_needs_the_scheduling_module(client, monkeypatch):
    # No diary → no booking chip, and the deep link must not stage an ask the
    # assistant can't honour.
    _page_env(monkeypatch, modules=set())
    res = client.get("/c/acme-plumbing?ref=ACME-7K3M&intent=book")
    assert res.status_code == 200
    assert BOOK_ASK not in res.text


def test_chips_hidden_until_verified(client, monkeypatch):
    _page_env(monkeypatch)
    stranger = client.get("/c/acme-plumbing").text
    assert 'id="pt-chips" class="flex flex-wrap gap-2 pb-3 hidden"' in stranger
    customer = client.get("/c/acme-plumbing?ref=ACME-7K3M").text
    assert 'id="pt-chips" class="flex flex-wrap gap-2 pb-3"' in customer


# ── settings: invite only, nothing to publish ────────────────────────────────
from tests.test_settings_fold import _env      # noqa: E402  (shared settings env)


def test_settings_offer_no_website_snippets(client, monkeypatch):
    _env(monkeypatch)                          # models the auto-minted slug "acme"
    html = client.get("/app/settings").text
    assert "Put it on your website" not in html
    assert "portal-snippet-link" not in html
    assert "?intent=book" not in html
    assert "blam8.com/c/acme · invite only" in html
    assert "It&#39;s invite only" in html or "It's invite only" in html
