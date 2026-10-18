"""Operator console — the platform-admin area at /admin.

A separate login from the tenant dashboard, then the cross-tenant margin view.
Every data route is gated by require_admin; reads use the service role and show
commercial AGGREGATES only (tokens, cost, margin) — never tenant email content.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from app import auth, build_requests, db, llm_admin, support, usage, voice_crm
from app.admin_auth import AdminCtx, clear_admin_cookies, require_admin, set_admin_cookies
from app.config import settings
from app.security import rate_limit, require_csrf
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])












@router.get("/outreach")
def outreach_page(request: Request, ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    current = outreach.state()
    cfg = outreach.validate_config(current["config"])
    messages = outreach.rows("admin_outreach_messages")
    return templates.TemplateResponse(request, "admin/outreach.html", {
        "ctx": ctx, "current": current, "cfg": cfg,
        "plan_text": "\n".join(f"{day}, {count}" for day, count in sorted(cfg["plan"].items())),
        "missing": outreach.readiness(cfg),
        "drafts": sorted([m for m in messages if m["status"] in ("draft", "approved")], key=lambda m: m["created_at"])[:100],
        "uncertain": [m for m in messages if m["status"] in ("sending", "unknown")],
        "prospects": outreach.rows("vcrm_prospects", lambda q: q.eq("kind", cfg["kind"])),
        "digests": db.db().table("admin_outreach_digests").select("*").order("day", desc=True).limit(14).execute().data or [],
        "notice": request.query_params.get("notice", ""),
    })


@router.post("/outreach/settings", dependencies=[Depends(require_csrf)])
async def outreach_settings(request: Request, ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    try:
        form = await request.form()
        if "config_json" in form:
            cfg = outreach.validate_config(json.loads(str(form["config_json"])))
        else:
            raw = outreach.validate_config(outreach.state()["config"])
            for key in ("name", "kind", "mode", "timezone", "start_time", "end_time", "sender",
                        "unsubscribe_email", "stop_line", "inbox_tenant_id", "fallback_line"):
                raw[key] = str(form.get(key, "")).strip()
            for key in ("daily_cap", "min_gap_seconds"):
                raw[key] = int(str(form.get(key, "")))
            raw["working_days"] = [int(str(day)) for day in form.getlist("working_days")]
            for key in ("holidays", "legacy_subjects", "reply_to"):
                raw[key] = [line.strip() for line in str(form.get(key, "")).splitlines() if line.strip()]
            raw["plan"] = {}
            for line in str(form.get("plan_text", "")).splitlines():
                if line.strip():
                    day, count = line.split(",", 1)
                    raw["plan"][day.strip()] = int(count.strip())
            for i, step in enumerate(raw["steps"]):
                step["template_subject"] = str(form.get(f"subject_{i}", "")).strip()
                step["template_body"] = str(form.get(f"body_{i}", "")).strip()
            raw["steps"][1]["delay_days"] = int(str(form.get("followup_days", "7")))
            cfg = outreach.validate_config(raw)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # Always pause on a settings change; approval applies to the exact saved payload.
    outreach.update_state({"config": cfg, "enabled": False})
    return RedirectResponse("/admin/outreach?notice=Settings+saved.+Sending+paused.+Prepare+fresh+drafts+before+resuming.", status_code=303)


@router.post("/outreach/control", dependencies=[Depends(require_csrf)])
def outreach_control(action: str = Form(...), ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    if action not in ("pause", "resume", "preview"):
        raise HTTPException(400, "Unknown outreach action")
    if action == "preview":
        outreach.run(preview=True)
    elif action == "pause":
        outreach.update_state({"enabled": False})
    else:
        cfg = outreach.validate_config(outreach.state()["config"])
        if outreach.readiness(cfg):
            raise HTTPException(400, "Complete the outreach settings and server keys first")
        outreach.update_state({"enabled": True})
    return RedirectResponse("/admin/outreach", status_code=303)


@router.post("/outreach/approve/{message_id}", dependencies=[Depends(require_csrf)])
def outreach_approve(message_id: str, ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    cfg = outreach.validate_config(outreach.state()["config"])
    db.db().table("admin_outreach_messages").update({"status": "approved"}).eq("id", message_id).eq("status", "draft").eq("config_snapshot", json.dumps(cfg)).execute()
    return RedirectResponse("/admin/outreach", status_code=303)


@router.post("/outreach/suppress", dependencies=[Depends(require_csrf)])
def outreach_suppress(email: str = Form(...), ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    try:
        outreach.suppress(email, "Operator: never contact again", outreach.state()["config"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/admin/outreach", status_code=303)


@router.post("/outreach/reconcile/{message_id}", dependencies=[Depends(require_csrf)])
def outreach_reconcile(message_id: str, provider_id: str = Form(""), ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    try:
        outreach.reconcile(message_id, provider_id.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/admin/outreach", status_code=303)


@router.post("/outreach/import", dependencies=[Depends(require_csrf)])
async def outreach_import(file: UploadFile = File(...), ctx: AdminCtx = Depends(require_admin)):
    from app import admin_outreach as outreach
    content = await file.read(2_000_001)
    if len(content) > 2_000_000:
        raise HTTPException(400, "CSV must be under 2 MB")
    try:
        count = outreach.import_csv(content.decode("utf-8-sig"), outreach.validate_config(outreach.state()["config"])["kind"])
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/admin/outreach?notice=Imported+{count}+new+prospects", status_code=303)


@router.post("/outreach/verify/{prospect_id}", dependencies=[Depends(require_csrf)])
def outreach_verify(prospect_id: str, company_number: str = Form(...), email_source: str = Form(...),
                    email_verified: bool = Form(False), soft_match: bool = Form(True),
                    ctx: AdminCtx = Depends(require_admin)):
    if not db.vcrm_get_prospect(prospect_id):
        raise HTTPException(404, "Prospect not found")
    db.vcrm_update_prospect(prospect_id, {"company_number": company_number.strip(), "email_source": email_source.strip(),
                                        "email_verified": email_verified, "soft_match": soft_match})
    return RedirectResponse("/admin/outreach", status_code=303)


# ── login / logout (separate session from the tenant dashboard) ──────────────
@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"error": None, "email": ""})


@router.post("/login", dependencies=[Depends(rate_limit("admin_login", 10, 300)), Depends(require_csrf)])
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        session = await auth.password_login(email.strip(), password)
    except ValueError as e:
        return templates.TemplateResponse(request, "admin/login.html",
                                          {"error": str(e), "email": email}, status_code=401)
    user = session.get("user") or {}
    if not user.get("id") or not db.is_platform_admin(user["id"]):
        return templates.TemplateResponse(request, "admin/login.html",
                                          {"error": "This account is not a platform admin.", "email": email},
                                          status_code=403)
    resp = RedirectResponse("/admin", status_code=303)
    set_admin_cookies(resp, session)
    return resp


@router.get("/logout")
def logout(request: Request):
    """Ends the console session at Supabase and in the browser — and the
    business session beside it (security review M5/M6: signing out of the
    console left a live tenant session in the same browser)."""
    from app import admin_auth as _aa, auth as _auth
    _auth.revoke_session(request.cookies.get(_aa.ADMIN_AT_COOKIE))
    _auth.revoke_session(request.cookies.get(_auth.AT_COOKIE))
    resp = RedirectResponse("/admin/login", status_code=303)
    clear_admin_cookies(resp)
    _auth.clear_session_cookies(resp)
    return resp


# ── console ──────────────────────────────────────────────────────────────────
def _unseen() -> int:
    """The Requests-badge count for the shared admin nav. Best-effort — a read
    failure must never take a console page down with it."""
    try:
        return db.count_unseen_feature_requests()
    except Exception:
        return 0


def _pending_os() -> int:
    """The OS-access-badge count for the shared admin nav (founding-partner
    requests awaiting a decision). Best-effort, like _unseen()."""
    try:
        return db.count_pending_os_requests()
    except Exception:
        return 0


@router.get("")
@router.get("/")
def overview(request: Request, ctx: AdminCtx = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/overview.html",
                                      {"ctx": ctx, "s": usage.operator_summary(),
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.get("/live-allowance")
def live_allowance_page(request: Request, ctx: AdminCtx = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/live_allowance.html", {
        "ctx": ctx, "enabled": db.concierge_live_rollout(),
        "limits": db.concierge_live_tier_limits(),
        "unseen_requests": _unseen(), "pending_os_requests": _pending_os(),
    })


@router.post("/live-allowance", dependencies=[Depends(require_csrf)])
def save_live_allowance(enabled: str = Form(""),
                        starter: int = Form(10), pro: int = Form(10),
                        core: int = Form(10), enterprise: int = Form(10),
                        ctx: AdminCtx = Depends(require_admin)):
    values = {"starter": starter, "pro": pro, "core": core, "enterprise": enterprise}
    if any(not 0 <= value <= 1440 for value in values.values()):
        raise HTTPException(status_code=422, detail="Allowances must be between 0 and 1,440 minutes.")
    for tier, minutes in values.items():
        db.set_concierge_live_tier_limit(tier, minutes, ctx.user_id)
    db.set_concierge_live_rollout(enabled == "on", ctx.user_id)
    return RedirectResponse("/admin/live-allowance", status_code=303)


@router.get("/tenants/{tenant_id}")
def tenant_detail(request: Request, tenant_id: str, ctx: AdminCtx = Depends(require_admin)):
    from app import journey
    return templates.TemplateResponse(request, "admin/tenant.html",
                                      {"ctx": ctx, "d": usage.operator_tenant_detail(tenant_id),
                                       "journey": journey.tenant_journey(tenant_id),
                                       "tenant_id": tenant_id, "rate": settings.usd_gbp_rate,
                                       "modules": db.active_modules(tenant_id),
                                       "trec": db.get_tenant(tenant_id) or {},
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/tenants/{tenant_id}/os-access", dependencies=[Depends(require_csrf)])
def tenant_os_access(tenant_id: str, granted: str = Form("off"),
                     ctx: AdminCtx = Depends(require_admin)):
    """Grant/revoke founding OS-tier access directly from the tenant page —
    for partners recruited in person who never filed a request. Revoking only
    re-locks the CHECKOUT; an existing subscription keeps working."""
    db.set_os_access_approved(tenant_id, granted == "on")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/revenue", dependencies=[Depends(require_csrf)])
def set_revenue(tenant_id: str, contract_monthly_gbp: str = Form(""), ctx: AdminCtx = Depends(require_admin)):
    val = contract_monthly_gbp.strip()
    db.set_tenant_contract(tenant_id, float(val) if val else None)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/reporting", dependencies=[Depends(require_csrf)])
def set_reporting(tenant_id: str, reporting_tier: str = Form(...), ctx: AdminCtx = Depends(require_admin)):
    db.set_tenant_reporting_tier(tenant_id, "enterprise" if reporting_tier == "enterprise" else "standard")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/comp", dependencies=[Depends(require_csrf)])
def set_comp(tenant_id: str, comp_mode: str = Form("off"), comp_until: str = Form(""),
             ctx: AdminCtx = Depends(require_admin)):
    """Grant/revoke free (comped) access — indefinite or until a date — from the
    console, independent of tier and Stripe. Honoured by the access gate
    (app/trial.evaluate); the Stripe webhook never writes these fields, so a
    subscription event can't revoke it. For free-for-feedback users etc."""
    mode = (comp_mode or "off").strip()
    if mode == "indefinite":
        db.set_tenant_comp(tenant_id, "indefinite", None)
    elif mode == "until_date" and (comp_until or "").strip():
        # HTML date input gives YYYY-MM-DD; store end-of-day UTC.
        db.set_tenant_comp(tenant_id, "until_date", f"{comp_until.strip()}T23:59:59+00:00")
    else:
        db.set_tenant_comp(tenant_id, None, None)   # 'off', or 'until' with no date
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/fair-use", dependencies=[Depends(require_csrf)])
def set_fair_use(tenant_id: str, exempt: str = Form(""), ctx: AdminCtx = Depends(require_admin)):
    """No daily limit on the Concierge for this account (src/app/fairuse.py) — for
    the operator's own rigs. Separate from comp on purpose: comp is free
    access, and real customers are comped too."""
    db.set_tenant_fair_use_exempt(tenant_id, exempt == "on")
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/account", dependencies=[Depends(require_csrf)])
def set_account_type(tenant_id: str, account_type: str = Form(...), ctx: AdminCtx = Depends(require_admin)):
    """The ONE Enterprise switch (multi-mailbox spec D2). Flipping to Enterprise
    sets the plan AND the reporting tier in one decision; reporting can still be
    flipped back independently afterwards. Downgrading is blocked while extra
    mailboxes are connected — tokens are never silently dropped."""
    if account_type == "enterprise":
        db.set_tenant_plan(tenant_id, "enterprise")
        db.set_tenant_reporting_tier(tenant_id, "enterprise")
    else:
        if len(db.list_mailboxes(tenant_id)) > 1:
            return RedirectResponse(f"/admin/tenants/{tenant_id}?err=mailboxes", status_code=303)
        db.set_tenant_plan(tenant_id, usage.DEFAULT_PLAN)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


