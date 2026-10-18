"""Per-plan daily fair-use allowance for concierge business chat.

The backstop for the pathological tail (docs/modules/module-concierge-cost-controls.md
Part B): the 30-day probe showed typical spend in pennies but a 20-questions-a-
day chatter would cost ~£33/month against a £39 Starter plan. Rather than
paywalling the chat (the concierge stays the primary money surface), each plan
carries a generous daily allowance a normal owner never touches.

Deliberately ALWAYS-ON — unlike the trial pots this is not behind
require_subscription_gate, because it protects paid-plan margins, not the
trial funnel. The trial's own total pot (TRIAL_CONCIERGE_CAP) still runs in
front of this, gate-flagged as before.

Metering mirrors the trial RPCs: one atomic increment_concierge_daily call
that resets the counter when the stored date isn't today and returns the new
total; the cap comparison happens here. Count-then-check — a refused retry
keeps incrementing past the cap, stays refused, and resets at midnight
Europe/London (the app's diary timezone: "back tomorrow" means UK tomorrow).
Fail-open by the trial-cap contract: a metering failure must never break an
answer.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db
from app.config import settings

log = logging.getLogger(__name__)

_LONDON = ZoneInfo("Europe/London")

# Plan key (src/app/plans.py — keys do NOT match display names) → settings suffix.
# Absent keys (trial's None, comped, anything new) fall back to the Pro tier:
# the trial seeds Pro modules, so Pro allowance is the honest default.
_PLAN_TIER = {"email": "starter", "starter": "pro", "concierge": "pro", "professional": "core"}


def daily_cap(tenant: dict | None) -> int | None:
    """This tenant's daily business-chat allowance; None = unlimited.

    `fair_use_exempt` is the operator's switch for their own accounts (Kris
    2026-09-23, after hitting the cap on his rig). Deliberately NOT the comp
    grant: comp is free access, and a real customer can be comped too."""
    if (tenant or {}).get("fair_use_exempt"):
        return None
    plan = (tenant or {}).get("plan") or ""
    if plan == "enterprise":
        return None
    tier = _PLAN_TIER.get(plan, "pro")
    cap = int(getattr(settings, f"concierge_daily_cap_{tier}", 0) or 0)
    return cap or None   # 0 in config = that tier unlimited


def consume(tenant_id: str, tenant: dict | None) -> tuple[bool, int | None]:
    """Count one business-chat question against today's allowance.

    Returns (allowed, cap). Unlimited plans skip the write entirely; a
    metering failure allows (fail-open, logged)."""
    cap = daily_cap(tenant)
    if cap is None:
        return True, None
    today_iso = datetime.now(_LONDON).date().isoformat()
    try:
        used = db.increment_concierge_daily(tenant_id, today_iso)
    except Exception:
        log.exception("fairuse: daily increment failed for %s — allowing", tenant_id)
        return True, cap
    return (used is None or int(used) <= cap), cap


_LIVE_TIER_DEFAULTS = {"starter": 10, "pro": 10, "core": 10, "enterprise": 10}


def live_tier(tenant: dict | None) -> str:
    plan = (tenant or {}).get("plan") or ""
    if plan == "enterprise":
        return "enterprise"
    return _PLAN_TIER.get(plan, "pro")


def live_limit_seconds(tenant: dict | None) -> int:
    """Live connection allowance in seconds; zero means unlimited.

    This is intentionally independent of the question counter and its
    fair_use_exempt switch. Tier settings are persisted and operator-managed.
    """
    limits = db.concierge_live_tier_limits()
    minutes = limits.get(live_tier(tenant), _LIVE_TIER_DEFAULTS[live_tier(tenant)])
    return max(0, min(1440, int(minutes))) * 60


def london_day() -> str:
    return datetime.now(_LONDON).date().isoformat()


# ── Onboarding cost surfaces (2026-09-02 cost review) ────────────────────────
# /concierge/onboard, /onboarding/knowledge/website and /onboarding/voice/learn
# sit OUTSIDE the business-chat allowance above (fair use only meters the
# business routes) and had no cost guard at all — the website distill and a
# voice-learning run are the two most expensive single actions in the product.
# Rather than a new counter table, the meter is the metering that already
# exists: count today's usage_events rows for the operation. Same contracts as
# consume(): London day boundary, fail-open, and a cap of 0 disables the guard.
# Caps live in config (ONBOARD_WEBSITE_DAILY_CAP etc.), sized far above any
# legitimate onboarding.
_ONBOARD_CAP_SETTING = {
    "kb_website": "onboard_website_daily_cap",
    "voice_learning": "voice_learning_daily_call_cap",
    "concierge_onboard": "concierge_onboard_daily_cap",
}


def onboarding_allowed(tenant_id: str, operation: str, client=None) -> bool:
    """True while this tenant is under today's ceiling for one onboarding cost
    surface. Check-before-spend (the routes call this BEFORE the LLM), so the
    usage_events row each call records is the increment — no double counting."""
    cap = int(getattr(settings, _ONBOARD_CAP_SETTING[operation], 0) or 0)
    if not cap:
        return True
    midnight = datetime.now(_LONDON).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        used = db.count_usage_events(tenant_id, operation, midnight.isoformat(),
                                     client=client)
    except Exception:
        log.exception("fairuse: onboarding count failed for %s/%s — allowing",
                      tenant_id, operation)
        return True
    return used < cap


def capped_reply(tenant: dict | None, cap: int) -> str:
    """The polite refusal, in the trial-cap voice. Starter gets the honest
    upgrade line; everyone else just gets the reset time."""
    reply = (f"That's today's {cap} assistant questions used — the allowance "
             "resets at midnight (UK time), so I'll be right here tomorrow. "
             "Your inbox, diary and everything else keep running as normal.")
    if ((tenant or {}).get("plan") or "") == "email":
        pro_cap = int(settings.concierge_daily_cap_pro or 0)
        if pro_cap:
            reply += (f" If you're finding the assistant useful, the Pro plan "
                      f"raises the daily allowance to {pro_cap} — upgrade any "
                      "time under Settings → Billing.")
    return reply
