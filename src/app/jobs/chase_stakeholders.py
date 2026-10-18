"""Multi-stakeholder chase — the Events OS killer feature (docs/os/events.md §6.2, §12).

For each events-module tenant, sweep the PENDING stakeholders on UPCOMING events whose
outstanding item (a `they_owe` crm_promise) is still open, under the chase cap, not muted
and DUE by the escalation cadence — and draft an escalating, voice-matched nudge to each,
so the venue manager stops being the human message-bus between the organisers, the supplier
and the bar. The events twin of src/app/jobs/send_service_reminders.py: same
compose_outbound → insert_email_log → log_agent_event → usage.record_anthropic →
auto-send-or-pending shape, per-party instead of per-recurring-job. Service role.

GUARDRAIL (§14): the chase is DRAFT-FIRST by default even for cfg.auto_send tenants —
over-chasing a supplier you have a relationship with is worse than under-chasing. A chase
is only auto-SENT when the tenant explicitly opts in via cfg.auto_chase_events (default
off); otherwise it lands as a pending draft for the manager to glance at and release. And
after MAX_CHASES quiet chases an item stops chasing and surfaces on the board as "needs a
human" — the same exhaustion contract as probe fields.

    python -m app.jobs.chase_stakeholders
"""
import asyncio
import logging

from app import db, events_os
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)


async def run() -> dict:
    from datetime import datetime
    now = datetime.now(DIARY_TZ)   # tz-aware "now" — the due/today windows key off it
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("events",)):
        try:
            queue = db.crm_stakeholders_to_chase(
                tenant_id, now=now, max_chases=events_os.MAX_CHASES,
                step_days=events_os.CHASE_STEP_DAYS)
        except Exception:
            log.warning("chase sweep query failed for tenant %s", tenant_id, exc_info=True)
            continue
        if not queue:
            continue
        cfg = db.effective_config(tenant_id, None)
        # DRAFT-FIRST guardrail (§14): auto-SEND a chase ONLY on the dedicated opt-in,
        # never on the general cfg.auto_send. Default off → pending drafts.
        auto = bool(cfg.get("auto_chase_events"))
        for s in queue:
            try:
                event = s.get("crm_events") or {}
                deal = event.get("crm_deals") or {}
                outcome = await events_os.compose_chase(
                    tenant_id, cfg, s, event, deal, now=now, auto=auto)
            except Exception:
                # One bad stakeholder (compose error, send failure) must not stop the
                # run. If a row was queued it stays pending for a human; if not, it
                # retries next run (the chase stamp lands only after the row is created).
                log.warning("stakeholder chase failed for %s (tenant %s)",
                            s.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
