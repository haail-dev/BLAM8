"""Unified slot-grid — the one calendar primitive (docs/architecture/unified-slot-grid.md).

Every diary, every industry, is a flat grid of **15-minute blocks**. A day is 96
blocks (24h × 4); block ``i`` spans ``[i*15, (i+1)*15)`` minutes from midnight. A
booking of any length is just ``length_blocks`` consecutive blocks starting at
``start_block`` — 30 min = 2 blocks, 1 h = 4, 2 h = 8, a full day = 96. The slot
size a tenant books in is *config* (``block_minutes``, a multiple of 15), never
schema, so the database never changes shape per industry.

This module is **pure**: config + a resource's existing bookings in, answers out.
No DB, no clock of its own (the caller passes ``now``/``day``), no module wiring —
it is the foundation the office, team member and fleet resource diaries will each sit on
(Phases 2–4). All diary semantics that can be tested without a database live here.

A booking, to this module, is a ``(start_block, length_blocks)`` pair (a tuple, or
a dict/object exposing those keys/attrs). A *resource* is whoever the block grid
belongs to — the business, a team member, a fleet resource — optionally carrying skills (FSM)
or non-time capacity (TMS, handled by callers in Phase 4, not here).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

BLOCK_MIN = 15                     # the atomic unit: 15 minutes
BLOCKS_PER_DAY = 24 * 60 // BLOCK_MIN   # 96
NOON_BLOCK = 12 * 60 // BLOCK_MIN       # 48

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DOW_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── unit conversions ─────────────────────────────────────────────────────────
def blocks_for_minutes(minutes: int | float) -> int:
    """Whole 15-min blocks a duration needs, rounded UP so a job is never
    under-allocated (a 50-min job takes 4 blocks = 60 min). Always ≥ 1."""
    return max(1, math.ceil((minutes or 0) / BLOCK_MIN))


def minutes_for_blocks(n: int) -> int:
    return int(n) * BLOCK_MIN


def normalize_block_minutes(minutes: int | float | None) -> int:
    """A tenant's chosen slot size, snapped to the grid: rounded to the nearest
    15-min multiple and clamped to [15 min, 24 h]. ``None``/garbage → 15."""
    try:
        m = int(round(float(minutes) / BLOCK_MIN)) * BLOCK_MIN
    except (TypeError, ValueError):
        return BLOCK_MIN
    return max(BLOCK_MIN, min(BLOCKS_PER_DAY * BLOCK_MIN, m))


def block_at(t: time | str) -> int:
    """The block index a wall-time falls in (09:00 → 36, 09:14 → 36, 09:15 → 37)."""
    t = _as_time(t)
    return (t.hour * 60 + t.minute) // BLOCK_MIN


def hhmm(block: int) -> str:
    """'HH:MM' for a block boundary. Block 96 renders '24:00' (end of day)."""
    mins = int(block) * BLOCK_MIN
    return f"{mins // 60:02d}:{mins % 60:02d}"


def time_at(block: int) -> time:
    """The wall-time at a block's start, for 0..95 (96 has no ``time`` — use hhmm)."""
    mins = int(block) * BLOCK_MIN
    return time(mins // 60, mins % 60)


def label(start_block: int, length_blocks: int) -> str:
    """'09:00–10:00' for a slot."""
    return f"{hhmm(start_block)}–{hhmm(start_block + length_blocks)}"


# ── parsing helpers (kept local so the module stays dependency-free) ──────────
def _as_time(value) -> time:
    return value if isinstance(value, time) else time.fromisoformat(str(value).strip())


def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) \
        else (value.date() if isinstance(value, datetime)
              else date.fromisoformat(str(value).strip()))


def _working_days(value) -> set[int]:
    return {_WEEKDAYS[tok] for tok in str(value).lower().replace(" ", "").split(",")
            if tok in _WEEKDAYS}


def _blackouts(value) -> set[date]:
    out: set[date] = set()
    for d in (value or []):
        try:
            out.add(_as_date(d))
        except (TypeError, ValueError):
            continue
    return out


# ── the grid the config describes ─────────────────────────────────────────────
def hours_for_day(config: dict, day=None) -> tuple[str, str]:
    """The (open_time, close_time) strings for a date: a ``day_hours`` override for that
    weekday if set, else the standard ``open_time``/``close_time``. ``day`` None → standard
    (so every existing caller is unchanged)."""
    o = config.get("open_time")
    c = config.get("close_time")
    if day is not None:
        ov = ((config or {}).get("day_hours") or {}).get(_DOW_ORDER[_as_date(day).weekday()])
        if isinstance(ov, dict) and ov.get("open") and ov.get("close"):
            o, c = ov["open"], ov["close"]
    return o, c


