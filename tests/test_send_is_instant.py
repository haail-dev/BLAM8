"""The thumbs-up answers the moment the email is out (Kris 2026-09-22).

"When i click the thumbs up to send a draft, it takes a good 5s for the
reaction/send. The draft is already written, it should just send instantly on
click?"

Measured on his own sends that afternoon: after the email left, the route still
awaited crm_intel (5.3-9.3s) and crm_action_resolve (3.0-3.5s) — both model
calls — plus the Google Calendar push, in series, before it redirected. None of
them changes the email.

Two things are pinned here, because detaching work is the easiest way there is
to lose it without anyone noticing:

  * the redirect does not wait for the bookkeeping; and
  * the bookkeeping still runs, every step of it, even when one step fails.

Run: python -m pytest tests/test_send_is_instant.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

BOOKING = {"id": "b-1", "booking_ref": "BK-1055", "booking_date": "2026-10-19",
           "summary": "Boarding for Mabel", "status": "confirmed"}


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _wire(monkeypatch, ran: list, detached: list):
    entry = {"id": "log-1", "from_address": "daniel@x.co", "subject": "Mabel",
             "draft_reply": "Booked in.", "kind": "reminder", "mailbox_id": None,
             "source_message_id": None}

    async def _ok(*a, **k):
        return None

    async def _slow(name):
        async def f(*a, **k):
            ran.append(name)
        return f

    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "set_email_log_review_seconds", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: 1)
    monkeypatch.setattr(dashboard.db, "bookings_for_log", lambda *a, **k: [BOOKING])
    monkeypatch.setattr(dashboard.mailbox, "send_mail", _ok)
    monkeypatch.setattr(dashboard.mailbox, "send_reply", _ok)
    monkeypatch.setattr(dashboard.crm, "note_booking_activity",
                        lambda *a, **k: ran.append("note_booking_activity"))
    monkeypatch.setattr(dashboard.crm, "note_outbound_sent",
                        lambda *a, **k: ran.append("note_outbound_sent"))
    monkeypatch.setattr(dashboard.crm_intel, "resolve_from_action",
                        asyncio.run(_slow("resolve_from_action")))
    monkeypatch.setattr(dashboard.crm_intel, "note_outbound", asyncio.run(_slow("note_outbound")))
    monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", asyncio.run(_slow("push_bookings")))
    # Capture the detached work instead of scheduling it, so the test can see
    # exactly what the route handed off and what it did inline.
    monkeypatch.setattr(dashboard, "_detach", lambda coro: detached.append(coro))


def test_the_redirect_does_not_wait_for_the_bookkeeping(client, monkeypatch):
    ran, detached = [], []
    _wire(monkeypatch, ran, detached)
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Booked in.", "return_to": "/app/conversations"})
    assert r.status_code == 303

    # the fast, true-now parts happened before the page answered...
    assert ran == ["note_booking_activity"]
    # ...and not one of the slow ones did: they were handed off, once
    assert "resolve_from_action" not in ran and "note_outbound" not in ran
    assert "push_bookings" not in ran
    assert len(detached) == 1
    assert detached[0].cr_code.co_name == "_after_send_bookkeeping"
    detached[0].close()                    # never scheduled here; don't leak it


def test_the_handed_off_work_still_runs_every_step():
    """Detaching must not mean dropping. All three still happen, in order."""
    ran = []

    async def rec(name, *a, **k):
        ran.append(name)

    orig = (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
            dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound)
    try:
        dashboard.crm_intel.resolve_from_action = lambda *a, **k: rec("resolve_from_action")
        dashboard.calendar_sync.push_bookings = lambda *a, **k: rec("push_bookings")
        dashboard.crm.note_outbound_sent = lambda *a, **k: ran.append("note_outbound_sent")
        dashboard.crm_intel.note_outbound = lambda *a, **k: rec("note_outbound")
        asyncio.run(dashboard._after_send_bookkeeping("t1", "log-1", "daniel@x.co", [BOOKING]))
    finally:
        (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
         dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound) = orig
    assert ran == ["resolve_from_action", "push_bookings", "note_outbound_sent", "note_outbound"]


def test_one_failing_step_never_costs_the_others():
    """Each step keeps its own try/except, as it did inline. A model call that
    errors must not cost the booking its calendar entry."""
    ran = []

    async def boom(*a, **k):
        raise RuntimeError("model down")

    async def rec(name, *a, **k):
        ran.append(name)

    orig = (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
            dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound)
    try:
        dashboard.crm_intel.resolve_from_action = boom
        dashboard.calendar_sync.push_bookings = lambda *a, **k: rec("push_bookings")
        dashboard.crm.note_outbound_sent = lambda *a, **k: ran.append("note_outbound_sent")
        dashboard.crm_intel.note_outbound = lambda *a, **k: rec("note_outbound")
        asyncio.run(dashboard._after_send_bookkeeping("t1", "log-1", "daniel@x.co", [BOOKING]))
    finally:
        (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
         dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound) = orig
    assert ran == ["push_bookings", "note_outbound_sent", "note_outbound"]


def test_no_confirmed_booking_skips_only_the_promise_pass():
    ran = []

    async def rec(name, *a, **k):
        ran.append(name)

    orig = (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
            dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound)
    try:
        dashboard.crm_intel.resolve_from_action = lambda *a, **k: rec("resolve_from_action")
        dashboard.calendar_sync.push_bookings = lambda *a, **k: rec("push_bookings")
        dashboard.crm.note_outbound_sent = lambda *a, **k: ran.append("note_outbound_sent")
        dashboard.crm_intel.note_outbound = lambda *a, **k: rec("note_outbound")
        asyncio.run(dashboard._after_send_bookkeeping("t1", "log-1", "daniel@x.co", []))
    finally:
        (dashboard.crm_intel.resolve_from_action, dashboard.calendar_sync.push_bookings,
         dashboard.crm.note_outbound_sent, dashboard.crm_intel.note_outbound) = orig
    assert ran == ["push_bookings", "note_outbound_sent", "note_outbound"]
