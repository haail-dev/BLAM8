"""The partner portal — /partner.

A partner signs in with the SAME SSO as everyone else (many partners are also
tenants — the pitch literally tells agencies to run BLAM8 on their own inbox);
what makes them a partner is their sign-in email matching a partners row. So
this router resolves the session USER (not the tenant) and gates on that match:

  * no session            → AuthRedirect → /login (the normal SSO flow)
  * signed in, no match   → a friendly "become a partner" page (never a 404 —
                            it's the natural landing spot for curious partners)
  * matched               → the portal: code + share link + approved share
                            text, referrals on the status ladder (Registered →
                            Trial → Payment pending → Active → Cancelled, with
                            the 90-day window expiry on unconverted ones),
                            money tiles from the COMMISSION LEDGER (earned =
                            collected by BLAM8; scheduled until paid out),
                            manual lead registration, payout statements, and
                            promo assets.

Privacy: a partner sees business name, date, status, plan and commission —
never a referral's inbox activity, contacts, jobs or customer data (the db
reads select exactly those columns and nothing more).
"""
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth, db, partner_settlement, partners
from app.auth import AuthRedirect
from app.config import settings
from app.security import rate_limit, require_csrf
from app.templating import templates

router = APIRouter(prefix="/partner", tags=["partner"])


async def _session_user(request: Request, response: Response) -> dict:
    """The signed-in GoTrue user — require_tenant's cookie/refresh dance without
    the tenant resolution (a pure partner may never onboard a tenant)."""
    access = request.cookies.get(auth.AT_COOKIE)
    user = await auth.get_user(access) if access else None
    if not user:
        rt = request.cookies.get(auth.RT_COOKIE)
        session = await auth.refresh_session(rt) if rt else None
        if not session:
            raise AuthRedirect()
        user = await auth.get_user(session["access_token"])
        if not user:
            raise AuthRedirect()
        auth.set_session_cookies(response, session)
        request.state.refreshed_session = session
    return user


async def _partner_or_none(request: Request, response: Response) -> tuple[dict, dict | None]:
    user = await _session_user(request, response)
    return user, db.get_partner_by_email(user.get("email") or "")


@router.get("/join")
def join(request: Request):
    """The public "become a partner" entry point. Not signed in yet — stash the
    intent in a cookie so the SSO callback knows this is a PARTNER joining (no
    business tenant, no onboarding wizard) and routes them back here to enrol.
    Someone already signed in just lands on the enrolment form directly."""
    resp = RedirectResponse("/login?partner=1", status_code=303)
    partners.set_partner_intent_cookie(resp)
    return resp


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    """The programme terms the click-wrap accepts — the same mechanics the
    product enforces, stated plainly. Public (linked from the join form)."""
    return templates.TemplateResponse(request, "partner/terms.html", {
        "terms_version": partners.TERMS_VERSION,
        "rate_pct": 20, "window_days": partners.ATTRIBUTION_WINDOW_DAYS,
    })


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def partner_home(request: Request, response: Response):
    user, partner = await _partner_or_none(request, response)
    if not partner:
        from app.auth import default_company_name
        resp = templates.TemplateResponse(request, "partner/join.html", {
            "email": user.get("email"),
            "default_name": default_company_name(user.get("email")) or "",
            "err": request.query_params.get("err"),
            "terms_version": partners.TERMS_VERSION,
        })
        partners.clear_partner_intent_cookie(resp)   # intent served — they're here
        return resp
    base = (settings.app_base_url or "https://blam8.com").rstrip("/")
    share_url = f"{base}/login?ref={partner['ref_code']}"
    # A PURE partner has no tenant (the SSO callback deliberately skips tenant
    # creation for recognised partners) — the header then offers an opt-in
    # "Use BLAM8 for my business" instead of a dashboard link.
    try:
        has_tenant = bool(db.get_tenant_for_user(user["id"]))
    except Exception:
        has_tenant = False
    return templates.TemplateResponse(request, "partner/home.html", {
        "p": partner,
        "s": partners.referral_summary(partner),
        "share_url": share_url,
        "share_text": partners.share_text(partner, share_url),
        "lead_msg": request.query_params.get("lead"),
        "disabled": partner.get("status") != "active",
        "window_days": partners.ATTRIBUTION_WINDOW_DAYS,
        "has_tenant": has_tenant,
    })


@router.post("/enroll", dependencies=[Depends(rate_limit("partner_enroll", 5, 3600)),
                                      Depends(require_csrf)])
