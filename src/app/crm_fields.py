"""CRM custom fields (phase 2 — docs/architecture/CRM_design.md §4/§9/§11).

The tenant defines what they track per entity (label + type + an
extraction_hint in plain English); the LLM (src/app/crm_extract.py) then fills the
values from every email. This module owns everything between the two: slugging
and validation, the 10-per-entity cap, typed coercion of raw extractions, and
the confidence gate — ≥0.75 writes custom_data itself, 0.5–0.75 queues a
one-click-confirm suggestion, below that is dropped. Vertical seed suggestions
come from src/app/industry_lingo.py CRM_SEEDS. Special-category (health) fields
skip all of this — see is_special_category.
"""
import logging
import re

from app import crm_visibility, db, industry_lingo

log = logging.getLogger(__name__)

ENTITIES = ("company", "contact", "deal")
FIELD_TYPES = ("text", "number", "dropdown", "date", "bool")
PROBE_BEHAVIOURS = ("passive", "probe_once", "probe_persistent")
# Certificates on contacts (docs/plan-progress-packages.md slice 4, 2026-09-05):
# a DATE field may EXPIRE — a first-aid certificate, a lifeguard NPLQ, a dive
# medical — and raise a recall `recall_days` before (30 is the referenced inspection reminder's lead time; a
# three-year certificate wants 90). Same words as a dependant's field.
DURABILITY = ("once", "expires")
RECALL_DEFAULT = 30

MAX_FIELDS_PER_ENTITY = 10   # guardrail §11: onboarding anxiety must not bloat the schema

AUTOFILL_AT = 0.75           # ≥ this: write custom_data directly
SUGGEST_AT = 0.50            # ≥ this (< AUTOFILL_AT): queue for one-click confirm

_TRUE_WORDS = {"true", "yes", "y", "1", "on"}
_FALSE_WORDS = {"false", "no", "n", "0", "off"}


_LEVEL_WORDS = re.compile(r"\b(?:level|stage|grade|belt|ladder|progress)\b", re.IGNORECASE)

# ── Special-category fields (security review M39, 2026-09-27) ────────────────
# Health, medical and allergy facts are special-category data (UK GDPR Art. 9).
# A field that holds them is NEVER filled or suggested from email, NEVER probed
# for, and NEVER put into an AI prompt (crm_intel.build_crm_context). The owner
# types the value in by hand.
#
# How a field is known to be special-category, in order:
#   1. the definition itself carries "sensitive": True (a preset seed does);
#   2. it matches a seed a preset marks "sensitive" (industry_lingo) — needed
#      because crm_field_definitions has no column for the flag, so an accepted
#      seed arrives in the database without it;
#   3. FALLBACK: its own label names health data in generic words. An owner can
#      type "Allergies" as a field of their own, and no preset can cover that.
#      These words are about the kind of data, not about any type of business.
#      Date fields are left out of the fallback: a date says when something is
#      due, not the health fact itself.
_SPECIAL_CATEGORY_WORDS = re.compile(
    r"\b(?:health|medical|medications?|allerg(?:y|ies|ic|ens?)|clinical)\b", re.IGNORECASE)


def is_special_category(definition: dict | None) -> bool:
    """True when this field holds health / medical / allergy data (see above)."""
    if not definition:
        return False
    if definition.get("sensitive"):
        return True
    entity = definition.get("entity")
    label = str(definition.get("label") or "")
    key = definition.get("key") or slug(label)
    for s_entity, s_label in industry_lingo.sensitive_seed_fields():
        if s_entity == entity and slug(s_label) == key:
            return True
    if definition.get("field_type") == "date":
        return False
    return bool(_SPECIAL_CATEGORY_WORDS.search(label.replace("_", " ")))


