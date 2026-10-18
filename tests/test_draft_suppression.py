"""Draft suppression — stop drafting for contacts the owner ignores (2026-07-26).

Pins the contract end to end (docs/modules/module-draft-suppression.md):
  * the streak: +1 per queued draft, flip to 'suppressed' at 3 (with the Agent
    Log event), reset + reactivate on a send, permanent ignore survives a send;
  * the pipeline guard: a suppressed sender's inbound is logged under FYI with
    NO model call; the auto-escalation fuse flips to permanently_ignored after
    2 more inbounds; the re-engagement valve (urgent subject / 3x length)
    drafts one-off and forces the row into the queue;
  * one-off drafts (re-engagement) never advance the streak; informational
    (fyi/automated) inbounds never advance it either;
  * the escalation banner renders on the thread with its three actions, and
    the routes drive resume / ignore-permanently / draft-now correctly;
  * the Ignored page lists paused contacts; the concierge search sidecar
    surfaces them; a stats failure fails open (drafts as before).

Run: python -m pytest tests/test_draft_suppression.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.pipeline as pipeline
from app import agent, concierge_data, events, suppression
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── a tiny in-memory contact_draft_stats store ───────────────────────────────
class Store:
    """Fake contact_draft_stats table + Agent Log recorder, patched onto app.db
    (suppression, pipeline and dashboard all resolve the same module object)."""

    def __init__(self, monkeypatch, rows: dict | None = None, body_lengths=()):
        self.rows: dict[tuple, dict] = dict(rows or {})
        self.events: list = []
        monkeypatch.setattr(suppression.db, "get_contact_draft_stats",
                            lambda t, a, client=None: self.rows.get((t, a)))
        monkeypatch.setattr(suppression.db, "upsert_contact_draft_stats", self._upsert)
        monkeypatch.setattr(suppression.db, "recent_inbound_body_lengths",
                            lambda *a, **k: list(body_lengths))
        monkeypatch.setattr(suppression.db, "log_agent_event",
                            lambda t, kind, summary, **k: self.events.append(
                                {"kind": kind, "summary": summary, **k}))

    def _upsert(self, tenant_id, address, fields, client=None):
        row = self.rows.setdefault((tenant_id, address), {})
        row.update(fields)

    def row(self, tenant="t1", addr="perplexity@perplexity.ai"):
        return self.rows.get((tenant, addr), {})


SUPPRESSED = {"status": "suppressed", "consecutive_unsent_drafts": 3,
              "total_unsent_drafts": 3, "total_sent_drafts": 0,
              "emails_since_suppressed": 0}
ADDR = "perplexity@perplexity.ai"


# ── the streak ───────────────────────────────────────────────────────────────
def test_streak_increments_and_flips_to_suppressed_at_three(monkeypatch):
    s = Store(monkeypatch)
    for n in (1, 2):
        suppression.record_draft_generated("t1", ADDR)
        assert s.row()["consecutive_unsent_drafts"] == n
        assert "status" not in s.row()          # still active — nothing flipped
        assert s.events == []                   # no noise below the threshold
    suppression.record_draft_generated("t1", ADDR)
    assert s.row()["status"] == "suppressed"
    assert s.row()["consecutive_unsent_drafts"] == 3
    assert s.row()["suppression_reason"] == "threshold_reached"
    assert s.row()["emails_since_suppressed"] == 0
    assert [e["kind"] for e in s.events] == ["suppression"]
    assert "Paused drafting" in s.events[0]["summary"]


def test_streak_never_advances_for_a_non_active_contact(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    suppression.record_draft_generated("t1", ADDR)   # e.g. a Draft-now one-off
    assert s.row()["consecutive_unsent_drafts"] == 3


def test_a_send_resets_the_streak_and_reactivates_a_pause(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED, emails_since_suppressed=1)})
    suppression.record_sent("t1", ADDR)
    row = s.row()
    assert row["status"] == "active"
    assert row["consecutive_unsent_drafts"] == 0
    assert row["emails_since_suppressed"] == 0
    assert row["total_sent_drafts"] == 1


def test_a_send_does_not_lift_a_permanent_ignore(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED, status="permanently_ignored")})
    suppression.record_sent("t1", ADDR)
    assert s.row()["status"] == "permanently_ignored"    # only Resume lifts it
    assert s.row()["consecutive_unsent_drafts"] == 0


def test_a_send_for_an_untracked_contact_is_a_noop(monkeypatch):
    s = Store(monkeypatch)
    suppression.record_sent("t1", "new@customer.com")
    assert s.rows == {}


def test_resume_and_ignore_permanently(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    suppression.resume("t1", ADDR)
    assert s.row()["status"] == "active" and s.row()["consecutive_unsent_drafts"] == 0
    suppression.ignore_permanently("t1", ADDR, by="owner")
    assert s.row()["status"] == "permanently_ignored"
    assert s.row()["suppressed_by"] == "owner"


def test_addresses_normalise_to_lowercase(monkeypatch):
    s = Store(monkeypatch)
    suppression.record_draft_generated("t1", "  Ops@Acme.CO.uk ")
    assert ("t1", "ops@acme.co.uk") in s.rows


# ── check_inbound: the pre-LLM gate ──────────────────────────────────────────
def test_active_or_unknown_contacts_pass_through(monkeypatch):
    s = Store(monkeypatch)
    assert suppression.check_inbound("t1", ADDR, "s", "b")["action"] == "draft"
    s.rows[("t1", ADDR)] = {"status": "active", "consecutive_unsent_drafts": 2}
    assert suppression.check_inbound("t1", ADDR, "s", "b")["action"] == "draft"


def test_suppressed_contact_is_suppressed_and_the_fuse_burns(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    out = suppression.check_inbound("t1", ADDR, "Weekly digest", "same as ever")
    assert out["action"] == "suppress"
    assert s.row()["emails_since_suppressed"] == 1
    assert s.row().get("status", "suppressed") == "suppressed"   # not yet
    # …the second silent inbound auto-escalates (5 emails total, owner silent):
    out = suppression.check_inbound("t1", ADDR, "Weekly digest", "same as ever")
    assert out["action"] == "suppress"
    assert s.row()["status"] == "permanently_ignored"
    assert s.row()["suppressed_by"] == "auto"
    auto = [e for e in s.events if "Auto-suppressed" in e["summary"]]
    assert len(auto) == 1 and "5 emails" in auto[0]["summary"]


def test_permanently_ignored_stays_suppressed_silently(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED, status="permanently_ignored")})
    out = suppression.check_inbound("t1", ADDR, "Weekly digest", "same as ever")
    assert out["action"] == "suppress"
    assert s.events == []                        # no banner, no log — decided


@pytest.mark.parametrize("subject", ["URGENT: account closing", "Final notice",
                                     "Complaint about last week", "please cancel"])
def test_urgency_keywords_break_suppression(monkeypatch, subject):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    out = suppression.check_inbound("t1", ADDR, subject, "short")
    assert out["action"] == "reengage"
    assert any("looks different" in e["summary"] for e in s.events)
    assert s.row()["emails_since_suppressed"] == 1    # the fuse still burned


def test_an_unusually_long_email_breaks_suppression(monkeypatch):
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)}, body_lengths=[100, 120, 80])
    assert suppression.check_inbound("t1", ADDR, "hello", "x" * 400)["action"] == "reengage"


def test_a_normal_length_email_stays_suppressed(monkeypatch):
    Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)}, body_lengths=[100, 120, 80])
    assert suppression.check_inbound("t1", ADDR, "hello", "x" * 150)["action"] == "suppress"


def test_a_stats_failure_fails_open_to_drafting(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(suppression.db, "get_contact_draft_stats", _boom)
    assert suppression.check_inbound("t1", ADDR, "s", "b")["action"] == "draft"


# ── the pipeline wiring ──────────────────────────────────────────────────────
class Env:
    """The test_pipeline_events harness, plus recorders for the suppression
    seams and a controllable model category."""

    def __init__(self, monkeypatch, *, auto_send=False, model_category="needs_reply"):
        self.model_calls = []
        self.inserts = []
        self.sent = []

        async def fake_draft(cfg, email, **kw):
            self.model_calls.append(email)
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1},
                                     model="m", category=model_category)

        async def fake_send(*a, **k):
            self.sent.append(a)

        async def fake_retrieve(*a, **k):
            return []

        def fake_insert(*args, **kwargs):
            self.inserts.append((args, kwargs))
            return "log-1", True

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.mailbox, "send_reply", fake_send)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"mailbox": "me@biz.co"})
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"auto_send": auto_send})
        monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: {"me@biz.co"})
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                            lambda *a, **k: {"activities": 0, "contact_id": None, "company_id": None})
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "insert_email_log", fake_insert)
        monkeypatch.setattr(pipeline.db, "mark_email_log_sent", lambda *a, **k: None)


EMAIL = {"from_address": ADDR, "from_name": "Perplexity", "subject": "Weekly digest",
         "body": "This week in AI…", "thread_id": "th-9"}


@pytest.fixture(autouse=True)
def _clean_bus():
    events._subscribers.clear()
    events._drafting.clear()
    yield
    events._subscribers.clear()
    events._drafting.clear()


def test_suppressed_sender_costs_no_model_call_and_files_under_fyi(monkeypatch):
    env = Env(monkeypatch)
    Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    out = asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                               mailbox_id="mb1"))
    assert out is None
    assert env.model_calls == []                      # the whole point: zero LLM spend
    assert len(env.inserts) == 1
    args, kwargs = env.inserts[0]
    assert kwargs["status"] == "skipped"
    assert kwargs["category"] == "fyi"
    assert "Drafting paused" in kwargs["category_reason"]
    assert kwargs["source_message_id"] == "m1"        # deduped like any inbound


def test_under_threshold_drafts_normally_and_extends_the_streak(monkeypatch):
    env = Env(monkeypatch)
    s = Store(monkeypatch, {("t1", ADDR): {"status": "active",
                                           "consecutive_unsent_drafts": 1,
                                           "total_unsent_drafts": 1}})
    out = asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert out == "log-1"
    assert len(env.model_calls) == 1                  # no regression: still drafts
    assert s.row()["consecutive_unsent_drafts"] == 2


def test_an_informational_inbound_never_extends_the_streak(monkeypatch):
    Env(monkeypatch, model_category="fyi")
    s = Store(monkeypatch)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert s.rows == {}                               # no draft to ignore → no count


def test_reengagement_drafts_one_off_into_the_queue_without_counting(monkeypatch):
    env = Env(monkeypatch, model_category="fyi")      # even an fyi call can't bury it
    s = Store(monkeypatch, {("t1", ADDR): dict(SUPPRESSED)})
    out = asyncio.run(pipeline.draft_for_email(
        "t1", {**EMAIL, "subject": "URGENT: please respond"}, source_message_id="m1"))
    assert out == "log-1"
    assert len(env.model_calls) == 1                  # the one-off draft happened
    args, kwargs = env.inserts[0]
    assert kwargs["status"] == "pending"              # forced into the approval queue
    assert kwargs["category"] == "needs_reply"
    assert "Unusual email" in kwargs["category_reason"]
    assert s.row()["consecutive_unsent_drafts"] == 3  # the streak did not move


def test_auto_send_resets_the_streak_straight_back(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    s = Store(monkeypatch, {("t1", ADDR): {"status": "active",
                                           "consecutive_unsent_drafts": 1,
                                           "total_unsent_drafts": 1}})
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         auto_send_ok=True))
    assert env.sent                                   # the reply went out
    assert s.row()["consecutive_unsent_drafts"] == 0  # counted, then reset by the send
    assert s.row()["total_sent_drafts"] == 1


# ── the routes + templates ───────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _thread_env(monkeypatch, stats):
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [
        {"id": "log-1", "status": "skipped", "body_in": "Digest", "subject": "Weekly",
         "created_at": "2026-07-24T09:00:00+00:00", "category": "fyi",
         "category_source": "auto", "draft_reply": ""}])
    monkeypatch.setattr(dashboard.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": []})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_contact_draft_stats",
                        lambda t, a, client=None: stats)


def test_banner_renders_on_a_suppressed_thread_with_all_three_actions(client, monkeypatch):
    _thread_env(monkeypatch, dict(SUPPRESSED))
    html = client.get("/app/conversations/view", params={"contact": ADDR}).text
    assert "You haven&#39;t responded to" in html or "You haven't responded to" in html
    assert "in 3 emails" in html
    assert "/app/suppression/resume" in html
    assert "/app/suppression/ignore" in html
    assert "/app/suppression/draft-now" in html


def test_no_banner_when_active_or_permanently_ignored(client, monkeypatch):
    for stats in (None, dict(SUPPRESSED, status="permanently_ignored")):
        _thread_env(monkeypatch, stats)
        html = client.get("/app/conversations/view", params={"contact": ADDR}).text
        assert "suppression-banner" not in html


def test_resume_route_resets(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.suppression, "resume",
                        lambda t, c, client=None: seen.update(t=t, c=c))
    r = client.post("/app/suppression/resume", data={"contact": ADDR},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen == {"t": "t1", "c": ADDR}


def test_ignore_route_is_owner_attributed(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.suppression, "ignore_permanently",
                        lambda t, c, by, client=None: seen.update(t=t, c=c, by=by))
    r = client.post("/app/suppression/ignore", data={"contact": ADDR},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen == {"t": "t1", "c": ADDR, "by": "owner"}


def test_draft_now_redrafts_the_latest_email_and_reopens_it(client, monkeypatch):
    calls = {}

    async def fake_redraft(tenant_id, log_id, client=None):
        calls["redraft"] = (tenant_id, log_id)
        return "draft"

    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [
        {"id": "log-old", "status": "sent"}, {"id": "log-new", "status": "skipped"}])
    monkeypatch.setattr(dashboard.pipeline, "redraft_email_log", fake_redraft)
    monkeypatch.setattr(dashboard.db, "set_email_log_status",
                        lambda log_id, status, tenant_id, client=None:
                        calls.update(status=(log_id, status)))
    monkeypatch.setattr(dashboard.suppression, "record_draft_generated",
                        lambda *a, **k: pytest.fail("Draft now must not touch the streak"))
    r = client.post("/app/suppression/draft-now", data={"contact": ADDR},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calls["redraft"] == ("t1", "log-new")          # the LATEST email
    assert calls["status"] == ("log-new", "pending")      # back into the queue


def test_ignored_page_lists_paused_contacts_with_unignore(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_ignored", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_draft_suppressed", lambda *a, **k: [
        {"address": ADDR, "status": "suppressed", "consecutive_unsent_drafts": 3,
         "total_unsent_drafts": 3, "emails_since_suppressed": 0,
         "suppressed_at": "2026-07-24T09:00:00+00:00", "suppressed_by": None},
        {"address": "digest@github.com", "status": "permanently_ignored",
         "consecutive_unsent_drafts": 3, "total_unsent_drafts": 3,
         "emails_since_suppressed": 2,
         "suppressed_at": "2026-07-25T09:00:00+00:00", "suppressed_by": "auto"}])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    html = client.get("/app/ignored").text
    assert "Drafting paused" in html
    assert ADDR in html and "digest@github.com" in html
    assert "3 drafts went unanswered" in html
    assert "automatically" in html                        # the auto-suppressed one says so
    assert "/app/suppression/resume" in html


# ── concierge sidecar ────────────────────────────────────────────────────────
def test_search_conversations_carries_the_suppressed_sidecar(monkeypatch):
    monkeypatch.setattr(concierge_data.db, "search_email_log", lambda *a, **k: [])
    monkeypatch.setattr(concierge_data.db, "list_draft_suppressed", lambda *a, **k: [
        {"address": ADDR, "status": "suppressed", "consecutive_unsent_drafts": 3,
         "emails_since_suppressed": 1, "suppressed_at": "2026-07-24T09:00:00+00:00"}])
    out = asyncio.run(concierge_data._search_conversations("t1", None, {}))
    assert out["suppressed_contacts"] == [{
        "contact_email": ADDR, "status": "drafting paused", "unanswered_drafts": 3,
        "emails_since_paused": 1, "since": "2026-07-24"}]


def test_no_sidecar_when_nothing_is_suppressed(monkeypatch):
    monkeypatch.setattr(concierge_data.db, "search_email_log", lambda *a, **k: [])
    out = asyncio.run(concierge_data._search_conversations("t1", None, {}))
    assert "suppressed_contacts" not in out


def test_agent_log_filter_knows_the_new_kind():
    assert "suppression" in dashboard._AGENT_LOG_KIND_SET
