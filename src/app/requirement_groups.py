"""Dependants — the booking is about something that belongs to the customer.

docs/modules/module-requirement-groups.md ("Copied Requirements"; Kris's word for
it: each contact has *Dependencies* — pets, children, cars, items). A tenant
defines up to three GROUPS in its own words ("Horse", "Dog", "Vehicle"), each
a short list of fields with a durability: `once` (collect on first meeting,
never re-ask), `confirm` (prefill, re-confirm each booking) or `expires` (a
date that invalidates itself — a vaccine, an MOT — and, when `gate` is on,
must cover the booking's date). A filled copy is an INSTANCE ("Murphy") on
the customer's record; a booking links the instances it is ABOUT (its
SUBJECTS). The gate evaluates per instance: Murphy's lapsed vaccine blocks
Murphy, Tilly is unaffected.

Nothing here knows what a horse is. Labels are tenant text; the mechanics
are fixed and small: ask, remember, prefill, gate. Pure — no DB, no I/O —
so every rule is unit-tested in isolation; src/app/db.py holds the rows.

The three runs that proved the need: kennels (two dogs' facts in one
booking), plumbing (facts per property), farriery (every per-horse field
became a "Murphy; Tilly" string, and the portal refused the second horse's
trim as a duplicate of the first horse's visit)."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime

MAX_GROUPS = 3
MAX_FIELDS = 10
MAX_INSTANCES_PER_CONTACT = 20
MAX_SUBJECTS_PER_BOOKING = 6
TYPES = ("text", "number", "date", "bool", "dropdown")
DURABILITY = ("once", "confirm", "expires")
_MAX_LABEL = 60
_MAX_VALUE = 300


# ── normalising ──────────────────────────────────────────────────────────────
def _norm(s) -> str:
    s = re.sub(r"[_\W]+", " ", str(s or "").lower())
    return " ".join(s.split())


def key_for(label: str) -> str:
    """A stable key from the operator's label: 'Passport expiry' → passport_expiry."""
    k = re.sub(r"[^a-z0-9]+", "_", str(label or "").lower()).strip("_")
    return k[:40] or "field"


def _plural(label: str) -> str:
    l = (label or "").strip()
    if not l:
        return ""
    if re.search(r"(s|x|z|ch|sh)$", l, re.I):
        return l + "es"
    if re.search(r"[^aeiou]y$", l, re.I):
        return l[:-1] + "ies"
    return l + "s"


# ── definitions ──────────────────────────────────────────────────────────────
def clean_field(f) -> dict | None:
    if not isinstance(f, dict):
        return None
    label = " ".join(str(f.get("label") or "").split())[:_MAX_LABEL]
    if not label:
        return None
    ftype = str(f.get("type") or "text").strip().lower()
    if ftype not in TYPES:
        ftype = "text"
    dur = str(f.get("durability") or "once").strip().lower()
    if dur not in DURABILITY:
        dur = "once"
    if dur == "expires":
        ftype = "date"                      # an expiry IS a date, whatever the form said
    options = [str(o).strip() for o in (f.get("options") or []) if str(o).strip()][:20]
    out = {"key": str(f.get("key") or "").strip() or key_for(label),
           "label": label, "type": ftype, "options": options,
           "required": bool(f.get("required")),
           "hint": " ".join(str(f.get("hint") or "").split())[:200],
           "durability": dur,
           "gate": bool(f.get("gate")) and dur == "expires"}
    # Proof (Kris 2026-09-21, from the vaccination take): a gated date the
    # CUSTOMER can simply type is not evidence. Little Paws' own website says
    # "evidence of yearly booster jabs must be provided annually" — but that
    # sentence lived in the knowledge base, which a model may or may not act
    # on, while the gate could only ever ask for a value. With `proof` on, the
    # gate also needs a document on that dependant's record: the date says
    # when, the document says it is true. Only meaningful on a gate.
    out["proof"] = bool(f.get("proof")) and out["gate"]
    # Who is allowed to CHANGE this fact (Kris 2026-09-21). `proof` asks "is
    # there a document?"; this asks the question underneath it — and it is the
    # business's call, not ours. A kennel with a licence at stake wants a person
    # to confirm a vaccination expiry however sure the model is; a dog walker
    # will take the customer's word. "owner" never autofills: the read becomes a
    # one-click confirm chip instead (src/app/dependants_extract.py), whatever the
    # confidence. Default "customer" — no existing tenant changes behaviour.
    out["set_by"] = "owner" if str(f.get("set_by") or "").strip().lower() == "owner" else "customer"
    if dur == "expires":
        # The recall lead (slice 4, 2026-09-05): 30 days was everyone's until
        # first aid needed 90 — now the field's own number.
        try:
            out["recall_days"] = max(1, min(365, int(float(str(f.get("recall_days") or 30)))))
        except (TypeError, ValueError):
            out["recall_days"] = 30
    # A progress ladder (src/app/ladders.py, 2026-09-05): an ordered dropdown the
    # business moves a dependant along — kept only when it IS a dropdown with
    # levels, and always a confirm fact (the customer reads it, never sets it).
    if isinstance(f.get("ladder"), dict) and ftype == "dropdown" and len(options) >= 2:
        from app import ladders as _ladders
        out["durability"] = "confirm"
        out["gate"] = False
        out["ladder"] = _ladders.clean_block(f["ladder"], options)
    return out


