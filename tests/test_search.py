"""The masthead search (Kris 2026-09-21): a magnifying glass beside the cog.

"Used to search Booking / Quote / Invoice IDs, and to search for contacts via
name or email. A smart search that suggests and filters during input. Once
that's in, remove the search box from the CRM page."

What this pins:
  - what a query means (app/search.parse): a reference or the start of one
    searches that kind, a bare number every kind, anything else a customer;
  - how a row is shown, in the operator's words and dates;
  - the endpoint obeys the doors and the staff lens of the pages it opens, and
    an ignored sender is not a customer here either;
  - the glass sits left of the cog on every page, the sheet is under the bar,
    the script drives the keyboard and never builds markup from data;
  - the CRM page lost its box but `q` still narrows (the inbox lands with it).

Run: python -m pytest tests/test_search.py -q
"""
import io

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import pages_index, search
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf
from tests.test_crm_routes import Env as CrmEnv


def _read(p):
    return io.open(p, encoding="utf-8").read()


# ── what a query means ────────────────────────────────────────────────────────
def test_parse_reads_references_numbers_and_names():
    assert search.parse("BK-1003") == {"refs": {"BK": "BK-1003"}, "text": ""}
    assert search.parse("bk 100")["refs"] == {"BK": "BK-100"}
    assert search.parse("booking #1003")["refs"] == {"BK": "BK-1003"}
    assert search.parse("inv-10")["refs"] == {"INV": "INV-10"}
    assert search.parse("Quote 1001")["refs"] == {"QUO": "QUO-1001"}
    assert search.parse("invoice")["refs"] == {"INV": "INV-"}            # a bare kind = the latest
    assert search.parse("1003")["refs"] == {"BK": "BK-1003", "QUO": "QUO-1003", "INV": "INV-1003"}
    assert search.parse("rob") == {"refs": {}, "text": "rob"}
    assert search.parse("rob@acme")["text"] == "rob@acme"
    assert search.parse("Anne-Marie  2")["text"] == "Anne-Marie 2"
    assert search.parse("   ") == {"refs": {}, "text": ""}
    assert search.safe_text("rob,(x)%*") == "robx"


def test_rows_are_shown_in_the_operators_words():
    b = search.booking_result({"id": "b1", "booking_ref": "BK-1003", "summary": "Bella",
                               "contact": "jo@x.co", "booking_date": "2026-10-07", "status": "confirmed"})
    assert b == {"kind": "booking", "ref": "BK-1003", "label": "BK-1003 · Bella",
                 "sub": "7th Oct 26 · confirmed · jo@x.co", "href": "/app/diary/b1"}
    q = search.quote_result({"id": "q1", "quote_ref": "QUO-1001", "title": "Kitchen",
                             "customer_name": "Jo", "status": "sent", "total": "1200.5"})
    assert q["label"] == "QUO-1001 · Kitchen" and q["sub"] == "£1,200.50 · sent · Jo"
    assert q["href"] == "/app/quotes/q1"
    i = search.invoice_result({"id": "i1", "invoice_ref": "INV-1002", "customer_email": "jo@x.co",
                               "status": "paid", "total": 80})
    assert i["label"] == "INV-1002 · jo@x.co" and i["sub"] == "£80.00 · paid" and i["href"] == "/app/invoices/i1"
    c = search.contact_result({"id": "c1", "name": "Rob Smith", "email": "rob@acme.co.uk",
                               "crm_companies": {"name": "Acme"}})
    assert c == {"kind": "contact", "ref": "", "label": "Rob Smith",
                 "sub": "rob@acme.co.uk · Acme", "href": "/app/crm/contacts/c1"}
    assert search.contact_result({"id": "c2", "email": "sarah@gmail.com"})["label"] == "sarah@gmail.com"


# ── the endpoint ──────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


BOOKINGS = [
    {"id": "b1", "booking_ref": "BK-1003", "summary": "Bella", "contact": "jo@x.co",
     "booking_date": "2026-10-07", "status": "confirmed", "resource_type": None},
    {"id": "b2", "booking_ref": "BK-1004", "summary": "Meet & greet", "contact": "jo@x.co",
     "booking_date": "2026-10-09", "status": "proposed", "resource_type": "Meet and greet"},
]
QUOTES = [{"id": "q1", "quote_ref": "QUO-1001", "title": "Kitchen", "customer_name": "Jo",
           "status": "sent", "total": 1200}]
INVOICES = [{"id": "i1", "invoice_ref": "INV-1002", "customer_name": "Jo", "status": "paid", "total": 80}]
CONTACTS = [
    {"id": "c1", "name": "Rob Smith", "email": "rob@acme.co.uk", "crm_companies": {"name": "Acme"}},
    {"id": "c2", "name": None, "email": "robot@spam.io", "crm_companies": None},
]