def open_close_blocks(config: dict, day=None) -> tuple[int, int]:
    """[open_block, close_block) for the bookable day. With ``day`` and a per-weekday
    ``day_hours`` override, that day's hours win; otherwise the standard hours. A nonsensical
    span (close ≤ open, bad times) or unset hours produces an empty grid."""
    o_str, c_str = hours_for_day(config, day)
    try:
        o = block_at(o_str)
        c_t = _as_time(c_str)
        c = (c_t.hour * 60 + c_t.minute + BLOCK_MIN - 1) // BLOCK_MIN  # round up
    except (TypeError, ValueError):
        return 0, 0
    if not 0 <= o < c <= BLOCKS_PER_DAY:
        return 0, 0
    return o, c


def day_slots(config: dict, day=None) -> list[tuple[int, int]]:
    """The bookable slots for one day as ``(start_block, length_blocks)`` aligned to
    the tenant's block size, within opening hours (per-day hours when ``day`` is given).
    Driven by ``block_minutes`` (a 15-min multiple); the legacy ``windows`` mode is honoured
    as a presentation: 'day' → one whole-day slot, 'ampm' → two halves. Capped at 96 slots."""
    o, c = open_close_blocks(config, day)
    if c <= o:
        return []
    mode = config.get("windows")
    if mode == "day":
        return [(o, c - o)]
    if mode == "ampm":
        mid = min(max(o, NOON_BLOCK), c)
        halves = [(o, mid - o), (mid, c - mid)]
        return [(s, n) for s, n in halves if n > 0]
    length = blocks_for_minutes(normalize_block_minutes(
        config.get("block_minutes", config.get("slot_minutes", 60))))
    if length >= c - o:                       # slot ≥ the day → one whole-day slot
        return [(o, c - o)]
    slots, s = [], o
    while s + length <= c and len(slots) < BLOCKS_PER_DAY:
        slots.append((s, length))
        s += length
    return slots


def day_service_starts(config: dict, length_blocks: int, day=None) -> list[int]:
    """Candidate start blocks on a day for a service of ``length_blocks`` (a service =
    ceil(minutes/15) blocks — app/services). Appointments begin on the tenant's slot grid
    (stepping by ``block_minutes``) but occupy their OWN length, so a 90-min service in a
    30-min-slot diary can start 09:00, 09:30, 10:00 … as long as the whole span fits inside
    opening hours (per-day hours when ``day`` is given). Pure — config + length in, aligned
    starts out (capped at 96)."""
    o, c = open_close_blocks(config, day)
    length = max(1, int(length_blocks))
    step = blocks_for_minutes(normalize_block_minutes(
        config.get("block_minutes", config.get("slot_minutes", 60))))
    starts, s = [], o
    while s + length <= c and len(starts) < BLOCKS_PER_DAY:
        starts.append(s)
        s += step
    return starts


# ── overlap + availability (pure) ─────────────────────────────────────────────
def overlaps(s1: int, l1: int, s2: int, l2: int) -> bool:
    return s1 < s2 + l2 and s2 < s1 + l1


def _span(b) -> tuple[int, int] | None:
    """(start_block, length_blocks) from a tuple/list or a dict/object exposing
    start_block + length_blocks. None (skipped) when either is missing."""
    if isinstance(b, (tuple, list)):
        return (int(b[0]), int(b[1])) if len(b) >= 2 and b[0] is not None else None
    get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
    s, n = get("start_block"), get("length_blocks")
    return (int(s), int(n)) if s is not None and n else None


def slot_is_free(start_block: int, length_blocks: int, resource_bookings) -> bool:
    """True when ``[start_block, +length)`` overlaps none of a resource's bookings."""
    for b in resource_bookings or []:
        span = _span(b)
        if span and overlaps(start_block, length_blocks, span[0], span[1]):
            return False
    return True


def date_bookable(config: dict, day, now: datetime) -> bool:
    """Whole-day gates that don't depend on the time of day: working day, not a
    blackout, within [today, today+horizon]. The per-slot notice cutoff is applied
    separately in :func:`free_slots`."""
    day = _as_date(day)
    today = now.date()
    if day < today or day > today + timedelta(days=int(config.get("horizon_days", 28))):
        return False
    if config.get("working_days") and day.weekday() not in _working_days(config["working_days"]):
        return False
    return day not in _blackouts(config.get("blackout_dates"))


