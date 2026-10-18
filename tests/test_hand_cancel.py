"""A booking cancelled by hand in the diary tells the customer (run #8, 2026-09-05,
benchmark stake I): the note is composed into the approval queue with the card
and the push; a released hold, a declined proposal or a booking with no contact
earns nothing; the diary's status route calls it. Run: python -m pytest
tests/test_hand_cancel.py -q
"""
import asyncio
from types import SimpleNamespace

from app import hand_cancel

BOOKING = {"id": "b-1", "booking_ref": "BK-1003", "booking_date": "2026-09-26", "start_block": 40,
           "length_blocks": 4, "status": "confirmed", "contact": "kris@x.co", "summary": "1-hour lesson — Kris",
           "details": {"Service": "1-hour lesson"}, "thread_id": "t-1", "mailbox_id": None}


def test_the_facts_name_the_booking_in_the_businesss_word():
    f = hand_cancel.facts_for(BOOKING, "Kris", {"booking": "lesson", "participant": "pupil"})
    assert f.startswith("The business has cancelled the customer's lesson BK-1003: 1-hour lesson on Saturday 26th Sep 26 at 10:00.")
    assert "Invite them to reply for another time" in f and "The message is to Kris." in f
    assert "reason" not in f.lower()
    with_reason = hand_cancel.facts_for(BOOKING, None, None, reason="the car is off the road")
    assert "The reason to give: the car is off the road." in with_reason and "booking BK-1003" in with_reason


def _env(monkeypatch):
    seen = {"queued": [], "events": [], "push": []}
    monkeypatch.setattr(hand_cancel.db, "crm_contact_by_email", lambda t, e, client=None: {"name": "Kris", "email": e})
    monkeypatch.setattr(hand_cancel.db, "effective_config", lambda t, m, client=None: {"sign_off": "Best, Jo"})
    monkeypatch.setattr(hand_cancel.db, "get_scheduling_config", lambda t, client=None: {"nouns": {"booking": "lesson"}})
    monkeypatch.setattr(hand_cancel.db, "insert_email_log",
                        lambda t, to, subject, body, draft, **kw: seen["queued"].append((to, subject, draft, kw)) or ("log-9", True))
    monkeypatch.setattr(hand_cancel.db, "log_agent_event", lambda t, kind, summary, **kw: seen["events"].append((kind, summary)))
    from app import agent, push, usage
    async def compose(cfg, purpose, facts, **kw):
        seen["facts"] = facts
        return SimpleNamespace(text="Hi Kris, sorry — your lesson BK-1003 on Saturday 26 September at 10:00 is cancelled. Reply and we'll find another time.", model="m", usage=None)
    monkeypatch.setattr(agent, "compose_outbound", compose)
    monkeypatch.setattr(push, "notify", lambda t, **kw: seen["push"].append(kw))
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    return seen


def test_a_confirmed_booking_with_a_contact_queues_the_note(monkeypatch):
    seen = _env(monkeypatch)
    out = asyncio.run(hand_cancel.on_hand_cancel("t1", BOOKING))
    assert out["queued"] is True and out["log_id"] == "log-9"
    to, subject, draft, kw = seen["queued"][0]
    assert to == "kris@x.co" and subject == "BK-1003 cancelled — lesson"
    assert kw["status"] == "pending" and kw["kind"] == "hand_cancel" and kw["thread_id"] == "t-1"
    assert "cancelled the customer's lesson BK-1003" in seen["facts"]
    assert seen["events"][0][0] == "hand_cancel" and "waiting for approval" in seen["events"][0][1]
    assert seen["push"][0]["kind"] == "hand_cancel" and seen["push"][0]["contact"] == "kris@x.co"


def test_nothing_for_a_hold_a_decline_or_no_contact(monkeypatch):
    seen = _env(monkeypatch)
    for b in ({**BOOKING, "status": "proposed"}, {**BOOKING, "status": "offered"}, {**BOOKING, "contact": None}):
        out = asyncio.run(hand_cancel.on_hand_cancel("t1", b))
        assert out["queued"] is False
    assert seen["queued"] == [] and seen["push"] == []


def test_a_compose_failure_never_raises(monkeypatch):
    seen = _env(monkeypatch)
    from app import agent
    async def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(agent, "compose_outbound", boom)
    out = asyncio.run(hand_cancel.on_hand_cancel("t1", BOOKING))
    assert out == {"ok": False, "error": "compose_failed"} and seen["queued"] == []


def test_the_diary_status_route_tells_the_customer_on_a_hand_cancel(monkeypatch):
    from fastapi.testclient import TestClient
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    told = []
    try:
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
        monkeypatch.setattr(dashboard.db, "get_booking", lambda *a, **k: dict(BOOKING))
        monkeypatch.setattr(dashboard.db, "set_booking_status", lambda *a, **k: None)
        monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)
        monkeypatch.setattr(dashboard.events, "publish", lambda *a, **k: None)
        async def remove(*a, **k): pass
        monkeypatch.setattr(dashboard.calendar_sync, "remove_pushed_event", remove)
        from app import courses
        async def freed(*a, **k): return []
        monkeypatch.setattr(courses, "on_place_freed", freed)
        async def tell(t, booking, **k):
            told.append(booking["booking_ref"]); return {"ok": True, "queued": True}
        monkeypatch.setattr(hand_cancel, "on_hand_cancel", tell)
        r = TestClient(app).post("/app/diary/b-1/status", data={"status": "cancelled"}, follow_redirects=False)
        assert r.status_code == 303 and told == ["BK-1003"]
        # confirming, or cancelling a proposed hold, tells nobody
        monkeypatch.setattr(dashboard.db, "get_booking", lambda *a, **k: {**BOOKING, "status": "proposed"})
        TestClient(app).post("/app/diary/b-1/status", data={"status": "cancelled"}, follow_redirects=False)
        assert told == ["BK-1003"]
    finally:
        app.dependency_overrides.clear()
