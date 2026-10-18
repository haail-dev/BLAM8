"""Field Service settings routes — the engineers / job-types / pricing pages render and
their forms parse correctly (docs/modules/module-field-service.md). Route-level via TestClient:
auth + CSRF overridden, db monkeypatched — in-process, no network. TestClient WITHOUT a
context manager on purpose (startup events must not run).

Run: python -m pytest tests/test_fsm_routes.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client(monkeypatch):
    # _ctx + the fsm gate
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_engineers_page_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_engineers", lambda *a, **k: [
        {"id": "e1", "name": "Sam", "skills": ["gas", "plumbing"],
         "daily_minutes": 480, "active": True}])
    monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
    r = client.get("/app/fsm/engineers")
    assert r.status_code == 200
    assert "Sam" in r.text and "gas, plumbing" in r.text


def test_job_types_page_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_job_types", lambda *a, **k: [
        {"id": "j1", "name": "Boiler service", "default_minutes": 60, "required_skill": "gas",
         "default_price": None, "emergency": False, "service_months": 12}])
    r = client.get("/app/fsm/job-types")
    assert r.status_code == 200 and "Boiler service" in r.text


def test_pricing_page_renders_with_sample(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_fsm_rate_card", lambda *a, **k:
                        {"callout_charge": 60, "hourly_rate": 55, "min_charge": 75, "vat_pct": 20})
    r = client.get("/app/fsm/pricing")
    assert r.status_code == 200 and "Sample" in r.text and "138" in r.text   # 60+55+VAT


def test_board_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.scheduling, "fsm_board", lambda *a, **k: {
        "days": [{"date": "2026-07-02", "label": "Thu 02 Jul", "is_today": True,
                  "engineers": [], "total_jobs": 0, "emergencies": 0}],
        "summary": "Engineer diary", "engineers": [{"id": "e1"}],
        "has_engineers": True, "job_types": []})
    r = client.get("/app/fsm")
    assert r.status_code == 200


def test_add_engineer_parses_skills_and_hours(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
    captured = {}
    monkeypatch.setattr(dashboard.db, "add_engineer",
                        lambda tid, name, **k: captured.update({"name": name, **k}))
    r = client.post("/app/fsm/engineers",
                    data={"name": "Alex", "skills": "gas; plumbing", "hours": "7.5"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert captured["name"] == "Alex"
    assert captured["skills"] == ["gas", "plumbing"]   # ';' and ',' both split
    assert captured["daily_minutes"] == 450            # 7.5h × 60


def test_add_job_type_parses_recurrence_and_price(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard.db, "add_job_type",
                        lambda tid, name, **k: captured.update({"name": name, **k}))
    r = client.post("/app/fsm/job-types",
                    data={"name": "Boiler service", "minutes": "60", "skill": "gas",
                          "price": "", "emergency": "", "months": "12"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert captured["service_months"] == 12 and captured["default_price"] is None
    assert captured["required_skill"] == "gas" and captured["emergency"] is False
