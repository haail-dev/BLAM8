"""Card payments — the owner's read of their connected Stripe account.

docs/plan-payments-panel.md. The reference product named there shows its users a payments dashboard
inside the app; this is ours, built from the API rather than Stripe's embedded
components, so it needs no JavaScript, no CSP change and no Stripe login popup. The
tenant's account is a Standard account connected by OAuth (src/app/payments.py), and every
read here goes through the platform key with `stripe_account=acct`, which is exactly
what that connection permits: balance, payouts, charges with their fee breakdown, and
refunds. BLAM8 takes no application fee, so the "BLAM8's cut" line is always £0.00.

Shape: pure helpers (testable without Stripe) + `load()`, which runs the four reads
concurrently, caches the result per account for a minute, and degrades to "Stripe
didn't answer" per section rather than failing the page. Money is in pence on the
wire and pounds here; every figure is rounded to 2 dp on the way in.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import stripe

from app.config import settings

log = logging.getLogger("blautom8.payments_panel")

_LONDON = ZoneInfo("Europe/London")
CACHE_TTL_S = 60.0           # a page reload inside a minute is a cache hit
METHODS_TTL_S = 300.0        # the account's enabled payment methods change rarely
RECENT_LIMIT = 25            # payments listed on the panel
WINDOW_DAYS = 30             # the "last 30 days" summary (and its charge fetch)
WINDOW_LIMIT = 100           # one page of charges is enough for a small business

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# How the customer paid, as the panel names it. Wallets arrive as card payments with
# a wallet type underneath; they're surfaced because "Apple Pay" answers the owner's
# question better than "card".
METHOD_LABELS = {
    "card": "Card", "apple_pay": "Apple Pay", "google_pay": "Google Pay", "link": "Link",
    "bacs_debit": "Bacs Direct Debit", "pay_by_bank": "Pay by Bank",
    "sepa_debit": "SEPA Direct Debit", "revolut_pay": "Revolut Pay", "klarna": "Klarna",
    "paypal": "PayPal", "customer_balance": "Bank transfer",
}
# Payment methods that settle later than the click — the invoice sits "in progress"
# between payment_intent.processing and payment_intent.succeeded.
ASYNC_METHODS = {"bacs_debit", "pay_by_bank", "sepa_debit", "customer_balance"}

# Account capabilities worth naming in Invoice settings (capability → label). Anything
# else Stripe switches on shows up in Checkout anyway; this is the owner's short list.
CAPABILITY_LABELS = [
    ("card_payments", "Cards, Apple Pay & Google Pay"),
    ("bacs_debit_payments", "Bacs Direct Debit"),
    ("pay_by_bank_payments", "Pay by Bank"),
    ("link_payments", "Link"),
    ("klarna_payments", "Klarna"),
    ("revolut_pay_payments", "Revolut Pay"),
]
STRIPE_PAYMENT_METHODS_SETTINGS = "https://dashboard.stripe.com/settings/payment_methods"


# ── pure helpers ─────────────────────────────────────────────────────────────
def _gbp(pence) -> float:
    try:
        return round(int(pence or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _dt(ts) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    except (TypeError, ValueError, OSError):
        return None


def method_of(charge: dict) -> str:
    """The way the customer paid, as a key. A wallet beats 'card' when Stripe says so."""
    pmd = charge.get("payment_method_details") or {}
    kind = pmd.get("type") or "card"
    if kind == "card":
        wallet = ((pmd.get("card") or {}).get("wallet") or {}).get("type")
        if wallet in ("apple_pay", "google_pay", "link"):
            return wallet
    return kind


def method_label(key: str | None) -> str:
    key = key or "card"
    return METHOD_LABELS.get(key, key.replace("_", " ").title())


def status_of(charge: dict) -> str:
    """One word the owner understands, in the order that matters: a disputed payment
    is disputed whatever else is true of it."""
    if charge.get("disputed"):
        return "disputed"
    if charge.get("refunded"):
        return "refunded"
    if int(charge.get("amount_refunded") or 0) > 0:
        return "partly refunded"
    st = charge.get("status")
    if st == "succeeded":
        return "paid"
    if st == "pending":
        return "in progress"
    return st or "unknown"


def shape_charge(ch: dict) -> dict:
    """One payment row. The fee comes from the expanded balance transaction; when
    Stripe hasn't settled one yet (a bank payment in progress) it is None, and the
    template shows a dash rather than £0.00."""
    bt = ch.get("balance_transaction")
    fee = _gbp(bt.get("fee")) if isinstance(bt, dict) else None
    meta = ch.get("metadata") or {}
    bd = ch.get("billing_details") or {}
    amount = _gbp(ch.get("amount"))
    refunded = _gbp(ch.get("amount_refunded"))
    pi = ch.get("payment_intent")
    if isinstance(pi, dict):
        pi = pi.get("id")
    kind = meta.get("kind")
    refs = meta.get("invoice_refs") or meta.get("invoice_ref") or ""
    method = method_of(ch)
    return {
        "id": ch.get("id"), "payment_intent": pi,
        "created": _dt(ch.get("created")),
        "customer": bd.get("name") or bd.get("email") or ch.get("receipt_email") or "",
        "email": bd.get("email") or ch.get("receipt_email") or "",
        "description": ch.get("description") or "",
        "kind": kind,
        "invoice_id": meta.get("invoice_id") if kind == "invoice" else None,
        "invoice_ids": [i for i in (meta.get("invoice_ids") or "").split(",") if i]
        if kind == "invoice_batch" else [],
        "invoice_refs": refs,
        "amount": amount, "fee": fee,
        "net": round(amount - fee, 2) if fee is not None else None,
        "refunded": refunded,
        "method": method, "method_label": method_label(method),
        "status": status_of(ch),
        "refundable": (ch.get("status") == "succeeded" and not ch.get("disputed")
                       and refunded < amount),
        "stripe_url": f"https://dashboard.stripe.com/payments/{pi or ch.get('id')}",
    }


def shape_payout(p: dict) -> dict:
    dest = p.get("destination")
    bank = None
    if isinstance(dest, dict):
        bank = dest.get("last4")
    return {
        "id": p.get("id"), "amount": _gbp(p.get("amount")), "status": p.get("status"),
        "created": _dt(p.get("created")), "arrival": _dt(p.get("arrival_date")),
        "bank": bank, "automatic": bool(p.get("automatic", True)),
        "stripe_url": f"https://dashboard.stripe.com/payouts/{p.get('id')}",
    }


def balance_shape(bal: dict | None, currency: str = "gbp") -> dict:
    """Available now / on its way, in the account's currency (GBP for every BLAM8
    tenant; other currencies are summed under 'other' so nothing hides)."""
    out = {"available": 0.0, "pending": 0.0, "other": 0.0}
    for bucket in ("available", "pending"):
        for row in (bal or {}).get(bucket) or []:
            if (row.get("currency") or "").lower() == currency:
                out[bucket] += _gbp(row.get("amount"))
            else:
                out["other"] += _gbp(row.get("amount"))
    return {k: round(v, 2) for k, v in out.items()}


def next_payout(payouts: list[dict]) -> dict | None:
    """The soonest payout not yet landed: pending or in transit, earliest arrival."""
    coming = [p for p in payouts if p.get("status") in ("pending", "in_transit")]
    if not coming:
        return None
    return sorted(coming, key=lambda p: p.get("arrival") or datetime.max.replace(
        tzinfo=timezone.utc))[0]


def day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start of today, start of yesterday) in London, as aware datetimes. 'Today'
    on the panel is the owner's day, not UTC's."""
    now = (now or datetime.now(timezone.utc)).astimezone(_LONDON)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today, today - timedelta(days=1)


