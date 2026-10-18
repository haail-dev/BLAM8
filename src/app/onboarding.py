"""First-run onboarding wizard.

A guided, value-first sequence — interview → connect mailbox → teach voice →
knowledge → live-data sources → finish. It reuses the mailbox connect (/oauth),
tenant_config, and knowledge-base ingest; the genuinely new parts are the
interview capture (the assistant asks what the business does + what it wants
handled, and fills the form — industry, brief, goals), the system-of-record
(data-source) capture, and gating the dashboard until `tenants.onboarded_at`
is set (see app.auth.require_onboarded). The classic industry picker survives
at /onboarding/industry as the manual fallback.

Steps after the mailbox are optional — each can be skipped and refined later from
the dashboard. Everything captured is structured tenant config (no per-tenant
code), which also doubles as the integration roadmap signal across tenants.
"""
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as FormUploadFile

from app import auth
from app import db
from app import fairuse
from app import kb_website
from app import knowledge as kb
from app import operating_systems
from app import partners
from app import sign_off
from app import storage
from app import voice_learning
from app.auth import require_tenant, TenantCtx
from app.config import settings
from app.security import require_csrf
from app.templating import templates
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

# Wizard step order (drives the progress bar). The live-data (system-of-record)
# step came OFF the wizard 2026-08-02 (Julius: another step between them and the
# plan; reintroduce later as an in-app prompt) — its routes below survive
# off-flow, so nothing links break and the capture can return cheaply.
from app import onboarding_journey
STEPS = list(onboarding_journey.STEPS)

INDUSTRIES = [
    {"id": "logistics", "label": "Logistics & haulage", "os": "field",
     "blurb": "Quotes, bookings, tracking, PODs — pallet & freight.", "live": True},
    {"id": "trades", "label": "Trades & field service", "os": "field",
     "blurb": "Plumbing, electrical, HVAC — jobs, callouts, quotes.", "live": True},
    # Construction is a core-only Field OS sector: visible in the picker (live=False,
    # so no dispatch module lights up — it runs core email + scheduling with tuned
    # construction vocabulary). The remaining sectors below stay hidden — their ids
    # stay valid (stored tenants, industry_lingo, concierge/intake all key off them)
    # and any of them still onboards via the "Something else" free-text catch-all.
    # Unhide by removing the "hidden" flag once the sector is ready to feature.
    {"id": "construction", "label": "Construction & contractors", "os": "field",
     "blurb": "Tenders, subcontractors, site schedules, RFQs.", "live": False},
    {"id": "recruitment", "label": "Recruitment & staffing", "os": "match",
     "blurb": "Candidates, clients, interviews, placements.", "live": False, "hidden": True},
    {"id": "wholesale", "label": "Wholesale & distribution", "os": "wholesale",
     "blurb": "Email orders, stock, pricing, fulfilment.", "live": True},
    # Assets OS (docs/os/assets.md §9) — the hire-shape verticals. lettings + hire are
    # the live wedge; the hire sub-industries stay present-but-hidden (valid ids for stored
    # tenants / lingo / seeds) until their vocab is featured — onboard via "Something else".
    {"id": "lettings", "label": "Lettings & property management", "os": "assets", "live": True,
     "blurb": "Availability, deposits and handovers — atomic bookings against every unit."},
    {"id": "hire", "label": "Equipment & plant hire", "os": "assets", "live": True,
     "blurb": "Availability, bookings, off-hire — never double-book a machine."},
    {"id": "car_hire", "label": "Car & van hire", "os": "assets",
     "blurb": "Availability, deposits, handover condition.", "live": False, "hidden": True},
    {"id": "boat_hire", "label": "Boat hire", "os": "assets",
     "blurb": "Availability, deposits, handover condition.", "live": False, "hidden": True},
    {"id": "tool_hire", "label": "Tool hire", "os": "assets",
     "blurb": "Fungible-pool availability, off-hire, deposits.", "live": False, "hidden": True},
    {"id": "party_hire", "label": "Party & marquee hire", "os": "assets",
     "blurb": "Availability, deposits, delivery & collection.", "live": False, "hidden": True},
    {"id": "camping_hire", "label": "Camping & outdoor hire", "os": "assets",
     "blurb": "Fungible-pool availability, deposits, returns.", "live": False, "hidden": True},
    # Events OS (docs/os/events.md §9) — the one-off-dated-occasion verticals. Event
    # venues are the initial market; suppliers and event planners are adjacent markets,
    # all live. All share the `event` primitive; ids key stored tenants / lingo / seeds.
    {"id": "events", "label": "Event venues & functions", "os": "events", "live": True,
     "blurb": "Weddings, corporate days, functions — guests, dietaries, suppliers, run sheet."},
    {"id": "caterer", "label": "Catering company", "os": "events", "live": True,
     "blurb": "Covers, dietaries and final numbers — the whole event off your inbox."},
    {"id": "wedding_planner", "label": "Wedding & event planner", "os": "events", "live": True,
     "blurb": "Every stakeholder tracked and chased to the run sheet, per event."},
    # Practice OS (docs/os/practice.md §9.2): the initial practice profile + the two biggest
    # adjacent markets un-hidden; the rest present-but-hidden (valid ids for stored
    # tenants / lingo / seeds) until their vocab is tuned — onboard via "Something else".
    {"id": "dentist", "label": "Dental practice", "os": "practice", "live": True,
     "blurb": "Recall, reminders, no-show prediction — the whole patient diary."},
    {"id": "salon", "label": "Salon, barber & beauty", "os": "practice", "live": True,
     "blurb": "Rebooking nudges, reminders, no-show prediction, client history."},
    {"id": "physio", "label": "Physio & clinic", "os": "practice", "live": True,
     "blurb": "Appointment reminders, recall, treatment-block follow-ups."},
    {"id": "chiropractor", "label": "Chiropractor & osteopath", "os": "practice",
     "blurb": "Recall, reminders, no-show prediction, patient history.",
     "live": False, "hidden": True},
    {"id": "vet", "label": "Veterinary practice", "os": "practice",
     "blurb": "Recall, reminders, no-show prediction, patient history.",
     "live": False, "hidden": True},
    {"id": "tutor", "label": "Tutor", "os": "practice",
     "blurb": "Rebooking nudges, reminders, no-show prediction, lesson history.",
     "live": False, "hidden": True},
    {"id": "coach", "label": "Coach", "os": "practice",
     "blurb": "Session reminders, rebooking, no-show prediction, client history.",
     "live": False, "hidden": True},
    {"id": "personal_trainer", "label": "Personal trainer", "os": "practice",
     "blurb": "Session reminders, rebooking, no-show prediction, client history.",
     "live": False, "hidden": True},
    {"id": "therapist", "label": "Therapist & counsellor", "os": "practice",
     "blurb": "Appointment reminders, recall, client history.",
     "live": False, "hidden": True},
    # Storefront OS (docs/os/storefront.md §9) — the shop-shape verticals, all live.
    {"id": "farm_shop", "label": "Farm shop & grocer", "os": "storefront", "live": True,
     "blurb": "Stock, orders, collections — photograph the delivery, the shop knows."},
    {"id": "deli", "label": "Deli & butcher", "os": "storefront", "live": True,
     "blurb": "Counter orders, reservations, best-before specials to your regulars."},
    {"id": "bakery", "label": "Bakery & cafe", "os": "storefront", "live": True,
     "blurb": "Pre-orders, collections and end-of-day clearance to past buyers."},
    {"id": "wine_merchant", "label": "Wine & artisan retail", "os": "storefront", "live": True,
     "blurb": "Stock by vintage/line, account customers, personalised offers."},
    {"id": "nursery", "label": "Nursery & garden centre", "os": "storefront", "live": True,
     "blurb": "Seasonal stock, reservations, collection nudges."},
    {"id": "other", "label": "Something else", "os": None,
     "blurb": "Not listed? You still get the full core platform — tell us your sector below.", "live": False},
]

