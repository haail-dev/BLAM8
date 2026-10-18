"""Gmail auto-draft poller (src/app/jobs/poll_gmail.py).

Exercises one connection's sweep with the Gmail API, DB and pipeline faked:
baseline-on-first-sighting (no backlog drafting), normal drafting + watermark
advance, expired-watermark (404) re-baseline, and one bad message not blocking
the rest.

Run: python -m pytest tests/ -q   (from the project root)
"""
import asyncio

import httpx

from app.jobs import poll_gmail


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.test")
    return httpx.HTTPStatusError(str(status), request=req, response=httpx.Response(status, request=req))


class _World:
    """Fake gmail/db/pipeline surface the poller touches, with call recording."""
    def __init__(self, monkeypatch, new_ids=(), new_watermark="200", history_raises=None,
                 draft_fails=frozenset()):
        self.saved_watermarks: list[tuple] = []
        self.drafted: list[tuple] = []
        self.profile_history_id = "999"

        async def fake_token(conn, provider):
            assert provider == "google"
            return "TOKEN"

        async def fake_current_history_id(access):
            return self.profile_history_id

        async def fake_new_inbox_message_ids(access, start):
            if history_raises:
                raise history_raises
            return list(new_ids), new_watermark

        async def fake_draft(tenant_id, mid, auto_send_ok=False, mailbox_id=None):
            if mid in draft_fails:
                raise RuntimeError("boom")
            self.drafted.append((tenant_id, mid, auto_send_ok, mailbox_id))
            return f"log-{mid}"

        monkeypatch.setattr(poll_gmail.mailbox, "valid_access_token", fake_token)
        monkeypatch.setattr(poll_gmail.gmail, "current_history_id", fake_current_history_id)
        monkeypatch.setattr(poll_gmail.gmail, "new_inbox_message_ids", fake_new_inbox_message_ids)
        monkeypatch.setattr(poll_gmail.pipeline, "draft_for_message", fake_draft)
        monkeypatch.setattr(poll_gmail.db, "set_gmail_history_id",
                            lambda cid, hid: self.saved_watermarks.append((cid, hid)))


def test_first_sighting_baselines_without_drafting(monkeypatch):
    w = _World(monkeypatch, new_ids=["m1"])
    conn = {"id": "c1", "tenant_id": "t1", "gmail_history_id": None}
    handled = asyncio.run(poll_gmail.poll_connection(conn))
    assert handled == 0
    assert w.drafted == []                                  # backlog never drafted
    assert w.saved_watermarks == [("c1", "999")]            # baselined to "now"


def test_polls_draft_new_mail_and_advance_the_watermark(monkeypatch):
    w = _World(monkeypatch, new_ids=["m1", "m2"], new_watermark="150")
    conn = {"id": "c1", "tenant_id": "t1", "gmail_history_id": "100"}
    handled = asyncio.run(poll_gmail.poll_connection(conn))
    assert handled == 2
    # automatic inbound path → auto_send_ok=True (per-tenant gate applies inside the
    # pipeline), and the polled connection IS the receiving mailbox (enterprise D5).
    assert w.drafted == [("t1", "m1", True, "c1"), ("t1", "m2", True, "c1")]
    assert w.saved_watermarks == [("c1", "150")]


def test_expired_watermark_rebaselines_instead_of_crashing(monkeypatch):
    w = _World(monkeypatch, history_raises=_http_error(404))
    conn = {"id": "c1", "tenant_id": "t1", "gmail_history_id": "1"}
    handled = asyncio.run(poll_gmail.poll_connection(conn))
    assert handled == 0
    assert w.saved_watermarks == [("c1", "999")]


def test_one_bad_message_does_not_block_the_rest(monkeypatch):
    w = _World(monkeypatch, new_ids=["bad", "m2"], new_watermark="150", draft_fails={"bad"})
    conn = {"id": "c1", "tenant_id": "t1", "gmail_history_id": "100"}
    handled = asyncio.run(poll_gmail.poll_connection(conn))
    assert handled == 1
    assert w.drafted == [("t1", "m2", True, "c1")]
    assert w.saved_watermarks == [("c1", "150")]


def test_poll_once_isolates_a_failing_connection(monkeypatch):
    from app import sync_backoff
    sync_backoff._state.clear()
    calls = []

    async def fake_poll_connection(conn):
        calls.append(conn["id"])
        if conn["id"] == "c1":
            raise RuntimeError("revoked token")
        return 3

    monkeypatch.setattr(poll_gmail.db, "list_google_connections",
                        lambda: [{"id": "c1"}, {"id": "c2"}])
    monkeypatch.setattr(poll_gmail, "poll_connection", fake_poll_connection)
    assert asyncio.run(poll_gmail.poll_once()) == 3
    assert calls == ["c1", "c2"]   # c2 still polled after c1 blew up
    assert sync_backoff.should_skip(("gmail", "c1")) is True    # c1 now backed off
    assert sync_backoff.should_skip(("gmail", "c2")) is False   # c2 healthy
