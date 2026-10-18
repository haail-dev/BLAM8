"""Customer Portal — the public self-service page (docs/modules/module-portal.md).

Every BLAM8 business gets blam8.com/c/{portal_slug}: customers book against
the real diary, check and reschedule their bookings by reference code, get
quotes answered from the knowledge base, and anything the assistant can't
handle lands in the business's inbox. No customer login — the reference code
is the identity, exactly like /track/{token} only typeable.

Invite-only (Kris 2026-09-24): the portal serves customers the business
already knows. Every reply the business sends carries the customer's code
and link — that is the invitation. A visitor without one gets the code
emailed to an address on file, or nothing; the portal never creates a
customer, and an unverified visitor never reaches the model.

Security model, in one place:
  * tenant_id derives from the slug in the URL — never from the request body.
  * The chat's identity/writes are guarded in src/app/portal_data.py; this module
    adds the per-IP rate limits (messages 20/h, bookings 5/day — the spec's
    abuse ceilings) on top of the shared per-route limiter.
  * The page is public and unauthenticated: no CSRF (no ambient credentials
    to forge — same reasoning as POST /concierge/ask).

The reference code ({PREFIX}-{4 alnum}) is minted lazily: on first outbound
reply (the sign-off footer below) or first portal identification. The footer
rides the same wrap-at-send convention as src/app/disclosure.py — the stored
draft stays clean, only the outbound copy gains the chrome.
"""
import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import crm, db, llm, portal_chat, portal_data, storage, trace, usage
from app.config import settings
from app import security as _security
from app.security import _client_ip, rate_limit
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["portal"])

MAX_QUESTION_CHARS = 1000
MAX_HISTORY_TURNS = 16
MAX_TURN_CHARS = 4000

# Reference-code alphabet: unambiguous on a phone screen and over the phone —
# no O/0, no I/1.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")


