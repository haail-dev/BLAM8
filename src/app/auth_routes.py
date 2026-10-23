"""Sign-in via SSO (Google / Microsoft) over Supabase Auth.

Passwordless by design: GoTrue's OAuth (PKCE) hands back a session we keep in
httpOnly cookies. There is no email/password or registration — the first SSO
sign-in creates the user, and ensure_tenant_for_user creates their company.
"""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from datetime import datetime, timezone

from app import auth, db, native_auth, navcache, partners, people_routes, slots
from app.config import settings
from app.security import rate_limit, require_csrf
from app.templating import templates

import logging

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


class _TenantUnavailable(Exception):
    """An active membership resolved to a tenant row that cannot be read."""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # A browser that already holds a BLAM8 session must not silently become
    # that account again (stays run #10, 2026-09-13: Kris landed on an old
    # test tenant without typing a thing) — the page names the session and
    # offers Continue or Sign out. Best-effort: an unreadable session shows
    # the plain page.
    signed_in_as = None
    access = request.cookies.get(auth.AT_COOKIE)
    refreshed_session = None
    user = None
    if access:
        try:
            user = await auth.get_user(access)
        except Exception:
            user = None
    if not user:
        refresh_token = request.cookies.get(auth.RT_COOKIE)
        if refresh_token:
            try:
                refreshed_session = await auth.refresh_session(refresh_token)
                user = (refreshed_session or {}).get("user")
            except Exception:
                log.warning("login page session refresh failed", exc_info=True)
    signed_in_as = (user or {}).get("email") or None
    resp = templates.TemplateResponse(
        request, "login.html",
        {"error": request.query_params.get("error"),
         "partner_join": request.query_params.get("partner") == "1",
         "signed_in_as": signed_in_as,
         "continue_blocked": request.query_params.get("continue_blocked") == "1"},
    )
    # Partner referral links land here (blam8.com/login?ref=CODE). SSO leaves
    # the site, so the code rides a cookie to /auth/callback for attribution.
    if request.query_params.get("ref"):
        partners.set_ref_cookie(resp, request.query_params["ref"])
    if refreshed_session and signed_in_as:
        auth.set_session_cookies(resp, refreshed_session)
        auth.seed_identity(refreshed_session)
    return resp


@router.get("/api/auth/google-slots", dependencies=[Depends(rate_limit("google_slots", 120, 60))])
def google_slots_api():
    """Honest Gmail-connect availability — {used, cap, remaining, verified}.
    Sign-in itself is uncapped (basic scopes); this is the unverified-app cap on
    Gmail MAILBOX connects. Cached in-process (~60s) so it never hits the DB per
    visit. Public (no auth): it's the same number the connect surfaces show."""
    return slots.google_slots()


@router.get("/auth/sso/{provider}", dependencies=[Depends(rate_limit("sso", 20, 300))])
def sso_start(provider: str, request: Request):
    """Begin SSO: redirect to GoTrue's provider consent, stashing the PKCE
    verifier in a short-lived cookie for the callback to replay.

    Both providers are open to everyone — SSO only asks for basic email+profile
    scopes, which Google doesn't cap. The scarce resource (Gmail's restricted
    scopes, 100 grants pre-verification) is guarded where it's actually spent:
    the mailbox connect step (src/app/oauth.py)."""
    if provider not in auth.SSO_PROVIDERS:
        return RedirectResponse("/login?error=Unknown%20sign-in%20provider", status_code=303)
    url, verifier = auth.sso_authorize_url(provider, f"{settings.app_base_url}/auth/callback")
    resp = RedirectResponse(url, status_code=303)
    auth.set_pkce_cookie(resp, verifier)
    return resp


def _partner_landing(request: Request, user: dict, is_new: bool) -> bool:
    """Whether this signed-in identity belongs in the partner portal.

    Keep this decision shared by SSO callbacks and an already-authenticated
    Continue action so neither path provisions a business for a partner.
    """
    partner = None
    if is_new:
        try:
            partner = db.get_partner_by_email(user.get("email") or "")
        except Exception:
            partner = None
    return bool(partner or request.cookies.get(partners.PARTNER_INTENT_COOKIE))


