"""Token & cost tracking (Phase 1).

Capture every paid API call (Anthropic drafting, Voyage embeddings), price it,
and summarise it per tenant for the Usage screen. Two design rules:

1. Capture is BEST-EFFORT — record() never raises into the caller, so a metering
   failure can never break a draft or an embed.
2. Everything is metered at the API call (draft/embed time), not at send time —
   so usage counts even when a draft is skipped or never sent.

Cost is stored in USD (the APIs bill in USD); the dashboard converts to GBP for
display via settings.usd_gbp_rate.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timedelta, timezone

from app import db, plans, trial, metering
from app.config import settings

log = logging.getLogger(__name__)

# Rates verified 2026-09-28: https://platform.claude.com/docs/en/about-claude/pricing
# Standard global Claude API only. Unsupported models/modifiers stay unknown.
# ── Pricing — USD per 1M tokens, keyed by (provider, alias) ───────────────────
# Dated model ids (e.g. claude-haiku-4-5-20251001) resolve to their alias.
PRICING: dict[tuple[str, str], dict[str, float]] = {
    ("anthropic", "claude-haiku-4-5"):  {"input": 1.00, "output": 5.00},
    ("anthropic", "claude-sonnet-4-6"): {"input": 3.00, "output": 15.00},
    ("anthropic", "claude-sonnet-5"):   {"input": 2.00, "output": 10.00},
    ("anthropic", "claude-opus-4-8"):   {"input": 5.00, "output": 25.00},
    # Claude 5.5 rows: the Claude API reference, cached 2026-10-06
    # (docs/plan-planner-worker-reviewer.md §1). Re-check on the pricing page.
    ("anthropic", "claude-haiku-5-5"):  {"input": 0.10, "output": 0.50},
    ("anthropic", "claude-sonnet-5-5"): {"input": 2.00, "output": 10.00},
    ("anthropic", "claude-opus-5-5"):   {"input": 4.00, "output": 20.00},
    # Voyage's remaining account free allocation is not observed here. Its
    # former permanent-zero assumption is not a verified price.
}
# GPT-Live voice sessions: "$0.05 per minute, billed per second", never rounded up to
# the minute (https://developers.openai.com/api/docs/models/gpt-live-1, read 2026-10-14).
# This is the audio session only; the reasoning and tool work runs on our own backend
# and is metered where it is made (llm.py). Creating a session bills VOICE_INIT_SECONDS
# up front, credited against the duration once the session starts running
# (https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live).
VOICE_PER_MINUTE: dict[tuple[str, str], float] = {("openai", "gpt-live-1"): 0.05}
VOICE_INIT_SECONDS = 15
# Haiku 5.5 bills a second rate card above a 100K-token prompt (input, cache reads
# and cache writes together). That card is not modelled, so such a call is unknown.
PROMPT_CAP_TOKENS: dict[tuple[str, str], int] = {("anthropic", "claude-haiku-5-5"): 100_000}
# Cache-read rates verified 2026-10-14: https://platform.claude.com/docs/en/about-claude/pricing
# Sonnet 5.5 and Opus 5.5 bill a cache hit at 0.05x base input ($0.10 and $0.20 per MTok).
# Haiku 5.5 is $0.01 per MTok, 0.1x its input. Every other model stays at 0.1x.
_CACHE_READ_MULT = 0.10    # cache reads ~0.1x base input price (default)
_CACHE_READ_MULT_BY_MODEL: dict[tuple[str, str], float] = {
    ("anthropic", "claude-sonnet-5-5"): 0.05,
    ("anthropic", "claude-opus-5-5"): 0.05,
}
_CACHE_WRITE_MULT = 1.25   # cache writes ~1.25x base input price (5-min TTL)


def _alias(model: str) -> str:
    """Strip a trailing -YYYYMMDD: claude-haiku-4-5-20251001 -> claude-haiku-4-5."""
    head, _, tail = model.rpartition("-")
    return head if (head and tail.isdigit() and len(tail) == 8) else model


def cost_usd(provider: str, model: str, *, input_tokens: int = 0, output_tokens: int = 0,
             cache_read: int = 0, cache_write: int = 0, embed_tokens: int = 0,
             cache_write_5m: int | None = None,
             cache_write_1h: int | None = None,
             voice_seconds: float | None = None) -> float | None:
    if voice_seconds is not None:
        rate = VOICE_PER_MINUTE.get((provider, _alias(model)))
        if rate is None:
            log.warning("usage: no verified voice rate for %s/%s", provider, model)
            return None
        return max(0.0, voice_seconds) / 60.0 * rate
    p = PRICING.get((provider, _alias(model)))
    if p is None:
        log.warning("usage: no verified price for %s/%s", provider, model)
        return None
    cap = PROMPT_CAP_TOKENS.get((provider, _alias(model)))
    if cap is not None and input_tokens + cache_read + cache_write > cap:
        log.warning("usage: %s/%s prompt is over %d tokens; no verified price", provider, model, cap)
        return None
    if cache_write and (cache_write_5m is None or cache_write_1h is None):
        return None
    if cache_write != (cache_write_5m or 0) + (cache_write_1h or 0):
        return None
    pin, pout, pemb = p.get("input", 0.0), p.get("output", 0.0), p.get("embed", 0.0)
    return (
        input_tokens / 1e6 * pin
        + output_tokens / 1e6 * pout
        + cache_read / 1e6 * pin * _CACHE_READ_MULT_BY_MODEL.get((provider, _alias(model)), _CACHE_READ_MULT)
        + (cache_write_5m or 0) / 1e6 * pin * _CACHE_WRITE_MULT
        + (cache_write_1h or 0) / 1e6 * pin * 2.0
        + embed_tokens / 1e6 * pemb
    )


# ── Source attribution (who triggered the spend) ──────────────────────────────
# Derived from (module, operation) rather than stored: every operation name is
# already unique to one trigger surface, so the mapping applies retroactively to
# every historical usage_events/llm_calls row and can't drift out of step with a
# write path. Answers "is tenant X's cost inbox volume or portal traffic?".
#   owner            — the business owner acting in the dashboard/concierge
#   inbound_email    — an inbound customer email through the drafting pipeline
#   portal           — a VERIFIED portal customer engaging with the business
#                      (bookings, business KB, escalation) — the business's cost
#   portal_unverified — an anonymous portal visitor, pre-verification — the
#                      PLATFORM's cost (recorded against db.PLATFORM_TENANT_ID)
#   portal_general   — a verified portal customer whose turn touched no
#                      business data (e.g. "what is BLAM8?") — also platform
#   automation       — proactive/background jobs (reminders, chasers, sweeps)
#   prospect         — the pre-auth marketing-site chatbot (platform tenant's cost)
#   retrieval        — embed_query serves several callers (draft-time recall,
#                      concierge, portal); the row alone can't say which, so it
#                      is bucketed honestly rather than guessed.
SOURCE_BY_OPERATION: dict[str, str] = {
    "llm_failure": "other",
    "draft": "inbound_email", "crm_extract": "inbound_email",
    "dependant_extract": "inbound_email",
    "document_read": "inbound_email",       # an attached card read for its label + facts (dependants_extract.read_documents)
    "booking_extract": "inbound_email",     # grounded booking's cited-date read (src/app/booking_extract.py)
    "crm_intel": "inbound_email", "tms_handoff": "inbound_email",
    "fsm_handoff": "inbound_email", "party_extract": "inbound_email",
    "redraft": "owner", "concierge_onboard": "owner", "concierge_help": "owner",
    "concierge_fix": "owner", "concierge_business": "owner", "concierge_discovery": "owner", "concierge_discovery_rewrite": "owner",
    "concierge_task": "owner",              # one task of the Concierge's task list (src/app/concierge_tasks.py)
    "gptlive_session": "owner",             # one GPT-Live voice session, priced by the second (src/app/gptlive.py)
    "concierge_task_plan": "owner", "concierge_task_review": "owner",  # the planner and reviewer seats (src/app/task_loop.py)
    "concierge_email": "owner",             # a new email the owner asked for by voice (src/app/concierge_draft.py)
    "voice_learning": "owner", "voice_sample": "owner", "ocr_stock_in": "owner",
    "condition_diff": "owner", "plan": "owner", "followup": "owner",
    "embed_ingest": "owner", "embed_update": "owner", "embed_flag": "owner",
    "kb_website": "owner",
    "edit_review": "owner",                 # after the owner sends an edited draft: worth saving as knowledge or a rule? (src/app/edit_review.py)
    "portal_chat": "portal",
    "concierge_ask": "prospect",
    "scenario_ask": "prospect",             # the FAQ scenario box (concierge.scenario_answer)
    "reminder": "automation", "on_my_way": "automation", "level_change": "automation", "package_balance": "automation",
    "hand_cancel": "automation",
    "waitlist_offer": "automation", "reenrol_window": "automation", "offer_nudge": "automation",
    "crm_action_resolve": "automation",
    "recall": "automation", "special": "automation",
    "concierge_warm": "automation",        # priming the voice answer format after a deploy (src/app/concierge_warm.py)
    "event_chase": "automation", "run_sheet": "automation",
    "sequence": "automation", "scheduled_plan": "automation", "no_show_score": "automation",
    "wholesale_order": "automation", "wholesale_hold": "automation",
    "missed_call": "automation",
    "embed_query": "retrieval",
    # Founder Voice CRM debrief extraction (/admin/voice-crm) — no tenant, so it
    # never meters a customer; mapped so the operation guard + /admin/llm stay whole.
    "vcrm_debrief": "owner",
}


# The portal chat turn splits by WHO PAYS (2026-07-26 directive): the same
# surface bills the platform until the visitor verifies, and bills the platform
# again when a verified customer asks something that needed no business data.
# portal_chat.billing_operation() picks the operation; this maps it to a source.
_PORTAL_SOURCES = {
    "portal_chat_unverified": "portal_unverified",
    "portal_chat_general": "portal_general",
}


def source_for(module: str | None, operation: str | None) -> str:
    """The trigger surface for one usage event. The two platform-paid portal
    operations win outright (they say who pays by construction, whatever the
    module); after that module='portal' wins (any future portal operation is
    portal traffic); unmapped operations land in 'other' so a new call site
    shows up as a visible bucket instead of silently inflating a real one."""
    src = _PORTAL_SOURCES.get(operation or "")
    if src:
        return src
    if (module or "") == "portal":
        return "portal"
    return SOURCE_BY_OPERATION.get(operation or "", "other")


def event_operation(event: dict) -> str:
    """Failure spend keeps its trigger, without becoming a completed turn."""
    op = event.get("operation") or ""
    if op == "llm_failure":
        attempts = (event.get("raw_usage") or {}).get("attempts") or []
        return (attempts[0].get("operation") or op) if attempts else op
    return op


# ── Recording (best-effort — never raises into the caller) ────────────────────
def record(tenant_id: str, provider: str, model: str, operation: str, *,
           input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
           cache_write: int = 0, embed_tokens: int = 0,
           email_log_id: str | None = None, module: str = "email") -> None:
    try:
        c = cost_usd(provider, model, input_tokens=input_tokens, output_tokens=output_tokens,
                     cache_read=cache_read, cache_write=cache_write, embed_tokens=embed_tokens)
        db.insert_usage_event({
            "tenant_id": tenant_id, "provider": provider, "model": model,
            "operation": operation, "module": module,
            "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read or 0), "cache_write_tokens": int(cache_write or 0),
            "embed_tokens": int(embed_tokens or 0), "cost_usd": round(c or 0, 6),
            "raw_usage": {"known_cost_usd": c, "complete": c is not None},
            "cost_version": metering.COST_VERSION if provider == "anthropic" else None,
            "email_log_id": email_log_id,
        })
    except Exception:  # metering must never break the pipeline
        log.exception("usage: failed to record %s/%s %s (tenant %s)", provider, model, operation, tenant_id)


def record_anthropic(tenant_id: str, model: str, usage, *, operation: str = "draft",
                     email_log_id: str | None = None, module: str = "email") -> None:
    """Persist the business event once, with all its provider attempt receipts."""
    try:
        attempts = metering.receipts(usage)
        if attempts is None:
            attempts = [{"usage": metering.raw_usage(usage), "complete": True}]
        attempts = [{**a, "cost_usd": None if a.get("pricing_unknown") else metering.price(a.get("model") or model, a)} for a in attempts]
        raw = metering.envelope(attempts)
        counts = {k: sum((a.get("usage") or {}).get(k) or 0 for a in attempts)
                  for k in metering.FIELDS}
        db.insert_usage_event({
            "tenant_id": tenant_id, "provider": "anthropic", "model": model,
            "operation": operation, "module": module, "email_log_id": email_log_id,
            "input_tokens": counts["input_tokens"], "output_tokens": counts["output_tokens"],
            "cache_read_tokens": counts["cache_read_input_tokens"],
            "cache_write_tokens": counts["cache_creation_input_tokens"], "embed_tokens": 0,
            # Legacy NOT NULL column is a known subtotal, never a complete total.
            "cost_usd": round(raw["known_cost_usd"], 6), "raw_usage": raw,
            "cost_version": metering.COST_VERSION,
        })
        from app import llm
        llm.mark_metered(attempts)
    except Exception:
        log.exception("usage: failed to record Anthropic receipts (tenant %s)", tenant_id)


def record_voice_session(tenant_id: str, model: str, seconds: float, *, confirmed: bool,
                         operation: str = "gptlive_session", module: str = "concierge") -> None:
    """One ledger event per GPT-Live session.

    `seconds` is the provider's own cumulative duration. Only a session.closed from the
    provider confirms it; without that the figure is a lower bound (we may have missed the
    tail), so the event is marked incomplete rather than presented as exact. A session that
    was created has billed at least VOICE_INIT_SECONDS, so a missing figure never records zero.
    """
    try:
        reported = float(seconds or 0)
        billed = reported if confirmed else max(reported, float(VOICE_INIT_SECONDS))
        c = cost_usd("openai", model, voice_seconds=billed)
        db.insert_usage_event({
            "tenant_id": tenant_id, "provider": "openai", "model": model,
            "operation": operation, "module": module,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "embed_tokens": 0,
            "cost_usd": round(c or 0, 6),
            "raw_usage": {"unit": "voice_seconds", "seconds": billed, "provider_reported_seconds": reported,
                          "init_seconds": VOICE_INIT_SECONDS,
                          "rate_per_minute": VOICE_PER_MINUTE.get(("openai", _alias(model))),
                          "known_cost_usd": c, "complete": bool(confirmed and c is not None),
                          "final_usage_confirmed": bool(confirmed),
                          "source": metering.VOICE_SOURCE},
            "cost_version": metering.VOICE_COST_VERSION,
            "email_log_id": None,
        })
    except Exception:  # metering must never break a voice session
        log.exception("usage: failed to record voice session (tenant %s)", tenant_id)


def record_voyage(tenant_id: str, model: str, total_tokens: int, *, operation: str,
                  module: str = "email") -> None:
    record(tenant_id, "voyage", model, operation, embed_tokens=total_tokens or 0, module=module)


# ── Plans ─────────────────────────────────────────────────────────────────────
# Allowances sized to real SMB inbound-customer-email volume (Kris 2026-07-04):
# a solo trade sees ~60-200 handled emails/month, a front-office inbox ~200-600,
# a busy multi-mailbox SMB ~600-1,500, a small branch ~1,500-4,000. Pilot matches
# the 14-day trial's 100-email pot so the free story is one number. KEEP IN STEP
# with the plans.py CATALOG `emails` strings — the catalog is what marketing and
# the concierge quote; this table is what the meter displays/enforces.
# 'enterprise' is the multi-mailbox tier (one Account-type switch in the operator
# console — docs/architecture/enterprise-multi-mailbox.md D2). Allowance is bespoke per MSA;
# the number here is a generous display placeholder. price_gbp 0 because revenue
# comes from tenants.contract_monthly_gbp, set by the operator.
PLANS: dict[str, dict] = {
    "pilot":        {"label": "Pilot",        "monthly_emails": 100,    "price_gbp": 0},
    "email":        {"label": "Starter",      "monthly_emails": 300,    "price_gbp": 39},   # renamed Email→Starter in the 2026-07-24 two-tier collapse
    "starter":      {"label": "Pro",          "monthly_emails": 750,    "price_gbp": 99},   # renamed Core→Pro (same key, same price)
    "concierge":    {"label": "Pro + Concierge", "monthly_emails": 750, "price_gbp": 249},  # 2026-09-25: Pro + the business Concierge
    "professional": {"label": "Core + your OS",   "monthly_emails": 2_000,  "price_gbp": 248},  # PARKED 2026-07-24 (off the public catalog; key kept for legacy subscriptions)
    "scale":        {"label": "Scale",        "monthly_emails": 6_000,  "price_gbp": 799},  # RETIRED 2026-07-11 (off the public catalog; key kept for legacy subscriptions)
    "enterprise":   {"label": "Enterprise",   "monthly_emails": 20_000, "price_gbp": 0},    # PARKED 2026-07-24 — bigger operations route to hello@blam8.com
}
DEFAULT_PLAN = "pilot"

# Each PAID extra mailbox brings the Email tier's per-mailbox allowance with it
# (Kris 2026-07-04): £39 buys a mailbox's worth of service wherever it appears —
# as the Email tier or as the add-on.
EXTRA_MAILBOX_EMAILS = 300

# Each PAID additional branch (£149 — Julius 2026-07-11) brings a branch's worth
# of email volume with it, and its own mailbox (see _billable_depots below —
# the mailbox allowance grows in step, exactly like billing).
EXTRA_DEPOT_EMAILS = 1_000


def plan_for(tenant: dict | None) -> dict:
    key = (tenant or {}).get("plan") or DEFAULT_PLAN
    return PLANS.get(key, PLANS[DEFAULT_PLAN])


def _billable_depots(tenant: dict, tenant_id: str, client=None) -> int:
    """Branches the additional-branch add-on charges for — mirrors
    app/billing.billable_depots (kept in step, same rules): live branches
    beyond the plan's included first branch, on an active subscription;
    Enterprise (unlimited) bills none."""
    if tenant.get("subscription_status") not in ("active", "trialing"):
        return 0
    included = plans.included_depots(tenant.get("plan"))
    if included is None:
        return 0
    return max(0, len(db.list_branches(tenant_id, client=client)) - int(included))


def extra_depot_allowance(tenant: dict | None, tenant_id: str, client=None) -> int:
    """+1,000 emails/month per BILLABLE additional branch — exactly the branches
    app/billing.sync_depot_addon charges for."""
    return _billable_depots(tenant or {}, tenant_id, client=client) * EXTRA_DEPOT_EMAILS


def extra_mailbox_allowance(tenant: dict | None, tenant_id: str, client=None) -> int:
    """+300 emails/month per BILLABLE extra mailbox — exactly the mailboxes
    app/billing.sync_mailbox_addon charges for: non-grandfathered, beyond the
    plan's included count (which grows by one per billable branch — the £149
    branch price includes the branch's own mailbox), on an active subscription.
    Grandfathered (free-forever) connections and Enterprise (unlimited
    included) add nothing."""
    t = tenant or {}
    if t.get("subscription_status") not in ("active", "trialing"):
        return 0
    included = plans.included_mailboxes(t.get("plan"))
    if included is None:  # Enterprise — unlimited mailboxes, no add-on
        return 0
    included = int(included) + _billable_depots(t, tenant_id, client=client)
    billable = sum(1 for m in db.list_mailboxes(tenant_id, client=client)
                   if not m.get("grandfathered"))
    return max(0, billable - included) * EXTRA_MAILBOX_EMAILS


# ── Billing-month window (per-tenant, anchored to billing_started_at) ─────────
def _add_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _clamp_day(y: int, m: int, d: int) -> int:
    return min(d, monthrange(y, m)[1])


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_billing_period(started_at: datetime, now: datetime) -> tuple[datetime, datetime]:
    """The [start, end) monthly window containing `now`, anchored to started_at's
    day-of-month. Handles short months by clamping (e.g. a 31st anchor -> Feb 28)."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    anchor = started_at.day
    y, m = now.year, now.month
    start = now.replace(day=_clamp_day(y, m, anchor), hour=0, minute=0, second=0, microsecond=0)
    if start > now:                       # anchor day this month is still ahead — step back one
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        start = start.replace(year=y, month=m, day=_clamp_day(y, m, anchor))
    ey, em = _add_month(start.year, start.month)
    end = start.replace(year=ey, month=em, day=_clamp_day(ey, em, anchor))
    return start, end