# ── slug + reference-code minting ────────────────────────────────────────────
def slugify(name: str) -> str:
    """Business name → portal slug: lowercase, alnum+hyphens, trimmed."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40].strip("-") or "business"


def ref_prefix(name: str) -> str:
    """Business name → reference-code prefix: first word, uppercase alnum,
    max 6 chars ('Acme Plumbing & Heating' → 'ACME')."""
    for word in (name or "").split():
        w = re.sub(r"[^A-Za-z0-9]", "", word).upper()
        if w:
            return w[:6]
    return "REF"


def mint_code(prefix: str) -> str:
    # 6 symbols (security review 2026-09, H3): 32^6 ≈ 1 billion per business,
    # up from 32^4 ≈ 1 million — codes minted before stay 4 and keep working.
    return prefix + "-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


# ── signed portal links (H3) ─────────────────────────────────────────────────
# The emailed link carries CODE.SIGNATURE, not the bare code: the signature is
# an HMAC over (business, code) with a key derived from the server secret, so a
# guessed code alone never opens the personalised page from a link. The short
# code stays the human-typeable identifier, behind a strict guess budget.
def _link_key() -> bytes:
    import hashlib
    return hashlib.sha256(b"blam8-portal-link-v1|" + (settings.token_encryption_key or "").encode()).digest()


def link_token(tenant_id: str, code: str) -> str:
    import base64, hashlib, hmac as _hmac
    sig = _hmac.new(_link_key(), f"{tenant_id}|{code}".encode(), hashlib.sha256).digest()
    return f"{code}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')[:22]}"


def code_from_link(tenant_id: str, token: str) -> str | None:
    import hmac as _hmac
    code, _, _sig = (token or "").strip().rpartition(".")
    if not code or not _sig:
        return None
    return code if _hmac.compare_digest(link_token(tenant_id, code), token.strip()) else None


# A business's reference codes answer at most MISS_BUDGET wrong guesses an hour
# (every channel together: the page, the lookup API, the chat); past that,
# code lookups for that business pause for the rest of the hour and the run is
# traced. Per business, so no number of IPs gets round it.
MISS_BUDGET = 30
MISS_WINDOW_S = 3600
_MISSES: dict[str, list[float]] = {}


def reference_paused(tenant_id: str) -> bool:
    import time as _time
    now = _time.monotonic()
    hits = [t for t in _MISSES.get(tenant_id, []) if now - t < MISS_WINDOW_S]
    _MISSES[tenant_id] = hits
    return len(hits) >= MISS_BUDGET


def note_reference_miss(tenant_id: str) -> None:
    import time as _time
    hits = _MISSES.setdefault(tenant_id, [])
    hits.append(_time.monotonic())
    if len(hits) == MISS_BUDGET:
        log.warning("portal: reference-code guesses paused for tenant %s (%d misses in an hour)",
                    tenant_id, MISS_BUDGET)
        try:
            trace.emit(tenant_id, "portal_identity_verify", outcome="paused",
                       detail={"reason": "miss budget reached", "misses": MISS_BUDGET})
        except Exception:
            pass


def contact_for_code(tenant_id: str, code: str) -> dict | None:
    """THE reference-code check every channel uses: the per-business pause,
    the lookup, and the miss that counts toward it."""
    if not code or reference_paused(tenant_id):
        return None
    c = db.crm_contact_by_reference(tenant_id, code)
    if not c:
        note_reference_miss(tenant_id)
    return c




def ensure_portal_slug(tenant_id: str, name: str | None = None) -> str | None:
    """The tenant's portal slug, minted from the business name on first need.
    Collisions append -2..-9. Returns None only if every claim fails."""
    t = db.get_tenant(tenant_id) or {}
    if t.get("portal_slug"):
        return t["portal_slug"]
    base = slugify(name or t.get("name") or "")
    for candidate in [base] + [f"{base}-{i}" for i in range(2, 10)]:
        if db.set_portal_slug(tenant_id, candidate):
            return candidate
    log.warning("portal slug minting failed for tenant %s", tenant_id)
    return None


def ensure_reference(tenant_id: str, email: str, name: str | None = None) -> dict:
    """The contact row for an email address WITH a reference code — created
    and/or minted on first need. The one entry point both the portal identity
    tools and the sign-off footer use."""
    contact = db.crm_get_or_create_contact(tenant_id, email, name=name)
    if contact.get("reference_code"):
        return contact
    prefix = ref_prefix((db.get_tenant(tenant_id) or {}).get("name") or "")
    for _ in range(6):   # collision retry — 4 chars of 32 across one tenant
        code = mint_code(prefix)
        if db.set_contact_reference(contact["id"], tenant_id, code):
            contact["reference_code"] = code
            return contact
    log.warning("reference minting failed for contact %s", contact.get("id"))
    return contact


# ── outbound sign-off footer ─────────────────────────────────────────────────
def email_footer(tenant_id: str, cfg: dict | None, contact_email: str | None) -> str:
    """The 'Your reference / Manage your booking' block appended to outbound
    EMAIL replies (server-side, after the draft — the LLM never writes it).
    Best-effort and empty on any failure: a footer must never cost a send."""
    try:
        if not contact_email or not (cfg or {}).get("portal_enabled", True):
            return ""
        slug = ensure_portal_slug(tenant_id)
        if not slug:
            return ""
        contact = ensure_reference(tenant_id, contact_email)
        code = contact.get("reference_code")
        if not code:
            return ""
        base = (settings.app_base_url or "https://blam8.com").rstrip("/")
        url = f"{base}/c/{slug}?t={link_token(tenant_id, code)}"
        try:
            offers = db.offered_bookings_for_contact(tenant_id, contact_email)
        except Exception:
            log.warning("portal footer: held times unavailable", exc_info=True)
            offers = []
        choice = f"\nOr pick online: {url}" if offers else ""
        return (f"\n\n---\nYour reference: {code}\n"
                f"Manage your booking: {url}{choice}")
    except Exception:
        log.warning("portal footer failed (tenant=%s)", tenant_id, exc_info=True)
        return ""


# ── booking budget (5/day per visitor per business — spec §security) ─────────
_BOOK_BUCKETS: dict[tuple[str, str], list[float]] = {}
_BOOK_LIMIT, _BOOK_WINDOW_S = 5, 86400


def _bucket_allowed(buckets: dict, key, limit: int, window_s: int) -> bool:
    """Sliding-window counter shared by the booking budget and the
    verification-email limits. Records the hit when allowed."""
    now = time.monotonic()
    hits = [t for t in buckets.get(key, []) if now - t < window_s]
    if len(hits) >= limit:
        buckets[key] = hits
        return False
    hits.append(now)
    buckets[key] = hits
    return True


def _booking_allowed(tenant_id: str, ip: str) -> bool:
    return _bucket_allowed(_BOOK_BUCKETS, (tenant_id, ip), _BOOK_LIMIT, _BOOK_WINDOW_S)


# Reschedule budget (2026-07-31): same shape as the booking budget — before
# this, a session could move a booking repeatedly with only the chat limit.
_MOVE_BUCKETS: dict[tuple[str, str], list[float]] = {}
_MOVE_LIMIT, _MOVE_WINDOW_S = 5, 86400


def _move_allowed(tenant_id: str, ip: str) -> bool:
    return _bucket_allowed(_MOVE_BUCKETS, (tenant_id, ip), _MOVE_LIMIT, _MOVE_WINDOW_S)


# ── identity verification (hotfix 2026-07-26) ────────────────────────────────
# Name+email alone never links to an existing account — both are discoverable.
# Instead the reference code is EMAILED to the address on file (3 per email
# per hour, 5 per IP per hour) and the customer types it back; the code the
# customer possesses is the capability, exactly like Path 1.
_VERIFY_EMAIL_BUCKETS: dict[str, list[float]] = {}
_VERIFY_IP_BUCKETS: dict[str, list[float]] = {}


def _verify_allowed(email: str, ip: str) -> bool:
    # Check the IP ceiling first WITHOUT recording, so a refused email attempt
    # doesn't burn the IP budget; record both only when the send may proceed.
    now = time.monotonic()
    ip_hits = [t for t in _VERIFY_IP_BUCKETS.get(ip, []) if now - t < 3600]
    if len(ip_hits) >= 5:
        _VERIFY_IP_BUCKETS[ip] = ip_hits
        return False
    if not _bucket_allowed(_VERIFY_EMAIL_BUCKETS, email.lower(), 3, 3600):
        return False
    ip_hits.append(now)
    _VERIFY_IP_BUCKETS[ip] = ip_hits
    return True


async def send_verification_email(tenant_id: str, business_name: str,
                                  slug: str | None, contact: dict) -> bool:
    """Email the reference code to the address on file — the only channel the
    full code ever travels after this hotfix. False = not sent (no code, no
    connected mailbox, provider error): the assistant then points the customer
    at the business directly. Every request is audit-logged."""
    code = contact.get("reference_code")
    email = contact.get("email")
    if not code or not email:
        return False
    first = ((contact.get("name") or "").split() or ["there"])[0]
    base = (settings.app_base_url or "https://blam8.com").rstrip("/")
    # ?ref= makes the click land personalised and pre-verified — the reader
    # of this email possesses the code anyway (it's two lines up).
    portal_url = f"{base}/c/{slug}?ref={code}" if slug else base
    body = (f"Hi {first},\n\n"
            f"You requested your reference code on the {business_name} "
            "customer portal.\n\n"
            f"Your reference code is: {code}\n\n"
            "You can use this code to check and manage your bookings at:\n"
            f"{portal_url}\n\n"
            "If you didn't request this, you can ignore this email — no "
            "changes have been made to your account.\n\n"
            "---\nPowered by BLAM8")
    try:
        from app import mailbox
        await mailbox.send_mail(tenant_id, email,
                                f"Your {business_name} reference code", body)
    except Exception as e:
        log.warning("portal verification email failed (tenant=%s)", tenant_id,
                    exc_info=True)
        # The provider-error case, distinct from find_contact's send_failed
        # summary row: THIS one says the mail provider raised.
        trace.emit(tenant_id, "portal_verification_email", outcome="provider_error",
                   contact=email, detail={"exception": repr(e)[:300]})
        return False
    try:
        db.log_agent_event(
            tenant_id, "portal_verification",
            f"Reference code emailed to {email} (requested on the customer "
            "portal)", contact=email)
    except Exception:
        pass
    return True


# ── chat session tokens (hotfix 2026-07-26) ──────────────────────────────────
# Sticky identity across chat turns used to be the reference code itself in
# the done-event — which put the full code on the wire and in sessionStorage.
# Now a verified turn mints an opaque single-tenant token (in-memory,
# per-process like the rate buckets; 6h TTL) and the client replays that.
_SESSIONS: dict[str, tuple[str, str, float]] = {}
_SESSION_TTL_S = 6 * 3600


def _mint_session(tenant_id: str, contact_id: str) -> str:
    now = time.monotonic()
    for tok in [t for t, (_, _, exp) in _SESSIONS.items() if exp < now]:
        _SESSIONS.pop(tok, None)
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = (tenant_id, contact_id, now + _SESSION_TTL_S)
    return token


def _resolve_session(tenant_id: str, token: str) -> dict | None:
    entry = _SESSIONS.get((token or "").strip())
    if not entry:
        return None
    tid, contact_id, expires = entry
    if tid != tenant_id or expires < time.monotonic():
        return None
    try:
        return db.crm_get_contact(contact_id, tenant_id)
    except Exception:
        return None


# ── page context ─────────────────────────────────────────────────────────────
def _portal_tenant(slug: str) -> tuple[dict | None, dict]:
    """(tenant, cfg) for a live portal slug — None tenant when the slug is
    unknown OR the owner switched the portal off (one 404 page for both:
    an off switch shouldn't confirm the business exists)."""
    t = db.get_tenant_by_portal_slug(slug)
    if not t:
        return None, {}
    cfg = db.get_tenant_config(t["id"]) or {}
    if not cfg.get("portal_enabled", True):
        return None, {}
    return t, cfg


def _services(tenant_id: str) -> list[str]:
    """Service names for the landing page, from the diary's service types —
    best-effort decoration only."""
    try:
        sched = db.get_scheduling_config(tenant_id) or {}
        types = sched.get("service_types") or []
        return [s.get("name") for s in types if isinstance(s, dict) and s.get("name")][:12]
    except Exception:
        return []


_WINDOW_WORDS = {"am": "in the morning", "pm": "in the afternoon", "day": ""}


def _booking_phrase(b: dict, customer_name: str | None = None) -> str:
    """'Annual boiler service — Thursday 31 July in the morning (BK-1001)' —
    the personalised-landing line for a customer arriving via their email
    link. Best-effort formatting; a weird row degrades to its ISO date."""
    from datetime import date as _date
    when = b.get("booking_date") or ""
    try:
        d = _date.fromisoformat(when)
        when = d.strftime("%A %d %B").replace(" 0", " ")
    except ValueError:
        pass
    window = (b.get("window") or "").strip()
    window_words = _WINDOW_WORDS.get(window, f"at {window}" if window else "")
    summary = (b.get("summary") or "your appointment").strip()
    # Agent-created bookings bake the customer's name into the summary
    # ("Boiler service — Velox Velcee") — greeting them with their own name
    # twice reads odd, so strip that trailing decoration here.
    if customer_name:
        summary = re.sub(r"\s*[—–-]+\s*" + re.escape(customer_name.strip()) + r"$",
                         "", summary, flags=re.IGNORECASE).strip() or summary
    ref = f" ({b['booking_ref']})" if b.get("booking_ref") else ""
    return " ".join(x for x in (f"{summary} —", when, window_words) if x) + ref


@router.get("/c/{slug}", response_class=HTMLResponse)
def portal_page(request: Request, slug: str,
                _=Depends(rate_limit("portal_page", limit=60, window_s=60))):
    t, cfg = _portal_tenant(slug.strip().lower())
    if not t:
        return templates.TemplateResponse(request, "portal.html", {"t": None},
                                          status_code=404)
    scheduling_on = "scheduling" in db.visible_modules(t["id"])
    customer = None
    # A signed link (?t=, every email since 2026-09-27) opens without a guess
    # budget; a bare ?ref= (older emails, a typed code) counts against the
    # business's miss budget and this IP's 12 misses an hour (H3).
    c = None
    signed = code_from_link(t["id"], request.query_params.get("t") or "")
    ref = (request.query_params.get("ref") or "").strip().upper()[:20]
    if signed:
        c = db.crm_contact_by_reference(t["id"], signed)
    elif ref and _security.count_hits("portal_ref_miss", request, 3600) < 12:
        c = contact_for_code(t["id"], ref)
        if not c:
            _security.note_hit("portal_ref_miss", request, 3600)
    if signed or ref:
        if c:
            # Personalised landing (the email link carries ?ref=): greet by
            # name and lead with their next booking — the likeliest reason
            # they clicked. Possessing the code IS the identity (Path 1), so
            # showing their own booking here reveals nothing new.
            customer = {"name": c.get("name") or "",
                        "reference_code": c.get("reference_code")}
            try:
                from datetime import date as _date
                today = _date.today().isoformat()
                upcoming = ([b for b in
                             db.bookings_for_contact(t["id"], c["email"])
                             if (b.get("booking_date") or "") >= today]
                            if c.get("email") else [])
                if not upcoming and c.get("email"):
                    # Party-aware landing (module-job-parties.md Phase 3): an
                    # on-site party's next visit greets them too — the chat
                    # already shows it, the landing must not say "nothing
                    # upcoming". Trusted rows only (D4 lives in the db read).
                    onsite = [m["booking_id"] for m in
                              db.job_party_memberships(t["id"], c["email"])
                              if m.get("on_site") and m.get("booking_id")]
                    upcoming = [b for b in
                                db.bookings_by_ids(t["id"], onsite)
                                if (b.get("booking_date") or "") >= today
                                and b.get("status") in ("confirmed", "proposed")
                                ] if onsite else []
                if upcoming:
                    customer["next_booking"] = _booking_phrase(
                        upcoming[0], customer_name=c.get("name"))
            except Exception:
                log.warning("portal landing: bookings read failed", exc_info=True)
    # ?intent=book (a website's "Book now" button) STAGES the booking ask in
    # the composer rather than auto-sending it — every portal turn is an LLM
    # call against the tenant's fair-use cap, so a crawler hitting the deep
    # link must cost nothing. The first human tap sends it.
    held_times = []
    if signed and c and c.get("email") and scheduling_on:
        from app.offer_deadlines import display, slot_label
        held_times = [{"id": b["id"], "label": slot_label(b), **display(b)}
                      for b in db.offered_bookings_for_contact(t["id"], c["email"])]
    prefill = ("I'd like to book a service"
               if request.query_params.get("intent") == "book" and scheduling_on
               and customer else "")
    return templates.TemplateResponse(request, "portal.html", {
        "t": t,
        "slug": t["portal_slug"],
        "banner_url": storage.banner_public_url(cfg.get("portal_banner_path")),
        "description": cfg.get("portal_description") or "",
        "service_area": cfg.get("portal_service_area") or "",
        "welcome": cfg.get("portal_welcome") or "",
        "services": _services(t["id"]),
        "scheduling_on": scheduling_on,
        # 5b nit: the "My notes" chip rides only when the log is on.
        "customer_notes_on": cfg.get("portal_customer_notes") is not False,
        "customer": customer,
        "held_times": held_times,
        "offer_token": request.query_params.get("t") if signed else "",
        "prefill": prefill,
        "ref_prefix": ref_prefix(t.get("name") or ""),
    })


class ChooseOfferRequest(BaseModel):
    token: str = Field(..., max_length=100)
    booking_id: str = Field(..., max_length=36)


@router.post("/api/portal/{slug}/choose",
             dependencies=[Depends(rate_limit("portal_choose", limit=20, window_s=3600))])
async def choose_offer(slug: str, body: ChooseOfferRequest) -> dict:
    t, cfg = _portal_tenant(slug)
    if not t:
        return {"error": "not_found"}
    code = code_from_link(t["id"], body.token)
    contact = db.crm_contact_by_reference(t["id"], code) if code else None
    if not contact or not contact.get("email"):
        return {"error": "not_identified", "message": "Please use the link in your email."}
    if "scheduling" not in db.visible_modules(t["id"]):
        return {"error": "unsupported", "message": "Please contact the business to choose a time."}
    session = portal_data.PortalSession(tenant_id=t["id"], business_name=t.get("name") or "",
        contact=contact, scheduling_on=True,
        confirmation_on=cfg.get("portal_booking_confirmation") is not False)
    return await portal_data.choose_held_time(session, body.booking_id)


# What the BROWSER may hold of the identified customer (security review
# 2026-09, H7: the lookup JSON and the chat's done event carried the model's
# view — every record field incl. health notes and children's dates of birth —
# to anyone holding the reference code). The model still reads the full record
# server-side; the browser gets who they are and the session handle, nothing else.
_BROWSER_CUSTOMER_KEYS = ("name", "reference_masked", "session")


def for_browser(customer: dict | None) -> dict | None:
    if not customer:
        return customer
    return {k: customer[k] for k in _BROWSER_CUSTOMER_KEYS if k in customer}


# ── lookup (reference code or name+email) ────────────────────────────────────
class LookupRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=60)
    ref: str = Field("", max_length=20)
    name: str = Field("", max_length=80)
    email: str = Field("", max_length=254)


