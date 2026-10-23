"""BLAM8 — Business Logistics Automated. Service entrypoint.

Public site (landing + legal), auth (login/register), and the email-client
dashboard are server-rendered with Jinja + Tailwind. Microsoft Graph mailbox
flow is at /oauth, the dashboard at /app.
"""
import asyncio
import logging
import sys
from pathlib import Path
from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

from app.config import background_jobs_on, settings  # noqa: F401  (loaded so env is read at startup)
from app.templating import templates

log = logging.getLogger(__name__)

# Logging: one stderr handler on the root logger, so every line carries a
# timestamp, level and logger name ("2026-09-18 08:19:27 ERROR blautom8.stripe:
# ...") instead of the bare message Python's last-resort handler prints when
# nothing is configured. The root stays at WARNING so third-party libraries
# are as quiet as before (httpx logs every request at INFO); the app's own
# namespaces get INFO. uvicorn's loggers do not propagate to the root, so its
# access lines are untouched. basicConfig is a no-op if a handler is already
# installed (pytest's capture, for one), so this never doubles up.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
for _app_logger in ("app", "blautom8"):
    logging.getLogger(_app_logger).setLevel(logging.INFO)
from app import auth, httpx_ssl, security, admin_auth, tracking
from app.startup_timing import span, mark
from app.auth import AuthRedirect, OnboardingRedirect, SubscriptionRequired, TenantGone
from app.trial import TrialLimitReached
from app.admin_auth import AdminAuthRedirect
from app.auth_routes import router as auth_router
from app.oauth import router as oauth_router
from app.dashboard import router as dashboard_router
from app.people_routes import join_router as people_join_router
from app.people_routes import router as people_router
from app.people_routes import viewas_router as people_viewas_router
from app.onboarding import router as onboarding_router
from app.onboarding_routes import router as onboarding_review_router
from app.guided_setup_routes import router as guided_setup_router
from app.webhook import router as webhook_router
from app.build_hooks import router as build_hooks_router
from app.sms_webhook import router as sms_webhook_router
from app.admin import router as admin_router
from app.billing_routes import router as billing_router
from app.payments import router as payments_router
from app.stripe_webhook import router as stripe_webhook_router
from app.concierge_routes import router as concierge_router
from app.crm_routes import router as crm_router
from app.practice_routes import router as practice_router
from app.shop_routes import router as shop_router
from app.shop_routes import api as shop_api_router
from app.channel_routes import router as channel_router
from app.telephone_routes import router as telephone_pilot_router
from app.telephone_live_routes import router as telephone_live_router
from app.assets_routes import router as assets_router
from app.assets_routes import api_router as assets_api_router
from app.assets_routes import capture_router as assets_capture_router
from app.events_routes import router as events_router
from app.wholesale_routes import router as wholesale_router
from app.wholesale_routes import api_router as wholesale_api_router
from app.partner_routes import router as partner_router
from app.push_routes import router as push_router
from app.portal import router as portal_router
from app.exit_export import router as exit_export_router
from app.quote_public import router as quote_public_router
from app.job_sheet_public import router as job_sheet_public_router
from app.sse import router as sse_router
from app.device_sync import router as device_sync_router

app = FastAPI(title="BLAM8 — Business Logistics Automated")
app.include_router(guided_setup_router)
app.include_router(onboarding_review_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
security.install_security(app)
templates.env.globals["csrf_token"] = security.csrf_token
# Cache-bust static assets per deploy: append the running git commit to asset
# URLs so a JS/CSS change reaches returning browsers immediately instead of
# serving a stale cached copy. RENDER_GIT_COMMIT is injected by Render. The
# local fallback is the server start time, NOT a constant: a constant ("dev")
# let Chrome's heuristic freshness serve stale JS/CSS forever, so edited
# assets silently never reached the local browser (the ?nocache trap).
import os as _os
import time as _time


def static_rev(root: Path | None = None) -> str:
    """A short fingerprint of everything under app/static (names and bytes). On Render the asset
    URLs' ?v= is this, NOT the commit: every deploy used to change every asset URL, so a phone
    re-downloaded ~700 KB of script and style (and the service worker cached a second full copy)
    after every backend-only change, and we deploy several times a day. Now the URLs, the browser
    cache and the worker's cache survive a deploy that touched no static file."""
    import hashlib
    root = root or (Path(__file__).parent / "static")
    h = hashlib.sha1()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"|")
            h.update(p.read_bytes())
    return h.hexdigest()[:7]


# Local dev keeps the start-time fallback (an edited asset must reach the browser on restart).
templates.env.globals["asset_rev"] = (static_rev() if _os.environ.get("RENDER_GIT_COMMIT")
                                      else f"dev{int(_time.time())}")
# SEO globals: canonical/OG URLs are always built on the public domain (never the
# Render *.onrender.com host, which would split ranking signal across two hosts),
# and search-engine ownership proofs come from env so verification never needs a code change.
templates.env.globals["base_url"] = (settings.app_base_url or "https://blam8.com").rstrip("/")
templates.env.globals["bing_site_verification"] = _os.environ.get("BING_SITE_VERIFICATION", "")
templates.env.globals["google_site_verification"] = _os.environ.get("GOOGLE_SITE_VERIFICATION", "")
# The Gmail-connect invite gate is env-driven and lifts without a code change, so
# shared chrome (footer, default meta description) asks live rather than baking in
# a "works with Gmail" claim that overpromises while Gmail connects are invite-only.
from app import slots as _slots
templates.env.globals["invite_gate_active"] = lambda: _slots.invite_gate_active()
# The mailbox-connect surfaces (onboarding, settings, mailboxes) decide between
# the open "Connect Gmail" button and the gated invite box (partials/gmail_gate.
# html) — server-side enforcement lives in /oauth/connect (src/app/oauth.py).
templates.env.globals["gmail_slots"] = lambda: _slots.google_slots()


def _gmail_connect_open(request) -> bool:
    s = _slots.google_slots()
    if s["verified"]:
        return True
    if s["full"]:
        return False
    if not _slots.invite_gate_active():
        return True
    return _slots.invite_cookie_valid(request.cookies.get(_slots.INVITE_COOKIE))


templates.env.globals["gmail_connect_open"] = _gmail_connect_open
# The free trial ends on 14 days OR this many emails, whichever comes first
# (src/app/trial.py). Public copy reads it from here rather than hardcoding a number, so
# the site can never quote a cap the deployment isn't actually enforcing — the value
# is env-driven (TRIAL_EMAIL_CAP).
templates.env.globals["trial_email_cap"] = settings.trial_email_cap
# Web Push: the VAPID public key the browser subscribes with (src/app/static/push.js
# reads it from a <meta> in the app shell). Empty = push disabled — push.js no-ops.
templates.env.globals["vapid_public_key"] = settings.vapid_public_key
# Live voice conversation (docs/voice-live-seed.md): the widget's mic button
# appears only when the flag is armed AND the selected speech provider is configured.
from app import speech
templates.env.globals["voice_live_on"] = bool(settings.voice_live_enabled and speech.ready())
templates.env.globals["speech_voice_choices"] = speech.voice_choices
from app import gptlive
templates.env.globals["gptlive_on"] = gptlive.available()
app.include_router(gptlive.router)
app.include_router(auth_router)
app.include_router(oauth_router)
app.include_router(dashboard_router)
app.include_router(people_router)
app.include_router(people_viewas_router)
app.include_router(people_join_router)
app.include_router(onboarding_router)
from app.knowledge_source_routes import router as knowledge_source_router
app.include_router(knowledge_source_router)
app.include_router(webhook_router)
app.include_router(build_hooks_router)
app.include_router(sms_webhook_router)
app.include_router(admin_router)
app.include_router(billing_router)
app.include_router(stripe_webhook_router)
app.include_router(payments_router)
app.include_router(concierge_router)
app.include_router(crm_router)
from app.tenant_outreach_routes import router as tenant_outreach_router
app.include_router(tenant_outreach_router)
from app.scheduled_plan_routes import router as scheduled_plan_router
app.include_router(scheduled_plan_router)
app.include_router(practice_router)
app.include_router(shop_router)
app.include_router(shop_api_router)
app.include_router(channel_router)
app.include_router(telephone_pilot_router)
app.include_router(telephone_live_router)
app.include_router(assets_router)
app.include_router(assets_api_router)
app.include_router(assets_capture_router)
app.include_router(events_router)
app.include_router(wholesale_router)
app.include_router(wholesale_api_router)
app.include_router(partner_router)
app.include_router(push_router)
app.include_router(portal_router)
app.include_router(quote_public_router)
app.include_router(job_sheet_public_router)
app.include_router(sse_router)
app.include_router(device_sync_router)
# The exit door: /app/export.zip on its own router, WITHOUT the subscription
# gate — the export must outlive the subscription (src/app/exit_export.py).
app.include_router(exit_export_router)


