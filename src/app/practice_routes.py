"""Practice OS routes (docs/os/practice.md §7) — the recall wedge surface.

P0+P1 scope: a minimal /app/practice landing that links to the recall queue, the recall
queue itself (/app/practice/recall — appointments due recall), the [Draft recall now]
action, and the completion outcome trigger (POST .../outcome) that stamps the recall and
fires a best-effort follow-up draft (§6.4). The full Today board + risk panel + patient
panel + encrypted notes are P3/P4 — deliberately deferred; this landing is a stub.

Every route gates on _require_practice (db.module_ui_visible(tenant_id, "practice") → 404 if
off), the twin of dashboard._require_tms/_require_fsm. Mutations carry CSRF.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import agent, db, disclosure, guards, mailbox, people, trace, usage
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.scheduling import DIARY_TZ
from app.security import require_csrf
from app.templating import templates
from app.jobs import send_recall, score_no_shows
from app.jobs import send_appointment_reminders as appt_reminders

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/practice", tags=["practice"])

# Band → chip colour classes for the risk panel (low=grey/green, medium=amber, high=red).
_BAND_CHIP = {
    "high": "bg-red-100 text-red-700",
    "medium": "bg-amber-100 text-amber-700",
    "low": "bg-slate-100 text-slate-600",
}

# How far ahead the recall queue looks (plus overdue) — mirrors send_recall.RECALL_LEAD_DAYS.
RECALL_QUEUE_DAYS = send_recall.RECALL_LEAD_DAYS

FOLLOWUP_PURPOSE = ("A warm follow-up after the patient's appointment: thank them for coming "
                    "in, offer any simple aftercare or next steps, and let them know you're "
                    "here if they need anything. Keep it short and personal, in the practice's "
                    "voice. Do NOT give clinical advice or invent treatment details.")


def _require_practice(ctx: TenantCtx) -> None:
    """The gate for every Practice surface. One subscriptions row (module='practice')
    lights the recall queue, reminders and (later) the Today board."""
    if not db.module_ui_visible(ctx.tenant_id, "practice", client=ctx.client):
        raise HTTPException(status_code=404, detail="Practice OS is not active for this account")


def _require_clinical(ctx: TenantCtx) -> None:
    """Clinical notes are private details (security review 2026-09, M40): the
    People tick "See private details" — whose own words are "Access codes,
    keys, medical notes" — read FRESH, so a switch-off or a demotion bites at
    once. Owner and manager hold it by definition; an accountant never does;
    a switched-off member is refused (guards.require_ability)."""
    guards.require_ability(ctx, "crm", "private")


def _may_see_clinical(ctx: TenantCtx) -> bool:
    try:
        _require_clinical(ctx)
        return True
    except HTTPException:
        return False


def _log_clinical(ctx: TenantCtx, what: str, contact_id: str, **detail) -> None:
    """Every read, write and delete of clinical notes leaves a trace row — who,
    which patient, what (never the note itself)."""
    trace.emit(ctx.tenant_id, what, user_id=getattr(ctx, "user_id", None),
               detail={"contact_id": contact_id, **detail})


def _recall_queue(ctx: TenantCtx) -> list[dict]:
    """The recall work set for the queue UI: completed appointments due within the window
    (plus overdue) and not yet chased. Same query the daily job runs, so the human sees
    exactly what the engine will draft."""
    cutoff = (datetime.now(DIARY_TZ) + timedelta(days=RECALL_QUEUE_DAYS)).date().isoformat()
    rows = db.list_appointments_due_recall(ctx.tenant_id, cutoff, client=ctx.client)
    return sorted(rows, key=lambda b: str(b.get("recall_due_date") or ""))


def _confirmed_state(b: dict) -> bool:
    """Has the patient confirmed this appointment? The reminder cycle asks them to reply to
    confirm (§2); for v1 a reply flips this true once §11's confirm link ships, so we read
    the strongest signal we have today: a 'confirmed' custom flag on the row, falling back to
    whether the 24h/2h reminder stages have gone out (so the board shows the cycle state)."""
    if b.get("patient_confirmed") is True:
        return True
    stages = b.get("reminder_stages_sent") or {}
    return isinstance(stages, dict) and any(s in stages for s in ("24h", "2h"))


def _today_appointments(ctx: TenantCtx) -> list[dict]:
    """Card 1 — today's appointments as a day view (§2): time · service · practitioner ·
    client · no-show chip · confirmed? · outcome buttons. Reads the same confirmed-rows query
    the risk panel uses, filtered to today and ordered by start time. Pure view-model."""
    today = datetime.now(DIARY_TZ).date().isoformat()
    rows = db.list_confirmed_appointments_between(ctx.tenant_id, today, today, client=ctx.client)
    out = []
    for b in rows:
        band = (b.get("no_show_band") or "").strip().lower()
        out.append({
            "id": b.get("id"),
            "contact": b.get("contact"),
            "summary": (b.get("summary") or "Appointment"),
            "practitioner": b.get("practitioner"),
            "window": b.get("window"),
            "band": band or None,
            "score": b.get("no_show_score"),
            "chip": _BAND_CHIP.get(band, "bg-slate-100 text-slate-500"),
            "confirmed": _confirmed_state(b),
        })
    out.sort(key=lambda r: str(r["window"] or ""))
    return out


def _risk_panel(ctx: TenantCtx) -> list[dict]:
    """Today's + tomorrow's CONFIRMED appointments, highest no-show risk first (§2, §12.1).
    The score/band/reason are the CACHED columns the hourly scorer wrote — the panel never
    calls the LLM, it just renders what the sweep computed. Unscored appointments (the sweep
    hasn't reached them yet) sort last with a neutral chip."""
    now = datetime.now(DIARY_TZ)
    today = now.date().isoformat()
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    rows = db.list_confirmed_appointments_between(ctx.tenant_id, today, tomorrow, client=ctx.client)
    out = []
    for b in rows:
        band = (b.get("no_show_band") or "").strip().lower()
        out.append({
            "id": b.get("id"),
            "contact": b.get("contact"),
            "summary": (b.get("summary") or "Appointment"),
            "practitioner": b.get("practitioner"),
            "booking_date": str(b.get("booking_date") or "")[:10],
            "window": b.get("window"),
            "score": b.get("no_show_score"),
            "band": band or None,
            "reason": b.get("no_show_reason"),
            "chip": _BAND_CHIP.get(band, "bg-slate-100 text-slate-500"),
            "already_reminded": bool(b.get("extra_reminder_queued_at")),
        })
    # Highest score first; unscored (None) last, then by date/time.
    out.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1),
                            r["booking_date"], str(r["window"] or "")))
    return out


