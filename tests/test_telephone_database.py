"""Durable telephone invariants on a disposable local PostgreSQL cluster."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import uuid

import pytest
from tests.test_booking_capacity_database import cluster

ROOT = Path(__file__).resolve().parents[1]
FORWARD = ROOT / "db/migrations/20261015_telephone_pilot.sql"
ROLLBACK = ROOT / "db/migrations/20261015_telephone_pilot.rollback.sql"
PHONE = "+447700900001"


@pytest.fixture(scope="module")
def telephone_db(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""
          CREATE TABLE tenants(id uuid primary key);
          CREATE TABLE tenant_members(tenant_id uuid,user_id uuid,role text,active boolean);
          CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
          GRANT SELECT ON tenants,tenant_members TO service_role;
        """)
        conn.execute(FORWARD.read_text())
    return driver, dsn


def command(database, tenant, user, action, payload=None):
    driver, dsn = database
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role")
        return conn.execute("SELECT telephone_command(%s,%s,%s,%s::jsonb)",
                            (tenant, user, action, json.dumps(payload or {}))).fetchone()[0]


@pytest.fixture
def setup(telephone_db):
    tenant, user = uuid.uuid4(), uuid.uuid4()
    driver, dsn = telephone_db
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO tenants VALUES (%s)", (tenant,))
        conn.execute("INSERT INTO tenant_members VALUES (%s,%s,'owner',true)", (tenant,user))
    command(telephone_db,tenant,user,"configure",dict(enabled=True,daily_budget_minor=200,
        max_concurrent=1,max_call_cost_minor=100,max_duration_seconds=60))
    command(telephone_db,tenant,user,"eligibility",dict(phone=PHONE,eligible=True,
        callback_requested=True,basis="owner_attested_callback",
        expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    return telephone_db,str(tenant),str(user)


def create(setup, key=None):
    database,tenant,user=setup
    return command(database,tenant,user,"create",dict(idempotency_key=key or str(uuid.uuid4()),
        to_number=PHONE,from_number="+447700900002",objective="Record callback result",
        script="Ask whether the requested callback is still needed.",max_duration_seconds=60,
        scenario="completed"))


def approve(setup, row):
    return command(*setup,"approve",dict(call_id=row["id"]))


def claim(setup, row):
    return command(*setup,"claim",dict(call_id=row["id"],attempt_id=str(uuid.uuid4())))


def submitted(setup,row):
    claim(setup,row)
    current=command(*setup,"read",dict(call_id=row["id"]))
    return command(setup[0],setup[1],None,"submitted",dict(call_id=row["id"],
        attempt_id=current["attempt_id"],provider_call_id="sim-"+row["id"]))


def event(setup,row,event_id="evt-terminal",sequence=3,status="completed"):
    return command(setup[0],setup[1],None,"event",dict(call_id=row["id"],
        provider_call_id="sim-"+row["id"],event_id=event_id,sequence=sequence,status=status,
        outcome="message_taken" if status=="completed" else "none",
        duration_seconds=20,cost_minor=50 if status=="completed" else 0))


def test_atomic_capacity_and_budget_reservations(setup):
    rows=[approve(setup,create(setup)) for _ in range(2)]
    def run(row):
        try:
            return claim(setup,row)["status"]
        except Exception as exc:
            return str(exc).splitlines()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(run,rows))
    assert sorted(outcomes)==["capacity_exhausted","submitting"]
    command(*setup,"configure",dict(max_concurrent=2,daily_budget_minor=100))
    remaining=rows[outcomes.index("capacity_exhausted")]
    with pytest.raises(Exception,match="budget_exhausted"):
        claim(setup,remaining)
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT sum(reserved_minor) FROM telephone_calls WHERE tenant_id=%s",
                            (setup[1],)).fetchone()[0]==100


