"""Feature requests & issue reports — the in-app form + the concierge chat
write path (POST /concierge/submit).

Route-level via TestClient (auth + CSRF overridden, db monkeypatched): the form
renders with the tenant's own list, a submit is trimmed + industry-tagged +
kind-tagged, an empty body is rejected, and the chat-approved draft files into
the same pipeline.

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from app.security import require_csrf, require_csrf_header


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class Env:
    def __init__(self, monkeypatch):
        self.added = []
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"tms"})
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "list_feature_requests", lambda *a, **k: [
            {"category": "Tracking", "body": "live ETA", "status": "new", "kind": "feature",
             "created_at": "2026-06-20T10:00:00+00:00"}])
        # dashboard.db and concierge_routes.db are the same module — one patch covers both
        monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"industry": "logistics"})
        monkeypatch.setattr(dashboard.db, "add_feature_request",
                            lambda tid, body, **kw: self.added.append({"tid": tid, "body": body, **kw}))


def test_requests_page_renders_form_and_list(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/requests")
    assert r.status_code == 200
    assert "Requests & reports" in r.text and "Send request" in r.text
    assert "Report an issue" in r.text            # the kind toggle is offered
    assert "live ETA" in r.text                    # the tenant's own submission


def test_submit_trims_and_tags_industry(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/requests", data={"category": "Tracking", "body": "  text the customer  "},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=thanks" in r.headers["location"]
    assert env.added[0]["body"] == "text the customer"     # trimmed
    assert env.added[0]["industry"] == "logistics"         # auto-tagged from the tenant
    assert env.added[0]["category"] == "Tracking"


def test_empty_body_is_rejected(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/requests", data={"category": "x", "body": "   "}, follow_redirects=False)
    assert "msg=empty" in r.headers["location"] and not env.added


def test_form_kind_defaults_and_coerces(client, monkeypatch):
    env = Env(monkeypatch)
    client.post("/app/requests", data={"category": "x", "body": "a"}, follow_redirects=False)
    client.post("/app/requests", data={"category": "x", "body": "b", "kind": "bug"},
                follow_redirects=False)
    client.post("/app/requests", data={"category": "x", "body": "c", "kind": "junk"},
                follow_redirects=False)
    assert [a["kind"] for a in env.added] == ["feature", "bug", "feature"]


# ── the chat write path: POST /concierge/submit ───────────────────────────────
def test_chat_submit_files_into_the_same_pipeline(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/concierge/submit",
                    json={"kind": "bug", "summary": "  Diary shows no slots on Mondays  "})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["link"]["href"] == "/app/requests"
    added = env.added[0]
    assert added["body"] == "Diary shows no slots on Mondays"      # trimmed
    assert added["kind"] == "bug"
    assert added["category"] == "Issue report"
    assert added["industry"] == "logistics"                        # auto-tagged
    assert added["submitted_by"] == "assistant chat"               # provenance


def test_chat_submit_feature_kind_and_category(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/concierge/submit",
                    json={"kind": "feature", "summary": "text customers a tracking link"})
    assert r.status_code == 200
    assert env.added[0]["kind"] == "feature"
    assert env.added[0]["category"] == "Feature idea"


def test_chat_submit_rejects_bad_kind_and_blank_summary(client, monkeypatch):
    env = Env(monkeypatch)
    assert client.post("/concierge/submit",
                       json={"kind": "rant", "summary": "x"}).status_code == 400
    assert client.post("/concierge/submit",
                       json={"kind": "bug", "summary": "   "}).status_code == 400
    assert not env.added
