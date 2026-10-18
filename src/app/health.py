"""Somebody is told when the product stops working.

Kris, 2026-09-04: "do the health notifications — mailbox disconnect is the big
one."

Until now a dead mailbox was invisible. Three separate paths, all silent:

  * the Gmail poller caught the 400/401, backed off to six-hourly and wrote ONE
    log line (src/app/sync_backoff.py — that backoff exists because retrying and
    tracebacking every sweep is what OOM-looped the box);
  * the calendar sync did the same;
  * the Graph subscription renewer swallowed its exceptions outright, so a
    Microsoft mailbox's webhook simply expired and auto-draft stopped inside
    three days.

In every case BLAM8 stopped doing the one thing a business pays it for — read
the mail and draft the reply — and the business found out when a customer rang
to ask why nobody had answered.

Three ideas hold this up.

**A streak, not a blip.** Providers 500. Tokens hiccup. An alert on the first
failed sweep would train the owner to ignore it, which is worse than no alert.
Nothing is said until a connection has been failing continuously for GRACE
(one hour) — long enough that a transient has resolved itself, short enough
that a working day is not lost.

**The clock lives in the database.** sync_backoff's state is per-process and
resets on restart, and Render redeploys often enough to hide an hour-long
outage forever. It is also where "we already told them" has to live: without
that, every deploy re-buzzes the same dead mailbox.

**Recovery is news too.** Being told your mailbox is dead and never being told
it is fixed leaves you checking. A reconnect sends one quiet notification and
clears the record, so the next failure alerts again from a clean slate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app import db

log = logging.getLogger(__name__)

# How long a connection must be continuously failing before the owner is told.
# One hour of consistent failure is not a provider having a moment.
GRACE = timedelta(hours=1)

# What each failing surface is called when it reaches the owner. The mailbox is
# the one that stops the product; the calendar is a degradation, and says so.
SURFACES = {
    "mailbox": {
        "kind": "mailbox_disconnected",
        "title": "Your mailbox has stopped",
        "body": "%s hasn't been readable for %s — no email is being answered. "
                "Reconnect it to start again.",
        "url": "/app/settings",
    },
    "calendar": {
        "kind": "calendar_sync_failing",
        "title": "Calendar sync has stopped",
        "body": "%s hasn't synced for %s — your diary may show slots that are "
                "busy in your calendar. Reconnect it to fix.",
        "url": "/app/settings",
    },
}

RECOVERED = {
    "mailbox": ("Your mailbox is back", "%s is being read again."),
    "calendar": ("Calendar sync is back", "%s is syncing again."),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _for_how_long(since: datetime, now: datetime) -> str:
    """"2 hours", "3 days" — how long it has been broken, in the words somebody
    would use. Never minutes: nothing reaches here inside the grace period."""
    hours = int((now - since).total_seconds() // 3600)
    if hours >= 48:
        days = hours // 24
        return "%d days" % days
    if hours >= 24:
        return "a day"
    return "an hour" if hours <= 1 else "%d hours" % hours


def record_failure(connection_id: str, surface: str, reason: str) -> None:
    """One failed sweep of one connection. Safe to call every sweep.

    Starts the streak on the first failure and tells the owner once it has
    outlasted GRACE. Best-effort throughout, by the same contract as every
    other side-effect in the background loops: a health write that fails must
    never turn a skipped sweep into a crashed one — the loops it runs inside
    exist precisely to stop one bad connection killing the others.
    """
    try:
        conn = db.get_connection_health(connection_id)
        if not conn:
            return
        now = _now()
        # An existing streak keeps its start — this is the whole point of
        # storing it: the clock measures the outage, not this process's uptime.
        since = _parse(conn.get("sync_failing_since")) or now
        alerted = _parse(conn.get("health_alerted_at"))
        due = alerted is None and now - since >= GRACE
        stamped = now if due else alerted
        db.set_connection_health(
            connection_id, failing_since=since.isoformat(), error=reason,
            alerted_at=stamped.isoformat() if stamped else None)
        # Stamped BEFORE the send, the mark_booking_reminded ordering: a crash
        # here costs one alert, where the other order would buzz the same dead
        # mailbox on every sweep for as long as it stayed dead.
        if due:
            _notify(conn, surface, since, now)
    except Exception:                                         # noqa: BLE001
        log.warning("health: failure bookkeeping failed (connection=%s)",
                    connection_id, exc_info=True)


def record_success(connection_id: str, surface: str) -> None:
    """A sweep that worked. Cheap when nothing was wrong: one narrow read, and
    a write only when there is a streak to clear."""
    try:
        conn = db.get_connection_health(connection_id)
        if not conn or not conn.get("sync_failing_since"):
            return                                    # healthy and stayed healthy
        told = bool(conn.get("health_alerted_at"))
        db.set_connection_health(connection_id, failing_since=None, error=None,
                                 alerted_at=None)
        if told:
            _notify_recovered(conn, surface)
    except Exception:                                         # noqa: BLE001
        log.warning("health: recovery bookkeeping failed (connection=%s)",
                    connection_id, exc_info=True)


def _who(conn: dict) -> str:
    return (conn.get("address") or "").strip() or "Your mailbox"


def visible_problems(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """The failing connections worth putting on a page, newest streak last.

    Filtered by the SAME grace period as the notification, deliberately. The
    page could lead the buzz — you are looking at the app, after all — but two
    thresholds means the strip appears for a blip that never earns a
    notification, and a warning that turns out to be nothing teaches the
    operator to ignore the one that isn't.

    Pure: dashboard.py does the read and hands the rows in.
    """
    now = now or _now()
    out = []
    for row in rows:
        since = _parse(row.get("sync_failing_since"))
        if since is None or now - since < GRACE:
            continue
        out.append({
            "address": _who(row),
            "how_long": _for_how_long(since, now),
            # The mailbox stops the product; the calendar degrades it. Said
            # plainly, because "sync error" means nothing to a stonemason.
            "what": "hasn't been readable",
            "error": row.get("sync_error") or "",
        })
    return out


def _notify(conn: dict, surface: str, since: datetime, now: datetime) -> None:
    from app import push

    spec = SURFACES.get(surface) or SURFACES["mailbox"]
    push.notify(
        conn["tenant_id"], title=spec["title"],
        body=spec["body"] % (_who(conn), _for_how_long(since, now)),
        url=spec["url"],
        # Tagged by CONNECTION, not by incident: a device that somehow gets two
        # of these replaces rather than stacks, and the operator never scrolls
        # a lock screen of the same dead mailbox.
        tag="health-%s-%s" % (surface, conn["id"]),
        kind=spec["kind"])


def _notify_recovered(conn: dict, surface: str) -> None:
    from app import push

    title, body = RECOVERED.get(surface) or RECOVERED["mailbox"]
    push.notify(conn["tenant_id"], title=title, body=body % _who(conn),
                url="/app/", tag="health-ok-%s-%s" % (surface, conn["id"]),
                kind="mailbox_reconnected")
