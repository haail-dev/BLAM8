"""?src= attribution — which short produced which click, and which signup.

Every short's description links to `blam8.com/?src=yt-<video id>`
(docs/shorts-checklist.md). This is what turns that into a number:

  1. A landing carrying `?src=` bumps `src_hits` for that source and today.
     The visitor has no tenant yet, so `tenant_visits` cannot hold this.
  2. The value is stashed in a cookie for 30 days. If they come back and sign
     up as a NEW tenant, it is written once to `tenants.signup_src`.

Deliberately the same shape as partner referrals (src/app/partners.py): a cookie is
the only thing that survives the SSO round-trip, and first touch wins and is
never rewritten — a second short that gets the same person back does not steal
the credit from the one that actually found them.

Everything here is best-effort. Analytics must never break a page load or a
sign-in, so every call swallows its own exceptions, and the hit counter runs
off-thread: a YouTube click lands on the homepage, which is the one page that
has to be fast.
"""
from __future__ import annotations

import re
import threading
from datetime import date, timedelta

from starlette.responses import Response

from app.db import db

SRC_COOKIE = "bl_src"
_SRC_COOKIE_DAYS = 30
_MAX_LEN = 40

# Sources are ours, not user input, but they arrive over the wire: keep them to
# a slug so nothing odd reaches the table or the operator's screen.
_UNSAFE = re.compile(r"[^a-z0-9_.-]+")


def normalize_src(value: str | None) -> str:
    """'YT-K1 ' -> 'yt-k1'. Empty string for anything that normalises to nothing."""
    slug = _UNSAFE.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:_MAX_LEN]


def set_src_cookie(response: Response, src: str) -> None:
    response.set_cookie(SRC_COOKIE, src[:_MAX_LEN],
                        max_age=60 * 60 * 24 * _SRC_COOKIE_DAYS,
                        httponly=True, secure=True, samesite="lax", path="/")


def clear_src_cookie(response: Response) -> None:
    response.delete_cookie(SRC_COOKIE, path="/")


def record_hit(src: str) -> None:
    """Count one landing. Upsert-with-increment lives in a SQL function
    (`bump_src_hit`) because two landings in the same second would otherwise
    race through a read-then-write and lose a count."""
    try:
        db().rpc("bump_src_hit", {"p_src": src}).execute()
    except Exception:
        pass


def record_hit_async(src: str) -> None:
    """Fire-and-forget: the click must not wait on the analytics write."""
    try:
        threading.Thread(target=record_hit, args=(src,), daemon=True).start()
    except Exception:
        pass


def attach_for_tenant(tenant_id: str, src: str) -> None:
    """First touch wins: only writes while signup_src is still null."""
    try:
        db().table("tenants").update({"signup_src": src[:_MAX_LEN]}) \
            .eq("id", tenant_id).is_("signup_src", "null").execute()
    except Exception:
        pass


def summary(days: int = 30) -> list[dict]:
    """One row per source: clicks in the window, and the tenants it signed up.

    Aggregated here rather than in SQL — PostgREST has no GROUP BY, and at a
    few sources times a few days the table is trivially small.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    hits: dict[str, int] = {}
    last: dict[str, str] = {}
    try:
        rows = db().table("src_hits").select("src,day,hits") \
            .gte("day", since).execute().data or []
        for r in rows:
            s = r["src"]
            hits[s] = hits.get(s, 0) + (r.get("hits") or 0)
            if r["day"] > last.get(s, ""):
                last[s] = r["day"]
    except Exception:
        pass

    signups: dict[str, list[dict]] = {}
    try:
        rows = db().table("tenants").select("id,name,signup_src,created_at") \
            .not_.is_("signup_src", "null").order("created_at", desc=True) \
            .execute().data or []
        for r in rows:
            signups.setdefault(r["signup_src"], []).append(r)
    except Exception:
        pass

    out = []
    for s in sorted(set(hits) | set(signups)):
        got = signups.get(s, [])
        clicks = hits.get(s, 0)
        out.append({"src": s, "clicks": clicks, "signups": len(got),
                    "last_seen": last.get(s, ""), "tenants": got[:5],
                    "rate": (len(got) / clicks * 100) if clicks else None})
    out.sort(key=lambda r: (-r["signups"], -r["clicks"]))
    return out
