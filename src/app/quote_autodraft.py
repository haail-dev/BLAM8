"""The quote writes itself (docs/modules/module-quotes.md Phase 3 — the moat).

When the reply drafter prices an enquiry (a booking/hold with quoted_amount,
placed while the draft was composed), BLAM8 also drafts the quote DOCUMENT —
zero extra LLM, the pricing work already happened; this is plumbing. Every
competitor's quote starts at a blank form; ours starts already written, from
the conversation that priced it.

tenant_config.quote_auto_draft:
  off      never auto-draft
  propose  (default) draft the quote + notify — the owner reviews and sends
           it from /app/quotes/{id} (or asks the concierge to)
  auto     draft AND weave the /q accept link into the outgoing reply at send
           time (approve or auto-send) — the quote goes out with the answer,
           "sent before leaving the driveway"

Both hooks are best-effort: a failure here must never cost a reply.
"""
from __future__ import annotations

import logging

from app import db, push, quoting, trace

log = logging.getLogger(__name__)

MODES = ("off", "propose", "auto")


def mode_for(cfg: dict | None) -> str:
    mode = str((cfg or {}).get("quote_auto_draft") or "propose").strip().lower()
    return mode if mode in MODES else "propose"


def maybe_draft_for_log(tenant_id: str, cfg: dict | None, log_id: str,
                        contact_email: str | None,
                        contact_name: str | None = None,
                        client=None) -> dict | None:
    """Draft the quote for one priced conversation, once. Reads the bookings
    this draft placed (bookings.email_log_id) and quotes ONLY what the drafter
    actually priced — a quote must never invent a price (the honesty rule).
    Returns the quote row, or None (mode off / nothing priced / already
    drafted / any failure)."""
    try:
        if mode_for(cfg) == "off" or not log_id:
            return None
        priced = [b for b in db.bookings_for_log(log_id, tenant_id, client=client)
                  if float(b.get("quoted_amount") or 0) > 0
                  # a released hold is not work to quote (a redraft re-runs this
                  # after cancelling the old holds - round three, 1 Oct 2026, F26)
                  and str(b.get("status") or "").lower() not in ("cancelled", "declined")]
        if not priced:
            return None
        # "Quote for a booking the diary already priced" (Invoice settings,
        # 2026-09-24): off means the booking's own price is the answer — a
        # kennels stay at its nightly rate doesn't want QUO-1029 restating it.
        from app import invoice_config
        try:
            restate = invoice_config.settings_for(
                db.get_invoice_settings(tenant_id, client=client))["quote_booking_priced"]
        except Exception:   # unreadable settings keep today's behaviour
            restate = True
        if not restate:
            return None
        if db.quote_for_log(log_id, tenant_id, client=client):
            return None                        # re-drafts never stack quotes
        lines = []
        for b in priced:
            lines.extend(quoting.lines_from_booking(b))
        email = (contact_email or "").strip() or None
        name = (contact_name or "").strip() or None
        # Job parties: if this job has an APPROVES party, the quote is theirs —
        # the enquiry sender may be the agent booking on a landlord's behalf.
        from app import job_parties
        approver = job_parties.resolve(
            priced[0], db.list_job_parties(tenant_id, priced[0]["id"],
                                           client=client))["approves"]
        if not approver.get("is_fallback") and approver.get("email"):
            email, name = approver["email"], approver.get("name")
        if email and db.open_quote_for_customer(tenant_id, email, client=client):
            return None                        # a quote is already in play with them
        crm_contact = (db.crm_contact_by_email(tenant_id, email, client=client)
                       if email else None)
        name = name or (crm_contact or {}).get("name")
        title = (priced[0].get("job_type") or priced[0].get("description")
                 or "").strip() or None
        quote = db.create_quote(
            tenant_id, email, name, lines, client=client,
            contact_id=(crm_contact or {}).get("id"),
            booking_id=priced[0].get("id"), email_log_id=log_id,
            title=title, created_by="ai")
        if not quote:
            return None
        ref = quote.get("quote_ref")
        total = float(quote.get("total") or 0)
        trace.emit(tenant_id, "quote_autodraft", email_log_id=log_id,
                   contact=email,
                   detail={"quote_ref": ref, "total": total,
                           "mode": mode_for(cfg), "bookings": len(priced)})
        db.log_agent_event(
            tenant_id, "quote_drafted",
            f"Drafted quote {ref} (£{total:,.2f}) from this enquiry's pricing",
            contact=email, email_log_id=log_id, client=client)
        if mode_for(cfg) == "propose":
            # The owner's cue — in auto mode the send hook is the cue instead.
            push.notify(tenant_id,
                        title=f"Quote {ref} drafted (£{total:,.2f})",
                        body=f"{name or email or 'The enquiry'} — review and "
                             "send it from Quotes",
                        url=f"/app/quotes/{quote['id']}",
                        tag=f"quote-autodraft-{quote['id']}",
                        kind="quote_drafted", contact=email, contact_name=name)
        return quote
    except Exception:
        log.warning("quote auto-draft failed for email_log %s", log_id,
                    exc_info=True)
        return None


