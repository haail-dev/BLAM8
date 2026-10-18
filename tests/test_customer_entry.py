"""General customer entry point (/for-customers) — the slug-less lookups.

The entry page needs two things no portal route offered before: reference
code → the business's portal URL, and business-name search → portal links.
Both are public and unauthenticated, so the tests pin the privacy line:
only business name + portal link ever come back — never contact data —
and anything ambiguous (cross-tenant code collision) is "not found".

Run: python -m pytest tests/test_customer_entry.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.portal as portal
from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    yield TestClient(app)


def _tenants(monkeypatch, rows, cfgs=None):
    monkeypatch.setattr(portal.db, "tenants_for_reference_code",
                        lambda code: [dict(r) for r in rows])
    monkeypatch.setattr(portal.db, "search_portal_tenants",
                        lambda q, **k: [dict(r) for r in rows])
    monkeypatch.setattr(portal.db, "get_tenant_config",
                        lambda tid: dict((cfgs or {}).get(tid, {"portal_enabled": True})))


# ── reference code → portal URL ──────────────────────────────────────────────
def test_entry_ref_redirects_to_the_minting_business(client, monkeypatch):
    _tenants(monkeypatch, [{"id": "t1", "name": "Test Blam8",
                            "portal_slug": "test-blam8"}])
    out = client.post("/api/portal/entry/ref", json={"ref": "test-krh7"}).json()
    assert out["found"] is True
    assert out["business"] == "Test Blam8"
    assert out["url"] == "/c/test-blam8?ref=TEST-KRH7"   # normalised to upper
    assert set(out) == {"found", "business", "url"}      # nothing else leaks


def test_entry_ref_unknown_or_malformed_is_not_found(client, monkeypatch):
    calls = []
    monkeypatch.setattr(portal.db, "tenants_for_reference_code",
                        lambda code: calls.append(code) or [])
    # malformed shapes never reach the database
    for bad in ("not a code", "TOOLONGPREFIX-AAAA", "ACME-AA", "ACME_7K2P"):
        assert client.post("/api/portal/entry/ref",
                           json={"ref": bad}).json() == {"found": False}
    assert not calls
    # well-formed but unknown → same answer
    assert client.post("/api/portal/entry/ref",
                       json={"ref": "ACME-7K2P"}).json() == {"found": False}
    assert calls == ["ACME-7K2P"]


def test_entry_ref_ambiguous_collision_is_not_found(client, monkeypatch):
    # Two tenants minted the same code (unique per tenant, not globally):
    # never guess which business the customer meant.
    _tenants(monkeypatch, [
        {"id": "t1", "name": "Acme A", "portal_slug": "acme-a"},
        {"id": "t2", "name": "Acme B", "portal_slug": "acme-b"},
    ])
    assert client.post("/api/portal/entry/ref",
                       json={"ref": "ACME-7K2P"}).json() == {"found": False}


def test_entry_ref_skips_disabled_or_slugless_portals(client, monkeypatch):
    # A switched-off portal must not be confirmed to exist via the entry page
    # (same line the /c/{slug} 404 draws); a slugless tenant has no URL to give.
    _tenants(monkeypatch, [{"id": "t1", "name": "Acme", "portal_slug": "acme"}],
             cfgs={"t1": {"portal_enabled": False}})
    assert client.post("/api/portal/entry/ref",
                       json={"ref": "ACME-7K2P"}).json() == {"found": False}
    _tenants(monkeypatch, [{"id": "t2", "name": "NoSlug Ltd", "portal_slug": None}])
    assert client.post("/api/portal/entry/ref",
                       json={"ref": "NOSLUG-7K2P"}).json() == {"found": False}


# ── business-name search ─────────────────────────────────────────────────────
def test_entry_search_returns_live_portal_links_only(client, monkeypatch):
    _tenants(monkeypatch, [
        {"id": "t1", "name": "Velox Velcee", "portal_slug": "velox-velcee"},
        {"id": "t2", "name": "Velox Off", "portal_slug": "velox-off"},
    ], cfgs={"t1": {"portal_enabled": True}, "t2": {"portal_enabled": False}})
    out = client.post("/api/portal/entry/search", json={"q": "Velox"}).json()
    assert out == {"results": [{"name": "Velox Velcee", "url": "/c/velox-velcee"}]}


def test_entry_search_short_query_returns_nothing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(portal.db, "search_portal_tenants",
                        lambda q, **k: calls.append(q) or [])
    assert client.post("/api/portal/entry/search",
                       json={"q": "v"}).json() == {"results": []}
    assert not calls        # never hits the database under the minimum length


def test_entry_search_caps_results_at_six(client, monkeypatch):
    rows = [{"id": f"t{i}", "name": f"Acme {i}", "portal_slug": f"acme-{i}"}
            for i in range(8)]
    _tenants(monkeypatch, rows)
    out = client.post("/api/portal/entry/search", json={"q": "Acme"}).json()
    assert len(out["results"]) == 6


# ── the page itself ──────────────────────────────────────────────────────────
def test_for_customers_page_prefills_search_query(client):
    html = client.get("/for-customers?q=Velox").text
    assert 'value="Velox"' in html
    # and the q is HTML-escaped, not reflected raw
    html = client.get('/for-customers?q="><script>x</script>').text
    assert "<script>x</script>" not in html
