"""Partner referral programme — attribution, the commission ledger, leads.

The 20%-lifetime referral track. A partner (accountant, MSP, agency, community
organiser) gets a code; a business is attributed to them one of three ways,
strongest signal first:

  1. ?ref=CODE link — stashed in a cookie at /login or the landing page,
     consumed at the SSO callback on the FIRST sign-in.
  2. Typed code on the onboarding interview.
  3. A pre-registered LEAD (company name + contact email) matched by signup
     email — the fallback that protects attribution when no link was used.

First attribution wins and is never rewritten (db.attach_referral only writes
while the tenant is unattributed — that is what makes "lifetime" safe to
promise). A lead that loses is MARKED disputed/ineligible, never silently
dropped. Self-referral is blocked in product: a code attaches only when the
signup email differs from the partner's own, and the lead form refuses the
partner's own address.

Money: commissions are EARNED only when BLAM8 actually collects a subscription
payment — the Stripe invoice.paid webhook writes a ledger row (scheduled →
paid at the monthly payout run). Catalog prices appear on the portal only as
per-referral "subscription value" context; the money tiles read the ledger.

Attribution window: 90 days (partner agreement). An unconverted referral shows
its expiry date; if the FIRST collected payment lands outside the window it
earns nothing. Once converted in-window, every later invoice earns for life.
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Response

from app import db, partner_settlement, plans

REF_COOKIE = "bl_ref"
_REF_COOKIE_DAYS = 30
ATTRIBUTION_WINDOW_DAYS = 90

# Payout mechanics (industry-standard net-30: FirstPromoter/Rewardful hold
# commissions "pending" through the refund window before they become due):
# the payout run happens at the END of each month and pays every scheduled
# commission earned BEFORE the 1st of that month — an effective 30–60 day
# hold that clears most refunds/chargebacks before money moves, while still
# satisfying the agreement's "within 30 days of the end of the month earned"
# (clause 4.2). Refund clawbacks (negative rows) are always payable
# immediately so they deduct from the very next statement (clause 4.7).
MIN_PAYOUT_GBP = 10.0   # clause 4.6 — smaller balances roll forward

# The headline rate (agreement clause 3.1). A partner row may override it via
# commission_rate; this is the fallback AND what the public/marketing surfaces
# quote, so it lives in one place rather than being retyped per caller.
DEFAULT_COMMISSION_RATE = 0.20

# Self-serve enrolment: the programme is open — the gate is on PAYOUTS (the
# ledger only moves collected money, self-referral is blocked, and a partner
# can be disabled), not on joining. The join form shows the full Partner
# Referral Agreement (templates/partner/_agreement_body.html) and the partner
# signs by typing their name; acceptance is stamped with this version so a
# later change to the agreement is distinguishable from what was signed.
#
# ⚠️ Bumping this does NOT re-bind anyone who already signed an earlier version —
# there is no re-acceptance flow, by design (a partner's signed terms are the
# ones they signed). v2.3 added the settlement clauses 4.9–4.12; it was safe to
# bump on 2026-07-30 because the partners table was still empty, so nobody was
# left holding older terms. If that is ever untrue again, existing partners keep
# their version and need explicit re-acceptance before the new clauses apply.
TERMS_VERSION = "2.3"

# "Become a partner" intent: set before SSO so a brand-new person joining as a
# partner never has a business tenant created or sees the onboarding wizard —
# the callback routes them straight to /partner to enrol.
PARTNER_INTENT_COOKIE = "bl_pjoin"


def set_partner_intent_cookie(response: Response) -> None:
    response.set_cookie(PARTNER_INTENT_COOKIE, "1", max_age=3600, httponly=True,
                        secure=True, samesite="lax", path="/")


def clear_partner_intent_cookie(response: Response) -> None:
    response.delete_cookie(PARTNER_INTENT_COOKIE, path="/")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_code(code: str | None) -> str:
    return (code or "").strip().upper()


def set_ref_cookie(response: Response, code: str) -> None:
    """Stash a ?ref= code for the signup round-trip (SSO leaves the site and
    comes back at /auth/callback, so a cookie is the only thing that survives)."""
    response.set_cookie(REF_COOKIE, normalize_code(code)[:32],
                        max_age=60 * 60 * 24 * _REF_COOKIE_DAYS,
                        httponly=True, secure=True, samesite="lax", path="/")


def clear_ref_cookie(response: Response) -> None:
    response.delete_cookie(REF_COOKIE, path="/")


def generate_code(name: str) -> str:
    """A memorable, typeable code from the partner's name: the first word's
    letters (≤8) + 2 digits, e.g. "Sterling Accounts" → STERLING41. Retries with
    fresh digits until unique (collisions are near-impossible at this scale)."""
    stem = "".join(c for c in (name or "").upper() if c.isalnum())[:8] or "PARTNER"
    for _ in range(20):
        code = f"{stem}{secrets.randbelow(90) + 10}"
        if not db.get_partner_by_code(code):
            return code
    return f"{stem}{secrets.token_hex(2).upper()}"


def enroll_self_serve(email: str, name: str, signed_name: str) -> dict | None:
    """Electronic signing of the Partner Referral Agreement from /partner: mint
    a code and create the partner against the signed-in email, recording the
    typed-name signature + acceptance time + agreement version. Idempotent —
    an email that's already a partner returns the existing row (so a double
    submit or a race can't error or double-mint). None on invalid input."""
    addr = (email or "").strip().lower()
    signature = (signed_name or "").strip()
    # Business/trading name is optional at registration — a partner referring
    # personally just signs with their name, which then also mints their code.
    display = (name or "").strip() or signature
    if not addr or "@" not in addr or not signature:
        return None
    existing = db.get_partner_by_email(addr)
    if existing:
        return existing
    try:
        return db.create_partner(display, addr, generate_code(display),
                                 source="self_serve",
                                 agreed_at=_now().isoformat(),
                                 terms_version=TERMS_VERSION,
                                 signed_name=signature)
    except Exception:
        # unique-violation race: someone (this user, twice) got there first
        return db.get_partner_by_email(addr)


