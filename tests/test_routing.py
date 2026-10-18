"""Routing module (route-planning slice 1) — tool variant, capture, manifest.

D2 is the contract under test: the SAME two tool names again, with hold_booking
gaining collection_postcode / delivery_postcode / reference ONLY when the
routing row is active. Fleet-without-routing tenants must keep FLEET_TOOLS
byte-identical (the deepcopy must never leak back), and an executor without
routing=True must never stamp routing fields — even if a model passes them
(hold_booking's schema is non-strict). The manifest is the first surface built
on the captured data; its route 404s unless the TMS module is active.

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.scheduling import (DIARY_TZ, FLEET_ROUTING_TOOLS, FLEET_TOOLS,
                            SCHEDULING_TOOLS, make_executor, routing_fields,
                            vehicle_manifest)
from app.security import require_csrf

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

VAN = {"id": "v-aaa", "name": "7.5t box", "registration": "YX24 ABC",
       "capacity_spaces": 6, "max_payload_kg": 2500, "active": True,
       "branch_id": "br-leeds"}
BRANCHES = [{"id": "br-leeds", "name": "Leeds", "postcode": "LS9 8AS"}]

ROUTING_ARGS = {"date": "2026-06-18", "summary": "6 pallets to Leeds",
                "pallets": [{"type": "full", "qty": 6}],
                "collection_postcode": "wf17 9lu",
                "delivery_postcode": "ls9 8as",
                "reference": "PO-1042"}


class RoutingDb:
    """Stubs every db call the routing executor path makes."""

    def __init__(self, monkeypatch, hold_row=True):
        self.routing_calls = []

        def hold_fleet_booking(tenant_id, date_iso, summary, spaces, **kw):
            if not hold_row:
                return None
            return {"id": "b-9", "booking_ref": "BK-1001", "booking_date": date_iso,
                    "vehicle_id": "v-aaa", "spaces": spaces, "status": "proposed",
                    "window": "day"}

        def set_booking_routing(booking_id, tenant_id, **kw):
            self.routing_calls.append({"booking_id": booking_id, **kw})

        monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
        monkeypatch.setattr(scheduling.db, "list_branches", lambda *a, **k: list(BRANCHES))
        monkeypatch.setattr(scheduling.db, "list_vehicles", lambda *a, **k: [VAN])
        monkeypatch.setattr(scheduling.db, "list_pallet_types", lambda *a, **k: [])
        monkeypatch.setattr(scheduling.db, "get_mailbox_default_branch", lambda *a, **k: None)
        monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
        monkeypatch.setattr(scheduling.db, "hold_fleet_booking", hold_fleet_booking)
        monkeypatch.setattr(scheduling.db, "set_booking_routing", set_booking_routing)


# ── tool definitions ──────────────────────────────────────────────────────────
def test_routing_tools_shape():
    assert [t["name"] for t in FLEET_ROUTING_TOOLS] == [t["name"] for t in FLEET_TOOLS]
    check, hold = FLEET_ROUTING_TOOLS
    # check_availability is untouched — postcodes don't affect availability.
    assert check == FLEET_TOOLS[0]
    props = hold["input_schema"]["properties"]
    for key in ("collection_postcode", "delivery_postcode", "reference"):
        assert key in props
    # The new args are OPTIONAL — required is unchanged.
    assert set(hold["input_schema"]["required"]) == {"date", "pallets", "summary"}
    # D5 still holds: nothing the model sees names a vehicle.
    for tool in FLEET_ROUTING_TOOLS:
        assert "vehicle" not in str(tool).lower()


def test_fleet_tools_are_frozen():
    """Fleet-without-routing tenants must see byte-identical tools — the
    routing variant is a deepcopy and must never leak back into FLEET_TOOLS."""
    for tool in FLEET_TOOLS:
        props = tool["input_schema"]["properties"]
        assert "collection_postcode" not in props
        assert "delivery_postcode" not in props
        assert "reference" not in props
    for tool in SCHEDULING_TOOLS:
        assert "collection_postcode" not in tool["input_schema"]["properties"]


# ── routing_fields (the capture rules) ───────────────────────────────────────
def test_routing_fields_normalises():
    out = routing_fields(ROUTING_ARGS)
    assert out == {"collection_postcode": "WF17 9LU",   # postcodes uppercased
                   "delivery_postcode": "LS9 8AS",
                   "reference": "PO-1042"}              # reference casing kept


def test_routing_fields_drops_blanks_and_caps_length():
    assert routing_fields({"collection_postcode": "  ", "reference": ""}) == {}
    assert routing_fields({}) == {}
    long = routing_fields({"delivery_postcode": "x" * 40, "reference": "r" * 200})
    assert len(long["delivery_postcode"]) == 16 and len(long["reference"]) == 80


# ── executor capture ─────────────────────────────────────────────────────────
def test_routing_hold_stamps_the_booking(monkeypatch):
    db = RoutingDb(monkeypatch)
    execute, state = make_executor("t1", now=NOW, fleet=True, routing=True)
    out = execute("hold_booking", dict(ROUTING_ARGS))
    assert out["held"] is True
    assert out["booking_ref"] == "BK-1001"   # the business-issued ref, for the reply
    assert db.routing_calls == [{"booking_id": "b-9",
                                 "collection_postcode": "WF17 9LU",
                                 "delivery_postcode": "LS9 8AS",
                                 "reference": "PO-1042"}]
    # The captured fields ride on the state row for anything downstream.
    assert state["bookings"][0]["delivery_postcode"] == "LS9 8AS"
    # D5: the tool result still never names the vehicle.
    assert "vehicle" not in str(out).lower()


def test_routing_hold_without_fields_skips_the_update(monkeypatch):
    db = RoutingDb(monkeypatch)
    execute, _ = make_executor("t1", now=NOW, fleet=True, routing=True)
    out = execute("hold_booking", {"date": "2026-06-18", "summary": "job",
                                   "pallets": [{"type": "full", "qty": 2}]})
    assert out["held"] is True
    assert db.routing_calls == []


def test_fleet_only_executor_never_stamps_routing(monkeypatch):
    """hold_booking's schema is non-strict, so a model COULD pass the routing
    args to a fleet-only tenant — the executor must ignore them."""
    db = RoutingDb(monkeypatch)
    execute, state = make_executor("t1", now=NOW, fleet=True)   # routing OFF
    out = execute("hold_booking", dict(ROUTING_ARGS))
    assert out["held"] is True
    assert db.routing_calls == []
    assert "delivery_postcode" not in state["bookings"][0]


def test_failed_hold_never_stamps(monkeypatch):
    db = RoutingDb(monkeypatch, hold_row=False)
    execute, _ = make_executor("t1", now=NOW, fleet=True, routing=True)
    out = execute("hold_booking", dict(ROUTING_ARGS))
    assert out["error"] == "full"
    assert db.routing_calls == []


# ── vehicle_manifest (the pure builder) ──────────────────────────────────────
DAY = date(2026, 6, 18)
ROWS = [
    {"id": "b-1", "vehicle_id": "v-aaa", "booking_date": "2026-06-18",
     "status": "confirmed", "summary": "6 pallets to Leeds", "contact": "ops@x.co",
     "spaces": 6, "weight_kg": 1800, "reference": "PO-1042",
     "collection_postcode": "WF17 9LU", "delivery_postcode": "LS9 8AS",
     "created_at": "2026-06-16T09:00:00+00:00", "source": "email"},
    {"id": "b-2", "vehicle_id": "v-aaa", "booking_date": "2026-06-18",
     "status": "proposed", "summary": "Half pallet", "contact": None,
     "spaces": 0.5, "weight_kg": None, "reference": None,
     "collection_postcode": None, "delivery_postcode": None,
     "created_at": "2026-06-16T08:00:00+00:00", "source": "email"},
    # Excluded: other vehicle / cancelled / other day.
    {"id": "b-3", "vehicle_id": "v-zzz", "booking_date": "2026-06-18",
     "status": "confirmed", "spaces": 1, "created_at": "x"},
    {"id": "b-4", "vehicle_id": "v-aaa", "booking_date": "2026-06-18",
     "status": "cancelled", "spaces": 1, "created_at": "x"},
    {"id": "b-5", "vehicle_id": "v-aaa", "booking_date": "2026-06-19",
     "status": "confirmed", "spaces": 1, "created_at": "x"},
]


def test_manifest_filters_sorts_and_totals():
    m = vehicle_manifest(VAN, ROWS, DAY)
    assert [r["id"] for r in m["rows"]] == ["b-2", "b-1"]   # oldest created first
    assert m["used_spaces"] == 6.5 and m["total_spaces"] == 6.0
    assert m["used_kg"] == 1800.0 and m["max_kg"] == 2500.0
    assert m["drops"] == 2 and m["located"] == 1 and m["unlocated"] == 1
    assert m["date"] == "2026-06-18"


def test_manifest_empty_vehicle_day():
    m = vehicle_manifest(VAN, [], DAY)
    assert m["rows"] == [] and m["used_spaces"] == 0 and m["unlocated"] == 0


# ── the manifest route (gating) ──────────────────────────────────────────────
class RouteEnv:
    def __init__(self, monkeypatch, modules=("tms",)):
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "get_vehicle",
                            lambda vid, tid, **k: dict(VAN) if vid == "v-aaa" else None)
        monkeypatch.setattr(dashboard.db, "list_vehicle_day_bookings",
                            lambda *a, **k: [dict(ROWS[0])])
        monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: list(BRANCHES))


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_manifest_page_renders(client, monkeypatch):
    RouteEnv(monkeypatch)
    r = client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18")
    assert r.status_code == 200
    assert "YX24 ABC" in r.text and "PO-1042" in r.text and "LS9 8AS" in r.text


def test_manifest_404_without_tms(client, monkeypatch):
    RouteEnv(monkeypatch, modules=("scheduling",))   # TMS add-on off
    assert client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18").status_code == 404


def test_manifest_404_on_unknown_vehicle_or_bad_date(client, monkeypatch):
    RouteEnv(monkeypatch)
    assert client.get("/app/fleet/vehicles/v-nope/day/2026-06-18").status_code == 404
    assert client.get("/app/fleet/vehicles/v-aaa/day/not-a-date").status_code == 404


# ── normal-use honesty (booking refs + availability reasons, 2026-06-11) ─────
def test_check_explains_notice_instead_of_claiming_full(monkeypatch):
    """An empty range caused by the notice rule must say so — the agent's reply
    saying 'fully booked' when the diary was merely inside 24h notice was a
    live-found anomaly."""
    RoutingDb(monkeypatch)
    monkeypatch.setattr(scheduling.db, "get_scheduling_config",
                        lambda *a, **k: carried_config({"min_notice_hours": 48}))
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability",
                  {"date_from": "2026-06-16", "date_to": "2026-06-17"})
    assert out["available"] == []
    assert out["earliest_bookable_date"] == "2026-06-19"   # Thu blocked by 48h, Fri ok
    assert "48 hours" in out["message"] and "too short notice" in out["message"]


def test_transport_desk_prompt_carries_the_normal_use_rules():
    # The transport tool-discipline moved to the TMS specialist (Phase 1) but the
    # same normal-use rules must survive — that's the behaviour-parity bar.
    from app import agent
    from app.agents import tms as tms_agent
    desk = tms_agent.build_system_prompt(diary_shape="Fleet diary: 1 vehicle")
    assert "booking_ref" in desk
    assert "fully booked" in desk                  # 'too short notice' ≠ 'full'
    assert "NEVER split" in desk                    # no split orders
    assert "check_availability" in desk
    assert "collection_postcode" in desk            # routing capture lives with the desk
    # The base office (windows) prompt keeps its own honesty wording.
    windows_prompt = agent.build_system_prompt({}, scheduling=True)
    assert "booking_ref" in windows_prompt
    assert "NEVER promise" in windows_prompt


# ── the route map page (D3 slice one: display) ───────────────────────────────
def test_route_page_explains_a_missing_key(client, monkeypatch):
    RouteEnv(monkeypatch)
    monkeypatch.setattr(dashboard.settings, "google_places_api_key", "")
    r = client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/route")
    assert r.status_code == 200
    assert "GOOGLE_PLACES_API_KEY" in r.text          # says why, never a broken map
    assert "maps.googleapis.com" not in r.text


def test_route_page_renders_map_with_key(client, monkeypatch):
    RouteEnv(monkeypatch)
    monkeypatch.setattr(dashboard.settings, "google_places_api_key", "test-key")
    monkeypatch.setattr(dashboard.db, "list_branches",
                        lambda *a, **k: [{"id": "br-leeds", "name": "Leeds",
                                          "postcode": "LS9 8AS"}])
    r = client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/route")
    assert r.status_code == 200
    assert "initRoute" in r.text and "maps.googleapis.com" in r.text
    assert "WF17 9LU" in r.text and "LS9 8AS" in r.text   # stops + depot origin
    assert "Depot" in r.text and "Return" in r.text       # round trip from the branch


def test_route_page_gated_like_everything_else(client, monkeypatch):
    RouteEnv(monkeypatch, modules=("scheduling",))   # TMS add-on off
    assert client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/route").status_code == 404


# ── branch postcode capture (the route origin) ───────────────────────────────
def test_branch_add_and_update_normalise_postcode(client, monkeypatch):
    RouteEnv(monkeypatch, modules=("tms",))
    calls = {}
    monkeypatch.setattr(dashboard.db, "add_branch",
                        lambda tid, name, postcode=None, **k: calls.update(add=(name, postcode)))
    monkeypatch.setattr(dashboard.db, "update_branch",
                        lambda bid, tid, fields, **k: calls.update(upd=(bid, fields)))
    r = client.post("/app/fleet/branches/add",
                    data={"name": "Leeds", "postcode": "ls9 8as"}, follow_redirects=False)
    assert r.status_code == 303 and calls["add"] == ("Leeds", "LS9 8AS")
    r = client.post("/app/fleet/branches/br-1/update",
                    data={"name": "Leeds", "postcode": ""}, follow_redirects=False)
    assert "branchsaved" in r.headers["location"]
    assert calls["upd"] == ("br-1", {"name": "Leeds", "postcode": None})
