"""Scheduling module — Diary view-model + settings parsing (build step 5).

diary_days() turns config + bookings into the date groups /app/diary renders:
every day from today to the horizon that is a working day OR has bookings (an
odd booking on a closed day must never be hidden), each with per-window
occupancy. parse_blackouts() is the forgiving settings-form parser.

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import calendar
from datetime import datetime

from app.scheduling import DIARY_TZ, diary_days, parse_blackouts

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday


def b(d, window="day", status="confirmed", **extra):
    return {"booking_date": d, "window": window, "status": status, **extra}


def test_groups_cover_working_days_to_horizon():
    days = diary_days(carried_config({"horizon_days": 6}), [], NOW)
    # Tue 16 → Mon 22, minus Sat/Sun: Tue Wed Thu Fri Mon — empty groups included.
    assert [d["date"] for d in days] == [
        "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22"]
    assert days[0]["label"].startswith("Tuesday")
    assert all(d["working"] and d["bookings"] == [] for d in days)
    assert days[0]["windows"] == [{"window": "day", "used": 0, "capacity": 4, "remaining": 4}]


def test_booking_on_closed_day_still_shown():
    days = diary_days(carried_config({"horizon_days": 6}), [b("2026-06-20")], NOW)  # Saturday
    sat = next(d for d in days if d["date"] == "2026-06-20")
    assert sat["working"] is False
    assert len(sat["bookings"]) == 1


def test_occupancy_counts_active_only_and_flags_full():
    rows = [b("2026-06-18"), b("2026-06-18", status="proposed"),
            b("2026-06-18", status="cancelled")]
    days = diary_days(carried_config({"capacity_per_window": 2, "horizon_days": 3}), rows, NOW)
    thu = next(d for d in days if d["date"] == "2026-06-18")
    assert thu["windows"][0]["used"] == 2      # cancelled doesn't occupy...
    assert len(thu["bookings"]) == 3           # ...but every row passed in is listed
    assert thu["full"] is True
    assert next(d for d in days if d["date"] == "2026-06-17")["full"] is False


def test_ampm_mode_two_windows_with_day_overlap():
    rows = [b("2026-06-18", "am"), b("2026-06-18", "day")]
    days = diary_days(carried_config({"windows": "ampm", "capacity_per_window": 2, "horizon_days": 3}), rows, NOW)
    thu = next(d for d in days if d["date"] == "2026-06-18")
    assert thu["windows"] == [
        {"window": "am", "used": 2, "capacity": 2, "remaining": 0},
        {"window": "pm", "used": 1, "capacity": 2, "remaining": 1},
    ]
    assert thu["full"] is False  # pm still has space


def test_blackout_day_closed_in_diary():
    days = diary_days(carried_config({"horizon_days": 3, "blackout_dates": ["2026-06-18"]}), [], NOW)
    assert "2026-06-18" not in [d["date"] for d in days]  # closed and empty — hidden


def test_calendar_weeks_alignment_and_payload():
    """The grid is a MONTH (Kris 2026-09-02: "they bleed into each other"), so
    it opens on the 1st, not on the Monday of today's week."""
    from app.scheduling import calendar_weeks
    weeks = calendar_weeks(carried_config({"horizon_days": 6}), [b("2026-06-20")], NOW)
    assert weeks[0]["start"] == "2026-06-01"          # 1 June 2026 is a Monday
    this_week = next(w for w in weeks if w["start"] == "2026-06-15")
    assert [d["dom"] for d in this_week["days"]] == [15, 16, 17, 18, 19, 20, 21]
    mon = this_week["days"][0]
    assert mon["out"] is True and mon["in_diary"] is False   # yesterday — muted pad
    sat = this_week["days"][5]
    assert sat["in_diary"] and sat["booked_count"] == 1      # booking on a closed day surfaces
    tue = this_week["days"][1]
    assert tue["today"] is True and tue["free"] == 4


def test_a_month_grid_holds_only_that_month():
    """The bug: a week from Mon 31 Aug to Sun 6 Sep was filed whole under an
    "August 2026" heading, so six September days appeared there — and October's
    heading then began on the 5th, its first four days already spent inside
    September. Every grid now owns exactly its own month; the days either side
    are blanks."""
    from app.scheduling import calendar_weeks
    weeks = calendar_weeks(carried_config({"horizon_days": 75}), [], NOW)

    months = {}
    for w in weeks:
        label = w["days"][0]["month_label"]
        assert {d["month_label"] for d in w["days"]} == {label},             "a week carries two month labels"
        months.setdefault(label, []).extend(
            d for d in w["days"] if not d.get("filler"))

    assert list(months) == ["June 2026", "July 2026", "August 2026"], list(months)
    for label, days in months.items():
        doms = [d["dom"] for d in days]
        assert doms == sorted(doms), f"{label} is out of order"
        assert doms[0] == 1, f"{label} does not start on the 1st"
        assert len(doms) == len(set(doms)) == calendar.monthrange(
            2026, {"June": 6, "July": 7, "August": 8}[label.split()[0]])[1],             f"{label} has {len(doms)} days"


def test_a_day_appears_in_exactly_one_grid():
    """A day rendered twice would render its booking panel twice, and the panel
    carries an id."""
    from app.scheduling import calendar_weeks
    weeks = calendar_weeks(carried_config({"horizon_days": 75}), [], NOW)
    real = [d["date"] for w in weeks for d in w["days"] if not d.get("filler")]
    assert len(real) == len(set(real))


def test_a_blank_is_never_clickable():
    """Filler cells carry no date, so nothing can open a day panel for a day
    belonging to the month next door."""
    from app.scheduling import calendar_weeks
    weeks = calendar_weeks(carried_config({"horizon_days": 75}), [], NOW)
    fillers = [d for w in weeks for d in w["days"] if d.get("filler")]
    assert fillers, "a 75-day range must straddle a month boundary"
    for f in fillers:
        assert f["in_diary"] is False and "date" not in f and "dom" not in f


def test_parse_blackouts_forgiving():
    text = "2026-12-25\n2026-12-25, 2027-01-01\nnot a date\n\n 2026-08-31 "
    assert parse_blackouts(text) == ["2026-08-31", "2026-12-25", "2027-01-01"]
    assert parse_blackouts("") == []
    assert parse_blackouts(None) == []
