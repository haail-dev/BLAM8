import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app import backlog, db, mailbox
from tests.mailbox_import_fixture import approved_run


@pytest.fixture
def run(monkeypatch):
    saved = []
    monkeypatch.setattr(db, "claim_mailbox_import", lambda *a: approved_run())
    monkeypatch.setattr(db, "mailbox_import_access", lambda *a, **kw: {"backlog_import": approved_run()})
    monkeypatch.setattr(db, "finish_mailbox_import", lambda *args: saved.append(args[-1]))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **kw: None)
    monkeypatch.setattr(db, "get_connection_by_id", lambda *a: {"mailbox": "self@example.test"})
    monkeypatch.setattr(mailbox, "connection_provider", lambda *a: "google")
    async def token(*a): return "token"
    monkeypatch.setattr(mailbox, "valid_access_token", token)
    monkeypatch.setattr(db, "get_tenant_config", lambda *a: {})
    return saved


@pytest.mark.asyncio
async def test_observed_100_messages_reconcile_without_changing_filter(run, monkeypatch):
    async def ids(*a, **kw): return [str(i) for i in range(100)]
    async def get(tid, mid, **kw):
        if int(mid) >= 98: raise PermissionError("403")
        return {"from": {"address": f"sender{mid}@example.test"},
                "headers": {"auto-submitted": "auto-generated"} if int(mid) >= 5 else {}}
    rows = []
    monkeypatch.setattr(backlog, "recent_inbox_ids", ids)
    monkeypatch.setattr(mailbox, "get_message", get)
    monkeypatch.setattr(db, "get_email_log_by_message", lambda *a: None)
    def insert(*a, **kw): rows.append(kw); return ("id", True)
    monkeypatch.setattr(db, "insert_email_log", insert)
    assert await backlog.import_recent("tenant", "mailbox") == 98
    assert run[-1]["state"] == "partial"
    assert run[-1]["counts"] == dict(selected=100, review=5, automated=93, already_known=0,
                                     excluded=0, fetch_failed=2, filing_failed=0, unprocessed=0)
    assert sum(r["category"] == "automated" for r in rows) == 93
    assert all(r["status"] == "skipped" for r in rows)
    summary = backlog.outcome_summary(run[-1])
    assert "93 filed under Automated" in summary and "2 couldn't be fetched" in summary


@pytest.mark.asyncio
async def test_failure_and_cancellation_are_durable(run, monkeypatch):
    async def fail(*a, **kw): raise RuntimeError("listing failed")
    monkeypatch.setattr(backlog, "recent_inbox_ids", fail)
    assert await backlog.import_recent("tenant", "mailbox") == 0
    assert run[-1]["state"] == "failed"
    async def cancel(*a, **kw): raise asyncio.CancelledError()
    monkeypatch.setattr(backlog, "recent_inbox_ids", cancel)
    with pytest.raises(asyncio.CancelledError): await backlog.import_recent("tenant", "mailbox")
    assert run[-1]["state"] == "interrupted"


@pytest.mark.asyncio
async def test_start_claims_before_scheduling_and_does_not_overlap(monkeypatch):
    claims = []
    completed = []
    def claim(*a):
        claims.append(1)
        return {"run_id": "one"} if len(claims) == 1 else None
    async def worker(*a, **kw): completed.append(kw["run"])
    monkeypatch.setattr(db, "claim_mailbox_import", claim)
    monkeypatch.setattr(backlog, "import_recent", worker)
    assert backlog.start_import("t", "c")
    assert not backlog.start_import("t", "c")
    assert len(claims) == 2 and not completed
    await asyncio.sleep(0)
    assert completed == [{"run_id": "one"}]
    def unavailable(*a): raise RuntimeError("database down")
    monkeypatch.setattr(db, "claim_mailbox_import", unavailable)
    assert not backlog.start_import("t", "c")


def render(status):
    from pathlib import Path
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "src/app/templates"), autoescape=True)
    return env.get_template("_backlog_status.html").render(backlog_status=status, csrf_token=lambda r: "csrf",
                                                         request=SimpleNamespace())


@pytest.mark.parametrize("state,selected,ready,auto,fetch,phrase,retry", [
    ("running", 0, 0, 0, 0, "Getting your recent messages ready", False),
    ("complete", 0, 0, 0, 0, "No messages were found", False),
    ("complete", 93, 0, 93, 0, "No recent messages need review", False),
    ("partial", 100, 5, 93, 2, "5 recent messages are ready to review", True),
    ("failed", 0, 0, 0, 0, "did not finish", True),
    ("not_started", 0, 0, 0, 0, "Import older emails?", True),
])
def test_rendered_states(monkeypatch, state, selected, ready, auto, fetch, phrase, retry):
    run = {"state": state, "expires_at": (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),
           "counts": {"selected": selected, "automated": auto, "fetch_failed": fetch}}
    monkeypatch.setattr(db, "get_mailbox_import", lambda *a, **kw: run)
    monkeypatch.setattr(db, "mailbox_import_access", lambda *a, **kw: {"address": "example@test.invalid"})
    def count(tid, **kw):
        assert kw["mailbox_id"] == "mailbox" and kw["strict"]
        return ready
    monkeypatch.setattr(backlog, "count_rows", count)
    status = backlog.status("tenant", "mailbox")
    html = render(status)
    assert phrase in html
    assert ('data-backlog-retry' in html) == retry
    if auto: assert f"{auto} messages filed under Automated" in html
    if fetch: assert f"{fetch} messages couldn’t be fetched" in html


