"""Offer choice against the merged readiness and reservation guarantees."""
import asyncio
import uuid

import pytest

from app import db, portal_data, scheduling
from app.booking_errors import CapacityConflict
from tests.test_booking_capacity_database import ROOT, cluster as base_cluster, insert
from tests.test_no_defaults_rollback import setup_tenants
from tests.test_offer_deadlines import choice, no_db  # noqa: F401


@pytest.fixture
def cluster():
    yield from base_cluster.__wrapped__()


def test_migration_stack_forward_reverse_forward_and_choice(cluster):
    driver, dsn = cluster
    names = ['booking_guarantees', 'no_defaults', 'offer_last_call', 'choose_offer']

    def migrate(conn, name, rollback=False):
        suffix = '.rollback' if rollback else ''
        conn.execute((ROOT / f'db/migrations/20260927_{name}{suffix}.sql').read_text(encoding='utf-8'))

    with driver.connect(dsn, autocommit=True) as conn:
        migrate(conn, 'booking_guarantees', True)
        setup_tenants(conn)
        conn.execute('create role service_role')
        conn.execute('alter table bookings add column contact text')
        conn.execute('alter table scheduling_config add column min_notice_hours int default 24')
        tenant = uuid.uuid4()
        conn.execute('insert into tenants values (%s)', (tenant,))
        conn.execute("insert into subscriptions values (%s,'scheduling','active')", (tenant,))
        conn.execute('insert into scheduling_config(tenant_id, min_notice_hours) values (%s,4)', (tenant,))
        for _ in range(2):
            for name in names:
                migrate(conn, name)
            assert conn.execute('select offer_last_call::text, offer_last_call_review from scheduling_config where tenant_id=%s', (tenant,)).fetchone() == ('20:00:00', 'carried_over_review')
            a = insert(conn, tenant, status='offered', expires_at='2099-01-01', contact='a@test.invalid')
            b = insert(conn, tenant, start=48, status='offered', expires_at='2099-01-01', contact='a@test.invalid')
            # A live hold is not permission to bypass a newly cleared setting.
            conn.execute('update scheduling_config set timezone=null where tenant_id=%s', (tenant,))
            with pytest.raises(driver.errors.RaiseException, match='BLAM8_BOOKING_UNSUPPORTED'):
                conn.execute('select * from choose_offered_booking(%s,%s)', (a, tenant))
            assert conn.execute('select status from bookings where id in (%s,%s)', (a,b)).fetchall() == [('offered',), ('offered',)]
            conn.execute("update scheduling_config set timezone='Europe/London' where tenant_id=%s", (tenant,))
            # A changed service invokes the real reliability capacity trigger.
            conn.execute('update scheduling_config set capacity_per_window=1 where tenant_id=%s', (tenant,))
            insert(conn, tenant, start=56)
            conn.execute('update scheduling_config set capacity_per_window=2 where tenant_id=%s', (tenant,))
            contender = insert(conn, tenant, start=56, status='offered', expires_at='2099-01-01', contact='b@test.invalid')
            conn.execute('update scheduling_config set capacity_per_window=1 where tenant_id=%s', (tenant,))
            with pytest.raises(driver.errors.RaiseException, match='BLAM8_CAPACITY_CONFLICT'):
                conn.execute("select * from choose_offered_booking(%s,%s,'{\"Service\":\"Changed\"}')", (contender, tenant))
            assert conn.execute('select status from bookings where id=%s', (contender,)).fetchone() == ('offered',)
            assert conn.execute('select id from choose_offered_booking(%s,%s)', (a, tenant)).fetchall() == [(a,)]
            assert conn.execute('select status from bookings where id=%s', (b,)).fetchone() == ('cancelled',)
            conn.execute('delete from bookings')
            for name in reversed(names):
                migrate(conn, name, True)


@pytest.mark.parametrize('atomic', [False, True])
def test_accept_translates_capacity_for_both_writers(atomic):
    class Writer:
        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            raise RuntimeError('BLAM8_CAPACITY_CONFLICT: private database detail')

    with pytest.raises(CapacityConflict) as error:
        db.accept_offered_booking('booking', 'tenant', client=Writer(), atomic=atomic)
    assert 'private' not in str(error.value)


def test_choose_readiness_and_capacity_use_real_executor(no_db, choice, monkeypatch):
    session, offered, finalise = choice
    monkeypatch.setattr(scheduling.db, 'get_scheduling_config', lambda *a, **k: {})
    result = asyncio.run(portal_data.choose_held_time(session, 'b1'))
    assert result['error'] == 'unsupported'
    finalise.assert_not_awaited()
    from tests.booking_fixtures import carried_config
    monkeypatch.setattr(scheduling.db, 'get_scheduling_config', lambda *a, **k: carried_config())
    monkeypatch.setattr(portal_data, '_dep_groups', lambda *a: [])

    def conflict(*a, **k):
        raise CapacityConflict()

    monkeypatch.setattr(scheduling.db, 'accept_offered_booking', conflict)
    result = asyncio.run(portal_data.choose_held_time(session, 'b1'))
    assert result['error'] == 'offer_gone'
    finalise.assert_not_awaited()
    assert not no_db['holds']

    from app.booking_errors import VerificationUnavailable

    def unavailable(*a, **k):
        raise VerificationUnavailable('required evidence')

    monkeypatch.setattr(scheduling.db, 'accept_offered_booking', unavailable)
    result = asyncio.run(portal_data.choose_held_time(session, 'b1'))
    assert result['error'] == 'verification_unavailable'
    assert not no_db['holds'] and not no_db['released']
    finalise.assert_not_awaited()

    def unresolved(*a, **k):
        raise RuntimeError('BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved')

    monkeypatch.setattr(scheduling.db, 'accept_offered_booking', unresolved)
    result = asyncio.run(portal_data.choose_held_time(session, 'b1'))
    assert result['error'] == 'unsupported'
    finalise.assert_not_awaited()


def test_choose_could_not_verify_is_not_a_failed_rule(choice, monkeypatch):
    session, offered, finalise = choice
    monkeypatch.setattr(portal_data, '_diary_executor', lambda *a, **k: (
        lambda *a: {'error': 'could_not_verify', 'held': False}, {'bookings': []}))
    result = asyncio.run(portal_data.choose_held_time(session, 'b1'))
    assert result['error'] == 'could_not_verify'
    assert "couldn't verify" in result['message']
    finalise.assert_not_awaited()