def _still_what_the_draft_wrote(quote: dict, lines: list[dict],
                                released: list[dict]) -> bool:
    """True only when the quote still reads exactly as maybe_draft_for_log wrote
    it from THESE released bookings: no owner-written intro or notes, and one
    line per priced booking with the line text and amount lines_from_booking
    gives it. Anything else (a line added, retyped, re-priced, removed, or a
    line for a booking that was not released) is the owner's work, left alone."""
    if quote.get("intro") or quote.get("notes"):
        return False
    expected: list[tuple] = []
    for b in released:
        if float(b.get("quoted_amount") or 0) > 0:
            for l in quoting.lines_from_booking(b):
                expected.append((l.get("booking_id"), l.get("description"),
                                 round(float(l.get("amount") or 0), 2)))
    actual = [(l.get("booking_id"), l.get("description"),
               round(float(l.get("amount") or 0), 2)) for l in lines]
    return bool(actual) and sorted(map(str, actual)) == sorted(map(str, expected))


def withdraw_for_released_holds(tenant_id: str, log_id: str, released: list[dict],
                                client=None) -> dict | None:
    """The holds an email placed were just released (skip, or a redraft): the
    quote auto-drafted from them must not outlive them (round three, 1 Oct 2026
    - F26: three redrafts left a draft quote naming cancelled bookings at the
    old prices). Withdraws it (status 'cancelled', as every withdrawn quote is)
    ONLY when it is the AI's own draft for this email (created_by 'ai',
    quotes.email_log_id), still a draft, and still exactly what the auto-draft
    wrote. Same setting as drafting: off means this does nothing either.
    Best-effort; returns the withdrawn quote or None."""
    try:
        if not log_id or not released:
            return None
        cfg = db.get_tenant_config(tenant_id, client=client) or {}
        if mode_for(cfg) == "off":
            return None
        quote = db.quote_for_log(log_id, tenant_id, client=client)
        if (not quote or quote.get("status") != "draft"
                or quote.get("created_by") != "ai"):
            return None
        lines = db.list_quote_lines(quote["id"], tenant_id, client=client)
        if not _still_what_the_draft_wrote(quote, lines, released):
            return None
        if not db.withdraw_draft_quote(quote["id"], tenant_id, client=client):
            return None
        ref = quote.get("quote_ref")
        db.log_agent_event(
            tenant_id, "quote_withdrawn",
            f"Withdrew draft quote {ref} - the bookings it was drafted from "
            "were released",
            contact=quote.get("customer_email"), email_log_id=log_id,
            detail={"quote_ref": ref, "total": float(quote.get("total") or 0),
                    "bookings": [b.get("booking_ref") or b.get("id") for b in released]},
            client=client)
        trace.emit(tenant_id, "quote_autodraft_withdrawn", email_log_id=log_id,
                   contact=quote.get("customer_email"),
                   detail={"quote_ref": ref})
        # Its "Quote drafted" card on Today is stale now - settle it.
        db.resolve_concierge_notifications(
            tenant_id, kind="quote_drafted", url=f"/app/quotes/{quote['id']}",
            client=client)
        return quote
    except Exception:
        log.warning("withdrawing the auto-drafted quote failed for email_log %s",
                    log_id, exc_info=True)
        return None


def link_for_send(tenant_id: str, cfg: dict | None, log_id: str,
                  client=None) -> str:
    """AUTO mode only: the accept-link block to append to the outgoing reply,
    flipping the drafted quote to 'sent' in the same breath (guarded — a quote
    the owner already sent or cancelled is left alone). Empty string otherwise,
    so callers can always concatenate."""
    try:
        if mode_for(cfg) != "auto" or not log_id:
            return ""
        quote = db.quote_for_log(log_id, tenant_id, client=client)
        if not quote or quote.get("status") != "draft":
            return ""
        from app.quote_send import quote_url
        db.set_quote_status(quote["id"], tenant_id, "sent", client=client)
        ref = quote.get("quote_ref")
        total = float(quote.get("total") or 0)
        db.log_agent_event(
            tenant_id, "sent",
            f"Quote {ref} (£{total:,.2f}) went out with the reply — the "
            "customer can accept it online",
            contact=quote.get("customer_email"), email_log_id=log_id,
            client=client)
        trace.emit(tenant_id, "quote_sent_with_reply", email_log_id=log_id,
                   contact=quote.get("customer_email"),
                   detail={"quote_ref": ref, "total": total})
        return (f"\n\nYour quote ({ref}, £{total:,.2f}) is ready to view and "
                f"accept online here:\n{quote_url(quote['id'])}")
    except Exception:
        log.warning("quote send-link failed for email_log %s", log_id,
                    exc_info=True)
        return ""
