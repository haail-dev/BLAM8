"""Cycle 2 offline fault injection, through actual claim/receipt query builders."""
import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app import db, concierge_routes as routes, proposal_outcomes as outcomes


def proposal():
    return dict(id="proposal-1", tenant_id="t1", user_id="u1", kind="add_kb_entry",
                payload={"secret": "private payload"}, status="pending", result={},
                created_at=datetime.now(timezone.utc).isoformat())


class Store:
    """Atomic in-memory PostgREST model; records the actual production filters."""
    def __init__(self):
        self.row = proposal()
        self.lock = threading.Lock()
        self.writes = []
        self.fail_claim_response = False
        self.fail_receipt = False
        self.lose_receipt_response = False

    def table(self, name):
        assert name == "concierge_proposals"
        return Query(self)


class Query:
    def __init__(self, store):
        self.store, self.filters, self.fields = store, [], None

    def update(self, fields): self.fields = fields; return self
    def select(self, *a): return self
    def limit(self, *a): return self
    def order(self, *a, **kw): return self
    def eq(self, key, value): self.filters.append((key, "eq", value)); return self
    def in_(self, key, value): self.filters.append((key, "in", value)); return self
    def gte(self, key, value): self.filters.append((key, "gte", value)); return self

    def execute(self):
        with self.store.lock:
            row = self.store.row
            for key, op, target in self.filters:
                value = (row.get("result", {}).get("execution", {}).get("id")
                         if key == "result->execution->>id" else row.get(key))
                if not {"eq": lambda: value == target, "in": lambda: value in target,
                        "gte": lambda: value is not None and value >= target}[op]():
                    return SimpleNamespace(data=[])
            if self.fields is not None:
                if self.store.fail_receipt and self.fields.get("status") != "executing":
                    raise OSError("receipt store unavailable")
                self.store.writes.append((copy.deepcopy(self.fields), copy.deepcopy(self.filters)))
                row.update(copy.deepcopy(self.fields))
                if self.store.fail_claim_response and row["status"] == "executing":
                    self.store.fail_claim_response = False
                    raise OSError("lost claim response after commit")
                if self.store.lose_receipt_response and row["status"] == "executed":
                    self.store.lose_receipt_response = False
                    raise OSError("lost receipt response after commit")
            return SimpleNamespace(data=[copy.deepcopy(row)])


