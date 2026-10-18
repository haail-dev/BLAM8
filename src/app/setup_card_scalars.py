"""Explicit diary choices. Missing decisions never become stored defaults."""
from copy import deepcopy
from datetime import date, time
from decimal import Decimal, InvalidOperation
import re

from app import booking_policy, setup_rows

SPECS = {}
STAGE_KEYS = {"waitlist_stage", "booked_stage"}


def needs_stages(card):
    return any(line.get("status") in {"proposed", "held"} and
               line.get("name") == "setting" and line.get("fields", {}).get("key") in STAGE_KEYS
               for line in (card or {}).get("lines", []))


def _add(key, kind, path=None, *, choices=None, minimum=None, maximum=None, nullable=False):
    SPECS[key] = {"type": kind, "path": path or [key], "label": key.replace("_", " ").capitalize(),
                  "nullable": nullable, "choices": choices, "minimum": minimum, "maximum": maximum}


for key, lo, hi in (("capacity_per_window", 0, 9999), ("min_notice_hours", 0, 336),
                    ("horizon_days", 1, 365), ("buffer_minutes", 0, 480)):
    _add(key, "number", minimum=lo, maximum=hi, nullable=key == "capacity_per_window")
_add("slot_minutes", "choice", choices=[15, 30, 45, 60, 90, 120, 180, 240, 360, 480])
_add("windows", "choice", choices=["slots", "ampm", "day"])
_add("working_days", "days", nullable=True)
for key in ("open_time", "close_time"):
    _add(key, "time", nullable=True)
_add("timezone", "timezone", nullable=True)
_add("blackout_dates", "dates")
_add("bank_holiday_nation", "choice", choices=["england-and-wales", "scotland", "northern-ireland"], nullable=True)
_add("job_done_when", "choice", choices=["marked", "day_end"])
_add("capacity_state", "choice", choices=["set", "unset", "not_applicable", "off"])
for key, sub in (("stay_rate", "rate"), ("stay_deposit", "deposit")):
    _add(key, "money", ["stay_pricing", sub], minimum=0.01, maximum=1000000, nullable=True)
_add("stay_rate_unit", "choice", ["stay_pricing", "rate_unit"], choices=["night", "day", "week"])
_add("stay_min_nights", "number", ["stay_pricing", "min_nights"], minimum=0, maximum=365)
for key in ("checkin_after", "checkout_before"):
    _add(key, "time", ["day_hours", key], nullable=True)
for day in setup_rows.DAYS:
    for part in ("open", "close"):
        _add(f"dayhours_{day}_{part}", "time", ["day_hours", day, part], nullable=True)
for key, path in {
    "party_enabled": ["party_size", "enabled"], "recurring_enabled": ["recurring", "enabled"],
    "delivery_offered": ["delivery", "offered"], "no_show_spends": ["credits", "no_show_spends"],
    "excused_refunds": ["credits", "excused_refunds"], "late_move_charged": ["credits", "late_move_charged"],
    "courses_on": ["courses", "courses"], "waitlist_on": ["courses", "waitlist"],
    "makeups_on": ["makeups", "on"], "makeup_no_show_ok": ["makeups", "no_show_ok"],
    "makeup_free": ["makeups", "free"],
}.items():
    _add(key, "boolean", path)
for key, group, sub, lo, hi in (
    ("party_max_people", "party_size", "max_people", 1, 100000),
    ("recurring_max", "recurring", "max_occurrences", 1, 52),
    ("delivery_radius", "delivery", "radius_miles", 1, 1000),
    ("late_cancel_hours", "credits", "late_hours", 0, 336),
    ("offer_hours", "courses", "offer_hours", 1, 336),
    ("reenrol_days", "courses", "reenrol_days", 0, 120),
    ("makeup_per_term", "makeups", "per_term", 0, 12),
    ("makeup_notice_hours", "makeups", "notice_hours", 0, 336),
    ("makeup_within_days", "makeups", "within_days", 0, 365),
):
    _add(key, "number", [group, sub], minimum=lo, maximum=hi, nullable=key in ("party_max_people", "delivery_radius"))
_add("delivery_fee", "money", ["delivery", "fee"], minimum=0.01, maximum=1000000, nullable=True)
_add("recurring_freq", "list", ["recurring", "frequencies"], choices=["weekly", "fortnightly", "monthly", "six_weekly", "quarterly"])
for key, group, sub, choices in (
    ("credits_spend", "credits", "spend", ["auto", "register"]),
    ("hours_unit", "credits", "hours", ["minutes", "hour"]),
    ("waitlist_notify", "courses", "waitlist_notify", ["approve", "auto"]),
    ("reenrol_notify", "courses", "reenrol_notify", ["approve", "auto"]),
):
    _add(key, "choice", [group, sub], choices=choices)
for key, sub in (("noun_booking", "booking"), ("noun_participant", "participant")):
    _add(key, "text", ["nouns", sub], maximum=24)
