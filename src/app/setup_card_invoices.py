"""Explicit invoice choices and package preferences, without form defaults."""
from copy import deepcopy
from app import setup_card_scalars

SPECS = {}


def add(key, kind, path=None, **kwargs):
    SPECS[key] = dict(type=kind, path=path or [key], label=key.replace('_', ' ').capitalize(),
                      nullable=False, choices=None, minimum=None, maximum=None) | kwargs


for key in ('show_vat', 'show_payment_terms', 'show_business_details'):
    add(key, 'boolean')
for key in ('deposit_on_confirm', 'prices_include_vat'):
    add(key, 'boolean', ['extras', key])
add('vat_rate', 'money', minimum=0, maximum=100)
add('default_deposit_percent', 'money', minimum=0.01, maximum=100, nullable=True)
add('default_quote_valid_days', 'number', minimum=1, maximum=365)
add('payment_terms', 'prose', maximum=600)
add('footer_note', 'prose', maximum=400)
for key, sub in (('packages_paid_only', 'paid_only'), ('packages_portal', 'portal')):
    add(key, 'boolean', ['extras', 'package_prefs', sub])
add('package_low_at', 'number', ['extras', 'package_prefs', 'low_at'], minimum=0, maximum=99)
add('package_expiry_days', 'number', ['extras', 'package_prefs', 'expiry_days'], minimum=0, maximum=365)
PACKAGE_KEYS = {k for k in SPECS if k.startswith('package')}
REQUIRED_NEW = {'show_vat', 'vat_rate', 'show_business_details', 'show_payment_terms',
                'payment_terms', 'default_quote_valid_days'}


def specs(page):
    return {k: v for k, v in SPECS.items() if (k in PACKAGE_KEYS) == (page == 'packages')}


def parse(page, fields):
    return setup_card_scalars.parse(fields, specs(page))


def read(row, key):
    value = row
    for part in SPECS[key]['path']:
        value = value.get(part) if isinstance(value, dict) else None
    return value


def describe(fields):
    value = fields['value']
    text = 'Not set' if value is None else 'Yes' if value is True else 'No' if value is False else str(value)
    return SPECS[fields['key']]['label'] + ': ' + (text or 'Empty')


def patch(card, ids, snapshot):
    from app.setup_cards import Invalid, PENDING
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise Invalid('Choose distinct pending lines.')
    selected = [v for v in card['lines'] if v['id'] in ids and v['status'] in PENDING]
    if len(selected) != len(ids):
        raise Invalid('A selected line is no longer pending.')
    before = {k: v for k, v in (snapshot or {}).items() if k != '_setup_scheduling'}
    result = deepcopy(before)
    changed, keys, receipts = set(), set(), []
    packages = [r for r in selected if r['name'] == 'package']
    if packages:
        from app import setup_card_packages
        package_patch, receipts = setup_card_packages.patch(packages, before, (snapshot or {}).get('_setup_scheduling'))
        result.update(package_patch)
        changed.update(package_patch)
    for line in selected:
        if line['name'] == 'package':
            continue
        try:
            fields = parse(card['page'], line['fields'])
        except (ValueError, TypeError) as exc:
            raise Invalid(str(exc)) from None
        key, value = fields['key'], fields['value']
        if key in keys:
            raise Invalid('Choose only one proposal for each setting.')
        keys.add(key)
        path = SPECS[key]['path']
        target = result
        for part in path[:-1]:
            if target.get(part) is None:
                target[part] = {}
            if not isinstance(target[part], dict):
                raise Invalid('Saved invoice settings could not be verified.')
            target = target[part]
        target[path[-1]] = value
        changed.add(path[0])
        receipts.append(dict(id=line['id'], name='setting', fields=fields,
                             before={'value': read(before, key)}, after={'value': value}))
    if not before and not REQUIRED_NEW <= keys:
        raise Invalid('First choose the required invoice settings: ' + ', '.join(sorted(REQUIRED_NEW - keys)) + '.')
    if result.get('show_vat') and keys & {'show_vat', 'vat_rate', 'prices_include_vat'}:
        if read(result, 'vat_rate') is None or type(read(result, 'prices_include_vat')) is not bool:
            raise Invalid('Choose the VAT rate and whether prices include VAT before enabling it.')
    if keys & {'default_deposit_percent', 'deposit_on_confirm'} and result.get('default_deposit_percent') is not None:
        if type(read(result, 'deposit_on_confirm')) is not bool:
            raise Invalid('Choose whether the deposit applies on booking confirmation.')
    if card['page'] == 'packages' and any(read(result, k) is None for k in PACKAGE_KEYS):
        raise Invalid('Choose all package preferences explicitly before accepting them.')
    return {k: result[k] for k in changed}, receipts
