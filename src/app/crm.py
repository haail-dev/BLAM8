"""CRM — "the CRM that fills itself in" (docs/architecture/CRM_design.md).

BLAM8 already reads every inbound email and drafts every reply, so the CRM never
asks the tenant to type: companies, contacts and the activity feed are DERIVED
from email_log — live at draft/send time (pipeline + dashboard hooks) and once
historically via the backfill (scripts/db/backfill_crm.py). This module owns that
derivation; the queries live in src/app/db.py (crm_*), the pages in src/app/dashboard.py,
the JSON API in src/app/crm_routes.py.

Company is the CRM's atom, inferred from the sender's email domain — several
people at one firm roll up. A freemail address (gmail/outlook/…) identifies a
person, not a firm, so it never becomes a company; the contact stands alone
until they state one (phase 3.5's extraction), which ties them to it — and
re-ties them when a later email states a different firm, latest word wins.
"""
import logging
import re
from datetime import date, datetime, timezone
from urllib.parse import quote

from app import agent, db

log = logging.getLogger(__name__)

# Consumer mailbox providers whose domain says nothing about a company. UK-heavy
# on purpose (btinternet/sky/talktalk…) — BLAM8's tenants are UK SMBs and their
# customers write from these all day.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.co.uk", "hotmail.com",
    "hotmail.co.uk", "live.com", "live.co.uk", "msn.com", "yahoo.com",
    "yahoo.co.uk", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "btinternet.com", "btopenworld.com", "sky.com", "talktalk.net",
    "virginmedia.com", "ntlworld.com", "blueyonder.co.uk", "protonmail.com",
    "proton.me", "pm.me", "gmx.com", "gmx.co.uk", "mail.com", "zoho.com",
    "fastmail.com",
}

# NO default pipeline (Julius, 2026-09-02): a tenant starts with ZERO stages.
# Stages exist only when the assistant recommended them or the owner added
# them — a seeded generic set read as "already set up", stacked under every
# recommendation, and resurrected itself after deletes. Every consumer of
# stages must cope with an empty list (deals land Unstaged).

STALE_AFTER_DAYS = 14  # contact-detail "gone quiet" indicator threshold


def domain_of(address: str | None) -> str | None:
    """The lowercased domain of an email address, or None when there isn't one."""
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return None
    dom = addr.rsplit("@", 1)[1].strip(".")
    return dom or None


def is_business_domain(domain: str | None) -> bool:
    return bool(domain) and domain not in FREEMAIL_DOMAINS


def company_name_from_domain(domain: str) -> str:
    """A presentable starting name from a domain: 'acme-services.co.uk' →
    'Acme Services'. The tenant (or phase 2's extraction) can refine it; blank
    would read as a bug, so we always offer something."""
    label = domain.split(".")[0]
    words = label.replace("-", " ").replace("_", " ").split()
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words) or domain


def _said(value) -> str | None:
    """A stated value, or None when it is blank or a placeholder ("...", "n/a"):
    run #8 filed "..." as a customer's company and wrote it into provenance."""
    s = (value or "").strip()
    return None if not s or agent._PLACEHOLDER.match(s) else s


def _stated_name(row: dict) -> str | None:
    """The customer's SELF-STATED name (agent-extracted) — beats the From header."""
    return _said(row.get("contact_name"))


def _header_name(row: dict) -> str | None:
    name = (row.get("from_name") or "").strip()
    # A header "name" that is just the address adds nothing over the email column.
    if not name or "@" in name:
        return None
    return name


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _newer(candidate, existing) -> bool:
    """Is `candidate` a later timestamp than `existing`? Parsed, not string-compared
    — 'Z' and '+00:00' suffixes must order correctly against each other."""
    cand = _parse_ts(candidate)
    if cand is None:
        return False
    old = _parse_ts(existing)
    return old is None or cand > old


