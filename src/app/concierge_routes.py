"""Concierge HTTP surface (docs/modules/module-concierge.md).

v1: the pre-auth Q&A endpoint. It is UNAUTHENTICATED and calls a paid model, so
it is a token-burn/abuse surface — D10 hardening is non-negotiable and lives here:
  * two stacked per-IP rate limits (in-memory burst + daily ceilings),
  * a body-size cap before JSON parsing, plus question/history/turn caps,
  * bounded model output,
  * a graceful fallback so a model/credential error never 500s a prospect.

The per-IP limits reset with the process and are not shared across app instances;
they do not replace a durable site-wide daily spend ceiling.

The transcript lives in the visitor's browser and is replayed on each call. A
separate opaque browser-session cookie tracks the public question allowance;
public LLM calls retain usage and reliability metadata, but not the anonymous
prompt or response content. Authenticated onboarding-driver tools are separate,
behind require_tenant + CSRF (they reuse the existing onboarding POSTs).
"""
import logging
import re
import uuid
import asyncio
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from fastapi.routing import APIRoute

from app import concierge, concierge_actions, concierge_data, concierge_intake, concierge_tasks, concierge_delegation, db, fairuse, llm, support, trace, trial, usage
from app import knowledge as kb
from app import settings_recommend, setup_handoff
from app import debrief_walk
from app.auth import require_concierge, require_onboarded, require_tenant, TenantCtx
from app.config import settings
from app.security import rate_limit, require_csrf_header
from app.setup_card_routes import router as setup_card_router

log = logging.getLogger(__name__)

MAX_PUBLIC_ASK_BODY_BYTES = 512 * 1024


class _BoundedPublicAskRoute(APIRoute):
    """Reject oversized anonymous chat bodies before FastAPI parses the JSON."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.path != "/concierge/ask" or "POST" not in self.methods:
            return handler

        async def bounded_handler(request: Request):
            declared = request.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > MAX_PUBLIC_ASK_BODY_BYTES:
                        raise HTTPException(status_code=413, detail="Request body is too large.")
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid Content-Length.")

            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_PUBLIC_ASK_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Request body is too large.")
                chunks.append(chunk)
            # Starlette's Request.body() uses this cache when the normal FastAPI
            # handler parses the request, so the bounded stream is not read twice.
            request._body = b"".join(chunks)
            return await handler(request)

        return bounded_handler


router = APIRouter(prefix="/concierge", tags=["concierge"], route_class=_BoundedPublicAskRoute)
router.include_router(setup_card_router)


# Anonymous visitor allowance. The opaque, HttpOnly session cookie keeps the
# count out of editable browser storage; the in-memory record expires after two
# days without activity. Existing per-IP burst/daily limits remain independent
# abuse backstops. As with those limits, this state is per worker process.
PUBLIC_ASK_SESSION_COOKIE = "bl_public_ask_session"
PUBLIC_ASK_SESSION_TTL_S = 2 * 24 * 60 * 60
PUBLIC_ASK_SESSION_LIMIT = 20
_public_ask_sessions: dict[str, dict[str, float | int]] = {}
_public_ask_locks: dict[str, asyncio.Lock] = {}


def _public_ask_session(request: Request, response: Response, *, create: bool = False) -> str:
    import re

    sid = request.cookies.get(PUBLIC_ASK_SESSION_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", sid):
        sid = secrets.token_urlsafe(32)
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        response.set_cookie(PUBLIC_ASK_SESSION_COOKIE, sid, httponly=True,
                            secure=request.url.scheme == "https" or forwarded_proto == "https",
                            samesite="lax", path="/")
    now = time.monotonic()
    # Bound stale anonymous state without retaining visitor identifiers forever.
    for old_sid, state in list(_public_ask_sessions.items()):
        if now - float(state.get("touched", 0)) > PUBLIC_ASK_SESSION_TTL_S:
            _public_ask_sessions.pop(old_sid, None)
            _public_ask_locks.pop(old_sid, None)
    state = _public_ask_sessions.get(sid)
    if state is None and create:
        state = {"used": 0, "touched": now}
        _public_ask_sessions[sid] = state
    if state is not None:
        state["touched"] = now
    return sid


def _public_ask_remaining(sid: str) -> int:
    used = int(_public_ask_sessions.get(sid, {}).get("used", 0))
    return max(0, PUBLIC_ASK_SESSION_LIMIT - used)


@router.get("/ask-limit")
async def ask_limit(request: Request, response: Response,
                    _guard: None = Depends(rate_limit("concierge_ask_status", limit=60,
                                                      window_s=60))) -> dict:
    """Return the public browser-session allowance and establish its cookie."""
    sid = _public_ask_session(request, response)
    return {"limit": PUBLIC_ASK_SESSION_LIMIT, "remaining": _public_ask_remaining(sid)}


def _tenant_context(ctx: TenantCtx) -> dict | None:
    """The signed-in tenant's profile for the help assistant: name, industry (a known id or
    their own words), business brief, active modules, the diary config (for the setup
    status), and a live SETUP snapshot (mailboxes, knowledge, sending mode) so 'what do I
    do next?' is answered from what's actually done, not generically. Best-effort — a read
    failure never breaks the help endpoint, the assistant just answers less personally."""
    try:
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
        tcfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        modules = db.active_modules(ctx.tenant_id, client=ctx.client)
        # Always fetched (not just for diary modules): the booking checklist
        # (intake_fields) rides in this row and matters on every tier — the
        # assistant grounds booking-details recommendations in it.
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
        # CRM shape (best-effort, read-only — never the lazy-seeding helper here)
        # so stage recommendations start from what already exists.
        crm_state = None
        if "crm" in modules:
            try:
                stages = [{"name": s.get("name"), "stage_type": s.get("stage_type")}
                          for s in db.crm_list_stages(ctx.tenant_id, client=ctx.client)]
                crm_state = {
                    "stages": stages,
                    "quiet_days": int(tcfg.get("crm_quiet_days") or 0) or None,
                    # The stages are lazily SEEDED on first CRM page view and
                    # quiet_days defaults to 14 on every row — flag when it's
                    # all still the untouched default so the assistant offers a
                    # tailored setup instead of congratulating (e2e 2026-08-02).
                    "default_seed": not setup_handoff.crm_configured(
                        stages, tcfg.get("crm_quiet_days")),
                }
            except Exception:
                crm_state = None
        # Snapshot reads degrade one line at a time (None = unknown, omitted from
        # the prompt) — a hiccup here never costs the whole profile.
        try:
            mailboxes = [{"provider": m.get("provider"), "address": m.get("address"),
                          "label": m.get("label")}
                         for m in db.list_mailboxes(ctx.tenant_id, client=ctx.client)]
        except Exception:
            mailboxes = None
        try:
            sources = sorted({s["source"] for s in
                              db.list_knowledge_sources(ctx.tenant_id, client=ctx.client)})
        except Exception:
            sources = None
        try:
            from app import knowledge_coverage
            _texts = db.knowledge_source_texts(ctx.tenant_id, client=ctx.client)
            _cov = knowledge_coverage.coverage(_texts)
            kcov = {"score": _cov["score"], "missing": [k["label"] for k in _cov["missing"]]}
            # The notes' own words, not just their titles: asked what payment
            # terms to recommend, the assistant said "your notes mention payment
            # but I can't see the exact terms" (run #9, 2026-09-06) — the owner
            # had written "within 7 days" and a 50% deposit rule that morning.
            knowledge_texts = [{"source": t["source"], "text": (t.get("text") or "")[:400]}
                               for t in _texts[:12]]
        except Exception:
            kcov = None
            knowledge_texts = None
        # The walk-through's position (same reads as /setup-status), so the
        # assistant never offers a step that is already done (run #9).
        try:
            mods = set(modules or [])
            invoice_row = db.get_invoice_settings(ctx.tenant_id, client=ctx.client)
            groups = (db.list_requirement_groups(ctx.tenant_id, client=ctx.client)
                      if setup_handoff.DIARY_MODULES & mods else [])
            stage_rows = (db.crm_list_stages(ctx.tenant_id, client=ctx.client)
                          if "crm" in mods else [])
            handoff = setup_handoff.steps(mods, cfg, stage_rows, tcfg.get("crm_quiet_days"),
                                          invoice_row, dependant_groups=groups)
        except Exception:
            handoff = None
        return {
            "name": tenant.get("name") or tenant.get("company"),
            "industry": tenant.get("industry"),
            "brief": (tcfg.get("business_brief") or "").strip() or None,
            "modules": sorted(modules) if modules else [],
            "cfg": cfg,
            "crm": crm_state,
            "handoff": handoff,
            # Held for later — the same row, no extra read.
            "held": [i for i in (tcfg.get("setup_held") or []) if isinstance(i, dict)],
            "setup": {
                "mailboxes": mailboxes,
                "knowledge_sources": sources,
                "knowledge_coverage": kcov,
                "knowledge_texts": knowledge_texts,
                "auto_send": bool(tcfg.get("auto_send")),
                "draft_hold": bool(tcfg.get("draft_hold")),
                # Only meaningful when a diary-backed module is active; None hides it.
                "diary_configured": bool(cfg) if {"scheduling", "tms", "fsm"} & set(modules) else None,
            },
        }
    except Exception:
        return None

# Input caps (D10). Generous enough for a real question + a short transcript,
# tight enough that one call can't ship a novel to the model.
MAX_QUESTION_CHARS = 1000            # the anonymous public /ask and the voice chat
MAX_INAPP_QUESTION_CHARS = 4000      # the in-app assistant box (_concierge.html maxlength):
                                     # the knowledge interview asks three questions a turn
MAX_HISTORY_TURNS = 16

# A bare "yes" typed while a card is waiting (Kris 2026-10-16, L2): a typed yes never confirms (only the
# owner's tap on the card does), and it must not start a new turn that stages a second card or says the
# change was made. Same word lists as the panel's own matcher (concierge_panel.js VOICE_AFFIRM_*).
_AFFIRM_VOCAB = frozenset((
    "y yes yep yeah yup aye sure ok okay k confirm confirmed correct right thats that is all both "
    "definitely absolutely go ahead for do it please perfect great lovely sounds good fine now then "
    "thanks thank you send this off one").split())
_AFFIRM_CORE = frozenset(
    "y yes yep yeah yup aye sure confirm confirmed correct definitely absolutely ahead do please okay ok send".split())
BARE_YES_REPLY = ("It's ready on your screen: tap the thumbs-up on the card to confirm it, or the bin to cancel. "
                  "A yes typed here can't confirm it.")


async def run_in_threadpool_safe(fn):
    """A read that must never break the turn: any failure answers None."""
    try:
        return await run_in_threadpool(fn)
    except Exception:
        return None


def _is_bare_affirmation(text: str) -> bool:
    import re
    words = [w for w in re.sub(r"[^a-z'\s]", " ", (text or "").lower()).replace("'", "").split() if w]
    return bool(words) and len(words) <= 6 and all(w in _AFFIRM_VOCAB for w in words) \
        and any(w in _AFFIRM_CORE for w in words)


def _claims_done(reply: str) -> bool:
    """A reply written alongside a staged card that talks as if the change were made ("Note added to
    Leah's record"). Nothing has run: only the owner's tap runs a card."""
    import re
    head = (reply or "")[:240].lower()
    if re.search(r"\b(?:ready|tap|confirm|review|waiting|prepared|draft|proposed|would|shall|want me)\b", head):
        return False
    return bool(re.search(r"\b(?:added|saved|done|sent|created|updated|marked|booked|blocked|deleted|removed|"
                          r"cancelled|moved|recorded|logged|logged|changed|set up)\b", head))
MAX_TURN_CHARS = 4000


class Turn(BaseModel):
    role: str = Field(..., max_length=16)
    content: str = Field(..., max_length=MAX_TURN_CHARS)
    # Business concierge only: the lookups behind an assistant turn (trail
    # labels), folded into its text by sanitize_history.
    lookups: list[str] | None = Field(default=None, max_length=12)


class AskRequest(BaseModel):
    model_config = {"extra": "forbid"}

    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


class AskResponse(BaseModel):
    reply: str
    offer_signup: bool
    suggested_industry: str
    questions_limit: int | None = None
    questions_remaining: int | None = None
    # A completed support-ticket draft awaiting the visitor's approval — the
    # widget renders it as a review card whose button POSTs /concierge/ticket.
    ticket: dict | None = None
    # A resolved status check (app/support.lookup_ticket shape) — SERVER truth
    # attached by the route when the model requested a lookup; the widget
    # renders it as a card, so a status can never be invented.
    ticket_status: dict | None = None


# Friendly degrade when the model/credentials are unavailable — never a 500 in a
# prospect's chat window.
_FALLBACK = ("Sorry — I'm having trouble answering right now. You can reach the "
             "team at hello@blam8.com and someone will be straight back to you.")


@router.post(
    "/ask",
    response_model=AskResponse,
    dependencies=[
        # Burst guard: a fast typist's cadence, not a script's.
        Depends(rate_limit("concierge_ask_burst", limit=12, window_s=60)),
        # Daily ceiling per IP: caps the cost one source can incur in a day.
        Depends(rate_limit("concierge_ask_daily", limit=150, window_s=86400)),
    ],
)
async def ask(body: AskRequest, request: Request, response: Response) -> AskResponse:
    """Answer one pre-auth question; transcriptless, allowance-limited and bounded."""
    sid = _public_ask_session(request, response, create=True)
    lock = _public_ask_locks.setdefault(sid, asyncio.Lock())
    question = body.question.strip()[:MAX_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)
    # Hold one session lock through the paid model call so parallel tabs cannot
    # spend past the displayed allowance. Failed model calls do not consume a
    # question; successful answers do.
    async with lock:
        if _public_ask_remaining(sid) <= 0:
            raise HTTPException(
                status_code=429,
                detail={"code": "question_allowance_exhausted",
                        "limit": PUBLIC_ASK_SESSION_LIMIT, "remaining": 0,
                        "message": "You've used all 20 questions in this browser session."},
            )
        try:
            result = await concierge.answer(question, history)
        except Exception:
            # Model/credential/transport error — keep the visitor moving, don't 500.
            remaining = _public_ask_remaining(sid)
            response.headers["X-Questions-Limit"] = str(PUBLIC_ASK_SESSION_LIMIT)
            response.headers["X-Questions-Remaining"] = str(remaining)
            return AskResponse(reply=_FALLBACK, offer_signup=False, suggested_industry="",
                               questions_limit=PUBLIC_ASK_SESSION_LIMIT,
                               questions_remaining=remaining)
        state = _public_ask_sessions[sid]
        state["used"] = int(state.get("used", 0)) + 1
        remaining = _public_ask_remaining(sid)
    response.headers["X-Questions-Limit"] = str(PUBLIC_ASK_SESSION_LIMIT)
    response.headers["X-Questions-Remaining"] = str(remaining)
    # Meter the spend (best-effort). Pre-auth has no tenant, so it books under the
    # platform sentinel tenant — anonymous chat burn is still visible, not free.
    def _blocking():
        usage.record_anthropic(concierge.PLATFORM_TENANT_ID, result.model, result.usage,
                               operation="concierge_ask", module="concierge")
        # A requested status check is resolved HERE, against the real table — the
        # model only ever hands over {reference, email}; the answer (found / not
        # found / system unreachable) is attached as data for the widget's card.
        ticket_status = support.lookup_ticket(**result.lookup) if result.lookup else None
        return AskResponse(reply=result.reply, offer_signup=result.offer_signup,
                           suggested_industry=result.suggested_industry,
                           ticket=result.ticket, ticket_status=ticket_status,
                           questions_limit=PUBLIC_ASK_SESSION_LIMIT,
                           questions_remaining=remaining)
    return await run_in_threadpool(_blocking)


# ── FAQ scenario box (pre-auth, one bounded answer) ──────────────────────────
# "Run a situation from your business past BLAM8." The narrowest public LLM
# surface: no history, no tools, no tickets, 400 characters in, ~150 words out.
# Layers, cheapest first, each a separate dependency so one failing never spends
# the next one's allowance:
#   1. kill switch (settings.scenario_box_enabled)
#   2. same-origin (a browser always sends Origin on a fetch POST; a page on
#      another site can't use a visitor's browser to spend our budget)
#   3. CSRF double-submit header (the bl_csrf cookie the landing page mints)
#   4. site-wide daily ceiling — counted in memory AND durably (llm_calls rows),
#      so a restart or a botnet of fresh IPs still hits it
#   5. per-IP burst + per-IP daily allowance (Cloudflare's CF-Connecting-IP)
# The reply is data, rendered with textContent by the widget — never HTML.
_SCENARIO_BUSY = "scenario_busy"
_scenario_seen: dict[str, int] = {}          # UTC day -> scenarios started (this process)


def _scenario_gate_enabled() -> None:
    if not settings.scenario_box_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _scenario_same_origin(request: Request) -> None:
    from urllib.parse import urlparse
    origin = (request.headers.get("origin") or "").strip()
    host = (request.headers.get("host") or "").strip().lower()
    if not origin or not host or urlparse(origin).netloc.lower() != host:
        raise HTTPException(status_code=403, detail="Forbidden")


def _scenario_ceiling(request: Request) -> None:
    if not settings.rate_limit_enabled:
        return
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cap = settings.scenario_global_daily
    if _scenario_seen.get(now.strftime("%Y-%m-%d"), 0) >= cap:
        raise HTTPException(status_code=429, detail=_SCENARIO_BUSY)
    try:
        used = db.count_llm_calls_since("scenario_ask", (now - timedelta(days=1)).isoformat())
    except Exception:
        return                                  # storage hiccup: the in-memory cap still holds
    if used >= cap:
        raise HTTPException(status_code=429, detail=_SCENARIO_BUSY)


class ScenarioRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(..., min_length=1, max_length=concierge.SCENARIO_MAX_CHARS)
    website: str = Field("", max_length=200)      # honeypot: a human never sees or fills it


class ScenarioResponse(BaseModel):
    reply: str
    ok: bool


@router.post(
    "/scenario",
    response_model=ScenarioResponse,
    dependencies=[
        Depends(_scenario_gate_enabled),
        Depends(_scenario_same_origin),
        Depends(require_csrf_header),
        Depends(_scenario_ceiling),
        Depends(rate_limit("scenario_burst", limit=1, window_s=20)),
        Depends(rate_limit("scenario_daily", limit=max(1, settings.scenario_per_ip_daily),
                           window_s=86400)),
    ],
)
async def scenario(body: ScenarioRequest, response: Response) -> ScenarioResponse:
    """Answer one scenario. Stateless; never a 500 in the visitor's face."""
    import asyncio
    from datetime import datetime, timezone
    response.headers["Cache-Control"] = "no-store"
    question = concierge.clean_scenario(body.question)
    if body.website.strip() or len(question) < 10:
        return ScenarioResponse(reply=concierge.SCENARIO_OFF_TOPIC, ok=False)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if day not in _scenario_seen:
        _scenario_seen.clear()                  # a new UTC day: yesterday's count is dead weight
    _scenario_seen[day] = _scenario_seen.get(day, 0) + 1
    try:
        reply, on_topic, usage_, model = await asyncio.wait_for(
            concierge.scenario_answer(question), timeout=45)
    except Exception:
        return ScenarioResponse(reply=_FALLBACK, ok=False)
    def _blocking():
        usage.record_anthropic(concierge.PLATFORM_TENANT_ID, model, usage_,
                               operation="scenario_ask", module="concierge")
        if not on_topic or not reply:
            return ScenarioResponse(reply=concierge.SCENARIO_OFF_TOPIC, ok=False)
        return ScenarioResponse(reply=reply[:1500], ok=True)
    return await run_in_threadpool(_blocking)


# ── Support tickets (pre-auth write path behind the ask-mode review card) ────
# The assistant DRAFTS the ticket ({...} in the /ask response); the visitor
# reviews it in the chat and presses "Raise ticket", and THIS endpoint files it —
# the only write an anonymous visitor can make. Defense layers: per-IP rate
# limits here, a per-email hourly cap + duplicate fold-in inside
# support.create_ticket, server-side secret redaction, and length caps below.
# No CSRF: there are no ambient credentials on the pre-auth surface to forge.
class TicketRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    category: str = Field("other", max_length=40)
    summary: str = Field(..., min_length=1, max_length=300)
    description: str = Field("", max_length=4000)
    error_message: str = Field("", max_length=2000)
    account_type: str = Field("", max_length=20)
    page_url: str = Field("", max_length=500)


class TicketResponse(BaseModel):
    ok: bool
    reference: str | None = None
    deduped: bool = False
    redacted: bool = False


@router.post(
    "/ticket",
    response_model=TicketResponse,
    dependencies=[
        # A visitor legitimately files one, maybe two tickets — never five an hour.
        Depends(rate_limit("concierge_ticket", limit=5, window_s=3600)),
        Depends(rate_limit("concierge_ticket_burst", limit=2, window_s=60)),
    ],
)
async def ticket(body: TicketRequest, request: Request) -> TicketResponse:
    """File the approved ticket draft. Anonymous-capable; returns the BLAM8-XXXX
    reference the visitor quotes in follow-ups and status checks."""
    if not support.valid_email(body.email):
        raise HTTPException(status_code=400, detail="That email address doesn't look right.")
    try:
        result = await support.create_ticket(
            email=body.email, category=body.category.strip(),
            summary=body.summary, description=body.description,
            error_message=body.error_message, account_type=body.account_type.strip(),
            page_url=body.page_url,
            user_agent=request.headers.get("user-agent", ""))
    except support.TicketRateLimited:
        raise HTTPException(status_code=429,
                            detail="That email address has raised several tickets in the "
                                   "last hour — please email hello@blam8.com instead.")
    except Exception:
        # Storage down — the widget shows the honest email fallback.
        raise HTTPException(status_code=503, detail="The ticket system is unavailable.")
    return TicketResponse(**result)


# ── Onboarding co-pilot (authenticated, build step 5) ────────────────────────
# Drives the EXISTING wizard: returns field values the frontend mirrors into the
# visible form. Writes nothing — the customer still clicks the real Continue
# button (the existing CSRF-protected onboarding POST is the only write path), so
# this endpoint needs no CSRF. Gated to logged-in tenants (it's an in-app helper)
# and rate-limited like /ask. The onboarding pages already pass require_tenant,
# so the session cookie is present when the widget calls this.
class OnboardRequest(BaseModel):
    step: str = Field(..., min_length=1, max_length=40)
    question: str = Field(..., min_length=1, max_length=MAX_INAPP_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list)
    current: dict[str, str] = Field(default_factory=dict)


class OnboardResponse(BaseModel):
    reply: str
    form_fill: dict
    ready_to_advance: bool
    notes: list[dict] = Field(default_factory=list)
    review_link: dict | None = None
    journey: dict | None = None
    # Labels of the pages this turn held facts for — written ONLY from what
    # the server recorded, so the widget's "Kept for later" line is never a
    # claim the store did not make good on (src/app/setup_held.py).
    held_for: list[str] = Field(default_factory=list)


@router.post(
    "/onboard",
    response_model=OnboardResponse,
    dependencies=[Depends(rate_limit("concierge_onboard", limit=30, window_s=60))],
)
async def onboard(body: OnboardRequest, ctx: TenantCtx = Depends(require_tenant)) -> OnboardResponse:
    """Co-pilot one onboarding step for the signed-in tenant. Returns field
    suggestions only; degrades gracefully."""
    if body.step not in concierge.ONBOARDING_STEPS:
        raise HTTPException(status_code=400, detail="Unknown onboarding step.")
    # Daily ceiling (2026-09-02 cost review): this surface is outside the
    # business-chat fair-use meter and its turns are otherwise unbounded.
    if not fairuse.onboarding_allowed(ctx.tenant_id, "concierge_onboard", client=ctx.client):
        return OnboardResponse(
            reply="That's a lot of setup questions for one day — my onboarding "
                  "allowance resets at midnight (UK time). You can carry on "
                  "filling the steps in directly, and I'll pick back up tomorrow.",
            form_fill={}, ready_to_advance=False)
    question = body.question.strip()[:MAX_INAPP_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)
    from app import onboarding_journey
    saved, connected = {}, None
    try:
        t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
        cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        connected = bool(db.get_connection_status(ctx.tenant_id, client=ctx.client))
        saved = {"interview": bool(t.get("industry")), "mailbox": connected,
                 "voice": bool(cfg.get("voice_profile_source")), "knowledge": bool(t.get("onboarded_at"))}
    except Exception:
        pass  # Unknown completion is never described as false or complete.
    journey = onboarding_journey.describe(body.step, connected=connected, saved=saved)
    if body.step == "knowledge":
        try:
            from app.onboarding import _next_step_target
            journey = onboarding_journey.describe(body.step, connected=connected, saved=saved,
                                                   next_target=_next_step_target(ctx))
        except Exception:
            pass  # The next tuning destination remains unverified, not guessed.
    coverage = None
    if body.step == "knowledge":
        from app import knowledge_coverage, knowledge_review
        try:
            review_context = knowledge_review.load(ctx.tenant_id)
            coverage = knowledge_coverage.coverage([{"source":s["source"],"text":s["content"]} for s in review_context["sources"] if s.get("mailbox_id") is None])
            coverage["review_topics"] = knowledge_review.topic_view(review_context)["topics"]
            if any(r["state"]=="pending" for r in review_context["reviews"]) or re.search(
                    r"\b(correct|rewrite|replacement|existing (notes|cards)|review.only|source.faithful)\b",question,re.I):
                grounded=knowledge_review.grounded_reply(review_context)
                return OnboardResponse(reply=grounded["reply"],form_fill={},ready_to_advance=False,
                                       notes=[],review_link=grounded["link"])
        except HTTPException:
            return OnboardResponse(reply="I couldn't verify your saved knowledge and pending drafts. Please reload before reviewing changes; nothing has been saved.",form_fill={},ready_to_advance=False)
    # The later steps know the business from step 1 (brief, sector) instead of
    # relying on the chat history to carry it (run #9 hand-off). Best-effort.
    tenant = _tenant_context(ctx) if body.step in ("voice", "knowledge", "mailbox") else None
    tenant = dict(tenant or {})
    tenant["onboarding_journey"] = journey
    if journey and body.step != "knowledge" and onboarding_journey.is_navigation_request(question):
        return OnboardResponse(reply=onboarding_journey.navigation_reply(journey), form_fill={},
                               ready_to_advance=False, journey=journey)
    # What earlier turns held (any step): the prompt carries it so nothing is
    # asked twice; what THIS turn holds is recorded after the reply.
    from app import setup_held
    held_before = setup_held.load(ctx.tenant_id, client=ctx.client)
    try:
        result = await concierge.answer_onboarding(body.step, question, history, body.current,
                                                   tenant_id=ctx.tenant_id, coverage=coverage,
                                                   tenant=tenant, held=held_before)
    except Exception:
        return OnboardResponse(
            reply="Sorry — I couldn't help with that just now. You can fill this "
                  "step in directly, and I'll be here if you need me.",
            form_fill={}, ready_to_advance=False)
    def _blocking():
        usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                               operation="concierge_onboard", module="concierge")
        held_for = setup_held.add(ctx.tenant_id, getattr(result, "held", None), step=body.step,
                                  client=ctx.client)
        review_link = None
        if body.step == "knowledge" and (result.notes or getattr(result,"unanswered",None)):
            from app import knowledge_review
            row = knowledge_review.stage(ctx.tenant_id,list(result.notes or []),origin="Assistant interview",
                                         questions=getattr(result,"unanswered",[]))
            review_link = {"label":"Review pending Knowledge", "href":f"/onboarding/knowledge/review/{row['id']}"}
        return OnboardResponse(reply=result.reply, form_fill=result.form_fill,
                               ready_to_advance=result.ready_to_advance,
                               notes=list(result.notes or []), held_for=held_for, review_link=review_link, journey=journey)
    return await run_in_threadpool(_blocking)


