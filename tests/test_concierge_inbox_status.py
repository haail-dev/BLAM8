"""Concierge inbox-status truth (regression for the live 2026-07-26 bug).

The owner asked "What needs replying to today?" with 6 conversations needing
attention and the concierge answered "your inbox is clear": its tool filtered
email_log on the RAW category column, every row predated auto-categorisation
(all NULL), so the query returned 0 while the inbox page — which derives labels
app-side — showed all 6.

Pins the fix end to end:
  * get_inbox_overview answers from the SAME snapshot the inbox page renders
    (app/inbox_labels.conversation_snapshot) — legacy NULL rows derive their
    label, brand-new senders fold in, counts match the page's pills;
  * needs_reply lists oldest-first (waited longest = most urgent);
  * an empty inbox is an explicit zero from a real lookup, never an assumption;
  * the business prompt orders the model to ALWAYS call the tool for
    inbox-status questions and forbids date-bounding them;
  * both tiers see the tool (inbox questions are a Starter feature).

Run: python -m pytest tests/test_concierge_inbox_status.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.inbox_labels as inbox_labels
import app.mailbox as mailbox
from app import concierge, concierge_data
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

# The bug's inbox, as email_log stores it: six conversations, every row
# predating auto-categorisation (category NULL) — 2 derive needs_reply,
# 4 derive draft_ready.
LEGACY_CONVOS = [
    {"contact": "team@mail.perplexity.ai", "from_name": "Perplexity", "state": "pending",
     "thread_state": "needs_attention", "subject": "Weekly digest", "preview": "hi",
     "category": None, "last_at": "2026-07-15T09:00:00+00:00"},
    {"contact": "kris@x.com", "from_name": "Kris", "state": "pending",
     "thread_state": "needs_attention", "subject": "Annual service enquiry", "preview": "hi",
     "category": None, "last_at": "2026-07-20T09:00:00+00:00"},
    {"contact": "notifications@onelink.com", "from_name": "Onelink", "state": "pending",
     "thread_state": "resolved", "subject": "Quote request", "preview": "hi",
     "category": None, "last_at": "2026-07-18T09:00:00+00:00"},
    {"contact": "louise@x.com", "from_name": "Louise Feaheny", "state": "pending",
     "thread_state": "resolved", "subject": "Booking confirmation", "preview": "hi",
     "category": None, "last_at": "2026-07-22T09:00:00+00:00"},
    {"contact": "dave@x.com", "from_name": "Dave Pearson", "state": "pending",
     "thread_state": "resolved", "subject": "Follow-up on quote", "preview": "hi",
     "category": None, "last_at": "2026-07-25T09:00:00+00:00"},
    {"contact": "julius@x.com", "from_name": "Julius Deane", "state": "pending",
     "thread_state": "resolved", "subject": "Test email", "preview": "hi",
     "category": None, "last_at": "2026-07-25T10:00:00+00:00"},
]


def _env(monkeypatch, convos=LEGACY_CONVOS, inbox_msgs=()):
    async def _messages(*a, **k):
        return list(inbox_msgs)
    monkeypatch.setattr(inbox_labels.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(inbox_labels.db, "list_conversations",
                        lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(inbox_labels.db, "list_draft_suppressed", lambda *a, **k: [])
    monkeypatch.setattr(mailbox, "list_messages", _messages)


def _overview(monkeypatch, **kwargs):
    _env(monkeypatch, **kwargs)
    return asyncio.run(concierge_data.execute(
        "t1", None, "get_inbox_overview", {}, full_qa=True))


# ── the exact live failure, fixed ─────────────────────────────────────────────
def test_overview_lists_all_six_legacy_null_category_conversations(monkeypatch):
    out = _overview(monkeypatch)
    assert out["counts"]["needs_reply"] == 2
    # Every waiting draft, whatever its label (2026-09-26): the six legacy rows
    # all carry a draft, the same number the Inbox's Draft Ready pill shows.
    assert out["counts"]["draft_ready"] == 6
    assert out["total_needing_attention"] == 6
    assert len(out["needs_attention"]) == 6
    names = {c["contact_name"] for c in out["needs_attention"]}
    assert names == {"Perplexity", "Kris", "Onelink", "Louise Feaheny",
                     "Dave Pearson", "Julius Deane"}


def test_overview_groups_needs_reply_first_oldest_first(monkeypatch):
    out = _overview(monkeypatch)
    cats = [c["category"] for c in out["needs_attention"]]
    assert cats == ["needs_reply"] * 2 + ["draft_ready"] * 4
    waiting = [c for c in out["needs_attention"] if c["category"] == "needs_reply"]
    assert [c["contact_name"] for c in waiting] == ["Perplexity", "Kris"]  # 15 Jul before 20 Jul


def test_overview_entries_carry_what_the_answer_needs(monkeypatch):
    entry = _overview(monkeypatch)["needs_attention"][0]
    assert entry["contact_email"] and entry["contact_name"]
    assert entry["subject"] and entry["date"] and entry["category"]


def test_overview_counts_match_the_inbox_page_pills(monkeypatch):
    """The validation contract: the tool and the page must report the SAME
    inbox. Render the conversations page over identical data and compare its
    pill counts with the tool's."""
    _env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_scheduling_config",
                        lambda *a, **k: {"intake_fields": []})
    monkeypatch.setattr(dashboard.db, "crm_contact_temperatures", lambda *a, **k: {})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        html = TestClient(app).get("/app/conversations").text
    finally:
        app.dependency_overrides.clear()
    out = asyncio.run(concierge_data.execute(
        "t1", None, "get_inbox_overview", {}, full_qa=True))
    assert ">Needs Reply <" in html and ">2</span>" in html
    assert ">Draft Ready <" in html and ">6</span>" in html
    assert out["counts"]["needs_reply"] == 2 and out["counts"]["draft_ready"] == 6


