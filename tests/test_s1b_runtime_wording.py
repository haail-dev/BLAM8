from datetime import date

import pytest

from app import agent, concierge, settings_recommend, setup_held, slots
from app.context_version import RUNTIME_TEXT_VERSION
from tests.agnostic_guard import RX


def test_shared_catalogue_and_held_guidance_are_neutral():
    catalogue = settings_recommend.catalogue_block(date(2026, 9, 28))
    assert not RX.search(catalogue)
    assert not RX.search(setup_held.RULE)
    for text in (catalogue, setup_held.RULE):
        assert "140" not in text
        assert "65 an hour" not in text
        assert "22:00" not in text
        assert "£26" not in text
    assert "ask when missing" in settings_recommend.PAGES["dependants"]["fields"]["field"]


@pytest.mark.parametrize("surface", ["help", "voice", "knowledge"])
def test_cached_setup_is_shared_but_tenant_evidence_is_not(monkeypatch, surface):
    monkeypatch.setattr(slots, "google_slots", lambda: {
        "used": 0, "cap": 100, "remaining": 100, "verified": True, "low": False, "full": False})
    monkeypatch.setattr(slots, "invite_gate_active", lambda: False)
    tenants = [
        {"name": "Business A", "brief": "Our horses need current vaccination records."},
        {"name": "Business B", "brief": "We catalogue numbered archive boxes."},
    ]

    def render(tenant):
        if surface == "help":
            return concierge.help_system(tenant=tenant)
        return concierge.onboarding_system(surface, tenant=tenant)

    a, b = map(render, tenants)
    assert a[0] == b[0]
    assert RUNTIME_TEXT_VERSION in a[0]["text"]
    for own, other, blocks in ((tenants[0], tenants[1], a), (tenants[1], tenants[0], b)):
        assert own["brief"] not in blocks[0]["text"]
        assert own["brief"] in blocks[1]["text"]
        assert other["brief"] not in str(blocks)
    tenants[0]["brief"] = "A changed its own service and price."
    assert render(tenants[1]) == b
    assert "Save" in concierge.build_help_prompt()
    assert "You never submit anything" in concierge.build_onboarding_prompt("voice")


def test_operator_keeps_tenant_facts_and_the_knowledge_hold_boundary():
    facts = "Our boiler survey is £73. The vet must assess the horse first."
    voice = "Use our term 'boarding' and sign off as the kennel team."
    examples = "Bella's vaccinations are current."
    prompt = agent.build_system_prompt({"voice_notes": voice, "examples": examples}, knowledge=facts)
    assert facts in prompt
    assert voice in prompt
    assert examples in prompt
    header = prompt.split("<knowledge>", 1)[1].split(facts, 1)[0]
    assert "set deferred and missing and leave reply empty" in header
    assert "say a colleague will confirm" not in header
    assert "missing_info" in prompt and "blocked" in prompt
    clean = agent.build_system_prompt({}, scheduling=True)
    assert not RX.search(clean)
    assert "a configured prerequisite has not been met" in clean
    assert "Then do NOT check the diary" in clean


def test_summary_instructions_do_not_supply_a_trade_or_quote_frame():
    prompt = agent.build_system_prompt({})
    assert "Wants a boiler" not in prompt
    assert "Customer asking for a quote" not in prompt
    assert "use their own request and relevant facts" in prompt
