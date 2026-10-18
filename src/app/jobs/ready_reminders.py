"""Storefront ready-to-collect nudges (in-process daily) — docs/os/storefront.md §3/§6.

For each storefront tenant, find reservations whose ready_date is today (or overdue) and
still status='reserved'; compose a short "your order's ready to collect" in the shop's
learned voice; queue it as a pending draft for approval — or send immediately for full-auto
tenants — and flip the reservation reserved→ready (the §3 transition). The Storefront twin
of src/app/jobs/send_service_reminders.py: iterate tenants, per-item try/except so one bad draft
never stops the run, compose_outbound → insert_email_log → optional auto-send → log_agent_event
+ usage.record_anthropic. Service role.

    python -m app.jobs.ready_reminders
"""
import asyncio
import logging
from datetime import datetime

from app import agent, db, disclosure, mailbox, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

READY_PURPOSE = ("A warm, brief heads-up that the customer's order is ready to collect from "
                 "the shop. Tell them it's ready and invite them to pop in. Do NOT invent a "
                 "collection time, price or anything beyond the facts — keep it short.")


def _facts(tx: dict) -> str:
    """The collection facts for one reservation — the items and the ready date, nothing else."""
    items = ", ".join(
        f"{float(it.get('qty') or 1):g}× {it.get('product_name')}"
        for it in (tx.get("shop_transaction_items") or []) if it.get("product_name"))
    parts = [f"The customer's order is ready to collect: {items or 'their order'}."]
    if tx.get("ready_date"):
        parts.append(f"It was booked to be ready on {str(tx['ready_date'])[:10]}.")
    return " ".join(parts)


def _dest(tx: dict) -> str | None:
    """Who to nudge — the reservation's CRM contact email. None (no contact / walk-in with
    no email) skips the row: there's nobody to email."""
    contact = tx.get("crm_contacts") or {}
    email = (contact.get("email") or "").strip()
    return email or None


async def _nudge_one(tenant_id: str, cfg: dict, auto: bool, tx: dict) -> str:
    """Compose + queue (and maybe send) one ready nudge, then flip reserved→ready.
    Returns 'sent' | 'drafted' | 'skipped' (no contact to reach)."""
    to = _dest(tx)
    if not to:
        # Nobody to email, but the order IS ready — advance it so the daily surface and the
        # stale-reservation sweep both see the right state; the counter handles collection.
        db.shop_set_transaction_status(tx["id"], tenant_id, "ready")
        return "skipped"
    subject = "Your order's ready to collect"
    facts = _facts(tx)
    result = await agent.compose_outbound(cfg, READY_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[ready to collect] {facts}", result.text,
        status="pending", kind="ready", thread_id=tx.get("thread_id"))
    # Flip reserved→ready now so a re-run never re-nudges the same reservation (dedupe),
    # even if the send below fails — the row is queued and the state has moved on.
    db.shop_set_transaction_status(tx["id"], tenant_id, "ready")
    db.log_agent_event(
        tenant_id, "ready",
        f"Told {to} their order is ready to collect",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the ready-to-collect nudge to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    today = datetime.now(DIARY_TZ).date().isoformat()
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("storefront",)):
        reservations = db.shop_reservations_ready_today(tenant_id, today)
        if not reservations:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        for tx in reservations:
            try:
                outcome = await _nudge_one(tenant_id, cfg, auto, tx)
            except Exception:
                # One bad reservation (compose error, send failure) must not stop the run.
                log.warning("ready nudge failed for reservation %s (tenant %s)",
                            tx.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"date": today, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