# ── Help mode (authenticated in-app assistant) ───────────────────────────────
# Answers how-to / capability questions for a logged-in customer from the platform
# KB + injected facts, and can return a navigation link to the right page. Gated to
# tenants and rate-limited. Durable card mode additionally requires CSRF.
class HelpRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_INAPP_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list)
    # Where the widget is and whether it asked by itself on arrival — the
    # walk's card and held filters key off both (setup_handoff.cards_for).
    path: str | None = Field(default=None, max_length=200)
    auto: bool = False
    card_mode: bool = False


class HelpResponse(BaseModel):
    reply: str
    link: dict | None = None
    submit: dict | None = None    # drafted {kind, summary} awaiting the customer's approval
    # Settings recommendations — a LIST of {page, label, href, values:[{name,value}]},
    # the widget renders one "Use recommended settings" card per entry
    # (src/app/settings_recommend.py).
    recommend: list[dict] | None = None
    # A hand-off step this turn closed ("dependants" when the assistant's
    # verdict was that nothing of the sort applies) — the panel refetches the
    # setup status so the strip moves on in the same breath (run #9).
    closed_step: str | None = None
    # Labels of the pages this turn held facts for (src/app/setup_held.py).
    held_for: list[str] = Field(default_factory=list)
    setup_cards: list[dict] = Field(default_factory=list)
    card_errors: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)


class SettingsRowsRequest(BaseModel):
    page: str = Field(..., max_length=40)
    values: list[dict] = Field(..., max_length=settings_recommend.MAX_ENTRIES)


@router.post("/settings-rows", dependencies=[
    Depends(rate_limit("concierge_settings_rows", limit=30, window_s=60)),
])
def settings_rows(body: SettingsRowsRequest, ctx: TenantCtx = Depends(require_tenant)):
    """Read-only validation for a card, including older held/browser rows.

    Never trust browser-supplied parsed fields or verification flags. Rebuild
    them from the raw row and this authenticated tenant's current settings.
    """
    from app import setup_rows, intake
    if body.page not in settings_recommend.PAGES:
        raise HTTPException(status_code=400, detail="Unknown settings page.")
    try:
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        groups = (db.list_requirement_groups(ctx.tenant_id, client=ctx.client, strict=True)
                  if body.page == "dependants" else [])
    except Exception:
        raise HTTPException(status_code=503, detail="Your saved settings could not be checked. Nothing was filled in.") from None
    saved = {"service": [s["name"] for s in (cfg.get("service_types") or []) if s.get("name")],
             "resource": [r["name"] for r in (cfg.get("resource_types") or []) if r.get("name")],
             "group": [g["label"] for g in groups if g.get("label")],
             "intake": [f["label"] for f in (cfg.get("intake_fields") or []) if f.get("label")],
             "group_fields": {g["label"]: [f["label"] for f in (g.get("fields") or []) if f.get("label")]
                              for g in groups if g.get("label")}}
    if intake.stay_mode(cfg):
        saved["service"].append("Stays")
    raw = [{"name": v.get("name"), "value": v.get("value")} for v in body.values]
    values = setup_rows.recommendations(body.page, raw)
    values = setup_rows.prepare(body.page, values, saved=saved)
    if not intake.stay_mode(cfg):
        for row in values:
            if row.get("name") == "service" and "at_once" in (row.get("fields") or {}):
                row["problems"].append("At once belongs to appointments on a stay diary.")
    return {"values": values, "ok": bool(values) and not any(v.get("problems") for v in values)}


