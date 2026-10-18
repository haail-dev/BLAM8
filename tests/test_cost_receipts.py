"""S1f: observed provider attempts, arithmetic, and bounded real callers."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app import db, llm, llm_admin, metering, usage
from app.config import settings

MODEL = "claude-sonnet-4-6"


def tokens(inp=100, out=20, read=30, five=40, hour=50):
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
              cache_creation_input_tokens=five + hour,
              cache_creation=NS(ephemeral_5m_input_tokens=five, ephemeral_1h_input_tokens=hour))


@pytest.mark.parametrize("inp,out,read,five,hour,expected", [
    (1_000_000, 0, 0, 0, 0, 3), (0, 1_000_000, 0, 0, 0, 15),
    (0, 0, 1_000_000, 0, 0, .3), (0, 0, 0, 1_000_000, 0, 3.75),
    (0, 0, 0, 0, 1_000_000, 6), (100, 20, 30, 40, 50, .001059),
])
def test_verified_arithmetic(inp, out, read, five, hour, expected):
    assert metering.price(MODEL, {"usage": metering.raw_usage(tokens(inp, out, read, five, hour))}) == pytest.approx(expected)


def test_unknown_split_model_and_missing_usage_are_not_zero():
    u = metering.raw_usage(tokens())
    u.pop("cache_creation")
    assert metering.price(MODEL, {"usage": u}) is None
    assert usage.cost_usd("voyage", "voyage-4", embed_tokens=100) is None
    assert metering.price("unknown-model", {"usage": metering.raw_usage(tokens())}) is None
    assert metering.price(MODEL, {"usage": None}) is None
    u["cache_creation"] = {"ephemeral_5m_input_tokens": 90, "ephemeral_1h_input_tokens": 1}
    assert metering.price(MODEL, {"usage": u}) is None


@pytest.fixture
def captured(monkeypatch):
    calls, events = [], []
    monkeypatch.setattr(db, "insert_llm_call", calls.append)
    monkeypatch.setattr(db, "insert_usage_event", events.append)
    monkeypatch.setattr(settings, "llm_log_mode", "meta")
    return calls, events


class Stream:
    def __init__(self, tag, delay=0, failure=False, observed=True):
        self.tag, self.delay, self.failure, self.observed = tag, delay, failure, observed
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self.events()

    async def events(self):
        if self.observed:
            yield NS(type="message_start", message=NS(id=f"msg-{self.tag}", usage=tokens(out=0)))
        await asyncio.sleep(self.delay)
        if self.failure:
            raise RuntimeError("stream failed")
        yield NS(type="content_block_start")
        yield NS(type="content_block_delta", delta=NS(type="text_delta", text="OK"))
        yield NS(type="message_delta", usage=NS(output_tokens=20))

    async def get_final_message(self):
        return NS(id=f"msg-{self.tag}", usage=tokens(), model=MODEL, stop_reason="end_turn", content=[])


@pytest.mark.parametrize("hedged", [False, True])
def test_stream_and_hedge_receipts_reach_usage_and_admin(captured, hedged):
    calls, events = captured
    streams = []

    def factory(**kw):
        s = Stream(len(streams), delay=1 if hedged and not streams else 0)
        streams.append(s)
        return s

    heard = []
    msg = asyncio.run(llm.stream(client=NS(messages=NS(stream=factory)), tenant_id="tenant-a",
                                operation="concierge_business", model=MODEL, messages=[],
                                on_delta=heard.append, hedge_after=.01 if hedged else None))
    usage.record_anthropic("tenant-a", MODEL, msg.usage, operation="concierge_business")
    assert heard == ["OK"] and all(s.closed for s in streams)
    assert len(calls) == (2 if hedged else 1)
    receipts = events[0]["raw_usage"]["attempts"]
    assert {a["attempt_id"] for a in receipts} == {c["raw_usage"]["attempt_id"] for c in calls}
    assert len({a["request_id"] for a in receipts}) == 1
    assert events[0]["cost_usd"] == pytest.approx(.001818 if hedged else .001059)
    assert events[0]["raw_usage"]["complete"] is (not hedged)
    assert llm_admin.group_exchanges(calls)[0]["cost_usd"] == pytest.approx(events[0]["cost_usd"])
    assert all("request" not in c and "response" not in c for c in calls)
    assert all(c["tenant_id"] == "tenant-a" for c in calls)


@pytest.mark.parametrize("observed", [False, True])
def test_failed_stream_retains_partial_usage_without_a_completed_turn(captured, observed):
    calls, events = captured
    s = Stream(0, failure=True, observed=observed)
    with pytest.raises(RuntimeError, match="stream failed"):
        asyncio.run(llm.stream(client=NS(messages=NS(stream=lambda **k: s)), tenant_id="tenant-b",
                               model=MODEL, operation="concierge_business", hedge_after=None))
    assert s.closed and len(calls) == len(events) == 1
    assert events[0]["operation"] == "llm_failure"
    assert not events[0]["raw_usage"]["complete"]
    assert events[0]["raw_usage"]["attempts"][0]["cost_usd"] == (pytest.approx(.000759) if observed else None)
    assert usage.handled_emails(events) == 0


def test_create_preserves_sdk_identity_and_metadata_when_logs_are_off(captured, monkeypatch):
    calls, events = captured
    monkeypatch.setattr(settings, "llm_log_mode", "off")
    original = NS(id="provider-message", usage=tokens(), model=MODEL, stop_reason="end_turn")

    async def create(**kwargs):
        return original

    result = asyncio.run(llm.create(client=NS(messages=NS(create=create)), model=MODEL))
    assert result is original
    totals = metering.Totals()
    metering.add(totals, result.usage)
    usage.record_anthropic("tenant-a", MODEL, totals)
    assert not calls
    assert events[0]["raw_usage"]["attempts"][0]["provider_message_id"] == "provider-message"
    assert events[0]["raw_usage"]["complete"]


def test_e9_real_agent_cap_and_corrective_call_keep_every_attempt(captured, monkeypatch):
    from tests.test_agent_tools import run_draft, tool_response
    from app.agent import TOOL_LOOP_MAX_CALLS
    fake, result = run_draft(monkeypatch, [tool_response("check_availability", {})],
                             lambda *args: {"available": []})
    receipts = metering.receipts(result.usage)
    assert len(receipts) == len(fake.calls) == TOOL_LOOP_MAX_CALLS + 1
    assert len({a["attempt_id"] for a in receipts}) == len(receipts)
    assert len({a["request_id"] for a in receipts}) == 1
    assert not result.text


def test_later_round_failure_keeps_earlier_paid_rounds_once(captured):
    calls, events = captured
    count = 0

    async def create(**kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("third round failed")
        return NS(usage=tokens(), model=MODEL, stop_reason="end_turn")

    async def run():
        with llm.request_scope():
            client = NS(messages=NS(create=create))
            first = await llm.create(client=client, tenant_id="tenant-a", model=MODEL,
                                     operation="concierge_business")
            # Nested calls may meter before the outer exchange ends.
            usage.record_anthropic("tenant-a", MODEL, first.usage, operation="concierge_business")
            await llm.create(client=client, tenant_id="tenant-a", model=MODEL,
                             operation="concierge_business")
            await llm.create(client=client, tenant_id="tenant-a", model=MODEL,
                             operation="concierge_business")

    with pytest.raises(RuntimeError, match="third round"):
        asyncio.run(run())
    ids = [a["attempt_id"] for e in events for a in e["raw_usage"]["attempts"]]
    assert len(ids) == len(set(ids)) == len(calls) == 3
    assert sum(e["cost_usd"] for e in events) == pytest.approx(.002118)
    assert sum(e["operation"] == "concierge_business" for e in events) == 1
    assert any(not e["raw_usage"]["complete"] for e in events)


def test_real_concierge_typed_and_voice_keep_receipts(captured, monkeypatch):
    from app import concierge
    from tests.test_agent_tools import FakeClient, text_response
    for voice in (False, True):
        response = text_response({"reply": "Hello there.", "links": [], "action": {"kind": "", "fields": "{}"}})
        response.usage = tokens()
        response.model = MODEL
        monkeypatch.setattr(concierge, "_client", FakeClient([response]))
        result = asyncio.run(concierge.answer_business("Hello", tools=[], tool_executor=None,
                                                       tenant_id="tenant-a", voice_mode=voice))
        assert result.reply == "Hello there."
        assert len(metering.receipts(result.usage)) == 1
        usage.record_anthropic("tenant-a", MODEL, result.usage, operation="concierge_business")
    assert all(e["cost_usd"] == pytest.approx(.001059) for e in captured[1])


def test_failure_cost_belongs_to_assistant_without_an_extra_turn(captured, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(db, "count_bookings_since", lambda *a, **k: 0)
    e = {"operation": "llm_failure", "module": "concierge", "cost_usd": .02,
         "raw_usage": {"attempts": [{"operation": "concierge_business"}]}}
    result = usage._assistant_and_portal({}, "tenant-a", [e], datetime.now(timezone.utc))
    assert result["assistant_turns"] == 0
    assert result["assistant_cost_usd"] == .02


def test_actual_sdk_retry_header_preserves_unknown_attempt(captured):
    import anthropic
    import httpx
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(529, json={"type": "error", "error": {
                "type": "overloaded_error", "message": "offline fixture"}})
        return httpx.Response(200, headers={"request-id": "req-offline"}, json={
            "id": "msg-offline", "type": "message", "role": "assistant", "model": MODEL,
            "content": [{"type": "text", "text": "Content must not enter receipts"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": metering.raw_usage(tokens())})

    async def run():
        async with anthropic.AsyncAnthropic(api_key="offline-test", max_retries=1,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            return await llm.create(client=client, tenant_id="tenant-a", model=MODEL,
                                    max_tokens=20, messages=[{"role": "user", "content": "private"}])

    msg = asyncio.run(run())
    usage.record_anthropic("tenant-a", MODEL, msg.usage)
    calls, events = captured
    assert len(requests) == len(calls) == 2
    raw = events[0]["raw_usage"]
    assert not raw["complete"] and len(raw["attempts"]) == 2
    assert raw["attempts"][0]["usage"] is None
    assert raw["attempts"][1]["provider_request_id"] == "req-offline"
    assert raw["attempts"][1]["sdk_retry_count"] == 1
    assert events[0]["cost_usd"] == pytest.approx(.001059)
    assert "private" not in str(raw) and "Content must" not in str(raw)


def test_actual_sdk_stream_preserves_ttl_and_request_id(captured):
    import anthropic
    import httpx
    import json
    start = {"id": "msg-stream", "type": "message", "role": "assistant", "model": MODEL,
             "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": metering.raw_usage(tokens(out=0))}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream", "request-id": "req-stream"}, content=body))

    async def run():
        async with anthropic.AsyncAnthropic(api_key="offline-test", max_retries=2,
                http_client=httpx.AsyncClient(transport=transport)) as client:
            return await llm.stream(client=client, tenant_id="tenant-a", model=MODEL,
                                    max_tokens=20, messages=[{"role": "user", "content": "private"}])

    msg = asyncio.run(run())
    usage.record_anthropic("tenant-a", MODEL, msg.usage)
    raw = captured[1][0]["raw_usage"]
    assert raw["complete"]
    assert raw["attempts"][0]["provider_request_id"] == "req-stream"
    assert raw["attempts"][0]["sdk_retry_count"] == 0
    assert raw["known_cost_usd"] == pytest.approx(.001059)


@pytest.mark.parametrize("modifier", [{"speed": "fast"}, {"inference_geo": "us"}])
def test_unverified_price_modifiers_stay_unknown(captured, modifier):
    async def create(**kwargs):
        return NS(usage=tokens(), model=MODEL, stop_reason="end_turn")
    result = asyncio.run(llm.create(client=NS(messages=NS(create=create)), model=MODEL, **modifier))
    usage.record_anthropic("tenant-a", MODEL, result.usage)
    raw = captured[1][0]["raw_usage"]
    assert not raw["complete"] and raw["attempts"][0]["cost_usd"] is None
