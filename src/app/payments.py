"""Invoice payments — Stripe Connect "Pay Now" + the paid-invoice lifecycle.

The tenant connects their OWN Stripe account (Connect OAuth, no keys stored — the
token exchange returns an account id). An issued invoice then carries a permanent
payment link to OUR public pay page (/pay/{invoice_id} — the uuid is the capability,
same pattern as /track/{token}); the page mints a FRESH Checkout Session on the
connected account at click time, so the link never expires the way a raw Checkout
URL (24h) would, and first-open doubles as the invoice's viewed_at stamp. Money
lands directly in the tenant's Stripe — BLAM8 takes no cut.

Without Stripe the same pay page shows the invoice + the tenant's bank-transfer
details (invoice_settings), and the owner marks it paid manually.

Webhook: /webhooks/stripe is the CONNECT endpoint (event["account"] set), with its
own signing secret — the platform-billing webhook at /stripe/webhook is untouched.
Invoice PaymentIntents are tagged metadata.kind='invoice' (mirroring the Assets
deposit marker) so nothing else can ever be mistaken for one.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import db, payments_panel, push, trace
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.security import rate_limit, require_csrf
from app.templating import templates
from app import guards as _guards

# "Pay all together" (docs/plan-payments-panel.md §0.5): Stripe metadata values are
# 500 characters, and a uuid list is 37 per invoice — ten is the honest cap, and
# more than ten open invoices is a statement, not a checkout.
BATCH_MAX = 10

log = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])


# ── capability checks ────────────────────────────────────────────────────────
def connect_configured() -> bool:
    """Platform-side: can tenants connect a Stripe account at all?"""
    return bool(settings.stripe_secret_key and settings.stripe_connect_client_id)


def tenant_can_take_payments(tenant_id: str, client=None) -> bool:
    """Tenant-side: is there a connected account to route a Checkout Session to?"""
    return connect_configured() and bool(db.get_stripe_account_id(tenant_id, client=client))


def payment_url(invoice_id: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/pay/{invoice_id}"


# ── Connect OAuth (no keys stored — the exchange returns an account id) ──────
def _state_for(tenant_id: str) -> str:
    """Signed OAuth state: tenant.timestamp.hmac — the callback is public, so the
    state is what proves which tenant initiated the connect (and when)."""
    ts = str(int(time.time()))
    sig = hmac.new(settings.token_encryption_key.encode(),
                   f"{tenant_id}.{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{tenant_id}.{ts}.{sig}"


def _tenant_from_state(state: str, max_age_s: int = 900) -> str | None:
    parts = (state or "").split(".")
    if len(parts) != 3:
        return None
    tenant_id, ts, sig = parts
    want = hmac.new(settings.token_encryption_key.encode(),
                    f"{tenant_id}.{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return None
    try:
        if time.time() - int(ts) > max_age_s:
            return None
    except ValueError:
        return None
    return tenant_id


@router.get("/app/payments/connect")
def payments_connect(ctx: TenantCtx = Depends(require_onboarded)):
    """Send the owner to Stripe's OAuth consent — 'Connect your Stripe account to
    accept payments'. No Stripe account yet? Stripe's flow offers signup inline."""
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    if not connect_configured():
        return RedirectResponse("/app/invoices/settings?msg=stripe_unavailable",
                                status_code=303)
    from urllib.parse import urlencode
    state = _state_for(ctx.tenant_id)
    q = urlencode({
        "response_type": "code",
        "client_id": settings.stripe_connect_client_id,
        "scope": "read_write",
        "state": state,
        "redirect_uri": f"{settings.app_base_url.rstrip('/')}/payments/connect/callback",
    })
    resp = RedirectResponse(f"https://connect.stripe.com/oauth/authorize?{q}", status_code=303)
    from app import security as _security
    _security.bind_consent(resp, state, "/payments")   # M8: only this browser may finish it
    return resp


@router.get("/payments/connect/callback",
            dependencies=[Depends(rate_limit("stripe_connect_cb", 10, 60))])
def payments_connect_callback(request: Request):
    """Public (Stripe redirects here) — the signed state carries the tenant."""
    tenant_id = _tenant_from_state(request.query_params.get("state") or "")
    from app import security as _security
    if tenant_id and not _security.consent_is_this_browsers(request, request.query_params.get("state")):
        log.warning("stripe connect callback: state not started in this browser (tenant=%s)", tenant_id)
        tenant_id = None                                         # M8
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Bad or expired state")
    if request.query_params.get("error"):
        # Owner clicked cancel on Stripe's page — no drama.
        return RedirectResponse("/app/invoices/settings?msg=stripe_declined",
                                status_code=303)
    code = request.query_params.get("code") or ""
    try:
        stripe.api_key = settings.stripe_secret_key
        resp = stripe.OAuth.token(grant_type="authorization_code", code=code)
        account_id = resp.get("stripe_user_id")
    except Exception:
        log.warning("stripe connect token exchange failed (tenant=%s)",
                    tenant_id, exc_info=True)
        return RedirectResponse("/app/invoices/settings?msg=stripe_failed",
                                status_code=303)
    if not account_id:
        return RedirectResponse("/app/invoices/settings?msg=stripe_failed",
                                status_code=303)
    db.set_stripe_account_id(tenant_id, account_id)
    trace.emit(tenant_id, "stripe_connect", detail={"account_id": account_id})
    db.log_agent_event(tenant_id, "payments",
                       "Stripe connected — invoices now go out with a Pay Now link")
    return RedirectResponse("/app/invoices/settings?msg=stripe_connected",
                            status_code=303)


@router.post("/app/payments/disconnect", dependencies=[Depends(require_csrf)])
def payments_disconnect(ctx: TenantCtx = Depends(require_onboarded)):
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    db.set_stripe_account_id(ctx.tenant_id, None)
    trace.emit(ctx.tenant_id, "stripe_connect", outcome="disconnected", detail={})
    return RedirectResponse("/app/invoices/settings?msg=stripe_disconnected",
                            status_code=303)


# ── the public pay page ──────────────────────────────────────────────────────
_PAYABLE = set(db.INVOICE_PAYABLE_STATUSES) - {"draft"}   # a draft was never issued


def _pay_page_html(inv: dict, s: dict, *, paid: bool, bank: str | None,
                   pay_now: bool, lines: list[dict] | None = None,
                   others: list[dict] | None = None, pending: bool = False) -> str:
    """The customer's invoice page — self-contained, no app chrome (the viewer is
    the tenant's CUSTOMER), styled like the public quote page it follows on
    from: the business header, the invoice's lines and totals, the due date,
    then how to pay. Kris 2026-09-04: it was a bare monospace bank block under
    one line — "the final invoice page could look a little better"."""
    from html import escape as e
    from app.dates import pretty_date
    ref = inv.get("invoice_ref") or "Invoice"
    # An empty business name shows NOTHING, never a placeholder — the invoice
    # sender's "Thank you, us" lesson (live e2e 2026-09-01: the page was headed
    # literally "the business").
    biz = (s.get("business_name") or "").strip()
    address = (s.get("business_address") or "").strip()
    contact = (s.get("business_contact") or "").strip()
    total = float(inv.get("total") or 0)
    due = pretty_date(inv.get("due_date"))
    issued = pretty_date(inv.get("issue_date"))
    lines = lines or []

    def money(v) -> str:
        return f"£{float(v or 0):,.2f}"

    header = ""
    if biz or address or contact:
        header = "<div class='biz'>" + (f"<h1>{e(biz)}</h1>" if biz else "") + \
            (f"<p class='muted small'>{e(address)}</p>" if address else "") + \
            (f"<p class='muted small'>{e(contact)}</p>" if contact else "") + "</div>"
    meta = [f"Issued {issued}" if issued else "", f"due {due}" if due and not paid else ""]
    meta_line = " · ".join(m for m in meta if m)
    your_ref = inv.get("customer_reference")
    rows = "".join(
        f"<tr><td>{e(str(l.get('description') or ''))}</td>"
        f"<td class='muted small'>{e(str(l.get('date') or ''))}</td>"
        f"<td class='num'>{money(l.get('amount'))}</td></tr>" for l in lines)
    table = ""
    if rows:
        if inv.get("prices_include_vat") is True and s.get("show_vat"):
            totals = (f"<tr class='grand'><td></td><td class='label'>Total</td><td class='num'>{money(total)} "
                      f"<span class='muted small'>(includes VAT {money(inv.get('vat'))})</span></td></tr>")
        else:
            totals = f"<tr><td></td><td class='label'>Subtotal</td><td class='num'>{money(inv.get('subtotal'))}</td></tr>"
            if s.get("show_vat"):
                totals += (f"<tr><td></td><td class='label'>VAT ({float(s.get('vat_rate') or 0):g}%)</td>"
                           f"<td class='num'>{money(inv.get('vat'))}</td></tr>")
            totals += f"<tr class='grand'><td></td><td class='label'>Total</td><td class='num'>{money(total)}</td></tr>"
        table = ("<table><thead><tr><th>Item</th><th>Date</th><th class='num'>Amount</th></tr></thead>"
                 f"<tbody>{rows}</tbody><tbody class='totals'>{totals}</tbody></table>")
    if paid:
        body = "<div class='banner ok'>✓ This invoice is paid — thank you.</div>"
    elif pending:
        # A Bacs / Pay by Bank payment submitted and clearing (plan §0.7): offering
        # Pay Now again here would invite a second payment.
        body = ("<div class='banner wait'>Your bank payment is being processed — nothing "
                "more to do. It usually clears within three working days, and this page "
                "will show the invoice as paid once it has.</div>")
    else:
        bits = []
        if pay_now:
            bits.append(f"<form method='post' action='/pay/{inv['id']}/checkout'>"
                        f"<button type='submit'>Pay {money(total)} now</button></form>")
            if others:
                # RPP's "Outstanding Balance": the same customer's other open invoices
                # with this business, settled in one checkout (plan §0.5).
                owed = sum(_balance(o) for o in others) + _balance(inv)
                n = len(others)
                refs = ", ".join(e(str(o.get("invoice_ref") or "")) for o in others)
                bits.append(
                    f"<div class='pay-box'><p class='how'>You also have {n} other unpaid "
                    f"invoice{'s' if n != 1 else ''} with {e(biz or 'us')}</p>"
                    f"<p class='muted small'>{refs}</p>"
                    f"<form method='post' action='/pay/{inv['id']}/checkout-all'>"
                    f"<button type='submit' class='secondary'>Pay all {n + 1} together — "
                    f"{money(owed)}</button></form></div>")
        if s.get("bank_sort_code") and s.get("bank_account_number"):
            acct = s.get("bank_account_name") or biz
            bits.append(
                "<div class='pay-box'><p class='how'>Pay by bank transfer</p>"
                + (f"<p><span class='k'>Account name</span> {e(str(acct))}</p>" if acct else "")
                + f"<p><span class='k'>Sort code</span> {e(str(s.get('bank_sort_code')))}</p>"
                + f"<p><span class='k'>Account number</span> {e(str(s.get('bank_account_number')))}</p>"
                + f"<p><span class='k'>Reference</span> <strong>{e(ref)}</strong></p>"
                + (f"<p class='muted small'>Please use {e(ref)} as the reference so the payment "
                   "is matched to this invoice.</p>") + "</div>")
        elif bank:
            bits.append(f"<pre>{e(bank)}\n  Reference: {e(ref)}</pre>")
        if not bits:
            # No payment route at all: give the customer an actual way to act,
            # not just "contact the business" with nobody named.
            who = biz or "us"
            how = f" ({e(contact)})" if contact else \
                " — replying to the invoice email works"
            bits.append(f"<p>Please get in touch with {e(who)}{how} to arrange "
                        "payment.</p>")
        body = "\n".join(bits)
    terms = (s.get("payment_terms") or "").strip() if s.get("show_payment_terms", True) else ""
    footer_note = (s.get("footer_note") or "").strip()
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<meta name='robots' content='noindex'><title>{e(ref)}{f" — {e(biz)}" if biz else ""}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1a202c;line-height:1.5;background:#fff}}
h1{{font-size:1.25rem;margin:0 0 .25rem}}h2{{font-size:1.1rem;margin:1.25rem 0 .25rem}}
table{{width:100%;border-collapse:collapse;margin:.75rem 0}}
th{{font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;color:#718096;text-align:left;border-bottom:1px solid #e2e8f0;padding:.4rem 0}}
td{{padding:.45rem 0;border-bottom:1px solid #edf2f7;vertical-align:top}}
.num{{text-align:right;white-space:nowrap}}.totals td{{border-bottom:0;padding:.15rem 0}}
.totals .label{{text-align:right;color:#718096}}.grand td{{font-weight:700;font-size:1.05rem}}
.muted{{color:#718096}}.small{{font-size:.85rem}}.biz{{margin-bottom:1.5rem}}
.banner{{border-radius:8px;padding:.8rem 1rem;margin:1rem 0;font-weight:600}}.ok{{background:#f0fff4;color:#276749}}.wait{{background:#fffaf0;color:#975a16}}
button{{background:#2b6cb0;color:#fff;border:0;border-radius:8px;padding:.8rem 1.6rem;font-size:1rem;cursor:pointer;margin:.5rem 0 1rem}}
button.secondary{{background:#fff;color:#2b6cb0;border:1px solid #2b6cb0;margin:.5rem 0 0}}
.pay-box{{background:#f7fafc;border:1px solid #e2e8f0;border-radius:10px;padding:1rem 1.25rem;margin:1rem 0}}
.pay-box p{{margin:.25rem 0}}.pay-box .how{{font-weight:700;margin-bottom:.5rem}}
.k{{display:inline-block;min-width:8.5rem;color:#718096}}
pre{{background:#f7fafc;padding:1rem;border-radius:8px;white-space:pre-wrap}}
footer{{margin-top:2.5rem;font-size:.8rem;color:#a0aec0}}
</style></head><body>
{header}
<h2>Invoice {e(ref)}</h2>
<p class='muted small'>{e(meta_line)}{f" · Your ref {e(str(your_ref))}" if your_ref else ""}</p>
{table if table else f"<p><strong>{money(total)}</strong>{f' · due {e(due)}' if due and not paid else ''}</p>"}
{body}
{f"<p class='muted small'>{e(terms)}</p>" if terms and not paid else ""}
{f"<p class='muted small'>{e(footer_note)}</p>" if footer_note else ""}
<footer>Powered by BLAM8</footer>
</body></html>"""


