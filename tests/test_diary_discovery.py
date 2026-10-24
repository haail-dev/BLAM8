"""Diary discovery through HTTP and disposable SQL: answers first, settings after."""
import json
from types import SimpleNamespace

import pytest

from app import concierge, guided_setup, llm, onboarding_state
from tests.test_guided_setup import guide_journey  # noqa: F401  (fixture)
from tests.test_agnostic_onboarding import cluster, database, journey  # noqa: F401  (fixtures)

WEEK = {d: ['09:00', '16:00'] for d in ('mon', 'tue', 'wed', 'thu', 'fri')}


def scripted(monkeypatch, *replies):
    """Synthetic structured model replies, in order; an Exception entry raises."""
    calls, queue = [], list(replies)

    async def create(**kwargs):
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(item))],
                               usage=SimpleNamespace(input_tokens=5, output_tokens=5), model='synthetic')
    monkeypatch.setattr(llm, 'create', create)
    monkeypatch.setattr(concierge, 'client', lambda: None)
    return calls


def say(j, text, expected=200):
    d = j.http.get('/app/setup/guide/state').json()
    r = j.http.post('/app/setup/guide/turn', json=dict(revision=j.state()['revision'], stop=d['stop']['id'],
                    question=text, path=d['stop']['href'].split('#')[0], history=[]))
    assert r.status_code == expected, r.text
    return r.json()


def stop(j):
    return j.http.get('/app/setup/guide/state').json()


def answer(model_question, diary, value, state='answered'):
    return dict(question=model_question, diary=diary, state=state, value_json=json.dumps(value))


def key(question, scope_type, scope_id):
    return onboarding_state.key_for(question, scope_type, scope_id)


def diary_id(j, label):
    return next(d['id'] for d in j.state()['diaries'].values() if d['label'] == label)


def test_the_24_october_sentence_is_understood_and_saved(guide_journey, monkeypatch):
    j = guide_journey
    j.diary('choose', labels=['Repair visits'])
    repairs = diary_id(j, 'Repair visits')
    assert stop(j)['stop']['id'] == f'diary.ask.{repairs}.example'
    sentence = ("We arrange contractor visits ourselves, so we don't want appointments booked or confirmed for us. "
                "Our office hours are Monday to Friday, 9 to 4.")
    calls = scripted(monkeypatch, dict(reply='What kind of repair is typical?', new_diaries=[], answers=[
        answer('diary.starter', 'Repair visits', 'we_arrange'),
        answer('diary.hours', None, {'working': WEEK, 'contact_same': True})]))
    before = j.state()['revision']
    out = say(j, sentence)
    state = j.state()
    assert state['revision'] == before + 1
    assert state['answers'][key('diary.starter', 'diary', repairs)]['value'] == 'we_arrange'
    assert key('diary.hours', 'organisation', 'organisation') not in state['answers']  # hours stay a proposal
    assert state['guide']['diary']['prefills']['organisation']['diary.hours']['value']['working'] == WEEK
    assert out['reply'].startswith('Noted for Repair visits: you arrange these.')
    assert 'Monday to Friday, 09:00 to 16:00' in out['reply'] and 'What kind of repair' in out['reply']
    assert out['fallback'] is False and 'detail' not in out
    assert not (j.row('tenant_config', j.tenant) or {}).get('setup_cards') and j.row('scheduling_config', j.tenant) is None
    system = calls[0]['system'][0]['text']
    assert 'Do not propose settings' in system and 'DIARY CONTEXT' in system
    assert 'setup_changes' not in json.dumps(calls[0]['output_config'])


def test_several_answers_for_two_diaries_save_in_one_revision(guide_journey, monkeypatch):
    j = guide_journey
    j.diary('choose', labels=['Repair visits', 'Follow-ups'])
    repairs, follow = diary_id(j, 'Repair visits'), diary_id(j, 'Follow-ups')
    scripted(monkeypatch, dict(reply='', new_diaries=[], answers=[
        answer('diary.starter', repairs, 'we_arrange'), answer('diary.occupies', repairs, ['people']),
        answer('diary.starter', 'follow-ups', 'internal'), answer('diary.occupies', follow, ['nothing']),
        answer('diary.starter', 'Keys', 'internal'), answer('diary.capacity', repairs, 0)]))
    before = j.state()['revision']
    out = say(j, 'We arrange repairs with a contractor; follow-ups are just us and take no time.')
    state = j.state()
    assert state['revision'] == before + 1
    assert state['answers'][key('diary.occupies', 'diary', follow)]['value'] == ['nothing']
    assert state['answers'][key('diary.starter', 'diary', follow)]['value'] == 'internal'
    assert 'I wasn’t sure which diary “Keys” meant.' in out['reply']
    assert 'I couldn’t use your answer to' in out['reply']
    assert key('diary.capacity', 'diary', repairs) not in state['answers']


