"""Assets OS — the deposit rail over the reused Stripe billing (docs/os/assets.md §11/§14).

Phase B. A hire's deposit is a Stripe payment, minted per booking, its link dropped into the
confirmation draft; the webhook (src/app/stripe_webhook.py) flips unit_deposits.status as the money
moves. We do NOT re-init Stripe — app.billing already set `stripe.api_key` module-scoped and owns
`ensure_stripe_customer`/`is_configured`; this module is the deposit-specific PaymentIntent
wrappers only.

Auth-vs-charge (§14 — the hold-duration risk). A Stripe manual-capture authorisation lapses
after ~7 days, so it can't cover a long hire:
  * hire ≤ AUTH_MAX_DAYS (5) → a manual-capture PaymentIntent — an authorised HOLD (no money
    moves; captured on damage, voided on clean return). method='stripe_hold'.
  * hire >  AUTH_MAX_DAYS      → a straight CHARGE (immediate-capture) — money moves and is
    refunded on clean return, so nothing depends on the auth window surviving. method='stripe_charge'.

Both are minted as a Stripe-hosted Checkout Session (PCI-zero, a customer-facing URL to drop in
the draft) carrying payment_intent_data.metadata={"kind":"deposit", ...} so the webhook can ROUTE
the resulting PaymentIntent to the deposit handler. `not billing.is_configured()` → a clear result,
never a crash.
"""
from __future__ import annotations

import logging

import stripe

from app import billing
from app.config import settings

log = logging.getLogger("blautom8.assets_deposits")

# The hold-duration cutoff (§14). At/under this a manual-capture authorisation still covers the
# return; over it the auth would lapse before the unit comes back, so we charge-and-refund.
AUTH_MAX_DAYS = 5

METHOD_HOLD = "stripe_hold"
METHOD_CHARGE = "stripe_charge"


class DepositResult:
    """The outcome of minting a deposit intent. `url` is the customer-facing Checkout URL for
    the draft; `ok=False` + `reason` when Stripe isn't configured (the route surfaces it, never
    crashes). `method`/`external_ref` are persisted onto the deposit row by the caller."""

    def __init__(self, ok: bool, *, url: str | None = None, method: str | None = None,
                 external_ref: str | None = None, reason: str | None = None):
        self.ok = ok
        self.url = url
        self.method = method
        self.external_ref = external_ref
        self.reason = reason


def _use_hold(hire_days: int) -> bool:
    """True → a manual-capture authorisation (hold) covers this hire; False → charge-and-refund.
    A hire whose length is unknown (None/<=0) defaults to the SAFE side: a hold for short-looking
    hires only when we can prove it's short — unknown falls to charge (money-moves, never lapses)."""
    try:
        d = int(hire_days)
    except (TypeError, ValueError):
        return False
    return 0 < d <= AUTH_MAX_DAYS