def slug(label: str) -> str:
    """A stable custom_data key from a display label: 'Fleet size' → 'fleet_size'."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return s[:60]


class FieldError(ValueError):
    """A human-readable validation failure — the page surfaces .args[0] verbatim."""


def clean_recall(value, default: int = RECALL_DEFAULT) -> int:
    try:
        return max(1, min(365, int(float(str(value).strip()))))
    except (TypeError, ValueError):
        return default


VALID_MAX_MONTHS = 600


def clean_valid(value) -> int:
    """How long the thing runs from the date on record, in MONTHS — 0 means the
    stored date IS the expiry (an inspection due date); 24 means the date is when it
    was issued and it lapses two years on, same day of the month (a theory
    test pass, run #8: "3 March 2026" read as lapsed the day it was
    extracted). Months, not days: certificates run in years, and 730 days
    across a leap year lands a day early."""
    try:
        return max(0, min(VALID_MAX_MONTHS, int(float(str(value).strip()))))
    except (TypeError, ValueError):
        return 0


def add_months(d, months: int):
    """Calendar months on, clamped to the month's last day (31 Jan + 1 = 28/29 Feb)."""
    import calendar
    from datetime import date as _date
    m = d.month - 1 + int(months)
    y = d.year + m // 12
    m = m % 12 + 1
    return _date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def expiry_date(definition: dict, value):
    """The date an expiring field lapses: the stored date plus its validity."""
    from app.requirement_groups import parse_date
    if (definition or {}).get("durability") != "expires":
        return None
    d = parse_date(value)
    if d is None:
        return None
    return add_months(d, clean_valid(definition.get("valid_months")))


def validate_definition(entity: str, label: str, field_type: str,
                        options: list | None, extraction_hint: str,
                        required: bool = False, durability: str = "once",
                        recall_days=None, valid_months=None,
                        probe_behaviour: str = "passive") -> dict:
    """Shape one definition row (raises FieldError with the message to show)."""
    if entity not in ENTITIES:
        raise FieldError("Unknown entity.")
    label = (label or "").strip()
    if not label:
        raise FieldError("The field needs a name.")
    key = slug(label)
    if not key:
        raise FieldError("The field name needs at least one letter or number.")
    if field_type not in FIELD_TYPES:
        raise FieldError("Unknown field type.")
    if probe_behaviour not in PROBE_BEHAVIOURS:
        raise FieldError("Unknown probe behaviour.")
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if field_type == "dropdown" and len(opts) < 2:
        raise FieldError("A dropdown needs at least two options.")
    dur = str(durability or "once").strip().lower()
    dur = "expires" if dur == "expires" and field_type == "date" else "once"
    return {"entity": entity, "key": key, "label": label, "field_type": field_type,
            "options": opts if field_type == "dropdown" else [],
            "required": bool(required),
            "probe_behaviour": probe_behaviour,
            "extraction_hint": (extraction_hint or "").strip() or None,
            "durability": dur, "recall_days": clean_recall(recall_days),
            # Certificates with a validity in months (run #8): 0 = the date is the expiry.
            "valid_months": clean_valid(valid_months) if dur == "expires" else 0}


def expiring(definitions: list[dict] | None, entity: str = "contact") -> list[dict]:
    """The definitions whose value is a date that lapses."""
    return [d for d in (definitions or []) if d.get("entity") == entity
            and d.get("durability") == "expires" and d.get("field_type") == "date"]


def expiry_note(definition: dict, value, today) -> str:
    """"lapses 12th Dec 26 (in 98 days)" / "lapsed 1st Sep 26" / "" — for the
    card and the drafter, in the operator's words."""
    from app.dates import pretty_date
    d = expiry_date(definition, value)          # the stored date + its validity
    if d is None:
        return ""
    days = (d - today).days
    if days < 0:
        return f"lapsed {pretty_date(d.isoformat())}"
    if days == 0:
        return "lapses today"
    return f"lapses {pretty_date(d.isoformat())} (in {days} day{'' if days == 1 else 's'})"


