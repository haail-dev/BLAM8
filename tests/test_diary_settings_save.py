"""Diary-settings save round-trip — the form arrays for the inbound-depth editors reach
db.upsert_scheduling_config as cleaned config. Route-level via TestClient with auth + CSRF
overridden and db monkeypatched (in-process, no network)."""
from tests.booking_fixtures import carried_config
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def make_saved(monkeypatch):
    """TestClient + captured upsert cfgs, with the module switchboard settable — the
    travel-toggle tests light the travel add-on exactly as the rendered form would."""
    def _make(mods=("scheduling",)):
        calls = []
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(mods))
        monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config({}))
        monkeypatch.setattr(dashboard.db, "upsert_scheduling_config",
                            lambda tid, cfg, **k: calls.append(cfg))
        monkeypatch.setattr(dashboard.db, "apply_booking_settings",
                            lambda tid, cfg, revision, **k: calls.append(cfg))
        app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
        app.dependency_overrides[require_csrf] = lambda: None
        return TestClient(app), calls
    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
def saved(make_saved):
    return make_saved()


def _depth(calls):
    return next(c for c in calls if "resource_types" in c)


def test_save_persists_all_depth_sections(saved):
    client, calls = saved
    # httpx encodes list-valued dict entries as repeated form keys (the array editors).
    form = {
        "windows": "slots", "open_time": "09:00", "close_time": "17:00",
        "slot_minutes": "30", "capacity_per_window": "3", "buffer_minutes": "15",
        "working_days": ["mon", "sat"],
        # one service bound to a resource with its own buffer
        "service_name": ["Cut"], "service_minutes": ["30"], "service_price": ["25"],
        "service_resource": ["Chair"], "service_buffer": ["10"],
        # two resource types
        "resource_name": ["Chair", "Room"], "resource_count": ["3", "1"],
        "resource_rate": ["", "20"], "resource_rate_unit": ["booking", "hour"],
        "resource_skills": ["colour, cut", ""],
        # stay pricing + check-in/out
        "stay_rate": "30", "stay_rate_unit": "night", "stay_deposit": "50",
        "stay_min_nights": "2", "checkin_after": "14:00", "checkout_before": "11:00",
        # per-day hours: Saturday short
        "dayhours_sat_open": "09:00", "dayhours_sat_close": "13:00",
        # party size, recurring, delivery
        "party_enabled": "1", "party_max_people": "40",
        "recurring_enabled": "1", "recurring_max": "8", "recurring_freq": ["weekly"],
        "delivery_offered": "1", "delivery_fee": "15", "delivery_radius": "20",
    }
    r = client.post("/app/settings/scheduling", data=form, follow_redirects=False)
    assert r.status_code == 303
    d = _depth(calls)
    # services keep their per-service resource + buffer
    assert d["service_types"][0] == {"name": "Cut", "minutes": 30, "price": 25.0,
                                     "resource": "Chair", "buffer": 10}
    # both resource types, cleaned (skills split, £0 → None)
    assert [r["name"] for r in d["resource_types"]] == ["Chair", "Room"]
    assert d["resource_types"][0]["skills"] == ["colour", "cut"] and d["resource_types"][0]["rate"] is None
    assert d["resource_types"][1] == {"name": "Room", "count": 1, "rate": 20.0,
                                      "rate_unit": "hour", "skills": []}
    assert d["stay_pricing"] == {"rate": 30.0, "rate_unit": "night", "deposit": 50.0, "min_nights": 2}
    assert d["day_hours"]["sat"] == {"open": "09:00", "close": "13:00"}
    assert d["day_hours"]["checkin_after"] == "14:00" and d["day_hours"]["checkout_before"] == "11:00"
    assert d["party_size"] == {"enabled": True, "max_people": 40}
    assert d["recurring"]["enabled"] and d["recurring"]["frequencies"] == ["weekly"]
    assert d["delivery"] == {"offered": True, "fee": 15.0, "radius_miles": 20}


