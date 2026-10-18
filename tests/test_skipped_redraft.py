"""Skipped-card "Draft a reply again" (/app/drafts/{id}/redraft with return_to):
a skip isn't final — the form flow re-drafts the inbound, re-opens the row
(status back to pending) and redirects to the thread; without return_to the
route stays the JSON correction-flow API and never touches the status.

Route-level via TestClient: auth + CSRF dependencies overridden, db + pipeline
monkeypatched — in-process only, no network. TestClient used WITHOUT a context
manager on purpose: startup events (secret assert, pollers) never run.

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


class Env:
    def __init__(self, monkeypatch, redraft_result="Fresh draft."):
        self.redrafted = []
        self.statuses = []
        self.events = []

        async def fake_redraft(tenant_id, log_id, client=None):
            self.redrafted.append((tenant_id, log_id))
            return redraft_result

        monkeypatch.setattr(dashboard.pipeline, "redraft_email_log", fake_redraft)
        monkeypatch.setattr(dashboard.db, "get_email_log_entry",
                            lambda log_id, tid, **k: {"id": log_id, "from_address": "sam@x.co"})
        monkeypatch.setattr(dashboard.db, "set_email_log_status",
                            lambda log_id, status, tid, **k: self.statuses.append((log_id, status)))
        monkeypatch.setattr(dashboard.events, "publish",
                            lambda tid, payload: self.events.append(payload))


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_form_flow_redrafts_reopens_and_redirects(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/app/drafts/log1/redraft",
                    data={"return_to": "/app/conversations/view?contact=sam%40x.co"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/conversations/view?contact=sam%40x.co"
    assert env.redrafted == [("t1", "log1")]
    assert env.statuses == [("log1", "pending")]           # the skip is undone
    assert env.events == [{"type": "status_changed", "log_id": "log1",
                           "contact": "sam@x.co", "status": "pending"}]


def test_json_flow_unchanged_and_status_untouched(client, monkeypatch):
    # The correction flow posts no return_to — same JSON contract as before,
    # and the (already pending) row's status is never rewritten.
    env = Env(monkeypatch)
    r = client.post("/app/drafts/log1/redraft")
    assert r.status_code == 200 and r.json() == {"draft": "Fresh draft."}
    assert env.statuses == [] and env.events == []


def test_missing_row_404s_before_any_reopen(client, monkeypatch):
    env = Env(monkeypatch, redraft_result=None)
    r = client.post("/app/drafts/nope/redraft",
                    data={"return_to": "/app/conversations"}, follow_redirects=False)
    assert r.status_code == 404
    assert env.statuses == [] and env.events == []


def test_skipped_card_offers_draft_again(client, monkeypatch):
    # The thread's skipped bubble now carries the "Draft a reply again" form.
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [
        {"id": "d7", "status": "skipped", "draft_reply": "Old draft.",
         "body_in": "Any space Tuesday?", "created_at": "2026-07-30T09:00:00", "subject": "Tues"}])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "You · Skipped — no reply sent" in html
    assert 'action="/app/drafts/d7/redraft"' in html
    assert "Draft a reply again" in html
    assert 'name="return_to" value="/app/conversations/view?contact=sam%40x.co"' in html
