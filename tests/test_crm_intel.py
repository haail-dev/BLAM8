import json
"""CRM intelligence — phase 3 (src/app/crm_intel.py + the pipeline/kanban/promise/
export surfaces).

Unit: analysis prompt + tolerant parsing, apply_deal's trust boundary (an
'existing' id must be one of the contact's own open deals), the new-deal
confidence floor, promise validation/dedup, drafter context building, the
per-tenant quiet threshold. Routes: stage move (status follows stage_type),
promise resolve, exports, the home "gone quiet" card. Anthropic mocked
throughout; db monkeypatched (house Env pattern).

Run: python -m pytest tests/test_crm_intel.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import agent, crm, crm_fields, crm_intel, events_os
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

OPEN_DEALS = [
    {"id": "d1", "title": "Weekly pallet run", "status": "open", "stage_id": "s1",
     "crm_stages": {"id": "s1", "name": "Prospect", "stage_type": "active"}},
]


# ── Analysis: prompt + parsing ───────────────────────────────────────────────

def test_build_prompt_carries_direction_and_deals():
    p = crm_intel.build_prompt(direction="inbound", subject="More pallets",
                               body="can you also do Fridays?", contact_name="Rob",
                               company_name="Acme", open_deals=OPEN_DEALS)
    assert "FROM the customer" in p and "deal_id=d1" in p and "Weekly pallet run" in p
    p2 = crm_intel.build_prompt(direction="outbound", subject="Re", body="We'll send "
                                "the quote Friday", contact_name="Rob",
                                company_name=None, open_deals=[])
    assert "WE sent" in p2 and "(none)" in p2


def test_parse_analysis_tolerant():
    good = ('{"deal": {"action": "new", "deal_id": "", "title": "Pallet contract", '
            '"value_estimate": 500, "confidence": 0.8}, '
            '"promises": [{"who": "us", "description": "Send quote", "due_hint": "Friday"}]}')
    out = crm_intel.parse_analysis(good)
    assert out["deal"]["action"] == "new" and out["promises"][0]["who"] == "us"
    # Events OS (docs/os/events.md §6.1): parse_analysis also carries an `event` branch
    # (empty when the model/tenant isn't event-shaped) — additive, always present.
    # Same for the CRM `sentiment` branch (mood read from the customer's words).
    assert crm_intel.parse_analysis("garbage") == {
        "deal": {}, "promises": [], "event": {}, "sentiment": {}}


# ── apply_deal: the trust boundary ───────────────────────────────────────────

def test_apply_deal_existing_only_trusts_own_open_deals(monkeypatch):
    stamped = []
    monkeypatch.setattr(crm_intel.db, "crm_set_activity_deal",
                        lambda tid, src, did: stamped.append((src, did)))
    assert crm_intel.apply_deal("t1", {"action": "existing", "deal_id": "d1"},
                                contact_id="ct1", company_id=None,
                                open_deals=OPEN_DEALS, email_log_id="log1") == "d1"
    assert stamped == [("log1", "d1")]
    # A deal id the model invented (or another customer's) is refused.
    assert crm_intel.apply_deal("t1", {"action": "existing", "deal_id": "other"},
                                contact_id="ct1", company_id=None,
                                open_deals=OPEN_DEALS, email_log_id="log1") is None


def test_apply_deal_new_respects_confidence_floor_and_creates(monkeypatch):
    created, stamped = [], []
    monkeypatch.setattr(crm_intel.crm, "ensure_default_stages",
                        lambda tid, client=None: [
                            {"id": "s1", "name": "Prospect", "stage_type": "active"}])
    monkeypatch.setattr(crm_intel.db, "crm_create_deal",
                        lambda tid, fields: created.append(fields) or {"id": "new1", **fields})
    monkeypatch.setattr(crm_intel.db, "crm_set_activity_deal",
                        lambda tid, src, did: stamped.append(did))
    low = {"action": "new", "title": "Maybe", "confidence": 0.4}
    assert crm_intel.apply_deal("t1", low, contact_id="ct1", company_id="co1",
                                open_deals=[], email_log_id="log1") is None
    good = {"action": "new", "title": "Pallet contract", "value_estimate": 500,
            "confidence": 0.8}
    assert crm_intel.apply_deal("t1", good, contact_id="ct1", company_id="co1",
                                open_deals=[], email_log_id="log1") == "new1"
    assert created[0]["stage_id"] == "s1" and created[0]["primary_contact_id"] == "ct1"
    assert created[0]["value_estimate"] == 500 and stamped == ["new1"]
    assert crm_intel.apply_deal("t1", {"action": "none"}, contact_id="ct1",
                                company_id=None, open_deals=[], email_log_id=None) is None


def test_apply_promises_validates_and_counts(monkeypatch):
    rows = []
    monkeypatch.setattr(crm_intel.db, "crm_insert_promise",
                        lambda tid, **kw: rows.append(kw) or True)
    tally = crm_intel.apply_promises("t1", [
        {"who": "us", "description": "Send the quote", "due_hint": "Friday"},
        {"who": "them", "description": "Confirm pallet count", "due_hint": ""},
        {"who": "aliens", "description": "nope", "due_hint": ""},
        {"who": "us", "description": "", "due_hint": ""},
    ], contact_id="ct1", company_id="co1", deal_id="d1", email_log_id="log1")
    assert tally["created"] == 2
    assert rows[0]["direction"] == "we_owe" and rows[0]["due_hint"] == "Friday"
    assert rows[1]["direction"] == "they_owe" and rows[1]["due_hint"] is None


def test_apply_promises_resolve_stamps_the_email_that_cleared_it(monkeypatch):
    """The email pass names its own email as what cleared the promise —
    'their email' inbound, 'your reply' outbound — so the tick opens it."""
    resolved, suggested, noted = [], [], []
    monkeypatch.setattr(crm_intel.db, "crm_resolve_promise_auto",
                        lambda pid, tid, evidence=None, **k: resolved.append((pid, k)))
    monkeypatch.setattr(crm_intel.db, "crm_suggest_promise_resolved",
                        lambda pid, tid, evidence=None, **k: suggested.append((pid, k)))
    monkeypatch.setattr(events_os, "on_promise_resolved", lambda tid, pid: None)
    monkeypatch.setattr(crm_intel.crm, "note_promise_cleared",
                        lambda tid, row, **k: noted.append((row["id"], row["resolved_by_id"])))
    open_p = [{"id": "p1", "direction": "they_owe", "description": "Send the PO"},
              {"id": "p2", "direction": "we_owe", "description": "Send the quote"}]
    tally = crm_intel.apply_promises("t1", [
        {"who": "them", "action": "resolve", "existing_promise_id": "p1",
         "description": "PO attached", "due_hint": "", "confidence": 0.9,
         "resolution_evidence": "PO 4471 attached"},
        {"who": "us", "action": "resolve", "existing_promise_id": "p2",
         "description": "quote", "due_hint": "", "confidence": 0.6,
         "resolution_evidence": "mentions a quote"},
    ], contact_id="ct1", company_id=None, deal_id=None, email_log_id="log9",
       open_promises=open_p, direction="inbound")
    assert tally["resolved"] == 1
    assert resolved == [("p1", {"by_kind": "email", "by_id": "log9", "by_label": "their email"})]
    assert suggested == [("p2", {"by_kind": "email", "by_id": "log9", "by_label": "their email"})]
    assert noted == [("p1", "log9")]
    # our own reply reads 'your reply'
    resolved.clear()
    crm_intel.apply_promises("t1", [
        {"who": "us", "action": "resolve", "existing_promise_id": "p2",
         "description": "quote", "due_hint": "", "confidence": 0.95,
         "resolution_evidence": "quote sent"}],
        contact_id="ct1", company_id=None, deal_id=None, email_log_id="log10",
        open_promises=open_p, direction="outbound")
    assert resolved[0][1]["by_label"] == "your reply"


def test_cleared_by_names_and_links_what_cleared_it():
    """The pure label/href the card renders (Kris 2026-09-20)."""
    email = "rob@acme-haulage.co.uk"
    assert crm.cleared_by({"status": "resolved", "resolved_by_kind": "booking",
                           "resolved_by_id": "bk1", "resolved_by_label": "BK-1017"}, email) == \
        {"label": "cleared by BK-1017", "href": "/app/diary/bk1"}
    assert crm.cleared_by({"status": "resolved", "resolved_by_kind": "invoice",
                           "resolved_by_id": "in1", "resolved_by_label": "INV-1003"}) == \
        {"label": "cleared by INV-1003", "href": "/app/invoices/in1"}
    assert crm.cleared_by({"status": "resolved", "resolved_by_kind": "quote",
                           "resolved_by_id": "q1", "resolved_by_label": "QUO-1012"}) == \
        {"label": "cleared by QUO-1012", "href": "/app/quotes/q1"}
    # an email opens the exact message in the thread
    assert crm.cleared_by({"status": "resolved", "resolved_by_kind": "email",
                           "resolved_by_id": "log9", "resolved_by_label": "their email"}, email) == \
        {"label": "cleared by their email",
         "href": "/app/conversations/view?contact=rob%40acme-haulage.co.uk#log-log9"}
    # no contact email → the label without a dead link
    assert crm.cleared_by({"status": "resolved", "resolved_by_kind": "email",
                           "resolved_by_id": "log9", "resolved_by_label": "your reply"})["href"] is None
    # by hand, dropped, and rows cleared before the columns existed
    assert crm.cleared_by({"status": "done", "resolved_by_kind": "manual"}) == \
        {"label": "you marked it done", "href": None}
    assert crm.cleared_by({"status": "dropped", "resolved_by_kind": "manual"}) == \
        {"label": "dropped", "href": None}
    assert crm.cleared_by({"status": "resolved", "resolution_type": "auto"}) == \
        {"label": "cleared", "href": None}


def test_note_promise_cleared_writes_one_timeline_line(monkeypatch):
    rows = []
    monkeypatch.setattr(crm.db, "crm_get_contact",
                        lambda cid, tid: {"id": cid, "email": "rob@acme-haulage.co.uk",
                                          "company_id": "co1"})
    monkeypatch.setattr(crm.db, "crm_insert_activity",
                        lambda tid, **kw: rows.append(kw) or True)
    assert crm.note_promise_cleared("t1", {
        "id": "p1", "contact_id": "ct1", "direction": "we_owe", "status": "resolved",
        "description": "Hold the October weekend", "resolution_evidence": "BK-1017 confirmed",
        "resolved_by_kind": "booking", "resolved_by_id": "bk1", "resolved_by_label": "BK-1017",
        "resolved_at": "2026-09-20T10:00:00+00:00"})
    (row,) = rows
    assert row["activity_type"] == "promise_cleared" and row["source_id"] == "p1"
    assert row["subject"] == "You owed: Hold the October weekend"
    assert row["summary"] == "cleared by BK-1017 — BK-1017 confirmed"
    assert row["occurred_at"] == "2026-09-20T10:00:00+00:00"
    # dropped reads as dropped, evidence not repeated
    crm.note_promise_cleared("t1", {"id": "p2", "contact_id": "ct1", "direction": "they_owe",
                                    "status": "dropped", "description": "Send the PO",
                                    "resolved_by_kind": "manual"})
    assert rows[1]["activity_type"] == "promise_dropped" and rows[1]["summary"] == "dropped"
    # no contact → nothing written, nothing raised
    assert crm.note_promise_cleared("t1", {"id": "p3"}) is False


@pytest.mark.asyncio
async def test_process_email_inbound_clusters_and_promises(monkeypatch):
    monkeypatch.setattr(crm_intel.db, "crm_list_deals",
                        lambda tid, contact_id=None, client=None: list(OPEN_DEALS))
    monkeypatch.setattr(crm_intel.db, "crm_open_promises",
                        lambda tid, contact_id=None, client=None: [])
    async def fake_analyze(tid, **kw):
        assert kw["direction"] == "inbound" and kw["open_deals"] == OPEN_DEALS
        return {"deal": {"action": "existing", "deal_id": "d1"},
                "promises": [{"who": "them", "description": "Send PO", "due_hint": ""}]}
    monkeypatch.setattr(crm_intel, "analyze", fake_analyze)
    monkeypatch.setattr(crm_intel.db, "crm_set_activity_deal", lambda *a: None)
    inserted = []
    monkeypatch.setattr(crm_intel.db, "crm_insert_promise",
                        lambda tid, **kw: inserted.append(kw) or True)
    out = await crm_intel.process_email("t1", direction="inbound", subject="s",
                                        body="b", contact_id="ct1", company_id="co1",
                                        contact_name="Rob", company_name="Acme",
                                        email_log_id="log1")
    assert out["deal_id"] == "d1"
    assert out["promises"]["created"] == 1
    assert inserted[0]["deal_id"] == "d1"


# ── action-side promise resolution (Julius 2026-09-03) ───────────────────────
# The email pass resolves promises the customer's email fulfils; these pin the
# ACTION pass: a booking confirmed / invoice sent by the business's own system
# ticks the ledger too ("We owe: hold the October weekend" sat open while
# BK-1017 was confirmed in the diary).
@pytest.mark.asyncio
async def test_resolve_from_action_gates_hard_before_any_llm(monkeypatch):
    async def never(*a, **k):
        pytest.fail("LLM must not run")
    monkeypatch.setattr(crm_intel, "_action_resolve_llm", never)
    # no email contact → zero spend
    assert await crm_intel.resolve_from_action("t1", None, "x") == \
        {"resolved": 0, "suggested": 0}
    assert await crm_intel.resolve_from_action("t1", "front desk", "x") == \
        {"resolved": 0, "suggested": 0}
    # contact with NO open promises → zero spend
    monkeypatch.setattr(crm_intel.db, "crm_contact_by_email",
                        lambda tid, email, client=None: {"id": "ct1"})
    monkeypatch.setattr(crm_intel.db, "crm_open_promises",
                        lambda tid, contact_id=None, client=None: [])
    assert await crm_intel.resolve_from_action("t1", "k@x.com", "x") == \
        {"resolved": 0, "suggested": 0}


@pytest.mark.asyncio
async def test_resolve_from_action_applies_the_confidence_gates(monkeypatch):
    monkeypatch.setattr(crm_intel.db, "crm_contact_by_email",
                        lambda tid, email, client=None: {"id": "ct1"})
    monkeypatch.setattr(crm_intel.db, "crm_open_promises",
                        lambda tid, contact_id=None, client=None: [
                            {"id": "p1", "direction": "we_owe",
                             "description": "Hold the October weekend"},
                            {"id": "p2", "direction": "they_owe",
                             "description": "Kris requests a meet and greet"},
                            {"id": "p3", "direction": "we_owe",
                             "description": "Send the invoice"}])
    prompts = []

    async def fake_llm(tid, prompt):
        prompts.append(prompt)
        return [{"promise_id": "p1", "confidence": 0.95,
                 "evidence": "BK-1017 confirmed for 10-11 Oct"},
                {"promise_id": "p2", "confidence": 0.9, "evidence": "m&g booked"},
                {"promise_id": "p3", "confidence": 0.6, "evidence": "maybe"},
                {"promise_id": "invented", "confidence": 1.0, "evidence": "no"}]
    monkeypatch.setattr(crm_intel, "_action_resolve_llm", fake_llm)
    resolved, suggested, flipped, noted = [], [], [], []
    monkeypatch.setattr(crm_intel.db, "crm_resolve_promise_auto",
                        lambda pid, tid, evidence=None, **k: resolved.append(
                            (pid, evidence, k.get("by_kind"), k.get("by_id"), k.get("by_label"))))
    monkeypatch.setattr(crm_intel.db, "crm_suggest_promise_resolved",
                        lambda pid, tid, evidence=None, **k: suggested.append((pid, k.get("by_label"))))
    monkeypatch.setattr(events_os, "on_promise_resolved",
                        lambda tid, pid: flipped.append(pid))
    monkeypatch.setattr(crm_intel.crm, "note_promise_cleared",
                        lambda tid, row, **k: noted.append(
                            (row["id"], row["status"], row["resolved_by_kind"], row["resolved_by_label"])))
    out = await crm_intel.resolve_from_action(
        "t1", "K@X.com", "Confirmed booking BK-1017 for 2026-10-10",
        ref=("booking", "bk1", "BK-1017"))
    assert out == {"resolved": 2, "suggested": 1}
    # the thing that cleared it rides every write (Kris 2026-09-20)...
    assert resolved == [("p1", "BK-1017 confirmed for 10-11 Oct", "booking", "bk1", "BK-1017"),
                        ("p2", "m&g booked", "booking", "bk1", "BK-1017")]
    assert suggested == [("p3", "BK-1017")]      # a suggestion keeps the link for Confirm
    assert flipped == ["p2"]                      # they_owe → Events OS hook only
    # ...and each clearing writes its timeline line, with the row as stamped
    assert noted == [("p1", "resolved", "booking", "BK-1017"),
                     ("p2", "resolved", "booking", "BK-1017")]
    # the prompt carried the action and every open promise, sides labelled
    assert "Confirmed booking BK-1017" in prompts[0]
    assert "promise_id=p1 (us)" in prompts[0]
    assert "promise_id=p2 (them)" in prompts[0]


# ── Drafter awareness ────────────────────────────────────────────────────────

CONTACT = {"id": "ct1", "email": "rob@acme.co.uk", "name": "Rob Smith",
           "custom_data": {"role": "Owner"},
           "crm_companies": {"id": "co1", "name": "Acme Haulage",
                             "custom_data": {"fleet_size": 12}}}
DEFS = [{"entity": "contact", "key": "role", "label": "Role"},
        {"entity": "company", "key": "fleet_size", "label": "Fleet size"}]


def test_build_crm_context_renders_the_relationship():
    ctx = crm_intel.build_crm_context(
        CONTACT, DEFS, OPEN_DEALS,
        [{"direction": "we_owe", "description": "Send the quote", "due_hint": "Friday"}])
    assert "Rob Smith at Acme Haulage" in ctx
    assert "Role: Owner" in ctx and "Fleet size: 12" in ctx
    assert "Open deal: Weekly pallet run — stage: Prospect" in ctx
    assert "We owe them: Send the quote (Friday)" in ctx
    assert crm_intel.build_crm_context(None, [], [], []) == ""
    bare = {"id": "x", "email": "a@b.com", "custom_data": {}, "crm_companies": None}
    assert crm_intel.build_crm_context(bare, [], [], []) == ""


def test_system_prompt_carries_customer_context():
    system = agent.build_system_prompt({"business_name": "Koth"},
                                       crm_context="Customer: Rob at Acme\nRole: Owner")
    assert "<customer_context>" in system and "Role: Owner" in system
    assert "Never recite this back" in system
    assert "<customer_context>" not in agent.build_system_prompt({"business_name": "Koth"})


def test_with_recency_honours_tenant_threshold():
    rows = [{"id": "a", "last_activity_at": "2026-07-01T09:00:00+00:00"}]
    from datetime import datetime, timezone
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)      # 4 days quiet
    assert crm.with_recency(rows, now)[0]["stale"] is False      # default 14
    assert crm.with_recency(rows, now, stale_after=3)[0]["stale"] is True


# ── Routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class Env:
    def __init__(self, monkeypatch, *, promises=(), quiet_contacts=()):
        self.deal_updates, self.promise_updates = [], []
        self.stage_writes, self.cfg_writes = [], []
        def snapshot(ctx, page):
            return {'page': page, 'stages': dashboard.db.crm_list_stages('t1'), 'quiet_days': 7}
        monkeypatch.setattr(dashboard.setup_forms, 'load', snapshot)
        def save(ctx, page, expected, operations):
            assert expected == snapshot(ctx, page)
            for op in operations:
                if op['table'] == 'tenant_config':
                    self.cfg_writes.append(op['fields'])
                elif op['op'] == 'insert':
                    f = op['fields']
                    self.stage_writes.append(('add',f['name'],f['display_order'],f['stage_type']))
                elif op['op'] == 'update':
                    self.stage_writes.append(('update',op['id'],op['fields']))
                else:
                    self.stage_writes.append(('delete',op['id']))
        monkeypatch.setattr(dashboard.setup_forms, 'save', save)
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})   # CRM unlocks at Core (2026-07-24)
        monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "crm_list_stages", lambda tid, client=None: [
            {"id": "s1", "name": "Prospect", "stage_type": "active"},
            {"id": "s2", "name": "Won", "stage_type": "won"}])
        monkeypatch.setattr(dashboard.db, "crm_update_deal",
                            lambda did, tid, fields, client=None:
                            self.deal_updates.append((did, fields)))
        monkeypatch.setattr(dashboard.db, "crm_resolve_promise",
                            lambda pid, tid, status, client=None:
                            self.promise_updates.append((pid, status)))
        monkeypatch.setattr(dashboard.db, "crm_open_promises",
                            lambda tid, contact_id=None, client=None: list(promises))
        # the cleared state (2026-09-20): confirm / keep + the timeline line
        self.confirmed, self.kept, self.noted = [], [], []
        monkeypatch.setattr(dashboard.db, "crm_get_promise",
                            lambda pid, tid, client=None: {"id": pid, "contact_id": "ct1"})
        monkeypatch.setattr(dashboard.db, "crm_confirm_promise_resolved",
                            lambda pid, tid, client=None: self.confirmed.append(pid))
        monkeypatch.setattr(dashboard.db, "crm_keep_promise_open",
                            lambda pid, tid, client=None: self.kept.append(pid))
        monkeypatch.setattr(dashboard.crm, "note_promise_cleared",
                            lambda tid, row, **k: self.noted.append(row["id"]))
        monkeypatch.setattr(dashboard.db, "crm_recent_cleared_promises",
                            lambda tid, contact_id=None, days=30, limit=8, client=None: [])
        monkeypatch.setattr(dashboard.db, "crm_export",
                            lambda tid, client=None: {"contacts": [{"email": "a@b.co"}]})
        monkeypatch.setattr(dashboard.db, "crm_list_contacts",
                            lambda tid, q=None, limit=100, offset=0, client=None:
                            list(quiet_contacts))
        monkeypatch.setattr(dashboard.db, "get_tenant_config",
                            lambda tid, client=None: {"crm_quiet_days": 7})
        monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                            lambda tid, cfg, client=None: self.cfg_writes.append(cfg))
        monkeypatch.setattr(dashboard.db, "crm_insert_stage",
                            lambda tid, name, display_order=0, stage_type="active", client=None:
                            self.stage_writes.append(("add", name, display_order, stage_type)))
        monkeypatch.setattr(dashboard.db, "crm_update_stage",
                            lambda sid, tid, fields, client=None:
                            self.stage_writes.append(("update", sid, fields)))
        monkeypatch.setattr(dashboard.db, "crm_delete_stage",
                            lambda sid, tid, client=None:
                            self.stage_writes.append(("delete", sid)))
        monkeypatch.setattr(dashboard.db, "crm_list_deals",
                            lambda tid, contact_id=None, company_id=None, client=None: [
                                {"id": "d1", "status": "open", "value_estimate": 99}])
        # home's other reads
        monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.usage, "summarize", lambda *a, **k: {
            "plan": {"label": "Pilot"}, "emails": 1, "allowance": 200, "pct": 0.5,
            "over": False, "warn": False})
        monkeypatch.setattr(dashboard.trial, "evaluate", lambda *a, **k: {
            "status": "converted", "active": False})


def test_stage_move_sets_status_from_stage_type(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/deals/d1/stage", data={"stage_id": "s2"},
                    follow_redirects=False)
    assert r.status_code == 303
    did, fields = env.deal_updates[0]
    assert did == "d1" and fields["status"] == "won" and fields["closed_at"]
    client.post("/app/crm/deals/d1/stage", data={"stage_id": "s1"},
                follow_redirects=False)
    _, reopened = env.deal_updates[1]
    assert reopened["status"] == "open" and reopened["closed_at"] is None
    assert client.post("/app/crm/deals/d1/stage", data={"stage_id": "nope"},
                       follow_redirects=False).status_code == 404


def test_promise_resolve_route(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/promises/p1/resolve",
                    data={"status": "done", "return_to": "/app/crm/contacts/ct1"},
                    follow_redirects=False)
    assert r.status_code == 303 and env.promise_updates == [("p1", "done")]
    assert env.noted == ["p1"]                    # the timeline line follows the click
    assert client.post("/app/crm/promises/p1/resolve", data={"status": "sideways"},
                       follow_redirects=False).status_code == 422
    # a suggested resolution: Confirm keeps the link, Still open clears it
    r = client.post("/app/crm/promises/p2/resolve", data={"status": "confirm"},
                    follow_redirects=False)
    assert r.status_code == 303 and env.confirmed == ["p2"] and env.noted == ["p1", "p2"]
    r = client.post("/app/crm/promises/p3/resolve", data={"status": "keep"},
                    follow_redirects=False)
    assert r.status_code == 303 and env.kept == ["p3"] and env.noted == ["p1", "p2"]


def test_exports(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/export.json")
    assert r.status_code == 200 and "a@b.co" in r.text
    assert "attachment" in r.headers["content-disposition"]
    monkeypatch.setattr(dashboard.db, "crm_list_contacts",
                        lambda tid, limit=200, offset=0, client=None: [
                            {"email": "rob@acme.co.uk", "name": "Rob",
                             "crm_companies": {"name": "Acme"},
                             "custom_data": {"role": "Owner"}}])
    r = client.get("/app/crm/export.csv")
    assert r.status_code == 200 and "rob@acme.co.uk" in r.text and "Acme" in r.text
    body = client.get("/api/crm/export").json()
    assert body["contacts"][0]["email"] == "a@b.co"
    assert client.get("/api/crm/promises").json() == {"promises": []}


def test_home_gone_quiet_card_uses_tenant_threshold(client, monkeypatch):
    Env(monkeypatch, quiet_contacts=[
        {"id": "ct1", "email": "rob@acme.co.uk", "name": "Rob Smith",
         "last_activity_at": "2000-01-01T00:00:00+00:00"}])   # ancient — quiet
    monkeypatch.setattr(dashboard, "_intake_configured", lambda ctx: False)
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: {"ok": True})
    r = client.get("/app/")
    assert r.status_code == 200
    # Flight Deck (2026-08-31): quiet contacts surface as person cards in the
    # deck rather than a "Gone quiet" panel — the threshold still decides WHO.
    assert "Rob Smith" in r.text
    assert "quiet" in r.text and "check-in keeps it warm" in r.text
    # The CRM routing card rides alongside: count + open pipeline, into /app/crm.
    assert '<a href="/app/crm"' in r.text
    assert "1 open deal" in r.text and "£99 in play" in r.text


# ── CRM settings page (stages editor + quiet threshold + exports) ────────────

def test_settings_page_renders_stages_and_threshold(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/settings")
    assert r.status_code == 200
    assert "Pipeline stages" in r.text and "Prospect" in r.text and "Won" in r.text
    assert 'value="7"' in r.text                      # tenant threshold, not the default
    assert "Export everything JSON" in r.text
    assert "/app/crm/settings" in r.text              # nav tab present


def test_settings_saves_quiet_threshold_with_bounds(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/crm/settings", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "quiet_days": "21"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    assert env.cfg_writes == [{"crm_quiet_days": 21}]
    r = client.post("/app/crm/settings", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "quiet_days": "9000"}, follow_redirects=False)
    assert env.cfg_writes[-1] == {"crm_quiet_days": 365}   # clamped
    r = client.post("/app/crm/settings", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "quiet_days": "soon"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_settings_stage_crud(client, monkeypatch):
    env = Env(monkeypatch)
    client.post("/app/crm/settings/stages",
                data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "name": "On hold", "stage_type": "active", "display_order": "45"},
                follow_redirects=False)
    assert env.stage_writes[-1] == ("add", "On hold", 45, "active")
    client.post("/app/crm/settings/stages/s1",
                data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "name": "Lead", "stage_type": "active", "display_order": "5"},
                follow_redirects=False)
    assert env.stage_writes[-1] == ("update", "s1",
                                    {"name": "Lead", "stage_type": "active", "display_order": 5})
    client.post("/app/crm/settings/stages/s1/delete", data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings"))}, follow_redirects=False)
    assert env.stage_writes[-1] == ("delete", "s1")
    r = client.post("/app/crm/settings/stages",
                    data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "name": "  ", "stage_type": "active"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post("/app/crm/settings/stages",
                    data={"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"crm_settings")), "name": "X", "stage_type": "sideways"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_build_crm_context_renders_recent_activity_without_mail_noise():
    acts = [
        {"activity_type": "portal_chat", "occurred_at": "2026-07-30T10:00:00Z",
         "summary": "Asked on the portal: do you fit heat pumps?"},
        {"activity_type": "email", "occurred_at": "2026-07-29T10:00:00Z",
         "summary": "the thread history already carries this"},
        {"activity_type": "calendar_event", "occurred_at": "2026-07-28T10:00:00Z",
         "summary": "Rescheduled via the portal: 2026-08-05 (am) → 2026-08-07 (pm)"},
        {"activity_type": "reschedule_request", "occurred_at": "2026-07-27T09:00:00Z",
         "summary": "Asked on the portal to move BK-1001 from A to B"},
    ]
    ctx = crm_intel.build_crm_context(CONTACT, [], [], [], activities=acts)
    assert "Recent activity (newest first):" in ctx
    assert "- 2026-07-30: Asked on the portal: do you fit heat pumps?" in ctx
    assert "Rescheduled via the portal" in ctx
    # email/SMS rows are the thread's job, not this block's
    assert "thread history already carries this" not in ctx
    # a contact with ONLY activity context still renders (portal-only customer)
    bare = {"id": "x", "email": "a@b.com", "name": "Ana",
            "custom_data": {}, "crm_companies": None}
    assert "Recent activity" in crm_intel.build_crm_context(
        bare, [], [], [], activities=acts)
    # and no activities keeps the old behaviour byte-identical
    assert crm_intel.build_crm_context(bare, [], [], [], activities=[]) == ""


def test_offered_slots_the_diary_holds_are_not_a_promise_we_owe(monkeypatch):
    """Run log F20 (plumber take 2026-09-25): Today said "You owe: hold the
    Saturday morning, Saturday afternoon and Monday morning slots open for
    Sarah". The diary holds offered slots; the owner owes nothing. Her
    reply-by is hers, and still kept."""
    inserted = []
    monkeypatch.setattr(crm_intel.db, "crm_insert_promise",
                        lambda tid, **k: inserted.append(k) or {"id": "p1"})
    monkeypatch.setattr(crm_intel, "_auto_task_for_promise", lambda *a, **k: None)
    tally = crm_intel.apply_promises("t1", [
        {"action": "create", "who": "us", "confidence": 0.9, "due_hint": "Friday 20:00",
         "description": "Hold the Saturday morning, Saturday afternoon and Monday morning slots open for Sarah",
         "existing_promise_id": "", "resolution_evidence": ""},
        {"action": "create", "who": "them", "confidence": 0.9, "due_hint": "8pm tonight",
         "description": "Reply to pick one of the slots", "existing_promise_id": "",
         "resolution_evidence": ""},
        {"action": "create", "who": "us", "confidence": 0.9, "due_hint": "",
         "description": "Send the gas safety certificate within 24 hours of the visit",
         "existing_promise_id": "", "resolution_evidence": ""},
    ], contact_id="c1", company_id=None, deal_id=None, email_log_id="e1")
    assert [k["direction"] for k in inserted] == ["they_owe", "we_owe"]
    assert tally["created"] == 2


def test_build_crm_context_probes_and_reports_the_asked_field(monkeypatch):
    """Phase 3.5 §18.2: a blank probeable field puts a PROBE block in the
    context AND names itself in probe_out — that's the field whose attempt
    the pipeline logs so the MAX_PROBE_ATTEMPTS cap can ever engage."""
    probe_def = {"id": "fd-budget", "entity": "contact", "key": "budget",
                 "label": "Budget", "field_type": "number", "options": [],
                 "extraction_hint": "rough spend",
                 "probe_behaviour": "probe_once", "display_order": 0}
    monkeypatch.setattr(crm_fields.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: [probe_def])
    monkeypatch.setattr(crm_fields.db, "crm_get_contact",
                        lambda cid, tid, client=None: {"id": cid, "custom_data": {}})
    monkeypatch.setattr(crm_fields.db, "crm_get_company",
                        lambda cid, tid, client=None: {"id": cid, "custom_data": {}})
    monkeypatch.setattr(crm_fields.db, "crm_probe_log_for_contact",
                        lambda tid, cid: [])
    asked = []
    ctx = crm_intel.build_crm_context(CONTACT, DEFS, [], [], tenant_id="t1",
                                      probe_out=asked)
    assert "PROBE:" in ctx and "Budget" in ctx
    assert [f["id"] for f in asked] == ["fd-budget"]
    # No tenant_id → the probe machinery never runs, probe_out stays empty.
    asked = []
    ctx = crm_intel.build_crm_context(CONTACT, DEFS, [], [], probe_out=asked)
    assert "PROBE:" not in ctx and asked == []
