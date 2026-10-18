"""Per-connection backoff for the background loops (Gmail poll + calendar sync).

A connection whose token refresh or provider call keeps failing must NOT be
retried — and its traceback logged — every sweep. That is precisely what filled
the 512 MB instance and OOM-looped it for days (live finding, June 2026): a
Google refresh token went bad and returned 400 on every 120s/300s sweep, so each
loop raised and logged a full traceback indefinitely. The retained
exception -> traceback -> frame -> httpx-response cycles outran the collector and
RSS climbed to the ceiling.

So a failing connection is recorded ONCE, then skipped with exponential backoff
(15 min, doubling, capped at 6 h). A single success clears it and the connection
resumes immediately. State is per-process and keyed by an arbitrary hashable —
callers namespace by loop (("calendar", cid) / ("gmail", cid)) so a calendar
failure never silences the Gmail poll. It resets on restart, which is correct: a
fresh process simply re-probes every connection once.
"""
from datetime import datetime, timedelta, timezone

_MIN_BACKOFF = timedelta(minutes=15)
_MAX_BACKOFF = timedelta(hours=6)

# key -> (retry_at, current_backoff)
_state: dict = {}


def should_skip(key) -> bool:
    """True while `key` is inside a backoff window — the caller skips its work."""
    entry = _state.get(key)
    return entry is not None and datetime.now(timezone.utc) < entry[0]


def record_failure(key) -> bool:
    """Register a failed sweep and (re)arm the backoff. Returns True only on the
    FIRST failure of a streak, so the caller logs one explanatory line rather
    than one per sweep."""
    now = datetime.now(timezone.utc)
    entry = _state.get(key)
    if entry is None:
        backoff, first = _MIN_BACKOFF, True
    else:
        backoff, first = min(entry[1] * 2, _MAX_BACKOFF), False
    _state[key] = (now + backoff, backoff)
    return first


def record_success(key) -> None:
    """Clear any backoff — the connection is healthy again."""
    _state.pop(key, None)
