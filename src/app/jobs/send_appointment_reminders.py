"""Practice OS appointment reminder cycle (in-process, every 30 min) — the 48/24/2h
cadence (docs/os/practice.md §6.2).

The generalisation of the day-before job (src/app/jobs/send_reminders.py, which is effectively
the single '24h' stage): for each lead time in the tenant's reminder_cycle.leads_hours
(default [48, 24, 2]) we find CONFIRMED appointments starting inside that lead's window and
send a short reminder in the practice's voice — a per-stage jsonb dedupe (reminder_stages_
sent) means each appointment gets each stage at most once. The 24h + 2h touches also ask
the patient to reply to confirm when ask_confirm is set (a reply flips "confirmed?" true on
the board). Same compose → log → dedupe → auto-send skeleton as the day-before job,
parameterised by lead time; the office-vs-transport _facts_for split is dropped (practice
appointments are always office rows). Gated to practice-module tenants. Service role.

    python -m app.jobs.send_appointment_reminders
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, practice_config, scheduling, sms, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# The sweep runs this often; each stage's window is [now+lead, now+lead+INTERVAL] so a
# single pass over 30 minutes catches every appointment that crossed a lead boundary since
# the last run. Matches the run_every below (registered in src/app/main.py).
SWEEP_INTERVAL_MINUTES = 30

# Per-stage purpose strings. The base is a plain reminder; the 24h + 2h touches append the
# confirm ask when the tenant's ask_confirm is on (§6.2). Keyed by the stage label ("48h").
_BASE_PURPOSE = ("A friendly reminder that the patient has an appointment coming up. Confirm "
                 "the date and time and keep it short and warm, in the practice's voice.")
_CONFIRM_TAIL = (" Warmly ask the patient to reply to confirm they'll be there, or to let "
                 "the practice know if they need to move it.")

# Which stages carry the confirm ask (the later, closer touches — the 48h heads-up doesn't).
_CONFIRM_STAGES = ("24h", "2h")


def _stage_label(lead_hours: int) -> str:
    """The reminder_stages_sent key for a lead time — e.g. 24 → '24h'. The presence of this
    key in the jsonb is the per-stage dedupe."""
    return f"{int(lead_hours)}h"


def _purpose_for(stage: str, ask_confirm: bool) -> str:
    """The composer purpose for a stage — the base reminder, plus the confirm ask on the
    24h/2h touches when the tenant asks to confirm (§6.2)."""
    if ask_confirm and stage in _CONFIRM_STAGES:
        return _BASE_PURPOSE + _CONFIRM_TAIL
    return _BASE_PURPOSE


def _appt_start(b: dict):
    """The appointment's start datetime in DIARY_TZ from booking_date + a slot-mode 'HH:MM'
    window, or None when the window isn't a parseable time. Practice tenants run
    windows='slots', so the window IS the start time (the twin of score_no_shows._appt_start)."""
    day = str(b.get("booking_date") or "")[:10]
    win = str(b.get("window") or "").strip()
    if not day or ":" not in win:
        return None
    try:
        hh, mm = win.split(":")[:2]
        return datetime(int(day[:4]), int(day[5:7]), int(day[8:10]),
                        int(hh), int(mm), tzinfo=DIARY_TZ)
    except (ValueError, IndexError):
        return None


def _facts(b: dict) -> str:
    """Neutral office facts for the composer, straight off the diary row — the date, the
    time, the service and the booking reference. Never a clinical detail."""
    date = str(b.get("booking_date") or "")[:10]
    parts = [f"The patient has a confirmed appointment on {date}."]
    start = _appt_start(b)
    if start is not None:
        parts.append(f"Time: {start.strftime('%H:%M')}.")
    else:
        win = b.get("window")
        if win and win not in ("day", "am", "pm"):
            parts.append(f"Time: {scheduling.window_label(win)}.")
    service = (b.get("summary") or "").strip()
    if service:
        parts.append(f"Service: {service}.")
    if b.get("practitioner"):
        parts.append(f"With {b['practitioner']}.")
    if b.get("booking_ref"):
        parts.append(f"Booking reference: {b['booking_ref']}.")
    return " ".join(parts)


async def _remind_one(tenant_id: str, cfg: dict, auto: bool, stage: str,
                      ask_confirm: bool, b: dict) -> str:
    """Compose + queue (and maybe send) one reminder for one stage. Returns 'sent' |
    'drafted'. The day-before job's skeleton, parameterised by stage: the per-stage jsonb
    stamp (mark_reminder_stage_sent) dedupes instead of the single reminder_queued_at.

    Channel routing (docs/modules/module-sms-channel.md §6) mirrors send_reminders: an
    SMS-booked contact, or a CRM contact whose preferred channel is SMS, gets the
    reminder as a text in the SMS register; everyone else gets email exactly as
    before. On the confirm-asking touches (24h/2h when ask_confirm) the SMS gains a
    reply-C ask — the concrete text equivalent of the email confirm tail, feeding
    the confirm-by-reply board."""
    to = b["contact"]
    dest_sms = sms.preferred_sms_destination(tenant_id, to)
    ref = b.get("booking_ref")
    service = (b.get("summary") or "appointment").strip()
    subject = f"Reminder: your {service}" + (f" ({ref})" if ref else "")
    facts = _facts(b)
    if dest_sms and ask_confirm and stage in _CONFIRM_STAGES:
        facts += " Ask them to reply C to confirm they'll be there."
    result = await agent.compose_outbound(cfg, _purpose_for(stage, ask_confirm), facts,
                                          channel="sms" if dest_sms else "email",
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, dest_sms or to, subject, f"[{stage} reminder] {facts}", result.text,
        status="pending", kind="reminder",
        thread_id=f"sms:{dest_sms}" if dest_sms else b.get("thread_id"),
        channel="sms" if dest_sms else "email")
    db.mark_reminder_stage_sent(b["id"], stage, tenant_id)   # per-stage dedupe, even if send fails
    db.log_agent_event(
        tenant_id, "reminder",
        f"Reminded {to} ({stage}) about their {service} on {str(b.get('booking_date'))[:10]}",
        direction="outbound", contact=dest_sms or to, subject=subject, email_log_id=log_id,
        booking_id=b["id"], booking_ref=ref, event_date=str(b.get("booking_date"))[:10])
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
                           f"Sent the {stage} reminder to {dest_sms or to} (auto)",
                           direction="outbound", contact=dest_sms or to, subject=subject,
                           email_log_id=log_id, booking_id=b["id"], booking_ref=ref,
                           detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    """Every 30 min: for each practice tenant, for each configured lead time, remind the
    CONFIRMED appointments that just crossed that lead's window and haven't had this stage
    yet. Per-appointment try/except — one bad reminder never stops the sweep."""
    now = datetime.now(DIARY_TZ)
    interval = timedelta(minutes=SWEEP_INTERVAL_MINUTES)
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("practice",)):
        cfg = db.effective_config(tenant_id, None)
        cycle = practice_config.reminder_cycle(db.get_scheduling_config(tenant_id))
        auto = bool(cfg.get("auto_send"))
        for lead in cycle["leads_hours"]:
            stage = _stage_label(lead)
            win_start = now + timedelta(hours=lead)
            win_end = win_start + interval
            appts = db.list_appointments_for_reminder_stage(
                tenant_id, stage, win_start.isoformat(), win_end.isoformat())
            for b in appts:
                start = _appt_start(b)
                # The date query is coarse (day granularity); the precise gate is the
                # appointment's start falling inside this lead's [now+lead, +interval) window.
                if start is None or not (win_start <= start < win_end):
                    continue
                try:
                    outcome = await _remind_one(
                        tenant_id, cfg, auto, stage, cycle["ask_confirm"], b)
                except Exception:
                    log.warning("appointment reminder failed for booking %s stage %s (tenant %s)",
                                b.get("id"), stage, tenant_id, exc_info=True)
                    continue
                sent += outcome == "sent"
                drafted += outcome == "drafted"
    return {"drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
