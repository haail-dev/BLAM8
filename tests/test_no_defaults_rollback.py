"""Forward → rollback → forward, RLS, revision races and null capacity on real Postgres."""
import json
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from tests.test_booking_capacity_database import ROOT, cluster as base_cluster, insert  # noqa: F401

FORWARD = ROOT / "db/migrations/20260927_no_defaults.sql"
ROLLBACK = ROOT / "db/migrations/20260927_no_defaults.rollback.sql"


@pytest.fixture
def cluster():
    yield from base_cluster.__wrapped__()


def setup_tenants(conn):
    conn.execute("create table tenants (id uuid primary key)")
    conn.execute("create table subscriptions (tenant_id uuid, module text, status text)")
    conn.execute("alter table scheduling_config add column working_days text default 'mon,tue,wed,thu,fri'")


def test_forward_rollback_forward_and_capacity(cluster):
    driver, dsn = cluster
    old, missing, fresh = (uuid.uuid4() for _ in range(3))
    with driver.connect(dsn, autocommit=True) as conn:
        setup_tenants(conn)
        conn.execute("insert into tenants values (%s),(%s)", (old, missing))
        conn.execute("insert into subscriptions values (%s,'scheduling','active'),(%s,'fsm','active')", (old, missing))
        nonbooking, inactive = uuid.uuid4(), uuid.uuid4()
        conn.execute("insert into tenants values (%s),(%s)", (nonbooking, inactive))
        conn.execute("insert into subscriptions values (%s,'email','active'),(%s,'scheduling','inactive')", (nonbooking, inactive))
        conn.execute("insert into scheduling_config(tenant_id,capacity_per_window) values(%s,2)", (old,))
        conn.execute("update scheduling_config set close_time='08:00' where tenant_id=%s", (old,))
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_SETTINGS_CONFLICT"):
            conn.execute(FORWARD.read_text(encoding="utf-8"))
        conn.execute("rollback")
        conn.execute("update scheduling_config set close_time='17:00' where tenant_id=%s", (old,))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        assert conn.execute("select tenant_id from scheduling_config where tenant_id in (%s,%s)",
                            (nonbooking, inactive)).fetchall() == []
        body = conn.execute("select pg_get_functiondef('enforce_booking_capacity()'::regprocedure)").fetchone()[0]
        assert body.count("BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved") == 1
        values = conn.execute("select capacity_per_window,timezone,booking_revision,booking_setting_review "
                              "from scheduling_config where tenant_id=%s", (old,)).fetchone()
        assert values[:3] == (2, "Europe/London", 1)
        assert values[3]["capacity"]["review_status"] == "carried_over_review"
        assert conn.execute("select capacity_per_window from scheduling_config where tenant_id=%s", (missing,)).fetchone() == (4,)
        conn.execute("insert into tenants values(%s)", (fresh,))
        conn.execute("insert into scheduling_config(tenant_id) values(%s)", (fresh,))
        assert conn.execute("select capacity_per_window,working_days,open_time,close_time,timezone "
                            "from scheduling_config where tenant_id=%s", (fresh,)).fetchone() == (None,)*5
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_BOOKING_UNSUPPORTED"):
            insert(conn, fresh)
        assert conn.execute("select relrowsecurity from pg_class where relname='booking_settings_rollback'").fetchone() == (True,)
        conn.execute("select apply_booking_settings(%s,1,%s)", (old, json.dumps({"capacity_per_window": 2, "booking_revision": 2})))
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_SETTINGS_STALE"):
            conn.execute("select apply_booking_settings(%s,1,%s)", (old, json.dumps({"capacity_per_window": 1, "booking_revision": 2})))
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert conn.execute("select capacity_per_window from scheduling_config where tenant_id=%s", (old,)).fetchone() == (2,)
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        insert(conn, old)
        insert(conn, old)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(conn, old)
        # Re-application must leave a new business unset, even after rollback.
        assert conn.execute("select capacity_per_window,timezone,booking_revision from scheduling_config "
                            "where tenant_id=%s", (fresh,)).fetchone() == (None, None, 0)
        conn.execute("select apply_booking_settings(%s,0,%s)", (fresh, json.dumps({
            "capacity_per_window": 1, "working_days": "mon,tue,wed,thu,fri",
            "open_time": "09:00", "close_time": "17:00", "timezone": "Europe/London", "booking_revision": 1})))
        # The columns are covered by the row's policy; rollback material is private.
        conn.execute("create role no_defaults_owner nologin")
        conn.execute("grant select,insert,update on scheduling_config to no_defaults_owner")
        conn.execute("grant execute on function apply_booking_settings(uuid,bigint,jsonb) to no_defaults_owner")
        conn.execute("alter table scheduling_config enable row level security")
        conn.execute("create policy owner_settings on scheduling_config to no_defaults_owner "
                     "using (tenant_id::text = current_setting('test.tenant')) "
                     "with check (tenant_id::text = current_setting('test.tenant'))")
        conn.execute("select set_config('test.tenant',%s,false)", (str(fresh),))
        conn.execute("set role no_defaults_owner")
        assert conn.execute("select count(*) from scheduling_config").fetchone() == (1,)
        assert conn.execute("update scheduling_config set booking_revision=99 where tenant_id=%s returning tenant_id", (old,)).fetchall() == []
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("select * from booking_settings_rollback")
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("select apply_booking_settings(%s,2,%s)", (old, json.dumps({"booking_revision": 3})))
        conn.execute("reset role")

    # Real contention after the new migration: exactly one customer gets the last place.
    attempted = threading.Event()
    def contender():
        with driver.connect(dsn) as conn:
            attempted.set()
            try:
                insert(conn, fresh)
                return "held"
            except driver.errors.RaiseException as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            insert(first, fresh)
            second = pool.submit(contender)
            assert attempted.wait(5)
        assert "BLAM8_CAPACITY_CONFLICT" in second.result(timeout=5)


def test_missing_function_pattern_aborts_entire_migration(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        setup_tenants(conn)
        tenant = uuid.uuid4()
        conn.execute("insert into tenants values (%s)", (tenant,))
        conn.execute("insert into subscriptions values (%s,'tms','active')", (tenant,))
        original = conn.execute("select pg_get_functiondef('enforce_booking_capacity()'::regprocedure)").fetchone()[0]
        pattern = "coalesce(c.capacity_per_window, 4)"
        conn.execute(original.replace(pattern, "coalesce(c.capacity_per_window, 5)"))
        def snapshot():
            return conn.execute("select pg_get_functiondef(oid) from pg_proc where pronamespace='public'::regnamespace order by oid").fetchall()
        before = snapshot()
        with pytest.raises(driver.errors.RaiseException) as error:
            conn.execute(FORWARD.read_text(encoding="utf-8"))
        conn.execute("rollback")
        assert "enforce_booking_capacity" in str(error.value)
        assert pattern in str(error.value)
        assert snapshot() == before
        assert conn.execute("select * from scheduling_config").fetchall() == []
        assert conn.execute("select to_regclass('booking_settings_rollback')").fetchone() == (None,)
        assert conn.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name='timezone'").fetchall() == []
        conn.execute("insert into scheduling_config(tenant_id) values (%s)", (tenant,))
        assert conn.execute("select capacity_per_window,working_days,open_time::text,close_time::text from scheduling_config").fetchone() == (4, "mon,tue,wed,thu,fri", "09:00:00", "17:00:00")
