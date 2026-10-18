"""CRM JSON API (docs/architecture/CRM_design.md §7) — read surfaces over the auto-populated
CRM, tenant-scoped like every /app route. The dashboard pages render server-side
(src/app/dashboard.py); this API is the programmatic face of the same data — and the
first seam of "yours to export".

Reads run on the caller's JWT-bound client, so RLS is the second wall behind the
explicit tenant_id filters, exactly as in the dashboard.
"""
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app import crm, crm_sequences, db
from app.auth import TenantCtx, require_onboarded
from app.security import require_csrf


def _require_crm_module(ctx: TenantCtx = Depends(require_onboarded)) -> None:
    """CRM tier gate (docs/crm-vs-os-architecture.md §1, Julius 2026-07-24): the
    whole CRM API unlocks at Core (£99) via the 'crm' module row — router-level,
    so every endpoint (present and future) inherits it. FastAPI caches the
    require_onboarded sub-dependency, so per-route ctx params cost nothing extra."""
    if "crm" not in db.visible_modules(ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="The CRM is not active for this account")


def _require_crm_door(request: Request, ctx: TenantCtx = Depends(require_onboarded)) -> None:
    """People doors on the JSON API too (security review 2026-09, M3: /api/crm
    ignored them, so staff and accountants could fetch the whole book and an
    accountant could enrol contacts in sequences). Reads need the CRM door;
    writes need "Change customer details"; a person narrowed to their own
    customers uses the pages (which filter) — the whole-book API refuses them."""
    from app import crm_visibility, guards, people
    m = guards.member_of(ctx)
    if m is None:
        return
    if not people.can_open(m, "crm") or crm_visibility.narrows_contacts(m):
        raise HTTPException(status_code=403, detail="You don't have access to the CRM API.")
    if request.method not in ("GET", "HEAD") and not people.can_do(m, "crm", "edit"):
        raise HTTPException(status_code=403, detail="You don't have permission to change the CRM.")


router = APIRouter(prefix="/api/crm", tags=["crm"],
                   dependencies=[Depends(_require_crm_module), Depends(_require_crm_door)])


@router.get("/contacts")
def list_contacts(q: str | None = None, limit: int = 100, offset: int = 0,
                  ctx: TenantCtx = Depends(require_onboarded)):
    limit = max(1, min(limit, 200))
    rows = db.crm_list_contacts(ctx.tenant_id, q=q, limit=limit, offset=max(offset, 0),
                                client=ctx.client)
    return {"contacts": crm.with_recency(rows), "limit": limit, "offset": max(offset, 0)}


