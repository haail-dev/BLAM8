"""Scheduling module — availability math (docs/modules/module-scheduling.md, build step 1-2).

The diary is a capacity diary, not a calendar engine (D1): a booking occupies one
window ('day', or 'am'/'pm' when the tenant runs half-days) on one date, against a
configured capacity per window. These tests pin the pure math in src/app/scheduling.py:

  - working days / blackouts filter dates ("closed")
  - min notice + horizon bound the bookable range ("outside_horizon"); a window is
    bookable only if its START (day/am 00:00, pm 12:00) is at/after now + notice —
    conservative by design: the agent can be too cautious, never wrong
  - 'ampm' vs 'day' window modes, including the overlap rule: a legacy 'day'
    booking blocks both halves; in 'day' mode every active booking counts
  - only proposed + confirmed consume capacity (holds count — D4)
  - place_hold: validates before calling the DB, maps an empty RPC result to 'full'

All diary math runs in Europe/London (DIARY_TZ). Times in tests are fixed and
timezone-aware, so they are deterministic on any machine.

Run: python -m pytest tests/ -q   (from the project root)
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

import pytest

from app import scheduling
from app.scheduling import DIARY_TZ, availability, hold_error, place_hold

# Tuesday 16 June 2026, 10:00 in the diary timezone (today is Wed 10 June 2026).
NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)


def slots(result):
    """(date, window) pairs, for compact assertions."""
    return [(s["date"], s["window"]) for s in result]


# ── defaults: working week, 24h notice, capacity 4/day ───────────────────────
def test_carried_over_default_week():
    out = availability(carried_config(None), [], date(2026, 6, 16), date(2026, 6, 21), NOW)
    # Tue 16: window already started; Wed 17: blocked by 24h notice (day starts
    # 00:00 < Wed 10:00 cutoff); Sat 20 / Sun 21: not working days.
    assert slots(out) == [("2026-06-18", "day"), ("2026-06-19", "day")]
    assert all(s["remaining"] == 4 for s in out)


def test_iso_string_dates_accepted():
    out = availability(carried_config({}), [], "2026-06-16", "2026-06-21", NOW)
    assert slots(out) == [("2026-06-18", "day"), ("2026-06-19", "day")]


# ── min notice against window starts (day/am 00:00, pm 12:00) ────────────────
def test_pm_window_bookable_same_day_when_notice_allows():
    cfg = carried_config({"windows": "ampm", "min_notice_hours": 1})
    out = availability(cfg, [], date(2026, 6, 16), date(2026, 6, 17), NOW)
    # Cutoff Tue 11:00 — Tue am started at 00:00 (gone), Tue pm starts 12:00 (open).
    assert slots(out) == [
        ("2026-06-16", "pm"),
        ("2026-06-17", "am"),
        ("2026-06-17", "pm"),
    ]


def test_zero_notice_still_excludes_started_windows():
    out = availability(carried_config({"min_notice_hours": 0}), [], date(2026, 6, 16), date(2026, 6, 17), NOW)
    assert slots(out) == [("2026-06-17", "day")]  # today's day window began at 00:00


# ── horizon + range clamping ──────────────────────────────────────────────────
def test_horizon_clamps_far_future():
    cfg = carried_config({"min_notice_hours": 0, "working_days": "mon,tue,wed,thu,fri,sat,sun", "horizon_days": 3})
    out = availability(cfg, [], date(2026, 6, 16), date(2026, 6, 30), NOW)
    assert slots(out) == [
        ("2026-06-17", "day"),
        ("2026-06-18", "day"),
        ("2026-06-19", "day"),  # today + 3 = last bookable date
    ]


def test_past_from_date_clamped_and_inverted_range_empty():
    out = availability(carried_config({}), [], date(2026, 6, 1), date(2026, 6, 19), NOW)
    assert slots(out) == [("2026-06-18", "day"), ("2026-06-19", "day")]
    assert availability(carried_config({}), [], date(2026, 6, 19), date(2026, 6, 16), NOW) == []


# ── working days + blackouts ──────────────────────────────────────────────────
def test_custom_working_days():
    cfg = carried_config({"min_notice_hours": 0, "working_days": "mon,wed"})
    out = availability(cfg, [], date(2026, 6, 16), date(2026, 6, 29), NOW)
    assert [s["date"] for s in out] == ["2026-06-17", "2026-06-22", "2026-06-24", "2026-06-29"]


def test_working_days_parse_is_tolerant():
    cfg = carried_config({"min_notice_hours": 0, "working_days": "Mon, Wed"})
    out = availability(cfg, [], date(2026, 6, 16), date(2026, 6, 22), NOW)
    assert [s["date"] for s in out] == ["2026-06-17", "2026-06-22"]


def test_blackout_dates_excluded():
    cfg = carried_config({"blackout_dates": ["2026-06-18"]})  # as PostgREST returns date[]
    out = availability(cfg, [], date(2026, 6, 16), date(2026, 6, 21), NOW)
    assert slots(out) == [("2026-06-19", "day")]


# ── capacity counting ─────────────────────────────────────────────────────────
def booked(d, window, status="confirmed"):
    return {"booking_date": d, "window": window, "status": status}


def test_remaining_decrements_and_full_window_disappears():
    rows = [booked("2026-06-18", "day"), booked("2026-06-18", "day", "proposed")]
    out = availability(carried_config({}), rows, date(2026, 6, 18), date(2026, 6, 19), NOW)
    assert out[0] == {"date": "2026-06-18", "window": "day", "remaining": 2}

    full = rows * 2  # 4 active bookings = capacity
    out = availability(carried_config({}), full, date(2026, 6, 18), date(2026, 6, 19), NOW)
    assert slots(out) == [("2026-06-19", "day")]


def test_day_mode_counts_bookings_of_any_window():
    cfg = carried_config({"capacity_per_window": 3})
    rows = [booked("2026-06-18", "am"), booked("2026-06-18", "pm"), booked("2026-06-18", "day")]
    out = availability(cfg, rows, date(2026, 6, 18), date(2026, 6, 18), NOW)
    assert out == []  # 3 active on the date, capacity 3 — day window full


def test_ampm_mode_legacy_day_booking_blocks_both_halves():
    cfg = carried_config({"windows": "ampm", "capacity_per_window": 2})
    rows = [booked("2026-06-18", "am", "proposed"), booked("2026-06-18", "day")]
    out = availability(cfg, rows, date(2026, 6, 18), date(2026, 6, 18), NOW)
    # am: 2 - (am + day) = 0 → gone; pm: 2 - (day) = 1.
    assert out == [{"date": "2026-06-18", "window": "pm", "remaining": 1}]


def test_only_proposed_and_confirmed_consume_capacity():
    rows = [
        booked("2026-06-18", "day", "cancelled"),
        booked("2026-06-18", "day", "declined"),
        booked("2026-06-18", "day", "completed"),
    ]
    out = availability(carried_config({"capacity_per_window": 1}), rows, date(2026, 6, 18), date(2026, 6, 18), NOW)
    assert out == [{"date": "2026-06-18", "window": "day", "remaining": 1}]


# ── hold validation (the executor's pre-flight; capacity itself is the RPC's) ─
def test_hold_error_invalid_window_for_mode():
    assert hold_error(carried_config({}), date(2026, 6, 18), "am", NOW) == "invalid_window"
    assert hold_error(carried_config({"windows": "ampm"}), date(2026, 6, 18), "day", NOW) == "invalid_window"
    assert hold_error(carried_config({}), date(2026, 6, 18), "evening", NOW) == "invalid_window"


def test_hold_error_outside_horizon():
    assert hold_error(carried_config({}), date(2026, 6, 17), "day", NOW) == "outside_horizon"  # < 24h notice
    assert hold_error(carried_config({}), date(2026, 6, 10), "day", NOW) == "outside_horizon"  # past
    assert hold_error(carried_config({}), date(2026, 7, 14), "day", NOW) is None               # today + 28
    assert hold_error(carried_config({}), date(2026, 7, 15), "day", NOW) == "outside_horizon"  # today + 29


def test_hold_error_closed_days():
    assert hold_error(carried_config({}), date(2026, 6, 20), "day", NOW) == "closed"  # Saturday
    cfg = carried_config({"blackout_dates": ["2026-06-18"]})
    assert hold_error(cfg, date(2026, 6, 18), "day", NOW) == "closed"
    assert hold_error(carried_config({}), date(2026, 6, 18), "day", NOW) is None


# ── place_hold: validate → RPC → row | 'full' ────────────────────────────────
def test_place_hold_rejects_before_touching_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("RPC must not be called when validation fails")

    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", boom)
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    row, err = place_hold("t1", "2026-06-20", "day", "3 pallets, M17 -> LS9", now=NOW)
    assert (row, err) == (None, "closed")


def test_place_hold_full_and_success(monkeypatch):
    calls = {}

    def fake_rpc(tenant_id, booking_date, window, summary, **kwargs):
        calls.update(tenant_id=tenant_id, booking_date=booking_date, window=window,
                     summary=summary, **kwargs)
        return calls.pop("_return", None)

    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", fake_rpc)

    row, err = place_hold("t1", date(2026, 6, 18), "day", "3 pallets", now=NOW)
    assert (row, err) == (None, "full")

    calls["_return"] = {"id": "b-1", "status": "proposed"}
    row, err = place_hold(
        "t1", "2026-06-18", "day", "3 pallets",
        details={"pallets": 3}, contact="ops@acme.co.uk", thread_id="th-9", now=NOW,
    )
    assert err is None and row["id"] == "b-1"
    assert calls["booking_date"] == "2026-06-18"
    assert calls["contact"] == "ops@acme.co.uk"
    assert calls["details"] == {"pallets": 3}
    assert calls["thread_id"] == "th-9"


# ── check_availability: the tool-facing wrapper (config + bookings from db) ──
def test_check_availability_wires_config_and_bookings(monkeypatch):
    monkeypatch.setattr(
        scheduling.db, "get_scheduling_config",
        lambda tid, client=None: carried_config({"tenant_id": tid, "capacity_per_window": 2}),
    )
    monkeypatch.setattr(
        scheduling.db, "list_active_bookings",
        lambda tid, dfrom, dto, client=None: [booked("2026-06-18", "day")],
    )
    out = scheduling.check_availability("t1", "2026-06-16", "2026-06-19", now=NOW)
    assert out == [
        {"date": "2026-06-18", "window": "day", "remaining": 1},
        {"date": "2026-06-19", "window": "day", "remaining": 2},
    ]