# ── attribution ──────────────────────────────────────────────────────────────
def attach_for_tenant(tenant_id: str, code: str, signer_email: str | None = None) -> str:
    """Validate a typed/cookied code and attribute the tenant. Returns a status
    the caller can surface: 'attached' | 'invalid' (unknown/disabled code) |
    'self' (the partner's own signup — blocked in product, not just the
    agreement) | 'taken' (already attributed — first code won) | 'none' (no
    code). Never raises on bad input — attribution must never break a signup."""
    ref_code = normalize_code(code)
    if not ref_code:
        return "none"
    partner = db.get_partner_by_code(ref_code)
    if not partner or partner.get("status") != "active":
        return "invalid"
    if signer_email and (signer_email or "").strip().lower() == partner.get("email"):
        return "self"
    if db.attach_referral(tenant_id, partner["id"], ref_code, _now().isoformat()):
        _settle_leads_after_attribution(tenant_id, signer_email, partner["id"])
        return "attached"
    return "taken"


def _settle_leads_after_attribution(tenant_id: str, signup_email: str | None,
                                    winning_partner_id: str) -> None:
    """After an attribution lands, resolve any leads registered against the
    signup email: the winner's lead converts; a rival's lead is marked DISPUTED
    (visible, reviewable) — never silently dropped."""
    if not signup_email:
        return
    try:
        for lead in db.list_leads_by_email(signup_email):
            if lead["partner_id"] == winning_partner_id:
                db.update_partner_lead(lead["id"], {
                    "status": "converted", "tenant_id": tenant_id,
                    "converted_at": _now().isoformat()})
            else:
                db.update_partner_lead(lead["id"], {
                    "status": "disputed", "reason": "other_partner",
                    "tenant_id": tenant_id})
    except Exception:
        pass