@router.post("/api/portal/lookup",
             dependencies=[Depends(rate_limit("portal_lookup", limit=12, window_s=3600))])
def portal_lookup(body: LookupRequest) -> dict:
    """Reference-code (or name+email) lookup → the matching customer's own
    upcoming bookings. Tenant-scoped by slug: a code from another business
    never resolves here."""
    t, _cfg = _portal_tenant(body.slug.strip().lower())
    if not t:
        return {"found": False}
    session = portal_data.PortalSession(tenant_id=t["id"],
                                        business_name=t.get("name") or "")
    if body.ref.strip():
        out = portal_data._lookup_reference(session, {"code": body.ref})
        return {"found": out.get("found"), "customer": for_browser(out.get("customer")),
                "upcoming_bookings": out.get("upcoming_bookings")}
    if body.name.strip() and body.email.strip():
        # Identity hotfix 2026-07-26: name+email are discoverable, so they
        # never unlock account data here — the chat's email-verification
        # path is the only route without a code.
        return {"found": False, "verification_required": True}
    return {"found": False}


# ── general entry point (/for-customers) ─────────────────────────────────────
# The public customer entry page needs two slug-less lookups: reference code →
# the business's portal, and business name → portal links. Both reveal only
# which businesses have a live portal (the code's prefix already names the
# business by design) — never customer data; identity stays the portal page's
# job. Anything ambiguous is "not found".
_ENTRY_CODE_RE = re.compile(r"^[A-Z0-9]{1,6}-[A-Z0-9]{4,6}$")


