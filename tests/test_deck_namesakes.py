"""Two Hannahs on Today, and no way to know which is which.

Kris, 2026-09-20: "I have noticed 2 people called Hannah in the Today page.
If these are different contacts it's not easy to know. I think we need to
display the email before the @ next to the names?"

When two cards on the deck share a name, each wears the handle of its
address — the part before the @ — beside the name. A name nobody else on
the deck shares stays as it was: "Sarah Vance · sarah.vance" on every card
would be noise, and the handle is there to tell people apart, not to
introduce them. Namesakes whose handles also match show the whole address.
The promise pop-up's Who row carries the address too.

Run: python -m pytest tests/test_deck_namesakes.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import deck
from app.auth import TenantCtx, require_onboarded
from app.main import app
from tests.test_home_dashboard import Env


def card(name, email=None):
    return {"name": name, "email": email, "facts": [], "diary": []}


# ── The rule (src/app/deck.py — pure) ──────────────────────────────────────────
def test_namesakes_wear_their_handles_and_nobody_else_does():
    cards = [card("Hannah", "hannah.b@gmail.com"), card("Hannah", "hannah@wellsfamily.co.uk"),
             card("Sarah Vance", "sarah@acme.co.uk")]
    assert deck.namesakes(cards) is cards
    assert [c.get("handle") for c in cards] == ["hannah.b", "hannah", None]


def test_the_name_is_matched_without_regard_to_case_or_spacing():
    cards = [card("hannah ", "a@x.com"), card("Hannah", "b@y.com")]
    deck.namesakes(cards)
    assert [c["handle"] for c in cards] == ["a", "b"]


def test_namesakes_whose_handles_also_match_show_the_whole_address():
    cards = [card("Hannah", "hannah@gmail.com"), card("Hannah", "Hannah@work.com")]
    deck.namesakes(cards)
    assert [c["handle"] for c in cards] == ["hannah@gmail.com", "Hannah@work.com"]


def test_a_card_with_no_address_cannot_wear_one_and_does_not_break_the_other():
    """An overdue invoice names its customer and nothing else."""
    cards = [card("Hannah", "hannah@x.com"), card("Hannah")]
    deck.namesakes(cards)
    assert cards[0]["handle"] == "hannah" and "handle" not in cards[1]


def test_a_card_headed_by_its_own_address_is_not_doubled():
    cards = [card("b@x.com", "b@x.com"), card("b@x.com", "b@x.com")]
    deck.namesakes(cards)
    assert all("handle" not in c for c in cards)


def test_empty_and_nameless_cards_are_left_alone():
    assert deck.namesakes([]) == []
    cards = [card("", "a@x.com"), card(None, "b@x.com")]
    deck.namesakes(cards)
    assert all("handle" not in c for c in cards)


# ── On the page ────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


HANNAH_B = {"id": "c1", "name": "Hannah", "email": "hannah.b@gmail.com", "crm_companies": None}
HANNAH_W = {"id": "c2", "name": "Hannah", "email": "hannah@wellsfamily.co.uk", "crm_companies": None}
SARAH = {"id": "c3", "name": "Sarah Vance", "email": "sarah@acme.co.uk", "crm_companies": None}


def _promise(pid, cid, desc):
    return {"id": pid, "contact_id": cid, "direction": "we_owe", "description": desc,
            "status": "open", "email_log_id": None, "created_at": "2026-09-04T10:00:00+00:00"}


def _crm(monkeypatch, contacts, promises):
    Env(monkeypatch, modules={"scheduling", "crm"}, convos=[])
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: list(contacts))
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: list(promises))
    monkeypatch.setattr(dashboard.db, "get_email_log_entries", lambda *a, **k: [])


def _header(card_html):
    start = card_html.index("font-display font-semibold text-navy")
    return card_html[start:card_html.index("</p>", start)]


def test_two_hannahs_on_today_are_told_apart(client, monkeypatch):
    _crm(monkeypatch, [HANNAH_B, HANNAH_W, SARAH],
         [_promise("p1", "c1", "Send Biscuit's vaccination dates"), _promise("p2", "c2", "Ring back about Biscuit"),
          _promise("p3", "c3", "Send the slab measurements")])
    html = client.get("/app/").text
    heads = [_header(c) for c in html.split("<article")[1:]]
    assert len(heads) == 3
    hannahs = [h for h in heads if "Hannah" in h]
    assert len(hannahs) == 2
    assert any("· hannah.b</span>" in h for h in hannahs)
    assert any("· hannah</span>" in h for h in hannahs)
    sarah = next(h for h in heads if "Sarah Vance" in h)
    assert "· sarah" not in sarah                       # a name nobody shares stays clean


def test_a_lone_hannah_stays_plain(client, monkeypatch):
    _crm(monkeypatch, [HANNAH_B, SARAH],
         [_promise("p1", "c1", "Send Biscuit's vaccination dates"), _promise("p3", "c3", "Send the slab measurements")])
    heads = [_header(c) for c in client.get("/app/").text.split("<article")[1:]]
    assert len(heads) == 2
    assert not any("· hannah.b" in h or "· sarah" in h for h in heads)


def test_the_promise_pop_up_names_the_address_too(client, monkeypatch):
    _crm(monkeypatch, [HANNAH_B, HANNAH_W],
         [_promise("p1", "c1", "Send Biscuit's vaccination dates"), _promise("p2", "c2", "Ring back about Biscuit")])
    html = client.get("/app/").text
    pop = html[html.index('<template data-promise-card="p1"'):]
    pop = pop[:pop.index("</template>")]
    assert "· hannah.b@gmail.com" in pop