def bucket_today(charges: list[dict], now: datetime | None = None) -> dict:
    """Gross taken today vs yesterday (succeeded payments only, by London day)."""
    today, yesterday = day_bounds(now)
    out = {"today": 0.0, "today_count": 0, "yesterday": 0.0, "yesterday_count": 0}
    for c in charges:
        if c.get("status") not in ("paid", "partly refunded", "refunded", "disputed"):
            continue
        when = c.get("created")
        if not when:
            continue
        if when >= today:
            out["today"] += c["amount"]
            out["today_count"] += 1
        elif when >= yesterday:
            out["yesterday"] += c["amount"]
            out["yesterday_count"] += 1
    out["today"] = round(out["today"], 2)
    out["yesterday"] = round(out["yesterday"], 2)
    return out


def summarize(charges: list[dict]) -> dict:
    """The window's totals: collected, Stripe's fees, net, refunded, count. Fees sum
    only the rows that have one; the count of fee-less rows is reported so the
    template can say 'fees on N payments not settled yet' instead of understating."""
    out = {"collected": 0.0, "fees": 0.0, "net": 0.0, "refunded": 0.0,
           "count": 0, "unfeed": 0, "blam8_cut": 0.0}
    for c in charges:
        if c.get("status") not in ("paid", "partly refunded", "refunded", "disputed"):
            continue
        out["count"] += 1
        out["collected"] += c["amount"]
        out["refunded"] += c["refunded"]
        if c["fee"] is None:
            out["unfeed"] += 1
        else:
            out["fees"] += c["fee"]
    out["net"] = out["collected"] - out["fees"] - out["refunded"]
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


def checkout_payment_method_types(min_gbp, amount_gbp: float) -> list[str] | None:
    """What the Pay Now checkout may offer (plan §0.6). None = let Stripe offer every
    method the connected account has switched on (bank methods included); ['card']
    = cards and their wallets only. Bank methods are cheaper on a big invoice and
    slow on a small one, so the owner names the invoice size from which they appear;
    blank means never."""
    try:
        threshold = float(min_gbp) if min_gbp not in (None, "") else None
    except (TypeError, ValueError):
        threshold = None
    if threshold is not None and threshold >= 0 and float(amount_gbp) >= threshold:
        return None
    return ["card"]


