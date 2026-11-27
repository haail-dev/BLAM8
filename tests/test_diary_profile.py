"""Named diaries: storage, planning, support states and plain profile lines."""
import pytest

from app import diary_profile, diary_support, onboarding_contract as contract, onboarding_state

MODULES = {'scheduling', 'crm'}


def new_state():
    return contract.neutral_state()


def say(state, diary, **answers):
    for field, raw in answers.items():
        identity = 'diary.' + field
        spec = contract.question(identity)
        status, value = ('answered', contract.answer_value(spec, raw)) if raw not in ('unknown', 'deferred') else (raw, None)
        onboarding_state._confirmed_answer(state, spec, 'diary', diary, status, value, 'owner')


def make(state, label, **answers):
    identity = diary_profile.create(state, label, 'owner', provenance='owner_typed')
    say(state, identity, **answers)
    return identity


# ── Storage ───────────────────────────────────────────────────────────────────
def test_create_rename_remove_keep_history_and_limits():
    state = new_state()
    first = make(state, 'Repair visits')
    assert state['diaries'][first]['order'] == 1 and state['diaries'][first]['status'] == 'named'
    with pytest.raises(ValueError, match='already'):
        diary_profile.create(state, '  repair   VISITS ', 'owner', provenance='owner_typed')
    diary_profile.rename(state, first, 'Contractor visits', 'owner')
    diary_profile.remove(state, first, 'owner')
    history = state['diaries'][first]['history']
    assert [h['change'] for h in history] == ['rename', 'remove'] and history[0]['previous'] == 'Repair visits'
    assert contract.active_diaries(state) == []
    for i in range(contract.DIARY_LIMIT):
        make(state, f'Diary {i}')
    with pytest.raises(ValueError, match='up to 8'):
        make(state, 'One too many')
    with pytest.raises(ValueError):
        diary_profile.create(new_state(), 'x' * 41, 'owner', provenance='owner_typed')
    with pytest.raises(ValueError):
        diary_profile.create(new_state(), 'Visits', 'owner', provenance='model')


def test_reorder_needs_every_active_diary_once():
    state = new_state()
    a, b = make(state, 'A'), make(state, 'B')
    diary_profile.reorder(state, [b, a])
    assert [d['id'] for d in contract.active_diaries(state)] == [b, a]
    with pytest.raises(ValueError):
        diary_profile.reorder(state, [a])


# ── Planning (spec §2.2) ──────────────────────────────────────────────────────
def test_from_records_asks_only_for_the_date():
    state = new_state()
    d = make(state, 'Renewals', example='Gas certificate for 4 Elm Road', starter='from_records')
    assert diary_profile.applicable(state, d) == ['diary.example', 'diary.starter', 'diary.date_source']
    assert diary_profile.next_question(state, d) == 'diary.date_source'
    say(state, d, date_source='requirement_group:g1:gas_expiry')
    assert diary_profile.profiled(state, d)


def test_nothing_occupied_skips_capacity_and_agreement_and_limits_timing():
    state = new_state()
    d = make(state, 'Follow-ups', example='Chase a landlord', starter='internal', occupies=['nothing'])
    plan = diary_profile.applicable(state, d)
    assert 'diary.capacity' not in plan and 'diary.agreement' not in plan
    assert diary_profile.choices(state, d, 'diary.time_shape') == ['set_time', 'due_by']
    say(state, d, time_shape='due_by')
    assert 'diary.length' not in diary_profile.applicable(state, d)


def test_changing_an_answer_replans_and_keeps_the_old_answer():
    state = new_state()
    d = make(state, 'Visits', starter='customers', occupies=['people'], agreement='someone_else')
    assert 'diary.agreement_who' in diary_profile.applicable(state, d)
    say(state, d, starter='internal')
    assert 'diary.agreement' not in diary_profile.applicable(state, d)
    assert diary_profile.answer(state, d, 'diary.agreement')['value'] == 'someone_else'


def test_unsure_answers_count_as_answered_for_planning():
    state = new_state()
    d = make(state, 'Visits', example='x', starter='unknown', occupies='deferred', capacity='unknown',
             time_shape='unknown', length='unknown', repeats='no', agreement='unknown')
    assert diary_profile.profiled(state, d)


# ── Support (spec §4.3) ───────────────────────────────────────────────────────
@pytest.mark.parametrize('answers,modules,state,reasons', [
    (dict(starter='from_records', date_source='x'), MODULES, 'from_records', ['records_view']),
    (dict(starter='from_records', date_source='not_recorded'), MODULES, 'from_records', ['records_view', 'records_date_missing']),
    (dict(starter='we_arrange', occupies=['people', 'places']), MODULES, 'noted', ['combined_resources']),
    (dict(starter='we_arrange', occupies=['people'], agreement='someone_else'), MODULES, 'noted', ['third_party_agreement']),
    (dict(starter='internal', occupies=['places'], repeats='depends'), MODULES, 'noted', ['rule_repeats']),
    (dict(starter='internal', occupies=['nothing'], time_shape='due_by'), MODULES, 'ready', []),
    (dict(starter='internal', occupies=['nothing'], time_shape='due_by'), {'scheduling'}, 'unavailable', ['module_crm']),
    (dict(starter='customers', occupies=['places']), {'crm'}, 'unavailable', ['module_scheduling']),
    (dict(starter='customers', occupies=['places']), MODULES, 'ready', []),
    (dict(starter='we_arrange', occupies=['people']), MODULES, 'ready', []),
    (dict(starter='internal', occupies=['equipment']), MODULES, 'ready', []),
    (dict(starter='internal', occupies=['nothing'], time_shape='set_time'), MODULES, 'noted', ['not_classified']),
    (dict(), MODULES, 'undecided', ['undecided']),
    (dict(starter='customers'), MODULES, 'undecided', ['undecided']),
])
def test_every_support_rule(answers, modules, state, reasons):
    values = {'diary.' + k: v for k, v in answers.items()}
    result = diary_support.classify(values, modules)
    assert (result['state'], result['reasons']) == (state, reasons)
    assert all(diary_support.REASONS[r] for r in result['reasons'])


