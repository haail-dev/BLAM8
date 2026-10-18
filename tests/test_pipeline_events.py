"""Live-inbox events out of the drafting pipeline (src/app/pipeline.py → src/app/events.py).

Pins the "instant chat" contract: the in-flight registry covers exactly the
arrival→insert window (visible mid-draft, cleared after — even on a crash),
listeners see drafting → drafting_done → draft_ready in that order, auto-send
adds draft_sent, and a pre-guard rejection (ignored sender) publishes nothing.

Run: python -m pytest tests/test_pipeline_events.py -q
"""
import asyncio

import pytest

from app import agent, events, pipeline


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    events._drafting.clear()
    pipeline._drafts_in_flight.clear()
    yield
    events._subscribers.clear()
    events._drafting.clear()
    pipeline._drafts_in_flight.clear()


class Env:
    """Patches draft_for_email's collaborators (the no-modules slice of the
    test_scheduling_pipeline Env) and snapshots the drafting registry from
    INSIDE the model call — the only moment the typing bubble should exist."""

    def __init__(self, monkeypatch, *, auto_send=False, draft_raises=False, ignored=False):
        self.during_draft: list = []
        self.sent = []

        async def fake_draft(cfg, email, **kw):
            self.during_draft = events.drafting_now("t1")
            if draft_raises:
                raise RuntimeError("model down")
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_send(*a, **k):
            self.sent.append(a)

        async def fake_retrieve(*a, **k):
            return []

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.mailbox, "send_reply", fake_send)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: ignored)
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"mailbox": "me@biz.co"})
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"auto_send": auto_send})
        monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: {"me@biz.co"})
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                            lambda *a, **k: {"activities": 0, "contact_id": None, "company_id": None})
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "insert_email_log", lambda *a, **k: ("log-1", True))
        monkeypatch.setattr(pipeline.db, "mark_email_log_sent", lambda *a, **k: None)


EMAIL = {"from_address": "ops@acme.co.uk", "from_name": "Ops", "subject": "Quote",
         "body": "Friday?", "thread_id": "th-9"}


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_drafting_then_done_then_ready_and_registry_covers_the_model_call(monkeypatch):
    env = Env(monkeypatch)
    q = events.attach("t1")
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    # The typing bubble existed exactly while the model ran…
    assert env.during_draft and env.during_draft[0]["from_address"] == "ops@acme.co.uk"
    assert env.during_draft[0]["key"] == "m1"
    # …carrying the inbound's preview (subject + body) — the thread renders a
    # provisional incoming bubble from it, since the row isn't inserted yet.
    assert env.during_draft[0]["subject"] == "Quote"
    assert env.during_draft[0]["body"] == "Friday?"
    # …and is gone once the row is stored.
    assert events.drafting_now("t1") == []
    drained = _drain(q)
    types = [e["type"] for e in drained if e["type"] != "drafting_step"]   # the live ticker narrates, variably
    assert types == ["drafting", "drafting_done", "draft_ready"]
    assert drained[0]["subject"] == "Quote" and drained[0]["body"] == "Friday?"


def test_draft_ready_carries_the_row_identity(monkeypatch):
    Env(monkeypatch)
    q = events.attach("t1")
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", mailbox_id="mb1"))
    ready = [e for e in _drain(q) if e["type"] == "draft_ready"][0]
    assert ready["log_id"] == "log-1" and ready["contact"] == "ops@acme.co.uk"
    assert ready["mailbox_id"] == "mb1"


def test_the_pipeline_keeps_the_provider_body_readable_beyond_the_old_preview(monkeypatch):
    env = Env(monkeypatch)
    body = "Details for the visit. " * 60 + "Please also read this final sentence."
    asyncio.run(pipeline.draft_for_email("t1", {**EMAIL, "body": body}, source_message_id="m1"))
    assert env.during_draft[0]["body"] == body


def test_a_crashed_draft_still_clears_the_typing_bubble(monkeypatch):
    Env(monkeypatch, draft_raises=True)
    q = events.attach("t1")
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert events.drafting_now("t1") == []           # the finally ran
    assert pipeline._drafts_in_flight == set()       # …and released the claim
    types = [e["type"] for e in _drain(q) if e["type"] != "drafting_step"]
    assert types == ["drafting", "drafting_done"]    # no draft_ready for a failure


def test_auto_send_publishes_draft_sent(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    q = events.attach("t1")
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.sent                                   # the reply actually went out
    types = [e["type"] for e in _drain(q) if e["type"] != "drafting_step"]
    assert types == ["drafting", "drafting_done", "draft_ready", "draft_sent"]


def test_guard_rejections_publish_nothing(monkeypatch):
    Env(monkeypatch, ignored=True)
    q = events.attach("t1")
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert _drain(q) == []                            # never began drafting


def test_concurrent_twin_inbound_drafts_exactly_once(monkeypatch):
    """The webhook/poller race (prod 2026-08-31: twin request_ids per inbound).
    Both pick up the same unseen message; the second must walk away at the
    in-flight claim — before retrieval or the model — leaving one draft loop,
    one insert, and a released claim."""
    Env(monkeypatch)
    drafted, inserted = [], []

    async def slow_draft(cfg, email, **kw):
        drafted.append(1)
        await asyncio.sleep(0)                        # a real suspension, so the twin gets in
        return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

    monkeypatch.setattr(pipeline.agent, "draft_reply", slow_draft)
    monkeypatch.setattr(pipeline.db, "insert_email_log",
                        lambda *a, **k: (inserted.append(1), ("log-1", True))[1])

    async def race():
        return await asyncio.gather(
            pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"),
            pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))

    results = asyncio.run(race())
    assert results == ["log-1", None]                    # winner's row + the twin's None
    assert drafted == [1] and inserted == [1]            # one loop, one row — not two
    assert pipeline._drafts_in_flight == set()           # claim released for the next inbound


def test_claim_is_per_message_not_per_tenant(monkeypatch):
    """Two DIFFERENT inbounds for one tenant must draft concurrently — the
    claim keys on the message, not the tenant."""
    Env(monkeypatch)
    drafted = []

    async def slow_draft(cfg, email, **kw):
        drafted.append(1)
        await asyncio.sleep(0)
        return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

    monkeypatch.setattr(pipeline.agent, "draft_reply", slow_draft)

    async def race():
        return await asyncio.gather(
            pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"),
            pipeline.draft_for_email("t1", EMAIL, source_message_id="m2"))

    assert asyncio.run(race()) == ["log-1", "log-1"]
    assert drafted == [1, 1]
