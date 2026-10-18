"""Dependants, read not typed (docs/modules/module-requirement-groups.md Phase 2).

One small call per inbound email, ONLY for tenants with a dependant group
and an email that produced a CRM contact (everyone else pays nothing): the
tenant's groups (label, fields, hints) and the customer's known instances go
in as the schema to fill; the email goes in as the source; values come back
per instance with a confidence each. The crm_fields gate decides:

- ≥ 0.75 on a KNOWN instance → written by durability (a `once` field already
  on record is never overwritten by a read; `confirm`/`expires` take the
  newest value);
- a middling read, a contradiction of a `once` field, or a NEW instance the
  customer mentioned ("we've got a new puppy, Biscuit") → a confirm chip on
  the contact card (crm_field_suggestions, entity 'dependant'); accepting
  writes it, accepting a new one creates the row;
- below the floor → dropped.

Usage is metered like drafting (operation 'dependant_extract'). The
vertical SaaS makes the owner type Rex's vaccine date into a form; this
reads the vaccination email."""
from __future__ import annotations

import json
import re
import logging

from app import agent, crm_fields, db, llm, requirement_groups as rg, usage
from app.config import settings

log = logging.getLogger(__name__)

NEW_PREFIX = "__new__:"      # field_key of a new-instance suggestion: __new__:<group_id>

SCHEMA = {
    "type": "object",
    "properties": {
        "instances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group": {"type": "string"},
                    "name": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                                "confidence": {"type": "number"},
                            },
                            "required": ["key", "value", "confidence"],
                            "additionalProperties": False,
                        },
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["group", "name", "fields", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["instances"],
    "additionalProperties": False,
}

MAX_BODY_CHARS = 4000

SYSTEM = (
    "You read facts about a customer's DEPENDANTS from one business email — the "
    "things the business's bookings are about (each kind is defined by the "
    "business: its name and the fields to know about each one). You are given the "
    "kinds, the dependants already on this customer's record, and the email. "
    "Return ONLY what the email actually supports — never guess, never fill from "
    "general knowledge. Use the record's exact name for a dependant already known; "
    "a dependant the email names that is NOT on record is returned as new with the "
    "name the customer used. `confidence` on the instance is your certainty this "
    "is a dependant of THIS customer (not a friend's, not a hypothetical); on each "
    "field, your certainty the value is right for that dependant (1.0 = stated "
    "outright, 0.5 = implied, lower = speculative). Omit fields the email says "
    "nothing about. Values are strings: numbers as digits, dates as YYYY-MM-DD, "
    "yes/no as Yes/No, dropdowns as one of the listed options."
)


def _kind_lines(groups: list[dict]) -> list[str]:
    lines = ["Kinds of dependant this business tracks:"]
    for g in groups:
        lines.append(f"- kind '{g['label']}' (plural {g['label_plural']}); fields:")
        for f in g["fields"]:
            opts = f" options={f['options']}" if f.get("options") else ""
            hint = f" — {f['hint']}" if f.get("hint") else ""
            # An expiry is the day it RUNS OUT. A vet card prints "Given" and
            # "Next due" side by side, and the given date is the wrong one.
            exp = (" [an EXPIRY: give the date it lapses — next due / valid until — never "
                   "the date it was given; several dates → the earliest]"
                   if f.get("durability") == "expires" else "")
            lines.append(f"    key={f['key']} ({f['label']}, type={f['type']}{opts}){hint}{exp}")
    return lines


def _known_lines(groups: list[dict], instances: list[dict]) -> list[str]:
    by_id = {g.get("id"): g for g in groups}
    known = []
    for inst in instances or []:
        g = by_id.get(inst.get("group_id"))
        if g:
            known.append(f"- {g['label']} '{inst.get('name')}': " + (json.dumps(inst.get("data") or {}, ensure_ascii=False)))
    return (["Already on this customer's record:"] + known) if known else \
        ["Nothing on this customer's record yet."]


