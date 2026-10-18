"""Microsoft Graph mailbox-connect flow (spec Days 4-6, §4 & §6).

  GET /oauth/connect   -> redirect to the Microsoft consent screen
  GET /oauth/callback  -> exchange the code for tokens, save to email_connections
  GET /oauth/status    -> is a mailbox connected for this tenant?
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import billing, db, gmail, graph, mailbox, member_calendar, slots
from app.auth import require_tenant, TenantCtx
from app.config import settings
from app.templating import templates
from app.security import rate_limit, require_csrf
from app import security as _security
log = logging.getLogger("blautom8.oauth")
from app import guards as _guards

router = APIRouter(prefix="/oauth", tags=["oauth"])

# The invite/request forms live on these pages; redeem/request redirect back to
# whichever one the tenant was on. Whitelisted so a crafted `next` can't bounce
# anywhere else.
_GATE_RETURN_PATHS = {"/onboarding/mailbox", "/app/settings", "/app/mailboxes"}


def _tenant_has_gmail(tenant_id: str) -> bool:
    """Does this tenant already hold a Google grant? Re-consenting the same
    account doesn't burn a new slot, so existing Gmail tenants (e.g. the two
    live founding connections) are never gated on reconnect."""
    try:
        return any(mailbox.connection_provider(m, tenant_id) == "google"
                   for m in db.list_mailboxes(tenant_id))
    except Exception:
        return False


def _google_connect_allowed(request: Request, tenant_id: str) -> bool:
    """Server-side Gmail-connect gate (src/app/slots.py). Enforced HERE — not just
    by hiding buttons — so a crafted /oauth/connect URL can't burn one of the
    100 unverified-app grants. Verification lifts everything; a tenant already
    holding a Google grant may always reconnect; a full cap blocks even coded
    visitors (Google would refuse the grant anyway); otherwise the armed gate
    wants a valid code cookie or an email-bound existing invite from POST
    /oauth/google-invite."""
    s = slots.google_slots()
    if s["verified"]:
        return True
    if _tenant_has_gmail(tenant_id):
        return True
    if s["full"]:
        return False
    if not slots.invite_gate_active():
        return True
    return (slots.invite_cookie_valid(request.cookies.get(slots.INVITE_COOKIE))
            or slots.google_invite_email_from_cookie(request.cookies.get(slots.INVITE_EMAIL_COOKIE)) is not None)


def _gate_return_path(tenant_id: str, mode: str) -> str:
    """Where a blocked/failed gate interaction lands: the connect surface the
    tenant is working from."""
    if mode == "add":
        return "/app/mailboxes"
    return "/app/settings" if db.is_onboarded(tenant_id) else "/onboarding/mailbox"


@router.get("/connect")
def connect(request: Request, provider: str = "microsoft", mode: str = "replace",
                  ctx: TenantCtx = Depends(require_tenant)):
    """Kick off mailbox consent for the tenant — Microsoft (default) or Google.

    Google is the capped provider (100 restricted-scope grants until CASA
    verification), so Gmail connects are invite-gated here while slots are
    scarce; Microsoft always proceeds.

    `mode=add` (the enterprise "Add mailbox" link) connects ALONGSIDE the
    existing mailboxes instead of replacing them. Only an enterprise tenant may
    add — the mode is resolved against the plan HERE and bound server-side into
    oauth_states, so the callback never trusts the URL."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    if provider == "google" and not _google_connect_allowed(request, ctx.tenant_id):
        gmail_msg = "full" if slots.google_slots()["full"] else "gate"
        return RedirectResponse(f"{_gate_return_path(ctx.tenant_id, mode)}?gmail={gmail_msg}",
                                status_code=303)
    state = secrets.token_urlsafe(24)
    # 'add' (connect ALONGSIDE existing) is allowed for enterprise (unlimited) and
    # any tenant on an active paid plan (extra mailboxes beyond the plan's included
    # allowance bill as the add-on). A pilot with no active subscription falls back
    # to 'replace' — it stays single-mailbox. Resolved server-side; the callback
    # never trusts the URL.
    effective_mode = "add" if (mode == "add" and billing.can_add_mailbox(ctx.tenant_id)) else "replace"
    db.put_oauth_state(state, ctx.tenant_id, mode=effective_mode)
    url = gmail.build_authorize_url(state) if provider == "google" else graph.build_authorize_url(state)
    resp = RedirectResponse(url)
    _security.bind_consent(resp, state, "/oauth")   # M8: only this browser may finish it
    return resp


