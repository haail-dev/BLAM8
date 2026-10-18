"""CRM gating via the 'crm' switchboard module.

Tier placement (2026-07-25, Julius — supersedes the 07-24 both-tiers collapse):
Starter (£39, key 'email') is email + scheduling; the CRM unlocks on Pro (£99,
key 'starter') and every tier above. The zero-LLM ingest still pre-fills for
EVERY tenant (the upgrade teaser). The gating MACHINERY is unchanged and still
enforced; plan-less pilots have no modules. Pins the four enforcement seams:
  * plans bundle 'crm' from Pro up — never on the £39 'email' key (sold as
    Starter), never on pilots/unknown tiers
  * /app/crm pages and /api/crm endpoints 404 without the module (and open
    with it — the with-module path is pinned across test_crm_routes.py)
  * the pipeline runs the zero-LLM ingest for EVERY tenant (the CRM pre-fills
    for upgrade day) but skips the LLM passes — field extraction and
    deal/promise/sentiment intel — without the module
  * the sequence engine exits an enrollment whose tenant lost the module
    (downgrade must stop sends, not just hide pages)

Run: python -m pytest tests/test_crm_gating.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import agent, crm, crm_extract, crm_fields, crm_intel, db, pipeline, plans, usage
from app import crm_sequences as seqmod
from app import knowledge as kb
from app.auth import TenantCtx, require_onboarded
from app.main import app

EMAIL = {"from_address": "ops@acme.co.uk", "subject": "Quote", "body": "Friday?"}


# ── plans: the entitlement bundles ───────────────────────────────────────────

def test_crm_bundles_from_pro_up_never_starter_or_pilots():
    # Tier ladder (2026-07-25): Starter £39 = email + scheduling only; the CRM
    # unlocks on Pro (£99, key 'starter') and every tier above.
    assert plans.BASE_PLAN_MODULES["email"] == {"scheduling"}
    for tier in ("starter", "professional", "scale", "enterprise"):
        assert plans.BASE_PLAN_MODULES[tier] >= {"scheduling", "crm"}, tier
    assert "crm" in plans.MANAGED_MODULES          # reconciled on plan changes
    assert "crm" not in plans.modules_for("email", "logistics")
    assert "scheduling" in plans.modules_for("email", "logistics")
    assert "crm" in plans.modules_for("starter", None)
    # free/unknown tiers still get nothing — the module gate stays real
    assert plans.modules_for("pilot", None) == set()
    assert plans.modules_for(None, None) == set()


# ── routes: pages + API 404 without the module ──────────────────────────────

@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_crm_pages_become_a_locked_door_and_the_api_still_404s(client, monkeypatch):
    """Kris 2026-09-13: "when someone has used the trial and gone for Starter,
    the CRM should be locked" — the front door opens on the lock (what the CRM
    is, what it has already met, the way to Pro), every other CRM page sends
    them to that door, and the machine endpoints keep their 404."""
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_companies", lambda *a, **k: [])
    door = client.get("/app/crm")
    assert door.status_code == 200 and "Your CRM is locked on this plan" in door.text
    nothing_usable = TestClient(app, follow_redirects=False)
    for path in ("/app/crm/pipeline", "/app/crm/tasks", "/app/crm/reports", "/app/crm/sequences"):
        r = nothing_usable.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/app/crm", path
    for path in ("/api/crm/contacts", "/api/crm/pipeline"):
        assert client.get(path).status_code == 404, path


def test_crm_page_opens_with_the_module(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    assert client.get("/app/crm").status_code == 200


def test_crm_preview_lights_the_pages_too(client, monkeypatch):
    """Preview is an additive overlay on every module surface — CRM included."""
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    assert client.get("/app/crm").status_code == 200


# ── pipeline: ingest for everyone, LLM passes only with the module ──────────

class _PipelineEnv:
    """The trial-pipeline Env pattern: patch every collaborator of
    draft_for_email, record which CRM passes ran."""

    def __init__(self, monkeypatch, modules: set):
        self.ingested = 0
        self.extracted = 0
        self.intel = 0

        async def fake_draft(cfg, email, **kw):
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_retrieve(*a, **k):
            return []

        async def fake_extract(*a, **k):
            self.extracted += 1
            return {}

        async def fake_intel(*a, **k):
            self.intel += 1
            return {}

        def fake_ingest(tid, row):
            self.ingested += 1
            return {"contact_id": "ct1", "company_id": "co1"}

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
        monkeypatch.setattr(kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(kb, "render_for_prompt", lambda *a, **k: None)
        monkeypatch.setattr(db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "own_mailbox_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"industry": "logistics"})
        monkeypatch.setattr(db, "effective_config", lambda *a, **k: {})
        monkeypatch.setattr(db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("log-1", True))
        monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(db, "crm_list_field_definitions",
                            lambda *a, **k: [{"id": "f1", "entity": "contact"}])
        monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(crm, "ingest_email_log_row", fake_ingest)
        monkeypatch.setattr(crm_extract, "extract", fake_extract)
        monkeypatch.setattr(crm_fields, "apply_extraction",
                            lambda *a, **k: {"autofilled": 0, "suggested": 0})
        monkeypatch.setattr(crm_intel, "process_email", fake_intel)


def test_pipeline_without_crm_ingests_but_never_pays_llm(monkeypatch):
    env = _PipelineEnv(monkeypatch, modules=set())
    log_id = asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert log_id == "log-1"
    assert env.ingested == 1          # zero-LLM prefill runs for every tenant
    assert env.extracted == 0         # …but the email tier pays no CRM AI costs
    assert env.intel == 0


def test_pipeline_with_crm_runs_the_llm_passes(monkeypatch):
    env = _PipelineEnv(monkeypatch, modules={"crm"})
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.ingested == 1
    assert env.extracted == 1
    assert env.intel == 1


# ── sequence engine: a downgraded tenant's enrollments stop, loudly ──────────

def test_sequence_engine_exits_enrollment_when_module_off(monkeypatch):
    exited = []
    monkeypatch.setattr(seqmod.db, "module_active", lambda *a, **k: False)
    monkeypatch.setattr(seqmod.db, "crm_exit_enrollment",
                        lambda eid, tid, reason: exited.append((eid, reason)))
    out = asyncio.run(seqmod._process_one(
        {"id": "e1", "tenant_id": "t1", "sequence_id": "sq1", "contact_id": "ct1"}))
    assert out == "exited"
    assert exited == [("e1", "module_off")]
