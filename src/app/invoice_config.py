"""Invoicing module — modular invoice config (docs/modules/module-invoicing.md).

Pure, no DB: the per-tenant settings that decide WHICH sections appear on an invoice
and the VAT rate. The invoice template renders sections by these flags, so the same
shared engine produces a tailored invoice — toggle VAT off, drop the business header,
change the footer, etc. New verticals can register their own toggleable sections under
`extras` without touching the engine.

VAT defaults OFF for a tenant with no settings row: most BLAM8 tenants are not VAT
registered, and charging VAT you can't remit is worse than under-charging — a
registered business flips it on (rate pre-filled at 20%) in invoice settings.
Bank details are the no-Stripe fallback: shown on the invoice + pay page so a
customer can settle by transfer when there's no Pay Now link.
"""
import math
import re
from datetime import date

DEFAULTS = {
    "show_vat": False,
    "vat_rate": 20.0,
    "show_business_details": True,
    "business_name": "",
    "business_address": "",
    "business_vat_no": "",
    "business_contact": "",
    "show_payment_terms": True,
    # No default: terms are unset until the owner sets them (no-defaults rule,
    # 1 Oct 2026). Empty = nothing about terms prints anywhere.
    "payment_terms": "",
    "footer_note": "",
    "bank_account_name": "",
    "bank_sort_code": "",
    "bank_account_number": "",
    # Quote defaults (docs/modules/module-quotes.md) — quotes share this settings row so
    # the two documents can never drift. deposit None = no deposit by default.
    "default_quote_valid_days": 30,
    "default_deposit_percent": None,
    "extras": {},
}

# The editable fields on the settings page (drives the form + the safe-write filter).
TEXT_FIELDS = ("business_name", "business_address", "business_vat_no",
               "business_contact", "payment_terms", "footer_note",
               "bank_account_name", "bank_sort_code", "bank_account_number")
TOGGLE_FIELDS = ("show_vat", "show_business_details", "show_payment_terms")

# ── invoice layout (drag-to-reorder, 2026-07-26) ─────────────────────────────
# The invoice is five top-level blocks the tenant can rearrange; "core" (the
# invoice meta + line table + totals) moves as ONE welded unit so no drag can
# ever put totals above the lines they sum. Order lives in extras.section_order;
# anything missing/unknown normalises back to the default, so a bad value can
# never brick a tenant's invoice. Bank details render only when filled in.
SECTIONS = ("business_header", "core", "payment_terms", "bank_details",
            "footer_note")
DEFAULT_SECTION_ORDER = list(SECTIONS)
SECTION_LABELS = {
    "business_header": "Your business details",
    "core": "Invoice details, line items & totals",
    "payment_terms": "Payment terms",
    "bank_details": "Bank transfer details",
    "footer_note": "Footer note",
}


def normalize_section_order(raw) -> list:
    """A safe, complete section order from whatever was stored or posted:
    known sections in the given order first, any missing ones appended in
    default order. Accepts a list or a comma-separated string; garbage in →
    the default order out."""
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",")]
    seen = []
    for s in raw or []:
        if s in SECTIONS and s not in seen:     # whitelist + order-preserving dedupe
            seen.append(s)
    return seen + [s for s in DEFAULT_SECTION_ORDER if s not in seen]


def sample_invoice(settings: dict) -> tuple[dict, list[dict]]:
    """Dummy invoice + lines for the settings-page preview — totals recomputed
    from the CURRENT settings so the VAT line previews truthfully. Never
    persisted; INV-0000 can't collide with real refs (minting starts at 1001)."""
    from datetime import date, timedelta
    lines = [
        {"description": "Annual service (standard)", "amount": 100.0,
         "date": date.today().isoformat()},
        {"description": "Replacement part", "amount": 45.0,
         "date": date.today().isoformat()},
    ]
    subtotal, vat, total = split_vat(sum(l["amount"] for l in lines), settings)
    inv = {
        "id": "sample", "invoice_ref": "INV-0000",
        "customer_name": "Example Customer",
        "customer_email": "customer@example.com",
        "status": "sent", "issue_date": date.today().isoformat(),
        "due_date": default_due_date(settings, date.today()),
        "subtotal": subtotal, "vat": vat, "total": total,
    }
    if settings.get("prices_include_vat") is True:
        inv["prices_include_vat"] = True      # the sample reads the way a real one will
    return inv, lines


