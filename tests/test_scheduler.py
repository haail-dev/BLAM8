"""In-process daily scheduler — the time math (app/scheduler.seconds_until).

The jobs themselves are tested in test_reminders / test_intake / the purge path; this
pins the "sleep until the next HH:MM UTC" calculation the in-process runner relies on.
"""
from datetime import datetime, timedelta, timezone

from app import scheduler


def test_seconds_until_later_today():
    now = datetime(2026, 6, 29, 6, 0, tzinfo=timezone.utc)
    assert scheduler.seconds_until(8, 0, now) == 2 * 3600


def test_seconds_until_rolls_to_tomorrow_when_passed():
    now = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    assert scheduler.seconds_until(8, 0, now) == 23 * 3600


def test_seconds_until_exact_time_rolls_forward_a_day():
    now = datetime(2026, 6, 29, 8, 0, tzinfo=timezone.utc)
    assert scheduler.seconds_until(8, 0, now) == 24 * 3600


def test_seconds_until_with_minutes():
    now = datetime(2026, 6, 29, 3, 0, tzinfo=timezone.utc)
    assert scheduler.seconds_until(3, 30, now) == 30 * 60


def test_seconds_until_normalises_non_utc_now():
    now = datetime(2026, 6, 29, 6, 0, tzinfo=timezone(timedelta(hours=2)))  # 04:00 UTC
    assert scheduler.seconds_until(8, 0, now) == 4 * 3600
