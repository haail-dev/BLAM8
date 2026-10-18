from copy import deepcopy
import pytest
from app import setup_cards as cards, wallet
from tests.test_setup_cards import append, start


PREFS = dict(paid_only=False, low_at=0, expiry_days=0, portal=True)


def package(**kw):
    return dict(name='Pass', quantity=5, unit='session', price=40, valid_days=30, services=[], required=False) | kw


def cfg(rows=None, services=None):
    return {'extras': {'packages': rows or [], 'package_prefs': deepcopy(PREFS), 'future': 7},
            '_setup_scheduling': {'service_types': services or []}}


def apply(rows, snapshot):
    card = start(*(append('package', **r) for r in rows), page='packages')
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], snapshot)


def test_new_package_and_explicit_zero_values_are_lossless():
    before = cfg()
    card, patch = apply([package(price=0, valid_days=0)], before)
    assert patch['extras']['packages'] == [package(price=0, valid_days=0) | {'key': 'pass'}]
    assert patch['extras']['future'] == 7 and before['extras']['packages'] == []
    assert card['status'] == 'applied'


def test_partial_edit_preserves_identity_unknown_attributes_and_scope():
    old = package(services=['Review']) | {'key': 'historical-key', 'future': {'a': 1}}
    _, patch = apply([{'name': 'pass', 'price': 50}], cfg([old], [{'name': 'Review'}]))
    assert patch['extras']['packages'] == [old | {'price': 50}]


def test_service_scope_change_receipt_warns_about_existing_wallet_balances():
    old = package(services=['Review']) | {'key': 'historical-key'}
    card, patch = apply([{'name': 'Pass', 'services': []}], cfg([old], [{'name': 'Review'}]))
    changed = patch['extras']['packages'][0]
    assert not wallet.covers(old, 'Consultation') and wallet.covers(changed, 'Consultation')
    assert 'existing balances' in card['last_action']['changes'][0]['notes'][0]


def test_package_and_preferences_can_be_accepted_in_one_patch():
    before = {'extras': {'packages': [], 'future': 7}}
    card = start(append('package', **package()),
                 *(append('setting', key=k, value=v) for k, v in dict(packages_paid_only=False,
                   package_low_at=0, package_expiry_days=0, packages_portal=True).items()), page='packages')
    _, patch = cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], before)
    assert patch['extras']['package_prefs'] == PREFS
    assert len(patch['extras']['packages']) == 1 and patch['extras']['future'] == 7


@pytest.mark.parametrize('row', [package(quantity=0), package(quantity=2.123), package(price=-1),
    package(unit='day'), package(required='false'), package(valid_days=3651), package(services=['Missing']),
    package(services=['Review', 'review']), package(name='!!!')])
def test_invalid_fields_or_missing_service_never_save(row):
    with pytest.raises(cards.Invalid):
        apply([row], cfg())


def test_missing_choices_collisions_unit_changes_and_capacity_are_rejected():
    with pytest.raises(cards.Invalid, match='missing package'):
        apply([{'name': 'New'}], cfg())
    old = package() | {'key': 'new'}
    with pytest.raises(cards.Invalid, match='wallet key'):
        apply([package(name='New')], cfg([old]))
    with pytest.raises(cards.Invalid, match='credit unit'):
        apply([{'name': 'Pass', 'unit': 'hour'}], cfg([old]))
    rows = [package(name=f'Pass {i}') | {'key': f'p{i}'} for i in range(12)]
    with pytest.raises(cards.Invalid, match='twelve'):
        apply([package()], cfg(rows))
    with pytest.raises(cards.Invalid, match='invoice settings'):
        apply([package()], {})


def test_ambiguous_saved_names_or_keys_block_merging():
    with pytest.raises(cards.Invalid, match='ambiguous'):
        apply([{'name': 'Pass', 'price': 50}], cfg([package() | {'key': 'same'}, package(name='Other') | {'key': 'same'}]))


def test_service_names_resolve_to_saved_spelling_and_stays_need_stay_mode():
    _, patch = apply([package(services=['review'])], cfg(services=[{'name': 'Review'}]))
    assert patch['extras']['packages'][0]['services'] == ['Review']
    before = cfg(); before['_setup_scheduling']['booking_mode'] = 'stay'
    _, patch = apply([package(services=['Stays'])], before)
    assert patch['extras']['packages'][0]['services'] == ['Stays']
