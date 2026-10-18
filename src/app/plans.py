"""Plan catalog — the single source of truth for what BLAM8 sells.

Both the billing pricing page (src/app/billing_routes.py) and the website concierge's
injected facts (src/app/platform_facts.py) render from this one list, so a price can
never drift between what we charge and what the assistant quotes. Stripe price ids
are attached at request time from settings (env-driven, test vs live) and are NOT
part of the catalog itself.
"""
import re

from app.config import settings

# Display catalog. `price` is the headline number, `per` its unit, `emails` the
# monthly allowance, `summary`/`features` the marketing copy. No price ids here —
# `billing_plans()` attaches those. `key` ties a row to its Stripe price-id env.
#
# Tier ladder (Julius, 2026-07-25 — supersedes the 07-24 "full loop on both
# tiers" collapse): Starter £39 = email + scheduling; Pro £99 adds the
# self-filling CRM. The CRM still PRE-FILLS for every tenant (zero-LLM ingest,
# src/app/pipeline.py) — below Pro it's the upgrade teaser, visible as counts on the
# locked modules card but not openable. The keys keep their historical names
# ('email' = Starter £39, 'starter' = Pro £99) because Stripe price ids, live
# tenants.plan values and the entitlement map all reference them; only display
# copy and bundles changed. `public: False` rows are PARKED, not deleted:
# professional (Core + OS) and enterprise stay valid for legacy/operator-set
# subscriptions and may return, but no public surface sells them.
CATALOG = [
    {"key": "email", "name": "Starter", "price": "£39", "per": "per month",
     "summary": "Replies & booking, for one-person businesses", "emails": "300 emails/month",
     "included_mailboxes": 1, "public": True, "highlight": True,
     "features": ["AI-drafted replies in your voice",
                  "Knowledge base — your prices, policies, FAQs",
                  "Conversation threading",
                  "Booking details gathered & pinned",
                  "Scheduling & capacity diary",
                  "Calendar sync — Outlook & Google",
                  "Draft-only by default"]},
    {"key": "starter", "name": "Pro", "price": "£99", "per": "per month",
     "summary": "The full loop — replies, booking and the self-filling CRM",
     "emails": "750 emails/month",
     "included_mailboxes": 1, "public": True,
     "features": ["Everything in Starter",
                  "Self-filling CRM: contacts, deals, promises",
                  "Follow-up sequences & customer-mood flags",
                  "Higher email volume — 750 emails/month",
                  "Additional mailboxes £39/mo each (+300 emails/month)",
                  "Priority support"]},
    # Pro + Concierge (Kris 2026-09-25): Pro plus the business Concierge — her
    # chat, cards and live voice. Below it there is no Concierge at all (App
    # help lives in the search). Same allowance as Pro, "for now".
    {"key": "concierge", "name": "Pro + Concierge", "price": "£249", "per": "per month",
     "summary": "Pro, plus the Concierge — ask, act and talk to your business",
     "emails": "750 emails/month",
     "included_mailboxes": 1, "public": True,
     "features": ["Everything in Pro",
                  "The Concierge: ask anything about your business",
                  "She drafts, moves, chases and books — you confirm",
                  "Live voice: talk to her hands-free",
                  "The morning brief opens straight into her"]},
    {"key": "professional", "name": "Core + your OS", "price": "£248", "per": "per month",
     "summary": "£99 Core + £149 your Operating System module",
     "emails": "2,000 emails/month", "included_mailboxes": 2, "public": False,
     "features": ["Everything in Core", "2 mailboxes included (+£39/mo each extra, each adding 300 emails/month)",
                  "Your Operating System module"]},
    {"key": "enterprise", "name": "Enterprise", "price": "Custom", "per": "negotiated",
     "summary": "Franchise groups & large estates · MSA invoice billing",
     "emails": "Custom allowance", "included_mailboxes": None, "public": False,
     "features": ["Everything in Core + your OS", "Unlimited depots & mailboxes",
                  "Group oversight across franchisees", "Custom SLAs & onboarding",
                  "Invoice billing (Net 30/60)"]},
]

# Retired tiers (2026-07-11): Scale (£799) came off the menu when pricing went
# line-item — its story is now the branch ladder (Core + OS + £149/additional
# branch). The plan KEY stays valid everywhere (usage.PLANS, BASE_PLAN_MODULES,
# price_to_plan) so an operator-set or legacy subscription keeps working; only
# the public catalog stopped selling it. included_mailboxes() consults this map
# so a legacy tenant keeps its allowance.
LEGACY_INCLUDED_MAILBOXES = {"scale": 5}

