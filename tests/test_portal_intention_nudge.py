"""The portal never ends a turn on an intention, and a picked slot is consent.

Live 2026-09-04 (farrier run, portal): "Can you book Murphy and Tilly in for
their next visit about six weeks on, any morning?" → the model replied "Let me
check what's available around mid-October for you." with NO tool call
(stop_reason end_turn). Nothing checked; the customer had to say "yes please,
go ahead". And after picking "Monday 12 October morning" from the assistant's
own list it was asked "Shall I go ahead with both?".

Run: python -m pytest tests/test_portal_intention_nudge.py -q
"""
import asyncio
import json
from types import SimpleNamespace

from app import portal_chat


def _text(reply):
    return SimpleNamespace(type="text", text=json.dumps({"reply": reply}))


def _tool(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def _msg(content, stop):
    return SimpleNamespace(content=content, stop_reason=stop, model="m",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def _run(monkeypatch, scripted):
    calls = []

    async def fake_create(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})   # snapshot: the loop mutates the list
        return scripted.pop(0)

    monkeypatch.setattr(portal_chat.llm, "create", fake_create)
    monkeypatch.setattr(portal_chat, "client", lambda: None)

    async def executor(name, args):
        return {"available": [{"date": "2026-10-12", "window": "am"}]}

    reply, _totals, _model = asyncio.run(portal_chat.answer_portal(
        "book them in six weeks on, any morning", [], system="s",
        tools=[{"name": "check_availability"}], tool_executor=executor,
        tenant_id="t1"))
    return reply, calls


def test_announces_an_action_matches_the_live_sentence_and_not_answers():
    assert portal_chat.announces_an_action("Let me check what's available around mid-October for you.")
    assert portal_chat.announces_an_action("I'll book that in for you now.")
    assert portal_chat.announces_an_action("One moment while I have a look at the diary.")
    assert not portal_chat.announces_an_action("Monday 12 October morning is free for both — BK-1007 is booked.")
    assert not portal_chat.announces_an_action("I don't have pricing details for trims. Would you like me to pass the question on?")
    assert not portal_chat.announces_an_action("You can check your booking any time with your reference.")


def test_an_intention_with_no_tool_call_is_sent_back_once(monkeypatch):
    reply, calls = _run(monkeypatch, [
        _msg([_text("Let me check what's available around mid-October for you.")], "end_turn"),
        _msg([_tool("check_availability", {"date_from": "2026-10-12"})], "tool_use"),
        _msg([_text("Monday 12 October morning is free.")], "end_turn"),
    ])
    assert reply == "Monday 12 October morning is free."
    assert len(calls) == 3
    # the nudge rides as a user turn after the model's own intention
    nudge = calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "called no tool" in nudge["content"]


def test_the_nudge_fires_once_so_a_stubborn_model_still_answers(monkeypatch):
    reply, calls = _run(monkeypatch, [
        _msg([_text("Let me check that for you.")], "end_turn"),
        _msg([_text("Let me check that for you.")], "end_turn"),
    ])
    assert reply == "Let me check that for you."
    assert len(calls) == 2


def test_a_real_answer_is_not_nudged(monkeypatch):
    reply, calls = _run(monkeypatch, [
        _msg([_text("We cover Wells and Frome, not Bath.")], "end_turn"),
    ])
    assert reply == "We cover Wells and Frome, not Bath." and len(calls) == 1


def test_a_picked_slot_is_consent_in_the_service_prompt():
    system = portal_chat.build_portal_prompt(business_name="Tanner & Vale", scheduling_on=True,
                                             diary_flavour="service")
    assert "that is their consent: call create_booking straight away" in system
    assert "never ask 'shall I go ahead?'" in system
