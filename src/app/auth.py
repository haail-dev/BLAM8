"""Supabase Auth (GoTrue) over server-side httpOnly cookies.

Registration stores the company name on the user; the tenant (company) +
membership are created on first login. The dashboard depends on `require_tenant`,
which validates the session cookie and resolves the user's tenant.
"""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, Request, Response
from supabase import Client
from starlette.concurrency import run_in_threadpool

from app import actor, db, navcache, viewas
from app.startup_timing import instrument, span, mark, pool_state
import logging as _logging
log = _logging.getLogger(__name__)
from app.config import settings

AUTH_BASE = f"{settings.supabase_url}/auth/v1"
_HEADERS = {"apikey": settings.supabase_anon_key, "Content-Type": "application/json"}

AT_COOKIE = "bl_at"
RT_COOKIE = "bl_rt"
PKCE_COOKIE = "bl_pkce"
_WEEK = 60 * 60 * 24 * 7

# SSO is the only sign-in path — email/password was removed because every
# recovery flow (reset, magic link) depends on email, which our Supabase SMTP
# can't deliver to Gmail/Outlook. Both providers are Supabase Auth (GoTrue)
# OAuth, reusing the existing Graph/Gmail OAuth app registrations.
SSO_PROVIDERS = {"google", "azure"}

# A personal mailbox's domain ('gmail.com') is a useless company name, so for
# these we derive the default tenant name from the local part instead.
_CONSUMER_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                     "hotmail.co.uk", "outlook.co.uk", "live.com", "live.co.uk",
                     "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "btinternet.com",
                     "sky.com", "aol.com", "protonmail.com", "proton.me"}


def is_consumer_address(email: str | None) -> bool:
    """True for a personal-provider address (gmail.com, hotmail.co.uk…) — the
    mailbox step asks "is this where enquiries arrive?" for these, because a
    business's customer inbox usually sits on its own domain."""
    e = (email or "").strip().lower()
    return "@" in e and e.rsplit("@", 1)[1] in _CONSUMER_DOMAINS


class AuthRedirect(Exception):
    """Raised when a protected route has no valid session → redirect to /login."""


@dataclass
class TenantCtx:
    """Per-request auth context: the caller's tenant id + a Supabase client bound
    to their JWT (so RLS enforces tenant isolation behind the in-app filters)."""

    tenant_id: str
    client: Client
    # The signed-in Supabase user id — attribution only (e.g. who asked the
    # concierge). Optional so tests and older call sites need not care.
    user_id: str | None = None
    # View as (src/app/viewas.py): the REAL owner's id while they view the app as
    # one of their people. user_id is then the person being viewed, so every
    # page answers as them; anything that writes checks this and stands down.
    viewing_as: str | None = None


def _pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) for the OAuth PKCE handshake."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def sso_authorize_url(provider: str, redirect_to: str) -> tuple[str, str]:
    """(GoTrue authorize URL, PKCE verifier) for an SSO sign-in. The verifier is
    held in a short-lived cookie and replayed on the callback to exchange the
    code, so an intercepted code is useless without this browser's cookie."""
    verifier, challenge = _pkce_pair()
    params = {
        "provider": provider,
        "redirect_to": redirect_to,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        # Always show the account chooser. Without this, Google (and Microsoft)
        # silently reuse the browser's current session — after a /logout you
        # can't sign in as a different account. GoTrue forwards unknown query
        # params to the provider; `prompt=select_account` is standard OIDC.
        "prompt": "select_account",
    }
    # GoTrue's Azure default is a bare `openid` scope, and personal Microsoft
    # accounts (outlook.com/live.com) then omit the email claim from the ID
    # token — the callback dies with "Error getting user email from external
    # provider" and NO Microsoft sign-in can complete. Ask for the standard
    # OIDC claims explicitly (Supabase's documented Azure fix); GoTrue appends
    # these to its default, so Microsoft sees `openid email profile`.
    if provider == "azure":
        params["scopes"] = "email profile"
    return f"{AUTH_BASE}/authorize?{urlencode(params)}", verifier


