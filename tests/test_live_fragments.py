"""Live-inbox fragments (src/app/dashboard.py + _conversation_list/_thread partials).

Route-level via TestClient (auth overridden, db monkeypatched — the house
pattern): the list fragment renders rows + pills without page chrome, the
"AI writing" typing indicator appears for in-flight drafts (on a matching row,
and as a standalone top row for a brand-new sender), the thread fragment shows
the typing bubble for its contact only, and the nav badge fragment counts
pending drafts. The full pages keep rendering through the same partials.

Run: python -m pytest tests/test_live_fragments.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import events
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    events._drafting.clear()
    yield
    events._subscribers.clear()
    events._drafting.clear()


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


CONVOS = [
    {"contact": "known@x.com", "state": "pending", "subject": "MOT", "mailbox_id": None,
     "last_at": "2026-07-02T09:00:00+00:00", "thread_state": "in_progress"},
]


def _env(monkeypatch, convos=CONVOS, pending=()):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: list(pending))
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": []})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [])


# ── the conversations list fragment ───────────────────────────────────────────
def test_list_fragment_renders_rows_and_pills_without_page_chrome(client, monkeypatch):
    _env(monkeypatch)
    r = client.get("/app/conversations/fragment")
    assert r.status_code == 200
    assert 'id="convo-list"' in r.text and 'id="convo-pills"' in r.text
    assert "known@x.com" in r.text
    assert "<html" not in r.text                     # a fragment, not a page


def test_in_flight_draft_marks_the_matching_row(client, monkeypatch):
    _env(monkeypatch)
    events.begin_drafting("t1", "m1", {"from_address": "known@x.com", "subject": "MOT"})
    r = client.get("/app/conversations/fragment")
    assert "AI writing" in r.text and "typing-dots" in r.text


def test_brand_new_sender_gets_a_standalone_drafting_row(client, monkeypatch):
    _env(monkeypatch)
    events.begin_drafting("t1", "m2", {"from_address": "new@y.com", "from_name": "Newbie",
                                       "subject": "First enquiry"})
    r = client.get("/app/conversations/fragment")
    assert "Newbie" in r.text and "First enquiry" in r.text and "AI writing" in r.text


def test_quiet_inbox_fragment_shows_no_indicator(client, monkeypatch):
    _env(monkeypatch)
    r = client.get("/app/conversations/fragment")
    assert "AI writing" not in r.text


def test_full_page_still_renders_through_the_partial(client, monkeypatch):
    _env(monkeypatch)
    r = client.get("/app/conversations")
    assert r.status_code == 200
    assert 'id="convo-list"' in r.text and "known@x.com" in r.text
    assert "<html" in r.text                          # the full page, chrome and all


# ── the thread fragment ───────────────────────────────────────────────────────
def test_thread_fragment_shows_typing_bubble_for_its_contact_only(client, monkeypatch):
    _env(monkeypatch)
    events.begin_drafting("t1", "m1", {"from_address": "Known@X.com", "subject": "MOT"})
    mine = client.get("/app/conversations/view/fragment", params={"contact": "known@x.com"})
    assert mine.status_code == 200
    assert 'id="thread-typing"' in mine.text          # case-insensitive contact match
    other = client.get("/app/conversations/view/fragment", params={"contact": "other@z.com"})
    assert 'id="thread-typing"' not in other.text


def test_drafting_fragment_births_the_ghost_not_a_ticker(client, monkeypatch):
    # Kris 2026-08-31: the toast is JUST the toast ("Agent writing reply" +
    # dots, bottom-right of the dock); the streamed tool-use steps build up as
    # fresh ghost text centre-stage (#stage-ghost-live, filled by live.js) —
    # and the previous exchange's stale ghost stands down while drafting.
    _env(monkeypatch)
    events.begin_drafting("t1", "m1", {"from_address": "known@x.com", "subject": "MOT"})
    r = client.get("/app/conversations/view/fragment", params={"contact": "known@x.com"})
    assert 'id="stage-ghost-live"' in r.text
    assert 'id="drafting-steps"' not in r.text        # the ticker left the toast
    assert 'id="stage-decision"' not in r.text        # stale ghost stands down
    quiet = client.get("/app/conversations/view/fragment", params={"contact": "other@z.com"})
    assert 'id="stage-ghost-live"' not in quiet.text  # only while drafting


def test_thread_fragment_shows_the_inbound_being_answered_during_drafting(client, monkeypatch):
    """The email being replied to exists in no table mid-draft — the fragment
    must still show it above the typing bubble, or the thread reads as
    'replying before the message arrived' (live finding 2026-07-31)."""
    _env(monkeypatch)
    events.begin_drafting("t1", "m1", {"from_address": "known@x.com", "subject": "MOT",
                                       "body": "Can you fit me in on Friday?"})
    r = client.get("/app/conversations/view/fragment", params={"contact": "known@x.com"})
    assert 'data-draft-key="m1"' in r.text
    assert "Can you fit me in on Friday?" in r.text and "MOT" in r.text
    assert 'id="thread-typing"' in r.text             # the bubble still follows


def test_thread_fragment_drops_the_provisional_bubble_once_the_row_landed(client, monkeypatch):
    """Insert→end_drafting race: the row is stored but the registry entry hasn't
    cleared yet — the real row wins, no doubled inbound."""
    _env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [
        {"id": "e1", "from_address": "known@x.com", "subject": "MOT",
         "body_in": "Can you fit me in on Friday?", "draft_reply": "Yes!",
         "status": "pending", "created_at": "2026-07-31T09:00:00+00:00",
         "source_message_id": "m1"}])
    events.begin_drafting("t1", "m1", {"from_address": "known@x.com", "subject": "MOT",
                                       "body": "Can you fit me in on Friday?"})
    r = client.get("/app/conversations/view/fragment", params={"contact": "known@x.com"})
    assert 'data-draft-key="m1"' not in r.text        # provisional suppressed
    # Duet stage (2026-08-31): the newest inbound renders exactly twice — once
    # as the stage's top-left bubble, once in the history layer's full pair —
    # and never a third time from the provisional registry entry.
    assert r.text.count("Can you fit me in on Friday?") == 2


def test_thread_page_carries_the_live_swap_container(client, monkeypatch):
    _env(monkeypatch)
    r = client.get("/app/conversations/view", params={"contact": "known@x.com"})
    assert r.status_code == 200
    assert 'id="thread-live"' in r.text
    assert "/app/conversations/view/fragment?contact=known%40x.com" in r.text


# ── the nav badge fragment ────────────────────────────────────────────────────
def test_nav_badge_counts_pending_drafts(client, monkeypatch):
    _env(monkeypatch, pending=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    r = client.get("/app/nav/badge")
    assert r.status_code == 200 and ">3</span>" in r.text


def test_nav_badge_is_empty_when_nothing_pends(client, monkeypatch):
    _env(monkeypatch)
    assert client.get("/app/nav/badge").text == ""