# Add-on: extra mailboxes beyond a plan's included allowance. One flat price per
# extra mailbox per month, billed as a Stripe subscription-item quantity that
# app/billing.sync_mailbox_addon keeps in step with the tenant's live mailbox
# count. Enterprise (included_mailboxes=None) is unlimited, so the add-on never
# applies. Its Stripe price id is env-driven (test vs live), like the tiers.
EXTRA_MAILBOX = {
    "name": "Extra mailbox", "price": "£39", "per": "per mailbox / month",
    "price_id_attr": "stripe_extra_mailbox_price_id",
}

# The tiers whose monthly price is a single flat number, so a quote may safely
# ADD to it (app/billing.mailbox_addon_quote). Deliberately just the two public
# tiers: "Core + your OS" heads at £248 on the assumption of a £149 OS module,
# but Transport's is £199 — a total built from that row would understate a
# transport operator's bill by £50. Enterprise is negotiated and retired tiers aren't in
# the catalog at all. Anything absent here quotes the add-on price alone rather
# than risk a wrong total.
FLAT_PRICED_TIERS = ("email", "starter", "concierge")

# Add-on: an email top-up (Kris 2026-09-01). Prepaid emails, bought ONE-OFF
# rather than as a subscription item — which is what makes it safe. A recurring
# top-up is either dominated by the extra mailbox (same £39, and that one brings
# a mailbox with it) or, priced below it, undercuts Pro: Starter £39 plus two
# cheap monthly top-ups would beat £99 for 750. A one-off competes with nothing,
# because it doesn't recur.
#
# The rate is Starter's, deliberately: £39 for 300 emails is what a mailbox's
# worth of service has cost since 2026-07-04, as the Starter tier and as the
# extra-mailbox add-on. Quoting a third number here would invite the question of
# which one is the real price.
#
# Credit is spent only after the plan allowance is exhausted, and carries over
# until used — "one-off" describes the purchase, not the credit's lifetime.
EMAIL_TOPUP = {
    "name": "Email top-up", "price": "£39", "emails": 300,
    "per": "one-off", "price_id_attr": "stripe_email_topup_price_id",
}

# Add-on: additional branches (2026-07-11, Julius's pick — the multi-branch /
# franchise expansion price). The FIRST branch is part of the plan; each branch
# beyond it is £149/mo and INCLUDES its own mailbox (the mailbox add-on's
# included count grows with billable branches — app/billing.sync_mailbox_addon)
# and +1,000 emails/month (usage.EXTRA_DEPOT_EMAILS). Billed exactly like the
# mailbox add-on: a Stripe subscription-item quantity that
# app/billing.sync_depot_addon reconciles to the tenant's live branch count.
# Anchor line: "your OS is already paid for — each extra branch runs for less
# than one admin hour a week." Enterprise (unlimited) never bills it.
EXTRA_DEPOT = {
    "name": "Additional depot", "price": "£149", "per": "per depot / month",
    "price_id_attr": "stripe_extra_depot_price_id",
}
INCLUDED_DEPOTS = 1  # every finite plan includes the first branch


def included_mailboxes(plan_key: str | None) -> int | None:
    """How many mailboxes a plan includes before the add-on bills. None = the
    plan is unlimited (Enterprise). 0 for unknown/free tiers — a pilot connects
    exactly one mailbox through the single-mailbox onboarding flow, not here.
    Retired tiers keep their allowance via LEGACY_INCLUDED_MAILBOXES."""
    for p in CATALOG:
        if p["key"] == plan_key:
            return p.get("included_mailboxes", 0)
    return LEGACY_INCLUDED_MAILBOXES.get(plan_key or "", 0)


def included_depots(plan_key: str | None) -> int | None:
    """How many fleet branches a plan includes before the branch add-on
    bills. None = unlimited (Enterprise); everyone else gets the first branch
    with their plan."""
    return None if plan_key == "enterprise" else INCLUDED_DEPOTS


def _gbp(price: str) -> int | None:
    """The whole-pound figure behind a catalog price string, or None if it isn't
    one ("Custom", or any future format this doesn't recognise). Returning None
    rather than guessing is the point: a quote that can't be built exactly says
    less instead of saying something wrong."""
    m = re.fullmatch(r"£(\d+)", (price or "").strip())
    return int(m.group(1)) if m else None


