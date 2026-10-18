import json
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_admin_outreach_sql import cluster
from test_tenant_outreach_sql import outreach_cluster


@pytest.fixture(scope='module')
def plan_cluster(outreach_cluster):
    with outreach_cluster.cursor() as q:
        q.execute('create table tenant_members(tenant_id uuid,user_id uuid,role text,active boolean default true)')
        q.execute('create table invoices(id uuid primary key,tenant_id uuid,status text)')
        text=Path('db/migrations/20261010_scheduled_plan.sql').read_text(encoding='utf-8')
        q.execute(text);q.execute(text)
    yield outreach_cluster


@pytest.fixture
def sql(plan_cluster):
    plan_cluster.autocommit=False
    q=plan_cluster.cursor()
    yield q
    plan_cluster.rollback();q.close();plan_cluster.autocommit=True


def setup(q,enabled=True):
    tid,iid,uid,token=[str(uuid.uuid4()) for _ in range(4)]
    q.execute('insert into tenants(id) values(%s)',(tid,))
    q.execute("insert into tenant_members(tenant_id,user_id,role) values(%s,%s,'owner')",(tid,uid))
    q.execute("insert into subscriptions(tenant_id,module,status) values(%s,'crm','active')",(tid,))
    cfg={'timezone':'UTC','working_days':list(range(7)),'holidays':[],'start_time':'00:00','end_time':'23:59','daily_cap':50,'min_gap_seconds':0}
    q.execute('insert into schedule_settings(tenant_id,enabled,config) values(%s,%s,%s)',(tid,enabled,json.dumps(cfg)))
    q.execute("insert into schedule_items(id,tenant_id,title,kind,next_at,created_by) values(%s,%s,'Test','task',now()-interval '1 minute',%s) returning next_at",(iid,tid,uid));due=q.fetchone()[0]
    return tid,iid,due,token


def reserve(q,ids):
    t,i,d,k=ids
    q.execute('select id from schedule_reserve(%s,%s,%s,null,%s)',(t,i,d,k))
    row=q.fetchone();return str(row[0]) if row else None


def ready(q,ids):
    r=reserve(q,ids);q.execute("select schedule_prepare(%s,%s,%s,'{}')",(ids[0],r,ids[3]));assert q.fetchone()[0]
    return r


def claim(q,tid,r,auto=False):
    q.execute('select schedule_claim(%s,%s,%s)',(tid,r,auto));return q.fetchone()[0]


def test_disabled_reserves_nothing(sql):
    ids=setup(sql,False);assert reserve(sql,ids) is None


def test_once_and_revision_guards(sql):
    ids=setup(sql);r=ready(sql,ids)
    assert reserve(sql,ids) is None
    sql.execute('update schedule_items set revision=revision+1 where id=%s',(ids[1],))
    assert claim(sql,ids[0],r)=='Item paused or changed'


def test_pause_and_auto_gate(sql):
    ids=setup(sql);r=ready(sql,ids)
    assert claim(sql,ids[0],r,True)=='Approval is required'
    sql.execute('update schedule_settings set enabled=false where tenant_id=%s',(ids[0],))
    assert claim(sql,ids[0],r)=='Scheduled plan is paused'


def test_manual_approval_claims_once_and_complete_is_idempotent(sql):
    ids=setup(sql);r=ready(sql,ids)
    assert claim(sql,ids[0],r)=='claimed'
    assert claim(sql,ids[0],r)=='This occurrence is not ready'
    sql.execute("select schedule_complete(%s,%s,'Created task',false)",(ids[0],r));assert sql.fetchone()[0]
    sql.execute("select schedule_complete(%s,%s,'Created task',false)",(ids[0],r));assert not sql.fetchone()[0]


def test_cross_tenant_claim_and_rpc_privileges(sql):
    ids=setup(sql);r=ready(sql,ids);other=setup(sql)
    assert claim(sql,other[0],r)=='Run not found'
    sql.execute("select has_function_privilege('authenticated','schedule_claim(uuid,uuid,boolean)','execute')")
    assert sql.fetchone()==(False,)
    sql.execute("select has_table_privilege('authenticated','schedule_items','update')")
    assert sql.fetchone()==(False,)


def test_cap_and_uncertainty_are_shared_across_items(sql):
    ids=setup(sql);r=ready(sql,ids)
    sql.execute("update schedule_runs set status='unknown' where id=%s",(r,))
    iid=str(uuid.uuid4());tok=str(uuid.uuid4())
    sql.execute("insert into schedule_items(id,tenant_id,title,kind,next_at,created_by) select %s,tenant_id,'Another','task',now()-interval '1 minute',created_by from schedule_items where id=%s returning next_at",(iid,ids[1]));due=sql.fetchone()[0]
    r2=ready(sql,(ids[0],iid,due,tok))
    assert claim(sql,ids[0],r2)=='Another outcome needs review'
    sql.execute("update schedule_runs set status='done',attempted_at=now() where id=%s",(r,))
    sql.execute("update schedule_settings set config=jsonb_set(config,'{daily_cap}','1') where tenant_id=%s",(ids[0],))
    assert claim(sql,ids[0],r2)=='Daily cap reached'


def test_preparation_recovery_cannot_repeat_an_attempt(sql):
    ids=setup(sql);r=ready(sql,ids)
    sql.execute('select id from schedule_retry_prepare(%s,%s,%s)',(ids[0],r,str(uuid.uuid4())));assert sql.fetchone() is None
    sql.execute("update schedule_runs set status='blocked' where id=%s",(r,))
    sql.execute('select id from schedule_retry_prepare(%s,%s,%s)',(ids[0],r,str(uuid.uuid4())));assert sql.fetchone()


