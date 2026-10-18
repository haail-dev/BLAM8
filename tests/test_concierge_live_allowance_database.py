"""Run the live allowance functions against a disposable real PostgreSQL cluster."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/migrations/20261014_concierge_live_allowance.sql"
ROLLBACK = ROOT / "db/migrations/20261014_concierge_live_allowance.rollback.sql"


@pytest.fixture(scope="module")
def live_allowance_cluster():
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
    folder = Path(tempfile.mkdtemp(prefix="live-allowance-db-", dir=cache))
    data = folder / "data"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([str(binary / ("initdb" + suffix)), "-D", str(data), "-A", "trust",
                    "-U", "blam8_test", "--no-locale", "-E", "UTF8"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=flags, timeout=90)
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write("\nunix_socket_directories = ''\n")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (folder / "postgres.log").open("w")
    server = subprocess.Popen([str(binary / ("postgres" + suffix)), "-D", str(data),
        "-h", "127.0.0.1", "-p", str(port), "-c", "fsync=off"], stdin=subprocess.DEVNULL,
        stdout=log, stderr=log, creationflags=flags)
    dsn = f"hostaddr=127.0.0.1 port={port} user=blam8_test dbname=postgres connect_timeout=5"
    try:
        for _ in range(100):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                if server.poll() is not None:
                    raise RuntimeError((folder / "postgres.log").read_text())
                time.sleep(.1)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("create table tenants(id uuid primary key)")
            for role in ("anon", "authenticated", "service_role"):
                conn.execute(f'create role "{role}"')
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield dsn
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_server_activation_limits_and_london_rollover_are_metered(live_allowance_cluster):
    import psycopg
    dsn = live_allowance_cluster
    tenant, user = uuid.uuid4(), uuid.uuid4()
    today = date.today()
    london_day = datetime.now(ZoneInfo("Europe/London")).date()
    with psycopg.connect(dsn) as conn:
        conn.execute("insert into tenants(id) values (%s)", (tenant,))
        active = uuid.uuid4()
        conn.execute("select concierge_live_reserve(%s,%s,%s,%s,%s)",
                     (active, tenant, user, london_day, 600))
        before_activation = conn.execute("select concierge_live_status(%s,%s,%s,%s)",
                     (tenant, user, london_day, 600)).fetchone()[0]
        assert before_activation["used_seconds"] == 0
        started = conn.execute("select concierge_live_heartbeat(%s,%s,%s,%s)",
                               (active, tenant, user, 600)).fetchone()[0]
        assert started["ok"] is True
        conn.execute("update concierge_live_sessions set connected_at=now()-interval '30 seconds', reserved_until=now()+interval '1 minute' where id=%s", (active,))
        stopped = conn.execute("select concierge_live_heartbeat(%s,%s,%s,%s)",
                               (active, tenant, user, 15)).fetchone()[0]
        assert stopped["reason"] == "limit_reached"
        charged = conn.execute("select charged_seconds from concierge_live_sessions where id=%s", (active,)).fetchone()[0]
        assert charged == 15

        yesterday = london_day - timedelta(days=1)
        rollover = uuid.uuid4()
        conn.execute("select concierge_live_reserve(%s,%s,%s,%s,%s)",
                     (rollover, tenant, user, yesterday, 600))
        day_end = datetime.combine(yesterday + timedelta(days=1), datetime.min.time(), ZoneInfo("Europe/London")).astimezone(timezone.utc)
        conn.execute("update concierge_live_sessions set connected_at=%s,reserved_until=now()+interval '1 hour' where id=%s",
                     (day_end - timedelta(seconds=30), rollover))
        ended = conn.execute("select concierge_live_heartbeat(%s,%s,%s,%s)",
                             (rollover, tenant, user, 600)).fetchone()[0]
        assert ended["reason"] == "day_rollover"
        rolled_charge = conn.execute("select charged_seconds from concierge_live_sessions where id=%s", (rollover,)).fetchone()[0]
        assert rolled_charge == 30


def test_migration_rollback_removes_allowance_schema(live_allowance_cluster):
    import psycopg
    with psycopg.connect(live_allowance_cluster, autocommit=True) as conn:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        tables = conn.execute("""
            select tablename from pg_tables
            where schemaname = 'public' and tablename like 'concierge_live_%'
        """).fetchall()
        functions = conn.execute("""
            select proname from pg_proc
            where pronamespace = 'public'::regnamespace and proname like 'concierge_live_%'
        """).fetchall()
    assert tables == []
    assert functions == []
