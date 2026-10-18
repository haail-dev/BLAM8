"""Morning debrief sweep (docs/modules/module-morning-debrief.md).

The platform's first per-tenant-TIMED job: not another hardcoded run_daily UTC
constant, but a 15-min interval sweep (the send_appointment_reminders pattern)
that fires each tenant's brief at their own debrief_time, in diary time
(Europe/London — scheduling.DIARY_TZ, D2).

Per due tenant, in order:
  1. stamp debrief_last_sent_on FIRST (a crash costs one brief, never a
     double-push — the mark_booking_reminded ordering);
  2. closed-day gate (D6): unless debrief_on_closed_days, a day outside the
     scheduling config's working_days (or in blackouts) sends nothing — the
     stamp already landed, so a closed-day tenant isn't re-read all day;
  3. gather + compose (zero-LLM, src/app/debrief.py) — an empty day composes to
     None and sends nothing;
  4. one agent-log row + ONE push.notify call (which also writes the concierge
     feed row and fires the live-tab glow), tagged debrief-{date} so a
     redelivery replaces rather than stacks — then the same brief lands as an
     assistant turn in the concierge chat (Phase 2, D5) with section
     deep-links, so the morning conversation starts already-briefed;
  5. and only THEN the held notification queue is stamped briefed — steps 2
     and 3 both leave it standing, which is what carries a Friday-evening
     held item across a closed weekend to Monday's brief.

Late-start catch-up is inherent: a redeploy over 07:30 delivers on the next
pass. Registered in src/app/main.py via run_every(900).
Manual run: python -m app.jobs.morning_debrief
"""
import logging
from datetime import datetime

from app import db, debrief, push
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)


def send_one(tenant_id: str, today, now_local: datetime, *,
             replay_since: str | None = None, test: bool = False) -> dict | None:
    """Compose and send one tenant's brief: push, feed row, the Concierge's
    opening turn. None when there is nothing to say.

    `test` (/rig/brief): sent now, on demand, and it leaves no trace on the
    real schedule — no activity-log row, and the held queue is NOT stamped,
    so tomorrow's brief still says what it would have."""
    brief = debrief.compose(debrief.gather(tenant_id, today, replay_since=replay_since),
                            today, at=now_local)
    if brief is None:
        return None
    if not test:
        db.log_agent_event(tenant_id, "morning_debrief", brief["summary"])
    push.notify(tenant_id, title=brief["title"], body=brief["body"],
                url="/app/", tag=f"debrief-{today.isoformat()}",
                kind="morning_debrief")
    # Phase 2 (D5): the same brief lands as the concierge's opening
    # turn, links as follow-up affordances — opening the chat in the
    # morning reads as the concierge having greeted you. Best-effort
    # by add_concierge_message's own contract (a storage failure must
    # never cost the push that already went out).
    db.add_concierge_message(tenant_id, "assistant", brief["chat"],
                             links=brief["links"])
    # The held queue closes only once a brief has actually gone out
    # (Kris 2026-09-04). A closed day, a quiet day or a crash above all
    # leave it standing, so what was held for the morning survives to
    # the morning it is mentioned. Last, and best-effort: a stamp that
    # fails costs one repeated mention tomorrow, never the brief.
    if not test:
        try:
            db.mark_notifications_briefed(tenant_id)
        except Exception:
            log.warning("debrief: held-queue stamp failed (tenant=%s)",
                        tenant_id, exc_info=True)
    return brief


async def run() -> dict:
    now_local = datetime.now(DIARY_TZ)
    today = now_local.date()
    today_iso = today.isoformat()
    hhmm = now_local.strftime("%H:%M")

    try:
        due = db.list_debrief_due_tenants(today_iso, hhmm)
    except Exception:
        log.warning("debrief due-read failed", exc_info=True)
        return {"due": 0, "error": "due-read failed"}

    sent = closed = quiet = errors = 0
    for row in due:
        tenant_id = row.get("tenant_id")
        try:
            db.stamp_debrief_sent(tenant_id, today_iso)   # stamp-first, always
            if not row.get("debrief_on_closed_days") and \
                    not debrief.open_today(tenant_id, today):
                closed += 1
                continue
            brief = send_one(tenant_id, today, now_local)
            if brief is None:
                quiet += 1
                continue
            sent += 1
        except Exception:
            # One bad tenant must not stop the sweep.
            errors += 1
            log.warning("debrief failed for tenant %s", tenant_id, exc_info=True)
    return {"due": len(due), "sent": sent, "closed_day": closed,
            "quiet": quiet, "errors": errors}


if __name__ == "__main__":
    import asyncio
    print(asyncio.run(run()))
