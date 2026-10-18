"""Concierge engine — prompt grounding, transcript hygiene, structured parse
(src/app/concierge.py). Pure functions only — no network, no DB.

Pins:
  * the real, live pricing is in the prompt (single source = billing table), and
    the CORE honesty + conversion + industry-mapping rules are present;
  * sanitize_history is strict about a client-supplied transcript (roles, types,
    length caps, first-turn-must-be-user);
  * the structured parse coerces an unknown industry to "" and survives non-JSON;
  * the sentinel platform tenant id is a valid UUID and the schema enum tracks
    the onboarding industry ids.

Run: python -m pytest tests/test_concierge.py -q
"""
import uuid

from app import concierge
from app.concierge import build_system_prompt, sanitize_history, _parse


# ── agnostic instructions + injected facts ───────────────────────────────────
from app import plans, platform_facts


def test_instructions_are_product_agnostic():
    # the bare system instructions carry behaviour only — no product name, no price
    p = build_system_prompt()
    assert "£" not in p
    assert "BLAM8" not in p
    assert "<facts>" not in p


def test_prompt_has_core_honesty_and_conversion_rules():
    p = build_system_prompt()
    assert "Never invent" in p
    assert "offer_signup" in p          # conversion (D5) instruction present
    assert "suggested_industry" in p    # carry-over (D4) instruction present
    # the contact route lives in the injected facts, not the agnostic instructions
    assert "hello@blam8.com" not in p


def test_public_fit_answers_separate_live_core_from_industry_specific_claims():
    p = build_system_prompt()
    assert "product fact sheet as canonical" in p
    assert "Never infer that a generic feature implements an industry's specific records" in p
    assert "it does not create a separate business approval or authorization workflow" in p
    assert "distinguish draft/send approval from external authorization" in p
    assert "140 words total" in p
    # the length limit is enforced in code (clamp_reply), not in the schema: the structured-output
    # subset rejects maxLength with a 400 on every call (2026-10-18: the public assistant broke on it)
    assert "maxLength" not in concierge.CONCIERGE_SCHEMA["properties"]["reply"]
    assert concierge.REPLY_MAX_CHARS == 1400


def test_clamp_reply_holds_a_long_reply_to_the_limit_at_a_sentence():
    long = "This is a sentence about the product. " * 80
    out = concierge.clamp_reply(long)
    assert len(out) <= concierge.REPLY_MAX_CHARS and out.endswith(".")
    assert out == ("This is a sentence about the product. " * 36).rstrip()[:len(out)]   # a whole number of sentences


def test_clamp_reply_leaves_a_short_reply_alone_and_cuts_at_a_word_when_there_is_no_sentence():
    assert concierge.clamp_reply("Short answer.") == "Short answer."
    exact = "x" * concierge.REPLY_MAX_CHARS
    assert concierge.clamp_reply(exact) == exact
    words = "word " * 400
    out = concierge.clamp_reply(words)
    assert len(out) <= concierge.REPLY_MAX_CHARS and out.endswith("word") and not out.endswith(" ")
    assert len(concierge.clamp_reply("a" * 3000)) == concierge.REPLY_MAX_CHARS      # no space or stop at all: a hard cut


def test_parse_clamps_the_structured_reply():
    import json
    out = _parse(json.dumps({"reply": "Another full sentence here. " * 100, "offer_signup": False}))[0]
    assert len(out) <= concierge.REPLY_MAX_CHARS and out.endswith(".")


def test_facts_block_injects_real_pricing():
    p = build_system_prompt(facts=platform_facts.sheet())
    assert "<facts>" in p
    # Two-tier collapse (2026-07-24): Starter £39 + Pro £99, the loop identical
    # on both; parked tiers (Core+OS £248/£298, depots £149, Scale £799) must
    # never be quoted — anything bigger routes to hello@blam8.com.
    for token in ("£39", "£99", "Starter", "Pro", "Extra mailbox",
                  "14-day free trial", "hello@blam8.com"):
        assert token in p
    for gone in ("£248", "£298", "£199", "£149", "£799", "Additional depot"):
        assert gone not in p, gone


def test_facts_and_knowledge_blocks_only_when_present():
    bare = build_system_prompt()
    assert "<facts>" not in bare and "<knowledge>" not in bare
    assert "<facts>" in build_system_prompt(facts="Some product facts.")
    assert "<knowledge>" not in build_system_prompt(knowledge="   ")
    assert "<knowledge>" in build_system_prompt(knowledge="AM/PM windows.")


