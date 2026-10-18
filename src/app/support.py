"""Support tickets — the chatbot's collect-and-route layer (no diagnosis here).

The concierge (ask mode) gathers a support issue conversationally and the widget
files it through POST /concierge/ticket; this module owns the semantics:

  * category / severity / status vocabulary (severity auto-high for a broken
    sign-in — it blocks all access);
  * secret redaction — passwords, tokens, auth codes and card numbers are
    scrubbed SERVER-SIDE before anything is stored, on top of the prompt-level
    "never include a password" rule (defense in depth);
  * create_ticket — dedupe (double-click idempotency), a per-email cap on top
    of the route's per-IP limits, the insert, and the best-effort operator
    notification email (the platform_notify_mailbox pattern billing/auth
    routes already use — sent to hello@ via the company tenant's own mailbox);
  * lookup_ticket — status by reference + follow-up email. The result is
    SERVER truth rendered by the widget as a card, so the model can never
    invent a status.

Tickets live in the existing feature_requests table (extended 2026-07-25) —
one pipeline for the roadmap signal, in-app reports and website tickets.
"""
import logging
import re

from app import db
from app.config import settings

log = logging.getLogger(__name__)

# The categories the concierge may file under (directive §3). `feature_request`
# folds into the existing roadmap kind; everything else is an issue ticket.
CATEGORIES = ("login_auth", "mailbox_connection", "billing", "bug",
              "feature_request", "access_request", "other")

CATEGORY_LABELS = {
    "login_auth": "Sign-in / access",
    "mailbox_connection": "Mailbox connection",
    "billing": "Billing",
    "bug": "Something broken",
    "feature_request": "Feature request",
    "access_request": "Access request",
    "other": "Other",
}

ACCOUNT_TYPES = ("", "microsoft", "google", "none")

# Ticket lifecycle (issue rows) + the legacy roadmap lifecycle (feature rows).
# One label map serves the admin table, the tenant page and the status card.
TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
FEATURE_STATUSES = ("new", "reviewing", "approved", "building", "pr_open",
                    "planned", "done")
STATUS_LABELS = {
    "open": "Open", "in_progress": "In progress", "resolved": "Resolved",
    "closed": "Closed", "new": "Open", "reviewing": "In progress",
    "planned": "Planned", "done": "Done", "approved": "Approved",
    "building": "Being built", "pr_open": "In review",
}

MAX_TICKETS_PER_EMAIL_PER_HOUR = 3


class TicketRateLimited(Exception):
    """This follow-up address already filed its hourly allowance."""


# ── secret redaction ─────────────────────────────────────────────────────────
_REDACTED = "[removed]"

# Keyword + explicit separator + value: "password: hunter2", "token = eyJ…",
# "client secret is abc123". The separator is REQUIRED so prose like "I forgot
# my password" or "the token expired" is never mangled.
_KEYWORD_VALUE = re.compile(
    r"(?i)\b(password|passcode|passwd|pwd|pass|otp|pin|api[_ -]?key|access[_ -]?key|"
    r"secret|client[_ -]?secret|token|auth[_ -]?code|verification[_ -]?code)\b"
    r"(\s*[:=]\s*|\s+(?:is|was)\s+)(\S+)")
# Bearer credentials and JWTs are self-identifying wherever they appear.
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b")
# Credential-ish query params pasted inside URLs (OAuth codes, tokens).
_URL_SECRET = re.compile(r"(?i)([?&](?:code|token|access_token|refresh_token|"
                         r"api_key|key|secret)=)[^&\s]+")
# Card-number-shaped digit runs (13–19 digits, optional space/dash groups).
# UK phone numbers stay under 13 digits, so they pass through untouched.
_CARDISH = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def redact(text: str | None) -> tuple[str, bool]:
    """(clean_text, anything_removed). Scrubs secret-shaped content; plain
    problem descriptions come back untouched."""
    if not text:
        return "", False
    hit = False

    def _kw(m: re.Match) -> str:
        nonlocal hit
        hit = True
        return m.group(1) + m.group(2) + _REDACTED

    def _flat(m: re.Match) -> str:
        nonlocal hit
        hit = True
        return _REDACTED

    def _url(m: re.Match) -> str:
        nonlocal hit
        hit = True
        return m.group(1) + _REDACTED

    out = _KEYWORD_VALUE.sub(_kw, text)
    out = _BEARER.sub(_flat, out)
    out = _JWT.sub(_flat, out)
    out = _URL_SECRET.sub(_url, out)
    out = _CARDISH.sub(_flat, out)
    return out, hit


# ── create ───────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email: str | None) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