def build_user_prompt(groups: list[dict], instances: list[dict], subject, body, from_name) -> str:
    lines = _kind_lines(groups) + [""] + _known_lines(groups, instances) + [""]
    lines.append(f"Email from: {from_name or 'unknown'}")
    lines.append(f"Subject: {subject or '(none)'}")
    lines.append(f"Body:\n{agent.strip_quoted(body or '')[:MAX_BODY_CHARS]}")
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    try:
        data = json.loads(text)
        return [i for i in (data.get("instances") or []) if isinstance(i, dict)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def extract(tenant_id: str, groups: list[dict], instances: list[dict], *,
                  subject, body, from_name=None, email_log_id=None) -> list[dict]:
    if not groups:
        return []
    msg = await llm.create(
        client=agent.client(), tenant_id=tenant_id, operation="dependant_extract",
        model=settings.anthropic_model, max_tokens=800, system=SYSTEM,
        messages=[{"role": "user",
                   "content": build_user_prompt(groups, instances, subject, body, from_name)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="dependant_extract",
                           email_log_id=email_log_id)
    return parse_response(text)


def _echo_norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())


def _echoes(value, text_norm: str) -> bool:
    """Only date-shaped and number-shaped values are held to the text: every
    digit run in the value must appear in the email (a date written any way
    round still shares its numbers)."""
    v = str(value or "")
    digits = re.findall(r"\d+", v)
    if not digits:
        return True

    def run_in_text(d: str) -> bool:
        d2 = d.lstrip("0") or "0"
        return bool(re.search(r"(?<!\d)(?:" + re.escape(d) + "|" + re.escape(d2) + r")(?!\d)", text_norm))

    d = rg.parse_date(v)
    if d:
        # A date written any way round shares its day and its year with the
        # text; the month may be a word ("19 September 2026").
        return run_in_text(str(d.year)) and run_in_text(f"{d.day:02d}")
    return all(run_in_text(x) for x in digits)


def _loose_match(g: dict, name: str, instances: list[dict] | None) -> dict | None:
    """"The Byre hot tub" is "The Byre" (live 2026-09-04: a second chip for a
    dependant that already existed): one name contains the other, same kind."""
    n = name.lower().strip()
    for inst in instances or []:
        if inst.get("group_id") != g.get("id"):
            continue
        have = str(inst.get("name") or "").strip().lower()
        if have and (have in n or n in have) and min(len(have), len(n)) >= 3:
            return inst
    return None


def plan(groups: list[dict], instances: list[dict], extracted: list[dict],
         text: str | None = None, confirm_all: bool = False) -> list[dict]:
    """Pure: what each extraction becomes. Returns steps
    {action: 'write'|'suggest'|'skip', group, name, existing, data, meta_source, conf}
    — write = merge onto a known instance (the gate above the autofill floor);
    suggest = a chip (a new instance, a middling read, or a contradiction of a
    `once` field already on record); skip = below the floor or unknown."""
    steps = []
    by_name = {}
    for inst in instances or []:
        by_name[(inst.get("group_id"), str(inst.get("name") or "").strip().lower())] = inst
    text_norm = _echo_norm(text) if text else None
    for item in extracted:
        g = rg.find_group(groups, item.get("group"))
        name = " ".join(str(item.get("name") or "").split())[:80]
        if not g or not name:
            continue
        inst_conf = float(item.get("confidence") or 0)
        raw = {}
        field_conf = {}
        for f in item.get("fields") or []:
            if not isinstance(f, dict):
                continue
            fd = rg.field_for(g, f.get("key"))
            if not fd:
                continue
            raw[fd["key"]] = f.get("value")
            field_conf[fd["key"]] = float(f.get("confidence") or 0)
        data = rg.normalise_data(g, raw)
        # A value the email never says is not a read (live 2026-09-04: a chip
        # offered "2025-07-08" from an email with no date in it) — dates and
        # numbers must echo the text; words are left to the confidence gate.
        if text_norm is not None:
            for k in list(data):
                if not _echoes(data[k], text_norm):
                    data.pop(k)
                    field_conf.pop(k, None)
        existing = by_name.get((g.get("id"), name.lower())) or _loose_match(g, name, instances)
        if existing is None:
            if crm_fields.gate(inst_conf) == "skip" or not data and inst_conf < 0.75:
                # a bare mention with nothing known about it is not worth a chip
                steps.append({"action": "skip", "group": g, "name": name})
                continue
            steps.append({"action": "suggest", "group": g, "name": name, "existing": None,
                          "data": data, "conf": inst_conf})
            continue
        # A read that says what the record already says changes nothing, so it
        # asks nothing (K1 rehearsal 2026-09-22: a vet card's "labradoodle"
        # queued "confirm Breed (Labradoodle)" against a record reading exactly
        # that). Compared normalised, so case and spacing are not a change.
        on_file = existing.get("data") or {}
        data = {k: v for k, v in data.items()
                if on_file.get(k) in (None, "") or rg._norm(on_file.get(k)) != rg._norm(v)}
        sure = {k: v for k, v in data.items() if crm_fields.gate(field_conf.get(k, 0)) == "autofill"}
        mid = {k: v for k, v in data.items() if crm_fields.gate(field_conf.get(k, 0)) == "suggest"}
        # a sure read that contradicts a once-field on record is a question, not a write
        cur = existing.get("data") or {}
        for k in list(sure):
            fd = rg.field_for(g, k)
            if fd and fd.get("durability") == "once" and cur.get(k) not in (None, "") \
                    and rg._norm(cur.get(k)) != rg._norm(sure[k]):
                mid[k] = sure.pop(k)
            # `set_by: owner` (Kris 2026-09-21) — the business said a person
            # decides this one, so confidence is beside the point: it becomes a
            # chip however sure the model is. That is the whole setting.
            # Everything downstream already existed: the chip, the accept
            # route, and the gate that opens when the value finally lands.
            elif fd and fd.get("set_by") == "owner":
                mid[k] = sure.pop(k)
            # A DOCUMENT that would CHANGE what is on file is the owner's call
            # (Kris 2026-09-22: the card should arrive "asking for a
            # confirmation to use it to update info" — and "it should only
            # offer to change info that has actually changed, if it's new info
            # then it's not an issue"). So an empty field fills itself from
            # the card; a different value waits for a person.
            elif confirm_all and on_file.get(k) not in (None, ""):
                mid[k] = sure.pop(k)
        if sure:
            steps.append({"action": "write", "group": g, "name": existing.get("name") or name,
                          "existing": existing, "data": sure, "conf": inst_conf})
        if mid:
            steps.append({"action": "suggest", "group": g, "name": existing.get("name") or name,
                          "existing": existing, "data": mid, "conf": inst_conf})
    return steps


def apply(tenant_id: str, contact_id: str, groups: list[dict], instances: list[dict],
          extracted: list[dict], email_log_id: str | None, text: str | None = None,
          confirm_all: bool = False) -> dict:
    """Execute the plan: writes merge by durability onto the instance;
    suggestions queue as chips keyed on the instance (or, for a new one, on
    the contact with the group in the key). Returns a tally for the log."""
    tally = {"written": 0, "suggested": 0, "skipped": 0}
    for step in plan(groups, instances, extracted, text=text, confirm_all=confirm_all):
        g = step["group"]
        if step["action"] == "write":
            ex = step["existing"]
            data, meta, changed = rg.merge(ex.get("data"), ex.get("field_meta"), step["data"], g,
                                           source="extracted", email_log_id=email_log_id)
            if changed:
                db.upsert_requirement_instance(tenant_id, g["id"], contact_id, ex.get("name") or step["name"],
                                               data, meta)
                tally["written"] += len(changed)
        elif step["action"] == "suggest":
            if step["existing"] is None:
                db.crm_insert_field_suggestion(
                    tenant_id, entity="dependant", record_id=contact_id,
                    field_key=NEW_PREFIX + str(g.get("id")),
                    value=json.dumps({"name": step["name"], "fields": step["data"]}, ensure_ascii=False),
                    confidence=step["conf"], email_log_id=email_log_id)
                tally["suggested"] += 1
            else:
                for k, v in step["data"].items():
                    db.crm_insert_field_suggestion(
                        tenant_id, entity="dependant", record_id=step["existing"]["id"],
                        field_key=k, value=str(v), confidence=step["conf"], email_log_id=email_log_id)
                    tally["suggested"] += 1
        else:
            tally["skipped"] += 1
    return tally


async def run_for_email(tenant_id: str, contact_id: str | None, *, subject, body,
                        from_name=None, email_log_id=None) -> dict | None:
    """The pipeline hook: nothing happens for a tenant with no group or an
    email with no contact. Best-effort — never fails the email."""
    if not contact_id:
        return None
    try:
        groups = rg.clean_groups(db.list_requirement_groups(tenant_id))
        if not groups:
            return None
        instances = db.list_requirement_instances(tenant_id, contact_id)
        extracted = await extract(tenant_id, groups, instances, subject=subject, body=body,
                                  from_name=from_name, email_log_id=email_log_id)
        if not extracted:
            return None
        tally = apply(tenant_id, contact_id, groups, instances, extracted, email_log_id,
                      text=f"{subject or ''}\n{body or ''}")
        if tally["written"] or tally["suggested"]:
            db.log_agent_event(
                tenant_id, "dependant_autofill",
                f"Read {tally['written']} dependant value(s) from this email"
                + (f", {tally['suggested']} to confirm" if tally["suggested"] else ""),
                email_log_id=email_log_id)
        return tally
    except Exception:
        log.warning("dependant extraction failed (tenant=%s)", tenant_id, exc_info=True)
        return None


# ── the card itself (plan-document-backed-facts slice 4, 2026-09-22) ─────────
# Until now a vet card, a certificate or an MOT arrived, was kept on the record
# — and nobody read it: no label, and the new expiry sat INSIDE the PDF where
# the gate could not see it, so the customer had to type the date out as well
# (Kris: "Doc still aren't being auto labelled"). Once the file is kept, its
# text is read and ONE call does both jobs: name the document the way the
# business would file it, and read the dependant facts it states. The facts go
# through the same `apply` as an email's — same confidence gate, same echo
# check (against the document's own text), and `set_by: owner` still turns a
# sure read into a confirm chip. PDFs with a text layer only: a photographed
# card needs OCR, which is out of scope.

DOC_SYSTEM = (
    "You read documents a customer attached to an email to a business: a vet's "
    "vaccination record, an insurance certificate, an MOT, a passport page. For "
    "each document: `label` is what the business would file it as, 2-5 words, "
    "plain and specific ('Vaccination record', 'Insurance certificate', 'MOT "
    "certificate') — no names, no dates; `dependant` is the exact record name of "
    "the dependant it is about when the document names one (empty when it names "
    "none or you cannot tell). Then return the dependant facts the documents "
    "state, exactly as for an email: only what a document actually says, never "
    "guessed. Use the record's exact name for a dependant already known. Values "
    "are strings: dates as YYYY-MM-DD. `confidence` on a field is your certainty "
    "the value is right for that dependant (1.0 = printed plainly on the document)."
)

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "label": {"type": "string"},
                    "dependant": {"type": "string"},
                },
                "required": ["index", "label", "dependant"],
                "additionalProperties": False,
            },
        },
        "instances": SCHEMA["properties"]["instances"],
    },
    "required": ["documents", "instances"],
    "additionalProperties": False,
}

