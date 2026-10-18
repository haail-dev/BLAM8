"""Transport planner module (docs/modules/module-transport-planner.md) — the engine.

What this pins, unit-first as always:
  - geometry: haversine on known UK distances; the NN tour is deterministic
  - the consolidation heuristic (D5): frees-a-van (the Mon-15 demo shape after
    a cancellation), the honest no-gain case (9.25 + 3 won't merge into 10),
    never-churn (a reshuffle that frees nothing is dropped), payload and
    branch respected, the haversine tie-break (a Hull-area drop joins the
    Hull run, not the Leeds one), unlocated drops consolidated by size,
    pinned (movable=False) rows never move
  - movable derivation (v1 guardrails)
  - auto_appliable classification (D6): ONLY same-day vehicle moves; any
    to_date/split/unknown key keeps the whole plan human — never partial-auto
  - plan_view: before/after bars, freed flag, stale detection
  - review_days: the passive banner only names days that free a vehicle
  - locate_postcodes: cache-first, misses cached too, API failure degrades
  - replan_day: no suggestion = no LLM call; propose accepts the engine's
    moves only; auto-apply fires only when configured AND safe

Run: python -m pytest tests/ -q
"""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app import planner
from app.planner import (auto_appliable, consolidate, locate_postcodes,
                         no_plan_reason, norm_postcode, plan_view, replan_day,
                         review_days)
from app.scheduling import booking_movable, haversine_miles, tour_miles

# Real-ish coordinates for the tie-break tests.
LEEDS = (53.7997, -1.5492)       # LS1
HULL = (53.7446, -0.3352)        # HU1
HULLISH = (53.7400, -0.3400)
LONDON = (51.5074, -0.1278)

COORDS = {"LS1 1AA": LEEDS, "HU1 1AA": HULL, "HU1 2BB": HULLISH}


def veh(vid, cap, branch=None, kg=None, name=None, active=True):
    return {"id": vid, "name": name or vid, "capacity_spaces": cap,
            "max_payload_kg": kg, "branch_id": branch, "active": active}


def bk(bid, vid, spaces, kg=None, drop=None, status="confirmed", date_iso="2026-06-15",
       ref=None, **extra):
    return {"id": bid, "vehicle_id": vid, "spaces": spaces, "weight_kg": kg,
            "delivery_postcode": drop, "status": status, "booking_date": date_iso,
            "booking_ref": ref or f"BK-{bid}", "summary": f"job {bid}", **extra}


# ── geometry ──────────────────────────────────────────────────────────────────
def test_haversine_known_distance():
    d = haversine_miles(*LONDON, *LEEDS)
    assert 160 < d < 180          # ~169 road-crow miles London → Leeds


def test_tour_miles_deterministic_and_chained():
    assert tour_miles(None, []) == 0.0
    assert tour_miles(LEEDS, []) == 0.0
    # start → nearest first: Leeds depot, drops Hull + Hullish → ~55mi total,
    # and strictly more than the single farthest leg.
    total = tour_miles(LEEDS, [HULL, HULLISH])
    assert haversine_miles(*LEEDS, *HULLISH) < total < 100
    # No depot: chain from the first point.
    assert tour_miles(None, [HULL, HULLISH]) == pytest.approx(
        haversine_miles(*HULL, *HULLISH))


def test_norm_postcode():
    assert norm_postcode("  ls9  8as ") == "LS9 8AS"
    assert norm_postcode(None) is None
    assert norm_postcode("   ") is None


# ── movable derivation (v1 guardrails) ───────────────────────────────────────
def test_booking_movable_rules():
    assert booking_movable(bk("b1", "v1", 3)) is True
    assert booking_movable(bk("b1", "v1", 3, status="proposed")) is True   # holds move too
    assert booking_movable(bk("b1", "v1", 3, status="cancelled")) is False
    assert booking_movable(bk("b1", None, 3)) is False        # no vehicle
    assert booking_movable(bk("b1", "v1", None)) is False     # unsized (legacy/calendar)


