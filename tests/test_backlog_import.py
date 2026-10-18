"""The recent backlog comes in at connect (src/app/backlog.py; Kris, kennels run
#10, 2026-09-13: an empty inbox gave nobody anything to ignore).

Pins:
- the two provider listings ask for the last N days of INBOX, ids only,
  bounded;
- import_recent files each unknown message as status skipped / category fyi
  with the "Before BLAM8" reason, skips known ids and the mailbox's own mail,
  never drafts, never mints a contact, never raises;
- both OAuth callbacks spawn it behind the redirect;
- the inbox row wears a "before BLAM8" pill; the connect step says so.

Run: python -m pytest tests/test_backlog_import.py -q
"""
from pathlib import Path

import pytest

from app import backlog, db, gmail, graph, mailbox

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def durable_import_state(monkeypatch):
    monkeypatch.setattr(db, "claim_mailbox_import", lambda *a: {"run_id": "test", "state": "running"})
    monkeypatch.setattr(db, "finish_mailbox_import", lambda *a: None)


# ── listings ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gmail_listing_asks_for_the_window_ids_only(monkeypatch):
    seen = {}

    async def fake_get(token, path, params=None, client=None):
        seen.update(path=path, params=params)
        return {"messages": [{"id": "m1"}, {"id": "m2"}, {}]}
    monkeypatch.setattr(gmail, "_get", fake_get)
    assert await gmail.list_recent_inbox_ids("tok", days=14, limit=100) == ["m1", "m2"]
    assert seen["path"] == "/messages"
    assert seen["params"] == {"labelIds": "INBOX", "q": "newer_than:14d", "maxResults": "100"}


@pytest.mark.asyncio
async def test_graph_listing_asks_for_the_window_ids_only(monkeypatch):
    seen = {}

    async def fake_get(token, path, params=None, extra_headers=None):
        seen.update(path=path, params=params)
        return {"value": [{"id": "g1"}, {"id": "g2"}]}
    monkeypatch.setattr(graph, "graph_get", fake_get)
    assert await graph.list_recent_inbox_ids("tok", days=14, limit=100) == ["g1", "g2"]
    assert seen["path"] == "/me/mailFolders/inbox/messages"
    assert seen["params"]["$select"] == "id" and seen["params"]["$top"] == "100"
    assert seen["params"]["$filter"].startswith("receivedDateTime ge 20")


# ── the import ───────────────────────────────────────────────────────────────
class _Rows(list):
    events: list


@pytest.fixture
def env(monkeypatch):
    rows = _Rows()
    known = {"old1"}
    monkeypatch.setattr(db, "get_connection_by_id",
                        lambda cid, tid=None: {"id": cid, "tenant_id": tid, "provider": "google",
                                               "mailbox": "business.blam8@gmail.com"})
    monkeypatch.setattr(mailbox, "connection_provider", lambda conn, tid=None: "google")

    async def token(conn, provider="microsoft"):
        return "tok"
    monkeypatch.setattr(mailbox, "valid_access_token", token)

    async def ids(token, days=14, limit=100):
        return ["old1", "new1", "self1", "new2", "gone"]
    monkeypatch.setattr(gmail, "list_recent_inbox_ids", ids)
    monkeypatch.setattr(db, "get_email_log_by_message",
                        lambda tid, mid, client=None: {"id": "x"} if mid in known else None)

    msgs = {
        "new1": {"from": {"address": "Farmfoods@news.example.com", "name": "Farmfoods"},
                 "subject": "Deals", "body": "Big savings", "thread_id": "t1"},
        "self1": {"from": {"address": "business.blam8@gmail.com"}, "subject": "note to self", "body": ""},
        "new2": {"from": {"address": "sam@example.com", "name": "Sam"}, "subject": "Boarding in Oct?",
                 "body": "Hi", "thread_id": "t2"},
    }

    async def get_message(tid, mid, mailbox_id=None):
        return msgs.get(mid)
    monkeypatch.setattr(mailbox, "get_message", get_message)

    def insert(tid, from_address, subject, body, draft, **kw):
        rows.append({"tenant": tid, "from": from_address, "subject": subject, **kw})
        return ("log-" + kw["source_message_id"], True)
    monkeypatch.setattr(db, "insert_email_log", insert)
    events = []
    monkeypatch.setattr(db, "log_agent_event", lambda tid, kind, summary, **kw: events.append((kind, summary)))
    rows.events = events
    return rows


