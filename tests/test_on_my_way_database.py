"""Disposable PostgreSQL: shared claims, fencing, receipts, isolation and rollback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

import pytest

from tests.test_offer_deadlines_database import ROOT, cluster  # noqa: F401

FORWARD = ROOT / "db/migrations/20260927_on_my_way.sql"
ROLLBACK = ROOT / "db/migrations/20260927_on_my_way.rollback.sql"


@pytest.fixture(scope="module")
def omw_db(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("""
          create role anon; create role authenticated;
          alter role service_role bypassrls;
          create table tenants(id uuid primary key);
          alter table bookings add column booking_ref text;
          alter table bookings add column start_block integer;
          alter table bookings add column length_blocks integer;
          create table job_sheets(id uuid primary key, tenant_id uuid, booking_id uuid,
            closed_at timestamptz, on_my_way_at timestamptz);
          create table email_log(id uuid primary key default gen_random_uuid(),
            tenant_id uuid, kind text, body_in text);
          create table concierge_notifications(id uuid primary key default gen_random_uuid(),
            tenant_id uuid not null,kind text not null,title text not null,
            body text not null default '',url text not null default '',contact text);
          grant select,update on bookings,job_sheets to service_role;
          grant select,insert,update on concierge_notifications to service_role;
        """)
        c.execute(FORWARD.read_text(encoding="utf-8"))
    return driver, dsn


def booking(c, tenant=None, **fields):
    tenant = tenant or uuid.uuid4()
    c.execute("insert into tenants values (%s) on conflict do nothing", (tenant,))
    bid = c.execute("insert into bookings(tenant_id,status,booking_date,booking_ref) "
                    "values (%s,'confirmed','2099-10-01',%s) returning id",
                    (tenant, fields.get("ref", "BK-1001"))).fetchone()[0]
    return tenant, bid


def call(c, tenant, bid, attempt, operation="claim", key="initial", data=None, sheet=None):
    if data is None and operation == "notice":
        data = {"title":"Jo is on the way", "what":"Booking", "url":"/app/diary/"+str(bid)}
    return c.execute("select on_my_way_action(%s,%s,%s,%s,%s,%s,%s)",
                     (tenant, bid, key, attempt, operation, json.dumps(data or {"eta": "30"}), sheet)).fetchone()[0]


def test_forward_rollback_forward_keeps_receipts_and_disables_writes(omw_db):
    driver, dsn = omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t, b = booking(c)
        a = uuid.uuid4()
        call(c, t, b, a)
        call(c, t, b, a, "dispatch", data={"text": "On the way"})
        receipt = call(c, t, b, a, "sent", data={"provider_id": "accepted-1"})
        c.execute(ROLLBACK.read_text(encoding="utf-8"))
        c.execute(ROLLBACK.read_text(encoding="utf-8"))
        c.execute("set role service_role")
        assert c.execute("select receipt from on_my_way_actions where id=%s", (receipt["id"],)).fetchone()[0] == receipt["receipt"]
        with pytest.raises(driver.errors.InsufficientPrivilege):
            call(c, t, b, uuid.uuid4())
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute("update on_my_way_actions set state='pre_send_failure'")
        c.execute("reset role")
        c.execute(FORWARD.read_text(encoding="utf-8"))
        c.execute("set role service_role")
        assert call(c, t, b, uuid.uuid4())["state"] == "sent"
        assert not call(c, t, b, uuid.uuid4())["owned"]


def test_rls_unique_constraints_and_forged_tenant(omw_db):
    driver, dsn = omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t, b = booking(c)
        other, ob = booking(c)
        a = uuid.uuid4()
        row = call(c, t, b, a)
        assert call(c, other, b, a) == {"error": "notlive"}
        with pytest.raises(driver.errors.UniqueViolation):
            c.execute("insert into on_my_way_actions(tenant_id,booking_id,action_key,state,attempt) "
                      "values(%s,%s,'initial','preparing',%s)", (t,b,a))
        with pytest.raises(driver.errors.ForeignKeyViolation):
            c.execute("insert into on_my_way_actions(tenant_id,booking_id,action_key,state,attempt) "
                      "values(%s,%s,'initial','preparing',%s)", (other,b,a))
        with pytest.raises(driver.errors.CheckViolation):
            c.execute("update on_my_way_actions set state='invented' where id=%s", (row["id"],))
        for role in ("anon", "authenticated"):
            c.execute("set role " + role)
            with pytest.raises(driver.errors.InsufficientPrivilege):
                call(c, t, b, a)
            with pytest.raises(driver.errors.InsufficientPrivilege):
                c.execute("select * from on_my_way_actions")
            c.execute("reset role")
        # Even an accidental future SELECT grant cannot bypass RLS.
        c.execute("grant select on on_my_way_actions to authenticated")
        c.execute("set role authenticated")
        assert c.execute("select * from on_my_way_actions").fetchall() == []
        c.execute("reset role")
        c.execute("revoke select on on_my_way_actions from authenticated")
        assert call(c, other, ob, a)["owned"]


def test_two_workers_have_one_claim_and_one_notice(omw_db):
    driver, dsn = omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t, b = booking(c)
    barrier = threading.Barrier(2)

    def contender(operation="claim", attempt=None):
        with driver.connect(dsn) as c:
            c.execute("set role service_role")
            barrier.wait(timeout=10)
            return call(c,t,b,attempt or uuid.uuid4(),operation)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: contender(), range(2)))
    assert sum(r["owned"] for r in results) == 1
    winner = next(r for r in results if r["owned"])
    with driver.connect(dsn, autocommit=True) as c:
        call(c,t,b,winner["attempt"],"dispatch")
        call(c,t,b,winner["attempt"],"sent")
    with ThreadPoolExecutor(2) as pool:
        notices = list(pool.map(lambda _: contender("notice", winner["attempt"]), range(2)))
    assert sum(r["owned"] for r in notices) == 1
    with driver.connect(dsn) as c:
        assert c.execute("select count(*) from concierge_notifications where tenant_id=%s",(t,)).fetchone() == (1,)


def test_crashes_fence_old_worker_and_never_retry_unknown(omw_db):
    driver, dsn = omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t,b = booking(c)
        first, second = uuid.uuid4(),uuid.uuid4()
        original = call(c,t,b,first)
        assert not call(c,t,b,second)["owned"]
        # Crash before dispatch: only an expired preparation lease is reclaimable.
        c.execute("update on_my_way_actions set lease_until=now()-interval '1 second' where id=%s", (original["id"],))
        assert call(c,t,b,second)["owned"]
        assert call(c,t,b,first,"dispatch")["error"] == "stale_attempt"
        assert call(c,t,b,first,"fail")["error"] == "stale_attempt"
        assert call(c,t,b,second,"dispatch")["state"] == "unknown"
        # Crash immediately before provider call OR immediately after acceptance:
        # neither leaves evidence permitting a resend, even years later.
        c.execute("update on_my_way_actions set lease_until='2000-01-01' where id=%s", (original["id"],))
        retry = call(c,t,b,uuid.uuid4())
        assert retry["state"] == "unknown" and not retry["owned"]
        assert call(c,t,b,second,"fail")["error"] == "stale_attempt"
        assert call(c,t,b,uuid.uuid4(),key="after:"+original["id"])["error"] == "stale_action"


def test_reconciliation_and_explicit_correction_preserve_evidence(omw_db):
    driver, dsn = omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t,b = booking(c)
        a = uuid.uuid4()
        row = call(c,t,b,a)
        call(c,t,b,a,"dispatch",data={"log_id":"old-log"})
        with pytest.raises(driver.errors.RaiseException):
            c.execute("select reconcile_on_my_way(%s,%s,%s,false,'')",(t,b,row["id"]))
        c.execute("select reconcile_on_my_way(%s,%s,%s,false,%s)",
                  (t,b,row["id"],"Provider trace abc confirms request was rejected before acceptance"))
        a2=uuid.uuid4()
        retried=call(c,t,b,a2)
        assert retried["owned"] and retried["history"][-1]["receipt"]["log_id"] == "old-log"
        assert "reconciliation_evidence" in retried["history"][-1]["receipt"]
        call(c,t,b,a2,"dispatch")
        c.execute("select reconcile_on_my_way(%s,%s,%s,true,%s)",
                  (t,b,row["id"],"Sending account message xyz proves provider acceptance"))
        assert not call(c,t,b,uuid.uuid4())["owned"]
        correction = call(c,t,b,uuid.uuid4(),key="after:"+row["id"],data={"eta":"45"})
        assert correction["owned"] and correction["id"] != row["id"]
        assert not call(c,t,b,uuid.uuid4(),key=correction["action_key"])["owned"]


def test_cancelled_changed_and_revoked_sheet_refused_before_dispatch(omw_db):
    driver, dsn=omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t,b=booking(c)
        sid,a=uuid.uuid4(),uuid.uuid4()
        c.execute("insert into job_sheets(id,tenant_id,booking_id) values(%s,%s,%s)",(sid,t,b))
        assert call(c,t,b,a,sheet=sid)["owned"]
        c.execute("update job_sheets set closed_at=now() where id=%s",(sid,))
        assert call(c,t,b,a,"dispatch",sheet=sid)["error"] == "notlive"
        c.execute("update job_sheets set closed_at=null where id=%s",(sid,))
        c.execute("update bookings set updated_at=now() where id=%s",(b,))
        assert call(c,t,b,a,"dispatch",sheet=sid)["error"] == "stale_action"
        c.execute("update bookings set status='cancelled' where id=%s",(b,))
        assert call(c,t,b,a,"dispatch",sheet=sid)["error"] == "notlive"
        t2,b2=booking(c)
        assert call(c,t2,b2,uuid.uuid4(),sheet=sid)["error"] == "notlive"


def test_historical_evidence_is_held_not_certified(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn, autocommit=True) as c:
        t,b=booking(c,ref="BK-7777")
        t2,b2=booking(c,ref="BK-7777")
        t3,b3=booking(c)
        c.execute("insert into email_log(tenant_id,kind,body_in) values(%s,'on_my_way',%s)",
                  (t,"[on my way] Booking reference: BK-7777."))
        c.execute("insert into job_sheets(id,tenant_id,booking_id,on_my_way_at) values(%s,%s,%s,now())",
                  (uuid.uuid4(),t3,b3))
        c.execute(FORWARD.read_text(encoding="utf-8"))
        assert call(c,t,b,uuid.uuid4())["state"] == "unknown"
        assert call(c,t3,b3,uuid.uuid4())["state"] == "unknown"
        assert call(c,t2,b2,uuid.uuid4())["owned"]
        historical=call(c,t,b,uuid.uuid4())
        c.execute("select reconcile_on_my_way(%s,%s,%s,false,%s)",
                  (t,b,historical["id"],"Stopped workers; provider trace proves this was never accepted"))
        recovered=call(c,t,b,uuid.uuid4(),data={"eta":"45","person_name":"Jo"})
        assert recovered["owned"] and recovered["eta"]=="45" and recovered["person_name"]=="Jo"


def test_real_sender_uses_postgres_receipt_after_process_restart(omw_db, monkeypatch):
    from app import on_my_way
    from tests.test_on_my_way import _env, BOOKING
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
    record={**BOOKING,"id":str(b),"tenant_id":str(t)}
    sent,_=_env(monkeypatch,parties=[],booking=record)

    def action(tenant,bid,key,attempt,operation,data=None,sheet_id=None):
        with driver.connect(dsn) as c:
            c.execute("set role service_role")
            return call(c,tenant,bid,attempt,operation,key,data,sheet_id)
    monkeypatch.setattr(on_my_way.store,"action",action)
    assert asyncio.run(on_my_way.send(str(t),record,"30"))["ok"]
    # A new event loop/attempt, no process-local claim memory.
    repeat=asyncio.run(on_my_way.send(str(t),record,"45"))
    assert repeat["ok"] and repeat["duplicate"] and len(sent["deliveries"]) == 1


def test_notice_is_atomic_retry_updates_the_same_record(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
        a=uuid.uuid4()
        row=call(c,t,b,a)
        call(c,t,b,a,"fail")
        payload={"title":"Jo is on the way", "what":"Booking", "url":"/app/diary/"+str(b)}
        notice=call(c,t,b,a,"notice",data=payload)
        assert notice["owned"] and notice["notice_id"]
        # Crash before push/its acknowledgement. Durable in-app notice exists.
        retry=uuid.uuid4()
        call(c,t,b,retry)
        call(c,t,b,retry,"dispatch")
        call(c,t,b,retry,"sent")
        repeated=call(c,t,b,retry,"notice",data=payload)
        assert not repeated["owned"] and repeated["notice_id"] == notice["notice_id"]
        assert c.execute("select body from concierge_notifications where tenant_id=%s",(t,)).fetchall()==[("Booking · the customer's been told",)]


def test_failed_notice_insert_rolls_back_claim_and_can_be_retried(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
        a=uuid.uuid4()
        call(c,t,b,a)
        call(c,t,b,a,"dispatch")
        call(c,t,b,a,"sent")
        c.execute("revoke insert on concierge_notifications from service_role")
        c.execute("set role service_role")
        with pytest.raises(driver.errors.InsufficientPrivilege):
            call(c,t,b,a,"notice")
        c.execute("reset role")
        c.execute("grant insert on concierge_notifications to service_role")
        assert call(c,t,b,a,"notice")["owned"]
        assert c.execute("select count(*) from concierge_notifications where tenant_id=%s",(t,)).fetchone()==(1,)


def test_explicit_account_removal_is_not_blocked_but_booking_receipts_are_retained(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
        a=uuid.uuid4()
        call(c,t,b,a)
        call(c,t,b,a,"dispatch")
        call(c,t,b,a,"sent")
        with pytest.raises(driver.errors.ForeignKeyViolation):
            c.execute("delete from bookings where id=%s",(b,))
        # Account closure is an explicit removal, not a rollback or retry.
        c.execute("delete from tenants where id=%s",(t,))
        assert c.execute("select count(*) from on_my_way_actions where tenant_id=%s",(t,)).fetchone()==(0,)


def test_owner_resend_is_atomic_fenced_and_retains_decision(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
        old=uuid.uuid4()
        initial=call(c,t,b,old)
        call(c,t,b,old,"dispatch",data={"body":"Original message"})
    form={"eta":"45", "resend_attempt":str(old), "initiator":{"actor_name":"Jo","actor_user_id":"u1"}}
    barrier=threading.Barrier(2)
    def resend():
        with driver.connect(dsn) as c:
            barrier.wait(timeout=10)
            return call(c,t,b,uuid.uuid4(),"owner_resend",data=form)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:resend(),range(2)))
    assert sum(r["owned"] for r in results)==1
    winner=next(r for r in results if r["owned"])
    assert winner["id"]==initial["id"] and winner["eta"]=="45"
    history=winner["history"][-1]
    assert history["receipt"]["body"]=="Original message"
    evidence=json.loads(history["receipt"]["reconciliation_evidence"])
    assert evidence["reason"]=="owner chose to resend" and evidence["initiator"]==form["initiator"]
    with driver.connect(dsn,autocommit=True) as c:
        assert call(c,t,b,old,"sent")["error"]=="stale_attempt"
        call(c,t,b,winner["attempt"],"dispatch")
        stale=call(c,t,b,uuid.uuid4(),"owner_resend",data=form)
        assert not stale["owned"] and stale["state"]=="unknown"
        assert stale["attempt"]==winner["attempt"]


def test_helper_cannot_authorise_resend_and_other_tenant_cannot_release_it(omw_db):
    driver,dsn=omw_db
    with driver.connect(dsn,autocommit=True) as c:
        t,b=booking(c)
        sid,old=uuid.uuid4(),uuid.uuid4()
        c.execute("insert into job_sheets(id,tenant_id,booking_id) values(%s,%s,%s)",(sid,t,b))
        call(c,t,b,old,sheet=sid)
        call(c,t,b,old,"dispatch",sheet=sid)
        form={"eta":"45","resend_attempt":str(old)}
        assert call(c,t,b,uuid.uuid4(),"owner_resend",data=form,sheet=sid)["error"]=="notlive"
        assert call(c,uuid.uuid4(),b,uuid.uuid4(),"owner_resend",data=form)["error"]=="notlive"
        assert call(c,t,b,uuid.uuid4())["state"]=="unknown"
