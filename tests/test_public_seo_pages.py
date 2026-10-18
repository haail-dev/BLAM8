"""Public SEO surface — the unauthenticated marketing pages and their plumbing.

Every comparison/vertical page must render (a template typo = a 500 on a page
Google is indexing), appear in sitemap.xml, and stay in step with _PUBLIC_PAGES.
The vs/* pages additionally carry FAQPage structured data — a malformed JSON-LD
block silently kills the rich result, so we parse it.

Run: python -m pytest tests/test_public_seo_pages.py -q
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app, _PUBLIC_PAGES


@pytest.fixture
def client():
    yield TestClient(app)  # no `with`: startup events must not run


VS_PAGES = [p for p in _PUBLIC_PAGES if p.startswith("/vs/")]
HELP_PAGES = [p for p in _PUBLIC_PAGES if p.startswith("/help/")]


def test_expected_vs_pages_are_registered():
    assert set(VS_PAGES) == {"/vs/mailchimp", "/vs/zapier", "/vs/jobber",
                             "/vs/servicem8", "/vs/tradify", "/vs/fyxer",
                             "/vs/podium", "/vs/moneypenny", "/vs/calendly",
                             "/vs/hubspot"}


def _faq_questions(html):
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    graph = json.loads(blocks[0]).get("@graph", [])
    faq = next(n for n in graph if n.get("@type") == "FAQPage")
    return [(q["name"], q["acceptedAnswer"]["text"]) for q in faq["mainEntity"]]


@pytest.mark.parametrize("path", ["/vs/tradify", "/vs/moneypenny"])
def test_new_vs_pages_keep_faq_json_ld_in_step_with_the_visible_faq(client, path):
    # 2026-09-25 wave 3: every FAQ answer an AI reads from JSON-LD must be the
    # same words a visitor sees on the page.
    html = client.get(path).text
    qs = _faq_questions(html)
    assert len(qs) >= 4
    visible = html.split('id="faq"', 1)[1]
    for name, text in qs:
        assert name in visible, name
        assert text in visible, name
    assert "checked September 2026" in html
    assert 'href="/login"' in html
    for stale in ("rolling out", "carrier verification", "Coming soon"):
        assert stale not in html, stale


def test_tradify_page_quotes_uk_per_user_prices_and_concedes_its_ground(client):
    html = client.get("/vs/tradify").text
    for price in ("£34", "£37", "£44"):
        assert price in html, price
    assert "per user" in html and "Plus plan only" in html
    assert "timesheets" in html.lower() and "purchase orders" in html
    assert "£39" in html and "£99" in html and "£249" in html


def test_moneypenny_page_never_claims_blam8_answers_calls(client):
    html = client.get("/vs/moneypenny").text
    assert "from £25 a month" in html
    assert "BLAM8 doesn't answer calls" in html or "BLAM8 doesn't answer the phone" in html
    assert "Does BLAM8 answer phone calls?" in html
    assert "cheaper than a part-time" not in html.lower()
    assert "time saved" not in html.lower()


@pytest.mark.parametrize("path", [p for p in _PUBLIC_PAGES if p != "/login"])
def test_public_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_sitemap_lists_every_public_page(client):
    xml = client.get("/sitemap.xml").text
    for path in _PUBLIC_PAGES:
        assert f"{path}</loc>" in xml or f"{path}<" in xml, f"{path} missing from sitemap"


@pytest.mark.parametrize("path", VS_PAGES + HELP_PAGES
                         + ["/how-it-works", "/security", "/stripe",
                            "/for-businesses", "/for-customers"])
def test_vs_page_has_valid_faq_structured_data(client, path):
    html = client.get(path).text
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert blocks, f"{path} has no JSON-LD block"
    data = json.loads(blocks[0])  # raises on malformed JSON-LD
    graph = data.get("@graph", [data])
    faq = next((n for n in graph if n.get("@type") == "FAQPage"), None)
    assert faq, f"{path} JSON-LD has no FAQPage node"
    assert faq["mainEntity"], f"{path} FAQPage is empty"


@pytest.mark.parametrize("path", VS_PAGES)
def test_vs_page_is_linked_from_compare_hub(client, path):
    # The internal-linking spine is footer → /compare → every vs page —
    # an orphaned page ranks for nothing.
    html = client.get("/compare").text
    assert f'href="{path}"' in html, f"{path} not linked from /compare"


def test_help_hub_links_every_topic(client):
    # Same spine rule as /compare: the hub must link every answer page, or
    # a topic is orphaned and ranks for nothing.
    html = client.get("/help").text
    for path in HELP_PAGES:
        assert f'href="{path}"' in html, f"{path} not linked from /help"
    assert client.get("/help/not-a-real-topic").status_code == 404


def test_llms_txt_serves_the_ai_briefing(client):
    # llms.txt is the briefing AI assistants read to recommend BLAM8 —
    # it must render, link every comparison page, and carry exactly the three
    # public tiers (parked OS/Enterprise prices must never leak back in).
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# BLAM8")
    for path in VS_PAGES:
        assert path in r.text, f"{path} missing from llms.txt"
    assert "Starter £39" in r.text and "Pro £99" in r.text
    assert "Pro + Concierge £249" in r.text
    for gone in ("£248", "£298", "£149", "£799"):
        assert gone not in r.text, gone


def test_homepage_leads_with_the_pitch_not_a_question(client):
    # Kris 2026-08-30: customers reach their portal by invite links (email/SMS),
    # so the homepage is the business pitch — the audience-split box is GONE.
    # Both entry pages stay one click away in the footer for crawlers.
    html = client.get("/").text
    assert "Are you a business or a customer?" not in html
    assert "data-audience" not in html
    assert 'href="/for-businesses"' in html             # footer
    assert 'href="/for-customers"' in html              # footer
    # The WebSite SearchAction still targets the customer entry's search.
    assert "/for-customers?q={search_term_string}" in html
    assert "SiteNavigationElement" in html


def test_audience_pages_linked_from_site_chrome(client):
    # The entry pages live in the footer only now (2026-08-30 nav diet: three
    # destinations in the header) — still one click from every page.
    html = client.get("/capabilities").text
    assert html.count('href="/for-businesses"') >= 1   # footer
    assert html.count('href="/for-customers"') >= 1


def test_compare_hub_is_linked_from_site_footer(client):
    # The other half of the spine: the hub itself must be one click from
    # every page's footer.
    html = client.get("/capabilities").text
    assert 'href="/compare"' in html, "/compare not linked in footer"


@pytest.mark.parametrize("host", ["blam8.haail.dev", "blautom8.onrender.com"])
def test_legacy_host_301s_to_canonical(client, host):
    # The legacy domain and the Render host each serve the whole site — a 301
    # (not just a canonical tag) is what stops them competing with blam8.com.
    r = client.get("/security?a=1", headers={"host": host},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith("/security?a=1")
    assert host not in r.headers["location"]


def test_legacy_host_leaves_health_and_posts_alone(client):
    # Render's health checker follows no redirects, and webhooks POST to
    # whatever host they were registered with — neither may bounce.
    r = client.get("/health", headers={"host": "blautom8.onrender.com"},
                   follow_redirects=False)
    assert r.status_code == 200
    r = client.post("/webhook", headers={"host": "blautom8.onrender.com"},
                    follow_redirects=False)
    assert r.status_code != 301


def test_homepage_prices_all_three_tiers_and_keeps_the_concierge_off_the_lower_two(client):
    # 2026-09-25: Pro + Concierge (£249) joined Starter and Pro. The price cards
    # and the JSON-LD offer carry it, and nothing says the Concierge is on
    # "both plans" any more.
    html = client.get("/").text
    for price in ("&pound;39", "&pound;99", "&pound;249"):
        assert price in html, price
    assert '"highPrice": "249"' in html and '"offerCount": "3"' in html
    assert "Pro + Concierge" in html
    assert "On both plans" not in html
    assert "full Pro product" not in html
