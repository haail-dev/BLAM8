"""Transport planner agent (docs/modules/module-transport-planner.md).

The 4pm transport-planner brain, automated: read a day's board, propose load
consolidations (fewest vans, sensible miles), apply approved plans atomically —
and, once trusted via planner_auto_apply, apply the safe class of moves itself.
A second persona whose tools read/write FLEET STATE, never an inbox; it ships
inside the `routing` module (D7 — no new module id in v1).

The division of labour is the module's spine (D5): the HEURISTIC computes —
first-fit-decreasing bin-pack onto the fewest vehicles that satisfy
spaces+payload+branch, tie-broken by estimated haversine miles — and the LLM
narrates and judges: it writes the one-line rationale a dispatcher would trust,
vetoes nonsense, and never does arithmetic. propose_plan therefore only accepts
the engine's moves VERBATIM; plans land as 'proposed' rows the board renders as
a before/after diff, and apply goes through the apply_plan SQL function — one
transaction, whole-day re-validation, stale plans die loudly (D6).

Geometry (D4): postcode → lat/lng from the free postcodes.io bulk API, cached
forever in the postcodes table (misses too). Straight-line haversine is plenty
to RANK merges and reject a Hull drop on a Manchester run; if postcodes.io is
down, scoring degrades to fewest-vans-only and never blocks a plan. Google
stays where it is: map display, and (a later slice) drop sequencing.
"""
from app import metering

import json
import logging
from datetime import date, datetime, timedelta

import httpx

from app import db, llm, usage
from app.config import settings
from app.scheduling import (ACTIVE_STATUSES, DIARY_TZ, booking_movable,
                            fmt_spaces, haversine_miles, tour_miles)

log = logging.getLogger(__name__)

_client = None


