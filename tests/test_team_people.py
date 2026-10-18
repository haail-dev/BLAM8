"""People without a login (docs/plan-people-team-and-jobs.md step 1; Kris
2026-09-24 "Yes build it"): someone on the team who never signs in — a partner
on the floor, an engineer sent out — can be named, given kinds, counted in the
diary, and later given a login that joins their existing row."""
import pytest

from app import db, people, people_routes
from tests.test_people import as_owner, as_staff  # noqa: F401 — fixtures

KRISTIE = {"id": "tp-k", "tenant_id": "t1", "name": "Kristie", "role": "owner",
           "user_id": None, "does": ["desk"], "phone": "07700 900123",
           "email": "kristie@example.com", "active": True}
SAM_ROW = {"id": "tp-s", "tenant_id": "t1", "name": "Sam", "role": "staff",
           "user_id": "u-sam", "does": [], "active": True}


@pytest.fixture
def team(monkeypatch):
    rows = {"tp-k": dict(KRISTIE), "tp-s": dict(SAM_ROW)}
    log = {"created": [], "updated": [], "capacity": 0}
    monkeypatch.setattr(db, "list_team_people", lambda tid: list(rows.values()))
    monkeypatch.setattr(db, "get_team_person", lambda tid, pid: rows.get(pid))
    monkeypatch.setattr(db, "create_team_person", lambda row: log["created"].append(row) or row)
    monkeypatch.setattr(db, "update_team_person",
                        lambda tid, pid, f: log["updated"].append((pid, f)))
    monkeypatch.setattr(people_routes, "_capacity_follows_people",
                        lambda ctx: log.__setitem__("capacity", log["capacity"] + 1))
    monkeypatch.setattr(people_routes, "_kinds", lambda ctx: [
        {"key": "desk", "label": "Front desk"}, {"key": "svc:meet & greet", "label": "Meet & greets"}])
    return rows, log


def test_a_team_row_reads_cleanly():
    t = people.team_person(KRISTIE)
    assert t["name"] == "Kristie" and t["role"] == "owner" and t["role_label"]
    assert t["has_login"] is False and t["does"] == ["desk"]
    assert people.team_person({"name": "  Jo  ", "role": "accountant"})["role"] == "staff"
    assert people.without_login([t, people.team_person(SAM_ROW)]) == [t]


def test_they_count_towards_capacity_like_anyone_who_signs_in():
    members = [people.member({"user_id": "u", "role": "staff", "scopes": {"does": ["desk"]}})]
    both = members + [people.as_worker(people.team_person(KRISTIE))]
    assert people.capacity_from_people(both, {})["front_desk"]["hands"] == 2
    gone = people.as_worker(people.team_person({**KRISTIE, "active": False}))
    assert people.capacity_from_people(members + [gone], {})["front_desk"]["hands"] == 1


def test_the_people_page_lists_them_apart_and_offers_to_add_one(as_owner, team):
    client, _ = as_owner
    html = client.get("/app/people").text
    assert "data-no-login" in html and "Freelancers &amp; helpers" in html
    assert 'href="/app/people/team/tp-k"' in html
    assert 'href="/app/people/team/tp-s"' not in html     # Sam signs in: listed with the members
    assert "Add a freelancer or helper" in html and 'action="/app/people/team/add"' in html


def test_adding_one_keeps_only_known_kinds_and_recounts(as_owner, team):
    client, _ = as_owner
    _, log = team
    r = client.post("/app/people/team/add", data={
        "name": "  Kristie  Paws ", "role": "owner", "kind": ["desk", "made-up"], "phone": "07700"})
    assert r.status_code == 303 and "msg=added" in r.headers["location"]
    row = log["created"][0]
    assert row["name"] == "Kristie Paws" and row["role"] == "owner" and row["does"] == ["desk"]
    assert "user_id" not in row                           # never a login from this form
    assert log["capacity"] == 1
    assert client.post("/app/people/team/add", data={"name": " "}).headers["location"].endswith("msg=noname")


def test_their_page_edits_them_and_a_login_person_is_sent_to_their_own(as_owner, team):
    client, _ = as_owner
    _, log = team
    html = client.get("/app/people/team/tp-k").text
    assert "data-team-person-page" in html and "Give them a login" in html
    assert client.get("/app/people/team/tp-s").headers["location"] == "/app/people/u-sam"
    r = client.post("/app/people/team/tp-k", data={"name": "Kristie", "role": "owner",
                                                    "kind": ["svc:meet & greet"], "active": "1"})
    assert r.headers["location"] == "/app/people/team/tp-k?msg=saved"
    pid, fields = log["updated"][0]
    assert pid == "tp-k" and fields["does"] == ["svc:meet & greet"] and fields["active"] is True


def test_giving_a_login_makes_an_invitation_for_that_person(as_owner, team):
    client, made = as_owner
    r = client.post("/app/people/team/tp-k/login")
    assert r.status_code == 303 and "msg=invited" in r.headers["location"]
    assert made["team_person_id"] == "tp-k" and made["display_name"] == "Kristie"
    assert made["role"] == "manager"                      # one owner signs in; a co-owner is a manager
    assert made["scopes"]["does"] == ["desk"] and made["email"] == "kristie@example.com"


def test_accepting_that_invitation_joins_the_login_to_the_row(monkeypatch):
    calls = []

    class Q:
        def __init__(self, name): self.name = name
        def __getattr__(self, attr):
            def f(*a, **k):
                calls.append((self.name, attr, a))
                return self
            return f
        def execute(self): return type("R", (), {"data": []})()

    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda self, n: Q(n)})())
    linked = []
    monkeypatch.setattr(db, "update_team_person", lambda tid, pid, f: linked.append((pid, f)))
    db.accept_member_invite({"id": "i1", "tenant_id": "t1", "role": "manager", "team_person_id": "tp-k"},
                            "u-new", "k@example.com", "2026-09-24T20:00:00Z")
    assert linked == [("tp-k", {"user_id": "u-new", "active": True})]
    # Any other invitation puts a new person on the team.
    added = []
    monkeypatch.setattr(db, "sync_team_person_for_member", lambda tid, uid, f: added.append((uid, f)))
    db.accept_member_invite({"id": "i2", "tenant_id": "t1", "role": "staff", "display_name": "Em",
                             "scopes": {"does": ["desk"]}}, "u-em", None, "2026-09-24T20:00:00Z")
    assert added[0][0] == "u-em" and added[0][1]["name"] == "Em" and added[0][1]["does"] == ["desk"]


def test_staff_cannot_add_or_open_them(as_staff, team):
    client = as_staff
    assert client.post("/app/people/team/add", data={"name": "X"}).headers["location"] == "/app/"
    assert client.get("/app/people/team/tp-k").headers["location"] == "/app/"
    assert team[1]["created"] == []
