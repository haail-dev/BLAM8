"""Scheduling intake — the per-tenant "what a booking needs" schema
(db/schema.sql scheduling_config.intake_fields).

Keeps email + scheduling fully AGNOSTIC: a business defines the fields a booking must
capture (name, address, subject, …) so the office agent collects them before a
booking completes. Missing details may reserve an offered slot while they are gathered;
that reservation is not a completed booking. Pure helpers — no DB; the dashboard persists
via db.upsert_scheduling_config and the pipeline reads db.get_scheduling_config.

A field is {label, required, hint, doc, services?, remember?, crm_key?}. `intake_fields`
is an ordered list of them. `services` (Kris 2026-09-04, the services run) scopes a
field to some of the tenant's services: a site permit is required before
onsite work, not before a design consultation — without scoping the gate blocked the
consultation and the agent keyed "not yet obtained" into a document field to get past it.
An unscoped field applies to every booking, exactly as before.

`remember` (Kris 2026-09-21: "is the booking requirements info saved on the contact for
future bookings?" — it wasn't; every answer lived on its own booking) is the field's
DURABILITY, the same words a dependant's field uses (src/app/requirement_groups.py):
  ask      — about THIS booking; asked every time (the default, and every row until now)
  once     — about the customer; asked on the first booking, then read from their record
  confirm  — about the customer; read from the record and confirmed in one line each time
  expires  — a date on the customer's record that lapses (a licence, a certificate)
A remembered row is promoted to a CUSTOMER FIELD (crm_field_definitions, entity contact —
src/app/intake_memory.py) so there is one store: the drafter's customer context, the portal
profile and the contact page all read it from where they already read. `crm_key` is that
definition's key. Per field, default off, because the wrong-reuse failure (a property owner's
certificate detail riding into a service at another property, live 2026-09-04) is worse than a
re-asked question; the concierge recommends the durability with each row.
"""
import re

MAX_FIELDS = 20
_MAX_LABEL = 60
_MAX_HINT = 120

# Durability of a booking requirement — see the module docstring. "ask" is never
# stored (a row without the key IS an ask-every-time row, byte-identical to before).
REMEMBER = ("ask", "once", "confirm", "expires")
REMEMBER_FACE = {"once": "remembered", "confirm": "on file, confirmed each time",
                 "expires": "on file until it lapses"}

# The base every business is offered, whatever the sector.
_BASE_FIELDS = (
    {"label": "Customer name", "required": True, "hint": "Who the booking is for."},
    {"label": "Contact number", "required": True, "hint": "Best phone or email to confirm on."},
    {"label": "Preferred date / time", "required": True, "hint": "What the customer asked for."},
)

# Sector-tailored suggestions appended to the base — a starting point, fully editable.
# Keys match app/onboarding INDUSTRIES; 'other' is the catch-all (other business types).
INDUSTRY_INTAKE = {
    "logistics": (
        {"label": "Collection address", "required": True, "hint": "Where to collect from."},
        {"label": "Delivery address", "required": True, "hint": "Where it's going."},
        {"label": "Goods / pallet count", "required": True, "hint": "What's being moved."},
    ),
    "trades": (
        {"label": "Job address", "required": True, "hint": "Where the work is."},
        {"label": "Job description", "required": True, "hint": "What needs doing."},
        {"label": "Access notes", "required": False, "hint": "Parking, keys, gate codes."},
    ),
    "construction": (
        {"label": "Site address", "required": True, "hint": "Where the work is."},
        {"label": "Works required", "required": True, "hint": "Scope of the job."},
        {"label": "Site contact", "required": False, "hint": "Who to ask for on site."},
    ),
    "recruitment": (
        {"label": "Role / position", "required": True, "hint": "What they're hiring or applying for."},
        {"label": "CV or details", "required": False, "hint": "Anything they've sent over."},
    ),
    "wholesale": (
        {"label": "Products", "required": True, "hint": "What they want."},
        {"label": "Quantities", "required": True, "hint": "How much of each."},
        {"label": "Delivery address", "required": False, "hint": "Where it ships."},
    ),
    "lettings": (
        {"label": "Property address", "required": True, "hint": "Which property."},
        {"label": "Viewing or maintenance", "required": True, "hint": "Type of appointment."},
    ),
    "hire": (
        {"label": "Item / equipment", "required": True, "hint": "What they want to hire."},
        {"label": "Hire dates", "required": True, "hint": "From and to."},
        {"label": "Delivery or collection", "required": False, "hint": "How they'll get it."},
    ),
    "events": (
        {"label": "Event type", "required": True, "hint": "What the booking is for."},
        {"label": "Guest count", "required": True, "hint": "How many people."},
        {"label": "Venue / location", "required": False, "hint": "Where it's happening."},
    ),
    "other": (
        {"label": "Service required", "required": True, "hint": "What the appointment is for."},
        {"label": "Address", "required": False, "hint": "If you visit them, or they visit you."},
    ),
}