def client():
    """Lazily-created Anthropic client (same pattern as app.agent.client —
    import never fails on missing env, tests monkeypatch `_client`)."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        from app import llm as _llm
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key,
                                 timeout=_llm.CALL_TIMEOUT_S)
    return _client


POSTCODES_URL = "https://api.postcodes.io/postcodes"
_POSTCODES_BATCH = 100          # postcodes.io bulk-lookup cap per request

# When no vehicle is freed, a plan must still cut estimated miles MATERIALLY
# to be worth a dispatcher's attention — never churn for churn's sake (D5).
_MIN_MILES_SAVED = 10.0
_MIN_MILES_PCT = 0.15

# The propose loop normally ends in three calls (state → propose → narrate);
# the cap is the backstop, same pattern as drafting's TOOL_LOOP_MAX_CALLS.
PLANNER_MAX_CALLS = 4


def norm_postcode(raw) -> str | None:
    """The postcodes-table key: uppercased, single-spaced. UK postcodes are
    case/spacing-insensitive; one canonical form keeps the cache hit rate
    honest."""
    s = " ".join(str(raw or "").split()).upper()
    return s[:16] or None


async def locate_postcodes(codes: set[str]) -> dict[str, tuple[float, float] | None]:
    """lat/lng for each (normalised) postcode: the postcodes table first, then
    one bulk postcodes.io call for the rest — results cached forever, MISSES
    included (lat/lng null), so a bad postcode is never re-queried. Any API
    failure degrades to 'unlocated' for the uncached codes: scoring then ranks
    by size alone — a plan is never blocked on geometry (D4)."""
    wanted = sorted({c for c in (norm_postcode(c) for c in codes) if c})
    out: dict = {}
    if not wanted:
        return out
    try:
        cached = db.get_postcodes(wanted)
    except Exception:
        log.warning("planner: postcode cache read failed", exc_info=True)
        cached = []
    for row in cached:
        out[row["postcode"]] = (
            (row["lat"], row["lng"]) if row.get("lat") is not None else None
        )
    missing = [c for c in wanted if c not in out]
    if not missing:
        return out
    fresh: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for i in range(0, len(missing), _POSTCODES_BATCH):
                batch = missing[i:i + _POSTCODES_BATCH]
                r = await c.post(POSTCODES_URL, json={"postcodes": batch})
                r.raise_for_status()
                for item in (r.json().get("result") or []):
                    code = norm_postcode(item.get("query"))
                    hit = item.get("result") or None
                    if not code:
                        continue
                    if hit and hit.get("latitude") is not None:
                        out[code] = (float(hit["latitude"]), float(hit["longitude"]))
                        fresh.append({"postcode": code, "lat": out[code][0],
                                      "lng": out[code][1]})
                    else:
                        out[code] = None          # a real miss — cache it too
                        fresh.append({"postcode": code, "lat": None, "lng": None})
    except Exception:
        log.warning("planner: postcodes.io lookup failed — scoring degrades to "
                    "fewest-vans for %d postcode(s)", len(missing), exc_info=True)
        for code in missing:
            out.setdefault(code, None)            # unlocated this run; NOT cached
    if fresh:
        try:
            db.put_postcodes(fresh)
        except Exception:
            log.warning("planner: postcode cache write failed", exc_info=True)
    return out


# ── the consolidation heuristic (pure — unit-first, like all diary math) ─────
def _drop_coords(b: dict, coords: dict) -> tuple | None:
    """A booking's drop for scoring = its DELIVERY postcode (collection is
    usually the depot side and the manifest's business, not the ranker's)."""
    code = norm_postcode(b.get("delivery_postcode"))
    return coords.get(code) if code else None


def consolidate(vehicles: list[dict], bookings: list[dict],
                coords: dict | None = None, branches: list[dict] | None = None) -> dict | None:
    """The v1 planning engine (D5): pack one day's sized bookings onto the
    fewest ACTIVE vehicles that satisfy spaces + payload + branch, then keep
    the result ONLY if it frees ≥1 vehicle or cuts estimated miles materially.

    First-fit-decreasing: bookings by spaces descending into bins (vehicles)
    ordered capacity-descending; when several bins fit, prefer a bin already
    in use (fewest vans), then the bin whose run is NEAREST the booking's drop
    (the haversine tie-break — a Hull drop never joins a Manchester run for
    nothing), then the booking's CURRENT vehicle (stability — never move a
    booking between two equally-good vans), then bin order. A booking may only
    ride vehicles of its CURRENT vehicle's branch — the planner consolidates
    runs, it never re-homes work to another depot. Unmovable rows
    (scheduling.booking_movable) are pinned where they are and only shrink the
    target bin.

    The worthwhile gate counts VEHICLES, not names: a packing that merely
    swaps which van runs (a lone box-van booking "freed" onto an empty 18t —
    the live Friday-19 case) reduces nothing and is churn, never a plan.

    `coords` maps normalised postcodes → (lat, lng) | None; empty/None = score
    by size only (the postcodes.io-down degradation, and the cheap render-time
    banner mode). Returns the suggestion dict, or None when nothing worthwhile:
      {moves: [{booking_id, to_vehicle_id}], frees: [names], vehicles_now,
       vehicles_after, miles_now, miles_after, saves_miles, unlocated,
       described: [human move lines]}
    Pure: vehicles + bookings + coords in, suggestion out."""
    coords = coords or {}
    pool = {v["id"]: v for v in vehicles if v.get("active", True)}
    rows = [b for b in bookings
            if b.get("vehicle_id") in pool
            and b.get("spaces") is not None
            and b.get("status", "confirmed") in ACTIVE_STATUSES]
    if not rows:
        return None
    depot_codes = {b["id"]: norm_postcode(b.get("postcode")) for b in (branches or [])}

    # Bins per branch group, capacity-descending (FFD's fixed order).
    by_branch: dict = {}
    for b in rows:
        by_branch.setdefault(pool[b["vehicle_id"]].get("branch_id"), []).append(b)

    assignment: dict = {}      # booking id -> target vehicle id
    infeasible = False
    for branch_id, group in by_branch.items():
        bins = []
        for v in sorted((v for v in pool.values() if v.get("branch_id") == branch_id),
                        key=lambda v: (-float(v["capacity_spaces"]), str(v["id"]))):
            depot = coords.get(depot_codes.get(branch_id)) if depot_codes.get(branch_id) else None
            bins.append({"vehicle": v, "free_sp": float(v["capacity_spaces"]),
                         "free_kg": (float(v["max_payload_kg"])
                                     if v.get("max_payload_kg") is not None else None),
                         "drops": [], "used": False, "depot": depot})
        bin_by_vid = {b["vehicle"]["id"]: b for b in bins}

        def _place(bin_, row):
            bin_["free_sp"] -= float(row["spaces"])
            if bin_["free_kg"] is not None:
                bin_["free_kg"] -= float(row.get("weight_kg") or 0)
            drop = _drop_coords(row, coords)
            if drop:
                bin_["drops"].append(drop)
            bin_["used"] = True
            assignment[row["id"]] = bin_["vehicle"]["id"]

        movable, pinned = [], []
        for row in group:
            # The derived guardrails, plus any explicit movable=False mark a
            # caller stamped (day_state's derivation, future date-locked rows).
            ok = booking_movable(row) and row.get("movable", True)
            (movable if ok else pinned).append(row)
        for row in pinned:                      # pinned rows stay put, shrink their bin
            _place(bin_by_vid[row["vehicle_id"]], row)

        def _added_miles(bin_, drop):
            if drop is None:
                return 0.0
            if bin_["drops"]:
                return min(haversine_miles(d[0], d[1], drop[0], drop[1])
                           for d in bin_["drops"])
            if bin_["depot"]:
                return haversine_miles(bin_["depot"][0], bin_["depot"][1],
                                       drop[0], drop[1])
            return 0.0

        for row in sorted(movable, key=lambda r: (-float(r["spaces"]),
                                                  -float(r.get("weight_kg") or 0),
                                                  str(r["id"]))):
            kg = float(row.get("weight_kg") or 0)
            fits = [
                (i, bn) for i, bn in enumerate(bins)
                if bn["free_sp"] >= float(row["spaces"]) - 1e-9
                and (bn["free_kg"] is None or bn["free_kg"] >= kg - 1e-9)
            ]
            if not fits:
                infeasible = True               # FFD failed — never propose nonsense
                break
            drop = _drop_coords(row, coords)
            _, best = min(fits, key=lambda ib: (not ib[1]["used"],
                                                _added_miles(ib[1], drop),
                                                ib[1]["vehicle"]["id"] != row["vehicle_id"],
                                                ib[0]))
            _place(best, row)
        if infeasible:
            break
    if infeasible:
        return None

    moves = [{"booking_id": b["id"], "to_vehicle_id": assignment[b["id"]]}
             for b in rows if assignment[b["id"]] != b["vehicle_id"]]
    if not moves:
        return None

    now_ids = {b["vehicle_id"] for b in rows}
    after_ids = set(assignment.values())
    # frees is honest only when the COUNT drops — a swap's set-difference is
    # not a freed van (the gate below uses the counts, never this list).
    frees = (sorted(pool[vid]["name"] for vid in now_ids - after_ids)
             if len(after_ids) < len(now_ids) else [])

    def _total_miles(assign_of) -> tuple[float, int]:
        per_vehicle: dict = {}
        unlocated = 0
        for b in rows:
            drop = _drop_coords(b, coords)
            if drop is None:
                unlocated += 1
                continue
            per_vehicle.setdefault(assign_of(b), []).append(drop)
        total = 0.0
        for vid, drops in per_vehicle.items():
            v = pool[vid]
            depot = (coords.get(depot_codes.get(v.get("branch_id")))
                     if depot_codes.get(v.get("branch_id")) else None)
            total += tour_miles(depot, drops)
        return total, unlocated

    miles_now, unlocated = _total_miles(lambda b: b["vehicle_id"])
    miles_after, _ = _total_miles(lambda b: assignment[b["id"]])
    saves = miles_now - miles_after
    worthwhile = len(after_ids) < len(now_ids) or (
        miles_now > 0 and saves >= max(_MIN_MILES_SAVED, miles_now * _MIN_MILES_PCT)
    )
    if not worthwhile:
        return None

    bmap = {b["id"]: b for b in rows}
    described = []
    for m in moves:
        b = bmap[m["booking_id"]]
        described.append(
            f"{b.get('booking_ref') or 'booking'} ({fmt_spaces(b['spaces'])} spaces"
            + (f", → {b['delivery_postcode']}" if b.get("delivery_postcode") else "")
            + f"): {pool[b['vehicle_id']]['name']} → {pool[m['to_vehicle_id']]['name']}"
        )
    return {
        "moves": moves,
        "frees": frees,
        "vehicles_now": len(now_ids),
        "vehicles_after": len(after_ids),
        "miles_now": round(miles_now, 1),
        "miles_after": round(miles_after, 1),
        "saves_miles": round(saves, 1),
        "unlocated": unlocated,
        "described": described,
    }


def no_plan_reason(vehicles: list[dict], bookings: list[dict]) -> str:
    """The honest one-liner when the engine has nothing to propose — shown on
    the board instead of a plan (the Mon-15 demo's 'no-gain verdict')."""
    pool = {v["id"] for v in vehicles if v.get("active", True)}
    rows = [b for b in bookings
            if b.get("vehicle_id") in pool and b.get("spaces") is not None
            and b.get("status", "confirmed") in ACTIVE_STATUSES]
    if not rows:
        return "Nothing is booked on the fleet for this date."
    if len({b["vehicle_id"] for b in rows}) < 2:
        return "Only one vehicle is in use — there is nothing to consolidate."
    return ("No move frees a vehicle or cuts meaningful miles — the loads "
            "don't fit together any tighter than they already are.")


def auto_appliable(moves: list) -> bool:
    """The safe class (D6): every move is a same-day VEHICLE move — exactly
    {booking_id, to_vehicle_id}, nothing else. Anything more exotic (a
    to_date, a split, an unknown key) keeps the WHOLE plan waiting for a
    human — never partial-auto; date moves always need a person (inherited
    route-planning amendment)."""
    if not isinstance(moves, list) or not moves:
        return False
    for m in moves:
        if not isinstance(m, dict) or set(m.keys()) != {"booking_id", "to_vehicle_id"}:
            return False
        if not m["booking_id"] or not m["to_vehicle_id"]:
            return False
    return True


# ── day state (the planner's read tool) ──────────────────────────────────────
async def day_state(tenant_id: str, date_iso: str, client=None) -> dict:
    """Everything the planner (and its LLM) sees for one date: per-vehicle
    capacity/payload/branch/depot + bookings (with `movable` derived from the
    guardrails), plus `suggestion` — the engine's consolidation, or null with
    an honest reason. One postcode lookup covers drops and depots alike."""
    day = date.fromisoformat(str(date_iso).strip()).isoformat()
    vehicles = db.list_vehicles(tenant_id, client=client, active_only=True)
    branches = db.list_branches(tenant_id, client=client)
    rows = db.list_date_bookings(tenant_id, day, client=client)
    codes = {b.get("delivery_postcode") for b in rows} | {b.get("postcode") for b in branches}
    coords = await locate_postcodes({c for c in codes if c})
    suggestion = consolidate(vehicles, rows, coords, branches)

    branch_names = {b["id"]: b.get("name") for b in branches}
    branch_codes = {b["id"]: b.get("postcode") for b in branches}
    out_vehicles = []
    for v in vehicles:
        v_rows = [b for b in rows
                  if b.get("vehicle_id") == v["id"] and b.get("spaces") is not None]
        used = round(sum(float(b["spaces"]) for b in v_rows), 2)
        out_vehicles.append({
            "vehicle_id": v["id"],
            "name": v["name"],
            "registration": v.get("registration"),
            "branch": branch_names.get(v.get("branch_id")),
            "depot_postcode": branch_codes.get(v.get("branch_id")),
            "capacity_spaces": float(v["capacity_spaces"]),
            "max_payload_kg": (float(v["max_payload_kg"])
                               if v.get("max_payload_kg") is not None else None),
            "used_spaces": used,
            "free_spaces": round(float(v["capacity_spaces"]) - used, 2),
            "bookings": [{
                "booking_id": b["id"],
                "booking_ref": b.get("booking_ref"),
                "summary": b.get("summary"),
                "spaces": float(b["spaces"]),
                "weight_kg": (float(b["weight_kg"])
                              if b.get("weight_kg") is not None else None),
                "collection_postcode": b.get("collection_postcode"),
                "delivery_postcode": b.get("delivery_postcode"),
                "status": b.get("status"),
                "movable": booking_movable(b),
            } for b in v_rows],
        })
    state = {"date": day, "vehicles": out_vehicles, "suggestion": suggestion}
    if not suggestion:
        state["verdict"] = no_plan_reason(vehicles, rows)
    elif suggestion["unlocated"]:
        state["note"] = (f"{suggestion['unlocated']} drop(s) unlocated (no delivery "
                         "postcode) — consolidated by size; the miles estimate is partial.")
    return state


# ── the persona (tools over fleet state — never an inbox) ────────────────────
PLANNER_TOOLS = [
    {
        "name": "get_day_state",
        "description": (
            "One day's transport board: every active vehicle with its capacity, "
            "payload, branch, depot postcode and bookings (each marked movable or "
            "not), plus `suggestion` — the consolidation the planning engine "
            "computed for this date (moves, what it frees, estimated miles before "
            "and after), or null with a `verdict` when nothing is worth proposing. "
            "Call this FIRST."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The date to plan, YYYY-MM-DD."},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_plan",
        "description": (
            "Put the engine's suggested plan on the board for a human to approve. "
            "Pass the suggestion's moves EXACTLY as get_day_state returned them — "
            "you are the judgment and the narrator, never the arithmetic — and a "
            "one-line summary a dispatcher would trust. Call it at most once, and "
            "ONLY when the suggestion is genuinely worth doing; to veto, simply "
            "don't call it and reply with one honest line saying why."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "format": "date",
                         "description": "The plan's date — the one you planned."},
                "moves": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "booking_id": {"type": "string"},
                            "to_vehicle_id": {"type": "string"},
                        },
                        "required": ["booking_id", "to_vehicle_id"],
                        "additionalProperties": False,
                    },
                    "description": "The engine's moves, verbatim.",
                },
                "summary": {"type": "string",
                            "description": "One line a dispatcher would trust, e.g. "
                                           "'Frees the 7.5t box — both part-loads fit "
                                           "the 18t with 0.25 spaces spare.'"},
            },
            "required": ["date", "moves", "summary"],
            "additionalProperties": False,
        },
    },
]


