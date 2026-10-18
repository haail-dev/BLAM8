"""Run-of-show assembly — the daily job (docs/os/events.md §6.3, §14).

For each events-module tenant, walk the events entering the run-of-show window (event_date
within ASSEMBLE_WINDOW_DAYS, tz-aware DIARY_TZ) and DETERMINISTICALLY assemble each one's run
sheet from its stated facts (timings + supplier confirmations + staff call-times + dietary
matrix + contingency notes), flagging gaps by rule — NEVER fabricating a timing (§14). Then,
on FIRST assembly (or when a prior sign-off wasn't yet requested), draft the CLIENT sign-off
request via events_os.compose_signoff — the ONLY LLM surface here, facts-only.

The sign-off is DRAFT-FIRST by default (§14): auto-SENT only on the dedicated
cfg.auto_signoff_events opt-in (never cfg.auto_send). It's DEDUPED via a signoff_requested_at
stamp inside run_of_show (no migration) so it isn't re-drafted every day. Per-event
try/except; service role. The assembly itself has no LLM anywhere and runs regardless.

    python -m app.jobs.assemble_run_sheets
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import db, events_os
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# The run-of-show window: events this many days out (or nearer) get assembled + a sign-off
# drafted. Matches the §6.3 "≈14 days out" horizon.
ASSEMBLE_WINDOW_DAYS = 14


async def run() -> dict:
    now = datetime.now(DIARY_TZ)   # tz-aware "now" — the window keys off it, not naive local
    today = now.date()
    horizon = (today + timedelta(days=ASSEMBLE_WINDOW_DAYS)).isoformat()
    assembled = drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("events",)):
        try:
            events = db.crm_events_between(tenant_id, today.isoformat(), horizon)
        except Exception:
            log.warning("run-sheet window query failed for tenant %s", tenant_id,
                        exc_info=True)
            continue
        if not events:
            continue
        cfg = db.effective_config(tenant_id, None)
        # DRAFT-FIRST guardrail (§14): auto-SEND a sign-off ONLY on the dedicated opt-in,
        # never on the general cfg.auto_send. Default off → pending drafts.
        auto = bool(cfg.get("auto_signoff_events"))
        for event in events:
            try:
                result = events_os.assemble_and_store(tenant_id, event, now=now)
                assembled += 1
            except Exception:
                # One bad event's assembly must never stop the run — every other event's
                # sheet still gets built (the assembly is the deliverable).
                log.warning("run-sheet assembly failed for event %s (tenant %s)",
                            event.get("id"), tenant_id, exc_info=True)
                continue
            # DEDUPE: draft the sign-off only if one hasn't already been requested for this
            # sheet (the stamp is carried forward across re-assembly by assemble_and_store).
            sheet = result["run_of_show"]
            if sheet.get("signoff_requested_at"):
                continue
            deal = event.get("crm_deals") or db.crm_get_deal(event["deal_id"], tenant_id) or {}
            try:
                outcome = await events_os.compose_signoff(
                    tenant_id, cfg, event, deal, sheet, now=now, auto=auto)
            except Exception:
                log.warning("run-sheet sign-off draft failed for event %s (tenant %s)",
                            event.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"assembled": assembled, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
