"""Drafting from the voice Concierge (Kris 2026-09-23).

"Unless the VC agent calls the Drafting agent to write the draft, then it's
just displayed in the VC view." So the Concierge never writes the email
itself. It picks WHO and WHICH KIND, passes the owner's own words through as
an instruction, and the Drafting agent writes it with everything it uses for
the inbox:

  * a REPLY re-drafts the customer's latest message through
    pipeline.redraft_email_log: knowledge base, diary tools and holds, the
    business's rules, the working panel — plus the owner's instruction;
  * a NEW email goes through agent.compose_outbound, the same composer the
    chases and sequences use, in the business's voice from the facts given.

Either way the result is an ordinary pending draft on email_log, so it waits
in the customer's thread if the owner hangs up, and the voice screen shows it
for approval. With full-auto on (auto_send) a REPLY is sent straight away
through the thread's own send path (dashboard.send_approved) as an automated
send. A NEW email never is (security review 2026-09, M21): it is written from
the model's own instruction to an address the model supplied, so it always
waits for the owner's yes, like every other Concierge write — and it may only
go to an address this turn's lookups returned or a contact already in the CRM.

Which one is the owner's call ("It depends on the request from the User"):
"reply to Hannah" is a reply, "email Hannah about Friday" is a new email. A
reply with nothing waiting says so and offers a new email instead.
"""
import logging
from urllib.parse import quote

from app import db, events, sign_off

log = logging.getLogger(__name__)

TOOL_NAME = "draft_email"
HINT = "Writing the email…"
LABEL = "Wrote the email"

DRAFT_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Have the business's Drafting agent write an email to a customer — the "
        "same agent that drafts the inbox's replies, with the knowledge base, "
        "diary and the business's voice. Call it ONLY when the owner asks for "
        "an email to be written or sent (reply to, email, write to, follow up "
        "with, tell them…) — never on your own initiative. Look the customer "
        "up first if you only have a name: `contact` must be their email "
        "address from a tool result in this conversation. mode='reply' answers "
        "their latest message ('reply to Hannah'); mode='new' starts a fresh "
        "email ('email Hannah about Friday', 'follow up with her'). Put what "
        "the owner wants it to say in `instruction`, in their words. The draft "
        "appears on the owner's screen; the result tells you whether it is "
        "waiting for approval or was already sent (full-auto)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "description": "the customer's email address"},
            "mode": {"type": "string", "enum": ["reply", "new"]},
            "instruction": {"type": "string",
                            "description": "what the owner wants the email to say or do"},
            "subject": {"type": "string",
                        "description": "mode='new' only: a short subject line"},
        },
        "required": ["contact", "mode"],
    },
}


# Where an address may come from in a tool result: the structured fields the
# lookups fill, never free text (an email body can carry any address it likes).
ADDRESS_KEYS = frozenset(("email", "contact_email", "contact", "owner_email",
                          "customer_email", "from_address", "to"))


def addresses_in(result) -> set[str]:
    """Every customer address a tool result names in a structured field,
    lowercased — the voice turn collects these as it goes (M21)."""
    out: set[str] = set()

    def walk(v, key=None):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and key in ADDRESS_KEYS:
            a = v.strip().lower()
            if "@" in a and " " not in a and len(a) <= 254:
                out.add(a)
    walk(result)
    return out


def _known_recipient(ctx, contact: str, known: set[str] | None) -> bool:
    """May a NEW email go to this address? Only one this turn's lookups
    returned, or an existing CRM contact of this business. A failed CRM read
    is a no."""
    if contact in {str(a).strip().lower() for a in (known or ())}:
        return True
    try:
        return bool(db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client))
    except Exception:
        log.warning("voice draft: recipient check failed (tenant %s)", ctx.tenant_id,
                    exc_info=True)
        return False


