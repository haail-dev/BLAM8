"""Wholesale OS — credit gate + view composition (docs/os/wholesale.md §6c, §2).

The credit gate is ARITHMETIC, not an LLM judgment: the model never decides
whether to refuse an order, it only phrases a refusal the deterministic check
already mandated (§12b). No limit set → the gate never blocks and the drafter
behaves exactly as today — a wholesaler who doesn't run credit terms is
completely unaffected.

Pure helpers here; DB reads live in src/app/db.py wholesale_*; the engine loops
live in src/app/wholesale_standing.py and src/app/wholesale_plan.py.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from app import db

log = logging.getLogger(__name__)

# Advance ladder (§3). Side states (held/skipped/on_hold_short) re-enter at
# 'order' when released; 'complete' is terminal.
STATUS_LADDER = ("quote", "order", "allocation", "fulfilment", "invoice",
                 "payment", "complete")


def next_status(current: str | None) -> str | None:
    """The next rung on the §3 ladder, or None when terminal/unknown. Side
    states (held / skipped / on_hold_short) resume at 'order'."""
    if current in ("held", "on_hold_short"):
        return "order"
    if current == "skipped":
        return None
    try:
        idx = STATUS_LADDER.index(current or "quote")
    except ValueError:
        return None
    return STATUS_LADDER[idx + 1] if idx + 1 < len(STATUS_LADDER) else None


def check_credit(tenant_id: str, company_id: str, order_value: float,
                 client=None) -> dict:
    """The §12b gate. Projects this order onto the buyer's live exposure.
    {'over': bool, 'headroom', 'projected', + wholesale_credit_exposure keys}.
    No credit_limit on the buyer → never blocks (over=False, headroom=None)."""
    exp = db.wholesale_credit_exposure(tenant_id, company_id, client=client)
    if exp["limit"] is None:
        return {**exp, "over": False, "projected": None}
    projected = round(exp["exposure"] + (order_value or 0), 2)
    # exp carries its own 'over' (exposure vs limit, without this order) —
    # the gate's 'over' is the PROJECTION, so it must win the merge.
    return {**exp, "over": projected > exp["limit"], "projected": projected,
            "headroom": round(exp["limit"] - exp["exposure"], 2)}


def credit_gate_block(gate: dict, order_value: float | None) -> str:
    """The §12b credit-gate block appended to crm_context when the buyer's
    account can't take more. Two wordings: a KNOWN new-order value that would
    breach the limit, or (order_value falsy) a buyer who is ALREADY over —
    any order this email places must not be confirmed. The figures are the
    gate's — the model must never invent beyond them."""
    header = (
        "## Credit gate — HARD RULE\n"
        f"This buyer is on account. Current exposure £{gate['exposure']:.2f} against a "
        f"£{gate['limit']:.2f} limit (£{gate['unpaid_invoices']:.2f} unpaid invoices, "
        f"£{gate['open_orders']:.2f} in open orders). ")
    if order_value:
        overage = round((gate.get("projected") or 0) - (gate.get("limit") or 0), 2)
        situation = (f"This new order (~£{order_value:.2f}) would take them to "
                     f"£{gate['projected']:.2f} — OVER their limit by £{overage:.2f}.\n\n"
                     "Do NOT confirm or accept this order. ")
    else:
        overage = round(gate["exposure"] - gate["limit"], 2)
        situation = (f"They are ALREADY OVER their limit by £{overage:.2f}.\n\n"
                     "If this email places or grows an order, do NOT confirm or accept it. ")
    return (
        header + situation +
        "Instead, warmly explain we can't release new orders until the account is brought "
        "back within terms, name the amount outstanding, and offer the way forward (settle "
        "the overdue balance" +
        (f", or a part-order within the £{max(gate.get('headroom') or 0, 0):.2f} of headroom"
         if (gate.get("headroom") or 0) > 0 else "") +
        "). Keep it courteous — this is a valued account, not a debt-collection notice. "
        "Never invent figures beyond those above."
    )


def inbound_credit_context(tenant_id: str, from_address: str | None,
                           client=None) -> str:
    """The pipeline hook (§6c): when a WHOLESALE tenant's inbound sender belongs
    to a buyer company that is over its credit limit, return the HARD-RULE block
    to append to crm_context; otherwise "". Deterministic — the model only
    phrases what the arithmetic mandated. Callers treat any exception as ""
    (best-effort, never costs the draft)."""
    if not from_address or "@" not in from_address:
        return ""
    contact = db.crm_contact_by_email(tenant_id, from_address)
    company_id = (contact or {}).get("company_id")
    if not company_id:
        return ""
    gate = check_credit(tenant_id, company_id, 0.0, client=client)
    if not gate["over"]:
        return ""
    return credit_gate_block(gate, None)