def expiry_lines(definitions: list[dict] | None, record: dict | None, today) -> list[str]:
    """One line per expiring contact field with a value: "First aid certificate:
    lapses 12th Dec 26 (in 98 days) — renewal due within 90 days" when inside
    the recall window, else the plain note."""
    data = (record or {}).get("custom_data") or {}
    out = []
    for d in expiring(definitions):
        note = expiry_note(d, data.get(d.get("key")), today)
        if not note:
            continue
        due = expiry_date(d, data.get(d.get("key")))
        recall = int(d.get("recall_days") or RECALL_DEFAULT)
        inside = due is not None and (due - today).days <= recall
        out.append(f"{d['label']}: {note}" + (f" — a renewal is due (the business recalls "
                                                f"{recall} days ahead)" if inside else ""))
    return out


def add_field(tenant_id: str, definition: dict, client=None) -> dict:
    """Insert one validated definition, enforcing the per-entity cap and
    duplicate keys with readable messages."""
    existing = db.crm_list_field_definitions(tenant_id, entity=definition["entity"],
                                             client=client)
    definition = prepare_field(definition, existing)
    return db.crm_insert_field_definition(tenant_id, definition, client=client)


def prepare_field(definition: dict, existing: list[dict]) -> dict:
    """Validate an addition against the original form's definitions, without writes."""
    if len(existing) >= MAX_FIELDS_PER_ENTITY:
        raise FieldError(f"That's the lot — max {MAX_FIELDS_PER_ENTITY} fields per "
                         "entity, so the schema stays honest. Remove one first.")
    if any(d["key"] == definition["key"] for d in existing):
        raise FieldError(f"You already track \"{definition['label']}\" there.")
    # A progress level is a LADDER, and one already sits on the customer record
    # (run #8: the card wrote "Current DVSA skill level | text" beside the ladder
    # it had written ten minutes earlier — two fields for one fact).
    ladder = next((d for d in existing if isinstance(d.get("ladder"), dict)), None)
    if ladder and definition.get("entity") == "contact" and _LEVEL_WORDS.search(definition.get("label") or ""):
        raise FieldError(f"\"{definition['label']}\" is a progress level — that is the "
                         f"\"{ladder.get('label')}\" ladder already on the customer record "
                         "(Diary → Dependants → Progress). Move the level there, not in a second field.")
    # "Staff can see" (docs/plan-people.md slice 3): closed for anything that
    # reads as access information, open otherwise — the owner flips either on
    # the fields page. Set HERE so there is one way in and no definition can be
    # born without an answer.
    definition = {**definition, "display_order": len(existing) * 10,
                  "staff_can_see": crm_visibility.default_staff_can_see(definition)}
    return definition


def seeds_for_tenant(tenant: dict, defined: list[dict]) -> dict:
    """The industry's seed suggestions MINUS anything already defined — the
    wizard's 'accept, edit or skip' chips."""
    seeds = industry_lingo.crm_seeds_for((tenant or {}).get("industry"))
    have = {(d["entity"], d["key"]) for d in defined}
    out = {}
    for entity in ENTITIES:
        remaining = [s for s in seeds.get(entity, ())
                     if (entity, slug(s["label"])) not in have]
        if remaining:
            out[entity] = remaining
    return out


def coerce_value(field_type: str, raw, options: list | None = None):
    """(ok, typed_value) for a raw extracted string. Coercion failure means the
    extraction wasn't trustworthy after all — callers downgrade or drop it."""
    if raw is None:
        return False, None
    s = str(raw).strip()
    if not s:
        return False, None
    if field_type == "number":
        cleaned = re.sub(r"[£$€,\s]", "", s)
        try:
            n = float(cleaned)
        except ValueError:
            return False, None
        return True, int(n) if n.is_integer() else n
    if field_type == "bool":
        low = s.lower()
        if low in _TRUE_WORDS:
            return True, True
        if low in _FALSE_WORDS:
            return True, False
        return False, None
    if field_type == "dropdown":
        for o in options or []:
            if str(o).strip().lower() == s.lower():
                return True, str(o)
        return False, None
    if field_type == "date":
        m = re.search(r"\d{4}-\d{2}-\d{2}", s)
        if not m:
            return False, None
        return True, m.group(0)
    return True, s[:500]   # text


