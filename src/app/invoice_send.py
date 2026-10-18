"""Sending invoices + payment reminders — the one shared sender.

Both surfaces compose this: the Send button on /app/invoices/{id} and the
concierge's invoice_send / invoice_reminder_draft executors (confirm-first —
the operator's Confirm click IS the approval, so the email goes out here).
Everything stamps the full outcome trail: payment_link, status, an email_log
row (the Sent list shows it under Quote Sent), the agent log and the CRM
activity timeline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import db, mailbox
from app import sign_off as sign_off_text
from app.dates import pretty_date

log = logging.getLogger(__name__)


def configured_sign_off(tenant_id: str, *, client=None) -> str:
    """The business's own email sign-off (the one set at onboarding / in Settings) in
    its canonical form, "" when none is set. Best-effort: a config read that fails
    must never stop an invoice or quote going out, so it reads as "none set"."""
    try:
        return sign_off_text.normalise(
            (db.get_tenant_config(tenant_id, client=client) or {}).get("sign_off"))
    except Exception:
        log.warning("sign-off config read failed for tenant %s", tenant_id)
        return ""


def invoice_email_body(inv: dict, s: dict, pay_link: str | None, sign_off: str = "") -> str:
    """The invoice email: amount + due date, then Pay Now link (Stripe connected)
    or the tenant's bank details (fallback), then their configured terms. An empty
    business name signs off plain 'Thank you' — never a placeholder (the old
    fallback mailed customers 'Thank you, us')."""
    from app import invoice_config
    ref = inv.get("invoice_ref") or "invoice"
    biz = (s.get("business_name") or "").strip()
    due = pretty_date(inv.get("due_date"))
    pay = ""
    if pay_link:
        pay = f"\nYou can pay online here: {pay_link}\n"
    else:
        bank = invoice_config.bank_details(s)
        if bank:
            pay = f"\n{bank}\n  Reference: {ref}\n"
    terms = (f"\n{s['payment_terms']}\n"
             if s.get("show_payment_terms") and s.get("payment_terms") else "")
    # The business's own sign-off when it has one; otherwise the plain closing.
    closing = sign_off_text.normalise(sign_off) or (f"Thank you,\n{biz}" if biz else "Thank you")
    return (f"Hello,\n\nPlease find attached invoice {ref} for "
            f"£{float(inv.get('total') or 0):,.2f}"
            + (f", due {due}" if due else "") + f".\n{pay}{terms}\n{closing}")


def payment_route_gap(s: dict, pay_link: str | None) -> str | None:
    """Why the customer would have NO way to pay this invoice, or None when a
    route exists (a Pay Now link or bank details). Surfaced as a warning on
    every send surface — an invoice without a payment route reads as sent but
    can never be settled (2026-07-26 e2e)."""
    from app import invoice_config
    if pay_link or invoice_config.bank_details(s):
        return None
    return ("No payment route on this invoice: Stripe isn't connected and there "
            "are no bank details in Settings › Getting paid, so the customer has no "
            "way to pay.")


def _portal_footer(tenant_id: str, to: str, *, client=None) -> str:
    """The reference-code sign-off approved replies carry (src/app/portal.py) — on
    invoice mail too, so the customer can manage things from the portal. Best-
    effort and empty on any failure; never costs a send."""
    try:
        from app import portal
        cfg = db.get_tenant_config(tenant_id, client=client) or {}
        return portal.email_footer(tenant_id, cfg, to)
    except Exception:
        log.warning("invoice portal footer failed (tenant=%s)", tenant_id, exc_info=True)
        return ""


def _crm_invoice_activity(tenant_id: str, inv: dict, *, activity_type: str,
                          subject: str, summary: str,
                          source_id: str | None = None) -> None:
    """Best-effort contact-timeline entry (idempotent on (type, source_id))."""
    try:
        to = inv.get("customer_email") or ""
        contact = db.crm_contact_by_email(tenant_id, to) if to else None
        db.crm_insert_activity(
            tenant_id, contact_id=(contact or {}).get("id") or inv.get("contact_id"),
            company_id=(contact or {}).get("company_id"),
            activity_type=activity_type, source_id=source_id or inv["id"],
            direction="outbound",
            subject=subject, summary=summary,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            deal_id=inv.get("deal_id"))
    except Exception:
        log.warning("invoice CRM activity failed (%s)", inv.get("id"), exc_info=True)


def _log_sent_email(tenant_id: str, to: str, subject: str, body: str, *,
                    kind: str, via: str, source_message_id: str | None = None) -> None:
    """One email_log row, immediately marked sent → the 'quote_sent' label (an
    outbound commercial email, not something awaiting the owner's reply).
    `source_message_id` ("invoice:<id>:sent") lets the thread find the PDF the
    email carried, for the paperclip on its bubble (Kris 2026-09-20)."""
    try:
        log_id, _ = db.insert_email_log(tenant_id, to, subject, f"[{kind}]", body,
                                        status="pending", kind=kind,
                                        source_message_id=source_message_id)
        db.mark_email_log_sent(log_id, tenant_id, via=via, edited=False)
    except Exception:
        log.warning("invoice email_log write failed", exc_info=True)


async def send_invoice_email(tenant_id: str, inv: dict, *, client=None,
                             via: str = "human",
                             body_override: str | None = None) -> None:
    """Send one invoice (PDF + Pay Now link when Stripe is connected) from the
    tenant's mailbox and stamp the outcome. Raises on send failure — callers
    decide how to surface it. `body_override` is the operator-edited email text
    from the concierge card's preview; the PDF and stamps are unaffected."""
    from app import invoice_config, invoice_pdf, payments
    invoice_id, to = inv["id"], (inv.get("customer_email") or "").strip()
    lines = db.list_invoice_lines(invoice_id, tenant_id, client=client)
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    ref = inv.get("invoice_ref") or "invoice"
    pay_link = (payments.payment_url(invoice_id)
                if payments.tenant_can_take_payments(tenant_id, client=client) else None)
    body = (body_override or "").strip() or invoice_email_body(
        inv, s, pay_link, configured_sign_off(tenant_id, client=client))
    pdf = invoice_pdf.render(inv, lines, s)
    # Job parties: 'kept informed' parties ride as cc (opt-in, D1). With a cc
    # the portal footer is DROPPED — its reference code is the payer's identity
    # capability and must never reach a cc'd party.
    from app import job_parties
    cc = job_parties.cc_for_document(tenant_id, inv.get("booking_id"), to,
                                     client=client, redirect_guard=True)
    footer = "" if cc else _portal_footer(tenant_id, to, client=client)
    await mailbox.send_mail(tenant_id, to, f"Invoice {ref}", body + footer,
                            attachments=[(f"{ref}.pdf", pdf, "application/pdf")],
                            cc=cc or None)
    if pay_link:
        db.set_invoice_payment_link(invoice_id, tenant_id, pay_link, client=client)
    db.set_invoice_status(invoice_id, tenant_id, "sent", client=client)
    # Packages (src/app/wallet.py): an issued invoice mints the credits its package
    # lines sold — best-effort, the send already happened.
    try:
        from app import wallet
        wallet.on_invoice_issued(tenant_id, {**inv, "status": "sent"}, client=client)
    except Exception:
        log.warning("wallet mint on issue failed (%s)", invoice_id, exc_info=True)
    _log_sent_email(tenant_id, to, f"Invoice {ref}", body, kind="invoice", via=via,
                    source_message_id=f"invoice:{invoice_id}:sent")
    db.log_agent_event(tenant_id, "sent", f"Emailed invoice {ref} to {to}"
                       + (" with a Pay Now link" if pay_link else ""),
                       direction="outbound", contact=to, subject=f"Invoice {ref}",
                       client=client)
    _crm_invoice_activity(
        tenant_id, inv, activity_type="invoice", subject=f"Invoice {ref} sent",
        summary=f"£{float(inv.get('total') or 0):,.2f}, "
                f"due {pretty_date(inv.get('due_date'))}")
    try:   # Action-side promise resolution (2026-09-03): "Send invoice to
           # Kris at the end of each stay" stayed open after INV-1001 went out
           # — the sent invoice IS the fulfilment, tick the ledger now.
        from app import crm_intel
        await crm_intel.resolve_from_action(
            tenant_id, to,
            f"Sent invoice {ref} (£{float(inv.get('total') or 0):,.2f}) to "
            "the customer", client=client,
            ref=("invoice", inv.get("id"), ref))
    except Exception:
        log.warning("invoice send: promise resolution failed", exc_info=True)


async def send_reminder_email(tenant_id: str, inv: dict, text: str, *,
                              client=None, via: str = "human") -> None:
    """Send a payment reminder for an unpaid invoice and stamp the chase trail
    (reminders_sent — the 'hasn't paid' escalation signal, NOT the draft
    suppression counter: chasing money is a different behaviour to ignoring a
    conversation)."""
    invoice_id, to = inv["id"], (inv.get("customer_email") or "").strip()
    ref = inv.get("invoice_ref") or "invoice"
    subject = f"Payment reminder — invoice {ref}"
    from app import job_parties
    cc = job_parties.cc_for_document(tenant_id, inv.get("booking_id"), to,
                                     client=client, redirect_guard=True)
    footer = "" if cc else _portal_footer(tenant_id, to, client=client)
    await mailbox.send_mail(tenant_id, to, subject, text + footer,
                            cc=cc or None)
    db.record_invoice_reminder(invoice_id, tenant_id, client=client)
    _log_sent_email(tenant_id, to, subject, text, kind="invoice_reminder", via=via)
    db.log_agent_event(tenant_id, "invoice_reminder",
                       f"Payment reminder for {ref} sent to {to}",
                       direction="outbound", contact=to, subject=subject,
                       client=client)
    n = int(inv.get("reminders_sent") or 0) + 1     # each chase is its own timeline row
    _crm_invoice_activity(
        tenant_id, inv, activity_type="invoice_reminder",
        source_id=f"{invoice_id}:r{n}",
        subject=f"Payment reminder — {ref}",
        summary=f"£{float(inv.get('total') or 0):,.2f} outstanding, "
                f"due {pretty_date(inv.get('due_date'))}")
