"""Explicit CRM threshold and pipeline-stage proposals."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5
from app import setup_card_scalars

SPECS = {'quiet_days': dict(type='number', minimum=1, maximum=365, nullable=False,
                           label='Quiet threshold (days)')}
TYPES = ('active', 'won', 'lost', 'dormant')
FIELDS = {'name', 'stage_type', 'display_order'}


def parse(name, raw):
    if name == 'setting':
        return setup_card_scalars.parse(raw, SPECS)
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Supply named stage fields only.')
    fields = deepcopy(raw)
    if not isinstance(fields.get('name'), str) or not fields['name'].strip() or len(fields['name']) > 60:
        raise ValueError('Supply a stage name of at most 60 characters.')
    fields['name'] = fields['name'].strip()
    if 'stage_type' in fields and fields['stage_type'] not in TYPES:
        raise ValueError('Choose active, won, lost or dormant explicitly.')
    if 'display_order' in fields:
        fields['display_order'] = setup_card_scalars.parse(
            {'key': 'order', 'value': fields['display_order']},
            {'order': dict(type='number', minimum=-100000, maximum=100000, nullable=False)})['value']
    return fields


def describe(name, fields):
    if name == 'setting':
        return 'Quiet threshold: ' + str(fields['value']) + ' days'
    return fields['name'] + ''.join(' · ' + str(fields[k]) for k in ('stage_type', 'display_order') if k in fields)


def patch(card, ids, snapshot):
    from app.setup_cards import Invalid, PENDING
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise Invalid('Choose distinct pending lines.')
    selected = [r for r in card['lines'] if r['id'] in ids and r['status'] in PENDING]
    if len(selected) != len(ids):
        raise Invalid('A selected line is no longer pending.')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('stages'), list) or 'quiet_days' not in snapshot:
        raise Invalid('Saved CRM settings could not be verified.')
    saved = snapshot['stages']
    names = [str(s.get('name') or '').strip().lower() for s in saved]
    if any(not n for n in names) or len(names) != len(set(names)):
        raise Invalid('Saved stage names are ambiguous. Resolve them on the CRM page.')
    result, receipts, seen = {'stages': []}, [], set()
    for line in selected:
        try:
            fields = parse(line['name'], line['fields'])
        except (TypeError, ValueError) as exc:
            raise Invalid(str(exc)) from None
        if line['name'] == 'setting':
            identity = ('setting', fields['key'])
            result['crm_quiet_days'] = fields['value']
            receipt = dict(id=line['id'], name='setting', fields=fields,
                           before={'value': snapshot['quiet_days']}, after={'value': fields['value']})
        else:
            identity = ('stage', fields['name'].lower())
            before = next((s for s in saved if s['name'].strip().lower() == identity[1]), None)
            merged = {k: v for k, v in (before or {}).items() if k in FIELDS} | fields
            if not FIELDS <= set(merged):
                raise Invalid('Choose the stage type and order explicitly before adding it.')
            try:
                merged = parse('stage', merged)
            except (TypeError, ValueError) as exc:
                raise Invalid(str(exc)) from None
            if before and merged['stage_type'] != before.get('stage_type'):
                raise Invalid('Changing an existing stage type needs a separate review of its deals. Add a new stage instead.')
            if before:
                merged['name'] = before['name']
            after = merged | {'id': before['id'] if before else str(uuid5(NAMESPACE_URL, 'blam8:setup-stage:' + line['id']))}
            result['stages'].append(after)
            receipt = dict(id=line['id'], name='stage', fields=merged, before=deepcopy(before), after=after,
                           review_before={k: before[k] for k in FIELDS if k in before} if before else {},
                           review_after=merged)
        if identity in seen:
            raise Invalid('Choose one proposal for each CRM setting or stage.')
        seen.add(identity)
        receipts.append(receipt)
    return result, receipts
