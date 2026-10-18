"""Progress ladders — slice 1 of docs/plan-progress-packages.md (2026-09-05).

A participant (a dependant — a named subject — or the customer
themselves) works through an ORDERED set of levels that the business moves
them along: Levels 1–7, belts, grades, "Beginner / Improver /
Intermediate". Run #7 proved the two halves that already existed — the level
as a confirm fact the operator writes, the portal answering "According to the
record, Finn is in Stage 4" — and found the halves that did not: the levels as
a setting (so the value is a dropdown, not free text), a prerequisite that
gates a service on a level, the "moved up" message, and the move as one tap.

The model adds NO table for the ladder itself. A ladder on a dependant kind is
a field of that kind — `type: dropdown`, `durability: confirm`, `options` = the
levels in order — carrying a `ladder` block {notify, gates}. A ladder on the
customer is a `crm_field_definitions` row (entity contact, dropdown) carrying
the same block in its `ladder` column. Everything that already reads confirm
facts (the drafter's customer context, the portal's record, the CRM card)
therefore sees a level with no new infrastructure; this module adds the setting's
editor, the prerequisite refusal, the prompt block, and the level change.
"""
from __future__ import annotations

import re

import logging
import re

from app import requirement_groups as rg

log = logging.getLogger(__name__)

NOTIFY = ("approve", "auto", "off")
MAX_LADDERS = 3
MAX_LEVELS = 20
KIND = "level_change"                 # feed / push / agent_events kind
CONTACT = "contact"                   # the "who" value when the customer progresses

PURPOSE = ("A short message telling the customer that a level has changed on the "
           "record — who moved, from what to what, said warmly in the business's "
           "voice and in the business's own words for the level. State the new level "
           "exactly as given. If the facts name a next step (what the new level "
           "means, what comes next) say it in one line; otherwise nothing else — no "
           "prices, no questions, no promises. Never guess a pronoun from a name: "
           "use the name, or 'they'. When the facts say the person who moved IS the "
           "reader, write to them as 'you' ('you've moved up to…') — never their "
           "name in the third person, they are not being told about someone else.")


# ── the definition ───────────────────────────────────────────────────────────
def clean_levels(levels) -> list[str]:
    """Levels in order, deduped, capped — from a list or a comma/newline text."""
    if isinstance(levels, str):
        levels = re.split(r"[\n,;|]+", levels)
    seen, out = set(), []
    for lv in levels or []:
        s = " ".join(str(lv or "").split())[:60]
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
        if len(out) >= MAX_LEVELS:
            break
    return out


def clean_gates(gates, levels: list[str]) -> list[dict]:
    """[{service, min}] — a service that needs at least a level. From a list of
    dicts or a text of 'Service = Level' lines. Unknown levels are dropped."""
    if isinstance(gates, str):
        items = []
        for line in re.split(r"[\n;]+", gates):
            if "=" not in line and ":" not in line:
                continue
            svc, lv = (re.split(r"\s*[=:]\s*", line.strip(), maxsplit=1) + [""])[:2]
            items.append({"service": svc, "min": lv})
        gates = items
    by_norm = {rg._norm(lv): lv for lv in levels}
    out, seen = [], set()
    for g in gates or []:
        if not isinstance(g, dict):
            continue
        svc = " ".join(str(g.get("service") or "").split())[:60]
        lv = by_norm.get(rg._norm(g.get("min") or g.get("level") or ""))
        if not svc or not lv or rg._norm(svc) in seen:
            continue
        seen.add(rg._norm(svc))
        out.append({"service": svc, "min": lv})
    return out[:20]


def clean_block(block, levels: list[str]) -> dict | None:
    """The `ladder` block on a field / definition: {notify, gates}."""
    if block is None or block is False:
        return None
    block = block if isinstance(block, dict) else {}
    notify = str(block.get("notify") or "approve").strip().lower()
    return {"notify": notify if notify in NOTIFY else "approve",
            "gates": clean_gates(block.get("gates"), levels)}


def is_ladder(field: dict | None) -> bool:
    return bool(field) and isinstance(field.get("ladder"), dict) \
        and field.get("type") == "dropdown" and len(field.get("options") or []) >= 2


def levels_of(field: dict) -> list[str]:
    return list(field.get("options") or [])


def index_of(field: dict, value) -> int | None:
    """Position of `value` on the ladder (0-based), None when not a level."""
    n = rg._norm(value)
    for i, lv in enumerate(levels_of(field)):
        if rg._norm(lv) == n:
            return i
    return None