def test_create_idempotency_conflict_and_cross_tenant_hidden(setup):
    key=str(uuid.uuid4())
    row=create(setup,key)
    assert create(setup,key)["id"]==row["id"]
    database,tenant,user=setup
    payload=dict(row["snapshot"],objective="Changed objective")
    with pytest.raises(Exception,match="idempotency_conflict"):
        command(database,tenant,user,"create",payload)
    other=uuid.uuid4()
    driver,dsn=database
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("INSERT INTO tenants VALUES (%s)",(other,))
        conn.execute("INSERT INTO tenant_members VALUES (%s,%s,'owner',true)",(other,user))
    with pytest.raises(Exception,match="not_found"):
        command(database,other,user,"read",dict(call_id=row["id"]))


def test_events_monotonic_duplicate_and_one_business_record(setup):
    row=submitted(setup,approve(setup,create(setup)))
    final=event(setup,row)
    assert final["status"]=="completed"
    assert event(setup,row)["status"]=="completed"
    assert event(setup,row,"evt-late",1,"ringing")["status"]=="completed"
    assert event(setup,row,"evt-higher",9,"answered")["status"]=="completed"
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        records=conn.execute("SELECT outcome,objective_achieved,simulated FROM telephone_business_records WHERE call_id=%s",
                             (row["id"],)).fetchall()
    assert len(records)==1 and records[0][0]=="message_taken" and records[0][2] is True


def test_suppression_and_fresh_role_rechecked_at_claim(setup):
    row=approve(setup,create(setup))
    command(*setup,"suppress",dict(phone=PHONE,suppressed=True))
    with pytest.raises(Exception,match="suppressed"):
        claim(setup,row)
    command(*setup,"suppress",dict(phone=PHONE,suppressed=False))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE tenant_members SET role='staff' WHERE tenant_id=%s",(setup[1],))
    with pytest.raises(Exception,match="owner_required"):
        claim(setup,row)


def test_browser_roles_cannot_read_tables_or_execute_rpc(setup):
    driver,dsn=setup[0]
    for role in ("anon","authenticated"):
        for query in ("SELECT * FROM telephone_calls",
                      "SELECT telephone_command(null,null,'list','{}')"):
            with driver.connect(dsn,autocommit=True) as conn:
                conn.execute("SET ROLE "+role)
                with pytest.raises(driver.errors.InsufficientPrivilege):
                    conn.execute(query)
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'telephone_%' AND relrowsecurity").fetchone()[0]==5


def test_unknown_dispatch_and_cancel_preserve_reservation(setup):
    row=claim(setup,approve(setup,create(setup)))
    unknown=command(setup[0],setup[1],None,"unknown",dict(call_id=row["id"],attempt_id=row["attempt_id"]))
    assert unknown["status"]=="unknown" and unknown["reserved_minor"]==100
    cancelled=command(*setup,"cancel",dict(call_id=row["id"]))
    assert cancelled["cancel_requested"] is True and cancelled["reserved_minor"]==100
    with pytest.raises(Exception,match="already_dispatched"):
        claim(setup,row)


def test_rollback_forward_preserves_calls_and_receipts(setup):
    row=submitted(setup,approve(setup,create(setup)))
    event(setup,row,event_id=str(uuid.uuid4()))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        before=conn.execute("SELECT to_jsonb(c) FROM telephone_calls c ORDER BY id").fetchall()
        receipts=conn.execute("SELECT to_jsonb(r) FROM telephone_business_records r ORDER BY call_id").fetchall()
        conn.execute(ROLLBACK.read_text())
        assert conn.execute("SELECT bool_or(enabled) FROM telephone_settings").fetchone()[0] is False
        conn.execute(FORWARD.read_text())
        assert conn.execute("SELECT to_jsonb(c) FROM telephone_calls c ORDER BY id").fetchall()==before
        assert conn.execute("SELECT to_jsonb(r) FROM telephone_business_records r ORDER BY call_id").fetchall()==receipts


