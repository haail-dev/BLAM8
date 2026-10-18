"""Live-inbox SSE stream (src/app/sse.py).

Drives the stream generator directly (a route-level test would hold a
never-ending response open): the hello frame carries the in-flight drafts,
published events arrive as named frames, silence produces heartbeat comments,
and closing the generator detaches the queue.

Run: python -m pytest tests/test_sse.py -q
"""
import asyncio
import json

import pytest

from app import events, sse


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    events._drafting.clear()
    yield
    events._subscribers.clear()
    events._drafting.clear()


def _parse(frame: str) -> tuple[str, dict]:
    lines = frame.strip().split("\n")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


def test_hello_first_with_current_drafting_state():
    events.begin_drafting("t1", "m1", {"from_address": "kim@x.co", "subject": "Quote"})

    async def run():
        gen = sse._stream("t1")
        name, data = _parse(await asyncio.wait_for(gen.__anext__(), 1))
        assert name == "hello"
        assert data["drafting"][0]["from_address"] == "kim@x.co"
        await gen.aclose()
    asyncio.run(run())


def test_published_events_arrive_as_named_frames():
    async def run():
        gen = sse._stream("t1")
        await asyncio.wait_for(gen.__anext__(), 1)   # hello
        events.publish("t1", {"type": "draft_ready", "log_id": "l1", "contact": "kim@x.co"})
        name, data = _parse(await asyncio.wait_for(gen.__anext__(), 1))
        assert name == "draft_ready" and data["log_id"] == "l1"
        await gen.aclose()
    asyncio.run(run())


def test_silence_yields_heartbeat_comments(monkeypatch):
    monkeypatch.setattr(sse, "_HEARTBEAT_SECONDS", 0.01)

    async def run():
        gen = sse._stream("t1")
        await asyncio.wait_for(gen.__anext__(), 1)   # hello
        assert (await asyncio.wait_for(gen.__anext__(), 1)) == ": ping\n\n"
        await gen.aclose()
    asyncio.run(run())


def test_closing_the_stream_detaches_the_listener():
    async def run():
        gen = sse._stream("t1")
        await asyncio.wait_for(gen.__anext__(), 1)   # hello — queue attached now
        assert "t1" in events._subscribers
        await gen.aclose()
        assert "t1" not in events._subscribers       # finally ran on disconnect
    asyncio.run(run())