def _env(monkeypatch, *, me=None, modules=("crm",), ignored=()):
    monkeypatch.setattr(dashboard, "_me", lambda ctx: me)
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set(modules))

    def refs(rows, col):
        return lambda tid, prefix, limit=6, client=None: [
            r for r in rows if r[col].startswith(prefix.upper())][:limit]
    monkeypatch.setattr(dashboard.db, "search_booking_refs", refs(BOOKINGS, "booking_ref"))
    monkeypatch.setattr(dashboard.db, "search_quote_refs", refs(QUOTES, "quote_ref"))
    monkeypatch.setattr(dashboard.db, "search_invoice_refs", refs(INVOICES, "invoice_ref"))
    monkeypatch.setattr(dashboard.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, offset=0, client=None: [
                            c for c in CONTACTS
                            if (q or "").lower() in ((c.get("name") or "") + c["email"]).lower()])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: list(ignored))
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"service_types": []})
    monkeypatch.setattr(dashboard.db, "list_booking_contacts", lambda *a, **k: [])


def test_a_reference_finds_its_kind_and_a_number_finds_every_kind(client, monkeypatch):
    _env(monkeypatch)
    r = client.get("/app/search", params={"q": "bk-100"}).json()
    assert [x["ref"] for x in r["results"]] == ["BK-1003", "BK-1004"]
    assert r["results"][0]["href"] == "/app/diary/b1" and r["q"] == "bk-100"
    r = client.get("/app/search", params={"q": "100"}).json()
    assert [(x["kind"], x["ref"]) for x in r["results"]] == [
        ("booking", "BK-1003"), ("booking", "BK-1004"), ("quote", "QUO-1001"), ("invoice", "INV-1002")]
    r = client.get("/app/search", params={"q": "inv 1002"}).json()
    assert [x["href"] for x in r["results"]] == ["/app/invoices/i1"]
    assert client.get("/app/search", params={"q": "quote 9"}).json()["results"] == []


def test_a_name_or_email_finds_customers_but_never_an_ignored_sender(client, monkeypatch):
    _env(monkeypatch, ignored=["robot@spam.io"])
    r = client.get("/app/search", params={"q": "rob"}).json()
    assert [x["label"] for x in r["results"]] == ["Rob Smith"]
    assert r["results"][0]["href"] == "/app/crm/contacts/c1"
    assert r["results"][0]["sub"] == "rob@acme.co.uk · Acme"
    assert client.get("/app/search", params={"q": "acme.co"}).json()["results"][0]["label"] == "Rob Smith"
    assert client.get("/app/search", params={"q": "r"}).json()["results"] == []      # two characters or more
    assert client.get("/app/search", params={"q": ""}).json() == {"q": "", "results": []}
    # no CRM on the plan → no customers, and no error
    _env(monkeypatch, modules=())
    assert client.get("/app/search", params={"q": "rob"}).json()["results"] == []


def test_staff_find_only_what_their_doors_and_kinds_allow(client, monkeypatch):
    staff = {"user_id": "u2", "role": "staff", "active": True,
             "scopes": {"pages": ["today", "diary"], "does": ["svc:meet and greet"]}}
    _env(monkeypatch, me=staff)
    # The diary's own lens decides which bookings are theirs (people.owns_booking);
    # here it is stubbed to one ref so the test is about the endpoint applying it.
    monkeypatch.setattr(dashboard.people, "owns_booking", lambda m, b, cfg: b["booking_ref"] == "BK-1004")
    r = client.get("/app/search", params={"q": "100"}).json()
    assert [(x["kind"], x["ref"]) for x in r["results"]] == [("booking", "BK-1004")]   # no Money door
    assert client.get("/app/search", params={"q": "rob"}).json()["results"] == []       # no CRM door


# ── the shell and the CRM page ────────────────────────────────────────────────
def test_the_glass_sits_beside_the_cog_on_every_page():
    base = _read("src/app/templates/app/base.html")
    assert 'id="search-btn"' in base and 'aria-label="Search"' in base
    assert base.index('id="search-btn"') < base.index('aria-label="Settings for this section"')   # left of the cog
    assert base.index('aria-label="Settings for this section"') < base.index('id="menu-btn"')
    sheet = base.split('id="search-box"', 1)[1]
    assert 'data-pull-refresh="off"' in sheet[:400] and 'id="search-input"' in sheet and 'id="search-results"' in sheet
    assert "/static/search.js" in base
    js = _read("src/app/static/search.js")
    assert "/app/search?q=" in js and "ArrowDown" in js and "'Escape'" in js and "metaKey" in js
    assert "innerHTML" not in js               # rows are DOM nodes, never markup with data in it
    assert "/app/search" in pages_index.EXCLUDED


def test_the_crm_page_lost_its_box_but_keeps_the_filter(client, monkeypatch):
    CrmEnv(monkeypatch)
    html = client.get("/app/crm").text
    assert 'name="q"' not in html and "Search name or email" not in html and "data-crm-filter" not in html
    html = client.get("/app/crm", params={"q": "rob"}).text
    assert 'Showing matches for <span class="font-medium text-navy">"rob"</span>' in html
    assert 'href="/app/crm"' in html.split("data-crm-filter", 1)[1][:240]
    assert "Rob Smith" in html and "sarah@gmail.com" not in html