def test_free_text_on_the_diaries_question_names_diaries_only_from_owner_words(guide_journey, monkeypatch):
    j = guide_journey
    scripted(monkeypatch, dict(reply='Anything else?', new_diaries=[
        dict(label='Repair visits', example='Leaking tap'), dict(label='Compliance calendar', example=None)],
        answers=[answer('diary.starter', 'Repair visits', 'we_arrange')]))
    out = say(j, 'We keep a diary for repair visits.')
    state = j.state()
    labels = [d['label'] for d in state['diaries'].values()]
    assert labels == ['Repair visits'] and state['diaries'][diary_id(j, 'Repair visits')]['example'] == 'Leaking tap'
    assert [p['label'] for p in state['guide']['diary']['proposed_labels']] == ['Compliance calendar']
    # On the diaries question an answer is a hint for later, never saved.
    repairs = diary_id(j, 'Repair visits')
    assert key('diary.starter', 'diary', repairs) not in state['answers']
    assert state['guide']['diary']['prefills'][repairs]['diary.starter']['value'] == 'we_arrange'
    assert 'Noted a new diary: Repair visits.' in out['reply'] and 'Add it if that’s right' in out['reply']
    assert state['guide']['current'] == 'diary.discover.diaries'  # the list is not chosen yet
    j.diary('accept_label', label='Compliance calendar')
    assert len(j.state()['diaries']) == 2 and j.state()['guide']['current'].startswith('diary.ask.')


def test_controls_follow_the_skip_rules_and_reach_the_profile(guide_journey):
    j = guide_journey
    j.diary('choose', labels=['Renewals'])
    renewals = diary_id(j, 'Renewals')
    j.diary('answer', state='answered', value='Gas safety, 4 Elm Road')
    j.diary('answer', state='answered', value='from_records')
    assert stop(j)['stop']['id'] == f'diary.ask.{renewals}.date_source'
    assert stop(j)['discovery']['date_sources'][-1] == dict(id='not_recorded', label='A date we don’t record yet')
    j.diary('answer', state='answered', value='requirement_group:nope:x', expected=400)
    j.diary('answer', state='answered', value='not_recorded')
    assert stop(j)['stop']['id'] == 'diary.ask.hours'
    j.diary('answer', state='answered', value={'working': {'fri': ['22:00', '02:00']}}, expected=400)
    j.diary('answer', state='answered', value={'working': WEEK, 'contact_same': True})
    d = stop(j)
    assert d['stop']['id'] == 'diary.profile'
    line = d['discovery']['profile']['diaries'][0]
    assert line['line'] == 'follows a date you don’t record yet' and line['support'] == 'from_records'
    assert d['discovery']['profile']['hours'].startswith('Hours: Monday to Friday, 09:00 to 16:00.')


def test_confirming_an_unbookable_profile_skips_booking_settings(guide_journey):
    j = guide_journey
    j.diary('choose', labels=['Repair visits'])
    for value in ('Leaking tap', 'we_arrange', ['people'], 1, 'window', {'amount': 2, 'unit': 'hours'}, 'no', 'someone_else', 'The resident'):
        j.diary('answer', state='answered', value=value)
    j.diary('answer', state='answered', value={'working': WEEK, 'contact_same': True})
    assert stop(j)['stop']['id'] == 'diary.profile'
    j.diary('confirm')
    state = j.state()
    assert state['capabilities']['time_occupancy'] == 'deferred'
    assert state['reviews']['diary.activities']['state'] == 'deferred'
    repairs = diary_id(j, 'Repair visits')
    assert state['diaries'][repairs]['status'] == 'confirmed'
    assert state['diaries'][repairs]['support']['reasons'] == ['third_party_agreement']
    ids = [s['id'] for s in guided_setup.steps({**onboarding_state.load(j.ctx), 'state': state})]
    assert not [i for i in ids if i.startswith('diary_settings/')]
    assert not (j.row('tenant_config', j.tenant) or {}).get('setup_cards') and j.row('scheduling_config', j.tenant) is None


@pytest.mark.parametrize('starter,occupies,shape,expect_notice,expect_hours,expect_mode', [
    ('customers', ['places'], 'days', True, True, True),
    ('we_arrange', ['people'], 'window', False, True, False)])
