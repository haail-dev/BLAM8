"""Assets OS routes (docs/os/assets.md §2, §7, §8) — Phase A.

The daily board (handovers today + upcoming), the unit registry (list + add/edit —
the vehicles-CRUD UI twin), and a "take a booking against a unit" flow that calls
db.hold_unit_booking (surfacing out-of-availability cleanly). Deposits, condition
capture and damage cases are Phases B/C — not built here.

Every surface gates on _require_assets (db.module_ui_visible(tenant_id, "assets") → 404
if off), the twin of dashboard._require_tms/_require_fsm. Writes carry CSRF and are
IDOR-safe (every read/write is tenant-scoped).
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from app import (assets, assets_condition, assets_deposits, assets_pdf, db, disclosure, mailbox,
                 slotgrid, storage, usage)
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.scheduling import DIARY_TZ
from app.security import require_csrf
from app.templating import templates
from fastapi.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/assets", tags=["assets"])

# The deposit JSON surface (§7) lives under /api/assets — a sibling router so the paths
# match the spec (POST /api/assets/bookings/{id}/deposit/*). Same gate + CSRF + IDOR posture.
api_router = APIRouter(prefix="/api/assets", tags=["assets"])


def _require_assets(ctx: TenantCtx) -> None:
    """The gate for every Assets surface. One subscriptions row (module='assets') lights
    the board, the unit registry and unit-scoped booking — the _require_tms twin."""
    if not db.module_ui_visible(ctx.tenant_id, "assets", client=ctx.client):
        raise HTTPException(status_code=404, detail="Assets OS is not active for this account")


def _num(raw: str) -> float | None:
    """Clean a numeric form field → a float, or None for blank/garbage (a rate/deposit
    left empty means 'use the tenant default', never 0)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v >= 0 else None
    except ValueError:
        return None


# ── the daily board (§2) ──────────────────────────────────────────────────────
@router.get("")
def assets_home(request: Request, msg: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The Assets board (§2): today's handovers (units going out / coming back) and the
    upcoming lane (next 14 days). Composed by assets.view from the shipped diary + the unit
    registry. The damage & maintenance lane is Phase C/D — not shown yet."""
    _require_assets(ctx)
    data = assets.view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/assets.html", _ctx(
        "assets", ctx, msg=msg, board=data))


# ── unit registry (list + add/edit — the vehicles-CRUD UI twin, §8) ───────────
@router.get("/units")
def units_page(request: Request, msg: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The unit registry: every hireable asset with its rate card, deposit, condition and
    next-service date. Add a new unit; edit each in place. Mirrors the Fleet settings page."""
    _require_assets(ctx)
    units = db.asset_list_units(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/assets_units.html", _ctx(
        "assets", ctx, msg=msg, units=units,
        active_count=sum(1 for u in units if u.get("active", True))))


@router.post("/units/add", dependencies=[Depends(require_csrf)])
def units_add(name: str = Form(...), category: str = Form(""), asset_ref: str = Form(""),
              resource_type: str = Form(""), rate_daily: str = Form(""),
              rate_weekly: str = Form(""), deposit_default: str = Form(""),
              ctx: TenantCtx = Depends(require_onboarded)):
    """Add a unit (the add_vehicle twin). A blank resource_type = a NAMED unit (books against
    its own id); a shared value groups a FUNGIBLE POOL (12 identical drills → best-fit at
    handover, §1). Empty rate/deposit fields fall back to the tenant stay_pricing default."""
    _require_assets(ctx)
    clean = name.strip()[:120]
    if not clean:
        return RedirectResponse("/app/assets/units?msg=badunit", status_code=303)
    unit = {
        "name": clean,
        "category": category.strip()[:60] or None,
        "asset_ref": asset_ref.strip()[:60] or None,
        "resource_type": resource_type.strip()[:60] or None,
        "rate_daily": _num(rate_daily),
        "rate_weekly": _num(rate_weekly),
        "deposit_default": _num(deposit_default),
    }
    db.asset_create_unit(ctx.tenant_id, unit, client=ctx.client)
    return RedirectResponse("/app/assets/units?msg=unitadded", status_code=303)


