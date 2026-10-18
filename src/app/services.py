"""Service types — the per-tenant list of bookable services, each with a duration
(db/schema.sql scheduling_config.service_types).

The inbound-appointments twin of the field-service job_types table, but config not
schema: a business defines the services it offers (a short visit or a 90-minute
session), each with how long it takes, so the office agent books the
RIGHT slot length per service requested instead of one fixed slot_minutes. A service of
N minutes maps onto the 15-minute grid as ceil(N/15) blocks (app/slotgrid), exactly the
way a job's duration fills an person-day — so a 90-min service occupies 6 blocks and
the agent only offers start times where a 6-block span still fits below capacity.

Pure helpers — no DB. The dashboard persists via db.upsert_scheduling_config and the
pipeline reads db.get_scheduling_config. A service is {name, minutes, price} (price
optional, None when unset). `service_types` is an ordered list of them; an EMPTY list is
the default — the tenant keeps the single fixed-slot diary, byte-identical to before.
"""

MAX_SERVICES = 30
# How a service is TIMED for the customer (Kris 2026-09-03, the Stone Mason run):
#   time — an appointment at an exact start time (a consultation at the workshop);
#   half — an ARRIVAL WINDOW: the customer is told morning or afternoon and the
#          business manages the exact hour (a site survey, a fitting, a delivery);
#   day  — a whole-day job (a full-day service).
# The diary still reserves the service's real duration on the grid for capacity;
# `timing` only decides what the customer is offered and told.
TIMINGS = ("time", "half", "day")
TIMING_LABELS = {"time": "Exact time", "half": "Morning or afternoon", "day": "Whole day"}
_MAX_NAME = 60
_MIN_MINUTES = 5
_MAX_MINUTES = 1440          # a full day; slotgrid clamps the grid length itself
_MAX_PRICE = 1_000_000.0


def _norm(name) -> str:
    """Normalise a name for matching/dedupe: lowercased, collapsed whitespace."""
    return " ".join(str(name or "").strip().lower().split())


def _clean_minutes(value) -> int:
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        return 60
    return max(_MIN_MINUTES, min(_MAX_MINUTES, m))


def _clean_price(value):
    """Optional price → a non-negative float, or None when blank/invalid/zero."""
    if value is None or str(value).strip() == "":
        return None
    try:
        p = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return p if 0 < p <= _MAX_PRICE else None


def clean_service_types(rows) -> list[dict]:
    """Normalise a raw service list (from the form OR the db) into clean
    {name, minutes, price} dicts — blanks and duplicate names dropped, trimmed, minutes
    clamped, capped at MAX_SERVICES. The single source of validity, used on save and read."""
    seen: set = set()
    out: list[dict] = []
    for r in rows or []:
        name = str((r or {}).get("name") or "").strip()[:_MAX_NAME]
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        svc = {"name": name, "minutes": _clean_minutes((r or {}).get("minutes")),
               "price": _clean_price((r or {}).get("price")),
               # Per-service attributes (#6): bind a service to a resource type (its own
               # capacity pool / skill) and give it its own turnaround buffer. Both
               # optional — None falls back to the global pool / global buffer.
               "resource": _clean_name((r or {}).get("resource")),
               "buffer": _clean_opt_minutes((r or {}).get("buffer"))}
        # Practice OS (docs/os/practice.md §4.2): an optional per-service recall cadence in
        # months (6 for a routine review). Only carried when set — a plain diary service
        # never gains the key, so non-practice tenants are byte-identical.
        recall = _clean_recall_months((r or {}).get("recall_months"))
        if recall is not None:
            svc["recall_months"] = recall
        # Arrival-window / whole-day services carry `timing`; an exact-time service
        # (the default) never gains the key, so existing tenants are byte-identical.
        t = _clean_timing((r or {}).get("timing"))
        if t != "time":
            svc["timing"] = t
        # A service's own booking rules (docs/plan-diary-multiview.md, Kris
        # 2026-09-23: "only take meet & greets between 1pm-4pm", on set days, up
        # to N a day). Each key rides only when set, so a service without rules
        # — every tenant before this — is byte-identical.
        hf, hu = _clean_hhmm((r or {}).get("hours_from")), _clean_hhmm((r or {}).get("hours_until"))
        if hf and hu and hf >= hu:
            hf = hu = None
        if hf:
            svc["hours_from"] = hf
        if hu:
            svc["hours_until"] = hu
        days = _clean_days((r or {}).get("days"))
        if days is not None:
            svc["days"] = days
        cap = _clean_max_per_day((r or {}).get("max_per_day"))
        if cap is not None:
            svc["max_per_day"] = cap
        starts = _clean_starts((r or {}).get("starts"))
        if starts:
            svc["starts"] = starts
        # An URGENT service keeps its own clock (docs/plan-service-urgency.md,
        # D1 stage 1): "same day if asked before 14:00" and its own notice,
        # replacing the diary-wide notice for this service only. Setting the
        # cut-off is what makes it urgent; both ride only when set.
        sdu = _clean_hhmm((r or {}).get("same_day_until"))
        if sdu:
            svc["same_day_until"] = sdu
        nh = _clean_notice_hours((r or {}).get("notice_hours"))
        if nh is not None:
            svc["notice_hours"] = nh
        # Out of hours (D1 stage 2): evenings until a time, extra days, its own
        # price, and whether the reply may offer it without the owner's yes.
        ooh = clean_out_of_hours((r or {}).get("out_of_hours"))
        if ooh:
            svc["out_of_hours"] = ooh
        # After the job (docs/plan-after-job-tasks.md): tasks the job leaves for
        # the business, each with a deadline from the job being done. Rides only
        # when set, so a service without any is byte-identical.
        from app import after_job
        aj = after_job.clean_tasks((r or {}).get("after_job"))
        if aj:
            svc["after_job"] = aj
        out.append(svc)
        if len(out) >= MAX_SERVICES:
            break
    return out