def clean_group(g) -> dict | None:
    """One group definition, validated: the operator's singular word, its
    plural, one-or-many per contact, and up to MAX_FIELDS fields (deduped
    by label). None when there is no label or no field."""
    if not isinstance(g, dict):
        return None
    label = " ".join(str(g.get("label") or "").split())[:_MAX_LABEL]
    if not label:
        return None
    seen, fields = set(), []
    for f in (g.get("fields") or []):
        cf = clean_field(f)
        if not cf or _norm(cf["label"]) in seen:
            continue
        seen.add(_norm(cf["label"]))
        fields.append(cf)
        if len(fields) >= MAX_FIELDS:
            break
    if not fields:
        return None
    card = str(g.get("cardinality") or "many").strip().lower()
    out = {"label": label,
           "label_plural": " ".join(str(g.get("label_plural") or "").split())[:_MAX_LABEL] or _plural(label),
           "cardinality": card if card in ("one", "many") else "many",
           "fields": fields}
    if g.get("id"):
        out["id"] = g["id"]
    if g.get("display_order") is not None:
        out["display_order"] = g["display_order"]
    return out


def clean_groups(groups) -> list[dict]:
    seen, out = set(), []
    for g in (groups or []):
        cg = clean_group(g)
        if not cg or _norm(cg["label"]) in seen:
            continue
        seen.add(_norm(cg["label"]))
        out.append(cg)
        if len(out) >= MAX_GROUPS:
            break
    for i, g in enumerate(out):
        g.setdefault("display_order", i)
    return out


def find_group(groups, name) -> dict | None:
    """The group a subject belongs to — by label, plural or id, loosely; the
    only group when the tenant has one and no name was given."""
    groups = list(groups or [])
    n = _norm(name)
    if not n:
        return groups[0] if len(groups) == 1 else None
    for g in groups:
        if n in (_norm(g.get("label")), _norm(g.get("label_plural")), _norm(g.get("id"))):
            return g
    for g in groups:                         # 'horse' ~ 'horses', 'dogs' ~ 'dog'
        if n.rstrip("s") == _norm(g.get("label")).rstrip("s"):
            return g
    return groups[0] if len(groups) == 1 else None


def _ensure(group: dict) -> dict:
    """A group as stored (keys present) or as typed in a test/tool (labels
    only): either way the fields carry their keys from here on."""
    if all("key" in (f or {}) for f in (group or {}).get("fields") or []):
        return group
    return clean_group(group) or {"label": "", "fields": []}


def field_for(group: dict, label) -> dict | None:
    group = _ensure(group)
    n = _norm(label)
    if not n:
        return None
    for f in group.get("fields") or []:
        if n in (_norm(f["label"]), _norm(f["key"])):
            return f
    return None


