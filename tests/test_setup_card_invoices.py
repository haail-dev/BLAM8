from copy import deepcopy
import pytest
from app import setup_cards as cards
from tests.test_setup_cards import start, append


def apply(values, cfg=None, page='invoice_settings'):
    card = start(*(append('setting', key=k, value=v) for k, v in values.items()), page=page)
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], cfg or {})


def test_preserves_unselected_values_and_literal_prose():
    cfg = {'show_vat': False, 'payment_terms': 'Old', 'bank_account_name': 'Owner',
           'extras': {'packages': [{'name': 'Pass'}], 'future': {'a': 1}}}
    original = deepcopy(cfg)
    card, patch = apply({'payment_terms': 'Due on Receipt.\nSpecial Terms apply.', 'deposit_on_confirm': False}, cfg)
    assert patch == {'payment_terms': 'Due on Receipt.\nSpecial Terms apply.',
                     'extras': cfg['extras'] | {'deposit_on_confirm': False}}
    assert cfg == original and card['status'] == 'applied'


def test_first_row_requires_explicit_choices_and_no_vat_guess():
    with pytest.raises(cards.Invalid, match='required invoice'):
        apply({'footer_note': 'Reference'})
    choices = dict(show_vat=False, vat_rate=0, show_business_details=True,
                   show_payment_terms=False, payment_terms='', default_quote_valid_days=14)
    _, patch = apply(choices)
    assert patch == choices
    with pytest.raises(cards.Invalid, match='include VAT'):
        apply(choices | {'show_vat': True})
    _, patch = apply(choices | {'show_vat': True, 'prices_include_vat': False})
    assert patch['extras']['prices_include_vat'] is False


def test_deposit_trigger_is_explicit_and_null_clears_only_deposit():
    cfg = {'default_deposit_percent': None, 'extras': {'packages': []}}
    with pytest.raises(cards.Invalid, match='confirmation'):
        apply({'default_deposit_percent': 25}, cfg)
    _, patch = apply({'default_deposit_percent': 25, 'deposit_on_confirm': False}, cfg)
    assert patch['default_deposit_percent'] == 25 and patch['extras']['deposit_on_confirm'] is False
    _, patch = apply({'default_deposit_percent': None}, cfg)
    assert patch == {'default_deposit_percent': None}


def test_package_preferences_keep_zero_false_and_preserve_catalogue():
    cfg = {'extras': {'packages': [{'name': 'Pass', 'future': 3}], 'section_order': ['core'],
                      'package_prefs': {'portal': True, 'future': 2}}}
    with pytest.raises(cards.Invalid, match='all package'):
        apply({'package_low_at': 0}, cfg, 'packages')
    _, patch = apply({'package_low_at': 0, 'package_expiry_days': 0, 'packages_paid_only': False}, cfg, 'packages')
    assert patch['extras']['packages'] == cfg['extras']['packages']
    assert patch['extras']['package_prefs'] == {'portal': True, 'future': 2, 'paid_only': False, 'low_at': 0, 'expiry_days': 0}
    assert patch['extras']['section_order'] == ['core']


@pytest.mark.parametrize('key,value', [('vat_rate', -1), ('vat_rate', 20.001),
    ('show_vat', 'false'), ('default_deposit_percent', 0), ('default_quote_valid_days', 1.5),
    ('payment_terms', 'a' * 601), ('packages_paid_only', False)])
def test_bad_or_wrong_page_values_rejected(key, value):
    with pytest.raises(cards.Invalid):
        apply({key: value}, {'show_vat': False})
