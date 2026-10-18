"""C3-T03/T05/T07: actual disposable PostgreSQL RPC/RLS races, never a supplied DSN."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from tests.test_booking_capacity_database import cluster

MIGRATION = Path("db/migrations/20261011_concierge_delegation.sql")
ROLLBACK = Path("db/migrations/20261011_concierge_delegation.rollback.sql")


@pytest.fixture(scope="module")
def delegation_cluster(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""
          create schema if not exists auth;
          create role authenticated nologin;
          create role service_role nologin bypassrls;
          create function auth.uid() returns uuid language sql stable as
            'select nullif(current_setting(''request.jwt.claim.sub'',true),'''')::uuid';
          create function auth_tenant_ids() returns setof uuid language sql stable as
            'select nullif(current_setting(''app.test.tenant'',true),'''')::uuid';
          create table tenants(id uuid primary key);
          create table concierge_tasks(id uuid primary key default gen_random_uuid(),tenant_id uuid references tenants,
            user_id uuid,batch_id uuid,position integer default 0,title text,instruction text,status text default 'queued',
            outcome text,proposal_id uuid,surface text default 'typed',created_at timestamptz default now(),
            started_at timestamptz,finished_at timestamptz);
          create table concierge_proposals(id uuid primary key default gen_random_uuid(),tenant_id uuid references tenants,
            user_id uuid,kind text,summary text,payload jsonb default '{}',status text default 'pending',
            result jsonb,created_at timestamptz default now(),executed_at timestamptz);
          alter table concierge_tasks enable row level security;
          alter table concierge_proposals enable row level security;
          grant usage on schema public,auth to authenticated,service_role;
          grant select,insert,update,delete on concierge_tasks,concierge_proposals to authenticated,service_role;
        """)
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())  # policy/function/column rerun is idempotent
    return driver, dsn


