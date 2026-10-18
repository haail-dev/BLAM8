"""Practice OS recall reminders (in-process daily) — the recall wedge (docs/os/practice.md §6.1).

For each practice tenant, find completed appointments whose recall is due soon (within
RECALL_LEAD_DAYS, plus overdue) and not yet chased; compose a short, personal nudge in the
practice's own voice inviting the patient to rebook; queue it as a pending draft for
approval — or send immediately for full-auto tenants. The Practice twin of
src/app/jobs/send_service_reminders.py: byte-for-byte the same compose → queue → dedupe →
auto-send skeleton, only the work-set query and the purpose string differ. Service role.

    python -m app.jobs.send_recall
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, sms, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# Chase the recall this many days before it falls due (and any overdue ones).
RECALL_LEAD_DAYS = 14

RECALL_PURPOSE = ("A warm nudge that the patient is due their routine appointment again "
                  "(for example a 6-month dental check-up or a hygiene visit). Invite them "
                  "to rebook; do NOT promise or suggest a specific date or time — keep it "
                  "short, personal and in the practice's voice. If they have a usual "
                  "practitioner, mention them by name.")


def _recall_facts(b: dict) -> str:
    """Neutral facts for the composer: the service, the last visit, and the usual
    practitioner when known. Never a clinical detail — recall is a rebook nudge."""
    service = (b.get("summary") or "your routine appointment").strip()
    parts = [f"The patient is due their {service} again."]
    last = str(b.get("booking_date") or "")[:10]
    if last:
        parts.append(f"They were last seen on {last}.")
    practitioner = (b.get("practitioner") or "").strip()
    if practitioner:
        parts.append(f"Their usual practitioner is {practitioner}.")
    parts.append("Invite them to get back in touch to rebook; do not offer a specific slot.")
    return " ".join(parts)


async def _remind_one(tenant_id: str, cfg: dict, auto: bool, b: dict) -> str:
    """Compose + queue (and maybe send) one recall. Returns 'sent' | 'drafted'."""
    to = b["contact"]
    # Channel routing (docs/modules/module-sms-channel.md §6): SMS-preferring patients get
    # the recall as a text in the same voice; everyone else gets email as before.
    dest_sms = sms.preferred_sms_destination(tenant_id, to)
    service = (b.get("summary") or "appointment").strip()
    subject = f"Time to book your {service}"
    facts = _recall_facts(b)
    result = await agent.compose_outbound(cfg, RECALL_PURPOSE, facts,
                                          channel="sms" if dest_sms else "email",
                                          tenant_id=tenant_id, log_operation="recall")
    log_id, _ = db.insert_email_log(
        tenant_id, dest_sms or to, subject, f"[recall] {facts}", result.text,
        status="pending", kind="recall",
        thread_id=f"sms:{dest_sms}" if dest_sms else b.get("thread_id"),
        channel="sms" if dest_sms else "email")
    db.mark_recall_reminded(b["id"], tenant_id)   # dedupe even if the send fails below
    due = str(b.get("recall_due_date"))[:10]
    db.log_agent_event(
        tenant_id, "recall",
        f"Nudged {to} that their {service} is due (recall {due})",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=b["id"], booking_ref=b.get("booking_ref"), event_date=due)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="recall", email_log_id=log_id)
    if auto:
        if dest_sms:
            await sms.send_sms(tenant_id, dest_sms,
                                   disclosure.wrap(cfg, result.text, channel="sms"),
                                   email_log_id=log_id)
        else:
            await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent", f"Sent the recall to {dest_sms or to} (auto)",
                           direction="outbound", contact=dest_sms or to, subject=subject,
                           email_log_id=log_id, booking_id=b["id"], detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    cutoff = (now + timedelta(days=RECALL_LEAD_DAYS)).date().isoformat()
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("practice",)):
        appts = db.list_appointments_due_recall(tenant_id, cutoff)
        if not appts:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        for b in appts:
            try:
                outcome = await _remind_one(tenant_id, cfg, auto, b)
            except Exception:
                # One bad appointment (compose error, send failure) must not stop the run.
                # If it was queued it stays pending for a human; if not it retries next run
                # (the mark happens only after the row is created).
                log.warning("recall failed for booking %s (tenant %s)",
                            b.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"cutoff": cutoff, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
