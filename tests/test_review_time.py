"""HRT — Human Review Time (Kris 2026-08-31).

The draft screens measure how long a draft is actually visible before its send
(_review_timer.html appends review_seconds to the send POST); the send route
stamps it best-effort; the Inbox's collapsed history months average it into an
"HRT 1m 05s" chip next to "X sent · Y drafted".

Run: python -m pytest tests/test_review_time.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.db import email_log_month_stats
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── the send route stamps the dwell ───────────────────────────────────────────
def _wire_send(client, monkeypatch, stamps):
    entry = {"id": "log-1", "from_address": "sam@x.co", "subject": "Reminder: tomorrow",
             "draft_reply": "Body.", "kind": "reminder", "mailbox_id": None,
             "source_message_id": None}

    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "set_email_log_review_seconds",
                        lambda log_id, tid, secs, **k: stamps.append((log_id, secs)))
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.mailbox, "send_mail", _ok)
    monkeypatch.setattr(dashboard.mailbox, "send_reply", _ok)
    monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", _ok)


def test_send_stamps_review_seconds(client, monkeypatch):
    stamps = []
    _wire_send(client, monkeypatch, stamps)
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Body.", "review_seconds": "42"})
    assert r.status_code == 303
    assert stamps == [("log-1", 42)]


def test_send_clamps_a_forgotten_tab_and_a_negative(client, monkeypatch):
    stamps = []
    _wire_send(client, monkeypatch, stamps)
    client.post("/app/drafts/log-1/send", follow_redirects=False,
                data={"draft_reply": "Body.", "review_seconds": "999999"})
    client.post("/app/drafts/log-1/send", follow_redirects=False,
                data={"draft_reply": "Body.", "review_seconds": "-5"})
    assert [s for _, s in stamps] == [1800, 0]


def test_send_without_the_field_stamps_nothing(client, monkeypatch):
    # Old tabs / no-JS submits carry no review_seconds — the send still works
    # and no stamp is written (null = untracked, never zero).
    stamps = []
    _wire_send(client, monkeypatch, stamps)
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Body."})
    assert r.status_code == 303
    assert stamps == []


def test_owner_written_send_is_stamped_owner(client, monkeypatch):
    # An empty original draft (escalation / knowledge hold) means the human
    # authored the reply outright — sent_via 'owner', excluded from the AI
    # edit-rate split (edited=None), labelled "You · Sent" in the thread.
    entry = {"id": "log-1", "from_address": "sam@x.co", "subject": "Refund?",
             "draft_reply": "", "kind": "reminder", "mailbox_id": None,
             "source_message_id": None}
    marks = {}

    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent",
                        lambda log_id, tid, via, edited=None, **k:
                        marks.update(via=via, edited=edited))
    monkeypatch.setattr(dashboard.db, "set_email_log_review_seconds", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.mailbox, "send_mail", _ok)
    monkeypatch.setattr(dashboard.mailbox, "send_reply", _ok)
    monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", _ok)
    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "I wrote this myself."})
    assert r.status_code == 303
    assert marks == {"via": "owner", "edited": None}


# ── the month aggregate ───────────────────────────────────────────────────────
def test_month_stats_average_human_sends_only(monkeypatch):
    rows = [
        {"created_at": "2026-07-03T10:00:00+00:00", "status": "sent", "sent_via": "human", "review_seconds": 30},
        {"created_at": "2026-07-09T10:00:00+00:00", "status": "sent", "sent_via": "human", "review_seconds": 90},
        {"created_at": "2026-07-11T10:00:00+00:00", "status": "sent", "sent_via": "auto", "review_seconds": None},
        {"created_at": "2026-07-14T10:00:00+00:00", "status": "pending", "sent_via": None, "review_seconds": None},
        {"created_at": "2026-06-02T10:00:00+00:00", "status": "sent", "sent_via": "human", "review_seconds": None},
    ]

    class _Q:
        def __init__(self, data): self._d = data
        def __getattr__(self, name): return lambda *a, **k: self
        def execute(self): return type("R", (), {"data": self._d})()

    import app.db as adb
    monkeypatch.setattr(adb, "db", lambda: type("C", (), {"table": lambda s, t: _Q(rows)})())
    stats = email_log_month_stats("t1")
    assert stats["2026-07"] == {"sent": 3, "drafted": 4, "hrt": 60.0}
    # a pre-tracking month: sends but no dwell data → hrt None, chip omitted
    assert stats["2026-06"] == {"sent": 1, "drafted": 1, "hrt": None}


def test_history_face_formatting():
    assert dashboard._fmt_review_time(None) is None
    assert dashboard._fmt_review_time(42) == "42s"
    assert dashboard._fmt_review_time(65.4) == "1m 05s"
    assert dashboard._fmt_review_time(600) == "10m 00s"
    # the operator console's totals cross the hour line
    from app.usage import fmt_review_time
    assert fmt_review_time(10260) == "2h 51m"


def test_review_time_stats_aggregates_per_tenant(monkeypatch):
    rows = [
        {"tenant_id": "t1", "review_seconds": 30},
        {"tenant_id": "t1", "review_seconds": 90},
        {"tenant_id": "t2", "review_seconds": 10},
    ]

    class _Q:
        def __init__(self, data): self._d = data
        def __getattr__(self, name): return lambda *a, **k: self
        @property
        def not_(self): return self
        def execute(self): return type("R", (), {"data": self._d})()

    import app.db as adb
    monkeypatch.setattr(adb, "db", lambda: type("C", (), {"table": lambda s, t: _Q(rows)})())
    stats = adb.review_time_stats()
    assert stats == {"t1": {"n": 2, "total": 120}, "t2": {"n": 1, "total": 10}}


# ── the inbox face wiring ─────────────────────────────────────────────────────
def test_history_months_wear_the_ledger_line(client, monkeypatch):
    from datetime import datetime, timezone
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    convos = [
        {"contact": "now@x.com", "state": "pending", "subject": "Hi", "mailbox_id": None,
         "last_at": f"{now_month}-01T09:00:00+00:00", "thread_state": "in_progress"},
        {"contact": "old@x.com", "state": "sent", "subject": "Then", "mailbox_id": None,
         "last_at": "2026-07-05T09:00:00+00:00", "thread_state": "in_progress"},
    ]

    async def _no_messages(*a, **k):
        return []

    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations",
                        lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)
    monkeypatch.setattr(dashboard.db, "email_log_month_stats",
                        lambda *a, **k: {"2026-07": {"sent": 12, "drafted": 15, "hrt": 100}})

    html = client.get("/app/conversations").text
    assert "12 sent · 15 drafted" in html            # July wears its ledger line
    assert "HRT 1m 40s" in html
    assert f'data-month="{now_month}" open' in html  # current month arrives open
    assert "July 2026" in html