def bank_details(settings: dict) -> str | None:
    """The bank-transfer block for an invoice email / pay page, or None when the
    tenant hasn't filled their details in."""
    name = (settings.get("bank_account_name") or "").strip()
    sort = (settings.get("bank_sort_code") or "").strip()
    acct = (settings.get("bank_account_number") or "").strip()
    if not (sort and acct):
        return None
    lines = ["Pay by bank transfer:"]
    if name:
        lines.append(f"  Account name: {name}")
    lines.append(f"  Sort code: {sort}")
    lines.append(f"  Account number: {acct}")
    return "\n".join(lines)


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_BARE_DAYS = re.compile(r"^\s*(\d{1,3})\s*(?:days?)?\s*\.?\s*$", re.IGNORECASE)


def payment_terms_text(value) -> str | None:
    """The payment-terms line as a SENTENCE. The setting is free text, and a
    bare number lands in it easily ("14" — live 2026-09-03, the Stone Mason
    rig's invoice page and PDF printed a lone 14 between the total and the bank
    details). A number, with or without 'days', reads as the sentence it stands
    for; 0 is 'on receipt'; anything else is the operator's own words, untouched."""
    text = str(value or "").strip()
    if not text:
        return None
    m = _BARE_DAYS.match(text)
    if not m:
        return text
    n = int(m.group(1))
    if n == 0:
        return "Payment due on receipt."
    return f"Payment due within {n} day{'' if n == 1 else 's'} of the invoice date."


DEFAULT_DUE_DAYS = 14
_ON_RECEIPT = re.compile(r"\b(on|upon)\s+receipt\b|\bimmediately\b|\bdue\s+now\b", re.IGNORECASE)
_TERMS_DAYS = re.compile(r"(\d{1,3})\s*(?:calendar\s+|working\s+|business\s+)?days?\b", re.IGNORECASE)


def due_days(settings: dict | None) -> int:
    """How many days after issue an invoice falls due — READ FROM THE PAYMENT
    TERMS SENTENCE, so the due date printed at the top of an invoice never
    disagrees with the terms printed under its total (live 2026-09-03: terms
    could say 30 days while every invoice fell due at a hard-coded 14). 'On
    receipt' / 'immediately' → 0; the first 'N days' in the sentence → N (0–365);
    no terms, terms hidden, or nothing parseable → the 14-day default. Working
    days are counted as calendar days — the sentence is for the customer, the
    date is a plain calendar date."""
    if not settings or not settings.get("show_payment_terms"):
        return DEFAULT_DUE_DAYS
    text = str(settings.get("payment_terms") or "")
    if not text.strip():
        return DEFAULT_DUE_DAYS
    if _ON_RECEIPT.search(text):
        return 0
    m = _TERMS_DAYS.search(text)
    if not m:
        return DEFAULT_DUE_DAYS
    return max(0, min(365, int(m.group(1))))


def default_due_date(settings: dict | None, issued) -> str:
    """ISO due date for an invoice issued on `issued` (a date) under these terms."""
    from datetime import timedelta
    return (issued + timedelta(days=due_days(settings))).isoformat()


