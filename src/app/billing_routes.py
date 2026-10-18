"""Self-serve billing — pricing page, Checkout entry, Customer Portal entry,
post-checkout landing pages.

Everything user-facing lives behind require_tenant + CSRF on state-changing
POSTs. The Checkout session has a `success_url` that brings the user back to
/billing/success?session_id=…; that handler retrieves the Session
synchronously and calls billing.sync_subscription() so the dashboard works
even if the webhook hasn't fired yet (Scout review #4 — closes the
Checkout→success-page race).
"""
from __future__ import annotations

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import billing, db, plans, trial
from app.auth import TenantCtx, require_tenant
from app.dashboard import _ctx as app_shell   # app header + Help-mode widget context
from app.config import settings
from app.security import require_csrf
from app.templating import templates
from app import guards as _guards


router = APIRouter(prefix="/billing", tags=["billing"])


# ── OS founding-partner gate ─────────────────────────────────────────────────
# "Core + your OS" is request-only while the founding cohort is hand-picked:
# the price stays public (the anchor), but the OS-tier checkout is locked until
# the operator approves the tenant (/admin/os-access, or directly from the
# tenant page). Enforced HERE, not just by hiding the button.

def _os_tier_price_ids() -> set[str]:
    """Both OS-tier price ids: the shared £248 OS price and the £298 Transport
    (professional) price. Same plan key, same gate."""
    return {settings.stripe_os_price_id, settings.stripe_professional_price_id} - {"", None}


def _os_access_granted(tenant: dict) -> bool:
    """Approved by the operator — or already ON an OS tier (a legacy/operator-set
    subscription must keep working, incl. the Customer Portal re-checkout path)."""
    return bool(tenant.get("os_access_approved")) or (
        tenant.get("plan") in {"professional", "scale", "enterprise"})


