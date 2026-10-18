"""Customer wording must not promote a diary record to a stronger state.

This is a Concierge boundary, not a change to the inbound booking policy.
Read all states (including cancelled) for the exact tenant and recipient.
Unverifiable wording is held for the owner, never treated as a failed rule.
"""
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import db
from app.dates import pretty_date

_REF = re.compile(r"\bBK-\d+\b", re.I)
_CLAIM = re.compile(
    r"\b(?:booked(?:\s+in)?|confirmed|scheduled|secured|guaranteed|"
    r"reserved|held|holding|pencilled\s+in|penciled\s+in|offered|"
    r"all set|will be with you|will attend|is going ahead|will go ahead)\b", re.I)
_FIRM = re.compile(r"booked(?:\s+in)?|confirmed|scheduled|secured|guaranteed|all set|"
                   r"will be with you|will attend|is going ahead|will go ahead", re.I)
_NEGATED = re.compile(
    r"(?:\bnot(?:\s+yet)?(?:\s+been)?|\bnever(?:\s+been)?|\bno longer|\bhasn't(?:\s+been)?|"
    r"\bhaven't(?:\s+been)?|\bisn't|\baren't|\bwasn't|\bweren't|"
    r"\bto be|\bonce|\bif|\bwhen)\s*$", re.I)
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan\w*|Feb\w*|Mar\w*|Apr\w*|May|Jun\w*|"
    r"Jul\w*|Aug\w*|Sep\w*|Oct\w*|Nov\w*|Dec\w*)|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|today|tomorrow)\b", re.I)
_LIVE = frozenset(('confirmed', 'proposed', 'held', 'offered'))
_PAST_STATE = re.compile(r"\b(?:was|were|had been)\s+$", re.I)
_HISTORICAL_STATE = frozenset(('booked', 'confirmed', 'scheduled', 'reserved', 'held', 'offered'))
_FUTURE = re.compile(r"\b(?:tomorrow|next|will|going to)\b", re.I)


def _claims(text):
    text = text.replace('’', "'").replace('**', '').replace('_', '')
    return text, [m for m in _CLAIM.finditer(text)
                  if not _NEGATED.search(text[max(0, m.start() - 35):m.start()])]


def read_bookings(tenant_id, contact, client=None):
    rows = ((client or db.db()).table("bookings")
            .select("booking_ref,booking_date,status,summary")
            .eq("tenant_id", tenant_id).eq("contact", contact)
            .limit(1000).execute().data)
    if rows is None or len(rows) >= 1000:
        raise ValueError("Incomplete diary result")
    return rows


def _business_today(tenant_id, client):
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    # A missing/invalid timezone cannot establish which records are live.
    return datetime.now(ZoneInfo(cfg['timezone'])).date()