def _clean_timing(value) -> str:
    v = str(value or "").strip().lower()
    aliases = {"arrival": "half", "ampm": "half", "am/pm": "half", "window": "half",
               "morning or afternoon": "half", "whole day": "day", "exact": "time",
               "exact time": "time", "slot": "time", "": "time"}
    v = aliases.get(v, v)
    return v if v in TIMINGS else "time"


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _clean_hhmm(value):
    """'13:00' / '1300' / '13.00' → 'HH:MM', or None when blank/invalid."""
    v = str(value or "").strip().replace(".", ":")
    if not v:
        return None
    if ":" not in v and v.isdigit() and len(v) in (3, 4):
        v = v[:-2] + ":" + v[-2:]
    try:
        h, m = (int(x) for x in v.split(":")[:2])
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else None


def _clean_days(value):
    """Days a service is booked on → sorted weekday numbers (Mon=0), or None for
    "every open day" (nothing ticked, or all seven)."""
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace(" ", "").split(",")
    out = set()
    for it in items:
        t = str(it).strip().lower()
        if t.isdigit() and 0 <= int(t) <= 6:
            out.add(int(t))
        elif t[:3] in DAY_KEYS:
            out.add(DAY_KEYS.index(t[:3]))
    return sorted(out) if out and len(out) < 7 else None


def _clean_starts(value):
    """Fixed start times ('11:00, 13:00, 15:00') → sorted unique 'HH:MM' list, or
    None. A meet & greet held at 11am, 1pm or 3pm is not a window: 12:00 is not one
    of its times."""
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace(";", ",").split(",")
    out = sorted({t for t in (_clean_hhmm(i) for i in items) if t})
    return out[:24] or None