def _balance(inv: dict) -> float:
    """What is still owed on an invoice — total less anything already paid."""
    return round(float(inv.get("total") or 0) - float(inv.get("amount_paid") or 0), 2)


def _payment_pending(inv: dict) -> bool:
    """A bank payment submitted but not yet cleared: the invoice is still payable, a
    PaymentIntent is stamped on it, and the method is one that settles later."""
    return (inv.get("status") in _PAYABLE and bool(inv.get("stripe_payment_intent_id"))
            and (inv.get("payment_method") or "") in payments_panel.ASYNC_METHODS)


def _load_payable_invoice(invoice_id: str) -> dict:
    inv = db.get_invoice_any_tenant(invoice_id)
    if not inv or inv.get("status") in ("cancelled", "void", "draft"):
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@router.get("/pay/{invoice_id}", response_class=HTMLResponse,
            dependencies=[Depends(rate_limit("invoice_pay_page", 30, 60))])
def pay_page(invoice_id: str):
    """The customer's landing from the invoice email. First open stamps viewed_at
    (and fires the opt-in 'invoice viewed' push) — Web analytics for free."""
    from app import invoice_config
    inv = _load_payable_invoice(invoice_id)
    tenant_id = inv["tenant_id"]
    if db.set_invoice_viewed(invoice_id, tenant_id):
        trace.emit(tenant_id, "invoice_viewed", contact=inv.get("customer_email"),
                   detail={"invoice_ref": inv.get("invoice_ref")})
        try:
            # Always told, not always announced — the mirror of the quote-viewed
            # path (Kris 2026-09-04). The toggle used to wrap this call and take
            # the feed row with it; notify_prefs owns the decision now.
            who = inv.get("customer_name") or inv.get("customer_email") or "Customer"
            push.notify(tenant_id,
                        title=f"{who} viewed invoice {inv.get('invoice_ref')}",
                        body=f"£{float(inv.get('total') or 0):,.2f} — not paid yet",
                        url=f"/app/invoices/{invoice_id}",
                        tag=f"invoice-viewed-{invoice_id}", kind="invoice_viewed",
                        contact=inv.get("customer_email"),
                        contact_name=inv.get("customer_name"))
        except Exception:
            log.warning("invoice viewed push failed", exc_info=True)
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id))
    try:
        lines = db.list_invoice_lines(invoice_id, tenant_id)
    except Exception:
        lines = []   # the page still stands on the invoice's own total
    pay_now = inv.get("status") in _PAYABLE and tenant_can_take_payments(tenant_id)
    pending = _payment_pending(inv)
    others: list[dict] = []
    if pay_now and not pending and inv.get("customer_email"):
        try:
            others = [o for o in db.open_invoices_for_customer(
                tenant_id, inv["customer_email"], exclude_id=invoice_id, limit=BATCH_MAX - 1)
                if _balance(o) > 0 and not _payment_pending(o)]
        except Exception:
            others = []          # the offer is a bonus; the invoice page never waits on it
    return HTMLResponse(_pay_page_html(
        inv, s, paid=inv.get("status") == "paid",
        bank=None if pending else invoice_config.bank_details(s),
        pay_now=pay_now and not pending, lines=lines, others=others, pending=pending))


