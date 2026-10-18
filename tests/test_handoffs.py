"""People slice 3 — telling a person something, fastened to the work.

Kris (2026-09-13), settling the scope: People is "work delegation and record
keeping & quick sharing info". This is the sharing half, and the research
(docs/research-people-tools-2026-09-13.md) says no competitor does it: the
breakpoint is the second crew, "I never saw that message" is the company motto,
and there is no record of who was told what.

Run: python -m pytest tests/test_handoffs.py -q
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db, handoffs
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the model ──────────────────────────────────────────────────────────────
def test_a_note_is_always_about_something():
    n = handoffs.build(body="  Ring them   before 4 ", from_user="u1", from_name="Kris",
                       to_user="u2", to_name="Em", about_kind="booking",
                       about_id="b-9", about_label="Bella, Wed 16 Sep")
    assert n["body"] == "Ring them before 4"          # collapsed, not mangled
    assert n["about_kind"] == "booking" and n["about_href"] == "/app/diary/b-9"
    assert n["about_label"] == "Bella, Wed 16 Sep"
    assert n["from_name"] == "Kris" and n["to_name"] == "Em"
    # Nothing to say is no note at all.
    assert handoffs.build(body="   ", from_user="u1", from_name="K", to_user="u2", to_name="E") is None
    # An unknown kind drops the whole attachment rather than storing a half one.
    loose = handoffs.build(body="x", from_user="u1", from_name="K", to_user="u2", to_name="E",
                           about_kind="nonsense", about_id="z", about_label="z")
    assert loose["about_kind"] is None and loose["about_id"] is None and loose["about_href"] is None


def test_every_kind_knows_its_own_door():
    assert handoffs.about_href("booking", "b-1") == "/app/diary/b-1"
    assert handoffs.about_href("contact", "sam@x.co") == "/app/crm/open?email=sam%40x.co"
    assert handoffs.about_href("thread", "sam@x.co").startswith("/app/conversations/view?contact=")
    assert handoffs.about_href("booking", "") == "" and handoffs.about_href("nope", "1") == ""


def test_what_state_the_reader_is_looking_at():
    now = datetime.now(timezone.utc).isoformat()
    assert handoffs.state({}) == "new" and handoffs.is_open({}) is True
    assert handoffs.state({"seen_at": now}) == "seen" and handoffs.is_open({"seen_at": now}) is True
    assert handoffs.state({"done_at": now}) == "done" and handoffs.is_open({"done_at": now}) is False
    assert handoffs.state({"cancelled_at": now}) == "cancelled"
    # Cancelled outranks everything — it was withdrawn, whatever else happened.
    assert handoffs.state({"seen_at": now, "done_at": now, "cancelled_at": now}) == "cancelled"
    rows = [{"id": "a", "created_at": "2026-09-13T09:00:00+00:00"},
            {"id": "b", "created_at": "2026-09-13T11:00:00+00:00", "done_at": now},
            {"id": "c", "created_at": "2026-09-13T10:00:00+00:00"}]
    assert [r["id"] for r in handoffs.for_deck(rows)] == ["c", "a"]     # newest open first
    assert [r["id"] for r in handoffs.unseen(rows)] == ["a", "c"]


# ── the routes ─────────────────────────────────────────────────────────────
def _member(uid, name, role="staff"):
    return {"user_id": uid, "role": role, "display_name": name, "email": f"{name.lower()}@x.co",
            "active": True, "scopes": {}}


@pytest.fixture
def owner(monkeypatch):
    from tests.test_home_dashboard import Env
    Env(monkeypatch, convos=[], modules={"scheduling"})
    made = {}
    monkeypatch.setattr(db, "list_members", lambda tid: [
        _member("u-own", "Kris", "owner"), _member("u-em", "Em")])
    monkeypatch.setattr(db, "get_member", lambda tid, uid: _member("u-own", "Kris", "owner")
                        if uid == "u-own" else _member("u-em", "Em"))
    monkeypatch.setattr(db, "create_staff_note", lambda row: made.update(row) or row)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_staff_notes_about", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u-own")
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False), made
    app.dependency_overrides.clear()
    dashboard.navcache.invalidate("tm:t1")


def test_telling_someone_stores_the_words_and_the_work(owner):
    client, made = owner
    r = client.post("/app/people/say", data={
        "to_user": "u-em", "body": "Ring them before 4 — they want a second dog.",
        "about_kind": "booking", "about_id": "b-9", "about_label": "Bella, Wed 16 Sep",
        "return_to": "/app/diary/b-9"})
    assert r.status_code == 303 and r.headers["location"] == "/app/diary/b-9?msg=said"
    assert made["tenant_id"] == "t1"
    assert made["from_user"] == "u-own" and made["from_name"] == "Kris"
    assert made["to_user"] == "u-em" and made["to_name"] == "Em"
    assert made["about_kind"] == "booking" and made["about_href"] == "/app/diary/b-9"
    assert "second dog" in made["body"]


def test_a_note_with_nobody_or_nothing_to_say_is_refused(owner):
    client, made = owner
    r = client.post("/app/people/say", data={"to_user": "u-em", "body": "  ",
                                             "return_to": "/app/diary/b-9"})
    assert r.headers["location"] == "/app/diary/b-9?msg=saynothing" and not made
    r = client.post("/app/people/say", data={"to_user": "nobody", "body": "hello",
                                             "return_to": "/app/diary/b-9"})
    assert r.headers["location"] == "/app/diary/b-9?msg=saynothing" and not made
    # An off-site return address is never honoured.
    client.post("/app/people/say", data={"to_user": "u-em", "body": "hi",
                                         "return_to": "https://evil.example/"})
    assert made["to_user"] == "u-em"


@pytest.fixture
def em(monkeypatch):
    from tests.test_home_dashboard import Env
    Env(monkeypatch, convos=[], modules={"scheduling"})
    saved = {}
    note = {"id": "n1", "tenant_id": "t1", "to_user": "u-em", "from_user": "u-own",
            "from_name": "Kris", "to_name": "Em", "body": "Ring them before 4",
            "about_kind": "booking", "about_id": "b-9", "about_label": "Bella, Wed 16 Sep",
            "about_href": "/app/diary/b-9", "created_at": "2026-09-13T09:00:00+00:00"}
    monkeypatch.setattr(db, "get_member", lambda tid, uid: _member("u-em", "Em"))
    monkeypatch.setattr(db, "list_members", lambda tid: [_member("u-own", "Kris", "owner"), _member("u-em", "Em")])
    monkeypatch.setattr(db, "get_staff_note", lambda tid, nid: dict(note) if nid == "n1" else None)
    monkeypatch.setattr(db, "update_staff_note", lambda tid, nid, fields: saved.update(fields))
    monkeypatch.setattr(db, "list_staff_notes_for", lambda tid, uid, **k: [dict(note)])
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    for name in ("list_invoices", "list_quotes", "list_pending_invoice_chases",
                 "crm_list_contacts", "crm_list_deals", "list_deck_notifications",
                 "list_unhealthy_connections", "list_requirement_groups"):
        monkeypatch.setattr(dashboard.db, name, lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    dashboard.navcache.invalidate("t:t1")
    dashboard.navcache.invalidate("sn:t1:u-em")
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u-em")
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False), saved
    app.dependency_overrides.clear()
    dashboard.navcache.invalidate("tm:t1")
    dashboard.navcache.invalidate("sn:t1:u-em")


def test_it_lands_on_their_today_with_a_way_in(em):
    client, _ = em
    html = client.get("/app/").text
    assert "data-my-notes" in html and 'data-note="n1"' in html
    assert "Kris" in html and "Ring them before 4" in html
    assert "about Bella, Wed 16 Sep" in html
    assert 'href="/app/diary/b-9"' in html and "Open it" in html
    assert "Seen it" in html and "Done" in html


def test_marking_it_seen_is_the_whole_point(em):
    client, saved = em
    r = client.post("/app/people/note/n1/seen", data={"return_to": "/app/"})
    assert r.status_code == 303 and r.headers["location"] == "/app/"
    assert "seen_at" in saved and saved["seen_at"]
    saved.clear()
    client.post("/app/people/note/n1/done", data={"return_to": "/app/"})
    assert saved.get("done_at") and saved.get("seen_at")      # done implies seen
    saved.clear()
    # It was asked OF her, so she cannot withdraw it.
    client.post("/app/people/note/n1/cancel", data={"return_to": "/app/"})
    assert saved == {}


def test_the_asker_can_withdraw_it(owner):
    client, _ = owner
    saved = {}
    import app.people_routes as pr
    note = {"id": "n1", "to_user": "u-em", "from_user": "u-own"}
    pr.db.get_staff_note = lambda tid, nid: dict(note)
    pr.db.update_staff_note = lambda tid, nid, fields: saved.update(fields)
    client.post("/app/people/note/n1/cancel", data={"return_to": "/app/"})
    assert saved.get("cancelled_at")


# ── the record ─────────────────────────────────────────────────────────────
def test_the_booking_keeps_what_was_said_about_it(owner, monkeypatch):
    client, _ = owner
    monkeypatch.setattr(db, "list_staff_notes_about", lambda tid, kind, aid: [
        {"from_name": "Kris", "to_name": "Em", "body": "Ring them before 4",
         "seen_at": "2026-09-13T10:00:00+00:00"}])
    ctx = TenantCtx(tenant_id="t1", client=None, user_id="u-own")
    out = dashboard._say_ctx(ctx, "booking", "b-9", "Bella")
    assert out["about_kind"] == "booking" and out["about_word"] == "booking"
    assert [m["display_name"] for m in out["say_people"]] == ["Kris", "Em"]
    assert out["about_notes"][0]["body"] == "Ring them before 4"
    part = _read("src/app/templates/app/_say_to.html")
    assert "data-say-to" in part and "data-about-notes" in part
    assert "not seen yet" in part and "It lands on their Today" in part
    # A one-person business never sees a control that cannot do anything —
    # and it is the OTHER active people who are anyone to tell (2026-09-20).
    assert "{% if say_others %}" in part and "{% for m in say_others %}" in part
    assert "Note for a colleague →" in part and "nothing goes to the customer" in part
    assert "Tell someone" not in part and "Tell them" not in part
    job = _read("src/app/templates/app/diary_job.html")
    assert '{% include "app/_say_to.html" %}' in job
    dash = _read("src/app/dashboard.py")
    assert '_say_ctx(ctx, "booking", booking_id,' in dash


def test_the_note_control_waits_for_a_second_person(owner, monkeypatch):
    """Kris 2026-09-20: "only show it once a second person has been added to
    the account" — and only the OTHERS, active, are anyone to tell."""
    rows = [{"user_id": "u-own", "role": "owner", "display_name": "Kris", "active": True},
            {"user_id": "u-em", "role": "staff", "display_name": "Em", "active": True},
            {"user_id": "u-old", "role": "staff", "display_name": "Old", "active": False}]
    monkeypatch.setattr(db, "list_members", lambda tid: [dict(r) for r in rows])
    monkeypatch.setattr(db, "list_staff_notes_about", lambda *a, **k: [])
    ctx = TenantCtx(tenant_id="t1", client=None, user_id="u-own")
    out = dashboard._say_ctx(ctx, "contact", "rob@x.co", "Rob")
    assert [m["display_name"] for m in out["say_others"]] == ["Em"]
    assert [m["display_name"] for m in out["say_people"]] == ["Kris", "Em", "Old"]
    # Alone on the account: nothing to show.
    monkeypatch.setattr(db, "list_members", lambda tid: [dict(rows[0])])
    assert dashboard._say_ctx(ctx, "contact", "rob@x.co", "Rob")["say_others"] == []
    # A second person who has been deactivated is nobody to tell either.
    monkeypatch.setattr(db, "list_members", lambda tid: [dict(rows[0]), dict(rows[2])])
    assert dashboard._say_ctx(ctx, "contact", "rob@x.co", "Rob")["say_others"] == []
    # …and from the other chair, the owner is the one to tell.
    em = TenantCtx(tenant_id="t1", client=None, user_id="u-em")
    monkeypatch.setattr(db, "list_members", lambda tid: [dict(r) for r in rows])
    assert [m["display_name"] for m in dashboard._say_ctx(em, "contact", "rob@x.co", "Rob")["say_others"]] == ["Kris"]


