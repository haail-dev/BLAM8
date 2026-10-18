"""Wholesale OS — fulfilment planning (docs/os/wholesale.md §6b, §12c).

Pure functions the home page's Card 3 and /app/wholesale/plan both read:
build_week_plan aggregates ordered qty per line-description per ship-day and
flags SHORTAGES against allocatable capacity, resolving them worst-case-first
by allocation_priority (buyer default in crm_companies.custom_data, overridable
per order via crm_deals.allocation_priority — 1 is highest).

Capacity is optional (v1: a per-line weekly number in tenant config; a
warehouse integration feeds it later). Absent capacity means no shortage
flags — graceful, never blocking (§6b).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app import db, wholesale

log = logging.getLogger(__name__)

# Statuses that consume capacity this week (a held/skipped order doesn't ship).
_PLANNABLE = ("order", "allocation", "fulfilment", "on_hold_short")


def build_week_plan(orders: list[dict], lines_by_deal: dict,
                    capacity: dict | None,
                    priority_by_company: dict | None = None) -> list[dict]:
    """Pure. Orders due this week grouped by ship date; per day, aggregate
    ordered qty per line description (case-insensitive); flag SHORT lines
    (ordered > allocatable) and resolve by allocation_priority — priority-1
    buyers filled first, the balance waitlisted. No capacity entry for a line
    → no flag for it. Returns [{date, orders, shortages, waitlist}]."""
    capacity = {k.strip().lower(): v for k, v in (capacity or {}).items()}
    priority_by_company = priority_by_company or {}
    by_day: dict = {}
    for o in orders:
        if (o.get("wholesale_status") or "quote") not in _PLANNABLE:
            continue
        day = str(o.get("required_by") or "")[:10]
        if not day:
            continue
        by_day.setdefault(day, []).append(o)

    plan = []
    for day in sorted(by_day):
        day_orders = by_day[day]
        # aggregate demand per line description
        demand: dict = {}
        for o in day_orders:
            for ln in lines_by_deal.get(o["id"], []):
                key = (ln.get("description") or "").strip().lower()
                if not key:
                    continue
                try:
                    qty = float(ln.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                demand.setdefault(key, {"label": ln.get("description"), "ordered": 0.0,
                                        "orders": []})
                demand[key]["ordered"] += qty
                demand[key]["orders"].append({"order": o, "qty": qty})
        shortages, waitlist = [], []
        for key, agg in demand.items():
            cap = capacity.get(key)
            if cap in (None, ""):
                continue
            try:
                cap = float(cap)
            except (TypeError, ValueError):
                continue
            if agg["ordered"] <= cap:
                continue
            # SHORT: fill by priority (order override, else buyer default, else 9).
            entries = sorted(
                agg["orders"],
                key=lambda e: int(e["order"].get("allocation_priority")
                                  or priority_by_company.get(e["order"].get("company_id"), 9)))
            remaining = cap
            filled, waiting = [], []
            for e in entries:
                take = min(e["qty"], max(remaining, 0.0))
                remaining -= take
                (filled if take >= e["qty"] else waiting).append(
                    {"title": e["order"].get("title"), "deal_id": e["order"]["id"],
                     "ordered": e["qty"], "allocatable": round(take, 2)})
            shortages.append({"line": agg["label"], "ordered": round(agg["ordered"], 2),
                              "allocatable": cap,
                              "short_by": round(agg["ordered"] - cap, 2)})
            waitlist.extend(waiting)
        plan.append({"date": day, "orders": day_orders,
                     "shortages": shortages, "waitlist": waitlist})
    return plan


def week_plan(tenant_id: str, client=None, today: date | None = None) -> list[dict]:
    """The composed this-week plan: orders due Mon..Sun, lines, tenant-config
    capacity ({'weekly_capacity': {'sourdough 800g': 240}}), buyer priorities."""
    start, end = wholesale.week_span(today)
    orders = db.wholesale_list_orders(tenant_id, due_after=start, due_before=end,
                                      client=client)
    lines_by_deal = db.wholesale_lines_for_deals(
        tenant_id, [o["id"] for o in orders], client=client)
    companies = db.wholesale_order_companies(tenant_id, client=client)
    names, priorities = {}, {}
    for comp in companies:
        names[comp["id"]] = comp.get("name") or "—"
        raw = (comp.get("custom_data") or {}).get("allocation_priority")
        try:
            priorities[comp["id"]] = int(str(raw).strip())
        except (TypeError, ValueError):
            pass
    for o in orders:
        o["buyer"] = names.get(o.get("company_id"), "—")
        o["total"] = wholesale.order_total(lines_by_deal.get(o["id"], [])) \
            or float(o.get("value_estimate") or 0)
    cfg = db.effective_config(tenant_id, None)
    capacity = cfg.get("weekly_capacity") if isinstance(cfg.get("weekly_capacity"), dict) else {}
    return build_week_plan(orders, lines_by_deal, capacity, priorities)