@pytest.fixture
def setup(monkeypatch):
    store = Store()
    ctx = SimpleNamespace(tenant_id="t1", user_id="u1", client=store)
    monkeypatch.setattr(routes, "_asker", lambda ctx: None)
    monkeypatch.setattr(routes, "_remember_speech", lambda *a: None)
    monkeypatch.setattr(routes.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(routes.concierge_tasks, "settle_card", lambda *a, **k: None)
    monkeypatch.setattr(db, "add_concierge_message", lambda *a, **k: None)
    monkeypatch.setattr(routes.concierge_actions, "mailbox_refusal", lambda *a, **k: None)
    effects = []
    async def execute(*args, **kwargs):
        effects.append(kwargs)
        return {"summary": "Saved.", "link": {"label": "Open", "href": "/app/knowledge"}}
    monkeypatch.setattr(routes.concierge_actions, "execute", execute)
    return store, ctx, effects


def run(ctx, **kwargs):
    return asyncio.run(routes.business_execute(routes.BusinessExecuteRequest(proposal_id="proposal-1", **kwargs), ctx))


def test_concurrent_claim_persists_exact_intent_once():
    store = Store()
    def claim(i):
        return db.claim_concierge_proposal("proposal-1", "t1", client=store,
                                          execution_input={"draft_text": str(i), "selected": [2]}, confirmed_by="u1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(claim, range(8)))
    winners = [row for row in rows if row]
    assert len(winners) == len(store.writes) == 1
    execution = winners[0]["result"]["execution"]
    assert execution["operation_id"] == "proposal-1" and execution["confirmed_by"] == "u1"
    assert execution["input"]["selected"] == [2]
    assert store.row["status"] == "executing" and store.row["executed_at"] is None


def test_lost_claim_response_never_dispatches(setup):
    store, ctx, effects = setup
    store.fail_claim_response = True
    assert run(ctx).state == "executing"
    assert run(ctx).state == "executing"
    assert effects == []


def test_saved_result_replays_without_dispatch_and_keeps_intent(setup):
    store, ctx, effects = setup
    result = run(ctx, draft_text="edited private draft", selected=[0, 2])
    assert result.state == "completed" and result.ok
    assert run(ctx, draft_text="another edit").state == "completed"
    assert effects == [{"edited_draft_text": "edited private draft", "selected": [0, 2]}]
    assert store.row["result"]["execution"]["input"]["draft_text"] == "edited private draft"
    assert "private" not in str(result.model_dump())


@pytest.mark.parametrize("fault", ["fail_receipt", "lose_receipt_response"])
def test_post_effect_receipt_faults_do_not_retry(setup, fault):
    store, ctx, effects = setup
    setattr(store, fault, True)
    result = run(ctx)
    assert result.state == ("completed" if fault == "lose_receipt_response" else "unknown")
    assert len(effects) == 1
    assert run(ctx).state in {"completed", "executing", "unknown"}
    assert len(effects) == 1


def test_optional_decorations_cannot_erase_completion(setup, monkeypatch):
    store, ctx, effects = setup
    def fail(*a, **k): raise RuntimeError("decoration unavailable")
    monkeypatch.setattr(routes.trace, "emit", fail)
    monkeypatch.setattr(routes.concierge_tasks, "settle_card", fail)
    monkeypatch.setattr(db, "add_concierge_message", fail)
    assert run(ctx).state == "completed"
    assert outcomes.state(store.row) == "completed"
    assert run(ctx).state == "completed"
    assert len(effects) == 1


@pytest.mark.parametrize("error", [RuntimeError("provider secret"), asyncio.CancelledError(),
                                   routes.concierge_actions.ActionError("already sent then failed")])
def test_executor_entry_failure_is_unknown(setup, monkeypatch, error):
    store, ctx, effects = setup
    async def fail(*a, **k): effects.append("effect"); raise error
    monkeypatch.setattr(routes.concierge_actions, "execute", fail)
    result = run(ctx)
    assert result.state == store.row["status"] == "unknown"
    assert "secret" not in str(result.model_dump()) and "nothing changed" not in result.summary.lower()
    assert run(ctx).state == "unknown" and effects == ["effect"]


def test_readonly_stale_status_explicit_reconcile_and_owned_completion(setup):
    store, ctx, effects = setup
    claimed = db.claim_concierge_proposal("proposal-1", "t1", client=store)
    execution_id = claimed["result"]["execution"]["id"]
    store.row["result"]["execution"]["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    before = copy.deepcopy(store.row)
    assert routes.business_status("proposal-1", ctx).state == "unknown"
    assert store.row == before
    body = routes.BusinessExecuteRequest(proposal_id="proposal-1")
    assert routes.business_reconcile(body, ctx).state == "unknown"
    assert store.row["status"] == "unknown"
    receipt = outcomes.receipt(claimed, "completed", {"summary": "Saved."})
    assert not db.finish_concierge_proposal("proposal-1", "t1", "executed", receipt, store, execution_id="wrong")
    assert db.finish_concierge_proposal("proposal-1", "t1", "executed", receipt, store, execution_id=execution_id)
    assert not db.finish_concierge_proposal("proposal-1", "t1", "unknown", {}, store, execution_id=execution_id)
    assert outcomes.state(store.row) == "completed" and not effects


def test_cancel_claim_race_cannot_report_false_cancellation(setup):
    store, ctx, effects = setup
    claimed = db.claim_concierge_proposal("proposal-1", "t1", client=store)
    result = asyncio.run(routes.business_cancel(routes.BusinessCancelRequest(proposal_id="proposal-1"), ctx))
    assert result["state"] == "executing"
    assert not db.finish_concierge_proposal("proposal-1", "t1", "expired", client=store)
    assert store.row["status"] == "executing" and claimed


def test_cancel_before_claim_never_executes(setup):
    store, ctx, effects = setup
    result = asyncio.run(routes.business_cancel(routes.BusinessCancelRequest(proposal_id="proposal-1"), ctx))
    assert result["state"] == "cancelled"
    assert run(ctx).state == "cancelled" and not effects


def test_status_scopes_tenant_member_permission_mailbox(setup, monkeypatch):
    store, ctx, effects = setup
    with pytest.raises(HTTPException) as exc:
        routes.business_status("proposal-1", SimpleNamespace(tenant_id="other", user_id="u1", client=store))
    assert exc.value.status_code == 404
    monkeypatch.setattr(routes, "_asker", lambda ctx: {"active": True, "role": "member"})
    monkeypatch.setattr(routes, "people_is_admin", lambda m: False)
    ctx.user_id = "another"
    with pytest.raises(HTTPException): routes.business_status("proposal-1", ctx)
    ctx.user_id = "u1"
    monkeypatch.setattr(routes.concierge_actions, "may_do", lambda *a: False)
    with pytest.raises(HTTPException): routes.business_status("proposal-1", ctx)
    monkeypatch.setattr(routes.concierge_actions, "may_do", lambda *a: True)
    monkeypatch.setattr(routes.concierge_actions, "mailbox_refusal", lambda *a: "private mailbox")
    with pytest.raises(HTTPException): routes.business_status("proposal-1", ctx)
    assert not store.writes and not effects


def test_refused_after_claim_is_proven_failed(setup, monkeypatch):
    store, ctx, effects = setup
    monkeypatch.setattr(routes.concierge_actions, "may_do", lambda *a: False)
    assert run(ctx).state == "failed" and not effects


def terminal():
    row = proposal()
    row["status"] = "executed"
    row["result"] = {"summary": "Saved.", "execution": {"version": 2, "id": "owner-1",
        "operation_id": row["id"], "input": {}, "state": "completed"}}
    return row


@pytest.mark.parametrize("result", [None, [], "x", 7, {"summary": "Done."},
    {"summary": "The email didn't send"}, {"execution": []}, {"execution": "x"}])
def test_unsupported_receipts_never_mean_completed(result):
    row = terminal(); row["result"] = result
    assert outcomes.public(row)["state"] == "unknown"


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(ok=False), lambda r: r.update(state="unknown"), lambda r: r.update(results="x"),
    lambda r: r.update(summary=[]), lambda r: r["execution"].update(operation_id="other"),
    lambda r: r["execution"].update(state="executing"), lambda r: r["execution"].update(input=[]),
    lambda r: r.update(results=[{"index": 0, "ok": True, "state": "unknown", "summary": "?"}]),
    lambda r: r.update(results=[None]), lambda r: r.update(error="provider failure"),
    lambda r: r.update(results=[{"index": 0, "ok": True, "state": [], "summary": "x"}]),
    lambda r: r.update(results=[{"index": 0, "ok": True, "state": {}, "summary": "x"}]),
])
def test_contradictory_receipts_fail_closed(mutation):
    row = terminal(); mutation(row["result"])
    assert outcomes.state(row) == "unknown"


def test_positive_receipt_only_and_no_private_input_exposure():
    row = terminal(); row["result"]["execution"]["input"] = {"draft_text": "secret"}
    assert outcomes.public(row)["state"] == "completed"
    assert "secret" not in str(outcomes.public(row))


def test_failed_receipt_cannot_contain_effects():
    row = terminal(); row["status"] = "failed"
    row["result"]["execution"]["state"] = "failed"
    row["result"]["results"] = [{"index": 0, "ok": True, "state": "completed", "summary": "Sent."}]
    assert outcomes.state(row) == "unknown"


def test_recovery_not_hidden_by_forty_newer_successes():
    rows = [terminal() for _ in range(45)]
    old = proposal(); old.update(id="old", status="unknown")
    rows.append(old)
    class ListQuery:
        def __init__(self): self.filters = []
        def select(self, *a): return self
        def order(self, *a, **k): return self
        def limit(self, n): self.n = n; return self
        def eq(self, key, value): self.filters.append(lambda r: r.get(key) == value); return self
        def in_(self, key, values): self.filters.append(lambda r: r.get(key) in values); return self
        def execute(self): return SimpleNamespace(data=[r for r in rows if all(f(r) for f in self.filters)][:self.n])
    found = db.list_recovering_proposals("t1", "u1", client=SimpleNamespace(table=lambda n: ListQuery()))
    assert [r["id"] for r in found] == ["old"]


def test_resurface_cannot_rebuild_after_concurrent_claim(setup, monkeypatch):
    store, ctx, effects = setup
    store.row["payload"]["_action"] = {"kind": "add_kb_entry"}
    original = db.set_concierge_proposal_status
    def race(*args, **kwargs):
        db.claim_concierge_proposal("proposal-1", "t1", client=store)
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "set_concierge_proposal_status", race)
    def forbidden(*a, **k): raise AssertionError("fresh proposal created after claim")
    monkeypatch.setattr(routes.concierge_actions, "stage", forbidden)
    result = routes.business_resurface(routes.BusinessWaitingRequest(proposal_id="proposal-1"), ctx)
    assert not result["ok"] and store.row["status"] == "executing" and not effects