def handled_emails(events: list[dict]) -> int:
    """Count HANDLED EMAILS — the sold unit, defined once in src/app/trial.py: ONE
    inbound message the agent drafted a reply to. Distinct email_log_id among
    'draft' events (an email drafted twice, e.g. a booking-details refresh, still
    counts once — matching the trial counter); a draft event with no linked email
    row still counts one. 'redraft' is a separate operation and never counts."""
    ids: set[str] = set()
    unlinked = 0
    for e in events:
        if e.get("operation") != "draft":
            continue
        lid = e.get("email_log_id")
        if lid:
            ids.add(lid)
        else:
            unlinked += 1
    return len(ids) + unlinked


# ── Summary for the Usage screen (current billing period, ALL usage) ──────────
# The owner's chats with the assistant — every mode (Kris 2026-09-13: "I plan
# to cap Concierge usage… we need to track that and portal use"). The daily
# fair-use cap (src/app/fairuse.py) meters business chat only; the count here is
# every turn, so the owner sees what they actually use.
ASSISTANT_OPERATIONS = frozenset({"concierge_help", "concierge_business", "concierge_fix",
                                  "concierge_onboard", "concierge_task"})


def _london_today_iso() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def _assistant_and_portal(tenant: dict, tenant_id: str, events: list[dict],
                          period_start: datetime, client=None) -> dict:
    """Turns and cost on the two surfaces that can run up a bill besides
    email, from the events already loaded — plus the portal's bookings this
    period (one count query, best-effort) and the assistant's day against
    its fair-use cap."""
    from app import fairuse
    today = _london_today_iso()
    a_turns = a_today = p_turns = 0
    a_cost = p_cost = 0.0
    for e in events:
        op = event_operation(e)
        completed = e.get("operation") != "llm_failure"
        if op in ASSISTANT_OPERATIONS:
            a_turns += int(completed)
            a_cost += float(e.get("cost_usd") or 0)
            if completed and str(e.get("created_at") or "")[:10] == today:
                a_today += 1
        elif source_for(e.get("module"), op) == "portal":
            p_turns += int(completed)
            p_cost += float(e.get("cost_usd") or 0)
    try:
        bookings = db.count_bookings_since(tenant_id, period_start.isoformat(), source="portal",
                                           client=client)
    except Exception:
        bookings = 0
    try:
        day_cap = fairuse.daily_cap(tenant)
    except Exception:
        day_cap = None
    return {"assistant_turns": a_turns, "assistant_today": a_today, "assistant_day_cap": day_cap,
            "assistant_cost_usd": a_cost, "portal_turns": p_turns, "portal_bookings": bookings,
            "portal_cost_usd": p_cost}