def ladder_fields(groups) -> list[tuple[dict, dict]]:
    """(group, field) for every ladder on every kind."""
    out = []
    for g in rg.clean_groups(groups):
        for f in g.get("fields") or []:
            if is_ladder(f):
                out.append((g, f))
    return out


# ── the editor (Dependants page, positional arrays) ──────────────────────────
def parse_editor(l_who, l_label, l_levels, l_notify, l_gates) -> list[dict]:
    """Rows from the Progress section → [{who, label, levels, notify, gates}].
    `who` is CONTACT or a kind's label; an empty label or fewer than two
    levels drops the row."""
    out = []
    for i, label in enumerate(l_label or []):
        def at(xs):
            return xs[i] if xs and i < len(xs) else ""
        label = " ".join(str(label or "").split())[:rg._MAX_LABEL]
        levels = clean_levels(at(l_levels))
        if not label or len(levels) < 2:
            continue
        who = " ".join(str(at(l_who) or CONTACT).split())
        out.append({"who": who or CONTACT, "label": label, "levels": levels,
                    **clean_block({"notify": at(l_notify), "gates": at(l_gates)}, levels)})
        if len(out) >= MAX_LADDERS:
            break
    return out


def apply_to_groups(groups: list[dict], ladders: list[dict]) -> list[dict]:
    """The kind-ladders written onto their groups as ladder fields (replacing
    the kind's previous ladder field of the same key; a ladder no longer
    listed loses its `ladder` block but keeps its values as a plain
    dropdown). Contact ladders are not touched here."""
    groups = rg.clean_groups(groups)
    wanted: dict[str, list[dict]] = {}
    for lad in ladders:
        if lad["who"] == CONTACT:
            continue
        wanted.setdefault(rg._norm(lad["who"]), []).append(lad)
    for g in groups:
        mine = wanted.get(rg._norm(g["label"]), [])
        keys = {rg.key_for(l["label"]) for l in mine}
        fields = []
        for f in g.get("fields") or []:
            if f.get("key") in keys:
                continue                        # rewritten below
            if is_ladder(f):
                f = {k: v for k, v in f.items() if k != "ladder"}
            fields.append(f)
        for lad in mine:
            fields.append({"key": rg.key_for(lad["label"]), "label": lad["label"],
                           "type": "dropdown", "options": lad["levels"], "required": False,
                           "hint": "The level on record — moved by the business, read by the customer.",
                           "durability": "confirm", "gate": False,
                           "ladder": {"notify": lad["notify"], "gates": lad["gates"]}})
        g["fields"] = fields[:rg.MAX_FIELDS]
    return groups


def from_groups(groups) -> list[dict]:
    """The editor's rows for the kind-ladders on record."""
    return [{"who": g["label"], "label": f["label"], "levels": levels_of(f),
             "notify": f["ladder"].get("notify", "approve"), "gates": f["ladder"].get("gates") or []}
            for g, f in ladder_fields(groups)]


def from_contact_defs(defs) -> list[dict]:
    out = []
    for d in defs or []:
        if d.get("entity") != "contact" or not isinstance(d.get("ladder"), dict):
            continue
        levels = clean_levels(d.get("options"))
        if len(levels) < 2:
            continue
        blk = clean_block(d.get("ladder"), levels) or {}
        out.append({"who": CONTACT, "label": d.get("label"), "levels": levels,
                    "notify": blk.get("notify", "approve"), "gates": blk.get("gates") or [],
                    "def_id": d.get("id"), "key": d.get("key")})
    return out


def sync_contact_ladders(tenant_id: str, ladders: list[dict], client=None) -> None:
    """The contact ladders as `crm_field_definitions` rows (entity contact,
    dropdown, options = the levels, `ladder` = the block): insert, update in
    place, or drop the block from a definition no longer a ladder (its values
    survive as a plain dropdown)."""
    from app import db
    wanted = {rg.key_for(l["label"]): l for l in ladders if l["who"] == CONTACT}
    existing = db.crm_list_field_definitions(tenant_id, entity=CONTACT, client=client)
    by_key = {d["key"]: d for d in existing}
    for key, lad in wanted.items():
        row = {"label": lad["label"], "field_type": "dropdown", "options": lad["levels"],
               "ladder": {"notify": lad["notify"], "gates": lad["gates"]},
               "extraction_hint": f"The customer's current {lad['label'].lower()} — one of: " + ", ".join(lad["levels"])}
        if key in by_key:
            db.crm_update_field_definition(by_key[key]["id"], tenant_id, row, client=client)
        else:
            db.crm_insert_field_definition(tenant_id, {"entity": CONTACT, "key": key, "required": False,
                                                       "display_order": len(existing) * 10, **row},
                                           client=client)
    for key, d in by_key.items():
        if key not in wanted and isinstance(d.get("ladder"), dict):
            db.crm_update_field_definition(d["id"], tenant_id, {"ladder": None}, client=client)