def _normalize_company_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for fuzzy comparison."""
    n = name.lower().strip()
    for suffix in ("ltd", "limited", "llc", "inc", "plc", "co", "corp"):
        n = n.removesuffix(f" {suffix}").removesuffix(f" {suffix}.")
    return n.strip()


def _fuzzy_company_match(stated: str, existing: list[dict]) -> dict | None:
    """Token-set fuzzy match: if the normalised stated name matches an existing
    company at ≥85% token overlap OR Levenshtein ≤2, return that company.
    Returns the best match or None."""
    norm = _normalize_company_name(stated)
    norm_tokens = set(norm.split())
    if not norm_tokens:
        return None
    best, best_score = None, 0.0
    for row in existing:
        cand = _normalize_company_name(row.get("name") or "")
        if not cand:
            continue
        # Exact normalised match (fast path)
        if cand == norm:
            return row
        # Levenshtein ≤2 (only for short names where token overlap is unreliable)
        if abs(len(cand) - len(norm)) <= 2 and _levenshtein(cand, norm) <= 2:
            return row
        # Token-set overlap
        cand_tokens = set(cand.split())
        if not cand_tokens:
            continue
        overlap = len(norm_tokens & cand_tokens) / max(len(norm_tokens), len(cand_tokens))
        if overlap > best_score:
            best, best_score = row, overlap
    return best if best_score >= 0.85 else None


def _levenshtein(a: str, b: str) -> int:
    """Minimal Levenshtein distance (no external dep needed at this scale)."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                            prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def _resolve_freemail_company(tenant_id: str, stated_company: str,
                              occurred: str | None = None) -> dict | None:
    """Phase 3.5 (§18.1): for freemail senders, resolve the LLM-extracted
    company name against existing companies via fuzzy match, or create a new
    domain-less company record. Returns the company row or None."""
    stated_company = stated_company.strip()
    if not stated_company:
        return None
    # Load existing companies for this tenant (name-based match)
    existing = db.crm_list_companies(tenant_id, limit=500)
    match = _fuzzy_company_match(stated_company, existing)
    if match:
        # Refresh last_activity_at on the matched company
        if occurred and _newer(occurred, match.get("last_activity_at")):
            db.crm_update_company(match["id"], tenant_id,
                                  {"last_activity_at": occurred})
        return match
    # No match — create a new company (domain=None for freemail-inferred)
    return db.crm_create_company_by_name(tenant_id, stated_company)