def plan_price_gbp(plan_key: str | None) -> int | None:
    """The plan's flat monthly price in £, or None when it isn't a single number
    a quote can add to — see FLAT_PRICED_TIERS."""
    if plan_key not in FLAT_PRICED_TIERS:
        return None
    for p in CATALOG:
        if p["key"] == plan_key:
            return _gbp(p["price"])
    return None


def plan_name(plan_key: str | None) -> str | None:
    """The display name for a plan key ('starter' → 'Pro'), or None if unknown."""
    for p in CATALOG:
        if p["key"] == plan_key:
            return p["name"]
    return None


def extra_mailbox_gbp() -> int | None:
    """The extra-mailbox add-on price in £."""
    return _gbp(EXTRA_MAILBOX["price"])


def topup_emails() -> int:
    """How many emails one top-up buys."""
    return int(EMAIL_TOPUP["emails"])


def topup_gbp() -> int | None:
    """What one top-up costs, in £."""
    return _gbp(EMAIL_TOPUP["price"])


def topup_price_id():
    """The Stripe price id for the email top-up, or None if unset — in which
    case nothing offers it for sale, exactly like the other add-ons."""
    return getattr(settings, EMAIL_TOPUP["price_id_attr"], "") or None


def extra_mailbox_price_id():
    """The Stripe price id for the extra-mailbox add-on, or None if unset."""
    return getattr(settings, EXTRA_MAILBOX["price_id_attr"], "") or None


def extra_depot_price_id():
    """The Stripe price id for the additional-branch add-on, or None if unset."""
    return getattr(settings, EXTRA_DEPOT["price_id_attr"], "") or None

_PRICE_ID_ATTR = {
    "email": "stripe_email_price_id",
    "starter": "stripe_starter_price_id",
    "concierge": "stripe_concierge_price_id",
    "professional": "stripe_professional_price_id",
    "scale": "stripe_scale_price_id",
    "enterprise": "stripe_enterprise_price_id",
}

# ── OS module pricing (the line-item menu, Julius 2026-07-10) ─────────────────
# One flat module price with ONE exception: every price must anchor to what the
# module REPLACES (the customer's current spend), and every extra price point is
# a comparison a prospect will litigate. Field OS · Transport replaces an entire
# TMS (Mandata/Microlise start ~£300/mo before per-resource fees) — the only
# module that swallows a whole category of software, so the only one priced
# differently. Keyed by module key ('tms'), not industry, so Transport keeps its
# price under any future industry seed that maps to it.
OS_MODULE_PRICE_GBP_DEFAULT = 149
OS_MODULE_PRICE_GBP = {"tms": 199}
CORE_PRICE_GBP = 99  # keep in step with the CATALOG 'starter' row


def os_module_price_gbp(industry: str | None) -> int:
    """The monthly OS-module price for a tenant's industry (£149, Transport £199)."""
    key = INDUSTRY_MODULE.get((industry or "").lower())
    return OS_MODULE_PRICE_GBP.get(key or "", OS_MODULE_PRICE_GBP_DEFAULT)


# ── Plan-bundled module entitlement (industry-aware) ─────────────────────────
# Email is the always-on base. Scheduling and the self-filling CRM come with
# EVERY paid tier — the loop (reply → book → CRM) is the product, and it is
# identical on both public tiers; Pro differs by volume, mailboxes and support
# only (Julius's landing overhaul, 2026-07-24 — this deliberately supersedes
# the same-day "CRM unlocks at Core" rule in docs/crm-vs-os-architecture.md §1;
# see docs/archive/2026-07/landing-overhaul-2026-07-24.md). The INDUSTRY module is custom-built
# PER VERTICAL — Logistics' is TMS; other verticals get their own as they're
# built — and the tenant's own one is unlocked on the "+ industry" tiers (all
# parked from public sale). So a paid plan gives base modules by tier PLUS the
# tenant's industry module (never another vertical's). Keys match
# price_to_plan() / tenants.plan. Billing reconciles a tenant's modules to this
# when a subscription goes active (app/db.apply_plan_modules).
BASE_PLAN_MODULES: dict[str, set[str]] = {
    "email":        {"scheduling"},          # Starter £39 — email + scheduling
    "starter":      {"scheduling", "crm"},   # Pro £99 — the CRM unlocks here
    "concierge":    {"scheduling", "crm", "concierge"},   # Pro + Concierge £249
    "professional": {"scheduling", "crm"},
    "scale":        {"scheduling", "crm"},
    "enterprise":   {"scheduling", "crm"},
}