# ── the heuristic ─────────────────────────────────────────────────────────────
def test_consolidation_frees_the_box_van():
    """The demo: a cancellation leaves the 18t at 6.75/10 and the box at 3/6 —
    everything fits the 18t, the box is freed, only the box's bookings move."""
    vehicles = [veh("v-big", 10, name="18t curtainsider"), veh("v-box", 6, name="7.5t box")]
    rows = [bk("b1", "v-big", 4.0), bk("b2", "v-big", 2.75),
            bk("b3", "v-box", 2.0), bk("b4", "v-box", 1.0)]
    s = consolidate(vehicles, rows)
    assert s is not None
    assert s["frees"] == ["7.5t box"]
    assert s["vehicles_now"] == 2 and s["vehicles_after"] == 1
    moved = {m["booking_id"] for m in s["moves"]}
    assert moved == {"b3", "b4"}
    assert all(m["to_vehicle_id"] == "v-big" for m in s["moves"])
    assert len(s["described"]) == 2


def test_no_gain_when_loads_do_not_fit_together():
    """The honest Mon-15 case: 9.25 + 3 = 12.25 > 10 — nothing merges, no plan."""
    vehicles = [veh("v-big", 10), veh("v-box", 6)]
    rows = [bk("b1", "v-big", 5.0), bk("b2", "v-big", 4.25), bk("b3", "v-box", 3.0)]
    assert consolidate(vehicles, rows) is None
    assert "fit together" in no_plan_reason(vehicles, rows)


def test_never_churn_for_churns_sake():
    """A reshuffle that frees nothing and saves no miles must be dropped even
    when FFD's packing differs from the current assignment."""
    vehicles = [veh("v-a", 5), veh("v-b", 5)]
    rows = [bk("b1", "v-a", 3.0), bk("b2", "v-b", 4.0)]   # FFD would swap them
    assert consolidate(vehicles, rows) is None


def test_swapping_which_van_runs_is_not_a_plan():
    """The live Friday-19 regression: ONE booking on the box van, the bigger
    18t empty. Moving it 'frees the box' in name only — same vehicle count,
    same miles. The engine must stay silent (and the banner must not flag it)."""
    vehicles = [veh("v-big", 10, name="18t"), veh("v-box", 6, name="box")]
    rows = [bk("b1", "v-box", 2.0, drop="LS1 1AA")]
    assert consolidate(vehicles, rows, coords=COORDS) is None
    assert review_days(vehicles, rows, today=date(2026, 6, 14), horizon_days=28) == []
    assert "one vehicle" in no_plan_reason(vehicles, rows).lower()


def test_freeing_a_van_wins_even_when_miles_increase():
    """The live Wednesday-17 shape: 6 spaces to Birmingham on the 18t, 2 to
    York on the box. Merging puts both drops on one run — more miles on that
    van, but a whole vehicle off the road. The gate counts VEHICLES first."""
    coords = {"LS9 8AS": LEEDS, "B11 2AX": (52.4569, -1.8580), "YO26 6GA": (53.9576, -1.1336)}
    vehicles = [veh("v-big", 10, branch="br-l", name="18t"),
                veh("v-box", 6, branch="br-l", name="box")]
    branches = [{"id": "br-l", "postcode": "LS9 8AS"}]
    rows = [bk("b1", "v-big", 6.0, drop="B11 2AX"), bk("b2", "v-box", 2.0, drop="YO26 6GA")]
    s = consolidate(vehicles, rows, coords=coords, branches=branches)
    assert s is not None and s["frees"] == ["box"]
    assert s["vehicles_now"] == 2 and s["vehicles_after"] == 1
    assert s["moves"] == [{"booking_id": "b2", "to_vehicle_id": "v-big"}]
    assert s["saves_miles"] <= 0      # honest: the surviving run gets longer