def ingest_email_log_row(tenant_id: str, row: dict,
                         own_addresses: set | None = None) -> dict:
    """Fold one email_log row into the CRM: upsert the company (business domains
    only) and contact, refresh their names/last-activity, and append the activity
    feed — the inbound email always, plus the outbound reply when the row says
    'sent'. Idempotent end to end (unique keys + crm_activities_source_uniq), so
    the backfill, a re-run and a duplicate webhook delivery all settle on the
    same rows. Returns {'activities': n, 'contact_id', 'company_id'} — the ids
    feed phase 2's field extraction; the tally feeds the backfill."""
    address = (row.get("from_address") or "").strip().lower()
    dom = domain_of(address)
    skipped = {"activities": 0, "contact_id": None, "company_id": None}
    if not dom:
        # SMS rows: the from_address is a phone number, not an email — the
        # contact is keyed by phone instead (docs/modules/module-sms-channel.md §1).
        # No company inference (a number has no domain); extraction can fill
        # email/company later, onto this same contact.
        from app import sms  # local import — pipeline imports crm at module load
        phone = sms.normalize_e164(row.get("from_address"))
        if phone:
            return _ingest_sms_row(tenant_id, row, phone)
        return skipped
    # Lazy import: pipeline imports crm at module load, so the top level would cycle.
    from app.pipeline import is_no_reply_address
    if is_no_reply_address(address):
        return skipped  # a robot sender is nobody's customer
    if own_addresses is None:
        own_addresses = db.own_mailbox_addresses(tenant_id)
    if address in own_addresses:
        return skipped  # our own desk writing in — not a customer

    occurred = row.get("created_at") or datetime.now(timezone.utc).isoformat()
    stated_company = _said(row.get("contact_company"))

    company = None
    if is_business_domain(dom):
        company = db.crm_get_or_create_company(
            tenant_id, dom, name=stated_company or company_name_from_domain(dom))
        updates = {}
        if stated_company and stated_company != (company.get("name") or ""):
            updates["name"] = stated_company  # what they CALL themselves wins
        if _newer(occurred, company.get("last_activity_at")):
            updates["last_activity_at"] = occurred
        if updates:
            db.crm_update_company(company["id"], tenant_id, updates)

    name = _stated_name(row) or _header_name(row)
    contact = db.crm_get_or_create_contact(
        tenant_id, address, name=name,
        company_id=company["id"] if company else None)

    stated_link = False   # this row's stated company decided the link (freemail path)
    if company is None and stated_company and (
            not contact.get("company_id")
            or _newer(occurred, contact.get("last_activity_at"))):
        # Phase 3.5 §18.1 — freemail sender with a stated company name (LLM-
        # extracted from body/signature by the pipeline). Fuzzy-match against
        # existing companies so "Pearson Services" / "Pearson Services Ltd"
        # don't create duplicates; create fresh when no match. Resolved AFTER
        # the contact fetch, gated on this row being newer than the contact's
        # last activity: the customer's LATEST stated company wins promptly
        # (zero-LLM), while a replayed or out-of-order old email can neither
        # move the link back nor orphan-create a company.
        company = _resolve_freemail_company(tenant_id, stated_company, occurred)
        stated_link = company is not None

    updates = {}
    stated = _stated_name(row)
    if stated and stated != (contact.get("name") or ""):
        updates["name"] = stated
    elif name and not contact.get("name"):
        updates["name"] = name
    moved = stated_link and contact.get("company_id") not in (None, company["id"])
    if company and (not contact.get("company_id") or moved):
        updates["company_id"] = company["id"]
        if stated_link:
            # Provenance rides custom_data (the seam column — underscore keys
            # never render, build_crm_context only surfaces defined fields):
            # who said so, in which email, and what the link replaced, so a
            # moved company is auditable and rescuable.
            db.crm_merge_custom_data("contact", contact["id"], tenant_id, {
                "_company_provenance": {
                    "source": "stated_company",
                    "stated": stated_company,
                    "email_log_id": row.get("id"),
                    "occurred_at": occurred,
                    "previous_company_id": contact.get("company_id"),
                }})
    if _newer(occurred, contact.get("last_activity_at")):
        updates["last_activity_at"] = occurred
    if updates:
        db.crm_update_contact(contact["id"], tenant_id, updates)

    company_id = company["id"] if company else contact.get("company_id")
    added = 0
    added += db.crm_insert_activity(
        tenant_id, contact_id=contact["id"], company_id=company_id,
        activity_type="email", source_id=row.get("id"), direction="inbound",
        subject=row.get("subject"), summary=row.get("request_summary"),
        occurred_at=occurred)
    if row.get("status") == "sent":
        added += db.crm_insert_activity(
            tenant_id, contact_id=contact["id"], company_id=company_id,
            activity_type="email", source_id=row.get("id"), direction="outbound",
            subject=row.get("subject"), summary=row.get("action_summary"),
            occurred_at=occurred)
    return {"activities": added, "contact_id": contact["id"], "company_id": company_id}


