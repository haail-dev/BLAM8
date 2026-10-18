"""UK bank holidays, per nation (docs/plan-service-urgency.md, D1 stage 3).

The gov.uk feed (https://www.gov.uk/bank-holidays.json) has three divisions —
england-and-wales, scotland, northern-ireland — each a list of {title, date,
notes, bunting}, substitute days already on the day they're taken, and one-off
days added when they happen (Scotland, 15 June 2026). So it is FETCHED, daily,
into the `bank_holidays` table, never hard-coded; reads come from a small
in-process cache of that table.

A business picks its nation in Diary settings (scheduling_config
.bank_holiday_nation). Unset — every business before this — nothing changes.
Set, its bank holidays are closed days for normal work (scheduling._norm adds
them to the blackouts, minus any the owner works: bank_holidays_open), and an
out-of-hours service that covers bank holidays is open on them at its price.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import httpx

from app import db

log = logging.getLogger(__name__)

FEED = "https://www.gov.uk/bank-holidays.json"
NATIONS = {"england-and-wales": "England & Wales", "scotland": "Scotland",
           "northern-ireland": "Northern Ireland"}
_TTL_S = 3600.0
_cache: dict[str, tuple[float, dict[date, str]]] = {}


def clean_nation(value) -> str | None:
    v = str(value or "").strip().lower().replace(" ", "-").replace("&", "and")
    aliases = {"england": "england-and-wales", "wales": "england-and-wales",
               "england-and-wales": "england-and-wales", "scotland": "scotland",
               "northern-ireland": "northern-ireland", "ni": "northern-ireland"}
    return aliases.get(v)


def days_for(nation: str | None) -> dict[date, str]:
    """{date: title} for a nation, from the table (cached an hour). {} when the
    nation is unset/unknown or the table can't be read — a read failure never
    closes a day that should be open."""
    n = clean_nation(nation)
    if not n:
        return {}
    hit = _cache.get(n)
    if hit and time.monotonic() - hit[0] < _TTL_S:
        return hit[1]
    try:
        rows = db.db().table("bank_holidays").select("day,title").eq("nation", n).execute().data or []
        days = {date.fromisoformat(str(r["day"])[:10]): str(r.get("title") or "Bank holiday")
                for r in rows}
    except Exception:
        log.warning("bank holidays read failed (nation=%s)", n, exc_info=True)
        days = hit[1] if hit else {}
    _cache[n] = (time.monotonic(), days)
    return days


def title(nation: str | None, day: date) -> str | None:
    """The bank holiday's name on `day` for this nation, else None."""
    return days_for(nation).get(day)


def parse_feed(data: dict) -> list[dict]:
    """The gov.uk JSON → rows {nation, day, title} for the three divisions."""
    out = []
    for nation in NATIONS:
        for ev in ((data or {}).get(nation) or {}).get("events") or []:
            try:
                d = date.fromisoformat(str(ev.get("date"))[:10])
            except (TypeError, ValueError):
                continue
            out.append({"nation": nation, "day": d.isoformat(),
                        "title": str(ev.get("title") or "Bank holiday")[:120]})
    return out


async def refresh() -> int:
    """Fetch the feed and upsert it (the daily job). Returns rows written; 0
    and a warning when gov.uk can't be reached — yesterday's list stands."""
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(FEED, headers={"Accept": "application/json"})
            resp.raise_for_status()
            rows = parse_feed(resp.json())
    except Exception:
        log.warning("bank holidays fetch failed — keeping the stored list", exc_info=True)
        return 0
    if not rows:
        return 0
    db.db().table("bank_holidays").upsert(rows, on_conflict="nation,day").execute()
    _cache.clear()
    return len(rows)