# Data-source options shown at the "live data" step, by industry.
DATA_SOURCES = {
    "logistics": [
        {"kind": "tms", "label": "Transport Management System",
         "hint": "Where jobs, bookings and tracking live.", "vendors": ["Mandata", "Microlise", "Other"]},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file you run the schedule from.", "vendors": []},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
    ],
    "trades": [
        {"kind": "fsm", "label": "Field service / job software",
         "hint": "Where your jobs, engineers and invoices live.",
         "vendors": ["Jobber", "Tradify", "Commusoft", "Simpro", "ServiceM8", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file you run the diary from.", "vendors": []},
    ],
    # Practice OS (docs/os/practice.md §9.4) — capture the PMS/booking system as the
    # integration-roadmap signal; v1 works from the inbox + calendar regardless.
    "dentist": [
        {"kind": "practice_system", "label": "Practice management system",
         "hint": "Where patients, appointments and recalls live.",
         "vendors": ["Software of Excellence (SOE)", "Dentally", "SOE Exact", "R4", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google.", "vendors": []},
    ],
    "physio": [
        {"kind": "practice_system", "label": "Clinic software",
         "hint": "Where patients, appointments and treatment blocks live.",
         "vendors": ["Cliniko", "Jane", "WriteUpp", "TM3", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google.", "vendors": []},
    ],
    "salon": [
        {"kind": "booking_system", "label": "Booking system",
         "hint": "Where clients, appointments and rebookings live.",
         "vendors": ["Fresha", "Treatwell", "Timely", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google.", "vendors": []},
    ],
    # Storefront OS (docs/os/storefront.md §9) — "None yet" is the wedge signal: a
    # shop with no POS is the target and onboards straight into OCR-first stock.
    "farm_shop": [
        {"kind": "pos", "label": "Existing till / POS",
         "hint": "Square, Shopify, Vend — we sync stock both ways.",
         "vendors": ["Square", "Shopify", "Vend", "None yet"]},
        {"kind": "spreadsheet", "label": "Stock spreadsheet",
         "hint": "A sheet you track stock on today.", "vendors": []},
        {"kind": "whatsapp", "label": "WhatsApp orders",
         "hint": "Where customers message their orders.", "vendors": []},
    ],
    # Assets OS (docs/os/assets.md §9) — capture the channel-manager / hire system as the
    # integration-roadmap signal; v1 works from the inbox + calendar regardless.
    "lettings": [
        {"kind": "channel_manager", "label": "Channel manager",
         "hint": "Where your listings and bookings sync from.",
         "vendors": ["Airbnb", "Booking.com", "Vrbo", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file you run bookings from.", "vendors": []},
    ],
    "hire": [
        {"kind": "hire_software", "label": "Hire software",
         "hint": "Where your assets, hires and off-hires live.",
         "vendors": ["inspHire", "Point of Rental", "Current RMS", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file you run availability from.", "vendors": []},
    ],
    # Events OS (docs/os/events.md §9) — capture the enquiry/booking system + supplier POS
    # as the integration-roadmap signal; v1 works from the inbox + calendar regardless.
    "events": [
        {"kind": "booking_system", "label": "Enquiry / booking system",
         "hint": "Where event enquiries and bookings live.",
         "vendors": ["Tripleseat", "Perfect Venue", "Other"]},
        {"kind": "pos", "label": "Caterer POS / dietary tool",
         "hint": "Where the catering order and dietaries are managed.",
         "vendors": ["Nutritics", "Caterease", "Other"]},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file you run events from.", "vendors": []},
    ],
    # Wholesale OS (docs/os/wholesale.md §9): accounts (credit truth), warehouse
    # (stock truth), and the spreadsheet the wedge actually runs on today.
    "wholesale": [
        {"kind": "accounts", "label": "Accounts software",
         "hint": "Where invoices, payments and credit live.",
         "vendors": ["Xero", "Sage", "QuickBooks", "Other"]},
        {"kind": "warehouse", "label": "Warehouse / inventory",
         "hint": "Stock levels and allocation, if you run one.",
         "vendors": ["Unleashed", "DEAR / Cin7", "Other"]},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "The order book you run today — a Google Sheet or Excel file.", "vendors": []},
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar.", "vendors": []},
    ],
    "_default": [
        {"kind": "calendar", "label": "Calendar",
         "hint": "Outlook or Google calendar — your appointments live here.", "vendors": []},
        {"kind": "booking_system", "label": "Booking system",
         "hint": "An online booking / scheduling tool.", "vendors": []},
        {"kind": "spreadsheet", "label": "Spreadsheet",
         "hint": "A Google Sheet or Excel file.", "vendors": []},
    ],
}
# `caterer` / `wedding_planner` share the event-venue data sources (all Events OS
# verticals onboard the same way — enquiry system / supplier POS / calendar).
for _ev in ("caterer", "wedding_planner"):
    DATA_SOURCES[_ev] = DATA_SOURCES["events"]
# deli / bakery / wine_merchant / nursery share the farm-shop data sources (all
# storefront shops onboard the same way — POS / spreadsheet / WhatsApp).
for _sf in ("deli", "bakery", "wine_merchant", "nursery"):
    DATA_SOURCES[_sf] = DATA_SOURCES["farm_shop"]


# The five tuning steps every sign-up meets after the wizard (the seeded
# modules are scheduling + crm; app/setup_handoff.steps is the authority), as
# short labels for the wizard's stepper — one flow of nine (Kris 2026-09-13:
# "it's still looking like 4 steps and we're done").
_TUNING_SHORT = {"diary": "Diary", "requirements": "Requirements", "dependants": "Dependants",
                 "crm": "CRM", "invoicing": "Getting paid"}


def tuning_steps() -> list[dict]:
    from app import setup_handoff
    return [{"key": s["key"], "label": _TUNING_SHORT.get(s["key"], s["label"])}
            for s in setup_handoff.steps({"scheduling", "crm"}, None, [], None, None)]


def _step_ctx(step: str, **extra) -> dict:
    # The classic industry picker isn't in STEPS (the interview replaced it) but
    # still renders — on the stepper it borrows the interview's slot.
    stepper = step if step in STEPS else "interview"
    saved = {}
    if "selected" in extra:
        saved["interview"] = bool(extra["selected"])
    if "connected" in extra:
        saved["mailbox"] = bool(extra["connected"])
    if "cfg" in extra:
        saved["voice"] = bool((extra["cfg"] or {}).get("voice_profile_source"))
    return {"steps": STEPS, "step": step, "stepper": stepper,
            "journey": onboarding_journey.describe(step, connected=extra.get("connected"), saved=saved,
                                                     next_target=extra.get("next_target")),
            "step_index": STEPS.index(stepper), "step_count": len(STEPS),
            "tuning": tuning_steps(), "knowledge_source_review_enabled": settings.knowledge_source_review_enabled, **extra}


def _kb_redirect(msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"/onboarding/knowledge?{q}" if q else "/onboarding/knowledge", status_code=303)


def _finish(ctx: TenantCtx) -> RedirectResponse:
    """Onboarding complete → straight into the app. Every sign-up is already
    on the full-Pro 14-day trial (db.ensure_tenant_for_user seeds it), so the
    plans page was a redundant interstitial (Julius, 2026-08-02) — choosing a
    plan belongs at trial end, where the trial banners/Settings link to
    /billing/plans. Landing on /app also hands straight over to the guided
    settings walk-through (src/app/setup_handoff.py).

    Drafting is HELD on the way out (Kris 2026-09-01). The wizard ends and the
    inbox fills with the tenant's real mail — including every newsletter,
    supplier and no-reply robot they have never marked as ignored, because until
    this moment they have never seen their inbox through this product. Drafting
    from here would spend tokens answering mail that was never going to be
    answered. So the last step of onboarding is theirs: read what landed, ignore
    whom you like, then turn drafting on in Settings.

    Written explicitly rather than left to the column default, so it holds however
    the tenant_config row came to exist — including rows created before the column
    did."""
    db.set_onboarded(ctx.tenant_id, client=ctx.client)
    # Best-effort: the column already defaults to held, so this is the belt to
    # that braces. A config-write blip must not 500 the very last step of
    # signing up — the tenant is held either way.
    try:
        db.upsert_tenant_config(ctx.tenant_id, {"draft_hold": True}, client=ctx.client)
    except Exception:
        logging.getLogger("blautom8.onboarding").warning(
            "could not stamp draft_hold at finish for %s", ctx.tenant_id, exc_info=True)
    # One flow of nine (Kris 2026-09-13): the wizard's last button is "Next:
    # <first tuning step>", and it lands THERE — not on Today with a strip
    # that could be dismissed. Today only when nothing is left to tune.
    from app import setup_handoff
    nxt = setup_handoff.first_open(setup_handoff.load_steps(ctx.tenant_id, client=ctx.client))
    return RedirectResponse(nxt["href"] if nxt else "/app/", status_code=303)


def _next_step_target(ctx: TenantCtx) -> dict:
    from app import setup_handoff
    nxt = setup_handoff.first_open(setup_handoff.load_steps(ctx.tenant_id, client=ctx.client))
    return {"label": nxt["label"], "href": nxt["href"]} if nxt else {"label": "Today", "href": "/app/"}


def _next_step_label(ctx: TenantCtx) -> str:
    target = _next_step_target(ctx)
    return "Finish setup" if target["href"] == "/app/" else "Next: " + target["label"]


# ── resume ───────────────────────────────────────────────────────────────────
@router.get("")
@router.get("/")
def home(ctx: TenantCtx = Depends(require_tenant)):
    """Resume at the furthest step reached (interview → mailbox → voice →
    knowledge). Fully-onboarded tenants go straight to the dashboard."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if t.get("onboarded_at"):
        return RedirectResponse("/app", status_code=303)
    if not t.get("industry"):
        return RedirectResponse("/onboarding/interview", status_code=303)
    if not db.get_connection_status(ctx.tenant_id, client=ctx.client):
        return RedirectResponse("/onboarding/mailbox", status_code=303)
    # Resume at the furthest step reached. Every way of finishing Voice (accept
    # the learned profile, save the form, adjust the learned one) stamps
    # voice_profile_source; nothing before the step does.
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    if cfg.get("voice_profile_source"):
        return RedirectResponse("/onboarding/knowledge", status_code=303)
    return RedirectResponse("/onboarding/voice", status_code=303)


# ── 1 · interview (the assistant-led front door) ─────────────────────────────
# The concierge interviews them (what the business does + what they want
# handled) and mirrors its answers into this form; the tenant reviews and
# clicks Continue — the POST here stays the only write path.
INTERVIEW_CFG_FIELDS = ("business_brief", "goals", "website")

# Knowledge sources the website reader saved — "Website — {label}" — so a
# re-run replaces rather than piles up, and the Knowledge step knows whether
# the site on file has been read yet.
WEBSITE_SOURCE_PREFIX = "Website — "


def _clean_website(raw: str) -> str | None:
    """The website as typed, tidied: trimmed, one line, must look like a host
    ('littledogs.co.uk', 'www.x.com', 'https://x.com'). Anything else — a
    stray word, an email address — is dropped rather than read later."""
    w = " ".join((raw or "").split())[:200]
    if not w or "@" in w or " " in w or "." not in w.split("://")[-1]:
        return None
    return w.rstrip("/")

def _industry_value(industry: str, industry_other: str) -> str:
    """The stored industry: preset ids as-is; 'other' becomes the free-text
    sector when given (the long-tail roadmap signal), else literal 'other'."""
    return (industry_other.strip() or "other") if industry == "other" else industry.strip()


def _referral_state(tenant_id: str) -> dict | None:
    """The tenant's existing attribution for the interview page — best-effort
    (a not-yet-applied partners migration must not break onboarding)."""
    try:
        return db.get_tenant_referral(tenant_id)
    except Exception:
        return None


async def _signer_email(request: Request) -> str | None:
    """The signed-in user's email, for the in-product self-referral block (a
    partner typing their own code earns nothing). Best-effort — require_tenant
    already validated this session, so a miss here just skips the check."""
    session = getattr(request.state, "refreshed_session", None)
    access = (session or {}).get("access_token") or request.cookies.get(auth.AT_COOKIE)
    if not access:
        return None
    try:
        user = await auth.get_user(access)
    except Exception:
        return None
    return (user or {}).get("email")


@router.get("/interview")
def interview_get(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    referral = _referral_state(ctx.tenant_id) or {}
    # The name seeded from the login address ("Business Blam8") is a
    # placeholder, not a value — shown as one, so a first-timer never leaves
    # it thinking the name is set (run #10; LittleDogs' portal read "Kris
    # Deane93").
    placeholder = auth.default_company_name(getattr(request.state, "user_email", "") or "")
    name = t.get("name") or ""
    name_is_placeholder = bool(placeholder and name.strip().lower() == placeholder.strip().lower())
    return templates.TemplateResponse(request, "onboarding/interview.html",
                                      _step_ctx("interview",
                                                selected=t.get("industry"), cfg=cfg,
                                                business_name="" if name_is_placeholder else name,
                                                name_placeholder=(name if name_is_placeholder else ""),
                                                banner_url=storage.banner_public_url(
                                                    cfg.get("portal_banner_path")),
                                                banner_err=request.query_params.get("banner_err"),
                                                referral_code_used=referral.get("referral_code_used"),
                                                ref_err=request.query_params.get("ref_err"),
                                                ref_why=request.query_params.get("why")))


@router.post("/interview", dependencies=[Depends(require_csrf)])
async def interview_post(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    form = await request.form()
    # The customer-facing trading name (portal header, agent replies). Sign-up
    # seeded a placeholder from the login email; an emptied box keeps what's on
    # file rather than blanking the portal.
    business_name = (form.get("business_name") or "").strip()
    if business_name:
        db.set_tenant_name(ctx.tenant_id, business_name[:120], client=ctx.client)
    industry = (form.get("industry") or "").strip()
    if industry:
        # Retain old open forms while the interview now submits plain owner words.
        value = (form.get("industry_other") or "").strip() if industry == "other" else ""
        db.set_tenant_industry(ctx.tenant_id, value or industry, client=ctx.client)
    cfg = {f: (form.get(f) or "").strip() or None for f in INTERVIEW_CFG_FIELDS}
    cfg["website"] = _clean_website(form.get("website") or "")
    # Optional portal banner (skippable — an empty file input just moves on).
    # Magic bytes decide the type; a bad/oversize file re-renders the step with
    # a message AFTER everything else is saved, so nothing typed is lost.
    banner_err = None
    banner = form.get("portal_banner")
    if isinstance(banner, FormUploadFile) and banner.filename:
        data = await banner.read()
        if data:
            sniffed = storage.sniff_banner(data)
            if not sniffed:
                banner_err = "invalid"
            elif len(data) > storage.MAX_BANNER_BYTES:
                banner_err = "too_big"
            else:
                old = (db.get_tenant_config(ctx.tenant_id, client=ctx.client)
                       or {}).get("portal_banner_path")
                try:
                    cfg["portal_banner_path"] = storage.upload_banner(
                        ctx.tenant_id, data, *sniffed)
                    storage.delete_banner(old)
                except storage.StorageError:
                    banner_err = "storage"
    db.upsert_tenant_config(ctx.tenant_id, cfg, client=ctx.client)
    # Partner referral code (optional). Everything above is already saved, so a
    # typo'd code re-renders the step with an error instead of silently losing
    # the partner's attribution — the one thing that mustn't happen quietly.
    # 'self' (a partner typing their own code) is refused in product; 'taken'
    # (already attributed) passes through — the first code won.
    ref_code = (form.get("referral_code") or "").strip()
    if ref_code and not (_referral_state(ctx.tenant_id) or {}).get("referral_code_used"):
        try:
            result = partners.attach_for_tenant(ctx.tenant_id, ref_code,
                                                signer_email=await _signer_email(request))
        except Exception:
            result = "invalid"
        if result in ("invalid", "self"):
            q = urlencode({"ref_err": partners.normalize_code(ref_code)[:32], "why": result})
            return RedirectResponse(f"/onboarding/interview?{q}", status_code=303)
    if banner_err:
        return RedirectResponse(f"/onboarding/interview?banner_err={banner_err}",
                                status_code=303)
    return RedirectResponse("/onboarding/mailbox", status_code=303)


# ── 1b · industry (classic picker — the interview's manual fallback) ─────────
# Two steps: pick a Shape (OS), then an industry within it. No `shape` (or an
# unknown / not-live one) shows the Shape grid; a live `shape` filters the
# industry cards to that OS. "Something else" (no OS) stays reachable from the
# grid so any sector can still onboard via the free-text catch-all.
@router.get("/industry")
def industry_get(request: Request, shape: str = "", ctx: TenantCtx = Depends(require_tenant)):
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    if not operating_systems.is_live(shape):
        # No / invalid / not-live shape → the Shape grid.
        return templates.TemplateResponse(request, "onboarding/industry.html",
                                          _step_ctx("industry", shapes=operating_systems.OPERATING_SYSTEMS))
    # A live shape → its industries (+ the free-text "other" escape hatch).
    industries = [i for i in INDUSTRIES
                  if not i.get("hidden") and (i.get("os") == shape or i["id"] == "other")]
    return templates.TemplateResponse(request, "onboarding/industry.html",
                                      _step_ctx("industry", industries=industries,
                                                shape=shape, selected=t.get("industry"),
                                                business_name=t.get("name")))


@router.post("/industry", dependencies=[Depends(require_csrf)])
def industry_post(industry: str = Form(...), industry_other: str = Form(""),
                  business_name: str = Form(""),
                  ctx: TenantCtx = Depends(require_tenant)):
    # Same rule as the interview: a filled name replaces the sign-up placeholder,
    # an emptied box keeps what's on file.
    if business_name.strip():
        db.set_tenant_name(ctx.tenant_id, business_name.strip()[:120], client=ctx.client)
    # "Something else" lets any sector sign up and captures the free-text industry,
    # so the long tail of requested verticals becomes a roadmap signal — it lands in
    # tenants.industry, where anything outside the preset ids is a request to track.
    db.set_tenant_industry(ctx.tenant_id, _industry_value(industry, industry_other),
                           client=ctx.client)
    return RedirectResponse("/onboarding/mailbox", status_code=303)


# ── 2 · mailbox (reuses /oauth) ──────────────────────────────────────────────
@router.get("/mailbox")
def mailbox_get(request: Request, change: str = "", ctx: TenantCtx = Depends(require_tenant)):
    """The step says which account is which (Kris 2026-09-13): the address
    they signed in with is the LOGIN; the inbox connected here is a separate
    choice, same address or not. A personal-provider inbox gets asked "is this
    where enquiries arrive?" (LittleDogs finding 7); ?change=1 shows the
    provider buttons again while a mailbox is already connected."""
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    connected = bool(db.get_connection_status(ctx.tenant_id, client=ctx.client))
    mailbox = (t.get("mailbox") or "").strip()
    signer = getattr(request.state, "user_email", "") or ""
    from app import backlog
    return templates.TemplateResponse(request, "onboarding/mailbox.html",
                                      _step_ctx("mailbox", connected=connected,
                                                mailbox=t.get("mailbox"), provider=t.get("provider"),
                                                signer_email=signer,
                                                backlog_status=backlog.default_status(ctx.tenant_id, client=ctx.client) if connected else {},
                                                change=bool(change) and connected,
                                                personal_inbox=connected and auth.is_consumer_address(mailbox),
                                                mailbox_domain=mailbox.rsplit("@", 1)[1].lower() if "@" in mailbox else "",
                                                same_as_login=bool(connected and mailbox and signer
                                                                   and mailbox.lower() == signer.lower())))


@router.post("/mailbox/backlog", dependencies=[Depends(require_csrf)])
async def mailbox_backlog(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    """Record the explicit mailbox-specific import choice before starting work."""
    from app import backlog
    await backlog.choose_import(request, ctx)
    return RedirectResponse("/onboarding/mailbox", status_code=303)


@router.get("/mailbox/import-status")
def mailbox_import_status(request: Request, surface: str = "", ctx: TenantCtx = Depends(require_tenant)):
    from app import backlog
    return templates.TemplateResponse(request, "_backlog_status.html", {
        "backlog_status": backlog.default_status(ctx.tenant_id, client=ctx.client),
        "backlog_settings": surface == "settings"}, headers={"Cache-Control": "no-store"})


# ── 3 · voice (auto-learned from sent mail; the form is the fallback) ────────
# With a mailbox connected, the step is a REVIEW screen: the profile learned
# from their sent emails + a sample draft, with "Looks good" / "Adjust". The
# manual form survives underneath — pre-populated when a profile exists, blank
# only when learning couldn't run (no mailbox / too few sent emails / failure).
VOICE_FIELDS = ("business_brief", "agent_name", "sign_off", "tone", "voice_notes")


def _learned_form_cfg(cfg: dict) -> dict:
    """The Adjust-form values: saved config, with gaps filled from the learned
    profile so adjusting starts from what was learned, never from blank."""
    out = dict(cfg)
    if cfg.get("voice_profile") and cfg.get("voice_learning_status") in ("ready", "accepted"):
        for k, v in voice_learning.profile_to_config(cfg["voice_profile"]).items():
            if v and not (out.get(k) or "").strip():
                out[k] = v
    return out


@router.get("/voice")
def voice_get(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    status = cfg.get("voice_learning_status")
    connected = bool(db.get_connection_status(ctx.tenant_id, client=ctx.client))
    if request.query_params.get("manual"):
        status = "manual"        # explicit escape hatch from the waiting screen
    profile = cfg.get("voice_profile") if status in ("ready", "accepted") else None
    analysed = int(cfg.get("voice_emails_analysed") or 0)
    form_cfg = _learned_form_cfg(cfg)
    # Held for later (src/app/setup_held.py): a sign-off or agent name given at
    # step 1 arrives filled in — empty fields only.
    from app import setup_held
    held_voice = {v["name"]: v["value"]
                  for v in setup_held.for_page(setup_held.load(ctx.tenant_id, client=ctx.client), "voice")}
    held_filled = [k for k, v in held_voice.items() if v and not (form_cfg.get(k) or "").strip()]
    for k in held_filled:
        form_cfg[k] = held_voice[k]
    voice_labels = {"agent_name": "what the agent calls itself", "tone": "tone of voice",
                    "sign_off": "sign-off"}
    return templates.TemplateResponse(request, "onboarding/voice.html", _step_ctx(
        "voice", cfg=form_cfg, learning_status=status, profile=profile,
        sample=cfg.get("voice_sample_draft"), analysed=analysed,
        soft=analysed < voice_learning.SOFT_EMAILS,
        held_filled=held_filled,
        held_filled_labels=[voice_labels.get(k, k) for k in held_filled],
        # Opt-in offer: mailbox connected, learning never run, not escaping to
        # manual — reading their sent mail happens only on an explicit yes.
        offer=connected and not status))


@router.get("/voice/status")
def voice_status(ctx: TenantCtx = Depends(require_tenant)):
    """Poll target for the 'learning your voice' screen."""
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    return {"status": cfg.get("voice_learning_status") or "none"}


@router.post("/voice/accept", dependencies=[Depends(require_csrf)])
def voice_accept(ctx: TenantCtx = Depends(require_tenant)):
    """'Looks good — continue': the learned profile becomes the live voice."""
    if voice_learning.apply_profile(ctx.tenant_id, client=ctx.client):
        return RedirectResponse("/onboarding/knowledge", status_code=303)
    return RedirectResponse("/onboarding/voice", status_code=303)


@router.post("/voice/learn", dependencies=[Depends(require_csrf)])
def voice_learn(ctx: TenantCtx = Depends(require_tenant)):
    """The opt-in: start (or retry) sent-mail analysis — the ONLY way it starts
    during onboarding."""
    if db.get_connection_status(ctx.tenant_id, client=ctx.client):
        # Daily ceiling (2026-09-02 cost review): each run is up to 8 Sonnet
        # batch calls and this button could re-spawn one per click, unmetered.
        # A capped click quietly keeps the existing profile — the voice screen
        # still shows the last run's result, so nothing is lost.
        if fairuse.onboarding_allowed(ctx.tenant_id, "voice_learning", client=ctx.client):
            voice_learning.spawn(ctx.tenant_id)
        else:
            logging.getLogger("blautom8.onboarding").warning(
                "voice learning capped for today (tenant %s) — spawn skipped", ctx.tenant_id)
    return RedirectResponse("/onboarding/voice", status_code=303)


@router.post("/voice", dependencies=[Depends(require_csrf)])
async def voice_post(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    form = await request.form()
    def _blocking():
        cfg = {f: (form.get(f) or "").strip() or None for f in VOICE_FIELDS}
        # One canonical form for the sign-off (a textarea posts CRLF), as in Settings.
        cfg["sign_off"] = sign_off.normalise(cfg.get("sign_off")) or None
        existing = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
        if existing.get("voice_profile") and existing.get("voice_learning_status") in ("ready", "accepted"):
            # They started from the learned values and hand-tuned — auto + manual.
            cfg["voice_profile_source"] = "hybrid"
            cfg["voice_learning_status"] = "accepted"
        else:
            cfg["voice_profile_source"] = "manual"
        db.upsert_tenant_config(ctx.tenant_id, cfg, client=ctx.client)
        from app import setup_held
        setup_held.mark_used(ctx.tenant_id, "voice", client=ctx.client)
        return RedirectResponse("/onboarding/knowledge", status_code=303)
    return await run_in_threadpool(_blocking)


# ── 4 · knowledge (reuses the KB ingest) ─────────────────────────────────────
@router.get("/knowledge")
def knowledge_get(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    from app import knowledge_coverage, knowledge_review
    review_context = knowledge_review.load(ctx.tenant_id)
    sources = [{**s,"chunks":s.get("chunks",1)} for s in review_context["sources"] if s.get("mailbox_id") is None]
    texts = [{"source":s["source"],"text":s["content"]} for s in sources]
    cov = knowledge_coverage.coverage(texts)
    pending_reviews = [knowledge_review.review_view(review_context, r) for r in review_context["reviews"] if r["state"] == "pending"]
    # Each source row opens to show its text (Kris, re-run 2026-09-13: "you
    # can't click any of the chunks to see what's been added").
    text_by_source = {t["source"]: (t.get("text") or "") for t in texts if t.get("source")}
    # The website given at step 1 is READ ON ARRIVAL (Kris 2026-09-13: the
    # offer used to be a box on the page and one line of chat, and the stays business
    # owner typed prices by hand). auto_site = a site is on file, the KB is
    # on, and nothing "Website — …" has been saved yet; the page starts the
    # read itself and the review cards land for their tick. Review-before-save
    # remains explicit — website/save stages a review; atomic admission writes the facts.
    cfg = db.get_tenant_config(ctx.tenant_id, client=ctx.client) or {}
    website = (cfg.get("website") or "").strip()
    site_notes = [s for s in sources if str(s.get("source") or "").startswith(WEBSITE_SOURCE_PREFIX)]
    site_reviews = [r for r in review_context["reviews"] if r["state"]=="saved" and r["payload"].get("origin","").startswith("Website")]
    site_sources = {s["source"] for s in site_notes} | {n["admitted_source"] for r in site_reviews for n in r["payload"]["entries"] if n.get("admitted_source")}
    site_read = bool(site_notes or site_reviews)
    auto_site = bool(website and kb.is_configured() and not site_read and not pending_reviews)
    # The interview opens itself: the page carries the ask for what's missing
    # and the panel sends it on arrival (services run 2026-09-04: the static
    # greeting waited for "what do you need from me?"). With a site on file the
    # ask never leads with "do you have a website?" — and while auto_site the
    # panel holds the ask until the read has finished (concierge.js), so the
    # assistant doesn't interview for prices the site is about to supply.
    coverage_ask = (knowledge_coverage.ask_text(cov["missing"],
                                                website=kb.is_configured() and not website)
                    if cov["missing"] else "")
    # Held for later: rules and facts stated at earlier steps arrive as a
    # ready note card (the panel renders it from this on arrival).
    from app import setup_held
    held_notes = setup_held.for_page(
        [i for i in (cfg.get("setup_held") or []) if isinstance(i, dict)], "knowledge")
    next_target = _next_step_target(ctx)
    return templates.TemplateResponse(request, "onboarding/knowledge.html", _step_ctx(
        "knowledge", sources=sources, configured=kb.is_configured(), coverage=cov,
        review_coverage=knowledge_review.topic_view(review_context), pending_reviews=pending_reviews,
        source_texts=text_by_source,
        coverage_ask=coverage_ask, site_url=website, auto_site=auto_site,
        site_done=site_read, site_note_count=len(site_sources), held_notes=held_notes,
        next_target=next_target,
        next_label="Finish setup" if next_target["href"] == "/app/" else "Next: " + next_target["label"],
        msg=request.query_params.get("msg"), err=request.query_params.get("err")))


@router.post("/knowledge/text", dependencies=[Depends(require_csrf)])
async def knowledge_text(request: Request, label: str = Form(""), content: str = Form(""),
                         then: str = Form(""), ctx: TenantCtx = Depends(require_tenant)):
    """Stage a pasted note for review. Source-review mode requires an explicit
    Review note action; Continue preserves unsaved text and pending drafts."""
    content = (content or "").strip()
    # Knowledge is the wizard's last step: Continue finishes setup (the
    # live-data step is off-flow since 2026-08-02).
    finishing = then == "continue"

    def stay(err: str):
        # Re-render with the note still in the box — an error must never eat typed work.
        sources = db.list_knowledge_sources(ctx.tenant_id, client=ctx.client)
        return templates.TemplateResponse(request, "onboarding/knowledge.html", _step_ctx(
            "knowledge", sources=sources, configured=kb.is_configured(),
            msg=None, err=err, prefill_label=label, prefill_content=content))

    if finishing and settings.knowledge_source_review_enabled:
        if content:
            return stay('Your pasted note is unsaved. Choose Review note, or clear the box before continuing. Your text is kept below.')
        from app import knowledge_review
        context = knowledge_review.load(ctx.tenant_id)
        form = await request.form()
        if any(r['state'] == 'pending' for r in context['reviews']) and form.get('knowledge_draft_decision') != 'keep':
            return _kb_redirect(err='Knowledge drafts remain pending. Review them or explicitly leave them for later. Nothing was saved.')
    if not content:
        return (_finish(ctx) if finishing else _kb_redirect(err="Type a note first, then Add note."))

    if not kb.is_configured():
        return stay("Knowledge base is off — set VOYAGE_API_KEY to enable it. "
                    "Your note is kept below; copy it somewhere safe.")
    # Unique fallback label: ingest replaces a source wholesale, so two notes both
    # called "Pasted note" would silently overwrite each other.
    source = label.strip() or f"Pasted note — {datetime.now(timezone.utc):%d %b %H:%M}"
    from app import knowledge_review
    try:
        row = knowledge_review.stage(ctx.tenant_id, [{"label": source, "content": content}])
    except HTTPException as exc:
        return stay(exc.detail)
    return RedirectResponse(f"/onboarding/knowledge/review/{row['id']}", status_code=303)



@router.post("/knowledge/upload", dependencies=[Depends(require_csrf)])
async def knowledge_upload(file: UploadFile = File(...), ctx: TenantCtx = Depends(require_tenant)):
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    name = file.filename or "upload"
    data = await file.read()
    if len(data) > kb.MAX_UPLOAD_BYTES:
        return _kb_redirect(err=f"{name} is too large (max 5 MB).")
    try:
        text = await asyncio.to_thread(kb.extract_text, name, data)
        from app import knowledge_review
        row = knowledge_review.stage(ctx.tenant_id, [{"label": name, "content": text}], origin="Uploaded file: " + name)
    except HTTPException:
        raise
    except Exception as e:
        return _kb_redirect(err=f"Could not read {name}: {e}")
    return RedirectResponse(f"/onboarding/knowledge/review/{row['id']}", status_code=303)



@router.post("/knowledge/website", dependencies=[Depends(require_csrf)])
async def knowledge_website(request: Request, url: str = Form(""), ctx: TenantCtx = Depends(require_tenant)):
    """Read the tenant's website and PROPOSE knowledge notes (JSON for the
    in-page review UI). Nothing is ingested here — the owner unticks/edits and
    Drafts and questions are stored for review; atomic admission writes facts
    only after evidence and overlap decisions are confirmed."""
    if settings.knowledge_source_review_enabled:
        from app.knowledge_source_routes import require_source_review
        require_source_review(ctx)
    if not kb.is_configured():
        return JSONResponse({"error": "Knowledge base is off — set VOYAGE_API_KEY to enable it."},
                            status_code=400)
    # Daily ceiling (2026-09-02 cost review): the crawl+distill is the single
    # most expensive onboarding action and this route had no guard at all.
    if not fairuse.onboarding_allowed(ctx.tenant_id, "kb_website", client=ctx.client):
        return JSONResponse({"error": "That's today's website-import allowance used — "
                                      "it resets at midnight (UK time). You can paste "
                                      "the key facts as notes below instead."},
                            status_code=429)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    try:
        result = await kb_website.build(ctx.tenant_id, url,
                                        business_name=t.get("name"), industry=t.get("industry"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logging.getLogger("blautom8.onboarding").warning(
            "kb_website build failed (tenant %s, url %r): %r", ctx.tenant_id, url, e)
        return JSONResponse({"error": "Could not read that site — check the address, "
                                      "or add your facts below instead."}, status_code=502)
    if result.get('distillation_error'):
        return JSONResponse({'error':result['distillation_error']},status_code=422)
    if not result["entries"]:
        return JSONResponse({"error": "Couldn't find usable facts on that site — "
                                      "upload a file or paste a few notes instead."},
                            status_code=422)
    from app import knowledge_review
    evidence_pages = result.pop('evidence_pages', None)
    if settings.knowledge_source_review_enabled:
        if evidence_pages is None:
            raise HTTPException(503, 'Source capture could not be confirmed. Nothing was staged.')
    row = knowledge_review.stage(ctx.tenant_id, result["entries"], origin="Website: " + result["site"], questions=result.get("questions"),source_units=result.get("source_units"), capture_manifest=result.get('capture_manifest'),
        **({'evidence_pages': evidence_pages} if evidence_pages is not None else {}))
    fragments = _knowledge_review_fragments(request, ctx)
    return JSONResponse({**result, **fragments, "review_id": row["id"]})


@router.post("/knowledge/website/save", dependencies=[Depends(require_csrf)])
async def knowledge_website_save(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    """Ingest the reviewed website notes. Fields are indexed (inc_i / label_i /
    content_i, i < count) because an unticked checkbox doesn't post — parallel
    getlist() arrays would silently misalign labels with contents."""
    if not kb.is_configured():
        return _kb_redirect(err="Knowledge base is off — set VOYAGE_API_KEY to enable it.")
    form = await request.form()
    try:
        count = min(int(form.get("count") or 0), kb_website.MAX_ENTRIES)
    except ValueError:
        count = 0
    entries = []
    for i in range(count):
        if not form.get(f"inc_{i}"):
            continue
        content = (form.get(f"content_{i}") or "").strip()
        if not content:
            continue
        label = (form.get(f"label_{i}") or "").strip() or f"Website note {i + 1}"
        entries.append((label, content))
    if not entries:
        return _kb_redirect(err="Nothing was ticked — tick at least one note to save it.")
    from app import knowledge_review
    row = knowledge_review.stage(ctx.tenant_id, [{"label": label, "content": content} for label,content in entries], origin="Website draft")
    return RedirectResponse(f"/onboarding/knowledge/review/{row['id']}", status_code=303)


def _knowledge_review_fragments(request, ctx):
    from app import knowledge_review
    context = knowledge_review.load(ctx.tenant_id)
    cov = knowledge_review.topic_view(context)
    base = {"request": request, "review_coverage": cov}
    reviews = [knowledge_review.review_view(context,r) for r in context["reviews"] if r["state"] == "pending"]
    return {"coverage_html": templates.env.get_template("_knowledge_coverage_review.html").render(**base),
            "review_html": "".join(templates.env.get_template("_knowledge_review.html").render(**base,review=r) for r in reviews)}


@router.get("/knowledge/reviews")
def knowledge_reviews(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    return JSONResponse(_knowledge_review_fragments(request,ctx), headers={"Cache-Control":"no-store"})


def _knowledge_review_row(context, review_id):
    row = next((r for r in context["reviews"] if r["id"] == review_id), None)
    if not row: raise HTTPException(404,"Review not found.")
    return row


@router.get("/knowledge/review/{review_id}")
def knowledge_review_get(request: Request, review_id: str, ctx: TenantCtx = Depends(require_tenant)):
    from app import knowledge_review
    context = knowledge_review.load(ctx.tenant_id)
    row = _knowledge_review_row(context,review_id)
    if row["state"] != "pending": return RedirectResponse(row["payload"].get("return_to","/onboarding/knowledge"),status_code=303)
    return templates.TemplateResponse(request,"knowledge_review.html",{"review":knowledge_review.review_view(context,row),
        "review_coverage":knowledge_review.topic_view(context)},headers={"Cache-Control":"no-store"})


@router.post("/knowledge/review/{review_id}",dependencies=[Depends(require_csrf)])
async def knowledge_review_save(request: Request, review_id: str, ctx: TenantCtx = Depends(require_tenant)):
    from app import knowledge_review
    context = knowledge_review.load(ctx.tenant_id)
    row = _knowledge_review_row(context,review_id)
    form = await request.form()
    if row['payload'].get('source_snapshot') and form.get('operation') != 'preview':
        from app.knowledge_source_routes import require_source_writer
        require_source_writer(ctx)
    try:
        if form.get("operation") == "discard":
            result = db.db().rpc("admit_knowledge_review",{"p_tenant":ctx.tenant_id,"p_review":review_id,
                "p_expected":form.get("version"),"p_payload":row["payload"],"p_notes":[],"p_discard":True}).execute().data
            if result is not True: raise HTTPException(409,"The review changed. Reload before discarding it.")
        elif form.get("operation") != "preview":
            admitted = await knowledge_review.admit(ctx.tenant_id,row,context,form,reviewer=getattr(ctx,"user_id",None))
            from app import setup_held
            if admitted:
                setup_held.mark_used(ctx.tenant_id, "knowledge", client=ctx.client)
        else:
            return templates.TemplateResponse(request,"knowledge_review.html",{"review":knowledge_review.review_view(context,row,form),
                "review_coverage":knowledge_review.topic_view(context)})
    except HTTPException as exc:
        return templates.TemplateResponse(request,"knowledge_review.html",{"review":knowledge_review.review_view(context,row,form),
            "review_coverage":knowledge_review.topic_view(context),"error":exc.detail},status_code=exc.status_code)
    except Exception:
        raise HTTPException(503,"The decision could not be confirmed. Reload to check before retrying.") from None
    return RedirectResponse(row["payload"].get("return_to","/onboarding/knowledge"),status_code=303)



# ── 5 · live data (system-of-record capture) ─────────────────────────────────
@router.get("/data")
def data_get(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    options = DATA_SOURCES.get(t.get("industry"), DATA_SOURCES["_default"])
    return templates.TemplateResponse(request, "onboarding/data.html",
                                      _step_ctx("data", options=options, industry=t.get("industry")))


@router.post("/data", dependencies=[Depends(require_csrf)])
async def data_post(request: Request, ctx: TenantCtx = Depends(require_tenant)):
    form = await request.form()
    def _blocking():
        items = []
        for kind in form.getlist("source"):
            vendor = (form.get(f"vendor_{kind}") or "").strip() or None
            items.append({"kind": kind, "vendor": vendor, "status": "requested"})
        db.set_data_sources(ctx.tenant_id, items, client=ctx.client)
        return _finish(ctx)
    return await run_in_threadpool(_blocking)


# ── finish (skip-the-rest) ───────────────────────────────────────────────────
@router.post("/finish", dependencies=[Depends(require_csrf)])
def finish(ctx: TenantCtx = Depends(require_tenant)):
    return _finish(ctx)
