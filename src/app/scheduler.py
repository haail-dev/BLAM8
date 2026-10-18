"""In-process daily job runner.

Keeps idempotent maintenance jobs (day-before reminders, retention purge) INSIDE the
always-on web service instead of separate Render cron services — which avoids the
per-deploy rebuild of each cron and the per-service overhead. This mirrors the existing
in-process loops (gmail poller, calendar sync, subscription renewer in src/app/main.py).

Safe because the web service is always-on + single-instance, AND the jobs are idempotent
(reminders dedupe via bookings.reminder_queued_at; purge re-redacts to the same value), so
a rare double-run is a no-op and a run missed across a redeploy is recoverable.
"""
import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def seconds_until(hour: int, minute: int, now: datetime) -> float:
    """Seconds from `now` (tz-aware) to the next HH:MM in UTC (today if still ahead,
    else tomorrow)."""
    now = now.astimezone(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily(hour: int, minute: int, job, name: str) -> None:
    """Forever: sleep to the next HH:MM UTC, run `job` (async awaited; sync run in a
    thread so it never blocks the event loop), log the outcome, repeat. A job failure is
    logged and never breaks the loop."""
    delay = seconds_until(hour, minute, datetime.now(timezone.utc))
    log.info("daily job %s scheduled for %02d:%02d UTC (first run in %.0f min)",
             name, hour, minute, delay / 60)
    while True:
        await asyncio.sleep(delay)
        try:
            result = await (job() if inspect.iscoroutinefunction(job) else asyncio.to_thread(job))
            log.info("daily job %s ran: %s", name, result)
        except Exception:
            log.warning("daily job %s failed", name, exc_info=True)
        delay = seconds_until(hour, minute, datetime.now(timezone.utc))


async def run_every(interval_seconds: int, job, name: str) -> None:
    """Forever: run `job` (async awaited; sync run in a thread so it never blocks the
    event loop), log the outcome, sleep `interval_seconds`, repeat. Same shape as
    run_daily but on a fixed interval — the CRM sequence sweep runs every 15 min. A job
    failure is logged and never breaks the loop."""
    log.info("interval job %s scheduled every %.0f min", name, interval_seconds / 60)
    while True:
        try:
            result = await (job() if inspect.iscoroutinefunction(job) else asyncio.to_thread(job))
            log.info("interval job %s ran: %s", name, result)
        except Exception:
            log.warning("interval job %s failed", name, exc_info=True)
        await asyncio.sleep(interval_seconds)
