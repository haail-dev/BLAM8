"""The go-live hold: drafting is off until the owner says go (Kris 2026-09-01).

Onboarding used to finish and the agent would start drafting the same minute —
before the owner had walked their loaded history and marked the newsletters,
suppliers and no-reply robots they never want answered. Tokens spent on mail
that was never going to be replied to, and a first impression made of drafts
nobody asked for.

So a new tenant lands held, triages, and releases drafting themselves. Sending
mode became a ladder to hold it: not yet → drafts I approve → full-auto.

Everything here is really one worry in three shapes — a hold that stops the
wrong agent, or never stops the right one:

  * an EXISTING tenant must never be held by the deploy (the migration's
    backfill, checked against prod at the time);
  * held mail must still ARRIVE — filed, threaded, readable — or the hold looks
    like the product silently doing nothing;
  * a save that doesn't mention the ladder must not move it.

Run: python -m pytest tests/test_draft_hold.py -q
"""
import re

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.pipeline as pipeline
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── The gate ───────────────────────────────────────────────────────────────
@pytest.fixture
def held(monkeypatch):
    """A tenant with drafting held, and a record of what got filed."""
    filed = []
    monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"draft_hold": True})
    monkeypatch.setattr(pipeline.db, "insert_email_log",
                        lambda *a, **k: filed.append(k) or ("log1", True))
    return filed


def test_a_held_tenant_drafts_nothing(held):
    assert pipeline._cfg_draft_hold("t1") is True


def test_a_released_tenant_drafts(monkeypatch):
    monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"draft_hold": False})
    assert pipeline._cfg_draft_hold("t1") is False


def test_a_tenant_with_no_flag_at_all_drafts(monkeypatch):
    """Absent means not held. The safe direction when we cannot tell is to
    behave exactly as before the feature existed."""
    monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {})
    assert pipeline._cfg_draft_hold("t1") is False


def test_a_config_read_that_breaks_does_not_silence_the_agent(monkeypatch):
    """A tenant wrongly held is the product appearing to do nothing at all,
    which is far worse than a few tokens spent. So a failed read drafts."""
    def boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(pipeline.db, "get_tenant_config", boom)
    assert pipeline._cfg_draft_hold("t1") is False


# ── The ladder ─────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _save_env(monkeypatch, existing):
    saved = {}
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: dict(existing))
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set())
    return saved


@pytest.mark.parametrize("mode, hold, auto", [
    ("hold", True, False),
    ("review", False, False),
    ("auto", False, True),
])
def test_each_rung_sets_both_flags(client, monkeypatch, mode, hold, auto):
    """One radio drives two booleans, so they can never disagree — there is no
    'held AND full-auto' to reason about."""
    saved = _save_env(monkeypatch, {})
    client.post("/app/settings", data={"send_mode": mode}, follow_redirects=False)
    assert saved["draft_hold"] is hold
    assert saved["auto_send"] is auto


@pytest.mark.parametrize("existing, stays", [
    ({"draft_hold": True}, ("hold", True, False)),
    ({"draft_hold": False, "auto_send": True}, ("auto", False, True)),
    ({"draft_hold": False, "auto_send": False}, ("review", False, False)),
])
def test_a_save_without_the_field_leaves_the_ladder_alone(client, monkeypatch, existing, stays):
    """A stale tab or a replayed POST arrives without send_mode. Defaulting to
    'hold' there would silently stop a working agent drafting; defaulting to
    'review' would release a tenant mid-triage. So it reads the current rung and
    writes it back."""
    saved = _save_env(monkeypatch, existing)
    client.post("/app/settings", data={"tone": "warm"}, follow_redirects=False)
    _, hold, auto = stays
    assert saved["draft_hold"] is hold
    assert saved["auto_send"] is auto


def test_a_junk_mode_is_ignored_rather_than_obeyed(client, monkeypatch):
    saved = _save_env(monkeypatch, {"draft_hold": False, "auto_send": True})
    client.post("/app/settings", data={"send_mode": "sudo-send-everything"},
                follow_redirects=False)
    assert saved["draft_hold"] is False
    assert saved["auto_send"] is True