# The legacy domain and the Render default host serve this same app, so to a
# search engine the site exists in triplicate; canonical <link>s point at
# blam8.com but a 301 is what actually consolidates. GET/HEAD only — webhooks
# POST to whatever host they were registered with and must never bounce — and
# /health stays put for Render's checker. Explicit host list, not "anything
# non-canonical": TestClient's `testserver` and local hosts must pass through.
_LEGACY_HOSTS = {"blam8.haail.dev", "www.blam8.haail.dev",
                 "blautom8.onrender.com", "www.blam8.com"}


@app.middleware("http")
async def _canonical_host_redirect(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if (host in _LEGACY_HOSTS and request.method in ("GET", "HEAD")
            and request.url.path != "/health"):
        target = templates.env.globals["base_url"] + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def _discretion(request: Request, call_next):
    """Filming mode, for tenants with tenant_config.discretion on (src/app/discretion.py).

    Injected here rather than added to the app shell so that a tenant without
    it receives no tag, no reference and no hint the mode exists — which is the
    whole point: everyone who joins gets the ordinary app.

    require_tenant has already put tenant_id on request.state by the time the
    response comes back, so this costs one cached lookup and nothing else.
    """
    response = await call_next(request)
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id or "text/html" not in (response.headers.get("content-type") or ""):
        return response
    from app import discretion
    def enabled():
        with span("discretion_lookup"):
            return discretion.enabled(tenant_id)
    with span("discretion_pool"):
        on = await run_in_threadpool(enabled)
    mark("discretion_on", on)
    if not on:
        return response
    if not hasattr(response, "body_iterator"):
        return response
    with span("discretion_body"):
        body = b"".join([chunk async for chunk in response.body_iterator])
    rev = templates.env.globals.get("asset_rev", "")
    tag = f'<script src="/static/discretion.js?v={rev}" defer></script>'.encode()
    if b"</body>" in body:
        body = body.replace(b"</body>", tag + b"</body>", 1)
    headers = dict(response.headers)
    headers.pop("content-length", None)          # the body just changed length
    return Response(content=body, status_code=response.status_code,
                    headers=headers, media_type=response.media_type)


@app.middleware("http")
async def _capture_src(request: Request, call_next):
    """`?src=yt-<id>` on ANY page — count the click and stash it for the signup
    round-trip (src/app/attribution.py). A middleware rather than a line in each
    route because the shorts link at the homepage, at /setups/<slug> and at
    whatever we point the next batch of descriptions at."""
    response = await call_next(request)
    if request.method in ("GET", "HEAD"):
        raw = request.query_params.get("src")
        if raw:
            from app import attribution
            src = attribution.normalize_src(raw)
            if src:
                attribution.set_src_cookie(response, src)
                attribution.record_hit_async(src)
    return response


@app.middleware("http")
async def _propagate_refreshed_session(request: Request, call_next):
    """If require_tenant refreshed an expired session mid-request, make sure the
    new cookies land on whatever response the handler returned."""
    response = await call_next(request)
    session = getattr(request.state, "refreshed_session", None)
    if session:
        auth.set_session_cookies(response, session)
    admin_session = getattr(request.state, "refreshed_admin_session", None)
    if admin_session:
        admin_auth.set_admin_cookies(response, admin_session)
    return response


@app.middleware("http")
async def _startup_diagnostics(request: Request, call_next):
    routes = {"/app": "home", "/app/": "home", "/app/concierge/panel": "panel",
              "/app/inbox": "inbox", "/app/conversations": "conversations"}
    route = routes.get(request.url.path)
    if request.method != "GET" or route is None or request.headers.get("X-BLAM8-Startup-Diagnostics") != "1":
        return await call_next(request)
    from app import startup_timing
    state, token = startup_timing.begin(route)
    startup_timing.start_sampler(state)
    probe = asyncio.create_task(startup_timing.loop_probe(state))
    status = 0
    response = None
    try:
        response = await call_next(request)
        startup_timing.mark("response_available", 1)
        status = response.status_code
        response.headers["X-BLAM8-Startup-Trace"] = state["id"]
        return response
    finally:
        with startup_timing.span("probe_cancel"):
            probe.cancel()
            try:
                await probe
            except asyncio.CancelledError:
                pass
        await startup_timing.stop_sampler(state)
        startup_timing.finish(state, token, status)
        if response is not None:
            # Includes the completion record's own emission, absent without opt-in.
            response.headers["X-BLAM8-Startup-Log-Sum-Ms"] = f'{state["log_sum_ms"]:.1f}'
            response.headers["X-BLAM8-Startup-Log-Max-Ms"] = f'{state["log_max_ms"]:.1f}'
            response.headers["X-BLAM8-Startup-Sampler"] = state.get("sampler_status", "unavailable")


@app.exception_handler(AuthRedirect)
async def _auth_redirect(request: Request, exc: AuthRedirect):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(OnboardingRedirect)
async def _onboarding_redirect(request: Request, exc: OnboardingRedirect):
    return RedirectResponse("/onboarding", status_code=303)


@app.exception_handler(AdminAuthRedirect)
async def _admin_auth_redirect(request: Request, exc: AdminAuthRedirect):
    return RedirectResponse("/admin/login", status_code=303)


@app.exception_handler(TenantGone)
async def _tenant_gone(request: Request, exc: TenantGone):
    """The session points at a tenant that no longer exists. Clearing the cookies
    is the whole fix: with them gone /login mints a fresh session instead of
    resolving the same ghost again (live 2026-09-04 a deleted test tenant was
    shown the plans page — billing for an account that is not there)."""
    from app import auth as _auth
    # A session refreshed earlier in THIS request would otherwise be re-set on the
    # way out by _propagate_refreshed_session, resurrecting what we just cleared.
    request.state.refreshed_session = None
    resp = RedirectResponse("/login", status_code=303)
    _auth.clear_session_cookies(resp)
    return resp


@app.exception_handler(SubscriptionRequired)
async def _subscription_required(request: Request, exc: SubscriptionRequired):
    """A tenant whose access has lapsed on a gated /app route → /billing/plans.
    Active only when REQUIRE_SUBSCRIPTION_GATE is on (require_subscription is a
    pass-through otherwise)."""
    return RedirectResponse("/billing/plans", status_code=303)


@app.exception_handler(TrialLimitReached)
async def _trial_limit_reached(request: Request, exc: TrialLimitReached):
    """A human-initiated draft on an expired free trial → the billing CTA. (The
    automatic paths return None instead of raising.)"""
    return RedirectResponse("/billing/plans?reason=trial", status_code=303)


@app.on_event("startup")
async def _check_secrets() -> None:
    """Refuse to start if a security-critical secret is missing."""
    settings.assert_runtime_secrets()


@app.on_event("startup")
async def _warm_cold_paths() -> None:
    """Pay the once-per-process costs at boot instead of charging them to
    whoever loads the first page (Kris 2026-09-01: "still a little slow first
    time"). Both are pure warm-ups — nothing here changes what a page renders,
    so any failure is swallowed and the first request simply pays as before.

      * the shared TLS trust store (~340ms to build, src/app/httpx_ssl.py)
      * the Jinja templates behind the surfaces people open first (~130ms of
        parse+compile across the set, then ~0.2ms each forever)
    """
    def _warm():
        httpx_ssl.warm()
        for name in ("app/home.html", "app/conversations.html",
                     "app/_conversation_list.html", "app/_thread.html",
                     "app/diary.html", "app/draft_review.html"):
            try:
                templates.env.get_template(name)
            except Exception:
                log.warning("template warm-up skipped for %s", name, exc_info=True)

    # In a thread: this is CPU-bound parsing, and the event loop is already
    # serving /health by the time it runs.
    await asyncio.to_thread(_warm)


@app.on_event("startup")
async def _resume_concierge_tasks() -> None:
    """The Concierge's task list (docs/plan-concierge-task-list.md): a deploy
    mid-batch stopped its worker — pick up whatever was left queued or working."""
    if not background_jobs_on():
        return
    from app import concierge_tasks
    app.state.concierge_tasks_resume = asyncio.create_task(concierge_tasks.resume_unfinished())


@app.on_event("startup")
async def _start_gmail_poller() -> None:
    """Gmail auto-draft. Microsoft gets push (Graph webhook); Gmail polls instead —
    see src/app/jobs/poll_gmail.py. GMAIL_POLL_SECONDS=0 disables it."""
    if settings.gmail_poll_seconds > 0 and background_jobs_on():
        from app.jobs.poll_gmail import poll_forever
        # keep a reference so the task isn't garbage-collected mid-loop
        app.state.gmail_poller = asyncio.create_task(poll_forever(settings.gmail_poll_seconds))


@app.on_event("startup")
async def _start_calendar_sync() -> None:
    """Calendar Connect busy-sync: mirror calendar-connected tenants' busy events
    into diary blocks — see src/app/jobs/sync_calendar.py. CALENDAR_SYNC_SECONDS=0
    disables it."""
    if settings.calendar_sync_seconds > 0 and background_jobs_on():
        from app.jobs.sync_calendar import sync_forever
        app.state.calendar_sync = asyncio.create_task(sync_forever(settings.calendar_sync_seconds))


@app.on_event("startup")
async def _start_memstat() -> None:
    """Resident memory and in-process store sizes, one log line per MEMSTAT_SECONDS (src/app/memstat.py).
    Off by default. It reads THIS process, so it does not wait on background_jobs_on()."""
    if settings.memstat_seconds > 0:
        from app import memstat
        app.state.memstat = asyncio.create_task(memstat.run_forever(settings.memstat_seconds))


@app.on_event("startup")
async def _warm_concierge() -> None:
    """Prime the voice Concierge's answer format with each tool set it sends,
    so the first question after a deploy isn't the one that waits (app/
    concierge_warm.py; Render only)."""
    from app import concierge_warm
    concierge_warm.start()


@app.on_event("startup")
async def _start_subscription_renewer() -> None:
    """Keep Microsoft Graph mailbox subscriptions alive — they expire ~3 days out,
    so without renewal a Microsoft mailbox silently stops auto-drafting. Runs
    in-process like the Gmail poller (recreates missing/expired subs on the first
    sweep), so no separate Render cron is needed — see
    src/app/jobs/renew_subscriptions.py. SUBSCRIPTION_RENEW_SECONDS=0 disables it (e.g.
    if you run the Render cron instead)."""
    if settings.subscription_renew_seconds > 0 and background_jobs_on():
        from app.jobs.renew_subscriptions import renew_forever
        app.state.subscription_renewer = asyncio.create_task(
            renew_forever(settings.subscription_renew_seconds))


@app.on_event("startup")
async def _start_daily_jobs() -> None:
    """In-process daily maintenance — day-before booking reminders (08:00 UTC) and the
    retention purge (03:30 UTC) — so neither needs a separate Render cron service (the
    subscription renewer already runs in-process above). RUN_DAILY_JOBS=false disables,
    e.g. if you choose to run them as an external cron instead."""
    if not background_jobs_on():
        log.info("background jobs OFF in this process (not Render, RUN_DAILY_JOBS unset) — M29")
        return
    from app import scheduler
    from app.admin_outreach import run as run_admin_outreach
    app.state.admin_outreach = asyncio.create_task(
        scheduler.run_every(60, run_admin_outreach, "admin_outreach"))
    from app.jobs.process_sequences import run as run_process_sequences
    from app.jobs.purge_email_log import run as run_purge
    from app.jobs.send_reminders import run as run_reminders
    from app.jobs.send_recall import run as run_recall
    from app.jobs.send_service_reminders import run as run_service_reminders
    from app.jobs.score_no_shows import run as run_score_no_shows
    from app.jobs.send_appointment_reminders import run as run_appointment_reminders
    from app.jobs.ready_reminders import run as run_ready_reminders
    from app.jobs.release_stale_reservations import run as run_release_stale_reservations
    from app.jobs.send_best_before import run as run_best_before
    from app.jobs.deposit_lifecycle import run as run_deposit_lifecycle
    from app.jobs.send_handover_reminders import run as run_handover_reminders
    from app.jobs.chase_stakeholders import run as run_chase_stakeholders
    from app.jobs.assemble_run_sheets import run as run_assemble_run_sheets
    from app.wholesale_standing import generate_due_standing_orders
    app.state.daily_reminders = asyncio.create_task(
        scheduler.run_daily(8, 0, run_reminders, "send_reminders"))
    # The balance, the day before a booking ends (Invoice settings › Bill the
    # balance — off for every business unless switched on).
    from app.jobs.raise_balances import run as run_raise_balances
    app.state.daily_balances = asyncio.create_task(
        scheduler.run_daily(8, 10, run_raise_balances, "raise_balances"))
    # After the job (src/app/after_job.py): jobs done at their day's end, and the
    # deadlines coming due reach Today — only businesses that use them.
    from app.after_job import run as run_after_job
    app.state.daily_after_job = asyncio.create_task(
        scheduler.run_daily(7, 20, run_after_job, "after_job"))
    # Dependants (docs/modules/module-requirement-groups.md Phase 2): a gated expiry
    # that lapses before an upcoming booking earns a word before it bites.
    from app.dependants_sweep import run_expiry_chases
    app.state.daily_dependant_expiry = asyncio.create_task(
        scheduler.run_daily(8, 5, run_expiry_chases, "dependant_expiry_chases"))
    # Packages (src/app/wallet.py): the automatic spend when the Diary says so, and
    # the balance / expiry notes — a tenant without packages costs nothing.
    from app.wallet import run_sweep as run_wallet_sweep
    app.state.daily_wallet = asyncio.create_task(
        scheduler.run_daily(8, 10, run_wallet_sweep, "wallet_sweep"))
    # Courses + the waiting list (src/app/courses.py): lapsed offers move on, opened
    # places are offered, the re-enrolment notes go out.
    from app.courses import run_sweep as run_courses_sweep
    app.state.daily_courses = asyncio.create_task(
        scheduler.run_daily(8, 20, run_courses_sweep, "courses_sweep"))
    # ...and every half hour for tenants that keep a list: a hold that lapsed
    # frees its place to whoever is waiting for it (docs/plan-when-its-full.md).
    from app.courses import run_frequent as run_waitlist_frequent
    app.state.waitlist_frequent = asyncio.create_task(
        scheduler.run_every(30 * 60, run_waitlist_frequent, "waitlist_frequent"))
    # Wholesale OS (docs/os/wholesale.md §6a) — 06:00 UTC, before the working day:
    # every due standing order spawns its cycle's order deal, runs the credit gate,
    # and drafts the confirmation (or the hold). Gated to wholesale rows by the
    # sweep's own due-read; a tenant without standing orders costs nothing.
    app.state.wholesale_standing = asyncio.create_task(
        scheduler.run_daily(6, 0, generate_due_standing_orders, "wholesale-standing"))
    app.state.daily_service_reminders = asyncio.create_task(
        scheduler.run_daily(8, 15, run_service_reminders, "send_service_reminders"))
    # Invoicing (directive §6.1) — overdue flip + 7/14/30-day milestone pushes,
    # each milestone firing exactly once (overdue_notified_days). Cross-tenant in
    # one read; invoicing is core so there's no module gate.
    from app.jobs.check_overdue_invoices import run as run_overdue_invoices
    app.state.overdue_invoices = asyncio.create_task(
        scheduler.run_daily(8, 45, run_overdue_invoices, "check_overdue_invoices"))
    # Quotes (docs/modules/module-quotes.md Phase 2) — the 3/7/pre-expiry chase, the
    # expiry flip, and the "they keep looking" escalation. Quotes are core, so
    # no module gate; the status filter keeps the read cheap.
    from app.jobs.check_quote_chases import run as run_quote_chases
    app.state.quote_chases = asyncio.create_task(
        scheduler.run_daily(9, 0, run_quote_chases, "check_quote_chases"))
    # Practice OS recall (docs/os/practice.md §6.1) — daily, gated to practice tenants
    # inside run(). The recall wedge: every customer chased exactly when they're due.
    app.state.daily_recall = asyncio.create_task(
        scheduler.run_daily(8, 30, run_recall, "send_recall"))
    app.state.daily_purge = asyncio.create_task(
        scheduler.run_daily(3, 30, run_purge, "purge_email_log"))
    # Storefront OS (docs/os/storefront.md §6) — gated to storefront tenants inside run().
    # Ready-to-collect nudges (reserved→ready on the ready_date), then the reservation-expiry
    # sweep that releases stale holds back to the shelf + drafts a "still want these?" nudge.
    app.state.storefront_ready = asyncio.create_task(
        scheduler.run_daily(7, 0, run_ready_reminders, "ready_reminders"))
    app.state.storefront_release = asyncio.create_task(
        scheduler.run_daily(7, 15, run_release_stale_reservations, "release_stale_reservations"))
    # Best-before drafter (docs/os/storefront.md §12.2) — the retention/margin killer:
    # personalised "going today, £X off" to the past buyers of each expiring product.
    app.state.storefront_best_before = asyncio.create_task(
        scheduler.run_daily(7, 30, run_best_before, "send_best_before"))
    # CRM Phase 4b §19.3: multi-step sequences — sweep due enrollments every 15 min.
    app.state.crm_sequences = asyncio.create_task(
        scheduler.run_every(900, run_process_sequences, "crm_sequences"))
    from app.scheduled_plan import run as run_scheduled_plan
    app.state.scheduled_plan = asyncio.create_task(
        scheduler.run_every(60, run_scheduled_plan, "scheduled_plan"))
    # Morning debrief (docs/modules/module-morning-debrief.md) — the first per-tenant-
    # TIMED job: a 15-min sweep fires each tenant's brief at their own
    # debrief_time in diary time, rather than one hardcoded UTC constant.
    # Zero-LLM composition; stamp-first dedupe; closed-day gate inside run().
    from app.jobs.morning_debrief import run as run_morning_debrief
    app.state.morning_debrief = asyncio.create_task(
        scheduler.run_every(900, run_morning_debrief, "morning_debrief"))
    # Practice OS no-show scoring (docs/os/practice.md §6.3) — hourly, gated to practice
    # tenants inside run(). Scores the next 2 days of confirmed appointments and fires ONE
    # extra reminder for the high-risk ones in the 3–6h-before window.
    app.state.score_no_shows = asyncio.create_task(
        scheduler.run_every(60 * 60, run_score_no_shows, "score_no_shows"))
    # Practice OS reminder cycle (docs/os/practice.md §6.2) — every 30 min so the 2h stage
    # lands on time. Gated to practice tenants inside run(). The 48/24/2h cadence, each
    # stage deduped per appointment via reminder_stages_sent; the 24h + 2h touches ask the
    # customer to confirm.
    app.state.appointment_reminders = asyncio.create_task(
        scheduler.run_every(30 * 60, run_appointment_reminders, "send_appointment_reminders"))
    # Job sheets (src/app/job_sheets.py): someone on a job who doesn't sign in is
    # sent it — and told when it changes or they come off it. Every path that
    # changes a job calls job_sheets.soon(); this is the net under the rest.
    # Bank holidays (src/app/bank_holidays.py, D1): gov.uk's list per nation, fetched
    # daily — one-off days are added when they're announced.
    from app.bank_holidays import refresh as run_bank_holidays
    app.state.bank_holidays = asyncio.create_task(
        scheduler.run_daily(4, 20, run_bank_holidays, "bank_holidays"))
    from app.job_sheets import sweep as run_job_sheets
    app.state.job_sheets = asyncio.create_task(
        scheduler.run_every(5 * 60, run_job_sheets, "job_sheets"))
    # Smart booking offers Phase 3 (docs/modules/module-smart-booking-offers.md) — the
    # pre-expiry nudge: offered slots entering their last hours get ONE "still
    # want it?" chase. Every 30 min so the 4h lead window is never missed;
    # per-tenant kill-switch (offer_nudge_enabled) checked inside run().
    from app.jobs.nudge_offered_slots import run as run_offer_nudges
    app.state.offer_nudges = asyncio.create_task(
        scheduler.run_every(30 * 60, run_offer_nudges, "nudge_offered_slots"))
    # Assets OS (docs/os/assets.md §6) — gated to assets tenants inside run(). The deposit
    # lifecycle sweep reaps stale unpaid holds every 15 min (alongside the CRM sequence sweep);
    # the handover reminder chases/reminds units going out tomorrow, once a day.
    app.state.assets_deposit_lifecycle = asyncio.create_task(
        scheduler.run_every(900, run_deposit_lifecycle, "deposit_lifecycle"))
    app.state.assets_handover_reminders = asyncio.create_task(
        scheduler.run_daily(7, 30, run_handover_reminders, "send_handover_reminders"))
    # Events OS (docs/os/events.md §6.2, §12) — the multi-stakeholder chase engine, gated
    # to events tenants inside run(). Draft-first by default (§14); auto-send only on the
    # cfg.auto_chase_events opt-in. The killer feature: every supplier + staff member on
    # every event chased to the run sheet, so the manager stops being the message-bus.
    app.state.events_chase = asyncio.create_task(
        scheduler.run_daily(8, 15, run_chase_stakeholders, "chase_stakeholders"))
    # Events OS run-of-show (docs/os/events.md §6.3, §14) — the daily assembly job, gated to
    # events tenants inside run(). DETERMINISTIC assembly (no LLM: a wrong carriage time is a
    # real liability) + a facts-only, draft-first client sign-off (auto only on
    # cfg.auto_signoff_events). 06:30 UTC, ahead of the 08:15 chase so the sheet reflects the
    # night's replies before the day's chases go out.
    app.state.events_run_sheets = asyncio.create_task(
        scheduler.run_daily(6, 30, run_assemble_run_sheets, "assemble_run_sheets"))


@app.get("/health")
def health(request: Request):
    """Render health check. `rev` is the running build's git commit (Render
    injects RENDER_GIT_COMMIT) — the objective answer to "did my push deploy?"
    when the dashboard isn't to hand. Empty locally. `jobs` says whether this
    process runs the background loops (M29).

    `?deep=1` also asks the database for one row (security review 2026-09,
    M26: /health said OK while the database was down) and answers 503 if it
    can't — for an uptime monitor. Render's own checker keeps the shallow one,
    so a database blip never makes Render restart a healthy web process."""
    import os
    out = {"status": "ok", "service": "blautom8",
           "rev": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
           "jobs": background_jobs_on()}
    if request.query_params.get("deep"):
        from app import db as _db
        try:
            _db.db().table("tenants").select("id").limit(1).execute()
            out["db"] = "ok"
        except Exception:  # noqa: BLE001 — the answer IS the failure
            log.warning("deep health: database check failed", exc_info=True)
            out.update(status="degraded", db="down")
            return JSONResponse(out, status_code=503)
    return out


# ── SEO surface ─────────────────────────────────────────────────────────────
# The public site is four pages (/, /login, /privacy, /terms); everything else
# is auth-gated app UI that must stay out of search results. robots.txt draws
# that line, sitemap.xml lists the public pages, and the IndexNow key file lets
# us push URL changes straight to Bing/Edge (scripts/ops/seo_indexnow_ping.py).

# One entry per comparison page. The /compare hub's cards, the sitemap and the
# SEO tests all render from this list, and the footer links only the hub — so
# shipping a new vs page is: template + route + one row here. hook = the page's
# title question; blurb = its meta description, distilled.
_COMPARISONS = [
    {"slug": "mailchimp", "name": "Mailchimp",
     "hook": "Can Mailchimp reply to customer emails?",
     "blurb": "Mailchimp sends marketing email to a list — from £9.88/month at 500 "
              "contacts (checked September 2026) — but it can't read or reply to an "
              "inbound customer email, and its appointment booking was discontinued "
              "in 2024. BLAM8 answers your email and books the job."},
    {"slug": "zapier", "name": "Zapier",
     "hook": "Buy a worker, or build the plumbing?",
     "blurb": "Zapier sells tasks and zaps you assemble into workflows yourself — no "
              "built-in email agent, knowledge base or diary. BLAM8 arrives working."},
    {"slug": "jobber", "name": "Jobber",
     "hook": "Who answers the email that starts the job?",
     "blurb": "Jobber runs the job after it exists — quotes, crews, invoices — but "
              "doesn't read inbound customer email, and bills UK businesses in USD. "
              "BLAM8 starts at the inbox."},
    {"slug": "servicem8", "name": "ServiceM8",
     "hook": "Who answers the email before it's a job card?",
     "blurb": "ServiceM8 runs the job once it's a job card — cheap per-job GBP "
              "pricing, iOS-first, and an AI Smart Assistant and Office Agent that "
              "help you write and act once the job exists. Its Inbox add-on files "
              "email into job cards for you to action; it doesn't answer your "
              "mailbox. BLAM8 reads every enquiry, replies in your voice and books "
              "the job."},
    {"slug": "tradify", "name": "Tradify",
     "hook": "Job software per user, or the email answered for the whole business?",
     "blurb": "Tradify runs UK trade jobs — scheduling, quotes, invoices, timesheets, "
              "certificates — from £34 per user a month, with its AI tools on the "
              "Plus plan only and no AI email replies (checked September 2026). "
              "BLAM8 answers the inbound email in your voice and books it into a "
              "live diary, priced per business, not per user."},
    {"slug": "fyxer", "name": "Fyxer AI",
     "hook": "Your inbox tidied, or your business answered?",
     "blurb": "Fyxer triages a professional's personal inbox and drafts in their tone — "
              "per seat, meetings-focused. BLAM8 runs the business mailbox: replies "
              "grounded in your real prices, jobs booked into a live diary."},
    {"slug": "podium", "name": "Podium",
     "hook": "The AI front desk, priced for a UK small business.",
     "blurb": "Podium's AI Employee answers texts and calls for US, Canadian and "
              "Australian businesses — from $399/month, and as of September 2026 we "
              "could find no UK operation. BLAM8 answers email for UK businesses "
              "at {{product:price.email}}–{{product:price.concierge}}/month, in GBP."},
    {"slug": "moneypenny", "name": "Moneypenny",
     "hook": "They answer your phone. Who answers your email?",
     "blurb": "Moneypenny answers your phone and website chat — human receptionists, "
              "or an AI Receptionist from £25 a month (checked September 2026). "
              "BLAM8 answers your email and runs the diary; it doesn't answer calls. "
              "The two can run side by side."},
    {"slug": "calendly", "name": "Calendly",
     "hook": "A booking link, or a booked conversation?",
     "blurb": "Calendly is a self-serve link the customer has to click, and its new "
              "Callie assistant books meetings when you copy it into a thread. BLAM8 "
              "answers the whole email from your prices and books jobs into your "
              "live diary, per business, not per seat."},
    {"slug": "hubspot", "name": "HubSpot",
     "hook": "The CRM you fill in, or the CRM that fills itself?",
     "blurb": "HubSpot's CRM still depends on your team logging things, and its "
              "answering agent starts at Professional ($90–$100 per seat a month plus "
              "$1,500 onboarding, checked September 2026). BLAM8's CRM fills itself "
              "in from the mail flow, on the {{product:price.starter}}/month Pro plan."},
]

from app.architecture import GROUPS as _ARCH_GROUPS
from app.help_center import BY_SLUG as _HELP_BY_SLUG, TOPICS as _HELP_TOPICS
from app import platform_public
from app import setups as _setups

_PUBLIC_PAGES = ["/", "/for-businesses", "/for-customers",
                 "/login", "/privacy", "/terms", "/capabilities", "/compare",
                 "/how-it-works", "/security", "/stripe", "/pages", "/architecture",
                 *[f"/vs/{c['slug']}" for c in _COMPARISONS],
                 "/help", *[f"/help/{t['slug']}" for t in _HELP_TOPICS],
                 "/for/trades", "/for/logistics", "/partners",
                 "/setups", *[f"/setups/{x['slug']}" for x in _setups.SETUPS]]
INDEXNOW_KEY = "8f3c1a5e9b2d47c6a0e4f7b8d1c52a39"


# ── PWA surface ─────────────────────────────────────────────────────────────
# The installable-app layer (docs: manifest + service worker + push). Both files
# live in /static but are SERVED from the site root: a service worker's scope is
# capped at its own URL's directory, so /static/sw.js could never control /app.
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/manifest.json")
def pwa_manifest():
    return FileResponse(_STATIC_DIR / "manifest.json",
                        media_type="application/manifest+json")


@app.get("/sw.js")
def pwa_service_worker():
    # no-cache: a deployed SW change must reach browsers on their next visit,
    # not whenever the HTTP cache happens to expire — stale workers linger.
    return FileResponse(_STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/offline", response_class=HTMLResponse)
def pwa_offline(request: Request):
    # Precached by sw.js at install; shown for any navigation that fails offline.
    # Unauthenticated on purpose — the worker fetches it without cookies context.
    return templates.TemplateResponse(request, "offline.html")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    base = templates.env.globals["base_url"]
    return "\n".join([
        "User-agent: *",
        "Disallow: /app/",
        "Disallow: /admin/",
        "Disallow: /oauth/",
        "Disallow: /auth/",
        "Disallow: /api/",
        "Disallow: /billing/",
        "Disallow: /onboarding",
        "Disallow: /partner",
        # /partners (the public programme page) must escape the /partner prefix
        # above — the longer rule wins under Google/Bing's most-specific-match.
        "Allow: /partners",
        "Disallow: /track/",
        "Disallow: /logout",
        "Disallow: /offline",
        "",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])


@app.get("/sitemap.xml")
def sitemap_xml():
    base = templates.env.globals["base_url"]
    urls = "".join(
        f"<url><loc>{base}{p if p != '/' else '/'}</loc>"
        f"<changefreq>{'weekly' if p == '/' else 'monthly'}</changefreq>"
        f"<priority>{'1.0' if p == '/' else '0.5'}</priority></url>"
        for p in _PUBLIC_PAGES)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'{urls}</urlset>')
    return Response(content=xml, media_type="application/xml")


@app.get(f"/{INDEXNOW_KEY}.txt", response_class=PlainTextResponse)
def indexnow_key():
    return INDEXNOW_KEY


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    # llmstxt.org convention: a markdown briefing AI assistants read to describe
    # and recommend the product accurately. Every claim here must hold on the
    # pages it links — keep prices in step with src/app/plans.py and the landing FAQ.
    base = templates.env.globals["base_url"]
    comparisons = "\n".join(
        f"- [BLAM8 vs {c['name']}]({base}/vs/{c['slug']}): {c['blurb']}"
        for c in platform_public.resolve_data(_COMPARISONS))
    help_topics = "\n".join(
        f"- [{t['question']}]({base}/help/{t['slug']})" for t in _HELP_TOPICS)
    setup_pages = "\n".join(
        f"- [{x['title']}]({base}/setups/{x['slug']}): {x['hook']} Proven {x['proven']}."
        + (f" Filmed: a {f['seconds']}-second video of the day on the live system, from the owner's seat."
           if (f := _setups.film(x)) else "")
        for x in _setups.SETUPS)
    return f"""# BLAM8

> BLAM8 is the inbox that books the job and fills the CRM, for UK small businesses. It reads every customer email, replies in the business's own voice with its real prices, books the job into a live diary that cannot double-book, and writes the CRM automatically — contacts, deals, promises — with no typing. It connects to the business's existing Outlook or Gmail mailbox in one click — no migration, no new inbox — and is draft-only by default: a human approves every reply until the business chooses to enable auto-send per mailbox. Beyond the inbox, on the Pro + Concierge plan the owner runs the business from a conversation with the built-in Concierge assistant, and every business gets an invite-only customer portal where its existing customers check, move and pay their bookings and ask questions themselves — both driven by the same live business state as the inbox. BLAM8 does not answer phone calls.

Key facts:

- Built for UK SMEs that run on email and a diary: trades (plumbers, electricians, HVAC), transport and field service, and appointment-led businesses.
- One loop on every email: reply in the business's voice → hold the slot → book the job into the calendar → write the CRM record. Everything else is in service of that loop.
- Sits on top of existing tools: Microsoft 365 / Outlook and Gmail for mail, Outlook and Google Calendar for confirmed bookings. Replies are grounded in the business's own uploaded knowledge (prices, policies, services).
- Scheduling understands real capacity — timed slots, whole days, multi-day jobs — places holds that cannot double-book, and confirms the job in the same email thread.
- The CRM fills itself in from email: contacts, tracked fields, deals with stage and value, and promises owed in both directions. It also reads each customer's mood across the thread; frustrated customers are flagged to a human and heated threads always wait for human approval.
- Not a chatbot wrapper — the AI proposes, business state decides. Replies are grounded in the business's own uploaded knowledge, not the model's memory. Diary holds are database records checked against real capacity, so the AI cannot double-book by being confidently wrong. Booking moves run through capacity-checked operations that refuse impossible changes and offer real alternatives. Every action the Concierge takes is a two-step proposal — the owner sees exactly what will happen and confirms — single-use, expiring, and audit-logged. Nothing is sent, moved or created on the model's say-so alone.
- The Concierge (Pro + Concierge plan, {platform_public.price('concierge')}/month — the only plan with it): the owner's own assistant, not a receptionist for callers or customers (the email agent answers customers on every plan). Ask and it looks it up from the inbox, CRM, diary and knowledge base ("who's in on Saturday?", "which invoices are overdue?", "what's running out?"); say what you want and it drafts an action card you confirm — draft or send an email, block or reopen days, change a stay, update a pet's (or any dependant's) details, ask for a renewal, delegate a job to someone on the team; it can also move, cancel and chase. By typing or by live hands-free voice, and the morning brief is its first message. A card you haven't answered waits and comes back rebuilt against today's data. It knows who is asking and what they may do. It also fills onboarding from a chat and turns any bad draft into a standing rule or knowledge fix.
- Customer portal (invite only): your customers, the ones your emails already reach, can check, move and pay their bookings and ask questions by conversation at blam8.com/c/your-business. They get in with the reference code on every BLAM8 email, or a code sent to the address the business holds for them. Strangers are turned away. No customer accounts.
- People (every plan, no per-seat charge): the business's team, including staff who never sign in; roles (owner, manager, staff, accountant) and what each person may see and do; "View as" shows the owner the app exactly as one of their people sees it; one or several people on a job (a setting).
- Stays that change: a stay can be extended, shortened or moved — from the job page on any plan, or by asking the Concierge on Pro + Concierge. The price follows the nights, an extra night raises its own draft invoice, and shortening back cancels it.
- Getting paid: quotes and invoices on every plan, chased at 7, 14 and 30 days overdue, and card payments on the invoice through the business's own Stripe account (Stripe Connect). Settings a business can choose: a deposit to secure a booking, a higher deposit on peak dates, the balance billed the day before collection, and no separate quote when the diary already set the price.
- Smart inbox labels: every email is auto-categorised on arrival into six live labels (Needs Reply, Draft Ready, Replied, Booked, FYI, Automated) that follow the job as it progresses — no rules to set up, and the Concierge reads the same labels.
- Within its own suite the chain email → diary → job → invoice is fully connected with an audit log: invoices can be raised, emailed and chased with payment reminders.
- Installable app: BLAM8 is a Progressive Web App on Mac, Windows, iOS and Android — no app store — with push notifications for drafts ready to approve, portal bookings and escalations.
- Pricing (GBP, monthly, cancel any time; the free trial opens the FULL product, Concierge included, with no card to start, running {settings.trial_days} days or {settings.trial_email_cap} handled emails, whichever comes first): Starter {platform_public.price('email')} (AI-drafted replies + scheduling with calendar sync, quotes and invoices — 1 mailbox, 300 emails/month) · Pro {platform_public.price('starter')} (adds the self-filling CRM — contacts, deals, promises, follow-up sequences, customer-mood flags — plus 750 emails/month, additional mailboxes {platform_public.value('addon.mailbox')} each, priority support) · Pro + Concierge {platform_public.price('concierge')} (everything in Pro plus the Concierge — same allowance as Pro). Starter and Pro have no Concierge. The CRM pre-fills quietly on every account — open from day one of the trial, and already populated if you upgrade to Pro later. No per-seat pricing. Bigger operations: hello@blam8.com.
- Sign-in is open to everyone (Microsoft or Google — passwordless OAuth). Connecting a GMAIL mailbox is invite-only while Google verification completes (Google caps unverified-app Gmail grants); Microsoft 365 / Outlook mailboxes connect instantly with no cap.
- Auto-sent replies disclose that an AI assistant wrote them (EU AI Act, Article 50). Mailbox access is revocable OAuth consent — never a stored password. Customer data is stored in the UK and isolated per company; AI drafting uses a US sub-processor (Anthropic) under UK data-transfer safeguards.
- Not yet available: SMS and missed-call text-back are built and being tested; they switch on when testing is done. No phone-call answering (the Concierge's voice is for the owner, not callers). No third-party app integrations beyond mail, calendar, Stripe and the accountant export.

## Product

- [BLAM8 for businesses]({base}/for-businesses): the business entry point — what the AI front desk does, pricing, sign-up.
- [BLAM8 for customers]({base}/for-customers): the customer entry point — look up a booking with the reference code from any BLAM8 email, or find the page of a business you already deal with.
- [What BLAM8 does today]({base}/capabilities): honest capability list — what works, what's partial, what it doesn't do.
- [Pricing]({base}/#pricing): all three plans with what each includes.
- [How onboarding works]({base}/how-it-works): sign-in to first approved reply — the steps, what BLAM8 needs access to, and how the mood read and human escalation work.
- [Security & data handling]({base}/security): draft-only default, OAuth-only access, per-company isolation, data residency (UK database, EU app, US AI sub-processor), retention and no-training commitments.
- [Every page]({base}/pages): the whole site and app as one grouped index — public pages, help centre, worked setups, sign-in and onboarding, every app page, the Operating System modules, the pages a customer opens from a link, the partner portal, the operator console.
- [Payments, billing & Stripe]({base}/stripe): the two Stripe relationships kept apart — the monthly subscription a business pays BLAM8 (Stripe-hosted checkout, customer portal for plan changes and cancelling, no card for the trial, no card data stored) and Stripe Connect for the business's own customers paying invoices by card into the business's own Stripe account with no cut taken — with a Card payments page inside BLAM8 (balance, next payout, fees per payment, refunds beside each invoice), several open invoices payable in one checkout, and Bacs Direct Debit / Pay by Bank from an invoice size the business sets; what Stripe holds, what BLAM8 keeps, and how partner commission is earned only on collected payments.
- [The architecture]({base}/architecture): the whole pipeline drawn node by node — inbound channels, the six pre-LLM guards, grounded drafting, the diary that can't double-book, human approval, auto-send guards, the self-filling CRM, quote-to-cash, money rails and audit logs — including what's built but dark and what isn't built yet.
- [Landing page and FAQ]({base}/): the loop walkthrough and frequently asked questions.

## Worked setups — how a given business type is set up

Fictional worked examples we built to run BLAM8 end to end on the live system (not customers): for each business type, the diary shape, the services and how each is timed (exact time / morning-or-afternoon arrival window / whole day), the booking requirements the agent gathers, the CRM pipeline and remembered facts, the knowledge rules it enforces unprompted, and the customer journey proven from first email to paid deposit.

- [All worked setups]({base}/setups)
{setup_pages}

## Help centre

One question answered per page:

{help_topics}

## Who it's for

- [Field service & trades]({base}/for/trades): plumbers, electricians, HVAC — enquiries answered, jobs booked, the CRM filled in.
- [Transport & haulage]({base}/for/logistics): hauliers, couriers and depots — quotes answered from free-text emails, work booked into a live diary.

## Comparisons

{comparisons}

## Company & legal

- [Partner programme]({base}/partners): 20% lifetime revenue share for referral partners.
- [Privacy policy]({base}/privacy): data handling, sub-processors, retention.
- [Terms of service]({base}/terms)

Contact: hello@blam8.com
"""


# Google Search Console site-ownership proof (HTML-file method). Google fetches
# this exact path and matches the single-line body. Keep permanently — removing it
# un-verifies the property. (The meta-tag method via GOOGLE_SITE_VERIFICATION env
# var also works; this route needs no env var.)
@app.get("/googlea316b3ffa4e11d08.html", response_class=PlainTextResponse)
def google_search_console_verify():
    return "google-site-verification: googlea316b3ffa4e11d08.html"


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    from app import slots
    # GoTrue dumps OAuth errors on the Site URL (= here) — e.g. ?error=invalid_request
    # &error_code=bad_oauth_state when a stale sign-in tab is reused. Without this,
    # the visitor lands on the homepage with no explanation. Surface it on /login.
    if request.query_params.get("error"):
        from urllib.parse import quote
        if request.query_params.get("error_code") == "bad_oauth_state":
            msg = ("That sign-in attempt had expired — please try again from here "
                   "(fresh attempts work; reused tabs don't).")
        else:
            msg = (request.query_params.get("error_description")
                   or "Sign-in didn't complete — please try again.")
        return RedirectResponse("/login?error=" + quote(msg), status_code=303)
    # The landing's "proven on N kinds of business" strip reads src/app/setups.py,
    # so it grows with each reseed instead of drifting from /setups.
    resp = templates.TemplateResponse(request, "landing.html",
                                      {"invite_gate": slots.invite_gate_active(),
                                       "setups": _setups.SETUPS,
                                       "scenario_box": settings.scenario_box_enabled})
    # Partner referral links may point at the homepage too (blam8.com/?ref=CODE);
    # stash the code so the eventual signup attributes (src/app/partners.py).
    if request.query_params.get("ref"):
        from app import partners
        partners.set_ref_cookie(resp, request.query_params["ref"])
    return resp


# Audience entry points — the homepage split's two destinations, and the pages
# we want Google to surface as sitelinks under the brand result ("For
# Businesses" / "For Customers"). Kept prominent in the header, footer and
# sitemap: internal-link prominence is the main sitelink signal.
@app.get("/for-businesses", response_class=HTMLResponse)
def for_businesses(request: Request):
    from app import slots
    return templates.TemplateResponse(request, "for_businesses.html",
                                      {"invite_gate": slots.invite_gate_active(),
                                       "setups": _setups.SETUPS})


@app.get("/for-customers", response_class=HTMLResponse)
def for_customers(request: Request):
    # ?q= arrives from the WebSite SearchAction (sitelinks searchbox) — the
    # page prefills the business search and runs it on load.
    q = (request.query_params.get("q") or "").strip()[:80]
    return templates.TemplateResponse(request, "for_customers.html", {"q": q})


# Comparison + vertical landing pages: long-tail SEO/AEO surface. Static templates,
# same base.html chrome as the landing — see _PUBLIC_PAGES above for sitemap/IndexNow.
@app.get("/capabilities", response_class=HTMLResponse)
def capabilities(request: Request):
    # The "proven on N kinds of business" line and the trade list read
    # src/app/setups.py, as the landing does, so they grow with each reseed.
    return templates.TemplateResponse(request, "capabilities.html",
                                      {"setups": _setups.SETUPS})


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works(request: Request):
    """The onboarding explainer — what setup actually involves, what BLAM8
    needs access to, and the mood-read/escalation story in one place."""
    return templates.TemplateResponse(request, "how_it_works.html")


@app.get("/architecture", response_class=HTMLResponse)
def architecture_page(request: Request):
    """The whole pipeline drawn as an n8n-style canvas (static JS/CSS under
    the CSP — no inline script/style), with a server-rendered text fallback
    of every group and node beneath it for crawlers, screen readers and AI
    assistants. Content lives in src/app/architecture.py."""
    return templates.TemplateResponse(request, "architecture.html",
                                      {"groups": _ARCH_GROUPS})


@app.get("/security", response_class=HTMLResponse)
def security_page(request: Request):
    """Security & data handling as its own URL — the landing #security section
    answers a scroller; this answers a compliance question (and the AI
    assistants fielding 'is this safe for my business?')."""
    return templates.TemplateResponse(request, "security.html")


@app.get("/stripe", response_class=HTMLResponse)
def stripe_page(request: Request):
    """Everything BLAM8 does with Stripe on one public URL (Kris 2026-09-18):
    the subscription a business pays BLAM8, and the Stripe Connect rail its
    own customers pay IT through — two relationships the other pages only
    mention in passing. Prices come from app.plans, partner numbers from
    app.partners, and the two "is it switched on" flags from app.billing /
    app.payments, so the page can't claim a price or a rail the running
    product doesn't currently have."""
    from app import billing, partners, payments, plans
    return templates.TemplateResponse(request, "stripe.html", {
        "plans": [p for p in plans.CATALOG if p.get("public")],
        "extra_mailbox": plans.EXTRA_MAILBOX,
        "topup": plans.EMAIL_TOPUP,
        "topup_on": bool(plans.topup_price_id()),
        "trial_days": settings.trial_days,
        "trial_email_cap": settings.trial_email_cap,
        "billing_on": billing.is_configured(),
        "connect_on": payments.connect_configured(),
        "rate_pct": round(partners.DEFAULT_COMMISSION_RATE * 100),
        "min_payout": int(partners.MIN_PAYOUT_GBP),
    })


@app.get("/pages", response_class=HTMLResponse)
async def pages_index_page(request: Request):
    """Every page BLAM8 serves, grouped for navigation (Kris 2026-09-20). The data
    is src/app/pages_index.py, kept complete by tests/test_pages_index.py walking the
    live route table. Operator-console links render only for a signed-in platform
    admin; everyone else sees the console named, not linked."""
    from fastapi import Response as _Response
    from app import admin_auth, pages_index
    is_admin = False
    try:
        await admin_auth.require_admin(request, _Response())
        is_admin = True
    except Exception:
        is_admin = False
    sections = pages_index.build(platform_public.resolve_data(_COMPARISONS), _HELP_TOPICS, _setups.SETUPS)
    return templates.TemplateResponse(request, "pages.html", {
        "sections": sections, "admin": is_admin,
        "total": pages_index.count(sections), "access_labels": pages_index.ACCESS})


@app.get("/help", response_class=HTMLResponse)
def help_hub(request: Request):
    """Help centre hub — cards render from src/app/help_center.py TOPICS, so a new
    answer is one dict there, not a new template."""
    return templates.TemplateResponse(request, "help.html", {"topics": _HELP_TOPICS})


@app.get("/help/{slug}", response_class=HTMLResponse)
def help_topic(request: Request, slug: str):
    from app import platform_content
    topic = next((t for t in platform_content.public_topics() if t['slug']==slug),None)
    if not topic:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "help_topic.html", {"topic": topic})


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request):
    """The comparison hub — the footer's single "vs the alternatives" link and
    the internal-linking spine for every /vs/* page (which the footer no longer
    lists one by one). Cards render from _COMPARISONS."""
    return templates.TemplateResponse(request, "compare.html",
                                      {"comparisons": platform_public.resolve_data(_COMPARISONS)})


