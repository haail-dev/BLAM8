"""Bill the balance the day before a booking ends (Kris 2026-09-24: "how
would a real kennels deal with payments?" — a deposit secures the dates, the
rest is paid on collection).

Invoice settings › "Bill the balance" (invoice_config.BALANCE_MODES):
  off   — by hand, as before (the default for every business);
  draft — a balance invoice is raised and waits for the owner, who's told;
  send  — it goes to the customer, Pay Now link when Stripe is connected.

The balance is the booking's price less the deposit asked for on it (bookings
.deposit_amount, stamped by src/app/deposits.py) — due on the booking's last day
(a stay's collection, a visit's own day). Raised once per booking:
bookings.balance_invoice_id / balance_invoiced_at. Safe to run twice.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import db, deposits, invoice_config
from app.dates import pretty_date

log = logging.getLogger("blautom8.jobs.raise_balances")


async def raise_for_tenant(tenant_id: str, last_day: date) -> list[dict]:
    """Raise the balance for every confirmed, priced booking of this tenant
    that ends on `last_day` and hasn't had one. Returns what it did."""
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id))
    mode = s["balance_mode"]
    if mode == "off":
        return []
    done = []
    for b in db.bookings_awaiting_balance(tenant_id, last_day.isoformat()):
        days = deposits.booking_days(b)
        if not days or days[1] != last_day:
            continue
        email = (b.get("contact") or "").strip()
        try:
            owed = deposits._money(Decimal(str(b.get("quoted_amount") or 0))
                                   - Decimal(str(b.get("deposit_amount") or 0)))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if owed <= 0 or not email:
            db.mark_booking_balance(b["id"], tenant_id, None)   # nothing to bill — never look again
            continue
        ref = b.get("booking_ref") or "booking"
        summary = (b.get("summary") or "Booking").strip()
        contact = db.crm_contact_by_email(tenant_id, email) or {}
        deposit = float(b.get("deposit_amount") or 0)
        inv = db.create_invoice(
            tenant_id, email, contact.get("name") or None, None, None,
            [{"booking_id": b["id"], "description": f"Balance — {summary} ({ref})",
              "amount": owed, "quantity": 1, "unit_price": owed}],
            contact_id=contact.get("id"), booking_id=b["id"], due_date=last_day.isoformat(),
            notes=(f"The balance for {ref}, due {pretty_date(last_day.isoformat())}"
                   + (f" — the £{deposit:,.2f} deposit already asked for is taken off" if deposit else "")),
            created_by="agent")
        if not inv:
            continue
        db.mark_booking_balance(b["id"], tenant_id, inv["id"])
        who = contact.get("name") or email
        status = "draft"
        if mode == "send":
            from app import invoice_send
            try:
                await invoice_send.send_invoice_email(tenant_id, inv, via="auto")
                status = "sent"
            except Exception:
                log.warning("balance send failed (tenant=%s invoice=%s)", tenant_id, inv["id"], exc_info=True)
                status = "draft (send failed)"
        db.log_agent_event(
            tenant_id, "balance_invoice",
            f"Balance invoice {inv.get('invoice_ref')} (£{owed:,.2f}) for {ref} — "
            + (f"sent to {email}" if status == "sent"
               else "ready for you to send, or to take at collection"),
            direction="outbound", contact=email, event_date=last_day.isoformat())
        if status != "sent":
            from app import push
            push.notify(tenant_id, title=f"Balance ready — {who}",
                        body=f"£{owed:,.2f} for {ref}, due {pretty_date(last_day.isoformat())}",
                        url=f"/app/invoices/{inv['id']}", tag=f"balance-{inv['id']}",
                        kind="invoice_balance_ready", contact=email,
                        contact_name=contact.get("name"))
        done.append({"booking": ref, "invoice": inv.get("invoice_ref"), "amount": owed,
                     "status": status})
    return done


async def run(today: date | None = None) -> dict:
    """The daily sweep: bookings ending TOMORROW (UK) get their balance now."""
    from app.scheduling import DIARY_TZ
    today = today or datetime.now(DIARY_TZ).date()
    last_day = today + timedelta(days=1)
    out = {}
    for tenant_id in db.tenants_billing_balances():
        try:
            got = await raise_for_tenant(tenant_id, last_day)
            if got:
                out[tenant_id] = got
        except Exception:
            log.warning("balance sweep failed (tenant=%s)", tenant_id, exc_info=True)
    return {"day": last_day.isoformat(), "raised": out}
