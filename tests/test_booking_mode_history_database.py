"""Explicit history mappings preserve spans, fail atomically, and reverse exactly."""
import json
import uuid
import pytest
from tests.test_setup_cards_database import cluster, initialise, ROOT
from tests.test_booking_capacity_database import insert
from app.intake import STAY_FIELD

FORWARD = ROOT / 'db/migrations/20261004_booking_mode_history.sql'
ROLLBACK = ROOT / 'db/migrations/20261004_booking_mode_history.rollback.sql'


def snapshot(c,t):
    return c.execute('select booking_mode_snapshot(%s)',(t,)).fetchone()[0]


def apply(c,t,before,patch,mapping):
    return c.execute('select migrate_booking_mode(%s,%s,%s,%s)',
        (t,json.dumps(before),json.dumps(patch),json.dumps(mapping))).fetchone()[0]


def setup(c):
    initialise(c)
    c.execute(FORWARD.read_text(encoding='utf-8'))
    tenant=str(uuid.uuid4())
    c.execute('insert into tenants values(%s)',(tenant,))
    c.execute("insert into scheduling_config(tenant_id,capacity_per_window,open_time,close_time) values(%s,2,'09:00','17:00')",(tenant,))
    return tenant


def patch(mode='stay',capacity=2,pools=None,services=None):
    return {'booking_mode':mode,'intake_fields':[STAY_FIELD] if mode=='stay' else [],
            'capacity_per_window':capacity,'resource_types':pools or [],'service_types':services or []}