# ── one person's own calendar (People slice 5) ─────────────────────────────
def _member_calendar_allowed(tenant_id: str, user_id: str) -> bool:
    """A person's calendar grant draws on the SAME 100 unverified-app grants as
    a Gmail mailbox: Google's cap counts users who authorise an unapproved
    sensitive OR restricted scope, and calendar.events is sensitive.

    The mailbox gate's "this tenant already holds a Google grant" escape does
    NOT apply here — that is true because re-consenting the same Google account
    is free, and a staff member is a different Google user. What IS free is
    RECONNECTING their own, so that is the only way past a full cap. No invite
    code: the gate exists to stop public advertising burning slots, and this
    person is already inside the business."""
    s = slots.google_slots()
    if s["verified"]:
        return True
    if db.get_member_calendar(tenant_id, user_id):
        return True
    return not s["full"]


@router.get("/my-calendar")
def my_calendar(request: Request, provider: str = "google",
                ctx: TenantCtx = Depends(require_tenant)):
    """Start the consent for THIS person's own calendar (docs/plan-people.md
    slice 5). Asks for calendar events and nothing else — no mail scope — and
    binds whose calendar it is into oauth_states server-side, so the callback
    never takes a user id from the URL."""
    provider = member_calendar.normalise_provider(provider)
    back = f"/app/people/{ctx.user_id}"
    if not ctx.user_id:
        return RedirectResponse("/app/", status_code=303)
    if provider == "google" and not _member_calendar_allowed(ctx.tenant_id, ctx.user_id):
        return RedirectResponse(f"{back}?cal=full", status_code=303)
    state = secrets.token_urlsafe(24)
    db.put_oauth_state(state, ctx.tenant_id, mode=member_calendar.MODE, user_id=ctx.user_id)
    resp = RedirectResponse(member_calendar.authorize_url(provider, state))
    _security.bind_consent(resp, state, "/oauth")   # M8
    return resp


async def _finish_member_calendar(request: Request, tenant_id: str, user_id: str | None,
                                  provider: str, code: str):
    """Both callbacks land here when the state says 'mycal'. Saves the grant
    against the person and sends them back to their own page — email_connections
    is never touched, so a person's calendar can never become the business's
    mailbox or its diary."""
    back = f"/app/people/{user_id}" if user_id else "/app/"
    if not user_id:
        return templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Connection failed",
             "message": "We lost track of whose calendar that was. Please try again.",
             "next_url": "/app/", "cta": "Back to BLAM8"}, status_code=400)
    try:
        tokens = await member_calendar.exchange(provider, code)
        access = tokens["access_token"]
        expires = member_calendar.backend(provider).expiry_from_now(tokens["expires_in"])
    except httpx.HTTPStatusError as e:
        logging.getLogger("blautom8.oauth").warning(
            "member calendar exchange failed: %s", e.response.status_code)
        return templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Couldn’t connect your calendar",
             "message": "We couldn’t finish the connection. Please try again.",
             "next_url": back, "cta": "Back to your page"}, status_code=502)
    if not tokens.get("refresh_token"):
        # Without one the grant dies at the first token expiry and the person is
        # left wondering why their calendar went quiet — better to say so now.
        return templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Couldn’t connect your calendar",
             "message": "Your calendar provider didn’t give us lasting permission. "
                        "Try again, and say yes to staying connected.",
             "next_url": back, "cta": "Back to your page"}, status_code=502)
    try:
        db.save_member_calendar(tenant_id, user_id, provider, access,
                                tokens.get("refresh_token"), expires,
                                calendar_email=member_calendar.account_email(tokens))
    except Exception:
        logging.getLogger("blautom8.oauth").warning("member calendar save failed", exc_info=True)
        return templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Couldn’t connect your calendar",
             "message": "We couldn’t save the connection. Please try again.",
             "next_url": back, "cta": "Back to your page"}, status_code=500)
    # Everything already on the books that is theirs, in their calendar now —
    # a connection that only works from tomorrow feels broken today.
    _spawn_member_backfill(tenant_id, user_id)
    return RedirectResponse(f"{back}?cal=on", status_code=303)


