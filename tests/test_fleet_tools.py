"""Fleet module — tool definitions + the fleet executor branch (build step 2).

D5 is the contract under test: the SAME two channel-agnostic tool names gain
load arguments (pallets / weight_kg / branch) ONLY when the fleet row is
active; windows-mode tenants see byte-identical tools and an executor that
never touches a fleet table; and nothing the model sees — schema or result —
ever names a vehicle. Branch resolution (D4) runs inside the executor:
explicit ask → mailbox default → only-branch/whole-fleet → ask the customer.

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

import pytest

from app import agent, scheduling
from app.scheduling import DIARY_TZ, FLEET_TOOLS, SCHEDULING_TOOLS, make_executor

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

SMALL = {"id": "v-aaa", "name": "Sprinter", "capacity_spaces": 4,
         "max_payload_kg": 1000, "active": True, "branch_id": "br-leeds"}
BIG = {"id": "v-bbb", "name": "18t", "capacity_spaces": 8,
       "max_payload_kg": 9000, "active": True, "branch_id": "br-manc"}
BRANCHES = [{"id": "br-leeds", "name": "Leeds"}, {"id": "br-manc", "name": "Manchester"}]


class FleetDb:
    """Stubs every db call the fleet executor makes, recording the interesting ones."""

    def __init__(self, monkeypatch, vehicles=None, branches=None, booked=None,
                 default_branch=None, hold_row=False):
        self.hold_kwargs = None
        self.vehicles_kwargs = None
        vehicles = [SMALL, BIG] if vehicles is None else vehicles
        branches = list(BRANCHES) if branches is None else branches

        def list_vehicles(tenant_id, client=None, active_only=False):
            self.vehicles_kwargs = {"active_only": active_only}
            return [v for v in vehicles if v.get("active", True)] if active_only else vehicles

        def hold_fleet_booking(tenant_id, date_iso, summary, spaces, **kw):
            self.hold_kwargs = {"date": date_iso, "summary": summary, "spaces": spaces, **kw}
            if not hold_row:
                return None
            return {"id": "b-9", "booking_date": date_iso, "vehicle_id": "v-bbb",
                    "spaces": spaces, "status": "proposed", "window": "day"}

        monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
        monkeypatch.setattr(scheduling.db, "list_branches", lambda *a, **k: branches)
        monkeypatch.setattr(scheduling.db, "list_vehicles", list_vehicles)
        monkeypatch.setattr(scheduling.db, "list_pallet_types", lambda *a, **k: [])
        monkeypatch.setattr(scheduling.db, "get_mailbox_default_branch",
                            lambda mid, **k: default_branch)
        monkeypatch.setattr(scheduling.db, "list_active_bookings",
                            lambda *a, **k: booked or [])
        monkeypatch.setattr(scheduling.db, "hold_fleet_booking", hold_fleet_booking)


# ── tool definitions ──────────────────────────────────────────────────────────
def test_fleet_tool_definitions_shape():
    # Same core names as windows mode — channel-agnostic, and the voice module reuses
    # them. The office list additionally carries offer_slots (smart-booking offers);
    # fleet deliberately doesn't take offered-slot reservations (whole-day loads).
    assert [t["name"] for t in FLEET_TOOLS] == \
        [t["name"] for t in SCHEDULING_TOOLS if t["name"] != "offer_slots"]
    check, hold = FLEET_TOOLS
    assert check["strict"] is True
    assert set(hold["input_schema"]["required"]) == {"date", "pallets", "summary"}
    for tool in FLEET_TOOLS:
        props = tool["input_schema"]["properties"]
        assert "pallets" in props and "weight_kg" in props and "branch" in props
        assert "window" not in props          # fleet is whole-day; no window concept
        # D5: nothing the model sees names a vehicle.
        assert "vehicle" not in str(tool).lower()
    assert "one hold per email" in hold["description"]
    assert "BEFORE" in check["description"]


def test_windows_tools_are_frozen():
    """Windows-mode tenants must see byte-identical tools after the fleet ships."""
    for tool in SCHEDULING_TOOLS:
        assert "pallets" not in tool["input_schema"]["properties"]
        assert "branch" not in tool["input_schema"]["properties"]
    assert "window" in SCHEDULING_TOOLS[1]["input_schema"]["properties"]
    assert set(SCHEDULING_TOOLS[1]["input_schema"]["required"]) == {"date", "window", "summary"}


def test_windows_executor_never_touches_fleet_tables(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fleet db reached from a windows-mode executor")

    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "list_vehicles", boom)
    monkeypatch.setattr(scheduling.db, "list_branches", boom)
    monkeypatch.setattr(scheduling.db, "list_pallet_types", boom)
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability", {"date_from": "2026-06-16", "date_to": "2026-06-19"})
    assert out["available"][0] == {"date": "2026-06-18", "window": "day", "remaining": 4}


# ── check_availability in fleet mode ──────────────────────────────────────────
def test_fleet_check_without_load(monkeypatch):
    db = FleetDb(monkeypatch, branches=[BRANCHES[0]])
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {"date_from": "2026-06-16", "date_to": "2026-06-19"})
    assert out["available"] == [
        {"date": "2026-06-18", "remaining_spaces": 8.0},
        {"date": "2026-06-19", "remaining_spaces": 8.0},
    ]
    assert db.vehicles_kwargs == {"active_only": True}   # deactivated vans never enter


def test_fleet_check_with_load_filters_and_flags(monkeypatch):
    booked = [{"booking_date": "2026-06-18", "vehicle_id": "v-bbb", "spaces": 6,
               "weight_kg": None, "status": "proposed", "window": "day"}]
    FleetDb(monkeypatch, branches=[BRANCHES[0]], booked=booked)
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {
        "date_from": "2026-06-16", "date_to": "2026-06-19",
        "pallets": [{"type": "full", "qty": 5}, {"type": "half", "qty": 2}],
    })
    assert out["spaces_requested"] == 6.0
    assert out["available"] == [{"date": "2026-06-19", "remaining_spaces": 8.0, "fits": True}]


def test_fleet_check_unknown_pallet_type_assumed_full(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]])
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {
        "date_from": "2026-06-18", "date_to": "2026-06-18",
        "pallets": [{"type": "mega", "qty": 2}],
    })
    assert out["assumed_full_pallets"] == ["mega"]
    assert "assumption" in out["note"]


def test_fleet_check_load_too_big_for_any_vehicle(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]])
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {
        "date_from": "2026-06-16", "date_to": "2026-06-19",
        "pallets": [{"type": "full", "qty": 20}],
    })
    assert out["available"] == []
    assert "colleague" in out["message"] and "split" in out["message"]


def test_fleet_check_zero_vehicles_reports_not_configured(monkeypatch):
    FleetDb(monkeypatch, vehicles=[], branches=[BRANCHES[0]])
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {"date_from": "2026-06-16", "date_to": "2026-06-19"})
    assert out["error"] == "not_configured"
    assert "colleague" in out["message"]


def test_fleet_check_invalid_inputs(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]])
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    assert execute("check_availability", {"date_from": "soon", "date_to": "later"})["error"] == "invalid_date"
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-18",
                                         "pallets": [{"type": "full", "qty": -1}]})
    assert out["error"] == "invalid_pallets"
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-18",
                                         "weight_kg": "heavy"})
    assert out["error"] == "invalid_weight"


# ── branch resolution through the executor (D4) ───────────────────────────────
def test_fleet_branch_required_when_ambiguous(monkeypatch):
    FleetDb(monkeypatch)   # two branches, no mailbox default
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-19"})
    assert out["error"] == "branch_required"
    assert out["branches"] == ["Leeds", "Manchester"]
    assert "Ask the customer" in out["message"]


def test_fleet_unknown_branch_lists_the_real_ones(monkeypatch):
    FleetDb(monkeypatch)
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-19",
                                         "branch": "Hull"})
    assert out["error"] == "unknown_branch"
    assert "Leeds, Manchester" in out["message"]


def test_fleet_mailbox_default_branch_scopes_the_pool(monkeypatch):
    FleetDb(monkeypatch, default_branch="br-leeds")
    execute, _ = make_executor("t1", now=NOW, fleet=True, mailbox_id="mb-1")
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-18"})
    assert out["available"] == [{"date": "2026-06-18", "remaining_spaces": 4.0}]  # Leeds van only


def test_fleet_explicit_branch_beats_mailbox_default(monkeypatch):
    FleetDb(monkeypatch, default_branch="br-leeds")
    execute, _ = make_executor("t1", now=NOW, fleet=True, mailbox_id="mb-1")
    out = execute("check_availability", {"date_from": "2026-06-18", "date_to": "2026-06-18",
                                         "branch": "manchester"})
    assert out["available"] == [{"date": "2026-06-18", "remaining_spaces": 8.0}]


# ── hold_booking in fleet mode ────────────────────────────────────────────────
def test_fleet_hold_success_never_leaks_the_vehicle(monkeypatch):
    db = FleetDb(monkeypatch, branches=[BRANCHES[0]], hold_row=True)
    execute, state = make_executor("t1", contact="ops@acme.co.uk", thread_id="th-9",
                                   now=NOW, fleet=True)
    out = execute("hold_booking", {
        "date": "2026-06-18", "summary": "6 pallets, M17 -> LS9",
        "pallets": [{"type": "full", "qty": 5}, {"type": "half", "qty": 2}],
        "weight_kg": 1200, "details": {"from": "M17", "to": "LS9"},
    })
    assert out == {"held": True, "booking_id": "b-9", "booking_ref": None,
                   "date": "2026-06-18",
                   "spaces": 6.0, "status": "proposed"}        # no vehicle anywhere
    assert db.hold_kwargs["spaces"] == 6.0
    assert db.hold_kwargs["weight_kg"] == 1200.0
    assert db.hold_kwargs["branch_id"] is None                  # single branch → whole fleet
    assert db.hold_kwargs["contact"] == "ops@acme.co.uk"
    assert db.hold_kwargs["thread_id"] == "th-9"
    assert state["bookings"][0]["id"] == "b-9"                  # the pipeline links this


def test_fleet_hold_requires_pallets(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]], hold_row=True)
    execute, state = make_executor("t1", now=NOW, fleet=True)
    out = execute("hold_booking", {"date": "2026-06-18", "summary": "job"})
    assert out["error"] == "pallets_required"
    assert "ask" in out["message"].lower()
    assert state["bookings"] == []


def test_fleet_hold_full_when_no_vehicle_fits(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]], hold_row=False)
    execute, state = make_executor("t1", now=NOW, fleet=True)
    out = execute("hold_booking", {"date": "2026-06-18", "summary": "job",
                                   "pallets": [{"type": "full", "qty": 6}]})
    assert out["error"] == "full"
    assert "check_availability" in out["message"]
    assert state["bookings"] == []


def test_fleet_hold_date_rules_still_apply(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]], hold_row=True)
    execute, _ = make_executor("t1", now=NOW, fleet=True)
    pallets = [{"type": "full", "qty": 1}]
    assert execute("hold_booking", {"date": "2026-06-20", "summary": "x",
                                    "pallets": pallets})["error"] == "closed"          # Saturday
    assert execute("hold_booking", {"date": "2026-06-17", "summary": "x",
                                    "pallets": pallets})["error"] == "outside_horizon"  # < 24h notice
    assert execute("hold_booking", {"date": "junk", "summary": "x",
                                    "pallets": pallets})["error"] == "invalid_date"


def test_fleet_hold_one_per_email(monkeypatch):
    FleetDb(monkeypatch, branches=[BRANCHES[0]], hold_row=True)
    execute, state = make_executor("t1", now=NOW, fleet=True)
    pallets = [{"type": "full", "qty": 1}]
    assert execute("hold_booking", {"date": "2026-06-18", "summary": "a", "pallets": pallets})["held"]
    out = execute("hold_booking", {"date": "2026-06-19", "summary": "b", "pallets": pallets})
    assert out["error"] == "already_held"
    assert len(state["bookings"]) == 1


# ── the transport desk's pallet wording (now in src/app/agents/tms.py — Phase 1) ──
def test_transport_desk_prompt_uses_pallet_capacity_wording():
    from datetime import date
    from app.agents import tms as tms_agent
    desk = tms_agent.build_system_prompt(today=date(2026, 6, 16))
    assert "pallet" in desk and "NEVER" in desk
    assert "never from memory" in desk
    assert "vans" in desk                        # the don't-name-vans rule
    # The base office (windows) prompt keeps the window wording — never pallets.
    windows = agent.build_system_prompt({}, scheduling=True, today=date(2026, 6, 16))
    assert "'am', 'pm'" in windows
    assert "pallet" not in windows
