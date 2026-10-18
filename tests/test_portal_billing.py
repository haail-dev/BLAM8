"""Portal cost attribution by verification state (2026-07-26 directive).

Anyone can chat on blam8.com/c/{slug}; the business must only pay for turns
where a VERIFIED customer actually engaged with the business. Pins:
  * billing_operation(): the three-way who-pays rule
  * /api/portal/chat records unverified turns against the platform sentinel
    tenant (operation portal_chat_unverified)
  * a verified turn that touched no business tool bills the platform
    (portal_chat_general) — including the pure verification turn itself
  * a verified turn that used a business tool bills the business (portal_chat)
  * the per-call tenant resolver flips platform → business the moment a tool
    verifies the visitor mid-turn (llm_calls transcript attribution)

Run: python -m pytest tests/test_portal_billing.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

import app.portal as portal
import app.portal_chat as portal_chat
import app.portal_data as portal_data
from app import db
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


_CONTACT = {"id": "c1", "name": "Sarah", "email": "s@x.com",
            "reference_code": "ACME-7K3M"}


def _env(monkeypatch):
    monkeypatch.setattr(portal.db, "get_tenant_by_portal_slug",
                        lambda slug: {"id": "t1", "name": "Acme Plumbing",
                                      "portal_slug": "acme-plumbing"}
                        if slug == "acme-plumbing" else None)
    monkeypatch.setattr(portal.db, "get_tenant_config",
                        lambda *a, **k: {"portal_enabled": True})
    monkeypatch.setattr(portal.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(portal.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda *a, **k: None)
    recorded = []
    monkeypatch.setattr(portal.usage, "record_anthropic",
                        lambda tenant_id, model, totals, **kw:
                        recorded.append({"tenant_id": tenant_id, **kw}))
    return recorded


def _chat(client, monkeypatch, fake_answer, ref=""):
    monkeypatch.setattr(portal.portal_chat, "answer_portal", fake_answer)
    body = {"slug": "acme-plumbing", "question": "hello"}
    if ref:
        body["ref"] = ref
    res = client.post("/api/portal/chat", json=body)
    assert res.status_code == 200
    done = [json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert done["type"] == "done"
    return done


# ── the rule itself ───────────────────────────────────────────────────────────
def test_billing_operation_three_way_split():
    bo = portal_chat.billing_operation
    # anonymous visitors are ALWAYS the platform's cost — even when the model
    # searched the business KB to answer "what services do you offer?"
    assert bo(False, []) == "portal_chat_unverified"
    assert bo(False, ["search_knowledge_base"]) == "portal_chat_unverified"
    # verified + business data touched -> the business pays
    assert bo(True, ["list_my_bookings"]) == "portal_chat"
    assert bo(True, ["lookup_reference", "check_availability"]) == "portal_chat"
    # verified but a pure platform/general turn -> platform pays
    assert bo(True, []) == "portal_chat_general"
    # …and the pure verification turn: lookup_reference/find_contact are
    # BLAM8's security gate, not a service to the business
    assert bo(True, ["lookup_reference"]) == "portal_chat_general"
    assert bo(True, ["find_contact"]) == "portal_chat_general"


def test_business_tools_catalogue_stays_in_step():
    """Every portal tool is deliberately classified: business engagement or
    platform verification. A new tool added to the catalogue must be placed
    HERE, not silently default to platform-paid."""
    verification = {"lookup_reference", "find_contact"}
    assert portal_data.TOOL_NAMES == portal_chat.BUSINESS_TOOLS | verification


# ── endpoint attribution ──────────────────────────────────────────────────────
def test_unverified_turn_never_reaches_the_model(client, monkeypatch):
    """Invite-only (Kris 2026-09-24): an anonymous visitor gets the invite
    gate for free — no model call, no spend to attribute to anyone."""
    recorded = _env(monkeypatch)
    ran = []

    async def _fake(question, history, **kw):
        ran.append(1)
        return "We fix boilers!", {"input_tokens": 5, "output_tokens": 5}, "m"
    done = _chat(client, monkeypatch, _fake)
    assert ran == [] and recorded == []
    assert "This page is for Acme Plumbing's customers" in done["reply"]
    assert done["customer"] is None


def test_verified_general_turn_bills_the_platform(client, monkeypatch):
    recorded = _env(monkeypatch)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: dict(_CONTACT)
                        if code == "ACME-7K3M" else None)

    async def _fake(question, history, **kw):   # "what is BLAM8?" — no tools
        return "BLAM8 is…", {"input_tokens": 5, "output_tokens": 5}, "m"
    _chat(client, monkeypatch, _fake, ref="ACME-7K3M")
    assert recorded[0]["tenant_id"] == db.PLATFORM_TENANT_ID
    assert recorded[0]["operation"] == "portal_chat_general"


def test_verified_business_turn_bills_the_business(client, monkeypatch):
    recorded = _env(monkeypatch)
    monkeypatch.setattr(portal.db, "crm_contact_by_reference",
                        lambda tid, code: dict(_CONTACT)
                        if code == "ACME-7K3M" else None)

    async def _fake(question, history, **kw):
        await kw["on_tool"]("list_my_bookings", {})   # the loop reports tools here
        return "Friday 10am.", {"input_tokens": 5, "output_tokens": 5}, "m"
    _chat(client, monkeypatch, _fake, ref="ACME-7K3M")
    assert recorded[0]["tenant_id"] == "t1"
    assert recorded[0]["operation"] == "portal_chat"


def test_typed_code_verifies_without_the_model(client, monkeypatch):
    """The turn where the visitor types their code is settled by the gate:
    verified, masked, and nothing billed (verification is platform work and
    now costs no tokens at all)."""
    recorded = _env(monkeypatch)
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda tid, code: dict(_CONTACT)
                        if code == "ACME-7K3M" else None)
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda *a, **k: [])
    ran = []

    async def _fake(question, history, **kw):
        ran.append(1)
        return "should not run", {"input_tokens": 5, "output_tokens": 5}, "m"
    monkeypatch.setattr(portal.portal_chat, "answer_portal", _fake)
    res = client.post("/api/portal/chat",
                      json={"slug": "acme-plumbing", "question": "ACME-7K3M"})
    done = [json.loads(l) for l in res.text.strip().splitlines()][-1]
    assert ran == [] and recorded == []
    assert done["customer"]["reference_masked"] == "ACME-7K**"
    assert "ACME-7K3M" not in done["reply"]


def test_engine_resolves_callable_tenant_per_call(monkeypatch):
    """answer_portal re-resolves a callable tenant_id before EVERY llm.create
    in the loop — the seam the mid-turn flip rides on."""
    import asyncio

    from app import llm

    tenants_logged = []

    class _Use:
        input_tokens = output_tokens = 1
        cache_read_input_tokens = cache_creation_input_tokens = 0

    class _Text:
        type, text = "text", json.dumps({"reply": "hi"})

    class _Msg:
        stop_reason, usage, model, content = "end_turn", _Use(), "m", [_Text()]

    async def _fake_create(*, client, tenant_id=None, **kw):
        tenants_logged.append(tenant_id)
        return _Msg()

    monkeypatch.setattr(llm, "create", _fake_create)
    monkeypatch.setattr(portal_chat, "_client", object())   # never build a real SDK client
    state = {"contact": None}

    async def _exec(name, args):
        return {}

    reply, totals, model = asyncio.run(portal_chat.answer_portal(
        "hi", [], system="s", tools=[], tool_executor=_exec,
        tenant_id=lambda: "t1" if state["contact"] else db.PLATFORM_TENANT_ID))
    assert reply == "hi"
    assert tenants_logged == [db.PLATFORM_TENANT_ID]
    state["contact"] = {"id": "c1"}
    asyncio.run(portal_chat.answer_portal(
        "hi again", [], system="s", tools=[], tool_executor=_exec,
        tenant_id=lambda: "t1" if state["contact"] else db.PLATFORM_TENANT_ID))
    assert tenants_logged == [db.PLATFORM_TENANT_ID, "t1"]