def test_payload_respected_in_packing():
    """The 1500 kg load may not land on the van with a 1000 kg limit; the
    consolidation flows the OTHER way."""
    vehicles = [veh("v-light", 10, kg=1000, name="light"), veh("v-heavy", 6, name="heavy")]
    rows = [bk("b1", "v-light", 2.0, kg=200), bk("b2", "v-heavy", 3.0, kg=1500)]
    s = consolidate(vehicles, rows)
    assert s is not None and s["frees"] == ["light"]
    assert s["moves"] == [{"booking_id": "b1", "to_vehicle_id": "v-heavy"}]


def test_branch_boundary_never_crossed():
    """A Leeds booking never re-homes to a Manchester van, even when that would
    free a vehicle — the planner consolidates runs, it doesn't move depots."""
    vehicles = [veh("v-leeds", 10, branch="br-l"), veh("v-manc", 10, branch="br-m")]
    rows = [bk("b1", "v-leeds", 2.0), bk("b2", "v-manc", 3.0)]
    assert consolidate(vehicles, rows) is None


def test_haversine_tie_break_joins_the_nearer_run():
    """Two used vans both have room: the Hull-area drop joins the Hull run,
    not the Leeds one (the 'geographic nonsense' rejection)."""
    vehicles = [veh("v-a", 10, name="leeds-run"), veh("v-b", 10, name="hull-run"),
                veh("v-c", 4, name="small")]
    rows = [bk("b1", "v-a", 6.0, drop="LS1 1AA"),
            bk("b2", "v-b", 6.0, drop="HU1 1AA"),
            bk("b3", "v-c", 3.0, drop="HU1 2BB")]
    s = consolidate(vehicles, rows, coords=COORDS)
    assert s is not None and s["frees"] == ["small"]
    assert s["moves"] == [{"booking_id": "b3", "to_vehicle_id": "v-b"}]


def test_unlocated_drops_consolidate_by_size():
    vehicles = [veh("v-big", 10), veh("v-box", 6)]
    rows = [bk("b1", "v-big", 4.0), bk("b2", "v-box", 2.0)]   # no postcodes at all
    s = consolidate(vehicles, rows)
    assert s is not None and s["frees"] == ["v-box"]
    assert s["unlocated"] == 2
    assert s["miles_now"] == 0.0 and s["miles_after"] == 0.0


def test_pinned_rows_never_move_but_shrink_the_bin():
    """A row marked movable=False stays put and its space is honoured — the
    consolidation must fit AROUND it or not happen."""
    vehicles = [veh("v-big", 10), veh("v-box", 6)]
    rows = [bk("b1", "v-big", 8.0, movable=False), bk("b2", "v-box", 3.0)]
    # 8 pinned on the big van leaves 2 free — the 3-space load can't merge.
    assert consolidate(vehicles, rows) is None
    rows2 = [bk("b1", "v-big", 8.0, movable=False), bk("b2", "v-box", 1.5)]
    s = consolidate(vehicles, rows2)
    assert s is not None
    assert s["moves"] == [{"booking_id": "b2", "to_vehicle_id": "v-big"}]
    assert "b1" not in {m["booking_id"] for m in s["moves"]}


def test_inactive_vehicles_and_foreign_rows_ignored():
    vehicles = [veh("v-big", 10), veh("v-dead", 6, active=False)]
    rows = [bk("b1", "v-big", 2.0), bk("b2", "v-dead", 3.0),   # dead van's load: ignored
            bk("b3", None, 2.0), bk("b4", "v-big", None)]      # unassigned / unsized
    assert consolidate(vehicles, rows) is None