def rpc(conn, operation, tenant, user, task=None, revision=None, attempt=None, **data):
    row = conn.execute("select concierge_task_transition(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                       (operation, tenant, user, task, revision, attempt, json.dumps(data))).fetchone()
    return row[0]


@pytest.fixture
def task_db(delegation_cluster):
    driver, dsn = delegation_cluster
    tenant, user, session = [str(uuid.uuid4()) for _ in range(3)]
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("set role service_role")
        created = rpc(conn, "create", tenant, user, batch_id=str(uuid.uuid4()), source_turn="turn-1",
                      voice_session=session, surface="voice", tasks=[dict(position=0,title="Move Bella",instruction="Move Bella to Friday")])
    return driver, dsn, tenant, user, session, created["tasks"][0]


def staged(conn, tenant, user, task):
    attempt = str(uuid.uuid4())
    assert rpc(conn,"claim",tenant,user,task["id"],task["revision"],attempt)["ok"]
    return rpc(conn,"stage",tenant,user,task["id"],task["revision"],attempt,
               kind="move_booking",summary="Move Bella to Friday",payload={"booking_id":"booking","_action":{"kind":"move_booking"}})["task"]


def claim_action(conn, tenant, user, task):
    receipt={"execution":{"version":2,"id":str(uuid.uuid4()),"operation_id":task["proposal_id"],"state":"executing","input":{}}}
    return rpc(conn,"approval_claim",tenant,user,proposal_id=task["proposal_id"],cutoff="2000-01-01T00:00:00Z",receipt=receipt)


def test_waiting_revision_invalidates_old_card_atomically(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role")
        old=staged(conn,tenant,user,task)
        changed=rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")
        assert changed["ok"] and changed["task"]["revision"]==2
        assert not claim_action(conn,tenant,user,old)["ok"]
        assert not rpc(conn,"resurface",tenant,user,proposal_id=old["proposal_id"])["ok"]
        assert conn.execute("select status from concierge_proposals where id=%s",(old["proposal_id"],)).fetchone()[0]=="superseded"
        assert conn.execute("select count(*) from concierge_task_history where task_id=%s",(task["id"],)).fetchone()[0]>=2


def test_claim_wins_correction_too_late_and_receipt_preserved(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role")
        old=staged(conn,tenant,user,task); claimed=claim_action(conn,tenant,user,old)
        assert claimed["ok"]
        assert rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")["state"]=="too_late"
        execution=claimed["proposal"]["result"]["execution"]
        saved=rpc(conn,"approval_finish",tenant,user,proposal_id=old["proposal_id"],status="executed",execution_id=execution["id"],
                  receipt={"execution":{**execution,"state":"completed"},"summary":"Bella moved to Friday."})
        assert saved["ok"] and saved["task"]["revision"]==1 and saved["task"]["commit_state"]=="committed"


def test_concurrent_revision_and_approval_have_one_winner(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role"); old=staged(conn,tenant,user,task)
    def compete(operation):
        with driver.connect(dsn,autocommit=True) as conn:
            conn.execute("set role service_role")
            return claim_action(conn,tenant,user,old) if operation=="claim" else rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(compete,["claim","revise"]))
    assert sum(bool(r["ok"]) for r in results)==1


@pytest.mark.parametrize("first", ["claim", "revise"])
def test_blocked_contenders_force_both_race_orderings(task_db, first):
    import threading
    import time
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as owner, driver.connect(dsn,autocommit=True) as observer:
        owner.execute("set role service_role"); old=staged(owner,tenant,user,task)
        owner.execute("begin")
        def perform(conn, operation):
            return claim_action(conn,tenant,user,old) if operation=="claim" else rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")
        assert perform(owner,first)["ok"]
        entered=threading.Event(); backend=[]
        def contender():
            with driver.connect(dsn,autocommit=True) as conn:
                conn.execute("set role service_role");conn.execute("set statement_timeout='3s'")
                backend.append(conn.execute("select pg_backend_pid()").fetchone()[0]);entered.set()
                return perform(conn,"revise" if first=="claim" else "claim")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(contender);assert entered.wait(2)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                wait=observer.execute("select wait_event_type from pg_stat_activity where pid=%s",(backend[0],)).fetchone()
                if wait and wait[0]=="Lock":break
                time.sleep(.01)
            else:pytest.fail("Contender did not block on the owned row lock")
            owner.execute("commit")
            assert not pending.result(timeout=3)["ok"]


def test_preparation_owner_cannot_overwrite_execution_or_duplicate_notice(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role");waiting=staged(conn,tenant,user,task)
        attempt=waiting["attempt_id"]
        assert rpc(conn,"notice_claim",tenant,user,task["id"],1,event_id=waiting["event_id"])["ok"]
        assert not rpc(conn,"notice_claim",tenant,user,task["id"],1,event_id=waiting["event_id"])["ok"]
        assert claim_action(conn,tenant,user,waiting)["ok"]
        assert not rpc(conn,"finish",tenant,user,task["id"],1,attempt,status="done",summary="forged",result={})["ok"]
        assert not rpc(conn,"stage",tenant,user,task["id"],1,attempt,kind="x",summary="forged",payload={})["ok"]


def test_old_attempt_cannot_finish_or_stage_after_restart_or_revision(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role"); attempt=str(uuid.uuid4())
        assert rpc(conn,"claim",tenant,user,task["id"],1,attempt)["ok"]
        assert rpc(conn,"recover",tenant,user,task["id"],1)["ok"]
        assert rpc(conn,"claim",tenant,user,task["id"],1,str(uuid.uuid4()))["ok"]
        assert not rpc(conn,"finish",tenant,user,task["id"],1,attempt,status="done",summary="old",result={"kind":"lookup_answer"})["ok"]
        assert not rpc(conn,"stage",tenant,user,task["id"],1,attempt,kind="x",summary="old",payload={})["ok"]


def test_authenticated_cannot_forge_authority_or_cross_user_read(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role"); old=staged(conn,tenant,user,task)
        conn.execute("reset role")
        conn.execute("select set_config('app.test.tenant',%s,false)",(tenant,))
        conn.execute("select set_config('request.jwt.claim.sub',%s,false)",(user,))
        conn.execute("set role authenticated")
        assert conn.execute("select id from concierge_tasks where id=%s",(task["id"],)).fetchone()
        for fields in ["revision=0", "status='done'", "result='{}'", "attempt_id=gen_random_uuid()"]:
            assert conn.execute(f"update concierge_tasks set {fields} where id=%s returning id",(task["id"],)).fetchall()==[]
        assert conn.execute("update concierge_proposals set payload='{}',status='executed' where id=%s returning id",(old["proposal_id"],)).fetchall()==[]
        for op in ["finish","stage","recover","approval_finish","delivery_claim"]:
            with pytest.raises(driver.errors.InsufficientPrivilege): rpc(conn,op,tenant,user,task["id"],1)
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("insert into concierge_tasks(tenant_id,user_id,batch_id,title,instruction,revision) values(%s,%s,%s,'forge','forge',1)",(tenant,user,str(uuid.uuid4())))
        conn.execute("select set_config('request.jwt.claim.sub',%s,false)",(str(uuid.uuid4()),))
        assert conn.execute("select id from concierge_tasks where id=%s",(task["id"],)).fetchall()==[]
        assert conn.execute("select id from concierge_proposals where id=%s",(old["proposal_id"],)).fetchall()==[]


def test_speech_claim_dedup_and_revision_supersession(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role"); waiting=staged(conn,tenant,user,task)
        delivery=dict(event_id=waiting["event_id"],voice_session=session,delivery_id=str(uuid.uuid4()))
        assert rpc(conn,"delivery_claim",tenant,user,task["id"],1,**delivery)["ok"]
        assert not rpc(conn,"delivery_claim",tenant,user,task["id"],1,**delivery)["ok"]
        assert rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")["ok"]
        assert not rpc(conn,"delivery_start",tenant,user,task["id"],1,**delivery)["ok"]


def test_expired_linked_card_is_classified_and_requires_revision(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role");waiting=staged(conn,tenant,user,task)
        conn.execute("update concierge_proposals set created_at=now()-interval '1 hour' where id=%s",(waiting["proposal_id"],))
        expired=rpc(conn,"resurface",tenant,user,proposal_id=waiting["proposal_id"])
        assert not expired["ok"] and expired["state"]=="expired"
        assert expired["task"]["status"]=="needs_you" and expired["proposal"]["status"]=="expired"
        assert not claim_action(conn,tenant,user,waiting)["ok"]
        revised=rpc(conn,"revise",tenant,user,task["id"],1,instruction="Move Bella to Monday instead")
        assert revised["ok"] and revised["task"]["revision"]==2


def test_source_turn_idempotency_and_tenant_user_session_isolation(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("set role service_role")
        repeated=rpc(conn,"create",tenant,user,batch_id=str(uuid.uuid4()),source_turn="turn-1",voice_session=session,
                     surface="voice",tasks=[dict(position=0,title="duplicate",instruction="must not replace"),
                                           dict(position=1,title="extra",instruction="must not append")])
        assert len(repeated["tasks"])==1
        assert repeated["tasks"][0]["id"]==task["id"] and repeated["tasks"][0]["instruction"]==task["instruction"]
        for wrong_tenant,wrong_user in [(str(uuid.uuid4()),user),(tenant,str(uuid.uuid4()))]:
            assert not rpc(conn,"cancel",wrong_tenant,wrong_user,task["id"],1)["ok"]
        waiting=staged(conn,tenant,user,task)
        assert not rpc(conn,"delivery_claim",tenant,user,task["id"],1,event_id=waiting["event_id"],
                       voice_session=str(uuid.uuid4()),delivery_id=str(uuid.uuid4()))["ok"]


def test_rollback_revokes_rpc_without_losing_evidence(task_db):
    driver,dsn,tenant,user,session,task=task_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute(ROLLBACK.read_text()); conn.execute("set role service_role")
        with pytest.raises(driver.errors.InsufficientPrivilege): rpc(conn,"claim",tenant,user,task["id"],1,str(uuid.uuid4()))
        conn.execute("reset role")
        assert conn.execute("select revision from concierge_tasks where id=%s",(task["id"],)).fetchone()[0]==1
        conn.execute(MIGRATION.read_text())
