"""The TMS cockpit (/app/tms) — the Fleet & Capacity chip's landing page.

What this pins:
  - tms.overview is pure and honest: the dispatch strip covers working days
    only, KPIs come from the same rows the board shows, transport rows only
    (the spaces-null isolation rule holds here too), exceptions bucket
    correctly, the feed is newest-first, planner data appears only with
    routing
  - the route gates on fleet (404 otherwise) and renders the live sections
  - the hub chip points at /app/tms

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling, tms
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.scheduling import DIARY_TZ
from app.security import require_csrf

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)   # Tuesday

BIG = {"id": "v-big", "name": "18t", "capacity_spaces": 10,
       "max_payload_kg": None, "active": True, "branch_id": None}
BOX = {"id": "v-box", "name": "box", "capacity_spaces": 6,
       "max_payload_kg": None, "active": True, "branch_id": None}
DEAD = {"id": "v-dead", "name": "old", "capacity_spaces": 99, "active": False,
        "max_payload_kg": None, "branch_id": None}


def bk(bid, vid, spaces, date_iso, status="confirmed", source="email",
       created="2026-06-16T09:00:00+00:00", **extra):
    return {"id": bid, "vehicle_id": vid, "spaces": spaces, "weight_kg": None,
            "booking_date": date_iso, "status": status, "source": source,
            "booking_ref": f"BK-{bid}", "summary": f"job {bid}",
            "created_at": created, "window": "day", **extra}


ROWS = [
    bk("b1", "v-big", 4.0, "2026-06-16", created="2026-06-15T08:00:00+00:00"),
    bk("b2", "v-box", 2.0, "2026-06-17", status="proposed",
       created="2026-06-16T09:30:00+00:00"),
    bk("b3", None, 3.0, "2026-06-18", source="import",
       created="2026-06-14T12:00:00+00:00"),                      # unassigned
    bk("b4", "v-big", 1.0, "2026-06-18", source="manual",
       created="2026-06-16T07:00:00+00:00", delivery_postcode="LS9 8AS"),
    # excluded: an office row (spaces None) and a cancelled row
    {"id": "b5", "vehicle_id": None, "spaces": None, "booking_date": "2026-06-16",
     "status": "confirmed", "source": "email", "summary": "workshop",
     "created_at": "x", "window": "day"},
    bk("b6", "v-big", 2.0, "2026-06-16", status="cancelled"),
]

PLANS = [{"id": "p1", "plan_date": "2026-06-17", "status": "proposed",
          "summary": "Frees the box", "moves": []}]


def make_overview(routing_on=True, rows=None, plans=None):
    return tms.overview(carried_config(), [BIG, BOX, DEAD], [], rows if rows is not None else list(ROWS),
                        plans if plans is not None else list(PLANS),
                        NOW, scheduling_on=True, routing_on=routing_on)


# ── the pure view-model ───────────────────────────────────────────────────────
def test_dispatch_strip_is_working_days_with_live_loads():
    o = make_overview()
    days = [d["date"] for d in o["dispatch"]]
    # Tue 16 → Mon 22 working days, weekend skipped, today first and flagged.
    assert days == ["2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22"]
    assert o["dispatch"][0]["today"] is True
    tue = o["dispatch"][0]
    assert tue["used"] == 4.0 and tue["total"] == 16.0   # active fleet only (no DEAD)
    assert tue["jobs"] == 1                              # office + cancelled excluded
    by_name = {v["name"]: v for v in tue["vehicles"]}
    assert by_name["18t"]["used"] == 4.0 and by_name["box"]["used"] == 0.0


def test_kpis_count_transport_rows_only():
    o = make_overview()
    k = o["kpis"]
    assert k["on_books_jobs"] == 4                       # b1,b2,b3,b4
    assert k["on_books_spaces"] == 10.0
    assert k["awaiting"] == 1                            # the proposed hold
    assert k["plans_pending"] == 1
    assert k["week_capacity"] == 16.0 * k["week_days"]
    assert dict(k["by_source"])["Email agent"] == 2      # b1 + b2
    assert dict(k["by_source"])["Order book"] == 1


def test_exceptions_bucket_correctly():
    o = make_overview()
    e = o["exceptions"]
    assert e["unassigned_n"] == 1 and e["unassigned"][0]["ref"] == "BK-b3"
    assert e["awaiting_n"] == 1 and e["awaiting"][0]["ref"] == "BK-b2"
    # b1, b2 have no postcode and ride vehicles; b3 unassigned doesn't count;
    # b4 HAS a postcode.
    assert e["no_postcode_n"] == 2
    assert {c["ref"] for c in e["no_postcode"]} == {"BK-b1", "BK-b2"}


def test_no_postcode_hidden_without_routing():
    o = make_overview(routing_on=False, plans=[])
    assert o["exceptions"]["no_postcode_n"] == 0
    assert o["review_days"] == [] and o["pending_plans"] == []


def test_feed_is_newest_first_and_flags_the_agent():
    o = make_overview()
    refs = [f["ref"] for f in o["feed"]]
    assert refs == ["BK-b2", "BK-b4", "BK-b1", "BK-b3"]   # created_at desc
    assert o["feed"][0]["agent"] is True and o["feed"][0]["status"] == "proposed"
    assert o["feed"][1]["source"] == "Added by you"


def test_pending_plans_carry_board_links():
    o = make_overview()
    assert o["pending_plans"] == [{"id": "p1", "date": "2026-06-17",
                                   "label": "Wednesday 17 June",
                                   "summary": "Frees the box"}]


def test_review_nudge_drops_days_with_a_pending_plan():
    """A day already carrying a proposed plan gets the plan card, not a second
    'replan this' nudge — one action per day."""
    rows = [bk("b1", "v-big", 4.0, "2026-06-17"), bk("b2", "v-box", 2.0, "2026-06-17")]
    with_plan = tms.overview(carried_config(), [BIG, BOX], [], rows, list(PLANS), NOW,
                             routing_on=True)
    assert with_plan["review_days"] == []           # consolidatable, but planned
    without = tms.overview(carried_config(), [BIG, BOX], [], rows, [], NOW, routing_on=True)
    assert [r["date"] for r in without["review_days"]] == ["2026-06-17"]


# ── the enterprise lens (multi-branch fleets) ────────────────────────────────
LEEDS_VAN = {"id": "v-leeds", "name": "Leeds 18t", "capacity_spaces": 10,
             "max_payload_kg": None, "active": True, "branch_id": "br-leeds"}
MANC_VAN = {"id": "v-manc", "name": "Manc box", "capacity_spaces": 6,
            "max_payload_kg": None, "active": True, "branch_id": "br-manc"}
BRANCHES = [{"id": "br-leeds", "name": "Leeds", "postcode": "LS9 8AS"},
            {"id": "br-manc", "name": "Manchester", "postcode": None}]
BRANCH_ROWS = [
    bk("L1", "v-leeds", 5.0, "2026-06-16"),
    bk("M1", "v-manc", 3.0, "2026-06-16", status="proposed"),
    bk("U1", None, 2.0, "2026-06-17"),     # vehicle deleted — branchless
]


def test_branch_lens_scopes_every_section():
    o = tms.overview(carried_config(), [LEEDS_VAN, MANC_VAN], BRANCHES, list(BRANCH_ROWS),
                     [], NOW, routing_on=True, branch_id="br-leeds")
    assert o["branch_name"] == "Leeds"
    assert o["kpis"]["on_books_jobs"] == 1            # Leeds van's booking only
    assert o["kpis"]["awaiting"] == 0                 # the proposed hold is Manchester's
    assert o["dispatch"][0]["total"] == 10.0          # Leeds capacity, not the fleet's
    assert [f["ref"] for f in o["feed"]] == ["BK-L1"]
    # Branchless (vehicle-deleted) rows belong to no depot — All view only.
    assert o["exceptions"]["unassigned_n"] == 0
    assert o["depots"] == []                          # tiles are the All view's


def test_depot_tiles_summarise_each_branch_on_the_all_view():
    o = tms.overview(carried_config(), [LEEDS_VAN, MANC_VAN], BRANCHES, list(BRANCH_ROWS),
                     [], NOW, routing_on=True)
    tiles = {d["name"]: d for d in o["depots"]}
    assert set(tiles) == {"Leeds", "Manchester"}
    assert tiles["Leeds"]["vehicles"] == 1 and tiles["Leeds"]["jobs"] == 1
    # 7 working days × 10 spaces = 70; 5 booked → 7%.
    assert tiles["Leeds"]["week_pct"] == 7
    assert tiles["Leeds"]["postcode"] == "LS9 8AS"
    assert o["exceptions"]["unassigned_n"] == 1       # the branchless row shows here
    # A single-branch tenant never sees tiles.
    single = tms.overview(carried_config(), [LEEDS_VAN], BRANCHES[:1], [], [], NOW)
    assert single["depots"] == []


def test_dispatch_bars_cap_for_big_fleets():
    vans = [{"id": f"v{i}", "name": f"van {i}", "capacity_spaces": 4,
             "max_payload_kg": None, "active": True, "branch_id": None}
            for i in range(9)]
    o = tms.overview(carried_config(), vans, [], [], [], NOW)
    assert len(o["dispatch"][0]["vehicles"]) == 6
    assert o["dispatch"][0]["more_vehicles"] == 3


# ── the route ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class TmsEnv:
    def __init__(self, monkeypatch, modules=("tms", "scheduling")):
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m in modules)
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(tms.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
        monkeypatch.setattr(tms.db, "list_vehicles", lambda *a, **k: [dict(BIG), dict(BOX)])
        monkeypatch.setattr(tms.db, "list_branches", lambda *a, **k: [])
        monkeypatch.setattr(tms.db, "list_bookings", lambda *a, **k: [dict(r) for r in ROWS])
        monkeypatch.setattr(tms.db, "list_plans", lambda *a, **k: list(PLANS))


def test_tms_page_renders_live_sections(client, monkeypatch):
    TmsEnv(monkeypatch)
    r = client.get("/app/tms")
    assert r.status_code == 200
    assert "Field OS · Transport" in r.text and "Dispatch" in r.text
    assert "Order intake" in r.text and "BK-b1" in r.text
    assert "Frees the box" in r.text                    # the pending plan card
    assert "/app/board?day=" in r.text                  # daysheet cards deep-link to the board


def test_tms_404_without_tms(client, monkeypatch):
    TmsEnv(monkeypatch, modules=("scheduling",))        # TMS add-on off
    assert client.get("/app/tms").status_code == 404


def test_hub_chip_points_at_tms():
    tms_card = next(m for m in dashboard.MODULES if m["id"] == "tms")
    assert tms_card["href"] == "/app/tms" and tms_card["group"] == "addon"


def test_tms_branch_tabs_and_lens_on_the_route(client, monkeypatch):
    TmsEnv(monkeypatch)
    monkeypatch.setattr(tms.db, "list_vehicles",
                        lambda *a, **k: [dict(LEEDS_VAN), dict(MANC_VAN)])
    monkeypatch.setattr(tms.db, "list_branches", lambda *a, **k: list(BRANCHES))
    monkeypatch.setattr(tms.db, "list_bookings",
                        lambda *a, **k: [dict(r) for r in BRANCH_ROWS])
    monkeypatch.setattr(tms.db, "list_plans", lambda *a, **k: [])
    r = client.get("/app/tms")
    assert r.status_code == 200
    assert "/app/tms?branch=br-leeds" in r.text        # tabs + depot tiles
    assert "booked this week" in r.text
    r2 = client.get("/app/tms?branch=br-leeds")
    assert "Leeds depot" in r2.text                    # the lens label
    assert "booked this week" not in r2.text           # tiles are All-view only
    # A foreign/stale branch id quietly falls back to the All view.
    r3 = client.get("/app/tms?branch=not-ours")
    assert r3.status_code == 200 and "booked this week" in r3.text
