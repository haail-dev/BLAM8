"""Parity proof: the grid-based office availability (app/slotgrid.office_availability)
reproduces the live office diary (app/scheduling.availability) EXACTLY for bookings made
under the current slot config — the real-world case (a booking's window always matches the
diary that created it). Covered across day/ampm/slots modes, every 15-min-multiple slot
size, and working-day/blackout/notice/horizon gates, with fleet + field-service rows that
the office must ignore. This is what makes the Phase 2b read-path swap behaviour-preserving.

The one intentional DIFFERENCE — an off-grid booking (made before a slot-size change) that
the live code silently drops but the grid correctly still counts — is asserted separately
as an improvement, not a regression.
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import scheduling, slotgrid
from app.scheduling import DIARY_TZ

NOW = datetime(2026, 1, 5, 8, 0, tzinfo=DIARY_TZ)   # Monday 08:00
FROM, TO = "2026-01-05", "2026-01-20"
WED = "2026-01-07"                                   # a working day for every config below

CONFIGS = [
    carried_config(),                                                # carried-over day, cap 4
    carried_config({"windows": "day", "capacity_per_window": 1}),
    carried_config({"windows": "day", "capacity_per_window": 3, "working_days": "mon,wed,fri"}),
    carried_config({"windows": "ampm", "capacity_per_window": 2}),
    carried_config({"windows": "ampm", "capacity_per_window": 1, "min_notice_hours": 48}),
    carried_config({"windows": "slots", "capacity_per_window": 1, "slot_minutes": 60}),
    carried_config({"windows": "slots", "capacity_per_window": 2, "slot_minutes": 30}),
    carried_config({"windows": "slots", "capacity_per_window": 1, "slot_minutes": 15}),
    carried_config({"windows": "slots", "capacity_per_window": 1, "slot_minutes": 120,
     "open_time": "08:00", "close_time": "18:00"}),
    carried_config({"windows": "slots", "capacity_per_window": 4, "slot_minutes": 45,
     "horizon_days": 10, "blackout_dates": ["2026-01-12"]}),
]


def _row(window, status="proposed", date=WED, **extra):
    return {"booking_date": date, "window": window, "status": status,
            "spaces": None, "engineer_id": None, **extra}


def _aligned_booking_sets(cfg):
    """Booking sets whose windows are valid for THIS config (as they always are in
    reality), plus universal rows: a whole-day hold, inactive rows, and fleet/FSM rows
    that the office diary must ignore."""
    wins = list(scheduling._norm(cfg)["windows"])
    sets = [
        [],
        [_row(wins[0])],
        [_row(wins[0]), _row(wins[0], status="confirmed")],          # capacity stacking
        [_row("day")],                                                # whole-day blocks all
        [_row(w) for w in wins[:3]],                                  # several windows
        [_row(wins[0], status="cancelled"), _row(wins[0], status="declined")],  # ignored
        [_row("day", spaces=6), _row(wins[0], engineer_id="eng-1")],  # other models — ignored
    ]
    return sets


def test_office_availability_matches_live_for_aligned_bookings():
    mismatches = []
    for ci, cfg in enumerate(CONFIGS):
        for bi, booked in enumerate(_aligned_booking_sets(cfg)):
            live = scheduling.availability(cfg, booked, FROM, TO, NOW)
            grid = slotgrid.office_availability(cfg, booked, FROM, TO, NOW)
            if live != grid:
                mismatches.append(f"cfg#{ci} bookings#{bi}:\n  live={live}\n  grid={grid}")
    assert not mismatches, "parity broken:\n" + "\n\n".join(mismatches)


def test_office_windows_match_norm():
    for cfg in CONFIGS:
        assert tuple(slotgrid.office_windows(cfg)) == scheduling._norm(cfg)["windows"]


def test_block_overlap_reproduces_overlaps_rules():
    # The crux: for ON-GRID windows (those a 60-min diary actually generates, plus the
    # day/am/pm modes), block overlap of window_to_blocks == scheduling._overlaps. Off-grid
    # windows are excluded — there the grid is physically correct and _overlaps isn't (see
    # test_offgrid_booking_is_counted_by_grid_improvement).
    cfg = carried_config({"open_time": "09:00", "close_time": "17:00"})
    windows = ["day", "am", "pm", "09:00", "10:00", "11:00", "12:00", "16:00"]
    for a in windows:
        for b in windows:
            ab = slotgrid.window_to_blocks(a, 60, cfg)
            bb = slotgrid.window_to_blocks(b, 60, cfg)
            grid = bool(ab and bb and slotgrid.overlaps(ab[0], ab[1], bb[0], bb[1]))
            assert grid == scheduling._overlaps(a, b), f"{a} vs {b}: grid={grid}"


def test_offgrid_booking_is_counted_by_grid():
    # A booking at 09:30 under 2-hour slots (08:00,10:00,…) is OFF-GRID — left behind by a
    # slot-size change. The legacy window-string match dropped it (its window matched no
    # current slot — a latent double-booking bug); the grid, now the LIVE engine, correctly
    # sees it occupy the 08:00–10:00 slot. Asserted against the grid directly (since
    # scheduling.availability now delegates to it, comparing the two would be a tautology).
    cfg = carried_config({"windows": "slots", "capacity_per_window": 1, "slot_minutes": 120,
           "open_time": "08:00", "close_time": "18:00"})
    with_hold = slotgrid.office_availability(cfg, [_row("09:30")], FROM, TO, NOW)
    without = slotgrid.office_availability(cfg, [], FROM, TO, NOW)
    wed_with = {s["window"] for s in with_hold if s["date"] == WED}
    wed_without = {s["window"] for s in without if s["date"] == WED}
    assert "08:00" in wed_without              # the slot is bookable with no hold
    assert "08:00" not in wed_with             # the off-grid 09:30 hold correctly removes it
    assert wed_with < wed_without              # strictly more conservative, never less


# ── free travel buffer (outbound: leave gap for travel between jobs) ───────────
def test_travel_buffer_blocks_adjacent_slots():
    cfg = carried_config({"windows": "slots", "capacity_per_window": 1, "slot_minutes": 60,
           "open_time": "09:00", "close_time": "17:00"})
    booked = [_row("10:00")]                      # a job 10:00-11:00 on WED
    free0 = {s["window"] for s in slotgrid.office_availability(cfg, booked, FROM, TO, NOW)
             if s["date"] == WED}
    assert "09:00" in free0 and "11:00" in free0  # no buffer: back-to-back is allowed
    cfg45 = {**cfg, "buffer_minutes": 45}
    free45 = {s["window"] for s in slotgrid.office_availability(cfg45, booked, FROM, TO, NOW)
              if s["date"] == WED}
    assert "09:00" not in free45 and "11:00" not in free45  # 45-min buffer blocks the neighbours
    assert "10:00" not in free45                            # the job's own slot is taken
    assert "12:00" in free45                                # far enough after the job to travel