def cost_complete(event: dict) -> bool:
    raw = event.get("raw_usage")
    return bool(isinstance(raw, dict) and raw.get("complete"))


def summarize(tenant_id: str, client=None) -> dict:
    tenant = db.get_tenant(tenant_id, client=client) or {}
    started = _parse_dt(tenant.get("billing_started_at") or tenant.get("created_at") or datetime.now(timezone.utc))
    period_start, period_end = current_billing_period(started, datetime.now(timezone.utc))
    events = db.list_usage_events(tenant_id, since=period_start.isoformat(), client=client)

    plan = plan_for(tenant)
    allowance = (plan["monthly_emails"]
                 + extra_mailbox_allowance(tenant, tenant_id, client=client)
                 + extra_depot_allowance(tenant, tenant_id, client=client))
    emails = handled_emails(events)
    cost = sum(float(e.get("cost_usd") or 0) for e in events)
    in_tok = sum(e.get("input_tokens") or 0 for e in events)
    out_tok = sum(e.get("output_tokens") or 0 for e in events)
    emb_tok = sum(e.get("embed_tokens") or 0 for e in events)

    by_op: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    for e in events:
        src = source_for(e.get("module"), event_operation(e))
        for bucket in (by_op.setdefault(e.get("operation") or "?",
                                        {"events": 0, "input": 0, "output": 0, "cost_complete": True,
                                         "embed": 0, "cost": 0.0, "source": src}),
                       by_source.setdefault(src,
                                            {"events": 0, "input": 0, "output": 0, "cost_complete": True,
                                             "embed": 0, "cost": 0.0})):
            bucket["cost_complete"] = bucket["cost_complete"] and cost_complete(e)
            bucket["events"] += 1
            bucket["input"] += e.get("input_tokens") or 0
            bucket["output"] += e.get("output_tokens") or 0
            bucket["embed"] += e.get("embed_tokens") or 0
            bucket["cost"] += float(e.get("cost_usd") or 0)

    pct = (emails / allowance * 100) if allowance else 0.0
    credits = int(tenant.get("email_credits") or 0)
    return {
        "plan": plan,
        # Prepaid top-up emails, held OUTSIDE the allowance percentage on
        # purpose: the bar should still read 100% when the plan is spent, and
        # the credit shown beside it as the headroom that is keeping the agent
        # drafting. Rolling it into `allowance` would hide the fact that they
        # are now burning something they paid extra for.
        "credits": credits,
        "period_start": period_start, "period_end": period_end,
        "emails": emails, "allowance": allowance, "pct": pct,
        "cost_usd": cost, "cost_gbp": cost * settings.usd_gbp_rate,
        "cost_complete": all(cost_complete(e) for e in events),
        "input_tokens": in_tok, "output_tokens": out_tok, "embed_tokens": emb_tok,
        "total_events": len(events), "by_op": by_op, "by_source": by_source,
        "over": bool(allowance) and emails >= allowance,
        "warn": pct >= 80,
        **_assistant_and_portal(tenant, tenant_id, events, period_start, client=client),
    }


