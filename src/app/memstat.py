"""Process memory and in-process store sizes, as numbers only.

The web service ran out of memory on 13 and 14 Oct 2026 and nothing in its logs said which
in-process store, if any, was growing. This module reads the process's resident memory and the
size of each store so the cause is measured before anything is changed. Like startup_timing it is
content-free: every value is an int, a float or None, and no key, tenant or user id, URL, text or
row value ever reaches the read-out. It does no network or database I/O and keeps nothing between
readings.

Off unless MEMSTAT_SECONDS is set (src/app/main.py logs one line per interval); GET /admin/memstat
reads the same numbers on demand.
"""
import asyncio
import gc
import importlib
import json
import logging
import re
import sys
import threading
import time

log = logging.getLogger(__name__)

MIN_SECONDS = 15
# The only database counter names that may be copied into the read-out: an exception class name
# and an outcome, never a message.
_RETRY_KEY = re.compile(r"[A-Za-z0-9_]{1,48}:(?:recovered|failed)")


def resident_mb():
    """VmRSS from /proc/self/status (Linux, so Render); None where there is no such file."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as status:
            for row in status:
                if row.startswith("VmRSS:"):
                    return round(int(row.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _listing(source, lock=None, items=False):
    """A stable list copy of a dict's values (or items) or of a set, taken under the store's own
    lock when it has one. Other threads mutate these stores, so a copy that races a resize is
    tried once more before giving up."""
    def read():
        if isinstance(source, dict):
            return list(source.items() if items else source.values())
        return list(source)
    for attempt in (1, 2):
        try:
            if lock is None:
                return read()
            with lock:
                return read()
        except RuntimeError:
            if attempt == 2:
                raise


# -- one reader per store; each returns a tuple aligned with its keys in _GROUPS -----------------

def _cache():
    from app import navcache
    now = time.monotonic()
    entries = _listing(navcache._store, navcache._lock, items=True)
    return (len(entries),
            sum(1 for _key, (expires, _value) in entries if expires <= now),
            sum(1 for key, _entry in entries if ":data:" in key),
            sum(len(value) for _key, (_expires, value) in entries if isinstance(value, list)),
            len(navcache._inflight))


def _live():
    from app import gptlive
    sessions = _listing(gptlive._sessions)
    return (len(sessions),
            sum(s.transcript_chars for s in sessions),
            sum(s.transcript_fragments for s in sessions),
            sum(len(s.events) for s in sessions),
            sum(len(s.fragments) for s in sessions),
            sum(len(s.history) for s in sessions),
            sum(len(s.seen) for s in sessions),
            sum(len(s.delegations) for s in sessions),
            sum(len(s.commands) for s in sessions),
            sum(s.output_chars for s in sessions),
            len(gptlive._cancelled))


def _spoken():
    from app import concierge_routes
    people = _listing(concierge_routes._SPOKEN)
    return len(people), sum(len(entries) for entries in people)


def _voice_samples():
    from app import concierge_routes
    samples = _listing(concierge_routes._VOICE_SAMPLES)
    return len(samples), sum(len(audio) for audio in samples)


def _rate():
    from app import security
    buckets = _listing(security._BUCKETS)
    return len(buckets), sum(len(hits) for hits in buckets)


def _sse():
    from app import events
    tenants = _listing(events._subscribers)
    queues = [queue for group in tenants for queue in _listing(group)]
    return len(tenants), len(queues), sum(queue.qsize() for queue in queues)


def _drafting():
    from app import events
    return (sum(len(entries) for entries in _listing(events._drafting)),)


def _executors():
    from app import dashboard
    return len(dashboard._FAN._threads), len(dashboard._WARM._threads)


def _database():
    """Sockets in the shared Supabase client's pool. The options hold the same httpx client the
    queries use (tests/test_memstat.py pins that), and reading them builds nothing."""
    from app import db
    client = db._client
    if client is None:
        return (None,)
    return (len(client.options.httpx_client._transport._pool.connections),)


def _collector_counts():
    return gc.get_count()


def _collector_full_runs():
    return (gc.get_stats()[2]["collections"],)


def _collector_garbage():
    return (len(gc.garbage),)


def _allocated_blocks():
    return (sys.getallocatedblocks(),)


def _size(module, name):
    """Reader for a store where only its size matters."""
    def read():
        return (len(getattr(importlib.import_module("app." + module), name)),)
    return read


_SIZES = {
    "voice_turns": ("voice_turn_state", "_states"),
    "journeys": ("journey", "_live"),
    "record_versions": ("record_versions", "_versions"),
    "backoff_entries": ("sync_backoff", "_state"),
    "walks": ("debrief_walk", "_WALKS"),
    "lanes": ("task_priority", "_LANES"),
    "portal_sessions": ("portal", "_SESSIONS"),
    "concierge_running": ("concierge_tasks", "_RUNNING"),
}

_GROUPS = (
    (("gc_gen0", "gc_gen1", "gc_gen2"), _collector_counts),
    (("gc_collections2",), _collector_full_runs),
    (("gc_garbage",), _collector_garbage),
    (("alloc_blocks",), _allocated_blocks),
    (("fan_threads", "warm_threads"), _executors),
    (("db_connections",), _database),
    (("cache_keys", "cache_expired", "cache_data_keys", "cache_rows", "cache_inflight"), _cache),
    (("live_sessions", "live_transcript_chars", "live_transcript_fragments", "live_events",
      "live_fragments", "live_history", "live_seen", "live_delegations", "live_commands",
      "live_output_chars", "live_cancelled"), _live),
    (("spoken_people", "spoken_entries"), _spoken),
    (("voice_samples", "voice_sample_bytes"), _voice_samples),
    (("rate_buckets", "rate_hits"), _rate),
    (("sse_tenants", "sse_queues", "sse_queued"), _sse),
    (("drafting",), _drafting),
) + tuple(((name,), _size(module, attr)) for name, (module, attr) in _SIZES.items())


def _retry_counts():
    """The database layer's retry counters, when this build has them: only names of the fixed
    'ErrorClass:outcome' shape are copied."""
    from app import db
    counts = getattr(db, "_retry_counts", None)
    if not isinstance(counts, dict):
        return {}
    return {"db_retry_" + key: value for key, value in _listing(counts, items=True)
            if isinstance(key, str) and _RETRY_KEY.fullmatch(key)}


def loop_state():
    """What only the event-loop thread can read: the task count and anyio's worker-thread limiter
    (the same pair startup_timing reports). Call it on the loop and pass the result to snapshot()."""
    state = {}
    try:
        state["tasks"] = len(asyncio.all_tasks())
    except Exception:
        pass
    try:
        from anyio.to_thread import current_default_thread_limiter
        limiter = current_default_thread_limiter()
        state["pool"] = (limiter.borrowed_tokens, limiter.total_tokens)
    except Exception:
        pass
    return state


def snapshot(tasks=None, pool=None, collect=False):
    """A flat dict of numbers. A store that cannot be read gives None for its keys and never
    stops the others. With collect=True the cyclic collector runs after the readings (admin
    route only, never the loop), adding how many objects it found and the memory afterwards."""
    values = {"rss_mb": resident_mb(), "threads": threading.active_count(), "tasks": tasks,
              "pool_borrowed": pool[0] if pool else None, "pool_total": pool[1] if pool else None}
    for keys, read in _GROUPS:
        try:
            found = read()
        except Exception:
            found = ()
        values.update(zip(keys, tuple(found) + (None,) * len(keys)))
    try:
        values.update(_retry_counts())
    except Exception:
        pass
    out = {key: value if type(value) in (int, float) else None for key, value in values.items()}
    if collect:
        out["collected"] = gc.collect()
        out["rss_mb_after"] = resident_mb()
    return out


def line(tasks=None, pool=None):
    return "memstat " + json.dumps(snapshot(tasks, pool), separators=(",", ":"))


async def run_forever(seconds):
    """One log line per interval (never under MIN_SECONDS), forever; a failed reading is logged
    and never ends the loop. The reading runs in a thread, off the event loop."""
    seconds = max(MIN_SECONDS, seconds)
    while True:
        try:
            log.info("%s", await asyncio.to_thread(line, **loop_state()))
        except Exception:
            log.warning("memstat failed", exc_info=True)
        await asyncio.sleep(seconds)