def order_total(lines: list[dict]) -> float:
    total = 0.0
    for ln in lines or []:
        try:
            total += float(ln.get("quantity") or 0) * float(ln.get("unit_price") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def confirmation_facts(order: dict, lines: list[dict], buyer_name: str | None) -> str:
    """Pure facts block for the drafter's order confirmation (§6a) — buyer, ship
    date, lines, total. The drafter phrases; these are the only figures it has."""
    rows = [f"- {ln['description']}: {_qty(ln)}{(' @ £%.2f' % float(ln['unit_price'])) if ln.get('unit_price') not in (None, '') else ''}"
            for ln in lines or []]
    total = order_total(lines)
    return (
        f"Order confirmation facts:\n"
        f"Buyer: {buyer_name or 'the customer'}\n"
        f"Ship / required-by date: {order.get('required_by') or 'to be confirmed'}\n"
        f"Lines:\n" + ("\n".join(rows) if rows else "- (no lines)") + "\n"
        f"Order total: £{total:.2f}"
    )


def _qty(ln: dict) -> str:
    q = ln.get("quantity")
    try:
        qf = float(q)
        q = int(qf) if qf == int(qf) else qf
    except (TypeError, ValueError):
        pass
    return f"{q} {ln.get('unit') or ''}".strip()


# ── The order-book / home view composition (§2 Card scaffolding for W1) ──────

def order_book(tenant_id: str, client=None, status: str | None = None) -> dict:
    """The order book page feed: orders grouped by wholesale_status column, with
    lines + totals attached. Statuses render in ladder order, side states last."""
    orders = db.wholesale_list_orders(tenant_id, status=status, client=client)
    lines_by_deal = db.wholesale_lines_for_deals(
        tenant_id, [o["id"] for o in orders], client=client)
    companies = {c["id"]: c for c in db.wholesale_order_companies(tenant_id, client=client)}
    for o in orders:
        o["lines"] = lines_by_deal.get(o["id"], [])
        o["total"] = order_total(o["lines"]) or float(o.get("value_estimate") or 0)
        comp = companies.get(o.get("company_id")) or {}
        o["buyer"] = comp.get("name") or "—"
    columns = []
    for st in STATUS_LADDER + ("held", "on_hold_short", "skipped"):
        bucket = [o for o in orders if (o.get("wholesale_status") or "quote") == st]
        if bucket or st in ("quote", "order", "allocation", "fulfilment"):
            columns.append({"status": st, "orders": bucket,
                            "total": round(sum(o["total"] for o in bucket), 2)})
    return {"columns": columns, "count": len(orders)}


def week_span(today: date | None = None) -> tuple[str, str]:
    """Monday..Sunday ISO dates for the current week — the this-week feeds."""
    t = today or date.today()
    monday = t - timedelta(days=t.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def home_view(tenant_id: str, client=None) -> dict:
    """The /app/wholesale home (§2). W1 renders the orders-due card with real
    data; the standing-orders and credit cards light up as W2/W3 land — each
    section is best-effort so a partial build never 500s the page."""
    start, end = week_span()
    view: dict = {"week_start": start, "week_end": end,
                  "due_orders": [], "credit_risk": [], "standing_due": []}
    try:
        due = db.wholesale_list_orders(tenant_id, due_before=end, client=client)
        due = [o for o in due
               if (o.get("wholesale_status") or "quote") in
               ("quote", "order", "allocation", "fulfilment", "held", "on_hold_short")]
        lines_by_deal = db.wholesale_lines_for_deals(
            tenant_id, [o["id"] for o in due], client=client)
        companies = {c["id"]: c for c in db.wholesale_order_companies(tenant_id, client=client)}
        for o in due:
            o["lines"] = lines_by_deal.get(o["id"], [])
            o["total"] = order_total(o["lines"]) or float(o.get("value_estimate") or 0)
            o["buyer"] = (companies.get(o.get("company_id")) or {}).get("name") or "—"
        view["due_orders"] = due
    except Exception:
        log.warning("wholesale home: due-orders card failed", exc_info=True)
    try:
        view["credit_risk"] = credit_risk_rows(tenant_id, client=client)
    except Exception:
        log.warning("wholesale home: credit card failed", exc_info=True)
    try:
        sos = db.wholesale_list_standing_orders(tenant_id, status="active", client=client)
        view["standing_due"] = [so for so in sos
                                if so.get("next_run_on") and so["next_run_on"] <= end]
    except Exception:
        log.warning("wholesale home: standing card failed", exc_info=True)
    return view


def credit_risk_rows(tenant_id: str, client=None) -> list[dict]:
    """Buyers at/over their limit, worst-first (§6b credit_risk). Only companies
    with a credit_limit set participate — no limit, no stance."""
    rows = []
    for comp in db.wholesale_order_companies(tenant_id, client=client):
        raw = (comp.get("custom_data") or {}).get("credit_limit")
        if raw in (None, ""):
            continue
        exp = db.wholesale_credit_exposure(tenant_id, comp["id"], client=client)
        if exp["limit"] is None:
            continue
        rows.append({"company_id": comp["id"], "name": comp.get("name") or "—", **exp,
                     "stance": "will hold new orders" if exp["over"] else "ok"})
    rows.sort(key=lambda r: (r["headroom"] if r["headroom"] is not None else 0))
    return rows