async def exchange_code_for_session(auth_code: str, code_verifier: str) -> dict:
    """Trade the OAuth code + PKCE verifier for a GoTrue session
    (access_token, refresh_token, user)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AUTH_BASE}/token?grant_type=pkce",
            headers=_HEADERS,
            json={"auth_code": auth_code, "code_verifier": code_verifier},
        )
    data = r.json()
    if r.status_code >= 400:
        raise ValueError(data.get("error_description") or data.get("msg") or "Sign-in failed")
    return data


async def password_login(email: str, password: str) -> dict:
    """Email + password sign-in via GoTrue (the password grant). Used ONLY by the
    operator console (/admin) — a single internal operator who knows their password and
    needs no email-recovery flow. The TENANT dashboard stays SSO-only (SSO_PROVIDERS):
    customer recovery (reset / magic link) depends on email delivery we can't guarantee.
    Returns the GoTrue session {access_token, refresh_token, user}; raises ValueError with
    a clean message on bad credentials."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AUTH_BASE}/token?grant_type=password",
            headers=_HEADERS,
            json={"email": email, "password": password},
        )
    data = r.json()
    if r.status_code >= 400:
        raise ValueError(data.get("error_description") or data.get("msg") or "Invalid email or password")
    return data


def default_company_name(email: str | None) -> str | None:
    """A sensible initial company name from the sign-in email — the domain's main
    label for work email, the local part for a personal mailbox. The tenant can
    rename it later; None falls back to a placeholder."""
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    base = local if domain.lower() in _CONSUMER_DOMAINS else domain.split(".")[0]
    return base.replace(".", " ").replace("_", " ").replace("-", " ").strip().title() or None


@instrument("identity_http")
async def get_user(access_token: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{AUTH_BASE}/user",
            headers={**_HEADERS, "Authorization": f"Bearer {access_token}"},
        )
    return r.json() if r.status_code < 400 else None


@instrument("refresh_http")
async def refresh_session(refresh_token: str) -> dict | None:
    """Exchange a refresh token for a fresh session (GoTrue). None on failure."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AUTH_BASE}/token?grant_type=refresh_token",
            headers=_HEADERS,
            json={"refresh_token": refresh_token},
        )
    return r.json() if r.status_code < 400 else None


async def admin_delete_user(user_id: str) -> None:
    """Remove an auth user (service role). Used when an uninvited Google sign-up
    is rejected at the SSO callback — the just-created GoTrue user is deleted so
    auth.users and the Google slot count stay clean."""
    async with httpx.AsyncClient(timeout=30) as c:
        await c.delete(
            f"{AUTH_BASE}/admin/users/{user_id}",
            headers={"apikey": settings.supabase_service_key,
                     "Authorization": f"Bearer {settings.supabase_service_key}"},
        )


def set_session_cookies(response, session: dict) -> None:
    common = dict(httponly=True, secure=True, samesite="lax", path="/")
    response.set_cookie(AT_COOKIE, session["access_token"], max_age=_WEEK, **common)
    response.set_cookie(RT_COOKIE, session.get("refresh_token", ""), max_age=_WEEK * 4, **common)


def revoke_session(access: str | None, scope: str = "local") -> None:
    """End the session at Supabase, not just in this browser (security review
    2026-09, M6: sign-out only deleted cookies, so a copied refresh token kept
    working). `scope`: 'local' = this session, 'global' = every device. Also
    drops the server's cached user for this token. Best-effort, never raises."""
    if not access:
        return
    try:
        navcache.invalidate("u:" + _token_key(access))
        navcache.invalidate("us:" + _token_key(access))
        navcache.invalidate("c:" + _token_key(access))
    except Exception:
        pass
    try:
        httpx.post(f"{AUTH_BASE}/logout", params={"scope": scope}, timeout=5,
                   headers={"apikey": settings.supabase_anon_key or settings.supabase_service_key,
                            "Authorization": f"Bearer {access}"})
    except Exception:
        log.warning("session revoke at sign-out failed", exc_info=True)


def clear_session_cookies(response) -> None:
    response.delete_cookie(AT_COOKIE, path="/")
    response.delete_cookie(RT_COOKIE, path="/")