def quota_status(tenant_id: str, client=None) -> dict:
    """Where the tenant stands against their allowance. `blocked` means the plan
    is spent, there is no credit behind it, AND enforcement is on."""
    s = summarize(tenant_id, client=client)
    out_of_road = s["over"] and s["credits"] <= 0
    return {"over": s["over"], "pct": s["pct"], "credits": s["credits"],
            "enforcement": settings.usage_enforcement,
            "blocked": out_of_road and settings.usage_enforcement == "hard"}


def claim_draft(tenant_id: str, email_log_id: str | None = None, client=None) -> dict:
    """Ask permission to draft one email, and take the payment for it if that is
    what it costs. Called BEFORE the model is, so a refusal costs nothing.

    Returns {"allowed": bool, "used_credit": bool, "credits_left": int|None}.

    Two rules matter more than the rest:

      * Under soft enforcement nothing is ever refused AND NO CREDIT IS SPENT.
        Going over the allowance costs a tenant nothing today, so burning
        prepaid credit on those emails would be charging for something they
        were already getting free.
      * Credit is only touched once the plan allowance is genuinely exhausted,
        and it is spent one email at a time by a database function, so two
        drafts racing for the last credit cannot both win it."""
    # Soft enforcement answers "yes" without asking anything. This sits on the
    # drafting path for EVERY inbound email, so while the answer cannot be no it
    # must not cost a database round-trip to reach — and it must not spend
    # credit either, since nothing is being refused for the credit to buy back.
    if settings.usage_enforcement != "hard":
        return {"allowed": True, "used_credit": False, "credits_left": None}
    s = summarize(tenant_id, client=client)
    if not s["over"]:
        return {"allowed": True, "used_credit": False, "credits_left": s["credits"]}
    left = db.spend_email_credit(tenant_id, email_log_id)
    if left < 0:
        return {"allowed": False, "used_credit": False, "credits_left": 0}
    return {"allowed": True, "used_credit": True, "credits_left": left}


