"""Deposit on confirmation — the deposit asked for in the confirmation itself.

Kris, watching the first filmed kennels take (2026-09-20): the owner raised
the 25% deposit by hand after the stay was confirmed, then marked it paid by
hand. "It should all be done through Stripe." So: when a confirmation goes
out — Approve & send, or the agent's auto-send — and the business has a
deposit set, one deposit invoice is raised for the bookings that email
confirms, and the email carries the amount and how to pay: the Pay Now link
when Stripe is connected (its webhook then marks the invoice paid; nobody
checks a bank), else the bank details (Mark paid stays for that path).

Rules, so the owner can predict it:
- a booking's deposit is the fixed stay deposit the diary stamped on the hold
  (bookings.deposit_amount), else the Invoice-settings default percentage of
  its quoted price; a booking with neither adds nothing;
- one invoice per confirmation email, a line per booking, linked to the first
  booking and to every booking through its lines (the same link the
  duplicate guard reads);
- a booking already on a live invoice is never invoiced again;
- no payment route (Stripe not connected, no bank details) means no invoice
  and one agent event saying why: asking for money with no way to pay it is
  worse than not asking;
- the switch: Invoice settings › "Ask for the deposit when a booking is
  confirmed" (extras.deposit_on_confirm, on by default).

Staged BEFORE the send (the block has to be in the email), finalised after
it, voided if the send fails. Every step is best-effort: a deposit must
never cost a confirmation.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app import db, invoice_config, payments
from app.dates import pretty_date

log = logging.getLogger("blautom8.deposits")

PROPOSED = "proposed"   # the hold status a send confirms (db.resolve_holds_for_log)


def _money(x) -> float:
    """Round the way an invoice does — half up, never Python's half-even
    (25% of £30.50 is £7.63, not £7.62)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_on(inv_settings: dict | None) -> bool:
    """The switch, on unless the owner turned it off."""
    v = ((inv_settings or {}).get("extras") or {}).get("deposit_on_confirm")
    return True if v is None else bool(v)


def deposit_for(booking: dict, percent: float | None) -> tuple[float, str] | None:
    """What one booking asks for: (amount, "fixed") when the hold carries the
    diary's stay deposit, (amount, "percent") for the default percentage of
    its quoted price, None when neither applies."""
    fixed = booking.get("deposit_amount")
    try:
        if fixed is not None and float(fixed) > 0:
            return _money(fixed), "fixed"
    except (TypeError, ValueError):
        pass
    quoted = booking.get("quoted_amount")
    try:
        if percent and float(percent) > 0 and quoted is not None and float(quoted) > 0:
            return _money(Decimal(str(quoted)) * Decimal(str(percent)) / Decimal(100)), "percent"
    except (TypeError, ValueError):
        pass
    return None


def _line_text(booking: dict, kind: str, percent: float | None) -> str:
    ref = booking.get("booking_ref") or "booking"
    summary = (booking.get("summary") or "Booking").strip()
    how = (f"{float(percent):g}% deposit" if kind == "percent"
           else f"{float(percent):g}% deposit (peak dates)" if kind == "peak" else "Deposit")
    return f"{how} — {summary} ({ref})"


def due_date(inv_settings: dict, bookings: list[dict], today: date | None = None) -> str:
    """Due on the tenant's terms, but never after the day before the first
    booking starts — a deposit that falls due after the stay is no deposit —
    and never before today."""
    today = today or datetime.now(timezone.utc).date()
    due = date.fromisoformat(invoice_config.default_due_date(inv_settings, today))
    starts = sorted(str(b.get("booking_date"))[:10] for b in bookings if b.get("booking_date"))
    if starts:
        try:
            eve = date.fromisoformat(starts[0]) - timedelta(days=1)
            if eve < due:
                due = max(eve, today)
        except ValueError:
            pass
    return due.isoformat()


