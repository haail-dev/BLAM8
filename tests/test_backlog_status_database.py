"""Real PostgreSQL proof: two workers/entry points cannot claim one mailbox."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest
from tests.test_booking_capacity_database import cluster

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/migrations/20261018_mailbox_import_status.sql"
CHOICE = ROOT / "db/migrations/20261018_mailbox_import_choice.sql"


@pytest.fixture(scope="module")
def import_db(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""create table email_connections(id uuid primary key, tenant_id uuid not null, created_at timestamptz default now());
          create role anon; create role authenticated; create role service_role;
          grant select, update on email_connections to service_role;""")
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())  # idempotent upgrade
        conn.execute(CHOICE.read_text())
        conn.execute(CHOICE.read_text())
    return driver, dsn


def claim(database, tenant, mailbox, role="service_role"):
    driver, dsn = database
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("set role " + role)
        return conn.execute("select claim_mailbox_import(%s,%s)", (tenant, mailbox)).fetchone()[0]


def choose(database, tenant, mailbox, expected="", decision="import"):
    driver, dsn = database
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("set role service_role")
        return conn.execute("select choose_mailbox_import(%s,%s,%s,%s,%s)",
                            (tenant,mailbox,uuid.uuid4(),decision,expected)).fetchone()[0]


def test_concurrent_claims_tenant_isolation_and_expired_recovery(import_db):
    driver, dsn = import_db
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into email_connections(id,tenant_id) values (%s,%s)", (mailbox, tenant))
    assert claim(import_db, uuid.uuid4(), mailbox) is None
    assert claim(import_db, tenant, mailbox) is None
    assert choose(import_db, tenant, mailbox)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: claim(import_db, tenant, mailbox), range(8)))
    assert sum(bool(c) for c in claims) == 1
    first = next(c for c in claims if c)
    assert first["state"] == "running"
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""update email_connections set backlog_import = backlog_import ||
          jsonb_build_object('expires_at',now()-interval '1 minute') where id=%s""", (mailbox,))
    assert claim(import_db, tenant, mailbox) is None  # expiration is not another approval
    assert choose(import_db, tenant, mailbox, first["run_id"])
    second = claim(import_db, tenant, mailbox)
    assert second["run_id"] != first["run_id"]
    with driver.connect(dsn, autocommit=True) as conn:
        stale = conn.execute("""update email_connections set backlog_import='{"state":"complete"}'
          where id=%s and backlog_import->>'run_id'=%s returning id""", (mailbox, first["run_id"])).fetchall()
    assert stale == []


@pytest.mark.parametrize("decision", ["skip", "later"])
def test_skip_later_no_claim_and_explicit_later_import(import_db, decision):
    driver, dsn = import_db
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into email_connections(id,tenant_id) values (%s,%s)", (mailbox, tenant))
    assert choose(import_db, tenant, mailbox, decision=decision)
    assert claim(import_db, tenant, mailbox) is None
    assert choose(import_db, tenant, mailbox)
    run = claim(import_db, tenant, mailbox)
    assert run["approval"]["actor"] and run["window_end"]
    assert not choose(import_db, tenant, mailbox)  # replay or concurrent page
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("update email_connections set backlog_import=backlog_import || '{\"state\":\"complete\"}' where id=%s",(mailbox,))
    assert not choose(import_db, tenant, mailbox,run["run_id"])


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_browser_roles_cannot_claim(import_db, role):
    driver, _ = import_db
    with pytest.raises(driver.errors.InsufficientPrivilege):
        claim(import_db, uuid.uuid4(), uuid.uuid4(), role)
    with driver.connect(import_db[1], autocommit=True) as conn:
        conn.execute("set role " + role)
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("select choose_mailbox_import(%s,%s,%s,'import','')",
                         (uuid.uuid4(),uuid.uuid4(),uuid.uuid4()))


def test_rollback_disables_claim_instead_of_restoring_automatic_import(import_db):
    driver, dsn = import_db
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into email_connections(id,tenant_id) values (%s,%s)", (mailbox, tenant))
        conn.execute((ROOT / 'db/migrations/20261018_mailbox_import_choice.rollback.sql').read_text())
        assert conn.execute("select claim_mailbox_import(%s,%s)", (tenant,mailbox)).fetchone()[0] is None
        conn.execute(CHOICE.read_text())
