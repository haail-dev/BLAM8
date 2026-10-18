"""Operating Systems — the seven business "shapes".

Pins the industry→OS derivation, the set_tenant_industry write-path that
auto-fills the OS (and leaves it out for the free-text long tail), and the
two-step Shape→Industry picker (grid, then filtered industries).

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

from app import db, operating_systems
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf


# ── derivation + helpers ──────────────────────────────────────────────────────
def test_os_for_industry_maps_the_eight_and_nothing_else():
    assert operating_systems.os_for_industry("logistics") == "field"
    assert operating_systems.os_for_industry("trades") == "field"
    assert operating_systems.os_for_industry("construction") == "field"
    assert operating_systems.os_for_industry("recruitment") == "match"
    assert operating_systems.os_for_industry("wholesale") == "wholesale"
    assert operating_systems.os_for_industry("lettings") == "assets"
    assert operating_systems.os_for_industry("hire") == "assets"
    assert operating_systems.os_for_industry("events") == "events"


def test_os_for_industry_off_the_map_is_none():
    assert operating_systems.os_for_industry("other") is None
    assert operating_systems.os_for_industry("Boarding kennels") is None   # free-text long tail
    assert operating_systems.os_for_industry("") is None
    assert operating_systems.os_for_industry(None) is None


def test_get_os_and_is_live():
    field = operating_systems.get_os("field")
    assert field and field["label"] == "Field OS"
    assert operating_systems.get_os("nope") is None
    assert operating_systems.get_os(None) is None
    assert operating_systems.is_live("field") is True
    assert operating_systems.is_live("storefront") is True     # built (docs/os/storefront.md Phase 1)
    assert operating_systems.is_live("nope") is False
    assert operating_systems.is_live(None) is False


def test_field_storefront_and_practice_are_live():
    # Field shipped first (TMS spine); Storefront is the shop OS; Practice the recall wedge;
    # Wholesale is the recurring-B2B-order OS (docs/os/wholesale.md); Assets OS is the hire
    # OS (lettings/plant/tool hire); Events OS is the one-off-occasion OS (docs/os/events.md).
    live = [os["id"] for os in operating_systems.OPERATING_SYSTEMS if os["live"]]
    assert live == ["field", "storefront", "practice", "wholesale", "assets", "events"]
    assert len(operating_systems.OPERATING_SYSTEMS) == 7


# ── the write-path derives (or omits) the OS ──────────────────────────────────
class _RecordingTable:
    """Captures the dict passed to .table('tenants').update(...)."""
    def __init__(self, sink):
        self.sink = sink

    def update(self, payload):
        self.sink["payload"] = payload
        return self

    def eq(self, col, val):
        self.sink["eq"] = (col, val)
        return self

    def execute(self):
        return type("Res", (), {"data": []})()


class _RecordingClient:
    def __init__(self, sink):
        self.sink = sink

    def table(self, name):
        self.sink["table"] = name
        return _RecordingTable(self.sink)


def test_set_tenant_industry_derives_os_for_a_mapped_industry():
    sink = {}
    db.set_tenant_industry("t1", "logistics", client=_RecordingClient(sink))
    assert sink["table"] == "tenants"
    assert sink["eq"] == ("id", "t1")
    assert sink["payload"] == {"industry": "logistics", "operating_system": "field"}


def test_set_tenant_industry_omits_os_for_the_long_tail():
    sink = {}
    db.set_tenant_industry("t1", "Boarding kennels", client=_RecordingClient(sink))
    # No OS key at all — never clobber a known shape to null for free-text sectors.
    assert sink["payload"] == {"industry": "Boarding kennels"}
    assert "operating_system" not in sink["payload"]


def test_set_tenant_industry_omits_os_for_other():
    sink = {}
    db.set_tenant_industry("t1", "other", client=_RecordingClient(sink))
    assert sink["payload"] == {"industry": "other"}


# ── the Shape → Industry picker ───────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"industry": None})
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_industry_get_renders_the_shape_grid(client):
    r = client.get("/onboarding/industry")
    assert r.status_code == 200
    # All seven OS labels present.
    for label in ("Field OS", "Storefront OS", "Practice OS", "Wholesale OS",
                  "Assets OS", "Events OS", "Match OS"):
        assert label in r.text
    assert "Coming soon" in r.text          # not-live shapes shown as coming-soon
    assert "What shape is your business?" in r.text


def test_shape_field_filters_to_field_industries(client):
    r = client.get("/onboarding/industry?shape=field")
    assert r.status_code == 200
    assert "Logistics &amp; haulage" in r.text     # Field industries present
    assert "Trades &amp; field service" in r.text
    # Construction is a visible core-only Field OS sector (no dispatch module yet).
    assert "Construction &amp; contractors" in r.text
    assert "Recruitment &amp; staffing" not in r.text   # Match — filtered out
    assert "Wholesale &amp; distribution" not in r.text
    assert "Choose a different shape" in r.text    # back-link to the grid


def test_construction_onboards_core_only_without_a_module(monkeypatch):
    # A visible Field sector with NO industry module must onboard/entitle cleanly:
    # only base modules light, no KeyError on the missing dispatch module.
    from app import db, plans
    assert plans.modules_for("professional", "construction") == {"scheduling", "crm"}
    states = {}
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "construction"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: None)
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm"}
    # no vertical dispatch module activated for a construction tenant
    assert states == {"scheduling": True, "crm": True, "concierge": False, "tms": False, "fsm": False,
                      "practice": False, "storefront": False, "assets": False,
                      "events": False, "wholesale": False}


def test_not_live_shape_falls_back_to_the_grid(client):
    # Match isn't built → no filtered industries, show the grid instead.
    r = client.get("/onboarding/industry?shape=match")
    assert r.status_code == 200
    assert "What shape is your business?" in r.text
    assert "Coming soon" in r.text
