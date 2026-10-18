"""Unit tests for the pure slot-grid engine (src/app/slotgrid.py). No DB, no clock —
config + bookings in, answers out. The whole point of the grid is that these
semantics are testable in isolation, so they're covered exhaustively here."""
from tests.booking_fixtures import carried_config
from datetime import datetime, time

from app import slotgrid as sg

# A plain office config: 60-min slots, 09:00–17:00, Mon–Fri.
CFG = carried_config({"block_minutes": 60, "open_time": "09:00", "close_time": "17:00",
       "working_days": "mon,tue,wed,thu,fri", "min_notice_hours": 24,
       "horizon_days": 28, "blackout_dates": []})
# A Wednesday well inside the horizon; "now" is the Monday before, 08:00.
NOW = datetime(2026, 1, 5, 8, 0)      # Mon
WED = datetime(2026, 1, 7).date()     # Wed (notice + working-day OK)


# ── conversions ───────────────────────────────────────────────────────────────
def test_block_unit_constants():
    assert sg.BLOCK_MIN == 15 and sg.BLOCKS_PER_DAY == 96 and sg.NOON_BLOCK == 48


def test_blocks_for_minutes_rounds_up_and_floors_at_one():
    assert sg.blocks_for_minutes(60) == 4
    assert sg.blocks_for_minutes(50) == 4        # rounds UP — never under-allocate
    assert sg.blocks_for_minutes(45) == 3
    assert sg.blocks_for_minutes(0) == 1
    assert sg.blocks_for_minutes(15 * 32) == 32  # an 8-hour job


def test_normalize_block_minutes_snaps_and_clamps():
    assert sg.normalize_block_minutes(60) == 60
    assert sg.normalize_block_minutes(50) == 45   # nearest 15
    assert sg.normalize_block_minutes(53) == 60
    assert sg.normalize_block_minutes(7) == 15     # clamp up
    assert sg.normalize_block_minutes(99999) == 1440  # clamp to a day
    assert sg.normalize_block_minutes(None) == 15


def test_block_at_and_labels():
    assert sg.block_at("09:00") == 36 and sg.block_at("09:14") == 36
    assert sg.block_at("09:15") == 37 and sg.block_at(time(0, 0)) == 0
    assert sg.hhmm(36) == "09:00" and sg.hhmm(96) == "24:00"
    assert sg.time_at(36) == time(9, 0)
    assert sg.label(36, 4) == "09:00–10:00"


# ── grid from config ──────────────────────────────────────────────────────────
def test_open_close_blocks():
    assert sg.open_close_blocks(CFG) == (36, 68)
    # close 17:10 rounds the closing block UP so the part-block stays bookable.
    assert sg.open_close_blocks(carried_config({**CFG, "close_time": "17:10"})) == (36, 69)
    # nonsense span → whole 24h grid, not empty.
    assert sg.open_close_blocks(carried_config({**CFG, "close_time": "08:00"})) == (0, 0)


def test_day_slots_60_minutes():
    slots = sg.day_slots(CFG)
    assert slots[0] == (36, 4) and slots[-1] == (64, 4)   # 09:00 … 16:00
    assert len(slots) == 8 and all(n == 4 for _, n in slots)


def test_day_slots_15_and_120_minutes():
    assert len(sg.day_slots(carried_config({**CFG, "block_minutes": 15}))) == 32   # 8h / 15m
    two_hour = sg.day_slots(carried_config({**CFG, "block_minutes": 120}))
    assert two_hour == [(36, 8), (44, 8), (52, 8), (60, 8)]


def test_day_slots_full_day_and_modes():
    # a block ≥ the open span collapses to one whole-day slot
    assert sg.day_slots(carried_config({**CFG, "block_minutes": 1440})) == [(36, 32)]
    assert sg.day_slots(carried_config({**CFG, "windows": "day"})) == [(36, 32)]
    assert sg.day_slots(carried_config({**CFG, "windows": "ampm"})) == [(36, 12), (48, 20)]


