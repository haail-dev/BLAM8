"""Scheduling — 'slots' window mode (D1 unlocked 2026-06-10 with Julius).

Third windows mode alongside day/ampm: opening hours + slot length generate real
start-time windows ('09:00', '10:00', …). The bookings schema is unchanged — the
window column simply carries the start time. These tests pin:

  - slot generation from open/close/slot_minutes (PostgREST returns time columns
    as 'HH:MM:SS' strings — parsing must accept both HH:MM and HH:MM:SS)
  - notice rule against the REAL slot start (no more midnight conservatism)
  - cross-mode overlap: a legacy 'day' booking blocks every slot; 'am'/'pm'
    block morning/afternoon slots; and the reverse when counting day/ampm
  - hold_error treats a non-generated time as invalid_window
  - diary_line() — the one-line diary shape injected into the agent prompt

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

from app.scheduling import DIARY_TZ, availability, diary_line, hold_error

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

SLOTS = carried_config({"windows": "slots", "open_time": "09:00:00", "close_time": "17:00:00",
         "slot_minutes": 60, "capacity_per_window": 1, "min_notice_hours": 1})


def b(d, window, status="confirmed"):
    return {"booking_date": d, "window": window, "status": status}


def test_slots_generated_from_opening_hours():
    out = availability(SLOTS, [], date(2026, 6, 17), date(2026, 6, 17), NOW)
    assert [s["window"] for s in out] == [
        "09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"]
    assert all(s["remaining"] == 1 for s in out)


def test_half_hour_slots_and_hhmm_config():
    cfg = carried_config({**SLOTS, "open_time": "09:30", "close_time": "11:00", "slot_minutes": 30})
    out = availability(cfg, [], date(2026, 6, 17), date(2026, 6, 17), NOW)
    assert [s["window"] for s in out] == ["09:30", "10:00", "10:30"]


def test_notice_cuts_at_real_slot_start():
    # Tuesday 10:00 + 1h notice = 11:00 cutoff — today's 09:00/10:00 gone, 11:00 on offer.
    out = availability(SLOTS, [], date(2026, 6, 16), date(2026, 6, 16), NOW)
    assert [s["window"] for s in out][0] == "11:00"
    assert len(out) == 6


def test_slot_taken_by_booking_and_legacy_day_blocks_all():
    rows = [b("2026-06-17", "10:00")]
    out = availability(SLOTS, rows, date(2026, 6, 17), date(2026, 6, 17), NOW)
    assert "10:00" not in [s["window"] for s in out]
    assert "11:00" in [s["window"] for s in out]

    rows = [b("2026-06-17", "day")]  # legacy whole-day booking occupies everything
    assert availability(SLOTS, rows, date(2026, 6, 17), date(2026, 6, 17), NOW) == []


def test_ampm_legacy_blocks_matching_halves():
    rows = [b("2026-06-17", "am")]
    out = availability(SLOTS, rows, date(2026, 6, 17), date(2026, 6, 17), NOW)
    assert [s["window"] for s in out] == ["12:00", "13:00", "14:00", "15:00", "16:00"]
    rows = [b("2026-06-17", "pm")]
    out = availability(SLOTS, rows, date(2026, 6, 17), date(2026, 6, 17), NOW)
    assert [s["window"] for s in out] == ["09:00", "10:00", "11:00"]


def test_slot_bookings_count_against_day_and_ampm_modes():
    # A tenant who switches BACK to day/ampm keeps honest counts for slot bookings.
    rows = [b("2026-06-18", "10:00"), b("2026-06-18", "14:00")]
    day_mode = availability(carried_config({"capacity_per_window": 2}), rows, date(2026, 6, 18), date(2026, 6, 18), NOW)
    assert day_mode == []  # both slot bookings occupy the day window
    ampm = availability(carried_config({"windows": "ampm", "capacity_per_window": 1}), rows,
                        date(2026, 6, 18), date(2026, 6, 18), NOW)
    assert ampm == []  # 10:00 fills am, 14:00 fills pm


def test_hold_error_rejects_non_generated_times():
    assert hold_error(SLOTS, date(2026, 6, 17), "10:30", NOW) == "invalid_window"
    assert hold_error(SLOTS, date(2026, 6, 17), "day", NOW) == "invalid_window"
    assert hold_error(SLOTS, date(2026, 6, 17), "10:00", NOW) is None
    assert hold_error(SLOTS, date(2026, 6, 16), "09:00", NOW) == "outside_horizon"  # before cutoff


def test_diary_line_per_mode():
    assert diary_line(SLOTS) == ("Open 09:00-17:00, bookings are 60-minute slots "
                                 "(1 per slot); working days Mon, Tue, Wed, Thu, Fri.")
    assert diary_line(carried_config(None)) == "Up to 4 bookings at the same time; working days Mon, Tue, Wed, Thu, Fri."
    assert diary_line(carried_config({"windows": "ampm", "capacity_per_window": 2})) == (
        "Up to 2 bookings per half-day window (AM/PM); working days Mon, Tue, Wed, Thu, Fri.")


def test_the_diary_line_reads_the_nouns_and_their_number():
    """Run #8: "Up to 1 bookings per day" on a driving school with nouns lesson /
    pupil set. The line takes the business's own noun and its number."""
    line = diary_line(carried_config({"capacity_per_window": 1, "buffer_minutes": 15,
                       "nouns": {"booking": "lesson", "participant": "pupil"}}))
    assert line.startswith("Up to 1 lesson at the same time;") and line.endswith("A 15-minute gap is left between lessons.")
    assert diary_line(carried_config({"capacity_per_window": 3, "nouns": {"booking": "class"}})).startswith("Up to 3 classes at the same time;")
    # the faces read the same noun — never "class" to a driving school
    from pathlib import Path
    face = Path(__file__).resolve().parents[1].joinpath("src", "app", "templates", "app", "_scheduling_settings.html").read_text(encoding="utf-8")
    assert "when the class happens" not in face and "when the ' ~ noun_b ~ ' happens" in face
