"""Security middleware + helpers, wired in src/app/main.py:

  * response security headers + a (report-only-first) Content-Security-Policy
  * CSRF protection (double-submit cookie) on state-changing POSTs
  * a small in-memory, per-IP rate limiter

All three are gated by settings flags so they can be tuned/disabled per env.
"""
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from app.config import settings
from app.startup_timing import span

_WEEK = 60 * 60 * 24 * 7

# ── Security headers + CSP ──────────────────────────────────────────────────
# Tailwind is self-hosted (built at deploy into /static/tw*.css); the Play CDN
# host is gone from every page and from this list (security review 2026-09,
# M10). 'unsafe-inline' stays for the pages' own inline scripts and styles.
CSP = (
    "default-src 'self'; "
    # maps.googleapis.com: the Maps JavaScript API on the fleet route page
    # (route-planning D3) — script loader, tile/sprite images, and its XHRs.
    "script-src 'self' https://maps.googleapis.com 'unsafe-inline'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://maps.googleapis.com https://maps.gstatic.com; "
    "connect-src 'self' https://maps.googleapis.com; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _apply_security_headers(response: Response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # microphone=(self): the Voice CRM debrief recorder (/admin/voice-crm) needs
    # getUserMedia on our own origin; third-party frames stay blocked (and
    # frame-ancestors 'none' means there are none anyway).
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(self), camera=()"
    # Render terminates TLS at its edge; browsers only ever reach it over HTTPS.
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    name = "Content-Security-Policy" if settings.csp_enforce else "Content-Security-Policy-Report-Only"
    from app import speech
    csp = CSP
    if settings.voice_live_enabled and speech.ready():
        origin = speech.websocket_origin() if speech.local() else "wss://api.openai.com"
        csp = csp.replace("connect-src 'self'", "connect-src 'self' " + origin)
        # AudioWorklet capture is generated in the browser; voice samples use blobs.
        csp = csp.replace("script-src 'self'", "script-src 'self' blob:")
        csp += "; media-src 'self' blob:; worker-src 'self' blob:"
    response.headers[name] = csp


# ── CSRF (double-submit cookie) ─────────────────────────────────────────────
CSRF_COOKIE = "bl_csrf"
CSRF_FIELD = "csrf_token"


def csrf_token(request: Request) -> str:
    """Jinja global: the CSRF token to embed in a form. Uses the token minted for
    this request if the cookie wasn't set yet, else the existing cookie value."""
    return getattr(request.state, "csrf_token", "") or request.cookies.get(CSRF_COOKIE, "")


async def require_csrf(request: Request) -> None:
    """Dependency for state-changing POSTs: the form's csrf_token must equal the
    cookie (double-submit). Defense-in-depth on top of SameSite=lax cookies."""
    cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    field = form.get(CSRF_FIELD)
    if not cookie or not field or not hmac.compare_digest(str(cookie), str(field)):
        raise HTTPException(status_code=403, detail="CSRF check failed")


async def require_csrf_header(request: Request) -> None:
    """Double-submit CSRF for JSON endpoints (fetch, not a form): the
    X-CSRF-Token header must equal the cookie. Same guarantee as require_csrf —
    a cross-site page can't read the cookie to forge the header."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(str(cookie), str(header)):
        raise HTTPException(status_code=403, detail="CSRF check failed")


# ── Consent callbacks bound to the browser that started them (M8) ───────────
# A consent URL is a capability: without this, a business could send ANOTHER
# person its own consent link, and if they consented, their mailbox (or Stripe
# account) was attached to the sender's business. The browser that starts a
# connect carries a short-lived httpOnly cookie holding the state; the callback
# must see the same value. (security review 2026-09, M8)
CONSENT_COOKIE = "bl_consent_state"
CONSENT_TTL_S = 15 * 60


def bind_consent(response, state: str, path: str) -> None:
    response.set_cookie(CONSENT_COOKIE, state, max_age=CONSENT_TTL_S, httponly=True,
                        secure=True, samesite="lax", path=path)


def consent_is_this_browsers(request: Request, state: str | None) -> bool:
    got = request.cookies.get(CONSENT_COOKIE) or ""
    return bool(state and got and hmac.compare_digest(str(got), str(state)))


# ── Rate limiting (in-memory, per-IP sliding window) ────────────────────────
_BUCKETS: dict[tuple[str, str], list[float]] = {}


def _client_ip(request: Request) -> str:
    """The visitor's IP for rate limits. blam8.com sits behind Cloudflare, which
    sets CF-Connecting-IP itself (a client-sent one is overwritten); the FIRST
    X-Forwarded-For hop is whatever the client wrote, so keying on it let anyone
    bypass every per-IP limit by sending a new value each time (security review
    2026-09, M2). Fallback: the LAST forwarded hop (added by the nearest proxy),
    then the socket peer."""
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def note_hit(route_key: str, request: Request, window_s: int) -> int:
    """Record one event for this IP under route_key and return how many it has
    in the window — for limits that count only SOME requests (failed lookups)."""
    now = time.monotonic()
    key = (route_key, _client_ip(request))
    hits = [t for t in _BUCKETS.get(key, []) if now - t < window_s]
    hits.append(now)
    _BUCKETS[key] = hits
    return len(hits)


def count_hits(route_key: str, request: Request, window_s: int) -> int:
    now = time.monotonic()
    return len([t for t in _BUCKETS.get((route_key, _client_ip(request)), []) if now - t < window_s])


def rate_limit(route_key: str, limit: int, window_s: int):
    """Dependency factory: allow `limit` requests per `window_s` per client IP."""
    def dep(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        now = time.monotonic()
        key = (route_key, _client_ip(request))
        hits = [t for t in _BUCKETS.get(key, []) if now - t < window_s]
        if len(hits) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
        hits.append(now)
        _BUCKETS[key] = hits
    return dep


# ── Wiring ──────────────────────────────────────────────────────────────────
def install_security(app) -> None:
    """Register the headers/CSP + CSRF-cookie middleware on the FastAPI app."""

    @app.middleware("http")
    async def _security_mw(request: Request, call_next):
        # Mint a CSRF token up front (before the handler renders any form) if the
        # client doesn't have one yet; remember to set the cookie on the way out.
        existing = request.cookies.get(CSRF_COOKIE)
        if not existing:
            request.state.csrf_token = secrets.token_urlsafe(32)

        response = await call_next(request)

        # Authenticated surfaces (and the login form) must never come from a
        # browser cache: a cached page shows an old layout and embeds a stale
        # CSRF token, so its forms 403 — experienced as "the button does
        # nothing", especially on mobile where tab-restore/back-forward caching
        # is aggressive. Static assets keep their own caching (asset_rev busts
        # them per deploy); public pages stay cacheable.
        _p = request.url.path
        if _p.startswith(("/app", "/onboarding", "/admin", "/billing", "/oauth")) or _p == "/login":
            response.headers.setdefault("Cache-Control", "no-store")

        if settings.security_headers_enabled:
            with span("security_headers"):
                _apply_security_headers(response)
        minted = getattr(request.state, "csrf_token", "")
        if minted and not existing:
            response.set_cookie(
                CSRF_COOKIE, minted, secure=True, samesite="lax", path="/", max_age=_WEEK,
            )
        return response
