"""Proactive invoice chasing — the escalation ladder behind the 08:45 job.

The research finding this answers (competitive sweep, 2026-09-01): every app in
the sector has payment "reminders" — one template, fired blind — and none of
them CHASES: tone that escalates with lateness, silence the moment the customer
writes back, and a human yes before anything leaves. BLAM8's whole premise is
proactive, so the overdue milestones don't notify the owner to go ask for a
reminder; they put the drafted chase itself in front of them (or send it, for
auto-send tenants) — the same doctrine as src/app/jobs/check_quote_chases.py.

Composition is one compose_outbound call in the tenant's learned voice; the
milestone picks the register. Statutory late-payment interest (Late Payment of
Commercial Debts (Interest) Act 1998) appears only at the 30-day register, only
as one gentle sentence, and never with an amount — the rate moves with the Bank
of England base rate, and a wrong figure in a legal-adjacent sentence is worse
than none. The operator edits before anything sends.

reminders_sent stays a count of SENDS (the concierge's "#n" and the 3-strike
escalation read it) — cadence dedupe lives in overdue_notified_days plus the
deterministic email_log source_message_id, not in this counter.
"""
from __future__ import annotations

import logging

from app import agent, db, disclosure, usage

log = logging.getLogger(__name__)

# The register ladder. Keys are the overdue milestones (days) the 08:45 job
# fires on; 0 is the "chase now" register for an operator chasing early.
PURPOSES = {
    0: ("A friendly payment reminder for an invoice that hasn't been paid yet. "
        "Assume good faith — it has probably just slipped their mind. Restate "
        "the invoice reference, amount and due date, say how to pay, and thank "
        "them. Warm and brief."),
    7: ("A friendly payment reminder for an invoice now a week overdue. Assume "
        "an oversight, not bad faith. Restate the invoice reference, amount and "
        "original due date, say how to pay, and invite them to shout if "
        "anything about the invoice looks wrong. Warm and brief."),
    14: ("A second payment reminder for an invoice two weeks overdue. Polite "
         "but firmer than a first nudge: note this is a second reminder, ask "
         "them to reply with a date the payment will be made, and offer to "
         "sort out any problem that's holding it up. No apologies for asking."),
    30: ("A direct payment request for an invoice a month overdue. Courteous, "
         "plain and firm: state how long it has been outstanding, ask what is "
         "blocking payment and for payment (or a payment date) now. If the "
         "customer is clearly a business, you may add ONE gentle sentence "
         "noting that UK law allows statutory interest and compensation on "
         "overdue commercial invoices — never quote an amount or a rate, and "
         "never threaten. This is a nudge toward a conversation, not a legal "
         "letter."),
}


# The 14-day rung says "second reminder" — true only when a first went out. A
# chase composed early ("Chase now") on an invoice never reminded before must not
# claim a first exists (live 2026-09-29: 14 days late, reminders_sent 0, draft
# said "our second reminder").
PURPOSE_14_FIRST = ("A payment reminder for an invoice two weeks overdue, and the "
                    "first one sent. Polite but clear that it is now well past due: "
                    "ask them to reply with a date the payment will be made, and "
                    "offer to sort out any problem that's holding it up. Do NOT "
                    "call it a second or follow-up reminder. No apologies for asking.")


def purpose_for(days_overdue: int, reminders_sent: int = 0) -> str:
    rung = register_for(days_overdue)
    if rung == 14 and not int(reminders_sent or 0):
        return PURPOSE_14_FIRST
    return PURPOSES[rung]


def register_for(days_overdue: int) -> int:
    """Which rung of the ladder a chase composed today should use."""
    return max((m for m in PURPOSES if m <= max(days_overdue, 0)), default=0)


def chase_source_id(invoice_id: str, label) -> str:
    """Deterministic email_log source_message_id: the DB's duplicate guard
    (tenant + source_message_id) makes a re-run or double-click physically
    unable to stage the same chase twice. `label` is the milestone (7/14/30)
    or a manual marker like 'now-2026-09-01'."""
    return f"invoice-chase-{invoice_id}-m{label}"


def invoice_id_from_source(source_message_id: str | None) -> str | None:
    """The inverse, for the approval path: when a pending chase is approved and
    sent, send_draft stamps the chase trail on the invoice this id names."""
    s = str(source_message_id or "")
    if not s.startswith("invoice-chase-"):
        return None
    return s.removeprefix("invoice-chase-").rsplit("-m", 1)[0] or None


