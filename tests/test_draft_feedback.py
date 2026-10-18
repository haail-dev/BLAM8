"""Thumbs-down draft feedback (/app/drafts/{id}/correct): a 'never say' rule is appended to
the tenant's permanent Never-do list (deduped) and the reply is re-drafted. Route-level via
TestClient, db + pipeline mocked (in-process, no network)."""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def env(monkeypatch):
    saved = {}

    async def fake_redraft(tenant_id, log_id, client=None):
        return "Re-drafted reply."
    monkeypatch.setattr(dashboard.pipeline, "redraft_email_log", fake_redraft)
    monkeypatch.setattr(dashboard.db, "get_tenant_config",
                        lambda *a, **k: {"never_do": saved.get("never_do", "")})
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.update(cfg))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), saved
    app.dependency_overrides.clear()


def test_correct_appends_rule_and_redrafts(env):
    client, saved = env
    r = client.post("/app/drafts/d1/correct", data={"never": "Never offer free delivery"})
    assert r.status_code == 200
    body = r.json()
    assert body["draft"] == "Re-drafted reply." and body["saved"] is True
    assert saved["never_do"] == "Never offer free delivery"


def test_correct_dedupes_existing_rule(env):
    client, saved = env
    saved["never_do"] = "Never offer free delivery"
    r = client.post("/app/drafts/d1/correct", data={"never": "  Never offer free delivery  "})
    body = r.json()
    assert body["saved"] is False                          # already present → not re-added
    assert saved["never_do"] == "Never offer free delivery"
    assert body["draft"] == "Re-drafted reply."            # still re-drafts


def test_correct_appends_to_existing_rules(env):
    client, saved = env
    saved["never_do"] = "Never quote a firm price"
    client.post("/app/drafts/d1/correct", data={"never": "Don't mention weekend collections"})
    assert saved["never_do"] == "Never quote a firm price\nDon't mention weekend collections"


def test_correct_blank_just_redrafts(env):
    client, saved = env
    r = client.post("/app/drafts/d1/correct", data={"never": ""})
    body = r.json()
    assert body["saved"] is False and body["draft"] == "Re-drafted reply."
    assert "never_do" not in saved                          # nothing written


def test_conversation_renders_icon_actions(monkeypatch):
    # The thread view shows the new thumbs-up / skip / thumbs-down icon row + panels.
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [
        {"id": "d9", "status": "drafted", "draft_reply": "Hi there.",
         "body_in": "Can you help?", "created_at": "2026-07-01T10:00:00", "subject": "Hi"}])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        html = TestClient(app).get("/app/conversations/view?contact=sarah@x.co").text
    finally:
        app.dependency_overrides.clear()
    assert 'aria-label="Approve and send"' in html
    assert 'data-pop="skip-d9"' in html                     # skip panel still inline
    # Thumbs-down now opens the assistant in fix mode (2026-07-02) — the inline
    # "Something's wrong" panel and its legacy /app/drafts detour are gone.
    assert 'data-fix="d9"' in html
    assert 'data-pop="wrong-d9"' not in html and 'data-correct="d9"' not in html
    assert "/app/drafts/d9?fix=1" not in html
    assert "Ignore this contact" not in html                # ignoring left the thread (Kris 2026-09-20)