def set_pkce_cookie(response, verifier: str) -> None:
    """Stash the PKCE verifier for the round-trip to the provider — short-lived,
    httpOnly, SameSite=Lax so it survives the top-level redirect back to /auth/callback."""
    response.set_cookie(PKCE_COOKIE, verifier, max_age=600, httponly=True,
                        secure=True, samesite="lax", path="/")


def clear_pkce_cookie(response) -> None:
    response.delete_cookie(PKCE_COOKIE, path="/")


def _token_key(access: str) -> str:
    """Cache key for an access token — its hash, never the token itself."""
    return hashlib.sha256(access.encode()).hexdigest()[:32]


def _token_ttl(access: str, cap: float) -> float:
    """How long a cache entry may trust this JWT: capped, and never past the
    token's own exp (minus a 30s guard) — so a cached user/client can't outlive
    the token's validity and turn into surprise 401s mid-window."""
    try:
        payload = access.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        return max(0.0, min(cap, float(exp) - time.time() - 30.0))
    except Exception:
        return 0.0


def seed_identity(session: dict) -> None:
    """Warm the identity cache from a session GoTrue has just minted (sign-in or refresh): its
    response carries the user, so the first request after sign-in need not ask /user again
    (~0.9 s on the page that lands them in the app). Same key, same token-bounded TTL as the
    normal fill; a no-op when the response has no usable user or the token has no life left."""
    try:
        user = session.get("user")
        access = session.get("access_token")
        if not (access and isinstance(user, dict) and user.get("id")):
            return
        ttl = _token_ttl(access, cap=60.0)
        if ttl:
            navcache.put("u:" + _token_key(access), ttl, dict(user))
            _remember_stale(access, user)
    except Exception:
        log.debug("identity seed skipped", exc_info=True)


def _remember_stale(access: str, user: dict) -> None:
    """Keep a copy of a freshly validated identity for the stale-while-revalidate window (see
    require_tenant). Bounded by the token's own life; a no-op when the feature is off."""
    swr = settings.identity_swr_seconds
    if swr <= 0:
        return
    ttl = _token_ttl(access, cap=float(swr))
    if ttl:
        navcache.put("us:" + _token_key(access), ttl, dict(user))


_DEFINITELY_INVALID = {400, 401, 403, 404}
_revalidating: set[str] = set()
_bg_tasks: set = set()