@router.post(
    "/help",
    response_model=HelpResponse,
    dependencies=[Depends(rate_limit("concierge_help", limit=20, window_s=60))],
)
async def help(body: HelpRequest, request: Request, ctx: TenantCtx = Depends(require_tenant)) -> HelpResponse:
    """In-app help for the signed-in tenant. Personalised with the tenant's profile + a
    settings recipe for their business type (so 'help me set up the diary' is tailored).
    Degrades gracefully."""
    question = body.question.strip()[:MAX_INAPP_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)
    from app.help_investigation import Investigator
    investigator = await run_in_threadpool(Investigator, ctx)
    # The legacy setup snapshot contains private configuration and Knowledge.
    # Resolve access before loading it, rather than trusting a model to hide it.
    tenant = (await run_in_threadpool(_tenant_context, ctx)) if investigator.can_configure else {}
    card_snapshot, card_enabled = {}, False
    if body.card_mode:
        if not investigator.can_configure:
            raise HTTPException(403, "Settings changes require a settings manager outside View as.")
        await require_csrf_header(request)
        from app import setup_card_model, setup_card_store, setup_cards
        try:
            card_snapshot = setup_card_store.load_all(ctx.tenant_id, client=ctx.client)
            saved_cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
            card_enabled = True
            tenant = {**(tenant or {}), "cfg": saved_cfg}
            if card_enabled:
                tenant["setup_card_context"] = setup_card_model.context(card_snapshot, body.path, saved_cfg)
        except Exception:
            raise HTTPException(503, "Saved proposals could not be checked. Reload before trying again.") from None
    try:
        tenant = tenant or {}
        try:
            tenant['_product_context'] = await run_in_threadpool(investigator.product_context)
        except Exception:
            tenant['_product_context'] = None  # Unknown availability never becomes permission.
        result = await concierge.answer_help(question, history, tenant=tenant,
                                             tenant_id=ctx.tenant_id, investigator=investigator)
    except Exception:
        return HelpResponse(
            reply="Sorry — I couldn't answer that just now. You can reach the team "
                  "at hello@blam8.com and someone will help.",
            link=None)
    def _blocking():
        usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                               operation="concierge_help", module="concierge")
        card_errors, updated_cards = [], []
        reply = result.reply
        recs = result.recommend if investigator.can_configure else None
        held = getattr(result, "held", None) if investigator.can_configure else None
        if card_enabled:
            from uuid import uuid4
            recs, rejected = setup_card_model.without_legacy_rows(recs)
            held, rejected_held = setup_card_model.without_legacy_rows(held)
            if rejected or rejected_held:
                card_errors.append("Some row suggestions lacked sourced changes and were not kept. Please ask for those rows again.")
            try:
                changes = setup_card_model.unpack(getattr(result, "setup_changes", None))
                if changes:
                    allowed_changes = setup_handoff.cards_for(changes, setup_handoff.load_steps(ctx.tenant_id, ctx.client), body.path, body.auto)
                    if len(allowed_changes) != len(changes):
                        raise setup_cards.Invalid("A proposal targets a completed or different setup step.")
                # Validate every page before the first write. Each page then has its
                # own compare-and-swap; partial storage success is reported exactly.
                prepared = [(entry, setup_cards.change(entry["page"], card_snapshot.get(entry["page"]),
                             entry["revision"], str(uuid4()), entry["changes"], entry.get("considered"))) for entry in changes]
                for entry, card in prepared:
                    try:
                        setup_card_store.save(ctx.tenant_id, entry["page"], entry["revision"], card, client=ctx.client)
                        card_snapshot[entry["page"]] = card
                        updated_cards.append(card)
                    except setup_cards.Stale:
                        card_errors.append("The " + entry["page"] + " proposal changed elsewhere. Reload it before asking again.")
                    except Exception:
                        card_errors.append("The " + entry["page"] + " proposal could not be confirmed. Reload to check its status.")
            except (setup_cards.Invalid, TypeError, ValueError):
                card_errors.append("The proposed changes were incomplete or invalid. Existing cards were kept; please ask again.")
            except Exception:
                card_errors.append("The setup steps could not be checked. Existing cards were kept; please try again.")
            if updated_cards or card_errors:
                labels = [settings_recommend.PAGES[c["page"]]["label"] for c in updated_cards]
                reply = (("Updated setup records: " + ", ".join(labels) + ". Review proposals in Suggested or Held and considered notes in the page record. ") if labels else "")
                reply += "Nothing from this reply has been applied to your settings."
                if card_errors:
                    reply += " " + " ".join(card_errors)
        # The assistant answered the Dependants question with NO kind (a ladder
        # on the customer, customer fields, or nothing at all): that IS the
        # answer, so the hand-off's step closes without a save (run #8, Kris).
        # Run #9 (2026-09-05): the verdict most often arrives as PROSE with no
        # card at all ("this step doesn't apply to Brightwell") — the structured
        # `no_dependants` flag carries it, and the response names the closed step
        # so the panel can move the strip on without a reload.
        closed_step = None
        try:
            cards = [rec for rec in (result.recommend or []) if rec.get("page") == "dependants"]
            proposes_kind = any((v.get("name") == "group")
                                for rec in cards for v in (rec.get("values") or []))
            if card_enabled:
                proposes_kind |= any(v["name"] == "group" and v["status"] in setup_cards.PENDING
                                     for v in (card_snapshot.get("dependants") or {}).get("lines", []))
            # A card with no kind, or the prose verdict — unless a kind is on the
            # table, in which case the owner still saves it.
            no_kind = not proposes_kind and not card_errors and (bool(cards) or bool(getattr(result, "no_dependants", False)))
            if no_kind and investigator.can_configure:
                db.mark_dependants_configured(ctx.tenant_id, client=ctx.client)
                closed_step = "dependants"
        except Exception:
            log.warning("dependants stamp from the card failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        from app import setup_held
        held_for = (setup_held.add(ctx.tenant_id, held, step="help",
                                  client=ctx.client, on_pages=setup_held.pages_for_path(body.path))
                    if investigator.can_configure else [])
        # During the walk a reply may not re-offer a finished page (re-run
        # 2026-09-13: the Invoice step's arrival ask carried five cards for
        # pages already done). Only read the steps when there is a card to judge.
        if recs:
            try:
                recs = setup_handoff.cards_for(recs, setup_handoff.load_steps(ctx.tenant_id, ctx.client),
                                               body.path, body.auto) or None
            except Exception:
                log.warning("card filter failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        # While the setup walk is unfinished, a reply never offers a door to
        # ANOTHER step's page (run #10 (ccc)).
        link = result.link
        if link and investigator.can_configure:
            try:
                if not setup_handoff.nav_allowed(link, setup_handoff.load_steps(ctx.tenant_id, ctx.client),
                                                 body.path):
                    link = None
            except Exception:
                log.warning("nav filter failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return HelpResponse(reply=reply, link=link, submit=result.submit,
                            recommend=recs, closed_step=closed_step,
                            held_for=held_for, setup_cards=updated_cards, card_errors=card_errors,
                            evidence=getattr(result, 'evidence', None) or [])
    return await run_in_threadpool(_blocking)


# ── Feature request / issue report submit (authenticated) ────────────────────
# The write path behind the help-mode draft card: the assistant DRAFTS a feature
# request or issue report ({kind, summary} in the /help response), the customer
# reviews it in the chat and presses Submit, and THIS endpoint files it — into the
# same feature_requests pipeline as the /app/requests form (operator view at
# /admin/requests). It writes, so it carries the CSRF double-submit (header
# variant — the widget fetches JSON) on top of the tenant gate + rate limit.
MAX_SUMMARY_CHARS = 2000

SUBMIT_KINDS = ("feature", "bug")


class SubmitRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=20)
    summary: str = Field(..., min_length=1, max_length=MAX_SUMMARY_CHARS)


class SubmitResponse(BaseModel):
    ok: bool
    link: dict | None = None
    reference: str | None = None    # BLAM8-XXXX — quoted in the card + follow-ups


@router.post(
    "/submit",
    response_model=SubmitResponse,
    dependencies=[Depends(rate_limit("concierge_submit", limit=10, window_s=600)),
                  Depends(require_csrf_header)],
)
def submit(body: SubmitRequest, ctx: TenantCtx = Depends(require_tenant)) -> SubmitResponse:
    """File the approved draft for the signed-in tenant. Industry-tagged like the
    form route; `submitted_by` marks the chat provenance for the operator."""
    if body.kind not in SUBMIT_KINDS:
        raise HTTPException(status_code=400, detail="Unknown submission kind.")
    text = body.summary.strip()[:MAX_SUMMARY_CHARS]
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to submit.")
    try:
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        tenant = {}    # tagging is best-effort — never blocks the filing
    category = "Issue report" if body.kind == "bug" else "Feature idea"
    reference = db.add_feature_request(
        ctx.tenant_id, text, category=category, industry=tenant.get("industry"),
        submitted_by="assistant chat", kind=body.kind,
        user_email=tenant.get("mailbox"),                       # status checks match on this
        status="open" if body.kind == "bug" else None,          # issue rows use the ticket lifecycle
        client=ctx.client)
    return SubmitResponse(ok=True, reference=reference,
                          link={"label": "View your requests & reports",
                                "href": "/app/requests"})


def _asker(ctx: TenantCtx) -> dict | None:
    """Who is asking, as a People member — or None for an account that
    predates People, whose single user owns everything (app/people.can_open
    reads None that way). Best-effort: if we cannot tell who they are we do
    NOT narrow, because an owner silently losing their own tools is worse than
    the leak it would guard against on a one-person account."""
    try:
        from app import people as _people
        row = db.get_member(ctx.tenant_id, ctx.user_id or "")
        return _people.member(row) if row else None
    except Exception:
        # Fail CLOSED (M1): never read a failed lookup as the owner.
        log.warning("concierge asker read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        from app import people as _people
        return _people.member({"role": "staff", "active": False, "scopes": {}})


def people_is_admin(m: dict | None) -> bool:
    from app import people
    return people.is_admin(m)


def _live_asker(ctx: TenantCtx) -> dict | None:
    """_asker, with a switched-off member refused outright (403) — the side
    lists, voice context and notifications are theirs to see only while they
    work here (security review 2026-09, M34). No user at all is the account's
    own owner, as guards.member_of reads it."""
    if not getattr(ctx, "user_id", None):
        return None
    m = _asker(ctx)
    if m is not None and not m.get("active", True):
        raise HTTPException(status_code=403,
                            detail="You don't have permission to do that. "
                                   "Ask whoever runs this account.")
    return m


def _not_their_proposal(ctx: TenantCtx, proposal_id: str) -> str | None:
    """Why this person may not confirm or bin this card, or None. A
    switched-off member gets 403 (via _live_asker); an owner or manager may act
    on any card; anyone else only on a card staged for them."""
    asker = _live_asker(ctx)
    from app import concierge_delegation
    if concierge_delegation.enabled():
        linked = db.get_concierge_proposal(proposal_id, ctx.tenant_id, client=ctx.client)
        if linked and concierge_delegation.binding(linked) and linked.get("user_id") != ctx.user_id:
            return "This delegated request belongs to someone else."
    if asker is None or people_is_admin(asker):
        return None
    try:
        row = db.get_concierge_proposal(proposal_id, ctx.tenant_id, client=ctx.client)
    except Exception:
        log.warning("proposal owner read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return "I couldn't check that card — nothing has changed."
    if row and row.get("user_id") and row.get("user_id") != ctx.user_id:
        return "That card was set up for someone else — nothing has changed."
    return None


def _drafts_they_may_see(ctx: TenantCtx, m: dict | None, rows: list[dict]) -> list[dict]:
    """Waiting or sent voice drafts, through the asker's Inbox door and their
    own-mailbox lens (M34). None = the account's own owner: every row. A read
    that fails shows nothing rather than everything."""
    rows = list(rows or [])
    if m is None:
        return rows
    from app import people
    if not m.get("active", True) or not people.can_open(m, "inbox"):
        return []
    if not people.inbox_is_theirs_only(m):
        return rows
    try:
        boxes = db.list_mailboxes(ctx.tenant_id, client=ctx.client)
        default_id = boxes[0]["id"] if boxes else None
        kept = []
        for r in rows:
            entry = db.get_email_log_entry(r.get("log_id"), ctx.tenant_id, client=ctx.client)
            if entry and people.may_read_mailbox(m, entry.get("mailbox_id"), default_id):
                kept.append(r)
        return kept
    except Exception:
        log.warning("draft lens failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return []


def _invoices_they_may_see(m: dict | None, rows: list[dict]) -> list[dict]:
    """Draft invoices, through the asker's Money door (M34)."""
    from app import people
    if m is None:
        return list(rows or [])
    if not m.get("active", True) or not people.can_open(m, "money"):
        return []
    return list(rows or [])


# A notification's door: its deck topic's door (people.TOPIC_DOORS), plus the
# topics People doesn't list. A kind with no topic (the morning brief, a
# mailbox problem) is about the whole business — owner and manager only.
_NOTIFICATION_DOORS = {"quotes": "money", "questions": "inbox", "records": "crm"}


def _notification_is_theirs(m: dict | None, n: dict) -> bool:
    from app import deck, people
    if m is None or (m.get("active", True) and people.is_admin(m)):
        return True
    topic = deck.topic_of(str(n.get("kind") or ""))
    door = people.TOPIC_DOORS.get(topic) or _NOTIFICATION_DOORS.get(topic)
    return bool(door) and people.can_open(m, door)


# ── CRM recommended-settings apply (authenticated) ───────────────────────────
# The write path behind the help-mode "Use recommended settings" card for CRM
# settings ONLY. Diary settings and Booking details prefill their page's real
# form client-side (nothing saved until the customer presses that form's Save),
# but the CRM page is per-row forms with no end-confirm — so here the click IS
# the confirm. Since 2026-09-02 the apply REPLACES the pipeline (Julius: "only
# apply what the AI suggests"): the recommended stages become the set — a
# matching existing stage is kept (never duplicated, case-insensitive), any
# stage NOT in the recommendation is removed, its deals surfacing under
# Unstaged exactly as a manual delete would leave them (and the summary says
# so). A quiet-days-only apply removes nothing.
class KnowledgeApplyRequest(BaseModel):
    values: list[dict] = Field(..., max_length=settings_recommend.MAX_ENTRIES)


class KnowledgeApplyResponse(BaseModel):
    ok: bool
    summary: str
    link: dict | None = None


@router.post(
    "/knowledge-apply",
    response_model=KnowledgeApplyResponse,
    dependencies=[Depends(rate_limit("concierge_knowledge_apply", limit=20, window_s=600)),
                  Depends(require_csrf_header)],
)
async def knowledge_apply(body: KnowledgeApplyRequest,
                          ctx: TenantCtx = Depends(require_tenant)) -> KnowledgeApplyResponse:
    """Stage assistant notes for explicit evidence and overlap review; admit no facts."""
    from app import knowledge_coverage
    notes = knowledge_coverage.parse_notes(body.values)
    if not notes:
        raise HTTPException(status_code=400, detail="Nothing to save.")
    if not kb.is_configured():
        raise HTTPException(status_code=503, detail="The knowledge base isn't available right now.")
    from app import knowledge_review
    row = knowledge_review.stage(ctx.tenant_id,notes,origin="Assistant suggestion")
    return KnowledgeApplyResponse(ok=True,summary=f"{len(notes)} notes are pending review; nothing has been saved to Knowledge.",
        link={"label":"Review pending Knowledge","href":f"/onboarding/knowledge/review/{row['id']}"})



class CrmApplyRequest(BaseModel):
    values: list[dict] = Field(..., max_length=settings_recommend.MAX_ENTRIES)


class CrmApplyResponse(BaseModel):
    ok: bool
    summary: str
    link: dict | None = None


@router.post(
    "/crm-apply",
    response_model=CrmApplyResponse,
    dependencies=[Depends(rate_limit("concierge_crm_apply", limit=10, window_s=600)),
                  Depends(require_csrf_header)],
)
def crm_apply(body: CrmApplyRequest,
                    ctx: TenantCtx = Depends(require_tenant)) -> CrmApplyResponse:
    if "crm" not in db.active_modules(ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="CRM is not active for this account")
    stages = settings_recommend.parse_stages(body.values)[:12]
    quiet_days = settings_recommend.parse_quiet_days(body.values)
    if not stages and quiet_days is None:
        raise HTTPException(status_code=400, detail="Nothing to apply.")
    existing = db.crm_list_stages(ctx.tenant_id, client=ctx.client)
    have = {(s.get("name") or "").strip().lower() for s in existing}
    next_order = max([int(s.get("display_order") or 0) for s in existing], default=0) + 10
    added, removed = [], []
    if stages:
        # Replace, not append: whatever isn't in the recommendation goes.
        wanted = {s["name"].lower() for s in stages}
        for s in existing:
            name = (s.get("name") or "").strip()
            if name.lower() in wanted or not s.get("id"):
                continue
            db.crm_delete_stage(s["id"], ctx.tenant_id, client=ctx.client)
            have.discard(name.lower())
            removed.append(name)
    for s in stages:
        if s["name"].lower() in have:
            continue
        db.crm_insert_stage(ctx.tenant_id, s["name"], display_order=next_order,
                            stage_type=s["stage_type"], client=ctx.client)
        have.add(s["name"].lower())
        next_order += 10
        added.append(s["name"])
    if quiet_days is not None:
        db.upsert_tenant_config(ctx.tenant_id, {"crm_quiet_days": quiet_days})
    bits = []
    if added:
        bits.append("Added stages: " + ", ".join(added))
    kept = len(stages) - len(added)
    if kept:
        bits.append(f"{kept} already existed — kept")
    if removed:
        bits.append("Removed: " + ", ".join(removed)
                     + " (any deals there moved to Unstaged)")
    if quiet_days is not None:
        bits.append(f"Gone-quiet threshold set to {quiet_days} days")
    return CrmApplyResponse(ok=True, summary=". ".join(bits) + ".",
                            link={"label": "Review CRM settings",
                                  "href": "/app/crm/settings?msg=saved"})


# ── Fix mode (authenticated — thumbs-down on a draft) ────────────────────────
# The operator disagrees with a draft: the widget opens the assistant scoped to
# THAT draft. /fix chats (server loads the draft + current rules/requirements as
# context, writes nothing); /fix/apply executes the ONE approved action — never
# rule / knowledge / booking requirement / plain redraft — then re-drafts the
# reply so the fix shows immediately. Apply writes, so it carries the CSRF
# header double-submit on top of the tenant gate + rate limit.
def _live_bookings(ctx: TenantCtx, contact: str | None) -> list[dict]:
    """{ref, date, window, status, summary} for the contact's offered / proposed /
    confirmed bookings — the diary facts a draft may legitimately quote. Never
    raises: a fix chat is still worth having when the diary is unavailable."""
    try:
        rows = db.bookings_for_contact(ctx.tenant_id, contact, client=ctx.client)
    except Exception:
        return []
    return [{"ref": b.get("booking_ref"), "date": b.get("booking_date"), "window": b.get("window"),
             "status": b.get("status"), "summary": (b.get("summary") or "")[:80]}
            for b in rows[:12]]


def _fix_context(ctx: TenantCtx, log_id: str) -> tuple[dict, dict, list[str], list, str | None]:
    """(draft_ctx, tenant_config, requirement_labels, intake_fields, mailbox_id)
    for one owned draft — 404 when the row isn't this tenant's (IDOR shape)."""
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if not entry:
        raise HTTPException(status_code=404, detail="Draft not found")
    draft_ctx = {
        "customer_email": entry.get("from_address"),
        "subject": entry.get("subject"),
        "customer_message": (entry.get("body_in") or "")[:2000],
        "draft_reply": (entry.get("draft_reply") or "")[:2000],
        "request_summary": entry.get("request_summary"),
        # The customer's live bookings (run #7, 2026-09-05): the assistant
        # called "Saturday 08:30" invented and asked the owner whether it was
        # real while BK-1003 sat at 08:30 in the diary — it could not see it.
        "live_bookings": _live_bookings(ctx, entry.get("from_address")),
    }
    tcfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    fields = (db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}).get("intake_fields") or []
    labels = [f.get("label") for f in fields if f.get("label")]
    return draft_ctx, tcfg, labels, fields, entry.get("mailbox_id")


_TRACE_CHUNK_CHARS = 2000


def _whole_chunk(text) -> str:
    """A traced chunk as stored; past the cap it ends with a marker saying so —
    never a silent cut the model can mistake for a truncated note."""
    s = str(text or "")
    if len(s) <= _TRACE_CHUNK_CHARS:
        return s
    return s[:_TRACE_CHUNK_CHARS] + f" […the stored note continues for {len(s) - _TRACE_CHUNK_CHARS} more characters]"


def _stored_note_text(ctx: TenantCtx, source: str, mailbox_id: str | None) -> str:
    """The whole text of one existing note, "" when it cannot be read — the
    guard below then stands aside rather than blocking a fix."""
    try:
        chunks = db.knowledge_source_chunks(ctx.tenant_id, source, client=ctx.client,
                                            mailbox_id=mailbox_id)
        return kb.join_chunks(chunks) if chunks else ""
    except Exception:
        return ""


def _knowledge_sources_safe(ctx: TenantCtx) -> list[dict]:
    """The tenant's knowledge sources ({source, mailbox_id, …}), never raising — a
    knowledge fix is still worth proposing when the listing is unavailable."""
    try:
        return db.list_knowledge_sources(ctx.tenant_id, client=ctx.client) or []
    except Exception:
        return []


def _knowledge_note_names(ctx: TenantCtx) -> list[str]:
    """The names of the tenant's knowledge notes — prompt context so a knowledge fix
    can name an EXISTING note and rewrite it in place rather than adding a second."""
    return sorted({str(s.get("source")) for s in _knowledge_sources_safe(ctx)
                   if s.get("source")})


# The agent-settings fields the draft prompt is actually built from — the
# non-knowledge half of the grounding a suspect statement could have come from.
_GROUNDING_FIELDS = ("business_brief", "agent_name", "tone", "sign_off", "do_list",
                     "never_do", "escalation", "custom_instructions", "examples")


_STOPWORDS = frozenset((
    "the", "and", "for", "you", "your", "our", "are", "was", "were", "has", "have",
    "had", "can", "will", "with", "that", "this", "then", "than", "from", "about",
    "into", "onto", "not", "but", "all", "any", "its", "it's", "they", "them",
    "she", "him", "her", "his", "who", "what", "when", "where", "why", "how",
    "does", "did", "been", "being", "would", "could", "should", "there", "here",
    "just", "also", "very", "some", "more", "most", "such", "only", "over",
    "under", "out", "off", "per", "via", "one", "two", "get", "got", "let",
    "say", "said", "says", "want", "wants", "need", "needs", "please",
))


def _sig_words(phrase: str) -> set[str]:
    """Significant words (≥3 alnum chars, minus stopwords) for loose grounding
    matches. 3, not 5: the stale-settings hunt lives on short trade words —
    'car', 'MOT', 'van', 'dog', 'gas' — which longer thresholds silently drop."""
    return {w for w in "".join(ch if ch.isalnum() else " " for ch in phrase.lower()).split()
            if len(w) >= 3 and w not in _STOPWORDS}


def _settings_hits(tcfg: dict, phrase: str) -> list[dict]:
    """Settings fields that plausibly ground a phrase — shared significant words,
    case-insensitive. A loose hint list: the model judges relevance."""
    words = _sig_words(phrase)
    hits = []
    for field in _GROUNDING_FIELDS:
        text = str(tcfg.get(field) or "")
        if not text.strip():
            continue
        low = text.lower()
        if any(w in low for w in words):
            hits.append({"setting": field, "excerpt": text[:300]})
    return hits


def _requirement_hits(fields: list, phrase: str) -> list[dict]:
    """Booking requirements (intake fields) that plausibly ground a phrase. The
    gather flow drafts FROM these, so draft content the knowledge base can't
    explain very often comes from here — e.g. stale 'MOT or service type'
    requirements outliving a business-profile change. Without this bucket the
    trace tool reported such content as hallucinated when it was grounded."""
    words = _sig_words(phrase)
    hits = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        text = " — ".join(x for x in [str(f.get("label") or ""), str(f.get("hint") or "")] if x)
        if not text.strip():
            continue
        low = text.lower()
        if any(w in low for w in words):
            hits.append({"booking_requirement": f.get("label"), "excerpt": text[:200],
                         "required": bool(f.get("required"))})
    return hits


class FixRequest(BaseModel):
    log_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=MAX_INAPP_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list)


@router.post(
    "/fix",
    dependencies=[Depends(rate_limit("concierge_fix", limit=20, window_s=60))],
)
async def fix(body: FixRequest, ctx: TenantCtx = Depends(require_tenant)) -> StreamingResponse:
    """Diagnose what's wrong with one draft. Reads only; degrades gracefully.
    The model can trace suspect statements via search_grounding — knowledge
    chunks in the SAME scope the draft retrieved from (company-wide ∪ its
    mailbox) plus the agent-settings fields the prompt is built from. An empty
    trace is the honest answer: the statement was inferred, not in their data.

    STREAMS newline-JSON events so the widget can show the trace live:
      {"type": "tool", "phrase", "hint", "label", "detail"} — a grounding search just started
      {"type": "done", "reply", "action", "trail"} — the turn's final answer (always last);
        trail = [{name, label, detail}], the ghost trail the widget keeps as pills."""
    import asyncio
    import json
    draft_ctx, tcfg, _, fields, mailbox_id = _fix_context(ctx, body.log_id)

    async def _trace(name: str, args: dict) -> dict:
        phrase = str((args or {}).get("phrase") or "").strip()[:300]
        if name != "search_grounding" or not phrase:
            return {"knowledge": [], "settings": [], "found": False}
        try:
            chunks = await kb.search_chunks(ctx.tenant_id, phrase, mailbox_id=mailbox_id)
        except Exception:
            chunks = []
        settings_hits = _settings_hits(tcfg, phrase)
        req_hits = _requirement_hits(fields, phrase)
        # Each chunk rides WHOLE (the chunker caps a chunk at 1,500 chars): a
        # 400-char clip reached the model cut mid-word and it read the clip as
        # truncation in the store, then offered a shorter note as the fix
        # (run #8, 2026-09-05). The marker only ever fires on an oversized
        # import chunk — and then it says the note continues.
        return {"knowledge": [{"source": c.get("source"),
                               "content": _whole_chunk(c.get("content"))} for c in chunks[:4]],
                "settings": settings_hits,
                "booking_requirements": req_hits,
                "found": bool(chunks or settings_hits or req_hits)}

    question = body.question.strip()[:MAX_INAPP_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)

    queue: asyncio.Queue = asyncio.Queue()
    trail: list[dict] = []   # the ghost trail: every grounding search this turn ran

    async def _on_tool(args: dict) -> None:
        phrase = str((args or {}).get("phrase") or "")[:120]
        trail.append({"name": "search_grounding",
                      "label": "Checked knowledge & settings", "detail": phrase})
        await queue.put({"type": "tool", "phrase": phrase,
                         "hint": "Checking your knowledge & settings…",
                         "label": "Checked knowledge & settings", "detail": phrase})

    async def _ask():
        result = await concierge.answer_fix(question, history, draft_ctx=draft_ctx,
                                            never_rules=str(tcfg.get("never_do") or ""),
                                            requirement_labels=fields,   # full fields: label+required+hint
                                            business_profile=str(tcfg.get("business_brief") or ""),
                                            knowledge_notes=_knowledge_note_names(ctx),
                                            tool_executor=_trace, on_tool=_on_tool,
                                            tenant_id=ctx.tenant_id)
        usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                               operation="concierge_fix", module="concierge")
        return result

    def _empty(result) -> bool:
        return not str(result.reply or "").strip() and not (result.action or {}).get("kind")

    async def _run() -> None:
        try:
            result = await _ask()
            if _empty(result):
                # A degenerate structured output — {"reply": ""} with no action and
                # no trace (run #8, live 2026-09-05: the widget drew an empty bubble).
                # One retry, then an honest line rather than a blank turn.
                result = await _ask()
            reply = result.reply if not _empty(result) else (
                "I couldn't work that one out just now — ask me again, or tell me in a "
                "sentence what's wrong with the draft.")
            await queue.put({"type": "done", "reply": reply, "action": result.action,
                             "queue": list(result.queue or []), "trail": trail})
        except Exception:
            await queue.put({"type": "done",
                             "reply": "Sorry — I couldn't help with that just now. You can "
                                      "edit the draft directly, or add what's missing on the "
                                      "Knowledge page.",
                             "action": None, "trail": trail})
        await queue.put(None)

    async def _stream():
        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            await task

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


class FixApplyRequest(BaseModel):
    log_id: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., min_length=1, max_length=20)
    content: str = Field("", max_length=4000)
    label: str = Field("", max_length=80)     # knowledge only: the note's name
    # The owner's own words from this fix thread (round three, 1 Oct 2026, F25).
    # Over the cap is REJECTED (422) by this model, never silently cut here; the
    # widget trims to the cap before sending. Absent/empty = the old behaviour.
    direction: str = Field("", max_length=2000)


class FixApplyResponse(BaseModel):
    ok: bool
    applied: str
    draft: str | None = None      # the re-drafted reply, for the page to swap in


@router.post(
    "/fix/apply",
    response_model=FixApplyResponse,
    dependencies=[Depends(rate_limit("concierge_fix_apply", limit=10, window_s=300)),
                  Depends(require_csrf_header)],
)
async def fix_apply(body: FixApplyRequest, ctx: TenantCtx = Depends(require_tenant)) -> FixApplyResponse:
    """Execute the ONE approved fix, then re-draft the reply so it shows immediately."""
    from app import pipeline
    from app.dashboard import _append_never_rule    # lazy — routes-layer helper, no cycle at import time
    _, _, _, fields, _ = _fix_context(ctx, body.log_id)
    content = body.content.strip()
    if body.kind == "never":
        if not content:
            raise HTTPException(status_code=400, detail="Empty rule.")
        _append_never_rule(ctx.tenant_id, content, client=ctx.client)
        applied = "Never-say rule saved"
    elif body.kind == "knowledge":
        if not content:
            raise HTTPException(status_code=400, detail="Empty knowledge.")
        if not kb.is_configured():
            raise HTTPException(status_code=400, detail="Knowledge base is off.")
        # The note is named — by the assistant, or by the operator editing the card.
        # A name that already exists REWRITES that note in place (kb.ingest replaces
        # a source), which is the whole point when the fix is "that note is wrong":
        # appending a second one would leave the agent two contradictory facts. The
        # existing note's scope is kept, so correcting a mailbox-scoped note doesn't
        # silently fork a company-wide copy of it.
        source = body.label.strip() or f"Assistant fix · {body.log_id[:8]}"
        existing = next((s for s in _knowledge_sources_safe(ctx)
                         if str(s.get("source")) == source), None)
        if existing:
            # A rewrite never SHRINKS a note (run #8: the card's rewrite was 419
            # chars against the stored 520 and would have dropped the DVSA
            # sentence). The note stays whole; the card says why, and the
            # owner adds the missing part back — or shortens it on purpose on
            # the Knowledge page, where the whole note is in front of them.
            stored = _stored_note_text(ctx, source, existing.get("mailbox_id"))
            if stored and len(content) < 0.85 * len(stored):
                raise HTTPException(
                    status_code=409,
                    detail=(f"That rewrite is shorter than the note it replaces "
                            f"({len(content)} of {len(stored)} characters), so \"{source}\" "
                            "stays as it is. Include everything the note already says plus "
                            "the correction — or shorten it on purpose on the Knowledge page."))
        await kb.ingest(ctx.tenant_id, source, content, client=ctx.client,
                        mailbox_id=(existing or {}).get("mailbox_id"))
        from app import setup_held
        setup_held.mark_used(ctx.tenant_id, "knowledge", client=ctx.client)
        applied = "Knowledge rewritten" if existing else "Knowledge added"
    elif body.kind == "requirement":
        if not content:
            raise HTTPException(status_code=400, detail="Empty requirement.")
        # 'Label' adds; 'Label — hint' adds with guidance, or UPDATES the existing
        # field's hint (clearer wording is the fix when the requirement was already
        # there but the agent asked for it badly).
        label, _, hint = (p.strip() for p in content.partition("—"))
        label, hint = label[:80], hint[:200]
        norm = label.lower()
        existing = next((f for f in fields if (f.get("label") or "").strip().lower() == norm), None)
        if existing and hint:
            db.save_scheduling_intake(
                ctx.tenant_id,
                [({**f, "hint": hint} if f is existing else f) for f in fields],
                client=ctx.client)
            applied = "Booking requirement reworded"
        elif existing:
            applied = "Requirement already existed"
        else:
            db.save_scheduling_intake(ctx.tenant_id,
                                      fields + [{"label": label, "required": True, "hint": hint}],
                                      client=ctx.client)
            applied = "Booking requirement added"
    elif body.kind == "redraft":
        applied = "Re-drafted"
    else:
        raise HTTPException(status_code=400, detail="Unknown fix kind.")
    # The owner's words are their direction for this reply — the same
    # owner_instruction the voice Concierge passes (it may also offer what the
    # diary holds back for the owner, e.g. out of hours). Round three, 1 Oct 2026
    # (F25): a never-rule / note / requirement used to redraft with NO instruction,
    # so a three-part correction lost two parts. Every kind now carries the
    # owner's own thread messages (`direction`); a redraft card's own content
    # wins when it has any.
    direction = body.direction.strip()
    instruction = (content or direction) if body.kind == "redraft" else direction
    draft = await pipeline.redraft_email_log(ctx.tenant_id, body.log_id, client=ctx.client,
                                             owner_instruction=instruction)
    return FixApplyResponse(ok=True, applied=applied, draft=draft)


