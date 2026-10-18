"""Multi-day stays on the continuous block timeline (app/slotgrid stay_* functions).
The kennel/hotel/hire pattern: book by the UNIT (N units), a booking is a continuous span
across days, and it fits when fewer than `capacity` stays overlap every block it covers.
A 1-hour appointment and a 3-day boarding use the same math — just a different length."""
from datetime import datetime

from app import slotgrid as sg


def _span(drop: datetime, collect: datetime):
    """An existing stay as (abs_start, length_blocks) — what stay_offer expects."""
    lo = sg.datetime_to_abs(drop)
    return (lo, sg.datetime_to_abs(collect) - lo)


# ── the timeline primitives ───────────────────────────────────────────────────
def test_abs_block_roundtrip_and_cross_midnight():
    d = datetime(2026, 6, 30, 12, 0)
    assert sg.abs_to_datetime(sg.datetime_to_abs(d)) == d
    # one block past 23:45 rolls into the next day
    end_of_day = sg.datetime_to_abs(datetime(2026, 6, 30, 23, 45))
    assert sg.abs_to_datetime(end_of_day + 1) == datetime(2026, 7, 1, 0, 0)


def test_kennel_stay_is_208_blocks():
    # Kris's exact example: drop 30 Jun 12:00, collect 2 Jul 16:00.
    lo, length = _span(datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 2, 16, 0))
    assert length == 208                      # 2 days + (16:00-12:00) = 2*96 + 16


def test_stay_span_from_booking_row():
    b = {"booking_date": "2026-06-30", "start_block": 48, "length_blocks": 208}
    assert sg.stay_span(b) == (sg.abs_block("2026-06-30", 48), 208)
    assert sg.stay_span({"start_block": None, "length_blocks": None}) is None


# ── concurrency + fit ─────────────────────────────────────────────────────────
def test_concurrent_count_across_days():
    spans = [
        _span(datetime(2026, 6, 30, 9, 0), datetime(2026, 7, 1, 9, 0)),   # A: 30→1
        _span(datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 3, 16, 0)),  # B: 30→3
        _span(datetime(2026, 7, 1, 8, 0), datetime(2026, 7, 2, 8, 0)),     # C: 1→2
    ]
    # midday on 1 July: A (until 09:00) has left, B + C are in -> 2
    assert sg.concurrent_at(spans, sg.datetime_to_abs(datetime(2026, 7, 1, 12, 0))) == 2
    # 30 Jun 13:00: A + B in -> 2
    assert sg.concurrent_at(spans, sg.datetime_to_abs(datetime(2026, 6, 30, 13, 0))) == 2


def test_kennel_fits_when_under_capacity():
    # 10 spaces; 9 dogs already boarding right across the requested window -> a 10th fits.
    nine = [_span(datetime(2026, 6, 29, 9, 0), datetime(2026, 7, 4, 17, 0)) for _ in range(9)]
    offer = sg.stay_offer(nine, datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 2, 16, 0), capacity=10)
    assert offer["fits"] is True and offer["nights"] == 2
    assert offer["alt_start"] is None


def test_kennel_full_offers_nearest_start():
    # 10 dogs fill every space from 30 Jun through 5 Jul -> the requested stay can't fit,
    # so the agent is handed the nearest start that does (5 Jul 00:00, same length).
    full = [_span(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    offer = sg.stay_offer(full, datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 2, 16, 0), capacity=10)
    assert offer["fits"] is False
    assert offer["alt_start"] == datetime(2026, 7, 5, 0, 0)
    # the offered alternative keeps the same length (208 blocks = 2 days 4 hours later end)
    assert offer["alt_end"] == datetime(2026, 7, 7, 4, 0)


def test_partial_overlap_blocks_only_when_capacity_hit():
    # 10 spaces, all 10 taken for ONE night (1 Jul) only. A stay that spans that night
    # can't fit; one that ends before it can.
    busy_night = [_span(datetime(2026, 7, 1, 0, 0), datetime(2026, 7, 2, 0, 0)) for _ in range(10)]
    over = sg.stay_offer(busy_night, datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 2, 16, 0), capacity=10)
    assert over["fits"] is False and over["alt_start"] == datetime(2026, 7, 2, 0, 0)
    before = sg.stay_offer(busy_night, datetime(2026, 6, 28, 9, 0), datetime(2026, 6, 30, 17, 0), capacity=10)
    assert before["fits"] is True


def test_appointment_is_just_a_short_stay():
    # The same engine handles a 1-hour appointment (4 blocks): one resource, capacity 1.
    spans = [_span(datetime(2026, 6, 30, 9, 0), datetime(2026, 6, 30, 10, 0))]
    clash = sg.stay_offer(spans, datetime(2026, 6, 30, 9, 30), datetime(2026, 6, 30, 10, 30), capacity=1)
    assert clash["fits"] is False and clash["alt_start"] == datetime(2026, 6, 30, 10, 0)
    free = sg.stay_offer(spans, datetime(2026, 6, 30, 10, 0), datetime(2026, 6, 30, 11, 0), capacity=1)
    assert free["fits"] is True
