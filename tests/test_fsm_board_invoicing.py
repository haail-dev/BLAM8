"""Field Service Phase 2b — the engineer board view-model + trades invoicing
(docs/modules/module-field-service.md D4/D6). Pure assembly, no DB.

Run: python -m pytest tests/test_fsm_board_invoicing.py -q
"""
from datetime import datetime

from app import invoicing, scheduling
from app.scheduling import DIARY_TZ


# ── the engineer board ────────────────────────────────────────────────────────
def test_fsm_board_lays_out_engineer_loads_and_flags_emergencies(monkeypatch):
    eng = {"id": "e1", "name": "Sam", "skills": ["gas"], "daily_minutes": 480, "active": True}
    jobs = [
        {"id": "b1", "engineer_id": "e1", "booking_date": "2026-06-29", "duration_minutes": 120,
         "status": "confirmed", "job_type": "Repair", "priority": "emergency",
         "summary": "Boiler", "booking_ref": "BK-1", "contact": "c@x.co", "quoted_amount": 115,
         "created_at": "2026-06-28T10:00:00Z"},
        {"id": "b2", "engineer_id": "e1", "booking_date": "2026-06-29", "duration_minutes": 60,
         "status": "proposed", "job_type": "Service", "priority": "standard",
         "summary": "Annual service", "booking_ref": "BK-2", "contact": "d@x.co",
         "created_at": "2026-06-28T11:00:00Z"},
    ]
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "list_engineers", lambda *a, **k: [eng])
    monkeypatch.setattr(scheduling.db, "list_branches", lambda *a, **k: [{"name": "Main"}])
    monkeypatch.setattr(scheduling.db, "list_job_types", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "list_bookings", lambda *a, **k: jobs)

    now = datetime(2026, 6, 29, 8, 0, tzinfo=DIARY_TZ)   # a Monday
    data = scheduling.fsm_board("t1", now=now, days=3)

    assert data["has_engineers"] is True and len(data["days"]) == 3
    today = data["days"][0]
    assert today["is_today"] and today["total_jobs"] == 2 and today["emergencies"] == 1
    ev = today["engineers"][0]
    assert ev["used_min"] == 180 and ev["daily_minutes"] == 480
    assert ev["pct"] == 38                                # round(100*180/480)
    assert [j["job_type"] for j in ev["jobs"]] == ["Repair", "Service"]   # oldest first
    # later days are empty for this engineer
    assert data["days"][1]["total_jobs"] == 0


# ── trades invoicing ──────────────────────────────────────────────────────────
RATE = {"callout_charge": 60, "hourly_rate": 55, "min_charge": 75, "vat_pct": 20}


def test_fsm_drafts_group_by_customer_use_stored_quote_add_vat():
    jobs = [
        {"id": "b1", "engineer_id": "e1", "contact": "Kris@x.co", "booking_date": "2026-06-20",
         "job_type": "Boiler service", "summary": "annual", "booking_ref": "BK-1",
         "duration_minutes": 60, "quoted_amount": 115},        # stored PRE-VAT net
        {"id": "b2", "engineer_id": "e1", "contact": "kris@x.co", "booking_date": "2026-06-22",
         "job_type": "Repair", "summary": "tap", "booking_ref": "BK-2",
         "duration_minutes": 60, "quoted_amount": 115},
    ]
    drafts = invoicing.fsm_drafts(jobs, RATE)
    assert len(drafts) == 1                                # grouped by customer (case-insensitive)
    d = drafts[0]
    assert d["count"] == 2 and d["subtotal"] == 230.0
    assert d["vat"] == 46.0 and d["total"] == 276.0        # +20% VAT


def test_fsm_drafts_recompute_when_unquoted():
    jobs = [{"id": "b1", "engineer_id": "e1", "contact": "a@x.co", "booking_date": "2026-06-20",
             "job_type": "Service", "summary": "", "booking_ref": "BK-9",
             "duration_minutes": 60, "quoted_amount": None}]
    d = invoicing.fsm_drafts(jobs, RATE)[0]
    assert d["subtotal"] == 115.0                          # callout 60 + labour 55 (pre-VAT)
    assert d["total"] == 138.0


def test_fsm_drafts_skips_non_fsm_rows():
    jobs = [{"id": "t1", "spaces": 6, "contact": "x@x.co", "booking_date": "2026-06-20"}]  # transport
    assert invoicing.fsm_drafts(jobs, RATE) == []