def test_equivalent_answers_under_different_labels_get_the_same_state():
    state = new_state()
    answers = dict(starter='customers', occupies=['places'], time_shape='days', repeats='no', agreement='customer')
    a, b = make(state, 'Stays', **answers), make(state, 'Overnight lodging', **answers)
    assert diary_profile.support(state, a, MODULES) == diary_profile.support(state, b, MODULES)


# ── Profile lines (spec §4.2, §4.6) ───────────────────────────────────────────
DATES = [dict(id='requirement_group:g1:gas_expiry', label='Property Gas safety expiry'),
         dict(id='requirement_group:g2:vacc', label='Dog vaccination expiry')]


def test_alder_and_key_profile():
    state = new_state()
    make(state, 'Repair visits', example='Leaking tap at 4 Elm Road', starter='we_arrange', occupies=['people'],
         capacity=2, time_shape='window', length={'amount': 2, 'unit': 'hours'}, repeats='no',
         agreement='someone_else', agreement_who='The resident')
    make(state, 'Certificate renewals', example='Gas safety at 4 Elm Road', starter='from_records',
         date_source='requirement_group:g1:gas_expiry')
    make(state, 'Follow-ups', example='Chase a landlord', starter='internal', occupies=['nothing'],
         time_shape='due_by', repeats='no')
    spec = contract.question('diary.hours')
    onboarding_state._confirmed_answer(state, spec, 'organisation', 'organisation', 'answered', contract.answer_value(
        spec, {'working': {d: ['09:00', '16:00'] for d in ('mon', 'tue', 'wed', 'thu', 'fri')}}), 'owner')
    result = diary_profile.profile(state, MODULES, DATES)
    lines = {d['label']: d for d in result['diaries']}
    assert lines['Repair visits']['line'] == ('you arrange these; they take up someone’s time, up to 2 at once; '
                                              'in an arrival window, about 2 hours; no set repeat; The resident needs to agree')
    assert (lines['Repair visits']['support'], lines['Repair visits']['reason_codes']) == ('noted', ['third_party_agreement'])
    assert lines['Certificate renewals']['line'] == 'follows Property Gas safety expiry from your records'
    assert lines['Certificate renewals']['support'] == 'from_records'
    assert (lines['Follow-ups']['support'], lines['Follow-ups']['kind']) == ('ready', 'tasks')
    assert all(d['profiled'] for d in result['diaries'])
    assert result['hours'] == 'Hours: Monday to Friday, 09:00 to 16:00. People can reach you at the same times.'


def test_little_paws_profile():
    state = new_state()
    make(state, 'Stays', example='Bella, 3 nights', starter='customers', occupies=['places'], capacity=12,
         time_shape='days', length={'amount': 1, 'unit': 'days'}, repeats='no', agreement='no')
    make(state, 'Meet and greets', example='New dog visit', starter='customers', occupies=['people'], capacity=1,
         time_shape='set_time', length={'amount': 30, 'unit': 'minutes'}, repeats='no', agreement='customer')
    make(state, 'Vaccination expiries', example='Bella’s kennel cough', starter='from_records',
         date_source='requirement_group:g2:vacc')
    lines = {d['label']: d for d in diary_profile.profile(state, MODULES, DATES)['diaries']}
    assert [lines[k]['support'] for k in ('Stays', 'Meet and greets', 'Vaccination expiries')] == ['ready', 'ready', 'from_records']
    assert lines['Stays']['line'] == ('customers ask for these; they take up a place, up to 12 at once; '
                                      'over one or more days, about 1 day; no set repeat')
    assert lines['Meet and greets']['line'].endswith('the customer confirms the time')


def test_open_answers_read_as_not_decided_yet():
    state = new_state()
    d = make(state, 'Visits')
    assert diary_profile.describe(state, d) == ('who starts these is not decided yet; what they take up is not decided yet; '
                                                'timing not decided yet; repeat not decided yet; who agrees the time is not decided yet')
    assert diary_profile.profile(state, MODULES)['hours'] == 'Hours: not decided yet.'


def test_profile_shows_confirmed_variable_hours():
    state = new_state()
    onboarding_state._confirmed_answer(state, contract.question('diary.hours'), 'organisation',
                                       'organisation', 'not_applicable', None, 'owner')
    result = diary_profile.profile(state, MODULES)
    assert result['hours'] == 'No fixed weekly hours.'
    assert result['hours_state'] == 'not_applicable'


def test_hours_line_groups_runs_and_reports_contact_hours():
    hours = contract.answer_value(contract.question('diary.hours'), {
        'working': {'mon': ['09:00', '17:00'], 'tue': ['09:00', '17:00'], 'thu': ['09:00', '17:00'], 'sat': ['10:00', '12:00']},
        'bank_holidays_closed': True, 'contact_same': False, 'contact': {'mon': ['08:00', '18:00']}})
    assert diary_profile.describe_hours(hours) == (
        'Hours: Monday to Tuesday, 09:00 to 17:00; Thursday, 09:00 to 17:00; Saturday, 10:00 to 12:00. '
        'Closed on bank holidays. People can reach you: Monday, 08:00 to 18:00.')
