"""The public quote page — /q/{quote_id} (docs/modules/module-quotes.md).

The uuid is the capability (the /pay/{invoice_id} doctrine): no login, no
tenant scope, rate-limited, noindex. The page IS the quote — branded via the
shared invoice_settings, first open stamps viewed_at (+ the quote-viewed push,
default on), optional lines are tickboxes whose totals recompute SERVER-side,
and acceptance is a typed-name signature that freezes db.accept_quote's
snapshot. A deposit-configured quote drops the customer straight onto the
deposit invoice's /pay page after accepting — sign, then pay, one flow.

Questions asked here land in the tenant's inbox as a needs-reply conversation
(the portal-escalation shape) — never an email ping-pong thread on the side.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import db, events, push, quoting, trace
from app.security import rate_limit
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["quotes-public"])


def _load_visible_quote(quote_id: str) -> dict:
    """Anything the customer may see. Drafts are the owner's workspace and a
    cancelled quote was withdrawn — both 404 (indistinguishable from a bad id)."""
    try:
        uuid.UUID(quote_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Quote not found")
    quote = db.get_quote_any_tenant(quote_id)
    if not quote or quote.get("status") in ("draft", "cancelled"):
        raise HTTPException(status_code=404, detail="Quote not found")
    return quote


def _date_expired(quote: dict) -> bool:
    valid = str(quote.get("valid_until") or "")[:10]
    return bool(valid) and valid < date.today().isoformat()


def _display_state(quote: dict) -> str:
    """What the page shows: sent quotes past their validity date render as
    expired even before the daily job flips the row (the read-side derivation,
    same doctrine as overdue invoices)."""
    status = quote.get("status") or "sent"
    if status == "sent" and _date_expired(quote):
        return "expired"
    if status == "invoiced":
        return "accepted"       # still an agreed quote — just also billed by now
    return status


def _settings_for(quote: dict) -> dict:
    from app import invoice_config
    return invoice_config.settings_for(db.get_invoice_settings(quote["tenant_id"]))


@router.get("/q/{quote_id}",
            dependencies=[Depends(rate_limit("quote_page", 30, 60))])
def quote_page(request: Request, quote_id: str, msg: str = ""):
    quote = _load_visible_quote(quote_id)
    tenant_id = quote["tenant_id"]
    if quote.get("status") == "sent" and db.set_quote_viewed(quote_id, tenant_id):
        trace.emit(tenant_id, "quote_viewed", contact=quote.get("customer_email"),
                   detail={"quote_ref": quote.get("quote_ref")})
        try:
            # Always told, not always announced (Kris 2026-09-04). The
            # "also when they just open a quote" toggle used to wrap this call,
            # which skipped the FEED ROW as well as the push — so a tenant with
            # it off had no record their customer ever opened the document.
            # notify_prefs owns that decision now; here we just report the fact.
            who = quote.get("customer_name") or quote.get("customer_email") or "Customer"
            push.notify(tenant_id,
                        title=f"{who} viewed quote {quote.get('quote_ref')}",
                        body=f"£{float(quote.get('total') or 0):,.2f} — not accepted yet",
                        url=f"/app/quotes/{quote_id}",
                        tag=f"quote-viewed-{quote_id}", kind="quote_viewed",
                        contact=quote.get("customer_email"),
                        contact_name=quote.get("customer_name"))
        except Exception:
            log.warning("quote viewed push failed", exc_info=True)
        quote = db.get_quote_any_tenant(quote_id) or quote   # fresh view_count
    state = _display_state(quote)
    if state == "accepted" and quote.get("acceptance_snapshot"):
        # The agreed document, not the live rows — they can never differ today,
        # but the snapshot is the contract so the snapshot is what renders.
        lines = list(quote["acceptance_snapshot"].get("lines") or [])
        snap = quote["acceptance_snapshot"]
        totals = {"subtotal": snap.get("subtotal"), "vat": snap.get("vat"),
                  "total": snap.get("total")}
    else:
        lines = db.list_quote_lines(quote_id, tenant_id)
        totals = {"subtotal": quote.get("subtotal"), "vat": quote.get("vat"),
                  "total": quote.get("total")}
    # The accepted state answers "what happens next" (live e2e 2026-09-01:
    # Julius — "the acceptance screen looks a bit bare"): the deposit invoice,
    # if one was minted, is the next step, so the page fetches it to say so.
    deposit_inv = None
    if state == "accepted" and quote.get("deposit_invoice_id"):
        try:
            deposit_inv = db.get_invoice(quote["deposit_invoice_id"], tenant_id)
        except Exception:
            log.warning("deposit invoice read failed for /q %s", quote_id,
                        exc_info=True)
    return templates.TemplateResponse(request, "quote_public.html", {
        "q": quote, "lines": lines, "totals": totals, "s": _settings_for(quote),
        "state": state, "msg": msg,
        "deposit_net": quoting.deposit_net_amount(quote),
        "deposit_inv": deposit_inv,
    })


@router.post("/q/{quote_id}/select",
             dependencies=[Depends(rate_limit("quote_select", 30, 60))])
def quote_select(quote_id: str, line_id: str = Form(...),
                 selected: str = Form("")):
    """An optional-line tick. Totals recompute server-side; the guard inside
    set_quote_line_selected refuses non-optional lines and non-'sent' quotes."""
    quote = _load_visible_quote(quote_id)
    if _display_state(quote) == "sent":
        db.set_quote_line_selected(quote_id, quote["tenant_id"], line_id.strip(),
                                   selected == "1")
    return RedirectResponse(f"/q/{quote_id}", status_code=303)


@router.post("/q/{quote_id}/accept",
             dependencies=[Depends(rate_limit("quote_accept", 10, 60))])
async def quote_accept(quote_id: str, accepted_name: str = Form("")):
    """The typed-name acceptance (the partner-agreement e-sign pattern: typed
    full name + timestamp + version, recorded against the frozen snapshot)."""
    quote = _load_visible_quote(quote_id)
    tenant_id = quote["tenant_id"]
    name = (accepted_name or "").strip()
    if not name:
        return RedirectResponse(f"/q/{quote_id}?msg=name_required", status_code=303)
    if _display_state(quote) != "sent":
        return RedirectResponse(f"/q/{quote_id}", status_code=303)
    row = db.accept_quote(quote_id, tenant_id, name)
    if row is None:      # raced a concurrent accept/decline/edit — show current state
        return RedirectResponse(f"/q/{quote_id}", status_code=303)
    accepted_side_effects(tenant_id, row)
    await resolve_accept_promises(tenant_id, row)
    deposit_url = mint_deposit_invoice(tenant_id, row)
    if deposit_url:
        await email_deposit_invoice(tenant_id, quote_id)
        return RedirectResponse(deposit_url, status_code=303)
    return RedirectResponse(f"/q/{quote_id}?msg=accepted", status_code=303)


async def resolve_accept_promises(tenant_id: str, quote: dict) -> None:
    """An accepted quote settles what the customer owed ("Kris will confirm the
    quote") and what the business owed by quoting — the same action-side pass
    invoices and bookings run (2026-09-03). Best-effort, zero spend when the
    contact has no open promises."""
    try:
        from app import crm_intel
        await crm_intel.resolve_from_action(
            tenant_id, quote.get("customer_email"),
            f"Customer accepted quote {quote.get('quote_ref') or ''} "
            f"(£{float(quote.get('total') or 0):,.2f}"
            f"{' — ' + quote['title'] if quote.get('title') else ''}) "
            f"signed as {quote.get('accepted_name') or 'the customer'}",
            ref=("quote", quote.get("id"), quote.get("quote_ref")))
    except Exception:
        log.warning("quote accept: promise resolution failed", exc_info=True)


async def email_deposit_invoice(tenant_id: str, quote_id: str) -> bool:
    """The minted deposit invoice goes to the customer the same way every other
    invoice does — PDF, bank details or Pay Now, the portal footer, the CRM
    line, the promise hook. Live 2026-09-03 (Stone Mason): INV-1001 was
    stamped 'sent' at acceptance and the customer never received anything —
    the redirect to /pay was the only delivery, and nothing to find later.
    Best-effort: a mail hiccup never costs the acceptance."""
    try:
        quote = db.get_quote(quote_id, tenant_id) or {}
        dep_id = quote.get("deposit_invoice_id")
        if not dep_id:
            return False
        inv = db.get_invoice(dep_id, tenant_id)
        if not inv or not (inv.get("customer_email") or "").strip():
            return False
        from app import invoice_send
        await invoice_send.send_invoice_email(tenant_id, inv, via="ai")
        return True
    except Exception:
        log.warning("deposit invoice email failed (quote=%s)", quote_id, exc_info=True)
        return False


def accepted_side_effects(tenant_id: str, quote: dict) -> None:
    """Everything acceptance triggers beyond the row itself. Best-effort — a
    side-effect failure never unwinds the acceptance."""
    ref = quote.get("quote_ref") or "quote"
    summary = quoting.acceptance_summary(quote)
    snap = quote.get("acceptance_snapshot") or {}
    try:
        trace.emit(tenant_id, "quote_accepted", contact=quote.get("customer_email"),
                   detail={"quote_ref": ref, "total": quote.get("total"),
                           "accepted_name": quote.get("accepted_name")})
    except Exception:
        log.warning("quote accept trace failed", exc_info=True)
    try:
        # The cross-module seam: invoicing's draft builders and the vertical
        # readers all price completed work off bookings.quoted_amount, in the
        # line basis (net when VAT is added; the stated price when prices
        # include VAT, so a later draft invoice bills what was agreed).
        if quote.get("booking_id") and snap.get("subtotal") is not None:
            db.set_booking_quote(quote["booking_id"], tenant_id,
                                 quoting.amount_as_written(
                                     {**snap, "prices_include_vat":
                                      snap.get("prices_include_vat", quote.get("prices_include_vat"))}))
    except Exception:
        log.warning("quote accept booking stamp failed (%s)", quote.get("id"),
                    exc_info=True)
    try:
        push.notify(tenant_id, title=f"Quote {ref} accepted ✓",
                    body=summary, url=f"/app/quotes/{quote['id']}",
                    tag=f"quote-accepted-{quote['id']}", kind="quote_accepted",
                    contact=quote.get("customer_email"),
                    contact_name=quote.get("customer_name"))
    except Exception:
        log.warning("quote accept push failed", exc_info=True)
    try:
        db.log_agent_event(tenant_id, "quote_accepted", summary,
                           contact=quote.get("customer_email"),
                           subject=f"Quote {ref} accepted")
    except Exception:
        log.warning("quote accept agent event failed", exc_info=True)
    try:
        from app.quote_send import _crm_quote_activity
        _crm_quote_activity(tenant_id, quote, activity_type="quote_accepted",
                            source_id=f"{quote['id']}:accepted",
                            subject=f"Quote {ref} accepted", summary=summary)
    except Exception:
        log.warning("quote accept CRM activity failed", exc_info=True)
    if quote.get("deal_id"):
        # The invoice-paid → Won pattern (payments.invoice_paid_followup): the
        # nudge seeds the conversation; the owner's reply mints the normal
        # confirm-first proposal. Nothing auto-executes.
        try:
            who = quote.get("customer_name") or quote.get("customer_email") or "the customer"
            db.add_concierge_message(
                tenant_id, "assistant",
                f"Quote {ref} (£{float(quote.get('total') or 0):,.2f}) is accepted "
                f"by {who} — shall I move their deal forward? Just tell me which "
                f"stage and I'll set it up for you to confirm.")
        except Exception:
            log.warning("quote-accepted concierge nudge failed", exc_info=True)


def mint_deposit_invoice(tenant_id: str, quote: dict) -> str | None:
    """Deposit-at-acceptance: mint the deposit invoice and hand back its /pay
    URL, or None when no deposit is configured (or minting failed — acceptance
    itself must never be blocked by invoice plumbing)."""
    lines = quoting.deposit_invoice_lines(quote)
    if not lines:
        return None
    try:
        inv = db.create_invoice(
            tenant_id, quote.get("customer_email"), quote.get("customer_name"),
            None, None, lines, contact_id=quote.get("contact_id"),
            deal_id=quote.get("deal_id"), booking_id=quote.get("booking_id"),
            notes=f"Deposit for quote {quote.get('quote_ref') or ''}".strip(),
            customer_reference=quote.get("customer_reference"),
            prices_include_vat=quote.get("prices_include_vat") is True,   # inherits the quote's basis
            created_by="ai")
        if not inv:
            return None
        # /pay 404s drafts (a draft was never issued) — this one is issued by
        # the act of acceptance.
        db.set_invoice_status(inv["id"], tenant_id, "sent")
        db.link_quote_invoice(quote["id"], tenant_id, inv["id"], deposit=True)
        from app import payments
        url = payments.payment_url(inv["id"])
        db.set_invoice_payment_link(inv["id"], tenant_id, url)
        return url
    except Exception:
        log.warning("deposit invoice mint failed (quote=%s)", quote.get("id"),
                    exc_info=True)
        return None


@router.post("/q/{quote_id}/decline",
             dependencies=[Depends(rate_limit("quote_decline", 10, 60))])
def quote_decline(quote_id: str, reason: str = Form("")):
    quote = _load_visible_quote(quote_id)
    tenant_id = quote["tenant_id"]
    if _display_state(quote) != "sent":
        return RedirectResponse(f"/q/{quote_id}", status_code=303)
    row = db.decline_quote(quote_id, tenant_id, reason)
    if row is None:
        return RedirectResponse(f"/q/{quote_id}", status_code=303)
    ref = row.get("quote_ref") or "quote"
    who = row.get("customer_name") or row.get("customer_email") or "Customer"
    why = (reason or "").strip()
    try:
        trace.emit(tenant_id, "quote_declined", contact=row.get("customer_email"),
                   detail={"quote_ref": ref, "reason": why[:300] or None})
        push.notify(tenant_id, title=f"{who} declined quote {ref}",
                    body=(why[:120] or "No reason given"),
                    url=f"/app/quotes/{quote_id}",
                    tag=f"quote-declined-{quote_id}", kind="quote_declined",
                    contact=row.get("customer_email"),
                    contact_name=row.get("customer_name"))
        db.log_agent_event(tenant_id, "quote_declined",
                           f"{who} declined {ref}" + (f" — {why[:200]}" if why else ""),
                           contact=row.get("customer_email"),
                           subject=f"Quote {ref} declined")
    except Exception:
        log.warning("quote decline side effects failed", exc_info=True)
    return RedirectResponse(f"/q/{quote_id}?msg=declined", status_code=303)


@router.post("/q/{quote_id}/question",
             dependencies=[Depends(rate_limit("quote_question", 6, 3600))])
def quote_question(quote_id: str, message: str = Form(""),
                   reply_to: str = Form("")):
    """A question asked ON the quote lands in the tenant's inbox as a normal
    needs-attention conversation (the portal-escalation shape) — the owner
    replies through the standard approval flow, with the quote as context."""
    quote = _load_visible_quote(quote_id)
    tenant_id = quote["tenant_id"]
    text = (message or "").strip()
    if not text:
        return RedirectResponse(f"/q/{quote_id}?msg=empty_question", status_code=303)
    email = (reply_to or "").strip() or (quote.get("customer_email") or "")
    name = quote.get("customer_name") or email or "Customer"
    ref = quote.get("quote_ref") or "quote"
    log_id, _ = db.insert_email_log(
        tenant_id, email,
        f"Question about quote {ref}",
        f"[Asked on the quote page /q — reply goes to {email or 'unknown'}]\n\n{text[:2000]}",
        "", status="pending",
        source_message_id=f"quote:{quote_id}:{uuid.uuid4()}",
        thread_state="needs_attention",
        request_summary=f"{name} asked a question about quote {ref}",
        from_name=name, contact_name=quote.get("customer_name"),
        category="needs_reply",
        category_reason="Question asked on the public quote page")
    try:
        db.log_agent_event(tenant_id, "escalated",
                           f"{name} asked about quote {ref} — needs your reply",
                           contact=email, email_log_id=log_id)
        push.notify(tenant_id, title=f"Question on quote {ref} — {name}",
                    body=(text[:120] + "…") if len(text) > 120 else text,
                    url="/app/conversations/view?contact=" + (email or ""),
                    tag=f"quote-question-{log_id}", kind="quote_question",
                    contact=email, contact_name=quote.get("customer_name") or name)
        events.publish(tenant_id, {"type": "status_changed", "log_id": log_id,
                                   "contact": email, "status": "pending"})
        trace.emit(tenant_id, "quote_question", contact=email,
                   detail={"quote_ref": ref, "message": text[:300]})
    except Exception:
        log.warning("quote question side effects failed", exc_info=True)
    return RedirectResponse(f"/q/{quote_id}?msg=question_sent", status_code=303)


@router.get("/q/{quote_id}.pdf",
            dependencies=[Depends(rate_limit("quote_pdf", 10, 60))])
def quote_pdf_download(quote_id: str):
    """The PDF courtesy copy — the page is the quote; this is for filing. An
    accepted quote's PDF renders the SNAPSHOT (the agreed document) and carries
    the acceptance record."""
    from app import quote_pdf
    quote = _load_visible_quote(quote_id)
    tenant_id = quote["tenant_id"]
    snap = quote.get("acceptance_snapshot") or {}
    if _display_state(quote) == "accepted" and snap.get("lines"):
        lines = snap["lines"]
        doc = {**quote, "subtotal": snap.get("subtotal", quote.get("subtotal")),
               "vat": snap.get("vat", quote.get("vat")),
               "total": snap.get("total", quote.get("total"))}
    else:
        lines = [l for l in db.list_quote_lines(quote_id, tenant_id)
                 if l.get("is_selected", True)]
        doc = quote
    pdf = quote_pdf.render(doc, lines, _settings_for(quote))
    ref = quote.get("quote_ref") or "quote"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="{ref}.pdf"'})