def reconcile_leads_for_signup(tenant_id: str, signup_email: str | None) -> None:
    """The lead fallback at first sign-in, AFTER any code attribution ran: if
    the tenant is still unattributed and a live (unexpired) lead matches the
    signup email, the EARLIEST lead's partner wins. Self-leads never attach."""
    if not signup_email:
        return
    leads = db.list_leads_by_email(signup_email)
    if not leads:
        return
    referral = db.get_tenant_referral(tenant_id) or {}
    if referral.get("referred_by_partner_id"):
        # A code already won — mark rival leads disputed / winner converted.
        _settle_leads_after_attribution(tenant_id, signup_email,
                                        referral["referred_by_partner_id"])
        return
    now = _now()
    for lead in leads:   # oldest first — the earliest introduction wins
        expires = lead.get("expires_at")
        if expires and datetime.fromisoformat(expires) < now:
            continue
        partner = db.get_partner(lead["partner_id"])
        if not partner or partner.get("status") != "active":
            continue
        if (signup_email or "").strip().lower() == partner.get("email"):
            continue   # self-referral via a lead — blocked
        if db.attach_referral(tenant_id, partner["id"], partner["ref_code"],
                              now.isoformat()):
            _settle_leads_after_attribution(tenant_id, signup_email, partner["id"])
        return


def mark_leads_already_customer(signup_email: str | None) -> None:
    """An EXISTING account signed in with an email some partner registered as a
    lead — the business already exists, so the lead is ineligible. Marked, not
    deleted: the partner sees exactly why it didn't convert."""
    if not signup_email:
        return
    for lead in db.list_leads_by_email(signup_email):
        db.update_partner_lead(lead["id"],
                               {"status": "ineligible", "reason": "already_customer"})


def register_lead(partner: dict, company_name: str, contact_email: str) -> str:
    """A manual introduction from the /partner form. Returns 'ok' | 'invalid'
    (bad fields) | 'self' (the partner's own email) | 'duplicate' (they already
    registered this email and it's still live) | 'exists' (that email already
    has a BLAM8 account — recorded as an INELIGIBLE lead, so the answer is
    visible on the portal rather than a silent refusal)."""
    company = (company_name or "").strip()
    email = (contact_email or "").strip().lower()
    if not company or "@" not in email or "." not in email.split("@")[-1]:
        return "invalid"
    if email == partner.get("email"):
        return "self"
    for lead in db.list_partner_leads(partner["id"]):
        if lead["contact_email"] == email and lead["status"] == "registered":
            return "duplicate"
    expires = (_now() + timedelta(days=ATTRIBUTION_WINDOW_DAYS)).isoformat()
    # Eligibility check at registration time: an email that already has an
    # account can never be a referral (a referral is a NEW customer). Checked
    # against auth.users via a service-role-only RPC; on any failure this
    # returns False and the lazy check at that email's next sign-in still
    # catches it (mark_leads_already_customer).
    if db.auth_user_exists(email):
        db.create_partner_lead(partner["id"], company, email, expires,
                               status="ineligible", reason="already_customer")
        return "exists"
    db.create_partner_lead(partner["id"], company, email, expires)
    return "ok"


