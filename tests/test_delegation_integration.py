"""C3-02/03/06/07: actual worker/control paths, with paid/network effects stubbed."""
import asyncio
import copy
from types import SimpleNamespace
import pytest
from app import concierge_tasks as tasks, concierge_delegation as delegation, db
from tests.test_voice_live import client


@pytest.fixture
def worker(monkeypatch):
    from app import concierge, concierge_data, concierge_actions, fairuse, usage
    row = dict(id="task", tenant_id="tenant", user_id="user", batch_id="batch", title="MOT price",
               instruction="Look up the MOT price", revision=1, row_version=0, status="queued",
               commit_state="preparation", result={"scope":"scope"})
    published, operations = [], []
    monkeypatch.setattr(delegation, "enabled", lambda: True)
    monkeypatch.setattr(delegation, "access_scope", lambda *a: ("scope", None, ["crm"]))
    monkeypatch.setattr(db, "get_concierge_task", lambda *a, **k: copy.deepcopy(row))
    monkeypatch.setattr(tasks, "_publish", lambda *a: published.append(copy.deepcopy(a[-1])))
    monkeypatch.setattr(fairuse, "consume", lambda *a: (True,None))
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_data, "tools_for", lambda *a: [{"name":"lookup"}])
    async def lookup(*a, **k): return {"price":"£45 including VAT"}
    monkeypatch.setattr(concierge_data, "execute", lookup)
    def transition(op, *a, revision=None, attempt_id=None, data=None, **kw):
        operations.append(op)
        if revision != row["revision"]: return {"ok":False}
        if op=="claim" and row["status"]=="queued":
            row.update(status="working",attempt_id=attempt_id)
        elif op=="finish" and row["status"]=="working" and row["attempt_id"]==attempt_id:
            row.update(status=data["status"],result=data["result"],outcome=data["summary"],event_id="event")
        else:return {"ok":False}
        return {"ok":True,"task":copy.deepcopy(row)}
    monkeypatch.setattr(delegation,"transition",transition)
    def answer(reply="I sent it.", action=None, read=True):
        async def model(*a,tool_executor,**kw):
            if read: await tool_executor("lookup",{})
            return SimpleNamespace(reply=reply,action=action,model="test",usage={})
        monkeypatch.setattr(concierge,"answer_business",model)
    async def run(): await tasks._run_revision(copy.deepcopy(row),tenant={},tenant_id="tenant",user_id="user")
    return SimpleNamespace(row=row,published=published,operations=operations,answer=answer,run=run,actions=concierge_actions)


def test_lookup_does_not_authorize_model_business_success(worker):
    worker.answer("I sent it and moved the booking.")
    asyncio.run(worker.run())
    assert worker.row["result"]["kind"]=="lookup_answer"
    assert "No change has been executed" in worker.row["outcome"]
    assert "sent it" not in str(worker.row["result"])
    assert worker.row["result"]["reads"][0]["result"]["price"]=="£45 including VAT"
    assert worker.published[-1]["event_id"]=="event"


@pytest.mark.parametrize("reply,read", [("",True),("Done.",False)])
def test_empty_or_ungrounded_model_reply_is_not_verified_done(worker,reply,read):
    worker.answer(reply,read=read);asyncio.run(worker.run())
    assert worker.row["status"]=="needs_you" and worker.row["result"]["kind"]=="unknown"


def test_revision_during_blocked_lookup_cannot_finish_or_publish(worker,monkeypatch):
    from app import concierge_data
    async def lookup(*a,**k):
        worker.row.update(revision=2,status="queued",instruction="Look up the tyre price")
        return {"price":"old"}
    monkeypatch.setattr(concierge_data,"execute",lookup)
    worker.answer();asyncio.run(worker.run())
    assert worker.operations==["claim"]
    assert all(row["status"]=="working" for row in worker.published)
    assert worker.row["revision"]==2


