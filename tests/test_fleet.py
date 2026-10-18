"""Fleet module — the pure capacity math (docs/modules/module-fleet-capacity.md, build step 2).

Vehicles are the unit of capacity (D1/D2): demand = Σ qty × pallet-type factor
(unknown type → full, conservative), supply = the active vehicle pool, and an
order rides ONE vehicle-day chosen best-fit — smallest remaining space that
still fits, ties to smallest capacity then id, so selection is deterministic
and big vans stay free for big orders. Weight is the legal second constraint,
enforced only when the order states one AND the vehicle has a limit. These
tests pin that math without a DB; the hold_fleet_booking RPC makes the same
choice atomically and is exercised live.

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

import pytest

from app.scheduling import (
    DIARY_TZ, _day_loads, best_fit, fleet_availability, fleet_day_groups,
    fleet_diary_line, fleet_weeks, fmt_spaces, order_spaces, pallet_factors,
    resolve_branch,
)

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

SMALL = {"id": "v-aaa", "name": "Sprinter", "capacity_spaces": 4,
         "max_payload_kg": 1000, "active": True, "branch_id": "br-leeds"}
BIG = {"id": "v-bbb", "name": "18t curtainsider", "capacity_spaces": 10,
       "max_payload_kg": 9000, "active": True, "branch_id": "br-manc"}
NO_LIMIT = {"id": "v-ccc", "name": "7.5t", "capacity_spaces": 6,
            "max_payload_kg": None, "active": True, "branch_id": None}

BRANCHES = [{"id": "br-leeds", "name": "Leeds"}, {"id": "br-manc", "name": "Manchester"}]


def b(d, vehicle_id=None, spaces=None, weight_kg=None, status="confirmed", **extra):
    return {"booking_date": d, "vehicle_id": vehicle_id, "spaces": spaces,
            "weight_kg": weight_kg, "status": status, "window": "day", **extra}


# ── factors & spaces arithmetic (D2) ─────────────────────────────────────────
def test_fmt_spaces_trims_trailing_zeros():
    assert fmt_spaces(6.5) == "6.5"
    assert fmt_spaces(8.0) == "8"
    assert fmt_spaces(0.25) == "0.25"


def test_pallet_factors_builtin_plus_tenant_overrides():
    factors = pallet_factors([{"name": "Half", "space_factor": 0.4},
                              {"name": "xl", "space_factor": 2.0}])
    assert factors["half"] == 0.4          # tenant row overrides the built-in
    assert factors["full"] == 1.0          # built-ins survive an unseeded tenant
    assert factors["quarter"] == 0.25
    assert factors["xl"] == 2.0


def test_order_spaces_sums_qty_times_factor():
    spaces, assumed = order_spaces(pallet_factors(None),
                                   [{"type": "full", "qty": 6}, {"type": "half", "qty": 2}])
    assert (spaces, assumed) == (7.0, [])


def test_order_spaces_unknown_type_counts_full_and_says_so():
    spaces, assumed = order_spaces(pallet_factors(None),
                                   [{"type": "XL Plus", "qty": 2}, {"type": "", "qty": 1}])
    assert spaces == 3.0                    # conservative: both counted as full
    assert assumed == ["xl plus", "unspecified"]


def test_order_spaces_rejects_junk():
    factors = pallet_factors(None)
    for bad in ([], "six", [{"type": "full", "qty": 0}], [{"type": "full", "qty": -2}],
                [{"type": "full", "qty": "lots"}], [{"type": "full"}],
                [{"type": "full", "qty": 1e7}]):
        with pytest.raises((ValueError, TypeError, KeyError)):
            order_spaces(factors, bad)


# ── branch resolution chain (D4) ─────────────────────────────────────────────
def test_resolve_branch_explicit_name_case_insensitive():
    assert resolve_branch(BRANCHES, explicit="  leeds ") == ("br-leeds", None, ["Leeds", "Manchester"])


def test_resolve_branch_unknown_name():
    bid, err, names = resolve_branch(BRANCHES, explicit="Hull")
    assert (bid, err) == (None, "unknown_branch")
    assert names == ["Leeds", "Manchester"]


def test_resolve_branch_mailbox_default_then_ask():
    # leg 2: the receiving mailbox's default
    assert resolve_branch(BRANCHES, default_branch_id="br-manc")[0] == "br-manc"
    # explicit beats the default
    assert resolve_branch(BRANCHES, explicit="Leeds", default_branch_id="br-manc")[0] == "br-leeds"
    # a stale default (branch deleted) falls through to the ask
    assert resolve_branch(BRANCHES, default_branch_id="br-gone")[1] == "branch_required"
    # >1 branches and no signal → the agent asks the customer
    assert resolve_branch(BRANCHES)[1] == "branch_required"


def test_resolve_branch_single_or_zero_branch_is_whole_fleet():
    one = [{"id": "br-main", "name": "Main"}]
    assert resolve_branch(one) == (None, None, ["Main"])      # never thinks about it
    assert resolve_branch([]) == (None, None, [])
    assert resolve_branch(one, default_branch_id="br-gone")[1] is None


# ── best-fit selection (D1) ──────────────────────────────────────────────────
def test_best_fit_smallest_remaining_space_wins():
    loads = _day_loads([SMALL, BIG], [], date(2026, 6, 18))
    pick = best_fit(loads, 3)
    assert pick["vehicle"]["id"] == "v-aaa"   # 4 free beats 10 free — big van stays big


def test_best_fit_skips_vehicles_too_small_or_too_loaded():
    booked = [b("2026-06-18", vehicle_id="v-bbb", spaces=8)]
    loads = _day_loads([SMALL, BIG], booked, date(2026, 6, 18))
    assert best_fit(loads, 6) is None         # small holds 4, big has 2 left
    assert best_fit(loads, 2)["vehicle"]["id"] == "v-bbb"  # 2 free beats 4 free


def test_best_fit_ties_break_smallest_capacity_then_id():
    half_loaded_big = [b("2026-06-18", vehicle_id="v-bbb", spaces=6)]   # big: 4 free
    loads = _day_loads([SMALL, BIG], half_loaded_big, date(2026, 6, 18))
    assert best_fit(loads, 4)["vehicle"]["id"] == "v-aaa"   # both 4 free; capacity 4 < 10
    twin = {**SMALL, "id": "v-zzz", "branch_id": None}
    loads = _day_loads([twin, SMALL], [], date(2026, 6, 18))
    assert best_fit(loads, 2)["vehicle"]["id"] == "v-aaa"   # full tie → id order


def test_best_fit_weight_is_the_legal_second_constraint():
    loads = _day_loads([SMALL, BIG], [], date(2026, 6, 18))
    assert best_fit(loads, 2, weight_kg=1500)["vehicle"]["id"] == "v-bbb"  # 1500 > Sprinter's 1000
    assert best_fit(loads, 2)["vehicle"]["id"] == "v-aaa"                  # no stated weight → spaces only
    assert best_fit(loads, 2, weight_kg=20000) is None                     # over every limit
    # a vehicle with NO limit accepts any stated weight
    assert best_fit(_day_loads([NO_LIMIT], [], date(2026, 6, 18)), 2, weight_kg=20000)


def test_best_fit_weight_accumulates_only_declared_weights():
    booked = [b("2026-06-18", vehicle_id="v-aaa", spaces=1, weight_kg=800),
              b("2026-06-18", vehicle_id="v-aaa", spaces=1)]   # undeclared → 0 kg
    loads = _day_loads([SMALL], booked, date(2026, 6, 18))
    assert best_fit(loads, 1, weight_kg=300) is None           # 800 + 300 > 1000
    assert best_fit(loads, 1, weight_kg=150)["vehicle"]["id"] == "v-aaa"


def test_one_vehicle_per_order_never_splits():
    """Two vans with 4 free each could carry 6 between them — D1 says no."""
    loads = _day_loads([SMALL, {**SMALL, "id": "v-ddd"}], [], date(2026, 6, 18))
    assert best_fit(loads, 6) is None


def test_day_loads_ignores_inactive_statuses():
    booked = [b("2026-06-18", vehicle_id="v-aaa", spaces=2, status="cancelled"),
              b("2026-06-18", vehicle_id="v-aaa", spaces=1, status="proposed")]
    loads = _day_loads([SMALL], booked, date(2026, 6, 18))
    assert loads[0]["used_spaces"] == 1     # holds count (D4); cancelled doesn't


# ── fleet availability (the tool's math) ─────────────────────────────────────
def test_fleet_availability_filters_days_like_windows_mode():
    out = fleet_availability(carried_config(None), [SMALL, BIG], [], date(2026, 6, 16), date(2026, 6, 21), NOW)
    # Tue: started; Wed: 24h notice; Sat/Sun: closed — exactly the windows rules.
    assert [(s["date"], s["remaining_spaces"]) for s in out] == [
        ("2026-06-18", 10.0), ("2026-06-19", 10.0)]   # largest single vehicle, not fleet total


def test_fleet_availability_with_load_returns_only_fitting_days():
    booked = [b("2026-06-18", vehicle_id="v-bbb", spaces=7)]
    out = fleet_availability(carried_config(None), [SMALL, BIG], booked, date(2026, 6, 16), date(2026, 6, 21),
                             NOW, spaces=6)
    assert out == [{"date": "2026-06-19", "remaining_spaces": 10.0, "fits": True}]


def test_fleet_availability_weight_blocks_a_day():
    booked = [b("2026-06-18", vehicle_id="v-aaa", spaces=1, weight_kg=800)]
    out = fleet_availability(carried_config(None), [SMALL], booked, date(2026, 6, 18), date(2026, 6, 19),
                             NOW, spaces=1, weight_kg=300)
    assert [s["date"] for s in out] == ["2026-06-19"]   # Thu fails on kg, not spaces


def test_fleet_availability_blackouts_and_horizon_apply():
    cfg = carried_config({"blackout_dates": ["2026-06-18"], "horizon_days": 3})
    out = fleet_availability(cfg, [SMALL], [], date(2026, 6, 16), date(2026, 6, 30), NOW)
    assert [s["date"] for s in out] == ["2026-06-19"]


# ── the fleet board view-model (D6) ──────────────────────────────────────────
def test_fleet_day_groups_badges_and_buckets():
    inactive = {"id": "v-old", "name": "Retired", "capacity_spaces": 8,
                "max_payload_kg": None, "active": False, "branch_id": None}
    booked = [
        b("2026-06-18", vehicle_id="v-bbb", spaces=3),
        b("2026-06-18", vehicle_id="v-old", spaces=2),       # on the inactive van
        b("2026-06-18", spaces=2),                           # unassigned (vehicle deleted)
        b("2026-06-18", summary="old windows booking"),      # legacy: no spaces
    ]
    days = fleet_day_groups(carried_config({"horizon_days": 6}), [SMALL, BIG, inactive], booked, NOW)
    thu = next(d for d in days if d["date"] == "2026-06-18")
    assert thu["total_spaces"] == 14.0          # active pool only (4 + 10)
    assert thu["used_spaces"] == 3.0            # the inactive van's load isn't pool load
    assert len(thu["unassigned"]) == 1 and len(thu["legacy"]) == 1
    assert {r["vehicle"]["id"] for r in thu["vehicles"]} == {"v-aaa", "v-bbb", "v-old"}
    wed = next(d for d in days if d["date"] == "2026-06-17")
    assert {r["vehicle"]["id"] for r in wed["vehicles"]} == {"v-aaa", "v-bbb"}  # idle inactive hidden
    assert thu["full"] is False and thu["booked_count"] == 3


def test_fleet_day_groups_branch_filter_scopes_everything():
    booked = [b("2026-06-18", vehicle_id="v-bbb", spaces=3),
              b("2026-06-18", spaces=1)]                     # unassigned shows everywhere
    days = fleet_day_groups(carried_config({"horizon_days": 6}), [SMALL, BIG], booked, NOW,
                            branch_id="br-leeds")
    thu = next(d for d in days if d["date"] == "2026-06-18")
    assert thu["total_spaces"] == 4.0
    assert thu["used_spaces"] == 0.0            # Manchester's load is invisible here
    assert [r["vehicle"]["id"] for r in thu["vehicles"]] == ["v-aaa"]
    assert len(thu["unassigned"]) == 1


def test_fleet_weeks_pads_like_the_windows_calendar():
    """Same shape as the windows calendar, including the month split it grew on
    2026-09-02 — the board opens on the 1st, not on the Monday of today's week,
    and a week never carries days from two months."""
    weeks = fleet_weeks(carried_config({"horizon_days": 6}), [SMALL], [], NOW)
    assert weeks[0]["start"] == "2026-06-01"          # 1 June 2026 is a Monday
    this_week = next(w for w in weeks if w["start"] == "2026-06-15")
    assert [d["dom"] for d in this_week["days"]] == [15, 16, 17, 18, 19, 20, 21]
    tue = this_week["days"][1]
    assert tue["today"] is True and tue["in_diary"] and tue["total_spaces"] == 4.0
    assert this_week["days"][0]["out"] is True
    for w in weeks:
        assert len({d["month_label"] for d in w["days"]}) == 1


# ── the one-line fleet diary shape ───────────────────────────────────────────
def test_fleet_diary_line_counts_and_branches():
    line = fleet_diary_line(carried_config(None), [SMALL, BIG], BRANCHES)
    assert "2 vehicles" in line and "14 pallet spaces" in line
    assert "Leeds, Manchester" in line and "Mon" in line


def test_fleet_diary_line_zero_vehicles_is_honest():
    line = fleet_diary_line(carried_config(None), [{**SMALL, "active": False}], [])
    assert "no vehicles configured" in line
