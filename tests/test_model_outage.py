"""A model outage queues a failed draft, never loses the email (live
2026-09-26: the API credit ran out mid-draft; the exception aborted the
pipeline before the row was written, so the owner saw a spinner that vanished
and "Opening…" for ever).

Run: python -m pytest tests/test_model_outage.py -q
"""
import asyncio

import anthropic
import httpx

from app import pipeline
from tests.test_push import EMAIL, Env


def _credit_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req, json={"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Anthropic API."}})
    return anthropic.BadRequestError("credit", response=resp, body=None)


def test_an_outage_lands_as_a_failed_draft_the_owner_can_see(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    rows = []
    real_insert = pipeline.db.insert_email_log

    def spy(*a, **k):
        rows.append((a, k))
        return real_insert(*a, **k)
    monkeypatch.setattr(pipeline.db, "insert_email_log", spy)

    async def boom(cfg, email, **k):
        raise _credit_error()
    monkeypatch.setattr(pipeline.agent, "draft_reply", boom)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert rows, "the email must be written even though the model failed"
    a, k = rows[0]
    assert k["status"] == "pending" and k["category"] == "needs_reply"
    assert k["action_summary"].startswith("Draft failed")
    assert env.sent == []


def test_anything_else_still_raises(monkeypatch):
    Env(monkeypatch, auto_send=True)

    async def bug(cfg, email, **k):
        raise KeyError("a real bug")
    monkeypatch.setattr(pipeline.agent, "draft_reply", bug)
    try:
        asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    except KeyError:
        return
    raise AssertionError("a bug must not be swallowed as an outage")
