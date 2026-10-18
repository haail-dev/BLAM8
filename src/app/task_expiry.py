"""Age and staleness of a Concierge task, judged at read time.

A task parked on `waiting` has no natural end, and a `working` task whose worker
died has no watchdog, so the panel polled both for ever. This reads the clock
instead of mutating the row: the stored task is untouched, nothing here can
race the transition RPC, and a late result still lands normally.
"""
from datetime import datetime, timezone

from app import concierge_settings


def _when(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def assess(row: dict, now: datetime | None = None, limits: dict | None = None) -> dict:
    """{"age_seconds": int|None, "stale": None|"nudge"|"expired"|"stalled"}"""
    now = now or datetime.now(timezone.utc)
    limits = limits or concierge_settings.resolved()
    status = row.get("status")
    if status == "working":
        since = _when(row.get("started_at")) or _when(row.get("created_at"))
        limit = limits["task_stall_minutes"]
        flag = "stalled"
    elif status in {"waiting", "needs_you"}:
        since = _when(row.get("finished_at")) or _when(row.get("started_at")) or _when(row.get("created_at"))
        limit = None
        flag = None
    else:
        return {"age_seconds": None, "stale": None}
    if since is None:
        return {"age_seconds": None, "stale": None}
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    age = max(0, int((now - since).total_seconds()))
    if flag:
        return {"age_seconds": age, "stale": flag if limit and age >= limit * 60 else None}
    if age >= limits["task_expire_minutes"] * 60:
        return {"age_seconds": age, "stale": "expired"}
    if age >= limits["task_nudge_minutes"] * 60:
        return {"age_seconds": age, "stale": "nudge"}
    return {"age_seconds": age, "stale": None}