# ── values ───────────────────────────────────────────────────────────────────
def _is_placeholder(v) -> bool:
    try:
        from app.intake import is_placeholder
        return is_placeholder(v)
    except Exception:                        # pure fallback
        return not str(v or "").strip()


def parse_date(v) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def coerce_value(field: dict, value):
    """Store what was said, in the field's shape: bools to Yes/No words the
    operator reads, numbers as numbers, dates as ISO when they parse, and
    everything else as trimmed text."""
    if value is None:
        return None
    s = str(value).strip()[:_MAX_VALUE]
    if not s:
        return None
    t = field.get("type")
    if t == "bool":
        low = s.lower()
        if low in ("yes", "y", "true", "1"):
            return "Yes"
        if low in ("no", "n", "false", "0"):
            return "No"
        return s
    if t == "number":
        try:
            n = float(s.replace(",", ""))
            return int(n) if n.is_integer() else n
        except ValueError:
            return s
    if t == "date":
        d = parse_date(s)
        return d.isoformat() if d else s
    return s


def normalise_data(group: dict, raw: dict | None) -> dict:
    """{label-or-key: value} as the model wrote it → {key: value} for the
    group's fields, loosely matched; unknown labels dropped; placeholders
    dropped (a placeholder is not an answer — the same rule as the booking
    gate)."""
    out: dict = {}
    group = _ensure(group)
    for k, v in (raw or {}).items():
        f = field_for(group, k)
        if not f or _is_placeholder(v):
            continue
        cv = coerce_value(f, v)
        if cv is not None:
            out[f["key"]] = cv
    return out


def missing_for(group: dict, data: dict | None, booking_date=None,
                has_proof: bool | set[str] | None = None, until=None) -> list[str]:
    """What still stands between THIS instance and a booking: required fields
    with no real value, gated expiry dates that don't cover the booking, and —
    on a `proof` field — a gate with no document behind it. Labels, in the
    operator's words, so the refusal can say 'Still needed for Murphy:
    Passport expiry'.

    `until` is the last day the booking runs to — a stay's departure. A date
    has to last the WHOLE booking, not just see the dog through the gate on
    arrival (Kris 2026-09-22): jabs lapsing on the Sunday of a Friday-to-Monday
    stay leave an unvaccinated dog in the kennels for a night, and the gate
    used to wave that through because it only looked at the arrival. Lasting
    TO the departure day is enough — the dog goes home that day.

    `has_proof` is whether a document sits on this dependant's record. None
    means the caller could not cheaply find out, and the proof check is then
    skipped rather than guessed at — a surface that merely LISTS gaps must
    never invent one. The booking gate (src/app/scheduling.py) always passes it,
    because that is the call where being wrong costs something.

    Booking gates pass a set of explicitly evidenced field keys. A document
    linked only to the subject proves no particular requirement. Boolean
    input remains supported for callers reporting a single aggregate status;
    None is for display-only callers, never a successful booking check."""
    data = data or {}
    group = _ensure(group)
    bd = parse_date(booking_date) if booking_date else None
    ud = parse_date(until) if until else None
    out = []
    for f in group.get("fields") or []:
        v = data.get(f["key"])
        empty = v is None or _is_placeholder(v)
        if f.get("required") and empty:
            out.append(f["label"])
            continue
        if f.get("gate") and not empty:
            d = parse_date(v)
            if d is None:
                out.append(f"{f['label']} (not a date: {v})")
                continue
            if bd and d < bd:
                out.append(f"{f['label']} (expired {d.isoformat()}, before the booking)")
                continue
            if ud and d < ud:
                out.append(f"{f['label']} (lapses {d.isoformat()}, part-way through the "
                           f"booking — it has to last until {ud.isoformat()})")
                continue
            proven = (f["key"] in has_proof if isinstance(has_proof, (set, frozenset))
                      else has_proof)
            if f.get("proof") and proven is False:
                out.append(f"{f['label']} (nothing on file — we need to see the "
                           "certificate itself, not just the date)")
    return out