class EntryRefRequest(BaseModel):
    ref: str = Field(..., min_length=1, max_length=20)


@router.post("/api/portal/entry/ref",
             dependencies=[Depends(rate_limit("entry_ref", limit=12, window_s=3600))])
def entry_ref_lookup(body: EntryRefRequest) -> dict:
    code = body.ref.strip().upper()
    if not _ENTRY_CODE_RE.match(code):
        return {"found": False}
    matches = []
    for t in db.tenants_for_reference_code(code):
        slug = t.get("portal_slug")
        if slug and (db.get_tenant_config(t["id"]) or {}).get("portal_enabled", True):
            matches.append({"name": t.get("name") or "", "slug": slug})
    if len(matches) != 1:   # 0 = unknown; 2+ = cross-tenant collision, don't guess
        return {"found": False}
    return {"found": True, "business": matches[0]["name"],
            "url": f"/c/{matches[0]['slug']}?ref={code}"}


class EntrySearchRequest(BaseModel):
    q: str = Field(..., min_length=1, max_length=80)


@router.post("/api/portal/entry/search",
             dependencies=[Depends(rate_limit("entry_search", limit=30, window_s=60))])
def entry_search(body: EntrySearchRequest) -> dict:
    q = body.q.strip()
    if len(q) < 2:
        return {"results": []}
    results = []
    for t in db.search_portal_tenants(q):
        if (db.get_tenant_config(t["id"]) or {}).get("portal_enabled", True):
            results.append({"name": t.get("name") or "",
                            "url": f"/c/{t['portal_slug']}"})
        if len(results) >= 6:
            break
    return {"results": results}


