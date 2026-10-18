"""Inbound SMS receiver → automatic drafting (docs/modules/module-sms-channel.md §3).

/webhook/sms — Twilio POSTs here (form-encoded) when a text arrives on a
tenant's provisioned number. We verify the request signature, route to the
tenant by the To number, honour STOP/START/HELP without ever touching the LLM,
then hand the message to the SAME pipeline the mailboxes use — normalised to
the provider-neutral email dict, with channel='sms' so the draft is composed
in the SMS register and any auto-send leaves through Twilio.

Public endpoint (called by Twilio, not a logged-in user), same posture as the
Graph/Gmail receivers in src/app/webhook.py: rate-limited, constant-time verified,
and it always ACKs (2xx) once authenticity is established — Twilio retries on
non-2xx, and the MessageSid → source_message_id unique index already makes any
retry a no-op, so a retry storm can only ever waste Twilio's time, not draft
twice. The pipeline call is awaited inline (the Graph-webhook precedent); a
slow draft that trips Twilio's timeout just triggers one such deduped retry.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app import agent, db, disclosure, events, pipeline, sms, usage
from app.security import rate_limit

log = logging.getLogger(__name__)
router = APIRouter(tags=["sms"])

# Compliance keywords (spec §11) — handled before the pipeline, static replies,
# no LLM. Matching strips everything non-alphabetic so "STOP!" and " Stop "
# collapse to one check. START deliberately excludes YES (a booking-confirm
# reply must never read as an opt-in/out action).
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop"}
HELP_WORDS = {"help", "info"}

# Confirm-by-reply (spec §6): a bare C/YES on a thread with an upcoming confirmed
# booking flips client_confirmed_at — no LLM, instant, and it feeds the no-show
# scorer. Only fires when such a booking EXISTS; otherwise a bare "yes" falls
# through to the drafter (it's usually answering a question mid-conversation).
CONFIRM_WORDS = {"c", "y", "yes", "confirm", "confirmed"}


def _twiml(inner: str = "") -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
        media_type="application/xml")


def _confirmable_booking(tenant_id: str, sender: str) -> dict | None:
    """The next upcoming CONFIRMED booking this texter could be confirming —
    keyed by their number (SMS-booked) or the email of the CRM contact whose
    phone this is (email-booked, SMS-preferring). Best-effort: None on any
    lookup failure, and the message falls through to the drafter."""
    try:
        addrs = [sender]
        contact = db.crm_contact_by_phone(tenant_id, sender)
        if contact and contact.get("email"):
            addrs.append(contact["email"])
        today = datetime.now(timezone.utc).date().isoformat()
        return db.upcoming_booking_for_contacts(tenant_id, addrs, today)
    except Exception:
        log.warning("sms webhook: confirm lookup failed for tenant %s", tenant_id,
                    exc_info=True)
        return None


@router.post("/webhook/sms", dependencies=[Depends(rate_limit("webhook_sms", 120, 60))])
async def webhook_sms(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    # Authenticity first — the Twilio mirror of the Graph clientState check. A
    # forged/unsigned request gets a 403 before any database touch.
    if not sms.validate_signature(sms.webhook_url("/webhook/sms"), form,
                                  request.headers.get("X-Twilio-Signature")):
        return Response(status_code=403)

    to = sms.normalize_e164(form.get("To"))
    sender = sms.normalize_e164(form.get("From"))
    body = (form.get("Body") or "").strip()
    conn = db.channel_connection_by_number(to) if to else None
    if not conn or not sender:
        # A number we don't hold (stale Twilio config) or an unparseable sender
        # (alphanumeric originator). ACK — a retry can't change either.
        log.info("sms webhook: no route for To=%s From=%s", to, form.get("From"))
        return _twiml()
    tenant_id = conn["tenant_id"]

    keyword = "".join(c for c in body.lower() if c.isalpha())
    if keyword in STOP_WORDS:
        db.sms_opt_out(tenant_id, sender)
        return _twiml("<Message>You've been unsubscribed and won't receive further "
                      "texts from us. Reply START to opt back in.</Message>")
    if keyword in START_WORDS:
        db.sms_opt_in(tenant_id, sender)
        return _twiml("<Message>You're opted back in — thanks. Reply STOP at any "
                      "time to unsubscribe.</Message>")
    if keyword in HELP_WORDS:
        name = (db.get_tenant(tenant_id) or {}).get("name") or "this business"
        return _twiml(f"<Message>This number is the text line for {name}. Reply "
                      "STOP to unsubscribe, START to opt back in.</Message>")
    if keyword in CONFIRM_WORDS:
        booking = _confirmable_booking(tenant_id, sender)
        if booking:
            db.stamp_client_confirmed(booking["id"], tenant_id)
            db.log_agent_event(
                tenant_id, "confirmed",
                f"{sender} confirmed their booking by text",
                direction="inbound", contact=sender, booking_id=booking["id"],
                booking_ref=booking.get("booking_ref"),
                event_date=str(booking.get("booking_date"))[:10])
            date = str(booking.get("booking_date"))[:10]
            return _twiml(f"<Message>Thanks — you're confirmed for {date}. Reply "
                          "here if anything changes.</Message>")
        # No upcoming booking to confirm — treat it as a normal message below.

    if body:  # media-only/empty messages have nothing to draft from
        try:
            await pipeline.draft_for_email(
                tenant_id,
                {
                    "from_address": sender,
                    "subject": "",                       # SMS has no subject
                    "body": body,
                    "thread_id": f"sms:{sender}",        # one rolling thread per number (§3)
                },
                source_message_id=form.get("MessageSid") or None,
                auto_send_ok=True,                        # gated by tenant auto_send inside
                mailbox_id=None,
                channel="sms",
            )
        except Exception:
            # Never fail the webhook once authenticated — the poller-style safety
            # net here is Twilio's own retry, which the MessageSid dedupe absorbs.
            log.warning("sms webhook: drafting failed for tenant %s", tenant_id,
                        exc_info=True)
    return _twiml()


# ── Missed-call text-back (docs/modules/module-sms-channel.md §5) ─────────────────────
# The tenant conditionally diverts unanswered calls to their BLAM8 number
# (*61*<number># — no porting, their public identity is untouched). A call
# arriving here therefore means "the tenant didn't pick up": we answer with a
# one-line apology + hang up, and text the caller back. The reply they send
# comes in through /webhook/sms above and the normal drafting flow takes over.
# No recording, no ring-through, no voice AI (spec D4).

# Twilio presents withheld caller ID as the literal word or this magic number
# (2-6-6-6-9-6-6-8-7 spells ANONYMOUS on a keypad).
_ANONYMOUS = {"+266696687"}

MISSED_CALL_PURPOSE = (
    "The customer just called the business and the call went unanswered. Text "
    "them back straight away: apologise briefly for missing the call and invite "
    "them to reply to this text with what they need, so it can be sorted by message."
)

# In-flight text-backs — referenced so the event loop can't GC a running task
# (the src/app/webhook.py _push_tasks pattern); each removes itself when done.
_bg_tasks: set = set()


def _schedule(coro) -> None:
    """Answer the call NOW, compose the text in the background: TwiML must reach
    Twilio while the caller is still on the line, and an LLM call is too slow."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _text_back_allowed(conn: dict, caller: str | None) -> bool:
    """The §5 guards, cheap-query only (they run inline so the <Say> can tell the
    truth about whether a text is coming). All fail closed on 'skip'."""
    if not caller or caller in _ANONYMOUS:
        return False                             # withheld caller — nobody to text
    tenant_id = conn["tenant_id"]
    if not (conn.get("config") or {}).get("text_back_enabled", True):
        return False
    if db.sms_opted_out(tenant_id, caller):
        return False                             # STOP means calls too
    if db.channel_connection_by_number(caller):
        return False                             # another BLAM8 number — never loop the platform
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if db.recent_missed_call_text(tenant_id, caller, day_ago):
        return False                             # one text-back per caller per 24h
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                  microsecond=0).isoformat()
    if db.sms_thread_since(tenant_id, caller, midnight):
        return False                             # already mid-conversation today
    return True


