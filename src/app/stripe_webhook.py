"""Stripe webhook — the source of truth for subscription state.

Stripe POSTs every billing event to this endpoint (the URL is registered in
Dashboard → Developers → Webhooks). We verify the signature, dedupe the event
by id, and translate the handful of subscription-lifecycle events we care
about into writes on `tenants.subscription_status` via
`app.billing.sync_subscription()`.

Public endpoint (called by Stripe, not a logged-in user) — same pattern as
the Microsoft Graph webhook at src/app/webhook.py:
  * No CSRF (server-to-server).
  * Rate-limited per-IP.
  * Raw body MUST be passed to stripe.Webhook.construct_event() (Stripe signs
    the bytes, not the parsed JSON — re-encoding loses the signature).
"""
from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app import billing, db, partners
from app.config import settings
from app.security import rate_limit


router = APIRouter(tags=["stripe"])


# Events we act on. Anything else is acknowledged 200 and ignored — Stripe
# stops retrying as soon as we 2xx. (Listing them so review can see exactly
# what surface we react to.)
_HANDLED = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    # Assets OS deposits (docs/os/assets.md §11) — a NEW branch keyed on
    # metadata.kind=='deposit'; a PaymentIntent/Charge WITHOUT that marker flows
    # exactly as before (subscriptions untouched, and an un-tagged charge.refunded
    # stays ignored). See _route_deposit_event.
    "payment_intent.amount_capturable_updated",
    "payment_intent.succeeded",
    "payment_intent.canceled",
    "payment_intent.payment_failed",
    "charge.refunded",
}


# Deposit PaymentIntent/Charge event → the unit_deposits status it lands the row on
# (docs/os/assets.md §11). Keyed on metadata.deposit_id / external_ref (the PI id).
_DEPOSIT_EVENT_STATUS = {
    "payment_intent.amount_capturable_updated": "held",     # manual-capture auth authorised
    "payment_intent.succeeded": "paid",                     # captured / charge cleared
    "payment_intent.canceled": "released",                  # hold voided
    "payment_intent.payment_failed": "failed",
    "charge.refunded": "refunded",
}


def _route_deposit_event(event_type: str, obj: dict) -> bool:
    """Route an Assets deposit PaymentIntent/Charge to db.set_deposit_status.

    Returns True iff this was a deposit event (metadata.kind=='deposit') and we handled it —
    the caller then SKIPS the subscription path. Anything without that marker returns False so
    the existing subscription/invoice handling runs unchanged (no regression). Keyed by
    metadata.deposit_id (preferred) else external_ref = the PaymentIntent id."""
    # For charge.refunded the object is a Charge; its metadata is copied from the PI, and the
    # PI id is on charge.payment_intent. For payment_intent.* the object IS the PaymentIntent.
    meta = obj.get("metadata") or {}
    if meta.get("kind") != "deposit":
        return False
    status = _DEPOSIT_EVENT_STATUS.get(event_type)
    if not status:
        return False
    tenant_id = meta.get("tenant_id")
    deposit_id = meta.get("deposit_id")
    if not tenant_id:
        return True  # a deposit event we can't key — acknowledged, nothing to write
    pi_id = obj.get("payment_intent") or obj.get("id")
    if deposit_id:
        db.set_deposit_status(deposit_id, tenant_id, status, external_ref=pi_id)
    elif pi_id:
        db.set_deposit_status_by_ref(tenant_id, pi_id, status)
    return True


@router.post("/stripe/webhook",
             dependencies=[Depends(rate_limit("stripe_webhook", 120, 60))])
