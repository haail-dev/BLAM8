"""Day-before booking reminders (Render cron) — the Phase 2B reverse hand-off
(docs/modules/module-agent-handoff.md D10).

For each diary tenant, find CONFIRMED bookings for tomorrow that haven't been reminded;
the relevant specialist frames the facts (TMS for transport — capacity/refs only, never
the vehicle; office direct from the diary row); the base agent composes a short reminder
in the tenant's voice; it's queued as a pending draft for approval — or sent immediately
for full-auto tenants, matching how replies behave. Service role.

    python -m app.jobs.send_reminders
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, scheduling, sms, usage
from app.agents import tms
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

REMINDER_PURPOSE = ("A friendly day-before reminder that the customer has a booking "
                    "tomorrow. Confirm the date and the booking reference, and keep it short.")


def _office_facts(b: dict) -> str:
    date = str(b.get("booking_date"))[:10]
    parts = [f"The customer has a confirmed appointment for tomorrow, {date}."]
    win = b.get("window")
    if win and win != "day":
        parts.append(f"Time: {scheduling.window_label(win)}.")
    if b.get("booking_ref"):
        parts.append(f"Booking reference: {b['booking_ref']}.")
    summary = (b.get("summary") or "").strip()
    if summary:
        parts.append(f"Details: {summary}.")
    return " ".join(parts)


def _facts_for(b: dict) -> str:
    """Transport bookings (sized) are framed by the TMS specialist (the reverse hand-off);
    office bookings frame directly from the diary row."""
    return tms.outbound_reminder(b) if b.get("spaces") is not None else _office_facts(b)


async def _remind_one(tenant_id: str, cfg: dict, auto: bool, b: dict) -> str:
    """Compose + queue (and maybe send) one reminder. Returns 'sent' | 'drafted'.

    Channel routing (docs/modules/module-sms-channel.md §6): an SMS-booked contact, or a
    CRM contact whose preferred channel is SMS, gets the reminder as a text —
    same voice, SMS register, and a reply-C confirm ask that feeds the no-show
    scorer. Everyone else gets email exactly as before."""
    # Job parties (docs/modules/module-job-parties.md): the day-before reminder goes to
    # whoever is ON SITE — the tenant who opens the door, not the landlord who
    # pays. Fallback = the booking's own contact, exactly as before.
    from app import job_parties
    parties = db.list_job_parties(tenant_id, b["id"])
    routing = job_parties.resolve(b, parties)
    to = routing["on_site"].get("email") or b["contact"]
    cc = job_parties.informed_cc(b, parties, exclude=to)
    dest_sms = sms.preferred_sms_destination(tenant_id, to)
    ref = b.get("booking_ref")
    subject = "Reminder: your booking tomorrow" + (f" ({ref})" if ref else "")
    facts = _facts_for(b)
    if dest_sms:
        facts += " Ask them to reply C to confirm they'll be there."
    result = await agent.compose_outbound(cfg, REMINDER_PURPOSE, facts,
                                          channel="sms" if dest_sms else "email",
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, dest_sms or to, subject, f"[day-before reminder] {facts}", result.text,
        status="pending", kind="reminder",
        thread_id=f"sms:{dest_sms}" if dest_sms else b.get("thread_id"),
        channel="sms" if dest_sms else "email")
    db.mark_booking_reminded(b["id"], tenant_id)   # dedupe even if the send fails below
    db.log_agent_event(
        tenant_id, "reminder",
        f"Reminded {to} about {ref or 'their booking'} on {str(b['booking_date'])[:10]}",
        direction="outbound", contact=dest_sms or to, subject=subject, email_log_id=log_id,
        booking_id=b["id"], booking_ref=ref, event_date=str(b["booking_date"])[:10])
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    if auto:
        if dest_sms:
            await sms.send_sms(tenant_id, dest_sms,
                                   disclosure.wrap(cfg, result.text, channel="sms"),
                                   email_log_id=log_id)
        else:
            # Phase 3: the reminder is the ON-SITE party's portal door — their
            # own reference code (minted here on first need) lets the person
            # who actually attends reschedule the visit themselves. Dropped
            # when a cc rides along: the code is the recipient's identity
            # capability and must never reach a cc'd party (the Phase 1 rule).
            footer = ""
            if not cc:
                try:
                    from app import portal
                    footer = portal.email_footer(
                        tenant_id, db.get_tenant_config(tenant_id) or {}, to)
                except Exception:
                    log.warning("reminder portal footer failed (tenant=%s)",
                                tenant_id, exc_info=True)
            await mailbox.send_mail(tenant_id, to, subject,
                                    disclosure.wrap(cfg, result.text) + footer,
                                    cc=cc or None)
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent", f"Sent the reminder to {dest_sms or to} (auto)",
                           direction="outbound", contact=dest_sms or to, subject=subject,
                           email_log_id=log_id, booking_id=b["id"], booking_ref=ref,
                           detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    target = (now + timedelta(days=1)).date().isoformat()   # tomorrow
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("scheduling", "tms")):
        bookings = db.list_bookings_for_reminder(tenant_id, target)
        if not bookings:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        for b in bookings:
            try:
                outcome = await _remind_one(tenant_id, cfg, auto, b)
            except Exception:
                # One bad booking (compose error, send failure) must not stop the run.
                # If it was already queued, it stays pending for a human; if not, it
                # retries next run (mark happens only after the row is created).
                log.warning("reminder failed for booking %s (tenant %s)", b.get("id"), tenant_id,
                            exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"date": target, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
