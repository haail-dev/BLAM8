"""Pure acceptance plans for booking and subject details, including linked records."""
from copy import deepcopy
from uuid import UUID, uuid5

from app import setup_rows, intake, requirement_groups as groups
from app.setup_cards import Invalid, PENDING


def named(page, name, row, kind=None):
    if page == "booking_details":
        return {"label": row["label"], "required": row.get("required", False),
                "kind": "document" if row.get("doc") else "answer",
                "hint": row.get("hint", ""), "services": row.get("services", []),
                "remember": row.get("remember", "ask")}
    if name == "group":
        return {"label": row["label"], "plural": row.get("label_plural", ""),
                "cardinality": row.get("cardinality", "many")}
    fields = {k: deepcopy(v) for k, v in row.items() if k in setup_rows.KEYS[page, name]}
    fields.update(kind=kind or row.get("kind"), type=row.get("type", "text"),
                  required=row.get("required", False), durability=row.get("durability", "once"),
                  gate=row.get("gate", False))
    if fields["durability"] != "expires":
        fields.pop("recall_days", None)
    return fields


def _parse(page, name, fields):
    row = setup_rows.parse(page, name, fields)
    if row["problems"]:
        raise Invalid("; ".join(row["problems"]))
    return row["fields"]


def _match(rows, label):
    matches = [r for r in rows if groups._norm(r["label"]) == groups._norm(label)]
    if len(matches) > 1:
        raise Invalid("Saved rows have duplicate labels. Review them on their settings page.")
    return matches[0] if matches else None


def _receipt(line, before, after, fields, page):
    return {"id": line["id"], "name": line["name"], "before": deepcopy(before),
            "after": deepcopy(after), "fields": fields,
            "review_before": named(page, line["name"], before, fields.get("kind")) if before else {},
            "review_after": deepcopy(fields)}


def patch(card, ids, snapshot):
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise Invalid("Choose distinct pending lines to accept.")
    selected = [v for v in card["lines"] if v["id"] in ids and v["status"] in PENDING]
    if len(selected) != len(ids):
        raise Invalid("A selected line is no longer pending on this card.")
    if card["page"] == "booking_details":
        modes = [r for r in selected if r['name'] == 'setting']
        if modes:
            from app import setup_card_mode
            if len(modes) != 1:
                raise Invalid('Choose one booking-mode proposal.')
            mode_patch, receipt = setup_card_mode.prepare(modes[0], snapshot)
            patch, changes = _intake([r for r in selected if r['name'] != 'setting'],
                                     snapshot | {'config': snapshot['config'] | mode_patch})
            if mode_patch['booking_mode'] == 'appointment' and any(intake._norm(s) == intake._norm(intake.STAYS_SCOPE) for r in patch['intake_fields'] for s in (r.get('services') or [])):
                raise Invalid('Remove Stays-only field scopes in the same acceptance before switching to appointments.')
            return patch | {'booking_mode': mode_patch['booking_mode']}, [receipt] + changes
        return _intake(selected, snapshot)
    ladder_lines = [r for r in selected if r['name'] == 'ladder']
    if ladder_lines:
        from app import setup_card_ladders
        ordinary, receipts = _groups([r for r in selected if r['name'] != 'ladder'], snapshot, allow_empty=True)
        combined = {g['id']: deepcopy(g) for g in snapshot['groups']}
        combined.update({g['id']: g for g in ordinary['groups']})
        progress, changes = setup_card_ladders.patch(ladder_lines, snapshot, list(combined.values()))
        writes = {g['id']: g for g in ordinary['groups']}
        writes.update({g['id']: g for g in progress['groups']})
        if any(not 1 <= len(g['fields']) <= groups.MAX_FIELDS for g in writes.values()):
            raise Invalid('Each subject kind needs one to ten fields.')
        for receipt in receipts:
            if receipt['name'] == 'group': receipt['after'] = deepcopy(writes[receipt['after']['id']])
        return {'groups': list(writes.values()), 'definitions': progress['definitions']}, receipts + changes
    return _groups(selected, snapshot)


