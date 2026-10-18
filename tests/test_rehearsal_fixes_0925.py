"""Fixes from the plumber film rehearsal (Kris 2026-09-25)."""
from app import db, setup_handoff


def test_the_today_tour_shows_search_and_where_people_lives():
    js = open("src/app/static/tour.js", encoding="utf-8").read()
    assert "sel: '[data-tour=\"search\"]'" in js and "sel: '[data-tour=\"menu\"]'" in js
    assert "Settings → People is where your team goes" in js
    base = open("src/app/templates/app/base.html", encoding="utf-8").read()
    assert 'id="search-btn" data-tour="search"' in base and 'id="menu-btn" data-tour="menu"' in base


def test_a_new_owner_is_on_their_own_team(monkeypatch):
    calls = []

    class Q:
        def __init__(self, t): self.t = t
        def __getattr__(self, a):
            return lambda *x, **k: self
        def execute(self):
            return type("R", (), {"data": [{"id": "t-new"}]})()
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(db, "has_any_membership", lambda uid: False)   # a brand-new user
    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda s, n: Q(n)})())
    monkeypatch.setattr(db, "sync_team_person_for_member", lambda tid, uid, f: calls.append((tid, uid, f)))
    import app.db as d
    try:
        d.ensure_tenant_for_user("u1", "Hartwell", email="Dan@Example.com")
    except Exception:
        pass   # the trial seeding after it may touch more; the team row comes first
    assert calls and calls[0][2]["role"] == "owner" and calls[0][2]["name"] == "dan@example.com"


def test_the_strip_says_what_finishes_getting_paid():
    steps = setup_handoff.steps(set(), None, None, None, {})
    pay = [s for s in steps if s["key"] == "invoicing"][0]
    assert pay["move_on"] == "Add your business name or bank details, then Save"


def test_the_help_never_calls_a_default_the_owners_own():
    src = open("src/app/concierge.py", encoding="utf-8").read()
    from app import concierge
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "never guessed: a value they never gave stays off the card" in prompt
    assert "Never call a field 'already filled in'" in prompt and "ALREADY in its boxes" not in src