def _clean_max_per_day(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= 500 else None


def _clean_notice_hours(value):
    """A service's own notice in whole hours, 0-72, or None (the diary's)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 72 else None


def rules(svc) -> dict:
    """The service's own booking rules — {hours_from, hours_until, days, max_per_day,
    starts, same_day_until, notice_hours}, each None when unset."""
    svc = svc or {}
    return {"hours_from": svc.get("hours_from"), "hours_until": svc.get("hours_until"),
            "days": svc.get("days"), "max_per_day": svc.get("max_per_day"),
            "starts": svc.get("starts"), "same_day_until": svc.get("same_day_until"),
            "notice_hours": svc.get("notice_hours")}


OOH_OFFERS = ("ask", "auto", "existing")
OOH_OFFER_LABELS = {"ask": "ask me first", "auto": "offer it automatically",
                    "existing": "automatically for existing customers"}


def clean_out_of_hours(value) -> dict | None:
    """{until, days, bank_holidays, price, price_after, offer} or None. On only
    when it opens something — an evening (until) or extra days — AND has a
    price: an out-of-hours slot is never offered without saying what it costs
    (research 2026-09-26: the DMCC Act 2024 s.230 and the Consumer Contracts
    Regs need the price, or how it's worked out, before attending)."""
    if not isinstance(value, dict):
        return None
    until = _clean_hhmm(value.get("until"))
    days = _clean_days(value.get("days")) or []
    bh = str(value.get("bank_holidays") or "").strip().lower() in ("1", "true", "yes", "on")
    price = _clean_price(value.get("price"))
    after = _clean_price(value.get("price_after"))
    offer = str(value.get("offer") or "ask").strip().lower()
    offer = offer if offer in OOH_OFFERS else "ask"
    if price is None or not (until or days or bh):
        return None
    out = {"price": price, "offer": offer}
    basis = value.get("price_basis")
    out["price_basis"] = basis if basis in ("first_hour", "booking") else "unspecified"
    terms = str(value.get("terms") or "").strip()[:1000]
    if terms:
        out["terms"] = terms
    review = [k for k in value.get("carried_over_review", []) if k in ("price_basis", "terms")]
    if review:
        out["carried_over_review"] = review
    if until:
        out["until"] = until
    if days:
        out["days"] = days
    if bh:
        out["bank_holidays"] = True
    if after is not None:
        out["price_after"] = after
    return out


def out_of_hours(svc) -> dict | None:
    return (svc or {}).get("out_of_hours") or None


def is_urgent(svc) -> bool:
    """An urgent service: one the business will do the same day before a cut-off."""
    return bool((svc or {}).get("same_day_until"))


def days_label(days) -> str:
    names = [DAY_NAMES[d] for d in days or []]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def rules_label(svc) -> str:
    """'Tue and Thu, 13:00–16:00, up to 4 a day' — only the rules that are set; ''
    when none are, so a service without rules reads exactly as before."""
    r = rules(svc)
    bits = []
    if r["days"]:
        bits.append(days_label(r["days"]))
    if r.get("starts"):
        s = r["starts"]
        bits.append("at " + (s[0] if len(s) == 1 else ", ".join(s[:-1]) + " or " + s[-1]))
    if r["hours_from"] and r["hours_until"]:
        bits.append(f"{r['hours_from']}–{r['hours_until']}")
    elif r["hours_from"]:
        bits.append(f"from {r['hours_from']}")
    elif r["hours_until"]:
        bits.append(f"until {r['hours_until']}")
    if r["max_per_day"]:
        bits.append(f"up to {r['max_per_day']} a day")
    if r.get("same_day_until"):
        bits.append(f"same day if asked before {r['same_day_until']}")
    if r.get("notice_hours") is not None:
        bits.append(f"{r['notice_hours']} h notice")
    return ", ".join(bits)


def ooh_label(svc) -> str:
    """'evenings until 22:00, Sun and bank holidays, £140 the first hour then
    £65 an hour (ask me first)' — '' when the service has no out of hours."""
    o = out_of_hours(svc)
    if not o:
        return ""
    when = []
    if o.get("until"):
        when.append(f"evenings until {o['until']}")
    extra = [days_label(o["days"])] if o.get("days") else []
    if o.get("bank_holidays"):
        extra.append("bank holidays")
    if extra:
        when.append(" and ".join(extra))
    price = ooh_price_label(o)
    return f"{', '.join(when)}, {price} ({OOH_OFFER_LABELS[o['offer']]})"


def ooh_price_label(o) -> str:
    price = f"£{o['price']:g}"
    if o.get("price_basis") == "first_hour":
        price += " for the first hour"
        if o.get("price_after") is not None:
            price += f" then £{o['price_after']:g} an hour"
    elif o.get("price_basis") == "booking":
        price += " per booking"
    return price


def timing(svc) -> str:
    """The service's timing kind, 'time' when unset (see TIMINGS)."""
    return _clean_timing((svc or {}).get("timing"))


def timing_label(svc) -> str:
    return TIMING_LABELS[timing(svc)]


def _clean_name(value):
    s = str(value or "").strip()[:_MAX_NAME]
    return s or None


def _clean_opt_minutes(value):
    """Optional per-service buffer in minutes → non-negative int, or None when blank/zero."""
    if value is None or str(value).strip() == "":
        return None
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return m if 0 < m <= 480 else None


def _clean_recall_months(value):
    """Optional Practice OS recall cadence in months → positive int, or None when
    blank/invalid/zero. Mirrors practice_config.clean_recall_months (kept local to avoid a
    services→practice_config import); ceiling five years."""
    if value is None or str(value).strip() == "":
        return None
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return m if 0 < m <= 60 else None


def configured(service_types) -> bool:
    """True when this tenant runs the per-service-duration diary (at least one service)."""
    return bool(clean_service_types(service_types))


def find(service_types, name) -> dict | None:
    """The service matching `name` (case/space-insensitive), or None — the executor's
    lookup so the model never invents a duration, exactly like FSM's job-type resolve."""
    want = _norm(name)
    if not want:
        return None
    return next((s for s in clean_service_types(service_types) if _norm(s["name"]) == want), None)


def names(service_types) -> list[str]:
    return [s["name"] for s in clean_service_types(service_types)]


def _duration_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} hr" + (f" {m} min" if m else "")