def test_postgrest_owned_filter_uses_json_text_path():
    from postgrest import SyncPostgrestClient
    with SyncPostgrestClient("http://127.0.0.1:1") as client:
        query = client.from_("concierge_proposals").update({"status": "executed"}).eq("result->execution->>id", "owner-1")
        assert query.request.params["result->execution->>id"] == "eq.owner-1"


def test_time_saved_reads_operation_identity(setup, monkeypatch):
    store, ctx, effects = setup
    run(ctx)
    selected = []
    original = Query.select
    def select(self, fields): selected.append(fields); return original(self, fields)
    monkeypatch.setattr(Query, "select", select)
    monkeypatch.setattr(db, "_page_all", lambda build: build().execute().data)
    assert db.time_saved_action_rows("t1", store)[0]["id"] == "proposal-1"
    assert "id" in selected[0].split(",")


def test_browser_recovery_replay():
    import shutil
    import subprocess
    node = shutil.which("node")
    assert node, "Node is required for the offline browser replay"
    completed = subprocess.run([node, "tests/replays/proposal_recovery_replay.mjs"], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_bulk_partial_unknown_preserves_known_success(monkeypatch):
    from app import concierge_actions as actions
    async def good(*a, **k): return {"summary": "First saved."}
    async def uncertain(*a, **k): raise actions.ActionError("external send may have happened")
    monkeypatch.setitem(actions._EXECUTORS, "add_kb_entry", good)
    monkeypatch.setitem(actions._EXECUTORS, "move_booking", uncertain)
    result = asyncio.run(actions._exec_bulk("t1", None, {"items": [
        {"kind": "add_kb_entry", "ok": True, "payload": {}},
        {"kind": "move_booking", "ok": True, "payload": {}}]}, None, selected=[0, 1]))
    assert result["state"] == "unknown"
    assert [item["state"] for item in result["results"]] == ["completed", "unknown"]
    assert "nothing changed" not in str(result).lower()


@pytest.mark.asyncio
async def test_bin_one_independent_proposal_does_not_cancel_sibling(setup,monkeypatch):
    store,ctx,effects=setup
    first=store.row
    second=copy.deepcopy(first);second['id']='proposal-2';second['payload']={'draft':'Independent second draft'}
    rows=[first,second]
    class MultiQuery(Query):
        def execute(self):
            matching=[row for row in rows if all(row.get(key)==value for key,op,value in self.filters)]
            if self.fields is not None:
                for row in matching:row.update(copy.deepcopy(self.fields))
                store.writes.append((copy.deepcopy(self.fields),copy.deepcopy(self.filters)))
            return SimpleNamespace(data=copy.deepcopy(matching))
    monkeypatch.setattr(store,'table',lambda name:MultiQuery(store))
    monkeypatch.setattr(routes,'_not_their_proposal',lambda *args:None)
    settled=[];monkeypatch.setattr(routes.concierge_tasks,'settle_card',lambda *args: settled.append(args))
    before=copy.deepcopy(second)
    response=await routes.business_cancel(routes.BusinessCancelRequest(proposal_id=first['id']),ctx)
    assert response['state']=='cancelled' and first['status']=='cancelled'
    assert second==before and effects==[]
    assert len(store.writes)==1 and ('id','eq',first['id']) in store.writes[0][1]
    assert ('tenant_id','eq','t1') in store.writes[0][1] and ('status','eq','pending') in store.writes[0][1]
    assert len(settled)==1 and settled[0][1]==first['id']