def test_brand_new_sender_folds_in_as_needs_reply(monkeypatch):
    out = _overview(monkeypatch, inbox_msgs=[
        {"from": {"address": "new@x.com", "name": "New Customer"},
         "subject": "Help", "preview": "…", "received": "2026-07-26T08:00:00+00:00",
         "id": "m1"}])
    assert out["counts"]["needs_reply"] == 3
    assert any(c["contact_email"] == "new@x.com" for c in out["needs_attention"])


def test_empty_inbox_is_an_explicit_zero_not_an_assumption(monkeypatch):
    out = _overview(monkeypatch, convos=[])
    assert out["needs_attention"] == []
    assert out["total_needing_attention"] == 0
    assert set(out["counts"]) == set(inbox_labels.CONVO_CATEGORIES)


def test_overview_surfaces_suppressed_contacts(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(inbox_labels.db, "list_draft_suppressed", lambda *a, **k: [
        {"address": "quiet@x.com", "status": "suppressed",
         "consecutive_unsent_drafts": 3, "emails_since_suppressed": 1,
         "suppressed_at": "2026-07-20T00:00:00+00:00"}])
    out = asyncio.run(concierge_data.execute(
        "t1", None, "get_inbox_overview", {}, full_qa=True))
    assert out["suppressed_contacts"][0]["contact_email"] == "quiet@x.com"


# ── the model-facing contract ─────────────────────────────────────────────────
def test_overview_tool_takes_no_filters_by_design():
    tool = next(t for t in concierge_data.TOOLS if t["name"] == "get_inbox_overview")
    assert tool["input_schema"]["properties"] == {}
    assert "today" in tool["description"].lower()


def test_overview_is_available_on_both_tiers():
    for full_qa in (True, False):
        names = {t["name"] for t in concierge_data.tools_for(full_qa)}
        assert "get_inbox_overview" in names


def test_business_prompt_orders_a_tool_call_for_inbox_status_questions():
    prompt = concierge.build_business_prompt(business_name="T", today="2026-07-26")
    assert "ALWAYS call get_inbox_overview" in prompt
    assert "never add date filters" in prompt
    assert "I checked your inbox" in prompt   # caught-up needs a verified zero


def test_prompt_splits_outstanding_from_awaiting_approval():
    # N3 / F10: "what's most urgent" read the inbox while a card awaited the owner's answer.
    prompt = concierge.build_business_prompt(business_name="T", today="2026-10-14")
    assert "3b. OUTSTANDING versus AWAITING APPROVAL" in prompt
    assert "call list_awaiting_approval first and lead with it" in prompt
    assert "ALWAYS call get_inbox_overview" in prompt                 # the day's work still starts there
    assert "say which you checked" in prompt
    assert "say so only about the inbox unless you also checked follow-ups and the cards" in prompt
    assert "'What's awaiting my approval / anything to confirm' → list_awaiting_approval" in prompt
    # The canned caught-up line must not say "approval" (that word now means the cards).
    assert "I checked your inbox — nothing needs a reply or a draft approved right now." in prompt
    assert "nothing needs a reply or approval right now" not in prompt
    tool = next(t for t in concierge_data.TOOLS if t["name"] == "get_inbox_overview")
    assert "does NOT know about the Confirm/Cancel cards" in tool["description"]
    assert "list_awaiting_approval" in tool["description"]


def test_overview_degrades_honestly_on_a_data_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(inbox_labels.db, "list_conversations", _boom)
    monkeypatch.setattr(inbox_labels.db, "ignored_addresses", lambda *a, **k: set())
    out = asyncio.run(concierge_data.execute(
        "t1", None, "get_inbox_overview", {}, full_qa=True))
    assert out["error"] == "lookup_failed"