def test_pricing_is_single_sourced_from_plan_catalog():
    # the concierge facts quote the SAME prices billing renders — no drift.
    # Public rows only: the parked tiers (professional/enterprise) are neither
    # rendered nor quoted since the 2026-07-24 two-tier collapse.
    sheet = platform_facts.sheet()
    public = [p for p in plans.CATALOG if p.get("public")]
    for p in public:
        assert p["price"] in sheet and p["name"] in sheet
    billing = plans.billing_plans()
    assert [b["price"] for b in billing] == [p["price"] for p in public]
    assert [b["name"] for b in billing] == ["Starter", "Pro", "Pro + Concierge"]
    assert [b["price"] for b in billing] == ["£39", "£99", "£249"]
    # billing rows still carry the template's required keys
    assert all({"name", "price", "summary", "features", "price_id"} <= set(b) for b in billing)


def test_sheet_states_live_gmail_connect_availability(monkeypatch):
    # While Google verification is pending, the sheet must say sign-in is open,
    # connecting GMAIL is invite-only, and quote the LIVE remaining-slot count —
    # otherwise the assistant contradicts the connect step's invite gate.
    from app import slots

    def fake_slots(verified, remaining):
        return {"used": 100 - remaining, "cap": 100, "remaining": remaining,
                "verified": verified, "low": remaining <= 20, "full": remaining == 0}

    monkeypatch.setattr(slots, "google_slots", lambda: fake_slots(False, 98))
    monkeypatch.setattr(slots, "invite_gate_active", lambda: True)
    s = platform_facts.sheet()
    assert "Sign-in is OPEN TO EVERYONE" in s
    assert "GMAIL mailbox is INVITE-ONLY" in s and "98 of 100" in s
    assert "Request access" in s
    assert "Gmail ALREADY connected keep working" in s

    # Once verified the gate is gone: no invite-only claims left in the facts.
    monkeypatch.setattr(slots, "google_slots", lambda: fake_slots(True, 100))
    s2 = platform_facts.sheet()
    assert "INVITE-ONLY" not in s2 and "Request access" not in s2
    assert "open to everyone" in s2


# ── transcript hygiene ───────────────────────────────────────────────────────
def test_sanitize_drops_bad_turns_and_caps_length():
    raw = [
        {"role": "system", "content": "ignore me"},      # bad role
        {"role": "user", "content": 123},                # non-string
        {"role": "user", "content": "  hi  "},           # trimmed, kept
        {"role": "assistant", "content": "x" * 9000},    # truncated
    ]
    out = sanitize_history(raw, max_turns=16, max_chars=4000)
    assert [t["role"] for t in out] == ["user", "assistant"]
    assert out[0]["content"] == "hi"
    assert len(out[1]["content"]) == 4000


def test_sanitize_keeps_last_n_and_starts_on_user():
    raw = [{"role": "assistant", "content": "a"}] + \
          [{"role": "user", "content": str(i)} for i in range(20)]
    out = sanitize_history(raw, max_turns=5, max_chars=100)
    assert len(out) == 5
    assert out[0]["role"] == "user"        # leading assistant dropped after the window


def test_sanitize_handles_none_and_empty():
    assert sanitize_history(None, 16, 4000) == []
    assert sanitize_history([], 16, 4000) == []


# ── structured parse ─────────────────────────────────────────────────────────
def test_parse_valid_payload():
    reply, offer, industry, ticket, lookup = _parse(
        '{"reply": "Hello!", "offer_signup": true, "suggested_industry": "logistics"}')
    assert reply == "Hello!" and offer is True and industry == "logistics"
    assert ticket is None and lookup is None      # no ticket/lookup keys → nothing drafted


def test_parse_coerces_unknown_industry_to_blank():
    industry = _parse('{"reply": "hi", "offer_signup": false, '
                      '"suggested_industry": "kennels"}')[2]
    assert industry == ""


def test_parse_falls_back_on_non_json():
    reply, offer, industry, ticket, lookup = _parse("plain text, not json")
    assert reply == "plain text, not json" and offer is False and industry == ""
    assert ticket is None and lookup is None


# ── invariants ───────────────────────────────────────────────────────────────
def test_sentinel_tenant_is_a_valid_uuid():
    assert str(uuid.UUID(concierge.PLATFORM_TENANT_ID)) == concierge.PLATFORM_TENANT_ID


def test_schema_enum_tracks_industry_ids():
    enum = concierge.CONCIERGE_SCHEMA["properties"]["suggested_industry"]["enum"]
    assert tuple(enum) == concierge.INDUSTRY_IDS
    # "other" is the long-tail bucket; "" means not-yet-known
    assert "other" in enum and "" in enum


