import asyncio
import copy
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from app import auth, actor, concierge_delegation as delegation, concierge_history, db, journey


@pytest.fixture
def task_reads(monkeypatch):
    counts = Counter()
    member = {"user_id":"u", "role":"owner", "active":True, "scopes":{}}
    ctx = SimpleNamespace(tenant_id="t", user_id="u", client=object())
    def get_member(*args, **kwargs):
        counts["member"] += 1
        return copy.deepcopy(member) if member else None
    def modules(*args, **kwargs):
        counts["modules"] += 1
        return {"crm", "concierge"}
    monkeypatch.setattr(db, "get_member", get_member)
    monkeypatch.setattr(db, "visible_modules", modules)
    scope = delegation.access_scope("t", "u")[0]
    rows = {str(i):dict(id=str(i), tenant_id="t", user_id="u", revision=1, title=str(i),
                       status="done", result={"kind":"lookup_answer", "scope":scope, "summary":"read result"}) for i in range(7)}
    def task(tenant, user, task_id, **kwargs):
        counts["task"] += 1
        assert (tenant, user, kwargs["client"]) == ("t", "u", ctx.client)
        return copy.deepcopy(rows[task_id])
    monkeypatch.setattr(db, "get_concierge_task", task)
    counts.clear()
    return ctx, rows, member, counts


def test_seven_task_permission_query_reduction_and_fresh_request(task_reads):
    ctx, rows, member, counts = task_reads
    original = [delegation.scoped_task(ctx, key) for key in rows]
    assert counts == {"task":7, "member":14, "modules":7}
    counts.clear()
    permissions = delegation.task_read_permissions(ctx)
    projected = [delegation.scoped_task(ctx, key, permissions=permissions) for key in rows]
    assert projected == original
    assert counts == {"task":7, "member":2, "modules":1}
    # No cross-request cache: narrower permissions hide the old lookup result.
    member["role"] = "staff"
    fresh = delegation.task_read_permissions(ctx)
    assert delegation.scoped_task(ctx, "0", permissions=fresh) is None


def test_snapshot_binding_inactive_and_legacy_owner(task_reads):
    ctx, rows, member, counts = task_reads
    permissions = delegation.task_read_permissions(ctx)
    for foreign in (SimpleNamespace(tenant_id="else", user_id="u", client=ctx.client),
                    SimpleNamespace(tenant_id="t", user_id="else", client=ctx.client),
                    SimpleNamespace(tenant_id="t", user_id="u", client=object())):
        with pytest.raises(PermissionError):
            delegation.scoped_task(foreign, "0", permissions=permissions)
    member["active"] = False
    with pytest.raises(HTTPException) as denied:
        delegation.task_read_permissions(ctx)
    assert denied.value.status_code == 403
    member.clear()
    assert delegation.task_read_permissions(ctx).asker is None


def test_snapshot_keeps_proposal_mailbox_scope_and_public_checks(task_reads, monkeypatch):
    from app import concierge_actions
    ctx, rows, member, counts = task_reads
    rows["0"]["proposal_id"] = "p"
    proposals, mailboxes = [], []
    def proposal(proposal_id, tenant, **kwargs):
        proposals.append((proposal_id, tenant, kwargs["client"]))
        return {"id":proposal_id,"kind":"bulk","payload":{"items":[{"ok":True,"kind":"send_email"}]}}
    monkeypatch.setattr(db, "get_concierge_proposal", proposal)
    monkeypatch.setattr(concierge_actions, "may_do", lambda asker, kind: kind == "send_email")
    monkeypatch.setattr(concierge_actions, "mailbox_refusal", lambda *args: mailboxes.append(args) or None)
    permissions = delegation.task_read_permissions(ctx)
    for _ in range(2):
        assert delegation.scoped_task(ctx, "0", permissions=permissions)
    assert len(proposals) == len(mailboxes) == 2
    assert all(p == ("p","t",ctx.client) for p in proposals)
    monkeypatch.setattr(concierge_actions, "may_do", lambda *args: False)
    assert delegation.scoped_task(ctx, "0", permissions=permissions) is None
    monkeypatch.setattr(concierge_actions, "may_do", lambda *args: True)
    rows["1"]["result"] = {"kind":"needs_input"}
    assert delegation.scoped_task(ctx, "1", permissions=permissions) is None
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *args, **kwargs: None)
    assert delegation.scoped_task(ctx, "0", permissions=permissions) is None


