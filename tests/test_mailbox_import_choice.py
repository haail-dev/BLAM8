"""Operation-specific approval, distinct from OAuth and new-mail sync."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import backlog, db, gmail, graph, mailbox
from app.auth import TenantCtx, require_tenant
from app.main import app
from tests.mailbox_import_fixture import approved_run


@pytest.fixture
def client(monkeypatch):
    calls = []
    app.dependency_overrides[require_tenant] = lambda: TenantCtx("tenant", None, user_id="owner")
    monkeypatch.setattr(db, "get_member", lambda *a: {"role": "owner", "active": True})
    monkeypatch.setattr(db, "mailbox_import_access", lambda *a, **kw: {"id": "selected"})
    monkeypatch.setattr(db, "choose_mailbox_import", lambda *a: calls.append(("choice", a)) or True)
    monkeypatch.setattr(backlog, "start_import", lambda *a: calls.append(("start", a)) or True)
    client = TestClient(app)
    client.cookies.set("bl_csrf", "choice")
    yield client, calls
    app.dependency_overrides.clear()


@pytest.mark.parametrize("decision", ["import", "skip", "later"])
def test_choice_is_explicit_mailbox_scoped_and_attributed(client, decision):
    http, calls = client
    response = http.post("/onboarding/mailbox/backlog", data={
        "csrf_token": "choice", "connection_id": "selected", "decision": decision, "expected_run": "previous"},
        follow_redirects=False)
    assert response.status_code == 303
    assert calls[0] == ("choice", ("tenant", "selected", "owner", decision, "previous"))
    assert len(calls) == (2 if decision == "import" else 1)


def test_csrf_missing_scope_denied_permissions_and_stale_choice_never_start(client, monkeypatch):
    http, calls = client
    data = {"connection_id": "selected", "decision": "import"}
    assert http.post("/onboarding/mailbox/backlog", data=data).status_code == 403
    assert calls == []
    data["csrf_token"] = "choice"
    monkeypatch.setattr(db, "get_member", lambda *a: {"role": "staff", "scopes": {}, "active": True})
    assert http.post("/onboarding/mailbox/backlog", data=data).status_code == 403
    assert calls == []
    monkeypatch.setattr(db, "get_member", lambda *a: {"role": "owner"})
    monkeypatch.setattr(db, "choose_mailbox_import", lambda *a: False)
    assert http.post("/onboarding/mailbox/backlog", data=data).status_code == 409
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["no_approval", "stale", "expired", "unavailable"])
async def test_worker_no_provider_access_without_current_approved_run(monkeypatch, bad):
    current = approved_run()
    if bad == "no_approval": current.pop("approval")
    if bad == "stale": current["run_id"] = "other"
    if bad == "expired": current["expires_at"] = "2020-01-01T00:00:00Z"
    def access(*a):
        if bad == "unavailable": raise RuntimeError("unavailable")
        return {"backlog_import": current}
    monkeypatch.setattr(db, "mailbox_import_access", access)
    calls = []
    monkeypatch.setattr(db, "get_connection_by_id", lambda *a: calls.append("connection"))
    monkeypatch.setattr(db, "finish_mailbox_import", lambda *a: None)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **kw: None)
    assert await backlog.import_recent("tenant", "mailbox", run=approved_run()) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_skip_means_no_claim_no_background_work(monkeypatch):
    monkeypatch.setattr(db, "claim_mailbox_import", lambda *a: None)
    async def fail(*a, **kw): pytest.fail("No worker should be created without approval")
    monkeypatch.setattr(backlog, "import_recent", fail)
    assert backlog.start_import("tenant", "mailbox") is False


@pytest.mark.asyncio
async def test_provider_queries_use_frozen_preconnection_window(monkeypatch):
    before = datetime(2026,10,18,20,0,tzinfo=timezone.utc)
    queries = []
    async def google(token, path, params): queries.append(params); return {}
    async def microsoft(token,path,params): queries.append(params); return {}
    monkeypatch.setattr(gmail, "_get", google)
    monkeypatch.setattr(graph, "graph_get", microsoft)
    await gmail.list_recent_inbox_ids("fake", before=before)
    await graph.list_recent_inbox_ids("fake", before=before)
    assert queries[0]["q"] == f"after:{int(before.timestamp())-14*86400} before:{int(before.timestamp())}"
    assert queries[1]["$filter"] == "receivedDateTime ge 2026-10-04T20:00:00Z and receivedDateTime lt 2026-10-18T20:00:00Z"