def test_lost_staging_response_cannot_overwrite_saved_waiting(worker,monkeypatch):
    def stage(*a,**k):
        assert k["task_binding"]["revision"]==1
        worker.row.update(status="waiting",proposal_id="proposal",result={"kind":"waiting"})
        raise OSError("response lost after commit")
    monkeypatch.setattr(worker.actions,"stage",stage)
    worker.answer(action={"kind":"move_booking"});asyncio.run(worker.run())
    assert worker.row["status"]=="waiting" and worker.row["proposal_id"]=="proposal"
    assert all(row["status"]!="failed" for row in worker.published)


def test_database_finish_failure_emits_no_constructed_terminal_hint(worker,monkeypatch):
    original=delegation.transition
    def unavailable(op,*a,**k):
        if op=="finish":raise OSError("DB unavailable")
        return original(op,*a,**k)
    monkeypatch.setattr(delegation,"transition",unavailable)
    worker.answer()
    with pytest.raises(OSError):asyncio.run(worker.run())
    assert len(worker.published)==1 and worker.published[0]["status"]=="working"


def test_scope_revocation_hides_lookup_and_private_question(monkeypatch):
    from app import concierge_routes
    ctx=SimpleNamespace(tenant_id="tenant",user_id="user",client=None)
    monkeypatch.setattr(concierge_routes,"_live_asker",lambda *a:None)
    monkeypatch.setattr(delegation,"access_scope",lambda *a:("new",None,[]))
    for kind in ["lookup_answer","needs_input"]:
        monkeypatch.setattr(db,"get_concierge_task",lambda *a,kind=kind,**k:dict(revision=1,result={"kind":kind,"scope":"old","summary":"Private"}))
        assert delegation.scoped_task(ctx,"task") is None


def test_single_task_ack_and_control_dispatch_are_real_starter_paths(monkeypatch):
    from app import concierge_routes
    ctx=SimpleNamespace(tenant_id="tenant",user_id="user",client=None,viewing_as=None)
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    monkeypatch.setattr(concierge_routes,"_live_asker",lambda *a:None)
    monkeypatch.setattr(delegation,"create",lambda *a,**k:[dict(id="task",batch_id="batch",title="MOT",status="queued",revision=1)])
    launched=[];monkeypatch.setattr(tasks,"launch",lambda *a:launched.append(a))
    starter=tasks.Starter(ctx,surface="voice",source_turn="turn",voice_session="session")
    out=asyncio.run(starter.start({"tasks":[{"title":"MOT","instruction":"Look up the MOT price"}]}))
    assert out["started"]==1 and "background" in out["_final_reply"] and len(launched)==1
    controls=[]
    monkeypatch.setattr(delegation,"scoped_task",lambda *a:{"id":"task","voice_session":"session"})
    monkeypatch.setattr(db,"list_session_concierge_tasks",lambda *a,**k:[])
    monkeypatch.setattr(delegation,"control",lambda *a:controls.append(a) or {"ok":True,"task":{"batch_id":"batch"}})
    assert asyncio.run(starter.control(dict(task_id="task",revision=1,operation="revise",instruction="Look up the tyre price")))["ok"]
    assert controls[0][1:4]==("task",1,"revise")
    assert {tool["name"] for tool in tasks.foreground_tools()}=={"start_tasks","control_task"}


def test_fragment_correction_requires_full_replacement(monkeypatch):
    ctx=SimpleNamespace(viewing_as=None)
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    monkeypatch.setattr(delegation,"scoped_task",lambda *a:{"revision":1})
    monkeypatch.setattr(delegation,"transition",lambda *a,**k:pytest.fail("Fragment must not transition"))
    for fragment in ["Friday instead please","Move it to Monday","Move Monday please"]:
        assert delegation.control(ctx,"task",1,"revise",fragment)["state"]=="needs_input"


