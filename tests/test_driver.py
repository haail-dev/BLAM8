"""Driver app — the mobile run sheet's pure view-model (TMS baseline).

No DB: one vehicle's bookings + shipment events in, a stop list out.

Run: python -m pytest tests/ -q
"""
from datetime import date

from app.driver import run

DAY = date(2026, 6, 20)
VEH = {"id": "v1", "name": "Van", "registration": "AB12 CDE"}


def _ev(bid, kind, **kw):
    return {"booking_id": bid, "kind": kind,
            "created_at": "2026-06-20T09:00:00+00:00", **kw}


def _bk(bid, vid="v1", date_iso="2026-06-20", spaces=2, **kw):
    return {"id": bid, "booking_ref": f"BK-{bid}", "booking_date": date_iso,
            "summary": f"drop {bid}", "spaces": spaces, "vehicle_id": vid,
            "delivery_postcode": "LS9 8AS", **kw}


def test_run_lists_only_this_vehicle_today_undelivered_first():
    out = run(VEH, [
        _bk("a"),
        _bk("b"),                                  # delivered
        _bk("c", vid="v2"),                        # other vehicle → excluded
        _bk("d", date_iso="2026-06-19"),           # other day → excluded
        {"id": "office", "spaces": None, "booking_date": "2026-06-20", "vehicle_id": "v1"},
    ], [_ev("b", "delivered", signed_by="X")], DAY)
    assert [s["id"] for s in out["stops"]] == ["a", "b"]   # undelivered first; c/d/office gone
    assert out["stops"][0]["done"] is False and out["stops"][1]["done"] is True
    assert out["delivered"] == 1 and out["total"] == 2


def test_run_no_vehicle_is_empty():
    out = run(None, [_bk("a")], [], DAY)
    assert out["stops"] == [] and out["total"] == 0