def _establish_tenant(request: Request, user: dict,
                     existing_tenant_id: str | None,
                     validate_tenant: bool = False) -> tuple[str | None, str | None]:
    """Resolve an authenticated person's business, creating one only when
    they have no membership at all. Shared by SSO and Continue.
    """
    email = user.get("email")
    # A deleted/reseeded tenant may still be cached for five minutes. Sign-in
    # re-establishes this mapping before the landing request resolves it.
    navcache.invalidate("ut:" + user["id"])

    # A usable invitation binds this identity to the inviter's business and
    # role instead of creating a separate business for them.
    join_token = request.cookies.get(people_routes.JOIN_COOKIE)
    joined = None
    if join_token:
        try:
            from app import people as _people
            invite_row = db.get_member_invite(join_token)
            if invite_row and _people.usable(invite_row):
                db.accept_member_invite(invite_row, user["id"], email,
                                        datetime.now(timezone.utc).isoformat())
                joined = invite_row["tenant_id"]
        except Exception:
            log.warning("invite accept failed for user %s", user["id"], exc_info=True)

    tenant_id = joined or existing_tenant_id
    if not tenant_id:
        tenant_id = db.ensure_tenant_for_user(
            user["id"], auth.default_company_name(email), email=email)
    if tenant_id:
        if validate_tenant:
            try:
                tenant = db.get_tenant(tenant_id)
            except Exception as exc:
                log.warning("tenant validation failed for signed-in user %s", user["id"], exc_info=True)
                raise _TenantUnavailable() from exc
            if not tenant:
                navcache.invalidate("ut:" + user["id"])
                raise _TenantUnavailable()
        navcache.put("ut:" + user["id"], 300.0, tenant_id)
        # Keep the People page's last-seen and display email current. Best-effort
        # so a profile stamp cannot prevent sign-in.
        try:
            db.touch_member_seen(tenant_id, user["id"], datetime.now(timezone.utc).isoformat())
            if email:
                db.update_member(tenant_id, user["id"], {"email": email.strip().lower()})
        except Exception:
            log.debug("member stamp failed", exc_info=True)
    return tenant_id, join_token


def _record_signup_attribution(request: Request, response, user: dict,
                               tenant_id: str | None, is_new: bool) -> None:
    """Apply and clear referral/source cookies after a completed landing."""
    email = user.get("email")
    # Referral work is best-effort; attribution must never break sign-in.
    try:
        if is_new:
            ref = request.cookies.get(partners.REF_COOKIE)
            if ref and tenant_id:
                partners.attach_for_tenant(tenant_id, ref, signer_email=email)
            partners.reconcile_leads_for_signup(tenant_id, email)
        else:
            partners.mark_leads_already_customer(email)
    except Exception:
        pass
    # Campaign source is a first-touch value and is only attached to a new tenant.
    from app import attribution
    try:
        src = request.cookies.get(attribution.SRC_COOKIE)
        if is_new and src and tenant_id:
            attribution.attach_for_tenant(tenant_id, attribution.normalize_src(src))
    except Exception:
        pass
    if request.cookies.get(partners.REF_COOKIE):
        partners.clear_ref_cookie(response)
    if request.cookies.get(attribution.SRC_COOKIE):
        attribution.clear_src_cookie(response)


@router.get("/auth/callback", dependencies=[Depends(rate_limit("sso_callback", 30, 300))])
async def sso_callback(request: Request):
    """Provider -> GoTrue -> here with ?code=. Exchange it (+ the PKCE verifier
    cookie) for a session, ensure the user's tenant, then land them in the app
    (require_onboarded sends brand-new users on to the onboarding wizard).

    A sign-in started by an iOS app (/auth/native/start) is instead handed
    back to that app; see native_auth."""
    raw_native = request.cookies.get(native_auth.NATIVE_COOKIE)
    pending = native_auth.parse_pending(raw_native)
    if pending:
        return await _native_callback(request, pending)
    resp = await _web_callback(request)
    if raw_native:
        native_auth.clear_cookie(resp)   # unusable native marker: ignored, then dropped
    return resp


async def _web_callback(request: Request):
    if request.query_params.get("error"):
        return RedirectResponse("/login?error=Sign-in%20was%20cancelled%20or%20failed", status_code=303)
    code = request.query_params.get("code")
    verifier = request.cookies.get(auth.PKCE_COOKIE)
    if not code or not verifier:
        return RedirectResponse(
            "/login?error=That%20sign-in%20link%20expired.%20Please%20try%20again.", status_code=303)
    try:
        session = await auth.exchange_code_for_session(code, verifier)
    except ValueError:
        return RedirectResponse(
            "/login?error=Could%20not%20complete%20sign-in.%20Please%20try%20again.", status_code=303)
    return _complete_sign_in(request, session)