def settings_for(row: dict | None) -> dict:
    """A tenant's invoice settings = DEFAULTS overlaid with their stored row (None = a
    tenant who never configured invoicing → the defaults, i.e. today's behaviour).
    section_order is always present and always complete (normalised from extras)."""
    merged = {**DEFAULTS, **{k: v for k, v in (row or {}).items() if v is not None}}
    merged["vat_rate"] = _f(merged.get("vat_rate"), 20.0)
    merged["payment_terms"] = payment_terms_text(merged.get("payment_terms"))
    merged["extras"] = (row or {}).get("extras") or {}
    merged["section_order"] = normalize_section_order(
        merged["extras"].get("section_order"))
    # Deposit on confirmation (src/app/deposits.py): on unless switched off.
    _doc = merged["extras"].get("deposit_on_confirm")
    merged["deposit_on_confirm"] = True if _doc is None else bool(_doc)
    # Card payments (docs/plan-payments-panel.md §0.6): the invoice size from which
    # the Pay Now checkout also offers bank methods (Bacs Direct Debit / Pay by
    # Bank). None = cards and wallets only, which is the default.
    raw_min = merged["extras"].get("bank_methods_min_gbp")
    merged["bank_methods_min_gbp"] = (_f(raw_min, -1.0) if raw_min not in (None, "") else -1.0)
    if merged["bank_methods_min_gbp"] < 0:
        merged["bank_methods_min_gbp"] = None
    # Payments as a real business runs them (Kris 2026-09-24): three settings,
    # each defaulting to what every tenant had before — a type's preset turns
    # them on, never the code (feedback: settings, not business types).
    _qbp = merged["extras"].get("quote_booking_priced")
    merged["quote_booking_priced"] = True if _qbp is None else bool(_qbp)
    merged["balance_mode"] = (merged["extras"].get("balance_mode")
                              if merged["extras"].get("balance_mode") in BALANCE_MODES else "off")
    merged["peak_deposits"] = clean_peaks(merged["extras"].get("peak_deposits"))
    merged["deposit_rules"] = clean_deposit_rules(merged["extras"].get("deposit_rules"))
    # Whether the prices the owner states already include VAT: True / False /
    # None (not chosen yet - behaves as False, VAT added on top, as it always has).
    _piv = merged["extras"].get("prices_include_vat")
    merged["prices_include_vat"] = _piv if isinstance(_piv, bool) else None
    return merged


# How the rest of a booking's price is billed (src/app/jobs/raise_balances.py):
# off — by hand, as before; draft — a balance invoice waits for you the day
# before the booking ends; send — it goes to the customer that day, due on
# the last day, Pay Now link when Stripe is connected.
BALANCE_MODES = ("off", "draft", "send")


def clean_peaks(raw) -> list[dict]:
    """Peak date ranges with their own deposit: [{from, to, percent}], sorted,
    valid only — a range the owner typed backwards is turned round."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        try:
            a, b = date.fromisoformat(str(p.get("from"))[:10]), date.fromisoformat(str(p.get("to"))[:10])
            pct = float(p.get("percent"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not 0 < pct <= 100:
            continue
        a, b = min(a, b), max(a, b)
        out.append({"from": a.isoformat(), "to": b.isoformat(), "percent": pct})
    return sorted(out, key=lambda p: p["from"])[:24]


_PEAK_LINE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:to|-|–|—)\s*(\d{4}-\d{2}-\d{2})\D+?(\d{1,3}(?:\.\d+)?)\s*%?")


def parse_peaks(text: str | None) -> list[dict]:
    """One range per line: "2026-12-18 to 2027-01-03: 100%"."""
    found = []
    for line in str(text or "").splitlines():
        m = _PEAK_LINE.search(line)
        if m:
            found.append({"from": m.group(1), "to": m.group(2), "percent": m.group(3)})
    return clean_peaks(found)


def peaks_text(peaks: list[dict]) -> str:
    return "\n".join(f"{p['from']} to {p['to']}: {p['percent']:g}%" for p in peaks or [])


def peak_percent(settings: dict, first: date, last: date) -> float | None:
    """The deposit % for a booking running first..last when it touches a peak
    range — the highest if it touches several — else None."""
    hits = [p["percent"] for p in settings.get("peak_deposits") or []
            if date.fromisoformat(p["from"]) <= last and first <= date.fromisoformat(p["to"])]
    return max(hits) if hits else None


DEPOSIT_RULES_MAX = 10


def clean_deposit_rules(raw) -> list[dict]:
    """Deposit rules on a document's amount: [{over, percent}] — "when the
    amount is over X, ask for N%". Valid rows only (over > 0, 0 < percent <= 100),
    sorted by `over` ascending, at most DEPOSIT_RULES_MAX. Empty = no rules."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            over, pct = float(r.get("over")), float(r.get("percent"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (math.isfinite(over) and math.isfinite(pct)):
            continue
        if over <= 0 or not 0 < pct <= 100:
            continue
        row = {"over": over, "percent": pct}
        # basis: "total" (the whole amount) is the default and is not stored;
        # "ticked" = only the lines marked as counting towards the rule.
        if str(r.get("basis") or "").strip().lower() == "ticked":
            row["basis"] = "ticked"
        out.append(row)
    return sorted(out, key=lambda r: (r["over"], r["percent"]))[:DEPOSIT_RULES_MAX]


