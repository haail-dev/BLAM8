"""TMS specialist agent + the base→TMS hand-off (docs/modules/module-agent-handoff.md, Phase 1).

What this pins:
  - the transport desk's prompt: role, the lifted tool-discipline, and the
    structured-result contract (no customer prose)
  - answer() runs the SAME tool loop the base agent does — check_availability →
    hold_booking → a structured result — over scheduling.FLEET_ROUTING_TOOLS, summing
    usage; non-JSON degrades to a clean error result (D7)
  - make_handoff: ONE hand-off per email (a second ask reuses the answer, never a
    second hold), the specialist's hold bubbles into state['bookings'] for the pipeline
    to link (D5), and its tokens bubble into state['usage'] to be metered
  - the base draft loop awaits the async hand-off executor and composes from the
    structured answer it feeds back

Run: python -m pytest tests/ -q
"""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

from app.agents import tms as tms_agent


def usage(n):
    return SimpleNamespace(input_tokens=n, output_tokens=n * 2,
                           cache_read_input_tokens=n * 3, cache_creation_input_tokens=0)


def text_response(payload, n=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason="end_turn", usage=usage(n), model="fake-model",
    )


def tool_response(name, args, tool_id="tu-1", n=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
        stop_reason="tool_use", usage=usage(n), model="fake-model",
    )


