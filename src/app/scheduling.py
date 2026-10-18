"""Scheduling module — the capacity diary (docs/modules/module-scheduling.md).

The original D1 diary booked one day or half-day window. It now also supports
exact-time appointments, multi-day stays and recurring bookings on the shared
15-minute grid (src/app/slotgrid.py). Tenant settings choose the capacity, working
hours, notice, horizon and blackout dates; this module assembles and enforces them.

The fleet module (docs/modules/module-fleet-capacity.md) lives in its own section of
this file — deliberately inside the scheduling engine, not a parallel stack. It grows the
capacity model a dimension: demand has a size (load-unit spaces, optionally weight)
and supply is a pool of fleet resources, each with its own size. When a tenant's
'tms' subscription is active it SUPERSEDES windows-mode capacity: the same
working-days/notice/horizon/blackout config shapes the diary, but capacity
comes from the fleet resource pool and every booking is whole-day, sized in spaces,
and placed on one fleet resource (best-fit, atomically, in the hold_fleet_booking
RPC). Windows-mode tenants see byte-identical tools and prompts.

Layering:
  - availability() / hold_error() are PURE: config + active bookings in, answers
    out. Grid arithmetic lives in src/app/slotgrid.py; these rules are tested without a DB.
  - check_availability() / place_hold() are the thin tool-facing wrappers the
    agent's executor (and later the Voice module) call; they fetch via app.db.
  - Capacity is enforced in the DB (hold_booking_if_capacity RPC, atomic), not
    here: place_hold pre-validates dates and maps an empty RPC result to 'full'.
    Two racing emails cannot overbook; this layer can only be too cautious.

Window-mode time semantics (deliberately conservative):
  - All diary math runs in Europe/London (DIARY_TZ) — the vertical is UK.
  - The bookable range is [now + min_notice_hours, today + horizon_days].
  - A window is bookable only if its START is at/after the notice cutoff:
    'day' and 'am' start 00:00, 'pm' starts 12:00. A window that has already
    begun is never offered; later windows or exact-time slots may still qualify.
  - Capacity overlap across modes: a 'day' booking blocks both halves; in 'day'
    mode every active booking on the date counts, whatever its window. Honest
    under config changes ('ampm' tenant switching to 'day' and back).
  - Only 'offered', 'proposed' and 'confirmed' bookings consume capacity (holds
    count — D4; declined/cancelled/completed do not). 'offered' is the soft hold
    on every slot the agent puts in front of a customer as an option
    (docs/modules/module-smart-booking-offers.md): exclusive while it lives, auto-released
    at its expires_at deadline — which the reply states to the customer.
"""
import copy
import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone

from app import dates as dates_mod
from app import db, diary_config, fsm_pricing, services, slotgrid, booking_policy

DIARY_TZ = booking_policy.ENGINE_TIMEZONE

# Statuses that occupy a slot. Holds (proposed) count, or racing emails would
# double-book the gap between "drafted" and "approved". Offered slots count too
# (exclusive offers), until their deadline passes — db.list_active_bookings
# filters expired ones out before they reach any capacity math here.
ACTIVE_STATUSES = ("offered", "proposed", "confirmed")

# The most slots one offer set may reserve (Julius, 2026-07-31: 2 alternatives
# when a named time is full, 3 for a day/time-of-day range — never more).
MAX_OFFER_SLOTS = 3

