"""Practice OS no-show scoring sweep (in-process hourly) — docs/os/practice.md §6.3.

For each practice tenant: find CONFIRMED appointments in the next couple of days whose
score is stale (list_appointments_to_score), assemble the §12.1 behavioural features from
db.contact_no_show_history + the booking row, compute the deterministic heuristic prior,
refine it with the metered Haiku scorer, and save the score/band/reason.

Then — the wedge's action — for a `high`-band appointment that hasn't already had its extra
touch (extra_reminder_queued_at null) and starts inside the extra-reminder window (≈3–6h
ahead), compose ONE additional warm reminder in the practice's voice and queue it (pending
draft, or auto-sent for full-auto tenants), then stamp extra_reminder_queued_at so a re-run
never double-nudges. The reminder facts are NEUTRAL — a friendly extra confirmation — never
"we think you'll no-show" (§12.1).

Every appointment is isolated in try/except so one failure never breaks the sweep (the
house rule in every job). The extra reminder reuses the send_recall/_remind_one skeleton.
Service role.

    python -m app.jobs.score_no_shows
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, db, disclosure, mailbox, practice_noshow, sms, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# The extra high-risk reminder fires only when the appointment starts inside this window
# ahead of now — close enough to matter, far enough that a reply can still save the slot.
EXTRA_WINDOW_MIN_HOURS = 3
EXTRA_WINDOW_MAX_HOURS = 6

EXTRA_REMINDER_PURPOSE = (
    "A friendly EXTRA confirmation for an appointment coming up in a few hours. Warmly ask "
    "the patient to reply to confirm they'll be there, or to let the practice know if they "
    "need to move it. Keep it short, personal and in the practice's voice. Do NOT say or "
    "imply anything about no-shows, risk or missing appointments — this is a helpful nudge, "
    "nothing more.")

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _appt_start(b: dict):
    """The appointment's start datetime in DIARY_TZ from booking_date + a slot-mode
    'HH:MM' window, or None when the window isn't a parseable time (day/ampm mode — those
    tenants don't run the extra-reminder window). Practice tenants run windows='slots'."""
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


def _service_paid(b: dict) -> bool | None:
    """Whether the appointment's service carries a price (a free service no-shows more).
    Reads the booking's own price/amount when present; None = unknown."""
    for k in ("price", "amount", "price_gbp"):
        v = b.get(k)
        if v is not None:
            try:
                return float(v) > 0
            except (TypeError, ValueError):
                pass
    return None


def build_features(b: dict, hist: dict, now: datetime) -> dict:
    """Assemble the §12.1 feature dict for one appointment from its booking row + the
    contact's no-show history. Behavioural metadata ONLY — no clinical detail."""
    start = _appt_start(b)
    lead_days = None
    weekday = hour = None
    weekday_label = time_label = None
    if start is not None:
        lead_days = max(0, (start.date() - now.date()).days)
        weekday = start.weekday()
        hour = start.hour
        weekday_label = _WEEKDAYS[weekday]
        time_label = start.strftime("%H:%M")
    total = int(hist.get("total_appts") or 0)
    return {
        "total_appts": total,
        "no_shows": int(hist.get("no_shows") or 0),
        "last_no_show_at": hist.get("last_no_show_at"),
        "has_prior_no_show": bool(hist.get("no_shows")),
        "avg_reply_latency_hours": hist.get("avg_reply_latency_hours"),
        "reminders_ignored": bool(hist.get("reminders_ignored")),
        "lead_days": lead_days,
        "weekday": weekday, "hour": hour,
        "weekday_label": weekday_label, "time_label": time_label,
        "first_visit": total == 0,
        "rescheduled": int(b.get("reschedule_count") or 0),
        "service_name": (b.get("summary") or "appointment").strip(),
        "service_paid": _service_paid(b),
        # The client's own "I'm coming": a reply-C by SMS today (or a confirm-link
        # click once practice §11 v1.5 ships) — docs/modules/module-sms-channel.md §6.
        "confirmed_click": bool(b.get("client_confirmed_at")),
    }


