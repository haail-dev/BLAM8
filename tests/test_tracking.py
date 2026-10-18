"""Live tracking + customer portal — pure view-models (TMS baseline).

No DB: vehicle/booking/event dicts in, simulated fleet positions and a public
timeline out. The simulation is deliberately simple (next undelivered drop) —
the point is the shape a real tracker would fill.

Run: python -m pytest tests/ -q
"""
from datetime import date

from app.tracking import fleet_positions, public_timeline

DAY = date(2026, 6, 20)
V1 = {"id": "v1", "name": "Van", "active": True}


def _ev(bid, kind, **kw):
    return {"booking_id": bid, "kind": kind,
            "created_at": "2026-06-20T09:00:00+00:00", **kw}


def _bk(bid, vid="v1", date_iso="2026-06-20", **kw):
    return {"id": bid, "booking_ref": f"BK-{bid}", "booking_date": date_iso,
            "summary": "drop", "spaces": 2, "vehicle_id": vid,
            "delivery_postcode": "LS9 8AS", **kw}


def test_positions_progress_and_next_target():
    rows = fleet_positions([V1], [
        _bk("a", delivery_postcode="LS1 1AA"),
        _bk("b", delivery_postcode="LS9 8AS"),
    ], [_ev("a", "delivered")], DAY, depot_postcode="WF17 9LU")
    assert len(rows) == 1
    r = rows[0]
    assert r["delivered"] == 1 and r["total"] == 2 and r["pct"] == 50
    assert r["status"] == "En route" and r["postcode"] == "LS9 8AS"   # next undelivered drop


def test_positions_back_at_depot_when_done():
    rows = fleet_positions([V1], [_bk("a")], [_ev("a", "delivered")], DAY,
                           depot_postcode="WF17 9LU")
    assert rows[0]["status"] == "Back at depot" and rows[0]["postcode"] == "WF17 9LU"
    assert rows[0]["pct"] == 100


def test_positions_skip_idle_and_inactive_vehicles():
    rows = fleet_positions(
        [V1, {"id": "v2", "name": "Idle", "active": True},
         {"id": "v3", "name": "Dead", "active": False}],
        [_bk("a", vid="v1")], [], DAY)
    assert [r["id"] for r in rows] == ["v1"]   # v2 has no drops, v3 inactive


def test_public_timeline_delivered_marks_all_prior_done():
    t = public_timeline(_bk("a"), [_ev("a", "out_for_delivery"),
                                   _ev("a", "delivered", signed_by="J. Smith")])
    assert t["delivered"] is True and t["signed_by"] == "J. Smith"
    steps = {s["key"]: s for s in t["steps"]}
    assert steps["delivered"]["done"] and steps["delivered"]["current"]
    assert steps["loaded"]["done"] is True     # everything before the furthest step is done


def test_public_timeline_booked_only_and_failed():
    booked = public_timeline(_bk("a"), [])
    steps = {s["key"]: s for s in booked["steps"]}
    assert steps["booked"]["done"] and steps["booked"]["current"]
    assert steps["delivered"]["done"] is False and booked["delivered"] is False

    failed = public_timeline(_bk("a"), [_ev("a", "failed", note="nobody home")])
    assert failed["failed"] is True and failed["delivered"] is False
