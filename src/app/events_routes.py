"""Events OS routes (docs/os/events.md §2, §7, §8) — Phase A.

The daily board (THIS WEEK's events + a NEXT 4 WEEKS summary) and the event detail
(the deal + event fields + guest list + dietary matrix + stakeholder list). An event
IS a crm_deals row (1:1) enriched, so stage moves / promises / activity all live on
the EXISTING /app/crm surfaces — Events OS deliberately doesn't re-implement them.

Every surface gates on _require_events (db.module_ui_visible(tenant_id, "events") → 404
if off), the twin of dashboard._require_tms/_require_assets. Writes carry CSRF and are
IDOR-safe (every read is tenant-scoped). The multi-stakeholder chase engine (Phase B),
run-of-show (C) and integrations (D) are DEFERRED — not built here.
"""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import db, events_os
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.security import require_csrf
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/events", tags=["events"])


def _require_events(ctx: TenantCtx) -> None:
    """The gate for every Events OS surface. One subscriptions row (module='events')
    lights the board + detail — the _require_tms/_require_assets twin."""
    if not db.module_ui_visible(ctx.tenant_id, "events", client=ctx.client):
        raise HTTPException(status_code=404, detail="Events OS is not active for this account")


# ── the daily board (§2) ──────────────────────────────────────────────────────
@router.get("")
def events_home(request: Request, msg: str = "",
                ctx: TenantCtx = Depends(require_onboarded)):
    """The Events board (§2): THIS WEEK's events as live rollup cards (timings, dietary
    matrix, supplier/staff status lights, headcount, value) + a NEXT 4 WEEKS summary.
    Composed server-side from crm_events + children — no data entry."""
    _require_events(ctx)
    board = events_os.board_view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/events.html", _ctx(
        "events", ctx, msg=msg, board=board))


# ── event detail (§2, §8) ─────────────────────────────────────────────────────
@router.get("/{event_id}")
def event_detail(request: Request, event_id: str, msg: str = "",
                 ctx: TenantCtx = Depends(require_onboarded)):
    """One event: the backing deal + event fields + guest list + dietary matrix +
    stakeholder list. Stage moves / promises / activity are on the deal's /app/crm
    surfaces (an event IS a deal). IDOR-safe: 404 for another tenant's event."""
    _require_events(ctx)
    data = events_os.detail_view(ctx.tenant_id, event_id, client=ctx.client)
    if data is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return templates.TemplateResponse(request, "app/event_detail.html", _ctx(
        "events", ctx, msg=msg, **data))


