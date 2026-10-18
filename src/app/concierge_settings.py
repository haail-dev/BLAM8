"""Per-business Concierge settings, stored in tenants.concierge_settings (jsonb).

Only what a business chose is stored; anything missing falls back to the
server default in src/app/config.py, so an empty object means "defaults" and a
business never needs a row to behave sensibly. Every value is clamped here, on
the way in AND on the way out, so a hand-edited row cannot hide every task.
"""
from app.config import settings

# key -> (server default, lowest, highest), all in minutes.
LIMITS = {
    "task_nudge_minutes": (lambda: settings.concierge_task_nudge_minutes, 5, 24 * 60),
    "task_expire_minutes": (lambda: settings.concierge_task_expire_minutes, 60, 7 * 24 * 60),
    "task_stall_minutes": (lambda: settings.concierge_task_stall_minutes, 2, 60),
}


def _whole(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _stored(tenant) -> dict:
    stored = tenant.get("concierge_settings") if isinstance(tenant, dict) else None
    return stored if isinstance(stored, dict) else {}


def resolved(tenant: dict | None = None) -> dict:
    """All three timeouts, in minutes, for this business."""
    stored = _stored(tenant)
    out = {}
    for key, (default, low, high) in LIMITS.items():
        value = _whole(stored.get(key))
        out[key] = default() if value is None else max(low, min(high, value))
    if out["task_nudge_minutes"] >= out["task_expire_minutes"]:
        # A nudge after the expiry would never show; fall back to the defaults.
        out["task_nudge_minutes"] = LIMITS["task_nudge_minutes"][0]()
        out["task_expire_minutes"] = LIMITS["task_expire_minutes"][0]()
    return out


def chosen(tenant: dict | None = None) -> dict:
    """What this business set itself (valid, clamped values only), for the form."""
    stored = _stored(tenant)
    out = {}
    for key, (_default, low, high) in LIMITS.items():
        value = _whole(stored.get(key))
        if value is not None:
            out[key] = max(low, min(high, value))
    return out


def parse_form(form) -> dict:
    """The settings the form carried: blank = use the default, so it is dropped."""
    out = {}
    for key, (_default, low, high) in LIMITS.items():
        value = _whole(form.get(key))
        if key == "task_expire_minutes":
            hours = _whole(form.get("task_expire_hours"))   # the page asks in hours
            value = hours * 60 if hours is not None else value
        if value is not None:
            out[key] = max(low, min(high, value))
    return out


def summary(tenant: dict | None = None) -> str:
    return "Custom timings" if _stored(tenant) else "Default timings"