def service(setup, provider=None):
    from app.telephone import TelephoneService
    from app.telephone_store import StoreError
    class PgStore:
        def command(self,tenant,user,action,payload=None):
            try:
                return command(setup[0],tenant,user,action,payload)
            except Exception as exc:
                raise StoreError(str(exc).splitlines()[0]) from None
    return TelephoneService(PgStore(),provider)


def test_simulated_service_durable_outcome_never_claims_business_success(setup):
    from app.telephone_store import StoreError
    row=approve(setup,create(setup))
    workflow=service(setup)
    result=workflow.dispatch(setup[1],setup[2],row["id"])
    assert result["status"]=="completed" and result["reserved_minor"]==0
    assert result["outcome"]=="none"
    with pytest.raises(StoreError,match="already_dispatched"):
        workflow.dispatch(setup[1],setup[2],row["id"])
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*),bool_or(objective_achieved) FROM telephone_business_records WHERE call_id=%s",
                            (row["id"],)).fetchone()==(1,False)


def test_ambiguous_service_submission_is_not_retried(setup):
    from app.telephone_provider import AmbiguousSubmission
    from app.telephone_store import StoreError
    class Ambiguous:
        attempts=0
        def submit(self,spec):
            self.attempts+=1
            raise AmbiguousSubmission("untrusted provider error destination/secret")
    adapter=Ambiguous()
    workflow=service(setup,adapter)
    row=approve(setup,create(setup))
    result=workflow.dispatch(setup[1],setup[2],row["id"])
    assert result["status"]=="unknown" and result["reserved_minor"]==100
    assert result["error_code"]=="submission_unknown"
    with pytest.raises(StoreError,match="already_dispatched"):
        workflow.dispatch(setup[1],setup[2],row["id"])
    assert adapter.attempts==1


def test_approval_cost_cap_cannot_be_inflated_by_later_configuration(setup):
    row=approve(setup,create(setup))
    command(*setup,"configure",dict(max_call_cost_minor=200,daily_budget_minor=400))
    assert claim(setup,row)["reserved_minor"]==100


def test_unrecognised_owner_and_expired_eligibility_fail_closed(setup):
    with pytest.raises(Exception,match="owner_required"):
        command(setup[0],setup[1],uuid.uuid4(),"list")
    row=approve(setup,create(setup))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE telephone_eligibility SET expires_at=now()-interval '1 second' WHERE tenant_id=%s",(setup[1],))
    with pytest.raises(Exception,match="ineligible"):
        claim(setup,row)


def test_cancel_during_submission_delivers_stop_but_waits_for_terminal_evidence(setup):
    import threading
    from app.telephone_provider import Submission,ProviderEvent
    entered,release=threading.Event(),threading.Event()
    class BlockingProvider:
        cancelled=[]
        def submit(self,spec):
            entered.set()
            assert release.wait(10)
            return Submission("sim_cancel_race")
        def cancel(self,provider_call_id):
            self.cancelled.append(provider_call_id)
            return True
        def events(self,*args):
            pytest.fail("cancelled submission must not synthesize completion")
    adapter=BlockingProvider()
    workflow=service(setup,adapter)
    row=approve(setup,create(setup))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(workflow.dispatch,setup[1],setup[2],row["id"])
        assert entered.wait(10)
        try:
            cancelled=workflow.cancel(setup[1],setup[2],row["id"])
            assert cancelled["cancel_requested"] and cancelled["reserved_minor"]==100
        finally:
            release.set()
        result=pending.result(timeout=10)
    assert adapter.cancelled==["sim_cancel_race"]
    assert result["status"]=="initiated" and result["reserved_minor"]==100
    terminal=workflow.event(ProviderEvent(row["id"],"sim_cancel_race",setup[1],
                                          "cancel-terminal",1,"cancelled"))
    assert terminal["status"]=="cancelled" and terminal["reserved_minor"]==0


