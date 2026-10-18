"""Pricing — the rate card → quote math (TMS baseline).

Pure-function tests, no DB (à la test_tms.py): a rate card + booking dicts + a
postcode coord index in, quote breakdowns out.

Run: python -m pytest tests/ -q
"""
from app.pricing import DEFAULT_RATE, miles_between, quote, rows_with_quotes

RATE = {"base_charge": 20, "per_mile": 2, "per_pallet": 10, "per_drop": 5,
        "min_charge": 50, "fuel_pct": 10}
IDX = {"WF17 9LU": {"lat": 53.71, "lng": -1.67},
       "LS9 8AS": {"lat": 53.79, "lng": -1.51}}


def test_quote_breakdown_adds_up():
    q = quote(RATE, spaces=3, miles=10)
    assert q["subtotal"] == 75.0          # 20 base + 20 miles + 30 pallets + 5 drop
    assert q["fuel"] == 7.5               # 10% of 75
    assert q["total"] == 82.5 and q["min_applied"] is False
    assert q["miles"] == 10.0


def test_quote_applies_minimum_charge():
    q = quote(RATE, spaces=0, miles=0)
    assert q["subtotal"] == 25.0          # 20 base + 5 drop
    assert q["total"] == 50.0 and q["min_applied"] is True   # 27.5 floored to 50


def test_quote_without_miles_zeroes_distance_and_flags_none():
    q = quote(RATE, spaces=2, miles=None)
    assert q["miles"] is None and q["mile_charge"] == 0.0
    assert q["total"] == 50.0             # 45 +10% = 49.5, floored to 50


def test_quote_defaults_when_no_rate_card():
    q = quote(None, spaces=1, miles=0)
    assert q["currency"] == "GBP" and q["total"] >= DEFAULT_RATE["min_charge"]


def test_miles_between_needs_both_geocoded():
    assert miles_between("WF17 9LU", "LS9 8AS", IDX) > 0
    assert miles_between("WF17 9LU", None, IDX) is None
    assert miles_between("WF17 9LU", "ZZ1 1ZZ", IDX) is None   # not in the cache


def _bk(**kw):
    return {"id": "b1", "booking_ref": "BK-1001", "booking_date": "2026-06-20",
            "summary": "3 pallets", "spaces": 3, **kw}


def test_rows_skip_office_rows_and_sort_newest_first():
    rows = rows_with_quotes(RATE, [
        _bk(collection_postcode="wf17 9lu", delivery_postcode="ls9 8as"),
        {"id": "office", "spaces": None, "booking_date": "2026-06-20", "summary": "call"},
        _bk(id="b2", booking_ref="BK-1002", booking_date="2026-06-21",
            collection_postcode=None, delivery_postcode=None, spaces=1),
    ], IDX)
    assert [r["ref"] for r in rows] == ["BK-1002", "BK-1001"]   # office row dropped, newest first
    b1 = next(r for r in rows if r["ref"] == "BK-1001")
    assert b1["miles"] is not None and b1["quote"]["total"] > 0
    b2 = next(r for r in rows if r["ref"] == "BK-1002")
    assert b2["miles"] is None             # no postcodes → no mileage leg