def create_deposit_intent(tenant_id: str, booking: dict, amount, *, hire_days: int,
                          deposit_id: str | None = None,
                          success_url: str | None = None,
                          cancel_url: str | None = None) -> DepositResult:
    """Mint the Stripe deposit and return the customer-facing URL to drop in the draft.

    Chooses HOLD vs CHARGE by hire length (§14). Reuses billing.ensure_stripe_customer so the
    payment sits under the same Stripe Customer as the tenant's subscription. Attaches
    metadata.kind='deposit' (+ booking_id/tenant_id/deposit_id) to the PaymentIntent so the
    webhook routes it. Stripe not configured → ok=False, reason set (no crash)."""
    if not billing.is_configured():
        return DepositResult(False, reason="Stripe is not configured for this account")

    booking_id = str(booking.get("id"))
    contact = booking.get("contact")
    try:
        customer_id = billing.ensure_stripe_customer(tenant_id, contact)
    except Exception:
        log.warning("deposit: ensure_stripe_customer failed for tenant %s", tenant_id,
                    exc_info=True)
        return DepositResult(False, reason="Could not open a Stripe customer for this hire")

    hold = _use_hold(hire_days)
    method = METHOD_HOLD if hold else METHOD_CHARGE
    metadata = {
        "kind": "deposit",
        "booking_id": booking_id,
        "tenant_id": str(tenant_id),
        "deposit_id": str(deposit_id or ""),
    }
    amount_pence = int(round(float(amount) * 100))
    base = (settings.app_base_url or "").rstrip("/")
    success = success_url or f"{base}/app/assets/bookings/{booking_id}?msg=deposit_paid"
    cancel = cancel_url or f"{base}/app/assets/bookings/{booking_id}?msg=deposit_cancelled"

    pi_data: dict = {"metadata": metadata}
    if hold:
        # An authorised HOLD — money is not captured until we deduct; auto-voids after ~7d.
        pi_data["capture_method"] = "manual"

    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[{
            "price_data": {
                "currency": (booking.get("deposit_currency") or "gbp").lower(),
                "product_data": {"name": "Hire deposit"},
                "unit_amount": amount_pence,
            },
            "quantity": 1,
        }],
        payment_intent_data=pi_data,
        success_url=success,
        cancel_url=cancel,
        client_reference_id=booking_id,
        metadata=metadata,
    )
    # The PaymentIntent id is the external_ref the webhook keys on; on a fresh session it may be
    # null until the customer pays, so we store it when present and otherwise fall back to the
    # session id (the webhook also carries the PI id in the object it routes).
    external_ref = session.get("payment_intent") or session.get("id")
    return DepositResult(True, url=session.get("url"), method=method,
                         external_ref=external_ref)


def refund_or_release(deposit: dict) -> DepositResult:
    """Give the deposit back on a clean return. A HELD authorisation is VOIDED
    (PaymentIntent.cancel — no money ever moved → status 'released'); a CHARGE is REFUNDED
    (Refund.create → status 'refunded'). Stripe not configured → ok=False."""
    if not billing.is_configured():
        return DepositResult(False, reason="Stripe is not configured for this account")
    ref = deposit.get("external_ref")
    if not ref:
        return DepositResult(False, reason="This deposit has no Stripe payment to release")
    method = deposit.get("method") or METHOD_HOLD
    if method == METHOD_HOLD:
        stripe.PaymentIntent.cancel(ref)
        return DepositResult(True, external_ref=ref, method=method, reason="released")
    stripe.Refund.create(payment_intent=ref)
    return DepositResult(True, external_ref=ref, method=method, reason="refunded")


def deduct(deposit: dict, amount, *, damage_case_id: str | None = None) -> DepositResult:
    """Take `amount` from the deposit (Phase B: a MANUAL figure; the photo-diff auto-figure is
    Phase C — damage_case_id is accepted and passed through but no diff is computed here).

    HELD authorisation → capture exactly `amount` (PaymentIntent.capture(amount_to_capture=N));
    Stripe auto-voids the uncaptured remainder. CHARGE → the customer already paid the full
    deposit, so refund the DIFFERENCE (deposit − amount) and keep `amount`. Either way the row
    lands status='deducted' with deducted_amount=amount. Stripe not configured → ok=False."""
    if not billing.is_configured():
        return DepositResult(False, reason="Stripe is not configured for this account")
    ref = deposit.get("external_ref")
    if not ref:
        return DepositResult(False, reason="This deposit has no Stripe payment to deduct from")
    total = float(deposit.get("amount") or 0)
    take = max(0.0, min(float(amount), total))
    take_pence = int(round(take * 100))
    method = deposit.get("method") or METHOD_HOLD
    if method == METHOD_HOLD:
        # Capture only what we're keeping; the rest of the authorisation auto-voids.
        stripe.PaymentIntent.capture(ref, amount_to_capture=take_pence)
    else:
        # Already charged the full deposit — refund the customer the un-deducted remainder.
        refund_pence = int(round((total - take) * 100))
        if refund_pence > 0:
            stripe.Refund.create(payment_intent=ref, amount=refund_pence)
    return DepositResult(True, external_ref=ref, method=method, reason="deducted")
