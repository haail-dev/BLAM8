"""Opt-in REAL PostgreSQL tests in a disposable loopback-only cluster.

Set OUTREACH_TEST_POSTGRES_BIN to a directory containing initdb and pg_ctl.
Never reads the application's database URL or connects to a shared database.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import uuid

import pytest

from app.admin_outreach import validate_config


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    binaries = os.getenv("OUTREACH_TEST_POSTGRES_BIN")
    if not binaries:
        pytest.skip("Set OUTREACH_TEST_POSTGRES_BIN for isolated PostgreSQL tests")
    psycopg = pytest.importorskip("psycopg2")
    root = tmp_path_factory.mktemp("outreach-postgres")
    data = root / "data"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    suffix = ".exe" if os.name == "nt" else ""
    def command(name, *args):
        # Windows postgres inherits pipe handles; capture_output would wait for
        # the long-lived server to close them even after pg_ctl has exited.
        try:
            with (root / "commands.log").open("a", encoding="utf-8") as output:
                return subprocess.run([str(Path(binaries) / (name + suffix)), *map(str, args)],
                                      stdout=output, stderr=subprocess.STDOUT, check=True, timeout=60, **flags)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logs = "\n".join(f"{p.name}:\n{p.read_text(encoding='utf-8', errors='replace')[-8000:]}"
                             for p in (root / "commands.log", root / "server.log") if p.exists())
            raise RuntimeError(f"Disposable PostgreSQL {name} failed:\n{logs}") from exc
    command("initdb", "-D", data, "-A", "trust", "-U", "postgres", "--encoding=UTF8", "--no-locale")
    # All clients use the random loopback TCP port. Linux packages may default
    # their Unix socket to /var/run/postgresql, which the CI user cannot write.
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write("\nunix_socket_directories = ''\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command("pg_ctl", "-D", data, "-l", root / "server.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start")
    try:
        conn = psycopg.connect(host="127.0.0.1", port=port, dbname="postgres", user="postgres")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
            create role anon; create role authenticated; create role service_role bypassrls;
            create table vcrm_prospects (
              id uuid primary key default gen_random_uuid(),email text,kind text default 'partner',
              status text default 'IDENTIFIED',next_action text,next_action_date date,updated_at timestamptz);
            create table vcrm_calls(id uuid primary key default gen_random_uuid(),
              prospect_id uuid references vcrm_prospects(id) on delete cascade,
              summary text,created_at timestamptz default now());
            grant all on vcrm_prospects,vcrm_calls to service_role;
            insert into vcrm_prospects(id,email,status,next_action) values
              ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','Legacy@EXAMPLE.com','EMAILED',null),
              ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','stop@example.com','NOT_NOW','STOP REQUEST');
            insert into vcrm_calls(prospect_id,summary) values
              ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','Emailed 2 Oct 2026 from hello@example.com. Resend id legacy-id.'),
              ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','Emailed via Resend duplicate touch'),
              ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','Follow-up sent. Resend id followup-id.');
            """)
            migration = Path("db/migrations/20261010_admin_outreach.sql").read_text(encoding="utf-8")
            cur.execute(migration)
            cur.execute(migration)  # redeploy/apply twice is safe
        yield conn
        conn.close()
    finally:
        command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture
def sql(cluster):
    cluster.autocommit = False
    cur = cluster.cursor()
    yield cur
    cluster.rollback()
    cur.close()
    cluster.autocommit = True


def setup_send(cur):
    # Any current time except the last minute of UTC day; UTC assertions below
    # run in a window covering the whole day and a plan beginning in 2000.
    cfg = validate_config({"timezone": "UTC", "working_days": list(range(7)), "start_time": "00:00",
                           "end_time": "23:59", "daily_cap": 100, "plan": {"2000-01-01": 100}, "mode": "draft"})
    token, pid, mid = (str(uuid.uuid4()) for _ in range(3))
    cur.execute("delete from admin_outreach_messages")
    cur.execute("update admin_outreach_config set enabled=true,config=%s::jsonb,last_sync_at=now(),last_attempt_at=null", (json.dumps(cfg),))
    cur.execute("select admin_outreach_lock(%s)", (token,))
    assert cur.fetchone()[0] is True
    cur.execute("""insert into vcrm_prospects(id,email,status,company_number,email_source,email_verified,soft_match)
                  values(%s,'new@example.com','IDENTIFIED','12345678','https://example.com',true,false)""", (pid,))
    cur.execute("""insert into admin_outreach_messages(id,prospect_id,email,step,status,config_snapshot)
                  values(%s,%s,'new@example.com',0,'approved',%s::jsonb)""", (mid, pid, json.dumps(cfg)))
    return cfg, token, mid, pid


def claim(cur, cfg, token, mid, observed=0, ids=None):
    cur.execute("select admin_outreach_claim(%s,%s,%s::jsonb,%s,%s::text[])", (mid, token, json.dumps(cfg), observed, ids or []))
    return cur.fetchone()[0]


def test_migration_imports_history_and_suppressions(sql):
    sql.execute("select email,step from admin_outreach_messages order by step")
    assert sql.fetchall() == [("legacy@example.com", 0), ("legacy@example.com", 1)]
    sql.execute("select email from admin_outreach_suppressions")
    assert sql.fetchall() == [("stop@example.com",)]


