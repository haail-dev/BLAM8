"""The one-time Xero import (src/app/xero_import.py + /app/import).

Research doctrine under test: one sitting, zero mapping questions, nothing
silently dropped — and the import feeds the machines (CRM, Money, the chase
list) rather than a dead archive.

Run: python -m pytest tests/test_xero_import.py -q
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import xero_import
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

TENANT = "11111111-1111-1111-1111-111111111111"


# ── contacts ─────────────────────────────────────────────────────────────────
def test_contacts_survive_xero_header_spellings_and_dedupe():
    csv_text = ("ContactName,EmailAddress,PhoneNumber\n"
                "Acme Haulage,ops@acme.example,01onephone\n"
                "Acme Haulage,OPS@ACME.EXAMPLE,\n"          # dupe, case-insensitive
                "Phone Only Ltd,,07700900123\n"
                "No Identity,,\n")
    rows, skipped = xero_import.parse_contacts(csv_text)
    assert [r["email"] for r in rows] == ["ops@acme.example", None]
    assert rows[1]["phone"] == "07700900123"
    assert skipped == 1                                     # nothing to act on


# ── invoices ─────────────────────────────────────────────────────────────────
def test_invoice_rows_group_by_number_and_keep_the_foreign_ref():
    csv_text = ("InvoiceNumber,ContactName,EmailAddress,Reference,InvoiceDate,DueDate,Total,Status,Description,Quantity,UnitAmount\n"
                'INV-0042,Harbour + Co,ops@harbour.example,PO 4471,25/07/2026,08/08/2026,"1,240.00",AUTHORISED,Weekly run,2,300\n'
                'INV-0042,Harbour + Co,ops@harbour.example,PO 4471,25/07/2026,08/08/2026,"1,240.00",AUTHORISED,Extra drop,1,640\n')
    (inv,), skipped = xero_import.parse_invoices(csv_text)
    assert skipped == 0
    assert inv["invoice_ref"] == "INV-0042"                 # theirs, preserved
    assert inv["customer_reference"] == "PO 4471"
    assert inv["issue_date"] == "2026-07-25" and inv["due_date"] == "2026-08-08"
    assert inv["total"] == 1240.0 and inv["status"] == "sent"
    assert [l["amount"] for l in inv["lines"]] == [600.0, 640.0]


def test_drafts_and_voids_stay_behind_and_paid_maps():
    csv_text = ("InvoiceNumber,Total,Status\n"
                "INV-1,100,DRAFT\n"
                "INV-2,100,VOIDED\n"
                "INV-3,100,PAID\n")
    rows, skipped = xero_import.parse_invoices(csv_text)
    assert skipped == 2
    assert rows[0]["invoice_ref"] == "INV-3" and rows[0]["status"] == "paid"
    assert rows[0]["amount_paid"] == 100.0
    assert rows[0]["lines"][0]["description"] == "Imported from Xero"


def test_totals_reconstruct_from_whichever_columns_exist():
    # AmountDue only → total; partial payment → partially_paid.
    csv_text = ("InvoiceNumber,AmountDue,AmountPaid,Status\n"
                "INV-9,60.00,40.00,Awaiting Payment\n")
    (inv,), _ = xero_import.parse_invoices(csv_text)
    assert inv["total"] == 100.0 and inv["amount_paid"] == 40.0
    assert inv["status"] == "partially_paid"


def test_date_formats_from_every_xero_mood():
    assert xero_import.parse_date("2026-07-25") == "2026-07-25"
    assert xero_import.parse_date("25/07/2026") == "2026-07-25"
    assert xero_import.parse_date("25 Jul 2026") == "2026-07-25"
    assert xero_import.parse_date("25 July 2026") == "2026-07-25"
    assert xero_import.parse_date("not a date") is None


# ── the route ────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_import_run_writes_and_reports(client, monkeypatch):
    created_contacts, imported = [], []
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "crm_get_or_create_contact",
                        lambda t, e, name=None, **k: created_contacts.append(e) or {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "get_invoice_by_ref",
                        lambda t, ref, client=None: ({"id": "x"} if ref == "INV-DUPE" else None))
    monkeypatch.setattr(dashboard.db, "import_invoice",
                        lambda t, inv, contact_id=None, client=None: imported.append(inv) or {"id": "i1"})
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)
    contacts_csv = "ContactName,EmailAddress\nRob,rob@acme.example\n"
    invoices_csv = ("InvoiceNumber,EmailAddress,DueDate,Total,Status\n"
                    "INV-NEW,rob@acme.example,25/07/2026,75,AUTHORISED\n"
                    "INV-DUPE,rob@acme.example,25/07/2026,50,AUTHORISED\n")
    r = client.post("/app/import", files={
        "contacts_csv": ("contacts.csv", contacts_csv, "text/csv"),
        "invoices_csv": ("invoices.csv", invoices_csv, "text/csv")})
    assert r.status_code == 200
    assert created_contacts == ["rob@acme.example"]
    assert [i["invoice_ref"] for i in imported] == ["INV-NEW"]   # dupe skipped
    assert "1 imported" in r.text and "1 already here" in r.text
    # The wow line: overdue imports are promised to the chase engine.
    assert "start drafting chases" in r.text


def test_import_page_reachable_and_settings_carries_the_door(client, monkeypatch):
    r = client.get("/app/import")
    assert r.status_code == 200
    assert "Bring your data" in r.text and "leave with everything" in r.text