# The modules an operator can flip per tenant. 'email' is always-on (no row), and
# roadmap modules stay un-flippable until they exist — extend this set per launch.
@router.get("/requests")
def feature_requests(request: Request, stage: str = "", msg: str = "",
                     ctx: AdminCtx = Depends(require_admin)):
    """Every tenant's requests & reports, newest first — the cross-account roadmap
    signal + bug queue. Service role; tenant names resolved for display. Opening
    the page stamps everything seen (clears the nav badge); rows still unseen at
    render are highlighted as new."""
    reqs = db.list_all_feature_requests()
    # Anonymous website tickets have no tenant — skip the name lookup for those.
    names = {tid: ((db.get_tenant(tid) or {}).get("name") or tid[:8])
             for tid in {r.get("tenant_id") for r in reqs} if tid}
    try:
        db.mark_feature_requests_seen()
    except Exception:
        pass   # the badge just clears next time — never block the page
    stories = {r["id"]: build_requests.storyline(r) for r in reqs}
    counts = build_requests.stage_counts(reqs)
    if stage in counts:
        reqs = [r for r in reqs if (
            "closed" if stories[r["id"]]["closed"] else stories[r["id"]]["current"]) == stage]
    return templates.TemplateResponse(request, "admin/requests.html",
                                      {"ctx": ctx, "requests": reqs, "tenant_names": names,
                                       "stories": stories, "stage_counts": counts,
                                       "stage_filter": stage, "stages": build_requests.STAGES,
                                       "flash": _BUILD_FLASH.get(msg) or (msg[:200] if msg.startswith("err:") else ""),
                                       "github_ready": build_requests.configured(),
                                       "build_sources": build_requests.SOURCES,
                                       "issue_url": build_requests.issue_url,
                                       "pr_url": build_requests.pr_url,
                                       "status_labels": support.STATUS_LABELS,
                                       "ticket_statuses": support.TICKET_STATUSES,
                                       "feature_statuses": support.FEATURE_STATUSES,
                                       "category_labels": support.CATEGORY_LABELS,
                                       "unseen_requests": 0,
                                       "pending_os_requests": _pending_os()})