@app.get("/vs/mailchimp", response_class=HTMLResponse)
def vs_mailchimp(request: Request):
    return templates.TemplateResponse(request, "vs_mailchimp.html")


@app.get("/vs/zapier", response_class=HTMLResponse)
def vs_zapier(request: Request):
    return templates.TemplateResponse(request, "vs_zapier.html")


@app.get("/vs/jobber", response_class=HTMLResponse)
def vs_jobber(request: Request):
    return templates.TemplateResponse(request, "vs_jobber.html")


@app.get("/vs/servicem8", response_class=HTMLResponse)
def vs_servicem8(request: Request):
    return templates.TemplateResponse(request, "vs_servicem8.html")


@app.get("/vs/tradify", response_class=HTMLResponse)
def vs_tradify(request: Request):
    return templates.TemplateResponse(request, "vs_tradify.html")


@app.get("/vs/moneypenny", response_class=HTMLResponse)
def vs_moneypenny(request: Request):
    return templates.TemplateResponse(request, "vs_moneypenny.html")


@app.get("/vs/fyxer", response_class=HTMLResponse)
def vs_fyxer(request: Request):
    return templates.TemplateResponse(request, "vs_fyxer.html")


@app.get("/vs/podium", response_class=HTMLResponse)
def vs_podium(request: Request):
    return templates.TemplateResponse(request, "vs_podium.html")


