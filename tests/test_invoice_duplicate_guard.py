"""Duplicate-invoice guard — completed work already on an invoice is never re-billed.

Before this guard, `list_completed_*` filtered on status='completed' plus the date
window and nothing else, so generating for July and then for June–August invoiced the
same jobs TWICE. Real double-billing, silent, on the main invoicing path.

The dedupe key is `invoice_lines.booking_id`, which all three draft builders already
populate. Dates and email threads are both fuzzy; the booking id is exact.

Run: python -m pytest tests/test_invoice_duplicate_guard.py -q
"""
import pytest

from app import db, invoicing

# Shaped for the office builder: it groups on `contact` and prices from
# `quoted_amount` (see invoicing.office_drafts / _office_amount_for).
BOOKING_A = {"id": "bk-a", "contact": "a@example.com", "status": "completed",
             "booking_ref": "BK-1001", "summary": "Service call",
             "booking_date": "2026-07-10", "quoted_amount": 120.0}
BOOKING_B = {"id": "bk-b", "contact": "b@example.com", "status": "completed",
             "booking_ref": "BK-1002", "summary": "Service call",
             "booking_date": "2026-07-12", "quoted_amount": 80.0}


@pytest.fixture(autouse=True)
def _no_vat(monkeypatch):
    monkeypatch.setattr(invoicing, "_preview_tax", lambda *a, **k: (0.0, False))


# ── the guard itself ──────────────────────────────────────────────────────────
def test_already_invoiced_booking_is_excluded_from_the_drafts(monkeypatch):
    monkeypatch.setattr(db, "job_parties_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_completed_office_bookings",
                        lambda *a, **k: [BOOKING_A, BOOKING_B])
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: {"bk-a"})
    drafts = invoicing.office_drafts_view("t1", "2026-07-01", "2026-07-31")
    emails = {d["customer_email"] for d in drafts}
    assert emails == {"b@example.com"}, "an already-invoiced booking was re-billed"


def test_regenerating_the_same_period_produces_nothing_the_second_time(monkeypatch):
    # The exact double-billing scenario: generate, then generate again.
    monkeypatch.setattr(db, "job_parties_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_completed_office_bookings",
                        lambda *a, **k: [BOOKING_A, BOOKING_B])
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: set())
    first = invoicing.office_drafts_view("t1", "2026-07-01", "2026-07-31")
    assert len(first) == 2                                  # both billed the first time

    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: {"bk-a", "bk-b"})
    second = invoicing.office_drafts_view("t1", "2026-07-01", "2026-07-31")
    assert second == [], "regenerating the same period double-billed every job"


def test_overlapping_period_only_bills_the_new_work(monkeypatch):
    # July billed; now generate June–August. Only the unbilled job may appear.
    monkeypatch.setattr(db, "job_parties_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_completed_office_bookings",
                        lambda *a, **k: [BOOKING_A, BOOKING_B])
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: {"bk-a"})
    drafts = invoicing.office_drafts_view("t1", "2026-06-01", "2026-08-31")
    assert [d["customer_email"] for d in drafts] == ["b@example.com"]


def test_guard_applies_to_every_vertical(monkeypatch):
    # One builder per vertical; the guard must not be wired into only one of them.
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda *a, **k: {"bk-a", "bk-b"})
    monkeypatch.setattr(db, "get_rate_card", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_fsm_rate_card", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_completed_bookings", lambda *a, **k: [BOOKING_A, BOOKING_B])
    monkeypatch.setattr(db, "list_completed_fsm_jobs", lambda *a, **k: [BOOKING_A, BOOKING_B])
    monkeypatch.setattr(db, "job_parties_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_completed_office_bookings",
                        lambda *a, **k: [BOOKING_A, BOOKING_B])
    assert invoicing.drafts_view("t1", "2026-07-01", "2026-07-31") == []
    assert invoicing.fsm_drafts_view("t1", "2026-07-01", "2026-07-31") == []
    assert invoicing.office_drafts_view("t1", "2026-07-01", "2026-07-31") == []


def test_no_bookings_short_circuits_without_a_lookup(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("looked up invoiced ids for an empty booking list")

    monkeypatch.setattr(db, "job_parties_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_completed_office_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "invoiced_booking_ids", _boom)
    assert invoicing.office_drafts_view("t1", "2026-07-01", "2026-07-31") == []


# ── editing a draft must not break the guard ──────────────────────────────────
def test_editing_lines_preserves_booking_id(monkeypatch):
    """The line editor deletes and re-inserts every line. It used to drop booking_id,
    which is the exact key the guard reads — so editing a draft silently freed its work
    to be invoiced a second time."""
    inserted = {}

    class _Tbl:
        def __init__(self, name):
            self.name = name

        def delete(self):
            return self

        def insert(self, rows):
            inserted[self.name] = rows
            return self

        def update(self, fields):
            return self

        def eq(self, *a):
            return self

        def select(self, *a):          # update_invoice_lines reads the invoice's stored price basis
            return self

        def limit(self, *a):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda s, n: _Tbl(n)})())
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: None)
    db.update_invoice_lines("i1", "t1", [
        {"description": "Kept job", "amount": 100.0, "booking_id": "bk-a"},
        {"description": "Hand-added line", "amount": 20.0, "booking_id": None}])
    assert [r["booking_id"] for r in inserted["invoice_lines"]] == ["bk-a", None]


def test_editor_route_pairs_each_line_with_its_own_booking(monkeypatch):
    """Mis-aligned columns would be WORSE than dropping booking_id — a line would claim
    another job, marking the wrong work as billed. Blank trailing rows (the two
    always-rendered 'Add a line' rows) must not shift the pairing."""
    import app.dashboard as dashboard
    from fastapi.testclient import TestClient
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf

    saved = {}
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda *a, **k: {"id": "i1", "status": "draft"})
    monkeypatch.setattr(dashboard.db, "update_invoice_lines",
                        lambda iid, tid, lines, **k: saved.update(lines=lines))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        # Repeated form fields, in row order: two real lines, one untouched blank row,
        # then a hand-added line with no booking of its own.
        TestClient(app).post("/app/invoices/i1/lines", data={
            "booking_id": ["bk-a", "bk-b", "", ""],
            "description": ["Job A", "Job B", "", "Hand-added"],
            "amount": ["100", "80", "", "20"],
        }, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert [(l["description"], l["booking_id"]) for l in saved["lines"]] == [
        ("Job A", "bk-a"), ("Job B", "bk-b"), ("Hand-added", None)]
