from copy import deepcopy
import pytest
from app import setup_cards as cards
from tests.test_setup_cards import append, start


def stage(**kw):
    return append('stage', **(dict(name='Waiting', stage_type='active', display_order=2) | kw))


def apply(*rows, snapshot=None):
    card = start(*rows, page='crm_settings')
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']],
                        snapshot if snapshot is not None else {'quiet_days': None, 'stages': []})


def test_new_stages_have_stable_preview_ids_and_threshold_saves_together():
    card = start(stage(), stage(name='Complete', stage_type='won', display_order=3),
                 append('setting', key='quiet_days', value=7), page='crm_settings')
    ids = [r['id'] for r in card['lines']]
    snapshot = {'quiet_days': None, 'stages': []}
    preview, changes = cards.acceptance_patch(card, ids, snapshot)
    accepted, patch = cards.accept(card, 1, 'accept', ids, snapshot)
    assert patch == preview and patch['crm_quiet_days'] == 7
    assert len({s['id'] for s in patch['stages']}) == 2
    assert accepted['last_action']['changes'] == changes


def test_existing_stage_keeps_id_name_type_and_unmentioned_attributes():
    old = dict(id='saved-id', name='Waiting', stage_type='active', display_order=2, future=7)
    snapshot = {'quiet_days': 14, 'stages': [old]}
    before = deepcopy(snapshot)
    _, patch = apply(append('stage', name='waiting', display_order=4), snapshot=snapshot)
    assert patch == {'stages': [dict(id='saved-id', name='Waiting', stage_type='active', display_order=4)]}
    assert snapshot == before
    with pytest.raises(cards.Invalid, match='separate review'):
        apply(stage(stage_type='won'), snapshot=snapshot)


@pytest.mark.parametrize('row', [append('stage', name='Waiting'), stage(stage_type='invented'),
    stage(display_order=1.5), stage(display_order=True), stage(name=''),
    append('setting', key='quiet_days', value=0), append('setting', key='quiet_days', value=366)])
def test_missing_or_invalid_crm_choices_are_rejected(row):
    with pytest.raises(cards.Invalid):
        apply(row)


def test_ambiguous_saved_names_and_unverified_snapshot_are_rejected():
    with pytest.raises(cards.Invalid, match='ambiguous'):
        apply(stage(), snapshot={'quiet_days': 14, 'stages': [{'name': 'Waiting'}, {'name': 'waiting'}]})
    with pytest.raises(cards.Invalid, match='verified'):
        apply(stage(), snapshot={})


def test_threshold_only_preserves_stages_and_leave_never_deletes_one():
    _, patch = apply(append('setting', key='quiet_days', value=1))
    assert patch == {'stages': [], 'crm_quiet_days': 1}
    card = start(stage(), page='crm_settings')
    left = cards.change('crm_settings', card, 1, 'leave', [{'op': 'remove', 'id': card['lines'][0]['id']}])
    assert left['status'] == 'left'
