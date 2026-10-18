"""Provider-native replies (the threaded-context fix).

Covers the pure MIME assembly for Gmail threaded replies and the dispatch logic in
mailbox.send_reply: native reply when the inbound provider message is known
(Graph /reply action / Gmail threaded send), plain 'Re:' send as the fallback —
including the original-message-deleted (404) path. Network and DB are faked.

Run: python -m pytest tests/ -q   (from the project root)
"""
import asyncio

import httpx
import pytest

from app import gmail
from app import mailbox


# ── Gmail MIME assembly ──────────────────────────────────────────────────────
def test_build_reply_mime_sets_threading_headers():
    msg = gmail.build_reply_mime(
        "dave@x.com", "Re: Quote", "Hi Dave", "<orig@mail.gmail.com>", "<root@x.com> <mid@x.com>"
    )
    assert msg["To"] == "dave@x.com"
    assert msg["Subject"] == "Re: Quote"
    assert msg["In-Reply-To"] == "<orig@mail.gmail.com>"
    # RFC 2822: original References + the message being answered, in order
    assert msg["References"] == "<root@x.com> <mid@x.com> <orig@mail.gmail.com>"
    assert "Hi Dave" in msg.get_content()


def test_build_reply_mime_first_reply_references_is_just_the_original():
    msg = gmail.build_reply_mime("dave@x.com", "Re: Quote", "Hi", "<orig@x.com>", "")
    assert msg["References"] == "<orig@x.com>"


def test_build_reply_mime_without_message_id_omits_threading_headers():
    msg = gmail.build_reply_mime("dave@x.com", "Re: Quote", "Hi", "", "")
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_gmail_reply_subject_matches_mailbox_semantics():
    assert gmail._reply_subject("Quote") == "Re: Quote"
    assert gmail._reply_subject("Re: Quote") == "Re: Quote"
    assert gmail._reply_subject("RE: Quote") == "RE: Quote"
    assert gmail._reply_subject("") == "Re:"
    assert gmail._reply_subject(None) == "Re:"


# ── mailbox.send_reply dispatch ──────────────────────────────────────────────
def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.test")
    return httpx.HTTPStatusError(str(status), request=req, response=httpx.Response(status, request=req))


class _Recorder:
    """Records calls to the faked send functions."""
    def __init__(self):
        self.graph_posts: list[tuple] = []
        self.gmail_replies: list[tuple] = []
        self.plain_sends: list[tuple] = []


def _wire(monkeypatch, recorder: _Recorder, provider: str, native_raises=None,
          send_as: str | None = None):
    async def fake_access(_tenant_id, _mailbox_id=None):
        return "TOKEN", provider, {"send_as_address": send_as}

    async def fake_graph_post(access, path, json=None, extra_headers=None):
        if native_raises:
            raise native_raises
        recorder.graph_posts.append((access, path, json))
        return {}

    async def fake_gmail_reply(access, to, message_id, body, send_as=None):
        if native_raises:
            raise native_raises
        recorder.gmail_replies.append((access, to, message_id, body, send_as))

    async def fake_send_mail(tenant_id, to, subject, body, mailbox_id=None):
        recorder.plain_sends.append((tenant_id, to, subject, body))

    monkeypatch.setattr(mailbox, "_access", fake_access)
    monkeypatch.setattr(mailbox.graph, "graph_post", fake_graph_post)
    monkeypatch.setattr(mailbox.gmail, "send_reply", fake_gmail_reply)
    monkeypatch.setattr(mailbox, "send_mail", fake_send_mail)


def test_send_reply_microsoft_uses_graph_reply_action(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "microsoft")
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi Dave", source_message_id="MSG1"))
    assert rec.graph_posts == [
        ("TOKEN", "/me/messages/MSG1/reply",
         {"message": {"body": {"contentType": "Text", "content": "Hi Dave"}}}),
    ]
    assert rec.plain_sends == []   # native reply — no fresh-compose fallback


def test_send_reply_google_uses_threaded_gmail_send(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "google")
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi Dave", source_message_id="MSG1"))
    assert rec.gmail_replies == [("TOKEN", "dave@x.com", "MSG1", "Hi Dave", None)]
    assert rec.plain_sends == []


def test_send_reply_without_source_message_falls_back_to_plain_send(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "microsoft")
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi Dave"))
    assert rec.graph_posts == []
    assert rec.plain_sends == [("t1", "dave@x.com", "Re: Quote", "Hi Dave")]


def test_send_reply_falls_back_when_original_message_is_gone(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "microsoft", native_raises=_http_error(404))
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi", source_message_id="GONE"))
    assert rec.plain_sends == [("t1", "dave@x.com", "Re: Quote", "Hi")]


def test_send_reply_reraises_non_404_errors(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "google", native_raises=_http_error(403))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi", source_message_id="M"))
    assert rec.plain_sends == []   # a real failure must surface, not silently degrade


# ── send-as identity (email_connections.send_as_address) ─────────────────────
def test_send_reply_google_passes_the_mailbox_send_as(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "google", send_as="hello@blam8.com")
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi", source_message_id="MSG1"))
    assert rec.gmail_replies == [("TOKEN", "dave@x.com", "MSG1", "Hi", "hello@blam8.com")]


def test_send_reply_microsoft_sets_graph_from_when_send_as(monkeypatch):
    rec = _Recorder()
    _wire(monkeypatch, rec, "microsoft", send_as="hello@blam8.com")
    asyncio.run(mailbox.send_reply("t1", "dave@x.com", "Quote", "Hi", source_message_id="MSG1"))
    assert rec.graph_posts == [
        ("TOKEN", "/me/messages/MSG1/reply",
         {"message": {"body": {"contentType": "Text", "content": "Hi"},
                      "from": {"emailAddress": {"address": "hello@blam8.com"}}}}),
    ]


def test_send_mail_google_sets_from_header_when_send_as(monkeypatch):
    sent = []

    async def fake_send_raw(access, msg, thread_id=None):
        sent.append(msg)
    monkeypatch.setattr(gmail, "_send_raw", fake_send_raw)

    asyncio.run(gmail.send_mail("TOK", "dave@x.com", "Quote", "Hi", send_as="hello@blam8.com"))
    asyncio.run(gmail.send_mail("TOK", "dave@x.com", "Quote", "Hi"))
    assert sent[0]["From"] == "hello@blam8.com"
    assert sent[1]["From"] is None   # unset → Gmail fills the authenticated user


def test_gmail_send_reply_sets_from_header_when_send_as(monkeypatch):
    sent = []

    async def fake_get(access, path, params=None):
        return {"threadId": "T1", "payload": {"headers": [
            {"name": "Message-ID", "value": "<orig@x.com>"},
            {"name": "Subject", "value": "Quote"},
        ]}}

    async def fake_send_raw(access, msg, thread_id=None):
        sent.append((msg, thread_id))
    monkeypatch.setattr(gmail, "_get", fake_get)
    monkeypatch.setattr(gmail, "_send_raw", fake_send_raw)

    asyncio.run(gmail.send_reply("TOK", "dave@x.com", "MSG1", "Hi", send_as="hello@blam8.com"))
    msg, thread_id = sent[0]
    assert msg["From"] == "hello@blam8.com"
    assert thread_id == "T1"
    assert msg["In-Reply-To"] == "<orig@x.com>"   # threading intact alongside From


def test_send_as_blank_and_missing_column_mean_none():
    assert mailbox._send_as({"send_as_address": "  "}) is None
    assert mailbox._send_as({}) is None
    assert mailbox._send_as(None) is None
    assert mailbox._send_as({"send_as_address": "hello@blam8.com"}) == "hello@blam8.com"