@router.post("/pay/{invoice_id}/checkout",
             dependencies=[Depends(rate_limit("invoice_checkout", 10, 60))])
def pay_checkout(invoice_id: str):
    """Mint a fresh Checkout Session on the tenant's connected account and hand the
    customer to Stripe. Fresh per click — sessions expire in 24h, our link doesn't."""
    inv = _load_payable_invoice(invoice_id)
    tenant_id = inv["tenant_id"]
    if inv.get("status") == "paid":
        return RedirectResponse(f"/pay/{invoice_id}", status_code=303)
    acct = db.get_stripe_account_id(tenant_id) if connect_configured() else None
    if not acct:
        return RedirectResponse(f"/pay/{invoice_id}", status_code=303)
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id))
    ref = inv.get("invoice_ref") or "Invoice"
    biz = s.get("business_name") or "Invoice"
    balance = float(inv.get("total") or 0) - float(inv.get("amount_paid") or 0)
    if balance <= 0:
        return RedirectResponse(f"/pay/{invoice_id}", status_code=303)
    meta = {"kind": "invoice", "tenant_id": tenant_id,
            "invoice_id": invoice_id, "invoice_ref": ref}
    try:
        session = _checkout_session(
            acct, line_items=[_line_item(ref, biz, balance)], meta=meta,
            customer_email=inv.get("customer_email"),
            success_url=f"{settings.app_base_url.rstrip('/')}/pay/{invoice_id}/done",
            cancel_url=payment_url(invoice_id),
            method_types=payments_panel.checkout_payment_method_types(
                s.get("bank_methods_min_gbp"), balance))
    except Exception:
        log.warning("checkout session create failed (invoice=%s)", invoice_id,
                    exc_info=True)
        raise HTTPException(status_code=502, detail="Payment provider unavailable")
    trace.emit(tenant_id, "invoice_checkout", contact=inv.get("customer_email"),
               detail={"invoice_ref": ref, "amount": balance})
    return RedirectResponse(session.url, status_code=303)


