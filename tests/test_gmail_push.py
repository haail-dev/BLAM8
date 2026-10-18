"""Gmail push (users.watch → Pub/Sub → POST /webhook/gmail).

Route-level via TestClient (public endpoint, secret in the URL): the
constant-time token gate, envelope decoding, connection lookup → background
poll scheduling, and graceful ACKs for malformed/unknown pushes. Plus the
push-triggered poll's backoff sharing and the renewal sweep's Gmail branch
(arm missing watches, renew expiring ones, skip healthy ones, off without a
topic).

Run: python -m pytest tests/test_gmail_push.py -q
"""
import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import sync_backoff, webhook
from app.jobs import renew_subscriptions as renew
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(webhook.settings, "gmail_pubsub_token", "sekret")
    yield TestClient(app)   # no `with`: startup events must not run


def _envelope(address="pilot@gmail.com", history_id=42) -> dict:
    data = json.dumps({"emailAddress": address, "historyId": history_id})
    return {"message": {"data": base64.b64encode(data.encode()).decode(),
                        "messageId": "pm-1"}, "subscription": "projects/p/subscriptions/s"}


def test_wrong_token_is_rejected(client, monkeypatch):
    scheduled = []
    monkeypatch.setattr(webhook, "_schedule_poll", scheduled.append)
    assert client.post("/webhook/gmail?token=nope", json=_envelope()).status_code == 403
    assert scheduled == []


def test_unconfigured_token_rejects_even_an_empty_match(client, monkeypatch):
    monkeypatch.setattr(webhook.settings, "gmail_pubsub_token", "")
    assert client.post("/webhook/gmail?token=", json=_envelope()).status_code == 403


def test_valid_push_schedules_a_poll_for_that_connection(client, monkeypatch):
    conn = {"id": "c1", "tenant_id": "t1", "gmail_history_id": "10"}
    looked_up, scheduled = [], []
    monkeypatch.setattr(webhook.db, "get_google_connection_by_address",
                        lambda addr: (looked_up.append(addr), conn)[1])
    monkeypatch.setattr(webhook, "_schedule_poll", scheduled.append)
    r = client.post("/webhook/gmail?token=sekret", json=_envelope(address="Pilot@Gmail.com"))
    assert r.status_code == 204
    assert looked_up == ["Pilot@Gmail.com"]   # lookup lowercases internally
    assert scheduled == [conn]


def test_unknown_address_is_acked_without_scheduling(client, monkeypatch):
    scheduled = []
    monkeypatch.setattr(webhook.db, "get_google_connection_by_address", lambda addr: None)
    monkeypatch.setattr(webhook, "_schedule_poll", scheduled.append)
    assert client.post("/webhook/gmail?token=sekret", json=_envelope()).status_code == 204
    assert scheduled == []


def test_malformed_envelope_is_acked_never_retried(client, monkeypatch):
    monkeypatch.setattr(webhook, "_schedule_poll", lambda conn: pytest.fail("scheduled"))
    assert client.post("/webhook/gmail?token=sekret", json={"message": {"data": "!!"}}).status_code == 204
    assert client.post("/webhook/gmail?token=sekret", content=b"not json").status_code == 204


# ── the push-triggered poll shares the poller's backoff ───────────────────────
def test_pushed_poll_success_and_failure_share_the_poll_backoff(monkeypatch):
    sync_backoff._state.clear()
    conn = {"id": "c1", "tenant_id": "t1"}

    async def ok(c):
        return 2
    monkeypatch.setattr(webhook.poll_gmail, "poll_connection", ok)
    asyncio.run(webhook._poll_pushed_connection(conn))
    assert sync_backoff.should_skip(("gmail", "c1")) is False

    async def boom(c):
        raise RuntimeError("revoked")
    monkeypatch.setattr(webhook.poll_gmail, "poll_connection", boom)
    asyncio.run(webhook._poll_pushed_connection(conn))
    assert sync_backoff.should_skip(("gmail", "c1")) is True   # armed once

    # While backed off, a redelivery storm never reaches the API again.
    called = []

    async def counting(c):
        called.append(c)
        return 0
    monkeypatch.setattr(webhook.poll_gmail, "poll_connection", counting)
    asyncio.run(webhook._poll_pushed_connection(conn))
    assert called == []
    sync_backoff._state.clear()


# ── the renewal sweep's Gmail branch ──────────────────────────────────────────
class _RenewEnv:
    def __init__(self, monkeypatch, conns, topic="projects/p/topics/gmail"):
        self.watched, self.stamped = [], []

        async def fake_token(conn, provider):
            assert provider == "google"
            return "TOKEN"

        async def fake_watch(access, topic_arg):
            self.watched.append(topic_arg)
            return {"historyId": "9", "expiration": "1790000000000"}   # epoch ms

        monkeypatch.setattr(renew.settings, "gmail_pubsub_topic", topic)
        monkeypatch.setattr(renew, "list_all_connections", lambda: conns)
        monkeypatch.setattr(renew.mailbox, "connection_provider", lambda conn, tid: "google")
        monkeypatch.setattr(renew.mailbox, "valid_access_token", fake_token)
        monkeypatch.setattr(renew.gmail, "watch_mailbox", fake_watch)
        monkeypatch.setattr(renew, "set_gmail_watch_expiry",
                            lambda cid, iso: self.stamped.append((cid, iso)))


def _gconn(**over):
    return {"id": "c1", "tenant_id": "t1", "refresh_token": "rt",
            "access_token": "at", "gmail_watch_expiry": None, **over}


def test_renew_arms_a_missing_watch_and_stamps_expiry(monkeypatch):
    env = _RenewEnv(monkeypatch, [_gconn()])
    res = asyncio.run(renew._run())
    assert env.watched == ["projects/p/topics/gmail"]
    assert env.stamped and env.stamped[0][0] == "c1"
    assert env.stamped[0][1].startswith("2026-")     # epoch-ms converted to iso utc
    assert res["renewed"] == 1


def test_renew_skips_a_healthy_watch(monkeypatch):
    env = _RenewEnv(monkeypatch, [_gconn(gmail_watch_expiry="2099-01-01T00:00:00+00:00")])
    res = asyncio.run(renew._run())
    assert env.watched == [] and res["skipped"] == 1


def test_renew_rearms_a_watch_inside_the_24h_window(monkeypatch):
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    env = _RenewEnv(monkeypatch, [_gconn(gmail_watch_expiry=soon)])
    asyncio.run(renew._run())
    assert env.watched == ["projects/p/topics/gmail"]


def test_no_topic_means_gmail_is_left_alone(monkeypatch):
    env = _RenewEnv(monkeypatch, [_gconn()], topic="")
    res = asyncio.run(renew._run())
    assert env.watched == [] and res["renewed"] == 0
