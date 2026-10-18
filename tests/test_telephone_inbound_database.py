"""Inbound carrier state and actual CRM delivery on disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import uuid

import pytest
from tests.test_booking_capacity_database import cluster
from tests.test_telephone_database import telephone_db, command as pilot_command

ROOT=Path(__file__).resolve().parents[1]
ACCOUNT="AC"+"1"*32
AGENT_NUMBER="+447700900010"
ORIGINAL_NUMBER="+447700900011"
TRANSFER_NUMBER="+447700900012"
CALLER_NUMBER="+447700900013"


@pytest.fixture(scope="module")
def inbound_db(telephone_db):
    driver,dsn=telephone_db
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("""CREATE TABLE crm_activities (
          id uuid primary key default gen_random_uuid(),tenant_id uuid not null references tenants(id),
          contact_id uuid,company_id uuid,activity_type text not null,source_id text,direction text,
          subject text,summary text,occurred_at timestamptz not null,created_at timestamptz not null default now());
          CREATE UNIQUE INDEX crm_activities_source_uniq
          ON crm_activities(tenant_id,activity_type,source_id,coalesce(direction,'')) WHERE source_id IS NOT NULL;
          GRANT SELECT,INSERT,UPDATE ON crm_activities TO service_role;""")
        conn.execute((ROOT/"db/migrations/20261015_telephone_inbound.sql").read_text())
        conn.execute((ROOT/"db/migrations/20261016_telephone_objectives.sql").read_text())
    return driver,dsn


def command(database,tenant,user,action,payload=None):
    driver,dsn=database
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("SET ROLE service_role")
        return conn.execute("SELECT telephone_inbound_command(%s,%s,%s,%s::jsonb)",
                            (tenant,user,action,json.dumps(payload or {}))).fetchone()[0]


@pytest.fixture
def setup(inbound_db):
    tenant,user=str(uuid.uuid4()),str(uuid.uuid4())
    driver,dsn=inbound_db
    phone="+4477"+str(uuid.uuid4().int)[0:8]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("INSERT INTO tenants VALUES(%s)",(tenant,))
        conn.execute("INSERT INTO tenant_members VALUES(%s,%s,'owner',true)",(tenant,user))
    pilot_command(inbound_db,tenant,user,"configure",dict(enabled=True,daily_budget_minor=200,
        max_concurrent=1,max_call_cost_minor=100,max_duration_seconds=60))
    command(inbound_db,tenant,user,"configure_route",dict(account_id=ACCOUNT,phone_number=phone,
        enabled=True,original_number=ORIGINAL_NUMBER,transfer_number=TRANSFER_NUMBER,
        transfer_verified=True,forwarding_state="active",transfer_timeout_seconds=20,
        greeting="You have reached the business assistant.",instructions="Take a message or offer human handover.",
        provider_verified=True))
    return inbound_db,tenant,user,phone


def admit(setup,call_id=None,**overrides):
    payload=dict(account_id=ACCOUNT,provider_call_id=call_id or "CA"+uuid.uuid4().hex,
                 to_number=setup[3],from_number=CALLER_NUMBER)
    payload.update(overrides)
    return command(setup[0],None,None,"admit",payload)


def event(setup,row,**overrides):
    payload=dict(call_id=row["id"],account_id=ACCOUNT,provider_call_id=row["provider_call_id"],
        event_id=str(uuid.uuid4()),sequence=3,status="completed",outcome="none",
        duration_seconds=20,cost_minor=50,usage_complete=True)
    payload.update(overrides)
    return command(setup[0],setup[1],None,"event",payload)


def read(setup,row):
    return command(setup[0],setup[1],setup[2],"read",dict(call_id=row["id"]))


def test_inbound_identity_comes_from_account_and_called_number(setup):
    row=admit(setup)
    assert row["tenant_id"]==setup[1] and row["direction"]=="inbound" and row["simulated"] is False
    with pytest.raises(Exception,match="not_found"):
        admit(setup,account_id="AC"+"2"*32)
    with pytest.raises(Exception,match="not_found"):
        admit(setup,to_number=CALLER_NUMBER)
    with pytest.raises(Exception,match="provider_mismatch"):
        command(setup[0],str(uuid.uuid4()),None,"admit",dict(account_id=ACCOUNT,
            provider_call_id="CA"+uuid.uuid4().hex,to_number=setup[3],from_number=CALLER_NUMBER))


def test_concurrent_admission_reserves_capacity_once(setup):
    def attempt(_):
        try:
            return admit(setup)["id"]
        except Exception as exc:
            return str(exc).splitlines()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(attempt,range(2)))
    assert outcomes.count("capacity_exhausted")==1
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*),sum(reserved_minor) FROM telephone_calls WHERE tenant_id=%s",(setup[1],)).fetchone()==(1,100)


def test_duplicate_admission_and_terminal_events_create_one_outcome(setup):
    provider_id="CA"+uuid.uuid4().hex
    row=admit(setup,provider_id)
    assert admit(setup,provider_id)["id"]==row["id"]
    final=event(setup,row,event_id="terminal-one")
    assert event(setup,row,event_id="terminal-one")["status"]==final["status"]
    event(setup,row,event_id="late-ringing",sequence=1,status="ringing")
    assert read(setup,row)["status"]=="completed"
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_inbound_outcomes WHERE call_id=%s",(row["id"],)).fetchone()[0]==1


def test_message_correction_requires_revision_and_updates_single_crm_record(setup):
    row=admit(setup)
    first=command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,text="Call on Monday.",confirmed=True))
    assert first["message_revision"]==1
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    with pytest.raises(Exception,match="revision_conflict"):
        command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,text="Stale overwrite.",confirmed=True))
    second=command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=1,text="Correction: call on Tuesday.",confirmed=True))
    assert second["message_revision"]==2
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT summary FROM crm_activities WHERE tenant_id=%s",(setup[1],)).fetchall()==[("Correction: call on Tuesday.",)]


def test_offboarding_requires_separate_carrier_restoration_attestation(setup):
    result=command(setup[0],setup[1],setup[2],"offboard",dict(restored=False))
    assert result["enabled"] is False and result["carrier_changed"] is False
    assert result["forwarding_state"]=="restoration_required"
    with pytest.raises(Exception,match="disabled"):
        admit(setup)
    restored=command(setup[0],setup[1],setup[2],"offboard",dict(restored=True))
    assert restored["forwarding_state"]=="restored" and restored["restoration_owner_attested"] is True
    assert restored["carrier_changed"] is False


def test_sip_correlation_is_one_use_and_stored_only_as_hash(setup):
    import hashlib,secrets
    row=admit(setup)
    token=secrets.token_hex(32)
    command(setup[0],setup[1],None,"bind_sip",dict(call_id=row["id"],token=token))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        expiry=conn.execute("SELECT expires_at FROM telephone_sip_sessions WHERE call_id=%s",(row["id"],)).fetchone()[0]
    command(setup[0],setup[1],None,"bind_sip",dict(call_id=row["id"],token=token))
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT expires_at FROM telephone_sip_sessions WHERE call_id=%s",(row["id"],)).fetchone()[0]==expiry
    claimed=command(setup[0],None,None,"claim_sip",dict(token=token,model_call_id="rtc_one"))
    assert claimed["id"]==row["id"] and claimed["tenant_id"]==setup[1]
    with pytest.raises(Exception,match="correlation_consumed"):
        command(setup[0],None,None,"claim_sip",dict(token=token,model_call_id="rtc_two"))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        stored=conn.execute("SELECT token_hash,model_call_id FROM telephone_sip_sessions WHERE call_id=%s",(row["id"],)).fetchone()
    assert stored==(hashlib.sha256(token.encode()).hexdigest(),"rtc_one")
    assert token not in json.dumps(read(setup,row))


def test_unconfirmed_message_never_delivers_crm_activity(setup):
    row=admit(setup)
    command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,
                                               text="Unconfirmed caller detail.",confirmed=False))
    with pytest.raises(Exception,match="invalid_state"):
        command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM crm_activities WHERE tenant_id=%s",(setup[1],)).fetchone()[0]==0


def test_unknown_usage_stays_reserved_until_actual_usage_receipt(setup):
    row=admit(setup)
    terminal=event(setup,row,event_id="terminal-without-cost",cost_minor=None,usage_complete=False)
    assert terminal["reserved_minor"]==100 and terminal["status"]=="completed"
    reconciled=event(setup,row,event_id="actual-usage",sequence=4,cost_minor=40,usage_complete=True)
    assert reconciled["reserved_minor"]==0 and reconciled["cost_minor"]==40
    assert reconciled["status"]=="completed"


def test_browser_roles_cannot_access_inbound_rpc_or_message_records(setup):
    driver,dsn=setup[0]
    for role in ("anon","authenticated"):
        for query in ("SELECT * FROM telephone_messages","SELECT telephone_inbound_command(null,null,'list','{}')"):
            with driver.connect(dsn,autocommit=True) as conn:
                conn.execute("SET ROLE "+role)
                with pytest.raises(driver.errors.InsufficientPrivilege):
                    conn.execute(query)


def test_crm_failure_keeps_confirmed_message_retryable_without_duplicate_activity(setup):
    row=admit(setup)
    command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,
                                               text="Please return the requested callback.",confirmed=True))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("""CREATE FUNCTION telephone_test_crm_failure() RETURNS trigger LANGUAGE plpgsql
          AS $$ BEGIN RAISE EXCEPTION 'crm_unavailable'; END $$;
          CREATE TRIGGER telephone_test_crm_fail BEFORE INSERT OR UPDATE ON crm_activities
          FOR EACH ROW EXECUTE FUNCTION telephone_test_crm_failure();""")
    try:
        with pytest.raises(Exception,match="crm_unavailable"):
            command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
        assert read(setup,row)["message_confirmed"] is True
        with driver.connect(dsn) as conn:
            assert conn.execute("SELECT status FROM telephone_message_outbox WHERE call_id=%s",(row["id"],)).fetchall()==[("pending",)]
    finally:
        with driver.connect(dsn,autocommit=True) as conn:
            conn.execute("DROP TRIGGER telephone_test_crm_fail ON crm_activities; DROP FUNCTION telephone_test_crm_failure()")
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM crm_activities WHERE tenant_id=%s",(setup[1],)).fetchone()[0]==1


def test_transfer_evidence_bound_to_target_parent_and_one_attempt(setup):
    row=admit(setup)
    requested=command(setup[0],setup[1],None,"transfer_request",dict(call_id=row["id"]))
    transfer=requested["transfer"]
    acknowledgement=command(setup[0],setup[1],None,"transfer_update",dict(call_id=row["id"],transfer_id=transfer["id"],status="accepted",sequence=0))
    assert acknowledgement["transfer"]["status"]=="accepted"
    payload=dict(call_id=row["id"],transfer_id=transfer["id"],status="answered",sequence=0,
        provider_leg_id="CA"+uuid.uuid4().hex,parent_call_id="wrong-parent",target_number=TRANSFER_NUMBER)
    with pytest.raises(Exception,match="provider_mismatch"):
        command(setup[0],setup[1],None,"transfer_update",payload)
    assert read(setup,row)["transfer"]["status"]=="accepted"
    payload["parent_call_id"]=row["provider_call_id"]
    network=command(setup[0],setup[1],None,"transfer_update",payload)
    assert network["transfer"]["status"]=="accepted"
    assert network["transfer"]["network_answered"] is True
    assert network["transfer"]["human_confirmed"] is False
    answered=command(setup[0],setup[1],None,"human_accept",payload)
    assert answered["transfer"]["status"]=="answered" and answered["transfer"]["human_confirmed"] is True
    assert command(setup[0],setup[1],None,"transfer_update",dict(call_id=row["id"],transfer_id=transfer["id"],status="accepted",sequence=0))["transfer"]["status"]=="answered"
    with pytest.raises(Exception,match="handover_already_requested"):
        command(setup[0],setup[1],None,"transfer_request",dict(call_id=row["id"]))


def test_transfer_timeout_and_forwarding_loop_are_not_success(setup):
    row=admit(setup)
    requested=command(setup[0],setup[1],None,"transfer_request",dict(call_id=row["id"]))
    driver,dsn=setup[0]
    with driver.connect(dsn,autocommit=True) as conn:
        conn.execute("UPDATE telephone_transfers SET expires_at=now()-interval '1 second' WHERE call_id=%s",(row["id"],))
    assert read(setup,row)["transfer"]["status"]=="timed_out"
    payload=dict(account_id=ACCOUNT,phone_number=setup[3],enabled=True,original_number=ORIGINAL_NUMBER,
        transfer_number=ORIGINAL_NUMBER,transfer_verified=True,forwarding_state="active",
        transfer_timeout_seconds=20,greeting="Hello.",instructions="Take a message.",provider_verified=True)
    with pytest.raises(Exception,match="handover_unavailable"):
        command(setup[0],setup[1],setup[2],"configure_route",payload)


def test_message_erasure_prevents_replay_and_opt_out_is_bound_to_caller(setup):
    row=admit(setup)
    command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,text="Private message.",confirmed=True))
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    erased=command(setup[0],setup[1],setup[2],"erase_message",dict(call_id=row["id"]))
    assert erased["message_text"] is None and erased["message_erased"] is True
    with pytest.raises(Exception,match="message_erased"):
        command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,text="Replay.",confirmed=True))
    command(setup[0],setup[1],None,"opt_out",dict(call_id=row["id"],phone="+447700900099"))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT summary FROM crm_activities WHERE tenant_id=%s",(setup[1],)).fetchall()==[(None,)]
        assert conn.execute("SELECT phone,suppressed FROM telephone_eligibility WHERE tenant_id=%s",(setup[1],)).fetchall()==[(CALLER_NUMBER,True)]


def test_inbound_budget_exhaustion_rejects_before_creating_call(setup):
    pilot_command(setup[0],setup[1],setup[2],"configure",dict(daily_budget_minor=50))
    with pytest.raises(Exception,match="budget_exhausted"):
        admit(setup)
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_calls WHERE tenant_id=%s",(setup[1],)).fetchone()[0]==0


def test_inbound_rollback_forward_preserves_message_crm_and_outcome(setup):
    row=admit(setup)
    command(setup[0],setup[1],None,"message",dict(call_id=row["id"],expected_revision=0,text="Retained audit message.",confirmed=True))
    command(setup[0],setup[1],None,"outbox_deliver",dict(call_id=row["id"]))
    event(setup,row)
    driver,dsn=setup[0]
    tables=("telephone_calls","telephone_messages","telephone_message_outbox","telephone_inbound_outcomes","crm_activities")
    with driver.connect(dsn,autocommit=True) as conn:
        before={table:conn.execute("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM "+table+" t").fetchone()[0] for table in tables}
        conn.execute((ROOT/"db/migrations/20261015_telephone_inbound.rollback.sql").read_text())
        assert conn.execute("SELECT bool_or(enabled) FROM telephone_routes").fetchone()[0] is False
        conn.execute((ROOT/"db/migrations/20261015_telephone_inbound.sql").read_text())
        after={table:conn.execute("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM "+table+" t").fetchone()[0] for table in tables}
        assert after==before


def test_real_outbound_approval_and_early_callback_bind_without_retry(setup):
    from datetime import datetime,timedelta,timezone
    pilot_command(setup[0],setup[1],setup[2],"eligibility",dict(phone=CALLER_NUMBER,
        eligible=True,callback_requested=True,basis="owner_attested_callback",
        expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    row=command(setup[0],setup[1],setup[2],"create_outbound",dict(idempotency_key=str(uuid.uuid4()),
        to_number=CALLER_NUMBER,from_number=setup[3],objective="Return the requested callback.",
        script="Ask if a message is needed.",max_duration_seconds=60))
    with pytest.raises(Exception,match="owner_required"):
        command(setup[0],setup[1],setup[2],"claim_outbound",dict(call_id=row["id"],attempt_id="attempt-one"))
    command(setup[0],setup[1],setup[2],"approve_outbound",dict(call_id=row["id"]))
    claimed=command(setup[0],setup[1],setup[2],"claim_outbound",dict(call_id=row["id"],attempt_id="attempt-one"))
    resolved=command(setup[0],None,None,"resolve_attempt",dict(account_id=ACCOUNT,
        call_id=row["id"],attempt_id="attempt-one",to_number=CALLER_NUMBER,from_number=setup[3]))
    assert resolved["id"]==claimed["id"]
    sid="CA"+uuid.uuid4().hex
    accepted=command(setup[0],setup[1],None,"submitted_outbound",dict(call_id=row["id"],attempt_id="attempt-one",provider_call_id=sid))
    unknown=command(setup[0],setup[1],None,"unknown_outbound",dict(call_id=row["id"],attempt_id="attempt-one"))
    assert accepted["provider_call_id"]==unknown["provider_call_id"]==sid
    assert unknown["status"]=="initiated"
    with pytest.raises(Exception,match="already_dispatched"):
        command(setup[0],setup[1],setup[2],"claim_outbound",dict(call_id=row["id"],attempt_id="attempt-two"))


def test_model_usage_is_numeric_immutable_and_bound_to_claimed_sip_session(setup):
    import secrets
    row=admit(setup)
    payload=dict(call_id=row["id"],response_id="resp_one",usage={"total_tokens":12,"input_tokens":8,"output_tokens":4})
    with pytest.raises(Exception,match="invalid_state"):
        command(setup[0],setup[1],None,"model_usage",payload)
    token=secrets.token_hex(32)
    command(setup[0],setup[1],None,"bind_sip",dict(call_id=row["id"],token=token))
    command(setup[0],None,None,"claim_sip",dict(token=token,model_call_id="rtc_usage"))
    command(setup[0],setup[1],None,"model_usage",payload)
    command(setup[0],setup[1],None,"model_usage",payload)
    with pytest.raises(Exception,match="event_conflict"):
        command(setup[0],setup[1],None,"model_usage",dict(payload,usage={"total_tokens":99}))
    with pytest.raises(Exception,match="invalid_payload"):
        command(setup[0],setup[1],None,"model_usage",dict(payload,response_id="resp_bad",usage={"transcript":"private caller text"}))
    driver,dsn=setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_model_usage WHERE call_id=%s",(row["id"],)).fetchone()[0]==1