def build_planner_prompt(today: date) -> str:
    return (
        "You are the transport planner for a UK haulage business, reviewing one "
        f"day's vehicle loads. Today is {today.strftime('%A %d %B %Y')} "
        f"({today.isoformat()}).\n\n"
        "How this works:\n"
        "1. Call get_day_state for the requested date. It includes `suggestion` — "
        "the moves computed by the planning engine (first-fit-decreasing "
        "consolidation: fewest vehicles, then fewest estimated road miles).\n"
        "2. Your role is JUDGMENT and NARRATION, never arithmetic. Do not invent, "
        "add, drop or reorder moves — propose_plan only accepts the engine's "
        "moves exactly as given.\n"
        "3. The engine only suggests a plan that REDUCES the vehicle count "
        "(vehicles_now → vehicles_after) or cuts estimated miles materially — "
        "trust those numbers, never re-derive them. Taking a whole vehicle off "
        "the road is normally worth a few extra miles on the surviving run. "
        "Veto only OPERATIONAL nonsense a dispatcher would refuse — loads that "
        "plainly cannot share a vehicle, pairings that make no real-world sense "
        "— by NOT calling propose_plan and replying with one honest line why.\n"
        "4. Otherwise call propose_plan with the moves verbatim and a one-line "
        "summary a dispatcher would trust — name what it frees or saves, and "
        "note when the miles estimate is partial because drops are unlocated.\n"
        "5. A proposed plan changes NOTHING until a human approves it on the "
        "board (or the business has switched safe auto-apply on) — never claim "
        "anything has already moved.\n"
        "6. Your final reply is shown to the dispatcher in the dashboard: one or "
        "two short factual lines. No greetings, no sign-offs."
    )