@pytest.mark.parametrize("status", ["working", "waiting", "ambiguous"])
def test_actual_voice_route_model_loop_delegates_then_revises(client,monkeypatch,status):
    import json
    import uuid
    from app import concierge_routes as routes, llm
    routes.voice_turn_state._states.clear()
    session=str(uuid.uuid4()); task_id=str(uuid.uuid4()); batch=str(uuid.uuid4())
    row=dict(id=task_id,tenant_id="t1",user_id="u1",batch_id=batch,title="Move Bella",
             instruction="Move Bella to Friday",status="queued",revision=1,row_version=0,
             voice_session=session,result={"scope":"scope"},commit_state="preparation")
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    monkeypatch.setattr(delegation,"access_scope",lambda *a:("scope",None,["crm"]))
    monkeypatch.setattr(db,"visible_modules",lambda *a,**k:{"crm"})
    monkeypatch.setattr(db,"get_tenant",lambda *a,**k:{"name":"Test"})
    monkeypatch.setattr(db,"get_tenant_config",lambda *a,**k:{})
    monkeypatch.setattr(db,"list_waiting_drafts",lambda *a,**k:[])
    monkeypatch.setattr(db,"get_concierge_task",lambda *a,**k:copy.deepcopy(row) if a[2]==task_id else None)
    monkeypatch.setattr(db,"list_session_concierge_tasks",lambda *a,**k:[copy.deepcopy(row)] if a[2]==session else [])
    monkeypatch.setattr(routes.fairuse,"consume",lambda *a:(True,None))
    monkeypatch.setattr(routes.trace,"emit",lambda *a,**k:None)
    monkeypatch.setattr(routes.usage,"record_anthropic",lambda *a,**k:None)
    monkeypatch.setattr(tasks,"launch",lambda *a:None)
    monkeypatch.setattr(routes,"finish_business_turn",lambda ctx,result,**k:dict(reply=result.reply,links=[],proposal=None,tasks=k.get("tasks")))
    calls=[]
    def transition(op,tenant,user,**kw):
        calls.append((op,tenant,user,kw))
        if op=="create":
            assert kw["data"]["source_turn"]=="delegate-turn" and kw["data"]["voice_session"]==session
            return {"ok":True,"tasks":[copy.deepcopy(row)]}
        assert op=="revise" and kw["revision"]==1 and kw["task_id"]==task_id
        row.update(revision=2,status="queued",instruction=kw["data"]["instruction"],proposal_id=None,result={"scope":"scope"})
        return {"ok":True,"task":copy.deepcopy(row)}
    monkeypatch.setattr(delegation,"transition",transition)
    scripted=[("start_tasks",{"tasks":[{"title":"Move Bella","instruction":"Move Bella to Friday"}]})]
    systems=[]
    async def provider(on_delta,**kw):
        from app import concierge_warm
        assert kw["tools"] == dict(concierge_warm.tool_sets())["owner"]
        systems.append((kw["system"],copy.deepcopy(kw["messages"])))
        if scripted:
            name,args=scripted.pop(0)
            content=[SimpleNamespace(type="tool_use",id="tool",name=name,input=args)];stop="tool_use"
        else:
            text=json.dumps({"reply":"I've updated the request. Approval is still required.","links":[],"action":{"kind":""}})
            await on_delta(text);content=[SimpleNamespace(type="text",text=text)];stop="end_turn"
        return SimpleNamespace(content=content,stop_reason=stop,model="test",usage=SimpleNamespace())
    monkeypatch.setattr(llm,"stream",provider)
    response=client.post("/concierge/voice/chat",json={"question":"Move Bella to Friday", "turn_id":"delegate-turn","voice_session":session})
    assert response.status_code==200 and '"t": "done"' in response.text and '"started"' not in response.text
    assert [call[0] for call in calls]==["create"] and "background" in response.text
    row["status"]=status
    if status=="ambiguous":
        row["status"]="working"
        second={**row,"id":str(uuid.uuid4()),"title":"Email Bob","instruction":"Prepare an email to Bob"}
        monkeypatch.setattr(db,"get_concierge_task",lambda *a,**k:copy.deepcopy(row if a[2]==task_id else second))
        monkeypatch.setattr(db,"list_session_concierge_tasks",lambda *a,**k:[copy.deepcopy(row),copy.deepcopy(second)])
    scripted.append(("control_task",dict(task_id=task_id,revision=1,operation="revise",instruction="Move Bella to Monday")))
    response=client.post("/concierge/voice/chat",json={"question":"Actually Monday instead" if status=="ambiguous" else "Actually Monday for Bella", "turn_id":"revise-turn","voice_session":session})
    assert response.status_code==200 and '"t": "done"' in response.text
    if status=="ambiguous":
        assert row["revision"]==1 and [call[0] for call in calls]==["create"]
        assert "Which task" in response.text and "I've updated" not in response.text
        return
    assert row["revision"]==2 and row["instruction"]=="Move Bella to Monday"
    assert task_id in str(systems[-1])


