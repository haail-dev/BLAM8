"""One-time Xero import — arrive with two CSVs (docs: the research's onboarding
finding, 2026-09-01).

The research was blunt about the tolerances: a one-time migration at switch-on
is accepted — for ONE sitting, with zero mapping questions — and a recurring
manual export would be abandoned by week three. So this is deliberately a
one-shot importer, not a sync: Xero's own contact and invoice CSV exports go
in, the CRM and the Money page are truthful from the first minute, and the
overdue rows enter the next 08:45 chase run. A live Xero API integration waits
for a paying customer to ask, the same rule as the export side.

Pure parsing here — tolerant of Xero's header spellings, date formats and BOM;
the route does the writes. Never a mapping UI: headers we can't place are
ignored, rows we can't trust are counted and reported, nothing errors a whole
file over one bad line.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

# Xero header spellings seen across its contact/invoice exports (and the shape
# it accepts for its own imports — the same vocabulary our export writes).
# Keys are normalised: lowercase, alphanumerics only.
_HEADERS = {
    "contactname": "name", "to": "name", "contact": "name", "customer": "name",
    "name": "name",
    "emailaddress": "email", "email": "email",
    "phonenumber": "phone", "phone": "phone", "mobilenumber": "phone",
    "invoicenumber": "number", "number": "number", "invoiceno": "number",
    "reference": "reference", "ref": "reference",
    "invoicedate": "issue_date", "date": "issue_date",
    "duedate": "due_date",
    "total": "total", "invoiceamount": "total", "gross": "total",
    "amountdue": "amount_due", "due": "amount_due", "balancedue": "amount_due",
    "outstanding": "amount_due",
    "amountpaid": "amount_paid", "paid": "amount_paid",
    "status": "status",
    "description": "description", "details": "description",
    "quantity": "quantity",
    "unitamount": "unit_amount", "unitprice": "unit_amount",
}

# Xero invoice statuses → ours. None = don't import (their drafts are private
# working state, voids aren't documents; both are counted and reported).
_STATUS = {"paid": "paid", "authorised": "sent", "awaiting payment": "sent",
           "awaitingpayment": "sent", "sent": "sent", "submitted": "sent",
           "overdue": "sent",   # ours derives overdue from the due date
           "draft": None, "voided": None, "deleted": None}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _rows(text: str) -> list[dict]:
    """CSV → [{canonical_key: value}], unknown columns dropped. utf-8-sig via
    the caller (Xero exports carry a BOM)."""
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        return []
    keys = [_HEADERS.get(_norm(h)) for h in raw_header]
    out = []
    for raw in reader:
        if not any((c or "").strip() for c in raw):
            continue
        row: dict = {}
        for key, val in zip(keys, raw):
            if key and (val or "").strip() and key not in row:
                row[key] = val.strip()
        if row:
            out.append(row)
    return out


def parse_date(value: str | None) -> str | None:
    """Xero exports dates as dd/mm/yyyy, yyyy-mm-dd or '25 Jul 2026' depending
    on where the CSV came from. Unparseable → None (reported, not fatal)."""
    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_money(value) -> float | None:
    s = re.sub(r"[£$€,\s]", "", str(value or ""))
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_contacts(text: str) -> tuple[list[dict], int]:
    """→ ([{name, email, phone}], skipped_no_identity). A row needs an email
    or a phone to be a contact we can ever act on."""
    out, skipped = [], 0
    seen: set[str] = set()
    for row in _rows(text):
        email = (row.get("email") or "").strip().lower()
        phone = (row.get("phone") or "").strip()
        if not email and not phone:
            skipped += 1
            continue
        key = email or phone
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": (row.get("name") or "").strip() or None,
                    "email": email or None, "phone": phone or None})
    return out, skipped


def parse_invoices(text: str) -> tuple[list[dict], int]:
    """→ ([invoice], skipped). Xero's invoice CSV is one row per LINE with the
    header fields repeated, so rows group on the invoice number. Amounts are
    imported as-is (subtotal = total, VAT 0) — their VAT split stayed in their
    books; what matters here is that the totals owed are exactly right."""
    grouped: dict[str, dict] = {}
    skipped = 0
    for row in _rows(text):
        number = (row.get("number") or "").strip()
        if not number:
            skipped += 1
            continue
        status_raw = _norm(row.get("status") or "sent")
        status = _STATUS.get(status_raw, "sent")
        inv = grouped.get(number)
        if inv is None:
            inv = grouped[number] = {
                "invoice_ref": number[:40],
                "customer_name": (row.get("name") or "").strip() or None,
                "customer_email": (row.get("email") or "").strip().lower() or None,
                "customer_reference": (row.get("reference") or "").strip() or None,
                "issue_date": parse_date(row.get("issue_date")),
                "due_date": parse_date(row.get("due_date")),
                "total": parse_money(row.get("total")),
                "amount_due": parse_money(row.get("amount_due")),
                "amount_paid": parse_money(row.get("amount_paid")),
                "status": status,
                "lines": [],
            }
        if row.get("description"):
            amount = parse_money(row.get("unit_amount"))
            qty = parse_money(row.get("quantity")) or 1
            inv["lines"].append({
                "description": row["description"][:200],
                "quantity": qty,
                "amount": round((amount or 0) * qty, 2) if amount else None})
    out = []
    for inv in grouped.values():
        if inv["status"] is None:            # draft/voided — not documents
            skipped += 1
            continue
        # Totals: the export may carry total, amount_due, amount_paid, or any
        # two of the three — reconstruct the third; a paid invoice owes 0.
        total, due, paid = inv["total"], inv["amount_due"], inv["amount_paid"]
        if total is None and due is not None:
            total = round(due + (paid or 0), 2)
        if total is None and inv["lines"]:
            total = round(sum(l["amount"] or 0 for l in inv["lines"]), 2)
        if total is None:
            skipped += 1
            continue
        if paid is None:
            paid = round(total - due, 2) if due is not None else (
                total if inv["status"] == "paid" else 0.0)
        paid = max(0.0, min(paid, total))
        if inv["status"] != "paid" and total > 0 and paid >= total:
            inv["status"] = "paid"
        elif inv["status"] == "sent" and 0 < paid < total:
            inv["status"] = "partially_paid"
        inv["total"] = total
        inv["amount_paid"] = paid
        inv.pop("amount_due", None)
        if not inv["lines"]:
            inv["lines"] = [{"description": "Imported from Xero",
                             "quantity": 1, "amount": total}]
        else:
            for l in inv["lines"]:
                if l["amount"] is None:
                    l["amount"] = 0.0
        out.append(inv)
    return out, skipped