def _ingest_sms_row(tenant_id: str, row: dict, phone: str) -> dict:
    """The SMS twin of the email ingest above: phone-keyed contact upsert +
    the activity append(s). Same idempotency (crm_activities_source_uniq), same
    return shape, so phase 2/3 extraction and intel ride on it unchanged."""
    occurred = row.get("created_at") or datetime.now(timezone.utc).isoformat()
    name = _stated_name(row) or _header_name(row)
    contact = db.crm_get_or_create_contact_by_phone(tenant_id, phone, name=name)
    updates = {}
    stated = _stated_name(row)
    if stated and stated != (contact.get("name") or ""):
        updates["name"] = stated
    elif name and not contact.get("name"):
        updates["name"] = name
    if _newer(occurred, contact.get("last_activity_at")):
        updates["last_activity_at"] = occurred
    if updates:
        db.crm_update_contact(contact["id"], tenant_id, updates)
    company_id = contact.get("company_id")
    added = db.crm_insert_activity(
        tenant_id, contact_id=contact["id"], company_id=company_id,
        activity_type="sms", source_id=row.get("id"), direction="inbound",
        subject=row.get("subject"), summary=row.get("request_summary"),
        occurred_at=occurred)
    if row.get("status") == "sent":
        added += db.crm_insert_activity(
            tenant_id, contact_id=contact["id"], company_id=company_id,
            activity_type="sms", source_id=row.get("id"), direction="outbound",
            subject=row.get("subject"), summary=row.get("action_summary"),
            occurred_at=occurred)
    return {"activities": added, "contact_id": contact["id"], "company_id": company_id}


def note_outbound_sent(tenant_id: str, log_id: str) -> None:
    """A reply just went out (approved or full-auto) — append the outbound
    activity. Re-ingests the row, whose status is now 'sent'; the inbound half
    dedupes away."""
    row = db.get_email_log_entry(log_id, tenant_id)
    if row:
        ingest_email_log_row(tenant_id, {**row, "status": "sent"})


def note_contact_activity(tenant_id: str, contact: dict | None, activity_type: str, *,
                          subject: str | None = None, summary: str | None = None,
                          source_id: str | None = None,
                          occurred: str | None = None) -> bool:
    """Append one NON-email activity to a known CRM contact's timeline and
    refresh last_activity_at — the customer-portal writes (self-serve bookings,
    reschedules, questions asked) come through here, so a portal-only customer
    stops being invisible in the contacts list. Idempotent when source_id is
    set (crm_activities_source_uniq); call sites treat it as best-effort."""
    if not contact or not contact.get("id"):
        return False
    occurred = occurred or datetime.now(timezone.utc).isoformat()
    added = db.crm_insert_activity(
        tenant_id, contact_id=contact["id"], company_id=contact.get("company_id"),
        activity_type=activity_type, source_id=source_id,
        subject=subject, summary=summary, occurred_at=occurred)
    if _newer(occurred, contact.get("last_activity_at")):
        db.crm_update_contact(contact["id"], tenant_id,
                              {"last_activity_at": occurred})
    return added


def note_booking_activity(tenant_id: str, booking: dict | None, *,
                          verb: str = "Booked",
                          source_suffix: str | None = None) -> bool:
    """A confirmed diary booking onto its contact's timeline as
    'calendar_event' — the type db/schema.sql anticipated but nothing wrote
    until 2026-07-31. Keyed by booking id so a re-confirm dedupes; a move
    passes source_suffix (the new slot) so each reschedule is its own feed
    row. Skips silently when the booking carries no email-shaped contact
    (manual diary rows often have none)."""
    if not booking:
        return False
    email = (booking.get("contact") or "").strip().lower()
    if "@" not in email:
        return False
    contact = db.crm_get_or_create_contact(tenant_id, email)
    when = booking.get("booking_date") or ""
    window = booking.get("window") or "day"
    ref = booking.get("booking_ref")
    return note_contact_activity(
        tenant_id, contact, "calendar_event",
        source_id=(f"{booking.get('id')}:{source_suffix}" if source_suffix
                   else str(booking.get("id"))),
        subject=booking.get("summary"),
        summary=f"{verb} for {when} ({window})" + (f" — {ref}" if ref else ""))


# ── Promises: the cleared state (Kris 2026-09-20) ────────────────────────────
# A promise that clears must be SHOWN cleared — green, ticked, and linked to
# the thing that cleared it — rather than vanish from the card. Two helpers:
# the pure label/href the card renders from, and the permanent timeline line
# written at the moment of clearing.

