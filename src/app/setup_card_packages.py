"""Lossless package catalogue proposals; existing wallet identities stay stable."""
from copy import deepcopy
from app import setup_card_scalars, wallet, intake

FIELDS = {'name', 'quantity', 'unit', 'price', 'valid_days', 'services', 'required'}
NUMBERS = {
    'quantity': dict(type='money', minimum=0.01, maximum=1000000, nullable=False),
    'price': dict(type='money', minimum=0, maximum=1000000, nullable=False),
    'valid_days': dict(type='number', minimum=0, maximum=3650, nullable=False),
}


def pending(card):
    return any(r.get('name') == 'package' and r.get('status') in {'proposed', 'held'}
               for r in (card or {}).get('lines', []))


def parse(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Supply named package fields only.')
    fields = deepcopy(raw)
    if not isinstance(fields.get('name'), str) or not wallet._norm(fields['name']) or len(fields['name']) > 80:
        raise ValueError('Supply a package name of at most 80 characters.')
    fields['name'] = ' '.join(fields['name'].split())
    for key, value in list(fields.items()):
        if key in NUMBERS:
            fields[key] = setup_card_scalars.parse({'key': key, 'value': value}, NUMBERS)['value']
        elif key == 'unit' and value not in wallet.UNITS:
            raise ValueError('Choose session or hour explicitly.')
        elif key == 'required' and type(value) is not bool:
            raise ValueError('Choose whether a package is required explicitly.')
        elif key == 'services':
            if not isinstance(value, list) or len(value) > 20 or any(not isinstance(s, str) or not s.strip() or len(s) > 60 for s in value):
                raise ValueError('Supply at most twenty named services, or an explicit empty list for all services.')
            if len({wallet._norm(s) for s in value}) != len(value):
                raise ValueError('List each service once.')
    return fields


def describe(fields):
    parts = [fields['name']]
    for key in ('quantity', 'unit', 'price', 'valid_days', 'services', 'required'):
        if key in fields:
            value = fields[key]
            text = ', '.join(value) or 'All services' if isinstance(value, list) else 'Yes' if value is True else 'No' if value is False else str(value)
            parts.append(key.replace('_', ' ') + ': ' + text)
    return ' · '.join(parts)


def patch(lines, invoice, cfg):
    from app.setup_cards import Invalid
    if not invoice:
        raise Invalid('Choose the required invoice settings before adding packages.')
    extras = deepcopy(invoice.get('extras') or {})
    rows = deepcopy(extras.get('packages') or [])
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise Invalid('Saved packages could not be verified.')
    names, keys = set(), set()
    for row in rows:
        name, key = wallet._norm(row.get('name')), str(row.get('key') or '').strip() or wallet.key_for(row.get('name'))
        if not name or name in names or key in keys:
            raise Invalid('Saved package names or keys are ambiguous. Resolve them on the packages page.')
        names.add(name); keys.add(key)
    services = ([intake.STAYS_SCOPE] if intake.stay_mode(cfg) else []) + [s['name'] for s in (cfg or {}).get('service_types') or []]
    seen, receipts = set(), []
    for line in lines:
        try:
            proposal = parse(line['fields'])
        except (ValueError, TypeError) as exc:
            raise Invalid(str(exc)) from None
        identity = wallet._norm(proposal['name'])
        if identity in seen:
            raise Invalid('Choose one proposal for each package.')
        seen.add(identity)
        index = next((i for i, r in enumerate(rows) if wallet._norm(r.get('name')) == identity), None)
        before = deepcopy(rows[index]) if index is not None else None
        merged = {k: v for k, v in (before or {}).items() if k in FIELDS} | proposal
        if not FIELDS <= set(merged):
            raise Invalid('Choose the missing package fields: ' + ', '.join(sorted(FIELDS - set(merged))) + '.')
        try:
            fields = parse(merged)
        except (ValueError, TypeError) as exc:
            raise Invalid(str(exc)) from None
        resolved = []
        for name in fields['services']:
            matches = [s for s in services if wallet._norm(s) == wallet._norm(name)]
            if len(matches) != 1:
                raise Invalid('Choose one verified saved service for each package link.')
            resolved.append(matches[0])
        fields['services'] = resolved
        if before and before.get('unit') != fields['unit']:
            raise Invalid('An existing package keeps its credit unit. Create a separate package for a different unit.')
        key = (before or {}).get('key') or wallet.key_for((before or fields)['name'])
        if before is None and key in keys:
            raise Invalid('The new package would reuse an existing wallet key. Choose a different name.')
        after = deepcopy(before or {}) | fields | {'key': key}
        if before:
            after['name'] = before['name']
        if index is None:
            rows.append(after); keys.add(key)
        else:
            rows[index] = after
        review_fields = {k: after[k] for k in FIELDS}
        receipt = dict(id=line['id'], name='package', fields=review_fields, before=before,
                       after=deepcopy(after), review_before={k: before[k] for k in FIELDS if k in before} if before else {},
                       review_after=review_fields)
        if before and before.get('services') != after['services']:
            receipt['notes'] = ['Changing service coverage also changes which services existing balances for this package can cover.']
        receipts.append(receipt)
    if len(rows) > wallet.MAX_PACKAGES:
        raise Invalid('This page can hold at most twelve packages. No rows were saved.')
    extras['packages'] = rows
    return {'extras': extras}, receipts