# ── chat ─────────────────────────────────────────────────────────────────────
class ChatTurn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=60)
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    history: list[ChatTurn] = Field(default_factory=list)
    # The visitor's saved identity, re-verified server-side every request:
    # `ref` is a reference code the visitor themselves supplied (?ref= link);
    # `session` is the opaque token a verified turn minted. A forged value of
    # either simply fails to resolve.
    ref: str = Field("", max_length=20)
    session: str = Field("", max_length=64)


_TOOL_HINTS = {
    "search_knowledge_base": "Checking our services and prices…",
    "lookup_reference": "Looking up your reference…",
    "find_contact": "Finding your details…",
    "list_my_bookings": "Checking your bookings…",
    "check_availability": "Checking the diary…",
    "create_booking": "Booking you in…",
    "move_booking": "Moving your booking…",
    "escalate_to_business": "Passing this to the team…",
    "list_my_invoices": "Checking your invoices…",
    "list_my_quotes": "Checking your quotes…",
}
# Settled pill labels for the ghost trail the page keeps after each answer.
_TOOL_LABELS = {
    "search_knowledge_base": "Checked services and prices",
    "lookup_reference": "Looked up your reference",
    "find_contact": "Found your details",
    "list_my_bookings": "Checked your bookings",
    "check_availability": "Checked the diary",
    "create_booking": "Booked you in",
    "move_booking": "Moved your booking",
    "escalate_to_business": "Passed this to the team",
    "list_my_invoices": "Checked your invoices",
    "list_my_quotes": "Checked your quotes",
}
# Settled labels for ACTION tools whose call-time label would otherwise claim
# success the result refused (live e2e 2026-09-03: "Booked you in" on an
# invalid_window). Read-tools keep their neutral labels — "Checked the diary"
# is honest whatever came back.
_TOOL_FAIL_LABELS = {
    "create_booking": "Couldn't book that",
    "move_booking": "Couldn't move that booking",
    "escalate_to_business": "Couldn't pass that on",
    "lookup_reference": "That code didn't match",
    "find_contact": "Couldn't match those details",
}
# Coaching errors the model routinely recovers from in the SAME turn (the
# intake gate listing required details, the identity gate) read as neutral
# steps, not failures — "Couldn't book that … Booked you in" confused a
# customer whose booking succeeded first try from their side (Julius
# 2026-09-03). If the model does NOT recover, the neutral label still never
# claims success.
_TOOL_SOFT_LABELS = {
    "missing_info": "Needed a few more details",
    "not_identified": "Needed to check who's asking",
    # The cross-session guards (2026-09-03): factual outcomes, not failures.
    "already_booked": "Already in the diary",
    "booking_limit": "Booking limit reached",
}


def _settled_label(name: str, out) -> str | None:
    """The pill's honest settled label for one tool RESULT — None keeps the
    call-time label (success, or a read-tool whose neutral label is already
    honest)."""
    if not (isinstance(out, dict) and out.get("error")):
        return None
    return _TOOL_SOFT_LABELS.get(out["error"]) or _TOOL_FAIL_LABELS.get(name)
# Pill click-through detail, PER-TOOL allowlisted. Deliberately tight: identity
# args (reference codes, emails) must never echo into the page — the identity
# hotfix's "codes never ride the chat wire" rule applies to the trail too.
_TOOL_DETAIL_KEYS = {"search_knowledge_base": "query", "check_availability": "date"}


def _portal_tool_detail(name: str, args: dict) -> str:
    key = _TOOL_DETAIL_KEYS.get(name)
    if not key:
        return ""
    if name == "check_availability":
        # Whichever the diary flavour passed: a stay's arrival, or a range's
        # first date.
        val = (args or {}).get("arrival") or (args or {}).get("date_from") \
            or (args or {}).get(key)
    else:
        val = (args or {}).get(key)
    return str(val or "").strip()[:80]


# Turns that consulted these tools are real questions worth the CRM timeline;
# identity handshakes and thank-yous stay out.
_QA_TOOLS = frozenset({"search_knowledge_base", "check_availability",
                       "list_my_invoices", "list_my_quotes"})