_BUILD_FLASH = {"built": "Sent to the builder.", "planok": "Plan approved. The build starts shortly."}


@router.post("/requests/{request_id}/build", dependencies=[Depends(require_csrf)])
def build_request_dispatch(request_id: str, source: str = Form(""), notes: str = Form(""),
                           ctx: AdminCtx = Depends(require_admin)):
    """Approve & build: open the labelled GitHub issue the build-request workflow
    picks up. One dispatch per request (github_issue is the guard)."""
    row = db.get_feature_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="No such request.")
    if row.get("github_issue"):
        return RedirectResponse("/admin/requests?msg=err:Already%20sent%20to%20the%20builder.", status_code=303)
    src = source if source in build_requests.SOURCES else build_requests.default_source(row)
    try:
        issue = build_requests.create_issue(row, src, notes)
    except build_requests.BuildRequestError as e:
        from urllib.parse import quote
        return RedirectResponse(f"/admin/requests?msg=err:{quote(str(e)[:180])}", status_code=303)
    db.update_feature_request_build(
        request_id, {"github_issue": issue["number"], "source": src, "status": "approved"},
        build_requests.make_event("dispatched", f"Sent to builder as {src} request", issue["url"]))
    return RedirectResponse("/admin/requests?msg=built", status_code=303)


