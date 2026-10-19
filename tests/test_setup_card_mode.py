from copy import deepcopy
import pytest
from app import setup_cards as cards, intake
from tests.test_setup_cards import start, append


def mode(on):
    return append('setting', key='stay_mode', value=on)


def test_history_read_is_tenant_scoped_and_fails_closed():
    from app import db
    from tests.test_setup_card_store import Client
    for rows, expected in [([], False), ([{'id': 'saved'}], True)]:
        client = Client(response=rows)
        assert db.setup_has_booking_history('owner', client=client) is expected
        assert ('eq', ('tenant_id', 'owner'), {}) in client.calls
        assert ('limit', (1,), {}) in client.calls
    with pytest.raises(RuntimeError):
        db.setup_has_booking_history('owner', client=Client(response=None))
    with pytest.raises(RuntimeError):
        db.setup_has_booking_history('owner', client=Client(error=RuntimeError('unavailable')))


def apply(on, cfg=None, history=False, *rows):
    # Build a supported current proposal, then present the serialized legacy
    # page shape to exercise acceptance of cards saved before ownership moved.
    card = start(mode(on), *rows, page='diary_settings')
    card['page'] = 'booking_details'
    return cards.accept(card, 1, 'accept', [r['id'] for r in card['lines']],
        {'config': cfg or {}, 'definitions': [], 'has_bookings': history})


def test_mode_and_reserved_capture_change_together_without_losing_other_fields():
    original = {'label': 'Reference', 'required': False, 'future': {'a': 1}, 'hint': 'Original'}
    cfg = {'intake_fields': [original], 'service_types': [], 'future': 3}
    before = deepcopy(cfg)
    _, patch = apply(True, cfg)
    assert patch['booking_mode'] == 'stay' and intake.is_stay(patch['intake_fields'])
    assert patch['intake_fields'][0] == original and cfg == before
    assert patch['definitions'] == [] and 'service_types' not in patch
    _, off = apply(False, cfg | patch)
    assert off['booking_mode'] == 'appointment' and off['intake_fields'] == [original]


def test_history_blocks_real_transitions_but_not_unchanged_confirmation():
    with pytest.raises(cards.Invalid, match='migration'):
        apply(True, {}, True)
    with pytest.raises(cards.Invalid, match='migration'):
        apply(False, {'booking_mode': 'stay', 'intake_fields': [intake.STAY_FIELD]}, True)
    _, patch = apply(True, {'booking_mode': 'stay', 'intake_fields': [intake.STAY_FIELD]}, True)
    assert patch['intake_fields'] == [intake.STAY_FIELD]
    with pytest.raises(cards.Invalid, match='verified'):
        apply(True, {}, None)


def test_legacy_mode_fallback_and_explicit_mode_precedence():
    _, patch = apply(True, {'intake_fields': [intake.STAY_FIELD]}, True)
    assert patch['booking_mode'] == 'stay'
    with pytest.raises(cards.Invalid, match='migration'):
        apply(True, {'booking_mode': 'appointment', 'intake_fields': [intake.STAY_FIELD]}, True)


def test_services_require_separate_explicit_appointment_capacity():
    cfg = {'service_types': [{'name': 'Review', 'minutes': 30}]}
    with pytest.raises(cards.Invalid, match='same-name'):
        apply(True, cfg)
    cfg['service_types'][0]['resource'] = 'Review'
    cfg['resource_types'] = [{'name': 'Review', 'count': 2}]
    with pytest.raises(cards.Invalid, match='stay capacity'):
        apply(True, cfg)
    cfg['capacity_per_window'] = 5
    _, patch = apply(True, cfg)
    assert patch['booking_mode'] == 'stay' and 'resource_types' not in patch


def test_no_truncation_of_full_intake_or_duplicate_departure_fields():
    with pytest.raises(cards.Invalid, match='no room'):
        apply(True, {'intake_fields': [{'label': str(i)} for i in range(20)]})
    with pytest.raises(cards.Invalid, match='duplicate departure'):
        apply(True, {'intake_fields': [intake.STAY_FIELD, intake.STAY_FIELD]})


def test_scoped_fields_change_with_mode_in_the_same_acceptance():
    cfg = {'booking_mode': 'stay', 'intake_fields': [intake.STAY_FIELD,
        {'label': 'Reference', 'required': False, 'services': ['Stays'], 'hint': '', 'doc': False}]}
    with pytest.raises(cards.Invalid, match='Stays-only'):
        apply(False, cfg)
    cfg['intake_fields'][1]['services'] = []
    _, patch = apply(False, cfg)
    assert patch['booking_mode'] == 'appointment'
    assert patch['intake_fields'][0]['services'] == []


@pytest.mark.parametrize('value', ['1', 1, None])
def test_native_boolean_choice_required(value):
    with pytest.raises(cards.Invalid):
        apply(value)


def _stay_cfg(pool):
    return {'service_types': [{'name': 'Review', 'minutes': 30, 'resource': 'Review'}],
            'resource_types': [pool, {'name': 'Space', 'count': 2}], 'capacity_per_window': 5}


def test_unpriced_appointment_pool_with_default_unit_can_switch_mode():
    from app import services
    legacy = {'name': 'Review', 'count': 1, 'rate': None, 'rate_unit': 'booking', 'skills': []}
    cfg = _stay_cfg(legacy)
    before = deepcopy(cfg)
    _, patch = apply(True, cfg)
    assert patch['booking_mode'] == 'stay' and cfg == before
    svcs, pools = services.bind_appointment_pools([{'name': 'Review', 'minutes': 30}], [])
    _, patch = apply(True, {'service_types': svcs, 'resource_types': pools, 'capacity_per_window': 5})
    assert patch['booking_mode'] == 'stay'
    _, patch = apply(True, _stay_cfg(legacy | {'rate': 35.0, 'rate_unit': 'day'}))
    assert patch['booking_mode'] == 'stay'


def test_priced_appointment_pool_still_needs_a_valid_unit_and_bad_capacity_is_refused():
    with pytest.raises(cards.Invalid, match='verified'):
        apply(True, _stay_cfg({'name': 'Review', 'count': 1, 'rate': 5, 'rate_unit': 'fortnight'}))
    with pytest.raises(cards.Invalid, match='verified'):
        apply(True, _stay_cfg({'name': 'Review', 'count': 0, 'rate': None, 'rate_unit': 'booking'}))