def deposit_block(inv: dict, lines: list[dict], *, pay_link: str | None,
                  bank: str | None, view_link: str) -> str:
    """The lines appended to the confirmation: the amount, how to pay, and
    where the invoice lives. Pay Now when Stripe is connected; else the bank
    details with the invoice reference to quote."""
    ref = inv.get("invoice_ref") or "invoice"
    total = float(inv.get("total") or 0)
    due = pretty_date(inv.get("due_date"))
    out = [f"\n\n---\nDeposit to secure your booking: £{total:,.2f} (invoice {ref}"
           + (f", due {due}" if due else "") + ")"]
    if len(lines) > 1:
        out.append("  " + " · ".join(
            f"{str(l['description']).split(' — ', 1)[-1]} £{float(l['amount']):,.2f}" for l in lines))
    if pay_link:
        out.append(f"Pay online: {pay_link}")
    else:
        out.append(bank or "")
        out.append(f"  Reference: {ref}")
        out.append(f"View the invoice: {view_link}")
    return "\n".join(out)


def _deposit_lines(tenant_id: str, log_id: str, customer_email: str | None, *, client=None):
    """What this draft's send would ask for: (inv_settings, held, lines), or
    None when there is nothing to ask. Reads only — creates nothing."""
    if not customer_email:
        return None
    inv_settings = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    if not is_on(inv_settings):
        return None
    held = [b for b in db.bookings_for_log(log_id, tenant_id, client=client)
            if b.get("status") == PROPOSED]
    if not held:
        return None
    already = set(db.invoiced_booking_ids(tenant_id, [b["id"] for b in held], client=client) or ())
    lines: list[dict] = []
    for b in held:
        if b["id"] in already:
            continue
        # The percentage comes from ONE place (invoice_config.deposit_percent_for):
        # the usual default, a value rule on the booking's price, or a peak range
        # the booking touches (the higher of a value rule and a peak).
        days = booking_days(b)
        percent, why = invoice_config.deposit_percent_for(
            inv_settings, b.get("quoted_amount"), *(days or (None, None)))
        peak = percent if why == "peak" else None
        # A fixed stay deposit stamped on the hold still comes first; otherwise
        # the percentage of the quoted price.
        got = deposit_for(b, None if why == "peak" else percent)
        if peak is not None and b.get("quoted_amount") not in (None, ""):
            try:
                pk = _money(Decimal(str(b["quoted_amount"])) * Decimal(str(peak)) / Decimal(100))
            except (TypeError, ValueError, ArithmeticError):
                pk = 0
            if pk > 0 and (not got or pk > got[0]):
                got = (pk, "peak")
        if not got:
            continue
        amount, kind = got
        lines.append({"booking_id": b["id"],
                      "description": _line_text(b, kind, percent),
                      "amount": amount, "quantity": 1, "unit_price": amount})
    return (inv_settings, held, lines) if lines else None


def booking_days(b: dict) -> tuple[date, date] | None:
    """A booking's first and last day — a stay's arrival and departure, else
    its one day."""
    from app import slotgrid
    try:
        span = slotgrid.stay_span(b)
        if span:
            return (slotgrid.abs_to_datetime(span[0]).date(),
                    slotgrid.abs_to_datetime(span[0] + span[1]).date())
        d = date.fromisoformat(str(b.get("booking_date"))[:10])
        return d, d
    except (TypeError, ValueError):
        return None


def _peak_for(inv_settings: dict, b: dict) -> float | None:
    days = booking_days(b)
    return invoice_config.peak_percent(inv_settings, *days) if days else None


def preview_for_log(tenant_id: str, log_id: str, customer_email: str | None, *,
                    client=None) -> dict | None:
    """What the thumbs-up will send WITH this draft — shown on the draft
    before it goes (Kris 2026-09-24: Sophie's confirmation carried a £7.63
    deposit invoice nobody saw on the draft). None when nothing rides along,
    or when the send would skip it (no way for the customer to pay)."""
    try:
        got = _deposit_lines(tenant_id, log_id, customer_email, client=client)
        if not got:
            return None
        inv_settings, held, lines = got
        if not payments.tenant_can_take_payments(tenant_id, client=client) \
                and not invoice_config.bank_details(inv_settings):
            return None
        on = {l["booking_id"] for l in lines}
        return {"total": round(sum(float(l["amount"]) for l in lines), 2),
                "refs": ", ".join(b.get("booking_ref") or "booking" for b in held if b["id"] in on)}
    except Exception:  # noqa: BLE001
        log.warning("deposit preview failed (tenant=%s log=%s)", tenant_id, log_id, exc_info=True)
        return None