@router.post("/requests/{request_id}/approve-plan", dependencies=[Depends(require_csrf)])
def build_request_approve_plan(request_id: str, ctx: AdminCtx = Depends(require_admin)):
    """Add the plan-approved label: the second human gate before any code is written."""
    row = db.get_feature_request(request_id)
    if not row or not row.get("github_issue"):
        raise HTTPException(status_code=404, detail="Not in the build loop.")
    try:
        build_requests.approve_plan(row["github_issue"])
    except build_requests.BuildRequestError as e:
        from urllib.parse import quote
        return RedirectResponse(f"/admin/requests?msg=err:{quote(str(e)[:180])}", status_code=303)
    db.update_feature_request_build(request_id, {}, build_requests.make_event(
        "plan_approved", "Plan approved by owner"))
    return RedirectResponse("/admin/requests?msg=planok", status_code=303)


@router.post("/requests/{request_id}/status", dependencies=[Depends(require_csrf)])
def set_request_status(request_id: str, status: str = Form(...),
                       status_note: str = Form(""),
                       ctx: AdminCtx = Depends(require_admin)):
    """Set one request/ticket's status (+ the note quoted back to the user on a
    chatbot status check). Only the team changes status — there is no tenant or
    visitor path to this."""
    allowed = set(support.TICKET_STATUSES) | set(support.FEATURE_STATUSES)
    if status in allowed:
        db.update_feature_request_status(request_id, status, status_note[:500])
    return RedirectResponse("/admin/requests", status_code=303)