def _line_item(ref: str, biz: str, amount_gbp: float) -> dict:
    return {"quantity": 1, "price_data": {
        "currency": "gbp", "unit_amount": int(round(amount_gbp * 100)),
        "product_data": {"name": f"{ref} — {biz}"}}}


def _checkout_session(acct: str, *, line_items: list[dict], meta: dict,
                      customer_email: str | None, success_url: str, cancel_url: str,
                      method_types: list[str] | None):
    """A Checkout Session on the tenant's connected account (a direct charge: the
    money lands in their Stripe, BLAM8 takes no application fee). method_types None
    lets Stripe offer every method the account has switched on — that is how Bacs
    Direct Debit and Pay by Bank reach the page (plan §0.6); ['card'] keeps it to
    cards and their wallets."""
    stripe.api_key = settings.stripe_secret_key
    params: dict = dict(
        mode="payment", line_items=line_items,
        payment_intent_data={"metadata": meta}, metadata=meta,
        customer_email=(customer_email or None),
        success_url=success_url, cancel_url=cancel_url,
        stripe_account=acct)
    if method_types is not None:
        params["payment_method_types"] = method_types
    return stripe.checkout.Session.create(**params)


@router.post("/pay/{invoice_id}/checkout-all",
             dependencies=[Depends(rate_limit("invoice_checkout_all", 10, 60))])
def pay_checkout_all(invoice_id: str):
    """One checkout for this invoice AND the same customer's other open invoices with
    the business (plan §0.5). One line item per invoice, metadata.kind='invoice_batch'
    with the ids; the webhook settles each on the same PaymentIntent."""
    inv = _load_payable_invoice(invoice_id)
    tenant_id = inv["tenant_id"]
    acct = db.get_stripe_account_id(tenant_id) if connect_configured() else None
    if not acct or inv.get("status") == "paid" or _payment_pending(inv):
        return RedirectResponse(f"/pay/{invoice_id}", status_code=303)
    try:
        others = db.open_invoices_for_customer(
            tenant_id, inv.get("customer_email") or "", exclude_id=invoice_id,
            limit=BATCH_MAX - 1)
    except Exception:
        others = []
    batch = [i for i in [inv] + list(others) if _balance(i) > 0 and not _payment_pending(i)]
    if len(batch) < 2:
        return RedirectResponse(f"/pay/{invoice_id}/checkout", status_code=307)
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id))
    biz = s.get("business_name") or "Invoice"
    refs = [str(i.get("invoice_ref") or "Invoice") for i in batch]
    total = round(sum(_balance(i) for i in batch), 2)
    meta = {"kind": "invoice_batch", "tenant_id": tenant_id,
            "invoice_ids": ",".join(i["id"] for i in batch),
            "invoice_refs": ", ".join(refs)[:450]}
    try:
        session = _checkout_session(
            acct, line_items=[_line_item(r, biz, _balance(i)) for r, i in zip(refs, batch)],
            meta=meta, customer_email=inv.get("customer_email"),
            success_url=f"{settings.app_base_url.rstrip('/')}/pay/{invoice_id}/done",
            cancel_url=payment_url(invoice_id),
            method_types=payments_panel.checkout_payment_method_types(
                s.get("bank_methods_min_gbp"), total))
    except Exception:
        log.warning("batch checkout session create failed (invoice=%s)", invoice_id,
                    exc_info=True)
        raise HTTPException(status_code=502, detail="Payment provider unavailable")
    trace.emit(tenant_id, "invoice_checkout", contact=inv.get("customer_email"),
               detail={"invoice_ref": meta["invoice_refs"], "amount": total,
                       "batch": len(batch)})
    return RedirectResponse(session.url, status_code=303)


