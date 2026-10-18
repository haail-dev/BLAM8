"""Applying partner commission as subscription credit.

The money half of the credit funnel: take cleared commission off a partner's own next
BLAM8 invoice instead of sending them a bank transfer.

⚠️ **Not Stripe Connect.** Connect (src/app/payments.py) is how a TENANT collects from THEIR
customers — money into the tenant's own Stripe account. This is the opposite direction
and a different mechanism entirely: a **customer balance credit** on BLAM8's own billing,
which Stripe then applies automatically to the next subscription invoice. A credit here
is simply a smaller invoice, so there is no transfer, no KYC and nothing to onboard.

⚠️ **Stripe can never pay a customer out.** A balance credit can reduce an invoice to £0
and no further; surplus sits on the balance. That is why the withdrawal flow exists
(src/app/partner_settlement.py) and why this module deliberately credits *at most* one
month's subscription at a time rather than dumping the whole balance on the customer.

Gated on `settings.partner_credit_enabled`, which is OFF by default. Agreement v2.3 now
covers this (clauses 4.9–4.12: the election, how credit applies, withdrawals, retained
cover and VAT), so the remaining gate is the VAT TREATMENT itself — a credit is a discount
on our supply to them, cash is them supplying a service to us, and the moment a bill hits
£0 and money flows they have moved from discount to income. Clause 4.12 allocates
responsibility for that; it does not decide it. With the flag off, `apply_monthly_credit`
refuses and changes nothing.
"""
from __future__ import annotations

import logging

import stripe

from app import db, partner_settlement, partners
from app.config import settings

_log = logging.getLogger("blautom8.partner_credit")

# What a refusal means, so callers (and tests) can branch without string-matching prose.
SKIP_DISABLED = "disabled"
SKIP_NOT_CREDIT_MODE = "not_credit_mode"
SKIP_NO_TENANT = "no_tenant"
SKIP_NO_CUSTOMER = "no_customer"
SKIP_NOTHING_TO_CREDIT = "nothing_to_credit"


def _pence(gbp: float) -> int:
    return int(round(float(gbp) * 100))


def apply_monthly_credit(partner: dict) -> dict:
    """Credit this partner's cleared commission against their own subscription.

    Returns {"ok": bool, "reason": str|None, "amount": float, "payout_id": str|None}.
    Never raises for an expected condition — a partner on cash, without a tenant, or
    with nothing cleared is a normal skip, not an error.

    Order matters: the Stripe credit is created FIRST, and only then is the ledger
    settled. If Stripe fails we have changed nothing; if the ledger write failed after a
    successful credit we would have given away money with no record of why, which is the
    worse of the two failures to leave behind.
    """
    if not settings.partner_credit_enabled:
        return {"ok": False, "reason": SKIP_DISABLED, "amount": 0.0, "payout_id": None}
    if partner_settlement.normalize_mode(partner.get("settlement_mode")) != "credit":
        return {"ok": False, "reason": SKIP_NOT_CREDIT_MODE, "amount": 0.0, "payout_id": None}

    tenant_id = partner.get("credit_tenant_id")
    if not tenant_id:
        return {"ok": False, "reason": SKIP_NO_TENANT, "amount": 0.0, "payout_id": None}
    customer_id = db.stripe_get_customer_id(tenant_id)
    if not customer_id:
        # No Checkout has ever run for this tenant, so there is no customer to credit.
        # Deliberately NOT creating one here: a credit against a customer with no
        # subscription would sit unusable and look like the money vanished.
        return {"ok": False, "reason": SKIP_NO_CUSTOMER, "amount": 0.0, "payout_id": None}

    summary = partners.referral_summary(partner)
    amount = float(summary["settlement"]["credit_applied"])
    if amount <= 0:
        return {"ok": False, "reason": SKIP_NOTHING_TO_CREDIT, "amount": 0.0,
                "payout_id": None}

    payable = partners.payable_commissions(db.list_partner_commissions(partner["id"]))
    # allow_overshoot: take the row that crosses the bill rather than stopping short.
    # £19.80 commission rows against a £39 bill would otherwise credit only £19.80 and
    # leave 60p outstanding, making "refer two, yours is free" untrue. A credit bigger
    # than the invoice just carries the remainder to the next one.
    ids, settled = partner_settlement.select_for_amount(payable, amount,
                                                        allow_overshoot=True)
    if not ids or settled <= 0:
        return {"ok": False, "reason": SKIP_NOTHING_TO_CREDIT, "amount": 0.0,
                "payout_id": None}

    # NEGATIVE amount = a credit on Stripe's customer balance, applied automatically to
    # the next invoice. Idempotency key keyed on the settled rows so a retry of the same
    # settlement can never double-credit.
    stripe.Customer.create_balance_transaction(
        customer_id,
        amount=-_pence(settled),
        currency="gbp",
        description=f"BLAM8 referral commission — {len(ids)} commission(s)",
        metadata={"partner_id": str(partner["id"]), "kind": "referral_commission"},
        idempotency_key=f"blam8-partner-credit-{partner['id']}-{'-'.join(sorted(ids))}"[:255],
    )

    payout = db.create_partner_payout(
        partner["id"], settled, None, None,
        reference="subscription credit", method="credit")
    db.mark_commission_ids_paid(ids, payout["id"])
    _log.info("partner_credit: credited %.2f to tenant %s for partner %s",
              settled, tenant_id, partner["id"])
    return {"ok": True, "reason": None, "amount": settled, "payout_id": payout["id"]}
