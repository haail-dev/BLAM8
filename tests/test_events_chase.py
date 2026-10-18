"""Events OS — Phase B (docs/os/events.md §6.2, §12, §14): the multi-stakeholder chase
engine — the killer feature. Every supplier + staff member on every event chased to the
run sheet, so the venue manager stops being the human message-bus.

Pins:
- linking a stakeholder's outstanding item to a they_owe promise (deduped, ONE promise
  across restatements — never stacked) + the stakeholder gets outstanding_promise_id;
- crm_stakeholders_to_chase selects pending + open-promise + due + under-max + not-muted,
  and EXCLUDES confirmed / muted / declined / exhausted / resolved-promise / past-event /
  not-yet-due ones;
- chase_stakeholders.run() drafts an ESCALATING chase (level by chase_count), stamps
  last_chased_at + chase_count, is DRAFT-FIRST (pending even when cfg.auto_send unless the
  cfg.auto_chase_events opt-in), and backs off at MAX_CHASES → needs-a-human on the board;
- the auto-resolve hook flips a stakeholder to confirmed when its promise resolves, and
  does NOTHING for a non-events tenant;
- the chase-now + mute/unmute routes (CSRF, IDOR-safe).

No network: db.* + agent.* are monkeypatched; the autouse conftest guard fails loudly on
any un-stubbed live Supabase call.

Run: python -m pytest tests/test_events_chase.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, events_os
from app.scheduling import DIARY_TZ


# ── a tiny multi-table fake Supabase (per-table row stores) — mirrors test_events_os ──
class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._rows = list(store.setdefault(name, []))
        self._pending = None

    def select(self, *a, **k): return self
    def insert(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        made = []
        for p in rows:
            row = dict(p)
            row.setdefault("id", f"{self._name}-{len(self._store[self._name]) + 1}")
            self._store[self._name].append(row)
            made.append(dict(row))
        self._pending = ("insert", made)
        return self
    def update(self, payload):
        for r in self._rows:
            r.update(payload)
        self._pending = ("update", [dict(r) for r in self._rows])
        return self
    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        if self._pending:
            _, data = self._pending
            self._pending = None
            return _FakeResult(data)
        return _FakeResult(self._rows)


class _FakeClient:
    def __init__(self, store=None):
        self._store = store if store is not None else {}

    def table(self, name):
        return _FakeQuery(self._store, name)


# ══ 1. linking a stakeholder's outstanding item to a they_owe promise ══════════
def test_link_stakeholder_promise_creates_and_links():
    store = {"crm_event_stakeholders": [{"id": "s1", "tenant_id": "t1", "event_id": "e1",
                                         "outstanding_promise_id": None}],
             "crm_promises": []}
    cl = _FakeClient(store)
    pid = db.crm_link_stakeholder_promise(
        "t1", stakeholder_id="s1", deal_id="d1",
        description="the bar owes us the drink-licence confirmation", client=cl)
    assert pid is not None
    assert len(store["crm_promises"]) == 1
    p = store["crm_promises"][0]
    assert p["direction"] == "they_owe" and p["deal_id"] == "d1"
    assert p["email_log_id"] is None                          # stakeholder-sourced
    assert store["crm_event_stakeholders"][0]["outstanding_promise_id"] == pid


def test_link_stakeholder_promise_dedups_restated_item_one_promise():
    """A restated item stays ONE promise (never stacked) — the promise engine's dedup
    discipline (§18.3): the second link finds the deal's open they_owe promise with the
    EXACT (case-insensitive) description and reuses it."""
    store = {"crm_event_stakeholders": [
        {"id": "s1", "tenant_id": "t1", "event_id": "e1", "outstanding_promise_id": None},
        {"id": "s2", "tenant_id": "t1", "event_id": "e1", "outstanding_promise_id": None}],
        "crm_promises": []}
    desc = "the bar owes us the drink-licence confirmation"
    pid1 = db.crm_link_stakeholder_promise("t1", stakeholder_id="s1", deal_id="d1",
                                           description=desc, client=_FakeClient(store))
    # a second stakeholder (or a reprocess) linking the SAME item, different case
    pid2 = db.crm_link_stakeholder_promise("t1", stakeholder_id="s2", deal_id="d1",
                                           description=desc.upper(), client=_FakeClient(store))
    assert pid1 == pid2                                        # ONE promise, reused
    assert len(store["crm_promises"]) == 1                     # not stacked


def test_link_stakeholder_promise_reuses_own_open_promise_when_already_linked():
    """A stakeholder already linked to an OPEN promise keeps that one — re-linking must
    not mint a second promise for the same chase."""
    store = {"crm_event_stakeholders": [
        {"id": "s1", "tenant_id": "t1", "event_id": "e1", "outstanding_promise_id": "p9"}],
        "crm_promises": [{"id": "p9", "tenant_id": "t1", "deal_id": "d1",
                          "direction": "they_owe", "status": "open",
                          "description": "old wording"}]}
    pid = db.crm_link_stakeholder_promise("t1", stakeholder_id="s1", deal_id="d1",
                                          description="new wording", client=_FakeClient(store))
    assert pid == "p9"
    assert len(store["crm_promises"]) == 1                     # not a second promise


def test_link_stakeholder_promise_exact_match_not_ilike_wildcard():
    """Dedup is EXACT (case-insensitive), never .ilike: an item carrying %/_/( must not
    collide with a different open promise."""
    store = {"crm_event_stakeholders": [
        {"id": "s1", "tenant_id": "t1", "event_id": "e1", "outstanding_promise_id": None}],
        "crm_promises": [{"id": "p1", "tenant_id": "t1", "deal_id": "d1",
                          "direction": "they_owe", "status": "open",
                          "description": "the bar owes us the licence (100% cover)"}]}
    # a DIFFERENT item whose literal text the first would .ilike-match if % were a wildcard
    pid = db.crm_link_stakeholder_promise(
        "t1", stakeholder_id="s1", deal_id="d1",
        description="the bar owes us the licence copy", client=_FakeClient(store))
    assert pid == "crm_promises-2"                             # a NEW promise, not p1
    assert len(store["crm_promises"]) == 2


def test_events_os_link_helper_builds_house_voice_description(monkeypatch):
    captured = {}
    monkeypatch.setattr(events_os.db, "crm_link_stakeholder_promise",
                        lambda t, **k: captured.update(k) or "p1")
    pid = events_os.link_stakeholder_outstanding(
        "t1", stakeholder={"id": "s1", "role": "bar"}, deal_id="d1",
        outstanding="the drink-licence confirmation")
    assert pid == "p1"
    assert captured["description"] == "the bar owes us the drink-licence confirmation"


# ══ 2. the chase queue — crm_stakeholders_to_chase selection contract ══════════
def _now():
    return datetime(2026, 7, 8, 9, 0, tzinfo=DIARY_TZ)


class _ChaseClient(_FakeClient):
    """A fake tuned for crm_stakeholders_to_chase: the stakeholders join embeds
    crm_events, and the promise-status lookup reads crm_promises by id."""
    def __init__(self, stakeholders, promises):
        super().__init__({"crm_event_stakeholders": stakeholders,
                          "crm_promises": promises})


def _sh(**kw):
    base = {"id": "s1", "tenant_id": "t1", "event_id": "e1", "confirmation": "pending",
            "chase_count": 0, "last_chased_at": None, "outstanding_promise_id": "p1",
            "crm_events": {"id": "e1", "deal_id": "d1", "event_date": "2026-07-12",
                           "crm_deals": {"id": "d1", "title": "Harper Wedding"}}}
    base.update(kw)
    return base


def _chase(stakeholders, promises=None, now=None, **kw):
    promises = promises if promises is not None else [
        {"id": "p1", "tenant_id": "t1", "status": "open"}]
    cl = _ChaseClient(stakeholders, promises)
    return db.crm_stakeholders_to_chase("t1", now=now or _now(), client=cl, **kw)


def test_chase_queue_selects_pending_open_due_undermax_notmuted():
    got = _chase([_sh()])
    assert [s["id"] for s in got] == ["s1"]                    # a fresh pending item is due


def test_chase_queue_excludes_confirmed():
    assert _chase([_sh(confirmation="confirmed")]) == []


def test_chase_queue_excludes_muted():
    assert _chase([_sh(confirmation="muted")]) == []           # §14 don't-chase


def test_chase_queue_excludes_declined_and_needs_info():
    assert _chase([_sh(confirmation="declined")]) == []
    assert _chase([_sh(confirmation="needs_info")]) == []


def test_chase_queue_excludes_past_event():
    assert _chase([_sh(crm_events={"id": "e1", "deal_id": "d1",
                                   "event_date": "2026-07-01",  # before now (07-08)
                                   "crm_deals": {}})]) == []


def test_chase_queue_excludes_null_dated_event():
    assert _chase([_sh(crm_events={"id": "e1", "deal_id": "d1", "event_date": None,
                                   "crm_deals": {}})]) == []


def test_chase_queue_excludes_exhausted():
    assert _chase([_sh(chase_count=3)]) == []                  # >= MAX_CHASES → needs a human


def test_chase_queue_excludes_resolved_promise():
    got = _chase([_sh()], promises=[{"id": "p1", "tenant_id": "t1", "status": "resolved"}])
    assert got == []                                           # promise fulfilled already


def test_chase_queue_excludes_unlinked_stakeholder():
    assert _chase([_sh(outstanding_promise_id=None)]) == []    # nothing tracked to chase


def test_chase_queue_excludes_not_yet_due():
    """chase_count 0, first step is 4 days: chased 1 day ago → not yet due."""
    last = (_now() - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    assert _chase([_sh(last_chased_at=last)]) == []


def test_chase_queue_includes_due_after_step():
    """chase_count 1, next step is 8 days: chased 9 days ago → due."""
    last = (_now() - timedelta(days=9)).astimezone(timezone.utc).isoformat()
    got = _chase([_sh(chase_count=1, last_chased_at=last)])
    assert [s["id"] for s in got] == ["s1"]


def test_chase_queue_zero_chase_count_not_falsy_coerced():
    """chase_count 0 must be treated as 0 (under max, first chase due), never coerced to
    a truthy/absent state."""
    got = _chase([_sh(chase_count=0, last_chased_at=None)])
    assert len(got) == 1


# ══ crm_stamp_stakeholder_chased ═══════════════════════════════════════════════
def test_stamp_stakeholder_chased_increments_and_stamps():
    store = {"crm_event_stakeholders": [{"id": "s1", "tenant_id": "t1", "chase_count": 1}]}
    got = db.crm_stamp_stakeholder_chased("s1", "t1", now=_now(), client=_FakeClient(store))
    assert got["chase_count"] == 2 and got["last_chased_at"]


def test_stamp_stakeholder_chased_from_zero():
    store = {"crm_event_stakeholders": [{"id": "s1", "tenant_id": "t1", "chase_count": 0}]}
    got = db.crm_stamp_stakeholder_chased("s1", "t1", now=_now(), client=_FakeClient(store))
    assert got["chase_count"] == 1


# ══ 3. chase_stakeholders.run() — escalation, draft-first, backoff ═════════════
class _FakeDraft:
    def __init__(self, text="Hi, just checking in on the licence."):
        self.text = text
        self.usage = {"input_tokens": 10, "output_tokens": 5}
        self.model = "claude-haiku"


def _run_chase(monkeypatch, *, queue, cfg, sent_box=None, stamped=None, logged=None):
    import asyncio
    from app.jobs import chase_stakeholders

    monkeypatch.setattr(chase_stakeholders.db, "tenants_with_modules", lambda m: ["t1"])
    monkeypatch.setattr(chase_stakeholders.db, "crm_stakeholders_to_chase",
                        lambda t, **k: queue)
    monkeypatch.setattr(chase_stakeholders.db, "effective_config", lambda *a, **k: cfg)
    return asyncio.run(chase_stakeholders.run())


def _patch_events_os_io(monkeypatch, capture, *, sent_box=None, stamped=None):
    """Patch compose_chase's I/O: it does `from app import agent, mailbox, usage` +
    db.* — so patch those real functions (not sys.modules)."""
    from app import agent, mailbox, usage

    async def fake_compose(cfg, purpose, facts, channel="email", **kw):
        capture.setdefault("purposes", []).append(purpose)
        capture["facts"] = facts
        return _FakeDraft()

    async def fake_send(tenant_id, to, subject, text):
        if sent_box is not None:
            sent_box.append((to, subject))

    monkeypatch.setattr(agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(mailbox, "send_mail", fake_send)
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(events_os.db, "insert_email_log",
                        lambda *a, **k: (capture.setdefault("logs", []).append(k) or "log1", True))
    monkeypatch.setattr(events_os.db, "crm_stamp_stakeholder_chased",
                        lambda sid, t, **k: (stamped.append(sid) if stamped is not None else None) or {})
    monkeypatch.setattr(events_os.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(events_os.db, "mark_email_log_sent", lambda *a, **k: None)


def test_chase_run_drafts_first_by_default(monkeypatch):
    """DRAFT-FIRST (§14): even with cfg.auto_send True, a chase stays a pending draft
    unless cfg.auto_chase_events is set."""
    cap = {}
    sent = []
    stamped = []
    _patch_events_os_io(monkeypatch, cap, sent_box=sent, stamped=stamped)
    queue = [_sh(email="bar@x.co", outstanding="the drink licence")]
    out = _run_chase(monkeypatch, queue=queue,
                     cfg={"auto_send": True})            # auto_send True, NO auto_chase_events
    assert out == {"drafted": 1, "sent": 0}              # draft-first wins
    assert sent == []                                    # nothing auto-sent
    assert stamped == ["s1"]                             # still stamped (dedupe/backoff)


def test_chase_run_auto_sends_only_on_opt_in(monkeypatch):
    cap = {}
    sent = []
    _patch_events_os_io(monkeypatch, cap, sent_box=sent)
    queue = [_sh(email="bar@x.co", outstanding="the drink licence")]
    out = _run_chase(monkeypatch, queue=queue,
                     cfg={"auto_send": True, "auto_chase_events": True})
    assert out == {"drafted": 0, "sent": 1}
    assert len(sent) == 1 and sent[0][0] == "bar@x.co"       # auto-sent to the supplier


def test_chase_run_escalation_level_by_chase_count(monkeypatch):
    """The chase purpose escalates by chase_count: 0 → gentle, 1 → deadline push."""
    cap = {}
    _patch_events_os_io(monkeypatch, cap)
    # first: a never-chased item → gentle (level 0)
    _run_chase(monkeypatch, queue=[_sh(email="a@x.co", chase_count=0)],
               cfg={})
    gentle = cap["purposes"][-1]
    cap["purposes"] = []
    # then: a once-chased item → deadline push (level 1)
    _run_chase(monkeypatch, queue=[_sh(email="a@x.co", chase_count=1)], cfg={})
    push = cap["purposes"][-1]
    assert gentle != push
    assert gentle == events_os.chase_purpose(0)
    assert push == events_os.chase_purpose(1)


def test_chase_purpose_clamps_past_last_level():
    assert events_os.chase_purpose(9) == events_os.chase_purpose(len(events_os._CHASE_PURPOSES) - 1)
    assert events_os.chase_purpose(-1) == events_os._CHASE_PURPOSES[0]


def test_chase_facts_carries_reference_and_outstanding():
    facts = events_os.chase_facts(
        {"role": "bar", "name": "Vine", "outstanding": "the drink licence"},
        {"id": "abcdef12-0000", "event_date": "2026-07-12", "guest_count": 118},
        {"title": "Harper Wedding"})
    assert "Harper Wedding" in facts and "2026-07-12" in facts
    assert "the drink licence" in facts
    assert "EVT-ABCDEF12" in facts                        # per-event reply reference (§14)
    assert "118" in facts


def test_chase_run_one_bad_stakeholder_does_not_stop_run(monkeypatch):
    cap = {}
    _patch_events_os_io(monkeypatch, cap)

    calls = {"n": 0}
    real = events_os.compose_chase

    async def flaky(tenant_id, cfg, s, event, deal, *, now, auto=False):
        calls["n"] += 1
        if s["id"] == "bad":
            raise RuntimeError("compose blew up")
        return await real(tenant_id, cfg, s, event, deal, now=now, auto=auto)
    monkeypatch.setattr(events_os, "compose_chase", flaky)
    queue = [_sh(id="bad", email="a@x.co"), _sh(id="good", email="b@x.co")]
    out = _run_chase(monkeypatch, queue=queue, cfg={})
    assert out["drafted"] == 1                            # the good one still drafted
    assert calls["n"] == 2                                # both attempted


# ══ 4. auto-resolve hook — promise resolves → stakeholder confirmed (§12.4) ════
def test_on_promise_resolved_flips_stakeholder_confirmed(monkeypatch):
    updates = []
    monkeypatch.setattr(events_os.db, "module_active", lambda *a, **k: True)
    monkeypatch.setattr(events_os.db, "crm_stakeholders_for_promise",
                        lambda pid, t, **k: [{"id": "s1", "confirmation": "pending"}])
    monkeypatch.setattr(events_os.db, "crm_update_event_stakeholder",
                        lambda sid, t, upd, **k: updates.append((sid, upd)))
    n = events_os.on_promise_resolved("t1", "p1")
    assert n == 1
    assert updates == [("s1", {"confirmation": "confirmed"})]


def test_on_promise_resolved_noop_for_non_events_tenant(monkeypatch):
    monkeypatch.setattr(events_os.db, "module_active", lambda *a, **k: False)
    monkeypatch.setattr(events_os.db, "crm_stakeholders_for_promise",
                        lambda *a, **k: pytest.fail("must not query for a non-events tenant"))
    assert events_os.on_promise_resolved("t1", "p1") == 0


def test_on_promise_resolved_skips_declined(monkeypatch):
    updates = []
    monkeypatch.setattr(events_os.db, "module_active", lambda *a, **k: True)
    monkeypatch.setattr(events_os.db, "crm_stakeholders_for_promise",
                        lambda pid, t, **k: [{"id": "s1", "confirmation": "declined"}])
    monkeypatch.setattr(events_os.db, "crm_update_event_stakeholder",
                        lambda *a, **k: updates.append(a))
    assert events_os.on_promise_resolved("t1", "p1") == 0     # a manual decline is not overridden
    assert updates == []


def test_intel_resolve_calls_events_hook_only_for_them(monkeypatch):
    """crm_intel.apply_promises fires the stakeholder-confirm hook when a THEY_OWE
    promise auto-resolves — and not for a we_owe one."""
    from app import crm_intel
    fired = []
    monkeypatch.setattr(crm_intel.db, "crm_resolve_promise_auto", lambda *a, **k: None)
    monkeypatch.setattr(crm_intel.db, "crm_suggest_promise_resolved", lambda *a, **k: None)
    monkeypatch.setattr(events_os, "on_promise_resolved",
                        lambda t, pid, **k: fired.append(pid))
    promises = [{"action": "resolve", "who": "them", "description": "licence",
                 "existing_promise_id": "p1", "confidence": 0.9, "resolution_evidence": "confirmed"}]
    crm_intel.apply_promises("t1", promises, contact_id="c1", company_id=None,
                             deal_id="d1", email_log_id="log1",
                             open_promises=[{"id": "p1"}])
    assert fired == ["p1"]


def test_intel_resolve_does_not_fire_hook_for_we_owe(monkeypatch):
    from app import crm_intel
    fired = []
    monkeypatch.setattr(crm_intel.db, "crm_resolve_promise_auto", lambda *a, **k: None)
    monkeypatch.setattr(events_os, "on_promise_resolved",
                        lambda t, pid, **k: fired.append(pid))
    promises = [{"action": "resolve", "who": "us", "description": "the quote",
                 "existing_promise_id": "p1", "confidence": 0.9, "resolution_evidence": "sent"}]
    crm_intel.apply_promises("t1", promises, contact_id="c1", company_id=None,
                             deal_id="d1", email_log_id="log1",
                             open_promises=[{"id": "p1"}])
    assert fired == []                                       # our own promise → not a stakeholder flip


# ══ 5. the chase-now + mute routes (CSRF, IDOR-safe) ═══════════════════════════
@pytest.fixture
def events_client(monkeypatch):
    import app.dashboard as dashboard
    import app.events_routes as er
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"events"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(er.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), er
    app.dependency_overrides.clear()


def test_chase_now_route_queues_a_draft(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders_by_id",
                        lambda sid, t, **k: [{"id": "s1", "event_id": "e1",
                                              "confirmation": "pending", "chase_count": 0,
                                              "email": "bar@x.co", "role": "bar",
                                              "outstanding": "the licence",
                                              "outstanding_promise_id": "p1"}])
    monkeypatch.setattr(er.db, "crm_get_event",
                        lambda eid, t, **k: {"id": "e1", "deal_id": "d1", "event_date": "2026-07-12"})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda did, t, **k: {"title": "Harper Wedding"})
    monkeypatch.setattr(er.db, "effective_config", lambda *a, **k: {})
    called = {}

    async def fake_compose(tid, cfg, s, event, deal, *, now, auto=False):
        called["auto"] = auto
        return "drafted"
    monkeypatch.setattr(er.events_os, "compose_chase", fake_compose)
    r = client.post("/app/events/stakeholders/s1/chase", follow_redirects=False)
    assert r.status_code == 303 and "msg=chased" in r.headers["location"]
    assert called["auto"] is False                           # chase-now is always a draft


def test_chase_now_route_404_for_foreign_stakeholder(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders_by_id", lambda *a, **k: [])
    r = client.post("/app/events/stakeholders/nope/chase", follow_redirects=False)
    assert r.status_code == 404                              # IDOR-safe


def test_mute_route_sets_muted(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders_by_id",
                        lambda sid, t, **k: [{"id": "s1", "event_id": "e1",
                                              "confirmation": "pending"}])
    monkeypatch.setattr(er.db, "crm_get_event", lambda *a, **k: {"id": "e1", "deal_id": "d1"})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda *a, **k: {})
    updates = {}
    monkeypatch.setattr(er.db, "crm_update_event_stakeholder",
                        lambda sid, t, upd, **k: updates.update(upd))
    r = client.post("/app/events/stakeholders/s1/mute", data={"on": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert updates == {"confirmation": "muted"}


def test_unmute_route_restores_pending(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders_by_id",
                        lambda sid, t, **k: [{"id": "s1", "event_id": "e1",
                                              "confirmation": "muted"}])
    monkeypatch.setattr(er.db, "crm_get_event", lambda *a, **k: {"id": "e1", "deal_id": "d1"})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda *a, **k: {})
    updates = {}
    monkeypatch.setattr(er.db, "crm_update_event_stakeholder",
                        lambda sid, t, upd, **k: updates.update(upd))
    r = client.post("/app/events/stakeholders/s1/mute", data={"on": "0"},
                    follow_redirects=False)
    assert updates == {"confirmation": "pending"}            # chasing resumes


def test_unmute_does_not_clobber_confirmed(events_client, monkeypatch):
    """Un-muting a stakeholder that is actually confirmed must not rewrite it to pending."""
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders_by_id",
                        lambda sid, t, **k: [{"id": "s1", "event_id": "e1",
                                              "confirmation": "confirmed"}])
    monkeypatch.setattr(er.db, "crm_get_event", lambda *a, **k: {"id": "e1", "deal_id": "d1"})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda *a, **k: {})
    updates = {}
    monkeypatch.setattr(er.db, "crm_update_event_stakeholder",
                        lambda sid, t, upd, **k: updates.update(upd))
    client.post("/app/events/stakeholders/s1/mute", data={"on": "0"}, follow_redirects=False)
    assert updates == {"confirmation": "confirmed"}          # unchanged


# ══ board + detail reflect the chase state ═════════════════════════════════════
def test_stakeholder_light_needs_human_at_max():
    s = {"id": "s1", "confirmation": "pending", "chase_count": 3}
    light = events_os._stakeholder_light(s)
    assert light["tone"] == "needs_human"


def test_stakeholder_light_chasing_shows_count():
    s = {"id": "s1", "confirmation": "pending", "chase_count": 2}
    light = events_os._stakeholder_light(s)
    assert light["tone"] == "chase" and light["chase_count"] == 2


def test_detail_view_enriches_stakeholders_with_chase_state(monkeypatch):
    monkeypatch.setattr(events_os.db, "crm_get_event",
                        lambda eid, t, **k: {"id": "e1", "deal_id": "d1"})
    monkeypatch.setattr(events_os.db, "crm_get_deal", lambda *a, **k: {"title": "Harper"})
    monkeypatch.setattr(events_os.db, "crm_list_event_guests", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_event_dietary_matrix", lambda *a, **k: {})
    monkeypatch.setattr(events_os.db, "crm_list_event_stakeholders",
                        lambda *a, **k: [{"id": "s1", "confirmation": "pending", "chase_count": 3}])
    data = events_os.detail_view("t1", "e1")
    s = data["stakeholders"][0]
    assert s["tone"] == "needs_human" and s["needs_human"] is True
    assert data["max_chases"] == events_os.MAX_CHASES