def _intake(selected, snapshot):
    from app import crm_fields, crm_visibility, intake_memory
    cfg = snapshot["config"]
    rows = deepcopy(cfg.get("intake_fields") or [])
    definitions = deepcopy(snapshot["definitions"])
    added, changes, seen = [], [], set()
    for line in selected:
        notes = []
        supplied = line["fields"]
        label = supplied.get("label")
        if not isinstance(label, str) or not label.strip():
            raise Invalid("Supply a booking detail label.")
        if intake._is_stay_label(label):
            raise Invalid("Change the stay mode on the booking details page.")
        identity = intake._norm(label)
        if identity in seen:
            raise Invalid("This booking detail appears more than once.")
        seen.add(identity)
        before = _match(rows, label)
        fields = _parse("booking_details", "field", (named("booking_details", "field", before) if before else {}) | supplied)
        services = {intake._norm(s["name"]): s["name"] for s in cfg.get("service_types") or []}
        if intake.stay_mode(cfg):
            services[intake._norm(intake.STAYS_SCOPE)] = intake.STAYS_SCOPE
        if any(intake._norm(s) not in services for s in fields["services"]):
            raise Invalid("Save the referenced services before accepting this booking detail.")
        after = deepcopy(before or {})
        for key in supplied if before else fields:
            target = "doc" if key == "kind" else key
            after[target] = fields[key] == "document" if key == "kind" else deepcopy(fields[key])
        if after.get("remember", "ask") == "ask":
            after.pop("remember", None)
            after.pop("crm_key", None)
        else:
            key = after.get("crm_key") or crm_fields.slug(fields["label"])
            matches = [d for d in definitions if d["key"] == key]
            if len(matches) > 1:
                raise Invalid("The remembered field has ambiguous customer definitions.")
            definition = matches[0] if matches else None
            if definition:
                if not after.get("crm_key") and intake._norm(definition.get("label")) != intake._norm(label):
                    raise Invalid("This label overlaps a different customer field. Review its saved link first.")
                if definition.get("ladder") or (fields["remember"] == "expires" and
                        (definition.get("field_type") != "date" or definition.get("durability") != "expires")):
                    raise Invalid("Review the linked customer field before changing its memory rules.")
            else:
                if after.get("crm_key"):
                    raise Invalid("The linked customer field is missing. Review it on the customer fields page.")
                if len(definitions) >= crm_fields.MAX_FIELDS_PER_ENTITY:
                    raise Invalid("The customer field limit has been reached.")
                if any(d.get("ladder") for d in definitions) and crm_fields._LEVEL_WORDS.search(label):
                    raise Invalid("Use the existing customer progress ladder for this detail.")
                try:
                    definition = intake_memory.definition_for(after)
                except crm_fields.FieldError as exc:
                    raise Invalid(str(exc)) from None
                definition.update(display_order=len(definitions) * 10,
                                  staff_can_see=crm_visibility.default_staff_can_see(definition))
                definitions.append(definition)
                added.append(definition)
                notes.append("Also creates customer field: " + definition["label"] + " (" + definition["field_type"] + ").")
                if definition["durability"] == "expires":
                    notes.append("Customer field page defaults: the date is its expiry, with a reminder "
                                 + str(definition["recall_days"]) + " days before. Edit these on the customer fields page.")
            after["crm_key"] = key
        if before is None:
            rows.append(after)
        else:
            rows[rows.index(before)] = after
        changes.append(_receipt(line, before, after, named("booking_details", "field", after), "booking_details") | {"notes": notes})
    if len(rows) > intake.MAX_FIELDS:
        raise Invalid("There can be at most twenty booking details, including the stay field.")
    return {"intake_fields": rows, "definitions": added}, changes


def _groups(selected, snapshot, allow_empty=False):
    rows = deepcopy(snapshot["groups"])
    changes, touched, seen = [], set(), set()
    # A batch can introduce a kind and its fields in either proposal order.
    for line in sorted(selected, key=lambda v: v["name"] != "group"):
        supplied = line["fields"]
        name = line["name"]
        label = supplied.get("label")
        if not isinstance(label, str) or not label.strip():
            raise Invalid("Supply a subject detail or kind label.")
        identity = (name, groups._norm(supplied.get("kind", "")), groups._norm(label))
        if identity in seen:
            raise Invalid("This subject row appears more than once.")
        seen.add(identity)
        if name == "group":
            before = _match(rows, label)
            fields = _parse("dependants", name, (named("dependants", name, before) if before else {}) | supplied)
            after = deepcopy(before or {"id": str(uuid5(UUID(line["id"]), "group")),
                                       "fields": [], "display_order": len(rows)})
            after.update(label=fields["label"], label_plural=fields["plural"], cardinality=fields["cardinality"])
            if before is None:
                rows.append(after)
            else:
                rows[rows.index(before)] = after
            touched.add(after["id"])
            changes.append(_receipt(line, before, after, fields, "dependants"))
        else:
            group = _match(rows, supplied.get("kind", ""))
            if group is None:
                raise Invalid("Accept the subject kind together with its details, or save the kind first.")
            before = _match(group["fields"], label)
            fields = _parse("dependants", name, (named("dependants", name, before, group["label"]) if before else {}) | supplied)
            if before and (fields["type"] != before.get("type", "text") or
                    (fields["type"] == "dropdown" and not set(before.get("options") or []).issubset(fields["options"]))):
                raise Invalid("Review existing subject answers on the subject page before changing their type or removing options.")
            if before and before.get("ladder") and any(fields.get(k) != named("dependants", name, before, group["label"]).get(k)
                    for k in ("type", "options", "durability", "gate")):
                raise Invalid("Edit progress options and rules in the Progress controls on the subject page.")
            after = deepcopy(before or {"key": groups.key_for(label), "proof": False, "set_by": "customer"})
            for key in supplied if before else fields:
                if key != "kind":
                    after[key] = deepcopy(fields[key])
            if before and before.get("proof") and not after.get("gate"):
                raise Invalid("Review the proof requirement on the subject page before removing this gate.")
            if before is None:
                if any(f.get("key") == after["key"] for f in group["fields"]):
                    raise Invalid("This label would reuse another subject field's stored key. Choose a distinct label.")
                group["fields"].append(after)
            else:
                group["fields"][group["fields"].index(before)] = after
            touched.add(group["id"])
            changes.append(_receipt(line, before, after, named("dependants", name, after, group["label"]), "dependants"))
    if len(rows) > groups.MAX_GROUPS or any(not (0 if allow_empty else 1) <= len(g["fields"]) <= groups.MAX_FIELDS for g in rows):
        raise Invalid("Use at most three subject kinds, each with one to ten details. Accept a new kind with its details.")
    for change in changes:
        if change["name"] == "group":
            change["after"] = deepcopy(next(g for g in rows if g["id"] == change["after"]["id"]))
    return {"groups": [g for g in rows if g["id"] in touched]}, changes
