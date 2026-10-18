"""Stripe billing — hosted Checkout + Customer Portal + webhook sync.

Design (Scout-reviewed):
  * Stripe Checkout (hosted page) keeps PCI scope at zero — we never see card
    data, and CSP needs no Stripe.js host (server-side redirect to stripe.com).
  * Stripe Customer Portal is the self-serve UI — upgrade / downgrade / cancel
    / update card all live there, no custom billing UI to maintain.
  * The webhook is the source of truth for subscription state. The app never
    writes subscription_status except via `sync_subscription()`, which reads
    the current Stripe object — re-ordered or duplicate events therefore
    converge on the same final state.
  * `subscription_status` (this module) is separate from `plan` (the existing
    feature-bundle column, app/db.is_enterprise). status gates ACCESS;
    plan gates FEATURES. Both come from Stripe (status from the webhook,
    plan from the price id via PRICE_TO_PLAN).
  * The existing `subscriptions` table (module switchboard, schema.sql:50) is
    untouched — naming collision avoided by prefixing every public helper here
    with `stripe_` or scoping it to this module.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import stripe

from app import db, plans, trial, usage

# How many top-up packs one Checkout may buy. A guard against a fat-fingered or
# tampered quantity turning a £39 order into a four-figure one.
TOPUP_MAX_PACKS = 20
from app.config import settings

_log = logging.getLogger("blautom8.billing")


# ── Stripe client init ──────────────────────────────────────────────────────
# Module-scoped: stripe.api_key is global on the SDK, so we set it once at
# import time. When the secret is empty (e.g. local dev without billing),
# every Stripe call will 401 fast — that's the right failure mode, and the
# /billing routes refuse to render when settings.stripe_secret_key is blank.
stripe.api_key = settings.stripe_secret_key


# ── Price → plan-name mapping ───────────────────────────────────────────────
# The price id is what Stripe sends in webhook payloads; the human plan name
# is what we record on the tenant for UI and feature-flag decisions. Built
# from settings so swapping price ids (e.g. test → live) needs no code change.
def price_to_plan() -> dict[str, str]:
    """{stripe_price_id: 'starter'|'professional'|'enterprise'} — built lazily
    so changing env vars in tests is reflected immediately."""
    mapping: dict[str, str] = {}
    if settings.stripe_email_price_id:
        mapping[settings.stripe_email_price_id] = "email"
    if settings.stripe_starter_price_id:
        mapping[settings.stripe_starter_price_id] = "starter"
    if settings.stripe_concierge_price_id:
        mapping[settings.stripe_concierge_price_id] = "concierge"
    if settings.stripe_professional_price_id:
        mapping[settings.stripe_professional_price_id] = "professional"
    # Line-item menu (2026-07-10): "Core + your OS" is £248 for every OS except
    # Field OS · Transport (£298, the professional price above). Both resolve to
    # the same plan key — the tenant's industry decides WHICH module unlocks.
    if settings.stripe_os_price_id:
        mapping[settings.stripe_os_price_id] = "professional"
    if settings.stripe_scale_price_id:
        mapping[settings.stripe_scale_price_id] = "scale"
    if settings.stripe_enterprise_price_id:
        mapping[settings.stripe_enterprise_price_id] = "enterprise"
    return mapping


def is_configured() -> bool:
    """True when the minimum env to run real Stripe calls is present. Drives
    the /billing/plans page CTA (real Checkout link vs. 'billing not
    configured' explainer) and lets the webhook 503 instead of NPE-ing."""
    return bool(settings.stripe_secret_key and settings.stripe_webhook_secret)


# ── Customer ←→ tenant linking ──────────────────────────────────────────────
def ensure_stripe_customer(tenant_id: str, email: str | None) -> str:
    """Return the Stripe customer id for this tenant, creating one on the fly
    if absent. Idempotent — second call just returns the stored id. The
    customer carries `metadata.tenant_id` so the Stripe Dashboard shows the
    right BLAM8 tenant alongside every payment and subscription."""
    existing = db.stripe_get_customer_id(tenant_id)
    if existing:
        return existing
    customer = stripe.Customer.create(
        email=email or None,
        metadata={"tenant_id": str(tenant_id)},
    )
    db.stripe_set_customer(tenant_id, customer.id, billing_provider="self_serve")
    return customer.id


# ── Checkout (self-serve) ───────────────────────────────────────────────────
def create_checkout_session(tenant_id: str, price_id: str, user_email: str | None,
                            success_url: str, cancel_url: str) -> str:
    """Create a Stripe-hosted Checkout session and return its URL. Caller
    303-redirects the user there. Carries client_reference_id=tenant_id so the
    checkout.session.completed webhook can link a brand-new Stripe Customer to
    this tenant unambiguously. Trial days come from settings (Stripe-native
    trial — no app-side trial bookkeeping)."""
    customer_id = ensure_stripe_customer(tenant_id, user_email)
    # ONE trial clock from sign-up: pin the paid plan's trial to the END of the
    # tenant's existing trial window (trial.paid_trial_end), so it never grants a
    # fresh 14 days on top of the pilot. Upgrade with 3 days left → 3 days of paid
    # trial; window already used up → no trial, the subscription bills now. This
    # stops a customer stacking the free pilot and a fresh paid trial.
    subscription_data: dict = {}
    trial_end = trial.paid_trial_end(db.get_tenant(tenant_id) or {})
    if trial_end:
        subscription_data["trial_end"] = trial_end
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(tenant_id),
        allow_promotion_codes=True,
        subscription_data=subscription_data or None,
        metadata={"tenant_id": str(tenant_id)},
    )
    return session.url