# ── the prerequisite (the booking gate) ──────────────────────────────────────
def _gate_for(field: dict, service: str) -> dict | None:
    n = rg._norm(service)
    for g in (field.get("ladder") or {}).get("gates") or []:
        gs = rg._norm(g.get("service"))
        if gs and (gs == n or gs in n or n in gs):
            return g
    return None


def prerequisite_refusal(group: dict, data: dict | None, name: str, service: str) -> dict | None:
    """None when every ladder on this kind clears the service, else the
    executor's refusal: the customer's level, the level the service needs,
    worded so the agent explains rather than apologises."""
    for f in group.get("fields") or []:
        if not is_ladder(f):
            continue
        gate = _gate_for(f, service)
        if not gate:
            continue
        have = (data or {}).get(f["key"])
        hi, mi = index_of(f, have), index_of(f, gate["min"])
        if mi is None:
            continue
        if hi is None:
            return {"error": "prerequisite",
                    "ladder": f["label"], "needed": gate["min"], "have": None,
                    "message": (f"{service} needs at least {gate['min']} on the {f['label'].lower()} "
                                f"ladder and {name} has no level on record yet. Tell the customer "
                                f"what the level needs to be and how the business assesses or "
                                f"places — do not book {service} for {name} until the level is on record.")}
        if hi < mi:
            return {"error": "prerequisite",
                    "ladder": f["label"], "needed": gate["min"], "have": have,
                    "message": (f"{name} is at {have} and {service} needs at least {gate['min']}. "
                                f"Say so plainly — which level {name} is at, which one {service} "
                                f"needs — and offer what the business does at {have} instead; "
                                f"never book {service} for {name} at this level.")}
    return None


def contact_prerequisite_refusal(tenant_id: str, contact_email: str | None, service: str,
                                 *, client=None) -> dict | None:
    """The customer's OWN ladders against a service — the adult side of
    prerequisite_refusal (run #8, 2026-09-05: a participant has no dependant,
    the subject gate never ran, and the portal booked the assessment resource for a
    participant with no level on record). Reads crm_field_definitions' ladder fields
    and the contact's custom_data; None when nothing gates this service, when
    the contact is unknown, or the required level is satisfied. A read error
    raises VerificationUnavailable for owner review; it never means the
    prerequisite passed."""
    if not contact_email or not (service or "").strip():
        return None
    try:
        from app import db
        defs = db.crm_list_field_definitions(tenant_id, entity=CONTACT, client=client)
        fields = []
        for d in defs or []:
            if not isinstance(d.get("ladder"), dict):
                continue
            levels = clean_levels(d.get("options"))
            if len(levels) < 2:
                continue
            fields.append({"key": d["key"], "label": d.get("label") or d["key"], "type": "dropdown",
                           "options": levels,
                           "ladder": clean_block(d.get("ladder"), levels) or {"notify": "approve", "gates": []}})
        if not any((f["ladder"] or {}).get("gates") for f in fields):
            return None
        c = db.crm_contact_by_email(tenant_id, contact_email, client=client)
        if not c:
            return None
        name = c.get("name") or contact_email
        return prerequisite_refusal({"fields": fields}, c.get("custom_data") or {}, name, service)
    except Exception:
        log.warning("contact prerequisite gate failed (tenant=%s)", tenant_id, exc_info=True)
        from app.booking_errors import VerificationUnavailable
        raise VerificationUnavailable("the required level on the customer record") from None


# ── the prompt block ─────────────────────────────────────────────────────────
def render_for_prompt(groups) -> str:
    lines = []
    for g, f in ladder_fields(groups):
        lines.append(f"{g['label']} — {f['label']}, levels in order: " + " → ".join(levels_of(f))
                     + ". The business moves a " + g["label"].lower() + " up; a level the customer "
                     "claims is not a level on record — read the record.")
        for gate in f["ladder"].get("gates") or []:
            lines.append(f"  {gate['service']} needs at least {gate['min']}.")
    return "\n".join(lines)


