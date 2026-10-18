import pytest
from app import setup_cards as cards, setup_card_model as model


def note(key='diary_settings/buffer_minutes', state='needs_answer'):
    return {'key': key, 'state': state}


def test_notes_persist_without_creating_executable_lines_and_omissions_carry():
    one = cards.change('diary_settings', None, 0, 'turn1', [], [note()])
    assert one['status'] == 'considered' and one['lines'] == []
    two = cards.change('diary_settings', one, 1, 'turn2', [], [note('diary_settings/slot_minutes', 'left_unchanged')])
    assert len(two['considered']) == 2 and two['id'] == one['id']
    assert one['revision'] == 1 and len(one['considered']) == 1
    assert one['considered'][0]['question'] == 'What should buffer minutes be?'
    three = cards.change('diary_settings', two, 2, 'turn3', [], [note(state='remove')])
    assert [n['key'] for n in three['considered']] == ['diary_settings/slot_minutes']
    with pytest.raises(cards.Stale):
        cards.change('diary_settings', two, 1, 'stale', [], [note()])


@pytest.mark.parametrize('updates', [[note('crm_settings/quiet_days')], [note('invented/key')],
    [note(state='applied')], [note(), note()], [{'key': 'diary_settings/buffer_minutes', 'state': 'needs_answer', 'label': 'Invented'}], 'bad'])
def test_unknown_cross_page_duplicate_and_invented_metadata_reject(updates):
    with pytest.raises(cards.Invalid):
        cards.change('diary_settings', None, 0, 'turn', [], updates)


def test_real_proposal_resolves_its_note_without_changing_other_notes():
    one = cards.change('diary_settings', None, 0, 'turn1', [], [note(), note('diary_settings/slot_minutes')])
    fields = {'key': 'buffer_minutes', 'value': 10}
    two = cards.change('diary_settings', one, 1, 'turn2', [{'op': 'append', 'name': 'setting',
        'fields': fields, 'sources': {k: {'kind': 'owner_said', 'reference': 'Please allow ten minutes'} for k in fields}}])
    assert [n['key'] for n in two['considered']] == ['diary_settings/slot_minutes']
    assert len(one['considered']) == 2


def test_model_contract_carries_notes_and_rejects_empty_page_updates():
    raw = [{'page': 'diary_settings', 'revision': 0, 'changes': [], 'considered': [note()]}]
    assert model.unpack(raw) == raw
    card = cards.change('diary_settings', None, 0, 'turn1', [], [note()])
    assert 'diary_settings/buffer_minutes' in model.context({'diary_settings': card}, '/app/diary/settings')
    raw[0]['considered'] = []
    with pytest.raises(cards.Invalid): model.unpack(raw)


def test_durable_prompt_retains_explicit_required_and_expiry_rules():
    assert 'only when the owner\nexplicitly says it cannot be booked without it' in model.RULE
    assert 'Expiry alone never implies a gate' in model.RULE
    assert 'a rule about how the business operates belongs in a knowledge note' in model.RULE
    assert 'before a confirmed save receipt' in model.RULE