for key in sorted(STAGE_KEYS):
    _add(key, "stage", ["courses", key], maximum=60, nullable=True)


def parse(fields, specs=None):
    specs = SPECS if specs is None else specs
    if not isinstance(fields, dict) or set(fields) != {"key", "value"} or not isinstance(fields.get("key"), str) or fields["key"] not in specs:
        raise ValueError("Choose a supported diary setting and supply its value explicitly.")
    key, value = fields["key"], fields["value"]
    spec = specs[key]
    kind = spec["type"]
    if value is None:
        if not spec["nullable"]:
            raise ValueError("This setting needs an explicit choice.")
        return {"key": key, "value": None}
    if kind in ("number", "money"):
        if isinstance(value, bool):
            raise ValueError("Enter a number, not a yes/no choice.")
        try:
            n = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a valid number.") from None
        if not n.is_finite() or not Decimal(str(spec["minimum"])) <= n <= spec["maximum"] or (kind == "number" and n != n.to_integral_value()) or (kind == "money" and n != n.quantize(Decimal("0.01"))):
            raise ValueError("Enter a value within this setting's range and precision.")
        value = int(n) if kind == "number" else float(n)
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Choose Yes or No explicitly.")
    elif kind == "choice":
        if type(value) is bool or value not in spec["choices"]:
            raise ValueError("Choose one of: " + ", ".join(map(str, spec["choices"])) + ".")
    elif kind == "days":
        value = setup_rows.day_codes(value)
    elif kind in ("dates", "list"):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value) or len(value) > 366:
            raise ValueError("Supply a list of explicit values.")
        value = list(dict.fromkeys(v.strip() for v in value))
        if kind == "dates":
            if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in value):
                raise ValueError("Use dates in YYYY-MM-DD format.")
            value = [date.fromisoformat(v).isoformat() for v in value]
        elif not value or any(v not in spec["choices"] for v in value):
            raise ValueError("Choose the offered recurrence frequencies explicitly.")
    elif kind == "time":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::00)?", value):
            raise ValueError("Use a time in HH:MM format.")
        value = time.fromisoformat(value).strftime("%H:%M")
    elif kind == "prose":
        if not isinstance(value, str) or len(value) > spec["maximum"]:
            raise ValueError("Supply text within the displayed limit.")
        value = value.strip()
    elif kind == "stage":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > spec["maximum"]:
            raise ValueError("Choose a saved CRM stage, or explicitly clear the link.")
        value = value.strip()
    elif kind == "timezone":
        booking_policy.validate("timezone", (value,))
    elif not isinstance(value, str) or not value.strip() or len(value.strip()) > spec["maximum"]:
        raise ValueError("Supply a non-empty label within the displayed limit.")
    else:
        value = " ".join(value.split()).lower()
    return {"key": key, "value": value}


def read(cfg, key):
    if key == "capacity_state":
        return (cfg.get("booking_setting_review") or {}).get("capacity", {}).get("state", "set" if cfg.get("capacity_per_window") is not None else "unset")
    value = cfg
    for part in SPECS[key]["path"]:
        value = value.get(part) if isinstance(value, dict) else None
    if key == "working_days" and isinstance(value, str):
        return value.split(",") if value else []
    if key == "working_days" and (cfg.get("booking_setting_review") or {}).get("working_days", {}).get("state") == "off":
        return []
    return value


def describe(fields):
    value = fields["value"]
    text = "Not set" if value is None else "Yes" if value is True else "No" if value is False else ", ".join(value) if isinstance(value, list) else str(value)
    return SPECS[fields["key"]]["label"] + ": " + (text or "None")