def _complete_sign_in(request: Request, session: dict, landing: str = "/app"):
    """Start the BLAM8 session for an identity GoTrue has just confirmed:
    partner landing or tenant (with any invite), attribution, then cookies.
    Shared by the web SSO callback and the iOS apps' completion step, which
    runs in the app's own web view so its invite/referral cookies apply."""
    user = session.get("user") or {}
    existing_tenant_id = db.get_tenant_for_user(user["id"])
    is_new = not existing_tenant_id
    # Partner landings: a recognised partner, or someone who clicked "become a
    # partner" (the intent cookie from /partner/join). Neither gets a business
    # tenant created on a first sign-in — the portal offers "Use BLAM8 for my
    # business" if they ever want the app; /partner shows the enrolment form
    # to the intent case. An EXISTING account with partner intent keeps its
    # tenant and simply lands on /partner to enrol.
    partner_intent = bool(request.cookies.get(partners.PARTNER_INTENT_COOKIE))
    if _partner_landing(request, user, is_new):
        resp = RedirectResponse("/partner", status_code=303)
        auth.set_session_cookies(resp, session)
        auth.clear_pkce_cookie(resp)
        if partner_intent:
            partners.clear_partner_intent_cookie(resp)
        if request.cookies.get(partners.REF_COOKIE):
            partners.clear_ref_cookie(resp)   # partners aren't referable
        return resp
    try:
        tenant_id, join_token = _establish_tenant(request, user, existing_tenant_id)
    except db.MembershipSwitchedOff:
        # Their access was switched off by the business (security review M1).
        resp = RedirectResponse("/login?error=Your%20access%20to%20this%20business%20has%20been%20switched%20off.%20Ask%20whoever%20runs%20it.", status_code=303)
        auth.clear_session_cookies(resp)
        return resp
    resp = RedirectResponse(landing, status_code=303)
    auth.set_session_cookies(resp, session)
    auth.seed_identity(session)     # the landing request need not ask GoTrue /user again
    auth.clear_pkce_cookie(resp)
    if join_token:
        people_routes.clear_join_cookie(resp)
    _record_signup_attribution(request, resp, user, tenant_id, is_new)
    return resp


# ── iOS app sign-in (docs/architecture/native-sign-in.md) ────────────────────
@router.get("/auth/native/start", dependencies=[Depends(rate_limit("native_sso", 20, 300))])
def native_sso_start(provider: str = "", scheme: str = "",
                     next_path: str = Query("", alias="next"), challenge: str = ""):
    """Begin SSO for an iOS app, inside the system sign-in sheet. Same GoTrue
    redirect as the web (no Supabase allow-list change), plus a marker cookie
    telling the callback to hand the result back to the app."""
    if scheme not in native_auth.NATIVE_SCHEMES:
        return PlainTextResponse("Unknown app.", status_code=400)
    if (provider not in auth.SSO_PROVIDERS or next_path not in native_auth.NATIVE_NEXT_PATHS
            or not native_auth.valid_challenge(challenge)):
        return RedirectResponse(native_auth.callback_url(
            scheme, error="That sign-in request was not valid. Please try again."), status_code=303)
    url, verifier = auth.sso_authorize_url(provider, f"{settings.app_base_url}/auth/callback")
    resp = RedirectResponse(url, status_code=303)
    auth.set_pkce_cookie(resp, verifier)
    native_auth.set_cookie(resp, native_auth.Pending(scheme, challenge, next_path))
    return resp


