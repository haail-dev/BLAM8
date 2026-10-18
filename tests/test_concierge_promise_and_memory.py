"""Business concierge, two live voice failures (2026-09-23):

  * a reply that only PROMISES a lookup ("Let me pull up Dan's contact record
    now.") ended the turn with nothing checked — both loops now send it back
    once to make the call;
  * each turn saw only the words of earlier replies, so after a grounded
    answer it said "I haven't actually looked anything up yet" — history now
    carries each assistant turn's lookups as a note on its text.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import concierge, llm, portal_chat
from app.config import settings


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
    calls = []

    def load(replies):
        replies = list(replies)

        async def fake_create(**kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            return replies.pop(0)
        monkeypatch.setattr(concierge.llm, "create", fake_create)
        return calls
    return load


def _run(voice_mode):
    looked = []

    async def executor(name, args):
        looked.append(name)
        return {"contacts": [{"name": "Dan Price"}]}
    out = asyncio.run(concierge.answer_business(
        "When does Dan's dog's vaccination expire?", [],
        tools=[{"name": "search_contacts", "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", voice_mode=voice_mode))
    return out, looked


def _json(reply):
    return json.dumps({"reply": reply, "links": [], "action": {"kind": ""}})


# ── the detector ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reply", [
    "Let me pull up Dan's contact record now.",
    "Let me check Jess's record now.",
    "I'll search your contacts for Dan.",
])
def test_the_live_promises_are_caught(reply):
    assert concierge._announces_only(reply, None)


def test_a_promise_with_a_proposal_is_an_answer():
    assert not concierge._announces_only("I'll book that in — confirm below.",
                                         {"kind": "booking_create"})
    assert not portal_chat.announces_an_action("Mabel's vaccinations run to 1st July 2027.")


# ── voice loop ───────────────────────────────────────────────────────────────
def test_voice_promise_is_sent_back_and_the_lookup_happens(scripted):
    calls = scripted([
        _text(_json("Let me pull up Dan's contact record now.")),
        _tool("search_contacts", {"query": "Dan"}),
        _text(_json("Mabel's vaccinations run to 1st July 2027.")),
    ])
    out, looked = _run(voice_mode=True)
    assert out.reply == "Mabel's vaccinations run to 1st July 2027."
    assert looked == ["search_contacts"]
    assert concierge._ANNOUNCE_NUDGE in calls[1]["messages"][-1]["content"]


def test_voice_promise_is_nudged_only_once(scripted):
    calls = scripted([
        _text(_json("Let me check Jess's record now.")),
        _text(_json("Let me check Jess's record now.")),
    ])
    out, _ = _run(voice_mode=True)
    assert len(calls) == 2
    assert out.reply == "Let me check Jess's record now."


# ── typed loop (answer tool) ─────────────────────────────────────────────────
def test_typed_promise_via_answer_tool_is_sent_back(scripted):
    calls = scripted([
        _tool("answer", {"reply": "Let me check Jess's record now."}, tid="a1"),
        _tool("search_contacts", {"query": "Jess"}, tid="s1"),
        _tool("answer", {"reply": "Jess is all paid up, nothing outstanding."}, tid="a2"),
    ])
    out, looked = _run(voice_mode=False)
    assert out.reply == "Jess is all paid up, nothing outstanding."
    assert looked == ["search_contacts"]
    nudge = calls[1]["messages"][-1]["content"][0]
    assert nudge["type"] == "tool_result" and nudge["tool_use_id"] == "a1"


# ── history carries the lookups ──────────────────────────────────────────────
def test_history_notes_the_lookups_behind_an_assistant_turn():
    out = concierge.sanitize_history([
        {"role": "user", "content": "When does Dan's dog's jab expire?"},
        {"role": "assistant", "content": "1st July 2027.",
         "lookups": ["Searched contacts (Dan)", "Opened contact record (Dan Price)"]},
        {"role": "user", "content": "Great, thank you", "lookups": ["ignored"]},
    ], 16, 4000)
    assert out[1]["content"].startswith("1st July 2027.")
    assert "[Lookups you ran for this reply: Searched contacts (Dan); " \
           "Opened contact record (Dan Price)." in out[1]["content"]
    # a user turn never carries a lookups note (a trailing one is abandoned)
    assert out[2]["content"] == "Great, thank you" + concierge.ABANDONED_NOTE


def test_history_without_lookups_is_unchanged():
    out = concierge.sanitize_history([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello.", "lookups": None},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y", "lookups": "not-a-list"},
    ], 16, 4000)
    assert [t["content"] for t in out] == ["hi", "Hello.", "x", "y"]


def test_prompt_explains_the_note():
    p = concierge.build_business_prompt(voice_mode=True)
    assert "never say you haven't looked anything up" in p
    assert "Never write such a note yourself" in p


# ── an interrupted question is abandoned ─────────────────────────────────────
def test_a_question_cut_off_before_its_answer_is_marked_abandoned():
    """Live 2026-09-23 (iOS voice): "Can you tell me the invoices…" was
    interrupted by "Actually, when is Laura Finch's meet and greet?" and the
    model answered both. Replay: 3/4 answered invoices before, 0/6 after."""
    out = concierge.sanitize_history([
        {"role": "user", "content": "Tell me the quotes."},
        {"role": "assistant", "content": "14 quotes."},
        {"role": "user", "content": "Can you tell me the invoices we've got out?"},
    ], 16, 4000)
    assert out[0]["content"] == "Tell me the quotes."            # answered: untouched
    assert out[2]["content"].endswith(concierge.ABANDONED_NOTE)   # trailing: abandoned
    mid = concierge.sanitize_history([
        {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"}], 16, 4000)
    assert mid[0]["content"].endswith(concierge.ABANDONED_NOTE)
    assert mid[1]["content"] == "b"