def _hold_result(name: str, reason: str, contact: str = "", instruction: str = "",
                 can_confirm: bool = False) -> dict:
    """What the model is told when booking wording is held: the reason and the
    way forward FOR THAT REASON (docs/plan-concierge-confirm-first.md). A held
    time waits on the business, so confirm it first (one card: confirm, then
    the email); a past date is moved first; only an offered slot gets the
    pencilled-in, please-choose wording. Nothing was queued."""
    from app.concierge_booking_claims import classify_hold, hold_ref
    code, ref = classify_hold(reason), hold_ref(reason) or "the booking"
    head = f"{reason} Nothing was queued or sent. "
    confirm = None
    if code == "firm_on_proposed" and can_confirm and ref.startswith("BK-"):
        # The card is staged by the server (the voice route attaches it to this
        # turn): the model never has to write the action, and the owner's yes
        # confirms the booking and then drafts the email.
        confirm = {"kind": "confirm_booking", "booking_id": ref, "contact": contact,
                   "instruction": instruction[:1500]}
        msg = (head + f"{ref} is a time held for {name} that waits on the OWNER. A card is ALREADY "
               "on the owner's screen: Confirm it, and the email is drafted afterwards for their "
               "approval (if the confirm fails, no email is made). Say in one sentence that it is "
               "only held and not confirmed, then ask 'Shall I confirm it and draft the email?'. "
               "Do NOT offer a 'pencilled in' email (the customer has nothing to confirm), do NOT "
               "write an action of your own and do NOT call show_draft.")
    elif code == "firm_on_proposed":
        msg = (head + f"{ref} is a time held for {name} that waits on the owner, who can confirm it "
               "in the diary (/app/diary); the email can say it is booked once it is confirmed. "
               "Tell the owner plainly. Do NOT call show_draft.")
    elif code == "past_date":
        msg = (head + f"{ref} was on a day that has passed, so it needs moving, not confirming: "
               f"offer to move it first (action kind 'move_booking', booking_id '{ref}') and "
               "ask which day. Do NOT call show_draft.")
    elif code == "firm_on_offered":
        msg = (head + f"{name} has not picked a time yet. Offer an email that says the times are "
               "pencilled in and asks them to choose or confirm: if the owner says yes, call "
               "draft_email AGAIN right away with that wording in `instruction` (never 'booked' "
               "or 'confirmed'). Do NOT call show_draft for it.")
    else:
        msg = (head + "Tell the owner plainly why, and offer to reword it or to check the diary "
               "with them. Do NOT call show_draft for it.")
    out = {"ok": False, "status": "empty", "message": msg}
    if confirm:
        out["confirm_action"] = confirm    # popped by the voice route, never shown to the model
    return out


def _can_confirm(ctx) -> bool:
    """A diary to confirm in (and so a confirm card) - the same test the typed path uses."""
    from app.concierge_booking_claims import _can_confirm as _diary
    return _diary(ctx.tenant_id)


def _log_hold(ctx, contact: str, subject: str, reason: str, mode: str, instruction: str) -> None:
    """The hold, on the agent log: show_draft reads it so a follow-up "send it"
    never resurfaces an older, unrelated draft to the same customer."""
    db.log_agent_event(ctx.tenant_id, "concierge_refused",
                       f"Held a voice email to {contact}: {reason}",
                       contact=contact, subject=subject,
                       detail={"via": "concierge_voice", "mode": mode,
                               "reason": "booking_wording", "hold": reason,
                               "instruction": instruction[:500]},
                       client=ctx.client)


def _recent_hold(ctx, contact: str) -> str | None:
    """The reason of a booking-wording hold on this customer in the last half
    hour with nothing drafted or sent since, else None (a failed read is None:
    show_draft then behaves as before)."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    try:
        rows = ((ctx.client or db.db()).table("agent_events").select("kind,detail,created_at")
                .eq("tenant_id", ctx.tenant_id).eq("contact", contact).gte("created_at", since)
                .order("created_at", desc=True).limit(10).execute().data or [])
    except Exception:
        return None
    for r in rows:
        if r.get("kind") in ("drafted", "sent"):
            return None
        d = r.get("detail") or {}
        if r.get("kind") == "concierge_refused" and d.get("reason") == "booking_wording":
            return str(d.get("hold") or "That email is held.")
    return None


def _name_for(rows: list[dict], contact: str) -> str:
    for r in reversed(rows or []):
        n = (r.get("contact_name") or r.get("from_name") or "").strip()
        if n:
            return n
    return contact


def _retire_earlier_drafts(ctx, contact: str) -> int:
    """Skip this customer's still-pending voice drafts from the last hour —
    the new one supersedes them. Best-effort: a failed read never blocks the
    draft being written."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    try:
        rows = ((ctx.client or db.db()).table("email_log").select("id")
                .eq("tenant_id", ctx.tenant_id).eq("kind", "owner_message")
                .eq("status", "pending").ilike("from_address", contact)
                .gte("created_at", since).execute().data or [])
        for r in rows:
            db.set_email_log_status(r["id"], "skipped", ctx.tenant_id, client=ctx.client)
        return len(rows)
    except Exception:
        log.warning("voice draft: retiring earlier drafts failed (tenant %s)", ctx.tenant_id,
                    exc_info=True)
        return 0


