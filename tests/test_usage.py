"""Usage metering — the email unit and billing-window math (src/app/usage.py).

Pins the fix for the operator-console discrepancy (2026-07-02): every surface
now counts the SAME sold unit — one inbound email the agent drafted a reply to
(matching src/app/trial.py's definition) — not raw draft API calls. A redraft or a
second draft event on the same email never inflates the EMAILS figure.

Run: python -m pytest tests/test_usage.py -q
"""
from datetime import datetime, timezone

from app.usage import current_billing_period, handled_emails


def _ev(op, log_id=None):
    return {"operation": op, "email_log_id": log_id}


def test_handled_emails_counts_distinct_emails_not_calls():
    events = [
        _ev("draft", "a"), _ev("draft", "a"),      # same email drafted twice → 1
        _ev("draft", "b"),
        _ev("redraft", "b"),                       # redraft never counts
        _ev("embed_query"), _ev("embed_ingest"),   # non-draft ops never count
    ]
    assert handled_emails(events) == 2


def test_handled_emails_unlinked_drafts_count_once_each():
    # defensive: a draft event with no email_log row linked still counts one
    assert handled_emails([_ev("draft"), _ev("draft"), _ev("draft", "a")]) == 3


def test_handled_emails_empty():
    assert handled_emails([]) == 0


def test_billing_period_contains_now_and_anchors_day():
    started = datetime(2026, 6, 30, 7, 52, tzinfo=timezone.utc)
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    start, end = current_billing_period(started, now)
    assert (start.day, start.month) == (30, 6)
    assert (end.day, end.month) == (30, 7)
    assert start <= now < end


def test_billing_period_clamps_short_months():
    started = datetime(2026, 1, 31, tzinfo=timezone.utc)
    start, end = current_billing_period(started, datetime(2026, 2, 10, tzinfo=timezone.utc))
    assert (start.month, start.day) == (1, 31)
    assert (end.month, end.day) == (2, 28)     # 2026 is not a leap year


# ── Source attribution (2026-07-26): who triggered the spend ─────────────────
def test_source_for_maps_every_trigger_surface():
    from app.usage import source_for
    assert source_for("email", "draft") == "inbound_email"
    assert source_for("email", "redraft") == "owner"
    assert source_for("concierge", "concierge_business") == "owner"
    assert source_for("concierge", "concierge_discovery") == "owner"
    assert source_for("concierge", "concierge_discovery_rewrite") == "owner"
    assert source_for("concierge", "concierge_ask") == "prospect"
    assert source_for("portal", "portal_chat") == "portal"
    # The portal who-pays split (2026-07-26): unverified visitors and verified
    # customers' general questions are the PLATFORM's cost, not the business's
    assert source_for("portal", "portal_chat_unverified") == "portal_unverified"
    assert source_for("portal", "portal_chat_general") == "portal_general"
    assert source_for("scheduling", "reminder") == "automation"
    assert source_for("voice", "voice_learning") == "owner"
    assert source_for("email", "embed_query") == "retrieval"
    # module='portal' wins even for an operation mapped elsewhere or unknown
    assert source_for("portal", "future_portal_op") == "portal"
    # an unmapped operation surfaces as its own visible bucket, never mislabelled
    assert source_for("email", "brand_new_op") == "other"


def test_every_known_operation_is_source_mapped():
    """Every operation string recorded anywhere in app/ must have a source —
    a new call site that forgets the mapping fails HERE, not silently in the
    admin view."""
    import re
    from pathlib import Path
    from app.usage import SOURCE_BY_OPERATION
    ops = set()
    for p in Path("src/app").rglob("*.py"):
        ops |= set(re.findall(r"operation=[\"']([a-z_]+)[\"']", p.read_text(encoding="utf-8")))
    unmapped = ops - set(SOURCE_BY_OPERATION)
    assert not unmapped, f"operations missing a source mapping: {sorted(unmapped)}"