def test_legacy_and_multiday_history_roundtrip_and_install_cycle(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        tenant=setup(c)
        legacy=insert(c,tenant,start=None,length=None,window='am',status='completed')
        live=insert(c,tenant,start=40,length=120)
        before=snapshot(c,tenant)
        mapping=[{'id':str(b),'resource_type':None} for b in (legacy,live)]
        receipt=apply(c,tenant,before,patch(),mapping)
        after=snapshot(c,tenant)
        bad=patch('appointment')
        bad['intake_fields']=[{'label':'Reference','required':True,'services':['Stays']}]
        with pytest.raises(driver.errors.RaiseException,match='Stays-only'):
            apply(c,tenant,after,bad,mapping)
        assert snapshot(c,tenant)==after
        by={b['id']:b for b in after['bookings']}
        assert (by[str(legacy)]['start_block'],by[str(legacy)]['length_blocks'])==(36,12)
        assert (by[str(live)]['start_block'],by[str(live)]['length_blocks'])==(40,120)
        for old in before['bookings']:
            assert {k:v for k,v in by[old['id']].items() if k not in ('start_block','length_blocks')} == {
                k:v for k,v in old.items() if k not in ('start_block','length_blocks')}
        with pytest.raises(driver.errors.RaiseException,match='roll back active'):
            c.execute(ROLLBACK.read_text(encoding='utf-8'))
        c.execute('rollback')
        c.execute('select rollback_booking_mode(%s)',(receipt,))
        assert snapshot(c,tenant)==before
        c.execute(ROLLBACK.read_text(encoding='utf-8'))
        c.execute(FORWARD.read_text(encoding='utf-8'))
        receipt=apply(c,tenant,before,patch(),mapping)
        # Reverse direction is a new explicit mapping, preserving a multi-day span.
        second=apply(c,tenant,snapshot(c,tenant),patch('appointment'),mapping)
        assert snapshot(c,tenant)['config']['booking_mode']=='appointment'
        c.execute('select rollback_booking_mode(%s)',(second,))
        c.execute('select rollback_booking_mode(%s)',(receipt,))
        assert snapshot(c,tenant)==before


def test_capacity_mapping_conflict_stale_snapshot_and_rollback_refuse_without_partial_writes(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        tenant=setup(c)
        one=insert(c,tenant,start=40,length=4)
        two=insert(c,tenant,start=40,length=4)
        before=snapshot(c,tenant)
        mapping=[{'id':str(b),'resource_type':None} for b in (one,two)]
        with pytest.raises(driver.errors.RaiseException,match='CAPACITY_CONFLICT'):
            apply(c,tenant,before,patch(capacity=1),mapping)
        assert snapshot(c,tenant)==before
        with pytest.raises(driver.errors.RaiseException,match='map every booking'):
            apply(c,tenant,before,patch(),mapping[:1])
        assert snapshot(c,tenant)==before
        c.execute("update bookings set notes='New information' where id=%s",(one,))
        with pytest.raises(driver.errors.RaiseException,match='SETTINGS_STALE'):
            apply(c,tenant,before,patch(),mapping)
        receipt=apply(c,tenant,snapshot(c,tenant),patch(),mapping)
        c.execute("update bookings set notes='Later information' where id=%s",(two,))
        changed=snapshot(c,tenant)
        with pytest.raises(driver.errors.RaiseException,match='SETTINGS_STALE'):
            c.execute('select rollback_booking_mode(%s)',(receipt,))
        assert snapshot(c,tenant)==changed


def test_named_pool_mapping_and_external_reservations(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        tenant=setup(c)
        appt=insert(c,tenant,start=40,length=4,details='{"Service":"Consultation"}')
        ext=insert(c,tenant,start=None,length=None,window='unknown',source='calendar',resource_type='External')
        before=snapshot(c,tenant)
        target=patch(pools=[{'name':'Consultation','count':1}],services=[{'name':'Consultation','resource':'Consultation','timing':'time','minutes':60}])
        mapping=[{'id':str(appt),'resource_type':'Consultation'},{'id':str(ext),'resource_type':'External'}]
        receipt=apply(c,tenant,before,target,mapping)
        after=snapshot(c,tenant)
        assert next(b for b in after['bookings'] if b['id']==str(ext))==next(b for b in before['bookings'] if b['id']==str(ext))
        assert next(b for b in after['bookings'] if b['id']==str(appt))['resource_type']=='Consultation'
        c.execute('select rollback_booking_mode(%s)',(receipt,))
        assert snapshot(c,tenant)==before


def test_installed_no_defaults_readiness_and_private_admin_permissions(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        initialise(c)
        c.execute('create table subscriptions(tenant_id uuid,module text,status text)')
        for role in ('anon','authenticated','service_role'):
            c.execute(f'create role {role} nologin')
        c.execute((ROOT/'db/migrations/20260927_no_defaults.sql').read_text(encoding='utf-8'))
        c.execute(FORWARD.read_text(encoding='utf-8'))
        tenant=str(uuid.uuid4())
        c.execute('insert into tenants values(%s)',(tenant,))
        c.execute("insert into scheduling_config(tenant_id,capacity_per_window,timezone,working_days,open_time,close_time) "
                  "values(%s,2,'Europe/London','mon,tue,wed,thu,fri','09:00','17:00')",(tenant,))
        booking=insert(c,tenant)
        before=snapshot(c,tenant)
        mapping=[{'id':str(booking),'resource_type':None}]
        receipt=apply(c,tenant,before,patch(),mapping)
        c.execute('select rollback_booking_mode(%s)',(receipt,))
        assert snapshot(c,tenant)==before
        c.execute('update scheduling_config set timezone=null where tenant_id=%s',(tenant,))
        unresolved=snapshot(c,tenant)
        with pytest.raises(driver.errors.RaiseException,match='BOOKING_UNSUPPORTED'):
            apply(c,tenant,unresolved,patch(),mapping)
        assert snapshot(c,tenant)==unresolved
        for role in ('anon','authenticated','service_role'):
            c.execute(f'set role {role}')
            with pytest.raises(driver.errors.InsufficientPrivilege): c.execute('select * from booking_mode_migrations')
            with pytest.raises(driver.errors.InsufficientPrivilege): c.execute('select booking_mode_snapshot(%s)',(tenant,))
            with pytest.raises(driver.errors.InsufficientPrivilege): apply(c,tenant,unresolved,patch(),mapping)
            with pytest.raises(driver.errors.InsufficientPrivilege): c.execute('select rollback_booking_mode(%s)',(receipt,))
            c.execute('reset role')


def test_mapping_that_cannot_reverse_through_normal_triggers_is_never_accepted(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        tenant=setup(c)
        c.execute('update scheduling_config set resource_types=%s where tenant_id=%s',
                  (json.dumps([{'name':'A','count':1},{'name':'B','count':2}]),tenant))
        one=insert(c,tenant,id=uuid.UUID(int=1),resource_type='A',quantity=1)
        two=insert(c,tenant,id=uuid.UUID(int=2),resource_type='B',quantity=2)
        before=snapshot(c,tenant)
        target=patch(pools=[{'name':'A','count':2},{'name':'B','count':3}])
        mapping=[{'id':str(one),'resource_type':'B'},{'id':str(two),'resource_type':'A'}]
        with pytest.raises(driver.errors.RaiseException,match='CAPACITY_CONFLICT'):
            apply(c,tenant,before,target,mapping)
        assert snapshot(c,tenant)==before
        assert c.execute('select count(*) from booking_mode_migrations').fetchone()==(0,)