class FakeClient:
    """Stands in for AsyncAnthropic: returns scripted responses, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


HELD = {"status": "held", "date": "2026-06-19", "spaces": 6, "weight_kg": 0,
        "booking_ref": "BK-1042", "branch": "", "assumed": [], "alternatives": [],
        "ask": "", "message": ""}


# ── the hand-off tool the base agent sees ────────────────────────────────────
def test_ask_transport_tool_shape():
    t = tms_agent.ASK_TRANSPORT_TOOL
    assert t["name"] == "ask_transport"
    assert list(t["input_schema"]["properties"]) == ["request"]
    assert t["input_schema"]["required"] == ["request"]
    # The base never sees vehicles/capacity internals — only the customer's words.
    assert "vehicle" not in str(t["input_schema"]).lower()


# ── the desk's prompt ─────────────────────────────────────────────────────────
def test_prompt_has_transport_rules_and_result_contract():
    p = tms_agent.build_system_prompt(diary_shape="Fleet diary: 2 vehicles",
                                      today=date(2026, 6, 16))
    assert "transport/logistics desk" in p
    assert "STRUCTURED result" in p
    assert "Tuesday 16 June 2026" in p and "2026-06-16" in p
    assert "check_availability" in p and "hold_booking" in p
    assert "Fleet diary: 2 vehicles" in p          # the diary shape is injected
    # the result-shaping instructions name the fields the base composes from
    for field in ("status", "booking_ref", "alternatives", "needs_info"):
        assert field in p
    # the desk reports facts, never writes to the customer
    assert "you do NOT write to the customer" in p


# ── answer(): the tool loop + structured output ───────────────────────────────
def test_answer_runs_tool_loop_holds_and_returns_structured(monkeypatch):
    fake = FakeClient([
        tool_response("check_availability",
                      {"date_from": "2026-06-19", "date_to": "2026-06-19",
                       "pallets": [{"type": "full", "qty": 6}]}, n=10),
        tool_response("hold_booking",
                      {"date": "2026-06-19", "summary": "6 pallets",
                       "pallets": [{"type": "full", "qty": 6}]}, tool_id="tu-2", n=5),
        text_response(HELD, n=3),
    ])
    monkeypatch.setattr(tms_agent, "_client", fake)

    executed = []

    def executor(name, args):
        executed.append(name)
        if name == "check_availability":
            return {"available": [{"date": "2026-06-19", "remaining_spaces": 6, "fits": True}]}
        return {"held": True, "booking_id": "b-1", "booking_ref": "BK-1042",
                "date": "2026-06-19", "spaces": 6, "status": "proposed"}

    result, totals = asyncio.run(tms_agent.answer(
        "van space Friday, 6 pallets", executor=executor,
        diary_shape="Fleet diary", today=date(2026, 6, 16)))

    assert executed == ["check_availability", "hold_booking"]
    assert result["status"] == "held" and result["booking_ref"] == "BK-1042"
    # usage summed across all three calls (10 + 5 + 3)
    assert totals["input_tokens"] == 18 and totals["output_tokens"] == 36
    # the desk was handed the fleet+routing tools and the structured-output schema
    first = fake.calls[0]
    assert [t["name"] for t in first["tools"]] == ["check_availability", "hold_booking"]
    assert first["output_config"]["format"]["schema"] is tms_agent.TMS_SCHEMA
    # the hold's tool_result was fed back to the model before it answered
    last_tool_result = fake.calls[-1]["messages"][-1]["content"][0]
    assert last_tool_result["type"] == "tool_result"
    assert json.loads(last_tool_result["content"])["held"] is True


def test_answer_can_ask_for_more_info_without_holding(monkeypatch):
    needs = {**HELD, "status": "needs_info", "booking_ref": "",
             "ask": "Which branch — Leeds or Manchester?"}
    fake = FakeClient([
        tool_response("check_availability", {"date_from": "2026-06-19", "date_to": "2026-06-19"}),
        text_response(needs, n=2),
    ])
    monkeypatch.setattr(tms_agent, "_client", fake)
    result, _ = asyncio.run(tms_agent.answer(
        "a delivery Friday", executor=lambda *a: {"error": "branch_required",
                                                  "branches": ["Leeds", "Manchester"]},
        today=date(2026, 6, 16)))
    assert result["status"] == "needs_info" and "branch" in result["ask"].lower()


def test_answer_falls_back_to_error_on_bad_output(monkeypatch):
    # A valid-JSON-but-not-a-result reply (a safety refusal/truncation) degrades to a
    # clean error so the base agent says a colleague will confirm — never a crash.
    fake = FakeClient([text_response("nonsense, no object here")])
    monkeypatch.setattr(tms_agent, "_client", fake)
    result, _ = asyncio.run(tms_agent.answer("x", executor=lambda *a: {},
                                             today=date(2026, 6, 16)))
    assert result["status"] == "error"


# ── make_handoff: one per email, bubbles holds + usage ────────────────────────
def _stub_handoff_db(monkeypatch):
    monkeypatch.setattr(tms_agent.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(tms_agent.db, "list_vehicles", lambda *a, **k: [])
    monkeypatch.setattr(tms_agent.db, "list_branches", lambda *a, **k: [])


def test_make_handoff_one_per_email_bubbles_hold_and_usage(monkeypatch):
    _stub_handoff_db(monkeypatch)

    def fake_make_executor(tenant_id, **kw):
        state = {"bookings": []}

        def execute(name, args):
            state["bookings"].append({"id": f"b-{len(state['bookings']) + 1}"})
            return {"held": True}
        return execute, state
    monkeypatch.setattr(tms_agent.scheduling, "make_executor", fake_make_executor)

    seen = []

    async def fake_answer(request, *, executor, diary_shape=None, today=None, **kw):
        seen.append(request)
        executor("hold_booking", {})                 # place the real hold via the fleet exec
        return ({**HELD, "booking_ref": "BK-9"}, {"input_tokens": 7})
    monkeypatch.setattr(tms_agent, "answer", fake_answer)

    execute, state = tms_agent.make_handoff("t1", contact="c@x.co", thread_id="th-1",
                                            mailbox_id="mb-1")
    out1 = asyncio.run(execute("ask_transport", {"request": "6 pallets Friday"}))
    assert out1["booking_ref"] == "BK-9"
    assert state["bookings"] == [{"id": "b-1"}]       # the hold bubbled up for linking
    assert state["usage"]["input_tokens"] == 7        # tokens bubbled up for metering

    out2 = asyncio.run(execute("ask_transport", {"request": "again, same email"}))
    assert out2 == out1                               # cached — the desk ran once
    assert seen == ["6 pallets Friday"]
    assert state["bookings"] == [{"id": "b-1"}]       # no second hold placed


# ── the base draft loop awaits the async hand-off executor ────────────────────
def test_base_draft_awaits_async_handoff_executor(monkeypatch):
    from app import agent
    fake = FakeClient([
        tool_response("ask_transport", {"request": "6 pallets Friday"}),
        text_response({"reply": "Pencilled in for Friday, ref BK-1042.",
                       "deferred": False, "missing": "", "scheduling_requested": False}),
    ])
    monkeypatch.setattr(agent, "_client", fake)

    seen = []

    async def executor(name, args):                  # async, like the real hand-off
        seen.append((name, args))
        return {"status": "held", "booking_ref": "BK-1042", "date": "2026-06-19"}

    result = asyncio.run(agent.draft_reply(
        {}, {"subject": "Quote", "body": "Any van space for 6 pallets Friday?"},
        tools=[tms_agent.ASK_TRANSPORT_TOOL], tool_executor=executor, transport=True))

    assert seen == [("ask_transport", {"request": "6 pallets Friday"})]
    assert result.text == "Pencilled in for Friday, ref BK-1042."
    # the structured answer was fed back to the model as the tool_result it composed from
    fed = json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])
    assert fed["booking_ref"] == "BK-1042" and fed["status"] == "held"
    # the base prompt for a TMS tenant carries the hand-off section, not transport rules
    system = fake.calls[0]["system"]
    system = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "<transport>" in system and "pallet spaces" not in system