def test_unchecked_toggles_save_off(saved):
    client, calls = saved
    r = client.post("/app/settings/scheduling",
                    data={"windows": "slots", "working_days": ["mon"]},
                    follow_redirects=False)
    assert r.status_code == 303
    d = _depth(calls)
    assert d["party_size"]["enabled"] is False
    assert d["recurring"]["enabled"] is False
    assert d["delivery"]["offered"] is False
    assert d["resource_types"] == [] and d["service_types"] == []


# ── Travel-assistant toggle (the checkbox only renders when the add-on is on) ─

_BASE = {"windows": "slots", "working_days": ["mon"]}


def test_travel_toggle_persists_when_addon_on(make_saved):
    client, calls = make_saved(mods=("scheduling", "travel"))
    r = client.post("/app/settings/scheduling", data={**_BASE, "travel_mode": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert len(calls) == 1 and calls[0]["travel_mode"] is True


def test_travel_toggle_unchecked_saves_off(make_saved):
    # The rendered-but-unchecked checkbox posts nothing; with the add-on on that
    # must save False (the tenant switched it off), not be dropped.
    client, calls = make_saved(mods=("scheduling", "travel"))
    r = client.post("/app/settings/scheduling", data=_BASE, follow_redirects=False)
    assert r.status_code == 303
    assert len(calls) == 1 and calls[0]["travel_mode"] is False


def test_travel_mode_untouched_when_addon_off(saved):
    # Add-on off → the toggle never rendered → the stored value must be preserved,
    # even against a forged/stray travel_mode field in the POST.
    client, calls = saved
    r = client.post("/app/settings/scheduling", data={**_BASE, "travel_mode": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert not any("travel_mode" in c for c in calls)


# ── Practice config (no template renders these inputs yet — absence ≠ cleared) ─

def test_practice_config_preserved_when_form_posts_none(saved):
    # A plain diary save posts no practitioner/reminder inputs; the depth write must
    # omit both keys so a practice tenant's stored config survives every save.
    client, calls = saved
    r = client.post("/app/settings/scheduling", data=_BASE, follow_redirects=False)
    assert r.status_code == 303
    d = _depth(calls)
    assert "practitioners" not in d and "reminder_cycle" not in d


def test_practice_config_persists_when_posted(saved):
    client, calls = saved
    form = {**_BASE,
            "practitioner_name": ["Dr Amara", ""],
            "practitioner_resource": ["Chair 1", ""],
            "practitioner_capacity": ["8", ""],
            "reminder_lead_hours": ["48", "24", ""],
            "reminder_ask_confirm": "1"}
    r = client.post("/app/settings/scheduling", data=form, follow_redirects=False)
    assert r.status_code == 303
    d = _depth(calls)
    assert d["practitioners"] == [
        {"name": "Dr Amara", "resource": "Chair 1", "capacity_per_day": 8}]
    assert d["reminder_cycle"] == {"leads_hours": [48, 24], "ask_confirm": True}


@pytest.mark.parametrize("value", ["20:00", ""])
def test_last_call_is_applied_or_explicitly_cleared(saved, value):
    client, calls = saved
    response = client.post("/app/settings/scheduling", data={
        "offer_last_call_present": "1", "offer_last_call": value}, follow_redirects=False)
    assert response.status_code == 303
    setting = next(c for c in calls if "offer_last_call" in c)
    assert len(calls) == 1
    assert {k: setting[k] for k in ("offer_last_call", "offer_last_call_review")} == {
        "offer_last_call": value or None, "offer_last_call_review": None}


def test_saving_carried_last_call_clears_review_marker(saved):
    client, calls = saved
    form = {"offer_last_call_present": "1", "offer_last_call": "20:00"}
    assert client.post("/app/settings/scheduling", data=form, follow_redirects=False).status_code == 303
    assert len(calls) == 1
    assert {k: calls[0][k] for k in ("offer_last_call", "offer_last_call_review")} == {
        "offer_last_call": "20:00", "offer_last_call_review": None}


def test_invalid_last_call_writes_nothing(saved):
    client, calls = saved
    assert client.post("/app/settings/scheduling", data={"offer_last_call_present": "1", "offer_last_call": "25:00"}, follow_redirects=False).status_code == 422
    assert calls == []