# ── Operator console — cross-tenant margin (admin-only; uses service role) ────
_OP_LOOKBACK_DAYS = 32   # >= the longest billing month, so one query covers every tenant's window


def revenue_gbp(tenant: dict) -> float:
    """Monthly revenue: the negotiated contract value if set, else the plan list price."""
    c = tenant.get("contract_monthly_gbp")
    return float(c) if c is not None else float(plan_for(tenant).get("price_gbp") or 0)


def _margin_status(revenue: float, cost_gbp: float) -> str:
    if revenue <= 0:
        return "pilot"                       # free/unpriced — cost is expected burn
    if cost_gbp > revenue:
        return "underwater"
    if cost_gbp > revenue * 0.5:
        return "thin"
    return "healthy"


def fmt_review_time(seconds: float | None) -> str | None:
    """'42s' / '1m 05s' / '2h 51m' — the one HRT formatter (inbox month faces
    and the operator console). None stays None so callers just omit the chip."""
    if seconds is None:
        return None
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def operator_summary() -> dict:
    """Per-tenant margin for each tenant's CURRENT billing month, plus platform totals.
    One cross-tenant query, then bucketed per tenant by its own billing window."""
    now = datetime.now(timezone.utc)
    # HRT (Kris 2026-08-31): all-time per-tenant review-time aggregates — the
    # "how much human time does an email actually take" number the pitch quotes.
    hrt_all = db.review_time_stats()
    # Time saved (Kris 2026-09-24): by-hand minutes BLAM8 took off the owner,
    # net of the human time spent — src/app/time_saved.py. All-time, operator-only.
    from app import time_saved
    saved_all = time_saved.summary()
    events = db.usage_events_since_all((now - timedelta(days=_OP_LOOKBACK_DAYS)).isoformat())
    by_tenant: dict[str, list] = {}
    for e in events:
        by_tenant.setdefault(e["tenant_id"], []).append(e)

    rows, tot_rev, tot_cost, tot_emails = [], 0.0, 0.0, 0
    for t in db.list_all_tenants():
        started = _parse_dt(t.get("billing_started_at") or t.get("created_at") or now)
        win_start, win_end = current_billing_period(started, now)
        evs = [e for e in by_tenant.get(t["id"], []) if _parse_dt(e["created_at"]) >= win_start]
        cost_gbp = sum(float(e.get("cost_usd") or 0) for e in evs) * settings.usd_gbp_rate
        emails = handled_emails(evs)
        rev = revenue_gbp(t)
        # Invoice-processed revenue (directive §8.2): the TENANT'S money, not ours —
        # shown as an engagement/value signal beside MRR and LLM cost, never summed
        # into platform revenue. Best-effort: a read failure hides the number, not
        # the page.
        try:
            collected = db.invoice_summary(
                t["id"], since_iso=win_start.isoformat())["collected"]
        except Exception:
            collected = None
        try:
            quoted = db.quote_summary(
                t["id"], since_iso=win_start.isoformat())["quoted"]
        except Exception:
            quoted = None
        # Journey flags (src/app/journey.py): surface a signup that never finished
        # the wizard right on the overview list — the silent-churn case that
        # otherwise only shows up in a hand-run DB query. Best-effort.
        try:
            from app import journey
            stalled = journey.stalled_step(t)
            lv = db.latest_visit(t["id"])
            last_seen = lv["last_seen_at"] if lv else None
        except Exception:
            stalled, last_seen = None, None
        rows.append({
            "tenant_id": t["id"], "name": t.get("name") or "(unnamed)",
            "stalled_at": stalled, "last_seen": last_seen,
            "signed_up": t.get("created_at"),
            "plan": plan_for(t)["label"], "reporting_tier": t.get("reporting_tier") or "standard",
            "period_start": win_start, "period_end": win_end, "emails": emails,
            "cost_complete": all(cost_complete(e) for e in evs),
            "revenue_gbp": rev, "cost_gbp": cost_gbp, "margin_gbp": rev - cost_gbp,
            "margin_pct": ((rev - cost_gbp) / rev * 100) if rev > 0 else None,
            "status": _margin_status(rev, cost_gbp),
            "contract_set": t.get("contract_monthly_gbp") is not None,
            "invoices_collected_gbp": collected,
            "quoted_gbp": quoted,
            "hrt": fmt_review_time(h["total"] / h["n"]) if (h := hrt_all.get(t["id"])) else None,
            "hrt_n": h["n"] if h else 0,
            "saved": fmt_review_time(ts["total_s"]) if (ts := saved_all.get(t["id"])) and ts["total_s"] else None,
        })
        tot_rev += rev; tot_cost += cost_gbp; tot_emails += emails
    rows.sort(key=lambda r: r["margin_gbp"])     # underwater first
    hrt_n = sum(m["n"] for m in hrt_all.values())
    hrt_total = sum(m["total"] for m in hrt_all.values())
    return {
        "cost_complete": all(r["cost_complete"] for r in rows),
        "rows": rows, "tenant_count": len(rows), "total_emails": tot_emails,
        "total_revenue_gbp": tot_rev, "total_cost_gbp": tot_cost,
        "total_margin_gbp": tot_rev - tot_cost,
        "total_margin_pct": ((tot_rev - tot_cost) / tot_rev * 100) if tot_rev > 0 else None,
        "hrt": {"n": hrt_n, "avg": fmt_review_time(hrt_total / hrt_n) if hrt_n else None,
                "total": fmt_review_time(hrt_total) if hrt_n else None},
        "saved": _saved_view(saved_all.values()),
    }