# ── auto-apply classification (D6) ───────────────────────────────────────────
def test_auto_appliable_safe_class_only():
    good = [{"booking_id": "b1", "to_vehicle_id": "v1"}]
    assert auto_appliable(good) is True
    assert auto_appliable([]) is False
    assert auto_appliable(None) is False
    assert auto_appliable([{"booking_id": "b1", "to_vehicle_id": "v1",
                            "to_date": "2026-06-16"}]) is False   # date move: human
    assert auto_appliable([{"booking_id": "b1", "to_vehicle_id": "v1",
                            "split": {"x": 1}}]) is False
    assert auto_appliable(good + [{"booking_id": "b2"}]) is False  # one bad = all wait
    assert auto_appliable([{"booking_id": "", "to_vehicle_id": "v1"}]) is False
    assert auto_appliable(["not-a-dict"]) is False


# ── plan_view (the board diff) ───────────────────────────────────────────────
def test_plan_view_bars_moves_and_freed():
    vehicles = [veh("v-big", 10, name="18t"), veh("v-box", 6, name="box")]
    rows = [bk("b1", "v-big", 4.0), bk("b2", "v-box", 3.0, drop="LS1 1AA", ref="BK-1010")]
    plan = {"id": "p1", "plan_date": "2026-06-15", "status": "proposed",
            "summary": "Frees the box", "created_at": "now",
            "moves": [{"booking_id": "b2", "to_vehicle_id": "v-big"}]}
    v = plan_view(plan, vehicles, rows)
    assert v["stale"] is False
    assert v["moves"][0]["ref"] == "BK-1010"
    assert v["moves"][0]["from_name"] == "box" and v["moves"][0]["to_name"] == "18t"
    bars = {b["name"]: b for b in v["vehicles"]}
    assert bars["box"]["before"] == 3.0 and bars["box"]["after"] == 0.0 and bars["box"]["freed"]
    assert bars["18t"]["before"] == 4.0 and bars["18t"]["after"] == 7.0
    assert v["frees"] == ["box"]


def test_plan_view_marks_stale_when_board_changed():
    vehicles = [veh("v-big", 10), veh("v-box", 6)]
    plan = {"id": "p1", "plan_date": "2026-06-15", "status": "proposed",
            "summary": "x", "moves": [{"booking_id": "b-gone", "to_vehicle_id": "v-big"}]}
    v = plan_view(plan, vehicles, [])
    assert v["stale"] is True and v["moves"] == []


# ── review_days (the passive banner) ─────────────────────────────────────────
def test_review_days_names_only_freeing_days():
    vehicles = [veh("v-big", 10, name="18t"), veh("v-box", 6, name="box")]
    rows = (
        [bk("b1", "v-big", 4.0, date_iso="2026-06-15"), bk("b2", "v-box", 2.0, date_iso="2026-06-15")]
        + [bk("b3", "v-big", 9.0, date_iso="2026-06-16"), bk("b4", "v-box", 3.0, date_iso="2026-06-16")]
        + [bk("b5", "v-big", 2.0, date_iso="2026-07-30")]   # beyond horizon / single van
    )
    out = review_days(vehicles, rows, today=date(2026, 6, 14), horizon_days=28)
    assert [r["date"] for r in out] == ["2026-06-15"]
    assert out[0]["frees"] == ["box"]


def test_review_days_respects_limit_and_past():
    vehicles = [veh("v-big", 10), veh("v-box", 6)]
    rows = []
    for i, d in enumerate(["2026-06-10", "2026-06-15", "2026-06-16", "2026-06-17"]):
        rows += [bk(f"a{i}", "v-big", 4.0, date_iso=d), bk(f"b{i}", "v-box", 2.0, date_iso=d)]
    out = review_days(vehicles, rows, today=date(2026, 6, 14), horizon_days=28, limit=2)
    assert [r["date"] for r in out] == ["2026-06-15", "2026-06-16"]   # past day dropped