@llm.with_request_scope   # every planner loop call of one replan shares one request_id
async def replan_day(tenant_id: str, date_iso: str) -> dict:
    """The 'Replan this day' brain (trigger v1 — locked Q3): compute the day
    state + suggestion, let the persona judge and narrate, write the plan row,
    and — when planner_auto_apply is on and every move is the safe class —
    apply it immediately. Returns {plan, applied, note, apply_error}.

    No suggestion = no LLM call: the honest no-gain verdict is deterministic
    and free. Metered like drafting (usage op 'plan', module 'tms')."""
    day = date.fromisoformat(str(date_iso).strip()).isoformat()
    state = await day_state(tenant_id, day)
    sugg = state.get("suggestion")
    if not sugg:
        return {"plan": None, "applied": False, "note": state.get("verdict"),
                "apply_error": None}

    run: dict = {"plan": None}
    canonical = {(m["booking_id"], m["to_vehicle_id"]) for m in sugg["moves"]}

    def execute(name: str, args: dict) -> dict:
        if name == "get_day_state":
            if str(args.get("date") or "").strip() != day:
                return {"error": "wrong_date",
                        "message": f"This run plans {day} — pass exactly that date."}
            return state
        if name == "propose_plan":
            if run["plan"]:
                return {"error": "already_proposed", "plan_id": run["plan"]["id"],
                        "message": "This run already proposed its plan."}
            if str(args.get("date") or "").strip() != day:
                return {"error": "wrong_date",
                        "message": f"This run plans {day} — pass exactly that date."}
            try:
                passed = {(m["booking_id"], m["to_vehicle_id"])
                          for m in (args.get("moves") or [])}
            except (TypeError, KeyError):
                passed = set()
            if passed != canonical:
                return {"error": "moves_mismatch",
                        "message": "Those are not the engine's moves. Pass the "
                                   "suggestion's moves exactly as get_day_state "
                                   "returned them, or decline without proposing."}
            summary = (str(args.get("summary") or "").strip()
                       or "Consolidation proposed by the planner")[:200]
            # Replan supersedes: one ACTIVE plan per day — any earlier proposed
            # plan for this date is discarded, so double-clicks and stale
            # proposals never stack cards on the board.
            db.discard_plans_for_date(tenant_id, day)
            run["plan"] = db.insert_plan(tenant_id, day, summary, sugg["moves"])
            return {"proposed": True, "plan_id": run["plan"]["id"],
                    "moves": len(sugg["moves"]),
                    "status": "proposed — a human approves it on the board"}
        return {"error": "unknown_tool", "message": f"No tool named {name}."}

    system = build_planner_prompt(datetime.now(DIARY_TZ).date())
    msgs: list[dict] = [{"role": "user", "content": f"Replan {day}."}]
    totals = metering.Totals()
    msg = None
    for i in range(PLANNER_MAX_CALLS):
        kwargs: dict = {"tools": PLANNER_TOOLS}
        if i == PLANNER_MAX_CALLS - 1:
            kwargs["tool_choice"] = {"type": "none"}   # budget spent — narrate now
        msg = await llm.create(
            client=client(), tenant_id=tenant_id, operation="plan", module="tms",
            model=settings.anthropic_model, max_tokens=512,
            system=system, messages=msgs, **kwargs,
        )
        metering.add(totals, msg.usage)
        if msg.stop_reason != "tool_use":
            break
        tool_uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            break
        msgs.append({"role": "assistant", "content": msg.content})
        results = []
        for tu in tool_uses:
            try:
                out = execute(tu.name, dict(tu.input or {}))
            except Exception:
                log.warning("planner tool %s failed", tu.name, exc_info=True)
                out = {"error": "tool_failed",
                       "message": "The board is temporarily unavailable — do not "
                                  "propose anything; say the replan should be retried."}
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    note = "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text").strip() if msg else ""
    usage.record_anthropic(tenant_id, msg.model if msg else settings.anthropic_model,
                           totals, operation="plan", module="tms")

    applied, apply_error = False, None
    if run["plan"]:
        cfg = db.get_tenant_config(tenant_id) or {}
        if cfg.get("planner_auto_apply") and auto_appliable(sugg["moves"]):
            applied_row, apply_error = db.apply_plan(run["plan"]["id"], tenant_id)
            if applied_row:
                run["plan"], applied = applied_row, True
            # On failure the plan stays proposed on the board — a human decides.
    return {"plan": run["plan"], "applied": applied, "note": note,
            "apply_error": apply_error}