def _spawn_member_backfill(tenant_id: str, user_id: str) -> None:
    """Fill a newly connected calendar behind the redirect (the backlog-import
    pattern). A context with no loop just skips it — the next booking pushes."""
    try:
        from app import calendar_sync
        task = asyncio.get_running_loop().create_task(
            calendar_sync.push_to_people(tenant_id, user_id=user_id))
        _BACKLOG_TASKS.add(task)
        task.add_done_callback(_BACKLOG_TASKS.discard)
    except Exception:
        logging.getLogger("blautom8.oauth").warning("member calendar backfill not started",
                                                    exc_info=True)


# ── a tappable invite (LittleDogs, 2026-09-10) ──────────────────────────────
# The operator sends the owner ONE link carrying their single-use code. Signed
# in, the page names the inbox connected today and shows one button that posts
# the same /oauth/google-invite form the Settings gate renders (code prefilled,
# mode=replace) — so swapping a mailbox is: tap the link, tap Connect, approve
# in Google. The first real owner had a personal Hotmail connected and
# "go to Settings, disconnect, type the code" was judged enough to lose them.
# A GET has no side effect (chat apps prefetch links); only the POST redeems,
# so nothing here bypasses the gate.
_INVITE_PATH_RE = re.compile(r"^/oauth/invite/[A-Za-z0-9-]{4,40}$")


def _gate_next(next_: str | None) -> str:
    """Where a gate POST returns: one of the fixed connect surfaces, or the
    invite page the owner tapped (so "already used" lands where they are)."""
    if next_ in _GATE_RETURN_PATHS or _INVITE_PATH_RE.match(next_ or ""):
        return next_
    return "/app/settings"


@router.get("/invite/{code}")
def invite_page(code: str, request: Request, ctx: TenantCtx = Depends(require_tenant)):
    if not _INVITE_PATH_RE.match(f"/oauth/invite/{code}"):
        return RedirectResponse("/app/settings", status_code=303)
    t = db.get_tenant(ctx.tenant_id) or {}
    current = [m.get("address") for m in db.list_mailboxes(ctx.tenant_id) if m.get("address")]
    # A phone that already passed the gate (the signed cookie from an earlier
    # code, a verified app, a tenant holding a Google grant) goes straight to
    # Google — no code consumed, and a link whose code was burned still works
    # there. LittleDogs redeemed their code at 13:01, turned back at Google's
    # screen, and connected a Hotmail instead; the cookie on that phone stands.
    allowed = _google_connect_allowed(request, ctx.tenant_id)
    return templates.TemplateResponse(request, "oauth_invite.html", {
        "code": code,
        "business": t.get("name") or "your business",
        "current": current,
        "allowed": allowed,
        "gate": slots.invite_gate_active(),
        "full": slots.google_slots()["full"],
        "msg": request.query_params.get("gmail"),
        "next_path": f"/oauth/invite/{code}",
    })


@router.post("/google-invite", dependencies=[Depends(require_csrf),
                                             Depends(rate_limit("google_invite", 10, 300))])
