"""Recurring service reminders (in-process daily) — the Phase 3 reverse hand-off
(docs/modules/module-field-service.md D5).

For each field-service tenant, find completed jobs whose recurring service is due soon
(within REMINDER_LEAD_DAYS) and not yet chased; the field-service specialist frames the
rebook facts; the base agent composes a short nudge in the tenant's voice; it's queued as
a pending draft for approval — or sent immediately for full-auto tenants. The trades twin
of src/app/jobs/send_reminders.py (day-before), forward-dated + recurring. Service role.

    python -m app.jobs.send_service_reminders
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, sms, usage
from app.agents import field_service as fs
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# Chase the rebook this many days before the service falls due (and any overdue ones).
REMINDER_LEAD_DAYS = 14

SERVICE_PURPOSE = ("A friendly nudge that the customer is due a recurring service (for "
                   "example an annual boiler service or safety check). Invite them to get "
                   "back in touch to rebook; do NOT promise or suggest a date — keep it "
                   "short and warm.")


async def _remind_one(tenant_id: str, cfg: dict, auto: bool, b: dict) -> str:
    """Compose + queue (and maybe send) one service reminder. Returns 'sent' | 'drafted'."""
    to = b["contact"]
    # Channel routing (docs/modules/module-sms-channel.md §6): SMS-preferring customers get
    # the nudge as a text in the same voice; everyone else gets email as before.
    dest_sms = sms.preferred_sms_destination(tenant_id, to)
    job = (b.get("job_type") or "service").strip()
    subject = f"Time to book your {job}"
    facts = fs.outbound_reminder(b)
    result = await agent.compose_outbound(cfg, SERVICE_PURPOSE, facts,
                                          channel="sms" if dest_sms else "email",
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, dest_sms or to, subject, f"[service reminder] {facts}", result.text,
        status="pending", kind="reminder",
        thread_id=f"sms:{dest_sms}" if dest_sms else b.get("thread_id"),
        channel="sms" if dest_sms else "email")
    db.mark_service_reminded(b["id"], tenant_id)   # dedupe even if the send fails below
    due = str(b.get("service_due_date"))[:10]
    db.log_agent_event(
        tenant_id, "reminder",
        f"Nudged {to} to rebook their {job} (due {due})",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=b["id"], booking_ref=b.get("booking_ref"), event_date=due)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    if auto:
        if dest_sms:
            await sms.send_sms(tenant_id, dest_sms,
                                   disclosure.wrap(cfg, result.text, channel="sms"),
                                   email_log_id=log_id)
        else:
            await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the service reminder to {dest_sms or to} (auto)",
                           direction="outbound", contact=dest_sms or to, subject=subject,
                           email_log_id=log_id, booking_id=b["id"], detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    cutoff = (now + timedelta(days=REMINDER_LEAD_DAYS)).date().isoformat()
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("fsm",)):
        jobs = db.list_jobs_due_service(tenant_id, cutoff)
        if not jobs:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        for b in jobs:
            try:
                outcome = await _remind_one(tenant_id, cfg, auto, b)
            except Exception:
                # One bad job (compose error, send failure) must not stop the run. If it
                # was queued, it stays pending for a human; if not, it retries next run
                # (the mark happens only after the row is created).
                log.warning("service reminder failed for booking %s (tenant %s)",
                            b.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"cutoff": cutoff, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
