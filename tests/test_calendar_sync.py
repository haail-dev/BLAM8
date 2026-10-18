"""Calendar Connect (v1.1, Outlook first) — the pure sync planner.

Continuous busy-sync: the tenant's calendar events become source='calendar'
busy-blocks that consume diary capacity through the normal overlap math.
plan_calendar_sync() is a pure reconciler — events + existing linked rows in,
{add, cancel, update-free} plan out. Rules pinned here:

  - an event maps to every window it overlaps (event_blocks): slots it spans,
    am/pm halves it touches, whole-day for all-day events; closed days skipped
  - events BLAM8 itself pushed (calendar_event_id on a non-calendar booking)
    are never re-imported
  - a block the tenant cancelled in the Diary is a TOMBSTONE — the sync never
    resurrects that event
  - an event that moved gets its stale blocks cancelled and new ones added;
    a deleted event gets its blocks cancelled

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app.scheduling import DIARY_TZ, event_blocks, plan_calendar_sync

NOW = datetime(2026, 6, 16, 8, 0, tzinfo=DIARY_TZ)  # Tuesday

SLOTS = carried_config({"windows": "slots", "open_time": "09:00", "close_time": "17:00",
         "slot_minutes": 60, "capacity_per_window": 1})


def ev(eid="ev1", start=(17, 14, 0), end=(17, 15, 0), all_day=False, subject="Team sync"):
    return {"id": eid, "subject": subject, "all_day": all_day,
            "start": datetime(2026, 6, start[0], start[1], start[2], tzinfo=DIARY_TZ),
            "end": datetime(2026, 6, end[0], end[1], end[2], tzinfo=DIARY_TZ)}


def row(rid, eid, d, w, status="confirmed", source="calendar"):
    return {"id": rid, "calendar_event_id": eid, "booking_date": d, "window": w,
            "status": status, "source": source}


# ── event → windows mapping ───────────────────────────────────────────────────
def test_event_blocks_slots_span():
    assert event_blocks(SLOTS, ev(start=(17, 14, 0), end=(17, 15, 0)), NOW) == [("2026-06-17", "14:00")]
    assert event_blocks(SLOTS, ev(start=(17, 14, 30), end=(17, 16, 30)), NOW) == [
        ("2026-06-17", "14:00"), ("2026-06-17", "15:00"), ("2026-06-17", "16:00")]


def test_event_blocks_all_day_and_modes():
    assert event_blocks(SLOTS, ev(all_day=True), NOW) == [("2026-06-17", "day")]
    assert event_blocks(carried_config({}), ev(), NOW) == [("2026-06-17", "day")]  # day mode: any event blocks the day
    ampm = carried_config({"windows": "ampm"})
    assert event_blocks(ampm, ev(start=(17, 10, 0), end=(17, 11, 0)), NOW) == [("2026-06-17", "am")]
    assert event_blocks(ampm, ev(start=(17, 11, 30), end=(17, 13, 0)), NOW) == [
        ("2026-06-17", "am"), ("2026-06-17", "pm")]


def test_event_blocks_skips_closed_days_and_outside_horizon():
    assert event_blocks(SLOTS, ev(start=(20, 10, 0), end=(20, 11, 0)), NOW) == []   # Saturday
    assert event_blocks(SLOTS, ev(start=(16, 6, 0), end=(16, 7, 0)), NOW) == []     # before opening
    far = {"id": "x", "subject": "s", "all_day": False,
           "start": datetime(2026, 9, 1, 10, 0, tzinfo=DIARY_TZ),
           "end": datetime(2026, 9, 1, 11, 0, tzinfo=DIARY_TZ)}
    assert event_blocks(SLOTS, far, NOW) == []                                       # beyond horizon


# ── the reconcile plan ────────────────────────────────────────────────────────
def test_plan_adds_new_events_with_summaries():
    plan = plan_calendar_sync(SLOTS, [], [ev()], NOW)
    assert plan["add"] == [{"event_id": "ev1", "summary": "Team sync",
                            "date": "2026-06-17", "window": "14:00"}]
    assert plan["cancel"] == []


EMPTY = {"add": [], "cancel": [], "flag_missing": [], "unflag_missing": []}


def test_plan_skips_events_blam8_pushed_and_already_imported():
    pushed = row("b1", "ev1", "2026-06-17", "14:00", source="email")
    assert plan_calendar_sync(SLOTS, [pushed], [ev()], NOW) == EMPTY
    imported = row("b2", "ev1", "2026-06-17", "14:00")
    assert plan_calendar_sync(SLOTS, [imported], [ev()], NOW) == EMPTY


def test_plan_tombstone_never_resurrects():
    dead = row("b3", "ev1", "2026-06-17", "14:00", status="cancelled")
    assert plan_calendar_sync(SLOTS, [dead], [ev()], NOW) == EMPTY


def test_plan_moved_event_cancels_stale_and_adds_new():
    stale = row("b4", "ev1", "2026-06-17", "14:00")
    plan = plan_calendar_sync(SLOTS, [stale], [ev(start=(18, 9, 0), end=(18, 10, 0))], NOW)
    assert plan["cancel"] == ["b4"]
    assert plan["add"] == [{"event_id": "ev1", "summary": "Team sync",
                            "date": "2026-06-18", "window": "09:00"}]


def test_plan_deleted_event_cancels_blocks():
    gone = row("b5", "ev9", "2026-06-17", "10:00")
    plan = plan_calendar_sync(SLOTS, [gone], [], NOW)
    assert plan == {**EMPTY, "cancel": ["b5"]}


def test_plan_leaves_past_blocks_alone():
    old = row("b6", "ev9", "2026-06-15", "10:00")   # before today — outside the sync window
    assert plan_calendar_sync(SLOTS, [old], [], NOW) == EMPTY


# ── hand-deleted events behind PUSHED bookings (Julius 2026-09-03) ───────────
# The tenant deleted a BLAM8-created event in their calendar. The booking is
# NEVER auto-cancelled (a fat-fingered deletion must not void a customer's
# booking) — the mismatch is flagged for the diary's chip instead, and only
# TRANSITIONS are emitted so the every-few-minutes sweep writes nothing in
# steady state.
def test_plan_flags_pushed_booking_whose_event_was_hand_deleted():
    pushed = row("b7", "ev-gone", "2026-06-17", "14:00", source="email")
    plan = plan_calendar_sync(SLOTS, [pushed], [], NOW)
    assert plan["flag_missing"] == ["b7"]
    assert plan["cancel"] == []                     # never auto-cancelled
    # already flagged → steady state, nothing to write
    pushed["details"] = {"calendar_event_missing_at": "2026-06-16T08:00:00Z"}
    assert plan_calendar_sync(SLOTS, [pushed], [], NOW)["flag_missing"] == []
    # past or already-cancelled pushed rows never flag
    past = row("b8", "ev-gone", "2026-06-10", "14:00", source="email")
    dead = row("b9", "ev-gone", "2026-06-17", "14:00",
               status="cancelled", source="email")
    assert plan_calendar_sync(SLOTS, [past, dead], [], NOW)["flag_missing"] == []


def test_plan_unflags_when_the_event_is_seen_again():
    flagged = row("b10", "ev1", "2026-06-17", "14:00", source="email")
    flagged["details"] = {"calendar_event_missing_at": "2026-06-16T08:00:00Z"}
    plan = plan_calendar_sync(SLOTS, [flagged], [ev()], NOW)
    assert plan["unflag_missing"] == ["b10"]
    assert plan["flag_missing"] == []


# ── Google event normalisation (the Graph side normalises inline) ────────────
def test_gcal_normalize_timed_offsets_and_zulu():
    from app.gmail import normalize_gcal_event
    ev = normalize_gcal_event({
        "id": "g1", "summary": "Standup",
        "start": {"dateTime": "2026-06-17T14:00:00+01:00"},
        "end": {"dateTime": "2026-06-17T13:30:00Z"},   # 14:30 London
    })
    assert ev["subject"] == "Standup" and ev["all_day"] is False
    assert ev["start"].astimezone(DIARY_TZ).hour == 14
    assert ev["end"].astimezone(DIARY_TZ).hour == 14 and ev["end"].astimezone(DIARY_TZ).minute == 30
    # …and it maps onto the diary like any other event
    assert event_blocks(SLOTS, ev, NOW) == [("2026-06-17", "14:00")]


def test_gcal_normalize_all_day_and_skips():
    from app.gmail import normalize_gcal_event
    ev = normalize_gcal_event({
        "id": "g2", "start": {"date": "2026-06-17"}, "end": {"date": "2026-06-18"},
    })
    assert ev["all_day"] is True and ev["subject"] == "Busy"
    assert event_blocks(SLOTS, ev, NOW) == [("2026-06-17", "day")]
    assert normalize_gcal_event({"id": "g3", "status": "cancelled",
                                 "start": {"date": "2026-06-17"}, "end": {"date": "2026-06-18"}}) is None
    assert normalize_gcal_event({"id": "g4", "transparency": "transparent",
                                 "start": {"dateTime": "2026-06-17T14:00:00Z"},
                                 "end": {"dateTime": "2026-06-17T15:00:00Z"}}) is None


# ── sync_all: a failing calendar grant logs once, then backs off ─────────────
def test_sync_all_logs_failing_grant_once_then_backs_off(monkeypatch, caplog):
    """Live finding (June 2026): one mailbox's calendar failure (a 403 with the
    Calendar API disabled, or a 400 from a dead refresh token) printed a full
    traceback EVERY sweep, which OOM-looped the box. Now the connection is logged
    once, then SKIPPED with backoff (sync_connection isn't even called again), and
    a recovered grant resumes once the backoff clears."""
    import asyncio
    import logging

    import httpx

    from app import calendar_sync, sync_backoff

    conn = {"id": "c-1", "tenant_id": "t1", "address": "admin@x.co"}
    refuse = {"on": True}
    calls = {"n": 0}

    async def fake_sync_connection(c):
        calls["n"] += 1
        if refuse["on"]:
            req = httpx.Request("GET", "https://www.googleapis.com/calendar/v3/x")
            raise httpx.HTTPStatusError("403", request=req,
                                        response=httpx.Response(403, request=req))
        return {"added": 0, "cancelled": 0}

    monkeypatch.setattr(calendar_sync.db, "list_calendar_connections", lambda: [dict(conn)])
    monkeypatch.setattr(calendar_sync, "sync_connection", fake_sync_connection)
    sync_backoff._state.clear()

    with caplog.at_level(logging.WARNING):
        asyncio.run(calendar_sync.sync_all())   # first sweep: one warning, no traceback
        asyncio.run(calendar_sync.sync_all())   # second sweep: skipped (in backoff), silent
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Calendar API" in warnings[0].getMessage()
    assert warnings[0].exc_info is None         # one line, not a traceback
    assert calls["n"] == 1                       # second sweep never hit the wire

    # backoff window passes + the grant is fixed → resumes, no longer skipped
    sync_backoff._state.clear()
    refuse["on"] = False
    asyncio.run(calendar_sync.sync_all())
    assert calls["n"] == 2
    assert sync_backoff.should_skip(("calendar", "c-1")) is False

    caplog.clear()
    refuse["on"] = True                         # breaks again → a fresh streak warns again
    with caplog.at_level(logging.WARNING):
        asyncio.run(calendar_sync.sync_all())
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ── pushed event length ───────────────────────────────────────────────────────
def test_pushed_event_uses_the_bookings_own_length():
    """Live 2026-09-03 (Stone Mason): a 45-minute design consultation (3 grid
    blocks) was pushed as a 30-minute event — _event_times only knew the diary's
    slot size. A row that carries length_blocks owns its end time; a plain slot
    row still falls back to slot_minutes."""
    from app import calendar_sync
    cfg = {**SLOTS, "slot_minutes": 30}
    svc = {"booking_date": "2026-09-08", "window": "15:30", "start_block": 62, "length_blocks": 3}
    start, end, all_day = calendar_sync._event_times(cfg, svc)
    assert (start, end, all_day) == ("2026-09-08T15:30:00", "2026-09-08T16:15:00", False)
    plain = {"booking_date": "2026-09-08", "window": "15:30"}
    assert calendar_sync._event_times(cfg, plain)[1] == "2026-09-08T16:00:00"
