"""Invoice export — CSV in the shapes Xero and QuickBooks import, plus a plain
accountant-readable sheet.

This is the "integrate, don't compete" half of the accounting story: BLAM8 owns
the *source documents* (it raised the invoice, it knows when it was paid), and a
bookkeeper's ledger owns the accounts. A CSV serves Xero, QuickBooks, FreeAgent
and Sage at once with no OAuth app to certify and no integration to maintain, so
it ships now and a real API sync waits until a paying customer asks for one.

Pure formatting — takes plain dicts, returns a string. No db, no request. The
route in dashboard.py does the fetching.

Three shapes:

  accountant   The lossless one, and the one to reach for by default: ISO dates,
               every field we hold including status/paid_at/payment_method, and
               line totals that add up to the invoice totals exactly. Any
               accounting package or spreadsheet can read it.
  xero         Xero's sales-invoice import columns, one row per line item with
               the header fields repeated (that's the format Xero expects).
  quickbooks   The QBO invoice-import equivalent.

⚠️ Two fields in the accounting formats are ORG-SPECIFIC and we can only supply a
sensible default: the account/nominal code (Xero's UK default chart uses 200 for
Sales) and the tax rate name. Both are overridable on the route, and the UI tells
the user to check them against their own chart of accounts before importing. We
can't know their chart, so guessing silently would be worse than saying so.

VAT lives on the invoice, not the line (see db.create_invoice), so the exported
lines are always ex-VAT and the target package derives the tax from the tax
type/taxable flag. A tenant with VAT off exports as not-taxable, which is right:
most BLAM8 tenants are not VAT registered.
"""
from __future__ import annotations

import csv
import io

# Xero's UK default chart of accounts uses 200 for Sales. A default beats a blank
# (Xero rejects rows with no account code) but it is still a guess about their org.
DEFAULT_ACCOUNT_CODE = "200"

# Xero UK tax type names: OUTPUT2 = "20% VAT on Income", NONE = no VAT.
XERO_TAX_VAT = "OUTPUT2"
XERO_TAX_NONE = "NONE"

# QBO wants a product/service name per line; tenants invoice for work, not stock.
QUICKBOOKS_DEFAULT_ITEM = "Services"

FORMATS = ("accountant", "xero", "quickbooks")

# Kept SHORT on purpose: the select sizes to its longest option, and a long label
# pushed the Export button into a two-line wrap on a narrow viewport. The detail
# these labels used to carry lives in the caveat paragraph under the table.
FORMAT_LABELS = {
    "accountant": "Accountant (recommended)",
    "xero": "Xero",
    "quickbooks": "QuickBooks",
}


def _iso_to_uk(d: str | None) -> str:
    """yyyy-mm-dd → dd/mm/yyyy. Xero and QuickBooks both read the import against
    the ORG's locale, and these are UK businesses, so UK order is the safe one.
    Anything unparseable passes through untouched rather than being dropped."""
    s = (d or "")[:10]
    parts = s.split("-")
    if len(parts) != 3 or not all(parts):
        return s
    y, m, day = parts
    return f"{day}/{m}/{y}"


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _line_rows(inv: dict, lines: list[dict]) -> list[dict]:
    """Normalise an invoice's lines for export.

    Two things worth knowing:

    1. An invoice with NO line rows still yields one row, described from its notes
       or its ref. Silently dropping an invoice from an accounting export would be
       a genuine data-integrity bug — a missing invoice is far worse than a thin
       description.
    2. `invoice_lines.amount` is the line TOTAL and is authoritative; unit_price is
       not guaranteed to satisfy quantity × unit_price == amount (db.create_invoice
       defaults unit_price to the full amount even when a quantity was passed). So
       the unit amount is DERIVED from the total, at 4dp, which both Xero and QBO
       accept — that keeps quantity meaningful while the row still totals to the
       figure BLAM8 holds and shows the customer.
    """
    if not lines:
        return [{"description": inv.get("notes") or f"Invoice {inv.get('invoice_ref') or ''}".strip(),
                 "quantity": 1.0,
                 "unit": round(_f(inv.get("subtotal")), 4),
                 "total": round(_f(inv.get("subtotal")), 2)}]
    out = []
    for ln in lines:
        total = round(_f(ln.get("amount")), 2)
        qty = _f(ln.get("quantity"), 1.0) or 1.0
        out.append({"description": ln.get("description") or "",
                    "quantity": qty,
                    "unit": round(total / qty, 4),
                    "total": total})
    return out


