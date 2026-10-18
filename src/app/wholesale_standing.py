"""Wholesale OS — the standing-order engine (docs/os/wholesale.md §6a, §12a).

Every day at 06:00 UTC (wired in src/app/main.py alongside the reminder sweeps),
each active standing order whose next_run_on has arrived spawns the cycle's
order deal from its line template, runs the §12b credit gate, and either
(a) drafts the confirmation for the owner's queue (default), (b) auto-confirms
a trusted account (auto_confirm + tenant auto_send — with the Art 50
disclosure footer), or (c) HOLDS the order and drafts the credit conversation
instead — a hold never auto-sends.

Service-role sweep across all tenants, scoping per row.tenant_id with per-item
isolation — the exact crm_sequences / send_service_reminders shape. The cursor
(next_run_on) advances even when the cycle HELD, so a blocked buyer never jams
the sweep; the held order deal itself stays on the board for the owner.

Cycle-date maths are pure (unit-tested in isolation): weekly/fortnightly pick
the next configured weekday after the current cycle; monthly pins a day of
month (clamped to short months).
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from app import agent, db, disclosure, mailbox, usage, wholesale

log = logging.getLogger(__name__)

ORDER_PURPOSE = ("Confirm the customer's regular standing order for the stated delivery "
                 "date, listing the lines and the order total from the facts. Warm, brief "
                 "and certain — this is a routine confirmation of an arrangement they "
                 "already have; do NOT invent products, prices or dates beyond the facts.")

HOLD_PURPOSE = ("Write to this regular customer about their standing order that we cannot "
                "release this cycle because their account is over its credit limit. Follow "
                "the credit-gate facts exactly: name the amount outstanding, stay warm and "
                "courteous (a valued account, not a debt-collection notice), and offer the "
                "way forward. Do NOT confirm the order and do NOT invent figures.")


def next_cycle_date(so: dict, after: date) -> date | None:
    """The next run date strictly after `after` (pure). weekly/fortnightly use
    days_of_week (0=Mon..6=Sun; fortnightly steps the base week); monthly pins
    day_of_month clamped to the month's length. Unknown cadence → None."""
    cadence = (so.get("cadence") or "").lower()
    if cadence in ("weekly", "fortnightly"):
        days = sorted(d for d in (so.get("days_of_week") or []) if 0 <= int(d) <= 6)
        if not days:
            return None
        step = 1 if cadence == "weekly" else 14
        # Weekly: the next configured weekday after `after`. Fortnightly: the next
        # configured weekday at least `step` days out (cycle skips a week).
        horizon = after + timedelta(days=1 if cadence == "weekly" else step)
        for offset in range(0, 22):
            cand = horizon + timedelta(days=offset)
            if cand.weekday() in days:
                return cand
        return None
    if cadence == "monthly":
        dom = int(so.get("day_of_month") or 1)
        year, month = after.year, after.month
        month += 1
        if month > 12:
            month, year = 1, year + 1
        return date(year, month, min(dom, calendar.monthrange(year, month)[1]))
    return None


def _buyer_email(so: dict, tenant_id: str) -> str | None:
    contact = None
    if so.get("contact_id"):
        contact = db.crm_get_contact(so["contact_id"], tenant_id)
    return (contact or {}).get("email")


async def generate_due_standing_orders() -> dict:
    """The 06:00 sweep. Per-standing-order isolation: one bad row never stops
    the rest; a failure leaves that row's cursor untouched for tomorrow."""
    today = date.today().isoformat()
    due = db.wholesale_due_standing_orders(today, limit=500)
    counts: dict = {}
    for so in due:
        try:
            outcome = await _spawn_cycle(so)
        except Exception:
            log.warning("standing order %s failed (tenant %s)", so.get("id"),
                        so.get("tenant_id"), exc_info=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"due": len(due), **counts}


async def _spawn_cycle(so: dict) -> str:
    """Spawn one cycle: order deal from the template → credit gate → confirm or
    hold. Returns 'confirmed' | 'drafted' | 'held' | 'skipped'."""
    tenant_id = so["tenant_id"]
    lines = so.get("line_template") or []
    run_on = str(so.get("next_run_on") or date.today().isoformat())[:10]
    if not lines:
        _advance(so, run_on)
        return "skipped"
    cfg = db.effective_config(tenant_id, None)
    order = db.wholesale_create_order(
        tenant_id, company_id=so["company_id"], contact_id=so.get("contact_id"),
        title=f"{so.get('name') or 'Standing order'} — {run_on}",
        required_by=run_on, lines=lines,
        standing_order_id=so["id"], status="order")
    order_lines = order.get("lines") or []
    total = wholesale.order_total(order_lines) or float(order.get("value_estimate") or 0)

    # The §12b gate runs BEFORE any confirmation leaves. The order's own value is
    # already inside exposure (status='order' counts), so project with 0 extra.
    gate = wholesale.check_credit(tenant_id, so["company_id"], 0.0)
    buyer_name = _company_name(tenant_id, so["company_id"])
    to = _buyer_email(so, tenant_id)

    if gate["over"]:
        db.wholesale_set_status(tenant_id, order["id"], "held")
        await _draft_credit_hold(tenant_id, cfg, to, order, gate, buyer_name)
        _advance(so, run_on)   # the cursor moves on; the held deal stays visible
        return "held"

    outcome = await _draft_order_confirmation(
        tenant_id, cfg, to, order, order_lines, buyer_name,
        auto=bool(so.get("auto_confirm")))
    _advance(so, run_on)
    return outcome