# Each vertical's custom industry module: display metadata + the switchboard key.
# A vertical with a built module has an entry here. Add a vertical here when its
# module ships — and only make that industry selectable in onboarding once it is,
# so a tenant never lands on a +industry tier whose module doesn't exist yet.
# industry_module_meta() falls back to a generic placeholder for any vertical not
# listed here (defensive — under that release policy it isn't user-facing).
INDUSTRY_MODULE_META: dict[str, dict] = {
    "logistics": {
        "key": "tms", "label": "Field OS · Transport", "name": "Field OS · Transport",
        "features": ["Fleet management", "Pallet capacity tracking",
                     "Vehicle-day booking", "Route capacity"],
    },
    "trades": {
        "key": "fsm", "label": "Field OS · Trades", "name": "Field OS · Trades",
        "features": ["Engineer scheduling & dispatch", "Skill-matched booking",
                     "Emergency triage", "Instant quotes", "Job invoicing",
                     "Recurring service reminders"],
    },
    # Practice OS (docs/os/practice.md §10.2) — one module key ("practice") shared by
    # every appointment vertical, wired exactly like tms/fsm: INDUSTRY_MODULE derives
    # {dentist,salon,physio,...: "practice"} and MANAGED_MODULES picks it up
    # automatically via dict.fromkeys(INDUSTRY_MODULE.values()).
    # PRICING (decided 2026-07-10): the line-item menu — £99 Core + £149 module
    # (billed on stripe_os_price_id, £248 combined). See OS_MODULE_PRICE_GBP.
    "dentist": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Patient history that fills itself in", "Recall cadence per service"],
    },
    "salon": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic rebooking nudges in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Client history that fills itself in", "Rebook cadence per service"],
    },
    "physio": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Patient history that fills itself in", "Recall cadence per service"],
    },
    "chiropractor": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Patient history that fills itself in", "Recall cadence per service"],
    },
    "vet": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Patient history that fills itself in", "Recall cadence per service"],
    },
    "tutor": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic rebooking nudges in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Client history that fills itself in", "Rebook cadence per service"],
    },
    "coach": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic rebooking nudges in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Client history that fills itself in", "Rebook cadence per service"],
    },
    "personal_trainer": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic rebooking nudges in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Client history that fills itself in", "Rebook cadence per service"],
    },
    "therapist": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Client history that fills itself in", "Recall cadence per service"],
    },
    "clinic": {
        "key": "practice", "label": "Practice OS", "name": "Practice OS",
        "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
                     "No-show prediction + extra reminders",
                     "Patient history that fills itself in", "Recall cadence per service"],
    },
    # Storefront OS (docs/os/storefront.md §10) — one module key ("storefront")
    # shared by every shop vertical, wired exactly like tms/fsm/practice:
    # INDUSTRY_MODULE derives {farm_shop,deli,bakery,...: "storefront"} and
    # MANAGED_MODULES picks it up via dict.fromkeys(INDUSTRY_MODULE.values()).
    # Slots on the professional tier at the standard OS module price (£99 + £149
    # = £248, stripe_os_price_id) — no new billing code (§10). Some listed features (OCR / best-before /
    # POS sync) are the OS's later phases; the tier copy previews the whole OS.
    "farm_shop": {
        "key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
        "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                     "Best-before specials to past buyers", "Stock levels & reorder alerts",
                     "POS sync (Square/Shopify)"],
    },
    "deli": {
        "key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
        "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                     "Best-before specials to past buyers", "Stock levels & reorder alerts",
                     "POS sync (Square/Shopify)"],
    },
    "bakery": {
        "key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
        "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                     "Best-before specials to past buyers", "Stock levels & reorder alerts",
                     "POS sync (Square/Shopify)"],
    },
    "wine_merchant": {
        "key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
        "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                     "Best-before specials to past buyers", "Stock levels & reorder alerts",
                     "POS sync (Square/Shopify)"],
    },
    "nursery": {
        "key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
        "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                     "Best-before specials to past buyers", "Stock levels & reorder alerts",
                     "POS sync (Square/Shopify)"],
    },
    # Assets OS (docs/os/assets.md §10) — one module key ("assets") shared by every
    # hire vertical, wired exactly like tms/fsm/practice/storefront: INDUSTRY_MODULE
    # derives {lettings,hire,...: "assets"} and MANAGED_MODULES picks it up. Slots on
    # the professional tier at the standard OS module price (£99 + £149 = £248,
    # stripe_os_price_id) — no new billing code. Deposits / condition photo-diffs are
    # the OS's later phases; the tier copy previews the whole OS.
    "lettings": {
        "key": "assets", "label": "Assets OS · Lettings", "name": "Assets OS · Lettings",
        "features": ["Unit availability & atomic booking", "Deposits (take, hold, refund, deduct)",
                     "Condition photo-diffs & damage claims", "Handover & return board",
                     "Maintenance scheduling"],
    },
    "hire": {
        "key": "assets", "label": "Assets OS · Hire", "name": "Assets OS · Hire",
        "features": ["Fleet-of-units availability", "Deposits & damage claims",
                     "Condition photo-diffs", "Off-hire & return board", "Service scheduling"],
    },
    # Wholesale OS (docs/os/wholesale.md §10) — one module key ("wholesale"),
    # the standard £149 OS module on the line-item menu (os_module_price_gbp
    # default). billing_plans("wholesale") renders "Core + Wholesale OS" with
    # this feature list; INDUSTRY_MODULE / MANAGED_MODULES derive automatically.
    "wholesale": {
        "key": "wholesale", "label": "Wholesale OS", "name": "Wholesale OS",
        "features": ["Standing-order automation", "Credit-limit-aware ordering",
                     "Allocation & fulfilment planning", "Order book that fills itself in"],
    },
    # Events OS (docs/os/events.md §10) — one module key ("events") shared by every
    # event vertical, wired exactly like tms/fsm/practice/storefront/assets:
    # INDUSTRY_MODULE derives {events,caterer,wedding_planner: "events"} and
    # MANAGED_MODULES picks it up via dict.fromkeys(INDUSTRY_MODULE.values()). Slots on
    # the professional tier at the standard OS module price (£99 + £149 = £248,
    # stripe_os_price_id) — no new billing code. The multi-stakeholder chase (Phase B),
    # run-of-show (C) and integrations (D) are the OS's later phases; the tier copy
    # previews the whole OS.
    "events": {
        "key": "events", "label": "Events OS", "name": "Events OS",
        "features": ["Guest & dietary tracking that fills itself in", "Multi-stakeholder chase",
                     "Timings & run-of-show", "Supplier & staff coordination",
                     "This-week events board"],
    },
    "caterer": {
        "key": "events", "label": "Events OS", "name": "Events OS",
        "features": ["Covers & dietary matrix from the inbox", "Final-numbers chase",
                     "Menu & service timings", "Supplier & staff coordination",
                     "This-week events board"],
    },
    "wedding_planner": {
        "key": "events", "label": "Events OS", "name": "Events OS",
        "features": ["Every stakeholder tracked per event", "Multi-supplier chase to the run sheet",
                     "Guest & dietary tracking", "Timings & run-of-show",
                     "This-week events board"],
    },
}