def patch(lines, cfg, actor=None):
    from app.setup_cards import Invalid
    cfg = cfg or {}
    result = deepcopy(cfg)
    parsed = []
    for line in lines:
        try:
            fields = parse(line["fields"])
        except (ValueError, TypeError) as exc:
            raise Invalid(str(exc)) from None
        parsed.append((line, fields))
    keys = [f["key"] for _, f in parsed]
    if len(set(keys)) != len(keys):
        raise Invalid("Choose only one proposal for each setting.")
    core = {key: setting for setting, fields in booking_policy.FIELDS.items() for key in fields}
    supplied, states, changed = {}, {}, set()
    for _, fields in parsed:
        key, value = fields["key"], fields["value"]
        if key in STAGE_KEYS:
            stages = cfg.get("_setup_stages")
            if not isinstance(stages, list):
                raise Invalid("Saved CRM stages could not be verified. Reload before accepting.")
            if value is not None:
                matches = [s for s in stages if isinstance(s.get("name"), str) and
                           s["name"].strip().lower() == value.lower()]
                if len(matches) != 1:
                    raise Invalid("Choose one unambiguous saved CRM stage. Save new stages before linking them.")
                value = matches[0]["name"]
            else:
                value = ""  # Existing course policy represents no link with an empty string.
        if key == "capacity_state":
            states["capacity"] = value
        elif key in core:
            supplied[key] = ",".join(value) if key == "working_days" and value is not None else value
            if key == "working_days" and value == []:
                states["working_days"] = "set"
        else:
            path = SPECS[key]["path"]
            target = result
            for part in path[:-1]:
                if target.get(part) is None:
                    target[part] = {}
                target = target[part]
            target[path[-1]] = deepcopy(value)
            changed.add(path[0])
    confirmed = {core[k] for k in supplied} | set(states)
    if any(k in supplied and supplied[k] is not None for k in ("open_time", "close_time")):
        if any(supplied.get(k, cfg.get(k)) is None for k in ("open_time", "close_time")):
            raise Invalid("Choose opening and closing times together; neither time can be guessed.")
    if states.get("capacity") in ("unset", "off", "not_applicable") and supplied.get("capacity_per_window") is not None:
        raise Invalid("The capacity state conflicts with its proposed number.")
    if states.get("capacity") == "set" and supplied.get("capacity_per_window", cfg.get("capacity_per_window")) is None:
        raise Invalid("Choose the capacity before marking it set.")
    if states.get("capacity") == "not_applicable" and not result.get("resource_types"):
        raise Invalid("Confirm named capacity pools before marking shared capacity not applicable.")
    if states.get("capacity") == "not_applicable":
        from app import intake, setup_card_stays
        if intake.stay_mode(result) and not setup_card_stays._stay_names(result):
            raise Invalid("Appointment pools cannot replace shared stay capacity.")
    try:
        changes = booking_policy.apply_settings(cfg, supplied, states, confirmed, actor, cfg.get("booking_revision", 0))
    except (ValueError, TypeError) as exc:
        raise Invalid(str(exc)) from None
    changes.pop("booking_revision", None)  # incremented once, inside the atomic RPC
    result.update(changes)
    changed.update(changes)
    if states.get("capacity") == "not_applicable" and not booking_policy.compile_profile(result).capacity.valid:
        raise Invalid("Named capacity pools could not be verified.")
    for day in setup_rows.DAYS:
        if any(k.startswith(f"dayhours_{day}_") for k in keys):
            hours = result["day_hours"][day]
            if hours.get("open") is None and hours.get("close") is None:
                result["day_hours"].pop(day)
            elif not hours.get("open") or not hours.get("close") or hours["open"] >= hours["close"]:
                raise Invalid("Choose both opening and closing times for that day, with closing later than opening.")
    for group, switch, required in (
        ("recurring", "enabled", ("max_occurrences", "frequencies")),
        ("makeups", "on", ("per_term", "notice_hours", "within_days", "no_show_ok", "free")),
        ("courses", "waitlist", ("waitlist_notify", "offer_hours")),
        ("courses", "courses", ("reenrol_days",)),
    ):
        data = result.get(group) or {}
        if group in changed and data.get(switch) and any(data.get(k) is None for k in required):
            labels = [next(s["label"] for s in SPECS.values() if s["path"] == [group, k]) for k in required if data.get(k) is None]
            raise Invalid("Choose the missing rules before enabling this policy: " + ", ".join(labels) + ".")
        if group in changed and data.get(switch):
            for sub in required:
                key = next(k for k, s in SPECS.items() if s["path"] == [group, sub])
                try:
                    parse({"key": key, "value": data[sub]})
                except (ValueError, TypeError) as exc:
                    raise Invalid(SPECS[key]["label"] + ": " + str(exc)) from None
    if "courses" in changed and (result["courses"].get("reenrol_days") or 0) > 0 and not result["courses"].get("reenrol_notify"):
        raise Invalid("Choose how the re-enrolment invitation is sent.")
    pricing = result.get("stay_pricing") or {}
    if "stay_pricing" in changed and pricing.get("rate") is not None and not pricing.get("rate_unit"):
        raise Invalid("Choose the unit for the stay price.")
    receipts = []
    for line, fields in parsed:
        key = fields["key"]
        after = {"key": key, "value": read(result, key)}
        if key in STAGE_KEYS and not after["value"]:
            after["value"] = None
        receipt = {"id": line["id"], "name": "setting", "fields": after,
                   "before": {"value": read(cfg, key)}, "after": {"value": after["value"]}}
        if key in core or key == "capacity_state":
            setting = core.get(key, "capacity")
            receipt["review_before"] = {k: cfg.get(k) for k in booking_policy.FIELDS[setting]}
            receipt["review_after"] = {k: result.get(k) for k in booking_policy.FIELDS[setting]}
            receipt["review_after"]["state"] = result["booking_setting_review"][setting]["state"]
            receipt["notes"] = ["This booking setting is explicitly confirmed when you accept."]
        receipts.append(receipt)
    return {key: result[key] for key in changed}, receipts