def _in_extra_window(start, now) -> bool:
    if start is None:
        return False
    lo = now + timedelta(hours=EXTRA_WINDOW_MIN_HOURS)
    hi = now + timedelta(hours=EXTRA_WINDOW_MAX_HOURS)
    return lo <= start <= hi


async def queue_extra_reminder(tenant_id: str, cfg: dict, auto: bool, b: dict) -> str:
    """Compose + queue (and maybe send) ONE extra high-risk reminder — the send_recall/
    _remind_one skeleton with neutral, warm facts. Stamps extra_reminder_queued_at for
    dedupe. Returns 'sent' | 'drafted'."""
    to = b["contact"]
    # Channel routing (docs/modules/module-sms-channel.md §6): the extra nudge lands where
    # the patient actually looks — as a text for SMS-preferring patients, with the
    # reply-C ask that flips client_confirmed_at and calms the scorer.
    dest_sms = sms.preferred_sms_destination(tenant_id, to)
    service = (b.get("summary") or "appointment").strip()
    subject = f"Quick confirmation for your {service}"
    when = ""
    start = _appt_start(b)
    if start is not None:
        when = f" at {start.strftime('%H:%M')}"
    facts = (f"The patient has an appointment for their {service} coming up in a few hours"
             f"{when}. A friendly extra confirmation — ask them to reply to confirm they'll "
             "be there, or to let us know if they need to move it.")
    if dest_sms:
        facts += " Ask them to reply C to confirm."
    result = await agent.compose_outbound(cfg, EXTRA_REMINDER_PURPOSE, facts,
                                          channel="sms" if dest_sms else "email",
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, dest_sms or to, subject, f"[extra reminder] {facts}", result.text,
        status="pending", kind="reminder",
        thread_id=f"sms:{dest_sms}" if dest_sms else b.get("thread_id"),
        channel="sms" if dest_sms else "email")
    db.mark_extra_reminder_queued(b["id"], tenant_id)   # dedupe even if the send fails below
    db.log_agent_event(
        tenant_id, "reminder",
        f"Queued an extra confirmation to {to} (high no-show risk) for their {service}",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=b["id"], booking_ref=b.get("booking_ref"),
        event_date=str(b.get("booking_date"))[:10])
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
                           f"Sent the extra confirmation to {dest_sms or to} (auto)",
                           direction="outbound", contact=dest_sms or to, subject=subject,
                           email_log_id=log_id, booking_id=b["id"], detail={"via": "auto"})
        return "sent"
    return "drafted"


async def _score_one(tenant_id: str, cfg: dict, auto: bool, b: dict, now: datetime) -> str:
    """Score one appointment, save it, and — if high-band, un-queued and in-window — fire the
    extra reminder. Returns 'scored' or 'scored+extra'."""
    hist = db.contact_no_show_history(tenant_id, b["contact"])
    features = build_features(b, hist, now)
    prior = practice_noshow.heuristic_prior(features)
    scored = await practice_noshow.score_appointment(cfg, features, prior, tenant_id=tenant_id)
    db.save_no_show_score(b["id"], tenant_id, scored["score"], scored["band"], scored["reason"])
    if (scored["band"] == "high" and not b.get("extra_reminder_queued_at")
            and _in_extra_window(_appt_start(b), now)):
        await queue_extra_reminder(tenant_id, cfg, auto, b)
        return "scored+extra"
    return "scored"


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    scored = extra = 0
    for tenant_id in db.tenants_with_modules(("practice",)):
        appts = db.list_appointments_to_score(tenant_id, horizon_days=2)
        if not appts:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        for b in appts:
            try:
                outcome = await _score_one(tenant_id, cfg, auto, b, now)
            except Exception:
                # One bad appointment (LLM error, send failure) must not stop the sweep.
                log.warning("no-show scoring failed for booking %s (tenant %s)",
                            b.get("id"), tenant_id, exc_info=True)
                continue
            scored += 1
            extra += outcome == "scored+extra"
    return {"scored": scored, "extra_reminders": extra}


if __name__ == "__main__":
    print(asyncio.run(run()))
