"""Field Service pricing — a per-tenant rate card turns a job into a quote
(docs/modules/module-field-service.md D3). The trades sibling of src/app/pricing.py.

Pure view-model in the house style: rate card + a job (duration, optional fixed
job-type price) in, a line-item quote out, unit-tested with no DB. A trades quote is
either a FIXED-PRICE job (the job type carries a default_price) or CALL-OUT + LABOUR
(estimated hours × hourly_rate); either way it is floored at min_charge and VAT is
shown on top. The quote total is stamped on bookings.quoted_amount so the customer is
told the same number invoicing later bills.
"""
from math import ceil

# What a tenant sees before they touch the rate card (mirrors seed_fsm_defaults).
DEFAULT_FSM_RATE = {
    "name": "Standard", "callout_charge": 60.0, "hourly_rate": 55.0,
    "min_charge": 75.0, "vat_pct": 20.0, "currency": "GBP",
}

FSM_RATE_FIELDS = ("callout_charge", "hourly_rate", "min_charge", "vat_pct")


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def quote(rate_card: dict | None, duration_minutes, job_price=None) -> dict:
    """The line-item quote for one job. When `job_price` is given (the job type is
    fixed-price), the work line is that price and labour is 0; otherwise labour =
    ceil(duration / 60) hours (min 1) × hourly_rate. Call-out always applies. The net
    is floored at min_charge, then VAT is added on top."""
    rc = {**DEFAULT_FSM_RATE, **(rate_card or {})}
    callout = round(_f(rc["callout_charge"]), 2)
    if job_price is not None:
        hours = 0
        labour = 0.0
        work = round(_f(job_price), 2)          # fixed-price job (parts + set price)
    else:
        hours = max(1, ceil(_f(duration_minutes) / 60.0))
        labour = round(_f(rc["hourly_rate"]) * hours, 2)
        work = 0.0
    net_raw = round(callout + labour + work, 2)
    net = round(max(net_raw, _f(rc["min_charge"])), 2)
    vat = round(net * _f(rc["vat_pct"]) / 100.0, 2)
    total = round(net + vat, 2)
    return {
        "callout": callout, "labour": labour, "parts": work, "hours": hours,
        "net": net, "vat": vat, "vat_pct": round(_f(rc["vat_pct"]), 2),
        "min_applied": net > net_raw, "total": total,
        "currency": rc.get("currency", "GBP"),
    }