def stage_for_log(tenant_id: str, log_id: str, customer_email: str | None, *,
                  client=None) -> tuple[dict | None, str]:
    """Raise the deposit invoice for the holds this draft is about to confirm.
    Returns (invoice, block): the block is what to append to the outgoing
    email, "" when there is nothing to ask for. Never raises."""
    try:
        got = _deposit_lines(tenant_id, log_id, customer_email, client=client)
        if not got:
            return None, ""
        inv_settings, held, lines = got
        refs = ", ".join(b.get("booking_ref") or "booking" for b in held
                         if b["id"] in {l["booking_id"] for l in lines})
        can_pay = bool(payments.tenant_can_take_payments(tenant_id, client=client))
        bank = invoice_config.bank_details(inv_settings)
        if not can_pay and not bank:
            db.log_agent_event(
                tenant_id, "deposit_skipped",
                f"No deposit asked for on {refs}: Stripe isn't connected and there are "
                "no bank details in Settings › Getting paid, so the customer would have no way "
                "to pay it. Connect Stripe or add bank details and the next confirmation "
                "will carry the deposit.",
                direction="outbound", contact=customer_email, email_log_id=log_id, client=client)
            return None, ""
        try:
            contact = db.crm_contact_by_email(tenant_id, customer_email, client=client) or {}
        except Exception:  # noqa: BLE001
            contact = {}
        inv = db.create_invoice(
            tenant_id, customer_email, (contact.get("name") or None), None, None, lines,
            client=client, contact_id=contact.get("id"), booking_id=lines[0]["booking_id"],
            due_date=due_date(inv_settings, [b for b in held if b["id"] in {l["booking_id"] for l in lines}]),
            notes=f"Deposit to secure {refs} — raised with the confirmation",
            created_by="agent")
        if not inv:
            return None, ""
        view = payments.payment_url(inv["id"])
        block = deposit_block(inv, lines, pay_link=(view if can_pay else None), bank=bank, view_link=view)
        return inv, block
    except Exception:  # noqa: BLE001
        log.warning("deposit staging failed (tenant=%s log=%s)", tenant_id, log_id, exc_info=True)
        return None, ""


def after_send(tenant_id: str, inv: dict | None, *, log_id: str | None,
               customer_email: str | None, client=None) -> None:
    """The confirmation went: the deposit invoice is sent (its link stamped),
    each booking remembers its deposit, and the owner's log and the CRM
    timeline say so. Best-effort — the email is already gone."""
    if not inv:
        return
    try:
        if payments.tenant_can_take_payments(tenant_id, client=client):
            db.set_invoice_payment_link(inv["id"], tenant_id, payments.payment_url(inv["id"]), client=client)
        db.set_invoice_status(inv["id"], tenant_id, "sent", client=client)
        for l in db.list_invoice_lines(inv["id"], tenant_id, client=client) or []:
            if l.get("booking_id"):
                db.set_booking_deposit(l["booking_id"], tenant_id, float(l.get("amount") or 0), client=client)
        ref = inv.get("invoice_ref")
        total = float(inv.get("total") or 0)
        db.log_agent_event(
            tenant_id, "sent",
            f"Deposit invoice {ref} (£{total:,.2f}) went out with the confirmation to {customer_email}",
            direction="outbound", contact=customer_email, subject=f"Invoice {ref}",
            email_log_id=log_id, client=client)
        from app import invoice_send
        invoice_send._crm_invoice_activity(
            tenant_id, inv, activity_type="invoice",
            subject=f"Deposit invoice {ref} sent with the confirmation",
            summary=f"£{total:,.2f}, due {pretty_date(inv.get('due_date'))}")
    except Exception:  # noqa: BLE001
        log.warning("deposit finalise failed (tenant=%s invoice=%s)", tenant_id, inv.get("id"), exc_info=True)


def discard(tenant_id: str, inv: dict | None, *, client=None) -> None:
    """The send failed after staging: void the invoice so Money never shows a
    deposit that was never asked for."""
    if not inv:
        return
    try:
        db.set_invoice_status(inv["id"], tenant_id, "void", client=client)
    except Exception:  # noqa: BLE001
        log.warning("deposit discard failed (tenant=%s invoice=%s)", tenant_id, inv.get("id"), exc_info=True)