# ── Pricing page ────────────────────────────────────────────────────────────
@router.get("/plans")
def plans_page(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    """The pricing table. Always reachable for a signed-in tenant (no
    require_subscription here — this is where unpaid tenants land). Each
    plan card renders the relevant price-id button as long as the env var
    is set; missing price ids quietly hide that tier so the page stays
    consistent in environments where only some tiers are configured."""
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    os_granted = _os_access_granted(tenant)
    # The founding-partner look-inside: this tenant's industry OS, if built,
    # can be opened in preview (dashboard's POST /app/os/preview).
    industry_os = plans.INDUSTRY_MODULE.get((tenant.get("industry") or "").lower())
    os_previewing = bool(industry_os and industry_os
                         in db.preview_modules(ctx.tenant_id, client=ctx.client))
    # Trial truth for the banner — the SAME state machine the /app gate runs
    # (trial.evaluate), so this page can never disagree with the redirect that
    # brought a lapsed tenant here. "Ended" is only claimed when the gate is on
    # and actually blocking; with the gate off the pilot stays open-ended.
    trial_state = trial.evaluate(tenant)
    trial_expired = settings.require_subscription_gate and not trial_state["active"]
    window_end = trial.trial_window_end(tenant)
    return templates.TemplateResponse(request, "billing/plans.html", {
        **app_shell("", ctx),             # logged-in: app chrome + Help-mode widget
        "tenant": tenant,
        "plans": plans.billing_plans(tenant.get("industry")),   # industry-aware (src/app/plans.py)
        "stripe_configured": billing.is_configured(),
        "current_status": tenant.get("subscription_status"),
        # On a paid plan? Otherwise the tenant is on the free (no-card) pilot.
        # 'email' (Starter £39) is a paid tier too — it was missing here, so a
        # £39 subscriber saw the pilot banner instead of their real status.
        "on_paid_plan": tenant.get("plan") in {"email", "starter", "concierge",
                                               "professional", "scale", "enterprise"},
        # Founding-partner gate state for the "Core + your OS" card.
        "os_access_granted": os_granted,
        "os_request_pending": (None if os_granted
                               else db.tenant_pending_os_request(ctx.tenant_id)),
        "os_requested": request.query_params.get("os_requested"),
        # Sent here from /app/concierge by a plan without the Concierge
        # (dashboard redirects to ?concierge=1): say which tier she's on.
        "concierge_upsell": request.query_params.get("concierge") == "1",
        "os_previewable": bool(industry_os),
        "os_previewing": os_previewing,
        # Trial-over banner state (see comment above).
        "trial_state": trial_state,
        "trial_expired": trial_expired,
        "trial_ended_on": (f"{window_end.day} {window_end.strftime('%B')}"
                           if trial_expired and window_end else None),
    })


# ── OS access request ("Request founding access" on the plans page) ─────────
@router.post("/request-os-access", dependencies=[Depends(require_csrf)])
async def request_os_access(request: Request, message: str = Form(""),
                            ctx: TenantCtx = Depends(require_tenant)):
    """Queue a founding-partner request for vetting in /admin/os-access, and
    best-effort email the operator — same PLATFORM_NOTIFY_MAILBOX path as the
    Google-gate requests, so it lands in hello@'s automated inbox. Notification
    failure never loses the request; duplicates collapse onto the open one."""
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if _os_access_granted(tenant):
        return RedirectResponse("/billing/plans", status_code=303)
    if not db.tenant_pending_os_request(ctx.tenant_id):
        req = db.create_os_access_request(ctx.tenant_id, tenant.get("mailbox"),
                                          message[:2000])
        if settings.platform_notify_mailbox:
            try:
                from app import mailbox
                conn = db.get_connection_by_address(settings.platform_notify_mailbox)
                if conn:
                    body = (f"A tenant requested founding access to Core + your OS.\n\n"
                            f"Business: {tenant.get('name') or ctx.tenant_id}\n"
                            f"Industry: {tenant.get('industry') or '(not set)'}\n"
                            f"Mailbox: {tenant.get('mailbox') or '(not connected yet)'}\n"
                            f"What they run: {message.strip() or '(nothing added)'}\n\n"
                            f"Approve or dismiss here: {settings.app_base_url}/admin/os-access\n"
                            f"Request id: {req['id']}")
                    await mailbox.send_mail(conn["tenant_id"], settings.access_request_notify_to,
                                            "BLAM8 founding access request — "
                                            + (tenant.get("name") or "new tenant"), body,
                                            mailbox_id=conn["id"])
            except Exception:
                pass   # the request is safely queued either way
    return RedirectResponse("/billing/plans?os_requested=1", status_code=303)


# ── Checkout entry ──────────────────────────────────────────────────────────
@router.post("/checkout", dependencies=[Depends(require_csrf)])
def start_checkout(request: Request, price_id: str = Form(...),
                         ctx: TenantCtx = Depends(require_tenant)):
    """Mint a Stripe Checkout session and 303 the user to stripe.com. The
    price id is validated against the configured tiers — never a free-form
    string from the form, even though CSRF protects the route."""
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    if not billing.is_configured():
        raise HTTPException(status_code=503, detail="Stripe billing is not configured")
    valid_ids = {
        settings.stripe_email_price_id,
        settings.stripe_starter_price_id,
        settings.stripe_concierge_price_id,
        settings.stripe_professional_price_id,
        settings.stripe_os_price_id,
        settings.stripe_scale_price_id,
        settings.stripe_enterprise_price_id,
    } - {""}
    if price_id not in valid_ids:
        raise HTTPException(status_code=400, detail="Unknown plan")

    # Founding-partner gate: OS-tier price ids need operator approval first.
    # The plans page hides the buy button too, but THIS is the lock.
    if price_id in _os_tier_price_ids():
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
        if not _os_access_granted(tenant):
            return RedirectResponse("/billing/plans?os_gated=1", status_code=303)

    # Best-effort: surface the user's email for the prefilled customer record.
    user_email = None
    # (The session cookie carries the access token; auth.get_user could fetch
    # the email, but we already created the Stripe Customer at first call —
    # email comes from the OAuth signup row on Supabase. Keep it optional.)

    success_url = f"{settings.app_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{settings.app_base_url}/billing/cancel"
    url = billing.create_checkout_session(
        tenant_id=ctx.tenant_id,
        price_id=price_id,
        user_email=user_email,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return RedirectResponse(url, status_code=303)


@router.post("/topup", dependencies=[Depends(require_csrf)])
def start_topup(request: Request, packs: int = Form(1),
                      ctx: TenantCtx = Depends(require_tenant)):
    """Buy email credit. A ONE-OFF payment, so it never touches the tenant's
    subscription — a tenant with no subscription at all can still top up.

    `packs` is clamped in billing.create_topup_checkout_session rather than
    trusted, and the price id is read from settings, never from the form."""
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    # The page hides the button, but THIS is the lock. Selling credit while
    # enforcement is soft would take £39 for emails the tenant already gets
    # free — claim_draft never spends credit under soft, so it could never be
    # used up. Same shape as the OS-tier gate on start_checkout above.
    if (not billing.is_configured() or not plans.topup_price_id()
            or settings.usage_enforcement != "hard"):
        raise HTTPException(status_code=503, detail="Top-ups are not available")
    url = billing.create_topup_checkout_session(
        tenant_id=ctx.tenant_id,
        quantity=packs,
        user_email=None,
        success_url=f"{settings.app_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.app_base_url}/app/usage",
    )
    return RedirectResponse(url, status_code=303)


# ── Post-checkout ───────────────────────────────────────────────────────────
@router.get("/success")
def checkout_success(request: Request, session_id: str | None = None,
                           ctx: TenantCtx = Depends(require_tenant)):
    """The Checkout success_url — user is back from Stripe. To close the race
    between this redirect and the webhook delivery, retrieve the Checkout
    Session synchronously and feed its subscription into the same
    sync_subscription() path the webhook uses. Subsequent webhook deliveries
    converge harmlessly (sync_subscription is idempotent)."""
    if session_id and billing.is_configured():
        try:
            session = stripe.checkout.Session.retrieve(
                session_id, expand=["subscription"]
            )
            # A top-up is a one-off payment with no subscription on it, so it
            # would fall straight past the sync below. Credit it here for the
            # same reason the subscription is synced here: the user is looking
            # at the page now and the webhook may not have landed. Idempotent,
            # so whichever arrives second changes nothing.
            billing.credit_topup_from_session(
                session if isinstance(session, dict) else session.to_dict())
            sub = session.get("subscription") if isinstance(session, dict) else session.subscription
            # The expanded subscription is either a dict (when retrieved with
            # expand=) or a Subscription object — both behave dict-like.
            if sub:
                # Normalise to a plain dict for sync_subscription
                sub_dict = sub if isinstance(sub, dict) else sub.to_dict()
                billing.sync_subscription(sub_dict)
        except Exception:
            # Render generic processing page if Stripe lookup fails — the
            # webhook will catch up. Never 5xx the user post-purchase.
            pass
    # Scheduling plans aren't done at checkout: the AI can't book until the tenant
    # defines what a booking needs (/app/diary/intake — the pipeline gates the diary
    # tools on it). Nobody knows that coming out of Stripe, so the success page's
    # primary CTA becomes that setup step. Best-effort — a read failure just renders
    # the generic welcome.
    needs_sched_setup = False
    try:
        if db.module_active(ctx.tenant_id, "scheduling", client=ctx.client):
            cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
            needs_sched_setup = not cfg.get("intake_configured_at")
    except Exception:
        pass
    return templates.TemplateResponse(request, "billing/success.html",
                                      app_shell("", ctx, needs_sched_setup=needs_sched_setup))


@router.get("/cancel")
def checkout_cancel(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    """User backed out of Stripe Checkout. Render a soft 'no charge made' page
    that links back to /billing/plans."""
    return templates.TemplateResponse(request, "billing/cancel.html", app_shell("", ctx))


# ── Customer Portal entry ───────────────────────────────────────────────────
@router.post("/portal", dependencies=[Depends(require_csrf)])
def open_portal(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    """Open the Stripe-hosted Customer Portal (upgrade / downgrade / cancel /
    update card). If the tenant has no Stripe customer yet (e.g.
    grandfathered or enterprise pre-link), redirect to /billing/plans instead
    of erroring."""
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    if not billing.is_configured():
        raise HTTPException(status_code=503, detail="Stripe billing is not configured")
    try:
        url = billing.create_portal_session(
            tenant_id=ctx.tenant_id,
            return_url=f"{settings.app_base_url}/app",
        )
    except billing.PortalUnavailable:
        return RedirectResponse("/billing/plans", status_code=303)
    return RedirectResponse(url, status_code=303)


# ── HTMX fragment: live billing status ──────────────────────────────────────
@router.get("/status")
def billing_status_fragment(request: Request,
                                  ctx: TenantCtx = Depends(require_tenant)):
    """Tiny status fragment a page can poll (HTMX hx-get) after returning from
    Checkout — refreshes once the webhook lands the real status. Renders the
    raw status string in a span so the parent page can swap based on it."""
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    return templates.TemplateResponse(request, "billing/status.html", {
        "status": tenant.get("subscription_status") or "none",
        "trial_ends_at": tenant.get("trial_ends_at"),
        "current_period_end": tenant.get("current_period_end"),
    })