# Sector intake keyed by app/settings_guide PROFILE id — the same 15-sector taxonomy the help
# assistant uses, so an appointment business that signs up via "Something else" (free-text
# sector, no preset industry id) still gets a tailored starting point. Each tuple is the EXTRA
# fields appended to the base three; the Overnight-stays field is the toggle, never seeded here.
PROFILE_INTAKE = {
    "salon": (
        {"label": "Service required", "required": True, "hint": "Cut, colour, blow-dry…"},
        {"label": "Stylist preference", "required": False, "hint": "Anyone in particular, or no preference."},
    ),
    "clinic": (
        {"label": "Reason for visit", "required": True, "hint": "What the appointment is for."},
        {"label": "New or existing patient", "required": False, "hint": "Have they been before?"},
    ),
    "kennels": (
        {"label": "Pet name(s) & breed", "required": True, "hint": "e.g. Bella & Coco, cocker spaniels."},
        {"label": "Number & size of pets", "required": True, "hint": "How many, and small / large."},
        {"label": "Vaccinations up to date?", "required": True, "hint": "Including kennel cough."},
    ),
    "hire": (
        {"label": "Item / equipment", "required": True, "hint": "What they want to hire."},
        {"label": "Quantity", "required": False, "hint": "How many of the item."},
        {"label": "Delivery or collection", "required": False, "hint": "How they'll get it."},
    ),
    "accommodation": (
        {"label": "Number of guests", "required": True, "hint": "How many staying."},
        {"label": "Room type preference", "required": False, "hint": "Single, double, suite…"},
    ),
    "vehicle_hire": (
        {"label": "Vehicle class wanted", "required": True, "hint": "Economy, SUV, van…"},
        {"label": "Driving licence held?", "required": True, "hint": "Full licence, age if relevant."},
    ),
    "storage": (
        {"label": "Unit size needed", "required": True, "hint": "Roughly how much space."},
    ),
    "venue": (
        {"label": "Event type", "required": True, "hint": "What the booking is for."},
        {"label": "Headcount", "required": True, "hint": "How many people."},
    ),
    "restaurant": (
        {"label": "Party size", "required": True, "hint": "How many covers."},
        {"label": "Dietary / notes", "required": False, "hint": "Allergies, high chair, occasion…"},
    ),
    "lessons": (
        {"label": "Level / subject", "required": True, "hint": "What they want to learn."},
        {"label": "Pickup location", "required": False, "hint": "If you travel to the pupil."},
    ),
    "garage": (
        {"label": "Vehicle registration", "required": True, "hint": "Reg plate, e.g. AB12 CDE."},
        {"label": "Make & model", "required": True, "hint": "e.g. Vauxhall Corsa 1.4."},
        {"label": "Service required", "required": True, "hint": "MOT, service, tyres…"},
    ),
    "studio": (
        {"label": "What you'd like", "required": True, "hint": "Design, session type, size…"},
        {"label": "Consent / age confirmed", "required": False, "hint": "Where relevant."},
    ),
    "field_service": (
        {"label": "Job address", "required": True, "hint": "Where the work is."},
        {"label": "Job description", "required": True, "hint": "What needs doing."},
        {"label": "Access notes", "required": False, "hint": "Parking, keys, gate codes."},
    ),
    "logistics": (
        {"label": "Collection address", "required": True, "hint": "Where to collect from."},
        {"label": "Delivery address", "required": True, "hint": "Where it's going."},
        {"label": "Goods / pallet count", "required": True, "hint": "What's being moved."},
    ),
    "appointments": (
        {"label": "Service required", "required": True, "hint": "What the appointment is for."},
        {"label": "Address", "required": False, "hint": "If you visit them, or they visit you."},
    ),
}


