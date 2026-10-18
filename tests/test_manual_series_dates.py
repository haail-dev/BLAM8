"""A standing series placed by hand keeps its length (run #7, 2026-09-05).

Live: Ripple Swim School (children's lessons) — the setup put Recurring =
weekly x12 on, the agent can hold a series, but the diary's "+ Add booking"
form had no repeat control, so the business could not enrol a child for the
term after the assessment (its everyday move). The form now takes Repeat +
Times on a services diary; a blackout date or closed day inside the series
is skipped and the term EXTENDS by a step, so twelve lessons stay twelve.

Run: python -m pytest tests/test_manual_series_dates.py -q
"""
from datetime import date

from app import scheduling

CFG = {"windows": "slots", "open_time": "08:00", "close_time": "18:30", "slot_minutes": 30,
       "working_days": "mon,tue,wed,thu,sat,sun",
       "blackout_dates": ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"],
       "recurring": {"enabled": True, "frequencies": ["weekly"], "max_occurrences": 12}}


def test_a_weekly_term_skips_the_half_term_saturday_and_extends():
    dates = scheduling.manual_series_dates(CFG, date(2026, 9, 19), "weekly", 12)
    assert len(dates) == 12
    assert all(d.weekday() == 5 for d in dates)              # every one a Saturday
    assert date(2026, 10, 31) not in dates                    # the half-term Saturday
    assert dates[0] == date(2026, 9, 19) and dates[-1] == date(2026, 12, 12)  # one week longer


def test_closed_days_are_skipped_too_and_the_cap_holds():
    cfg = {**CFG, "working_days": "sat"}
    dates = scheduling.manual_series_dates(cfg, date(2026, 9, 18), "weekly", 30)   # a Friday
    assert dates == [] or all(d.weekday() == 5 for d in dates)
    assert len(scheduling.manual_series_dates(CFG, date(2026, 9, 19), "weekly", 30)) == 12


def test_off_or_unoffered_repeat_is_no_series():
    assert scheduling.manual_series_dates({**CFG, "recurring": {"enabled": False}}, date(2026, 9, 19), "weekly", 12) == []
    assert scheduling.manual_series_dates(CFG, date(2026, 9, 19), "monthly", 3) == []
    assert scheduling.manual_series_dates(CFG, date(2026, 9, 19), "", 12) == []