# Derived: tenants.industry → switchboard module key (the entitlement map).
INDUSTRY_MODULE: dict[str, str] = {k: v["key"] for k, v in INDUSTRY_MODULE_META.items()}

_MODULE_IN_DEV = {
    "key": None, "label": "your industry module", "name": "your sector's module",
    "live": False, "features": ["Your sector's custom module"],
}


def industry_module_meta(industry: str | None) -> dict:
    """Display metadata for a vertical's industry module, or a generic
    placeholder for verticals without one yet (live=False)."""
    meta = INDUSTRY_MODULE_META.get((industry or "").lower())
    return {**meta, "live": True} if meta else dict(_MODULE_IN_DEV)

# Tiers that include the tenant's industry module ("Core + <your industry>").
INDUSTRY_TIERS = ("professional", "scale", "enterprise")

# Every switchboard module the entitlement manages = base + all industry modules.
MANAGED_MODULES = ("scheduling", "crm", "concierge") + tuple(dict.fromkeys(INDUSTRY_MODULE.values()))


def modules_for(plan_name: str | None, industry: str | None = None) -> set[str]:
    """The switchboard modules a plan unlocks for a tenant in `industry`: base
    modules by tier, plus the tenant's OWN industry module on the +industry tiers
    (never another vertical's). Empty for unknown/free tiers."""
    mods = set(BASE_PLAN_MODULES.get(plan_name or "", set()))
    if plan_name in INDUSTRY_TIERS:
        industry_module = INDUSTRY_MODULE.get((industry or "").lower())
        if industry_module:
            mods.add(industry_module)
    return mods


