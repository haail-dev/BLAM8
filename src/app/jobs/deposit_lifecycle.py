"""Assets OS — deposit lifecycle sweep (in-process, every 15 min) — docs/os/assets.md §6.

For each assets tenant, release stale UNPAID deposit holds past a cutoff: a deposit still
pending/failed and older than `deposit_hold_days` (config, sensible default) whose hire hasn't
been paid gets its booking cancelled + hire_state cleared + the deposit marked 'released', and a
short "still going ahead?" note drafted in the tenant's voice. Idempotent — a released deposit
leaves the unpaid set, so a re-run is a no-op (the same dedupe posture as the other sweeps).

The other half of the lifecycle (held→paid→refunded/deducted) is driven by the Stripe webhook +
the refund/deduct routes, not this timed sweep; here we only reap the never-completed holds.
Per-tenant + per-item try/except so one bad row never stops the run. Service role.

    python -m app.jobs.deposit_lifecycle
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# How long an unpaid deposit hold may sit before we release it (config, default 3 days).
DEFAULT_HOLD_DAYS = 3

STALE_PURPOSE = ("A gentle check-in: the customer was asked for a deposit to hold their hire a "
                 "few days ago but hasn't paid it, so the hold has now lapsed and the dates have "
                 "been freed up. Ask warmly whether they still want to go ahead and to get back "
                 "in touch to re-book. Do NOT invent prices or promise the dates are still open — "
                 "keep it short and friendly.")


def _hold_days(cfg: dict) -> int:
    """The tenant's deposit-hold window in days (config, default 3). Clamped so a bad value can
    never release same-day holds or never release at all."""
    try:
        n = int(cfg.get("deposit_hold_days") or DEFAULT_HOLD_DAYS)
    except (TypeError, ValueError):
        n = DEFAULT_HOLD_DAYS
    return max(1, min(60, n))


def _facts(booking: dict) -> str:
    summary = booking.get("summary") or "your hire"
    when = str(booking.get("booking_date") or "")[:10]
    return (f"The customer asked to hold {summary}{f' starting {when}' if when else ''} but the "
            "deposit was never paid, so the hold has now lapsed and the dates are free again.")


async def _release_one(tenant_id: str, cfg: dict, auto: bool, deposit: dict) -> str:
    """Release one stale unpaid deposit + its hold, then draft (and maybe send) the nudge.
    Returns 'released' (no reachable contact) | 'drafted' | 'sent'."""
    booking = db.get_booking(deposit["booking_id"], tenant_id)
    # Free the hold FIRST — the dates go back regardless of whether we can reach the customer.
    if booking:
        db.release_hire(booking["id"], tenant_id)
    db.set_deposit_status(deposit["id"], tenant_id, "released")
    to = (booking or {}).get("contact")
    if not db.reachable_contact(to):
        return "released"
    subject = "About the hire you asked to hold"
    facts = _facts(booking or {})
    result = await agent.compose_outbound(cfg, STALE_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[deposit lapsed] {facts}", result.text,
        status="pending", kind="reminder", thread_id=(booking or {}).get("thread_id"))
    db.log_agent_event(
        tenant_id, "deposit_lapsed",
        f"Released {to}'s lapsed deposit hold and asked if they still want the hire",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=(booking or {}).get("id"))
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id, module="email")
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the 'still going ahead?' nudge to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    released = drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("assets",)):
        cfg = db.effective_config(tenant_id, None)
        cutoff = (now - timedelta(days=_hold_days(cfg))).isoformat()
        stale = db.deposits_stale_unpaid(tenant_id, cutoff)
        if not stale:
            continue
        auto = bool(cfg.get("auto_send"))
        for deposit in stale:
            try:
                outcome = await _release_one(tenant_id, cfg, auto, deposit)
            except Exception:
                # One bad row (missing booking, compose/send failure) must not stop the sweep.
                log.warning("deposit release failed for %s (tenant %s)",
                            deposit.get("id"), tenant_id, exc_info=True)
                continue
            released += 1
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"released": released, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