def render_for_prompt(service_types) -> str:
    """The services block injected into the office <scheduling> prompt: each service with
    its duration (and price when set), so the agent maps the customer's words to a known
    service and books the right length. Empty string when no services are defined."""
    svcs = clean_service_types(service_types)
    if not svcs:
        return ""
    lines = ["Services — this business offers set services, each a fixed length. Work out "
             "which service the customer wants, pass it as the `service` argument to "
             "check_availability and hold_booking (so the right duration is booked), and "
             "tell the customer the price when one is shown:"]
    arrival = False
    ruled = False
    for s in svcs:
        line = f"- {s['name']} ({_duration_label(s['minutes'])}"
        if s.get("price") is not None:
            line += f", £{s['price']:g}"
        if rules_label(s):
            ruled = True
            line += f", booked {rules_label(s)}"
        if ooh_label(s):
            line += f"; out of hours: {ooh_label(s)}"
            if out_of_hours(s).get("terms"):
                line += f"; out-of-hours terms: {out_of_hours(s)['terms']}"
        line += ")"
        t = timing(s)
        if t == "half":
            arrival = True
            line += (" — ARRIVAL WINDOW: offer and confirm the MORNING or the AFTERNOON, "
                     "never a clock time; the window value is 'am' or 'pm'")
        elif t == "day":
            arrival = True
            line += " — WHOLE-DAY job: the window value is 'day'; never quote a clock time"
        lines.append(line)
    if arrival:
        lines.append("For an arrival-window or whole-day service the customer is told which "
                     "part of the day the business will arrive ('we'll be with you on Monday "
                     "morning'), not an appointment time — the exact hour is the business's "
                     "to manage. Exact-time services are still offered as start times.")
    if ruled:
        lines.append(RULES_PROMPT_LINE)
    if any(is_urgent(s) for s in svcs):
        lines.append(URGENT_PROMPT_LINE)
    lines.append("If the request doesn't match a service above, ask the customer to confirm "
                 "which service they need before booking — never guess the duration.")
    return "\n".join(lines)


RULES_PROMPT_LINE = (
    "A service's days, start times, hours and daily limit are already applied: the availability tools "
    "only return times inside them, so offer only what they return and never a time "
    "outside them.")


# An urgent service (docs/plan-service-urgency.md, D1): same day is HELD when
# the diary has it, and said as an aim with a window — never a clock time
# (research 2026-09-26: firms publish same day as an aim, and a firm time in a
# reply is a binding term under the Consumer Rights Act 2015 s.50).
URGENT_PROMPT_LINE = (
    "An URGENT service ('same day if asked before HH:MM'): when check_availability "
    "returns today, HOLD it — the customer asked for today and the business does "
    "urgent work the same day. Say it as a window and an aim, never a clock time: "
    "'we'll get someone to you this afternoon — we'll text when they set off'. Never "
    "promise an exact arrival time. If today isn't returned (past the cut-off, or "
    "the team is fully booked), say plainly that you can't get to them today and hold "
    "the soonest time the tool shows. When the business's knowledge has a safety "
    "instruction for an emergency (who to call or what to do first), give that "
    "before offering the visit.")
# ↑ Agnostic by rule (Kris 2026-09-26): this line reaches EVERY business with an
# urgent service — any business that offers an urgent service. Trade specifics (which line to
# ring) live in that business's own knowledge, never here.


