"""Remembered booking requirements — the bridge between a booking's `details`
and the customer's record (Kris 2026-09-21).

The question that started it: "is the Booking requirements info saved on the
Contacts CRM for future bookings?" It was not. Every answer the gather flow
collected lived on its own booking row, so a repeat customer was asked their
care instructions, their licence number, their parking spot on every visit,
while the CRM sat empty — the phone number rode four bookings' details before
the built-in extraction lifted it (Julius 2026-09-03), and the inscription
Kris sent by email lived on BK-1005 alone (2026-09-04).

The mechanism is small and reuses what already exists:

  * a row on the Booking requirements page carries a DURABILITY (src/app/intake.py
    `remember`: ask / once / confirm / expires — a dependant field's words);
  * a remembered row is PROMOTED to a customer field (crm_field_definitions,
    entity contact) on save — `sync_definitions` — so there is ONE store: the
    drafter's <customer_context>, the portal profile and the contact page all
    read the value where they already read customer fields;
  * the hold gate (app/scheduling._intake_gated) reads the record BEFORE it
    judges what is missing — `prefill_from_record` — and says in the hold
    result what it used, so the reply never asks for a fact on file;
  * a value the customer gives in this email is written BACK to the record —
    `values_to_remember` + `remember` — at the gate when the record exists, or
    from the pipeline after CRM ingest when this is their first email
    (`flush_pending`).

Per field, default off. The wrong-reuse failure (a property owner's certificate detail
riding into a service at another property, live 2026-09-04) is worse than a
re-asked question, and the label is tenant text so only the owner knows which
kind of fact a row is; the concierge recommends the durability with each row.
"""
from __future__ import annotations

import logging
from datetime import date

from app import crm_fields, db, intake

log = logging.getLogger(__name__)


# ── keys and definitions ───────────────────────────────────────────────────────
def key_of(field: dict) -> str:
    """The custom_data key a remembered row reads and writes."""
    return (field or {}).get("crm_key") or crm_fields.slug((field or {}).get("label") or "")


def labels_for(fields, keys) -> list[str]:
    """The row labels behind a set of record keys, in row order."""
    keys = set(keys or ())
    return [f["label"] for f in intake.remembered(fields) if key_of(f) in keys]


def definition_for(field: dict) -> dict:
    """The customer field a remembered row is promoted to: text, or a date that
    expires (recalled RECALL_DEFAULT days ahead; the owner tunes the recall and
    a validity on the Customer fields page). The hint doubles as the
    extraction hint, so the same fact is read from emails too."""
    mem = intake.remember_of(field)
    ftype = "date" if mem == "expires" else "text"
    hint = (field.get("hint") or "").strip() or f"Given when booking: {field['label']}"
    return crm_fields.validate_definition(
        "contact", field["label"], ftype, [], hint,
        durability="expires" if mem == "expires" else "once")


def sync_definitions(tenant_id: str, fields, client=None) -> tuple[list[dict], list[str]]:
    """On an intake save: every remembered row has a customer field definition
    — an existing one adopted by key, else created. A row that cannot be
    promoted (the per-entity cap, a ladder collision) drops back to
    ask-every-time and the reason is returned for the page to show, so the
    owner is never left believing a fact is kept when it is not. A read blip
    keeps the rows as they are (the next save retries)."""
    fields = intake.clean_fields(fields)
    if not any(f.get("remember") for f in fields):
        return fields, []
    try:
        existing = db.crm_list_field_definitions(tenant_id, entity="contact", client=client)
    except Exception:
        log.warning("intake memory: reading customer fields failed", exc_info=True)
        return fields, []
    by_key = {d.get("key"): d for d in existing}
    out: list[dict] = []
    problems: list[str] = []
    for f in fields:
        if not f.get("remember"):
            out.append(f)
            continue
        if f.get("crm_key") and f["crm_key"] in by_key:
            out.append(f)
            continue
        want = crm_fields.slug(f["label"])
        if want in by_key:
            out.append({**f, "crm_key": want})
            continue
        try:
            d = definition_for(f)
            crm_fields.add_field(tenant_id, d, client=client)
            by_key[d["key"]] = d
            out.append({**f, "crm_key": d["key"]})
        except crm_fields.FieldError as e:
            problems.append(f"{f['label']}: {e}")
            out.append({k: v for k, v in f.items() if k not in ("remember", "crm_key")})
        except Exception:
            log.warning("intake memory: promoting %r failed", f.get("label"), exc_info=True)
            out.append(f)
    return out, problems


# ── reading the record ─────────────────────────────────────────────────────────
def _real(value) -> bool:
    return value is not None and str(value).strip() != "" and not intake.is_placeholder(value)


