"""Partner settlement — subscription credit first, then a withdrawal.

The second referral funnel: a tenant who refers OTHER businesses takes their
commission off their own bill rather than as a bank transfer. "Refer two, yours is
free" (2 × 20% of £99 = £39.60 against a £39 Starter) is a far better pitch than
"earn £19.80 a month", it removes a manual transfer per partner per month, and —
the part that actually matters while chasing the first ten tenants — cancelling then
costs them the discount, so it buys retention that cash payouts do not.

Pure maths, no db and no Stripe, so every rule below is testable on its own. The
wiring is built (8c29003): the settlement preference column, withdrawal records,
the partner routes, admin settlement and partner_credit.apply_monthly_credit. What
is NOT wired is anything that CALLS the monthly credit (no job or webhook), and it
is gated off by settings.partner_credit_enabled pending the VAT answer. See the
caveats at the bottom.

Three rules worth understanding before changing anything here:

1. **Stripe will never pay a customer out.** A credit can only reduce an invoice to
   £0, never past it. So surplus commission does not "just get paid eventually" — it
   piles up unusable (5 Pro referrals on a £39 bill accrues £60/month, ~£360 by month
   six) until the partner asks for it. That is why a withdrawal step exists at all.

2. **The buffer is for CLAWBACKS, not for securing the discount.** A refund reverses
   the commission on that payment; if the cash is already gone, it has to be chased.
   The user-facing story is friendlier and equally true — "we always keep your next
   month covered, so your service never lapses".

3. **A big partner is LESS risky than a small one.** A clawback auto-deducts from the
   next statement (agreement clause 4.7), so ongoing commission is itself collateral.
   Real exposure needs clawbacks AND earnings stopping together. The buffer therefore
   grows only gently with run-rate and is capped: sizing it to "every referral refunds
   at once" would trap hundreds of pounds against a near-zero risk.
"""
from __future__ import annotations

SETTLEMENT_MODES = ("cash", "credit")
DEFAULT_SETTLEMENT_MODE = "cash"       # what existing partners signed up to

# Below this, a withdrawal isn't worth anyone's admin — and it stops the surplus
# quietly accumulating, since anyone above it can clear their balance whenever.
MIN_WITHDRAWAL_GBP = 50.0

# Buffer = one month's subscription + this slice of the monthly commission run-rate…
BUFFER_COMMISSION_SHARE = 0.20
# …capped at this multiple of the subscription, so it stays bounded as a partner grows
# (see rule 3 — large partners are better collateralised, not worse).
BUFFER_CAP_MULTIPLE = 3.0


def _f(v, default: float = 0.0) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return default


def normalize_mode(mode: str | None) -> str:
    m = (mode or "").strip().lower()
    return m if m in SETTLEMENT_MODES else DEFAULT_SETTLEMENT_MODE


def clawback_buffer(subscription_gbp, monthly_commission_gbp) -> float:
    """Commission held back against future reversals.

    One month's subscription plus a 20% slice of the run-rate, capped at 3× the
    subscription. A partner with no subscription (a pure referrer, not a tenant) has
    nothing to protect a service on, so the floor is just the commission slice.
    """
    sub = max(_f(subscription_gbp), 0.0)
    run = max(_f(monthly_commission_gbp), 0.0)
    buffer = sub + run * BUFFER_COMMISSION_SHARE
    cap = sub * BUFFER_CAP_MULTIPLE
    return round(min(buffer, cap) if cap > 0 else run * BUFFER_COMMISSION_SHARE, 2)


def credit_applied(cleared_gbp, subscription_gbp) -> float:
    """How much of the cleared balance comes off this month's bill.

    Capped at the bill itself — this is the Stripe customer-balance limit expressed as
    a rule (rule 1): a credit cannot take an invoice below zero.
    """
    return round(min(max(_f(cleared_gbp), 0.0), max(_f(subscription_gbp), 0.0)), 2)


def available_to_withdraw(cleared_gbp, subscription_gbp, monthly_commission_gbp,
                          mode: str = DEFAULT_SETTLEMENT_MODE) -> float:
    """Cash a partner may request right now.

    On CREDIT, the month's bill is settled first and the buffer is retained, so only
    what is left over is withdrawable. On CASH, nothing is going to the bill, but the
    buffer still applies — the clawback risk is identical either way.
    """
    cleared = max(_f(cleared_gbp), 0.0)
    sub = max(_f(subscription_gbp), 0.0)
    buffer = clawback_buffer(sub, monthly_commission_gbp)
    spoken_for = buffer + (credit_applied(cleared, sub)
                           if normalize_mode(mode) == "credit" else 0.0)
    return round(max(cleared - spoken_for, 0.0), 2)


def blocked_reason(available_gbp, cleared_gbp, subscription_gbp,
                   monthly_commission_gbp, mode: str = DEFAULT_SETTLEMENT_MODE) -> str | None:
    """Why the withdraw button is disabled, in words a partner can act on. None when
    they can withdraw."""
    if _f(cleared_gbp) <= 0:
        return "Nothing cleared yet — commission becomes available about a month after we collect it."
    if normalize_mode(mode) == "credit" and _f(available_gbp) <= 0 and \
            credit_applied(cleared_gbp, subscription_gbp) > 0:
        return "It's all going to your subscription this month — refer more and the surplus becomes withdrawable."
    if _f(available_gbp) <= 0:
        return "Held as cover while your referrals are still inside the refund window."
    if _f(available_gbp) < MIN_WITHDRAWAL_GBP:
        return f"£{MIN_WITHDRAWAL_GBP:.0f} minimum — you're at £{_f(available_gbp):.2f}."
    return None


