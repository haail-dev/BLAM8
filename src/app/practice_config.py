"""Practice config — practitioners + per-service recall cadence (docs/os/practice.md §4.2).

The Practice OS twin of src/app/services.py. Practice OS reuses scheduling_config verbatim
and adds richer jsonb, NO schema change:

  scheduling_config.service_types[i].recall_months : int | null  -- standard recall cadence
        for this service (6 for a dental check-up, 3 for hygiene). Stamped onto the booking
        at completion, so every patient is chased on THEIR clock (db.complete_appointment_
        and_schedule_recall), not a batch date.
  scheduling_config.practitioners : [ {name, resource, capacity_per_day} ]  -- the resource
        configuration list. This does not expose named-person availability in the
        diary prompt. Individual working hours and leave are not modelled here.

Pure helpers — no DB. The dashboard persists via db.upsert_scheduling_config and the
recall stamp reads db.get_scheduling_config. Empty lists are the default (a plain diary).
"""

MAX_PRACTITIONERS = 40
_MAX_NAME = 60
_MAX_RECALL_MONTHS = 60          # five years — a generous ceiling; None = no recall

# No-show band thresholds (docs/os/practice.md §12.1): low < LOW, medium [LOW,HIGH), high >= HIGH.
# Config-overridable per tenant (scheduling_config.no_show_thresholds) so a practice with a
# packed book can raise the bar. Only the `high` band triggers the extra reminder.
DEFAULT_NO_SHOW_THRESHOLDS = {"low": 40, "high": 70}

# The reminder cycle (docs/os/practice.md §6.2): lead times (hours before the appointment)
# at which a reminder goes out, and whether the later touches ask the patient to confirm.
# Per-tenant overridable via scheduling_config.reminder_cycle. The default is the 3-touch
# 48/24/2h cadence the design promises.
DEFAULT_REMINDER_CYCLE = {"leads_hours": [48, 24, 2], "ask_confirm": True}
_MAX_LEADS = 6                    # a sane ceiling on how many reminder touches a tenant sets
_MAX_LEAD_HOURS = 24 * 30        # ~a month ahead — anything longer is recall's job, not a reminder


def _norm(name) -> str:
    """Normalise a name for matching/dedupe: lowercased, collapsed whitespace."""
    return " ".join(str(name or "").strip().lower().split())


def _clean_name(value):
    s = str(value or "").strip()[:_MAX_NAME]
    return s or None


def clean_recall_months(value):
    """Optional per-service recall cadence → a positive int in months, or None when
    blank/invalid/zero (no recall for that service)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return m if 0 < m <= _MAX_RECALL_MONTHS else None


def _clean_capacity(value):
    """Optional per-practitioner daily capacity → a positive int, or None (falls back to
    the global pool default)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        c = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return c if 0 < c <= 200 else None


def clean_practitioners(rows) -> list[dict]:
    """Normalise a raw practitioner list (from the form OR the db) into clean
    {name, resource, capacity_per_day} dicts — blanks and duplicate names dropped,
    trimmed, capped at MAX_PRACTITIONERS. The single source of validity, on save and read.
    `resource` defaults to the name (a practitioner IS their own capacity pool)."""
    seen: set = set()
    out: list[dict] = []
    for r in rows or []:
        name = _clean_name((r or {}).get("name"))
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "resource": _clean_name((r or {}).get("resource")) or name,
            "capacity_per_day": _clean_capacity((r or {}).get("capacity_per_day")),
        })
        if len(out) >= MAX_PRACTITIONERS:
            break
    return out


def practitioners(scheduling_config) -> list[dict]:
    """The tenant's clean practitioner pools from a scheduling_config row (or None)."""
    cfg = scheduling_config or {}
    return clean_practitioners(cfg.get("practitioners"))