def test_history_reuses_only_one_permission_snapshot_and_preserves_order(task_reads, monkeypatch):
    ctx, rows, member, counts = task_reads
    batch = "00000000-0000-0000-0000-000000000001"
    for row in rows.values():
        row["batch_id"] = batch
    monkeypatch.setattr(db, "list_concierge_tasks", lambda *args, **kwargs: list(rows.values()))
    history = [{"role":"assistant", "content":"Results [Tasks: "+batch+"]"}]
    concierge_history.attach_task_lists(ctx, history)
    import json
    tasks = json.loads(history[0]["tasks_json"])["tasks"]
    assert [t["id"] for t in tasks] == list(rows)
    # One permission snapshot and no per-task re-read of rows the history already holds.
    assert counts == {"member":2, "modules":1}
    assert all("payload" not in t and "instruction" not in t for t in tasks)


@pytest.mark.asyncio
async def test_auth_db_wait_keeps_loop_responsive_and_actor_on_request(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    observed = []
    async def user(token):return {"id":"u", "email":"owner@example.invalid"}
    def tenant(user_id):
        observed.append(threading.get_ident());entered.set()
        assert release.wait(2), "Auth read blocked event loop"
        return "t"
    monkeypatch.setattr(auth, "get_user", user)
    monkeypatch.setattr(auth.navcache, "peek", lambda key: (False,None))
    monkeypatch.setattr(auth.navcache, "put", lambda *args: None)
    monkeypatch.setattr(auth, "_token_ttl", lambda *args, **kwargs: 60)
    monkeypatch.setattr(db, "get_tenant_for_user", tenant)
    client = object()
    monkeypatch.setattr(db, "db_user", lambda token: client)
    monkeypatch.setattr(journey, "touch", lambda *args: observed.append(threading.get_ident()))
    request = Request({"type":"http","method":"GET","path":"/app/","headers":[(b"cookie",b"bl_at=fixture")]})
    async def unblock():
        assert await asyncio.to_thread(entered.wait, 1)
        release.set()
    helper = asyncio.create_task(unblock())
    try:
        ctx = await auth.require_tenant(request, Response())
        assert (ctx.tenant_id,ctx.user_id,ctx.client) == ("t","u",client)
        assert actor.current()["user_id"] == "u"
        assert observed and all(t != main_thread for t in observed)
        await helper
    finally:
        release.set();actor.forget()


@pytest.mark.asyncio
async def test_html_discretion_read_does_not_block_event_loop(monkeypatch):
    from app import discretion
    from app.main import _discretion
    from starlette.responses import HTMLResponse
    entered, release = threading.Event(), threading.Event()
    def enabled(tenant):
        entered.set()
        assert release.wait(2), "HTML middleware blocked event loop"
        return False
    monkeypatch.setattr(discretion,"enabled",enabled)
    request = Request({"type":"http","method":"GET","path":"/app/","headers":[]})
    request.state.tenant_id="t"
    response=HTMLResponse("unchanged")
    async def call_next(request):return response
    async def unblock():
        assert await asyncio.to_thread(entered.wait,1)
        release.set()
    helper=asyncio.create_task(unblock())
    try:
        assert await _discretion(request,call_next) is response
        await helper
    finally:release.set()


def test_journey_concurrent_cold_and_throttled_updates_are_serialized(monkeypatch):
    monkeypatch.setattr(journey, "_live", {})
    calls = Counter()
    entered, release = threading.Event(), threading.Event()
    def latest(tenant):
        calls["latest"] += 1
        entered.set()
        assert release.wait(2)
        return None
    def insert(tenant,user):calls["insert"] += 1;return {"id":"visit"}
    def bump(*args):calls["bump"] += 1
    monkeypatch.setattr(db,"latest_visit",latest)
    monkeypatch.setattr(db,"insert_visit",insert)
    monkeypatch.setattr(db,"bump_visit",bump)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(journey.touch,"t","u")
        assert entered.wait(1)
        rest = [pool.submit(journey.touch,"t","u") for _ in range(7)]
        # Hold the first cold DB read while competing workers reach touch.
        release.wait(.03)
        release.set()
        first.result()
        for future in rest:
            future.result()
    assert calls == {"latest":1,"insert":1}
    assert journey._live["t"].pending == 7
    journey._live["t"].last_write = datetime.now(timezone.utc)-timedelta(seconds=61)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:journey.touch("t","u"),range(8)))
    assert calls["bump"] == 1 and journey._live["t"].pending == 7
    def failed(*args):raise RuntimeError("offline fixture")
    monkeypatch.setattr(db,"insert_visit",failed)
    journey.touch("other","u")
    monkeypatch.setattr(db,"insert_visit",insert)
    journey.touch("other","u")
    assert "other" in journey._live