# ── edit event fields (§7 PATCH /api/events/{id}, Phase A form-post version) ───
@router.post("/{event_id}/update", dependencies=[Depends(require_csrf)])
def event_update(event_id: str, event_date: str = Form(""), event_type: str = Form(""),
                 guest_count: str = Form(""), drink_licence: str = Form(""),
                 contingency_notes: str = Form(""),
                 ctx: TenantCtx = Depends(require_onboarded)):
    """Edit the event-shaped fields by hand (date/type/headcount/licence/contingency).
    The self-fill hook keeps these current from the inbox; this is the human override."""
    _require_events(ctx)
    if not db.crm_get_event(event_id, ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Event not found")
    updates: dict = {}
    if event_date.strip():
        updates["event_date"] = event_date.strip()[:10]
    if event_type.strip():
        updates["event_type"] = event_type.strip()[:40]
    gc = guest_count.strip()
    if gc:
        try:
            updates["guest_count"] = int(float(gc))
        except ValueError:
            pass
    if drink_licence.strip():
        updates["drink_licence"] = drink_licence.strip()[:40]
    # contingency_notes always writable (blank clears it — a deliberate edit).
    updates["contingency_notes"] = contingency_notes.strip()[:2000] or None
    db.crm_update_event(event_id, ctx.tenant_id, updates, client=ctx.client)
    return RedirectResponse(f"/app/events/{event_id}?msg=saved", status_code=303)


# ── the multi-stakeholder chase (§6.2, §7, §12) ───────────────────────────────
def _stakeholder_ctx(stakeholder_id: str, ctx: TenantCtx) -> dict:
    """Load a stakeholder + its event + deal, tenant-scoped (IDOR-safe: a foreign
    stakeholder resolves to nothing → 404). Returns {stakeholder, event, deal}."""
    rows = db.crm_list_event_stakeholders_by_id(stakeholder_id, ctx.tenant_id,
                                                client=ctx.client)
    if not rows:
        raise HTTPException(status_code=404, detail="Stakeholder not found")
    s = rows[0]
    event = db.crm_get_event(s["event_id"], ctx.tenant_id, client=ctx.client)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    deal = db.crm_get_deal(event["deal_id"], ctx.tenant_id, client=ctx.client) or {}
    return {"stakeholder": s, "event": event, "deal": deal}


@router.post("/stakeholders/{stakeholder_id}/chase",
             dependencies=[Depends(require_csrf)])
async def stakeholder_chase_now(stakeholder_id: str,
                                ctx: TenantCtx = Depends(require_onboarded)):
    """Chase this stakeholder NOW — the manual trigger of the auto-chase (§7). Same
    compose/queue path as the daily job (events_os.compose_chase), leaving a pending
    draft (draft-first, §14: the manual button is always a draft to glance at). If the
    outstanding item isn't yet linked to a promise, link it first so auto-resolve works."""
    _require_events(ctx)
    data = _stakeholder_ctx(stakeholder_id, ctx)
    s, event, deal = data["stakeholder"], data["event"], data["deal"]
    from datetime import datetime
    from app.scheduling import DIARY_TZ
    now = datetime.now(DIARY_TZ)
    # Ensure the outstanding item is tracked as a they_owe promise (so a reply auto-
    # resolves it + flips the light) — best-effort, mirrors the capture-time link.
    if (s.get("outstanding") or "").strip() and not s.get("outstanding_promise_id"):
        events_os.link_stakeholder_outstanding(
            ctx.tenant_id, stakeholder=s, deal_id=event["deal_id"],
            outstanding=s.get("outstanding"), client=None)
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    try:
        await events_os.compose_chase(ctx.tenant_id, cfg, s, event, deal, now=now,
                                      auto=False)
        msg = "chased"
    except Exception:
        log.warning("chase-now failed for stakeholder %s", stakeholder_id, exc_info=True)
        msg = "chase_failed"
    return RedirectResponse(f"/app/events/{s['event_id']}?msg={msg}", status_code=303)


@router.post("/stakeholders/{stakeholder_id}/mute",
             dependencies=[Depends(require_csrf)])
def stakeholder_mute(stakeholder_id: str, on: str = Form("1"),
                     ctx: TenantCtx = Depends(require_onboarded)):
    """Per-stakeholder don't-chase (§14): mute → confirmation='muted' so the sweep skips
    it; unmute → back to 'pending' so chasing resumes. No migration — reuses the
    confirmation column. IDOR-safe via _stakeholder_ctx."""
    _require_events(ctx)
    data = _stakeholder_ctx(stakeholder_id, ctx)
    s = data["stakeholder"]
    muting = on.strip() not in ("0", "false", "off", "")
    if muting:
        new_conf = "muted"
    else:
        # Un-mute only from a muted state; never clobber a confirmed/declined truth.
        new_conf = "pending" if s.get("confirmation") == "muted" else s.get("confirmation")
    db.crm_update_event_stakeholder(stakeholder_id, ctx.tenant_id,
                                    {"confirmation": new_conf}, client=ctx.client)
    return RedirectResponse(f"/app/events/{s['event_id']}?msg=saved", status_code=303)


# ── run-of-show (§6.3, §7, §8, §14) ───────────────────────────────────────────
def _event_or_404(event_id: str, ctx: TenantCtx) -> dict:
    """Load an event tenant-scoped (IDOR-safe: a foreign id → 404)."""
    event = db.crm_get_event(event_id, ctx.tenant_id, client=ctx.client)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.post("/{event_id}/run-of-show/assemble", dependencies=[Depends(require_csrf)])
def run_of_show_assemble(event_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """(Re)assemble the run sheet on demand (§7). DETERMINISTIC — no LLM (§14): merges the
    event's current stated facts into an ordered sheet + flags gaps by rule, persisting it
    onto crm_events.run_of_show. IDOR-safe."""
    _require_events(ctx)
    event = _event_or_404(event_id, ctx)
    from datetime import datetime
    from app.scheduling import DIARY_TZ
    events_os.assemble_and_store(ctx.tenant_id, event, now=datetime.now(DIARY_TZ),
                                 client=ctx.client)
    return RedirectResponse(f"/app/events/{event_id}?msg=assembled", status_code=303)


@router.get("/{event_id}/run-of-show")
def run_of_show_get(request: Request, event_id: str,
                    ctx: TenantCtx = Depends(require_onboarded)):
    """The assembled sheet + flagged gaps (§7). Renders the same event-detail page anchored
    at the run-of-show section — the sheet lives on the detail view-model. IDOR-safe."""
    _require_events(ctx)
    data = events_os.detail_view(ctx.tenant_id, event_id, client=ctx.client)
    if data is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return templates.TemplateResponse(request, "app/event_detail.html", _ctx(
        "events", ctx, **data))


@router.post("/{event_id}/run-of-show/send", dependencies=[Depends(require_csrf)])
async def run_of_show_send(event_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Blast the FINAL run sheet to every stakeholder (§7) — one facts-only compose_outbound
    per party, DRAFT-FIRST (§14). Assembles fresh first so the sheet reflects the latest
    facts. IDOR-safe."""
    _require_events(ctx)
    event = _event_or_404(event_id, ctx)
    from datetime import datetime
    from app.scheduling import DIARY_TZ
    now = datetime.now(DIARY_TZ)
    result = events_os.assemble_and_store(ctx.tenant_id, event, now=now, client=ctx.client)
    deal = db.crm_get_deal(event["deal_id"], ctx.tenant_id, client=ctx.client) or {}
    stakeholders = db.crm_list_event_stakeholders(ctx.tenant_id, event_id, client=ctx.client)
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    try:
        await events_os.blast_run_sheet(
            ctx.tenant_id, cfg, event, deal, result["run_of_show"],
            stakeholders=stakeholders, now=now, auto=False)
        msg = "blasted"
    except Exception:
        log.warning("run-sheet blast failed for event %s", event_id, exc_info=True)
        msg = "blast_failed"
    return RedirectResponse(f"/app/events/{event_id}?msg={msg}", status_code=303)


@router.get("/{event_id}/run-of-show/export.pdf")
def run_of_show_export_pdf(event_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Export the run sheet as a PDF (§7/§8) — reuses the invoicing fpdf2 renderer via
    app.events_pdf. Assembles fresh so the exported sheet is current. IDOR-safe."""
    _require_events(ctx)
    from datetime import datetime
    from app import events_pdf
    from app.scheduling import DIARY_TZ
    event = _event_or_404(event_id, ctx)
    result = events_os.assemble_and_store(ctx.tenant_id, event, now=datetime.now(DIARY_TZ),
                                          client=ctx.client)
    deal = db.crm_get_deal(event["deal_id"], ctx.tenant_id, client=ctx.client) or {}
    pdf_bytes = events_pdf.render(event, deal, result["run_of_show"])
    ref = str(event_id)[:8]
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="run-sheet-{ref}.pdf"'})
