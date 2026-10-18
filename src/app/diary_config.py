"""Inbound-business diary depth — the pure config layer for the six capabilities added on
top of the unified slot grid (docs/architecture/unified-slot-grid.md). No DB and no clock: cleaners that
normalise each scheduling_config jsonb blob, tiny compute helpers (a stay quote), and the
prompt renderers the agent's <scheduling> section injects.

Every block defaults EMPTY/off, and every cleaner of an empty value returns the empty shape,
so a tenant that configures none of this is byte-identical to the plain diary. Wiring into the
booking engine lives in src/app/scheduling.py and src/app/slotgrid.py; the validity rules live here so
they are testable without a database (the same split as src/app/intake.py and src/app/services.py).

  resource_types : [{name, count, rate, rate_unit, skills}] — typed capacity pools
  stay_pricing   : {rate, rate_unit, deposit, min_nights}
  day_hours      : {mon:{open,close}, …, checkin_after, checkout_before}
  party_size     : {enabled, max_people}
  recurring      : {enabled, max_occurrences, frequencies}
  delivery       : {offered, fee, radius_miles}
"""
from __future__ import annotations

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RATE_UNITS = ("hour", "day", "night", "week", "booking")
# Kris 2026-09-04: a farrier's cycle is every six to eight weeks and a lot of
# maintenance is quarterly — neither fitted weekly/fortnightly/monthly.
_FREQS = ("weekly", "fortnightly", "monthly", "six_weekly", "quarterly")
FREQ_LABELS = {"weekly": "Weekly", "fortnightly": "Fortnightly", "monthly": "Monthly",
               "six_weekly": "Every 6 weeks", "quarterly": "Quarterly"}
# Days between repeats for the week-based ones; the month-based ones step by
# calendar months instead (same day of month, clamped).
FREQ_DAYS = {"weekly": 7, "fortnightly": 14, "six_weekly": 42}
FREQ_MONTHS = {"monthly": 1, "quarterly": 3}
_MAX_NAME = 60
_MAX_LIST = 30
_MAX_MONEY = 1_000_000.0


# ── shared scalar coercion ────────────────────────────────────────────────────
def _norm(s) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _money(value):
    """Optional money → non-negative float (2dp), or None when blank/invalid/zero."""
    if value is None or str(value).strip() == "":
        return None
    try:
        v = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= _MAX_MONEY else None


def _int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _hhmm(value):
    """A 'HH:MM' time string, or None. Accepts 'HH:MM' / 'HH:MM:SS'."""
    s = str(value or "").strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


def _rate_unit(value, default="booking"):
    v = _norm(value)
    return v if v in _RATE_UNITS else default


# ── resource_types — typed capacity catalogue (the big one) ───────────────────
def clean_resource_types(rows) -> list[dict]:
    """Normalise the resource-type catalogue: each {name, count, rate, rate_unit, skills}.
    Dedupe by name, count ≥ 1, rate optional, skills a clean string list. EMPTY = one
    fungible pool of capacity_per_window (today's behaviour)."""
    seen: set = set()
    out: list[dict] = []
    for r in rows or []:
        name = str((r or {}).get("name") or "").strip()[:_MAX_NAME]
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "count": _int((r or {}).get("count"), 1, 9999, 1),
            "rate": _money((r or {}).get("rate")),
            "rate_unit": _rate_unit((r or {}).get("rate_unit")),
            "skills": _clean_str_list((r or {}).get("skills")),
        })
        if len(out) >= _MAX_LIST:
            break
    return out


def _clean_str_list(value) -> list[str]:
    if isinstance(value, str):
        value = [t for t in value.replace(";", ",").split(",")]
    seen, out = set(), []
    for s in value or []:
        t = str(s).strip()
        k = _norm(t)
        if t and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:_MAX_LIST]


def resources_configured(resource_types) -> bool:
    return bool(clean_resource_types(resource_types))