def chase_ref_from_intent(body_in: str | None) -> str | None:
    """Invoice ref out of a chase row's intent label ('[invoice chase] INV-1041')
    — how the Money page matches pending chase drafts to overdue rows."""
    s = str(body_in or "")
    return s.removeprefix("[invoice chase] ").strip() if s.startswith("[invoice chase] ") else None


def _facts(inv: dict, days_overdue: int, pay_link: str | None,
           bank_known: bool = True) -> str:
    from app.dates import pretty_date
    ref = inv.get("invoice_ref") or "the invoice"
    parts = [f"Invoice {ref} for £{float(inv.get('total') or 0):,.2f} was due on "
             f"{pretty_date(inv.get('due_date')) or 'its due date'}"
             + (f" and is now {days_overdue} days overdue." if days_overdue > 0
                else " and has not been paid yet.")]
    paid = float(inv.get("amount_paid") or 0)
    if paid > 0:
        parts.append(f"£{paid:,.2f} has been paid; "
                     f"£{max(float(inv.get('total') or 0) - paid, 0):,.2f} is outstanding.")
    n = int(inv.get("reminders_sent") or 0)
    if n:
        parts.append(f"This is reminder number {n + 1} for this invoice.")
    if pay_link:
        parts.append(f"They can pay online here: {pay_link} (include this link).")
    elif bank_known:
        parts.append("Bank transfer details are on the invoice PDF they already "
                     f"have; the payment reference is {ref}.")
    else:
        # Live e2e 2026-09-01: a tenant with no Stripe AND no bank details got a
        # chase claiming "bank details are on the PDF" — they weren't. Never
        # assert a payment route that doesn't exist.
        parts.append("Do not state how to pay — no payment details are "
                     "configured; invite them to reply if they need them.")
    return " ".join(parts)


async def stage_chase(tenant_id: str, cfg: dict, inv: dict, days_overdue: int,
                      *, auto: bool = False, dedupe_label=None,
                      client=None) -> tuple[str, str | None]:
    """Compose one chase and put it on the rails: the draft/approval queue for
    review tenants, straight out of the mailbox for auto-send tenants. Returns
    (outcome, email_log_id) — outcome 'sent' | 'drafted' | 'no_email' |
    'stood_down' (they wrote back since the last touch) | 'already_staged'.

    Stop conditions live HERE so the job and the Money-page button can never
    disagree: no customer email, or an inbound from them since the last touch
    (a robot nudge on top of a live conversation reads as not listening).
    """
    from app import invoice_send, payments
    to = (inv.get("customer_email") or "").strip()
    if not to:
        return "no_email", None
    since = inv.get("last_reminder_at") or inv.get("sent_at") or inv.get("due_date") or ""
    if db.has_inbound_from_since(tenant_id, to, str(since)):
        return "stood_down", None
    ref = inv.get("invoice_ref") or "invoice"
    pay_link = (payments.payment_url(inv["id"])
                if payments.tenant_can_take_payments(tenant_id, client=client) else None)
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    bank_known = bool(s.get("bank_sort_code") and s.get("bank_account_number"))
    purpose = purpose_for(days_overdue, inv.get("reminders_sent"))
    result = await agent.compose_outbound(cfg, purpose,
                                          _facts(inv, days_overdue, pay_link,
                                                 bank_known=bank_known),
                                          tenant_id=tenant_id,
                                          log_operation="reminder")
    body = result.text
    if pay_link and pay_link not in body:
        body += f"\n\nYou can pay online here: {pay_link}"
    subject = f"Payment reminder — invoice {ref}"

    if auto:
        # Auto-send tenants: the chase goes out now, through the same door the
        # concierge's confirmed reminder uses — one place stamps the chase
        # trail (reminders_sent, CRM timeline, sent log row).
        await invoice_send.send_reminder_email(
            tenant_id, inv, disclosure.wrap(cfg, body), client=client, via="auto")
        usage.record_anthropic(tenant_id, result.model, result.usage,
                               operation="reminder")
        return "sent", None

    log_id, created = db.insert_email_log(
        tenant_id, to, subject, f"[invoice chase] {ref}", body,
        status="pending", kind="invoice_reminder",
        source_message_id=chase_source_id(inv["id"], dedupe_label
                                          if dedupe_label is not None
                                          else days_overdue))
    if not created:
        # This exact chase was already staged (re-run, double-click) — the
        # composed text is discarded; the standing draft is the one to review.
        return "already_staged", log_id
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id)
    db.log_agent_event(
        tenant_id, "invoice_reminder",
        f"Drafted a payment chase for {ref} "
        f"({days_overdue} days overdue, £{float(inv.get('total') or 0):,.2f}) — "
        "waiting for your yes",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    return "drafted", log_id
