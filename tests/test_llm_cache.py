"""Prompt caching at the llm.create choke point
(docs/modules/module-concierge-cost-controls.md Part A).

Pins:
- ONE ephemeral breakpoint lands on the last cacheable content block of the
  final message: string content is wrapped to a text block, list content gets
  the marker on its last block only;
- conservative pass-through: SDK-object messages, empty content, non-cacheable
  block types, and blocks that already carry a marker all go through UNCHANGED;
- caller structures are never mutated (tool loops reuse their messages list);
- the LLM_PROMPT_CACHE kill-switch restores byte-identical requests;
- create() sends the transformed kwargs to the SDK.

Run: python -m pytest tests/test_llm_cache.py -q
"""
import pytest

from app import llm
from app.config import settings

_MARK = {"type": "ephemeral"}


def test_string_content_wrapped_original_untouched():
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "hi there"}]}
    out = llm._cached_kwargs(kwargs)
    assert out["messages"][-1]["content"] == [
        {"type": "text", "text": "hi there", "cache_control": _MARK}]
    assert kwargs["messages"][-1]["content"] == "hi there"   # caller unmutated
    assert out["model"] == "m"


def test_list_content_stamps_last_block_only():
    blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "a"},
              {"type": "tool_result", "tool_use_id": "t2", "content": "b"}]
    kwargs = {"messages": [{"role": "user", "content": blocks}]}
    out = llm._cached_kwargs(kwargs)
    got = out["messages"][-1]["content"]
    assert "cache_control" not in got[0]
    assert got[1]["cache_control"] == _MARK
    # the caller's block objects are untouched
    assert "cache_control" not in blocks[1]


def test_only_final_message_is_touched():
    msgs = [{"role": "user", "content": "first"},
            {"role": "assistant", "content": "middle"},
            {"role": "user", "content": "last"}]
    out = llm._cached_kwargs({"messages": msgs})
    assert out["messages"][0]["content"] == "first"
    assert out["messages"][1]["content"] == "middle"
    assert out["messages"][2]["content"][0]["cache_control"] == _MARK


def test_existing_marker_passes_through_unchanged():
    kwargs = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}]}
    assert llm._cached_kwargs(kwargs) is kwargs


def test_sdk_object_message_passes_through():
    class _Block:                      # an SDK pydantic block, not a dict
        type = "text"
    kwargs = {"messages": [{"role": "user", "content": [_Block()]}]}
    assert llm._cached_kwargs(kwargs) is kwargs
    kwargs2 = {"messages": [object()]}   # whole message isn't a dict
    assert llm._cached_kwargs(kwargs2) is kwargs2


def test_non_cacheable_block_type_passes_through():
    kwargs = {"messages": [{"role": "assistant",
                            "content": [{"type": "thinking", "thinking": "…"}]}]}
    assert llm._cached_kwargs(kwargs) is kwargs


def test_empty_shapes_pass_through():
    for kwargs in ({}, {"messages": []}, {"messages": None},
                   {"messages": [{"role": "user", "content": ""}]},
                   {"messages": [{"role": "user", "content": "   "}]},
                   {"messages": [{"role": "user", "content": []}]}):
        assert llm._cached_kwargs(kwargs) is kwargs


def test_kill_switch(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_cache", False)
    kwargs = {"messages": [{"role": "user", "content": "hi"}]}
    assert llm._cached_kwargs(kwargs) is kwargs


@pytest.mark.asyncio
async def test_create_cache_false_sends_unstamped_request():
    """One-shot bulk callers (website distill, voice-learning batches) opt out:
    their unique payload would bill as a cache WRITE at 1.25x with no read ever
    following, so cache=False must restore the byte-identical request."""
    captured = {}

    class _Messages:
        async def create(self, **kw):
            captured.update(kw)

            class _R:
                usage = None
                stop_reason = "end_turn"
            return _R()

    class _Client:
        messages = _Messages()

    await llm.create(client=_Client(), tenant_id="t1", operation="kb_website",
                     cache=False, model="m",
                     messages=[{"role": "user", "content": "big one-shot corpus"}])
    assert captured["messages"][-1]["content"] == "big one-shot corpus"
    assert "cache" not in captured   # the flag is ours, never forwarded to the SDK


@pytest.mark.asyncio
async def test_create_sends_marker_to_sdk_without_mutating_caller():
    captured = {}

    class _Messages:
        async def create(self, **kw):
            captured.update(kw)

            class _R:
                usage = None
                stop_reason = "end_turn"
            return _R()

    class _Client:
        messages = _Messages()

    caller_msgs = [{"role": "user", "content": "question"}]
    await llm.create(client=_Client(), tenant_id="t1", operation="test",
                     model="m", messages=caller_msgs)
    assert captured["messages"][-1]["content"][0]["cache_control"] == _MARK
    assert caller_msgs[-1]["content"] == "question"
