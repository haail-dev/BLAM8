"""Daily overdue-invoice check (invoicing directive §6.1) — now the chase engine.

Flips issued-but-unsettled invoices past their due date to 'overdue', and at
each milestone (7 / 14 / 30 days) DOES the chasing rather than asking the owner
to: a payment chase in the tenant's learned voice, tone escalating with the
milestone (src/app/invoice_chase.py), lands in the draft/approval queue — or goes
straight out for auto-send tenants — and the push tells the owner what was
done, not what to go do. The proactive doctrine, same as check_quote_chases.

The chase stands down by itself: an inbound from the customer since the last
touch means the conversation is live, so the milestone falls back to a plain
"it's overdue" push and the reply flow owns the thread. An invoice the owner
has already chased 3+ times stops getting drafts and escalates with the
distinct "hasn't paid" framing (directive §5.3) — at that point it's a phone
call, not a fourth email.

overdue_notified_days records the last milestone fired (stamped BEFORE acting,
so a redeploy or double-run never re-notifies), and the chase draft's
deterministic source_message_id makes double-staging physically impossible on
top of that.

Registered in src/app/main.py at 08:45 UTC. Manual run: python -m app.jobs.check_overdue_invoices
"""
import logging
from datetime import datetime, timezone

from app import db, invoice_chase, push

log = logging.getLogger(__name__)

MILESTONES = (30, 14, 7)   # checked biggest-first; fire the highest one crossed

# Per-tenant, per-run ceiling on milestone touches (drafts/sends/escalations
# AND their pushes). A fresh Xero import can land thirty overdue invoices at
# once — thirty chase drafts the next morning is a wall of noise, not help.
# Deferred rows stay UNstamped, so each following run picks up the next batch,
# oldest first.
MAX_TENANT_TOUCHES = 5


def _days_overdue(due_date: str, today) -> int:
    try:
        return (today - datetime.strptime(str(due_date)[:10], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return 0


async def run() -> dict:
    today = datetime.now(timezone.utc).date()
    # Oldest due date first, so a capped tenant's most-overdue invoices are
    # the ones that get today's touches.
    rows = sorted(db.list_unpaid_due_invoices(today.isoformat()),
                  key=lambda r: str(r.get("due_date") or "9999"))
    flipped = notified = escalated = drafted = sent = deferred = 0
    cfg_cache: dict = {}
    touches: dict = {}
    for inv in rows:
        try:
            tenant_id, ref = inv["tenant_id"], inv.get("invoice_ref") or "invoice"
            days = _days_overdue(inv.get("due_date"), today)
            if days <= 0:
                continue
            milestone = next((m for m in MILESTONES if days >= m), 0)
            already = int(inv.get("overdue_notified_days") or 0)
            crossing = milestone > already
            if crossing and touches.get(tenant_id, 0) >= MAX_TENANT_TOUCHES:
                # Deliberately BEFORE the stamp: an unstamped row is picked up
                # by a later run; a stamped-but-untouched one never would be.
                deferred += 1
                continue
            if inv.get("status") == "sent" or crossing:
                # Stamp first (dedupe even if what follows fails), mirror of
                # mark_booking_reminded's ordering.
                db.set_invoice_overdue_milestone(inv["id"], tenant_id,
                                                 max(milestone, already))
                flipped += 1
            if not crossing:
                continue
            touches[tenant_id] = touches.get(tenant_id, 0) + 1
            total = float(inv.get("total") or 0)
            who = inv.get("customer_name") or inv.get("customer_email") or "customer"
            reminders = int(inv.get("reminders_sent") or 0)
            if reminders >= 3:
                # The distinct escalation: they're not ignoring conversation —
                # they're not paying. No fourth email; this is a phone call.
                title = f"{who} hasn't paid {ref} after {reminders} reminders"
                body = f"£{total:,.2f}, {days} days overdue — want to escalate?"
                summary = (f"{who} hasn't responded to {reminders} payment "
                           f"reminders for {ref} (£{total:,.2f}, {days} days "
                           "overdue). Want me to escalate?")
                url = f"/app/invoices/{inv['id']}"
                escalated += 1
            else:
                if tenant_id not in cfg_cache:
                    cfg_cache[tenant_id] = db.effective_config(tenant_id, None)
                cfg = cfg_cache[tenant_id]
                outcome, _ = await invoice_chase.stage_chase(
                    tenant_id, cfg, inv, days,
                    auto=bool(cfg.get("auto_send")), dedupe_label=milestone)
                to = (inv.get("customer_email") or "").strip()
                if outcome == "sent":
                    sent += 1
                    title = f"Chased {ref} — {days} days overdue"
                    body = f"£{total:,.2f} · payment reminder sent to {who} (auto)"
                    summary = (f"Sent a payment chase for {ref} (£{total:,.2f}, "
                               f"{days} days overdue) to {who} (auto)")
                    url = f"/app/invoices/{inv['id']}"
                elif outcome == "drafted":
                    drafted += 1
                    title = f"Payment chase drafted — {ref} is {days} days overdue"
                    body = f"£{total:,.2f} from {who} — review and send"
                    summary = (f"Invoice {ref} (£{total:,.2f}) to {who} is "
                               f"{days} days overdue — chase drafted, waiting "
                               "for your yes")
                    url = "/app/conversations/view?contact=" + to
                else:
                    # No email to chase, or they've written back since the last
                    # touch — the thread owns it; the owner just gets the fact.
                    title = f"Invoice {ref} is {days} days overdue"
                    body = f"£{total:,.2f} from {who}" + (
                        " — they've been in touch, see the thread"
                        if outcome == "stood_down" else "")
                    summary = (f"Invoice {ref} (£{total:,.2f}) to {who} is now "
                               f"{days} days overdue")
                    url = f"/app/invoices/{inv['id']}"
            db.log_agent_event(tenant_id, "invoice_overdue", summary,
                               contact=inv.get("customer_email"))
            push.notify(tenant_id, title=title, body=body, url=url,
                        tag=f"invoice-overdue-{inv['id']}", kind="invoice_overdue",
                        contact=inv.get("customer_email"),
                        contact_name=inv.get("customer_name"))
            notified += 1
        except Exception:
            # One bad invoice must not stop the run.
            log.warning("overdue check failed for invoice %s", inv.get("id"),
                        exc_info=True)
    return {"checked": len(rows), "flipped": flipped, "notified": notified,
            "escalated": escalated, "drafted": drafted, "sent": sent,
            "deferred": deferred}


if __name__ == "__main__":
    import asyncio
    print(asyncio.run(run()))
