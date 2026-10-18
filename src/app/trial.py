"""Free-trial gate — 14 days OR the trial email pot (TRIAL_EMAIL_CAP, default
100), whichever runs out first.

The trial is the no-Stripe path a brand-new tenant lands on at sign-up. It is a
SEPARATE concept from the Stripe subscription_status (which gates paid access):
a tenant with an active/trialing Stripe subscription is "converted" and never
trial-limited.

Enforcement is behind settings.require_subscription_gate so Julius can switch it
on without a redeploy. Until then evaluate() still reports state (so the operator
console and settings page show where each tenant stands) but nothing blocks and
nothing is counted.

"1 email" is defined once, here: ONE inbound message the agent drafts a reply to
— the same unit the plan tiers' monthly allowances are sold in. The
inbound and its drafted reply are one email-handling event, counted once when the
draft row is first created (a duplicate webhook delivery, or a re-draft of an
already-handled email, never re-counts).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app import db
from app.config import settings

log = logging.getLogger(__name__)

TRIAL_EMAIL_CAP = settings.trial_email_cap   # env-driven (TRIAL_EMAIL_CAP, default 100)
TRIAL_CONCIERGE_CAP = settings.trial_concierge_cap   # env-driven (TRIAL_CONCIERGE_CAP, default 30)
_ACTIVE_SUB = {"active", "trialing"}   # a Stripe subscription that grants access


class TrialLimitReached(Exception):
    """Raised by the pipeline on a HUMAN-initiated draft once the trial is over
    and the gate is on. The handler in src/app/main.py 303s the user to
    /billing/plans; automatic paths (webhook/poller) return None instead."""

    def __init__(self, info: dict | None = None):
        self.info = info or {}
        super().__init__((self.info or {}).get("status", "trial_expired"))


def _days_left(end, now) -> int:
    """Whole days left, rounded UP: a 14-day trial reads '14 days left' on
    day one, not 13 (stays run #10, 2026-09-13)."""
    import math
    return max(0, math.ceil((end - now).total_seconds() / 86400))


def _parse(value) -> datetime | None:
    """Supabase hands timestamps back as ISO strings; normalise to aware UTC."""
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def on_trial(tenant: dict | None) -> bool:
    """True while the tenant is on the free trial — i.e. has no active/trialing
    Stripe subscription. Grandfathered launch tenants carry subscription_status
    'active', so they read as converted and are never trial-limited."""
    return (tenant or {}).get("subscription_status") not in _ACTIVE_SUB


def concierge_blocked(tenant: dict | None) -> bool:
    """Has this trial tenant spent its concierge allowance? (Full-product trial,
    2026-07-31: the modules are all open, so the two LLM surfaces each carry a
    pot — TRIAL_EMAIL_CAP for drafts, this one for owner-assistant questions.)
    Enforcement rides the SAME flag as the email gate: with
    require_subscription_gate off nothing blocks, and a converted tenant is
    never limited."""
    if not settings.require_subscription_gate:
        return False
    if not on_trial(tenant):
        return False
    return int((tenant or {}).get("trial_concierge_used") or 0) >= TRIAL_CONCIERGE_CAP


def consume_concierge(tenant_id: str) -> None:
    """Count one trial concierge question. Best-effort — a metering failure must
    never break an answer (mirrors consume())."""
    try:
        db.increment_trial_concierge(tenant_id)
    except Exception:
        log.exception("trial: failed to increment concierge counter for %s", tenant_id)


def evaluate(tenant: dict | None, now: datetime | None = None) -> dict:
    """The trial state machine — the single source of truth for both the gate and
    the operator/settings surfaces. Returns a dict with `status`
    (active|grace_active|converted|expired_time|expired_cap), `active` (bool),
    plus email/day counters for display."""
    now = now or datetime.now(timezone.utc)
    t = tenant or {}
    used = int(t.get("trial_emails_used") or 0)
    base = {"emails_used": used, "cap": TRIAL_EMAIL_CAP,
            "emails_remaining": max(0, TRIAL_EMAIL_CAP - used)}

    # Admin comp — free access granted from the operator console, independent of
    # Stripe and the trial (highest priority). The Stripe webhook never writes
    # comp_access/comp_until, so a subscription event can never revoke a comp.
    comp = t.get("comp_access")
    if comp:
        comp_until = _parse(t.get("comp_until"))
        if comp == "indefinite" or (comp_until and now < comp_until):
            days = max(0, (comp_until - now).days) if comp_until else None
            return {**base, "status": "comped", "active": True, "days_remaining": days}
        # comp set but past its date → fall through to normal trial/subscription

    # Converted: a real (or grandfathered) Stripe subscription → full access.
    if not on_trial(t):
        return {**base, "status": "converted", "active": True, "days_remaining": None}

    # Grace: tenants already past their notional trial at the deploy that turned
    # the gate on get a short runway before they're treated as expired.
    grace_until = _parse(t.get("trial_grace_until"))
    if t.get("trial_status") == "grace_active" and grace_until and now < grace_until:
        return {**base, "status": "grace_active", "active": True,
                "days_remaining": max(0, (grace_until - now).days)}

    start = _parse(t.get("trial_start_at") or t.get("created_at"))
    if start is None:                      # no anchor to measure from → treat as over
        return {**base, "status": "expired_time", "active": False, "days_remaining": 0}
    end = start + timedelta(days=settings.trial_days)
    if now >= end:
        return {**base, "status": "expired_time", "active": False, "days_remaining": 0}
    if used >= TRIAL_EMAIL_CAP:
        return {**base, "status": "expired_cap", "active": False,
                "days_remaining": max(0, _days_left(end, now))}
    return {**base, "status": "active", "active": True,
            "days_remaining": max(0, _days_left(end, now))}


def is_active(tenant: dict | None, now: datetime | None = None) -> bool:
    return evaluate(tenant, now)["active"]


def trial_window_end(tenant: dict | None) -> datetime | None:
    """The end of the tenant's SINGLE trial window (start anchor + trial_days) —
    the same window evaluate() measures. None when there's no anchor."""
    start = _parse((tenant or {}).get("trial_start_at") or (tenant or {}).get("created_at"))
    return start + timedelta(days=settings.trial_days) if start else None


# Stripe requires trial_end to be at least ~48h out; under that we just bill now.
_MIN_PAID_TRIAL = timedelta(hours=48)


def paid_trial_end(tenant: dict | None, now: datetime | None = None) -> int | None:
    """The Stripe `trial_end` (unix ts) for a tenant upgrading to a paid plan: the
    end of their ONE trial window from sign-up, so a paid plan never grants a fresh
    14 days on top of the pilot. None when the window is already gone (or under
    Stripe's ~48h floor) — the subscription then bills immediately. This is what
    stops a customer stacking the pilot's free period with a fresh paid trial."""
    now = now or datetime.now(timezone.utc)
    end = trial_window_end(tenant)
    if end and (end - now) >= _MIN_PAID_TRIAL:
        return int(end.timestamp())
    return None


def gate_allows(tenant: dict | None) -> bool:
    """Whether a draft may proceed. Returns True instantly (no work) when the
    access gate is off — so with the flag down the pipeline is byte-identical to
    today. With the gate on, blocks once the trial is over."""
    if not settings.require_subscription_gate:
        return True
    return is_active(tenant)


def consume(tenant_id: str) -> None:
    """Count one drafted trial email atomically. Best-effort — a metering failure
    must never break a draft (mirrors app/usage.record)."""
    try:
        total = db.increment_trial_email(tenant_id, cap=TRIAL_EMAIL_CAP)
    except Exception:
        log.exception("trial: failed to increment email counter for %s", tenant_id)
        return
    # Exactly at the cap, so exactly once — the counter keeps climbing past it,
    # so equality is the edge and needs no "have we said this yet" column
    # (Kris 2026-09-04). The same increment has just flipped trial_status to
    # expired_cap: from the next email on, nothing gets drafted. Being told
    # that by a customer asking why nobody replied is the failure this closes.
    if total == TRIAL_EMAIL_CAP:
        try:
            from app import push

            push.notify(
                tenant_id, title="You've used your trial emails",
                body="All %d drafted — no more replies will be written until you "
                     "pick a plan." % TRIAL_EMAIL_CAP,
                url="/app/usage", tag="trial-cap-%s" % tenant_id,
                kind="email_cap_reached")
        except Exception:
            log.warning("trial: cap notification failed for %s", tenant_id,
                        exc_info=True)
