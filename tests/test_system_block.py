"""The system-constraint hold (rule 5b — Julius, live e2e 2026-09-02).

The model worked out that a DIARY SETTING made the request impossible
(checkout_before 09:30 refused every daycare collection) — and the product
made it write prose anyway, which leaked its deliberation into the draft box.
Now it's the third hold family: gap (missing knowledge) → hot (sentiment) →
BLOCKED (a setting stands in the way). No draft is written or stored; the
thread queues needs_attention with the constraint named and where to fix it.

Also pins the ⓘ working capture: the loop's text turns + tool calls land in
email_log.draft_reasoning so "why did it write this?" never needs a dig.

Run: python -m pytest tests/test_system_block.py -q
"""
import asyncio

import pytest

from app import agent, crm, db, pipeline, usage
from app import knowledge as kb
from app.agent import DRAFT_SCHEMA, build_system_prompt

EMAIL = {"from_address": "kris@x.co", "subject": "Booking Bella in",
         "body": "Can I book daycare Monday?"}


# ── the envelope + prompt carry the signal ───────────────────────────────────
def test_schema_carries_blocked_and_requires_it():
    assert "blocked" in DRAFT_SCHEMA["properties"]
    assert "blocked" in DRAFT_SCHEMA["required"]


def test_prompt_teaches_the_blocked_hold():
    p = build_system_prompt({})
    assert "`blocked` is the same hold for a SYSTEM constraint" in p
    assert "leave `reply` completely empty" in p
    assert "Never use it to think" in p


def test_parse_draft_surfaces_blocked():
    import json
    out = agent._parse_draft(json.dumps({
        "reply": "", "deferred": False, "missing": "",
        "blocked": "the diary refuses same-day departures: check-out is limited to 09:30"}))
    assert out["blocked"].startswith("the diary refuses")
    assert agent._parse_draft("{}")["blocked"] == ""


# ── the pipeline holds, names the constraint, stores the working ─────────────
def _env(monkeypatch, result):
    captured = {}

    async def fake_draft(cfg, email, **kw):
        return result

    async def fake_retrieve(*a, **k):
        return []
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(kb, "retrieve", fake_retrieve)
    monkeypatch.setattr(kb, "render_for_prompt", lambda *a, **k: None)
    for name, value in (("is_ignored", False), ("mailbox_self_addresses", set()),
                        ("own_mailbox_addresses", set()),
                        ("bookings_for_contact", []), ("get_scheduling_config", None),
                        ("get_tenant", {"industry": "kennels"}),
                        ("effective_config", {}), ("get_conversation", []),
                        ("active_modules", set()), ("bookings_for_log", [])):
        monkeypatch.setattr(db, name, lambda *a, _v=value, **k: _v)

    def fake_insert(tenant_id, from_address, subject, body_in, draft, **kw):
        captured.update(kw, draft=draft)
        return "log-1", True
    monkeypatch.setattr(db, "insert_email_log", fake_insert)
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(crm, "ingest_email_log_row",
                        lambda *a, **k: {"contact_id": None, "company_id": None})
    return captured


def test_blocked_queues_with_no_draft_and_the_constraint_named(monkeypatch):
    captured = _env(monkeypatch, agent.DraftResult(
        text="", usage={"input_tokens": 1}, model="m",
        blocked="the diary refuses same-day departures: check-out is limited to 09:30",
        reasoning="→ check_availability(...)\n← {\"error\": \"checkout_time\"}"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert captured["draft"] == ""
    assert captured["thread_state"] == "needs_attention"
    assert captured["category"] == "needs_reply"
    assert "A setting blocks this request" in captured["category_reason"]
    assert "check-out is limited to 09:30" in captured["category_reason"]
    assert "Diary settings" in captured["category_reason"]
    assert captured["action_summary"] == "No draft written — a setting blocks this request"
    # The working rode along — the ⓘ shows exactly what the diary refused.
    assert "checkout_time" in captured["draft_reasoning"]


def test_blocked_discards_a_half_written_reply(monkeypatch):
    # Rule 5b says write nothing; if the model half-complies, the pipeline
    # enforces it — a reply written around a wall promises what the system
    # won't do (the live case stored leaked deliberation).
    captured = _env(monkeypatch, agent.DraftResult(
        text="I'll get that booked for you!", usage={"input_tokens": 1}, model="m",
        blocked="min_nights refuses same-day stays"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m2"))
    assert captured["draft"] == ""
    assert captured["thread_state"] == "needs_attention"


def test_unblocked_drafts_store_their_working(monkeypatch):
    captured = _env(monkeypatch, agent.DraftResult(
        text="Hi Kris — Monday works!", usage={"input_tokens": 1}, model="m",
        reasoning="Checked availability for Monday.\n→ check_availability(...)\n← {\"available\": true}"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m3"))
    assert captured["draft"].startswith("Hi Kris")
    assert captured["thread_state"] != "needs_attention"
    assert "check_availability" in captured["draft_reasoning"]

# ── the knowledge hold enforces rule 5 the same way ─────────────────────────
def test_a_knowledge_hold_discards_a_half_written_reply(monkeypatch):
    # Rule 5 says: when you defer a business fact, leave the reply empty. Live
    # 2026-09-30 (Hartwell rig) the model deferred "whether the business installs
    # wet underfloor heating, and pricing" and still wrote a reply that invented
    # why it could not be priced and implied the work is done. The pipeline now
    # enforces the rule, exactly as it does for a system block.
    captured = _env(monkeypatch, agent.DraftResult(
        text="Wet underfloor heating isn't something I can price here: it depends on the floor build-up.",
        usage={"input_tokens": 1}, model="m", deferred=True,
        missing="whether the business installs wet underfloor heating, and pricing for it"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m4"))
    assert captured["draft"] == ""
    assert captured["thread_state"] == "needs_attention"
    assert captured["action_summary"] == "No draft written — missing knowledge"


def test_a_reply_that_defers_nothing_is_kept(monkeypatch):
    captured = _env(monkeypatch, agent.DraftResult(
        text="Hi Kris — Monday works!", usage={"input_tokens": 1}, model="m", deferred=False, missing=""))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m5"))
    assert captured["draft"].startswith("Hi Kris")
