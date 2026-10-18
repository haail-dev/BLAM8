"""In-process live-inbox event bus — the "instant chat" layer.

Each open dashboard tab holds one SSE connection (/app/stream, src/app/sse.py); the
drafting pipeline publishes tiny events (drafting / draft_ready / draft_sent /
status_changed) the moment they happen, and the browser refreshes the affected
fragment. In-process by design: the web service is single-instance and always-on
— the same assumption every background loop in src/app/main.py already relies on —
so no external queue is needed. State resets on restart, which is fine: clients
reconnect (EventSource auto-retries) and repaint from the DB.

Everything here is best-effort. A slow, full, or absent listener must never
block or break drafting: publish never raises, never blocks, and a full
subscriber queue simply drops the event (the client's next fragment fetch
repaints the true state anyway).

This module also owns the transient "drafting…" registry. An inbound email
exists in NO table between arrival and insert_email_log — the row is only
written once the LLM reply is finished — so the typing indicator's source of
truth lives here, in memory. Page renders read it too (a cold page load
mid-draft still shows the bubble, not just connected SSE clients). Entries
expire defensively so a crash mid-draft can't pin a forever-"drafting" bubble.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from app import navcache

# A dashboard tab that stops reading (laptop lid closed) must not buffer forever;
# 50 events is far beyond what a repaint needs — beyond it, drop and move on.
_QUEUE_MAX = 50
# A draft that "never finished" (process crash between begin and the finally)
# stops showing as drafting after this long. Real drafts take seconds.
_DRAFTING_TTL = timedelta(minutes=5)

# tenant_id -> set of subscriber queues (one per open SSE connection)
_subscribers: dict[str, set[asyncio.Queue]] = {}
# tenant_id -> {key: info} of drafts currently in-flight (key = source message id)
_drafting: dict[str, dict[str, dict]] = {}


def publish(tenant_id: str, event: dict) -> None:
    """Fan an event out to every listener of this tenant. Never blocks, never
    raises — drafting must not care whether anyone is watching."""
    # An event usually means the inbox moved — bust the cached nav draft count
    # so the badge fetch the tabs make next reads fresh, not the 20s cache, and
    # with it the two other reads that describe a moved inbox: the mailbox scan
    # (src/app/inbox_labels.py) and the month ledger (the Inbox's history faces).
    # This is what keeps those caches honest: they hold only for as long as
    # nothing has happened.
    for k in ("drafts", "inbox", "monthstats"):
        navcache.invalidate("t:" + tenant_id + ":" + k)
    # Belt and braces for the page-data caches (conversation list, bookings): the db client already
    # drops them on every write, this also covers an event raised by a path with no write of its own.
    navcache.bump_data(tenant_id)
    for q in _subscribers.get(tenant_id, set()).copy():
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # stalled tab — it repaints from the DB on its next fetch


def attach(tenant_id: str) -> asyncio.Queue:
    """Register a listener queue for this tenant. The SSE stream consumes the
    queue directly (it interleaves heartbeats via wait_for, which an async
    generator cannot survive — a cancelled __anext__ finalizes the generator).
    Callers MUST detach() in a finally."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.setdefault(tenant_id, set()).add(q)
    return q


def detach(tenant_id: str, q: asyncio.Queue) -> None:
    """Unregister a listener queue — the other half of attach()."""
    subs = _subscribers.get(tenant_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            _subscribers.pop(tenant_id, None)


async def subscribe(tenant_id: str) -> AsyncIterator[dict]:
    """Yield this tenant's events until the caller disconnects — the plain
    iteration wrapper over attach/detach (tests, non-heartbeat consumers)."""
    q = attach(tenant_id)
    try:
        while True:
            yield await q.get()
    finally:
        detach(tenant_id, q)


def drafting_step(tenant_id: str, contact: str | None, label: str) -> None:
    """One live line for the open thread's typing indicator — a mechanical step
    of the draft in flight ('Searched your knowledge — 6 matches', 'Checking
    the diary…'). Pure fan-out, deliberately unregistered: a step that fires
    while no tab is watching simply never shows, and the finished draft's
    decision ghost is the durable record (Kris 2026-08-31)."""
    publish(tenant_id, {"type": "drafting_step", "contact": contact, "label": label})


def begin_drafting(tenant_id: str, key: str, info: dict) -> None:
    """Record an in-flight draft (and tell listeners). `info` carries what the
    typing indicator AND the provisional inbound bubble show: from_address /
    from_name / subject / mailbox_id / body (a short preview of the email being
    answered — it exists in no table until the draft lands, so this is the only
    way an open thread can display it during drafting)."""
    entry = dict(info)
    entry["started_at"] = datetime.now(timezone.utc)
    _drafting.setdefault(tenant_id, {})[key] = entry
    publish(tenant_id, {"type": "drafting", "key": key,
                        "contact": info.get("from_address"),
                        "from_name": info.get("from_name"),
                        "subject": info.get("subject"),
                        "body": info.get("body"),
                        "mailbox_id": info.get("mailbox_id")})


def end_drafting(tenant_id: str, key: str) -> None:
    """Clear an in-flight draft (and tell listeners) — fires whether the draft
    landed or died, so the bubble always goes away."""
    entries = _drafting.get(tenant_id)
    if entries is not None:
        entries.pop(key, None)
        if not entries:
            _drafting.pop(tenant_id, None)
    publish(tenant_id, {"type": "drafting_done", "key": key})


def drafting_now(tenant_id: str) -> list[dict]:
    """The tenant's drafts currently being written, oldest first — for template
    renders and the SSE hello event. Prunes anything past the defensive TTL."""
    entries = _drafting.get(tenant_id)
    if not entries:
        return []
    cutoff = datetime.now(timezone.utc) - _DRAFTING_TTL
    stale = [k for k, e in entries.items() if e["started_at"] < cutoff]
    for k in stale:
        entries.pop(k, None)
    if not entries:
        _drafting.pop(tenant_id, None)
        return []
    out = []
    for key, e in sorted(entries.items(), key=lambda kv: kv[1]["started_at"]):
        out.append({"key": key, "from_address": e.get("from_address"),
                    "from_name": e.get("from_name"), "subject": e.get("subject"),
                    "body": e.get("body"), "mailbox_id": e.get("mailbox_id")})
    return out
