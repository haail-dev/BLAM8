"""Owner/manager controls for the optional tenant outreach module."""
import csv
import io
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import db, guards, tenant_outreach as outreach
from app.auth import TenantCtx, require_onboarded, require_subscription
from app.security import require_csrf
from app.templating import templates
from fastapi.concurrency import run_in_threadpool


def require_outreach(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    guards.require_admin(ctx)
    if "crm" not in db.visible_modules(ctx.tenant_id, client=ctx.client):
        raise HTTPException(404, "CRM is not available for this business.")
    if request.method != "GET" and not db.module_active(ctx.tenant_id, "crm", client=ctx.client):
        raise HTTPException(403, "Activate CRM before using outreach.")
    return ctx


router = APIRouter(prefix="/app/outreach", tags=["outreach"], dependencies=[Depends(require_subscription)])


def redirect(message="", error=""):
    return RedirectResponse("/app/outreach?" + urlencode({"msg": message, "err": error}), status_code=303)


def config_from(form):
    return outreach.validate_config({"mode": str(form.get("mode", "draft")),
        "timezone": str(form.get("timezone", "Europe/London")),
        "working_days": [int(v) for v in form.getlist("working_days")],
        "holidays": str(form.get("holidays", "")).split(),
        "start_time": str(form.get("start_time", "09:00")), "end_time": str(form.get("end_time", "17:00")),
        "daily_cap": int(form.get("daily_cap", 50)), "min_gap_seconds": int(form.get("min_gap_seconds", 150))})


@router.get("")
def page(request: Request, ctx: TenantCtx = Depends(require_outreach)):
    from app.dashboard import _ctx
    tid, client = ctx.tenant_id, ctx.client
    lists = outreach.rows("outreach_lists", tid, client)
    targets = outreach.rows("outreach_targets", tid, client)
    suppressions = outreach.rows("outreach_suppressions", tid, client, order="email")
    suppressed = {s["email"]: s["reason"] for s in suppressions}
    for l in lists:
        l["targets"] = [t | {"blocked": suppressed.get(t["email"], "")} for t in targets if t["list_id"] == l["id"]]
    campaigns = outreach.rows("outreach_campaigns", tid, client)
    messages = outreach.rows("outreach_messages", tid, client)
    for c in campaigns:
        c["messages"] = [m for m in messages if m["campaign_id"] == c["id"]]
        c["enrollments"] = db.crm_list_enrollments(tid, sequence_id=c["sequence_id"], client=client)
        by_contact = {t["contact_id"]: t["email"] for t in targets if t["list_id"] == c["list_id"]}
        for e in c["enrollments"]:
            e["email"] = by_contact.get(e["contact_id"], "Recipient no longer in list")
        c["draft_count"] = sum(m["status"] == "draft" for m in c["messages"])
        c["sent_count"] = sum(m["status"] == "sent" for m in c["messages"])
    return templates.TemplateResponse(request, "app/outreach.html", _ctx("outreach", ctx,
        outreach_settings=outreach.settings(tid, client), lists=lists, campaigns=campaigns,
        sequences=db.crm_list_sequences(tid, client=client), identities=outreach.identities(tid, client),
        defaults=outreach.DEFAULTS, contacts=db.crm_list_contacts(tid, q=request.query_params.get("q"), limit=200, client=client),
        msg=request.query_params.get("msg", ""), err=request.query_params.get("err", "")))


@router.get("/template.csv")
def csv_template(ctx: TenantCtx = Depends(require_outreach)):
    buf = io.StringIO()
    csv.writer(buf).writerow(["name", "email", "basis", "evidence", "email_source", "company_number", "email_verified", "soft_match"])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=outreach-list.csv"})


@router.post("/settings", dependencies=[Depends(require_csrf)])
async def save_settings(request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            cap = int(form.get("daily_cap", 100))
            if not 1 <= cap <= 1000:
                raise ValueError("Shared cap must be between 1 and 1,000.")
            db.db().table("outreach_settings").upsert({"tenant_id": ctx.tenant_id,
                "enabled": form.get("enabled") == "on", "daily_cap": cap}, on_conflict="tenant_id").execute()
            return redirect("Outreach settings saved.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/lists", dependencies=[Depends(require_csrf)])
async def create_list(request: Request, ctx: TenantCtx = Depends(require_outreach)):
    name = str((await request.form()).get("name", "")).strip()
    def _blocking():
        if not name or len(name) > 100:
            return redirect(error="Give the list a name of up to 100 characters.")
        db.db().table("outreach_lists").insert({"tenant_id": ctx.tenant_id, "name": name}).execute()
        return redirect("List created.")
    return await run_in_threadpool(_blocking)


@router.post("/lists/{list_id}/import", dependencies=[Depends(require_csrf)])
async def import_list(list_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    try:
        upload = form.get("file")
        if not upload or not hasattr(upload, "read"):
            raise ValueError("Choose a CSV file.")
        data = await upload.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("CSV must be smaller than 1 MB.")
        count = outreach.import_csv(ctx.tenant_id, list_id, data.decode("utf-8-sig"))
        return redirect(f"Added {count} contacts. Existing list entries were kept.")
    except (ValueError, UnicodeError) as exc:
        return redirect(error=str(exc))


@router.post("/lists/{list_id}/contact", dependencies=[Depends(require_csrf)])
async def add_contact(list_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            contact = db.crm_get_contact(str(form.get("contact_id", "")), ctx.tenant_id, client=ctx.client)
            if not contact:
                raise ValueError("Choose a contact from this business.")
            row = dict(form) | {"email": contact.get("email"), "name": contact.get("name"),
                "email_verified": "true" if form.get("email_verified") == "on" else "false",
                "soft_match": "false" if form.get("identity_confirmed") == "on" else "true"}
            count = outreach.import_targets(ctx.tenant_id, list_id, [row])
            return redirect(f"Added {count} contacts.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/targets/{target_id}", dependencies=[Depends(require_csrf)])
async def verify_target(target_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            old = outreach.get("outreach_targets", ctx.tenant_id, target_id, ctx.client)
            row = outreach.validate_target(dict(form) | {"email": old["email"],
                "email_verified": "true" if form.get("email_verified") == "on" else "false",
                "soft_match": "false" if form.get("identity_confirmed") == "on" else "true"})
            row.pop("name")
            outreach.update("outreach_targets", ctx.tenant_id, target_id, row)
            return redirect("Evidence saved. Permanent stops remain in force.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/campaigns", dependencies=[Depends(require_csrf)])
async def create_campaign(request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            outreach.create_campaign(ctx.tenant_id, str(form.get("name", "")), str(form.get("list_id", "")),
                str(form.get("sequence_id", "")), str(form.get("mailbox_id", "")), config_from(form))
            return redirect("Campaign created paused. Add eligible recipients, then resume when ready.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/campaigns/{campaign_id}", dependencies=[Depends(require_csrf)])
async def edit_campaign(campaign_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            outreach.get("outreach_campaigns", ctx.tenant_id, campaign_id, ctx.client)
            box = outreach.identity(ctx.tenant_id, str(form.get("mailbox_id", "")))
            outreach.update("outreach_campaigns", ctx.tenant_id, campaign_id,
                {"config": config_from(form), "mailbox_id": box["id"], "enabled": False,
                 "last_result": "Settings changed; refresh existing drafts before sending"})
            return redirect("Saved and paused. Refresh any drafts created with the previous settings.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/campaigns/{campaign_id}/control", dependencies=[Depends(require_csrf)])
async def control(campaign_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        action = form.get("action")
        try:
            c = outreach.get("outreach_campaigns", ctx.tenant_id, campaign_id, ctx.client)
            if action == "enroll":
                return redirect(f"Added {outreach.enroll_list(ctx.tenant_id, campaign_id)} eligible recipients to the sequence.")
            if action not in ("pause", "resume"):
                raise ValueError("Unknown campaign action.")
            if action == "resume":
                outreach.identity(ctx.tenant_id, c["mailbox_id"])
                outreach.validate_config(c["config"])
            outreach.update("outreach_campaigns", ctx.tenant_id, campaign_id,
                {"enabled": action == "resume", "last_result": "Waiting for sequence sweep" if action == "resume" else "Paused"})
            return redirect("Campaign " + ("resumed." if action == "resume" else "paused."))
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/suppress", dependencies=[Depends(require_csrf)])
async def suppress(request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            address = outreach.email(form.get("email"))
            outreach.suppress(ctx.tenant_id, address, str(form.get("reason") or "Do not contact")[:300])
            return redirect("Address permanently excluded from this business's outreach.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)


@router.post("/messages/{message_id}/review", dependencies=[Depends(require_csrf)])
async def review(message_id: str, request: Request, ctx: TenantCtx = Depends(require_outreach)):
    form = await request.form()
    def _blocking():
        try:
            if form.get("action") == "confirm_sent" and form.get("checked_sent") != "on":
                raise ValueError("Check the sending mailbox's Sent folder and confirm acceptance first.")
            outreach.review_message(ctx.tenant_id, message_id, str(form.get("action")))
            return redirect("Send record updated.")
        except ValueError as exc:
            return redirect(error=str(exc))
    return await run_in_threadpool(_blocking)