# The reserved field that turns a tenant's bookings into STAYS (multi-day visits,
# equipment hire or storage): a booking captures a departure/collection time as well as an
# arrival, so it can span multiple days (app/slotgrid stay_* — the continuous timeline).
# It's controlled from Diary settings, not edited in the field grid, so it never
# appears as a normal row; its presence in intake_fields IS the stay-mode flag (no schema
# change needed). Capacity (spaces/rooms/units) is the existing capacity_per_window.
STAY_FIELD = {"label": "Departure / collection date & time", "required": True,
              "hint": "When the booking ends — pick-up / check-out / drop-back."}


def _norm(label) -> str:
    """Normalise a label for matching/dedupe: lowercased, underscores and
    punctuation folded to spaces, whitespace collapsed. Models naturally write
    JSON keys in snake_case — live 2026-08-31, a hold carrying perfect values
    under company_name/email_provider keys was refused as missing ALL FOUR
    required fields ('Company name' etc.), and the model read the refusal as
    a system snag and apologised to the customer. company_name == Company
    name now, as it always should have."""
    s = re.sub(r"[_\W]+", " ", str(label or "").lower())
    return " ".join(s.split())


def _is_stay_label(label) -> bool:
    return _norm(label) == _norm(STAY_FIELD["label"])


def stay_mode(config) -> bool:
    """Explicit saved mode, with a compatibility fallback for pre-migration settings."""
    cfg = config or {}
    if cfg.get("booking_mode") in ("stay", "appointment"):
        return cfg["booking_mode"] == "stay"
    return is_stay(cfg.get("intake_fields"))


def is_stay(fields) -> bool:
    """True when this tenant books STAYS — the reserved departure/collection field is on,
    so a booking has an arrival AND a departure time and may run across days."""
    return any(_is_stay_label((f or {}).get("label")) for f in (fields or []))


def editable_fields(fields) -> list[dict]:
    """The intake fields shown in the editable grid — everything EXCEPT the toggle-managed
    stay field (which is surfaced as the Overnight-stays toggle instead)."""
    return [f for f in clean_fields(fields) if not _is_stay_label(f["label"])]


def apply_stay(fields, on: bool) -> list[dict]:
    """Add or remove the reserved departure/collection field per the stays toggle, leaving
    the rest of the field list untouched."""
    base = [f for f in clean_fields(fields) if not _is_stay_label(f["label"])]
    if on:
        base.append(dict(STAY_FIELD))
    return clean_fields(base)


def clean_fields(fields) -> list[dict]:
    """Normalise a raw field list (from the form OR the db) into clean {label, required,
    hint, doc} dicts — blanks and duplicate labels dropped, trimmed, capped at
    MAX_FIELDS. `doc` (2026-09-02, Julius: "allow a requirement to require a
    document") marks a detail that is a DOCUMENT the customer supplies — a
    compliance certificate, a licence — rather than an answer they type; the
    agent asks for it as an attachment. The single source of validity, used on
    save and on read."""
    seen: set = set()
    out: list[dict] = []
    for f in fields or []:
        label = str((f or {}).get("label") or "").strip()[:_MAX_LABEL]
        key = _norm(label)
        if not key or key in seen:
            continue
        seen.add(key)
        row = {"label": label, "required": bool((f or {}).get("required")),
               "hint": str((f or {}).get("hint") or "").strip()[:_MAX_HINT],
               "doc": bool((f or {}).get("doc"))}
        # Service scope rides only when set, so an unscoped field's row is
        # byte-identical to before (every existing tenant, every existing test).
        scope = _clean_services((f or {}).get("services"))
        if scope:
            row["services"] = scope
        # Durability rides only when remembered. A document is attached per
        # booking, never a fact kept on the record; the stay field is a mode
        # flag. Both stay ask-every-time whatever the form said.
        mem = clean_remember((f or {}).get("remember"))
        if mem != "ask" and not row["doc"] and not _is_stay_label(label):
            row["remember"] = mem
            ck = re.sub(r"[^a-z0-9_]", "", str((f or {}).get("crm_key") or "").strip().lower())[:60]
            if ck:
                row["crm_key"] = ck
        out.append(row)
        if len(out) >= MAX_FIELDS:
            break
    return out


_MAX_SCOPE = 30