# ── the level change ─────────────────────────────────────────────────────────
def changed_ladder(group: dict, before: dict | None, after: dict | None) -> list[tuple[dict, str | None, str]]:
    """(field, old, new) for every ladder field whose value moved."""
    out = []
    for f in group.get("fields") or []:
        if not is_ladder(f):
            continue
        old = (before or {}).get(f["key"])
        new = (after or {}).get(f["key"])
        if new and rg._norm(new) != rg._norm(old) and index_of(f, new) is not None:
            out.append((f, old, new))
    return out


_AWARD_WORDS = ("badge", "award", "belt", "grade", "certificate", "level", "stage")


def normalise_level(value, levels: list[str]) -> str | None:
    """A near miss for a level — "1", "stage 1", "Stage1", "STAGE 1 " — as the
    level itself (1b nit, run #7: Isla's stage sat on the ladder as "1")."""
    v = " ".join(str(value or "").split()).lower()
    if not v:
        return None
    for lv in levels:
        if lv.lower() == v:
            return lv
    squash = re.sub(r"[^a-z0-9]", "", v)
    for lv in levels:
        if re.sub(r"[^a-z0-9]", "", lv.lower()) == squash:
            return lv
    num = re.fullmatch(r"(?:[a-z ]*?)(\d+)", v)
    if num:
        hits = [lv for lv in levels if re.search(r"\b" + re.escape(num.group(1)) + r"\b", lv)]
        if len(hits) == 1:
            return hits[0]
    return None


def normalise_instances(tenant_id: str, groups: list[dict], client=None) -> int:
    """After a ladder is created or edited: every dependant whose ladder value
    is a near miss is rewritten to the level itself. Returns how many changed."""
    from app import db
    changed = 0
    for g in groups:
        lad = [f for f in g.get("fields") or [] if is_ladder(f)]
        if not lad or not g.get("id"):
            continue
        try:
            rows = db.db().table("requirement_instances").select("id,data").eq("tenant_id", tenant_id) \
                .eq("group_id", g["id"]).is_("archived_at", "null").execute().data or []
        except Exception:
            continue
        for inst in rows:
            data = dict(inst.get("data") or {})
            fixed = {}
            for f in lad:
                cur = data.get(f["key"])
                if cur in (None, "") or cur in levels_of(f):
                    continue
                norm = normalise_level(cur, levels_of(f))
                if norm:
                    fixed[f["key"]] = norm
            if fixed:
                data.update(fixed)
                try:
                    (client or db.db()).table("requirement_instances").update({"data": data}).eq("id", inst["id"]).execute()
                    changed += 1
                except Exception:
                    log.warning("ladder normalise failed (instance=%s)", inst.get("id"), exc_info=True)
    return changed


def contact_field_changed(tenant_id: str, contact: dict, key: str, old, new, *, client=None) -> bool:
    """The contact-card side of a level change (1b nit): a customer-ladder
    field written by an accepted chip or an extraction fires the same trail
    and message as a dependant's move. Sync caller, async work — scheduled on
    the running loop, or run to completion when there is none."""
    import asyncio
    from app import db
    if not contact or not key or new in (None, "") or str(old or "") == str(new):
        return False
    defs = db.crm_list_field_definitions(tenant_id, entity=CONTACT, client=client)
    d = next((x for x in defs if x.get("key") == key and isinstance(x.get("ladder"), dict)), None)
    if not d:
        return False
    levels = clean_levels(d.get("options"))
    field = {"key": key, "label": d.get("label") or key, "type": "dropdown", "options": levels,
             "ladder": clean_block(d.get("ladder"), levels) or {"notify": "approve", "gates": []}}
    new_lv = normalise_level(new, levels) or str(new)
    if new_lv not in levels:
        return False
    instance = {"id": contact.get("id"), "name": contact.get("name") or contact.get("email") or "the customer"}
    group = {"label": "Customer", "label_plural": "Customers"}
    coro = on_level_change(tenant_id, contact, instance, group, field, old, new_lv, client=client)
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        asyncio.run(coro)
    return True


