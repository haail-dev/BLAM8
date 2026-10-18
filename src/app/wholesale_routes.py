"""Wholesale OS routes (docs/os/wholesale.md §2, §7, §8) — Phase W1/W2.

The OS home (three daily-rhythm cards), the order book + order detail (lines,
credit stance, status ladder), and the credit ledger. Standing-order surfaces
land with the W3 engine.

Every surface gates on _require_wholesale (db.module_ui_visible(tenant_id,
"wholesale") → 404 if off), the twin of assets_routes._require_assets. Writes
carry CSRF and are IDOR-safe (every read/write is tenant-scoped).
"""
import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import db, wholesale
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.security import require_csrf
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/wholesale", tags=["wholesale"])
api_router = APIRouter(prefix="/api/wholesale", tags=["wholesale"])


def _require_wholesale(ctx: TenantCtx) -> None:
    """One subscriptions row (module='wholesale') lights every wholesale surface."""
    if not db.module_ui_visible(ctx.tenant_id, "wholesale", client=ctx.client):
        raise HTTPException(status_code=404,
                            detail="Wholesale OS is not active for this account")


def _order_or_404(ctx: TenantCtx, deal_id: str) -> dict:
    order = db.wholesale_get_order(ctx.tenant_id, deal_id, client=ctx.client)
    if not order:
        raise HTTPException(status_code=404, detail="No such order")
    return order


# ── pages ─────────────────────────────────────────────────────────────────────

@router.get("")
def wholesale_home(request: Request, msg: str = "",
                   ctx: TenantCtx = Depends(require_onboarded)):
    """The OS home (§2): standing orders due · credit-limit risk · orders due
    this week. Cards are best-effort — a partial build never 500s the page."""
    _require_wholesale(ctx)
    view = wholesale.home_view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/wholesale.html",
                                      _ctx("wholesale", ctx, view=view, msg=msg))