# ── Business Q&A (authenticated — /app/concierge + the widget's business tab) ─
# Read-only Q&A over the tenant's OWN data via the fixed tool catalogue in
# src/app/concierge_data.py. Tier gate: Starter sees inbox + knowledge tools only;
# the Pro set (CRM/pipeline/follow-ups/diary) turns on with the 'crm' module —
# the same boundary the CRM pages gate on. Every tool call is tenant-scoped
# server-side AND runs under the caller's RLS client, so a prompt-injected
# "show me another tenant" has nothing to reach. The endpoint persists the
# transcript (concierge_messages) so /app/concierge reloads with history; it
# writes only that, so it carries the CSRF header double-submit.
_BUSINESS_UUID = None   # lazy compiled regexes for link-id validation


def _business_link_patterns():
    global _BUSINESS_UUID
    if _BUSINESS_UUID is None:
        import re
        _BUSINESS_UUID = (
            re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
            re.compile(r"^\d{4}-\d{2}-\d{2}$"))
    return _BUSINESS_UUID


def resolve_business_links(links: list[dict]) -> list[dict]:
    """kind+id → real in-app hrefs. The model only ever names a KIND and an id
    it saw in tool results; URLs are minted here, and a malformed id drops the
    link rather than shipping a broken (or crafted) href."""
    from urllib.parse import quote
    uuid_re, date_re = _business_link_patterns()
    fixed = {"pipeline": "/app/crm/pipeline", "knowledge": "/app/knowledge",
             "settings": "/app/settings", "billing": "/billing/plans",
             "invoice_settings": "/app/invoices/settings",
             "concierge": "/app/concierge",
             "inbox": "/app/conversations", "invoices": "/app/invoices", "quotes": "/app/quotes", "money": "/app/money"}
    out, seen = [], set()
    for l in links or []:
        kind, id_, label = l.get("kind"), l.get("id") or "", l.get("label") or "Open"
        href = None
        if kind == "conversation" and "@" in id_ and " " not in id_:
            href = "/app/conversations/view?contact=" + quote(id_)
        elif kind == "contact" and uuid_re.match(id_):
            href = f"/app/crm/contacts/{id_}"
        elif kind == "contact" and "@" in id_ and " " not in id_:
            # The model often only holds the email (bookings/invoices carry no
            # contact_id — live 2026-09-06: 3 links asked, 1 rendered). The
            # conversation view is the right door for an email.
            href = "/app/conversations/view?contact=" + quote(id_)
        elif kind == "diary":
            href = "/app/diary" + (f"?date={id_}" if date_re.match(id_) else "")
        elif kind in fixed:
            href = fixed[kind]
        if href and href not in seen:
            seen.add(href)
            out.append({"label": label, "href": href})
    return out


class BusinessRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_INAPP_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list)
    # Which surface is asking (2026-09-06, Kris): the Assistant widget's "Your
    # business" tab is questions only; proposals (confirm-first actions) exist
    # only on the Concierge page. Default = the safe side.
    surface: Literal["widget", "page"] = "widget"
    display_turn_id: str = Field("", max_length=40)
    discovery_revision: str | None = Field(None, max_length=64)
    discovery_request_id: str | None = Field(None, max_length=2048)


@router.post("/business/discovery", dependencies=[
    Depends(rate_limit("concierge_discovery", limit=30, window_s=60)),
    Depends(require_csrf_header),
])
async def business_discovery(ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    """Generate/cache discovery text only; never invoke a business action."""
    from app import concierge_discovery
    from starlette.concurrency import run_in_threadpool
    try:
        snap = await run_in_threadpool(lambda: concierge_discovery.snapshot(ctx, _live_asker(ctx)))
        result = await concierge_discovery.discover(ctx, snap)
        # Membership/settings may change during a slow model response. Never
        # return the old view on either generation or a shared in-flight wait.
        current = await run_in_threadpool(lambda: concierge_discovery.snapshot(ctx, _live_asker(ctx)))
        if current["revision"] != snap["revision"]:
            raise HTTPException(status_code=409, detail="Your available requests changed. Explore again.")
        result["scope"] = concierge_discovery.scope_key(ctx)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception:
        log.warning("Concierge discovery context unavailable", exc_info=False)
        raise HTTPException(status_code=503, detail="Discovery is unavailable. You can still type a request.") from None


_SERVICE_FAILURE = ("The assistant can't reach its AI service right now — that's a "
                    "problem on our side, not with your question, and we've been "
                    "alerted. Please try again in a few minutes.")
_RETRY_SAME = ("I had trouble with that one — could you try asking it a "
               "different way?")


def service_failure_reply(e: BaseException) -> str:
    """Live 2026-09-06: the platform key ran out of API credit and every owner
    saw 'try asking it a different way' — and burned a trial turn rephrasing
    a question that was fine. Provider-side failures (billing, auth, quota,
    overload, timeout) say so; only a genuine processing error keeps the old
    line."""
    name = type(e).__name__
    text = str(e).lower()
    provider = {"AuthenticationError", "PermissionDeniedError", "RateLimitError",
                "InternalServerError", "APIConnectionError", "APITimeoutError",
                "APIStatusError", "OverloadedError", "BadRequestError"}
    if name in provider or any(w in text for w in (
            "credit balance", "billing", "api key", "overloaded", "rate limit",
            "timed out", "timeout")):
        return _SERVICE_FAILURE
    return _RETRY_SAME


def diary_shape_line(cfg: dict | None) -> str | None:
    """One sentence of diary facts for the business prompt so the model stops
    judging 'free' from a booking list (live 2026-09-06: capacity 3, two AM
    bookings → 'Thursday isn't free'). Data, not instructions."""
    from app.booking_policy import compile_policy
    policy = compile_policy(cfg)
    if not policy["ready"]:
        return policy["message"]
    try:
        cap = int(cfg.get("capacity_per_window") or 0)
        windows = str(cfg.get("windows") or "day")
        parts = []
        if windows == "day":
            parts.append(f"a timed diary: {int(cfg.get('slot_minutes') or 60)}-minute slots "
                         f"between {str(cfg.get('open_time') or '')[:5]} and "
                         f"{str(cfg.get('close_time') or '')[:5]}, up to {cap} "
                         f"booking{'s' if cap != 1 else ''} at the same time")
        else:
            parts.append(f"arrival windows ({windows}), up to {cap} "
                         f"booking{'s' if cap != 1 else ''} per window")
        svcs = []
        for s in (cfg.get("service_types") or [])[:8]:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            timing = {"half": "morning/afternoon window", "day": "whole day"}.get(
                str(s.get("timing") or ""), "clock time")
            mins = s.get("minutes")
            svcs.append(f"{s['name']} ({mins} min, booked by {timing})" if mins
                        else f"{s['name']} (booked by {timing})")
        if svcs:
            parts.append("services: " + "; ".join(svcs))
        if cfg.get("buffer_minutes"):
            parts.append(f"a {int(cfg['buffer_minutes'])}-minute gap between bookings")
        if cfg.get("working_days"):
            parts.append(f"working days {cfg['working_days']}")
        if cfg.get("min_notice_hours"):
            parts.append(f"minimum notice {int(cfg['min_notice_hours'])} hours")
        if cfg.get("blackout_dates"):
            parts.append(f"{len(cfg['blackout_dates'])} blackout dates set")
        return "; ".join(parts)
    except Exception:
        return None


# Live phrasing while a lookup runs vs the settled pill it leaves behind in the
# ghost trail — "Checking your inbox…" is honest mid-flight, "Checked your
# inbox" is honest afterwards.
_BUSINESS_TOOL_HINTS = {
    "get_product_help": "Checking product help…",
    "get_inbox_overview": "Checking your inbox…",
    "search_conversations": "Searching your inbox…",
    "get_conversation": "Reading the conversation…",
    "search_contacts": "Looking up contacts…",
    "get_contact_timeline": "Reading the customer's history…",
    "list_quotes": "Checking your quotes…",
    "get_availability": "Checking what fits in the diary…",
    "get_contact_record": "Opening the customer's record…",
    "list_dormant_contacts": "Looking for customers who've gone quiet…",
    "list_expiring": "Checking what's running out…",
    "list_awaiting_approval": "Checking what's awaiting your approval…",
    "list_deals": "Checking your pipeline…",
    "get_pipeline_summary": "Summarising your pipeline…",
    "list_followups": "Checking follow-ups…",
    "list_bookings": "Checking the diary…",
    "search_knowledge_base": "Searching your knowledge base…",
    "list_invoices": "Checking your invoices…",
    "draft_email": "Writing the email…",
    "show_draft": "Bringing up the draft…",
    concierge_tasks.TOOL_NAME: concierge_tasks.HINT,
    debrief_walk.TOOL_NAME: debrief_walk.HINT,
}
_BUSINESS_TOOL_LABELS = {
    "get_product_help": "Checked product help",
    "get_inbox_overview": "Checked your inbox",
    "search_conversations": "Searched your inbox",
    "get_conversation": "Read the conversation",
    "search_contacts": "Looked up contacts",
    "get_contact_timeline": "Read the customer's history",
    "list_quotes": "Checked your quotes",
    "list_expiring": "Checked what's running out",
    "list_awaiting_approval": "Checked what's awaiting your approval",
    "get_availability": "Checked what fits in the diary",
    "get_contact_record": "Opened the customer's record",
    "list_dormant_contacts": "Looked for customers who've gone quiet",
    "list_deals": "Checked your pipeline",
    "get_pipeline_summary": "Summarised your pipeline",
    "list_followups": "Checked follow-ups",
    "list_bookings": "Checked the diary",
    "search_knowledge_base": "Searched your knowledge base",
    "list_invoices": "Checked your invoices",
    "draft_email": "Wrote the email",
    "show_draft": "Brought up the draft",
    concierge_tasks.TOOL_NAME: concierge_tasks.LABEL,
    debrief_walk.TOOL_NAME: debrief_walk.LABEL,
}
# Which tool-input keys are safe to echo into a pill's click-through detail, in
# preference order. An allowlist, never the raw input dict — tool args are model
# output and could carry anything.
_TOOL_DETAIL_KEYS = ("query", "contact", "stage", "date_from", "status")


def tool_detail(args: dict) -> str:
    for key in _TOOL_DETAIL_KEYS:
        val = str((args or {}).get(key) or "").strip()
        if val:
            return val[:80]
    return ""


def finish_business_turn(ctx: TenantCtx, result, *, question: str, trail: list,
                         tools_called: list, t0: float,
                         full_qa: bool, scheduling_on: bool,
                         actions: bool = True, member: dict | None = None,
                         persist: bool = True, spoken_ms: int = 0,
                         tasks: dict | None = None, origin: str = "typed", display_turn_id: str = "", native_events: dict | None = None,
                         intake: dict | None = None) -> dict:
    """Everything that happens AFTER answer_business returns, shared by the
    typed /business route and the live-voice /voice/chat route so the two
    surfaces can never drift: meter the usage, resolve links, trace the turn,
    validate + stage a proposed action (a proposal that can't be prepared
    degrades to an honest sentence), persist the exchange, and return the
    final `done` payload."""
    import time as _time
    usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                           operation="concierge_business", module="concierge")
    links = resolve_business_links(result.links)
    reply = result.reply
    # The turn trace: which lookups ran (an empty list makes the
    # "answered without looking anything up" pattern visible), whether
    # an action was proposed, and how long the exchange took.
    trace.emit(ctx.tenant_id, "concierge_turn", user_id=ctx.user_id,
               latency_ms=(_time.monotonic() - t0) * 1000,
               detail={"question": question[:300], "reply_chars": len(reply or ""),
                       "tools_called": tools_called,
                       "proposed_action": bool(result.action)})
    # A proposed action is validated against REAL data and staged as a
    # pending confirmation card — never executed here. A proposal that
    # can't be prepared degrades to an honest sentence in the reply.
    proposal = None
    correction = None
    if tasks and result.action:
        # A task list started this turn: every change arrives from the list
        # itself (docs/plan-concierge-task-list.md), never a card on top of it.
        result = result._replace(action=None)
    if result.action and not actions:
        # Defence in depth for the questions-only surface: whatever the model
        # returned, nothing is staged from here.
        trace.emit(ctx.tenant_id, "concierge_propose", outcome="rejected",
                   user_id=ctx.user_id,
                   detail={"kind": str((result.action or {}).get("kind") or "")[:40],
                           "reason": "questions-only surface"})
    elif result.action and getattr(ctx, "viewing_as", None):
        # View as (src/app/viewas.py): the owner is seeing what this person would
        # get. Staging would leave a real waiting card on THEIR account, so
        # nothing is staged — the answer says what would have happened.
        try:
            what = concierge_actions.stage_check(ctx.tenant_id, result.action, member=member,
                                                 full_qa=full_qa, scheduling_on=scheduling_on,
                                                 client=ctx.client)
            # Said TO the person being viewed, as the app would say it to
            # them (Kris 2026-09-24: "talk to them, not about them").
            correction = (f"(You'd get a card here to confirm — \"{what}\". "
                          "View as sets nothing up.)")
        except concierge_actions.ActionError as e:
            correction = f"({str(e)[:1].upper()}{str(e)[1:]}.)"
        reply = f"{reply}\n\n{correction}"
    elif result.action and persist:
        try:
            proposal = concierge_actions.stage(
                ctx.tenant_id, ctx.client, result.action,
                full_qa=full_qa, scheduling_on=scheduling_on,
                user_id=ctx.user_id, member=member, spoken_ms=spoken_ms)
            trace.emit(ctx.tenant_id, "concierge_propose",
                       user_id=ctx.user_id, proposal_id=proposal["proposal_id"],
                       detail={"kind": proposal["kind"],
                               "summary": proposal["summary"],
                               "expires_s": proposal["expires_s"]})
            if _claims_done(reply):
                # Only a card was staged. Never let the words say it was done (L2, 2026-10-16: "Note added").
                reply = ("That's ready for you to confirm: " + str(proposal["summary"]).rstrip(".") +
                         ". Tap the thumbs-up on the card to save it, or the bin to cancel.")
        except concierge_actions.ActionError as e:
            # The model's words were written expecting a card ("I'll update
            # that now — want me to go ahead?"); with no card they promise
            # something that isn't there, and a "yes" to them lands on nothing
            # (live 2026-09-23: Bella's food → "the record's ready to go").
            # The reply becomes the honest outcome, and voice says it too.
            correction = f"Sorry — I can't do that: {e} Nothing has changed."
            reply = correction
            trace.emit(ctx.tenant_id, "concierge_propose", outcome="rejected",
                       user_id=ctx.user_id,
                       detail={"kind": str((result.action or {}).get("kind") or "")[:40],
                               "reason": str(e)[:300]})
        except Exception:
            correction = "Sorry — I couldn't prepare that just now. Nothing has changed."
            reply = correction
            trace.emit(ctx.tenant_id, "concierge_propose", outcome="error",
                       user_id=ctx.user_id,
                       detail={"kind": str((result.action or {}).get("kind") or "")[:40]})
    # Persist the exchange (best-effort) so /app/concierge reloads with it —
    # unless the owner struck it out (tapped the heard words): usage and trace still count.
    if persist and not getattr(ctx, "viewing_as", None):   # View as saves nothing under their name
        db.add_concierge_message(ctx.tenant_id, "user", question,
                                 user_id=ctx.user_id, client=ctx.client, origin=origin, display_turn_id=display_turn_id, native_events=native_events)
        db.add_concierge_message(
            ctx.tenant_id, "assistant",
            reply + (f"\n\n[Proposed: {proposal['summary']}]" if proposal else "")
            + (f"\n\n[Tasks: {tasks['batch_id']}]" if tasks else ""),
            user_id=ctx.user_id, links=links, client=ctx.client, origin=origin, display_turn_id=display_turn_id, native_events=native_events,
            # The intake receipt (src/app/concierge_intake.py) rides hidden on the stored reply only;
            # the `trail` returned to the page below is exactly the lookups, as before.
            trail=list(trail) + [r for r in [concierge_intake.receipt(
                tasks, intake, question=question, tools_called=tools_called)] if r])
    if proposal:
        proposal = {**proposal, "origin":origin}
    return {"type": "done", "reply": reply, "links": links,
            "proposal": proposal, "trail": trail, "correction": correction,
            "tasks": tasks, "origin": origin, "display_turn_id": display_turn_id}


@router.post(
    "/business",
    dependencies=[
        Depends(rate_limit("concierge_business_burst", limit=10, window_s=60)),
        # The spec's abuse ceiling: 30 questions per hour.
        Depends(rate_limit("concierge_business", limit=30, window_s=3600)),
        Depends(require_csrf_header),
    ],
)
async def business(body: BusinessRequest, ctx: TenantCtx = Depends(require_concierge)) -> StreamingResponse:
    """Answer one question about the tenant's own business data. STREAMS
    newline-JSON so the UI can narrate the lookups live:
      {"type": "tool", "name", "hint", "label", "detail"} — a lookup just started
      {"type": "done", "reply", "links", "trail"}         — the final answer (always
        last); trail = [{name, label, detail}], persisted with the message so the
        ghost trail replays from history."""
    import asyncio
    import json

    if body.discovery_revision is not None or body.discovery_request_id is not None:
        from app import concierge_discovery
        from starlette.concurrency import run_in_threadpool
        try:
            snap = await run_in_threadpool(lambda: concierge_discovery.snapshot(ctx, _live_asker(ctx)))
            valid = body.surface == "page" and concierge_discovery.verify(
                snap, body.discovery_revision, body.discovery_request_id, body.question)
        except HTTPException:
            raise
        except Exception:
            valid = False
        if not valid:
            raise HTTPException(status_code=409, detail=
                                "These suggestions have changed. Explore again to choose a current request.")

    question = body.question.strip()[:MAX_INAPP_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)

    modules = db.visible_modules(ctx.tenant_id, client=ctx.client)
    full_qa = "crm" in modules
    scheduling_on = "scheduling" in modules
    actions = body.surface == "page"
    if actions and _is_bare_affirmation(question):
        waiting = await run_in_threadpool_safe(lambda: db.get_live_pending_proposal(
            ctx.tenant_id, ctx.user_id, client=ctx.client))
        if waiting:
            # A card is waiting and this is only a yes: say where it is. No model turn, no second card.
            trace.emit(ctx.tenant_id, "concierge_turn", user_id=ctx.user_id,
                       detail={"question": question[:300], "reply_chars": len(BARE_YES_REPLY), "tools_called": [],
                               "proposed_action": False, "answered_without_model": "card_waiting"})
            if not getattr(ctx, "viewing_as", None):
                db.add_concierge_message(ctx.tenant_id, "user", question, user_id=ctx.user_id, client=ctx.client,
                                         origin="typed", display_turn_id=body.display_turn_id)
                db.add_concierge_message(ctx.tenant_id, "assistant", BARE_YES_REPLY, user_id=ctx.user_id,
                                         client=ctx.client, origin="typed", display_turn_id=body.display_turn_id)

            async def _tap_the_card():
                yield json.dumps({"type": "done", "reply": BARE_YES_REPLY, "links": [], "trail": [], "proposal": None,
                                  "correction": None, "tasks": None, "origin": "typed",
                                  "display_turn_id": body.display_turn_id}) + "\n"
            return StreamingResponse(_tap_the_card(), media_type="application/x-ndjson")
    try:
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        tenant = {}
    diary_shape = None
    if scheduling_on:
        try:
            diary_shape = diary_shape_line(
                db.get_scheduling_config(ctx.tenant_id, client=ctx.client))
        except Exception:
            diary_shape = None
    # Trial concierge allowance (full-product trial, 2026-07-31): the trial
    # opens every module, so each LLM surface carries its own pot — drafts on
    # TRIAL_EMAIL_CAP, this assistant on TRIAL_CONCIERGE_CAP. Same enforcement
    # flag as the email gate; a paying tenant never sees this.
    if trial.concierge_blocked(tenant):
        async def _capped():
            yield json.dumps({"type": "done", "links": [], "trail": [], "reply":
                "You've used the assistant questions included in the free trial. "
                "Any paid plan removes this limit — upgrade under Settings → "
                "Billing (it takes about a minute), and everything you've set up "
                "carries straight over."}) + "\n"
        return StreamingResponse(_capped(), media_type="application/x-ndjson")
    if settings.require_subscription_gate and trial.on_trial(tenant):
        trial.consume_concierge(ctx.tenant_id)
    # Daily fair-use allowance (docs/modules/module-concierge-cost-controls.md Part B) —
    # ALWAYS-ON, per plan tier, generous enough a normal owner never sees it.
    # Fail-open inside consume(); refusal uses the trial-cap NDJSON shape.
    allowed, day_cap = fairuse.consume(ctx.tenant_id, tenant)
    if not allowed:
        async def _day_capped():
            yield json.dumps({"type": "done", "links": [], "trail": [],
                              "reply": fairuse.capped_reply(tenant, day_cap)}) + "\n"
        return StreamingResponse(_day_capped(), media_type="application/x-ndjson")
    # A compact voice block so proposed drafts read like the business writes
    # (best-effort — its absence just means a more generic draft).
    voice = None
    try:
        tcfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        voice = "\n".join(f"{k}: {v}" for k, v in (
            ("Signs off as", tcfg.get("sign_off")),
            ("Tone", tcfg.get("tone")),
            ("Agent name", tcfg.get("agent_name")),
            ("Style notes", (tcfg.get("voice_notes") or "")[:300]),
        ) if v) or None
    except Exception:
        pass
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)")

    # People slice 4: the assistant answers within THIS person's doors — the
    # catalogue is narrowed and the executor refuses, because hiding a tool
    # from the model is not the same as refusing it.
    asker = _asker(ctx)
    # Explanation-only setup evidence for typed business chat. The catalogue
    # is shared with discovery; it introduces no settings tools or write path.
    settings_guidance = None
    if actions:
        from app import concierge_guidance
        from starlette.concurrency import run_in_threadpool
        try:
            settings_guidance = await run_in_threadpool(
                lambda: concierge_guidance.context(ctx, _live_asker(ctx),
                    db.visible_modules(ctx.tenant_id, client=ctx.client)))
        except Exception:
            log.warning("Concierge settings guidance unavailable", exc_info=False)
    # Several separate things in one message start a task list worked in the
    # background (docs/plan-concierge-task-list.md) — wherever cards can be set up.
    starter = concierge_tasks.Starter(ctx, surface="typed", question=question) if actions else None

    async def _executor(name: str, args: dict) -> dict:
        if starter and name == concierge_tasks.TOOL_NAME:
            return await starter.start(args)
        if starter and name == concierge_tasks.CONTROL_NAME:
            return await starter.control(args)
        out = await concierge_data.execute(ctx.tenant_id, ctx.client, name, args,
                                           full_qa=full_qa, member=asker, user_id=ctx.user_id)
        # What the lookup FOUND rides on its trail line, so the next turn can
        # reuse it (test run 4.2b, 24 Sep: "yes, anyway" re-ran the contact
        # search and the record lookup for Graham, already found a turn before).
        found = _found_in(out)
        if found:
            for t in reversed(trail):
                if t.get("name") == name and not t.get("found"):
                    t["found"] = found
                    break
        return out

    queue: asyncio.Queue = asyncio.Queue()

    tools_called: list[str] = []
    trail: list[dict] = []   # the ghost trail: [{name, label, detail}] per lookup

    async def _on_tool(name: str, args: dict) -> None:
        tools_called.append(name)
        detail = tool_detail(args)
        label = _BUSINESS_TOOL_LABELS.get(name, "Searched your data")
        trail.append({"name": name, "label": label, "detail": detail})
        await queue.put({"type": "tool", "name": name,
                         "hint": _BUSINESS_TOOL_HINTS.get(name, "Searching your data…"),
                         "label": label, "detail": detail})

    async def _run() -> None:
        # One request scope over the whole turn: the tool-loop llm_calls rows,
        # the turn trace AND any proposal trace share one request_id.
        import time as _time
        t0 = _time.monotonic()
        with llm.request_scope():
            await _run_scoped(t0)

    async def _run_scoped(t0: float) -> None:
        import time as _time
        try:
            result = await concierge.answer_business(
                question, history,
                tools=concierge_data.tools_for(full_qa, asker)
                + (concierge_tasks.foreground_tools() if starter else []),
                tool_executor=_executor, on_tool=_on_tool,
                business_name=tenant.get("name") or tenant.get("company"),
                today=today, full_qa=full_qa, voice=voice, tenant_id=ctx.tenant_id,
                actions=actions, diary_shape=diary_shape,
                settings_guidance=settings_guidance,
                question_note="\n".join(filter(None, [
                    _people_note(ctx, asker, full_qa, scheduling_on),
                    concierge_delegation.context_note(ctx, None, surface="typed") if actions else ""])),
                tasks_on=bool(starter))
            done = finish_business_turn(
                ctx, result, question=question, trail=trail,
                tools_called=tools_called, t0=t0,
                tasks=starter.batch if starter else None, intake=starter.intake if starter else None,
                display_turn_id=body.display_turn_id,
                full_qa=full_qa, scheduling_on=scheduling_on, actions=actions,
                member=asker)
            await queue.put(done)
        except Exception as e:
            trace.emit(ctx.tenant_id, "concierge_turn", outcome="error",
                       user_id=ctx.user_id,
                       latency_ms=(_time.monotonic() - t0) * 1000,
                       detail={"question": question[:300],
                               "tools_called": tools_called,
                               "error": type(e).__name__})
            reply = service_failure_reply(e)
            if reply == _SERVICE_FAILURE:
                # A provider failure is ours: the turn must not count against
                # the owner's trial or daily pot (live 2026-09-06: the key ran
                # out of credit and every failed rephrase burned a turn).
                try:
                    db.refund_concierge_turn(ctx.tenant_id, client=ctx.client)
                except Exception:
                    log.warning("concierge refund failed (tenant=%s)", ctx.tenant_id, exc_info=True)
            await queue.put({"type": "done", "reply": reply,
                             "links": [], "trail": trail})
        await queue.put(None)

    async def _stream():
        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            await task

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


