"""Service types with durations (src/app/services.py + the slot-grid availability that books
the right length per service). Pure layer — no DB. The executor path is in
tests/test_service_executor.py.

A service is {name, minutes, price}; a service of N minutes is ceil(N/15) blocks on the
unified grid, so the agent only offers start times where the WHOLE span fits below
capacity (concurrent occupancy — the Bookings-per-slot value).
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

from app import services, slotgrid
from app.scheduling import DIARY_TZ, service_availability, service_diary_line

NOW = datetime(2026, 6, 16, 8, 0, tzinfo=DIARY_TZ)  # Tuesday, before opening
CFG = carried_config({"windows": "slots", "open_time": "09:00", "close_time": "17:00", "slot_minutes": 30,
       "capacity_per_window": 1, "min_notice_hours": 0,
       "working_days": "mon,tue,wed,thu,fri"})


# ── pure module ───────────────────────────────────────────────────────────────
def test_clean_dedupes_clamps_and_prices():
    out = services.clean_service_types([
        {"name": "Cut", "minutes": 30, "price": "25"},
        {"name": " cut ", "minutes": 45},                  # dup name (case/space) → dropped
        {"name": "Massage", "minutes": 5000, "price": "0"},  # minutes clamped, price 0 → None
        {"name": "", "minutes": 30},                       # blank → dropped
        {"name": "Colour", "minutes": "abc"},              # bad minutes → default 60
    ])
    assert [s["name"] for s in out] == ["Cut", "Massage", "Colour"]
    assert out[0] == {"name": "Cut", "minutes": 30, "price": 25.0, "resource": None, "buffer": None}
    assert out[1]["minutes"] == 1440 and out[1]["price"] is None
    assert out[2]["minutes"] == 60


def test_find_and_configured():
    st = [{"name": "Cut & blow dry", "minutes": 45}]
    assert services.configured(st) is True
    assert services.configured([]) is False
    assert services.find(st, "  cut & BLOW dry ")["minutes"] == 45
    assert services.find(st, "perm") is None


def test_render_for_prompt_lists_durations_and_price():
    out = services.render_for_prompt([{"name": "Cut", "minutes": 30, "price": 25.0},
                                      {"name": "Full day", "minutes": 480, "price": None}])
    assert "Cut (30 min, £25)" in out
    assert "Full day (8 hr)" in out
    assert services.render_for_prompt([]) == ""


def test_parse_form_aligned_arrays():
    out = services.parse_form(["Cut", "Massage"], ["30", "90"], ["25", ""])
    assert out == [{"name": "Cut", "minutes": 30, "price": 25.0, "resource": None, "buffer": None},
                   {"name": "Massage", "minutes": 90, "price": None, "resource": None, "buffer": None}]


# ── slot-grid helper ──────────────────────────────────────────────────────────
def test_day_service_starts_fits_within_opening_hours():
    # 09:00-17:00, 30-min step, a 90-min (6-block) service: last start that fits is 15:30.
    starts = slotgrid.day_service_starts(CFG, slotgrid.blocks_for_minutes(90))
    assert slotgrid.hhmm(starts[0]) == "09:00"
    assert slotgrid.hhmm(starts[-1]) == "15:30"   # 15:30 + 90 min = 17:00
    # A 30-min service steps to 16:30 (last 30-min slot before close).
    short = slotgrid.day_service_starts(CFG, slotgrid.blocks_for_minutes(30))
    assert slotgrid.hhmm(short[-1]) == "16:30"


# ── availability (concurrent capacity) ────────────────────────────────────────
def _booking(day, start_block, length_blocks, status="confirmed"):
    return {"booking_date": day, "start_block": start_block, "length_blocks": length_blocks,
            "status": status, "spaces": None, "engineer_id": None}


def test_service_availability_blocks_overlapping_starts():
    length = slotgrid.blocks_for_minutes(60)               # 4 blocks
    # An existing 10:00-11:00 booking (start_block 40, 4 blocks) at capacity 1.
    booked = [_booking("2026-06-17", 40, 4)]
    out = service_availability(CFG, booked, length, date(2026, 6, 17), date(2026, 6, 17), NOW)
    windows = [s["window"] for s in out]
    # A 60-min service can't START at 09:30/10:00/10:30 (would overlap 10:00-11:00) but 11:00 is free.
    assert "10:00" not in windows and "10:30" not in windows and "09:30" not in windows
    assert "09:00" in windows and "11:00" in windows


def test_service_availability_capacity_two_allows_overlap():
    length = slotgrid.blocks_for_minutes(60)
    booked = [_booking("2026-06-17", 40, 4)]                # one 10:00 booking, cap 2
    cfg = carried_config({**CFG, "capacity_per_window": 2})
    out = service_availability(cfg, booked, length, date(2026, 6, 17), date(2026, 6, 17), NOW)
    by = {s["window"]: s["remaining"] for s in out}
    assert by["10:00"] == 1          # one space still free at 10:00
    assert by["09:00"] == 2          # untouched slot has both free


def test_service_availability_only_offers_starts_the_length_fits():
    length = slotgrid.blocks_for_minutes(120)              # 2-hour service = 8 blocks
    out = service_availability(CFG, [], length, date(2026, 6, 17), date(2026, 6, 17), NOW)
    # last 2-hour start that fits before 17:00 is 15:00.
    assert [s["window"] for s in out][-1] == "15:00"


def test_service_diary_line_lists_services():
    line = service_diary_line(CFG, [{"name": "Cut", "minutes": 30, "price": 25.0}])
    assert "Services: Cut (30 min, £25)." in line
    # No services → identical to the plain diary line (no Services suffix).
    assert "Services:" not in service_diary_line(CFG, [])


# ── per-service timing: exact time / arrival window / whole day (Kris 2026-09-03) ──
def test_timing_kind_is_cleaned_and_only_stored_when_set():
    """A stonemason's site survey is an ARRIVAL WINDOW, its workshop consultation an
    exact time. `timing` rides per service; the default never gains the key so every
    existing tenant's rows are byte-identical."""
    rows = services.clean_service_types([
        {"name": "Consultation", "minutes": 45},
        {"name": "Site survey", "minutes": 60, "timing": "half"},
        {"name": "Installation day", "minutes": 480, "timing": "Whole day"},
        {"name": "Odd", "minutes": 30, "timing": "banana"},
    ])
    assert "timing" not in rows[0] and services.timing(rows[0]) == "time"
    assert rows[1]["timing"] == "half" and services.timing_label(rows[1]) == "Morning or afternoon"
    assert rows[2]["timing"] == "day"
    assert "timing" not in rows[3]
    form = services.parse_form(["A", "B"], ["30", "60"], ["", ""], None, None, None, ["time", "half"])
    assert "timing" not in form[0] and form[1]["timing"] == "half"
    prompt = services.render_for_prompt(rows)
    assert "ARRIVAL WINDOW" in prompt and "'am' or 'pm'" in prompt and "WHOLE-DAY" in prompt
    assert "we'll be with you on Monday" in prompt
    # an all-exact-time list keeps the old prompt: no arrival coaching
    assert "ARRIVAL WINDOW" not in services.render_for_prompt([{"name": "Cut", "minutes": 30}])


