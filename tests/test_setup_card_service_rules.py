from copy import deepcopy
import pytest
from app import setup_cards as cards
from tests.test_setup_cards import append, start, service
from tests.test_setup_card_scalars import setting


def rule(name='service_urgent', **kw):
    return append(name, **({'name': 'Review'} | kw))


def ooh(**kw):
    return rule('service_out_of_hours', **(dict(enabled=True, until='21:00', days=['sat'], bank_holidays=False,
        price=100, price_after=None, price_basis='booking', offer='ask', terms='') | kw))


def apply(*rows, cfg=None):
    cfg = cfg if cfg is not None else {'service_types': [{'name': 'Review', 'minutes': 30, 'price': 60, 'future': 7}], 'job_done_when': 'marked'}
    card = start(*rows)
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']], cfg)


def test_service_and_rules_can_be_accepted_together_with_explicit_zero_notice():
    _, patch = apply(service(), rule(enabled=True, same_day_until='14:00', notice_hours=0), cfg={})
    assert patch['service_types'][0]['same_day_until'] == '14:00'
    assert patch['service_types'][0]['notice_hours'] == 0


def test_rules_preserve_other_service_attributes_and_disable_explicitly():
    _, first = apply(ooh(), rule(enabled=True, same_day_until='14:00', notice_hours=2))
    before = deepcopy(first)
    _, changed = apply(rule(notice_hours=0), cfg=first)
    row = changed['service_types'][0]
    assert row['future'] == 7 and row['out_of_hours'] == first['service_types'][0]['out_of_hours']
    assert first == before
    _, off = apply(rule(enabled=False), cfg=changed)
    assert 'same_day_until' not in off['service_types'][0] and 'notice_hours' not in off['service_types'][0]
    assert off['service_types'][0]['out_of_hours'] == row['out_of_hours']


def test_ooh_preserves_nested_extras_and_requires_carried_over_confirmation():
    _, first = apply(ooh())
    first['service_types'][0]['out_of_hours']['future'] = 'keep'
    _, changed = apply(rule('service_out_of_hours', until=None, days=['sun']), cfg=first)
    block = changed['service_types'][0]['out_of_hours']
    assert 'until' not in block and block['days'] == [6] and block['future'] == 'keep'
    first['service_types'][0]['out_of_hours']['carried_over_review'] = ['terms']
    with pytest.raises(cards.Invalid, match='Confirm'):
        apply(rule('service_out_of_hours', price=110), cfg=first)
    _, confirmed = apply(rule('service_out_of_hours', terms=''), cfg=first)
    assert 'carried_over_review' not in confirmed['service_types'][0]['out_of_hours']


def test_tasks_need_explicit_trigger_keep_deadlines_and_can_be_cleared():
    tasks = rule('service_after_job', tasks=['Send report — 2 working days', 'Follow up — 30 days'])
    with pytest.raises(cards.Invalid, match='done'):
        apply(tasks, cfg={'service_types': [{'name': 'Review'}]})
    _, patch = apply(tasks, setting('job_done_when', 'marked'), cfg={'service_types': [{'name': 'Review'}]})
    assert patch['service_types'][0]['after_job'] == [
        {'title': 'Send report', 'days': 2, 'unit': 'working'}, {'title': 'Follow up', 'days': 30, 'unit': 'calendar'}]
    _, cleared = apply(rule('service_after_job', tasks=[]), cfg=patch)
    assert cleared['service_types'][0]['after_job'] == []


@pytest.mark.parametrize('entry', [rule(same_day_until='14:00', notice_hours=0), rule(enabled=True),
    rule(enabled=True, same_day_until='24:00', notice_hours=0), rule(enabled=True, same_day_until='14:00', notice_hours=True),
    ooh(price=0), ooh(days=['mon','tue','wed','thu','fri','sat','sun']), ooh(until=None, days=[]),
    ooh(price_basis='unspecified'), rule('service_after_job', tasks=['Missing deadline']),
    rule('service_after_job', tasks=['Task — 1 days', 'Task — 2 days']),
    rule('service_after_job', tasks=['x'*81 + ' — 1 days'])])
def test_invalid_or_lossy_rules_are_not_applied(entry):
    with pytest.raises(cards.Invalid): apply(entry)


def test_disabling_a_pending_rule_does_not_require_repairing_its_inactive_values():
    _, patch = apply(rule(enabled=False, same_day_until='bad', notice_hours=999))
    assert 'same_day_until' not in patch['service_types'][0]


def test_legacy_rows_keep_explicit_values_without_guessing_missing_ooh_choices():
    from app import setup_rows
    parsed = setup_rows.parse('diary_settings', 'service_urgent', 'Review | 14:00 | 0')
    assert parsed['fields']['notice_hours'] == 0
    parsed = setup_rows.parse('diary_settings', 'service_out_of_hours', 'Review | 21:00 | sat | no | 100 |')
    assert parsed['fields']['days'] == ['sat'] and 'offer' not in parsed['fields'] and 'price_basis' not in parsed['fields']
