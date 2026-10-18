"""src/app/llm.py — the single Anthropic choke point (full-transcript observability).

Every messages.create in the app flows through llm.create(client=..., ...), which
writes the EXACT request payload and raw response to llm_calls (best-effort — a
logging failure never breaks the call). Modes: full / meta / off. Base64 image
data is the one redaction (a size stub), and redaction must never mutate the
caller's kwargs.

Run: python -m pytest tests/test_llm_log.py -q
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import agent, db, llm
from app.config import settings


def usage(n=10):
    return SimpleNamespace(input_tokens=n, output_tokens=n * 2,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeMessage:
    """SDK Message stand-in with the pydantic surface llm._jsonable relies on."""

    def __init__(self, text="hello", stop_reason="end_turn", model="fake-model", n=10):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop_reason
        self.model = model
        self.usage = usage(n)
        self._text = text

    def model_dump(self, mode=None):
        return {"content": [{"type": "text", "text": self._text}],
                "stop_reason": self.stop_reason, "model": self.model}


class FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


@pytest.fixture
def rows(monkeypatch):
    """Recorder for db.insert_llm_call (overrides the conftest no-op stub)."""
    captured = []
    monkeypatch.setattr(db, "insert_llm_call", captured.append)
    monkeypatch.setattr(settings, "llm_log_mode", "full")
    return captured


# ── the wrapper itself ────────────────────────────────────────────────────────
def test_create_passes_through_and_logs_full_row(rows):
    fake = FakeClient([FakeMessage("hi there", n=7)])
    msg = asyncio.run(llm.create(
        client=fake, tenant_id="t-1", operation="draft",
        model="fake-model", max_tokens=100, system="SYSTEM PROMPT",
        messages=[{"role": "user", "content": "question"}],
    ))
    # Drop-in: the SDK sees exactly the model kwargs, none of the logging ones.
    # The one deliberate transformation is the prompt-cache breakpoint stamped
    # on the final message (test_llm_cache.py pins its mechanics).
    # (2026-09-06: the system block carries a second breakpoint so the stable
    # tools→system prefix stays cached while the history churns.)
    assert fake.calls == [{"model": "fake-model", "max_tokens": 100,
                           "system": [{"type": "text", "text": "SYSTEM PROMPT",
                                       "cache_control": {"type": "ephemeral"}}],
                           "messages": [{"role": "user", "content": [
                               {"type": "text", "text": "question",
                                "cache_control": {"type": "ephemeral"}}]}]}]
    assert msg.stop_reason == "end_turn"
    (row,) = rows
    assert row["tenant_id"] == "t-1"
    assert row["operation"] == "draft"
    assert row["module"] == "email"
    assert row["model"] == "fake-model"
    assert row["stop_reason"] == "end_turn"
    assert row["input_tokens"] == 7 and row["output_tokens"] == 14
    # Every word, both directions (logged as sent — incl. the cache marker).
    assert row["request"]["system"] == [{"type": "text", "text": "SYSTEM PROMPT",
                                         "cache_control": {"type": "ephemeral"}}]
    assert row["request"]["messages"][0]["content"][0]["text"] == "question"
    assert row["response"]["content"][0]["text"] == "hi there"
    assert row["duration_ms"] >= 0


def test_meta_mode_drops_payloads_keeps_envelope(rows, monkeypatch):
    monkeypatch.setattr(settings, "llm_log_mode", "meta")
    asyncio.run(llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                           operation="draft", model="fake-model",
                           messages=[{"role": "user", "content": "q"}]))
    (row,) = rows
    assert "request" not in row and "response" not in row
    assert row["model"] == "fake-model" and row["input_tokens"] == 10


def test_off_mode_writes_nothing(rows, monkeypatch):
    monkeypatch.setattr(settings, "llm_log_mode", "off")
    asyncio.run(llm.create(client=FakeClient([FakeMessage()]), operation="draft",
                           model="fake-model", messages=[]))
    assert rows == []


def test_logging_failure_never_breaks_the_call(monkeypatch):
    def boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "insert_llm_call", boom)
    monkeypatch.setattr(settings, "llm_log_mode", "full")
    msg = asyncio.run(llm.create(client=FakeClient([FakeMessage("still works")]),
                                 operation="draft", model="fake-model", messages=[]))
    assert msg.content[0].text == "still works"


def test_api_error_is_logged_then_reraised(rows):
    fake = FakeClient(error=ValueError("api exploded"))
    with pytest.raises(ValueError):
        asyncio.run(llm.create(client=fake, tenant_id="t-1", operation="draft",
                               model="fake-model", messages=[{"role": "user", "content": "q"}]))
    (row,) = rows
    assert row["stop_reason"] == "api_error"
    assert "api exploded" in row["response"]["error"]
    # The request still shows what went to the API (cache-stamped form).
    assert row["request"]["messages"][0]["content"][0]["text"] == "q"


# ── redaction ─────────────────────────────────────────────────────────────────
def test_base64_image_redacted_without_mutating_caller_kwargs(rows):
    image_block = {"type": "image",
                   "source": {"type": "base64", "media_type": "image/jpeg",
                              "data": "A" * 5000}}
    messages = [{"role": "user", "content": [image_block,
                                             {"type": "text", "text": "compare these"}]}]
    asyncio.run(llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                           operation="condition_diff", model="fake-model",
                           messages=messages))
    (row,) = rows
    logged = row["request"]["messages"][0]["content"]
    assert logged[0]["source"]["data"] == "<5000 base64 chars redacted>"
    assert logged[1]["text"] == "compare these"          # words survive
    assert image_block["source"]["data"] == "A" * 5000   # caller's dict untouched


def test_sdk_content_blocks_in_messages_serialize(rows):
    """Tool loops replay assistant turns whose content is SDK objects (pydantic);
    the log must serialize them rather than crash or drop the row."""
    class Block:
        def model_dump(self, mode=None):
            return {"type": "tool_use", "id": "tu-1", "name": "check_availability",
                    "input": {"day": "friday"}}
    messages = [{"role": "assistant", "content": [Block()]}]
    asyncio.run(llm.create(client=FakeClient([FakeMessage()]), operation="draft",
                           model="fake-model", messages=messages))
    (row,) = rows
    assert row["request"]["messages"][0]["content"][0]["name"] == "check_availability"


# ── through the real callers ──────────────────────────────────────────────────
def test_draft_reply_logs_every_loop_call(rows, monkeypatch):
    responses = [
        SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="tu-1",
                                                 name="check_availability",
                                                 input={"day": "friday"})],
                        stop_reason="tool_use", usage=usage(5), model="fake-model"),
        SimpleNamespace(content=[SimpleNamespace(type="text",
                                                 text=json.dumps({"reply": "Booked."}))],
                        stop_reason="end_turn", usage=usage(5), model="fake-model"),
    ]
    fake = FakeClient(responses)
    monkeypatch.setattr(agent, "_client", fake)
    tools = [{"name": "check_availability", "description": "x",
              "input_schema": {"type": "object"}}]
    result = asyncio.run(agent.draft_reply(
        {}, {"subject": "Quote", "body": "Friday?"},
        tools=tools, tool_executor=lambda name, args: {"ok": True},
        tenant_id="t-9",
    ))
    assert result.text == "Booked."
    assert len(rows) == 2                      # one row per API call in the loop
    assert all(r["tenant_id"] == "t-9" and r["operation"] == "draft" for r in rows)
    # The second call's request carries the fed-back tool_result — every word.
    round2 = json.dumps(rows[1]["request"]["messages"])
    assert "tool_result" in round2 and "check_availability" in json.dumps(rows[1]["request"])


def test_standalone_calls_get_distinct_request_ids(rows):
    for _ in range(2):
        asyncio.run(llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                               operation="draft", model="fake-model", messages=[]))
    a, b = rows
    assert a["request_id"] and b["request_id"] and a["request_id"] != b["request_id"]


def test_request_scope_groups_loop_calls_and_nesting_keeps_outer_id(rows):
    async def nested():
        # Mirrors a specialist entry point: its own scope must be a no-op here.
        with llm.request_scope():
            await llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                             operation="tms_handoff", model="fake-model", messages=[])

    async def exchange():
        with llm.request_scope():
            await llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                             operation="draft", model="fake-model", messages=[])
            await nested()
            await llm.create(client=FakeClient([FakeMessage()]), tenant_id="t-1",
                             operation="draft", model="fake-model", messages=[])

    asyncio.run(exchange())
    assert len(rows) == 3
    assert len({r["request_id"] for r in rows}) == 1   # one exchange, one id
    # A later exchange gets a fresh id — the scope reset on exit.
    asyncio.run(exchange())
    assert rows[3]["request_id"] != rows[0]["request_id"]


def test_draft_reply_loop_rows_share_one_request_id(rows, monkeypatch):
    responses = [
        SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="tu-1",
                                                 name="check_availability",
                                                 input={"day": "friday"})],
                        stop_reason="tool_use", usage=usage(5), model="fake-model"),
        SimpleNamespace(content=[SimpleNamespace(type="text",
                                                 text=json.dumps({"reply": "Booked."}))],
                        stop_reason="end_turn", usage=usage(5), model="fake-model"),
    ]
    monkeypatch.setattr(agent, "_client", FakeClient(responses))
    tools = [{"name": "check_availability", "description": "x",
              "input_schema": {"type": "object"}}]
    asyncio.run(agent.draft_reply(
        {}, {"subject": "Quote", "body": "Friday?"},
        tools=tools, tool_executor=lambda name, args: {"ok": True},
        tenant_id="t-9",
    ))
    assert len(rows) == 2
    assert rows[0]["request_id"] == rows[1]["request_id"]


def test_compose_outbound_logs_with_attribution(rows, monkeypatch):
    fake = FakeClient([SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"message": "Reminder!"}))],
        stop_reason="end_turn", usage=usage(3), model="fake-model")])
    monkeypatch.setattr(agent, "_client", fake)
    result = asyncio.run(agent.compose_outbound(
        {}, "day-before reminder", "Booking BLAM-1 tomorrow 9am",
        tenant_id="t-2", log_operation="reminder", log_module="scheduling"))
    assert result.text == "Reminder!"
    (row,) = rows
    assert (row["tenant_id"], row["operation"], row["module"]) == ("t-2", "reminder", "scheduling")
    system = row["request"]["system"]
    system_text = system if isinstance(system, str) else "".join(b.get("text", "") for b in system)
    assert "BLAM-1" in system_text