def test_voice_control_cannot_target_another_session(monkeypatch):
    ctx=SimpleNamespace(tenant_id="t",user_id="u",client=None,viewing_as=None)
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    monkeypatch.setattr(delegation,"scoped_task",lambda *a:{"voice_session":"old-session"})
    monkeypatch.setattr(delegation,"control",lambda *a:pytest.fail("Wrong session must not mutate"))
    result=asyncio.run(tasks.Starter(ctx,surface="voice",voice_session="new-session").control(dict(task_id="task",revision=1,operation="cancel")))
    assert not result["ok"]


def test_delivery_route_sanitizes_failed_snapshot_and_requires_csrf(client,monkeypatch):
    import uuid
    from app import concierge_routes as routes
    from app.main import app
    from app.security import require_csrf_header
    task_id,session,event,delivery_id=[str(uuid.uuid4()) for _ in range(4)]
    row=dict(id=task_id,revision=1,voice_session=session,status="waiting",result={"kind":"waiting","summary":"Prepared."},instruction="Private original instruction",attempt_id="private-attempt")
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    monkeypatch.setattr(delegation,"scoped_task",lambda *a:copy.deepcopy(row))
    monkeypatch.setattr(delegation,"transition",lambda *a,**k:{"ok":False,"state":"stale","task":copy.deepcopy(row)})
    body=dict(task_id=task_id,revision=1,event_id=event,voice_session=session,delivery_id=delivery_id,operation="claim")
    response=client.post('/concierge/business/task-delivery',json=body)
    assert response.status_code==200 and not response.json()["ok"]
    assert "Private original" not in response.text and "private-attempt" not in response.text
    body["voice_session"]=str(uuid.uuid4())
    assert client.post('/concierge/business/task-delivery',json=body).json()["state"]=="unavailable"
    del app.dependency_overrides[require_csrf_header]
    assert client.post('/concierge/business/task-delivery',json=body).status_code==403


def test_closed_voice_notice_is_durable_generic_and_deduplicated(monkeypatch):
    from app import push
    row=dict(id="task",revision=1,event_id="event",status="failed",title="Private customer",result={"kind":"failed"})
    monkeypatch.setattr(db,"list_concierge_tasks",lambda *a,**k:[row])
    claimed=set();messages=[];notifications=[]
    def claim(*a,**k):
        event=k["data"]["event_id"]
        if event in claimed:return {"ok":False}
        claimed.add(event);return {"ok":True}
    monkeypatch.setattr(delegation,"transition",claim)
    monkeypatch.setattr(db,"add_concierge_message",lambda *a,**k:messages.append((a,k)))
    monkeypatch.setattr(push,"notify",lambda *a,**k:notifications.append((a,k)))
    tasks._finish_batch("tenant","user","batch");tasks._finish_batch("tenant","user","batch")
    assert len(messages)==len(notifications)==1 and messages[0][1]["user_id"]=="user"
    assert "Private customer" not in str(messages)+str(notifications) and "done" not in messages[0][0][2]