def test_arrival_window_availability_offers_halves_not_times():
    """A 60-min arrival-window service on a 09:00–17:00 day: the morning is offered as
    one 'am' entry (not eight start times); once the morning is solid the afternoon
    alone remains; a whole-day service offers 'day'."""
    from app.scheduling import first_fit_start, window_bounds, canonical_window, window_words
    day = date(2026, 6, 17)
    out = service_availability(CFG, [], 4, day, day, NOW, timing="half")
    assert [(o["window"], o["remaining"]) for o in out] == [("am", 1), ("pm", 1)]
    assert out[0]["label"] == "Morning (arrival between 09:00 and 12:00)"
    assert out[1]["label"] == "Afternoon (arrival between 12:00 and 17:00)"
    # book the whole morning (09:00–12:00 = 12 blocks) → only the afternoon is left
    morning = [{"booking_date": "2026-06-17", "start_block": 36, "length_blocks": 12,
                "status": "confirmed", "spaces": None, "engineer_id": None}]
    out2 = service_availability(CFG, morning, 4, day, day, NOW, timing="half")
    assert [o["window"] for o in out2] == ["pm"]
    # whole-day timing → one 'day' entry; exact timing unchanged (start times)
    assert [o["window"] for o in service_availability(CFG, [], 4, day, day, NOW, timing="day")] == ["day"]
    assert service_availability(CFG, [], 4, day, day, NOW)[0]["window"] == "09:00"
    # placement: earliest fit inside the window; the exact start for a clock time
    assert first_fit_start(CFG, morning, 4, day, "pm", NOW) == 48          # 12:00
    assert first_fit_start(CFG, morning, 4, day, "am", NOW) is None
    assert first_fit_start(CFG, [], 4, day, "10:30", NOW) == 42
    assert first_fit_start(CFG, morning, 4, day, "10:30", NOW) is None
    assert window_bounds(CFG, day, "am") == (36, 48) and window_bounds(CFG, day, "day") == (36, 68)
    half = {"name": "Survey", "minutes": 60, "timing": "half"}
    assert canonical_window(half, "am") == "am" and canonical_window(half, "Afternoon") == "pm"
    assert canonical_window(half, "14:00") == "pm" and canonical_window(half, "09:30") == "am"
    assert canonical_window(half, "sometime") is None
    assert canonical_window({"name": "Fit", "minutes": 480, "timing": "day"}, "am") == "day"
    assert canonical_window({"name": "Cut", "minutes": 30}, "14:00") == "14:00"
    assert canonical_window({"name": "Cut", "minutes": 30}, "am") is None
    assert window_words("am") == "in the morning" and window_words("15:30") == "at 15:30"
    line = service_diary_line(CFG, [half])
    assert "[arrival window: morning or afternoon]" in line
