"""The office/transport split — now via the multi-agent hand-off (Phase 1/2a).

Office and transport are TWO SEPARATE AGENTS: the base agent owns the office diary
(SCHEDULING_TOOLS) and hands transport off to the TMS specialist (ask_transport →
src/app/agents/tms.py, FLEET_ROUTING_TOOLS). The old single-prompt dual-diary tooling
(DUAL_TOOLS / the per-call `diary` enum / the executor's dual dispatch) was retired
in Phase 2a; what remains here:

  - single-purpose tool lists carry no `diary` arg; the executor ignores stray diary
    noise (each executor does ONE capacity model)
  - the pipeline gate (_scheduling_setup): scheduling → office tools, tms →
    ask_transport, both → both; the base diary summary is office-only
  - the base prompt: office <scheduling> + a <transport> hand-off section, never the
    transport domain rules
  - capacity ISOLATION survives: a sized (goods) row never consumes an office window;
    the board's own `dual` flag still hides office rows from the transport board
  - the surfaces: /app/board is the transport board (404 without TMS), /app/diary
    redirects there for TMS-only tenants and stays the windows calendar otherwise;
    plan routes gate on TMS

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import asyncio
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.pipeline as pipeline_mod
from app import scheduling
from app.agent import build_system_prompt
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.pipeline import _scheduling_setup
from app.scheduling import (DIARY_TZ, FLEET_ROUTING_TOOLS, FLEET_TOOLS,
                            SCHEDULING_TOOLS, make_executor)
from app.security import require_csrf

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

VAN = {"id": "v-aaa", "name": "7.5t box", "capacity_spaces": 6,
       "max_payload_kg": 2500, "active": True, "branch_id": None}


class DualDb:
    """Stubs every db call BOTH executor paths make."""

    def __init__(self, monkeypatch):
        self.fleet_holds, self.window_holds = [], []

        def hold_fleet_booking(tenant_id, date_iso, summary, spaces, **kw):
            self.fleet_holds.append((date_iso, spaces))
            return {"id": f"bf-{len(self.fleet_holds)}", "booking_ref": "BK-2001",
                    "booking_date": date_iso, "vehicle_id": "v-aaa",
                    "spaces": spaces, "status": "proposed", "window": "day"}

        def hold_booking_if_capacity(tenant_id, date_iso, window, summary, **kw):
            self.window_holds.append((date_iso, window))
            return {"id": f"bw-{len(self.window_holds)}", "booking_ref": "BK-2002",
                    "booking_date": date_iso, "window": window, "status": "proposed"}

        for mod in (scheduling,):
            monkeypatch.setattr(mod.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
            monkeypatch.setattr(mod.db, "list_branches", lambda *a, **k: [])
            monkeypatch.setattr(mod.db, "list_vehicles", lambda *a, **k: [dict(VAN)])
            monkeypatch.setattr(mod.db, "list_pallet_types", lambda *a, **k: [])
            monkeypatch.setattr(mod.db, "get_mailbox_default_branch", lambda *a, **k: None)
            monkeypatch.setattr(mod.db, "list_active_bookings", lambda *a, **k: [])
            monkeypatch.setattr(mod.db, "hold_fleet_booking", hold_fleet_booking)
            monkeypatch.setattr(mod.db, "hold_booking_if_capacity", hold_booking_if_capacity)


# ── tool definitions ──────────────────────────────────────────────────────────
def test_single_module_tools_carry_no_diary_arg():
    """Each executor is single-purpose now (office OR fleet — the dual `diary`
    dispatch was retired in Phase 2a), so no tool list carries a diary arg."""
    for tools in (SCHEDULING_TOOLS, FLEET_TOOLS, FLEET_ROUTING_TOOLS):
        for tool in tools:
            assert "diary" not in tool["input_schema"]["properties"]
            assert "diary" not in tool["input_schema"].get("required", [])


# ── executor (single-purpose: the dual `diary` dispatch is gone) ──────────────
def test_single_mode_executors_unchanged_by_diary_noise(monkeypatch):
    """A model passing a stray diary value changes nothing — the executor ignores
    unknown args and the fleet executor stays the fleet executor."""
    db = DualDb(monkeypatch)
    execute, _ = make_executor("t1", now=NOW, fleet=True)      # dual OFF
    out = execute("hold_booking", {"diary": "office", "date": "2026-06-18",
                                   "summary": "job",
                                   "pallets": [{"type": "full", "qty": 2}]})
    assert out["held"] is True and db.fleet_holds and not db.window_holds


# ── capacity isolation (D1: two capacity models share one bookings table) ────
def test_office_capacity_ignores_transport_rows():
    """A sized row is GOODS on a vehicle — it must never consume an office
    window, even though its window is 'day' (which blocks everything in the
    cross-mode overlap rule). Mirrored in the hold_booking_if_capacity RPC."""
    cfg = carried_config({"capacity_per_window": 1, "windows": "slots", "slot_minutes": 60})
    pallets = {"booking_date": "2026-06-18", "window": "day", "status": "confirmed",
               "spaces": 6.0, "vehicle_id": "v-1"}
    slots = scheduling.availability(cfg, [pallets], date(2026, 6, 18), date(2026, 6, 18), NOW)
    assert slots, "pallet rows must not block office windows"
    workshop = {"booking_date": "2026-06-18", "window": "day", "status": "confirmed",
                "spaces": None}
    assert scheduling.availability(cfg, [workshop], date(2026, 6, 18),
                                   date(2026, 6, 18), NOW) == []   # people still block


def test_office_rows_filter():
    rows = [{"id": "a", "spaces": None}, {"id": "b", "spaces": 3.0},
            {"id": "c", "spaces": None, "source": "calendar"}]
    assert [r["id"] for r in scheduling.office_rows(rows)] == ["a", "c"]


def test_dual_board_hides_office_rows_single_mode_keeps_legacy():
    """On a dual tenant the board shows goods only (office rows live on
    /app/diary); a fleet-ONLY tenant keeps the legacy bucket for windows-era
    leftovers — today's behaviour, frozen."""
    booked = [
        {"id": "b1", "booking_date": "2026-06-18", "window": "day",
         "status": "confirmed", "spaces": 3.0, "vehicle_id": "v-aaa"},
        {"id": "b2", "booking_date": "2026-06-18", "window": "day",
         "status": "confirmed", "spaces": None, "vehicle_id": None,
         "summary": "Planning workshop"},
    ]
    dual_days = scheduling.fleet_day_groups(carried_config(None), [dict(VAN)], booked, NOW, dual=True)
    day = next(d for d in dual_days if d["date"] == "2026-06-18")
    assert day["legacy"] == []
    single_days = scheduling.fleet_day_groups(carried_config(None), [dict(VAN)], booked, NOW)
    day = next(d for d in single_days if d["date"] == "2026-06-18")
    assert [b["id"] for b in day["legacy"]] == ["b2"]