def free_slots(config: dict, resource_bookings, day, now: datetime) -> list[tuple[int, int]]:
    """The bookable slots free for ONE resource on ``day``: the day's slots minus any
    that overlap an existing booking or start before the minimum-notice cutoff. Empty
    when the date itself isn't bookable (closed day / blackout / out of horizon)."""
    if not date_bookable(config, day, now):
        return []
    day = _as_date(day)
    cutoff = now + timedelta(hours=int(config.get("min_notice_hours", 24)))
    out = []
    for start, length in day_slots(config, day):
        start_dt = datetime.combine(day, time_at(start), tzinfo=now.tzinfo)
        if start_dt < cutoff:
            continue
        if slot_is_free(start, length, resource_bookings):
            out.append((start, length))
    return out


def availability(config: dict, resources, bookings_by_resource: dict, day,
                 now: datetime, required_skill: str | None = None) -> list[dict]:
    """Offered slots for a day across one or more resources. A slot is offered when
    at least one (skill-matched) resource is free for it.

    ``resources`` is a list of resource dicts/objects (``id``, optional ``skills``);
    pass ``[None]`` (or ``[]``) for an office tenant with a single implicit resource.
    ``bookings_by_resource`` maps resource id → that resource's bookings. Returns
    ``[{start_block, length_blocks, label, resource_ids}]`` sorted by start."""
    res = list(resources) if resources else [None]
    offered: dict[tuple[int, int], list] = {}
    for r in res:
        if required_skill and not _has_skill(r, required_skill):
            continue
        rid = _rid(r)
        for slot in free_slots(config, bookings_by_resource.get(rid, []), day, now):
            offered.setdefault(slot, []).append(rid)
    return [
        {"start_block": s, "length_blocks": n, "label": label(s, n), "resource_ids": ids}
        for (s, n), ids in sorted(offered.items())
    ]


def _rid(resource):
    if resource is None:
        return None
    return resource.get("id") if isinstance(resource, dict) else getattr(resource, "id", None)


def _has_skill(resource, skill: str) -> bool:
    if resource is None:
        return True
    skills = resource.get("skills") if isinstance(resource, dict) else getattr(resource, "skills", None)
    if not skills:
        return False
    return skill.lower() in {str(s).lower() for s in skills}


# ── office diary ON the grid (parity with scheduling.availability) ────────────
# These reproduce the existing office diary's semantics — {date, window, remaining}
# keyed by the office window strings — but compute occupancy with block overlap
# instead of the bespoke `_overlaps` rules. tests/test_slotgrid_parity.py proves the
# output is identical to scheduling.availability() for 15-min-multiple slot sizes, so
# Phase 2b can swap the live read path onto the grid without behaviour change.
_OFFICE_DEFAULTS = {"windows": "day", "slot_minutes": 60, "min_notice_hours": 24,
                    "horizon_days": 28, "blackout_dates": [], "buffer_minutes": 0}


def _cfgval(cfg: dict | None, key: str):
    v = (cfg or {}).get(key)
    return _OFFICE_DEFAULTS.get(key) if v is None else v


def office_windows(cfg: dict | None, day=None) -> tuple[str, ...]:
    """The office window strings for a config — byte-identical to scheduling._norm's
    window list: 'day' → ('day',), 'ampm' → ('am','pm'), 'slots' → ('09:00', …)
    generated from opening hours and the slot length (which must be a 15-min multiple
    to live on the grid; non-multiples still generate, they just won't grid-align). With
    ``day`` and a per-weekday ``day_hours`` override, the slot list uses that day's hours."""
    mode = _cfgval(cfg, "windows")
    if mode == "ampm":
        return ("am", "pm")
    if mode != "slots":
        return ("day",)
    o_str, c_str = _cfgval(cfg, "open_time"), _cfgval(cfg, "close_time")
    if day is not None:
        ov = ((cfg or {}).get("day_hours") or {}).get(_DOW_ORDER[_as_date(day).weekday()])
        if isinstance(ov, dict) and ov.get("open") and ov.get("close"):
            o_str, c_str = ov["open"], ov["close"]
    o, c = _as_time(o_str), _as_time(c_str)
    step = max(10, min(480, int(_cfgval(cfg, "slot_minutes") or 60)))
    t, end, slots = o.hour * 60 + o.minute, c.hour * 60 + c.minute, []
    while t + step <= end and len(slots) < BLOCKS_PER_DAY:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += step
    return tuple(slots) or ("day",)


