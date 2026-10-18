"""In-process live-inbox event bus (src/app/events.py).

Pins the contracts the SSE layer and pipeline rely on: per-tenant isolation,
publish never blocking/raising (full queues just drop), attach/detach symmetry,
and the in-flight "drafting…" registry (begin/end, ordering, defensive TTL).

Run: python -m pytest tests/test_events.py -q
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import events


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    events._drafting.clear()
    yield
    events._subscribers.clear()
    events._drafting.clear()


def test_publish_without_listeners_is_a_noop():
    events.publish("t1", {"type": "draft_ready"})   # nobody listening — no error


def test_events_fan_out_to_every_listener_of_that_tenant_only():
    async def run():
        a = events.attach("t1")
        b = events.attach("t1")
        other = events.attach("t2")
        events.publish("t1", {"type": "draft_ready", "log_id": "l1"})
        assert (await a.get())["log_id"] == "l1"
        assert (await b.get())["log_id"] == "l1"
        assert other.empty()
    asyncio.run(run())


def test_full_queue_drops_instead_of_blocking_or_raising():
    async def run():
        q = events.attach("t1")
        for i in range(events._QUEUE_MAX + 10):
            events.publish("t1", {"n": i})          # beyond maxsize: dropped silently
        assert q.qsize() == events._QUEUE_MAX
    asyncio.run(run())


def test_detach_stops_delivery_and_clears_the_tenant_entry():
    async def run():
        q = events.attach("t1")
        events.detach("t1", q)
        events.publish("t1", {"type": "draft_ready"})
        assert q.empty()
        assert "t1" not in events._subscribers
    asyncio.run(run())


def test_subscribe_generator_yields_and_detaches_on_close():
    async def run():
        gen = events.subscribe("t1")
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)                       # let it attach + block on get
        events.publish("t1", {"type": "draft_ready"})
        assert (await task)["type"] == "draft_ready"
        await gen.aclose()
        assert "t1" not in events._subscribers
    asyncio.run(run())


# ── the "drafting…" registry ──────────────────────────────────────────────────
def test_begin_end_drafting_registry_and_events():
    async def run():
        q = events.attach("t1")
        events.begin_drafting("t1", "m1", {"from_address": "kim@x.co", "from_name": "Kim",
                                           "subject": "Quote", "mailbox_id": "mb1",
                                           "body": "How much for a full service?"})
        now = events.drafting_now("t1")
        assert [d["key"] for d in now] == ["m1"]
        assert now[0]["from_address"] == "kim@x.co" and now[0]["mailbox_id"] == "mb1"
        # The inbound preview rides along — it exists in no table mid-draft, so
        # the registry/event is the thread's only way to show it.
        assert now[0]["subject"] == "Quote"
        assert now[0]["body"] == "How much for a full service?"
        assert events.drafting_now("t2") == []       # tenant-scoped

        events.end_drafting("t1", "m1")
        assert events.drafting_now("t1") == []

        first, second = q.get_nowait(), q.get_nowait()
        assert first["type"] == "drafting" and first["contact"] == "kim@x.co"
        assert first["subject"] == "Quote" and first["body"] == "How much for a full service?"
        assert second["type"] == "drafting_done" and second["key"] == "m1"
    asyncio.run(run())


def test_drafting_now_is_oldest_first():
    events.begin_drafting("t1", "m2", {"from_address": "b@x.co"})
    events.begin_drafting("t1", "m1", {"from_address": "a@x.co"})
    events._drafting["t1"]["m2"]["started_at"] -= timedelta(seconds=5)
    assert [d["key"] for d in events.drafting_now("t1")] == ["m2", "m1"]


def test_stuck_drafting_entries_expire():
    events.begin_drafting("t1", "m1", {"from_address": "kim@x.co"})
    events._drafting["t1"]["m1"]["started_at"] = (
        datetime.now(timezone.utc) - events._DRAFTING_TTL - timedelta(seconds=1))
    assert events.drafting_now("t1") == []           # pruned, not shown forever
    assert "t1" not in events._drafting


def test_end_drafting_for_unknown_key_is_safe():
    events.end_drafting("t1", "never-began")         # no raise, still publishes done
