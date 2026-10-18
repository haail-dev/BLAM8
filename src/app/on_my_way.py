"""On my way (docs/benchmark-hot-tub-servicing.md §6, item 1).

The touch on the day: the operator taps "On my way" on a job and picks an
ETA; the agent writes two lines in the tenant's voice to whoever is ON SITE
(the housekeeper, the tenant — job_parties.resolve), else the booking's own
contact, and it goes straight out — the tap is the approval, like "Approve &
send", because the operator chose the message's one fact. Email today, SMS the
day the channel is live (sms.preferred_sms_destination decides, exactly as
the day-before reminder does). ServiceM8 and Commusoft send one; the tenant's
own voice note asked for it ("always say when we'll be there"); and it is the
customer-side proof against the top complaint in the sector — not turning up.

No map arithmetic: the operator knows the ETA.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from uuid import uuid4

from app import actor, agent, db, disclosure, mailbox, sms, usage, on_my_way_store as store
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

PURPOSE = ("A two-line 'on my way' message: we are on our way to the customer now and "
           "will be there at the time given. Say the arrival time exactly as stated, "
           "who is coming if named, and nothing else — no confirmation of what will "
           "be done, no prices, no questions. Warm, short, in the business's voice. "
           "Never guess a pronoun from a name: write '<Name> is on the way to you', "
           "or 'we', never 'his' / 'her'.")

# The ETA picks the job page offers (minutes); a typed clock time is also fine.
ETA_CHOICES = (15, 30, 45, 60, 90)

# Where the work happens is not a switch the owner ticks — it is in what they
# ask for when booking. A trade, a field engineer, a cleaning round asks for
# the job address; a kennels, a salon, a garage does not: the customer comes
# to them, and "On my way" has nowhere to go (kennels film, 2026-09-20: the
# stays job page offered an ETA to a dog's owner).
_WHERE_WORDS = re.compile(r"\b(address|postcode|post code|location)\b", re.IGNORECASE)


def goes_to_customer(intake_fields, booking: dict | None = None) -> bool:
    """Does this job take the business to the customer? Yes when the booking
    itself carries somewhere to go — a routing postcode, or an address-shaped
    detail with a value in it — or when the business's booking requirements
    REQUIRE where the work is (a 'Job address', 'Site address', 'Collection
    address' field on the intake). An optional where-field ('Pickup location —
    if you travel to the pupil') defers to the booking: a pickup given is a
    visit, none is not. Otherwise the customer comes to them."""
    b = booking or {}
    if b.get("delivery_postcode") or b.get("collection_postcode"):
        return True
    details = b.get("details") if isinstance(b.get("details"), dict) else {}
    for key, value in details.items():
        if _WHERE_WORDS.search(str(key)) and str(value or "").strip():
            return True
    for f in intake_fields or []:
        if not isinstance(f, dict) or not f.get("required"):
            continue
        if _WHERE_WORDS.search(str(f.get("label") or "")):
            return True
    return False


def eta_phrase(eta: str | None, now: datetime | None = None) -> str | None:
    """'30' → 'in about 30 minutes (around 10:45)'; '10:45' → 'at about 10:45';
    anything else → None (the route refuses)."""
    s = str(eta or "").strip().lower()
    now = now or datetime.now(DIARY_TZ)
    if re.fullmatch(r"\d{1,3}", s):
        mins = int(s)
        if not 5 <= mins <= 240:
            return None
        at = (now + timedelta(minutes=mins)).strftime("%H:%M")
        return f"in about {mins} minutes (around {at})"
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
    if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
        return f"at about {int(m.group(1)):02d}:{m.group(2)}"
    return None


def recipient(booking: dict, parties: list[dict]) -> dict:
    """Who gets the message: the confirmed on-site party, else the booking's
    contact. {name, email, phone, is_fallback}."""
    from app import job_parties
    return job_parties.resolve(booking, parties)["on_site"]


def facts_for(booking: dict, who: dict, phrase: str, engineer: str | None) -> str:
    parts = [f"We are on our way to the customer now and will arrive {phrase}."]
    if who.get("name"):
        parts.append(f"The message is to {who['name']}" + (f" ({who['label']})" if who.get("label") else "") + ".")
    if engineer:
        parts.append(f"{engineer} is the one coming.")
    summary = (booking.get("summary") or "").strip()
    if summary:
        parts.append(f"The visit: {summary}.")
    if booking.get("booking_ref"):
        parts.append(f"Booking reference: {booking['booking_ref']}.")
    return " ".join(parts)


def page_state(tenant_id: str, booking_id: str, booking: dict | None = None) -> dict:
    """Read authoritative state; an unavailable store never becomes permission."""
    try:
        row = store.latest(tenant_id, booking_id)
        if row and (row.get("receipt") or {}).get("reason") == "historical_evidence_requires_review":
            day = str((booking or {}).get("booking_date") or "")[:10]
            try:
                past = datetime.fromisoformat(day).date() < datetime.now(DIARY_TZ).date()
            except ValueError:
                past = False
            if past:
                return {**row, "hide_historical": True}
        return row or {}
    except Exception:
        return {"state": "unavailable"}


def outcome(row: dict, *, duplicate=False) -> dict:
    state = row.get("state")
    return {**(row.get("receipt") or {}), "ok": state == "sent", "state": state,
            "error": row.get("error") or (None if state == "sent" else
                "in_progress" if state == "preparing" else state or "unavailable"),
            "action_id": row.get("id"), "action_key": row.get("action_key"),
            "attempt": row.get("attempt"), "person_name": row.get("person_name"),
            "duplicate": duplicate}


async def send(tenant_id: str, booking: dict, eta: str, *, engineer: str | None = None,
               cfg: dict | None = None, client=None, action_key: str = "initial",
               sheet_id: str | None = None, resend_attempt: str | None = None) -> dict:
    """Claim, prepare, fence, dispatch once, then persist the provider receipt.

    A crash while preparing is reclaimable. After the dispatch marker commits,
    Failure or a lost response is uncertain. An owner can explicitly choose
    to resend that attempt, accepting that the customer may get it twice.
    """
    if not eta_phrase(eta):
        return {"ok": False, "error": "bad_eta"}
    attempt = str(uuid4())

    def change(operation, data=None):
        return store.action(tenant_id, booking["id"], action_key, attempt,
                            operation, data, sheet_id)

    try:
        claim_data = {"eta": eta, "person_name": engineer, "initiator": actor.stamp(tenant_id)}
        if resend_attempt:
            claim_data["resend_attempt"] = resend_attempt
        row = change("owner_resend" if resend_attempt else "claim", claim_data)
    except Exception:
        return {"ok": False, "error": "unavailable"}
    if not row.get("owned"):
        return outcome(row, duplicate=True)
    phrase = eta_phrase(row["eta"])
    engineer = row.get("person_name")
    receipt = {}
    try:
        # Refresh facts and travel eligibility after a potentially slow claim.
        current = db.get_booking(booking["id"], tenant_id, client=client)
        if not current or current.get("status") not in ("confirmed", "proposed"):
            raise ValueError("notlive")
        sched = db.get_scheduling_config(tenant_id, client=client) or {}
        if not goes_to_customer(sched.get("intake_fields"), current):
            raise ValueError("nowhere")
        booking = current
        parties = db.list_job_parties(tenant_id, booking["id"], client=client)
        who = recipient(booking, parties)
        to = who.get("email") or booking.get("contact")
        if not to and not who.get("phone"):
            failed = change("fail", {"reason": "no_recipient"})
            return {**outcome(failed), "error": "no_recipient"}
        dest_sms = sms.preferred_sms_destination(tenant_id, to) if to else None
        if not dest_sms and not to and who.get("phone"):
            dest_sms = who["phone"]
        channel = "sms" if dest_sms else "email"
        if channel == "email":
            access, _, _ = await mailbox._access(tenant_id, booking.get("mailbox_id"))
            if not access:
                raise mailbox.MailNotSentBeforeRequest("No mailbox connected for this tenant")
        cfg = cfg or db.effective_config(tenant_id, booking.get("mailbox_id"), client=client)
        facts = facts_for(booking, who, phrase, engineer)
        result = await agent.compose_outbound(cfg, PURPOSE, facts, channel=channel,
                                              tenant_id=tenant_id, log_operation="on_my_way")
        # The booking, settings and sheet can change while composition awaits.
        current = db.get_booking(booking["id"], tenant_id, client=client)
        sched = db.get_scheduling_config(tenant_id, client=client) or {}
        if current != booking or not goes_to_customer(sched.get("intake_fields"), current):
            raise ValueError("changed_before_send")
        if sheet_id:
            from app import job_sheets
            if job_sheets.ended(current):
                raise ValueError("notlive")
        if db.list_job_parties(tenant_id, booking["id"], client=client) != parties:
            raise ValueError("recipient_changed")
        if db.effective_config(tenant_id, booking.get("mailbox_id"), client=client) != cfg:
            raise ValueError("configuration_changed")
        ref = booking.get("booking_ref")
        subject = "On our way" + (f" — {ref}" if ref else "")
        body = disclosure.wrap(cfg, result.text, channel=channel)
        log_id, _ = db.insert_email_log(
            tenant_id, dest_sms or to, subject, f"[on my way] {facts}", result.text,
            status="skipped", kind="on_my_way", mailbox_id=booking.get("mailbox_id"),
            thread_id=f"sms:{dest_sms}" if dest_sms else booking.get("thread_id"), channel=channel)
        receipt = {"to": dest_sms or to, "recipient_name": who.get("name") or dest_sms or to,
                   "channel": channel, "text": result.text,
                   "log_id": log_id, "eta": phrase, "subject": subject,
                   "body": body, "mailbox_id": booking.get("mailbox_id"),
                   "booking_updated_at": booking.get("updated_at")}
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="on_my_way", email_log_id=log_id)
    except Exception:
        try:
            return outcome(change("fail", {**receipt, "reason": "pre_send_failure"}))
        except Exception:
            pass  # Still preparing: safe recovery needs a new fenced attempt.
        return {"ok": False, "error": "pre_send_failure", "state": "pre_send_failure"}
    try:
        dispatch = change("dispatch", receipt)
    except Exception:
        # The marker may have committed. Do not send or reset it on a lost reply.
        return {"ok": False, "error": "unknown", "state": "unknown"}
    if not dispatch.get("owned"):
        return outcome(dispatch)
    try:
        if dest_sms:
            provider_id = await sms.send_sms(tenant_id, dest_sms, body, email_log_id=log_id)
        else:
            provider_id = await mailbox.send_mail(tenant_id, to, subject, body,
                                                  mailbox_id=booking.get("mailbox_id"))
        row = change("sent", {"provider_id": provider_id,
                               "accepted_at": datetime.now(DIARY_TZ).isoformat()})
        if not row.get("owned"):
            return {"ok": False, "error": "unknown", "state": "unknown"}
    except (sms.SmsNotConfigured, sms.SmsOptedOut, mailbox.MailNotSentBeforeRequest):
        # These typed rejections occur before a message-send request.
        try:
            return outcome(change("not_sent", {"reason": "rejected_before_request"}))
        except Exception:
            return {"ok": False, "error": "unknown", "state": "unknown"}
    except Exception:
        # Includes receipt persistence failure after the provider accepted.
        return {"ok": False, "error": "unknown", "state": "unknown",
                "log_id": log_id, "action_key": action_key, "attempt": attempt,
                "person_name": engineer}
    # Secondary projections cannot turn a proven send into a retryable failure.
    try:
        db.mark_email_log_sent(log_id, tenant_id, via="human", edited=False)
        db.log_agent_event(
            tenant_id, "on_my_way",
            f"Told {who.get('name') or dest_sms or to} we're on our way to {ref or 'the visit'} — arriving {phrase}",
            direction="outbound", contact=dest_sms or to, subject=subject, email_log_id=log_id,
            booking_id=booking.get("id"), booking_ref=ref,
            event_date=str(booking.get("booking_date") or "")[:10] or None,
            detail={"channel": channel, "eta": phrase, "action_id": row["id"]})
    except Exception:
        log.warning("On-my-way receipt saved; history projection needs repair (action=%s)", row["id"])
    return outcome(row)