def test_business_record_failure_rolls_back_event_and_allows_safe_retry(setup):
    row=submitted(setup,approve(setup,create(setup)))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("""CREATE FUNCTION telephone_test_fail_record() RETURNS trigger LANGUAGE plpgsql
          AS $$ BEGIN RAISE EXCEPTION 'injected_record_failure'; END $$;
          CREATE TRIGGER telephone_test_failure BEFORE INSERT ON telephone_business_records
          FOR EACH ROW EXECUTE FUNCTION telephone_test_fail_record();""")
    try:
        with pytest.raises(Exception,match="injected_record_failure"):
            event(setup,row,event_id="retry-after-record-failure")
        state=command(*setup,"read",dict(call_id=row["id"]))
        assert state["status"]=="initiated" and state["reserved_minor"]==100
        with driver.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM telephone_provider_events WHERE call_id=%s",(row["id"],)).fetchone()[0]==0
    finally:
        with driver.connect(dsn,autocommit=True) as conn:
            conn.execute("DROP TRIGGER telephone_test_failure ON telephone_business_records; DROP FUNCTION telephone_test_fail_record()")
    assert event(setup,row,event_id="retry-after-record-failure")["status"]=="completed"


def test_event_provider_and_cross_tenant_mismatch_cannot_mutate(setup):
    row=submitted(setup,approve(setup,create(setup)))
    payload=dict(call_id=row["id"],provider_call_id="wrong-provider",event_id="forged",
                 sequence=3,status="completed",outcome="message_taken",cost_minor=50)
    with pytest.raises(Exception,match="provider_mismatch"):
        command(setup[0],setup[1],None,"event",payload)
    other=uuid.uuid4()
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("INSERT INTO tenants VALUES (%s)",(other,))
    payload["provider_call_id"]="sim-"+row["id"]
    with pytest.raises(Exception,match="not_found"):
        command(setup[0],other,None,"event",payload)
    assert command(*setup,"read",dict(call_id=row["id"]))==row
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_provider_events WHERE call_id=%s",(row["id"],)).fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM telephone_business_records WHERE call_id=%s",(row["id"],)).fetchone()[0]==0


def test_concurrent_duplicate_terminal_and_changed_event_cannot_rewrite(setup):
    row=submitted(setup,approve(setup,create(setup)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: event(setup,row,"concurrent-terminal"),range(2)))
    assert results[0]==results[1]
    with pytest.raises(Exception,match="event_conflict"):
        event(setup,row,"concurrent-terminal",4,"failed")
    assert command(*setup,"read",dict(call_id=row["id"]))==results[0]
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_provider_events WHERE call_id=%s",(row["id"],)).fetchone()[0]==1
        assert conn.execute("SELECT transport_status,outcome FROM telephone_business_records WHERE call_id=%s",(row["id"],)).fetchall()==[("completed","message_taken")]


def test_expired_approval_cannot_claim_or_reserve(setup):
    row=approve(setup,create(setup))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE telephone_calls SET approved_until=now()-interval '1 second' WHERE id=%s",(row["id"],))
    with pytest.raises(Exception,match="approval_expired"):
        claim(setup,row)
    state=command(*setup,"read",dict(call_id=row["id"]))
    assert state["reserved_minor"]==0 and state["attempt_id"] is None and state["status"]=="approved"


def test_cost_overrun_retains_actual_cost_and_disables_new_dispatch(setup):
    row=submitted(setup,approve(setup,create(setup)))
    waiting=approve(setup,create(setup))
    final=command(setup[0],setup[1],None,"event",dict(call_id=row["id"],
        provider_call_id="sim-"+row["id"],event_id="overrun",sequence=3,status="completed",
        outcome="none",cost_minor=175,duration_seconds=20))
    assert final["cost_minor"]==175 and final["error_code"]=="cost_overrun" and final["reserved_minor"]==0
    assert command(*setup,"config")["enabled"] is False
    with pytest.raises(Exception,match="disabled"):
        claim(setup,waiting)
    assert command(*setup,"read",dict(call_id=waiting["id"]))["attempt_id"] is None
