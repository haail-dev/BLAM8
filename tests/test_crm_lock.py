"""The CRM stays a door on a plan without it — locked, not gone (Kris
2026-09-13: "when someone has used the trial and gone for Starter, the CRM
should be locked"). The lock the Concierge tab will wear next.

Run: python -m pytest tests/test_crm_lock.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf
from tests.test_home_dashboard import Env

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture
def client(monkeypatch):
    Env(monkeypatch, modules={"scheduling"})                       # Starter: no CRM
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [{}] * 12)
    monkeypatch.setattr(dashboard.db, "crm_list_companies", lambda *a, **k: [{}] * 3)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_the_front_door_shows_the_lock_and_what_is_behind_it(client):
    r = client.get("/app/crm")
    assert r.status_code == 200
    html = r.text
    assert "data-crm-locked-page" in html and "Your CRM is locked on this plan" in html
    assert "already met <span class=\"font-semibold text-navy\">12 contacts</span> across <span class=\"font-semibold text-navy\">3 companies</span>" in html
    assert 'href="/billing/plans"' in html and "Unlock on Pro — £99/month" in html
    assert "Sarah" not in html and "a@x.com" not in html                  # nothing usable shows
    # The shell: the CRM door stays, marked locked, in the menu and the strip; no cog.
    assert 'data-crm-locked>Locked</span>' in html
    assert 'href="/app/crm" class="flex-1 flex flex-col items-center' in html and "data-crm-locked>" in html
    assert 'aria-label="Settings for this section"' not in html


def test_every_other_crm_page_sends_a_locked_account_to_the_door(client):
    for path in ("/app/crm/companies", "/app/crm/pipeline", "/app/crm/settings", "/app/crm/open?email=x%40y.co"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/app/crm", path


def test_todays_crm_tile_stays_locked_with_the_count(client):
    html = client.get("/app/").text
    tile = html.split('data-tour="crm"')[1].split("</a>")[0]
    assert 'data-locked="1"' in html and ">Locked<" in tile and "12" in tile
    assert "Already filled in from your inbox · unlock on Pro to open them" in tile
    js = _read("src/app/static/tour.js")
    assert "var locked = !!document.querySelector('[data-tour=\"crm\"][data-locked]');" in js
    assert "unlock it on Pro to open the cards." in js


def test_an_unlocked_account_is_untouched(monkeypatch):
    Env(monkeypatch, modules={"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_companies", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        html = TestClient(app, follow_redirects=False).get("/app/").text
        assert 'data-tour="crm"' in html and 'data-locked="1"' not in html and "data-crm-locked" not in html
    finally:
        app.dependency_overrides.clear()
    shell = _read("src/app/templates/app/base.html")
    assert "{% if false %}" in shell                                       # the strip's old Modules fallback, retired
    assert 'href="/app/modules" class="{{ item }}' in shell                # Modules keeps its place in the menu