# ── Onboarding hands over held ─────────────────────────────────────────────
def test_finishing_onboarding_holds_drafting(monkeypatch):
    import app.onboarding as onboarding
    saved = {}
    monkeypatch.setattr(onboarding.db, "set_onboarded", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    onboarding._finish(TenantCtx(tenant_id="t1", client=None))
    assert saved == {"draft_hold": True}


def test_a_broken_config_write_still_lets_signup_finish(monkeypatch):
    """The column already defaults to held, so this write is the belt to that
    braces — it must never 500 the last step of signing up."""
    import app.onboarding as onboarding
    monkeypatch.setattr(onboarding.db, "set_onboarded", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("write failed")
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", boom)
    r = onboarding._finish(TenantCtx(tenant_id="t1", client=None))
    assert r.status_code == 303


def test_a_brand_new_tenant_is_born_held(monkeypatch):
    """Julius 2026-09-02: the hold starts at tenant CREATION, not wizard
    finish — mail arriving mid-wizard (or after an abandoned wizard) must be
    held too. The finish-step stamp stays as belt-and-braces."""
    from types import SimpleNamespace

    from app import db as real_db

    class _Table:
        def __init__(self, name):
            self.name = name

        def insert(self, row):
            self.row = row
            return self

        def execute(self):
            return SimpleNamespace(data=[{"id": "t-new", **self.row}])

    monkeypatch.setattr(real_db, "db", lambda: SimpleNamespace(table=_Table))
    monkeypatch.setattr(real_db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(real_db, "apply_plan_modules", lambda *a, **k: None)
    saved = {}
    monkeypatch.setattr(real_db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    import app.push as push
    monkeypatch.setattr(push, "notify_signup", lambda *a, **k: None)
    assert real_db.ensure_tenant_for_user("u1", "Acme", "a@x.co") == "t-new"
    assert saved["draft_hold"] is True


def test_the_concierge_knows_the_hold_rung(monkeypatch):
    """The help assistant told a held tenant 'draft-first mode is on' (live
    e2e 2026-09-02) — the sending ladder has THREE rungs and the prompt must
    say which one they're on."""
    from app import concierge
    held = concierge._customer_block({
        "name": "Willowbrook", "setup": {"draft_hold": True, "auto_send": False}})
    assert "ON HOLD" in held and "never tell them drafting is already on" in held
    released = concierge._customer_block({
        "name": "Willowbrook", "setup": {"draft_hold": False, "auto_send": False}})
    assert "draft-first" in released and "ON HOLD" not in released


def test_the_connect_screen_does_not_promise_drafts(client, monkeypatch):
    """onboarding/mailbox said 'Auto-draft is on' the moment Gmail connected —
    while the tenant was held. The connected state now tells the truth: mail
    files, drafting waits for them in Settings."""
    import app.onboarding as onboarding
    monkeypatch.setattr(onboarding.db, "get_tenant",
                        lambda *a, **k: {"mailbox": "hello@acme.co.uk",
                                         "provider": "google"})
    monkeypatch.setattr(onboarding.db, "get_connection_status",
                        lambda *a, **k: {"connected": True})
    from app.auth import require_tenant
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        r = client.get("/onboarding/mailbox")
    finally:
        app.dependency_overrides.pop(require_tenant, None)
    assert r.status_code == 200
    assert "Auto-draft is on" not in r.text
    assert "Recent mail is filed without replying" in r.text
    assert "sending settings before enabling drafting" in r.text


# ── The two facts stop contradicting each other ────────────────────────────
def _settings_env(monkeypatch, hold):
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {"draft_hold": hold})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "google", "mailbox": "hello@acme.co.uk", "portal_slug": "acme"})
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: {"sub_expires": None})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.portal, "ensure_portal_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(dashboard.storage, "banner_public_url", lambda *a, **k: None)


def test_a_held_mailbox_does_not_claim_to_be_auto_drafting(client, monkeypatch):
    """Kris 2026-09-01: "I changed the draft reply mode but the Mailbox still
    shows Auto-draft active?" Two different facts sharing one name — the mailbox
    WATCH being live says mail reaches us, not that a reply gets written. Held,
    the fold was stating the opposite of what the agent was doing."""
    _settings_env(monkeypatch, hold=True)
    fold = client.get("/app/settings").text.split(
        'data-fold="Mailbox connection"', 1)[1].split("</details>", 1)[0]
    assert "Auto-draft active" not in fold
    assert "new mail is drafted automatically" not in fold
    assert "nothing is drafted" in fold


def test_a_released_mailbox_still_says_auto_draft_active(client, monkeypatch):
    _settings_env(monkeypatch, hold=False)
    fold = client.get("/app/settings").text.split(
        'data-fold="Mailbox connection"', 1)[1].split("</details>", 1)[0]
    assert "Auto-draft active" in fold