_CLEARED_HREF = {
    "booking": "/app/diary/{id}",
    "invoice": "/app/invoices/{id}",
    "quote": "/app/quotes/{id}",
}


def cleared_by(promise: dict, contact_email: str | None = None) -> dict:
    """{'label', 'href'} for a promise's cleared-by link, read off its
    resolved_by_* columns. Pure. A hand click reads 'you marked it done'; an
    email links to the exact message in the thread (#log-<id>); a booking,
    invoice or quote to its own page. A row cleared before these columns
    existed reads plainly 'cleared' — the evidence phrase still shows."""
    kind = promise.get("resolved_by_kind")
    rid = promise.get("resolved_by_id")
    label = promise.get("resolved_by_label")
    if promise.get("status") == "dropped":
        return {"label": "dropped", "href": None}
    if kind == "manual" or (kind is None and promise.get("resolution_type") == "manual"):
        return {"label": "you marked it done", "href": None}
    if kind == "email":
        href = None
        if contact_email and rid:
            href = f"/app/conversations/view?contact={quote(contact_email)}#log-{rid}"
        return {"label": f"cleared by {label or 'an email'}", "href": href}
    if kind in _CLEARED_HREF:
        return {"label": f"cleared by {label or kind}",
                "href": _CLEARED_HREF[kind].format(id=rid) if rid else None}
    return {"label": "cleared", "href": None}


# ── Today's promise pop-up (Kris 2026-09-20) ────────────────────────────────
# "You owe" / "They owe" on a person's Today card used to be a link into the
# CRM. It opens a pop-up now: the promise itself, and the exchange it was read
# out of — the subject, the day, what they asked, what you replied, and their
# own words — with the email itself one click away. Pure: the page hands in
# the rows it already fetched, and the template renders what comes back.

SNIPPET_CHARS = 240


