"""CRM Phase 4b — multi-step follow-up sequences (§19.3).

Covers the engine (advance / exit-on-reply / completion / rate-limit), template
substitution, the guardrails, and the route CRUD happy-paths + rejections.

Everything is monkeypatched off app.db / app.agent / app.mailbox / app.usage — the
autouse loud-guard in conftest fails any test that reaches live Supabase.

Run: python -m pytest tests/test_crm_sequences.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.crm_routes as crm_routes
import app.dashboard as dashboard
from app import crm_sequences as seqmod
from app import db
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── render_template ──────────────────────────────────────────────────────────

def test_render_template_substitutes_and_blanks_missing():
    ctx = {"contact": {"name": "Rob", "email": "rob@x.co"},
           "company": {"name": "Acme"},
           "deal": {"title": "Pallet run", "stage": "Prospect", "value": 1200},
           "tenant": {"business_name": "Koth", "sender_name": "Sam"},
           "days_since_last_contact": 9, "promise_summary": ""}
    out = seqmod.render_template(
        "Hi {{contact.name}} at {{company.name}} re {{deal.title}} "
        "(£{{deal.value}}, {{days_since_last_contact}}d) — {{missing.key}} {{nope}}", ctx)
    assert out == "Hi Rob at Acme re Pallet run (£1200, 9d) —  "


def test_render_template_empty_body_is_empty():
    assert seqmod.render_template("", {}) == ""
    assert seqmod.render_template(None, {}) == ""


# ── engine ───────────────────────────────────────────────────────────────────

class _Result(SimpleNamespace):
    pass


def _stub_common(monkeypatch, *, cfg=None):
    """Stub the reads every _process_one path touches."""
    # CRM unlocks at Core (2026-07-24): the engine exits any enrollment whose
    # tenant lost the module — these tests exercise an entitled tenant.
    monkeypatch.setattr(seqmod.db, "module_active", lambda *a, **k: True)
    monkeypatch.setattr(seqmod.db, "effective_config", lambda *a, **k: cfg or {"auto_send": False})
    monkeypatch.setattr(seqmod.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
    monkeypatch.setattr(seqmod.db, "crm_get_contact",
                        lambda cid, tid, **k: {"id": cid, "email": "rob@x.co", "name": "Rob",
                                               "last_activity_at": None, "crm_companies": None})
    monkeypatch.setattr(seqmod.db, "crm_get_deal", lambda did, tid, **k: None)
    monkeypatch.setattr(seqmod.db, "crm_contact_replied_since", lambda *a, **k: False)
    monkeypatch.setattr(seqmod.db, "crm_sequenced_today", lambda *a, **k: False)
    monkeypatch.setattr(seqmod.db, "log_agent_event", lambda *a, **k: None)


def test_engine_advances_a_due_enrollment_and_composes(monkeypatch):
    seq = {"id": "sq1", "status": "active", "name": "Re-engage"}
    steps = [
        {"id": "s1", "step_order": 0, "delay_days": 3, "action_type": "send_email",
         "exit_on_reply": True, "template_subject": "Hi {{contact.name}}",
         "template_body": "check in"},
        {"id": "s2", "step_order": 1, "delay_days": 5, "action_type": "send_email",
         "exit_on_reply": True, "template_subject": "again", "template_body": "value"},
    ]
    _stub_common(monkeypatch)
    monkeypatch.setattr(seqmod.db, "crm_get_sequence", lambda *a, **k: seq)
    monkeypatch.setattr(seqmod.db, "crm_list_steps", lambda *a, **k: steps)

    calls = {"insert": [], "usage": [], "advance": [], "sent": []}

    async def fake_compose(cfg, purpose, facts, **kwargs):
        return _Result(text="Hi Rob, just checking in.", usage={"input_tokens": 5},
                       model="fake-model")
    monkeypatch.setattr(seqmod.agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(seqmod.db, "insert_email_log",
                        lambda *a, **k: (calls["insert"].append((a, k)) or ("log-1", True)))
    monkeypatch.setattr(seqmod.usage, "record_anthropic",
                        lambda *a, **k: calls["usage"].append(k))
    monkeypatch.setattr(seqmod.db, "crm_advance_enrollment",
                        lambda eid, tid, **k: calls["advance"].append(k))

    async def fake_send(*a, **k):
        calls["sent"].append(a)
    monkeypatch.setattr(seqmod.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(seqmod.db, "mark_email_log_sent", lambda *a, **k: None)

    enr = {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "c1",
           "deal_id": None, "current_step": 0, "enrolled_at": "2026-06-01T00:00:00+00:00"}
    outcome = asyncio.run(seqmod._process_one(enr))

    assert outcome == "advanced"
    # composed + persisted as a pending 'sequence' outbound (subject rendered)
    a, kw = calls["insert"][0]
    assert kw["kind"] == "sequence" and kw["status"] == "pending"
    assert a[2] == "Hi Rob"                 # subject arg
    assert calls["usage"][0]["operation"] == "sequence"
    assert calls["sent"] == []              # draft-only tenant never auto-sends
    # advanced to step index 1 with a re-armed timer
    assert calls["advance"][0]["current_step"] == 1
    assert calls["advance"][0]["next_action_at"]


def test_engine_auto_send_tenant_sends(monkeypatch):
    seq = {"id": "sq1", "status": "active", "name": "x"}
    steps = [{"id": "s1", "step_order": 0, "delay_days": 3, "action_type": "send_email",
              "exit_on_reply": False, "template_subject": "hi", "template_body": "b"}]
    _stub_common(monkeypatch, cfg={"auto_send": True})
    monkeypatch.setattr(seqmod.db, "crm_get_sequence", lambda *a, **k: seq)
    monkeypatch.setattr(seqmod.db, "crm_list_steps", lambda *a, **k: steps)

    async def fake_compose(cfg, purpose, facts, **kwargs):
        return _Result(text="body", usage={}, model="m")
    monkeypatch.setattr(seqmod.agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(seqmod.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(seqmod.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(seqmod.db, "crm_advance_enrollment", lambda *a, **k: None)
    monkeypatch.setattr(seqmod.db, "crm_complete_enrollment", lambda *a, **k: None)
    sent = []

    async def fake_send(tid, to, subj, body, **k):
        sent.append((to, subj))
    monkeypatch.setattr(seqmod.mailbox, "send_mail", fake_send)
    marked = []
    monkeypatch.setattr(seqmod.db, "mark_email_log_sent",
                        lambda *a, **k: marked.append((a, k)))

    enr = {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "c1",
           "deal_id": None, "current_step": 0, "enrolled_at": "2026-06-01T00:00:00+00:00"}
    outcome = asyncio.run(seqmod._process_one(enr))
    assert outcome == "completed"           # only step → completes after sending
    assert sent and sent[0][0] == "rob@x.co"
    assert marked and marked[0][1]["via"] == "auto"


def test_engine_exits_on_reply(monkeypatch):
    seq = {"id": "sq1", "status": "active", "name": "x"}
    steps = [{"id": "s1", "step_order": 0, "delay_days": 3, "action_type": "send_email",
              "exit_on_reply": True, "template_subject": "hi", "template_body": "b"}]
    _stub_common(monkeypatch)
    monkeypatch.setattr(seqmod.db, "crm_get_sequence", lambda *a, **k: seq)
    monkeypatch.setattr(seqmod.db, "crm_list_steps", lambda *a, **k: steps)
    monkeypatch.setattr(seqmod.db, "crm_contact_replied_since", lambda *a, **k: True)
    exits = []
    monkeypatch.setattr(seqmod.db, "crm_exit_enrollment",
                        lambda eid, tid, reason: exits.append((eid, reason)))

    enr = {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "c1",
           "deal_id": None, "current_step": 0, "enrolled_at": "2026-06-01T00:00:00+00:00"}
    outcome = asyncio.run(seqmod._process_one(enr))
    assert outcome == "replied"
    assert exits == [("e1", "replied")]


def test_engine_completes_when_no_next_step(monkeypatch):
    seq = {"id": "sq1", "status": "active", "name": "x"}
    steps = [{"id": "s1", "step_order": 0, "delay_days": 3, "action_type": "create_task",
              "exit_on_reply": False, "template_subject": "Follow up", "template_body": "b"}]
    _stub_common(monkeypatch)
    monkeypatch.setattr(seqmod.db, "crm_get_sequence", lambda *a, **k: seq)
    monkeypatch.setattr(seqmod.db, "crm_list_steps", lambda *a, **k: steps)
    tasks = []
    monkeypatch.setattr(seqmod.db, "crm_create_task",
                        lambda tid, **k: tasks.append(k))
    monkeypatch.setattr(seqmod.db, "crm_advance_enrollment", lambda *a, **k: None)
    completed = []
    monkeypatch.setattr(seqmod.db, "crm_complete_enrollment",
                        lambda eid, tid: completed.append(eid))

    enr = {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "c1",
           "deal_id": None, "current_step": 0, "enrolled_at": "2026-06-01T00:00:00+00:00"}
    outcome = asyncio.run(seqmod._process_one(enr))
    assert outcome == "completed"
    assert tasks and tasks[0]["source"] == "auto_sequence"
    assert completed == ["e1"]


def test_engine_rate_limit_reschedules(monkeypatch):
    seq = {"id": "sq1", "status": "active", "name": "x"}
    steps = [{"id": "s1", "step_order": 0, "delay_days": 3, "action_type": "send_email",
              "exit_on_reply": True, "template_subject": "hi", "template_body": "b"}]
    _stub_common(monkeypatch)
    monkeypatch.setattr(seqmod.db, "crm_get_sequence", lambda *a, **k: seq)
    monkeypatch.setattr(seqmod.db, "crm_list_steps", lambda *a, **k: steps)
    monkeypatch.setattr(seqmod.db, "crm_sequenced_today", lambda *a, **k: True)
    resched = []
    monkeypatch.setattr(seqmod.db, "crm_reschedule_enrollment",
                        lambda eid, tid, nxt: resched.append((eid, nxt)))

    enr = {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "c1",
           "deal_id": None, "current_step": 0, "enrolled_at": "2026-06-01T00:00:00+00:00"}
    outcome = asyncio.run(seqmod._process_one(enr))
    assert outcome == "rate_limited"
    assert resched and resched[0][0] == "e1"


def test_process_queue_isolates_failures(monkeypatch):
    monkeypatch.setattr(seqmod.db, "crm_due_enrollments", lambda *a, **k: [
        {"id": "bad", "tenant_id": "t1"}, {"id": "good", "tenant_id": "t1"}])

    async def flaky(enr):
        if enr["id"] == "bad":
            raise RuntimeError("boom")
        return "advanced"
    monkeypatch.setattr(seqmod, "_process_one", flaky)
    summary = asyncio.run(seqmod.process_sequence_queue())
    assert summary["due"] == 2 and summary["failed"] == 1 and summary["advanced"] == 1


def test_default_sequences_per_vertical():
    assert seqmod.default_sequences("trades")[0]["name"] == "Quote follow-up"
    assert seqmod.default_sequences("logistics")[0]["name"] == "RFQ response"
    assert seqmod.default_sequences(None)[0]["trigger_type"] == "gone_quiet"
    assert seqmod.default_sequences("unknown-vertical")[0]["name"].startswith("Gone quiet")


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from app import crm_routes
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    # CRM unlocks at Core (2026-07-24): these tests exercise an entitled tenant,
    # so the router-level module gate is overridden like the auth deps above.
    app.dependency_overrides[crm_routes._require_crm_module] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def _nav_stubs(monkeypatch):
    """The _ctx nav reads that every /app page hits."""
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})   # CRM unlocks at Core (2026-07-24)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)


def test_create_sequence_validates_and_inserts(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_count_active_sequences", lambda *a, **k: 0)
    created = []
    monkeypatch.setattr(crm_routes.db, "crm_create_sequence",
                        lambda tid, **k: (created.append(k) or {"id": "sq1", **k}))
    r = client.post("/api/crm/sequences",
                    data={"name": "Re-engage", "trigger_type": "gone_quiet"})
    assert r.status_code == 200 and r.json()["sequence"]["id"] == "sq1"
    assert created[0]["trigger_type"] == "gone_quiet"
    # blank name + bad trigger both 422
    assert client.post("/api/crm/sequences", data={"name": "  "}).status_code == 422
    assert client.post("/api/crm/sequences",
                       data={"name": "x", "trigger_type": "nope"}).status_code == 422


def test_create_sequence_rejects_sixth_active(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_count_active_sequences", lambda *a, **k: 5)
    r = client.post("/api/crm/sequences", data={"name": "Sixth"})
    assert r.status_code == 422 and "active" in r.json()["detail"].lower()


def test_add_step_rejects_eighth(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: {"id": "sq1"})
    monkeypatch.setattr(crm_routes.db, "crm_list_steps",
                        lambda *a, **k: [{"id": f"s{i}"} for i in range(7)])
    r = client.post("/api/crm/sequences/sq1/steps",
                    data={"delay_days": 3, "action_type": "send_email"})
    assert r.status_code == 422 and "step" in r.json()["detail"].lower()


def test_add_step_happy_path(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: {"id": "sq1"})
    monkeypatch.setattr(crm_routes.db, "crm_list_steps", lambda *a, **k: [])
    added = []
    monkeypatch.setattr(crm_routes.db, "crm_add_step",
                        lambda tid, sid, **k: (added.append(k) or {"id": "s1", **k}))
    r = client.post("/api/crm/sequences/sq1/steps",
                    data={"delay_days": 4, "action_type": "send_email",
                          "template_subject": "hi", "exit_on_reply": "true"})
    assert r.status_code == 200
    assert added[0]["step_order"] == 0 and added[0]["delay_days"] == 4
    # bad action_type 422
    assert client.post("/api/crm/sequences/sq1/steps",
                       data={"action_type": "explode"}).status_code == 422


def test_enroll_blocked_by_do_not_sequence(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: {"id": "sq1"})
    monkeypatch.setattr(crm_routes.db, "crm_enroll", lambda *a, **k: None)  # blocked
    r = client.post("/api/crm/sequences/sq1/enroll", data={"contact_id": "c1"})
    assert r.status_code == 422


def test_enroll_happy_path(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: {"id": "sq1"})
    monkeypatch.setattr(crm_routes.db, "crm_enroll",
                        lambda tid, sid, cid, **k: {"id": "e1", "contact_id": cid})
    r = client.post("/api/crm/sequences/sq1/enroll", data={"contact_id": "c1"})
    assert r.status_code == 200 and r.json()["enrollment"]["id"] == "e1"


def test_archive_sequence(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: {"id": "sq1"})
    updates = []
    monkeypatch.setattr(crm_routes.db, "crm_update_sequence",
                        lambda sid, tid, fields, **k: updates.append(fields))
    r = client.request("DELETE", "/api/crm/sequences/sq1", data={})
    assert r.status_code == 200 and updates[0]["status"] == "archived"


def test_sequence_404_across_tenants(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_sequence", lambda *a, **k: None)
    assert client.get("/api/crm/sequences/other").status_code == 404


def test_pause_and_exit_enrollment(client, monkeypatch):
    monkeypatch.setattr(crm_routes.db, "crm_get_enrollment", lambda *a, **k: {"id": "e1"})
    statuses = []
    monkeypatch.setattr(crm_routes.db, "crm_set_enrollment_status",
                        lambda eid, tid, status, **k: statuses.append(status))
    assert client.post("/api/crm/enrollments/e1/pause").status_code == 200
    assert client.post("/api/crm/enrollments/e1/exit").status_code == 200
    assert statuses == ["paused", "exited"]


def test_sequences_page_renders(client, monkeypatch):
    _nav_stubs(monkeypatch)
    monkeypatch.setattr(dashboard.db, "crm_list_sequences",
                        lambda tid, **k: [{"id": "sq1", "name": "Re-engage",
                                           "status": "active", "trigger_type": "gone_quiet",
                                           "description": None}])
    monkeypatch.setattr(dashboard.db, "crm_list_steps",
                        lambda sid, tid, **k: [{"id": "s1", "step_order": 0, "delay_days": 3,
                                                "action_type": "send_email",
                                                "template_subject": "Hi", "exit_on_reply": True}])
    monkeypatch.setattr(dashboard.db, "crm_list_stages", lambda tid, **k: [])
    r = client.get("/app/crm/sequences")
    assert r.status_code == 200
    assert "Re-engage" in r.text and "Sequences" in r.text