def merge(existing_data: dict | None, existing_meta: dict | None, incoming: dict,
          group: dict, *, source: str = "extracted", email_log_id: str | None = None,
          now_iso: str | None = None) -> tuple[dict, dict, list[str]]:
    """Fold new values into an instance by durability. `once` fields already
    on record are never overwritten by a read (the first answer is the
    answer; the operator can edit); `confirm` and `expires` fields take the
    newest value. Returns (data, meta, changed labels)."""
    data = dict(existing_data or {})
    meta = dict(existing_meta or {})
    changed: list[str] = []
    group = _ensure(group)
    stamp = now_iso or datetime.now().isoformat(timespec="seconds")
    for f in group.get("fields") or []:
        k = f["key"]
        if k not in incoming or incoming[k] in (None, ""):
            continue
        cur = data.get(k)
        if cur not in (None, "") and f.get("durability") == "once" and source in ("extracted", "customer"):
            continue                          # the first answer stands; only the operator rewrites it
        if cur == incoming[k]:
            continue
        data[k] = incoming[k]
        meta[k] = {"source": source, "at": stamp, "email_log_id": email_log_id}
        changed.append(f["label"])
    return data, meta, changed


# ── the tool argument ────────────────────────────────────────────────────────
def subjects_from_args(args: dict | None, groups) -> tuple[list[dict], str | None]:
    """hold_booking / create_booking `subjects`: [{name, group?, fields{}}] as
    the model wrote it → [{group, name, data{key: value}}]. Returns
    (subjects, error message). A subject needs a name and a group it can be
    matched to; an unmatched group is an error the model can fix."""
    raw = (args or {}).get("subjects")
    if not raw:
        return [], None
    if isinstance(raw, (dict, str)):
        raw = [raw]
    if not isinstance(raw, list):
        return [], "`subjects` must be a list of {name, group, fields}."
    out = []
    for item in raw[:MAX_SUBJECTS_PER_BOOKING]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:80]
        if not name:
            return [], "Every subject needs a `name` — the word the customer uses for it."
        g = find_group(groups, item.get("group"))
        if not g:
            names = ", ".join(x["label"] for x in groups)
            return [], (f"Which kind is '{name}'? Set `group` to one of: {names}.")
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else \
            (item.get("details") if isinstance(item.get("details"), dict) else {})
        # Fields the model DID supply but as a bare placeholder ("None", "n/a"):
        # normalise_data drops them; the gate needs to know they were given so
        # its refusal can teach the rephrase (run #7: "Finn has no medical
        # conditions" -> "None" -> refused -> the parent was asked to repeat it).
        placeholders = {}
        for k, v in fields.items():
            f = field_for(g, k)
            if f and str(v or "").strip() and _is_placeholder(v):
                placeholders[f["label"]] = str(v).strip()
        out.append({"group": g, "name": name, "data": normalise_data(g, fields),
                    "placeholders": placeholders})
    return out, None


def with_subjects(tools, groups) -> list[dict]:
    """Deep-copy a tool list and give every booking-placing tool a `subjects`
    argument spelled in the tenant's words — the exact labels, first call.
    The module-level tool constants stay byte-stable."""
    groups = clean_groups(groups)
    if not groups:
        return list(tools or [])
    desc = ("The " + " / ".join(g["label_plural"].lower() for g in groups)
            + " this booking is ABOUT — one entry per " + " / ".join(g["label"].lower() for g in groups)
            + ", NEVER several in one string. Each: {name, group, fields}. ")
    for g in groups:
        keys = ", ".join(f"'{f['label']}'" for f in g["fields"])
        desc += (f"group '{g['label']}' fields (use EXACTLY these keys in `fields`): {keys}. ")
    desc += ("Give every value the customer stated; a field already on the customer's record "
             "may be left out (it is kept). Required fields missing on any subject refuse the "
             "booking and name the subject.")
    schema = {"type": "array", "description": desc,
              "items": {"type": "object",
                        "properties": {"name": {"type": "string"},
                                       "group": {"type": "string"},
                                       "fields": {"type": "object"}},
                        "required": ["name"]}}
    out = []
    for t in tools or []:
        props = ((t.get("input_schema") or {}).get("properties") or {})
        # Every tool the executor sends through the intake gate must be able to
        # CARRY what that gate requires. Live 2026-09-22: offer_stay shipped
        # without being added here, so the gate refused it for missing
        # `subjects` while the schema gave the model no way to send any — the
        # model retried four times and the draft never finished. A tool missing
        # from this list is not "unsupported", it is unwinnable.
        if t.get("name") in ("hold_booking", "offer_slots", "offer_stay",
                             "create_booking", "join_waitlist") and "details" in props:
            t = copy.deepcopy(t)
            t["input_schema"]["properties"]["subjects"] = copy.deepcopy(schema)
        out.append(t)
    return out


