"""Revisioned setup proposals. A proposal never changes saved settings by itself.

Changes address stable line IDs; omitted fields and lines carry forward. Sources
belong to individual values, not to a model-written summary. This module is pure;
the repository commits the next card and any settings patch in one transaction.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

from app import setup_rows

PAGES = {page for page, _ in setup_rows.SUPPORTED}
PENDING = {"proposed", "held"}
MAX_LINES = 60


class Invalid(ValueError):
    pass


class Stale(Invalid):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid("A bounded, non-empty reference is required.")
    return value.strip()


def _base(page, current, revision, turn):
    if page not in PAGES:
        raise Invalid("This page does not support named setup cards yet.")
    if type(revision) is not int or revision < 0 or revision != (current or {}).get("revision", 0):
        raise Stale("The card changed. Reload it before trying again.")
    if current and current.get("page") != page:
        raise Invalid("The card belongs to another page.")
    card = deepcopy(current) if current else {
        "page": page, "id": str(uuid4()), "created_at": _now(), "lines": []}
    card.update(revision=revision + 1, source_turn=_text(turn), updated_at=_now())
    return card


def change(page, current, revision, turn, changes, considered=None):
    """append adds a proposal; set edits named fields; remove leaves a proposal.

    Removing a proposal never deletes a saved setting. Terminal lines remain in
    the card's record and cannot be edited back into pending work.
    """
    card = _base(page, current, revision, turn)
    if changes and current and not any(v["status"] in PENDING for v in card["lines"]):
        # The previous terminal card remains in revision history. New work gets
        # a new card ID without resetting the page's concurrency revision.
        card.update(id=str(uuid4()), created_at=card["updated_at"], lines=[])
        card.pop("last_action", None)
    if not isinstance(changes, list) or len(changes) > MAX_LINES or (not changes and not considered):
        raise Invalid("Supply between one and sixty explicit changes.")
    touched = set()
    for patch in changes:
        if not isinstance(patch, dict) or set(patch) - {"op", "id", "name", "fields", "sources", "held"}:
            raise Invalid("Unknown change fields.")
        op = patch.get("op")
        if not isinstance(op, str):
            raise Invalid("Choose append, set or remove.")
        if op == "append":
            if patch.get("id") is not None or not isinstance(patch.get("name"), str) or (page, patch["name"]) not in setup_rows.SUPPORTED:
                raise Invalid("Append requires a supported row name and no existing ID.")
            line = {"id": str(uuid4()), "name": patch["name"], "fields": {}, "sources": {},
                    "status": "proposed", "created_at": card["updated_at"]}
            card["lines"].append(line)
        elif op in {"set", "remove"}:
            line = next((v for v in card["lines"] if v["id"] == patch.get("id")), None)
            if line is None or line["status"] not in PENDING:
                raise Invalid("Choose a pending line from this card.")
            if line["id"] in touched:
                raise Invalid("Change each line only once per revision.")
            if "name" in patch:
                raise Invalid("An existing line's row type cannot change.")
        else:
            raise Invalid("Choose append, set or remove.")
        touched.add(line["id"])
        line.update(source_turn=card["source_turn"], updated_at=card["updated_at"])
        if op == "remove":
            if set(patch) != {"op", "id"}:
                raise Invalid("Removing a proposal takes only its ID.")
            line["status"] = "left"
            continue
        fields, sources = patch.get("fields"), patch.get("sources")
        if not isinstance(fields, dict) or (not fields and (op != "set" or "held" not in patch)) or set(fields) - setup_rows.KEYS[page, line["name"]]:
            raise Invalid("Supply only named fields supported by this row.")
        if not isinstance(sources, dict) or set(sources) != set(fields):
            raise Invalid("Every changed value needs its own source.")
        if line["name"] == "setting" and line["fields"].get("key") and fields.get("key", line["fields"]["key"]) != line["fields"]["key"]:
            raise Invalid("A setting line keeps its original key. Add a different setting as another line.")
        for source in sources.values():
            if not isinstance(source, dict) or set(source) != {"kind", "reference"}:
                raise Invalid("A source needs kind and reference.")
            if not isinstance(source["kind"], str) or source["kind"] not in {"owner_said", "owner_site", "assumed"}:
                raise Invalid("Unknown value source.")
            _text(source["reference"], 2000)
        if "held" in patch:
            if type(patch["held"]) is not bool:
                raise Invalid("Held must be true or false.")
            line["status"] = "held" if patch["held"] else "proposed"
        line["fields"].update(deepcopy(fields))
        line["sources"].update(deepcopy(sources))
    if len(card["lines"]) > MAX_LINES:
        raise Invalid("This card has reached its line limit.")
    identities = set()
    for line in card["lines"]:
        if line["status"] not in PENDING:
            continue
        fields = line["fields"]
        label = fields.get("name") or fields.get("label") or fields.get("key")
        kind = fields.get("kind", "") if page == "dependants" and line["name"] == "field" else ""
        if line['name'] == 'ladder': kind = fields.get('who', '')
        if not isinstance(label, str) or not isinstance(kind, str):
            continue
        identity = (line["name"], " ".join(label.casefold().split()), " ".join(kind.casefold().split()))
        if identity in identities:
            raise Invalid("This row appears more than once. Edit its existing line instead.")
        identities.add(identity)
    if considered is not None:
        from app import setup_considered
        card["considered"] = setup_considered.merge(page, card.get("considered", []), considered, card["source_turn"])
    # A newly changed proposal supersedes its old considered note. Untouched
    # controls and revision history retain their notes.
    answered = {page + "/" + (line["fields"].get("key", "") if line["name"] == "setting" else line["name"])
                for line in card["lines"] if line["id"] in touched and line["status"] in PENDING}
    if "considered" in card:
        card["considered"] = [item for item in card["considered"] if item["key"] not in answered]
    try:
        size = len(json.dumps(card, allow_nan=False))
    except (ValueError, TypeError):
        raise Invalid("Card values must be finite JSON values.") from None
    if size > 120_000:
        raise Invalid("The card is too large.")
    card["last_action"] = {"kind": "change", "ids": sorted(touched)}
    return refresh(card)


def refresh(card, saved=None):
    """Recompute problems and descriptions. Never trust client verdicts/prose."""
    card = deepcopy(card)
    pending = [line for line in card["lines"] if line["status"] in PENDING]
    rows = setup_rows.prepare(card["page"], [
        {"name": line["name"], "value": line["fields"]} for line in pending], saved=saved)
    for line, row in zip(pending, rows):
        line["problems"] = row["problems"]
        line["display"] = (setup_rows.describe(card["page"], line["name"], row["fields"], row["provided"])
                           if row.get("fields") else "Needs your answer: " + "; ".join(row["problems"]))
    card["status"] = ("proposed" if any(v["status"] == "proposed" for v in pending)
                      else "held" if pending else "applied" if any(v["status"] == "applied" for v in card["lines"])
                      else "considered" if card.get("considered") else "left")
    return card


def diary_patch(card, ids, cfg):
    """Merge selected service/resource rows through the diary's validation functions.

    Preserve every unrelated row and attribute, including settings introduced
    after this adapter. Reject unresolved references and validation data loss.
    Other page adapters must implement their own domain side effects first.
    """
    from app import diary_config, intake, services
    if card["page"] != "diary_settings":
        raise Invalid("Immediate acceptance is not available on this page yet.")
    if cfg is None:
        cfg = {}
    stay = intake.stay_mode(cfg)
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise Invalid("Choose distinct pending lines to accept.")
    selected = [v for v in card["lines"] if v["id"] in ids and v["status"] in PENDING]
    if len(selected) != len(ids):
        raise Invalid("A selected line is no longer pending on this card.")
    result = deepcopy(cfg)
    changes = []
    seen = set()
    for line in selected:
        name = line["name"]
        key, cleaner = {"service": ("service_types", services.clean_service_types),
                        "resource": ("resource_types", diary_config.clean_resource_types)}[name]
        label = _text(line["fields"].get("name"), 60)
        identity = (name, services._norm(label))
        if identity in seen:
            raise Invalid("This row appears more than once. Keep one version before applying.")
        seen.add(identity)
        rows = result.setdefault(key, [])
        if rows is None:
            rows = result[key] = []
        matches = [i for i, row in enumerate(rows) if services._norm(row.get("name")) == identity[1]]
        if len(matches) > 1:
            raise Invalid("Saved rows have duplicate names. Review them on the diary page.")
        index = matches[0] if matches else None
        before = deepcopy(rows[index]) if index is not None else None
        proposed = (_named(name, before) if before is not None else {}) | line["fields"]
        parsed = setup_rows.parse(card["page"], name, proposed)
        if parsed["problems"]:
            raise Invalid("; ".join(parsed["problems"]))
        fields = parsed["fields"]
        if name == "service" and "at_once" in fields and not stay:
            raise Invalid("At once belongs to appointments on a stay diary. Use the resource capacity on this diary.")
        merged = deepcopy(before or {})
        for field in line["fields"]:
            if field == "at_once":
                continue  # stored on the appointment's pool, not the service
            merged[field] = fields[field]
        # Only changed supported fields are written back. Validation may omit an
        # explicit clear (exact timing, unrestricted days); remove that key then.
        cleaned = cleaner([merged])[0]
        after = deepcopy(before or cleaned)
        for field in line["fields"]:
            if field == "at_once":
                continue
            if field in cleaned:
                after[field] = cleaned[field]
            else:
                after.pop(field, None)
        if before is not None:
            # A row name locates its saved identity; changing its spelling can
            # detach existing bookings whose pool references are exact strings.
            after["name"] = before["name"]
        if index is None:
            rows.append(after)
        else:
            rows[index] = after
        changes.append({"id": line["id"], "name": name, "before": before, "after": deepcopy(after)})
    if stay:
        from app import setup_card_stays
        setup_card_stays.bind(card, selected, cfg, result, changes)
    # Validate the resulting complete catalogue, including unselected saved rows.
    resources = [r["name"] for r in result.get("resource_types") or []]
    canonical = []
    for name, key in (("service", "service_types"), ("resource", "resource_types")):
        for row in result.get(key) or []:
            canonical.append({"name": name, "value": _named(name, row)})
    problems = [p for row in setup_rows.prepare(card["page"], canonical, saved={"resource": resources}) for p in row["problems"]]
    if problems:
        raise Invalid("; ".join(dict.fromkeys(problems)))
    keys = {"service_types" if v["name"] == "service" else "resource_types" for v in selected}
    if stay:
        keys.update(key for key in ("service_types", "resource_types") if result.get(key) != cfg.get(key))
    return {key: result[key] for key in keys}, changes


def _named(name, row):
    """Read existing domain encoding without inventing a new owner's choices."""
    fields = {k: v for k, v in row.items() if k in setup_rows.KEYS["diary_settings", name]}
    if name == "service":
        fields.setdefault("timing", "time")  # domain encoding omits exact timing
        if "days" in fields:
            fields["days"] = [setup_rows.DAYS[d] for d in fields["days"]]
        if fields.get("resource") is None:
            fields.pop("resource", None)
    elif fields.get("rate") is None:
        fields.pop("rate_unit", None)
    return fields