@router.get("/pay/{invoice_id}/done", response_class=HTMLResponse)
def pay_done(invoice_id: str):
    """Stripe's success_url. The webhook is the source of truth — this page just
    reads current state (which the webhook usually beat us to)."""
    from app import invoice_config
    inv = _load_payable_invoice(invoice_id)
    s = invoice_config.settings_for(db.get_invoice_settings(inv["tenant_id"]))
    if inv.get("status") == "paid":
        return HTMLResponse(_pay_page_html(inv, s, paid=True, bank=None, pay_now=False))
    ref = inv.get("invoice_ref") or "Invoice"
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{ref}</title></head>"
        f"<body style='font-family:system-ui;max-width:26rem;margin:3rem auto'>"
        f"<p>✓ Payment received — {ref} will show as paid shortly.</p></body></html>")


# ── the owner's Card payments page (docs/plan-payments-panel.md §1) ─────────
def _owner_account(ctx: TenantCtx) -> str | None:
    return (db.get_stripe_account_id(ctx.tenant_id, client=ctx.client)
            if connect_configured() else None)


@router.get("/app/payments", response_class=HTMLResponse)
def payments_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """Balance, next payout, today vs yesterday, the last 30 days with Stripe's fees,
    every payment linked to its invoice, and refunds — read from the connected
    account, no Stripe login. Not connected → the connect card."""
    from app.dashboard import _ctx
    acct = _owner_account(ctx)
    data = None
    if acct:
        data = payments_panel.load(acct, force=request.query_params.get("refresh") == "1")
    return templates.TemplateResponse(request, "app/payments.html", _ctx(
        "payments", ctx, acct=acct, data=data, stripe_available=connect_configured(),
        msg=request.query_params.get("msg"), pretty=_pretty_when))


def _pretty_when(dt) -> str:
    """'18th Sep 26' from an aware datetime, via the house date helper."""
    from app.dates import pretty_date
    if not dt:
        return "—"
    try:
        return pretty_date(dt.astimezone(payments_panel._LONDON).date().isoformat())
    except Exception:
        return str(dt)[:10]


@router.post("/app/payments/refund", dependencies=[Depends(require_csrf)])
def payments_refund(invoice_id: str = Form(""), payment_intent: str = Form(""),
                    amount: str = Form(""), reason: str = Form(""), back: str = Form(""),
                    ctx: TenantCtx = Depends(require_onboarded)):
    """Refund a card payment from BLAM8 — full (blank amount) or partial — on the
    tenant's own Stripe account, then run the refund lifecycle so it lands on the
    invoice, the CRM timeline and the owner's phone. The charge.refunded event Stripe
    then sends is deduped by the ledger key (plan §0.4)."""
    _guards.require_admin(ctx)   # security review 2026-09 (H1/H2)
    dest = back if back.startswith("/app/") else "/app/payments"
    acct = _owner_account(ctx)
    if not acct:
        return RedirectResponse(f"{dest}?msg=refund_unavailable", status_code=303)
    inv = db.get_invoice(invoice_id, ctx.tenant_id, client=ctx.client) if invoice_id else None
    pi = (payment_intent or (inv or {}).get("stripe_payment_intent_id") or "").strip()
    if not pi.startswith("pi_"):
        return RedirectResponse(f"{dest}?msg=refund_nopayment", status_code=303)
    amt: float | None = None
    raw = (amount or "").strip().replace("£", "").replace(",", "")
    if raw:
        try:
            amt = round(float(raw), 2)
        except ValueError:
            return RedirectResponse(f"{dest}?msg=refund_badamount", status_code=303)
        if amt <= 0:
            return RedirectResponse(f"{dest}?msg=refund_badamount", status_code=303)
    meta = {"kind": "invoice_refund", "tenant_id": ctx.tenant_id,
            "invoice_id": (inv or {}).get("id") or "", "origin": "blam8"}
    try:
        rf, charge = payments_panel.refund(acct, pi, amount_gbp=amt, reason=reason,
                                           metadata=meta)
    except Exception:
        log.warning("refund failed (tenant=%s pi=%s)", ctx.tenant_id, pi, exc_info=True)
        return RedirectResponse(f"{dest}?msg=refund_failed", status_code=303)
    payments_panel.invalidate(acct)
    trace.emit(ctx.tenant_id, "invoice_refund", detail={
        "payment_intent": pi, "amount": rf.get("amount"), "invoice_id": meta["invoice_id"]})
    if inv:
        invoice_refund_followup(
            ctx.tenant_id, inv, charge_id=charge.get("id") or rf.get("charge"),
            amount_refunded_pence=int(charge.get("amount_refunded") or rf.get("amount") or 0),
            refunded_now_pence=int(rf.get("amount") or 0), reason=reason,
            via="refunded by you", client=ctx.client)
    return RedirectResponse(f"{dest}?msg=refunded", status_code=303)


@router.post("/app/payments/methods", dependencies=[Depends(require_csrf)])
def payments_methods(bank_methods_min_gbp: str = Form(""),
                     invoice_snapshot: str = Form(""),
                     ctx: TenantCtx = Depends(require_onboarded)):
    """'Offer bank payment on invoices from £N' (plan §0.6) — kept in
    invoice_settings.extras beside the other jsonb prefs. Blank clears it."""
    raw = (bank_methods_min_gbp or "").strip().replace("£", "").replace(",", "")
    value: float | None
    if not raw:
        value = None
    else:
        try:
            value = max(0.0, round(float(raw), 2))
        except ValueError:
            return RedirectResponse("/app/invoices/settings?msg=methods_badamount",
                                    status_code=303)
    import json
    from fastapi import HTTPException
    try:
        if not isinstance(invoice_snapshot, str) or len(invoice_snapshot)>120000:
            raise ValueError()
        expected = json.loads(invoice_snapshot)
        if (not isinstance(expected, dict) or set(expected)!={"invoice", "quote_auto_draft"}
                or not isinstance(expected["invoice"], dict)
                or not isinstance(expected["invoice"].get("extras") or {}, dict)
                or not isinstance(expected["quote_auto_draft"], (str, type(None)))):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(409, "Reload invoice settings before saving this form.") from None
    existing = expected["invoice"]
    extras = dict(existing.get("extras") or {})
    if value is None:
        extras.pop("bank_methods_min_gbp", None)
    else:
        extras["bank_methods_min_gbp"] = value
    try:
        db.save_invoice_settings_checked(ctx.tenant_id, expected, {"extras": extras}, None, client=ctx.client)
    except db.InvoiceSettingsStale:
        raise HTTPException(409, "Invoice settings changed. Reload before saving; this form saved nothing.") from None
    except Exception:
        raise HTTPException(503, "The save could not be confirmed. Reload to check before trying again.") from None
    return RedirectResponse("/app/invoices/settings?msg=methods_saved", status_code=303)