# ── prompt rendering ─────────────────────────────────────────────────────────
def _dur_words(f: dict) -> str:
    if f.get("durability") == "once":
        return "ask once, never again"
    if f.get("durability") == "confirm":
        return "on record: confirm it still stands in one short line, never ask afresh"
    if not f.get("gate"):
        return "an expiry date"
    words = ("an expiry date — must cover the whole booking (right through to "
             "its last day, not just the first) or the booking waits")
    if f.get("proof"):
        words += ", and the certificate itself must be on file: a date the customer types is not evidence"
    if f.get("set_by") == "owner":
        words += ". The customer cannot set this one: whatever they send goes to the "
        words += "business to confirm, so never tell them it is updated or booked on the strength of it"
    return words


def render_for_prompt(groups) -> str:
    """The definition block for every agent surface: what a booking may be
    about, the fields per kind, and how to pass them. Empty when no group is
    defined — the zero-config tenant's prompt is byte-identical."""
    groups = clean_groups(groups)
    if not groups:
        return ""
    lines = ["Dependants — this business's bookings are ABOUT something that belongs to the "
             "customer, and each one is its own record with its own facts. Never squeeze two "
             "into one answer ('Murphy; Tilly'): pass them as separate entries in the booking "
             "tool's `subjects` argument, one per " + " / ".join(g["label"].lower() for g in groups)
             + ", each with its `name` and its `fields` keyed EXACTLY by the labels below."]
    for g in groups:
        many = "a customer may have several" if g["cardinality"] == "many" else "one per customer"
        lines.append(f"{g['label']} ({g['label_plural']}; {many}):")
        for f in g["fields"]:
            req = "REQUIRED" if f.get("required") else "optional"
            opts = f" one of {', '.join(f['options'])}" if f.get("options") else ""
            hint = f" — {f['hint']}" if f.get("hint") else ""
            lines.append(f"- {f['label']} ({req}; {_dur_words(f)}{opts}){hint}")
    from app import ladders as _ladders
    lad = _ladders.render_for_prompt(groups)
    if lad:
        lines.append("Progress levels (moved by the business, never from the customer's say-so):\n" + lad)
    lines.append("A required field missing on a subject refuses the hold and names the subject "
                 "('Still needed for Murphy: …') — ask the customer for exactly that, for that "
                 "one. Facts about a subject the customer already gave, anywhere in the thread "
                 "or in the customer context, count as collected: key them yourself. When the "
                 "customer says there are NONE (no medical conditions, no known issues), write "
                 "it in their words — 'No medical conditions — parent confirmed' — never a bare "
                 "'none' or 'n/a': a bare placeholder reads as unknown and is refused.")
    return "\n".join(lines)


def instance_line(group: dict, inst: dict) -> str:
    group = _ensure(group)
    data = inst.get("data") or {}
    bits = []
    for f in group.get("fields") or []:
        v = data.get(f["key"])
        if v in (None, ""):
            continue
        bits.append(f"{f['label']}: {v}")
    return f"{inst.get('name')}" + (" — " + "; ".join(bits) if bits else " — nothing on record yet")