def test_a_ready_profile_opens_only_the_settings_it_needs(guide_journey, starter, occupies, shape, expect_notice,
                                                          expect_hours, expect_mode):
    j = guide_journey
    j.diary('choose', labels=['Work'])
    for value in ('One job', starter, occupies, 2, shape, {'amount': 1, 'unit': 'days'}, 'no', 'no'):
        j.diary('answer', state='answered', value=value)
    j.diary('answer', state='answered', value={'working': WEEK, 'contact_same': True})
    j.diary('confirm')
    state = j.state()
    assert state['capabilities']['time_occupancy'] == 'needed'
    ids = [s['id'] for s in guided_setup.steps({**onboarding_state.load(j.ctx), 'state': state})]
    assert ('diary_settings/min_notice_hours' in ids) is expect_notice
    assert ('diary_settings/working_days' in ids) is expect_hours
    # Booking layout matters for customer booking or work over several days, not an arranged window.
    assert ('diary_settings/stay_mode' in ids) is expect_mode
    assert state['guide']['current'] != 'diary.profile'


def test_adding_more_returns_to_the_list_and_confirm_belongs_to_the_summary(guide_journey):
    j = guide_journey
    j.guide_action('next', expected=400)  # the list must be chosen first
    j.diary('choose', labels=['Visits'])
    visits = diary_id(j, 'Visits')
    j.diary('confirm', expected=400)
    j.diary('add_more')
    assert j.state()['guide']['current'] == 'diary.discover.diaries'
    j.diary('choose', labels=['Stock takes'])
    assert len(j.state()['diaries']) == 2
    assert j.state()['guide']['current'] == f'diary.ask.{visits}.example'
    j.diary('choose', state='not_applicable', expected=400)


def test_model_failure_keeps_state_and_offers_the_controls(guide_journey, monkeypatch):
    j = guide_journey
    j.diary('choose', labels=['Visits'])
    scripted(monkeypatch, RuntimeError('offline'))
    before = j.state()
    out = say(j, 'They take an hour.')
    assert out['fallback'] is True and 'options shown' in out['reply']
    assert j.state() == before


def test_a_stale_revision_is_refused(guide_journey):
    j = guide_journey
    current = j.state()
    r = j.http.post('/app/setup/guide/diary', json=dict(revision=current['revision'] + 5, stop='diary.discover.diaries',
                                                        operation='choose', labels=['Visits']))
    assert r.status_code == 409 and j.state() == current


def test_removing_a_diary_updates_the_list_and_stops_its_answers_blocking(guide_journey):
    j = guide_journey
    j.diary('choose', labels=['Visits', 'Stock takes'])
    stock = diary_id(j, 'Stock takes')
    visits = diary_id(j, 'Visits')
    j.diary('answer', state='unknown')
    j.diary('remove', diary=visits)
    state = j.state()
    assert state['answers'][key('diary.list', 'organisation', 'organisation')]['value'] == [stock]
    unknown = state['answers'][key('diary.example', 'diary', visits)]
    assert unknown['state'] == 'unknown' and not guided_setup.discovery_counts(state, unknown)
    assert state['guide']['current'] == f'diary.ask.{stock}.example'


def test_suggestions_keep_only_cited_trusted_facts(guide_journey, monkeypatch):
    j = guide_journey
    assert stop(j)['discovery']['suggestions'] == []  # nothing saved yet, so no fallback suggestion
    scripted(monkeypatch, dict(suggestions=[
        dict(label='Bookings', reason='You have diary booking.', signal_ids=['module:scheduling'], date_source=None, service=None),
        dict(label='Kennel stays', reason='Kennels usually do.', signal_ids=['industry:kennels'], date_source=None, service=None),
        dict(label='Repairs', reason='Mentioned once.', signal_ids=[], date_source=None, service=None),
        dict(label='Renewals', reason='Records.', signal_ids=['module:crm'], date_source='made_up', service=None)]))
    r = j.http.post('/app/setup/guide/diary/suggest', json=dict(revision=j.state()['revision'], stop='diary.discover.diaries',
                                                               operation='suggest'))
    assert r.status_code == 200, r.text
    suggestions = stop(j)['discovery']['suggestions']
    assert [(s['id'], s['label']) for s in suggestions] == [('s1', 'Bookings')]
    j.diary('choose', suggestions=['s1'])
    created = next(iter(j.state()['diaries'].values()))
    assert created['label_provenance'] == 'owner_selected_suggestion' and created['suggestion']['signal_ids'] == ['module:scheduling']
