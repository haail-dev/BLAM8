import pathlib
import shutil
import subprocess

import pytest


def test_deferred_browser_lifecycle_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser behavioral regressions")
    script = pathlib.Path(__file__).parent.joinpath("replays", "voice_lifecycle_replay.mjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_voice_model_loop_stops_after_cancellation_and_preserves_usage(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import concierge

    cancelled = False
    calls = []
    message = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="lookup", input={}, id="one")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0), model="test", stop_reason="tool_use")

    async def provider(**kw):
        calls.append("model")
        return message

    async def executor(*a):
        nonlocal cancelled
        cancelled = True
        return {"ok": True}

    async def delta(*a):
        pass

    monkeypatch.setattr(concierge, "client", lambda: object())
    monkeypatch.setattr(concierge.llm, "stream", provider)
    result = asyncio.run(concierge.answer_business(
        "Look up", [], tools=[], tool_executor=executor, on_delta=delta,
        voice_mode=True, cancelled=lambda: cancelled))
    assert calls == ["model"]
    assert result.action is None and result.reply == ""
    assert result.usage["input_tokens"] == 3 and result.usage["output_tokens"] == 2


@pytest.mark.parametrize("cancel_at", ["stream", "correction"])
def test_real_voice_cancellation_blocks_correction_and_repair(monkeypatch, cancel_at):
    import asyncio
    from types import SimpleNamespace
    from app import concierge, concierge_booking_claims

    cancelled = False
    calls = []
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"reply":"The booking (","links":[],"action":null}')],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0), model="test", stop_reason="end_turn")

    async def stream(**kw):
        nonlocal cancelled
        calls.append("stream")
        cancelled = cancel_at == "stream"
        return message

    async def correction(**kw):
        nonlocal cancelled
        calls.append("correction")
        cancelled = True
        return message

    async def repair(*a):
        raise AssertionError("Cancelled turn entered repair")

    async def delta(*a):
        pass

    monkeypatch.setattr(concierge, "client", lambda: object())
    monkeypatch.setattr(concierge.llm, "stream", stream)
    monkeypatch.setattr(concierge.llm, "create", correction)
    monkeypatch.setattr(concierge_booking_claims, "repair_action", repair)
    result = asyncio.run(concierge.answer_business(
        "Look up", [], tools=[], tool_executor=None, on_delta=delta,
        voice_mode=True, cancelled=lambda: cancelled))
    assert calls == (["stream"] if cancel_at == "stream" else ["stream", "correction"])
    assert result.action is None and result.reply == ""
    assert result.usage["input_tokens"] == (3 if cancel_at == "stream" else 6)