def clean_remember(value) -> str:
    """One durability word from the form/db/model: 'once' | 'confirm' | 'expires',
    else 'ask'. Tolerates the model's phrasing ('remember: once', 'confirmed
    each time', 'expiry')."""
    s = _norm(value)
    if not s:
        return "ask"
    s = re.sub(r"^remember(ed)?\s*", "", s)
    for word in ("once", "confirm", "expir"):
        if word in s:
            return "expires" if word == "expir" else word
    return "ask"


def remember_of(field: dict) -> str:
    """The field's durability — 'ask' unless it carries a remembered one."""
    return clean_remember((field or {}).get("remember")) if (field or {}).get("remember") else "ask"


def remembered(fields) -> list[dict]:
    """The rows kept on the customer's record (durability once/confirm/expires)."""
    return [f for f in clean_fields(fields) if f.get("remember")]


def _clean_services(value) -> list[str]:
    """A field's service scope from the db (a list) or the form (one string, services
    separated by '|' or ';', or by ',' when neither is present): trimmed, deduped
    case-insensitively, capped."""
    if value is None:
        return []
    if isinstance(value, str):
        # '|' is the form's (and the card's) separator; a comma only separates
        # when nothing else does, so "Drain, clean and refill" stays one name.
        parts = re.split(r"[|;]", value) if re.search(r"[|;]", value) else value.split(",")
    else:
        parts = list(value) if isinstance(value, (list, tuple)) else []
    out, seen = [], set()
    for p in parts:
        name = str(p or "").strip()[:60]
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        out.append(name)
        if len(out) >= _MAX_SCOPE:
            break
    return out


# On a STAYS diary that also takes appointments, the stay itself is one of the
# things a detail can be scoped to (2026-09-23, the Ellen test: the meet & greet
# was refused for a missing "Collection date and time"). A stay hold is checked
# as "Stays", an appointment hold as its own service, so "Drop-off" ticked for
# Stays never gates a meet & greet. The same scope list the services diary uses.
STAYS_SCOPE = "Stays"
APPOINTMENT_TOOL_NAMES = ("check_appointment_availability", "hold_appointment")


def applies(field: dict, service) -> bool:
    """Does this field bind the booking at hand? Unscoped fields always do; a scoped
    field binds only a booking FOR one of its services. With no service known
    (a windows/stay diary, or a hold before the service is named) a scoped field
    stands down — it can't be for THIS booking until we know what the booking is."""
    scope = (field or {}).get("services") or []
    if not scope:
        return True
    if not service:
        return False
    want = _norm(service)
    return any(_norm(s) == want for s in scope)


def scope_label(field: dict) -> str:
    """'only for Onsite work' / 'only for Site survey, Onsite work' / ''."""
    scope = (field or {}).get("services") or []
    return ("only for " + ", ".join(scope)) if scope else ""


_DOC_FILE = re.compile(r"\S+\.(pdf|jpe?g|png|heic|heif|gif|webp|tiff?|bmp|docx?)\b", re.IGNORECASE)


def looks_like_document(value) -> bool:
    """A DOCUMENT field is satisfied by a file, not a sentence: the attachment's
    filename (the '[Attachments: …]' marker the mail readers append), or the
    Graph reader's names-unavailable fallback. 'Not yet obtained — we'll apply
    after the consultation' was keyed and accepted live (2026-09-03, twice);
    it is a promise, not a permit."""
    s = str(value or "")
    return bool(_DOC_FILE.search(s)) or "files came" in s.lower() or "[attachments" in s.lower()


def suggest_for(industry=None, brief=None) -> list[dict]:
    """Generic first-run suggestions only; descriptions never choose requirements.

    Arguments remain accepted for existing callers. These editable suggestions
    are not saved until the owner submits the checklist.
    """
    return clean_fields(list(_BASE_FIELDS))


def required_labels(fields, service=None) -> list[str]:
    # The reserved stay field is a MODE flag, not a captured detail — the agent passes the
    # departure time as a tool argument, not in `details`, so it never gates a hold.
    # `service` = the booking's service, so a field scoped to other services stands down.
    return [f["label"] for f in clean_fields(fields)
            if f["required"] and not _is_stay_label(f["label"]) and applies(f, service)]


_EMAIL_LABEL = re.compile(r"e-?mail", re.IGNORECASE)