# ── Stripe reads ─────────────────────────────────────────────────────────────
def _read(acct: str, what: str):
    """One Stripe call by name. Raises; load() catches per call."""
    stripe.api_key = settings.stripe_secret_key
    if what == "balance":
        return stripe.Balance.retrieve(stripe_account=acct)
    if what == "payouts":
        return stripe.Payout.list(limit=10, expand=["data.destination"],
                                  stripe_account=acct)
    if what == "recent":
        return stripe.Charge.list(limit=RECENT_LIMIT, expand=["data.balance_transaction"],
                                  stripe_account=acct)
    if what == "window":
        since = int((datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp())
        return stripe.Charge.list(limit=WINDOW_LIMIT, created={"gte": since},
                                  expand=["data.balance_transaction"], stripe_account=acct)
    raise ValueError(what)


def _rows(listing) -> list[dict]:
    data = getattr(listing, "data", None)
    if data is None and isinstance(listing, dict):
        data = listing.get("data")
    return [dict(r) if not isinstance(r, dict) else r for r in (data or [])]


def load(acct: str, *, force: bool = False, now: datetime | None = None) -> dict:
    """Everything the panel shows, for one connected account. Four reads run
    concurrently (Stripe answers each in a few hundred ms; sequentially the page
    would feel slow), the result is cached for CACHE_TTL_S, and a failed read leaves
    its name in `errors` rather than failing the page."""
    key = f"{acct}:panel"
    if not force:
        with _lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]

    results: dict[str, object] = {}
    errors: list[str] = []

    def run(what):
        try:
            results[what] = _read(acct, what)
        except Exception:
            log.warning("stripe read %s failed for %s", what, acct, exc_info=True)
            errors.append(what)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, ("balance", "payouts", "recent", "window")))

    bal = results.get("balance")
    payouts = [shape_payout(p) for p in _rows(results.get("payouts"))]
    recent = [shape_charge(c) for c in _rows(results.get("recent"))]
    window = [shape_charge(c) for c in _rows(results.get("window"))]
    data = {
        "balance": balance_shape(dict(bal) if bal is not None else None),
        "payouts": payouts,
        "next_payout": next_payout(payouts),
        "charges": recent,
        "today": bucket_today(window, now),
        "window": summarize(window),
        "window_days": WINDOW_DAYS,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc),
    }
    with _lock:
        _cache[key] = (time.monotonic(), data)
    return data


def invalidate(acct: str) -> None:
    """After a refund: the next page load must show it."""
    with _lock:
        for k in [k for k in _cache if k.startswith(f"{acct}:")]:
            _cache.pop(k, None)


def account_methods(acct: str) -> list[dict] | None:
    """Which payment methods the connected account can take, for the settings fold.
    None when Stripe didn't answer (the fold then just links to Stripe)."""
    key = f"{acct}:methods"
    with _lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < METHODS_TTL_S:
        return hit[1]["methods"]
    try:
        stripe.api_key = settings.stripe_secret_key
        account = stripe.Account.retrieve(acct)
        caps = dict(getattr(account, "capabilities", None) or {})
    except Exception:
        log.warning("stripe account read failed for %s", acct, exc_info=True)
        return None
    methods = [{"key": cap, "label": label, "active": caps.get(cap) == "active",
                "state": caps.get(cap) or "off"}
               for cap, label in CAPABILITY_LABELS]
    with _lock:
        _cache[key] = (time.monotonic(), {"methods": methods})
    return methods


def refund(acct: str, payment_intent: str, *, amount_gbp: float | None, reason: str,
           metadata: dict) -> tuple[dict, dict]:
    """Refund a payment on the connected account, full (amount None) or partial, and
    return (refund, charge-after) — the charge re-read so the caller has Stripe's
    absolute amount_refunded, which is what record_invoice_refund keys on. The free-
    text reason travels in metadata (Stripe's own `reason` enum is for fraud/duplicate
    flags, not for "the part was damaged")."""
    stripe.api_key = settings.stripe_secret_key
    params: dict = {"payment_intent": payment_intent,
                    "metadata": {**metadata, "note": (reason or "")[:400]}}
    if amount_gbp is not None:
        params["amount"] = int(round(float(amount_gbp) * 100))
    rf = stripe.Refund.create(stripe_account=acct, **params)
    charge = stripe.Charge.retrieve(rf["charge"], stripe_account=acct)
    return dict(rf), dict(charge)


def refund_key(charge_id: str, amount_refunded_pence: int) -> str:
    """The ledger identity of 'this charge has now refunded this much' — the same
    string whether the refund came from our route or from Stripe's dashboard, so
    the two doors dedupe on it (plan §0.4)."""
    return f"[{charge_id}@{int(amount_refunded_pence or 0)}]"
