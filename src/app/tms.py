"""The TMS view — one operational cockpit over the transport modules.

Julius's "what's possible" demo (2026-06-11): the Fleet & Capacity chip opens
a TMS page that looks like the product hauliers already know (Mandata's job
board, MyTrucking's daysheet, Stream's dispatch view) — except every number on
it is LIVE from the same diary the email agent books into, and the actions on
it (Replan) are the planner agent. Not a new module: a SURFACE over fleet (+
routing when active), gated like the board. The research behind the framing:
incumbents' intake is a human re-keying emails; the modern AI-TMS (Qargo)
extracts orders but never answers the customer and leaves planning manual —
this page exists to show both halves wired in.

Pure view-model (overview) + a thin fetch wrapper (view) in the house style:
config + rows in, render-ready dict out, unit-tested without a db. Transport
rows only (spaces is null = a people row — the office diary's business, never
counted here; the same isolation rule as everywhere).
"""
from datetime import datetime, timedelta

from app import db
from app import planner as planner_mod
from app.scheduling import (ACTIVE_STATUSES, DIARY_TZ, _as_date, _day_loads,
                            _fleet_cfg, _norm, fleet_diary_line, fmt_spaces)

DISPATCH_DAYS = 5      # the daysheet strip: next N working days
WEEK_DAYS = 7          # the utilisation KPI window (working days)
POSTCODE_LOOKAHEAD = 14   # days scanned for drops with no delivery postcode
FEED_ROWS = 8          # the order-intake feed length

_SOURCE_LABELS = {"email": "Email agent", "import": "Order book",
                  "manual": "Added by you", "calendar": "Calendar"}


def _working_days(c: dict, today, count: int) -> list:
    """The next `count` working days (blackouts skipped), today included."""
    out, day, hop = [], today, 0
    while len(out) < count and hop <= c["horizon_days"]:
        if day.weekday() in c["working_days"] and day not in c["blackouts"]:
            out.append(day)
        day += timedelta(days=1)
        hop += 1
    return out


_DISPATCH_BARS = 6     # vehicle mini-bars shown per day card before "+N more"