def satisfy_from_sender(fields, details, sender: str | None) -> dict:
    """The sender's own address answers any email-shaped requirement — asking
    someone to send you their email address BY EMAIL is the silliest question
    the gather flow can ask, and it asked it live (2026-09-03: 'we will also
    need your email address to complete the booking', to the address it was
    replying to). Fills ONLY absent/blank keys, so a customer who explicitly
    gave a different address keeps it. Returns a new details dict."""
    out = dict(details or {})
    if not sender or "@" not in sender:
        return out
    for f in fields or []:
        label = (f or {}).get("label") or ""
        # An email-shaped value with no "@" is a placeholder, not an address —
        # live 2026-09-03 the agent keyed the literal words "sender's email
        # address" and the gate kept them. Only a real address survives.
        if _EMAIL_LABEL.search(label) and "@" not in str(out.get(label) or ""):
            out[label] = sender
    return out


def sender_filled_labels(fields, details, sender: str | None) -> list[str]:
    """The labels satisfy_from_sender WOULD fill for this call — so the hold
    result can tell the agent "this was taken from the sender, it's on file,
    never ask for it". Live 2026-09-03 (Stone Mason acceptance): the gate
    filled the email silently, the agent never saw that it had, and the reply
    still said "if you could include your email address when you next write"."""
    before = dict(details or {})
    after = satisfy_from_sender(fields, before, sender)
    return [k for k, v in after.items() if v != before.get(k)]


_PLACEHOLDER_WORDS = {
    "unknown", "not known", "tbc", "tba", "tbd", "to be confirmed", "to be advised",
    "n a", "na", "none", "nil", "null", "not stated", "not given", "not provided",
    "not specified", "not yet", "not yet known", "pending", "unspecified", "missing",
    "unsure", "no idea", "dont know", "don t know", "ask customer", "ask the customer",
    "awaiting", "later",
}


def is_placeholder(value) -> bool:
    """A value that stands in for an answer is not an answer. Live 2026-09-04
    (services run): the gate refused offer_slots for two missing required fields,
    the model called again with "<UNKNOWN>" in both, and three offers went out
    with placeholders stored as the customer's details. Angle-bracketed tokens
    (<unknown>, <not given>), the bare words above, and '?' / '-' are all
    refused; anything with a digit or a real word survives ("07700 900123",
    "No — neither subject has a reported condition")."""
    s = str(value or "").strip()
    if not s:
        return True
    if re.fullmatch(r"<[^<>]{0,60}>", s) or re.fullmatch(r"[\[\(][^\]\)]{0,60}[\]\)]", s):
        return True
    if re.fullmatch(r"[\W_]+", s):          # "?", "-", "…", "--"
        return True
    n = _norm(s)
    if n in _PLACEHOLDER_WORDS:
        return True
    # "Unknown — drain and refill required…" (live 2026-09-04, a confirm field):
    # a placeholder word followed by a separator is still a placeholder, the
    # rest is the model's reasoning, not the customer's answer.
    head = re.split(r"\s*[—–:;,(/-]\s*", s, maxsplit=1)[0]
    return bool(head) and head != s and _norm(head) in _PLACEHOLDER_WORDS


def drop_out_of_scope(fields, details, service) -> dict:
    """Strip details that belong to OTHER services from a booking's bag. A field
    scoped to some services is asked only for them (applies) — but a value can
    still arrive by reuse: live 2026-09-04 the portal booked a full service
    carrying "Reviewer name and business", a remedial-only field, straight off the
    customer's earlier remedial job. With no service known nothing is dropped
    (the scope can't be judged yet); unscoped fields always stay."""
    if not service or not fields:
        return dict(details or {})
    by_norm = {_norm(f["label"]): f for f in clean_fields(fields)}
    out = {}
    for k, v in (details or {}).items():
        f = by_norm.get(_norm(k))
        if f and f.get("services") and not applies(f, service):
            continue
        out[k] = v
    return out


# A whole address (Kris 2026-09-26: "taking address should mean WHOLE
# address" — BK-1003 was booked at "14 Sydney Buildings, Bath"). A required
# address field counts only once a UK postcode is in the booking's details —
# in that field, or in a separate postcode field the business asks for.
UK_POSTCODE = re.compile(r"\b(?:GIR ?0AA|[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})\b", re.IGNORECASE)
_ADDRESS_LABEL = re.compile(r"\baddress\b", re.IGNORECASE)
_NOT_A_PLACE = re.compile(r"\b(e-?mail|web|ip)\s+address\b", re.IGNORECASE)