def _valid_percent(v) -> float | None:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if 0 < p <= 100 else None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def deposit_rule_for(settings: dict, amount, ticked_amount=None) -> dict | None:
    """The deposit rule that applies, or None. A "total" rule (the default basis)
    is tested on `amount`; a "ticked" rule on `ticked_amount` (the gross sum of
    the lines marked as counting towards the rule; None / no such lines = the
    rule cannot match). "over" is strictly greater. Of the rules that match, the
    one with the highest `over` wins (ties: the higher percentage)."""
    amt, tick = _num(amount), _num(ticked_amount)
    hits = []
    for r in clean_deposit_rules(settings.get("deposit_rules")):
        tested = tick if r.get("basis") == "ticked" else amt
        if tested is not None and tested > r["over"]:
            hits.append(r)
    if not hits:
        return None
    return max(hits, key=lambda r: (r["over"], r["percent"], r.get("basis") != "ticked"))


def value_rule_percent(settings: dict, amount, ticked_amount=None) -> float | None:
    """The percentage of the deposit rule that applies (see deposit_rule_for)."""
    r = deposit_rule_for(settings, amount, ticked_amount)
    return None if r is None else r["percent"]


def deposit_percent_for(settings: dict, amount, first: date | None = None,
                        last: date | None = None, ticked_amount=None) -> tuple[float | None, str | None]:
    """THE place a deposit percentage is decided: (percent, which) where which
    is "value" (a deposit rule on the amount), "peak" (a peak date range
    touched by first..last), "default" (the flat default) or None (no deposit).
    A value rule beats the flat default; a peak beats the flat default only
    when higher (as it always has); when a value rule and a peak both match the
    higher percentage wins (a tie goes to the value rule). It decides how much,
    never whether a deposit is asked at all."""
    value = value_rule_percent(settings, amount, ticked_amount)
    peak = peak_percent(settings, first, last) if first and last else None
    default = _valid_percent(settings.get("default_deposit_percent"))
    if value is not None and (peak is None or value >= peak):
        return value, "value"
    if peak is not None and value is not None:
        return peak, "peak"
    if peak is not None and (default is None or peak > default):
        return peak, "peak"
    if default is not None:
        return default, "default"
    return None, None


def effective_vat_rate(settings: dict) -> float:
    """The VAT rate to apply: the configured rate when VAT is shown, else 0 (VAT off)."""
    return _f(settings.get("vat_rate"), 20.0) if settings.get("show_vat") else 0.0


