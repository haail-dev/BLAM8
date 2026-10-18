"""Real PostgreSQL proof: two workers/entry points cannot claim one mailbox."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest
from tests.test_booking_capacity_database import cluster

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/migrations/20261018_mailbox_import_status.sql"


@pytest.fixture(scope="module")
def import_db(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""create table email_connections(id uuid primary key, tenant_id uuid not null);
          create role anon; create role authenticated; create role service_role;
          grant select, update on email_connections to service_role;""")
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())  # idempotent upgrade
    return driver, dsn


def claim(database, tenant, mailbox, role="service_role"):
    driver, dsn = database
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("set role " + role)
        return conn.execute("select claim_mailbox_import(%s,%s)", (tenant, mailbox)).fetchone()[0]


def test_concurrent_claims_tenant_isolation_and_expired_recovery(import_db):
    driver, dsn = import_db
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into email_connections(id,tenant_id) values (%s,%s)", (mailbox, tenant))
    assert claim(import_db, uuid.uuid4(), mailbox) is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: claim(import_db, tenant, mailbox), range(8)))
    assert sum(bool(c) for c in claims) == 1
    first = next(c for c in claims if c)
    assert first["state"] == "running"
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("""update email_connections set backlog_import = backlog_import ||
          jsonb_build_object('expires_at',now()-interval '1 minute') where id=%s""", (mailbox,))
    second = claim(import_db, tenant, mailbox)
    assert second["run_id"] != first["run_id"]
    with driver.connect(dsn, autocommit=True) as conn:
        stale = conn.execute("""update email_connections set backlog_import='{"state":"complete"}'
          where id=%s and backlog_import->>'run_id'=%s returning id""", (mailbox, first["run_id"])).fetchall()
    assert stale == []


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_browser_roles_cannot_claim(import_db, role):
    driver, _ = import_db
    with pytest.raises(driver.errors.InsufficientPrivilege):
        claim(import_db, uuid.uuid4(), uuid.uuid4(), role)