async def enroll(request: Request, response: Response,
                 partner_name: str = Form(""), signed_name: str = Form(""),
                 agree: str = Form("")):
    """Self-serve signing of the Partner Referral Agreement: the signed-in
    email becomes a partner on reading the agreement and signing with their
    typed full name. The gate is on payouts (collected-money ledger,
    self-referral block, disable switch), not on joining — so this is instant.
    Idempotent for an email that's already a partner."""
    user = await _session_user(request, response)
    if db.get_partner_by_email(user.get("email") or ""):
        return RedirectResponse("/partner", status_code=303)
    if not agree:
        return RedirectResponse("/partner?err=agree", status_code=303)
    if not (signed_name or "").strip():
        return RedirectResponse("/partner?err=sign", status_code=303)
    partner = partners.enroll_self_serve(user.get("email") or "", partner_name, signed_name)
    if not partner:
        return RedirectResponse("/partner?err=name", status_code=303)
    return RedirectResponse("/partner", status_code=303)


@router.post("/setup", dependencies=[Depends(require_csrf)])
async def setup_tenant(request: Request, response: Response):
    """The opt-in for a pure partner who wants BLAM8 for their own business:
    creates their tenant (idempotent) and hands them to the app — which sends
    a fresh tenant into the normal onboarding wizard."""
    user = await _session_user(request, response)
    db.ensure_tenant_for_user(user["id"], auth.default_company_name(user.get("email")),
                              email=user.get("email"))
    return RedirectResponse("/app", status_code=303)


@router.post("/leads", dependencies=[Depends(rate_limit("partner_leads", 30, 3600)),
                                     Depends(require_csrf)])
async def register_lead(request: Request, response: Response,
                        company_name: str = Form(""), contact_email: str = Form("")):
    """Manual introduction: registers a lead BEFORE the business signs up, so
    the partner's attribution survives a signup that never touches their link.
    90-day window from registration; self-leads are refused in product."""
    user, partner = await _partner_or_none(request, response)
    if not partner or partner.get("status") != "active":
        return RedirectResponse("/partner", status_code=303)
    result = partners.register_lead(partner, company_name, contact_email)
    return RedirectResponse(f"/partner?lead={result}#leads", status_code=303)


@router.post("/settlement", dependencies=[Depends(require_csrf)])
async def set_settlement(request: Request, response: Response, mode: str = Form(...)):
    """Choose bank payouts or subscription credit.

    Credit needs a tenant to credit, so it resolves the signed-in user's tenant and
    stores it on the partner row — a pure referrer with no BLAM8 account of their own
    is told why rather than being switched into a mode that can't pay them.
    """
    user, partner = await _partner_or_none(request, response)
    if not partner or partner.get("status") != "active":
        return RedirectResponse("/partner", status_code=303)
    if mode == "credit":
        tenant_id = db.get_tenant_for_user(user["id"])
        if not tenant_id:
            return RedirectResponse("/partner?err=notenant#settlement", status_code=303)
        db.set_partner_settlement(partner["id"], "credit", tenant_id)
    else:
        db.set_partner_settlement(partner["id"], "cash")
    return RedirectResponse("/partner?msg=settlement#settlement", status_code=303)


@router.post("/withdraw", dependencies=[Depends(rate_limit("partner_withdraw", 10, 3600)),
                                        Depends(require_csrf)])
async def request_withdrawal(request: Request, response: Response,
                             amount: str = Form("")):
    """Ask for surplus commission as cash.

    The amount is CLAMPED against what's actually available rather than trusted: the UI
    is a slider from £0 to available, so anything outside that range is a hand-built
    request. Only the amount is recorded — the commission rows that settle it are chosen
    when it's paid, so a clawback arriving in between can't be missed.
    """
    user, partner = await _partner_or_none(request, response)
    if not partner or partner.get("status") != "active":
        return RedirectResponse("/partner", status_code=303)
    if db.open_partner_withdrawal(partner["id"]):
        return RedirectResponse("/partner?err=wopen#settlement", status_code=303)
    summary = partners.referral_summary(partner)
    settled = partner_settlement.clamp_request(amount, summary["settlement"]["available"])
    if settled <= 0:
        return RedirectResponse("/partner?err=wamount#settlement", status_code=303)
    if db.create_partner_withdrawal(partner["id"], settled) is None:
        return RedirectResponse("/partner?err=wopen#settlement", status_code=303)
    return RedirectResponse("/partner?msg=wrequested#settlement", status_code=303)


@router.post("/withdraw/{withdrawal_id}/cancel", dependencies=[Depends(require_csrf)])
async def cancel_withdrawal(request: Request, response: Response, withdrawal_id: str):
    """Withdraw the ask before it's paid. Ownership is checked explicitly — the id is
    in the URL, so it must never be enough on its own."""
    user, partner = await _partner_or_none(request, response)
    if not partner:
        return RedirectResponse("/partner", status_code=303)
    w = db.get_partner_withdrawal(withdrawal_id)
    if not w or w.get("partner_id") != partner["id"]:
        return RedirectResponse("/partner?err=wmissing#settlement", status_code=303)
    db.cancel_partner_withdrawal(withdrawal_id, note="cancelled by partner")
    return RedirectResponse("/partner?msg=wcancelled#settlement", status_code=303)