def _office_window_start(day, window: str, tz) -> datetime:
    """When a window opens, for the notice cutoff — matches scheduling._window_start:
    a slot carries its real time; 'pm' starts at noon; 'day'/'am' at midnight."""
    if ":" in window:
        h, m = window.split(":")
        return datetime.combine(_as_date(day), time(int(h), int(m)), tzinfo=tz)
    start = time(12, 0) if window == "pm" else time(0, 0)
    return datetime.combine(_as_date(day), start, tzinfo=tz)


def _is_office_booking(b) -> bool:
    """An office row only — a row with `spaces` (transport) or `engineer_id` (field
    service) belongs to another capacity model and never consumes an office window."""
    get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
    return get("spaces") is None and not get("engineer_id")


def window_used(booked, day, window: str, cfg: dict | None, slot_minutes: int,
                buffer_blocks: int = 0) -> int:
    """Active office bookings occupying (day, window), counted on the ABSOLUTE block
    timeline so a multi-day STAY is counted on every day it spans — not just its arrival
    date. A booking with grid columns (start_block/length_blocks) uses its real absolute
    span (appointments stay within a day, so this is identical to before for them); a
    legacy row without grid columns falls back to within-day window overlap on its own
    date. Fleet/field-service rows are excluded (other capacity models).

    `buffer_blocks` (the free travel buffer) widens each existing booking by that many
    15-min blocks on both sides, so a new slot must leave that much gap before/after a
    neighbouring job — i.e. the agent won't book back-to-back without travel time. 0
    (the default, and what the diary view uses) is exact occupancy."""
    wb = window_to_blocks(window, slot_minutes, cfg or {})
    if not wb:
        return 0
    day = _as_date(day)
    lo = abs_block(day, wb[0])
    hi = lo + wb[1]
    used = 0
    for b in booked or []:
        get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
        if not _is_office_booking(b) or get("status", "confirmed") not in ("proposed", "confirmed"):
            continue
        span = stay_span(b)
        if not span:
            bb = window_to_blocks(get("window"), slot_minutes, cfg or {})
            if not bb or _as_date(get("booking_date")) != day:
                continue
            span = (abs_block(day, bb[0]), bb[1])
        bs, bl = span
        if (bs - buffer_blocks) < hi and lo < (bs + bl + buffer_blocks):
            used += 1
    return used


