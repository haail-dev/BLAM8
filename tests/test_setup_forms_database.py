"""Ordinary forms serialize with cards and other writers in real PostgreSQL."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest
from tests.test_setup_cards_database import cluster, initialise, ROOT

FORWARD = ROOT / 'db/migrations/20261004_setup_forms.sql'
ROLLBACK = ROOT / 'db/migrations/20261004_setup_forms.rollback.sql'


def snapshot(c, tenant, page):
    return c.execute('select setup_form_snapshot(%s,%s)', (tenant, page)).fetchone()[0]


def save(c, tenant, page, before, operations):
    c.execute('select save_setup_form_checked(%s,%s,%s,%s)',
              (tenant, page, json.dumps(before), json.dumps(operations)))


def initialise_forms(c):
    initialise(c)
    c.execute('alter table requirement_instances add column data jsonb; '
              'alter table requirement_instances add column archived_at timestamptz')
    c.execute(FORWARD.read_text(encoding='utf-8'))


def test_related_writes_are_atomic_stale_forms_refused_and_migration_cycles(cluster):
    from app.setup_forms import operation
    driver, dsn = cluster
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    with driver.connect(dsn, autocommit=True) as c:
        initialise_forms(c)
        c.execute('insert into tenants values(%s),(%s)', (tenant, other))
        before = snapshot(c, tenant, 'booking_details')
        definition = operation('crm_field_definitions', {'entity':'contact','key':'reference','label':'Reference','field_type':'text'})
        config = operation('scheduling_config', {'intake_fields': [], 'booking_mode':'appointment'}, tenant)
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(c, tenant, 'booking_details', before, [definition, operation('crm_field_definitions', {'label':'Missing'}, str(uuid.uuid4())),config])
        assert snapshot(c, tenant, 'booking_details') == before
        save(c, tenant, 'booking_details', before, [definition, config])
        after = snapshot(c, tenant, 'booking_details')
        assert len(after['definitions']) == 1 and after['config']['booking_revision'] == 1
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(c, tenant, 'booking_details', before, [config])
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(c, other, 'crm_fields', snapshot(c,other,'crm_fields'), [operation('crm_field_definitions', identity=definition['id'],delete=True)])
        c.execute(ROLLBACK.read_text(encoding='utf-8'))
        c.execute(FORWARD.read_text(encoding='utf-8'))
        assert snapshot(c, tenant, 'booking_details') == after


def test_waiting_form_reads_winners_committed_version(cluster):
    from app.setup_forms import operation
    driver, dsn = cluster
    tenant = str(uuid.uuid4())
    with driver.connect(dsn, autocommit=True) as c:
        initialise_forms(c)
        c.execute('insert into tenants values(%s)',(tenant,))
        before = snapshot(c,tenant,'crm_settings')
    ready = threading.Event()
    def contender():
        with driver.connect(dsn) as c:
            ready.set()
            try:
                save(c,tenant,'crm_settings',before,[operation('tenant_config',{'crm_quiet_days':20},tenant)])
                return 'saved'
            except driver.errors.RaiseException as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as c:
            save(c,tenant,'crm_settings',before,[operation('tenant_config',{'crm_quiet_days':10},tenant)])
            future = pool.submit(contender)
            assert ready.wait(5)
            time.sleep(.1)
            assert not future.done()
        assert 'SETTINGS_STALE' in future.result(timeout=5)


def test_knowledge_rename_collision_stale_delete_and_failed_insert_are_atomic(cluster):
    driver, dsn = cluster
    tenant, other, mailbox = str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())
    with driver.connect(dsn, autocommit=True) as c:
        initialise_forms(c)
        c.execute('insert into tenants values(%s),(%s)',(tenant,other))
        c.execute("insert into knowledge_chunks(tenant_id,source,content,mailbox_id) values"
                  "(%s,'Hours','Old',null),(%s,'Terms','Keep',null),(%s,'Hours','Scoped',%s),(%s,'Hours','Other',null)",
                  (tenant,tenant,tenant,mailbox,other))
        def read():
            return c.execute("select knowledge_form_snapshot(%s,'Hours',null)",(tenant,)).fetchone()[0][0]
        before=read()
        def write(name,rows,version=before['version']):
            return c.execute('select save_knowledge_form_checked(%s,%s,%s,null,%s,%s)',
                              (tenant,'Hours',name,version,json.dumps(rows) if rows is not None else None))
        rows=[{'tenant_id':tenant,'source':'Renamed','content':text,'embedding':'[]','mailbox_id':None} for text in ['New','Second','Third']]
        with pytest.raises(driver.errors.RaiseException,match='SETTINGS_STALE'):
            write('Terms',rows)
        with pytest.raises(driver.errors.RaiseException,match='SETUP_INVALID'):
            write('Renamed',[{**rows[0],'tenant_id':other}])
        assert read()==before
        write('Renamed',rows)
        with pytest.raises(driver.errors.RaiseException,match='SETTINGS_STALE'):
            write('Hours',None)
        sources=c.execute('select tenant_id,source,content,mailbox_id from knowledge_chunks').fetchall()
        assert len(sources)==6
        assert {r[2] for r in sources}=={'Keep','Scoped','Other','New','Second','Third'}
        note=c.execute("select knowledge_form_snapshot(%s,'Renamed',null)",(tenant,)).fetchone()[0][0]
        assert note['content']==['New','Second','Third']
        c.execute(ROLLBACK.read_text(encoding='utf-8'))
        c.execute(FORWARD.read_text(encoding='utf-8'))
        assert c.execute('select tenant_id,source,content,mailbox_id from knowledge_chunks').fetchall()==sources


def test_form_rpcs_honour_tenant_row_security(cluster):
    from app.setup_forms import operation
    driver,dsn=cluster
    tenant,other=str(uuid.uuid4()),str(uuid.uuid4())
    with driver.connect(dsn,autocommit=True) as c:
        initialise_forms(c)
        c.execute('insert into tenants values(%s),(%s)',(tenant,other))
        c.execute('insert into tenant_config(tenant_id) values(%s),(%s)',(tenant,other))
        c.execute('create role forms_owner nologin')
        for table in ('tenant_config','crm_field_definitions','knowledge_chunks'):
            c.execute(f'grant select,insert,update,delete on {table} to forms_owner')
            c.execute(f'alter table {table} enable row level security')
            c.execute(f'create policy form_owner on {table} to forms_owner using(tenant_id in(select auth_tenant_ids())) with check(tenant_id in(select auth_tenant_ids()))')
        for signature in ('setup_form_snapshot(uuid,text)','save_setup_form_checked(uuid,text,jsonb,jsonb)',
                          'knowledge_form_snapshot(uuid,text,uuid)','save_knowledge_form_checked(uuid,text,text,uuid,text,jsonb)',
                          'replace_knowledge_source_atomic(uuid,text,uuid,jsonb)'):
            c.execute(f'grant execute on function {signature} to forms_owner')
        c.execute("insert into knowledge_chunks(tenant_id,source,content) values(%s,'Private','Private')",(other,))
        c.execute("select set_config('test.tenant',%s,false)",(tenant,))
        c.execute('set role forms_owner')
        assert c.execute('select knowledge_form_snapshot(%s)',(other,)).fetchone()[0]==[]
        with pytest.raises(driver.errors.InsufficientPrivilege):
            save(c,other,'crm_fields',{'page':'crm_fields','definitions':[]},[])
        save(c,tenant,'crm_fields',snapshot(c,tenant,'crm_fields'),[operation('crm_field_definitions',
            {'entity':'contact','key':'reference','label':'Reference','field_type':'text'})])
        rows=[{'tenant_id':other,'source':'New','content':'Do not insert','embedding':'[]','mailbox_id':None}]
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute("select save_knowledge_form_checked(%s,'New','New',null,'d751713988987e9331980363e24189ce',%s)",(other,json.dumps(rows)))
        c.execute('reset role')
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s',(other,)).fetchone()==(1,)
