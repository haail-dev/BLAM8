"""Onboarding resume lands on the furthest step reached.

Incident: first-walk re-run, 1 Oct 2026: bounced from Knowledge back to Voice.
An unfinished owner opening an app page goes to GET /onboarding; it always
resumed at Voice once a mailbox was connected, even after Voice was done.

Run: python -m pytest tests/test_onboarding_resume_step.py -q
"""
import pytest
from fastapi.testclient import TestClient

from app import onboarding
from app.auth import TenantCtx, require_tenant
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _state(monkeypatch, tenant, connected=False, cfg=None):
    monkeypatch.setattr(onboarding.db, "get_tenant", lambda *a, **k: tenant)
    monkeypatch.setattr(onboarding.db, "get_connection_status", lambda *a, **k: connected)
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda *a, **k: cfg or {})


def _where(client):
    r = client.get("/onboarding", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_no_sector_resumes_at_interview(client, monkeypatch):
    _state(monkeypatch, {"industry": None})
    assert _where(client) == "/onboarding/interview"


def test_sector_without_mailbox_resumes_at_mailbox(client, monkeypatch):
    _state(monkeypatch, {"industry": "x"}, connected=False)
    assert _where(client) == "/onboarding/mailbox"


def test_mailbox_voice_not_done_resumes_at_voice(client, monkeypatch):
    _state(monkeypatch, {"industry": "x"}, connected=True,
           cfg={"voice_learning_status": "ready", "voice_profile": {"a": 1}})
    assert _where(client) == "/onboarding/voice"


@pytest.mark.parametrize("source", ["auto_learned", "hybrid", "manual"])
def test_voice_done_resumes_at_knowledge(client, monkeypatch, source):
    _state(monkeypatch, {"industry": "x"}, connected=True,
           cfg={"voice_profile_source": source,"agent_name":"Office team","tone":"Professional","sign_off":"Regards, team"})
    assert _where(client) == "/onboarding/knowledge"


def test_onboarded_goes_to_app(client, monkeypatch):
    _state(monkeypatch, {"industry": "x", "onboarded_at": "2026-10-01"}, connected=True,
           cfg={"voice_profile_source": "manual"})
    assert _where(client) == "/app"
