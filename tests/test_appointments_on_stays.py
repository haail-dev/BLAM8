"""Appointment services on a STAYS diary (Kris 2026-09-13: "if we have more
than 1 use of Diary we should show Capacity and Slots (for meet & greet)").

Run: python -m pytest tests/test_appointments_on_stays.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime
from pathlib import Path

from app import dashboard, scheduling, services, slotgrid
from app.intake import STAY_FIELD

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cfg(**over):
    cfg = carried_config({"working_days": "mon,tue,wed,thu,fri,sat,sun", "windows": "day", "slot_minutes": 60,
           "capacity_per_window": 30, "horizon_days": 7, "blackout_dates": "",
           "open_time": "07:00", "close_time": "18:45",
           "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
           "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
           "resource_types": [{"name": "Meet & greet", "count": 1}],
           "nouns": {"booking": "booking", "participant": "dog"}})
    cfg.update(over)
    return cfg


def _rows(day_iso):
    stay = {"id": "s1", "booking_date": day_iso, "start_block": 28, "length_blocks": 96 * 2,
            "status": "confirmed", "summary": "Bella", "window": "day", "details": {}}
    appt = {"id": "a1", "booking_date": day_iso, "start_block": 40, "length_blocks": 2,
            "status": "confirmed", "summary": "Meet & greet — Sam", "window": "10:00",
            "resource_type": "Meet & greet", "details": {"Service": "Meet & greet"}}
    return stay, appt


def test_an_appointment_never_occupies_a_kennel_space():
    cfg = _cfg()
    stay, appt = _rows("2026-09-14")
    assert len(scheduling._pool_spans([stay, appt], cfg, None)) == 2                       # the old accounting
    assert len(scheduling._pool_spans([stay, appt], cfg, None, exclude_typed=True)) == 1   # the spaces
    assert len(scheduling._pool_spans([stay, appt], cfg, "Meet & greet")) == 1             # the pool
    assert services.is_appointment_row(appt, cfg["service_types"]) and not services.is_appointment_row(stay, cfg["service_types"])


def test_the_day_labels_an_appointment_and_keeps_it_out_of_the_spaces():
    cfg = _cfg()
    stay, appt = _rows("2026-09-14")
    now = datetime(2026, 9, 14, 9, 0, tzinfo=scheduling.DIARY_TZ)
    days = scheduling.diary_days(cfg, [stay, appt], now)
    today = next(d for d in days if d["date"] == "2026-09-14")
    assert today["windows"][0]["used"] == 1 and today["windows"][0]["remaining"] == 29   # the dog, not the meet & greet
    by_id = {b["id"]: b for b in today["bookings"]}
    assert by_id["a1"]["is_appointment"] is True and by_id["a1"]["time_label"] == "10:00–10:30 · Meet & greet"
    assert by_id["s1"]["is_stay"] is True and "night" in by_id["s1"]["stay_label"]
    strip = dashboard._today_strip(cfg, [stay, appt], now)
    assert strip["used"] == 1 and strip["remaining"] == 29
    assert strip["appointments"] == [{"name": "Meet & greet", "count": 1}]


def test_the_appointment_tools_ride_the_service_schema_with_service_required():
    names = [t["name"] for t in scheduling.APPOINTMENT_TOOLS]
    assert names == ["check_appointment_availability", "hold_appointment"]
    for t in scheduling.APPOINTMENT_TOOLS:
        assert "APPOINTMENT service on this stay diary" in t["description"]
        assert "service" in (t["input_schema"].get("required") or [])
    assert [t["name"] for t in scheduling.SERVICE_TOOLS][:2] == ["check_availability", "hold_booking"]   # untouched
    py = _read("src/app/pipeline.py")
    assert "tools += list(scheduling.APPOINTMENT_TOOLS)" in py
    # The combined-executor whitelist (the trap every new office tool falls into).
    # Re-wrapped 2026-09-22 when offer_stay joined it, so assert the membership
    # rather than one line's spelling.
    for _name in ("move_booking", "check_appointment_availability", "hold_appointment",
                  "offer_stay"):
        assert f'"{_name}"' in py
    assert "service_types=(service_types if (svc or appts) else None)," in py
    assert "scheduling.stay_diary_line(cfg_row, service_types if appts else None)" in py
    sched = _read("src/app/scheduling.py")
    assert "appt_active = bool(service_types) and stay and not fleet" in sched
    assert 'name, appt = "hold_booking", True' in sched and "if svc_active or appt:" in sched


def test_the_diary_line_lists_the_appointments_for_the_agent():
    cfg = _cfg(service_types=[{"name": "Meet & greet", "minutes": 30, "price": 25, "resource": "Meet & greet"}])
    line = scheduling.stay_diary_line(cfg, cfg["service_types"])
    assert "30 space(s) total" in line
    assert "APPOINTMENTS, separate from the spaces" in line
    assert "Meet & greet — 30 min, 1 at a time, £25.00" in line
    assert "check_appointment_availability and hold_appointment" in line
    assert "APPOINTMENTS" not in scheduling.stay_diary_line(cfg)     # no services passed = the old line


def test_binding_gives_each_appointment_its_own_pool():
    svcs, pools = services.bind_appointment_pools(
        [{"name": "Meet & greet", "minutes": 30, "timing": "half"}, {"name": "Assessment", "minutes": 45}],
        [{"name": "Meet & greet", "count": 1}], at_once=["3", ""])
    assert [s["resource"] for s in svcs] == ["Meet & greet", "Assessment"]
    assert all("timing" not in s for s in svcs)                        # an appointment is an exact time
    counts = {p["name"]: p["count"] for p in pools}
    assert counts == {"Meet & greet": 3, "Assessment": 1}
    assert services.appointment_at_once(svcs, pools) == {"Meet & greet": 3, "Assessment": 1}


def test_the_pages_carry_the_other_use():
    home = _read("src/app/templates/app/home.html")
    assert "{% for a in strip.appointments %}" in home and "data-diary-appts" in home
    assert "{{ a.count }} {{ a.name | lower }}{{ '' if a.count == 1 else 's' }} today" in home
    face = _read("src/app/templates/app/_scheduling_settings.html")
    assert "{{ sface('Appointments'," in face and 'name="service_at_once"' in face
    assert "data-appointments-help" in face and "'Add an appointment' if diary_stay_mode else 'Add a service'" in face
    diary = _read("src/app/templates/app/diary.html")
    assert 'name="add_kind" value="appointment"' in diary and "data-appointment-fields" in diary
    dash = _read("src/app/dashboard.py")
    assert "adding_appointment = bool(service.strip())" in dash
    assert "services.bind_appointment_pools(" in dash and "service_at_once: list[str] = Form([])," in dash
    assert '"diary_stay_mode": intake.stay_mode(' in dash and "is_mixed=bool(svc_types) and intake.stay_mode(sched_cfg)," in dash
    cat = _read("src/app/settings_recommend.py")
    assert "EXCEPT for APPOINTMENTS" in cat and "Never write a stay itself" in cat


def test_the_portal_books_appointments_beside_stays():
    """Slice 2: the portal's stay flavour gains check_appointment_availability /
    create_appointment when the stays diary has appointment services, run by
    the same executor in the appointment's pool."""
    from app import portal_data, portal_chat
    assert {"check_appointment_availability", "create_appointment"} <= portal_data.TOOL_NAMES
    assert {"check_appointment_availability", "create_appointment"} <= portal_data._DIARY_TOOLS
    assert {"check_appointment_availability", "create_appointment"} <= portal_chat.BUSINESS_TOOLS   # the business's cost
    assert portal_data._SYNC["check_appointment_availability"] is portal_data._check_appointment_availability
    assert portal_data._ASYNC["create_appointment"] is portal_data._create_appointment
    src = _read("src/app/portal_data.py")
    assert 'service_types=(svc_types if (flavour == "service" or (flavour == "stay" and svc_types)) else None),' in src
    assert 'return await _create_booking(session, args, tool="hold_appointment")' in src
    assert 'blocked = _booking_block(session, args, "service" if appointment else flavour)' in src
    assert 'out = execute(tool, call)' in src
    assert 'if _services.configured(_svc):' in src and '"name": "create_appointment",' in src