# ── What the inbox says while it is holding ────────────────────────────────
def _inbox_env(monkeypatch, held, convos=None, temps=None):
    convos = convos if convos is not None else [{
        "contact": "sarah@x.com", "state": "new", "subject": "Boiler quote",
        "mailbox_id": None, "count": 1, "last_at": "2026-09-01T09:00:00+00:00",
        "thread_id": "thread-A",
        "category": "needs_reply",
        "category_reason": "Drafting is off while you set up — turn it on in Settings"}]
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {"draft_hold": held})
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "crm_contact_temperatures",
                        lambda *a, **k: temps or {})
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)


def test_the_inbox_says_once_why_nothing_is_drafted(client, monkeypatch):
    """Kris 2026-09-01: "there was no sign of why the Agent wasn't writing a
    draft". Every row is un-drafted for the same reason, so the page says it
    rather than leaving each card to imply it."""
    _inbox_env(monkeypatch, held=True)
    html = client.get("/app/conversations").text
    # a phrase only the notice uses — "Drafting is off" also appears in the
    # row's tooltip, where it is the stored category_reason
    assert "Mail arrives and files as normal" in html
    assert "/app/settings" in html and "/app/ignored" in html


def test_no_such_notice_once_drafting_is_on(client, monkeypatch):
    _inbox_env(monkeypatch, held=False)
    assert "Mail arrives and files as normal" not in client.get("/app/conversations").text


def test_held_rows_glow_quietly_rather_than_rose(client, monkeypatch):
    """A wall of rose reads as a wall of alarm. Nothing is waiting on the agent
    while it is held, so nothing pulses like it is — but the CATEGORY is
    untouched, so the chip, the counts and the filter still work."""
    _inbox_env(monkeypatch, held=True)
    html = client.get("/app/conversations").text
    # scoped to the ROW markup — the .glow-* CSS rules are always in the page
    rows = re.findall(r'class="convo-swipe[^"]*"', html)
    assert rows
    assert all("glow-held" in r for r in rows), rows
    assert not any("glow-needs_reply" in r for r in rows), rows
    assert ">Needs Reply <" in html      # the filter chip is still there


def test_a_stale_mood_does_not_flash_over_unread_mail(client, monkeypatch):
    """Mood lives on the CONTACT, not the conversation, so an angry email this
    morning tints everything that person sends afterwards. Held, crm_intel
    returns before it ever sees the mail — so the mood on screen was measured on
    a DIFFERENT email. Better no temperature than one we never took."""
    # the read came from THIS conversation, so it is allowed to tint it
    temps = {"sarah@x.com": {"mood": "frustrated", "trend": "worsening",
                             "reason": "wants a refund", "thread_id": "thread-A"}}
    def row_classes(html):
        return " ".join(re.findall(r'class="convo-swipe[^"]*"', html))
    _inbox_env(monkeypatch, held=True, temps=temps)
    assert "mood-hot" not in row_classes(client.get("/app/conversations").text)
    _inbox_env(monkeypatch, held=False, temps=temps)
    assert "mood-hot" in row_classes(client.get("/app/conversations").text)


def test_the_reason_is_no_longer_hidden_on_a_new_sender(client, monkeypatch):
    """The tooltip gated category_reason on state != 'new' — precisely the case
    where "the agent didn't write this one, and here is why" matters most."""
    _inbox_env(monkeypatch, held=True)
    html = client.get("/app/conversations").text
    assert "Drafting is off while you set up" in html


# ── Mood belongs to the thread it was read from ────────────────────────────
ANGRY = {"mood": "frustrated", "trend": "worsening", "reason": "wants a refund"}


def _convo(thread):
    return [{"contact": "sarah@x.com", "state": "new", "subject": "Boiler quote",
             "mailbox_id": None, "count": 1, "thread_id": thread,
             "last_at": "2026-09-01T09:00:00+00:00", "category": "needs_reply"}]


def test_a_mood_read_elsewhere_does_not_tint_this_conversation(client, monkeypatch):
    """The exact shape of Kris's report: a refund row's temper washing red over
    a cheerful boiler enquiry sent hours later by the same person."""
    _inbox_env(monkeypatch, held=False, convos=_convo("thread-BOILER"),
               temps={"sarah@x.com": {**ANGRY, "thread_id": "thread-REFUND"}})
    rows = " ".join(re.findall(r'class="convo-swipe[^"]*"',
                               client.get("/app/conversations").text))
    assert "mood-hot" not in rows


def test_it_still_tints_the_conversation_it_came_from(client, monkeypatch):
    """Scoping must not mean silencing — an angry thread should still look it."""
    _inbox_env(monkeypatch, held=False, convos=_convo("thread-REFUND"),
               temps={"sarah@x.com": {**ANGRY, "thread_id": "thread-REFUND"}})
    rows = " ".join(re.findall(r'class="convo-swipe[^"]*"',
                               client.get("/app/conversations").text))
    assert "mood-hot" in rows


