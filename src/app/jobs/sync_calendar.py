"""Continuous calendar busy-sync (in-process loop, like the Gmail poller).

Every sweep mirrors each calendar-connected tenant's busy events into
source='calendar' diary blocks (and clears blocks for moved/deleted events),
so a meeting added in Outlook protects the BLAM8 diary within one interval.
CALENDAR_SYNC_SECONDS=0 disables. Started from app.main at startup.
"""
import asyncio
import gc
import logging

from app import calendar_sync

log = logging.getLogger(__name__)


async def sync_forever(interval_seconds: int) -> None:
    log.info("calendar busy-sync started (every %ss)", interval_seconds)
    while True:
        try:
            await calendar_sync.sync_all()
        except Exception:
            log.exception("calendar sync sweep failed; continuing")
        # Like the Gmail poller: reclaim per-sweep cyclic garbage so RSS stays
        # bounded on the 512MB instance (see poll_gmail.poll_forever for the why).
        gc.collect()
        await asyncio.sleep(interval_seconds)