# ── locate_postcodes (cache-first, misses cached, failure degrades) ──────────
class FakeHttpx:
    """Async-context fake for httpx.AsyncClient with a scripted bulk response."""

    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.posts = result or [], fail, []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.posts.append(json)
        if self.fail:
            raise RuntimeError("postcodes.io down")
        payload = {"status": 200, "result": self.result}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def test_locate_cache_hit_skips_network(monkeypatch):
    monkeypatch.setattr(planner.db, "get_postcodes", lambda codes: [
        {"postcode": "LS1 1AA", "lat": 53.8, "lng": -1.55},
        {"postcode": "ZZ1 1ZZ", "lat": None, "lng": None},   # a cached miss
    ])
    fake = FakeHttpx()
    monkeypatch.setattr(planner.httpx, "AsyncClient", fake)
    out = asyncio.run(locate_postcodes({"ls1 1aa", "zz1 1zz"}))
    assert out == {"LS1 1AA": (53.8, -1.55), "ZZ1 1ZZ": None}
    assert fake.posts == []                      # nothing left to fetch


def test_locate_fetches_and_caches_hits_and_misses(monkeypatch):
    written = []
    monkeypatch.setattr(planner.db, "get_postcodes", lambda codes: [])
    monkeypatch.setattr(planner.db, "put_postcodes", lambda rows: written.extend(rows))
    fake = FakeHttpx(result=[
        {"query": "LS1 1AA", "result": {"latitude": 53.8, "longitude": -1.55}},
        {"query": "ZZ1 1ZZ", "result": None},
    ])
    monkeypatch.setattr(planner.httpx, "AsyncClient", fake)
    out = asyncio.run(locate_postcodes({"LS1 1AA", "ZZ1 1ZZ"}))
    assert out["LS1 1AA"] == (53.8, -1.55) and out["ZZ1 1ZZ"] is None
    assert {w["postcode"]: w["lat"] for w in written} == {"LS1 1AA": 53.8, "ZZ1 1ZZ": None}


def test_locate_api_failure_degrades_and_caches_nothing(monkeypatch):
    written = []
    monkeypatch.setattr(planner.db, "get_postcodes", lambda codes: [])
    monkeypatch.setattr(planner.db, "put_postcodes", lambda rows: written.extend(rows))
    monkeypatch.setattr(planner.httpx, "AsyncClient", FakeHttpx(fail=True))
    out = asyncio.run(locate_postcodes({"LS1 1AA"}))
    assert out == {"LS1 1AA": None}              # unlocated this run
    assert written == []                          # a transient outage is never cached