async def draft(ctx, args: dict, *, member: dict | None,
                spoken_ms: int = 0,
                known_addresses: set[str] | None = None) -> tuple[dict, dict | None]:
    """Run one draft_email call. Returns (tool_result for the model, card for
    the voice screen or None). Never raises: a refusal is an honest payload
    the Concierge can say out loud. `known_addresses`: the addresses this
    turn's lookups returned (addresses_in) — a NEW email may only go to one
    of those or to a contact already in the CRM."""
    from app import concierge_actions, dashboard, pipeline

    if not concierge_actions.may_do(member, "draft_reply"):
        return {"error": "not_allowed",
                "message": "This person can't approve replies on this account — "
                           "say so; nothing was drafted."}, None
    contact = str((args or {}).get("contact") or "").strip().lower()[:254]
    mode = "new" if str((args or {}).get("mode") or "").strip() == "new" else "reply"
    instruction = str((args or {}).get("instruction") or "").strip()[:1500]
    if "@" not in contact:
        return {"error": "need_email",
                "message": "I need the customer's email address — look them up "
                           "first, then call draft_email again."}, None
    rows = db.get_conversation(ctx.tenant_id, contact, client=ctx.client) or []
    name = _name_for(rows, contact)
    booking_hold = ""
    reworded = ""   # the booking-state reason a draft was silently corrected for

    if mode == "reply":
        latest = rows[-1] if rows else None
        if latest is None:
            return {"error": "no_conversation",
                    "message": f"There's no conversation with {name} to reply to. "
                               "Offer to write them a new email instead."}, None
        waiting = ((latest.get("kind") or "inbound") == "inbound"
                   and latest.get("status") != "sent")
        if not waiting:
            return {"error": "nothing_waiting",
                    "message": f"{name}'s last message has already been answered — "
                               "nothing is waiting for a reply. Say so, and offer "
                               "to write them a new email instead."}, None
        try:
            dashboard._require_their_mail(ctx, latest)
        except Exception:
            return {"error": "not_theirs",
                    "message": "That conversation is in a mailbox this person "
                               "can't see — nothing was drafted."}, None
        log_id = latest["id"]
        text = await pipeline.redraft_email_log(ctx.tenant_id, log_id, client=ctx.client,
                                                owner_instruction=instruction)
        text = (text or "").strip()
        from app.concierge_booking_claims import check
        booking_hold, _ = check(ctx.tenant_id, contact, text, ctx.client)
        if booking_hold:
            original_reason = booking_hold
            from app.concierge_booking_claims import classify_hold
            if classify_hold(booking_hold) in ("firm_on_proposed", "past_date"):
                text = ""   # confirm (or move) first - a reworded draft is the wrong answer
            else:
                text = (await pipeline.redraft_email_log(
                    ctx.tenant_id, log_id, client=ctx.client,
                    owner_instruction=instruction + "\nBooking wording correction: " + booking_hold) or "").strip()
            booking_hold, _ = check(ctx.tenant_id, contact, text, ctx.client)
            if not text:
                booking_hold = original_reason
            elif not booking_hold:
                reworded = original_reason
            if booking_hold:
                _log_hold(ctx, contact, latest.get("subject") or "", booking_hold, "reply", instruction)
                db.update_email_log_draft(log_id, "", ctx.tenant_id, client=ctx.client)
                db.set_email_log_category(log_id, ctx.tenant_id, "needs_reply",
                                          reason=booking_hold, client=ctx.client)
        subject = latest.get("subject") or ""
        mailbox_id = latest.get("mailbox_id")
        if latest.get("status") != "pending":
            db.set_email_log_status(log_id, "pending", ctx.tenant_id, client=ctx.client)
    else:
        if not instruction:
            return {"error": "need_instruction",
                    "message": "Ask the owner what the email should say."}, None
        if not _known_recipient(ctx, contact, known_addresses):
            db.log_agent_event(ctx.tenant_id, "concierge_refused",
                               f"Refused a voice email to {contact}: not a customer "
                               "and not found by a lookup this turn",
                               contact=contact,
                               detail={"via": "concierge_voice", "mode": "new",
                                       "reason": "unknown_recipient",
                                       "instruction": instruction[:500]},
                               client=ctx.client)
            return {"error": "unknown_recipient",
                    "message": "That address isn't one of the business's customers and "
                               "no lookup found it — nothing was drafted. Look the "
                               "customer up first; a new email only goes to someone "
                               "on file."}, None
        from app import agent, usage
        cfg = db.effective_config(ctx.tenant_id, None, client=ctx.client)
        facts = (f"The owner asked for this email, in their words: {instruction}\n"
                 f"The customer: {name} <{contact}>")
        result = await agent.compose_outbound(
            cfg, "An email the business owner asked for — say what they asked, "
                 "nothing more.", facts, tenant_id=ctx.tenant_id,
            log_operation="concierge_email", log_module="concierge")
        text = (result.text or "").strip()
        from app.concierge_booking_claims import check as _check, classify_hold, repair
        first = text
        pre, _ = _check(ctx.tenant_id, contact, text, ctx.client)
        if classify_hold(pre) in ("firm_on_proposed", "past_date"):
            booking_hold = pre   # confirm (or move) first - not a reworded email
        else:
            text, booking_hold = await repair(ctx.tenant_id, contact, text, cfg=cfg, client=ctx.client)
        if not booking_hold and text != first:
            reworded = "the diary does not back the booking wording the owner asked for"
        subject = (str((args or {}).get("subject") or "").strip()[:150]
                   or "A quick note")
        mailbox_id = None
        # One email to one customer waiting at a time: an earlier voice draft
        # to them that's still pending is replaced, not joined (Kris
        # 2026-09-23: "send it" re-drafted, leaving three copies to Daniel).
        if booking_hold:
            # Nothing to queue: an empty pending row would sit on Today as a
            # phantom email. The owner hears the reason and the way forward;
            # the hold is on the agent log.
            _log_hold(ctx, contact, subject, booking_hold, "new", instruction)
            usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                                   operation="concierge_email", module="concierge")
            card = {"log_id": None, "contact": contact, "name": name, "subject": subject,
                    "text": "", "mode": mode, "status": "empty", "reason": booking_hold,
                    "href": "/app/conversations/view?contact=" + quote(contact)}
            return _hold_result(name, booking_hold, contact, instruction,
                                _can_confirm(ctx)), card
        _retire_earlier_drafts(ctx, contact)
        log_id, _ = db.insert_email_log(
            ctx.tenant_id, contact, subject, f"[owner message] {instruction[:300]}",
            text,
            status="pending", kind="owner_message",
            thread_id=(rows[-1].get("thread_id") if rows else None),
            contact_name=None if name == contact else name,
            category="draft_ready",
            category_reason="You asked the Concierge for this email")
        usage.record_anthropic(ctx.tenant_id, result.model, result.usage,
                               operation="concierge_email", email_log_id=log_id,
                               module="concierge")

    if spoken_ms:   # the owner's asking time, for src/app/time_saved.py (best-effort)
        db.set_email_log_spoken_seconds(log_id, ctx.tenant_id, round(spoken_ms / 1000),
                                        client=ctx.client)
    href = "/app/conversations/view?contact=" + quote(contact)
    card = {"log_id": log_id, "contact": contact, "name": name, "subject": subject,
            "text": text, "mode": mode, "status": "pending", "href": href}
    if booking_hold:
        card.update(status="empty", text="", reason=booking_hold)
        return _hold_result(name, booking_hold, contact, instruction, _can_confirm(ctx)), card
    # One config read serves the full-auto check below and the "no sign-off set"
    # badge; an empty draft needs neither, so it keeps skipping the read. Best-effort:
    # the draft is already stored, so a failed read must not lose its agent-log event or
    # the live status update. Unreadable = no badge and no full-auto send (the draft
    # simply waits for the owner, who is right there).
    cfg, cfg_read = {}, False
    if text:
        try:
            cfg, cfg_read = db.effective_config(ctx.tenant_id, mailbox_id, client=ctx.client), True
        except Exception:
            log.warning("voice draft: config read failed (tenant %s)", ctx.tenant_id,
                        exc_info=True)
    detail = {"via": "concierge_voice", "mode": mode,
              # the owner's words, as the model passed them (M21)
              "instruction": instruction[:500]}
    if cfg_read and not sign_off.normalise(cfg.get("sign_off")):
        detail["sign_off_missing"] = True    # the agent log says so (never blocks)
    db.log_agent_event(ctx.tenant_id, "drafted",
                       f"Drafted {'a reply' if mode == 'reply' else 'an email'} to "
                       f"{contact} for the owner (voice)",
                       contact=contact, subject=subject, email_log_id=log_id,
                       detail=detail, client=ctx.client)
    events.publish(ctx.tenant_id, {"type": "status_changed", "log_id": log_id,
                                   "contact": contact, "status": "pending"})
    if not text:
        card["status"] = "empty"
        return {"ok": False, "status": "empty",
                "message": "The Drafting agent couldn't write this one — it's in "
                           f"{name}'s thread to write by hand. Say so."}, card

    # Full-auto: the business sends without review, and the owner asked for
    # this one — a REPLY goes now, through the thumbs-up's own send path. A
    # NEW email always waits for the owner's yes (M21): the only Concierge
    # write that could otherwise go unconfirmed to an address of the model's
    # choosing.
    if mode == "reply" and cfg.get("auto_send"):
        try:
            outcome = await dashboard.send_approved(ctx, log_id, text, auto=True, concierge=True)
        except Exception:
            log.warning("voice full-auto send failed (tenant %s, log %s)",
                        ctx.tenant_id, log_id, exc_info=True)
            outcome = "failed"
        card["status"] = "sent" if outcome == "sent" else "failed"
        card["auto"] = True
        if card["status"] == "sent":
            return {"ok": True, "status": "sent",
                    "message": f"Full-auto is on, so it has been SENT to {name}. "
                               "Tell the owner in one sentence; the email is on "
                               "their screen."}, card
        return {"ok": False, "status": "failed",
                "message": "Full-auto tried to send it and the send failed — it's "
                           f"waiting in {name}'s thread. Say so."}, card

    if reworded:
        # 2026-09-30 (Ann Fowler, from llm_calls): the draft was quietly
        # corrected from "booked in" to "still confirming", the result said
        # nothing of it, and the Concierge invented a block ("confirm it in the
        # diary first") while the corrected draft sat on screen — the owner's
        # yes then sent it. Say what happened so speech matches the card.
        return {"ok": True, "status": "pending",
                "message": "The draft is ALREADY on the owner's screen, waiting for approval "
                           "— don't call show_draft for it. NOTE: the owner asked for wording "
                           f"the diary doesn't support ({reworded}), so the Drafting agent "
                           "reworded it: it now says the booking is not yet confirmed. Nothing "
                           "is blocked. Say that plainly in one sentence, then ask ONLY 'Shall "
                           "I send it?' — do not offer to confirm anything or ask any other "
                           "question, because the owner's yes sends this draft.",
                "draft_preview": text[:600]}, card
    return {"ok": True, "status": "pending",
            # "already … no show_draft": 24 Sep, the model called show_draft on
            # the draft it had just made — a 2.6 s round for nothing.
            "message": f"The draft is ALREADY on the owner's screen, waiting for approval "
                       "— don't call show_draft for it. Say in one sentence what it says, "
                       "then ask whether to send it. Don't read it all out.",
            "draft_preview": text[:600]}, card