def filter_invoices(invoices: list[dict], date_from: str = "", date_to: str = "",
                    status: str = "") -> list[dict]:
    """Filter by issue_date (inclusive, ISO strings compare lexicographically) and
    exact status. Blank filters mean 'everything', and an invoice with no issue_date
    is KEPT — an export that quietly omits rows is worse than one with a gap."""
    out = []
    for inv in invoices:
        issued = (inv.get("issue_date") or "")[:10]
        if date_from and issued and issued < date_from[:10]:
            continue
        if date_to and issued and issued > date_to[:10]:
            continue
        if status and (inv.get("status") or "") != status:
            continue
        out.append(inv)
    return out


def filename(fmt: str) -> str:
    return f"blam8-invoices-{fmt if fmt in FORMATS else 'accountant'}.csv"


def build_csv(fmt: str, invoices: list[dict], lines_by_invoice: dict[str, list[dict]],
              settings: dict, *, account_code: str = DEFAULT_ACCOUNT_CODE,
              tax_name: str = "") -> str:
    """Render the CSV. `settings` is the tenant's invoice settings (already merged
    through invoice_config.settings_for) — only show_vat and vat_rate are read."""
    vat_on = bool(settings.get("show_vat"))
    vat_rate = _f(settings.get("vat_rate"), 20.0) if vat_on else 0.0
    buf = io.StringIO()
    # Excel is the usual destination on the way to an accounting package, and it
    # wants CRLF — matching src/app/spreadsheets.py.
    w = csv.writer(buf, lineterminator="\r\n")

    if fmt == "xero":
        tax = tax_name or (XERO_TAX_VAT if vat_on else XERO_TAX_NONE)
        w.writerow(["ContactName", "EmailAddress", "InvoiceNumber", "Reference",
                    "InvoiceDate", "DueDate", "Description", "Quantity",
                    "UnitAmount", "AccountCode", "TaxType", "Currency"])
        for inv in invoices:
            for r in _line_rows(inv, lines_by_invoice.get(inv["id"], [])):
                w.writerow([
                    inv.get("customer_name") or inv.get("customer_email") or "",
                    inv.get("customer_email") or "",
                    inv.get("invoice_ref") or "",
                    # Xero's Reference is exactly where a customer PO belongs;
                    # without one, the invoice ref keeps the column useful.
                    inv.get("customer_reference") or inv.get("invoice_ref") or "",
                    _iso_to_uk(inv.get("issue_date")), _iso_to_uk(inv.get("due_date")),
                    r["description"], f"{r['quantity']:g}", f"{r['unit']:.4f}",
                    account_code, tax, "GBP"])
        return buf.getvalue()

    if fmt == "quickbooks":
        w.writerow(["InvoiceNo", "Customer", "InvoiceDate", "DueDate",
                    "Item(Product/Service)", "ItemDescription", "ItemQuantity",
                    "ItemRate", "ItemAmount", "Taxable", "Currency"])
        for inv in invoices:
            for r in _line_rows(inv, lines_by_invoice.get(inv["id"], [])):
                w.writerow([
                    inv.get("invoice_ref") or "",
                    inv.get("customer_name") or inv.get("customer_email") or "",
                    _iso_to_uk(inv.get("issue_date")), _iso_to_uk(inv.get("due_date")),
                    tax_name or QUICKBOOKS_DEFAULT_ITEM, r["description"],
                    f"{r['quantity']:g}", f"{r['unit']:.4f}", f"{r['total']:.2f}",
                    "Y" if vat_on else "N", "GBP"])
        return buf.getvalue()

    # accountant — the lossless default
    w.writerow(["invoice_ref", "status", "issue_date", "due_date", "paid_at",
                "payment_method", "customer_name", "customer_email",
                "customer_reference",
                "line_description", "quantity", "unit_price_ex_vat",
                "line_total_ex_vat", "invoice_subtotal_ex_vat", "invoice_vat",
                "invoice_total", "vat_rate_pct", "notes"])
    for inv in invoices:
        for r in _line_rows(inv, lines_by_invoice.get(inv["id"], [])):
            w.writerow([
                inv.get("invoice_ref") or "", inv.get("status") or "",
                (inv.get("issue_date") or "")[:10], (inv.get("due_date") or "")[:10],
                (inv.get("paid_at") or "")[:10], inv.get("payment_method") or "",
                inv.get("customer_name") or "", inv.get("customer_email") or "",
                inv.get("customer_reference") or "",
                r["description"], f"{r['quantity']:g}", f"{r['unit']:.4f}",
                f"{r['total']:.2f}",
                f"{_f(inv.get('subtotal')):.2f}", f"{_f(inv.get('vat')):.2f}",
                f"{_f(inv.get('total')):.2f}", f"{vat_rate:g}",
                (inv.get("notes") or "").replace("\n", " ")])
    return buf.getvalue()