# ── OS founding-partner access queue ─────────────────────────────────────────
@router.get("/os-access")
def os_access_queue(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """The founding-partner vetting queue: every "Request founding access" from
    /billing/plans, newest first. Approve unlocks that tenant's OS-tier checkout
    (tenants.os_access_approved — the flag billing_routes enforces); dismiss
    parks it. Tenants recruited in person can be granted directly from their
    tenant page without a request."""
    reqs = db.list_os_access_requests()
    tenants = {tid: (db.get_tenant(tid) or {})
               for tid in {r["tenant_id"] for r in reqs}}
    return templates.TemplateResponse(request, "admin/os_access.html",
                                      {"ctx": ctx, "requests": reqs, "tenants": tenants,
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/os-access/{request_id}/approve", dependencies=[Depends(require_csrf)])
def os_access_approve(request_id: str, ctx: AdminCtx = Depends(require_admin)):
    db.approve_os_access_request(request_id)
    return RedirectResponse("/admin/os-access", status_code=303)


@router.post("/os-access/{request_id}/dismiss", dependencies=[Depends(require_csrf)])
def os_access_dismiss(request_id: str, ctx: AdminCtx = Depends(require_admin)):
    db.dismiss_os_access_request(request_id)
    return RedirectResponse("/admin/os-access", status_code=303)


# ── Gmail connection invites ─────────────────────────────────────────────────
_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L lookalikes


def _mint_invite_code() -> str:
    import secrets
    part = lambda n: "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(n))
    return f"BLAM8-{part(4)}-{part(4)}"


@router.get("/src")
def src_attribution(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """Which short (or campaign) produced which clicks and which signups — the
    read side of src/app/attribution.py. The shorts checklist
    (docs/shorts-checklist.md) is what puts the ?src= on the links."""
    from app import attribution
    return templates.TemplateResponse(request, "admin/src.html",
                                      {"ctx": ctx, "rows": attribution.summary(),
                                       "days": 30,
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.get("/invites")
def google_invites(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """Mint / track / revoke single-use Gmail-connect invite codes (the gate in
    src/app/slots.py — sign-in is open, it's connecting a Gmail mailbox that's
    invite-only), plus the "request access" vetting queue — invite or dismiss
    each requester. Each active invite carries a prefilled mailto so sending it
    is one click from the operator's own mail client."""
    from app import slots
    return templates.TemplateResponse(request, "admin/invites.html",
                                      {"ctx": ctx, "invites": db.list_google_invites(),
                                       "access_requests": db.list_google_access_requests(),
                                       "gate_active": slots.invite_gate_active(),
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/invites/new", dependencies=[Depends(require_csrf)])
def google_invite_new(email: str = Form(""), note: str = Form(""),
                      ctx: AdminCtx = Depends(require_admin)):
    db.create_google_invite(_mint_invite_code(), email, note)
    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/invites/{invite_id}/revoke", dependencies=[Depends(require_csrf)])
def google_invite_revoke(invite_id: str, ctx: AdminCtx = Depends(require_admin)):
    db.revoke_google_invite(invite_id)
    return RedirectResponse("/admin/invites", status_code=303)


# ── Partner referral programme ───────────────────────────────────────────────
@router.get("/llm")
def llm_calls_page(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """Every Anthropic call, grouped into exchanges by request_id — the diagnostic
    surface for "what did the assistant actually do, and did it cost what I expect".

    Cost here is RECOMPUTED from tokens and is an estimate (see src/app/llm_admin.py);
    /admin's margin numbers remain the authority. Nothing on this page renders prompt
    or response content — rows can hold customer email bodies, so reading one is a
    deliberate click through to the detail view.
    """
    q = request.query_params
    tenant = (q.get("tenant") or "").strip() or None
    operation = (q.get("operation") or "").strip() or None
    try:
        limit = max(10, min(int(q.get("limit") or 100), 500))
    except ValueError:
        limit = 100
    rows = db.list_llm_calls(tenant, operation=operation, limit=limit,
                             before=(q.get("before") or "").strip() or None)
    return templates.TemplateResponse(request, "admin/llm.html", {
        "ctx": ctx, "rows": llm_admin.annotate(rows),
        "exchanges": llm_admin.group_exchanges(rows),
        "totals": llm_admin.totals(rows),
        "operations": llm_admin.operations(rows),
        "tenant": tenant or "", "operation": operation or "", "limit": limit,
        "rate": settings.usd_gbp_rate,
        "log_mode": settings.llm_log_mode,
        "unseen_requests": _unseen(), "pending_os_requests": _pending_os()})


@router.get("/llm/{call_id}")
def llm_call_detail(request: Request, call_id: str, ctx: AdminCtx = Depends(require_admin)):
    """One call. The envelope (model, tokens, duration, stop reason) always shows; the
    request/response bodies only with ?body=1, because they can contain a customer's
    email content and an operator should have to choose to read it."""
    row = db.get_llm_call(call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")
    show_body = request.query_params.get("body") == "1"
    # Sibling calls in the same exchange, found by scanning a bounded recent page for
    # the shared request_id — enough to reconstruct a tool loop without a wider query.
    siblings = []
    if row.get("request_id"):
        siblings = [c for c in llm_admin.annotate(
            db.list_llm_calls(row.get("tenant_id"), limit=200))
            if c.get("request_id") == row["request_id"]]
    return templates.TemplateResponse(request, "admin/llm_call.html", {
        "ctx": ctx, "c": llm_admin.annotate([row])[0], "siblings": siblings,
        "show_body": show_body, "has_body": llm_admin.has_body(row),
        "rate": settings.usd_gbp_rate,
        "unseen_requests": _unseen(), "pending_os_requests": _pending_os()})


@router.get("/partners")
def partners_page(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """Every partner + their ledger (earned this month / scheduled / lifetime)
    — the monthly payout worksheet. "Record payout" moves everything scheduled
    into a paid statement. Leads across partners are listed for dispute review;
    the partner sees the same numbers on /partner."""
    from app import partners as prt
    rows = []
    for p in db.list_partners():
        s = prt.referral_summary(p)
        rows.append({**p, "summary": s})
    partner_names = {p["id"]: p["name"] for p in rows}
    leads = [{**l, "partner_name": partner_names.get(l["partner_id"], "?")}
             for l in db.list_all_partner_leads()]
    withdrawals = [{**w, "partner_name": partner_names.get(w["partner_id"], "?")}
                   for w in db.list_open_withdrawals()]
    return templates.TemplateResponse(request, "admin/partners.html",
                                      {"ctx": ctx, "partners": rows, "leads": leads,
                                       "withdrawals": withdrawals,
                                       "err": request.query_params.get("err"),
                                       "msg": request.query_params.get("msg"),
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/withdrawals/{withdrawal_id}/pay", dependencies=[Depends(require_csrf)])
def withdrawal_pay(withdrawal_id: str, reference: str = Form(""),
                   ctx: AdminCtx = Depends(require_admin)):
    """Settle a partner's withdrawal request after actually sending the money.

    The commission rows are chosen HERE, not when the request was made, so a clawback
    that landed in between is picked up rather than paid over. We pay in whole
    commission amounts (never splitting a ledger row), so the settled figure can come
    out under what was asked for — that's recorded on the request.
    """
    from app import partner_settlement as pset
    from app import partners as prt
    w = db.get_partner_withdrawal(withdrawal_id)
    if not w or w.get("status") != "requested":
        return RedirectResponse("/admin/partners?err=wmissing", status_code=303)
    payable = prt.payable_commissions(db.list_partner_commissions(w["partner_id"]))
    ids, total = pset.select_for_amount(payable, w.get("requested_gbp"))
    if not ids or total <= 0:
        return RedirectResponse("/admin/partners?err=wnothing", status_code=303)
    dates = sorted((c.get("earned_at") or "")[:10]
                   for c in payable if c["id"] in set(ids))
    payout = db.create_partner_payout(w["partner_id"], total,
                                      (dates[0] if dates else None),
                                      (dates[-1] if dates else None),
                                      reference.strip() or None, method="bank")
    db.mark_commission_ids_paid(ids, payout["id"])
    # Status-guarded, so a double click can't settle the same request twice.
    if db.settle_partner_withdrawal(withdrawal_id, payout["id"], total) is None:
        return RedirectResponse("/admin/partners?err=wraced", status_code=303)
    return RedirectResponse("/admin/partners?msg=wpaid", status_code=303)


@router.post("/withdrawals/{withdrawal_id}/cancel", dependencies=[Depends(require_csrf)])
def withdrawal_cancel(withdrawal_id: str, note: str = Form(""),
                      ctx: AdminCtx = Depends(require_admin)):
    """Refuse a request without paying it — the commissions stay scheduled and the
    partner can ask again."""
    if db.cancel_partner_withdrawal(withdrawal_id, note.strip() or "cancelled by operator") is None:
        return RedirectResponse("/admin/partners?err=wmissing", status_code=303)
    return RedirectResponse("/admin/partners?msg=wcancelled", status_code=303)


@router.post("/partners/new", dependencies=[Depends(require_csrf)])
def partners_new(name: str = Form(...), email: str = Form(...), ref_code: str = Form(""),
                 rate_pct: str = Form(""), ctx: AdminCtx = Depends(require_admin)):
    from app import partners as prt
    code = prt.normalize_code(ref_code) or prt.generate_code(name)
    try:
        rate = float(rate_pct.strip()) / 100 if rate_pct.strip() else 0.20
        db.create_partner(name, email, code, commission_rate=rate)
    except Exception:
        # Almost always a unique violation (email or code already taken).
        return RedirectResponse("/admin/partners?err=exists", status_code=303)
    return RedirectResponse("/admin/partners", status_code=303)


@router.post("/partners/{partner_id}/status", dependencies=[Depends(require_csrf)])
def partners_status(partner_id: str, status: str = Form(...),
                    ctx: AdminCtx = Depends(require_admin)):
    db.set_partner_status(partner_id, "active" if status == "active" else "disabled")
    return RedirectResponse("/admin/partners", status_code=303)


@router.post("/partners/{partner_id}/payout", dependencies=[Depends(require_csrf)])
def partners_payout(partner_id: str, reference: str = Form(""),
                    ctx: AdminCtx = Depends(require_admin)):
    """The month-end payout run for one partner: pays only CLEARED commissions
    (earned before this month began — the net-30 hold; clawbacks always
    included so refunds deduct from this statement, clause 4.7). Balances
    under the £10 threshold roll forward (clause 4.6). Run after actually
    sending the money — `reference` is the bank reference."""
    from app import partners as prt
    payable = prt.payable_commissions(db.list_partner_commissions(partner_id))
    if not payable:
        return RedirectResponse("/admin/partners?err=nothing", status_code=303)
    total = round(sum(float(c["amount"]) for c in payable), 2)
    if total < prt.MIN_PAYOUT_GBP:
        return RedirectResponse("/admin/partners?err=threshold", status_code=303)
    dates = sorted((c.get("earned_at") or "")[:10] for c in payable)
    payout = db.create_partner_payout(partner_id, total,
                                      dates[0] or None, dates[-1] or None,
                                      reference.strip() or None)
    db.mark_commission_ids_paid([c["id"] for c in payable], payout["id"])
    return RedirectResponse("/admin/partners?msg=paid", status_code=303)


@router.post("/invites/requests/{request_id}/invite", dependencies=[Depends(require_csrf)])
def access_request_invite(request_id: str, ctx: AdminCtx = Depends(require_admin)):
    """Vetted → mint an invite pre-addressed to the requester (its mailto in the
    invites table is then the one-click send) and mark the request handled."""
    req = db.get_google_access_request(request_id)
    if req:
        db.create_google_invite(_mint_invite_code(), req.get("email"),
                                "access request" + (f" — {req['message'][:80]}" if req.get("message") else ""))
        db.mark_access_request_invited(request_id)
    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/invites/requests/{request_id}/dismiss", dependencies=[Depends(require_csrf)])
def access_request_dismiss(request_id: str, ctx: AdminCtx = Depends(require_admin)):
    db.dismiss_access_request(request_id)
    return RedirectResponse("/admin/invites", status_code=303)


TOGGLABLE_MODULES = {"scheduling", "crm", "concierge", "tms", "fsm", "practice", "storefront",
                     "assets", "events", "wholesale"}


@router.post("/tenants/{tenant_id}/modules", dependencies=[Depends(require_csrf)])
def set_module(tenant_id: str, module: str = Form(...), active: str = Form(...),
               ctx: AdminCtx = Depends(require_admin)):
    """Flip a module's subscriptions row — the whole rollout: the tenant's hub card
    lights, the burger gains the surface, and the agent gains the module's tools.
    Activation seeds each module's defaults (idempotent) so the tenant lands on a
    working operation, not empty tables: TMS seeds load-unit types + a 'Main' branch + a
    default rate card; Field Service (fsm — docs/modules/module-field-service.md) seeds the
    starter job-type menu + a 'Main' area + a default rate card (team members are the
    tenant's own, added in settings)."""
    if module in TOGGLABLE_MODULES:
        db.set_module_active(tenant_id, module, active == "1")
        if module == "tms" and active == "1":
            db.seed_fleet_defaults(tenant_id)
            db.seed_tms_defaults(tenant_id)
        if module == "fsm" and active == "1":
            db.seed_fsm_defaults(tenant_id)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


# ── Voice CRM (founder tool — voice-crm-build-spec.md) ───────────────────────
# Kris's post-cold-call debrief pipeline: paste a Maps link, talk, review, save.
# Deliberately self-contained so it rips out clean (spec §6): this block, the
# admin/voice_crm*.html templates, the nav line, src/app/voice_crm.py, the vcrm_*
# db helpers + schema block, and scripts/db/migrations/apply_vcrm_migration.py.
def _vcrm_form_fields(form) -> dict:
    """One prospect payload from a review/edit form: blank → None, floats for
    the coordinates, unknown status → NEW (never an unconstrained write)."""
    fields = {k: ((form.get(k) or "").strip() or None)
              for k in db._VCRM_PROSPECT_FIELDS
              if k not in ("lat", "lng") + db._VCRM_OPTIONAL_FIELDS}
    # Partner-pipeline fields (kind/role/channel/source/email) are only written when the form
    # carries them, so the call-debrief review form (which has none) never blanks them.
    for k in db._VCRM_OPTIONAL_FIELDS:
        if k in form:
            fields[k] = (form.get(k) or "").strip() or None
    if "kind" in fields and fields["kind"] not in voice_crm.KINDS:
        fields["kind"] = "customer"
    if "channel" in fields and fields["channel"] not in voice_crm.CHANNELS:
        fields["channel"] = None
    if fields.get("status") not in voice_crm.STATUSES:
        fields["status"] = "IDENTIFIED" if fields.get("kind") == "partner" else "NEW"
    for k in ("lat", "lng"):
        try:
            fields[k] = float(form.get(k))
        except (TypeError, ValueError):
            fields[k] = None
    return fields


@router.get("/voice-crm")
def voice_crm_list(request: Request, status: str = "", kind: str = "",
                   ctx: AdminCtx = Depends(require_admin)):
    """The prospect list — filter pills per kind and status, sorted next-action-first."""
    status = status if status in voice_crm.STATUSES else ""
    kind = kind if kind in voice_crm.KINDS else ""
    return templates.TemplateResponse(request, "admin/voice_crm.html",
                                      {"ctx": ctx,
                                       "prospects": db.vcrm_list_prospects(status or None, kind or None),
                                       "statuses": voice_crm.STATUSES, "status": status,
                                       "kind": kind, "call_statuses": voice_crm.CALL_STATUSES,
                                       "partner_statuses": voice_crm.PARTNER_STATUSES,
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.get("/voice-crm/new")
def voice_crm_new(request: Request, prospect: str = "",
                  ctx: AdminCtx = Depends(require_admin)):
    """The debrief page: paste link → record → review → approve. ?prospect=<id>
    (the repeat-call door from a prospect page) prefills the record and skips
    straight to the mic."""
    from app import speech
    existing = db.vcrm_get_prospect(prospect) if prospect else None
    return templates.TemplateResponse(request, "admin/voice_crm_new.html",
                                      {"ctx": ctx, "existing": existing,
                                       "statuses": voice_crm.STATUSES,
                                       "outcomes": voice_crm.OUTCOMES,
                                       "whisper_ready": speech.ready(),
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/voice-crm/resolve", dependencies=[Depends(require_csrf)])
async def voice_crm_resolve(maps_url: str = Form(...),
                            ctx: AdminCtx = Depends(require_admin)):
    """Follow the pasted share link, parse name/coords/address, and say whether
    this prospect already exists (the call then attaches, never duplicates)."""
    try:
        parsed = await voice_crm.resolve_maps_link(maps_url)
    except voice_crm.VoiceCrmError as e:
        raise HTTPException(status_code=400, detail=str(e))
    existing = db.vcrm_find_prospect(parsed.get("maps_url"), parsed.get("business_name"))
    return {"ok": True, "parsed": parsed, "existing": existing}


@router.post("/voice-crm/debrief", dependencies=[Depends(require_csrf)])
async def voice_crm_debrief(audio: UploadFile = File(...), prospect_id: str = Form(""),
                            ctx: AdminCtx = Depends(require_admin)):
    """Audio blob → Whisper transcript → Claude extraction → JSON for the review
    form. Nothing is written here (Approve does that), and the audio bytes are
    dropped with the request — no retention, per the spec."""
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="No audio received")
    prospect = db.vcrm_get_prospect(prospect_id) if prospect_id.strip() else None
    try:
        transcript = await voice_crm.transcribe(
            data, audio.filename or "debrief.webm", audio.content_type or "audio/webm")
        result = await voice_crm.extract_debrief(transcript, prospect)
    except voice_crm.VoiceCrmError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "transcript": transcript,
            "fields": result["fields"], "raw": result["raw"]}


@router.post("/voice-crm/approve", dependencies=[Depends(require_csrf)])
async def voice_crm_approve(request: Request, ctx: AdminCtx = Depends(require_admin)):
    """The explicit commit: upsert the prospect from the REVIEWED form values,
    then attach the call row (outcome/summary/transcript/raw extraction)."""
    form = await request.form()
    fields = _vcrm_form_fields(form)
    if not fields.get("business_name"):
        raise HTTPException(status_code=400, detail="A business name is required")
    pid = (form.get("prospect_id") or "").strip()
    if pid and db.vcrm_get_prospect(pid):
        db.vcrm_update_prospect(pid, fields)
    else:
        pid = db.vcrm_create_prospect(fields)["id"]
    outcome = (form.get("outcome") or "").strip()
    try:
        extraction = json.loads(form.get("extraction_json") or "null")
    except json.JSONDecodeError:
        extraction = None
    db.vcrm_create_call(pid, outcome if outcome in voice_crm.OUTCOMES else None,
                        (form.get("summary") or "").strip() or None,
                        (form.get("transcript") or "").strip() or None, extraction)
    return RedirectResponse(f"/admin/voice-crm/{pid}", status_code=303)


@router.get("/voice-crm/add")
def voice_crm_add(request: Request, kind: str = "partner",
                  ctx: AdminCtx = Depends(require_admin)):
    """Add a prospect by hand (no call, no Maps link): the door for partner outreach."""
    return templates.TemplateResponse(request, "admin/voice_crm_add.html",
                                      {"ctx": ctx, "kind": kind if kind in voice_crm.KINDS else "partner",
                                       "call_statuses": voice_crm.CALL_STATUSES,
                                       "partner_statuses": voice_crm.PARTNER_STATUSES,
                                       "channels": voice_crm.CHANNELS,
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/voice-crm/add", dependencies=[Depends(require_csrf)])
async def voice_crm_add_post(request: Request, ctx: AdminCtx = Depends(require_admin)):
    form = await request.form()
    fields = _vcrm_form_fields(form)
    if not fields.get("business_name"):
        return RedirectResponse("/admin/voice-crm/add?err=name", status_code=303)
    fields.setdefault("kind", "customer")
    return RedirectResponse(f"/admin/voice-crm/{db.vcrm_create_prospect(fields)['id']}", status_code=303)


@router.post("/voice-crm/{prospect_id}/touch", dependencies=[Depends(require_csrf)])
def voice_crm_touch(prospect_id: str, summary: str = Form(...), status: str = Form(""),
                    ctx: AdminCtx = Depends(require_admin)):
    """Log a non-call touch (email sent, forum reply, met at an event) on the prospect's
    history; optionally move its status in the same tap."""
    if not db.vcrm_get_prospect(prospect_id):
        raise HTTPException(status_code=404, detail="Prospect not found")
    if summary.strip():
        db.vcrm_create_call(prospect_id, None, summary.strip(), None, None)
    if status in voice_crm.STATUSES:
        db.vcrm_update_prospect(prospect_id, {"status": status})
    return RedirectResponse(f"/admin/voice-crm/{prospect_id}", status_code=303)


@router.get("/voice-crm/{prospect_id}")
def voice_crm_prospect(request: Request, prospect_id: str,
                       ctx: AdminCtx = Depends(require_admin)):
    """One prospect: every field editable inline + the call history with
    collapsible transcripts."""
    prospect = db.vcrm_get_prospect(prospect_id)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return templates.TemplateResponse(request, "admin/voice_crm_prospect.html",
                                      {"ctx": ctx, "p": prospect,
                                       "calls": db.vcrm_list_calls(prospect_id),
                                       "statuses": voice_crm.STATUSES,
                                       "call_statuses": voice_crm.CALL_STATUSES,
                                       "partner_statuses": voice_crm.PARTNER_STATUSES,
                                       "channels": voice_crm.CHANNELS,
                                       "unseen_requests": _unseen(),
                                       "pending_os_requests": _pending_os()})


@router.post("/voice-crm/{prospect_id}/update", dependencies=[Depends(require_csrf)])
async def voice_crm_prospect_update(request: Request, prospect_id: str,
                                    ctx: AdminCtx = Depends(require_admin)):
    if not db.vcrm_get_prospect(prospect_id):
        raise HTTPException(status_code=404, detail="Prospect not found")
    form = await request.form()
    fields = _vcrm_form_fields(form)
    if not fields.get("business_name"):
        return RedirectResponse(f"/admin/voice-crm/{prospect_id}?err=name", status_code=303)
    db.vcrm_update_prospect(prospect_id, fields)
    return RedirectResponse(f"/admin/voice-crm/{prospect_id}", status_code=303)


def _vcrm_back(back: str) -> str:
    """Quick-action return path: only ever back into the Voice CRM pages —
    a form field must not become an open redirect."""
    return back if (back or "").startswith("/admin/voice-crm") else "/admin/voice-crm"


@router.post("/voice-crm/{prospect_id}/status", dependencies=[Depends(require_csrf)])
def voice_crm_quick_status(prospect_id: str, status: str = Form(...),
                           back: str = Form(""),
                           ctx: AdminCtx = Depends(require_admin)):
    """The long-press quick action (Kris, 2026-08-03): flip a prospect's status
    straight from the list — no edit form, no debrief. Unknown statuses are
    dropped, not stored."""
    if status in voice_crm.STATUSES and db.vcrm_get_prospect(prospect_id):
        db.vcrm_update_prospect(prospect_id, {"status": status})
    return RedirectResponse(_vcrm_back(back), status_code=303)


@router.post("/voice-crm/{prospect_id}/delete", dependencies=[Depends(require_csrf)])
def voice_crm_delete(prospect_id: str, back: str = Form(""),
                     ctx: AdminCtx = Depends(require_admin)):
    """Delete a prospect + its calls (FK cascade). Only reachable from the
    quick menu's two-tap confirm."""
    if not db.vcrm_get_prospect(prospect_id):
        raise HTTPException(status_code=404, detail="Prospect not found")
    db.vcrm_delete_prospect(prospect_id)
    return RedirectResponse(_vcrm_back(back), status_code=303)


@router.get("/memstat")
async def memstat_readout(collect: str = "", ctx: AdminCtx = Depends(require_admin)):
    """The process's memory read-out on demand (src/app/memstat.py): numbers only, no tenant data.
    ?collect=1 also runs the cyclic collector once and reports what it freed. The loop-thread
    numbers are read here, on the loop; the rest is read in a worker thread."""
    from app import memstat
    return await asyncio.to_thread(memstat.snapshot, collect=collect == "1", **memstat.loop_state())
