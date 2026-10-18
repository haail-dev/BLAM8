"""The "When it's full" card (docs/plan-when-its-full.md item 4, Kris 2026-09-23: "hold
appointments while gathering too, also allow appointments to be added to waiting list.
This should all be Agnostic Config settings").

- hold_while_gathering: a setting now (default on = the behaviour since it shipped), for
  every kind of booking — and appointments reserve while gathering too (the Ellen test:
  the trial night was held while Pepper's details came in, the 1pm meet & greet was not);
- the waiting list takes an appointment: its room is its own pool, inside its own rules;
- the card saves its keys without resetting the ones it doesn't post.

Run: python -m pytest tests/test_when_full_card.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime, timedelta

from app import courses, scheduling, services, slotgrid
from app.intake import STAY_FIELD
from tests.test_stay_executor import CFG, NOW

MEET = {"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet", "starts": ["11:00", "13:00", "15:00"]}
# "Dog name(s)" is the rig's own field: its "(s)" once read as a judged rule, so a
# missing dog name never reserved anything. Every reservation test below uses it.
NEEDS_NAME = [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}]


def _exec(monkeypatch, inserted, *, hold=True, waitlist=False, joined=None, booked=None):
    cfg = {**CFG, "intake_fields": NEEDS_NAME, "service_types": [MEET],
           "resource_types": [{"name": "Meet & greet", "count": 1}],
           "courses": {"hold_while_gathering": hold, "waitlist": waitlist}}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: list(booked or []))
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "count_service_bookings_by_day", lambda *a, **k: {})
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": f"b{len(inserted) + 1}", "booking_ref": f"BK-91{len(inserted) + 1:02d}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    if joined is not None:
        monkeypatch.setattr(courses, "join", lambda t, c, cid, key, **k: joined.append(key) or
                            {"joined": True, "position": 1, "row": {"series_key": key}})
    execute, _ = scheduling.make_executor("t1", contact="ellen@x.co", stay=True, now=NOW,
                                          intake_fields=NEEDS_NAME, service_types=[MEET])
    return execute


APPT = {"service": "Meet & greet", "date": "2026-06-30", "window": "13:00", "summary": "Meet & greet"}
STAY = {"arrival": "2026-06-30T13:30", "departure": "2026-07-01T09:30", "summary": "Trial night"}


# ── the setting ──────────────────────────────────────────────────────────────
def test_hold_while_gathering_is_on_by_default_and_on_the_card_face():
    assert courses.clean({})["hold_while_gathering"] is True
    assert courses.full_face(courses.clean({})) == (
        "held while details come in · other dates offered and held · no waiting list")
    off = courses.clean({"hold_while_gathering": False, "other_dates": "offer", "waitlist": True})
    assert courses.full_face(off) == (
        "nothing held until details are in · other dates offered, not held · waiting list (offers held 48 h)")


# ── appointments reserve while gathering, like stays ─────────────────────────
def test_an_appointment_missing_a_detail_is_held_while_it_is_asked_for(monkeypatch):
    inserted = []
    out = _exec(monkeypatch, inserted)("hold_appointment", dict(APPT))
    assert out["error"] == "missing_info" and out.get("reserved") is True, out
    assert [(r.get("status"), r.get("window"), r.get("resource_type")) for r in inserted] == \
        [("offered", "13:00", "Meet & greet")]
    assert "held for them until" in out["message"]


def test_both_kinds_are_held_in_one_email_while_details_come_in(monkeypatch):
    inserted = []
    execute = _exec(monkeypatch, inserted)
    assert execute("hold_appointment", dict(APPT)).get("reserved") is True
    assert execute("hold_booking", dict(STAY)).get("reserved") is True
    assert [r.get("status") for r in inserted] == ["offered", "offered"]


def test_switched_off_nothing_is_held_and_nothing_says_so(monkeypatch):
    inserted = []
    execute = _exec(monkeypatch, inserted, hold=False)
    for name, args in (("hold_appointment", APPT), ("hold_booking", STAY)):
        out = execute(name, dict(args))
        assert out["error"] == "missing_info" and "reserved" not in out
        assert "held for them" not in out["message"]
    assert inserted == []


# ── the waiting list takes an appointment ────────────────────────────────────
def test_a_full_appointment_check_names_the_waiting_list(monkeypatch):
    taken = {"booking_date": "2026-06-30", "start_block": slotgrid.block_at("13:00"), "length_blocks": 2,
             "status": "confirmed", "window": "13:00", "resource_type": "Meet & greet"}
    out = _exec(monkeypatch, [], waitlist=True, booked=[taken])(
        "check_appointment_availability", {"service": "Meet & greet", "date_from": "2026-06-30",
                                           "date_to": "2026-06-30"})
    assert "13:00" not in {s["window"] for s in out["available"]}
    assert "waiting list" in out["waiting_list"].lower()


def test_joining_lists_the_appointment_by_service_date_and_time(monkeypatch):
    joined = []
    out = _exec(monkeypatch, [], waitlist=True, joined=joined)(
        "join_waitlist", {"service": "meet & GREET", "date": "2026-06-30", "window": "13:00"})
    assert out.get("joined") is True, out
    assert joined == ["date|Meet & greet|2026-06-30|13:00"]                # the service's own name


def test_an_appointment_list_has_room_only_in_its_own_pool(monkeypatch):
    cfg = {**CFG, "service_types": [MEET], "resource_types": [{"name": "Meet & greet", "count": 1}]}
    day = date.today() + timedelta(days=10)                                   # inside the horizon
    key = courses.date_key("Meet & greet", day, "13:00")
    stay = {"booking_date": day.isoformat(), "start_block": 0, "length_blocks": 96 * 2,
            "status": "confirmed", "window": "day", "resource_type": None}
    appt = {"booking_date": day.isoformat(), "start_block": slotgrid.block_at("13:00"), "length_blocks": 2,
            "status": "confirmed", "window": "13:00", "resource_type": "Meet & greet"}
    monkeypatch.setattr(courses.db, "count_service_bookings_by_day", lambda *a, **k: {})
    monkeypatch.setattr(courses.db, "list_active_bookings", lambda *a, **k: [stay] * 12)   # kennels full
    assert courses.has_room("t1", key, date.today(), cfg_row=cfg) is True           # the stays don't count
    monkeypatch.setattr(courses.db, "list_active_bookings", lambda *a, **k: [appt])
    assert courses.has_room("t1", key, date.today(), cfg_row=cfg) is False          # its own place is taken
    rules = {**cfg, "service_types": [{**MEET, "starts": ["11:00"]}]}
    monkeypatch.setattr(courses.db, "list_active_bookings", lambda *a, **k: [])
    assert courses.has_room("t1", key, date.today(), cfg_row=rules) is False        # 13:00 isn't a start


# ── the card ─────────────────────────────────────────────────────────────────
def test_the_card_posts_its_three_choices_and_the_list_moved_with_it():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/app/templates/app/_scheduling_settings.html").read_text(encoding="utf-8")
    card = src[src.index("data-when-full"):src.index("RECURRING — standing weekly/term bookings")]
    for name in ('name="hold_while_gathering"', 'name="other_dates" value="hold"', 'name="other_dates" value="offer"',
                 'name="other_dates" value="off"', 'name="waitlist_on"', 'name="waitlist_notify"', 'name="offer_hours"',
                 'name="full_card_present"'):
        assert name in card, name
    assert src.count('name="waitlist_on"') == 1                              # moved, not copied
    dash = (Path(__file__).resolve().parents[1] / "src/app/dashboard.py").read_text(encoding="utf-8")
    assert "_courses_mod.clean({**_stored_courses, **_full," in dash
