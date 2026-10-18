import json
import pytest
from fastapi.testclient import TestClient
from app import concierge, db
from app.auth import TenantCtx, require_tenant
from app.main import app


@pytest.fixture
def client(monkeypatch):
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="this-tenant", client=None)
    monkeypatch.setattr(db, "get_scheduling_config", lambda tid, **kw: {
        "service_types": [{"name": "A"}, {"name": "B"}], "resource_types": [{"name": "Bay"}]})
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_browser_cannot_forge_parsed_fields_or_validation(client):
    r = client.post("/concierge/settings-rows", json={"page": "diary_settings", "values": [
        {"name": "service", "value": "Check | 45 | | time | days: money",
         "fields": {"days": ["mon"]}, "problems": [], "ok": True}]})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["values"][0]["fields"] is None


def test_scopes_are_verified_against_the_authenticated_tenant(client, monkeypatch):
    called = []
    def read(tid, **kw):
        called.append(tid)
        return {"service_types": [{"name": "A"}, {"name": "B"}]}
    monkeypatch.setattr(db, "get_scheduling_config", read)
    payload = {"page": "booking_details", "values": [
        {"name": "field", "value": "Evidence | optional | document | only for: A; B"}]}
    r = client.post("/concierge/settings-rows", json=payload)
    assert r.json()["ok"]
    assert r.json()["values"][0]["fields"]["services"] == ["A", "B"]
    assert called == ["this-tenant"]
    payload["values"][0]["value"] += "; Other tenant's service"
    assert client.post("/concierge/settings-rows", json=payload).json()["ok"] is False


def test_failed_saved_read_is_not_empty_success(client, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(db, "get_scheduling_config", broken)
    r = client.post("/concierge/settings-rows", json={"page": "diary_settings", "values": [
        {"name": "service", "value": "Check | 45 | | time"}]})
    assert r.status_code == 503


def test_dependants_reference_read_is_strict(client, monkeypatch):
    def groups(tid, *, client, strict):
        assert tid == "this-tenant" and strict is True
        raise RuntimeError("unavailable")
    monkeypatch.setattr(db, "list_requirement_groups", groups)
    r = client.post("/concierge/settings-rows", json={"page": "dependants", "values": [
        {"name": "field", "value": "Item | Detail | text | optional | once | no gate"}]})
    assert r.status_code == 503


def test_help_retains_bad_rows_with_visible_reasons():
    result = concierge._parse_help(json.dumps({"reply": "Please check the card.", "recommend": [
        {"page": "diary_settings", "values": [{"name": "service", "value": "Check | 45 | | time | unknown"}]}]}))
    row = result[3][0]["values"][0]
    assert row["problems"]
    assert row["display"].startswith("Needs your answer:")


def test_help_summary_contains_only_the_parsed_resource_and_days():
    result = concierge._parse_help(json.dumps({"reply": "Please check the card.", "recommend": [
        {"page": "diary_settings", "values": [{"name": "service", "value": "Check | 45 | | time | pool: Bay | days: sat"}]}]}))
    row = result[3][0]["values"][0]
    assert "on Bay" in row["display"] and "Saturday" in row["display"]


def test_new_help_boundary_keeps_existing_cross_row_rules():
    recs = concierge._parse_help(json.dumps({"reply": "Review these.", "recommend": [
        {"page": "diary_settings", "values": [{"name": "service", "value": "Long | 120 | | time"},
                                               {"name": "hours_unit", "value": "hour"}]},
        {"page": "booking_details", "values": [{"name": "stay_mode", "value": "1"},
            {"name": "field", "value": "Collection date and time | required"},
            {"name": "field", "value": "Contact number | required"}]}]}))[3]
    assert recs[0]["values"][1]["value"] == "minutes"
    assert len(recs[1]["values"]) == 2
    assert recs[1]["values"][1]["fields"]["label"] == "Contact number"


def test_nullable_legacy_lists_are_a_successful_empty_read(client, monkeypatch):
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **kw: {
        "service_types": None, "resource_types": None, "intake_fields": None})
    r = client.post("/concierge/settings-rows", json={"page": "diary_settings", "values": [
        {"name": "service", "value": "Check | 45 | | time"}]})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_named_at_once_requires_verified_stay_mode(client, monkeypatch):
    row = {"name": "service", "value": {"name": "Review", "minutes": 45, "timing": "time", "at_once": 2}}
    result = client.post("/concierge/settings-rows", json={"page": "diary_settings", "values": [row]}).json()
    assert not result["ok"]
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **kw: {"booking_mode": "stay"})
    result = client.post("/concierge/settings-rows", json={"page": "diary_settings", "values": [row]}).json()
    assert result["ok"] and result["values"][0]["fields"]["at_once"] == 2