# ── the paid-invoice lifecycle (shared by webhook + manual + concierge) ──────
def invoice_paid_followup(tenant_id: str, inv: dict, *, via: str) -> None:
    """Everything that should happen once an invoice is settled, whoever settled it:
    owner push, agent-log entry, CRM activity on the contact timeline, and — when a
    deal is linked — the Concierge nudge proposing the move to Won (confirm-first:
    the nudge seeds the conversation; the owner's reply mints the proposal card).
    Best-effort throughout: a side-effect failure never unwinds the payment."""
    ref = inv.get("invoice_ref") or "invoice"
    who = inv.get("customer_name") or inv.get("customer_email") or "customer"
    total = float(inv.get("total") or 0)
    try:                                   # Packages: the wallet's rows read "paid"
        from app import wallet
        wallet.on_invoice_paid(tenant_id, inv)
    except Exception:
        log.warning("wallet paid flag failed (%s)", inv.get("id"), exc_info=True)
    try:
        push.notify(tenant_id, title=f"Invoice {ref} paid (£{total:,.2f})",
                    body=f"{who} · {via}", url=f"/app/invoices/{inv['id']}",
                    tag=f"invoice-paid-{inv['id']}", kind="invoice_paid",
                    contact=inv.get("customer_email"),
                    contact_name=inv.get("customer_name"))
    except Exception:
        log.warning("invoice paid push failed", exc_info=True)
    try:
        db.log_agent_event(tenant_id, "invoice_paid",
                           f"Invoice {ref} paid — £{total:,.2f} from {who} ({via})",
                           contact=inv.get("customer_email"))
    except Exception:
        log.warning("invoice paid agent event failed", exc_info=True)
    try:
        contact = (db.crm_contact_by_email(tenant_id, inv.get("customer_email") or "")
                   if inv.get("customer_email") else None)
        db.crm_insert_activity(
            tenant_id, contact_id=(contact or {}).get("id") or inv.get("contact_id"),
            company_id=(contact or {}).get("company_id"),
            activity_type="invoice_paid", source_id=inv["id"], direction="inbound",
            subject=f"Invoice {ref} paid",
            summary=f"£{total:,.2f} received ({via})",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            deal_id=inv.get("deal_id"))
    except Exception:
        log.warning("invoice paid CRM activity failed", exc_info=True)
    if inv.get("deal_id"):
        try:
            db.add_concierge_message(
                tenant_id, "assistant",
                f"Invoice {ref} (£{total:,.2f}) from {who} is paid — shall I move "
                f"their deal to Won? Just say \"move {who}'s deal to won\" and I'll "
                f"set it up for you to confirm. I can also update the deal's value "
                f"from its paid invoices — ask any time.")
        except Exception:
            log.warning("deal-won concierge nudge failed", exc_info=True)


def invoice_refund_followup(tenant_id: str, inv: dict, *, charge_id: str,
                            amount_refunded_pence: int, refunded_now_pence: int,
                            reason: str, via: str, client=None) -> bool:
    """Everything that follows a refund, whoever issued it (our route or Stripe's own
    dashboard, which reaches us as charge.refunded): the invoice row, an owner push,
    the agent log and the CRM timeline. Keyed on the charge's absolute amount_refunded
    (payments_panel.refund_key) so the two doors converge: whichever arrives second
    finds the key in the invoice's notes and does nothing. Returns True when applied."""
    from app.dates import pretty_date
    ref = inv.get("invoice_ref") or "invoice"
    who = inv.get("customer_name") or inv.get("customer_email") or "customer"
    now_gbp = round(int(refunded_now_pence or 0) / 100.0, 2)
    key = payments_panel.refund_key(charge_id, amount_refunded_pence)
    why = (reason or "").strip()
    line = (f"Refunded £{now_gbp:,.2f} on {pretty_date(datetime.now(timezone.utc).date().isoformat())}"
            + (f" — {why}" if why else "") + f" ({via})")
    row = db.record_invoice_refund(inv["id"], tenant_id,
                                   amount_refunded_total=int(amount_refunded_pence or 0) / 100.0,
                                   key=key, note=line, client=client)
    if not row:
        return False
    full = row.get("status") == "refunded"
    title = f"{'Refund' if not full else 'Full refund'} £{now_gbp:,.2f} on {ref}"
    try:
        push.notify(tenant_id, title=title, body=f"{who} · {via}",
                    url=f"/app/invoices/{inv['id']}", tag=f"invoice-refund-{charge_id}",
                    kind="invoice_refunded", contact=inv.get("customer_email"),
                    contact_name=inv.get("customer_name"))
    except Exception:
        log.warning("refund push failed", exc_info=True)
    try:
        db.log_agent_event(tenant_id, "invoice_refunded",
                           f"{title} — {who}" + (f": {why}" if why else "") + f" ({via})",
                           contact=inv.get("customer_email"))
    except Exception:
        log.warning("refund agent event failed", exc_info=True)
    try:
        contact = (db.crm_contact_by_email(tenant_id, inv.get("customer_email") or "")
                   if inv.get("customer_email") else None)
        db.crm_insert_activity(
            tenant_id, contact_id=(contact or {}).get("id") or inv.get("contact_id"),
            company_id=(contact or {}).get("company_id"),
            activity_type="invoice_refunded", source_id=f"{charge_id}:{int(amount_refunded_pence or 0)}",
            direction="outbound", subject=title,
            summary=f"£{now_gbp:,.2f} refunded ({via})" + (f" — {why}" if why else ""),
            occurred_at=datetime.now(timezone.utc).isoformat(),
            deal_id=inv.get("deal_id"))
    except Exception:
        log.warning("refund CRM activity failed", exc_info=True)
    return True