def is_address_field(label: str | None) -> bool:
    """A field that asks WHERE — "Job address", "Address", "Collection
    address" — not an email or web address."""
    lab = label or ""
    return bool(_ADDRESS_LABEL.search(lab)) and not _NOT_A_PLACE.search(lab)


def has_postcode(value) -> bool:
    return bool(UK_POSTCODE.search(str(value or "")))


def needs_postcode(fields, details, service=None) -> list[str]:
    """Required address labels whose value is there but no postcode is given
    anywhere in the details — each is still missing (missing_required)."""
    have = {_norm(k): v for k, v in (details or {}).items()}
    if any(has_postcode(v) for v in have.values()):
        return []
    return [label for label in required_labels(fields, service)
            if is_address_field(label) and not is_placeholder(have.get(_norm(label)) or "")
            and have.get(_norm(label)) is not None]


def missing_required(fields, details, service=None) -> list[str]:
    """Required field labels NOT present (non-empty, not a placeholder) in a hold's
    `details` bag — for THIS booking's `service` (fields scoped to other services don't
    bind). Matching is on the normalised label (case/space-insensitive), so the agent's
    keys only need to match loosely. A DOCUMENT field counts only when its value names
    a file (looks_like_document); a placeholder ("<UNKNOWN>", "TBC", "?") counts as
    missing (is_placeholder). This is the HARD gate the office hold executor enforces."""
    have = {_norm(k): v for k, v in (details or {}).items()}
    docs = {_norm(f["label"]) for f in clean_fields(fields) if f.get("doc")}
    out = []
    for label in required_labels(fields, service):
        v = have.get(_norm(label))
        if v is None or is_placeholder(v):
            out.append(label)
        elif _norm(label) in docs and not looks_like_document(v):
            out.append(label)
    out += [lab for lab in needs_postcode(fields, details, service) if lab not in out]
    return out


def detail_keys_line(fields) -> str:
    """One sentence naming the EXACT `details` keys the gate accepts, for the
    booking tools' argument description. The gate matches labels loosely
    (case, snake_case) but the model still invented keys — "notes", "phone",
    "inscription_wording" — on 26 of 31 refusals in a week (2026-08-28 →
    09-04), and every refusal is a second full model turn. Empty when the
    tenant defined no fields."""
    fields = [f for f in clean_fields(fields) if not _is_stay_label(f["label"])]
    if not fields:
        return ""
    keys = ", ".join(f"'{f['label']}'" for f in fields)
    return (f" Use EXACTLY these keys in `details`, copied verbatim (spaces, ampersands "
            f"and punctuation included), never your own names: {keys}. A key not in "
            "this list is ignored and will get the hold refused.")


def with_detail_keys(tools, fields, appointment_names=None) -> list[dict]:
    """Deep-copy a tool list and append the tenant's exact detail keys to every
    `details` argument description. The module-level tool constants stay
    byte-stable (they are shared); the per-tenant copy is what the model sees.

    `appointment_names` (a stays diary's appointments): each tool is told only the
    keys that apply to it — the appointment tools the details scoped to an
    appointment (or unscoped), the stay tools those scoped to Stays (or unscoped)."""
    import copy
    line = detail_keys_line(fields)
    if not line:
        return list(tools or [])
    if appointment_names:
        cf = clean_fields(fields)
        appt_line = detail_keys_line([f for f in cf if any(applies(f, n) for n in appointment_names)])
        stay_line = detail_keys_line([f for f in cf if applies(f, STAYS_SCOPE)])
    out = []
    for t in tools or []:
        props = ((t.get("input_schema") or {}).get("properties") or {})
        if appointment_names:
            line = appt_line if t.get("name") in APPOINTMENT_TOOL_NAMES else stay_line
        if "details" in props and line:
            t = copy.deepcopy(t)
            d = t["input_schema"]["properties"]["details"]
            d["description"] = (d.get("description") or "").rstrip() + line
        out.append(t)
    return out