@pytest.mark.asyncio
async def test_import_files_unknown_mail_as_before_blam8_and_nothing_else(env):
    n = await backlog.import_recent("t1", "c1")
    assert n == 2
    assert [r["source_message_id"] for r in env] == ["new1", "new2"]     # old1 known, self1 own, gone missing
    row = env[0]
    assert row["from"] == "farmfoods@news.example.com"                    # lower-cased
    assert row["status"] == "skipped" and row["category"] == "fyi"
    assert row["category_reason"].startswith("Before BLAM8")
    assert row["mailbox_id"] == "c1" and row["thread_id"] == "t1"
    # the outcome lands in the Agent log
    assert len(env.events) == 1 and env.events[0][0] == "mailbox_backlog"
    assert "2 filed for review" in env.events[0][1]
    assert "1 already imported" in env.events[0][1]
    assert "1 couldn't be fetched" in env.events[0][1]


@pytest.mark.asyncio
async def test_import_never_raises_and_records_why(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_connection_by_id", boom)
    events = []
    monkeypatch.setattr(db, "log_agent_event", lambda tid, kind, summary, **kw: events.append(summary))
    assert await backlog.import_recent("t1", "c1") == 0
    assert len(events) == 1 and "import failed" in events[0]


def test_the_connected_card_counts_or_offers_a_retry():
    html = _read("src/app/templates/_backlog_status.html")
    assert "data-backlog-count" in html and "data-backlog-retry" in html and "data-backlog-started" in html
    assert "'/onboarding/mailbox/backlog'" in html
    src = _read("src/app/onboarding.py")
    assert '@router.post("/mailbox/backlog"' in src
    assert "backlog.default_status(ctx.tenant_id, client=ctx.client) if connected else {}" in src


def test_import_writes_no_contacts_and_no_drafts():
    src = _read("src/app/backlog.py")
    assert "crm." not in src and "draft_for" not in src and "answer" not in src


# ── the hook and the surfaces ────────────────────────────────────────────────
def test_both_callbacks_spawn_the_import_and_the_surfaces_say_so():
    oauth = _read("src/app/oauth.py")
    assert oauth.count("_spawn_backlog(tenant_id, conn_id)") == 2         # Microsoft + Google
    assert "backlog.start_import(tenant_id, conn_id)" in oauth
    assert "_TASKS.add(task)" in _read("src/app/backlog.py")                  # never garbage-collected mid-run
    assert "data-before-blam8" in _read("src/app/templates/app/_conversation_list.html")
    assert '{% include "_backlog_status.html" %}' in _read("src/app/templates/onboarding/mailbox.html")


# ── the count Today and the connected card show ───────────────────────────────
class _Chain:
    """A tiny stand-in for the postgrest builder: every call chains, execute
    returns the rows it was built with."""
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self._rows, "count": len(self._rows)})()


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "email_log"
        return _Chain(self._rows)


def test_count_rows_falls_as_senders_are_ignored(monkeypatch):
    """The nudge says "ignore any sender the agent should never answer", so
    the number it shows has to drop as the owner does exactly that (the rig
    on 2026-09-20: four senders ignored a week earlier, Today still said 66)."""
    rows = ([{"from_address": "kris@example.com"}] * 54
            + [{"from_address": "no-reply@accounts.google.com"}] * 8
            + [{"from_address": "Mailer-Daemon@googlemail.com"}] * 4)
    ignored = set()
    monkeypatch.setattr(db, "ignored_addresses", lambda tid, client=None: ignored)
    assert backlog.count_rows("t1", client=_Client(rows)) == 66
    ignored.update({"no-reply@accounts.google.com", "mailer-daemon@googlemail.com"})
    assert backlog.count_rows("t1", client=_Client(rows)) == 54
    ignored.add("kris@example.com")
    assert backlog.count_rows("t1", client=_Client(rows)) == 0


def test_count_rows_is_zero_on_any_failure(monkeypatch):
    class _Broken:
        def table(self, name):
            raise RuntimeError("db down")
    assert backlog.count_rows("t1", client=_Broken()) == 0
