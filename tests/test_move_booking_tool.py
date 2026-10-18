"""The email-side move_booking tool: the customer's own booking, by reference,
to a new slot. In time → moved in place (same reference, no charge). Inside the
notice rule → the new time held first, then the old booking cancelled as a late
cancellation through the cancel tool. Refusals never touch the diary.

Run: python -m pytest tests/test_move_booking_tool.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import agent, pipeline, scheduling, wallet
from app.scheduling import DIARY_TZ

CFG = carried_config({"windows": "slots", "open_time": "08:00", "close_time": "19:00", "slot_minutes": 60,
       "capacity_per_window": 1, "min_notice_hours": 48, "horizon_days": 120,
       "working_days": "mon,tue,wed,thu,fri,sat",
       "credits": {"spend": "auto", "late_hours": 48, "no_show_spends": True, "excused_refunds": True,
                   "hours": "minutes"}})
SERVICES = [{"name": "1-hour lesson", "minutes": 60, "price": 34},
            {"name": "2-hour lesson", "minutes": 120, "price": 66}]
NOW = datetime(2026, 9, 5, 17, 0, tzinfo=DIARY_TZ)          # Saturday 5 Sep, 17:00


def _row(ref, day, sb=40, length=8, status="confirmed", service="2-hour lesson"):
    return {"id": "b-" + ref, "booking_ref": ref, "booking_date": day, "start_block": sb, "length_blocks": length,
            "window": "10:00", "status": status, "summary": f"{service} — Kris", "contact": "kris@x.co",
            "details": {"Service": service, "Pick-up address": "14 Homend"}, "recurrence_id": None}


def _env(monkeypatch, mine, booked=()):
    seen = {"status": [], "register": [], "events": [], "after": [], "moved": [], "held": []}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda t, a, b, **k: list(booked))
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda t, c, client=None: list(mine))
    monkeypatch.setattr(scheduling.db, "set_booking_status",
                        lambda bid, status, tid, client=None: seen["status"].append((bid, status)))
    monkeypatch.setattr(scheduling.db, "log_agent_event",
                        lambda tid, kind, summary, **k: seen["events"].append((kind, summary)))
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)

    def move_in_place(bid, tid, new_date, sb, length, window, client=None):
        seen["moved"].append((bid, new_date, sb, length, window))
        return {"id": bid, "booking_date": new_date, "start_block": sb}
    monkeypatch.setattr(scheduling.db, "move_booking_in_place", move_in_place)

    def insert(tid, date_iso, sb, length, summary, **kw):
        seen["held"].append((date_iso, sb, length, kw.get("details"), kw.get("window")))
        return {"id": "b-BK-1020", "booking_ref": "BK-1020", "booking_date": date_iso, "start_block": sb,
                "length_blocks": length, "status": "proposed", "contact": "kris@x.co"}
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", insert)

    def fake_attendance(tid, booking, status, *, note=None, logged_by="business", today=None, client=None,
                        notified_at=None):
        seen["register"].append((booking["booking_ref"], status, logged_by, note))
        return {"status": status, "wallet": ({"spent": 2.0} if status == "late_cancel" else {"refunded": 2.0})}
    monkeypatch.setattr(wallet, "apply_attendance", fake_attendance)

    async def remove(tid, bid):
        seen["after"].append(("calendar-remove", bid))
    async def push(tid, email_log_id=None, booking_id=None):
        seen["after"].append(("calendar-push", booking_id))
    async def freed(tid, booking, **k):
        seen["after"].append(("freed", booking["booking_ref"], booking["booking_date"]))
        return []
    from app import calendar_sync, courses
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", remove)
    monkeypatch.setattr(calendar_sync, "push_bookings", push)
    monkeypatch.setattr(courses, "on_place_freed", freed)
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", service_types=SERVICES, now=NOW)
    return execute, state, seen


def test_a_move_in_time_moves_in_place_and_keeps_the_reference(monkeypatch):
    # Saturday 26 Sep 10:00 → Monday 28 Sep 14:00, asked three weeks ahead: in time, no charge
    execute, state, seen = _env(monkeypatch, [_row("BK-1003", "2026-09-26")])
    out = execute("move_booking", {"booking_ref": "bk-1003", "new_date": "2026-09-28", "new_window": "14:00"})
    assert out["moved"] is True and out["late"] is False and out["booking_ref"] == "BK-1003"
    assert out["from_date"] == "2026-09-26" and out["date"] == "2026-09-28" and out["window"] == "14:00"
    assert "MOVED from 26th Sep 26 10:00 to 28th Sep 26 14:00" in out["message"] and "no charge" in out["message"]
    (bid, new_date, sb, length, window), = seen["moved"]
    assert (bid, new_date, window) == ("b-BK-1003", "2026-09-28", "14:00")
    assert length == 8                                          # a 2-hour lesson stays 2 hours
    assert seen["status"] == [] and seen["register"] == [] and seen["held"] == []
    assert ("calendar-remove", "b-BK-1003") in seen["after"] and ("calendar-push", "b-BK-1003") in seen["after"]
    assert ("freed", "BK-1003", "2026-09-26") in seen["after"]   # the old Saturday goes to the waiting list
    assert seen["events"][0][0] == "booking_moved" and state["moved"][0]["late"] is False


def test_a_late_move_holds_the_new_time_then_cancels_the_old_as_a_late_cancel(monkeypatch):
    # Monday 7 Sep 10:00, asked Saturday 17:00 → 41 hours, inside the 48-hour rule
    execute, state, seen = _env(monkeypatch, [_row("BK-1001", "2026-09-07")])
    out = execute("move_booking", {"booking_ref": "BK-1001", "new_date": "2026-09-09", "new_window": "10:00",
                                   "reason": "work"})
    assert out["moved"] is True and out["late"] is True
    assert out["old_booking_ref"] == "BK-1001" and out["booking_ref"] == "BK-1020" and out["status"] == "proposed"
    assert "CANCELLED as a late cancellation" in out["message"] and "HELD as BK-1020 on 9th Sep 26 10:00" in out["message"]
    assert "2 hour(s) have come off their block" in out["message"]
    (date_iso, sb, length, details, window), = seen["held"]
    assert (date_iso, window, length) == ("2026-09-09", "10:00", 8) and details["Pick-up address"] == "14 Homend"
    assert seen["status"] == [("b-BK-1001", "cancelled")]
    assert seen["register"][0][:3] == ("BK-1001", "late_cancel", "agent") and "moved to 2026-09-09" in seen["register"][0][3]
    assert seen["moved"] == []                                   # never an in-place move when late
    assert state["bookings"][0]["booking_ref"] == "BK-1020"     # the hold confirms on approval like any hold
    assert state["moved"][0] == {"from_ref": "BK-1001", "to_ref": "BK-1020", "late": True}


def test_a_late_move_whose_new_time_cannot_be_held_changes_nothing(monkeypatch):
    # Sunday is closed: the hold is refused, so the old booking stands untouched
    execute, state, seen = _env(monkeypatch, [_row("BK-1001", "2026-09-07")])
    out = execute("move_booking", {"booking_ref": "BK-1001", "new_date": "2026-09-13", "new_window": "10:00"})
    assert out["error"] == "closed"
    assert seen["status"] == [] and seen["register"] == [] and seen["held"] == []


def test_refusals_never_touch_the_diary(monkeypatch):
    mine = [_row("BK-1003", "2026-09-26"), _row("BK-1002", "2026-09-04"), _row("BK-1009", "2026-10-10", status="offered")]
    execute, state, seen = _env(monkeypatch, mine)
    m = lambda **kw: execute("move_booking", {"booking_ref": "BK-1003", "new_date": "2026-09-28", "new_window": "14:00", **kw})
    assert m(booking_ref="BK-9999")["error"] == "not_theirs"
    assert m(booking_ref="BK-1002")["error"] == "already_happened"
    assert m(booking_ref="BK-1009")["error"] == "not_booked"
    assert m(booking_ref="")["error"] == "no_ref"
    assert m(new_date="next monday")["error"] == "invalid_date"
    assert m(new_window="")["error"] == "invalid_window"
    assert m(new_date="2026-09-13")["error"] == "closed"                 # Sunday
    assert m(new_date="2026-09-07")["error"] == "outside_horizon"        # Monday, inside 48 hours' notice
    state["bookings"].append({"id": "h1", "booking_ref": "BK-1020", "status": "proposed"})
    assert "held in THIS conversation" in m(booking_ref="BK-1020")["message"]
    assert seen["moved"] == [] and seen["status"] == [] and seen["held"] == []
    # a taken slot is 'full', with the prescriptive message
    execute2, _, seen2 = _env(monkeypatch, [_row("BK-1003", "2026-09-26")],
                              booked=[{"id": "other", "booking_date": "2026-09-28", "start_block": 56,
                                       "length_blocks": 8, "status": "confirmed", "window": "14:00"}])
    out = execute2("move_booking", {"booking_ref": "BK-1003", "new_date": "2026-09-28", "new_window": "14:00"})
    assert out["error"] == "full" and seen2["moved"] == []
    # its own old slot never blocks it: moving within the same day to its own time is fine
    execute3, _, seen3 = _env(monkeypatch, [_row("BK-1003", "2026-09-26")],
                              booked=[_row("BK-1003", "2026-09-26")])
    assert execute3("move_booking", {"booking_ref": "BK-1003", "new_date": "2026-09-26", "new_window": "11:00"})["moved"]


def test_the_tool_rides_with_the_cancel_and_is_whitelisted():
    assert scheduling.MOVE_BOOKING_TOOL["name"] == "move_booking"
    assert set(scheduling.MOVE_BOOKING_TOOL["input_schema"]["required"]) == {"booking_ref", "new_date", "new_window"}
    import inspect
    src = inspect.getsource(pipeline)
    assert "tools.append(scheduling.MOVE_BOOKING_TOOL)" in src
    assert '"move_booking"' in src.split("Every office tool rides here")[1][:600]
    assert "use move_booking" in scheduling.CANCEL_BOOKING_TOOL["description"]
    p = agent.build_system_prompt({"business_name": "Hartley"}, scheduling=True, diary_summary="one car", cancel_tool=True)
    assert "call move_booking" in p and "Never cancel-and-hold by hand to move" in p


def test_a_late_move_is_free_when_the_business_says_so(monkeypatch):
    """Kris 2026-09-06: the late move charged as a late cancel is the default, and a
    business option — off, a move inside the window moves in place, no charge."""
    execute, state, seen = _env(monkeypatch, [_row("BK-1001", "2026-09-07")])
    free = {**CFG, "credits": {**CFG["credits"], "late_move_charged": False}}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(free))
    out = execute("move_booking", {"booking_ref": "BK-1001", "new_date": "2026-09-09", "new_window": "10:00"})
    assert out["moved"] is True and out["late"] is False and out["inside_rule"] is True
    assert out["booking_ref"] == "BK-1001" and out["date"] == "2026-09-09"
    assert "inside the 48-hour rule, but the business does not charge for a late move" in out["message"]
    assert seen["moved"] and seen["held"] == [] and seen["status"] == [] and seen["register"] == []
    from app import settings_recommend
    assert "late_move_charged" in settings_recommend.catalogue_block()
    from pathlib import Path
    face = Path(scheduling.__file__).resolve().parents[1].joinpath("app", "templates", "app", "_scheduling_settings.html").read_text(encoding="utf-8")
    assert 'name="late_move_charged"' in face and "late move ' ~ ('charged'" in face