def split_vat(amount_sum, settings: dict | None = None, *,
              include: bool | None = None) -> tuple[float, float, float]:
    """THE place line amounts become (subtotal, vat, total), each to 2 dp.

    `amount_sum` is the sum of the lines as the owner wrote them (and the
    customer was told). The basis decides how that sum splits, never what a
    line costs:
      * VAT added on top (the default, and `include` False/None with the
        setting unset): subtotal = sum, vat = sum x rate, total = subtotal + vat;
      * prices include VAT: total = sum, vat = total x r / (1 + r),
        subtotal = total - vat - so the total always equals the lines.
    VAT off (rate 0) gives (sum, 0, sum) on either basis. `include` overrides
    the settings' basis - pass a document's own stored basis (None = use the
    settings; a document with none stored passes False)."""
    from decimal import ROUND_HALF_UP, Decimal
    total_in = round(float(amount_sum or 0), 2)
    settings = settings or {}
    pct = effective_vat_rate(settings)
    basis = settings.get("prices_include_vat") if include is None else include
    if basis is True and pct:
        # Decimal, half up: a stated price must never land a half-penny on the
        # wrong side of float error.
        d = Decimal(str(total_in))
        p = Decimal(str(pct))
        vat = float((d * p / (Decimal(100) + p)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        return round(total_in - vat, 2), vat, total_in
    vat = round(total_in * (pct / 100.0), 2)
    return total_in, vat, round(total_in + vat, 2)


def _posted_list(form, name: str) -> list:
    if hasattr(form, "getlist"):
        return list(form.getlist(name))
    v = form.get(name)
    return list(v) if isinstance(v, (list, tuple)) else ([] if v is None else [v])


def _rules_from_form(form) -> list[dict]:
    """Deposit rules posted as repeated deposit_rule_over / deposit_rule_percent
    fields (one trio per row of the settings page; basis is "total" or "ticked")."""
    from itertools import zip_longest
    overs, pcts = _posted_list(form, "deposit_rule_over"), _posted_list(form, "deposit_rule_percent")
    bases = _posted_list(form, "deposit_rule_basis")
    rows = [{"over": o, "percent": p, "basis": b} for o, p, b in zip_longest(overs, pcts, bases)
            if o is not None and p is not None]
    return clean_deposit_rules(rows)


def clean_form(form: dict) -> dict:
    """The writable settings from a posted settings form — only known fields, typed.
    Checkboxes arrive as '1' when ticked / absent when not. section_order arrives as
    a comma-separated hidden field (the drag list serialises into it) and is stored
    normalised under extras — the jsonb future-proofing column finally earning
    its keep."""
    out = {f: (str(form.get(f) or "").strip() or None) for f in TEXT_FIELDS}
    out["payment_terms"] = payment_terms_text(out.get("payment_terms"))
    for f in TOGGLE_FIELDS:
        out[f] = form.get(f) == "1"
    rate = str(form.get("vat_rate") or "").strip()
    out["vat_rate"] = _f(rate, 20.0) if rate else 20.0
    days = str(form.get("default_quote_valid_days") or "").strip()
    out["default_quote_valid_days"] = max(1, min(365, int(_f(days, 30.0)))) if days else 30
    dep = str(form.get("default_deposit_percent") or "").strip()
    dep_v = _f(dep, -1.0) if dep else -1.0
    # Blank or out-of-range = no default deposit (0 is "none", not "0% deposit").
    out["default_deposit_percent"] = dep_v if 0 < dep_v <= 100 else None
    if form.get("section_order") is not None:
        out["extras"] = {"section_order":
                         normalize_section_order(str(form.get("section_order")))}
    # The deposit-on-confirmation switch travels with a presence marker, so a
    # form that never showed it (another section's save) leaves it alone.
    if form.get("deposit_on_confirm_form") == "1":
        out.setdefault("extras", {})["deposit_on_confirm"] = form.get("deposit_on_confirm") == "1"
    # Whether stated prices include VAT: radio "1" (included) / "0" (added);
    # neither chosen = unset. Presence marker, same rule - and an unset is
    # written as null so it clears a choice made earlier.
    if form.get("prices_include_vat_form") == "1":
        v = form.get("prices_include_vat")
        out.setdefault("extras", {})["prices_include_vat"] = (
            True if v == "1" else False if v == "0" else None)
    # The payments settings (2026-09-24) — same presence-marker rule.
    if form.get("payments_form") == "1":
        ex = out.setdefault("extras", {})
        ex["quote_booking_priced"] = form.get("quote_booking_priced") == "1"
        mode = str(form.get("balance_mode") or "off")
        ex["balance_mode"] = mode if mode in BALANCE_MODES else "off"
        ex["peak_deposits"] = parse_peaks(form.get("peak_deposits"))
        ex["deposit_rules"] = _rules_from_form(form)
    return out