# ── board view-models (pure) ─────────────────────────────────────────────────
def plan_view(plan: dict, vehicles: list[dict], bookings: list[dict]) -> dict:
    """One proposed plan as the board renders it (D6): before/after load per
    affected vehicle + the move list with refs. `bookings` are the plan date's
    rows. stale=True when a move's booking is missing/moved/resized — rendered
    as a warning; apply remains the only judge."""
    vmap = {v["id"]: v for v in vehicles}
    bmap = {b["id"]: b for b in bookings
            if b.get("spaces") is not None
            and b.get("status", "confirmed") in ACTIVE_STATUSES}
    used: dict = {}
    for b in bmap.values():
        if b.get("vehicle_id"):
            used[b["vehicle_id"]] = used.get(b["vehicle_id"], 0.0) + float(b["spaces"])
    after = dict(used)
    moves_out, affected, stale = [], [], False
    for m in plan.get("moves") or []:
        b = bmap.get(m.get("booking_id"))
        to_v = vmap.get(m.get("to_vehicle_id"))
        if not b or not to_v or not b.get("vehicle_id"):
            stale = True
            continue
        from_v = vmap.get(b["vehicle_id"])
        sp = float(b["spaces"])
        after[b["vehicle_id"]] = after.get(b["vehicle_id"], 0.0) - sp
        after[to_v["id"]] = after.get(to_v["id"], 0.0) + sp
        affected += [b["vehicle_id"], to_v["id"]]
        moves_out.append({
            "ref": b.get("booking_ref"),
            "summary": b.get("summary"),
            "spaces": sp,
            "delivery_postcode": b.get("delivery_postcode"),
            "from_name": (from_v or {}).get("name") or "(removed vehicle)",
            "to_name": to_v["name"],
        })
    bars = []
    seen = set()
    for vid in affected:
        if vid in seen or vid not in vmap:
            continue
        seen.add(vid)
        cap = float(vmap[vid]["capacity_spaces"])
        before_sp, after_sp = round(used.get(vid, 0.0), 2), round(after.get(vid, 0.0), 2)
        bars.append({
            "name": vmap[vid]["name"], "capacity": cap,
            "before": before_sp, "after": after_sp,
            "before_pct": min(100, round(100 * before_sp / cap) if cap else 0),
            "after_pct": min(100, round(100 * after_sp / cap) if cap else 0),
            "freed": after_sp <= 1e-9 < before_sp,
        })
    return {
        "id": plan["id"], "date": plan["plan_date"], "status": plan["status"],
        "summary": plan.get("summary"), "created_at": plan.get("created_at"),
        "moves": moves_out, "vehicles": bars,
        "frees": [b["name"] for b in bars if b["freed"]],
        "stale": stale or not moves_out,
    }


def review_days(vehicles: list[dict], bookings: list[dict], today: date,
                horizon_days: int, limit: int = 3) -> list[dict]:
    """The passive freed-capacity banner (locked Q3): upcoming dates whose
    board could run on FEWER vehicles, computed size-only (no geocoding at
    render time — the replan run does the full scoring). Pure; returns at most
    `limit` entries: [{date, label, frees, vehicles_now, vehicles_after}]."""
    try:
        end = today + timedelta(days=int(horizon_days))
    except (TypeError, ValueError):
        end = None
    by_date: dict = {}
    for b in bookings:
        d = str(b.get("booking_date") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(b)
    out = []
    for d in sorted(by_date):
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        if day < today or (end and day > end):
            continue
        s = consolidate(vehicles, by_date[d])
        if s and s["frees"]:
            out.append({"date": d, "label": day.strftime("%A %d %B"),
                        "frees": s["frees"], "vehicles_now": s["vehicles_now"],
                        "vehicles_after": s["vehicles_after"]})
            if len(out) >= limit:
                break
    return out
