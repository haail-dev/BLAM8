"""Opening a New sender's thread must not wait on the draft.

Kris, 2026-09-21, filming: "I want to be able to open the thread while the Agent
is Writing the reply, it shouldn't block people." The click used to hold the whole
live draft — twenty-odd seconds of a dead row — and bounce back to the list with a
notice when the draft declined. It now redirects to the thread at once and leaves
the draft running behind it; the thread already paints the inbound and the "Agent
writing reply" indicator from events.drafting_now.

Run: python -m pytest tests/test_open_conversation_nonblocking.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import events
from app.auth import TenantCtx, require_onboarded
from app.main import app

CONTACT = "hannah@example.com"
MSG = "msg-1"


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_drafting():
    yield
    for d in events.drafting_now("t1"):
        events.end_drafting("t1", d["key"])


def _env(monkeypatch, *, trial_ok=True):
    async def get_message(*a, **k):
        return {"from": {"address": CONTACT, "name": "Hannah Price"},
                "subject": "A visit", "body": "Could I arrange a visit?"}
    monkeypatch.setattr(dashboard.mailbox, "get_message", get_message)
    monkeypatch.setattr(dashboard.db, "get_email_log_by_message", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard, "_selected_mailbox_id", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.trial, "gate_allows", lambda *a, **k: trial_ok)
    monkeypatch.setattr(dashboard.trial, "evaluate", lambda *a, **k: {"state": "expired"})


def test_the_thread_opens_before_the_draft_finishes(client, monkeypatch):
    _env(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_draft(tenant_id, message_id, mailbox_id=None, **kwargs):
        started.set()
        await release.wait()          # a draft that never returns during the request
        return "log-1"

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _slow_draft)
    r = client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
                   follow_redirects=False)
    assert r.status_code == 303
    # The thread, not the list-with-a-notice: reading is not gated on writing.
    assert "/app/conversations/view" in r.headers["location"]
    assert CONTACT.replace("@", "%40") in r.headers["location"]
    assert "notice=nodraft" not in r.headers["location"]
    release.set()


def test_the_thread_it_opens_already_shows_the_draft_in_flight(client, monkeypatch):
    """The redirect is only useful if the thread has something to paint, so the
    route registers the in-flight entry itself rather than waiting for the
    pipeline to reach its own begin_drafting several awaits later."""
    _env(monkeypatch)
    registered = []
    monkeypatch.setattr(dashboard.events, "begin_drafting",
                        lambda tid, key, info: registered.append((tid, key, info)))

    async def _slow_draft(tenant_id, message_id, mailbox_id=None, **kwargs):
        return "log-1"

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _slow_draft)
    client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
               follow_redirects=False)
    assert len(registered) == 1
    tid, key, info = registered[0]
    # Keyed the way the pipeline keys its own entry, so the richer one replaces
    # this placeholder rather than showing the customer twice.
    assert (tid, key) == ("t1", MSG)
    assert info["from_address"] == CONTACT


def test_a_declined_draft_clears_the_indicator_instead_of_spinning(client, monkeypatch):
    """draft_for_message returns None for a message that has gone — the bubble
    must still go away, or the thread types forever."""
    _env(monkeypatch)

    async def _declined(tenant_id, message_id, mailbox_id=None, **kwargs):
        return None

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _declined)
    client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
               follow_redirects=False)
    assert events.drafting_now("t1") == []


def test_a_draft_that_raises_clears_the_indicator_too(client, monkeypatch):
    _env(monkeypatch)

    async def _boom(tenant_id, message_id, mailbox_id=None, **kwargs):
        raise RuntimeError("mailbox unreachable")

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _boom)
    r = client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
                   follow_redirects=False)
    assert r.status_code == 303                      # the reader still gets their thread
    assert events.drafting_now("t1") == []


def test_a_second_click_does_not_start_a_second_draft(client, monkeypatch):
    """The impatient second tap, with the first draft still in flight: no new
    entry, no second draft — and so nothing that could clear the first click's
    bubble when it returns early on the pipeline's idempotency claim."""
    _env(monkeypatch)
    monkeypatch.setattr(dashboard.events, "drafting_now",
                        lambda tid: [{"key": MSG, "from_address": CONTACT}])
    registered, drafted = [], []
    monkeypatch.setattr(dashboard.events, "begin_drafting",
                        lambda *a, **k: registered.append(a))

    async def _draft(tenant_id, message_id, mailbox_id=None, **kwargs):
        drafted.append(message_id)
        return "log-1"

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _draft)
    r = client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
                   follow_redirects=False)
    assert r.status_code == 303
    assert "/app/conversations/view" in r.headers["location"]   # still opens
    assert registered == [] and drafted == []


def test_the_trial_gate_still_routes_to_billing_rather_than_a_thread(client, monkeypatch):
    """The one outcome that must stay on the request: detached, the exception
    would be raised inside a background task, the app's TrialLimitReached
    handler would never see it, and the owner would sit on a thread that never
    drafts instead of being shown the billing CTA."""
    _env(monkeypatch, trial_ok=False)

    async def _never(tenant_id, message_id, mailbox_id=None, **kwargs):
        raise AssertionError("no draft should be attempted past the trial gate")

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _never)
    r = client.get(f"/app/conversations/open?message_id={MSG}&contact={CONTACT}",
                   follow_redirects=False)
    assert "/app/conversations/view" not in r.headers.get("location", "")
    assert events.drafting_now("t1") == []


def test_a_link_without_an_address_keeps_the_old_blocking_path(client, monkeypatch):
    """An old bookmark has nothing to open until the drafted row names the contact."""
    _env(monkeypatch)

    async def _draft(tenant_id, message_id, mailbox_id=None, **kwargs):
        return None

    monkeypatch.setattr(dashboard.pipeline, "draft_for_message", _draft)
    r = client.get(f"/app/conversations/open?message_id={MSG}", follow_redirects=False)
    assert r.status_code == 303
    assert "notice=nodraft" in r.headers["location"]


# ── The live "AI writing" card above the month folds ─────────────────────────
def _page_env(monkeypatch):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)
    monkeypatch.setattr(dashboard.events, "drafting_now",
                        lambda tid: [{"key": MSG, "from_address": CONTACT,
                                      "from_name": "Hannah Price", "subject": "Boarding",
                                      "body": "Hi", "mailbox_id": None}])


def test_the_ai_writing_card_is_a_link_to_the_thread(client, monkeypatch):
    """A sender with no logged conversation yet is lifted above the month folds
    while the AI writes. It used to be a dead <div>: the one row you most want
    to watch was the one row you could not open (Kris 2026-09-21). Opening it
    starts nothing — the draft this card is rendered from is already running."""
    _page_env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "AI writing" in html
    href = '/app/conversations/view?contact=' + CONTACT.replace("@", "%40")
    assert href in html


def test_the_ai_writing_card_is_not_a_swipe_surface(client, monkeypatch):
    """It must not wear .convo-row: that is the swipe surface, and a swipe here
    would commit Ignore & Remove with no bin under it, or open a CRM card for a
    contact that does not exist yet."""
    _page_env(monkeypatch)
    html = client.get("/app/conversations").text
    card = html[html.index("AI writing") - 700:html.index("AI writing")]
    assert "convo-draft-row" in card
    assert "convo-row" not in card.replace("convo-draft-row", "")