def accept(current, revision, turn, ids, cfg, *, actor=None):
    card = _base(current["page"], current, revision, turn)
    patch, changes = acceptance_patch(card, ids, cfg, actor=actor)
    for line in card["lines"]:
        if line["id"] in ids:
            line.update(status="applied", applied_at=card["updated_at"])
            applied = next(v["after"] for v in changes if v["id"] == line["id"])
            receipt = next(v for v in changes if v["id"] == line["id"])
            fields = receipt.get("fields") or setup_rows.parse(card["page"], line["name"], _named(line["name"], applied))["fields"]
            line.update(problems=[], display=setup_rows.describe(card["page"], line["name"], fields))
    card["last_action"] = {"kind": "accept", "changes": changes}
    return refresh(card), patch


def acceptance_patch(card, ids, snapshot, *, actor=None):
    if card['page'] == 'knowledge':
        from app import setup_card_knowledge
        return setup_card_knowledge.patch(card, ids, snapshot)
    if card["page"] == "crm_fields":
        from app import setup_card_fields
        return setup_card_fields.patch(card, ids, snapshot)
    if card["page"] == "crm_settings":
        from app import setup_card_crm
        return setup_card_crm.patch(card, ids, snapshot)
    if card["page"] in {"invoice_settings", "packages"}:
        from app import setup_card_invoices
        return setup_card_invoices.patch(card, ids, snapshot)
    if card["page"] == "diary_settings":
        if any(v["name"] not in {'service', 'resource'} and v["id"] in ids for v in card["lines"]):
            from app import setup_card_scalars, setup_card_service_rules
            if not ids or len(set(ids)) != len(ids):
                raise Invalid("Choose distinct pending lines.")
            selected = [v for v in card["lines"] if v["id"] in ids and v["status"] in PENDING]
            if len(selected) != len(ids):
                raise Invalid("A selected line is no longer pending.")
            rows = [v["id"] for v in selected if v["name"] in {'service', 'resource'}]
            patch, changes = diary_patch(card, rows, snapshot) if rows else ({}, [])
            scalar_rows = [v for v in selected if v['name'] == 'setting']
            if scalar_rows:
                scalar_patch, scalar_changes = setup_card_scalars.patch(scalar_rows, (snapshot or {}) | patch, actor)
                patch.update(scalar_patch)
                changes.extend(scalar_changes)
            rule_rows = [v for v in selected if v['name'] in setup_card_service_rules.FIELDS]
            if rule_rows:
                rule_patch, rule_changes = setup_card_service_rules.patch(rule_rows, (snapshot or {}) | patch)
                patch.update(rule_patch)
                changes.extend(rule_changes)
            return patch, changes
        return diary_patch(card, ids, snapshot)
    from app import setup_card_details
    return setup_card_details.patch(card, ids, snapshot)
