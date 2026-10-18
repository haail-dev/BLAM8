"""Masthead search (Kris 2026-09-21): the magnifying glass beside the cog.

"Used to search Booking / Quote / Invoice IDs, and to search for contacts via
name or email. A smart search that suggests and filters as you type." One box
on every page of the app; it replaced the CRM page's own search box, which
only ever found contacts and only from that page.

Pure helpers here — what a query MEANS and how a row is shown. The JSON
endpoint (/app/search, app/dashboard.masthead_search) applies the doors and
the staff lens the same way the pages behind each result do; the widget
(src/app/static/search.js) draws the suggestions and drives the keyboard.

What a query means:
  * "BK-1003", "bk 1003", "booking 1003", "inv-10", "quote" — a reference, or
    the start of one: that kind, by number prefix (a bare kind lists the latest);
  * "1003" — a bare number: every kind, by number prefix;
  * anything else — a customer's name or email (two characters or more).
"""
from __future__ import annotations

import re

KINDS = {"BK": "booking", "QUO": "quote", "INV": "invoice"}          # in the order shown
_WORDS = {"bk": "BK", "booking": "BK", "bookings": "BK",
          "quo": "QUO", "quote": "QUO", "quotes": "QUO", "quotation": "QUO",
          "inv": "INV", "invoice": "INV", "invoices": "INV"}
MAX_RESULTS = 10
PER_KIND = 6
MIN_TEXT = 2
_MAX_Q = 80


def clean(q) -> str:
    return " ".join(str(q or "").split())[:_MAX_Q]


def safe_text(s: str) -> str:
    """A name/email fragment safe inside a PostgREST `ilike` filter — the
    grammar characters (`,()`) and the wildcards are dropped, as
    db.search_bookings does for a contact fragment."""
    return "".join(c for c in (s or "") if c not in ",()%*\"'\\")[:_MAX_Q]


def parse(q) -> dict:
    """{"refs": {"BK": "BK-10", …}, "text": "rob"} — which lookups a query means.
    Exactly one of the two is non-empty (or both empty for a blank query)."""
    s = clean(q)
    out: dict = {"refs": {}, "text": ""}
    if not s:
        return out
    m = re.fullmatch(r"([A-Za-z]+)[\s#:\-]*(\d*)", s)
    if m and m.group(1).lower() in _WORDS:
        prefix = _WORDS[m.group(1).lower()]
        out["refs"][prefix] = f"{prefix}-{m.group(2)}"
        return out
    m = re.fullmatch(r"#?(\d{1,6})", s)
    if m:
        for prefix in KINDS:
            out["refs"][prefix] = f"{prefix}-{m.group(1)}"
        return out
    out["text"] = s
    return out


# ── one row, as the list shows it ────────────────────────────────────────────
def _money(v) -> str:
    try:
        return f"£{float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return ""


def _status(row) -> str:
    return str((row or {}).get("status") or "").replace("_", " ")


def booking_result(b: dict) -> dict:
    from app.dates import pretty_date
    ref = b.get("booking_ref") or "Booking"
    when = pretty_date(str(b.get("booking_date") or "")[:10]) if b.get("booking_date") else ""
    sub = " · ".join(x for x in (when, _status(b), str(b.get("contact") or "")) if x)
    return {"kind": "booking", "ref": ref,
            "label": f"{ref} · {b.get('summary') or 'Booking'}",
            "sub": sub, "href": f"/app/diary/{b.get('id')}"}


def quote_result(qt: dict) -> dict:
    ref = qt.get("quote_ref") or "Quote"
    who = qt.get("customer_name") or qt.get("customer_email") or ""
    label = f"{ref} · {qt.get('title') or who or 'Quote'}"
    sub = " · ".join(x for x in (_money(qt.get("total")), _status(qt),
                                 who if qt.get("title") else "") if x)
    return {"kind": "quote", "ref": ref, "label": label, "sub": sub,
            "href": f"/app/quotes/{qt.get('id')}"}


def invoice_result(inv: dict) -> dict:
    ref = inv.get("invoice_ref") or "Invoice"
    who = inv.get("customer_name") or inv.get("customer_email") or ""
    sub = " · ".join(x for x in (_money(inv.get("total")), _status(inv)) if x)
    return {"kind": "invoice", "ref": ref, "label": f"{ref} · {who or 'Invoice'}",
            "sub": sub, "href": f"/app/invoices/{inv.get('id')}"}


def contact_result(c: dict) -> dict:
    name = (c.get("name") or "").strip()
    email = (c.get("email") or "").strip()
    company = ((c.get("crm_companies") or {}) or {}).get("name") or ""
    sub = " · ".join(x for x in ((email if name else ""), company) if x)
    return {"kind": "contact", "ref": "", "label": name or email or "Customer",
            "sub": sub, "href": f"/app/crm/contacts/{c.get('id')}"}