# What the drafter is told about a remembered row (per field) and how to treat
# what the gate filled from the record (once per prompt). The gate is the
# guarantee — it prefills from the record and SAYS so in the hold result
# (`prefilled_from_record`) — these lines stop the reply asking anyway, the
# way the silent sender-address fill did (live 2026-09-03).
_REMEMBER_FLAG = {
    "once": (" — REMEMBERED on the customer's record: when the customer context shows "
             "it, copy it into `details` and never ask; ask only a first-time customer, "
             "and only once"),
    "confirm": ((
        " — ON FILE from their last booking: when the customer context shows it, copy it into "
        "`details` and confirm it in ONE line of your reply ('is that still correct?') instead of "
        "asking afresh; a first-time customer is asked as normal"
    )),
    "expires": (" — a DATE kept on the customer's record until it lapses: use the one on "
                "file while it is in date; ask for the new one only when the record says "
                "it has lapsed, or there is none"),
}
_REMEMBER_RULE = (
    "A detail marked REMEMBERED / ON FILE is kept on the customer's record: the hold "
    "fills it from the record itself and lists what it used under "
    "`prefilled_from_record` — those are on file, so never ask for them and never say "
    "you still need them. A remembered detail the customer DOES give in this email "
    "is the new truth: pass it in `details` and it updates their record.")


def render_for_prompt(fields) -> str:
    """The intake block injected into the office <scheduling> prompt — the required and
    optional fields, so the agent knows exactly what to gather before booking. Empty
    string when the tenant defined no fields (nothing extra to collect)."""
    fields = clean_fields(fields)
    stay = is_stay(fields)
    # The reserved stay field drives the toggle/flag, not a captured detail — exclude it from
    # the listed fields (the arrival/departure times are tool arguments, not `details`).
    fields = [f for f in fields if not _is_stay_label(f["label"])]
    if not fields and not stay:
        return ""
    req = [f for f in fields if f["required"]]
    opt = [f for f in fields if not f["required"]]
    lines = ["Booking intake — this business REQUIRES specific details on every booking. "
             "Collect them from the customer and pass each one in the hold_booking "
             "`details` object, keyed by the field name shown:"]
    if stay:
        lines.append((
            "This business takes STAYS (arrival-to-departure bookings): a booking has an ARRIVAL/drop-off "
            "time AND a DEPARTURE/collection time (passed as the hold_booking arrival/departure arguments, "
            "NOT in details), and may span several days. Always capture BOTH times before booking."
        ))
    def _flag(f):
        if _EMAIL_LABEL.search(f["label"]):
            # Filled from the sender's own address at the gate — the one detail
            # that is NEVER worth a question (live 2026-09-03, twice).
            return (" — filled in FOR YOU from the sender's own address: leave it out "
                    "of `details` and NEVER ask for it")
        out = " — a DOCUMENT" if f.get("doc") else ""
        if f.get("services"):
            out += f" — {scope_label(f).upper()}"
        out += _REMEMBER_FLAG.get(f.get("remember") or "", "")
        return out
    for f in req:
        lines.append(f"- {f['label']} (REQUIRED{_flag(f)})"
                     + (f" — {f['hint']}" if f["hint"] else ""))
    for f in opt:
        lines.append(f"- {f['label']} (optional{_flag(f)})"
                     + (f" — {f['hint']}" if f["hint"] else ""))
    if any(f.get("services") for f in fields):
        lines.append((
            "A detail marked ONLY FOR some services applies when the booking is for one of those services "
            "and NOT otherwise: for any other service leave it out of `details` and never ask for it (a "
            "detail required for Service A need not apply to Service B)."
        ))
    if any(f.get("remember") for f in fields):
        lines.append(_REMEMBER_RULE)
    if any(f.get("doc") for f in fields):
        lines.append("A field marked 'a DOCUMENT' is something the customer SUPPLIES "
                     "(a certificate, a licence, a form) rather than answers: ask them "
                     "to attach it to their reply — a clear photo or scan is fine. The "
                     "email you're answering shows '[Attachments: …]' when files came "
                     "with it: an attachment that plainly matches the document counts "
                     "as collected (put its filename in `details` under the field "
                     "name); never claim to have checked the document's contents — a "
                     "human reviews it. ONLY a file counts: a required document that "
                     "hasn't arrived means the booking waits — never key 'not yet', "
                     "'to follow' or a promise in its place. An OPTIONAL document that "
                     "applies to the service being booked still gets one line in the SAME "
                     "reply as your required asks — 'attach it if you have it' — so the "
                     "customer's next message can close the whole checklist; never make "
                     "the booking wait on an optional one, and never ask twice.")
    lines.append("A detail the customer has ALREADY given — in an earlier message of "
                 "this thread, on the customer portal, or in the customer context — "
                 "counts as collected: copy it into `details` yourself and NEVER ask "
                 "for it again (re-asking something they already answered reads as "
                 "not listening, and has lost real bookings). The EMAIL ITSELF "
                 "carries details too: an email-address requirement is satisfied by "
                 "the SENDER'S OWN ADDRESS (you are literally replying to it — "
                 "asking someone to send you their email address by email is "
                 "nonsense), and a name signed off in the message counts as their "
                 "name. Ask only for details "
                 "genuinely never provided. Do NOT call hold_booking until EVERY "
                 "required detail above is in `details`; if one is truly missing, ask "
                 "for just that one and do not book yet.")
    return "\n".join(lines)