async def _handle_missed_call(conn: dict, caller: str, call_sid: str | None) -> None:
    """Compose + queue (and under auto_send, send) the text-back. Best-effort:
    a failure here loses one nudge, never the webhook."""
    tenant_id = conn["tenant_id"]
    try:
        cfg = db.effective_config(tenant_id, None)
        result = await agent.compose_outbound(
            cfg, MISSED_CALL_PURPOSE,
            f"The caller's number: {caller}. They called just now and the call "
            "was not answered. No other details are known — invite them to reply "
            "with what they need.",
            channel="sms", tenant_id=tenant_id, log_operation="missed_call")
        log_id, created = db.insert_email_log(
            tenant_id, caller, "", "(Missed call — no message)", result.text,
            status="pending", kind="missed_call", thread_id=f"sms:{caller}",
            source_message_id=call_sid, channel="sms",
            request_summary="Missed call", action_summary="Texted the caller back")
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="missed_call", email_log_id=log_id)
        if not created:
            return                               # duplicate voice webhook delivery
        db.log_agent_event(
            tenant_id, "missed_call",
            f"Missed call from {caller} — drafted a text back",
            direction="outbound", contact=caller, email_log_id=log_id)
        events.publish(tenant_id, {"type": "draft_ready", "contact": caller,
                                   "mailbox_id": None, "log_id": log_id,
                                   "thread_state": "in_progress"})
        if cfg.get("auto_send"):
            # A text-back is only worth anything in the first minutes, so under
            # auto_send it goes straight out — with the EU AI Act one-liner, like
            # every other auto-sent message (docs/security/eu-ai-act-compliance.md).
            body_out = disclosure.wrap(cfg, result.text, channel="sms", kind="reply")
            await sms.send_sms(tenant_id, caller, body_out, email_log_id=log_id)
            db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
            events.publish(tenant_id, {"type": "draft_sent", "contact": caller,
                                       "mailbox_id": None, "log_id": log_id})
            db.log_agent_event(tenant_id, "sent",
                               f"Sent the missed-call text to {caller} (auto)",
                               direction="outbound", contact=caller,
                               email_log_id=log_id, detail={"via": "auto"})
    except Exception:
        log.warning("missed-call text-back failed for tenant %s caller %s",
                    tenant_id, caller, exc_info=True)


@router.post("/webhook/voice", dependencies=[Depends(rate_limit("webhook_voice", 120, 60))])
async def webhook_voice(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not sms.validate_signature(sms.webhook_url("/webhook/voice"), form,
                                  request.headers.get("X-Twilio-Signature")):
        return Response(status_code=403)

    to = sms.normalize_e164(form.get("To"))
    caller = sms.normalize_e164(form.get("From"))
    conn = db.channel_connection_by_number(to) if to else None
    if not conn:
        return _twiml("<Hangup/>")               # a number we don't hold
    name = escape((db.get_tenant(conn["tenant_id"]) or {}).get("name") or "us")

    if _text_back_allowed(conn, caller):
        _schedule(_handle_missed_call(conn, caller, form.get("CallSid") or None))
        say = (f"Sorry — {name} can't take your call right now. We're sending "
               "you a text message; reply there and we'll get you sorted.")
    else:
        # Guarded off (withheld number, opted out, already texted today...) — the
        # <Say> must not promise a text that isn't coming.
        say = f"Sorry — {name} can't take your call right now. Please try again later."
    return _twiml(f"<Say>{say}</Say><Hangup/>")