def create_portal_session(tenant_id: str, return_url: str) -> str:
    """Create a Stripe Customer Portal session (upgrade / downgrade / cancel /
    update card) and return its URL. The tenant must already have a Stripe
    customer id (set on first Checkout); raise rather than auto-creating, so
    we never open the portal for a tenant with no real billing history."""
    customer_id = db.stripe_get_customer_id(tenant_id)
    if not customer_id:
        raise PortalUnavailable("This tenant has no Stripe customer yet.")
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return session.url


class PortalUnavailable(Exception):
    """Raised by create_portal_session when the tenant has no Stripe customer
    yet — the /billing/portal route renders the plans page instead."""


# ── Webhook: the source of truth ────────────────────────────────────────────
def sync_subscription(stripe_subscription: dict) -> None:
    """Mirror one Stripe Subscription onto the tenant row. Called from every
    customer.subscription.* event AND from /billing/success (the latter
    eliminates the Checkout→success-page race: the user arrives back at our
    success URL BEFORE the webhook fires, so the success handler retrieves
    the subscription synchronously and syncs here too — same code path).

    Re-orderable: we always write the FULL state of the current Subscription
    object, so an out-of-order 'updated' followed by 'created' converges
    correctly on the latest Stripe truth.
    """
    customer_id = stripe_subscription.get("customer")
    if not customer_id:
        return
    tenant_id = db.stripe_get_tenant_id_by_customer(customer_id)
    if not tenant_id:
        # Subscription belongs to a customer we haven't linked yet — the
        # checkout.session.completed handler will link, then the customer.
        # subscription.created event re-fires the sync. Drop silently.
        return

    status = stripe_subscription.get("status") or "none"
    items = (stripe_subscription.get("items") or {}).get("data") or []
    # Pick the TIER price. A subscription can now carry a second item — the
    # extra-mailbox add-on — so items[0] is no longer guaranteed to be the plan.
    # Prefer the item whose price id maps to a known plan; fall back to the first.
    known = price_to_plan()
    item_price_ids = [(it.get("price") or {}).get("id") for it in items]
    price_id = next((pid for pid in item_price_ids if pid in known),
                    item_price_ids[0] if item_price_ids else None)

    db.stripe_set_subscription(
        tenant_id,
        subscription_id=stripe_subscription.get("id"),
        status=status,
        price_id=price_id,
        trial_ends_at=_ts(stripe_subscription.get("trial_end")),
        current_period_end=_ts(stripe_subscription.get("current_period_end")),
    )
    # Mirror the resolved plan name onto tenants.plan so existing feature
    # gates (is_enterprise) keep working without touching their callers.
    plan_name = price_to_plan().get(price_id) if price_id else None
    if plan_name:
        db.set_tenant_plan(tenant_id, plan_name)
        # Plan-bundled entitlement: a live subscription activates the plan's
        # modules (and pauses excluded ones on downgrade), so upgrading on the
        # billing page is genuinely self-serve — no operator step needed.
        if status in ACTIVE_STATUSES:
            db.apply_plan_modules(tenant_id, plan_name)
    # Reconcile the add-ons to the tenant's live counts on every subscription
    # event (first checkout, upgrade/downgrade re-checks the new tier's included
    # allowance). Cheap, idempotent, self-correcting. Branches FIRST — the mailbox
    # sync's included count depends on the billable branch count.
    if status in ACTIVE_STATUSES:
        for sync in (sync_depot_addon, sync_mailbox_addon):
            try:
                sync(tenant_id)
            except Exception:
                _log.warning("%s failed for tenant %s", sync.__name__, tenant_id,
                             exc_info=True)


