"""Sonnet 5 request plumbing and synthetic receipts; no live model proof."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from app import db, llm, metering, usage
from app.config import settings
from tests.test_agent_tools import FakeClient, run_draft, text_response, tool_response
from tests.test_cost_receipts import Stream, tokens


MODEL = "claude-sonnet-5"


@pytest.mark.parametrize("model", [MODEL, "claude-sonnet-4-6", "claude-haiku-4-5"])
@pytest.mark.parametrize("thinking", [None, {"type": "adaptive"}])
@pytest.mark.parametrize("choice", [None, "any", "none"])
@pytest.mark.parametrize("cache", [False, True])
def test_resolved_model_controls_thinking_without_changing_caller(model, thinking, choice, cache):
    fake = FakeClient([text_response({"reply": "Hello."})])
    request = {"model": model, "max_tokens": 60,
               "messages": [{"role": "user", "content": "Hello"}]}
    if thinking is not None:
        request["thinking"] = thinking
    if choice is not None:
        request["tool_choice"] = {"type": choice}
    before = deepcopy(request)
    asyncio.run(llm.create(client=fake, cache=cache, **request))
    sent = fake.calls[0]
    assert sent.get("thinking") == ({"type": "disabled"} if model == MODEL else thinking)
    assert sent.get("tool_choice") == request.get("tool_choice")
    assert sent["max_tokens"] == 60 and sent["model"] == model
    assert request == before


@pytest.mark.parametrize("entry", [llm.create, llm.stream])
def test_every_hedged_attempt_disables_thinking(entry):
    requests = []

    def stream(**kwargs):
        requests.append(kwargs)
        return Stream(len(requests), delay=1 if len(requests) == 1 else 0)

    asyncio.run(entry(client=NS(messages=NS(stream=stream)), model=MODEL,
                      messages=[], max_tokens=60, tool_choice={"type": "any"},
                      hedge_after=.01))
    assert len(requests) == 2
    assert all(r["thinking"] == {"type": "disabled"} for r in requests)
    assert all(r["tool_choice"] == {"type": "any"} for r in requests)


@pytest.mark.parametrize("booking", [False, True])
def test_both_drafting_settings_reach_transport_and_price_receipts(monkeypatch, booking):
    # Set only the selected path to Sonnet 5, so selecting the wrong setting fails.
    monkeypatch.setattr(settings, "anthropic_model", "claude-sonnet-4-6" if booking else MODEL)
    monkeypatch.setattr(settings, "anthropic_booking_model", MODEL if booking else "claude-sonnet-4-6")
    monkeypatch.setattr(settings, "llm_log_mode", "full")
    calls, events = [], []
    monkeypatch.setattr(db, "insert_llm_call", calls.append)
    monkeypatch.setattr(db, "insert_usage_event", events.append)
    responses = [text_response({"reply": "Hello.", "deferred": False, "missing": ""})]
    if booking:
        responses.insert(0, tool_response("ready_to_write", {}))
    for response in responses:
        response.model, response.usage = MODEL, tokens()
    fake, result = run_draft(monkeypatch, responses, lambda *args: {},
                             tools=[{"name": "check_availability", "description": "Check the diary",
                                     "input_schema": {"type": "object"}}] if booking else [],
                             tenant_id="tenant-offline")
    assert result.text == "Hello."
    assert len(fake.calls) == len(calls) == len(responses)
    assert all(r["model"] == MODEL and r["thinking"] == {"type": "disabled"} for r in fake.calls)
    if booking:
        assert fake.calls[0]["tool_choice"] == {"type": "any"}
    assert all(r["request"]["thinking"] == {"type": "disabled"} for r in calls)
    # Synthetic per-call receipt: 100 input, 20 output, 30 cache reads,
    # 40 five-minute writes, 50 one-hour writes = $0.000706.
    assert all(r["raw_usage"]["cost_usd"] == pytest.approx(.000706) for r in calls)
    usage.record_anthropic("tenant-offline", MODEL, result.usage)
    assert events[0]["cost_usd"] == pytest.approx(.000706 * len(responses))
    assert events[0]["raw_usage"]["complete"]


@pytest.mark.parametrize("inp,out,read,five,hour,expected", [
    (1_000_000, 0, 0, 0, 0, 2), (0, 1_000_000, 0, 0, 0, 10),
    (0, 0, 1_000_000, 0, 0, .2), (0, 0, 0, 1_000_000, 0, 2.5),
    (0, 0, 0, 0, 1_000_000, 4),
])
def test_sonnet5_price_components(inp, out, read, five, hour, expected):
    assert metering.price(MODEL, {"usage": metering.raw_usage(tokens(inp, out, read, five, hour))}) == pytest.approx(expected)
