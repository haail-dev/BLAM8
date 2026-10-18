"""Compliance — the RAG date math (TMS baseline).

No DB: driver/vehicle/hours dicts + today in, flagged rows out.

Run: python -m pytest tests/ -q
"""
from datetime import date

from app.compliance import _date_status, overview

TODAY = date(2026, 6, 20)   # Saturday; ISO week starts Mon 2026-06-15


def test_date_status_rag():
    assert _date_status("2026-06-10", TODAY) == "expired"
    assert _date_status("2026-07-05", TODAY) == "soon"        # within 30 days
    assert _date_status("2027-01-01", TODAY) == "ok"
    assert _date_status(None, TODAY) == "unknown"


def test_overview_flags_hours_and_documents():
    drivers = [
        {"id": "d1", "name": "Anna", "licence_expiry": "2027-01-01", "cpc_expiry": "2026-07-01"},  # cpc soon
        {"id": "d2", "name": "Bob", "licence_expiry": "2026-06-01", "cpc_expiry": "2027-01-01"},   # licence expired
    ]
    vehicles = [
        {"id": "v1", "name": "Van", "mot_expiry": "2027-01-01", "tax_expiry": "2027-01-01",
         "service_due": "2027-01-01", "insurance_expiry": "2027-01-01"},
        {"id": "v2", "name": "Truck", "mot_expiry": "2026-06-10"},   # MOT expired, rest unknown
    ]
    hours = [
        {"driver_id": "d1", "work_date": "2026-06-15", "hours": 40},
        {"driver_id": "d1", "work_date": "2026-06-18", "hours": 20},   # 60 > 56 limit → expired
        {"driver_id": "d2", "work_date": "2026-06-01", "hours": 50},   # last week → not counted
    ]
    out = overview(drivers, vehicles, hours, TODAY)

    anna = next(d for d in out["drivers"] if d["name"] == "Anna")
    assert anna["hours"] == 60.0 and anna["hours_status"] == "expired"
    assert anna["cpc_status"] == "soon" and anna["overall"] == "expired"   # hours is worst

    bob = next(d for d in out["drivers"] if d["name"] == "Bob")
    assert bob["licence_status"] == "expired" and bob["hours"] == 0.0      # last week's hours excluded

    v1 = next(v for v in out["vehicles"] if v["name"] == "Van")
    assert v1["overall"] == "ok"
    v2 = next(v for v in out["vehicles"] if v["name"] == "Truck")
    assert v2["docs"]["mot_expiry"]["status"] == "expired" and v2["overall"] == "expired"

    assert out["alerts"] == 3   # anna + bob + v2
