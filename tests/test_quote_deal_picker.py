""""Which job is this for?" — asking, when the answer can't be walked to.

A quote raised FROM a job finds that job's deal on its own (db.deal_id_for_job:
quote → booking → the email that created it → the deal it was clustered onto).
A quote started from scratch on the form has neither a booking nor an email, and
a repeat customer with two live jobs is exactly where a guess would file it
wrong — so the form asks, and only when there is something to ask about.

What this pins:
  - the picker appears at TWO or more open deals; one is inherited
    automatically and none is nothing to ask about
  - closed deals are not offered
  - a deal id posted on the form is only trusted when it is genuinely one of
    THIS customer's open deals (the crm_intel.apply_deal rule: a form field must
    not be able to file a quote onto another customer's deal)
  - an explicit pick beats both the job walk and the single-deal guess

Run: python -m pytest tests/test_quote_deal_picker.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]
CTX = TenantCtx(tenant_id="t1", client=None)

DEALS = [
    {"id": "d-boiler", "title": "Boiler repair callout", "status": "open",
     "crm_stages": {"name": "Quoting"}},
    {"id": "d-cp12", "title": "Landlord gas safety certificate", "status": "open",
     "crm_stages": {"name": "New enquiry"}},
    {"id": "d-old", "title": "Last year's install", "status": "won",
     "crm_stages": {"name": "Won"}},
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email",
                        lambda tid, email, client=None:
                        {"id": "c1"} if email == "kris@x.co" else None)
    monkeypatch.setattr(dashboard.db, "crm_list_deals",
                        lambda tid, contact_id=None, company_id=None, client=None:
                        DEALS if contact_id == "c1" else [])
    app.dependency_overrides[require_onboarded] = lambda: CTX
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── the lookup ───────────────────────────────────────────────────────────────
def test_the_customers_open_deals_come_back_with_their_stage(client):
    r = client.get("/app/quotes/deals", params={"email": "kris@x.co"})
    assert r.status_code == 200
    deals = r.json()["deals"]
    assert [d["id"] for d in deals] == ["d-boiler", "d-cp12"]   # the won one is gone
    assert deals[0]["stage"] == "Quoting"


def test_an_unknown_customer_has_nothing_to_offer(client):
    assert client.get("/app/quotes/deals", params={"email": "nobody@x.co"}).json() == {"deals": []}
    assert client.get("/app/quotes/deals", params={"email": "  "}).json() == {"deals": []}
    assert client.get("/app/quotes/deals").json() == {"deals": []}


# ── the trust rule ───────────────────────────────────────────────────────────
def test_only_this_customers_own_open_deal_is_trusted(client):
    assert dashboard._picked_deal_id(CTX, "d-cp12", "kris@x.co") == "d-cp12"
    # a won deal is not offered, so it is not accepted either
    assert dashboard._picked_deal_id(CTX, "d-old", "kris@x.co") is None
    # somebody else's deal id, typed or forged into the form
    assert dashboard._picked_deal_id(CTX, "d-someone-else", "kris@x.co") is None
    # a deal id with the wrong customer attached to it
    assert dashboard._picked_deal_id(CTX, "d-cp12", "nobody@x.co") is None
    # nothing picked
    assert dashboard._picked_deal_id(CTX, "", "kris@x.co") is None
    assert dashboard._picked_deal_id(CTX, "   ", "kris@x.co") is None


# ── the form honours it ──────────────────────────────────────────────────────
def _post(client, monkeypatch, deal_id):
    seen = {}
    monkeypatch.setattr(dashboard.db, "create_quote",
                        lambda *a, **kw: seen.update(kw) or {"id": "q1"})
    r = client.post("/app/quotes/new", follow_redirects=False, data={
        "customer_email": "kris@x.co", "customer_name": "Kris",
        "deal_id": deal_id,
        "description": "Replace the pump", "amount": "240", "optionality": "required",
        "booking_id": "", "kb_entry_id": "", "is_selected": "1"})
    assert r.status_code == 303, r.text[:300]
    return seen


def test_an_explicit_pick_reaches_the_quote(client, monkeypatch):
    assert _post(client, monkeypatch, "d-cp12")["deal_id"] == "d-cp12"


def test_a_forged_pick_does_not_stick(client, monkeypatch):
    # None → create_quote falls through to the job walk, then the single-deal
    # guess, exactly as if nothing had been picked.
    assert _post(client, monkeypatch, "d-someone-else")["deal_id"] is None


def test_picking_nothing_leaves_it_unassigned(client, monkeypatch):
    assert _post(client, monkeypatch, "")["deal_id"] is None


# ── the form's own contract ──────────────────────────────────────────────────
def test_the_picker_ships_hidden_and_only_opens_when_it_has_to():
    html = (ROOT / "src/app" / "templates" / "app" / "quote_new.html").read_text(encoding="utf-8")
    assert 'id="deal-pick"' in html
    assert 'class="hidden sm:col-span-2" id="deal-pick"' in html   # hidden until asked for
    assert 'name="deal_id"' in html
    assert "Not one of these" in html                              # opting out stays possible
    assert "deals.length < 2" in html                              # one deal is not a question
    assert "/app/quotes/deals?email=" in html