# ── the commission ledger (earned = collected) ───────────────────────────────
def record_commission_for_invoice(tenant_id: str, invoice: dict) -> bool:
    """Stripe invoice.paid → an EARNED ledger row for the referring partner.
    Rules, in order:
      * unreferred tenant / unknown partner → nothing;
      * £0 invoices (trial starts) never earn;
      * the 90-day window: if this referral has never earned before and the
        window has passed, it never converts — no row, ever;
      * idempotent per invoice (unique invoice_id — webhook retries are safe).
    Commission = the partner's CURRENT rate × the amount actually collected,
    so Stripe promos (FOUNDING50) and add-ons are exact by construction."""
    referral = db.get_tenant_referral(tenant_id) or {}
    partner_id = referral.get("referred_by_partner_id")
    invoice_id = invoice.get("id")
    if not partner_id or not invoice_id:
        return False
    amount_paid = invoice.get("amount_paid") or 0            # pence, tax-inclusive
    if amount_paid <= 0:
        return False
    # Industry-standard commissionable base (clause 3.1): the amount collected
    # EXCLUDING VAT/sales tax, net of discounts (amount_paid is already
    # post-discount). Payment-processor fees are NOT deducted — no major
    # affiliate platform deducts them. Today no tax is charged so base ==
    # collected; once Stripe Tax turns on, the invoice's tax portion drops out
    # automatically here.
    base = max(0, amount_paid - (invoice.get("tax") or 0)) / 100.0
    if base <= 0:
        return False
    partner = db.get_partner(partner_id)
    if not partner:
        return False
    referred_at = referral.get("referred_at")
    if referred_at and not db.tenant_has_commission(tenant_id):
        window_end = (datetime.fromisoformat(referred_at)
                      + timedelta(days=ATTRIBUTION_WINDOW_DAYS))
        if _now() > window_end:
            return False   # first payment landed outside the attribution window
    rate = float(partner.get("commission_rate") or DEFAULT_COMMISSION_RATE)
    return db.create_partner_commission(
        partner_id, tenant_id, invoice_id,
        round(amount_paid / 100.0, 2), round(base * rate, 2), _now().isoformat())


def record_refund_for_charge(charge: dict) -> bool:
    """Stripe charge.refunded → automatic clawback (clause 4.7; the same
    negative-commission behaviour FirstPromoter/Rewardful apply the moment a
    refund lands). One companion row per invoice, keyed '<invoice>:clawback',
    holds the CUMULATIVE negative amount — proportional for partial refunds,
    the full commission when fully refunded — and is upserted so repeated
    refund events stay consistent. The earned row is never mutated (audit
    trail). Clawback rows are 'scheduled', so:
      * earned not yet paid → the pair nets out in the same payout sweep;
      * earned already paid → the negative deducts from the next statement,
        exactly as clause 4.7 promises."""
    invoice_id = charge.get("invoice")
    if not invoice_id:
        return False
    earned = db.get_commission_by_invoice(invoice_id)
    if not earned or float(earned.get("amount") or 0) <= 0:
        return False
    amount = charge.get("amount") or 0
    refunded = charge.get("amount_refunded") or 0
    if amount <= 0 or refunded <= 0:
        return False
    fraction = min(1.0, refunded / amount)
    clawback = -round(float(earned["amount"]) * fraction, 2)
    if clawback == 0:
        return False
    key = f"{invoice_id}:clawback"
    existing = db.get_commission_by_invoice(key)
    if existing:
        if existing.get("status") == "paid":
            return False   # already settled on a statement — beyond auto-adjust
        db.update_partner_commission(existing["id"], {"amount": clawback})
        return True
    return db.create_partner_commission(
        earned["partner_id"], earned.get("tenant_id"), key,
        -round(refunded / 100.0, 2), clawback, _now().isoformat())


def payable_commissions(commissions: list[dict]) -> list[dict]:
    """The subset of scheduled commissions the month-end payout run may pay:
    positive earnings CLEAR once the month they were earned in has ended
    (the net-30 hold); negative clawback rows are always payable so they
    deduct from the very next statement."""
    month_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for c in commissions:
        if c.get("status") != "scheduled":
            continue
        if float(c.get("amount") or 0) < 0:
            out.append(c)
        elif c.get("earned_at") and datetime.fromisoformat(c["earned_at"]) < month_start:
            out.append(c)
    return out


def next_payout_date() -> str:
    """The next payout run: the last day of the current month (paying
    everything earned before this month began — the net-30 hold)."""
    now = _now()
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    first_of_next = datetime(year, month, 1, tzinfo=timezone.utc)
    return (first_of_next - timedelta(days=1)).date().isoformat()


