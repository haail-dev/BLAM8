from copy import deepcopy
import pytest
from app import setup_cards as cards, setup_card_ladders as progress
from tests.test_setup_cards import start, append


def row(who='contact', **kw):
    fields = dict(who=who, label='Progress', levels=['Stage 1', 'Stage 2'], notify='approve', gates=[])
    if who == 'contact': fields['staff_can_see'] = False
    return append('ladder', **(fields | kw))


def snapshot(**kw):
    return {'config': {'service_types': [{'name': 'Review'}]}, 'groups': [], 'definitions': [], **kw}


def apply(*rows, saved=None):
    card = start(*rows, page='dependants')
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], saved or snapshot())


def test_customer_ladder_is_explicit_stable_and_does_not_move_progress():
    card = start(row(gates=['Review = Stage 2']), page='dependants')
    _, patch = cards.accept(card, 1, 'accept', [card['lines'][0]['id']], snapshot())
    again = cards.accept(card, 1, 'accept', [card['lines'][0]['id']], snapshot())[1]
    assert patch == again
    d = patch['definitions'][0]
    assert d['key'] == 'progress' and d['staff_can_see'] is False and d['staff_can_see_set'] is True
    assert d['ladder'] == {'notify': 'approve', 'gates': [{'service': 'Review', 'min': 'Stage 2'}]}
    assert set(patch) == {'groups', 'definitions'}


def test_subject_kind_and_ladder_can_be_accepted_together():
    _, patch = apply(append('group', label='Asset', plural='Assets', cardinality='many'), row('Asset'))
    assert patch['definitions'] == []
    field = patch['groups'][0]['fields'][0]
    assert field['durability'] == 'confirm' and field['required'] is False
    assert field['ladder']['notify'] == 'approve'


def test_corrections_preserve_keys_and_unrelated_attributes_and_allow_only_suffix_levels():
    _, first = apply(row())
    old = first['definitions'][0] | {'future': {'keep': True}, 'extraction_hint': 'Retain'}
    saved = snapshot(definitions=[old])
    before = deepcopy(saved)
    _, patch = apply(append('ladder', who='contact', label='Progress', levels=['Stage 1', 'Stage 2', 'Stage 3']), saved=saved)
    assert patch['definitions'][0]['id'] == old['id']
    assert patch['definitions'][0]['future'] == old['future'] and saved == before
    for levels in [['Stage 2', 'Stage 1'], ['Stage 1', 'New', 'Stage 2']]:
        with pytest.raises(cards.Invalid, match='migration'):
            apply(row(levels=levels), saved=saved)


@pytest.mark.parametrize('change', [{'notify': 'guess'}, {'levels': ['A', 'a']}, {'levels': ['A']},
    {'gates': ['Missing = Stage 1']}, {'gates': ['Review = Missing']}, {'gates': ['Review = Stage 1', 'Review = Stage 2']},
    {'staff_can_see': 'false'}, {'levels': ['Stage-1', 'Stage 1']}, {'levels': ['!!!', 'A']}, {'label': '!!!'}])
def test_invalid_choices_are_not_silently_dropped(change):
    with pytest.raises(cards.Invalid): apply(row(**change))


def test_ambiguous_runtime_service_matching_is_rejected():
    with pytest.raises(cards.Invalid, match='another service'):
        apply(row(gates=['Review = Stage 1']), saved=snapshot(config={'service_types': [{'name': 'Review'}, {'name': 'Extended Review'}]}))


def test_plain_field_conversion_and_missing_choices_are_rejected():
    with pytest.raises(cards.Invalid, match='migration'):
        apply(row(), saved=snapshot(definitions=[{'id': 'x', 'key': 'progress', 'label': 'Progress'}]))
    with pytest.raises(cards.Invalid, match='explicitly'):
        apply(append('ladder', who='contact', label='Progress', levels=['A', 'B']))


def test_same_label_for_different_participants_is_not_a_duplicate():
    _, patch = apply(append('group', label='Asset', plural='Assets', cardinality='many'), row('Asset'), row())
    assert len(patch['definitions']) == 1 and len(patch['groups']) == 1


def test_ladder_limit_is_shared_between_participants():
    with pytest.raises(cards.Invalid, match='three'):
        apply(*(row(label='Progress ' + str(i)) for i in range(4)))


def test_legacy_ladder_boundary_keeps_explicit_order_and_empty_prerequisites():
    from app import setup_rows
    parsed = setup_rows.parse('dependants', 'ladder', 'customer | Progress | A, B | off |')
    assert parsed['fields'] == {'who': 'contact', 'label': 'Progress', 'levels': ['A', 'B'], 'notify': 'off', 'gates': []}
