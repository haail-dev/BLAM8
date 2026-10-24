"""The BLAM8 dashboard — a mirrored email client (Inbox · Sent).

Every action is wired to the automation backend, and email_log gives full
traceability. Gated by Supabase Auth: each route resolves the logged-in user's
tenant via `require_tenant`, which also yields a JWT-scoped Supabase client so
dashboard reads/writes go through Row-Level Security (defense in depth).
"""
import uuid
import asyncio
import contextvars
import csv
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from time import perf_counter
from app.startup_timing import instrument
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile as FormUploadFile
from urllib.parse import quote_plus

from app import setup_forms, knowledge_forms

from app import (
    diary_config,agent, billing, calendar_sync, compliance, crm, crm_extract, crm_fields,
                 crm_intel, crm_visibility, db, debrief, deck, diary_config, driver, events, events_os,
                 health,
                 inbox_labels,
                 intake, invoice_config, invoice_export, invoicing,
                 concierge_settings, mailbox, money, navcache, notify_prefs, oauth, partners, people, pipeline, planner,
                 plans, pod, portal,
                 practice_config,
                 pricing, scheduling, services, sms, spreadsheets, storage, suppression,
                 tms, trace, tracking, usage, voice_learning)
from app import edit_review
from app import knowledge as kb
from app import trial
from app import setup_handoff
from app import sign_off
from app.config import settings
from app.dates import pretty_date
from app.auth import has_concierge, require_onboarded, require_subscription, TenantCtx, clear_session_cookies
from app.security import require_csrf, require_csrf_header
from app.templating import templates
from fastapi.concurrency import run_in_threadpool

# The access gate rides on the whole /app router. It's a pass-through while
# REQUIRE_SUBSCRIPTION_GATE is off (the launch state); once on, it bounces a
# tenant whose free trial has lapsed (and who hasn't subscribed) to
# /billing/plans. Individual handlers keep depending on require_onboarded for
# their ctx — FastAPI caches the shared require_onboarded sub-dependency, so this
# adds no extra work per request.
def _page_prewarm(request: Request, ctx: TenantCtx = Depends(require_onboarded)) -> None:
    """Start the shared per-page reads the moment sign-in is checked, so they run while the page's
    own handler does its reads, not after it (a page used to pay its data wave and THEN the nav
    chrome, tour, setup strip and draft count, one after the other). Only for page navigations
    (Accept: text/html); fetches and fragments ask for */* or JSON and skip it. Cheap when warm."""
    if request.method == "GET" and "text/html" in (request.headers.get("accept") or ""):
        _prewarm_ctx(ctx)


from app.device_sync import SyncRoute
router = APIRouter(prefix="/app", tags=["app"], route_class=SyncRoute,
                   dependencies=[Depends(require_subscription), Depends(_page_prewarm)])
log = logging.getLogger(__name__)

# Home, the Inbox and the nav chrome fan their independent Supabase reads out on
# this pool instead of paying them one round trip after another. The workers are
# I/O-bound — each spends its life waiting on one HTTPS response — so the pool is
# sized for several simultaneous page loads (home fans ~7 wide, the inbox 3, the
# chrome 3) rather than for CPU. A pool too small silently puts the serial chain
# back. The client's httpx pool is thread-safe and multiplexes over one HTTP/2
# connection, so a wide fan costs no extra handshakes.
_FAN = ThreadPoolExecutor(max_workers=32, thread_name_prefix="page-fan")
# The pre-warm tasks run here, NOT on _FAN: one of them (the chrome fill) fans out on _FAN and waits
# for it, and a task that waits on its own pool can deadlock when the pool is full.
_WARM = ThreadPoolExecutor(max_workers=16, thread_name_prefix="page-warm")


def _chrome(ctx: TenantCtx) -> dict:
    """The nav-chrome reads every page repeats — modules, tenant row, enterprise.
    Cached per tenant (navcache, 60s; module toggles invalidate through src/app/db.py)
    so a navigation stops paying these round trips."""
    # Nav flags come from VISIBLE modules (active + preview): a founding-partner
    # preview lights the same surfaces; the agent stays off (pipeline gates on
    # module_active). preview_mods drives the "you're previewing" banner.
    # Three independent round trips — in flight together, not one after another.
    # This runs on the critical path of EVERY page whenever the 60s key is cold,
    # so its wall clock is one round trip, not three.
    f_preview = _FAN.submit(db.preview_modules, ctx.tenant_id, client=ctx.client)
    f_active = _FAN.submit(db.active_modules, ctx.tenant_id, client=ctx.client)
    f_tenant = _FAN.submit(db.get_tenant, ctx.tenant_id, client=ctx.client)
    preview_mods = f_preview.result()
    # Active on its own, not just folded into the union: surfaces that gate on
    # ACTIVE (the agent can only act on what is switched on, never on a preview)
    # would otherwise have to re-query for it.
    active_mods = f_active.result()
    mods = active_mods | preview_mods
    # Nav-chrome data (enterprise / Mailboxes visibility) is best-effort: a
    # tenant-row read failure must never 500 every page for a menu flag. On
    # failure we fall back to the id-based enterprise check and hide Mailboxes.
    try:
        tenant = f_tenant.result() or {}
    except Exception:
        tenant = {}
    enterprise = db.is_enterprise(tenant or ctx.tenant_id, client=ctx.client)
    return {"preview_mods": preview_mods, "active_mods": active_mods, "mods": mods,
            "tenant": tenant, "enterprise": enterprise}


def _setup_prefill(ctx: TenantCtx, page: str, fresh: bool) -> dict | None:
    """Held-for-later facts for one settings page (src/app/setup_held.py), handed
    to the page's form by the shell on arrival — {page, values, fresh}.
    `fresh` = the page has never been saved, so held values may fill every
    field; a saved page takes held values into EMPTY fields only (an owner's
    saved value is never overwritten). None when nothing is held."""
    from app import setup_held
    values = setup_held.for_page(setup_held.load(ctx.tenant_id, client=ctx.client), page)
    return {"page": page, "values": values, "fresh": bool(fresh)} if values else None


def _held_used(ctx: TenantCtx, page: str) -> None:
    """The page was saved: its held facts are done (best-effort stamp), and
    the setup strip's cached position is dropped so "done" shows at once."""
    from app import setup_held
    setup_held.mark_used(ctx.tenant_id, page, client=ctx.client)
    _bust_setup_strip(ctx)


def _setup_strip_ctx(ctx: TenantCtx, tenant: dict) -> dict | None:
    """One flow of nine (app/setup_handoff.strip): the tuning steps with live
    done flags, cached briefly (every Save busts the tenant's keys through
    _held_used's neighbour below) so a page never pays five reads twice."""
    try:
        tcfg = navcache.get_or_fill(
            "t:" + ctx.tenant_id + ":tcfg-strip", 15.0, _fill_tcfg_strip(ctx))
        step_list = navcache.get_or_fill(
            "t:" + ctx.tenant_id + ":setupsteps", 15.0, _fill_setup_steps(ctx))
        return {"steps": step_list, "onboarded_at": tenant.get("onboarded_at"),
                "dismissed_at": tcfg.get("settings_handoff_dismissed_at")}
    except Exception:
        return None


def _setup_walk_active(ctx: TenantCtx, tenant: dict) -> bool:
    raw = _setup_strip_ctx(ctx, tenant)
    try:
        return bool(raw and not raw.get("dismissed_at") and setup_handoff.first_open(raw["steps"]))
    except Exception:
        return False


def _tour_for(ctx: TenantCtx, tenant: dict, page: str, force: bool = False,
              after: str | None = None) -> tuple[str | None, str]:
    """The first walk of `page` (src/app/static/tour.js), owed once every setup
    step is done and the page has never been toured — tenant_config.tours
    keeps the stamp per ACCOUNT (a phone and a laptop see it once between
    them). Returns (page or None, the dependant plural for the copy)."""
    try:
        if not force:
            raw = _setup_strip_ctx(ctx, tenant)
            steps = (raw or {}).get("steps") or []
            if not steps or not all(s.get("done") for s in steps):
                return None, ""
            tcfg = navcache.get_or_fill(
                "t:" + ctx.tenant_id + ":tcfg-strip", 15.0,
                lambda: db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {})
            tours = tcfg.get("tours") or {}
            guided = (tcfg.get('onboarding_state') or {}).get('guide') or {}
            if guided.get('status') == 'active':
                return None, ''
            if tours.get(page):
                return None, ""
            # An orientation introduces the page; it does not explain every
            # legacy tour topic or certify unseen topics as completed.
            if after and not tours.get(after):
                return None, ""          # one page at a time, in order (Today first)
        plural = ""
        try:
            groups = db.list_requirement_groups(ctx.tenant_id, client=ctx.client)
            plural = str((groups or [{}])[0].get("label_plural") or "").strip().lower()
        except Exception:
            plural = ""
        return page, plural
    except Exception:
        return None, ""


@router.post("/settings/drafting", dependencies=[Depends(require_csrf)])
def settings_drafting(mode: str = Form("review"), ctx: TenantCtx = Depends(require_onboarded)):
    """The drafting rung on its own (the Inbox walk's last bubble, Kris
    2026-09-13): hold / review / auto — writes ONLY those two flags, so a
    partial post never wipes the rest of Settings the way the main form's
    field map would."""
    mode = (mode or "").strip().lower()
    if mode not in ("hold", "review", "auto"):
        mode = "review"
    db.upsert_tenant_config(ctx.tenant_id, {"draft_hold": mode == "hold", "auto_send": mode == "auto"},
                            client=ctx.client)
    return JSONResponse({"ok": True, "mode": mode})


@router.post("/tour/done", dependencies=[Depends(require_csrf)])
def tour_done(page: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    """The walk of `page` is done (Done, Skip or the last box tapped): stamp
    it on the account so it never shows again. Best-effort."""
    key = "".join(ch for ch in page.lower() if ch.isalnum() or ch == "_")[:24]
    if key:
        try:
            tcfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
            tours = dict(tcfg.get("tours") or {})
            tours[key] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.upsert_tenant_config(ctx.tenant_id, {"tours": tours}, client=ctx.client)
            _bust_setup_strip(ctx)
        except Exception:
            log.warning("tour stamp failed (tenant=%s, page=%s)", ctx.tenant_id, key, exc_info=True)
    return JSONResponse({"ok": True, "page": key})


def _bust_setup_strip(ctx: TenantCtx) -> None:
    navcache.invalidate("t:" + ctx.tenant_id + ":setupsteps")
    navcache.invalidate("t:" + ctx.tenant_id + ":tcfg-strip")


# ── People (docs/plan-people.md): whose BLAM8 this is, and which doors they
# may open. Cached per USER inside the tenant — a role is not a property of
# the business, and two people on the same account must never share this read.
def _me(ctx: TenantCtx) -> dict | None:
    """The signed-in person's membership, or None for an account that predates
    People (its single user owns it — app/people.can_open reads None that way)."""
    if not ctx.user_id:
        return None
    def _load():
        row = db.get_member(ctx.tenant_id, ctx.user_id)
        return people.member(row) if row else None
    try:
        return navcache.get_or_fill("tm:" + ctx.tenant_id + ":" + ctx.user_id, 60.0, _load)
    except Exception:
        # Fail CLOSED (security review M1): a failed read used to return None,
        # which every door reads as "the account's own owner".
        log.warning("member read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return people.member({"role": "staff", "active": False, "scopes": {}})


# The dashboard's `active` key → the door it belongs to. Anything unmapped is
# not gated by People (a module's own settings page, the modules hub).
_ACTIVE_DOORS = {
    "home": "today",
    "conversations": "inbox", "inbox": "inbox", "drafts": "inbox", "sent": "inbox",
    "diary": "diary", "board": "diary", "fsm": "diary",
    "crm": "crm", "crm_locked": "crm", "crm_companies": "crm", "crm_pipeline": "crm",
    "crm_sequences": "crm", "crm_fields": "crm", "crm_settings": "crm", "crm_tasks": "crm",
    "crm_reports": "crm", "outreach": "crm",
    "money": "money", "invoices": "money", "quotes": "money", "invoice_settings": "money",
    "payments": "money",
    "settings": "settings",
    # Practice (security review 2026-09, M40): appointments are the diary's —
    # an accountant has no business on the clinical board. The notes on it are
    # behind "See private details" as well (app/practice_routes).
    "practice": "diary",
}


# CRM pages that are the WHOLE BOOK rather than a customer: every company,
# every deal, the sequences, the reports, and the definitions of what the
# business tracks. A staff member whose CRM is the customers behind their own
# work has no version of these — a "pipeline" of a subset is not a pipeline —
# so they land on their contacts instead (People slice 3).
_CRM_WHOLE_BOOK = ("crm_companies", "crm_pipeline", "crm_sequences", "crm_tasks",
                   "crm_reports", "crm_fields", "crm_settings", "outreach")


def require_page(ctx: TenantCtx, active: str) -> None:
    """A door this person may not open sends them to one they may — never a
    wall, and never a 404 that reads as "it's broken" (docs/plan-people.md)."""
    door = _ACTIVE_DOORS.get(active)
    if not door:
        return
    m = _me(ctx)
    if not people.can_open(m, door):
        raise HTTPException(status_code=303,
                            headers={"Location": _DOOR_HREFS.get(people.home_page(m), "/app/")})
    if active in _CRM_WHOLE_BOOK and crm_visibility.narrows_contacts(m):
        raise HTTPException(status_code=303, headers={"Location": "/app/crm"})


_DOOR_HREFS = {"today": "/app/", "inbox": "/app/conversations", "diary": "/app/diary",
               "crm": "/app/crm", "money": "/app/money", "settings": "/app/settings"}


def _say_ctx(ctx: TenantCtx, kind: str, about_id: str, label: str) -> dict:
    """The "Tell someone" control and the record beneath it, for whatever page
    is being rendered (docs/plan-people.md slice 3). Best-effort: a read
    failure costs the control, never the page."""
    from app import handoffs
    try:
        people_rows = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    except Exception:
        people_rows = []
    try:
        notes = db.list_staff_notes_about(ctx.tenant_id, kind, str(about_id)) if about_id else []
    except Exception:
        notes = []
    # Only the OTHER active people are anyone to tell (Kris 2026-09-20: the
    # control shows once a second person is on the account) — never me,
    # never someone deactivated.
    me_id = str(ctx.user_id or "")
    say_others = [m for m in people_rows
                  if m.get("active") and m.get("user_id") and m["user_id"] != me_id]
    return {"say_people": people_rows, "say_others": say_others,
            "about_kind": kind, "about_id": str(about_id or ""),
            "about_label": label, "about_word": handoffs.ABOUT_LABELS.get(kind, "record"),
            "about_notes": [n for n in notes if not n.get("cancelled_at")]}


def require_can(ctx: TenantCtx, page: str, ability: str) -> None:
    """The write side of a door (docs/plan-people.md; Kris 2026-09-13: "each of
    these tick boxes should have more options once ticked — more control
    basically"). A door on its own is reading; changing something behind it is
    a tick of its own. Refused with 403 rather than a redirect: this is a form
    post, and quietly landing somewhere else would read as "it saved"."""
    if not people.can_do(_me(ctx), page, ability):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to do that. Ask whoever runs this account.")


@instrument("navigation_context")
def _fill_drafts(ctx: TenantCtx):
    return lambda: len(db.list_email_log(ctx.tenant_id, status="pending", client=ctx.client))


def _fill_tcfg_strip(ctx: TenantCtx):
    return lambda: db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}


def _fill_setup_steps(ctx: TenantCtx):
    return lambda: setup_handoff.load_steps(ctx.tenant_id, client=ctx.client)


def _fill_platform_admin(ctx: TenantCtx):
    return lambda: db.is_platform_admin(ctx.user_id)


def _fill_member_count(ctx: TenantCtx):
    return lambda: len(db.list_members(ctx.tenant_id))


def _prewarm_ctx(ctx: TenantCtx, me=...) -> list:
    """Start the cold per-page fills that _ctx would otherwise pay one after another, so they run
    beside the nav-chrome read instead of behind it. Each task fills EXACTLY the navcache key (same
    name, same TTL, same thunk) that _ctx reads right after, so the original code below finds it
    warm; a task that fails just leaves the key cold and _ctx reruns the read and fails or succeeds
    as it always did. Nothing here is skipped for a warm cache (a hit costs nothing) and nothing is
    submitted when the cache is off (tests)."""
    if navcache._disabled:
        return []
    t = ctx.tenant_id

    def warm(key, ttl, fill):
        def run():
            try:
                navcache.get_or_fill(key, ttl, fill)
            except Exception:
                pass
        return _WARM.submit(contextvars.copy_context().run, run)

    def warm_members():
        # Only an owner who is not already viewing as someone reads the member count (see _ctx).
        who = _me(ctx) if me is ... else me
        if not getattr(ctx, "viewing_as", None) and (who is None or people.normalise_role(who.get("role")) == "owner"):
            navcache.get_or_fill("pc:" + t, 120.0, _fill_member_count(ctx))

    def safe(fn):
        def run():
            try:
                fn()
            except Exception:
                pass
        return _WARM.submit(contextvars.copy_context().run, run)
    futures = [
        warm("t:" + t + ":chrome", 60.0, lambda: _chrome(ctx)),
        warm("t:" + t + ":drafts", 20.0, _fill_drafts(ctx)),
        warm("t:" + t + ":tcfg-strip", 15.0, _fill_tcfg_strip(ctx)),
        warm("t:" + t + ":setupsteps", 15.0, _fill_setup_steps(ctx)),
        safe(warm_members),
    ]
    if ctx.user_id:
        futures.append(warm("pa:" + ctx.user_id, 600.0, _fill_platform_admin(ctx)))
    return futures


def _discovery_scope(ctx: TenantCtx) -> str:
    from app import concierge_discovery   # late: it pulls in the whole Concierge
    return concierge_discovery.scope_key(ctx)


def _ctx(active: str, ctx: TenantCtx, **extra) -> dict:
    # People (2026-09-13): every page a person can navigate to passes through
    # here, so this is the one place the doors are enforced.
    require_page(ctx, active)
    me = _me(ctx)
    # The independent cold fills start now and overlap the chrome read just below; everything after
    # reads the same keys (see _prewarm_ctx), so with warm caches this costs nothing at all.
    warm = _prewarm_ctx(ctx, me)
    ch = navcache.get_or_fill("t:" + ctx.tenant_id + ":chrome", 60.0, lambda: _chrome(ctx))
    for f in warm:
        f.result()
    # Draft count on its own short clock: it moves with the inbox, and the SSE
    # push that announces a new draft also busts this key (src/app/sse.py).
    draft_count = navcache.get_or_fill(
        "t:" + ctx.tenant_id + ":drafts", 20.0, _fill_drafts(ctx))
    preview_mods, mods, tenant = ch["preview_mods"], ch["mods"], ch["tenant"]
    enterprise = ch["enterprise"]
    preview_names = [m["name"] for m in MODULES if m["id"] in preview_mods]
    # TMS bundles Fleet · Capacity · Routing · Planner + the baseline transport
    # surfaces (the fleet+routing merge): one module row lights the whole
    # operation. fleet_on/routing_on are kept as aliases of tms_on so the existing
    # templates keep working unchanged.
    tms_on = "tms" in mods
    # The operator's own account (platform_admins membership) gets an Admin
    # console entry in the burger menu — invisible to every other account.
    # Best-effort like the tenant read above: a lookup failure only hides the
    # link (the /admin routes have their own wall, admin_auth.require_admin).
    # Cached long: platform_admins changes by migration, not by anything a
    # tenant does in-app.
    try:
        platform_admin = bool(ctx.user_id and navcache.get_or_fill(
            "pa:" + ctx.user_id, 600.0, _fill_platform_admin(ctx)))
    except Exception:
        platform_admin = False
    # View as (src/app/viewas.py): the account's owner, with somebody else on the
    # account to look through the eyes of. Not while already viewing (the
    # banner carries the way out then).
    view_as_ok = False
    if not getattr(ctx, "viewing_as", None) and (me is None or people.normalise_role(me.get("role")) == "owner"):
        try:
            view_as_ok = navcache.get_or_fill(
                "pc:" + ctx.tenant_id, 120.0, _fill_member_count(ctx)) > 1
        except Exception:
            view_as_ok = False
    try:
        guide_meta = (navcache.get_or_fill('t:'+ctx.tenant_id+':tcfg-strip', 15.0,
            _fill_tcfg_strip(ctx)).get('onboarding_state') or {}).get('guide')
    except Exception:
        guide_meta = None
    return {"active": active, "draft_count": draft_count,
            "guided_setup_meta": guide_meta,
            "platform_admin": platform_admin,
            "view_as_ok": view_as_ok,
            "scheduling_on": "scheduling" in mods,
            "tms_on": tms_on,
            "fleet_on": tms_on,
            "routing_on": tms_on,
            "fsm_on": "fsm" in mods,
            # Practice OS (docs/os/practice.md §10.1) — the appointment-vertical add-on.
            "practice_on": "practice" in mods,
            # Storefront OS (docs/os/storefront.md §10) — the shop-vertical add-on; its
            # /app/shop daily surface is the storefront tenant's home.
            "storefront_on": "storefront" in mods,
            # Assets OS (docs/os/assets.md §8/§10) — the hire-vertical add-on; its
            # /app/assets board is the assets tenant's home surface.
            "assets_on": "assets" in mods,
            # Events OS (docs/os/events.md §8/§10) — the events-vertical add-on; its
            # /app/events board is the events tenant's home surface.
            "events_on": "events" in mods,
            # CRM unlocks at Core (docs/crm-vs-os-architecture.md §1) — a module
            # like scheduling since 2026-07-24; the email tier navigates without it.
            "crm_on": "crm" in mods,
            # The business Concierge (Pro + Concierge, 2026-09-25): without it
            # there is no person icon, no modal, no voice — App help is in search.
            "concierge_on": "concierge" in mods,
            # Who the Explore map saved in this browser belongs to (static/concierge_discovery.js).
            "concierge_scope": _discovery_scope(ctx) if "concierge" in mods else "",
            # People (docs/plan-people.md): the doors THIS person may open, so
            # the strip and the menu show only what their role allows. Empty
            # for an account that predates People — everything shows.
            "my_doors": list(people.pages_of(me)),
            # What they may CHANGE behind each door, so a page never renders a
            # control its reader cannot use.
            "my_can": {people.ability_key(p, a): people.can_do(me, p, a)
                       for p in people.PAGES for a, _l, _h in people.abilities_for(p)},
            "my_role": (me or {}).get("role") or "owner",
            "people_admin": people.is_admin(me),
            # Is their CRM the customers behind their own work rather than the
            # whole book? The menu reads this so it never offers a page the
            # route will bounce them off (People slice 3).
            "crm_mine": crm_visibility.narrows_contacts(me),
            # Headline referral rate for the burger menu's "Refer a business" badge —
            # single-sourced from app.partners so the menu can't quote a stale number.
            "partner_rate_pct": round(partners.DEFAULT_COMMISSION_RATE * 100),
            # Outbound/travel is a diary CAPABILITY (route + travel-time aware scheduling),
            # not a vertical — the paid "travel assistant" add-on. Off for everyone until it
            # ships; the Diary-settings toggle un-grays from this flag.
            "travel_on": "travel" in mods,
            "enterprise": enterprise,
            # The multi-mailbox surface entitlement — MUST match _require_multimailbox:
            # enterprise or any active paid plan. The nav renders Mailboxes from this,
            # not from `enterprise` (which hid a paid-for feature from non-enterprise).
            "multimailbox": enterprise or billing.has_active_subscription(tenant),
            "preview_mods": preview_mods,
            "preview_names": preview_names,
            # One flow of nine: the setup strip's raw material — the template
            # works out "here" from request.url.path (setup_handoff.strip).
            "setup_strip_raw": _setup_strip_ctx(ctx, tenant),
            "setup_strip_fn": setup_handoff.strip,
            # True while the nine-step walk still has open steps (and was not
            # hidden): the settings pages drop their cross-step links then —
            # the strip says what is next, and a link ahead is a way to skip
            # (Kris 2026-09-13).
            "setup_walk_active": _setup_walk_active(ctx, tenant),
            **extra}


@router.post("/settings/mailbox/backlog", dependencies=[Depends(require_csrf)])
async def settings_mailbox_backlog(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Explicit historical import choice; never infer the mailbox from ordering."""
    from app import backlog
    await backlog.choose_import(request, ctx)
    return RedirectResponse("/app/settings", status_code=303)


@router.post("/setup/hide", dependencies=[Depends(require_csrf)])
def setup_hide(return_to: str = Form("/app/"), ctx: TenantCtx = Depends(require_onboarded)):
    """Hide the setup strip — offered only to a tenant past the fresh window
    (setup_handoff.strip's can_hide); a fresh sign-up cannot dismiss the walk."""
    from datetime import datetime, timezone
    db.upsert_tenant_config(ctx.tenant_id, {"settings_handoff_dismissed_at":
                                            datetime.now(timezone.utc).isoformat()})
    _bust_setup_strip(ctx)
    return RedirectResponse(_safe_next(return_to), status_code=303)


# ── Mailbox helpers (enterprise multi-mailbox) ──────────────────────────────
def _mailbox_display(m: dict) -> str:
    return m.get("label") or m.get("address") or "Mailbox"


def _tenant_mailboxes(ctx: TenantCtx) -> list[dict]:
    """The tenant's mailboxes (non-secret), oldest (default) first, with their
    display names attached."""
    return [{**m, "display": _mailbox_display(m)} for m in db.list_mailboxes(ctx.tenant_id, client=ctx.client)]


def _default_mailbox_id(ctx: TenantCtx) -> str | None:
    """The tenant's default mailbox — the oldest, which is the owner's own.
    Cached briefly: it changes only when a mailbox is connected or dropped, and
    it is read on every inbox render."""
    def _read():
        boxes = db.list_mailboxes(ctx.tenant_id, client=ctx.client)
        return boxes[0]["id"] if boxes else None
    try:
        return navcache.get_or_fill("mb0:" + ctx.tenant_id, 300.0, _read,
                                    cache_if=lambda v: True)
    except Exception:
        log.warning("default mailbox read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return None


def _inbox_is_theirs(ctx: TenantCtx) -> bool:
    """Is this person's Inbox their OWN mailbox rather than the business's?
    (Kris 2026-09-14: "the inbox thing should only happen if it's a business
    email for that person".)"""
    return people.inbox_is_theirs_only(_me(ctx))


def _mail_is_theirs(ctx: TenantCtx, row) -> bool:
    """May they read this piece of mail? Mail with no mailbox on it belongs to
    the DEFAULT one — the owner's — so nothing legacy becomes theirs by
    accident."""
    me = _me(ctx)
    if not people.inbox_is_theirs_only(me):
        return True
    return people.may_read_mailbox(me, (row or {}).get("mailbox_id"),
                                   _default_mailbox_id(ctx))


def _their_mail_only(ctx: TenantCtx, rows):
    me = _me(ctx)
    if not people.inbox_is_theirs_only(me):
        return list(rows or [])
    default_id = _default_mailbox_id(ctx)
    return [r for r in (rows or [])
            if people.may_read_mailbox(me, (r or {}).get("mailbox_id"), default_id)]


def _require_their_mail(ctx: TenantCtx, entry) -> None:
    """Refused with 403 rather than a redirect: these are form posts, and
    quietly landing somewhere else would read as "it worked"."""
    if not _mail_is_theirs(ctx, entry):
        raise HTTPException(status_code=403,
                            detail="That message is in a mailbox that isn't yours.")


def _require_their_log(ctx: TenantCtx, log_id: str) -> None:
    """The same guard where the route doesn't already hold the row. Checks
    WHETHER it applies before reading anything — an owner is the overwhelming
    case and must not pay a round trip to be told they own their own inbox."""
    if not _inbox_is_theirs(ctx):
        return
    _require_their_mail(ctx, db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client))


def _selected_mailbox_id(ctx: TenantCtx, candidate: str | None) -> str | None:
    """Validate a mailbox id from a query/form param against the tenant's OWN
    mailboxes — anything else (another tenant's id, a stale id after disconnect)
    quietly falls back to None, the default mailbox."""
    if not candidate:
        return None
    boxes = db.list_mailboxes(ctx.tenant_id, client=ctx.client)
    return candidate if any(m["id"] == candidate for m in boxes) else None


def _safe_next(path: str | None) -> str:
    """Where to land after an action — only same-app relative paths, never an open redirect."""
    return path if path and path.startswith("/app/") else "/app/conversations"


def _thread_after_draft(ctx: TenantCtx, log_id: str | None) -> str:
    """Where to land after drafting a reply: the contact's thread in the Inbox, where the
    draft shows inline ready to approve — the approval queue is the Inbox, not a separate
    page. Falls back to the per-draft review page if the contact can't be resolved."""
    if not log_id:
        # Drafting declined (no-reply sender, ignored contact, self-loop guard,
        # message gone) — land on the list WITH a visible reason, never a silent
        # bounce that reads as "clicking does nothing".
        return "/app/conversations?notice=nodraft"
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    contact = (entry or {}).get("from_address")
    if not contact:
        return f"/app/drafts/{log_id}"
    return "/app/conversations/view?" + urlencode({"contact": contact})


def _draft_edited(submitted: str | None, stored: str | None) -> bool:
    """Did the human change the agent's draft before approving? Line endings are
    normalised first — browsers submit textarea content with CRLF, the stored
    draft uses LF, and that round-trip must never count as an edit."""
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    return norm(submitted) != norm(stored)


# ── Module hub (the /app home) ───────────────────────────────────────────────
# The platform registry the hub renders from. A module is DATA here: it lights up
# for a tenant when a subscriptions row (module, status='active') exists — flipping
# that row is the whole rollout, no code path changes. `always_on` marks the base
# module every tenant starts with; entries without an href are roadmap cards.
# `group` buckets the hub: 'core' = what every vertical runs (email + scheduling),
# 'addon' = a vertical bolt-on, 'soon' = roadmap. A module still lights only when a
# subscriptions row activates it. TMS is one add-on covering the whole transport
# operation (the fleet+routing merge), not a row per feature.
MODULES = (
    {"id": "email", "name": "Email Automation", "always_on": True, "group": "core",
     "href": "/app/conversations",
     "blurb": "Replies to incoming mail in your voice — Outlook & Gmail, threaded into the "
              "conversation, approval queue or full-auto."},
    {"id": "scheduling", "name": "Scheduling", "group": "core", "badge": "Next up", "href": "/app/diary",
     "blurb": "Bookings, slots and diary management — quote requests become scheduled jobs "
              "without the back-and-forth."},
    # The CRM is module-gated like scheduling. Tier ladder (Julius 2026-07-25):
    # Starter £39 = email + scheduling; the CRM unlocks on Pro £99
    # (plans.BASE_PLAN_MODULES). Below Pro the card is the TEASER: the zero-LLM
    # ingest pre-fills the CRM for every tenant, so the locked card shows what
    # it already knows (contact/company counts) — see a little, use nothing.
    # It remains the sharpest single-line differentiator BLAM8 has.
    {"id": "crm", "name": "CRM", "group": "core", "badge": "Pro", "href": "/app/crm",
     "blurb": "The CRM that fills itself in — contacts, companies and every conversation, "
              "auto-populated from your inbox. Nothing to type, ever."},
    # Invoicing was extracted from Field OS into a standalone core module (2026-07-26):
    # active for every tenant, no subscriptions row — state is computed specially in
    # modules_page. The per-vertical generate-from-completed-work builders still light
    # only with their module; standalone/concierge invoices need nothing else.
    {"id": "invoicing", "name": "Invoicing", "group": "core", "bundled": True, "href": "/app/invoices",
     "blurb": "Invoices with Pay Now links — created by the Concierge from your bookings and "
              "prices, emailed as a PDF, paid online via your own Stripe (or bank transfer), "
              "chased when overdue, and marked paid the moment the money lands."},
    {"id": "tms", "name": "Field OS · Transport", "group": "addon", "badge": "Logistics",
     "href": "/app/tms",
     "blurb": "The whole transport operation: vehicle & pallet-space capacity, routing and the "
              "load planner, job pricing, proof of delivery, a driver app, customer tracking, a "
              "live fleet view and compliance — every screen live off the diary the agent books into."},
    {"id": "fsm", "name": "Field OS · Trades", "group": "addon", "badge": "Trades",
     "href": "/app/fsm",
     "blurb": "The whole jobs operation: engineer capacity by the day, skill-matched booking, "
              "emergency triage, instant quotes and invoicing — every screen live off the diary "
              "the agent books into."},
    {"id": "practice", "name": "Practice OS", "group": "addon", "badge": "Practice",
     "href": "/app/practice",
     "blurb": "The whole patient diary: recall to every patient exactly when they're due in "
              "your voice, 48/24/2-hour reminders, no-show prediction with an extra nudge for "
              "the risky ones, and patient-history-aware replies — every screen live off the "
              "diary the agent books into."},
    {"id": "storefront", "name": "Storefront OS", "group": "addon", "badge": "Shops",
     "href": "/app/shop",
     "blurb": "The whole shop off your inbox: stock that fills itself in from a photo of the "
              "delivery note, reservations and collections, and best-before specials pushed to "
              "the regulars who buy that product — no POS, no typing."},
    {"id": "assets", "name": "Assets OS", "group": "addon", "badge": "Lettings & hire",
     "href": "/app/assets",
     "blurb": "Hire out property or equipment off your inbox: a unit registry with per-unit "
              "rates and condition, atomic bookings against every unit (never double-booked), "
              "deposits, and before/after photo-diffs that become damage-claim evidence — every "
              "screen live off the diary the agent books into."},
    {"id": "events", "name": "Events OS", "group": "addon", "badge": "Events",
     "href": "/app/events",
     "blurb": "Run every event as one flow: guests, dietaries, suppliers and staff, all filling "
              "themselves in from your inbox — and (coming next) BLAM8 chases every stakeholder to "
              "the run sheet so you never have to."},
    # Wholesale OS (docs/os/wholesale.md §10): the recurring B2B book.
    {"id": "wholesale", "name": "Wholesale OS", "group": "addon", "badge": "New",
     "href": "/app/wholesale",
     "blurb": "The whole wholesale book: standing orders that generate themselves, "
              "credit-limit-aware order confirmations, and a week-ahead fulfilment plan "
              "that flags every shortfall — all off the inbox the agent already reads."},
    # The first non-email channel (docs/modules/module-sms-channel.md): SMS + missed-call
    # text-back, cross-vertical like invoicing but bought on its own.
    {"id": "sms", "name": "Text messaging", "group": "addon", "badge": "New",
     "href": "/app/channels",
     "blurb": "Customers text your business number and get drafted-in-your-voice replies "
              "through the same inbox — plus missed-call text-back: a call you can't take "
              "gets an instant text, and the job books itself by message."},
    {"id": "voice", "name": "Voice", "group": "soon", "badge": "Coming soon",
     "blurb": "Answer and triage inbound calls, hand off to a human when it matters."},
    {"id": "whatsapp", "name": "WhatsApp", "group": "soon", "badge": "Coming soon",
     "blurb": "Reply to WhatsApp enquiries in your voice — the same draft-first approvals, "
              "diary checks and bookings as email."},
    {"id": "telegram", "name": "Telegram", "group": "soon", "badge": "Coming soon",
     "blurb": "Handle Telegram messages on the same booking brain — drafts, holds and "
              "confirmations, always with a human in the loop."},
)
from app import promise_kinds
from app import guards as _guards


def _today_strip(cfg_row: dict | None, rows: list[dict], now: datetime) -> dict:
    """View-model for home's Today's-diary strip: today's windows in diary order,
    each carrying its bookings, plus any all-day blocks that sit outside the
    window list (a calendar import on a slots diary). A closed day falls back to
    the next open day so the strip always says something useful. Pure — the
    occupancy math is diary_days'."""
    days = scheduling.diary_days(cfg_row, rows, now)
    entry = next((d for d in days if d["date"] == now.date().isoformat()), None)
    if entry is None:
        nxt = next((d for d in days if d["working"]), None)
        return {"closed": True, "label": now.strftime("%A %d %B"),
                "next_label": nxt["label"] if nxt else None}
    slot_minutes = scheduling._norm(cfg_row)["slot_minutes"]
    window_ids = {w["window"] for w in entry["windows"]}
    slots = []
    for w in entry["windows"]:
        wid = w["window"]
        # A slot is 'past' once it has ENDED — the live slot stays prominent.
        end = (scheduling._window_start(now.date(), wid)
               + timedelta(minutes=slot_minutes)) if ":" in wid else None
        slots.append({
            "label": scheduling.window_label(wid),
            "past": bool(end and end <= now),
            "bookings": [b for b in entry["bookings"] if b.get("window") == wid],
            "used": w.get("used", 0), "capacity": w.get("capacity", 0),
            "remaining": w.get("remaining", 0),
        })
    # A STAYS diary is one all-day window with N concurrent spaces (Kris
    # 2026-09-13: "it's showing 1 free slot but the stays business has 30 subject slots")
    # — the chip counts spaces, not windows, in the business's own noun.
    stay_mode = bool(entry.get("stay_mode"))
    # The diary's other use (Kris 2026-09-13): each appointment service and
    # how many are booked today — "2 meet & greets today".
    appointments = []
    if stay_mode:
        for svc in services.clean_service_types((cfg_row or {}).get("service_types")):
            n = sum(1 for b in entry["bookings"]
                    if str(b.get("resource_type") or "").strip().lower() == svc["name"].lower())
            appointments.append({"name": svc["name"], "count": n})
    noun = str(((cfg_row or {}).get("nouns") or {}).get("participant") or "").strip().lower()
    plural = (noun + ("" if noun.endswith("s") else "s")) if noun else "in"
    cap = max((s["capacity"] for s in slots), default=0)
    used = max((s["used"] for s in slots), default=0) if stay_mode else 0
    return {"closed": False, "label": entry["label"], "working": entry["working"],
            "slots": slots, "stay_mode": stay_mode,
            "capacity": cap, "used": used, "remaining": max(0, cap - used),
            "noun_plural": plural, "appointments": appointments,
            "allday": [b for b in entry["bookings"]
                       if b.get("window") not in window_ids]}


def _concierge_counts(ctx: TenantCtx) -> dict:
    """What the Concierge has waiting on this person and what she has done for
    them today — the same lists as her ⏳ and ✓ counters
    (concierge_routes.business_waiting / business_done), so the Today row and
    her badges never disagree. Best-effort: a failed read counts nothing."""
    waiting = done = 0
    try:
        waiting = (len(db.list_waiting_proposals(ctx.tenant_id, ctx.user_id, client=ctx.client))
                   + len(db.list_waiting_drafts(ctx.tenant_id, client=ctx.client))
                   + len(db.list_waiting_invoices(ctx.tenant_id, client=ctx.client)))
    except Exception:
        log.warning("today: concierge waiting read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    try:
        start = datetime.now(scheduling.DIARY_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        rows = (db.list_done_proposals(ctx.tenant_id, ctx.user_id, client=ctx.client)
                + db.list_sent_drafts(ctx.tenant_id, client=ctx.client))
        done = sum(1 for r in rows if (r.get("done_at") or "") >= start)
    except Exception:
        log.warning("today: concierge done read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    return {"waiting": waiting, "done": done}


def _todays_brief(ctx: TenantCtx, f_notes, me) -> dict | None:
    """This morning's brief for Today's top line (Kris 2026-09-25: "I had the
    notification come through on my phone but there's nowhere in app that
    displays it"). The feed row is what the × dismisses; the words are the
    Concierge's spoken turn, the push's terse list when that won't read.
    Today's only (UK), the owner's only — it talks money and every door."""
    if not people.is_admin(me):
        return None
    try:
        start = datetime.now(scheduling.DIARY_TZ).replace(hour=0, minute=0, second=0,
                                                         microsecond=0)
        rows = [r for r in (f_notes.result() or [])
                if r.get("kind") == "morning_debrief"
                and (r.get("created_at") or "") >= start.astimezone(timezone.utc).isoformat()]
        if not rows:
            return None
        row = max(rows, key=lambda r: r.get("created_at") or "")
        text = ""
        for m in db.latest_concierge_assistant_since(
                ctx.tenant_id, start.astimezone(timezone.utc).isoformat(), client=ctx.client):
            if debrief.is_brief(m.get("content")):
                text = " ".join((m.get("content") or "").split())
                break
        return {"id": row["id"], "text": text or (row.get("body") or row.get("title") or "")}
    except Exception:
        log.warning("today: brief read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return None


@router.get("/")
@instrument("home")
def app_home(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Home — the Today dashboard: who this company is (profile strip), what's
    waiting for approval (grouped by mailbox), today's board, and the usage
    meter. The module hub (the old home) lives at /app/modules.

    The page's reads used to run one after another — ~10 sequential Supabase
    round trips (the "seconds to load", Kris 2026-08-31). They fan out on the
    _FAN pool now, in two waves: everything tenant-only first, then the
    module-gated reads once the chrome says which modules are on. This route is
    sync `def`, so it already lives in Starlette's threadpool — the fan-out
    blocks worker threads, never the event loop."""
    today = datetime.now(scheduling.DIARY_TZ).date().isoformat()
    f_tenant = _FAN.submit(db.get_tenant, ctx.tenant_id, client=ctx.client)
    f_boxes = _FAN.submit(_tenant_mailboxes, ctx)
    f_convos = _FAN.submit(db.list_conversations, ctx.tenant_id, client=ctx.client)
    f_rows = _FAN.submit(db.list_bookings, ctx.tenant_id, today, client=ctx.client)
    f_invoices = _FAN.submit(db.list_invoices, ctx.tenant_id, client=ctx.client)
    # The usage meter reads the tenant row and every usage event of the billing period; a 30 s old meter
    # is as good as a live one, so the page shares one read per half minute.
    f_usage = _FAN.submit(
        navcache.get_or_fill, "t:" + ctx.tenant_id + ":usage-summary", 30.0,
        lambda: usage.summarize(ctx.tenant_id, client=ctx.client))
    # Same key _ctx reads at the end — warmed here, so the tail render is free.
    chrome = navcache.get_or_fill("t:" + ctx.tenant_id + ":chrome", 60.0,
                                  lambda: _chrome(ctx))
    mods = chrome["mods"]   # visible modules: active | preview, as everywhere

    # Wave 2 — the module-gated reads, all in flight together.
    # The Concierge row (2026-09-25): her ⏳ and ✓ counts, with the tier only.
    f_cz = _FAN.submit(_concierge_counts, ctx) if "concierge" in mods else None
    strip_wanted = ("scheduling" in mods
                    and not ({"tms", "fsm", "practice", "storefront", "assets", "events"} & mods))
    f_sched = (_FAN.submit(db.get_scheduling_config, ctx.tenant_id, client=ctx.client)
               if strip_wanted else None)
    # Wanted by every tenant now, not just CRM ones: the deck's topic order
    # lives in tenant_config, and a tenant without CRM still has a deck.
    f_cfg = _FAN.submit(db.get_tenant_config, ctx.tenant_id, client=ctx.client)
    # The notification feed, joined onto the deck's cards below. Best-effort
    # like every other section: a feed that will not read costs Today its
    # event facts, never the page.
    f_notes = _FAN.submit(
        db.list_deck_notifications, ctx.tenant_id,
        since=(datetime.now(timezone.utc) - deck.EVENT_WINDOW).isoformat(),
        client=ctx.client)
    # Anything that has STOPPED (src/app/health.py). Its own read because a dead
    # mailbox outranks everything else on this page: the deck is full of people
    # waiting for replies that are no longer being written.
    f_health = _FAN.submit(db.list_unhealthy_connections, ctx.tenant_id,
                           client=ctx.client)
    f_contacts = f_deals = f_promises = f_suggest = f_instances = f_docs = None
    if "crm" in mods:
        f_contacts = _FAN.submit(db.crm_list_contacts, ctx.tenant_id, client=ctx.client)
        f_deals = _FAN.submit(db.crm_list_deals, ctx.tenant_id, client=ctx.client)
        f_promises = _FAN.submit(db.crm_open_promises, ctx.tenant_id, client=ctx.client)
        # The record needing a hand (Kris 2026-09-21): details BLAM8 read but
        # wants confirmed, documents nobody has labelled. Best-effort reads.
        f_suggest = _FAN.submit(db.crm_pending_suggestions, ctx.tenant_id, client=ctx.client)
        f_instances = _FAN.submit(db.list_requirement_instances_all, ctx.tenant_id, client=ctx.client)
        f_docs = _FAN.submit(db.list_unlabelled_documents, ctx.tenant_id, client=ctx.client)
        f_groups = _FAN.submit(db.list_requirement_groups, ctx.tenant_id, client=ctx.client)
    # A chase already staged, so an overdue card says "reminder drafted" and
    # leads to the draft rather than to Money.
    f_chases = _FAN.submit(db.list_pending_invoice_chases, ctx.tenant_id, client=ctx.client)
    f_events = (_FAN.submit(events_os.board_view, ctx.tenant_id, client=ctx.client)
                if "events" in mods else None)

    t = f_tenant.result() or {}
    boxes = f_boxes.result()
    default_id = boxes[0]["id"] if boxes else None

    # The approval queue, grouped by mailbox. Legacy rows carry a null mailbox_id
    # and belong to the DEFAULT (oldest) box — same rule as the conversations page.
    convos = f_convos.result()
    waiting = [c for c in convos if c.get("state") == "pending"]
    by_box: dict = {}
    for c in waiting:
        by_box.setdefault(c.get("mailbox_id") or default_id, []).append(c)
    queue = [{"box": m, "items": by_box.pop(m["id"], [])} for m in boxes]
    leftovers = [c for orphans in by_box.values() for c in orphans]
    if queue:
        queue[0]["items"].extend(leftovers)   # a disconnected box's drafts → default box
    elif leftovers:                           # single-mailbox tenant, no connection row
        queue = [{"box": None, "items": leftovers}]
    queue = [g for g in queue if g["items"]]

    # Today on the board — bookings are the shared diary unit across office diary,
    # transport board and team member board, so one query covers whichever is live.
    # list_bookings returns everything from today FORWARD. The board and the
    # strip below mean TODAY and narrow it; the deck means "what's next" and
    # needs the rest — reusing the narrowed list is why an appointment tomorrow
    # morning never reached a card (Kris 2026-09-04).
    diary_ahead = f_rows.result()
    rows = [b for b in diary_ahead if b.get("booking_date") == today]
    board = {"total": len(rows),
             "confirmed": sum(1 for b in rows if b.get("status") == "confirmed"),
             "proposed": sum(1 for b in rows if b.get("status") != "confirmed")}

    # Today's diary strip — office-diary tenants see the day slot-by-slot right
    # on home. Fleet/field-service tenants keep the count card (their day lives on
    # their own board), as does a diary still blocked on intake set-up (the
    # intake check reads the same scheduling-config row the strip needs — one
    # fetch serves both).
    # People (docs/plan-people.md): whose Today this is — their own work, and
    # only the cards they can act on (Kris 2026-09-13: "they should have their
    # own Today page for what they've got on"). Read BEFORE the strip, which
    # narrows the day by it (live 2026-09-21: Today 500'd on the rig the first
    # morning its intake was configured — `_mine` was assigned twenty lines
    # below its first use, in a branch no test had reached).
    _mine = _me(ctx)
    strip = None
    cfg_row = f_sched.result() if f_sched else None
    if strip_wanted and (cfg_row or {}).get("intake_configured_at"):
        day_rows = scheduling.office_rows(rows)
        # Delegation by kind (docs/plan-people.md slice 2): a staff member's
        # day is the work handed to them — the desk, their services — not the
        # whole business's. Everyone else sees everything, as before.
        if people.does(_mine):
            from app import job_people as _jp
            _jp.annotate(ctx.tenant_id, day_rows)
        day_rows = [b for b in day_rows if people.owns_booking(_mine, b, cfg_row)]
        strip = _today_strip(cfg_row, day_rows, datetime.now(scheduling.DIARY_TZ))

    # CRM on Today (dashboard presence): one routing card with the shape of
    # the book — who the CRM knows, what's open in the pipeline — plus the
    # gone-quiet nudge card when someone's overdue. Module-gated (the CRM
    # unlocks at Core, docs/crm-vs-os-architecture.md §1) and best-effort: a
    # CRM read failure hides the cards, never the dashboard.
    try:
        cfg_row = f_cfg.result() or {}
    except Exception:                                   # noqa: BLE001
        # The deck falls back to its default topic order rather than vanishing.
        cfg_row = {}
    # Mail brought in from before BLAM8 is not "nothing waiting" (run #10
    # (qqq): the done moment said All clear over 32 unskimmed messages). Shown
    # while the account is still new, which is the only time it is news.
    before_blam8 = 0
    if setup_handoff.is_fresh(t.get("onboarded_at")):
        try:
            from app import backlog as _backlog
            before_blam8 = navcache.get_or_fill(
                "t:" + ctx.tenant_id + ":backlog", 120.0,
                lambda: _backlog.count_rows(ctx.tenant_id, client=ctx.client))
        except Exception:
            before_blam8 = 0
    crm_quiet, crm_hot, crm_card = [], [], None
    if "crm" in mods:
        try:
            quiet_days = int(cfg_row.get("crm_quiet_days") or crm.STALE_AFTER_DAYS)
            contacts = crm.with_recency(
                f_contacts.result(),
                stale_after=quiet_days)
            crm_quiet = [c for c in contacts if c["stale"]][:3]
            # Running hot (sentiment addendum): frustrated/angry customers, freshest
            # read first — the "look at these first" card.
            crm_hot = sorted(
                [c for c in contacts if c.get("sentiment") in ("frustrated", "angry")],
                key=lambda c: c.get("sentiment_updated_at") or "", reverse=True)[:3]
            open_deals = [d for d in f_deals.result() if d.get("status") == "open"]
            crm_card = {"contacts": len(contacts), "open_deals": len(open_deals),
                        "value": sum(float(d.get("value_estimate") or 0) for d in open_deals)}
        except Exception:
            crm_quiet, crm_hot, crm_card = [], [], None
    else:
        # Locked (Kris 2026-09-13): the tile stays, with what the CRM has
        # already filled in and the way to Pro — never a missing door.
        teaser = _crm_teaser(ctx) or {"contacts": 0, "companies": 0}
        crm_card = {"contacts": teaser["contacts"], "open_deals": 0, "value": 0, "locked": True}

    # What somebody has asked of THIS person (docs/plan-people.md slice 3) —
    # the sharing half of People. Cheap, cached, and never fatal.
    from app import handoffs as _handoffs
    my_notes = []
    if ctx.user_id:
        try:
            my_notes = _handoffs.for_deck(navcache.get_or_fill(
                "sn:" + ctx.tenant_id + ":" + ctx.user_id, 20.0,
                lambda: db.list_staff_notes_for(ctx.tenant_id, ctx.user_id)))
        except Exception:
            my_notes = []

    # ── The deck: one glass card per person, ranked by who needs you next ──
    # Sources feed FACTS onto people; a card's rank is its loudest fact.
    #
    # Two kinds of fact share the card since 2026-09-04 (Kris: "use it more
    # like a mobile notification system"). STATE facts — drafts waiting, gone
    # quiet, overdue — are recomputed here every render and have no time on
    # them. EVENT facts come off the notification feed, carry the clock time
    # they happened at, and can be swiped away. Both rank through src/app/deck.py,
    # whose eight topics are what the tenant orders in Settings.
    deck_people: dict[str, dict] = {}

    def _person(name=None, email=None, company=None, contact_id=None):
        key = (email or name or "someone").strip().lower()
        p = deck_people.get(key)
        if not p:
            p = {"name": (name or email or "Someone"), "email": email, "company": company,
                 "contact_id": contact_id, "facts": [], "diary": []}
            deck_people[key] = p
        if name and p["name"] == p.get("email"):
            p["name"] = name
        if company and not p["company"]:
            p["company"] = company
        if contact_id and not p["contact_id"]:
            p["contact_id"] = contact_id
        return p

    notes_by_email: dict[str, list[str]] = {}   # the customer's on-the-day items, by address
    for c in waiting:
        gap = (c.get("gap") or "").strip()
        _person(c.get("display_name"), c.get("contact"), c.get("contact_company"))["facts"].append({
            "kind": "draft", "topic": "drafts",
            "rank": deck.rank_of("drafts", cfg_row),
            # A knowledge hold has NO draft (finding 19, 2026-09-20): say what it
            # is waiting on, never "Draft ready" for a reply that does not exist.
            "chip": "Held — needs a fact" if gap else "Draft ready",
            "tone": "amber" if gap else "brand",
            "text": (f"Waiting on: {gap}" if gap
                     else c.get("subject") or c.get("preview") or "Reply drafted, waiting for your yes"),
            "href": f"/app/conversations/view?contact={quote(c.get('contact') or '')}"})

    if "crm" in mods:
        try:
            by_id = {c["id"]: c for c in contacts}
            # Only what someone still owes someone (src/app/promise_kinds.py):
            # a booking the diary holds, a deposit Money tracks and the
            # customer's on-the-day logistics are not "You owe" rows.
            _all_open = [pr for pr in f_promises.result() if by_id.get(pr.get("contact_id"))]
            _pets: dict[str, list[str]] = {}
            try:
                for _i in (f_instances.result() if f_instances else None) or []:
                    if _i.get("contact_id") and _i.get("name"):
                        _pets.setdefault(_i["contact_id"], []).append(_i["name"])
            except Exception:                                 # noqa: BLE001
                _pets = {}
            _route_today = datetime.now(scheduling.DIARY_TZ).date()
            # Every obligation, both ways (Kris 2026-09-21, second thought: what the
            # customer owes and what waits on a condition are still things to keep
            # an eye on this morning). Bookings and payments stay with the diary
            # and Money; care notes ride the day's booking.
            open_promises = [pr for pr in _all_open if promise_kinds.kind(pr) == "obligation"]
            # What the customer must bring or do on the day rides the arrival
            # it belongs to (deck.booking_facts), keyed by their address.
            for pr in _all_open:
                if promise_kinds.kind(pr) == "on_the_day":
                    _em = (by_id[pr["contact_id"]].get("email") or "").strip().lower()
                    if _em:
                        notes_by_email.setdefault(_em, []).append((pr.get("description") or "").strip().rstrip("."))
            # The exchange each promise was read out of, for the pop-up on its
            # row (Kris 2026-09-20: what is owed opens the promise and what
            # caused it, not the CRM). One read for the whole deck, and
            # best-effort: without it the pop-up still says what is owed,
            # just not what said so.
            sources: dict = {}
            source_ids = [pr["email_log_id"] for pr in open_promises if pr.get("email_log_id")]
            if source_ids:
                try:
                    sources = {e["id"]: e for e in db.get_email_log_entries(
                        ctx.tenant_id, source_ids, client=ctx.client)}
                except Exception:                                 # noqa: BLE001
                    log.warning("today: promise source emails read failed", exc_info=True)
            # The deal a promise sits on, by name. Best-effort too: a deals
            # read that failed never used to cost the deck its promises.
            try:
                deals_by_id = {d["id"]: d for d in f_deals.result() if d.get("id")}
            except Exception:                                     # noqa: BLE001
                deals_by_id = {}
            for pr in open_promises:
                who = by_id[pr["contact_id"]]
                owe = pr.get("direction") == "we_owe"
                _person(who.get("name"), who.get("email"), contact_id=who["id"])["facts"].append({
                    "kind": "promise", "topic": "promises",
                    # A promise with a date on it floats the way a booking
                    # does; one without waits its turn near the bottom. That
                    # split is what the original deck had (rank 1 vs rank 6)
                    # and what collapsing them into one topic lost.
                    "rank": deck.rank_of("promises", cfg_row,
                                         urgent=bool(pr.get("due_hint")),
                                         imminent=bool(pr.get("due_hint"))),
                    "chip": "You owe" if owe else "They owe", "tone": "rose" if owe else "brand",
                    "text": (pr.get("description") or "") + (f" · {pr['due_hint']}" if pr.get("due_hint") else ""),
                    "href": f"/app/crm/contacts/{who['id']}",
                    # The pop-up's contents, rendered into the row (home.html).
                    "promise": crm.promise_card(
                        pr, who, email=sources.get(pr.get("email_log_id")),
                        deal=deals_by_id.get(pr.get("deal_id")),
                        action=promise_kinds.route(pr, contact=who, pets=_pets.get(who["id"], []),
                                                   today=_route_today))})
            for h in crm_hot:
                _person(h.get("name"), h.get("email"), contact_id=h.get("id"))["facts"].append({
                    "kind": "hot", "topic": "unhappy",
                    "rank": deck.rank_of("unhappy", cfg_row),
                    "chip": h.get("sentiment") or "running hot", "tone": "rose",
                    "text": h.get("sentiment_reason") or "Recent messages read unhappy — look first",
                    "href": f"/app/conversations/view?contact={quote(h.get('email') or '')}"})
            for q in crm_quiet:
                _person(q.get("name"), q.get("email"), contact_id=q.get("id"))["facts"].append({
                    "kind": "quiet", "topic": "quiet",
                    "rank": deck.rank_of("quiet", cfg_row),
                    "chip": f"quiet {q.get('days_since_last')}d", "tone": "ink",
                    "text": "No word either way — a check-in keeps it warm",
                    "href": f"/app/conversations/view?contact={quote(q.get('email') or '')}"})
            # The record needing a hand (topic "records", 2026-09-21): details
            # BLAM8 read but wants confirmed, per person, and documents to label.
            try:
                from app import dependants_extract, requirement_groups as _rgroups
                _inst = f_instances.result() or []
                inst_owner = {i["id"]: i.get("contact_id") for i in _inst if i.get("id")}
                # Only what the card itself would show (dependants_extract.annotate):
                # a "new subject" chip for a subject already on the record is not a thing to do.
                _visible = dependants_extract.annotate(
                    f_suggest.result() or [], _rgroups.clean_groups(f_groups.result() or []), _inst)
                to_confirm: dict[str, list[str]] = {}
                for s in _visible:
                    rid = s.get("record_id")
                    cid = rid if rid in by_id else inst_owner.get(rid)
                    if cid in by_id:
                        to_confirm.setdefault(cid, []).append(s.get("who") or "")
                for cid, names in to_confirm.items():
                    who = by_id[cid]
                    dogs = sorted({n for n in names if n})
                    n = len(names)
                    _person(who.get("name"), who.get("email"), contact_id=cid)["facts"].append({
                        "kind": "record", "topic": "records",
                        "rank": deck.rank_of("records", cfg_row),
                        "chip": "To confirm", "tone": "amber",
                        "text": f"{n} detail{'' if n == 1 else 's'} BLAM8 read"
                                + (f" about {', '.join(dogs)}" if dogs else "") + " — a tap to confirm or dismiss",
                        "href": f"/app/crm/contacts/{cid}"})
                to_label: dict[str, int] = {}
                for d in f_docs.result() or []:
                    cid = d.get("contact_id")
                    if not cid:
                        _em = (d.get("contact_email") or "").strip().lower()
                        cid = next((c["id"] for c in contacts if (c.get("email") or "").lower() == _em), None)
                    if cid in by_id:
                        to_label[cid] = to_label.get(cid, 0) + 1
                for cid, n in to_label.items():
                    who = by_id[cid]
                    _person(who.get("name"), who.get("email"), contact_id=cid)["facts"].append({
                        "kind": "record", "topic": "records",
                        "rank": deck.rank_of("records", cfg_row),
                        "chip": "To label", "tone": "amber",
                        "text": f"{n} document{'' if n == 1 else 's'} from their emails, not yet labelled",
                        "href": f"/app/crm/contacts/{cid}"})
            except Exception:                                 # noqa: BLE001
                log.warning("today: records read failed", exc_info=True)
        except Exception:
            pass

    # The diary, forward-looking and time-stamped (app/deck.booking_facts).
    # `rows` is everything from today FORWARD, which the old inline version
    # chipped "Booked today" wholesale — so tomorrow's 09:00 read as background
    # while an undated promise led the card (Kris 2026-09-04).
    # On a stays diary a booking owes two facts — the arrival and the
    # collection — and the arrival carries what the customer is bringing.
    _stay_span = None
    try:
        # Stay mode the way the diary decides it: from the intake fields (scheduling.diary_days).
        if f_sched is not None and intake.is_stay((f_sched.result() or {}).get("intake_fields")):
            from app import slotgrid as _slotgrid

            def _stay_span(b, _sg=_slotgrid):
                span = _sg.stay_span(b)
                if not span:
                    return None
                return _sg.abs_to_datetime(span[0]), _sg.abs_to_datetime(span[0] + span[1])
    except Exception:                                         # noqa: BLE001
        _stay_span = None
    for contact, fact in deck.booking_facts(diary_ahead, cfg_row, notes=notes_by_email,
                                            stay_span=_stay_span):
        _person(email=contact)["facts"].append(fact)
    # The flip face says "· today", so only today belongs on it.
    for b in rows:
        if not b.get("contact") or str(b.get("booking_date") or "")[:10] != today:
            continue
        _person(email=b["contact"])["diary"].append({
            "summary": b.get("summary") or "Booked",
            "window": b.get("window") or "", "status": b.get("status") or ""})

    try:
        _invoices = f_invoices.result()
        _today_d = datetime.now(scheduling.DIARY_TZ).date()
        # A chase already drafted: the card says so and leads to the draft.
        chase_pending: dict = {}
        try:
            from app import invoice_chase as _ic
            for cr in f_chases.result() or []:
                _ref = _ic.chase_ref_from_intent(cr.get("body_in"))
                if _ref:
                    chase_pending[_ref] = cr.get("from_address") or ""
        except Exception:                                     # noqa: BLE001
            chase_pending = {}
        for r in money.overdue_rows(_invoices, _today_d):
            ref = r.get("invoice_ref")
            text = f"{ref} · £{float(r.get('outstanding') or 0):,.2f} outstanding · {r.get('days_late')}d late"
            href = "/app/money"
            if ref in chase_pending:
                text += " · reminder drafted — review"
                href = f"/app/conversations/view?contact={quote(chase_pending[ref])}"
            elif r.get("reminders_sent"):
                text += f" · {r['reminders_sent']} reminder{'' if r['reminders_sent'] == 1 else 's'} sent"
            _person(name=r.get("customer"), email=r.get("email") or None)["facts"].append({
                "kind": "money", "topic": "money",
                "rank": deck.rank_of("money", cfg_row, urgent=ref in chase_pending),
                "chip": "Overdue", "tone": "rose", "text": text, "href": href})
        # A deposit or balance due TODAY with nothing paid: worth a look before
        # it becomes tomorrow's overdue.
        for inv in money.owing(_invoices):
            if (inv.get("due_date") or "")[:10] != _today_d.isoformat():
                continue
            _em = (inv.get("customer_email") or "").strip().lower() or None
            _person(name=inv.get("customer_name") or _em, email=_em)["facts"].append({
                "kind": "money", "topic": "money",
                "rank": deck.rank_of("money", cfg_row, imminent=True),
                "chip": "Due today", "tone": "amber",
                "text": f"{inv.get('invoice_ref')} · £{money._outstanding(inv):,.2f} due today, nothing paid yet",
                "href": f"/app/invoices/{inv.get('id')}"})
    except Exception:
        pass

    # What actually HAPPENED, off the notification feed and onto the card of
    # whoever it happened to (2026-09-04). Last of the sources on purpose: an
    # event fact joins a person the earlier sources have already named, so the
    # card usually knows them properly before the feed's own label is needed.
    try:
        for contact, contact_name, fact in deck.event_facts(f_notes.result(),
                                                            cfg_row):
            p = _person(contact_name or None, contact)
            # The "Draft ready" push and the waiting draft are one thing: the
            # state fact already says it (and stays true until sent), so the
            # event would only print it twice (Kris 2026-09-27, Dev + Ben).
            if fact["topic"] == "drafts" and any(
                    f.get("kind") == "draft" and f.get("href") == fact["href"]
                    for f in p["facts"]):
                continue
            p["facts"].append(fact)
    except Exception:
        log.warning("today: notification feed read failed", exc_info=True)

    try:
        problems = health.visible_problems(f_health.result())
    except Exception:                                         # noqa: BLE001
        # Best-effort like every other section — but logged at warning, because
        # this is the read whose silence is the bug it exists to fix.
        log.warning("today: connection health read failed", exc_info=True)
        problems = []

    # Ignored senders are not contacts (the CRM page's rule, Kris 2026-09-01), so they
    # have no card here either: ignoring someone used to clear their thread and drafts
    # while their promises, sentiment and held bookings stayed on Today (live
    # 2026-09-30). Read-time filter, like the CRM: un-ignoring brings them straight back.
    try:
        _ignored = {a.lower() for a in db.ignored_addresses(ctx.tenant_id, client=ctx.client)}
    except Exception:                                         # noqa: BLE001
        _ignored = set()
    cards = []
    for p in deck_people.values():
        if _ignored and (p.get("email") or "").strip().lower() in _ignored:
            continue
        # Rank, then the clock: two bookings that are both imminent tie on rank,
        # and "soonest first" is the only sensible way to break that.
        p["facts"].sort(key=lambda f: (f["rank"], f.get("at_ts") or 0))
        p["rank"] = p["facts"][0]["rank"] if p["facts"] else 999
        # An unread event anywhere on the card lights the header dot — the
        # phone gesture: the stack tells you it has something new in it before
        # you read a word of it.
        p["unseen"] = any(f.get("unseen") for f in p["facts"])
        if p["facts"] or p["diary"]:
            cards.append(p)
    cards.sort(key=lambda p: (p["rank"], (p["name"] or "").lower()))

    # Events OS on Today (docs/os/events.md §2): a routing card with this week's events —
    # "N events this week · …". Same best-effort contract as the CRM card: any read
    # failure (or the module being off) hides the card, never the dashboard.
    events_card = None
    try:
        if f_events is not None:
            eb = f_events.result()
            if eb["this_week_count"] or eb["next_weeks_count"]:
                events_card = eb
    except Exception:
        events_card = None

    # Whose cards these are (People), then namesakes told apart (Kris
    # 2026-09-20: two Hannahs and no way to know which is which) — on the
    # cards actually shown, so a card the lens took away cannot make the one
    # left wear a handle for nobody.
    shown = deck.namesakes([c for c in cards if people.card_is_theirs(_mine, c, cfg_row)])

    brief = _todays_brief(ctx, f_notes, _mine)
    # Today's numbers gain a Concierge row (Kris 2026-09-25). With Pro +
    # Concierge: what she has waiting and has done today, a tap opens her.
    # Without it: a locked row to the plans page, like the CRM on Starter —
    # shown to whoever can change the plan.
    if f_cz is not None:
        concierge_card = f_cz.result()
    elif people.is_admin(_mine):
        concierge_card = {"locked": True}
    else:
        concierge_card = None

    tour_page, tour_plural = _tour_for(ctx, t, "today", force=request.query_params.get("tour") == "1")
    return templates.TemplateResponse(request, "app/home.html", _ctx(
        "home", ctx,
        tenant=t,
        tour_page=tour_page, tour_kind_plural=tour_plural,
        # Today's order, for the cog on the Who-needs-you-next box.
        deck_topics=deck.ordered_topics(f_cfg.result() or {}),
        deck_saved=request.query_params.get("deck") == "saved",
        crm_quiet=crm_quiet, crm_hot=crm_hot, crm_card=crm_card, events_card=events_card,
        before_blam8=before_blam8, my_notes=my_notes,
        deck=shown, brief=brief, concierge_card=concierge_card,
        problems=problems,
        mailbox_count=len(boxes) or (1 if t.get("mailbox") else 0),
        queue=queue, waiting_total=len(waiting),
        board=board, strip=strip,
        u=f_usage.result(),
        trial=trial.evaluate(t),
    ))


@router.get("/modules")
def modules_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The module hub — what's live for this tenant, what's next. This is the
    subscriptions switchboard made visible: activating a module for a tenant
    lights its card here with zero code changes."""
    active = db.active_modules(ctx.tenant_id, client=ctx.client)
    previewing = db.preview_modules(ctx.tenant_id, client=ctx.client)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    # The one OS this tenant may preview: their own industry's module, when it's
    # built but not yet bought (the founding-partner look-inside).
    industry_os = plans.INDUSTRY_MODULE.get((t.get("industry") or "").lower())

    def _card(m: dict) -> dict:
        # Invoicing is standalone core since the Field OS extraction — active for every
        # tenant, no subscription row (the modules switchboard never has to know).
        if m["id"] == "invoicing":
            return {**m, "state": "active"}
        # SMS stays LOCKED until the platform Twilio creds exist (channel under
        # verification): without them the card presents as roadmap ("Coming
        # soon", no buy link) instead of a buyable add-on. It flips back to the
        # add-on card the moment the creds land on Render — no code change. An
        # operator-activated tenant (internal testing) still sees it live.
        if m["id"] == "sms" and m["id"] not in active and not sms.configured():
            return {**m, "state": "off", "group": "soon", "badge": "Coming soon"}
        state = ("active" if (m.get("always_on") or m["id"] in active)
                 else "preview" if m["id"] in previewing else "off")
        # The look-inside affordance: this tenant's own OS, built, not bought.
        previewable = (m["id"] == industry_os and state == "off")
        return {**m, "state": state, "previewable": previewable}

    modules = [_card(m) for m in MODULES]

    # CRM teaser (Julius 2026-07-25): the zero-LLM ingest pre-fills the CRM for
    # EVERY tenant, so the locked card can show what it already knows — enough
    # to want it, nothing usable.
    crm_teaser = None if ({"crm"} & (active | previewing)) else _crm_teaser(ctx)

    return templates.TemplateResponse(request, "app/modules.html", _ctx(
        "modules", ctx, modules=modules, crm_teaser=crm_teaser,
        email_connected=bool(db.get_connection_status(ctx.tenant_id, client=ctx.client)),
        email_mailbox=t.get("mailbox"),
    ))


# ── OS preview (the founding-partner look-inside) ────────────────────────────
_MODULE_HOME = {m["id"]: m.get("href") for m in MODULES}


@router.post("/os/preview", dependencies=[Depends(require_csrf)])
def start_os_preview(ctx: TenantCtx = Depends(require_onboarded)):
    """Open the tenant's own industry OS in preview: the subscriptions row goes
    to status='preview' (UI surfaces light up, seeded with the same working
    defaults as real activation) while the AGENT stays off — pipeline tools,
    proactive jobs and calendar sync all still gate on module_active. Buying
    the tier upgrades the row to active; previewing something already active
    is a no-op redirect."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    module = plans.INDUSTRY_MODULE.get((t.get("industry") or "").lower())
    if not module:
        return RedirectResponse("/app/modules", status_code=303)
    if module not in db.active_modules(ctx.tenant_id, client=ctx.client):
        db.set_module_preview(ctx.tenant_id, module)
        db.seed_module_defaults(ctx.tenant_id, module)
    return RedirectResponse(_MODULE_HOME.get(module) or "/app/modules", status_code=303)


# ── Inbox ──────────────────────────────────────────────────────────────────
@router.get("/inbox")
def inbox(request: Request):
    """The legacy per-message inbox page is retired — /app/conversations IS the inbox
    (the POV shakedown flagged the two parallel surfaces as onboarding friction, and
    the 86bb6c9 booking pill shipped invisibly here). Old links land on the live one,
    keeping their mailbox tab."""
    mb = request.query_params.get("mailbox")
    dest = "/app/conversations" + (f"?{urlencode({'mailbox': mb})}" if mb else "")
    return RedirectResponse(dest, status_code=303)


@router.get("/inbox/{message_id}")
async def read_message(request: Request, message_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    # ?mailbox= says which mailbox the message lives in (enterprise — a Graph/Gmail
    # message id can only be fetched with its own mailbox's token).
    sel = _selected_mailbox_id(ctx, request.query_params.get("mailbox"))
    # If this message has already been drafted, the approval queue (the conversation thread)
    # shows the draft + approve controls — the bare message view can't. Send the user there so
    # a refreshed/stale /inbox link never looks like "nothing was drafted" (Fix 1).
    existing = db.get_email_log_by_message(ctx.tenant_id, message_id, client=ctx.client)
    if existing:
        return RedirectResponse(_thread_after_draft(ctx, existing["id"]), status_code=303)
    try:
        msg = await mailbox.get_message(ctx.tenant_id, message_id, mailbox_id=sel)
    except Exception:
        log.warning("read_message: mailbox read failed for tenant %s mailbox %s",
                    ctx.tenant_id, sel or "(default)", exc_info=True)
        msg = None
    def _blocking():
        return templates.TemplateResponse(request, "app/message.html",
                                          _ctx("inbox", ctx, msg=msg, sel=sel))
    return await run_in_threadpool(_blocking)


# ── Draft review (per-draft detail + the "something's wrong" fix flow) ───────
# The approval QUEUE is the Inbox (conversations): each thread surfaces its pending
# draft inline with approve / skip / re-draft, and the menu's draft-count badge links
# there. This page is the per-draft deep-dive the Inbox links INTO — chiefly the
# "Something's wrong?" knowledge-correction flow (?fix=1).
@router.get("/drafts/{log_id}")
def review_draft(request: Request, log_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if entry is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    _require_their_mail(ctx, entry)
    # Enterprise: show which mailbox this reply will leave from.
    from_box = None
    if entry.get("mailbox_id") and db.is_enterprise(ctx.tenant_id, client=ctx.client):
        box = next((m for m in _tenant_mailboxes(ctx) if m["id"] == entry["mailbox_id"]), None)
        from_box = (box or {}).get("address") or (box or {}).get("display")
    return templates.TemplateResponse(request, "app/draft_review.html",
        _ctx("drafts", ctx, e=entry, kb_configured=kb.is_configured(), from_box=from_box))


@router.post("/drafts/{log_id}/send", dependencies=[Depends(require_csrf)])
async def send_draft(request: Request, log_id: str, draft_reply: str = Form(...),
                     return_to: str = Form("/app/conversations"),
                     review_seconds: int | None = Form(None),
                     ctx: TenantCtx = Depends(require_onboarded)):
    # Files to go with the reply (photos both ways, Kris 2026-09-26): ticked
    # from the customer's record, or added from the phone there and then.
    form = await request.form()
    doc_ids = [str(v) for v in form.getlist("attach_doc") if str(v).strip()]
    new_files = []
    for f in form.getlist("attach_new"):
        if isinstance(f, FormUploadFile) and f.filename:
            data = await f.read()
            if data:
                new_files.append((f.filename, data, f.content_type))
    dest = _safe_next(return_to)
    review: dict = {}
    try:
        out = await send_approved(ctx, log_id, draft_reply, review_seconds=review_seconds,
                                  doc_ids=doc_ids, new_files=new_files,
                                  concierge=dest == "/app/concierge", review_out=review)
    except HTTPException as e:
        # Nothing was sent (an empty reply, a file that couldn't be kept or
        # read) — back to the thread, which says why (Kris 2026-09-26: a raw
        # {"detail": …} page after tapping send).
        # The Concierge fetch needs the 409 body for its card. A normal form
        # post returns to the thread, where send_err displays the same reason.
        if (e.status_code not in (400, 409, 503) or not isinstance(e.detail, str)
                or (e.status_code == 409 and dest == "/app/concierge")):
            raise
        return RedirectResponse(dest + ("&" if "?" in dest else "?") + "send_err="
                                + quote_plus(e.detail[:200]), status_code=303)
    # "Sent ✓" on the page it lands on (Kris 2026-09-24) — only when the send
    # really went; a skipped SMS (opted out) says nothing of the kind.
    if out == "sent":
        dest += ("&" if "?" in dest else "?") + "sent=1"
        # ...and, when the owner changed the draft in a way that could teach the
        # agent something, the card that asks (base.html reads ?learn=).
        if review.get("ask"):
            dest += "&learn=" + quote_plus(log_id)
    return RedirectResponse(dest, status_code=303)


async def send_approved(ctx: TenantCtx, log_id: str, draft_reply: str, *,
                        review_seconds: int | None = None, auto: bool = False,
                        doc_ids: list[str] | None = None,
                        new_files: list[tuple] | None = None,
                        concierge: bool = False,
                        review_out: dict | None = None) -> str:
    """Send one pending draft — THE send path behind the thread's thumbs-up,
    shared with the voice Concierge (src/app/concierge_draft.py) so a draft sent by
    voice gets exactly what an approved one does: deposits, the portal footer,
    holds confirmed, the CRM and calendar bookkeeping. Returns "sent", or
    "skipped" when an SMS recipient has opted out; raises HTTPException as the
    route always did.

    `auto` is the voice full-auto send (Kris 2026-09-23): the owner asked for
    the email but did not read it before it left, so it is an AUTOMATED send —
    it carries the AI disclosure footer (src/app/disclosure.py, EU AI Act Art 50)
    and is stamped via="auto", exactly as a full-auto inbound reply is. Only
    server code sets it; the form route never does."""
    # Where the wait goes (Kris 2026-09-24: 👍 on Sophie's reply sat ~33 s
    # behind a spinner). One trace row per send, ms per step (_Stopwatch).
    sw = _Stopwatch()
    require_can(ctx, "inbox", "approve")
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if entry is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if entry.get("kind") == "on_my_way":
        raise HTTPException(status_code=409,
                            detail="Use the booking's On my way action or ETA correction.")
    _require_their_mail(ctx, entry)
    if entry.get("kind") == "scheduled_plan":
        from app import scheduled_plan
        _guards.require_admin(ctx)
        if doc_ids or new_files:
            raise HTTPException(400, "Scheduled messages do not support attachments.")
        return await scheduled_plan.send_draft(ctx.tenant_id, entry, draft_reply, auto=auto)
    if entry.get("kind") == "tenant_outreach":
        from app import tenant_outreach
        # All inbox/voice approvals re-enter outreach's current send gates.
        # Attachments are not supported by list campaigns; never silently omit them.
        if doc_ids or new_files:
            raise HTTPException(400, "Outreach messages do not support attachments.")
        return await tenant_outreach.send_draft(ctx.tenant_id, entry, draft_reply, auto=auto)
    if concierge or entry.get("kind") == "owner_message":
        from app.concierge_booking_claims import check
        reason, _ = check(ctx.tenant_id, entry.get("from_address"), draft_reply, ctx.client)
        if reason:
            raise HTTPException(status_code=409, detail=reason)
    sw.mark("read_draft")
    if not draft_reply.strip():
        # A failed draft queues with an empty body — approving it must never send
        # an empty email. The operator writes something (or Redrafts) first.
        raise HTTPException(status_code=400,
                            detail="This draft is empty — write a reply before sending.")
    # Capture whether the human changed the agent's text BEFORE the edit
    # overwrites it — the operator's edit-rate signal. An EMPTY original draft
    # (escalation / knowledge hold) means the human authored this reply outright:
    # stamped 'owner', excluded from the AI edit-rate split, labelled "You ·
    # Sent" in the thread (Kris 2026-08-31).
    owner_authored = not (entry.get("draft_reply") or "").strip()
    edited = _draft_edited(draft_reply, entry.get("draft_reply"))
    # A substantive change is worth a question after the send (src/app/edit_review.py):
    # keep the agent's own text, since the next line overwrites it. Not for auto
    # sends — nobody edited those.
    if (edited and not auto and review_out is not None
            and edit_review.substantive(entry.get("draft_reply"), draft_reply)
            and db.set_email_log_draft_original(log_id, ctx.tenant_id, entry["draft_reply"],
                                                client=ctx.client)):
        review_out["ask"] = True
    db.update_email_log_draft(log_id, draft_reply, ctx.tenant_id, client=ctx.client)  # persist edits first
    sw.mark("save_edit")
    # Proactive outbounds (day-before reminders, offer nudges, payment chases)
    # are fresh sends with their own subject — never threaded as a reply to an
    # inbound.
    is_reminder = entry.get("kind") in ("reminder", "offer_nudge", "invoice_reminder",
                                        "owner_message")
    if auto:
        from app import disclosure
        _auto_cfg = db.effective_config(ctx.tenant_id, entry.get("mailbox_id"), client=ctx.client)
    # What leaves the building: the approved text, plus the AI footer when
    # nobody read it first. The stored draft stays clean either way.
    def _outbound(text: str, channel: str = "email") -> str:
        if not auto:
            return text
        return disclosure.wrap(_auto_cfg, text, channel=channel,
                               kind="message" if is_reminder else "reply")
    # The files riding this reply — read (and any new ones kept on the
    # customer's record) BEFORE the send, so a file that can't be read stops
    # the send rather than going out as a reply that says "attached" and isn't.
    files, file_rows = [], []
    if (doc_ids or new_files) and entry.get("channel") != "sms":
        files, file_rows = _reply_files(ctx, entry, doc_ids or [], new_files or [])
    if entry.get("from_address"):
        if entry.get("channel") == "sms":
            # SMS thread (docs/modules/module-sms-channel.md §4): the approved draft leaves
            # through the tenant's provisioned number — reminder vs reply is the
            # same send on a channel with no subjects or threading headers. A
            # STOP received since the draft was queued turns the send into a
            # visible skip (never a silent drop, and never a compliance breach).
            try:
                await sms.send_sms(ctx.tenant_id, entry["from_address"],
                                   _outbound(draft_reply, "sms"), email_log_id=log_id)
            except sms.SmsOptedOut:
                db.set_email_log_status(log_id, "skipped", ctx.tenant_id, client=ctx.client)
                events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                               "contact": entry.get("from_address"),
                                               "status": "skipped"})
                return "skipped"
        else:
            box = _selected_mailbox_id(ctx, entry.get("mailbox_id"))
            if is_reminder:
                # A proactive reminder (Phase 2B) is a fresh outbound, not a reply — send it
                # with its own clean subject, never threaded onto an inbound.
                await mailbox.send_mail(ctx.tenant_id, entry["from_address"],
                                        entry.get("subject") or "Reminder", _outbound(draft_reply),
                                        mailbox_id=box, **({"attachments": files} if files else {}))
            else:
                # Customer portal: approved replies carry the reference-code
                # sign-off too (src/app/portal.py — "" when portal off). Outbound
                # copy only; the stored draft stays clean.
                cfg_footer = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
                # Quotes Phase 3 'auto' mode: the accept link for the quote
                # this draft priced rides the approved reply too ("" in every
                # other mode/state — approving is the same send as auto-send).
                from app import deposits, quote_autodraft
                # Deposit on confirmation (src/app/deposits.py): the deposit invoice
                # for the holds this approval confirms rides the same email —
                # staged here, finalised once the send succeeds, voided if it
                # fails. "" and None when there is nothing to ask for.
                dep_inv, dep_block = deposits.stage_for_log(
                    ctx.tenant_id, log_id, entry["from_address"], client=ctx.client)
                sw.mark("deposit_stage")
                body_out = (_outbound(draft_reply)
                            + quote_autodraft.link_for_send(
                                ctx.tenant_id, cfg_footer, log_id, client=ctx.client)
                            + dep_block
                            + portal.email_footer(
                                ctx.tenant_id, cfg_footer, entry["from_address"]))
                sw.mark("build_body")
                # Reply from the mailbox that received the inbound (enterprise D5). A
                # stale stamp (mailbox since disconnected) falls back to the default.
                try:
                    await mailbox.send_reply(ctx.tenant_id, entry["from_address"], entry.get("subject"), body_out,
                                             source_message_id=entry.get("source_message_id"), mailbox_id=box,
                                             **({"attachments": files} if files else {}))
                except Exception:
                    deposits.discard(ctx.tenant_id, dep_inv, client=ctx.client)
                    raise
                sw.mark("send_email")
                deposits.after_send(ctx.tenant_id, dep_inv, log_id=log_id,
                                    customer_email=entry["from_address"], client=ctx.client)
                sw.mark("deposit_finalise")
    for r in file_rows:
        # The paperclip on OUR bubble: a 'sent' row on the same stored file.
        db.insert_document(ctx.tenant_id, filename=r.get("filename") or "attachment",
                           content_type=r.get("content_type") or "application/octet-stream",
                           size_bytes=r.get("size_bytes") or 0, storage_path=r.get("url_or_path"),
                           source="sent", contact_email=(entry.get("from_address") or "").lower() or None,
                           contact_id=r.get("contact_id"), email_log_id=log_id,
                           booking_id=r.get("booking_id"))
    via = "auto" if auto else ("owner" if owner_authored else "human")
    db.mark_email_log_sent(log_id, ctx.tenant_id, via=via,
                           edited=None if (owner_authored or auto) else edited, client=ctx.client)
    sw.mark("mark_sent")
    if entry.get("kind") == "invoice_reminder":
        # An approved payment chase (src/app/invoice_chase.py) stamps the invoice's
        # chase trail exactly as the concierge's confirmed reminder does:
        # reminders_sent feeds the 3-strike "hasn't paid" escalation, and the
        # chase joins the contact's CRM timeline as money, not mail.
        # Best-effort — the email is already gone; bookkeeping must not 500 it.
        try:
            from app import invoice_chase, invoice_send
            inv_id = invoice_chase.invoice_id_from_source(entry.get("source_message_id"))
            inv = db.get_invoice(inv_id, ctx.tenant_id, client=ctx.client) if inv_id else None
            if inv:
                db.record_invoice_reminder(inv["id"], ctx.tenant_id, client=ctx.client)
                n = int(inv.get("reminders_sent") or 0) + 1
                from app.dates import pretty_date
                invoice_send._crm_invoice_activity(
                    ctx.tenant_id, inv, activity_type="invoice_reminder",
                    source_id=f"{inv['id']}:r{n}",
                    subject=f"Payment reminder — {inv.get('invoice_ref')}",
                    summary=f"£{float(inv.get('total') or 0):,.2f} outstanding, "
                            f"due {pretty_date(inv.get('due_date'))}")
        except Exception:
            log.warning("chase-trail stamp failed for email_log %s", log_id,
                        exc_info=True)
    if review_seconds is not None:
        # HRT (Kris 2026-08-31): visible on-screen dwell the client measured
        # before this send (_review_timer.html). Clamped hard — a forgotten
        # tab is not review.
        db.set_email_log_review_seconds(log_id, ctx.tenant_id,
                                        max(0, min(review_seconds, 1800)),
                                        client=ctx.client)
    # Draft suppression: sending IS engagement — the contact's unsent streak
    # resets, and a threshold pause lifts (a permanent ignore stays).
    suppression.record_sent(ctx.tenant_id, entry.get("from_address"), client=ctx.client)
    sw.mark("review_suppression")
    trace.emit(ctx.tenant_id, "label_transition", email_log_id=log_id,
               contact=entry.get("from_address"), user_id=ctx.user_id,
               detail={"to_category": "quote_sent", "trigger": "reply_sent",
                       "via": via,
                       "edited": None if (owner_authored or auto) else edited})
    db.log_agent_event(ctx.tenant_id, "sent",
                       f"Sent the {'reminder' if is_reminder else 'reply'} to {entry.get('from_address')}"
                       + (" (full-auto, asked for by voice)" if auto
                          else " (edited)" if edited else " (approved)"),
                       direction="outbound" if is_reminder else "inbound",
                       contact=entry.get("from_address"), subject=entry.get("subject"),
                       email_log_id=log_id, mailbox_id=entry.get("mailbox_id"),
                       detail={"via": "auto" if auto else "human", "edited": edited},
                       client=ctx.client)
    sw.mark("trace_and_log")
    # Scheduling D4 — one approval drives both: approve & send confirms the draft's
    # linked diary hold (no-op when the draft placed none), and the confirmed
    # booking lands in the tenant's calendar (best-effort).
    if db.resolve_holds_for_log(log_id, "confirmed", ctx.tenant_id, client=ctx.client):
        trace.emit(ctx.tenant_id, "label_transition", email_log_id=log_id,
                   contact=entry.get("from_address"), user_id=ctx.user_id,
                   detail={"to_category": "booking_pending",
                           "trigger": "booking_created", "via": via})
        try:   # CRM: the booking this approval just confirmed joins the
               # contact's timeline (calendar_event; id-keyed so re-sends dedupe).
            confirmed = [_b for _b in db.bookings_for_log(log_id, ctx.tenant_id,
                                                          client=ctx.client)
                         if _b.get("status") == "confirmed"]
            for _b in confirmed:
                crm.note_booking_activity(ctx.tenant_id, _b)
        except Exception:
            confirmed = []
            log.warning("CRM booking activity failed for email_log %s", log_id,
                        exc_info=True)
    else:
        confirmed = []
    sw.mark("confirm_bookings")
    # Live inbox: other open tabs drop this draft from their list/badge at once —
    # the moment the send and the booking are true, not after the bookkeeping.
    events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                   "contact": entry.get("from_address"), "status": "sent"})
    # Everything below is bookkeeping about a send that has ALREADY happened,
    # and the owner was waiting for it. Kris 2026-09-22: "When i click the
    # thumbs up to send a draft, it takes a good 5s ... it should just send
    # instantly on click?" Measured on his own sends that afternoon: crm_intel
    # 5.3-9.3s and crm_action_resolve 3.0-3.5s, both model calls, plus the
    # Google Calendar push, all awaited in series before this redirect. None of
    # them changes the email; the email was out before any of them started.
    # They were already best-effort — each had its own try/except and a
    # warning — so detaching them keeps their contract, and moves the wait off
    # the owner. The one real cost: a process restart inside the few seconds
    # they take drops that send's bookkeeping, which is the same outcome as
    # the exception path they always had.
    _detach(_after_send_bookkeeping(ctx.tenant_id, log_id, entry.get("from_address"),
                                    confirmed))
    sw.mark("publish_detach")
    try:
        trace.emit(ctx.tenant_id, "send_timing", email_log_id=log_id, user_id=ctx.user_id,
                   latency_ms=sw.total_ms(), detail=sw.steps())
    except Exception:
        pass
    return "sent"


class _Stopwatch:
    """Milliseconds per named step of one request, for a trace row."""

    def __init__(self):
        import time as _t
        self._t = _t
        self._start = self._last = _t.monotonic()
        self._steps: dict[str, int] = {}

    def mark(self, name: str) -> None:
        now = self._t.monotonic()
        self._steps[name] = self._steps.get(name, 0) + int((now - self._last) * 1000)
        self._last = now

    def steps(self) -> dict:
        return dict(self._steps)

    def total_ms(self) -> int:
        return int((self._t.monotonic() - self._start) * 1000)


async def _after_send_bookkeeping(tenant_id: str, log_id: str, contact: str | None,
                                  confirmed: list[dict]) -> None:
    """What an approved send sets off, once the owner is no longer waiting.

    Each step keeps its own try/except, exactly as it did inline, so one
    failing never costs the others. No request client: the request is over by
    the time this runs, so it uses the service client, the same one the
    pipeline already runs crm_intel on."""
    if confirmed:
        # Action-side promise resolution (2026-09-03): the confirmed booking(s)
        # may BE a promise fulfilled ("hold the October weekend") — tick the
        # ledger, don't wait for a future email.
        try:
            await crm_intel.resolve_from_action(
                tenant_id, contact,
                "Confirmed booking(s): " + "; ".join(
                    f"{_b.get('booking_ref') or 'booking'} on "
                    f"{_b.get('booking_date')} — {_b.get('summary') or ''}"
                    for _b in confirmed),
                ref=("booking", confirmed[0].get("id"), confirmed[0].get("booking_ref")))
        except Exception:
            log.warning("CRM promise resolution failed for email_log %s", log_id,
                        exc_info=True)
    try:   # the confirmed booking lands in the tenant's calendar (best-effort)
        await calendar_sync.push_bookings(tenant_id, email_log_id=log_id)
    except Exception:
        log.warning("calendar push failed for email_log %s", log_id, exc_info=True)
    try:  # CRM: the approved reply joins the contact's activity feed, and our
          # own commitments in it become we_owe promises (best-effort).
        crm.note_outbound_sent(tenant_id, log_id)
        await crm_intel.note_outbound(tenant_id, log_id)
    except Exception:
        log.warning("CRM outbound ingest failed for email_log %s", log_id, exc_info=True)


_AFTER_SEND_TASKS: set = set()


def _detach(coro) -> None:
    """Run `coro` after the response, keeping a reference so it is not
    garbage-collected mid-flight (the retained-task pattern in src/app/backlog.py)."""
    task = asyncio.get_running_loop().create_task(coro)
    _AFTER_SEND_TASKS.add(task)
    task.add_done_callback(_AFTER_SEND_TASKS.discard)


@router.post("/drafts/{log_id}/skip", dependencies=[Depends(require_csrf)])
def skip_draft(log_id: str, return_to: str = Form("/app/conversations"), ctx: TenantCtx = Depends(require_onboarded)):
    _require_their_log(ctx, log_id)
    db.set_email_log_status(log_id, "skipped", ctx.tenant_id, client=ctx.client)
    # Smart inbox label: a skipped draft means the owner decided no reply is
    # needed — the conversation files under FYI, not left claiming Draft Ready.
    db.set_email_log_category(log_id, ctx.tenant_id, "fyi",
                              reason="You skipped the draft — no reply sent",
                              client=ctx.client)
    trace.emit(ctx.tenant_id, "label_transition", email_log_id=log_id,
               user_id=ctx.user_id,
               detail={"to_category": "fyi", "trigger": "draft_skipped",
                       "via": "human"})
    # Scheduling D4: skipping the draft releases its diary hold.
    db.resolve_holds_for_log(log_id, "cancelled", ctx.tenant_id, client=ctx.client)
    events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                   "status": "skipped"})
    return RedirectResponse(_safe_next(return_to), status_code=303)


# ── Diary & Board (Scheduling/Fleet modules — gated on subscriptions rows) ───
# The diary split (transport-planner D3): /app/diary is the OFFICE diary (the
# windows/slots calendar — people's time) and /app/board is the TRANSPORT board
# (the fleet view — goods on fleet resources). Both live at once when both modules are
# on; a single-module tenant sees one surface, today's behaviour.
def _diary_modules(ctx: TenantCtx) -> set:
    # Visible (active + preview): a founding-partner preview opens the same
    # diary/board surfaces — the agent never books into them (pipeline gates
    # on module_active).
    return db.visible_modules(ctx.tenant_id, client=ctx.client)


def _office_diary(ctx: TenantCtx) -> bool:
    """Does the OFFICE (windows) diary own /app/diary for this tenant? True when
    the scheduling row is active; False for TMS-only (the transport board owns
    the surface — callers redirect/dispatch there); 404 when neither is on."""
    mods = _diary_modules(ctx)
    if "scheduling" in mods:
        return True
    if "tms" in mods:
        return False
    raise HTTPException(status_code=404, detail="Scheduling is not active for this account")


def _fleet_only(ctx: TenantCtx) -> bool:
    """TMS supersedes the windows capacity model ONLY when scheduling is off
    (fleet D3 as amended by transport-planner D1). Drives which settings fields
    render: a dual tenant's windows fields shape the office diary, so they show."""
    mods = _diary_modules(ctx)
    return "tms" in mods and "scheduling" not in mods


def _require_tms(ctx: TenantCtx) -> None:
    """The gate for every transport surface. TMS bundles fleet/capacity, routing
    and the planner plus the baseline TMS surfaces (the fleet+routing merge) into
    one sellable module — a single subscriptions row lights the whole operation."""
    if not db.module_ui_visible(ctx.tenant_id, "tms", client=ctx.client):
        raise HTTPException(status_code=404, detail="Field OS · Transport is not active for this account")


def _require_fsm(ctx: TenantCtx) -> None:
    """The gate for every field-service surface (docs/modules/module-field-service.md) — the
    team member board + trades invoicing. One subscriptions row (module='fsm') lights it."""
    if not db.module_ui_visible(ctx.tenant_id, "fsm", client=ctx.client):
        raise HTTPException(status_code=404, detail="Field OS · Trades is not active for this account")


def _intake_configured(ctx: TenantCtx) -> bool:
    """Has the business completed the scheduling-intake onboarding? Until they have, the
    office diary is BLOCKED (set up first) — a freshly-unlocked scheduling tenant defines
    what every booking needs before the agent will schedule. Existing tenants were
    grandfathered configured by the migration, so this never blocks a live tenant."""
    cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    return bool(cfg.get("intake_configured_at"))


@router.get("/diary")
def diary_view(request: Request, msg: str = "",
               ctx: TenantCtx = Depends(require_onboarded)):
    if not _office_diary(ctx):
        # Fleet-only: the board moved to /app/board (D3) — old links follow it.
        q = urlencode({k: v for k, v in request.query_params.items() if v})
        return RedirectResponse(f"/app/board?{q}" if q else "/app/board", status_code=303)
    if not _intake_configured(ctx):
        guide = ((db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}).get('onboarding_state') or {}).get('guide') or {}
        orientation = guide.get('status') == 'active' and guide.get('intro_ack') and guide.get('current') == 'orientation.diary'
        if not orientation:
            return RedirectResponse("/app/diary/intake", status_code=303)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    data = scheduling.diary(ctx.tenant_id, client=ctx.client)
    # Deep links (Kris 2026-09-20): the thread's "Booked slot" line lands on
    # its day with the booking lit (?day=…&booking=…). Today's booking rows
    # say ?date= and the fleet board already said ?day=, so both are taken.
    open_day = (request.query_params.get("day") or request.query_params.get("date") or "").strip()[:10]
    focus_booking = (request.query_params.get("booking") or "").strip()
    # Delegation by kind (docs/plan-people.md slice 2): the month grid and its
    # bars stay whole — how busy the business is was never a secret — but a
    # staff member's day panel lists the work that is theirs.
    _mine = _me(ctx)
    if people.does(_mine):
        _cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        from app import job_people as _jp
        _jp.annotate(ctx.tenant_id, [b for _w in data["weeks"] for _d in _w["days"]
                                     for b in (_d.get("bookings") or [])])
        for _w in data["weeks"]:
            for _d in _w["days"]:
                if _d.get("bookings"):
                    _d["bookings"] = [b for b in _d["bookings"]
                                      if people.owns_booking(_mine, b, _cfg)]
    # Page 3 of the walk-through (Kris 2026-09-13: "instead of clicking Done,
    # we click Diary. Then run a tour in the Diary").
    tour_page, tour_plural = _tour_for(ctx, t, "diary", force=request.query_params.get("tour") == "1")
    # The tenant's booking requirements feed the manual add form too (expandable
    # "+ Requirements" section) — captured values land in bookings.details, the
    # same field the agent's gather flow fills, so both paths read identically.
    sched_cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    clean = intake.clean_fields(sched_cfg.get("intake_fields") or [])
    req_fields = intake.editable_fields(clean)
    svc_types = services.clean_service_types(sched_cfg.get("service_types"))
    # A prefilled Add (Kris 2026-09-21): a promise's door arrives here with
    # ?add=1 and the customer, the subject, the days and the repeat in the query;
    # the form opens filled in, ready to submit. Times default to the diary's
    # own check-in / check-out.
    add_prefill = None
    if request.query_params.get("add") == "1":
        _qp = request.query_params
        _d = (_qp.get("date") or open_day or "").strip()[:10]
        try:
            _hours = diary_config.clean_day_hours(sched_cfg.get("day_hours") or sched_cfg.get("hours") or {})
        except Exception:                                     # noqa: BLE001
            _hours = {}
        _ci = (_qp.get("arrival") or _hours.get("checkin_after") or sched_cfg.get("open_time") or "")[:5]
        _co = (_qp.get("departure") or _hours.get("checkout_before") or sched_cfg.get("close_time") or "")[:5]
        add_prefill = {
            "date": _d, "contact": (_qp.get("contact") or "").strip(),
            "summary": (_qp.get("summary") or "").strip(), "subjects": (_qp.get("subjects") or "").strip(),
            "service": (_qp.get("service") or "").strip(),
            "repeat": (_qp.get("repeat") or "").strip(), "occurrences": (_qp.get("occurrences") or "").strip(),
            "weekdays": [w for w in (_qp.get("weekdays") or "").split(",") if w],
            "promise": (_qp.get("promise") or "").strip(),
            "arrival": f"{_d}T{_ci}" if _d else "", "departure": f"{_d}T{_co}" if _d else "",
        }
    # A standing series lands as a LIST of dates to confirm (Kris 2026-09-21:
    # "once we land in the Diary it shows an area with a list of days/dates to
    # confirm as booked, for as long as the settings allow prebookings"): every
    # occurrence to the horizon, closed days greyed, full days flagged, the
    # times editable, one button to book the ticked ones. The by-hand form
    # stays folded behind "Adjust by hand".
    series_preview = None
    if add_prefill and add_prefill["repeat"] and add_prefill["date"]:
        try:
            _today = datetime.now(scheduling.DIARY_TZ).date()
            _items = scheduling.series_to_horizon(sched_cfg, add_prefill["date"], add_prefill["repeat"],
                                                  add_prefill["weekdays"], today=_today)
        except Exception:                                     # noqa: BLE001
            log.warning("diary: series preview failed", exc_info=True)
            _items = []
        if _items:
            _fits: dict = {}
            if intake.stay_mode(sched_cfg):
                try:
                    _fits = scheduling.stay_fits_by_date(
                        ctx.tenant_id, [i["date"] for i in _items if not i["closed"]], _ci, _co, client=ctx.client)
                except Exception:                             # noqa: BLE001
                    log.warning("diary: series fit check failed", exc_info=True)
            _ord = lambda n: "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")  # noqa: E731
            _months: list = []
            _n_ok = _n_closed = _n_full = 0
            for it in _items:
                d = it["date"]
                fits = _fits.get(d, True)
                ok = (not it["closed"]) and fits
                _n_ok += ok
                _n_closed += it["closed"]
                _n_full += (not it["closed"]) and not fits
                key = d.strftime("%B %Y")
                if not _months or _months[-1][0] != key:
                    _months.append((key, []))
                _months[-1][1].append({"date": d.isoformat(), "label": f"{d.strftime('%a')} {d.day}{_ord(d.day)}",
                                       "closed": it["closed"], "fits": fits, "ok": ok})
            _wd = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
            _days = [_wd[w] for w in add_prefill["weekdays"] if w in _wd]
            _every = (", ".join(_days[:-1]) + " & " + _days[-1]) if len(_days) > 1 else (_days[0] if _days else
                     _items[0]["date"].strftime("%A"))
            _rep = {"weekly": "every week", "fortnightly": "every fortnight", "six_weekly": "every six weeks"}.get(
                add_prefill["repeat"], add_prefill["repeat"])
            series_preview = {
                "title": add_prefill["summary"] or "Regular booking",
                "line": f"{_every} {_rep} · to {pretty_date(_items[-1]['date'].isoformat())} — as far ahead as the diary takes bookings",
                "from": _ci, "to": _co, "months": _months,
                "n_ok": _n_ok, "n_closed": _n_closed, "n_full": _n_full,
            }
    try:
        from app import requirement_groups as _rg
        dep_groups = _rg.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
    except Exception:
        dep_groups = []
    # The filter (docs/plan-diary-multiview.md stage 3): ?show=stays / a service.
    # The view only — each day's bookings, bar and chart lanes for that kind.
    diary_kinds = scheduling.diary_kinds(sched_cfg)
    _show = (request.query_params.get("show") or "").strip().lower()
    show_kind = next((k for k in diary_kinds if k["key"] == _show), None)
    if show_kind and show_kind["group"] is not None:
        for _w in data["weeks"]:
            _w["days"] = [scheduling.filter_day(_d, show_kind, diary_kinds) if _d.get("in_diary") else _d
                          for _d in _w["days"]]
    # 3b nit: who is waiting for a class on each visible day (src/app/courses.py).
    try:
        from app import courses as _courses
        _wait_days = (_courses.waiting_by_day(
            ctx.tenant_id, [d["date"] for w in data["weeks"] for d in w["days"] if d.get("in_diary")],
            client=ctx.client) if _courses.waitlist_on(sched_cfg) else {})
    except Exception:
        _wait_days = {}
    return templates.TemplateResponse(request, "app/diary.html",
        _ctx("diary", ctx, msg=msg, weeks=data["weeks"], cfg=data["config"],
             open_day=open_day, focus_booking=focus_booking,
             diary_kinds=diary_kinds, show_kind=(show_kind or {}).get("key") if show_kind else None,
             show_service=(show_kind or {}).get("service"),
             summary=data["summary"], mode=data["mode"], dep_groups=dep_groups,
             waiting_by_day=_wait_days, tour_page=tour_page, tour_kind_plural=tour_plural,
             # Stay diaries swap the add-form's date+window for an
             # arrival→departure span (Julius 2026-09-03).
             is_stay=intake.stay_mode(sched_cfg),
             add_prefill=add_prefill, series_preview=series_preview,
             # Services diaries pick a service, then a time / half-day / day per
             # its timing kind (Kris 2026-09-03: the form offered 'Whole day' only).
             is_service=bool(svc_types) and not intake.stay_mode(sched_cfg),
             is_mixed=bool(svc_types) and intake.stay_mode(sched_cfg),
             service_types=[{**s, "timing": services.timing(s)} for s in svc_types],
             # Standing series by hand (run #7): the repeat control shows only
             # when the tenant offers recurring bookings.
             recurring=diary_config.clean_recurring(sched_cfg.get("recurring")),
             freq_label=diary_config.freq_label,
             open_time=str((sched_cfg or {}).get("open_time") or "")[:5],
             close_time=str((sched_cfg or {}).get("close_time") or "")[:5],
             window_options=data["window_options"], req_fields=req_fields,
             provider=t.get("provider") or "microsoft",
             calendar_connected=db.calendar_connected(ctx.tenant_id, client=ctx.client)))


@router.get("/board")
def board_view(request: Request, msg: str = "", branch: str = "", day: str = "",
               note: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The transport board (D3): the fleet view that used to squat on /app/diary.
    With routing on it also carries the planner surface — proposed-plan diffs,
    the per-day Replan button, and the passive freed-capacity banner (Q3)."""
    _require_tms(ctx)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    mods = _diary_modules(ctx)
    data = scheduling.fleet_board(ctx.tenant_id, client=ctx.client,
                                  branch_id=branch.strip() or None,
                                  dual="scheduling" in mods)
    routing_on = "tms" in mods
    today = datetime.now(scheduling.DIARY_TZ).date()
    plans_by_date: dict[str, list] = {}
    review_days: list = []
    if routing_on:
        rows_by_date: dict[str, list] = {}
        for b in data["bookings"]:
            rows_by_date.setdefault(str(b.get("booking_date"))[:10], []).append(b)
        for p in db.list_plans(ctx.tenant_id, date_from_iso=today.isoformat(),
                               status="proposed", client=ctx.client):
            view = planner.plan_view(p, data["vehicles"],
                                     rows_by_date.get(str(p["plan_date"])[:10], []))
            plans_by_date.setdefault(str(p["plan_date"])[:10], []).append(view)
        review_days = planner.review_days(
            data["vehicles"], data["bookings"], today,
            int(data["config"].get("horizon_days") or 28))
    return templates.TemplateResponse(request, "app/diary_fleet.html",
        _ctx("board", ctx, msg=msg, note=note, weeks=data["weeks"], cfg=data["config"],
             summary=data["summary"], branches=data["branches"],
             branch_id=data["branch_id"], type_options=data["type_options"],
             has_vehicles=any(v.get("active", True) for v in data["vehicles"]),
             routing_on=routing_on, open_day=day.strip(),
             focus_booking=(request.query_params.get("booking") or "").strip(),
             plans_by_date=plans_by_date, review_days=review_days,
             planner_auto=bool((db.get_tenant_config(ctx.tenant_id, client=ctx.client)
                                or {}).get("planner_auto_apply")) if routing_on else False,
             provider=t.get("provider") or "microsoft",
             calendar_connected=db.calendar_connected(ctx.tenant_id, client=ctx.client)))


async def _board_add(ctx: TenantCtx, booking_date: str, summary: str, contact: str,
                     qty: str, pallet_type: str, weight_kg: str, branch_id: str):
    """The transport board's manual add — today's fleet add verbatim, landing
    back on /app/board. Confirmed immediately (their diary, their call); the
    atomic best-fit RPC still owns capacity."""
    try:
        day = date.fromisoformat(booking_date.strip())
    except ValueError:
        return RedirectResponse("/app/board?msg=baddate", status_code=303)
    factors = scheduling.pallet_factors(db.list_pallet_types(ctx.tenant_id, client=ctx.client))
    try:
        spaces, _ = scheduling.order_spaces(
            factors, [{"type": pallet_type, "qty": float(qty or 0)}])
    except (TypeError, ValueError, KeyError):
        return RedirectResponse("/app/board?msg=badload", status_code=303)
    try:
        weight = float(weight_kg) if weight_kg.strip() else None
    except ValueError:
        weight = None
    branch = branch_id.strip() or None
    if branch and not any(b["id"] == branch for b in db.list_branches(ctx.tenant_id, client=ctx.client)):
        branch = None
    readiness, _ = scheduling.make_executor(ctx.tenant_id, source="manual", fleet=True)
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise HTTPException(status_code=409, detail=blocked)
    row = db.hold_fleet_booking(
        ctx.tenant_id, day.isoformat(), summary.strip() or "Booking", spaces,
        weight_kg=weight if weight and weight > 0 else None, branch_id=branch,
        details={"pallets": f"{scheduling.fmt_spaces(float(qty))} × {pallet_type.strip() or 'full'}"},
        contact=(contact.strip() or None), source="manual", status="confirmed",
    )
    if row:
        await calendar_sync.push_bookings(ctx.tenant_id, booking_id=row["id"])
        try:   # CRM: manual board bookings join the timeline (email contacts only).
            crm.note_booking_activity(ctx.tenant_id, row)
        except Exception:
            log.warning("CRM booking activity failed", exc_info=True)
    return RedirectResponse("/app/board?msg=" + ("added" if row else "nofit"), status_code=303)


@router.post("/board/add", dependencies=[Depends(require_csrf)])
async def board_add(booking_date: str = Form(...), summary: str = Form(...),
                    contact: str = Form(""), qty: str = Form(""), pallet_type: str = Form(""),
                    weight_kg: str = Form(""), branch_id: str = Form(""),
                    ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    return await _board_add(ctx, booking_date, summary, contact, qty, pallet_type,
                            weight_kg, branch_id)


def _link_manual_subjects(tenant_id: str, row: dict, contact_email: str | None,
                          subjects_text: str) -> list[str]:
    """Names typed on the manual add ("The Byre, The Dairy") → the contact's
    matching dependant records, linked to the new booking and written under
    the tenant's plural in its details. Unknown names are ignored (the diary
    is the owner's; the job page can link later). Best-effort."""
    names = [" ".join(n.split()) for n in re.split(r"[,;/]|\band\b", subjects_text or "") if n.strip()]
    if not names or not contact_email or not row or not row.get("id"):
        return []
    try:
        from app import requirement_groups as rgroups
        groups = rgroups.clean_groups(db.list_requirement_groups(tenant_id))
        contact = db.crm_contact_by_email(tenant_id, contact_email) if groups else None
        if not groups or not contact:
            return []
        by_id = {g.get("id"): g for g in groups}
        instances = db.list_requirement_instances(tenant_id, contact["id"])
        matched: list[dict] = []
        for n in names:
            low = n.lower()
            for inst in instances:
                have = str(inst.get("name") or "").strip().lower()
                if have and (have == low or have in low or low in have) and inst.get("group_id") in by_id \
                        and inst not in matched:
                    matched.append(inst)
                    break
        if not matched:
            return []
        db.link_booking_subjects(tenant_id, row["id"], [i["id"] for i in matched])
        details = dict(row.get("details") or {})
        for g in groups:
            mine = [i.get("name") for i in matched if i.get("group_id") == g.get("id")]
            if mine:
                plural = g.get("label_plural") or g.get("label") or "Dependants"
                details[plural] = ", ".join(mine)
        db.update_booking_details(row["id"], tenant_id, details)
        row["details"] = details
        return [i.get("name") for i in matched]
    except Exception:
        log.warning("manual add: linking dependants failed", exc_info=True)
        return []


@router.post("/diary/add", dependencies=[Depends(require_csrf)])
async def diary_add(booking_date: str = Form(""), window: str = Form("day"), summary: str = Form(...),
                    contact: str = Form(""), qty: str = Form(""), pallet_type: str = Form(""),
                    weight_kg: str = Form(""), branch_id: str = Form(""),
                    arrival: str = Form(""), departure: str = Form(""),
                    service: str = Form(""), subjects_text: str = Form(""),
                    repeat: str = Form(""), occurrences: str = Form(""),
                    req_label: list[str] = Form([]), req_value: list[str] = Form([]),
                    makeup_for: str = Form(""),
                    weekdays: list[str] = Form([]), promise_id: str = Form(""),
                    dates: list[str] = Form([]), series_from: str = Form(""), series_to: str = Form(""),
                    ctx: TenantCtx = Depends(require_onboarded)):
    """Manual 'Add booking' on the OFFICE diary (D4 — standalone v1). Entered by
    a human, so it is confirmed immediately and skips the notice/horizon checks
    (their diary, their call) — but capacity still goes through the atomic RPC
    like every booking. A fleet-only tenant posting here (a stale tab from
    before the split) dispatches to the board's add and lands on /app/board."""
    require_can(ctx, "diary", "add")
    if not _office_diary(ctx):
        return await _board_add(ctx, booking_date, summary, contact, qty, pallet_type,
                                weight_kg, branch_id)
    # Booking requirements filled on the form → the same bookings.details the
    # agent's gather flow writes (keyed by label). Blank values drop out — a
    # human adding to their own diary is never forced through the intake gate.
    details = {k.strip(): v.strip() for k, v in zip(req_label, req_value)
               if k.strip() and v.strip()}
    sched_cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
    readiness, _ = scheduling.make_executor(ctx.tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise HTTPException(status_code=409, detail=blocked)
    # The landing panel's ticked dates (2026-09-21): an explicit list, one
    # series per weekday, placed instead of a repeat/occurrences rule.
    try:
        _dates = sorted({date.fromisoformat(x.strip()[:10]) for x in dates if x.strip()})
    except ValueError:
        return RedirectResponse("/app/diary?msg=baddate", status_code=303)
    _by_weekday = lambda ds: [[d for d in ds if d.weekday() == w] for w in sorted({d.weekday() for d in ds})]  # noqa: E731
    # A stays diary with a service picked is adding an APPOINTMENT (Kris
    # 2026-09-13): the service branch below places it in its own pool.
    adding_appointment = bool(service.strip()) and services.configured((sched_cfg or {}).get("service_types"))
    if intake.stay_mode(sched_cfg) and not adding_appointment:
        # STAY diary (Julius 2026-09-03): the form posts an arrival→departure
        # span, not a date+window — the same shape the agent and portal book.
        try:
            if _dates and series_from.strip() and series_to.strip():
                arr = datetime.combine(_dates[0], datetime.strptime(series_from.strip()[:5], "%H:%M").time())
                dep = datetime.combine(_dates[0], datetime.strptime(series_to.strip()[:5], "%H:%M").time())
            else:
                arr = datetime.fromisoformat(arrival.strip()[:16])
                dep = datetime.fromisoformat(departure.strip()[:16])
        except ValueError:
            return RedirectResponse("/app/diary?msg=baddate", status_code=303)
        # A standing series of stays by hand (Kris 2026-09-21: "lock in Mondays,
        # Wednesdays and Thursdays as Mabel's regular slots"): the same span each
        # named weekday, `occurrences` times, one series per weekday.
        rows = []
        _series = (_by_weekday(_dates) if _dates else
                   scheduling.manual_series_by_weekday(sched_cfg, arr.date(), repeat, occurrences, weekdays)
                   if repeat.strip() else [])
        if _series:
            for _dates in _series:
                _rec = str(uuid.uuid4()) if len(_dates) > 1 else None
                for _d in _dates:
                    _arr = datetime.combine(_d, arr.time())
                    _dep = datetime.combine(_d + (dep.date() - arr.date()), dep.time())
                    r, err = scheduling.manual_stay_add(
                        ctx.tenant_id, _arr, _dep, summary.strip() or "Booking",
                        details=details or None, contact=(contact.strip() or None),
                        client=ctx.client, **({"recurrence_id": _rec} if _rec else {}))
                    if err == "bad_span":
                        return RedirectResponse("/app/diary?msg=baddate", status_code=303)
                    if r:
                        rows.append(r)
            row = rows[0] if rows else None
            for r in rows[1:]:
                _link_manual_subjects(ctx.tenant_id, r, contact.strip() or None, subjects_text)
                await calendar_sync.push_bookings(ctx.tenant_id, booking_id=r["id"])
        else:
            row, err = scheduling.manual_stay_add(
                ctx.tenant_id, arr, dep, summary.strip() or "Booking",
                details=details or None, contact=(contact.strip() or None),
                client=ctx.client)
            if err == "bad_span":
                return RedirectResponse("/app/diary?msg=baddate", status_code=303)
            rows = [row] if row else []
    elif service.strip() and services.configured((sched_cfg or {}).get("service_types")):
        # SERVICES diary (Kris 2026-09-03): the form posts a service plus a time,
        # a half-day or 'day' per that service's timing kind — the same shape the
        # agent and portal book, placed by the same span math.
        try:
            day = _dates[0] if _dates else date.fromisoformat(booking_date.strip())
        except ValueError:
            return RedirectResponse("/app/diary?msg=baddate", status_code=303)
        # A standing series by hand (run #7, 2026-09-05: a classes business enrols a
        # participant for the term AFTER the assessment — the business's move, and
        # the form had no way to make it). Each date is placed like a single
        # add; blackouts and closed days are skipped and the term extends.
        # …and on ticked weekdays (2026-09-21) one series per day, each its own.
        _multi = (_by_weekday(_dates) if _dates else
                  scheduling.manual_series_by_weekday(sched_cfg, day, repeat, occurrences, weekdays)
                  if repeat.strip() else [])
        _runs = [(str(uuid.uuid4()) if len(s) > 1 else None, s) for s in _multi] or [(None, [day])]
        rows, err = [], None
        for rec_id, d in ((rid, dd) for rid, ss in _runs for dd in ss):
            r, err = scheduling.manual_service_add(
                ctx.tenant_id, d, service.strip(), window, summary.strip() or "Booking",
                details=details or None, contact=(contact.strip() or None), client=ctx.client,
                **({"recurrence_id": rec_id} if rec_id else {}))
            if err in ("unknown_service", "bad_window"):
                return RedirectResponse("/app/diary?msg=badwindow", status_code=303)
            if r:
                rows.append(r)
        row = rows[0] if rows else None
        for r in rows[1:]:
            _link_manual_subjects(ctx.tenant_id, r, contact.strip() or None, subjects_text)
            await calendar_sync.push_bookings(ctx.tenant_id, booking_id=r["id"])
    else:
        try:
            day = _dates[0] if _dates else date.fromisoformat(booking_date.strip())
        except ValueError:
            return RedirectResponse("/app/diary?msg=baddate", status_code=303)
        # The window must be one the tenant's CURRENT mode offers (the form's options
        # come from the same set). The old clamp-to-'day' silently turned a slots-mode
        # time like '14:00' into a whole-day block — which blocks every slot that date.
        valid_windows = scheduling._norm(sched_cfg)["windows"]
        if window not in valid_windows:
            return RedirectResponse("/app/diary?msg=badwindow", status_code=303)
        rows = []
        for _dd in (_dates or [day]):
            r = db.hold_booking_if_capacity(
                ctx.tenant_id, _dd.isoformat(), window, summary.strip() or "Booking",
                details=details or None,
                contact=(contact.strip() or None), source="manual", status="confirmed",
            )
            if r:
                rows.append(r)
        row = rows[0] if rows else None
        for r in rows[1:]:
            _link_manual_subjects(ctx.tenant_id, r, contact.strip() or None, subjects_text)
            await calendar_sync.push_bookings(ctx.tenant_id, booking_id=r["id"])
    if row:
        # Dependants (run #5, 2026-09-04): a booking added by hand can say which
        # of the customer's dependants it is about — names matched loosely to
        # the contact's record, linked, and written under the tenant's plural.
        _link_manual_subjects(ctx.tenant_id, row, contact.strip() or None, subjects_text)
        # Remembered requirements (app/intake_memory): what the owner typed on a
        # remembered row lands on the customer's record, as the agent's does.
        if details and contact.strip():
            try:
                from app import intake_memory
                _fields = intake.clean_fields((sched_cfg or {}).get("intake_fields"))
                intake_memory.remember_by_email(
                    ctx.tenant_id, contact.strip(),
                    intake_memory.values_to_remember(_fields, details, service.strip() or None),
                    client=ctx.client)
            except Exception:
                log.warning("manual add: remembering details failed", exc_info=True)
        await calendar_sync.push_bookings(ctx.tenant_id, booking_id=row["id"])
        try:   # CRM: manual diary bookings join the timeline (email contacts only),
               # and any promise the added booking fulfils ticks off (2026-09-03).
            crm.note_booking_activity(ctx.tenant_id, row)
            await crm_intel.resolve_from_action(
                ctx.tenant_id, contact.strip() or None,
                f"Added confirmed booking {row.get('booking_ref') or ''} for "
                f"{row.get('booking_date')} — {summary.strip()}",
                client=ctx.client,
                ref=("booking", row.get("id"), row.get("booking_ref")))
        except Exception:
            log.warning("CRM booking activity failed", exc_info=True)
        # The promise this Add came from (its door on Today / the card) is
        # cleared by what was just booked — the green tick links to the first.
        if promise_id.strip():
            try:
                _n = len(rows) if isinstance(rows, list) and rows else 1
                db.crm_resolve_promise_auto(
                    promise_id.strip(), ctx.tenant_id,
                    evidence=(f"{_n} bookings added by hand from this promise" if _n > 1
                              else "Booked by hand from this promise"),
                    by_kind="booking", by_id=row.get("id"), by_label=row.get("booking_ref"))
            except Exception:
                log.warning("manual add: clearing the promise failed", exc_info=True)
    def _blocking():
        if row and makeup_for.strip():
            # Make-ups (src/app/makeups.py): a make-up placed by hand is bound to its
            # missed occurrence like the agent's — the rule is checked the same way.
            try:
                from app import makeups
                sched = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
                mk = makeups.settings_of(sched)
                missed, merr = makeups.check(ctx.tenant_id, contact.strip() or None, makeup_for, mk, client=ctx.client)
                if missed:
                    makeups.record(ctx.tenant_id, missed, row, mk, contact_email=contact.strip() or None, client=ctx.client)
                    db.update_booking_details(row["id"], ctx.tenant_id, {"Make-up for": missed.get("booking_ref")}, client=ctx.client)
                    return RedirectResponse(f"/app/diary/{row['id']}?msg=makeup", status_code=303)
                return RedirectResponse(f"/app/diary/{row['id']}?msg=notmakeup", status_code=303)
            except Exception:
                log.warning("manual make-up record failed", exc_info=True)
        _land = f"&date={row['booking_date']}" if row and row.get("booking_date") else ""
        return RedirectResponse("/app/diary?msg=" + ("added" if row else "full") + _land, status_code=303)
    return await run_in_threadpool(_blocking)


def _job_day_notes(ctx: TenantCtx, b: dict) -> list[dict]:
    """The "On the day" notes for THIS visit (Kris 2026-09-23: "the job page
    should show the 'On the day' promises") — bring the food portioned, drop
    off at 1pm, the meds labelled. Promises carry the email they were read
    from and a booking carries its thread, so a note belongs to this job when
    it came from this booking's email thread: another visit's notes stay on
    that visit. Best-effort — never breaks the page."""
    try:
        if not b.get("contact"):
            return []
        c = db.crm_contact_by_email(ctx.tenant_id, b.get("contact"), client=ctx.client)
        if not c:
            return []
        mine = set(db.email_log_ids_for_thread(ctx.tenant_id, b.get("thread_id"), client=ctx.client)) \
            if b.get("thread_id") else set()
        if b.get("email_log_id"):
            mine.add(b["email_log_id"])
        if not mine:
            return []
        return [p for p in promise_kinds.on_the_day(db.crm_open_promises(ctx.tenant_id, c["id"],
                                                                         client=ctx.client))
                if p.get("email_log_id") in mine]
    except Exception:  # noqa: BLE001
        log.warning("job day notes failed for %s", b.get("id"), exc_info=True)
        return []


# ── the diary job page (docs/modules/module-job-parties.md Phase 1) ──────────────────
# The single-booking surface the diary never had: who's involved, and where
# each document goes. Per D2, parties are edited HERE and nowhere else.
@router.get("/diary/{booking_id:uuid}")
def diary_job_page(request: Request, booking_id: "UUID",
                   ctx: TenantCtx = Depends(require_onboarded)):
    # :uuid converter, deliberately: /diary/settings and /diary/intake are
    # sibling GETs — a bare {booking_id} would swallow them (route order).
    from app import job_parties
    booking_id = str(booking_id)
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    parties = db.list_job_parties(ctx.tenant_id, booking_id, client=ctx.client)
    # "On my way" is for a business that goes to the customer
    # (on_my_way.goes_to_customer): a business where customers come to the premises is not on
    # its way to anyone — the tap stays off their job pages.
    from app import on_my_way as _omw
    try:
        _sched = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        _sched = {}
    on_my_way_on = _omw.goes_to_customer(_sched.get("intake_fields"), b)
    # Dependants (docs/modules/module-requirement-groups.md): the instances this
    # booking is about, and the customer's others that could be linked.
    subjects, linkable, dep_groups = [], [], []
    try:
        from app import requirement_groups as rgroups
        dep_groups = rgroups.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
        # A dependant's own fields obey the same rule as a customer's: the
        # "Property" group is exactly where a key-safe number lives. Narrowed
        # on the GROUP, so the rows, the gaps and the one-line summary all
        # agree rather than one of them leaking what the others hide.
        for _g in dep_groups:
            _g["fields"] = crm_visibility.visible_group_fields(_g, _me(ctx))
        if dep_groups:
            by_id = {g.get("id"): g for g in dep_groups}
            linked = db.subjects_for_bookings(ctx.tenant_id, [booking_id], client=ctx.client).get(booking_id, [])
            from app import ladders as _ladders
            for inst in linked:
                g = by_id.get(inst.get("group_id"))
                if g:
                    subjects.append({**inst, "group": g, "line": rgroups.instance_line(g, inst),
                                     # Written out as a record, label then value
                                     # (Kris 2026-09-23: the one-line summary wrapped
                                     # badly on a phone) — ladders sit apart below.
                                     "rows": [(f["label"], (inst.get("data") or {}).get(f["key"]), f)
                                              for f in g.get("fields") or [] if not _ladders.is_ladder(f)],
                                     "gaps": rgroups.missing_for(g, inst.get("data"), b.get("booking_date")),
                                     # Progress ladders: the level on record and the
                                     # levels to move to — one tap on the job page.
                                     "ladders": [{"field": f, "levels": _ladders.levels_of(f),
                                                  "current": (inst.get("data") or {}).get(f["key"])}
                                                 for f in g.get("fields") or [] if _ladders.is_ladder(f)]})
            c = db.crm_contact_by_email(ctx.tenant_id, b.get("contact"), client=ctx.client) if b.get("contact") else None
            if c:
                have = {s["id"] for s in subjects}
                linkable = [{**i, "group": by_id[i["group_id"]]}
                            for i in db.list_requirement_instances(ctx.tenant_id, c["id"], client=ctx.client)
                            if i.get("group_id") in by_id and i["id"] not in have]
    except Exception:
        subjects, linkable = [], []
    # Documents (src/app/documents.py): pinned to this booking first, then the
    # customer's others — the certificate that came by email is one click away.
    job_documents = []
    try:
        from app import documents as _documents
        pinned = db.list_documents(ctx.tenant_id, booking_id=booking_id, client=ctx.client)
        others = (db.list_documents(ctx.tenant_id, contact_email=b.get("contact"), client=ctx.client)
                  if b.get("contact") else [])
        seen = set()
        for d in pinned + others:
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["pinned"] = d.get("booking_id") == booking_id
            d["size_text"] = _documents.pretty_size(d.get("size_bytes"))
            job_documents.append(d)
    except Exception:
        job_documents = []
    # Photos (Kris 2026-09-26: evidence "both ways") — this job's own first,
    # then the ones the customer sent; the rest stay in the Documents list.
    from app import photos as _photos
    job_photos = [d for d in job_documents if _photos.is_photo(d.get("filename"), d.get("content_type"))]
    job_documents = [d for d in job_documents if d not in job_photos]
    day_notes = _job_day_notes(ctx, b)
    # The Move form's "when" prefill: the clock time when there is one, else the
    # window — an unchanged value must resolve to the same slot (2026-09-06).
    try:
        from app import slotgrid as _slotgrid
        when_now = (_slotgrid.hhmm(int(b["start_block"])) if b.get("start_block") is not None
                    else (b.get("window") if b.get("window") and b.get("window") != "day" else ""))
    except Exception:
        when_now = b.get("window") or ""
    # A stay's own form takes drop-off and collection (record_edits.stay_times).
    from app import record_edits as _re
    _st = _re.stay_times(b)
    stay_arrival = _st[0].strftime("%Y-%m-%dT%H:%M") if _st else ""
    stay_departure = _st[1].strftime("%Y-%m-%dT%H:%M") if _st else ""
    return templates.TemplateResponse(request, "app/diary_job.html", _ctx(
        "diary", ctx, b=b, parties=parties, subjects=subjects, linkable=linkable, dep_groups=dep_groups,
        # The dependant just unlinked (?undo=), offered back with one tap — only
        # if it is one of the customer's own, so a stray id renders nothing.
        undo=next((i for i in linkable if i.get("id") == request.query_params.get("undo")), None),
        on_my_way_on=on_my_way_on, omw_action=_omw.page_state(ctx.tenant_id, booking_id, b),
        # Who's on it (src/app/job_people.py, plan step 2).
        **_job_people_ctx(ctx.tenant_id, b, _sched),
        # After the job (src/app/after_job.py): the job's tasks and deadlines —
        # None (nothing shown) for a business that uses none.
        **__import__("app.after_job", fromlist=["job_context"]).job_context(ctx.tenant_id, b, _sched),
        may_assign=people.can_do(_me(ctx), "diary", "add"),
        **_say_ctx(ctx, "booking", booking_id,
                   (b.get("summary") or b.get("booking_ref") or "this booking")),
        job_documents=job_documents, job_photos=job_photos, when_now=when_now, day_notes=day_notes,
        stay_arrival=stay_arrival, stay_departure=stay_departure,
        # The register and the wallet (src/app/wallet.py) for this occurrence.
        **__import__("app.wallet", fromlist=["job_context"]).job_context(
            ctx.tenant_id, b, register_on=bool(dep_groups), client=ctx.client),
        # Who is waiting for this class (src/app/courses.py).
        **__import__("app.courses", fromlist=["job_context"]).job_context(ctx.tenant_id, b, client=ctx.client),
        # The log on this booking (src/app/customer_log.py): the customer's notes and the business's.
        **__import__("app.customer_log", fromlist=["job_context"]).job_context(ctx.tenant_id, b, client=ctx.client),
        # Make-ups (src/app/makeups.py): does this miss earn one, was it used, is this one?
        **__import__("app.makeups", fromlist=["job_context"]).job_context(ctx.tenant_id, b, None, client=ctx.client),
        # Phase 2 split: what a human vouched for vs what the thread claims —
        # 'extracted' rows render as confirm cards, never as settled rows.
        held=[p for p in parties if p.get("source") != "extracted"],
        suggested=[p for p in parties if p.get("source") == "extracted"],
        routing=job_parties.resolve(b, parties),
        duty_labels=job_parties.DUTY_LABELS,
        msg=request.query_params.get("msg")))


@router.post("/diary/{booking_id}/parties", dependencies=[Depends(require_csrf)])
async def diary_add_party(request: Request, booking_id: str,
                          ctx: TenantCtx = Depends(require_onboarded)):
    if db.get_booking(booking_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    def _blocking():
        email = (form.get("email") or "").strip().lower()
        phone = (form.get("phone") or "").strip()
        if not email and not phone:
            return RedirectResponse(f"/app/diary/{booking_id}?msg=noidentity",
                                    status_code=303)
        contact = None
        if email:
            # The CRM is the address book (module-job-parties.md principle 6):
            # every party links to a contact row so history accrues per person.
            contact = db.crm_get_or_create_contact(ctx.tenant_id, email,
                                                   name=(form.get("name") or "").strip() or None)
        db.upsert_job_party(ctx.tenant_id, booking_id, {
            "name": form.get("name"), "email": email, "phone": phone,
            "label": form.get("label"),
            "pays": form.get("pays") == "1",
            "approves": form.get("approves") == "1",
            "on_site": form.get("on_site") == "1",
            "informed": form.get("informed") == "1",
            "contact_id": (contact or {}).get("id"), "source": "owner",
        }, client=ctx.client)
        return RedirectResponse(f"/app/diary/{booking_id}?msg=saved", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/diary/{booking_id}/parties/{party_id}/delete",
             dependencies=[Depends(require_csrf)])
def diary_delete_party(booking_id: str, party_id: str,
                       ctx: TenantCtx = Depends(require_onboarded)):
    db.delete_job_party(party_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse(f"/app/diary/{booking_id}?msg=removed", status_code=303)


@router.post("/diary/{booking_id}/parties/{party_id}/confirm",
             dependencies=[Depends(require_csrf)])
def diary_confirm_party(booking_id: str, party_id: str,
                        ctx: TenantCtx = Depends(require_onboarded)):
    """The human's yes on an extracted party (Phase 2, D4): only now do its
    duties start resolving. The CRM link happens here too — a suggestion is
    not an address-book entry until someone vouches for it."""
    parties = db.list_job_parties(ctx.tenant_id, booking_id, client=ctx.client)
    p = next((x for x in parties if x.get("id") == party_id), None)
    if p is None:
        raise HTTPException(status_code=404, detail="Party not found")
    contact = None
    if p.get("email"):
        contact = db.crm_get_or_create_contact(ctx.tenant_id, p["email"],
                                               name=p.get("name"))
    confirmed = db.confirm_job_party(party_id, ctx.tenant_id,
                                     contact_id=(contact or {}).get("id"),
                                     client=ctx.client)
    if confirmed:
        b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client) or {}
        money = p.get("pays") or p.get("approves")
        db.log_agent_event(
            ctx.tenant_id, "party_confirmed",
            f"You confirmed {p.get('name') or p.get('email') or 'a party'} "
            f"({p.get('label') or 'party'}) on {b.get('booking_ref') or 'a job'}"
            + (" — money documents now route to them." if money else "."),
            contact=p.get("email"), booking_id=booking_id,
            booking_ref=b.get("booking_ref"), client=ctx.client)
    return RedirectResponse(f"/app/diary/{booking_id}?msg=confirmed",
                            status_code=303)


@router.post("/diary/{booking_id}/status", dependencies=[Depends(require_csrf)])
async def diary_set_status(booking_id: str, status: str = Form(...),
                           return_to: str = Form("/app/diary"),
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Manual confirm / decline / cancel — the human always has direct control
    over any booking, whoever created it, from either surface (`return_to`
    brings them back to the diary or the board they acted on). Calendar
    follows: confirm pushes an event, cancel/decline removes the pushed one."""
    if (status or "").strip().lower() in ("cancelled", "declined"):
        require_can(ctx, "diary", "cancel")     # a cancellation reaches the customer
    else:
        require_can(ctx, "diary", "add")
    mods = _diary_modules(ctx)
    if not ({"scheduling", "tms"} & mods):
        raise HTTPException(status_code=404, detail="Scheduling is not active for this account")
    if status not in ("confirmed", "declined", "cancelled"):
        raise HTTPException(status_code=400, detail="Bad status")
    if status == "confirmed":
        # One confirm for the Diary and the Concierge (src/app/booking_confirm.py).
        from app import booking_confirm
        done = await booking_confirm.confirm(ctx.tenant_id, booking_id, client=ctx.client)
        if not done["ok"] and done["reason"] == "full":
            from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
            target = urlsplit(_safe_next(return_to))
            query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                     if key != "msg"]
            query.append(("msg", "full"))
            return RedirectResponse(urlunsplit(target._replace(query=urlencode(query))), status_code=303)
        return RedirectResponse(_safe_next(return_to), status_code=303)
    # Read BEFORE the write: the event should say what the row WAS (releasing
    # an offered slot reads differently from cancelling a confirmed booking).
    booking = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if status in ("cancelled", "declined"):
        await calendar_sync.remove_pushed_event(ctx.tenant_id, booking_id)
    from app.booking_errors import capacity_conflict
    try:
        db.set_booking_status(booking_id, status, ctx.tenant_id, client=ctx.client)
    except Exception as exc:
        if not capacity_conflict(exc):
            raise
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        target = urlsplit(_safe_next(return_to))
        query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                 if key != "msg"]
        query.append(("msg", "full"))
        return RedirectResponse(urlunsplit(target._replace(query=urlencode(query))), status_code=303)
    from app import job_sheets   # the people on it hear it's off (or, confirmed, get it)
    job_sheets.soon(ctx.tenant_id, booking_id)
    if status == "cancelled" and booking and booking.get("status") == "confirmed":
        try:   # the waiting list (src/app/courses.py): a freed place goes to the first waiting
            from app import courses as _courses
            await _courses.on_place_freed(ctx.tenant_id, booking, client=ctx.client)
        except Exception:
            log.warning("waitlist freed-place hook failed (%s)", booking_id, exc_info=True)
        try:   # the customer is told (src/app/hand_cancel.py, run #8): a note for approval
            from app import hand_cancel as _hand_cancel
            await _hand_cancel.on_hand_cancel(ctx.tenant_id, booking, client=ctx.client)
        except Exception:
            log.warning("hand-cancel note failed (%s)", booking_id, exc_info=True)
    # Agent Log visibility (Kris 2026-08-31): releasing a slot the AGENT held
    # left the feed telling half a story — "Held BK-x" and then nothing. A
    # human decision earns a line only when it changes the agent's work, i.e.
    # the booking is tied to an email thread; pure manual housekeeping (added
    # by hand, removed by hand — Kris watched exactly that and asked why it
    # would log) stays out of the agent's story. Best-effort by design.
    def _blocking():
        if booking and booking.get("email_log_id"):
            word = ("Released" if status == "cancelled"
                    and booking.get("status") == "offered"
                    else status.capitalize())
            ref = booking.get("booking_ref") or booking_id[:8]
            when = " — ".join(x for x in [pretty_date(booking.get("booking_date")),
                                          str(booking.get("window") or "")] if x)
            db.log_agent_event(
                ctx.tenant_id, "booking_manual",
                f"{word} {ref}{f' ({when})' if when else ''} by hand in the diary",
                contact=booking.get("contact"), booking_id=booking_id,
                booking_ref=booking.get("booking_ref"),
                event_date=str(booking.get("booking_date") or "") or None)
        # The thread ghost lists this booking's live holds — a hand change in the
        # diary repaints the open thread the same way a pipeline change does
        # (Kris 2026-09-03: released holds must leave the list as they go).
        if booking and booking.get("contact"):
            events.publish(ctx.tenant_id, {"type": "status_changed",
                                           "log_id": booking.get("email_log_id"),
                                           "contact": booking.get("contact"),
                                           "booking_id": booking_id, "status": status})
        return RedirectResponse(_safe_next(return_to), status_code=303)
    return await run_in_threadpool(_blocking)


def _job_people_ctx(tenant_id: str, b: dict, sched: dict) -> dict:
    """Who's on it (src/app/job_people.py) and, for those who don't sign in, where
    their job sheet has got to (src/app/job_sheets.py)."""
    from app import job_people, job_sheets
    out = job_people.job_context(tenant_id, b, sched)
    try:
        out["sheet_lines"] = job_sheets.status_lines(tenant_id, b, out["on_it"])
    except Exception:
        out["sheet_lines"] = {}
    return out


@router.post("/diary/{booking_id}/done", dependencies=[Depends(require_csrf)])
def diary_job_done(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Mark the job done — starts its service's after-job deadlines
    (src/app/after_job.py). A stamp, not a status: nothing else about the
    booking changes, and a second tap does nothing."""
    from app import after_job
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    made = after_job.mark_done(ctx.tenant_id, b, by=f"owner:{ctx.user_id or ''}")
    return RedirectResponse(f"/app/diary/{booking_id}?msg=done{len(made)}#after-job", status_code=303)


@router.post("/diary/{booking_id}/after-job/{task_id}/done", dependencies=[Depends(require_csrf)])
async def diary_after_job_task_done(booking_id: str, task_id: str, request: Request,
                                    ctx: TenantCtx = Depends(require_onboarded)):
    """Tick an after-job task off — with an optional file as proof (a
    certificate, a photo), kept on the job and the customer's record."""
    from app import after_job, documents, photos
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    doc_id = None
    f = form.get("proof")
    if isinstance(f, FormUploadFile) and f.filename:
        data = await f.read()
        if data and len(data) <= documents.MAX_BYTES * 3:
            name, data, ct = photos.web_ready(data, f.filename, f.content_type)
            if len(data) <= documents.MAX_BYTES:
                c = db.crm_contact_by_email(ctx.tenant_id, b.get("contact")) if b.get("contact") else None
                row = documents.store(ctx.tenant_id, filename=name, data=data, content_type=ct,
                                      source="upload", contact_email=(b.get("contact") or "").lower() or None,
                                      contact_id=(c or {}).get("id"), booking_id=booking_id)
                doc_id = (row or {}).get("id")
    after_job.complete(ctx.tenant_id, task_id, doc_id=doc_id)
    return RedirectResponse(f"/app/diary/{booking_id}?msg=taskdone#after-job", status_code=303)


@router.post("/diary/{booking_id}/photos", dependencies=[Depends(require_csrf)])
async def diary_job_photos(booking_id: str, request: Request,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Photos of the job, taken on the phone — before and after, the fault,
    the finished work (Kris 2026-09-26: evidence "both ways"). Kept pinned to
    this booking and on the customer's record, so the thread can attach them
    to a reply."""
    from app import documents, photos
    back = f"/app/diary/{booking_id}"
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    contact = (b.get("contact") or "").strip().lower() or None
    c = db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client) if contact else None
    kept = 0
    for f in form.getlist("photo"):
        if not isinstance(f, FormUploadFile) or not f.filename:
            continue
        data = await f.read()
        if not data or len(data) > documents.MAX_BYTES * 3:
            continue
        name, data, ct = photos.web_ready(data, f.filename, f.content_type)
        if len(data) > documents.MAX_BYTES:
            continue
        if documents.store(ctx.tenant_id, filename=name, data=data, content_type=ct, source="upload",
                           contact_email=contact, contact_id=(c or {}).get("id"),
                           booking_id=booking_id):
            kept += 1
    if not kept:
        return RedirectResponse(back + "?msg=photoerr#photos", status_code=303)
    db.log_agent_event(ctx.tenant_id, "document",
                       f"Kept {kept} photo{'s' if kept != 1 else ''} on the job "
                       f"({b.get('summary') or 'booking'})", contact=contact)
    return RedirectResponse(back + "?msg=photos#photos", status_code=303)


@router.post("/diary/{booking_id}/people", dependencies=[Depends(require_csrf)])
async def diary_job_people(booking_id: str, request: Request,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Who's on it (src/app/job_people.py): exactly the people ticked, each with
    their part — the first only, when the business works one person per job.
    Putting people on work is the Diary's "add and move" ability."""
    from app import job_people
    if not people.can_do(_me(ctx), "diary", "add"):
        return RedirectResponse(f"/app/diary/{booking_id}", status_code=303)
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    def _blocking():
        ids = [str(v) for v in form.getlist("person")]
        parts = {pid: str(form.get(f"part_{pid}") or "") for pid in ids}
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        try:
            job_people.set_people(ctx.tenant_id, booking_id, ids, parts, cfg=cfg, by=ctx.user_id)
        except Exception:
            log.warning("job people save failed (tenant=%s)", ctx.tenant_id, exc_info=True)
            return RedirectResponse(f"/app/diary/{booking_id}?msg=editerr", status_code=303)
        from app import job_sheets   # anyone on it who doesn't sign in is sent it
        job_sheets.soon(ctx.tenant_id, booking_id)
        return RedirectResponse(f"/app/diary/{booking_id}?msg=people", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/diary/people-per-job", dependencies=[Depends(require_csrf)])
def diary_people_per_job(value: str = Form("one"), ctx: TenantCtx = Depends(require_onboarded)):
    """Diary settings: one person per job, or several (Kris 2026-09-24:
    "configurable — agnostic")."""
    from app import job_people
    if not people.can_do(_me(ctx), "settings", "change"):
        return RedirectResponse("/app/diary/settings", status_code=303)
    db.upsert_scheduling_config(ctx.tenant_id, {
        "people_per_job": job_people.SEVERAL if value == job_people.SEVERAL else job_people.ONE},
        client=ctx.client)
    return RedirectResponse("/app/diary/settings?sched=saved", status_code=303)


@router.post("/diary/{booking_id}/on-my-way", dependencies=[Depends(require_csrf)])
async def diary_on_my_way(booking_id: str, eta: str = Form(...), engineer: str = Form(""),
                          return_to: str = Form(""), action_key: str = Form("initial"),
                          resend_attempt: str = Form(""),
                          ctx: TenantCtx = Depends(require_onboarded)):
    """The touch on the day (src/app/on_my_way.py): the operator's tap sends the
    message — to the on-site party, else the booking's contact — with the ETA
    they chose. Nothing to approve after: the tap is the approval."""
    from app import on_my_way
    require_can(ctx, "diary", "add")
    mods = _diary_modules(ctx)
    if not ({"scheduling", "tms"} & mods):
        raise HTTPException(status_code=404, detail="Scheduling is not active for this account")
    booking = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if not booking:
        raise HTTPException(status_code=404, detail="No such booking")
    back = _safe_next(return_to or f"/app/diary/{booking_id}")
    if booking.get("status") not in ("confirmed", "proposed"):
        return RedirectResponse(back + "?msg=omw_notlive", status_code=303)
    # The same gate as the job page: only a job the business travels to.
    try:
        sched = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    except Exception:
        sched = {}
    if not on_my_way.goes_to_customer(sched.get("intake_fields"), booking):
        return RedirectResponse(back + "?msg=omw_nowhere", status_code=303)
    resend = {"resend_attempt": resend_attempt} if resend_attempt else {}
    out = await on_my_way.send(ctx.tenant_id, booking, eta, engineer=engineer.strip()[:60] or None,
                               client=ctx.client, action_key=action_key, **resend)
    if not out.get("ok"):
        return RedirectResponse(back + "?msg=omw_" + str(out.get("error") or "failed"), status_code=303)
    # No promise is settled here (run #6, 2026-09-04: the tap resolved "we will
    # collect the asset" before the asset was collected) — an ETA is not the visit;
    # the visit's completion keeps the promise.
    return RedirectResponse(back + "?msg=omw_sent", status_code=303)


@router.post("/diary/cancel-all", dependencies=[Depends(require_csrf)])
async def diary_cancel_all(return_to: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Every live booking (offered, proposed, confirmed — past dates too) →
    cancelled, each pushed calendar event removed first. One call instead of
    one per row (Kris 2026-09-04: the e2e rig reset cleared the diary booking
    by booking, and a row missed leaves an orphan event the busy-sync brings
    back as a ref-less block). Same door as the single-row route: the signed-in
    tenant's session + CSRF. Two callers: the diary's own "Cancel every live
    booking" control posts a form with `return_to` and is sent back with a
    flash (Kris 2026-09-05, run #7 fix 12b — a page control, not a script);
    a bare call gets what it did as JSON."""
    require_can(ctx, "diary", "cancel")
    mods = _diary_modules(ctx)
    if not ({"scheduling", "tms"} & mods):
        raise HTTPException(status_code=404, detail="Scheduling is not active for this account")
    rows = db.list_live_bookings(ctx.tenant_id, client=ctx.client)
    done = []
    for b in rows:
        await calendar_sync.remove_pushed_event(ctx.tenant_id, b["id"])
        db.set_booking_status(b["id"], "cancelled", ctx.tenant_id, client=ctx.client)
        done.append(b.get("booking_ref") or b["id"][:8])
        if b.get("contact"):
            events.publish(ctx.tenant_id, {"type": "status_changed",
                                           "log_id": b.get("email_log_id"),
                                           "contact": b.get("contact"),
                                           "booking_id": b["id"], "status": "cancelled"})
    def _blocking():
        if done:
            db.log_agent_event(ctx.tenant_id, "booking_manual",
                               f"Cancelled every live booking by hand in the diary ({len(done)}): "
                               + ", ".join(done))
        if return_to.startswith("/app/"):
            return RedirectResponse(f"{return_to}?msg=cancelled_all&n={len(done)}", status_code=303)
        return {"cancelled": len(done), "refs": done}
    return await run_in_threadpool(_blocking)


@router.get("/tms")
def tms_view(request: Request, branch: str = "",
             ctx: TenantCtx = Depends(require_onboarded)):
    """The TMS cockpit — the Fleet & Capacity chip's landing page. One screen
    over the live operation: the daysheet (dispatch strip), utilisation KPIs,
    the exceptions a transport office chases, the planner's nudges, and the
    order-intake feed straight off the diary the email agent books into.
    Multi-branch tenants get per-branch tiles and a ?branch= lens (the
    enterprise view). A surface over fleet (+ routing when on), not a new
    module — gated like the board."""
    _require_tms(ctx)
    mods = _diary_modules(ctx)
    data = tms.view(ctx.tenant_id, client=ctx.client,
                    scheduling_on="scheduling" in mods,
                    routing_on="tms" in mods,
                    branch_id=branch.strip() or None)
    return templates.TemplateResponse(request, "app/tms.html",
                                      _ctx("tms", ctx, **data))


# ── TMS baseline surfaces (pricing · POD · invoicing · tracking · compliance) ─
# Each gates on _require_tms — they're sections of the one transport add-on.
@router.get("/tms/pricing")
def tms_pricing(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    data = pricing.view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/tms_pricing.html",
                                      _ctx("pricing", ctx, **data))


@router.post("/tms/pricing/rate", dependencies=[Depends(require_csrf)])
async def tms_save_rate(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    form = await request.form()
    def _blocking():
        fields = {}
        for f in pricing.RATE_FIELDS:
            try:
                fields[f] = float((form.get(f) or "").strip() or 0)
            except ValueError:
                fields[f] = 0.0
        db.upsert_rate_card(ctx.tenant_id, fields, client=ctx.client)
        return RedirectResponse("/app/tms/pricing?msg=saved", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/tms/pricing/quote/{booking_id}", dependencies=[Depends(require_csrf)])
def tms_quote_booking(booking_id: str, amount: str = Form(...),
                      ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    try:
        amt = round(float(amount), 2)
    except ValueError:
        return RedirectResponse("/app/tms/pricing?err=badamount", status_code=303)
    db.set_booking_quote(booking_id, ctx.tenant_id, amt, client=ctx.client)
    return RedirectResponse("/app/tms/pricing?msg=quoted", status_code=303)


# POD board + the shared mark-delivered write path (the driver app posts here too).
@router.get("/tms/pod")
def tms_pod(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    data = pod.view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/tms_pod.html",
                                      _ctx("pod", ctx, **data))


@router.post("/tms/pod/{booking_id}", dependencies=[Depends(require_csrf)])
def tms_mark_pod(booking_id: str, outcome: str = Form(...), signed_by: str = Form(""),
                 note: str = Form(""), return_to: str = Form("/app/tms/pod"),
                 ctx: TenantCtx = Depends(require_onboarded)):
    """One write path for both the office POD board and the driver app: record
    the delivered/failed event (signature) and complete the booking on delivery."""
    _require_tms(ctx)
    db.mark_delivered(booking_id, ctx.tenant_id, outcome == "delivered",
                      signed_by=signed_by.strip() or None,
                      note=note.strip() or None, client=ctx.client)
    target = return_to if return_to.startswith("/app/") else "/app/tms/pod"
    # carry a success flag for the POD board (the driver app ignores it)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg=pod", status_code=303)


# Driver app — a mobile run sheet (own layout, no office chrome).
@router.get("/driver")
def driver_run_view(request: Request, vehicle: str = "",
                    ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    data = driver.view(ctx.tenant_id, vehicle_id=vehicle.strip() or None, client=ctx.client)
    return templates.TemplateResponse(request, "app/driver.html", {**data})


# Invoicing — group delivered jobs by customer into invoices.
def _default_invoice_period() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=30)).isoformat(), today.isoformat()


# Invoicing is ONE shared surface across every vertical (docs/modules/module-invoicing.md): the
# generic engine (invoices/invoice_lines + create_invoice/VAT/refs) plus a per-vertical
# line-builder (invoicing.draft_view_for dispatches on the tenant's module) and a modular
# invoice-settings config (which sections show, the VAT rate). Order matters: the static
# /invoices/settings + /invoices/generate + /invoices/new routes are declared BEFORE
# /invoices/{id}.
def _require_invoicing(ctx: TenantCtx) -> set:
    """The invoicing surface is standalone core since the Field OS extraction — every
    onboarded tenant has it. Returns the tenant's visible modules (the generate flow
    still dispatches its draft builder on the vertical)."""
    return db.visible_modules(ctx.tenant_id, client=ctx.client)


@router.get("/invoices")
def invoices_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    mods = _require_invoicing(ctx)
    pf = request.query_params.get("from") or ""
    pt = request.query_params.get("to") or ""
    if not (pf and pt):
        pf, pt = _default_invoice_period()
    return templates.TemplateResponse(request, "app/invoices.html", _ctx(
        "invoices", ctx, period_from=pf, period_to=pt,
        drafts=invoicing.draft_view_for(ctx.tenant_id, mods, pf, pt, client=ctx.client),
        export_formats=invoice_export.FORMAT_LABELS,
        invoices=db.list_invoices(ctx.tenant_id, client=ctx.client)))


@router.post("/invoices/generate", dependencies=[Depends(require_csrf)])
def invoices_generate(period_from: str = Form(...), period_to: str = Form(...),
                      customers: list[str] = Form([]),
                      ctx: TenantCtx = Depends(require_onboarded)):
    mods = _require_invoicing(ctx)
    pf, pt = period_from.strip(), period_to.strip()
    drafts = invoicing.draft_view_for(ctx.tenant_id, mods, pf, pt, client=ctx.client)
    # Only the customers ticked in the preview. Empty selection = all of them, so a
    # caller that posts no selection behaves exactly as before.
    drafts = invoicing.select_drafts(drafts, customers)
    for d in drafts:
        # Link the invoice to its CRM contact, like the manual (/invoices/new) and
        # concierge paths already do. Without this the BULK path — the main one —
        # orphaned every invoice it created, so client history and the assistant's
        # grounding both missed everything generated in bulk. Contacts pre-fill on
        # every tier, so this works on Starter too; only *viewing* the CRM is Pro.
        email = (d.get("customer_email") or "").strip()
        contact = db.crm_contact_by_email(ctx.tenant_id, email,
                                          client=ctx.client) if email else None
        db.create_invoice(ctx.tenant_id, d["customer_email"], d["customer_name"],
                          pf, pt, d["lines"], client=ctx.client,
                          contact_id=(contact or {}).get("id"))
    if not drafts:
        return RedirectResponse("/app/invoices?msg=none", status_code=303)
    return RedirectResponse(f"/app/invoices?msg=generated&n={len(drafts)}", status_code=303)


@router.get("/money")
def money_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Cashflow: what's owed, what's late, who owes it. Core for every tenant, like
    invoicing itself. Deliberately NOT called "Accounting" — see src/app/money.py."""
    _require_invoicing(ctx)
    invoices = db.list_invoices(ctx.tenant_id, client=ctx.client)
    quotes = db.list_quotes(ctx.tenant_id, client=ctx.client)
    today_d = date.today()
    month_start = today_d.replace(day=1).isoformat()
    # Page 4 of the walk-through (Kris 2026-09-13: "now the Money page?").
    tour_page, tour_plural = _tour_for(ctx, {}, "money", force=request.query_params.get("tour") == "1")
    from app import wallet as _wallet
    packages_m = _wallet.summary(ctx.tenant_id, client=ctx.client)
    m = money.view(
        invoices,
        db.summarize_invoice_rows(invoices),
        db.summarize_invoice_rows(
            [i for i in invoices if (i.get("created_at") or "") >= month_start]),
        today_d,
        quotes=quotes)

    # Money's lens on the dock rail: dated money — what's late, what's coming
    # due, and money promised (accepted quotes not yet invoiced).
    def _day(iso):
        try:
            return date.fromisoformat(str(iso)[:10]).strftime("%d %b").lstrip("0")
        except ValueError:
            return str(iso)[:10]

    dock = []
    for r in m["overdue"][:4]:
        dock.append({"tone": "rose", "href": f"/app/invoices/{r['id']}",
                     "label": f"{r['days_late']}d late · {r['customer']} · £{r['outstanding']:,.0f}"})
    upcoming = sorted(
        (i for i in money.owing(invoices)
         if (i.get("due_date") or "")[:10] >= today_d.isoformat()),
        key=lambda i: i.get("due_date") or "")
    for i in upcoming[:4]:
        dock.append({"tone": "plain", "href": f"/app/invoices/{i['id']}",
                     "label": f"due {_day(i.get('due_date'))} · {i.get('customer_name') or i.get('customer_email') or '—'} · £{float(i.get('total') or 0):,.0f}"})
    for qr in [q for q in quotes if (q.get("status") or "") == "accepted"][:3]:
        dock.append({"tone": "emerald", "href": f"/app/quotes/{qr['id']}",
                     "label": f"£{float(qr.get('total') or 0):,.0f} accepted · {qr.get('customer_name') or '—'}"})

    # Chases already staged and waiting for a yes — the overdue table shows
    # "review the draft" instead of offering to chase what's already chased.
    from app import invoice_chase
    chase_pending = {}
    for r in db.list_pending_invoice_chases(ctx.tenant_id, client=ctx.client):
        ref = invoice_chase.chase_ref_from_intent(r.get("body_in"))
        if ref:
            chase_pending[ref] = r.get("from_address") or ""

    return templates.TemplateResponse(request, "app/money.html", _ctx(
        "money", ctx, m=m, dock=dock, chase_pending=chase_pending, packages_m=packages_m,
        export_formats=invoice_export.FORMAT_LABELS,
        tour_page=tour_page, tour_kind_plural=tour_plural))


@router.get("/money/overdue")
def money_overdue_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The chase board — the Overdue row's own page: the debt by age, each
    invoice's chase trail, and the engine's stated NEXT move ('fires 11th Sep',
    'next morning run') — the proactive promise made legible."""
    _require_invoicing(ctx)
    invoices = db.list_invoices(ctx.tenant_id, client=ctx.client)
    ov = money.overdue_view(invoices, date.today())
    from app import invoice_chase
    chase_pending = {}
    for r in db.list_pending_invoice_chases(ctx.tenant_id, client=ctx.client):
        ref = invoice_chase.chase_ref_from_intent(r.get("body_in"))
        if ref:
            chase_pending[ref] = r.get("from_address") or ""
    return templates.TemplateResponse(request, "app/money_overdue.html", _ctx(
        "money", ctx, ov=ov, chase_pending=chase_pending,
        msg=request.query_params.get("msg")))


@router.get("/money/collected")
def money_collected_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The Collected rows' own page: all-time = the collection story month by
    month plus who pays you most; ?month=YYYY-MM = that month's payments one
    by one, with how long each took to arrive."""
    _require_invoicing(ctx)
    month = (request.query_params.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = ""
    invoices = db.list_invoices(ctx.tenant_id, client=ctx.client)
    c = money.collections_view(invoices, month=month or None)
    return templates.TemplateResponse(request, "app/money_collected.html", _ctx(
        "money", ctx, c=c))


@router.post("/invoices/{invoice_id}/chase", dependencies=[Depends(require_csrf)])
async def invoice_chase_now(invoice_id: str,
                            ctx: TenantCtx = Depends(require_onboarded)):
    """The Money page's per-row lever on the proactive chase engine: stage the
    next chase NOW instead of waiting for the 08:45 milestone. Always lands as
    a draft for review (the operator asked for a chase, not a send); the
    deterministic per-day dedupe id means double-clicking can't stage twice."""
    require_can(ctx, "money", "chase")
    _require_invoicing(ctx)
    from app import invoice_chase, money as money_mod
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if (inv.get("status") or "") not in money_mod.OWING_STATUSES:
        return RedirectResponse("/app/money", status_code=303)
    # keyword! effective_config's second positional is mailbox_id — passing the
    # client there sent a Supabase client into a mailbox lookup (500, live e2e
    # 2026-09-01; the tests' loose lambda stub let it through).
    cfg = db.effective_config(ctx.tenant_id, client=ctx.client)
    days = money_mod.days_late(inv, date.today())
    outcome, _ = await invoice_chase.stage_chase(
        ctx.tenant_id, cfg, inv, days, auto=False,
        dedupe_label=f"now-{date.today().isoformat()}")
    def _blocking():
        if outcome in ("drafted", "sent"):
            # A manual chase IS a chase: stamp the milestone it satisfies, or the
            # 08:45 ladder stages ANOTHER one hours later (live, 2026-09-02: e2e
            # chased at 21:22, the morning run re-drafted the same invoice at 08:45).
            from app.jobs.check_overdue_invoices import MILESTONES
            milestone = next((m for m in MILESTONES if days >= m), 0)
            already = int(inv.get("overdue_notified_days") or 0)
            if milestone > already:
                db.set_invoice_overdue_milestone(invoice_id, ctx.tenant_id, milestone,
                                                 client=ctx.client)
        to = (inv.get("customer_email") or "").strip()
        if outcome == "drafted" and to:
            return RedirectResponse("/app/conversations/view?contact=" + quote(to),
                                    status_code=303)
        return RedirectResponse(f"/app/money?msg=chase_{outcome}", status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/invoices/export.csv")
def invoices_export_csv(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Invoices as CSV for an accounting package — the integrate-don't-compete play
    (docs/modules/module-invoice-export.md).

    ⚠️ Declared BEFORE /invoices/{invoice_id}: FastAPI matches routes in declaration
    order, so placing this any later lets the uuid route swallow "export.csv" and
    return a 404 for a path that exists.
    """
    _guards.require_admin(ctx)   # whole-business export: owner or manager (M4)
    _require_invoicing(ctx)
    q = request.query_params
    fmt = q.get("format") or "accountant"
    if fmt not in invoice_export.FORMATS:
        fmt = "accountant"
    rows = invoice_export.filter_invoices(
        db.list_invoices(ctx.tenant_id, client=ctx.client),
        q.get("from") or "", q.get("to") or "", q.get("status") or "")
    lines = db.invoice_lines_by_invoice(ctx.tenant_id, [r["id"] for r in rows],
                                       client=ctx.client)
    inv_settings = invoice_config.settings_for(
        db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    body = invoice_export.build_csv(
        fmt, rows, lines, inv_settings,
        account_code=(q.get("account") or invoice_export.DEFAULT_ACCOUNT_CODE).strip(),
        tax_name=(q.get("tax") or "").strip())
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{invoice_export.filename(fmt)}"'})


@router.get("/invoices/settings")
def invoice_settings_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    from app import invoice_config, payments
    row = db.get_invoice_settings(ctx.tenant_id, client=ctx.client)
    s = invoice_config.settings_for(row)
    # The business's name, already given at step 1, comes pre-filled (Kris
    # 2026-09-25, rehearsal: step 9 "Getting paid" only counts as done with a
    # name or bank details — a plain Save left it undone, with no clue why).
    if not (s.get("business_name") or "").strip():
        try:
            s["business_name"] = ((db.get_tenant(ctx.tenant_id, client=ctx.client) or {})
                                  .get("name") or "").strip() or None
        except Exception:
            pass
    from app import payments_panel, quote_autodraft
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    # Card payments (docs/plan-payments-panel.md §0.6): which methods the connected
    # Stripe account can take, so the bank-methods threshold reads truthfully.
    acct = (db.get_stripe_account_id(ctx.tenant_id, client=ctx.client)
            if payments.connect_configured() else None)
    stripe_methods = payments_panel.account_methods(acct) if acct else None
    return templates.TemplateResponse(request, "app/invoice_settings.html", _ctx(
        "invoice_settings", ctx, s=s, msg=request.query_params.get("msg"),
        invoice_snapshot={"invoice": row or {}, "quote_auto_draft": cfg.get("quote_auto_draft", "propose")},
        section_labels=invoice_config.SECTION_LABELS,
        quote_auto_draft=quote_autodraft.mode_for(cfg),
        stripe_available=payments.connect_configured(),
        stripe_connected=bool(acct),
        stripe_methods=stripe_methods,
        stripe_methods_url=payments_panel.STRIPE_PAYMENT_METHODS_SETTINGS,
        setup_prefill=_setup_prefill(ctx, "invoice_settings", fresh=not row)))


def _preview_settings(form) -> dict:
    """The settings dict a posted (possibly unsaved) form WOULD produce — the
    same clean_form → settings_for pipeline a real save goes through, run on a
    throwaway dict. Nothing persists."""
    from app import invoice_config
    return invoice_config.settings_for(invoice_config.clean_form(form))


@router.post("/invoices/settings/preview", dependencies=[Depends(require_csrf)])
async def invoice_settings_preview(request: Request,
                                   ctx: TenantCtx = Depends(require_onboarded)):
    """The live preview: current form state in, the sample invoice fragment out —
    rendered through the SAME section macro as the real invoice page."""
    _require_invoicing(ctx)
    from app import invoice_config
    s = _preview_settings(await request.form())
    def _blocking():
        inv, lines = invoice_config.sample_invoice(s)
        return templates.TemplateResponse(request, "app/_invoice_preview.html", {
            "inv": inv, "lines": lines, "s": s,
            "sbadge": {"sent": "bg-blue-100 text-blue-700"}})
    return await run_in_threadpool(_blocking)


@router.post("/invoices/settings/preview.pdf", dependencies=[Depends(require_csrf)])
async def invoice_settings_preview_pdf(request: Request,
                                       ctx: TenantCtx = Depends(require_onboarded)):
    """Sample PDF from the current form state — the attachment a customer would
    actually open, via the real PDF engine (formtarget=_blank on the form)."""
    _require_invoicing(ctx)
    from fastapi.responses import Response

    from app import invoice_config, invoice_pdf
    s = _preview_settings(await request.form())
    def _blocking():
        inv, lines = invoice_config.sample_invoice(s)
        pdf = invoice_pdf.render(inv, lines, s)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 'inline; filename="sample-invoice.pdf"'})
    return await run_in_threadpool(_blocking)


@router.post("/invoices/settings", dependencies=[Depends(require_csrf)])
async def invoice_settings_save(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    from app import invoice_config, quote_autodraft
    from app import wallet
    form = await request.form()
    def _blocking():
        import json
        try:
            raw = form.get("invoice_snapshot")
            if not isinstance(raw, str) or len(raw) > 120000:
                raise ValueError()
            expected = json.loads(raw)
            if (not isinstance(expected, dict) or set(expected) != {"invoice", "quote_auto_draft"}
                    or not isinstance(expected["invoice"], dict)
                    or not isinstance(expected["invoice"].get("extras") or {}, dict)
                    or not isinstance(expected["quote_auto_draft"], (str, type(None)))):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(409, "This form has no valid saved version. Reload invoice settings before saving.") from None
        fields = invoice_config.clean_form(form)
        # Packages (src/app/wallet.py) live under extras beside section_order; the
        # face posts its rows and prefs with the rest of the page.
        existing = expected["invoice"]
        extras = dict(existing.get("extras") or {})
        extras.update(fields.get("extras") or {})
        if form.get("pkg_form") == "1":
            extras["packages"] = wallet.parse_packages_form(form)
            extras["package_prefs"] = wallet.clean_prefs({
                "paid_only": form.get("packages_paid_only") == "1",
                "low_at": form.get("package_low_at"), "expiry_days": form.get("package_expiry_days"),
                "portal": form.get("packages_portal") == "1"})
        fields["extras"] = extras
        mode = str(form.get("quote_auto_draft") or "").strip().lower()
        mode = mode if mode in quote_autodraft.MODES else None
        try:
            db.save_invoice_settings_checked(ctx.tenant_id, expected, fields, mode, client=ctx.client)
        except db.InvoiceSettingsStale:
            # Nothing was written: show the page with the latest values and a message.
            return RedirectResponse("/app/invoices/settings?err=stale", status_code=303)
        except Exception:
            raise HTTPException(503, "The save could not be confirmed. Reload invoice settings to check before trying again.") from None
        _held_used(ctx, "invoice_settings")
        return RedirectResponse("/app/invoices/settings?msg=saved", status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/invoices/new")
def invoice_new_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Standalone invoice — the extraction's new path: any tenant can bill anything,
    no completed-work period required (the concierge uses the same create path)."""
    _require_invoicing(ctx)
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    from app import wallet
    return templates.TemplateResponse(request, "app/invoice_new.html", _ctx(
        "invoices", ctx, msg=request.query_params.get("msg"),
        # The blank-due-date hint states the tenant's real default (their payment
        # terms), not a hard-coded 14 (Kris 2026-09-04).
        due_days=invoice_config.due_days(s),
        # Packages (src/app/wallet.py): a line can sell one — picked, it fills the row.
        packages=wallet.packages_of(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))))


@router.post("/invoices/new", dependencies=[Depends(require_csrf)])
async def invoice_new_create(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    form = await request.form()
    def _blocking():
        email = (form.get("customer_email") or "").strip()
        name = (form.get("customer_name") or "").strip()
        if not (email or name):
            return RedirectResponse("/app/invoices/new?msg=nocustomer", status_code=303)
        lines = []
        from itertools import zip_longest
        for desc, qty, unit, pkey, for_name in zip_longest(form.getlist("description"), form.getlist("quantity"),
                                                           form.getlist("unit_price"), form.getlist("package_key"),
                                                           form.getlist("for_name"), fillvalue=""):
            desc = (desc or "").strip()
            if not desc:
                continue
            try:
                q = max(float(qty), 0.0)
            except (TypeError, ValueError):
                q = 1.0
            try:
                u = round(float(unit), 2)
            except (TypeError, ValueError):
                u = 0.0
            lines.append({"description": desc[:200], "quantity": q or 1.0,
                          "unit_price": u, "amount": round((q or 1.0) * u, 2),
                          "package_key": (pkey or "").strip() or None,
                          "for_name": (for_name or "").strip() or None})
        if not lines:
            return RedirectResponse("/app/invoices/new?msg=nolines", status_code=303)
        # 2b nit: "for (name)" on a package line → that dependant's credits.
        if any(l.get("for_name") for l in lines) and email:
            try:
                _c = db.crm_contact_by_email(ctx.tenant_id, email, client=ctx.client)
                _insts = db.list_requirement_instances(ctx.tenant_id, _c["id"], client=ctx.client) if _c else []
                for l in lines:
                    nm = (l.get("for_name") or "").lower().split()
                    if nm:
                        hit = [i for i in _insts if str(i.get("name") or "").lower().split()[:1] == nm[:1]]
                        if len(hit) == 1:
                            l["instance_id"] = hit[0]["id"]
            except Exception:
                log.warning("invoice for_name match failed", exc_info=True)
        for l in lines:
            l.pop("for_name", None)
        contact = db.crm_contact_by_email(ctx.tenant_id, email) if email else None
        inv = db.create_invoice(
            ctx.tenant_id, email or None, name or (contact or {}).get("name"),
            None, None, lines, client=ctx.client,
            contact_id=(contact or {}).get("id"),
            due_date=(form.get("due_date") or "").strip() or None,
            notes=(form.get("notes") or "").strip() or None,
            customer_reference=(form.get("customer_reference") or "").strip() or None,
            created_by="owner")
        if inv is None:
            return RedirectResponse("/app/invoices/new?msg=failed", status_code=303)
        return RedirectResponse(f"/app/invoices/{inv['id']}", status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/invoices/{invoice_id}/pdf")
def invoice_pdf_view(invoice_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """The invoice's PDF, inline — the file the customer's email carried (Kris
    2026-09-20: the paperclip on a sent invoice opens it)."""
    from app import invoice_config, invoice_pdf
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client)
    if not inv:
        raise HTTPException(status_code=404, detail="No such invoice")
    lines = db.list_invoice_lines(invoice_id, ctx.tenant_id, client=ctx.client)
    s = invoice_config.settings_for(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    pdf = invoice_pdf.render(inv, lines, s)
    ref = inv.get("invoice_ref") or "invoice"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{ref}.pdf"'})


@router.get("/invoices/{invoice_id}")
def invoice_detail(request: Request, invoice_id: str,
                   ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    from app import invoice_config, invoice_send as invsend, payments
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    lines = db.list_invoice_lines(invoice_id, ctx.tenant_id, client=ctx.client)
    s = invoice_config.settings_for(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    pay_now = payments.tenant_can_take_payments(ctx.tenant_id, client=ctx.client)
    payment_gap = None if pay_now else invsend.payment_route_gap(s, None)
    from app import payments_panel
    return templates.TemplateResponse(request, "app/invoice.html",
                                      _ctx("invoices", ctx, inv=inv, lines=lines, s=s,
                                           payment_gap=payment_gap,
                                           stripe_connected=pay_now,
                                           method_label=payments_panel.method_label,
                                           pretty_date=pretty_date))


@router.post("/invoices/{invoice_id}/status", dependencies=[Depends(require_csrf)])
def invoice_status(invoice_id: str, status: str = Form(...),
                   payment_method: str = Form("other"),
                   ctx: TenantCtx = Depends(require_onboarded)):
    """Status transitions. 'paid' goes through mark_invoice_paid (idempotent, stamps
    method + amount) and then the shared paid-followup — same lifecycle whether the
    money arrived via Stripe, bank transfer or cash."""
    from app import payments
    _require_invoicing(ctx)
    if status == "paid":
        method = payment_method if payment_method in (
            "card", "bank_transfer", "cash", "other") else "other"
        row = db.mark_invoice_paid(invoice_id, ctx.tenant_id, payment_method=method,
                                   client=ctx.client)
        if row:
            payments.invoice_paid_followup(ctx.tenant_id, row,
                                           via=f"marked paid by you ({method})")
    elif status in ("draft", "sent", "cancelled", "void"):
        db.set_invoice_status(invoice_id, ctx.tenant_id, status, client=ctx.client)
    return RedirectResponse(f"/app/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/lines", dependencies=[Depends(require_csrf)])
async def invoice_edit_lines(request: Request, invoice_id: str,
                             ctx: TenantCtx = Depends(require_onboarded)):
    """Edit a DRAFT invoice's lines before issuing (Move 2). Replaces the lines and
    recomputes totals; blank-description rows are dropped (that's how you remove a line)."""
    _require_invoicing(ctx)
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if inv.get("status") != "draft":
        return RedirectResponse(f"/app/invoices/{invoice_id}?msg=locked", status_code=303)
    form = await request.form()
    def _blocking():
        lines = []
        # booking_id is carried hidden per row and preserved: dropping it here used to
        # break the line's link to its job, which silently freed that work to be invoiced
        # again by the generate flow. zip_longest so a row missing the hidden input can
        # never shift the columns out of alignment.
        for desc, amt, bid in zip_longest(form.getlist("description"),
                                          form.getlist("amount"),
                                          form.getlist("booking_id")):
            desc = (desc or "").strip()
            if not desc:
                continue
            try:
                value = round(float(amt), 2)
            except (TypeError, ValueError):
                value = 0.0
            lines.append({"description": desc[:200], "amount": value,
                          "booking_id": (bid or "").strip() or None})
        db.update_invoice_lines(invoice_id, ctx.tenant_id, lines, client=ctx.client)
        return RedirectResponse(f"/app/invoices/{invoice_id}?msg=saved", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/invoices/{invoice_id}/send", dependencies=[Depends(require_csrf)])
async def invoice_send(invoice_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Email the invoice (as a PDF, with a Pay Now link when Stripe is connected) to
    the customer from the tenant's mailbox, then stamp it sent (Move 2). A failure
    leaves the invoice as-is with an error flag. The sender itself is shared with
    the concierge's invoice_send executor — src/app/invoice_send.py."""
    require_can(ctx, "money", "send")
    from app import invoice_send as invsend
    _require_invoicing(ctx)
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if not (inv.get("customer_email") or "").strip():
        return RedirectResponse(f"/app/invoices/{invoice_id}?msg=noemail", status_code=303)
    try:
        await invsend.send_invoice_email(ctx.tenant_id, inv, client=ctx.client)
    except Exception:
        log.warning("invoice send failed for %s", invoice_id, exc_info=True)
        return RedirectResponse(f"/app/invoices/{invoice_id}?msg=sendfail", status_code=303)
    return RedirectResponse(f"/app/invoices/{invoice_id}?msg=sent", status_code=303)


# ── Quotes (docs/modules/module-quotes.md) — the invoice surface's sibling ──────────
# Same access rule as invoicing (standalone core, every tenant), same route
# shape (/quotes/new declared BEFORE /quotes/{quote_id}).

@router.get("/quotes")
def quotes_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    quotes = db.list_quotes(ctx.tenant_id, client=ctx.client)
    status = (request.query_params.get("status") or "").strip()
    shown = [q for q in quotes if q.get("status") == status] if status else quotes
    outstanding = [q for q in quotes if q.get("status") == "sent"]
    return templates.TemplateResponse(request, "app/quotes.html", _ctx(
        "quotes", ctx, quotes=shown, status_filter=status,
        counts={s: sum(1 for q in quotes if q.get("status") == s)
                for s in ("draft", "sent", "accepted", "declined", "expired",
                          "invoiced", "cancelled")},
        outstanding_n=len(outstanding),
        outstanding_value=sum(float(q.get("total") or 0) for q in outstanding)))


def _quote_lines_from_form(form) -> list[dict]:
    """Posted editor rows → line dicts. Five parallel lists ride every row
    (including the blank add-a-line ones) so the columns stay index-aligned —
    the invoice editor's zip_longest lesson, with three more hidden carriers:
    kb_entry_id and is_selected are preserved through edits (is_selected is the
    CUSTOMER's optional-line tick; an owner edit must never silently re-add an
    extra the customer deselected), optionality posts from a select (a checkbox
    wouldn't post unticked rows and would shear the alignment)."""
    lines = []
    for i, (desc, amt, opt, bid, kbid, sel, cnt) in enumerate(zip_longest(
            form.getlist("description"), form.getlist("amount"),
            form.getlist("optionality"), form.getlist("booking_id"),
            form.getlist("kb_entry_id"), form.getlist("is_selected"),
            form.getlist("counts_deposit"))):
        desc = (desc or "").strip()
        if not desc:
            continue
        try:
            value = round(float(amt), 2)
        except (TypeError, ValueError):
            value = 0.0
        is_optional = (opt or "").strip() == "optional"
        lines.append({"description": desc[:200], "amount": value,
                      "is_optional": is_optional,
                      # A required line is always selected; an optional one keeps
                      # the customer's tick (new rows default to ticked).
                      "is_selected": True if not is_optional else (sel or "1") == "1",
                      "booking_id": (bid or "").strip() or None,
                      "kb_entry_id": (kbid or "").strip() or None,
                      # "Counts towards the deposit rule" (a hidden 0/1 carrier per
                      # row keeps the lists aligned; an unticked box posts nothing).
                      "line_order": i,
                      **({"counts_to_deposit_rule": True} if (cnt or "").strip() == "1" else {})})
    return lines


def _open_deals_for_email(ctx: TenantCtx, email: str) -> list[dict]:
    """This customer's OPEN deals, newest first — what the quote form offers when
    a job can't be walked to a deal on its own."""
    email = (email or "").strip()
    if not email:
        return []
    contact = db.crm_contact_by_email(ctx.tenant_id, email, client=ctx.client)
    if not contact:
        return []
    return [d for d in db.crm_list_deals(ctx.tenant_id, contact_id=contact["id"],
                                         client=ctx.client)
            if (d.get("status") or "open") == "open"]


def _picked_deal_id(ctx: TenantCtx, deal_id: str, email: str) -> str | None:
    """A deal id chosen on the form is only trusted if it is genuinely one of
    THIS customer's open deals — the same rule crm_intel.apply_deal applies to
    the model's choice, for the same reason: a form field must not be able to
    file a quote onto another customer's (or another tenant's) deal."""
    deal_id = (deal_id or "").strip()
    if not deal_id:
        return None
    return deal_id if any(d["id"] == deal_id
                          for d in _open_deals_for_email(ctx, email)) else None


@router.get("/quotes/deals")
def quote_deals_for_customer(email: str = "",
                             ctx: TenantCtx = Depends(require_onboarded)):
    """The customer's open deals, for the quote form's "which job is this for?"
    picker. The form asks once the customer is known; the picker only appears
    when there are two or more, because one is inherited automatically and none
    is nothing to ask about."""
    _require_invoicing(ctx)
    return {"deals": [{"id": d["id"], "title": d.get("title") or "Untitled",
                       "stage": ((d.get("crm_stages") or {}) or {}).get("name") or ""}
                      for d in _open_deals_for_email(ctx, email)]}


@router.get("/quotes/new")
def quote_new_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Standalone quote, or pre-filled from a booking (?from=booking_id) — the
    booking's stored quoted_amount seeds the first line when it has one."""
    _require_invoicing(ctx)
    from app import quoting
    prefill: dict = {}
    booking_id = (request.query_params.get("from") or "").strip()
    if booking_id:
        booking = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
        if booking:
            # Job parties: the quote goes to whoever APPROVES on this job
            # (fallback = the booking's own contact, exactly as before —
            # bookings carry no name column, so the fallback name is blank).
            from app import job_parties
            approver = job_parties.resolve(
                booking, db.list_job_parties(ctx.tenant_id, booking_id,
                                             client=ctx.client))["approves"]
            prefill = {"customer_email": approver.get("email") or "",
                       "customer_name": approver.get("name") or "",
                       "booking_id": booking_id,
                       "lines": quoting.lines_from_booking(booking)}
    return templates.TemplateResponse(request, "app/quote_new.html", _ctx(
        "quotes", ctx, msg=request.query_params.get("msg"), prefill=prefill))


@router.post("/quotes/new", dependencies=[Depends(require_csrf)])
async def quote_new_create(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    form = await request.form()
    def _blocking():
        email = (form.get("customer_email") or "").strip()
        name = (form.get("customer_name") or "").strip()
        if not (email or name):
            return RedirectResponse("/app/quotes/new?msg=nocustomer", status_code=303)
        lines = _quote_lines_from_form(form)
        if not lines:
            return RedirectResponse("/app/quotes/new?msg=nolines", status_code=303)
        deposit_raw = (form.get("deposit_percent") or "").strip()
        try:
            # "" = use the tenant default; "0" = explicitly no deposit on this quote.
            deposit = float(deposit_raw) if deposit_raw else None
            if deposit is not None and deposit <= 0:
                deposit = 0
        except ValueError:
            deposit = None
        contact = db.crm_contact_by_email(ctx.tenant_id, email) if email else None
        quote = db.create_quote(
            ctx.tenant_id, email or None, name or (contact or {}).get("name"),
            lines, client=ctx.client,
            contact_id=(contact or {}).get("id"),
            # An explicit pick beats both the job walk and the single-open-deal
            # guess: the owner is answering the question the machinery can't
            # (a from-scratch quote has no booking and no email to walk from).
            deal_id=_picked_deal_id(ctx, form.get("deal_id") or "", email),
            booking_id=(form.get("quote_booking_id") or "").strip() or None,
            title=(form.get("title") or "").strip() or None,
            intro=(form.get("intro") or "").strip() or None,
            notes=(form.get("notes") or "").strip() or None,
            valid_until=(form.get("valid_until") or "").strip() or None,
            show_item_prices=form.get("show_item_prices") != "0",
            deposit_percent=deposit,
            customer_reference=(form.get("customer_reference") or "").strip() or None,
            created_by="owner")
        if quote is None:
            return RedirectResponse("/app/quotes/new?msg=failed", status_code=303)
        return RedirectResponse(f"/app/quotes/{quote['id']}", status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/quotes/{quote_id}")
def quote_detail(request: Request, quote_id: str,
                 ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    from app import invoice_config, quote_send as qsend
    quote = db.get_quote(quote_id, ctx.tenant_id, client=ctx.client)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    lines = db.list_quote_lines(quote_id, ctx.tenant_id, client=ctx.client)
    s = invoice_config.settings_for(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    deposit_inv = None
    if quote.get("deposit_invoice_id"):
        deposit_inv = db.get_invoice(quote["deposit_invoice_id"], ctx.tenant_id,
                                     client=ctx.client)
    return templates.TemplateResponse(request, "app/quote.html", _ctx(
        "quotes", ctx, q=quote, lines=lines, s=s,
        public_url=qsend.quote_url(quote_id), deposit_inv=deposit_inv))


@router.post("/quotes/{quote_id}/lines", dependencies=[Depends(require_csrf)])
async def quote_edit_lines(request: Request, quote_id: str,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Edit lines while the quote is still a working document (draft or sent —
    a sent edit bumps `version` and /q shows 'revised'). Terminal states are
    locked: the snapshot (or the decline) is the record."""
    _require_invoicing(ctx)
    quote = db.get_quote(quote_id, ctx.tenant_id, client=ctx.client)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.get("status") not in ("draft", "sent"):
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=locked", status_code=303)
    form = await request.form()
    def _blocking():
        lines = _quote_lines_from_form(form)
        db.update_quote_lines(quote_id, ctx.tenant_id, lines, client=ctx.client)
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=saved", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/quotes/{quote_id}/status", dependencies=[Depends(require_csrf)])
async def quote_status(quote_id: str, status: str = Form(...),
                       accepted_name: str = Form(""), reason: str = Form(""),
                       ctx: TenantCtx = Depends(require_onboarded)):
    """Owner-side transitions. 'accepted' is the manual record of a yes that
    arrived by phone/email — it runs the SAME guarded accept_quote (snapshot
    frozen, booking stamped) so a manual yes is as much evidence as a /q one;
    the deposit invoice is minted but the customer isn't redirected anywhere —
    the owner sends it (or shares its pay link) themselves."""
    from app import quote_public
    _require_invoicing(ctx)
    if status == "accepted":
        row = db.accept_quote(quote_id, ctx.tenant_id,
                              accepted_name.strip() or "Accepted verbally")
        if row:
            quote_public.accepted_side_effects(ctx.tenant_id, row)
            await quote_public.resolve_accept_promises(ctx.tenant_id, row)
            if quote_public.mint_deposit_invoice(ctx.tenant_id, row):
                # The customer said yes by phone: they still get the deposit
                # invoice by email like a /q acceptance does (2026-09-03).
                await quote_public.email_deposit_invoice(ctx.tenant_id, quote_id)
    elif status == "declined":
        db.decline_quote(quote_id, ctx.tenant_id, reason or "Recorded by you")
    elif status in ("cancelled",):
        db.set_quote_status(quote_id, ctx.tenant_id, status, client=ctx.client)
    return RedirectResponse(f"/app/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/send", dependencies=[Depends(require_csrf)])
async def quote_send_route(quote_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Email the quote's /q link to the customer from the tenant's mailbox, then
    stamp it sent. The sender is shared with the (Phase 2) concierge executor —
    src/app/quote_send.py."""
    require_can(ctx, "money", "send")
    from app import quote_send as qsend
    _require_invoicing(ctx)
    quote = db.get_quote(quote_id, ctx.tenant_id, client=ctx.client)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if not (quote.get("customer_email") or "").strip():
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=noemail", status_code=303)
    if quote.get("status") not in ("draft", "sent"):
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=locked", status_code=303)
    try:
        await qsend.send_quote_email(ctx.tenant_id, quote, client=ctx.client)
    except qsend.QuoteHasNoLines:
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=nolines", status_code=303)
    except Exception:
        log.warning("quote send failed for %s", quote_id, exc_info=True)
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=sendfail", status_code=303)
    return RedirectResponse(f"/app/quotes/{quote_id}?msg=sent", status_code=303)


@router.post("/quotes/{quote_id}/convert", dependencies=[Depends(require_csrf)])
def quote_convert(quote_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Accepted quote → draft invoice, lines copied from the acceptance snapshot
    (only what was agreed), any deposit already invoiced as a negative net line so the
    invoice total is exactly the outstanding balance. Never re-keyed."""
    from app import quoting
    _require_invoicing(ctx)
    quote = db.get_quote(quote_id, ctx.tenant_id, client=ctx.client)
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    if quote.get("status") != "accepted":
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=notaccepted", status_code=303)
    dep = (db.get_invoice(quote["deposit_invoice_id"], ctx.tenant_id, client=ctx.client)
           if quote.get("deposit_invoice_id") else None)
    lines = quoting.conversion_lines(quote, dep)
    if not lines:
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=nolines", status_code=303)
    inv = db.create_invoice(
        ctx.tenant_id, quote.get("customer_email"), quote.get("customer_name"),
        None, None, lines, client=ctx.client,
        contact_id=quote.get("contact_id"), deal_id=quote.get("deal_id"),
        booking_id=quote.get("booking_id"),
        notes=f"From quote {quote.get('quote_ref') or ''}".strip(),
        customer_reference=quote.get("customer_reference"),
        prices_include_vat=quote.get("prices_include_vat") is True,   # inherits the quote's basis
        created_by="owner")
    if inv is None:
        return RedirectResponse(f"/app/quotes/{quote_id}?msg=failed", status_code=303)
    db.link_quote_invoice(quote_id, ctx.tenant_id, inv["id"], client=ctx.client)
    return RedirectResponse(f"/app/invoices/{inv['id']}", status_code=303)


# ── Bring your data: the one-time Xero import (src/app/xero_import.py) ──────────
@router.get("/import")
def import_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    return templates.TemplateResponse(request, "app/import.html",
                                      _ctx("import", ctx, result=None))


@router.post("/import", dependencies=[Depends(require_csrf)])
async def import_run(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Both files optional — import whichever was chosen. One sitting, no
    mapping questions; everything that couldn't be trusted is counted and
    said, never silently dropped (the exit bundle's MANIFEST doctrine)."""
    from app import xero_import
    form = await request.form()
    result = {"contacts_new": 0, "contacts_known": 0, "contacts_skipped": 0,
              "inv_imported": 0, "inv_paid": 0, "inv_overdue": 0,
              "inv_existing": 0, "inv_skipped": 0, "errors": []}

    async def _read(field: str) -> str | None:
        up = form.get(field)
        if up is None or not getattr(up, "filename", ""):
            return None
        data = await up.read()
        if len(data) > 2_000_000:
            result["errors"].append(f"{up.filename} is over 2 MB — export a "
                                    "smaller date range and try again.")
            return None
        return data.decode("utf-8-sig", errors="replace")

    contacts_text = await _read("contacts_csv")
    if contacts_text is not None:
        rows, skipped = xero_import.parse_contacts(contacts_text)
        result["contacts_skipped"] = skipped
        for c in rows:
            try:
                if c["email"]:
                    existing = db.crm_contact_by_email(ctx.tenant_id, c["email"],
                                                       client=ctx.client)
                    if existing:
                        result["contacts_known"] += 1
                        continue
                    db.crm_get_or_create_contact(ctx.tenant_id, c["email"],
                                                 name=c["name"])
                else:
                    db.crm_get_or_create_contact_by_phone(ctx.tenant_id,
                                                          c["phone"], name=c["name"])
                result["contacts_new"] += 1
            except Exception:
                log.warning("contact import failed", exc_info=True)
                result["contacts_skipped"] += 1

    invoices_text = await _read("invoices_csv")
    if invoices_text is not None:
        rows, skipped = xero_import.parse_invoices(invoices_text)
        result["inv_skipped"] = skipped
        today = date.today().isoformat()
        for inv in rows:
            try:
                if db.get_invoice_by_ref(ctx.tenant_id, inv["invoice_ref"],
                                         client=ctx.client):
                    result["inv_existing"] += 1
                    continue
                contact = (db.crm_contact_by_email(ctx.tenant_id,
                                                   inv["customer_email"],
                                                   client=ctx.client)
                           if inv.get("customer_email") else None)
                db.import_invoice(ctx.tenant_id, inv,
                                  contact_id=(contact or {}).get("id"),
                                  client=ctx.client)
                result["inv_imported"] += 1
                if inv["status"] == "paid":
                    result["inv_paid"] += 1
                elif (inv.get("due_date") or "9999") < today:
                    result["inv_overdue"] += 1
            except Exception:
                log.warning("invoice import failed (%s)", inv.get("invoice_ref"),
                            exc_info=True)
                result["inv_skipped"] += 1
        db.log_agent_event(
            ctx.tenant_id, "import",
            f"Imported {result['inv_imported']} invoice(s) from Xero "
            f"({result['inv_overdue']} overdue) and "
            f"{result['contacts_new']} contact(s)", client=ctx.client)

    return templates.TemplateResponse(request, "app/import.html",
                                      _ctx("import", ctx, result=result))


# Their PO / works-order number — editable in any state on both documents. It
# usually arrives AFTER the quote goes out ("please add PO 4471"), so locking
# it to draft would recreate the exact re-keying leak this field closes.
@router.post("/quotes/{quote_id}/reference", dependencies=[Depends(require_csrf)])
def quote_set_reference(quote_id: str, customer_reference: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    if db.get_quote(quote_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    db.set_quote_reference(quote_id, ctx.tenant_id, customer_reference,
                           client=ctx.client)
    return RedirectResponse(f"/app/quotes/{quote_id}", status_code=303)


@router.post("/invoices/{invoice_id}/reference", dependencies=[Depends(require_csrf)])
def invoice_set_reference(invoice_id: str, customer_reference: str = Form(""),
                          ctx: TenantCtx = Depends(require_onboarded)):
    _require_invoicing(ctx)
    if db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    db.set_invoice_reference(invoice_id, ctx.tenant_id, customer_reference,
                             client=ctx.client)
    return RedirectResponse(f"/app/invoices/{invoice_id}", status_code=303)


# Old per-vertical invoice URLs → the shared surface (bookmarks/deep links keep working).
@router.get("/tms/invoices")
def _tms_invoices_redirect():
    return RedirectResponse("/app/invoices", status_code=308)


@router.get("/tms/invoices/{invoice_id}")
def _tms_invoice_redirect(invoice_id: str):
    return RedirectResponse(f"/app/invoices/{invoice_id}", status_code=308)


# ── Field Service (Trades) — team member board + invoicing (docs/modules/module-field-service.md) ──
@router.get("/fsm")
def fsm_board_view(request: Request, msg: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The team member cockpit (D6): a rolling week of each team member's load + job chips,
    emergencies flagged — the trades sibling of the transport board."""
    _require_fsm(ctx)
    data = scheduling.fsm_board(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/fsm_board.html",
                                      _ctx("fsm", ctx, msg=msg, **data))


@router.post("/fsm/jobs/{booking_id}/done", dependencies=[Depends(require_csrf)])
def fsm_mark_done(booking_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Mark a job completed — it leaves the active board and becomes invoiceable. If its
    job type recurs (service_months), this also schedules the next service-due date so the
    recurring-service reminder can chase the rebook (D5)."""
    _require_fsm(ctx)
    db.complete_job_and_schedule_service(booking_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/fsm?msg=done", status_code=303)


# FSM invoicing moved to the shared /app/invoices surface (docs/modules/module-invoicing.md);
# these keep old links working.
@router.get("/fsm/invoices")
def _fsm_invoices_redirect():
    return RedirectResponse("/app/invoices", status_code=308)


@router.get("/fsm/invoices/{invoice_id}")
def _fsm_invoice_redirect(invoice_id: str):
    return RedirectResponse(f"/app/invoices/{invoice_id}", status_code=308)


# ── Field Service settings: team members · job types · pricing (the module's setup) ──
def _fsm_skills(raw: str) -> list:
    """Parse a 'skill A, skill B; skill C' skills field into a clean list."""
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


def _fsm_int(raw: str, default: int) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return default


def _fsm_float(raw: str, default: float = 0.0) -> float:
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return default


def _fsm_money_or_none(raw: str):
    raw = (raw or "").strip()
    return _fsm_float(raw) if raw else None


def _fsm_hours_to_minutes(raw: str) -> int:
    """Working-hours-per-day field → daily_minutes, preserving half-hours (7.5h → 450,
    not 480). Floored at 60 so a team member always has a usable day."""
    return max(60, int(round(_fsm_float(raw, 8.0) * 60)))


@router.get("/fsm/engineers")
def fsm_engineers(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    return templates.TemplateResponse(request, "app/fsm_engineers.html", _ctx(
        "fsm_engineers", ctx,
        engineers=db.list_engineers(ctx.tenant_id, client=ctx.client),
        branches=db.list_branches(ctx.tenant_id, client=ctx.client),
        msg=request.query_params.get("msg")))


@router.post("/fsm/engineers", dependencies=[Depends(require_csrf)])
def fsm_add_engineer(name: str = Form(...), skills: str = Form(""), hours: str = Form("8"),
                     branch_id: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    nm = name.strip()
    if nm:
        br = branch_id.strip() or None
        if br and not any(b["id"] == br for b in db.list_branches(ctx.tenant_id, client=ctx.client)):
            br = None
        db.add_engineer(ctx.tenant_id, nm, skills=_fsm_skills(skills),
                        daily_minutes=_fsm_hours_to_minutes(hours), branch_id=br,
                        client=ctx.client)
    return RedirectResponse("/app/fsm/engineers?msg=saved", status_code=303)


@router.post("/fsm/engineers/{engineer_id}", dependencies=[Depends(require_csrf)])
def fsm_update_engineer(engineer_id: str, name: str = Form(...), skills: str = Form(""),
                        hours: str = Form("8"), active: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    db.update_engineer(engineer_id, ctx.tenant_id, {
        "name": name.strip() or "Engineer", "skills": _fsm_skills(skills),
        "daily_minutes": _fsm_hours_to_minutes(hours), "active": active == "1",
    }, client=ctx.client)
    return RedirectResponse("/app/fsm/engineers?msg=saved", status_code=303)


@router.post("/fsm/engineers/{engineer_id}/delete", dependencies=[Depends(require_csrf)])
def fsm_delete_engineer(engineer_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    db.delete_engineer(engineer_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/fsm/engineers?msg=deleted", status_code=303)


@router.get("/fsm/job-types")
def fsm_job_types(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    return templates.TemplateResponse(request, "app/fsm_job_types.html", _ctx(
        "fsm_job_types", ctx,
        job_types=db.list_job_types(ctx.tenant_id, client=ctx.client),
        msg=request.query_params.get("msg")))


@router.post("/fsm/job-types", dependencies=[Depends(require_csrf)])
def fsm_add_job_type(name: str = Form(...), minutes: str = Form("60"), skill: str = Form(""),
                     price: str = Form(""), emergency: str = Form(""), months: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    nm = name.strip()
    if nm:
        db.add_job_type(ctx.tenant_id, nm, default_minutes=max(5, _fsm_int(minutes, 60)),
                        required_skill=skill.strip() or None, default_price=_fsm_money_or_none(price),
                        emergency=emergency == "1",
                        service_months=(_fsm_int(months, 0) or None) if months.strip() else None,
                        client=ctx.client)
    return RedirectResponse("/app/fsm/job-types?msg=saved", status_code=303)


@router.post("/fsm/job-types/{job_type_id}", dependencies=[Depends(require_csrf)])
def fsm_update_job_type(job_type_id: str, name: str = Form(...), minutes: str = Form("60"),
                        skill: str = Form(""), price: str = Form(""), emergency: str = Form(""),
                        months: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    db.update_job_type(job_type_id, ctx.tenant_id, {
        "name": name.strip() or "Job", "default_minutes": max(5, _fsm_int(minutes, 60)),
        "required_skill": skill.strip() or None, "default_price": _fsm_money_or_none(price),
        "emergency": emergency == "1",
        "service_months": (_fsm_int(months, 0) or None) if months.strip() else None,
    }, client=ctx.client)
    return RedirectResponse("/app/fsm/job-types?msg=saved", status_code=303)


@router.post("/fsm/job-types/{job_type_id}/delete", dependencies=[Depends(require_csrf)])
def fsm_delete_job_type(job_type_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    db.delete_job_type(job_type_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/fsm/job-types?msg=deleted", status_code=303)


@router.get("/fsm/pricing")
def fsm_pricing(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    from app.fsm_pricing import DEFAULT_FSM_RATE, quote
    rate = db.get_fsm_rate_card(ctx.tenant_id, client=ctx.client) or dict(DEFAULT_FSM_RATE)
    return templates.TemplateResponse(request, "app/fsm_pricing.html", _ctx(
        "fsm_pricing", ctx, rate=rate, sample=quote(rate, 60),
        msg=request.query_params.get("msg")))


@router.post("/fsm/pricing", dependencies=[Depends(require_csrf)])
def fsm_update_pricing(callout_charge: str = Form("0"), hourly_rate: str = Form("0"),
                       min_charge: str = Form("0"), vat_pct: str = Form("20"),
                       ctx: TenantCtx = Depends(require_onboarded)):
    _require_fsm(ctx)
    db.upsert_fsm_rate_card(ctx.tenant_id, {
        "callout_charge": _fsm_float(callout_charge), "hourly_rate": _fsm_float(hourly_rate),
        "min_charge": _fsm_float(min_charge), "vat_pct": _fsm_float(vat_pct, 20),
    }, client=ctx.client)
    return RedirectResponse("/app/fsm/pricing?msg=saved", status_code=303)


# Live tracking — the fleet on a map today (simulated positions, clearly labelled).
@router.get("/tms/tracking")
def tms_tracking(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    data = tracking.view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/tms_tracking.html",
                                      _ctx("tracking", ctx, **data))


# Compliance — driver and fleet-resource document expiry + weekly hours (RAG).
@router.get("/tms/compliance")
def tms_compliance(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    data = compliance.view(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/tms_compliance.html",
                                      _ctx("compliance", ctx, **data))


# ── Feature requests (every tenant — the in-app roadmap signal) ──────────────
# Not TMS-gated: any tenant can ask. The operator sees them all at /admin/requests.
REQUEST_CATEGORIES = ("Feature idea", "Integration", "Pricing / quoting",
                      "Proof of delivery", "Tracking", "Invoicing", "Driver app",
                      "Reporting", "Something else")


@router.get("/requests")
def feature_requests_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    from app import support
    return templates.TemplateResponse(request, "app/requests.html", _ctx(
        "requests", ctx, categories=REQUEST_CATEGORIES,
        status_labels=support.STATUS_LABELS,
        items=db.list_feature_requests(ctx.tenant_id, client=ctx.client)))


@router.post("/requests", dependencies=[Depends(require_csrf)])
def submit_feature_request(body: str = Form(...), category: str = Form(""),
                           kind: str = Form("feature"),
                           ctx: TenantCtx = Depends(require_onboarded)):
    text = (body or "").strip()
    if not text:
        return RedirectResponse("/app/requests?msg=empty", status_code=303)
    kind = kind if kind in ("feature", "bug") else "feature"
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    db.add_feature_request(ctx.tenant_id, text, category=category.strip() or None,
                           industry=tenant.get("industry"), kind=kind,
                           user_email=tenant.get("mailbox"),              # ties status checks to this account
                           status="open" if kind == "bug" else None,      # issue rows use the ticket lifecycle
                           client=ctx.client)
    return RedirectResponse("/app/requests?msg=thanks", status_code=303)


# ── Planner (transport-planner D5/D6 — ships inside routing, D7) ─────────────
@router.post("/board/replan", dependencies=[Depends(require_csrf)])
async def board_replan(plan_date: str = Form(...),
                       ctx: TenantCtx = Depends(require_onboarded)):
    """'Replan this day' (trigger v1 — locked Q3): run the planner persona over
    the day's board. Proposes a plan (or reports the honest no-gain verdict);
    with planner_auto_apply on, a safe-class plan applies itself and says so."""
    _require_tms(ctx)
    try:
        day = date.fromisoformat(plan_date.strip()).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad date")
    try:
        out = await planner.replan_day(ctx.tenant_id, day)
    except Exception:
        log.warning("replan failed for tenant %s day %s", ctx.tenant_id, day, exc_info=True)
        out = {"plan": None, "applied": False,
               "note": "The planner hit a snag — try again in a minute.",
               "apply_error": None}
    msg = ("plan_auto" if out["applied"]
           else "plan_proposed" if out["plan"]
           else "plan_none")
    note = (out.get("note") or "")
    if out.get("apply_error"):
        note = f"{note} (auto-apply held: {out['apply_error']})".strip()
    q = urlencode({"day": day, "msg": msg, "note": note[:300]})
    return RedirectResponse(f"/app/board?{q}", status_code=303)


@router.post("/board/plans/{plan_id}/apply", dependencies=[Depends(require_csrf)])
def board_plan_apply(plan_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Approve a proposed plan: every move in ONE transaction through the
    apply_plan SQL function — any move that no longer fits fails the WHOLE plan
    loudly, with the blocking booking/resource named (D6)."""
    _require_tms(ctx)
    p = db.get_plan(plan_id, ctx.tenant_id, client=ctx.client)
    if not p:
        raise HTTPException(status_code=404, detail="Plan not found")
    _, err = db.apply_plan(plan_id, ctx.tenant_id)
    q = ({"day": str(p["plan_date"]), "msg": "plan_failed", "note": (err or "")[:300]}
         if err else {"day": str(p["plan_date"]), "msg": "plan_applied"})
    return RedirectResponse(f"/app/board?{urlencode(q)}", status_code=303)


@router.post("/board/plans/{plan_id}/discard", dependencies=[Depends(require_csrf)])
def board_plan_discard(plan_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    p = db.get_plan(plan_id, ctx.tenant_id, client=ctx.client)
    if not p:
        raise HTTPException(status_code=404, detail="Plan not found")
    db.discard_plan(plan_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse(
        f"/app/board?{urlencode({'day': str(p['plan_date']), 'msg': 'plan_discarded'})}",
        status_code=303)


@router.get("/diary/settings")
def diary_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Standalone diary settings — what the Diary's and Board's 'Diary settings'
    links open. One form shapes BOTH diaries (working days, notice, horizon,
    blackouts apply to each). The windows-capacity fields hide only for a
    fleet-ONLY tenant (the fleet resource pool is their whole capacity model); a dual
    tenant's windows fields shape the office diary, so they show."""
    # Request collection also needs booking mode on accounts without a diary.
    mode_only = not ({"scheduling", "tms"} & _diary_modules(ctx))
    fleet_mode = _fleet_only(ctx)
    mode_snapshot = setup_forms.load(ctx, "booking_details") if not fleet_mode else None
    row = mode_snapshot["config"] if mode_snapshot else (db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {})
    cfg = scheduling._merged(row)
    return templates.TemplateResponse(request, "app/diary_settings.html", _ctx(
        "inbox" if mode_only else ("diary" if not fleet_mode else "board"), ctx,
        scheduling_cfg=cfg, fleet_mode=fleet_mode, mode_only=mode_only,
        calendar_connected=db.calendar_connected(ctx.tenant_id, client=ctx.client) if not mode_only else False,
        booking_mode_snapshot=mode_snapshot,
        booking_mode_saved=request.query_params.get("mode") == "saved",
        sched_saved=request.query_params.get("sched") == "saved", **_depth_ctx(cfg),
        crm_stages=_crm_stage_names(ctx),
        setup_prefill=_setup_prefill(ctx, "diary_settings", fresh=not row),
    ))


@router.post("/diary/settings/booking-mode", dependencies=[Depends(require_csrf)])
def save_diary_booking_mode(booking_mode: str = Form(...),
                           settings_snapshot: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Save booking mode with departure capture and booking-history protection."""
    if _fleet_only(ctx):
        raise HTTPException(404, "Booking mode is unavailable for this diary.")
    if booking_mode not in {"appointment", "stay"}:
        raise HTTPException(422, "Choose appointments or arrival-to-departure bookings.")
    from app import setup_card_mode, setup_cards
    expected = setup_forms.parse(settings_snapshot, "booking_details")
    try:
        patch, _ = setup_card_mode.prepare(
            {"id": "form", "fields": {"key": "stay_mode", "value": booking_mode == "stay"}},
            {"config": expected["config"], "has_bookings": False})
    except setup_cards.Invalid as exc:
        raise HTTPException(400, str(exc)) from None
    if booking_mode == "appointment" and any(
        intake._norm(scope) == intake._norm(intake.STAYS_SCOPE)
        for row in patch["intake_fields"] for scope in row.get("services", [])
    ):
        raise HTTPException(400, "Remove Stays-only field scopes in Booking requirements before switching to appointments.")
    # The existing atomic transaction verifies the snapshot and booking history
    # under lock. A mode-only save does not complete Booking requirements.
    setup_forms.save(ctx, "booking_details", expected,
                     [setup_forms.operation("scheduling_config", patch, ctx.tenant_id)])
    return RedirectResponse("/app/diary/settings?mode=saved", status_code=303)


def _crm_stage_names(ctx) -> list[str]:
    """The tenant's own pipeline stage names for the Courses face's CRM link."""
    try:
        return [s.get("name") for s in db.crm_list_stages(ctx.tenant_id, client=ctx.client) if s.get("name")]
    except Exception:
        return []


def _hhmm(value: str, fallback: str) -> str:
    try:
        return time.fromisoformat(value.strip()).strftime("%H:%M")
    except (ValueError, AttributeError):
        return fallback


def _depth_ctx(cfg: dict | None) -> dict:
    """Cleaned inbound-business depth config for the Diary-settings template — the service
    list plus the six diary_config blobs, each normalised so the editors render their stored
    values (and empty when unconfigured)."""
    cfg = cfg or {}
    return {
        "service_types": services.clean_service_types(cfg.get("service_types")),
        # A stays diary's services are appointments (Kris 2026-09-13): the face
        # reads Appointments and shows each one's "at once".
        "diary_stay_mode": intake.stay_mode(cfg),
        "appointment_at_once": services.appointment_at_once(cfg.get("service_types"), cfg.get("resource_types")),
        # The front desk rule (Kris 2026-09-13): may an appointment overlap a drop-off?
        "front_desk": diary_config.front_desk_of(cfg),
        # Practice OS config (docs/os/practice.md §4.2/§6.2) — rendered in the same diary form.
        "practitioners": practice_config.practitioners(cfg),
        "reminder_cycle": practice_config.reminder_cycle(cfg),
        "resource_types": diary_config.clean_resource_types(cfg.get("resource_types")),
        "stay_pricing": diary_config.clean_stay_pricing(cfg.get("stay_pricing")),
        "day_hours": diary_config.clean_day_hours(cfg.get("day_hours")),
        "party_size": diary_config.clean_party_size(cfg.get("party_size")),
        "recurring": diary_config.clean_recurring(cfg.get("recurring")),
        "delivery": diary_config.clean_delivery(cfg.get("delivery")),
        # Cancellations & credits (src/app/wallet.py): how a package credit is spent.
        "credits": __import__("app.wallet", fromlist=["credits_of"]).credits_of(cfg),
        # Courses + the waiting list (src/app/courses.py).
        "courses": __import__("app.courses", fromlist=["settings_of"]).settings_of(cfg),
        # Make-ups (src/app/makeups.py).
        "makeups": __import__("app.makeups", fromlist=["settings_of"]).settings_of(cfg),
        # The business's own words.
        "nouns": diary_config.nouns_of(cfg),
    }


@router.post("/settings/scheduling", dependencies=[Depends(require_csrf)])
async def save_scheduling_settings(
    request: Request,
    capacity_per_window: str | None = Form(None), windows: str = Form("day"),
    working_days: list[str] | None = Form(None), min_notice_hours: int = Form(24),
    horizon_days: int = Form(28), blackout_dates: str = Form(""),
    open_time: str | None = Form(None), close_time: str | None = Form(None),
    timezone: str | None = Form(None), booking_revision: int = Form(0),
    capacity_state: str | None = Form(None), working_days_state: str | None = Form(None),
    opening_hours_state: str | None = Form(None), timezone_state: str | None = Form(None),
    confirm_booking_setting: list[str] = Form([]),
    slot_minutes: int = Form(60), buffer_minutes: int = Form(0),
    offer_ttl_hours: int = Form(24), offer_nudge_enabled: str = Form(""),
    offer_last_call: str = Form(""), offer_last_call_present: str = Form(""),
    travel_mode: str = Form(""),
    service_name: list[str] = Form([]), service_minutes: list[str] = Form([]),
    service_price: list[str] = Form([]), service_resource: list[str] = Form([]),
    service_buffer: list[str] = Form([]), service_recall_months: list[str] = Form([]),
    service_timing: list[str] = Form([]),
    service_at_once: list[str] = Form([]),
    # A service's own booking rules (docs/plan-diary-multiview.md): hours, days, max a day.
    service_from: list[str] = Form([]), service_until: list[str] = Form([]),
    service_days: list[str] = Form([]), service_max: list[str] = Form([]),
    service_starts: list[str] = Form([]),
    service_same_day: list[str] = Form([]), service_notice: list[str] = Form([]),
    service_ooh_until: list[str] = Form([]), service_ooh_days: list[str] = Form([]),
    service_ooh_bh: list[str] = Form([]), service_ooh_price: list[str] = Form([]),
    service_ooh_after: list[str] = Form([]), service_ooh_offer: list[str] = Form([]),
    service_ooh_basis: list[str] = Form([]), service_ooh_terms: list[str] = Form([]),
    service_ooh_reviewed: list[str] = Form([]),
    service_after_job: list[str] = Form([]), job_done_when: str = Form(""),
    bank_holiday_nation: str = Form(""), bank_holidays_open: str = Form(""),
    front_desk_present: str = Form(""), front_desk_enabled: str = Form(""), front_desk_minutes: str = Form("15"),
    practitioner_name: list[str] = Form([]), practitioner_resource: list[str] = Form([]),
    practitioner_capacity: list[str] = Form([]),
    reminder_lead_hours: list[str] = Form([]), reminder_ask_confirm: str = Form(""),
    reminder_cycle_present: str = Form(""),
    resource_name: list[str] = Form([]), resource_count: list[str] = Form([]),
    resource_rate: list[str] = Form([]), resource_rate_unit: list[str] = Form([]),
    resource_skills: list[str] = Form([]),
    stay_rate: str = Form(""), stay_rate_unit: str = Form("night"),
    stay_deposit: str = Form(""), stay_min_nights: str = Form(""),
    checkin_after: str = Form(""), checkout_before: str = Form(""),
    dayhours_mon_open: str = Form(""), dayhours_mon_close: str = Form(""),
    dayhours_tue_open: str = Form(""), dayhours_tue_close: str = Form(""),
    dayhours_wed_open: str = Form(""), dayhours_wed_close: str = Form(""),
    dayhours_thu_open: str = Form(""), dayhours_thu_close: str = Form(""),
    dayhours_fri_open: str = Form(""), dayhours_fri_close: str = Form(""),
    dayhours_sat_open: str = Form(""), dayhours_sat_close: str = Form(""),
    dayhours_sun_open: str = Form(""), dayhours_sun_close: str = Form(""),
    party_enabled: str = Form(""), party_max_people: str = Form(""),
    recurring_enabled: str = Form(""), recurring_max: str = Form("12"),
    recurring_freq: list[str] = Form([]),
    delivery_offered: str = Form(""), delivery_fee: str = Form(""),
    delivery_radius: str = Form(""),
    credits_spend: str = Form(""), late_cancel_hours: str = Form(""),
    no_show_spends: str = Form(""), excused_refunds: str = Form(""), hours_unit: str = Form(""),
    late_move_charged: str = Form(""),
    courses_on: str = Form(""), waitlist_on: str = Form(""), waitlist_notify: str = Form(""),
    offer_hours: str = Form(""), reenrol_days: str = Form(""), reenrol_notify: str = Form(""),
    makeups_on: str = Form(""), makeup_per_term: str = Form(""), makeup_notice_hours: str = Form(""),
    makeup_within_days: str = Form(""), makeup_no_show_ok: str = Form(""), makeup_free: str = Form(""),
    noun_booking: str = Form(""), noun_participant: str = Form(""),
    waitlist_stage: str = Form(""), booked_stage: str = Form(""),
    # When it's full (docs/plan-when-its-full.md): its own card on the same form.
    full_card_present: str = Form(""), other_dates: str = Form(""),
    hold_while_gathering: str = Form(""),
    return_to: str = Form("/app/settings"),
    ctx: TenantCtx = Depends(require_onboarded),
):
    """Apply explicit booking choices; omitted fields and review status are preserved."""
    from app import booking_policy
    if offer_last_call_present == "1" and offer_last_call.strip() and not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", offer_last_call.strip()):
        raise HTTPException(status_code=422, detail="Enter a time in HH:MM, or leave blank for N/A.")
    _office_diary(ctx)
    fleet_mode = _fleet_only(ctx)
    posted = await request.form()
    def _blocking():
        existing = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        # FastAPI maps empty optional form strings to None; raw key presence preserves clear vs omit.
        submitted = {k: posted.get(k) for keys in booking_policy.FIELDS.values() for k in keys
                     if k in posted and k != "working_days"}
        if "working_days" in posted or working_days_state:
            submitted["working_days"] = ",".join(posted.getlist("working_days"))
        states = {k: v for k, v in {"capacity": capacity_state, "working_days": working_days_state,
                                  "opening_hours": opening_hours_state, "timezone": timezone_state}.items() if v}
        try:
            booking_changes = booking_policy.apply_settings(existing, submitted, states,
                                                            confirm_booking_setting, ctx.user_id, booking_revision)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        # The courses block is ONE stored dict edited by two cards (Recurring and
        # When it's full): start from what is stored, so a key neither card posted
        # is never reset to its default by saving the page.
        from app import courses as _courses_mod
        _stored_courses = (_courses_mod.settings_of(db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {})
                           if waitlist_notify else {})
        _full = ({"other_dates": other_dates or "hold", "hold_while_gathering": hold_while_gathering == "1"}
                 if full_card_present == "1" else {})
        payload = {
            "windows": windows if windows in ("day", "ampm", "slots") else "day",
            # Cancellations & credits (src/app/wallet.py): how a credit is spent — the
            # business's call, every knob a setting (Kris 2026-09-05).
            "credits": __import__("app.wallet", fromlist=["clean_credits"]).clean_credits({
                "spend": credits_spend or "auto", "late_hours": late_cancel_hours or 24,
                "no_show_spends": no_show_spends != "0", "excused_refunds": excused_refunds != "0",
                "late_move_charged": late_move_charged != "0",
                "hours": hours_unit or "minutes"}) if credits_spend else None,
            # Courses + the waiting list (src/app/courses.py): every knob a setting.
            "courses": _courses_mod.clean({**_stored_courses, **_full,
                "courses": courses_on == "1", "waitlist": waitlist_on == "1",
                "waitlist_notify": waitlist_notify, "offer_hours": offer_hours or 48,
                "reenrol_days": reenrol_days if reenrol_days != "" else 21,
                "reenrol_notify": reenrol_notify,
                "waitlist_stage": waitlist_stage, "booked_stage": booked_stage}) if waitlist_notify else None,
            # The business's own words (Kris 2026-09-05): a booking is a "session", the
            # person who attends a "participant" — read by every rule the programme added.
            "nouns": diary_config.clean_nouns({"booking": noun_booking, "participant": noun_participant})
            if (noun_booking or noun_participant or waitlist_notify) else None,
            # Make-ups (src/app/makeups.py): the rule, every knob a setting.
            "makeups": __import__("app.makeups", fromlist=["clean"]).clean({
                "on": makeups_on == "1", "per_term": makeup_per_term if makeup_per_term != "" else 1,
                "notice_hours": makeup_notice_hours or 24, "within_days": makeup_within_days if makeup_within_days != "" else 42,
                "no_show_ok": makeup_no_show_ok == "1", "free": makeup_free != "0"}) if makeup_free else None,
            "min_notice_hours": max(0, min(336, min_notice_hours)),
            "horizon_days": max(1, min(365, horizon_days)),
            "blackout_dates": scheduling.parse_blackouts(blackout_dates),
            # Bank holidays (D1): the nation's gov.uk list, minus the days they work.
            "bank_holiday_nation": __import__("app.bank_holidays", fromlist=["clean_nation"]).clean_nation(bank_holiday_nation),
            "bank_holidays_open": scheduling.parse_blackouts(bank_holidays_open),
            "slot_minutes": max(10, min(480, slot_minutes)),
        }
        if payload.get("credits") is None:
            payload.pop("credits", None)           # the face wasn't on the form: keep what's stored
        if payload.get("courses") is None:
            payload.pop("courses", None)
        if payload.get("makeups") is None:
            payload.pop("makeups", None)
        if payload.get("nouns") is None:
            payload.pop("nouns", None)
        if fleet_mode:
            existing = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
            for k in ("windows", "slot_minutes"):
                if existing.get(k) is not None:
                    payload[k] = existing[k]
        # Assemble every part before writing: one failed column or policy leaves
        # the entire previous configuration intact.
        payload["buffer_minutes"] = max(0, min(480, buffer_minutes))
        if not fleet_mode:
            payload.update({"offer_ttl_hours": max(1, min(168, offer_ttl_hours)),
                            "offer_nudge_enabled": offer_nudge_enabled == "1"})
        if not fleet_mode and offer_last_call_present == "1":
            payload.update({"offer_last_call": offer_last_call.strip() or None,
                            "offer_last_call_review": None})
        if "travel" in _diary_modules(ctx):
            payload["travel_mode"] = travel_mode == "1"
        if not fleet_mode:
            day_hours = {
                "mon": {"open": dayhours_mon_open, "close": dayhours_mon_close},
                "tue": {"open": dayhours_tue_open, "close": dayhours_tue_close},
                "wed": {"open": dayhours_wed_open, "close": dayhours_wed_close},
                "thu": {"open": dayhours_thu_open, "close": dayhours_thu_close},
                "fri": {"open": dayhours_fri_open, "close": dayhours_fri_close},
                "sat": {"open": dayhours_sat_open, "close": dayhours_sat_close},
                "sun": {"open": dayhours_sun_open, "close": dayhours_sun_close},
                "checkin_after": checkin_after, "checkout_before": checkout_before,
            }
            for key, hours in day_hours.items():
                if isinstance(hours, dict) and (hours.get("open") or hours.get("close")):
                    try:
                        booking_policy.validate("opening_hours", (hours.get("open"), hours.get("close")))
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=f"{key}: {exc}") from None
            resources = [
                {"name": resource_name[i] if i < len(resource_name) else "",
                 "count": resource_count[i] if i < len(resource_count) else 1,
                 "rate": resource_rate[i] if i < len(resource_rate) else None,
                 "rate_unit": resource_rate_unit[i] if i < len(resource_rate_unit) else "booking",
                 "skills": resource_skills[i] if i < len(resource_skills) else ""}
                for i in range(len(resource_name))
            ]
            depth = {
                "service_types": services.parse_form(service_name, service_minutes, service_price,
                                                     service_resource, service_buffer,
                                                     service_recall_months, service_timing,
                                                     service_from, service_until, service_days,
                                                     service_max, service_starts,
                                                     service_same_day, service_notice,
                                                     service_ooh_until, service_ooh_days, service_ooh_bh,
                                                     service_ooh_price, service_ooh_after, service_ooh_offer,
                                                     service_after_job, service_ooh_basis, service_ooh_terms),
                "resource_types": diary_config.clean_resource_types(resources),
                "stay_pricing": diary_config.clean_stay_pricing(
                    {"rate": stay_rate, "rate_unit": stay_rate_unit,
                     "deposit": stay_deposit, "min_nights": stay_min_nights}),
                "day_hours": diary_config.clean_day_hours(day_hours),
                "party_size": diary_config.clean_party_size(
                    {"enabled": party_enabled == "1", "max_people": party_max_people}),
                "recurring": diary_config.clean_recurring(
                    {"enabled": recurring_enabled == "1", "max_occurrences": recurring_max,
                     "frequencies": recurring_freq}),
                "delivery": diary_config.clean_delivery(
                    {"offered": delivery_offered == "1", "fee": delivery_fee,
                     "radius_miles": delivery_radius}),
            }
            # After the job (docs/plan-after-job-tasks.md): a form that didn't render
            # the tasks field posts none — keep each service's saved tasks rather
            # than wiping them; and the business-wide "when is a job done" only when
            # it was on the form.
            from app import after_job as _after_job
            if not service_after_job:
                try:
                    _prev = {services._norm(x["name"]): x.get("after_job")
                             for x in services.clean_service_types(
                                 (db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {})
                                 .get("service_types")) if x.get("after_job")}
                    for _svc in depth["service_types"]:
                        if _prev.get(services._norm(_svc["name"])):
                            _svc["after_job"] = _prev[services._norm(_svc["name"])]
                except Exception:
                    log.warning("after-job carry-over failed (tenant=%s)", ctx.tenant_id, exc_info=True)
                    raise HTTPException(status_code=503, detail="Your existing settings could not be read. Nothing was saved.") from None
            if job_done_when:
                depth["job_done_when"] = _after_job.clean_done_when(job_done_when)
            # A STAYS diary's services are APPOINTMENTS (Kris 2026-09-13): each bound to a
            # pool of its own name whose count is the form's "at once".
            try:
                _cur = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
                previous = {services._norm(s["name"]): s.get("out_of_hours") or {}
                            for s in services.clean_service_types(_cur.get("service_types"))}
                reviewed = {services._norm(n) for n in service_ooh_reviewed}
                for svc in depth["service_types"]:
                    name = services._norm(svc["name"])
                    review = previous.get(name, {}).get("carried_over_review")
                    if review and name not in reviewed and svc.get("out_of_hours"):
                        svc["out_of_hours"]["carried_over_review"] = review
                if intake.stay_mode(_cur):
                    depth["service_types"], depth["resource_types"] = services.bind_appointment_pools(
                        depth["service_types"], depth["resource_types"], service_at_once)
            except Exception:
                log.warning("appointment pool binding failed (tenant=%s)", ctx.tenant_id, exc_info=True)
                raise HTTPException(status_code=503, detail="Your existing settings could not be read. Nothing was saved.") from None
            # The front desk rule rides the Appointments face of a STAYS diary only; a
            # form that never rendered it posts nothing and must not clear it.
            if front_desk_present == "1":
                depth["front_desk"] = diary_config.clean_front_desk(
                    {"enabled": front_desk_enabled == "1", "minutes": front_desk_minutes})
            # Practice OS (docs/os/practice.md §4.2, §6.2): providers (resource pools) +
            # the reminder cadence live in the SAME diary settings form. A form that doesn't
            # render those inputs posts nothing — indistinguishable from "cleared" — so each
            # key is written only when the form actually posted it; a plain diary save must
            # never wipe a practice's providers or reminder cycle.
            if practitioner_name:
                depth["practitioners"] = practice_config.parse_practitioner_form(
                    practitioner_name, practitioner_resource, practitioner_capacity)
            leads = practice_config.clean_lead_hours(reminder_lead_hours)
            if reminder_cycle_present == "1" and not leads:
                raise HTTPException(status_code=422, detail="Keep at least one reminder time.")
            if leads:
                depth["reminder_cycle"] = {"leads_hours": leads,
                                           "ask_confirm": reminder_ask_confirm == "1"}
            payload.update(depth)
        if not any(k.startswith("dayhours_") or k in ("checkin_after", "checkout_before") for k in posted):
            payload.pop("day_hours", None)
        elif payload.get("day_hours") != diary_config.clean_day_hours(existing.get("day_hours")):
            # Per-day opening hours belong to the same reviewed setting and revision.
            booking_changes = booking_policy.apply_settings(
                existing, submitted, states, [*confirm_booking_setting, "opening_hours"],
                ctx.user_id, booking_revision)
        core_names = {k for keys in booking_policy.FIELDS.values() for k in keys}
        core_names |= {"capacity_state", "working_days_state", "opening_hours_state", "timezone_state",
                       "confirm_booking_setting", "booking_revision", "csrf_token", "return_to"}
        if set(posted) <= core_names:
            payload = {}
        payload.update(booking_changes)
        try:
            if payload:
                # A row-only page save must also compete with card acceptance. The
                # early revision read alone cannot protect the later database write.
                payload["booking_revision"] = booking_revision + 1
                db.apply_booking_settings(ctx.tenant_id, payload, booking_revision, client=ctx.client)
        except Exception as exc:
            if "BLAM8_SETTINGS_STALE" in str(exc):
                raise HTTPException(status_code=409, detail="These settings changed. Reload before applying.") from None
            log.warning("diary settings save failed (tenant=%s)", ctx.tenant_id, exc_info=True)
            raise HTTPException(status_code=503, detail="Your diary settings were not saved. Please try again.") from None
        _held_used(ctx, "diary_settings")
        return RedirectResponse(f"{_safe_next(return_to)}?sched=saved#scheduling", status_code=303)
    return await run_in_threadpool(_blocking)


# ── Scheduling intake (the "block the diary until set up" onboarding) ─────────
def _require_scheduling(ctx: TenantCtx) -> None:
    if "scheduling" not in _diary_modules(ctx):
        raise HTTPException(status_code=404, detail="Scheduling is not active for this account")


@router.get("/diary/intake")
def scheduling_intake(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Scheduling onboarding: the business defines what every booking needs — required +
    optional fields — so the agent collects them and NEVER schedules without the required
    info (src/app/intake.py). Starts with generic suggestions on first run; editable forever
    after. On scheduling plans this is the gate the office diary is blocked behind; on
    email-only it lets the agent GATHER every booking detail (but never place the booking)
    so the human confirms from a complete inbox request."""
    snapshot = setup_forms.load(ctx, "booking_details")
    cfg = snapshot["config"]
    configured = bool(cfg.get("intake_configured_at"))
    fields = intake.clean_fields(cfg.get("intake_fields") or [])
    if not fields and not configured:
        fields = intake.suggest_for()
    # Diary settings owns booking mode and its reserved departure field.
    # Keep that field outside the customer checklist editor.
    sched_on = "scheduling" in _diary_modules(ctx)
    # A services diary can scope a detail to some services (Kris 2026-09-04) —
    # the editor offers the tenant's own service names as tick-boxes.
    service_names = services.names(cfg.get("service_types")) if not intake.is_stay(fields) else []
    if intake.is_stay(fields) and services.configured(cfg.get("service_types")):
        # A stays diary with appointments: a detail can be for the stays, for an
        # appointment, or both (nothing ticked) — 2026-09-23, the Ellen test.
        service_names = [intake.STAYS_SCOPE] + services.names(cfg.get("service_types"))
    # The Dependants line is for the OWNER, not the agent: once the kind is
    # known it uses that kind alone, never a list of unrelated kinds
    # (Kris 2026-09-13, run #10 (yy): the stays rig should name its own
    # dependant kind, not suggest unrelated assets). A saved kind first, else one held
    # for the Dependants step by the assistant.
    dep_kind = ""
    try:
        from app import requirement_groups as _rg
        groups = _rg.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
        dep_kind = str((groups or [{}])[0].get("label") or "").strip().lower()
        if not dep_kind:
            from app import setup_held
            held = setup_held.for_page(setup_held.load(ctx.tenant_id, client=ctx.client), "dependants")
            first = next((str(h.get("value") or "") for h in held if h.get("name") == "group"), "")
            dep_kind = first.split("|")[0].strip().lower()
    except Exception:
        dep_kind = ""
    return templates.TemplateResponse(request, "app/diary_intake.html", _ctx(
        "diary" if sched_on else "inbox", ctx, fields=intake.editable_fields(fields), settings_snapshot=snapshot,
        stay=intake.stay_mode(cfg), configured=configured, sched_on=sched_on,
        booking_ready=__import__("app.booking_policy", fromlist=["compile_policy"]).compile_policy(cfg)["ready"],
        service_names=service_names, dep_kind=dep_kind,
        saved=request.query_params.get("intake") == "saved",
        # A remembered row that could not be promoted to a customer field
        # (app/intake_memory.sync_definitions): the reason, or nothing.
        memerr=(request.query_params.get("memerr") or "")[:400],
        remember_face=intake.REMEMBER_FACE,
        setup_prefill=_setup_prefill(ctx, "booking_details", fresh=not configured)))


@router.post("/diary/intake", dependencies=[Depends(require_csrf)])
def save_scheduling_intake(label: list[str] = Form([]), required: list[str] = Form([]),
                           hint: list[str] = Form([]), doc: list[str] = Form([]),
                           services: list[str] = Form([]),
                           remember: list[str] = Form([]), crm_key: list[str] = Form([]),
                           settings_snapshot: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Persist the intake schema and stamp it configured — which unblocks the office
    diary. Fields are reassembled from the form's positionally-aligned arrays and cleaned
    (blanks/dupes dropped) by app/intake.parse_form. The saved booking mode and its reserved departure field are preserved. Available on
    email-only too (gather-don't-book) — after save, scheduling plans land on the diary,
    email-only lands back on this page.

    A remembered row (app/intake_memory, 2026-09-21) is promoted to a customer field
    here, on save — one store, so the contact page and the drafter read it where they
    already read customer fields. A row that cannot be promoted (the ten-field cap)
    drops back to ask-every-time and the page says so, rather than the owner believing
    a fact is kept when it is not."""
    expected = setup_forms.parse(settings_snapshot, "booking_details")
    fields = intake.parse_form(label, required, hint, doc, services, remember, crm_key)
    stay = intake.stay_mode(expected["config"])
    fields = intake.apply_stay(fields, stay)
    operations, config, problems = setup_forms.intake_plan(expected, fields, stay)
    config.pop("booking_mode", None)
    operations.append(setup_forms.operation("scheduling_config", config, ctx.tenant_id))
    setup_forms.save(ctx, "booking_details", expected, operations)
    dest = "/app/diary" if "scheduling" in _diary_modules(ctx) else "/app/diary/intake"
    _held_used(ctx, "booking_details")
    if problems:
        return RedirectResponse(f"/app/diary/intake?intake=saved&memerr={quote(' · '.join(problems))}",
                                status_code=303)
    return RedirectResponse(f"{dest}?intake=saved", status_code=303)


# ── Dependants (docs/modules/module-requirement-groups.md) ─────────────────────────────
@router.get("/diary/dependants")
def diary_dependants(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """What a booking is ABOUT — the tenant's own kinds (Subject, Asset)
    and what to know about each one. Zero-config: an empty page changes
    nothing anywhere."""
    from app import ladders, requirement_groups as rgroups
    snapshot = setup_forms.load(ctx, "dependants")
    groups = rgroups.clean_groups(sorted(snapshot["groups"], key=lambda row: row.get("display_order") or 0))
    contact_defs = sorted([d for d in snapshot["definitions"] if d["entity"] == "contact"], key=lambda row: row.get("display_order") or 0)
    return templates.TemplateResponse(request, "app/diary_dependants.html", _ctx(
        "diary", ctx, groups=groups, settings_snapshot=snapshot, max_groups=rgroups.MAX_GROUPS,
        max_fields=rgroups.MAX_FIELDS,
        # Progress ladders (slice 1, 2026-09-05): the kind-ladders live on the
        # groups' fields, the customer's own on the contact field definitions.
        ladders=ladders.from_groups(groups) + ladders.from_contact_defs(contact_defs),
        max_ladders=ladders.MAX_LADDERS, notify_options=ladders.NOTIFY,
        saved=request.query_params.get("dependants") == "saved",
        setup_prefill=_setup_prefill(ctx, "dependants", fresh=not groups)))


@router.post("/diary/dependants", dependencies=[Depends(require_csrf)])
def save_diary_dependants(g_label: list[str] = Form([]), g_plural: list[str] = Form([]),
                          g_cardinality: list[str] = Form([]), g_id: list[str] = Form([]),
                          f_group: list[str] = Form([]), f_label: list[str] = Form([]),
                          f_type: list[str] = Form([]), f_required: list[str] = Form([]),
                          f_durability: list[str] = Form([]), f_gate: list[str] = Form([]),
                          f_hint: list[str] = Form([]), f_recall: list[str] = Form([]),
                          f_proof: list[str] = Form([]), f_setby: list[str] = Form([]),
                          f_options: list[str] = Form([]),
                          l_who: list[str] = Form([]), l_label: list[str] = Form([]),
                          l_levels: list[str] = Form([]), l_notify: list[str] = Form([]),
                          l_gates: list[str] = Form([]),
                          settings_snapshot: str = Form(""),
                          ctx: TenantCtx = Depends(require_onboarded)):
    from app import ladders, requirement_groups as rgroups
    expected = setup_forms.parse(settings_snapshot, "dependants")
    groups = rgroups.parse_editor_form(g_label, g_plural, g_cardinality, f_group, f_label,
                                       f_type, f_required, f_durability, f_gate, f_hint, g_id=g_id,
                                       f_recall=f_recall, f_proof=f_proof, f_setby=f_setby,
                                       f_options=f_options,
                                       existing=expected["groups"])
    # Progress ladders: the editor's own rows override any ladder block a field
    # row carried, so the two sections cannot disagree.
    lads = ladders.parse_editor(l_who, l_label, l_levels, l_notify, l_gates)
    groups = ladders.apply_to_groups(groups, lads)
    from datetime import datetime, timezone
    operations = setup_forms.groups_plan(expected, groups, lads)
    operations.append(setup_forms.operation("scheduling_config", {"dependants_configured_at": datetime.now(timezone.utc).isoformat()}, ctx.tenant_id))
    setup_forms.save(ctx, "dependants", expected, operations)
    _held_used(ctx, "dependants")
    return RedirectResponse("/app/diary/dependants?dependants=saved", status_code=303)


# ── Documents on the record (src/app/documents.py; Kris 2026-09-04) ──────────────


@router.get("/crm/documents/{doc_id}")
def crm_document_open(doc_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Open one file: a short-TTL signed URL behind the operator's login —
    the bucket is private, nothing is ever linked directly."""
    from app import documents
    d = db.get_document(doc_id, ctx.tenant_id, client=ctx.client)
    if not d:
        raise HTTPException(status_code=404, detail="No such document")
    url = documents.signed_url(d.get("url_or_path"))
    if not url:
        raise HTTPException(status_code=503, detail="Document storage is unavailable right now")
    return RedirectResponse(url, status_code=303)


@router.post("/crm/contacts/{contact_id}/documents", dependencies=[Depends(require_csrf)])
async def crm_contact_document_upload(request: Request, contact_id: str,
                                      ctx: TenantCtx = Depends(require_onboarded)):
    """Keep a file the operator has in hand — the certificate they were sent
    on WhatsApp, the form filled in at the yard — on the customer's record,
    labelled and pinned like one that came by email."""
    from app import documents
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="No such contact")
    form = await request.form()
    back = f"/app/crm/contacts/{contact_id}"
    upload = form.get("file")
    if not isinstance(upload, FormUploadFile) or not upload.filename:
        return RedirectResponse(back + "?err=nofile", status_code=303)
    data = await upload.read()
    def _blocking():
        if not data:
            return RedirectResponse(back + "?err=nofile", status_code=303)
        if len(data) > documents.MAX_BYTES:
            return RedirectResponse(back + "?err=toobig", status_code=303)
        row = documents.store(
            ctx.tenant_id, filename=upload.filename, data=data, content_type=upload.content_type,
            source="upload", contact_email=contact.get("email"), contact_id=contact_id,
            instance_id=(str(form.get("instance_id") or "").strip() or None),
            booking_id=(str(form.get("booking_id") or "").strip() or None),
            label=str(form.get("label") or ""))
        if not row:
            return RedirectResponse(back + "?err=storage", status_code=303)
        db.log_agent_event(ctx.tenant_id, "document",
                           f"Kept {row.get('filename')} on {contact.get('name') or contact.get('email')}'s record (uploaded by hand)",
                           contact=contact.get("email"))
        return RedirectResponse(back + "?msg=document", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/documents/{doc_id}/update", dependencies=[Depends(require_csrf)])
async def crm_document_update(request: Request, doc_id: str,
                              ctx: TenantCtx = Depends(require_onboarded)):
    """Label a file as the checklist document it satisfies, pin it to a
    dependant or a booking."""
    form = await request.form()
    def _blocking():
        d = db.get_document(doc_id, ctx.tenant_id, client=ctx.client)
        if not d:
            raise HTTPException(status_code=404, detail="No such document")
        instance_id = str(form.get("instance_id") or "").strip() or None
        proof_keys = list(dict.fromkeys(str(k) for k in form.getlist("requirement_keys")))
        if proof_keys:
            from app import requirement_groups as rg
            inst = db.get_requirement_instance(instance_id, ctx.tenant_id, client=ctx.client) if instance_id else None
            groups = rg.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
            group = next((g for g in groups if inst and g.get("id") == inst.get("group_id")), None)
            allowed = {f["key"] for f in (group or {}).get("fields", []) if f.get("proof")}
            if not inst or not set(proof_keys) <= allowed:
                raise HTTPException(status_code=422, detail="Choose requirements belonging to the selected record.")
            if d.get("contact_id") and inst.get("contact_id") != d["contact_id"]:
                raise HTTPException(status_code=422, detail="The document and record must belong to the same customer.")
        db.update_document(doc_id, ctx.tenant_id, {
            "label": (str(form.get("label") or "").strip()[:120] or None),
            "instance_id": instance_id,
            "booking_id": (str(form.get("booking_id") or "").strip() or None),
            "requirement_keys": proof_keys,
        }, client=ctx.client)
        return RedirectResponse(_safe_next(str(form.get("return_to") or "/app/crm")) + "?msg=document",
                                status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/documents/{doc_id}/delete", dependencies=[Depends(require_csrf)])
async def crm_document_delete(request: Request, doc_id: str,
                              ctx: TenantCtx = Depends(require_onboarded)):
    from app import documents
    form = await request.form()
    def _blocking():
        d = db.get_document(doc_id, ctx.tenant_id, client=ctx.client)
        if d:
            documents.remove_object(d.get("url_or_path"))
            db.delete_document(doc_id, ctx.tenant_id, client=ctx.client)
        return RedirectResponse(_safe_next(str(form.get("return_to") or "/app/crm")), status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/contacts/{contact_id}/dependants", dependencies=[Depends(require_csrf)])
async def crm_contact_dependant_save(request: Request, contact_id: str,
                                     ctx: TenantCtx = Depends(require_onboarded)):
    """Add or edit one instance by hand on the contact card — the operator's
    entry, which overrides whatever was read (source 'owner')."""
    from app import requirement_groups as rgroups
    form = await request.form()
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    groups = rgroups.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
    g = rgroups.find_group(groups, str(form.get("group_id") or ""))
    name = " ".join(str(form.get("name") or "").split())[:80]
    if g and g.get("id") and name:
        raw = {f["key"]: form.get("f_" + f["key"]) for f in g["fields"]}
        raw = {k: v for k, v in raw.items() if v is not None}
        # One path with the Concierge's update_dependant card (src/app/record_edits.py).
        from app import record_edits
        await record_edits.save_dependant(ctx.tenant_id, contact, g, name, raw,
                                          via="the record page", client=ctx.client)
    return RedirectResponse(f"/app/crm/contacts/{contact_id}?msg=dependant", status_code=303)


@router.post("/crm/contacts/{contact_id}/dependants/{instance_id}/archive", dependencies=[Depends(require_csrf)])
def crm_contact_dependant_archive(contact_id: str, instance_id: str,
                                  ctx: TenantCtx = Depends(require_onboarded)):
    inst = db.get_requirement_instance(instance_id, ctx.tenant_id, client=ctx.client)
    if inst and inst.get("contact_id") == contact_id:
        db.archive_requirement_instance(instance_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse(f"/app/crm/contacts/{contact_id}", status_code=303)


@router.post("/diary/{booking_id}/subjects", dependencies=[Depends(require_csrf)])
async def diary_link_subject(request: Request, booking_id: str,
                             ctx: TenantCtx = Depends(require_onboarded)):
    """Link one of the customer's dependants to this booking (the job page)."""
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    def _blocking():
        iid = str(form.get("instance_id") or "")
        inst = db.get_requirement_instance(iid, ctx.tenant_id, client=ctx.client) if iid else None
        if inst:
            db.link_booking_subjects(ctx.tenant_id, booking_id, [inst["id"]], client=ctx.client)
        return RedirectResponse(f"/app/diary/{booking_id}?msg=subject", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/diary/{booking_id}/attendance", dependencies=[Depends(require_csrf)])
async def diary_attendance(request: Request, booking_id: str,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """The register for one occurrence (src/app/wallet.py): attended / no-show /
    late cancel / excused — settles the customer's wallet per the Diary's
    Cancellations & credits rules, and overrides the automatic spend."""
    from app import wallet
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    _told = str(form.get("notified_at") or "").strip()
    out = wallet.apply_attendance(ctx.tenant_id, b, str(form.get("status") or ""),
                                  note=str(form.get("note") or ""), client=ctx.client,
                                  notified_at=(_told + ":00+00:00") if len(_told) == 16 else (_told or None))
    msg = "attendance" if not out.get("error") else "badstatus"
    return RedirectResponse(f"/app/diary/{booking_id}?msg={msg}", status_code=303)


@router.post("/diary/{booking_id}/notes", dependencies=[Depends(require_csrf)])
async def diary_add_note(request: Request, booking_id: str,
                         ctx: TenantCtx = Depends(require_onboarded)):
    """The business's own line in a booking's log (src/app/customer_log.py)."""
    from app import customer_log
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if b is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    out = customer_log.add_note(ctx.tenant_id, b, str(form.get("note") or ""), author="business",
                                client=ctx.client)
    return RedirectResponse(f"/app/diary/{booking_id}?msg={'note' if out.get('added') else 'badnote'}#notes",
                            status_code=303)


@router.post("/diary/{booking_id}/waitlist/{row_id}", dependencies=[Depends(require_csrf)])
async def diary_waitlist_action(request: Request, booking_id: str, row_id: str,
                                ctx: TenantCtx = Depends(require_onboarded)):
    """The job page's waiting list: offer this waiter the place now, or remove them."""
    from app import courses
    form = await request.form()
    action = str(form.get("action") or "")
    rows = [r for r in courses.list_rows(ctx.tenant_id, client=ctx.client) if r["id"] == row_id]
    if not rows:
        raise HTTPException(status_code=404, detail="Not on the list")
    if action == "offer":
        cfg = courses.settings_of(db.get_scheduling_config(ctx.tenant_id, client=ctx.client))
        await courses.offer_place(ctx.tenant_id, rows[0], cfg, client=ctx.client)
        msg = "offered"
    else:
        courses.leave(ctx.tenant_id, row_id, client=ctx.client)
        msg = "removed"
    return RedirectResponse(f"/app/diary/{booking_id}?msg={msg}", status_code=303)


@router.post("/crm/contacts/{contact_id}/waitlist", dependencies=[Depends(require_csrf)])
async def crm_contact_waitlist(request: Request, contact_id: str,
                               ctx: TenantCtx = Depends(require_onboarded)):
    """Add a customer (or their dependant) to a class's waiting list by hand,
    or take them off one (src/app/courses.py)."""
    from app import courses
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    form = await request.form()
    def _blocking():
        row_id = str(form.get("remove") or "").strip()
        if row_id:
            courses.leave(ctx.tenant_id, row_id, client=ctx.client)
            return RedirectResponse(f"/app/crm/contacts/{contact_id}?msg=waitlist", status_code=303)
        try:
            day = date.fromisoformat(str(form.get("date") or "").strip())
        except ValueError:
            return RedirectResponse(f"/app/crm/contacts/{contact_id}?msg=badwait", status_code=303)
        window = str(form.get("window") or "").strip()[:5]
        service = str(form.get("service") or "").strip()
        cfg = courses.settings_of(db.get_scheduling_config(ctx.tenant_id, client=ctx.client))
        key = courses.slot_key(service, day, window) if cfg["courses"] or form.get("weekly") == "1" \
            else courses.date_key(service, day, window)
        instance_id = str(form.get("instance_id") or "").strip() or None
        participant = None
        if instance_id:
            inst = next((i for i in db.list_requirement_instances(ctx.tenant_id, contact_id, client=ctx.client)
                         if i.get("id") == instance_id), None)
            participant = (inst or {}).get("name")
        courses.join(ctx.tenant_id, contact.get("email"), contact_id, key, participant=participant,
                     instance_id=instance_id, note=str(form.get("note") or ""), source="manual", client=ctx.client)
        return RedirectResponse(f"/app/crm/contacts/{contact_id}?msg=waitlist", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/contacts/{contact_id}/credits", dependencies=[Depends(require_csrf)])
async def crm_contact_credits(request: Request, contact_id: str,
                              ctx: TenantCtx = Depends(require_onboarded)):
    """Adjust a customer's wallet by hand — a goodwill session, a refund, a
    package sold over the counter (src/app/wallet.py). A package key mints that
    package; otherwise a free adjustment of ± units."""
    from app import wallet
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    form = await request.form()
    packages = wallet.packages_of(db.get_invoice_settings(ctx.tenant_id, client=ctx.client))
    pkg = wallet.find_package(packages, str(form.get("package_key") or ""))
    note = str(form.get("note") or "").strip()[:200] or None
    instance_id = str(form.get("instance_id") or "").strip() or None
    if pkg:
        row = wallet.mint(ctx.tenant_id, contact_id, contact.get("email"), pkg, instance_id=instance_id,
                          paid=(form.get("paid") == "1"), note=note, client=ctx.client)
    else:
        try:
            qty = float(str(form.get("quantity") or "0"))
        except ValueError:
            qty = 0.0
        row = wallet.mint(ctx.tenant_id, contact_id, contact.get("email"), None, quantity=qty,
                          unit=str(form.get("unit") or "session"), instance_id=instance_id, paid=True,
                          note=note or "Adjustment", client=ctx.client) if qty else None
    if row:
        db.log_agent_event(ctx.tenant_id, wallet.KIND,
                           f"{contact.get('name') or contact.get('email')}: {wallet.unit_words(row['unit'], row['quantity'])} "
                           f"{'of ' + row['package_name'] + ' ' if row.get('package_key') else ''}added by hand"
                           + (f" — {note}" if note else ""), contact=contact.get("email"))
    return RedirectResponse(f"/app/crm/contacts/{contact_id}?msg=credits", status_code=303)


@router.post("/diary/{booking_id}/subjects/{instance_id}/level", dependencies=[Depends(require_csrf)])
async def diary_move_level(request: Request, booking_id: str, instance_id: str,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Move a dependant up (or down) a ladder from the job page — the
    business's tap. Writes the confirm fact as the owner's entry and fires
    the level change (src/app/ladders.py: trail, then the message per the
    ladder's notify setting)."""
    from app import ladders, requirement_groups as rgroups
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    inst = db.get_requirement_instance(instance_id, ctx.tenant_id, client=ctx.client)
    if b is None or inst is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    key, level = str(form.get("key") or ""), " ".join(str(form.get("level") or "").split())
    groups = rgroups.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
    g = next((x for x in groups if x.get("id") == inst.get("group_id")), None)
    f = next((x for x in (g or {}).get("fields") or [] if x.get("key") == key and ladders.is_ladder(x)), None)
    if not g or not f or ladders.index_of(f, level) is None:
        return RedirectResponse(f"/app/diary/{booking_id}?msg=badlevel", status_code=303)
    old = (inst.get("data") or {}).get(key)
    data, meta, _c = rgroups.merge(inst.get("data"), inst.get("field_meta"), {key: level}, g, source="owner")
    row = db.upsert_requirement_instance(ctx.tenant_id, g["id"], inst["contact_id"], inst["name"], data, meta,
                                         client=ctx.client)
    contact = db.crm_get_contact(inst["contact_id"], ctx.tenant_id, client=ctx.client) or {}
    out = await ladders.on_level_change(ctx.tenant_id, contact, row or inst, g, f, old, level,
                                        booking=b, client=ctx.client)
    msg = "level_sent" if out.get("sent") else ("level_queued" if out.get("log_id") else "level")
    return RedirectResponse(f"/app/diary/{booking_id}?msg={msg}", status_code=303)


@router.post("/diary/{booking_id}/subjects/{instance_id}/unlink", dependencies=[Depends(require_csrf)])
def diary_unlink_subject(booking_id: str, instance_id: str,
                         ctx: TenantCtx = Depends(require_onboarded)):
    if db.get_booking(booking_id, ctx.tenant_id, client=ctx.client) is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    db.unlink_booking_subject(ctx.tenant_id, booking_id, instance_id, client=ctx.client)
    # ?undo= names what went so the page can offer it back (Kris 2026-09-21).
    return RedirectResponse(f"/app/diary/{booking_id}?msg=unlinked&undo={instance_id}", status_code=303)


# ── Fleet settings (Fleet module — branches + fleet resources CRUD, factors read-only) ─
def _clamp_spaces(raw: str) -> float | None:
    try:
        v = round(float(raw), 2)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 9999 else None


def _clamp_kg(raw: str) -> float | None:
    """Optional payload limit: blank/invalid/non-positive → None (not enforced)."""
    try:
        v = round(float(raw), 1)
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 99999 else None


def _own_branch_or_none(ctx: TenantCtx, branch_id: str | None) -> str | None:
    bid = (branch_id or "").strip()
    if bid and any(b["id"] == bid for b in db.list_branches(ctx.tenant_id, client=ctx.client)):
        return bid
    return None


@router.get("/fleet")
def fleet_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The fleet itself: branches and fleet resources (the capacity the agent books
    against), plus the load-unit factors (read-only in v1 — D2)."""
    _require_tms(ctx)
    branches = db.list_branches(ctx.tenant_id, client=ctx.client)
    vehicles = db.list_vehicles(ctx.tenant_id, client=ctx.client)
    branch_names = {b["id"]: b["name"] for b in branches}
    types = db.list_pallet_types(ctx.tenant_id, client=ctx.client) or [
        {"name": n, "space_factor": f} for n, f in db.DEFAULT_PALLET_TYPES
    ]
    total = sum(float(v["capacity_spaces"]) for v in vehicles if v.get("active", True))
    return templates.TemplateResponse(request, "app/fleet.html", _ctx(
        "fleet", ctx, branches=branches, vehicles=vehicles, branch_names=branch_names,
        pallet_types=types, total_spaces=total,
        active_count=sum(1 for v in vehicles if v.get("active", True)),
        msg=request.query_params.get("msg"),
    ))


@router.post("/fleet/branches/add", dependencies=[Depends(require_csrf)])
def fleet_add_branch(name: str = Form(...), postcode: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    clean = name.strip()[:80]
    if clean:
        db.add_branch(ctx.tenant_id, clean,
                      postcode=postcode.strip().upper()[:16] or None, client=ctx.client)
        _sync_depot_addon(ctx.tenant_id)
    return RedirectResponse("/app/fleet?msg=branchadded" if clean else "/app/fleet", status_code=303)


@router.post("/fleet/branches/{branch_id}/update", dependencies=[Depends(require_csrf)])
def fleet_update_branch(branch_id: str, name: str = Form(...), postcode: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Edit a branch in place. The postcode is the branch's route origin on the
    fleet route map — without it the route starts at the first drop instead."""
    _require_tms(ctx)
    clean = name.strip()[:80]
    if not clean:
        return RedirectResponse("/app/fleet", status_code=303)
    db.update_branch(branch_id, ctx.tenant_id, {
        "name": clean, "postcode": postcode.strip().upper()[:16] or None,
    }, client=ctx.client)
    return RedirectResponse("/app/fleet?msg=branchsaved", status_code=303)


@router.post("/fleet/branches/{branch_id}/delete", dependencies=[Depends(require_csrf)])
def fleet_delete_branch(branch_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Fleet resources in the branch stay (they become unassigned — bookable in the
    whole-fleet pool); any mailbox default pointing here clears."""
    _require_tms(ctx)
    db.delete_branch(branch_id, ctx.tenant_id, client=ctx.client)
    _sync_depot_addon(ctx.tenant_id)
    return RedirectResponse("/app/fleet?msg=branchremoved", status_code=303)


def _sync_depot_addon(tenant_id: str) -> None:
    """Reconcile the additional-branch add-on (and the mailbox allowance that
    moves with it) after a branch change. Best-effort, like the mailbox sync on
    disconnect — a Stripe hiccup must never block the fleet page."""
    for sync in (billing.sync_depot_addon, billing.sync_mailbox_addon):
        try:
            sync(tenant_id)
        except Exception:
            logging.getLogger("blautom8.dashboard").warning(
                "%s failed", sync.__name__, exc_info=True)


@router.post("/fleet/vehicles/add", dependencies=[Depends(require_csrf)])
def fleet_add_vehicle(name: str = Form(...), capacity_spaces: str = Form(...),
                      max_payload_kg: str = Form(""), branch_id: str = Form(""),
                      registration: str = Form(""),
                      ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    spaces = _clamp_spaces(capacity_spaces)
    clean = name.strip()[:120]
    if not clean or spaces is None:
        return RedirectResponse("/app/fleet?msg=badvehicle", status_code=303)
    db.add_vehicle(ctx.tenant_id, clean, spaces,
                   max_payload_kg=_clamp_kg(max_payload_kg),
                   branch_id=_own_branch_or_none(ctx, branch_id),
                   registration=registration.strip().upper()[:16] or None,
                   client=ctx.client)
    return RedirectResponse("/app/fleet?msg=vehicleadded", status_code=303)


@router.post("/fleet/vehicles/{vehicle_id}/update", dependencies=[Depends(require_csrf)])
def fleet_update_vehicle(vehicle_id: str, name: str = Form(...), capacity_spaces: str = Form(...),
                         max_payload_kg: str = Form(""), branch_id: str = Form(""),
                         active: str = Form(""), registration: str = Form(""),
                         ctx: TenantCtx = Depends(require_onboarded)):
    """Edit one fleet resource in place. Deactivating (active unticked) blocks NEW holds
    only — its existing bookings keep their dates and stay on the board."""
    _require_tms(ctx)
    spaces = _clamp_spaces(capacity_spaces)
    clean = name.strip()[:120]
    if not clean or spaces is None:
        return RedirectResponse("/app/fleet?msg=badvehicle", status_code=303)
    db.update_vehicle(vehicle_id, ctx.tenant_id, {
        "name": clean, "capacity_spaces": spaces,
        "max_payload_kg": _clamp_kg(max_payload_kg),
        "branch_id": _own_branch_or_none(ctx, branch_id),
        "active": active == "1",
        "registration": registration.strip().upper()[:16] or None,
    }, client=ctx.client)
    return RedirectResponse("/app/fleet?msg=vehiclesaved", status_code=303)


@router.post("/fleet/vehicles/{vehicle_id}/delete", dependencies=[Depends(require_csrf)])
def fleet_delete_vehicle(vehicle_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Remove a fleet resource — its future bookings keep their date and show as
    'unassigned' on the board for a human to re-place."""
    _require_tms(ctx)
    db.delete_vehicle(vehicle_id, ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/fleet?msg=vehicleremoved", status_code=303)


def _manifest_or_404(ctx: TenantCtx, vehicle_id: str, day: str) -> dict:
    _require_tms(ctx)
    try:
        data = scheduling.manifest_view(ctx.tenant_id, vehicle_id, day, client=ctx.client)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Bad date")
    if not data:
        raise HTTPException(status_code=404, detail="No such vehicle")
    return data


@router.get("/fleet/vehicles/{vehicle_id}/day/{day}")
def fleet_manifest(request: Request, vehicle_id: str, day: str,
                   ctx: TenantCtx = Depends(require_onboarded)):
    """The resource-day manifest (route-planning slice 1): everything on this van
    that day — refs, loads, postcodes — the dispatcher's printable truth, and
    the structured data the map and the spreadsheet exports build on."""
    return templates.TemplateResponse(request, "app/fleet_manifest.html",
                                      _ctx("board", ctx, m=_manifest_or_404(ctx, vehicle_id, day)))


@router.get("/fleet/vehicles/{vehicle_id}/day/{day}/manifest.csv")
def fleet_manifest_csv(vehicle_id: str, day: str,
                       ctx: TenantCtx = Depends(require_onboarded)):
    """Manifests OUT (route-planning D4, first spreadsheet deliverable): the
    resource-day as a pure CSV table — drivers, customers, the office."""
    m = _manifest_or_404(ctx, vehicle_id, day)
    return Response(
        spreadsheets.manifest_csv(m), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{spreadsheets.manifest_filename(m, "csv")}"'})


@router.get("/fleet/vehicles/{vehicle_id}/day/{day}/manifest.xlsx")
def fleet_manifest_xlsx(vehicle_id: str, day: str,
                        ctx: TenantCtx = Depends(require_onboarded)):
    m = _manifest_or_404(ctx, vehicle_id, day)
    return Response(
        spreadsheets.manifest_xlsx(m),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{spreadsheets.manifest_filename(m, "xlsx")}"'})


ROUTE_MAX_STOPS = 23   # Google Directions caps waypoints; never truncate silently


@router.get("/fleet/vehicles/{vehicle_id}/day/{day}/route")
def fleet_route(request: Request, vehicle_id: str, day: str,
                ctx: TenantCtx = Depends(require_onboarded)):
    """The resource-day on a map (route-planning D3, slice one: display, not
    optimisation): branch postcode as origin when the branch has one, then every
    collection/delivery postcode in board order. Drops with no postcode are
    counted out loud — the manifest stays the complete truth."""
    m = _manifest_or_404(ctx, vehicle_id, day)
    stops = []
    for r in m["rows"]:
        label = (r["reference"] or r["summary"] or "")[:48]
        if r["collection_postcode"]:
            stops.append({"postcode": r["collection_postcode"], "kind": "collect",
                          "label": label})
        if r["delivery_postcode"]:
            stops.append({"postcode": r["delivery_postcode"], "kind": "deliver",
                          "label": label})
    overflow = max(0, len(stops) - ROUTE_MAX_STOPS)
    return templates.TemplateResponse(request, "app/fleet_route.html", _ctx(
        "board", ctx, m=m, stops=stops[:ROUTE_MAX_STOPS], overflow=overflow,
        origin=(m.get("branch") or {}).get("postcode") or None,
        maps_key=settings.google_places_api_key,
    ))


# ── Order-book import (route-planning D4: upload → preview → commit) ─────────
IMPORT_MAX_BYTES = 2_000_000
IMPORT_MAX_ROWS = 500
_COMMIT_FIELDS = ("line", "date", "qty", "pallet_type", "weight_kg", "summary",
                  "contact", "reference", "collection_postcode",
                  "delivery_postcode", "branch_id")


@router.get("/fleet/import")
def fleet_import_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_tms(ctx)
    return templates.TemplateResponse(request, "app/fleet_import.html",
                                      _ctx("board", ctx, stage="upload", error=None))


@router.post("/fleet/import/preview", dependencies=[Depends(require_csrf)])
async def fleet_import_preview(request: Request, file: UploadFile = File(...),
                               ctx: TenantCtx = Depends(require_onboarded)):
    """Parse + verdict, commit nothing: 'here's what I understood, row by row'.
    The preview simulates the file cumulatively against today's board, so a
    file that overfills a day shows its first no-fit row HERE, not at commit."""
    _require_tms(ctx)
    data = await file.read()
    if len(data) > IMPORT_MAX_BYTES:
        return templates.TemplateResponse(request, "app/fleet_import.html",
            _ctx("board", ctx, stage="upload",
                 error="That file is over 2 MB — an order book export shouldn't be; "
                       "check it's a plain csv/xlsx."))
    rows, fatal = await asyncio.to_thread(spreadsheets.parse_order_book, file.filename or "", data)
    if fatal:
        return templates.TemplateResponse(request, "app/fleet_import.html",
                                          _ctx("board", ctx, stage="upload", error=fatal))
    rows = rows[:IMPORT_MAX_ROWS]
    preview = spreadsheets.preview_order_rows(
        rows,
        scheduling.pallet_factors(db.list_pallet_types(ctx.tenant_id, client=ctx.client)),
        db.list_branches(ctx.tenant_id, client=ctx.client),
        db.list_vehicles(ctx.tenant_id, client=ctx.client, active_only=True),
        db.list_bookings(ctx.tenant_id, date.today().isoformat(), client=ctx.client),
        now=datetime.now(scheduling.DIARY_TZ),
    )
    committable = [{k: r.get(k) for k in _COMMIT_FIELDS}
                   for r in preview if r["status"] != "error"]
    counts = {s: sum(1 for r in preview if r["status"] == s)
              for s in ("ok", "no_fit", "error")}
    return templates.TemplateResponse(request, "app/fleet_import.html",
        _ctx("board", ctx, stage="preview", rows=preview, counts=counts,
             filename=file.filename, payload=json.dumps(committable), error=None))


@router.post("/fleet/import/commit", dependencies=[Depends(require_csrf)])
async def fleet_import_commit(request: Request, payload: str = Form(...),
                              ctx: TenantCtx = Depends(require_onboarded)):
    """Book every committable row through the atomic best-fit RPC — confirmed
    and source='import', mirroring the Diary's manual add (the dispatcher's own
    order book: their diary, their call; capacity still can't be overrun).
    Every row gets a reported outcome — booked (with the van), no fit, or bad —
    never a silent skip. The payload is re-validated field by field; nothing in
    it can book anything the manual-add form couldn't."""
    _require_tms(ctx)
    try:
        items = json.loads(payload)
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        raise HTTPException(status_code=400, detail="Bad import payload")
    readiness, _ = scheduling.make_executor(ctx.tenant_id, source="import", fleet=True)
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise HTTPException(status_code=409, detail=blocked)
    items = items[:IMPORT_MAX_ROWS]
    factors = scheduling.pallet_factors(db.list_pallet_types(ctx.tenant_id, client=ctx.client))
    branch_ids = {b["id"] for b in db.list_branches(ctx.tenant_id, client=ctx.client)}
    vehicles = {v["id"]: v for v in db.list_vehicles(ctx.tenant_id, client=ctx.client)}

    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        line = it.get("line")
        try:
            day = date.fromisoformat(str(it.get("date")))
            qty = float(it.get("qty") or 0)
            ptype = str(it.get("pallet_type") or "full").strip().lower() or "full"
            spaces, _assumed = scheduling.order_spaces(factors, [{"type": ptype, "qty": qty}])
        except (TypeError, ValueError, KeyError):
            results.append({"line": line, "summary": str(it.get("summary") or ""),
                            "outcome": "bad", "date": str(it.get("date") or "")})
            continue
        weight = it.get("weight_kg")
        try:
            weight = float(weight) if weight else None
        except (TypeError, ValueError):
            weight = None
        branch_id = it.get("branch_id") if it.get("branch_id") in branch_ids else None
        summary = (str(it.get("summary") or "").strip()
                   or f"{scheduling.fmt_spaces(qty)} × {ptype}"
                   + (f" · {it.get('collection_postcode')}→{it.get('delivery_postcode')}"
                      if it.get("delivery_postcode") else ""))[:200]
        row = db.hold_fleet_booking(
            ctx.tenant_id, day.isoformat(), summary, spaces,
            weight_kg=weight if weight and weight > 0 else None,
            branch_id=branch_id,
            details={"pallets": f"{scheduling.fmt_spaces(qty)} × {ptype}",
                     "import_line": line},
            contact=(str(it.get("contact") or "").strip() or None),
            source="import", status="confirmed",
        )
        if row:
            extras = scheduling.routing_fields(it)
            if extras:
                db.set_booking_routing(row["id"], ctx.tenant_id, **extras, client=ctx.client)
            await calendar_sync.push_bookings(ctx.tenant_id, booking_id=row["id"])
            veh = vehicles.get(row.get("vehicle_id")) or {}
            results.append({"line": line, "summary": summary, "date": day.isoformat(),
                            "outcome": "booked", "ref": row.get("booking_ref"),
                            "vehicle": veh.get("name") or "—",
                            "registration": veh.get("registration")})
        else:
            results.append({"line": line, "summary": summary, "date": day.isoformat(),
                            "outcome": "no_fit"})
    def _blocking():
        booked_n = sum(1 for r in results if r["outcome"] == "booked")
        return templates.TemplateResponse(request, "app/fleet_import.html",
            _ctx("board", ctx, stage="result", results=results, booked_n=booked_n,
                 error=None))
    return await run_in_threadpool(_blocking)


# The triage buckets the inbox filters/colours by. Mirrors agent.THREAD_STATES,
# plus the synthetic 'new' (an inbound the agent hasn't drafted yet) which triages
# as needs-attention. Order = how the filter pills read left→right.
CONVO_STATES = ("needs_attention", "in_progress", "booked", "resolved")


def _conversation_display_state(c: dict) -> str:
    """The triage bucket for one inbox row. Drafted rows carry the agent's self-reported
    thread_state; a brand-new un-drafted sender needs a first reply, so it triages as
    needs_attention; a legacy row drafted before thread_state existed is inferred from the
    old gap/booking signals (else it's just an ongoing exchange)."""
    if c.get("state") == "new":
        return "needs_attention"
    ts = c.get("thread_state")
    if ts in CONVO_STATES:
        return ts
    if c.get("gap") or c.get("sched_gap"):
        return "needs_attention"
    return "in_progress"


# ── Smart inbox labels (auto-categorisation) ─────────────────────────────────
# The ONE primary label a conversation wears at a time — stored per email_log row
# (latest wins, like thread_state), stamped by the pipeline's transitions and
# derived for legacy rows. These replaced the old Attention/In-progress
# state pills as THE inbox filter (the rose row-tint keeps carrying the
# attention signal). The derivation lives in src/app/inbox_labels.py — shared with
# the concierge so both surfaces always describe the same inbox. Order = pill
# order, left→right.
CONVO_CATEGORIES = inbox_labels.CONVO_CATEGORIES
# tone/dot = the Counting House state colours; fill = the toolbar KEY chip when
# its filter is active (Kris 2026-08-31: chips carry the state colour, rows wear
# it as the card's glow — glow-<category> in conversations.html must stay in the
# same family as fill). chip stays for the thread header, pill for legacy callers.
CATEGORY_META = {
    "needs_reply":     {"label": "Needs Reply",     "glyph": "↩",
                        "chip": "bg-red-100 text-red-700",     "pill": "bg-red-600",
                        "tone": "text-rose-700",  "dot": "bg-rose-600",
                        "fill": "bg-rose-600",
                        "hint": "Waiting for a response"},
    "draft_ready":     {"label": "Draft Ready",     "glyph": "✓",
                        "chip": "bg-blue-100 text-blue-700",   "pill": "bg-blue-600",
                        "tone": "text-brand",     "dot": "bg-brand",
                        "fill": "bg-brand",
                        "hint": "A draft is waiting for your approval"},
    "quote_sent":      {"label": "Replied",      "glyph": "✓✓",
                        "chip": "bg-green-100 text-green-700", "pill": "bg-green-600",
                        "tone": "text-emerald-700", "dot": "bg-emerald-600",
                        "fill": "bg-emerald-600",
                        "hint": "Reply sent — awaiting the customer"},
    "booking_pending": {"label": "Booked", "glyph": "✓",
                        "chip": "bg-amber-100 text-amber-700", "pill": "bg-amber-600",
                        "tone": "text-amber-700", "dot": "bg-amber-500",
                        "fill": "bg-amber-500",
                        "hint": "A booking is held or confirmed"},
    "fyi":             {"label": "FYI",             "glyph": "·",
                        "chip": "bg-gray-100 text-gray-600",   "pill": "bg-gray-500",
                        "tone": "text-slate-500", "dot": "bg-slate-200",
                        "fill": "bg-slate-500",
                        "hint": "Informational — no action needed"},
    "automated":       {"label": "Ignored",         "glyph": "·",
                        "chip": "bg-slate-200 text-slate-600", "pill": "bg-slate-500",
                        "tone": "text-slate-400", "dot": "bg-slate-200",
                        "fill": "bg-slate-400",
                        "hint": "Robots and bulk mail the agent left alone — change what counts in Settings"},
}


_conversation_category = inbox_labels.derive_category


# ── Conversations (per-contact chat-style threads) ──────────────────────────
# The one HRT formatter lives with the operator aggregates (usage.py) — the
# inbox month faces and the admin console must print the same shape.
_fmt_review_time = usage.fmt_review_time


def _inbox_temperatures(ctx: TenantCtx) -> dict:
    """Customer temperature for the Inbox's mood chips. Best-effort — a CRM read
    failure never costs the Inbox."""
    try:
        return db.crm_contact_temperatures(ctx.tenant_id, client=ctx.client)
    except Exception:
        return {}


def _inbox_month_stats(ctx: TenantCtx) -> dict:
    """The month ledger behind the Inbox's collapsed history faces. Cached: it
    pages the whole email_log to aggregate months that have already closed, and
    it was re-read on every render — including every SSE-driven fragment
    refresh. An event on this tenant busts the key (src/app/events.py), so a send
    still shows up in its month immediately."""
    return navcache.get_or_fill(
        "t:" + ctx.tenant_id + ":monthstats", 120.0,
        lambda: db.email_log_month_stats(ctx.tenant_id, client=ctx.client))


async def _conversations_data(request: Request, ctx: TenantCtx) -> dict:
    """Everything the conversations list renders from — shared by the full page
    and the live fragment (GET /conversations/fragment) so both stay identical."""
    # The nav chrome (navcache, 60s) already answers "enterprise?" and "which
    # modules are on?" for this navigation — asking Supabase both questions
    # again cost two more serial round trips on every render of this page and
    # of its live fragment.
    chrome = navcache.get_or_fill("t:" + ctx.tenant_id + ":chrome", 60.0,
                                  lambda: _chrome(ctx))
    enterprise = chrome["enterprise"]
    # The page's other reads are independent of each other and of the snapshot,
    # so they go out together on the fan pool and are collected at the point
    # each is first needed. They also move OFF the event loop: this handler is
    # `async def`, so a synchronous Supabase call here doesn't just make this
    # page wait — it stalls every other request in the process, SSE included.
    f_sched = _FAN.submit(db.get_scheduling_config, ctx.tenant_id, client=ctx.client)
    f_temps = _FAN.submit(_inbox_temperatures, ctx)
    f_hist = _FAN.submit(_inbox_month_stats, ctx)
    boxes, sel, default_id = [], "all", None
    if enterprise:
        boxes = await asyncio.to_thread(_tenant_mailboxes, ctx)
        default_id = boxes[0]["id"] if boxes else None
        wanted = request.query_params.get("mailbox") or "all"
        sel = wanted if (wanted == "all" or any(m["id"] == wanted for m in boxes)) else "all"
    # Their own address, and only it (People). Their mailbox is the ONLY tab:
    # an "All" that spans somebody else's inbox is the thing this replaces.
    if _inbox_is_theirs(ctx):
        _mine = set(people.mailboxes_of(_me(ctx)))
        if not boxes:
            boxes = await asyncio.to_thread(_tenant_mailboxes, ctx)
        boxes = [m for m in boxes if m["id"] in _mine]
        default_id = _default_mailbox_id(ctx)
        sel = sel if sel in _mine else (boxes[0]["id"] if boxes else "all")
    box_names = {m["id"]: m["display"] for m in boxes}

    # Logged conversations + brand-new senders folded in from the mailbox, each
    # wearing its derived smart label — the shared snapshot (src/app/inbox_labels.py)
    # the concierge answers from too, so both surfaces describe the same inbox.
    # Enterprise 'all' scans every mailbox for new senders; a tab scans just its own.
    scan = [None]
    if enterprise and boxes:
        scan = [m["id"] for m in boxes] if sel == "all" else [sel]
    # A person reading their own mailbox scans THEIRS, never the default. The
    # filter below would drop the owner's new senders anyway; this stops us
    # fetching somebody else's inbox to throw it away.
    if _inbox_is_theirs(ctx):
        # Never [None]: that means "the default mailbox", which is the owner's.
        # A person whose mailbox has been disconnected scans nothing and reads
        # an empty inbox, which is the truth.
        scan = [m["id"] for m in boxes]
    convos = await inbox_labels.conversation_snapshot(
        ctx.tenant_id, ctx.client,
        mailbox_id=None if sel == "all" else sel, default_mailbox_id=default_id,
        scan=tuple(scan))
    # Belt as well as braces: the snapshot is filtered by the selected tab, and
    # this filters by WHOSE mail it is. A person's inbox must not depend on a
    # query parameter being right.
    convos = _their_mail_only(ctx, convos)
    # The row's green "Details" copy-out tick is gone (Kris 2026-09-21): it was
    # the manual bridge into the calendar for an email-only tenant's gather
    # thread, and every tier books now. The thread's pinned booking card keeps
    # its copy-out (src/app/templates/app/_thread.html) for the gather threads
    # that remain. sched_cfg still feeds the setup nag below.
    sched_cfg = await asyncio.wrap_future(f_sched) or {}
    # The category filter (pills) over the snapshot's labels. Counts are over the
    # full list (post-mailbox) so the pills always show the totals; the selected
    # pill filters the rows shown. 'all' (or an unknown value) shows everything
    # EXCEPT Automated — system mail is bookkeeping, not inbox; its pill appears
    # only when it has rows to show. disp_state stays computed for the row-tint.
    for c in convos:
        c["disp_state"] = _conversation_display_state(c)
    cat_counts = {k: 0 for k in CONVO_CATEGORIES}
    for c in convos:
        cat_counts[c["category"]] += 1
    cat_sel = request.query_params.get("cat") or "all"
    # "Waiting for you" (Kris 2026-09-24): every draft awaiting a yes, whatever
    # the thread is labelled — exactly what Today's "N drafts waiting" counts.
    # Filtering that link by the Draft Ready label hid Jess's booking draft
    # (labelled Booking pending) from the list the count promised.
    waiting_count = sum(1 for c in convos if c.get("state") == "pending")
    # The Draft Ready pill counts and shows the same rows as the "Draft ready"
    # word on the cards — every draft awaiting a yes, whatever the label (Kris
    # 2026-09-26: the pill said 7 while 10 rows said Draft ready).
    if "draft_ready" in cat_counts:
        cat_counts["draft_ready"] = waiting_count
    if cat_sel in ("waiting", "draft_ready"):
        convos = [c for c in convos if c.get("state") == "pending"]
    elif cat_sel in CONVO_CATEGORIES:
        convos = [c for c in convos if c["category"] == cat_sel]
    else:
        cat_sel = "all"
        convos = [c for c in convos if c["category"] != "automated"]
    # Customer temperature (sentiment addendum): warm/hot contacts get a mood chip
    # on their row. One bounded query; best-effort — a CRM read failure never
    # costs the Inbox.
    temps = await asyncio.wrap_future(f_temps)
    # Live inbox: replies the AI is writing RIGHT NOW (in-memory, src/app/events.py) —
    # a matching row gets the writing chip; a brand-new sender gets a top row.
    drafting = events.drafting_now(ctx.tenant_id)
    # Scheduling bought but its setup never finished: until the intake is configured the
    # AI can't book (pipeline gates office tools on intake_configured_at), and nothing
    # else tells the owner — surface the one missing step right where they live. Gated on
    # ACTIVE (not preview): in preview the agent never books anyway, so no nag. Reuses the
    # scheduling_config row already fetched above — no extra query.
    sched_setup_needed = (not sched_cfg.get("intake_configured_at")
                          and "scheduling" in chrome["active_mods"])
    # Month deck (Kris 2026-08-31): the list renders as one glass card per month;
    # a collapsed month shows only its stats line. Rows keep list order (newest
    # first) and a dateless row files under whichever month is open at that point
    # — same filing rule the old flat headings used.
    _MONTHS = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
    month_groups: list[dict] = []
    for c in convos:
        key = (c.get("last_at") or "")[:7]
        if key and (not month_groups or month_groups[-1]["key"] != key):
            month_groups.append({"key": key, "convos": [],
                                 "label": f"{_MONTHS[int(key[5:7]) - 1]} {key[:4]}"})
        elif not month_groups:
            month_groups.append({"key": "", "label": "Earlier", "convos": []})
        month_groups[-1]["convos"].append(c)
    for g in month_groups:
        g["needs"] = sum(1 for c in g["convos"]
                         if c["category"] == "needs_reply" or c["state"] == "new"
                         or c["disp_state"] == "needs_attention")
        g["drafts"] = sum(1 for c in g["convos"] if c["category"] == "draft_ready")
    # The current month opens by default; a quiet current month falls back to
    # the newest one that has rows, so the page never renders all-folded.
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    open_key = (now_month if any(g["key"] == now_month for g in month_groups)
                else (month_groups[0]["key"] if month_groups else ""))
    # History months wear their ledger line instead of the live chips (Kris
    # 2026-08-31): what the agent shipped that month, and HRT — the average
    # visible time a draft sat under human eyes before its send. Best-effort:
    # stats {} (e.g. migration lag) just leaves every face on the live chips.
    hist = await asyncio.wrap_future(f_hist)
    if month_groups:
        for g in month_groups:
            h = hist.get(g["key"])
            if h and g["key"] < now_month:
                g["hist"] = {"sent": h["sent"], "drafted": h["drafted"],
                             "hrt": _fmt_review_time(h["hrt"])}
    # Drafting held (Kris 2026-09-01): every row on this page is un-drafted for
    # ONE reason, so it is a tenant-level fact, not a per-row one — the inbox can
    # say it once at the top instead of leaving each card to imply it.
    try:
        held = bool((db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}).get("draft_hold"))
    except Exception:
        held = False
    return dict(convos=convos, enterprise=enterprise, temps=temps,
                draft_hold=held,
                sched_setup_needed=sched_setup_needed,
                boxes=boxes, sel=sel, box_names=box_names, default_id=default_id,
                cat_sel=cat_sel, cat_counts=cat_counts, cat_meta=CATEGORY_META,
                waiting_count=waiting_count,
                total_convos=sum(v for k, v in cat_counts.items() if k != "automated"),
                drafting=drafting, month_groups=month_groups, open_key=open_key,
                drafting_addrs={d["from_address"] for d in drafting if d.get("from_address")})


# Fixed keys only — the banner must not be a free-text reflection of the URL.
_LIST_NOTICES = {
    "nodraft": "No reply was drafted — that message is from a no-reply or automated "
               "sender, an ignored contact, or this mailbox's own address, so the "
               "agent leaves it alone (robots and bulk mail file under Ignored).",
}


@router.get("/conversations")
async def conversations(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    data = await _conversations_data(request, ctx)
    # Swipe-to-ignore confirmation: a count only, clamped — the toast's words
    # live in the template, never reflected from the URL.
    def _blocking():
        try:
            ignored_count = max(0, min(int(request.query_params.get("ignored") or 0), 99))
        except ValueError:
            ignored_count = 0
        # Owed on its own (Kris 2026-09-13: "even if the tour has been skipped on
        # the Today page") — the swipe demo is the one thing every owner should see.
        tour_page, _plural = _tour_for(ctx, {}, "inbox", force=request.query_params.get("tour") == "1")
        return templates.TemplateResponse(request, "app/conversations.html",
                                          _ctx("conversations", ctx,
                                               notice=_LIST_NOTICES.get(request.query_params.get("notice")),
                                               ignored_count=ignored_count,
                                               tour_page=tour_page,
                                               **data))
    return await run_in_threadpool(_blocking)


@router.get("/conversations/fragment")
async def conversations_fragment(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The live half of the list (pills + rows), no page chrome — live.js swaps
    its #convo-pills / #convo-list into the open page on SSE activity."""
    data = await _conversations_data(request, ctx)
    def _blocking():
        return templates.TemplateResponse(request, "app/_conversation_list.html", data)
    return await run_in_threadpool(_blocking)


@router.post("/conversations/category", dependencies=[Depends(require_csrf)])
def change_conversation_category(contact: str = Form(...), category: str = Form(...),
                                 ctx: TenantCtx = Depends(require_onboarded)):
    """Manual override from the thread header's label menu: stamp the chosen
    category on the conversation's latest row (category_source='manual'). It
    sticks until the next pipeline event — a new draft, a send, a confirmed
    hold — restamps it, because the system state genuinely changed by then."""
    if category not in CONVO_CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category")
    db.set_conversation_category(ctx.tenant_id, contact, category, client=ctx.client)
    # Manual overrides get an actor — the one transition a specific person chose.
    trace.emit(ctx.tenant_id, "label_transition", contact=contact,
               user_id=ctx.user_id,
               detail={"to_category": category, "trigger": "manual_override",
                       "via": "human"})
    # Live inbox: open tabs re-pull the list/thread so the new label shows at once.
    events.publish(ctx.tenant_id, {"type": "status_changed", "contact": contact,
                                   "status": "category:" + category})
    return RedirectResponse("/app/conversations/view?contact=" + quote(contact, safe=""),
                            status_code=303)


def _booking_pin(ctx: TenantCtx, items: list[dict]) -> dict:
    """The single pinned booking card for a thread (email-only gather flow). A thread is a
    booking request when any handled row flagged scheduling_gap (booking asked for, no diary
    module). We render ONE card — not one per message — fed by the whole thread: the latest
    request summary, the cumulative booking details the agent has gathered, what's still
    outstanding (from the tenant's booking requirements), the current triage state, and the
    latest still-pending draft (the ready reply the operator confirms or changes)."""
    if not any(it.get("scheduling_gap") for it in items):
        return {"is_request": False}
    # Latest-wins for the accumulating fields; the model carries details forward each turn.
    summary = ""
    details: list[dict] = []
    state = None
    for it in items:
        if it.get("request_summary"):
            summary = it["request_summary"]
        if it.get("booking_details"):
            details = it["booking_details"]
        if it.get("thread_state"):
            state = it["thread_state"]
    pending = next((it for it in reversed(items) if it.get("status") == "pending"), None)
    # What's still outstanding — required labels not yet in the gathered details. This is the
    # reliable "complete" signal: thread_state can't be used for it, because the pipeline
    # force-stamps needs_attention on every gather booking draft regardless of completeness.
    cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    have = {(d.get("label") or ""): (d.get("value") or "") for d in details}
    missing = intake.missing_required(cfg.get("intake_fields") or [], have)
    # The Scheduling upsell shows OCCASIONALLY, not on every card: on every 5th
    # manually-confirmed booking send (5, 10, 15…) — by then the manual step is a
    # habit worth automating, and the nudge stays a nudge instead of wallpaper.
    confirms = db.count_confirmed_booking_sends(ctx.tenant_id, client=ctx.client)
    return {
        "is_request": True,
        "summary": summary,
        "details": details,
        "missing": missing,
        "state": state,
        "gathered": bool(details) and not missing,
        "pending_id": (pending or {}).get("id"),
        "pending_draft": (pending or {}).get("draft_reply") or "",
        "upsell": confirms >= 5 and confirms % 5 == 0,
    }


def _source_pills(draft_sources) -> list[dict]:
    """The "Grounded on" pills for one draft: its stored chunk provenance
    deduped to one pill per source, strongest match first. None (pre-feature
    rows / KB off) and [] (retrieval matched nothing) both mean no pills."""
    best: dict[str, float | None] = {}
    for s in draft_sources or []:
        name = (s or {}).get("source") or "source"
        sim = s.get("similarity")
        if name not in best or (sim or 0) > (best[name] or 0):
            best[name] = sim
    return [{"source": name, "similarity": sim}
            for name, sim in sorted(best.items(),
                                    key=lambda kv: kv[1] or 0, reverse=True)]


# One rule for the thread and the inbox row (db.conversation_preview): the
# placeholder an outbound-only round carries, and the words for each kind.
_PLACEHOLDER_BODY = db.PLACEHOLDER_BODY
_OUTBOUND_KIND_WORDS = db.OUTBOUND_KIND_WORDS


def _inbound_display(it: dict) -> tuple[str, bool]:
    """(what the thread shows as the customer's message, outbound_only). Invoice,
    quote and reminder sends are logged as one email_log row with a '[invoice]'
    placeholder where a customer message would be — the thread was rendering that
    placeholder as if the customer had written "[Invoice]" (Kris 2026-09-04). Such
    a round has NO customer message: say so, and let the template drop the bubble."""
    body = it.get("body_in") or ""
    kind = str(it.get("kind") or "")
    if _PLACEHOLDER_BODY.match(body) and kind in _OUTBOUND_KIND_WORDS:
        return (f"No message from the customer in this round — this is "
                f"{_OUTBOUND_KIND_WORDS[kind]}."), True
    return agent.strip_quoted(body).strip() or body.strip() or "No message text was included.", False


_INVOICE_SRC = re.compile(r"^invoice:([0-9a-fA-F-]{36})")
_INVOICE_REF = re.compile(r"\b(INV-\d+)\b")


def _invoice_attachment(ctx: TenantCtx, it: dict) -> list[dict]:
    """The PDF an invoice email carried, as a file the bubble can open: by the
    row's source stamp (sends since 2026-09-20), else by the reference in its
    subject (rows from before the stamp). Best-effort — never costs the thread."""
    if it.get("kind") != "invoice":
        return []
    inv_id, ref = None, None
    m = _INVOICE_SRC.match(it.get("source_message_id") or "")
    if m:
        inv_id = m.group(1)
    r = _INVOICE_REF.search(it.get("subject") or "")
    if r:
        ref = r.group(1)
    if not inv_id and ref:
        try:
            inv = db.get_invoice_by_ref(ctx.tenant_id, ref, client=ctx.client)
        except TypeError:
            inv = db.get_invoice_by_ref(ctx.tenant_id, ref)
        except Exception:  # noqa: BLE001
            inv = None
        if inv:
            inv_id, ref = inv.get("id"), inv.get("invoice_ref") or ref
    if not inv_id:
        return []
    return [{"name": f"{ref or 'invoice'}.pdf", "href": f"/app/invoices/{inv_id}/pdf"}]


REPLY_FILES_MAX = 20 * 1024 * 1024     # all files on one reply (Gmail's cap is 25 MB)


def _reply_files(ctx: TenantCtx, entry: dict, doc_ids: list[str],
                 new_files: list[tuple]) -> tuple[list[tuple], list[dict]]:
    """(attachments for the send, the stored rows they came from). Ticked
    files come off the customer's record; new ones (a photo taken there and
    then) are made web-ready and kept on the record first. Raises 400 with
    the reason when one can't be had — never a reply sent without them."""
    from app import documents, photos
    contact = (entry.get("from_address") or "").strip().lower()
    out, rows = [], []
    for doc_id in dict.fromkeys(doc_ids):
        d = db.get_document(doc_id, ctx.tenant_id, client=ctx.client)
        if not d or (d.get("contact_email") or "").lower() not in ("", contact):
            raise HTTPException(status_code=400, detail="That file isn't on this customer's record.")
        data = documents.download(d.get("url_or_path"))
        if not data:
            raise HTTPException(status_code=503, detail=f"Couldn't read {d.get('filename')} — try again.")
        name, data, ct = photos.web_ready(data, d.get("filename"), d.get("content_type"))
        out.append((name, data, ct))
        rows.append(d)
    for filename, data, ct in new_files:
        if len(data) > documents.MAX_BYTES * 3:
            raise HTTPException(status_code=400, detail=f"{filename} is too big to send.")
        name, data, ct = photos.web_ready(data, filename, ct)
        if len(data) > documents.MAX_BYTES:
            raise HTTPException(status_code=400, detail=f"{filename} is too big to send.")
        c = db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client) if contact else None
        row = documents.store(ctx.tenant_id, filename=name, data=data, content_type=ct,
                              source="upload", contact_email=contact or None,
                              contact_id=(c or {}).get("id"))
        if not row:
            raise HTTPException(status_code=503, detail=f"Couldn't keep {filename} — try again.")
        out.append((row.get("filename") or name, data, ct))
        rows.append(row)
    if sum(len(b) for _, b, _ in out) > REPLY_FILES_MAX:
        raise HTTPException(status_code=400, detail="Those files are too big to send together — pick fewer.")
    return out, rows


def _file_link(d: dict) -> dict:
    """One stored file as the thread shows it — a photo gets a thumbnail."""
    from app import photos
    return {"id": d.get("id"), "name": d.get("filename") or "attachment",
            "href": f"/app/crm/documents/{d['id']}",
            "photo": photos.is_photo(d.get("filename"), d.get("content_type"))}


def _thread_data(ctx: TenantCtx, contact: str) -> dict:
    """Everything one thread renders from — shared by the full page and the live
    fragment (GET /conversations/view/fragment) so both stay identical."""
    from app import working

    items = _their_mail_only(ctx, db.get_conversation(ctx.tenant_id, contact, client=ctx.client))
    # Display-only: drop the quoted reply-chain a client (esp. Gmail) tacks onto a reply,
    # so the thread shows just the new message. The full body_in stays in the DB for audit.
    for it in items:
        it["body_in_display"], it["outbound_only"] = _inbound_display(it)
        it["working_text"] = working.owner_panel(it.get("draft_reasoning"))
        it["working_record"] = working.unpack(it.get("draft_reasoning"))
        it["search_record"] = it["working_record"].get("retrieval") or {}
        it["recorded_query"] = (it["search_record"].get("query") or ""
                                if it["search_record"].get("state") == "ok" else "")
        it["knowledge_line"] = working.knowledge_line(it["search_record"])
        it["source_pills"] = _source_pills(it.get("draft_sources"))
        # The round ghost's "Why" (Kris 2026-09-01). Two reasons stand down, the
        # same two the stage ghost already refuses: a hand-set label has no agent
        # reasoning behind it, and a reason that merely restates the category
        # ("Reply sent — awaiting the customer") explains nothing the status line
        # above it hasn't. On this tenant that second rule is most of them.
        _why = it.get("category_reason")
        _hint = (CATEGORY_META.get(it.get("category") or "") or {}).get("hint")
        if it.get("category_source") == "manual" or (_why and _hint and
                                                     _why.strip() == _hint.strip()):
            _why = None
        it["ghost_why"] = _why
    # Attachments (Kris 2026-09-20): a paperclip on the bubble whenever the
    # message carried a file — the customer's attachment the agent saved to
    # the record (crm_artifacts remembers which email it came from), and the
    # PDF an invoice email went out with (regenerated on demand). Best-effort:
    # a documents read failure costs the clip, never the thread.
    # Photos show as thumbnails, and the files WE sent ride our bubble (photos
    # both ways, 2026-09-26). reply_files = what the draft box can attach.
    _docs_by_log: dict[str, list[dict]] = {}
    _sent_by_log: dict[str, list[dict]] = {}
    reply_files: list[dict] = []
    try:
        for d in db.list_documents(ctx.tenant_id, contact_email=contact, client=ctx.client) or []:
            if d.get("id"):
                reply_files.append(_file_link(d))
                if d.get("email_log_id"):
                    _docs_by_log.setdefault(d["email_log_id"], []).append(_file_link(d))
        for d in db.list_documents(ctx.tenant_id, contact_email=contact, sent=True,
                                   client=ctx.client) or []:
            if d.get("email_log_id") and d.get("id"):
                _sent_by_log.setdefault(d["email_log_id"], []).append(_file_link(d))
    except Exception:  # noqa: BLE001
        log.warning("thread attachments read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    for it in items:
        it["attachments"] = _docs_by_log.get(it.get("id") or "", [])
        it["reply_attachments"] = (_invoice_attachment(ctx, it)
                                   + _sent_by_log.get(it.get("id") or "", []))
    # A live entry may only carry an address (open-before-fetch), or refer to
    # a stored row being redrafted. Recover text only from that exact message:
    # the previous round must never stand in for a different inbound.
    drafting_msgs = []
    for live in events.drafting_now(ctx.tenant_id):
        if (live.get("from_address") or "").lower() != contact.lower():
            continue
        d = dict(live)
        key = d.get("key")
        row = next((it for it in reversed(items) if key and key in (
            it.get("source_message_id"), it.get("id"), f"held:{it.get('id')}")), None)
        if row:
            # The normal insert→end_drafting race: the completed row wins.
            if key == row.get("source_message_id") and (
                    row.get("draft_reply") or row.get("status") in ("sent", "skipped")):
                continue
            row["drafting"] = True
            d.update(body=row["body_in_display"], subject=row.get("subject") or "",
                     from_name=row.get("from_name") or d.get("from_name"), stored=True)
        else:
            d["body"] = (d.get("body") or "").strip() or "Message text is not available yet."
        drafting_msgs.append(d)
    drafting_here = bool(drafting_msgs)
    try:
        crm = db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client) if contact else None
    except Exception:
        crm = None
    sender_name = next(((e.get("from_name") or "").strip()
                        for e in reversed([*items, *drafting_msgs])
                        if (e.get("from_name") or "").strip()), "")
    contact_name = ((crm or {}).get("name") or "").strip() or sender_name or contact
    contact_crm_href = (f"/app/crm/contacts/{crm['id']}" if crm and crm.get("id")
                        else f"/app/crm/open?email={quote(contact)}")
    # Smart inbox label for the thread header: same latest-row-wins contract as the
    # list, fed by this thread's newest row + whether the contact still has an
    # upcoming booking (best-effort — a bookings read failure never costs the thread).
    category = category_reason = None
    if items:
        latest = items[-1]
        upcoming = False
        try:
            today = date.today().isoformat()
            upcoming = any((b.get("booking_date") or "") >= today
                           for b in db.bookings_for_contact(ctx.tenant_id, contact,
                                                            client=ctx.client))
        except Exception:
            log.warning("bookings read failed for thread header %s", contact, exc_info=True)
        category = _conversation_category({
            "state": latest.get("status"), "category": latest.get("category"),
            "category_source": latest.get("category_source"),
            "thread_state": latest.get("thread_state"),
            "gap": latest.get("knowledge_gap"), "sched_gap": latest.get("scheduling_gap"),
            "has_upcoming_booking": upcoming,
        })
        category_reason = latest.get("category_reason")
    # Draft suppression: threshold hit + owner undecided → the escalation banner
    # (Resume / Ignore permanently / Draft now). Permanently ignored is silent by
    # design — the owner already chose. Best-effort: a stats read failure never
    # costs the thread.
    supp = None
    try:
        _stats = db.get_contact_draft_stats(ctx.tenant_id, (contact or "").lower(),
                                            client=ctx.client)
        if _stats and _stats.get("status") == "suppressed":
            supp = _stats
    except Exception:
        log.warning("draft-stats read failed for thread %s", contact, exc_info=True)
    # Why you're needed (Kris 2026-08-31): the stage's bubble carries the
    # concrete reasons a human must engage — needs-you signals AND the
    # customer's temperature (concerned = amber, frustrated/angry = rose).
    # Composed from stored ground truth only.
    attention = []
    if items and category == "needs_reply":
        _l = items[-1]
        _esc = re.match(r"\[⚠ ESCALATION — matched '([^']+)'", _l.get("draft_reply") or "")
        if _esc:
            attention.append({"tone": "rose",
                              "text": f"An escalation keyword matched: “{_esc.group(1)}” — "
                                      "read the draft before it goes anywhere."})
        if _l.get("knowledge_gap"):
            attention.append({"tone": "rose",
                              "text": f"The agent couldn't answer: {_l['knowledge_gap']}."})
        if _l.get("scheduling_gap"):
            attention.append({"tone": "rose",
                              "text": "They asked to book something, and no diary "
                                      "module is active to place it."})
        if _l.get("category_reason") and _l.get("category_source") != "manual":
            attention.append({"tone": "rose", "text": "Agent assessment: " + _l["category_reason"]})
        if not attention:
            attention.append({"tone": "rose",
                              "text": "The agent flagged this thread for your judgment."})
    # Customer temperature (same treatment as the needs-you alerts, Kris
    # 2026-08-31): a mood flash explains itself here — the read, its trend,
    # and the sentence that earned it. Only notable moods are stored, so
    # presence alone means the bubble shows, whatever the thread's label.
    if items:
        try:
            _temp = db.crm_contact_temperatures(ctx.tenant_id,
                                                client=ctx.client).get(contact)
        except Exception:
            _temp = None
        if _temp and _temp.get("mood"):
            _hot = _temp["mood"] in ("frustrated", "angry")
            _line = f"The customer reads {_temp['mood']}"
            if _temp.get("trend") and _temp["trend"] != "steady":
                _line += f" and {_temp['trend']}"
            if _temp.get("reason"):
                _line += f" — {_temp['reason']}"
            attention.append({"tone": "rose" if _hot else "amber", "text": _line + "."})
    attention_tone = ("rose" if any(a["tone"] == "rose" for a in attention)
                      else "amber")
    # Agent decision (Kris 2026-08-31): the stage's centre tells the truth of the
    # newest exchange from what's already recorded — the rule-6 self-reports,
    # the KB-retrieval trace, and any diary holds this draft placed. Every piece
    # best-effort: a missing one just drops its line, never the thread.
    decision = None
    if items:
        latest = items[-1]
        kb = None
        try:
            for t in db.list_agent_trace(ctx.tenant_id, email_log_id=latest.get("id"),
                                         limit=20):
                if t.get("trace_type") == "kb_retrieval":
                    d = t.get("detail") or {}
                    kb = {"query": (d.get("query") or "").strip(),
                          "results": d.get("results_returned") or 0}
                    break
        except Exception:
            kb = None
        held = []
        try:
            try:
                _sched = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
                _noun = diary_config.nouns_of(_sched)["booking"]
            except Exception:
                _sched, _noun = None, "booking"
            held = _held_rows(db.bookings_for_log(latest.get("id"), ctx.tenant_id,
                                                  client=ctx.client), _noun, _sched)
        except Exception:
            held = []
        # The WHY (Kris: "I know it done that, I want the why"): proactive sends
        # explain their trigger from `kind`; everything else gets the model's
        # own stated reasoning about the inbound (category_reason). There is no
        # deeper stored rationale — inventing one here would be fiction.
        kind = latest.get("kind")
        if kind == "reminder":
            why = ("No customer message triggered this — it's the day-before "
                   "reminder for their booking.")
        elif kind == "offer_nudge":
            why = ("No customer message triggered this — it's a nudge about "
                   "the offered slots before they lapse.")
        elif latest.get("category_source") == "manual":
            # A hand-set label has no agent reasoning behind it — "Set manually"
            # as a WHY is noise (Kris 2026-08-31), so the line stands down until
            # the next pipeline event restamps a real one.
            why = None
        else:
            why = latest.get("category_reason")
        # category_reason feeds the rose "Why you're needed" lines too, so on a
        # flagged thread the ghost was printing the same sentence twice — once
        # in rose, once as "Why:" directly under it (live 2026-09-01). It reads
        # better where it is louder, so the quiet copy stands down.
        if why and any(a["text"].removeprefix("Agent assessment: ").strip() == why.strip() for a in attention):
            why = None
        # No they-said / I-did lines (Kris 2026-08-31): the message and the
        # draft are both on the stage — the ghost carries only the invisible
        # part of the story (why, searches, diary moves, gaps).
        # Use the recorded retrieval query. A model summary or source title
        # is not evidence of which text was searched.
        pills = latest.get("source_pills") or []
        decision = {
            "log_id": latest.get("id"),   # the ghost's match-count link target
            "why": why,
            "status": latest.get("status"),
            "kb": ({"results": latest["search_record"].get("matches", 0)}
                   if latest.get("search_record") else kb),
            # Versioned evidence wins over older traces after a failed redraft.
            "searched": (latest.get("recorded_query") if latest.get("working_record")
                         else (kb or {}).get("query")),
            "knowledge_line": latest.get("knowledge_line"),
            "sources": [p["source"] for p in pills[:3]],
            "more_sources": max(0, len(pills) - 3),
            # Legacy empty lists do not prove that no knowledge was supplied.
            "sources_n": len(pills),
            "grounded_known": latest.get("draft_sources") is not None,
            "held": held,
            "gap": latest.get("knowledge_gap"),
        }
        if not any([decision["why"], decision["searched"], decision["sources"], decision["knowledge_line"],
                    held, decision["gap"],
                    decision["grounded_known"] and not decision["sources_n"]]):
            decision = None   # nothing recorded — no ghost, not an empty header
    # Portal conversation (Kris 2026-08-31: "the inbox has no idea of the
    # Portal conversation"): the contact's portal chat transcript interleaves
    # into the history blind by timestamp — each exchange carries the portal
    # turns that happened BEFORE it (portal_before), the rest trail the thread
    # (portal_tail). Rendered as violet bubbles so the channel is unmistakable.
    portal_tail: list[dict] = []
    try:
        # Service client, not ctx.client: portal_messages is RLS'd service-role
        # only (no policies) — the tenant-scoped client reads an empty table.
        # Tenant scoping is the .eq("tenant_id") inside the helper.
        _pmsgs = db.list_portal_messages(ctx.tenant_id, contact)
    except Exception:
        _pmsgs = []
    if _pmsgs:
        _pi = 0
        for it in items:
            before = []
            while (_pi < len(_pmsgs)
                   and (_pmsgs[_pi].get("created_at") or "") < (it.get("created_at") or "9999")):
                before.append(_pmsgs[_pi])
                _pi += 1
            it["portal_before"] = before
        portal_tail = _pmsgs[_pi:]
    # Portal request strip (Kris 2026-08-31: "no way to approve it"): when the
    # newest exchange arrived FROM the portal and still awaits a reply, resolve
    # any booking ref the customer mentioned into its live row — the dock then
    # offers the cancel action right above the reply pen, so approving the
    # request and writing the follow-up are one surface.
    portal_request = None
    if items:
        _l = items[-1]
        if ((_l.get("source_message_id") or "").startswith("portal:")
                and _l.get("status") not in ("sent", "skipped")):
            for _ref in dict.fromkeys(re.findall(r"BK-\d+", _l.get("body_in") or "")):
                try:
                    b = db.booking_by_ref(ctx.tenant_id, _ref, client=ctx.client)
                except Exception:
                    b = None
                if b:
                    portal_request = {
                        "ref": _ref, "booking_id": b.get("id"),
                        "date": b.get("booking_date"), "window": b.get("window"),
                        "status": b.get("status"), "summary": b.get("summary")}
                    if b.get("status") in ("confirmed", "offered"):
                        break   # an actionable row wins over a dead one
    return dict(contact=contact, items=items, booking=_booking_pin(ctx, items),
                contact_name=contact_name, contact_crm_href=contact_crm_href,
                drafting_here=drafting_here, drafting_msgs=drafting_msgs,
                category=category, category_reason=category_reason,
                cat_meta=CATEGORY_META, suppression=supp, decision=decision,
                attention=attention, attention_tone=attention_tone,
                portal_tail=portal_tail, portal_request=portal_request,
                doc_hold=_doc_hold_for(ctx, items, contact),
                sends_with=_sends_with(ctx, items), reply_files=reply_files[:24])


def _sends_with(ctx: TenantCtx, items: list[dict]) -> dict | None:
    """What goes out WITH the waiting draft when it's approved — today the
    deposit invoice (src/app/deposits.py), which was invisible until sent."""
    if not items or items[-1].get("status") != "pending":
        return None
    from app import deposits
    last = items[-1]
    return deposits.preview_for_log(ctx.tenant_id, last.get("id"), last.get("from_address"),
                                    client=ctx.client)


def _doc_hold_for(ctx: TenantCtx, items: list[dict], contact: str) -> dict | None:
    """The document card for a thread whose reply waits on a card (Kris
    2026-09-22: "instead of the Agent responding to it, we have a notification
    box … telling us there's a document that arrived and needs our attention,
    text linked directly to it"). The held row, its documents, and what each
    would change — so the decision is made right here in the chat. None when
    nothing on the thread is held for a document. Best-effort."""
    states = {pipeline.DOC_HOLD_READING: "reading", pipeline.DOC_HOLD_CONFIRM: "confirm",
              pipeline.DOC_HOLD_WRITING: "writing"}
    held = next((e for e in reversed(items or [])
                 if e.get("status") == "pending" and not (e.get("draft_reply") or "").strip()
                 and (e.get("action_summary") or "") in states), None)
    if not held:
        return None
    out = {"id": held["id"], "state": states[held["action_summary"]],
           "who": (held.get("from_name") or "").split(" ")[0] or "The customer", "docs": []}
    try:
        from app import dependants_extract, documents as _documents, requirement_groups as rgroups
        crm = db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client) or {}
        every = db.list_documents(ctx.tenant_id, contact_email=contact, contact_id=crm.get("id"),
                                  client=ctx.client)
        for d in every:
            d["when"] = pretty_date(str(d.get("created_at") or "")[:10])
        groups = rgroups.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
        insts = db.list_requirement_instances(ctx.tenant_id, crm.get("id"), client=ctx.client) \
            if crm.get("id") else []
        names = {i["id"]: i.get("name") for i in insts}
        chips = dependants_extract.annotate(db.pending_chips_for_email(ctx.tenant_id, held["id"]),
                                            groups, insts)
        every, _ = _documents.with_updates(every, chips)
        out["docs"] = [{**d, "about": names.get(d.get("instance_id"))}
                       for d in every if d.get("email_log_id") == held["id"]]
    except Exception:  # noqa: BLE001
        log.warning("document card failed for %s", held.get("id"), exc_info=True)
    return out


@router.get("/conversations/view")
def conversation(request: Request, contact: str, ctx: TenantCtx = Depends(require_onboarded)):
    # No "Note for a colleague" here on purpose: the thread stage owns the viewport and
    # never scrolls, so a fold fights the layout — and the contact card, which
    # has it, is one tap away and is about the same person.
    return templates.TemplateResponse(request, "app/conversation.html",
        _ctx("conversations", ctx, **_thread_data(ctx, contact), **_thread_flags(ctx)))


def _thread_flags(ctx: TenantCtx) -> dict:
    """What the thread view says about the business, from the ONE config read the
    page already makes: is drafting still held, and is no sign-off set (so a waiting
    draft can say so). Best-effort: unknown = not held and nothing to announce, so
    a filed thread offers its Draft a reply rather than a dead end."""
    try:
        cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        return {"draft_hold": bool(cfg.get("draft_hold")),
                "sign_off_missing": not sign_off.normalise(cfg.get("sign_off"))}
    except Exception:
        return {"draft_hold": False, "sign_off_missing": False}


@router.get("/conversations/view/fragment")
def conversation_fragment(request: Request, contact: str, ctx: TenantCtx = Depends(require_onboarded)):
    """One thread's live half (bubbles + booking pin), no page chrome — live.js
    swaps it into #thread-live when the SSE stream reports this contact."""
    return templates.TemplateResponse(request, "app/_thread.html",
                                      {**_thread_data(ctx, contact), **_thread_flags(ctx)})


@router.get("/nav/badge")
def nav_badge(ctx: TenantCtx = Depends(require_onboarded)):
    """The Inbox menu's pending-draft count, as a fragment — live.js swaps it
    into #nav-draft-badge (base.html) whenever the SSE stream reports activity."""
    n = len(db.list_email_log(ctx.tenant_id, status="pending", client=ctx.client))
    body = (f'<span class="rounded-full bg-slate-100 text-slate-600 px-2 py-0.5 '
            f'text-xs font-semibold">{n}</span>') if n else ""
    return HTMLResponse(body)


_OPEN_DRAFT_TASKS: set = set()


async def _draft_detached(tenant_id: str, message_id: str, mailbox_id: str | None,
                          clear: bool, provider_message: dict | None = None) -> None:
    """The draft an already-open thread is waiting for, off the request.

    `clear` says this call OWNS the in-flight entry the route registered, so the
    finally is safe: whatever happens — declined before the pipeline ever marks
    it, message gone, an exception — the typing indicator clears instead of
    spinning until the 5-minute TTL prunes it. A second click does not own the
    entry and must never clear the first click's bubble."""
    try:
        await pipeline.draft_for_message(tenant_id, message_id, mailbox_id=mailbox_id,
                                         provider_message=provider_message)
    except Exception:  # noqa: BLE001 — a detached draft has no request to fail
        log.warning("detached draft failed (tenant=%s)", tenant_id, exc_info=True)
    finally:
        if clear:
            events.end_drafting(tenant_id, message_id)


@router.get("/conversations/open")
async def open_conversation(message_id: str, mailbox: str = "", contact: str = "",
                            ctx: TenantCtx = Depends(require_onboarded)):
    """A brand-new sender (no draft yet) clicked from the conversation list. Draft a reply on
    demand, then land on the thread with the draft inline to approve — the genuine auto-draft
    flow, in the conversation view, with no /inbox detour (Fix 1). draft_for_message is
    idempotent on (tenant, source_message_id), so a double-click won't double-draft.

    Reading is never blocked by writing (Kris 2026-09-21: "I want to be able to open
    the thread while the Agent is Writing the reply, it shouldn't block people"). The
    click used to hold the whole live draft — twenty seconds of a dead row wearing a
    "Drafting…" chip, and a bounce back to the list if the draft declined. Now the
    draft runs DETACHED and the thread opens at once: it already paints the inbound
    being answered and the "Agent writing reply" indicator from events.drafting_now,
    and live.js swaps the finished reply in when it lands.

    The provider fetch and trial gate stay on the request: the message is ready
    to read before redirecting, and an expired trial still routes to billing.
    An old bookmark without an address keeps the blocking fallback unless its
    message is already logged or in flight."""
    mailbox_id = _selected_mailbox_id(ctx, mailbox)
    existing = db.get_email_log_by_message(ctx.tenant_id, message_id, client=ctx.client)
    if existing:
        return RedirectResponse(_thread_after_draft(ctx, existing["id"]), status_code=303)
    in_flight = next((d for d in events.drafting_now(ctx.tenant_id)
                      if d.get("key") == message_id), None)
    if in_flight:
        return RedirectResponse("/app/conversations/view?" + urlencode({
            "contact": in_flight.get("from_address") or contact}), status_code=303)
    if not contact:
        log_id = await pipeline.draft_for_message(ctx.tenant_id, message_id,
                                                  mailbox_id=mailbox_id)
        return RedirectResponse(_thread_after_draft(ctx, log_id), status_code=303)
    tenant = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if not trial.gate_allows(tenant):
        raise trial.TrialLimitReached(trial.evaluate(tenant))
    # Fetch the inbound before opening the thread, not after redirecting to an
    # address-only placeholder. Writing remains detached; reading waits only on
    # the provider. Pass this same message to the pipeline to avoid a second fetch.
    from app import mailbox as provider
    msg = await provider.get_message(ctx.tenant_id, message_id, mailbox_id=mailbox_id)
    if not msg:
        return RedirectResponse("/app/conversations?notice=nodraft", status_code=303)
    contact = (msg.get("from") or {}).get("address") or contact
    # Another click or the poller may have finished/started during the fetch.
    existing = db.get_email_log_by_message(ctx.tenant_id, message_id, client=ctx.client)
    if existing:
        return RedirectResponse(_thread_after_draft(ctx, existing["id"]), status_code=303)
    already = any(d.get("key") == message_id for d in events.drafting_now(ctx.tenant_id))
    if not already:
        events.begin_drafting(ctx.tenant_id, message_id,
                              {"from_address": contact, "mailbox_id": mailbox_id,
                               "from_name": (msg.get("from") or {}).get("name"),
                               "subject": msg.get("subject"),
                               "body": agent.strip_quoted(msg.get("body") or "")})
        task = asyncio.get_running_loop().create_task(
            _draft_detached(ctx.tenant_id, message_id, mailbox_id, clear=True,
                            provider_message=msg))
        _OPEN_DRAFT_TASKS.add(task)
        task.add_done_callback(_OPEN_DRAFT_TASKS.discard)
    return RedirectResponse("/app/conversations/view?" + urlencode({"contact": contact}),
                            status_code=303)


# ── Ignore (stop drafting to a sender + hide their thread) ──────────────────
@router.post("/ignore", dependencies=[Depends(require_csrf)])
def add_ignored_contact(contact: str = Form(...), reason: str = Form(""), return_to: str = Form("/app/conversations"),
                        ctx: TenantCtx = Depends(require_onboarded)):
    db.ignore_contact(ctx.tenant_id, contact, (reason or "").strip() or None, client=ctx.client)
    db.skip_pending_for_contact(ctx.tenant_id, contact, client=ctx.client)  # clear anything queued
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.post("/conversations/ignore-remove", dependencies=[Depends(require_csrf)])
def ignore_remove_bulk(contacts: list[str] = Form(default=[]), ctx: TenantCtx = Depends(require_onboarded)):
    """'Ignore & Remove' from the conversations swipe (still list-shaped for any
    future bulk surface): ignore each sender (hides their thread + stops the
    agent drafting to them) and clear any queued drafts. Reversible from
    /app/ignored — the count rides the redirect so the list page can toast
    where the conversation went (never the address itself: fixed-shape query
    params only, same rule as the notice keys)."""
    seen = set()
    for raw in contacts:
        addr = (raw or "").strip()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        db.ignore_contact(ctx.tenant_id, addr, "Ignored & removed from inbox", client=ctx.client)
        db.skip_pending_for_contact(ctx.tenant_id, addr, client=ctx.client)
    suffix = f"?ignored={len(seen)}" if seen else ""
    return RedirectResponse(f"/app/conversations{suffix}", status_code=303)


@router.get("/ignored")
def ignored_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    return templates.TemplateResponse(request, "app/ignored.html",
        _ctx("ignored", ctx, ignored=db.list_ignored(ctx.tenant_id, client=ctx.client),
             suppressed=db.list_draft_suppressed(ctx.tenant_id, client=ctx.client)))


@router.post("/ignored/remove", dependencies=[Depends(require_csrf)])
def remove_ignored_contact(contact: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    db.unignore_contact(ctx.tenant_id, contact, client=ctx.client)
    return RedirectResponse("/app/ignored", status_code=303)


# ── Draft suppression (docs/modules/module-draft-suppression.md — the escalation banner) ──
@router.post("/suppression/resume", dependencies=[Depends(require_csrf)])
def suppression_resume(contact: str = Form(...), return_to: str = Form("/app/conversations"),
                       ctx: TenantCtx = Depends(require_onboarded)):
    """'Resume drafting' (banner) / 'Un-ignore' (Ignored page): clean slate —
    the streak resets to 0 and future emails draft again as normal."""
    suppression.resume(ctx.tenant_id, contact, client=ctx.client)
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.post("/suppression/ignore", dependencies=[Depends(require_csrf)])
def suppression_ignore(contact: str = Form(...), return_to: str = Form("/app/conversations"),
                       ctx: TenantCtx = Depends(require_onboarded)):
    """'Ignore permanently': no future drafts, no future banners. Their mail still
    arrives and files under FYI (the soft ignore — the hard one is /app/ignore)."""
    suppression.ignore_permanently(ctx.tenant_id, contact, by="owner", client=ctx.client)
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.post("/suppression/draft-now", dependencies=[Depends(require_csrf)])
async def suppression_draft_now(contact: str = Form(...),
                                return_to: str = Form("/app/conversations"),
                                ctx: TenantCtx = Depends(require_onboarded)):
    """'Draft now': a one-off draft for the contact's LATEST email, without
    resetting the streak — the owner wants to answer this one, not un-pause the
    contact. Reuses the redraft path (suppression.check_inbound only guards the
    automatic pipeline), then re-opens the row so the draft enters the queue."""
    items = _their_mail_only(ctx, db.get_conversation(ctx.tenant_id, contact, client=ctx.client))
    if items:
        log_id = items[-1]["id"]
        await pipeline.redraft_email_log(ctx.tenant_id, log_id, client=ctx.client)
        db.set_email_log_status(log_id, "pending", ctx.tenant_id, client=ctx.client)
        events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                       "contact": contact, "status": "pending"})
    def _blocking():
        return RedirectResponse(_safe_next(return_to), status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/drafts/{log_id}/sources")
def draft_sources(log_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """The live content behind a draft's "Grounded on" pills — the stored
    {source, chunk_id, similarity} provenance joined to the chunks' CURRENT
    text. A chunk edited or deleted since drafting comes back missing=True
    (the UI says "updated since drafting" instead of quoting stale text)."""
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if entry is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    _require_their_mail(ctx, entry)
    stored = entry.get("draft_sources") or []
    chunk_ids = [s.get("chunk_id") for s in stored if s.get("chunk_id")]
    live = {c["id"]: c for c in db.get_knowledge_chunks_by_ids(
        ctx.tenant_id, chunk_ids, client=ctx.client)}
    out = []
    for s in stored:
        chunk = live.get(s.get("chunk_id"))
        out.append({"source": s.get("source"),
                    "similarity": s.get("similarity"),
                    "content": (chunk or {}).get("content") or "",
                    "missing": chunk is None})
    return {"sources": out}


# ── Correction loop (Phase 3 F2 — "something's wrong" → fix the KB → re-draft) ──
# These return JSON for the inline panel on the review page. All gated by
# require_onboarded + CSRF and scoped to the caller's tenant.
@router.post("/drafts/{log_id}/flag", dependencies=[Depends(require_csrf)])
async def flag_draft(log_id: str, text: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    """Trace a flagged piece of the draft back to the knowledge chunks that grounded it
    — searched in the same scope the draft retrieved from (its mailbox's layer + shared)."""
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if entry is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    _require_their_mail(ctx, entry)
    if not kb.is_configured():
        return {"configured": False, "matches": []}
    try:
        matches = await kb.search_chunks(ctx.tenant_id, text, mailbox_id=entry.get("mailbox_id"))
    except Exception as e:
        return {"configured": True, "matches": [], "error": f"Couldn't search the knowledge base: {e}"}
    return {"configured": True, "matches": matches}


@router.post("/knowledge/chunk/{chunk_id}", dependencies=[Depends(require_csrf)])
async def correct_knowledge_chunk(chunk_id: str, content: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    """Correct one matched knowledge chunk in place (re-embed + save), tenant-scoped."""
    if not kb.is_configured():
        raise HTTPException(status_code=400, detail="Knowledge base is off.")
    try:
        result = await kb.update_chunk(ctx.tenant_id, chunk_id, content, client=ctx.client)
    except LookupError:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@router.post("/drafts/{log_id}/redraft", dependencies=[Depends(require_csrf)])
async def redraft_draft(log_id: str, return_to: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Re-generate this draft against the (now corrected) knowledge base.

    Two callers: the correction flow posts no `return_to` and gets {draft} JSON;
    the skipped card's "Draft a reply again" posts `return_to` — a skip isn't
    final, so the row also re-opens (status back to pending, the fresh draft
    re-enters the queue) before redirecting back to the thread. The pipeline
    itself releases the old holds and recomputes the smart label, so the skip's
    FYI filing is undone too."""
    draft = await pipeline.redraft_email_log(ctx.tenant_id, log_id, client=ctx.client)
    def _blocking():
        if draft is None:
            raise HTTPException(status_code=404, detail="Draft not found")
        if return_to:
            entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client) or {}
            _require_their_mail(ctx, entry)
            db.set_email_log_status(log_id, "pending", ctx.tenant_id, client=ctx.client)
            events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                           "contact": entry.get("from_address"),
                                           "status": "pending"})
            return RedirectResponse(_safe_next(return_to), status_code=303)
        return {"draft": draft}
    return await run_in_threadpool(_blocking)


def _held_what(b: dict, noun: str = "booking", cfg: dict | None = None) -> tuple[str, str]:
    """(what the row IS, its times) for the thread's held list (Kris 2026-09-23:
    "Offered slot doesn't differentiate between diaries — it should say the
    Diary type, Offered Meet & Greet / Offered Stay, with date and times").
    A service / appointment by its own name with start–end; a span that runs
    past its day as a stay, arrival → departure; anything else the business's
    own word for a booking, with its window."""
    from app import slotgrid
    details = b.get("details") or {}
    span = slotgrid.stay_span(b)
    svc = str(details.get("Service") or "").strip()
    w = str(b.get("window") or "")
    if svc and w == "eve":
        return svc, "Evening (out of hours)"
    if svc and w in ("am", "pm"):
        # An arrival window reads as its window, the way the customer was told
        # it (run log F19: Sarah's "morning" showed as 08:00–10:00, the first
        # two hours of it, and an owner could promise 8–10 from that).
        word = "Morning" if w == "am" else "Afternoon"
        try:
            d = datetime.fromisoformat(str(b.get("booking_date"))[:10]).date()
            lo, hi = scheduling.window_bounds(cfg or {}, d, w)
            return svc, f"{word} ({slotgrid.hhmm(lo)}–{slotgrid.hhmm(hi)})" if cfg else word
        except (TypeError, ValueError):
            return svc, word
    if svc:
        when = (slotgrid.label(int(b["start_block"]), int(b["length_blocks"]))
                if b.get("start_block") is not None and b.get("length_blocks") else
                scheduling.window_label(b.get("window")) if b.get("window") else "")
        return svc, ("" if when == "Whole day" else when)
    if span and str(b.get("window") or "day") == "day":
        a = slotgrid.abs_to_datetime(span[0])
        d = slotgrid.abs_to_datetime(span[0] + span[1])
        if d.date() == a.date():
            return "stay", f"{a:%H:%M}–{d:%H:%M}"
        return "stay", f"{a:%H:%M} → {pretty_date(d.date())}, {d:%H:%M}"
    when = scheduling.window_label(b.get("window")) if b.get("window") else ""
    # A diary that never named its bookings keeps the word it always had —
    # "Booked booking" says nothing; a session / visit says itself.
    word = noun if noun and noun != "booking" else "slot"
    return word, ("" if when == "Whole day" else when)


def _held_rows(bookings: list[dict], noun: str = "booking", cfg: dict | None = None) -> list[dict]:
    """The diary moves a draft made, for the thread ghost — LIVE rows only.
    Released / cancelled / declined holds drop out (Kris 2026-09-03: three
    "Held slot · 15:30" lines stood for one appointment after two redrafts,
    each redraft's superseded hold still rendering as held). The TIME is the
    point of a slot (Kris 2026-09-01); 'Whole day' carries no information on a
    day-granularity diary so it drops out. `label` says what the row IS —
    Booked / Offered / Held — rather than folding offers into holds."""
    out = []
    now = datetime.now(timezone.utc)
    for b in bookings or []:
        status = b.get("status")
        if status in ("cancelled", "declined", "expired"):
            continue
        # An offer past its deadline is gone, whether or not the sweep has
        # caught up — db._drop_expired_offers: "no longer exists for ANY
        # consumer". The thread is a consumer, and showing a dead offer as
        # live would have the operator believe a space is still theirs.
        until = until_iso = ""
        if status == "offered" and b.get("expires_at"):
            try:
                exp = datetime.fromisoformat(str(b["expires_at"]).replace("Z", "+00:00"))
            except ValueError:
                exp = None
            if exp is not None:
                if exp <= now:
                    continue
                # The deadline is the customer's promise, so the operator sees
                # exactly what the reply told them (Kris 2026-09-22).
                from app.offer_deadlines import display
                shown = display(b, now)
                until, until_iso = shown["until"], shown["until_iso"]
        what, when = _held_what(b, noun, cfg)
        label = ("Booked" if status == "confirmed"
                 else "Offered" if status == "offered" else "Held")
        # The line is the way to the slot (Kris 2026-09-20: "quick
        # navigation"): the Diary on that day with this booking lit, or the
        # booking's own page when the row carries no day.
        bid = str(b.get("id") or "")
        date = str(b.get("booking_date") or "")[:10]
        href = (f"/app/diary?day={date}&booking={bid}" if date and bid
                else f"/app/diary?day={date}" if date
                else f"/app/diary/{bid}" if bid else "")
        out.append({"status": status, "label": label, "what": what,
                    "date": b.get("booking_date"), "when": when,
                    # Only an offer carries one: a confirmed booking has no
                    # deadline, and a proposed hold's deadline is the owner's
                    # approval, not a clock the customer is watching.
                    "until": until,
                    # The thread's compact line (Kris 2026-09-27: the three
                    # offer lines filled half a phone screen): a live
                    # countdown from until_iso, and the window as "Morning 8–12".
                    "until_iso": until_iso, "until_left": _left(until_iso, now),
                    "when_short": _short_when(when),
                    "id": bid or None, "href": href})
    return out


def _left(until_iso: str, now) -> str:
    """The countdown's first paint ("3h 01m", "42m"); hold_clock.js keeps it live."""
    if not until_iso:
        return ""
    mins = int((datetime.fromisoformat(until_iso) - now).total_seconds() // 60)
    if mins <= 0:
        return "releasing"
    return f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def _short_when(when: str) -> str:
    """'Morning (08:00–12:00)' → 'Morning 8–12'; '12:30–13:00' stays as it is."""
    def hm(t: str) -> str:
        h, m = t.split(":")
        return str(int(h)) + ("" if m == "00" else ":" + m)
    import re as _re
    return _re.sub(r"\s*\((\d{2}:\d{2})–(\d{2}:\d{2})\)",
                   lambda m: f" {hm(m.group(1))}–{hm(m.group(2))}", when or "")


def _append_rule(tenant_id: str, field: str, rule: str, client=None) -> bool:
    """Append one line to a tenant rule list (`never_do` | `do_list`), de-duped.
    True if it was newly added."""
    rule = (rule or "").strip()
    if not rule:
        return False
    cfg = db.get_tenant_config(tenant_id, client=client) or {}
    lines = [ln.strip() for ln in str(cfg.get(field) or "").splitlines() if ln.strip()]
    if rule in lines:
        return False
    lines.append(rule)
    db.upsert_tenant_config(tenant_id, {field: chr(10).join(lines)}, client=client)
    return True


def _append_never_rule(tenant_id: str, rule: str, client=None) -> bool:
    """Append a 'never say / never do' rule to the tenant's permanent Never-do rules (the
    same field the Settings page edits and agent.py injects as 'Never:' into every prompt),
    de-duped. Returns True if it was newly added. The thumbs-down draft feedback writes here
    so a correction shapes EVERY future reply, not just this one."""
    rule = (rule or "").strip()
    if not rule:
        return False
    cfg = db.get_tenant_config(tenant_id, client=client) or {}
    lines = [ln.strip() for ln in str(cfg.get("never_do") or "").splitlines() if ln.strip()]
    if rule in lines:
        return False
    lines.append(rule)
    db.upsert_tenant_config(tenant_id, {"never_do": "\n".join(lines)}, client=client)
    return True


@router.post("/drafts/{log_id}/correct", dependencies=[Depends(require_csrf)])
async def correct_draft(log_id: str, never: str = Form(""),
                        ctx: TenantCtx = Depends(require_onboarded)):
    """Thumbs-down feedback: capture a 'never say / don't do this' rule from the draft,
    save it to the tenant's permanent Never-do rules (so it applies to every future reply,
    like the onboarding rules), then re-draft this reply so the fix shows immediately. A
    blank `never` just re-drafts. Returns {draft, saved}."""
    _require_their_log(ctx, log_id)
    saved = _append_never_rule(ctx.tenant_id, never, client=ctx.client)
    draft = await pipeline.redraft_email_log(ctx.tenant_id, log_id, client=ctx.client)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"draft": draft, "saved": saved}


@router.post("/drafts/{log_id}/edit-review", dependencies=[Depends(require_csrf_header)])
async def edit_review_suggest(log_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """After a send where the owner changed the agent's draft substantively: what
    (if anything) is worth teaching the agent — {kind, title, text, why}, kind
    'none' when nothing (the card then never shows). See src/app/edit_review.py."""
    _require_their_log(ctx, log_id)
    entry = db.get_email_log_entry(log_id, ctx.tenant_id, client=ctx.client)
    if entry is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    original, sent = entry.get("draft_original") or "", entry.get("draft_reply") or ""
    if not edit_review.substantive(original, sent):
        return {"kind": "none", "title": "", "text": "", "why": ""}
    return await edit_review.suggest(ctx.tenant_id, original, sent)


@router.post("/drafts/{log_id}/edit-review/save", dependencies=[Depends(require_csrf_header)])
async def edit_review_save(log_id: str, request: Request,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Save what the edit-review card confirmed: `kind` knowledge | always | never,
    `text` (as the owner left it) and `title` (knowledge only)."""
    require_can(ctx, "settings", "change")
    _require_their_log(ctx, log_id)
    body = await request.json()
    kind = str(body.get("kind") or "")
    text = str(body.get("text") or "").strip()[:2000]
    title = str(body.get("title") or "").strip()[:80] or text[:60]
    if kind not in ("knowledge", "always", "never") or not text:
        raise HTTPException(status_code=400, detail="Nothing to save.")
    if kind == "knowledge":
        await kb.ingest(ctx.tenant_id, f"Edit review · {title}"[:120], text, client=ctx.client)
        db.log_agent_event(ctx.tenant_id, "knowledge",
                           f"Added “{title}” to the knowledge base from an edited draft "
                           "— confirmed by you", client=ctx.client)
    else:
        _append_rule(ctx.tenant_id, "do_list" if kind == "always" else "never_do", text,
                     client=ctx.client)
        db.log_agent_event(ctx.tenant_id, "knowledge",
                           f"Added a {'do' if kind == 'always' else 'never-do'} rule from an "
                           f"edited draft: {text[:120]} — confirmed by you", client=ctx.client)
    return {"saved": kind}


@router.post("/inbox/{message_id}/draft", dependencies=[Depends(require_csrf)])
async def draft_for_message(message_id: str, return_to: str = Form(""), mailbox_id: str = Form(""),
                            ctx: TenantCtx = Depends(require_onboarded)):
    """Draft a reply to a specific inbound message, then return to the thread (or the
    draft). `mailbox_id` (enterprise) is the mailbox the message lives in — it picks
    the token to fetch with, the brain that drafts, and the address that replies."""
    log_id = await pipeline.draft_for_message(
        ctx.tenant_id, message_id, mailbox_id=_selected_mailbox_id(ctx, mailbox_id))
    def _blocking():
        if return_to:
            return RedirectResponse(_safe_next(return_to), status_code=303)
        return RedirectResponse(_thread_after_draft(ctx, log_id), status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/generate", dependencies=[Depends(require_csrf)])
async def generate_draft(ctx: TenantCtx = Depends(require_onboarded)):
    """Manual override: draft a reply to the single latest email."""
    log_id = await pipeline.draft_for_latest(ctx.tenant_id)
    def _blocking():
        return RedirectResponse(_thread_after_draft(ctx, log_id), status_code=303)
    return await run_in_threadpool(_blocking)


# ── Sent ───────────────────────────────────────────────────────────────────
@router.get("/sent")
def sent(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    items = _their_mail_only(ctx, db.list_email_log(ctx.tenant_id, status="sent", client=ctx.client))
    return templates.TemplateResponse(request, "app/sent.html", _ctx("sent", ctx, items=items))


# ── CRM (docs/architecture/CRM_design.md — "the CRM that fills itself in") ────────────────
def _crm_teaser(ctx: TenantCtx) -> dict | None:
    """What the CRM has already filled in for a tenant who hasn't unlocked it —
    the zero-LLM ingest runs for everyone. Best-effort: a read failure hides
    the teaser, never the page; nothing met yet renders no teaser."""
    try:
        n_contacts = len(db.crm_list_contacts(ctx.tenant_id, client=ctx.client))
        n_companies = len(db.crm_list_companies(ctx.tenant_id, client=ctx.client))
        return {"contacts": n_contacts, "companies": n_companies} if n_contacts else None
    except Exception:
        return None


def _crm_unlocked(ctx: TenantCtx) -> bool:
    return "crm" in db.visible_modules(ctx.tenant_id, client=ctx.client)


def require_crm(ctx: TenantCtx = Depends(require_onboarded)) -> TenantCtx:
    """CRM tier gate (docs/crm-vs-os-architecture.md §1, Julius 2026-07-24): the
    CRM unlocks at Core (£99) as the 'crm' module, bundled by
    plans.BASE_PLAN_MODULES from starter up. Visible counts preview too, matching
    every other module surface; the pipeline's LLM passes gate separately on
    module_active. A locked account is sent to the CRM's front door, which
    shows the lock (Kris 2026-09-13: "when someone has used the trial and gone
    for Starter, the CRM should be locked" — a door, not a 404)."""
    if not _crm_unlocked(ctx):
        raise HTTPException(status_code=303, headers={"Location": "/app/crm"})
    return ctx


# How far back a staff member's own customers reach. A year covers a season
# and then some; the whole book would make "theirs" meaningless on a business
# that has been running for a decade.
CRM_MINE_DAYS = 365


def require_crm_write(ctx: TenantCtx, contact_id: str = "") -> None:
    """Changing a customer is a tick of its own ("Change customer details"),
    and it only extends as far as the customers that are theirs. Both checks
    together, because either alone leaves a way round: the tick without the
    scope lets a staff member edit the whole book, and the scope without the
    tick lets a read-only pair of hands rewrite their own customers."""
    require_can(ctx, "crm", "edit")
    if not contact_id:
        return
    mine, allowed = _crm_lens(ctx)
    if not crm_visibility.narrows_contacts(mine):
        return
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if not crm_visibility.may_open(contact, mine, allowed):
        raise HTTPException(status_code=403,
                            detail="That customer isn't one of yours to change.")


def _crm_lens(ctx: TenantCtx) -> tuple[dict | None, set[str]]:
    """(this person, the addresses their CRM contains). An empty set with a
    narrowed person means they genuinely have nobody yet; `narrows_contacts`
    is what says whether to apply it at all, never the emptiness."""
    me = _me(ctx)
    if not crm_visibility.narrows_contacts(me):
        return me, set()
    try:
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client)
        since = (date.today() - timedelta(days=CRM_MINE_DAYS)).isoformat()
        rows = db.list_booking_contacts(ctx.tenant_id, since, client=ctx.client)
    except Exception:
        log.warning("crm lens read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return me, set()
    return me, crm_visibility.theirs(me, rows, cfg)


@router.get("/search")
def masthead_search(q: str = "", ctx: TenantCtx = Depends(require_onboarded)):
    """The masthead's magnifying glass (Kris 2026-09-21; src/app/search.py): a
    booking, quote or invoice by its reference, a customer by name or email,
    suggested as they type. Every result obeys the door and the lens of the
    page it opens — a staff member sees the bookings that are their work, the
    customers behind it, and no invoices without the Money door — so nothing
    is found here that could not be reached anyway. Ignored senders are not
    customers here either (the CRM list's rule)."""
    from app import search as _search
    want = _search.parse(q)
    me = _me(ctx)
    results: list[dict] = []
    if want["refs"]:
        if "BK" in want["refs"] and people.can_open(me, "diary"):
            rows = db.search_booking_refs(ctx.tenant_id, want["refs"]["BK"],
                                          limit=_search.PER_KIND, client=ctx.client)
            if people.does(me):
                cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
                from app import job_people as _jp
                _jp.annotate(ctx.tenant_id, rows)
                rows = [b for b in rows if people.owns_booking(me, b, cfg)]
            results += [_search.booking_result(b) for b in rows]
        if people.can_open(me, "money"):
            if "QUO" in want["refs"]:
                results += [_search.quote_result(r) for r in db.search_quote_refs(
                    ctx.tenant_id, want["refs"]["QUO"], limit=_search.PER_KIND, client=ctx.client)]
            if "INV" in want["refs"]:
                results += [_search.invoice_result(r) for r in db.search_invoice_refs(
                    ctx.tenant_id, want["refs"]["INV"], limit=_search.PER_KIND, client=ctx.client)]
    elif len(want["text"]) >= _search.MIN_TEXT and people.can_open(me, "crm") and _crm_unlocked(ctx):
        contacts = db.crm_list_contacts(ctx.tenant_id, q=_search.safe_text(want["text"]),
                                        limit=_search.MAX_RESULTS + 4, client=ctx.client)
        try:
            ignored = {a.lower() for a in db.ignored_addresses(ctx.tenant_id, client=ctx.client)}
            if ignored:
                contacts = [c for c in contacts if (c.get("email") or "").lower() not in ignored]
        except Exception:
            pass
        _mine, _allowed = _crm_lens(ctx)
        contacts = crm_visibility.keep_contacts(contacts, _mine, _allowed)
        results += [_search.contact_result(c) for c in contacts]
    return {"q": _search.clean(q), "results": results[:_search.MAX_RESULTS]}


@router.get("/crm")
def crm_contacts_page(request: Request, q: str = "", sort: str = "",
                      ctx: TenantCtx = Depends(require_onboarded)):
    """The CRM's front door: everyone BLAM8 has met, newest activity first —
    auto-populated from email_log, never typed in. Locked on a plan without
    the CRM: the same door, showing what is behind it and the way to Pro."""
    if not _crm_unlocked(ctx):
        from app import plans as _plans
        return templates.TemplateResponse(request, "app/crm_locked.html", _ctx(
            "crm_locked", ctx, crm_teaser=_crm_teaser(ctx),
            unlock_plan=_plans.plan_name("starter") or "Pro",
            unlock_price=_plans.plan_price_gbp("starter")))
    # Ignored senders are not contacts (Kris 2026-09-01). Telling the agent to
    # ignore someone is the operator saying "this is not a customer" — and on the
    # BLAM8 tenant that was already 7 of 11 rows: Cloudflare, Perplexity,
    # smtp2go, a mailing-list robot. A CRM whose list is more than half noise is
    # not a CRM you look at.
    #
    # Filtered at READ time against ignored_contacts rather than stamped onto
    # crm_contacts. One source of truth, so nothing can drift; un-ignoring
    # restores the contact and its whole history for free; and the row keeps
    # accruing nothing meanwhile, because the pipeline's ignore gate returns
    # before CRM ingest anyway.
    contacts = crm.with_recency(
        db.crm_list_contacts(ctx.tenant_id, q=q.strip() or None, client=ctx.client))
    hidden = 0
    try:
        _ignored = {a.lower() for a in db.ignored_addresses(ctx.tenant_id, client=ctx.client)}
        if _ignored:
            _before = len(contacts)
            contacts = [c for c in contacts
                        if (c.get("email") or "").lower() not in _ignored]
            hidden = _before - len(contacts)
    except Exception:
        # Best-effort, like every other CRM read: if the ignore list can't be
        # read, show every contact rather than an empty page.
        hidden = 0

    # Whose customers (People slice 3). Applied AFTER the ignore filter and
    # BEFORE the dock, which is built from these names — so a promise about
    # somebody else's customer never reaches their rail either.
    _mine, _allowed = _crm_lens(ctx)
    contacts = crm_visibility.keep_contacts(contacts, _mine, _allowed)

    # The CRM's lens on the dock rail: open promises, both directions — what
    # you said you'd do and what they owe you by. Best-effort like every other
    # CRM read: a failure hides the rail, never the page.
    dock = []
    open_promises: list = []
    try:
        names = {c["id"]: (c.get("name") or c.get("email") or "someone") for c in contacts}
        open_promises = promise_kinds.obligations(
            db.crm_open_promises(ctx.tenant_id, client=ctx.client))
        for pr in open_promises:
            who = names.get(pr.get("contact_id"))
            if not who:
                continue
            owe = pr.get("direction") == "we_owe"
            desc = (pr.get("description") or "").strip()
            if len(desc) > 44:
                desc = desc[:43] + "…"
            dock.append({
                "tone": "rose" if owe else "plain",
                "href": f"/app/crm/contacts/{pr.get('contact_id')}",
                "label": ("You owe" if owe else f"{who} owes") + f" · {desc}"
                         + (f" · {pr['due_hint']}" if pr.get("due_hint") else "")})
        # Cleared this week (Kris 2026-09-20): the green tick on the rail, so a
        # promise that just cleared is SEEN to clear rather than vanish. Its own
        # try — the open chips must survive a cleared-read failure.
        try:
            for pr in db.crm_recent_cleared_promises(ctx.tenant_id, days=7,
                                                      client=ctx.client):
                who = names.get(pr.get("contact_id"))
                if not who or pr.get("status") == "dropped":
                    continue
                desc = (pr.get("description") or "").strip()
                if len(desc) > 44:
                    desc = desc[:43] + "…"
                dock.append({
                    "tone": "emerald",
                    "href": f"/app/crm/contacts/{pr.get('contact_id')}",
                    "label": f"✓ Cleared · {desc}"})
        except Exception:
            pass
    except Exception:
        dock = []

    # The logbook (Kris 2026-09-21): each name with its dependants, its last
    # log line and the facts that need an eye — the next stay, what they owe,
    # promises open — from four bounded reads fanned out together. Best-effort,
    # each on its own: a failed read empties its column, never the page.
    def _read(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return []
    _fs = [_FAN.submit(_read, db.list_requirement_instances_all, ctx.tenant_id, client=ctx.client),
           _FAN.submit(_read, db.list_bookings, ctx.tenant_id, date.today().isoformat(), client=ctx.client),
           _FAN.submit(_read, db.list_invoices, ctx.tenant_id, client=ctx.client),
           _FAN.submit(_read, db.crm_latest_activities, ctx.tenant_id, client=ctx.client)]
    _deps, _bookings, _invoices, _acts = (f.result() for f in _fs)
    order = "recent" if sort == "recent" else "name"   # A–Z by default, Recent on ask (Kris 2026-09-21)
    logbook = crm.logbook(contacts, dependants=_deps, bookings=_bookings, invoices=_invoices,
                          promises=open_promises, activities=_acts, order=order)
    return templates.TemplateResponse(request, "app/crm_contacts.html",
                                      _ctx("crm", ctx, contacts=contacts, q=q.strip(),
                                           hidden=hidden, dock=dock[:8], logbook=logbook, order=order,
                                           msg=request.query_params.get("msg") or ""))


@router.get("/crm/companies")
def crm_companies_page(request: Request, ctx: TenantCtx = Depends(require_crm)):
    companies = crm.with_recency(db.crm_list_companies(ctx.tenant_id, client=ctx.client))
    return templates.TemplateResponse(request, "app/crm_companies.html",
                                      _ctx("crm_companies", ctx, companies=companies))


@router.get("/crm/pipeline")
def crm_pipeline_page(request: Request, ctx: TenantCtx = Depends(require_crm)):
    """Deals grouped under the tenant's stages. Sparse in phase 1 (deals arrive
    with phase 3's auto-clustering) — the surface exists so the pipeline is real
    from day one."""
    stages = crm.ensure_default_stages(ctx.tenant_id, client=ctx.client)
    deals = db.crm_list_deals(ctx.tenant_id, client=ctx.client)
    return templates.TemplateResponse(request, "app/crm_pipeline.html",
                                      _ctx("crm_pipeline", ctx, stages=stages,
                                           columns=crm.pipeline_view(stages, deals)))


@router.get("/crm/open")
def crm_open(email: str = "", ctx: TenantCtx = Depends(require_crm)):
    """The customer's CRM card by address — the inbox's left swipe (Kris
    2026-09-13). No card yet → the contacts list, searched for them."""
    from urllib.parse import quote
    addr = (email or "").strip().lower()[:254]
    row = db.crm_contact_by_email(ctx.tenant_id, addr, client=ctx.client) if addr else None
    if row and row.get("id"):
        return RedirectResponse(f"/app/crm/contacts/{row['id']}", status_code=303)
    # No card yet: the list searched for them, with a line saying why (Kris
    # 2026-09-13: "the dead landing is fine, a small explanation… helpful").
    return RedirectResponse(f"/app/crm?q={quote(addr)}&from=inbox" if addr else "/app/crm", status_code=303)


@router.get("/crm/contacts/{contact_id}")
def crm_contact_page(request: Request, contact_id: str,
                     ctx: TenantCtx = Depends(require_crm)):
    """The killer view (design §8): who they are, their deals, the whole
    conversation history, and how long they've been quiet — with a one-click
    path into the draft flow."""
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    # Somebody else's customer. Back to their own list with a line saying so,
    # not a 404 — the person exists, they are just not this person's work, and
    # "not found" would send them looking for a bug (People slice 3).
    _mine, _allowed = _crm_lens(ctx)
    if not crm_visibility.may_open(contact, _mine, _allowed):
        return RedirectResponse("/app/crm?msg=nottheirs", status_code=303)
    contact = crm.with_recency([contact])[0]
    deals = [{**d, "days_open": crm.days_since(d.get("opened_at"))}
             for d in db.crm_list_deals(ctx.tenant_id, contact_id=contact_id, client=ctx.client)]
    activities = db.crm_activities_for_contact(contact_id, ctx.tenant_id, limit=20,
                                               client=ctx.client)
    # Custom fields (phase 2): the tenant's definitions rendered against this
    # contact's (and their company's) custom_data, plus any pending suggestions
    # awaiting one-click confirm. No definitions → the About card invites setup.
    defs = db.crm_list_field_definitions(ctx.tenant_id, client=ctx.client)
    # What the business marks private stays private unless this person holds
    # "See private details" — the tick People already offers, whose own words
    # are "Access codes, keys, medical notes". Narrowed HERE, at the source, so
    # the card, the suggestions and anything else built from `defs` all agree.
    defs = crm_visibility.visible_defs(defs, _mine)
    contact["custom_data"] = crm_visibility.redact(contact.get("custom_data"), defs, _mine)
    company = contact.get("crm_companies") or {}
    fields = _crm_card_fields(defs, "contact", contact) + \
        (_crm_card_fields(defs, "company", db.crm_get_company(
            company["id"], ctx.tenant_id, client=ctx.client) or {}) if company.get("id") else [])
    record_ids = [contact_id] + ([company["id"]] if company.get("id") else [])
    suggestions = _crm_annotate_suggestions(
        db.crm_pending_suggestions(ctx.tenant_id, record_ids=record_ids, client=ctx.client), defs,
        {"contact": contact.get("custom_data") or {},
         "company": (company or {}).get("custom_data") or {}})
    _all_open = promise_kinds.annotate(
        db.crm_open_promises(ctx.tenant_id, contact_id=contact_id, client=ctx.client))
    promises = [p for p in _all_open if p["kind"] == "obligation"]
    day_notes = [p for p in _all_open if p["kind"] == "on_the_day"]
    # The cleared state (Kris 2026-09-20): an open row BLAM8 believes is done
    # reads amber with a Confirm; the recently cleared sit under the open ones,
    # green with a tick and a link to whatever cleared them. Best-effort like
    # every other CRM read — a failure hides the ticks, never the page.
    for p in promises:
        if p.get("resolution_type") == "suggested":
            p["by"] = crm.cleared_by(p, contact.get("email"))
    try:
        cleared = db.crm_recent_cleared_promises(ctx.tenant_id, contact_id=contact_id,
                                                  client=ctx.client)
        for p in cleared:
            p["by"] = crm.cleared_by(p, contact.get("email"))
    except Exception:
        cleared = []
    # Invoice history: how much they've been billed, how much they've actually paid,
    # and how quickly. Best-effort like the enrollments below — a tenant whose
    # invoicing has never been touched must not 500 their contact page.
    try:
        contact_invoices = db.invoices_for_crm_contact(
            ctx.tenant_id, contact_id, email=contact.get("email"), client=ctx.client)
        invoice_stats = db.summarize_invoice_rows(contact_invoices)
    except Exception:
        contact_invoices, invoice_stats = [], None
    # Quote history (docs/modules/module-quotes.md Phase 2) — same best-effort stance
    # and the same contact_id + email-fallback matching as invoices.
    try:
        contact_quotes = db.quotes_for_crm_contact(
            ctx.tenant_id, contact_id, email=contact.get("email"), client=ctx.client)
        accepted = [q for q in contact_quotes
                    if q.get("status") in ("accepted", "invoiced")]
        answered = accepted + [q for q in contact_quotes
                               if q.get("status") in ("declined", "expired")]
        quote_stats = {
            "quoted": round(sum(float(q.get("total") or 0)
                                for q in contact_quotes
                                if q.get("status") != "draft"), 2),
            "won": round(sum(float(q.get("total") or 0) for q in accepted), 2),
            "acceptance_rate": (round(100 * len(accepted) / len(answered))
                                if answered else None),
        } if contact_quotes else None
    except Exception:
        contact_quotes, quote_stats = [], None
    # Job parties (docs/modules/module-job-parties.md Phase 1): every job this person is
    # a party on — "property owner on BK-1077 (pays, approves)". Best-effort: the line
    # is a courtesy; it never 500s the contact page.
    try:
        from app import job_parties
        party_rows = db.job_parties_for_contact(
            ctx.tenant_id, contact_id, email=contact.get("email"), client=ctx.client)
        jobs = {b["id"]: b for b in db.bookings_by_ids(
            ctx.tenant_id, [p["booking_id"] for p in party_rows], client=ctx.client)}
        party_roles = sorted(
            [{"booking_id": p["booking_id"],
              "booking_ref": jobs[p["booking_id"]].get("booking_ref"),
              "booking_date": jobs[p["booking_id"]].get("booking_date"),
              "summary": jobs[p["booking_id"]].get("summary"),
              "label": p.get("label"),
              "duties": ", ".join(job_parties.DUTY_LABELS[d].lower()
                                  for d in job_parties.DUTIES if p.get(d))}
             for p in party_rows if p["booking_id"] in jobs],
            key=lambda r: r.get("booking_date") or "", reverse=True)
    except Exception:
        party_roles = []
    # Dependants (docs/modules/module-requirement-groups.md): the kinds the tenant
    # defined, and this customer's own — Murphy, Tilly — with what is known.
    dep_groups, dep_instances, dep_suggestions = [], [], []
    try:
        from app import requirement_groups as rgroups
        dep_groups = rgroups.clean_groups(db.list_requirement_groups(ctx.tenant_id, client=ctx.client))
        # A dependant's own fields obey the same rule as a customer's: the
        # "Property" group is exactly where a key-safe number lives. Narrowed
        # on the GROUP, so the rows, the gaps and the one-line summary all
        # agree rather than one of them leaking what the others hide.
        for _g in dep_groups:
            _g["fields"] = crm_visibility.visible_group_fields(_g, _mine)
        if dep_groups:
            by_id = {g.get("id"): g for g in dep_groups}
            for inst in db.list_requirement_instances(ctx.tenant_id, contact_id, client=ctx.client):
                g = by_id.get(inst.get("group_id"))
                if not g:
                    continue
                inst["group"] = g
                inst["data"] = crm_visibility.redact_dependant(inst.get("data"), g, _mine)
                inst["line"] = rgroups.instance_line(g, inst)
                inst["gaps"] = rgroups.missing_for(g, inst.get("data"))
                inst["rows"] = [(f["label"], (inst.get("data") or {}).get(f["key"]), f)
                                for f in g["fields"]]
                dep_instances.append(inst)
            # chips read from their emails (Phase 2): on an instance, or a new one on the contact
            from app import dependants_extract
            pend = db.crm_pending_suggestions(
                ctx.tenant_id, record_ids=[contact_id] + [i["id"] for i in dep_instances], client=ctx.client)
            dep_suggestions = dependants_extract.annotate(pend, dep_groups, dep_instances)
    except Exception:
        dep_groups, dep_instances, dep_suggestions = [], [], []
    # Phase 4b: this contact's sequence enrollments (best-effort — never 500 the page).
    try:
        enrollments = db.crm_list_enrollments(ctx.tenant_id, contact_id=contact_id,
                                              client=ctx.client)
        seq_names = {s["id"]: s.get("name")
                     for s in db.crm_list_sequences(ctx.tenant_id, include_archived=True,
                                                    client=ctx.client)}
        for e in enrollments:
            e["sequence_name"] = seq_names.get(e.get("sequence_id"))
    except Exception:
        enrollments = []
    # Documents on the record (src/app/documents.py): the files kept from their
    # emails and uploads; the checklist's document-type labels they can be
    # filed as; their bookings and dependants they can be pinned to.
    documents_rows, doc_labels, doc_bookings = [], [], []
    try:
        from app import documents as _documents
        documents_rows = db.list_documents(ctx.tenant_id, contact_email=contact.get("email"),
                                           contact_id=contact_id, client=ctx.client)
        for d in documents_rows:
            d["size_text"] = _documents.pretty_size(d.get("size_bytes"))
            d["when"] = pretty_date(str(d.get("created_at") or "")[:10])
        # What each document asks to change (a new certificate → "use it to update
        # Alfie?"), shown ON the document; those chips leave the Dependants list.
        documents_rows, dep_suggestions = _documents.with_updates(documents_rows, dep_suggestions)
        sched = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        doc_labels = [f.get("label") for f in (sched.get("intake_fields") or []) if f.get("doc") and f.get("label")]
        doc_bookings = [{"id": b["id"], "text": f"{b.get('booking_ref') or ''} · {pretty_date(str(b.get('booking_date') or ''))}"}
                        for b in db.bookings_for_contact(ctx.tenant_id, contact.get("email"), client=ctx.client)]
    except Exception:
        documents_rows, doc_labels, doc_bookings = [], [], []
    # The door that fulfils each promise (promise_kinds.route), beside Done / Drop.
    for _p in promises:
        _p["action"] = promise_kinds.route(_p, contact=contact,
                                           pets=[_i.get("name") for _i in dep_instances if _i.get("name")],
                                           today=datetime.now(scheduling.DIARY_TZ).date())
    return templates.TemplateResponse(request, "app/crm_contact.html",
                                      _ctx("crm", ctx, c=contact, deals=deals,
                                           why=(request.query_params.get("why") or "")[:300],
                                           editable_fields=[d for d in defs if d.get("entity") == "contact"
                                                            and not isinstance(d.get("ladder"), dict)],
                                           documents=documents_rows, doc_labels=doc_labels,
                                           doc_bookings=doc_bookings,
                                           activities=activities, fields=fields,
                                           suggestions=suggestions,
                                           promises=promises, cleared=cleared, day_notes=day_notes,
                                           enrollments=enrollments,
                                           party_roles=party_roles,
                                           # The note says WHO it is about by name (a CRM
                                           # contact has `name`; display_name was never set
                                           # here, so the line read the address).
                                           **_say_ctx(ctx, "contact", contact.get("email") or "",
                                                      contact.get("name") or contact.get("email") or "this customer"),
                                           contact_invoices=contact_invoices,
                                           invoice_stats=invoice_stats,
                                           contact_quotes=contact_quotes,
                                           quote_stats=quote_stats,
                                           dep_groups=dep_groups, dep_instances=dep_instances,
                                           # Packages & credits (src/app/wallet.py): what they bought ahead of use.
                                           **__import__("app.wallet", fromlist=["card_context"]).card_context(
                                               ctx.tenant_id, contact.get("id"), client=ctx.client),
                                           # Waiting lists (src/app/courses.py).
                                           **__import__("app.courses", fromlist=["card_context"]).card_context(
                                               ctx.tenant_id, contact, client=ctx.client),
                                           dep_suggestions=dep_suggestions,
                                           has_field_defs=bool(defs)))


def _crm_card_fields(defs: list[dict], entity: str, record: dict) -> list[dict]:
    """View-model rows for the About card: one per definition of this entity,
    labelled and display-formatted from the record's custom_data."""
    data = (record or {}).get("custom_data") or {}
    from app import ladders as _ladders
    return [{"label": d["label"], "entity": entity, "key": d["key"], "raw": data.get(d["key"]),
             "value": crm_fields.display_value(d, data.get(d["key"])),
             # Certificates (slice 4): "lapses 12th Dec 26 (in 98 days)" beside the date.
             "note": crm_fields.expiry_note(d, data.get(d["key"]), date.today()),
             # A customer ladder's levels, for the card's own Move control (run #8).
             "ladder": (_ladders.clean_levels(d.get("options"))
                        if entity == "contact" and isinstance(d.get("ladder"), dict) else None)}
            for d in defs if d["entity"] == entity]


@router.post("/diary/{booking_id}/edit", dependencies=[Depends(require_csrf)])
async def diary_job_edit(booking_id: str, request: Request,
                         ctx: TenantCtx = Depends(require_onboarded)):
    """Move a booking or rename it BY HAND from the job page (Kris 2026-09-06:
    the diary could add and cancel but never change). The move plan is the one
    the Concierge's move card uses (src/app/record_edits.py) — same placement
    rules, same in-place write, same calendar refresh."""
    from app import record_edits
    back = f"/app/diary/{booking_id}"
    b = db.get_booking(booking_id, ctx.tenant_id, client=ctx.client)
    if not b:
        raise HTTPException(status_code=404, detail="Booking not found")
    form = await request.form()
    new_date = str(form.get("new_date") or "").strip()
    when = str(form.get("when") or "").strip()
    when_was = str(form.get("when_was") or "").strip()
    summary = str(form.get("summary") or "").strip()
    arrival = str(form.get("arrival") or "").strip()
    departure = str(form.get("departure") or "").strip()
    stay_changed = ((arrival and arrival != str(form.get("arrival_was") or "").strip())
                    or (departure and departure != str(form.get("departure_was") or "").strip()))
    # Only a CHANGED day or time is a move — the form always posts both
    # (live 2026-09-06: a rename-only save re-placed the booking at 08:00).
    same_day = (not new_date) or new_date == str(b.get("booking_date") or "")[:10]
    same_time = (not when) or when.lower() == when_was.lower()
    done = []
    try:
        if summary and summary != (b.get("summary") or ""):
            record_edits.edit_booking_summary(ctx.tenant_id, b, summary, client=ctx.client)
            done.append("renamed")
        if stay_changed:
            # A stay: the same plan + apply the Concierge's change_stay card runs.
            plan = record_edits.plan_stay_change(ctx.tenant_id, b, new_arrival=arrival or None,
                                                 new_departure=departure or None, client=ctx.client)
            await record_edits.apply_stay_change(ctx.tenant_id, b, plan, client=ctx.client)
            done.append("moved")
        elif not (same_day and same_time):
            plan = record_edits.plan_booking_move(
                ctx.tenant_id, b, new_date or str(b.get("booking_date"))[:10],
                when or (b.get("window") or "day"), client=ctx.client)
            await record_edits.apply_booking_move(ctx.tenant_id, b, plan, client=ctx.client)
            done.append("moved")
    except record_edits.EditError as e:
        return RedirectResponse(f"{back}?msg=editerr&why={quote(str(e))}", status_code=303)
    if not done:
        return RedirectResponse(f"{back}?msg=nochange", status_code=303)
    from app import job_sheets   # a moved job's new details go to whoever's on it
    job_sheets.soon(ctx.tenant_id, booking_id)
    return RedirectResponse(f"{back}?msg={'+'.join(done)}", status_code=303)


@router.post("/crm/contacts/{contact_id}/edit", dependencies=[Depends(require_csrf)])
async def crm_contact_edit(contact_id: str, request: Request,
                           ctx: TenantCtx = Depends(require_onboarded)):
    """Name / email / phone by hand (Kris 2026-09-06: "no discreet way to
    manually change most stuff"). Same write the Concierge would make."""
    from app import record_edits
    back = f"/app/crm/contacts/{contact_id}"
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    require_crm_write(ctx, contact_id)
    form = await request.form()
    try:
        record_edits.edit_contact_basics(
            ctx.tenant_id, contact, name=form.get("name"), email=form.get("email"),
            phone=form.get("phone"), client=ctx.client)
    except record_edits.EditError as e:
        return RedirectResponse(f"{back}?msg=editerr&why={quote(str(e))}", status_code=303)
    return RedirectResponse(f"{back}?msg=edited", status_code=303)


@router.post("/crm/contacts/{contact_id}/fields", dependencies=[Depends(require_csrf)])
async def crm_contact_fields(contact_id: str, request: Request,
                             ctx: TenantCtx = Depends(require_onboarded)):
    """The About card's field values by hand: one input per contact field
    (form keys `f_<key>`); only the values that changed are written, each
    through record_edits.set_contact_field — the same function the Concierge's
    update_contact card runs on Confirm."""
    from app import record_edits
    require_crm_write(ctx, contact_id)
    back = f"/app/crm/contacts/{contact_id}"
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    form = await request.form()
    def _blocking():
        defs = [d for d in (db.crm_list_field_definitions(ctx.tenant_id, client=ctx.client) or [])
                if d.get("entity") == "contact" and not isinstance(d.get("ladder"), dict)]
        # A field they cannot see is a field they cannot write. Without this, the
        # form could post f_alarm_code and set what the page refused to show.
        defs = crm_visibility.visible_defs(defs, _me(ctx))
        data = contact.get("custom_data") or {}
        changed, errors = 0, []
        for d in defs:
            key = f"f_{d['key']}"
            if key not in form:
                continue
            raw = str(form.get(key) or "").strip()
            old = data.get(d["key"])
            if raw == ("" if old in (None, "") else str(old).strip()):
                continue
            try:
                record_edits.set_contact_field(ctx.tenant_id, contact, d, raw, client=ctx.client)
                changed += 1
            except record_edits.EditError as e:
                errors.append(str(e))
        if errors:
            return RedirectResponse(f"{back}?msg=fielderr&why={quote(' '.join(errors)[:300])}", status_code=303)
        return RedirectResponse(f"{back}?msg={'fields' if changed else 'nochange'}", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/contacts/{contact_id}/note", dependencies=[Depends(require_csrf)])
def crm_contact_note(contact_id: str, note: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    """A dated note on the customer's timeline, by hand."""
    from app import record_edits
    require_crm_write(ctx, contact_id)
    back = f"/app/crm/contacts/{contact_id}"
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        record_edits.add_contact_note(ctx.tenant_id, contact, note, client=ctx.client)
    except record_edits.EditError as e:
        return RedirectResponse(f"{back}?msg=editerr&why={quote(str(e))}", status_code=303)
    return RedirectResponse(f"{back}?msg=noted", status_code=303)


@router.post("/crm/contacts/{contact_id}/level", dependencies=[Depends(require_csrf)])
def crm_contact_level(contact_id: str, key: str = Form(...), level: str = Form(...),
                            ctx: TenantCtx = Depends(require_onboarded)):
    """A customer ladder moved by hand on the contact card (run #8, 2026-09-05:
    a participant IS the customer — no dependant, so the job page's ladder
    select never renders, and the About face was read-only). The write is the
    same one an accepted chip makes, so the trail and the level message
    (src/app/ladders.py contact_field_changed) follow exactly as for a dependant."""
    from app import ladders
    require_crm_write(ctx, contact_id)
    back = f"/app/crm/contacts/{contact_id}"
    contact = db.crm_get_contact(contact_id, ctx.tenant_id, client=ctx.client)
    if not contact:
        raise HTTPException(status_code=404, detail="No such contact")
    defs = db.crm_list_field_definitions(ctx.tenant_id, entity="contact", client=ctx.client)
    d = next((x for x in defs if x.get("key") == key and isinstance(x.get("ladder"), dict)), None)
    levels = ladders.clean_levels(d.get("options")) if d else []
    if not d or level not in levels:
        return RedirectResponse(back + "?msg=badlevel", status_code=303)
    old = (contact.get("custom_data") or {}).get(key)
    db.crm_merge_custom_data("contact", contact_id, ctx.tenant_id, {key: level}, client=ctx.client)
    fired = ladders.contact_field_changed(ctx.tenant_id, contact, key, old, level, client=ctx.client)
    notify = (d.get("ladder") or {}).get("notify") or "approve"
    msg = "level" if not fired or notify == "off" else ("level_sent" if notify == "auto" else "level_queued")
    return RedirectResponse(f"{back}?msg={msg}", status_code=303)


def _crm_annotate_suggestions(suggestions: list[dict], defs: list[dict],
                              records: dict | None = None) -> list[dict]:
    """Attach the human label to each pending suggestion (dropping any whose
    definition has since been deleted — nothing to confirm into), plus what
    accepting it would REPLACE.

    Since 2026-09-04 an extraction that contradicts a stored value queues here
    instead of overwriting, so a chip may now be a change rather than a filling
    of an empty slot. `replaces` is what the record says today: without it the
    operator is asked to approve a swap they cannot see. `records` maps entity →
    that record's custom_data."""
    labels = {(d["entity"], d["key"]): d["label"] for d in defs}
    out = []
    for s in suggestions:
        label = labels.get((s["entity"], s["field_key"]))
        if not label:
            continue
        stored = ((records or {}).get(s["entity"]) or {}).get(s["field_key"])
        replaces = str(stored).strip() if stored not in (None, "") else ""
        out.append({**s, "label": label,
                    "replaces": replaces if replaces != str(s.get("value") or "").strip() else ""})
    return out


@router.get("/crm/fields")
def crm_fields_page(request: Request, ctx: TenantCtx = Depends(require_crm)):
    """The fields wizard (design §9): what to track per entity, seeded by the
    tenant's industry, each field carrying the extraction hint the autofill
    reads with. Meant to be done in under 90 seconds."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    snapshot = setup_forms.load(ctx, "crm_fields")
    defs = sorted(snapshot["definitions"], key=lambda row: row.get("display_order") or 0)
    return templates.TemplateResponse(request, "app/crm_fields.html", _ctx(
        "crm_fields", ctx,
        by_entity={e: [d for d in defs if d["entity"] == e] for e in crm_fields.ENTITIES},
        seeds=crm_fields.seeds_for_tenant(t, defs),
        msg=request.query_params.get("msg"), err=request.query_params.get("err"),
        added=request.query_params.get("added"),
        preview=None, settings_snapshot=snapshot))


@router.post("/crm/fields", dependencies=[Depends(require_csrf)])
def crm_add_field(entity: str = Form(...), label: str = Form(...),
                  field_type: str = Form("text"), options: str = Form(""),
                  required: str = Form(""), extraction_hint: str = Form(""),
                  durability: str = Form("once"), recall_days: str = Form(""),
                  valid_months: str = Form(""),
                  probe_behaviour: str = Form("passive"),
                  settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_fields")
    require_crm_write(ctx)
    try:
        d = crm_fields.validate_definition(
            entity, label, field_type, [o for o in options.split(",") if o.strip()],
            extraction_hint, required=str(required).strip() in ("1", "on", "true"),
            # Certificates on contacts (slice 4): a date that expires, recalled N days ahead;
            # run #8: with a validity, when the date on record is the day it was issued.
            durability=durability, recall_days=recall_days, valid_months=valid_months,
            probe_behaviour=probe_behaviour)
        d = crm_fields.prepare_field(d, [r for r in expected["definitions"] if r["entity"] == entity])
        setup_forms.save(ctx, "crm_fields", expected, [setup_forms.operation("crm_field_definitions", d)])
    except crm_fields.FieldError as e:
        return RedirectResponse(f"/app/crm/fields?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/app/crm/fields?msg=saved", status_code=303)


@router.post("/crm/fields/preview", dependencies=[Depends(require_csrf)])
async def crm_fields_preview(request: Request, ctx: TenantCtx = Depends(require_crm)):
    """The sales moment (design §9): dry-run the extraction over the tenant's
    last 5 real emails and show what WOULD be filled in — nothing is written."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    snapshot = setup_forms.load(ctx, "crm_fields")
    defs = sorted(snapshot["definitions"], key=lambda row: row.get("display_order") or 0)
    previews = []
    if defs:
        for e in db.crm_recent_inbound(ctx.tenant_id, limit=5, client=ctx.client):
            try:
                extracted = await crm_extract.extract(
                    ctx.tenant_id, defs, subject=e.get("subject"), body=e.get("body_in"),
                    from_name=e.get("from_name"), email_log_id=None)
            except Exception:
                log.warning("CRM preview extraction failed", exc_info=True)
                continue
            plan = [p for p in crm_fields.plan_application(defs, extracted)
                    if p["action"] != "skip"]
            previews.append({"email": e, "plan": plan})
    return templates.TemplateResponse(request, "app/crm_fields.html", _ctx(
        "crm_fields", ctx,
        by_entity={e: [d for d in defs if d["entity"] == e] for e in crm_fields.ENTITIES},
        seeds=crm_fields.seeds_for_tenant(t, defs),
        msg=None, err=None, preview=previews, settings_snapshot=snapshot))


@router.post("/crm/fields/{def_id}", dependencies=[Depends(require_csrf)])
def crm_update_field(def_id: str, label: str = Form(...), extraction_hint: str = Form(""),
                     required: str = Form(""), durability: str = Form(""),
                     recall_days: str = Form(""), valid_months: str = Form(""),
                     staff_can_see: str = Form(""),
                     probe_behaviour: str = Form("passive"),
                     settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    """Edit the human-facing parts. The key (custom_data slot) and type stay
    stable — type migration is a later guardrail (design §11). A date field's
    expiry + recall (slice 4) are edited here too; posted only for date fields."""
    expected = setup_forms.parse(settings_snapshot, "crm_fields")
    require_crm_write(ctx)
    label = label.strip()
    if not label:
        return RedirectResponse("/app/crm/fields?err=" + quote("The field needs a name."),
                                status_code=303)
    if probe_behaviour not in crm_fields.PROBE_BEHAVIOURS:
        return RedirectResponse("/app/crm/fields?err=" + quote("Unknown probe behaviour."),
                                status_code=303)
    fields = {"label": label, "extraction_hint": extraction_hint.strip() or None,
              "probe_behaviour": probe_behaviour,
              "required": str(required).strip() in ("1", "on", "true"),
              # Whether staff see it, and the fact that a person has now decided
              # — after which the heuristic never touches it again.
              "staff_can_see": str(staff_can_see).strip() in ("1", "on", "true"),
              "staff_can_see_set": True}
    if durability:
        fields["durability"] = "expires" if durability == "expires" else "once"
        fields["recall_days"] = crm_fields.clean_recall(recall_days)
        fields["valid_months"] = crm_fields.clean_valid(valid_months) if fields["durability"] == "expires" else 0
    setup_forms.save(ctx, "crm_fields", expected, [setup_forms.operation("crm_field_definitions", fields, def_id)])
    return RedirectResponse("/app/crm/fields?msg=saved", status_code=303)


@router.post("/crm/fields/{def_id}/delete", dependencies=[Depends(require_csrf)])
def crm_delete_field(def_id: str, settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_fields")
    require_crm_write(ctx)
    setup_forms.save(ctx, "crm_fields", expected, [setup_forms.operation("crm_field_definitions", identity=def_id, delete=True)])
    return RedirectResponse("/app/crm/fields?msg=deleted", status_code=303)


def _held_reply_for(tenant_id: str, suggestion_id: str, client) -> str | None:
    """The email a dependant chip was read from — a document-held reply may be
    waiting on exactly this chip (pipeline.release_document_hold)."""
    try:
        s = db.crm_get_suggestion(suggestion_id, tenant_id, client=client) or {}
        return s.get("email_log_id") if s.get("entity") == "dependant" else None
    except Exception:  # noqa: BLE001
        return None


@router.post("/crm/documents/{doc_id}/updates", dependencies=[Depends(require_csrf)])
async def crm_document_updates(doc_id: str, request: Request, use: str = Form("1"),
                               return_to: str = Form("/app/crm"),
                               ctx: TenantCtx = Depends(require_crm)):
    """One decision for everything a document asks to change (Kris 2026-09-22:
    a certificate read three facts; confirming each separately took three taps
    to release one reply). `use=1` takes them all, `use=0` takes none; either
    way the reply waiting on the card is then written, once. Same permission
    as confirming one chip (crm_accept_suggestion)."""
    form = await request.form()
    def _blocking():
        ids = [str(x) for x in form.getlist("ids") if str(x).strip()][:20]
        held: set = set()
        for sid in ids:
            h = _held_reply_for(ctx.tenant_id, sid, ctx.client)
            if h:
                held.add(h)
            if use == "1":
                crm_fields.accept_suggestion(ctx.tenant_id, sid, client=ctx.client)
            else:
                crm_fields.dismiss_suggestion(ctx.tenant_id, sid, client=ctx.client)
        for h in held:
            _detach(pipeline.release_document_hold(ctx.tenant_id, h))
        return RedirectResponse(_safe_next(return_to), status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/crm/suggestions/{suggestion_id}/accept", dependencies=[Depends(require_csrf)])
def crm_accept_suggestion(suggestion_id: str, return_to: str = Form("/app/crm"),
                                ctx: TenantCtx = Depends(require_crm)):
    held = _held_reply_for(ctx.tenant_id, suggestion_id, ctx.client)
    crm_fields.accept_suggestion(ctx.tenant_id, suggestion_id, client=ctx.client)
    if held:
        # The owner just confirmed what the card says: write the reply that was
        # waiting for it — detached, so the tick answers at once (Kris 2026-09-22).
        _detach(pipeline.release_document_hold(ctx.tenant_id, held))
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.post("/crm/suggestions/{suggestion_id}/dismiss", dependencies=[Depends(require_csrf)])
def crm_dismiss_suggestion(suggestion_id: str, return_to: str = Form("/app/crm"),
                                 ctx: TenantCtx = Depends(require_crm)):
    held = _held_reply_for(ctx.tenant_id, suggestion_id, ctx.client)
    crm_fields.dismiss_suggestion(ctx.tenant_id, suggestion_id, client=ctx.client)
    if held:
        # Dismissed is decided too: the reply is written against the record as
        # it stands, and asks the customer again if the card did not settle it.
        _detach(pipeline.release_document_hold(ctx.tenant_id, held))
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.get("/crm/settings")
def crm_settings_page(request: Request, ctx: TenantCtx = Depends(require_crm)):
    """CRM settings — the self-serve face of what the onboarding interview asks
    about: pipeline stages, the gone-quiet threshold, and your-data exports.
    Fields keep their own tab (they're the daily surface; this is the infrastructure)."""
    crm.ensure_default_stages(ctx.tenant_id, client=ctx.client)
    snapshot = setup_forms.load(ctx, "crm_settings")
    return templates.TemplateResponse(request, "app/crm_settings.html", _ctx(
        "crm_settings", ctx,
        stages=sorted(snapshot["stages"], key=lambda row: row.get("display_order") or 0), settings_snapshot=snapshot,
        quiet_days=int(snapshot["quiet_days"] or crm.STALE_AFTER_DAYS),
        msg=request.query_params.get("msg"), err=request.query_params.get("err")))


@router.post("/crm/settings", dependencies=[Depends(require_csrf)])
def crm_save_settings(quiet_days: str = Form("14"),
                      settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_settings")
    try:
        days = max(1, min(int(quiet_days), 365))
    except ValueError:
        return RedirectResponse("/app/crm/settings?err=" +
                                quote("Quiet threshold needs to be a number of days."),
                                status_code=303)
    setup_forms.save(ctx, "crm_settings", expected, [setup_forms.operation("tenant_config", {"crm_quiet_days": days}, ctx.tenant_id)])
    _held_used(ctx, "crm_settings")
    return RedirectResponse("/app/crm/settings?msg=saved", status_code=303)


@router.post("/crm/settings/stages", dependencies=[Depends(require_csrf)])
def crm_settings_add_stage(name: str = Form(...), stage_type: str = Form("active"),
                           display_order: str = Form("0"),
                           settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_settings")
    name = name.strip()
    if not name or stage_type not in ("active", "won", "lost", "dormant"):
        return RedirectResponse("/app/crm/settings?err=" +
                                quote("A stage needs a name and a valid type."),
                                status_code=303)
    try:
        order = int(display_order)
    except ValueError:
        order = 0
    setup_forms.save(ctx, "crm_settings", expected, [setup_forms.operation("crm_stages",
        {"name": name, "display_order": order, "stage_type": stage_type})])
    return RedirectResponse("/app/crm/settings?msg=saved", status_code=303)


@router.post("/crm/settings/stages/{stage_id}", dependencies=[Depends(require_csrf)])
def crm_settings_update_stage(stage_id: str, name: str = Form(...),
                              stage_type: str = Form("active"),
                              display_order: str = Form("0"),
                              settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_settings")
    name = name.strip()
    if not name or stage_type not in ("active", "won", "lost", "dormant"):
        return RedirectResponse("/app/crm/settings?err=" +
                                quote("A stage needs a name and a valid type."),
                                status_code=303)
    try:
        order = int(display_order)
    except ValueError:
        order = 0
    setup_forms.save(ctx, "crm_settings", expected, [setup_forms.operation("crm_stages",
        {"name": name, "stage_type": stage_type, "display_order": order}, stage_id)])
    return RedirectResponse("/app/crm/settings?msg=saved", status_code=303)


@router.post("/crm/settings/stages/{stage_id}/delete", dependencies=[Depends(require_csrf)])
def crm_settings_delete_stage(stage_id: str, settings_snapshot: str = Form(""),
                  ctx: TenantCtx = Depends(require_crm)):
    expected = setup_forms.parse(settings_snapshot, "crm_settings")
    setup_forms.save(ctx, "crm_settings", expected, [setup_forms.operation("crm_stages", identity=stage_id, delete=True)])
    return RedirectResponse("/app/crm/settings?msg=deleted", status_code=303)


@router.post("/crm/deals/{deal_id}/stage", dependencies=[Depends(require_csrf)])
def crm_move_deal(deal_id: str, stage_id: str = Form(...),
                  ctx: TenantCtx = Depends(require_crm)):
    """Move a deal between stages (the pipeline's kanban action). The stage's
    type drives the deal's status: won/lost/dormant stamp closed_at, moving
    back to an active stage reopens it."""
    stage = next((s for s in db.crm_list_stages(ctx.tenant_id, client=ctx.client)
                  if s["id"] == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    stage_type = stage.get("stage_type") or "active"
    fields = {"stage_id": stage_id,
              "status": "open" if stage_type == "active" else stage_type,
              "closed_at": (None if stage_type == "active"
                            else datetime.now(timezone.utc).isoformat())}
    db.crm_update_deal(deal_id, ctx.tenant_id, fields, client=ctx.client)
    return RedirectResponse("/app/crm/pipeline", status_code=303)


@router.post("/crm/promises/{promise_id}/resolve", dependencies=[Depends(require_csrf)])
def crm_resolve_promise_route(promise_id: str, status: str = Form("done"),
                              return_to: str = Form("/app/crm"),
                              ctx: TenantCtx = Depends(require_crm)):
    """Done / Drop by hand, plus the two answers to a SUGGESTED resolution
    (Kris 2026-09-20): 'confirm' keeps the evidence + cleared-by link the
    suggestion carried; 'keep' puts the row back to plainly open. Every
    clearing writes its timeline line — best-effort, after the ledger."""
    if status not in ("done", "dropped", "confirm", "keep"):
        raise HTTPException(status_code=422, detail="Unknown status")
    if status == "keep":
        db.crm_keep_promise_open(promise_id, ctx.tenant_id, client=ctx.client)
        return RedirectResponse(_safe_next(return_to), status_code=303)
    if status == "confirm":
        db.crm_confirm_promise_resolved(promise_id, ctx.tenant_id, client=ctx.client)
    else:
        db.crm_resolve_promise(promise_id, ctx.tenant_id, status, client=ctx.client)
    try:
        crm.note_promise_cleared(
            ctx.tenant_id, db.crm_get_promise(promise_id, ctx.tenant_id, client=ctx.client))
    except Exception:
        log.warning("promise-cleared timeline line failed for %s", promise_id, exc_info=True)
    return RedirectResponse(_safe_next(return_to), status_code=303)


@router.get("/crm/export.json")
def crm_export_json(ctx: TenantCtx = Depends(require_crm)):
    """The whole CRM as one JSON download — "yours to export" (design §14)."""
    data = db.crm_export(ctx.tenant_id, client=ctx.client)
    return Response(json.dumps(data, indent=2, default=str),
                    media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=blam8-crm.json"})


@router.get("/crm/export.csv")
def crm_export_csv(ctx: TenantCtx = Depends(require_crm)):
    """Contacts as CSV (the spreadsheet-shaped half of the export promise)."""
    _guards.require_admin(ctx)   # whole-business export: owner or manager (M4)
    rows = db.crm_list_contacts(ctx.tenant_id, limit=200, offset=0, client=ctx.client)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "company", "first_seen", "last_activity", "custom_data"])
    for c in rows:
        comp = (c.get("crm_companies") or {}).get("name") or ""
        w.writerow([c.get("name") or "", c["email"], comp,
                    (c.get("first_seen_at") or "")[:10],
                    (c.get("last_activity_at") or "")[:10],
                    json.dumps(c.get("custom_data") or {})])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=blam8-contacts.csv"})


@router.get("/crm/reports")
def crm_reports_page(request: Request, period: int = 30,
                     ctx: TenantCtx = Depends(require_crm)):
    """Phase 4a §19.1: reporting dashboards — pipeline, activity, won/lost."""
    period = max(7, min(int(period), 365))
    pipeline_data = db.crm_report_pipeline(ctx.tenant_id, client=ctx.client)
    activity_data = db.crm_report_activity(ctx.tenant_id, period=period, client=ctx.client)
    won_lost_data = db.crm_report_won_lost(ctx.tenant_id, period=period, client=ctx.client)
    return templates.TemplateResponse(request, "app/crm_reports.html",
                                      _ctx("crm_reports", ctx, pipeline=pipeline_data,
                                           activity=activity_data, won_lost=won_lost_data,
                                           period=period))


@router.get("/crm/tasks")
def crm_tasks_page(request: Request, status: str = "open",
                   ctx: TenantCtx = Depends(require_crm)):
    """Phase 4a §19.2: task management page."""
    if status not in ("open", "done", "cancelled", "all"):
        status = "open"
    tasks = db.crm_list_tasks(ctx.tenant_id, status=status, client=ctx.client)
    from datetime import date as _date
    return templates.TemplateResponse(request, "app/crm_tasks.html",
                                      _ctx("crm_tasks", ctx, tasks=tasks,
                                           current_status=status,
                                           today=_date.today().isoformat()))


@router.get("/crm/sequences")
def crm_sequences_page(request: Request, ctx: TenantCtx = Depends(require_crm)):
    """Phase 4b §19.3: sequence list + builder."""
    sequences = db.crm_list_sequences(ctx.tenant_id, client=ctx.client)
    for seq in sequences:
        seq["steps"] = db.crm_list_steps(seq["id"], ctx.tenant_id, client=ctx.client)
    stages = db.crm_list_stages(ctx.tenant_id, client=ctx.client)
    from app.crm_sequences import ACTION_TYPES, MAX_ACTIVE_SEQUENCES, MAX_STEPS, TRIGGER_TYPES
    active_count = sum(1 for s in sequences if s.get("status") == "active")
    return templates.TemplateResponse(
        request, "app/crm_sequences.html",
        _ctx("crm_sequences", ctx, sequences=sequences, stages=stages,
             trigger_types=TRIGGER_TYPES, action_types=ACTION_TYPES,
             max_steps=MAX_STEPS, max_active=MAX_ACTIVE_SEQUENCES,
             active_count=active_count))


# ── Usage (customer-facing — value units only; never cost/tokens/model) ──────
def _mailbox_usage(rows: list[dict], boxes: list[dict]) -> list[dict]:
    """Per-mailbox handled/sent counts from email_log rows (the spec's
    'derive via email_log join — no usage_events change'). Legacy null stamps
    count toward the default (oldest) mailbox; a stamp whose mailbox has been
    disconnected keeps its history under a tombstone row."""
    default_id = boxes[0]["id"] if boxes else None
    by_box: dict = {}
    for r in rows:
        key = r.get("mailbox_id") or default_id
        b = by_box.setdefault(key, {"handled": 0, "sent": 0})
        b["handled"] += 1
        if r.get("status") == "sent":
            b["sent"] += 1
    known = {m["id"]: m for m in boxes}
    out = []
    for key, counts in by_box.items():
        m = known.get(key)
        out.append({
            "display": _mailbox_display(m) if m else "(disconnected mailbox)",
            "address": (m or {}).get("address"),
            "department": (m or {}).get("department"),
            **counts,
        })
    return sorted(out, key=lambda r: r["handled"], reverse=True)


@router.get("/usage")
def usage_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    summary = usage.summarize(ctx.tenant_id, client=ctx.client)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    enterprise = (t.get("reporting_tier") == "enterprise")
    status_counts: dict[str, int] = {}
    mailbox_usage: list[dict] = []
    if enterprise:
        rows = db.list_email_log_for_period(ctx.tenant_id, summary["period_start"].isoformat(), client=ctx.client)
        for r in rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        boxes = _tenant_mailboxes(ctx)
        if len(boxes) > 1:
            mailbox_usage = _mailbox_usage(rows, boxes)
    return templates.TemplateResponse(request, "app/usage.html", _ctx(
        "usage", ctx, summary=summary, enterprise=enterprise, status_counts=status_counts,
        mailbox_usage=mailbox_usage,
        # Two conditions, and the second one matters more than it looks. No price
        # id = nothing for sale, the rule every add-on uses. But ALSO: under soft
        # enforcement claim_draft never spends credit, so a top-up bought now
        # could never be used — £39 for nothing. The button appears when the
        # allowance is actually enforced and not a moment before.
        topup_on=bool(plans.topup_price_id()) and settings.usage_enforcement == "hard",
        topup_emails=plans.topup_emails(), topup_gbp=plans.topup_gbp(),
        enforcement=settings.usage_enforcement,
    ))


# ── Agent log (transparency — the burger-menu activity feed) ─────────────────
# A curated, human-readable view of what the agents DID: drafted a reply, asked the
# transport desk (the hand-off), held a booking. The date filter is the CALENDAR date
# an action concerns, so a diary entry can be traced back to its cause.
AGENT_LOG_KINDS = (
    ("", "All activity"),
    ("drafted", "Replies drafted"),
    ("transport", "Transport hand-offs"),
    ("field_service", "Field-service hand-offs"),
    ("office_booking", "Appointments held"),
    ("booking_confirmed", "Bookings confirmed"),
    ("suppression", "Draft pauses"),
    ("reminder", "Reminders"),
    ("sent", "Replies/reminders sent"),
    ("sentiment", "Mood flags"),
    ("portal", "Customer portal"),
)
# 'portal' is a GROUP filter: everything the customer portal writes, in one
# pill — these kinds only appeared under "All activity" until 2026-07-31.
PORTAL_EVENT_KINDS = ("booked", "booking_moved", "reschedule_requested",
                      "escalated", "portal_verification")
_AGENT_LOG_KIND_SET = {k for k, _ in AGENT_LOG_KINDS if k}
AGENT_LOG_PAGE = 60


_TASKS_MARK = re.compile(r"\s*\[Tasks: ([0-9a-f-]{36})\]\s*$")


def _attach_task_lists(ctx: TenantCtx, history: list[dict]) -> None:
    from app.concierge_history import attach_task_lists
    attach_task_lists(ctx, history)


@instrument("panel_context")
def _concierge_panel_ctx(ctx: TenantCtx, timings: list | None = None) -> dict:
    """What app/_concierge_panel.html needs, for both of its frames."""
    started = perf_counter()
    def timed(name):
        nonlocal started
        at = perf_counter()
        if timings is not None:
            timings.append(f"{name};dur={(at - started) * 1000:.1f}")
        started = at
    try:
        history = db.list_concierge_messages(
            ctx.tenant_id, user_id=ctx.user_id,
            include_shared=_guards.sees_business_wide(ctx), client=ctx.client)
    except Exception:
        history = []   # table missing / read hiccup — the chat still works
    timed("history")
    full_qa = "crm" in db.visible_modules(ctx.tenant_id, client=ctx.client)
    timed("modules")
    _attach_task_lists(ctx, history)
    timed("tasks")
    # The brief's bubble carries a small cog (Kris 2026-09-25): what the brief
    # tells you, changed where you read it. On the NEWEST brief only, and only
    # for someone who may change settings. (The "Yes please" chip that used to
    # answer her closing offer went with the offer itself.)
    brief_at = next((i for i in range(len(history) - 1, -1, -1)
                     if history[i].get("role") == "assistant"
                     and debrief.is_brief(history[i].get("content"))), None)
    brief_cog = None
    if brief_at is not None and people.is_admin(_me(ctx)):
        try:
            on = debrief.topics_on(db.get_tenant_config(ctx.tenant_id, client=ctx.client))
        except Exception:
            on = debrief.DEFAULT_TOPICS
        brief_cog = {"at": brief_at, "topics": [
            {"key": k, "label": label, "help": help_, "on": k in on}
            for k, label, help_, _d in debrief.TOPICS]}
    timed("brief")
    # The Debrief button (src/app/debrief_walk.py): owner and managers, never while
    # viewing as someone else — the walk crosses every door.
    return dict(chat_history=history, full_qa=full_qa, brief_cog=brief_cog,
                voice_user_id=ctx.user_id,
                debrief_on=bool(people.is_admin(_me(ctx))) and not ctx.viewing_as)


@router.get("/concierge")
def concierge_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The Concierge — Q&A over the tenant's own data (inbox, CRM, diary,
    knowledge) plus actions behind a Confirm card. The page renders the saved
    transcript; asking happens over POST /concierge/business (concierge_routes),
    which streams the lookups live. full_qa mirrors the endpoint's tier gate so
    the suggestion chips match what the tools can actually answer. Since
    2026-09-23 the masthead's person icon opens the same panel as a modal
    (/concierge/panel); this page stays for deep links."""
    if not has_concierge(ctx.tenant_id, client=ctx.client):
        return RedirectResponse("/billing/plans?concierge=1", status_code=303)
    return templates.TemplateResponse(request, "app/concierge.html", _ctx(
        "concierge", ctx, **_concierge_panel_ctx(ctx)))


@router.get("/concierge/panel")
@instrument("panel")
def concierge_panel(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The Concierge panel alone, for the modal (static/concierge_panel.js)."""
    started = perf_counter()
    if not has_concierge(ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=403, detail="The Concierge is part of Pro + Concierge.")
    timings = [f"access;dur={(perf_counter() - started) * 1000:.1f}"]
    context = _concierge_panel_ctx(ctx, timings)
    started = perf_counter()
    response = templates.TemplateResponse(request, "app/_concierge_panel.html", context)
    timings.append(f"template;dur={(perf_counter() - started) * 1000:.1f}")
    response.headers["Server-Timing"] = ", ".join(timings)
    return response


@router.get("/agent-log")
def agent_log_page(request: Request, date: str = "", kind: str = "", q: str = "",
                   who: str = "", page: int = 0,
                   ctx: TenantCtx = Depends(require_onboarded)):
    """The Agent Log: a chronological feed of agent activity, filterable by the calendar
    date an action concerns, by kind, by a contact/subject search, or by WHO did it.
    Reads with the tenant's JWT client so RLS double-guards isolation."""
    on_date = (date or "").strip() or None
    kind = (kind or "").strip()
    kind = kind if kind in _AGENT_LOG_KIND_SET else ""
    q = (q or "").strip()
    page = max(0, page)
    # Who: a user id from this business's own people, or 'blam8' for what the
    # agent did on its own. Checked against the membership list rather than
    # trusted from the query string, so the filter can never be used to probe
    # for ids that belong to somebody else.
    hands = _agent_log_hands(ctx)
    who = (who or "").strip()
    who = who if (who == "blam8" or who in {h["key"] for h in hands}) else ""
    kind_filter = list(PORTAL_EVENT_KINDS) if kind == "portal" else (kind or None)
    rows = db.list_agent_events(
        ctx.tenant_id, on_date=on_date, kind=kind_filter, q=q or None, who=who or None,
        limit=AGENT_LOG_PAGE + 1, offset=page * AGENT_LOG_PAGE, client=ctx.client)
    has_more = len(rows) > AGENT_LOG_PAGE
    rows = rows[:AGENT_LOG_PAGE]
    # Group by the day the action happened, for readable day headers.
    groups: list[dict] = []
    for r in rows:
        day = (r.get("created_at") or "")[:10]
        if not groups or groups[-1]["day"] != day:
            groups.append({"day": day, "rows": []})
        groups[-1]["rows"].append(r)
    return templates.TemplateResponse(request, "app/agent_log.html", _ctx(
        "agent_log", ctx, groups=groups, kinds=AGENT_LOG_KINDS, sel_kind=kind,
        on_date=on_date or "", q=q, page=page, has_more=has_more,
        hands=hands, sel_who=who))


def _agent_log_hands(ctx: TenantCtx) -> list[dict]:
    """Who the log can be read by. Only shown once there IS more than one pair
    of hands — a one-person business asking itself "who did this" is noise."""
    try:
        members = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    except Exception:
        log.warning("people read failed for the log (tenant=%s)", ctx.tenant_id, exc_info=True)
        return []
    if len(members) < 2:
        return []
    return [{"key": m["user_id"], "label": people.name_of(m)} for m in members if m["user_id"]]


@router.get("/usage/export.csv")
def usage_export(ctx: TenantCtx = Depends(require_onboarded)):
    """Enterprise reporting export — the tenant's own handled-emails log. Value /
    governance fields only (date, sender, subject, status, mailbox); never tokens
    or cost."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if t.get("reporting_tier") != "enterprise":
        raise HTTPException(status_code=404, detail="Not available on this plan")
    summary = usage.summarize(ctx.tenant_id, client=ctx.client)
    rows = db.list_email_log_for_period(ctx.tenant_id, summary["period_start"].isoformat(), client=ctx.client)
    boxes = db.list_mailboxes(ctx.tenant_id, client=ctx.client)
    default_id = boxes[0]["id"] if boxes else None
    addr = {m["id"]: m.get("address") or _mailbox_display(m) for m in boxes}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["received_at", "from", "subject", "status", "mailbox"])
    for r in rows:
        w.writerow([r.get("created_at"), r.get("from_address"), r.get("subject"), r.get("status"),
                    addr.get(r.get("mailbox_id") or default_id, "")])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="blam8-emails-handled.csv"'})


# ── Mailboxes (enterprise multi-mailbox — the per-mailbox brain's surface) ───
# All gated on the Account type: a standard tenant 404s here and sees today's
# single-mailbox UI untouched.
def _require_enterprise(ctx: TenantCtx) -> None:
    if not db.is_enterprise(ctx.tenant_id, client=ctx.client):
        raise HTTPException(status_code=404, detail="Not available on this plan")


def _require_multimailbox(ctx: TenantCtx) -> None:
    """Gate the multi-mailbox UI. Available to enterprise (unlimited mailboxes)
    and any tenant on an active paid plan — extra mailboxes beyond the plan's
    included allowance bill as the £39/mo add-on. A pilot with no active
    subscription stays single-mailbox and 404s here."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if db.is_enterprise(t) or billing.has_active_subscription(t):
        return
    raise HTTPException(status_code=404, detail="Not available on this plan")


def _mailbox_or_404(ctx: TenantCtx, mailbox_id: str) -> dict:
    box = next((m for m in _tenant_mailboxes(ctx) if m["id"] == mailbox_id), None)
    if box is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    return box


# The persona fields a mailbox may override (null = inherit), plus its one-line
# role framing. Mirrors db.MAILBOX_OVERRIDE_FIELDS minus auto_send (a tri-state
# select below, not a text field).
MAILBOX_PERSONA_FIELDS = (
    "purpose", "tone", "agent_name", "sign_off",
    "do_list", "never_do", "escalation", "custom_instructions", "examples",
)
# D6: categories are free text; these just seed the datalist in the form.
MAILBOX_CATEGORY_SUGGESTIONS = ("info", "orders", "support", "invoicing", "accounts", "dispatch")


@router.get("/mailboxes")
def mailboxes_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Every connected mailbox, grouped by department (D6 — labels in v1)."""
    _require_multimailbox(ctx)
    boxes = _tenant_mailboxes(ctx)
    for i, m in enumerate(boxes):
        m["is_default"] = i == 0
        m["auto_draft"] = (m.get("provider") == "google") or _sub_active(m.get("sub_expires"))
    groups: dict[str, list] = {}
    for m in boxes:
        groups.setdefault(m.get("department") or "", []).append(m)
    # named departments alphabetically, un-departmented last
    grouped = sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0].lower()))
    return templates.TemplateResponse(request, "app/mailboxes.html", _ctx(
        "mailboxes", ctx, grouped=grouped, count=len(boxes),
        msg=request.query_params.get("msg"),
        has_gmail=any(m.get("provider") == "google" for m in boxes),
    ))


@router.get("/mailboxes/add")
def mailbox_add_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The price, before the consent screen (Kris 2026-09-01).

    "+ Add Outlook" used to link straight to the provider, and the extra-mailbox
    add-on appeared on the way back — the customer's first sight of the £39 was
    their next invoice. Every route to connecting an ADDITIONAL mailbox now lands
    here first, so the number is on screen before anyone signs into anything.

    Declared above /mailboxes/{mailbox_id}/... so 'add' is never read as an id."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    _require_multimailbox(ctx)
    boxes = _tenant_mailboxes(ctx)
    return templates.TemplateResponse(request, "app/mailbox_add.html", _ctx(
        "mailboxes", ctx,
        quote=billing.mailbox_addon_quote(ctx.tenant_id, client=ctx.client),
        has_gmail=any(m.get("provider") == "google" for m in boxes),
    ))


@router.get("/mailboxes/{mailbox_id}/settings")
def mailbox_settings_page(request: Request, mailbox_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """One mailbox's brain: its labels, its role line, and its persona overrides.
    Every blank field inherits the company-wide setting (shown as the placeholder).
    With the fleet module on, the mailbox also picks its default branch (fleet D4:
    orders@leeds books Leeds vans)."""
    _require_multimailbox(ctx)
    box = _mailbox_or_404(ctx, mailbox_id)
    mc = db.get_mailbox_config(mailbox_id, client=ctx.client) or {}
    tenant_cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    tms_on = db.module_ui_visible(ctx.tenant_id, "tms", client=ctx.client)
    return templates.TemplateResponse(request, "app/mailbox_settings.html", _ctx(
        "mailboxes", ctx, box=box, mc=mc, tenant_cfg=tenant_cfg,
        category_suggestions=MAILBOX_CATEGORY_SUGGESTIONS,
        fleet_branches=db.list_branches(ctx.tenant_id, client=ctx.client) if tms_on else [],
        saved=request.query_params.get("saved") == "1",
    ))


@router.post("/mailboxes/{mailbox_id}/settings", dependencies=[Depends(require_csrf)])
async def save_mailbox_settings(request: Request, mailbox_id: str,
                                ctx: TenantCtx = Depends(require_onboarded)):
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    _require_multimailbox(ctx)
    _mailbox_or_404(ctx, mailbox_id)
    form = await request.form()
    def _blocking():
        clean = lambda name: (form.get(name) or "").strip() or None
        db.update_mailbox_meta(mailbox_id, ctx.tenant_id,
                               clean("label"), clean("department"), clean("category"),
                               client=ctx.client)
        cfg = {f: clean(f) for f in MAILBOX_PERSONA_FIELDS}
        # Same canonical sign-off as the company setting; a blank stays None (inherit).
        cfg["sign_off"] = sign_off.normalise(cfg.get("sign_off")) or None
        # Sending mode is a tri-state: inherit the company setting, or pin this
        # mailbox to full-auto / draft-only (D3 — orders@ full-auto, complaints@ not).
        sending = form.get("sending")
        cfg["auto_send"] = True if sending == "auto" else False if sending == "draft" else None
        # Fleet D4: this mailbox's default branch — only a branch the tenant owns;
        # blank (or a stale id) clears the default. Saved only when fleet is on, so
        # a non-fleet save can never null a column it didn't render.
        if db.module_ui_visible(ctx.tenant_id, "tms", client=ctx.client):
            cfg["default_branch_id"] = _own_branch_or_none(ctx, form.get("default_branch_id"))
        db.upsert_mailbox_config(mailbox_id, ctx.tenant_id, cfg, client=ctx.client)
        return RedirectResponse(f"/app/mailboxes/{mailbox_id}/settings?saved=1", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/mailboxes/{mailbox_id}/disconnect", dependencies=[Depends(require_csrf)])
async def disconnect_mailbox(mailbox_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Disconnect ONE mailbox — its siblings keep running (the delete is by
    connection id, never by tenant). Its mailbox_config and scoped knowledge go
    with it; its email_log history stays."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    _require_multimailbox(ctx)
    _mailbox_or_404(ctx, mailbox_id)
    conn = db.get_connection_by_id(mailbox_id, ctx.tenant_id)
    if conn:
        await oauth._delete_graph_subscription(conn, ctx.tenant_id)
    def _blocking():
        db.delete_connection_by_id(mailbox_id, ctx.tenant_id)
        db.sync_tenant_mailbox_mirror(ctx.tenant_id)
        # Removing a mailbox may drop the tenant back under its included allowance —
        # reconcile the add-on down. Best-effort; no-op for pilots/enterprise.
        try:
            billing.sync_mailbox_addon(ctx.tenant_id)
        except Exception:
            logging.getLogger("blautom8.dashboard").warning("mailbox add-on sync failed", exc_info=True)
        return RedirectResponse("/app/mailboxes?msg=disconnected", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/mailboxes/{mailbox_id}/resubscribe", dependencies=[Depends(require_csrf)])
async def resubscribe_mailbox(mailbox_id: str, ctx: TenantCtx = Depends(require_onboarded)):
    """Re-enable auto-draft for one Microsoft mailbox (rebuilds its Graph watch
    from the saved token — Gmail mailboxes poll and need no subscription)."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    _require_multimailbox(ctx)
    _mailbox_or_404(ctx, mailbox_id)
    try:
        ok = await oauth._create_subscription_for(ctx.tenant_id, connection_id=mailbox_id)
    except Exception:
        ok = False
    return RedirectResponse(f"/app/mailboxes?msg={'resubscribed' if ok else 'suberror'}", status_code=303)


# ── Settings (the agent's voice, rules & identity — the tenant layer) ────────
# The fields a tenant edits, in render order. build_system_prompt() consumes the
# same set, so adding a field here + in the template + in tenant_config is all it
# takes to extend what a tenant can configure.
# `examples` is deliberately NOT here (Kris 2026-09-01: "Remove the Examples
# part completely"). It is still WRITTEN by voice-apply from the learned sample
# and still read into the prompt by agent.build_system_prompt — it just stopped
# being something anyone pastes by hand. Leaving it in this tuple would have
# been the bug: the form no longer posts it, so every save would write None over
# whatever the learner had put there.
SETTINGS_FIELDS = (
    "business_brief", "tone", "agent_name", "sign_off", "voice_notes",
    "do_list", "never_do", "escalation", "custom_instructions", "alert_words",
)


def _sub_active(sub_expires: str | None) -> bool:
    """True if the Graph subscription expiry is in the future (auto-draft live)."""
    if not sub_expires:
        return False
    try:
        exp = datetime.fromisoformat(sub_expires)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return False


def _inbox_rules_ctx(cfg: dict) -> dict:
    """Settings › Automated mail: the three switches and a one-line face."""
    from app import inbox_rules
    on = inbox_rules.settings_of(cfg)
    n = sum(1 for v in on.values() if v)
    total = len(on)
    if n == total:
        summary = "No-reply, bulk and auto-generated mail filed, never answered"
    elif n == 0:
        summary = "Off — every email is treated as a person's"
    else:
        summary = ", ".join(inbox_rules.RULES[k]["label"].lower() for k, v in on.items() if v) + " filed, never answered"
    return {"inbox_rules_all": list(inbox_rules.RULES.items()), "inbox_rules_on": on,
            "inbox_rules_summary": summary}


@router.get("/settings")
def settings_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    status = db.get_connection_status(ctx.tenant_id, client=ctx.client)
    sched_cfg = None
    mods = db.visible_modules(ctx.tenant_id, client=ctx.client)
    if "scheduling" in mods or "tms" in mods:
        sched_cfg = scheduling._merged(db.get_scheduling_config(ctx.tenant_id, client=ctx.client))
    # Customer Portal: the slug is minted lazily the first time the owner
    # opens Settings — every business gets a portal address without a click.
    try:
        portal_slug = t.get("portal_slug") or portal.ensure_portal_slug(
            ctx.tenant_id, t.get("name"))
    except Exception:
        portal_slug = t.get("portal_slug")
    # People's face on the Settings row: the state, without opening the page.
    people_face = "Just you"
    try:
        rows = [people.member(r) for r in db.list_members(ctx.tenant_id)]
        live = [r for r in rows if r["active"]]
        waiting = len([i for i in db.list_member_invites(ctx.tenant_id) if people.usable(i)])
        if len(live) > 1 or waiting:
            people_face = f"{len(live)} {'person' if len(live) == 1 else 'people'}"
            if waiting:
                people_face += f" · {waiting} invitation{'' if waiting == 1 else 's'} out"
    except Exception:
        people_face = "Invite your team"
    from app import backlog
    return templates.TemplateResponse(request, "app/settings.html", _ctx(
        "settings", ctx, cfg=cfg, people_face=people_face,
        backlog_status=backlog.default_status(ctx.tenant_id, client=ctx.client),
        trial=trial.evaluate(t),
        gate_on=settings.require_subscription_gate,
        fleet_mode="tms" in mods and "scheduling" not in mods,
        saved=request.query_params.get("saved") == "1",
        # The five families in a fixed order, plus the one line the closed face
        # reads back (src/app/notify_prefs.py owns both).
        notify_families=list(notify_prefs.FAMILIES.items()),
        # What the brief tells you (2026-09-25): the same list as the brief's cog.
        brief_topics=[{"key": k, "label": label, "help": help_,
                       "on": k in debrief.topics_on(cfg)}
                      for k, label, help_, _d in debrief.TOPICS],
        # Which settings each family actually offers — Problems refuse "never".
        notify_modes={col: notify_prefs.modes_for(col)
                      for col in notify_prefs.FAMILIES},
        # The two per-kind opt-ins, already defaulted — the template must not
        # keep its own copy of which one is on when unset (2026-09-04).
        notify_optin_on={col: not notify_prefs.opted_out(cfg, kind)
                         for kind, (col, _d) in notify_prefs.OPT_INS.items()},
        notify_summary=notify_prefs.summary(cfg),
        concierge_limits=concierge_settings.resolved(None),
        concierge_chosen=concierge_settings.chosen(t),
        concierge_summary=concierge_settings.summary(t),
        # None when the channel is off OR the address is the monitored mailbox
        # — the template says which, because "saved but not sending" is the
        # one state a settings page must never show silently.
        notify_email_live=notify_prefs.email_target(cfg, t.get("mailbox")),
        # Today's eight topics, listed in the order this tenant will actually
        # meet them — the row shows the consequence of the choice, not an
        # alphabet the reader has to simulate in their head.
        deck_topics=deck.ordered_topics(cfg),
        deck_places=deck.placements(cfg),
        deck_summary=deck.summary(cfg),
        **_inbox_rules_ctx(cfg),
        portal_slug=portal_slug,
        portal_saved=request.query_params.get("portal") == "saved",
        portal_msg=request.query_params.get("portal"),
        portal_banner_url=storage.banner_public_url(cfg.get("portal_banner_path")),
        connected=bool(status),
        provider=t.get("provider") or "microsoft",
        mailbox=t.get("mailbox"),
        mailbox_count=len(db.list_mailboxes(ctx.tenant_id, client=ctx.client)),
        # Settings signposts the add-on at its headline price; the exact
        # arithmetic against this tenant's plan happens on the confirm step
        # (/app/mailboxes/add), which is where the money is actually committed.
        extra_mailbox_gbp=plans.extra_mailbox_gbp(),
        extra_mailbox_emails=usage.EXTRA_MAILBOX_EMAILS,
        auto_draft_active=_sub_active(status.get("sub_expires") if status else None),
        mailbox_msg=request.query_params.get("mailbox"),
        voice_msg=request.query_params.get("voice"),
        scheduling_cfg=sched_cfg,
        sched_saved=request.query_params.get("sched") == "saved",
        **_depth_ctx(sched_cfg),
        crm_stages=_crm_stage_names(ctx),
    ))


@router.post("/settings", dependencies=[Depends(require_csrf)])
async def save_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    form = await request.form()
    def _blocking():
        cfg = {field: (form.get(field) or "").strip() or None for field in SETTINGS_FIELDS}
        # One canonical form for the sign-off (a textarea posts CRLF): the same
        # value agent.py and the Concierge cards append (src/app/sign_off.py).
        cfg["sign_off"] = sign_off.normalise(cfg.get("sign_off")) or None
        # The sending ladder (Kris 2026-09-01): hold → review → full-auto, posted as
        # one radio so the rungs stay ordered and mutually exclusive.
        #
        # The default when the field is ABSENT matters more than it looks. This form
        # is the only thing that posts send_mode, but a stale tab, a replayed POST or
        # any future partial save would arrive without it — and defaulting to 'hold'
        # there would silently stop a working agent drafting. So an absent field
        # means "leave the ladder alone": read the tenant's current rung and write it
        # back unchanged.
        _existing_cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        _current = ("hold" if _existing_cfg.get("draft_hold")
                    else "auto" if _existing_cfg.get("auto_send") else "review")
        mode = form.get("send_mode") or _current
        if mode not in ("hold", "review", "auto"):
            mode = _current
        cfg["draft_hold"] = mode == "hold"
        cfg["auto_send"] = mode == "auto"
        # EU AI Act Art 50 disclosure on auto-sent replies — default ON; the settings
        # page always renders this toggle, so absent = deliberately unticked.
        cfg["ai_disclosure"] = form.get("ai_disclosure") is not None
        # Automated mail by kind (src/app/inbox_rules.py): written only when the form
        # carried the switches — an unticked box posts nothing, and a form without
        # the fold must never wipe them.
        if form.get("inbox_rules_present"):
            from app import inbox_rules
            cfg["inbox_ignore"] = {key: form.get("ignore_" + key) is not None for key in inbox_rules.RULES}
        # Planner auto-apply (transport-planner D6): saved ONLY when the planner
        # surface rendered the toggle (routing on) — a non-routing tenant's save can
        # never flip a setting it didn't show.
        if "tms" in db.visible_modules(ctx.tenant_id, client=ctx.client):
            cfg["planner_auto_apply"] = form.get("planner_auto_apply") is not None
        # A hand-edit on top of an auto-learned voice = hybrid provenance (voice
        # auto-learning, 2026-07-25) — but only when a voice field actually changed;
        # a save that touched nothing voice-shaped keeps the auto_learned label.
        existing = _existing_cfg
        if existing.get("voice_profile_source") == "auto_learned":
            voice_keys = ("tone", "agent_name", "sign_off", "voice_notes")
            def _voice_value(values, key):
                # The sign-off is compared in its canonical form, so saving an
                # old CRLF value unchanged does not re-label the provenance.
                return sign_off.normalise(values.get(key)) if key == "sign_off" else (values.get(key) or "")
            if any(_voice_value(cfg, k) != _voice_value(existing, k) for k in voice_keys):
                cfg["voice_profile_source"] = "hybrid"
        db.upsert_tenant_config(ctx.tenant_id, cfg, client=ctx.client)
        return RedirectResponse("/app/settings?saved=1", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/settings/concierge", dependencies=[Depends(require_csrf)])
async def save_concierge_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Its own form (the portal-form pattern). Blank fields fall back to the
    server default; every value is clamped by src/app/concierge_settings.py."""
    form = await request.form()
    def _blocking():
        db.set_concierge_settings(ctx.tenant_id, concierge_settings.parse_form(form), client=ctx.client)
        return RedirectResponse("/app/settings?saved=1", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/settings/portal", dependencies=[Depends(require_csrf)])
async def save_portal_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Customer Portal settings — its own form so the big voice/config form and
    this one can't clobber each other's checkbox semantics."""
    form = await request.form()
    cfg = {
        "portal_enabled": form.get("portal_enabled") is not None,
        "portal_show_prices": form.get("portal_show_prices") is not None,
        # 'auto' (default) applies customer reschedules on the spot; 'approve'
        # turns them into needs-attention inbox requests the owner confirms.
        "portal_reschedule_mode": ("approve"
                                   if form.get("portal_reschedule_mode") == "approve"
                                   else "auto"),
        # Self-serve bookings email the customer a confirmation (ref + portal
        # link). Default ON — the checkbox posts nothing when unticked.
        "portal_booking_confirmation": form.get("portal_booking_confirmation") is not None,
        # The customer's own log (src/app/customer_log.py, slice 5): a customer who IS
        # the participant may add notes to their own bookings. Default ON; never
        # for a dependant's booking whatever this says.
        "portal_customer_notes": form.get("portal_customer_notes") is not None,
        # Most upcoming bookings one customer can hold via the portal at once
        # (0/blank = no limit).
        "portal_max_active_bookings": (
            max(0, min(99, int(form.get("portal_max_active_bookings"))))
            if str(form.get("portal_max_active_bookings") or "").strip().isdigit()
            else 0),
        "portal_description": (form.get("portal_description") or "").strip()[:200] or None,
        "portal_service_area": (form.get("portal_service_area") or "").strip()[:120] or None,
        "portal_welcome": (form.get("portal_welcome") or "").strip()[:300] or None,
    }
    # Banner: an uploaded file (magic bytes decide, never the declared type) or
    # the remove tick. A bad image still saves everything above — the error
    # redirect only concerns the banner, the form mustn't be eaten.
    banner_err = None
    banner = form.get("portal_banner")
    if isinstance(banner, FormUploadFile) and banner.filename:
        data = await banner.read()
        if data:
            sniffed = storage.sniff_banner(data)
            if not sniffed:
                banner_err = "banner_invalid"
            elif len(data) > storage.MAX_BANNER_BYTES:
                banner_err = "banner_too_big"
            else:
                old = (db.get_tenant_config(ctx.tenant_id, client=ctx.client)
                       or {}).get("portal_banner_path")
                try:
                    cfg["portal_banner_path"] = storage.upload_banner(
                        ctx.tenant_id, data, *sniffed)
                    storage.delete_banner(old)
                except storage.StorageError:
                    banner_err = "banner_storage"
    elif form.get("portal_banner_remove") is not None:
        old = (db.get_tenant_config(ctx.tenant_id, client=ctx.client)
               or {}).get("portal_banner_path")
        cfg["portal_banner_path"] = None
        storage.delete_banner(old)
    def _blocking():
        db.upsert_tenant_config(ctx.tenant_id, cfg, client=ctx.client)
        # Slug edit: validated shape, uniqueness enforced by the DB index — a taken
        # slug degrades to a visible message, never a 500.
        new_slug = (form.get("portal_slug") or "").strip().lower()
        current = (db.get_tenant(ctx.tenant_id, client=ctx.client) or {}).get("portal_slug")
        if new_slug and new_slug != current:
            if not portal._SLUG_RE.match(new_slug):
                return RedirectResponse("/app/settings?portal=slug_invalid", status_code=303)
            if not db.set_portal_slug(ctx.tenant_id, new_slug):
                return RedirectResponse("/app/settings?portal=slug_taken", status_code=303)
        if banner_err:
            return RedirectResponse(f"/app/settings?portal={banner_err}", status_code=303)
        return RedirectResponse("/app/settings?portal=saved", status_code=303)
    return await run_in_threadpool(_blocking)


# ── Morning debrief settings (docs/modules/module-morning-debrief.md) ────────────────
_DEBRIEF_TIME_RE = re.compile(r"^([01]\d|2[0-3]):(00|15|30|45)$")
_QUIET_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _save_brief_topics(ctx: TenantCtx, picked) -> list[str]:
    """Store what the brief covers — known keys only, in TOPICS order."""
    keys = [k for k in debrief.TOPIC_KEYS if k in set(picked or [])]
    db.upsert_tenant_config(ctx.tenant_id, {"debrief_topics": keys}, client=ctx.client)
    return keys


@router.post("/settings/debrief/topics", dependencies=[Depends(require_csrf_header)])
async def save_brief_topics(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """The cog on the brief's bubble (Kris 2026-09-25): JSON {topics: [...]}.
    Owners and admins only, as Settings is."""
    if not people.is_admin(_me(ctx)):
        raise HTTPException(status_code=403, detail="Only the owner or an admin can change this.")
    body = await request.json()
    def _blocking():
        return {"topics": _save_brief_topics(ctx, (body or {}).get("topics"))}
    return await run_in_threadpool(_blocking)


@router.post("/settings/debrief", dependencies=[Depends(require_csrf)])
async def save_debrief_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Its own form (the portal-form pattern) so checkbox semantics can't be
    clobbered by the big voice/config form. A malformed time degrades to the
    07:30 default rather than erroring — the sweep's lexicographic compare
    must never see junk."""
    form = await request.form()
    def _blocking():
        t = (form.get("debrief_time") or "").strip()
        cfg: dict = {
            "debrief_enabled": form.get("debrief_enabled") is not None,
            "debrief_time": t if _DEBRIEF_TIME_RE.match(t) else "07:30",
        }
        # Closed-days override saved ONLY when the form rendered it (a diary
        # exists) — a diary-less tenant's save can never flip a setting it didn't
        # show (the planner_auto_apply rule).
        mods = db.visible_modules(ctx.tenant_id, client=ctx.client)
        if "scheduling" in mods or "tms" in mods:
            cfg["debrief_on_closed_days"] = form.get("debrief_on_closed_days") is not None

        # The five families and the quiet window ride the same form (Kris
        # 2026-09-02) — but only when the form actually RENDERED them. Checkboxes
        # are absent when unticked, so without this marker any other post to this
        # route would read "no fields" as "switch everything off", which is the
        # same trap the closed-days override above is guarded against.
        #
        # An unrecognised select value falls back to 'now': the failure mode of a
        # bad value must be a notification you still get, never one you silently
        # stop getting.
        if form.get("notify_form"):
            for col in notify_prefs.FAMILIES:
                picked = (form.get(col) or "").strip()
                # modes_for, not MODES: a family that offers no "never" must not be
                # switchable to one by a hand-made POST either (Kris 2026-09-04).
                cfg[col] = (picked if picked in notify_prefs.modes_for(col)
                            else notify_prefs.NOW)

            # Both or neither — half a window is not a window, and would read as
            # quiet hours running when they are not.
            start = (form.get("quiet_start") or "").strip()
            end = (form.get("quiet_end") or "").strip()
            ok = bool(_QUIET_TIME_RE.match(start) and _QUIET_TIME_RE.match(end)
                    and start != end)
            cfg["quiet_start"] = start if ok else None
            cfg["quiet_end"] = end if ok else None

            # The two per-kind opt-ins that live inside their family rows. A view is
            # lower signal than an acceptance or a payment, so they stay opt-in — but
            # they are reachable now (notify_invoice_viewed had no interface at all).
            cfg["notify_quote_viewed"] = form.get("notify_quote_viewed") is not None
            cfg["notify_invoice_viewed"] = form.get("notify_invoice_viewed") is not None

            # Where notification mail goes. Blank turns the channel off; anything
            # that isn't an address is treated as blank rather than saved, so the
            # row cannot sit there looking configured while nothing is sent.
            addr = (form.get("notify_email") or "").strip().lower()
            cfg["notify_email"] = addr if notify_prefs.EMAIL_RE.match(addr) else None

        # What the brief tells you (2026-09-25) — saved only when the list was on
        # screen, so an unticked box means off and a missing one means untouched.
        if form.get("debrief_topics_form"):
            cfg["debrief_topics"] = [k for k in debrief.TOPIC_KEYS
                                     if form.get("debrief_topic_" + k) is not None]

        db.upsert_tenant_config(ctx.tenant_id, cfg, client=ctx.client)
        return RedirectResponse("/app/settings?saved=1", status_code=303)
    return await run_in_threadpool(_blocking)


# ── Today's order (Kris 2026-09-04) ─────────────────────────────────────────
@router.post("/settings/deck", dependencies=[Depends(require_csrf)])
async def save_deck_settings(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Which topic Today leads with. Its own form, like the debrief's, so a
    save here can never touch a notification setting a screen away.

    Every topic is written on every save — the form renders all eight selects,
    so absent means junk rather than unticked, and junk lands on 'normal'. A
    tenant who has never opened this stores {} and gets the default order."""
    form = await request.form()
    def _blocking():
        picked: dict = {}
        order = deck.clean_order([str(v) for v in form.getlist("order")])
        if order is not None:
            # The list control (Today's cog, or the Settings fold): the whole order.
            picked = {deck.ORDER_KEY: order}
        else:
            for topic in deck.TOPICS:
                value = (form.get("deck_" + topic) or "").strip()
                if value in deck.PLACEMENTS and value != deck.NORMAL:
                    picked[topic] = value
        db.upsert_tenant_config(ctx.tenant_id, {"deck_priority": picked},
                                client=ctx.client)
        # The cog on Today posts from Today and lands back there (Kris 2026-09-13).
        if (form.get("back") or "") == "/app/":
            return RedirectResponse("/app/?deck=saved", status_code=303)
        return RedirectResponse("/app/settings?saved=1", status_code=303)
    return await run_in_threadpool(_blocking)


# ── Voice auto-learning (settings-side re-run + apply) ───────────────────────
@router.post("/settings/voice-learn", dependencies=[Depends(require_csrf)])
async def settings_voice_learn(ctx: TenantCtx = Depends(require_onboarded)):
    """Re-run the sent-mail analysis on demand (e.g. after reconnecting a
    mailbox, or a tenant who onboarded before this feature). Async so the run
    is spawned on the event loop."""
    if await run_in_threadpool(db.get_connection_status, ctx.tenant_id, client=ctx.client):
        voice_learning.spawn(ctx.tenant_id)
    return RedirectResponse("/app/settings?voice=learning", status_code=303)


@router.post("/settings/voice-apply", dependencies=[Depends(require_csrf)])
def settings_voice_apply(ctx: TenantCtx = Depends(require_onboarded)):
    """Apply the learned profile to the live voice fields."""
    voice_learning.apply_profile(ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/settings?saved=1", status_code=303)


@router.post("/settings/voice-discard", dependencies=[Depends(require_csrf)])
def settings_voice_discard(ctx: TenantCtx = Depends(require_onboarded)):
    """Forget the learned profile (revocation). Live voice fields stay — they're
    the tenant's editable config; only the learned artefacts are deleted."""
    voice_learning.discard_profile(ctx.tenant_id, client=ctx.client)
    return RedirectResponse("/app/settings?voice=discarded", status_code=303)


@router.post("/settings/voice-ignore", dependencies=[Depends(require_csrf)])
async def settings_voice_ignore(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Skip (or un-skip) voice learning as an outstanding setup step.

    Its own column rather than a status value: voice_learning_status records
    what the LEARNER did, and writing 'accepted' here to silence the prompt
    would have claimed a profile exists when none does — misleading the
    onboarding progress and the drafter's provenance label at once. This records
    only that the operator has seen it and moved on, and it is reversible, so
    "Remind me about this step" puts it back.
    """
    form = await request.form()
    def _blocking():
        ignored = (form.get("ignored") or "1") == "1"
        db.upsert_tenant_config(ctx.tenant_id, {"voice_learning_ignored": ignored},
                                client=ctx.client)
        return RedirectResponse("/app/settings", status_code=303)
    return await run_in_threadpool(_blocking)


@router.get("/settings/voice-status")
def settings_voice_status(ctx: TenantCtx = Depends(require_onboarded)):
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    return {"status": cfg.get("voice_learning_status") or "none"}


@router.post("/settings/close-account", dependencies=[Depends(require_csrf)])
async def close_account(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Self-serve account closure (GDPR right to erasure / OAuth-verification req).

    Revokes each mailbox's provider webhook, hard-deletes the tenant — every
    tenant-scoped table cascades from tenants(id) — then deletes any auth user
    left with no other tenant, and signs out. External calls are best-effort so a
    single provider/API hiccup can't leave the account half-deleted.
    """
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    form = await request.form()
    if (form.get("confirm") or "").strip() != "DELETE":
        return RedirectResponse("/app/settings?err=confirm", status_code=303)
    tenant_id = ctx.tenant_id

    # 1. Best-effort: stop Microsoft change-notifications for each mailbox (Gmail
    #    is poll-based — nothing to revoke). The connection rows are removed by
    #    the cascade in step 3.
    for mb in db.list_mailboxes(tenant_id, client=ctx.client):
        conn = db.get_connection_by_id(mb.get("id"), tenant_id)
        if conn:
            try:
                await oauth._delete_graph_subscription(conn, tenant_id)
            except Exception:
                pass

    # 2. Capture member user_ids BEFORE the cascade removes tenant_members.
    def _blocking():
        member_ids = db.list_tenant_member_user_ids(tenant_id)

        # 3. Stored files first (documents, photos, the public banner) — they sit
        #    outside the database cascade (security review M15). Best-effort.
        try:
            from app import storage as _storage
            _storage.remove_tenant_files(tenant_id)
        except Exception:
            log.warning("close_account: file removal failed for %s", tenant_id, exc_info=True)

        # 4. Hard-delete the tenant → cascades all tenant data in one transaction.
        db.delete_tenant(tenant_id)

        # 5. Delete the login of anyone who belonged ONLY to this tenant.
        for uid in member_ids:
            try:
                if not db.user_has_other_tenants(uid):
                    db.delete_auth_user(uid)
            except Exception:
                pass

        # 6. Sign out and land on the public home page.
        resp = RedirectResponse("/?account=closed", status_code=303)
        clear_session_cookies(resp)
        return resp
    return await run_in_threadpool(_blocking)


# ── Knowledge base (fact-check sources) ─────────────────────────────────────
def _kb_redirect(msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"/app/knowledge?{q}" if q else "/app/knowledge", status_code=303)


@router.get("/knowledge")
def knowledge_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    # Enterprise (D4 — layered knowledge): sources carry their scope (company-wide
    # or one mailbox) and the add-forms gain a target selector. SaaS: unchanged.
    enterprise = db.is_enterprise(ctx.tenant_id, client=ctx.client)
    boxes = _tenant_mailboxes(ctx) if enterprise else []
    box_names = {m["id"]: m["display"] for m in boxes}
    sources = knowledge_forms.read(ctx)
    knowledge_versions = [{k: row[k] for k in ("source", "mailbox_id", "version")} for row in sources]
    for s in sources:
        s["scope"] = box_names.get(s.get("mailbox_id")) if s.get("mailbox_id") else None
        # A note is editable in place; an imported document is not text the owner
        # wrote, so it stays Remove + re-import. The text itself is fetched only
        # when they open the editor — the list never carries the whole base.
        s["editable"] = s.get("chunks", 0) <= kb.EDITABLE_MAX_CHUNKS
    # Knowledge via chat (Kris 2026-09-04): the coverage strip and the ask —
    # best-effort, the page never waits on them.
    from app import knowledge_coverage
    try:
        cov = knowledge_coverage.coverage(db.knowledge_source_texts(ctx.tenant_id, client=ctx.client))
        brief = ((db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}).get("business_brief") or "")[:120]
    except Exception:
        cov, brief = knowledge_coverage.coverage([]), ""
    return templates.TemplateResponse(request, "app/knowledge.html", _ctx(
        "knowledge", ctx, sources=sources, knowledge_versions=knowledge_versions, configured=kb.is_configured(),
        coverage=cov, ask_text=knowledge_coverage.ask_text(cov["missing"], brief),
        msg=request.query_params.get("msg"), err=request.query_params.get("err"),
        prefill=request.query_params.get("prefill"),
        # Knowledge hold (Kris 2026-08-31): the gap card's Add-to-Knowledge link
        # carries its email_log id — saving the note then drafts that reply.
        redraft=request.query_params.get("redraft"),
        enterprise=enterprise, boxes=boxes,
        sel=_selected_mailbox_id(ctx, request.query_params.get("mailbox")) if enterprise else None,
    ))


@router.post("/knowledge/upload", dependencies=[Depends(require_csrf)])
async def knowledge_upload(file: UploadFile = File(...), mailbox_id: str = Form(""),
                           knowledge_versions: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    name = file.filename or "upload"
    box = _selected_mailbox_id(ctx, mailbox_id)
    expected = knowledge_forms.version(knowledge_versions, name, box)
    data = await file.read()
    if len(data) > kb.MAX_UPLOAD_BYTES:
        return _kb_redirect(err=f"{name} is too large (max 5 MB).")
    try:
        n = await kb.ingest(ctx.tenant_id, name, await asyncio.to_thread(kb.extract_text, name, data),
                            client=ctx.client,
                            mailbox_id=box, expected_version=expected)
    except HTTPException:
        raise
    except Exception as e:  # extraction/embedding failures → tell the tenant
        return _kb_redirect(err=f"Could not import {name}: {e}")
    def _blocking():
        return _kb_redirect(msg=f"Imported {name} ({n} chunk{'' if n == 1 else 's'}).")
    return await run_in_threadpool(_blocking)


@router.post("/knowledge/url", dependencies=[Depends(require_csrf)])
async def knowledge_url(url: str = Form(...), label: str = Form(""), mailbox_id: str = Form(""),
                        knowledge_versions: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    source = label.strip() or url.strip()
    box = _selected_mailbox_id(ctx, mailbox_id)
    expected = knowledge_forms.version(knowledge_versions, source, box)
    try:
        n = await kb.ingest(ctx.tenant_id, source, await kb.fetch_url(url.strip()), client=ctx.client,
                            mailbox_id=box, expected_version=expected)
    except HTTPException:
        raise
    except Exception as e:
        return _kb_redirect(err=f"Could not import that URL: {e}")
    def _blocking():
        return _kb_redirect(msg=f"Imported {source} ({n} chunk{'' if n == 1 else 's'}).")
    return await run_in_threadpool(_blocking)


@router.post("/knowledge/text", dependencies=[Depends(require_csrf)])
async def knowledge_text(label: str = Form(...), content: str = Form(...), mailbox_id: str = Form(""),
                         redraft_log: str = Form(""),
                         knowledge_versions: str = Form(""),
                           ctx: TenantCtx = Depends(require_onboarded)):
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    source = label.strip() or "Pasted note"
    box = _selected_mailbox_id(ctx, mailbox_id)
    expected = knowledge_forms.version(knowledge_versions, source, box)
    try:
        n = await kb.ingest(ctx.tenant_id, source, content, client=ctx.client,
                            mailbox_id=box, expected_version=expected)
    except HTTPException:
        raise
    except Exception as e:
        return _kb_redirect(err=f"Could not add that note: {e}")
    # Knowledge hold (Kris 2026-08-31): this note was added FOR a held reply —
    # draft it now against the fresh knowledge and land the owner back on the
    # thread with the draft waiting. Best-effort: a redraft failure still keeps
    # the note (the thread's "Draft anyway" remains).
    if redraft_log.strip():
        entry = db.get_email_log_entry(redraft_log.strip(), ctx.tenant_id, client=ctx.client)
        if entry:
            try:
                await pipeline.redraft_email_log(ctx.tenant_id, entry["id"], client=ctx.client)
                events.publish(ctx.tenant_id, {"type": "draft_ready", "log_id": entry["id"],
                                               "contact": entry.get("from_address")})
                return RedirectResponse(
                    "/app/conversations/view?contact=" + quote(entry.get("from_address") or ""),
                    status_code=303)
            except Exception:
                log.warning("post-knowledge redraft failed for %s", redraft_log, exc_info=True)
    def _blocking():
        return _kb_redirect(msg=f"Added {source} ({n} chunk{'' if n == 1 else 's'}).")
    return await run_in_threadpool(_blocking)


@router.get("/knowledge/note")
def knowledge_note(source: str, mailbox_id: str = "",
                   ctx: TenantCtx = Depends(require_onboarded)):
    """One note's text, for the in-place editor. Tenant-scoped by construction."""
    rows = knowledge_forms.read(ctx, source, _selected_mailbox_id(ctx, mailbox_id))
    if not rows:
        raise HTTPException(status_code=404, detail="Note not found")
    row = rows[0]
    return {"source": source, "content": kb.join_chunks(row['content']),
            "knowledge_versions": [{k: row[k] for k in ('source','mailbox_id','version')}]}



@router.post("/knowledge/edit", dependencies=[Depends(require_csrf)])
async def knowledge_edit(source: str = Form(...), content: str = Form(...),
                         label: str = Form(""), mailbox_id: str = Form(""),
                         knowledge_versions: str = Form(""),
                         ctx: TenantCtx = Depends(require_onboarded)):
    """Correct a note in place (Kris 2026-09-04: fixing a fact meant Remove, then
    retype the whole thing from memory). Re-ingesting under the same name REPLACES
    the source, so the old chunks never linger; a changed name moves the note and
    removes the old one, rather than leaving the agent both."""
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    box = _selected_mailbox_id(ctx, mailbox_id)
    new_name = label.strip() or source
    expected = knowledge_forms.version(knowledge_versions, source, box, existing=True)
    try:
        n = await kb.ingest(ctx.tenant_id, new_name, content, client=ctx.client,
                            mailbox_id=box, expected_version=expected, original_source=source)
    except HTTPException:
        raise
    except Exception as e:
        return _kb_redirect(err=f"Could not save that note: {e}")
    def _blocking():
        return _kb_redirect(msg=f"Updated {new_name} ({n} chunk{'' if n == 1 else 's'}).")
    return await run_in_threadpool(_blocking)


@router.post("/knowledge/delete", dependencies=[Depends(require_csrf)])
def knowledge_delete(source: str = Form(...), mailbox_id: str = Form(""),
                     knowledge_versions: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    # The scope is part of the source's identity (D4): deleting orders@'s copy of
    # price-list.pdf never touches the company-wide one, and vice versa.
    box = _selected_mailbox_id(ctx, mailbox_id)
    expected = knowledge_forms.version(knowledge_versions, source, box, existing=True)
    knowledge_forms.save(ctx.tenant_id, source, source, box, expected, None, client=ctx.client)
    return _kb_redirect(msg=f"Removed {source}.")