# ── replan_day (the persona loop) ────────────────────────────────────────────
def usage_ns(n=10):
    return SimpleNamespace(input_tokens=n, output_tokens=n,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn", usage=usage_ns(), model="fake-model")


def tool_response(name, args, tool_id="tu-1"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
        stop_reason="tool_use", usage=usage_ns(), model="fake-model")


class FakePlannerClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


SUGG = {"moves": [{"booking_id": "b3", "to_vehicle_id": "v-big"}],
        "frees": ["box"], "vehicles_now": 2, "vehicles_after": 1,
        "miles_now": 30.0, "miles_after": 28.0, "saves_miles": 2.0,
        "unlocated": 0, "described": ["BK-1010 (3 spaces): box → 18t"]}


def wire_replan(monkeypatch, responses, suggestion=SUGG, auto_apply=False,
                apply_result=({"id": "p1", "status": "applied"}, None)):
    env = SimpleNamespace(inserted=[], applied=[], usage=[], superseded=[])

    async def fake_state(tenant_id, date_iso, client=None):
        state = {"date": date_iso, "vehicles": [], "suggestion": suggestion}
        if not suggestion:
            state["verdict"] = "Only one vehicle is in use — there is nothing to consolidate."
        return state

    def fake_insert(tenant_id, day, summary, moves):
        row = {"id": "p1", "tenant_id": tenant_id, "plan_date": day,
               "summary": summary, "moves": moves, "status": "proposed"}
        env.inserted.append(row)
        return row

    monkeypatch.setattr(planner, "day_state", fake_state)
    monkeypatch.setattr(planner.db, "insert_plan", fake_insert)
    monkeypatch.setattr(planner.db, "discard_plans_for_date",
                        lambda tid, day, **k: env.superseded.append(day))
    monkeypatch.setattr(planner.db, "get_tenant_config",
                        lambda *a, **k: {"planner_auto_apply": auto_apply})
    monkeypatch.setattr(planner.db, "apply_plan",
                        lambda pid, tid: (env.applied.append(pid) or apply_result))
    monkeypatch.setattr(planner.usage, "record_anthropic",
                        lambda *a, **k: env.usage.append(k))
    fake = FakePlannerClient(responses)
    monkeypatch.setattr(planner, "_client", fake)
    env.client = fake
    return env


def test_replan_no_suggestion_skips_the_llm(monkeypatch):
    env = wire_replan(monkeypatch, [text_response("never called")], suggestion=None)
    out = asyncio.run(replan_day("t1", "2026-06-15"))
    assert out["plan"] is None and out["applied"] is False
    assert "nothing to consolidate" in out["note"]
    assert env.client.calls == []                 # the honest verdict is free


def test_replan_proposes_with_engine_moves_verbatim(monkeypatch):
    env = wire_replan(monkeypatch, [
        tool_response("get_day_state", {"date": "2026-06-15"}),
        tool_response("propose_plan", {"date": "2026-06-15", "moves": SUGG["moves"],
                                       "summary": "Frees the box — both fit the 18t."}),
        text_response("Proposed: frees the box."),
    ])
    out = asyncio.run(replan_day("t1", "2026-06-15"))
    assert out["plan"]["id"] == "p1" and out["applied"] is False
    assert env.inserted[0]["summary"] == "Frees the box — both fit the 18t."
    assert env.inserted[0]["moves"] == SUGG["moves"]   # the ENGINE's moves stored
    assert env.superseded == ["2026-06-15"]   # replan supersedes prior proposals
    assert out["note"] == "Proposed: frees the box."
    assert env.usage and env.usage[0]["operation"] == "plan"
    assert env.applied == []                       # auto-apply off


def test_replan_rejects_invented_moves(monkeypatch):
    """The model may never do arithmetic: moves that aren't the engine's are
    refused and no plan row is written."""
    env = wire_replan(monkeypatch, [
        tool_response("get_day_state", {"date": "2026-06-15"}),
        tool_response("propose_plan", {"date": "2026-06-15",
                                       "moves": [{"booking_id": "b3", "to_vehicle_id": "v-WRONG"}],
                                       "summary": "creative plan"}),
        text_response("Declining."),
    ])
    out = asyncio.run(replan_day("t1", "2026-06-15"))
    assert out["plan"] is None and env.inserted == []
    # The refusal reached the model as a tool result.
    last_tool_result = env.client.calls[-1]["messages"][-1]["content"][0]
    assert "moves_mismatch" in last_tool_result["content"]


def test_replan_auto_applies_safe_plans_when_configured(monkeypatch):
    env = wire_replan(monkeypatch, [
        tool_response("get_day_state", {"date": "2026-06-15"}),
        tool_response("propose_plan", {"date": "2026-06-15", "moves": SUGG["moves"],
                                       "summary": "Frees the box."}),
        text_response("Done."),
    ], auto_apply=True)
    out = asyncio.run(replan_day("t1", "2026-06-15"))
    assert env.applied == ["p1"]
    assert out["applied"] is True and out["plan"]["status"] == "applied"


def test_replan_auto_apply_failure_leaves_plan_proposed(monkeypatch):
    env = wire_replan(monkeypatch, [
        tool_response("get_day_state", {"date": "2026-06-15"}),
        tool_response("propose_plan", {"date": "2026-06-15", "moves": SUGG["moves"],
                                       "summary": "Frees the box."}),
        text_response("Done."),
    ], auto_apply=True, apply_result=(None, "booking BK-1010 is no longer active"))
    out = asyncio.run(replan_day("t1", "2026-06-15"))
    assert out["applied"] is False
    assert out["plan"]["status"] == "proposed"     # still on the board for a human
    assert "no longer active" in out["apply_error"]