def google_invite(code: str = Form(""), email: str = Form(""), mode: str = Form("replace"),
                  next: str = Form("/app/settings"), ctx: TenantCtx = Depends(require_tenant)):
    """Redeem a code or look up an existing invite by its registered email.

    Email-addressed invites remain unused until the callback confirms the
    selected Google account, so a cancelled or mismatched consent can be retried.
    """
    nxt = _gate_next(next)
    if not slots.invite_gate_active():
        return RedirectResponse(f"/oauth/connect?provider=google&mode={mode}", status_code=303)
    if slots.google_invite_email_available(email):
        resp = RedirectResponse(f"/oauth/connect?provider=google&mode={mode}", status_code=303)
        resp.set_cookie(slots.INVITE_EMAIL_COOKIE, slots.google_invite_email_cookie_value(email),
                        max_age=slots.INVITE_EMAIL_COOKIE_TTL_SECONDS,
                        httponly=True, secure=True, samesite="lax", path="/")
        return resp
    if not slots.redeem_invite(code):
        resp = RedirectResponse(f"{nxt}?gmail=badcode", status_code=303)
        resp.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return resp
    resp = RedirectResponse(f"/oauth/connect?provider=google&mode={mode}", status_code=303)
    resp.set_cookie(slots.INVITE_COOKIE, slots.invite_cookie_value(), max_age=60 * 60 * 24 * 90,
                    httponly=True, secure=True, samesite="lax", path="/")
    resp.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
    return resp


@router.post("/google-email-check", dependencies=[Depends(require_csrf),
                                                   Depends(rate_limit("google_email_check", 20, 300))])
def google_email_check(email: str = Form(""), ctx: TenantCtx = Depends(require_tenant)):
    """Progressive UI check for an active invite addressed to this email."""
    return JSONResponse({"eligible": slots.google_invite_email_available(email)})


@router.post("/request-google-access", dependencies=[Depends(require_csrf),
                                                     Depends(rate_limit("access_request", 5, 3600))])
async def request_google_access(email: str = Form(""), message: str = Form(""),
                                next: str = Form("/app/settings"),
                                ctx: TenantCtx = Depends(require_tenant)):
    """The gate's "no code? request access" path: queue the request for vetting
    in /admin/invites, and best-effort email the operator about it — sent from
    the company tenant's own connected mailbox (PLATFORM_NOTIFY_MAILBOX) to the
    notify address, so once hello@'s automated tenant is live the request lands
    straight in its inbox. Notification failure never loses the request."""
    nxt = _gate_next(next)
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return RedirectResponse(f"{nxt}?gmail=bademail", status_code=303)
    req = db.create_google_access_request(email, message[:2000])
    if settings.platform_notify_mailbox:
        try:
            conn = db.get_connection_by_address(settings.platform_notify_mailbox)
            if conn:
                body = (f"Someone requested a Gmail connection slot on BLAM8.\n\n"
                        f"Email: {email}\n"
                        f"About their business: {message.strip() or '(nothing added)'}\n\n"
                        f"Vet and invite (or dismiss) here: "
                        f"{settings.app_base_url}/admin/invites\n"
                        f"Request id: {req['id']}")
                await mailbox.send_mail(conn["tenant_id"], settings.access_request_notify_to,
                                        "BLAM8 access request — " + email, body,
                                        mailbox_id=conn["id"])
        except Exception:
            pass   # the request is safely queued either way
    return RedirectResponse(f"{nxt}?gmail=requested", status_code=303)


_BACKLOG_TASKS: set = set()   # strong refs so a running import is never garbage-collected


