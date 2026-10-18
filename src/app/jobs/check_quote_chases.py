"""Daily quote chase (docs/modules/module-quotes.md Phase 2 — the conversion lifter).

The research's clearest finding: the median SMB converts ~38% of quotes and a
3/7-day follow-up cadence measurably lifts it — yet almost nobody chases. So
BLAM8 does: for every quote still out for an answer, this job

  * drafts a follow-up at day 3 and day 7 after sending, plus a final one
    3 days before expiry — capped at 3 touches total (reminders_sent), in the
    tenant's learned voice, through the normal draft/approval queue (auto-send
    tenants send immediately, same as every other composed nudge);
  * STOPS the moment the customer engages: accepted/declined quotes never
    reach this job (status filter), an inbound reply from the customer since
    the last touch stands the chase down, and expiry ends it;
  * flips sent → expired on the day (guarded — an acceptance racing the
    calendar wins) with an owner push;
  * fires the "they keep looking" escalation exactly once: viewed twice or
    more, still unaccepted at day 7 — that's a phone call, not another email
    (zero LLM, push only).

Chase touches never feed draft-suppression (record_draft_generated is only
called from pipeline.draft_for_email) — chasing money is a different behaviour
to ignoring a conversation (the payment-reminder doctrine).

reminders_sent increments at DRAFT time, not send time: the counter's job is
cadence dedupe (a redeploy or double-run must not double-draft), mirroring
ready_reminders' flip-at-draft ordering.

Registered in src/app/main.py at 09:00 UTC. Manual run: python -m app.jobs.check_quote_chases
"""
import asyncio
import logging
from datetime import datetime, timezone

from app import agent, db, disclosure, mailbox, push, usage

log = logging.getLogger(__name__)

CHASE_PURPOSE = (
    "A brief, friendly follow-up on a quote we sent that hasn't been answered. "
    "Check they received it, invite questions, and offer to adjust anything. "
    "If the facts say it expires soon, mention that gently — never pressure. "
    "Do NOT restate every line item, invent discounts, or change any price. "
    "Keep it short and human.")

MAX_CHASES = 3


def _days_between(earlier_iso: str, today) -> int:
    try:
        return (today - datetime.strptime(str(earlier_iso)[:10], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return 0


def _touches_due(quote: dict, today) -> int:
    """How many chase touchpoints have passed for this quote: day 3, day 7,
    and 3-days-before-expiry. Compared against reminders_sent, so each touch
    fires once and a missed day catches up by exactly one (never a burst)."""
    days_out = _days_between(quote.get("sent_at"), today)
    due = (days_out >= 3) + (days_out >= 7)
    valid = str(quote.get("valid_until") or "")[:10]
    if valid and 0 <= _days_between(today.isoformat(), datetime.strptime(
            valid, "%Y-%m-%d").date()) <= 3:
        due += 1
    return min(due, MAX_CHASES)


def _facts(quote: dict, link: str, today) -> str:
    parts = [f"We sent quote {quote.get('quote_ref')} "
             f"(£{float(quote.get('total') or 0):,.2f})"]
    if quote.get("title"):
        parts[0] += f" for: {quote['title']}"
    parts[0] += f" {_days_between(quote.get('sent_at'), today)} days ago and "
    parts[0] += "haven't heard back."
    if quote.get("valid_until"):
        parts.append(f"It's valid until {str(quote['valid_until'])[:10]}.")
    if quote.get("viewed_at"):
        parts.append("They have opened it, but not accepted.")
    else:
        parts.append("It doesn't look like they've opened it yet.")
    parts.append(f"They can view and accept it at {link} (include this link).")
    return " ".join(parts)


async def _chase_one(tenant_id: str, cfg: dict, auto: bool, quote: dict,
                     today) -> str:
    """Compose + queue (and maybe send) one chase. Returns
    'sent' | 'drafted' | 'skipped'."""
    from app.quote_send import quote_url
    to = (quote.get("customer_email") or "").strip()
    if not to:
        return "skipped"          # nobody to chase — the owner has the link
    since = quote.get("last_reminder_at") or quote.get("sent_at") or ""
    if db.has_inbound_from_since(tenant_id, to, since):
        return "skipped"          # they wrote back — the conversation is live
    ref = quote.get("quote_ref") or "quote"
    link = quote_url(quote["id"])
    subject = f"Your quote {ref}"
    result = await agent.compose_outbound(cfg, CHASE_PURPOSE,
                                          _facts(quote, link, today),
                                          tenant_id=tenant_id,
                                          log_operation="reminder")
    body = result.text
    if link not in body:
        # The link is the quote — a chase without it is a dead end. Composed
        # text usually carries it (the facts say to); this is the guarantee.
        body += f"\n\nYou can view and accept the quote here:\n{link}"
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[quote chase] {ref}", body,
        status="pending", kind="quote_reminder")
    # Count the touch NOW so a re-run can't double-draft (cadence dedupe).
    db.record_quote_reminder(quote["id"], tenant_id)
    db.log_agent_event(
        tenant_id, "quote_reminder",
        f"Drafted a follow-up on {ref} ({_days_between(quote.get('sent_at'), today)} "
        f"days unanswered)",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, body))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the quote follow-up for {ref} to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