def clamp_request(requested_gbp, available_gbp) -> float:
    """What a submitted amount actually settles at.

    The UI is a slider from £0 to available, so a value outside that range means a
    hand-built request: clamp rather than trust it, and never pay more than is
    available. Below the minimum settles at 0 — the caller refuses it.
    """
    req = max(_f(requested_gbp), 0.0)
    avail = max(_f(available_gbp), 0.0)
    amount = min(req, avail)
    return amount if amount >= MIN_WITHDRAWAL_GBP else 0.0


def select_for_amount(commissions: list[dict], target,
                      allow_overshoot: bool = False) -> tuple[list[str], float]:
    """Which cleared commission rows settle an amount around `target`.

    Commission rows are discrete, so an arbitrary target rarely lands on an exact
    subset sum. Splitting a row would fork the ledger's one-row-per-collected-invoice
    invariant, so rows are taken whole and the settled figure lands near the target
    rather than on it. `allow_overshoot` decides which side:

      * **False (cash withdrawals).** Never pay out more than was asked for — a row
        that doesn't fit is skipped, and a smaller later row may still fit.
      * **True (subscription credit).** Take the row that crosses the target and stop.
        Without this, a £39 bill and £19.80 commission rows could only ever credit
        £19.80 — two referrals would leave 60p of bill outstanding and "refer two,
        yours is free" would be a lie. Overshooting is harmless here because a Stripe
        customer-balance credit larger than the invoice simply carries the remainder to
        the next one; nothing is stranded.

    Clawbacks (negative rows) are always included, per agreement clause 4.7: they must
    deduct from the very next statement. Including them first also lowers the running
    total, which lets more positive rows fit.

    Recomputed at payment time rather than stored on the request, so a clawback landing
    between "I'd like £120" and the operator paying it can never be missed.
    """
    tgt = max(_f(target), 0.0)
    negatives = [c for c in commissions if _f(c.get("amount")) < 0]
    positives = sorted((c for c in commissions if _f(c.get("amount")) > 0),
                       key=lambda c: c.get("earned_at") or "")
    ids = [c["id"] for c in negatives]
    total = round(sum(_f(c.get("amount")) for c in negatives), 2)
    for c in positives:
        amount = _f(c.get("amount"))
        if round(total + amount, 2) > tgt:
            if not allow_overshoot:
                continue      # skip rather than split; a smaller later row may fit
            ids.append(c["id"])
            total = round(total + amount, 2)
            break             # crossed the target — that's enough
        ids.append(c["id"])
        total = round(total + amount, 2)
    if total <= 0:
        # Net negative or nothing fitted: there is no payment to make, and the
        # clawbacks stay open so they net against the next earnings instead.
        return [], 0.0
    return ids, total


def view(*, cleared_gbp, subscription_gbp, monthly_commission_gbp,
         mode: str = DEFAULT_SETTLEMENT_MODE) -> dict:
    """Everything the partner page needs to render the settlement panel."""
    m = normalize_mode(mode)
    credit = credit_applied(cleared_gbp, subscription_gbp) if m == "credit" else 0.0
    available = available_to_withdraw(cleared_gbp, subscription_gbp,
                                      monthly_commission_gbp, m)
    sub = max(_f(subscription_gbp), 0.0)
    return {
        "mode": m,
        "cleared": max(_f(cleared_gbp), 0.0),
        "subscription": sub,
        "credit_applied": credit,
        "bill_after_credit": round(max(sub - credit, 0.0), 2),
        "is_free_this_month": bool(sub > 0 and credit >= sub),
        "buffer": clawback_buffer(sub, monthly_commission_gbp),
        "available": available,
        "min_withdrawal": MIN_WITHDRAWAL_GBP,
        "can_withdraw": available >= MIN_WITHDRAWAL_GBP,
        "blocked_reason": blocked_reason(available, cleared_gbp, sub,
                                         monthly_commission_gbp, m),
    }


# ── Not built yet (deliberate) ────────────────────────────────────────────────
# The rules above are complete and tested; the integration is not, and two pieces of it
# are Julius's rather than code:
#
#   * A settlement_mode column on partners + a withdrawal-request table (migration).
#   * Applying the credit as a Stripe CUSTOMER BALANCE credit on BLAM8's own billing —
#     NOT via Connect. Connect (src/app/payments.py) is for tenants collecting from THEIR
#     customers; a credit here is simply a smaller invoice.
#   * The withdrawal itself mints a system-generated invoice FROM the partner TO BLAM8,
#     paid by bank transfer. It must be generated from the ledger balance and never
#     hand-authored, or someone eventually invoices BLAM8 for £5,000.
#   * ⚠️ VAT treatment (credit is a discount on our supply to them; cash is them
#     supplying a service to us — and the moment a bill hits £0 and money flows they
#     have moved from discount to income) needs an accountant's answer. This is now the
#     ONLY remaining gate on settings.partner_credit_enabled.
#   * ✅ The agreement covers it as of v2.3 (clauses 4.9–4.12). Note 4.11 in particular:
#     the retained cover below is a real restriction on money the partner has earned, so
#     it had to be in the contract before the product could hold anything back.