# ── overlap + freeness ────────────────────────────────────────────────────────
def test_overlaps():
    assert sg.overlaps(36, 4, 38, 4) is True       # 9:00-10:00 vs 9:30-10:30
    assert sg.overlaps(36, 4, 40, 4) is False      # 9:00-10:00 vs 10:00-11:00 (touching)
    assert sg.overlaps(40, 4, 36, 8) is True


def test_slot_is_free_accepts_tuples_and_dicts():
    assert sg.slot_is_free(36, 4, [(40, 4)]) is True
    assert sg.slot_is_free(36, 4, [(38, 2)]) is False
    assert sg.slot_is_free(36, 4, [{"start_block": 36, "length_blocks": 4}]) is False
    # a legacy row with no grid columns is skipped, not crashed on
    assert sg.slot_is_free(36, 4, [{"start_block": None, "length_blocks": None}]) is True


# ── date gates ────────────────────────────────────────────────────────────────
def test_date_bookable_gates():
    assert sg.date_bookable(CFG, WED, NOW) is True
    assert sg.date_bookable(CFG, datetime(2026, 1, 4).date(), NOW) is False   # Sun (closed)
    assert sg.date_bookable(CFG, datetime(2026, 1, 5).date(), NOW) is True    # today (Mon) — notice handled per-slot
    assert sg.date_bookable(CFG, datetime(2026, 3, 1).date(), NOW) is False   # beyond horizon
    assert sg.date_bookable({**CFG, "blackout_dates": ["2026-01-07"]}, WED, NOW) is False


# ── free_slots: notice + overlap ──────────────────────────────────────────────
def test_free_slots_applies_notice_cutoff():
    # now = Tue 08:00, 24h notice → only slots from Wed 08:00 are offered; the whole
    # Wed working day (09:00+) clears the cutoff, so all 8 slots stand.
    now = datetime(2026, 1, 6, 8, 0)
    assert len(sg.free_slots(CFG, [], WED, now)) == 8
    # Same-day Tue with 24h notice → nothing on Tue is far enough out.
    assert sg.free_slots(CFG, [], datetime(2026, 1, 6).date(), now) == []


def test_free_slots_removes_overlapping_bookings():
    booked = [(36, 4), (44, 4)]              # 09:00-10:00 and 11:00-12:00 taken
    free = sg.free_slots(CFG, booked, WED, NOW)
    assert (36, 4) not in free and (44, 4) not in free
    assert (40, 4) in free and len(free) == 6


# ── availability across resources ─────────────────────────────────────────────
def test_availability_single_implicit_resource():
    out = sg.availability(CFG, [None], {}, WED, NOW)
    assert len(out) == 8
    assert out[0]["label"] == "09:00–10:00" and out[0]["resource_ids"] == [None]


def test_availability_multi_resource_union():
    res = [{"id": "a"}, {"id": "b"}]
    # 'a' is fully booked 09:00-10:00; 'b' is free → slot still offered (by b only).
    out = sg.availability(CFG, res, {"a": [(36, 4)], "b": []}, WED, NOW)
    first = next(s for s in out if s["start_block"] == 36)
    assert first["resource_ids"] == ["b"]
    assert len(out) == 8


def test_availability_skill_filter():
    res = [{"id": "gas", "skills": ["gas"]}, {"id": "plumb", "skills": ["plumbing"]}]
    out = sg.availability(CFG, res, {}, WED, NOW, required_skill="gas")
    assert out and all(s["resource_ids"] == ["gas"] for s in out)
    assert sg.availability(CFG, res, {}, WED, NOW, required_skill="electrical") == []


# ── migration helper ──────────────────────────────────────────────────────────
def test_window_to_blocks_legacy_mapping():
    assert sg.window_to_blocks("day", 60, CFG) == (36, 32)
    assert sg.window_to_blocks("am", 60, CFG) == (36, 12)     # 09:00–12:00
    assert sg.window_to_blocks("pm", 60, CFG) == (48, 20)     # 12:00–17:00
    assert sg.window_to_blocks("14:00", 60, CFG) == (56, 4)   # 2pm, 1h
    assert sg.window_to_blocks("14:00", 90, CFG) == (56, 6)   # 2pm, 90m → 6 blocks
    assert sg.window_to_blocks("nonsense", 60, CFG) is None