def can_add_mailbox(tenant: dict | str) -> bool:
    """True when the tenant may connect an ADDITIONAL mailbox alongside existing
    ones. Enterprise is unlimited; any tenant on an active paid plan may add
    (each mailbox beyond the plan's included allowance bills as the extra-mailbox
    add-on). A pilot with no active subscription stays single-mailbox — the
    /oauth 'add' link falls back to replace for them."""
    t = tenant if isinstance(tenant, dict) else (db.get_tenant(tenant) or {})
    return db.is_enterprise(t) or has_active_subscription(t)


def create_topup_checkout_session(tenant_id: str, quantity: int, user_email: str | None,
                                  success_url: str, cancel_url: str) -> str:
    """Checkout for an email top-up. mode="payment", not "subscription" — the
    whole point of the top-up is that it does NOT recur (a recurring one is
    either dominated by the extra-mailbox add-on or undercuts Pro; see
    plans.EMAIL_TOPUP).

    The emails bought are stamped into metadata rather than recomputed from the
    price on the way back, so a later change to what £39 buys can never restate
    what an already-paid order was worth."""
    price_id = plans.topup_price_id()
    if not price_id:
        raise RuntimeError("email top-up price id is not configured")
    quantity = max(1, min(int(quantity or 1), TOPUP_MAX_PACKS))
    emails = quantity * plans.topup_emails()
    customer_id = ensure_stripe_customer(tenant_id, user_email)
    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": quantity}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(tenant_id),
        metadata={"tenant_id": str(tenant_id), "kind": "email_topup",
                  "emails": str(emails)},
        payment_intent_data={"metadata": {"tenant_id": str(tenant_id),
                                          "kind": "email_topup",
                                          "emails": str(emails)}},
    )
    return session.url


def credit_topup_from_session(session: dict) -> int | None:
    """Apply a paid top-up Checkout session. Returns the new balance, or None if
    this session is not a top-up / not paid.

    Idempotent through db.grant_email_credit, which is keyed on the payment
    intent: Stripe retries webhooks, and the /billing/success page applies the
    same session too so the user sees their credit without waiting for one. Both
    paths landing at once must credit exactly once."""
    meta = session.get("metadata") or {}
    if meta.get("kind") != "email_topup":
        return None
    if session.get("payment_status") != "paid":
        return None
    payment = session.get("payment_intent") or session.get("id")
    if isinstance(payment, dict):
        payment = payment.get("id")
    tenant_id = session.get("client_reference_id") or meta.get("tenant_id")
    try:
        emails = int(meta.get("emails") or 0)
    except (TypeError, ValueError):
        emails = 0
    if not (tenant_id and payment and emails > 0):
        return None
    return db.grant_email_credit(tenant_id, emails, str(payment))