def prefill_from_record(fields, details, record, definitions=None, on=None):
    """(details, filled, lapsed): the booking's bag with every remembered row
    the record can answer filled in — ONLY where this call carries no real
    value for it (what the customer says now wins over what they said last
    time). `filled` = {label: {"value", "remember"}} for the hold result;
    `lapsed` = {label: note} for an expiring date that no longer covers `on`
    (the booking date) — left unfilled so the gate asks for the new one, and
    the refusal can say why. Pure: `record` is the contact row, `definitions`
    the tenant's contact field definitions (for a certificate's validity)."""
    out = dict(details or {})
    filled: dict = {}
    lapsed: dict = {}
    data = (record or {}).get("custom_data") or {}
    if not data:
        return out, filled, lapsed
    have = {intake._norm(k): v for k, v in out.items()}
    defs = {d.get("key"): d for d in (definitions or []) if isinstance(d, dict)}
    try:
        on_date = date.fromisoformat(str(on)[:10]) if on else None
    except ValueError:
        on_date = None
    for f in intake.remembered(fields):
        key = key_of(f)
        value = data.get(key)
        if not _real(value):
            continue
        if _real(have.get(intake._norm(f["label"]))):
            continue
        mem = f["remember"]
        d = defs.get(key) or {}
        if mem == "expires" or d.get("durability") == "expires":
            due = crm_fields.expiry_date(
                {**d, "durability": "expires"} if d else
                {"durability": "expires", "field_type": "date", "valid_months": 0}, value)
            if due is None:
                continue
            if on_date and due < on_date:
                lapsed[f["label"]] = crm_fields.expiry_note(
                    {**d, "durability": "expires"} if d else
                    {"durability": "expires", "valid_months": 0}, value, on_date)
                continue
        shown = crm_fields.display_value(d, value) if d.get("field_type") else str(value)
        out[f["label"]] = shown
        filled[f["label"]] = {"value": shown, "remember": mem}
    return out, filled, lapsed


def record_note(filled: dict) -> str:
    """The hold result's line about what came from the record — the reply must
    not ask for it, and a confirm row is checked in one line, not re-asked."""
    if not filled:
        return ""
    note = ("Filled from the customer's record (already on file — do NOT ask for "
            "it, and do not say you still need it): "
            + "; ".join(f"{label}: {m['value']}" for label, m in filled.items()) + ".")
    confirms = [label for label, m in filled.items() if m.get("remember") == "confirm"]
    if confirms:
        note += (" Confirm " + ", ".join(confirms) + " in ONE line of your reply "
                 "('still …?') so the customer can correct it — never ask for it afresh.")
    return note


def lapsed_note(lapsed: dict) -> str:
    """Appended to a missing-info refusal: the record HAD the date, but it has
    lapsed for this booking — the agent asks for the new one, and says why."""
    if not lapsed:
        return ""
    return (" On the customer's record, " + "; ".join(f"{label} {note}" for label, note in lapsed.items())
            + " — ask for the new date (and say the one on file has lapsed).")


# ── writing it back ────────────────────────────────────────────────────────────
def values_to_remember(fields, details, service=None, definitions=None) -> dict:
    """{record key: typed value} — what the CUSTOMER gave in this call for the
    remembered rows that bind this booking (`service` scope), shaped for the
    record: a date row keeps ISO dates only, a typed customer field is coerced
    the way extraction coerces. A placeholder, or a date that will not parse,
    stays on the booking and never reaches the record."""
    from app.requirement_groups import parse_date
    have = {intake._norm(k): v for k, v in (details or {}).items()}
    defs = {d.get("key"): d for d in (definitions or []) if isinstance(d, dict)}
    out: dict = {}
    for f in intake.remembered(fields):
        if not intake.applies(f, service):
            continue
        v = have.get(intake._norm(f["label"]))
        if not _real(v):
            continue
        key = key_of(f)
        d = defs.get(key) or {}
        ftype = d.get("field_type") or ("date" if f["remember"] == "expires" else "text")
        if ftype == "date":
            dt = parse_date(v)
            if dt is None:
                continue
            out[key] = dt.isoformat()
            continue
        ok, typed = crm_fields.coerce_value(ftype, v, d.get("options") or [])
        if ok:
            out[key] = typed
    return out


def remember(tenant_id: str, contact_id: str | None, values: dict, client=None) -> dict:
    """Merge the values onto the contact's record. The customer stated them for
    THIS booking on a row the owner marked as about the customer, so they are
    the new truth — overwrite, unlike extraction's fill-if-empty. Best-effort:
    the hold stands whether or not the record write lands. Returns what was
    written ({} when nothing was)."""
    if not values or not contact_id:
        return {}
    try:
        db.crm_merge_custom_data("contact", contact_id, tenant_id, dict(values),
                                 client=client, overwrite=True)
        return dict(values)
    except Exception:
        log.warning("intake memory: record write failed for %s", contact_id, exc_info=True)
        return {}


def remember_by_email(tenant_id: str, email: str | None, values: dict, client=None) -> dict:
    """The manual-add path: the diary form names the customer by email."""
    if not values or not email:
        return {}
    try:
        c = db.crm_contact_by_email(tenant_id, email, client=client)
    except Exception:
        c = None
    return remember(tenant_id, (c or {}).get("id"), values, client=client)


def flush_pending(tenant_id: str, contact_id: str | None, state: dict | None, *,
                  fields=None, email_log_id: str | None = None, contact: str | None = None,
                  subject: str | None = None, mailbox_id: str | None = None) -> list[str]:
    """After CRM ingest: write what the hold gate could not (a first-time
    customer had no record at hold time — the record is born in ingest, after
    the draft) and log ONE Agent Log line for everything this email left on
    the record, whether the gate wrote it or this did. Returns the labels."""
    state = state if isinstance(state, dict) else {}
    pending = state.pop("remember_pending", None) or {}
    written = remember(tenant_id, contact_id, pending) if pending else {}
    labels = list(state.get("remembered_labels") or [])
    if written:
        labels += [l for l in labels_for(fields, written) if l not in labels] or \
                  [k for k in written if k not in labels]
    if labels:
        try:
            db.log_agent_event(
                tenant_id, "crm_autofill",
                f"Kept {len(labels)} booking detail{'' if len(labels) == 1 else 's'} on "
                f"the customer's record: " + ", ".join(labels),
                contact=contact, subject=subject, email_log_id=email_log_id,
                mailbox_id=mailbox_id, detail={"remembered": labels})
        except Exception:
            log.warning("intake memory: agent event failed", exc_info=True)
    return labels
