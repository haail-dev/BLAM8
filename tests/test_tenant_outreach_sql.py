"""Real PostgreSQL reservation/isolation tests; never connects to production."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_admin_outreach_sql import cluster  # disposable localhost cluster only
from app.tenant_outreach import validate_config


@pytest.fixture(scope="module")
def outreach_cluster(cluster):
    with cluster.cursor() as q:
        q.execute("""
        create table tenants(id uuid primary key);
        create function auth_tenant_ids() returns setof uuid language sql stable as
          $$select nullif(current_setting('test.tenant',true),'')::uuid$$;
        create table crm_sequences(id uuid primary key,tenant_id uuid references tenants(id),
          status text default 'active',trigger_type text default 'manual');
        create table crm_contacts(id uuid primary key,tenant_id uuid references tenants(id),email text,do_not_sequence boolean default false);
        create table email_connections(id uuid primary key,tenant_id uuid references tenants(id),address text,send_as_address text,sync_failing_since timestamptz);
        create table crm_sequence_steps(id uuid primary key,tenant_id uuid,sequence_id uuid,step_order integer,
          delay_days integer default 0,action_type text default 'send_email',exit_on_reply boolean default true,
          template_subject text default 'Hello',template_body text default 'Introduce the business');
        create table crm_sequence_enrollments(id uuid primary key,tenant_id uuid references tenants(id),sequence_id uuid,
          contact_id uuid,status text default 'active',current_step integer default 0,enrolled_at timestamptz default now(),next_action_at timestamptz default now());
        create table email_log(id uuid primary key default gen_random_uuid(),tenant_id uuid references tenants(id),from_address text,
          subject text,body_in text,draft_reply text,status text default 'pending',kind text default 'inbound',mailbox_id uuid,
          source_message_id text,created_at timestamptz default now(),sent_at timestamptz,sent_via text);
        create table subscriptions(tenant_id uuid,module text,status text);
        create table tenant_config(tenant_id uuid primary key,auto_send boolean default false);
        create table mailbox_config(mailbox_id uuid primary key,tenant_id uuid,auto_send boolean);
        create table ignored_contacts(tenant_id uuid,address text);
        grant all on all tables in schema public to service_role;
        grant select,insert,update on crm_sequences,crm_sequence_enrollments to authenticated;
        """)
        migration = Path('db/migrations/20261010_tenant_outreach.sql').read_text(encoding='utf-8')
        q.execute(migration)
        q.execute(migration)
    yield cluster


@pytest.fixture
def sql(outreach_cluster):
    outreach_cluster.autocommit = False
    q = outreach_cluster.cursor()
    yield q
    outreach_cluster.rollback()
    q.close()
    outreach_cluster.autocommit = True


def setup(q, *, other_tenant=None):
    ids = {k: str(uuid.uuid4()) for k in ('tenant','seq','contact','box','step','enr','list','target','campaign','token')}
    if other_tenant:
        ids['tenant'] = other_tenant
    tid = ids['tenant']
    cfg = validate_config({'mode':'auto','timezone':'UTC','working_days':list(range(7)),
                           'start_time':'00:00','end_time':'23:59','min_gap_seconds':60})
    q.execute('insert into tenants values(%s) on conflict do nothing', (tid,))
    q.execute("insert into subscriptions values(%s,'crm','active')",(tid,))
    q.execute('insert into tenant_config values(%s,true) on conflict do nothing',(tid,))
    q.execute('insert into outreach_settings(tenant_id,enabled,last_sync_at) values(%s,true,now()) on conflict do nothing',(tid,))
    q.execute("insert into crm_sequences(id,tenant_id,outreach_managed) values(%s,%s,true)",(ids['seq'],tid))
    q.execute("insert into crm_contacts(id,tenant_id,email) values(%s,%s,%s)",(ids['contact'],tid,ids['contact']+'@example.com'))
    q.execute("insert into email_connections(id,tenant_id,address) values(%s,%s,'sender@example.com')",(ids['box'],tid))
    q.execute('insert into crm_sequence_steps(id,tenant_id,sequence_id,step_order) values(%s,%s,%s,0)',(ids['step'],tid,ids['seq']))
    q.execute('select to_jsonb(s) from crm_sequence_steps s where id=%s',(ids['step'],)); step=q.fetchone()[0]
    q.execute('insert into crm_sequence_enrollments(id,tenant_id,sequence_id,contact_id) values(%s,%s,%s,%s)',(ids['enr'],tid,ids['seq'],ids['contact']))
    q.execute("insert into outreach_lists(id,tenant_id,name) values(%s,%s,'Test')",(ids['list'],tid))
    q.execute("""insert into outreach_targets(id,tenant_id,list_id,contact_id,email,basis,evidence,email_source,company_number,email_verified,soft_match)
      values(%s,%s,%s,%s,%s,'corporate','Company register','Published address','12345678',true,false)""",
      (ids['target'],tid,ids['list'],ids['contact'],ids['contact']+'@example.com'))
    q.execute("""insert into outreach_campaigns(id,tenant_id,list_id,sequence_id,mailbox_id,name,enabled,config)
      values(%s,%s,%s,%s,%s,'Campaign',true,%s::jsonb)""",(ids['campaign'],tid,ids['list'],ids['seq'],ids['box'],json.dumps(cfg)))
    q.execute('select id from outreach_begin_step(%s,%s,%s,%s,0,%s::jsonb,%s::jsonb,%s,%s,%s,%s)',
      (tid,ids['campaign'],ids['enr'],ids['target'],json.dumps(step),json.dumps(cfg),ids['contact']+'@example.com',ids['box'],'sender@example.com',ids['token']))
    ids['message']=str(q.fetchone()[0])
    q.execute("select outreach_finish_draft(%s,%s,%s,'Hello','Reviewed body')",(tid,ids['message'],ids['token']))
    ids['log']=str(q.fetchone()[0])
    return ids


def claim(q, ids, auto=False):
    q.execute('select outreach_claim(%s,%s,%s)',(ids['tenant'],ids['message'],auto))
    return q.fetchone()[0]


def test_completion_and_approval_are_idempotent(sql):
    ids=setup(sql)
    assert claim(sql,ids)=='claimed'
    assert claim(sql,ids)!='claimed'
    for _ in range(2):
        sql.execute("select outreach_complete(%s,%s,'Approved body',false)",(ids['tenant'],ids['message']))
    sql.execute('select status,sent_via,draft_reply from email_log where id=%s',(ids['log'],))
    assert sql.fetchone()==('sent','human','Approved body')
    sql.execute('select current_step from crm_sequence_enrollments where id=%s',(ids['enr'],))
    assert sql.fetchone()==(0,)  # existing engine owns advancement


@pytest.mark.parametrize('mutation,reason',[
 ("update outreach_settings set enabled=false",'switched off'),
 ("update outreach_campaigns set enabled=false",'paused'),
 ("update subscriptions set status='inactive'",'CRM'),
 ("update outreach_settings set last_sync_at=now()-interval '3 minutes'",'sync'),
 ("update crm_sequence_enrollments set status='paused'",'Enrollment'),
 ("update crm_sequences set status='paused'",'Sequence'),
 ("update crm_sequence_steps set exit_on_reply=false",'Sequence changed'),
 ("update outreach_targets set email_verified=false",'verification'),
 ("update outreach_targets set soft_match=true",'verification'),
 ("update crm_contacts set do_not_sequence=true",'blocked'),
 ("update crm_contacts set email='changed@example.com'",'changed'),
 ("update email_connections set sync_failing_since=now()",'mailbox'),
 ("update email_connections set send_as_address='changed@example.com'",'mailbox'),
 ("update email_log set status='skipped'",'pending'),
 ("update outreach_settings set last_attempt_at=now()",'gap'),
 ("update outreach_campaigns set config=jsonb_set(config,'{mode}','\"draft\"')",'changed'),
])
def test_current_send_gates_apply_to_human_approvals(sql,mutation,reason):
    ids=setup(sql);sql.execute(mutation)
    assert reason.lower() in claim(sql,ids).lower()
    sql.execute('select attempted_at from outreach_messages where id=%s',(ids['message'],))
    assert sql.fetchone()==(None,)


def test_window_enforced_at_approval(sql):
    ids=setup(sql)
    for table in ['outreach_campaigns','outreach_messages']:
        col='config' if table=='outreach_campaigns' else 'config_snapshot'
        sql.execute(f"update {table} set {col}=jsonb_set({col},'{{working_days}}','[]')")
    assert claim(sql,ids)=='Outside working window'


def test_permission_basis_and_auto_ladder(sql):
    ids=setup(sql)
    sql.execute("update outreach_targets set basis='permission',company_number=''")
    sql.execute('update tenant_config set auto_send=false')
    assert claim(sql,ids,True)=='Approval required'
    assert claim(sql,ids,False)=='claimed'


@pytest.mark.parametrize('kind',['suppression','ignored','reply'])
def test_permanent_stop_and_late_reply_block_pending_draft(sql,kind):
    ids=setup(sql);address=ids['contact']+'@example.com'
    if kind=='suppression':
        sql.execute("insert into outreach_suppressions(tenant_id,email,reason) values(%s,%s,'stop')",(ids['tenant'],address))
    elif kind=='ignored':
        sql.execute('insert into ignored_contacts values(%s,%s)',(ids['tenant'],address))
    else:
        sql.execute("insert into email_log(tenant_id,from_address) values(%s,%s)",(ids['tenant'],address.upper()))
    assert claim(sql,ids) in ('Recipient must not be contacted','Recipient replied')


def test_shared_cap_and_uncertain_hold_across_campaigns(sql):
    a=setup(sql);b=setup(sql,other_tenant=a['tenant'])
    assert claim(sql,a)=='claimed'
    assert claim(sql,b)=='Reconcile an uncertain send first'
    sql.execute("update outreach_messages set status='sent' where id=%s",(a['message'],))
    sql.execute('update outreach_settings set daily_cap=1,last_attempt_at=null')
    assert claim(sql,b)=='Shared outreach cap reached'


def test_other_tenant_cannot_claim_or_read(sql):
    a=setup(sql);b=setup(sql)
    sql.execute('select outreach_claim(%s,%s,false)',(a['tenant'],b['message']))
    assert sql.fetchone()[0]=='This draft is not available to send'
    sql.execute("select set_config('test.tenant',%s,true)",(a['tenant'],))
    sql.execute('set local role authenticated')
    sql.execute('select distinct tenant_id::text from outreach_messages')
    assert sql.fetchall()==[(a['tenant'],)]


def test_browser_cannot_forge_claims_or_disable_managed_guard(sql):
    ids=setup(sql)
    sql.execute('set local role authenticated')
    with pytest.raises(Exception,match='permission denied'):
        claim(sql,ids)


def test_browser_cannot_write_settings(sql):
    setup(sql);sql.execute('set local role authenticated')
    with pytest.raises(Exception,match='permission denied'):
        sql.execute('update outreach_settings set enabled=true')


def test_browser_cannot_remove_sequence_marker(sql):
    ids=setup(sql);sql.execute('set local role authenticated')
    with pytest.raises(Exception,match='managed by the application'):
        sql.execute('update crm_sequences set outreach_managed=false where id=%s',(ids['seq'],))


def test_unique_enrollment_even_after_completion(sql):
    ids=setup(sql)
    sql.execute("update crm_sequence_enrollments set status='completed'")
    with pytest.raises(Exception,match='outreach_enrollment_once'):
        sql.execute('insert into crm_sequence_enrollments(id,tenant_id,sequence_id,contact_id) values(%s,%s,%s,%s)',
          (str(uuid.uuid4()),ids['tenant'],ids['seq'],ids['contact']))


def test_cross_tenant_target_link_rejected(sql):
    a=setup(sql);b=setup(sql)
    with pytest.raises(Exception,match='foreign key'):
        sql.execute('update outreach_targets set contact_id=%s where id=%s',(b['contact'],a['target']))


def test_service_role_can_claim_but_cannot_cross_tenant(sql):
    a=setup(sql);b=setup(sql)
    sql.execute('set local role service_role')
    assert claim(sql,a)=='claimed'
    sql.execute('select outreach_claim(%s,%s,false)',(a['tenant'],b['message']))
    assert sql.fetchone()[0]=='This draft is not available to send'


def test_stale_composer_cannot_overwrite_new_draft(sql):
    ids=setup(sql)
    sql.execute("update outreach_messages set status='preparing',work_token=%s,lease_until=now()+interval '5 minutes' where id=%s",
                (str(uuid.uuid4()),ids['message']))
    sql.execute("select outreach_finish_draft(%s,%s,%s,'Wrong','Stale worker')",(ids['tenant'],ids['message'],ids['token']))
    assert sql.fetchone()==(None,)
    sql.execute('select draft_reply from email_log where id=%s',(ids['log'],))
    assert sql.fetchone()==('Reviewed body',)


def test_campaign_creation_is_atomic_and_claims_only_unused_sequence(sql):
    ids=setup(sql)
    sid=str(uuid.uuid4())
    sql.execute("insert into crm_sequences(id,tenant_id) values(%s,%s)",(sid,ids['tenant']))
    sql.execute('insert into crm_sequence_steps(id,tenant_id,sequence_id,step_order) values(%s,%s,%s,0)',
                (str(uuid.uuid4()),ids['tenant'],sid))
    cfg=json.dumps(validate_config({}))
    sql.execute("select id,enabled from outreach_create_campaign(%s,'New',%s,%s,%s,%s::jsonb)",
                (ids['tenant'],ids['list'],sid,ids['box'],cfg))
    assert sql.fetchone()[1] is False
    sql.execute('select outreach_managed from crm_sequences where id=%s',(sid,));assert sql.fetchone()==(True,)
    sql.execute("select id from outreach_create_campaign(%s,'Again',%s,%s,%s,%s::jsonb)",
                (ids['tenant'],ids['list'],sid,ids['box'],cfg))
    assert sql.fetchall()==[]


def test_wrong_mailbox_rolls_back_campaign_marker(sql):
    ids=setup(sql);other=setup(sql)
    sid=str(uuid.uuid4())
    sql.execute("insert into crm_sequences(id,tenant_id) values(%s,%s)",(sid,ids['tenant']))
    sql.execute('insert into crm_sequence_steps(id,tenant_id,sequence_id,step_order) values(%s,%s,%s,0)',
                (str(uuid.uuid4()),ids['tenant'],sid))
    sql.execute('savepoint before_create')
    with pytest.raises(Exception,match='foreign key'):
        sql.execute("select id from outreach_create_campaign(%s,'Wrong',%s,%s,%s,%s::jsonb)",
                    (ids['tenant'],ids['list'],sid,other['box'],json.dumps(validate_config({}))))
    sql.execute('rollback to savepoint before_create')
    sql.execute('select outreach_managed from crm_sequences where id=%s',(sid,));assert sql.fetchone()==(False,)


def test_concurrent_workers_only_one_provider_reservation(outreach_cluster):
    import psycopg2
    with outreach_cluster.cursor() as q:
        ids=setup(q)
    def reserve(_):
        with psycopg2.connect(**outreach_cluster.get_dsn_parameters()) as c:
            with c.cursor() as q:
                return claim(q,ids)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve,range(2)))
        assert results.count('claimed')==1
    finally:
        # Dedicated disposable cluster; leave the committed fixture for teardown.
        pass
