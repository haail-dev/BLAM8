"""Quotes module — pure helpers (docs/modules/module-quotes.md).

The invoicing.py sibling: no DB, no routes. Three jobs:
  * shape draft quote LINES from what already priced the work (a booking's
    stored quoted_amount — the same number the reply drafter computed);
  * the deposit maths (net-side, so VAT composes instead of compounding);
  * shape INVOICE lines from an accepted quote's acceptance_snapshot — the
    frozen truth of what was agreed, never the live (editable) lines.

Line contract matches invoices: `amount` is the line TOTAL in the business's own
price basis - net of VAT when VAT is added on top (the default), or the stated
price when the document's prices include VAT (`prices_include_vat` True).
"""
from __future__ import annotations


def lines_from_booking(booking: dict) -> list[dict]:
    """Draft quote lines for a booking. One line at the stored quoted_amount when
    the reply drafter (or a previous quote) already priced it; a blank-amount
    line for the owner to fill otherwise — a quote must never invent a price."""
    desc = (booking.get("job_type") or booking.get("description") or "").strip()
    ref = (booking.get("booking_ref") or "").strip()
    label = desc or "Work as discussed"
    if ref:
        label = f"{label} ({ref})"
    stored = booking.get("quoted_amount")
    amount = round(float(stored), 2) if stored is not None else 0.0
    return [{"description": label[:200], "quantity": 1, "unit_price": amount,
             "amount": amount, "booking_id": booking.get("id")}]


def selected_lines(lines: list[dict]) -> list[dict]:
    return [l for l in lines if l.get("is_selected", True)]