def _note_chat_activity(session: "portal_data.PortalSession", question: str,
                        tools_called: list) -> None:
    """CRM visibility for portal Q&A (2026-07-31): an identified customer's
    question joins their contact timeline, so the business — and the drafter
    reading crm_context — can see what they've been asking. Only turns that
    consulted business data land, and turns whose action already wrote its own
    feed row (booking / move / request / escalation) don't double up. The
    contact's reference code is masked out of the stored question (the code
    never persists readable — same rule as the chat wire). Best-effort: a CRM
    hiccup never costs the turn."""
    try:
        contact = session.contact or {}
        if not contact.get("id"):
            return
        if (session.bookings_made or session.moves_made
                or session.move_requests or session.escalations):
            return
        if not (_QA_TOOLS & set(tools_called or ())):
            return
        q = (question or "").strip()
        code = (contact.get("reference_code") or "").strip()
        if code and len(code) > 4:
            q = re.sub(re.escape(code), portal_data.mask_code(code), q,
                       flags=re.IGNORECASE)
        if q:
            crm.note_contact_activity(
                session.tenant_id, contact, "portal_chat",
                summary=f"Asked on the portal: {q[:200]}")
    except Exception:
        log.warning("portal chat: CRM activity failed", exc_info=True)


def _store_chat_turns(session: "portal_data.PortalSession", question: str,
                      reply: str) -> None:
    """Portal transcript (Kris 2026-08-31: "the inbox has no idea of the Portal
    conversation"). Both sides of an IDENTIFIED customer's turn persist against
    their contact email — the inbox thread interleaves them and the drafter
    replays them as context for the next email. Unlike the CRM note this keeps
    EVERY identified turn (bookings, escalations, thank-yous included) — it's
    the record, not the highlight reel. Anonymous turns never land; reference
    codes are masked out first (codes never persist readable). Best-effort."""
    try:
        contact = session.contact or {}
        email = (contact.get("email") or "").strip()
        if not email:
            return
        code = (contact.get("reference_code") or "").strip()

        def _mask(text: str) -> str:
            t = (text or "").strip()
            if code and len(code) > 4:
                t = re.sub(re.escape(code), portal_data.mask_code(code), t,
                           flags=re.IGNORECASE)
            return t[:2000]

        db.insert_portal_messages(session.tenant_id, email, [
            {"role": "customer", "content": _mask(question)},
            {"role": "assistant", "content": _mask(reply)},
        ])
    except Exception:
        log.warning("portal chat: transcript store failed", exc_info=True)


# ── the identity switch (Julius, live e2e 2026-09-01) ───────────────────────
# Identity is a deterministic gate, not a conversation: a typed code or email
# is settled here for zero tokens. Since the portal went invite-only (Kris
# 2026-09-24) the gate is the WHOLE unverified experience — the model only
# runs once the visitor is a verified customer.
_CODE_SHAPE = re.compile(r"^[A-Z0-9]{2,6}-[A-Z0-9]{4,6}$")
_EMAIL_IN_TEXT = re.compile(r"[^@\s<>(),;:\"']+@[^@\s<>(),;:\"']+\.[A-Za-z]{2,}")


def _is_identity_turn(text: str) -> bool:
    """A turn that was only the visitor identifying themselves — a code, or
    an email address — rather than something they wanted done."""
    s = (text or "").strip()
    return bool(_CODE_SHAPE.match(re.sub(r"[\s.]+", "", s).upper())
                or _EMAIL_IN_TEXT.search(s))


def _pending_question(history: list[dict]) -> str | None:
    """The question the invite gate held back, if any: the most recent user
    turn that wasn't itself a code or an email. Verifying should ANSWER it,
    not make the customer ask twice (Julius, live e2e 2026-09-01)."""
    for turn in reversed(history or []):
        if turn.get("role") != "user":
            continue
        content = (turn.get("content") or "").strip()
        if content and not _is_identity_turn(content):
            return content
    return None


def _gate_reply(business_name: str) -> str:
    prefix = ref_prefix(business_name)
    return (f"This page is for {business_name}'s customers. To carry on, type "
            f"the reference code from any email from us (it looks like "
            f"{prefix}-XXXX), or the email address we have for you and I'll "
            f"send it there.")


async def _identity_switch(question: str, history: list[dict], session,
                           tenant_id: str, business_name: str):
    """The invite gate (Kris 2026-09-24: the portal is invite-only). An
    unverified visitor never reaches the model — no answers, no bookings,
    no spend — until they prove they are a customer the business already
    knows: a reference code they typed, or an email on file that the code
    is sent to.

    Returns (reply, customer, trail, resume_question). A non-None
    resume_question means: verified — now run the model on the question the
    gate held back, instead of returning the canned reply."""
    from app import portal_data
    bare = re.sub(r"[\s.]+", "", question).upper()
    if _CODE_SHAPE.match(bare):
        out = portal_data._lookup_reference(session, {"code": bare})
        trail = [{"name": "lookup_reference",
                  "label": "Looked up your reference", "detail": ""}]
        if out.get("found"):
            pending = _pending_question(history)
            customer = portal_data._contact_view(session)
            if session.contact.get("id"):
                minted = _mint_session(tenant_id, session.contact["id"])
                customer["session"] = minted
                session.trace_session = (session.trace_session
                                         or trace.session_ref(minted))
            if pending:
                # Verified with a question waiting: continue, don't celebrate.
                return None, customer, trail, pending
            first = ((session.contact.get("name") or "").split() or [""])[0]
            nxt = out.get("upcoming_bookings") or []
            reply = (f"Thanks{', ' + first if first else ''} — you're verified. "
                     + (f"Your next booking: {nxt[0].get('summary') or nxt[0].get('date')}. "
                        if nxt else "")
                     + "Ask me about your bookings, invoices or quotes.")
            return reply, customer, trail, None
        # _lookup_reference's message is phrased for the model; this one is
        # for the customer directly.
        return ("That code doesn't match our records — double-check the email "
                "it came in, or type the email address we have for you and "
                "I'll send it again."), None, trail, None
    m = _EMAIL_IN_TEXT.search(question)
    if m:
        out = await portal_data._find_contact(session, {"email": m.group(0)})
        trail = [{"name": "find_contact",
                  "label": "Checked our customer list", "detail": ""}]
        if out.get("error") == "verification_limit":
            reply = ("That's a lot of code requests — please try again in an "
                     f"hour, or email {business_name} directly.")
        elif out.get("error") == "invalid_details":
            reply = "That email address doesn't look right — could you check it?"
        else:
            # One answer whether or not the address is on file, so the gate
            # never tells a stranger who is a customer here.
            reply = (f"If that's an address {business_name} has for you, your "
                     f"reference code is on its way — check your inbox and type "
                     f"it here. Nothing arriving? Email {business_name} directly "
                     f"and their reply will carry it.")
        return reply, None, trail, None
    return _gate_reply(business_name), None, [], None


