"""Customer field proposals without re-keying existing customer data."""
from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL
from app import crm_fields, setup_card_scalars

FIELDS = {'label', 'field_type', 'options', 'required', 'extraction_hint', 'probe_behaviour',
          'durability', 'recall_days', 'valid_months', 'staff_can_see'}
REQUIRED = {'label', 'field_type', 'required', 'probe_behaviour', 'durability', 'staff_can_see'}


def parse(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Supply named customer field choices only.')
    fields = deepcopy(raw)
    if not isinstance(fields.get('label'), str) or not fields['label'].strip() or len(fields['label']) > 100 or not crm_fields.slug(fields['label']):
        raise ValueError('Supply a field label with letters or numbers, at most 100 characters.')
    fields['label'] = fields['label'].strip()
    for key, choices in (('field_type', crm_fields.FIELD_TYPES), ('probe_behaviour', crm_fields.PROBE_BEHAVIOURS), ('durability', crm_fields.DURABILITY)):
        if key in fields and fields[key] not in choices:
            raise ValueError('Choose a supported ' + key.replace('_', ' ') + '.')
    for key in ('required', 'staff_can_see'):
        if key in fields and type(fields[key]) is not bool:
            raise ValueError('Choose ' + key.replace('_', ' ') + ' explicitly.')
    for key, lo, hi in (('recall_days', 1, 365), ('valid_months', 0, 600)):
        if key in fields:
            fields[key] = setup_card_scalars.parse({'key': key, 'value': fields[key]},
                {key: dict(type='number', minimum=lo, maximum=hi, nullable=False)})['value']
    if 'options' in fields:
        opts = fields['options']
        if not isinstance(opts, list) or len(opts) > 100 or any(not isinstance(o, str) or not o.strip() or len(o) > 100 for o in opts):
            raise ValueError('Supply a bounded list of non-empty dropdown options.')
        fields['options'] = [o.strip() for o in opts]
        if len({o.lower() for o in fields['options']}) != len(opts):
            raise ValueError('List each dropdown option once.')
    if 'extraction_hint' in fields:
        hint = fields['extraction_hint']
        if not isinstance(hint, str) or len(hint) > 1000:
            raise ValueError('Supply hint text of at most 1000 characters.')
        fields['extraction_hint'] = hint.strip()
    return fields


def named(row):
    return {k: ('' if k == 'extraction_hint' and v is None else deepcopy(v)) for k, v in row.items() if k in FIELDS}


def describe(fields):
    return fields['label'] + ''.join(' · ' + str(fields[k]) for k in ('field_type', 'durability') if k in fields)


def patch(card, ids, snapshot):
    from app.setup_cards import Invalid, PENDING
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise Invalid('Choose distinct pending lines.')
    selected = [r for r in card['lines'] if r['id'] in ids and r['status'] in PENDING]
    if len(selected) != len(ids):
        raise Invalid('A selected line is no longer pending.')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('definitions'), list):
        raise Invalid('Customer field definitions could not be verified.')
    saved = deepcopy(snapshot['definitions'])
    changes, writes, seen = [], [], set()
    for line in selected:
        try:
            supplied = parse(line['fields'])
        except (TypeError, ValueError) as exc:
            raise Invalid(str(exc)) from None
        identity = supplied['label'].lower()
        matches = [d for d in saved if d['label'].strip().lower() == identity]
        if len(matches) > 1 or identity in seen:
            raise Invalid('Choose one unambiguous customer field label.')
        seen.add(identity)
        before = matches[0] if matches else None
        if before and before.get('ladder'):
            raise Invalid('Edit progress through its ladder, not a customer field proposal.')
        fields = (named(before) if before else {}) | supplied
        if not REQUIRED <= set(fields):
            raise Invalid('Choose the missing customer field rules: ' + ', '.join(sorted(REQUIRED - set(fields))) + '.')
        try:
            fields = parse(fields)
        except (TypeError, ValueError) as exc:
            raise Invalid(str(exc)) from None
        if before and (fields['field_type'] != before['field_type'] or fields['durability'] != before['durability']):
            raise Invalid('An existing field keeps its type and durability. Use a separate field for a different meaning.')
        if fields['durability'] == 'expires':
            if fields['field_type'] != 'date' or not {'recall_days', 'valid_months'} <= set(fields):
                raise Invalid('Expiring dates need explicit recall days and validity months; zero months means the stored date is its expiry.')
        elif any(k in supplied for k in ('recall_days', 'valid_months')) and (supplied.get('valid_months', 0) != 0 or 'recall_days' in supplied):
            raise Invalid('Recall and validity rules belong to an expiring date field.')
        if fields['field_type'] == 'dropdown':
            if len(fields.get('options', [])) < 2:
                raise Invalid('A dropdown needs at least two explicit options.')
            if before and not set(before.get('options') or []) <= set(fields['options']):
                raise Invalid('Removing an option needs a separate migration of saved customer values.')
        elif fields.get('options'):
            raise Invalid('Only dropdown fields have options.')
        key = before['key'] if before else crm_fields.slug(fields['label'])
        if not before:
            if len(saved) >= crm_fields.MAX_FIELDS_PER_ENTITY:
                raise Invalid('The customer field limit has been reached.')
            if any(d['key'] == key for d in saved):
                raise Invalid('That label would reuse another customer data key.')
            if any(d.get('ladder') for d in saved) and crm_fields._LEVEL_WORDS.search(fields['label']):
                raise Invalid('Use the existing progress ladder for this detail.')
        # Use the established domain encoding. Recall is dormant for once fields;
        # activating expiry requires a separate field with explicit expiry rules.
        definition = crm_fields.validate_definition('contact', fields['label'], fields['field_type'],
            fields.get('options', []), fields.get('extraction_hint', ''), fields['required'], fields['durability'],
            fields.get('recall_days'), fields.get('valid_months'), fields['probe_behaviour'])
        after = deepcopy(before or {}) | definition | {'key': key, 'staff_can_see': fields['staff_can_see']}
        if before:
            after = deepcopy(before) | {k: after[k] for k in supplied if k != 'label'}
        else:
            after.update(id=str(uuid5(NAMESPACE_URL, 'blam8:setup-field:' + line['id'])), display_order=len(saved) * 10)
        if 'staff_can_see' in supplied or not before:
            after['staff_can_see_set'] = True
        # SQL receives only writable columns; saved unrelated attributes stay put.
        writes.append({k: deepcopy(after[k]) for k in FIELDS | {'id', 'key', 'entity', 'display_order', 'staff_can_see_set'} if k in after})
        notes = []
        if after['durability'] == 'once':
            notes.append('Recall and validity are inactive because this field does not expire.')
        if before and any(after.get(k) != before.get(k) for k in ('recall_days', 'valid_months')):
            notes.append('These expiry rules also apply to dates already saved on customer records.')
        if crm_fields.is_special_category(after):
            notes.append('This field remains manual-entry under the existing customer-field rules.')
        changes.append(dict(id=line['id'], name='contact_field', fields=named(after), before=before,
                            after=deepcopy(after), review_before=named(before) if before else {},
                            review_after=named(after), notes=notes))
        if before:
            saved[saved.index(before)] = after
        else:
            saved.append(after)
    return {'definitions': writes}, changes
