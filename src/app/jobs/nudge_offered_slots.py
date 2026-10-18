"""Pre-expiry offer nudge (smart booking Phase 3 — docs/modules/module-smart-booking-offers.md).

When the agent offered a customer a choice of times, each slot is reserved
exclusively for them until a stated deadline (Phase 1). This sweep fires ONE
friendly chase per customer as that deadline approaches — "still want Tuesday
10:00? It's released at 17:00 today" — because the deadline in their inbox is
real and the fence-sitters convert. Runs every 30 minutes (scheduler.run_every);
the 4-hour lead window plus the offer_nudged_at stamp means each offer set is
nudged at most once, and an offer whose whole TTL is shorter than the lead time
is never nudged at all (the original email is still fresh).

Same three-beat as every outbound job (send_reminders is the template): compose
in the tenant's voice → queue a pending draft → send immediately only for
full-auto tenants, disclosure-wrapped. Tenant kill-switch: scheduling_config.
offer_nudge_enabled (Diary settings, default ON). A customer who has replied
since the offers went out is never chased — the drafter owns that thread now.

    python -m app.jobs.nudge_offered_slots
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, scheduling, usage
from app.scheduling import DIARY_TZ
from app import offer_deadlines

log = logging.getLogger(__name__)

# Nudge when release is within this window; require the offer to be at least
# this old. Together: a 24h-TTL offer is nudged at the ~20h mark, one whose
# whole TTL is under the lead time never is.
NUDGE_LEAD_HOURS = 4

NUDGE_PURPOSE = (
    "A short, friendly nudge that the appointment time(s) being held for this "
    "customer will be released soon. State the release deadline and each held "
    "time exactly as given, and ask them to reply with the one that suits (or "
    "to say if none do). Do not pressure, and NEVER offer, promise or invent "
    "any other time."
)


def _deadline(rows: list[dict]) -> datetime | None:
    """Earliest release, used only to decide whether a set has a deadline."""
    exps = []
    for b in rows:
        try:
            exps.append(datetime.fromisoformat(str(b["expires_at"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            continue
    return min(exps).astimezone(DIARY_TZ) if exps else None


def _slot_line(b: dict) -> str:
    day = str(b.get("booking_date"))[:10]
    try:
        label = datetime.fromisoformat(day).strftime("%A %d %B")
    except ValueError:
        label = day
    win = b.get("window")
    if win and win != "day":
        label += f", {scheduling.window_label(win)}" if win in ("am", "pm") else f" at {win}"
    if b.get("booking_ref"):
        label += f" (ref {b['booking_ref']})"
    return label


async def _nudge_contact(tenant_id: str, cfg: dict, auto: bool, contact: str,
                         rows: list[dict]) -> str:
    """Compose + queue (and maybe send) one nudge covering the contact's whole
    offer set. Returns 'sent' | 'drafted' | 'skipped'."""
    deadline = _deadline(rows)
    if deadline is None:
        return "skipped"
    # The customer replied after the offers went out → the reply drafter owns
    # this conversation; a parallel chase would talk over it.
    oldest = min(str(b.get("created_at") or "") for b in rows)
    if oldest and db.has_inbound_from_since(tenant_id, contact, oldest):
        db.mark_offers_nudged([b["id"] for b in rows], tenant_id)
        return "skipped"
    subject = "Your reserved times are about to be released"
    line = offer_deadlines.deadline_line(rows)
    facts = ("Held times: " + "; ".join(_slot_line(b) for b in rows) + ". " + line
             + " Include the deadline_line verbatim; do not paraphrase any deadline.")
    result = await agent.compose_outbound(cfg, NUDGE_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="offer_nudge")
    reply = offer_deadlines.enforce(result.text, [line])
    log_id, _ = db.insert_email_log(
        tenant_id, contact, subject, f"[offer nudge] {facts}", reply,
        status="pending", kind="offer_nudge",
        thread_id=next((b.get("thread_id") for b in rows if b.get("thread_id")), None))
    db.mark_offers_nudged([b["id"] for b in rows], tenant_id)  # dedupe even if the send fails
    db.log_agent_event(
        tenant_id, "offer_nudge",
        f"Nudged {contact}: {line}",
        direction="outbound", contact=contact, subject=subject, email_log_id=log_id,
        booking_id=rows[0]["id"], booking_ref=rows[0].get("booking_ref"))
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="offer_nudge", email_log_id=log_id)
    if auto:
        await mailbox.send_mail(tenant_id, contact, subject, disclosure.wrap(cfg, reply))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent", f"Sent the offer nudge to {contact} (auto)",
                           direction="outbound", contact=contact, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    window_end = (now + timedelta(hours=NUDGE_LEAD_HOURS)).isoformat()
    min_age = (now - timedelta(hours=NUDGE_LEAD_HOURS)).isoformat()
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("scheduling",)):
        sched_cfg = db.get_scheduling_config(tenant_id) or {}
        if not sched_cfg.get("offer_nudge_enabled", True):
            continue
        rows = db.list_offers_to_nudge(tenant_id, window_end, min_age)
        if not rows:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        by_contact: dict = {}
        for b in rows:
            by_contact.setdefault(b["contact"], []).append(b)
        for contact, group in by_contact.items():
            try:
                # The lead window selects the contact, not the contents of the
                # reminder. Later-expiring siblings belong in the same nudge.
                group = db.offered_bookings_for_contact(tenant_id, contact)
                if not group:
                    continue
                outcome = await _nudge_contact(tenant_id, cfg, auto, contact, group)
            except Exception:
                # One bad set (compose error, send failure) must not stop the run.
                # If already queued it stays pending; if not, it retries next tick
                # (the nudged stamp lands only after the row is created).
                log.warning("offer nudge failed for %s (tenant %s)", contact, tenant_id,
                            exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
