"""Drafts say only what the business told them (Kris 2026-09-26, plumber rig).

Three drafts on Hartwell & Daughters each got the headline rule right and
added something nobody said:
  - Priya (boiler fitted last April): "usually points to a small leak… or a
    faulty pressure relief valve", and Vaillant will "arrange a visit from
    their own engineers" — rule 2b, no judgement or outside facts.
  - Tom (new boiler): "plenty of availability… as early as next week" with no
    diary tool called — rule 2c, no timing without checking.
  - Leanne (Swindon): the business's "Coverage" note never reached the prompt,
    so "outside our area" came from the model's geography — a small knowledge
    base is now read whole.

Run: python -m pytest tests/test_draft_says_only_what_it_knows.py -q
"""
import asyncio
import json
from types import SimpleNamespace

from app import agent, knowledge, pipeline

KB = ("[Warranty rule]\nA boiler under 2 years old is usually still under the manufacturer's "
      "warranty: tell the customer to call the manufacturer first rather than booking a paid repair.")


def _usage():
    return SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def _text(envelope: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(envelope))],
                           usage=_usage(), stop_reason="end_turn", model="m")


def _tool(name: str, args: dict, tid: str = "t1"):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=args, id=tid)],
                           usage=_usage(), stop_reason="tool_use", model="m")


def _env(reply: str) -> dict:
    return {"reply": reply, "deferred": False, "missing": "", "action_summary": "",
            "thread_state": "open", "category": "needs_reply"}


def _script(monkeypatch, turns: list):
    """Feed the drafting loop these model turns in order; record what it was sent."""
    seen = []

    async def fake_create(**kw):
        seen.append(kw)
        return turns.pop(0)
    monkeypatch.setattr(agent.llm, "create", fake_create)
    return seen


def test_the_real_drafts_are_caught_and_the_business_own_words_are_not():
    priya = ("A boiler losing pressure every couple of days usually points to a small leak "
             "somewhere in the system. Vaillant can arrange a visit from their own engineers.")
    assert agent._unsourced_bits(priya, KB) == ["usually points to", "their own engineers"]
    assert agent._unsourced_bits("checkatrade.com is a good place to start.", KB) == ["checkatrade"]
    # The business's own rule, in its own words, is never flagged.
    assert agent._unsourced_bits("A boiler under 2 years old is usually still under the "
                                 "manufacturer's warranty, so call them first.", KB) == []
    assert agent._TIMING_CLAIM.search("We've got plenty of availability — we could get someone "
                                      "over as early as next week.")
    assert not agent._TIMING_CLAIM.search("Monday morning is still available and you're booked in.")


def test_a_diagnosis_is_sent_back_and_rewritten(monkeypatch):
    seen = _script(monkeypatch, [
        _text(_env("Losing pressure usually points to a small leak. Call Vaillant first.")),
        _text(_env("As it was fitted last April, give Vaillant a call first — it may be covered.")),
    ])
    out = asyncio.run(agent.draft_reply({"business_name": "Hartwell"},
                                        {"subject": "Boiler losing pressure", "body": "Fitted April last year."},
                                        knowledge=KB))
    assert out.text.startswith("As it was fitted last April")
    assert "usually points to" in seen[1]["messages"][-1]["content"]


def test_a_timing_claim_without_the_diary_is_sent_back(monkeypatch):
    ran = []

    def executor(name, args):
        ran.append(name)
        return {"available": [{"date": "2026-09-29", "window": "am"}]}
    tools = [{"name": "check_availability", "description": "", "input_schema": {"type": "object"}}]
    seen = _script(monkeypatch, [
        _tool(agent.READY_TOOL["name"], {}),
        _text(_env("We've got plenty of availability — as early as next week.")),
        _tool("check_availability", {"date_from": "2026-09-28", "date_to": "2026-10-02",
                                     "service": "Survey visit"}, tid="t2"),
        _text(_env("The first free survey is Tuesday morning — shall I put you down for it?")),
    ])
    out = asyncio.run(agent.draft_reply({"business_name": "Hartwell"},
                                        {"subject": "New boiler", "body": "How soon?"},
                                        tools=tools, tool_executor=executor, scheduling=True))
    assert ran == ["check_availability"]
    assert out.text.startswith("The first free survey is Tuesday")
    nudges = [m for m in seen[2]["messages"] if m["role"] == "user" and isinstance(m["content"], str)]
    assert "no diary tool ran" in nudges[-1]["content"]


