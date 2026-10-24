"""The shared setup read: deterministic, read-only, cited, and never from the industry label."""
from copy import deepcopy
import json

import pytest

from app import onboarding_contract as contract, onboarding_state, setup_context


class FakeRead:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def _go(self, name, value):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(name)
        return value

    def team_people(self):
        return self._go('team_people', [dict(name='Sam', active=True), dict(name='Old', active=False)])

    def field_definitions(self):
        return self._go('field_definitions', [
            dict(id='f1', entity='contact', key='gas_cert', label='Gas certificate expiry', field_type='date'),
            dict(id='f2', entity='contact', key='notes', label='Access notes', field_type='text')])

    def stages(self):
        return self._go('stages', [dict(name='Enquiry', stage_type='active'), dict(name='Managed', stage_type='won')])

    def source_texts(self):
        return self._go('source_texts', [dict(source='Website', text='We manage rented homes.'),
                                         dict(source='Owner clarifications · abc', text='Residents email repairs.')])

    def count(self, table):
        return self._go('count', {'crm_contacts': 3, 'crm_companies': 1, 'requirement_instances': 4,
                                  'crm_tasks': 0, 'bookings': 0}[table])


def context(**cfg):
    state = contract.neutral_state()
    state['answers']['k1'] = dict(question='activity.purpose', state='answered', value='Repairs for landlords',
                                  scope_type='organisation', scope_id='organisation',
                                  provenance=dict(kind='about_you_owner_review'))
    state['answers']['k2'] = dict(question='knowledge.x', state='answered', value='Old wording',
                                  provenance=dict(kind='knowledge_conflict'))
    state['answers']['k3'] = dict(question='activity.authority', state='unknown', value=None,
                                  provenance=dict(kind='owner_answer'))
    return dict(state=state, tenant_id='t1', client=None, modules={'scheduling', 'crm'},
                config=dict(business_brief='Two-person property agency.', goals='Fewer missed repairs.',
                            setup_held=[dict(page='diary_settings', values={'open_time': '09:00'})]),
                cfg=cfg, knowledge=None, knowledge_unavailable=False,
                groups=[dict(id='g1', label='Property', plural='Properties', cardinality='many', fields=[
                    dict(key='gas_expiry', label='Gas safety expiry', durability='expires'),
                    dict(key='keys', label='Key code', durability='once')])])


CONFIRMED_HOURS = dict(working_days='mon,tue,wed,thu,fri', open_time='09:00', close_time='16:00',
                       booking_setting_review=dict(
                           working_days=dict(state='set', review_status='confirmed'),
                           opening_hours=dict(state='set', review_status='confirmed')))


def test_the_same_data_gives_the_same_pack_and_revision():
    a = setup_context.build(context(), read=FakeRead())
    b = setup_context.build(context(), read=FakeRead())
    assert a == b and len(a['evidence_revision']) == 64
    ids = [s['id'] for s in a['signals']]
    assert len(ids) == len(set(ids))
    assert [s['trust'] for s in a['signals']] == sorted((s['trust'] for s in a['signals']),
                                                        key=setup_context.TRUST.index)


def test_the_industry_label_is_never_evidence():
    plain = context()
    labelled = context()
    labelled['config']['industry'] = 'Kennels'
    labelled['industry'] = 'Kennels'
    assert setup_context.build(plain, read=FakeRead()) == setup_context.build(labelled, read=FakeRead())
    assert 'Kennels' not in json.dumps(setup_context.build(labelled, read=FakeRead()))


def test_only_confirmed_answers_are_signals():
    pack = setup_context.build(context(), read=FakeRead())
    answers = [s for s in pack['signals'] if s['kind'] == 'answer']
    assert [s['source']['reference'] for s in answers] == ['k1']
    assert answers[0]['trust'] == 'confirmed'


def test_unconfirmed_booking_settings_are_gaps_not_prefills():
    carried = dict(CONFIRMED_HOURS, booking_setting_review=dict(
        working_days=dict(state='set', review_status='carried_over_review'),
        opening_hours=dict(state='set', review_status='carried_over_review')))
    pack = setup_context.build(context(**carried), read=FakeRead())
    assert 'diary.hours' not in pack['prefills']
    assert {'unconfirmed_working_days', 'unconfirmed_opening_hours'} <= set(pack['gaps'])
    assert not any(s['kind'] == 'booking_setting' for s in pack['signals'])


def test_confirmed_hours_prefill_the_hours_question():
    pack = setup_context.build(context(**CONFIRMED_HOURS), read=FakeRead())
    prefill = pack['prefills']['diary.hours']
    assert prefill['value']['working'] == {d: ['09:00', '16:00'] for d in ('mon', 'tue', 'wed', 'thu', 'fri')}
    assert all(setup_context.signal(pack, i)['trust'] == 'config' for i in prefill['signal_ids'])


