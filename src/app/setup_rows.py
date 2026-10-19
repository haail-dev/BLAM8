"""Lossless boundary between model rows and settings controls.

Legacy pipe rows are read here, once. Consumers use named fields. A row with
an unknown part is retained with problems and cannot be applied; it is never
silently shortened, shifted, or assigned to a different subject.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

SUPPORTED = {("diary_settings", "service"), ("diary_settings", "resource"),
             ("knowledge", "note"),
             ("dependants", "ladder"),
             ("diary_settings", "service_urgent"), ("diary_settings", "service_out_of_hours"), ("diary_settings", "service_after_job"),
             ("diary_settings", "setting"),
             ("invoice_settings", "setting"), ("packages", "setting"),
             ("packages", "package"),
             ("crm_settings", "setting"), ("crm_settings", "stage"),
             ("crm_fields", "contact_field"),
             ("booking_details", "setting"),
             ("booking_details", "field"), ("dependants", "group"),
             ("dependants", "field")}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
KEYS = {
    ("diary_settings", "service_urgent"): {'name', 'enabled', 'same_day_until', 'notice_hours'},
    ("diary_settings", "service_out_of_hours"): {'name', 'enabled', 'until', 'days', 'bank_holidays', 'price', 'price_after', 'offer', 'price_basis', 'terms'},
    ("diary_settings", "service_after_job"): {'name', 'tasks'},
    ("dependants", "ladder"): {'who', 'label', 'levels', 'notify', 'gates', 'staff_can_see'},
    ("knowledge", "note"): {"label", "content"},
    ("booking_details", "setting"): {"key", "value"},
    ("crm_fields", "contact_field"): {"label", "field_type", "options", "required", "extraction_hint", "probe_behaviour", "durability", "recall_days", "valid_months", "staff_can_see"},
    ("crm_settings", "setting"): {"key", "value"},
    ("crm_settings", "stage"): {"name", "stage_type", "display_order"},
    ("packages", "package"): {"name", "quantity", "unit", "price", "valid_days", "services", "required"},
    ("invoice_settings", "setting"): {"key", "value"},
    ("packages", "setting"): {"key", "value"},
    ("diary_settings", "setting"): {"key", "value"},
    ("diary_settings", "service"): {"name", "minutes", "price", "timing", "resource", "days", "at_once"},
    ("diary_settings", "resource"): {"name", "count", "rate", "rate_unit"},
    ("booking_details", "field"): {"label", "required", "kind", "hint", "services", "remember"},
    ("dependants", "group"): {"label", "plural", "cardinality"},
    ("dependants", "field"): {"kind", "label", "type", "options", "required", "durability", "recall_days", "gate", "hint"},
}


def day_codes(value):
    if isinstance(value, list):
        value = ",".join(value) if all(isinstance(x, str) for x in value) else "?"
    if not isinstance(value, str):
        raise ValueError("Choose recognised weekdays.")
    aliases = dict(zip(DAY_NAMES, DAYS)) | dict(zip(DAYS, DAYS))
    raw = value.strip().lower()
    if raw in ("weekday", "weekdays"):
        return list(DAYS[:5])
    if not raw:
        return []
    out = set()
    for chunk in re.split(r"\s*(?:[,;/&]|\band\b)\s*", raw):
        ends = re.split(r"\s*(?:[-–—]|\bto\b)\s*", chunk)
        if any(x not in aliases for x in ends) or len(ends) > 2:
            raise ValueError("Choose recognised weekdays.")
        first, last = DAYS.index(aliases[ends[0]]), DAYS.index(aliases[ends[-1]])
        if first > last:
            raise ValueError("Write a week-spanning range as separate days.")
        out.update(DAYS[first:last + 1])
    return [d for d in DAYS if d in out]


def _number(value, label, low, high, integer=False, optional=False):
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Enter a valid {label}.")
    if isinstance(value, str):
        value = value.strip()
        if label == "duration":
            value = re.sub(r"\s*(?:min|mins|minutes)$", "", value, flags=re.I)
        if label in ("price", "rate"):
            value = re.sub(r"^[£$€]\s*", "", value)
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {label}.") from None
    if not n.is_finite() or not low <= n <= high or (integer and n != n.to_integral_value()):
        raise ValueError(f"Enter a valid {label}.")
    if label in ("price", "rate") and (n == 0 or n != n.quantize(Decimal("0.01"))):
        raise ValueError(f"Enter a positive {label} with at most two decimal places, or leave it unset.")
    return int(n) if integer else format(n, "f")


def _bool(value, label):
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "required", "req", "gate", "gated"):
        return True
    if raw in ("0", "false", "no", "optional", "opt", "no gate"):
        return False
    raise ValueError(f"Choose {label} explicitly.")


def _text(value, label, limit, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.strip()) > limit:
        raise ValueError(f"Enter {label} within {limit} characters.")
    if "|" in value or re.search(r"[\]}]{2,}", value):
        raise ValueError(f"Check {label}: it contains an unexpected separator or closing fragment.")
    return value.strip()


def _enum(value, options, label):
    value = str(value).strip().lower()
    if value not in options:
        raise ValueError(f"Choose {label}: {', '.join(options)}.")
    return value


def _put(fields, key, value):
    if key in fields:
        raise ValueError(f"{key.replace('_', ' ').capitalize()} was supplied twice.")
    fields[key] = value


def _legacy(page, name, value):
    p = [part.strip() for part in value.split("|")]
    f = {}
    if (page, name) == ("diary_settings", "service"):
        f["name"] = p.pop(0)
        rest = []
        for part in p:
            m = re.fullmatch(r"(pool|resource|unit|days?|minutes|price|timing)\s*[:=]\s*(.*)", part, re.I)
            if m:
                key = m[1].lower()
                key = "resource" if key in ("pool", "unit") else "days" if key in ("day", "days") else key
                _put(f, key, m[2])
            elif part.lower() in ("time", "half", "day"):
                _put(f, "timing", part.lower())
            else:
                rest.append(part)
        for key in ("minutes", "price"):
            if key not in f and rest:
                f[key] = rest.pop(0)
        if any(rest):
            raise ValueError("The service has an unrecognised extra part.")
        # Missing timing is unresolved, never silently turned into an exact time.
        return f
    if (page, name) == ("diary_settings", "resource"):
        if len(p) > 4:
            raise ValueError("The resource has an unrecognised extra part.")
        return dict(zip(("name", "count", "rate", "rate_unit"), p))
    if (page, name) == ("dependants", "group"):
        if len(p) != 3:
            raise ValueError("Supply the kind, plural and one or several.")
        return dict(zip(("label", "plural", "cardinality"), p))
    if (page, name) == ("booking_details", "field"):
        f["label"] = p.pop(0)
        for part in p:
            low = part.lower()
            if not part:
                continue
            if low in ("required", "optional", "req", "opt"):
                _put(f, "required", low)
            elif low in ("answer", "document"):
                _put(f, "kind", low)
            elif re.match(r"^(?:only\s+)?for\s*:", part, re.I):
                _put(f, "services", re.split(r"\s*:\s*", part, maxsplit=1)[1].split(";"))
            elif re.match(r"^remember(?:ed)?\s*[:=]", part, re.I):
                _put(f, "remember", re.split(r"\s*[:=]\s*", part, maxsplit=1)[1])
            elif low.startswith("kind:"):
                _put(f, "kind", part.split(":", 1)[1].strip())
            elif low.startswith("hint:"):
                _put(f, "hint", part.split(":", 1)[1].strip())
            elif ":" not in part and "hint" not in f:
                f["hint"] = part
            else:
                raise ValueError("The booking detail has an unrecognised extra part.")
        return f
    if len(p) < 2:
        raise ValueError("Supply both the kind and the detail label.")
    f["kind"], f["label"] = p[:2]
    for part in p[2:]:
        low = part.lower()
        if not part:
            continue
        dropdown = re.fullmatch(r"(?:dropdown|drop-down|one of|choice)\s*:\s*(.+)", part, re.I)
        expiry = re.fullmatch(r"expires(?:\s+(\d+))?", low)
        if dropdown:
            _put(f, "type", "dropdown")
            _put(f, "options", re.split(r"\s*[,;]\s*", dropdown[1]))
        elif low in ("text", "number", "date", "yes/no", "yes or no", "bool", "boolean", "true/false"):
            _put(f, "type", low if low in ("text", "number", "date") else "bool")
        elif low in ("required", "optional", "req", "opt"):
            _put(f, "required", low)
        elif low in ("once", "confirm") or expiry:
            _put(f, "durability", "expires" if expiry else low)
            if expiry and expiry[1]:
                _put(f, "recall_days", expiry[1])
        elif low in ("gate", "gated", "no gate"):
            _put(f, "gate", low)
        elif low.startswith("hint:"):
            _put(f, "hint", part.split(":", 1)[1].strip())
        elif all(k in f for k in ("required", "durability", "gate")) and "hint" not in f:
            f["hint"] = part
        else:
            raise ValueError("The subject detail has an unrecognised part; specify its type and rules explicitly.")
    return f


def parse(page, name, value):
    """Return {fields, problems}; invalid rows have no partially usable fields."""
    if (page, name) not in SUPPORTED:
        return {"fields": None, "problems": []}
    try:
        if name in ('service_urgent', 'service_out_of_hours', 'service_after_job'):
            from app import setup_card_service_rules
            if isinstance(value, str): value = setup_card_service_rules.legacy(name, value)
            return {'fields': setup_card_service_rules.parse(name, value), 'problems': [], 'provided': sorted(value)}
        if (page, name) == ('dependants', 'ladder'):
            from app import setup_card_ladders
            if isinstance(value, str):
                parts = [p.strip() for p in value.split('|')]
                if len(parts) != 5:
                    raise ValueError('Supply participant, label, ordered levels, notification and prerequisites.')
                value = dict(who='contact' if parts[0].lower() == 'customer' else parts[0], label=parts[1],
                             levels=[s.strip() for s in parts[2].split(',')], notify=parts[3],
                             gates=[s.strip() for s in parts[4].split(';') if s.strip()])
            return {'fields': setup_card_ladders.parse(value), 'problems': [], 'provided': sorted(value)}
        if page == 'knowledge':
            from app import setup_card_knowledge
            if isinstance(value, str):
                label, separator, content = value.partition('|')
                if not separator:
                    raise ValueError('Supply a note label and content.')
                value = {'label': label.strip(), 'content': content.strip()}
            return {"fields": setup_card_knowledge.parse(value), "problems": [], "provided": sorted(value)}
        if (page, name) in {("booking_details", "setting"), ("diary_settings", "setting")} and isinstance(value, dict) and value.get("key") == "stay_mode":
            from app import setup_card_mode
            return {"fields": setup_card_mode.parse(value), "problems": [], "provided": sorted(value)}
        if page == "crm_fields":
            from app import setup_card_fields
            return {"fields": setup_card_fields.parse(value), "problems": [], "provided": sorted(value)}
        if page == "crm_settings":
            from app import setup_card_crm
            return {"fields": setup_card_crm.parse(name, value), "problems": [], "provided": sorted(value)}
        if (page, name) == ("packages", "package"):
            from app import setup_card_packages
            return {"fields": setup_card_packages.parse(value), "problems": [], "provided": sorted(value)}
        if page in {"invoice_settings", "packages"} and name == "setting":
            from app import setup_card_invoices
            return {"fields": setup_card_invoices.parse(page, value), "problems": [], "provided": sorted(value)}
        if (page, name) == ("diary_settings", "setting"):
            from app import setup_card_scalars
            return {"fields": setup_card_scalars.parse(value), "problems": [], "provided": sorted(value)}
        if isinstance(value, dict):
            f = deepcopy(value)
        elif isinstance(value, str) and len(value) <= 2000:
            f = _legacy(page, name, value)
        else:
            raise ValueError("The row is missing or too long.")
        if set(f) - KEYS[page, name]:
            raise ValueError("The row contains unrecognised named fields.")
        provided = sorted(f)
        if page == "diary_settings":
            f["name"] = _text(f.get("name"), "a name", 60)
            if name == "service":
                f["minutes"] = _number(f.get("minutes"), "duration", 5, 1440, integer=True)
                f["price"] = _number(f.get("price"), "price", 0, 1000000, optional=True)
                f["timing"] = _enum(f.get("timing", ""), ("time", "half", "day"), "timing")
                if "resource" in f:
                    f["resource"] = _text(f["resource"], "a resource name", 60, empty=True)
                if "days" in f:
                    f["days"] = day_codes(f["days"])
                if "at_once" in f:
                    f["at_once"] = _number(f["at_once"], "appointments at once", 1, 9999, integer=True)
            else:
                f["count"] = _number(f.get("count"), "capacity", 1, 9999, integer=True)
                f["rate"] = _number(f.get("rate"), "rate", 0, 1000000, optional=True)
                if f.get("rate") is not None:
                    f["rate_unit"] = _enum(f.get("rate_unit", ""), ("booking", "hour", "day", "night", "week"), "rate unit")
                elif f.get("rate_unit"):
                    raise ValueError("A rate unit needs its rate.")
        else:
            f["label"] = _text(f.get("label"), "a label", 60)
            if name == "group":
                f["plural"] = _text(f.get("plural"), "a plural label", 60)
                f["cardinality"] = _enum(f.get("cardinality"), ("one", "several", "many"), "one or several")
                if f["cardinality"] == "several":
                    f["cardinality"] = "many"
            else:
                f["required"] = _bool(f.get("required"), "required or optional")
                f["hint"] = _text(f.get("hint", ""), "a hint", 120 if page == "booking_details" else 200, empty=True)
                if page == "booking_details":
                    f["kind"] = _enum(f.get("kind", "answer"), ("answer", "document"), "answer or document")
                    f["remember"] = _enum(f.get("remember", "ask"), ("ask", "once", "confirm", "expires"), "how to remember it")
                    values = f.get("services", [])
                    if not isinstance(values, list) or len(values) > 30:
                        raise ValueError("Supply service names as a list.")
                    f["services"] = [_text(s, "a service name", 60) for s in values if s != ""]
                    if f["kind"] == "document" and f["remember"] != "ask":
                        raise ValueError("A document is attached per booking and cannot be remembered.")
                else:
                    f["kind"] = _text(f.get("kind"), "a subject kind", 80)
                    f["type"] = _enum(f.get("type", ""), ("text", "number", "date", "bool", "dropdown"), "an answer type")
                    f["durability"] = _enum(f.get("durability", ""), ("once", "confirm", "expires"), "how to remember it")
                    f["gate"] = _bool(f.get("gate"), "gate or no gate")
                    if f["gate"] and f["durability"] != "expires":
                        raise ValueError("A blocking gate needs an expiring date.")
                    if f["durability"] == "expires":
                        if f["type"] != "date":
                            raise ValueError("An expiring detail must be a date.")
                        f["recall_days"] = _number(f.get("recall_days"), "reminder interval", 1, 365, integer=True)
                    elif "recall_days" in f:
                        raise ValueError("A reminder interval needs an expiring date.")
                    if f["type"] == "dropdown":
                        options = f.get("options")
                        if not isinstance(options, list) or not 2 <= len(options) <= 20:
                            raise ValueError("A dropdown needs between two and twenty options.")
                        f["options"] = [_text(o, "an option", 80) for o in options]
                        if any(re.search(r"[,;/]", o) for o in f["options"]):
                            raise ValueError("An option cannot contain the editor's list separators.")
                        if len({o.casefold() for o in f["options"]}) != len(options):
                            raise ValueError("Dropdown options must be distinct.")
                    elif f.get("options"):
                        raise ValueError("Options need a dropdown answer type.")
        return {"fields": f, "problems": [], "provided": provided}
    except (ValueError, TypeError) as exc:
        return {"fields": None, "problems": [str(exc)]}


def prepare(page, values, *, saved=None):
    """Parse then validate references against this card and a successful saved read.

    saved=None means unavailable, not an empty successful read. Reference checks
    then remain unresolved unless the target exists on this card.
    """
    rows = [{**deepcopy(v), **parse(page, v.get("name"), v.get("value"))} for v in values]
    seen = {}
    for r in rows:
        f = r.get("fields")
        if not f:
            continue
        label = f.get("name") or f.get("label") or f.get("key")
        identity = (r["name"], " ".join(label.casefold().split()),
                    f.get("kind", "").casefold() if page == "dependants" and r["name"] == "field" else "")
        if r['name'] == 'ladder': identity = (r['name'], label.casefold(), f['who'].casefold())
        if identity in seen:
            message = "This row appears more than once. Keep one version before applying."
            r["problems"].append(message)
            seen[identity]["problems"].append(message)
        seen[identity] = r
    names = {"service": set(), "resource": set(), "group": set()}
    for r in rows:
        f = r.get("fields")
        if f and r["name"] in names:
            names[r["name"]].add((f.get("name") or f.get("label")).casefold())
    if saved is not None:
        for key in names:
            names[key].update(str(x).strip().casefold() for x in saved.get(key, []))
    for r in rows:
        f = r.get("fields")
        if not f:
            continue
        refs = []
        if page == "diary_settings" and r["name"] == "service" and f.get("resource"):
            refs.append(("resource", f["resource"]))
        if page == "booking_details":
            refs.extend(("service", s) for s in f.get("services", []))
        if page == "dependants" and r["name"] == "field":
            refs.append(("group", f["kind"]))
        for kind, ref in refs:
            if ref.casefold() not in names[kind]:
                r["problems"].append(f"{kind.capitalize()} '{ref}' is not on this card or in verified saved settings.")
    services = [r for r in rows if r["name"] == "service" and r.get("fields")]
    by_name = {r["fields"]["name"].casefold(): r["fields"] for r in services}
    for r in services:
        f = r["fields"]
        parts = re.split(r"\s*\+\s*|\s+and\s+", f["name"].casefold())
        if len(parts) > 1 and all(p in by_name for p in parts):
            if f["minutes"] < max(by_name[p]["minutes"] for p in parts):
                r["problems"].append("The combined service cannot be shorter than its longest part.")
    # The domain save paths cap list sizes. Refuse visibly before they could
    # truncate a newly appended row. Existing names count because filling merges.
    limits = {"diary_settings": {"service": 30, "resource": 30},
              "booking_details": {"field": 20}, "dependants": {"group": 3}}
    for name, limit in limits.get(page, {}).items():
        matching = [r for r in rows if r["name"] == name and r.get("fields")]
        prior = (saved or {}).get("intake" if page == "booking_details" else name, [])
        keys = {str(n).strip().casefold() for n in prior}
        keys.update((r["fields"].get("name") or r["fields"].get("label")).casefold() for r in matching)
        if len(keys) > limit:
            for r in matching:
                r["problems"].append(f"This page can hold at most {limit} {name} rows. Review the existing rows first.")
    if page == "dependants":
        fields_by_kind = {str(k).casefold(): {str(n).casefold() for n in v}
                          for k, v in (saved or {}).get("group_fields", {}).items()}
        for r in rows:
            if r["name"] == "field" and r.get("fields"):
                f = r["fields"]
                fields_by_kind.setdefault(f["kind"].casefold(), set()).add(f["label"].casefold())
            if r['name'] == 'ladder' and r.get('fields') and r['fields']['who'] != 'contact':
                f = r['fields']
                fields_by_kind.setdefault(f['who'].casefold(), set()).add(f['label'].casefold())
        for r in rows:
            f = r.get("fields")
            if not f:
                continue
            kind = f["kind"] if r["name"] == "field" else f["label"] if r["name"] == "group" else None
            if kind is not None:
                count = len(fields_by_kind.get(kind.casefold(), set()))
                if count == 0 or count > 10:
                    r["problems"].append("Each subject kind needs between one and ten details.")
    return rows


def recommendations(page, values):
    """Keep rejected structured rows visible; use the existing scalar catalogue."""
    from app import settings_recommend as sr
    if page not in sr.PAGES or not isinstance(values, list):
        return []
    if page not in {p for p, _ in SUPPORTED}:
        return sr.clean(page, values) or []
    out, seen = [], set()
    for item in values[:sr.MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not sr._valid_name(page, name):
            continue
        if (page, name) in SUPPORTED:
            value = item.get("value")
            result = parse(page, name, value)
            # Retain a bounded raw value for correction; the limit rejection is explicit.
            if not isinstance(value, (str, dict)) or len(str(value)) > 8000:
                value = "Row too long — please provide it again."
            out.append({"name": name, "value": value, **result})
        else:
            cleaned = sr.clean(page, [item]) or []
            if name not in sr.PAGES[page]["repeat"] and name in seen:
                continue
            out.extend(cleaned)
        seen.add(name)
    # Preserve the existing cross-row rules when moving row parsing out of
    # clean(): package charging uses durations; stay mode owns collection time.
    if page == "diary_settings":
        durations = [r["fields"]["minutes"] for r in out if r["name"] == "service" and r.get("fields")]
        if durations and any(m != 60 for m in durations):
            for r in out:
                if r["name"] == "hours_unit":
                    r["value"] = "minutes"
    if page == "booking_details" and any(r["name"] == "stay_mode" and str(r["value"]).strip() in ("1", "true", "yes") for r in out):
        out = [r for r in out if not (r["name"] == "field" and r.get("fields") and
               any(w in r["fields"]["label"].lower() for w in sr._COLLECTION_WORDS))]
    return out


def describe(page, name, fields, provided=None):
    """Card facts come from parsed fields; do not repeat unparsed model prose."""
    from app import settings_recommend as sr
    if name in ('service_urgent', 'service_out_of_hours', 'service_after_job'):
        return fields['name'] + ' · ' + name.replace('service_', '').replace('_', ' ')
    if name == 'ladder':
        return fields['who'] + ': ' + fields['label'] + ' · ' + ' → '.join(fields.get('levels', []))
    if page == 'knowledge':
        return fields['label'] + ': ' + fields['content']
    if page == "crm_fields":
        from app import setup_card_fields
        return setup_card_fields.describe(fields)
    if page == "crm_settings":
        from app import setup_card_crm
        return setup_card_crm.describe(name, fields)
    if (page, name) == ("packages", "package"):
        from app import setup_card_packages
        return setup_card_packages.describe(fields)
    if name == "setting":
        if (page, name) == ("booking_details", "setting") or (page, name) == ("diary_settings", "setting") and fields.get("key") == "stay_mode":
            from app import setup_card_mode
            return setup_card_mode.describe(fields)
        if page in {"invoice_settings", "packages"}:
            from app import setup_card_invoices
            return setup_card_invoices.describe(fields)
        from app import setup_card_scalars
        return setup_card_scalars.describe(fields)
    f = fields
    included = set(f if provided is None else provided)
    if name == "service":
        bits = [f["name"], f"{f['minutes']} min", sr._WINDOW_WORDS[f["timing"]]]
        if f.get("price") is not None:
            bits.append(sr._money(f["price"]))
        if f.get("resource"):
            bits.append("on " + f["resource"])
        if f.get("days"):
            bits.append(sr.days_words(",".join(f["days"])))
        if "at_once" in f:
            bits.append(f"{f['at_once']} at once")
    elif name == "resource":
        bits = [f["name"], f"capacity {f['count']}"]
        if f.get("rate") is not None:
            bits.append(sr._money(f["rate"]) + " per " + f["rate_unit"])
    elif name == "group":
        bits = [f["label"], f["plural"], "one" if f["cardinality"] == "one" else "several"]
    else:
        bits = [f["label"], "required" if f["required"] else "optional"]
        if page == "booking_details":
            if "kind" in included:
                bits.append(f["kind"])
            if "remember" in included:
                bits.append("remember: " + f["remember"])
            if f["services"]:
                bits.append("only for: " + "; ".join(f["services"]))
        else:
            bits.extend(["for " + f["kind"], f["type"], f["durability"], "gate" if f["gate"] else "no gate"])
            if f.get("recall_days"):
                bits.append(f"remind {f['recall_days']} days before expiry")
            if f.get("options"):
                bits.append(", ".join(f["options"]))
        if "hint" in included and f.get("hint"):
            bits.append(f["hint"])
    return " — ".join(bits)