def render_for_gather(fields) -> str:
    """The intake block for the EMAIL-ONLY 'gather but don't book' prompt section: the same
    required/optional fields as render_for_prompt, but with NO hold_booking tool references
    (this tier has no diary). Empty string when the tenant defined no fields."""
    fields = clean_fields(fields)
    stay = is_stay(fields)
    fields = [f for f in fields if not _is_stay_label(f["label"])]
    if not fields and not stay:
        return ""
    req = [f for f in fields if f["required"]]
    opt = [f for f in fields if not f["required"]]
    lines = ["The details this business needs to take a booking:"]
    if stay:
        lines.append((
            "This business takes STAYS (arrival-to-departure bookings): capture BOTH an arrival/drop-off "
            "date and a departure/collection date — a booking can span several days."
        ))
    def _tag(f):
        t = " — a DOCUMENT" if f.get("doc") else ""
        if f.get("services"):
            t += f" — {scope_label(f).upper()}"
        if f.get("remember"):
            t += (" — kept on the customer's record: a repeat customer is not asked "
                  "again" + (", just asked to confirm it in one line" if f["remember"] == "confirm" else ""))
        return t
    for f in req:
        lines.append(f"- {f['label']} (REQUIRED{_tag(f)})"
                     + (f" — {f['hint']}" if f["hint"] else ""))
    for f in opt:
        lines.append(f"- {f['label']} (optional{_tag(f)})"
                     + (f" — {f['hint']}" if f["hint"] else ""))
    if any(f.get("services") for f in fields):
        lines.append("A detail marked ONLY FOR some services applies when the booking is "
                     "for one of those services and not otherwise — never ask for it "
                     "on any other booking.")
    if any(f.get("doc") for f in fields):
        lines.append("A field marked 'a DOCUMENT' is something the customer supplies — "
                     "ask them to attach it to their reply (a clear photo or scan is "
                     "fine). '[Attachments: …]' on their email shows what came with "
                     "it; never claim to have checked a document's contents.")
    lines.append("A detail already given anywhere earlier in the thread or in the "
                 "customer context counts as collected — never ask for it again; "
                 "ask only for what is genuinely missing.")
    return "\n".join(lines)


def parse_form(labels, requireds, hints, docs=None, services=None, remembers=None,
               crm_keys=None) -> list[dict]:
    """Reassemble the intake field list from the setup form's positionally-aligned
    arrays: label[], required[] and doc[] (selects that always submit '1'/'0', so
    alignment never breaks the way unchecked checkboxes would), hint[], and
    services[] — one hidden input per row holding the scoped service names joined
    with '|' (blank = every booking), kept in step with the row's tick-boxes by JS.
    remember[] (a select, always submitted) is the row's durability; crm_key[] a
    hidden input carrying the customer field the row was promoted to on an
    earlier save, so a re-save adopts it instead of minting a second."""
    labels = labels or []
    requireds = requireds or []
    hints = hints or []
    docs = docs or []
    services = services or []
    remembers = remembers or []
    crm_keys = crm_keys or []
    rows = []
    for i, label in enumerate(labels):
        rows.append({
            "label": label,
            "required": str(requireds[i]).strip() == "1" if i < len(requireds) else False,
            "hint": hints[i] if i < len(hints) else "",
            "doc": str(docs[i]).strip() == "1" if i < len(docs) else False,
            "services": services[i] if i < len(services) else "",
            "remember": remembers[i] if i < len(remembers) else "",
            "crm_key": crm_keys[i] if i < len(crm_keys) else "",
        })
    return clean_fields(rows)