async def _native_callback(request: Request, pending: native_auth.Pending):
    """The sheet's end of a native sign-in: exchange the code exactly as the web
    does, but park the session under a one-time code for the app rather than
    starting it here. No tenant work: that needs the app's own cookies."""
    code = request.query_params.get("code")
    verifier = request.cookies.get(auth.PKCE_COOKIE)
    error = None
    if request.query_params.get("error"):
        error = "Sign-in was cancelled or failed."
    elif not code or not verifier:
        error = "That sign-in link expired. Please try again."
    else:
        try:
            session = await auth.exchange_code_for_session(code, verifier)
        except ValueError:
            error = "Could not complete sign-in. Please try again."
    if error:
        target = native_auth.callback_url(pending.scheme, error=error)
    else:
        target = native_auth.callback_url(
            pending.scheme, code=native_auth.issue(session, pending.challenge, pending.next))
    resp = RedirectResponse(target, status_code=303, headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    auth.clear_pkce_cookie(resp)
    native_auth.clear_cookie(resp)
    return resp


@router.post("/auth/native/complete", dependencies=[Depends(rate_limit("native_complete", 20, 300))])
async def native_sso_complete(request: Request):
    """The app's web view redeems its one-time code with the verifier only it
    holds, then signs in exactly as the web callback would. POST keeps both
    values out of URLs and access logs. No CSRF token: the shell posts this
    directly with a required non-simple header. Cross-origin browser forms are
    rejected separately from the single-use verifier binding."""
    # A verifier protects interception, but an attacker knows the verifier for
    # their own login. Require a native-only request header so a foreign form
    # cannot plant that login in another browser. This route exposes no CORS.
    if request.headers.get('X-Blam8-Native-Auth') != '1':
        return PlainTextResponse('Update the app and sign in again.', status_code=403, headers={'Cache-Control':'no-store'})
    origin = request.headers.get('origin')
    if (origin is not None and origin != settings.app_base_url.rstrip('/')) or request.headers.get('sec-fetch-site') in {'cross-site','same-site'}:
        return PlainTextResponse('Sign-in must finish in the app that started it.', status_code=403, headers={'Cache-Control':'no-store'})
    form = await request.form()
    redeemed = native_auth.redeem(str(form.get("code") or ""), str(form.get("verifier") or ""))
    if not redeemed:
        resp = RedirectResponse(
            "/login?error=That%20sign-in%20expired.%20Please%20try%20again.", status_code=303)
    else:
        session, landing = redeemed
        resp = _complete_sign_in(request, session, landing=landing)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/auth/continue", dependencies=[
    Depends(require_csrf), Depends(rate_limit("auth_continue", 20, 300))
])
async def continue_session(request: Request):
    """Continue the identity already named on /login without another OAuth
    round-trip. If its old business was deleted, provision the same fresh
    tenant the SSO callback would create; never revive switched-off access.
    """
    access = request.cookies.get(auth.AT_COOKIE)
    user = None
    try:
        user = await auth.get_user(access) if access else None
    except Exception:
        log.warning("continue session identity lookup failed", exc_info=True)

    session = None
    if not user or not user.get("id"):
        refresh_token = request.cookies.get(auth.RT_COOKIE)
        if refresh_token:
            try:
                session = await auth.refresh_session(refresh_token)
                user = (session or {}).get("user")
            except Exception:
                log.warning("continue session refresh failed", exc_info=True)
        if not user or not user.get("id"):
            resp = RedirectResponse(
                "/login?error=Your%20session%20has%20expired.%20Please%20sign%20in%20again.",
                status_code=303)
            auth.clear_session_cookies(resp)
            return resp
        auth.seed_identity(session)

    existing_tenant_id = db.get_tenant_for_user(user["id"])
    is_new = not existing_tenant_id
    partner_intent = bool(request.cookies.get(partners.PARTNER_INTENT_COOKIE))
    if _partner_landing(request, user, is_new):
        resp = RedirectResponse("/partner", status_code=303)
        if session:
            auth.set_session_cookies(resp, session)
        if partner_intent:
            partners.clear_partner_intent_cookie(resp)
        if request.cookies.get(partners.REF_COOKIE):
            partners.clear_ref_cookie(resp)
        return resp

    try:
        tenant_id, join_token = _establish_tenant(
            request, user, existing_tenant_id, validate_tenant=True)
    except db.MembershipSwitchedOff:
        resp = RedirectResponse(
            "/login?error=Your%20access%20to%20this%20business%20has%20been%20switched%20off.%20Ask%20whoever%20runs%20it.",
            status_code=303)
        auth.clear_session_cookies(resp)
        return resp
    except _TenantUnavailable:
        resp = RedirectResponse(
            "/login?error=We%20couldn't%20find%20the%20business%20linked%20to%20this%20account.%20Please%20contact%20support.&continue_blocked=1",
            status_code=303)
        if session:
            auth.set_session_cookies(resp, session)
        return resp

    resp = RedirectResponse("/app", status_code=303)
    if session:
        auth.set_session_cookies(resp, session)
    auth.seed_identity(session or {"access_token": access, "user": user})
    if join_token:
        people_routes.clear_join_cookie(resp)
    from app import attribution
    if (is_new or request.cookies.get(partners.REF_COOKIE)
            or request.cookies.get(attribution.SRC_COOKIE)):
        _record_signup_attribution(request, resp, user, tenant_id, is_new)
    return resp


@router.get("/logout")
def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.AT_COOKIE))   # M6: ends it at Supabase too
    resp = RedirectResponse("/", status_code=303)
    auth.clear_session_cookies(resp)
    return resp
