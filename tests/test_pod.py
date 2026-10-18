"""Proof of delivery — the POD board's pure view-model (TMS baseline).

No DB (à la test_tms.py): bookings + shipment_event dicts in, board rows out.

Run: python -m pytest tests/ -q
"""
from datetime import datetime

from app.pod import board, latest_state
from app.scheduling import DIARY_TZ

NOW = datetime(2026, 6, 20, 10, 0, tzinfo=DIARY_TZ)


def _ev(bid, kind, **kw):
    return {"booking_id": bid, "kind": kind,
            "created_at": "2026-06-20T09:00:00+00:00", **kw}


def _bk(bid, date_iso="2026-06-20", spaces=3, **kw):
    return {"id": bid, "booking_ref": f"BK-{bid}", "booking_date": date_iso,
            "summary": "job", "spaces": spaces, "vehicle_id": "v1", **kw}


def test_latest_state_picks_newest_with_signature():
    st = latest_state([_ev("b", "booked"), _ev("b", "out_for_delivery"),
                       _ev("b", "delivered", signed_by="J. Smith")])
    assert st["kind"] == "delivered" and st["signed_by"] == "J. Smith"
    assert st["colour"] == "emerald"


def test_latest_state_empty_is_awaiting():
    assert latest_state([])["kind"] == "pending"


def test_board_filters_window_office_rows_and_sorts_delivered_last():
    out = board([
        _bk("b1"),                                                       # in window, no events
        _bk("b2"),                                                       # delivered below
        {"id": "office", "spaces": None, "booking_date": "2026-06-20", "summary": "call"},
        _bk("b3", date_iso="2026-05-01"),                                # too old → excluded
    ], [_ev("b2", "delivered", signed_by="A")], NOW, vehicle_names={"v1": "Van"})
    ids = [r["id"] for r in out["rows"]]
    assert "office" not in ids and "b3" not in ids
    assert out["delivered"] == 1 and out["total"] == 2
    assert out["rows"][0]["id"] == "b1" and out["rows"][-1]["id"] == "b2"   # delivered last
    assert out["rows"][0]["vehicle"] == "Van"