def test_saved_knowledge_hours_offer_the_owners_words(monkeypatch):
    monkeypatch.setattr(onboarding_state, 'knowledge_candidates', lambda k: [
        dict(id='knowledge.q1', topic='hours', text='When are you available?', answer='Mon to Fri, 9 to 4',
             state='answered', source_review='r1'),
        dict(id='knowledge.q2', topic='rules', text='Any rules?', answer='', state='unknown', source_review='r1')])
    pack = setup_context.build(context(), read=FakeRead())
    assert pack['prefills']['diary.hours'] == dict(value=None, words='Mon to Fri, 9 to 4',
                                                   signal_ids=['knowledge_answer:q1'])
    assert [s['id'] for s in pack['signals'] if s['kind'] == 'knowledge_answer'] == ['knowledge_answer:q1']


def test_records_and_their_dates_are_read():
    pack = setup_context.build(context(), read=FakeRead())
    assert [d['id'] for d in pack['date_sources']] == ['crm_field:contact:gas_cert', 'requirement_group:g1:gas_expiry']
    assert {d['label'] for d in pack['date_sources']} == {'Gas certificate expiry', 'Property Gas safety expiry'}
    assert setup_context.signal(pack, 'record:stages')['value']['names'] == ['Enquiry', 'Managed']
    assert setup_context.signal(pack, 'people:team')['value'] == {'count': 1}


def test_a_failed_read_is_a_gap_and_the_rest_still_reads():
    pack = setup_context.build(context(), read=FakeRead(fail={'team_people', 'stages', 'source_texts'}))
    assert {'people_unavailable', 'stages_unavailable', 'source_texts_unavailable'} <= set(pack['gaps'])
    assert setup_context.signal(pack, 'business:business_brief')


def test_building_reads_and_never_changes_the_context():
    ctx = context(**CONFIRMED_HOURS)
    before = deepcopy(ctx)
    read = FakeRead()
    setup_context.build(ctx, read=read)
    assert ctx == before
    assert set(read.calls) == {'team_people', 'field_definitions', 'stages', 'source_texts', 'count'}


def test_held_values_and_counts_are_low_trust():
    pack = setup_context.build(context(), read=FakeRead())
    assert setup_context.signal(pack, 'held:diary_settings:0')['trust'] == 'held'
    assert setup_context.signal(pack, 'count:crm_contacts')['trust'] == 'derived'
    assert setup_context.signal(pack, 'count:crm_tasks') is None  # zero is not a signal


def test_owner_clarifications_come_before_other_saved_sources():
    pack = setup_context.build(context(), read=FakeRead())
    sources = [s['value']['source'] for s in pack['signals'] if s['kind'] == 'source_text']
    assert sources[0].startswith('Owner clarifications')


def test_model_view_drops_lowest_trust_first_to_fit():
    ctx = context()
    big = [dict(source=f'Source {i}', text='x' * 600) for i in range(12)]
    read = FakeRead()
    read.source_texts = lambda: big
    ctx['config']['setup_held'] = [dict(page=f'p{i}', values={'v': 'y' * 380}) for i in range(60)]
    view = setup_context.model_view(setup_context.build(ctx, read=read), 'diary')
    assert view['truncated'] and len(json.dumps(view, separators=(',', ':'))) <= setup_context.MODEL_CAP
    kept = [s['trust'] for s in view['signals']]
    assert 'confirmed' in kept and kept == sorted(kept, key=setup_context.TRUST.index)
    with pytest.raises(ValueError):
        setup_context.model_view(setup_context.build(context(), read=FakeRead()), 'charges')


def test_records_step_sources_are_in_the_shared_picture():
    ctx = context()
    ctx['state']['capabilities'] = {'related_records': 'needed', 'lifecycle': 'undecided'}
    pack = setup_context.build(ctx, read=FakeRead())
    group = setup_context.signal(pack, 'record:group:g1')['value']
    assert (group['plural'], group['cardinality']) == ('Properties', 'many')
    assert [f['durability'] for f in group['fields']] == ['expires', 'once']
    assert setup_context.signal(pack, 'record:stages')['value']['types'] == ['active', 'won']
    assert setup_context.signal(pack, 'count:crm_companies')['value']['count'] == 1
    assert setup_context.signal(pack, 'count:requirement_instances')['value']['count'] == 4
    assert setup_context.signal(pack, 'capability:related_records')['trust'] == 'confirmed'
    assert setup_context.signal(pack, 'capability:lifecycle') is None  # undecided is not a decision