def amount_as_written(doc: dict | None) -> float:
    """What a document's lines add up to AS WRITTEN: its subtotal when VAT is
    added on top, its total when its prices include VAT (the document's own
    stored basis, never the current setting). Deposits and balances are taken
    and netted off in this basis - never mix a subtotal with a total."""
    doc = doc or {}
    key = "total" if doc.get("prices_include_vat") is True else "subtotal"
    try:
        return float(doc.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def ticked_net_sum(lines: list[dict]) -> float | None:
    """The sum of the selected lines flagged "counts towards the deposit rule"
    AS WRITTEN (the quote's subtotal basis when VAT is added, the stated prices
    when they include VAT - the sum is the same either way), or None when no
    selected line carries the flag. A line without the key is not ticked."""
    ticked = [l for l in selected_lines(lines) if l.get("counts_to_deposit_rule")]
    if not ticked:
        return None
    return round(sum(float(l.get("amount") or 0) for l in ticked), 2)


def deposit_net_amount(quote: dict) -> float | None:
    """The amount a deposit invoice's line carries, IN THE LINE BASIS, or None
    when no deposit is configured. (The name predates prices-include-VAT: it is
    "net" only when VAT is added on top.) Computed on the accepted document
    (snapshot first - the agreed document - falling back to the live quote
    pre-acceptance): pct of the subtotal when VAT is added, pct of the TOTAL when
    the quote's prices include VAT, or of the ticked lines' sum as written
    (`deposit_base`) in both. split_vat then derives the invoice's totals on the
    quote's basis, so the customer's deposit is exactly pct% of the quote as they
    were told it, and VAT is never charged on VAT."""
    pct = quote.get("deposit_percent")
    if pct is None:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    if not (0 < pct <= 100):
        return None
    snap = quote.get("acceptance_snapshot") or {}
    # The amount the percentage is taken of: the quote's deposit_base (the sum of
    # the lines a value rule counted) when it has one, else the whole subtotal.
    # The snapshot's frozen base wins once accepted.
    part = snap.get("deposit_base", quote.get("deposit_base"))
    if part is not None:
        base = part
    else:
        merged = {**quote, **{k: snap[k] for k in ("subtotal", "total", "prices_include_vat")
                              if k in snap}}
        base = amount_as_written(merged)
    try:
        base = float(base or 0)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return round(base * pct / 100.0, 2)


def deposit_invoice_lines(quote: dict) -> list[dict]:
    """The one-line deposit invoice minted at acceptance."""
    net = deposit_net_amount(quote)     # the line amount, in the line basis
    if net is None:
        return []
    ref = quote.get("quote_ref") or "quote"
    pct = float(quote.get("deposit_percent") or 0)
    label = f"Deposit ({pct:g}%) — {ref}"
    title = (quote.get("title") or "").strip()
    if title:
        label += f": {title}"
    return [{"description": label[:200], "quantity": 1,
             "unit_price": net, "amount": net,
             "booking_id": quote.get("booking_id")}]


def invoice_lines_from_quote(quote: dict, *, deposit_net: float | None = None,
                             deposit_paid: bool = True) -> list[dict]:
    """Invoice lines for the full conversion of an ACCEPTED quote.

    Reads acceptance_snapshot — the frozen agreed document — never the live lines
    (which stay editable pre-acceptance and could have drifted). A deposit already
    invoiced becomes a NEGATIVE net line, so create_invoice's VAT applies to the
    remaining net and the invoice total is exactly the outstanding balance.
    The line says "paid" only when the deposit invoice is paid; an unpaid one
    reads "invoiced" (it is still owed on its own invoice — the two together
    make the quote total).
    Returns [] when there is nothing to convert (no snapshot = not accepted)."""
    snap = quote.get("acceptance_snapshot") or {}
    lines = []
    for l in snap.get("lines") or []:
        desc = (l.get("description") or "").strip()
        if not desc:
            continue
        try:
            amount = round(float(l.get("amount") or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        lines.append({"description": desc, "amount": amount,
                      "quantity": l.get("quantity") or 1,
                      "unit_price": l.get("unit_price", amount),
                      "booking_id": l.get("booking_id"),
                      "kb_entry_id": l.get("kb_entry_id")})
    if not lines:
        return []
    if deposit_net:
        ref = quote.get("deposit_invoice_ref") or "deposit invoice"
        word = "paid" if deposit_paid else "invoiced"
        lines.append({"description": f"Less deposit {word} ({ref})"[:200],
                      "quantity": 1, "unit_price": -abs(round(float(deposit_net), 2)),
                      "amount": -abs(round(float(deposit_net), 2)),
                      "booking_id": None})
    return lines


def conversion_lines(quote: dict, deposit_invoice: dict | None = None) -> list[dict]:
    """The one shared quote→invoice conversion shaper (dashboard route + the
    concierge executor both call this — never re-derive it). Pure: pass the
    deposit invoice ROW when the quote has one; a dead (cancelled/void) deposit
    never credits."""
    deposit_net = None
    deposit_paid = False
    if deposit_invoice and deposit_invoice.get("status") not in ("cancelled", "void"):
        try:
            total = float(deposit_invoice.get("total") or 0)
            deposit_paid = (deposit_invoice.get("status") == "paid"
                            or (total > 0 and float(deposit_invoice.get("amount_paid") or 0) >= total))
        except (TypeError, ValueError):
            deposit_paid = False
        # The deposit as written (its subtotal, or its total when its prices
        # include VAT) so the credit nets off in the same basis as the lines.
        deposit_net = amount_as_written(deposit_invoice) or None
        if deposit_net:
            quote = {**quote,
                     "deposit_invoice_ref": deposit_invoice.get("invoice_ref")}
    return invoice_lines_from_quote(quote, deposit_net=deposit_net, deposit_paid=deposit_paid)


def acceptance_summary(quote: dict) -> str:
    """One line for pushes / agent log / CRM timeline."""
    ref = quote.get("quote_ref") or "quote"
    total = float(quote.get("total") or 0)
    who = quote.get("accepted_name") or quote.get("customer_name") \
        or quote.get("customer_email") or "Customer"
    return f"{who} accepted {ref} (£{total:,.2f})"