def test_claim_only_once_and_completion_is_atomic_idempotent(sql):
    cfg, token, mid, pid = setup_send(sql)
    assert claim(sql, cfg, token, mid)
    assert not claim(sql, cfg, token, mid)
    for _ in range(2):
        sql.execute("select admin_outreach_complete(%s,'receipt-id',current_date+7)", (mid,))
    sql.execute("select status,next_action_date=current_date+7 from vcrm_prospects where id=%s", (pid,))
    assert sql.fetchone() == ("EMAILED", True)
    sql.execute("select count(*) from vcrm_calls where prospect_id=%s", (pid,))
    assert sql.fetchone()[0] == 1


@pytest.mark.parametrize("mutation", [
    "update admin_outreach_config set enabled=false",
    "update admin_outreach_config set last_sync_at=null",
    "update admin_outreach_config set last_sync_at=now()-interval '3 minutes'",
    "update admin_outreach_config set lease_until=now()-interval '1 second'",
    "update admin_outreach_config set last_attempt_at=now()",
    "update admin_outreach_messages set status='draft'",
    "update vcrm_prospects set email_verified=false where email='new@example.com'",
    "update vcrm_prospects set email=null where email='new@example.com'",
    "update vcrm_prospects set status='REPLIED' where email='new@example.com'",
    "insert into admin_outreach_suppressions(email,reason) values('new@example.com','stop')",
    "insert into vcrm_prospects(email,status) values('NEW@example.com','NOT_NOW')",
    "update admin_outreach_config set config=jsonb_set(config,'{plan}','{}')",
])
def test_claim_rechecks_every_safety_gate(sql, mutation):
    cfg, token, mid, pid = setup_send(sql)
    sql.execute(mutation)
    assert not claim(sql, cfg, token, mid)


def test_shared_cap_includes_followups_and_other_provider_mail(sql):
    cfg, token, mid, pid = setup_send(sql)
    assert not claim(sql, cfg, token, mid, observed=100)
    sql.execute("""insert into admin_outreach_messages(email,step,status,attempted_at,provider_id)
                   values('other@example.com',1,'sent',now(),'other-id')""")
    assert not claim(sql, cfg, token, mid, observed=99)
    assert claim(sql, cfg, token, mid, observed=99, ids=["other-id"])


def test_lease_rejects_second_worker_and_expired_token(sql):
    cfg, token, mid, pid = setup_send(sql)
    second = str(uuid.uuid4())
    sql.execute("select admin_outreach_lock(%s)", (second,))
    assert sql.fetchone()[0] is False
    sql.execute("update admin_outreach_config set lease_until=now()-interval '1 second'")
    sql.execute("select admin_outreach_lock(%s)", (second,))
    assert sql.fetchone()[0] is True
    assert not claim(sql, cfg, token, mid)


def test_plan_zero_and_window_are_enforced_in_database(sql):
    cfg, token, mid, pid = setup_send(sql)
    cfg["plan"] = {"2000-01-01": 0}
    sql.execute("update admin_outreach_config set config=%s::jsonb", (json.dumps(cfg),))
    sql.execute("update admin_outreach_messages set config_snapshot=%s::jsonb", (json.dumps(cfg),))
    assert not claim(sql, cfg, token, mid)
    cfg["plan"] = {"2000-01-01": 10}
    sql.execute("select to_char(current_date,'YYYY-MM-DD')")
    cfg["holidays"] = [sql.fetchone()[0]]
    sql.execute("update admin_outreach_config set config=%s::jsonb", (json.dumps(cfg),))
    sql.execute("update admin_outreach_messages set config_snapshot=%s::jsonb", (json.dumps(cfg),))
    assert not claim(sql, cfg, token, mid)


def test_browser_roles_cannot_execute_send_functions(sql):
    sql.execute("select has_function_privilege('authenticated','admin_outreach_claim(uuid,uuid,jsonb,integer,text[])','EXECUTE')")
    assert sql.fetchone()[0] is False
    sql.execute("select has_function_privilege('anon','admin_outreach_lock(uuid)','EXECUTE')")
    assert sql.fetchone()[0] is False
    sql.execute("select has_function_privilege('service_role','admin_outreach_complete(uuid,text,date)','EXECUTE')")
    assert sql.fetchone()[0] is True
    sql.execute("select has_table_privilege('authenticated','admin_outreach_messages','SELECT')")
    assert sql.fetchone()[0] is False


def test_service_role_can_reserve_and_complete(sql):
    cfg, token, mid, pid = setup_send(sql)
    sql.execute("set local role service_role")
    assert claim(sql, cfg, token, mid)
    sql.execute("select admin_outreach_complete(%s,'service-receipt',current_date+7)", (mid,))
    sql.execute("select status from admin_outreach_messages where id=%s", (mid,))
    assert sql.fetchone()[0] == "sent"


def test_concurrent_reservations_send_only_once(cluster):
    import concurrent.futures
    import psycopg2
    with cluster.cursor() as cur:
        cfg, token, mid, pid = setup_send(cur)
    def reserve():
        with psycopg2.connect(cluster.dsn) as conn:
            with conn.cursor() as cur:
                return claim(cur, cfg, token, mid)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(outcomes) == [False, True]
