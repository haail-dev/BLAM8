"""Knowledge readiness follows a typical enquiry, the saved facts and the owner's approval."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import app
from app import knowledge_sample


def knowledge(*contents):
    return {"sources": [{"source": str(i), "content": value} for i, value in enumerate(contents)],
            "reviews": []}


def test_approved_reply_expires_when_business_facts_or_voice_change():
    cfg = {"tone": "Warm"}
    facts = knowledge("Four hours for £750", "Eight hours for £1,200")
    approved = {"version": knowledge_sample.version(cfg, facts), "approved": True}
    cfg["onboarding_state"] = {"knowledge_sample": approved}
    facts["sources"].reverse()
    assert knowledge_sample.current(cfg, facts) == approved
    assert not knowledge_sample.current(dict(cfg, tone="Formal"), facts)
    facts["sources"][0]["content"] = "Eight hours by individual quote"
    assert not knowledge_sample.current(cfg, facts)


def test_mailbox_notes_and_pending_reviews_cannot_qualify():
    facts = knowledge("Private fact")
    facts["sources"][0]["mailbox_id"] = "other-inbox"
    assert not knowledge_sample.can_test(facts)
    facts["sources"].extend(knowledge("Public fact")["sources"])
    assert knowledge_sample.can_test(facts)
    facts["reviews"].append({"state": "pending"})
    assert not knowledge_sample.can_test(facts)


@pytest.fixture
def services(monkeypatch):
    def install(question="", outcome="ok", missing=""):
        response = SimpleNamespace(model="test", usage={}, content=[SimpleNamespace(type="text", text=json.dumps({
            "enquiry": "Do you offer wedding photography?", "question": question}))])
        planner = AsyncMock(return_value=response)
        reader = AsyncMock(return_value=SimpleNamespace(outcome=outcome))
        writer = AsyncMock(return_value=SimpleNamespace(model="test", usage={}, deferred=bool(missing),
            blocked="", missing=missing, text="I offer four hours for £750."))
        for name, value in {
            "agent": SimpleNamespace(draft_reply=writer), "concierge": SimpleNamespace(client=Mock()),
            "knowledge": SimpleNamespace(retrieve=reader, render_for_prompt=Mock(return_value="Approved facts")),
            "llm": SimpleNamespace(create=planner), "usage": SimpleNamespace(record_anthropic=Mock())
        }.items():
            monkeypatch.setattr(app, name, value, raising=False)
        return planner, reader, writer
    return install


def test_one_gap_without_creating_a_reply(services):
    question = "How do you price a half day?"
    _, reader, writer = services(question)
    sample = asyncio.run(knowledge_sample.prepare("tenant", {}, knowledge(), ""))
    assert sample["question"] == question
    assert sample["enquiry"] and not sample["approved"] and not sample["reply"]
    reader.assert_not_awaited()
    writer.assert_not_awaited()


def test_rehearsal_reuses_enquiry_and_drafts_using_approved_context(services):
    planner, reader, writer = services()
    facts = knowledge("Four hours for £750")
    facts["sources"].append({"source":"private", "mailbox_id":"mailbox", "content":"PRIVATE"})
    enquiry = "What does half a day cost?"
    sample = asyncio.run(knowledge_sample.prepare("tenant", {"industry":"Photographer"}, facts, enquiry))
    assert sample["enquiry"] == enquiry and sample["reply"] == "I offer four hours for £750."
    assert not sample["question"] and not sample["approved"] and sample["id"]
    data = json.loads(planner.await_args.kwargs["messages"][0]["content"])
    assert data["notes"] == ["Four hours for £750"] and data["profile"]["industry"] == "Photographer"
    reader.assert_awaited_once_with("tenant", enquiry, whole_if_small=True)
    assert writer.await_args.args[1]["body"] == enquiry
    assert writer.await_args.kwargs == dict(knowledge="Approved facts", tenant_id="tenant",
                                           log_operation="concierge_onboard")


def test_native_draft_deferral_stays_an_open_question(services):
    services(missing="Whether travel is included")
    sample = asyncio.run(knowledge_sample.prepare("tenant", {}, knowledge("We photograph weddings"), ""))
    assert "travel" in sample["question"] and not sample["reply"]


def test_truncated_notes_get_full_retrieval_even_when_planner_sees_gap(services):
    _, reader, writer = services(question="How much is a half day?")
    sample = asyncio.run(knowledge_sample.prepare("tenant", {}, knowledge("A"*2001), ""))
    reader.assert_awaited_once()
    writer.assert_awaited_once()
    assert sample["reply"] and not sample["question"]


def test_retrieval_outage_cannot_produce_approval_candidate(services):
    _, _, writer = services(outcome="failed")
    with pytest.raises(HTTPException, match="could not be checked"):
        asyncio.run(knowledge_sample.prepare("tenant", {}, knowledge("Our prices vary"), ""))
    writer.assert_not_awaited()

def test_owner_can_only_advance_with_current_approved_reply(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import onboarding, onboarding_routes, onboarding_state, setup_handoff
    from app.auth import TenantCtx, require_tenant
    from app.security import require_csrf

    web = FastAPI()
    web.include_router(onboarding.router)
    ctx = TenantCtx(tenant_id="sample-tenant", client=None)
    web.dependency_overrides[require_tenant] = lambda: ctx
    web.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(web, follow_redirects=False)
    facts = knowledge("Four hours for £750")
    cfg = {"agent_name":"Owner", "sign_off":"Thanks", "tone":"Warm"}
    saved_flags = []
    completed = []
    monkeypatch.setattr(onboarding_routes, "require_manager", lambda acting: None)
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(knowledge_sample.knowledge_review, "load", lambda tenant: facts)
    monkeypatch.setattr(onboarding.fairuse, "onboarding_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda tenant, data, **kw: saved_flags.append(data))
    monkeypatch.setattr(onboarding.db, "set_onboarded", lambda tenant, **kw: completed.append(tenant))
    monkeypatch.setattr(setup_handoff, "load_steps", lambda *a, **kw: [])
    monkeypatch.setattr(setup_handoff, "first_open", lambda steps: None)

    async def preview(tenant, cfg, knowledge, enquiry):
        assert tenant == ctx.tenant_id and enquiry == "How much for a half day?"
        return dict(id="preview", enquiry=enquiry, approved=False, question="", reply="Four hours is £750.",
                    version=knowledge_sample.version(cfg, knowledge))
    monkeypatch.setattr(knowledge_sample, "prepare", preview)
    monkeypatch.setattr(onboarding_state, "save", lambda acting, before, after: cfg.update(onboarding_state=after))

    def post(operation, **data):
        response = client.post("/onboarding/knowledge/sample", data=dict(operation=operation, **data))
        assert response.status_code == 303
        return response.headers["location"]

    assert "err=" in post("approve")
    assert "err=" in onboarding._finish(ctx).headers["location"]
    assert post("try", enquiry="How much for a half day?") == "/onboarding/knowledge"
    assert "err=" in post("approve", sample_id="stale")
    assert "err=" in onboarding._finish(ctx).headers["location"]
    facts["reviews"].append({"state":"pending"})
    assert "err=" in post("approve", sample_id="preview")
    facts["reviews"].clear()
    assert post("approve", sample_id="preview") == "/onboarding/knowledge"
    assert not saved_flags and not completed
    assert onboarding._finish(ctx).headers["location"] == "/app/"
    assert completed == [ctx.tenant_id] and saved_flags == [{"draft_hold": True}]
    facts["sources"][0]["content"] = "Four hours for £800"
    assert "err=" in onboarding._finish(ctx).headers["location"]
    assert "err=" in post("approve", sample_id="preview")