def find_resource(resource_types, name) -> dict | None:
    want = _norm(name)
    if not want:
        return None
    return next((r for r in clean_resource_types(resource_types) if _norm(r["name"]) == want), None)


def resource_names(resource_types) -> list[str]:
    return [r["name"] for r in clean_resource_types(resource_types)]


def resource_total_count(resource_types) -> int:
    return sum(r["count"] for r in clean_resource_types(resource_types))


def _rate_label(rate, rate_unit) -> str:
    per = {"hour": "/hr", "day": "/day", "night": "/night", "week": "/wk", "booking": ""}
    return f"£{rate:g}{per.get(rate_unit, '')}"


def render_resources_for_prompt(resource_types) -> str:
    rts = clean_resource_types(resource_types)
    if not rts:
        return ""
    lines = ["Resource types — this business has DIFFERENT kinds of bookable unit, each with "
             "its own availability. Work out which type the customer needs and pass it as the "
             "`resource` argument; capacity is counted per type, so one type can be full while "
             "another is free:"]
    for r in rts:
        bits = [f"{r['count']} available"]
        if r.get("rate") is not None:
            bits.append(_rate_label(r["rate"], r["rate_unit"]))
        if r.get("skills"):
            bits.append("can do: " + ", ".join(r["skills"]))
        lines.append(f"- {r['name']} ({'; '.join(bits)})")
    lines.append("If it's unclear which type they need, ask before booking — never assume.")
    return "\n".join(lines)


# ── stay_pricing — stays finally get a price ──────────────────────────────────
def clean_stay_pricing(d) -> dict:
    d = d or {}
    return {
        "rate": _money(d.get("rate")),
        "rate_unit": _rate_unit(d.get("rate_unit"), "night"),
        "deposit": _money(d.get("deposit")),
        "min_nights": _int(d.get("min_nights"), 0, 365, 0),
    }


def stay_pricing_configured(d) -> bool:
    c = clean_stay_pricing(d)
    return c["rate"] is not None or c["deposit"] is not None or c["min_nights"] > 0


