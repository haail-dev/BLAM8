"""A tool call written out as text is never a reply (live 2026-09-29, typed
Concierge): the model answered a lookup round with `<invoke name="list_bookings">
<parameter …>` as TEXT, stop_reason "tool_use" but no tool_use block, and the
typed loop showed it to the owner as the reply. One corrective retry, else an
honest failure — in the typed loop, the voice loop, and customer drafts.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import agent, concierge, llm, model_markup
from app.config import settings

# The live text, verbatim in shape.
LIVE = ('<invoke name="list_bookings">\n<parameter name="contact">jess@example.com'
        '</parameter>\n<parameter name="date_from">2026-10-01</parameter>\n'
        '<parameter name="date_to">2026-10-01</parameter>')


class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _text(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, usage=_Usage(), model="test")


def _tool(name, inp, tid="tu1"):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name,
                                                    input=inp, id=tid)],
                           stop_reason="tool_use", usage=_Usage(), model="test")


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(llm.db, "insert_llm_call", lambda row: None)
    monkeypatch.setattr(settings, "llm_prompt_cache", False)
    monkeypatch.setattr(concierge, "client", lambda: object())
    monkeypatch.setattr(agent, "client", lambda: object())
    calls = []

    def load(replies):
        replies = list(replies)

        async def fake_create(**kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            return replies.pop(0)
        monkeypatch.setattr(llm, "create", fake_create)
        return calls
    return load


def _run(voice_mode):
    async def executor(name, args):
        return {"bookings": []}
    return asyncio.run(concierge.answer_business(
        "Draft a reminder to Jess about BK-1043.", [],
        tools=[{"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=voice_mode))


# ── the detector ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    LIVE,
    '<parameter name="x">1</parameter>',
    "Here you go </parameter>",
    "<function_calls><invoke name=\"a\"></invoke></function_calls>",
    "<invoke name=\"a\">",
    "<tool_use>{}</tool_use>",
])
def test_markup_is_detected(text):
    assert model_markup.has_tool_markup(text)


@pytest.mark.parametrize("text", [
    "", None, "Jess's visit is on 1 October at 9am.",
    "Use a < b and c > d.", "Invoke the clause in the parameter list.",
])
def test_ordinary_text_is_not_markup(text):
    assert not model_markup.has_tool_markup(text)


def test_markup_is_found_inside_nested_action_fields():
    assert model_markup.any_tool_markup({"a": [{"b": LIVE}]})
    assert not model_markup.any_tool_markup({"a": [{"b": "fine"}], "c": None})


# ── typed loop: the incident, replayed ───────────────────────────────────────
def test_typed_markup_turn_is_sent_back_and_the_owner_never_sees_it(scripted):
    calls = scripted([
        _text(LIVE, stop="tool_use"),                       # 18:59:02 UTC, as logged
        _tool("list_bookings", {"contact": "jess@example.com"}, tid="l1"),
        _tool("answer", {"reply": "Here's the reminder for Jess."}, tid="a1"),
    ])
    out = _run(voice_mode=False)
    assert out.reply == "Here's the reminder for Jess."
    assert model_markup.CORRECTION in json.dumps(calls[1]["messages"][-1]["content"])


def test_typed_markup_twice_is_an_honest_failure(scripted):
    scripted([_text(LIVE, stop="tool_use")] * 3)   # sent back, then the forced answer
    out = _run(voice_mode=False)
    assert out.reply == model_markup.HONEST_FAILURE
    assert out.links == [] and out.action is None


def test_typed_markup_inside_the_answer_tool_is_caught(scripted):
    calls = scripted([
        _tool("answer", {"reply": "Sure. " + LIVE}, tid="a1"),
        _tool("answer", {"reply": "Done — here is the reminder."}, tid="a2"),
    ])
    out = _run(voice_mode=False)
    assert out.reply == "Done — here is the reminder."
    assert calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "a1"


def test_typed_markup_in_the_answer_tool_twice_is_an_honest_failure(scripted):
    scripted([_tool("answer", {"reply": LIVE}, tid="a1"),
              _tool("answer", {"reply": LIVE}, tid="a2")])
    out = _run(voice_mode=False)
    assert out.reply == model_markup.HONEST_FAILURE


def test_typed_markup_in_an_action_field_never_stages(scripted):
    scripted([_tool("answer", {"reply": "Ok.", "action": {"kind": "update_contact", "content": LIVE}}, "a1"),
              _tool("answer", {"reply": "Ok.", "action": {"kind": "update_contact", "content": LIVE}}, "a2")])
    out = _run(voice_mode=False)
    assert out.reply == model_markup.HONEST_FAILURE and out.action is None


# ── voice loop ───────────────────────────────────────────────────────────────
def _json(reply):
    return json.dumps({"reply": reply, "links": [], "action": {"kind": ""}})


def test_voice_markup_turn_is_sent_back_once(scripted):
    calls = scripted([
        _text(LIVE, stop="tool_use"),
        _text(_json("Here's the reminder for Jess.")),
    ])
    out = _run(voice_mode=True)
    assert out.reply == "Here's the reminder for Jess."
    assert calls[1]["messages"][-1]["content"] == model_markup.CORRECTION


def test_voice_markup_twice_is_an_honest_failure(scripted):
    scripted([_text(LIVE, stop="tool_use"), _text(LIVE, stop="end_turn")])
    out = _run(voice_mode=True)
    assert out.reply == model_markup.HONEST_FAILURE


# ── customer drafts ──────────────────────────────────────────────────────────
def test_a_draft_reply_that_is_markup_is_empty_not_a_draft():
    parsed = agent._parse_draft(json.dumps({"reply": LIVE, "thread_state": "in_progress"}))
    assert parsed["reply"] == ""
    assert agent._parse_draft(json.dumps({"reply": "Hi Jess, all set."}))["reply"] == "Hi Jess, all set."


def test_an_outbound_message_that_is_markup_is_retried_once(scripted, monkeypatch):
    monkeypatch.setattr(agent, "build_outbound_prompt", lambda *a, **k: "system")
    calls = scripted([_text(json.dumps({"message": LIVE})),
                      _text(json.dumps({"message": "Hi Jess, see you on the 1st."}))])
    result = asyncio.run(agent.compose_outbound({}, "reminder", "facts"))
    assert result.text == "Hi Jess, see you on the 1st."
    assert len(calls) == 2


def test_an_outbound_message_that_stays_markup_is_empty(scripted, monkeypatch):
    monkeypatch.setattr(agent, "build_outbound_prompt", lambda *a, **k: "system")
    scripted([_text(json.dumps({"message": LIVE})), _text(LIVE)])
    result = asyncio.run(agent.compose_outbound({}, "reminder", "facts"))
    assert result.text == ""


# ── honest failure must not deny earlier effects ─────────────────────────────
def test_mutating_tool_then_markup_failure_does_not_deny_prior_effect(scripted):
    """Astra review finding #1: if a mutating tool succeeded earlier in the turn,
    the fallback must not say 'Nothing was sent or changed'. The tool receipt
    proves the mutation happened; the fallback covers only the final reply."""
    mutated = []

    async def executor(name, args):
        if name == "update_contact":
            mutated.append(args)
            return {"ok": True}
        return {"bookings": []}

    calls = scripted([
        _tool("update_contact", {"id": "c1", "notes": "Called back"}, tid="u1"),
        _tool("answer", {"reply": LIVE}, tid="a1"),
        _tool("answer", {"reply": LIVE}, tid="a2"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Update contact notes and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}},
               {"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=False))
    # The mutating tool DID execute:
    assert len(mutated) == 1
    # The reply must not claim "Nothing was sent or changed" because the tool ran:
    assert "Nothing was sent or changed" not in out.reply
    # It should still be a failure message (action is None, links empty):
    assert out.action is None
    assert out.links == []


# ── voice: successful effect then malformed reply (Astra finding #1) ─────────
def test_voice_mutating_tool_then_markup_failure_does_not_deny_prior_effect(scripted):
    """Voice-mode equivalent: mutating tool succeeds, then the final answer is
    malformed markup. Must not say 'Nothing was sent or changed'."""
    mutated = []

    async def executor(name, args):
        if name == "update_contact":
            mutated.append(args)
            return {"ok": True}
        return {"bookings": []}

    scripted([
        _tool("update_contact", {"id": "c1", "notes": "Called back"}, tid="u1"),
        _text(LIVE, stop="end_turn"),       # malformed final answer
        _text(LIVE, stop="end_turn"),       # retry also malformed
    ])
    out = asyncio.run(concierge.answer_business(
        "Update contact notes and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}},
               {"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=True))
    assert len(mutated) == 1
    assert "Nothing was sent or changed" not in out.reply
    assert out.action is None


# ── typed: tool effect + exception + malformed reply (Astra finding #3) ──────
def test_typed_tool_effect_then_exception_then_malformed_reply(scripted):
    """A tool records a side effect then raises (e.g. timeout after commit).
    tools_ran must already be True, and the failure must not deny effects."""
    effected = []

    async def executor(name, args):
        if name == "update_contact":
            effected.append(args)
            raise TimeoutError("timed out after commit")
        return {"bookings": []}

    scripted([
        _tool("update_contact", {"id": "c1", "notes": "Noted"}, tid="u1"),
        _tool("answer", {"reply": LIVE}, tid="a1"),
        _tool("answer", {"reply": LIVE}, tid="a2"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Note and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}},
               {"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=False))
    assert len(effected) == 1
    assert "Nothing was sent or changed" not in out.reply
    assert out.action is None


def test_voice_tool_effect_then_exception_then_malformed_reply(scripted):
    """Voice-mode: tool records effect then raises, final answer is markup."""
    effected = []

    async def executor(name, args):
        if name == "update_contact":
            effected.append(args)
            raise TimeoutError("timed out after commit")
        return {"bookings": []}

    scripted([
        _tool("update_contact", {"id": "c1", "notes": "Noted"}, tid="u1"),
        _text(LIVE, stop="end_turn"),
        _text(LIVE, stop="end_turn"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Note and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}},
               {"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=True))
    assert len(effected) == 1
    assert "Nothing was sent or changed" not in out.reply
    assert out.action is None


# ── single correction budget: no extra forced call (Astra finding #4) ────────
def test_typed_plaintext_markup_twice_has_single_correction_budget(scripted):
    """Plain-text markup path: one corrective call, then honest failure.
    The old code permitted an extra forced-answer call after the retry."""
    calls = scripted([
        _text(LIVE, stop="tool_use"),       # first: plain-text markup
        _text(LIVE, stop="tool_use"),       # correction also markup
        # If a third call were made (extra forced answer), the test would fail
        # because scripted would run out or the wrong reply would leak through.
    ])
    out = asyncio.run(concierge.answer_business(
        "Check Jess's bookings.", [],
        tools=[{"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=lambda n, a: {"bookings": []},
        tenant_id="t1", voice_mode=False))
    assert out.reply == model_markup.HONEST_FAILURE
    # Exactly 2 provider calls: original + one correction. No third.
    assert len(calls) == 2


# ── correction after tools: answer-only, no re-execution (Astra finding #4) ─
def test_typed_correction_after_tools_does_not_reexecute(scripted):
    """After a mutating tool ran, the model attempts the SAME tool again in the
    correction round. The executor-side guard must reject it: effect exactly
    once, honest failure (not a second execution)."""
    executed = []

    async def executor(name, args):
        executed.append(name)
        if name == "update_contact":
            return {"ok": True}
        return {"bookings": []}

    scripted([
        # Round 0: model calls update_contact → executed once
        _tool("update_contact", {"id": "c1", "notes": "Done"}, tid="u1"),
        # Round 1: answer with markup → correction triggers
        _tool("answer", {"reply": LIVE}, tid="a1"),
        # Round 2 (correction): model returns the SAME mutating tool again
        # instead of answering. The executor guard must reject this.
        _tool("update_contact", {"id": "c1", "notes": "Done"}, tid="u2"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Update and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=False))
    # update_contact executed exactly once — the repeated attempt was rejected:
    assert executed.count("update_contact") == 1
    # Reply is honest failure (not a clean answer, not a false denial):
    assert "Nothing was sent or changed" not in out.reply
    assert out.action is None


def test_voice_correction_after_tools_does_not_reexecute(scripted):
    """Voice-mode equivalent: mutating tool ran, model retries it after
    correction. Executor guard rejects. Effect once, honest failure."""
    executed = []

    async def executor(name, args):
        executed.append(name)
        if name == "update_contact":
            return {"ok": True}
        return {"bookings": []}

    scripted([
        # Round 0: update_contact → executed
        _tool("update_contact", {"id": "c1", "notes": "Done"}, tid="u1"),
        # Round 1: malformed markup answer → correction
        _text(LIVE, stop="end_turn"),
        # Round 2 (correction, tool_choice=none): model returns tool_use anyway
        _tool("update_contact", {"id": "c1", "notes": "Done"}, tid="u2"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Update and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}},
               {"name": "list_bookings", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=True))
    assert executed.count("update_contact") == 1
    assert "Nothing was sent or changed" not in out.reply
    assert out.action is None


# ── mixed answer + sibling tool blocks: complete tool_result IDs ─────────────
def _multi_tool(*blocks):
    """Response with multiple tool_use blocks."""
    content = [SimpleNamespace(type="tool_use", name=n, input=inp, id=tid)
               for n, inp, tid in blocks]
    return SimpleNamespace(content=content, stop_reason="tool_use",
                           usage=_Usage(), model="test")


def test_typed_mixed_answer_plus_sibling_gets_complete_tool_results(scripted):
    """When the model returns answer + another tool_use in one response and the
    answer has markup, the correction must include tool_results for ALL IDs
    (not just the answer), and must NOT execute the dropped sibling action.
    Inspects the actual provider request messages for evidence."""
    executed = []

    async def executor(name, args):
        executed.append(name)
        return {"ok": True}

    calls = scripted([
        # Round 0: answer (markup) + sibling update_contact in the same response
        _multi_tool(("answer", {"reply": LIVE}, "a1"),
                    ("update_contact", {"id": "c1"}, "s1")),
        # Correction round: clean answer
        _tool("answer", {"reply": "Here you go."}, tid="a2"),
    ])
    out = asyncio.run(concierge.answer_business(
        "Update and confirm.", [],
        tools=[{"name": "update_contact", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=False))
    assert out.reply == "Here you go."
    # The sibling update_contact must NOT have been executed:
    assert "update_contact" not in executed
    # The correction request (calls[1]) must carry tool_results for BOTH IDs
    # from the previous assistant response: "a1" (answer) and "s1" (sibling).
    correction_msg = calls[1]["messages"][-1]
    result_ids = {r["tool_use_id"] for r in correction_msg["content"]
                  if isinstance(r, dict) and r.get("type") == "tool_result"}
    assert "a1" in result_ids, "missing tool_result for answer tool_use"
    assert "s1" in result_ids, "missing tool_result for sibling tool_use"


# ── normal successful recovery after markup correction ───────────────────────
def test_typed_successful_recovery_after_markup_correction(scripted):
    """After one markup correction, the model answers cleanly. Verify the
    correction works end-to-end and the reply reaches the caller."""
    calls = scripted([
        _tool("answer", {"reply": LIVE}, tid="a1"),       # markup
        _tool("answer", {"reply": "All sorted."}, tid="a2"),  # clean correction
    ])
    out = _run(voice_mode=False)
    assert out.reply == "All sorted."
    assert len(calls) == 2
