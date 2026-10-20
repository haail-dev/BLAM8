"""Cross-device claims and revisions against disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import uuid4

import pytest
from tests.test_booking_capacity_database import cluster

ROOT = Path(__file__).resolve().parents[1]
FORWARD = ROOT / "db/migrations/20261020_device_sync.sql"
ROLLBACK = ROOT / "db/migrations/20261020_device_sync.rollback.sql"


@pytest.fixture(scope="module")
def sync_db(cluster):
    driver, dsn = cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute(
            "create table tenants(id uuid primary key); create role anon; create role authenticated; create role service_role bypassrls;"
        )
        c.execute(FORWARD.read_text())
        c.execute(FORWARD.read_text())
    return driver, dsn


@pytest.fixture
def sync_case(sync_db):
    driver, dsn = sync_db
    tenant, user = uuid4(), uuid4()
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("insert into tenants values (%s)", (tenant,))
    return sync_db, tenant, user


def call(case, name, *args):
    (driver, dsn), tenant, user = case
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("set role service_role")
        placeholders = ",".join(["%s"] * (len(args) + 2))
        return c.execute(
            f"select {name}({placeholders})", (tenant, user, *args)
        ).fetchone()[0]


def claim(case, operation, revision=0, channel="flow", fingerprint="request"):
    return call(case, "claim_device_sync", channel, operation, fingerprint, revision)


def finish(case, operation, patch=None):
    return call(
        case,
        "finish_device_sync",
        operation,
        json.dumps(patch or {}),
        json.dumps(
            {"status": 303, "body": "", "headers": {"location": "/onboarding/mailbox"}}
        ),
    )


def test_two_devices_cannot_both_claim_the_same_revision(sync_case):
    def attempt(_):
        try:
            return claim(sync_case, uuid4())
        except Exception as exc:
            assert "SYNC_RUNNING" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(r is not None for r in pool.map(attempt, range(4))) == 1


def test_replay_after_reconnect_returns_original_result_and_does_not_write_again(
    sync_case,
):
    operation = uuid4()
    claim(sync_case, operation)
    first = finish(sync_case, operation, {"path": "/onboarding/mailbox"})
    assert first["revision"] == 1
    later = uuid4()
    claim(sync_case, later, 1)
    finish(sync_case, later, {"path": "/onboarding/voice"})
    replay = claim(sync_case, operation)  # old revision is intentional
    assert replay["replay"]["headers"]["location"] == "/onboarding/mailbox"
    repeated = finish(sync_case, operation, {"path": "/onboarding/interview"})
    assert repeated["revision"] == 2
    assert repeated["payload"]["path"] == "/onboarding/voice"


def test_stale_device_and_reused_operation_are_rejected(sync_case):
    operation = uuid4()
    claim(sync_case, operation)
    finish(sync_case, operation)
    with pytest.raises(Exception, match="SYNC_STALE"):
        claim(sync_case, uuid4())
    with pytest.raises(Exception, match="SYNC_REUSED"):
        claim(sync_case, operation, fingerprint="different body")


def test_accounts_and_tenants_have_independent_workspaces(sync_case):
    db, tenant, user = sync_case
    first = uuid4()
    claim(sync_case, first)
    finish(
        sync_case, first, {"messages": [{"role": "user", "content": "Private text"}]}
    )
    assert claim((db, tenant, uuid4()), uuid4())["payload"] == {}
    driver, dsn = db
    other = uuid4()
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("insert into tenants values (%s)", (other,))
    assert claim((db, other, user), uuid4())["payload"] == {}


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_browser_roles_cannot_read_or_claim_another_session(sync_case, role):
    (driver, dsn), tenant, user = sync_case
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("set role " + role)
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute("select * from device_sync")
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute(
                "select claim_device_sync(%s,%s,'flow',%s,'body',0)",
                (tenant, user, uuid4()),
            )


def test_unconfirmed_operation_blocks_reexecution_even_with_a_new_id(sync_case):
    operation = uuid4()
    claim(sync_case, operation)
    with pytest.raises(Exception, match="SYNC_RUNNING"):
        claim(sync_case, operation)
    with pytest.raises(Exception, match="SYNC_RUNNING"):
        claim(sync_case, uuid4())


def test_oversized_checkpoint_is_atomic(sync_case):
    operation = uuid4()
    claim(sync_case, operation)
    with pytest.raises(Exception):
        finish(sync_case, operation, {"text": "x" * 1000001})
    with pytest.raises(Exception, match="SYNC_RUNNING"):
        claim(sync_case, operation)


def test_rollback_reapply_and_tenant_deletion(sync_case):
    (driver, dsn), tenant, _ = sync_case
    operation = uuid4()
    claim(sync_case, operation)
    finish(sync_case, operation)
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("begin")
        c.execute(ROLLBACK.read_text().replace("begin;", "").replace("commit;", ""))
        assert c.execute("select to_regclass('device_sync')").fetchone()[0] is None
        c.execute(FORWARD.read_text().replace("begin;", "").replace("commit;", ""))
        c.execute("rollback")
        c.execute("delete from tenants where id=%s", (tenant,))
        assert (
            c.execute(
                "select count(*) from device_sync_operations where tenant_id=%s",
                (tenant,),
            ).fetchone()[0]
            == 0
        )