async def stripe_webhook(request: Request):
    if not billing.is_configured():
        # Webhook URL hit before keys are configured — refuse loudly so the
        # operator notices in the Stripe Dashboard delivery log rather than
        # silently dropping events. Say which setting is missing in our own
        # logs too (names only, never values).
        missing = [name for name, value in (
            ("STRIPE_SECRET_KEY", settings.stripe_secret_key),
            ("STRIPE_WEBHOOK_SECRET", settings.stripe_webhook_secret),
        ) if not value]
        logging.getLogger("blautom8.stripe").error(
            "stripe webhook refused 503: billing not configured, missing %s",
            ", ".join(missing) or "unknown")
        raise HTTPException(status_code=503, detail="Stripe billing is not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        # Bad payload OR forged signature — refuse with 400 so the sender
        # sees the rejection. Stripe never expects 4xx for a real event,
        # so this also lights up the Dashboard if our secret rotates and
        # we forget to update it on Render. Logged here too: which check
        # failed and whether the header was there (never the payload or
        # the secret).
        logging.getLogger("blautom8.stripe").error(
            "stripe webhook rejected 400: %s, stripe-signature header %s: %s",
            ("signature verification failed"
             if isinstance(exc, stripe.error.SignatureVerificationError)
             else "unparseable payload"),
            "present" if sig else "absent", exc)
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Idempotency wall — Stripe retries up to 3 days, and the same event id
    # can arrive twice. The first arrival inserts; the duplicate is dropped
    # silently and we 200 (Stripe stops retrying).
    if not db.stripe_record_event(event["id"], event["type"]):
        return PlainTextResponse("duplicate", status_code=200)

    event_type = event["type"]
    if event_type not in _HANDLED:
        # Acknowledge so Stripe stops retrying, but don't act — we only
        # subscribe to the events listed above. Listing all events in the
        # Dashboard endpoint is a one-line change there.
        return PlainTextResponse("ignored", status_code=200)

    obj = (event.get("data") or {}).get("object") or {}

    # Assets deposits (docs/os/assets.md §11): a PaymentIntent/Charge tagged
    # metadata.kind=='deposit' is a NEW branch — route it and STOP, so it never touches
    # the subscription path. An event without that marker returns False and flows exactly
    # as today (subscriptions/invoices unchanged; an un-tagged charge.refunded stays a no-op).
    if _route_deposit_event(event_type, obj):
        return PlainTextResponse("ok", status_code=200)

    if event_type == "checkout.session.completed":
        # An email top-up is a one-off payment, not a subscription — it never
        # reaches the subscription.* handlers below, so the credit is granted
        # here. Idempotent on the payment intent, because Stripe retries and the
        # /billing/success page applies the same session to close the race.
        try:
            if billing.credit_topup_from_session(obj) is not None:
                return PlainTextResponse("ok", status_code=200)
        except Exception:
            logging.getLogger("blautom8.stripe").exception("top-up credit failed")

        # Self-serve first-Checkout: link the brand-new Stripe Customer to the
        # tenant. client_reference_id is the tenant_id we passed when creating
        # the Session (billing.create_checkout_session). The customer.
        # subscription.created event that follows will fill in the rest.
        tenant_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        if tenant_id and customer_id:
            existing = db.stripe_get_customer_id(tenant_id)
            if not existing:
                db.stripe_set_customer(tenant_id, customer_id, billing_provider="self_serve")

    elif event_type in ("customer.subscription.created",
                        "customer.subscription.updated"):
        billing.sync_subscription(obj)

    elif event_type == "customer.subscription.deleted":
        billing.mark_canceled(obj)

    elif event_type == "invoice.paid":
        # An invoice paying through means the subscription is active. The
        # subscription.* events handle the full state mirror; here we just
        # flip status to 'active' as a belt-and-braces guard against an
        # invoice arriving before the subscription update (rare but seen).
        sub_id = obj.get("subscription")
        if sub_id:
            tenant_id = db.stripe_get_tenant_id_by_subscription(sub_id)
            if tenant_id:
                db.stripe_set_subscription_status(tenant_id, "active")
                # Partner commission: money EARNS only here — on a collected
                # payment (never from catalog estimates). Idempotent per
                # invoice; the 90-day attribution window is enforced inside.
                # Best-effort: a ledger failure must never 4xx the webhook.
                try:
                    partners.record_commission_for_invoice(tenant_id, obj)
                except Exception:
                    logging.getLogger("blautom8.stripe").exception(
                        "partner commission failed: event=%s invoice=%s tenant=%s",
                        event.get("id"), obj.get("id"), tenant_id)

    elif event_type == "invoice.payment_failed":
        sub_id = obj.get("subscription")
        if sub_id:
            tenant_id = db.stripe_get_tenant_id_by_subscription(sub_id)
            if tenant_id:
                db.stripe_set_subscription_status(tenant_id, "past_due")

    elif event_type == "charge.refunded":
        # A refunded SUBSCRIPTION charge (deposit-tagged ones were routed and
        # returned above) claws back the partner commission automatically —
        # clause 4.7, applied the moment Stripe tells us. Best-effort: a
        # ledger failure must never 4xx the webhook.
        try:
            partners.record_refund_for_charge(obj)
        except Exception:
            logging.getLogger("blautom8.stripe").exception(
                "partner clawback failed: event=%s charge=%s invoice=%s",
                event.get("id"), obj.get("id"), obj.get("invoice"))

    return PlainTextResponse("ok", status_code=200)