def mailbox_addon_quote(tenant_id: str, client=None) -> dict:
    """What connecting ONE more mailbox does to this tenant's bill.

    Built for the confirm step (Kris 2026-09-01): until now "+ Add Outlook" went
    straight to the provider's consent screen, and sync_mailbox_addon added a
    prorated Stripe item on the way back — a customer could put their own bill
    up 40% without ever seeing a number.

    `total_after` is the honest part. It is filled in ONLY when the whole monthly
    figure can be stated exactly: a flat-priced tier (plans.FLAT_PRICED_TIERS)
    and no branch add-on riding alongside it. Anything else — Core + your OS,
    Enterprise, a retired tier, or any tenant paying for branches — leaves it None,
    and the caller quotes the £39 alone rather than a total that would be wrong.

    Mirrors sync_mailbox_addon's arithmetic exactly, including grandfathered
    mailboxes not counting, so the quote and the charge can never disagree."""
    tenant = db.get_tenant(tenant_id, client=client) or {}
    included = plans.included_mailboxes(tenant.get("plan"))
    live = db.list_mailboxes(tenant_id, client=client)
    depots = billable_depots(tenant, tenant_id, client=client)
    extra_gbp = plans.extra_mailbox_gbp()

    quote = {
        "can_add": can_add_mailbox(tenant),
        "unlimited": included is None,
        "plan_name": plans.plan_name(tenant.get("plan")),
        "count": len(live),
        "ordinal": len(live) + 1,
        "extra_gbp": extra_gbp,
        "emails_added": usage.EXTRA_MAILBOX_EMAILS,
        "charged": False, "included": None,
        "mailbox_gbp_after": 0, "total_after": None,
    }
    if included is None:            # Enterprise — unlimited, nothing to bill
        return quote

    allowance = int(included) + depots
    billable_after = sum(1 for m in live if not m.get("grandfathered")) + 1
    after_qty = max(0, billable_after - allowance)
    plan_gbp = plans.plan_price_gbp(tenant.get("plan"))
    quote["included"] = allowance
    quote["charged"] = after_qty > max(0, billable_after - 1 - allowance)
    if extra_gbp is not None:
        quote["mailbox_gbp_after"] = after_qty * extra_gbp
        if plan_gbp is not None and not depots:
            quote["total_after"] = plan_gbp + quote["mailbox_gbp_after"]
    return quote


def billable_depots(tenant: dict, tenant_id: str, client=None) -> int:
    """How many fleet branches the tenant pays the additional-branch
    add-on for: live branches beyond the plan's included first branch. 0 for
    Enterprise (unlimited) or without an active subscription — mirrors the
    mailbox add-on's rules so billing and the usage meter always agree."""
    if tenant.get("subscription_status") not in ACTIVE_STATUSES:
        return 0
    included = plans.included_depots(tenant.get("plan"))
    if included is None:  # Enterprise — unlimited, the add-on never applies
        return 0
    return max(0, len(db.list_branches(tenant_id, client=client)) - int(included))


def _reconcile_addon_item(sub_id: str, price_id: str, quantity: int) -> None:
    """Set one add-on price's subscription-item quantity to `quantity` (0 =
    remove the item). Idempotent — only writes when the quantity changes, so
    the resulting subscription.updated webhook converges rather than looping."""
    sub = stripe.Subscription.retrieve(sub_id)
    item = next((it for it in (sub.get("items", {}).get("data") or [])
                 if (it.get("price") or {}).get("id") == price_id), None)
    if quantity > 0:
        if item is None:
            stripe.SubscriptionItem.create(
                subscription=sub_id, price=price_id, quantity=quantity,
                proration_behavior="create_prorations")
        elif item.get("quantity") != quantity:
            stripe.SubscriptionItem.modify(
                item["id"], quantity=quantity, proration_behavior="create_prorations")
    elif item is not None:
        stripe.SubscriptionItem.delete(item["id"], proration_behavior="create_prorations")