@router.post("/units/{unit_id}/update", dependencies=[Depends(require_csrf)])
def units_update(unit_id: str, name: str = Form(...), category: str = Form(""),
                 asset_ref: str = Form(""), resource_type: str = Form(""),
                 rate_daily: str = Form(""), rate_weekly: str = Form(""),
                 deposit_default: str = Form(""), next_service_due: str = Form(""),
                 active: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    """Edit one unit in place (IDOR-safe: scoped to tenant + unit). Deactivating (active
    unchecked) only blocks NEW holds — existing hires keep their unit_id."""
    _require_assets(ctx)
    if not db.asset_get_unit(unit_id, ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Unit not found")
    clean = name.strip()[:120]
    if not clean:
        return RedirectResponse("/app/assets/units", status_code=303)
    fields = {
        "name": clean,
        "category": category.strip()[:60] or None,
        "asset_ref": asset_ref.strip()[:60] or None,
        "resource_type": resource_type.strip()[:60] or None,
        "rate_daily": _num(rate_daily),
        "rate_weekly": _num(rate_weekly),
        "deposit_default": _num(deposit_default),
        "next_service_due": next_service_due.strip()[:10] or None,
        "active": bool(active),
    }
    db.asset_update_unit(unit_id, ctx.tenant_id, fields, client=ctx.client)
    return RedirectResponse("/app/assets/units?msg=unitsaved", status_code=303)


# ── maintenance: mark a unit serviced (§6/§11) — the FSM service-completion twin ─
@router.post("/units/{unit_id}/serviced", dependencies=[Depends(require_csrf)])
def units_serviced(unit_id: str, serviced_on: str = Form(""),
                   ctx: TenantCtx = Depends(require_onboarded)):
    """Log a completed service against a unit (§11): advance next_service_due by the unit's
    service_interval_days (the FSM complete_job_and_schedule_service twin, by DAYS not months)
    and lift condition back toward 5.0. IDOR-safe (tenant + unit scoped). Falls off the
    maintenance lane once the next date moves past the lead window."""
    _require_assets(ctx)
    if not db.asset_get_unit(unit_id, ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Unit not found")
    on = (serviced_on or "").strip() or None
    db.complete_unit_service(unit_id, ctx.tenant_id, serviced_on=on, client=ctx.client)
    return RedirectResponse("/app/assets?msg=serviced", status_code=303)


# ── handover: assign a specific pool unit (§1/§12a) — the best_fit twin ────────
@router.post("/bookings/{booking_id}/assign-unit", dependencies=[Depends(require_csrf)])
def bookings_assign_unit(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Pin a specific unit to a fungible-pool hire at HANDOVER (§1/§12a) — picks a free unit
    from the hire's pool exactly as best_fit / hold_fleet_booking picks a vehicle, so the
    condition capture + damage diff key on that specific asset. Idempotent (already-assigned
    → returns the pinned unit); a full pool → 200 + a clear 'no free unit' payload."""
    _require_assets(ctx)
    _load_hire(booking_id, ctx)
    unit = db.assign_pool_unit(booking_id, ctx.tenant_id, client=ctx.client)
    if not unit:
        return JSONResponse({"ok": False, "reason": "No free unit in the pool to assign"},
                            status_code=200)
    return JSONResponse({"ok": True, "unit_id": unit.get("id"), "unit": unit.get("name")})


# ── take a booking against a unit (the unit-scoped hold, §7) ──────────────────
@router.post("/units/{unit_id}/book", dependencies=[Depends(require_csrf)])
def units_book(unit_id: str, arrival: str = Form(...), nights: str = Form("1"),
               contact: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    """Hold a hire against a NAMED unit — the demo-visible spine (§13): a lettings tenant
    registers cottages and takes atomic bookings against them. Delegates to
    db.hold_unit_booking, which reuses the shipped stay-availability engine scoped to the
    unit; a span that clashes with an existing hire on the unit returns None → msg=full
    (never a double-book). Deposits are Phase B, so the hold carries the unit default only."""
    _require_assets(ctx)
    unit = db.asset_get_unit(unit_id, ctx.tenant_id, client=ctx.client)
    if not unit:
        raise HTTPException(status_code=404, detail="Unit not found")
    try:
        from datetime import date as _date
        day = _date.fromisoformat(arrival.strip())
        n = max(1, int(nights))
    except (ValueError, TypeError):
        return RedirectResponse("/app/assets/units?msg=baddates", status_code=303)

    # Same convention as the shipped stay path (scheduling stay tool): start_block is the
    # WITHIN-DAY block (0 = arrival at midnight/collection time), length_blocks is the whole
    # span (nights × blocks-per-day); booking_date carries the arrival day.
    start_block = 0
    length_blocks = n * slotgrid.BLOCKS_PER_DAY
    summary = f"Hire · {unit['name']}"
    from app import scheduling
    readiness, _ = scheduling.make_executor(ctx.tenant_id, source="manual", fleet=True)
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise HTTPException(status_code=409, detail=blocked)
    row = db.hold_unit_booking(
        ctx.tenant_id, unit_id, start_block, length_blocks, summary,
        deposit_amount=unit.get("deposit_default"),
        contact=contact.strip() or None, booking_date_iso=day.isoformat(),
        client=ctx.client)
    if row is None:
        return RedirectResponse("/app/assets/units?msg=full", status_code=303)
    return RedirectResponse("/app/assets?msg=held", status_code=303)


# ── deposits — the Stripe rail (§7, §11) ──────────────────────────────────────
def _hire_days(booking: dict) -> int:
    """Whole-day length of a hire from its grid span — the auth-vs-charge input (§14). A booking
    with no readable span defaults to 1 (short → a hold), the least surprising for a same-day hold."""
    span = slotgrid.stay_span(booking)
    if not span:
        return 1
    _lo, length = span
    return max(1, length // slotgrid.BLOCKS_PER_DAY)


def _load_hire(booking_id: str, ctx: TenantCtx) -> dict:
    """Fetch a booking scoped to the tenant (IDOR-safe) — 404 if it isn't theirs."""
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if not b:
        raise HTTPException(status_code=404, detail="Hire not found")
    return b


@api_router.post("/bookings/{booking_id}/deposit/link", dependencies=[Depends(require_csrf)])
def deposit_link(booking_id: str, amount: str = Form(""),
                 ctx: TenantCtx = Depends(require_onboarded)):
    """Mint a Stripe deposit (hold ≤5d / charge >5d, §14) and return the customer-facing URL to
    drop in the confirmation draft. Persists the pending unit_deposits row first (so the webhook
    can route the resulting PaymentIntent by deposit_id). Stripe not configured → 200 + a clear
    'not configured' payload rather than a crash."""
    _require_assets(ctx)
    b = _load_hire(booking_id, ctx)
    amt = _num(amount)
    if amt is None:
        amt = b.get("deposit_amount")
    if amt is None or float(amt) <= 0:
        return JSONResponse({"ok": False, "reason": "No deposit amount set for this hire"},
                            status_code=400)
    dep = db.upsert_deposit(ctx.tenant_id, booking_id, b.get("unit_id"), amt,
                            method=assets_deposits.METHOD_HOLD, client=ctx.client)
    res = assets_deposits.create_deposit_intent(
        ctx.tenant_id, b, amt, hire_days=_hire_days(b), deposit_id=dep["id"])
    if not res.ok:
        return JSONResponse({"ok": False, "reason": res.reason}, status_code=200)
    # Persist the resolved method + Stripe ref onto the row (status stays 'pending' until the
    # webhook confirms the hold/charge).
    db.set_deposit_status(dep["id"], ctx.tenant_id, "pending",
                          external_ref=res.external_ref, client=ctx.client)
    return JSONResponse({"ok": True, "url": res.url, "method": res.method,
                         "deposit_id": dep["id"]})


@api_router.post("/bookings/{booking_id}/deposit/refund", dependencies=[Depends(require_csrf)])
def deposit_refund(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Give the deposit back on a clean return — void a held authorisation or refund a charge
    (§11). Idempotent-ish: no deposit → 404; Stripe not configured → 200 + reason."""
    _require_assets(ctx)
    _load_hire(booking_id, ctx)
    dep = db.get_deposit_for_booking(ctx.tenant_id, booking_id, client=ctx.client)
    if not dep:
        raise HTTPException(status_code=404, detail="No deposit on this hire")
    res = assets_deposits.refund_or_release(dep)
    if not res.ok:
        return JSONResponse({"ok": False, "reason": res.reason}, status_code=200)
    status = "released" if dep.get("method") == assets_deposits.METHOD_HOLD else "refunded"
    db.set_deposit_status(dep["id"], ctx.tenant_id, status, client=ctx.client)
    return JSONResponse({"ok": True, "status": status})


@api_router.post("/bookings/{booking_id}/deposit/deduct", dependencies=[Depends(require_csrf)])
def deposit_deduct(booking_id: str, amount: str = Form(...),
                   ctx: TenantCtx = Depends(require_onboarded)):
    """Deduct a MANUAL amount from the deposit (§11; the photo-diff auto-figure is Phase C). Captures
    N of a hold (remainder auto-voids) or partial-refunds a charge, and stamps deducted_amount."""
    _require_assets(ctx)
    _load_hire(booking_id, ctx)
    dep = db.get_deposit_for_booking(ctx.tenant_id, booking_id, client=ctx.client)
    if not dep:
        raise HTTPException(status_code=404, detail="No deposit on this hire")
    amt = _num(amount)
    if amt is None or float(amt) <= 0:
        return JSONResponse({"ok": False, "reason": "Enter an amount to deduct"}, status_code=400)
    res = assets_deposits.deduct(dep, amt)
    if not res.ok:
        return JSONResponse({"ok": False, "reason": res.reason}, status_code=200)
    db.set_deposit_status(dep["id"], ctx.tenant_id, "deducted",
                          deducted_amount=float(amt), client=ctx.client)
    return JSONResponse({"ok": True, "status": "deducted", "deducted_amount": float(amt)})


# ══════════════════════════════════════════════════════════════════════════════
# Phase C — condition capture (magic-link) + damage cases (docs/os/assets.md §7/§12b)
# ══════════════════════════════════════════════════════════════════════════════

# The public capture page is a tokenised no-auth magic-link (the /track/{token} twin):
# the token IS the capability (token → the one booking, IDOR-safe), so this router
# carries NO require_onboarded / module gate — a caretaker with the link can capture.
capture_router = APIRouter(tags=["assets-capture"])


def _capture_context(token: str) -> tuple[dict, dict | None, str]:
    """Resolve a capture token → (booking, unit, phase). Service role (the token is the
    capability). Raises 404 when the token is unknown."""
    booking = db.get_booking_by_capture_token(token)
    if not booking:
        raise HTTPException(status_code=404, detail="Capture link not recognised")
    tenant_id = booking["tenant_id"]
    unit = db.asset_get_unit(booking.get("unit_id"), tenant_id) if booking.get("unit_id") else None
    phase = assets.capture_phase(tenant_id, booking["id"])
    return booking, unit, phase


@capture_router.get("/capture/{token}")
def capture_page(request: Request, token: str):
    """The mobile condition-capture page (§8) — no auth. Shows the fixed shot list for
    the unit's category, a photo input per shot, a 0–5 rating and a captured-by name,
    and states whether this is the OUT (handover) or IN (return) capture."""
    booking = db.get_booking_by_capture_token(token)
    if not booking:
        return templates.TemplateResponse(request, "capture.html", {"c": None},
                                          status_code=404)
    tenant_id = booking["tenant_id"]
    unit = db.asset_get_unit(booking.get("unit_id"), tenant_id) if booking.get("unit_id") else None
    phase = assets.capture_phase(tenant_id, booking["id"])
    ctx = {
        "token": token,
        "ref": booking.get("booking_ref") or (booking.get("id") or "")[:8],
        "unit": (unit or {}).get("name") or "your hire",
        "phase": phase,
        "phase_label": "Handover (out)" if phase == "out" else "Return (in)",
        "shots": assets.shot_list_for(unit),
    }
    return templates.TemplateResponse(request, "capture.html", {"c": ctx})


@capture_router.post("/capture/{token}")
async def capture_submit(request: Request, token: str):
    """Persist a condition_report (+ photos to Storage) for the hire the token resolves.
    phase = 'out' if no out-report yet, else 'in'. On an 'in' report the diff pipeline
    runs (compare against the out baseline, gate, open a case + draft). GRACEFUL: a
    Storage outage records the report with storage_error and shows a clear message —
    never crashes the capture. Reads the whole multipart form itself (photos + fields)
    so the request body is parsed once."""
    booking = db.get_booking_by_capture_token(token)
    if not booking:
        return templates.TemplateResponse(request, "capture.html", {"c": None},
                                          status_code=404)
    tenant_id = booking["tenant_id"]
    booking_id = booking["id"]
    unit = db.asset_get_unit(booking.get("unit_id"), tenant_id) if booking.get("unit_id") else None
    phase = assets.capture_phase(tenant_id, booking_id)

    # Read every uploaded photo (the form posts photo_00, photo_01, … in shot order) and
    # the text fields, from the single parsed multipart form.
    form = await request.form()
    rating = str(form.get("rating") or "")
    captured_by = str(form.get("captured_by") or "")
    notes = str(form.get("notes") or "")
    photo_paths: list[str] = []
    storage_error = False
    idx = 0
    for key in sorted(k for k in form.keys() if k.startswith("photo_")):
        upload = form[key]
        if not isinstance(upload, UploadFile):
            continue
        data = await upload.read()
        if not data:
            continue
        try:
            path = storage.upload_photo(
                tenant_id, booking_id, phase, idx, data,
                content_type=upload.content_type or "image/jpeg")
            photo_paths.append(path)
            idx += 1
        except storage.StorageError:
            storage_error = True   # keep going: the report is still recorded

    try:
        r = _num(rating)
        r = r if r is not None and 0 <= r <= 5 else None
    except Exception:  # noqa: BLE001
        r = None

    report = db.insert_condition_report(
        tenant_id, booking.get("unit_id"), booking_id, phase,
        rating=r, photo_urls=photo_paths, notes=(notes.strip() or None),
        captured_by=(captured_by.strip() or None), storage_error=storage_error)

    # The diff pipeline triggers ONLY on the return ('in') report (§6.3). Failures here
    # never fail the capture — the caretaker's submission is already saved.
    if phase == "in":
        try:
            await _run_condition_diff(tenant_id, report, unit)
        except Exception:
            log.warning("condition diff pipeline failed for booking %s (tenant %s)",
                        booking_id, tenant_id, exc_info=True)

    def _blocking():
        ctx = {"done": True, "phase": phase, "storage_error": storage_error,
               "unit": (unit or {}).get("name") or "your hire",
               "count": len(photo_paths)}
        return templates.TemplateResponse(request, "capture.html", {"c": ctx})
    return await run_in_threadpool(_blocking)


async def _run_condition_diff(tenant_id: str, in_report: dict, unit: dict | None) -> dict:
    """Run the photo-diff on a return report and queue the right drafter touchpoint (§12b):
    damage_claim → a PENDING claim draft (NEVER auto-sent, even if cfg.auto_send); clean →
    a deposit-refund note (auto-send honoured); review/no_baseline/manual_review → no draft.
    Every draft is insert_email_log(status=…) → metered, exactly like the reminder shape."""
    from app import agent
    cfg = db.effective_config(tenant_id, None)
    # cfg carries the tenant's per-category repair-cost defaults (§12b) → the diff opens the
    # case with a SUGGESTED proposed_deduction (a hint only; still human-approved, never auto-sent).
    verdict = await assets_condition.run_diff(tenant_id, in_report, unit=unit, cfg=cfg)
    outcome = verdict["outcome"]
    booking = db.get_booking(in_report.get("booking_id"), tenant_id) \
        if in_report.get("booking_id") else None
    to = (booking or {}).get("contact")
    if not to or not db.reachable_contact(to):
        return verdict   # nothing to draft to
    auto = bool(cfg.get("auto_send"))

    if outcome == "damage_claim":
        # A deduction CLAIM is dispute-prone → FORCE pending, never auto-send (§12b/§14).
        # The SUGGESTED figure rides into the facts as a proposal only (the owner decides).
        facts = assets_condition.damage_facts(unit, verdict["findings"],
                                              proposed_deduction=verdict.get("proposed_deduction"))
        purpose = ("Tell the customer, warmly and factually, that on return we found new "
                   "damage not present at handover, that photo evidence is on file, and "
                   "that we're reviewing a possible deposit deduction. Do NOT state a final "
                   "amount or demand payment — this is a heads-up before the owner decides.")
        result = await agent.compose_outbound(cfg, purpose, facts, tenant_id=tenant_id,
                                              log_operation="condition_diff", log_module="assets")
        log_id, _ = db.insert_email_log(
            tenant_id, to, "About your returned hire",
            f"[damage claim draft] {facts}", result.text,
            status="pending", kind="reminder", thread_id=(booking or {}).get("thread_id"))
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="condition_diff", email_log_id=log_id, module="assets")
        # NEVER auto-sent regardless of cfg.auto_send — the claim stays pending for approval.
    elif outcome == "clean":
        facts = assets_condition.clean_return_facts(unit)
        purpose = ("A warm note that the hire was returned in good condition and the deposit "
                   "is being released in full. Keep it short and grateful.")
        result = await agent.compose_outbound(cfg, purpose, facts, tenant_id=tenant_id,
                                              log_operation="condition_diff", log_module="assets")
        log_id, _ = db.insert_email_log(
            tenant_id, to, "Your deposit is on its way back",
            f"[clean return refund] {facts}", result.text,
            status="pending", kind="reminder", thread_id=(booking or {}).get("thread_id"))
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="condition_diff", email_log_id=log_id, module="assets")
        if auto:
            await mailbox.send_mail(tenant_id, to, "Your deposit is on its way back",
                                    disclosure.wrap(cfg, result.text))
            db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
    # review / no_baseline / manual_review → surfaced on the queue, no draft.
    return verdict


# ── the damage-case queue + evidence bundle (§7/§8/§12b) ──────────────────────
@router.get("/damage")
def damage_queue(request: Request, status: str = "open",
                 ctx: TenantCtx = Depends(require_onboarded)):
    """The damage-case queue (open / review / resolved). Every row links to its evidence
    bundle. status filters the lane (default open)."""
    _require_assets(ctx)
    st = status if status in ("open", "resolved") else None
    cases = db.list_damage_cases(ctx.tenant_id, status=st, client=ctx.client)
    return templates.TemplateResponse(request, "app/assets_damage.html", _ctx(
        "assets", ctx, cases=cases, status=status))


@router.get("/damage/{case_id}")
def damage_case_page(request: Request, case_id: str,
                     ctx: TenantCtx = Depends(require_onboarded)):
    """One case: the EVIDENCE BUNDLE (§12b) — side-by-side out/in photos (signed URLs),
    per-finding annotations, the deposit trail, and the deducted/refunded/waived actions.
    The deduct action is HARD-BLOCKED with no out-report baseline (no baseline, no claim, §14)."""
    _require_assets(ctx)
    case = db.get_damage_case(case_id, ctx.tenant_id, client=ctx.client)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    bundle = assets.evidence_bundle(ctx.tenant_id, case, client=ctx.client)
    return templates.TemplateResponse(request, "app/assets_damage_case.html", _ctx(
        "assets", ctx, **bundle))


@router.get("/damage/{case_id}/export.pdf")
def damage_case_export_pdf(case_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Export the evidence bundle as a PDF (§12b, the 'yours to export' promise) — the
    side-by-side out/in photos, findings, timestamps, captured_by, model reasoning and the
    deposit trail, rendered via app.assets_pdf (which reuses the invoice_pdf fpdf2 generator).
    Photos degrade gracefully (a 'photo unavailable' placeholder) if Storage is down."""
    _require_assets(ctx)
    case = db.get_damage_case(case_id, ctx.tenant_id, client=ctx.client)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    bundle = assets.evidence_bundle(ctx.tenant_id, case, client=ctx.client)
    pdf_bytes = assets_pdf.render(bundle, client=ctx.client)
    ref = (case.get("booking_id") or case_id)[:8]
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="damage-{ref}.pdf"'})


@api_router.get("/damage/{case_id}/export")
def damage_case_export(case_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Export the evidence bundle as JSON (the 'yours to export' promise, §12b). Signed
    URLs are included so the recipient can view the photos for their TTL."""
    _require_assets(ctx)
    case = db.get_damage_case(case_id, ctx.tenant_id, client=ctx.client)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    bundle = assets.evidence_bundle(ctx.tenant_id, case, client=ctx.client)
    return JSONResponse({
        "case": case, "unit": bundle.get("unit"), "booking": bundle.get("booking"),
        "findings": bundle.get("findings"), "deposit": bundle.get("deposit"),
        "has_baseline": bundle.get("has_baseline"),
    })


@api_router.post("/damage/{case_id}/resolve", dependencies=[Depends(require_csrf)])
def damage_resolve(case_id: str, resolution: str = Form(...), amount: str = Form(""),
                   ctx: TenantCtx = Depends(require_onboarded)):
    """Close a case — deducted | refunded | waived (§7/§12b). HUMAN-APPROVED always (§14):
    * deducted → Phase B assets_deposits.deduct(deposit, amount, damage_case_id=id). HARD-
      BLOCKED when there's no out-report baseline (no baseline, no claim, §14).
    * refunded → assets_deposits.refund_or_release(deposit).
    * waived   → record only (no money moves)."""
    _require_assets(ctx)
    case = db.get_damage_case(case_id, ctx.tenant_id, client=ctx.client)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    if resolution not in ("deducted", "refunded", "waived"):
        return JSONResponse({"ok": False, "reason": "Unknown resolution"}, status_code=400)

    booking_id = case.get("booking_id")
    deposit = db.get_deposit_for_booking(ctx.tenant_id, booking_id, client=ctx.client) \
        if booking_id else None
    resolved_amount = None

    if resolution == "deducted":
        # No baseline (no out-report) → the claim can't stand. HARD-BLOCK (§14).
        if not case.get("out_report_id"):
            return JSONResponse(
                {"ok": False, "reason": "No handover baseline was captured for this hire, "
                                        "so a deposit deduction can't be made."},
                status_code=400)
        if not deposit:
            return JSONResponse({"ok": False, "reason": "No deposit on this hire to deduct from"},
                                status_code=400)
        amt = _num(amount)
        if amt is None or float(amt) <= 0:
            return JSONResponse({"ok": False, "reason": "Enter an amount to deduct"},
                                status_code=400)
        res = assets_deposits.deduct(deposit, amt, damage_case_id=case_id)
        if not res.ok:
            return JSONResponse({"ok": False, "reason": res.reason}, status_code=200)
        db.set_deposit_status(deposit["id"], ctx.tenant_id, "deducted",
                              deducted_amount=float(amt), damage_case_id=case_id,
                              client=ctx.client)
        resolved_amount = float(amt)
    elif resolution == "refunded":
        if deposit:
            res = assets_deposits.refund_or_release(deposit)
            if not res.ok:
                return JSONResponse({"ok": False, "reason": res.reason}, status_code=200)
            status = "released" if deposit.get("method") == assets_deposits.METHOD_HOLD else "refunded"
            db.set_deposit_status(deposit["id"], ctx.tenant_id, status, client=ctx.client)

    db.resolve_damage_case(case_id, ctx.tenant_id, resolution=resolution,
                           resolved_amount=resolved_amount, client=ctx.client)
    return JSONResponse({"ok": True, "resolution": resolution, "amount": resolved_amount})


@api_router.post("/damage/{case_id}/rerun", dependencies=[Depends(require_csrf)])
async def damage_rerun(case_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Re-run the diff for a case (e.g. after adding photos) — re-reads the in-report and
    re-runs run_diff. Best-effort; the queue reflects the new verdict."""
    _require_assets(ctx)
    case = db.get_damage_case(case_id, ctx.tenant_id, client=ctx.client)
    if not case:
        raise HTTPException(status_code=404, detail="Damage case not found")
    in_report = db.get_condition_report(case.get("in_report_id"), ctx.tenant_id, client=ctx.client) \
        if case.get("in_report_id") else None
    if not in_report:
        return JSONResponse({"ok": False, "reason": "No return report to re-compare"},
                            status_code=400)
    unit = db.asset_get_unit(case.get("unit_id"), ctx.tenant_id, client=ctx.client) \
        if case.get("unit_id") else None
    verdict = await assets_condition.run_diff(ctx.tenant_id, in_report, unit=unit)
    return JSONResponse({"ok": True, "outcome": verdict["outcome"],
                         "diff_confidence": verdict["diff_confidence"],
                         "severity": verdict["severity"]})