def parse_form(name_list, minutes_list, price_list,
               resource_list=None, buffer_list=None, recall_months_list=None,
               timing_list=None, from_list=None, until_list=None, days_list=None,
               max_list=None, starts_list=None, same_day_list=None,
               notice_list=None, ooh_until_list=None, ooh_days_list=None,
               ooh_bh_list=None, ooh_price_list=None, ooh_after_list=None,
               ooh_offer_list=None, after_job_list=None,
               ooh_basis_list=None, ooh_terms_list=None) -> list[dict]:
    """Reassemble the service list from the settings form's positionally-aligned arrays:
    name[], minutes[], price[], and optional resource[]/buffer[]/recall_months[]. Cleaned
    (blanks/dupes dropped) by clean_service_types. recall_months[] is the Practice OS per-
    service cadence — absent for non-practice tenants, so the shape is unchanged for them."""
    name_list = name_list or []
    minutes_list = minutes_list or []
    price_list = price_list or []
    resource_list = resource_list or []
    buffer_list = buffer_list or []
    recall_months_list = recall_months_list or []
    timing_list = timing_list or []
    from_list, until_list = from_list or [], until_list or []
    days_list, max_list, starts_list = days_list or [], max_list or [], starts_list or []
    same_day_list, notice_list = same_day_list or [], notice_list or []
    ooh = [ooh_until_list or [], ooh_days_list or [], ooh_bh_list or [],
           ooh_price_list or [], ooh_after_list or [], ooh_offer_list or []]

    def _at(lst, i):
        return lst[i] if i < len(lst) else None
    rows = []
    for i, name in enumerate(name_list):
        rows.append({
            "name": name,
            "minutes": minutes_list[i] if i < len(minutes_list) else 60,
            "price": price_list[i] if i < len(price_list) else None,
            "resource": resource_list[i] if i < len(resource_list) else None,
            "buffer": buffer_list[i] if i < len(buffer_list) else None,
            "recall_months": recall_months_list[i] if i < len(recall_months_list) else None,
            "timing": timing_list[i] if i < len(timing_list) else None,
            "hours_from": from_list[i] if i < len(from_list) else None,
            "hours_until": until_list[i] if i < len(until_list) else None,
            "days": days_list[i] if i < len(days_list) else None,
            "max_per_day": max_list[i] if i < len(max_list) else None,
            "starts": starts_list[i] if i < len(starts_list) else None,
            "same_day_until": same_day_list[i] if i < len(same_day_list) else None,
            "notice_hours": notice_list[i] if i < len(notice_list) else None,
            "out_of_hours": {"until": _at(ooh[0], i), "days": _at(ooh[1], i),
                             "bank_holidays": _at(ooh[2], i), "price": _at(ooh[3], i),
                             "price_after": _at(ooh[4], i), "offer": _at(ooh[5], i),
                             "price_basis": _at(ooh_basis_list or [], i),
                             "terms": _at(ooh_terms_list or [], i)},
            "after_job": _at(after_job_list or [], i),
        })
    return clean_service_types(rows)


# ── Appointments on a STAYS diary (Kris 2026-09-13) ─────────────────────────
def bind_appointment_pools(service_types, resource_types, at_once=None) -> tuple[list[dict], list[dict]]:
    """On a stays diary every service is an APPOINTMENT — a timed visit the
    customer comes to (a meet & greet, an assessment) — with its own
    concurrency, kept apart from the stay spaces by binding each service to a
    resource pool of the SAME name whose count is "at once". `at_once` is the
    form's positional list (blank = keep the pool's count, default 1). An
    appointment is always an exact time. Returns (services, resources)."""
    from app import diary_config
    svcs = clean_service_types(service_types)
    pools = diary_config.clean_resource_types(resource_types)
    wanted = list(at_once or [])
    by_key = {_norm(r["name"]): r for r in pools}
    for i, svc in enumerate(svcs):
        want = None
        if i < len(wanted) and str(wanted[i] or "").strip():
            try:
                want = max(1, int(float(wanted[i])))
            except ValueError:
                want = None
        pool = by_key.get(_norm(svc["name"]))
        if pool is None:
            pool = {"name": svc["name"], "count": want or 1, "rate": None,
                    "rate_unit": "booking", "skills": []}
            pools.append(pool)
            by_key[_norm(svc["name"])] = pool
        elif want:
            pool["count"] = want
        svc["resource"] = svc["name"]
        svc.pop("timing", None)
    return svcs, diary_config.clean_resource_types(pools)


def appointment_at_once(service_types, resource_types) -> dict[str, int]:
    """{service name: how many at once} for the Appointments face."""
    from app import diary_config
    out = {}
    for svc in clean_service_types(service_types):
        pool = diary_config.find_resource(resource_types, svc.get("resource") or svc["name"])
        out[svc["name"]] = int(pool["count"]) if pool else 1
    return out


def is_appointment_row(booking, service_types) -> bool:
    """A booking on a stays diary that is one of its appointment services —
    by its pool (resource_type), which carries the service's own name."""
    get = booking.get if isinstance(booking, dict) else lambda k, d=None: getattr(booking, k, d)
    rt = str(get("resource_type") or "").strip()
    if not rt:
        return False
    key = _norm(rt)
    return any(_norm(svc["name"]) == key for svc in clean_service_types(service_types))