def office_availability(cfg: dict | None, booked, date_from, date_to, now: datetime) -> list[dict]:
    """Grid-computed twin of scheduling.availability(): bookable office slots in
    [date_from, date_to] as [{date, window, remaining}] where remaining > 0, gated by
    working days, blackouts, notice and horizon — occupancy via the block grid."""
    from app.booking_policy import compile_policy
    if not compile_policy(cfg)["ready"]:
        return []
    cap = int(_cfgval(cfg, "capacity_per_window") or 0)
    slot_minutes = max(10, min(480, int(_cfgval(cfg, "slot_minutes") or 60)))
    # Free travel buffer (minutes → blocks, rounded up): the agent leaves this much gap
    # before/after a neighbouring job so it never books back-to-back without travel time.
    buf = int(_cfgval(cfg, "buffer_minutes") or 0)
    buffer_blocks = -(-buf // BLOCK_MIN) if buf > 0 else 0
    wdays = _working_days(_cfgval(cfg, "working_days"))
    blackouts = _blackouts(_cfgval(cfg, "blackout_dates"))
    cutoff = now + timedelta(hours=int(_cfgval(cfg, "min_notice_hours")))
    start = max(_as_date(date_from), now.date())
    end = min(_as_date(date_to), now.date() + timedelta(days=int(_cfgval(cfg, "horizon_days"))))
    out, day = [], start
    while day <= end:
        if day.weekday() in wdays and day not in blackouts:
            for w in office_windows(cfg, day):
                if _office_window_start(day, w, now.tzinfo) < cutoff:
                    continue
                remaining = cap - window_used(booked, day, w, cfg, slot_minutes, buffer_blocks)
                if remaining > 0:
                    out.append({"date": day.isoformat(), "window": w, "remaining": remaining})
        day += timedelta(days=1)
    return out


# ── multi-day stays: the continuous block timeline ───────────────────────────
# A booking is N consecutive 15-minute blocks on ONE continuous ribbon across days, so a
# 1-hour appointment (4 blocks) and a 52-hour stay (208 blocks) use IDENTICAL math —
# there is no separate "multi-day" model, just a longer span. The absolute block index
# anchors a within-day block (0..95) to a real date so spans compare across midnight.
# Capacity here is concurrent occupancy: a stay fits when fewer than `capacity` stays
# overlap EVERY block it spans (stays, hire, storage — book by the unit, N units).

def abs_block(day, start_block: int) -> int:
    """A within-day block on a date → its absolute block on the continuous timeline."""
    return _as_date(day).toordinal() * BLOCKS_PER_DAY + int(start_block)


def datetime_to_abs(dt: datetime) -> int:
    """A datetime → the absolute block it falls in (quantised DOWN to the 15-min grid).
    Used for a drop-off/check-in; the matching collect/check-out time, quantised the same
    way, is the EXCLUSIVE end (the first free block), so length = end_abs − start_abs."""
    return dt.date().toordinal() * BLOCKS_PER_DAY + (dt.hour * 60 + dt.minute) // BLOCK_MIN


def abs_to_datetime(abs_b: int) -> datetime:
    """Inverse of :func:`datetime_to_abs` — an absolute block back to its wall-time."""
    from datetime import date as _date
    mins = (abs_b % BLOCKS_PER_DAY) * BLOCK_MIN
    return datetime.combine(_date.fromordinal(abs_b // BLOCKS_PER_DAY), time(mins // 60, mins % 60))


def stay_span(b) -> tuple[int, int] | None:
    """(abs_start, length_blocks) for a booking from booking_date + start_block +
    length_blocks (length may run past a day for a multi-day stay). None if unmapped."""
    get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
    sb, lb = get("start_block"), get("length_blocks")
    if sb is None or not lb:
        return None
    return abs_block(get("booking_date"), sb), int(lb)


# Spans may be (start, length) OR (start, length, qty) — qty defaults to 1, so a plain
# stay/appointment is unweighted exactly as before. A "quantity" booking (hire 5 units,
# a table of 6 covers, 2 subjects in one stay) carries its qty so concurrency is WEIGHTED:
# occupancy at a block is the SUM of quantities covering it, checked against capacity.
def _span_qty(span) -> int:
    return int(span[2]) if len(span) > 2 and span[2] is not None else 1


def expand_spans(spans, buffer_blocks: int):
    """Widen each span by `buffer_blocks` on both sides — the gap between bookings (turnaround
    /turnaround for stays, travel for mobile services). A new booking must then leave that gap
    before AND after a neighbour. 0 (the default) returns the spans unchanged (exact)."""
    if not buffer_blocks:
        return list(spans)
    b = int(buffer_blocks)
    return [(int(s[0]) - b, int(s[1]) + 2 * b, _span_qty(s)) for s in spans]


def stay_span_q(b) -> tuple[int, int, int] | None:
    """:func:`stay_span` plus the booking's quantity as a 3rd element (units consumed /
    party size; ``quantity`` NULL → 1) — the weighted-concurrency form for capacity that
    counts units, not just bookings. None when the row has no grid span."""
    span = stay_span(b)
    if not span:
        return None
    get = b.get if isinstance(b, dict) else lambda k, d=None: getattr(b, k, d)
    q = get("quantity")
    return span[0], span[1], (int(q) if q else 1)


def concurrent_at(spans, abs_b: int) -> int:
    """Occupancy at absolute block `abs_b`: the SUM of quantities of the spans covering it
    (each span counts its qty, default 1)."""
    return sum(_span_qty(s) for s in spans if s[0] <= abs_b < s[0] + s[1])


def max_concurrent(spans, lo: int, hi: int) -> int:
    """The peak occupancy anywhere in [lo, hi). Occupancy only rises at a span's start, so
    the max sits at `lo` or at some span-start inside the window — checking those points is
    exact and O(spans)."""
    points = [lo] + [s[0] for s in spans if lo <= s[0] < hi]
    return max((concurrent_at(spans, p) for p in points), default=0)


def stay_fits(spans, abs_start: int, length: int, capacity: int,
              qty: int = 1, buffer_blocks: int = 0) -> bool:
    """Does a new `qty`-unit, `length`-block booking from `abs_start` fit? It fits when peak
    occupancy over its span PLUS its own qty stays within capacity. `buffer_blocks` leaves a
    gap before/after neighbours (turnaround/travel). With qty=1, buffer=0 this is the
    original 'occupancy < capacity' test, byte-identical."""
    spans = expand_spans(spans, buffer_blocks)
    return max_concurrent(spans, abs_start, abs_start + length) + int(qty) <= int(capacity)


def nearest_fit(spans, abs_start: int, length: int, capacity: int,
                max_slide: int = BLOCKS_PER_DAY * 14,
                qty: int = 1, buffer_blocks: int = 0, legal=None) -> int | None:
    """The nearest start at/after `abs_start` where the booking fits, sliding forward a
    block at a time up to `max_slide` (default ~2 weeks). None if none fits.

    `legal(abs_start) -> bool` narrows "fits" to "could actually be booked".
    Capacity alone finds the first moment the arithmetic works, which is not the
    same thing: live 2026-09-22, a stays business asked for 08:00→17:00 over two nights
    and was offered **16:30 → 01:30** — a collection in the middle of the night,
    hours outside a close_time of 18:45. The caller owns the rules (check-in and
    check-out windows live in the tenant's config, not in the grid), so it
    passes them down; without a predicate the behaviour is exactly as before."""
    for delta in range(0, int(max_slide) + 1):
        cand = abs_start + delta
        if stay_fits(spans, cand, length, capacity, qty, buffer_blocks) and (
                legal is None or legal(cand)):
            return cand
    return None


def stay_offer(spans, start_dt: datetime, end_dt: datetime, capacity: int,
               max_slide: int = BLOCKS_PER_DAY * 14,
               qty: int = 1, buffer_blocks: int = 0, legal=None) -> dict:
    """The whole booking decision for a stay (your two-step): given a requested
    drop-off→collect and the existing stays, either confirm the requested span fits, or
    offer the nearest later start for the SAME length.

    `spans` is the existing stays as (abs_start, length_blocks[, qty]) — e.g.
    ``[s for s in map(stay_span, bookings) if s]``. `qty` is the new booking's unit count
    (default 1); `buffer_blocks` is the turnaround/travel gap. Returns
    ``{fits, start, end, nights, alt_start, alt_end}`` (alt_* present only when it didn't
    fit but an alternative was found within `max_slide`; both None when the diary is full
    that far out)."""
    lo = datetime_to_abs(start_dt)
    length = datetime_to_abs(end_dt) - lo
    nights = max(0, (end_dt.date().toordinal() - start_dt.date().toordinal()))
    if length <= 0:
        return {"fits": False, "start": start_dt, "end": end_dt, "nights": nights,
                "alt_start": None, "alt_end": None}
    if stay_fits(spans, lo, length, capacity, qty, buffer_blocks):
        return {"fits": True, "start": start_dt, "end": end_dt, "nights": nights,
                "alt_start": None, "alt_end": None}
    alt = nearest_fit(spans, lo, length, capacity, max_slide, qty, buffer_blocks, legal=legal)
    return {"fits": False, "start": start_dt, "end": end_dt, "nights": nights,
            "alt_start": abs_to_datetime(alt) if alt is not None else None,
            "alt_end": abs_to_datetime(alt + length) if alt is not None else None}


# ── migration helper: legacy `window` text → grid blocks ──────────────────────
def window_to_blocks(window, slot_minutes, config: dict) -> tuple[int, int] | None:
    """Map a legacy ``bookings.window`` value to ``(start_block, length_blocks)`` for
    the back-fill (docs migration step 2). Returns None for an unrecognised value so
    the row keeps NULL grid columns and renders exactly as it does today.

      'day'    → the whole bookable day
      'am'     → open … noon
      'pm'     → noon … close
      'HH:MM'  → a slot of `slot_minutes` starting at that time
    """
    o, c = open_close_blocks(config)
    w = (str(window).strip().lower() if window is not None else "")
    if w == "day":
        return (o, c - o)
    if w == "am":
        end = min(max(o, NOON_BLOCK), c)
        return (o, end - o) if end > o else (o, c - o)
    if w == "pm":
        start = min(max(o, NOON_BLOCK), c)
        return (start, c - start) if c > start else (o, c - o)
    try:
        start = block_at(w)                    # 'HH:MM'
    except (TypeError, ValueError):
        return None
    length = blocks_for_minutes(slot_minutes or minutes_for_blocks(1))
    return (start, length)
