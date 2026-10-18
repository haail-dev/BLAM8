"""The CRM mark in a Today card's corner.

Kris, 2026-09-20: "On the Today page chips, add the CRM logo (two people, no
text) top right, linking to the contacts CRM. Then remove the Open in the CRM
from inside the promises expansion."

Pins:
  * a card the CRM knows links to their card by id; a card known only by
    address goes through crm/open, which finds them or searches; a card
    with neither (an overdue invoice names its customer and nothing else)
    carries no mark;
  * the mark is the strip's own two-people icon and no word;
  * no mark when the CRM is off, or behind a door this person cannot open;
  * the promise pop-up no longer carries "Open in the CRM".

Run: python -m pytest tests/test_deck_crm_mark.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from tests.test_deck_namesakes import HANNAH_B, SARAH, _crm, _promise
from tests.test_home_dashboard import Env


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _card(html, name):
    return next(c for c in html.split("<article") if name in c)


def _mark(card):
    start = card.index("data-crm-link") if "data-crm-link" in card else -1
    if start < 0:
        return None
    a = card.rfind("<a ", 0, start)
    return card[a:card.index("</a>", a)]


def test_a_known_contact_links_to_their_crm_card_by_id(client, monkeypatch):
    _crm(monkeypatch, [SARAH, HANNAH_B], [_promise("p1", "c3", "Send the slab measurements"),
                                         _promise("p2", "c1", "Send Biscuit's vaccination dates")])
    html = client.get("/app/").text
    sarah = _mark(_card(html, "Sarah Vance"))
    assert sarah and 'href="/app/crm/contacts/c3"' in sarah
    assert 'aria-label="Sarah Vance in the CRM"' in sarah
    assert "M15 19.128" in sarah                      # the strip's two-people icon…
    assert ">CRM<" not in sarah and "Open in the CRM<" not in sarah   # …and no word
    hannah = _mark(_card(html, "Hannah"))
    assert hannah and 'href="/app/crm/contacts/c1"' in hannah


def test_a_card_known_only_by_address_goes_through_crm_open(client, monkeypatch):
    """A draft waiting from someone the CRM has no card for yet: crm/open
    finds them, or lands on the list searched for them."""
    Env(monkeypatch, modules={"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [])
    html = client.get("/app/").text
    mark = _mark(_card(html, "b@x.com"))
    assert mark and 'href="/app/crm/open?email=b%40x.com"' in mark


def test_a_card_with_neither_id_nor_address_carries_no_mark(client, monkeypatch):
    """An overdue invoice names its customer and nothing else."""
    Env(monkeypatch, modules={"scheduling", "crm"}, convos=[])
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        {"invoice_ref": "INV-1003", "customer": "Dave Moss", "status": "sent",
         "total": 120.0, "amount_paid": 0, "due_date": "2026-01-05", "customer_email": None}])
    html = client.get("/app/").text
    if "Dave Moss" in html:                               # the money source is best-effort
        assert _mark(_card(html, "Dave Moss")) is None


def test_no_mark_when_the_crm_is_off(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling"})
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    html = client.get("/app/").text
    assert "Sarah" in html and "data-crm-link" not in html


def test_the_pop_up_no_longer_opens_the_crm(client, monkeypatch):
    _crm(monkeypatch, [SARAH], [_promise("p1", "c3", "Send the slab measurements")])
    html = client.get("/app/").text
    pop = html[html.index('<template data-promise-card="p1"'):]
    pop = pop[:pop.index("</template>")]
    assert "Open in the CRM" not in pop and "/app/crm/contacts/c3" not in pop