@router.get("/orders")
def wholesale_orders(request: Request, status: str = "", msg: str = "",
                     ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    book = wholesale.order_book(ctx.tenant_id, client=ctx.client,
                                status=(status or None))
    companies = db.wholesale_order_companies(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/wholesale_orders.html",
                                      _ctx("wholesale", ctx, book=book, msg=msg,
                                           companies=companies, status_filter=status))


@router.get("/orders/{deal_id}")
def wholesale_order_detail(request: Request, deal_id: str, msg: str = "",
                           ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    order = _order_or_404(ctx, deal_id)
    lines = db.wholesale_list_lines(ctx.tenant_id, deal_id, client=ctx.client)
    order["total"] = wholesale.order_total(lines) or float(order.get("value_estimate") or 0)
    companies = {c["id"]: c for c in
                 db.wholesale_order_companies(ctx.tenant_id, client=ctx.client)}
    buyer = companies.get(order.get("company_id")) or {}
    gate = None
    if order.get("company_id"):
        try:
            gate = wholesale.check_credit(ctx.tenant_id, order["company_id"],
                                          order["total"], client=ctx.client)
        except Exception:
            log.warning("credit stance failed for order %s", deal_id, exc_info=True)
    return templates.TemplateResponse(
        request, "app/wholesale_order.html",
        _ctx("wholesale", ctx, order=order, lines=lines, buyer=buyer, gate=gate,
             next_status=wholesale.next_status(order.get("wholesale_status")), msg=msg))


@router.get("/credit")
def wholesale_credit(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The credit ledger (§8): every buyer with a limit, exposure vs limit,
    worst-first — with the honest §14 caveat that exposure counts only the
    invoices BLAM8 has raised."""
    _require_wholesale(ctx)
    rows = wholesale.credit_risk_rows(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/wholesale_credit.html",
                                      _ctx("wholesale", ctx, rows=rows))


# ── page-form writes (CSRF) ───────────────────────────────────────────────────

@router.post("/orders", dependencies=[Depends(require_csrf)])
async def create_order_form(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """New order from the order-book form: buyer + up to 8 line rows."""
    _require_wholesale(ctx)
    form = await request.form()
    company_id = (form.get("company_id") or "").strip()
    if not company_id:
        return RedirectResponse("/app/wholesale/orders?msg=nobuyer", status_code=303)
    lines = []
    for i in range(8):
        desc = (form.get(f"line_desc_{i}") or "").strip()
        if not desc:
            continue
        lines.append({"description": desc,
                      "quantity": form.get(f"line_qty_{i}") or 0,
                      "unit": (form.get(f"line_unit_{i}") or "").strip() or None,
                      "unit_price": form.get(f"line_price_{i}") or None})
    if not lines:
        return RedirectResponse("/app/wholesale/orders?msg=nolines", status_code=303)
    title = (form.get("title") or "").strip() or "Wholesale order"
    order = db.wholesale_create_order(
        ctx.tenant_id, company_id=company_id,
        contact_id=(form.get("contact_id") or "").strip() or None,
        title=title, required_by=(form.get("required_by") or "").strip() or None,
        lines=lines, status="quote")
    db.log_agent_event(ctx.tenant_id, "wholesale",
                       f"Order created: {title} ({len(lines)} lines)")
    return RedirectResponse(f"/app/wholesale/orders/{order['id']}?msg=created",
                            status_code=303)


@router.post("/orders/{deal_id}/advance", dependencies=[Depends(require_csrf)])
def advance_order_form(deal_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Move wholesale_status one rung up the §3 ladder (held/short resume at
    'order'). The ladder is fixed — no free-form status from the form."""
    _require_wholesale(ctx)
    order = _order_or_404(ctx, deal_id)
    nxt = wholesale.next_status(order.get("wholesale_status"))
    if not nxt:
        return RedirectResponse(f"/app/wholesale/orders/{deal_id}?msg=terminal",
                                status_code=303)
    db.wholesale_set_status(ctx.tenant_id, deal_id, nxt)
    db.log_agent_event(ctx.tenant_id, "wholesale",
                       f"Order advanced to {nxt}", detail={"deal_id": deal_id})
    return RedirectResponse(f"/app/wholesale/orders/{deal_id}?msg=advanced",
                            status_code=303)


@router.post("/orders/{deal_id}/hold", dependencies=[Depends(require_csrf)])
def hold_order_form(deal_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    _order_or_404(ctx, deal_id)
    db.wholesale_set_status(ctx.tenant_id, deal_id, "held")
    return RedirectResponse(f"/app/wholesale/orders/{deal_id}?msg=held", status_code=303)


@router.post("/orders/{deal_id}/allocate", dependencies=[Depends(require_csrf)])
async def allocate_form(request: Request, deal_id: str,
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Set allocated_qty per line from the detail form; short lines flag
    on_hold_short automatically (partial allocation carries the balance)."""
    _require_wholesale(ctx)
    _order_or_404(ctx, deal_id)
    form = await request.form()
    lines = db.wholesale_list_lines(ctx.tenant_id, deal_id, client=ctx.client)
    short = False
    for ln in lines:
        raw = form.get(f"alloc_{ln['id']}")
        if raw in (None, ""):
            continue
        try:
            alloc = max(float(raw), 0.0)
        except ValueError:
            continue
        db.wholesale_set_allocation(ctx.tenant_id, ln["id"], alloc)
        if alloc < float(ln.get("quantity") or 0):
            short = True
    db.wholesale_set_status(ctx.tenant_id, deal_id,
                            "on_hold_short" if short else "allocation")
    return RedirectResponse(f"/app/wholesale/orders/{deal_id}?msg=allocated",
                            status_code=303)


# ── JSON API (§7) ─────────────────────────────────────────────────────────────

@api_router.get("/orders")
def api_list_orders(status: str = "", due_before: str = "", company_id: str = "",
                    ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    orders = db.wholesale_list_orders(
        ctx.tenant_id, status=status or None, due_before=due_before or None,
        company_id=company_id or None, client=ctx.client)
    return JSONResponse({"orders": orders})


@api_router.post("/orders", dependencies=[Depends(require_csrf)])
async def api_create_order(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    company_id = (body.get("company_id") or "").strip()
    lines = body.get("lines") or []
    if not company_id or not isinstance(lines, list) or not lines:
        raise HTTPException(status_code=400, detail="company_id and lines are required")
    order = db.wholesale_create_order(
        ctx.tenant_id, company_id=company_id,
        contact_id=(body.get("contact_id") or None),
        title=(body.get("title") or "Wholesale order"),
        required_by=(body.get("required_by") or None),
        lines=lines, status=(body.get("status") or "quote"))
    return JSONResponse({"order": order}, status_code=201)


@api_router.get("/orders/{deal_id}")
def api_order_detail(deal_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    order = _order_or_404(ctx, deal_id)
    lines = db.wholesale_list_lines(ctx.tenant_id, deal_id, client=ctx.client)
    gate = None
    if order.get("company_id"):
        gate = wholesale.check_credit(
            ctx.tenant_id, order["company_id"],
            wholesale.order_total(lines) or float(order.get("value_estimate") or 0),
            client=ctx.client)
    return JSONResponse({"order": order, "lines": lines, "credit": gate})


@api_router.get("/credit")
def api_credit(ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    return JSONResponse({"buyers": wholesale.credit_risk_rows(ctx.tenant_id,
                                                              client=ctx.client)})


@api_router.get("/credit/{company_id}")
def api_credit_one(company_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    return JSONResponse(db.wholesale_credit_exposure(ctx.tenant_id, company_id,
                                                     client=ctx.client))


# ── standing orders (§7/§8 — the W3 surfaces) ─────────────────────────────────

@router.get("/standing")
def standing_list(request: Request, msg: str = "",
                  ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    sos = db.wholesale_list_standing_orders(ctx.tenant_id, client=ctx.client)
    companies = db.wholesale_order_companies(ctx.tenant_id, client=ctx.client)
    names = {c["id"]: (c.get("name") or "—") for c in companies}
    for so in sos:
        so["buyer"] = names.get(so.get("company_id"), "—")
        so["line_count"] = len(so.get("line_template") or [])
    return templates.TemplateResponse(request, "app/wholesale_standing.html",
                                      _ctx("wholesale", ctx, standing=sos,
                                           companies=companies, msg=msg))


@router.post("/standing", dependencies=[Depends(require_csrf)])
async def standing_create(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The builder: buyer + cadence + up to 8 template lines. next_run_on seeds
    from the first matching cycle date so the engine picks it up naturally."""
    _require_wholesale(ctx)
    from app import wholesale_standing
    from datetime import date
    form = await request.form()
    company_id = (form.get("company_id") or "").strip()
    cadence = (form.get("cadence") or "weekly").strip().lower()
    if cadence not in ("weekly", "fortnightly", "monthly"):
        cadence = "weekly"
    days = [int(d) for d in form.getlist("days_of_week") if str(d).isdigit() and 0 <= int(d) <= 6]
    day_of_month = form.get("day_of_month")
    lines = []
    for i in range(8):
        desc = (form.get(f"line_desc_{i}") or "").strip()
        if not desc:
            continue
        lines.append({"description": desc,
                      "quantity": float(form.get(f"line_qty_{i}") or 0),
                      "unit": (form.get(f"line_unit_{i}") or "").strip() or None,
                      "unit_price": (form.get(f"line_price_{i}") or None)})
    if not company_id or not lines or (cadence != "monthly" and not days):
        return RedirectResponse("/app/wholesale/standing?msg=incomplete", status_code=303)
    so_fields = {
        "company_id": company_id,
        "contact_id": (form.get("contact_id") or "").strip() or None,
        "name": (form.get("name") or "").strip() or None,
        "cadence": cadence, "days_of_week": days,
        "day_of_month": int(day_of_month) if (day_of_month or "").isdigit() else None,
        "lead_days": int(form.get("lead_days") or 1),
        "line_template": lines,
        "auto_confirm": form.get("auto_confirm") == "on",
        "status": "active",
    }
    nxt = wholesale_standing.next_cycle_date(so_fields, date.today())
    so_fields["next_run_on"] = nxt.isoformat() if nxt else None
    db.wholesale_create_standing_order(ctx.tenant_id, so_fields)
    db.log_agent_event(ctx.tenant_id, "wholesale",
                       f"Standing order created ({cadence}, {len(lines)} lines)")
    return RedirectResponse("/app/wholesale/standing?msg=created", status_code=303)


@router.post("/standing/{so_id}/pause", dependencies=[Depends(require_csrf)])
def standing_pause(so_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    so = db.wholesale_get_standing_order(ctx.tenant_id, so_id, client=ctx.client)
    if not so:
        raise HTTPException(status_code=404, detail="No such standing order")
    flip = "paused" if so.get("status") == "active" else "active"
    db.wholesale_update_standing_order(ctx.tenant_id, so_id, {"status": flip})
    return RedirectResponse("/app/wholesale/standing?msg=updated", status_code=303)


@router.post("/standing/{so_id}/skip", dependencies=[Depends(require_csrf)])
def standing_skip(so_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Skip the next cycle (a cafe on holiday): the cursor jumps one cycle
    without spawning an order."""
    _require_wholesale(ctx)
    from app import wholesale_standing
    from datetime import date
    so = db.wholesale_get_standing_order(ctx.tenant_id, so_id, client=ctx.client)
    if not so:
        raise HTTPException(status_code=404, detail="No such standing order")
    base = so.get("next_run_on")
    try:
        after = date.fromisoformat(str(base)[:10]) if base else date.today()
    except ValueError:
        after = date.today()
    nxt = wholesale_standing.next_cycle_date(so, after)
    db.wholesale_update_standing_order(
        ctx.tenant_id, so_id, {"next_run_on": nxt.isoformat() if nxt else None})
    db.log_agent_event(ctx.tenant_id, "wholesale",
                       f"Standing order cycle skipped ({so.get('name') or so_id})")
    return RedirectResponse("/app/wholesale/standing?msg=skipped", status_code=303)


@router.post("/standing/{so_id}/run", dependencies=[Depends(require_csrf)])
async def standing_run_now(so_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Force-generate this cycle now (§7) — the one-click on the home card."""
    _require_wholesale(ctx)
    from app import wholesale_standing
    so = db.wholesale_get_standing_order(ctx.tenant_id, so_id, client=ctx.client)
    if not so:
        raise HTTPException(status_code=404, detail="No such standing order")
    if so.get("status") != "active":
        return RedirectResponse("/app/wholesale/standing?msg=notactive", status_code=303)
    outcome = await wholesale_standing._spawn_cycle(so)
    return RedirectResponse(f"/app/wholesale/standing?msg={outcome}", status_code=303)


@api_router.get("/standing")
def api_standing(ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    return JSONResponse({"standing": db.wholesale_list_standing_orders(
        ctx.tenant_id, client=ctx.client)})


# ── the this-week fulfilment plan (§6b/§12c — W4) ─────────────────────────────

@router.get("/plan")
def wholesale_plan_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Day columns for the week: every plannable order, with shortage/waitlist
    flags when tenant-config capacity says a line is over-committed."""
    _require_wholesale(ctx)
    from app import wholesale_plan
    plan = wholesale_plan.week_plan(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/wholesale_plan.html",
                                      _ctx("wholesale", ctx, plan=plan))


@api_router.get("/plan")
def api_plan(ctx: TenantCtx = Depends(require_onboarded)):
    _require_wholesale(ctx)
    from app import wholesale_plan
    return JSONResponse({"plan": wholesale_plan.week_plan(ctx.tenant_id,
                                                          client=ctx.client)})