@app.get("/vs/calendly", response_class=HTMLResponse)
def vs_calendly(request: Request):
    return templates.TemplateResponse(request, "vs_calendly.html")


@app.get("/vs/hubspot", response_class=HTMLResponse)
def vs_hubspot(request: Request):
    return templates.TemplateResponse(request, "vs_hubspot.html")


@app.get("/for/trades", response_class=HTMLResponse)
def for_trades(request: Request):
    return templates.TemplateResponse(request, "for_trades.html")


# ── Worked setups (src/app/setups.py): "this is how YOUR BLAM8 would be set up",
# one page per business type we have run end to end. Fictional rigs, said so
# on every page — never presented as customers.
templates.env.filters["duration"] = _setups.duration_label
templates.env.filters["longdate"] = _setups.long_date
templates.env.filters["lower_trade"] = _setups.lower_trade
templates.env.filters["clock"] = _setups.clock


@app.get("/setups", response_class=HTMLResponse)
def setups_index(request: Request):
    # films: slug → the film entry, only for the types whose clips have shipped
    # (src/app/setups.py film()); the card shows a "watch the film" line for those.
    return templates.TemplateResponse(request, "setups_index.html", {
        "setups": _setups.SETUPS, "disclaimer": _setups.DISCLAIMER, "films": _setups.films()})