def test_a_read_with_no_thread_tints_nothing(client, monkeypatch):
    """Rows written before the column exists. A read we cannot place is one we
    cannot honestly attribute — and the next inbound re-stamps it anyway."""
    _inbox_env(monkeypatch, held=False, convos=_convo("thread-BOILER"),
               temps={"sarah@x.com": {**ANGRY, "thread_id": None}})
    rows = " ".join(re.findall(r'class="convo-swipe[^"]*"',
                               client.get("/app/conversations").text))
    assert "mood-hot" not in rows


def test_the_crm_keeps_the_rolling_read(monkeypatch):
    """Only the Inbox narrowed. "This customer has been getting angrier" is
    exactly what a per-thread read cannot say, and the CRM still wants it."""
    import app.crm_intel as crm_intel
    written = {}
    monkeypatch.setattr(crm_intel.db, "crm_get_contact", lambda *a, **k: {"sentiment": "neutral"})
    monkeypatch.setattr(crm_intel.db, "crm_update_contact",
                        lambda cid, tid, fields: written.update(fields))
    monkeypatch.setattr(crm_intel.db, "crm_stamp_activity_sentiment", lambda *a, **k: None)
    monkeypatch.setattr(crm_intel.db, "get_email_log_entry",
                        lambda *a, **k: {"thread_id": "thread-REFUND"})
    crm_intel.apply_sentiment("t1", {"mood": "frustrated", "confidence": 0.9,
                                     "trigger": "wants a refund"},
                              contact_id="c1", email_log_id="log1")
    assert written["sentiment"] == "frustrated"
    assert written["sentiment_trend"] == "worsening"      # the rolling arc survives
    assert written["sentiment_thread_id"] == "thread-REFUND"


def test_an_unreadable_thread_still_records_the_mood(monkeypatch):
    """The thread lookup is a nicety; losing it must not lose the mood."""
    import app.crm_intel as crm_intel
    written = {}
    monkeypatch.setattr(crm_intel.db, "crm_get_contact", lambda *a, **k: {})
    monkeypatch.setattr(crm_intel.db, "crm_update_contact",
                        lambda cid, tid, fields: written.update(fields))
    monkeypatch.setattr(crm_intel.db, "crm_stamp_activity_sentiment", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(crm_intel.db, "get_email_log_entry", boom)
    crm_intel.apply_sentiment("t1", {"mood": "angry", "confidence": 0.9, "trigger": "x"},
                              contact_id="c1", email_log_id="log1")
    assert written["sentiment"] == "angry"
    assert written["sentiment_thread_id"] is None


def test_the_notice_links_to_the_row_not_the_page(client, monkeypatch):
    """Kris 2026-09-01: "it would be nice if it took me to the exact location on
    screen". Settings is a dozen folded rows deep; landing at the top and
    hunting for Sending mode is the thing the link is meant to save."""
    _inbox_env(monkeypatch, held=True)
    assert "/app/settings#sending-mode" in client.get("/app/conversations").text


def test_the_row_it_points_at_exists_and_can_be_opened(client, monkeypatch):
    """A fragment is only as good as its target — and a <details> the browser
    scrolls to stays SHUT unless something opens it, because auto-expansion only
    applies to a target inside one."""
    _settings_env(monkeypatch, hold=True)
    html = client.get("/app/settings").text
    assert 'id="sending-mode"' in html
    assert "if (target.tagName === 'DETAILS') target.open = true;" in html


# ── Held mail still reaches the CRM (Julius 2026-09-03) ─────────────────────
# A whole held period's senders were invisible: the born-held gate filed the
# email and returned before ANY CRM work, and the redraft that finally wrote a
# draft didn't backfill. Now the hold path runs the zero-LLM ingest (contact +
# timeline, not a token spent) and the redraft path runs the full enrichment.
def _held_env(monkeypatch, filed, ingested, ingest=None):
    monkeypatch.setattr(pipeline.db, "get_tenant_config",
                        lambda *a, **k: {"draft_hold": True})
    monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
    monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: set())
    monkeypatch.setattr(pipeline.db, "insert_email_log",
                        lambda *a, **k: filed.append(k) or ("held-1", True))
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                        ingest or (lambda tid, row: ingested.append(row)
                                   or {"contact_id": "c1", "company_id": None}))