def promise_snippet(body: str | None, limit: int = SNIPPET_CHARS) -> str:
    """Their own words, cut short: the new message only (no quoted chain),
    whitespace folded, clipped on a word with an ellipsis."""
    text = " ".join((agent.strip_quoted(body or "") or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (cut or text[:limit]) + "…"


def promise_origin(promise: dict, email: dict | None, contact: dict) -> dict:
    """Where a promise came from — the pop-up's 'Where it came from' box.
    Three shapes: the exchange itself when its email_log row is to hand; the
    thread link alone when the row is not (the read failed, or retention
    purged the content); and a plain 'not from an email' for a promise with
    no email behind it (an event's outstanding item, or one added by hand)."""
    who = contact.get("name") or contact.get("email") or "them"
    address = contact.get("email")
    log_id = promise.get("email_log_id")
    href = None
    if address:
        href = f"/app/conversations/view?contact={quote(address)}"
        if log_id:
            href += f"#log-{log_id}"
    blank = {"subject": "", "asked": "", "replied": "", "snippet": ""}
    if email:
        sms = (email.get("channel") or "email") == "sms"
        kind = str(email.get("kind") or "inbound").strip().lower()
        if sms:
            label = f"Read out of a text exchange with {who}"
        elif kind == "inbound":
            label = f"Read out of an email exchange with {who}"
        else:
            # An invoice / quote / reminder round: nothing came in from them,
            # so no words of theirs to quote (the body is a placeholder).
            label = f"Read out of the {kind.replace('_', ' ')} sent to {who}"
        return {"kind": "email", "sms": sms, "label": label,
                "when": email.get("created_at"),
                "subject": (email.get("subject") or "").strip(),
                "asked": (email.get("request_summary") or "").strip(),
                "replied": (email.get("action_summary") or "").strip(),
                "snippet": promise_snippet(email.get("body_in")) if kind == "inbound" else "",
                "href": href}
    if log_id:
        return {"kind": "email", "sms": False,
                "label": f"Read out of an email exchange with {who}",
                "when": promise.get("created_at"), "href": href, **blank}
    return {"kind": "none", "sms": False,
            "label": "Not read out of an email — added from an event or by hand",
            "when": promise.get("created_at"), "href": None, **blank}


def promise_card(promise: dict, contact: dict, *, email: dict | None = None,
                 deal: dict | None = None, action: dict | None = None) -> dict:
    """Everything the pop-up shows for one open promise, off rows the page
    already holds: the promise, whose it is, and where it came from. Pure."""
    owe = promise.get("direction") == "we_owe"
    company = contact.get("crm_companies")
    return {
        "id": promise.get("id"),
        "owe": owe,
        "chip": "You owe" if owe else "They owe",
        "description": (promise.get("description") or "").strip(),
        "due_hint": (promise.get("due_hint") or "").strip(),
        "created_at": promise.get("created_at"),
        "last_seen_at": promise.get("last_seen_at"),
        "mention_count": int(promise.get("mention_count") or 0),
        "looks_done": promise.get("resolution_type") == "suggested",
        "evidence": (promise.get("resolution_evidence") or "").strip(),
        "name": contact.get("name") or contact.get("email") or "Someone",
        "email": contact.get("email"),
        "company": (company.get("name") or None) if isinstance(company, dict) else None,
        "deal": (deal or {}).get("title") or None,
        "crm_href": f"/app/crm/contacts/{contact.get('id')}",
        # The door that fulfils it (promise_kinds.route): the Diary's Add form
        # prefilled, the invoice, the thread, Knowledge — or nothing.
        "action": action,
        "origin": promise_origin(promise, email, contact),
    }


def note_promise_cleared(tenant_id: str, promise: dict | None, *,
                         contact: dict | None = None) -> bool:
    """The permanent record: one 'promise_cleared' (or 'promise_dropped') line
    on the contact's timeline, keyed by promise id so a re-run never doubles
    it. Reads the cleared-by link off the row, so callers pass the row AS
    STAMPED. A direct feed insert on purpose — clearing a promise is a record,
    not customer activity, so it must not move last_activity_at and make a
    quiet customer look busy. Best-effort at every call site."""
    if not promise or not promise.get("contact_id"):
        return False
    contact = contact or db.crm_get_contact(promise["contact_id"], tenant_id)
    if not contact:
        return False
    dropped = promise.get("status") == "dropped"
    by = cleared_by(promise, contact.get("email"))
    evidence = (promise.get("resolution_evidence") or "").strip()
    who = "You owed" if promise.get("direction") == "we_owe" else "They owed"
    summary = by["label"] + (f" — {evidence}" if evidence and not dropped else "")
    return db.crm_insert_activity(
        tenant_id, contact_id=contact["id"], company_id=contact.get("company_id"),
        activity_type="promise_dropped" if dropped else "promise_cleared",
        source_id=str(promise.get("id")),
        subject=f"{who}: {promise.get('description') or ''}".strip(),
        summary=summary or None,
        occurred_at=promise.get("resolved_at") or datetime.now(timezone.utc).isoformat())


def backfill_tenant(tenant_id: str) -> dict:
    """Derive the whole CRM from a tenant's historical email_log (oldest first).
    Idempotent — safe to re-run on prod. Returns a tally for the script's output."""
    own = db.own_mailbox_addresses(tenant_id)
    rows = db.crm_email_log_rows(tenant_id)
    activities = 0
    for row in rows:
        try:
            activities += ingest_email_log_row(tenant_id, row, own_addresses=own)["activities"]
        except Exception:
            log.warning("CRM backfill: email_log %s skipped", row.get("id"), exc_info=True)
    return {"rows": len(rows), "activities": activities}


def ensure_default_stages(tenant_id: str, client=None) -> list[dict]:
    """The tenant's pipeline stages — a plain read. The name survives from the
    lazy-seed era; since 2026-09-02 NOTHING is seeded (see the note by the old
    DEFAULT_STAGES site): only the assistant's recommendations and the owner's
    own additions create stages, and an empty list is a real, respected state."""
    return db.crm_list_stages(tenant_id, client=client)


# ── View-model helpers (pure — the pages and API shape through these) ────────

def days_since(ts: str | None, now: datetime | None = None) -> int | None:
    """Whole days since an ISO timestamp; None when there's no timestamp."""
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max((now - then).days, 0)


def with_recency(rows: list[dict], now: datetime | None = None,
                 stale_after: int | None = None) -> list[dict]:
    """Attach days_since_last / stale to contact or company rows for the lists
    and the detail page ('Alison went quiet 12 days ago'). `stale_after`
    overrides the default threshold (tenant_config.crm_quiet_days)."""
    now = now or datetime.now(timezone.utc)
    threshold = STALE_AFTER_DAYS if stale_after is None else stale_after
    out = []
    for r in rows:
        days = days_since(r.get("last_activity_at"), now)
        out.append({**r, "days_since_last": days,
                    "stale": days is not None and days > threshold})
    return out


def pipeline_view(stages: list[dict], deals: list[dict]) -> list[dict]:
    """Deals grouped under the tenant's stages, in display order. A deal whose
    stage was deleted (stage_id null) lands in a trailing 'Unstaged' bucket so
    nothing silently disappears."""
    by_stage: dict = {s["id"]: {"stage": s, "deals": []} for s in stages}
    unstaged = []
    for d in deals:
        bucket = by_stage.get(d.get("stage_id"))
        (bucket["deals"] if bucket else unstaged).append(d)
    out = list(by_stage.values())
    if unstaged:
        out.append({"stage": {"id": None, "name": "Unstaged", "stage_type": "active"},
                    "deals": unstaged})
    return out


# ── The contacts logbook (Kris 2026-09-21: "more like a contact logbook") ────
# A name-and-address ledger said nothing about the people on it. Each entry
# now carries what the book knows: who they are (initials, tinted by their
# mood), what the bookings are about (the subjects or assets),
# the last thing that happened between you, and the facts that need an eye —
# the next stay, what they owe, promises open. Grouped by when you last spoke,
# newest first, the way a logbook reads. Pure: every read is handed in.
_TONES = {"positive": "emerald", "concerned": "amber", "frustrated": "rose", "angry": "rose"}
_GROUP_ORDER = ("Today", "Yesterday", "This week", "This month", "Earlier")
_LIVE_BOOKING = ("offered", "proposed", "confirmed")
_SAID = ("email", "sms", "portal", "portal_chat", "call")


def _initials(name: str, email: str) -> str:
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    if words:
        return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()
    local = (email or "").split("@")[0]
    return (local[:1] or "?").upper()


def _when(days) -> str:
    if days is None:
        return "—"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


def _group_label(days) -> str:
    if days is None:
        return "Earlier"
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return "This week"
    if days < 31:
        return "This month"
    return "Earlier"


def logbook(contacts: list[dict], *, dependants=(), bookings=(), invoices=(),
            promises=(), activities=(), order: str = "recent",
            today: date | None = None) -> list[dict]:
    """The contacts page's groups: [{label, entries}], each entry a contact row
    plus title, initials, tone, company, `pets` (dependants), last {arrow, text}, facts
    [{tone, text}] and when. Contacts arrive newest-activity first (with
    days_since_last / stale from with_recency) and keep that order."""
    from app import money
    from app.dates import pretty_date
    today = today or date.today()

    pets: dict[str, list[str]] = {}
    for d in dependants or []:
        cid, name = d.get("contact_id"), (d.get("name") or "").strip()
        if not cid or not name:
            continue
        breed = str(((d.get("data") or {}).get("breed") or "")).strip()
        pets.setdefault(cid, []).append(f"{name} · {breed}" if breed else name)

    nxt: dict[str, str] = {}
    for b in bookings or []:
        if (b.get("status") or "") not in _LIVE_BOOKING:
            continue
        em = (b.get("contact") or "").strip().lower()
        d = str(b.get("booking_date") or "")[:10]
        if not em or not d or d < today.isoformat():
            continue
        if em not in nxt or d < nxt[em]:
            nxt[em] = d

    owed: dict[str, dict] = {}
    for inv in invoices or []:
        if (inv.get("status") or "") not in money.OWING_STATUSES:
            continue
        em = (inv.get("customer_email") or "").strip().lower()
        if not em:
            continue
        g = owed.setdefault(em, {"amount": 0.0, "late": 0})
        g["amount"] = round(g["amount"] + money._outstanding(inv), 2)
        g["late"] = max(g["late"], money.days_late(inv, today))

    prom: dict[str, dict] = {}
    for p in promises or []:
        cid = p.get("contact_id")
        if not cid:
            continue
        g = prom.setdefault(cid, {"we": 0, "they": 0})
        g["we" if p.get("direction") == "we_owe" else "they"] += 1

    # The last line is what was last SAID — an email, a text, a portal chat —
    # ahead of the bookkeeping that follows it (a promise clearing, a payment
    # landing), which only stands in when nothing was said at all.
    last: dict[str, dict] = {}
    fallback: dict[str, dict] = {}
    for a in activities or []:            # newest first: the first seen wins
        cid = a.get("contact_id")
        if not cid:
            continue
        text = (a.get("summary") or a.get("subject") or "").strip()
        if not text:
            continue
        if len(text) > 90:
            text = text[:89] + "…"
        arrow = {"inbound": "←", "outbound": "→"}.get(a.get("direction") or "", "·")
        line = {"arrow": arrow, "text": text}
        if (a.get("activity_type") or "") in _SAID:
            last.setdefault(cid, line)
        else:
            fallback.setdefault(cid, line)
    for cid, line in fallback.items():
        last.setdefault(cid, line)

    entries: list[dict] = []
    for c in contacts:
        cid, em = c.get("id"), (c.get("email") or "").strip().lower()
        name = (c.get("name") or "").strip()
        facts: list[dict] = []
        if em in nxt:
            facts.append({"tone": "emerald", "text": f"Next {pretty_date(nxt[em])}"})
        o = owed.get(em)
        if o and o["amount"] > 0:
            facts.append({"tone": "rose" if o["late"] > 0 else "amber",
                          "text": f"Owes £{o['amount']:,.2f}"
                                  + (f" · {o['late']}d late" if o["late"] > 0 else "")})
        pr = prom.get(cid)
        if pr and (pr["we"] or pr["they"]):
            bits = ([f"you owe {pr['we']}"] if pr["we"] else []) + \
                   ([f"they owe {pr['they']}"] if pr["they"] else [])
            facts.append({"tone": "slate", "text": " · ".join(bits)})
        company = c.get("crm_companies")
        days = c.get("days_since_last")
        entries.append({
            **c,
            "title": name or c.get("email") or "—",
            "initials": _initials(name, c.get("email") or ""),
            "tone": _TONES.get((c.get("sentiment") or "").lower(), "slate"),
            "company": (company.get("name") or "") if isinstance(company, dict) else "",
            "pets": pets.get(cid, []),
            "last": last.get(cid),
            "facts": facts,
            "when": _when(days),
        })
    groups: dict[str, list] = {}
    if order == "name":
        # A–Z (Kris 2026-09-21): the address-book reading — by the name the
        # page shows, anything not starting with a letter under "#" at the end.
        for e in sorted(entries, key=lambda e: e["title"].lower()):
            ch = e["title"][:1].upper()
            groups.setdefault(ch if ch.isalpha() else "#", []).append(e)
        return [{"label": k, "entries": groups[k]}
                for k in sorted(groups, key=lambda k: (k == "#", k))]
    for e in entries:
        groups.setdefault(_group_label(e.get("days_since_last")), []).append(e)
    return [{"label": k, "entries": groups[k]} for k in _GROUP_ORDER if k in groups]
