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


# ── restored conversations say when they happened ────────────────────────────
# Live 2026-10-19 (rig, GPT-Live): with a 17 Oct exchange restored into its
# history, "when was the last time we spoke" got "this is the start of our
# conversation… each session starts fresh for me".
def test_restored_conversations_are_headed_with_when_they_happened():
    out = concierge.sanitize_history([
        {"role": "user", "content": "Hi there", "created_at": "2026-10-17T10:33:27Z"},
        {"role": "assistant", "content": "Hello.", "created_at": "2026-10-17T10:33:40Z"},
        {"role": "user", "content": "When did we last speak?", "created_at": "2026-10-19T15:00:16+00:00"},
        {"role": "assistant", "content": "On Saturday.", "created_at": "2026-10-19T15:00:30+00:00"},
        {"role": "user", "content": "How many contacts?", "at": "2026-10-19T17:49:09Z"},
        {"role": "assistant", "content": "Twenty."},
        {"role": "user", "content": "Find Priya"},
        {"role": "assistant", "content": "Found Priya Test."},
    ], 16, 4000)
    # Europe/London, as the prompt's date: 10:33 UTC is 11:33 BST.
    assert out[0]["content"] == "[Conversation on Saturday 17 October 2026, from 11:33]\nHi there"
    assert out[1]["content"].endswith("\nHello.")                          # same conversation
    assert out[2]["content"].startswith("[Conversation on Monday 19 October 2026, from 16:00]\n")
    assert out[4]["content"].startswith("[Conversation on Monday 19 October 2026, from 18:49]\n")
    assert [t["content"] for t in out[5:]] == ["Twenty.", "Find Priya", "Found Priya Test."]


def test_turns_without_a_usable_time_get_no_heading():
    out = concierge.sanitize_history([
        {"role": "user", "content": "a", "created_at": "not a time"},
        {"role": "assistant", "content": "b", "at": None},
        {"role": "user", "content": "c", "at": ""},
        {"role": "assistant", "content": "d"},
    ], 16, 4000)
    assert [t["content"] for t in out] == ["a", "b", "c", "d"]


def test_the_heading_survives_the_history_window():
    turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i),
              "created_at": f"2026-10-17T10:{i:02d}:00Z"} for i in range(10)]
    out = concierge.sanitize_history(turns, 4, 4000)
    # The conversation began before the window; its first kept turn says when.
    assert out[0]["content"] == "[Conversation on Saturday 17 October 2026, from 11:06]\n6"
    assert [t["content"].split("\n")[-1] for t in out[1:]] == ["7", "8", "9"]


def test_prompt_treats_restored_conversations_as_memory():
    for voice_mode in (False, True):
        p = concierge.build_business_prompt(voice_mode=voice_mode)
        assert "[Conversation on …, from …]" in p
        assert "When none are supplied, say you have no " in p
        assert "An unheaded turn has no supplied date" in p
        assert "Rule 5 still decides what an unnamed reference points to" in p


def test_the_page_sends_when_restored_turns_were_said():
    from pathlib import Path
    from app.concierge_routes import Turn
    assert Turn(role="user", content="hi", at="2026-10-17T10:33:27Z").model_dump()["at"] == "2026-10-17T10:33:27Z"
    js = (Path(__file__).resolve().parents[1] / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
    assert "lookups: lookupsOf(m.trail), at: m.created_at || null" in js   # typed: restored turns
    assert "lookups: m.lookups, at: m.at || null" in js                    # Standard voice: same shape


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


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00", "broken"])
def test_timestamp_boundaries_do_not_break_history(stamp):
    turns = [{"role": "user", "content": "Question", "at": stamp},
             {"role": "assistant", "content": "Answer"}]
    out = concierge.sanitize_history(turns, 16, 4000)
    assert len(out) == 2 and out[0]["content"].endswith("Question")
    assert out[1]["content"] == "Answer"


def test_resanitizing_restored_history_does_not_repeat_dates():
    out = concierge.sanitize_history([
        {"role": "user", "content": "Question", "at": "2026-10-17T10:00:00Z"},
        {"role": "assistant", "content": "Answer", "at": "2026-10-17T10:01:00Z"},
    ], 16, 4000)
    assert concierge.sanitize_history(out, 16, 4000) == out


def test_saved_times_keep_order_across_unknown_and_backwards_metadata():
    turns = [
        {"role": "user", "content": "First", "at": "2026-10-25T01:30:00+00:00"},
        {"role": "assistant", "content": "Second", "at": "2026-10-25T01:45:00"},
        {"role": "user", "content": "Third", "at": "invalid"},
        {"role": "assistant", "content": "Fourth", "at": "2026-10-25T01:50:00Z"},
        {"role": "user", "content": "Fifth", "at": "2026-10-24T23:00:00Z"},
        {"role": "assistant", "content": "Last"},
    ]
    out = concierge.sanitize_history(turns, 16, 100)
    assert out[0]["content"].startswith("[Conversation on Sunday 25 October 2026, from 01:30]")
    assert out[1]["content"].startswith("[Saved turn on Sunday 25 October 2026, at 01:45]")
    assert out[2]["content"] == "Third"
    assert out[3]["content"].startswith("[Conversation on Sunday 25 October 2026, from 01:50]")
    assert out[4]["content"].startswith("[Conversation on Sunday 25 October 2026, from 00:00]")
    assert out[5]["content"] == "Last"
