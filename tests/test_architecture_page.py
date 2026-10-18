"""/architecture — the public canvas page and its text fallback.

The canvas is drawn client-side from data duplicated in
src/app/static/architecture.js; the crawlable fallback renders server-side from
src/app/architecture.py. The consistency test here is what stops the two copies
drifting: change a node in one place and it fails until you change the other.

Run: python -m pytest tests/test_architecture_page.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.architecture import GROUPS
from app.main import app, _PUBLIC_PAGES

STATIC = Path(__file__).resolve().parents[1] / "src/app" / "static"


@pytest.fixture
def client():
    yield TestClient(app)  # no `with`: startup events must not run


def test_architecture_renders_unauthenticated(client):
    r = client.get("/architecture")
    assert r.status_code == 200
    # No auth redirect, no cookie-gated content: plain 200 HTML.
    assert r.headers["content-type"].startswith("text/html")


def test_text_fallback_is_server_rendered(client):
    # Crawlers/screen readers/AI retrieval must get every group heading and
    # every node label WITHOUT executing JS — straight from the HTML.
    from markupsafe import escape  # match Jinja's autoescaping ("Q&A" → "Q&amp;A")
    html = client.get("/architecture").text
    for group in GROUPS:
        assert str(escape(group["title"])) in html, f"group {group['title']!r} missing"
        for label, sublabel, _badge in group["nodes"]:
            assert str(escape(label)) in html, f"node {label!r} missing from fallback"
            if sublabel:
                assert str(escape(sublabel)) in html, f"sublabel {sublabel!r} missing"


def test_honest_sections_stay_in(client):
    # "Built, but dark" and "Not built" are deliberate — the page's whole
    # pitch is honesty, so their removal is a bug, not a cleanup.
    html = client.get("/architecture").text
    assert "Built, but dark" in html
    assert "Not built" in html
    assert "shown because hiding it would be lying" in html


def test_fallback_matches_canvas_data():
    # The JS canvas has its own copy of the node data; every label/sublabel in
    # the Python fallback must appear there too, or the two have drifted.
    js = (STATIC / "architecture.js").read_text(encoding="utf-8")
    for group in GROUPS:
        assert group["title"] in js, f"group {group['title']!r} not on the canvas"
        for label, sublabel, _badge in group["nodes"]:
            assert label in js, f"node {label!r} not on the canvas"
            if sublabel:
                assert sublabel in js, f"sublabel {sublabel!r} not on the canvas"


def test_no_inline_style_or_script_added(client):
    # The CSP must be satisfiable without 'unsafe-inline': the canvas ships as
    # /static/architecture.{css,js}, and this page's own template adds no
    # inline <style>, no style= attributes and no executable inline <script>.
    # (base.html's Tailwind bootstrap and the shared _concierge include are
    # site-wide concerns tracked separately — the check here is that the
    # architecture page introduces nothing new.)
    html = client.get("/architecture").text
    assert 'src="/static/architecture.js' in html
    assert 'href="/static/architecture.css' in html
    tpl = (Path(__file__).resolve().parents[1]
           / "src/app" / "templates" / "architecture.html").read_text(encoding="utf-8")
    assert 'style="' not in tpl
    assert "<style" not in tpl
    for tag in tpl.split("<script")[1:]:
        head = tag[:120]
        assert 'src="' in head or "application/ld+json" in head, \
            f"executable inline <script> in architecture.html: {head!r}"


def test_in_sitemap_and_llms_txt(client):
    assert "/architecture" in _PUBLIC_PAGES
    assert "/architecture</loc>" in client.get("/sitemap.xml").text
    assert "/architecture" in client.get("/llms.txt").text


def test_linked_from_capabilities_and_how_it_works(client):
    for page in ("/capabilities", "/how-it-works"):
        html = client.get(page).text
        assert 'href="/architecture"' in html, f"{page} does not link /architecture"


def test_og_preview_meta(client):
    html = client.get("/architecture").text
    assert "/static/img/og-architecture.png" in html
    assert (STATIC / "img" / "og-architecture.png").exists()
    # og:description comes from the meta_description block via base.html.
    assert 'property="og:description"' in html