def billing_plans(industry: str | None = None) -> list[dict]:
    """The PUBLIC catalog with Stripe price ids attached — the three loop tiers
    (Starter, Pro, Pro + Concierge). Parked tiers (professional, enterprise) are
    filtered out: no public surface sells them, though their keys stay
    valid everywhere (a legacy subscription keeps its modules, allowance and
    Customer Portal path). `industry` is accepted for call-site compatibility;
    the loop tiers are identical for every industry."""
    del industry  # loop tiers are industry-agnostic; parked tiers were not
    out = []
    for p in CATALOG:
        if not p.get("public"):
            continue
        row = {**p, "price_id": getattr(settings, _PRICE_ID_ATTR[p["key"]], "") or None}
        # Keep the mailbox allowance visible on every tier. Dedup so a tier
        # whose CATALOG features already state it isn't doubled.
        inc = p.get("included_mailboxes")
        mbx = (f"{inc} mailbox{'es' if inc != 1 else ''} included" if inc is not None
               else "Unlimited mailboxes")
        if not any("mailbox" in f.lower() for f in row["features"]):
            row["features"] = list(row["features"]) + [mbx]
        out.append(row)
    return out


def pricing_blurb() -> str:
    """The pricing section for the concierge's injected facts — rendered from the
    SAME catalog the billing page uses, so the assistant always quotes what we
    actually charge. Public tiers only: the parked OS/Enterprise tiers must not
    be quoted or sold — anything bigger than Pro + Concierge routes to
    hello@blam8.com."""
    lines = []
    for p in CATALOG:
        if not p.get("public"):
            continue
        inc = p.get("included_mailboxes")
        mbx = f" {inc} mailbox{'es' if (inc or 0) != 1 else ''} included." if inc else ""
        lines.append(f"- {p['name']}: {p['price']} {p['per']} — {p['summary']}."
                     f"{mbx} {p['emails']}. {'; '.join(p['features'])}.")
    lines.append(f"- Extra mailbox: {EXTRA_MAILBOX['price']} {EXTRA_MAILBOX['per']} "
                 "(each mailbox beyond the plan's included allowance; each paid "
                 "extra mailbox also adds 300 emails/month to the plan's allowance).")
    lines.append(
        "- Starter (£39) covers replies in the business's voice plus real-diary "
        "booking with calendar sync. The self-filling CRM — contacts, deals, "
        "promises, follow-up sequences and customer-mood flags — unlocks on Pro "
        "(£99). The CRM quietly pre-fills from the mail flow on every account, "
        "so upgrading opens a CRM that is already populated — but on Starter it "
        "cannot be opened or used.")
    lines.append(
        "- The Concierge is the difference between Pro and Pro + Concierge "
        "(£249): the in-app assistant that knows the business's own data. Ask "
        "her anything (\"who owes me money?\", \"what's on tomorrow?\") and she "
        "acts behind a confirm card — drafts replies, moves and books diary "
        "slots, chases invoices, handles bulk jobs — with live hands-free voice, "
        "and the morning brief opens straight into her. Starter and Pro have no "
        "Concierge; they keep App help (how-to-use-the-app help from the search "
        "box). Pro + Concierge has the same allowance as Pro.")
    lines.append(
        "- Bigger operations (multiple sites or depots, franchise groups, custom "
        "volumes): not sold self-serve — the visitor should email hello@blam8.com "
        "and the team will help directly. Never quote a price for these.")
    header = ("Pricing (every account starts with a 14-day free trial of the "
              "FULL product — Pro + Concierge, every feature open, CRM and the "
              "Concierge included, on one mailbox; no card to start, no card "
              "data ever touches BLAM8, cancel any time. The trial ends at 14 "
              f"days or {settings.trial_email_cap} handled emails, whichever "
              "comes first; there is no free-forever tier). Three public tiers, "
              "one flat price per month each:")
    return header + "\n" + "\n".join(lines)
