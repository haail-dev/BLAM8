from copy import deepcopy
import pytest
from app import setup_cards as cards
from tests.test_setup_cards import append, start


def field(**kw):
    return dict(label='Reference', field_type='text', required=False, probe_behaviour='passive',
                durability='once', staff_can_see=False) | kw


def proposal(*fields):
    return start(*(append('contact_field', **f) for f in fields), page='crm_fields')


def apply(fields, saved=None):
    card = proposal(fields)
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], {'definitions': saved or []})


def saved_field(**kw):
    _, patch = apply(field())
    return patch['definitions'][0] | kw


def test_new_field_preserves_explicit_policy_and_preview_id():
    card = proposal(field())
    ids = [card['lines'][0]['id']]
    preview, _ = cards.acceptance_patch(card, ids, {'definitions': []})
    accepted, patch = cards.accept(card, 1, 'accept', ids, {'definitions': []})
    assert patch == preview
    row = patch['definitions'][0]
    assert row['key'] == 'reference' and row['entity'] == 'contact'
    assert row['staff_can_see'] is False and row['staff_can_see_set'] is True
    assert row['required'] is False and accepted['status'] == 'applied'


def test_partial_correction_preserves_saved_identity_and_visibility_choice():
    old = saved_field(key='original-key', staff_can_see_set=False, future={'a': 1})
    before = deepcopy(old)
    accepted, patch = apply({'label': 'reference', 'extraction_hint': 'Exact Reference\nfrom the owner'}, [old])
    row = patch['definitions'][0]
    assert row['id'] == old['id'] and row['key'] == 'original-key' and row['label'] == 'Reference'
    assert row['staff_can_see_set'] is False and old == before
    assert accepted['last_action']['changes'][0]['after']['future'] == {'a': 1}


def test_expiry_requires_owner_choices_and_exposes_effect_on_saved_dates():
    with pytest.raises(cards.Invalid, match='explicit recall'):
        apply(field(field_type='date', durability='expires'))
    _, patch = apply(field(field_type='date', durability='expires', recall_days=60, valid_months=24))
    old = patch['definitions'][0]
    card, patch = apply({'label': 'Reference', 'valid_months': 0}, [old])
    assert patch['definitions'][0]['valid_months'] == 0
    assert 'dates already saved' in card['last_action']['changes'][0]['notes'][0]


def test_dropdown_extensions_keep_old_values_but_removal_is_rejected():
    _, patch = apply(field(field_type='dropdown', options=['A', 'B']))
    old = patch['definitions'][0]
    _, patch = apply({'label': 'Reference', 'options': ['A', 'B', 'C']}, [old])
    assert patch['definitions'][0]['options'] == ['A', 'B', 'C']
    with pytest.raises(cards.Invalid, match='Removing an option'):
        apply({'label': 'Reference', 'options': ['B', 'C']}, [old])


@pytest.mark.parametrize('values', [field(required='false'), field(staff_can_see=None),
    field(probe_behaviour='unknown'), field(field_type='dropdown', options=['Only']),
    field(field_type='dropdown', options=['A', 'a']),
    field(field_type='date', durability='expires', recall_days=0, valid_months=12),
    field(field_type='date', durability='expires', recall_days=30, valid_months=601),
    field(options=['A', 'B']), field(recall_days=30), {'label': 'Reference', 'field_type': 'text'}])
def test_invalid_or_incomplete_choices_never_save(values):
    with pytest.raises(cards.Invalid):
        apply(values)


def test_key_collisions_limits_ladders_and_type_changes_are_blocked():
    old = saved_field()
    with pytest.raises(cards.Invalid, match='different meaning'):
        apply({'label': 'Reference', 'field_type': 'date'}, [old])
    with pytest.raises(cards.Invalid, match='data key'):
        apply(field(label='Reference!'), [old])
    with pytest.raises(cards.Invalid, match='ladder'):
        apply({'label': 'Reference', 'required': True}, [old | {'ladder': {'steps': []}}])
    with pytest.raises(cards.Invalid, match='limit'):
        apply(field(label='New'), [old | {'id': str(i), 'key': str(i), 'label': str(i)} for i in range(10)])
