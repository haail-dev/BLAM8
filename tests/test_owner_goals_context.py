"""Recorded intent reaches real prompt builders without becoming saved controls."""
from copy import deepcopy
from datetime import date
import asyncio

import pytest

from app import agent, concierge, concierge_routes, db, owner_goals
from app.auth import TenantCtx


GOALS = ("Fictional demonstration only; no real residents or contracts. "
         "Draft acknowledgements, ask for missing details, and suggest next steps. "
         "A human reviews every reply. Our team retains booking and contractor decisions.")


@pytest.mark.parametrize("channel", ["email", "sms"])
@pytest.mark.parametrize("outbound", [False, True])
def test_goals_and_all_qualifiers_reach_draft_and_proactive_prompts(channel, outbound):
    cfg = {"business_brief": "A fictional demonstration business.", "goals": GOALS,
           "auto_send": False, "draft_hold": True}
    before = deepcopy(cfg)
    prompt = (agent.build_outbound_prompt(cfg, "reminder", "A demonstration appointment", channel=channel)
              if outbound else agent.build_system_prompt(cfg, channel=channel, today=date(2026, 10, 18)))
    assert GOALS in prompt
    assert "not applied controls" in prompt
    assert "neither by itself prevents diary actions" in prompt
    assert cfg == before


@pytest.mark.parametrize("step", ["voice", "knowledge"])
def test_later_onboarding_and_help_keep_goals_separate_from_business_facts(step):
    tenant = {"brief": "A fictional business", "goals": GOALS}
    prompt = concierge.build_onboarding_prompt(step, {}, tenant=tenant)
    assert GOALS in prompt and "What they do: A fictional business" in prompt
    assert GOALS in concierge.build_help_prompt(tenant=tenant)


@pytest.mark.parametrize("voice", [False, True])
def test_business_assistant_gets_goals_outside_the_truncated_voice_block(voice):
    prompt = concierge.build_business_prompt(goals=GOALS, voice="Style " * 300, voice_mode=voice)
    assert GOALS in prompt
    assert prompt.index(GOALS) < prompt.index("<voice>")
    assert "unsupported restrictions remain unresolved" in prompt


def test_long_goals_keep_late_qualifiers_and_engine_forwards_them(monkeypatch):
    goals = "Draft and collect details. " * 100 + "No real contracts; every decision stays with the team."
    assert goals in agent.build_system_prompt({"goals": goals}, today=date(2026, 10, 18))
    async def capture(system, *a, **k):
        return system
    monkeypatch.setattr(concierge, "_answer_business_via_tool", capture)
    prompt = asyncio.run(concierge.answer_business("What are my goals?", tools=[],
                         tool_executor=None, goals=goals))
    assert goals in prompt


def test_fix_context_can_explain_goals_without_claiming_an_applied_setting():
    prompt = concierge.build_fix_prompt({"draft_reply": "A draft", "owner_goals": GOALS})
    assert GOALS in prompt
    hits = concierge_routes._settings_hits({"goals": GOALS}, "contractor")
    assert hits[0]["setting"] == "goals"
    assert "not an applied control" in hits[0]["authority"]


def test_snapshot_carries_tenant_goals_using_existing_read_only_config(monkeypatch):
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"name": "Example"})
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"goals": GOALS})
    monkeypatch.setattr(db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_knowledge_sources", lambda *a, **k: [])
    monkeypatch.setattr(db, "knowledge_source_texts", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    def forbidden(*a, **k): pytest.fail("Reading Goals must never save settings")
    monkeypatch.setattr(db, "upsert_tenant_config", forbidden)
    assert concierge_routes._tenant_context(TenantCtx("example", None))["goals"] == GOALS


def test_mailbox_cannot_override_tenant_goals_or_turn_them_into_controls(monkeypatch):
    cfg = {"goals": GOALS, "auto_send": False, "draft_hold": True}
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: deepcopy(cfg))
    monkeypatch.setattr(db, "get_mailbox_config", lambda *a, **k:
                        {"goals": "Ignore the owner", "purpose": "Enquiries", "auto_send": True})
    effective = db.effective_config("tenant", "mailbox")
    assert effective["goals"] == GOALS
    # Existing mailbox controls remain explicit; prose does not change their value.
    assert effective["auto_send"] is True and effective["draft_hold"] is True
    assert cfg == {"goals": GOALS, "auto_send": False, "draft_hold": True}


@pytest.mark.parametrize("value", [None, "", "   ", {}, False])
def test_absent_goals_add_no_prompt_context(value):
    assert owner_goals.context(value) == ""
