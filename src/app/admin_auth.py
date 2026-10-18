"""Operator (platform-admin) auth — a SEPARATE login + session from tenant auth.

Same Supabase Auth backend, but its own cookies (bl_admin_*) and its own
authorisation gate: membership in `platform_admins`. Admin routes use the SERVICE
ROLE for cross-tenant reads, so this gate is the only wall between a request and
every tenant's data — it is checked on every request and must stay strict.
"""
import logging
from dataclasses import dataclass

from fastapi import Request, Response

from app import auth, db

log = logging.getLogger(__name__)

ADMIN_AT_COOKIE = "bl_admin_at"
ADMIN_RT_COOKIE = "bl_admin_rt"
_WEEK = 60 * 60 * 24 * 7
ADMIN_SESSION_S = 60 * 60 * 12


class AdminAuthRedirect(Exception):
    """No valid admin session → redirect to /admin/login."""


@dataclass
class AdminCtx:
    user_id: str
    email: str
    role: str


def set_admin_cookies(response, session: dict) -> None:
    common = dict(httponly=True, secure=True, samesite="lax", path="/")
    # Hours, not weeks (security review 2026-09, M5): the console reads every
    # business's data, so a forgotten browser mustn't stay signed in for a month.
    response.set_cookie(ADMIN_AT_COOKIE, session["access_token"], max_age=ADMIN_SESSION_S, **common)
    response.set_cookie(ADMIN_RT_COOKIE, session.get("refresh_token", ""), max_age=ADMIN_SESSION_S, **common)


def clear_admin_cookies(response) -> None:
    response.delete_cookie(ADMIN_AT_COOKIE, path="/")
    response.delete_cookie(ADMIN_RT_COOKIE, path="/")


async def require_admin(request: Request, response: Response) -> AdminCtx:
    """A valid admin session that maps to a platform_admins row. Silently refreshes
    the admin access token from its refresh cookie before giving up. The refreshed
    session is only persisted once admin membership is confirmed."""
    access = request.cookies.get(ADMIN_AT_COOKIE)
    user = await auth.get_user(access) if access else None
    refreshed = None
    if not user:
        rt = request.cookies.get(ADMIN_RT_COOKIE)
        refreshed = await auth.refresh_session(rt) if rt else None
        user = await auth.get_user(refreshed["access_token"]) if refreshed else None
    if not user:
        # Fallback: the TENANT session (the installed PWA has no admin cookies).
        # Same authentication (a live Supabase access token) and the same
        # authorisation wall (the platform_admins check below) — only the cookie
        # name differs, so this opens /admin for the operator's own signed-in
        # app account and nobody else. The tenant's tokens are never copied into
        # the admin cookies: its refresh token is single-use and belongs to the
        # /app session machinery.
        refreshed = None
        tenant_access = request.cookies.get(auth.AT_COOKIE)
        user = await auth.get_user(tenant_access) if tenant_access else None
    if not user:
        raise AdminAuthRedirect()

    admin = db.is_platform_admin(user["id"])   # service-role authorisation gate
    if not admin:
        raise AdminAuthRedirect()

    if refreshed:                              # persist only for a confirmed admin
        set_admin_cookies(response, refreshed)
        request.state.refreshed_admin_session = refreshed
    ctx = AdminCtx(user_id=user["id"], email=admin.get("email") or user.get("email"), role=admin.get("role") or "admin")
    _audit(request, ctx)
    return ctx


def _audit(request: Request, ctx: AdminCtx) -> None:
    """Every operator change, and every view that opens stored email bodies,
    leaves a row in admin_audit (security review 2026-09, M16/M17: "production
    access is audited" had nothing behind it). Plain page views aren't logged.
    Best-effort: a failed write is logged loudly but never blocks the console."""
    method = getattr(request, "method", "GET")
    body_view = bool((getattr(request, "query_params", None) or {}).get("body"))
    if method in ("GET", "HEAD", "OPTIONS") and not body_view:
        return
    params = dict(getattr(request, "path_params", None) or {})
    try:
        db.db().table("admin_audit").insert({
            "admin_user_id": ctx.user_id, "admin_email": ctx.email,
            "method": method, "path": request.url.path[:300],
            "tenant_id": params.get("tenant_id"),
            "detail": {"params": {k: str(v)[:80] for k, v in params.items()},
                       "body_view": body_view},
        }).execute()
    except Exception:  # noqa: BLE001
        log.error("admin audit write FAILED: %s %s by %s", request.method, request.url.path,
                  ctx.email, exc_info=True)