def render_known(groups, instances, booking_date=None, proof_ids=None, proof_keys=None) -> str:
    """The customer's own dependants for the customer context: what is on
    record per instance, what is missing, and which expiries have lapsed —
    so the drafter re-asks nothing that is known and asks exactly what
    isn't."""
    groups = clean_groups(groups)
    by_id = {g.get("id"): g for g in groups if g.get("id")}
    by_label = {_norm(g["label"]): g for g in groups}
    lines = []
    for inst in instances or []:
        g = by_id.get(inst.get("group_id")) or by_label.get(_norm(inst.get("group_label")))
        if not g:
            continue
        line = f"{g['label']}: {instance_line(g, inst)}"
        has_proof = (None if proof_ids is None
                     else bool(inst.get("id") and inst["id"] in proof_ids))
        if proof_keys is not None:
            has_proof = set(proof_keys.get(inst.get("id"), []))
        gaps = missing_for(g, inst.get("data"), booking_date, has_proof)
        if gaps:
            line += f". Still needed: {', '.join(gaps)}"
        lines.append(line)
    if not lines:
        return ""
    return ("Known dependants for this customer (on record — never re-ask a value shown; "
            "pass the name in `subjects` and only the fields that are new or changed). "
            "When a note here bears on what is being booked — a temperament, a "
            "medication, an access need — say back in one short clause that it is on "
            "record for this booking. They told you once and should not have to say it "
            "again, and being told it was remembered is most of why they trust you "
            "with it. Do not promise an arrangement the business has not confirmed; "
            "say what is known, not what will be done about it:\n"
            + "\n".join("- " + ln for ln in lines))


def summary_of(subjects: list[dict]) -> str:
    """'Murphy, Tilly' — for the booking's details/summary so every existing
    surface (diary, calendar, Today) shows what the visit is about."""
    return ", ".join(s["name"] for s in subjects if s.get("name"))


# ── the settings form ────────────────────────────────────────────────────────
def parse_editor_form(g_label, g_plural, g_cardinality, f_group, f_label, f_type,
                      f_required, f_durability, f_gate, f_hint, g_id=None,
                      f_recall=None, f_proof=None, f_setby=None,
                      f_options=None, existing=None) -> list[dict]:
    """Positionally aligned arrays from the Dependants editor → clean groups.
    Each field row names the group index it belongs to (f_group). `f_options`
    is the comma-separated options of a "One of" row; when none is posted for a
    dropdown (an old form post), the stored options of the same-labelled field
    in the same-labelled group in `existing` are kept, never dropped."""
    stored: dict[tuple, list] = {}
    for eg in clean_groups(existing or []):
        for ef in eg["fields"]:
            if ef.get("options"):
                stored[(_norm(eg["label"]), _norm(ef["label"]))] = list(ef["options"])
    groups: list[dict] = []
    for i, label in enumerate(g_label or []):
        groups.append({"label": label,
                       "label_plural": (g_plural or [""] * len(g_label))[i] if i < len(g_plural or []) else "",
                       "cardinality": (g_cardinality or [])[i] if i < len(g_cardinality or []) else "many",
                       "id": (g_id or [])[i] if g_id and i < len(g_id) and (g_id or [])[i] else None,
                       "fields": []})
    n = len(f_label or [])

    def at(xs, i, default=""):
        return xs[i] if xs and i < len(xs) else default

    for i in range(n):
        try:
            gi = int(at(f_group, i, "0") or 0)
        except ValueError:
            gi = 0
        if not (0 <= gi < len(groups)):
            continue
        ftype = at(f_type, i, "text")
        opts = [o.strip() for o in str(at(f_options, i, "")).split(",") if o.strip()][:20]
        # Only an OLD form (no options posted at all) falls back to what is
        # stored; a box the owner blanked on the page clears the options.
        if ftype == "dropdown" and not opts and not f_options:
            opts = stored.get((_norm(groups[gi]["label"]), _norm(at(f_label, i))), [])
        groups[gi]["fields"].append({
            "label": at(f_label, i), "type": ftype,
            "options": opts if ftype == "dropdown" else [],
            "required": str(at(f_required, i, "0")) in ("1", "on", "true"),
            "durability": at(f_durability, i, "once"),
            "gate": str(at(f_gate, i, "0")) in ("1", "on", "true"),
            "proof": str(at(f_proof, i, "0")) in ("1", "on", "true"),
            "set_by": "owner" if str(at(f_setby, i, "0")) in ("1", "on", "true") else "customer",
            "hint": at(f_hint, i),
            "recall_days": at(f_recall, i, "30") or "30"})
    return clean_groups(groups)