# ── landing → the Today board (§2) ───────────────────────────────────────────
@router.get("")
def practice_home(request: Request, msg: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The Practice Today board (§2), three stacked cards: (1) today's appointments — time,
    service, practitioner, client, no-show chip, confirmed? state, outcome buttons; (2) the
    no-show risk panel (today + tomorrow, riskiest first); (3) the recall-queue summary. All
    derived from existing rows + the cached scorer output — no new aggregation store."""
    _require_practice(ctx)
    queue = _recall_queue(ctx)
    return templates.TemplateResponse(request, "app/practice.html", _ctx(
        "practice", ctx, msg=msg, recall_count=len(queue),
        today=_today_appointments(ctx), risk=_risk_panel(ctx)))


# ── appointment detail + patient panel (§2, §7, §8) ──────────────────────────
@router.get("/appointments/{booking_id}")
def appointment_detail(request: Request, booking_id: str, msg: str = "",
                       ctx: TenantCtx = Depends(require_onboarded)):
    """The appointment detail (§7): service, practitioner, client, the patient-history
    summary (the REDACTED drafter-safe line) and — the patient panel — the DECRYPTED medical
    notes with an add/delete form. Tenant-user context only: no public/portal route touches
    medical data (§7, §14). Resolves the CRM contact by the booking's email."""
    _require_practice(ctx)
    b = db.get_booking(booking_id, ctx.tenant_id)
    if not b:
        raise HTTPException(status_code=404, detail="Appointment not found")
    contact = None
    email = str(b.get("contact") or "").strip()
    if "@" in email:
        contact = db.crm_contact_by_email(ctx.tenant_id, email, client=ctx.client)
    summary = notes = None
    if contact:
        summary = db.patient_history_summary(ctx.tenant_id, contact["id"], client=ctx.client)
        # The decrypted notes only for someone who holds private details (M40);
        # everyone else sees the page as a patient with no notes panel.
        if _may_see_clinical(ctx):
            notes = db.list_medical_notes(ctx.tenant_id, contact["id"], client=ctx.client)
            _log_clinical(ctx, "clinical_notes_read", contact["id"],
                          count=len(notes or []), via="appointment")
    return templates.TemplateResponse(request, "app/practice_appointment.html", _ctx(
        "practice", ctx, msg=msg, appt=b, contact=contact,
        patient_summary=summary, notes=notes))


# ── encrypted medical notes (tenant-user context ONLY, §4.3/§7/§14) ──────────
@router.post("/patients/{contact_id}/notes", dependencies=[Depends(require_csrf)])
def add_patient_note(contact_id: str, text: str = Form(""), kind: str = Form("note"),
                     booking_id: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    """Add an ENCRYPTED medical note for a patient (§4.3). Typed by the practice — manual
    only, never auto-written from extraction in this phase (§14). Redirects back to the
    appointment detail (or the board) so the note shows immediately."""
    _require_practice(ctx)
    _require_clinical(ctx)
    kind = kind if kind in ("note", "allergy", "medication", "condition") else "note"
    text = (text or "").strip()
    if text:
        row = db.add_medical_note(ctx.tenant_id, contact_id, text, kind=kind,
                                  source="manual", created_by=getattr(ctx, "user_id", None),
                                  client=ctx.client)
        _log_clinical(ctx, "clinical_note_added", contact_id,
                      note_id=(row or {}).get("id"), kind=kind)
    dest = f"/app/practice/appointments/{booking_id}?msg=note" if booking_id else "/app/practice"
    return RedirectResponse(dest, status_code=303)


@router.get("/patients/{contact_id}/notes")
def list_patient_notes(contact_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """The decrypted notes list as JSON (tenant-user only). decrypt_error rows surface as
    text='' + decrypt_error=True rather than crashing (§14). Never a public route."""
    _require_practice(ctx)
    _require_clinical(ctx)
    notes = db.list_medical_notes(ctx.tenant_id, contact_id, client=ctx.client)
    _log_clinical(ctx, "clinical_notes_read", contact_id, count=len(notes or []), via="json")
    return {"notes": notes}


@router.post("/patients/{contact_id}/notes/{note_id}/delete",
             dependencies=[Depends(require_csrf)])
def delete_patient_note(contact_id: str, note_id: str, booking_id: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Delete a medical note (IDOR-safe: scoped to tenant + contact). Tenant-user only.
    A POST-with-delete rather than a DELETE verb so the plain HTML form can drive it (the
    house pattern for destructive actions behind CSRF)."""
    _require_practice(ctx)
    _require_clinical(ctx)
    db.delete_medical_note(ctx.tenant_id, contact_id, note_id, client=ctx.client)
    _log_clinical(ctx, "clinical_note_deleted", contact_id, note_id=note_id)
    dest = f"/app/practice/appointments/{booking_id}?msg=deleted" if booking_id else "/app/practice"
    return RedirectResponse(dest, status_code=303)


# ── manual extra reminder (the risk-panel [Extra reminder →] action, §12.1) ──
@router.post("/appointments/{booking_id}/remind", dependencies=[Depends(require_csrf)])
async def appointment_remind(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Fire the high-risk extra reminder by hand (the risk-panel action). Reuses the sweep's
    compose→queue→dedupe path so the manual and automatic nudge are identical; the
    extra_reminder_queued_at stamp dedupes, so a second click is a no-op (msg=already)."""
    _require_practice(ctx)
    b = db.get_booking(booking_id, ctx.tenant_id)
    if not b or "@" not in str(b.get("contact") or ""):
        raise HTTPException(status_code=404, detail="Appointment not found")
    if b.get("extra_reminder_queued_at"):
        return RedirectResponse("/app/practice?msg=already", status_code=303)
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    try:
        await score_no_shows.queue_extra_reminder(ctx.tenant_id, cfg, False, b)
    except Exception:
        log.warning("manual extra reminder failed for booking %s", booking_id, exc_info=True)
        return RedirectResponse("/app/practice?msg=err", status_code=303)
    return RedirectResponse("/app/practice?msg=reminded", status_code=303)


# ── recall queue (the wedge's human face) ────────────────────────────────────
@router.get("/recall")
def recall_queue(request: Request, msg: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """Appointments due recall — due date, service, last seen, usual practitioner — each
    with a [Draft recall now] action. The daily job (send_recall) drafts these proactively;
    this is the review/trigger surface."""
    _require_practice(ctx)
    return templates.TemplateResponse(request, "app/practice_recall.html", _ctx(
        "practice", ctx, msg=msg, queue=_recall_queue(ctx)))


@router.post("/recall/{booking_id}/draft", dependencies=[Depends(require_csrf)])
async def recall_draft(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Draft a recall now (the queue's [Draft recall now] button) — reuses the daily job's
    per-appointment compose→queue→dedupe path so the manual and automatic draft are
    identical. Never auto-sends from the manual button; it lands in the approval queue."""
    _require_practice(ctx)
    b = db.get_booking(booking_id, ctx.tenant_id)
    if not b or not str(b.get("contact") or "").strip():
        raise HTTPException(status_code=404, detail="Appointment not found")
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    try:
        await send_recall._remind_one(ctx.tenant_id, cfg, False, b)
    except Exception:
        log.warning("manual recall draft failed for booking %s", booking_id, exc_info=True)
        return RedirectResponse("/app/practice/recall?msg=err", status_code=303)
    return RedirectResponse("/app/practice/recall?msg=drafted", status_code=303)


# ── completion trigger → recall stamp + follow-up draft (§6.4) ───────────────
@router.post("/appointments/{booking_id}/outcome", dependencies=[Depends(require_csrf)])
async def appointment_outcome(booking_id: str, outcome: str = Form("completed"),
                              ctx: TenantCtx = Depends(require_onboarded)):
    """Mark an appointment's outcome. The COMPLETED path is the recall trigger: it stamps
    recall_due_date = booking_date + service.recall_months (so the daily job can chase it)
    and fires a best-effort follow-up draft in the practice's voice. no_show sets the
    status (feeding the P3 scorer's history); the cancelled/no-show risk chips are P3."""
    _require_practice(ctx)
    outcome = outcome if outcome in ("completed", "no_show", "cancelled") else "completed"
    if outcome == "cancelled":
        db.set_booking_status(booking_id, "cancelled", ctx.tenant_id, client=ctx.client)
        return RedirectResponse("/app/practice/recall?msg=cancelled", status_code=303)

    # completed | no_show — stamp the recall (completed only, inside the db fn).
    db.complete_appointment_and_schedule_recall(
        booking_id, ctx.tenant_id, outcome=outcome, client=ctx.client)
    if outcome == "completed":
        try:
            await _followup_draft(ctx, booking_id)
        except Exception:
            # A follow-up failure must never fail the outcome write — the recall is stamped.
            log.warning("follow-up draft failed for booking %s", booking_id, exc_info=True)
    return RedirectResponse("/app/practice/recall?msg=outcome", status_code=303)


async def _followup_draft(ctx: TenantCtx, booking_id: str) -> None:
    """Best-effort post-appointment follow-up (§6.4): compose in the practice's voice and
    queue as a pending draft (or auto-send for full-auto tenants), synchronous with the
    outcome click — no scheduled job, like the CRM's on-write hooks."""
    b = db.get_booking(booking_id, ctx.tenant_id)
    to = str((b or {}).get("contact") or "").strip()
    if not b or "@" not in to:
        return
    cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
    service = (b.get("summary") or "appointment").strip()
    facts = f"The patient came in for their {service}. Thank them and offer to help if they need anything."
    subject = f"Thanks for coming in — your {service}"
    result = await agent.compose_outbound(cfg, FOLLOWUP_PURPOSE, facts,
                                          tenant_id=ctx.tenant_id, log_operation="followup")
    log_id, _ = db.insert_email_log(
        ctx.tenant_id, to, subject, f"[follow-up] {facts}", result.text,
        status="pending", kind="reminder", thread_id=b.get("thread_id"))
    usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                           operation="followup", email_log_id=log_id)
    db.log_agent_event(
        ctx.tenant_id, "followup", f"Drafted a follow-up to {to} after their {service}",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=booking_id)
    if bool(cfg.get("auto_send")):
        await mailbox.send_mail(ctx.tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, ctx.tenant_id, via="auto", edited=False)