def test_parallel_workers_reserve_one_occurrence(plan_cluster):
    import psycopg2
    with plan_cluster.cursor() as q:ids=setup(q)
    def work(_):
        c=psycopg2.connect(**plan_cluster.get_dsn_parameters());c.autocommit=True
        try:
            with c.cursor() as q:return reserve(q,ids)
        finally:c.close()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(work,range(4)))
    assert sum(bool(r) for r in results)==1


def test_atomic_draft_and_late_stop(sql):
    ids=setup(sql);t,i,d,k=ids
    box,contact=str(uuid.uuid4()),str(uuid.uuid4())
    sql.execute("insert into email_connections(id,tenant_id,address) values(%s,%s,'me@example.com')",(box,t))
    sql.execute("insert into crm_contacts(id,tenant_id,email) values(%s,%s,'person@example.com')",(contact,t))
    sql.execute("update schedule_items set kind='email',mode='auto',payload=%s where id=%s",(json.dumps({'contact_id':contact}),i))
    r=reserve(sql,ids)
    data=json.dumps({'to':'person@example.com','subject':'Test','body':'Draft','mailbox_id':box,'sender':'me@example.com'})
    sql.execute('select schedule_prepare(%s,%s,%s,%s)',(t,r,str(uuid.uuid4()),data));assert not sql.fetchone()[0]
    sql.execute('select schedule_prepare(%s,%s,%s,%s)',(t,r,k,data));assert sql.fetchone()[0]
    sql.execute('select schedule_prepare(%s,%s,%s,%s)',(t,r,k,data));assert not sql.fetchone()[0]
    sql.execute('select count(*) from email_log where tenant_id=%s',(t,));assert sql.fetchone()==(1,)
    assert claim(sql,t,r,True)=='Mailbox requires approval'
    sql.execute('insert into tenant_config(tenant_id,auto_send) values(%s,true)',(t,))
    sql.execute('update crm_contacts set do_not_sequence=true where id=%s',(contact,))
    assert claim(sql,t,r)=='Contact is stopped'
    sql.execute('update crm_contacts set do_not_sequence=false where id=%s',(contact,))
    assert claim(sql,t,r,True)=='claimed'
    sql.execute("select schedule_complete(%s,%s,'Sent',true)",(t,r));assert sql.fetchone()[0]
    sql.execute('select status,sent_via from email_log where tenant_id=%s',(t,));assert sql.fetchone()==('sent','auto')


def test_tenant_rls_hides_other_plans(sql):
    first,second=setup(sql),setup(sql)
    sql.execute("select set_config('test.tenant',%s,true)",(first[0],))
    sql.execute('set local role authenticated')
    sql.execute('select tenant_id::text from schedule_items')
    assert sql.fetchall()==[(first[0],)]


def test_future_skip_is_recorded_once(sql):
    ids=setup(sql,False)
    sql.execute('select schedule_skip_future(%s,%s,%s,null)',ids[:3]);assert sql.fetchone()[0]
    sql.execute('select schedule_skip_future(%s,%s,%s,null)',ids[:3]);assert not sql.fetchone()[0]
    sql.execute('select status,result from schedule_runs where item_id=%s',(ids[1],))
    assert sql.fetchone()==('skipped','Skipped by the owner')


def test_reconciliation_requires_pause_delay_and_never_retries(sql):
    ids=setup(sql);r=ready(sql,ids);assert claim(sql,ids[0],r)=='claimed'
    sql.execute('select schedule_reconcile(%s,%s,false)',(ids[0],r));assert not sql.fetchone()[0]
    sql.execute('update schedule_settings set enabled=false where tenant_id=%s',(ids[0],))
    sql.execute('select schedule_reconcile(%s,%s,false)',(ids[0],r));assert not sql.fetchone()[0]
    sql.execute("update schedule_runs set attempted_at=now()-interval '6 minutes' where id=%s",(r,))
    sql.execute('select schedule_reconcile(%s,%s,false)',(ids[0],r));assert sql.fetchone()[0]
    sql.execute('select id from schedule_retry_prepare(%s,%s,%s)',(ids[0],r,str(uuid.uuid4())));assert sql.fetchone() is None


def test_late_payment_and_owner_gates(sql):
    ids=setup(sql);invoice=str(uuid.uuid4())
    sql.execute("insert into invoices values(%s,%s,'draft')",(invoice,ids[0]))
    sql.execute('update schedule_items set dependency=%s where id=%s',(json.dumps({'kind':'after_payment','invoice_id':invoice}),ids[1]))
    r=ready(sql,ids)
    assert claim(sql,ids[0],r)=='Waiting for payment.'
    sql.execute("update invoices set status='paid' where id=%s",(invoice,))
    sql.execute("update tenant_members set role='staff' where tenant_id=%s",(ids[0],))
    assert claim(sql,ids[0],r)=='Planner owner no longer has permission'


def test_reply_arriving_after_preparation_blocks_claim(sql):
    ids=setup(sql);cid=str(uuid.uuid4())
    sql.execute("insert into crm_contacts(id,tenant_id,email) values(%s,%s,'Person@example.com')",(cid,ids[0]))
    sql.execute('update schedule_items set payload=%s,dependency=%s where id=%s',
                (json.dumps({'contact_id':cid}),json.dumps({'kind':'if_no_reply','since':'2026-01-01T00:00:00Z'}),ids[1]))
    r=ready(sql,ids)
    sql.execute("insert into email_log(tenant_id,from_address,kind) values(%s,'person@example.com','inbound')",(ids[0],))
    assert claim(sql,ids[0],r)=='A reply has arrived; review this occurrence.'