def test_summarize_breaks_cost_down_by_source(monkeypatch):
    import app.usage as usage
    events = [
        {"operation": "draft", "module": "email", "email_log_id": "a",
         "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        {"operation": "portal_chat", "module": "portal",
         "input_tokens": 200, "output_tokens": 80, "cost_usd": 0.02},
        {"operation": "reminder", "module": "scheduling",
         "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001},
    ]
    monkeypatch.setattr(usage.db, "get_tenant", lambda *a, **k: {
        "plan": "starter", "billing_started_at": "2026-07-01T00:00:00+00:00"})
    monkeypatch.setattr(usage.db, "list_usage_events", lambda *a, **k: list(events))
    monkeypatch.setattr(usage.db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(usage.db, "list_branches", lambda *a, **k: [])
    s = usage.summarize("t1")
    assert set(s["by_source"]) == {"inbound_email", "portal", "automation"}
    assert s["by_source"]["portal"]["cost"] == 0.02
    assert s["by_source"]["portal"]["events"] == 1
    assert s["by_op"]["draft"]["source"] == "inbound_email"


# ── Extra-mailbox allowance (2026-07-04): each PAID extra mailbox adds 300 ────
def _mailbox_env(monkeypatch, boxes, branches=0):
    import app.usage as usage
    monkeypatch.setattr(usage.db, "list_mailboxes", lambda *a, **k: list(boxes))
    monkeypatch.setattr(usage.db, "list_branches",
                        lambda *a, **k: [{"id": f"b{i}"} for i in range(branches)])


def test_extra_mailbox_allowance_paid_extras_add_300(monkeypatch):
    from app.usage import extra_mailbox_allowance
    _mailbox_env(monkeypatch, [{"grandfathered": False}] * 3)   # Core includes 1 → 2 extras
    t = {"plan": "starter", "subscription_status": "active"}
    assert extra_mailbox_allowance(t, "t1") == 600


def test_extra_mailbox_allowance_grandfathered_and_free_add_nothing(monkeypatch):
    from app.usage import extra_mailbox_allowance
    # grandfathered boxes never count — same rule as billing.sync_mailbox_addon
    _mailbox_env(monkeypatch, [{"grandfathered": False}, {"grandfathered": True}])
    assert extra_mailbox_allowance({"plan": "starter", "subscription_status": "active"}, "t1") == 0
    # no active subscription (pilot) → nothing, regardless of mailboxes
    _mailbox_env(monkeypatch, [{"grandfathered": False}] * 3)
    assert extra_mailbox_allowance({"plan": "pilot", "subscription_status": None}, "t1") == 0


def test_extra_mailbox_allowance_enterprise_unlimited(monkeypatch):
    from app.usage import extra_mailbox_allowance
    _mailbox_env(monkeypatch, [{"grandfathered": False}] * 10)
    assert extra_mailbox_allowance({"plan": "enterprise", "subscription_status": "active"}, "t1") == 0


# ── Additional-depot allowance (2026-07-11): each PAID extra depot adds 1,000 ─
def test_extra_depot_allowance_paid_extras_add_1000(monkeypatch):
    from app.usage import extra_depot_allowance
    _mailbox_env(monkeypatch, [], branches=3)   # first depot included → 2 billable
    t = {"plan": "professional", "subscription_status": "active"}
    assert extra_depot_allowance(t, "t1") == 2_000


def test_extra_depot_allowance_free_and_enterprise_add_nothing(monkeypatch):
    from app.usage import extra_depot_allowance
    _mailbox_env(monkeypatch, [], branches=3)
    assert extra_depot_allowance({"plan": "pilot", "subscription_status": None}, "t1") == 0
    assert extra_depot_allowance({"plan": "enterprise", "subscription_status": "active"}, "t1") == 0


def test_billable_depot_includes_its_own_mailbox(monkeypatch):
    # The £149 depot price includes the depot's mailbox: a 3-depot professional
    # tenant has 2 plan mailboxes + 2 depot mailboxes included, so 4 connected
    # mailboxes bill NO mailbox add-on and a 5th bills exactly one.
    from app.usage import extra_mailbox_allowance
    t = {"plan": "professional", "subscription_status": "active"}
    _mailbox_env(monkeypatch, [{"grandfathered": False}] * 4, branches=3)
    assert extra_mailbox_allowance(t, "t1") == 0
    _mailbox_env(monkeypatch, [{"grandfathered": False}] * 5, branches=3)
    assert extra_mailbox_allowance(t, "t1") == 300