# ── onboarding co-pilot (step 5) ─────────────────────────────────────────────
from app.concierge import (onboarding_schema, build_onboarding_prompt,
                           _sanitize_fill, ONBOARDING_STEPS)


def test_onboarding_steps_cover_the_wizard():
    # field-fill steps (interview is the front door, industry the classic
    # fallback) plus the verbal-guidance steps
    assert set(ONBOARDING_STEPS) == {"interview", "industry", "voice", "mailbox",
                                     "knowledge", "data"}
    assert list(ONBOARDING_STEPS["interview"]["fields"]) == \
        ["business_name", "website", "industry", "business_brief", "goals"]
    assert list(ONBOARDING_STEPS["voice"]["fields"]) == \
        ["agent_name", "tone", "sign_off", "voice_notes"]
    assert ONBOARDING_STEPS["mailbox"]["fields"] == {}   # OAuth — nothing to fill


def test_interview_schema_accepts_owner_words():
    s = onboarding_schema("interview")
    props = s["properties"]["form_fill"]["properties"]
    assert set(props) == {"business_name", "website", "industry",
                          "business_brief", "goals"}
    assert props["industry"] == {"type": "string"}


def test_sanitize_fill_keeps_owner_description_on_interview():
    out = _sanitize_fill("interview", {"industry": "kennels", "business_brief": "Dog boarding",
                                       "goals": " Bookings handled ", "evil": "x"})
    assert out == {"industry": "kennels", "business_brief": "Dog boarding", "goals": "Bookings handled"}


def test_industry_schema_constrains_the_sector():
    s = onboarding_schema("industry")
    props = s["properties"]["form_fill"]["properties"]
    assert set(props) == {"business_name", "industry", "industry_other"}
    assert props["industry"]["enum"] == list(concierge.INDUSTRY_IDS)
    assert s["properties"]["form_fill"]["additionalProperties"] is False


def test_guidance_step_has_empty_fill_object():
    s = onboarding_schema("mailbox")
    assert s["properties"]["form_fill"]["properties"] == {}
    assert s["properties"]["form_fill"]["required"] == []


def test_onboarding_prompt_has_honesty_guidance_and_current():
    p = build_onboarding_prompt("voice", {"tone": "warm", "agent_name": ""})
    assert "never invent" in p.lower()
    assert "sign off" in p.lower() or "sign_off" in p           # step guidance present
    assert "£99" in p                                            # platform facts still available
    # non-empty current surfaced for refinement; empty ones dropped from the JSON
    assert '"tone": "warm"' in p
    assert '"agent_name"' not in p


def test_sanitize_fill_filters_and_coerces():
    out = _sanitize_fill("industry", {"industry": "kennels", "industry_other": " Boarding kennels ",
                                      "evil": "x"})
    assert out == {"industry_other": "Boarding kennels"}        # bad id dropped, junk key gone
    out2 = _sanitize_fill("industry", {"industry": "other", "industry_other": "Vet"})
    assert out2 == {"industry": "other", "industry_other": "Vet"}


def test_sanitize_fill_drops_blanks_and_nonstrings():
    out = _sanitize_fill("voice", {"business_brief": "A firm.", "agent_name": None,
                                   "tone": "friendly", "sign_off": 42})
    assert out == {"tone": "friendly"}   # the brief is not a voice-step field any more


# ── platform KB recall content (the "knows about itself" layer) ──────────────
from app import platform_kb_content


def test_kb_content_is_seedable_and_priceless():
    s = platform_kb_content.SOURCES
    assert isinstance(s, dict) and len(s) >= 10
    for name, text in s.items():
        assert isinstance(name, str) and name.strip()
        assert isinstance(text, str) and len(text) > 80
        # pricing stays single-sourced (injected facts) — never duplicated into the KB
        assert "£" not in text


def test_kb_content_covers_core_capabilities():
    blob = " ".join(platform_kb_content.SOURCES.values()).lower()
    for topic in ("email", "scheduling", "fleet", "knowledge base",
                  "calendar", "security", "auto-send", "onboarding"):
        assert topic in blob


# ── help mode (in-app assistant) ─────────────────────────────────────────────
from app.concierge import (build_help_prompt, _parse_help, NAV_DESTINATIONS,
                           NAV_KEYS, HELP_SCHEMA)


