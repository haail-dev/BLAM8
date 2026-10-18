"""Names on actions — who did it (People slice 4, the last of it).

"By-hand actions already log to the activity feed; they carry the person's
name." With more than one person on a business, "someone cancelled BK-1042" is
not a record, and record keeping is a third of what this product said it was
for (Kris 2026-09-13: "delegation, record keeping & quick sharing info").

What this file exists to hold:

  - a person's request stamps their name; BLAM8's own work stamps nothing, and
    NOTHING is ever guessed — an unattributed row stays unattributed;
  - the agent's work keeps no name even when a person pressed the button, and
    the name comes BACK afterwards, so an approve route that redrafts and then
    sends still records the send as theirs;
  - a context inherited by a background task can never put one business's
    person on another business's record;
  - the log can be read by person, and the filter cannot be used to probe for
    ids that are not this business's.

Run: python -m pytest tests/test_actor.py -q
"""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import actor
from app.auth import TenantCtx, require_onboarded
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
T1, T2 = "t1", "t2"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every test starts with nobody driving, and names resolve without a db."""
    actor.forget()
    monkeypatch.setattr(actor, "name_for", lambda t, u, email="": {"u1": "Em", "u2": "Sam"}.get(u, email))
    yield
    actor.forget()


# ── the stamp ──────────────────────────────────────────────────────────────
def test_blam8s_own_work_wears_nobodys_name():
    """Most of this feed is the agent: drafting, the nightly jobs, a webhook.
    An empty stamp is the honest answer, and far better than a guess."""
    assert actor.stamp(T1) == {}


def test_a_persons_request_stamps_their_name():
    actor.remember(T1, "u1", "em@example.com")
    assert actor.stamp(T1) == {"actor_user_id": "u1", "actor_name": "Em"}


def test_the_name_falls_back_to_what_they_signed_in_as(monkeypatch):
    monkeypatch.setattr(actor, "name_for", lambda t, u, email="": email)
    actor.remember(T1, "u9", "New@Person.com")
    assert actor.stamp(T1)["actor_name"] == "new@person.com"


def test_a_stamp_never_crosses_a_business():
    """A background task inherits the context of the request that spawned it.
    Without this check it would put one business's person on another's record."""
    actor.remember(T1, "u1", "em@example.com")
    assert actor.stamp(T2) == {}
    assert actor.stamp("") == {}


def test_nothing_is_remembered_without_both_halves():
    actor.remember(T1, None)
    assert actor.current() is None
    actor.remember("", "u1")
    assert actor.current() is None


# ── the agent's own work ───────────────────────────────────────────────────
def test_the_agent_keeps_no_name_and_hands_it_back():
    """Pressing "draft a reply" is a human act; the reply is still the agent's.
    But the approve route that redrafts THEN sends must record the send as
    theirs, so the name has to come back."""
    seen = {}

    @actor.agent_work
    async def draft():
        seen["during"] = actor.stamp(T1)
        return "drafted"

    actor.remember(T1, "u1", "em@example.com")
    assert asyncio.run(draft()) == "drafted"
    assert seen["during"] == {}
    assert actor.stamp(T1) == {"actor_user_id": "u1", "actor_name": "Em"}


def test_the_name_comes_back_even_when_the_agent_fails():
    @actor.agent_work
    async def boom():
        raise RuntimeError("model down")

    actor.remember(T1, "u1")
    with pytest.raises(RuntimeError):
        asyncio.run(boom())
    assert actor.stamp(T1)["actor_user_id"] == "u1"


def test_every_way_into_the_agent_is_marked():
    """A drafting path that forgot this would say "Em drafted a reply to Jo",
    which is a lie about the one thing this product has to be honest about."""
    src = _read("src/app/pipeline.py")
    for fn in ("draft_for_email", "redraft_email_log", "draft_for_message", "draft_for_latest"):
        assert f"@actor.agent_work\nasync def {fn}" in src, fn
    # The backlog import behind a mailbox connect is BLAM8 filing old mail.
    assert "@actor.agent_work\nasync def import_recent" in _read("src/app/backlog.py")


# ── the wiring ─────────────────────────────────────────────────────────────
def test_it_is_set_once_where_every_request_already_goes():
    src = _read("src/app/auth.py")
    assert "actor.remember(tenant_id, user[\"id\"], request.state.user_email)" in src
    # …and read once, in the single place that writes the record.
    db_src = _read("src/app/db.py")
    body = db_src.split("def log_agent_event")[1].split("\ndef ")[0]
    assert "_actor.stamp(tenant_id)" in body and "**who," in body


def test_the_writer_never_invents_an_actor():
    """stamp() returns {} rather than a None-valued pair, so an unattributed
    row keeps whatever the column already had instead of being overwritten."""
    assert actor.stamp(T1) == {}
    assert "actor_user_id" not in actor.stamp(T1)


def test_a_row_with_no_actor_says_nothing_at_all():
    """Rows written before this existed have no actor. Rendering "BLAM8" on
    them would be inventing a record that nobody kept."""
    assert actor.did_it({}) == ""
    assert actor.did_it(None) == ""
    assert actor.did_it({"actor_name": "  Em  "}) == "Em"
    html = _read("src/app/templates/app/agent_log.html")
    assert "{% if r.actor_name %}" in html and "data-actor" in html


# ── reading the log by person ──────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id=T1, client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _log_env(monkeypatch, members, rows=()):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_members", lambda *a, **k: list(members))
    seen = {}

    def fake(tenant_id, *, on_date=None, kind=None, q=None, who=None,
             limit=60, offset=0, client=None):
        seen["who"] = who
        return list(rows)
    monkeypatch.setattr(dashboard.db, "list_agent_events", fake)
    return seen


TWO = [{"user_id": "u1", "role": "owner", "display_name": "Em", "active": True},
       {"user_id": "u2", "role": "staff", "display_name": "Sam", "active": True}]


def test_a_one_person_business_is_not_asked_who_did_it(client, monkeypatch):
    """"Who did this" is noise when the answer can only be you."""
    _log_env(monkeypatch, TWO[:1])
    assert "data-who" not in client.get("/app/agent-log").text


def test_two_pairs_of_hands_get_the_filter(client, monkeypatch):
    seen = _log_env(monkeypatch, TWO)
    html = client.get("/app/agent-log?who=u2").text
    assert "data-who" in html and ">Sam<" in html
    assert "BLAM8 itself" in html            # …and what the agent did alone
    assert seen["who"] == "u2"


def test_the_filter_cannot_be_used_to_probe_for_other_ids(client, monkeypatch):
    """Checked against this business's own people rather than trusted from the
    query string."""
    seen = _log_env(monkeypatch, TWO)
    client.get("/app/agent-log?who=somebody-elses-uuid")
    assert seen["who"] is None
    client.get("/app/agent-log?who=blam8")
    assert seen["who"] == "blam8"


def test_the_who_filter_survives_paging(client, monkeypatch):
    _log_env(monkeypatch, TWO, rows=[{"id": "e1", "kind": "sent", "direction": "outbound",
                                      "summary": "Sent the reply", "created_at": "2026-09-14T09:00:00+00:00",
                                      "actor_user_id": "u2", "actor_name": "Sam", "detail": {}}])
    html = client.get("/app/agent-log?who=u2&page=1").text
    assert "who=u2" in html                  # the Newer/Older links carry it
    assert 'data-actor>Sam<' in html         # …and the row says who did it