class BusinessHistoryResponse(BaseModel):
    messages: list[dict]
    full_qa: bool
    html: str = ""


@router.get("/business/history", response_model=BusinessHistoryResponse)
def business_history(ctx: TenantCtx = Depends(require_concierge), context_only: bool = False) -> BusinessHistoryResponse:
    """The saved transcript (oldest-first) + the tier flag, for page/widget load."""
    try:
        from app import guards as _guards
        messages = db.list_concierge_messages(
            ctx.tenant_id, user_id=ctx.user_id,
            include_shared=_guards.sees_business_wide(ctx), include_native=not context_only, client=ctx.client)
    except Exception:
        messages = []
    full_qa = "crm" in db.visible_modules(ctx.tenant_id, client=ctx.client)
    from app.concierge_history import attach_task_lists
    from app.templating import templates
    if not context_only:
        attach_task_lists(ctx, messages)
    return BusinessHistoryResponse(messages=messages, full_qa=full_qa,
        html="" if context_only else templates.env.get_template("app/_concierge_history.html").render(chat_history=messages, brief_cog=None))



@router.post(
    "/business/clear",
    dependencies=[Depends(rate_limit("concierge_business_clear", limit=6, window_s=600)),
                  Depends(require_csrf_header)],
)
def business_clear(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    """Start fresh: wipe the saved transcript (their own chat log only)."""
    try:
        from app import guards as _guards
        db.clear_concierge_messages(ctx.tenant_id, user_id=ctx.user_id,
                                    include_shared=_guards.sees_business_wide(ctx),
                                    client=ctx.client)
    except Exception:
        pass
    return {"ok": True}


# ── Post-onboarding guided-settings hand-off (src/app/setup_handoff.py) ──────────
# The widget's read side: should the guided strip show for this tenant, and
# which settings steps remain. Decisions are pure (setup_handoff); this route
# just gathers the rows. Degrade-not-500: any read hiccup means "don't show" —
# the strip is a nudge, never worth an error.
class SetupStatusResponse(BaseModel):
    show: bool
    steps: list[dict]


@router.get(
    "/setup-status",
    response_model=SetupStatusResponse,
    dependencies=[Depends(rate_limit("concierge_setup_status", limit=30, window_s=60))],
)
def setup_status(ctx: TenantCtx = Depends(require_onboarded)) -> SetupStatusResponse:
    """The guided hand-off state for the signed-in tenant."""
    try:
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
        tcfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        # Cheapest gates first: a stale or dismissed tenant costs two reads, not five.
        if tcfg.get("settings_handoff_dismissed_at") or \
                not setup_handoff.is_fresh(tenant.get("onboarded_at")):
            return SetupStatusResponse(show=False, steps=[])
        modules = db.active_modules(ctx.tenant_id, client=ctx.client)
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
        stages = (db.crm_list_stages(ctx.tenant_id, client=ctx.client)
                  if "crm" in modules else [])
        invoice_row = db.get_invoice_settings(ctx.tenant_id, client=ctx.client)
        groups = (db.list_requirement_groups(ctx.tenant_id, client=ctx.client)
                  if setup_handoff.DIARY_MODULES & modules else [])
        step_list = setup_handoff.steps(modules, cfg, stages,
                                        tcfg.get("crm_quiet_days"), invoice_row,
                                        dependant_groups=groups)
        return SetupStatusResponse(
            show=setup_handoff.show(tenant.get("onboarded_at"),
                                    tcfg.get("settings_handoff_dismissed_at"), step_list),
            steps=step_list)
    except Exception:
        return SetupStatusResponse(show=False, steps=[])


@router.post(
    "/setup-dismiss",
    dependencies=[Depends(rate_limit("concierge_setup_dismiss", limit=6, window_s=600)),
                  Depends(require_csrf_header)],
)
def setup_dismiss(ctx: TenantCtx = Depends(require_onboarded)) -> dict:
    """The explicit "I'll finish these myself" — stamps the dismiss so the strip
    stays gone on every device."""
    from datetime import datetime, timezone
    db.upsert_tenant_config(
        ctx.tenant_id,
        {"settings_handoff_dismissed_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


# ── Proactive notifications (the bubble's badge/glow + digest card) ──────────
# Rows are written wherever a push fires (src/app/push.py) — this is only the read
# side. Degrade-not-500: a feed hiccup renders as "nothing new", never an error.
class NotificationsResponse(BaseModel):
    unseen: int
    items: list[dict]


@router.get(
    "/notifications",
    response_model=NotificationsResponse,
    dependencies=[Depends(rate_limit("concierge_notifications", limit=60, window_s=60))],
)
def notifications(ctx: TenantCtx = Depends(require_onboarded)) -> NotificationsResponse:
    """Unseen count + the latest feed items, for the bubble badge and digest.
    Each item through the asker's doors (M34): a staff member's feed is what
    their doors open onto; the business-wide ones are the owner's."""
    asker = _live_asker(ctx)
    try:
        items = db.list_concierge_notifications(ctx.tenant_id, client=ctx.client)
        if asker is None or people_is_admin(asker):
            unseen = db.count_unseen_concierge_notifications(ctx.tenant_id, client=ctx.client)
        else:
            items = [n for n in items if _notification_is_theirs(asker, n)]
            unseen = sum(1 for n in items if not n.get("seen_at"))
    except Exception:
        items, unseen = [], 0
    return NotificationsResponse(
        unseen=unseen,
        items=[{"kind": n.get("kind"), "title": n.get("title"),
                "body": n.get("body") or "", "url": n.get("url") or "",
                "created_at": n.get("created_at"),
                "seen": bool(n.get("seen_at"))} for n in items])


@router.post(
    "/notifications/{notification_id}/dismiss",
    dependencies=[Depends(rate_limit("concierge_notification_dismiss",
                                     limit=60, window_s=60)),
                  Depends(require_csrf_header)],
)
def dismiss_notification(notification_id: str,
                               ctx: TenantCtx = Depends(require_onboarded)) -> dict:
    asker = _live_asker(ctx)
    if asker is not None and not people_is_admin(asker):
        # Their swipe must not clear a row off the owner's Today that they
        # were never shown (security review M34/M38).
        row = db.get_concierge_notification(ctx.tenant_id, notification_id, client=ctx.client)
        if not row or not _notification_is_theirs(asker, row):
            return {"ok": True}
    """The swipe (Kris 2026-09-04). Seen kills the glow; dismissed takes the row
    off Today for good — the two are different answers and the feed keeps both.

    Returns ok even when nothing was updated: an already-dismissed row and a
    double-tap are the same outcome from the operator's side, and the id came
    off a page so it is never trusted to be theirs — db scopes the update to
    the tenant as well as leaning on RLS."""
    try:
        db.dismiss_concierge_notification(ctx.tenant_id, notification_id,
                                          client=ctx.client)
    except Exception:
        log.warning("notification dismiss failed (tenant=%s)", ctx.tenant_id,
                    exc_info=True)
        return {"ok": False}
    return {"ok": True}


@router.post(
    "/notifications/seen",
    dependencies=[Depends(rate_limit("concierge_notifications_seen", limit=30, window_s=60)),
                  Depends(require_csrf_header)],
)
def notifications_seen(ctx: TenantCtx = Depends(require_onboarded)) -> dict:
    """Opening the panel acknowledges everything unseen — kills the glow.
    Someone with a narrower view acknowledges only the rows they were shown."""
    asker = _live_asker(ctx)
    try:
        if asker is not None and not people_is_admin(asker):
            rows = db.list_concierge_notifications(ctx.tenant_id, limit=200, client=ctx.client)
            ids = [n["id"] for n in rows if not n.get("seen_at") and n.get("id")
                   and _notification_is_theirs(asker, n)]
            if ids:
                db.mark_concierge_notifications_seen(ctx.tenant_id, ids=ids, client=ctx.client)
            return {"ok": True}
        db.mark_concierge_notifications_seen(ctx.tenant_id, client=ctx.client)
    except Exception:
        pass
    return {"ok": True}


# ── Business actions: Confirm / Cancel (Phase 2) ─────────────────────────────
# The ONLY write paths behind a proposal card. Execute atomically claims the
# pending row (single-use, 5-minute expiry — a stale or replayed Confirm can
# never fire twice), runs the platform's existing write helpers, and stamps
# the outcome back on the row (the audit trail). Cancel just closes the card.
class BusinessExecuteRequest(BaseModel):
    display_origin: Literal["typed", "voice", "live", "unknown"] = "unknown"
    # How the confirm was made: the card's own Confirm button sends "tap"; the Standard
    # voice client's spoken yes sends "words". A missing marker is "unknown", and only
    # "tap" is a tap: a kind in concierge_actions.TAP_ONLY_KINDS (a delete) is refused
    # to anything else, so no client can run one by words.
    confirm_via: Literal["tap", "words", "unknown"] = "unknown"
    proposal_id: str = Field(..., min_length=1, max_length=64)
    # draft_reply only: the operator's edited text wins over the staged draft.
    draft_text: str = Field("", max_length=8000)
    # bulk only: the indices the owner left ticked (None = every prepared item).
    selected: list[int] | None = None


class BusinessExecuteResponse(BaseModel):
    state: str = "unknown"
    proposal_id: str | None = None
    ok: bool
    summary: str
    link: dict | None = None
    results: list | None = None      # bulk: one {index, ok, summary} per item run


_UNSET_PROPOSAL_ASKER = object()


def _proposal_refusal(ctx, row, *, asker=_UNSET_PROPOSAL_ASKER):
    if asker is _UNSET_PROPOSAL_ASKER:
        asker = _live_asker(ctx)
    kinds = [row.get("kind") or ""]
    if row.get("kind") == "bulk":
        kinds = [item.get("kind") for item in (row.get("payload") or {}).get("items") or [] if item.get("ok")]
    if any(not concierge_actions.may_do(asker, kind) for kind in kinds):
        return "You don't have permission for this action."
    return concierge_actions.mailbox_refusal(ctx.tenant_id, ctx.client, row.get("kind") or "", row.get("payload") or {}, asker)


def _status_row(ctx, proposal_id):
    refused = _not_their_proposal(ctx, proposal_id)
    if refused:
        raise HTTPException(status_code=403, detail="This proposal isn't available to you.")
    row = db.get_concierge_proposal(proposal_id, ctx.tenant_id, client=ctx.client)
    if not row:
        raise HTTPException(status_code=404, detail="This proposal isn't available.")
    if _proposal_refusal(ctx, row):
        raise HTTPException(status_code=403, detail="This proposal isn't available to you.")
    return row


def _public_proposal(ctx, row):
    from app import proposal_outcomes
    response = proposal_outcomes.public(row)
    _remember_speech(ctx, response["summary"])
    return BusinessExecuteResponse(**response)


@router.get("/business/status/{proposal_id}", response_model=BusinessExecuteResponse)
def business_status(proposal_id: str, ctx: TenantCtx = Depends(require_concierge)):
    """Read-only reconciliation: same operation, scoped result, never replay."""
    return _public_proposal(ctx, _status_row(ctx, proposal_id))


@router.post("/business/reconcile", response_model=BusinessExecuteResponse,
             dependencies=[Depends(require_csrf_header)])
def business_reconcile(body: BusinessExecuteRequest, ctx: TenantCtx = Depends(require_concierge)):
    """Persist conservative overdue classification; never dispatch effects."""
    from app import proposal_outcomes
    row = _status_row(ctx, body.proposal_id)
    execution = proposal_outcomes._dict(proposal_outcomes._dict(row.get("result")).get("execution"))
    if row.get("status") == "executing" and proposal_outcomes.state(row) == "unknown" and execution.get("id"):
        try:
            db.finish_concierge_proposal(body.proposal_id, ctx.tenant_id, "unknown",
                                        result=proposal_outcomes.receipt(row, "unknown", {"summary": proposal_outcomes.UNKNOWN}),
                                        execution_id=execution["id"], client=ctx.client)
            row = _status_row(ctx, body.proposal_id)
        except Exception:
            log.warning("proposal reconciliation write unavailable (proposal=%s)", body.proposal_id)
    return _public_proposal(ctx, row)


def _save_proposal_outcome(ctx, row, current, result):
    """Durable receipt before decoration. Read back a lost write response."""
    from app import proposal_outcomes
    receipt = proposal_outcomes.receipt(row, current, result)
    execution_id = (receipt.get("execution") or {}).get("id")
    try:
        if db.finish_concierge_proposal(row["id"], ctx.tenant_id,
                                       "executed" if current == "completed" else current,
                                       result=receipt, execution_id=execution_id, client=ctx.client):
            return _public_proposal(ctx, {**row, "status": "executed" if current == "completed" else current, "result": receipt})
    except Exception:
        log.warning("proposal receipt write unavailable (proposal=%s)", row["id"])
    # A response can be lost after the receipt committed. Only its stored
    # positive receipt permits reporting completion; no executor is retried.
    try:
        existing = db.get_concierge_proposal(row["id"], ctx.tenant_id, client=ctx.client)
        if existing and proposal_outcomes.state(existing) in {"completed", "failed", "unknown"}:
            return _public_proposal(ctx, existing)
    except Exception:
        pass
    unknown = proposal_outcomes.receipt(row, "unknown", {**result, "summary": proposal_outcomes.UNKNOWN})
    try:
        db.finish_concierge_proposal(row["id"], ctx.tenant_id, "unknown", result=unknown,
                                     execution_id=execution_id, client=ctx.client)
    except Exception:
        pass  # persisted executing remains uncertain, never successful
    return _public_proposal(ctx, {**row, "status": "unknown", "result": unknown})


def _stamp_decision(ctx, proposal_id: str, via: str | None, *, by="user") -> None:
    """Sign-off record: who settled this card and how (best effort, never raises)."""
    try:
        db.stamp_concierge_decision(proposal_id, ctx.tenant_id, via or "unknown",
                                    by=ctx.user_id if by == "user" else None)
    except Exception:
        pass


@router.post(
    "/business/execute", response_model=BusinessExecuteResponse,
    dependencies=[Depends(rate_limit("concierge_business_execute", limit=20, window_s=600)),
                  Depends(require_csrf_header)],
)
async def business_execute(body: BusinessExecuteRequest,
                           ctx: TenantCtx = Depends(require_concierge)) -> BusinessExecuteResponse:
    """One atomic claim. Only a saved successful receipt means completion."""
    from app import proposal_outcomes
    import asyncio
    import time as _time
    from datetime import datetime, timezone
    t0 = _time.monotonic()
    refused = _not_their_proposal(ctx, body.proposal_id)
    if refused:
        return BusinessExecuteResponse(ok=False, state="unknown", summary="This proposal or its status is not available to you.", proposal_id=body.proposal_id)
    accepted_input = {"draft_text": body.draft_text, "selected": body.selected}
    # A delete is confirmed by the card's own button and nothing else: anything that is not
    # that tap is turned away BEFORE the claim, so the card stays waiting for the real one.
    only_by_tap = {} if body.confirm_via == "tap" else {"refuse_if": concierge_actions.row_needs_tap}
    try:
        row = db.claim_concierge_proposal(body.proposal_id, ctx.tenant_id,
                                         max_age_s=concierge_actions.PROPOSAL_TTL_S, client=ctx.client,
                                         execution_input=accepted_input, confirmed_by=ctx.user_id,
                                         confirm_via=body.confirm_via, **only_by_tap)
    except db.ClaimRefused as refused:
        try:
            trace.emit(ctx.tenant_id, "concierge_confirm", outcome="refused", user_id=ctx.user_id,
                       proposal_id=body.proposal_id,
                       detail={"kind": (refused.args[0] or {}).get("kind"), "reason": "tap_only"})
        except Exception:
            pass
        _remember_speech(ctx, concierge_actions.TAP_ONLY_SENTENCE)
        return BusinessExecuteResponse(ok=False, state="pending", proposal_id=body.proposal_id,
                                       summary=concierge_actions.TAP_ONLY_SENTENCE)
    except Exception:
        # The claim may have committed even if its response was lost.
        try:
            return await run_in_threadpool(business_status, body.proposal_id, ctx)
        except Exception:
            return BusinessExecuteResponse(ok=False, state="unknown", summary=proposal_outcomes.UNKNOWN, proposal_id=body.proposal_id)
    if not row:
        existing = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id, client=ctx.client)
        if existing and existing.get("status") == "pending":
            try:
                db.finish_concierge_proposal(body.proposal_id, ctx.tenant_id, "expired", client=ctx.client)
            except Exception:
                pass
            existing = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id, client=ctx.client)
            try:
                trace.emit(ctx.tenant_id, "concierge_expire", outcome="expired", user_id=ctx.user_id,
                           proposal_id=body.proposal_id, detail={"reason": "timeout", "kind": (existing or {}).get("kind")})
            except Exception:
                pass
        if (existing or {}).get("status") == "expired" and not existing.get("decided_at"):
            _stamp_decision(ctx, body.proposal_id, "system", by=None)   # also the delegated RPC's expiry
        if existing:
            if _proposal_refusal(ctx, existing):
                raise HTTPException(status_code=403, detail="This proposal isn't available to you.")
            try:
                trace.emit(ctx.tenant_id, "concierge_confirm", outcome="replayed", user_id=ctx.user_id,
                           proposal_id=body.proposal_id, detail={"kind": existing.get("kind")})
            except Exception:
                pass
        return _public_proposal(ctx, existing or {"id": body.proposal_id})
    _stamp_decision(ctx, body.proposal_id, body.confirm_via)
    execution = proposal_outcomes._dict(proposal_outcomes._dict(row.get("result")).get("execution"))
    if not proposal_outcomes._versioned(row, proposal_outcomes._dict(row.get("result"))):
        return _public_proposal(ctx, {**row, "status": "unknown"})
    refusal = _proposal_refusal(ctx, row)
    if refusal:
        return _save_proposal_outcome(ctx, row, "failed", {"summary": refusal})
    try:
        created = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        time_to_confirm_ms = int((datetime.now(timezone.utc) - created).total_seconds() * 1000)
    except (ValueError, TypeError):
        time_to_confirm_ms = None
    try:
        trace.emit(ctx.tenant_id, "concierge_confirm", outcome="confirmed", user_id=ctx.user_id,
                   proposal_id=body.proposal_id, detail={"kind": row["kind"], "proposed_by": row.get("user_id"),
                   "time_to_confirm_ms": time_to_confirm_ms, "draft_edited": bool(body.draft_text)})
    except Exception:
        pass
    intent = execution["input"]
    try:
        result = await concierge_actions.execute(ctx.tenant_id, ctx.client, row["kind"], row.get("payload") or {},
                  edited_draft_text=intent.get("draft_text") or None, selected=intent.get("selected"))
        if not isinstance(result, dict) or not result.get("summary"):
            raise RuntimeError("Executor returned no usable receipt")
        current = result.get("state") if result.get("state") in {"unknown", "failed"} else "completed"
    except (Exception, asyncio.CancelledError) as error:
        # Executor entry can already have changed data or sent externally.
        # ActionError is not uniformly pre-effect; don't claim nothing changed.
        result = {"summary": proposal_outcomes.UNKNOWN}
        current = "unknown"
        log.warning("proposal executor outcome uncertain (proposal=%s kind=%s)", row["id"], row["kind"])
    def _blocking():
        outcome = _save_proposal_outcome(ctx, row, current, result)
        # Everything below is decoration. Failure cannot erase a durable receipt.
        try:
            trace.emit(ctx.tenant_id, "concierge_execute", outcome="ok" if outcome.ok else "error",
                       user_id=ctx.user_id, proposal_id=body.proposal_id, latency_ms=(_time.monotonic() - t0) * 1000,
                       detail={"kind": row["kind"], "result_summary": outcome.summary, "state": outcome.state})
        except Exception:
            pass
        try:
            concierge_tasks.settle_card(ctx.tenant_id, body.proposal_id,
                                       "done" if outcome.ok else "needs_you", outcome.summary)
        except Exception:
            pass
        if outcome.ok:
            try:
                db.add_concierge_message(ctx.tenant_id, "assistant", outcome.summary, user_id=ctx.user_id,
                                         links=[outcome.link] if outcome.link else [], client=ctx.client, origin=body.display_origin)
            except Exception:
                log.warning("proposal history decoration unavailable (proposal=%s)", body.proposal_id)
        return outcome
    return await run_in_threadpool(_blocking)


class BusinessCancelRequest(BaseModel):
    display_origin: Literal["typed", "voice", "live", "unknown"] = "unknown"
    proposal_id: str = Field(..., min_length=1, max_length=64)


@router.post(
    "/business/cancel",
    dependencies=[Depends(rate_limit("concierge_business_cancel", limit=30, window_s=600)),
                  Depends(require_csrf_header)],
)
async def business_cancel(body: BusinessCancelRequest,
                          ctx: TenantCtx = Depends(require_concierge)) -> dict:
    """Close a pending proposal without executing it."""
    refused = _not_their_proposal(ctx, body.proposal_id)
    if refused:
        raise HTTPException(status_code=403, detail="This proposal isn't available to you.")
    from app import concierge_delegation
    linked_row = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id, client=ctx.client)
    linked = concierge_delegation.binding(linked_row or {})
    if linked:
        result = concierge_delegation.control(ctx, linked["task_id"], linked["revision"], "cancel")
        if result.get("ok"):
            _stamp_decision(ctx, body.proposal_id, "unknown")
            return {"ok": False, "state": "cancelled", "proposal_id": body.proposal_id,
                    "summary": "This proposal was cancelled before execution."}
        return (await run_in_threadpool(business_status, body.proposal_id, ctx)).model_dump()
    was_pending = db.cancel_concierge_proposal(body.proposal_id, ctx.tenant_id,
                                               client=ctx.client)
    if was_pending:
        _stamp_decision(ctx, body.proposal_id, "unknown")
        kind = None
        summary = ""
        try:   # best-effort decoration — the deny trace stands without it
            card = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id,
                                             client=ctx.client) or {}
            kind = card.get("kind")
            summary = str(card.get("summary") or "")
        except Exception:
            pass
        trace.emit(ctx.tenant_id, "concierge_deny", outcome="denied",
                   user_id=ctx.user_id, proposal_id=body.proposal_id,
                   detail={"reason": "user_denied", "kind": kind})
        concierge_tasks.settle_card(ctx.tenant_id, body.proposal_id, "declined",
                                    "You said no — nothing changed.")
        # The confirm path saves "Done — …" (above); a cancel saved nothing, so the
        # next conversation read the staged card as the change (live 2026-10-14:
        # "the quote we just sent" about a cancelled send). Decoration, best-effort.
        try:
            db.add_concierge_message(
                ctx.tenant_id, "assistant",
                f"Cancelled — nothing changed: {summary}" if summary else "Cancelled — nothing changed.",
                user_id=ctx.user_id, client=ctx.client, origin=body.display_origin)
        except Exception:
            log.warning("cancel history decoration unavailable (proposal=%s)", body.proposal_id)
    if was_pending:
        return {"ok": False, "state": "cancelled", "proposal_id": body.proposal_id,
                "summary": "This proposal was cancelled before execution."}
    return (await run_in_threadpool(business_status, body.proposal_id, ctx)).model_dump()