def _announce(tenant_id: str, inv: dict, *, kind: str, title: str, body: str,
              activity_summary: str, source_id: str) -> None:
    """A push + agent-log line + CRM activity for a payment event that changes no
    money on the invoice: a bank payment in progress, a dispute opened."""
    who = inv.get("customer_name") or inv.get("customer_email") or "customer"
    try:
        push.notify(tenant_id, title=title, body=f"{who} · {body}",
                    url=f"/app/invoices/{inv['id']}", tag=f"{kind}-{inv['id']}",
                    kind=kind, contact=inv.get("customer_email"),
                    contact_name=inv.get("customer_name"))
    except Exception:
        log.warning("%s push failed", kind, exc_info=True)
    try:
        db.log_agent_event(tenant_id, kind, f"{title} — {who}: {body}",
                           contact=inv.get("customer_email"))
    except Exception:
        log.warning("%s agent event failed", kind, exc_info=True)
    try:
        contact = (db.crm_contact_by_email(tenant_id, inv.get("customer_email") or "")
                   if inv.get("customer_email") else None)
        db.crm_insert_activity(
            tenant_id, contact_id=(contact or {}).get("id") or inv.get("contact_id"),
            company_id=(contact or {}).get("company_id"),
            activity_type=kind, source_id=source_id, direction="inbound",
            subject=title, summary=activity_summary,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            deal_id=inv.get("deal_id"))
    except Exception:
        log.warning("%s CRM activity failed", kind, exc_info=True)


# ── the Connect webhook ──────────────────────────────────────────────────────
@router.post("/webhooks/stripe",
             dependencies=[Depends(rate_limit("stripe_connect_webhook", 120, 60))])