def _advance(so: dict, run_on: str) -> None:
    try:
        after = date.fromisoformat(run_on)
    except ValueError:
        after = date.today()
    nxt = next_cycle_date(so, after)
    db.wholesale_advance_standing_order(
        so["tenant_id"], so["id"], last_run_on=run_on,
        next_run_on=nxt.isoformat() if nxt else None)


def _company_name(tenant_id: str, company_id: str) -> str | None:
    for comp in db.wholesale_order_companies(tenant_id):
        if comp["id"] == company_id:
            return comp.get("name")
    return None


async def _draft_order_confirmation(tenant_id: str, cfg: dict, to: str | None,
                                    order: dict, lines: list[dict],
                                    buyer_name: str | None, auto: bool) -> str:
    """The reminders pattern verbatim: compose → pending email_log → usage →
    optional auto-send (auto_confirm AND tenant auto_send) with the Art 50
    disclosure footer. No buyer email → the draft still queues for the owner
    to route by hand."""
    facts = wholesale.confirmation_facts(order, lines, buyer_name)
    result = await agent.compose_outbound(cfg, ORDER_PURPOSE, facts, tenant_id=tenant_id,
                                          log_operation="wholesale_order")
    subject = f"Your order for {order.get('required_by') or 'the next delivery'}"
    log_id, _ = db.insert_email_log(
        tenant_id, to or "", subject, f"[wholesale standing order] {facts}",
        result.text, status="pending", kind="wholesale_order")
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="wholesale_order", email_log_id=log_id)
    db.log_agent_event(tenant_id, "wholesale",
                       f"Standing-order confirmation queued for {buyer_name or to or 'buyer'}",
                       direction="outbound", contact=to, subject=subject,
                       email_log_id=log_id, event_date=order.get("required_by"))
    if auto and cfg.get("auto_send") and to:
        await mailbox.send_mail(tenant_id, to, subject,
                                disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the standing-order confirmation to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "confirmed"
    return "drafted"


async def _draft_credit_hold(tenant_id: str, cfg: dict, to: str | None,
                             order: dict, gate: dict, buyer_name: str | None) -> None:
    """The held cycle's credit conversation — ALWAYS a pending draft for the
    owner (§12b: a refusal never auto-sends, full-auto or not)."""
    facts = (f"Buyer: {buyer_name or 'the customer'}\n"
             + wholesale.credit_gate_block(gate, None)
             + f"\n\nThe held order: {order.get('title')} "
               f"(£{float(order.get('value_estimate') or 0):.2f}, "
               f"required by {order.get('required_by')})")
    result = await agent.compose_outbound(cfg, HOLD_PURPOSE, facts, tenant_id=tenant_id,
                                          log_operation="wholesale_hold")
    subject = "About your standing order"
    log_id, _ = db.insert_email_log(
        tenant_id, to or "", subject, f"[wholesale credit hold] {facts}",
        result.text, status="pending", kind="wholesale_hold")
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="wholesale_hold", email_log_id=log_id)
    db.log_agent_event(tenant_id, "wholesale",
                       f"Standing order HELD (over credit limit) — refusal drafted for "
                       f"{buyer_name or to or 'buyer'}, waiting for you",
                       direction="outbound", contact=to, subject=subject,
                       email_log_id=log_id, event_date=order.get("required_by"))


def seed_examples(tenant_id: str) -> int:
    """Onboarding opt-in (§9): a couple of paused example templates so the
    standing-orders surface isn't empty on day one. Never auto-runs — they're
    created 'paused' with no next_run_on."""
    made = 0
    companies = db.wholesale_order_companies(tenant_id)
    example = companies[0]["id"] if companies else None
    if not example:
        return 0
    for name, days in (("Tuesday book — example", [1]), ("Friday book — example", [4])):
        db.wholesale_create_standing_order(tenant_id, {
            "company_id": example, "name": name, "cadence": "weekly",
            "days_of_week": days, "lead_days": 1, "line_template": [],
            "status": "paused"})
        made += 1
    return made
