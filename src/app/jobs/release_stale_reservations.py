"""Storefront reservation-expiry sweep (in-process daily) — docs/os/storefront.md §14.

A reservation never collected holds stock forever. For each storefront tenant, a reservation
still 'reserved' and older than `reservation_hold_days` (config, default 3) is ATOMICALLY
released — its held qty goes back to the shelf (db.shop_release_reservation decrements
qty_reserved per line, writes a 'release' stock_movement, sets status='cancelled') — and then
a short "still want these?" nudge is drafted in the shop's voice (compose_outbound), queued as
a pending draft for approval or auto-sent for full-auto tenants. The Storefront twin of the
proactive reminder jobs: iterate tenants, per-item try/except so one bad row never stops the
run, log_agent_event + usage.record_anthropic. Service role.

    python -m app.jobs.release_stale_reservations
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

DEFAULT_HOLD_DAYS = 3

STALE_PURPOSE = ("A gentle check-in: the customer reserved some items a few days ago but "
                 "hasn't collected them, so the shop has had to put them back on the shelf. "
                 "Ask warmly whether they still want them and to get back in touch to "
                 "re-reserve. Do NOT invent prices or promise the stock is still there — "
                 "keep it short and friendly.")


def _hold_days(cfg: dict) -> int:
    """The tenant's reservation hold window in days (config, default 3). Clamped to a sane
    range so a bad value can never sweep same-day holds or never sweep at all."""
    try:
        n = int(cfg.get("reservation_hold_days") or DEFAULT_HOLD_DAYS)
    except (TypeError, ValueError):
        n = DEFAULT_HOLD_DAYS
    return max(1, min(60, n))


def _facts(tx: dict) -> str:
    items = ", ".join(
        f"{float(it.get('qty') or 1):g}× {it.get('product_name')}"
        for it in (tx.get("shop_transaction_items") or []) if it.get("product_name"))
    return (f"The customer reserved these a few days ago but hasn't collected them: "
            f"{items or 'their order'}. The hold has now lapsed and the items have gone "
            "back on the shelf.")


def _dest(tx: dict) -> str | None:
    contact = tx.get("crm_contacts") or {}
    email = (contact.get("email") or "").strip()
    return email or None


async def _release_one(tenant_id: str, cfg: dict, auto: bool, tx: dict) -> str:
    """Release the stale reservation's stock, then draft (and maybe send) the nudge.
    Returns 'sent' | 'drafted' | 'skipped' (no contact to reach)."""
    # ATOMIC-in-spirit release FIRST — the stock goes back regardless of whether we can
    # reach the customer; the transaction is now 'cancelled'.
    db.shop_release_reservation(tx["id"], tenant_id)
    to = _dest(tx)
    if not to:
        return "skipped"
    subject = "About the items you reserved"
    facts = _facts(tx)
    result = await agent.compose_outbound(cfg, STALE_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[reservation lapsed] {facts}", result.text,
        status="pending", kind="reminder", thread_id=tx.get("thread_id"))
    db.log_agent_event(
        tenant_id, "reservation_lapsed",
        f"Released {to}'s lapsed reservation and asked if they still want the items",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the 'still want these?' nudge to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    released = drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("storefront",)):
        cfg = db.effective_config(tenant_id, None)
        cutoff = (now - timedelta(days=_hold_days(cfg))).isoformat()
        stale = db.shop_reservations_stale(tenant_id, cutoff)
        if not stale:
            continue
        auto = bool(cfg.get("auto_send"))
        for tx in stale:
            try:
                outcome = await _release_one(tenant_id, cfg, auto, tx)
            except Exception:
                # One bad row (compose or send failure) must not stop the sweep. The
                # release itself may already have happened; the nudge just won't send.
                log.warning("stale-reservation release failed for %s (tenant %s)",
                            tx.get("id"), tenant_id, exc_info=True)
                continue
            released += 1
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"released": released, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