def test_a_timing_claim_after_checking_the_diary_stands(monkeypatch):
    tools = [{"name": "check_availability", "description": "", "input_schema": {"type": "object"}}]
    _script(monkeypatch, [
        _tool("check_availability", {"date_from": "2026-09-28", "date_to": "2026-10-02"}),
        _tool(agent.READY_TOOL["name"], {}, tid="t2"),
        _text(_env("We could get someone over as early as Tuesday morning.")),
    ])
    out = asyncio.run(agent.draft_reply({"business_name": "Hartwell"},
                                        {"subject": "Leak", "body": "When?"}, tools=tools,
                                        tool_executor=lambda n, a: {"available": [{"date": "2026-09-29"}]},
                                        scheduling=True))
    assert out.text == "We could get someone over as early as Tuesday morning."


def test_a_small_knowledge_base_is_read_whole_but_the_pills_show_only_matches(monkeypatch):
    rows = [{"id": "1", "source": "How a first booking works", "content": "x" * 300, "similarity": 0.6},
            {"id": "2", "source": "Coverage", "content": "Bath and within 15 miles.", "similarity": 0.3},
            {"id": "3", "source": "Price", "content": "£85 call-out.", "similarity": 0.2}]

    async def embed(q):
        return [0.0], 1
    monkeypatch.setattr(knowledge, "is_configured", lambda: True)
    monkeypatch.setattr(knowledge.embeddings, "embed_query", embed)
    monkeypatch.setattr(knowledge.usage, "record_voyage", lambda *a, **k: None)
    monkeypatch.setattr(knowledge.db, "match_knowledge_chunks", lambda *a, **k: [dict(r) for r in rows])
    whole = asyncio.run(knowledge.retrieve("t", "do you cover Swindon?", whole_if_small=True))
    assert [r["source"] for r in whole] == ["How a first booking works", "Coverage", "Price"]
    assert "Bath and within 15 miles" in knowledge.render_for_prompt(whole)
    assert [s["source"] for s in pipeline._draft_sources(whole)] == ["How a first booking works"]
    # Without the switch (the help assistant's large KB), the floor still rules.
    top = asyncio.run(knowledge.retrieve("t", "do you cover Swindon?"))
    assert [r["source"] for r in top] == ["How a first booking works"]
    # A big knowledge base is never dumped whole.
    rows[0]["content"] = "x" * 20000
    big = asyncio.run(knowledge.retrieve("t", "do you cover Swindon?", whole_if_small=True))
    assert [r["source"] for r in big] == ["How a first booking works"]


def test_the_redrafts_that_slipped_through_are_caught_too():
    """The first redraft round (12b1234): Tom's "plenty of survey slots
    available from Monday onwards" and Priya's "they may send an engineer out
    at no charge" both got past."""
    assert agent._TIMING_CLAIM.search("We have plenty of survey slots available from Monday onwards")
    assert agent._TIMING_CLAIM.search("Survey appointments are free from Tuesday.")
    assert not agent._TIMING_CLAIM.search("The survey is free and takes about an hour.")
    assert agent._unsourced_bits("It's worth calling Vaillant directly — they may send an engineer "
                                 "out at no charge to you.", KB)
    assert not agent._unsourced_bits("Give Vaillant a call first, before booking a paid repair.", KB)


def test_a_weekday_that_is_not_that_date_is_caught():
    """Rachel's CP12 hold was Monday 28 September; the reply said Monday 29th."""
    from datetime import date
    today = date(2026, 9, 26)
    assert agent._wrong_weekdays("12 Oldfield Road — Monday 29 September, morning", today)
    assert agent._wrong_weekdays("Monday 28 September, morning (BK-1006)", today) == []
    assert agent._wrong_weekdays("Saturday 3rd October in the afternoon", today) == []
    assert agent._wrong_weekdays("Friday, 2 Oct", today) == []           # 2 Oct 2026 is a Friday
    assert agent._wrong_weekdays("Thursday 1 January", today)            # 1 Jan 2027 is a Friday
    assert agent._wrong_weekdays("see you on the 29th", today) == []


def test_a_wrong_weekday_is_sent_back(monkeypatch):
    seen = _script(monkeypatch, [
        _text(_env("I've held Monday 29 September, morning (BK-1006).")),
        _text(_env("I've held Monday 28 September, morning (BK-1006).")),
    ])
    out = asyncio.run(agent.draft_reply({"business_name": "Hartwell"},
                                        {"subject": "CP12", "body": "Book it please"}))
    assert out.text == "I've held Monday 28 September, morning (BK-1006)."
    assert "wrong date" in seen[1]["messages"][-1]["content"]