# ── the pipeline gate ladder ──────────────────────────────────────────────────
def gate_for(monkeypatch, modules):
    monkeypatch.setattr(pipeline_mod.db, "active_modules", lambda *a, **k: set(modules))
    # Scheduling intake CONFIGURED (office diary unblocked), no extra required fields.
    cfg = {"intake_configured_at": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(pipeline_mod.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(pipeline_mod.db, "list_vehicles", lambda *a, **k: [dict(VAN)])
    monkeypatch.setattr(pipeline_mod.db, "list_branches", lambda *a, **k: [])
    return _scheduling_setup("t1", "c@x.co", None, None)


@pytest.mark.parametrize("modules,expected_tools,flags", [
    (set(), None, {"scheduling": False, "transport": False, "field_service": False, "storefront": False, "intake": "", "gather_intake": False}),
    ({"scheduling"}, "office", {"scheduling": True, "transport": False, "field_service": False, "storefront": False, "intake": "", "gather_intake": False, "other_dates": "hold"}),
    ({"tms"}, "handoff", {"scheduling": False, "transport": True, "field_service": False, "storefront": False, "intake": "", "gather_intake": False}),
    ({"scheduling", "tms"}, "both", {"scheduling": True, "transport": True, "field_service": False, "storefront": False, "intake": "", "gather_intake": False, "other_dates": "hold"}),
    ({"voice"}, None, {"scheduling": False, "transport": False, "field_service": False, "storefront": False, "intake": "", "gather_intake": False}),  # non-relevant
])
def test_gate_selects_exactly_the_handoff_tooling(monkeypatch, modules, expected_tools, flags):
    """Phase 1 (docs/modules/module-agent-handoff.md): the base agent gets the OFFICE tools
    when scheduling is on and the ONE ask_transport hand-off tool when tms is on —
    never fleet tools. A logistics tenant (scheduling+tms) gets both."""
    tools, executor, state, diary, got_flags = gate_for(monkeypatch, modules)
    policy = got_flags.pop("booking_policy", None)
    if expected_tools in ("office", "both"):
        assert policy["ready"]
    assert got_flags == flags
    if expected_tools is None:
        assert tools is None and executor is None
        return
    names = [t["name"] for t in tools]
    if expected_tools == "office":
        assert names == ["check_availability", "hold_booking", "offer_slots", "cancel_booking", "move_booking"]
    elif expected_tools == "handoff":
        assert names == ["ask_transport"]
    elif expected_tools == "both":
        assert names == ["check_availability", "hold_booking", "offer_slots", "cancel_booking", "move_booking",
                         "ask_transport"]
    assert executor is not None and state["bookings"] == []
    if expected_tools in ("office", "both"):
        # Live e2e finding #4 (2026-07-31): offer_slots was in the TOOL LIST but
        # the combined executor's dispatch whitelist didn't route it, so every
        # call came back unknown_tool and offers were never placed. The gate's
        # executor must route every office tool it advertises. (Empty slots →
        # the office executor's own validation error, never unknown_tool.)
        out = executor("offer_slots", {"slots": [], "summary": "x"})
        assert out.get("error") != "unknown_tool"
        # Run #8 (2026-09-05): cancel_booking shipped in the tool list without
        # the dispatch line and the first live cancel died the same way.
        monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [])
        out = executor("cancel_booking", {"booking_ref": "BK-1"})
        assert out.get("error") == "not_theirs"          # routed, and refused honestly
        out = executor("move_booking", {"booking_ref": "BK-1", "new_date": "2026-09-28", "new_window": "10:00"})
        assert out.get("error") == "not_theirs"          # the move rides the same whitelist


def test_gate_half_configured_scheduling_degrades_to_gather(monkeypatch):
    """Scheduling ACTIVE but intake setup never finished (module bought, setup screens
    skipped): the tenant degrades to gather-don't-book — the agent asks for the defined
    booking fields in drafts — never the <booking> no-capability punt ("a colleague
    will be in touch"). Gate on configured (office_on), not subscribed (sched_on)."""
    monkeypatch.setattr(pipeline_mod.db, "active_modules", lambda *a, **k: {"scheduling"})
    cfg = {"intake_configured_at": None,
           "intake_fields": [{"label": "Customer name", "required": True, "hint": ""}]}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(pipeline_mod.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    tools, executor, state, diary, flags = _scheduling_setup("t1", "c@x.co", None, None)
    assert tools is None and executor is None
    assert flags["scheduling"] is False and flags["gather_intake"] is True
    assert "Customer name" in flags["intake"]


def test_gate_dual_diary_summary_is_office_only(monkeypatch):
    """The base prompt's diary line carries only the OFFICE shape now; the transport
    diary shape lives inside the TMS specialist (spec D8)."""
    tools, _, _, diary, flags = gate_for(monkeypatch, {"scheduling", "tms"})
    assert {k: v for k, v in flags.items() if k != "booking_policy"} == {"scheduling": True, "transport": True, "field_service": False,
                     "storefront": False, "intake": "", "gather_intake": False,
                     # When it's full: an office diary always carries the business's choice.
                     "other_dates": "hold"}
    assert "bookings at the same time" in diary
    assert "TRANSPORT" not in diary and "pallet spaces" not in diary
    assert [t["name"] for t in tools][-1] == "ask_transport"


# ── the prompt (base agent — office + the transport hand-off section) ──────────
def test_prompt_transport_handoff_section_when_tms_active():
    # A logistics tenant: office diary + transport hand-off (spec D8).
    p = build_system_prompt({}, scheduling=True, today=date(2026, 6, 16),
                            diary_summary="Up to 4 bookings per day", transport=True)
    assert "<transport>" in p and "ask_transport" in p
    assert "status 'held'" in p and "status 'needs_info'" in p
    # The office diary is scoped to appointments, so goods hand off instead.
    assert "for delivery and collection requests use the ask_transport tool" in p
    # The base prompt carries NO transport DOMAIN rules — those live in the TMS agent.
    assert "pallet spaces" not in p
    assert "collection_postcode" not in p
    assert "check_availability" in p               # office tools still referenced
    # And no leftover dual-diary machinery.
    assert "TWO diaries" not in p and "diary='office'" not in p


def test_prompt_single_modes_have_no_dual_wording():
    windows_p = build_system_prompt({}, scheduling=True, today=date(2026, 6, 16),
                                    diary_summary="Up to 4 bookings per day")
    tms_only_p = build_system_prompt({}, today=date(2026, 6, 16), transport=True)
    for p in (windows_p, tms_only_p):
        assert "TWO diaries" not in p
        assert "diary='office'" not in p
    # Windows-only: the office rules read exactly as before, and no transport section.
    assert "booking facts come ONLY from the tools, never from memory" in windows_p
    assert "<transport>" not in windows_p
    assert "Diary shape: " in windows_p
    # TMS-only: the hand-off section, and no office diary section.
    assert "<transport>" in tms_only_p and "<scheduling>" not in tms_only_p


# ── the surfaces ──────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class SurfaceEnv:
    def __init__(self, monkeypatch, modules):
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "module_active",
                            lambda tid, m, **k: m in modules)
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "microsoft"})
        monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: True)
        monkeypatch.setattr(dashboard.db, "get_tenant_config",
                            lambda *a, **k: {"planner_auto_apply": False})
        monkeypatch.setattr(dashboard.db, "list_plans", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
        monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_vehicles", lambda *a, **k: [dict(VAN)])
        monkeypatch.setattr(dashboard.db, "list_pallet_types", lambda *a, **k: [])


def test_board_is_the_fleet_view(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    r = client.get("/app/board")
    assert r.status_code == 200
    assert "Add booking" in r.text and "/app/board/add" in r.text


def test_board_404_without_fleet(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"scheduling"})
    assert client.get("/app/board").status_code == 404


def test_diary_redirects_fleet_only_tenants_to_board(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    r = client.get("/app/diary?branch=br-1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/board?branch=br-1"


def test_diary_stays_windows_for_dual_tenants(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"scheduling", "tms"})
    monkeypatch.setattr(dashboard.setup_forms,'load',lambda ctx,page: {
        'page':page,'config':dashboard.db.get_scheduling_config('t1') or {},'definitions':[]})
    r = client.get("/app/diary")
    assert r.status_code == 200
    assert "Office diary" in r.text                # the dual heading
    r2 = client.get("/app/board")
    assert r2.status_code == 200 and "Transport board" in r2.text


def test_replan_route_gates_on_tms(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"scheduling"})        # TMS off — no transport surfaces
    r = client.post("/app/board/replan", data={"plan_date": "2026-06-15"})
    assert r.status_code == 404


def test_replan_route_runs_planner_and_redirects(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})

    async def fake_replan(tenant_id, day):
        return {"plan": {"id": "p1", "plan_date": day, "status": "proposed"},
                "applied": False, "note": "Frees the box.", "apply_error": None}

    monkeypatch.setattr(dashboard.planner, "replan_day", fake_replan)
    r = client.post("/app/board/replan", data={"plan_date": "2026-06-15"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "/app/board?" in loc and "msg=plan_proposed" in loc and "day=2026-06-15" in loc


def test_plan_apply_route_surfaces_the_loud_failure(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    monkeypatch.setattr(dashboard.db, "get_plan",
                        lambda pid, tid, **k: {"id": pid, "plan_date": "2026-06-15"})
    monkeypatch.setattr(dashboard.db, "apply_plan",
                        lambda pid, tid: (None, "vehicle box was deactivated since this plan was proposed"))
    r = client.post("/app/board/plans/p1/apply", follow_redirects=False)
    assert r.status_code == 303
    assert "msg=plan_failed" in r.headers["location"]
    assert "deactivated" in r.headers["location"]


def test_plan_discard_route(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    discarded = []
    monkeypatch.setattr(dashboard.db, "get_plan",
                        lambda pid, tid, **k: {"id": pid, "plan_date": "2026-06-15"})
    monkeypatch.setattr(dashboard.db, "discard_plan",
                        lambda pid, tid, **k: discarded.append(pid))
    r = client.post("/app/board/plans/p1/discard", follow_redirects=False)
    assert r.status_code == 303 and discarded == ["p1"]
    assert "msg=plan_discarded" in r.headers["location"]


def test_board_renders_proposed_plan_diff(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    # The board starts on the diary's date, which can be tomorrow on a UTC
    # CI runner during British Summer Time. Keep every fixture row on it.
    board_day = datetime.now(DIARY_TZ).date().isoformat()
    big = {"id": "v-big", "name": "18t", "capacity_spaces": 10,
           "max_payload_kg": None, "active": True, "branch_id": None,
           "created_at": "x"}
    rows = [
        {"id": "b1", "vehicle_id": "v-big", "spaces": 4.0, "weight_kg": None,
         "status": "confirmed", "booking_date": board_day,
         "booking_ref": "BK-1001", "summary": "big job", "window": "day",
         "created_at": "x", "source": "import", "details": {}},
        {"id": "b2", "vehicle_id": "v-aaa", "spaces": 3.0, "weight_kg": None,
         "status": "confirmed", "booking_date": board_day,
         "booking_ref": "BK-1002", "summary": "small job", "window": "day",
         "created_at": "x", "source": "import", "details": {},
         "delivery_postcode": "LS9 8AS"},
    ]
    monkeypatch.setattr(dashboard.db, "list_vehicles", lambda *a, **k: [big, dict(VAN)])
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: rows)
    monkeypatch.setattr(dashboard.db, "list_plans", lambda *a, **k: [
        {"id": "p1", "plan_date": board_day, "status": "proposed",
         "summary": "Frees the 7.5t box", "created_at": "x",
         "moves": [{"booking_id": "b2", "to_vehicle_id": "v-big"}]},
    ])
    r = client.get("/app/board")
    assert r.status_code == 200
    assert "PROPOSED PLAN" in r.text and "Frees the 7.5t box" in r.text
    assert "/app/board/plans/p1/apply" in r.text
    assert "Replan this day" in r.text


def test_diary_settings_shows_windows_fields_for_dual(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"scheduling", "tms"})
    r = client.get("/app/diary/settings")
    assert r.status_code == 200
    # Dual: the windows-capacity fields are LIVE (they shape the office diary).
    assert 'name="capacity_per_window"' in r.text
    SurfaceEnv(monkeypatch, {"tms"})
    r2 = client.get("/app/diary/settings")
    assert 'name="capacity_per_window"' not in r2.text   # fleet-only hides them


def test_settings_planner_block_gated_on_tms(client, monkeypatch):
    SurfaceEnv(monkeypatch, {"tms"})
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    r = client.get("/app/settings")
    assert r.status_code == 200 and 'name="planner_auto_apply"' in r.text
    SurfaceEnv(monkeypatch, {"scheduling"})        # TMS off → no planner block
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])
    r2 = client.get("/app/settings")
    assert 'name="planner_auto_apply"' not in r2.text