async def create_ticket(*, email: str, category: str, summary: str,
                        description: str = "", error_message: str = "",
                        account_type: str = "", page_url: str = "",
                        user_agent: str = "", tenant_id: str | None = None,
                        industry: str | None = None,
                        submitted_by: str = "website chat") -> dict:
    """File one support ticket. Returns {ok, reference, deduped, redacted}.
    Raises TicketRateLimited past the per-email hourly cap. The operator
    notification is best-effort — a mail hiccup never loses the ticket."""
    email = (email or "").strip()
    category = category if category in CATEGORIES else "other"
    account_type = account_type if account_type in ACCOUNT_TYPES else ""
    severity = "high" if category == "login_auth" else "normal"
    kind = "feature" if category == "feature_request" else "bug"

    summary, r1 = redact((summary or "").strip())
    description, r2 = redact((description or "").strip())
    error_message, r3 = redact((error_message or "").strip())
    redacted = r1 or r2 or r3
    body = summary if not description or description == summary \
        else f"{summary}\n\n{description}"

    try:
        if db.count_recent_tickets_by_email(email) >= MAX_TICKETS_PER_EMAIL_PER_HOUR:
            raise TicketRateLimited()
        existing = db.find_recent_ticket_duplicate(email, body)
    except TicketRateLimited:
        raise
    except Exception:
        existing = None    # the guards degrade open — filing must not be blocked
    if existing:
        return {"ok": True, "reference": existing, "deduped": True, "redacted": redacted}

    reference = db.add_feature_request(
        tenant_id, body, category=category, submitted_by=submitted_by,
        industry=industry, kind=kind, user_email=email, account_type=account_type,
        error_message=error_message or None, severity=severity, status="open",
        page_url=(page_url or "")[:500] or None,
        user_agent=(user_agent or "")[:300] or None)

    # Two independent best-effort notifications: the email is the record, the push
    # is the interruption. Separate try blocks on purpose — a mail hiccup must not
    # cost the push, and neither may cost the ticket (it is already filed above).
    try:
        await _notify(reference=reference, category=category, email=email,
                      account_type=account_type, severity=severity, body=body,
                      error_message=error_message, page_url=page_url)
    except Exception:
        log.warning("ticket notification email failed (ticket %s is filed)", reference)
    try:
        _notify_push(reference=reference, category=category, severity=severity,
                     body=body)
    except Exception:
        log.warning("ticket notification push failed (ticket %s is filed)", reference)
    return {"ok": True, "reference": reference, "deduped": False, "redacted": redacted}


async def _notify(*, reference: str | None, category: str, email: str,
                  account_type: str, severity: str, body: str,
                  error_message: str, page_url: str) -> None:
    """Email the team about a new ticket — same mechanism as the access-request
    notifications: FROM the company tenant's connected mailbox named in
    PLATFORM_NOTIFY_MAILBOX, TO the support address. Silently skipped when the
    mailbox isn't configured/connected (the ticket still queues in /admin/requests)."""
    if not settings.platform_notify_mailbox:
        return
    from app import mailbox    # lazy — mirrors billing_routes; avoids import cycles
    conn = db.get_connection_by_address(settings.platform_notify_mailbox)
    if not conn:
        return
    ref = reference or "no reference"
    lines = [
        "A new support ticket was raised via the website chatbot.",
        "",
        f"Reference: {ref}",
        f"Category: {CATEGORY_LABELS.get(category, category)} ({category})",
        f"Severity: {severity}",
        f"User email: {email}",
        f"Account type: {account_type or 'not stated'}",
    ]
    if page_url:
        lines.append(f"Page: {page_url}")
    lines += ["", "Description:", body]
    if error_message:
        lines += ["", "Error message:", error_message]
    lines += ["", "---",
              f"Reply to the user at: {email}",
              f"View/manage: {settings.app_base_url}/admin/requests"]
    await mailbox.send_mail(conn["tenant_id"], settings.access_request_notify_to,
                            f"[{ref}] New support ticket — {category}",
                            "\n".join(lines), mailbox_id=conn["id"])


def _notify_push(*, reference: str | None, category: str, severity: str,
                 body: str) -> None:
    """Push the operator's devices when a ticket lands, so /admin/requests never
    has to be polled. Routed to the tenant that owns PLATFORM_NOTIFY_MAILBOX —
    the same lookup _notify uses — because push subscriptions are per-tenant and
    that tenant is the operator's own.

    Deliberately thinner than the email: no user address or error text, because a
    push payload travels through a third-party push service and the reference is
    enough to pull the full ticket. Tag is ticket-{reference} so a redelivery
    replaces the notification instead of stacking."""
    from app import push          # lazy — mirrors _notify's mailbox import
    if not push.push_enabled() or not settings.platform_notify_mailbox:
        return
    conn = db.get_connection_by_address(settings.platform_notify_mailbox)
    if not conn:
        return
    ref = reference or "New ticket"
    gist = " ".join((body or "").split())
    if len(gist) > 90:
        gist = gist[:89].rstrip() + "…"
    push.notify(
        conn["tenant_id"],
        title=f"{ref} — {CATEGORY_LABELS.get(category, category)}",
        body=f"{severity.upper()} · {gist}" if severity == "high" else gist,
        url="/admin/requests",
        tag=f"ticket-{ref}",
        kind="support_ticket")


# ── status lookup ────────────────────────────────────────────────────────────
def lookup_ticket(reference: str, email: str) -> dict:
    """Status by reference + the follow-up email it was filed with. The widget
    renders this verbatim — server truth, never the model's guess.
      {ok: False}                     — the ticket system is unreachable
      {ok: True, found: False}        — no such ticket for that email (honest miss)
      {ok: True, found: True, ...}    — reference, status label, note, summary, date
    """
    try:
        row = db.get_ticket_by_reference(reference, email)
    except Exception:
        return {"ok": False}
    if not row:
        return {"ok": True, "found": False}
    status = (row.get("status") or "open").strip()
    summary = (row.get("body") or "").strip().splitlines()[0][:160] if row.get("body") else ""
    return {
        "ok": True, "found": True,
        "reference": row.get("reference"),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.replace("_", " ").title()),
        "status_note": (row.get("status_note") or "").strip() or None,
        "summary": summary,
        "created_at": (row.get("created_at") or "")[:10] or None,
    }
