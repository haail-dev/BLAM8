"""Today's diary chip on a STAYS diary counts spaces, not windows (Kris
2026-09-13: "it's showing 1 free slot but the kennels has 30 dog slots
available").

Run: python -m pytest tests/test_today_strip.py -q
"""
from datetime import datetime
from pathlib import Path

from app import dashboard, scheduling
from app.intake import STAY_FIELD

ROOT = Path(__file__).resolve().parents[1]


def _cfg(**over):
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat,sun", "windows": "day",
           "capacity_per_window": 30, "horizon_days": 30, "blackout_dates": "",
           "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
           "nouns": {"booking": "booking", "participant": "dog"}}
    cfg.update(over)
    return cfg


def test_a_stays_diary_reports_spaces_in_the_business_noun():
    now = datetime(2026, 9, 13, 15, 0, tzinfo=scheduling.DIARY_TZ)
    strip = dashboard._today_strip(_cfg(), [], now)
    assert strip["stay_mode"] is True
    assert strip["capacity"] == 30 and strip["used"] == 0 and strip["remaining"] == 30
    assert strip["noun_plural"] == "dogs"
    assert len(strip["slots"]) == 1 and strip["slots"][0]["remaining"] == 30


def test_a_slots_diary_keeps_its_window_count():
    now = datetime(2026, 9, 13, 15, 0, tzinfo=scheduling.DIARY_TZ)
    cfg = _cfg(windows="slots", open_time="09:00", close_time="12:00", slot_minutes=60,
               intake_fields=[{"label": "Address", "required": True}], nouns=None)
    strip = dashboard._today_strip(cfg, [], now)
    assert strip["stay_mode"] is False and len(strip["slots"]) == 3
    assert strip["noun_plural"] == "in"                      # no noun set: the chip never says "ins"


def test_the_chip_reads_spaces_on_a_stays_diary():
    html = (ROOT / "src/app/templates/app/home.html").read_text(encoding="utf-8")
    assert "{% if strip.stay_mode %}" in html and "data-diary-spaces" in html
    assert "{{ strip.capacity }} space{{ '' if strip.capacity == 1 else 's' }} free" in html
    assert "{{ strip.remaining }} of {{ strip.capacity }} space" in html