@router.get("/callback", dependencies=[Depends(rate_limit("oauth_callback", 20, 600))])
async def callback(request: Request):
    params = dict(request.query_params)
    if "error" in params:
        # Never reflect the upstream error/description (was reflected XSS).
        return templates.TemplateResponse(
            request,
            "oauth_result.html",
            {
                "ok": False,
                "title": "Connection failed",
                "message": "Microsoft reported an error completing the connection. Please try again.",
                "next_url": "/app/conversations",
            },
            status_code=400,
        )

    code, state = params.get("code"), params.get("state")
    tenant_id, mode, state_user = (db.pop_oauth_state(state) if (code and state)
                                   else (None, "replace", None))
    if tenant_id is not None and not _security.consent_is_this_browsers(request, state):
        log.warning("oauth callback: state not started in this browser (tenant=%s) — refused", tenant_id)
        tenant_id = None                                        # M8
    if tenant_id is None:
        return templates.TemplateResponse(
            request,
            "oauth_result.html",
            {
                "ok": False,
                "title": "Connection failed",
                "message": "That sign-in link was invalid or expired. Please try connecting again.",
                "next_url": "/app/conversations",
            },
            status_code=400,
        )
    # One person's own calendar — a different grant entirely (see slice 5).
    # Branched before the exchange, which would otherwise ask for mail scopes.
    if mode == member_calendar.MODE:
        return await _finish_member_calendar(request, tenant_id, state_user, "microsoft", code)

    try:
        tokens = await graph.exchange_code_for_tokens(code)
        access = tokens["access_token"]
        expires = graph.expiry_from_now(tokens["expires_in"])
        me = await graph.graph_get(
            access, "/me", {"$select": "mail,userPrincipalName,displayName"}
        )
    except httpx.HTTPStatusError as e:
        # Never echo the upstream response body (was an info/secret leak).
        logging.getLogger("blautom8.oauth").warning(
            "Token exchange failed: %s", e.response.status_code
        )
        return templates.TemplateResponse(
            request,
            "oauth_result.html",
            {
                "ok": False,
                "title": "Couldn’t connect that mailbox",
                "message": "We couldn’t complete the connection with Microsoft. Please try again.",
                "next_url": "/app/conversations",
            },
            status_code=502,
        )

    mailbox = me.get("mail") or me.get("userPrincipalName")
    was_onboarded = db.is_onboarded(tenant_id)
    conn_id = db.upsert_email_connection(
        tenant_id, access, tokens.get("refresh_token"), expires,
        address=mailbox, provider="microsoft", mode=mode,
    )
    # tenants.mailbox/provider mirror the default (oldest) mailbox — legacy reads.
    db.sync_tenant_mailbox_mirror(tenant_id)
    # A newly added mailbox may push a paid tenant past its included allowance —
    # reconcile the extra-mailbox add-on. Best-effort: never block the connection.
    # No-op for pilots (no active sub) and enterprise (unlimited).
    try:
        billing.sync_mailbox_addon(tenant_id)
    except Exception:
        logging.getLogger("blautom8.oauth").warning("mailbox add-on sync failed", exc_info=True)
    # Calendar Connect: Microsoft echoes the granted scopes back — when calendar
    # access is among them, light the busy-sync + push for this connection.
    if "Calendars" in (tokens.get("scope") or ""):
        db.set_calendar_connected(conn_id)

    # Subscribe to new mail so drafting is automatic (Days 10-11). Best-effort:
    # the manual trigger still works if this fails (e.g. URL not reachable yet).
    try:
        sub = await graph.create_subscription(
            access, f"{settings.app_base_url}/webhook", settings.graph_webhook_secret
        )
        db.set_subscription(conn_id, sub["id"], sub["expirationDateTime"])
    except Exception:
        pass
    # Historical import is a separate owner choice after connection.

    # Mid-onboarding? Return to the wizard so they continue the guided setup.
    # Voice auto-learning is OFFERED (opt-in) at the voice step — deliberately
    # not started here, so nothing reads their sent mail without an explicit yes.
    if not was_onboarded:
        return RedirectResponse("/onboarding/mailbox", status_code=303)

    return templates.TemplateResponse(
        request,
        "oauth_result.html",
        {
            "ok": True,
            "title": "Mailbox connected",
            "mailbox": mailbox,
            "message": "is now connected to BLAM8 — draft-only, nothing sends without a human.",
            "next_url": "/app/conversations",
        },
    )