def violation(text, bookings, *, business_today=None):
    """Return an owner-facing reason, or empty. Unknown/ambiguous is a hold.

    Associate claims with references in their sentence, then explicit dates,
    then the last named record. Several records in one claim must all back it.
    Nouns are never classified: the record's own reference identifies it.
    """
    text, claims = _claims(text)
    if not claims:
        return ""
    current = None
    unresolved = ""
    for sentence in re.split(r"[.!?;\n]+", text):
        _, local = _claims(sentence)
        refs = {r.upper() for r in _REF.findall(sentence)}
        if refs:
            current = [b for b in bookings if str(b.get('booking_ref', '')).upper() in refs]
            unresolved = ("Could not verify the booking reference in this draft. Please check the diary."
                          if refs - {str(b.get('booking_ref', '')).upper() for b in current} else "")
        elif _DATE.search(sentence):
            matched = []
            normal = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", sentence, flags=re.I)
            numeric_date = any(m.group()[0].isdigit() for m in _DATE.finditer(normal))
            today = datetime.now(ZoneInfo('Europe/London')).date()
            for b in bookings:
                try:
                    day = date.fromisoformat(str(b.get('booking_date'))[:10])
                except ValueError:
                    continue
                variants = ((day.isoformat(), f"{day.day} {day:%B}", f"{day.day} {day:%b}",
                             f"{day.day}/{day.month}", day.strftime('%d/%m')) if numeric_date else
                            (day.strftime('%A'), 'today' if day == today else '',
                             'tomorrow' if day == today + timedelta(days=1) else ''))
                if any(v and re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", normal, re.I)
                       for v in variants):
                    matched.append(b)
            # A cancelled record on the same day as a live one is the superseded
            # duplicate, not what the customer holds: the live record governs.
            # (A reference names its record exactly, so that path is untouched.)
            live = [b for b in matched if str(b.get('status') or '').lower() in _LIVE]
            current = live or matched
            unresolved = ("Could not verify the booking date in this draft. Please check the diary."
                          if not current else "")
        if not local:
            continue
        if unresolved:
            return unresolved
        candidates = current
        if current is None:
            try:
                today = business_today() if callable(business_today) else business_today
                if not isinstance(today, date):
                    raise ValueError('Business date unavailable')
            except Exception:
                return "Could not verify today's date in the business timezone. Please check the diary."
            candidates = []
            for b in bookings:
                try:
                    day = date.fromisoformat(str(b.get('booking_date'))[:10])
                except ValueError:
                    continue
                if str(b.get('status') or '').lower() in _LIVE and day >= today:
                    candidates.append(b)
            if len({str(b['status']).lower() for b in candidates}) > 1:
                return "Could not verify which live booking this draft means: their states differ. Name the booking reference."
        if not candidates:
            return "Could not verify the booking state in this draft. Please check the diary."
        for b in candidates:
            status = str(b.get('status') or '').lower()
            label = b.get('booking_ref') or b.get('booking_date') or 'The diary record'
            # Only an explicitly anchored, completed record supports a narrow
            # historical state statement. Never exempt a whole sentence because
            # it mentions completion: every other booking predicate still counts.
            if status == 'completed' and current is not None and not _FUTURE.search(sentence) and all(
                    m.group().lower() in _HISTORICAL_STATE
                    and _PAST_STATE.search(sentence[:m.start()]) for m in local):
                continue
            remedy = f"Confirm {label} in the diary first, or change the wording."
            if status not in _LIVE:
                return f"{label}: status is {status or 'not verified'}; this draft promises a booking. {remedy}"
            if status != 'confirmed' and any(_FIRM.fullmatch(m.group()) for m in local):
                gone = _past_day(b, business_today)
                if gone:
                    return (f"{label} was on {pretty_date(gone)}, which has passed. "
                            "Move it to a new date first; nothing can promise it.")
                if status == 'offered':
                    # Slots were offered and the customer has not chosen yet.
                    return (f"{label}: status is offered, not confirmed. {remedy} "
                            "For a hold, say it is pencilled in and ask the customer to confirm.")
                # A held/proposed time waits on the BUSINESS, not the customer:
                # confirming it is what lets the email say it is booked.
                return (f"{label}: status is {status}, not confirmed. {remedy} "
                        "The time is held for them and waits on you, so confirm it "
                        "and the email can say it is booked.")
    return ""


def _past_day(booking, business_today):
    """The booking's date when it is before today, else None (unknown is None)."""
    try:
        today = business_today() if callable(business_today) else business_today
        if not isinstance(today, date):
            return None
        day = date.fromisoformat(str(booking.get('booking_date'))[:10])
    except Exception:
        return None
    return day if day < today else None


HOLD_CODES = ("firm_on_proposed", "firm_on_offered", "past_date", "not_live", "unverified")


def classify_hold(reason):
    """The structured code for an owner-facing hold reason ("" for no hold).

    The codes drive what the Concierge offers: confirm first (a held time
    waits on the business), move first (a past date), pick-a-time wording
    (offered slots), or a plain hold."""
    reason = str(reason or "")
    if not reason:
        return ""
    if "which has passed" in reason:
        return "past_date"
    if "status is offered, not confirmed" in reason:
        return "firm_on_offered"
    if re.search(r"status is (?:proposed|held), not confirmed", reason):
        return "firm_on_proposed"
    if "this draft promises a booking" in reason:
        return "not_live"
    return "unverified"


def hold_ref(reason):
    """The booking reference a hold reason is about, if it names one."""
    m = _REF.match(str(reason or "").strip())
    return m.group().upper() if m else None


def check(tenant_id, contact, text, client=None):
    if not _claims(text)[1]:
        return "", []
    try:
        rows = read_bookings(tenant_id, contact, client)
    except Exception:
        return "Could not verify the booking state because the diary lookup failed. Please check the diary.", []
    return violation(text, rows, business_today=lambda: _business_today(tenant_id, client)), rows