# ── Bringing back a draft that's already written (Kris 2026-09-24) ─────────
# "I'm trying to tell it to send the email to Laura Finch. It's already
# written. It comes back asking if I want to send it, I say Yes, it then goes
# and searches the inbox again … then tells me it cannot send from here."
# A spoken yes sends the draft card ON SCREEN; once later turns had cleared
# the stage, the Concierge could see the pending draft in the inbox but had no
# way to put it back in front of the owner, so a yes had nothing to send.
# show_draft puts the existing draft back on the screen, unchanged — the
# owner's yes (or 👍) then sends it through the thread's own path. It never
# rewrites and never sends by itself.

SHOW_TOOL_NAME = "show_draft"
SHOW_HINT = "Bringing up the draft…"
SHOW_LABEL = "Brought up the draft"

SHOW_TOOL = {
    "name": SHOW_TOOL_NAME,
    "description": (
        "Put an email that is ALREADY drafted and waiting for approval back on "
        "the owner's screen, unchanged, so their spoken yes sends it. Call it "
        "whenever the owner asks to send, see or check a draft that already "
        "exists ('send the email to Laura', 'is there a draft for Laura?') — "
        "instead of drafting again and instead of telling them to approve it "
        "somewhere else. `contact` must be the customer's email address from a "
        "tool result in this conversation. If nothing is waiting, the result "
        "says so; then offer to write one with draft_email."),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "description": "the customer's email address"},
        },
        "required": ["contact"],
    },
}