async def _check_user(access: str) -> tuple[int | None, dict | None]:
    """(status, user) from GoTrue /user: unlike get_user it keeps the status, so a rejected session
    (401/403: revoked, expired, deleted) is told apart from a brown-out (5xx, 429, a timeout)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{AUTH_BASE}/user",
                            headers={**_HEADERS, "Authorization": f"Bearer {access}"})
    except Exception:
        return None, None
    if r.status_code < 400:
        try:
            return r.status_code, r.json()
        except Exception:
            return None, None
    return r.status_code, None


def _revalidate_in_background(access: str) -> None:
    """Ask GoTrue again off the request path (one task per token at a time). A rejected session drops
    every cached copy, so the very next request validates live and is sent to sign in; a success
    renews both caches; anything uncertain leaves the stale copy to run out on its own."""
    key = _token_key(access)
    if key in _revalidating:
        return
    _revalidating.add(key)

    async def run():
        try:
            status, user = await _check_user(access)
            if user and user.get("id"):
                ttl = _token_ttl(access, cap=60.0)
                if ttl:
                    navcache.put("u:" + key, ttl, dict(user))
                _remember_stale(access, user)
            elif status in _DEFINITELY_INVALID:
                for prefix in ("u:", "us:", "c:"):
                    navcache.invalidate(prefix + key)
        except Exception:
            log.debug("identity revalidation failed", exc_info=True)
        finally:
            _revalidating.discard(key)
    task = asyncio.get_running_loop().create_task(run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _token_expired(access: str) -> bool:
    """True only when the access token is a JWT whose own exp has passed. Anything that does not
    parse (the plain strings tests and legacy sessions use) is NOT called expired, so it still goes
    to GoTrue and gets the answer GoTrue always gave. An expired JWT is rejected by GoTrue anyway;
    asking costs a full round trip (0.3-0.9 s) before the refresh that has to happen regardless."""
    try:
        payload = access.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return isinstance(exp, (int, float)) and float(exp) <= time.time()
    except Exception:
        return False


@instrument("auth")
async def require_tenant(request: Request, response: Response) -> TenantCtx:
    """Dependency for /app routes: a valid session resolving to a tenant, plus a
    Supabase client bound to the user's JWT. Silently refreshes an expired access
    token with the refresh-token cookie before giving up.

    The chain used to cost 3+ HTTPS round trips on EVERY navigation (GoTrue
    /user, the tenant_members read, a fresh PostgREST client's TLS handshake).
    Each leg now sits in the navcache for a short, token-bounded window."""
    access = request.cookies.get(AT_COOKIE)
    user = None
    if access:
        hit, user = navcache.peek("u:" + _token_key(access))
        mark("identity_cache_hit", hit)
        if not hit:
            # Stale-while-revalidate (GET only; see settings.identity_swr_seconds): a page view whose
            # 60 s identity just lapsed, inside the stale window, is served on the identity validated
            # moments ago while GoTrue is asked again in the background. Writes, first-seen tokens and
            # anything past the window validate live, as before.
            stale = None
            if (request.method == "GET" and settings.identity_swr_seconds > 0
                    and not _token_expired(access)):
                stale_hit, stale_user = navcache.peek("us:" + _token_key(access))
                if stale_hit and isinstance(stale_user, dict) and stale_user.get("id"):
                    stale = stale_user
            if stale is not None:
                user = dict(stale)
                mark("identity_stale_served", True)
                _revalidate_in_background(access)
            else:
                # An expired token skips the doomed /user call and goes straight to the refresh below.
                user = None if _token_expired(access) else await get_user(access)
                ttl = _token_ttl(access, cap=60.0)
                if user and ttl:
                    navcache.put("u:" + _token_key(access), ttl, user)
                    _remember_stale(access, user)

    if not user:
        rt = request.cookies.get(RT_COOKIE)
        session = await refresh_session(rt) if rt else None
        if not session:
            raise AuthRedirect()
        access = session["access_token"]
        # GoTrue's refresh response already carries the user; asking /user again for the token it
        # just minted is a second round trip for the same answer. Fall back to /user if absent.
        user = session.get("user") if isinstance(session.get("user"), dict) and session["user"].get("id") else None
        if not user:
            user = await get_user(access)
        if not user:
            raise AuthRedirect()
        ttl = _token_ttl(access, cap=60.0)
        if ttl:
            navcache.put("u:" + _token_key(access), ttl, dict(user))
            _remember_stale(access, user)
        # Re-set cookies on this response, and stash for the middleware so they
        # survive handlers that return their own Redirect/Template response.
        set_session_cookies(response, session)
        request.state.refreshed_session = session

    # user → tenant is effectively static (memberships change by operator action);
    # never cache a miss so a just-created membership is seen immediately.
    with span("tenant_lookup"):
        tenant_id = await run_in_threadpool(navcache.get_or_fill,
            "ut:" + user["id"], 300.0,
            lambda: db.get_tenant_for_user(user["id"]),  # service key — runs before user-client context
            cache_if=bool)
    if not tenant_id:
        raise AuthRedirect()
    # Journey heartbeat (operator console): "this tenant is here right now".
    # Throttled + best-effort inside touch(); imported late to keep auth free of
    # a module cycle (journey → db, never back into auth).
    from app import journey
    with span("journey"):
        await run_in_threadpool(journey.touch, tenant_id, user["id"])
    # One JWT-bound client per token, reused across its requests: same auth, so
    # no cross-user bleed (the thing the db_user docstring guards against), and
    # its warm connection pool spares every page a fresh TLS handshake.
    #
    # Held for as long as the token itself is valid, not an arbitrary slice of
    # it. The cap used to be 10 minutes, so a session that stayed open kept
    # throwing its warm connection away and dialling Supabase again — ~400ms on
    # the next page load, several times an hour, for no gain in safety: the
    # window is still clamped to the token's own exp (minus the 30s guard) by
    # _token_ttl, and PostgREST re-validates the JWT on every request anyway, so
    # a longer cache can never outlive the credential it is bound to.
    # Templates read this for per-tenant browser-storage scoping (_concierge.html
    # → data-scope): two tenants signed in from the same browser must not inherit
    # each other's widget state. Set on the request, never rendered as an id the
    # page acts on.
    request.state.tenant_id = tenant_id
    # The signed-in address, for pages that must say WHICH account this is.
    # The stays business sign-up (2026-09-10) connected a personal Hotmail because
    # nothing on the mailbox step said "you signed in as X — that was your
    # login; now pick the inbox customers email". Display only, never an
    # identity the page acts on.
    request.state.user_email = (user.get("email") or "").strip().lower()
    # Who is driving this request (src/app/actor.py). Set HERE, once, because this
    # is the only place that knows — and because "a person did it" is exactly
    # "a signed-in request did it". Nothing downstream has to remember to pass
    # a name, and a nightly job cannot accidentally acquire one.
    actor.remember(tenant_id, user["id"], request.state.user_email)
    ckey = "c:" + _token_key(access)
    hit, client = navcache.peek(ckey)
    if not hit:
        with span("user_client"):
            client = db.db_user(access)
        ttl = _token_ttl(access, cap=3600.0)
        if ttl:
            navcache.put(ckey, ttl, client)
    # View as (src/app/viewas.py; Kris 2026-09-24): an owner looking at the app as
    # one of their people. The whole request then answers as that person —
    # read-only: a write is refused here, before any handler runs.
    with span("view_as"):
        seen_as = await run_in_threadpool(_viewing_as, request, tenant_id, user["id"])
    if seen_as:
        return TenantCtx(tenant_id=tenant_id, client=client, user_id=seen_as,
                         viewing_as=user["id"])
    return TenantCtx(tenant_id=tenant_id, client=client, user_id=user["id"])


def _viewing_as(request: Request, tenant_id: str, real_id: str) -> str | None:
    """The person this OWNER is viewing the app as, or None. Every check is
    here: the cookie is signed, fresh, made by this user for this tenant; the
    user is this tenant's owner; the person is an active member of it."""
    got = viewas.read(request.cookies.get(viewas.COOKIE))
    if not got:
        return None
    cookie_real, target_id, cookie_tenant = got
    if cookie_real != real_id or cookie_tenant != tenant_id or target_id == real_id:
        return None
    from app import people

    def _load():
        real = db.get_member(tenant_id, real_id)
        target = db.get_member(tenant_id, target_id)
        return {"real": real, "target": target}
    pair = navcache.get_or_fill("va:" + tenant_id + ":" + real_id + ":" + target_id, 60.0, _load)
    real, target = pair.get("real"), pair.get("target")
    # No row is the account's own original owner (people.can_open reads it so).
    if real and people.normalise_role(real.get("role")) != "owner":
        return None
    if not target or target.get("active") is False:
        return None
    name = target.get("display_name") or target.get("email") or "them"
    if not viewas.may_write(request.method, request.url.path):
        raise HTTPException(
            status_code=403,
            detail=f"You're viewing BLAM8 as {name} — nothing can be changed. Leave View as first.")
    request.state.viewing_as = {"name": name, "role": people.normalise_role(target.get("role"))}
    return target_id


class OnboardingRedirect(Exception):
    """A logged-in tenant that hasn't finished onboarding → send them to the wizard."""


@instrument("onboarding")
async def require_onboarded(ctx: TenantCtx = Depends(require_tenant)) -> TenantCtx:
    """Dependency for /app routes: a valid tenant that has completed onboarding.
    Un-onboarded tenants are bounced to the wizard by the exception handler.
    Only True is cached (onboarding never un-happens), so completing the wizard
    shows up on the very next request with no invalidation needed."""
    if not await run_in_threadpool(navcache.get_or_fill,
            "t:" + ctx.tenant_id + ":onboarded", 600.0,
            lambda: db.is_onboarded(ctx.tenant_id, client=ctx.client),
            cache_if=bool):
        raise OnboardingRedirect()
    return ctx


@instrument("entitlement")
def has_concierge(tenant_id: str, client=None) -> bool:
    """Is the business Concierge part of this tenant's plan? Pro + Concierge
    (£249) only (Kris 2026-09-25: "No person icon / concierge access for lower
    tiers at all"). The 'concierge' switchboard module, active or preview;
    cached with the tenant's nav chrome, and set_module_active clears it."""
    return bool(navcache.get_or_fill(
        "t:" + tenant_id + ":concierge", 60.0,
        lambda: "concierge" in db.visible_modules(tenant_id, client=client)))


async def require_concierge(ctx: TenantCtx = Depends(require_onboarded)) -> TenantCtx:
    """Dependency for every business-Concierge route (chat, cards, voice).
    App help, onboarding and the website assistant never use it."""
    return await run_in_threadpool(check_concierge_access, ctx)


@instrument("access")
def check_concierge_access(ctx: TenantCtx) -> TenantCtx:
    """Synchronous access read, separable for async transport offloading."""
    if not has_concierge(ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=403,
                            detail="The Concierge is part of Pro + Concierge.")
    return ctx


class SubscriptionRequired(Exception):
    """A signed-in onboarded tenant whose access has lapsed (no active/trialing
    subscription AND no live free trial). Bounces them to /billing/plans via the
    exception handler in main.py.

    Defined here (not in src/app/billing.py) so dashboard routes can depend on
    require_subscription without importing the Stripe SDK at app start."""


class TenantGone(Exception):
    """The session resolves to a tenant that no longer exists (or is no longer
    visible to this user). Nothing is wrong with their BILLING — there is
    nothing left to bill — so the honest answer is to end the session and let
    them sign in again, never a paywall. Handled in main.py: cookies cleared,
    back to /login."""


@instrument("subscription_worker")
def _subscription_tenant(ctx):
    # The gate is ON in production, so this used to be a full tenant-row read on EVERY page, fragment
    # and poll (80-165 ms each, measured 14 Oct). Cached per tenant for 10 s: the decision is still
    # recomputed on each request from the cached row (the trial clock keeps running), only the read is
    # shared. Writers that change what the gate answers drop the key (db.stripe_set_subscription*,
    # set_tenant_comp, set_tenant_plan); anything else (trial usage counters) is bounded by the TTL.
    # A missing tenant is never cached, so a ghost session stays a live question (TenantGone).
    return navcache.get_or_fill("t:" + ctx.tenant_id + ":subgate", 10.0,
                                lambda: db.get_tenant(ctx.tenant_id, client=ctx.client), cache_if=bool)


@instrument("subscription")
async def require_subscription(ctx: TenantCtx = Depends(require_onboarded)) -> TenantCtx:
    """The access gate, wired onto the /app dashboard router. Behind the
    REQUIRE_SUBSCRIPTION_GATE flag so it can be turned on without a redeploy:

      * gate OFF (default, the launch state) → pass through, identical to
        depending on require_onboarded. Nothing is read, nothing blocks.
      * gate ON → grant access when the tenant has an active/trialing Stripe
        subscription (grandfathered launch tenants are 'active'), OR while their
        free trial is still live (src/app/trial.py — 14 days / TRIAL_EMAIL_CAP emails).
        Otherwise raise SubscriptionRequired → /billing/plans.

    Reads via the user's JWT-scoped client so RLS still gates the row read."""
    mark("subscription_gate", settings.require_subscription_gate)
    pool_state(log_values=True)
    if not settings.require_subscription_gate:
        return ctx
    from app import trial
    tenant = await run_in_threadpool(_subscription_tenant, ctx)
    pool_state(log_values=True)
    if not tenant:
        # A deleted tenant used to read as "your pilot ended" and land the owner
        # on the plans page (live 2026-09-04, after a test tenant was reset): a
        # bill for an account that no longer exists. The membership row and the
        # cached user→tenant hop are what keep this session pointing at a ghost —
        # drop the hop and end the session instead.
        if ctx.user_id:      # never a bare "ut:" — that prefix drops every user's hop
            navcache.invalidate("ut:" + ctx.user_id)
        raise TenantGone()
    if tenant.get("subscription_status") in {"active", "trialing"} or trial.is_active(tenant):
        return ctx
    raise SubscriptionRequired()