@router.get("/google/callback", dependencies=[Depends(rate_limit("oauth_callback", 20, 600))])
async def google_callback(request: Request):
    params = dict(request.query_params)
    if "error" in params:
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Connection failed",
             "message": "Google reported an error completing the connection. Please try again.",
             "next_url": "/app/conversations"}, status_code=400)
        response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return response

    code, state = params.get("code"), params.get("state")
    tenant_id, mode, state_user = (db.pop_oauth_state(state) if (code and state)
                                   else (None, "replace", None))
    if tenant_id is not None and not _security.consent_is_this_browsers(request, state):
        log.warning("google callback: state not started in this browser (tenant=%s) — refused", tenant_id)
        tenant_id = None                                        # M8
    if tenant_id is None:
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Connection failed",
             "message": "That sign-in link was invalid or expired. Please try connecting again.",
             "next_url": "/app/conversations"}, status_code=400)
        response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return response
    # One person's own calendar (slice 5): branched before the exchange, and
    # before get_profile_email — a calendar-only token may not read a profile.
    if mode == member_calendar.MODE:
        return await _finish_member_calendar(request, tenant_id, state_user, "google", code)

    try:
        tokens = await gmail.exchange_code_for_tokens(code)
        access = tokens["access_token"]
        expires = gmail.expiry_from_now(tokens["expires_in"])
        mailbox_addr = await gmail.get_profile_email(access)
    except httpx.HTTPStatusError as e:
        logging.getLogger("blautom8.oauth").warning("Google token exchange failed: %s", e.response.status_code)
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Couldn’t connect that mailbox",
             "message": "We couldn’t complete the connection with Google. Please try again.",
             "next_url": "/app/conversations"}, status_code=502)
        response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return response

    expected_invite_email = slots.google_invite_email_from_cookie(
        request.cookies.get(slots.INVITE_EMAIL_COOKIE))
    if expected_invite_email and slots.normalize_google_email(mailbox_addr) != expected_invite_email:
        log.warning("Google invite email mismatch; refusing mailbox connect")
        try:
            await httpx.post("https://oauth2.googleapis.com/revoke", data={"token": access}, timeout=8)
        except Exception:
            log.warning("Could not revoke mismatched Google invite token", exc_info=True)
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "Choose the invited Google account",
             "message": f"Google returned {mailbox_addr or 'a different account'}, but the existing BLAM8 invite is for {expected_invite_email}. No mailbox was connected. Go back and choose the invited account.",
             "next_url": "/onboarding/mailbox" if not db.is_onboarded(tenant_id) else "/app/mailboxes"},
            status_code=400)
        response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return response
    if expected_invite_email and not db.use_google_invite_for_email(expected_invite_email):
        log.warning("Google invite email was no longer available at callback")
        try:
            await httpx.post("https://oauth2.googleapis.com/revoke", data={"token": access}, timeout=8)
        except Exception:
            log.warning("Could not revoke token for unavailable Google invite", exc_info=True)
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": False, "title": "This Gmail invite is no longer available",
             "message": "The invite for this Google account has already been used or revoked. No mailbox was connected.",
             "next_url": "/onboarding/mailbox" if not db.is_onboarded(tenant_id) else "/app/mailboxes"},
            status_code=400)
        response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
        return response

    was_onboarded = db.is_onboarded(tenant_id)
    conn_id = db.upsert_email_connection(
        tenant_id, access, tokens.get("refresh_token"), expires,
        address=mailbox_addr, provider="google", mode=mode,
    )
    db.sync_tenant_mailbox_mirror(tenant_id)
    # A newly added mailbox may push a paid tenant past its included allowance —
    # reconcile the extra-mailbox add-on. Best-effort; no-op for pilots/enterprise.
    try:
        billing.sync_mailbox_addon(tenant_id)
    except Exception:
        logging.getLogger("blautom8.oauth").warning("mailbox add-on sync failed", exc_info=True)
    # Calendar Connect: Google echoes the granted scopes back (incremental consent
    # merges them) — when calendar access is among them, light push + busy-sync.
    if "calendar" in (tokens.get("scope") or ""):
        db.set_calendar_connected(conn_id)

    # Baseline the Gmail history watermark to "now" so the poller (app/jobs/
    # poll_gmail.py) only auto-drafts mail that arrives AFTER connection — never
    # the pre-existing inbox backlog. Best-effort: the poller self-baselines too.
    try:
        hid = await gmail.current_history_id(access)
        if hid:
            db.set_gmail_history_id(conn_id, hid)
    except Exception:
        pass
    # Gmail push: arm users.watch → Pub/Sub for this mailbox so inbound drafts
    # the moment it lands instead of on the next poll. Best-effort — the renewal
    # loop (src/app/jobs/renew_subscriptions.py) arms/repairs it too, and the poller
    # stays on as the safety net. No-op until GMAIL_PUBSUB_TOPIC is configured.
    if settings.gmail_pubsub_topic:
        try:
            res = await gmail.watch_mailbox(access, settings.gmail_pubsub_topic)
            exp_ms = int(res.get("expiration") or 0)
            if exp_ms:
                db.set_gmail_watch_expiry(
                    conn_id, datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).isoformat())
        except Exception:
            logging.getLogger("blautom8.oauth").warning("gmail watch arm failed", exc_info=True)
    # Preserve the new-mail watermark; historical import needs its own choice.
    if not was_onboarded:
        # Voice auto-learning is opt-in at the voice step (see the Microsoft
        # callback above) — never started from a connect.
        response = RedirectResponse("/onboarding/mailbox", status_code=303)
    else:
        response = templates.TemplateResponse(
            request, "oauth_result.html",
            {"ok": True, "title": "Mailbox connected", "mailbox": mailbox_addr,
             "message": "is now connected to BLAM8 — draft-only, nothing sends without a human.",
             "next_url": "/app/conversations"})
    response.delete_cookie(slots.INVITE_EMAIL_COOKIE, path="/")
    return response