def _saved_view(tallies) -> dict | None:
    """Sum time_saved tallies into the console's shape (None when nothing)."""
    tallies = list(tallies)
    e_n = sum(t["emails"]["n"] for t in tallies)
    e_s = sum(t["emails"]["s"] for t in tallies)
    a_n = sum(t["actions"]["n"] for t in tallies)
    a_s = sum(t["actions"]["s"] for t in tallies)
    if not (e_s or a_s):
        return None
    return {"total": fmt_review_time(e_s + a_s),
            "emails_n": e_n, "emails": fmt_review_time(e_s),
            "actions_n": a_n, "actions": fmt_review_time(a_s)}


def operator_tenant_detail(tenant_id: str) -> dict:
    """Drill-down for one tenant (service role): billing-window usage + margin + recent events."""
    t = db.get_tenant(tenant_id) or {}
    s = summarize(tenant_id)                      # client=None -> service role (admin cross-tenant)
    rev = revenue_gbp(t)
    quality = db.email_log_quality(tenant_id, s["period_start"].isoformat())
    # HRT — all-time for this tenant (the quoting number wants the full record).
    h = db.review_time_stats(tenant_id).get(tenant_id)
    quality["hrt"] = {"n": h["n"], "avg": fmt_review_time(h["total"] / h["n"]),
                      "total": fmt_review_time(h["total"])} if h else None
    from app import time_saved
    quality["saved"] = _saved_view(time_saved.summary(tenant_id).values())
    # Conversations the tenant removed from their inbox (reset / Ignore & Remove)
    # delete the email_log rows but never the usage events — so quality can only
    # describe what survives. Say so instead of silently under-reporting.
    quality["removed"] = max(0, s["emails"] - quality["handled"])
    s.update({
        "tenant_id": tenant_id, "name": t.get("name"),
        "revenue_gbp": rev, "contract_monthly_gbp": t.get("contract_monthly_gbp"),
        "reporting_tier": t.get("reporting_tier") or "standard",
        "plan_key": t.get("plan") or DEFAULT_PLAN,           # the Account type card's state
        "mailbox_count": len(db.list_mailboxes(tenant_id)),  # downgrade guard (enterprise)
        "margin_gbp": rev - s["cost_gbp"], "status": _margin_status(rev, s["cost_gbp"]),
        "quality": quality,
        "recent": db.list_usage_events(tenant_id, limit=30),
        "trial": trial.evaluate(t),                          # free-trial state (lock + email cap)
    })
    # Tenant-side invoicing revenue (directive §8.1) — this billing window plus
    # all-time, from the invoices table directly (no reporting engine).
    try:
        s["invoices"] = db.invoice_summary(tenant_id,
                                           since_iso=s["period_start"].isoformat())
        s["invoices_all_time"] = db.invoice_summary(tenant_id)
    except Exception:
        s["invoices"] = s["invoices_all_time"] = None
    # Quote funnel (docs/modules/module-quotes.md Phase 2) — quoted vs won is the
    # engagement signal upstream of the invoice numbers above.
    try:
        s["quotes"] = db.quote_summary(tenant_id,
                                       since_iso=s["period_start"].isoformat())
        s["quotes_all_time"] = db.quote_summary(tenant_id)
    except Exception:
        s["quotes"] = s["quotes_all_time"] = None
    return s