async def stripe_connect_webhook(request: Request):
    """Connected-account events. Signature-verified, idempotent two ways: the
    stripe_events id wall (shared with the platform webhook — event ids are
    globally unique) AND mark_invoice_paid's status-guarded update."""
    if not settings.stripe_connect_webhook_secret:
        # Same treatment as the platform webhook's guard: 503 for Stripe's
        # delivery log, and a line here naming the missing setting (name
        # only, never a value).
        log.error("stripe connect webhook refused 503: Stripe Connect not configured, "
                  "missing STRIPE_CONNECT_WEBHOOK_SECRET")
        raise HTTPException(status_code=503, detail="Stripe Connect is not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.stripe_connect_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        # Same treatment as the platform webhook: 400 for Stripe's delivery
        # log, plus a line here saying which check failed and whether the
        # header was present (never the payload or the secret).
        log.error(
            "stripe connect webhook rejected 400: %s, stripe-signature header %s: %s",
            ("signature verification failed"
             if isinstance(exc, stripe.error.SignatureVerificationError)
             else "unparseable payload"),
            "present" if sig else "absent", exc)
        raise HTTPException(status_code=400, detail="Invalid signature")

    if not db.stripe_record_event(event["id"], event["type"]):
        return PlainTextResponse("duplicate", status_code=200)

    event_type = event["type"]
    obj = (event.get("data") or {}).get("object") or {}
    account = event.get("account")

    if event_type == "charge.dispute.created":
        # A Dispute carries no invoice metadata of its own; its charge does. One
        # read on the connected account, best-effort (plan §0.8).
        _hook_dispute(obj, account)
        return PlainTextResponse("ok", status_code=200)

    meta = obj.get("metadata") or {}
    kind = meta.get("kind")
    if kind not in ("invoice", "invoice_batch"):
        return PlainTextResponse("ignored", status_code=200)
    tenant_id = meta.get("tenant_id")
    invoice_ids = ([meta.get("invoice_id")] if kind == "invoice"
                   else [i for i in (meta.get("invoice_ids") or "").split(",") if i])
    invoice_ids = [i for i in invoice_ids if i]
    if not (tenant_id and invoice_ids):
        return PlainTextResponse("unkeyed", status_code=200)
    # The event must come from THIS business's own connected account (security
    # review 2026-09, M9): metadata is whatever the payer's side wrote, so a
    # tenant with their own Stripe account could otherwise mark another
    # business's invoice paid with a genuinely signed event for £0.30.
    import hmac as _hmac
    stored = db.get_stripe_account_id(tenant_id) or ""
    if not (account and stored and _hmac.compare_digest(str(account), str(stored))):
        log.warning("stripe connect webhook: account %s is not tenant %s's (%s) — ignored",
                    account, tenant_id, stored or "none")
        trace.emit(tenant_id, "stripe_webhook", outcome="account_mismatch",
                   detail={"event_type": event_type, "account": account})
        return PlainTextResponse("account mismatch", status_code=200)
    refs = meta.get("invoice_refs") or meta.get("invoice_ref")

    trace.emit(tenant_id, "stripe_webhook", detail={
        "event_type": event_type, "invoice_id": invoice_ids[0], "batch": len(invoice_ids),
        "invoice_ref": refs, "account": account})

    if event_type == "payment_intent.succeeded":
        method = _method_from_intent(obj)
        via = ("paid online via Stripe" if len(invoice_ids) == 1
               else f"paid online via Stripe — {len(invoice_ids)} invoices in one payment")
        for invoice_id in invoice_ids:
            row = db.mark_invoice_paid(invoice_id, tenant_id, payment_method=method,
                                       stripe_payment_intent_id=obj.get("id"))
            if row:
                invoice_paid_followup(tenant_id, row, via=via)
    elif event_type == "payment_intent.processing":
        # A bank payment submitted, clearing over the next days (plan §0.7).
        method = _method_from_intent(obj)
        for invoice_id in invoice_ids:
            row = db.mark_invoice_payment_pending(
                invoice_id, tenant_id, payment_method=method,
                stripe_payment_intent_id=obj.get("id"))
            if row:
                _announce(tenant_id, row, kind="invoice_payment_pending",
                          title=f"{payments_panel.method_label(method)} payment started — "
                                f"{row.get('invoice_ref') or 'invoice'}",
                          body=f"£{float(row.get('total') or 0):,.2f} clearing, usually "
                               "within three working days",
                          activity_summary=f"Bank payment submitted "
                                           f"({payments_panel.method_label(method)})",
                          source_id=f"{obj.get('id')}:processing")
    elif event_type == "payment_intent.payment_failed":
        err = ((obj.get("last_payment_error") or {}).get("message") or "")[:200]
        label = refs or invoice_ids[0]
        db.log_agent_event(
            tenant_id, "invoice_payment_failed",
            f"Payment failed for {label}" + (f" — {err}" if err else ""),
            contact=(obj.get("receipt_email") or None))
        try:
            inv = db.get_invoice(invoice_ids[0], tenant_id)
            who = (inv or {}).get("customer_name") or (inv or {}).get("customer_email") or "customer"
            push.notify(tenant_id,
                        title=f"Payment failed — {label}",
                        body=f"{who}'s payment didn't go through",
                        url=f"/app/invoices/{invoice_ids[0]}",
                        tag=f"invoice-fail-{invoice_ids[0]}", kind="invoice_payment_failed",
                        contact=(inv or {}).get("customer_email"),
                        contact_name=(inv or {}).get("customer_name"))
        except Exception:
            log.warning("payment failed push failed", exc_info=True)
    elif event_type == "charge.refunded":
        # A refund — ours (already applied, the key dedupes) or one issued in Stripe's
        # own dashboard (plan §0.4). The Charge carries the PaymentIntent's metadata.
        _hook_refunded(obj, tenant_id, invoice_ids, refs)
    elif event_type == "invoice.payment_action_required":
        # 3DS challenge in flight — the customer completes it on Stripe's side;
        # we just leave a trace (already emitted above) and wait for the outcome.
        pass

    return PlainTextResponse("ok", status_code=200)


def _method_from_intent(obj: dict) -> str:
    """The payment method key off a PaymentIntent event: the confirmed method's
    type when Stripe has it, else the first offered type, else card."""
    try:
        pm = obj.get("payment_method")
        if isinstance(pm, dict) and pm.get("type"):
            return pm["type"]
        types = obj.get("payment_method_types") or ["card"]
        return types[0] or "card"
    except Exception:
        return "card"


def _hook_refunded(charge: dict, tenant_id: str, invoice_ids: list[str],
                   refs: str | None) -> None:
    total_refunded = int(charge.get("amount_refunded") or 0)
    if len(invoice_ids) == 1:
        inv = db.get_invoice(invoice_ids[0], tenant_id)
        if inv:
            # The amount refunded THIS time is the change since the last ledger line;
            # the notes hold the previous cumulative figure inside its key.
            previous = _last_refunded_pence(inv.get("notes") or "", charge.get("id") or "")
            invoice_refund_followup(
                tenant_id, inv, charge_id=charge.get("id") or "",
                amount_refunded_pence=total_refunded,
                refunded_now_pence=max(0, total_refunded - previous),
                reason="", via="refunded in Stripe")
        return
    # A refund on a payment that settled several invoices at once can't be split
    # without a decision only the owner can make: say so, loudly, once.
    try:
        inv = db.get_invoice(invoice_ids[0], tenant_id)
        if inv:
            _announce(tenant_id, inv, kind="invoice_refunded",
                      title=f"Refund £{total_refunded / 100:,.2f} on a payment covering "
                            f"{len(invoice_ids)} invoices",
                      body=f"{refs or ''} — update the invoices by hand",
                      activity_summary=f"Refund £{total_refunded / 100:,.2f} on a combined payment "
                                       f"({refs or len(invoice_ids)}); invoices not changed",
                      source_id=f"{charge.get('id')}:{total_refunded}")
    except Exception:
        log.warning("batch refund announce failed", exc_info=True)


def _last_refunded_pence(notes: str, charge_id: str) -> int:
    """The cumulative pence the invoice's notes already record for this charge (0 when
    none): the highest '[ch_…@N]' key present."""
    import re
    if not charge_id:
        return 0
    found = re.findall(r"\[" + re.escape(charge_id) + r"@(\d+)\]", notes)
    return max((int(n) for n in found), default=0)


def _hook_dispute(dispute: dict, account: str | None) -> None:
    charge_id = dispute.get("charge")
    if not (charge_id and account):
        return
    try:
        stripe.api_key = settings.stripe_secret_key
        charge = stripe.Charge.retrieve(charge_id, stripe_account=account)
        meta = dict(charge.get("metadata") or {})
    except Exception:
        log.warning("dispute charge read failed (%s)", charge_id, exc_info=True)
        return
    tenant_id = meta.get("tenant_id")
    invoice_id = meta.get("invoice_id") or (meta.get("invoice_ids") or "").split(",")[0]
    if not (tenant_id and invoice_id and meta.get("kind") in ("invoice", "invoice_batch")):
        return
    inv = db.get_invoice(invoice_id, tenant_id)
    if not inv:
        return
    amount = int(dispute.get("amount") or 0) / 100.0
    reason = (dispute.get("reason") or "").replace("_", " ")
    ref = meta.get("invoice_refs") or inv.get("invoice_ref") or "invoice"
    trace.emit(tenant_id, "stripe_webhook", detail={
        "event_type": "charge.dispute.created", "invoice_id": invoice_id,
        "invoice_ref": ref, "account": account})
    _announce(tenant_id, inv, kind="invoice_disputed",
              title=f"Payment disputed — {ref}",
              body=f"£{amount:,.2f}" + (f", {reason}" if reason else "")
                   + " — respond in your Stripe dashboard",
              activity_summary=f"Dispute opened for £{amount:,.2f}"
                               + (f" ({reason})" if reason else "")
                               + f" — https://dashboard.stripe.com/disputes/{dispute.get('id')}",
              source_id=str(dispute.get("id") or charge_id))