@router.get("/status")
def status(ctx: TenantCtx = Depends(require_tenant)):
    conn = db.get_email_connection(ctx.tenant_id, client=ctx.client)
    return {
        "connected": bool(conn),
        "token_expires": conn["token_expires"] if conn else None,
    }


async def _create_subscription_for(tenant_id: str, connection_id: str | None = None) -> bool:
    """(Re)build the Graph mail subscription from the SAVED token — no user
    re-consent. Drops any stale subscription first. Returns True on success.
    This is what makes auto-draft self-recoverable without reconnecting.
    `connection_id` targets one mailbox; default = the tenant's default mailbox."""
    conn = (
        db.get_connection_by_id(connection_id, tenant_id) if connection_id
        else db.get_email_connection(tenant_id)
    )
    if not conn or mailbox.connection_provider(conn, tenant_id) == "google":
        return False
    access = await mailbox.valid_access_token(conn, "microsoft")
    if conn.get("subscription_id"):
        try:
            await graph.delete_subscription(access, conn["subscription_id"])
        except Exception:
            pass
    sub = await graph.create_subscription(
        access, f"{settings.app_base_url}/webhook", settings.graph_webhook_secret
    )
    db.set_subscription(conn["id"], sub["id"], sub["expirationDateTime"])
    return True


@router.post("/resubscribe", dependencies=[Depends(require_csrf)])
async def resubscribe(ctx: TenantCtx = Depends(require_tenant)):
    """Re-enable auto-draft: rebuild the mailbox watch from the saved token (Microsoft
    only; Gmail auto-draft does not use Graph subscriptions — it polls)."""
    try:
        ok = await _create_subscription_for(ctx.tenant_id)
    except Exception:
        ok = False
    return RedirectResponse(
        f"/app/settings?mailbox={'resubscribed' if ok else 'suberror'}", status_code=303
    )


async def _delete_graph_subscription(conn: dict, tenant_id: str) -> None:
    """Best-effort teardown of one connection's Graph watch before disconnecting."""
    if not conn or not conn.get("subscription_id"):
        return
    if mailbox.connection_provider(conn, tenant_id) == "google":
        return
    try:
        access = await mailbox.valid_access_token(conn, "microsoft")
        await graph.delete_subscription(access, conn["subscription_id"])
    except Exception:
        pass


@router.post("/disconnect", dependencies=[Depends(require_csrf)])
async def disconnect(ctx: TenantCtx = Depends(require_tenant)):
    """Sign the mailbox out: delete the Graph subscription + the stored connection,
    so a different account can be connected cleanly. SaaS single-mailbox path —
    an enterprise tenant with several mailboxes manages them on the Mailboxes
    page (per-connection), so this never nukes siblings."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    if db.is_enterprise(ctx.tenant_id) and len(db.list_mailboxes(ctx.tenant_id)) > 1:
        return RedirectResponse("/app/mailboxes", status_code=303)
    conn = db.get_email_connection(ctx.tenant_id)
    await _delete_graph_subscription(conn, ctx.tenant_id)
    db.delete_email_connection(ctx.tenant_id)
    db.sync_tenant_mailbox_mirror(ctx.tenant_id)
    return RedirectResponse("/app/settings?mailbox=disconnected", status_code=303)