async def show(ctx, args: dict, *, member: dict | None) -> tuple[dict, dict | None]:
    """Run one show_draft call: the newest pending draft to this customer, as
    the same card draft_email makes. Never raises."""
    from app import concierge_actions, dashboard

    if not concierge_actions.may_do(member, "draft_reply"):
        return {"error": "not_allowed",
                "message": "This person can't approve replies on this account — "
                           "say so."}, None
    contact = str((args or {}).get("contact") or "").strip().lower()[:254]
    if "@" not in contact:
        return {"error": "need_email",
                "message": "I need the customer's email address — look them up "
                           "first, then call show_draft again."}, None
    rows = db.get_conversation(ctx.tenant_id, contact, client=ctx.client) or []
    name = _name_for(rows, contact)
    if name == contact:
        try:
            name = (db.crm_contact_by_email(ctx.tenant_id, contact, client=ctx.client)
                    or {}).get("name") or contact
        except Exception:
            pass
    held = _recent_hold(ctx, contact)
    if held:
        # The email this conversation just tried to make was held: a follow-up
        # "send it" must not surface an older, unrelated draft (2026-09-30: an
        # old, unrelated reply came up and was refused).
        return _hold_result(name, held, contact, "", _can_confirm(ctx)), {
            "log_id": None, "contact": contact, "name": name, "subject": "", "text": "",
            "mode": "new", "status": "empty", "reason": held,
            "href": "/app/conversations/view?contact=" + quote(contact)}
    waiting = [r for r in rows
               if r.get("status") == "pending" and (r.get("draft_reply") or "").strip()]
    if not waiting:
        return {"error": "no_draft",
                "message": f"There's no draft waiting for {name}. Say so, and offer "
                           "to write one."}, None
    row = waiting[-1]
    try:
        dashboard._require_their_log(ctx, row["id"])
    except Exception:
        return {"error": "not_theirs",
                "message": "That draft is in a mailbox this person can't see."}, None
    kind = row.get("kind") or "inbound"
    card = {"log_id": row["id"], "contact": contact, "name": name,
            "subject": row.get("subject") or "", "text": row["draft_reply"].strip(),
            "mode": "new" if kind == "owner_message" else "reply", "status": "pending",
            "href": "/app/conversations/view?contact=" + quote(contact)}
    from app.concierge_booking_claims import check
    reason, _ = check(ctx.tenant_id, contact, card["text"], ctx.client)
    reworded = False
    if reason:
        # A held draft is never the thing to send. Give it the same single
        # corrective rewrite a fresh draft gets (only the booking-state claims
        # change); the owner still sees the result before their yes sends it.
        from app.concierge_booking_claims import repair
        fixed, reason = await repair(ctx.tenant_id, contact, card["text"], client=ctx.client)
        if not reason and fixed.strip() and fixed.strip() != card["text"]:
            db.update_email_log_draft(row["id"], fixed.strip(), ctx.tenant_id, client=ctx.client)
            card["text"] = fixed.strip()
            reworded = True
        elif not reason:
            reason = "Could not correct the booking wording in this draft."
    if reason:
        card.update(status="empty", text="", reason=reason)
        return _hold_result(name, reason, contact, "", _can_confirm(ctx)), card
    if reworded:
        # A reworded draft is NOT staged for the owner's bare yes: the
        # client sends whatever pending card is on screen on any yes, whatever
        # the Concierge just asked (2026-09-30: a yes to "confirm it in the
        # diary?" sent the email). The owner hears the new wording first, and
        # a second show_draft then stages it.
        card["status"] = "left"
        return {"ok": True, "status": "reworded",
                "message": f"The draft to {name} claimed a booking the diary doesn't back, so "
                           "it was reworded to say the booking is pencilled in and asks them "
                           "to confirm. It is saved in their thread but NOT on the send "
                           "screen, and nothing has been sent. Say in one sentence what it "
                           "now says, then ask whether to send that pencilled-in email. Only "
                           "if the owner says yes to THAT, call show_draft again.",
                "draft_preview": card["text"][:600]}, card
    return {"ok": True, "status": "pending",
            "message": f"The draft to {name} is back on the owner's screen, "
                       "unchanged. Say in one sentence what it says, then ask "
                       "'Shall I send it?' — their yes sends it.",
            "draft_preview": card["text"][:600]}, card