@pytest.mark.parametrize("denial", ["unavailable", "inactive", "ability", "mailbox", "view_as"])
def test_task_control_endpoint_refuses_current_authorization_loss(client,monkeypatch,denial):
    import uuid
    from app import concierge_routes as routes
    from app.auth import TenantCtx,require_onboarded
    from app.main import app
    from fastapi import HTTPException
    task_id=str(uuid.uuid4())
    row=dict(id=task_id,tenant_id="t1",user_id="u1",revision=1,status="waiting",proposal_id="proposal",result={})
    monkeypatch.setattr(delegation,"enabled",lambda:True)
    seen=[]
    def read(tenant,user,identity,**kw):
        seen.append((tenant,user,identity));return None if denial=="unavailable" else copy.deepcopy(row)
    monkeypatch.setattr(db,"get_concierge_task",read)
    monkeypatch.setattr(delegation,"access_scope",lambda *a:("scope",None,[]))
    monkeypatch.setattr(db,"get_concierge_proposal",lambda *a,**k:{"kind":"draft_reply","payload":{}})
    def member(*a):
        if denial=="inactive":raise HTTPException(status_code=403)
        return {"active":True}
    monkeypatch.setattr(routes,"_live_asker",member)
    monkeypatch.setattr(routes.concierge_actions,"may_do",lambda *a:denial!="ability")
    monkeypatch.setattr(routes.concierge_actions,"mailbox_refusal",lambda *a:"Mailbox unavailable" if denial=="mailbox" else None)
    monkeypatch.setattr(delegation,"transition",lambda *a,**k:pytest.fail("Denied request must not transition"))
    if denial=="view_as":
        app.dependency_overrides[require_onboarded]=lambda:TenantCtx(tenant_id="t1",user_id="u1",client=None,viewing_as="owner")
    response=client.post('/concierge/business/task-control',json=dict(task_id=task_id,revision=1,operation="cancel"))
    assert response.status_code in {200,403}
    if response.status_code==200:assert not response.json()["ok"]
    if seen:assert seen[0]==("t1","u1",task_id)


def test_typed_session_query_uses_sql_null_filter(monkeypatch):
    calls=[]
    class Query:
        def __getattr__(self,name):
            def call(*a,**kw):calls.append((name,a));return self
            return call
        def execute(self):return SimpleNamespace(data=[])
    db.list_session_concierge_tasks("tenant","user",None,client=Query())
    assert ("is_",("voice_session","null")) in calls
    assert ("eq",("tenant_id","tenant")) in calls and ("eq",("user_id","user")) in calls


@pytest.mark.parametrize("status,receipt_state,kind,public_state",[
    ("pending",None,"waiting","pending"),
    ("executed","completed","action_completed","completed"),
    ("failed","failed","failed","failed"),
    ("cancelled",None,"cancelled","cancelled"),
    ("unknown",None,"unknown","unknown"),
    ("executing","executing","unknown","executing"),
    ("executed",None,"unknown","unknown"),
])
def test_task_public_preserves_authoritative_proposal_outcome_states(monkeypatch,status,receipt_state,kind,public_state):
    from datetime import datetime, timezone
    proposal={"id":"proposal","status":status,"result":{}}
    if receipt_state:
        proposal["result"]={"state":receipt_state,"summary":"Verified fixture result","ok":receipt_state=="completed",
            "execution":{"version":2,"id":"claim","operation_id":"proposal","input":{},"state":receipt_state,
                         "started_at":datetime.now(timezone.utc).isoformat()}}
    monkeypatch.setattr(db,"get_concierge_proposal",lambda *a,**k:proposal)
    projected=delegation.public({"tenant_id":"tenant","proposal_id":"proposal","status":"done","result":{"kind":"action_receipt"}})
    assert projected["result_kind"]==kind and projected["proposal_state"]==public_state