BOOKING_HANDOFF_TOOL = {
    "name": "handoff_booking_to_owner",
    "description": "Booking is unavailable. Hand the request to the owner; reserve nothing.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

DEFAULT_CONFIG = {
    "windows": "day",                        # 'day' | 'ampm' | 'slots'
    "min_notice_hours": 24,
    "horizon_days": 28,
    "blackout_dates": [],
    "slot_minutes": 60,                      # slots mode: appointment length
    "buffer_minutes": 0,                     # gap between bookings (travel/turnaround); 0 = off
    "offer_ttl_hours": 24,                   # how long offered slots stay reserved
    "offer_nudge_enabled": True,             # pre-expiry "still want it?" chase (Phase 3)
}

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ── parsing helpers ──────────────────────────────────────────────────────────
def _as_date(value) -> date:
    """Accept a date or an ISO 'YYYY-MM-DD' string (tool args arrive as strings)."""
    return value if isinstance(value, date) else date.fromisoformat(str(value).strip())


def _as_time(value) -> time:
    """Accept a time, 'HH:MM' (forms) or 'HH:MM:SS' (PostgREST time columns)."""
    return value if isinstance(value, time) else time.fromisoformat(str(value).strip())


def _merged(cfg: dict | None) -> dict:
    return {**DEFAULT_CONFIG, **{k: None for keys in booking_policy.FIELDS.values() for k in keys},
            **{k: v for k, v in (cfg or {}).items() if v is not None}}


def _slot_windows(merged: dict) -> tuple:
    """Generate windows from explicit hours; unset/invalid hours create no windows."""
    try:
        o, c = _as_time(merged["open_time"]), _as_time(merged["close_time"])
        step = max(10, min(480, int(merged["slot_minutes"] or 60)))
    except (TypeError, ValueError):
        return ()
    if o >= c:
        return ()
    t, end, slots = o.hour * 60 + o.minute, c.hour * 60 + c.minute, []
    while t + step <= end and len(slots) < 96:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += step
    return tuple(slots) or ("day",)


def _norm(cfg: dict | None) -> dict:
    """Merge a scheduling_config row (or None) over the defaults and parse the
    text/array columns into working sets. Tolerant of case/spacing in
    working_days and of PostgREST returning date[] as ISO strings."""
    merged = _merged(cfg)
    days = {
        _WEEKDAYS[token]
        for token in str(merged["working_days"]).lower().replace(" ", "").split(",")
        if token in _WEEKDAYS
    }
    if merged["windows"] == "slots":
        windows = _slot_windows(merged)
    elif merged["windows"] == "ampm":
        windows = ("am", "pm")
    else:
        windows = ("day",)
    blackouts = {_as_date(d) for d in merged["blackout_dates"] or []}
    nation = (cfg or {}).get("bank_holiday_nation")
    if nation:
        # A business that picked its nation (D1 stage 3): its bank holidays are
        # closed days for normal work, minus any it has said it works.
        from app import bank_holidays
        worked = {_as_date(d) for d in (cfg or {}).get("bank_holidays_open") or []}
        blackouts |= set(bank_holidays.days_for(nation)) - worked
    return {
        "capacity": int(merged["capacity_per_window"] or 0),
        "mode": merged["windows"],
        "windows": windows,
        "working_days": days,
        "min_notice_hours": int(merged["min_notice_hours"]),
        "horizon_days": int(merged["horizon_days"]),
        "blackouts": blackouts,
        "slot_minutes": max(10, min(480, int(merged["slot_minutes"] or 60))),
    }



# WHY a range has no times (Kris 2026-09-25, services film: a same-day
# emergency at 17:58 read "Checked the diary — no space" in the trail when
# the real reason was the four hours' notice). One reason per requested day,
# in the order that explains it: shut, the day's hours already over, inside
# the notice period, and only then full.
CLOSED, OVER, NOTICE, FULL = "closed", "over", "notice", "full"
AHEAD = "ahead"      # past how far ahead the diary books (live 2026-09-26: Boxing Day read "full")

_PLACE_KEY = re.compile(r"\b(address|postcode|post code|location|site)\b", re.IGNORECASE)


def _place_of(details) -> str:
    """Where a visit is, from its address-shaped details, normalised — two
    holds at different addresses are two visits. '' when none is given."""
    d = details if isinstance(details, dict) else {}
    vals = [str(v) for k, v in d.items() if _PLACE_KEY.search(str(k)) and str(v or "").strip()]
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", " ".join(vals).lower()).split())


def _latest_start(cfg_row, c: dict, d: date, timing: str | None, length_blocks: int | None) -> int:
    """The last block anything can START on day `d`. A morning/afternoon
    diary's last start is the afternoon window's opening (noon), not closing
    time (Kris 2026-09-26: a danger report at 12:08 on a Saturday, 4 h notice —
    cut-off 16:08, closing 18:00 — read "full" because the check compared the
    closing time; the afternoon window had opened at 12:00)."""
    from app import slotgrid
    o, cl = slotgrid.open_close_blocks(cfg_row or {}, d)
    kind = timing or {"ampm": "half", "day": "day"}.get(c["mode"], "time")
    if kind == "half":
        return window_bounds(cfg_row, d, "pm")[0] if cl > NOON_BLOCK else o
    if kind == "day":
        return o
    length = length_blocks or slotgrid.blocks_for_minutes(c["slot_minutes"])
    return max(o, cl - length)


def empty_reasons(cfg_row: dict | None, start: date, end: date,
                  now: datetime | None = None, timing: str | None = None,
                  length_blocks: int | None = None, svc: dict | None = None) -> dict[str, str]:
    """{iso_day: reason} for each day in start..end (at most 62). `timing` is
    the service's ('time' / 'half' / 'day') when the ask names one."""
    from app import slotgrid
    c = _norm(cfg_row)
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    cutoff = now + timedelta(hours=service_notice(c, svc))
    out: dict[str, str] = {}
    for i in range(min((end - start).days + 1, 62)):
        d = start + timedelta(days=i)
        if d > now.date() + timedelta(days=c["horizon_days"]):
            out[d.isoformat()] = AHEAD
            continue
        if d in c["blackouts"] or d.weekday() not in c["working_days"]:
            out[d.isoformat()] = CLOSED
            continue
        try:
            _o, cl = slotgrid.open_close_blocks(cfg_row or {}, d)
            closes = datetime.combine(d, slotgrid.time_at(cl), tzinfo=DIARY_TZ)
            last = datetime.combine(d, slotgrid.time_at(_latest_start(cfg_row, c, d, timing, length_blocks)),
                                    tzinfo=DIARY_TZ)
        except Exception:  # noqa: BLE001 — unreadable hours: say nothing we can't back
            out[d.isoformat()] = FULL
            continue
        if d == now.date() and past_same_day_cutoff(svc, now) and closes > now:
            out[d.isoformat()] = NOTICE          # past the service's same-day cut-off
            continue
        out[d.isoformat()] = OVER if closes <= now else NOTICE if last < cutoff else FULL
    return out


def why_none(cfg_row: dict | None, start: date, end: date, now: datetime | None = None,
             timing: str | None = None, length_blocks: int | None = None,
             svc: dict | None = None) -> dict:
    """What an empty availability answer carries: the per-day reasons and a
    note the drafting model reads before it words the reply."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    reasons = empty_reasons(cfg_row, start, end, now, timing, length_blocks, svc)
    if not reasons:
        return {}
    kinds = set(reasons.values())
    out = {"why_none": reasons[min(reasons)] if len(kinds) == 1 else "mixed", "why_none_days": reasons}
    if AHEAD in kinds:
        out["why_none_ahead"] = (
            f"Days after {now.date() + timedelta(days=_norm(cfg_row)['horizon_days'])} "
            "are beyond how far ahead the diary takes bookings — they are NOT full. Say "
            "it's too far ahead to book yet, and when it opens if they ask; never 'full' or "
            "'fully booked'.")
    if kinds <= {OVER, NOTICE, CLOSED} and kinds & {OVER, NOTICE}:
        # The customer hears the business's reason — it's too late to get to
        # them then — never the internal rule (run log F13: "we've hit our
        # 4-hour notice cut-off" read like a system talking).
        out["why_none_note"] = (
            "Nothing is bookable here because it is TOO SOON, not because the diary is full. "
            "Tell the customer it's too late to get to them "
            + ("today" if min(reasons) == now.date().isoformat() else "then")
            + " and offer the soonest time the diary shows. Never say the diary is full, "
              "fully booked or has no space, and never quote the hours of notice.")
    return out

def _window_start(day: date, window: str) -> datetime:
    """When the window begins, in diary time — the notice cutoff compares here.
    Slot windows carry their real start time; 'day'/'am' start at midnight
    (conservative) and 'pm' at noon."""
    if ":" in window:
        h, m = window.split(":")
        return datetime.combine(day, time(int(h), int(m)), tzinfo=DIARY_TZ)
    return datetime.combine(day, time(12, 0) if window == "pm" else time(0, 0), tzinfo=DIARY_TZ)


def _overlaps(booked_window: str, window: str) -> bool:
    """Does a booking in `booked_window` occupy `window`? 'day' blocks everything;
    'am'/'pm' block their half (slot times before/after 12:00); a slot blocks only
    itself. Keeps counts honest when a tenant switches window modes."""
    if booked_window == window or "day" in (booked_window, window):
        return True
    half = {"am": lambda s: s < "12:00", "pm": lambda s: s >= "12:00"}
    if booked_window in half and ":" in window:
        return half[booked_window](window)
    if window in half and ":" in booked_window:
        return half[window](booked_window)
    return False


def _used(booked: list[dict], day: date, window: str,
          cfg: dict | None = None, slot_minutes: int = 60) -> int:
    """Active bookings occupying (day, window), counted on the unified 15-minute grid
    (slotgrid.window_used). Block overlap reproduces the legacy cross-mode `_overlaps`
    rules EXACTLY for grid-aligned bookings (tests/test_slotgrid_parity.py) and correctly
    counts an off-grid hold a slot-size change left behind.

    PEOPLE rows only: a row carrying `spaces` is a transport booking (the fleet capacity
    model) and a row carrying `engineer_id` is a field-service job (the team member model);
    neither ever consumes an office window — slotgrid excludes them, exactly as before."""
    return slotgrid.window_used(booked, day, window, cfg or {}, slot_minutes)


def _booking_on_day(b: dict, day) -> bool:
    """Does a booking cover `day`? A grid booking (start_block/length_blocks) overlaps the
    day's absolute block range — so a multi-day stay shows on EVERY day it spans, not just
    its arrival date; a legacy row without grid columns falls back to its booking_date."""
    span = slotgrid.stay_span(b)
    if span:
        lo = slotgrid.abs_block(day, 0)
        return span[0] < lo + slotgrid.BLOCKS_PER_DAY and lo < span[0] + span[1]
    return _as_date(b["booking_date"]) == _as_date(day)


# The month cell's bars — one per diary type a day serves (Kris 2026-09-13:
# "split the box in 2/3/4 depending how many diary types get used, each with
# a bar growing upwards until capacity is full, then reaching the top; once
# inside the day, the bookings of all diary types colour coded"). Position →
# colour: the spaces first, then each appointment service. Whole class names
# (the CSS build scans this file): bg-emerald-500 bg-emerald-500/40
# hover:bg-emerald-600 text-emerald-700 bg-blue-500 bg-blue-500/40
# hover:bg-blue-600 text-blue-700 bg-violet-500 bg-violet-500/40
# hover:bg-violet-600 text-violet-700 bg-teal-500 bg-teal-500/40
# hover:bg-teal-600 text-teal-700
DIARY_TYPE_COLOURS = [
    {"bar": "bg-emerald-500", "soft": "bg-emerald-500/40", "chart": "bg-emerald-500 hover:bg-emerald-600",
     "text": "text-emerald-700"},
    {"bar": "bg-blue-500", "soft": "bg-blue-500/40", "chart": "bg-blue-500 hover:bg-blue-600",
     "text": "text-blue-700"},
    {"bar": "bg-violet-500", "soft": "bg-violet-500/40", "chart": "bg-violet-500 hover:bg-violet-600",
     "text": "text-violet-700"},
    {"bar": "bg-teal-500", "soft": "bg-teal-500/40", "chart": "bg-teal-500 hover:bg-teal-600",
     "text": "text-teal-700"},
]


def _plural(name: str) -> str:
    n = str(name or "").strip()
    return n + ("" if n.lower().endswith("s") else "s")


def day_types(cfg: dict | None, todays: list[dict], windows: list[dict], day,
              stay_mode: bool, appt_types) -> list[dict]:
    """The bars of one month cell: the spaces (or the windows) first, then —
    on a stays diary — each appointment service in its own pool. `pct` is
    how full the type is (booked + offered), `soft_pct` the offered share
    of that, `full` the agent's own refusal: nothing bookable left. For an
    appointment service `full` comes from the same slot finder the agent
    books by, so the cell says full on exactly the days it will decline;
    its height is the larger of the pool-minutes used and the share of
    start times gone, so a bar reaches the top exactly when the type is
    full. Pure — config + the day's rows in."""
    from app import diary_config, services as _services
    c = _norm(cfg)
    out = []
    cap_total = sum(int(w["capacity"]) for w in windows)
    used_total = (max((w["used"] for w in windows), default=0) if stay_mode
                  else sum(int(w["used"]) for w in windows))
    cap_here = int(c["capacity"]) if stay_mode else cap_total
    offered = sum(1 for b in todays if b.get("status") == "offered"
                  and not (appt_types and _services.is_appointment_row(b, appt_types)))
    noun = str(((cfg or {}).get("nouns") or {}).get("participant") or "").strip().lower()
    # Offers sit on TOP of what is booked (slotgrid.window_used counts only
    # proposed/confirmed): the bar is booked + offered, the offered share amber.
    booked_pct = 100 * used_total / cap_here if cap_here else 0
    soft = min(100 - booked_pct, 100 * offered / cap_here) if cap_here else 0
    out.append({
        "key": "spaces", "label": (_plural(noun) if noun else "spaces") if stay_mode else "bookings",
        "used": used_total, "capacity": cap_here,
        "pct": min(100, round(booked_pct + soft)),
        "soft_pct": round(soft),
        "full": cap_here > 0 and used_total >= cap_here,
        **DIARY_TYPE_COLOURS[0],
    })
    if not (stay_mode and appt_types):
        return out
    o, cl = slotgrid.open_close_blocks(cfg or {}, day)
    ds = slotgrid.abs_block(day, 0)
    for k, svc in enumerate(_services.clean_service_types(appt_types), start=1):
        pool = diary_config.find_resource((cfg or {}).get("resource_types"), svc.get("resource") or svc["name"])
        count = int(pool["count"]) if pool else 1
        resource = pool["name"] if pool else svc["name"]
        length = max(1, slotgrid.blocks_for_minutes(int(svc.get("minutes") or c["slot_minutes"])))
        rows = [b for b in todays if b.get("status", "confirmed") in ACTIVE_STATUSES
                and _services.is_appointment_row(b, appt_types)
                and str(b.get("resource_type") or "").strip().lower() == resource.lower()]
        spans = _pool_spans(rows, cfg, resource)
        # The service's own rules (docs/plan-diary-multiview.md): a day it isn't
        # booked on is OFF, not free; its start times / hours are the only starts;
        # its max a day is a full day however much of the pool is left.
        r = _services.rules(svc)
        off = r["days"] is not None and day.weekday() not in r["days"]
        starts = [] if off else slotgrid.day_service_starts(cfg or {}, length, day)
        if r["starts"]:
            fixed = {slotgrid.block_at(t) for t in r["starts"]}
            starts = [s for s in starts if s in fixed]
        lo_r = slotgrid.block_at(r["hours_from"]) if r["hours_from"] else None
        hi_r = slotgrid.block_at(r["hours_until"]) if r["hours_until"] else None
        starts = [s for s in starts if (lo_r is None or s >= lo_r) and (hi_r is None or s + length <= hi_r)]
        fits = _service_fits(cfg, spans, length, day, None, count, 1) if starts else []
        fits = [f for f in fits if f[0] in set(starts)]
        desk = desk_allows(cfg, todays, length, day, None, resource)
        if desk is not None:
            fits = [f for f in fits if f[0] in desk]
        blocks_used = sum(max(0, min(s + ln, ds + slotgrid.BLOCKS_PER_DAY) - max(s, ds)) * q
                          for s, ln, q in spans)
        open_blocks = max(1, (cl - o) * count)
        pct = max(blocks_used / open_blocks, (1 - len(fits) / len(starts)) if starts else 0)
        at_max = bool(r["max_per_day"]) and len(rows) >= r["max_per_day"]
        full = (bool(starts) and not fits) or at_max
        if r["max_per_day"]:
            pct = max(pct, len(rows) / r["max_per_day"])
        offered_n = sum(1 for b in rows if b.get("status") == "offered")
        out.append({
            "key": svc["name"], "label": _plural(svc["name"]).lower(),
            "used": len(rows), "capacity": count, "max": r["max_per_day"], "off": off,
            "pct": 100 if full else min(100, round(100 * pct)),
            "soft_pct": min(100, round(100 * pct * offered_n / len(rows))) if rows else 0,
            "full": full,
            **DIARY_TYPE_COLOURS[k % len(DIARY_TYPE_COLOURS)],
        })
    return out


def _day_chart(merged: dict, todays: list[dict], day, capacity: int,
               appts: list[dict] | None = None, cfg: dict | None = None) -> dict:
    """A per-day occupancy chart (the Diary's clickable bar graph): each booking that covers
    `day` becomes a vertical bar over the open→close axis, packed into lanes — each lane is
    one space, and overlapping bookings get different lanes (so N lanes ≈ N spaces in use).
    Bars carry a % top/height and the booking id so the template can draw them and swap a
    details paragraph on click. `appts` (a stays diary's appointment services, each with its
    pool `count` and `colour` — Kris 2026-09-13) get lanes of their own after the spaces,
    their bars in the service's colour."""
    from app import services as _services
    o, c = _as_time(merged["open_time"]), _as_time(merged["close_time"])
    open_min, close_min = o.hour * 60 + o.minute, c.hour * 60 + c.minute
    span_min = max(1, close_min - open_min)
    appt_types = [a["svc"] for a in (appts or [])]
    def _group_of(b) -> int:
        if not appt_types or not _services.is_appointment_row(b, appt_types):
            return 0
        rt = str(b.get("resource_type") or "").strip().lower()
        return next((i for i, a in enumerate(appts, start=1) if a["resource"].lower() == rt), 0)
    on_day = []
    for b in todays:
        s = slotgrid.stay_span(b)
        if not s:  # legacy / no grid columns — derive the bar from the window on its date
            wb = slotgrid.window_to_blocks(b.get("window"), int(merged.get("slot_minutes") or 60), merged)
            if not wb:
                continue
            s = (slotgrid.abs_block(_as_date(b["booking_date"]), wb[0]), wb[1])
        ds = slotgrid.abs_block(day, 0)
        start_min = max(open_min, (max(s[0], ds) - ds) * slotgrid.BLOCK_MIN)
        end_min = min(close_min, (min(s[0] + s[1], ds + slotgrid.BLOCKS_PER_DAY) - ds) * slotgrid.BLOCK_MIN)
        if end_min > start_min:
            on_day.append((start_min, end_min, b, _group_of(b)))
    on_day.sort(key=lambda x: x[0])
    # Lanes are packed per group: the spaces first (as many lanes as the
    # capacity, or more if overbooked), then each appointment pool's lanes.
    groups = [{"label": "spaces", "count": int(capacity), "colour": DIARY_TYPE_COLOURS[0]}]
    groups += [{"label": _plural(a["svc"]["name"]).lower(), "count": int(a["count"]), "colour": a["colour"]}
               for a in (appts or [])]
    lane_ends: list[list[int]] = [[] for _ in groups]
    bars = []
    for start_min, end_min, b, g in on_day:
        lane_end = lane_ends[g]
        lane = next((i for i, le in enumerate(lane_end) if le <= start_min), None)
        if lane is None:
            lane = len(lane_end)
            lane_end.append(end_min)
        else:
            lane_end[lane] = end_min
        bars.append({
            "booking_id": b["id"], "lane": lane, "group": g,
            "cls": groups[g]["colour"]["chart"],
            "top_pct": round((start_min - open_min) / span_min * 100, 2),
            "height_pct": round((end_min - start_min) / span_min * 100, 2),
            "start_label": f"{start_min // 60:02d}:{start_min % 60:02d}",
            "end_label": f"{end_min // 60:02d}:{end_min % 60:02d}",
            "summary": b.get("summary"),
            "status": b.get("status") or "confirmed",
        })
    # Lane numbers run on across the groups so the template draws one row of
    # columns; each group says where its lanes start and how many it has.
    offset = 0
    for gi, grp in enumerate(groups):
        grp["lanes"] = max(grp["count"], len(lane_ends[gi]), 1)
        grp["used"] = len(lane_ends[gi])
        grp["start"] = offset
        offset += grp["lanes"]
    for bar in bars:
        bar["lane"] += groups[bar["group"]]["start"]
    axis = []
    h = (open_min + 59) // 60 * 60
    while h <= close_min:
        axis.append({"label": f"{h // 60:02d}:00", "pct": round((h - open_min) / span_min * 100, 2)})
        h += 60
    return {"lanes": offset, "bars": bars, "axis": axis, "used": groups[0]["used"],
            "groups": groups if len(groups) > 1 else []}


def _kind_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "service"


def diary_kinds(cfg: dict | None) -> list[dict]:
    """The Diary's filter chips (docs/plan-diary-multiview.md stage 3, Kris
    2026-09-23: "the view mode becomes more of a filter"): All, Stays, then each
    appointment service — only on a diary that holds more than one kind. Empty
    otherwise, so a single-kind diary looks exactly as it did. `group` is the
    kind's index in the day chart's lane groups and the cell's bars."""
    from app import intake as _intake, services as _services
    if not _intake.stay_mode(cfg):
        return []
    svcs = _services.clean_service_types((cfg or {}).get("service_types"))
    if not svcs:
        return []
    out = [{"key": "all", "label": "All", "group": None},
           {"key": "stays", "label": "Stays", "group": 0}]
    for k, svc in enumerate(svcs, start=1):
        out.append({"key": _kind_key(svc["name"]), "label": svc["name"], "group": k,
                    "service": svc["name"], "max": _services.rules(svc)["max_per_day"]})
    return out


def booking_kind(b: dict, kinds: list[dict]) -> str:
    """Which filter a booking belongs to: its appointment service, else Stays."""
    rt = str(b.get("resource_type") or "").strip().lower()
    if rt:
        for k in kinds:
            if k.get("service") and k["service"].lower() == rt:
                return k["key"]
    return "stays"


def filter_day(d: dict, kind: dict, kinds: list[dict]) -> dict:
    """One kind's view of a calendar day — the VIEW only, never the data: its
    bookings, its bar, its lanes in the day chart. A copy; the day is untouched."""
    if kind.get("group") is None:
        return d
    g = kind["group"]
    out = dict(d)
    types = d.get("types") or []
    if len(types) > g:
        out["types"] = [types[g]]
        out["all_full"] = bool(types[g].get("full"))
        out["kind_type"] = types[g]
    if d.get("bookings") is not None:
        mine = [b for b in d["bookings"] if booking_kind(b, kinds) == kind["key"]]
        out["bookings"] = mine
        out["offered_count"] = sum(1 for b in mine if b.get("status") == "offered")
        out["booked_count"] = sum(1 for b in mine if b.get("status", "confirmed") in ("proposed", "confirmed"))
    chart = d.get("chart")
    if chart and chart.get("groups") and len(chart["groups"]) > g:
        grp = dict(chart["groups"][g])
        start = grp["start"]
        out["chart"] = {**chart, "lanes": grp["lanes"], "used": grp["used"], "groups": [],
                        "bars": [{**b, "lane": b["lane"] - start} for b in chart["bars"] if b["group"] == g]}
    out["show_kind"] = kind["key"]
    return out


def diary_line(cfg: dict | None) -> str:
    """One honest sentence describing the diary's shape — shown in the UI and
    injected into the agent's scheduling prompt section."""
    policy = booking_policy.compile_policy(cfg)
    if not policy["ready"]:
        return policy["message"]

    merged = _merged(cfg)
    days = ", ".join(t.strip().title() for t in str(merged["working_days"]).split(",") if t.strip())
    cap = int(merged["capacity_per_window"] or 0)
    buf = int(merged.get("buffer_minutes") or 0)
    # The business's own noun, and its number (run #8: "Up to 1 bookings per day"
    # on a classes business whose nouns were session / participant).
    noun = diary_config.nouns_of(cfg)["booking"]
    many = diary_config.plural(noun)
    caps = noun if cap == 1 else many
    # The gap is already enforced in availability (the agent only sees slots that leave it),
    # but state it so the agent can explain the spacing if asked. The buffer covers travel
    # OR turnaround time between bookings, so the wording stays neutral.
    buffer_note = (f" A {buf}-minute gap is left between {many}." if buf > 0 else "")
    if merged["windows"] == "slots":
        o, c = _as_time(merged["open_time"]), _as_time(merged["close_time"])
        line = (f"Open {o.hour:02d}:{o.minute:02d}-{c.hour:02d}:{c.minute:02d}, {many} are "
                f"{int(merged['slot_minutes'])}-minute slots ({cap} per slot); working days {days}.")
    elif merged["windows"] == "ampm":
        line = f"Up to {cap} {caps} per half-day window (AM/PM); working days {days}."
    else:
        # Capacity is CONCURRENCY (_service_fits counts overlapping spans): three
        # staff = three services at the same time, not three a day (run #9,
        # 2026-09-05 — "Up to 3 cleans per day" on a diary that allowed nine).
        line = f"Up to {cap} {caps} at the same time; working days {days}."
    return line + buffer_note


# ── service-duration diary (inbound appointments, per-service length) ─────────
# When a tenant defines service_types (app/services), a booking's length is the service's
# duration, not a fixed slot. A service of N minutes is ceil(N/15) blocks on the grid; the
# agent only sees start times where a span of that length still fits below capacity
# (concurrent occupancy — capacity_per_window parallel resources, the same math stays use).
# The default (no services) path is untouched — availability()/place_hold() above.
def _office_abs_span(b: dict, slot_minutes: int, cfg: dict | None) -> tuple[int, int, int] | None:
    """An active OFFICE booking's absolute (start_block, length_blocks, quantity) span — grid
    columns when present, else the legacy window mapped onto its date (quantity 1). Fleet/
    team member rows (other capacity models) and inactive rows return None. Mirrors
    slotgrid.window_used's accounting so service capacity counts exactly what the diary does."""
    get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
    if get("spaces") is not None or get("engineer_id"):
        return None
    if get("status", "confirmed") not in ACTIVE_STATUSES:
        return None
    span = slotgrid.stay_span_q(b)
    if span:
        return span
    wb = slotgrid.window_to_blocks(get("window"), slot_minutes, cfg or {})
    if wb and get("booking_date"):
        q = get("quantity")
        return slotgrid.abs_block(_as_date(get("booking_date")), wb[0]), wb[1], (int(q) if q else 1)
    return None


def _pool_spans(booked, cfg, resource, exclude_typed: bool = False) -> list[tuple[int, int, int]]:
    """Active office spans (abs_start, length, qty) for one capacity pool: all office rows
    when `resource` is None (the single fungible pool), else only rows whose `resource_type`
    matches (per-type capacity — untyped legacy rows don't bleed across types)."""
    slot_minutes = _norm(cfg)["slot_minutes"]
    spans = []
    for b in booked:
        sp = _office_abs_span(b, slot_minutes, cfg)
        if sp is None:
            continue
        get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
        if resource is not None:
            if (get("resource_type") or None) != resource:
                continue
        elif exclude_typed and (get("resource_type") or None):
            # A stays diary's appointments (Kris 2026-09-13) live in pools of
            # their own: an introductory visit never occupies a stay space.
            continue
        spans.append(sp)
    return spans


# ── The front desk rule (Kris 2026-09-13) ───────────────────────────────────
# One person handles drop-offs, collections and appointments — unless the
# business says otherwise. Off (the default), the two diaries never see each
# other. On: every active stay's arrival and departure instant is `minutes`
# of desk time during which an appointment cannot start or run (the busy
# span carries the whole pool's capacity, so no pool of the service is free),
# and a stay's arrival or departure is refused when it falls inside an
# appointment. Humans adding by hand are not bound (their diary, their call).
def _front_desk_on(cfg: dict | None) -> bool:
    from app import diary_config, intake as _intake, services as _services
    cfg = cfg or {}
    return (bool(diary_config.front_desk_of(cfg)["enabled"])
            and _intake.stay_mode(cfg)
            and bool(_services.clean_service_types(cfg.get("service_types"))))


def _front_desk_blocks(cfg: dict | None) -> int:
    from app import diary_config
    return max(1, slotgrid.blocks_for_minutes(diary_config.front_desk_of(cfg)["minutes"]))


def front_desk_hands(cfg: dict | None) -> int:
    """How many people are on the desk. Filled from People (slice 2) when
    anyone is assigned to it; one otherwise, which is the rule as it
    shipped."""
    from app import diary_config
    return max(1, int(diary_config.front_desk_of(cfg)["hands"]))


def front_desk_spans(cfg: dict | None, booked: list[dict], qty: int = 1) -> list[tuple[int, int, int]]:
    """Busy spans at the desk: (abs_block, gap, qty) at every active stay's
    arrival and departure — one pair of hands each. Empty unless the rule is
    on."""
    if not _front_desk_on(cfg):
        return []
    gap = _front_desk_blocks(cfg)
    slot_minutes = _norm(cfg)["slot_minutes"]
    out = []
    for b in booked:
        get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
        if get("status", "confirmed") not in ACTIVE_STATUSES or get("resource_type"):
            continue                       # stays are the untyped rows; appointments carry their pool
        sp = _office_abs_span(b, slot_minutes, cfg)
        if sp is None:
            continue
        out.append((sp[0], gap, int(qty)))
        out.append((sp[0] + sp[1], gap, int(qty)))
    return out


def desk_allows(cfg: dict | None, booked: list[dict], length: int, day, cutoff,
                resource: str | None) -> set[int] | None:
    """The start blocks the DESK allows for an appointment on `day` — or None
    when the rule is off and the desk constrains nothing.

    The desk is a pool of its own (People, slice 2): every drop-off and
    collection takes one pair of hands for the rule's minutes, and every
    appointment takes one for its length. A start is allowed while hands
    remain. Checked beside the service's own pool, never mixed into it — a
    drop-off must not eat a stay space, and a second person on the desk must
    not silently double a service's own capacity."""
    if not _front_desk_on(cfg) or not resource:
        return None
    hands = front_desk_hands(cfg)
    spans = front_desk_spans(cfg, booked, 1)
    # Appointments already placed take a hand too — whichever service they are.
    from app import services as _services
    appt_types = (cfg or {}).get("service_types")
    slot_minutes = _norm(cfg)["slot_minutes"]
    for b in booked:
        get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
        if get("status", "confirmed") not in ACTIVE_STATUSES:
            continue
        if not _services.is_appointment_row(b, appt_types):
            continue
        sp = _office_abs_span(b, slot_minutes, cfg)
        if sp:
            spans.append((sp[0], sp[1], 1))
    return {sb for sb, _ in _service_fits(cfg, spans, length, _as_date(day), cutoff, hands, 1)}


def front_desk_clash(cfg: dict | None, booked: list[dict], arr: datetime, dep: datetime) -> dict | None:
    """The appointment a stay's arrival or departure would land inside, or None."""
    from app import services as _services
    if not _front_desk_on(cfg):
        return None
    gap = _front_desk_blocks(cfg)
    hands = front_desk_hands(cfg)
    slot_minutes = _norm(cfg)["slot_minutes"]
    appt_types = (cfg or {}).get("service_types")
    for when, t in (("arrival", arr), ("departure", dep)):
        lo = slotgrid.datetime_to_abs(t)
        hi = lo + gap
        busy = []
        for b in booked:
            get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
            if get("status", "confirmed") not in ACTIVE_STATUSES or not _services.is_appointment_row(b, appt_types):
                continue
            sp = _office_abs_span(b, slot_minutes, cfg)
            if sp and sp[0] < hi and lo < sp[0] + sp[1]:
                busy.append((b, sp))
        # Every pair of hands taken, and only then (People, slice 2: a partner
        # on the desk is exactly why this is a count and not a yes/no).
        if len(busy) >= hands and busy:
            b, sp = busy[0]
            get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
            return {"when": when, "at": t.strftime("%H:%M"),
                    "service": str(get("resource_type") or "appointment"),
                    "from": slotgrid.abs_to_datetime(sp[0]).strftime("%H:%M"),
                    "until": slotgrid.abs_to_datetime(sp[0] + sp[1]).strftime("%H:%M")}
    return None


def front_desk_hit(cfg: dict | None, booked: list[dict], day, start_block: int, length: int) -> str | None:
    """The 'HH:MM' of the drop-off or collection an appointment at (day, start_block)
    would run into, or None."""
    lo = slotgrid.abs_block(_as_date(day), int(start_block))
    hi = lo + max(1, int(length))
    for s, ln, _q in front_desk_spans(cfg, booked, 1):
        if s < hi and lo < s + ln:
            return slotgrid.abs_to_datetime(s).strftime("%H:%M")
    return None


def service_notice(c: dict, svc: dict | None) -> int:
    """The notice this service needs: its own when set (an urgent service,
    docs/plan-service-urgency.md), else the diary's."""
    own = services.rules(svc).get("notice_hours")
    return int(own) if own is not None else int(c["min_notice_hours"])


def past_same_day_cutoff(svc: dict | None, now: datetime) -> bool:
    """Asked after an urgent service's same-day cut-off: today is off for it."""
    cut = services.rules(svc).get("same_day_until")
    return bool(cut) and now.astimezone(DIARY_TZ).strftime("%H:%M") >= cut


EVE = "eve"      # the evening arrival window an out-of-hours service adds


def ooh_windows(cfg: dict | None, svc: dict | None, day) -> list[tuple[str, int, int]]:
    """The out-of-hours spans a service opens on `day` (D1 stage 2):
    [(window, lo, hi)] — the evening after closing on a normal working day
    ('eve', close → until); a whole extra day (an extra weekday it covers, or a
    bank holiday when it covers them) as its morning and afternoon, open →
    noon → until-or-close. [] for a service without out of hours."""
    o_cfg = services.out_of_hours(svc)
    if not o_cfg:
        return []
    c = _norm(cfg)
    day = _as_date(day)
    o, cl = slotgrid.open_close_blocks(cfg or {}, day)
    until = slotgrid.block_at(o_cfg["until"]) if o_cfg.get("until") else None
    is_bh = False
    if (cfg or {}).get("bank_holiday_nation"):
        from app import bank_holidays
        worked = {_as_date(d) for d in (cfg or {}).get("bank_holidays_open") or []}
        is_bh = bool(bank_holidays.title(cfg["bank_holiday_nation"], day)) and day not in worked
    extra_day = ((day.weekday() in (o_cfg.get("days") or []) and day.weekday() not in c["working_days"])
                 or (is_bh and o_cfg.get("bank_holidays")))
    own_blackout = day in {_as_date(d) for d in (cfg or {}).get("blackout_dates") or []}
    if own_blackout:
        return []                       # the owner's own closure wins over everything
    if extra_day:
        hi = max(cl, until or cl)
        out = []
        if o < NOON_BLOCK:
            out.append(("am", o, min(NOON_BLOCK, hi)))
        if hi > NOON_BLOCK:
            out.append(("pm", max(o, NOON_BLOCK), hi))
        return out
    if day.weekday() in c["working_days"] and day not in c["blackouts"] and until and until > cl:
        return [(EVE, cl, until)]
    return []


def _ooh_fits(cfg, spans, length: int, day, lo: int, hi: int, cutoff, cap: int,
              q: int) -> list[tuple[int, int]]:
    """(start_block, remaining) inside an out-of-hours span [lo, hi) — the
    diary's own step, the notice cutoff, the capacity pool."""
    step = slotgrid.blocks_for_minutes(slotgrid.normalize_block_minutes(
        (cfg or {}).get("block_minutes", (cfg or {}).get("slot_minutes", 60))))
    fits, sb = [], lo
    while sb + length <= hi:
        start_dt = datetime.combine(day, slotgrid.time_at(sb), tzinfo=DIARY_TZ)
        if cutoff is None or start_dt >= cutoff:
            abs_start = slotgrid.abs_block(day, sb)
            used = slotgrid.max_concurrent(spans, abs_start, abs_start + length)
            if used + q <= cap:
                fits.append((sb, cap - used))
        sb += step
    return fits


def is_ooh_slot(cfg, svc, day, window: str) -> bool:
    """Is (day, window) one of the service's out-of-hours slots?"""
    w = str(window or "")
    if ":" in w:
        try:
            b = slotgrid.block_at(w)
        except (TypeError, ValueError):
            return False
        return any(lo <= b < hi for _, lo, hi in ooh_windows(cfg, svc, day))
    return any(x == w for x, _, _ in ooh_windows(cfg, svc, day))


def _ooh_slots(cfg, svc, spans, length: int, day, cutoff, cap: int, q: int,
               timing: str) -> list[dict]:
    """The out-of-hours slots on `day`, marked with the service's out-of-hours
    price — the same capacity pool (two vans are still two vans)."""
    o_cfg = services.out_of_hours(svc)
    out = []
    for w, lo, hi in ooh_windows(cfg, svc, day):
        fits = _ooh_fits(cfg, spans, length, day, lo, hi, cutoff, cap, q)
        if not fits:
            continue
        mark = {"out_of_hours": True, "price": o_cfg["price"]}
        if o_cfg.get("price_basis"):
            mark["price_basis"] = o_cfg["price_basis"]
        if o_cfg.get("price_basis") == "first_hour" and o_cfg.get("price_after") is not None:
            mark["price_after"] = o_cfg["price_after"]
        if timing == "time":
            out.extend({"date": day.isoformat(), "window": slotgrid.hhmm(b),
                        "label": slotgrid.label(b, length) + " (out of hours)",
                        "remaining": rem, **mark} for b, rem in fits)
        else:
            name = {"am": "Morning", "pm": "Afternoon", EVE: "Evening"}[w]
            out.append({"date": day.isoformat(), "window": w,
                        "label": f"{name} (arrival between {slotgrid.hhmm(lo)} and "
                                 f"{slotgrid.hhmm(hi)}) — out of hours",
                        "remaining": max(r for _, r in fits), **mark})
    return out


def service_availability(cfg: dict | None, booked: list[dict], length_blocks: int,
                         date_from, date_to, now: datetime, *,
                         capacity: int | None = None, qty: int = 1,
                         buffer_blocks: int = 0, resource: str | None = None,
                         timing: str = "time", svc: dict | None = None,
                         day_counts: dict | None = None) -> list[dict]:
    """Bookable START times for a service of `length_blocks` in [date_from, date_to]:
    [{date, window, label, remaining}] where window is the start time ('HH:MM') and a span of
    that length fits within capacity at every block it covers. Same date discipline as the
    office diary (working days, blackouts, notice, horizon) and per-day opening hours.

    `capacity` overrides capacity_per_window (a resource type's count); `qty` is the units the
    new booking consumes (party size / quantity); `buffer_blocks` leaves a gap before/after
    neighbours (turnaround/travel); `resource` counts only that resource type's bookings. With
    the defaults this is the original concurrent-occupancy availability, unchanged.

    `svc` applies the service's OWN rules (docs/plan-diary-multiview.md): its days, its
    booking hours (a start is offered only when the whole service fits inside them) and
    its max a day, against `day_counts` ({YYYY-MM-DD: already booked}). A service with no
    rules — every one before 2026-09-23 — changes nothing."""
    c = _norm(cfg)
    now = now.astimezone(DIARY_TZ)
    cutoff = now + timedelta(hours=service_notice(c, svc))
    start = max(_as_date(date_from), now.date())
    # Past an urgent service's same-day cut-off, today's STANDARD hours are
    # off — its out-of-hours evening is not (Sarah at 17:58: too late for the
    # afternoon, still tonight at the out-of-hours price).
    no_normal_today = past_same_day_cutoff(svc, now)
    end = min(_as_date(date_to), now.date() + timedelta(days=c["horizon_days"]))
    length = max(1, int(length_blocks))
    cap = int(capacity if capacity is not None else c["capacity"])
    q = max(1, int(qty))
    spans = slotgrid.expand_spans(_pool_spans(booked, cfg, resource), buffer_blocks)
    r = services.rules(svc)
    lo_rule = slotgrid.block_at(r["hours_from"]) if r["hours_from"] else None
    hi_rule = slotgrid.block_at(r["hours_until"]) if r["hours_until"] else None
    out, day = [], start
    while day <= end:
        ruled_out = ((r["days"] is not None and day.weekday() not in r["days"])
                     or (r["max_per_day"] and (day_counts or {}).get(day.isoformat(), 0) >= r["max_per_day"]))
        normal_off = no_normal_today and day == now.date()
        if (day.weekday() in c["working_days"] and day not in c["blackouts"] and not ruled_out
                and not normal_off):
            fits = _service_fits(cfg, spans, length, day, cutoff, cap, q)
            if lo_rule is not None or hi_rule is not None:
                fits = [f for f in fits if (lo_rule is None or f[0] >= lo_rule)
                        and (hi_rule is None or f[0] + length <= hi_rule)]
            if r["starts"]:
                fixed = {slotgrid.block_at(t) for t in r["starts"]}
                fits = [f for f in fits if f[0] in fixed]
            desk = desk_allows(cfg, booked, length, day, cutoff, resource)
            if desk is not None:
                fits = [f for f in fits if f[0] in desk]
            if timing == "time":
                out.extend({"date": day.isoformat(), "window": slotgrid.hhmm(sb),
                            "label": slotgrid.label(sb, length), "remaining": rem}
                           for sb, rem in fits)
            else:
                # An arrival-window service is offered as the half-day (or the day)
                # it can be done in, never as a clock time (Kris 2026-09-03).
                for w in (("am", "pm") if timing == "half" else ("day",)):
                    lo, hi = window_bounds(cfg, day, w)
                    inside = [rem for sb, rem in fits if lo <= sb and sb + length <= hi]
                    if inside:
                        out.append({"date": day.isoformat(), "window": w,
                                    "label": arrival_window_label(w, lo, hi),
                                    "remaining": max(inside)})
        if services.out_of_hours(svc) and not ruled_out:
            have = {(x["date"], x["window"]) for x in out}
            out.extend(x for x in _ooh_slots(cfg, svc, spans, length, day, cutoff, cap, q, timing)
                       if (x["date"], x["window"]) not in have)
        day += timedelta(days=1)
    return out


def _service_fits(cfg, spans, length: int, day, cutoff, cap: int, q: int) -> list[tuple[int, int]]:
    """(start_block, remaining) for every grid start on `day` where a span of `length`
    fits below capacity and starts after the notice cutoff (None = no cutoff)."""
    out = []
    for sb in slotgrid.day_service_starts(cfg or {}, length, day):
        if cutoff is not None:
            start_dt = datetime.combine(day, slotgrid.time_at(sb), tzinfo=DIARY_TZ)
            if start_dt < cutoff:
                continue
        abs_start = slotgrid.abs_block(day, sb)
        used = slotgrid.max_concurrent(spans, abs_start, abs_start + length)
        if used + q <= cap:
            out.append((sb, cap - used))
    return out


NOON_BLOCK = 48   # 12:00 on the 15-minute grid — the morning/afternoon divide
ARRIVAL_WINDOWS = ("am", "pm", "day")


def window_bounds(cfg, day, window: str, svc: dict | None = None) -> tuple[int, int]:
    """[lo, hi) blocks an arrival window covers on `day`: 'am' = opening→noon,
    'pm' = noon→closing, 'day' = the whole open day, 'eve' = closing → the
    service's out-of-hours end. On an out-of-hours extra day the service's own
    spans win (its afternoon may run to its `until`)."""
    for w, lo, hi in ooh_windows(cfg, svc, day) if svc else []:
        if w == window:
            return lo, hi
    o, c = slotgrid.open_close_blocks(cfg or {}, day)
    if window == EVE:
        return c, c
    if window == "am":
        return o, min(c, NOON_BLOCK)
    if window == "pm":
        return max(o, NOON_BLOCK), c
    return o, c


def arrival_window_label(window: str, lo: int, hi: int) -> str:
    name = {"am": "Morning", "pm": "Afternoon"}.get(window, "Whole day")
    return f"{name} (arrival between {slotgrid.hhmm(lo)} and {slotgrid.hhmm(hi)})"


def window_words(window: str | None) -> str:
    """Customer-facing words for a booking's window: 'in the morning', 'in the
    afternoon', 'during the day', or 'at 14:00' for an exact start."""
    w = str(window or "").strip()
    if w == "am":
        return "in the morning"
    if w == "pm":
        return "in the afternoon"
    if w == "day":
        return "during the day"
    if w == "eve":
        return "in the evening"
    return f"at {w}" if w else ""


def arrival_words(day, window, today=None) -> str:
    """A booking's timing as a customer would hear it: 'this afternoon',
    'tomorrow morning', 'on Monday 7th September, in the afternoon'. Today and
    tomorrow get their natural words — "on Friday 4th September" for a job
    happening in three hours reads like a different appointment."""
    words = window_words(window)
    if not words:
        return ""
    d, t = dates_mod._as_date(day), dates_mod._as_date(today)
    if d is None:
        return words
    if t is not None:
        near = {0: {"in the morning": "this morning", "in the afternoon": "this afternoon",
                    "during the day": "today", "in the evening": "tonight"},
                1: {"in the morning": "tomorrow morning", "in the afternoon": "tomorrow afternoon",
                    "during the day": "tomorrow", "in the evening": "tomorrow evening"}}.get((d - t).days)
        if near:
            return near.get(words, f"{words} today" if (d - t).days == 0 else f"{words} tomorrow")
    return f"on {dates_mod.pretty_day(d)}, {words}"


def arrival_sentence(day, window, today=None) -> str:
    """The ready sentence a drafter can lift: "we'll be with you this afternoon".

    Live 2026-09-04 (services run) the hold result handed the model the
    ingredients — 'in the afternoon' — and it bolted them onto a noun twice:
    "a this afternoon urgent visit". Ingredients make the phrasing the model's
    problem; a sentence makes it ours."""
    words = arrival_words(day, window, today)
    return f"we'll be with you {words}" if words else ""


def canonical_window(svc: dict | None, window) -> str | None:
    """The `window` an agent/portal/form passed, resolved for this service's timing:
    exact-time services take a start time ('14:00'); arrival-window services take
    'am'/'pm' (a clock time or 'morning'/'afternoon' maps to its half); whole-day
    services always resolve to 'day'. None = not a valid window for this service."""
    t = services.timing(svc)
    w = str(window or "").strip().lower()
    if t == "time":
        if ":" not in w:
            return None
        try:
            return slotgrid.hhmm(slotgrid.block_at(w))
        except (TypeError, ValueError):
            return None
    if t == "day":
        return "day"
    if w in ("am", "morning"):
        return "am"
    if w in ("pm", "afternoon"):
        return "pm"
    if w in (EVE, "evening", "tonight") and services.out_of_hours(svc):
        return EVE
    if ":" in w:
        try:
            return "am" if slotgrid.block_at(w) < NOON_BLOCK else "pm"
        except (TypeError, ValueError):
            return None
    return None


def window_hint(svc: dict | None) -> str:
    """The invalid_window message for this service's timing."""
    t = services.timing(svc)
    if t == "half":
        return ("This service is booked as an ARRIVAL WINDOW: pass window 'am' (morning) or "
                "'pm' (afternoon), exactly as check_availability returned it — never a "
                "clock time.")
    if t == "day":
        return "This service uses a whole-day booking window: pass window 'day'."
    return "window must be a start time like '14:00', exactly as check_availability returned it."


def first_fit_start(cfg, booked: list[dict], length_blocks: int, day, window: str,
                    now: datetime | None, *, capacity: int | None = None, qty: int = 1,
                    buffer_blocks: int = 0, resource: str | None = None,
                    notice_hours: int | None = None, svc: dict | None = None) -> int | None:
    """The grid block a service booking is placed at for `window` on `day`: the exact
    start for a clock-time window (None unless that very start fits), else the
    EARLIEST start inside the arrival window where the span fits below capacity. `now`
    applies the notice cutoff; None skips it (a human adding to their own diary)."""
    c = _norm(cfg)
    length = max(1, int(length_blocks))
    cap = int(capacity if capacity is not None else c["capacity"])
    spans = slotgrid.expand_spans(_pool_spans(booked, cfg, resource), buffer_blocks)
    hours = c["min_notice_hours"] if notice_hours is None else notice_hours
    cutoff = (now.astimezone(DIARY_TZ) + timedelta(hours=hours)) if now else None
    if svc and is_ooh_slot(cfg, svc, _as_date(day), window):
        # An out-of-hours slot (D1 stage 2): its own span, outside opening hours.
        d = _as_date(day)
        for w, lo, hi in ooh_windows(cfg, svc, d):
            if ":" in str(window):
                want = slotgrid.block_at(window)
                if lo <= want < hi:
                    return next((sb for sb, _ in _ooh_fits(cfg, spans, length, d, lo, hi, cutoff, cap,
                                                           max(1, int(qty))) if sb == want), None)
            elif w == window:
                fits = _ooh_fits(cfg, spans, length, d, lo, hi, cutoff, cap, max(1, int(qty)))
                return fits[0][0] if fits else None
        return None
    fits = _service_fits(cfg, spans, length, _as_date(day), cutoff, cap, max(1, int(qty)))
    desk = desk_allows(cfg, booked, length, day, cutoff, resource)
    if desk is not None:
        fits = [f for f in fits if f[0] in desk]
    if ":" in str(window):
        want = slotgrid.block_at(window)
        return next((sb for sb, _ in fits if sb == want), None)
    lo, hi = window_bounds(cfg, _as_date(day), window)
    return next((sb for sb, _ in fits if lo <= sb and sb + length <= hi), None)


def manual_series_dates(cfg_row: dict | None, first, repeat: str, occurrences) -> list[date]:
    """The dates of a standing series placed BY HAND (run #7, the classes business's
    term): from `first`, every `repeat` (weekly / fortnightly / six_weekly, or
    monthly / quarterly by day-of-month), `occurrences` times, capped by the
    tenant's max. A blackout date or a closed day is skipped and the series
    EXTENDS by one step — a term keeps its twelve sessions, it does not lose the
    half-term one (the agent's series, by contrast, reports a skipped date and
    is one shorter). Empty when recurring is off or the repeat isn't offered."""
    rc = diary_config.clean_recurring((cfg_row or {}).get("recurring"))
    rep = str(repeat or "").strip().lower()
    if not rc["enabled"] or rep not in rc["frequencies"]:
        return []
    n = max(1, min(int(occurrences or 1), rc["max_occurrences"]))
    norm = _norm(cfg_row)
    step = diary_config.FREQ_DAYS.get(rep)
    months = diary_config.FREQ_MONTHS.get(rep, 1)
    first = _as_date(first)
    out: list[date] = []
    i = 0
    while len(out) < n and i < n * 2 + 8:
        if step:
            d = first + timedelta(days=step * i)
        else:
            import calendar
            m = first.month - 1 + months * i
            y, mo = first.year + m // 12, m % 12 + 1
            d = date(y, mo, min(first.day, calendar.monthrange(y, mo)[1]))
        i += 1
        if d in norm["blackouts"] or d.weekday() not in norm["working_days"]:
            continue
        out.append(d)
    return out


def series_to_horizon(cfg_row: dict | None, first, repeat: str, weekdays=(), today=None) -> list[dict]:
    """Every occurrence of a standing series from `first` to the diary's booking
    horizon (Kris 2026-09-21: "a list of days/dates to confirm as booked, for as
    long as the settings allow prebookings"), on the named weekdays or `first`'s
    own. Closed days (blackouts, non-working days) are LISTED and flagged, not
    skipped — the person unticks or sees why. Empty when recurring is off or
    the repeat isn't a day-step one. [{date, closed}] in date order."""
    rc = diary_config.clean_recurring((cfg_row or {}).get("recurring"))
    rep = str(repeat or "").strip().lower()
    step = diary_config.FREQ_DAYS.get(rep)
    if not rc["enabled"] or rep not in rc["frequencies"] or not step:
        return []
    norm = _norm(cfg_row)
    first = _as_date(first)
    today = _as_date(today) if today else date.today()
    end = today + timedelta(days=norm["horizon_days"])
    order = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    days = sorted({str(w).strip().lower()[:3] for w in (weekdays or []) if str(w).strip().lower()[:3] in order},
                  key=order.index)
    starts = [first + timedelta(days=(order.index(w) - first.weekday()) % 7) for w in days] or [first]
    out: list[dict] = []
    for start in starts:
        d = start
        while d <= end:
            out.append({"date": d, "closed": d in norm["blackouts"] or d.weekday() not in norm["working_days"]})
            d += timedelta(days=step)
    out.sort(key=lambda x: x["date"])
    return out


def stay_fits_by_date(tenant_id: str, dates, arr_time: str, dep_time: str, client=None) -> dict:
    """Which of `dates` still has room for a stay of arr_time→dep_time that day:
    one read of the live bookings, then the same span math as manual_stay_add
    per date. {date: fits}."""
    days = sorted({_as_date(d) for d in dates or []})
    if not days:
        return {}
    cfg_row = db.get_scheduling_config(tenant_id, client=client) or {}
    merged = _merged(cfg_row)
    capacity = int(merged["capacity_per_window"] or 0)
    booked = db.list_active_bookings(
        tenant_id, (days[0] - timedelta(days=365)).isoformat(), days[-1].isoformat(), client=client)
    spans = _pool_spans(booked, cfg_row, None, exclude_typed=True)
    buf = int(merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    t1, t2 = time.fromisoformat(str(arr_time)[:5]), time.fromisoformat(str(dep_time)[:5])
    out = {}
    for d in days:
        a, b = datetime.combine(d, t1), datetime.combine(d, t2)
        out[d] = b > a and bool(slotgrid.stay_offer(spans, a, b, capacity, buffer_blocks=buffer_blocks)["fits"])
    return out


def stay_resource_types(cfg_row: dict | None, service_types: list | None = None) -> list[dict]:
    """A STAYS diary's space types: its resource types minus the pools its
    appointments are bound to (services.bind_appointment_pools gives each
    appointment a pool of its own name). Empty = the one shared pool of spaces."""
    cfg_row = cfg_row or {}
    appt = set()
    for svc in services.clean_service_types(list(cfg_row.get("service_types") or []) + list(service_types or [])):
        appt.add(" ".join(str(svc.get("resource") or svc["name"]).lower().split()))
    return [r for r in diary_config.clean_resource_types(cfg_row.get("resource_types"))
            if " ".join(str(r["name"]).lower().split()) not in appt]


def stay_has_room(tenant_id: str, arr: datetime, dep: datetime, resource: str | None = None, *,
                  cfg_row: dict | None = None, client=None) -> bool:
    """Is there room for a stay of arr→dep, every night of it? The waiting list's
    question for a span (src/app/courses.py): the same pool, capacity and turnaround
    the executor's own stay check uses, read once."""
    cfg_row = cfg_row if cfg_row is not None else (db.get_scheduling_config(tenant_id, client=client) or {})
    rts = stay_resource_types(cfg_row)
    if rts:
        r = diary_config.find_resource(rts, resource) if resource else (rts[0] if len(rts) == 1 else None)
        if not r:
            return False
        name, capacity = r["name"], int(r["count"])
    else:
        name, capacity = None, int(_merged(cfg_row)["capacity_per_window"] or 0)
    booked = db.list_active_bookings(
        tenant_id, (arr.date() - timedelta(days=365)).isoformat(), dep.date().isoformat(), client=client)
    spans = _pool_spans(booked, cfg_row, name, exclude_typed=name is None)
    buf = int(_merged(cfg_row).get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    return dep > arr and bool(slotgrid.stay_offer(spans, arr, dep, capacity,
                                                  buffer_blocks=buffer_blocks)["fits"])


def manual_series_by_weekday(cfg_row: dict | None, first, repeat: str, occurrences,
                             weekdays) -> list[list[date]]:
    """One standing series per named weekday (Kris 2026-09-21: "Mondays,
    Wednesdays and Thursdays as Mabel's regular slots"): each starts on the
    first such weekday on or after `first` and runs `occurrences` times by
    manual_series_dates. No weekdays named → one series from `first`."""
    first = _as_date(first)
    order = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    days = [w for w in (weekdays or []) if str(w).strip().lower()[:3] in order]
    if not days:
        s = manual_series_dates(cfg_row, first, repeat, occurrences)
        return [s] if s else []
    out: list[list[date]] = []
    for w in sorted({d.strip().lower()[:3] for d in days}, key=order.index):
        start = first + timedelta(days=(order.index(w) - first.weekday()) % 7)
        s = manual_series_dates(cfg_row, start, repeat, occurrences)
        if s:
            out.append(s)
    return out


def manual_service_add(tenant_id: str, day, service_name: str, window: str, summary: str,
                       details: dict | None = None, contact: str | None = None,
                       client=None, recurrence_id: str | None = None) -> tuple[dict | None, str | None]:
    """Manual 'Add booking' on a SERVICES diary (Kris 2026-09-03: the form offered only
    'Whole day' against a per-service diary). Human-entered, so notice/horizon/working-day
    rules are skipped (their diary, their call) — capacity still runs with the agent's
    own span math, and an arrival-window service lands at the earliest fit in its half.
    Returns (row, None) or (None, 'unknown_service' | 'bad_window' | 'full')."""
    readiness, _ = make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        return None, blocked["error"]

    cfg_row = db.get_scheduling_config(tenant_id, client=client) or {}
    svc = services.find(cfg_row.get("service_types"), service_name)
    if not svc:
        return None, "unknown_service"
    w = canonical_window(svc, window)
    if not w:
        return None, "bad_window"
    length = slotgrid.blocks_for_minutes(svc["minutes"])
    merged = _merged(cfg_row)
    capacity, resource = int(merged["capacity_per_window"] or 0), None
    if svc.get("resource"):
        r = diary_config.find_resource(cfg_row.get("resource_types"), svc["resource"])
        if r:
            capacity, resource = int(r["count"]), r["name"]
    buf_min = int(svc.get("buffer") or merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf_min // slotgrid.BLOCK_MIN) if buf_min > 0 else 0
    d = _as_date(day)
    booked = db.list_active_bookings(tenant_id, (d - timedelta(days=1)).isoformat(),
                                     d.isoformat(), client=client)
    sb = first_fit_start(cfg_row, booked, length, d, w, None, capacity=capacity,
                         buffer_blocks=buffer_blocks, resource=resource)
    if sb is None:
        return None, "full"
    dets = dict(details or {})
    dets.setdefault("Service", svc["name"])
    row = db.insert_stay_booking(
        tenant_id, d.isoformat(), sb, length, summary, details=dets, contact=contact,
        source="manual", status="confirmed", window=w, resource_type=resource,
        recurrence_id=recurrence_id, client=client)
    return row, None


def service_diary_line(cfg: dict | None, service_types: list[dict] | None) -> str:
    """The one-line diary shape for a service tenant — the base diary_line plus the
    configured services with their durations/prices, injected into the agent prompt so it
    maps the customer's request to a known service length."""
    policy = booking_policy.compile_policy(cfg)
    if not policy["ready"]:
        return policy["message"]

    line = diary_line(cfg)
    svcs = services.clean_service_types(service_types)
    if svcs:
        bits = []
        for s in svcs:
            label = f"{s['name']} ({s['minutes']} min" + (f", £{s['price']:g}" if s.get("price") is not None else "") + ")"
            t = services.timing(s)
            if t == "half":
                label += " [arrival window: morning or afternoon]"
            elif t == "day":
                label += " [whole day]"
            bits.append(label)
        line += "\nServices: " + "; ".join(bits) + "."
    return line


# ── pure diary math ──────────────────────────────────────────────────────────
def availability(
    cfg: dict | None, booked: list[dict], date_from, date_to, now: datetime
) -> list[dict]:
    """Bookable slots in [date_from, date_to]: [{date, window, remaining}], only
    where remaining > 0, filtered by working days, blackouts, notice and horizon.
    `booked` is the tenant's bookings overlapping the range (any status is safe
    to pass; only active ones count)."""
    if not booking_policy.compile_policy(cfg)["ready"]:
        return []

    # Computed on the unified 15-minute grid (slotgrid.office_availability). The grid
    # reproduces the legacy window/overlap math EXACTLY for grid-aligned bookings
    # (tests/test_slotgrid_parity.py) — same {date, window, remaining} shape, so the agent
    # tool and Voice path are unchanged — and additionally counts an off-grid hold a
    # slot-size change left behind, which the old code silently dropped.
    return slotgrid.office_availability(cfg, booked, date_from, date_to, now.astimezone(DIARY_TZ))


def day_summary(slots: list[dict]) -> list[dict]:
    """Per-day counts for a period ask ('what's your availability next week?'):
    [{date, day, free}] where `free` is how many bookable windows/start times
    that day still has (every entry in `slots` already has remaining > 0).
    Attached to a multi-day check_availability result so the agent can answer
    'Tuesday has 4 slots, Wednesday 5 — which day suits?' with counts the tool
    actually returned. Counts are informational: nothing is reserved until
    concrete times go through offer_slots/hold_booking. Pure — slots in."""
    by_day: dict[str, int] = {}
    for s in slots:
        d = str(s["date"])
        by_day[d] = by_day.get(d, 0) + 1
    return [{"date": d, "day": _as_date(d).strftime("%A %d %B"), "free": n}
            for d, n in sorted(by_day.items())]


def hold_error(cfg: dict | None, booking_date, window: str, now: datetime) -> str | None:
    """Pre-flight for a hold request (everything except capacity, which only the
    atomic RPC can answer). Returns a clean error code, or None when bookable:
      'invalid_window'  — window doesn't exist in the tenant's mode
      'outside_horizon' — before the notice cutoff or past the booking horizon
      'closed'          — not a working day, or a blackout date
    """
    if not booking_policy.compile_policy(cfg)["ready"]:
        return "unsupported"

    c = _norm(cfg)
    day = _as_date(booking_date)
    now = now.astimezone(DIARY_TZ)
    if window not in c["windows"]:
        return "invalid_window"
    if (
        _window_start(day, window) < now + timedelta(hours=c["min_notice_hours"])
        or day > now.date() + timedelta(days=c["horizon_days"])
    ):
        return "outside_horizon"
    if day.weekday() not in c["working_days"] or day in c["blackouts"]:
        return "closed"
    return None


def offer_expiry(cfg: dict | None, booking_date, window: str, now: datetime,
                 svc: dict | None = None, *, start_at: datetime | None = None) -> datetime:
    """Earliest of TTL, real opening minus notice, and an applied last call.

    Missing/blank last call is off. A deadline already passed is not extended;
    offer writers refuse it. Existing stored expiries are never recalculated.
    """
    c = _norm(cfg)
    ttl = max(1, int((cfg or {}).get("offer_ttl_hours") or DEFAULT_CONFIG["offer_ttl_hours"]))
    now = now.astimezone(DIARY_TZ)
    day = _as_date(booking_date)
    start = start_at or (_window_start(day, window) if ":" in window else
             datetime.combine(day, time(), tzinfo=DIARY_TZ)
             + timedelta(minutes=window_bounds(cfg, day, window, svc)[0] * slotgrid.BLOCK_MIN))
    if start.tzinfo is None:
        start = start.replace(tzinfo=DIARY_TZ)
    notice = services.rules(svc).get("notice_hours") if svc else None
    caps = [(now.astimezone(timezone.utc) + timedelta(hours=ttl)).astimezone(DIARY_TZ),
            (start.astimezone(timezone.utc) - timedelta(
                hours=c["min_notice_hours"] if notice is None else notice)).astimezone(DIARY_TZ)]
    last_call = (cfg or {}).get("offer_last_call")
    if last_call:
        caps.append(datetime.combine(day - timedelta(days=1), _as_time(last_call), tzinfo=DIARY_TZ))
    return min(caps, key=lambda cap: cap.astimezone(timezone.utc))


# ── tool-facing wrappers (the executor calls these; Voice will too) ──────────
def check_availability(tenant_id: str, date_from, date_to, now: datetime | None = None,
                       client=None) -> list[dict]:
    """The check_availability tool: live availability for one tenant."""
    now = now or datetime.now(DIARY_TZ)
    cfg = db.get_scheduling_config(tenant_id, client=client)
    start, end = _as_date(date_from), _as_date(date_to)
    booked = db.list_active_bookings(tenant_id, start.isoformat(), end.isoformat(), client=client)
    return availability(cfg, booked, start, end, now)


def place_hold(
    tenant_id: str,
    booking_date,
    window: str,
    summary: str,
    details: dict | None = None,
    contact: str | None = None,
    source: str = "email",
    thread_id: str | None = None,
    now: datetime | None = None,
) -> tuple[dict | None, str | None]:
    """The hold_booking tool: place a provisional 'proposed' hold. Returns
    (booking_row, None) on success or (None, error_code) — error codes are
    hold_error()'s plus 'full' when the atomic capacity check loses the slot.
    The agent path only ever creates holds; confirming is a human action (D4)."""
    readiness, _ = make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        return None, blocked["error"]

    now = now or datetime.now(DIARY_TZ)
    cfg = db.get_scheduling_config(tenant_id)
    day = _as_date(booking_date)
    err = hold_error(cfg, day, window, now)
    if err:
        return None, err
    row = db.hold_booking_if_capacity(
        tenant_id, day.isoformat(), window, summary,
        details=details, contact=contact, source=source, thread_id=thread_id,
    )
    if not row:
        return None, "full"
    # Dual-write the unified slot-grid coordinates (docs/architecture/unified-slot-grid.md) alongside the
    # legacy `window`. Best-effort metadata — a failure here must never void a real hold.
    try:
        blocks = slotgrid.window_to_blocks(window, _norm(cfg)["slot_minutes"], cfg or {})
        if blocks:
            db.set_booking_blocks(row["id"], blocks[0], blocks[1], tenant_id)
            row["start_block"], row["length_blocks"] = blocks
    except Exception:
        pass
    return row, None


# ── Diary view-model (/app/diary) ────────────────────────────────────────────
def diary_days(cfg: dict | None, booked: list[dict], now: datetime) -> list[dict]:
    """Date groups for the Diary: every day from today to the horizon that is a
    working day OR has bookings (a booking on a closed day is never hidden), each
    with per-window occupancy and its bookings. Pure — config + rows in."""
    c = _norm(cfg)
    today = now.astimezone(DIARY_TZ).date()
    from app import intake as _intake
    stay_mode = _intake.stay_mode(cfg)
    # A stays diary's appointments (Kris 2026-09-13): labelled by time and
    # service, never counted against the spaces.
    from app import services as _services
    appt_types = (cfg or {}).get("service_types") if stay_mode else None
    def _is_appt(b) -> bool:
        return bool(appt_types) and _services.is_appointment_row(b, appt_types)
    # The day chart's appointment lanes: each service's pool, in its colour.
    appt_lanes = []
    if appt_types:
        from app import diary_config as _dc
        for k, svc in enumerate(_services.clean_service_types(appt_types), start=1):
            pool = _dc.find_resource((cfg or {}).get("resource_types"), svc.get("resource") or svc["name"])
            appt_lanes.append({"svc": svc, "resource": pool["name"] if pool else svc["name"],
                               "count": int(pool["count"]) if pool else 1,
                               "colour": DIARY_TYPE_COLOURS[k % len(DIARY_TYPE_COLOURS)]})
    # Stamp every STAY booking with its arrival→departure span (display) once, so the diary
    # can show it on each day it covers — a multi-day stay is no longer pinned to its
    # arrival date. Appointments (within a day) get a span too; it just reads as that day.
    for b in booked:
        span = slotgrid.stay_span(b)
        if not (span and b.get("status", "confirmed") in ACTIVE_STATUSES
                and not b.get("spaces") and not b.get("engineer_id")):
            continue
        arr = slotgrid.abs_to_datetime(span[0])
        dep = slotgrid.abs_to_datetime(span[0] + span[1])
        if stay_mode and _is_appt(b):
            svc_name = (b.get("details") or {}).get("Service") or b.get("resource_type")
            b["is_stay"] = False
            b["is_appointment"] = True
            b["time_label"] = f"{arr.strftime('%H:%M')}–{dep.strftime('%H:%M')} · {svc_name}"
            rt = str(b.get("resource_type") or "").strip().lower()
            b["dot"] = next((a["colour"]["bar"] for a in appt_lanes if a["resource"].lower() == rt),
                            DIARY_TYPE_COLOURS[1]["bar"])
        elif stay_mode:
            nights = max(0, (dep.date().toordinal() - arr.date().toordinal()))
            b["is_stay"] = True
            b["dot"] = DIARY_TYPE_COLOURS[0]["bar"]
            b["stay_label"] = (f"{arr.strftime('%a %d %b %H:%M')} → {dep.strftime('%a %d %b %H:%M')}"
                               + (f" · {nights} night{'s' if nights != 1 else ''}" if nights else ""))
        elif b.get("window") in ARRIVAL_WINDOWS and b.get("window") != "day":
            # An arrival-window job: the customer was told the half-day; the grid
            # placement is the operator's pencilled start, shown as such.
            b["time_label"] = (f"{window_label(b['window'])} · pencilled in "
                               f"{arr.strftime('%H:%M')}–{dep.strftime('%H:%M')}")
        else:  # appointment — a within-day time label for the chart's detail panel
            # A mode migration preserves existing spans, including several days.
            # Keep both dates visible when the current diary is appointments.
            b["time_label"] = (f"{arr.strftime('%a %d %b %H:%M')} → {dep.strftime('%a %d %b %H:%M')}"
                               if arr.date() != dep.date() else
                               f"{arr.strftime('%H:%M')}–{dep.strftime('%H:%M')}")
    # Offered slots carry their release deadline as display labels (the grey
    # "Offered" pill in the diary): the absolute deadline plus a countdown.
    for b in booked:
        if b.get("status") == "offered" and b.get("expires_at"):
            try:
                exp = datetime.fromisoformat(str(b["expires_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            b["offer_until_label"] = exp.astimezone(DIARY_TZ).strftime("%a %d %b, %H:%M")
            left = exp - now
            mins = int(left.total_seconds() // 60)
            if mins <= 0:
                b["offer_countdown"] = "releasing"
            elif mins < 60:
                b["offer_countdown"] = f"releases in {mins}m"
            elif mins < 48 * 60:
                b["offer_countdown"] = f"releases in {-(-mins // 60)}h"
            else:
                b["offer_countdown"] = f"releases in {-(-mins // (24 * 60))} days"
    out = []
    for offset in range(c["horizon_days"] + 1):
        day = today + timedelta(days=offset)
        todays = [b for b in booked if _booking_on_day(b, day)]
        working = day.weekday() in c["working_days"] and day not in c["blackouts"]
        if not (working or todays):
            continue
        windows = []
        pool_rows = [b for b in todays if not _is_appt(b)]     # the spaces, without the appointments
        for w in c["windows"]:
            used = _used(pool_rows, day, w, cfg, c["slot_minutes"])
            windows.append({"window": w, "used": used, "capacity": c["capacity"],
                            "remaining": max(0, c["capacity"] - used)})
        # The month cell of a STAYS diary (Kris 2026-09-13: "make the Diary
        # view smart, especially if it's acting as a couple of diary types"):
        # how many are in against the spaces, and each appointment service's
        # count on the day — "3 in · 27 free · 2 meet & greets".
        appts = []
        if stay_mode and appt_types:
            live = [b for b in todays
                    if b.get("status", "confirmed") in ("proposed", "confirmed") and _is_appt(b)]
            for svc in _services.clean_service_types(appt_types):
                n = sum(1 for b in live
                        if str(b.get("resource_type") or "").strip().lower() == svc["name"].lower())
                if n:
                    appts.append({"name": svc["name"], "count": n})
        types = day_types(cfg, todays, windows, day, stay_mode, appt_types)
        # The Stays view's month cell (docs/plan-diary-multiview.md): who comes
        # in and who goes home today, beside the spaces free.
        stay_in = stay_out = 0
        if stay_mode:
            for b in pool_rows:
                if b.get("status", "confirmed") not in ("proposed", "confirmed"):
                    continue
                span = slotgrid.stay_span(b)
                if not span:
                    continue
                stay_in += slotgrid.abs_to_datetime(span[0]).date() == day
                stay_out += slotgrid.abs_to_datetime(span[0] + span[1]).date() == day
        out.append({
            "date": day.isoformat(), "label": day.strftime("%A %d %B"),
            "working": working, "windows": windows, "bookings": todays, "stay_mode": stay_mode,
            "stay_used": max((w["used"] for w in windows), default=0) if stay_mode else 0,
            "appts": appts, "stay_in": stay_in, "stay_out": stay_out,
            # The cell's bars (one per diary type) and whether EVERY type is full —
            # a stays business with 30 subjects in and both introductory visit pools open is not
            # "Full" (Kris 2026-09-13: "what if capacity is full but slots aren't?").
            "types": types,
            "all_full": bool(types) and all(t["full"] for t in types),
            "full": all(w["remaining"] == 0 for w in windows),
            # Day-level offered count (the grey "N offered" chip): offers consume
            # capacity in `windows` above, this says how much of that is only soft.
            "offered_count": sum(1 for b in todays if b.get("status") == "offered"),
            "chart": _day_chart(_merged(cfg), todays, day, c["capacity"], appt_lanes, cfg)
                     if c["mode"] == "slots" else None,
        })
    return out


def _month_weeks(today: date, end: date):
    """Monday-aligned weeks, month by month — a week NEVER straddles two of them.

    Kris, 2026-09-02: "The diary isn't splitting months correctly. They bleed
    into each other."

    Both calendars used to pad the whole today→horizon range into a flat run of
    weeks and let the template start a new heading whenever the MONDAY's month
    changed. A week from Mon 31 Aug to Sun 6 Sep therefore filed six September
    days under "August 2026", and October's own heading did not appear until the
    5th because the 1st to the 4th had already been spent inside September.

    Each month now gets its own grid: the days either side belong to a
    neighbour and come back as None, which the templates render as a blank cell.
    Yields (month_label, monday, [date|None x7]).
    """
    month = date(today.year, today.month, 1)
    while month <= end:
        nxt = date(month.year + (month.month == 12), (month.month % 12) + 1, 1)
        label = month.strftime("%B %Y")
        cur = month - timedelta(days=month.weekday())
        while cur < nxt:
            yield label, cur, [
                (cur + timedelta(days=i)) if month <= cur + timedelta(days=i) < nxt
                else None
                for i in range(7)
            ]
            cur += timedelta(days=7)
        month = nxt


def calendar_weeks(cfg: dict | None, booked: list[dict], now: datetime) -> list[dict]:
    """The month→week→day payload for the calendar Diary: diary_days() padded into
    Monday-aligned weeks covering today → horizon. Cells outside the bookable
    range render muted; in-diary cells carry their full day group (windows +
    bookings) so the day panel needs no extra fetch."""
    day_map = {d["date"]: d for d in diary_days(cfg, booked, now)}
    c = _norm(cfg)
    today = now.astimezone(DIARY_TZ).date()
    cutoff = now.astimezone(DIARY_TZ) + timedelta(hours=c["min_notice_hours"])
    end = today + timedelta(days=c["horizon_days"])
    weeks = []
    for label, cur, dates in _month_weeks(today, end):
        days = []
        for d in dates:
            if d is None:                    # a neighbouring month's day
                days.append({"filler": True, "month_label": label,
                             "in_diary": False, "out": True})
                continue
            entry = day_map.get(d.isoformat())
            cell = {
                "date": d.isoformat(), "dom": d.day,
                "month_label": label,
                "today": d == today,
                "out": d < today or d > end,
                "in_diary": entry is not None,
            }
            if entry:
                # Offered slots are counted separately in the month cell — "2
                # booked · 1 offered" reads honestly, "3 booked" would not.
                active = [b for b in entry["bookings"]
                          if b.get("status", "confirmed") in ("proposed", "confirmed")]
                # notice_blocked: every window starts inside the minimum-notice
                # cutoff — the same per-window rule the agent books by, so the
                # month cell says "needs Xh notice" on exactly the days the agent
                # will refuse instead of a "free" the owner then disbelieves
                # (2026-07-26 e2e: "free" Monday, agent correctly refusing it).
                # A stays diary's "free" is spaces, not windows summed (a slots
                # diary has an hourly window each; the stay sits in all of them).
                cell.update(entry, booked_count=len(active),
                            free=(max(0, c["capacity"] - entry["stay_used"]) if entry.get("stay_mode")
                                  else sum(w["remaining"] for w in entry["windows"])),
                            notice_hours=c["min_notice_hours"],
                            notice_blocked=(not cell["out"]) and all(
                                _window_start(d, w["window"]) < cutoff
                                for w in entry["windows"]))
            days.append(cell)
        weeks.append({"start": cur.isoformat(),
                      "label": f"Week of {cur.day} {cur.strftime('%B')}", "days": days})
    return weeks


_WINDOW_LABELS = {"day": "Whole day", "am": "Morning (AM)", "pm": "Afternoon (PM)",
                  "eve": "Evening (out of hours)"}


def window_label(window: str) -> str:
    return _WINDOW_LABELS.get(window, window)


def office_rows(rows: list[dict]) -> list[dict]:
    """The office diary's working set: people rows only. A row carrying `spaces`
    is a transport booking and belongs to the fleet board (D1); a row carrying
    `engineer_id` is a field-service job and belongs to the team member board
    (docs/modules/module-field-service.md). On a windows-only tenant nothing is filtered."""
    return [r for r in rows if r.get("spaces") is None and not r.get("engineer_id")]


def diary(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    """Everything /app/diary renders: the calendar weeks, the (merged) config,
    the one-line diary shape, and the windows offered by the add-booking form.
    Transport rows are filtered out — the office diary shows people."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    cfg_row = db.get_scheduling_config(tenant_id, client=client)
    rows = office_rows(db.list_bookings(tenant_id, now.date().isoformat(), client=client))
    merged = _merged(cfg_row)
    return {
        "weeks": calendar_weeks(cfg_row, rows, now),
        "config": merged,
        "summary": diary_line(cfg_row),
        "mode": merged["windows"],
        "window_options": [(w, window_label(w)) for w in _norm(cfg_row)["windows"]],
    }


# ═══ Fleet module (docs/modules/module-fleet-capacity.md) ═══════════════════════════
# Fleet resources as the unit of capacity. Pure math first (unit-tested without a DB),
# then the tool definitions and the board view-model. The atomic capacity
# decision itself lives in the hold_fleet_booking RPC — like windows mode, this
# layer can only ever be too cautious, never wrong.

# D2 — built-in factors for the canonical load-unit names. Tenant pallet_types rows
# (seeded on activation, editable later) overlay these; an unknown name counts
# as a FULL load-unit (conservative) and the tool result says what it assumed so
# the draft can state the assumption.
DEFAULT_PALLET_FACTORS = {"full": 1.0, "euro": 1.0, "half": 0.5, "quarter": 0.25}

# Spaces are numeric(6,2); load-unit sums beyond this are junk input, not a load.
_MAX_SPACES = 9999.0


def fmt_spaces(value) -> str:
    """'6.5' not '6.50', '8' not '8.0' — fractional spaces display per the spec."""
    return f"{float(value):g}"


def pallet_factors(rows: list[dict] | None) -> dict:
    """The tenant's name→factor map: built-in canonical factors overlaid with
    their pallet_types rows (so an unseeded tenant still sizes 'half' as 0.5)."""
    factors = dict(DEFAULT_PALLET_FACTORS)
    for r in rows or []:
        name = str(r.get("name") or "").strip().lower()
        if name:
            factors[name] = float(r["space_factor"])
    return factors


def order_spaces(factors: dict, pallets) -> tuple[float, list[str]]:
    """Demand = Σ qty × factor (D2). Returns (spaces, assumed_full_types) where
    assumed_full_types are the names counted as full load units because they aren't
    in the factor map. Raises ValueError/TypeError/KeyError on junk shapes —
    callers map that to an invalid_pallets tool error."""
    if not isinstance(pallets, list) or not pallets:
        raise ValueError("pallets must be a non-empty list")
    total, assumed = 0.0, []
    for p in pallets:
        qty = float(p["qty"])
        if qty <= 0:
            raise ValueError("qty must be positive")
        name = str(p.get("type") or "").strip().lower()
        factor = factors.get(name)
        if factor is None:
            factor = 1.0
            assumed.append(name or "unspecified")
        total += qty * factor
    if total <= 0 or total > _MAX_SPACES:
        raise ValueError("implausible total spaces")
    return round(total, 2), assumed


def resolve_branch(branches: list[dict], explicit=None, default_branch_id=None):
    """The D4 resolution chain → (branch_id | None, error | None, branch_names).
    branch_id None with no error means THE WHOLE FLEET — the right pool for a
    zero/one-branch tenant (strays unassigned to any branch stay bookable).
      explicit name        → that branch ('unknown_branch' when it matches none)
      mailbox default      → that branch (ignored if the branch was deleted)
      ≤ 1 branch           → whole fleet (a single-branch tenant never thinks
                             about branches)
      else                 → 'branch_required' (the agent asks the customer)
    """
    names = [b.get("name") or "" for b in branches]
    if explicit is not None and str(explicit).strip():
        want = str(explicit).strip().lower()
        hit = next((b for b in branches
                    if (b.get("name") or "").strip().lower() == want), None)
        if hit:
            return hit["id"], None, names
        return None, "unknown_branch", names
    if default_branch_id and any(b["id"] == default_branch_id for b in branches):
        return default_branch_id, None, names
    if len(branches) <= 1:
        return None, None, names
    return None, "branch_required", names


def _fleet_cfg(cfg: dict | None) -> dict:
    """The scheduling_config row through fleet eyes: working days, notice,
    horizon and blackouts apply unchanged; windows/capacity are superseded (D3),
    so the window model is forced to whole-day for hold_error/availability."""
    merged = dict(cfg or {})
    merged["windows"] = "day"
    return merged


def earliest_bookable_day(cfg: dict | None, now: datetime):
    """The first date a NEW whole-day booking could legally start — working
    days, blackouts and the notice rule only (capacity not considered). The
    honest answer to 'why is nothing available this soon': the agent must say
    'too short notice, the earliest date is X', never 'fully booked'."""
    c = _norm(_fleet_cfg(cfg))
    now = now.astimezone(DIARY_TZ)
    cutoff = now + timedelta(hours=c["min_notice_hours"])
    day = now.date()
    for _ in range(c["horizon_days"] + 1):
        if (day.weekday() in c["working_days"] and day not in c["blackouts"]
                and _window_start(day, "day") >= cutoff):
            return day
        day += timedelta(days=1)
    return None


def _day_loads(vehicles: list[dict], booked: list[dict], day: date) -> list[dict]:
    """Per-resource load for one date: [{vehicle, used_spaces, used_kg,
    free_spaces, free_kg}] (free_kg None = no legal limit configured). Only
    ACTIVE bookings count; bookings with no recorded weight weigh 0 kg — the
    spec is honest that weight enforcement is only as good as what customers
    declare. Pass the ACTIVE fleet resource pool; a booking on a fleet resource outside it
    simply doesn't appear (a deactivated fleet resource's load can't free pool space)."""
    used: dict = {}
    for b in booked:
        if (b.get("vehicle_id")
                and b.get("status", "confirmed") in ACTIVE_STATUSES
                and _as_date(b["booking_date"]) == day):
            slot = used.setdefault(b["vehicle_id"], {"spaces": 0.0, "kg": 0.0})
            slot["spaces"] += float(b.get("spaces") or 0)
            slot["kg"] += float(b.get("weight_kg") or 0)
    out = []
    for v in vehicles:
        u = used.get(v["id"], {"spaces": 0.0, "kg": 0.0})
        cap = float(v["capacity_spaces"])
        max_kg = v.get("max_payload_kg")
        out.append({
            "vehicle": v,
            "used_spaces": round(u["spaces"], 2),
            "used_kg": round(u["kg"], 1),
            "free_spaces": round(cap - u["spaces"], 2),
            "free_kg": round(float(max_kg) - u["kg"], 1) if max_kg is not None else None,
        })
    return out


def best_fit(loads: list[dict], spaces: float, weight_kg: float | None = None) -> dict | None:
    """D1 — the fleet resource a load goes on: the SMALLEST remaining space that still
    fits (ties → smallest capacity, then id — deterministic), so big vans stay
    free for big orders. Both constraints must pass when known: spaces always,
    weight only when the order states one AND the fleet resource has a legal limit.
    None = no single fleet resource fits (one fleet resource per order — splits are v2).
    Mirrors the hold_fleet_booking RPC, which makes the same choice atomically."""
    fits = [
        l for l in loads
        if l["free_spaces"] >= spaces - 1e-9
        and (weight_kg is None or l["free_kg"] is None or l["free_kg"] >= weight_kg - 1e-9)
    ]
    if not fits:
        return None
    return min(fits, key=lambda l: (l["free_spaces"],
                                    float(l["vehicle"]["capacity_spaces"]),
                                    str(l["vehicle"]["id"])))


def fleet_availability(
    cfg: dict | None, vehicles: list[dict], booked: list[dict],
    date_from, date_to, now: datetime,
    spaces: float | None = None, weight_kg: float | None = None,
) -> list[dict]:
    """Bookable dates in [date_from, date_to] against the fleet resource pool:
    [{date, remaining_spaces}] where remaining_spaces is the LARGEST load a
    single fleet resource could still take that day — the honest number for one order
    (D1); fleet-total free space would overpromise across fragmented vans.
    With a load given (spaces/weight_kg), only dates the load actually FITS are
    returned, each marked fits:true. Working days, blackouts, notice and
    horizon filter exactly as windows mode ('day' starts at midnight)."""
    c = _norm(_fleet_cfg(cfg))
    now = now.astimezone(DIARY_TZ)
    cutoff = now + timedelta(hours=c["min_notice_hours"])
    start = max(_as_date(date_from), now.date())
    end = min(_as_date(date_to), now.date() + timedelta(days=c["horizon_days"]))

    out = []
    day = start
    while day <= end:
        if (day.weekday() in c["working_days"] and day not in c["blackouts"]
                and _window_start(day, "day") >= cutoff):
            loads = _day_loads(vehicles, booked, day)
            largest = max((l["free_spaces"] for l in loads), default=0.0)
            if spaces is not None:
                if best_fit(loads, spaces, weight_kg):
                    out.append({"date": day.isoformat(),
                                "remaining_spaces": round(largest, 2), "fits": True})
            elif largest > 0:
                out.append({"date": day.isoformat(), "remaining_spaces": round(largest, 2)})
        day += timedelta(days=1)
    return out


def fleet_diary_line(cfg: dict | None, vehicles: list[dict], branches: list[dict]) -> str:
    """The one-line diary shape in its fleet flavour — shown in the UI and
    injected into the agent's <scheduling> prompt section. Names the branches so
    the agent can match customer wording ('our Leeds branch') to a `branch` arg."""
    merged = _merged(cfg)
    days = ", ".join(t.strip().title() for t in str(merged["working_days"]).split(",") if t.strip())
    active = [v for v in vehicles if v.get("active", True)]
    if not active:
        return (f"Fleet diary: no vehicles configured yet, so capacity is unknown; "
                f"working days {days}.")
    total = sum(float(v["capacity_spaces"]) for v in active)
    line = (f"Fleet diary: {len(active)} vehicle{'s' if len(active) != 1 else ''}, "
            f"{fmt_spaces(total)} pallet spaces in total")
    names = [b.get("name") or "" for b in branches]
    if len(names) > 1:
        line += f", across {len(names)} branches ({', '.join(names)})"
    return line + (f"; bookings are whole-day, sized in pallet spaces; "
                   f"working days {days}.")


# Fleet flavour of the agent tools (D5): same two channel-agnostic names — the
# voice module will call exactly these — gaining the load arguments. Injected
# ONLY when the fleet row is active; windows-mode tenants keep SCHEDULING_TOOLS
# byte-identical. Static constants for the same prompt-cache reason as above.
# Replies talk capacity, never vans: nothing in these schemas or results names
# a fleet resource.
_PALLETS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string",
                     "description": "Pallet type as the customer said it — e.g. 'full', "
                                    "'euro', 'half', 'quarter'."},
            "qty": {"type": "number", "description": "How many pallets of this type."},
        },
        "required": ["type", "qty"],
        "additionalProperties": False,
    },
    "description": "The customer's load as pallet counts by type. Include it whenever "
                   "the email states quantities.",
}

FLEET_TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "Check the business's real booking diary for free capacity. Call this BEFORE "
            "offering, agreeing to, or declining ANY date — never rely on memory or "
            "guesswork for availability. Returns every bookable date between date_from "
            "and date_to (inclusive) as {date, remaining_spaces}, where remaining_spaces "
            "is the largest load (in pallet spaces) the business could still take as one "
            "booking that day. Pass the customer's load — pallets, and weight_kg when "
            "stated — to get only the dates that load actually fits, marked fits:true. "
            "Any date NOT in the result is not bookable (closed, full, too short notice, "
            "or beyond the booking horizon)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "format": "date",
                              "description": "Start of the range to check, YYYY-MM-DD."},
                "date_to": {"type": "string", "format": "date",
                            "description": "End of the range (inclusive), YYYY-MM-DD."},
                "pallets": _PALLETS_SCHEMA,
                "weight_kg": {"type": "number",
                              "description": "Total load weight in kg — ONLY when the "
                                             "customer states a weight."},
                "branch": {"type": "string",
                           "description": "Branch/depot name — ONLY when the customer "
                                          "names one (e.g. 'Leeds')."},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict, same as windows mode: `details` is deliberately free-form jsonb.
        "name": "hold_booking",
        "description": (
            "Place ONE provisional hold in the diary for this email's request. You MUST "
            "call this whenever the customer asks to book a date and check_availability "
            "shows their load fits that date — do not merely tell them it is available, "
            "and never say a booking is held unless this tool returned held:true. At "
            "most one hold per email. The hold really reserves capacity; a human "
            "confirms or releases it later. Returns the booking id, or an error "
            "explaining why it cannot be held."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The booking date, YYYY-MM-DD."},
                "pallets": _PALLETS_SCHEMA,
                "weight_kg": {"type": "number",
                              "description": "Total load weight in kg — ONLY when the "
                                             "customer states a weight."},
                "branch": {"type": "string",
                           "description": "Branch/depot name — ONLY when the customer "
                                          "names one."},
                "summary": {"type": "string",
                            "description": "One line describing the job, e.g. "
                                           "'6 pallets, M17 -> LS9'."},
                "details": {"type": "object",
                            "description": "Structured facts pulled from the email "
                                           "(postcodes, access notes, references...). "
                                           "Include what the business would need to do "
                                           "the job."},
            },
            "required": ["date", "pallets", "summary"],
            "additionalProperties": False,
        },
    },
]


# Routing flavour (part of the 'tms' module — route-planning D2): the
# SAME two tool names once more; only hold_booking changes, gaining the
# location/reference args so the manifest and the map get structured data.
# Injected ONLY when the routing row is active — fleet-without-routing tenants
# keep FLEET_TOOLS byte-identical (frozen by tests). Built from FLEET_TOOLS at
# import time so the two can never drift; still a static module constant, so
# the prompt cache stays stable. FLEET_TOOLS order is [check, hold] (asserted
# in tests), so index 1 is hold_booking. The props live in one dict so the
# dual-diary routing variant below carries exactly the same capture contract.
_ROUTING_HOLD_PROPS = {
    "collection_postcode": {
        "type": "string",
        "description": "Collection-point postcode — ONLY when the email states one "
                       "(e.g. 'WF17 9LU'). Never guess or invent it.",
    },
    "delivery_postcode": {
        "type": "string",
        "description": "Delivery-point postcode — ONLY when the email states one. "
                       "Never guess or invent it.",
    },
    "reference": {
        "type": "string",
        "description": "The CUSTOMER'S own order / PO / invoice reference for this "
                       "job — ONLY when the email states one, exactly as written.",
    },
}
FLEET_ROUTING_TOOLS = copy.deepcopy(FLEET_TOOLS)
FLEET_ROUTING_TOOLS[1]["input_schema"]["properties"].update(copy.deepcopy(_ROUTING_HOLD_PROPS))


# The dual-diary AGENT tooling (DUAL_TOOLS / DUAL_ROUTING_TOOLS and the per-call
# `diary` enum) was retired in the multi-agent hand-off (docs/modules/module-agent-handoff.md,
# Phase 2a). A logistics tenant no longer routes office vs transport through one
# prompt's `diary` argument: the base agent owns the office diary (SCHEDULING_TOOLS)
# and hands transport off to the TMS specialist (src/app/agents/tms.py), which uses
# FLEET_ROUTING_TOOLS. The capacity ISOLATION between the two (a sized row never
# consumes an office window) lives on in availability()/_used(); the board keeps its
# own `dual` flag (below) to hide office rows from the transport board.


def routing_fields(args: dict) -> dict:
    """The routing capture (route-planning D2): the optional location/reference
    hold args, trimmed, dropped when blank. Postcodes are uppercased (UK
    postcodes are case-insensitive; geocoding and dedup prefer one casing);
    the reference keeps the customer's exact casing. Pure so the capture rules
    are testable without a db."""
    out = {}
    for key in ("collection_postcode", "delivery_postcode"):
        val = str(args.get(key) or "").strip().upper()
        if val:
            out[key] = val[:16]
    ref = str(args.get("reference") or "").strip()
    if ref:
        out["reference"] = ref[:80]
    return out


# ── Plan-scoring geometry (transport-planner D4) ─────────────────────────────
# Straight-line haversine over cached postcode coordinates — plenty to RANK
# load merges and reject geographic nonsense (a Hull drop on a Manchester
# run). Google stays where it is: map display and, later, drop sequencing.

_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles between two lat/lng points."""
    from math import asin, cos, radians, sin, sqrt
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lng2 - lng1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * asin(sqrt(h))


def tour_miles(start: tuple | None, points: list[tuple]) -> float:
    """Estimated run length: a greedy nearest-neighbour chain from `start`
    (the branch) through every point (the located drops), in haversine miles,
    no return leg. `start` None (no branch postcode) chains from the first
    point. Not an optimiser — a consistent yardstick to COMPARE two
    assignments of the same drops; ties break by list order, so the score is
    deterministic."""
    remaining = list(points)
    if not remaining:
        return 0.0
    if start is None:
        here, total = remaining.pop(0), 0.0
    else:
        here, total = start, 0.0
    while remaining:
        best_i, best_d = 0, None
        for i, p in enumerate(remaining):
            d = haversine_miles(here[0], here[1], p[0], p[1])
            if best_d is None or d < best_d - 1e-9:
                best_i, best_d = i, d
        here = remaining.pop(best_i)
        total += best_d or 0.0
    return total


def booking_movable(b: dict) -> bool:
    """Can the PLANNER move this booking to another fleet resource (transport-planner
    D5 guardrails)? v1: same-day fleet resource moves only — a booking is movable iff
    it is sized (a real fleet booking), active, and currently on a fleet resource.
    Date changes are NEVER movable by the planner in v1 (they need the human
    approval flow); calendar busy-blocks and legacy windows rows have no
    resource assignment/size and are never touched."""
    return bool(
        b.get("spaces") is not None
        and b.get("vehicle_id")
        and b.get("status", "confirmed") in ACTIVE_STATUSES
    )


# ── Fleet board view-model (/app/diary in fleet mode — D6) ───────────────────
def fleet_day_groups(cfg: dict | None, vehicles: list[dict], booked: list[dict],
                     now: datetime, branch_id: str | None = None,
                     dual: bool = False) -> list[dict]:
    """Date groups for the fleet board. Each working (or booked) day carries:
      vehicles   — one row per visible fleet resource (active, or inactive-but-loaded
                   that day): its bookings, used/capacity spaces and kg
      unassigned — fleet bookings whose fleet resource was deleted (spaces set, no
                   fleet resource): kept on their date for a human to re-place,
                   counted against nothing
      legacy     — windows-era bookings (no spaces): rendered date-grouped,
                   counted against nothing. With `dual` (transport-planner D1:
                   the scheduling row is ALSO active) these are OFFICE bookings
                   with a surface of their own (/app/diary), so the board
                   doesn't show them at all.
    used/total spaces count ACTIVE fleet resources only (the real bookable pool).
    `branch_id` filters the board to one branch's fleet resources (D6)."""
    if dual:
        booked = [b for b in booked if b.get("spaces") is not None]
    c = _norm(_fleet_cfg(cfg))
    today = now.astimezone(DIARY_TZ).date()
    visible = [v for v in vehicles if branch_id is None or v.get("branch_id") == branch_id]
    pool = [v for v in visible if v.get("active", True)]
    pool_ids = {v["id"] for v in pool}
    total = round(sum(float(v["capacity_spaces"]) for v in pool), 2)

    out = []
    for offset in range(c["horizon_days"] + 1):
        day = today + timedelta(days=offset)
        todays = [b for b in booked if _as_date(b["booking_date"]) == day]
        working = day.weekday() in c["working_days"] and day not in c["blackouts"]
        if not (working or todays):
            continue
        loads = _day_loads(visible, todays, day)
        rows = []
        for l in loads:
            v = l["vehicle"]
            v_bookings = [b for b in todays if b.get("vehicle_id") == v["id"]]
            if not (v.get("active", True) or v_bookings):
                continue   # inactive and empty that day — declutter
            rows.append({**l, "bookings": v_bookings})
        used = round(sum(l["used_spaces"] for l in loads
                         if l["vehicle"]["id"] in pool_ids), 2)
        unassigned = [b for b in todays
                      if b.get("spaces") is not None and not b.get("vehicle_id")]
        legacy = [b for b in todays
                  if b.get("spaces") is None and not b.get("vehicle_id")]
        if not working and not (rows and any(r["bookings"] for r in rows)) \
                and not unassigned and not legacy:
            continue   # closed day with nothing on it (todays were filtered bookings)
        out.append({
            "date": day.isoformat(), "label": day.strftime("%A %d %B"),
            "working": working, "vehicles": rows,
            "unassigned": unassigned, "legacy": legacy,
            "used_spaces": used, "total_spaces": total,
            "booked_count": sum(len(r["bookings"]) for r in rows) + len(unassigned),
            "full": total > 0 and used >= total - 1e-9,
        })
    return out


def fleet_weeks(cfg: dict | None, vehicles: list[dict], booked: list[dict],
                now: datetime, branch_id: str | None = None,
                dual: bool = False) -> list[dict]:
    """fleet_day_groups() padded into Monday-aligned weeks — the same month →
    week → day shape calendar_weeks() gives the windows Diary, so the board
    template renders the same grid with fleet cells."""
    day_map = {d["date"]: d
               for d in fleet_day_groups(cfg, vehicles, booked, now, branch_id, dual=dual)}
    c = _norm(_fleet_cfg(cfg))
    today = now.astimezone(DIARY_TZ).date()
    end = today + timedelta(days=c["horizon_days"])
    weeks = []
    for label, cur, dates in _month_weeks(today, end):
        days = []
        for d in dates:
            if d is None:                    # a neighbouring month's day
                days.append({"filler": True, "month_label": label,
                             "in_diary": False, "out": True})
                continue
            entry = day_map.get(d.isoformat())
            cell = {
                "date": d.isoformat(), "dom": d.day,
                "month_label": label,
                "today": d == today,
                "out": d < today or d > end,
                "in_diary": entry is not None,
            }
            if entry:
                cell.update(entry)
            days.append(cell)
        weeks.append({"start": cur.isoformat(),
                      "label": f"Week of {cur.day} {cur.strftime('%B')}", "days": days})
    return weeks


def fleet_board(tenant_id: str, client=None, now: datetime | None = None,
                branch_id: str | None = None, dual: bool = False) -> dict:
    """Everything /app/board renders: the board weeks, the fleet summary line,
    the branch filter, and the manual-add form's options. `dual` = the
    scheduling row is also active, so office rows stay off this surface."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    cfg_row = db.get_scheduling_config(tenant_id, client=client)
    branches = db.list_branches(tenant_id, client=client)
    vehicles = db.list_vehicles(tenant_id, client=client)
    rows = db.list_bookings(tenant_id, now.date().isoformat(), client=client)
    if branch_id and not any(b["id"] == branch_id for b in branches):
        branch_id = None
    types = db.list_pallet_types(tenant_id, client=client) or [
        {"name": n, "space_factor": f} for n, f in DEFAULT_PALLET_FACTORS.items()
    ]
    return {
        "weeks": fleet_weeks(cfg_row, vehicles, rows, now, branch_id=branch_id, dual=dual),
        "config": _merged(cfg_row),
        "summary": fleet_diary_line(cfg_row, vehicles, branches),
        "branches": branches, "branch_id": branch_id,
        "vehicles": vehicles,
        "bookings": rows,   # raw rows — the planner's plan-diff/banner input
        "type_options": [(t["name"], float(t["space_factor"])) for t in types],
    }


# ── Resource-day manifest (route-planning module, slice 1) ────────────────────
def vehicle_manifest(vehicle: dict, bookings: list[dict], day: date) -> dict:
    """What's on this van that day — the manifest the dispatcher (and later the
    map and the xlsx export) reads. Active bookings only (proposed + confirmed —
    the same capacity-consuming set the board shows), oldest first to match the
    board's chip order. `located` counts rows with a delivery postcode — the
    mappable set — so the page can say '2 of 5 drops unlocated' honestly instead
    of quietly drawing a partial route."""
    rows = sorted(
        (b for b in bookings
         if b.get("vehicle_id") == vehicle["id"]
         and b.get("status", "confirmed") in ACTIVE_STATUSES
         and _as_date(b["booking_date"]) == day),
        key=lambda b: str(b.get("created_at") or ""),
    )
    out_rows = [{
        "id": b["id"],
        "booking_ref": b.get("booking_ref"),
        "summary": b.get("summary") or "Booking",
        "contact": b.get("contact"),
        "reference": b.get("reference"),
        "status": b.get("status", "confirmed"),
        "spaces": float(b.get("spaces") or 0),
        "weight_kg": float(b["weight_kg"]) if b.get("weight_kg") is not None else None,
        "collection_postcode": b.get("collection_postcode"),
        "delivery_postcode": b.get("delivery_postcode"),
        "details": b.get("details") or {},
        "source": b.get("source"),
    } for b in rows]
    cap = float(vehicle["capacity_spaces"])
    max_kg = vehicle.get("max_payload_kg")
    located = sum(1 for r in out_rows if r["delivery_postcode"])
    return {
        "vehicle": vehicle,
        "date": day.isoformat(),
        "label": day.strftime("%A %d %B"),
        "rows": out_rows,
        "used_spaces": round(sum(r["spaces"] for r in out_rows), 2),
        "total_spaces": cap,
        "used_kg": round(sum(r["weight_kg"] or 0 for r in out_rows), 1),
        "max_kg": float(max_kg) if max_kg is not None else None,
        "drops": len(out_rows),
        "located": located,
        "unlocated": len(out_rows) - located,
    }


def manifest_view(tenant_id: str, vehicle_id: str, day_iso: str, client=None) -> dict | None:
    """Everything the manifest page renders. None = fleet resource not found / not this
    tenant's (the route 404s). Raises ValueError on a junk date (also a 404).
    The branch rides along as the route origin-to-be (its address/postcode)."""
    vehicle = db.get_vehicle(vehicle_id, tenant_id, client=client)
    if not vehicle:
        return None
    day = _as_date(day_iso)
    rows = db.list_vehicle_day_bookings(tenant_id, vehicle_id, day.isoformat(), client=client)
    out = vehicle_manifest(vehicle, rows, day)
    branches = db.list_branches(tenant_id, client=client)
    out["branch"] = next((b for b in branches if b["id"] == vehicle.get("branch_id")), None)
    return out


def parse_blackouts(text: str | None) -> list[str]:
    """Settings-form helper: free text (one date per line, or comma-separated) →
    sorted unique ISO dates. Invalid tokens are dropped, not fatal — a typo should
    never block saving the rest of the diary config."""
    out = set()
    for token in re.split(r"[,\n]", text or ""):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(date.fromisoformat(token).isoformat())
        except ValueError:
            pass
    return sorted(out)


# ── Calendar Connect (v1.1) — busy-sync mapping + reconcile planner ─────────
def event_blocks(cfg: dict | None, event: dict, now: datetime) -> list[tuple]:
    """Map one calendar event onto the diary: [(date_iso, window), …] for every
    window the event overlaps, within [today, horizon], skipping closed days
    (no capacity there to protect). All-day events block the whole day in any
    mode; in day mode any event blocks the day."""
    c = _norm(cfg)
    now = now.astimezone(DIARY_TZ)
    today = now.date()
    horizon_end = today + timedelta(days=c["horizon_days"])
    start = event["start"].astimezone(DIARY_TZ)
    end = event["end"].astimezone(DIARY_TZ)
    if end <= start:
        end = start + timedelta(minutes=1)
    out = []
    day = max(start.date(), today)
    last = min((end - timedelta(seconds=1)).date(), horizon_end)
    while day <= last:
        if day.weekday() in c["working_days"] and day not in c["blackouts"]:
            day_start = datetime.combine(day, time(0, 0), tzinfo=DIARY_TZ)
            seg_start = max(start, day_start)
            seg_end = min(end, day_start + timedelta(days=1))
            if event.get("all_day") or c["mode"] == "day":
                out.append((day.isoformat(), "day"))
            elif c["mode"] == "ampm":
                noon = day_start + timedelta(hours=12)
                if seg_start < noon:
                    out.append((day.isoformat(), "am"))
                if seg_end > noon:
                    out.append((day.isoformat(), "pm"))
            else:
                for w in c["windows"]:
                    ws = _window_start(day, w)
                    if seg_start < ws + timedelta(minutes=c["slot_minutes"]) and seg_end > ws:
                        out.append((day.isoformat(), w))
        day += timedelta(days=1)
    return out


def plan_calendar_sync(cfg: dict | None, rows: list[dict], events: list[dict],
                       now: datetime) -> dict:
    """Pure reconciler for the busy-sync poller. `rows` are the tenant's bookings
    that carry a calendar_event_id (any status); `events` are the busy events
    currently in the calendar window. Never touches events BLAM8 pushed, never
    resurrects a block the tenant cancelled (tombstone), cancels blocks whose
    event moved or disappeared, and leaves the past alone."""
    today = now.astimezone(DIARY_TZ).date().isoformat()
    pushed = {r["calendar_event_id"] for r in rows
              if r.get("source") != "calendar" and r.get("calendar_event_id")}
    imports: dict = {}
    for r in rows:
        if r.get("source") == "calendar" and r.get("calendar_event_id"):
            imports.setdefault(r["calendar_event_id"], []).append(r)

    add, cancel, seen = [], [], set()
    for event in events:
        eid = event["id"]
        seen.add(eid)
        if eid in pushed:
            continue
        existing = imports.get(eid, [])
        if existing and all(r["status"] in ("cancelled", "declined") for r in existing):
            continue  # the tenant removed this block — stay removed
        active = {(r["booking_date"], r["window"]): r for r in existing
                  if r["status"] not in ("cancelled", "declined")}
        targets = event_blocks(cfg, event, now)
        for d, w in targets:
            if (d, w) not in active:
                add.append({"event_id": eid, "summary": (event.get("subject") or "Busy")[:200],
                            "date": d, "window": w})
        target_set = set(targets)
        for (d, w), r in active.items():
            if (d, w) not in target_set and d >= today:
                cancel.append(r["id"])

    # Pushed bookings whose event was deleted BY HAND in the calendar (Julius
    # 2026-09-03): never auto-cancelled — a fat-fingered event deletion must
    # not void a customer's booking — but the mismatch is FLAGGED so the diary
    # shows a chip. flag/unflag carry only TRANSITIONS (the sweep runs every
    # few minutes; steady state must write nothing).
    flag_missing, unflag_missing = [], []
    for r in rows:
        if r.get("source") == "calendar" or not r.get("calendar_event_id"):
            continue
        flagged = bool((r.get("details") or {}).get("calendar_event_missing_at"))
        if r["calendar_event_id"] in seen:
            if flagged:
                unflag_missing.append(r["id"])
        elif (r["status"] in ("proposed", "confirmed")
                and (r.get("booking_date") or "") >= today and not flagged):
            flag_missing.append(r["id"])

    for eid, rws in imports.items():   # events deleted from the calendar
        if eid in seen:
            continue
        for r in rws:
            if r["status"] not in ("cancelled", "declined") and r["booking_date"] >= today:
                cancel.append(r["id"])
    return {"add": add, "cancel": cancel,
            "flag_missing": flag_missing, "unflag_missing": unflag_missing}


# ── agent tools (D3 — the locked platform pattern; Voice reuses these verbatim) ─
# Static module constants: the tool list must be byte-stable across requests or it
# invalidates the prompt cache (tools render first in the prompt). The volatile bit
# (today's date) lives in the system prompt's scheduling section, not here.
SCHEDULING_TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "Check the business's real booking diary for free capacity. Call this BEFORE "
            "offering, agreeing to, or declining ANY date or time — never rely on memory or "
            "guesswork for availability. Returns every bookable slot between date_from and "
            "date_to (inclusive) as {date, window, remaining}. Windows may be whole days "
            "('day'), half-days ('am'/'pm'), or start times like '14:00', depending on how "
            "the business runs its diary. Any date or window NOT in the result is not "
            "bookable (closed, full, too short notice, or beyond the booking horizon)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "format": "date",
                              "description": "Start of the range to check, YYYY-MM-DD."},
                "date_to": {"type": "string", "format": "date",
                            "description": "End of the range (inclusive), YYYY-MM-DD."},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict: `details` is deliberately free-form jsonb (D5 — industry-shaped
        # facts), and strict mode forbids open objects. The executor validates instead.
        "name": "hold_booking",
        "description": (
            (
                "Place ONE provisional hold in the diary for this email's request. You MUST call this whenever "
                "the customer asks to book a date and check_availability shows that date and window with "
                "remaining capacity — do not merely tell them it is available, and never say a slot is held "
                "unless this tool returned held:true. One hold per visit — if the email is about several "
                "dependants (several named subjects) hold each one separately, one call per subject, up to "
                "three. The hold really reserves diary capacity; a human confirms or releases it later. "
                "Returns the booking id, or an error explaining why the slot cannot be held."
            )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The booking date, YYYY-MM-DD."},
                "window": {"type": "string",
                           "description": "A window value EXACTLY as check_availability "
                                          "returned it for that date — 'day', 'am', 'pm', "
                                          "or a start time like '14:00'."},
                "summary": {"type": "string",
                            "description": "One line describing this request in the customer's words."},
                "details": {"type": "object",
                            "description": (
                                "Structured facts pulled from the email (the requested service, quantity, location and "
                                "relevant notes). Include what the business would need to do the job."
                            )},
            },
            "required": ["date", "window", "summary"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict: `details` is free-form jsonb, like hold_booking.
        "name": "offer_slots",
        "description": (
            "Reserve up to THREE slots as exclusive OFFERS for this customer while they "
            "choose (docs: the offer IS the hold). You MUST call this whenever your reply "
            "lists specific dates/times as options — the customer's requested time is taken "
            "and you're offering the nearest alternatives (max 2, nearest before and after, "
            "same day preferred), or they gave a day / time of day to choose within (max 3, "
            "spread across it). Every option you put in front of a customer must be "
            "reserved here first so no other enquiry can take it while they decide. The "
            "offers auto-release at their own expires_at deadlines; your reply MUST state "
            "the deadline_line sentence verbatim and ask which slot suits. Pass slot "
            "values EXACTLY as check_availability returned them. Do NOT use this when the "
            "customer asked to book one specific available time — that is hold_booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "description": "The slots to offer, best first — at most 3 (2 when "
                                   "answering a taken requested time).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "format": "date",
                                     "description": "The slot's date, YYYY-MM-DD."},
                            "window": {"type": "string",
                                       "description": "A window value EXACTLY as "
                                                      "check_availability returned it for "
                                                      "that date."},
                        },
                        "required": ["date", "window"],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string",
                            "description": (
                                "One line describing what these options are for, using this request's facts."
                            )},
                "details": {"type": "object",
                            "description": "Structured facts pulled from the email, as for "
                                           "hold_booking."},
            },
            "required": ["slots", "summary"],
            "additionalProperties": False,
        },
    },
]

# Service-duration flavour of the office tools — SAME two names (the base agent uses these
# when the tenant defined service_types, app/services), gaining a `service` argument so the
# booked length is the service's duration, not a fixed slot. Static constant for the same
# prompt-cache reason as the windows/stay tools; the tenant's own service list is described
# in the prompt (service_diary_line), not baked into an enum, so this stays byte-stable.
SERVICE_TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "Check the business's real booking diary for free start times for a SERVICE. "
            "This business offers set services, each a fixed length, so pass the `service` "
            "(one of the services listed for this business) and you get only the start times "
            "where that service's full length still fits. Call this BEFORE offering, agreeing "
            "to, or declining ANY time — never guess. Returns bookable slots between date_from "
            "and date_to (inclusive) as {date, window, remaining}, where window is the start "
            "time (e.g. '14:00') — or 'am' / 'pm' / 'day' for a service booked as an ARRIVAL "
            "WINDOW (the customer is told the morning or the afternoon, not a clock time). "
            "Any time NOT in the result is not bookable (closed, full, too short notice, or "
            "beyond the booking horizon)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "format": "date",
                              "description": "Start of the range to check, YYYY-MM-DD."},
                "date_to": {"type": "string", "format": "date",
                            "description": "End of the range (inclusive), YYYY-MM-DD."},
                "service": {"type": "string",
                            "description": "The service the customer wants, as one of the "
                                           "configured services, so the right length is used. "
                                           "Omit only if the service isn't known yet."},
                "services": {"type": "array", "items": {"type": "string"},
                             "description": (
                                 "SEVERAL services in ONE visit (Service A and Service B) — list them all and the check uses "
                                 "their combined length, in ONE call. Then `service` may be omitted."
                             )},
                "resource": {"type": "string",
                             "description": "The resource type, ONLY when the business lists "
                                            "resource types and the customer needs a specific "
                                            "one (e.g. a named room or station)."},
                "party_size": {"type": "integer",
                               "description": "Number of people, ONLY when this business books "
                                              "by party size."},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict: `details` is deliberately free-form jsonb, like the other hold tools.
        "name": "hold_booking",
        "description": (
            (
                "Place ONE provisional hold in the diary for this email's request. You MUST call this whenever "
                "the customer asks to book a service and check_availability shows a start time with remaining "
                "capacity — do not merely tell them it is available, and never say a slot is held unless this "
                "tool returned held:true. One hold per visit - if the email is about several dependants "
                "(several named subjects) hold each one separately, one call per subject, up to three (a "
                "recurring series still counts as one). The booking's length is the service's duration. The "
                "hold really reserves diary capacity; a human confirms or releases it later. Returns the "
                "booking id, or an error explaining why it can't be held."
            )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The booking date, YYYY-MM-DD (the FIRST date of a "
                                        "recurring series)."},
                "window": {"type": "string",
                           "description": "The start time EXACTLY as check_availability "
                                          "returned it for that date, e.g. '14:00'."},
                "service": {"type": "string",
                            "description": "The service being booked, as one of the configured "
                                           "services — this sets how long the slot is."},
                "summary": {"type": "string",
                            "description": (
                                "One line describing this booking in the customer's words."
                            )},
                "resource": {"type": "string",
                             "description": "The resource type, ONLY when the business lists "
                                            "resource types and a specific one is needed."},
                "party_size": {"type": "integer",
                               "description": "Number of people, ONLY when this business books "
                                              "by party size."},
                "repeat": {"type": "string",
                           "enum": ["weekly", "fortnightly", "monthly", "six_weekly", "quarterly"],
                           "description": "ONLY when the business offers recurring bookings AND "
                                          "the customer wants a STANDING repeat slot — one of the "
                                          "frequencies the business offers (six_weekly = every "
                                          "six weeks, quarterly = every three months)."},
                "occurrences": {"type": "integer",
                                "description": "How many times the recurring booking repeats "
                                               "(used with `repeat`)."},
                "delivery_required": {"type": "boolean",
                                      "description": "ONLY when the business offers delivery and "
                                                     "the customer wants it."},
                "delivery_address": {"type": "string",
                                     "description": "The delivery address, when delivery_required."},
                "no_charge": {"type": "boolean",
                              "description": "true ONLY when the business's own rules or knowledge "
                                             "you were given say THIS visit is free of charge (for "
                                             "example a return visit to put right the business's own "
                                             "work under its guarantee). The booking is then stamped "
                                             "with no price and nothing is quoted or invoiced for it. "
                                             "Never set it on your own judgement: no rule, no flag."},
                "details": {"type": "object",
                            "description": "Structured facts pulled from the email (the booking "
                                           "intake fields this business requires)."},
            },
            "required": ["date", "window", "service", "summary"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict: `details` is free-form jsonb, like hold_booking.
        "name": "offer_slots",
        "description": (
            "Reserve up to THREE start times as exclusive OFFERS for this customer while "
            "they choose. You MUST call this whenever your reply lists specific times as "
            "options — the customer's requested time is taken and you're offering the "
            "nearest alternatives (max 2, nearest before and after, same day preferred), "
            "or they gave a day / time of day to choose within (max 3, spread across it). "
            "Every option you put in front of a customer must be reserved here first so no "
            "other enquiry can take it while they decide. The booked length is the "
            "service's duration. Each offer auto-releases at its own expires_at "
            "deadline; your reply MUST include deadline_line verbatim and ask "
            "which time suits. Pass times EXACTLY as check_availability returned them. Do "
            "NOT use this when the customer asked to book one specific available time — "
            "that is hold_booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "description": "The start times to offer, best first — at most 3 (2 "
                                   "when answering a taken requested time).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "format": "date",
                                     "description": "The slot's date, YYYY-MM-DD."},
                            "window": {"type": "string",
                                       "description": "The start time EXACTLY as "
                                                      "check_availability returned it, "
                                                      "e.g. '14:00'."},
                        },
                        "required": ["date", "window"],
                        "additionalProperties": False,
                    },
                },
                "service": {"type": "string",
                            "description": "The service being offered, as one of the "
                                           "configured services — this sets how long each "
                                           "offered slot is."},
                "summary": {"type": "string",
                            "description": "One line describing the booking these options "
                                           "are for, e.g. 'Cut & blow dry — Sarah'."},
                "no_charge": {"type": "boolean",
                              "description": "true ONLY when the business's own rules or knowledge "
                                             "you were given say THIS visit is free of charge (for "
                                             "example a return visit to put right the business's own "
                                             "work under its guarantee). The booking is then stamped "
                                             "with no price and nothing is quoted or invoiced for it. "
                                             "Never set it on your own judgement: no rule, no flag."},
                "details": {"type": "object",
                            "description": "Structured facts pulled from the email, as for "
                                           "hold_booking."},
                "resource": {"type": "string",
                             "description": "The resource type, ONLY when the business "
                                            "lists resource types and a specific one is "
                                            "needed."},
                "party_size": {"type": "integer",
                               "description": "Number of people, ONLY when this business "
                                              "books by party size."},
            },
            "required": ["slots", "service", "summary"],
            "additionalProperties": False,
        },
    },
]


# When it's full, the business chose (docs/plan-when-its-full.md). The setting
# picks what the agent is TOLD — Kris 2026-09-23: "the switch changes the agent's
# context, so the agent knows different, instead of knowing there are options".
# On "hold" every tool is returned untouched (the same objects: byte-identical to
# before the setting existed). On "offer"/"off" the stay offer tool is not handed
# over at all, and the lines that say "reserve the alternative" are rewritten, so
# nothing the agent reads tells it to do what this business does not do.
_STAY_FULL_HOLD_LINE = ("Do not hold that opening — the customer "
                        "has not agreed to it — reserve it with offer_stay and ask.")
_STAY_FULL_LINE = {
    "offer": ("Do not hold that opening — the customer has not agreed to it. Name it and "
              "ask; this business reserves nothing until they say yes."),
    "off": "If it's full, say so: this business does not offer other dates.",
}
_SLOTS_FULL_CLAUSE = re.compile(
    r"the customer's requested time is taken\s+and you're offering the\s+nearest alternatives "
    r"\(max 2, nearest before and after,\s+same day preferred\),\s+or ")
_SLOTS_EVERY_OPTION = "Every option you put in front of a customer must be reserved here first"
_SLOTS_FULL_NOTE = {
    "offer": (" (alternatives to a FULL request are the exception: this business names them "
              "without reserving them — never call this for those)"),
    "off": " (this business offers no alternatives to a FULL request — never call this for those)",
}


def tools_for_other_dates(tools: list[dict], other: str) -> list[dict]:
    if other not in _STAY_FULL_LINE:
        return tools
    out = []
    for t in tools:
        name = (t or {}).get("name")
        desc = str((t or {}).get("description") or "")
        if name == "offer_stay":
            continue
        if name == "check_availability" and _STAY_FULL_HOLD_LINE in " ".join(desc.split()):
            desc = " ".join(desc.split()).replace(_STAY_FULL_HOLD_LINE, _STAY_FULL_LINE[other])
            t = {**t, "description": desc}
        elif name == "offer_slots" and _SLOTS_FULL_CLAUSE.search(desc):
            desc = _SLOTS_FULL_CLAUSE.sub("", desc)
            desc = desc.replace(_SLOTS_EVERY_OPTION, _SLOTS_EVERY_OPTION + _SLOTS_FULL_NOTE[other])
            t = {**t, "description": desc}
        out.append(t)
    return out


# The waiting list (src/app/courses.py, slice 3): offered to the model ONLY when the
# tenant keeps one (pipeline._scheduling_setup / portal_data.tools_for append it).
JOIN_WAITLIST_TOOL = {
    "name": "join_waitlist",
    "description": (
        "Add the customer to the WAITING LIST for a class, start time or stay that is full — "
        "only after they said yes to being added (or asked for it). One call per participant. "
        "For a class or time pass `date` + `window`; for a STAY pass `arrival` + `departure` "
        "exactly as they asked. Returns their position; tell them it and that a place is "
        "offered to the first waiting when one comes up. Never use this when there is space: "
        "hold it instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "format": "date",
                     "description": "The date they wanted, YYYY-MM-DD (a class that runs every week "
                                    "is listed by its weekday and time — pass any date it runs)."},
            "window": {"type": "string",
                       "description": "The start time they wanted, e.g. '09:30' (or 'am'/'pm'/'day')."},
            "arrival": {"type": "string",
                        "description": "A stay: the arrival they asked for, YYYY-MM-DDTHH:MM."},
            "departure": {"type": "string",
                          "description": "A stay: the departure they asked for, YYYY-MM-DDTHH:MM."},
            "resource": {"type": "string",
                         "description": "A stay: the resource type, when the diary has several."},
            "service": {"type": "string",
                        "description": "The service / class, as one of the configured services."},
            "details": {"type": "object",
                        "description": "Optional: anything the customer said about it (a note)."},
        },
        "required": [],
        "additionalProperties": False,
    },
}


# Cancelling (run #8, 2026-09-05): the customer's OWN booking, by its reference.
# The executor applies the Cancellations & credits rule (notice measured from
# now), writes the register, settles the wallet and returns the outcome — the
# reply then states the rule and what happened, and never defers to a colleague.
CANCEL_BOOKING_TOOL = {
    "name": "cancel_booking",
    "description": (
        "Cancel ONE of the customer's own existing bookings (the ones listed for them) when "
        "they ask to cancel it, skip it or can't make it. Pass its reference (e.g. 'BK-1042'). "
        "The business's cancellation rule is applied automatically from the notice given "
        "(late = charged or spends their credit; in time = no charge, credit returned) and the "
        "outcome comes back — say it plainly in the reply. To MOVE a booking use move_booking, "
        "never this. Never use this on a slot held in this conversation, and never on another "
        "customer's booking."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "booking_ref": {"type": "string",
                            "description": "The booking's reference exactly as listed for this customer, e.g. 'BK-1042'."},
            "reason": {"type": "string",
                       "description": "Optional: the customer's reason, in a few words (kept on the register)."},
        },
        "required": ["booking_ref"],
        "additionalProperties": False,
    },
}


MOVE_BOOKING_TOOL = {
    "name": "move_booking",
    "description": (
        "Move ONE of the customer's own existing bookings (the ones listed for them) to a new "
        "date and slot they asked for — pass its reference (e.g. 'BK-1042'), the new date and "
        "the new slot exactly as check_availability lists it (a start time like '10:00', or "
        "'am' / 'pm' / 'day'). Call check_availability for the new date first. In time, the "
        "booking moves in place and keeps its reference; inside the business's notice rule the "
        "old booking is cancelled as a late cancellation (charged or spends their credit) and "
        "the new time is held — the result says which happened; say it plainly. One booking per "
        "call (one date of a standing series is fine; the whole series is rearranged by the "
        "business). Never on a slot held in this conversation, never on another customer's."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "booking_ref": {"type": "string",
                            "description": "The booking's reference exactly as listed for this customer, e.g. 'BK-1042'."},
            "new_date": {"type": "string", "description": "The new date, YYYY-MM-DD."},
            "new_window": {"type": "string",
                           "description": "The new slot as check_availability lists it: a start time '10:00', or 'am' / 'pm' / 'day'."},
            "reason": {"type": "string",
                       "description": "Optional: the customer's reason, in a few words (kept on the register)."},
        },
        "required": ["booking_ref", "new_date", "new_window"],
        "additionalProperties": False,
    },
}


# What the model should do about each hold failure — prescriptive, so the reply
# stays honest and the conversation moves forward.
_HOLD_ERROR_MESSAGES = {
    "ooh_needs_owner": ("That time is out of hours, and this business decides those itself — "
                        "don't hold or promise it; hold the soonest standard time instead."),
    "full": "That window just filled up. Call check_availability again and offer the "
            "customer the nearest available alternative.",
    "outside_horizon": "That date is outside the bookable range — too soon for the "
                       "business's minimum notice, or beyond its booking horizon. Offer "
                       "a date check_availability shows as available.",
    "closed": (
        "The business does not take bookings that day (outside the configured working days, or a "
        "blackout date). Offer a date check_availability shows as available."
    ),
    "invalid_window": "That window does not exist in this diary. Use exactly the window "
                      "values check_availability returns.",
}


def _missing_info(missing: list[str]) -> dict:
    """The intake gate's refusal, worded so the agent RETRIES instead of
    apologising (live 2026-08-31: a refusal over mismatched detail keys was
    read as a system fault and the customer got a 'snag our end' draft)."""
    return {"error": "missing_info", "missing": missing,
            "message": ("Do not book yet — this business requires these details "
                        "first: " + ", ".join(missing) + ". Key each one in "
                        "`details` EXACTLY by the name shown. If the customer "
                        "already gave a value anywhere in this conversation, call "
                        "this tool again with it filled in; otherwise do NOT call "
                        "again — ask the customer for it in your reply. A "
                        "placeholder (<UNKNOWN>, TBC, 'not given', '?') is not a "
                        "value and is refused the same way. In the reply say "
                        "NOTHING about availability, days, times or slots: nothing "
                        "is reserved until these details are in, and the times come "
                        "next. This is a normal gathering step, not a fault: never "
                        "tell the customer something went wrong.")}


# ── Stays and hire — multi-day bookings on the continuous grid ──
# When the tenant's booking requirements have the Overnight-stays toggle on (intake.is_stay),
# the office agent gets THESE tools instead of the slot tools above: a booking is a
# continuous span from an arrival to a departure (which may run across days), and capacity
# is concurrent occupancy — N spaces/rooms/units (the Bookings-per-slot value). The decision
# is slotgrid.stay_offer ("does the span fit? else the nearest start").

# Holds one stay email may place: unlike a slot diary, one email can book several
# separate visits (a day visit AND an overnight weekend — live e2e 2026-09-02).
STAY_HOLDS_MAX = 3
# Non-stay diaries keep one hold per email — EXCEPT when the email is about
# several dependants (two assets, two subjects): each distinct subject is its own
# visit and its own hold (run #6, 2026-09-04: "the diary only allows one hold
# per email, so I wasn't able to lock in Sam's Inspection… could you send a separate
# email" reached the customer). Same cap as stays.
SUBJECT_HOLDS_MAX = 3


def _subject_names(args: dict | None) -> set[str]:
    """The dependant names a hold_booking call is about, lowercased — from
    `subjects` as the model wrote it ([{name…}] or ["Murphy"])."""
    raw = (args or {}).get("subjects") or []
    if isinstance(raw, (dict, str)):
        raw = [raw]
    out: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        name = item if isinstance(item, str) else (item or {}).get("name") if isinstance(item, dict) else ""
        name = " ".join(str(name or "").split()).lower()
        if name:
            out.add(name)
    return out

STAY_TOOLS = [
    {
        "name": "check_availability",
        "description": (
            (
                "Check whether the business has a free space for a STAY between an arrival and a departure "
                "time. This business books by the unit (the configured resource type) for a continuous period "
                "that MAY SPAN SEVERAL DAYS — or a SAME-DAY visit (arrival and departure on the same date), "
                "which is booked exactly the same way with both times on one date. Call this BEFORE offering "
                "or agreeing any dates — never guess. Returns whether the requested span fits and how many "
                "nights; if it's full it returns the nearest opening that fits the SAME length. Do not hold "
                "that opening — the customer has not agreed to it — reserve it with offer_stay and ask."
            )
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "arrival": {"type": "string",
                            "description": "Drop-off / check-in, 'YYYY-MM-DDTHH:MM' (24-hour)."},
                "departure": {"type": "string",
                              "description": "Collection / check-out, 'YYYY-MM-DDTHH:MM', after arrival."},
                "resource": {"type": "string",
                             "description": (
                                 "The unit type, ONLY when the business lists resource types (use its configured name)."
                             )},
                "quantity": {"type": "integer",
                             "description": (
                                 "How many units are needed, ONLY when more than one (the quantity requested)."
                             )},
                "party_size": {"type": "integer",
                               "description": "Number of people, ONLY when this business books "
                                              "by party size (guests/covers)."},
            },
            "required": ["arrival", "departure"],
            "additionalProperties": False,
        },
    },
    {
        "name": "hold_booking",
        "description": (
            (
                "Place ONE provisional STAY hold from arrival to departure — only after check_availability "
                "showed the span fits. Same-day visits hold the same way: arrival and departure on one date. "
                "When the customer asks to book more than one SEPARATE visit (a same-day visit AND a separate "
                "multi-day stay), call this once for EACH visit — up to three holds per email; never hold the "
                "same visit twice. EVERY visit that puts the subject of the booking on site is a diary booking "
                "— a FREE visit (a configured introductory appointment) holds exactly like a paid one; "
                "agreeing a visit in words without holding it leaves the business with no record of it. The "
                "hold really reserves a space for the whole stay; a human confirms or releases it later. "
                "Returns the booking id + reference, or an error explaining why it can't be held."
            )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "arrival": {"type": "string",
                            "description": "Drop-off / check-in, 'YYYY-MM-DDTHH:MM'."},
                "departure": {"type": "string",
                              "description": "Collection / check-out, 'YYYY-MM-DDTHH:MM'."},
                "summary": {"type": "string",
                            "description": "One line identifying the stay from the customer's request."},
                "resource": {"type": "string",
                             "description": (
                                 "The unit type, ONLY when the business lists resource types (use its configured name)."
                             )},
                "quantity": {"type": "integer",
                             "description": "How many units are needed, ONLY when more than one."},
                "party_size": {"type": "integer",
                               "description": "Number of people, ONLY when this business books "
                                              "by party size."},
                "delivery_required": {"type": "boolean",
                                      "description": "ONLY when the business offers delivery and "
                                                     "the customer wants it (e.g. hire delivered)."},
                "delivery_address": {"type": "string",
                                     "description": "The delivery address, when delivery_required."},
                "details": {"type": "object",
                            "description": (
                                "Structured facts the business needs (the subject, resource type and relevant notes)."
                            )},
            },
            "required": ["arrival", "departure", "summary"],
            "additionalProperties": False,
        },
    },
    {
        # Not strict: `details` is free-form jsonb, like hold_booking.
        # The stay sibling of offer_slots (docs/modules/module-smart-booking-offers.md,
        # docs/plan-alternatives-and-offers.md). Until 2026-09-22 a stay diary
        # had only hold_booking, so when a customer's dates were full the agent
        # either booked them into an alternative they had never seen — approving
        # the reply CONFIRMS a proposed hold — or reserved nothing at all and
        # hoped the space survived the exchange. A driving instructor has had
        # exclusive time-limited offers since July; a stays business had neither.
        "name": "offer_stay",
        "description": (
            "Reserve up to TWO alternative stays as exclusive OFFERS for this customer "
            "while they decide. You MUST call this whenever the dates the customer asked "
            "for are FULL and your reply names other dates — offering a date without "
            "reserving it means the space can be gone before they answer, and holding it "
            "with hold_booking would BOOK them into dates they have not agreed to. The "
            "offer is the hold: each space is theirs until its own expires_at deadline, then it "
            "is released automatically. Your reply MUST include deadline_line verbatim "
            "and ask which suits. Pass only spans check_availability "
            "returned as free."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stays": {
                    "type": "array",
                    "description": "1 or 2 alternative spans, exactly as check_availability "
                                   "returned them (e.g. its nearest_arrival/nearest_departure).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "arrival": {"type": "string",
                                        "description": "'YYYY-MM-DDTHH:MM' drop-off."},
                            "departure": {"type": "string",
                                          "description": "'YYYY-MM-DDTHH:MM' collection."},
                        },
                        "required": ["arrival", "departure"],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string",
                            "description": "One line naming who the stay is for."},
                "details": {"type": "object",
                            "description": "Structured facts the business needs, as for "
                                           "hold_booking."},
            },
            "required": ["stays", "summary"],
            "additionalProperties": False,
        },
    },
]

MAX_OFFER_STAYS = 2      # "nearest before and after" — never a menu


def _appointment_tools() -> list[dict]:
    """The service diary's two booking tools, renamed for a STAYS diary's
    appointments (Kris 2026-09-13: an introductory visit beside the stays business's spaces)
    — the same schema, `service` required, and a description that says which
    bookings they are for. They run the service path against the
    appointment's own pool; the stay tools keep their names."""
    import copy
    out = []
    for src, name, verb in (("check_availability", "check_appointment_availability", "check"),
                            ("hold_booking", "hold_appointment", "hold")):
        tool = copy.deepcopy(next(t for t in SERVICE_TOOLS if t["name"] == src))
        tool["name"] = name
        tool["description"] = (
            f"Only for an APPOINTMENT service on this stay diary — a timed visit the customer "
            f"comes to (a configured service), listed under APPOINTMENTS in the diary "
            f"line; never for a stay or a same-day visit (those use the stay tools). Pass "
            f"`service`. To {verb}: " + tool["description"])
        schema = tool.get("input_schema") or {}
        if isinstance(schema, dict) and "properties" in schema:
            req = [r for r in (schema.get("required") or []) if r != "service"]
            schema["required"] = req + ["service"]
        out.append(tool)
    return out


APPOINTMENT_TOOLS = _appointment_tools()


def stay_diary_line(cfg: dict | None, service_types: list[dict] | None = None) -> str:
    """The one-sentence diary shape for a STAY tenant (capacity = concurrent
    spaces) — plus its APPOINTMENTS, when it has any (Kris 2026-09-13)."""
    policy = booking_policy.compile_policy(cfg)
    if not policy["ready"]:
        return policy["message"]

    from app import diary_config, services as _services
    merged = _merged(cfg)
    days = ", ".join(t.strip().title() for t in str(merged["working_days"]).split(",") if t.strip())
    cap = int(merged["capacity_per_window"] or 0)
    o, c = _as_time(merged["open_time"]), _as_time(merged["close_time"])
    line = (f"Bookings are STAYS — a continuous period from an arrival to a departure that may "
            f"span several days, or a SAME-DAY visit (arrival and departure on one date) "
            f"booked the same way. {cap} space(s) total; a stay "
            f"fits whenever fewer than {cap} are "
            f"occupied for every moment it covers. Drop-off/collection during {o.hour:02d}:"
            f"{o.minute:02d}-{c.hour:02d}:{c.minute:02d}; arrival days {days} (a stay may run "
            f"through closed days/nights).")
    appts = _services.clean_service_types(service_types) if service_types else []
    if appts:
        parts = []
        for svc in appts:
            pool = diary_config.find_resource((cfg or {}).get("resource_types"),
                                              svc.get("resource") or svc["name"])
            n = int(pool["count"]) if pool else 1
            price = f", £{float(svc['price']):.2f}" if svc.get("price") is not None else ""
            ruled = _services.rules_label(svc)
            parts.append(f"{svc['name']} — {int(svc['minutes'])} min, {n} at a time{price}"
                         + (f", booked {ruled}" if ruled else ""))
        line += (" APPOINTMENTS, separate from the spaces (a timed visit the customer comes "
                 "to, within opening hours): " + "; ".join(parts) + ". Book these ONLY with "
                 "check_appointment_availability and hold_appointment (pass `service`); the "
                 "stay tools are for stays and same-day visits, never for an appointment.")
        if any(_services.rules_label(s) for s in appts):
            line += " " + _services.RULES_PROMPT_LINE
        fd = diary_config.front_desk_of(cfg)
        if fd["enabled"]:
            line += (f" FRONT DESK RULE: one person handles drop-offs, collections and appointments. "
                     f"An appointment is never available within {fd['minutes']} minutes after a stay's "
                     "arrival or departure (the appointment tools already leave those starts out), and "
                     "a stay's arrival or departure time is refused when it falls inside an appointment "
                     "(error front_desk) — offer the clear time the error names.")
    return line


def manual_stay_add(tenant_id: str, arrival: datetime, departure: datetime,
                    summary: str, details: dict | None = None,
                    contact: str | None = None,
                    client=None, recurrence_id: str | None = None) -> tuple[dict | None, str | None]:
    """Manual 'Add booking' on a STAY diary (dashboard /app/diary/add — Julius
    2026-09-03: the form offered only 'Whole day' against an arrival→departure
    diary, the same windows-shape gap the portal had). Human-entered, so the
    notice/horizon/check-in rules are skipped (their diary, their call) —
    capacity still runs against live occupancy with the same span math as the
    agent's hold. Returns (row, None) or (None, 'bad_span' | 'full')."""
    readiness, _ = make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        return None, blocked["error"]

    if departure <= arrival:
        return None, "bad_span"
    cfg_row = db.get_scheduling_config(tenant_id, client=client) or {}
    merged = _merged(cfg_row)
    capacity = int(merged["capacity_per_window"] or 0)
    booked = db.list_active_bookings(
        tenant_id, (arrival.date() - timedelta(days=365)).isoformat(),
        departure.date().isoformat(), client=client)
    spans = _pool_spans(booked, cfg_row, None, exclude_typed=True)
    buf = int(merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    offer = slotgrid.stay_offer(spans, arrival, departure, capacity,
                                buffer_blocks=buffer_blocks)
    if not offer["fits"]:
        return None, "full"
    row = db.insert_stay_booking(
        tenant_id, arrival.date().isoformat(), slotgrid.block_at(arrival.time()),
        slotgrid.datetime_to_abs(departure) - slotgrid.datetime_to_abs(arrival),
        summary, details=details, contact=contact, source="manual",
        status="confirmed", recurrence_id=recurrence_id, client=client)
    return row, None


def booked_service(args: dict | None) -> str:
    """What this call is BOOKING, for the gates that care which service it is.

    A slots/service diary passes `service` outright. A STAY does not — its
    hold_booking supplies arrival, departure and details without a service argument — so a
    ladder gate reading `args["service"]` saw an empty string and, because a
    blank matches every gate, fired for whatever was configured. Live
    2026-09-22: an "Overnight stays need a trial night" rule would have blocked the
    day visit the same subject already attends.

    The stay's service is in its details, under the tenant's own label, which
    the model fills on every hold. Matched loosely because it writes the key
    however it likes (the 2026-08-31 company_name lesson)."""
    args = args or {}
    direct = str(args.get("service") or "").strip()
    if direct:
        return direct
    from app import intake as _intake
    details = args.get("details")
    if isinstance(details, dict):
        for k, v in details.items():
            if _intake._norm(k) in ("service type", "service"):
                return str(v or "").strip()
    return ""


def gate_date(args: dict | None):
    """The booking date a dependant's expiry must cover: a hold's `date`, a
    stay's `arrival`, or the EARLIEST of an offer's `slots[].date` — an offer
    is a real reservation and slipped past a lapsed certificate when only the
    first two were read (live 2026-09-04)."""
    args = args or {}
    direct = args.get("date") or args.get("arrival")
    if direct:
        return direct
    dates = []
    for slot in args.get("slots") or []:
        d = (slot or {}).get("date") if isinstance(slot, dict) else None
        if d:
            dates.append(str(d)[:10])
    return min(dates) if dates else None


def gate_until(args: dict | None):
    """The last day a dependant's expiry must reach: a stay's `departure`. None
    for anything that happens on one day — the arrival check is the whole check
    there. A stay has to be covered to the end, not just on the way in
    (Kris 2026-09-22: certificates lapsing mid-stay passed the gate)."""
    args = args or {}
    if args.get("arrival") and args.get("departure"):
        return args["departure"]
    return None


def make_executor(tenant_id: str, contact: str | None = None, thread_id: str | None = None,
                  source: str = "email", now: datetime | None = None,
                  fleet: bool = False, mailbox_id: str | None = None,
                  routing: bool = False, intake_fields: list | None = None,
                  stay: bool = False, service_types: list | None = None,
                  groups: list | None = None, existing_bookings: list | None = None,
                  grounding=None, owner_directed: bool = False, expected_offer_id: str | None = None,
                  booking_profile: booking_policy.BookingProfile | None = None):
    """The tool executor for one email: (name, args) -> JSON-able dict.

    `owner_directed`: the owner asked for this reply (a redraft with their
    words) — an out-of-hours service set to "ask me first" may be offered.

    `grounding` (app/grounding.Grounding, docs/plan-grounded-booking.md — only
    when scheduling_config.grounded_dates is on): the dates this draft is
    allowed to act on, with provenance. A hold/offer/move for a date outside it
    is refused `missing_info` before anything is placed; a hold for a date
    check_availability has not returned free THIS draft is refused
    `check_first`. Both are enforced here — the prompt only explains them.
    None (the default, every tenant until they opt in) changes nothing.

    Returns (execute, state). state['bookings'] collects the rows actually placed,
    so the pipeline can stamp bookings.email_log_id once the draft row exists and
    confirm/release the hold on approve/skip/auto-send. The per-email hold limits
    are enforced HERE, in code — the prompt merely explains them. Stay diaries
    allow up to STAY_HOLDS_MAX non-overlapping visits (live e2e 2026-09-02),
    separately from appointments. Office appointments also allow distinct subjects
    or job addresses, up to SUBJECT_HOLDS_MAX; repeating the same visit is refused.

    `intake_fields` (office/windows only — the tenant's scheduling-intake schema): the
    HARD gate that an office hold carries every REQUIRED booking detail. If any is
    missing from the hold's `details`, the hold is refused with a 'missing_info' error
    so the agent asks the customer instead — the prompt explains it, this enforces it.

    `fleet=True` (the fleet module — D3: supersedes windows capacity) answers both
    tools from the fleet resource pool instead of the windows config; `mailbox_id` feeds
    branch resolution (D4 — the receiving mailbox's default branch). The default
    path is byte-identical to before, so windows-mode tenants are untouched.

    `routing=True` (the routing module — route-planning D2, only ever alongside
    fleet) stamps the captured collection/delivery postcodes + customer reference
    onto a fresh hold via a follow-up update — the atomic capacity RPC is never
    touched; these fields carry location, not capacity.

    A logistics tenant runs office and transport as TWO SEPARATE AGENTS now (the base
    agent for the office diary, the TMS specialist for transport — see
    docs/modules/module-agent-handoff.md); each builds its own single-purpose executor here,
    so this executor only ever does ONE capacity model (windows OR fleet), never both.

    `existing_bookings` (the contact's live diary rows, db.bookings_for_contact) feeds
    the live-diary duplicate gate below — a hold_booking for a visit the diary already
    holds for this contact (proposed or confirmed) is refused `already_booked` with its
    reference. The pipeline passes the list it already read for the prompt; a caller
    that passes None gets a best-effort read at the first hold."""
    state: dict = {"bookings": [], "grounding": grounding, "ooh_withheld": []}
    if booking_profile is not None:
        state["booking_profile"] = booking_profile

    def _ooh_allowed(svc) -> bool:
        """May this reply offer the service's out-of-hours times? (D1 stage 2:
        the research — owner-operators decide evenings job by job.)"""
        o = services.out_of_hours(svc)
        if not o:
            return False
        if o["offer"] == "auto" or owner_directed:
            return True
        if o["offer"] == "existing":
            return any((b or {}).get("status") in ("confirmed", "completed")
                       for b in existing_bookings or [])
        return False

    def _ooh_note(svc) -> str:
        o = services.out_of_hours(svc) or {}
        return ("Slots marked out_of_hours are the business's evening/weekend/bank-holiday "
                f"cover at {services.ooh_price_label(o)}. When you offer one, "
                "say that full price in the same sentence. "
                + (f"The business's terms are: {o['terms']}. " if o.get("terms") else "")
                + "Offer the soonest "
                "standard-hours time beside it at the normal price, so the customer chooses. "
                + ("Use the exact time returned by the diary." if services.timing(svc) == "time"
                   else "Never an exact arrival time — a window."))
    fl: dict = {}   # lazy per-email fleet context (config, pool, factors)
    oc: dict = {}   # lazy per-email office context (the scheduling_config row)
    # Dependants (docs/modules/module-requirement-groups.md): the tenant's groups, and
    # the customer's own instances, read once per email. `groups` empty means
    # nothing here runs — the zero-config tenant is byte-identical.
    from app import requirement_groups as rgroups
    groups = rgroups.clean_groups(groups)
    dep: dict = {}
    rec: dict = {}

    def _record_ctx() -> dict:
        """The customer's CRM record, read once per email: {contact, contact_id}
        (None/None for a first-time customer or no sender). Shared by the
        dependant gate and the remembered-requirement gate (app/intake_memory)."""
        if not rec:
            c = None
            try:
                c = db.crm_contact_by_email(tenant_id, contact) if contact else None
            except Exception:
                c = None
            rec["contact"] = c
            rec["contact_id"] = (c or {}).get("id")
        return rec

    def _record_defs() -> list[dict]:
        """The tenant's contact field definitions, read once — a remembered date's
        validity lives there. Best-effort: none means the date on record is the
        expiry itself."""
        if "defs" not in rec:
            try:
                rec["defs"] = db.crm_list_field_definitions(tenant_id, entity="contact")
            except Exception:
                rec["defs"] = []
        return rec["defs"]

    def _dep_ctx() -> dict:
        if not dep:
            cid = _record_ctx()["contact_id"]
            dep["contact_id"] = cid
            dep["instances"] = db.list_requirement_instances(tenant_id, cid) if cid else []
            dep["proof_keys"] = {}
            try:
                docs = db.list_documents(tenant_id, contact_email=contact,
                                         contact_id=cid, strict=True) if (contact or cid) else []
                for d in docs or []:
                    if d.get("instance_id"):
                        dep["proof_keys"].setdefault(d["instance_id"], set()).update(
                            d.get("requirement_keys") or [])
            except Exception:   # noqa: BLE001
                logging.getLogger(__name__).warning("proof lookup failed (tenant=%s)", tenant_id, exc_info=True)
                dep["proof_unavailable"] = True
        return dep

    def _subject_gate(args: dict) -> tuple[list[dict], dict | None]:
        """(resolved subjects, refusal). A tenant with a group needs at least one
        subject on every booking; each subject is checked against ITS record
        (prefilled) plus what this call states, per instance — Murphy's lapsed
        certificate blocks Murphy, Tilly is unaffected."""
        subs, err = rgroups.subjects_from_args(args, groups)
        if err:
            return [], {"error": "missing_info", "missing": ["subjects"], "message": err}
        if not subs:
            kinds = " / ".join(g["label"].lower() for g in groups)
            return [], {"error": "missing_info", "missing": ["subjects"],
                        "message": (f"Which {kinds} is this booking for? Pass `subjects` — one "
                                    "entry per " + kinds + " with its name and fields — before "
                                    "booking. If the customer named none, ask.")}
        ctx = _dep_ctx()
        missing: list[str] = []
        teach: list[str] = []
        resolved = []
        for s in subs:
            g = s["group"]
            on_record = next((i for i in ctx["instances"]
                              if (i.get("group_id") == g.get("id") or not g.get("id"))
                              and str(i.get("name") or "").strip().lower() == s["name"].lower()), None)
            if on_record is None:
                # A near miss (slice 1b nit, live 2026-09-05): "Isla Deane" for the
                # record's "Isla" — the first name alone, when it names exactly one
                # dependant of this kind, IS that dependant, not a new one.
                first = s["name"].lower().split()[0] if s["name"].strip() else ""
                cands = [i for i in ctx["instances"]
                         if (i.get("group_id") == g.get("id") or not g.get("id"))
                         and first and str(i.get("name") or "").strip().lower().split()[:1] == [first]]
                if len(cands) == 1:
                    on_record = cands[0]
                    s = {**s, "name": on_record.get("name") or s["name"]}
            # A date field the model restates in WORDS never replaces a real
            # date on record (K1 take 2026-09-22: the card had just set Alfie's
            # certificates to 2027-09-22, the model passed "Updated — alfie-certificates-
            # 2026.pdf received", the merge let the words win and the gate
            # refused "not a date" — asking the customer for a date already on
            # file). A new DATE still wins; words are dropped for the record's.
            stated = dict(s["data"])
            rec_data = (on_record or {}).get("data") or {}
            for f in g.get("fields") or []:
                k = f.get("key")
                if f.get("type") == "date" and k in stated \
                        and rgroups.parse_date(stated[k]) is None \
                        and rgroups.parse_date(rec_data.get(k)) is not None:
                    stated.pop(k)
            data, meta, _changed = rgroups.merge(
                (on_record or {}).get("data"), (on_record or {}).get("field_meta"), stated, g,
                source="extracted", email_log_id=None)
            # A proof field needs the certificate on this dependant's record.
            # A dependant we have never seen has none by definition, so a first
            # booking waits for the card — which is the point of the rule.
            has_proof = (None if ctx.get("proof_unavailable") else
                         ctx.get("proof_keys", {}).get((on_record or {}).get("id"), set()))
            for gap in rgroups.missing_for(g, data, gate_date(args), has_proof,
                                           until=gate_until(args)):
                missing.append(f"{s['name']}: {gap}")
                given = (s.get("placeholders") or {}).get(gap)
                if given:
                    # Run #7 (classes business): the parent wrote "Finn has no medical
                    # conditions", the model keyed "None", the gate refused it as a
                    # placeholder and the reply asked the parent to say it again.
                    # The refusal now says how to record a genuine none.
                    teach.append(
                        f"'{given}' was given for {s['name']}'s {gap} — a bare '{given.lower()}' "
                        "reads as unknown, not as an answer. If the customer said there are "
                        f"none, write their answer in words without a leading placeholder "
                        f"('No {gap.lower()} — per the customer'); ask only if they never said.")
            resolved.append({**s, "data": data, "meta": meta, "existing": on_record})
        if not missing:
            # Progress ladders (src/app/ladders.py): a service that needs at least a
            # level refuses a subject below it — the agent explains, never books.
            from app import ladders as _ladders
            for r in resolved:
                ref = _ladders.prerequisite_refusal(r["group"], r["data"], r["name"],
                                                    booked_service(args))
                if ref:
                    return [], ref
        if missing:
            by_name: dict = {}
            for m in missing:
                n, _, what = m.partition(": ")
                by_name.setdefault(n, []).append(what)
            said = "; ".join(f"Still needed for {n}: {', '.join(w)}" for n, w in by_name.items())
            return [], {"error": "missing_info", "missing": missing,
                        "message": (said + ". " + ("".join(x + " " for x in teach))
                                    + "Ask the customer for exactly these, for that "
                                    + " / ".join(g["label"].lower() for g in groups)
                                    + " — key each under `subjects[].fields` EXACTLY by the label "
                                    "shown. A placeholder is not a value. This is a normal "
                                    "gathering step, not a fault.")}
        if ctx.get("proof_unavailable") and any(
                f.get("proof") for r in resolved for f in r["group"].get("fields") or []):
            from app.booking_errors import VerificationUnavailable
            raise VerificationUnavailable("the required documents")
        return resolved, None

    def _write_subjects(resolved: list[dict], rows: list[dict]) -> list[str]:
        """After a hold/offer landed: the instances (created or updated on the
        customer's record) and the booking links. Best-effort: the hold stands
        even if the record write fails."""
        ctx = _dep_ctx()
        names = [s["name"] for s in resolved]
        if not ctx.get("contact_id"):
            return names
        ids: list[str] = []
        from app import dependants_extract as _dx
        for s in resolved:
            g = s["group"]
            if not g.get("id"):
                continue
            row = db.upsert_requirement_instance(tenant_id, g["id"], ctx["contact_id"], s["name"],
                                                 s["data"], s.get("meta") or {})
            if row and row.get("id"):
                ids.append(row["id"])
                # The enquiry may have queued "a new subject: Biscuit — add?" on the
                # card; the booking has just made Biscuit — the chip is answered.
                _dx.absorb_new_chips(tenant_id, ctx["contact_id"], g, row)
        try:
            for b in rows:
                if b.get("id"):
                    db.link_booking_subjects(tenant_id, b["id"], ids)
        except Exception:
            pass
        dep.pop("instances", None); dep.clear()   # re-read next time
        return names

    def _fleet_ctx() -> dict:
        if not fl:
            fl["cfg"] = db.get_scheduling_config(tenant_id)
            fl["branches"] = db.list_branches(tenant_id)
            fl["vehicles"] = db.list_vehicles(tenant_id, active_only=True)
            fl["factors"] = pallet_factors(db.list_pallet_types(tenant_id))
            fl["default_branch"] = (
                db.get_mailbox_default_branch(mailbox_id) if mailbox_id else None
            )
        return fl

    def _fleet_pool(args: dict) -> tuple[list | None, str | None, dict | None]:
        """Resolve (fleet resource pool, branch_id) for one tool call, or an error dict.
        The chain (D4): explicit `branch` arg → mailbox default → with ≤1 branch
        the whole fleet → otherwise the agent must ask the customer."""
        f = _fleet_ctx()
        branch_id, err, names = resolve_branch(f["branches"], args.get("branch"),
                                               f["default_branch"])
        if err == "unknown_branch":
            return None, None, {
                "error": "unknown_branch",
                "branches": names,
                "message": f"No branch called '{args.get('branch')}'. The branches are: "
                           f"{', '.join(names)}. Use one of those names, or omit `branch` "
                           "and ask the customer which one they need.",
            }
        if err == "branch_required":
            return None, None, {
                "error": "branch_required",
                "branches": names,
                "message": "This business books per branch and the email doesn't say "
                           f"which. Ask the customer which branch they need — the options "
                           f"are: {', '.join(names)}. Do not promise capacity until the "
                           "branch is known.",
            }
        pool = [v for v in f["vehicles"]
                if branch_id is None or v.get("branch_id") == branch_id]
        if not pool:
            return None, None, {
                "error": "not_configured",
                "message": "No vehicles are configured for this diary yet, so capacity "
                           "is unknown. Do not promise or decline any date — say a "
                           "colleague will confirm the booking.",
            }
        return pool, branch_id, None

    def _fleet_load(args: dict) -> tuple[float | None, float | None, list, dict | None]:
        """Parse (spaces, weight_kg, assumed_full_types) from tool args, or an error."""
        spaces, assumed = None, []
        if args.get("pallets") is not None:
            try:
                spaces, assumed = order_spaces(_fleet_ctx()["factors"], args["pallets"])
            except (TypeError, ValueError, KeyError):
                return None, None, [], {
                    "error": "invalid_pallets",
                    "message": "pallets must be a list of {type, qty} with positive "
                               "quantities, e.g. [{\"type\": \"full\", \"qty\": 6}].",
                }
        weight = None
        if args.get("weight_kg") is not None:
            try:
                weight = float(args["weight_kg"])
            except (TypeError, ValueError):
                return None, None, [], {
                    "error": "invalid_weight",
                    "message": "weight_kg must be a number — the total load weight in kg.",
                }
            if weight <= 0:
                weight = None
        return spaces, weight, assumed, None

    def _note_assumed(out: dict, assumed: list) -> dict:
        if assumed:
            out["assumed_full_pallets"] = assumed
            out["note"] = ("Unrecognised pallet type(s) "
                           + ", ".join(f"'{a}'" for a in assumed)
                           + " were counted as FULL pallets (1 space each). State this "
                             "assumption in your reply.")
        return out

    def _fleet_check(args: dict) -> dict:
        try:
            start, end = _as_date(args.get("date_from")), _as_date(args.get("date_to"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "Dates must be YYYY-MM-DD."}
        pool, _branch_id, err = _fleet_pool(args)
        if err:
            return err
        spaces, weight, assumed, lerr = _fleet_load(args)
        if lerr:
            return lerr
        booked = db.list_active_bookings(tenant_id, start.isoformat(), end.isoformat())
        slots = fleet_availability(_fleet_ctx()["cfg"], pool, booked, start, end,
                                   now or datetime.now(DIARY_TZ),
                                   spaces=spaces, weight_kg=weight)
        out = {"available": slots}
        if spaces is not None:
            out["spaces_requested"] = spaces
        if not slots:
            biggest = max((float(v["capacity_spaces"]) for v in pool), default=0.0)
            earliest = earliest_bookable_day(_fleet_ctx()["cfg"],
                                             now or datetime.now(DIARY_TZ))
            if spaces is not None and spaces > biggest:
                out["message"] = (
                    f"This load needs {fmt_spaces(spaces)} pallet spaces — more than the "
                    "business can ever take as ONE booking. Do not offer any date: say a "
                    "colleague will be in touch to arrange it, and NEVER promise to split "
                    "the order across days or deliveries."
                )
            elif earliest and end < earliest:
                # The range is empty because of the notice rule / closed days,
                # NOT because capacity is used — the reply must not say "full".
                notice = _norm(_fleet_cfg(_fleet_ctx()["cfg"]))["min_notice_hours"]
                out["earliest_bookable_date"] = earliest.isoformat()
                out["message"] = (
                    f"Nothing that soon can be booked: the diary needs {notice} hours' "
                    f"notice, so the earliest possible date is {earliest.isoformat()}. "
                    "This is NOT 'fully booked' — tell the customer it is too short "
                    "notice and check/offer dates from "
                    f"{earliest.isoformat()} in THIS reply."
                )
            else:
                out["message"] = ("No bookable dates for that load in this range. Call "
                                  "this tool again for the FOLLOWING days now and offer "
                                  "the customer the nearest dates that fit in THIS reply "
                                  "— never promise to check and get back to them.")
        return _note_assumed(out, assumed)

    def _fleet_hold(args: dict) -> dict:
        pool, branch_id, err = _fleet_pool(args)
        if err:
            return err
        if not args.get("pallets"):
            return {"error": "pallets_required",
                    "message": "A hold needs the customer's pallet counts. If the email "
                               "doesn't say how many pallets (and of what type), ask the "
                               "customer instead of booking."}
        spaces, weight, assumed, lerr = _fleet_load(args)
        if lerr:
            return lerr
        try:
            day = _as_date(args.get("date"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "The date must be YYYY-MM-DD."}
        derr = hold_error(_fleet_cfg(_fleet_ctx()["cfg"]), day, "day", now or datetime.now(DIARY_TZ))
        if derr:
            return {"error": derr, "message": _HOLD_ERROR_MESSAGES[derr]}
        row = db.hold_fleet_booking(
            tenant_id, day.isoformat(),
            (args.get("summary") or "").strip() or "Booking request",
            spaces, weight_kg=weight, branch_id=branch_id,
            details=args.get("details"), contact=contact,
            source=source, thread_id=thread_id,
        )
        if not row:
            return {"error": "full",
                    "message": "That load cannot be taken on that date — the capacity "
                               "just isn't there as one booking. Call check_availability "
                               "with the load and offer the nearest dates that fit."}
        if routing:
            extras = routing_fields(args)
            if extras:
                db.set_booking_routing(row["id"], tenant_id, **extras)
                row.update(extras)
        state["bookings"].append(row)
        # D5: the fleet resource the RPC picked is internal — never in the tool result.
        # booking_ref IS for the customer: the business-issued reference they
        # quote in every follow-up (normal-use fix, 2026-06-11).
        return _note_assumed({"held": True, "booking_id": row["id"],
                              "booking_ref": row.get("booking_ref"),
                              "date": row["booking_date"], "spaces": spaces,
                              "status": "proposed"}, assumed)

    def _horizon_note(out: dict, start, end) -> dict:
        """A range past the booking horizon says so IN the tool result (run #8,
        2026-09-05: an empty Monday 100 days out read as "fully booked", and the
        customer was put on a waiting list for it). Whole range beyond → the
        message replaces the full-day one; the tail beyond → a note."""
        try:
            c = _norm(_office_cfg())
            today = (now or datetime.now(DIARY_TZ)).date()
            last = today + timedelta(days=int(c.get("horizon_days") or 0))
            s, e = _as_date(start), _as_date(end)
        except (TypeError, ValueError, KeyError):
            return out
        if s > last:
            opens = s - timedelta(days=int(c.get("horizon_days") or 0))
            out["beyond_horizon"] = True
            out["horizon_days"] = int(c.get("horizon_days") or 0)
            out["opens_on"] = opens.isoformat()
            out["message"] = (
                f"Those dates are BEYOND the business's {c.get('horizon_days')}-day booking horizon — "
                f"the diary is not open that far ahead yet; bookings for {s.isoformat()} open on "
                f"{opens.isoformat()}. They are NOT 'fully booked' and there is nothing to hold or "
                "list: tell the customer plainly when the diary opens for that date and invite them "
                "to get in touch then. Do not offer the waiting list for it and do not call the day full.")
        elif e > last:
            out["horizon_note"] = (
                f"Dates after {last.isoformat()} are beyond the {c.get('horizon_days')}-day booking "
                "horizon and are not open yet — that is why nothing shows past it; say so if it matters.")
        return out

    def _add_notice_note(out: dict, date_from, svc: dict | None = None) -> dict:
        """When the requested range starts inside the minimum-notice window, say so
        IN the tool result — the model otherwise sees silently-thinner availability
        and invents its own reason ('isn't available in the diary', 2026-07-26 e2e).
        Best-effort: a config hiccup never breaks the availability answer."""
        try:
            c = _norm(_office_cfg())
            _now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
            hours = service_notice(c, svc)
            cutoff = _now + timedelta(hours=hours)
            if svc and past_same_day_cutoff(svc, _now) and _as_date(date_from) <= _now.date():
                out["notice_rule"] = (
                    f"{svc.get('name')} is done the same day only when asked before "
                    f"{services.rules(svc)['same_day_until']} — it's past that now, so today "
                    "is not offered. Tell the customer it's too late to get to them today and "
                    "offer the soonest time `available` shows; never say full.")
            elif _as_date(date_from) < cutoff.date() + timedelta(days=1):
                out["notice_rule"] = (
                    f"This {'service' if svc else 'diary'} requires {hours} hours' notice: times "
                    f"before {cutoff.strftime('%A %d %B %H:%M')} are excluded from "
                    "`available`. If you decline a requested time for this reason, tell "
                    "the customer it's too soon for you to get to them then — never quote "
                    "the hours of notice, and never say the diary is full or the day is "
                    "unavailable.")
        except Exception:
            pass
        return out

    def _windows_check(args: dict) -> dict:
        try:
            slots = check_availability(tenant_id, args.get("date_from"),
                                       args.get("date_to"), now=now)
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "Dates must be YYYY-MM-DD."}
        out = {"available": slots}
        # A multi-day range is a period ask — attach the per-day counts so the
        # reply can say "Tuesday has 4 slots, Wednesday 5" without inventing
        # numbers (the counts come from THIS tool, say-vs-do).
        if slots and _as_date(args.get("date_to")) > _as_date(args.get("date_from")):
            out["day_summary"] = day_summary(slots)
        if not slots:
            out["message"] = ("No bookable windows in that range. Try a wider or "
                              "later range within the booking horizon.")
            try:
                out.update(why_none(_office_cfg(), _as_date(args.get("date_from")),
                                    _as_date(args.get("date_to")), now))
            except Exception:  # noqa: BLE001 — the reason is extra, never the answer
                pass
        return _horizon_note(_add_notice_note(out, args.get("date_from")),
                             args.get("date_from"), args.get("date_to"))

    def _intake_gated(hold_fn, args: dict) -> dict:
        """The intake gate, and — when the thing stopping a customer booking is detail
        the customer has not sent yet — a reservation while it is gathered
        (Kris 2026-09-22, K2 rehearsal: "Agent should have held that slot while
        gathering this info", and then: "reserve on missing details only").

        The dates here have already passed grounding (_execute_inner checks
        before anything is held), so a reservation is never on a date nobody
        chose. It is the same `offered` hold an alternative gets — exclusive,
        shown with its release time, gone at expiry — and it becomes the
        booking in place when the details arrive and the hold is called again
        (_accept_offered / _accept_offered_stay). A RULE that fails — a lapsed
        certificate, a level not yet reached, a date the customer never gave —
        reserves nothing: that is not fixed by a quick reply, and a slot held
        for a subject that cannot come keeps it from someone who can. If verification
        itself is unavailable, the separate owner-review path may retain or place
        an offered reservation after capacity/rule checks; it confirms nothing
        and permits no customer reply."""
        from app.booking_errors import VerificationUnavailable
        try:
            out = _intake_checked(hold_fn, args)
        except VerificationUnavailable as exc:
            state["verification_unavailable"] = str(exc)
            # This is an owner-review hold, never a customer-detail request.
            # The placers still check capacity and scheduling rules; no place
            # is claimed if those checks fail as well.
            try:
                if expected_offer_id:
                    # Keep the existing offer for owner review; never replace it.
                    held = {}
                elif hold_fn is _stay_hold and args.get("arrival") and args.get("departure"):
                    held = _stay_offer_place({**args, "stays": [{
                        "arrival": args["arrival"], "departure": args["departure"]}]})
                elif hold_fn is _service_hold and args.get("date") and args.get("window"):
                    held = _service_offer({**args, "slots": [{
                        "date": args["date"], "window": args["window"]}]})
                elif hold_fn is _windows_hold and args.get("date") and args.get("window"):
                    held = _windows_offer({**args, "slots": [{
                        "date": args["date"], "window": args["window"]}]})
                else:
                    held = {}
            except Exception:
                held = {}
                logging.getLogger(__name__).warning("owner-review reservation unavailable (tenant=%s)",
                                                    tenant_id, exc_info=True)
            return {"error": "verification_unavailable", "owner_review": True,
                    "reserved": held.get("status") == "offered", "held": False,
                    "hold_until": held.get("hold_until"),
                    "message": str(exc) + " Do not write a confirmation or a customer reply."}
        if expected_offer_id or not _only_details_missing(out):
            return out
        # The business's choice (Diary settings -> When it's full): some hold
        # nothing until every detail is in. Off, the refusal goes back exactly
        # as it is — it never mentions a reservation, so the agent is told none.
        from app import courses as _courses
        try:
            if not _courses.settings_of(_office_cfg())["hold_while_gathering"]:
                return out
        except Exception:  # noqa: BLE001 — settings unreadable: ask, hold nothing
            return out
        prior = _gathering_match(args, stay_kind=hold_fn is _stay_hold)
        if prior is not None:
            # The same request again in this email — the dependant named, a detail
            # added, still one missing (the Ellen test: the retry naming Pepper was
            # refused "already reserved"). It IS that reservation: say so again.
            return {**out, "reserved": True, "hold_until": prior.get("_hold_until"),
                    "hold_until_phrase": prior.get("_phrase") or "",
                    "deadline_line": prior.get("_deadline_line") or "",
                    "booking_ref": prior.get("booking_ref"),
                    "message": (f"{out.get('message', '').rstrip()} This is the SAME request you "
                                f"already reserved in this email ({prior.get('booking_ref')}) — it "
                                f"stays held until {prior.get('_phrase')} while they send these. "
                                "Nothing new was reserved.")}
        if hold_fn is _stay_hold and args.get("arrival") and args.get("departure"):
            placer, offer_args = _stay_offer_place, {
                **args, "stays": [{"arrival": args["arrival"], "departure": args["departure"]}]}
        elif hold_fn is _windows_hold and args.get("date") and args.get("window"):
            placer, offer_args = _windows_offer, {
                **args, "slots": [{"date": args["date"], "window": args["window"]}]}
        elif hold_fn is _service_hold and args.get("date") and args.get("window") and args.get("service"):
            # An appointment / service time (2026-09-23, the Ellen test: the trial
            # night was held while Pepper's details came in, the 1pm meet & greet
            # was not). The service's own offer honours its pool and its rules.
            placer, offer_args = _service_offer, {
                **args, "slots": [{"date": args["date"], "window": args["window"]}]}
        else:
            return out
        try:
            reserved = placer(offer_args)
        except Exception:  # noqa: BLE001 — a reservation that breaks must never break the ask
            logging.getLogger(__name__).warning("reserve-while-gathering failed (tenant=%s)",
                                                tenant_id, exc_info=True)
            return out
        if not isinstance(reserved, dict) or reserved.get("status") != "offered":
            return out          # full / outside the rules: the hold would have failed anyway
        phrase = reserved.get("hold_until_phrase") or ""
        refs = {o.get("booking_ref") for o in (reserved.get("offered") or []) if isinstance(o, dict)}
        for b in state["bookings"]:
            if b.get("booking_ref") in refs:
                # A reservation made while details are gathered — not an offer of
                # alternatives: the same request again updates it, never trips
                # the one-offer-per-email rule.
                b["_gathering"] = True
                b["_phrase"], b["_hold_until"] = phrase, reserved.get("hold_until")
                b["_deadline_line"] = reserved.get("deadline_line")
        return {**out, "reserved": True, "hold_until": reserved.get("hold_until"),
                "hold_until_phrase": phrase,
                "deadline_line": reserved.get("deadline_line"),
                "booking_ref": ((reserved.get("offered") or [{}])[0] or {}).get("booking_ref"),
                "message": (f"{out.get('message', '').rstrip()} The dates ARE reserved for this "
                            f"customer until {phrase} while they send these — NOT booked yet. "
                            "Say so in the reply: the slot is held for them until then, and it "
                            "is confirmed as soon as the details arrive. When they reply with "
                            "them, call the same hold again: it confirms this reservation in "
                            "place.")}

    def _gathering_match(args: dict, stay_kind: bool) -> dict | None:
        """This email's reservation-while-gathering for the SAME slot, or None:
        a stay by its date span, anything else by its date and start/window."""
        rows = [b for b in state["bookings"] if b.get("_gathering") and b.get("status") == "offered"]
        if not rows:
            return None
        if stay_kind:
            arr, dep, err = _stay_times(args)
            if err:
                return None
            want = (arr.date().isoformat(), dep.date().isoformat())
            return next((b for b in rows if not _is_appt_row(b) and _row_span_dates(b) == want), None)
        try:
            day = _as_date(args.get("date")).isoformat()
        except (TypeError, ValueError):
            return None
        w = str(args.get("window") or "").strip()
        if args.get("service"):
            svc, _l = _service_meta(args.get("service"))
            w = (canonical_window(svc, w) if svc else None) or w
        return next((b for b in rows if str(b.get("booking_date")) == day
                     and str(b.get("window") or "") == w), None)

    def _only_details_missing(out) -> bool:
        """A refusal for detail the customer simply has not given — a required
        field empty, or which dependant it is for — and nothing else. Anything
        the gate JUDGED (expired, lapses, not a date, needs the certificate, a
        lapsed date on record, a date not grounded, a level) is a rule."""
        if not isinstance(out, dict) or out.get("error") != "missing_info":
            return False
        if out.get("reason") or out.get("lapsed_on_record") or out.get("reserved"):
            return False
        missing = out.get("missing") or []
        # A judged item reads "Label (expired …)"; a plain one is just the label —
        # which can itself carry a plural, "Subject name(s)" on the stays rig
        # (2026-09-23: that "(" read as a rule, so a missing subject name never held
        # anything). The plural is not a judgement.
        return bool(missing) and all(
            "(" not in str(m).replace("(s)", "").replace("(es)", "") for m in missing)

    def _intake_checked(hold_fn, args: dict) -> dict:
        """Scheduling-intake HARD gate around every hold flavour: refuse until each
        REQUIRED detail the tenant defined is in `details` (the agent then asks the
        customer for what's missing rather than booking incomplete). The sender's
        own address fills email-shaped fields first (intake.satisfy_from_sender,
        live 2026-09-03) — and a successful hold SAYS which fields it filled, so
        the agent knows they're on file and never asks (the silent fill left the
        reply asking for the email anyway, same day)."""
        filled: list[str] = []
        resolved_subjects: list[dict] = []
        if groups:
            resolved_subjects, refusal = _subject_gate(args)
            if refusal:
                return refusal
            # Legible on every surface that exists today (diary, calendar, Today):
            # the names under the tenant's own plural — 'Subjects: Murphy, Tilly'.
            details = dict(args.get("details") or {})
            for g in groups:
                names = [s["name"] for s in resolved_subjects if s["group"] is g]
                if names:
                    details[g["label_plural"]] = ", ".join(names)
            args["details"] = details
        from_record: dict = {}
        given: dict = {}
        svc_name = None
        if intake_fields:
            from app import intake
            filled = intake.sender_filled_labels(intake_fields, args.get("details"), contact)
            args["details"] = intake.satisfy_from_sender(
                intake_fields, args.get("details"), contact)
            # Fields scoped to some services bind only a booking FOR one of them
            # (Kris 2026-09-04: the permit gated the consultation).
            svc_name = (str(args.get("service") or "").strip() or None) if svc_active else None
            if appt_active:
                # A stays diary with appointments: the stay is scoped as "Stays",
                # an appointment as its own service (intake.STAYS_SCOPE).
                svc_name = ((str(args.get("service") or "").strip() or None)
                            if hold_fn is _service_hold else intake.STAYS_SCOPE)
            # Remembered rows (app/intake_memory, Kris 2026-09-21): what THIS call
            # carries is the customer's word; the record answers the rest before
            # the gate judges what is missing, so a repeat customer is never asked
            # for a fact on file. A lapsed date is left for the ask, and named.
            lapsed: dict = {}
            if intake.remembered(intake_fields) and contact:
                from app import intake_memory
                given = dict(args.get("details") or {})
                rc = _record_ctx()
                args["details"], from_record, lapsed = intake_memory.prefill_from_record(
                    intake_fields, args.get("details"), rc.get("contact"),
                    _record_defs() if rc.get("contact") else [], on=gate_date(args))
            # …and a value for one of THOSE services never rides onto this one
            # (live 2026-09-04: a remedial-only reviewer field on a full-set booking).
            args["details"] = intake.drop_out_of_scope(intake_fields, args.get("details"), svc_name)
            missing = intake.missing_required(intake_fields, args.get("details"), svc_name)
            if missing:
                refusal = _missing_info(missing)
                short = intake.needs_postcode(intake_fields, args.get("details"), svc_name)
                if short:
                    # The address is there but not whole — say which part.
                    refusal["message"] += (
                        " " + ", ".join(short) + ": the whole address is needed, "
                        "INCLUDING the postcode — ask the customer for their postcode "
                        "(never guess or look one up).")
                    refusal["needs_postcode"] = short
                if lapsed:
                    refusal["message"] += intake_memory.lapsed_note(
                        {k: v for k, v in lapsed.items() if k in missing} or lapsed)
                    refusal["lapsed_on_record"] = lapsed
                return refusal
        # The customer's OWN ladder (src/app/ladders.py, run #8): a contact with no
        # dependant is gated on the ladders kept on the contact record — the
        # subject gate above never sees them.
        from app import ladders as _ladders
        ref = _ladders.contact_prerequisite_refusal(tenant_id, contact, booked_service(args))
        if ref:
            return ref
        # A stay's drop-off / collection TIME must be one the customer gave
        # (K11, Kris 2026-09-23: "drop-off 8am, collection 5pm" was written to a
        # customer who had named no time). Last of the checks, so every RULE
        # above still refuses first; this one is a missing detail, so
        # _intake_gated reserves the stay while the agent asks. Grounded
        # tenants only — byte-identical elsewhere.
        if grounding is not None and hold_fn is _stay_hold:
            from app import grounding as _grounding
            gaps = _grounding.time_gaps(grounding, args)
            if gaps:
                refusal = _missing_info(gaps)
                refusal["message"] = (refusal.get("message", "").rstrip() + " The customer has "
                                      "not said what time — ask them. Never pick a time for "
                                      "them, and never write one back to them as theirs; you "
                                      "may give the business's drop-off / collection hours "
                                      "so they can choose.")
                return refusal
        before = len(state["bookings"])
        out = hold_fn(args)
        if resolved_subjects and isinstance(out, dict) and (out.get("held") or out.get("offered")):
            out["subjects"] = _write_subjects(resolved_subjects, state["bookings"][before:])
            names = {" ".join(str(sub["name"]).split()).lower() for sub in resolved_subjects}
            for held_row in state["bookings"][before:]:
                held_row["_subject_names"] = names     # who this hold is about (this email only)
        if filled and isinstance(out, dict) and (out.get("held") or out.get("offered")):
            out["prefilled_from_sender"] = filled
            note = (", ".join(filled) + ": taken from the sender's own email address "
                    "and already on file — do NOT ask the customer for it, and do not "
                    "mention needing it.")
            out["message"] = (out["message"].rstrip() + " " + note) if out.get("message") else note
        if isinstance(out, dict) and (out.get("held") or out.get("offered")) and intake_fields \
                and intake.remembered(intake_fields) and contact:
            from app import intake_memory
            if from_record:
                out["prefilled_from_record"] = list(from_record)
                note = intake_memory.record_note(from_record)
                out["message"] = (out["message"].rstrip() + " " + note) if out.get("message") else note
            # What the customer gave THIS time on a remembered row is the new
            # truth: onto their record now, or — a first email, no record yet —
            # parked on the executor state for the pipeline to write after
            # CRM ingest (intake_memory.flush_pending).
            rc = _record_ctx()
            values = intake_memory.values_to_remember(
                intake_fields, given, svc_name, _record_defs() if rc.get("contact") else [])
            if values:
                labels = intake_memory.labels_for(intake_fields, values)
                if rc.get("contact_id"):
                    if intake_memory.remember(tenant_id, rc["contact_id"], values):
                        state.setdefault("remembered_labels", []).extend(
                            l for l in labels if l not in state.get("remembered_labels", []))
                        # A second hold in the same email reads the fresh record.
                        rc["contact"]["custom_data"] = {**(rc["contact"].get("custom_data") or {}), **values}
                else:
                    state.setdefault("remember_pending", {}).update(values)
        return out

    def _accept_offered(date_arg, window: str, details: dict | None = None) -> dict | None:
        """The customer picked one of THEIR offered slots: convert the offered row in
        place (it already holds the capacity — a fresh hold would collide with it)
        and release the siblings. None = no live offer matches (never offered, or it
        expired) — fall through to a normal capacity-checked hold. Sweep-race safe:
        accept_offered_booking only flips a row still 'offered'. `details` = the
        acceptance call's intake bag, merged over the offer's (anything the customer
        added since the offer, plus the gate's own fills, must land on the row —
        live 2026-09-03 the flip dropped them)."""
        if not contact:
            return None
        try:
            want = _as_date(date_arg).isoformat()
        except (TypeError, ValueError):
            return None
        live = db.offered_bookings_for_contact(tenant_id, contact)
        for o in live:
            if expected_offer_id and str(o["id"]) != expected_offer_id:
                continue
            if str(o.get("booking_date")) == want and str(o.get("window")) == window:
                merged = dict(o.get("details") or {})
                merged.update({k: v for k, v in (details or {}).items()
                               if v is not None and str(v).strip()})
                accepted = db.accept_offered_booking(
                    o["id"], tenant_id, details=(merged or None),
                    **({"atomic": True, "exclude_ids": _choice_keep(live, True)} if expected_offer_id else {}))
                if not accepted:
                    return {"error": "offer_gone"} if expected_offer_id else None
                # The accepted slot carries its own price (D1, live 2026-09-26:
                # Dev's Sunday offer was out of hours — accepted, it's £140,
                # not the £85 day rate; an offer held no price at all).
                try:
                    svc_a = services.find(service_types, merged.get("Service"))
                    if svc_a and str(merged.get("Charge") or "").strip().lower() == "no charge":
                        db.set_booking_quote(accepted["id"], tenant_id, 0.0)   # a guarantee visit stays free
                        accepted["quoted_amount"] = 0.0
                    elif svc_a:
                        ooh_a = (services.out_of_hours(svc_a) or {}).get("price") if is_ooh_slot(
                            _office_cfg(), svc_a, _as_date(accepted["booking_date"]), window) else None
                        price_a = ooh_a if ooh_a is not None else svc_a.get("price")
                        if price_a is not None:
                            db.set_booking_quote(accepted["id"], tenant_id, float(price_a))
                            accepted["quoted_amount"] = float(price_a)
                except Exception:  # noqa: BLE001 — a price is extra, never the acceptance
                    logging.getLogger(__name__).warning("accepted-offer price failed", exc_info=True)
                if not expected_offer_id:
                    _release_superseded([accepted["id"]], appt_kind=True)
                # In-memory tag for the pipeline's duplicate-delivery cleanup:
                # this row was FLIPPED (the customer's acceptance of an offer
                # that predates this run), not created — a duplicate run must
                # never cancel it as its own leftover (live 2026-08-31:
                # BK-1048 was cancelled that way while the reply told the
                # customer it was theirs).
                accepted["accepted_flip"] = True
                state["bookings"].append(accepted)
                return {"held": True, "booking_id": accepted["id"],
                        "booking_ref": accepted.get("booking_ref"),
                        "date": accepted["booking_date"],
                        "window": accepted["window"], "status": "proposed",
                        "accepted_offer": True,
                        "message": "This was one of the slots already offered to this "
                                   "customer — the offer is now a real hold and the other "
                                   "offered slots are released. Keep the same booking_ref "
                                   "in your reply."}
        return {"error": "offer_gone"} if expected_offer_id else None

    def _offer_result(placed: list, exps: list, rejected: list,
                      extra: dict | None = None) -> dict:
        """Only successfully stored offers contribute to the customer promise."""
        if not placed:
            return {"error": "nothing_offered", "rejected": rejected,
                    "message": "None of those slots could be reserved as offers. Call "
                               "check_availability again and offer only slots it lists."}
        deadline = min(exps)
        phrase = dates_mod.deadline_phrase(deadline, now)
        from app.offer_deadlines import deadline_line
        line = deadline_line(placed, now)
        out = {"offered": [{"date": r["booking_date"], "window": r["window"],
                            "booking_ref": r.get("booking_ref"),
                            "expires_at": r["expires_at"]} for r in placed],
               "status": "offered",
               "deadline_line": line,
               "hold_until": deadline.isoformat(),
               "hold_until_phrase": phrase,
               "message": "These slots are now reserved EXCLUSIVELY for this customer. "
                          "Your reply MUST list exactly these slots and include "
                          f"this deadline_line verbatim: {line} Ask which one suits."}
        if rejected:
            out["rejected"] = rejected
            out["message"] += (" Some requested slots could not be reserved (see "
                               "`rejected`) — offer ONLY the slots in `offered`.")
        if extra:
            out.update(extra)
        return out

    def _windows_hold(args: dict) -> dict:
        window = str(args.get("window") or "").strip()
        accepted = _accept_offered(args.get("date"), window, args.get("details"))
        if accepted:
            return accepted
        try:
            row, err = place_hold(
                tenant_id, args.get("date"), window,
                (args.get("summary") or "").strip() or "Booking request",
                details=args.get("details"), contact=contact,
                source=source, thread_id=thread_id, now=now,
            )
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "The date must be YYYY-MM-DD."}
        if err:
            return {"error": err, "message": _HOLD_ERROR_MESSAGES[err]}
        state["bookings"].append(row)
        # A fresh hold supersedes any offer set still dangling for this contact
        # ("none of those work — do Friday instead" releases the old options).
        if contact:
            db.release_offered_for_contact(tenant_id, contact)
        return {"held": True, "booking_id": row["id"],
                "booking_ref": row.get("booking_ref"),
                "date": row["booking_date"],
                "window": row["window"], "status": "proposed"}

    def _windows_offer(args: dict) -> dict:
        slots = args.get("slots")
        if not isinstance(slots, list) or not slots:
            return {"error": "invalid_slots",
                    "message": "Pass `slots` as a list of {date, window} — 1 to 3 entries, "
                               "values exactly as check_availability returned them."}
        cfg = _office_cfg()
        when = now or datetime.now(DIARY_TZ)
        summary = (args.get("summary") or "").strip() or "Booking options offered"
        placed, exps, rejected = [], [], []
        for s in slots[:MAX_OFFER_SLOTS]:
            s = s if isinstance(s, dict) else {}
            window = str(s.get("window") or "").strip()
            try:
                day = _as_date(s.get("date"))
            except (TypeError, ValueError):
                rejected.append({"date": s.get("date"), "window": window,
                                 "error": "invalid_date"})
                continue
            err = hold_error(cfg, day, window, when)
            if err:
                rejected.append({"date": day.isoformat(), "window": window, "error": err})
                continue
            exp = offer_expiry(cfg, day, window, when)
            if exp <= when:
                rejected.append({"date": day.isoformat(), "window": window, "error": "offer_closed"})
                continue
            row = db.hold_booking_if_capacity(
                tenant_id, day.isoformat(), window, summary,
                details=args.get("details"), contact=contact, source=source,
                thread_id=thread_id, status="offered", expires_at=exp.isoformat())
            if not row:
                rejected.append({"date": day.isoformat(), "window": window,
                                 "error": "full"})
                continue
            try:   # same best-effort grid dual-write as place_hold
                blocks = slotgrid.window_to_blocks(window, _norm(cfg)["slot_minutes"], cfg or {})
                if blocks:
                    db.set_booking_blocks(row["id"], blocks[0], blocks[1], tenant_id)
            except Exception:
                pass
            state["bookings"].append(row)
            placed.append(row)
            exps.append(exp)
        # A NEW offer set supersedes the old one (live e2e catch, Kris
        # 2026-08-31: "do Friday instead" left the Thursday trio blocking the
        # diary until expiry). Same rule place_hold already applies — release
        # this contact's dangling offers, minus the ones just placed. Only when
        # something actually placed: an all-rejected call must not strip the
        # customer's existing options and leave them with nothing.
        if placed and contact:
            _release_superseded([r["id"] for r in placed], appt_kind=True)
        return _offer_result(placed, exps, rejected)

    # ── Shared office-depth helpers (resources, quantity, buffer, pricing, delivery) ──
    # Used by BOTH the service and stay paths. Each reads the tenant's scheduling_config; all
    # default to today's single-pool, no-pricing behaviour when the relevant block is empty.
    def _office_cfg() -> dict:
        if "cfg" not in oc:
            oc["cfg"] = db.get_scheduling_config(tenant_id) or {}
        return oc["cfg"]

    def _buffer_blocks(cfg_row) -> int:
        """The 'gap between bookings' (turnaround/travel) in 15-min blocks. Applies to the
        service + stay concurrency paths (the legacy windows path has its own buffer)."""
        buf = int(_merged(cfg_row).get("buffer_minutes") or 0)
        return -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0

    def _other_dates(cfg_row) -> str:
        """When it's full (docs/plan-when-its-full.md): hold / offer / off."""
        from app import courses
        return courses.settings_of(cfg_row)["other_dates"]

    def _waitlist_on() -> bool:
        from app import courses
        return courses.waitlist_on(_office_cfg())

    def _resolve_resource(args, cfg_row):
        """(resource_name|None, capacity, rate, rate_unit, error|None). With resource_types
        configured: match args['resource'] (a lone type auto-selects), capacity = that type's
        count. Without: the single fungible pool of capacity_per_window (today)."""
        rts = diary_config.clean_resource_types(cfg_row.get("resource_types"))
        if stay:
            # A stays diary's appointment pools are not spaces (2026-09-23): with a
            # Meet & greet pool as the ONLY type, the "lone type auto-selects" rule
            # below booked every stay into it — capacity 2, filed as a meet & greet.
            rts = stay_resource_types(cfg_row, service_types)
        if not rts:
            return None, int(_merged(cfg_row)["capacity_per_window"] or 0), None, None, None
        want = str(args.get("resource") or "").strip()
        r = rts[0] if (not want and len(rts) == 1) else diary_config.find_resource(rts, want)
        if not r:
            names = [x["name"] for x in rts]
            return None, 0, None, None, {
                "error": "unknown_resource", "resources": names,
                "message": ("Which type does the customer need? It must be one of: "
                            + ", ".join(names) + ". Ask them, then pass it as `resource`.")}
        return r["name"], int(r["count"]), r.get("rate"), r.get("rate_unit"), None

    def _booking_qty(args, cfg_row) -> int:
        """Units the booking consumes: party_size when party-size mode is on, else quantity
        (hire N, 2 subjects). Defaults to 1 — so a plain booking is unweighted as before."""
        party = diary_config.clean_party_size(cfg_row.get("party_size"))
        raw = args.get("party_size") if party["enabled"] else args.get("quantity")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _apply_delivery(args, cfg_row, details: dict):
        """Capture delivery onto a hold's details, returning the delivery fee (or None).
        No-op unless the tenant offers delivery AND the customer asked for it."""
        d = diary_config.clean_delivery(cfg_row.get("delivery"))
        if d["offered"] and args.get("delivery_required"):
            details["Delivery"] = "Yes"
            addr = str(args.get("delivery_address") or "").strip()
            if addr:
                details["Delivery address"] = addr[:200]
            return d["fee"]
        return None

    def _recurrence_dates(first: date, repeat: str, occurrences: int, cfg_row) -> list[date]:
        """The dates of a standing series from `first`. Empty when recurring is off or the
        repeat value isn't offered. Capped by the tenant's max_occurrences."""
        rc = diary_config.clean_recurring(cfg_row.get("recurring"))
        rep = str(repeat or "").strip().lower()
        if not rc["enabled"] or rep not in rc["frequencies"]:
            return []
        n = max(1, min(int(occurrences or 1), rc["max_occurrences"]))
        step = diary_config.FREQ_DAYS.get(rep)
        months = diary_config.FREQ_MONTHS.get(rep, 1)
        out = []
        for i in range(n):
            if step:
                out.append(first + timedelta(days=step * i))
            else:                                   # monthly/quarterly — same day-of-month, clamped
                m = first.month - 1 + months * i
                y, mo = first.year + m // 12, m % 12 + 1
                import calendar
                out.append(date(y, mo, min(first.day, calendar.monthrange(y, mo)[1])))
        return out

    # ── Stays and hire: multi-day spans, capacity = concurrent occupancy ──
    def _parse_dt(s):
        try:
            return datetime.fromisoformat(str(s).strip().replace(" ", "T")[:16])
        except (TypeError, ValueError):
            return None

    def _stay_offer(arr: datetime, dep: datetime, cfg_row, resource, capacity, qty):
        # Active stays that could overlap — a wide window back-catches long stays that
        # started before the requested arrival; their grid span does the real overlap math.
        # Filtered to the targeted resource pool, weighted by quantity, with the turnaround gap.
        booked = db.list_active_bookings(
            tenant_id, (arr.date() - timedelta(days=365)).isoformat(), dep.date().isoformat())
        spans = _pool_spans(booked, cfg_row, resource, exclude_typed=resource is None)
        length = slotgrid.datetime_to_abs(dep) - slotgrid.datetime_to_abs(arr)

        def _open_at(when) -> bool:
            o, c = slotgrid.hours_for_day(cfg_row, when.date())
            return o <= when.strftime("%H:%M") <= c

        def _legal(cand_abs: int) -> bool:
            """An alternative the business could actually accept.

            Two parts. The tenant's own stay rules — check-in and check-out
            windows, minimum nights, the front desk — and, because most tenants
            configure none of those, the opening hours themselves: a drop-off or
            a collection has to happen while somebody is there. Without the
            second part a stays business that asked for 08:00→17:00 over two nights was
            offered 16:30 → 01:30 (live 2026-09-22), a collection in the middle
            of the night against a close_time of 18:45.

            This narrows what the diary OFFERS. What it will ACCEPT from a
            customer who asks for 3am directly is a separate hole, written up in
            docs/plan-stay-hours-gap.md — a stay's times are validated against
            opening hours nowhere, and closing that changes refusals for every
            stay tenant."""
            a = slotgrid.abs_to_datetime(cand_abs)
            d = slotgrid.abs_to_datetime(cand_abs + length)
            if not (_open_at(a) and _open_at(d)):
                return False
            return not (_stay_rules(a, d, cfg_row) or _front_desk_rule(a, d, cfg_row))

        return slotgrid.stay_offer(spans, arr, dep, capacity, qty=qty,
                                   buffer_blocks=_buffer_blocks(cfg_row), legal=_legal)

    def _stay_times(args) -> tuple[datetime | None, datetime | None, dict | None]:
        arr, dep = _parse_dt(args.get("arrival")), _parse_dt(args.get("departure"))
        if not arr or not dep:
            return None, None, {"error": "invalid_date",
                                "message": "arrival and departure must be 'YYYY-MM-DDTHH:MM'."}
        if dep <= arr:
            return None, None, {"error": "invalid_range", "message": "departure must be after arrival."}
        return arr, dep, None

    def _stay_rules(arr, dep, cfg_row) -> dict | None:
        """Check-in/out time rules + minimum-stay, before capacity. Returns an error dict the
        agent acts on, or None when the request is within the rules.

        A SAME-DAY visit (arrival and departure on one day, including
        same-day hire) is exempt from all three: check-in/check-out windows and
        min_nights govern OVERNIGHT stays, and applying them to a day visit
        made day visits unbookable at a stays business (live e2e 2026-09-02:
        checkout_before 09:30 refused every afternoon collection, and
        min_nights 1 refused the day itself). Capacity still applies — a day
        visit occupies its span like any stay. Blackout dates still block either endpoint."""
        # A BLOCKED day takes no drop-off and no collection — day visits included
        # (live 2026-09-24: Claire's day visit went into the 1-6 Oct block
        # the owner had set; the block card promises "nothing can be booked on
        # those days"). A stay may still run THROUGH closed days; only its ends
        # are refused. Blackouts only — ordinary non-working days are left as
        # they were, so no other business's arrivals change.
        closed = _norm(cfg_row)["blackouts"]
        ends = [("drop-off", arr.date()), ("collection", dep.date())]
        shut = [(what, d) for what, d in ends if d in closed]
        if shut:
            what, d = shut[0]
            return {"error": "closed",
                    "closed_day": d.isoformat(),
                    "message": (f"The business is CLOSED on {d.strftime('%A %d %B')} — no "
                                f"{what} that day. Say it's closed (never full) and offer the "
                                "nearest open day for the "
                                + ("visit" if arr.date() == dep.date() else f"{what}") + ".")}
        nights = max(0, dep.date().toordinal() - arr.date().toordinal())
        if nights == 0:
            return None
        dh = diary_config.clean_day_hours(cfg_row.get("day_hours"))
        ci, co = dh.get("checkin_after"), dh.get("checkout_before")
        if ci and arr.strftime("%H:%M") < ci:
            return {"error": "checkin_time",
                    "message": f"Check-in is from {ci}. Offer an arrival at or after {ci}."}
        if co and dep.strftime("%H:%M") > co:
            return {"error": "checkout_time",
                    "message": f"Check-out is by {co}. Offer a departure at or before {co}."}
        sp = diary_config.clean_stay_pricing(cfg_row.get("stay_pricing"))
        if sp["min_nights"] and nights < sp["min_nights"]:
            return {"error": "min_nights",
                    "message": f"The minimum stay is {sp['min_nights']} night(s). Offer a "
                               "longer stay or explain the minimum."}
        return None

    def _stay_quote(cfg_row, arr, dep, qty, rate, rate_unit) -> dict | None:
        """A price for the stay — the resource type's rate if it has one, else the global
        stay_pricing rate. None when no rate is configured — and None for a
        SAME-DAY visit priced per night/week: quoting 0 nights as "1 night"
        billed a £28 day visit at the £38 overnight rate (live e2e
        2026-09-02). With no stay quote the agent prices the day from the
        service list / knowledge instead, which is where day rates live."""
        nights = max(0, dep.date().toordinal() - arr.date().toordinal())
        unit = (rate_unit or "night") if rate is not None else \
            diary_config.clean_stay_pricing(cfg_row.get("stay_pricing"))["rate_unit"]
        if nights == 0 and unit in ("night", "week"):
            return None
        length = slotgrid.datetime_to_abs(dep) - slotgrid.datetime_to_abs(arr)
        days = max(1, -(-length // slotgrid.BLOCKS_PER_DAY))
        if rate is not None:
            return diary_config.quote_stay({"rate": rate, "rate_unit": rate_unit or "night"},
                                           nights, days, qty)
        return diary_config.quote_stay(cfg_row.get("stay_pricing"), nights, days, qty)

    def _stay_alt(out: dict, offer: dict) -> dict:
        if offer.get("alt_start"):
            out["nearest_arrival"] = offer["alt_start"].isoformat(timespec="minutes")
            out["nearest_departure"] = offer["alt_end"].isoformat(timespec="minutes")
        return out

    def _front_desk_rule(arr, dep, cfg_row) -> dict | None:
        """The front desk rule (Kris 2026-09-13): a stay's arrival or departure
        that lands inside an appointment is refused, with the clear time named."""
        if not _front_desk_on(cfg_row):
            return None
        booked = db.list_active_bookings(tenant_id, arr.date().isoformat(), dep.date().isoformat())
        clash = front_desk_clash(cfg_row, booked, arr, dep)
        if not clash:
            return None
        return {"error": "front_desk",
                "message": (f"The {clash['when']} at {clash['at']} falls inside a {clash['service']} "
                            f"({clash['from']}–{clash['until']}) and the front desk handles one thing "
                            f"at a time. Offer the {clash['when']} from {clash['until']}, or before "
                            f"{clash['from']}.")}

    def _own_live_cover(arr: datetime, dep: datetime) -> dict | None:
        """This customer's own offered/proposed row for exactly these DATES.

        The capacity math cannot tell whose reservation is in the way, so a
        customer accepting what we offered them reads as a full diary. Matched
        on the date span, not the clock: the customer agreed to days, and the
        times are whatever we told them. A stay of theirs on DIFFERENT days
        that merely overlaps is a real clash and stays one."""
        if not contact:
            return None
        try:
            rows = db.bookings_for_contact(tenant_id, contact)
        except Exception:
            return None
        want = (arr.date().isoformat(), dep.date().isoformat())
        for b in db._drop_expired_offers(rows or []):
            if b.get("status") in ("offered", "proposed") and _row_span_dates(b) == want:
                return b
        return None

    def _stay_check(args: dict) -> dict:
        arr, dep, err = _stay_times(args)
        if err:
            return err
        cfg_row = _office_cfg()
        resource, capacity, rate, rate_unit, rerr = _resolve_resource(args, cfg_row)
        if rerr:
            return rerr
        rule = _stay_rules(arr, dep, cfg_row) or _front_desk_rule(arr, dep, cfg_row)
        if rule:
            return rule
        qty = _booking_qty(args, cfg_row)
        offer = _stay_offer(arr, dep, cfg_row, resource, capacity, qty)
        quote = _stay_quote(cfg_row, arr, dep, qty, rate, rate_unit)
        if offer["fits"]:
            out = {"available": True, "nights": offer["nights"],
                   "arrival": arr.isoformat(timespec="minutes"),
                   "departure": dep.isoformat(timespec="minutes"),
                   "message": f"A space is free for the whole stay ({offer['nights']} night(s))."}
            if resource:
                out["resource"] = resource
            if quote:
                out["quote"] = quote
            return out
        # "Full" because of THIS customer's own live reservation is not full —
        # it is theirs. Live 2026-09-22: Jess accepted the 19th-21st we had
        # offered her, the diary counted her own BK-1049 against her, answered
        # full, and the reply apologised that the slot "didn't hold". It had.
        mine = _own_live_cover(arr, dep)
        if mine:
            return {"available": True, "nights": offer["nights"],
                    "arrival": arr.isoformat(timespec="minutes"),
                    "departure": dep.isoformat(timespec="minutes"),
                    "already_theirs": mine.get("booking_ref"),
                    "already_theirs_status": mine.get("status"),
                    "message": (f"These dates are already reserved for THIS customer as "
                                f"{mine.get('booking_ref')} ({mine.get('status')}). They are "
                                "not full — they are taken by this customer. If the customer "
                                "is accepting them, call hold_booking for exactly these dates "
                                "and it becomes theirs; never tell them the space was lost.")}
        other = _other_dates(cfg_row)
        if other == "off":
            return {"available": False,
                    "message": "Full for those exact dates. This business does not offer other "
                               "dates — tell the customer those dates are full."}
        if not offer.get("alt_start"):
            msg = "No space in the bookable range — say a colleague will confirm."
        elif other == "offer":
            msg = ("Full for those exact dates. The nearest opening for the same length starts "
                   "then — NAME it and ask whether it suits, but do NOT reserve it (no "
                   "offer_stay): this business holds nothing until the customer says yes.")
        else:
            msg = ("Full for those exact dates. The nearest opening for the same length starts "
                   "then — reserve it with offer_stay (NOT hold_booking: they have not agreed "
                   "to it) and ask whether it suits.")
        return _stay_alt({"available": False, "message": msg}, offer)

    def _row_span_dates(b: dict) -> tuple[str, str] | None:
        """A booking row's (first date, last date). The DATES are what a
        customer agrees to — "Monday to Wednesday is perfect" — and the times
        are whatever we told them. Matching on exact blocks made the software
        brittle in the one place it must not be: live 2026-09-22, we offered
        07:00→16:00, the customer said yes, and the model reconstructed the
        times from her ORIGINAL 8am/5pm request. Different span, no match, and
        the diary then called her own reservation 'full'."""
        span = slotgrid.stay_span(b)
        if not span:
            return None
        start, length = span[0], span[1]
        return (slotgrid.abs_to_datetime(start).date().isoformat(),
                slotgrid.abs_to_datetime(start + length).date().isoformat())

    def _accept_offered_stay(arr: datetime, dep: datetime,
                             details: dict | None = None) -> dict | None:
        """The customer picked one of THEIR offered stays. The offered row already
        holds the capacity, so a fresh hold would collide with it — convert it in
        place and release the siblings, exactly as the slots path does. None = no
        live offer matches (never offered, or expired), and the caller falls
        through to a normal capacity-checked hold."""
        if not contact:
            return None
        want = (arr.date().isoformat(), dep.date().isoformat())
        try:
            live = db.offered_bookings_for_contact(tenant_id, contact)
        except Exception:
            # This lookup rides EVERY hold. If the offers read fails, a normal
            # capacity-checked hold is the right fallback — losing the flip
            # costs a duplicate row the dedupe catches; raising here would cost
            # the whole draft.
            logging.getLogger(__name__).warning(
                "offered-stay lookup failed (tenant=%s) — holding normally", tenant_id)
            if expected_offer_id:
                raise
            return None
        for o in live:
            if expected_offer_id and str(o["id"]) != expected_offer_id:
                continue
            if _row_span_dates(o) != want:
                continue
            merged = dict(o.get("details") or {})
            merged.update({k: v for k, v in (details or {}).items()
                           if v is not None and str(v).strip()})
            accepted = db.accept_offered_booking(o["id"], tenant_id, details=(merged or None),
                    **({"atomic": True, "exclude_ids": _choice_keep(live, False)} if expected_offer_id else {}))
            if not accepted:
                return {"error": "offer_gone"} if expected_offer_id else None
            if not expected_offer_id:
                _release_superseded([accepted["id"]], appt_kind=False)
            accepted["accepted_flip"] = True       # see _accept_offered: not this run's leftover
            # The customer's OWN times win over the offer's (K11, 2026-09-23): a
            # stay reserved while their times were gathered carries placeholder
            # hours, and "drop off at 9" must land as 9. Only times the customer
            # (or a sent reply of ours) actually gave — the grounding check says
            # so — and only on a grounded tenant; elsewhere the offer's times
            # stand, exactly as before.
            if grounding is not None:
                from app import grounding as _grounding
                iso = {"arrival": arr.isoformat(timespec="minutes"),
                       "departure": dep.isoformat(timespec="minutes")}
                cur = slotgrid.stay_span(accepted)            # (start_abs, length)
                want_start = slotgrid.datetime_to_abs(arr)
                want_len = slotgrid.datetime_to_abs(dep) - want_start
                if cur and not _grounding.time_gaps(grounding, iso) \
                        and (cur[0], cur[1]) != (want_start, want_len):
                    try:
                        db.set_booking_blocks(accepted["id"], slotgrid.block_at(arr.time()),
                                              want_len, tenant_id)
                        accepted["start_block"] = slotgrid.block_at(arr.time())
                        accepted["length_blocks"] = want_len
                    except Exception:  # noqa: BLE001 — the booking stands at the offer's times
                        logging.getLogger(__name__).warning(
                            "could not move accepted stay to the customer's times (tenant=%s)",
                            tenant_id, exc_info=True)
            state["bookings"].append(accepted)
            span = slotgrid.stay_span(accepted)
            real_arr = slotgrid.abs_to_datetime(span[0]) if span else arr
            real_dep = slotgrid.abs_to_datetime(span[0] + span[1]) if span else dep
            return {"held": True, "booking_id": accepted["id"],
                    "booking_ref": accepted.get("booking_ref"),
                    # The OFFER's own times, which are the times the customer was
                    # promised — not whatever this call happened to pass.
                    "arrival": real_arr.isoformat(timespec="minutes"),
                    "departure": real_dep.isoformat(timespec="minutes"),
                    "status": "proposed", "was_offered": True,
                    "message": ("The dates you offered them are now theirs — the other "
                                "offer (if any) is released. Nothing new was booked. "
                                "State the arrival and departure EXACTLY as returned "
                                "here: they are the times you promised them.")}
        return {"error": "offer_gone"} if expected_offer_id else None

    def _stay_offer_place(args: dict) -> dict:
        """offer_stay: reserve 1-2 alternative spans as exclusive offers.

        Same shape as _windows_offer, in the stay's grid terms. Capacity is
        checked in the app layer immediately before the insert, which is the
        same window _stay_hold already lives with (insert_stay_booking's
        docstring names the atomic RPC as the production hardening)."""
        stays = args.get("stays")
        if not isinstance(stays, list) or not stays:
            return {"error": "invalid_stays",
                    "message": "Pass `stays` as a list of {arrival, departure} — 1 or 2 "
                               "entries, exactly as check_availability returned them."}
        cfg_row = _office_cfg()
        when = now or datetime.now(DIARY_TZ)
        summary = (args.get("summary") or "").strip() or "Stay options offered"
        resource, capacity, rate, rate_unit, rerr = _resolve_resource(args, cfg_row)
        if rerr:
            return rerr
        qty = _booking_qty(args, cfg_row)
        placed, exps, rejected = [], [], []
        for raw in stays[:MAX_OFFER_STAYS]:
            raw = raw if isinstance(raw, dict) else {}
            arr, dep, err = _stay_times(raw)
            if err:
                rejected.append({**raw, "error": err.get("error")})
                continue
            rule = _stay_rules(arr, dep, cfg_row) or _front_desk_rule(arr, dep, cfg_row)
            if rule:
                rejected.append({"arrival": raw.get("arrival"), "departure": raw.get("departure"),
                                 "error": rule.get("error")})
                continue
            if not _stay_offer(arr, dep, cfg_row, resource, capacity, qty)["fits"]:
                rejected.append({"arrival": raw.get("arrival"),
                                 "departure": raw.get("departure"), "error": "full"})
                continue
            exp = offer_expiry(cfg_row, arr.date(), "day", when, start_at=arr)
            if exp <= when:
                rejected.append({"arrival": raw.get("arrival"), "departure": raw.get("departure"),
                                 "error": "offer_closed"})
                continue
            details = dict(args.get("details") or {})
            if resource:
                details.setdefault("Resource type", resource)
            row = db.insert_stay_booking(
                tenant_id, arr.date().isoformat(), slotgrid.block_at(arr.time()),
                slotgrid.datetime_to_abs(dep) - slotgrid.datetime_to_abs(arr),
                summary, details=details, contact=contact, source=source,
                thread_id=thread_id, status="offered", expires_at=exp.isoformat(),
                quantity=(qty if qty > 1 else None), resource_type=resource)
            row["_arrival"] = arr.isoformat(timespec="minutes")
            row["_departure"] = dep.isoformat(timespec="minutes")
            state["bookings"].append(row)
            placed.append(row)
            exps.append(exp)
        if not placed:
            return {"error": "nothing_offered", "rejected": rejected,
                    "message": "None of those stays could be reserved as offers. Call "
                               "check_availability again and offer only spans it returns "
                               "as free."}
        deadline = min(exps)
        phrase = dates_mod.deadline_phrase(deadline, now)
        from app.offer_deadlines import deadline_line
        line = deadline_line(placed, now)
        out = {"offered": [{"arrival": r["_arrival"], "departure": r["_departure"],
                            "booking_ref": r.get("booking_ref"),
                            "expires_at": r["expires_at"]} for r in placed],
               "status": "offered", "hold_until": deadline.isoformat(),
               "deadline_line": line,
               "hold_until_phrase": phrase,
               "message": ("These dates are now reserved EXCLUSIVELY for this customer. "
                           "Your reply MUST name exactly these dates and include "
                           f"this deadline_line verbatim: {line} Ask which suits. Nothing is booked until "
                           "they say yes.")}
        if rejected:
            out["rejected"] = rejected
            out["message"] += (" Some spans could not be reserved (see `rejected`) — name "
                               "ONLY the ones in `offered`.")
        return out

    def _stay_hold(args: dict) -> dict:
        arr, dep, err = _stay_times(args)
        if err:
            return err
        # The customer accepting a stay we offered is not a new booking (the
        # offered row already holds the space) — flip it, don't collide with it.
        accepted = _accept_offered_stay(arr, dep, args.get("details"))
        if accepted:
            return accepted
        cfg_row = _office_cfg()
        resource, capacity, rate, rate_unit, rerr = _resolve_resource(args, cfg_row)
        if rerr:
            return rerr
        rule = _stay_rules(arr, dep, cfg_row) or _front_desk_rule(arr, dep, cfg_row)
        if rule:
            return rule
        qty = _booking_qty(args, cfg_row)
        offer = _stay_offer(arr, dep, cfg_row, resource, capacity, qty)
        if not offer["fits"]:
            msg = "That stay can't be held — full for those dates."
            other = _other_dates(cfg_row)
            if other == "off":
                return {"error": "full", "message": msg + " This business does not offer other dates."}
            if offer.get("alt_start"):
                msg += (" Name the nearest opening and ask whether it suits — do NOT reserve it."
                        if other == "offer" else
                        " Reserve the nearest opening with offer_stay and ask whether it suits.")
            return _stay_alt({"error": "full", "message": msg}, offer)
        details = dict(args.get("details") or {})
        if resource:
            details.setdefault("Resource type", resource)
        _apply_delivery(args, cfg_row, details)
        quote = _stay_quote(cfg_row, arr, dep, qty, rate, rate_unit)
        deposit = diary_config.clean_stay_pricing(cfg_row.get("stay_pricing"))["deposit"]
        row = db.insert_stay_booking(
            tenant_id, arr.date().isoformat(), slotgrid.block_at(arr.time()),
            slotgrid.datetime_to_abs(dep) - slotgrid.datetime_to_abs(arr),
            (args.get("summary") or "").strip() or "Stay booking",
            details=details, contact=contact, source=source, thread_id=thread_id,
            quantity=(qty if qty > 1 else None), resource_type=resource,
            deposit_amount=deposit)
        if quote:
            try:
                db.set_booking_quote(row["id"], tenant_id, quote["amount"])
                row["quoted_amount"] = quote["amount"]
            except Exception:
                pass
        state["bookings"].append(row)
        out = {"held": True, "booking_id": row["id"], "booking_ref": row.get("booking_ref"),
               "arrival": arr.isoformat(timespec="minutes"),
               "departure": dep.isoformat(timespec="minutes"),
               "nights": offer["nights"], "status": "proposed"}
        if resource:
            out["resource"] = resource
        if qty > 1:
            out["quantity"] = qty
        if quote:
            out["quote"] = quote
        if deposit is not None:
            out["deposit"] = deposit
        return out

    # ── The customer's LIVE diary — never the same visit twice ──────────────────────
    # 2026-09-20, stays rig (Little Paws, thread seed:kennels:price): a details-only
    # follow-up ("my mobile is…, Biscuit was born on 2 May 2023, see you on the 7th")
    # on a thread with two CONFIRMED stays re-held both as fresh proposed bookings —
    # BK-1018/BK-1019 beside BK-1003/BK-1004 — re-quoted the £122 the customer already
    # had, and the reply said "both stays are now booked in" with the new references.
    # The one-hold-per-visit rule in execute() only sees THIS email's holds; this gate
    # sees what the diary already holds for the contact. A proposed or confirmed
    # booking covering the requested span IS that visit: refused with its reference,
    # so the reply cites the booking that stands. Offered rows never block (accepting
    # one flips it in place); a hold naming a DIFFERENT dependant on the same dates is
    # its own visit (the two-subjects rule). The portal's cross-session guard
    # (portal_data._booking_block) is this rule on its surface.
    live: dict = {}

    def _live_rows() -> list[dict]:
        if "rows" not in live:
            rows = existing_bookings
            if rows is None and contact:
                try:
                    rows = db.bookings_for_contact(tenant_id, contact)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "live-diary duplicate guard: bookings read failed (tenant=%s)",
                        tenant_id, exc_info=True)
                    rows = []
            live["rows"] = [b for b in (rows or [])
                            if b.get("status") in ("proposed", "confirmed")]
        return live["rows"]

    def _span_words(b: dict) -> str:
        """The dates a booking covers, for the refusal: 'YYYY-MM-DD', or
        'YYYY-MM-DD to YYYY-MM-DD' for a stay that runs across days."""
        when = str(b.get("booking_date") or "")
        span = slotgrid.stay_span(b)
        if span:
            end = slotgrid.abs_to_datetime(span[0] + span[1]).date().isoformat()
            if end != when:
                return f"{when} to {end}"
        return when

    def _already_booked(b: dict, about: list[str]) -> dict:
        ref = b.get("booking_ref") or "a booking"
        standing = ("confirmed" if b.get("status") == "confirmed"
                    else "held, awaiting the business's confirmation")
        about_txt = (" It is for " + ", ".join(about) + ".") if about else ""
        return {"error": "already_booked", "booking_id": b.get("id"),
                "booking_ref": b.get("booking_ref"), "status": b.get("status"),
                "dates": _span_words(b), "about": about,
                "message": (f"This customer ALREADY has {ref} covering those dates "
                            f"({_span_words(b)}) — it is {standing}.{about_txt} Never "
                            "hold the same visit twice, and do not call this tool again "
                            f"for it. In your reply, refer to {ref} as the booking that "
                            "already stands: NEVER say it is now booked, held, pencilled "
                            "in or newly confirmed, and never quote a new price for it. "
                            "If this email only adds details (contact numbers, an "
                            "address, a date of birth), say they are noted. Place a NEW "
                            "hold only for a separate, additional visit the customer is "
                            "actually asking for"
                            + (" — a DIFFERENT dependant named in `subjects` on the same "
                               "dates is one" if groups else "") + ".")}

    def _is_appt_row(b) -> bool:
        """A held row that is a stays diary's APPOINTMENT (a timed start), not a
        stay ('day') — the same test _diary_duplicate uses for the live diary."""
        return str(b.get("window") or "day").strip().lower() != "day"

    def _choice_keep(live: list[dict], appt_kind: bool) -> list:
        keep = [b["id"] for b in state["bookings"] if b.get("id")]
        if stay and service_types:
            keep += [o["id"] for o in live if _is_appt_row(o) != appt_kind]
        return keep

    def _release_superseded(keep_ids, appt_kind: bool) -> None:
        """Release this contact's other offered rows — the options a new hold or
        choice supersedes. Never this email's own rows (a stay reserved while
        details are gathered is still the customer's), and on a stays diary only
        the SAME kind: booking the introductory visit never releases the trial night
        held for the same subject, nor the reverse (docs/plan-diary-multiview.md)."""
        if not contact:
            return
        keep = [i for i in keep_ids if i] + [b["id"] for b in state["bookings"] if b.get("id")]
        if stay and service_types:
            try:
                keep += [o["id"] for o in db.offered_bookings_for_contact(tenant_id, contact)
                         if _is_appt_row(o) != appt_kind]
            except Exception:  # noqa: BLE001 — unsure what's what: release nothing
                logging.getLogger(__name__).warning(
                    "offer release skipped: offered read failed (tenant=%s)", tenant_id, exc_info=True)
                return
        keep = list(dict.fromkeys(keep))
        db.release_offered_for_contact(tenant_id, contact, exclude_ids=keep or None)

    def _diary_duplicate(args: dict, appt: bool) -> dict | None:
        """The refusal when the diary already holds this visit for this contact,
        else None. Stays: an overlapping grid span (the arrival date alone for a
        row without grid columns), against the customer's STAY rows only — an
        appointment they have during the stay (a meet & greet on a stays diary,
        Kris 2026-09-13) is not the stay. Windows and service diaries: the same
        date and window ('day' covers the whole day); an appointment on a stays
        diary matches its window exactly, so it is never the stay it falls
        inside. This run's own holds are the in-memory rule's business, not
        this one's."""
        own = {b.get("id") for b in state["bookings"] if b.get("id")}
        rows = [b for b in _live_rows() if b.get("id") not in own]
        if not rows:
            return None

        def _is_stay_row(b) -> bool:
            return str(b.get("window") or "day").strip().lower() == "day"

        if stay and not appt:
            arr, dep, terr = _stay_times(args)
            if terr:
                return None            # the hold itself reports the bad dates
            lo, hi = slotgrid.datetime_to_abs(arr), slotgrid.datetime_to_abs(dep)
            day = arr.date().isoformat()

            def _hit(b):
                if not _is_stay_row(b):
                    return False       # their appointment, not their stay
                span = slotgrid.stay_span(b)
                if span:
                    return lo < span[0] + span[1] and span[0] < hi
                return str(b.get("booking_date")) == day
        else:
            try:
                day = _as_date(args.get("date")).isoformat()
            except (TypeError, ValueError):
                return None
            window = str(args.get("window") or "").strip().lower()
            if svc_active or appt:
                svc, _ = _service_meta(args.get("service"))
                canon = canonical_window(svc, args.get("window")) if svc else None
                if canon:
                    window = str(canon).lower()

            def _hit(b):
                have = str(b.get("window") or "").strip().lower()
                if str(b.get("booking_date")) != day:
                    return False
                if appt:
                    return have == window   # an appointment inside a stay is its own booking
                return have == window or "day" in (window, have)
        hits = [b for b in rows if _hit(b)]
        if not hits:
            return None
        wanted = _subject_names(args)
        about: dict = {}
        if wanted:
            found = db.subjects_for_bookings(tenant_id, [b["id"] for b in hits if b.get("id")])
            about = {bid: [str(i.get("name") or "").strip() for i in insts if i.get("name")]
                     for bid, insts in (found or {}).items()}
        for b in hits:
            have = {" ".join(n.split()).lower() for n in about.get(b.get("id")) or []}
            if wanted and have and not (wanted & have):
                continue               # another dependant on those dates: its own visit
            return _already_booked(b, about.get(b.get("id")) or [])
        return None

    # ── Service-duration appointments: per-service length on the slot grid ──────────
    svc_active = bool(service_types) and not stay and not fleet
    # A stays diary's APPOINTMENTS (Kris 2026-09-13): the service path, under
    # its own tool names, beside the stay tools.
    appt_active = bool(service_types) and stay and not fleet

    def _service_meta(name) -> tuple[dict | None, int | None]:
        """Resolve a service name → (service dict, length_blocks), or (None, None)."""
        svc = services.find(service_types, name)
        if not svc:
            return None, None
        return svc, slotgrid.blocks_for_minutes(svc["minutes"])

    def _unknown_service() -> dict:
        names = services.names(service_types)
        return {"error": "unknown_service", "services": names,
                "message": ("Which service is this? It must be one of: "
                            + (", ".join(names) if names else "(none configured)")
                            + ". Ask the customer to confirm the service, then use that "
                            "`service` value.")}

    def _service_pool(args, svc, cfg_row):
        """The capacity pool for a service booking: the service's bound resource type if it
        has one, else the customer-named/default resource, else the single fungible pool.
        Returns (resource_name|None, capacity, error|None)."""
        if svc and svc.get("resource"):
            r = diary_config.find_resource(cfg_row.get("resource_types"), svc["resource"])
            if r:
                return r["name"], int(r["count"]), None
        resource, capacity, _rate, _ru, rerr = _resolve_resource(args, cfg_row)
        return resource, capacity, rerr

    def _svc_day_counts(svc, start, end, exclude_id=None):
        """Bookings of this service per day — read only when it has a max a day."""
        if not svc or not services.rules(svc)["max_per_day"]:
            return None
        try:
            return db.count_service_bookings_by_day(tenant_id, svc["name"], start.isoformat(),
                                                    end.isoformat(), exclude_id=exclude_id)
        except Exception:  # noqa: BLE001 — unknown is not permission
            logging.getLogger(__name__).warning(
                "max-a-day count failed (tenant=%s, service=%s)", tenant_id, svc.get("name"), exc_info=True)
            from app.booking_errors import VerificationUnavailable
            raise VerificationUnavailable("the daily booking limit") from None

    def _service_buffer(svc, cfg_row) -> int:
        """Per-service buffer if set, else the global gap-between-bookings."""
        if svc and svc.get("buffer"):
            return -(-int(svc["buffer"]) // slotgrid.BLOCK_MIN)
        return _buffer_blocks(cfg_row)

    def _service_check(args: dict) -> dict:
        try:
            start, end = _as_date(args.get("date_from")), _as_date(args.get("date_to"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "Dates must be YYYY-MM-DD."}
        cfg_row = _office_cfg()
        svc = None
        combined = [str(n).strip() for n in (args.get("services") or []) if str(n or "").strip()]
        if combined:
            # One visit, several services (live 2026-09-04: two subjects = two
            # diary checks per turn). The first names the timing/pool/buffer;
            # the lengths add up.
            metas = [_service_meta(n) for n in combined]
            if any(m[0] is None for m in metas):
                return _unknown_service()
            svc, length = metas[0][0], sum(m[1] for m in metas)
        elif str(args.get("service") or "").strip():
            svc, length = _service_meta(args.get("service"))
            if not svc:
                return _unknown_service()
        else:
            # No service named yet — fall back to the tenant's default slot length so the
            # agent can still show general availability while it works out the service.
            length = slotgrid.blocks_for_minutes(_norm(cfg_row)["slot_minutes"])
        resource, capacity, perr = _service_pool(args, svc, cfg_row)
        if perr:
            return perr
        qty = _booking_qty(args, cfg_row)
        booked = db.list_active_bookings(tenant_id, start.isoformat(), end.isoformat())
        timing = services.timing(svc) if svc else "time"
        slots = service_availability(cfg_row, booked, length, start, end,
                                     now or datetime.now(DIARY_TZ), capacity=capacity, qty=qty,
                                     buffer_blocks=_service_buffer(svc, cfg_row), resource=resource,
                                     timing=timing, svc=svc, day_counts=_svc_day_counts(svc, start, end))
        ooh_note, held_back = "", []
        if any(x.get("out_of_hours") for x in slots):
            if _ooh_allowed(svc):
                ooh_note = _ooh_note(svc)
            else:
                # "Ask me first" (the default): the owner decides evenings, so the
                # reply never sees them — Today asks instead (pipeline).
                held_back = [x for x in slots if x.get("out_of_hours")]
                slots = [x for x in slots if not x.get("out_of_hours")]
                state["ooh_withheld"].append({"service": svc["name"], "slots": held_back[:3],
                                              "price": services.out_of_hours(svc)["price"]})
        out = {"available": slots}
        if held_back:
            # Live 2026-09-26 (Ann, Ben): with the evening and Sunday held back
            # the reply said "we're closed tomorrow (Sunday)" — Hartwell isn't
            # closed, it decides Sunday jobs itself.
            out["out_of_hours_held_back"] = (
                "Some times in this range are out-of-hours cover the business decides "
                "itself — they are not listed. Don't mention them, and never say the "
                "business is closed or can't come on those days: just offer the soonest "
                "time `available` shows.")
        if ooh_note:
            out["out_of_hours_note"] = ooh_note
        if combined:
            out["services_combined"] = {"services": [m[0]["name"] for m in metas],
                                        "minutes": sum(int(m[0].get("minutes") or 0) for m in metas)}
        if timing != "time":
            out["arrival_window"] = True
            out["message_timing"] = (
                "This service is booked as an ARRIVAL WINDOW: offer the customer the "
                + ("morning or the afternoon" if timing == "half" else "day")
                + " shown here, never a clock time, and pass that window value "
                "('am'/'pm'/'day') to hold_booking. The business manages the exact hour.")
        # Same period-ask summary as the windows path: per-day counts of free
        # start times, so a "next week?" reply quotes real numbers per day.
        if slots and end > start:
            out["day_summary"] = day_summary(slots)
        _add_notice_note(out, args.get("date_from"), svc)
        if svc:
            out["service"] = svc["name"]
            out["duration_minutes"] = svc["minutes"]
            if svc.get("price") is not None:
                out["price"] = svc["price"]
        if resource:
            out["resource"] = resource
        # WHY a day has no times, when the reason is the diary being shut:
        # an empty day read as "fully booked" (live 2026-09-24, Sophie/Pepper:
        # "we're fully booked for meet & greets on Tuesday 6th October" — the
        # diary was closed 1-6 Oct, nothing was booked).
        norm = _norm(cfg_row)
        closed = [start + timedelta(days=i) for i in range(min((end - start).days + 1, 62))]
        closed = [d for d in closed if d in norm["blackouts"] or d.weekday() not in norm["working_days"]]
        if closed:
            out["closed_days"] = [d.isoformat() for d in closed]
            out["closed_note"] = ("The business is CLOSED on the closed_days — not taking bookings "
                                  "those days. Say it's closed (or not taking bookings) then; "
                                  "never call a closed day full or fully booked.")
        if not slots:
            out["message"] = ("No bookable times for that service in this range. Check the "
                              "FOLLOWING days now and offer the customer the nearest times "
                              "that fit in THIS reply — never promise to check and get back "
                              "to them.")
            try:
                out.update(why_none(cfg_row, start, end, now, timing=timing, length_blocks=length,
                                    svc=svc))
            except Exception:  # noqa: BLE001 — the reason is extra, never the answer
                pass
        return _horizon_note(out, start, end)

    def _service_slot_error(cfg_row, day, sb, length, when, svc=None) -> str:
        """Why a requested (day, start) isn't offered — mapped to a _HOLD_ERROR_MESSAGES code."""
        c = _norm(cfg_row)
        if day == when.astimezone(DIARY_TZ).date() and past_same_day_cutoff(svc, when):
            return "outside_horizon"
        o, cl = slotgrid.open_close_blocks(cfg_row or {}, day)
        if sb < o or sb + length > cl:
            return "invalid_window"
        if day.weekday() not in c["working_days"] or day in c["blackouts"]:
            return "closed"
        if (datetime.combine(day, slotgrid.time_at(sb), tzinfo=DIARY_TZ)
                < when.astimezone(DIARY_TZ) + timedelta(hours=service_notice(c, svc))
                or day > when.astimezone(DIARY_TZ).date() + timedelta(days=c["horizon_days"])):
            return "outside_horizon"
        return "full"

    def _service_rule_error(svc, day, sb, length) -> dict | None:
        """Which of the service's own rules a (day, start) breaks, with the words to
        say — None when it breaks none."""
        r = services.rules(svc)
        name = svc.get("name") or "That service"
        if r["days"] is not None and day.weekday() not in r["days"]:
            return {"error": "service_day",
                    "message": (f"{name} is only booked on {services.days_label(r['days'])}. Offer the "
                                "nearest of those days that the availability check shows.")}
        if r["starts"] and sb not in {slotgrid.block_at(t) for t in r["starts"]}:
            return {"error": "service_start",
                    "message": (f"{name} starts only {services.rules_label({'starts': r['starts']})}. "
                                "Offer one of those times that the availability check shows.")}
        lo = slotgrid.block_at(r["hours_from"]) if r["hours_from"] else None
        hi = slotgrid.block_at(r["hours_until"]) if r["hours_until"] else None
        if (lo is not None and sb < lo) or (hi is not None and sb + length > hi):
            return {"error": "service_hours",
                    "message": (f"{name} is only booked {services.rules_label({**r, 'days': None, 'max_per_day': None})}. "
                                "Offer a start the availability check shows.")}
        if r["max_per_day"]:
            n = (_svc_day_counts(svc, day, day) or {}).get(day.isoformat(), 0)
            if n >= r["max_per_day"]:
                return {"error": "service_day_full",
                        "message": (f"{name} is full that day (up to {r['max_per_day']} a day). "
                                    "Offer another day the availability check shows.")}
        return None

    def _service_hold(args: dict) -> dict:
        svc, length = _service_meta(args.get("service"))
        if not svc:
            return _unknown_service()
        try:
            day = _as_date(args.get("date"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "The date must be YYYY-MM-DD."}
        timing = services.timing(svc)
        w = canonical_window(svc, args.get("window"))
        if w is None:
            return {"error": "invalid_window", "message": window_hint(svc)}
        accepted = _accept_offered(args.get("date"), w, args.get("details"))
        if accepted:
            accepted.setdefault("service", svc["name"])
            return accepted
        cfg_row = _office_cfg()
        if is_ooh_slot(cfg_row, svc, day, w) and not _ooh_allowed(svc):
            return {"error": "ooh_needs_owner",
                    "message": ("That time is out of hours, and this business decides those itself — "
                                "don't hold or promise it. Hold the soonest standard time "
                                "check_availability shows.")}
        ooh_price = (services.out_of_hours(svc) or {}).get("price") if is_ooh_slot(cfg_row, svc, day, w) else None
        resource, capacity, perr = _service_pool(args, svc, cfg_row)
        if perr:
            return perr
        qty = _booking_qty(args, cfg_row)
        buf = _service_buffer(svc, cfg_row)
        when = now or datetime.now(DIARY_TZ)
        # The dates to place: a single date, or a recurring series (each capacity-checked).
        series = _recurrence_dates(day, args.get("repeat"), args.get("occurrences"), cfg_row) or [day]
        # Re-derive availability per date: the ONE place that applies opening hours, working
        # days, notice, horizon AND capacity, so a hold never beats the checks the agent saw.
        def _booked_on(d):
            return db.list_active_bookings(
                tenant_id, (d - timedelta(days=1)).isoformat(), d.isoformat())

        def _offered(d):
            return {s["window"] for s in service_availability(
                cfg_row, _booked_on(d), length, d, d, when, capacity=capacity, qty=qty,
                buffer_blocks=buf, resource=resource, timing=timing,
                svc=svc, day_counts=_svc_day_counts(svc, d, d))}

        def _start_for(d):
            # The grid block the span lands on: the exact start, or the earliest fit
            # inside the arrival window (the operator's pencilled start).
            return first_fit_start(cfg_row, _booked_on(d), length, d, w, when,
                                   capacity=capacity, qty=qty, buffer_blocks=buf,
                                   resource=resource, svc=svc,
                                   notice_hours=services.rules(svc).get("notice_hours"))
        # The FIRST date must be bookable — if not, refuse with the precise reason.
        sb = _start_for(day) if w in _offered(day) else None
        if sb is None:
            probe = slotgrid.block_at(w) if ":" in w else window_bounds(cfg_row, day, w)[0]
            if resource:
                hit = front_desk_hit(cfg_row, _booked_on(day), day, probe, length)
                if hit:
                    return {"error": "front_desk",
                            "message": (f"The front desk is busy at {hit} with a drop-off or collection, and "
                                        "the business keeps appointments clear of those. Offer the nearest "
                                        "start check_appointment_availability shows.")}
            ruled = _service_rule_error(svc, day, probe, length)
            if ruled:
                # The availability tools never showed this time, so reaching here
                # means the agent tried one it wasn't given (the backstop firing).
                logging.getLogger(__name__).warning(
                    "service rule backstop: %s refused (tenant=%s, service=%s)",
                    ruled["error"], tenant_id, svc.get("name"))
                return ruled
            err = _service_slot_error(cfg_row, day, probe, length, when, svc)
            return {"error": err, "message": _HOLD_ERROR_MESSAGES[err]}
        base_details = dict(args.get("details") or {})
        base_details.setdefault("Service", svc["name"])
        if resource:
            base_details.setdefault("Resource type", resource)
        _apply_delivery(args, cfg_row, base_details)
        rec_id = str(uuid.uuid4()) if len(series) > 1 else None
        placed, skipped = [], []
        for d in series:
            sb_d = sb if d == day else (_start_for(d) if w in _offered(d) else None)
            if sb_d is None:
                skipped.append(d.isoformat())
                continue
            row = db.insert_stay_booking(
                tenant_id, d.isoformat(), sb_d, length,
                (args.get("summary") or "").strip() or svc["name"],
                details=dict(base_details), contact=contact, source=source, thread_id=thread_id,
                window=w, quantity=(qty if qty > 1 else None),
                resource_type=resource, recurrence_id=rec_id)
            price = ooh_price if (ooh_price is not None and d == day) else svc.get("price")
            if price is not None:
                try:
                    db.set_booking_quote(row["id"], tenant_id, float(price))
                    row["quoted_amount"] = float(price)
                except Exception:
                    pass
            if ooh_price is not None and d == day:
                row["out_of_hours"] = True
            placed.append(row)
            state["bookings"].append(row)
        # A fresh hold supersedes any offer set still dangling for this contact.
        _release_superseded([], appt_kind=True)
        first = placed[0]
        out = {"held": True, "booking_id": first["id"], "booking_ref": first.get("booking_ref"),
               "date": first["booking_date"], "window": w,
               "service": svc["name"], "duration_minutes": svc["minutes"], "status": "proposed"}
        if timing != "time":
            out["arrival_window"] = True
            said = arrival_sentence(first["booking_date"], w, when.astimezone(DIARY_TZ).date())
            out["message"] = (f"Arrival window — do not quote a clock time. Say it in those "
                              f"words: \"{said}\". Keep the sentence's shape; never squeeze "
                              f"the window into a noun (\"a this afternoon callout\").")
        if svc.get("price") is not None:
            out["price"] = svc["price"]
        if resource:
            out["resource"] = resource
        if len(series) > 1:
            out["occurrences_booked"] = [r["booking_date"] for r in placed]
            if skipped:
                out["occurrences_unavailable"] = skipped
        return out

    def _service_offer(args: dict) -> dict:
        svc, length = _service_meta(args.get("service"))
        if not svc:
            return _unknown_service()
        slots = args.get("slots")
        if not isinstance(slots, list) or not slots:
            return {"error": "invalid_slots",
                    "message": "Pass `slots` as a list of {date, window} — 1 to 3 entries, "
                               "times exactly as check_availability returned them."}
        cfg_row = _office_cfg()
        resource, capacity, perr = _service_pool(args, svc, cfg_row)
        if perr:
            return perr
        qty = _booking_qty(args, cfg_row)
        buf = _service_buffer(svc, cfg_row)
        when = now or datetime.now(DIARY_TZ)

        timing = services.timing(svc)

        def _booked_on(d):
            return db.list_active_bookings(
                tenant_id, (d - timedelta(days=1)).isoformat(), d.isoformat())

        def _bookable(d):
            # Same re-derivation discipline as _service_hold: an offer never beats
            # the checks the agent saw.
            return {x["window"] for x in service_availability(
                cfg_row, _booked_on(d), length, d, d, when, capacity=capacity, qty=qty,
                buffer_blocks=buf, resource=resource, timing=timing,
                svc=svc, day_counts=_svc_day_counts(svc, d, d))}

        base_details = dict(args.get("details") or {})
        base_details.setdefault("Service", svc["name"])
        if resource:
            base_details.setdefault("Resource type", resource)
        placed, exps, rejected = [], [], []
        for s in slots[:MAX_OFFER_SLOTS]:
            s = s if isinstance(s, dict) else {}
            window = str(s.get("window") or "").strip()
            w = canonical_window(svc, window)
            try:
                day = _as_date(s.get("date"))
            except (TypeError, ValueError):
                w = None
            if w is None:
                rejected.append({"date": s.get("date"), "window": window,
                                 "error": "invalid_slot"})
                continue
            if is_ooh_slot(cfg_row, svc, day, w) and not _ooh_allowed(svc):
                rejected.append({"date": day.isoformat(), "window": w, "error": "ooh_needs_owner"})
                continue
            sb = (first_fit_start(cfg_row, _booked_on(day), length, day, w, when,
                                  notice_hours=services.rules(svc).get("notice_hours"), svc=svc,
                                  capacity=capacity, qty=qty, buffer_blocks=buf,
                                  resource=resource) if w in _bookable(day) else None)
            if sb is None:
                probe = slotgrid.block_at(w) if ":" in w else window_bounds(cfg_row, day, w)[0]
                rejected.append({"date": day.isoformat(), "window": w,
                                 "error": _service_slot_error(cfg_row, day, probe, length, when, svc)})
                continue
            exp = offer_expiry(cfg_row, day, w, when, svc)
            if exp <= when:
                rejected.append({"date": day.isoformat(), "window": w, "error": "offer_closed"})
                continue
            row = db.insert_stay_booking(
                tenant_id, day.isoformat(), sb, length,
                (args.get("summary") or "").strip() or svc["name"],
                details=dict(base_details), contact=contact, source=source,
                thread_id=thread_id, status="offered", window=w,
                quantity=(qty if qty > 1 else None), resource_type=resource,
                expires_at=exp.isoformat())
            state["bookings"].append(row)
            placed.append(row)
            exps.append(exp)
        extra = {"service": svc["name"], "duration_minutes": svc["minutes"]}
        if timing != "time":
            extra["arrival_window"] = True
            today = when.astimezone(DIARY_TZ).date()
            said = [arrival_words(r["booking_date"], r.get("window"), today) for r in placed]
            extra["message_timing"] = (
                "Arrival windows — never a clock time. Each offered slot, in customer "
                "words: " + "; ".join(f'"{s}"' for s in said if s) +
                (
                    ". Offer them in that shape — \"we could be with you <slot>\" — and never squeeze a window into "
                    "a noun; state the time window separately."
                ))
        if svc.get("price") is not None:
            extra["price"] = svc["price"]
        return _offer_result(placed, exps, rejected, extra)

    def _cancel_booking(args: dict) -> dict:
        """Cancel the customer's own booking and settle it (run #8): the notice
        against the Cancellations & credits rule decides late_cancel or excused
        on the register, the wallet spends or refunds per the rule, the calendar
        event goes, the freed place is offered. Refuses anything that is not this
        customer's live future booking."""
        import asyncio
        from app import wallet as _wallet
        if not contact:
            return {"error": "no_contact",
                    "message": "The customer's email address is needed to find their booking."}
        ref = " ".join(str(args.get("booking_ref") or "").split()).upper()
        if not ref:
            return {"error": "no_ref", "message": "Pass the booking's reference, e.g. 'BK-1042'."}
        mine = db.bookings_for_contact(tenant_id, contact)
        row = next((b for b in mine if str(b.get("booking_ref") or "").upper() == ref), None)
        if row is None:
            held_here = any(str(b.get("booking_ref") or "").upper() == ref for b in state["bookings"])
            return {"error": "not_theirs",
                    "message": (f"{ref} is a slot held in THIS conversation — do not cancel it; "
                                "leave it out of the reply or hold a different time instead."
                                if held_here else
                                f"{ref} is not one of this customer's live bookings. Their bookings: "
                                + (", ".join(str(b.get("booking_ref")) for b in mine) or "none")
                                + ". Ask which booking they mean if it is unclear; never cancel another customer's.")}
        if row.get("status") == "offered":
            return {"error": "not_booked",
                    "message": f"{ref} is an offered slot, not a booking — it lapses on its own; nothing to cancel."}
        nowdt = now if isinstance(now, datetime) else datetime.now(DIARY_TZ)
        if nowdt.tzinfo is None:
            nowdt = nowdt.replace(tzinfo=DIARY_TZ)
        try:
            day = _as_date(row.get("booking_date"))
            sb = row.get("start_block")
            start = datetime.combine(day, slotgrid.time_at(int(sb)) if sb is not None else time(0, 0), tzinfo=DIARY_TZ)
        except (TypeError, ValueError):
            return {"error": "bad_booking", "message": f"{ref} has no usable date on record — say a colleague will look at it."}
        if start < nowdt:
            return {"error": "already_happened",
                    "message": f"{ref} was on {day.isoformat()} and has already passed — nothing to cancel; "
                               "if they missed it, the register decides, not the reply."}
        cfg_row = _office_cfg()
        credits = _wallet.credits_of(cfg_row)
        notice_hours = round((start - nowdt).total_seconds() / 3600.0, 1)
        late = notice_hours < float(credits.get("late_hours") or 0)
        outcome = "late_cancel" if late else "excused"
        reason = " ".join(str(args.get("reason") or "").split())[:120]
        db.set_booking_status(row["id"], "cancelled", tenant_id)
        reg = {}
        try:
            reg = _wallet.apply_attendance(
                tenant_id, row, outcome, logged_by="agent",
                note=(f"cancelled by the customer by {source}" + (f" — {reason}" if reason else "")
                      + f"; {notice_hours:g} hours' notice against a {credits.get('late_hours')}-hour rule"),
                notified_at=nowdt.isoformat()) or {}
        except Exception:
            logging.getLogger(__name__).warning("cancel: register write failed (%s)", ref, exc_info=True)
        async def _after():
            try:
                from app import calendar_sync
                await calendar_sync.remove_pushed_event(tenant_id, row["id"])
            except Exception:
                logging.getLogger(__name__).warning("cancel: calendar removal failed (%s)", ref, exc_info=True)
            try:
                from app import courses as _courses
                await _courses.on_place_freed(tenant_id, {**row, "status": "confirmed"})
            except Exception:
                logging.getLogger(__name__).warning("cancel: freed-place hook failed (%s)", ref, exc_info=True)
        try:
            asyncio.get_running_loop().create_task(_after())
        except RuntimeError:
            asyncio.run(_after())
        when = f"{dates_mod.pretty_date(day)}" + (f" {slotgrid.hhmm(int(sb))}" if sb is not None else "")
        wallet_out = reg.get("wallet") or {}
        spent = wallet_out.get("spent") or wallet_out.get("amount")
        refunded = wallet_out.get("refunded")
        if late:
            consequence = (f"that is inside the {credits.get('late_hours')}-hour rule, so it counts as a late "
                           "cancellation: "
                           + (f"{spent:g} hour(s) have come off their block" if spent
                              else "it is charged in full (or spends their credit) per the rules")
                           + "; no make-up applies to a late cancellation")
        else:
            consequence = (f"that is within the {credits.get('late_hours')}-hour rule, so there is no charge"
                           + (f" and {refunded:g} hour(s) are back on their block" if refunded else "")
                           + "; if the business offers make-ups, they may ask to book one")
        db.log_agent_event(tenant_id, "booking_cancelled",
                           f"{ref} ({when}) cancelled at the customer's request by {source} — "
                           f"{notice_hours:g} hours' notice, marked {outcome.replace('_', ' ')}",
                           contact=contact, booking_id=row["id"], booking_ref=ref,
                           event_date=day.isoformat())
        state.setdefault("cancelled", []).append({**row, "status": "cancelled", "outcome": outcome,
                                                  "notice_hours": notice_hours})
        return {"cancelled": True, "booking_ref": ref, "date": day.isoformat(),
                "window": slotgrid.hhmm(int(sb)) if sb is not None else row.get("window"),
                "notice_hours": notice_hours, "rule_hours": credits.get("late_hours"),
                "late": late, "register": outcome, "wallet": wallet_out or None,
                "message": (f"{ref} on {when} is now CANCELLED. The customer gave {notice_hours:g} hours' notice — "
                            f"{consequence}. Say exactly this in the reply, as done; do not defer it to a "
                            "colleague and do not promise anything the result does not say.")}

    def _move_booking(args: dict) -> dict:
        """Move the customer's own booking to a new slot. In time: in place, same
        reference, no charge. Inside the notice rule: the new time is held first,
        then the old booking is cancelled as a late cancellation through the
        cancel tool (register + wallet), so a late change costs what a late
        cancel costs and the moved session still spends when it happens."""
        import asyncio
        from app import wallet as _wallet
        if stay or fleet:
            return {"error": "unsupported",
                    "message": "This kind of booking is rearranged by the business — say a colleague "
                               "will confirm the new dates; do not promise the move."}
        if not contact:
            return {"error": "no_contact",
                    "message": "The customer's email address is needed to find their booking."}
        ref = " ".join(str(args.get("booking_ref") or "").split()).upper()
        if not ref:
            return {"error": "no_ref", "message": "Pass the booking's reference, e.g. 'BK-1042'."}
        mine = db.bookings_for_contact(tenant_id, contact)
        row = next((b for b in mine if str(b.get("booking_ref") or "").upper() == ref), None)
        if row is None:
            held_here = any(str(b.get("booking_ref") or "").upper() == ref for b in state["bookings"])
            return {"error": "not_theirs",
                    "message": (f"{ref} is a slot held in THIS conversation — it is not booked yet; hold a "
                                "different time instead of moving it."
                                if held_here else
                                f"{ref} is not one of this customer's live bookings. Their bookings: "
                                + (", ".join(str(b.get("booking_ref")) for b in mine) or "none")
                                + ". Ask which booking they mean if it is unclear; never move another customer's.")}
        if row.get("status") == "offered":
            return {"error": "not_booked",
                    "message": f"{ref} is an offered slot, not a booking — hold the time they want instead."}
        if row.get("status") not in ("proposed", "confirmed"):
            return {"error": "not_movable", "message": f"{ref} is {row.get('status')} — it can't be moved."}
        nowdt = now if isinstance(now, datetime) else datetime.now(DIARY_TZ)
        if nowdt.tzinfo is None:
            nowdt = nowdt.replace(tzinfo=DIARY_TZ)
        try:
            day = _as_date(row.get("booking_date"))
            sb = row.get("start_block")
            start = datetime.combine(day, slotgrid.time_at(int(sb)) if sb is not None else time(0, 0), tzinfo=DIARY_TZ)
        except (TypeError, ValueError):
            return {"error": "bad_booking", "message": f"{ref} has no usable date on record — say a colleague will look at it."}
        if start < nowdt:
            return {"error": "already_happened",
                    "message": f"{ref} was on {day.isoformat()} and has already passed — nothing to move."}
        try:
            new_day = _as_date(args.get("new_date"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "The new date must be YYYY-MM-DD."}
        new_w = " ".join(str(args.get("new_window") or "").split())
        if not new_w:
            return {"error": "invalid_window", "message": "Pass the new slot as check_availability lists it."}
        cfg_row = _office_cfg()
        credits = _wallet.credits_of(cfg_row)
        notice_hours = round((start - nowdt).total_seconds() / 3600.0, 1)
        inside = notice_hours < float(credits.get("late_hours") or 0)
        # Inside the window the business decides (Cancellations & credits): a late
        # move charged as a late cancel plus a fresh booking, or free.
        late = inside and bool(credits.get("late_move_charged", True))
        reason = " ".join(str(args.get("reason") or "").split())[:120]
        old_when = f"{dates_mod.pretty_date(day)}" + (f" {slotgrid.hhmm(int(sb))}" if sb is not None else "")
        details = dict(row.get("details") or {})

        if late:
            # Hold the new time FIRST (nothing changes if it cannot be held), then
            # cancel the old booking through the cancel tool: late cancel on the
            # register, the wallet spends per the rule, the place freed.
            if service_types:
                svc, _length = _service_meta(details.get("Service") or args.get("service"))
                if not svc:
                    return {"error": "unknown_service",
                            "message": f"{ref}'s service is no longer on the menu — say a colleague will move it."}
                held = _service_hold({"service": svc["name"], "date": new_day.isoformat(), "window": new_w,
                                      "details": details, "summary": row.get("summary")})
            else:
                held = _windows_hold({"date": new_day.isoformat(), "window": new_w,
                                      "details": details, "summary": row.get("summary")})
            if held.get("error"):
                return held
            gone = _cancel_booking({"booking_ref": ref,
                                    "reason": (f"moved to {new_day.isoformat()} {new_w}" + (f" — {reason}" if reason else ""))})
            if gone.get("error"):
                return gone
            new_when = f"{dates_mod.pretty_date(new_day)} {new_w}"
            wallet_out = gone.get("wallet") or {}
            spent = wallet_out.get("spent") or wallet_out.get("amount")
            db.log_agent_event(tenant_id, "booking_moved",
                               f"{ref} ({old_when}) moved late by the customer by {source} to {new_when} as "
                               f"{held.get('booking_ref')} — {notice_hours:g} hours' notice, the old booking a late cancellation",
                               contact=contact, booking_id=held.get("booking_id"), booking_ref=held.get("booking_ref"),
                               event_date=new_day.isoformat())
            state.setdefault("moved", []).append({"from_ref": ref, "to_ref": held.get("booking_ref"), "late": True})
            return {"moved": True, "late": True, "old_booking_ref": ref, "booking_ref": held.get("booking_ref"),
                    "booking_id": held.get("booking_id"), "date": new_day.isoformat(), "window": new_w,
                    "notice_hours": notice_hours, "rule_hours": credits.get("late_hours"),
                    "status": "proposed", "wallet": wallet_out or None,
                    "message": (f"{ref} on {old_when} was inside the {credits.get('late_hours')}-hour rule "
                                f"({notice_hours:g} hours' notice), so it is CANCELLED as a late cancellation — "
                                + (f"{spent:g} hour(s) have come off their block" if spent
                                   else "it is charged in full (or spends their credit) per the rules")
                                + f" — and the new time is HELD as {held.get('booking_ref')} on {new_when}, "
                                "confirmed when this reply is approved. Say both plainly, as done; do not "
                                "defer either to a colleague.")}

        # In time: move in place — same reference, details, subjects and linkage.
        if service_types:
            svc, length = _service_meta(details.get("Service") or args.get("service"))
            if not svc:
                return {"error": "unknown_service",
                        "message": f"{ref}'s service is no longer on the menu — say a colleague will move it."}
            w = canonical_window(svc, new_w)
            if w is None:
                return {"error": "invalid_window", "message": window_hint(svc)}
            length = int(row.get("length_blocks") or length)   # a 2-hour session stays 2 hours
            resource, capacity, perr = _service_pool({}, svc, cfg_row)
            if perr:
                return perr
            qty = int(row.get("quantity") or 1)
            buf = _service_buffer(svc, cfg_row)
            booked = [b for b in db.list_active_bookings(
                tenant_id, (new_day - timedelta(days=1)).isoformat(), new_day.isoformat())
                if b.get("id") != row["id"]]                     # its own old slot never blocks it
            offered = {s["window"] for s in service_availability(
                cfg_row, booked, length, new_day, new_day, nowdt, capacity=capacity, qty=qty,
                buffer_blocks=buf, resource=resource, timing=services.timing(svc),
                svc=svc, day_counts=_svc_day_counts(svc, new_day, new_day, exclude_id=row["id"]))}
            sb_new = first_fit_start(cfg_row, booked, length, new_day, w, nowdt, capacity=capacity, qty=qty,
                                     notice_hours=services.rules(svc).get("notice_hours"), svc=svc,
                                     buffer_blocks=buf, resource=resource) if w in offered else None
            if sb_new is None:
                probe = slotgrid.block_at(w) if ":" in w else window_bounds(cfg_row, new_day, w)[0]
                err = _service_slot_error(cfg_row, new_day, probe, length, nowdt, svc)
                return {"error": err, "message": _HOLD_ERROR_MESSAGES[err]}
            moved = db.move_booking_in_place(row["id"], tenant_id, new_day.isoformat(), sb_new, length, w)
            if not moved:
                return {"error": "full", "message": _HOLD_ERROR_MESSAGES["full"]}
            new_when = f"{dates_mod.pretty_date(new_day)} {slotgrid.hhmm(int(sb_new))}"
        else:
            moved, err = move_booking(tenant_id, row["id"], new_day, new_w, now=nowdt)
            if err:
                code = err if err in _HOLD_ERROR_MESSAGES else "full"
                return {"error": err, "message": _HOLD_ERROR_MESSAGES[code]}
            w = new_w
            new_when = f"{dates_mod.pretty_date(new_day)} {new_w}"

        async def _after():
            try:
                from app import calendar_sync
                await calendar_sync.remove_pushed_event(tenant_id, row["id"])
                if row.get("status") == "confirmed":
                    await calendar_sync.push_bookings(tenant_id, booking_id=row["id"])
            except Exception:
                logging.getLogger(__name__).warning("move: calendar refresh failed (%s)", ref, exc_info=True)
            try:
                from app import courses as _courses
                await _courses.on_place_freed(tenant_id, {**row, "status": "confirmed"})
            except Exception:
                logging.getLogger(__name__).warning("move: freed-place hook failed (%s)", ref, exc_info=True)
        try:
            asyncio.get_running_loop().create_task(_after())
        except RuntimeError:
            asyncio.run(_after())
        db.log_agent_event(tenant_id, "booking_moved",
                           f"{ref} moved at the customer's request by {source} from {old_when} to {new_when} — "
                           f"{notice_hours:g} hours' notice, no charge" + (f" ({reason})" if reason else ""),
                           contact=contact, booking_id=row["id"], booking_ref=ref,
                           event_date=new_day.isoformat())
        state.setdefault("moved", []).append({"from_ref": ref, "to_ref": ref, "late": False,
                                              "from": day.isoformat(), "to": new_day.isoformat()})
        why_free = (f"inside the {credits.get('late_hours')}-hour rule, but the business does not charge for "
                    "a late move" if inside else
                    f"{notice_hours:g} hours' notice against the {credits.get('late_hours')}-hour rule")
        return {"moved": True, "late": False, "inside_rule": inside, "booking_ref": ref, "booking_id": row["id"],
                "from_date": day.isoformat(), "date": new_day.isoformat(), "window": w,
                "notice_hours": notice_hours, "rule_hours": credits.get("late_hours"),
                "status": row.get("status"),
                "message": (f"{ref} is now MOVED from {old_when} to {new_when} — same reference, no charge "
                            f"({why_free}). Say exactly this in the reply, as done; do not defer it to a "
                            "colleague and do not promise anything the result does not say.")}

    def _join_waitlist(args: dict) -> dict:
        """The waiting list (src/app/courses.py): one row per named participant (or
        one for the customer) for the class behind date + window + service."""
        from app import courses
        cfg_row = _office_cfg()
        wl = courses.settings_of(cfg_row)
        if not wl["waitlist"]:
            return {"error": "no_waitlist",
                    "message": "This business does not keep a waiting list. Offer the free "
                               "alternatives check_availability shows instead."}
        if not contact:
            return {"error": "no_contact", "message": "The customer's email address is needed to add them."}
        # A stay waits for its exact span (docs/plan-when-its-full.md): the
        # dates they asked for, checked full here — a list for a span that
        # has room is a booking nobody made.
        stay_key = None
        if stay and (args.get("arrival") or args.get("departure") or not args.get("date")):
            arr, dep, err = _stay_times(args)
            if err:
                return {**err, "message": "For a stay, pass `arrival` and `departure` exactly as "
                                          "the customer asked (YYYY-MM-DDTHH:MM)."}
            resource, capacity, _r, _ru, rerr = _resolve_resource(args, cfg_row)
            if rerr:
                return rerr
            if _stay_offer(arr, dep, cfg_row, resource, capacity, _booking_qty(args, cfg_row))["fits"]:
                return {"error": "not_full",
                        "message": "Those dates have space — there is nothing to wait for. Hold "
                                   "them with hold_booking instead."}
            stay_key = courses.stay_key(resource, arr.isoformat(timespec="minutes"),
                                        dep.isoformat(timespec="minutes"))
            day = arr.date()
        else:
            try:
                day = _as_date(args.get("date"))
            except (TypeError, ValueError):
                return {"error": "invalid_date", "message": "The date must be YYYY-MM-DD."}
        # The list is for a class or slot that is FULL — never a date the diary
        # has not opened yet (run #8: a waiting-list place for an empty day).
        _h = int(_norm(cfg_row).get("horizon_days") or 0)
        _last = (now or datetime.now(DIARY_TZ)).date() + timedelta(days=_h)
        if day > _last:
            return {"error": "beyond_horizon", "horizon_days": _h,
                    "opens_on": (day - timedelta(days=_h)).isoformat(),
                    "message": (f"{day.isoformat()} is beyond the {_h}-day booking horizon — the diary "
                                f"is not open for it yet (it opens on {(day - timedelta(days=_h)).isoformat()}), "
                                "so it is not full and there is no list to join. Tell the customer when "
                                "the diary opens for that date; do not add them to a waiting list.")}
        w = str(args.get("window") or "").strip()
        svc_name = str(args.get("service") or "").strip()
        if (svc_active or appt_active) and svc_name:
            svc, _l = _service_meta(svc_name)
            if not svc:
                return _unknown_service()
            svc_name = svc["name"]
        standing = wl["courses"]            # a course tenant lists the CLASS; otherwise the date
        key = stay_key or (courses.slot_key(svc_name, day, w) if standing else courses.date_key(svc_name, day, w))
        ctx = _dep_ctx()
        subs = [s for s in (args.get("subjects") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
        note = str((args.get("details") or {}).get("note") or "")[:300] or None
        results = []
        if subs:
            for s in subs:
                nm = str(s["name"]).strip()
                inst = next((i for i in ctx["instances"]
                             if str(i.get("name") or "").strip().lower() == nm.lower()), None)
                results.append(courses.join(tenant_id, contact, ctx.get("contact_id"), key,
                                            participant=nm, instance_id=(inst or {}).get("id"),
                                            note=note, source=source))
        else:
            results.append(courses.join(tenant_id, contact, ctx.get("contact_id"), key, note=note, source=source))
        bad = next((r for r in results if r.get("error")), None)
        if bad:
            return bad
        pos = [r["position"] for r in results]
        return {"joined": True, "label": courses.label(key), "position": pos[0] if len(pos) == 1 else pos,
                "participants": [s["name"] for s in subs] if subs else None,
                "already_listed": any(r.get("already") for r in results),
                "message": (f"Tell the customer they are number {', '.join(str(p) for p in pos)} on the "
                            f"waiting list for {courses.label(key)}; when a place comes up it is offered "
                            f"to the first waiting and held for {wl['offer_hours']} hours. Do not hold a slot.")}

    # A read the model has already made THIS draft, keyed on (name, args). The
    # diary cannot change between two calls in one draft, so a repeat carries no
    # new information — it is the model circling (live 2026-09-22: the same
    # check_availability for 1 Nov, twice in a row, which spent tokens and
    # printed the same line three times in the ⓘ). Only pure reads are cached;
    # anything that writes goes through every time.
    _READ_ONLY = ("check_availability", "check_appointment_availability")
    _reads: dict[str, dict] = {}

    def execute(name: str, args: dict) -> dict:
        nonlocal booking_profile
        stale_profile = False
        try:
            current_cfg = db.get_scheduling_config(tenant_id) or {}
            if current_cfg != oc.get("cfg"):
                _reads.clear()
                fl.clear()
            oc["cfg"] = copy.deepcopy(current_cfg)
            profile = booking_policy.compile_profile(current_cfg, tenant_id=tenant_id,
                mailbox_id=mailbox_id, fleet=fleet)
            state["booking_profile"] = profile
            state["booking_policy"] = profile.as_policy()
            blocked = booking_policy.refusal(profile)
            if booking_profile is not None and profile.identity != booking_profile.identity:
                stale_profile = True
                # Ready-but-changed policy cannot authorise tools assembled earlier.
                # Unresolved settings retain their existing unsupported outcome.
                blocked = blocked or {"error": "could_not_verify", "outcome": "could_not_verify",
                    "held": False, "owner_review": True,
                    "message": "Booking settings changed. Hold for owner review and rebuild the request."}
            if booking_profile is None:
                booking_profile = profile
        except Exception:
            profile = booking_policy.compile_profile(None, tenant_id=tenant_id,
                mailbox_id=mailbox_id, read_failed=True)
            state["booking_profile"] = profile
            state["booking_policy"] = profile.as_policy()
            blocked = booking_policy.refusal(profile)
        if blocked and (stale_profile or name not in ("cancel_booking", "join_waitlist")):
            if blocked["error"] == "could_not_verify":
                state["verification_unavailable"] = blocked["message"]
            else:
                state["booking_unsupported"] = blocked
            return blocked
        if name == "booking_readiness":
            return {"ready": True, "held": False}
        if name in _READ_ONLY:
            try:
                key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
            except (TypeError, ValueError):
                key = ""
            if key and key in _reads:
                prev = _reads[key]
                return dict(prev, repeated=True, message=(
                    str(prev.get("message") or "") + " You already asked this exact "
                    "question in this draft and the answer has not changed. Do not "
                    "call it again — use the answer you have, or write the reply."))
        # Make-ups (src/app/makeups.py, slice 6): a hold with `makeup_for` books ONE
        # date against a missed occurrence the rule allows — checked here, the
        # booking stamped, priced per the setting and the pair recorded.
        missed = None
        _bk_before = len(state["bookings"])
        if name == "hold_booking" and isinstance(args, dict) and args.get("makeup_for"):
            from app import makeups
            mk = makeups.settings_of(_office_cfg())
            missed, merr = makeups.check(tenant_id, contact, str(args["makeup_for"]), mk,
                                         nouns=diary_config.nouns_of(_office_cfg()))
            if merr:
                return merr
            args = dict(args)
            d = dict(args.get("details") or {})
            d["Make-up for"] = missed.get("booking_ref")
            args["details"] = d
            args.pop("repeat", None)
            args.pop("occurrences", None)
            # The make-up is for whoever missed the class — the record's own
            # name, so the subject gate finds Isla's row however the model
            # spelled her (live 2026-09-05: "Isla Deane" read as a new participant
            # and the reply asked for a date of birth already on file).
            if missed.get("participant"):
                args["subjects"] = [{"name": missed["participant"], **(
                    next((dict(s) for s in (args.get("subjects") or []) if isinstance(s, dict)
                          and str(s.get("name") or "").strip().lower().startswith(str(missed["participant"]).lower())), {}))}]
                args["subjects"][0]["name"] = missed["participant"]
        from app.booking_errors import VerificationUnavailable, capacity_conflict, CAPACITY_MESSAGE
        try:
            out = _execute_inner(name, args)
        except VerificationUnavailable as exc:
            state["verification_unavailable"] = str(exc)
            return {"error": "verification_unavailable", "message": str(exc),
                    "owner_review": True, "held": False}
        except Exception as exc:
            if not capacity_conflict(exc):
                raise
            # A concurrent writer won the place after our availability read.
            # No provider/SQL error text reaches a customer or the model.
            return {"error": "full", "held": False,
                    "message": CAPACITY_MESSAGE + " Check availability again; do not promise these dates."}
        if name in _READ_ONLY and isinstance(out, dict) and not out.get("error"):
            try:
                _reads[f"{name}:{json.dumps(args, sort_keys=True, default=str)}"] = out
            except (TypeError, ValueError):
                pass
        # Grounded booking: a span the diary just confirmed is CHECKED (a hold
        # on it passes check_first if the customer named it); a slot list the
        # diary enumerated is also bookable (docs/plan-grounded-booking.md).
        if grounding is not None and name in ("check_availability", "check_appointment_availability"):
            from app import grounding as _grounding
            _grounding.add_checked(grounding, name, out)
            # A span the customer never named: the diary's answer still goes
            # back, with the line that it cannot be held (the hold itself is
            # refused regardless — this just says so a call earlier).
            _cn = _grounding.check_note(grounding, name, out)
            if _cn and isinstance(out, dict):
                out = dict(out, grounding=_cn)
        if missed is not None and isinstance(out, dict) and out.get("held") and state["bookings"]:
            try:
                from app import makeups
                mk = makeups.settings_of(_office_cfg())
                row = state["bookings"][-1]
                makeups.record(tenant_id, missed, row, mk, contact_email=contact)
                out["makeup_for"] = missed.get("booking_ref")
                if mk["free"]:
                    out["price"] = 0
                    row["quoted_amount"] = 0.0
                out["message"] = ((out.get("message") or "") + f" This is a MAKE-UP for {missed.get('booking_ref')}"
                                  + (" at no charge" if mk["free"] else "") + " — say so in the reply.").strip()
            except Exception:
                logging.getLogger(__name__).warning("make-up record failed (tenant=%s)", tenant_id, exc_info=True)
        # No-charge visits (`no_charge`, set only when the business's own rules say so, e.g. a return
        # visit under its guarantee): every row this call created is stamped free, so no price is quoted,
        # invoiced or auto-drafted for it (live 2026-09-30: a guarantee call-back was told "no charge" in the
        # reply but the booking carried the service's 85 GBP and a stray quote was drafted).
        if (isinstance(args, dict) and args.get("no_charge") and isinstance(out, dict) and "error" not in out
                and name in ("hold_booking", "offer_slots")):
            try:
                for row in state["bookings"][_bk_before:]:
                    db.update_booking_details(row["id"], tenant_id, {"Charge": "No charge"})
                    if row.get("status") != "offered":
                        db.set_booking_quote(row["id"], tenant_id, 0.0)
                        row["quoted_amount"] = 0.0
                out["price"] = 0
                out["message"] = ((out.get("message") or "") + " NO CHARGE for this visit under the business's own "
                                  "rules: say so in the reply, and never quote a price for it.").strip()
            except Exception:
                logging.getLogger(__name__).warning("no-charge stamp failed (tenant=%s)", tenant_id, exc_info=True)
        # The waiting list (src/app/courses.py): a FULL refusal names it when the tenant
        # keeps one, and a hold placed for a waiter closes their list row.
        try:
            if isinstance(out, dict) and name in ("check_availability", "check_appointment_availability",
                                                  "hold_booking", "hold_appointment", "offer_slots"):
                from app import courses
                wl = courses.settings_of(_office_cfg())
                if name == "check_availability" and wl["waitlist"] and out.get("available") is False \
                        and "error" not in out:
                    out["waiting_list"] = (
                        "This business keeps a WAITING LIST: offer the customer a place on it for "
                        "exactly these dates FIRST"
                        + (", then the nearest opening above for a customer who can move"
                           if wl["other_dates"] != "off" else "")
                        + " — and call join_waitlist with this arrival and departure as soon as "
                          "they accept (their email may already ask).")
                elif (name in ("check_availability", "check_appointment_availability")
                      and wl["waitlist"] and "error" not in out):
                    out["waiting_list"] = ("Any time NOT listed here is taken. This business keeps a "
                                           "waiting list: if the customer wanted a time that is not "
                                           "listed, offer them the list for it — and call join_waitlist "
                                           "as soon as they accept (their email may already ask).")
                elif out.get("error") == "full" and wl["waitlist"]:
                    out["message"] = (out.get("message") or "") + (
                        " This business keeps a WAITING LIST: offer the customer a place on it for "
                        "that class, time or appointment (and any free alternative), and once they accept — or if "
                        "they already asked — call join_waitlist.")
                elif out.get("held") and wl["waitlist"] and contact:
                    courses.on_booked(tenant_id, contact, list(state["bookings"]), standing=wl["courses"])
        except Exception:
            logging.getLogger(__name__).warning("waitlist hook failed (tenant=%s)", tenant_id, exc_info=True)
        return out

    def _execute_inner(name: str, args: dict) -> dict:
        # Grounded booking (docs/plan-grounded-booking.md): a date the model
        # cannot point to is refused BEFORE any capacity is touched. Runs only
        # for a tenant with the flag on — `grounding` is None otherwise and
        # the path below is byte-identical to before.
        if grounding is not None:
            from app import grounding as _grounding
            refused = _grounding.check(grounding, name, args)
            if refused:
                return refused
        appt = False
        if name == "check_appointment_availability":
            if not appt_active:
                return {"error": "unsupported", "message": "This diary has no appointment services."}
            return _service_check(args)
        if name == "hold_appointment":
            if not appt_active:
                return {"error": "unsupported", "message": "This diary has no appointment services."}
            name, appt = "hold_booking", True
        if name == "join_waitlist":
            return _join_waitlist(args)
        if name == "cancel_booking":
            return _cancel_booking(args)
        if name == "move_booking":
            return _move_booking(args)
        if name == "check_availability":
            if stay:
                return _stay_check(args)
            if fleet:
                return _fleet_check(args)
            if svc_active:
                return _service_check(args)
            return _windows_check(args)

        if name == "hold_booking":
            # A stays diary's appointments and its stays are different KINDS of
            # visit (docs/plan-diary-multiview.md stage 2): a new subject's introductory
            # visit and its trial night are two bookings for the same subject in one
            # email. Each rule below counts only this email's holds of the SAME
            # kind; a diary without appointments sees every hold, as before.
            same_kind = ([b for b in state["bookings"] if _is_appt_row(b) == appt]
                         if stay else list(state["bookings"]))
            if stay and not appt:
                # A stay email may legitimately book SEVERAL separate visits
                # (live e2e 2026-09-02: a day visit AND an overnight weekend in
                # one email — the one-hold gate forced the model to drop one).
                # Up to three holds, one per distinct visit; an overlapping
                # span is the same visit twice and is refused.
                if len(same_kind) >= STAY_HOLDS_MAX:
                    return {"error": "hold_limit",
                            "message": f"{STAY_HOLDS_MAX} holds are already placed for "
                                       "this email — that is the limit. Reference the "
                                       "existing holds in your reply."}
                arr, dep, terr = _stay_times(args)
                if terr:
                    return terr
                lo, hi = slotgrid.datetime_to_abs(arr), slotgrid.datetime_to_abs(dep)
                again = _gathering_match(args, stay_kind=True)
                for held in same_kind:
                    if held is again:
                        continue         # the same request again: _intake_gated reuses it
                    span = slotgrid.stay_span(held)
                    if span and lo < span[0] + span[1] and span[0] < hi:
                        ref = held.get("booking_ref") or held["id"]
                        return {"error": "already_held", "booking_id": held["id"],
                                "message": f"A hold covering those dates is already placed "
                                           f"for this email ({ref}) — never hold the same "
                                           "visit twice. Reference it in your reply."}
                # …and never a visit the diary ALREADY holds for this customer
                # (a confirmed stay re-held on a details-only follow-up, 2026-09-20).
                dup = _diary_duplicate(args, appt)
                if dup:
                    return dup
                return _intake_gated(_stay_hold, args)
            # One hold per VISIT, enforced HERE in code (the prompt merely explains
            # it): a second hold on the same email is refused — unless it is about a
            # DIFFERENT dependant (a second named asset after the first), which is its own visit.
            if same_kind and _gathering_match(args, stay_kind=False) is not None:
                same_kind = []           # the same request again: _intake_gated reuses it
            if same_kind:
                held = same_kind[0]
                if held.get("status") == "offered":
                    return {"error": "already_offered",
                            "message": "You already reserved offered slots for this email "
                                       "— the customer picks one by replying; do not also "
                                       "place a hold. List the offered slots and their "
                                       "deadline in your reply."}
                wanted = _subject_names(args)
                already = set().union(*(set(b.get("_subject_names") or ()) for b in same_kind))
                distinct = bool(wanted) and bool(already) and not (wanted & already)
                # …or a DIFFERENT job address (Kris 2026-09-26, services rig: an
                # agent's three safety certificates — three houses —
                # got one hold and "our diary only allows one booking per email").
                place = _place_of(args.get("details"))
                places = [_place_of(b.get("details")) for b in same_kind]
                distinct = distinct or (bool(place) and all(places) and place not in places)
                if not distinct:
                    return {"error": "already_held",
                            "booking_id": held["id"],
                            "message": (
                                "A hold is already placed for this email — one per visit. Reference the existing hold in your "
                                "reply. (A second hold is allowed only for a DIFFERENT dependant named in `subjects` — another "
                                "separately named subject — or a DIFFERENT job address in `details`, never the same one twice.)"
                            )}
                if len(same_kind) >= SUBJECT_HOLDS_MAX:
                    return {"error": "hold_limit",
                            "message": f"{SUBJECT_HOLDS_MAX} holds are already placed for "
                                       "this email — that is the limit. Reference the "
                                       "existing holds in your reply."}
            if fleet:
                return _fleet_hold(args)
            # The live-diary gate: the same date and window the customer already
            # holds is that visit again, not a new one.
            dup = _diary_duplicate(args, appt)
            if dup:
                return dup
            if svc_active or appt:
                return _intake_gated(_service_hold, args)
            return _intake_gated(_windows_hold, args)

        # When it's full, the business chose how alternatives go
        # (docs/plan-when-its-full.md). The CONTROL is the agent's context:
        # on offer/off it is never handed offer_stay and nothing it reads says
        # to reserve an alternative (tools_for_other_dates, agent._diary_rules).
        # This is only the backstop, and it firing means the context failed —
        # so it is logged. offer_slots is not gated: a slots diary also uses it
        # for "you said Tuesday — here are three times", which is not "full".
        # The space reserved while a customer's details are gathered is their
        # own request, not an alternative, and never comes through here.
        if name == "offer_stay":
            other = _other_dates(_office_cfg())
            if other in ("offer", "off"):
                logging.getLogger(__name__).warning(
                    "other_dates backstop: offer_stay called on a '%s' tenant (tenant=%s) — "
                    "the context should have prevented this", other, tenant_id)
            if other == "offer":
                return {"error": "offers_not_held",
                        "message": "This business does not reserve alternatives. Name the free "
                                   "dates/times in your reply and ask whether one suits; hold it "
                                   "with hold_booking only once the customer says yes."}
            if other == "off":
                return {"error": "no_other_dates",
                        "message": "This business does not offer other dates. Tell the customer "
                                   "what they asked for is full"
                                   + (" and offer the waiting list." if _waitlist_on() else ".")}

        if name == "offer_stay":
            if not stay:
                return {"error": "wrong_tool",
                        "message": "offer_stay is for span bookings — use offer_slots."}
            # One booking action per email, exactly as offer_slots: a hold OR one
            # offer set, never both — among STAYS. A meet & greet held in the same
            # email is a different kind of visit and never blocks the stay offer.
            stays_held = [b for b in state["bookings"] if not _is_appt_row(b)]
            if stays_held:
                held = stays_held[0]
                if held.get("status") == "offered":
                    return {"error": "already_offered",
                            "message": "You already reserved alternatives for this email — "
                                       "one offer_stay call per email. Name those dates and "
                                       "their deadline in your reply."}
                return {"error": "already_held",
                        "booking_id": held["id"],
                        "message": "A stay is already held for this email — do not also "
                                   "offer alternatives. Reference the held booking."}
            # An offer is an exclusive reservation, so it gathers the same required
            # details a hold does (the 2026-09-03 lesson: offers placed with no
            # intake had the gate bite only at acceptance).
            return _intake_gated(_stay_offer_place, args)

        if name == "offer_slots":
            # One booking action per email: a hold OR one offer set, never both.
            if state["bookings"]:
                held = state["bookings"][0]
                if held.get("status") == "offered":
                    return {"error": "already_offered",
                            "message": "You already reserved an offer set for this email — "
                                       "one offer_slots call per email. List those slots "
                                       "and their deadline in your reply."}
                return {"error": "already_held",
                        "booking_id": held["id"],
                        "message": "A hold is already placed for this email — do not also "
                                   "offer alternatives. Reference the held booking in "
                                   "your reply."}
            if stay:
                return {"error": "wrong_tool",
                        "message": "This diary books SPANS, not slots — use offer_stay "
                                   "with {arrival, departure} to reserve alternatives."}
            if fleet:
                return {"error": "unsupported",
                        "message": "This diary does not take offered-slot reservations — "
                                   "offer the available dates in your reply and ask the "
                                   "customer to confirm."}
            # Offers are exclusive reservations, so they gather the same required
            # details a hold does (live 2026-09-03: BK-1001..3 were offered with
            # no email, permit or visit type on them and the gate only bit at
            # acceptance).
            if svc_active:
                return _intake_gated(_service_offer, args)
            return _intake_gated(_windows_offer, args)

        return {"error": "unknown_tool", "message": f"No tool named {name}."}

    return execute, state


# ─────────────────────────────────────────────────────────────────────────────
# Field Service (Trades) capacity — team members (docs/modules/module-field-service.md)
# ─────────────────────────────────────────────────────────────────────────────
# The team member sibling of the fleet model above: a job's DURATION IN MINUTES fills
# an person-day the way load-unit SPACES fill a resource-day, and a SKILL gates who can
# take it the way a branch gates a load. The pure capacity math here mirrors
# _day_loads / best_fit / fleet_availability, reusing the SAME date discipline
# (working days, notice, horizon, blackouts via _fleet_cfg/_norm). The atomic
# decision itself lives in the hold_engineer_booking RPC; this layer can only ever be
# too cautious, never wrong. Used by the field-service specialist (app/agents/
# field_service.py) through make_engineer_executor.

def engineer_has_skill(engineer: dict, skill: str | None) -> bool:
    """Does this team member hold `skill`? No skill required → anyone qualifies."""
    if not skill:
        return True
    want = str(skill).strip().lower()
    return any(str(s).strip().lower() == want for s in (engineer.get("skills") or []))


def engineer_day_loads(engineers: list[dict], booked: list[dict], day: date) -> list[dict]:
    """Per-person load for one date: [{engineer, used_min, free_min}]. Only ACTIVE
    field-service bookings (engineer_id set) count; a job on a team member outside the
    given pool simply doesn't appear (a deactivated team member's load can't free pool
    time). Mirrors fleet _day_loads."""
    used: dict = {}
    for b in booked:
        if (b.get("engineer_id")
                and b.get("status", "confirmed") in ACTIVE_STATUSES
                and _as_date(b["booking_date"]) == day):
            used[b["engineer_id"]] = used.get(b["engineer_id"], 0) + int(b.get("duration_minutes") or 0)
    out = []
    for e in engineers:
        u = used.get(e["id"], 0)
        cap = int(e.get("daily_minutes") or 0)
        out.append({"engineer": e, "used_min": u, "free_min": cap - u})
    return out


def engineer_best_fit(loads: list[dict], duration: int, skill: str | None = None) -> dict | None:
    """The team member a job goes to: among those who hold the required skill AND have
    enough free minutes, the SMALLEST remaining day that still fits (ties → smallest
    day, then id — deterministic), so team members with a big free gap stay open for big
    jobs. None = nobody fits. Mirrors best_fit + the hold_engineer_booking RPC."""
    fits = [l for l in loads
            if engineer_has_skill(l["engineer"], skill) and l["free_min"] >= duration]
    if not fits:
        return None
    return min(fits, key=lambda l: (l["free_min"],
                                    int(l["engineer"].get("daily_minutes") or 0),
                                    str(l["engineer"]["id"])))


def engineer_availability(
    cfg: dict | None, engineers: list[dict], booked: list[dict],
    date_from, date_to, now: datetime,
    duration: int | None = None, skill: str | None = None,
) -> list[dict]:
    """Bookable dates in [date_from, date_to] against the team member pool:
    [{date, free_minutes}] where free_minutes is the LARGEST single-person gap that
    day — the honest number for one job (team-total free time would overpromise across
    fragmented days). With a job given (duration/skill), only dates where a qualifying
    team member can actually fit it are returned, marked fits:true. Working days,
    blackouts, notice and horizon filter exactly as fleet ('day' starts midnight)."""
    c = _norm(_fleet_cfg(cfg))
    now = now.astimezone(DIARY_TZ)
    cutoff = now + timedelta(hours=c["min_notice_hours"])
    start = max(_as_date(date_from), now.date())
    end = min(_as_date(date_to), now.date() + timedelta(days=c["horizon_days"]))

    out = []
    day = start
    while day <= end:
        if (day.weekday() in c["working_days"] and day not in c["blackouts"]
                and _window_start(day, "day") >= cutoff):
            loads = engineer_day_loads(engineers, booked, day)
            qualified = [l for l in loads if engineer_has_skill(l["engineer"], skill)]
            largest = max((l["free_min"] for l in qualified), default=0)
            if duration is not None:
                if engineer_best_fit(loads, duration, skill):
                    out.append({"date": day.isoformat(),
                                "free_minutes": largest, "fits": True})
            elif largest > 0:
                out.append({"date": day.isoformat(), "free_minutes": largest})
        day += timedelta(days=1)
    return out


def _job_type_line(jt: dict) -> str:
    s = f"{jt.get('name')} ({int(jt.get('default_minutes') or 0)} min"
    if jt.get("required_skill"):
        s += f", needs {jt['required_skill']}"
    s += ")"
    return s + " [emergency]" if jt.get("emergency") else s


def fsm_diary_line(cfg: dict | None, engineers: list[dict], branches: list[dict],
                   job_types: list[dict] | None = None) -> str:
    """The one-line diary shape in its field-service flavour — injected into the
    specialist's prompt. Names the branches (so the agent can match 'your Leeds team'
    to a branch) and lists the configured job types with their durations/skills (so the
    agent maps the customer's words to a known job)."""
    merged = _merged(cfg)
    days = ", ".join(t.strip().title() for t in str(merged["working_days"]).split(",") if t.strip())
    active = [e for e in engineers if e.get("active", True)]
    if not active:
        return (f"Engineer diary: no engineers configured yet, so capacity is unknown; "
                f"working days {days}.")
    line = (f"Engineer diary: {len(active)} engineer{'s' if len(active) != 1 else ''}, "
            f"bookable by the day in minutes")
    names = [b.get("name") or "" for b in branches]
    if len(names) > 1:
        line += f", across {len(names)} areas ({', '.join(names)})"
    line += f"; working days {days}."
    if job_types:
        line += "\nJob types: " + "; ".join(_job_type_line(jt) for jt in job_types) + "."
    return line


# Field-service flavour of the agent tools — distinct names (the base agent never sees
# these; only the specialist does), sized in minutes and gated by skill. Static
# constants for the same prompt-cache reason as the windows/fleet tools. Replies talk
# dates, job types and references — NEVER the individual team member (internal, like vans).
FSM_TOOLS = [
    {
        "name": "check_engineer_availability",
        "description": (
            "Check the business's real engineer diary for free capacity BEFORE offering, "
            "agreeing to, or declining ANY date for a job, callout or site visit — never "
            "rely on memory. Returns every bookable date between date_from and date_to "
            "(inclusive) as {date, free_minutes}, the largest single-engineer gap that "
            "day. Pass the job_type so only dates a suitably-skilled engineer can actually "
            "fit the job are returned, marked fits:true. Any date NOT in the result is not "
            "bookable (closed, full, too short notice, or beyond the booking horizon)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "format": "date",
                              "description": "Start of the range to check, YYYY-MM-DD."},
                "date_to": {"type": "string", "format": "date",
                            "description": "End of the range (inclusive), YYYY-MM-DD."},
                "job_type": {"type": "string",
                             "description": "The job as one of the configured job types, so "
                                            "the right duration and skill are used."},
                "branch": {"type": "string",
                           "description": "Branch/area name — ONLY when the customer names one."},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_job",
        "description": (
            (
                "Place ONE provisional hold in the engineer diary for this email's job. You MUST call this "
                "whenever the customer asks to book a date and check_engineer_availability shows the job fits "
                "— do not merely tell them it is available, and never say a job is booked unless this tool "
                "returned held:true. One hold per visit — if the email is about several dependants (several "
                "named subjects) hold each one separately, one call per subject, up to three. The hold really "
                "reserves an engineer's time; a human confirms or releases it later. Returns the booking "
                "reference, or an error explaining why it cannot be held."
            )
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The job date, YYYY-MM-DD."},
                "job_type": {"type": "string",
                             "description": "The job as one of the configured job types."},
                "priority": {"type": "string", "enum": ["standard", "emergency"],
                             "description": "'emergency' for an urgent callout (no heating/hot "
                                            "water, leak, gas smell, no power, lockout). Omit "
                                            "for standard work."},
                "summary": {"type": "string",
                            "description": "One line describing the job, e.g. 'Boiler not "
                                           "firing — no hot water, LS9'."},
                "site_postcode": {"type": "string",
                                  "description": "The job site's postcode — ONLY when the "
                                                 "email states one. Never guess."},
                "details": {"type": "object",
                            "description": "Structured facts from the email (address, access "
                                           "notes, make/model, the customer's reference). "
                                           "Include what the engineer would need on site."},
            },
            "required": ["date", "job_type", "summary"],
            "additionalProperties": False,
        },
    },
]


def make_engineer_executor(tenant_id: str, contact: str | None = None,
                           thread_id: str | None = None, source: str = "email",
                           now: datetime | None = None, mailbox_id: str | None = None):
    """The field-service tool executor for one email: (name, args) -> JSON-able dict,
    plus state. The team member sibling of make_executor's fleet path — its own capacity
    model (person-day minutes + skills), the SAME one-hold-per-email rule (enforced
    HERE in code), and state['bookings'] so the pipeline links/resolves the hold exactly
    as before. The job is sized from its job_type (the executor owns the lookup — the
    model never invents a duration), and branch resolution reuses the fleet chain."""
    state: dict = {"bookings": []}
    fs: dict = {}

    def _ctx() -> dict:
        if not fs:
            fs["cfg"] = db.get_scheduling_config(tenant_id)
            fs["branches"] = db.list_branches(tenant_id)
            fs["engineers"] = db.list_engineers(tenant_id, active_only=True)
            fs["job_types"] = db.list_job_types(tenant_id)
            fs["rate"] = db.get_fsm_rate_card(tenant_id)
            fs["default_branch"] = (
                db.get_mailbox_default_branch(mailbox_id) if mailbox_id else None
            )
        return fs

    def _pool(args: dict):
        f = _ctx()
        branch_id, err, names = resolve_branch(f["branches"], args.get("branch"),
                                               f["default_branch"])
        if err == "unknown_branch":
            return None, None, {
                "error": "unknown_branch", "branches": names,
                "message": f"No branch/area called '{args.get('branch')}'. The areas are: "
                           f"{', '.join(names)}. Use one of those, or omit `branch` and ask "
                           "the customer which area they need."}
        if err == "branch_required":
            return None, None, {
                "error": "branch_required", "branches": names,
                "message": "This business works per area and the email doesn't say which. "
                           f"Ask the customer which area — the options are: {', '.join(names)}. "
                           "Do not promise capacity until the area is known."}
        pool = [e for e in f["engineers"]
                if branch_id is None or e.get("branch_id") == branch_id]
        if not pool:
            return None, None, {
                "error": "not_configured",
                "message": "No engineers are configured for this diary yet, so capacity is "
                           "unknown. Do not promise or decline any date — say a colleague "
                           "will confirm the booking."}
        return pool, branch_id, None

    def _resolve_job(args: dict):
        """Match args['job_type'] to a configured job_type → (job dict, None) or
        (None, error) asking which service when it doesn't match (never guess a size)."""
        f = _ctx()
        want = str(args.get("job_type") or "").strip().lower()
        match = next((j for j in f["job_types"]
                      if str(j.get("name") or "").strip().lower() == want), None)
        if not match:
            names = [j.get("name") for j in f["job_types"]]
            return None, {
                "error": "unknown_job_type", "job_types": names,
                "message": ("Which service is this? It must be one of the configured job "
                            "types: " + (", ".join(names) if names else "(none set up yet)")
                            + ". Ask the customer to confirm the service, then use the "
                            "matching job_type.")}
        return {"name": match["name"], "duration": int(match.get("default_minutes") or 60),
                "skill": match.get("required_skill"),
                "price": match.get("default_price"),
                "emergency": bool(match.get("emergency"))}, None

    def _check(args: dict) -> dict:
        try:
            start, end = _as_date(args.get("date_from")), _as_date(args.get("date_to"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "Dates must be YYYY-MM-DD."}
        pool, _branch_id, err = _pool(args)
        if err:
            return err
        duration = skill = None
        job = None
        if str(args.get("job_type") or "").strip():
            job, jerr = _resolve_job(args)
            if jerr:
                return jerr
            duration, skill = job["duration"], job["skill"]
        booked = db.list_active_bookings(tenant_id, start.isoformat(), end.isoformat())
        slots = engineer_availability(_ctx()["cfg"], pool, booked, start, end,
                                      now or datetime.now(DIARY_TZ),
                                      duration=duration, skill=skill)
        out = {"available": slots}
        if duration is not None:
            out["duration_minutes"] = duration
        if job is not None:
            # An ESTIMATE so the agent can quote the cost before booking (D3).
            out["quote"] = fsm_pricing.quote(_ctx()["rate"], duration, job_price=job["price"])
        if not slots:
            earliest = earliest_bookable_day(_ctx()["cfg"], now or datetime.now(DIARY_TZ))
            if skill and not any(engineer_has_skill(e, skill) for e in pool):
                out["message"] = (
                    f"No engineer is configured with the '{skill}' skill, so this job "
                    "cannot be booked here. Do not offer a date — say a colleague will be "
                    "in touch to arrange it.")
            elif earliest and end < earliest:
                notice = _norm(_fleet_cfg(_ctx()["cfg"]))["min_notice_hours"]
                out["earliest_bookable_date"] = earliest.isoformat()
                out["message"] = (
                    f"Nothing that soon can be booked: the diary needs {notice} hours' "
                    f"notice, so the earliest possible date is {earliest.isoformat()}. This "
                    "is NOT 'fully booked' — tell the customer it is too short notice and "
                    f"check/offer dates from {earliest.isoformat()} in THIS reply.")
            else:
                out["message"] = (
                    "No bookable dates for that job in this range. Check the FOLLOWING days "
                    "now and offer the customer the nearest dates that fit in THIS reply — "
                    "never promise to check and get back to them.")
        return out

    def _book(args: dict) -> dict:
        pool, branch_id, err = _pool(args)
        if err:
            return err
        job, jerr = _resolve_job(args)
        if jerr:
            return jerr
        try:
            day = _as_date(args.get("date"))
        except (TypeError, ValueError):
            return {"error": "invalid_date", "message": "The date must be YYYY-MM-DD."}
        derr = hold_error(_fleet_cfg(_ctx()["cfg"]), day, "day", now or datetime.now(DIARY_TZ))
        if derr:
            return {"error": derr, "message": _HOLD_ERROR_MESSAGES[derr]}
        priority = str(args.get("priority") or "").strip().lower()
        if priority not in ("standard", "emergency"):
            priority = "emergency" if job["emergency"] else "standard"
        details = dict(args.get("details") or {})
        site = str(args.get("site_postcode") or "").strip().upper()
        if site:
            details["site_postcode"] = site[:16]
        row = db.hold_engineer_booking(
            tenant_id, day.isoformat(),
            (args.get("summary") or "").strip() or job["name"],
            job["duration"], required_skill=job["skill"], job_type=job["name"],
            priority=priority, branch_id=branch_id, details=details or None,
            contact=contact, source=source, thread_id=thread_id,
        )
        if not row:
            return {"error": "full",
                    "message": "No engineer can take that job on that date — the capacity "
                               "or skill isn't there. Call check_engineer_availability with "
                               "the job_type and offer the nearest dates that fit."}
        # Price the held job and stamp the PRE-VAT net on the booking (the same
        # convention as TMS quoted_amount — invoicing adds VAT, reconstructing the
        # inc-VAT total the customer was quoted). The full quote (with inc-VAT total)
        # goes back to the agent to state.
        q = fsm_pricing.quote(_ctx()["rate"], job["duration"], job_price=job["price"])
        row["quoted_amount"] = q["net"]
        try:
            db.set_booking_quote(row["id"], tenant_id, q["net"])
        except Exception:
            pass   # a quote-stamp failure must never lose the hold — invoicing recomputes
        state["bookings"].append(row)
        # The team member the RPC picked is internal — never in the result (like vans in
        # TMS). booking_ref IS for the customer: the reference they quote on follow-up.
        return {"held": True, "booking_id": row["id"],
                "booking_ref": row.get("booking_ref"), "date": row["booking_date"],
                "job_type": job["name"], "duration_minutes": job["duration"],
                "priority": priority, "quote": q, "status": "proposed"}

    def execute(name: str, args: dict) -> dict:
        readiness, _ = make_executor(tenant_id, fleet=True)
        blocked = readiness("booking_readiness", {})
        if blocked.get("error"):
            return blocked
        if name == "check_engineer_availability":
            return _check(args)
        if name == "book_job":
            # One hold per email, enforced HERE in code (the prompt merely explains it).
            if state["bookings"]:
                held = state["bookings"][0]
                return {"error": "already_held", "booking_id": held["id"],
                        "message": "A hold is already placed for this email — at most one "
                                   "per email. Reference the existing hold in your reply."}
            return _book(args)
        return {"error": "unknown_tool", "message": f"No tool named {name}."}

    return execute, state


def fsm_board(tenant_id: str, client=None, now: datetime | None = None, days: int = 7) -> dict:
    """Everything the /app/fsm team member cockpit renders: a rolling `days`-day window
    from today, each day showing every active team member's load (booked vs daily minutes)
    and their job chips, emergencies flagged. Read-only view-model in the house style —
    rows in, render-ready dict out. The board's working set is the active (proposed +
    confirmed) field-service jobs (engineer_id set); completed jobs flow to invoicing."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    cfg = db.get_scheduling_config(tenant_id, client=client)
    engineers = db.list_engineers(tenant_id, client=client)
    active = [e for e in engineers if e.get("active", True)]
    branches = db.list_branches(tenant_id, client=client)
    job_types = db.list_job_types(tenant_id, client=client)
    rows = [b for b in db.list_bookings(tenant_id, now.date().isoformat(), client=client)
            if b.get("engineer_id")]

    out_days = []
    for i in range(max(1, days)):
        d = now.date() + timedelta(days=i)
        loads = engineer_day_loads(active, rows, d)
        eng_views = []
        for l in loads:
            e = l["engineer"]
            ejobs = sorted(
                (b for b in rows if b.get("engineer_id") == e["id"]
                 and _as_date(b["booking_date"]) == d),
                key=lambda b: str(b.get("created_at") or ""))
            cap = int(e.get("daily_minutes") or 0)
            eng_views.append({
                "engineer": e,
                "used_min": l["used_min"], "free_min": l["free_min"], "daily_minutes": cap,
                "pct": min(100, round(100 * l["used_min"] / cap)) if cap else 0,
                "jobs": [{
                    "id": b["id"], "ref": b.get("booking_ref"),
                    "summary": (b.get("summary") or "Job")[:60],
                    "job_type": b.get("job_type"),
                    "minutes": int(b.get("duration_minutes") or 0),
                    "priority": b.get("priority") or "standard",
                    "status": b.get("status"), "contact": b.get("contact"),
                    "quoted_amount": b.get("quoted_amount"),
                } for b in ejobs],
            })
        out_days.append({
            "date": d.isoformat(), "label": d.strftime("%a %d %b"),
            "is_today": d == now.date(),
            "engineers": eng_views,
            "total_jobs": sum(len(ev["jobs"]) for ev in eng_views),
            "emergencies": sum(1 for ev in eng_views for j in ev["jobs"]
                               if j["priority"] == "emergency"),
        })
    return {
        "days": out_days,
        "summary": fsm_diary_line(cfg, engineers, branches, job_types),
        "engineers": active, "has_engineers": bool(active),
        "job_types": job_types,
    }


def move_booking(tenant_id: str, booking_id: str, new_date, new_window: str,
                 now: datetime | None = None) -> tuple[dict | None, str | None]:
    """Move one live booking to a new date/window (the concierge's confirmed
    action). Same contract as place_hold: (updated_row, None) on success or
    (None, error_code) — hold_error()'s codes plus 'full' when the atomic
    capacity check loses the slot. The row is updated in place, so booking_ref,
    details and calendar linkage survive; the caller refreshes the calendar
    event (delete + re-push) afterwards."""
    readiness, _ = make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        return None, blocked["error"]

    now = now or datetime.now(DIARY_TZ)
    cfg = db.get_scheduling_config(tenant_id)
    day = _as_date(new_date)
    err = hold_error(cfg, day, new_window, now)
    if err:
        return None, err
    from app.booking_errors import capacity_conflict
    try:
        row = db.move_booking_if_capacity(tenant_id, booking_id, day.isoformat(), new_window)
    except Exception as exc:
        if capacity_conflict(exc):
            return None, "full"
        raise
    if not row:
        return None, "full"
    return row, None