async def repair(tenant_id, contact, text, *, cfg=None, client=None):
    """One rewrite with the executor's reason; a second failure stays held."""
    reason, rows = check(tenant_id, contact, text, client)
    if not reason:
        return text, ""
    from app import agent, usage
    try:
        cfg = cfg if cfg is not None else db.effective_config(tenant_id, None, client=client)
        result = await agent.compose_outbound(
            cfg, "Correct only the booking-state claims in this customer draft. "
                 "Preserve the other facts and the business's own nouns. "
                 "Do not invent a state or remove the booking reference/date to evade the check. "
                 "Never mention cancelled or superseded bookings, or that the diary holds several records.",
            json.dumps({"draft": text, "correction": reason,
                        "diary_records": [r for r in rows
                                          if str(r.get('status') or '').lower() in _LIVE]}, default=str),
            tenant_id=tenant_id, log_operation="concierge_email", log_module="concierge")
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="concierge_email", module="concierge")
        fixed = (result.text or '').strip()
        if fixed:
            reason, _ = check(tenant_id, contact, fixed, client)
            return fixed, reason
    except Exception:
        pass
    return text, reason


async def repair_action(action, tenant_id):
    """Both typed and voice answers, including each item in a bulk proposal.

    A firm claim on a HELD booking is not reworded: the time waits on the
    business, so the draft becomes a confirm-first card (confirm, then this
    email). A past date is held as a move-first reason. Anything else gets the
    one corrective rewrite."""
    if not action or not tenant_id:
        return False
    changed = False
    items = action.get('items', []) if action.get('kind') == 'bulk' else [action]
    for item in items:
        if item.get('kind') != 'draft_reply':
            continue
        original = str(item.get('draft_text') or '')
        contact = str(item.get('contact') or '').strip().lower()
        reason, _ = check(tenant_id, contact, original)
        code = classify_hold(reason)
        ref = hold_ref(reason)
        if code == 'firm_on_proposed' and ref and _can_confirm(tenant_id):
            item.update(kind='confirm_booking', booking_id=ref, contact=contact,
                        draft_text=original, hold_reason=reason, hold_code=code)
            changed = True
            continue
        if code == 'past_date':
            item['draft_text'] = original
            item['hold_reason'] = reason
            item['hold_code'] = code
            changed = True
            continue
        text, reason = await repair(tenant_id, contact, original)
        item['draft_text'] = text
        item['hold_reason'] = reason
        item['hold_code'] = classify_hold(reason)
        changed |= text != original or bool(reason)
    return changed


def _can_confirm(tenant_id):
    """Does this account have a diary to confirm in? (No diary, no confirm card.)"""
    try:
        return bool({"scheduling", "tms"} & set(db.active_modules(tenant_id)))
    except Exception:
        return False


def repair_reply(action):
    """The Concierge's words once a draft card has been through repair_action.

    A held time: say it is only held and that the card confirms it, then
    emails. A past date: offer the move. Anything else held names the reason
    and both ways forward, so a yes is never a dead end. The Concierge can
    confirm a booking itself only through the confirm card."""
    items = action.get('items', []) if (action or {}).get('kind') == 'bulk' else [action or {}]
    for i in items:
        if i.get('kind') == 'confirm_booking' and i.get('hold_code') == 'firm_on_proposed':
            return (f"{i.get('booking_id')} is only held for them, not confirmed, so that email "
                    "can't say it's booked yet. Shall I confirm it and queue the email? The "
                    "card has both - if the confirm fails, no email is queued.")
    held = [i for i in items if i.get('hold_reason')]
    if not held:
        return "Please review the customer draft card and any hold reason before approving it."
    reason, code = str(held[0]['hold_reason']), held[0].get('hold_code')
    if code == 'past_date':
        return (f"I've held that draft card and sent nothing: {reason} Shall I move it to a new "
                "date first? Tell me the day and I'll set up the move.")
    return (f"I've held that draft card and sent nothing: {reason} I can redraft it as pencilled in, "
            "asking them to confirm, or you can confirm the booking in the diary first and I'll "
            "send it after. Which would you like?")