def recall_months_for_service(service_types, name) -> int | None:
    """The recall cadence (months) for the service matching `name`, or None — the recall
    stamp's lookup, so completion never invents a cadence. Mirrors services.find()."""
    want = _norm(name)
    if not want:
        return None
    for s in service_types or []:
        if _norm((s or {}).get("name")) == want:
            return clean_recall_months((s or {}).get("recall_months"))
    return None


def no_show_thresholds(scheduling_config) -> dict:
    """The tenant's no-show band cut-offs {low, high}, defaulting to DEFAULT_NO_SHOW_
    THRESHOLDS (§12.1). A partial/invalid override falls back per-key; `low` is clamped
    below `high` so the three bands never collapse or invert."""
    cfg = scheduling_config or {}
    raw = cfg.get("no_show_thresholds") or {}
    def _pick(key):
        try:
            v = int(round(float(raw.get(key))))
        except (TypeError, ValueError):
            return DEFAULT_NO_SHOW_THRESHOLDS[key]
        return v if 0 <= v <= 100 else DEFAULT_NO_SHOW_THRESHOLDS[key]
    low, high = _pick("low"), _pick("high")
    if low >= high:                       # keep a real medium band between them
        low, high = DEFAULT_NO_SHOW_THRESHOLDS["low"], DEFAULT_NO_SHOW_THRESHOLDS["high"]
    return {"low": low, "high": high}


def band_for_score(score: int, scheduling_config=None) -> str:
    """Map a 0–100 score to low | medium | high using the tenant's thresholds. The single
    source of banding, so the heuristic prior, the LLM refine and the board never disagree."""
    t = no_show_thresholds(scheduling_config)
    s = max(0, min(100, int(score)))
    if s >= t["high"]:
        return "high"
    if s >= t["low"]:
        return "medium"
    return "low"


def clean_lead_hours(values) -> list[int]:
    """Normalise a raw lead-hours list into positive ints, deduped, sorted DESCENDING (the
    48→24→2 order the cycle fires in), capped at _MAX_LEADS. Blanks/invalid/out-of-range
    dropped. Empty in → the default cadence, so a tenant never ends up with no reminders by
    accident (they disable the whole cycle by unsetting the module, not by clearing leads)."""
    seen: set = set()
    out: list[int] = []
    for v in values or []:
        if v is None or str(v).strip() == "":
            continue
        try:
            h = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if not (0 < h <= _MAX_LEAD_HOURS) or h in seen:
            continue
        seen.add(h)
        out.append(h)
    out.sort(reverse=True)
    return out[:_MAX_LEADS]


def reminder_cycle(scheduling_config) -> dict:
    """The tenant's reminder cadence {leads_hours:[...], ask_confirm:bool}, defaulting to
    DEFAULT_REMINDER_CYCLE (§6.2). A partial/invalid override falls back per-key; an empty
    or all-invalid leads list falls back to the default 48/24/2h so the cycle never silently
    stops. The single source of the cadence, read by app/jobs/send_appointment_reminders."""
    cfg = scheduling_config or {}
    raw = cfg.get("reminder_cycle") or {}
    leads = clean_lead_hours(raw.get("leads_hours"))
    if not leads:
        leads = list(DEFAULT_REMINDER_CYCLE["leads_hours"])
    ask = raw.get("ask_confirm")
    ask = DEFAULT_REMINDER_CYCLE["ask_confirm"] if ask is None else bool(ask)
    return {"leads_hours": leads, "ask_confirm": ask}


def parse_practitioner_form(name_list, resource_list=None, capacity_list=None) -> list[dict]:
    """Reassemble the practitioner list from the settings form's positionally-aligned
    arrays: name[], and optional resource[]/capacity[]. Cleaned by clean_practitioners."""
    name_list = name_list or []
    resource_list = resource_list or []
    capacity_list = capacity_list or []
    rows = []
    for i, name in enumerate(name_list):
        rows.append({
            "name": name,
            "resource": resource_list[i] if i < len(resource_list) else None,
            "capacity_per_day": capacity_list[i] if i < len(capacity_list) else None,
        })
    return clean_practitioners(rows)