def sync_mailbox_addon(tenant_id: str) -> None:
    """Reconcile the Stripe extra-mailbox add-on quantity to the tenant's live,
    non-grandfathered mailbox count minus the plan's included allowance —
    where the allowance GROWS by one per billable additional branch, because
    the £149 branch price includes the branch's own mailbox (a three-branch tenant on
    Core + Transport has 2 plan mailboxes + 2 branch mailboxes included).

    No-op unless ALL of: an active Stripe subscription exists, the plan has a
    finite included allowance (Enterprise = unlimited), and the add-on price id
    is configured. Grandfathered mailboxes (every mailbox that existed at launch)
    never count, so switching this on can't retro-bill an existing customer."""
    price_id = plans.extra_mailbox_price_id()
    if not price_id:
        return
    tenant = db.get_tenant(tenant_id) or {}
    sub_id = tenant.get("stripe_subscription_id")
    if not sub_id or tenant.get("subscription_status") not in ACTIVE_STATUSES:
        return
    included = plans.included_mailboxes(tenant.get("plan"))
    if included is None:  # Enterprise — unlimited, the add-on never applies
        return
    included = int(included) + billable_depots(tenant, tenant_id)
    billable = sum(1 for m in db.list_mailboxes(tenant_id) if not m.get("grandfathered"))
    _reconcile_addon_item(sub_id, price_id, max(0, billable - included))


def sync_depot_addon(tenant_id: str) -> None:
    """Reconcile the Stripe additional-branch add-on quantity to the tenant's
    live branch count beyond the plan's included first branch. Same contract as
    sync_mailbox_addon: no-op without an active subscription, a finite branch
    allowance (Enterprise = unlimited) and a configured price id. Called from
    subscription events and from branch add/delete on the fleet page."""
    price_id = plans.extra_depot_price_id()
    if not price_id:
        return
    tenant = db.get_tenant(tenant_id) or {}
    sub_id = tenant.get("stripe_subscription_id")
    if not sub_id or tenant.get("subscription_status") not in ACTIVE_STATUSES:
        return
    if plans.included_depots(tenant.get("plan")) is None:
        return
    _reconcile_addon_item(sub_id, price_id, billable_depots(tenant, tenant_id))


def mark_canceled(stripe_subscription: dict) -> None:
    """Apply a customer.subscription.deleted event — write 'canceled' on the
    matching tenant. Defensive against deletes for an unlinked customer."""
    customer_id = stripe_subscription.get("customer")
    if not customer_id:
        return
    tenant_id = db.stripe_get_tenant_id_by_customer(customer_id)
    if not tenant_id:
        return
    db.stripe_set_subscription_status(tenant_id, "canceled")


# ── status helpers ──────────────────────────────────────────────────────────
ACTIVE_STATUSES = {"active", "trialing"}


def has_active_subscription(tenant: dict | str) -> bool:
    """Treat 'active' and 'trialing' as paid access; everything else is locked.
    Accepts a tenant dict (no extra query) or a tenant id."""
    t = tenant if isinstance(tenant, dict) else (db.get_tenant(tenant) or {})
    return (t or {}).get("subscription_status") in ACTIVE_STATUSES


def _ts(unix_or_none) -> str | None:
    """Stripe timestamps are seconds-since-epoch ints. Return an ISO 8601 UTC
    string for Supabase timestamptz columns, or None."""
    if not unix_or_none:
        return None
    return datetime.fromtimestamp(int(unix_or_none), tz=timezone.utc).isoformat()