def test_expired_and_read_errors_are_not_empty_inbox(monkeypatch):
    monkeypatch.setattr(db, "mailbox_import_access", lambda *a, **kw: {})
    monkeypatch.setattr(db, "get_mailbox_import", lambda *a, **kw: {
        "state": "running", "expires_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
    monkeypatch.setattr(backlog, "count_rows", lambda *a, **kw: 0)
    assert backlog.status("t", "c")["state"] == "interrupted"
    def fail(*a, **kw): raise RuntimeError("read failed")
    monkeypatch.setattr(backlog, "count_rows", fail)
    status = backlog.status("t", "c")
    assert status["state"] == "unavailable" and not status["retry"]
    assert "No messages" not in render(status)


@pytest.mark.asyncio
async def test_duplicates_exclusions_and_filing_failures_reconcile(run, monkeypatch):
    async def ids(*a, **kw): return ["known", "race", "self", "no-sender", "write-fails"]
    async def get(tid, mid, **kw):
        address = "self@example.test" if mid == "self" else "sender@example.test"
        return {"from": {"address": "" if mid == "no-sender" else address}}
    monkeypatch.setattr(backlog, "recent_inbox_ids", ids)
    monkeypatch.setattr(mailbox, "get_message", get)
    monkeypatch.setattr(db, "get_email_log_by_message", lambda tid, mid: {"id": "known"} if mid == "known" else None)
    def insert(*a, **kw):
        if kw["source_message_id"] == "write-fails": raise RuntimeError("write failed")
        return ("race", False)
    monkeypatch.setattr(db, "insert_email_log", insert)
    assert await backlog.import_recent("t", "c") == 0
    c = run[-1]["counts"]
    assert c["already_known"] == 2 and c["excluded"] == 2 and c["filing_failed"] == 1
    assert c["selected"] == sum(v for k,v in c.items() if k != "selected")
    assert run[-1]["state"] == "partial"


@pytest.mark.asyncio
async def test_interrupted_run_reports_unprocessed_messages(run, monkeypatch):
    async def interrupted(tid, cid, days, limit, counts, **kwargs):
        counts.update(selected=10, review=2)
        raise asyncio.CancelledError()
    monkeypatch.setattr(backlog, "_import_messages", interrupted)
    with pytest.raises(asyncio.CancelledError): await backlog.import_recent("t", "c")
    assert run[-1]["counts"]["unprocessed"] == 8


@pytest.mark.asyncio
async def test_both_explicit_choice_routes_use_shared_handler(monkeypatch):
    from app import dashboard, onboarding
    ctx = SimpleNamespace(tenant_id="tenant", client=object(), user_id="owner")
    calls = []
    async def choose(request, context): calls.append((request, context))
    monkeypatch.setattr(backlog, "choose_import", choose)
    request = object()
    assert (await onboarding.mailbox_backlog(request, ctx)).headers["location"] == "/onboarding/mailbox"
    assert (await dashboard.settings_mailbox_backlog(request, ctx)).headers["location"] == "/app/settings"
    assert calls == [(request, ctx)] * 2


def test_strict_counts_are_mailbox_scoped_paginated_and_exclude_ignored(monkeypatch):
    class Query:
        offset = 0
        filters = []
        def select(self, *a): return self
        def eq(self, *a): self.filters.append(a); return self
        def like(self, *a): return self
        def order(self, *a): return self
        def range(self, start, end): self.offset = start; return self
        def execute(self):
            rows = ([{"from_address": "ignored@example.test", "category": "fyi"}] * 200 if self.offset == 0 else
                    [{"from_address": "real@example.test", "category": "fyi"},
                     {"from_address": "auto@example.test", "category": "automated"}])
            return SimpleNamespace(data=rows)
    q = Query()
    monkeypatch.setattr(db, "ignored_addresses", lambda *a, **kw: {"ignored@example.test"})
    assert backlog.count_rows("tenant", client=SimpleNamespace(table=lambda t:q), mailbox_id="mailbox", strict=True) == 1
    assert ("tenant_id", "tenant") in q.filters and ("mailbox_id", "mailbox") in q.filters


def test_status_endpoint_uses_authenticated_tenant_and_settings_retry_action(monkeypatch):
    from app import onboarding
    from starlette.requests import Request
    ctx = SimpleNamespace(tenant_id="tenant", client=object())
    seen = []
    def state(tid, client=None):
        seen.append((tid, client))
        return {"state": "partial", "message": "5 messages ready", "ready": 5, "retry": True}
    monkeypatch.setattr(backlog, "default_status", state)
    monkeypatch.setitem(onboarding.templates.env.globals, "csrf_token", lambda r: "token")
    request = Request({"type":"http", "method":"GET", "path":"/onboarding/mailbox/import-status", "headers":[]})
    response = onboarding.mailbox_import_status(request, "settings", ctx)
    assert response.headers["cache-control"] == "no-store"
    assert seen == [("tenant", ctx.client)]
    assert 'action="/app/settings/mailbox/backlog"' in response.body.decode()