def test_held_mail_still_reaches_the_crm(monkeypatch):
    import asyncio
    filed, ingested = [], []
    _held_env(monkeypatch, filed, ingested)
    out = asyncio.run(pipeline.draft_for_email(
        "t1", {"from_address": "family@x.co", "subject": "Headstone",
               "body": "hello"}, source_message_id="m1"))
    assert out is None
    assert filed and filed[0]["status"] == "skipped"
    assert ingested[0]["id"] == "held-1"
    assert ingested[0]["from_address"] == "family@x.co"
    assert ingested[0]["status"] == "skipped"


def test_a_broken_crm_never_breaks_the_held_filing(monkeypatch):
    import asyncio
    filed = []

    def boom(tid, row):
        raise RuntimeError("crm down")
    _held_env(monkeypatch, filed, [], ingest=boom)
    out = asyncio.run(pipeline.draft_for_email(
        "t1", {"from_address": "family@x.co", "subject": "s", "body": "b"},
        source_message_id="m2"))
    assert out is None and len(filed) == 1     # filing survived the CRM outage


def test_redraft_backfills_the_crm(monkeypatch):
    """The redraft of a held-then-released thread runs ingest + field
    extraction + intel — the first real CRM pass that thread ever gets."""
    import asyncio
    from types import SimpleNamespace
    from app import crm_extract, crm_fields, crm_intel

    entry = {"id": "log9", "from_address": "family@x.co", "from_name": "Kris Deane",
             "subject": "Headstone", "kind": "inbound", "body_in": "hello", "mailbox_id": None,
             "thread_id": None, "status": "skipped"}
    for name, value in (("get_email_log_entry", entry),
                        ("effective_config", {}), ("get_tenant", {"industry": "trades"}),
                        ("get_conversation", []), ("active_modules", set()),
                        ("get_scheduling_config", {}),
                        ("bookings_for_contact", []),
                        ("crm_list_field_definitions",
                         [{"entity": "contact", "key": "d", "label": "D",
                           "field_type": "text", "options": [],
                           "extraction_hint": "x"}])):
        monkeypatch.setattr(pipeline.db, name, lambda *a, _v=value, **k: _v)
    monkeypatch.setattr(pipeline.db, "module_active", lambda tid, m, **k: True)
    monkeypatch.setattr(pipeline.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.db, "resolve_holds_for_log", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline.kb, "retrieve",
                        lambda *a, **k: _async([]) , raising=True)

    async def _kb_retrieve(*a, **k):
        return []
    monkeypatch.setattr(pipeline.kb, "retrieve", _kb_retrieve)
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_merge_portal_history",
                        lambda tid, addr, h, client=None: h)
    monkeypatch.setattr(pipeline, "_party_context",
                        lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_scheduling_setup",
                        lambda *a, **k: (None, None, None, None,
                                         {"scheduling": False, "transport": False,
                                          "field_service": False, "storefront": False,
                                          "intake": "", "gather_intake": False}))
    monkeypatch.setattr(pipeline, "_log_agent_activity", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_trace_kb_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_draft(cfg, email, **kw):
        return SimpleNamespace(
            text="Dear family, ...", usage={}, model="m", deferred=False,
            missing="", blocked="", reasoning="", scheduling_requested=False,
            thread_state="in_progress", request_summary="Wants a headstone",
            action_summary="Explained", searched_for="", contact_name="Kris Deane",
            contact_company="", booking_details=(), category="needs_reply",
            category_reason="", request_id=None)
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)

    ingested, extracted_calls, intel_calls = [], [], []
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                        lambda tid, row: ingested.append(row)
                        or {"contact_id": "c1", "company_id": None})

    async def fake_extract(tid, defs, **kw):
        extracted_calls.append(defs)
        return []
    monkeypatch.setattr(crm_extract, "extract", fake_extract)
    monkeypatch.setattr(crm_fields, "apply_extraction", lambda *a, **k: {"autofilled": 0})

    async def fake_intel(tid, **kw):
        intel_calls.append(kw)
        return {}
    monkeypatch.setattr(crm_intel, "process_email", fake_intel)

    out = asyncio.run(pipeline.redraft_email_log("t1", "log9"))
    assert out is not None
    assert ingested and ingested[0]["id"] == "log9"
    assert ingested[0]["contact_name"] == "Kris Deane"
    # built-ins ride the extraction, intel ran on the contact
    assert extracted_calls and any(d.get("key") == "_phone" for d in extracted_calls[0])
    assert intel_calls and intel_calls[0]["contact_id"] == "c1"


def _async(v):
    async def _f(*a, **k):
        return v
    return _f
