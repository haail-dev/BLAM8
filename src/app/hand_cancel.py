"""A booking cancelled BY THE BUSINESS, by hand in the diary, tells the customer
(run #8, 2026-09-05 — benchmark stake I: Total Drive's 1★ theme (paraphrased) is "edited
session times are not sent as a notification"; on the rig the owner cancelled
nine Saturdays and the participant learned nothing).

The message is composed into the approval queue (approve mode — the owner
is right there, having just cancelled) and raises the feed card + push. Only
a CONFIRMED booking with a contact earns one: a released hold or a declined
proposal was never the customer's booking. The diary's cancel-all stays
silent — a reset or a re-plan is not a message per row.
"""
import logging

from app import db

log = logging.getLogger(__name__)

KIND = "hand_cancel"                  # feed / push / agent_events / email_log kind

PURPOSE = ("A short message telling the customer that the business has had to cancel "
           "one of their bookings — which one (its reference, day and time), a plain "
           "apology in the business's voice, and one line inviting them to reply for "
           "another time. Say nothing about why unless the facts give a reason, promise "
           "nothing about charges or credits, ask no questions beyond the invitation. "
           "Write to the customer as 'you'.")


def facts_for(booking: dict, contact_name: str | None, nouns: dict | None = None,
              reason: str | None = None) -> str:
    """The facts block: the booking in the business's own word for one."""
    from app.dates import pretty_date
    word = (nouns or {}).get("booking") or "booking"
    ref = booking.get("booking_ref") or "the booking"
    when = pretty_date(booking.get("booking_date")) if booking.get("booking_date") else ""
    # The weekday rides with the date (live 2026-09-05: without it the model
    # wrote "Friday 26th September" for a Saturday).
    try:
        from datetime import date as _date
        when = _date.fromisoformat(str(booking.get("booking_date"))[:10]).strftime("%A") + " " + when
    except (TypeError, ValueError):
        pass
    hhmm = ""
    try:
        from app import slotgrid
        if booking.get("start_block") is not None:
            hhmm = slotgrid.hhmm(int(booking["start_block"]))
        elif booking.get("window") and str(booking["window"]) not in ("day", "am", "pm"):
            hhmm = str(booking["window"])
    except (TypeError, ValueError):
        hhmm = ""
    svc = ((booking.get("details") or {}).get("Service")) or booking.get("summary") or word
    parts = [f"The business has cancelled the customer's {word} {ref}: {svc}"
             + (f" on {when}" if when else "") + (f" at {hhmm}" if hhmm else "") + "."]
    if reason:
        parts.append(f"The reason to give: {reason}.")
    parts.append("Invite them to reply for another time; the diary will be checked when they do.")
    if contact_name:
        parts.append(f"The message is to {contact_name}.")
    return " ".join(parts)


async def on_hand_cancel(tenant_id: str, booking: dict, *, reason: str | None = None,
                         client=None) -> dict:
    """The owner cancelled a confirmed booking from the diary: compose the note
    into the approval queue and raise the card. Never raises past here."""
    from app import agent, diary_config, push, usage
    to = (booking or {}).get("contact")
    if not booking or not to or booking.get("status") != "confirmed":
        return {"ok": True, "queued": False, "why": "no contact or not a confirmed booking"}
    contact = db.crm_contact_by_email(tenant_id, to, client=client) or {}
    cfg = db.effective_config(tenant_id, booking.get("mailbox_id"), client=client)
    nouns = diary_config.nouns_of(db.get_scheduling_config(tenant_id, client=client))
    facts = facts_for(booking, contact.get("name"), nouns, reason=reason)
    try:
        result = await agent.compose_outbound(cfg, PURPOSE, facts, channel="email",
                                              tenant_id=tenant_id, log_operation=KIND)
    except Exception:
        log.warning("hand-cancel compose failed (tenant=%s)", tenant_id, exc_info=True)
        return {"ok": False, "error": "compose_failed"}
    ref = booking.get("booking_ref") or booking.get("id", "")[:8]
    subject = f"{ref} cancelled — {nouns.get('booking') or 'booking'}"
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[cancelled by hand] {facts}", result.text,
        status="pending", kind=KIND, thread_id=booking.get("thread_id"), channel="email")
    usage.record_anthropic(tenant_id, result.model, result.usage, operation=KIND, email_log_id=log_id)
    db.log_agent_event(tenant_id, KIND,
                       f"{ref} cancelled by hand — a note to {contact.get('name') or to} is waiting for approval",
                       contact=to, booking_id=booking.get("id"), booking_ref=booking.get("booking_ref"),
                       event_date=str(booking.get("booking_date") or "") or None)
    push.notify(tenant_id, title=f"{ref} cancelled — tell {contact.get('name') or 'the customer'}?",
                body=f"A message to {contact.get('name') or to} is waiting for your approval.",
                url=f"/app/conversations/view?contact={to}", tag=f"hand-cancel-{booking.get('id')}",
                kind=KIND, contact=to, contact_name=contact.get("name"))
    return {"ok": True, "queued": True, "log_id": log_id, "text": result.text}