def facts_for(name: str, kind_label: str, field: dict, old: str | None, new: str,
              contact_name: str | None) -> str:
    levels = levels_of(field)
    i = index_of(field, new)
    award = any(w in field["label"].lower() for w in _AWARD_WORDS)
    parts = [f"{name} (the customer's {kind_label.lower()}) has moved to {new} on the "
             f"{field['label'].lower()} ladder" + (f", from {old}" if old else "") + "."]
    if award:
        # 1b nit: name the award — "the Stage 5 badge", "the Yellow belt" — so the
        # message says what was earned, not just that a value changed.
        parts.append(f"Name it exactly as the business does: they have earned \"{new}\" "
                     f"({field['label']}) — congratulate them by that name.")
    if i is not None:
        parts.append(f"That is level {i + 1} of {len(levels)}."
                     + (f" The next level is {levels[i + 1]}." if i + 1 < len(levels)
                        else " That is the top of the ladder."))
    if kind_label.lower() == "customer":
        # The adult side (run #8, 2026-09-05): the participant IS the reader.
        # "Kris has moved to Prompted" went to Kris; the facts now say who is
        # reading, and the message speaks to them.
        parts[0] = (f"The reader of this message — {name}, the customer themselves — has moved to "
                    f"{new} on the {field['label'].lower()} ladder"
                    + (f", from {old}" if old else "") + ". Write to them in the second person "
                    f"(\"you've moved up to {new}\"); never \"{name} has moved\" — they are not "
                    "being told about someone else.")
        parts.append(f"The message is to {contact_name or name}, about their own progress.")
    elif contact_name:
        parts.append(f"The message is to {contact_name}.")
    return " ".join(parts)


async def on_level_change(tenant_id: str, contact: dict, instance: dict, group: dict, field: dict,
                          old: str | None, new: str, *, booking: dict | None = None,
                          client=None) -> dict:
    """A ladder field moved on a dependant: the trail line, then per the
    ladder's `notify` — 'auto' composes and sends the message now (the tap is
    the approval, like On my way); 'approve' composes it into the approval
    queue and raises the feed card; 'off' records only. Never raises past
    here — the operator sees the outcome on the page."""
    from app import agent, db, disclosure, mailbox, push, usage
    notify = (field.get("ladder") or {}).get("notify", "approve")
    name = instance.get("name") or "the participant"
    to = (contact or {}).get("email")
    ref = (booking or {}).get("booking_ref")
    summary = f"{name} moved to {new} ({field['label']})" + (f" from {old}" if old else "")
    db.log_agent_event(tenant_id, KIND, summary, contact=to, booking_id=(booking or {}).get("id"),
                       booking_ref=ref, detail={"ladder": field["label"], "from": old, "to": new,
                                                "instance_id": instance.get("id"), "notify": notify})
    if notify == "off" or not to:
        return {"ok": True, "notify": notify, "sent": False}
    cfg = db.effective_config(tenant_id, (booking or {}).get("mailbox_id"), client=client)
    facts = facts_for(name, group["label"], field, old, new, (contact or {}).get("name"))
    try:
        result = await agent.compose_outbound(cfg, PURPOSE, facts, channel="email",
                                              tenant_id=tenant_id, log_operation=KIND)
    except Exception:
        log.warning("level-change compose failed (tenant=%s)", tenant_id, exc_info=True)
        return {"ok": False, "error": "compose_failed"}
    subject = f"{name} — {new}"
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[level change] {facts}", result.text,
        status="pending", kind=KIND, thread_id=(booking or {}).get("thread_id"), channel="email")
    usage.record_anthropic(tenant_id, result.model, result.usage, operation=KIND, email_log_id=log_id)
    url = f"/app/conversations/view?contact={to}"
    if notify == "auto":
        try:
            await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text),
                                    mailbox_id=(booking or {}).get("mailbox_id"))
        except Exception:
            log.warning("level-change send failed (tenant=%s)", tenant_id, exc_info=True)
            return {"ok": False, "error": "send_failed", "log_id": log_id}
        db.mark_email_log_sent(log_id, tenant_id, via="human", edited=False)
        push.notify(tenant_id, title=f"{name} moved to {new}", body=f"Told {contact.get('name') or to}.",
                    url=url, tag=f"level-{instance.get('id')}-{field['key']}", kind=KIND,
                    contact=to, contact_name=contact.get("name"))
        return {"ok": True, "notify": notify, "sent": True, "log_id": log_id, "text": result.text}
    push.notify(tenant_id, title=f"{name} moved to {new}",
                body=f"A message to {contact.get('name') or to} is waiting for your approval.",
                url=url, tag=f"level-{instance.get('id')}-{field['key']}", kind=KIND,
                contact=to, contact_name=contact.get("name"))
    return {"ok": True, "notify": notify, "sent": False, "log_id": log_id, "text": result.text}