@router.get("/contacts/{contact_id}")
def get_contact(contact_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    deals = db.crm_list_deals(ctx.tenant_id, contact_id=contact_id, client=ctx.client)
    activities = db.crm_activities_for_contact(contact_id, ctx.tenant_id, client=ctx.client)
    return {"contact": crm.with_recency([contact])[0], "deals": deals,
            "activities": activities}


@router.get("/companies")
def list_companies(limit: int = 100, offset: int = 0,
                   ctx: TenantCtx = Depends(require_onboarded)):
    limit = max(1, min(limit, 200))
    rows = db.crm_list_companies(ctx.tenant_id, limit=limit, offset=max(offset, 0),
                                 client=ctx.client)
    return {"companies": crm.with_recency(rows), "limit": limit, "offset": max(offset, 0)}


@router.get("/companies/{company_id}")
def get_company(company_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    company = db.crm_get_company(company_id, ctx.tenant_id, client=ctx.client)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    contacts = db.crm_contacts_for_company(company_id, ctx.tenant_id, client=ctx.client)
    deals = db.crm_list_deals(ctx.tenant_id, company_id=company_id, client=ctx.client)
    return {"company": crm.with_recency([company])[0],
            "contacts": crm.with_recency(contacts), "deals": deals}


@router.get("/deals")
def list_deals(ctx: TenantCtx = Depends(require_onboarded)):
    return {"deals": db.crm_list_deals(ctx.tenant_id, client=ctx.client)}


@router.get("/deals/{deal_id}")
def get_deal(deal_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    deal = db.crm_get_deal(deal_id, ctx.tenant_id, client=ctx.client)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    activities = db.crm_activities_for_deal(deal_id, ctx.tenant_id, client=ctx.client)
    return {"deal": deal, "activities": activities}


@router.get("/pipeline")
def pipeline(ctx: TenantCtx = Depends(require_onboarded)):
    stages = crm.ensure_default_stages(ctx.tenant_id, client=ctx.client)
    deals = db.crm_list_deals(ctx.tenant_id, client=ctx.client)
    return {"pipeline": crm.pipeline_view(stages, deals)}


@router.get("/stages")
def list_stages(ctx: TenantCtx = Depends(require_onboarded)):
    return {"stages": crm.ensure_default_stages(ctx.tenant_id, client=ctx.client)}


@router.get("/promises")
def list_promises(contact_id: str | None = None,
                  ctx: TenantCtx = Depends(require_onboarded)):
    return {"promises": db.crm_open_promises(ctx.tenant_id, contact_id=contact_id,
                                             client=ctx.client)}


@router.get("/export")
def export_all(ctx: TenantCtx = Depends(require_onboarded)):
    """The whole CRM in one JSON body — "yours to export" (design §14)."""
    from app import guards as _guards
    _guards.require_admin(ctx)   # the whole book: owner or manager (M3/M4)
    return db.crm_export(ctx.tenant_id, client=ctx.client)


@router.post("/stages", dependencies=[Depends(require_csrf)])
def add_stage(name: str = Form(...), stage_type: str = Form("active"),
              display_order: int = Form(0), ctx: TenantCtx = Depends(require_onboarded)):
    if stage_type not in ("active", "won", "lost", "dormant"):
        raise HTTPException(status_code=422, detail="Unknown stage_type")
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Stage needs a name")
    stage = db.crm_insert_stage(ctx.tenant_id, name, display_order=display_order,
                                stage_type=stage_type, client=ctx.client)
    return {"stage": stage}


# ── Phase 4a: Reports (§19.1) ───────────────────────────────────────────────

@router.get("/reports/pipeline")
def report_pipeline(ctx: TenantCtx = Depends(require_onboarded)):
    """Pipeline summary: deal count + total value by stage, avg days open."""
    return {"pipeline": db.crm_report_pipeline(ctx.tenant_id, client=ctx.client)}


@router.get("/reports/activity")
def report_activity(period: int = 30, ctx: TenantCtx = Depends(require_onboarded)):
    """Activity heatmap: emails sent/received per day over trailing period."""
    period = max(7, min(period, 365))
    return {"activity": db.crm_report_activity(ctx.tenant_id, period=period,
                                               client=ctx.client)}


@router.get("/reports/won-lost")
def report_won_lost(period: int = 90, ctx: TenantCtx = Depends(require_onboarded)):
    """Won/Lost analysis: deals closed in period, win rate, avg value."""
    period = max(7, min(period, 365))
    return {"won_lost": db.crm_report_won_lost(ctx.tenant_id, period=period,
                                               client=ctx.client)}


# ── Phase 4a: Tasks (§19.2) ─────────────────────────────────────────────────

@router.get("/tasks")
def list_tasks(status: str = "open", contact_id: Optional[str] = None,
               deal_id: Optional[str] = None,
               ctx: TenantCtx = Depends(require_onboarded)):
    """List tasks, filterable by status, contact, or deal."""
    if status not in ("open", "done", "cancelled", "all"):
        status = "open"
    return {"tasks": db.crm_list_tasks(ctx.tenant_id, status=status,
                                       contact_id=contact_id, deal_id=deal_id,
                                       client=ctx.client)}


@router.get("/tasks/today")
def tasks_today(ctx: TenantCtx = Depends(require_onboarded)):
    """Overdue + due-today tasks for the Today dashboard card."""
    return {"tasks": db.crm_tasks_due_today(ctx.tenant_id, client=ctx.client)}


@router.post("/tasks", dependencies=[Depends(require_csrf)])
def create_task(title: str = Form(...), description: str = Form(""),
                due_date: Optional[str] = Form(None),
                priority: str = Form("normal"),
                contact_id: Optional[str] = Form(None),
                company_id: Optional[str] = Form(None),
                deal_id: Optional[str] = Form(None),
                ctx: TenantCtx = Depends(require_onboarded)):
    """Create a manual task."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Task needs a title")
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"
    parsed_due = None
    if due_date:
        try:
            parsed_due = date.fromisoformat(due_date.strip())
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid due_date (use YYYY-MM-DD)")
    task = db.crm_create_task(ctx.tenant_id, title=title,
                              description=description.strip() or None,
                              due_date=parsed_due, priority=priority,
                              contact_id=contact_id or None,
                              company_id=company_id or None,
                              deal_id=deal_id or None, source="manual")
    return {"task": task}


@router.patch("/tasks/{task_id}", dependencies=[Depends(require_csrf)])
def update_task(task_id: str, title: Optional[str] = Form(None),
                due_date: Optional[str] = Form(None),
                priority: Optional[str] = Form(None),
                status: Optional[str] = Form(None),
                ctx: TenantCtx = Depends(require_onboarded)):
    """Update task fields."""
    updates = {}
    if title is not None:
        title = title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Task needs a title")
        updates["title"] = title
    if due_date is not None:
        try:
            updates["due_date"] = date.fromisoformat(due_date.strip()) if due_date.strip() else None
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid due_date")
    if priority is not None and priority in ("low", "normal", "high", "urgent"):
        updates["priority"] = priority
    if status is not None and status in ("open", "done", "cancelled"):
        updates["status"] = status
        if status == "done":
            updates["completed_at"] = datetime.now(timezone.utc).isoformat()
    if not updates:
        raise HTTPException(status_code=422, detail="Nothing to update")
    db.crm_update_task(task_id, ctx.tenant_id, updates, client=ctx.client)
    return {"ok": True}


@router.post("/tasks/{task_id}/complete", dependencies=[Depends(require_csrf)])
def complete_task(task_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Quick-complete a task."""
    db.crm_update_task(task_id, ctx.tenant_id, {
        "status": "done",
        "completed_at": datetime.now(timezone.utc).isoformat()
    }, client=ctx.client)
    return {"ok": True}


# ── Phase 4b: Sequences (§19.3) ─────────────────────────────────────────────

def _require_sequence(sequence_id: str, ctx: TenantCtx) -> dict:
    seq = db.crm_get_sequence(sequence_id, ctx.tenant_id, client=ctx.client)
    if seq is None:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return seq


@router.get("/sequences")
def list_sequences(ctx: TenantCtx = Depends(require_onboarded)):
    return {"sequences": db.crm_list_sequences(ctx.tenant_id, client=ctx.client)}


@router.get("/sequences/{sequence_id}")
def get_sequence(sequence_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    seq = _require_sequence(sequence_id, ctx)
    return {"sequence": seq,
            "steps": db.crm_list_steps(sequence_id, ctx.tenant_id, client=ctx.client)}


@router.post("/sequences", dependencies=[Depends(require_csrf)])
def create_sequence(name: str = Form(...), description: str = Form(""),
                    trigger_type: str = Form("manual"),
                    ctx: TenantCtx = Depends(require_onboarded)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Sequence needs a name")
    if trigger_type not in crm_sequences.TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail="Unknown trigger_type")
    if db.crm_count_active_sequences(ctx.tenant_id, client=ctx.client) >= \
            crm_sequences.MAX_ACTIVE_SEQUENCES:
        raise HTTPException(status_code=422,
                            detail=f"Max {crm_sequences.MAX_ACTIVE_SEQUENCES} active sequences")
    seq = db.crm_create_sequence(ctx.tenant_id, name=name,
                                 description=description.strip() or None,
                                 trigger_type=trigger_type, client=ctx.client)
    return {"sequence": seq}


@router.patch("/sequences/{sequence_id}", dependencies=[Depends(require_csrf)])
def update_sequence(sequence_id: str, name: Optional[str] = Form(None),
                    description: Optional[str] = Form(None),
                    trigger_type: Optional[str] = Form(None),
                    status: Optional[str] = Form(None),
                    ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    updates: dict = {}
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Sequence needs a name")
        updates["name"] = name
    if description is not None:
        updates["description"] = description.strip() or None
    if trigger_type is not None:
        if trigger_type not in crm_sequences.TRIGGER_TYPES:
            raise HTTPException(status_code=422, detail="Unknown trigger_type")
        updates["trigger_type"] = trigger_type
    if status is not None:
        if status not in ("active", "paused", "archived"):
            raise HTTPException(status_code=422, detail="Unknown status")
        updates["status"] = status
    if not updates:
        raise HTTPException(status_code=422, detail="Nothing to update")
    db.crm_update_sequence(sequence_id, ctx.tenant_id, updates, client=ctx.client)
    return {"ok": True}


@router.delete("/sequences/{sequence_id}", dependencies=[Depends(require_csrf)])
def archive_sequence(sequence_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Soft-delete: archive the sequence (status='archived')."""
    _require_sequence(sequence_id, ctx)
    db.crm_update_sequence(sequence_id, ctx.tenant_id, {"status": "archived"},
                           client=ctx.client)
    return {"ok": True}


@router.get("/sequences/{sequence_id}/steps")
def list_steps(sequence_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    return {"steps": db.crm_list_steps(sequence_id, ctx.tenant_id, client=ctx.client)}


@router.post("/sequences/{sequence_id}/steps", dependencies=[Depends(require_csrf)])
def add_step(sequence_id: str, delay_days: int = Form(3),
             action_type: str = Form("send_email"),
             template_subject: str = Form(""), template_body: str = Form(""),
             exit_on_reply: bool = Form(True), stage_id: Optional[str] = Form(None),
             ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    if action_type not in crm_sequences.ACTION_TYPES:
        raise HTTPException(status_code=422, detail="Unknown action_type")
    existing = db.crm_list_steps(sequence_id, ctx.tenant_id, client=ctx.client)
    if len(existing) >= crm_sequences.MAX_STEPS:
        raise HTTPException(status_code=422,
                            detail=f"Max {crm_sequences.MAX_STEPS} steps per sequence")
    side_effects = [{"action": "move_stage", "stage_id": stage_id}] \
        if action_type == "update_stage" and stage_id else []
    step = db.crm_add_step(
        ctx.tenant_id, sequence_id, step_order=len(existing),
        delay_days=max(0, delay_days), action_type=action_type,
        template_subject=template_subject.strip() or None,
        template_body=template_body.strip() or None,
        exit_on_reply=exit_on_reply, side_effects=side_effects, client=ctx.client)
    return {"step": step}


@router.patch("/sequences/{sequence_id}/steps/{step_id}",
              dependencies=[Depends(require_csrf)])
def update_step(sequence_id: str, step_id: str, delay_days: Optional[int] = Form(None),
                action_type: Optional[str] = Form(None),
                template_subject: Optional[str] = Form(None),
                template_body: Optional[str] = Form(None),
                exit_on_reply: Optional[bool] = Form(None),
                step_order: Optional[int] = Form(None),
                ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    updates: dict = {}
    if delay_days is not None:
        updates["delay_days"] = max(0, delay_days)
    if action_type is not None:
        if action_type not in crm_sequences.ACTION_TYPES:
            raise HTTPException(status_code=422, detail="Unknown action_type")
        updates["action_type"] = action_type
    if template_subject is not None:
        updates["template_subject"] = template_subject.strip() or None
    if template_body is not None:
        updates["template_body"] = template_body.strip() or None
    if exit_on_reply is not None:
        updates["exit_on_reply"] = exit_on_reply
    if step_order is not None:
        updates["step_order"] = max(0, step_order)
    if not updates:
        raise HTTPException(status_code=422, detail="Nothing to update")
    db.crm_update_step(step_id, ctx.tenant_id, updates, client=ctx.client)
    return {"ok": True}


@router.delete("/sequences/{sequence_id}/steps/{step_id}",
               dependencies=[Depends(require_csrf)])
def delete_step(sequence_id: str, step_id: str,
                ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    db.crm_delete_step(step_id, ctx.tenant_id, client=ctx.client)
    return {"ok": True}


@router.post("/sequences/{sequence_id}/enroll", dependencies=[Depends(require_csrf)])
def enroll(sequence_id: str, contact_id: str = Form(...),
           deal_id: Optional[str] = Form(None),
           ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    enrollment = db.crm_enroll(ctx.tenant_id, sequence_id, contact_id,
                               deal_id=deal_id or None, client=ctx.client)
    if enrollment is None:
        raise HTTPException(
            status_code=422,
            detail="Could not enroll — contact opted out, already enrolled, or no steps")
    return {"enrollment": enrollment}


@router.get("/sequences/{sequence_id}/enrollments")
def list_enrollments(sequence_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_sequence(sequence_id, ctx)
    return {"enrollments": db.crm_list_enrollments(ctx.tenant_id, sequence_id=sequence_id,
                                                   client=ctx.client)}


@router.post("/enrollments/{enrollment_id}/pause", dependencies=[Depends(require_csrf)])
def pause_enrollment(enrollment_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    if db.crm_get_enrollment(enrollment_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    db.crm_set_enrollment_status(enrollment_id, ctx.tenant_id, "paused", client=ctx.client)
    return {"ok": True}


@router.post("/enrollments/{enrollment_id}/exit", dependencies=[Depends(require_csrf)])
def exit_enrollment(enrollment_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    if db.crm_get_enrollment(enrollment_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    db.crm_set_enrollment_status(enrollment_id, ctx.tenant_id, "exited", client=ctx.client)
    return {"ok": True}