def _expire(quote: dict) -> bool:
    tenant_id = quote["tenant_id"]
    if not db.expire_quote(quote["id"], tenant_id):
        return False
    ref = quote.get("quote_ref") or "quote"
    who = quote.get("customer_name") or quote.get("customer_email") or "customer"
    total = float(quote.get("total") or 0)
    db.log_agent_event(tenant_id, "quote_expired",
                       f"Quote {ref} (£{total:,.2f}) to {who} expired unanswered",
                       contact=quote.get("customer_email"))
    push.notify(tenant_id, title=f"Quote {ref} expired unanswered",
                body=f"£{total:,.2f} to {who} — re-quote or let it go?",
                url=f"/app/quotes/{quote['id']}",
                tag=f"quote-expired-{quote['id']}", kind="quote_expired",
                contact=quote.get("customer_email"),
                contact_name=quote.get("customer_name"))
    return True


def _escalate_keeps_looking(quote: dict, today) -> bool:
    """Viewed ≥2×, unaccepted at day 7 — they're interested but stuck; that's a
    phone call. ==7 so the daily run fires it exactly once, no dedupe column."""
    if not (int(quote.get("view_count") or 0) >= 2
            and _days_between(quote.get("sent_at"), today) == 7):
        return False
    tenant_id = quote["tenant_id"]
    ref = quote.get("quote_ref") or "quote"
    who = quote.get("customer_name") or quote.get("customer_email") or "The customer"
    push.notify(tenant_id,
                title=f"{who} keeps looking at quote {ref}",
                body=f"Opened {quote.get('view_count')}× but not accepted — "
                     "worth a call?",
                url=f"/app/quotes/{quote['id']}",
                tag=f"quote-looking-{quote['id']}", kind="quote_keeps_looking",
                contact=quote.get("customer_email"),
                contact_name=quote.get("customer_name"))
    db.log_agent_event(tenant_id, "quote_interest",
                       f"{who} has opened {ref} {quote.get('view_count')} times "
                       "without accepting — a call may close it",
                       contact=quote.get("customer_email"))
    return True


async def run() -> dict:
    today = datetime.now(timezone.utc).date()
    rows = db.list_open_sent_quotes()
    drafted = sent = expired = escalated = 0
    cfg_cache: dict = {}
    for quote in rows:
        try:
            tenant_id = quote["tenant_id"]
            valid = str(quote.get("valid_until") or "")[:10]
            if valid and valid < today.isoformat():
                expired += _expire(quote)
                continue
            escalated += _escalate_keeps_looking(quote, today)
            already = int(quote.get("reminders_sent") or 0)
            if already >= MAX_CHASES or _touches_due(quote, today) <= already:
                continue
            if tenant_id not in cfg_cache:
                cfg_cache[tenant_id] = db.effective_config(tenant_id, None)
            cfg = cfg_cache[tenant_id]
            outcome = await _chase_one(tenant_id, cfg,
                                       bool(cfg.get("auto_send")), quote, today)
            sent += outcome == "sent"
            drafted += outcome == "drafted"
        except Exception:
            # One bad quote must not stop the run.
            log.warning("quote chase failed for %s", quote.get("id"), exc_info=True)
    return {"checked": len(rows), "drafted": drafted, "sent": sent,
            "expired": expired, "escalated": escalated}


if __name__ == "__main__":
    print(asyncio.run(run()))