# ── the portal view-model ────────────────────────────────────────────────────
def plan_monthly_gbp(tenant: dict) -> float | None:
    """The tenant's list-price monthly subscription value — per-referral CONTEXT
    on the portal (the money tiles read the ledger, not this). Enterprise/MSA
    tenants use the operator-set contract; None = can't estimate."""
    contract = tenant.get("contract_monthly_gbp")
    if contract:
        return float(contract)
    plan = tenant.get("plan") or ""
    if plan == "email":
        return 39.0
    if plan == "starter":
        return float(plans.CORE_PRICE_GBP)
    if plan == "professional":
        return float(plans.CORE_PRICE_GBP + plans.os_module_price_gbp(tenant.get("industry")))
    if plan == "scale":     # retired tier — legacy subscriptions keep working
        return 799.0
    return None


def plan_label(tenant: dict) -> str:
    plan = tenant.get("plan") or ""
    for p in plans.CATALOG:
        if p["key"] == plan:
            # "Core + your OS" reads oddly in a table; name the tenant's module.
            if plan == "professional":
                return f"Core + {plans.industry_module_meta(tenant.get('industry'))['label']}"
            return p["name"]
    return {"scale": "Scale (legacy)", "pilot": "Pilot"}.get(plan, plan or "—")


def _status_ladder(tenant: dict) -> tuple[str, bool]:
    """(label, is_paying) on the agreed ladder:
    Registered → Trial → Payment pending → Active → Cancelled."""
    status = tenant.get("subscription_status") or "none"
    if tenant.get("contract_monthly_gbp"):
        return "Active", True             # invoice-billed (MSA/Enterprise)
    if status == "active":
        return "Active", True
    if status == "trialing":
        return "Trial", False
    if status in ("past_due", "incomplete"):
        return "Payment pending", False
    if status == "canceled":
        return "Cancelled", False
    return "Registered", False


def _window_state(tenant: dict, converted: bool) -> tuple[str | None, bool]:
    """(expiry date iso, is_expired) for an unconverted referral — the 90-day
    attribution window made visible. Converted referrals have no expiry."""
    if converted:
        return None, False
    referred_at = tenant.get("referred_at") or tenant.get("created_at")
    if not referred_at:
        return None, False
    expiry = datetime.fromisoformat(referred_at) + timedelta(days=ATTRIBUTION_WINDOW_DAYS)
    return expiry.date().isoformat(), _now() > expiry


def _lead_view(lead: dict) -> dict:
    status = lead["status"]
    expired = False
    if status == "registered":
        expires = lead.get("expires_at")
        expired = bool(expires and datetime.fromisoformat(expires) < _now())
    label = {"registered": "Awaiting signup", "converted": "Converted",
             "disputed": "Disputed", "ineligible": "Ineligible"}.get(status, status)
    if expired:
        label = "Expired"
    reason = {"other_partner": "another partner's referral was used at signup",
              "already_customer": "already a BLAM8 customer before this introduction"
              }.get(lead.get("reason") or "")
    return {"company": lead["company_name"], "email": lead["contact_email"],
            "registered": (lead.get("created_at") or "")[:10],
            "expires": (lead.get("expires_at") or "")[:10],
            "status": label, "reason": reason}


