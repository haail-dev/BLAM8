"""Real PostgreSQL races on a disposable local cluster, never a supplied DSN."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/migrations/20260927_choose_offer.sql"


@pytest.fixture(scope="module")
def cluster():
    psycopg = pytest.importorskip("psycopg")
    binary = Path(os.environ["OUTREACH_TEST_POSTGRES_BIN"]) if os.environ.get("OUTREACH_TEST_POSTGRES_BIN") else None
    if binary is None:
        pg_config = shutil.which("pg_config")
        if pg_config:
            binary = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
    suffix = ".exe" if os.name == "nt" else ""
    if not binary or not (binary / ("initdb" + suffix)).exists():
        pytest.skip("PostgreSQL binaries are required for isolated database tests")
    cache = Path(os.environ.get("BLAM8_PG_TEST_TMP", ROOT / ".cache"))
    cache.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="booking-db-", dir=cache))
    data = folder / "data"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(tool, *args):
        # Windows server children inherit pipe handles; communicate() can wait
        # forever after pg_ctl exits. A file also preserves startup diagnostics.
        with (folder / "commands.log").open("a") as output:
            return subprocess.run([str(binary / (tool + suffix)), *map(str, args)],
                                  check=True, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=output, text=True, creationflags=flags, timeout=90)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    run("initdb", "-D", data, "-A", "trust", "-U", "blam8_test", "--no-locale", "-E", "UTF8")
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write("\nunix_socket_directories = ''\n")
    # pg_ctl's Windows token reduction fails inside an already restricted sandbox.
    # Launch only this newly initialised cluster, hidden, under the current token.
    with (folder / "postgres.log").open("w") as output:
        server = subprocess.Popen([str(binary / ("postgres" + suffix)), "-D", str(data),
                                   "-h", "127.0.0.1", "-p", str(port), "-c", "fsync=off"],
                                  stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                  creationflags=flags)
    # libpq uses this freshly created loopback cluster only, not PG* variables.
    dsn = f"hostaddr=127.0.0.1 port={port} user=blam8_test dbname=postgres connect_timeout=5"
    try:
        for attempt in range(100):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                if server.poll() is not None:
                    raise RuntimeError((folder / "postgres.log").read_text())
                time.sleep(0.1)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("""
                create role service_role;
                create table scheduling_config (
                  tenant_id uuid primary key, min_notice_hours int default 24,
                  windows text default 'day', capacity_per_window int default 4,
                  working_days text default 'mon,tue,wed,thu,fri',
                  open_time time default '09:00', close_time time default '17:00',
                  timezone text default 'Europe/London', booking_setting_review jsonb default '{}');
                create table bookings (
                  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
                  booking_date date, "window" text default 'day', contact text,
                  status text default 'proposed', expires_at timestamptz,
                  details jsonb default '{}', updated_at timestamptz);
            """)
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield psycopg, dsn
    finally:
        if server.poll() is None:
            run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
        server.wait(timeout=10)
        shutil.rmtree(folder)


def make_offer(conn, tenant, contact="c@example.test", expires="2099-01-01T00:00Z"):
    conn.execute("insert into scheduling_config(tenant_id) values (%s) on conflict do nothing", (tenant,))
    return conn.execute("insert into bookings(tenant_id, contact, status, expires_at, booking_date) "
                        "values (%s, %s, 'offered', %s, '2026-10-01') returning id",
                        (tenant, contact, expires)).fetchone()[0]


def choose(conn, tenant, booking):
    return conn.execute("select id from choose_offered_booking(%s,%s,null)", (booking, tenant)).fetchall()


def test_forward_rollback_forward(cluster):
    driver, dsn = cluster
    rollback = ROOT / "db/migrations/20260927_choose_offer.rollback.sql"
    with driver.connect(dsn, autocommit=True) as conn:
        tenant = uuid.uuid4()
        first = make_offer(conn, tenant)
        sibling = make_offer(conn, tenant)
        assert choose(conn, tenant, first) == [(first,)]
        assert conn.execute("select status from bookings where id=%s", (sibling,)).fetchone() == ("cancelled",)
        assert choose(conn, tenant, first) == []
        conn.execute(rollback.read_text(encoding="utf-8"))
        conn.execute(rollback.read_text(encoding="utf-8"))
        assert conn.execute("select count(*) from pg_proc where proname='choose_offered_booking'").fetchone() == (0,)
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        other = make_offer(conn, uuid.uuid4())
        assert choose(conn, tenant, other) == []
        expired = make_offer(conn, tenant, expires="2000-01-01T00:00Z")
        assert choose(conn, tenant, expired) == []


def test_simultaneous_sibling_choices_have_one_winner(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        a, b = make_offer(conn, tenant), make_offer(conn, tenant)
    ready = threading.Event()
    def contender():
        with driver.connect(dsn) as conn:
            ready.set()
            return choose(conn, tenant, b)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            assert choose(first, tenant, a) == [(a,)]
            future = pool.submit(contender)
            assert ready.wait(5)
            time.sleep(0.1)
            assert not future.done()
        assert future.result(timeout=5) == []


def test_capacity_guard_rolls_back_choice_and_sibling_release(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        tenant = uuid.uuid4()
        a, b = make_offer(conn, tenant), make_offer(conn, tenant)
        conn.execute("""create function reject_choice() returns trigger language plpgsql as $$
          begin if new.status in ('proposed', 'confirmed') then raise exception 'BLAM8_CAPACITY_CONFLICT'; end if;
          return new; end $$;
          create trigger reject_choice before update on bookings for each row execute function reject_choice();""")
        try:
            with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
                choose(conn, tenant, a)
            assert conn.execute("select status from bookings where id in (%s,%s)", (a,b)).fetchall() == [("offered",), ("offered",)]
        finally:
            conn.execute("drop trigger reject_choice on bookings; drop function reject_choice()")


def test_last_call_forward_rollback_forward_and_no_default(cluster):
    from datetime import time as clock_time
    driver, dsn = cluster
    migration = ROOT / "db/migrations/20260927_offer_last_call.sql"
    rollback = ROOT / "db/migrations/20260927_offer_last_call.rollback.sql"
    with driver.connect(dsn, autocommit=True) as conn:
        ids = [uuid.uuid4() for _ in range(5)]
        for tenant, notice in zip(ids, [4, 0, 24, 48, None]):
            conn.execute("insert into scheduling_config(tenant_id,min_notice_hours) values (%s,%s)", (tenant, notice))
        for _ in range(2):
            conn.execute(migration.read_text(encoding="utf-8"))
            rig = conn.execute("select offer_last_call,offer_last_call_review from scheduling_config where tenant_id=%s", (ids[0],)).fetchone()
            assert rig == (clock_time(20), "carried_over_review")
            for tenant in ids[1:]:
                assert conn.execute("select offer_last_call,offer_last_call_review from scheduling_config where tenant_id=%s", (tenant,)).fetchone() == (None, None)
            fresh = uuid.uuid4()
            conn.execute("insert into scheduling_config(tenant_id) values (%s)", (fresh,))
            conn.execute(migration.read_text(encoding="utf-8"))
            assert conn.execute("select offer_last_call,offer_last_call_review from scheduling_config where tenant_id=%s", (fresh,)).fetchone() == (None, None)
            conn.execute(rollback.read_text(encoding="utf-8"))
            conn.execute(rollback.read_text(encoding="utf-8"))
            assert conn.execute("select count(*) from information_schema.columns where table_name='scheduling_config' and column_name like 'offer_%'").fetchone() == (0,)
            assert conn.execute("select min_notice_hours from scheduling_config where tenant_id=%s", (ids[0],)).fetchone() == (4,)


def test_choice_keeps_a_separate_booking_kind(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        tenant = uuid.uuid4()
        a, sibling, separate = [make_offer(conn, tenant) for _ in range(3)]
        assert conn.execute("select id from choose_offered_booking(%s,%s,null,%s)", (a,tenant,[separate])).fetchall() == [(a,)]
        assert conn.execute("select status from bookings where id=%s", (separate,)).fetchone() == ("offered",)
        assert conn.execute("select status from bookings where id=%s", (sibling,)).fetchone() == ("cancelled",)


@pytest.mark.parametrize('lock_kind', ['day', 'office'])
def test_choice_waiting_for_capacity_lock_rechecks_expiry(cluster, lock_kind):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        offer = make_offer(conn, tenant)
    ready = threading.Event()
    def contender():
        with driver.connect(dsn) as conn:
            ready.set()
            return choose(conn, tenant, offer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as blocker:
            lock = str(tenant)+":2026-10-01" if lock_kind == 'day' else 'blam8:office:'+str(tenant)
            blocker.execute("select pg_advisory_xact_lock(hashtextextended(%s,0))", (lock,))
            future = pool.submit(contender)
            assert ready.wait(5)
            time.sleep(0.1)
            assert not future.done()
            blocker.execute("update bookings set expires_at='2000-01-01T00:00Z' where id=%s", (offer,))
        assert future.result(timeout=5) == []