def quote_stay(pricing, nights: int, days: int | None = None, qty: int = 1) -> dict | None:
    """Price a stay from the cleaned pricing + its length. `nights` = calendar nights,
    `days` = calendar days (nights + 1 for a same-units count); the rate_unit picks which.
    Returns {amount, deposit, rate, rate_unit, units} or None when no rate is set."""
    c = clean_stay_pricing(pricing)
    if c["rate"] is None:
        return None
    unit = c["rate_unit"]
    if unit == "week":
        units = max(1, -(-int(nights) // 7))           # whole weeks, rounded up
    elif unit == "day":
        units = max(1, int(days if days is not None else nights + 1))
    elif unit in ("hour", "booking"):
        units = 1                                       # not a multi-day unit — flat
    else:                                               # night
        units = max(1, int(nights))
    amount = round(c["rate"] * units * max(1, int(qty)), 2)
    return {"amount": amount, "deposit": c["deposit"], "rate": c["rate"],
            "rate_unit": unit, "units": units}


def render_stay_pricing_for_prompt(pricing) -> str:
    c = clean_stay_pricing(pricing)
    if not stay_pricing_configured(c):
        return ""
    bits = []
    if c["rate"] is not None:
        bits.append(f"the rate is {_rate_label(c['rate'], c['rate_unit'])}")
    if c["min_nights"] > 0:
        bits.append(f"the minimum stay is {c['min_nights']} night(s)")
    if c["deposit"] is not None:
        bits.append(f"a £{c['deposit']:g} deposit is taken to hold")
    return "Stay pricing — " + "; ".join(bits) + ". State the price and any deposit before booking."


# ── day_hours — per-weekday hours + check-in/out time rules ───────────────────
def clean_day_hours(d) -> dict:
    d = d or {}
    out: dict = {}
    for dow in _WEEKDAYS:
        ov = d.get(dow)
        if isinstance(ov, dict):
            o, c = _hhmm(ov.get("open")), _hhmm(ov.get("close"))
            if o and c and o < c:
                out[dow] = {"open": o, "close": c}
    ci, co = _hhmm(d.get("checkin_after")), _hhmm(d.get("checkout_before"))
    if ci:
        out["checkin_after"] = ci
    if co:
        out["checkout_before"] = co
    return out


def day_hours_configured(d) -> bool:
    return bool(clean_day_hours(d))


def render_day_hours_for_prompt(d) -> str:
    c = clean_day_hours(d)
    lines = []
    perday = [(dow, c[dow]) for dow in _WEEKDAYS if dow in c]
    if perday:
        lines.append("Opening hours vary by day: "
                     + "; ".join(f"{dow.title()} {h['open']}-{h['close']}" for dow, h in perday)
                     + " (other days use the standard hours).")
    if c.get("checkin_after") or c.get("checkout_before"):
        rule = []
        if c.get("checkin_after"):
            rule.append(f"arrivals from {c['checkin_after']}")
        if c.get("checkout_before"):
            rule.append(f"departures by {c['checkout_before']}")
        lines.append("Check-in/out: " + ", ".join(rule) + " — keep bookings within these times.")
    return "\n".join(lines)


# ── party_size — a booking is FOR N people (covers/guests) ────────────────────
def clean_party_size(d) -> dict:
    d = d or {}
    enabled = bool(d.get("enabled"))
    return {"enabled": enabled, "max_people": _int(d.get("max_people"), 1, 100000, 0) or None}


def party_size_enabled(d) -> bool:
    return clean_party_size(d)["enabled"]


def render_party_size_for_prompt(d) -> str:
    c = clean_party_size(d)
    if not c["enabled"]:
        return ""
    extra = f" (up to {c['max_people']} at once)" if c["max_people"] else ""
    return ("Party size — every booking is for a NUMBER OF PEOPLE" + extra + ". Always capture "
            "how many people and pass it as `party_size`; capacity is counted in people, so a "
            "large party may not fit even when smaller ones would.")


# ── recurring — standing weekly/term bookings ─────────────────────────────────
def freq_label(freq: str) -> str:
    return FREQ_LABELS.get(str(freq or "").strip().lower(), str(freq or "").replace("_", " ").title())


def clean_recurring(d) -> dict:
    d = d or {}
    given = {_norm(x).replace(" ", "_") for x in (d.get("frequencies") or [])}
    given |= {"six_weekly" for x in given if x in ("6_weekly", "every_6_weeks", "6-weekly", "six-weekly")}
    freqs = [f for f in _FREQS if f in given]
    return {"enabled": bool(d.get("enabled")),
            "max_occurrences": _int(d.get("max_occurrences"), 1, 52, 12),
            "frequencies": freqs or list(_FREQS)}


def recurring_enabled(d) -> bool:
    return bool(clean_recurring(d)["enabled"])


def render_recurring_for_prompt(d) -> str:
    c = clean_recurring(d)
    if not c["enabled"]:
        return ""
    return ("Recurring bookings — this business takes STANDING bookings that repeat ("
            + ", ".join(f"{f} = {freq_label(f).lower()}" for f in c["frequencies"])
            + f", up to {c['max_occurrences']} times). If the customer wants a regular slot "
            "(\"every six weeks\", \"once a quarter\"), set `repeat` to the nearest offered "
            "value and `occurrences`; each date is capacity-checked and any that can't be "
            "held are reported back.")


# ── delivery — a stay/hire that is delivered + collected ──────────────────────
def clean_delivery(d) -> dict:
    d = d or {}
    return {"offered": bool(d.get("offered")),
            "fee": _money(d.get("fee")),
            "radius_miles": _int(d.get("radius_miles"), 0, 1000, 0) or None}


def delivery_offered(d) -> bool:
    return bool(clean_delivery(d)["offered"])


def render_delivery_for_prompt(d) -> str:
    c = clean_delivery(d)
    if not c["offered"]:
        return ""
    bits = ["this business can DELIVER and COLLECT the booking"]
    if c["fee"] is not None:
        bits.append(f"the delivery fee is £{c['fee']:g}")
    if c["radius_miles"]:
        bits.append(f"within {c['radius_miles']} miles")
    return ("Delivery — " + "; ".join(bits) + ". If the customer wants delivery, capture the "
            "address and set `delivery_required`; otherwise it's collection/on-site.")


# ── the business's own words (Kris 2026-09-05: "config, not hardcoded") ──────
NOUN_DEFAULTS = {"booking": "booking", "participant": "customer"}


def clean_nouns(d) -> dict:
    """{"booking": "lesson", "participant": "pupil"} — one word each, lower
    case, the defaults when blank. Every customer-facing rule the
    class-and-lesson programme added reads these instead of saying "class"."""
    d = d or {}
    out = {}
    for k, default in NOUN_DEFAULTS.items():
        v = " ".join(str(d.get(k) or "").split()).lower()[:24]
        out[k] = v or default
    return out


# ── front_desk — may an appointment overlap a drop-off or a collection? ──────
# Kris 2026-09-13: "a Front desk rule option, so a user can decide if they
# want overlapping — for instance they have an employee/partner that can deal
# with one thing while you deal with the other." Off by default (overlap
# allowed). On: a drop-off or collection takes `minutes` of the desk, during
# which no appointment is offered; and a stay's arrival/departure is never
# booked inside an appointment. Stays diaries with appointment services only.
def clean_front_desk(d) -> dict:
    """`hands` = how many people are on the desk at once (People, slice 2).
    One by default, which is the behaviour the rule shipped with: a drop-off
    occupies the only pair of hands, so nothing else can be booked into it.
    Two people, and a drop-off and a meet & greet run side by side — which is
    the case Kris raised when the rule was asked for."""
    d = d or {}
    return {"enabled": bool(d.get("enabled")),
            "minutes": _int(d.get("minutes"), 5, 120, 15),
            "hands": _int(d.get("hands"), 1, 50, 1)}


def front_desk_of(cfg: dict | None) -> dict:
    return clean_front_desk((cfg or {}).get("front_desk"))


def nouns_of(cfg: dict | None) -> dict:
    return clean_nouns((cfg or {}).get("nouns"))


def plural(word: str) -> str:
    w = word or ""
    if w.endswith("s") or w.endswith("x") or w.endswith("sh") or w.endswith("ch"):
        return w + "es"
    if w.endswith("y") and w[-2:-1] not in "aeiou":
        return w[:-1] + "ies"
    return w + "s"


# ── one prompt block for the whole lot (injected after the diary line) ────────
def render_all_for_prompt(cfg: dict | None) -> str:
    cfg = cfg or {}
    nouns = nouns_of(cfg)
    # A stays diary's appointment pools are the appointments' own (the diary
    # line names them with their "at a time"); listed here they read as a KIND
    # OF SPACE and the agent passes "Meet & greet" as a stay's resource.
    rts = cfg.get("resource_types")
    from app import intake as _intake
    if _intake.stay_mode(cfg):
        from app import scheduling as _scheduling
        rts = _scheduling.stay_resource_types(cfg)
    parts = [
        render_resources_for_prompt(rts),
        render_stay_pricing_for_prompt(cfg.get("stay_pricing")),
        render_day_hours_for_prompt(cfg.get("day_hours")),
        render_party_size_for_prompt(cfg.get("party_size")),
        render_recurring_for_prompt(cfg.get("recurring")),
        render_delivery_for_prompt(cfg.get("delivery")),
        # Courses + the waiting list (src/app/courses.py): only what is switched on.
        __import__("app.courses", fromlist=["render_for_prompt"]).render_for_prompt(cfg.get("courses"), nouns),
        # Make-ups (src/app/makeups.py): the rule, only when on.
        __import__("app.makeups", fromlist=["render_for_prompt"]).render_for_prompt(cfg.get("makeups"), nouns),
    ]
    return "\n".join(p for p in parts if p)
