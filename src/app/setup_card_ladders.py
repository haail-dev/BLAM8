"""Progress definitions without moving or reinterpreting recorded levels."""
from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL
from app import ladders, requirement_groups as rg, crm_fields

FIELDS = {'who', 'label', 'levels', 'notify', 'gates', 'staff_can_see'}


def pending(card):
    return any(r['name'] == 'ladder' and r['status'] in {'proposed', 'held'} for r in (card or {}).get('lines', []))


def parse(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Supply named progression choices.')
    out = deepcopy(raw)
    for key in ('who', 'label'):
        if not isinstance(out.get(key), str) or not out[key].strip() or len(out[key]) > 60:
            raise ValueError('Supply a progression participant and label within sixty characters.')
        out[key] = ' '.join(out[key].split())
    if not crm_fields.slug(out['label']):
        raise ValueError('The progression label needs letters or numbers.')
    if 'levels' in out:
        if not isinstance(out['levels'], list) or not 2 <= len(out['levels']) <= ladders.MAX_LEVELS:
            raise ValueError('Supply two to twenty ordered levels.')
        if any(not isinstance(x, str) for x in out['levels']) or ladders.clean_levels(out['levels']) != out['levels']:
            raise ValueError('Levels must be distinct, non-empty text within sixty characters, without extra spacing.')
        if any(not rg._norm(x) for x in out['levels']) or len({rg._norm(x) for x in out['levels']}) != len(out['levels']):
            raise ValueError('Levels must remain distinct under the progress matching rules.')
    if 'notify' in out and out['notify'] not in ladders.NOTIFY:
        raise ValueError('Choose approve, auto or off for level-change messages.')
    if 'staff_can_see' in out and type(out['staff_can_see']) is not bool:
        raise ValueError('Choose staff visibility explicitly.')
    if 'gates' in out and (not isinstance(out['gates'], list) or len(out['gates']) > 20 or any(not isinstance(x, str) or len(x) > 123 for x in out['gates'])):
        raise ValueError('Supply service prerequisites as Service = Level lines, or an empty list.')
    return out


def named(who, row):
    result = {'who': who, 'label': row['label'], 'levels': deepcopy(row['options']),
              'notify': row['ladder']['notify'],
              'gates': [g['service'] + ' = ' + g['min'] for g in row['ladder']['gates']]}
    if who == 'contact': result['staff_can_see'] = row['staff_can_see']
    return result


def patch(selected, snapshot, groups):
    from app.setup_cards import Invalid
    groups = deepcopy(groups)
    definitions = deepcopy(snapshot['definitions'])
    changed, writes, receipts, seen = set(), [], [], set()
    for line in selected:
        try: supplied = parse(line['fields'])
        except ValueError as exc: raise Invalid(str(exc)) from None
        who = supplied['who']
        matches = [g for g in groups if rg._norm(g['label']) == rg._norm(who)] if who != 'contact' else []
        if who != 'contact' and len(matches) != 1:
            raise Invalid('Choose one saved subject kind, or contact for the customer.')
        group = matches[0] if matches else None
        rows = group['fields'] if group else definitions
        matched = [r for r in rows if rg._norm(r['label']) == rg._norm(supplied['label'])]
        identity = (rg._norm(who), rg._norm(supplied['label']))
        if len(matched) > 1 or identity in seen: raise Invalid('Choose one unambiguous progression.')
        seen.add(identity)
        before = matched[0] if matched else None
        if before and not isinstance(before.get('ladder'), dict):
            raise Invalid('Converting a saved field to progress requires a separate migration.')
        if before and before.get('type' if group else 'field_type') != 'dropdown':
            raise Invalid('The saved progression type could not be verified.')
        fields = (named(who, before) if before else {}) | supplied
        if not {'levels', 'notify', 'gates'} <= fields.keys() or (who == 'contact' and 'staff_can_see' not in fields):
            raise Invalid('Choose levels, notification, service prerequisites and customer-field visibility explicitly.')
        if who != 'contact' and 'staff_can_see' in fields:
            raise Invalid('Customer-field visibility does not apply to subject progress.')
        try: fields = parse(fields)
        except ValueError as exc: raise Invalid(str(exc)) from None
        if before and fields['levels'][:len(before['options'])] != before['options']:
            raise Invalid('Keep existing levels in their saved order. Renaming, removing or inserting levels requires migration.')
        gates, services = [], [s['name'] for s in snapshot['config'].get('service_types') or []]
        for entry in fields['gates']:
            service, sep, level = entry.partition('=')
            service, level = service.strip(), level.strip()
            if not sep or services.count(service) != 1 or level not in fields['levels'] or any(g['service'] == service for g in gates):
                raise Invalid('Each prerequisite must name one saved service and one exact level, without duplicates.')
            gates.append({'service': service, 'min': level})
        block = {'notify': fields['notify'], 'gates': gates}
        # The domain matcher includes partial service names; reject unintended
        # matches rather than silently gating a second configured service.
        for gate in gates:
            if any(s != gate['service'] and ladders._gate_for({'ladder': {'gates': [gate]}}, s) for s in services):
                raise Invalid('This prerequisite also matches another service name. Resolve the ambiguous service names first.')
        after = deepcopy(before or {})
        if not before:
            key = rg.key_for(fields['label'])
            if any(r['key'] == key for r in rows): raise Invalid('That label would reuse a saved data key.')
            if len(rows) >= (rg.MAX_FIELDS if group else crm_fields.MAX_FIELDS_PER_ENTITY):
                raise Invalid('The field limit has been reached.')
            after = {'key': key, 'label': fields['label'], 'required': False}
            if group:
                after.update(type='dropdown', durability='confirm', gate=False,
                             hint='The level on record — moved by the business, read by the customer.')
            else:
                after.update(id=str(uuid5(NAMESPACE_URL, 'blam8:setup-ladder:' + line['id'])), entity='contact',
                             field_type='dropdown', durability='once', probe_behaviour='passive',
                             recall_days=30, valid_months=0, display_order=len(rows)*10)
        after.update(options=fields['levels'], ladder=block)
        if group:
            changed.add(group['id'])
        else:
            after.update(staff_can_see=fields['staff_can_see'])
            if not before or 'staff_can_see' in supplied: after['staff_can_see_set'] = True
            writes.append(after)
        if before: rows[rows.index(before)] = after
        else: rows.append(after)
        receipts.append({'id': line['id'], 'name': 'ladder', 'fields': named(who, after),
            'before': before, 'after': deepcopy(after), 'review_before': named(who, before) if before else {},
            'review_after': named(who, after), 'notes': [
                'Recorded progress is preserved. No level-change message is sent by this acceptance.',
                'Service prerequisites are booking gates and apply to levels already on record.']})
    count = sum(isinstance(f.get('ladder'), dict) for g in groups for f in g['fields']) + sum(isinstance(d.get('ladder'), dict) for d in definitions)
    if count > ladders.MAX_LADDERS: raise Invalid('Use at most three progress ladders across customers and subjects.')
    return {'groups': [g for g in groups if g['id'] in changed], 'definitions': writes}, receipts