def overview(cfg: dict | None, vehicles: list[dict], branches: list[dict],
             bookings: list[dict], plans: list[dict], now: datetime,
             scheduling_on: bool = False, routing_on: bool = False,
             branch_id: str | None = None) -> dict:
    """Everything /app/tms renders. `bookings` = the tenant's rows from today
    forward (any active status); only TRANSPORT rows (sized) participate.
    `plans` = the proposed plans. Pure — frozen by tests like the board.

    `branch_id` is the enterprise lens (multi-depot fleets): every section —
    KPIs, dispatch, exceptions, nudges, the feed — scopes to that depot's
    vehicles, exactly like the board's branch tabs. The all-branches view of a
    multi-depot tenant additionally opens with PER-DEPOT tiles (vans, week
    utilisation, jobs), each drilling into that depot's own cockpit. Bookings
    whose vehicle was deleted have no branch, so they appear on the
    all-branches view only."""
    c = _norm(_fleet_cfg(cfg))
    today = now.astimezone(DIARY_TZ).date()
    all_active = [v for v in vehicles if v.get("active", True)]
    visible = [v for v in vehicles
               if branch_id is None or v.get("branch_id") == branch_id]
    pool = [v for v in visible if v.get("active", True)]
    pool_ids = {v["id"] for v in pool}
    capacity = round(sum(float(v["capacity_spaces"]) for v in pool), 2)
    vnames = {v["id"]: v["name"] for v in vehicles}

    all_rows = [b for b in bookings
                if b.get("spaces") is not None
                and b.get("status", "confirmed") in ACTIVE_STATUSES]
    rows = (all_rows if branch_id is None
            else [b for b in all_rows if b.get("vehicle_id") in pool_ids])

    # ── dispatch strip: the next working days as a daysheet ──────────────────
    dispatch = []
    for day in _working_days(c, today, DISPATCH_DAYS):
        todays = [b for b in rows if _as_date(b["booking_date"]) == day]
        loads = _day_loads(pool, todays, day)
        used = round(sum(l["used_spaces"] for l in loads), 2)
        dispatch.append({
            "date": day.isoformat(),
            "label": day.strftime("%a %d %b"),
            "today": day == today,
            "used": used, "total": capacity,
            "pct": min(100, round(100 * used / capacity)) if capacity else 0,
            "full": capacity > 0 and used >= capacity - 1e-9,
            "jobs": len(todays),
            "vehicles": [{
                "name": l["vehicle"]["name"],
                "used": l["used_spaces"],
                "capacity": float(l["vehicle"]["capacity_spaces"]),
                "pct": (min(100, round(100 * l["used_spaces"]
                                       / float(l["vehicle"]["capacity_spaces"])))
                        if float(l["vehicle"]["capacity_spaces"]) else 0),
            } for l in loads[:_DISPATCH_BARS]],
            "more_vehicles": max(0, len(loads) - _DISPATCH_BARS),
        })

    # ── KPIs (honest, computed from the same rows the board shows) ───────────
    week = _working_days(c, today, WEEK_DAYS)
    week_set = {d for d in week}
    week_rows = [b for b in rows if _as_date(b["booking_date"]) in week_set]
    week_spaces = round(sum(float(b["spaces"]) for b in week_rows), 2)
    week_capacity = round(capacity * len(week), 2)

    # ── per-depot tiles (the enterprise overview — all-branches view only) ───
    depots = []
    if branch_id is None and len(branches) > 1:
        for br in branches:
            bpool = [v for v in all_active if v.get("branch_id") == br["id"]]
            bids = {v["id"] for v in bpool}
            bcap = round(sum(float(v["capacity_spaces"]) for v in bpool), 2)
            bweek_cap = round(bcap * len(week), 2)
            bweek_spaces = round(sum(
                float(b["spaces"]) for b in week_rows
                if b.get("vehicle_id") in bids), 2)
            depots.append({
                "id": br["id"], "name": br.get("name") or "Branch",
                "postcode": br.get("postcode"),
                "vehicles": len(bpool),
                "capacity": bcap,
                "week_pct": (min(100, round(100 * bweek_spaces / bweek_cap))
                             if bweek_cap else 0),
                "week_spaces": bweek_spaces,
                "jobs": sum(1 for b in rows if b.get("vehicle_id") in bids),
            })
    by_source: dict = {}
    for b in rows:
        key = _SOURCE_LABELS.get(b.get("source") or "manual", "Added by you")
        by_source[key] = by_source.get(key, 0) + 1
    proposed = [b for b in rows if b.get("status") == "proposed"]
    kpis = {
        "week_pct": (min(100, round(100 * week_spaces / week_capacity))
                     if week_capacity else 0),
        "week_spaces": week_spaces, "week_capacity": week_capacity,
        "week_days": len(week),
        "on_books_jobs": len(rows),
        "on_books_spaces": round(sum(float(b["spaces"]) for b in rows), 2),
        "by_source": sorted(by_source.items(), key=lambda kv: -kv[1]),
        "awaiting": len(proposed),
        "plans_pending": len(plans),
    }

    # ── exceptions: what a transport office chases ───────────────────────────
    def _chip(b):
        return {"ref": b.get("booking_ref") or (b.get("id") or "")[:8],
                "date": str(b.get("booking_date"))[:10],
                "spaces": fmt_spaces(b.get("spaces") or 0),
                "summary": (b.get("summary") or "")[:60],
                "vehicle": vnames.get(b.get("vehicle_id")) or "—"}

    unassigned = [b for b in rows if not b.get("vehicle_id")]
    horizon_pc = today + timedelta(days=POSTCODE_LOOKAHEAD)
    no_postcode = [b for b in rows
                   if b.get("vehicle_id") and not b.get("delivery_postcode")
                   and today <= _as_date(b["booking_date"]) <= horizon_pc] if routing_on else []
    exceptions = {
        "unassigned": [_chip(b) for b in unassigned[:6]],
        "unassigned_n": len(unassigned),
        "awaiting": [_chip(b) for b in proposed[:6]],
        "awaiting_n": len(proposed),
        "no_postcode": [_chip(b) for b in no_postcode[:6]],
        "no_postcode_n": len(no_postcode),
    }

    # ── planner: nudges + pending proposals (routing only) ───────────────────
    pending_plans = [{
        "id": p["id"], "date": str(p["plan_date"])[:10],
        "label": _as_date(p["plan_date"]).strftime("%A %d %B"),
        "summary": p.get("summary"),
    } for p in plans]
    pending_dates = {p["date"] for p in pending_plans}
    # A day with a plan already on the table gets the plan card, not a nudge
    # to replan it again. Nudges respect the branch lens (consolidate only
    # ever moves within a branch anyway, so the filtered view stays honest).
    review = ([r for r in planner_mod.review_days(visible, bookings, today,
                                                  c["horizon_days"])
               if r["date"] not in pending_dates]
              if routing_on else [])

    # ── order-intake feed: newest first, straight off the diary ──────────────
    feed = [{
        **_chip(b),
        "source": _SOURCE_LABELS.get(b.get("source") or "manual", "Added by you"),
        "status": b.get("status", "confirmed"),
        "agent": (b.get("source") or "") == "email",
    } for b in sorted(rows, key=lambda b: str(b.get("created_at") or ""),
                      reverse=True)[:FEED_ROWS]]

    return {
        "summary": fleet_diary_line(cfg, vehicles, branches),
        "today_label": today.strftime("%A %d %B %Y"),
        "dispatch": dispatch,
        "kpis": kpis,
        "depots": depots,
        "branches": branches,
        "branch_id": branch_id,
        "branch_name": next((b.get("name") for b in branches
                             if b["id"] == branch_id), None),
        "exceptions": exceptions,
        "review_days": review,
        "pending_plans": pending_plans,
        "feed": feed,
        "scheduling_on": scheduling_on,
        "routing_on": routing_on,
    }


def view(tenant_id: str, client=None, now: datetime | None = None,
         scheduling_on: bool = False, routing_on: bool = False,
         branch_id: str | None = None) -> dict:
    """Fetch + assemble for the route (same shape as scheduling.fleet_board).
    A branch id that isn't the tenant's own quietly falls back to all-branches,
    like the board."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    cfg = db.get_scheduling_config(tenant_id, client=client)
    vehicles = db.list_vehicles(tenant_id, client=client)
    branches = db.list_branches(tenant_id, client=client)
    if branch_id and not any(b["id"] == branch_id for b in branches):
        branch_id = None
    bookings = db.list_bookings(tenant_id, now.date().isoformat(), client=client)
    plans = (db.list_plans(tenant_id, date_from_iso=now.date().isoformat(),
                           status="proposed", client=client)
             if routing_on else [])
    return overview(cfg, vehicles, branches, bookings, plans, now,
                    scheduling_on=scheduling_on, routing_on=routing_on,
                    branch_id=branch_id)
