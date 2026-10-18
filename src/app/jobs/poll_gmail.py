"""Gmail auto-draft poller — the safety net under Gmail push.

Gmail's primary inbound path is now push (users.watch → Pub/Sub →
/webhook/gmail, when GMAIL_PUBSUB_TOPIC is configured), whose handler runs
poll_connection below for the notified mailbox. This loop stays on as the
backstop for anything push misses (a lapsed watch, a dropped notification):
every GMAIL_POLL_SECONDS it asks each connected Gmail mailbox for messages
added to INBOX since the stored history watermark (users.history.list) and
runs them through the normal drafting pipeline. The pipeline is idempotent on
source_message_id and gates full-auto send per tenant, exactly like the
webhook path — so push and poll can never double-draft.

Watermark lifecycle: null = not yet baselined → first sweep stores "now" WITHOUT
drafting (a freshly-connected mailbox's backlog must never be drafted); a 404
from history.list means the watermark is older than Gmail keeps (~a week, e.g.
the service was down) → re-baseline the same way.

Runs in-process from app.main startup; `python -m app.jobs.poll_gmail` does one
manual sweep.
"""
import asyncio
import gc
import logging

import httpx

from app import db, gmail, health, mailbox, pipeline, sync_backoff

log = logging.getLogger("blautom8.gmail_poll")


async def _rebaseline(conn: dict, access: str) -> None:
    hid = await gmail.current_history_id(access)
    if hid:
        db.set_gmail_history_id(conn["id"], hid)


async def poll_connection(conn: dict) -> int:
    """Draft replies for new INBOX mail on one Gmail connection. Returns how many
    messages were handed to the pipeline."""
    access = await mailbox.valid_access_token(conn, "google")
    watermark = conn.get("gmail_history_id")
    if not watermark:
        await _rebaseline(conn, access)   # first sighting: draft nothing older than now
        return 0
    try:
        ids, new_watermark = await gmail.new_inbox_message_ids(access, watermark)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:   # watermark expired — skip the gap, resume from now
            await _rebaseline(conn, access)
            return 0
        raise
    handled = 0
    for mid in ids:
        try:
            # The connection being polled IS the receiving mailbox (enterprise D5).
            if await pipeline.draft_for_message(
                conn["tenant_id"], mid, auto_send_ok=True, mailbox_id=conn["id"]
            ):
                handled += 1
        except Exception:
            # One bad message (e.g. deleted between listing and fetch) must not
            # block the rest — and the unadvanced watermark would retry it anyway.
            log.warning("gmail poll: drafting message %s failed", mid, exc_info=True)
    if new_watermark and new_watermark != str(watermark):
        db.set_gmail_history_id(conn["id"], new_watermark)
    return handled


async def poll_once() -> int:
    """One sweep over every Gmail connection. Never raises — a failing mailbox is
    logged once and skipped (with backoff) so the others still poll, and so a
    permanently-broken grant (e.g. an expired refresh token 400ing every sweep)
    can't traceback-spam the loop into an OOM (see src/app/sync_backoff.py)."""
    total = 0
    for conn in db.list_google_connections():
        cid = conn.get("id")
        key = ("gmail", cid)
        if sync_backoff.should_skip(key):
            continue
        try:
            total += await poll_connection(conn)
            sync_backoff.record_success(key)
            health.record_success(cid, "mailbox")
        except httpx.HTTPStatusError as e:
            # The log line is for us; health.record_failure is for the owner —
            # a grant that stays dead for an hour stops being our problem to
            # notice quietly and becomes theirs to fix (src/app/health.py).
            health.record_failure(
                cid, "mailbox", "%s from %s" % (e.response.status_code,
                                                e.request.url.host))
            if sync_backoff.record_failure(key):
                log.warning(
                    "gmail poll: connection %s failing — %s from %s. The token grant is "
                    "expired or revoked; skipping with backoff. Reconnect the mailbox to "
                    "re-consent. Resumes automatically once fixed.",
                    cid, e.response.status_code, e.request.url.host)
        except Exception as exc:
            health.record_failure(cid, "mailbox", repr(exc)[:200])
            if sync_backoff.record_failure(key):
                log.warning("gmail poll failed for connection %s; skipping with backoff",
                            cid, exc_info=True)
    return total


async def poll_forever(interval_seconds: int) -> None:
    """The in-process loop started by app.main. poll_once already swallows
    per-mailbox errors; this guards the sweep itself so the task never dies."""
    while True:
        try:
            n = await poll_once()
            if n:
                log.info("gmail poll: handled %d new email(s)", n)
        except Exception:
            log.exception("gmail poll sweep failed")
        # Reclaim this sweep's cyclic garbage (httpx request/response cycles +
        # caught-exception tracebacks). Python's automatic cyclic collector ran too
        # rarely on the 512MB instance, so cycles accumulated ~250/sweep → +43MB/h →
        # OOM in ~6.5h (live finding, June 2026). Forcing it per sweep keeps RSS flat.
        gc.collect()
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    print(f"gmail poll: handled {asyncio.run(poll_once())} new email(s)")