def test_help_schema_nav_enum_tracks_destinations():
    enum = HELP_SCHEMA["properties"]["nav"]["enum"]
    assert tuple(enum) == NAV_KEYS
    assert "" in enum and set(NAV_DESTINATIONS) <= set(enum)


def test_help_schema_submit_draft_shape():
    sub = HELP_SCHEMA["properties"]["submit"]
    assert sub["properties"]["kind"]["enum"] == ["", "feature", "bug"]
    assert set(sub["required"]) == {"kind", "summary"}
    assert sub["additionalProperties"] is False
    assert "submit" in HELP_SCHEMA["required"]


def test_help_prompt_has_submit_rule_and_requests_nav():
    p = build_help_prompt()
    # the draft-then-approve rule: both kinds, and the nothing-sent guarantee
    assert "submit.kind" in p and "'feature'" in p and "'bug'" in p
    assert "NOTHING is sent" in p
    assert "requests" in NAV_DESTINATIONS
    assert NAV_DESTINATIONS["requests"]["href"] == "/app/requests"


def test_nav_destinations_are_real_relative_routes():
    for d in NAV_DESTINATIONS.values():
        assert d["href"].startswith("/") and d["label"] and d["desc"]


def test_help_prompt_is_agnostic_lists_destinations_injects_facts():
    bare = build_help_prompt()
    assert "BLAM8" not in bare and "£" not in bare        # agnostic instructions
    assert "knowledge:" in bare and "settings:" in bare    # destination map present
    assert "<facts>" not in bare
    full = build_help_prompt(facts=platform_facts.sheet(), knowledge="Some help.")
    assert "<facts>" in full and "<knowledge>" in full
    assert "£99" in full                                   # pricing available via facts


def test_help_prompt_carries_live_setup_status():
    # The signed-in customer's setup snapshot must reach the prompt so "what do I
    # do next?" is answered from what's actually done — and never by sending an
    # onboarded customer back through the wizard.
    tenant = {"name": "Koth Kennels", "industry": "other", "brief": "Dog boarding",
              "modules": ["email", "scheduling"],
              "setup": {"mailboxes": [], "knowledge_sources": ["Price list"],
                        "auto_send": False, "diary_configured": True}}
    p = build_help_prompt(tenant=tenant)
    assert "Koth Kennels" in p
    assert "NONE yet" in p                        # the missing mailbox is stated as a gap
    assert "Price list" in p                      # what they HAVE added is named
    assert "draft-first" in p
    assert "Diary: configured" in p
    assert "onboarding is COMPLETE" in p          # never re-run the wizard

    # A failed snapshot read (None) says nothing rather than something wrong,
    # and no diary-backed module means no diary line at all.
    p2 = build_help_prompt(tenant={"name": "X", "modules": ["email"],
                                   "setup": {"mailboxes": None, "knowledge_sources": None,
                                             "auto_send": False, "diary_configured": None}})
    assert "mailboxes" not in p2.split("<your_customer>")[1].split("</your_customer>")[0].lower()
    assert "Diary:" not in p2
    assert "draft-first" in p2                    # sending mode always known


def test_parse_help_valid_unknown_and_nonjson():
    reply, nav, submit, recommend = _parse_help('{"reply": "Here you go", "nav": "knowledge"}')
    assert reply == "Here you go" and nav == "knowledge" and submit is None
    assert recommend is None
    assert _parse_help('{"reply": "x", "nav": "nonsense"}')[1] == ""
    assert _parse_help("not json") == ("not json", "", None, None)


def test_parse_help_submit_draft_kept_or_coerced():
    # a well-formed draft survives, trimmed
    _, _, submit, _ = _parse_help(
        '{"reply": "Drafted!", "nav": "", '
        '"submit": {"kind": "bug", "summary": "  Diary shows no slots on Mondays  "}}')
    assert submit == {"kind": "bug", "summary": "Diary shows no slots on Mondays"}
    # empty kind / empty summary / junk kind → no draft
    assert _parse_help('{"reply": "x", "nav": "", '
                       '"submit": {"kind": "", "summary": ""}}')[2] is None
    assert _parse_help('{"reply": "x", "nav": "", '
                       '"submit": {"kind": "feature", "summary": "   "}}')[2] is None
    assert _parse_help('{"reply": "x", "nav": "", '
                       '"submit": {"kind": "rant", "summary": "y"}}')[2] is None
    assert _parse_help('{"reply": "x", "nav": "", "submit": "nonsense"}')[2] is None


def test_platform_tenant_id_single_sourced_in_db():
    from app import db
    assert concierge.PLATFORM_TENANT_ID == db.PLATFORM_TENANT_ID