# ── Waiting cards (Kris 2026-09-23) ─────────────────────────────────────────
# "We should create a pending chip, for times like that when something gets
# ignored. It can be resurfaced at any time until confirmation." A card
# nobody answered (pushed aside by the next message, or left to expire) waits
# as a chip. Bringing it back REBUILDS it from the original request against
# today's data — a fresh check, a fresh live card (still one at a time) —
# never a replay of a stale one. "No" was an answer: cancelled cards don't wait.
@router.get("/business/waiting")
def business_waiting(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    asker = _live_asker(ctx)
    try:
        rows = db.list_waiting_proposals(
            ctx.tenant_id, ctx.user_id, client=ctx.client,
            max_age_s=concierge_actions.parked_lifetime_s(ctx.tenant_id, ctx.client))
    except Exception:
        log.warning("waiting cards read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        rows = []
    # Emails asked for by voice and still awaiting approval wait here too —
    # behind the Inbox door and in a mailbox the asker reads (M34).
    rows = rows + _drafts_they_may_see(
        ctx, asker, db.list_waiting_drafts(ctx.tenant_id, client=ctx.client))
    # …and extra-night invoices a stay change left as drafts: a tap opens them.
    rows = rows + _invoices_they_may_see(
        asker, db.list_waiting_invoices(ctx.tenant_id, client=ctx.client))
    recovery = []
    try:
        for row in db.list_recovering_proposals(ctx.tenant_id, ctx.user_id, client=ctx.client):
            if not _proposal_refusal(ctx, row):
                recovery.append({**_public_proposal(ctx, row).model_dump(), "type": "recovery",
                                 "created_at": row.get("created_at"),
                                 "action_summary": str(row.get("summary") or "")[:500],
                                 "kind": row.get("kind")})
    except Exception:
        log.warning("recovery list unavailable (tenant=%s)", ctx.tenant_id)
    return {"waiting": rows, "recovery": recovery}


# Kris 2026-09-24: "another counter chip, for done actions, so the user can
# see a list of what's happened. Not a whole thread of chat. Just done
# actions." Executed cards and sent voice emails from the last day, newest
# first — the waiting list's twin.
@router.get("/business/done")
def business_done(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    asker = _live_asker(ctx)
    try:
        rows = db.list_done_proposals(ctx.tenant_id, ctx.user_id, client=ctx.client)
    except Exception:
        log.warning("done cards read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        rows = []
    rows = rows + _drafts_they_may_see(
        ctx, asker, db.list_sent_drafts(ctx.tenant_id, client=ctx.client))
    rows.sort(key=lambda r: r.get("done_at") or "", reverse=True)
    return {"done": rows[:20]}


# The task lists on screen, as they stand (Kris 2026-09-25: "The list doesn't
# seem to update reliably, some stay on the timer, others get a tick"). The
# live stream is the fast path; a phone that drops it mid-list still catches
# up by asking here while any row is unfinished.
@router.get("/business/tasks")
def business_tasks(batch: str = "", ctx: TenantCtx = Depends(require_concierge)) -> dict:
    _live_asker(ctx)
    ids = [b for b in (x.strip() for x in batch.split(",")) if len(b) == 36][:10]
    try:
        rows = db.list_concierge_tasks(ctx.tenant_id, ids, client=ctx.client)
    except Exception:
        log.warning("task list read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        rows = []
    from app import concierge_delegation
    mine = [row for row in rows if row.get("user_id") == ctx.user_id]
    delegated = {row["id"]: row for row in concierge_delegation.scoped_public_tasks(
        ctx, [row for row in mine if row.get("revision", 0) > 0])}
    visible = []
    for row in mine:
        if row.get("revision", 0) > 0:
            if row["id"] in delegated:
                visible.append(delegated[row["id"]])
        else:
            visible.append({k: row.get(k) for k in ("id", "batch_id", "title", "status", "outcome", "proposal_id")})
    return {"tasks": visible}


def _text_allowance(ctx: TenantCtx, tenant: dict) -> dict:
    """The TEXT allowance (assistant questions per day, src/app/fairuse.py), never merged with the voice
    minutes. Counted from the same tenant columns the fair-use counter writes."""
    cap = fairuse.daily_cap(tenant)
    if cap is None:
        return {"unlimited": True}
    used = 0
    try:
        res = (ctx.client.table("tenants").select("concierge_daily_used,concierge_daily_date")
               .eq("id", ctx.tenant_id).limit(1).execute())
        row = (res.data or [None])[0] or {}
        if str(row.get("concierge_daily_date") or "") == fairuse.london_day():
            used = max(0, int(row.get("concierge_daily_used") or 0))
    except Exception:
        log.warning("text allowance read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return {"unavailable": True}
    return {"unlimited": False, "cap": cap, "used": used, "left": max(0, cap - used)}


# The full-screen page's status column (Kris 2026-10-17): the work ledger (what is open, what went
# through today) and the two allowances, in one read-only call. The ledger is the same one the
# Concierge's own list_awaiting_approval tool reads, so the column and the chat never disagree.
@router.get("/business/status")
def business_column_status(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    from app import concierge_ledger, parallel_reads
    asker = _live_asker(ctx)
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}

    def ledger_of(args: dict):
        try:
            return concierge_ledger.ledger(ctx.tenant_id, ctx.client, args, asker, ctx.user_id)
        except HTTPException:
            raise
        except Exception:
            log.warning("status column ledger unavailable (tenant=%s)", ctx.tenant_id, exc_info=True)
            return {"unavailable": True}

    def voice_of():
        try:
            enabled, limit, status = _live_allowance(ctx)
        except Exception:
            return {"unavailable": True}
        if not enabled:
            return {"enabled": False}
        return {"enabled": True, "unlimited": limit == 0, "limit_seconds": limit,
                "used_seconds": int(status.get("used_seconds") or 0),
                "remaining_seconds": status.get("remaining_seconds")}

    open_, today, voice = parallel_reads.run_all([
        lambda: ledger_of({"status": "open"}),
        lambda: ledger_of({"status": "recent", "since_days": 1}),
        voice_of,
    ])
    return {"open": open_, "today": today,
            "limits": {"text": _text_allowance(ctx, tenant), "voice": voice},
            "loop": settings.concierge_task_loop}


# The owner's wider day around the chat (follow-ups due, diary, inbox): the live debrief's own agenda,
# owner and managers only, read when the column's box is opened.
@router.get("/business/around")
def business_around(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    asker = _live_asker(ctx)
    if debrief_walk.refusal(ctx, asker):
        return {"groups": []}
    try:
        agenda = debrief_walk.gather_agenda(ctx.tenant_id, debrief_walk.now_local(), client=ctx.client)
    except Exception:
        log.warning("status column agenda unavailable (tenant=%s)", ctx.tenant_id, exc_info=True)
        return {"groups": [], "unavailable": True}
    return {"groups": [{"key": g["key"], "label": g["label"], "count": g["count"], "more": g["more"],
                        "items": [{"who": i["who"], "chip": i["chip"], "text": i["text"]} for i in g["items"]]}
                       for g in agenda["groups"]],
            "unread": agenda.get("unread") or []}


class TaskControlRequest(BaseModel):
    task_id: str = Field(..., min_length=36, max_length=36)
    revision: int = Field(..., ge=1)
    operation: Literal["revise", "cancel"]
    instruction: str = Field("", max_length=1000)


@router.post("/business/task-control", dependencies=[Depends(require_csrf_header)])
def business_task_control(body: TaskControlRequest, ctx: TenantCtx = Depends(require_concierge)):
    from app import concierge_delegation
    if not concierge_delegation.enabled() or getattr(ctx, "viewing_as", None):
        raise HTTPException(status_code=403, detail="Delegation isn't available.")
    result = concierge_delegation.control(ctx, body.task_id, body.revision, body.operation, body.instruction)
    if result.get("ok") and body.operation == "revise":
        concierge_tasks.launch(ctx.tenant_id, ctx.user_id, result["task"]["batch_id"])
    return result


@router.get("/business/session-tasks/{voice_session}")
async def business_session_tasks(voice_session: str, ctx: TenantCtx = Depends(require_concierge)):
    import asyncio
    return await asyncio.to_thread(session_task_snapshot, voice_session, ctx)


def session_task_snapshot(voice_session: str, ctx: TenantCtx):
    from app import concierge_delegation
    if not concierge_delegation.enabled():
        return {"tasks": []}
    _live_asker(ctx)
    try:
        import uuid
        voice_session = str(uuid.UUID(voice_session))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session.")
    rows = db.list_session_concierge_tasks(ctx.tenant_id, ctx.user_id, voice_session, client=ctx.client)
    return {"tasks": concierge_delegation.scoped_public_tasks(ctx, rows)}


class TaskDeliveryRequest(BaseModel):
    task_id: str = Field(..., min_length=36, max_length=36)
    revision: int = Field(..., ge=1)
    event_id: str = Field(..., min_length=36, max_length=36)
    voice_session: str = Field(..., min_length=36, max_length=36)
    delivery_id: str = Field(..., min_length=36, max_length=36)
    operation: Literal["claim", "start", "ack", "release"]
    outcome: Literal["completed", "interrupted"] = "interrupted"


@router.post("/business/task-delivery", dependencies=[Depends(require_csrf_header)])
def business_task_delivery(body: TaskDeliveryRequest, ctx: TenantCtx = Depends(require_concierge)):
    from app import concierge_delegation
    if not concierge_delegation.enabled() or getattr(ctx, "viewing_as", None):
        raise HTTPException(status_code=403, detail="Delegation isn't available.")
    row = concierge_delegation.scoped_task(ctx, body.task_id)
    if not row or row.get("voice_session") != body.voice_session:
        return {"ok": False, "state": "unavailable"}
    result = concierge_delegation.transition("delivery_" + body.operation, ctx.tenant_id, ctx.user_id,
        task_id=body.task_id, revision=body.revision, data=body.model_dump())
    current = result.get("task")
    if current:
        result["task"] = concierge_delegation.public(current)
        if result.get("ok") and body.operation in {"claim", "start"}:
            _remember_speech(ctx, result["task"]["outcome"])
    return result


class BusinessWaitingRequest(BaseModel):
    proposal_id: str = Field(..., min_length=1, max_length=64)


def _row_created(row):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@router.post(
    "/business/resurface",
    dependencies=[Depends(rate_limit("concierge_business_resurface", limit=30, window_s=600)),
                  Depends(require_csrf_header)],
)
def business_resurface(body: BusinessWaitingRequest,
                             ctx: TenantCtx = Depends(require_concierge)) -> dict:
    row = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id, client=ctx.client)
    from app import concierge_delegation
    if row and concierge_delegation.binding(row):
        if not concierge_delegation.enabled() or getattr(ctx, "viewing_as", None) or row.get("user_id") != ctx.user_id or _proposal_refusal(ctx, row):
            return {"ok": False, "error": "This request isn't available."}
        result = concierge_delegation.transition("resurface", ctx.tenant_id, ctx.user_id,
                                                 data={"proposal_id": row["id"]})
        if not result.get("ok"):
            return {"ok": False, "error": "This request isn't waiting any more."}
        saved = result["proposal"]
        payload = saved.get("payload") or {}
        return {"ok": True, "proposal": {"proposal_id": saved["id"], "kind": saved["kind"],
            "summary": saved["summary"], **payload.get("_prepared_card", {}),
            "expires_s": concierge_actions.PROPOSAL_TTL_S,
            "approval": concierge_actions.approval_mode(saved["kind"]),
            **concierge_actions.tap_flag(saved["kind"], payload.get("items"))}}
    action = ((row or {}).get("payload") or {}).get("_action")
    spoken_ms = ((row or {}).get("payload") or {}).get("_spoken_ms") or 0
    if not row or row.get("status") not in ("pending", "superseded") or not action:
        return {"ok": False, "error": "That one isn't waiting any more."}
    if ctx.user_id and row.get("user_id") and row.get("user_id") != ctx.user_id:
        return {"ok": False, "error": "That one isn't waiting any more."}
    from datetime import datetime, timezone
    # How long it may wait is the owner's task-expiry setting; what may come back depends on
    # the kind (concierge_actions.reprepare_class). Neither revives the old card: a NEW one is
    # prepared from today's records below.
    kind_class = concierge_actions.reprepare_class(row.get("kind"), row.get("payload"))
    created = _row_created(row)
    life = concierge_actions.parked_lifetime_s(ctx.tenant_id, ctx.client)
    if created is not None and (datetime.now(timezone.utc) - created).total_seconds() > life:
        if db.set_concierge_proposal_status(body.proposal_id, ctx.tenant_id, "expired",
                                            from_statuses=("pending", "superseded"), client=ctx.client):
            _stamp_decision(ctx, body.proposal_id, "system", by=None)
            concierge_tasks.settle_card(ctx.tenant_id, body.proposal_id, "failed",
                                        "It expired before it was confirmed. Nothing was done.")
        hours = max(1, life // 3600)
        return {"ok": False, "error": f"That one has expired (kept for {hours} hour{'s' if hours != 1 else ''}). "
                                      "Nothing was done. Ask me to prepare it again."}
    if kind_class == "never":
        return {"ok": False, "error": concierge_actions.NEVER_SENTENCE}
    # Reserve before rebuilding. A concurrent confirmation must not leave a
    # second pending card for the same action after the original was claimed.
    if not db.set_concierge_proposal_status(body.proposal_id, ctx.tenant_id, "resurfaced",
                                            from_statuses=("pending", "superseded"), client=ctx.client):
        return {"ok": False, "error": "That one isn't waiting any more."}
    modules = db.visible_modules(ctx.tenant_id, client=ctx.client)
    try:
        proposal = concierge_actions.stage(
            ctx.tenant_id, ctx.client, action,
            full_qa="crm" in modules, scheduling_on="scheduling" in modules,
            user_id=ctx.user_id, member=_asker(ctx), spoken_ms=spoken_ms)
    except concierge_actions.ActionError as e:
        # Today's data says no (already done, the day is full, the record's
        # gone): say why; the chip goes, since it can't be done as asked.
        db.set_concierge_proposal_status(body.proposal_id, ctx.tenant_id, "stale",
                                         from_statuses=("resurfaced",), client=ctx.client)
        concierge_tasks.settle_card(ctx.tenant_id, body.proposal_id, "failed", f"Can't be done now: {e}")
        return {"ok": False, "error": f"That can't be done now: {e}"}
    concierge_tasks.follow_card(ctx.tenant_id, body.proposal_id, proposal["proposal_id"])
    db.link_reprepared(proposal["proposal_id"], body.proposal_id, ctx.tenant_id)
    proposal = {**proposal, "reprepared_from": body.proposal_id,
                "notice": (concierge_actions.REPREPARED_SILENT if kind_class == "silent"
                           else concierge_actions.REPREPARED_REVISE)}
    trace.emit(ctx.tenant_id, "concierge_propose", user_id=ctx.user_id,
               proposal_id=proposal["proposal_id"],
               detail={"kind": proposal["kind"], "summary": proposal["summary"],
                       "resurfaced_from": body.proposal_id, "reprepared": kind_class})
    return {"ok": True, "proposal": proposal}


@router.post(
    "/business/dismiss",
    dependencies=[Depends(rate_limit("concierge_business_dismiss", limit=60, window_s=600)),
                  Depends(require_csrf_header)],
)
def business_dismiss(body: BusinessWaitingRequest,
                           ctx: TenantCtx = Depends(require_concierge)) -> dict:
    if _not_their_proposal(ctx, body.proposal_id):
        return {"ok": False}
    from app import concierge_delegation
    saved = db.get_concierge_proposal(body.proposal_id, ctx.tenant_id, client=ctx.client)
    linked = concierge_delegation.binding(saved or {})
    if linked:
        out = concierge_delegation.control(ctx, linked["task_id"], linked["revision"], "cancel")
        if (out or {}).get("ok"):
            _stamp_decision(ctx, body.proposal_id, "unknown")
        return out
    if not db.set_concierge_proposal_status(body.proposal_id, ctx.tenant_id, "dismissed", client=ctx.client):
        return {"ok": False}
    _stamp_decision(ctx, body.proposal_id, "unknown")
    concierge_tasks.settle_card(ctx.tenant_id, body.proposal_id, "declined", "Binned — nothing changed.")
    return {"ok": True}


# ── Live voice conversation (docs/voice-live-seed.md) ────────────────────────
# Three endpoints over the SAME business brain — the typed concierge is
# untouched. Audio is the one job Anthropic's API doesn't do, so STT/TTS go to
# OpenAI (settings.openai_api_key — the same boundary src/app/voice_crm.py draws):
#   voice/token — mint an ephemeral OpenAI Realtime key (browser connects the
#                 WebSocket itself; the real key never leaves the server)
#   voice/chat  — one answer_business turn streamed as SSE; the spoken filter
#                 (src/app/voice_spoken.py) extracts the `reply` field so TTS never
#                 reads JSON
#   voice/tts   — proxy one sentence to OpenAI TTS as streamed 24kHz PCM
# All three: tenant-authed + CSRF, and voice/chat carries the exact trial +
# fair-use gates of /business — same brain, same meter.

_TTS_MODEL = "gpt-4o-mini-tts"
_TTS_VOICES = {"marin", "cedar", "alloy", "sage", "coral"}
_OPENAI_REALTIME_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
_OPENAI_SPEECH = "https://api.openai.com/v1/audio/speech"


class VoiceTokenRequest(BaseModel):
    silence_ms: int = Field(500, ge=0, le=5000)
    purpose: str = Field("", max_length=20)
    # Which of OpenAI's noise filters: "near" for a phone held to the face,
    # "far" for a laptop across the desk (docs/…/Voice agent turn taking
    # practices: clean the audio BEFORE the speech decision, not after it).
    noise: str = Field("", max_length=8)


def _live_allowance(ctx: TenantCtx) -> tuple[bool, int, dict]:
    enabled = db.concierge_live_rollout()
    if not enabled:
        return False, 0, {"used_seconds": 0, "remaining_seconds": None}
    if not ctx.user_id:
        raise HTTPException(status_code=401, detail="Sign in to use live voice.")
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    limit = fairuse.live_limit_seconds(tenant)
    status = db.concierge_live_status(ctx.tenant_id, ctx.user_id, fairuse.london_day(), limit)
    return enabled, limit, status


@router.post("/voice/allowance", dependencies=[Depends(require_csrf_header)])
async def voice_allowance(ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    try:
        enabled, limit, status = await run_in_threadpool(_live_allowance, ctx)
    except Exception:
        log.exception("live voice allowance status unavailable (tenant=%s)", ctx.tenant_id)
        return JSONResponse({"error": "Live voice allowance is temporarily unavailable."}, status_code=503)
    return JSONResponse({"ok": True, "enabled": enabled, **status,
                         "limit_seconds": limit, "unlimited": limit == 0})


_BARE_ANSWER_WORDS = frozenset(
    "yes yeah yep yup no nope nah ok okay sure please go ahead do it thanks thank you "
    "cheers fine right correct that's thats".split())


def _bare_answer(text: str) -> bool:
    """A quick yes/no is said the instant the question ends, so it nearly
    always trips the "started speaking while you were talking" flag — and the
    model then doubts it ("did you say yes?", live 2026-09-23). A reply made
    only of answer words has nothing to lose to a clipped start."""
    import re
    words = re.sub(r"[^a-z' ]", " ", str(text or "").lower()).split()
    return 0 < len(words) <= 4 and all(w in _BARE_ANSWER_WORDS for w in words)


def _found_in(result, limit: int = 3) -> str:
    """The identifiers a lookup returned, briefly: customers as "Name <email>"
    and booking references - what a follow-up action needs, so the model
    doesn't have to search for them again."""
    import re as _re
    people: list[str] = []
    refs: list[str] = []

    def walk(node, depth=0):
        if depth > 5:
            return
        if isinstance(node, dict):
            email = node.get("email") or node.get("contact")
            if isinstance(email, str) and "@" in email and len(people) < limit:
                label = f"{node.get('name') or node.get('contact_name') or ''} <{email}>".strip()
                if label not in people:
                    people.append(label)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:20]:
                walk(v, depth + 1)
        elif isinstance(node, str):
            for r in _re.findall(r"\bBK-\d{3,6}\b", node):
                if r not in refs and len(refs) < limit:
                    refs.append(r)
    walk(result)
    return "; ".join(people + refs)[:200]


def _people_note(ctx: TenantCtx, asker: dict | None, full_qa: bool,
                 scheduling_on: bool) -> str | None:
    """Who's asking and what they may do (concierge_actions.asker_note), and
    the team (team_note) — per turn, so the cached prompt stays shared."""
    notes = [concierge_actions.asker_note(asker, full_qa=full_qa, scheduling_on=scheduling_on)]
    if scheduling_on:
        try:
            notes.append(concierge_actions.team_note(ctx.tenant_id))
        except Exception:
            log.warning("team note failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    return "\n".join(n for n in notes if n) or None


def _voice_note(ctx: TenantCtx, clipped: bool, member: dict | None = None, *,
                live: bool = False) -> str | None:
    """What the voice Concierge should know before it looks anything up: the
    drafts already waiting for the owner's approval, with the address
    show_draft needs. Kris 2026-09-24 ("it took a very long time Thinking"):
    "send that email to Laura" cost a contact search AND a draft lookup —
    two model rounds (~6 s) — to find a draft the screen already knew about.
    Plus the clipped-start note when the opening words may be missing.
    `member`: the asker — the drafts go through their Inbox door and mailbox
    lens (M34); None is the account's own owner.
    `live`: the turn comes from a GPT-Live session, where only a tap on the card
    confirms (the debrief note says the same); Standard voice also accepts a
    spoken yes, so this is per turn and never part of the cached prompt."""
    parts: list[str] = []
    if live:
        parts.append("[GPT-Live: a spoken yes does nothing here — only a tap on the Confirm card runs a "
                     "change or sends a draft. When you propose or show one, say it is ready and to tap "
                     "Confirm; never ask 'Shall I?', 'Want me to?' or 'Shall I send it?'.]")
    if clipped:
        parts.append(concierge.CLIPPED_NOTE)
    try:
        drafts = _drafts_they_may_see(
            ctx, member, db.list_waiting_drafts(ctx.tenant_id, client=ctx.client))[:5]
    except Exception:
        drafts = []
    if drafts:
        lines = "; ".join(f"{d.get('name') or d.get('contact')} <{d.get('contact')}>"
                          + (f" — \"{d.get('subject')}\"" if d.get("subject") else "")
                          for d in drafts)
        # Only for "send / show that email": run 2 of test 3.1 (24 Sep) asked
        # to extend a stay and got the waiting stay note shown instead — and
        # its "yes" SENT that note. Everything else ignores this list.
        parts.append("[for reference only — emails already drafted and waiting for the "
                     f"owner's approval: {lines}. ONLY if the owner asks to send, see or "
                     "check one of these, call show_draft with that address (no lookup "
                     "needed). For anything else — a change, a question, a new task — "
                     "ignore this list and do what they asked]")
    return "\n".join(parts) or None


def _transcription_hint(ctx: TenantCtx, member: dict | None = None) -> str | None:
    """Context for the speech-to-text model: what the business is and the
    names it will hear. Live 2026-09-23: with only language="en", a clipped
    "…vaccination…" came back as "psychosis on my mind" — with no idea this
    is a dog business with a customer called Dan, the model picks whatever
    common phrase sounds closest. Best-effort: any read failing just shortens
    the hint. Never shown to anyone; it only steers the transcription.

    It goes to OpenAI, so it carries only what the asker may read (security
    review 2026-09, M37): customer and dependant names only behind an open
    CRM door onto the whole book — never for a person narrowed to their own
    customers. Names only, never a field's value. `member` None = the
    account's own owner."""
    from app import crm_visibility, people as _people
    names_ok = member is None or (
        member.get("active", True) and _people.can_open(member, "crm")
        and not crm_visibility.narrows_contacts(member))
    parts: list[str] = []
    try:
        tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        tenant = {}
    trade = str(tenant.get("industry") or "").replace("_", " ").strip()
    biz = str(tenant.get("name") or "").strip()
    parts.append("A UK small-business owner" + (f" ({trade})" if trade else "")
                 + " talking to their assistant" + (f" at {biz}" if biz else "")
                 + " about customers, bookings, the diary, invoices, quotes and emails."
                 # A short "Yes." came back as "Jess." (a customer) on 24 Sep:
                 # the names below pull short words towards them.
                 + " They often answer with a single word: yes or no.")
    names: list[str] = []
    labels: list[str] = []
    # The team first: a short name like "Em" is otherwise heard as the letter
    # M (Kris 2026-09-24, "delegate the next meet & greet to Em").
    try:
        from app import job_people
        team = [t["name"] for t in job_people.team(ctx.tenant_id)]
        if len(team) > 1:
            parts.append("The people who work there: " + ", ".join(team) + ".")
    except Exception:
        log.warning("voice hint: team read failed", exc_info=True)
    try:
        rows = ((ctx.client or db.db()).table("crm_contacts").select("name")
                .eq("tenant_id", ctx.tenant_id)
                .order("last_activity_at", desc=True).limit(60).execute().data or []
                ) if names_ok else []
        names += [str(r.get("name") or "").strip() for r in rows]
    except Exception:
        log.warning("voice hint: contacts read failed", exc_info=True)
    try:
        for r in (db.list_requirement_instances_all(ctx.tenant_id, limit=120, client=ctx.client)
                  if names_ok else []):
            names.append(str(r.get("name") or "").strip())
            labels.append(str(r.get("group_label_plural") or r.get("group_label") or "").strip())
    except Exception:
        log.warning("voice hint: dependants read failed", exc_info=True)
    seen: set[str] = set()
    uniq = [n for n in names if n and "@" not in n and not (n.lower() in seen or seen.add(n.lower()))]
    kinds = sorted({l.lower() for l in labels if l})
    if kinds:
        parts.append("They keep records of: " + ", ".join(kinds) + ".")
    if uniq:
        parts.append("Names that come up: " + ", ".join(uniq) + ".")
    return " ".join(parts)[:1200] or None


async def _mint_realtime_token(captions: bool, silence_ms: int,
                               hint: str | None = None, noise: str = "") -> JSONResponse:
    """Mint an ephemeral OpenAI Realtime key for streaming STT — shared by the
    authed route and the rig. Default: a gpt-4o-transcribe TRANSCRIPTION
    session with server VAD; the threshold stays 0.5 with 600ms prefix padding
    (OurTab field lesson: raising the threshold to fix noise eats quiet first
    syllables; noise is solved client-side). captions=True mints a DISPLAY-ONLY
    gpt-live-transcribe session instead: it rejects turn_detection, emits
    word-by-word deltas but no speech events, and must never drive turns."""
    import httpx
    from app import speech
    if speech.local():
        if captions:
            return JSONResponse({"ok": True, "provider": "local", "captions": "same_session"})
        try:
            return JSONResponse(await speech.mint_token(silence_ms, hint))
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"error": "Couldn't reach the local listening service."}, status_code=503)
    if not settings.openai_api_key:
        return JSONResponse({"error": "Live voice isn't configured yet "
                                      "(OPENAI_API_KEY missing)."}, status_code=503)
    input_cfg: dict = {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": "gpt-live-transcribe" if captions
                          else "gpt-4o-transcribe", "language": "en"},
    }
    if not captions:
        input_cfg["turn_detection"] = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 600,
            "silence_duration_ms": min(1200, max(200, silence_ms or 500)),
        }
        if hint:
            input_cfg["transcription"]["prompt"] = hint
        # OpenAI's own noise filter runs before its speech detector — the one
        # place noise should be removed (2026-09-24 research: every stack
        # researched filters upstream; our loudness gates after the
        # transcript were dropping real words).
        if noise in ("near", "far"):
            input_cfg["noise_reduction"] = {"type": f"{noise}_field"}

    async def _post(cfg: dict):
        async with httpx.AsyncClient(timeout=15) as cl:
            return await cl.post(
                _OPENAI_REALTIME_SECRETS,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"expires_after": {"anchor": "created_at", "seconds": 600},
                      "session": {"type": "transcription",
                                  "audio": {"input": cfg}}})
    try:
        r = await _post(input_cfg)
        if r.status_code == 400 and "prompt" in input_cfg["transcription"]:
            # The hint is an improvement, never a dependency: if it is ever
            # refused, listen without it rather than not at all.
            log.warning("voice token: hint refused (%s), retrying without", r.text[:200])
            input_cfg["transcription"].pop("prompt", None)
            r = await _post(input_cfg)
        if r.status_code == 400 and "noise_reduction" in input_cfg:
            # Same rule for the noise filter: better without it than deaf.
            log.warning("voice token: noise_reduction refused (%s), retrying without", r.text[:200])
            input_cfg.pop("noise_reduction", None)
            r = await _post(input_cfg)
    except httpx.HTTPError as exc:
        log.warning("voice token: %s", exc.__class__.__name__)
        return JSONResponse({"error": "Couldn't start a listening session."},
                            status_code=502)
    if r.status_code != 200:
        log.warning("voice token: openai returned %s: %s", r.status_code, r.text[:400])
        return JSONResponse({"error": "Couldn't start a listening session."},
                            status_code=502)
    data = r.json()
    return JSONResponse({"ok": True, "value": data.get("value"),
                         "expires_at": data.get("expires_at")})


@router.post(
    "/voice/token",
    dependencies=[Depends(rate_limit("concierge_voice_token", limit=10, window_s=60)),
                  Depends(require_csrf_header)],
)
async def voice_token(body: VoiceTokenRequest,
                      ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    captions = body.purpose == "captions"
    asker = _live_asker(ctx)
    return await _mint_realtime_token(captions, body.silence_ms,
                                      None if captions else _transcription_hint(ctx, asker),
                                      noise="" if captions else body.noise)


class VoiceChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    history: list[Turn] = Field(default_factory=list)
    # The client heard this start while its mic was gated for playback — the
    # opening words are likely missing (voice-live.js, CLIP_WINDOW_MS).
    clipped: bool = False
    # The client's id for this turn, so tapping the heard words can strike it.
    turn_id: str = Field("", max_length=40)
    voice_session: str = Field("", max_length=36)
    # How long the owner spoke since the last card/draft (voice-live.js) —
    # human time that time saved takes off what that card or draft saves.
    spoken_ms: int = 0
    # The Debrief button (src/app/debrief_walk.py): "start" builds today's agenda and
    # begins the walk; "next" moves it on without waiting to be asked in words.
    debrief: Literal["", "start", "next"] = ""


# Cancellation is scoped to the authenticated tenant AND user. State is local
# to this process; deployment must remain single-worker (like _SPOKEN).
from app import voice_turn_state
_VOICE_RUNNERS: set = set()
_DISCARDED = voice_turn_state._states  # compatibility for test cleanup


def _discarded(tenant_id: str, turn_id: str, user_id=None) -> bool:
    state = _DISCARDED.get(voice_turn_state.key(tenant_id, user_id, turn_id))
    return bool(state and state.status == "cancelled")


class VoiceHeardRequest(BaseModel):
    outcome: str = Field(..., min_length=1, max_length=24)
    reason: str = Field("", max_length=40)
    text: str = Field("", max_length=300)
    peak: float = 0
    gate: float = 0
    segments: int = 0
    salvaged: bool = False
    clipped: bool = False
    duration_ms: int = 0


# Every utterance the voice screen heard, and what became of it (sent, an
# answer to the card, held as too quiet, held while thinking, nothing heard,
# a late transcript ignored). Kris 2026-09-24: "it doesn't accept this
# transcript? Just continues to say Listening" — and nothing recorded which
# of four paths had eaten it. Now every outcome is a trace row.
@router.post(
    "/voice/heard",
    dependencies=[Depends(rate_limit("concierge_voice_heard", limit=120, window_s=60)),
                  Depends(require_csrf_header)],
)
def voice_heard(body: VoiceHeardRequest,
                      ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    trace.emit(ctx.tenant_id, "voice_heard", user_id=ctx.user_id,
               outcome=body.outcome,
               detail={"reason": body.reason, "text": body.text,
                       "peak": round(body.peak, 4), "gate": round(body.gate, 4),
                       "segments": body.segments, "salvaged": body.salvaged,
                       "clipped": body.clipped, "duration_ms": body.duration_ms})
    return JSONResponse({"ok": True})


# "Context" (Kris 2026-09-24: "shows exactly what the Agent will see"): the
# note the server adds to the next voice question — the drafts waiting. The
# conversation turns themselves are the ones the phone sends.
@router.get("/voice/context")
def voice_context(ctx: TenantCtx = Depends(require_concierge)) -> dict:
    return {"note": _voice_note(ctx, False, member=_live_asker(ctx)) or ""}


class VoiceLocalRequest(BaseModel):
    display_turn_id: str = Field("", max_length=40)
    voice_session: str = Field("", max_length=36)
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    answer: str = Field(..., min_length=1, max_length=400)


# An exchange the phone answered by itself — "yes" sending the draft on
# screen, "no" leaving it, "thank you" → "You're welcome." Kris 2026-09-24
# (phrase 1.2): those never reached the server, so after a reload the saved
# history still ended "…Shall I send it?", and the next thing said was taken
# as carrying on with that email — it drafted Daniel's again. Saved now, so
# the history the model sees is what actually happened.
@router.post(
    "/voice/local",
    dependencies=[Depends(rate_limit("concierge_voice_local", limit=60, window_s=60)),
                  Depends(require_csrf_header)],
)
def voice_local(body: VoiceLocalRequest,
                      ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    try:
        mode_visit_id = str(uuid.UUID(body.voice_session)) if body.voice_session else ""
    except ValueError:
        mode_visit_id = ""
    native_events = {"mode_visit_id": mode_visit_id} if mode_visit_id else {}
    db.add_concierge_message(ctx.tenant_id, "user", body.question, user_id=ctx.user_id,
                             client=ctx.client, origin="voice", display_turn_id=body.display_turn_id,
                             native_events=native_events)
    db.add_concierge_message(ctx.tenant_id, "assistant", body.answer, user_id=ctx.user_id,
                             client=ctx.client, origin="voice", display_turn_id=body.display_turn_id,
                             native_events=native_events)
    return JSONResponse({"ok": True})


class VoiceDiscardRequest(BaseModel):
    turn_id: str = Field(..., min_length=1, max_length=40)


@router.post(
    "/voice/discard",
    dependencies=[Depends(rate_limit("concierge_voice_discard", limit=30, window_s=60)),
                  Depends(require_csrf_header)],
)
def voice_discard(body: VoiceDiscardRequest,
                        ctx: TenantCtx = Depends(require_concierge)) -> JSONResponse:
    """Accept cancellation only before a write helper/finalization begins."""
    try:
        outcome = voice_turn_state.cancel(
            voice_turn_state.key(ctx.tenant_id, ctx.user_id, body.turn_id))
    except RuntimeError:
        return JSONResponse({"ok": False, "status": "unknown"}, status_code=503)
    trace.emit(ctx.tenant_id, "voice_cancel", user_id=ctx.user_id,
               outcome=outcome["status"], detail={"turn_id": body.turn_id,
                                               "boundary": outcome.get("boundary", "")})
    return JSONResponse(outcome)



def _said_402(ctx, error: str) -> JSONResponse:
    """A cap, as the voice client speaks its first sentence (recorded for TTS)."""
    _remember_speech(ctx, error)
    return JSONResponse({"error": error}, status_code=402)


@router.post(
    "/voice/chat",
    dependencies=[
        # Voice turns arrive faster than typed ones (the utterance merger can
        # fire several in a minute); the DAILY fair-use cap below is the real
        # meter, these just stop runaway loops.
        Depends(rate_limit("concierge_voice_chat_burst", limit=20, window_s=60)),
        Depends(rate_limit("concierge_voice_chat", limit=200, window_s=3600)),
        Depends(require_csrf_header),
    ],
)
async def voice_chat(body: VoiceChatRequest,
                     ctx: TenantCtx = Depends(require_concierge)) -> StreamingResponse:
    """One spoken business-concierge turn, streamed as SSE:
      data: {"t":"speak","text":...}   — speakable text (the `reply` field only,
                                          extracted by src/app/voice_spoken.py)
      data: {"t":"tool","name","hint","label","detail"} — a lookup just started
      data: {"t":"error","error":...}
      data: {"t":"done","reply","links","proposal","trail"} — always last
    Same brain, same gates, same staging and persistence as POST /business —
    the shared finish_business_turn keeps the two surfaces from drifting."""
    import asyncio
    import json

    from app import voice_spoken
    import uuid
    turn_id = body.turn_id or uuid.uuid4().hex
    identity = voice_turn_state.key(ctx.tenant_id, ctx.user_id, turn_id)


    question = body.question.strip()[:MAX_QUESTION_CHARS]
    from app import gptlive
    origin = "live" if any(session.voice_session == body.voice_session and session.tenant == ctx.tenant_id
                           and session.user == ctx.user_id and not session.closed
                           for session in gptlive._sessions.values()) else "voice"

    native_events = dict(gptlive._history_events.get() or {}) if origin == "live" else {}
    try:
        mode_visit_id = str(uuid.UUID(body.voice_session)) if body.voice_session else ""
    except ValueError:
        mode_visit_id = ""
    if mode_visit_id:
        native_events["mode_visit_id"] = mode_visit_id
    history = concierge.sanitize_history(
        [t.model_dump() for t in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)
    spoken_ms = max(0, min(int(body.spoken_ms or 0), 600_000))   # at most 10 min

    modules = await asyncio.to_thread(db.visible_modules, ctx.tenant_id, client=ctx.client)
    full_qa = "crm" in modules
    scheduling_on = "scheduling" in modules
    try:
        tenant = await asyncio.to_thread(db.get_tenant, ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        tenant = {}
    # The exact gate stack of /business. Caps come back as a 402 JSON body —
    # the voice client speaks a short version and shows the full text.
    if trial.concierge_blocked(tenant):
        return _said_402(ctx,
            "You've used the assistant questions included in the free trial. "
            "Any paid plan removes this limit — upgrade under Settings → "
            "Billing.")
    if settings.require_subscription_gate and trial.on_trial(tenant):
        await asyncio.to_thread(trial.consume_concierge, ctx.tenant_id)
    allowed, day_cap = await asyncio.to_thread(fairuse.consume, ctx.tenant_id, tenant)
    if not allowed:
        return _said_402(ctx, fairuse.capped_reply(tenant, day_cap))

    voice = None
    try:
        tcfg = await asyncio.to_thread(db.get_tenant_config, ctx.tenant_id, client=ctx.client) or {}
        voice = "\n".join(f"{k}: {v}" for k, v in (
            ("Signs off as", tcfg.get("sign_off")),
            ("Tone", tcfg.get("tone")),
            ("Agent name", tcfg.get("agent_name")),
            ("Style notes", (tcfg.get("voice_notes") or "")[:300]),
        ) if v) or None
    except Exception:
        pass
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)")

    # People slice 4: the assistant answers within THIS person's doors — the
    # catalogue is narrowed and the executor refuses, because hiding a tool
    # from the model is not the same as refusing it.
    asker = await asyncio.to_thread(_asker, ctx)

    # The live debrief (src/app/debrief_walk.py). The Debrief button sends "start"
    # (read today fresh, in the owner's Today order, and begin) or "next"; while
    # a walk is on, every turn of THIS conversation carries its note and may call
    # debrief_next. Owner and managers only — the walk crosses every door.
    walk = debrief_walk.current(ctx.tenant_id, ctx.user_id, body.voice_session)
    refused = debrief_walk.refusal(ctx, asker)
    if refused:
        # An existing walk carries account-wide data too, even on an ordinary turn.
        walk = None
        if body.debrief:
            return JSONResponse({"error": refused}, status_code=403)
    agenda = None
    if body.debrief and (body.debrief == "start" or walk is None or walk.finished):
        # Read before admission so an unavailable agenda still returns HTTP 503.
        # Installing/replacing/advancing the walk waits for the admitted checkpoint.
        try:
            agenda = await asyncio.to_thread(debrief_walk.gather_agenda, ctx.tenant_id,
                                             debrief_walk.now_local(), client=ctx.client)
        except Exception:
            log.exception("debrief agenda failed (tenant=%s)", ctx.tenant_id)
            return JSONResponse({"error": "I couldn't put the debrief together just now."},
                                status_code=503)

    try:
        turn_state = voice_turn_state.begin(identity)
    except RuntimeError:
        return JSONResponse({"error": "Voice is busy; try again shortly."}, status_code=503)

    # Drafting by voice (src/app/concierge_draft.py): the Concierge asks, the
    # Drafting agent writes, the card for the screen rides the done frame.
    from app import concierge_draft
    drafted: dict = {}
    # A task list by voice too (docs/plan-concierge-task-list.md).
    starter = concierge_tasks.Starter(ctx, surface="voice", source_turn=turn_id,
                                      voice_session=body.voice_session or None, question=question)

    async def _executor(name: str, args: dict) -> dict:
        if turn_state.status == "cancelled":
            return {"error": "cancelled", "_final_reply": "Turn cancelled."}
        if name in {concierge_tasks.TOOL_NAME, concierge_tasks.CONTROL_NAME, concierge_draft.TOOL_NAME, concierge_draft.SHOW_TOOL_NAME}:
            # These helpers write internally, sometimes after awaiting a model.
            # Once entered, a discard must truthfully report too_late.
            voice_turn_state.commit(turn_state, name)
        if name == concierge_tasks.TOOL_NAME:
            return await starter.start(args)
        if name == concierge_tasks.CONTROL_NAME:
            return await starter.control(args)
        if name == debrief_walk.TOOL_NAME:
            return debrief_walk.run_tool(walk if walk is not None and not walk.finished else None, args)
        if name == concierge_draft.TOOL_NAME and getattr(ctx, "viewing_as", None):
            # View as: drafting writes a real email into their thread — not here.
            return {"error": "view_as",
                    "message": "The owner is viewing the app as this person: say in one "
                               "sentence you'd draft that email for them, and that nothing "
                               "is drafted while they're viewing."}
        if name == concierge_draft.TOOL_NAME:
            try:
                result, card = await concierge_draft.draft(
                    ctx, args, member=asker, spoken_ms=spoken_ms,
                    known_addresses=drafted.setdefault("seen", set()))
            except Exception:
                log.exception("voice draft_email failed (tenant %s)", ctx.tenant_id)
                return {"error": "draft_failed",
                        "message": "The draft didn't come through — say so; "
                                   "nothing was sent."}
            if card:
                drafted["card"] = card
            if isinstance(result, dict) and result.get("confirm_action"):
                drafted["confirm_action"] = result.pop("confirm_action")
            return result
        if name == concierge_draft.SHOW_TOOL_NAME:
            try:
                result, card = await concierge_draft.show(ctx, args, member=asker)
            except Exception:
                log.exception("voice show_draft failed (tenant %s)", ctx.tenant_id)
                return {"error": "show_failed",
                        "message": "The draft wouldn't come up — say so; "
                                   "nothing was sent."}
            if card:
                drafted["card"] = card
            if isinstance(result, dict) and result.get("confirm_action"):
                drafted["confirm_action"] = result.pop("confirm_action")
            return result
        out = await concierge_data.execute(ctx.tenant_id, ctx.client, name, args,
                                           full_qa=full_qa, member=asker, user_id=ctx.user_id)
        drafted.setdefault("seen", set()).update(concierge_draft.addresses_in(out))
        # What the lookup FOUND rides on its trail line, so the next turn can
        # reuse it (test run 4.2b, 24 Sep: "yes, anyway" re-ran the contact
        # search and the record lookup for Graham, already found a turn before).
        found = _found_in(out)
        if found:
            for t in reversed(trail):
                if t.get("name") == name and not t.get("found"):
                    t["found"] = found
                    break
        return out

    queue: asyncio.Queue = asyncio.Queue()
    tools_called: list[str] = []
    trail: list[dict] = []

    async def _on_tool(name: str, args: dict) -> None:
        if turn_state.status == "cancelled":
            return
        tools_called.append(name)
        detail = tool_detail(args)
        label = _BUSINESS_TOOL_LABELS.get(name, "Searched your data")
        trail.append({"name": name, "label": label, "detail": detail})
        await queue.put({"t": "tool", "name": name,
                         "hint": _BUSINESS_TOOL_HINTS.get(name, "Searching your data…"),
                         "label": label, "detail": detail})

    # The spoken filter: each tool round is its own JSON document, so the
    # extractor resets at every round boundary (the donor flushes its filter
    # the same way). Extracted text goes straight out as speak frames.
    state = {"round": -1, "filter": None}

    said = _remember_speech(ctx)

    def _speak(text: str) -> None:
        if text and turn_state.status != "cancelled":
            said[1] += " " + text
            queue.put_nowait({"t": "speak", "text": text})

    async def _on_delta(text: str, round_no: int) -> None:
        if turn_state.status == "cancelled":
            return
        if round_no != state["round"]:
            if state["filter"] is not None:
                state["filter"].end()
            state["round"] = round_no
            state["filter"] = voice_spoken.ReplyExtractor(_speak)
        state["filter"].push(text)

    async def _run() -> None:
        nonlocal walk
        import time as _time
        t0 = _time.monotonic()
        with llm.request_scope():
            await asyncio.to_thread(trace.emit,ctx.tenant_id, "voice_turn_start", user_id=ctx.user_id,
                       detail={"turn_id": turn_id})
            try:
                voice_turn_state.checkpoint(turn_state)
                if agenda is not None:
                    walk = debrief_walk.begin(ctx.tenant_id, ctx.user_id, body.voice_session, agenda)
                    # Counts and group keys only; no customer-written content.
                    await asyncio.to_thread(
                        trace.emit, ctx.tenant_id, "debrief_walk", user_id=ctx.user_id,
                        outcome="started" if walk.groups else "empty",
                        detail={"groups": [g["key"] for g in walk.groups],
                                "counts": [g["count"] for g in walk.groups],
                                "unread": list(agenda.get("unread") or []), "trigger": body.debrief})
                elif body.debrief == "next" and walk is not None and not walk.finished:
                    debrief_walk.advance(walk, "done")
                question_note="\n".join(n for n in (
                    await asyncio.to_thread(concierge_delegation.context_note, ctx, body.voice_session),
                    await asyncio.to_thread(_people_note, ctx, asker, full_qa, scheduling_on),
                    await asyncio.to_thread(_voice_note, ctx, body.clipped and not _bare_answer(question), member=asker,
                                            live=origin == "live"),
                    debrief_walk.note(walk, live=origin == "live")) if n) or None
                voice_turn_state.checkpoint(turn_state)
                result = await concierge.answer_business(
                    question, history,
                    tools=(concierge_data.tools_for(full_qa, asker)
                           + [concierge_draft.DRAFT_TOOL, concierge_draft.SHOW_TOOL,
                              *concierge_tasks.foreground_tools(),
                              *([debrief_walk.TOOL] if walk is not None and not walk.finished else [])]),
                    tool_executor=_executor, on_tool=_on_tool,
                    business_name=tenant.get("name") or tenant.get("company"),
                    today=today, full_qa=full_qa, voice=voice,
                    tenant_id=ctx.tenant_id,
                    on_delta=_on_delta, voice_mode=True, tasks_on=True,
                    cancelled=lambda: turn_state.status == "cancelled",
                    question_note=question_note)
                if turn_state.status == "cancelled":
                    usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                                           operation="concierge_business", module="concierge")
                voice_turn_state.checkpoint(turn_state)
                if state["filter"] is not None:
                    state["filter"].end()
                # In voice the Drafting agent writes emails; the old
                # write-it-yourself draft_reply card is not offered here.
                if starter.final and result.reply == starter.final:
                    # The task list's line was set by the app, not streamed by
                    # the model — so it is spoken here, straight away.
                    _speak(starter.final)
                if (result.action or {}).get("kind") == "draft_reply":
                    result = result._replace(action=None)
                if drafted.get("confirm_action") and not (result.action or {}).get("kind"):
                    # A held booking: the card that confirms it (then drafts the
                    # email) comes from the server, not the model (confirm first).
                    result = result._replace(action=drafted["confirm_action"])
                voice_turn_state.commit(turn_state, "finalize")
                done = await asyncio.to_thread(finish_business_turn,
                    ctx, result, question=question, trail=trail,
                    tools_called=tools_called, t0=t0,
                    full_qa=full_qa, scheduling_on=scheduling_on, member=asker,
                    persist=True,
                    spoken_ms=spoken_ms, tasks=starter.batch, intake=starter.intake, origin=origin, display_turn_id=turn_id, native_events=native_events)
                voice_turn_state.complete(turn_state)
                await asyncio.to_thread(trace.emit,ctx.tenant_id, "voice_turn_complete", user_id=ctx.user_id,
                           latency_ms=(_time.monotonic() - t0) * 1000,
                           detail={"turn_id": turn_id, "boundary": turn_state.boundary})
                done["turn_id"] = turn_id
                done.pop("type", None)
                said[1] += " " + str(done.get("correction") or "")
                done["draft"] = drafted.get("card")
                if walk is not None:
                    # The opening has been given once she has actually replied;
                    # until then the next turn re-sends it (a cancelled first
                    # reply must not lose the greeting). The screen shows where
                    # in the walk we are.
                    if done.get("reply"):
                        walk.opened = True
                    done["debrief"] = debrief_walk.progress(walk)
                await queue.put({"t": "done", **done})
            except voice_turn_state.Cancelled:
                await asyncio.to_thread(trace.emit,ctx.tenant_id, "voice_turn_cancelled", user_id=ctx.user_id,
                           outcome="cancelled", detail={"turn_id": turn_id})
                await queue.put({"t": "cancelled", "turn_id": turn_id})
            except asyncio.CancelledError:
                await asyncio.to_thread(trace.emit,ctx.tenant_id, "voice_turn_interrupted", user_id=ctx.user_id,
                           outcome="unknown", detail={"turn_id": turn_id,
                                                      "boundary": turn_state.boundary})
                raise
            except Exception:
                log.exception("voice chat turn failed (tenant %s)", ctx.tenant_id)
                await asyncio.to_thread(trace.emit,ctx.tenant_id, "concierge_turn", outcome="error",
                           user_id=ctx.user_id,
                           latency_ms=(_time.monotonic() - t0) * 1000,
                           detail={"question": question[:300],
                                   "tools_called": tools_called,
                                   "surface": "voice", "turn_id": turn_id})
                await queue.put({"t": "error",
                                 "error": "I had trouble with that one — could "
                                          "you try asking it a different way?"})
            finally:
                voice_turn_state.complete(turn_state)
                await queue.put(None)

    async def _stream():
        task = asyncio.create_task(_run())
        _VOICE_RUNNERS.add(task)
        task.add_done_callback(_VOICE_RUNNERS.discard)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                # Disconnect may cancel generation, but cannot revoke a write
                # helper/send already entered. Keep that runner referenced.
                voice_turn_state.cancel(identity)
                # Cooperative cancellation lets an in-flight model finish and
                # meter its receipt. No next model/tool/write may start after
                # acceptance; an already-entered helper is allowed to finish.


    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform"})


# What the server has said to each person lately, so /voice/tts speaks only
# that (security review 2026-09, M37: it proxied any text a signed-in client
# sent). In-process, like _DISCARDED: a turn's sentences are spoken within
# seconds, by the instance that streamed them. "tenant:user" -> [[when, text]].
_SPOKEN: dict[str, list] = {}
_SPOKEN_TTL_S = 900
_SPOKEN_MAX = 40


def _squash(text) -> str:
    """Whitespace-free and lower-case: the phone re-splits the streamed text
    into sentences and glues rounds with a space, so spacing never matches."""
    return "".join(str(text or "").split()).lower()


def _remember_speech(ctx, text: str = "") -> list:
    """Record something the server is about to have spoken; returns the entry
    so a streaming turn can keep appending to it."""
    import time as _time
    now = _time.time()
    key = f"{ctx.tenant_id}:{getattr(ctx, 'user_id', None) or ''}"
    entries = [e for e in _SPOKEN.get(key, []) if now - e[0] < _SPOKEN_TTL_S][-(_SPOKEN_MAX - 1):]
    entry = [now, str(text or "")]
    entries.append(entry)
    _SPOKEN[key] = entries
    for k in [k for k, v in _SPOKEN.items() if not v or now - v[-1][0] > _SPOKEN_TTL_S]:
        _SPOKEN.pop(k, None)
    return entry


def _fixed_lines() -> frozenset:
    """The lines the phone says by itself (voice-live.js LOCAL_LINES, the
    panel's acks, the greeting) and the lookup chips it reads out."""
    from app import concierge_draft
    lines = ["Sent.", "You're welcome.", "Okay — it's waiting in the thread.",
             "Okay — I'll leave it.", "Sorry — was that a yes? Say yes or no.",
             "Bye for now.", "Done.", "Nothing changed.",
             "That didn't send — it's still waiting in the thread.",
             "That didn't run — nothing has changed. You can say yes again, or tap Confirm.",
             "Sorry, I couldn't get an answer just then.",
             "Morning.", "Afternoon.", "Evening.", "Looking that up.",
             "Searching your data.", concierge_draft.HINT, concierge_draft.SHOW_HINT,
             *_BUSINESS_TOOL_HINTS.values()]
    return frozenset(_squash(t.replace("…", ".")) for t in lines) | frozenset(
        _squash(t) for t in lines)


def _server_said(ctx, text: str) -> bool:
    """Is this text something the server produced for this person lately?"""
    import time as _time
    want = _squash(text)
    if not want:
        return False
    if want in _fixed_lines():
        return True
    key = f"{ctx.tenant_id}:{getattr(ctx, 'user_id', None) or ''}"
    now = _time.time()
    return any(now - t < _SPOKEN_TTL_S and want in _squash(said)
               for t, said in _SPOKEN.get(key, []))


def _spoken_texts(ctx) -> list:
    """The texts behind _server_said: what the server said to this person lately."""
    import time as _time
    key = f"{ctx.tenant_id}:{getattr(ctx, 'user_id', None) or ''}"
    now = _time.time()
    return [said for t, said in _SPOKEN.get(key, []) if now - t < _SPOKEN_TTL_S]


class VoiceTtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=800)
    voice: str = Field("marin", max_length=20)


# One connection to OpenAI's speech API, kept open between sentences (Kris
# 2026-09-24: "the text reply appears a couple of seconds before the TTS
# fires"). A fresh client per sentence paid a new TLS handshake every time.
_TTS_HTTP = None


def _tts_http():
    global _TTS_HTTP
    import httpx
    if _TTS_HTTP is None or getattr(_TTS_HTTP, "is_closed", False):
        _TTS_HTTP = httpx.AsyncClient(timeout=60, limits=httpx.Limits(
            max_keepalive_connections=8, keepalive_expiry=120))
    return _TTS_HTTP


async def _tts_stream_response(text: str, voice: str, response_format: str = "pcm") -> StreamingResponse:
    """Proxy one sentence to the selected TTS provider as raw 24kHz PCM16 mono
    (matches the mic side, so nothing is ever resampled twice) — shared by the
    authed route and the rig. stream_format "audio" is MANDATORY — without it
    the API buffers the whole clip and first byte goes from ~0.4s to 0.9–1.8s
    (OurTab, measured 2026-08-22)."""
    import httpx
    from app import speech
    if not speech.local() and not settings.openai_api_key:
        return JSONResponse({"error": "Live voice isn't configured yet "
                                      "(OPENAI_API_KEY missing)."}, status_code=503)
    voice = speech.resolve_voice(voice)

    client = _tts_http()
    try:
        if speech.local():
            url = speech.endpoint("/v1/audio/speech")
            auth = speech.headers()
            payload = {"voice": voice, "input": text,
                       "response_format": "wav" if response_format == "mp3" else response_format}
        else:
            url = _OPENAI_SPEECH
            auth = {"Authorization": f"Bearer {settings.openai_api_key}"}
            payload = {"model": _TTS_MODEL, "voice": voice, "input": text,
                       "response_format": response_format, "stream_format": "audio",
                       "instructions": "Speak warmly at a natural, quick conversational pace. British English."}
        req = client.build_request(
            "POST", url, headers=auth, json=payload)
        upstream = await client.send(req, stream=True)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("voice tts: %s", exc.__class__.__name__)
        return JSONResponse({"error": "The voice couldn't speak that."},
                            status_code=502)
    if upstream.status_code != 200:
        detail = (await upstream.aread())[:300]
        await upstream.aclose()
        log.warning("voice tts: provider returned %s: %s", upstream.status_code, detail)
        return JSONResponse({"error": "The voice couldn't speak that."},
                            status_code=502)

    async def _pcm():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()   # the connection goes back to the pool

    return StreamingResponse(_pcm(), media_type="application/octet-stream",
                             headers={"Cache-Control": "no-cache"})


@router.post(
    "/voice/tts",
    dependencies=[Depends(rate_limit("concierge_voice_tts", limit=60, window_s=60)),
                  Depends(require_csrf_header)],
)
async def voice_tts(body: VoiceTtsRequest,
                    ctx: TenantCtx = Depends(require_concierge)) -> StreamingResponse:
    """Speak one sentence — only one the server said to this person (M37)."""
    text = body.text.strip()
    if not _server_said(ctx, text):
        log.warning("voice tts: refused text the server didn't say (tenant=%s)", ctx.tenant_id)
        return JSONResponse({"error": "The voice only reads out the assistant's own replies."},
                            status_code=403)
    return await _tts_stream_response(text, body.voice)


_VOICE_SAMPLE_LINE = "Hi, I'm your Concierge. Here's what needs you today."
_VOICE_SAMPLES: dict[str, bytes] = {}
_VOICE_SAMPLE_LOCK = None


class VoiceSampleRequest(BaseModel):
    voice: str = Field("marin", max_length=20)


@router.post(
    "/voice/sample",
    dependencies=[Depends(rate_limit("concierge_voice_sample", limit=10, window_s=3600)),
                  Depends(require_csrf_header)],
)
async def voice_sample(body: VoiceSampleRequest,
                       ctx: TenantCtx = Depends(require_concierge)) -> Response:
    """One fixed clip per voice per worker lifetime; concurrent plays share it.

    The request limit also bounds retries if upstream fails. No client text
    can reach this endpoint or bypass the normal server-said speech guard.
    """
    import asyncio
    from app import speech
    global _VOICE_SAMPLE_LOCK
    voice = speech.resolve_voice(body.voice)
    cache_key = settings.speech_provider + ":" + voice
    if _VOICE_SAMPLE_LOCK is None:
        _VOICE_SAMPLE_LOCK = asyncio.Lock()
    async with _VOICE_SAMPLE_LOCK:
        if cache_key not in _VOICE_SAMPLES:
            response = await _tts_stream_response(_VOICE_SAMPLE_LINE, voice, "mp3")
            if response.status_code != 200:
                return response
            try:
                audio = b"".join([chunk async for chunk in response.body_iterator])
            except Exception:
                return JSONResponse({"error": "The voice couldn't speak that."}, status_code=502)
            if not audio:
                return JSONResponse({"error": "The voice couldn't speak that."}, status_code=502)
            _VOICE_SAMPLES[cache_key] = audio
    return Response(_VOICE_SAMPLES[cache_key], media_type="audio/wav" if speech.local() else "audio/mpeg",
                    headers={"Cache-Control": "private, max-age=86400"})
