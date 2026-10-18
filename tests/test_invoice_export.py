"""Invoice CSV export — the accountant / Xero / QuickBooks shapes and the route.

The "integrate, don't compete" slice: BLAM8 hands its source documents to whatever
ledger the customer's bookkeeper already uses, instead of trying to be the ledger.

Run: python -m pytest tests/test_invoice_export.py -q
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import invoice_config, invoice_export
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

VAT_OFF = invoice_config.settings_for(None)                          # most tenants
VAT_ON = invoice_config.settings_for({"show_vat": True, "vat_rate": 20})

INV = {"id": "i1", "invoice_ref": "INV-1001", "status": "paid",
       "customer_name": "Mrs Patel", "customer_email": "patel@example.co.uk",
       "issue_date": "2026-07-02", "due_date": "2026-07-16",
       "paid_at": "2026-07-09T11:20:00+00:00", "payment_method": "stripe",
       "subtotal": 240.0, "vat": 0.0, "total": 240.0, "notes": "Two visits"}


def _rows(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body)))


# ── the lossless accountant sheet ─────────────────────────────────────────────
def test_accountant_format_carries_status_and_payment_fields():
    body = invoice_export.build_csv("accountant", [INV], {"i1": [
        {"description": "Visit 1", "amount": 120.0, "quantity": 1},
        {"description": "Visit 2", "amount": 120.0, "quantity": 1}]}, VAT_OFF)
    rows = _rows(body)
    assert rows[0][:6] == ["invoice_ref", "status", "issue_date", "due_date",
                           "paid_at", "payment_method"]
    assert len(rows) == 3                                  # header + one row per line
    assert rows[1][0] == "INV-1001" and rows[1][1] == "paid"
    assert rows[1][4] == "2026-07-09"                      # paid_at trimmed to a date
    assert rows[1][5] == "stripe"
    assert rows[0][8] == "customer_reference"              # their PO rides the export
    assert rows[1][9] == "Visit 1" and rows[2][9] == "Visit 2"


def test_accountant_line_totals_sum_to_the_invoice_subtotal():
    # The whole point of an accounting export: the lines must reconcile.
    body = invoice_export.build_csv("accountant", [INV], {"i1": [
        {"description": "Visit 1", "amount": 120.0, "quantity": 1},
        {"description": "Visit 2", "amount": 120.0, "quantity": 1}]}, VAT_OFF)
    rows = _rows(body)
    assert sum(float(r[12]) for r in rows[1:]) == pytest.approx(float(INV["subtotal"]))


def test_accountant_reports_zero_vat_rate_when_tenant_has_vat_off():
    off = _rows(invoice_export.build_csv("accountant", [INV], {}, VAT_OFF))
    on = _rows(invoice_export.build_csv("accountant", [INV], {}, VAT_ON))
    assert off[1][16] == "0" and on[1][16] == "20"


def test_notes_newlines_flattened_so_they_cannot_break_a_row():
    inv = {**INV, "notes": "line one\nline two"}
    rows = _rows(invoice_export.build_csv("accountant", [inv], {}, VAT_OFF))
    assert len(rows) == 2 and "line one line two" in rows[1][17]


# ── Xero ──────────────────────────────────────────────────────────────────────
def test_xero_headers_dates_and_tax_type():
    body = invoice_export.build_csv("xero", [INV], {"i1": [
        {"description": "Visit 1", "amount": 240.0, "quantity": 1}]}, VAT_ON)
    rows = _rows(body)
    assert rows[0][0] == "ContactName" and rows[0][10] == "TaxType"
    assert rows[1][4] == "02/07/2026" and rows[1][5] == "16/07/2026"   # UK order
    assert rows[1][9] == invoice_export.DEFAULT_ACCOUNT_CODE
    assert rows[1][10] == invoice_export.XERO_TAX_VAT
    assert rows[1][11] == "GBP"


def test_xero_tax_type_is_none_for_a_non_vat_registered_tenant():
    rows = _rows(invoice_export.build_csv("xero", [INV], {}, VAT_OFF))
    assert rows[1][10] == invoice_export.XERO_TAX_NONE


def test_xero_account_code_and_tax_name_are_overridable():
    rows = _rows(invoice_export.build_csv("xero", [INV], {}, VAT_ON,
                                          account_code="4000", tax_name="ZERORATEDOUTPUT"))
    assert rows[1][9] == "4000" and rows[1][10] == "ZERORATEDOUTPUT"


def test_xero_falls_back_to_email_when_the_customer_has_no_name():
    inv = {**INV, "customer_name": None}
    rows = _rows(invoice_export.build_csv("xero", [inv], {}, VAT_OFF))
    assert rows[1][0] == "patel@example.co.uk"


# ── QuickBooks ────────────────────────────────────────────────────────────────
def test_quickbooks_taxable_flag_follows_the_vat_toggle():
    on = _rows(invoice_export.build_csv("quickbooks", [INV], {}, VAT_ON))
    off = _rows(invoice_export.build_csv("quickbooks", [INV], {}, VAT_OFF))
    assert on[0][9] == "Taxable" and on[1][9] == "Y" and off[1][9] == "N"


def test_quickbooks_defaults_the_product_service_name():
    rows = _rows(invoice_export.build_csv("quickbooks", [INV], {}, VAT_OFF))
    assert rows[1][4] == invoice_export.QUICKBOOKS_DEFAULT_ITEM


# ── the quantity × unit == line total invariant ───────────────────────────────
def test_unit_amount_is_derived_so_quantity_times_unit_equals_the_line_total():
    # db.create_invoice defaults unit_price to the FULL amount even when a quantity
    # was passed, so trusting the stored unit_price would export 2 × £100 = £200 for
    # a £100 line. The export derives the unit from the authoritative total instead.
    body = invoice_export.build_csv("xero", [INV], {"i1": [
        {"description": "Two visits", "amount": 100.0, "quantity": 2,
         "unit_price": 100.0}]}, VAT_OFF)
    rows = _rows(body)
    qty, unit = float(rows[1][7]), float(rows[1][8])
    assert qty == 2 and unit == 50.0 and qty * unit == pytest.approx(100.0)


def test_zero_quantity_does_not_divide_by_zero():
    rows = _rows(invoice_export.build_csv("xero", [INV], {"i1": [
        {"description": "Odd row", "amount": 60.0, "quantity": 0}]}, VAT_OFF))
    assert float(rows[1][8]) == 60.0


# ── an invoice with no lines must never vanish ────────────────────────────────
def test_invoice_with_no_lines_still_exports_one_row():
    # Silently dropping an invoice from an accounting export is a data-integrity bug,
    # so a lineless invoice is described from its notes and carries its subtotal.
    rows = _rows(invoice_export.build_csv("accountant", [INV], {}, VAT_OFF))
    assert len(rows) == 2
    assert rows[1][9] == "Two visits" and float(rows[1][12]) == 240.0


def test_lineless_invoice_without_notes_falls_back_to_its_ref():
    inv = {**INV, "notes": None}
    rows = _rows(invoice_export.build_csv("accountant", [inv], {}, VAT_OFF))
    assert rows[1][9] == "Invoice INV-1001"


# ── the customer's reference (PO) ─────────────────────────────────────────────
def test_customer_reference_rides_the_accountant_sheet():
    inv = {**INV, "customer_reference": "PO 4471"}
    rows = _rows(invoice_export.build_csv("accountant", [inv], {}, VAT_OFF))
    assert rows[1][8] == "PO 4471"
    # And blank stays blank — never invented.
    assert _rows(invoice_export.build_csv("accountant", [INV], {}, VAT_OFF))[1][8] == ""


def test_xero_reference_column_prefers_the_customer_po():
    # Xero's Reference is exactly where a PO belongs; without one the invoice
    # ref keeps the column useful (yesterday's behaviour).
    inv = {**INV, "customer_reference": "PO 4471"}
    with_po = _rows(invoice_export.build_csv("xero", [inv], {}, VAT_OFF))
    without = _rows(invoice_export.build_csv("xero", [INV], {}, VAT_OFF))
    assert with_po[0][3] == "Reference"
    assert with_po[1][3] == "PO 4471" and with_po[1][2] == "INV-1001"
    assert without[1][3] == "INV-1001"


# ── filtering ─────────────────────────────────────────────────────────────────
def test_filter_by_date_window_is_inclusive_on_both_ends():
    a = {"id": "a", "issue_date": "2026-07-01", "status": "paid"}
    b = {"id": "b", "issue_date": "2026-07-15", "status": "paid"}
    c = {"id": "c", "issue_date": "2026-08-01", "status": "paid"}
    got = invoice_export.filter_invoices([a, b, c], "2026-07-01", "2026-07-15")
    assert [i["id"] for i in got] == ["a", "b"]


def test_filter_by_status_and_blank_filters_pass_everything():
    rows = [{"id": "a", "issue_date": "2026-07-01", "status": "paid"},
            {"id": "b", "issue_date": "2026-07-02", "status": "draft"}]
    assert [i["id"] for i in invoice_export.filter_invoices(rows, status="draft")] == ["b"]
    assert len(invoice_export.filter_invoices(rows)) == 2


def test_invoice_without_an_issue_date_is_kept_not_silently_dropped():
    rows = [{"id": "a", "issue_date": None, "status": "draft"}]
    assert len(invoice_export.filter_invoices(rows, "2026-07-01", "2026-07-31")) == 1


# ── format plumbing ───────────────────────────────────────────────────────────
def test_unknown_format_falls_back_to_the_accountant_sheet():
    assert invoice_export.filename("nonsense") == "blam8-invoices-accountant.csv"
    assert invoice_export.filename("xero") == "blam8-invoices-xero.csv"


def test_rows_are_crlf_terminated_for_excel():
    body = invoice_export.build_csv("accountant", [INV], {}, VAT_OFF)
    assert body.endswith("\r\n") and "\r\n" in body


def test_empty_invoice_list_still_yields_a_header_row():
    for fmt in invoice_export.FORMATS:
        rows = _rows(invoice_export.build_csv(fmt, [], {}, VAT_OFF))
        assert len(rows) == 1 and rows[0]


# ── the route ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [INV])
    monkeypatch.setattr(dashboard.db, "invoice_lines_by_invoice", lambda *a, **k: {
        "i1": [{"description": "Visit 1", "amount": 240.0, "quantity": 1}]})
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_export_route_serves_a_csv_attachment(client):
    r = client.get("/app/invoices/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "blam8-invoices-accountant.csv" in r.headers["content-disposition"]
    assert "INV-1001" in r.text


def test_export_path_is_not_swallowed_by_the_invoice_id_route(client):
    # /invoices/{invoice_id} is declared later; FastAPI matches in declaration order,
    # so a regression that reorders them turns this into a 404 for a real path.
    r = client.get("/app/invoices/export.csv")
    assert r.status_code == 200 and "invoice_ref" in r.text


def test_export_route_honours_the_format_parameter(client):
    r = client.get("/app/invoices/export.csv?format=xero")
    assert "blam8-invoices-xero.csv" in r.headers["content-disposition"]
    assert _rows(r.text)[0][0] == "ContactName"


def test_export_route_rejects_an_unknown_format_without_erroring(client):
    r = client.get("/app/invoices/export.csv?format=../etc/passwd")
    assert r.status_code == 200
    assert "blam8-invoices-accountant.csv" in r.headers["content-disposition"]


def test_export_route_passes_the_account_code_override_through(client):
    r = client.get("/app/invoices/export.csv?format=xero&account=4000")
    assert _rows(r.text)[1][9] == "4000"


def test_export_route_applies_the_status_filter(client):
    assert "INV-1001" in client.get("/app/invoices/export.csv?status=paid").text
    assert "INV-1001" not in client.get("/app/invoices/export.csv?status=draft").text


def test_invoices_page_offers_the_export_control(client, monkeypatch):
    monkeypatch.setattr(dashboard.invoicing, "draft_view_for", lambda *a, **k: [])
    r = client.get("/app/invoices")
    assert r.status_code == 200
    assert "/app/invoices/export.csv" in r.text and "Export CSV" in r.text
    assert "chart of accounts" in r.text          # the honest org-specific caveat


def test_export_control_hidden_when_there_are_no_invoices(client, monkeypatch):
    monkeypatch.setattr(dashboard.invoicing, "draft_view_for", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/invoices")
    assert r.status_code == 200 and "Export CSV" not in r.text
    # …but the New-invoice button must survive. It shares a row with the export
    # button for layout, so a careless restructure can hide the only way a fresh
    # tenant creates their first invoice.
    assert "+ New invoice" in r.text and "/app/invoices/new" in r.text


def test_export_button_stays_wired_to_the_form_from_outside_it(client, monkeypatch):
    # The submit button sits OUTSIDE the form on purpose (form="invoice-export"), so
    # that on mobile the select gets a full-width row and both buttons share the row
    # under it. Verified in a real browser at 375px. Moving the button back inside the
    # form silently reintroduces the clipped-button layout bug, so pin both halves.
    monkeypatch.setattr(dashboard.invoicing, "draft_view_for", lambda *a, **k: [])
    r = client.get("/app/invoices")
    assert 'id="invoice-export"' in r.text
    assert 'form="invoice-export"' in r.text
