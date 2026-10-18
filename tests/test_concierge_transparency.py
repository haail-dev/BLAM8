"""Concierge transparency & proactivity (docs/modules/module-concierge-transparency.md):
the ghost trail (§A), draft grounding pills (§B), and the proactive notification
feed behind the bubble's badge/glow (§C).

Run: python -m pytest tests/test_concierge_transparency.py -q
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import concierge, pipeline, push
from app import db as db_module
from app.auth import require_onboarded, TenantCtx
from app.concierge_routes import tool_detail
from app.dashboard import _source_pills
from app.main import app
from app.security import require_csrf_header

ROOT = Path(__file__).resolve().parent.parent

# conftest's autouse _no_push_sends fixture no-ops push.notify/notify_draft_ready
# per-test; grab the real functions at import time (collection runs first) so
# the §C tests exercise the genuine feed-recording path.
_REAL_NOTIFY = push.notify
_REAL_NOTIFY_DRAFT_READY = push.notify_draft_ready


@pytest.fixture
def client(monkeypatch):
    # No membership row = the account's own owner (a failed lookup fails
    # CLOSED since the security review, so it has to be said).
    monkeypatch.setattr(db_module, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


# ── §A ghost trail: detail allowlist ─────────────────────────────────────────
def test_tool_detail_prefers_query_and_truncates():
    assert tool_detail({"query": "van hire tuesday"}) == "van hire tuesday"
    assert tool_detail({"contact": "sarah@x.com"}) == "sarah@x.com"
    # query wins over contact; unknown keys never leak.
    assert tool_detail({"query": "q", "contact": "c", "api_key": "sk-evil"}) == "q"
    assert tool_detail({"secret": "sk-evil"}) == ""
    assert tool_detail({}) == "" and tool_detail(None) == ""
    assert len(tool_detail({"query": "x" * 500})) == 80


# ── §A ghost trail: the business stream carries + persists it ────────────────
def test_business_stream_carries_trail_and_persists_it(client, monkeypatch):
    from app import concierge_routes

    saved = []
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tenant_id, client=None: {"name": "Kennel Co"})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tenant_id, role, content, **kw: saved.append((role, kw)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic",
                        lambda *a, **k: None)

    async def fake_answer(question, history, **kwargs):
        # The engine reports each lookup as it starts — the route must turn
        # them into tool events AND the persisted trail.
        await kwargs["on_tool"]("search_conversations", {"query": "van hire"})
        await kwargs["on_tool"]("list_bookings", {"date_from": "2026-07-30"})
        return concierge.BusinessReply(
            reply="Two things found.", links=[], action=None,
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)

    res = client.post("/concierge/business", json={"question": "Anything on vans?"})
    assert res.status_code == 200
    events = [json.loads(l) for l in res.text.strip().splitlines()]
    tools = [e for e in events if e["type"] == "tool"]
    assert tools[0]["hint"] == "Searching your inbox…"
    assert tools[0]["label"] == "Searched your inbox"
    assert tools[0]["detail"] == "van hire"
    assert tools[1]["detail"] == "2026-07-30"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["trail"] == [
        {"name": "search_conversations", "label": "Searched your inbox", "detail": "van hire"},
        {"name": "list_bookings", "label": "Checked the diary", "detail": "2026-07-30"},
    ]
    # The assistant row persists the trail so history replays the pills.
    assistant_kw = [kw for role, kw in saved if role == "assistant"][0]
    assert assistant_kw["trail"] == done["trail"]


def test_business_history_returns_trail(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "list_concierge_messages",
                        lambda tenant_id, client=None, **k: [
                            {"role": "assistant", "content": "Done.", "links": [],
                             "trail": [{"name": "list_invoices",
                                        "label": "Checked your invoices", "detail": ""}]},
                        ])
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: {"crm"})
    res = client.get("/concierge/business/history")
    assert res.status_code == 200
    assert res.json()["messages"][0]["trail"][0]["label"] == "Checked your invoices"


def test_concierge_page_replays_trail_pills(client, monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "K"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [
        {"role": "assistant", "content": "1 prospect.", "links": [],
         "trail": [{"name": "search_conversations", "label": "Searched your inbox",
                    "detail": "prospects"}]},
    ])
    res = client.get("/app/concierge")
    assert res.status_code == 200
    assert "Searched your inbox" in res.text        # settled pill
    assert 'data-cz-detail="prospects"' in res.text  # tap-through detail


# ── §B grounding: capture shape ──────────────────────────────────────────────
def test_draft_sources_maps_retrieval_rows():
    rows = [{"id": "c1", "source": "Pricing FAQ", "similarity": 0.83456,
             "content": "never persisted"},
            {"id": "c2", "source": None, "similarity": "junk"}]
    out = pipeline._draft_sources(rows)
    assert out == [{"source": "Pricing FAQ", "chunk_id": "c1", "similarity": 0.8346},
                   {"source": "source", "chunk_id": "c2", "similarity": None}]
    assert pipeline._draft_sources([]) == []
    assert pipeline._draft_sources(None) == []


def test_source_pills_dedupe_by_source_strongest_first():
    pills = _source_pills([
        {"source": "Terms", "chunk_id": "a", "similarity": 0.61},
        {"source": "Pricing FAQ", "chunk_id": "b", "similarity": 0.72},
        {"source": "Pricing FAQ", "chunk_id": "c", "similarity": 0.90},
    ])
    assert pills == [{"source": "Pricing FAQ", "similarity": 0.90},
                     {"source": "Terms", "similarity": 0.61}]
    assert _source_pills(None) == [] and _source_pills([]) == []


def test_kb_retrieval_trace_skips_when_kb_off(monkeypatch):
    emitted = []
    monkeypatch.setattr(pipeline.trace, "emit",
                        lambda *a, **k: emitted.append((a, k)))
    # None = KB off → no row at all ("empty" must mean ran-and-found-nothing).
    pipeline._trace_kb_retrieval("t1", "log1", "q", None,
                                 request_id=None, trigger="draft_generated")
    assert emitted == []
    pipeline._trace_kb_retrieval("t1", "log1", "q", [],
                                 request_id="r1", trigger="draft_generated")
    assert emitted[0][1]["outcome"] == "empty"
    pipeline._trace_kb_retrieval("t1", "log1", "q" * 400,
                                 [{"source": "Pricing FAQ", "chunk_id": "c", "similarity": 0.9}],
                                 request_id="r1", trigger="draft_regenerated")
    args, kw = emitted[1]
    assert kw["outcome"] == "ok"
    assert kw["detail"]["sources"] == ["Pricing FAQ"]
    assert len(kw["detail"]["query"]) == 300
    assert kw["detail"]["trigger"] == "draft_regenerated"


# ── §B grounding: the click-through endpoint ─────────────────────────────────
def test_draft_sources_endpoint_joins_live_chunks(client, monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "get_email_log_entry",
                        lambda log_id, tenant_id, client=None: {
                            "id": log_id,
                            "draft_sources": [
                                {"source": "Pricing FAQ", "chunk_id": "c1", "similarity": 0.9},
                                {"source": "Old doc", "chunk_id": "gone", "similarity": 0.5},
                            ]})
    monkeypatch.setattr(dashboard.db, "get_knowledge_chunks_by_ids",
                        lambda tenant_id, ids, client=None: [
                            {"id": "c1", "source": "Pricing FAQ", "content": "From £40/hr."}])
    res = client.get("/app/drafts/log1/sources")
    assert res.status_code == 200
    rows = res.json()["sources"]
    assert rows[0] == {"source": "Pricing FAQ", "similarity": 0.9,
                       "content": "From £40/hr.", "missing": False}
    # Edited/deleted since drafting → honest missing flag, no stale quote.
    assert rows[1]["missing"] is True and rows[1]["content"] == ""


def test_draft_sources_endpoint_404s_cross_tenant(client, monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "get_email_log_entry",
                        lambda log_id, tenant_id, client=None: None)
    assert client.get("/app/drafts/other/sources").status_code == 404


# ── §C proactive: the feed rides every push, push on or off ──────────────────
def test_notify_records_feed_row_even_with_push_off(monkeypatch):
    recorded, published = [], []
    monkeypatch.setattr(push, "push_enabled", lambda: False)
    monkeypatch.setattr(push.db, "add_concierge_notification",
                        lambda tenant_id, **kw: recorded.append((tenant_id, kw)))
    monkeypatch.setattr(push.events, "publish",
                        lambda tenant_id, event: published.append(event))
    _REAL_NOTIFY("t1", title="Invoice INV-1042 is 7 days overdue",
                 body="Dave owes £120", url="/app/invoices", tag="ov-1",
                 kind="invoice_overdue")
    assert recorded[0][0] == "t1"
    assert recorded[0][1]["kind"] == "invoice_overdue"
    assert recorded[0][1]["url"] == "/app/invoices"
    assert published == [{"type": "notification", "kind": "invoice_overdue"}]


def test_notify_excludes_operator_plumbing_kinds(monkeypatch):
    recorded = []
    monkeypatch.setattr(push, "push_enabled", lambda: False)
    monkeypatch.setattr(push.db, "add_concierge_notification",
                        lambda tenant_id, **kw: recorded.append(kw))
    _REAL_NOTIFY("t1", title="New ticket", body="", url="/admin/requests",
                 tag="ticket-1", kind="support_ticket")
    assert recorded == []   # the platform tenant's digest must not show tickets


def test_notify_draft_ready_records_feed_row(monkeypatch):
    recorded = []
    monkeypatch.setattr(push, "push_enabled", lambda: False)
    monkeypatch.setattr(push.db, "add_concierge_notification",
                        lambda tenant_id, **kw: recorded.append(kw))
    monkeypatch.setattr(push.events, "publish", lambda *a, **k: None)
    _REAL_NOTIFY_DRAFT_READY("t1", contact_name="Sarah", contact_address="s@x.com",
                             subject="Quote", log_id="l1")
    assert recorded[0]["kind"] == "draft_ready"
    assert "Sarah" in recorded[0]["title"]


def test_notifications_endpoint_returns_unseen_and_items(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "list_concierge_notifications",
                        lambda tenant_id, client=None, **k: [
                            {"kind": "invoice_paid", "title": "INV-1042 paid",
                             "body": "", "url": "/app/invoices",
                             "created_at": "2026-07-30T10:00:00+00:00", "seen_at": None}])
    monkeypatch.setattr(concierge_routes.db, "count_unseen_concierge_notifications",
                        lambda tenant_id, client=None: 1)
    res = client.get("/concierge/notifications")
    assert res.status_code == 200
    data = res.json()
    assert data["unseen"] == 1
    assert data["items"][0]["title"] == "INV-1042 paid"
    assert data["items"][0]["seen"] is False


def test_notifications_seen_marks_all(client, monkeypatch):
    from app import concierge_routes
    called = {}
    monkeypatch.setattr(concierge_routes.db, "mark_concierge_notifications_seen",
                        lambda tenant_id, client=None, **k: called.setdefault("t", tenant_id))
    res = client.post("/concierge/notifications/seen")
    assert res.status_code == 200
    assert called["t"] == "t1"


def test_notifications_degrade_not_500(client, monkeypatch):
    from app import concierge_routes

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(concierge_routes.db, "list_concierge_notifications", boom)
    monkeypatch.setattr(concierge_routes.db, "count_unseen_concierge_notifications", boom)
    res = client.get("/concierge/notifications")
    assert res.status_code == 200
    assert res.json() == {"unseen": 0, "items": []}


# ── UI contract wiring (ids/classes the JS relies on) ────────────────────────
def test_no_unseen_badge_or_digest_anywhere():
    # Kris 2026-09-23: no "While you were away" card and no badge counting
    # toward it — the Morning debrief is how the day's news arrives.
    base = (ROOT / "src/app" / "templates" / "app" / "base.html").read_text(encoding="utf-8")
    html = (ROOT / "src/app" / "templates" / "_concierge.html").read_text(encoding="utf-8")
    assert "concierge-badge" not in base
    assert 'id="bc-badge"' not in html and "bc-attention" not in html
    assert "prefers-reduced-motion" in html
    for name in ("concierge_panel.js", "concierge.js", "live.js"):
        js = (ROOT / "src/app" / "static" / name).read_text(encoding="utf-8")
        for gone in ("While you were away", "/concierge/notifications", "blam8ConciergeNotify"):
            assert gone not in js, (name, gone)


def test_concierge_js_wires_the_trail():
    panel = (ROOT / "src/app" / "static" / "concierge_panel.js").read_text(encoding="utf-8")
    assert "trailStrip" in panel and "settlePill" in panel
    # The widget keeps its own trail for fix mode.
    widget = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    assert "trailStrip" in widget and "settleTrailPill" in widget


def test_thread_template_wires_the_recall_viewer():
    # The pill row retired (Kris 2026-08-31): the evidence lives behind the
    # decision ghost's match-count link, which turns the dock into the recall
    # viewer — every match, each expandable to the exact text recalled.
    html = (ROOT / "src/app" / "templates" / "app" / "_thread.html").read_text(encoding="utf-8")
    assert "data-source-pill" not in html
    assert "data-matches" in html                    # the ghost's match-count link
    assert 'id="matches-panel"' in html              # the dock's recall viewer
    assert "What the search recalled" in html
    # the honest no-grounding line survives, in the ghost
    assert "No knowledge matches recorded." in html
