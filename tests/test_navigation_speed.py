"""The navigation hot path — what makes /app pages open now rather than in a
second or two (Kris 2026-09-01: "it takes actual seconds for the Inbox to open").

These are behaviour tests for the SHAPE of the reads, not timings: a timing
assertion would be flaky, but "this page must not ask Supabase for every email
body it has ever stored" and "the mailbox is not re-scanned on every render"
are exactly the properties that regress silently.

Run: python -m pytest tests/test_navigation_speed.py -q
"""
import asyncio

import pytest

from app import db, events, inbox_labels, navcache


# ── list_conversations: the body payload ────────────────────────────────────
class _Recorder:
    """A fake Supabase client that records what each table was asked for."""

    def __init__(self, tables):
        self.tables = tables
        self.selects = []          # (table, columns) in call order

    def table(self, name):
        return _RecorderTable(self, name)


class _RecorderTable:
    def __init__(self, rec, name):
        self.rec, self.name, self.rows = rec, name, rec.tables.get(name, [])
        self._ids = None

    def select(self, cols, *_a, **_k):
        self.rec.selects.append((self.name, cols))
        return self

    def eq(self, *_a): return self
    def gte(self, *_a): return self
    def order(self, *_a, **_k): return self
    def range(self, *_a): return self

    def in_(self, col, values):
        if col == "id":
            self._ids = set(values)
        return self

    def execute(self):
        rows = self.rows
        if self._ids is not None:
            rows = [r for r in rows if r.get("id") in self._ids]
        return type("R", (), {"data": rows})()


def _log_row(rid, contact, body, **kw):
    return {"id": rid, "from_address": contact, "subject": "s", "body_in": body,
            "status": "pending", "created_at": f"2026-08-{rid[-2:]}T09:00:00+00:00", **kw}


def test_the_list_query_does_not_ship_every_email_body():
    """The list query selects no bodies — only the rows that WIN the fold have
    their body read, so the payload scales with contacts, not with volume."""
    rows = [_log_row("r01", "a@x.com", "first"), _log_row("r02", "a@x.com", "second"),
            _log_row("r03", "b@y.com", "hello")]
    rec = _Recorder({"email_log": rows, "bookings": []})
    db.list_conversations("t1", client=rec)

    listing = [cols for tbl, cols in rec.selects if tbl == "email_log"][0]
    assert "body_in" not in listing, "the list query must not select message bodies"
    bodies = [cols for tbl, cols in rec.selects if tbl == "email_log" and "body_in" in cols]
    assert bodies == ["id,body_in,kind"], "bodies are read once, by id (the row's kind rides for the preview rule), and nothing else"


def test_the_preview_is_still_the_latest_message():
    """Identical output to the single-query version: last row per contact wins."""
    rows = [_log_row("r01", "a@x.com", "the older message"),
            _log_row("r02", "a@x.com", "the NEWER message"),
            _log_row("r03", "b@y.com", "b's only message")]
    rec = _Recorder({"email_log": rows, "bookings": []})
    convos = {c["contact"]: c for c in db.list_conversations("t1", client=rec)}

    assert convos["a@x.com"]["preview"] == "the NEWER message"
    assert convos["b@y.com"]["preview"] == "b's only message"
    assert convos["a@x.com"]["count"] == 2
    # the id used to fetch the body is bookkeeping, never handed to a template
    assert not [k for c in convos.values() for k in c if k.startswith("_")]


def test_a_preview_read_failure_costs_the_preview_not_the_inbox():
    class _Boom(_Recorder):
        def table(self, name):
            t = _RecorderTable(self, name)
            if name == "email_log":
                original = t.execute

                def execute():
                    if t._ids is not None:
                        raise RuntimeError("previews unavailable")
                    return original()
                t.execute = execute
            return t

    rec = _Boom({"email_log": [_log_row("r01", "a@x.com", "body")], "bookings": []})
    (c,) = db.list_conversations("t1", client=rec)
    assert c["contact"] == "a@x.com" and c["preview"] == ""


# ── the live mailbox scan ───────────────────────────────────────────────────
@pytest.fixture
def live_cache(monkeypatch):
    """Re-enable the navcache this one file cares about (conftest disables it)."""
    navcache._store.clear()
    monkeypatch.setattr(navcache, "_disabled", False)
    yield
    navcache._store.clear()


def _snapshot_env(monkeypatch, calls):
    monkeypatch.setattr(db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(db, "list_conversations", lambda *a, **k: [])

    async def _list_messages(tenant_id, folder, mailbox_id=None):
        calls.append(mailbox_id)
        return [{"id": "m1", "subject": "hi", "preview": "p", "received": "2026-09-01T08:00:00Z",
                 "from": {"address": "new@sender.com", "name": "New"}}]

    from app import mailbox
    monkeypatch.setattr(mailbox, "list_messages", _list_messages)


def test_the_mailbox_is_scanned_once_not_once_per_render(live_cache, monkeypatch):
    """The scan is a live Graph/Gmail call — on Gmail, twenty-six round trips.
    Re-rendering the inbox (or its SSE fragment) must reuse it."""
    calls = []
    _snapshot_env(monkeypatch, calls)

    first = asyncio.run(inbox_labels.conversation_snapshot("t1"))
    asyncio.run(inbox_labels.conversation_snapshot("t1"))
    asyncio.run(inbox_labels.conversation_snapshot("t1"))

    assert len(calls) == 1, "the mailbox was re-scanned on a repeat render"
    assert [c["contact"] for c in first] == ["new@sender.com"]


def test_an_inbox_event_makes_the_next_render_look_again(live_cache, monkeypatch):
    """The cache holds only for as long as nothing has happened: the pipeline
    publishes the moment it logs an inbound, and that busts the scan."""
    calls = []
    _snapshot_env(monkeypatch, calls)

    asyncio.run(inbox_labels.conversation_snapshot("t1"))
    events.publish("t1", {"type": "draft_ready"})
    asyncio.run(inbox_labels.conversation_snapshot("t1"))

    assert len(calls) == 2


def test_a_mailbox_read_failure_is_not_cached(live_cache, monkeypatch):
    """A degraded render must not pin the inbox to 'no new senders' for the
    whole window — the next render tries the mailbox again."""
    calls = []

    async def _boom(tenant_id, folder, mailbox_id=None):
        calls.append(mailbox_id)
        raise RuntimeError("graph is down")

    from app import mailbox
    monkeypatch.setattr(db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(db, "list_conversations", lambda *a, **k: [])
    monkeypatch.setattr(mailbox, "list_messages", _boom)

    assert asyncio.run(inbox_labels.conversation_snapshot("t1")) == []
    assert asyncio.run(inbox_labels.conversation_snapshot("t1")) == []
    assert len(calls) == 2