def referral_summary(partner: dict) -> dict:
    """Everything /partner renders. Referral rows show name/date/status/plan
    ONLY (never inbox activity, contacts or customer data). Money tiles come
    from the ledger: earned = collected by BLAM8; scheduled = earned, awaiting
    the next payout; lifetime = every earned pound so far."""
    rate = float(partner.get("commission_rate") or DEFAULT_COMMISSION_RATE)
    commissions = db.list_partner_commissions(partner["id"])
    earned_tenants = {c["tenant_id"] for c in commissions}

    rows, active = [], 0
    for t in db.list_partner_referrals(partner["id"]):
        converted = t["id"] in earned_tenants or bool(t.get("contract_monthly_gbp"))
        label, paying = _status_ladder(t)
        expiry, expired = _window_state(t, converted or paying)
        if expired and label == "Registered":
            label = "Expired"
        monthly = plan_monthly_gbp(t)
        if paying:
            active += 1
        rows.append({
            "name": t.get("name") or "—",
            "joined": (t.get("referred_at") or t.get("created_at") or "")[:10],
            "status": label, "paying": paying, "expired": expired,
            "window_expires": expiry,
            "plan": plan_label(t) if (paying or t.get("subscription_status")) else "—",
            "monthly_gbp": monthly if paying else None,
            "commission_gbp": round(monthly * rate, 2) if (paying and monthly) else None,
        })

    now = _now()
    this_month = sum(float(c["amount"]) for c in commissions
                     if c.get("earned_at")
                     and datetime.fromisoformat(c["earned_at"]).year == now.year
                     and datetime.fromisoformat(c["earned_at"]).month == now.month
                     and c["status"] != "void")
    scheduled = sum(float(c["amount"]) for c in commissions if c["status"] == "scheduled")
    payable = sum(float(c["amount"]) for c in payable_commissions(commissions))
    lifetime = sum(float(c["amount"]) for c in commissions if c["status"] != "void")

    payouts = [{"period": f"{(p.get('period_start') or '')[:10]} → {(p.get('period_end') or '')[:10]}"
                if p.get("period_start") else "—",
                "paid": (p.get("paid_at") or "")[:10],
                "total": float(p.get("total") or 0),
                "reference": p.get("reference") or "—"}
               for p in db.list_partner_payouts(partner["id"])]

    # Settlement panel: what comes off their own bill this month and what's left to
    # withdraw. Run-rate is the monthly commission from PAYING referrals — the buffer
    # is sized against it, since ongoing commission is what collateralises a clawback.
    run_rate = round(sum(r["commission_gbp"] or 0.0 for r in rows), 2)
    subscription = 0.0
    if partner.get("credit_tenant_id"):
        # Best-effort: a deleted or unreadable tenant must not break the portal, it
        # just means there is no bill to credit and the panel shows cash behaviour.
        try:
            subscription = plan_monthly_gbp(
                db.get_tenant(partner["credit_tenant_id"]) or {}) or 0.0
        except Exception:
            subscription = 0.0
    settlement = partner_settlement.view(
        cleared_gbp=round(payable, 2), subscription_gbp=subscription,
        monthly_commission_gbp=run_rate,
        mode=partner.get("settlement_mode"))

    return {"rows": rows, "total": len(rows), "active": active,
            "rate_pct": round(rate * 100),
            "this_month": round(this_month, 2), "scheduled": round(scheduled, 2),
            "payable": round(payable, 2), "min_payout": MIN_PAYOUT_GBP,
            "lifetime": round(lifetime, 2), "next_payout": next_payout_date(),
            "leads": [_lead_view(l) for l in db.list_partner_leads(partner["id"])],
            "payouts": payouts,
            "settlement": settlement, "run_rate": run_rate,
            "withdrawals": db.list_partner_withdrawals(partner["id"]),
            "open_withdrawal": db.open_partner_withdrawal(partner["id"])}


def share_text(partner: dict, share_url: str) -> str:
    """The approved one-paragraph share blurb — grounded in shipped capability
    (nothing the product can't demo live; never claims candidate ranking or
    guarantees). Shown copyable next to the code."""
    return (
        "I partner with BLAM8 — an AI assistant that answers your business email in "
        "your own voice, gathers every booking's details, and books work straight into "
        "your live diary. Draft-first (a human approves every send until you switch to "
        "auto), your data stored in the UK, 14-day free trial with no card. "
        f"Sign up with my link: {share_url} — or enter code {partner['ref_code']} "
        "when it asks how you heard about them."
    )
