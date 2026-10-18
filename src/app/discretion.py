"""Discretion mode — is it on for this tenant, and nothing else.

The mask itself lives in `src/app/static/discretion.js`; this only answers the
question of whether to send that file, and caches the answer so the check costs
nothing on the pages it is off for (which is all of them, for every real
tenant).

Default off. A tenant without it never receives the script and there is no
trace of the mode in their HTML — no tag, no setting, no mention.
"""
from __future__ import annotations

import time

from app.db import db

_TTL = 60.0                       # seconds; a flip shows up within a minute
_cache: dict[str, tuple[bool, float]] = {}


def enabled(tenant_id: str | None) -> bool:
    if not tenant_id:
        return False
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit and hit[1] > now:
        return hit[0]
    value = False
    try:
        r = (db().table("tenant_config").select("discretion")
             .eq("tenant_id", tenant_id).limit(1).execute())
        value = bool((r.data or [{}])[0].get("discretion"))
    except Exception:
        # A filming convenience must never take a page down. Off is the safe
        # answer for correctness (real data shows) but the RISKY one for
        # filming — so if this ever starts failing, the OCR gate in
        # tools/video/redact_check.py is what catches it before publish.
        value = False
    _cache[tenant_id] = (value, now + _TTL)
    return value


def forget(tenant_id: str | None = None) -> None:
    """Drop the cache — used by the flip script so a change is instant."""
    if tenant_id:
        _cache.pop(tenant_id, None)
    else:
        _cache.clear()
