"""A failed or interrupted synced request must not wedge the channel.

Real routes behind the sync wrapper, with the real migration functions on
disposable PostgreSQL.
"""

import json
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.testclient import TestClient
import pytest

from app import device_sync as sync
from app.auth import TenantCtx, require_tenant
from tests.test_device_sync import Store
from tests.test_device_sync_database import call, claim, cluster, sync_db, sync_case


class OperationStore(Store):
    """Adds the one table write the wrapper makes outside the migration functions."""

    def table(self, name):
        if name != "device_sync_operations":
            return super().table(name)
        self.name, self.filters, self.update_values = name, [], None
        return self

    def update(self, values):
        self.update_values = values
        return self

    def eq(self, name, value):
        if self.name == "device_sync_operations":
            self.filters.append((name + "=%s", value))
            return self
        return super().eq(name, value)

    def is_(self, name, value):
        assert value == "null"
        self.filters.append((name + " is null", None))
        return self

    def lt(self, name, value):
        self.filters.append((name + "<%s", value))
        return self

    def execute(self):
        if self.name != "device_sync_operations":
            return super().execute()
        (driver, dsn), _, _ = self.case
        where = " and ".join(clause for clause, _ in self.filters)
        values = [v for clause, v in self.filters if "%s" in clause]
        with driver.connect(dsn, autocommit=True) as c:
            c.execute("set role service_role")
            rows = c.execute(
                f"update device_sync_operations set response=%s where {where} returning id,started_at",
                (json.dumps(self.update_values["response"]), *values),
            ).fetchall()
        data = [dict(id=str(r[0]), started_at=r[1].isoformat()) for r in rows]
        return type("Result", (), {"data": data})()


@pytest.fixture
def harness(sync_case, monkeypatch):
    _, tenant, user = sync_case
    ctx = TenantCtx(str(tenant), None, str(user))

    async def auth(request: Request, response: Response):
        return ctx

    monkeypatch.setattr(sync.settings, "device_sync_enabled", True)
    monkeypatch.setattr(sync, "require_tenant", auth)
    monkeypatch.setattr(sync.db, "db", lambda: OperationStore(sync_case))
    app = FastAPI()
    app.dependency_overrides[require_tenant] = auth
    app.include_router(sync.router)
    return app, ctx


def state(client):
    return client.get("/device-sync/flow").json()


def checkpoint(client, revision):
    return client.post(
        "/device-sync/flow",
        json={"operation": str(uuid4()), "revision": revision, "patch": {"form": {}}},
        headers={"X-CSRF-Token": "test-csrf"},
    )


def submit(client, path, revision, operation):
    return client.post(
        path,
        data={
            "csrf_token": "test-csrf",
            "_sync_operation": str(operation),
            "_sync_revision": str(revision),
            "_sync_channel": "flow",
        },
        follow_redirects=False,
    )


def test_a_failed_handler_records_its_error_and_releases_the_channel(harness):
    app, _ = harness
    routes = APIRouter(route_class=sync.SyncRoute)
    runs = []

    @routes.post("/onboarding/voice/learn")
    async def broken():
        runs.append(True)
        raise RuntimeError("no running event loop")

    app.include_router(routes)
    operation = uuid4()
    with TestClient(app, cookies={"bl_csrf": "test-csrf"}) as client:
        failed = submit(client, "/onboarding/voice/learn", 0, operation)
        assert failed.status_code == 500
        assert failed.headers["X-Blam8-Revision"] == "1"
        # The owner's next edit, on this or another device, is accepted.
        assert checkpoint(client, state(client)["revision"]).status_code == 200
        # A retry of the same operation returns the recorded error; the
        # handler does not run twice.
        again = submit(client, "/onboarding/voice/learn", 0, operation)
        assert again.status_code == 500
        assert runs == [True]


def test_an_unconfirmed_claim_blocks_only_until_it_is_abandoned(harness, sync_case):
    (driver, dsn), tenant, user = sync_case
    app, _ = harness
    stopped = uuid4()
    claim(sync_case, stopped)  # its process stopped before recording an outcome
    with TestClient(app, cookies={"bl_csrf": "test-csrf"}) as client:
        assert checkpoint(client, 0).status_code == 409
        with driver.connect(dsn, autocommit=True) as c:
            c.execute(
                "update device_sync_operations set started_at=now()-interval '16 minutes' where id=%s",
                (stopped,),
            )
        # Releasing writes nothing, so the edit at the same revision goes through.
        assert checkpoint(client, 0).status_code == 200
        assert state(client)["revision"] == 1
    replay = claim(sync_case, stopped)["replay"]
    assert replay == sync.INTERRUPTED


def test_a_recent_unconfirmed_claim_still_blocks(harness, sync_case):
    app, _ = harness
    claim(sync_case, uuid4())
    with TestClient(app, cookies={"bl_csrf": "test-csrf"}) as client:
        assert checkpoint(client, 0).status_code == 409
        assert checkpoint(client, 0).status_code == 409
    assert call(sync_case, "claim_device_sync", "assistant", uuid4(), "other", 0)