def test_a_read_failure_costs_the_control_never_the_page(owner, monkeypatch):
    client, _ = owner
    monkeypatch.setattr(db, "list_members", lambda tid: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(db, "list_staff_notes_about", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = dashboard._say_ctx(TenantCtx(tenant_id="t1", client=None, user_id="u-own"),
                             "booking", "b-9", "Bella")
    assert out["say_people"] == [] and out["about_notes"] == []


# ── the owner's half: what's out, and what each person is holding ──────────
def test_the_owner_sees_what_they_have_asked_for(owner, monkeypatch):
    client, _ = owner
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(db, "list_staff_notes_sent", lambda tid, uid, **k: [
        {"id": "n1", "to_name": "Em", "body": "Ring them before 4",
         "about_label": "Bella, Wed 16 Sep", "created_at": now},
        {"id": "n2", "to_name": "Em", "body": "Already handled", "done_at": now, "created_at": now},
        {"id": "n3", "to_name": "Em", "body": "Seen but not done", "seen_at": now, "created_at": now}])
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [])
    html = client.get("/app/people").text
    assert "data-out-notes" in html and "You've asked for" in html
    assert "Ring them before 4" in html and "not seen yet" in html
    assert "Seen but not done" in html and ">seen<" in html
    assert "Already handled" not in html          # answered notes leave the list
    assert "Drop it" in html                      # the asker can withdraw it


def test_a_persons_page_says_what_they_are_holding(owner, monkeypatch):
    client, _ = owner
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(db, "list_staff_notes_for", lambda tid, uid, **k: [
        {"id": "n1", "body": "Ring them before 4", "about_label": "Bella", "created_at": now},
        {"id": "n2", "body": "Done one", "done_at": now, "created_at": now}])
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [])
    html = client.get("/app/people/u-em").text
    assert "data-asked" in html and "Asked of them" in html
    assert "Ring them before 4" in html and "Bella" in html
    assert "Done one" not in html


def test_the_contact_card_carries_the_control_but_the_thread_does_not():
    """The thread stage owns the viewport and never scrolls, so the fold would
    fight the layout — and the contact card, which has it, is one tap away."""
    card = _read("src/app/templates/app/crm_contact.html")
    assert '{% include "app/_say_to.html" %}' in card
    assert "Note left — it's on their Today." in card
    dash = _read("src/app/dashboard.py")
    assert '_say_ctx(ctx, "contact", contact.get("email") or "",' in dash
    assert 'No "Note for a colleague" here on purpose' in dash   # the thread, and why
    thread = _read("src/app/templates/app/conversation.html")
    assert "_say_to.html" not in thread


def test_a_sent_notes_read_failure_costs_the_list_never_the_page(owner, monkeypatch):
    client, _ = owner
    monkeypatch.setattr(db, "list_staff_notes_sent",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [])
    r = client.get("/app/people")
    assert r.status_code == 200 and "data-out-notes" not in r.text