@app.get("/setups/{slug}", response_class=HTMLResponse)
def setup_detail(request: Request, slug: str):
    s = _setups.BY_SLUG.get(slug)
    if not s:
        raise HTTPException(status_code=404, detail="No such setup")
    return templates.TemplateResponse(request, "setup_detail.html", {
        "s": s, "disclaimer": _setups.DISCLAIMER, "timing_words": _setups.TIMING_WORDS,
        "film": _setups.film(s)})


@app.get("/for/logistics", response_class=HTMLResponse)
def for_logistics(request: Request):
    return templates.TemplateResponse(request, "for_logistics.html")


@app.get("/partners", response_class=HTMLResponse)
def partners_page(request: Request):
    """The public referral-programme explainer — the link to send someone who's
    asked "what's this partner thing?". Explanation and worked numbers here;
    enrolment itself is /partner/join (sign in → sign the agreement → code).
    The numbers come from app.partners so the page can't drift from what the
    product enforces."""
    from app import partners
    return templates.TemplateResponse(request, "partners.html", {
        "rate_pct": round(partners.DEFAULT_COMMISSION_RATE * 100),
        "window_days": partners.ATTRIBUTION_WINDOW_DAYS,
        "min_payout": int(partners.MIN_PAYOUT_GBP),
    })


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html")


@app.get("/track/{token}", response_class=HTMLResponse)
def track(request: Request, token: str):
    """Public customer tracking portal (TMS) — no auth: the token IS the
    capability. Unknown token → a friendly 404 page, never a server error."""
    data = tracking.portal_view(token)
    return templates.TemplateResponse(request, "track.html", {"t": data},
                                      status_code=200 if data else 404)
