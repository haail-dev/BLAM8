"""The email-side cancel_booking tool (run #8, 2026-09-05): the customer's own
booking cancelled by its reference, the notice measured against the
Cancellations & credits rule, the register written (late cancel or excused,
'told us' = now), the wallet settled, the outcome returned for the reply.

Run: python -m pytest tests/test_cancel_booking_tool.py -q
"""
from datetime import datetime

from app import agent, scheduling, wallet
from app.scheduling import DIARY_TZ

CFG = {"windows": "slots", "open_time": "08:00", "close_time": "19:00", "slot_minutes": 60,
       "capacity_per_window": 1, "min_notice_hours": 48, "working_days": "mon,tue,wed,thu,fri,sat",
       "credits": {"spend": "auto", "late_hours": 48, "no_show_spends": True, "excused_refunds": True,
                   "hours": "minutes"}}
SERVICES = [{"name": "1-hour lesson", "minutes": 60, "price": 34}]
NOW = datetime(2026, 9, 5, 17, 0, tzinfo=DIARY_TZ)          # Saturday 5 Sep, 17:00


def _row(ref, day, sb=40, status="confirmed"):
    return {"id": "b-" + ref, "booking_ref": ref, "booking_date": day, "start_block": sb, "length_blocks": 8,
            "window": "10:00", "status": status, "summary": "Assessment lesson — Kris",
            "contact": "kris@x.co", "details": {"Service": "Assessment lesson"}, "recurrence_id": None}


def _env(monkeypatch, mine):
    seen = {"status": [], "register": [], "events": [], "after": []}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: CFG)
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda t, c, client=None: list(mine))
    monkeypatch.setattr(scheduling.db, "set_booking_status",
                        lambda bid, status, tid, client=None: seen["status"].append((bid, status)))
    monkeypatch.setattr(scheduling.db, "log_agent_event",
                        lambda tid, kind, summary, **k: seen["events"].append((kind, summary)))

    def fake_attendance(tid, booking, status, *, note=None, logged_by="business", today=None, client=None,
                        notified_at=None):
        seen["register"].append((booking["booking_ref"], status, logged_by, notified_at, note))
        return {"status": status, "wallet": ({"spent": 2.0} if status == "late_cancel" else {"refunded": 2.0})}
    monkeypatch.setattr(wallet, "apply_attendance", fake_attendance)

    async def remove(tid, bid):
        seen["after"].append(("calendar", bid))
    async def freed(tid, booking, **k):
        seen["after"].append(("freed", booking["booking_ref"]))
        return []
    from app import calendar_sync, courses
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", remove)
    monkeypatch.setattr(courses, "on_place_freed", freed)
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", service_types=SERVICES, now=NOW)
    return execute, state, seen


def test_a_late_cancel_spends_and_says_so(monkeypatch):
    # Monday 7 Sep 10:00, asked Saturday 17:00 → 41 hours, inside the 48-hour rule
    execute, state, seen = _env(monkeypatch, [_row("BK-1001", "2026-09-07")])
    out = execute("cancel_booking", {"booking_ref": "bk-1001", "reason": "work"})
    assert out["cancelled"] is True and out["late"] is True and out["register"] == "late_cancel"
    assert out["notice_hours"] == 41.0 and out["rule_hours"] == 48
    assert "CANCELLED" in out["message"] and "inside the 48-hour rule" in out["message"]
    assert "2 hour(s) have come off their block" in out["message"]
    assert seen["status"] == [("b-BK-1001", "cancelled")]
    ref, status, by, told, note = seen["register"][0]
    assert (ref, status, by) == ("BK-1001", "late_cancel", "agent")
    assert told.startswith("2026-09-05T17:00") and "work" in note and "41 hours" in note
    assert ("calendar", "b-BK-1001") in seen["after"] and ("freed", "BK-1001") in seen["after"]
    assert seen["events"][0][0] == "booking_cancelled" and "41 hours" in seen["events"][0][1]
    assert state["cancelled"][0]["outcome"] == "late_cancel"


def test_a_timely_cancel_is_excused_and_refunds(monkeypatch):
    execute, state, seen = _env(monkeypatch, [_row("BK-1003", "2026-09-26")])
    out = execute("cancel_booking", {"booking_ref": "BK-1003"})
    assert out["cancelled"] and out["late"] is False and out["register"] == "excused"
    assert "no charge" in out["message"] and "2 hour(s) are back on their block" in out["message"]
    assert seen["register"][0][1] == "excused"


def test_refusals_never_touch_the_diary(monkeypatch):
    mine = [_row("BK-1001", "2026-09-07"), _row("BK-1002", "2026-09-04"), _row("BK-1009", "2026-10-10", status="offered")]
    execute, state, seen = _env(monkeypatch, mine)
    assert execute("cancel_booking", {"booking_ref": "BK-9999"})["error"] == "not_theirs"
    assert "BK-1001" in execute("cancel_booking", {"booking_ref": "BK-9999"})["message"]
    assert execute("cancel_booking", {"booking_ref": "BK-1002"})["error"] == "already_happened"
    assert execute("cancel_booking", {"booking_ref": "BK-1009"})["error"] == "not_booked"
    assert execute("cancel_booking", {"booking_ref": ""})["error"] == "no_ref"
    assert seen["status"] == [] and seen["register"] == []
    # a slot held in this conversation is not theirs to cancel
    state["bookings"].append({"id": "h1", "booking_ref": "BK-1020", "status": "proposed"})
    out = execute("cancel_booking", {"booking_ref": "BK-1020"})
    assert out["error"] == "not_theirs" and "held in THIS conversation" in out["message"]


def test_without_a_contact_nothing_is_cancelled(monkeypatch):
    execute, state, seen = _env(monkeypatch, [_row("BK-1001", "2026-09-07")])
    execute2, _ = scheduling.make_executor("t1", contact=None, service_types=SERVICES, now=NOW)
    assert execute2("cancel_booking", {"booking_ref": "BK-1001"})["error"] == "no_contact"


def test_the_prompt_swaps_the_colleague_line_for_the_tool():
    cfg = {"business_name": "Hartley", "tone": "plain"}
    with_tool = agent.build_system_prompt(cfg, scheduling=True, diary_summary="one car", cancel_tool=True)
    without = agent.build_system_prompt(cfg, scheduling=True, diary_summary="one car", cancel_tool=False)
    assert "call cancel_booking" in with_tool and "say a colleague will arrange it" not in with_tool
    assert "say a colleague will arrange it" in without and "cancel_booking" not in without
