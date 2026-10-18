"""Auto-categorisation — smart inbox labels (2026-07-26).

Pins the contract end to end:
  * the draft schema asks the model to classify the INBOUND only
    (needs_reply | fyi | automated), and a malformed answer clamps to
    needs_reply (over-flagging is the safe direction);
  * pipeline._classify: ground-truth signals beat the model — anything a human
    must act on is needs_reply, a hold placed is booking_pending, a clean
    fyi/automated call suppresses the queue entry (informational=True);
  * a no-reply system sender is LOGGED with the Automated label (and still
    never drafted);
  * dashboard._conversation_category: stored label wins, legacy rows derive,
    manual overrides stick, and bookings are ground truth at read time;
  * the inbox filters by ?cat= (All hides Automated), rows wear the chip;
  * the manual override endpoint validates + restamps;
  * the concierge search_conversations tool passes `category` through.

Run: python -m pytest tests/test_auto_categorisation.py -q
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.pipeline as pipeline
from app import agent, concierge_data
from app.agent import DRAFT_SCHEMA, INBOUND_CATEGORIES, _parse_draft
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── the model-facing contract ─────────────────────────────────────────────────
def test_draft_schema_carries_the_inbound_category():
    assert DRAFT_SCHEMA["properties"]["category"]["enum"] == list(INBOUND_CATEGORIES)
    assert "category" in DRAFT_SCHEMA["required"]
    assert "category_reason" in DRAFT_SCHEMA["required"]


def test_parse_draft_keeps_a_valid_category_and_reason():
    parsed = _parse_draft(json.dumps({"reply": "ok", "category": "fyi",
                                      "category_reason": "Payment receipt"}))
    assert parsed["category"] == "fyi"
    assert parsed["category_reason"] == "Payment receipt"


@pytest.mark.parametrize("bad", ["quote_sent", "banana", "", None])
def test_parse_draft_clamps_a_bad_category_to_needs_reply(bad):
    parsed = _parse_draft(json.dumps({"reply": "ok", "category": bad}))
    assert parsed["category"] == "needs_reply"


def test_parse_draft_defaults_on_non_json():
    parsed = _parse_draft("plain refusal text")
    assert parsed["category"] == "needs_reply" and parsed["category_reason"] == ""


# ── pipeline._classify: ground truth beats the model ─────────────────────────
BASE = dict(escalated=False, scheduling_gap=False, deferred=False,
            thread_state="in_progress", held=False)


@pytest.mark.parametrize("override", [
    {"escalated": True}, {"scheduling_gap": True}, {"deferred": True},
    {"thread_state": "needs_attention"},
])
def test_action_signals_force_needs_reply_even_over_fyi(override):
    cat, informational = pipeline._classify("fyi", **{**BASE, **override})
    assert cat == "needs_reply" and informational is False


def test_a_hold_placed_wins_over_an_fyi_call():
    cat, informational = pipeline._classify("fyi", **{**BASE, "held": True})
    assert cat == "booking_pending" and informational is False


@pytest.mark.parametrize("model_cat", ["fyi", "automated"])
def test_a_clean_informational_call_is_honoured_and_suppresses_the_queue(model_cat):
    cat, informational = pipeline._classify(model_cat, **BASE)
    assert cat == model_cat and informational is True


def test_an_ordinary_inbound_becomes_draft_ready():
    cat, informational = pipeline._classify("needs_reply", **BASE)
    assert cat == "draft_ready" and informational is False


# ── no-reply senders are logged with the Automated label ─────────────────────
def test_no_reply_sender_is_logged_as_automated_and_never_drafted(monkeypatch):
    calls = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))
        return "log-1", True

    monkeypatch.setattr(pipeline.db, "insert_email_log", _capture)
    out = asyncio.run(pipeline.draft_for_email(
        "t1", {"from_address": "no-reply@stripe.com", "subject": "Receipt",
               "body": "Your payment", "thread_id": "th1"},
        source_message_id="m1", mailbox_id="mb1"))
    assert out is None                      # still no draft, ever
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["category"] == "automated"
    assert kwargs["status"] == "skipped"    # never enters the approval queue
    assert kwargs["source_message_id"] == "m1"   # deduped like any inbound


def test_a_logging_failure_never_changes_the_skip(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(pipeline.db, "insert_email_log", _boom)
    out = asyncio.run(pipeline.draft_for_email(
        "t1", {"from_address": "mailer-daemon@x.com", "subject": "b", "body": ""}))
    assert out is None


# ── dashboard._conversation_category ──────────────────────────────────────────
def test_stored_label_wins():
    assert dashboard._conversation_category(
        {"category": "fyi", "category_source": "auto", "state": "skipped"}) == "fyi"


def test_brand_new_sender_is_needs_reply():
    assert dashboard._conversation_category({"state": "new"}) == "needs_reply"


@pytest.mark.parametrize("row,expected", [
    ({"state": "sent", "thread_state": "in_progress"}, "quote_sent"),
    ({"state": "pending", "thread_state": "in_progress"}, "draft_ready"),
    ({"state": "pending", "thread_state": "needs_attention"}, "needs_reply"),
    ({"state": "pending", "gap": "prices"}, "needs_reply"),
    ({"state": "skipped", "thread_state": "resolved"}, "fyi"),
    ({"state": "pending", "thread_state": "booked", "has_upcoming_booking": True},
     "booking_pending"),
])
def test_legacy_rows_derive_from_status_and_thread_state(row, expected):
    assert dashboard._conversation_category(row) == expected


def test_bookings_are_ground_truth_at_read_time():
    # date passed → the booking completed → the thread reads Quote Sent
    assert dashboard._conversation_category(
        {"category": "booking_pending", "category_source": "auto",
         "state": "sent"}) == "quote_sent"
    # upcoming booking keeps a replied thread Booking Pending
    assert dashboard._conversation_category(
        {"category": "quote_sent", "category_source": "auto", "state": "sent",
         "has_upcoming_booking": True}) == "booking_pending"


def test_manual_override_skips_the_booking_adjustments():
    assert dashboard._conversation_category(
        {"category": "booking_pending", "category_source": "manual",
         "state": "sent"}) == "booking_pending"


# ── the inbox routes ──────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


CONVOS = [
    {"contact": "a@x.com", "state": "pending", "subject": "Quote", "mailbox_id": None,
     "last_at": "2026-07-25T09:00:00+00:00", "category": "draft_ready",
     "category_source": "auto"},
    {"contact": "b@x.com", "state": "sent", "subject": "Leak", "mailbox_id": None,
     "last_at": "2026-07-24T09:00:00+00:00", "category": "quote_sent",
     "category_source": "auto"},
    {"contact": "noreply@sys.com", "state": "skipped", "subject": "Notice", "mailbox_id": None,
     "last_at": "2026-07-23T09:00:00+00:00", "category": "automated",
     "category_source": "auto"},
    {"contact": "d@x.com", "state": "skipped", "subject": "Receipt", "mailbox_id": None,
     "last_at": "2026-07-22T09:00:00+00:00", "category": "fyi",
     "category_source": "auto", "category_reason": "Payment receipt — no reply needed"},
]


def _env(monkeypatch, convos=CONVOS):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": []})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)


def test_all_view_hides_automated_but_its_pill_shows(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "a@x.com" in html and "b@x.com" in html and "d@x.com" in html
    assert "noreply@sys.com" not in html          # bookkeeping, not inbox
    assert ">Ignored <" in html                   # …but one click away (the label reads Ignored — Kris 2026-09-13)
    # All is no longer on an unfiltered rail (Kris 2026-09-01) — it repeated
    # what the list already said and cost the width Booked needed. Its count
    # still has to exclude automated, so check it where it now appears: on a
    # filtered rail, as the way back to the whole inbox.
    assert ">All <" not in html
    filtered = client.get("/app/conversations", params={"cat": "quote_sent"}).text
    assert ">All <span" in filtered and ">3</span>" in filtered


def test_category_filter_narrows_the_list(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations", params={"cat": "quote_sent"}).text
    assert "b@x.com" in html
    assert "a@x.com" not in html and "d@x.com" not in html


def test_automated_filter_shows_the_hidden_rows(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations", params={"cat": "automated"}).text
    assert "noreply@sys.com" in html and "a@x.com" not in html


def test_row_state_carries_tone_and_the_classifiers_reason(client, monkeypatch):
    # Counting House (2026-08-29): row state = coloured text + a 6px dot (the
    # CATEGORY_META tone/dot pair), not a filled chip.
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "text-brand" in html and "bg-brand self-center" in html   # Draft Ready tone + dot
    assert "Payment receipt — no reply needed" in html    # reason on the FYI state title


def test_fragment_url_carries_the_selected_category(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations", params={"cat": "fyi"}).text
    assert "/app/conversations/fragment?cat=fyi" in html  # live refresh keeps the filter


# ── manual override endpoint ──────────────────────────────────────────────────
def test_manual_override_restamps_and_redirects(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.db, "set_conversation_category",
                        lambda tenant_id, contact, category, client=None:
                        seen.update(contact=contact, category=category) or True)
    r = client.post("/app/conversations/category",
                    data={"contact": "a@x.com", "category": "fyi"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen == {"contact": "a@x.com", "category": "fyi"}


def test_manual_override_rejects_an_unknown_category(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "set_conversation_category",
                        lambda *a, **k: pytest.fail("must not be called"))
    r = client.post("/app/conversations/category",
                    data={"contact": "a@x.com", "category": "banana"},
                    follow_redirects=False)
    assert r.status_code == 400


# ── concierge pass-through ────────────────────────────────────────────────────
def test_search_conversations_tool_offers_the_category_filter():
    tool = next(t for t in concierge_data.TOOLS if t["name"] == "search_conversations")
    enum = tool["input_schema"]["properties"]["category"]["enum"]
    assert set(agent.CATEGORIES) <= set(enum)


def test_search_conversations_category_filter_uses_derived_labels(monkeypatch):
    """Regression (live 2026-07-26): every email_log row predating the feature
    stores category NULL, so a raw eq-filter returned 0 while the inbox page
    showed the conversation — and the concierge told the owner "your inbox is
    clear". A category filter must run over the same derived snapshot the page
    renders, never the raw column."""
    import app.inbox_labels as inbox_labels

    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(concierge_data.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(concierge_data.db, "list_draft_suppressed", lambda *a, **k: [])
    monkeypatch.setattr(concierge_data.db, "list_conversations", lambda *a, **k: [
        # legacy row: NULL category, pending + needs_attention → derives needs_reply
        {"contact": "a@x.com", "state": "pending", "thread_state": "needs_attention",
         "subject": "Quote", "preview": "hi", "category": None,
         "last_at": "2026-07-15T09:00:00+00:00"},
    ])
    monkeypatch.setattr(
        concierge_data.db, "search_email_log",
        lambda *a, **k: pytest.fail("category filters must not hit the raw column"))
    import app.mailbox as mailbox
    monkeypatch.setattr(mailbox, "list_messages", _no_messages)
    out = asyncio.run(concierge_data._search_conversations(
        "t1", None, {"category": "needs_reply"}))
    assert out["count"] == 1
    assert out["conversations"][0]["category"] == "needs_reply"
    assert inbox_labels.derive_category is dashboard._conversation_category
