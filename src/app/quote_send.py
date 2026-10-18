"""Sending quotes + quote reminders — the one shared sender (invoice_send.py's
sibling; docs/modules/module-quotes.md).

Both surfaces compose this: the Send button on /app/quotes/{id} and (Phase 2)
the concierge's quote_send executor. The email is a short note + the /q/{id}
link — the LINK is the quote (view tracking, optional-item ticks and one-click
acceptance all live there), which is also why there's no PDF attached by
default: an attachment freezes a document that /q keeps current, and PDF-heavy
quote mail is the category's spam-folder failure mode. The page offers a PDF
download for customers who want one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import db, mailbox
from app import sign_off as sign_off_text
from app.config import settings as app_settings
from app.dates import pretty_date

log = logging.getLogger(__name__)


def quote_url(quote_id: str) -> str:
    return f"{app_settings.app_base_url.rstrip('/')}/q/{quote_id}"


def quote_email_body(quote: dict, s: dict, link: str, sign_off: str = "") -> str:
    """The quote email: what it's for + total + validity, then the view/accept
    link. An empty business name signs off plain 'Thank you' — never a
    placeholder (the invoice sender's lesson)."""
    ref = quote.get("quote_ref") or "quote"
    biz = (s.get("business_name") or "").strip()
    title = (quote.get("title") or "").strip()
    valid = pretty_date(quote.get("valid_until"))
    intro = (quote.get("intro") or "").strip()
    what = f" for {title}" if title else ""
    body = f"Hello,\n\nPlease find our quote {ref}{what}: "
    body += f"£{float(quote.get('total') or 0):,.2f}"
    body += f", valid until {valid}.\n" if valid else ".\n"
    if intro:
        body += f"\n{intro}\n"
    body += (f"\nYou can view the full quote, ask a question, and accept online "
             f"here:\n{link}\n")
    # The business's own sign-off when it has one; otherwise the plain closing.
    closing = sign_off_text.normalise(sign_off) or (f"Thank you,\n{biz}" if biz else "Thank you")
    return body + f"\n{closing}"


def _crm_quote_activity(tenant_id: str, quote: dict, *, activity_type: str,
                        subject: str, summary: str,
                        source_id: str | None = None) -> None:
    """Best-effort contact-timeline entry (idempotent on (type, source_id))."""
    try:
        to = quote.get("customer_email") or ""
        contact = db.crm_contact_by_email(tenant_id, to) if to else None
        db.crm_insert_activity(
            tenant_id, contact_id=(contact or {}).get("id") or quote.get("contact_id"),
            company_id=(contact or {}).get("company_id"),
            activity_type=activity_type, source_id=source_id or quote["id"],
            direction="outbound", subject=subject, summary=summary,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            deal_id=quote.get("deal_id"))
    except Exception:
        log.warning("quote CRM activity failed (%s)", quote.get("id"), exc_info=True)


def _log_sent_email(tenant_id: str, to: str, subject: str, body: str, *,
                    kind: str, via: str) -> None:
    """One email_log row, immediately marked sent → the 'quote_sent' label (a
    real quote doc now, not just a priced reply)."""
    try:
        log_id, _ = db.insert_email_log(tenant_id, to, subject, f"[{kind}]", body,
                                        status="pending", kind=kind)
        db.mark_email_log_sent(log_id, tenant_id, via=via, edited=False)
    except Exception:
        log.warning("quote email_log write failed", exc_info=True)


def _portal_footer(tenant_id: str, to: str, *, client=None) -> str:
    """Same reference-code sign-off as invoice mail (src/app/portal.py) — best-effort,
    empty on any failure, never costs a send."""
    try:
        from app import portal
        cfg = db.get_tenant_config(tenant_id, client=client) or {}
        return portal.email_footer(tenant_id, cfg, to)
    except Exception:
        log.warning("quote portal footer failed (tenant=%s)", tenant_id, exc_info=True)
        return ""


class QuoteHasNoLines(ValueError):
    """Refused: the quote has nothing on it to send."""


async def send_quote_email(tenant_id: str, quote: dict, *, client=None,
                           via: str = "human",
                           body_override: str | None = None) -> None:
    """Send one quote (the /q link) from the tenant's mailbox and stamp the
    outcome trail: status → sent, email_log (quote_sent label), agent log, CRM
    activity. Raises on send failure — callers decide how to surface it.
    `body_override` is operator-edited email text; the stamps are unaffected."""
    from app import invoice_config
    quote_id, to = quote["id"], (quote.get("customer_email") or "").strip()
    # A quote with no selected lines is never sent: the customer would open a
    # total with nothing under it (1 Oct 2026, a quote whose lines failed to
    # save was still sendable). Whatever sends a quote goes through here.
    if not [l for l in db.list_quote_lines(quote_id, tenant_id, client=client)
            if l.get("is_selected", True)]:
        raise QuoteHasNoLines(quote_id)
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    ref = quote.get("quote_ref") or "quote"
    link = quote_url(quote_id)
    from app.invoice_send import configured_sign_off
    body = (body_override or "").strip() or quote_email_body(
        quote, s, link, configured_sign_off(tenant_id, client=client))
    # Job parties: 'kept informed' parties ride as cc (strictly opt-in, D1).
    # With a cc the portal footer is DROPPED: the reference code in it is the
    # recipient's identity capability, and a cc'd party must never hold the
    # approver's code.
    from app import job_parties
    cc = job_parties.cc_for_document(tenant_id, quote.get("booking_id"), to,
                                     client=client)
    footer = "" if cc else _portal_footer(tenant_id, to, client=client)
    await mailbox.send_mail(tenant_id, to, f"Quote {ref}", body + footer,
                            cc=cc or None)
    db.set_quote_status(quote_id, tenant_id, "sent", client=client)
    _log_sent_email(tenant_id, to, f"Quote {ref}", body, kind="quote", via=via)
    db.log_agent_event(tenant_id, "sent",
                       f"Emailed quote {ref} (£{float(quote.get('total') or 0):,.2f}) to {to}",
                       direction="outbound", contact=to, subject=f"Quote {ref}",
                       client=client)
    _crm_quote_activity(
        tenant_id, quote, activity_type="quote", subject=f"Quote {ref} sent",
        summary=f"£{float(quote.get('total') or 0):,.2f}, "
                f"valid until {pretty_date(quote.get('valid_until'))}")
    try:   # Action-side promise resolution (live 2026-09-03: "provide a full
           # written quote" stayed open after two quotes went out — the sent
           # quote IS the fulfilment; the resolver still respects conditions).
        from app import crm_intel
        await crm_intel.resolve_from_action(
            tenant_id, to,
            f"Sent written quote {ref} (£{float(quote.get('total') or 0):,.2f}"
            f"{' — ' + quote['title'] if quote.get('title') else ''}) to the customer",
            client=client,
            ref=("quote", quote.get("id"), ref))
    except Exception:
        log.warning("quote send: promise resolution failed", exc_info=True)


async def send_quote_reminder(tenant_id: str, quote: dict, text: str, *,
                              client=None, via: str = "human") -> None:
    """Send a chase for an unanswered quote and stamp the trail (reminders_sent —
    never the draft-suppression counter: chasing a quote is a different behaviour
    to ignoring a conversation, the invoice-reminder doctrine)."""
    quote_id, to = quote["id"], (quote.get("customer_email") or "").strip()
    ref = quote.get("quote_ref") or "quote"
    subject = f"Your quote {ref}"
    from app import job_parties
    cc = job_parties.cc_for_document(tenant_id, quote.get("booking_id"), to,
                                     client=client)
    footer = "" if cc else _portal_footer(tenant_id, to, client=client)
    await mailbox.send_mail(tenant_id, to, subject, text + footer,
                            cc=cc or None)
    db.record_quote_reminder(quote_id, tenant_id, client=client)
    _log_sent_email(tenant_id, to, subject, text, kind="quote_reminder", via=via)
    db.log_agent_event(tenant_id, "quote_reminder",
                       f"Quote follow-up for {ref} sent to {to}",
                       direction="outbound", contact=to, subject=subject,
                       client=client)
    n = int(quote.get("reminders_sent") or 0) + 1
    _crm_quote_activity(
        tenant_id, quote, activity_type="quote_reminder",
        source_id=f"{quote_id}:r{n}",
        subject=f"Quote follow-up — {ref}",
        summary=f"£{float(quote.get('total') or 0):,.2f}, "
                f"valid until {pretty_date(quote.get('valid_until'))}")