# Built-in extraction targets (Julius 2026-09-03): the customer's phone number
# rode two email signatures and four bookings' details while
# crm_contacts.phone stayed null — the portal then asked a VERIFIED customer
# for a number the business had been given twice. These ride the same
# extraction call as the tenant's own definitions (so tenants with no
# definitions still pay nothing), but autofill lands on the FIRST-CLASS
# column — and only when it's empty, so extraction never overwrites a number
# a human already set. Underscore keys never render and never touch
# custom_data.
BUILTIN_DEFINITIONS = [
    {"entity": "contact", "key": "_phone", "label": "Phone number",
     "field_type": "text", "options": [],
     "extraction_hint": "The SENDER'S own phone or mobile number, usually in "
                        "their signature — never the business's own number "
                        "quoted back to them"},
]


def gate(confidence) -> str:
    """'autofill' | 'suggest' | 'skip' for one extraction's confidence (§10)."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "skip"
    if c >= AUTOFILL_AT:
        return "autofill"
    if c >= SUGGEST_AT:
        return "suggest"
    return "skip"


def plan_application(definitions: list[dict], extracted: list[dict]) -> list[dict]:
    """Pure: pair raw extractions with their definitions and decide each one's
    fate. Returns [{definition, raw_value, confidence, action, value}] — the
    live preview renders this directly; apply_extraction executes it."""
    by_key = {(d["entity"], d["key"]): d for d in definitions}
    out = []
    for item in extracted or []:
        d = by_key.get(((item.get("entity") or "").strip(),
                        (item.get("key") or "").strip()))
        if d is None:
            continue   # the model invented a field — never apply it
        if is_special_category(d):
            # M39: health data is never filled OR suggested from email, at any
            # confidence — the owner types it in by hand.
            out.append({"definition": d, "raw_value": item.get("value"),
                        "confidence": item.get("confidence"), "action": "skip",
                        "value": None})
            continue
        action = gate(item.get("confidence"))
        ok, value = coerce_value(d["field_type"], item.get("value"), d.get("options"))
        if not ok:
            # Un-coercible autofill is downgraded, not trusted: the human can
            # still read the raw text on the suggestion chip. Un-coercible
            # suggestions keep their raw value for the same reason.
            action = "suggest" if action == "autofill" else action
            value = str(item.get("value") or "").strip() or None
            if value is None:
                action = "skip"
        out.append({"definition": d, "raw_value": item.get("value"),
                    "confidence": item.get("confidence"), "action": action,
                    "value": value})
    return out


def _norm_value(v) -> str:
    return " ".join(str(v or "").lower().split())


def loses_information(new, stored) -> bool:
    """A re-read that says LESS than the record already holds is not a
    contradiction. Live 2026-09-04 (services run): the site address on file was
    "Coxley Green premises, just outside Wells, BA5 1RB"; a later email re-read
    "Coxley Green premises" and the card offered to REPLACE the fuller value with
    the shorter one — twice. A new value contained in the stored one adds
    nothing, so it is neither written nor offered."""
    n, s = _norm_value(new), _norm_value(stored)
    return bool(n) and n != s and n in s


def apply_extraction(tenant_id: str, records: dict, definitions: list[dict],
                     extracted: list[dict], email_log_id: str | None) -> dict:
    """Execute a plan for one email: autofills merge into custom_data,
    suggestions queue, skips vanish. `records` maps entity → record id (only
    entities present are touched). Returns a tally for the audit log."""
    tally = {"autofilled": 0, "suggested": 0, "skipped": 0}
    pending_cache: dict = {}

    def _already_offered(record_id: str, key: str, value) -> bool:
        """The same (field, value) is already waiting on this record's card —
        one chip per fact, however many emails re-read it (the duplicate
        yard-address chips, 2026-09-04). Best-effort: a lookup failure never
        swallows a suggestion."""
        try:
            if record_id not in pending_cache:
                pending_cache[record_id] = db.crm_pending_suggestions(tenant_id, [record_id])
            want = _norm_value(value)
            return any(p.get("field_key") == key and _norm_value(p.get("value")) == want
                       for p in pending_cache[record_id])
        except Exception:
            return False

    for step in plan_application(definitions, extracted):
        entity = step["definition"]["entity"]
        record_id = records.get(entity)
        if record_id is None:
            continue
        if entity == "contact" and step["definition"]["key"] == "_phone":
            # Built-in: first-class column, fill-if-empty only, no suggestion
            # chip (there's no custom_data home for it to suggest into).
            if step["action"] == "autofill" and step["value"]:
                db.crm_set_contact_phone_if_empty(record_id, tenant_id,
                                                  str(step["value"])[:40])
                tally["autofilled"] += 1
            else:
                tally["skipped"] += 1
            continue
        if step["action"] == "autofill":
            # Fill an empty slot outright; never REPLACE a value that is already
            # there. Extraction reads one email at a time and cannot know which
            # of the customer's things the email was about — the property owner's
            # second property overwrote the customer's own appliance this way
            # (2026-09-04). A contradiction becomes the same confirm chip a
            # middling-confidence read gets, so the human decides.
            conflicts = db.crm_merge_custom_data(
                entity, record_id, tenant_id,
                {step["definition"]["key"]: step["value"]}, overwrite=False)
            if not conflicts:
                tally["autofilled"] += 1
                if entity == "contact" and isinstance(step["definition"].get("ladder"), dict):
                    try:   # 1b nit: an extracted customer-ladder level is a level change too
                        from app import ladders
                        ladders.contact_field_changed(
                            tenant_id, db.crm_get_contact(record_id, tenant_id) or {"id": record_id},
                            step["definition"]["key"], None, step["value"])
                    except Exception:
                        log.warning("contact ladder change failed", exc_info=True)
                continue
            if loses_information(step["value"], conflicts.get(step["definition"]["key"])):
                tally["skipped"] += 1
                continue
            step = {**step, "action": "suggest"}
        if step["action"] == "suggest":
            if _already_offered(record_id, step["definition"]["key"], step["value"]):
                tally["skipped"] += 1
                continue
            db.crm_insert_field_suggestion(
                tenant_id, entity=entity, record_id=record_id,
                field_key=step["definition"]["key"], value=step["value"],
                confidence=step["confidence"], email_log_id=email_log_id)
            tally["suggested"] += 1
        else:
            tally["skipped"] += 1
    return tally


def accept_suggestion(tenant_id: str, suggestion_id: str, client=None) -> bool:
    """One-click confirm: coerce the stored raw value under today's definition
    and merge it. Returns False when the suggestion or its definition is gone."""
    s = db.crm_get_suggestion(suggestion_id, tenant_id, client=client)
    if s is not None and s.get("status") == "pending" and s.get("entity") == "dependant":
        # A dependant chip (module-requirement-groups.md Phase 2): a field on a
        # known instance, or a whole new one — its own accept path.
        from app import dependants_extract
        ok = dependants_extract.accept(tenant_id, s, client=client)
        db.crm_resolve_suggestion(suggestion_id, tenant_id, "accepted" if ok else "dismissed", client=client)
        return ok
    if s is None or s.get("status") != "pending":
        return False
    defs = db.crm_list_field_definitions(tenant_id, entity=s["entity"], client=client)
    d = next((x for x in defs if x["key"] == s["field_key"]), None)
    if d is None:   # field was deleted since — nothing to confirm into
        db.crm_resolve_suggestion(suggestion_id, tenant_id, "dismissed", client=client)
        return False
    ok, value = coerce_value(d["field_type"], s.get("value"), d.get("options"))
    before = None
    if s["entity"] == "contact" and isinstance(d.get("ladder"), dict):
        before = db.crm_get_contact(s["record_id"], tenant_id, client=client)
    db.crm_merge_custom_data(s["entity"], s["record_id"], tenant_id,
                             {s["field_key"]: value if ok else s.get("value")},
                             client=client)
    db.crm_resolve_suggestion(suggestion_id, tenant_id, "accepted", client=client)
    if before is not None:
        # A customer-ladder level confirmed on the card (1b nit): the same
        # trail + message a dependant's move gets from the job page.
        try:
            from app import ladders
            ladders.contact_field_changed(tenant_id, before, s["field_key"],
                                          (before.get("custom_data") or {}).get(s["field_key"]),
                                          value if ok else s.get("value"), client=client)
        except Exception:
            log.warning("contact ladder change failed", exc_info=True)
    return True


def dismiss_suggestion(tenant_id: str, suggestion_id: str, client=None) -> None:
    db.crm_resolve_suggestion(suggestion_id, tenant_id, "dismissed", client=client)


def display_value(definition: dict, value) -> str:
    """Render one custom_data value for the entity card."""
    if value is None or value == "":
        return "—"
    if definition["field_type"] == "bool":
        return "Yes" if value in (True, "true", "True") else "No"
    return str(value)


# ── Phase 3.5 §18.2: High-requirement field probing ─────────────────────────

MAX_PROBE_ATTEMPTS = {"probe_once": 1, "probe_persistent": 3}


def get_missing_probeable_fields(tenant_id: str, contact_id: str,
                                 company_id: str | None = None) -> list[dict]:
    """Fields with probe_behaviour != 'passive' that are still empty on this
    contact (or their company). Returns definitions sorted by priority for the
    drafter to pick from. Respects max-attempt exhaustion."""
    definitions = db.crm_list_field_definitions(tenant_id)
    if not definitions:
        return []
    contact = db.crm_get_contact(contact_id, tenant_id)
    company = db.crm_get_company(company_id, tenant_id) if company_id else None
    probe_log = db.crm_probe_log_for_contact(tenant_id, contact_id)

    missing = []
    for d in definitions:
        behaviour = d.get("probe_behaviour") or "passive"
        if behaviour == "passive" or is_special_category(d):
            continue   # M39: the agent never asks for health data either
        entity = d["entity"]
        record = contact if entity == "contact" else (company if entity == "company" else None)
        if record is None:
            continue
        # Check if the field already has a value
        custom = record.get("custom_data") or {}
        if custom.get(d["key"]) not in (None, "", 0):
            continue
        # Check attempt exhaustion
        max_attempts = MAX_PROBE_ATTEMPTS.get(behaviour, 1)
        attempts = sum(1 for p in probe_log if p.get("field_definition_id") == d["id"])
        if attempts >= max_attempts:
            continue
        missing.append({**d, "_attempts": attempts})
    # Sort: fewer attempts first (prioritise fresh fields), then by display_order
    missing.sort(key=lambda f: (f["_attempts"], f.get("display_order", 0)))
    return missing


def build_probe_instructions(missing_fields: list[dict]) -> str:
    """The prompt fragment injected into the drafter's CRM context block.
    Instructs the drafter to weave ONE natural question into the reply."""
    if not missing_fields:
        return ""
    lines = [
        "PROBE: The following information is still missing for this contact. "
        "If natural and conversational, weave ONE question into your reply to "
        "collect it. Do NOT make the email feel like a form. Do NOT ask more "
        "than one thing. Pick the first field listed.",
        ""
    ]
    missing_fields = [f for f in missing_fields if not is_special_category(f)]
    if not missing_fields:
        return ""
    for f in missing_fields[:3]:  # show up to 3 so the model can pick naturally
        hint = f.get("extraction_hint") or f.get("label")
        asked = f"(asked {f['_attempts']}x before)" if f.get("_attempts") else "(not yet asked)"
        lines.append(f"- {f['label']}: {hint} {asked}")
    return "\n".join(lines)


def log_probe_attempt(tenant_id: str, contact_id: str,
                      field_definition_id: str, draft_id: str | None = None) -> None:
    """Record that a probe was included in a draft for this field+contact."""
    db.crm_insert_probe_log(tenant_id, contact_id=contact_id,
                            field_definition_id=field_definition_id,
                            draft_id=draft_id)
