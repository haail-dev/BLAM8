"""/pages — every page BLAM8 serves, grouped (src/app/pages_index.py).

The guarantee this pins: the FastAPI route table and the index can't drift. Every
GET route is either a listed page, an excluded non-page with a reason, an /api/
endpoint or a machine file by suffix — anything else fails here, so a new page
lands in the index the day it lands in the app. The other direction holds too:
a listed route or an excluded path that no longer exists fails.

Run: python -m pytest tests/test_pages_index.py -q
"""
from __future__ import annotations

import html

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import help_center, pages_index, setups
from app import main as app_main
from app.main import app


def _sections():
    return pages_index.build(app_main._COMPARISONS, help_center.TOPICS, setups.SETUPS)


def _get_routes() -> set[str]:
    return {r.path for r in app.routes if isinstance(r, APIRoute) and "GET" in r.methods}


def test_every_get_route_is_a_listed_page_or_excluded_with_a_reason():
    routes = _get_routes()
    listed = pages_index.listed_routes(_sections())
    missing = sorted(
        p for p in routes
        if not p.startswith("/api/") and not p.endswith(pages_index.MACHINE_SUFFIXES)
        and p not in pages_index.EXCLUDED and p not in listed)
    assert not missing, f"routes neither listed on /pages nor excluded: {missing}"


def test_no_listed_or_excluded_route_is_dead():
    routes = _get_routes()
    dead = sorted(r for r in pages_index.listed_routes(_sections()) if r not in routes)
    assert not dead, f"/pages lists routes the app no longer serves: {dead}"
    stale = sorted(p for p in pages_index.EXCLUDED if p not in routes)
    assert not stale, f"EXCLUDED names routes that no longer exist: {stale}"


def test_index_shape():
    sections = _sections()
    keys = [s["key"] for s in sections]
    assert keys == ["site", "in", "app", "os", "customers", "partners", "admin"]
    for s in sections:
        for g in s["groups"]:
            assert g["access"] in pages_index.ACCESS, (s["key"], g["title"])
            for p in g["pages"]:
                # a per-record page has no link of its own but says where it opens
                # from; a dynamic family (help topics, setups) links each member
                if p["href"] is None:
                    assert "{" in p["route"] or p["route"] == "/app/conversations/view", p
                    assert p["via"], p
                elif "{" not in p["route"]:
                    assert p["href"] == p["route"], p
    # the dynamic families track their sources
    help_links = {p["href"] for s in sections for g in s["groups"] for p in g["pages"]
                  if p["route"] == "/help/{slug}"}
    assert help_links == {f"/help/{t['slug']}" for t in help_center.TOPICS}
    assert pages_index.count(sections) > 100


@pytest.fixture
def client():
    yield TestClient(app)


def test_pages_renders_every_section_and_hides_admin_links(client):
    r = client.get("/pages")
    assert r.status_code == 200
    for s in _sections():
        assert f'id="{s["key"]}"' in r.text, s["key"]
        for g in s["groups"]:
            assert html.escape(g["title"], quote=False) in r.text, g["title"]
    assert 'href="/app/payments"' in r.text and 'href="/help"' in r.text
    assert 'href="/vs/servicem8"' in r.text and 'href="/setups"' in r.text
    assert 'href="/vs/tradify"' in r.text and 'href="/vs/moneypenny"' in r.text
    # no admin session → the console is named, its links are not
    assert "Operator console" in r.text and 'href="/admin/llm"' not in r.text
    assert 'href="/pages"' in r.text          # the footer link, and the self entry
