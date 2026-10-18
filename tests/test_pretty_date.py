"""Dates as a person writes them (src/app/dates.py).

Kris 2026-09-01, looking at the thread's hold trail: "instead of showing the
date like 2026-09-11 it should be 11th Sep 26". One helper, shared by the
templates (the `pretty_date` Jinja filter) and by the agents writing their own
agent_events summaries, so the same date never appears two ways.

Run: python -m pytest tests/ -q
"""
from datetime import date, datetime, timezone

import pytest

from app.dates import pretty_date, pretty_day
from app.templating import templates


@pytest.mark.parametrize("day,expected", [
    (1, "1st Sep 26"), (2, "2nd Sep 26"), (3, "3rd Sep 26"), (4, "4th Sep 26"),
    # The teens are all 'th' — 11/12/13 are the classic ordinal trap.
    (11, "11th Sep 26"), (12, "12th Sep 26"), (13, "13th Sep 26"),
    (21, "21st Sep 26"), (22, "22nd Sep 26"), (23, "23rd Sep 26"),
    (30, "30th Sep 26"),
])
def test_ordinal_suffixes(day, expected):
    assert pretty_date(date(2026, 9, day)) == expected


def test_accepts_the_shapes_callers_actually_hold():
    """bookings.booking_date arrives as an ISO string from the DB, as a date
    from the scheduling layer, and occasionally as a full timestamp."""
    assert pretty_date("2026-09-11") == "11th Sep 26"
    assert pretty_date(date(2026, 9, 11)) == "11th Sep 26"
    assert pretty_date(datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)) == "11th Sep 26"
    assert pretty_date("2026-09-11T10:00:00+00:00") == "11th Sep 26"
    assert pretty_date("  2026-09-11  ") == "11th Sep 26"


def test_unparseable_values_pass_through_untouched():
    """A summary line must never lose its content (or raise) over an odd value —
    callers pipe straight through without proving they hold a date."""
    assert pretty_date(None) == ""
    assert pretty_date("") == ""
    assert pretty_date("whenever suits") == "whenever suits"


def test_registered_as_a_template_filter():
    assert templates.env.filters["pretty_date"] is pretty_date
    assert templates.env.from_string(
        "{{ d | pretty_date }}").render(d="2026-09-11") == "11th Sep 26"


def test_pretty_day_says_it_to_a_customer():
    """The operator files a date; a customer checks a promise against a weekday.
    Used by the arrival-window sentence the drafter is handed (app/scheduling)."""
    assert pretty_day(date(2026, 9, 7)) == "Monday 7th September"
    assert pretty_day("2026-09-01") == "Tuesday 1st September"
    assert pretty_day(datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)) == "Friday 11th September"
    # same forgiving contract as pretty_date
    assert pretty_day(None) == "" and pretty_day("whenever suits") == "whenever suits"


# ── sent_when: the time on the thread's sent bubble (Kris 2026-09-23) ──

def test_sent_when_today_is_just_the_london_time():
    from datetime import datetime, timezone
    from app.dates import sent_when
    now = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
    # BST: 13:32 UTC reads 14:32 in London.
    assert sent_when("2026-09-23T13:32:10.123456+00:00", now=now) == "14:32"


def test_sent_when_from_the_next_day_adds_the_date():
    from datetime import datetime, timezone
    from app.dates import sent_when
    now = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    assert sent_when("2026-09-22T13:32:00+00:00", now=now) == "22nd Sep 26 · 14:32"


def test_sent_when_uses_the_london_day_boundary():
    from datetime import datetime, timezone
    from app.dates import sent_when
    # 23:30 UTC on the 22nd is 00:30 on the 23rd in London — still "today".
    now = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    assert sent_when("2026-09-22T23:30:00Z", now=now) == "00:30"


def test_sent_when_blank_or_junk_shows_nothing():
    from app.dates import sent_when
    assert sent_when(None) == ""
    assert sent_when("") == ""
    assert sent_when("not a time") == ""
