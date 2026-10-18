"""Pricing — a per-tenant rate card turns a booking into a quote (TMS baseline).

Pure view-model in the house style (à la src/app/tms.py): rate card + booking rows
in, render-ready quotes out, unit-tested with no DB. Honest about its inputs — a
job with no postcodes can't be measured for miles, so the row says so and prices
the distance leg at zero rather than guessing. Miles are the collection→delivery
great-circle distance over the cached postcode geometry (reused from the planner).
"""
from datetime import datetime, timedelta

from app import db
from app.scheduling import DIARY_TZ, fmt_spaces, haversine_miles

# What a tenant sees before they touch the rate card (mirrors seed_tms_defaults).
DEFAULT_RATE = {
    "name": "Standard", "base_charge": 25.0, "per_mile": 1.20, "per_pallet": 8.0,
    "per_drop": 5.0, "min_charge": 45.0, "fuel_pct": 6.0, "currency": "GBP",
}

RATE_FIELDS = ("base_charge", "per_mile", "per_pallet", "per_drop", "min_charge", "fuel_pct")

LOOKBACK_DAYS = 14   # recent jobs shown alongside upcoming ones on the pricing list


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def quote(rate_card: dict | None, spaces, miles: float | None, drops: int = 1) -> dict:
    """The line-item breakdown for one job. `miles` None = no postcodes to measure
    (the mile leg is then zero and the caller flags the row). fuel% uplifts the
    pre-fuel subtotal; min_charge floors the total."""
    rc = {**DEFAULT_RATE, **(rate_card or {})}
    m = _f(miles)
    base = round(_f(rc["base_charge"]), 2)
    mile_charge = round(_f(rc["per_mile"]) * m, 2)
    pallet_charge = round(_f(rc["per_pallet"]) * _f(spaces), 2)
    drop_charge = round(_f(rc["per_drop"]) * max(1, int(drops or 1)), 2)
    subtotal = round(base + mile_charge + pallet_charge + drop_charge, 2)
    fuel = round(subtotal * _f(rc["fuel_pct"]) / 100.0, 2)
    gross = round(subtotal + fuel, 2)
    total = round(max(gross, _f(rc["min_charge"])), 2)
    return {
        "miles": round(m, 1) if miles is not None else None,
        "base": base, "mile_charge": mile_charge, "pallet_charge": pallet_charge,
        "drop_charge": drop_charge, "subtotal": subtotal, "fuel": fuel,
        "min_applied": total > gross, "total": total,
        "currency": rc.get("currency", "GBP"),
    }


def miles_between(coll: str | None, deliv: str | None, coord_idx: dict) -> float | None:
    """Great-circle miles between two postcodes, or None when either is missing
    or hasn't been geocoded yet."""
    if not coll or not deliv:
        return None
    a, b = coord_idx.get(coll), coord_idx.get(deliv)
    if not a or not b:
        return None
    return haversine_miles(a["lat"], a["lng"], b["lat"], b["lng"])


def rows_with_quotes(rate_card: dict | None, bookings: list[dict], coord_idx: dict) -> list[dict]:
    """One quoted row per TRANSPORT booking (office rows — spaces null — skipped),
    newest first. `coord_idx` maps an upper-cased postcode → {lat,lng}."""
    out = []
    for b in bookings:
        if b.get("spaces") is None:
            continue
        coll = (b.get("collection_postcode") or "").upper() or None
        deliv = (b.get("delivery_postcode") or "").upper() or None
        miles = miles_between(coll, deliv, coord_idx)
        q = quote(rate_card, b.get("spaces") or 0, miles)
        out.append({
            "id": b.get("id"),
            "ref": b.get("booking_ref") or (b.get("id") or "")[:8],
            "date": str(b.get("booking_date"))[:10],
            "summary": (b.get("summary") or "")[:60],
            "spaces": fmt_spaces(b.get("spaces") or 0),
            "collection": coll or "—", "delivery": deliv or "—",
            "miles": q["miles"], "quote": q,
            "stored": b.get("quoted_amount"),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def view(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    """Fetch + assemble for /app/tms/pricing."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    rate = db.get_rate_card(tenant_id, client=client) or dict(DEFAULT_RATE)
    start = (now.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    bookings = db.list_bookings(tenant_id, start, client=client)
    codes = set()
    for b in bookings:
        for k in ("collection_postcode", "delivery_postcode"):
            if b.get(k):
                codes.add(b[k].upper())
    coord_idx = {r["postcode"]: r for r in db.get_postcodes(sorted(codes))
                 if r.get("lat") is not None}
    rows = rows_with_quotes(rate, bookings, coord_idx)
    return {"rate": rate, "rows": rows, "currency": rate.get("currency", "GBP"),
            "located": sum(1 for r in rows if r["miles"] is not None),
            "total_rows": len(rows)}
