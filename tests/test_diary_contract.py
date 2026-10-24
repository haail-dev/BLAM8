"""Diary discovery questions: typed values, the diary scope, and staying out of generic stops."""
import pytest

from app import guided_setup, onboarding_contract as contract, onboarding_state


def value(identity, raw):
    return contract.answer_value(contract.question(identity), raw)


def test_discovery_questions_are_code_owned_and_marked():
    discovery = {i for i, q in contract.QUESTIONS.items() if q.get('discovery')}
    assert discovery == {'diary.list', 'diary.example', 'diary.starter', 'diary.date_source', 'diary.occupies',
                         'diary.capacity', 'diary.time_shape', 'diary.length', 'diary.repeats', 'diary.repeats_detail',
                         'diary.agreement', 'diary.agreement_who', 'diary.hours', 'diary.profile'}
    assert all(contract.QUESTIONS[i]['adapter'] is None for i in discovery)
    assert contract.question('diary.hours')['scopes'] == ['organisation']
    assert contract.question('diary.starter')['scopes'] == ['diary']


@pytest.mark.parametrize('identity,raw,expected', [
    ('diary.starter', 'from_records', 'from_records'),
    ('diary.occupies', ['places', 'people'], ['people', 'places']),
    ('diary.occupies', ['nothing'], ['nothing']),
    ('diary.capacity', 1, 1),
    ('diary.length', {'amount': 2, 'unit': 'hours'}, {'amount': 2, 'unit': 'hours'}),
    ('diary.repeats', 'six_weekly', 'six_weekly'),
    ('diary.example', 'Boiler service at 4 Elm Road', 'Boiler service at 4 Elm Road'),
    ('diary.list', ['a', 'b'], ['a', 'b']),
])
def test_typed_values_round_trip(identity, raw, expected):
    assert value(identity, raw) == expected


@pytest.mark.parametrize('identity,raw', [
    ('diary.starter', 'customer'),
    ('diary.occupies', ['nothing', 'people']),
    ('diary.occupies', []),
    ('diary.capacity', 0),
    ('diary.capacity', True),
    ('diary.capacity', 1000),
    ('diary.length', {'amount': 2, 'unit': 'weeks'}),
    ('diary.length', {'amount': 1.5, 'unit': 'hours'}),
    ('diary.example', 'x' * 201),
    ('diary.list', ['a'] * 2),
    ('diary.list', [str(i) for i in range(9)]),
])
def test_invalid_values_are_refused(identity, raw):
    with pytest.raises(ValueError):
        value(identity, raw)


def test_hours_keep_working_and_contact_times_and_refuse_overnight():
    hours = value('diary.hours', {'working': {'mon': ['09:00', '16:00'], 'fri': ['09:00', '13:00']},
                                  'bank_holidays_closed': True, 'contact_same': False,
                                  'contact': {'mon': ['08:00', '18:00']}, 'notes': 'Emergencies by phone'})
    assert hours == {'working': {'mon': ['09:00', '16:00'], 'fri': ['09:00', '13:00']}, 'bank_holidays_closed': True,
                     'contact_same': False, 'contact': {'mon': ['08:00', '18:00']}, 'notes': 'Emergencies by phone'}
    assert value('diary.hours', {'working': {'tue': ['10:00', '12:00']}})['contact'] is None
    for bad in ({'working': {'fri': ['22:00', '02:00']}}, {'working': {'mon': ['09:10', '16:00']}},
                {'working': {}}, {'working': {'mon': ['9:00', '16:00']}}, {'working': {'xyz': ['09:00', '10:00']}},
                {'working': {'mon': ['09:00', '16:00']}, 'contact_same': False},
                {'working': {'mon': ['09:00', '16:00']}, 'extra': 1}):
        with pytest.raises(ValueError):
            value('diary.hours', bad)
    with pytest.raises(ValueError, match='Overnight'):
        value('diary.hours', {'working': {'fri': ['22:00', '02:00']}})


def test_the_diary_scope_accepts_only_active_diaries():
    state = contract.neutral_state()
    state['diaries'] = {'d1': dict(id='d1', order=1, status='named'), 'd2': dict(id='d2', order=2, status='removed')}
    spec = contract.question('diary.starter')
    contract.validate_scope(spec, 'diary', 'd1', state, [])
    for scope_id in ('d2', 'missing'):
        with pytest.raises(ValueError):
            contract.validate_scope(spec, 'diary', scope_id, state, [])
    with pytest.raises(ValueError):
        contract.validate_scope(spec, 'organisation', 'organisation', state, [])


def test_existing_states_load_with_an_empty_diaries_map():
    stored = contract.neutral_state()
    del stored['diaries']
    assert contract.read_state(stored)['diaries'] == {}
    stored['diaries'] = []
    with pytest.raises(ValueError):
        contract.read_state(stored)


def test_discovery_questions_never_become_generic_fact_stops():
    context = dict(state=contract.neutral_state(), modules={'scheduling', 'crm'}, cfg={}, groups=[],
                   config={}, invoice={})
    context['state']['capabilities']['time_occupancy'] = 'needed'
    ids = [s['id'] for s in guided_setup.steps(context)]
    assert not [i for i in ids if i.startswith('question.diary.')]
    assert 'question.activity.purpose' in ids


def test_several_answers_save_in_one_revision():
    state = contract.neutral_state()
    state['diaries'] = {'d1': dict(id='d1', order=1, status='named')}
    for identity, raw in (('diary.starter', 'we_arrange'), ('diary.occupies', ['people'])):
        spec = contract.question(identity)
        onboarding_state._confirmed_answer(state, spec, 'diary', 'd1', 'answered', value(identity, raw), 'u1')
    assert state['revision'] == 0  # one CAS save later bumps it once
    assert {a['question'] for a in state['answers'].values()} == {'diary.starter', 'diary.occupies'}