MAX_DOC_CHARS = 3000         # per document; a vet card is well under 1,000
MAX_DOCS = 3                 # per email


def build_doc_prompt(groups: list[dict], instances: list[dict], docs: list[dict]) -> str:
    lines = (_kind_lines(groups) + [""] + _known_lines(groups, instances) + [""]) if groups \
        else ["This business tracks no dependants: return no instances, only the labels.", ""]
    for i, d in enumerate(docs):
        lines.append(f"Document {i} — file '{d.get('filename')}':")
        lines.append(str(d.get("text") or "")[:MAX_DOC_CHARS])
        lines.append("")
    return "\n".join(lines)


def parse_doc_response(text: str) -> tuple[list[dict], list[dict]]:
    try:
        data = json.loads(text)
        return ([d for d in (data.get("documents") or []) if isinstance(d, dict)],
                [i for i in (data.get("instances") or []) if isinstance(i, dict)])
    except (json.JSONDecodeError, AttributeError, TypeError):
        return [], []


def clean_label(label) -> str | None:
    s = " ".join(str(label or "").split()).strip(" .-—:")[:60]
    return (s[:1].upper() + s[1:]) if s else None


def label_plan(docs: list[dict], read: list[dict], instances: list[dict]) -> list[dict]:
    """Pure: the patch each kept document gets — {id, patch}. A label only
    where the document has none (the owner's own label is never overwritten);
    a dependant link only where the file was not already linked AND the name
    is one on the record (a guessed name never invents a link)."""
    by_name = {str(i.get("name") or "").strip().lower(): i for i in instances or []}
    out = []
    for r in read:
        try:
            idx = int(r.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(docs):
            continue
        d = docs[idx]
        patch = {}
        label = clean_label(r.get("label"))
        if label and not (d.get("label") or "").strip():
            patch["label"] = label
        inst = by_name.get(str(r.get("dependant") or "").strip().lower())
        if inst and not d.get("instance_id"):
            patch["instance_id"] = inst.get("id")
        if patch:
            out.append({"id": d.get("id"), "patch": patch})
    return out


async def read_documents(tenant_id: str, contact_id: str | None, docs: list[dict], *,
                         email_log_id: str | None = None, contact: str | None = None) -> dict | None:
    """The pipeline hook, after the files are kept: `docs` are the kept rows,
    each with the `text` read from it. Labels them, links them to their
    dependant, and reads the facts they state. Best-effort — never fails the
    email."""
    docs = [d for d in (docs or []) if (d.get("text") or "").strip() and d.get("id")][:MAX_DOCS]
    if not docs:
        return None
    try:
        groups = rg.clean_groups(db.list_requirement_groups(tenant_id))
        instances = db.list_requirement_instances(tenant_id, contact_id) if contact_id else []
        msg = await llm.create(
            client=agent.client(), tenant_id=tenant_id, operation="document_read",
            model=settings.anthropic_model, max_tokens=800, system=DOC_SYSTEM,
            messages=[{"role": "user", "content": build_doc_prompt(groups, instances, docs)}],
            output_config={"format": {"type": "json_schema", "schema": DOC_SCHEMA}},
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="document_read",
                               email_log_id=email_log_id)
        read, extracted = parse_doc_response(text)
        labelled = []
        for step in label_plan(docs, read, instances):
            try:
                db.update_document(step["id"], tenant_id, step["patch"])
                labelled.append(step)
            except Exception:  # noqa: BLE001
                log.warning("document label write failed (tenant=%s)", tenant_id, exc_info=True)
        tally = {"written": 0, "suggested": 0, "skipped": 0}
        if groups and contact_id and extracted:
            # The echo check reads the DOCUMENTS' text: a date the card never
            # prints is not a read, however sure the model sounds.
            tally = apply(tenant_id, contact_id, groups, instances, extracted, email_log_id,
                          text="\n".join(d["text"] for d in docs), confirm_all=True)
        names = [s["patch"]["label"] for s in labelled if s["patch"].get("label")]
        if names or tally["written"] or tally["suggested"]:
            said = f"Read {', '.join(names) or 'the attached document'}"
            if tally["written"]:
                said += f": {tally['written']} value(s) updated on the record"
            if tally["suggested"]:
                said += f"{',' if tally['written'] else ':'} {tally['suggested']} waiting for you to confirm"
            db.log_agent_event(tenant_id, "document_read", said, contact=contact,
                               email_log_id=email_log_id,
                               detail={"labelled": labelled, **tally})
        return {"labelled": len(labelled), **tally}
    except Exception:
        log.warning("document read failed (tenant=%s)", tenant_id, exc_info=True)
        return None


# ── the chips: what accepting one does ────────────────────────────────────────
def accept(tenant_id: str, suggestion: dict, client=None) -> bool:
    """A dependant chip: a field on a known instance, or a whole new instance.
    Returns False when the instance or group has gone."""
    try:
        key = suggestion.get("field_key") or ""
        groups = rg.clean_groups(db.list_requirement_groups(tenant_id, client=client))
        if key.startswith(NEW_PREFIX):
            g = next((x for x in groups if x.get("id") == key[len(NEW_PREFIX):]), None)
            if not g:
                return False
            payload = json.loads(suggestion.get("value") or "{}")
            name = " ".join(str(payload.get("name") or "").split())[:80]
            if not name:
                return False
            data, meta, _c = rg.merge({}, {}, rg.normalise_data(g, payload.get("fields") or {}), g,
                                      source="confirmed", email_log_id=suggestion.get("email_log_id"))
            return db.upsert_requirement_instance(tenant_id, g["id"], suggestion["record_id"], name,
                                                  data, meta, client=client) is not None
        inst = db.get_requirement_instance(suggestion["record_id"], tenant_id, client=client)
        if not inst:
            return False
        g = next((x for x in groups if x.get("id") == inst.get("group_id")), None)
        if not g:
            return False
        data, meta, _c = rg.merge(inst.get("data"), inst.get("field_meta"),
                                  rg.normalise_data(g, {key: suggestion.get("value")}), g,
                                  source="confirmed", email_log_id=suggestion.get("email_log_id"))
        db.upsert_requirement_instance(tenant_id, g["id"], inst["contact_id"], inst["name"], data, meta,
                                       client=client)
        return True
    except Exception:
        log.warning("dependant suggestion accept failed", exc_info=True)
        return False


def _same_dependant(name, have) -> bool:
    """'biscuit' is 'Biscuit', and 'The Byre hot tub' is 'The Byre' — the same
    loose rule the plan uses to match a read to a row."""
    n = str(name or "").strip().lower()
    h = str(have or "").strip().lower()
    if not n or not h:
        return False
    return n == h or ((h in n or n in h) and min(len(h), len(n)) >= 3)


def absorb_new_chips(tenant_id: str, contact_id: str, group: dict, instance: dict | None,
                     client=None) -> int:
    """A dependant that has just landed on the record by another door — a
    booking's subjects, the portal, the owner's own add — takes over any
    pending 'a new <kind>: <name>' chip for the same customer, rather than
    leaving the chip offering to create what now exists (kennels film,
    2026-09-20: the enquiry queued 'a new dog: Biscuit — add?', the booking
    then created Biscuit, and the chip sat above her row). The chip's facts
    fill only what the row still lacks — the row is the newer read — and the
    chip resolves as accepted. Best-effort; returns how many chips it took."""
    if not (instance and instance.get("id") and group and group.get("id") and contact_id):
        return 0
    try:
        want = NEW_PREFIX + str(group["id"])
        pend = [s for s in db.crm_pending_suggestions(tenant_id, record_ids=[contact_id], client=client)
                if s.get("entity") == "dependant" and (s.get("field_key") or "") == want]
        if not pend:
            return 0
        row = dict(instance)
        taken = 0
        for s in pend:
            try:
                payload = json.loads(s.get("value") or "{}")
            except ValueError:
                payload = {}
            name = " ".join(str(payload.get("name") or "").split())
            if not _same_dependant(name, row.get("name")):
                continue
            incoming = rg.normalise_data(group, payload.get("fields") or {})
            gaps = {k: v for k, v in incoming.items() if (row.get("data") or {}).get(k) in (None, "")}
            if gaps:
                data, meta, changed = rg.merge(row.get("data"), row.get("field_meta"), gaps, group,
                                               source="extracted", email_log_id=s.get("email_log_id"))
                if changed:
                    saved = db.upsert_requirement_instance(tenant_id, group["id"], contact_id,
                                                           row.get("name") or name, data, meta, client=client)
                    row = dict(saved or {**row, "data": data, "field_meta": meta})
            db.crm_resolve_suggestion(s["id"], tenant_id, "accepted", client=client)
            taken += 1
        return taken
    except Exception:
        log.warning("absorbing a new-dependant chip failed (tenant=%s)", tenant_id, exc_info=True)
        return 0


def annotate(suggestions: list[dict], groups: list[dict], instances: list[dict]) -> list[dict]:
    """Pure: the chip text for the contact card — which dependant, which field,
    what it would replace; a new one reads 'a new Horse: Biscuit'. A new-one
    chip for a dependant already on the record is not shown: the row is the
    answer (absorb_new_chips settles it at the door it came in by)."""
    by_gid = {g.get("id"): g for g in groups}
    by_iid = {i.get("id"): i for i in instances}
    out = []
    for s in suggestions:
        if s.get("entity") != "dependant":
            continue
        key = s.get("field_key") or ""
        if key.startswith(NEW_PREFIX):
            g = by_gid.get(key[len(NEW_PREFIX):])
            if not g:
                continue
            try:
                payload = json.loads(s.get("value") or "{}")
            except ValueError:
                payload = {}
            who = payload.get("name") or "?"
            if any(i.get("group_id") == g.get("id") and _same_dependant(who, i.get("name"))
                   for i in instances):
                continue
            facts = "; ".join(f"{rg.field_for(g, k)['label']}: {v}" for k, v in (payload.get("fields") or {}).items()
                              if rg.field_for(g, k))
            out.append({**s, "kind": "new", "label": f"a new {g['label'].lower()}",
                        "who": who, "value_text": facts or "nothing else yet",
                        "replaces": ""})
            continue
        inst = by_iid.get(s.get("record_id"))
        g = by_gid.get((inst or {}).get("group_id"))
        fd = rg.field_for(g, key) if g else None
        if not inst or not fd:
            continue
        stored = (inst.get("data") or {}).get(fd["key"])
        out.append({**s, "kind": "field", "label": fd["label"], "who": inst.get("name"),
                    "value_text": str(s.get("value") or ""),
                    "replaces": str(stored) if stored not in (None, "") and str(stored) != str(s.get("value")) else ""})
    return out