@router.post(
    "/api/portal/chat",
    dependencies=[
        Depends(rate_limit("portal_chat_burst", limit=8, window_s=60)),
        # The spec's ceiling: 20 messages per visitor-hour.
        Depends(rate_limit("portal_chat", limit=20, window_s=3600)),
    ],
)
async def portal_chat_endpoint(body: ChatRequest, request: Request) -> StreamingResponse:
    """One customer chat turn. Streams newline-JSON like /concierge/business:
      {"type": "tool", "hint", "label", "detail"}     — a lookup just started
      {"type": "done", "reply", "customer", "trail"}  — always last; trail =
        [{name, label, detail}], the page keeps it as pills (per-visit only)."""
    import asyncio
    import json

    from app import concierge

    t, cfg = _portal_tenant(body.slug.strip().lower())
    if not t:
        # Same degrade-not-500 contract as the concierge routes.
        async def _gone():
            yield json.dumps({"type": "done",
                              "reply": "This business's portal isn't available.",
                              "customer": None}) + "\n"
        return StreamingResponse(_gone(), media_type="application/x-ndjson")

    tenant_id = t["id"]
    business_name = t.get("name") or "the business"
    question = body.question.strip()[:MAX_QUESTION_CHARS]
    history = concierge.sanitize_history(
        [turn.model_dump() for turn in body.history], MAX_HISTORY_TURNS, MAX_TURN_CHARS)
    scheduling_on = "scheduling" in db.visible_modules(tenant_id)
    show_prices = bool(cfg.get("portal_show_prices", True))
    reschedule_mode = ("approve"
                       if (cfg.get("portal_reschedule_mode") or "auto") == "approve"
                       else "auto")
    ip = _client_ip(request)

    async def _send_verification(contact: dict) -> bool:
        return await send_verification_email(tenant_id, business_name,
                                             t.get("portal_slug"), contact)

    session = portal_data.PortalSession(
        tenant_id=tenant_id, business_name=business_name,
        scheduling_on=scheduling_on, show_prices=show_prices,
        reschedule_mode=reschedule_mode,
        # Absent column / NULL = default ON, same contract as show_prices.
        confirmation_on=(cfg.get("portal_booking_confirmation") is not False),
        # The customer's own log (slice 5): default ON, the business may switch it off.
        notes_on=(cfg.get("portal_customer_notes") is not False),
        max_bookings=int(cfg.get("portal_max_active_bookings") or 0),
        booking_allowed=lambda: _booking_allowed(tenant_id, ip),
        move_allowed=lambda: _move_allowed(tenant_id, ip),
        ensure_reference=ensure_reference,
        verify_allowed=lambda email: _verify_allowed(email, ip),
        send_verification=_send_verification,
        # Observability: the visitor handle every trace row of this turn
        # carries — a hash of the session token, never the token itself.
        trace_session=trace.session_ref(body.session))
    # Sticky identity across turns, re-verified server-side every request:
    # the opaque session token from an earlier verified turn, or a reference
    # code the visitor supplied themselves (?ref= link / typed earlier).
    if body.session.strip():
        contact = _resolve_session(tenant_id, body.session)
        if contact:
            session.contact = contact
    if not session.contact and body.ref.strip():
        contact = contact_for_code(tenant_id, body.ref.strip().upper())   # miss budget (H3)
        if contact:
            session.contact = contact

    # The invite gate — see _identity_switch above. Unverified visitors
    # always get a canned reply; only a verified customer reaches the model.
    pre_trail: list[dict] = []
    if not session.contact:
        canned = await _identity_switch(question, history, session, tenant_id,
                                        business_name)
        if canned is not None:
            reply, customer, trail0, resume = canned
            if resume is not None:
                # Verified mid-conversation with a question waiting: run the
                # model on THAT question — the customer shouldn't ask twice.
                question = resume
                pre_trail = trail0
            else:
                trace.emit(tenant_id, "portal_chat",
                           session_id=session.trace_session,
                           contact=(session.contact or {}).get("email"),
                           detail={"message": question[:300], "reply": reply[:300],
                                   "tools_called": [x["name"] for x in trail0],
                                   "identified": bool(session.contact),
                                   "identity_switch": True})
                _store_chat_turns(session, question, reply)

                async def _canned_stream():
                    for item in trail0:
                        yield json.dumps({"type": "tool", "hint": "One moment…",
                                          **item}, ensure_ascii=False) + "\n"
                    yield json.dumps({"type": "done", "reply": reply,
                                      "customer": for_browser(customer), "trail": trail0},
                                     ensure_ascii=False) + "\n"
                return StreamingResponse(_canned_stream(),
                                         media_type="application/x-ndjson")

    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)")
    system = portal_chat.build_portal_prompt(
        # Identity state rides the prompt (Kris 2026-08-31): the ?ref link /
        # session token verified this visitor before the model ran — say so,
        # or the model re-asks a customer it just greeted by name.
        verified_name=((session.contact or {}).get("name")
                       or (session.contact or {}).get("email")
                       if session.contact else None),
        business_name=business_name, today=today, scheduling_on=scheduling_on,
        show_prices=show_prices,
        reschedule_approve=(reschedule_mode == "approve"),
        service_area=(cfg.get("portal_service_area") or None),
        services=_services(tenant_id) or None,
        # The tenant's diary flavour steers the booking rules AND the diary
        # tool shapes below — a stay tenant's portal books arrival→departure
        # spans exactly like its email agent (live e2e 2026-09-03).
        diary_flavour=(portal_data._flavour(session)[0]
                       if scheduling_on else "windows"),
        # What the CRM already holds on a pre-verified visitor, so the chat
        # never re-asks for details on file (a mid-turn verification gets the
        # same lines via the identity tools' on_file instead). The customer's
        # record and the facts from ONE earlier job go in SEPARATELY: "never
        # re-ask for this" is right for the first and wrong for the second.
        customer_profile=(portal_data._profile_sections(session)[0]
                          if session.contact else None),
        customer_job_facts=(portal_data._profile_sections(session)[1]
                            if session.contact else None),
        # With the confirmation email ON the assistant may honestly say one is
        # coming; OFF keeps the never-promise rule.
        booking_confirmation=(scheduling_on and session.confirmation_on))

    queue: asyncio.Queue = asyncio.Queue()
    tools_called: list[str] = []
    trail: list[dict] = []   # ghost trail: [{name, label, detail}] per lookup
    # A resumed turn's identity lookup leads the trail — the customer sees
    # "Looked up your reference" settle before the answer's own pills.
    for item in pre_trail:
        tools_called.append(item["name"])
        trail.append(item)
        queue.put_nowait({"type": "tool", "hint": "One moment…", **item})

    async def _on_tool(name: str, args: dict) -> None:
        tools_called.append(name)
        detail = _portal_tool_detail(name, args)
        # Approve-first reschedules: the pill must not claim a move happened —
        # the tool passes a request to the business instead.
        if name == "move_booking" and reschedule_mode == "approve":
            hint, label = ("Sending your request to the team…",
                           "Passed your reschedule request to the team")
        else:
            hint = _TOOL_HINTS.get(name, "One moment…")
            label = _TOOL_LABELS.get(name, "Checked our information")
        trail.append({"name": name, "label": label, "detail": detail})
        await queue.put({"type": "tool", "name": name,
                         "hint": hint, "label": label, "detail": detail})

    async def _on_result(name: str, out) -> None:
        # Honest pills (live e2e 2026-09-03): "Booked you in" settled on a
        # create_booking that returned invalid_window. Once the result is in,
        # an action tool that ERRORED corrects its pill — the page swaps the
        # label on the matching live pill, and the stored trail row follows.
        # Recoverable coaching errors settle NEUTRAL (_TOOL_SOFT_LABELS).
        label = _settled_label(name, out)
        if not label:
            return
        for item in reversed(trail):
            if item["name"] == name:
                item["label"] = label
                break
        await queue.put({"type": "tool_done", "name": name, "label": label})

    async def _executor(name: str, args: dict) -> dict:
        return await portal_data.execute(session, name, args)

    async def _run() -> None:
        # One request scope over the whole turn: every tool-loop llm_calls row
        # AND every trace row the executors emit share one request_id.
        t0 = time.monotonic()
        with llm.request_scope():
            await _run_scoped(t0)

    async def _run_scoped(t0: float) -> None:
        try:
            reply, totals, model = await portal_chat.answer_portal(
                question, history, system=system,
                tools=portal_data.tools_for(scheduling_on, session),
                tool_executor=_executor, on_tool=_on_tool,
                on_result=_on_result,
                # Re-resolved before every call in the loop: unverified calls'
                # transcripts land on the platform tenant, and flip to the
                # business the moment a tool verifies the visitor mid-turn.
                tenant_id=lambda: (tenant_id if session.contact
                                   else db.PLATFORM_TENANT_ID))
            # Who pays for THIS turn (2026-07-26 directive): the business only
            # when a verified customer actually touched business data; anything
            # anonymous, and a verified customer's platform/general questions,
            # is BLAM8's cost — recorded against the platform sentinel tenant
            # so "what does the portal cost us?" is one query, not a null hunt.
            bill_op = portal_chat.billing_operation(bool(session.contact),
                                                    tools_called)
            bill_to = tenant_id if bill_op == "portal_chat" else db.PLATFORM_TENANT_ID
            usage.record_anthropic(bill_to, model, totals,
                                   operation=bill_op, module="portal")
            customer = None
            if session.contact:
                # Masked view only + an opaque session token — the full
                # reference code never rides the chat wire (hotfix 2026-07-26).
                customer = portal_data._contact_view(session)
                if session.contact.get("id"):
                    minted = _mint_session(tenant_id, session.contact["id"])
                    customer["session"] = minted
                    # A turn that verified mid-conversation gets its handle
                    # from the token it just minted.
                    session.trace_session = (session.trace_session
                                             or trace.session_ref(minted))
            trace.emit(tenant_id, "portal_chat",
                       session_id=session.trace_session,
                       contact=(session.contact or {}).get("email"),
                       latency_ms=(time.monotonic() - t0) * 1000,
                       detail={"message": question[:300], "reply": (reply or "")[:300],
                               "tools_called": tools_called,
                               "identified": bool(session.contact)})
            _note_chat_activity(session, question, tools_called)
            _store_chat_turns(session, question, reply or "")
            await queue.put({"type": "done", "reply": reply, "customer": for_browser(customer),
                             "trail": trail})
        except Exception:
            log.warning("portal chat turn failed (tenant=%s)", tenant_id, exc_info=True)
            trace.emit(tenant_id, "portal_chat", outcome="error",
                       session_id=session.trace_session,
                       latency_ms=(time.monotonic() - t0) * 1000,
                       detail={"message": question[:300],
                               "tools_called": tools_called})
            await queue.put({"type": "done",
                             "reply": "Sorry — I had trouble with that. Could "
                                      "you try again in a moment?",
                             "customer": None, "trail": trail})
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
