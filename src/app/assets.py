"""Assets OS — time-limited hire of property/equipment (docs/os/assets.md).

Phase A: the pure view-models + the unit-scoped availability read. No new capacity
math — this module COMPOSES the shipped scheduling primitives (slotgrid spans,
db.hold_unit_booking) exactly as pod.board / scheduling.diary do. The genuinely new
part of Assets OS is the SUPPLY-SIDE identity: which specific asset (a unit), its
running condition, its rate card, its next service date.

Phase A ships the `board` (handovers today + the upcoming lane) and `quote_unit`;
the damage/maintenance lane, deposits and condition photo-diffs are Phases B–D, built
in their own modules: src/app/assets_deposits.py (B) and src/app/assets_condition.py (C), with
maintenance in D (2c15e6c) — not here (§2, §13).
"""
import secrets
from datetime import datetime, timedelta

from app import db
from app.scheduling import DIARY_TZ, _as_date
from app import slotgrid

UPCOMING_DAYS = 14      # §2 "next 14 days" upcoming lane

# The fixed shot list drives the capture prompts AND the ordinal pairing the diff
# pipeline relies on (out[i] ↔ in[i]) — the same list is shown at handover and return,
# so photo N is the same view both times (§12b step 1). A unit overrides via
# asset_units.custom_data._shot_list; else a sensible default per category.
DEFAULT_SHOT_LIST = ["Front", "Interior", "Mechanism / engine", "Accessories"]
SHOT_LISTS = {
    "cottage": ["Living area", "Kitchen", "Bedroom", "Bathroom", "Exterior"],
    "car": ["Front", "Rear", "Interior", "Dashboard / mileage", "Wheels & tyres"],
    "digger": ["Front", "Cab / controls", "Hydraulics", "Tracks / tyres", "Attachments"],
    "marquee": ["Exterior", "Interior", "Frame / joints", "Flooring", "Accessories"],
    "kayak": ["Hull top", "Hull bottom", "Seat / cockpit", "Paddle & accessories"],
    "tool": ["Front", "Working end", "Body / casing", "Accessories"],
}


def shot_list_for(unit: dict | None) -> list[str]:
    """The fixed shot list for a unit — its custom override, else the category default,
    else the generic list. Shown identically at handover and return so ordinal pairing
    (out[i] ↔ in[i]) is meaningful."""
    custom = ((unit or {}).get("custom_data") or {}).get("_shot_list")
    if isinstance(custom, list) and custom:
        return [str(s) for s in custom]
    return SHOT_LISTS.get((unit or {}).get("category"), DEFAULT_SHOT_LIST)


def mint_capture_token() -> str:
    """A URL-safe capture-link token per handover (the track_token convention)."""
    return secrets.token_urlsafe(24)


def _stay_dates(b: dict) -> tuple:
    """(arrival_date, return_date) for a hire from its grid span — arrival is the booking's
    date, return is arrival + the span's whole-day length. Uses slotgrid.stay_span (the
    SHIPPED span reader: booking_date + start_block + length_blocks). None when unmapped."""
    from datetime import date as _date
    span = slotgrid.stay_span(b)
    if not span:
        return None, None
    lo, length = span
    bpd = slotgrid.BLOCKS_PER_DAY
    return _date.fromordinal(lo // bpd), _date.fromordinal((lo + length) // bpd)


def _rate_line(u: dict) -> str | None:
    """A short per-unit rate-card summary for the row ('£120/night · dep £200'),
    reading the per-unit override fields. None when the unit carries no rate."""
    bits = []
    if u.get("rate_daily") is not None:
        bits.append(f"£{float(u['rate_daily']):g}/day")
    elif u.get("rate_weekly") is not None:
        bits.append(f"£{float(u['rate_weekly']):g}/wk")
    if u.get("deposit_default") is not None:
        bits.append(f"dep £{float(u['deposit_default']):g}")
    return " · ".join(bits) or None


def quote_unit(u: dict, nights: int) -> dict | None:
    """A hire total for `nights` nights against a unit's rate card (per-unit override
    of the tenant stay_pricing default). Prefers the daily rate, falls back to weekly/7.
    Pure — the diary's diary_config.quote_stay owns the tenant-default path; this applies
    the unit's own rate. None when the unit has no rate set."""
    n = max(1, int(nights))
    daily = u.get("rate_daily")
    if daily is None and u.get("rate_weekly") is not None:
        daily = float(u["rate_weekly"]) / 7.0
    if daily is None:
        return None
    total = round(float(daily) * n, 2)
    return {"nights": n, "rate_daily": round(float(daily), 2), "total": total,
            "deposit": (float(u["deposit_default"]) if u.get("deposit_default") is not None else None)}


def confirmation_facts(booking: dict, unit: dict | None, *,
                       deposit_amount=None, deposit_url: str | None = None) -> str:
    """Assemble the facts for a hire-confirmation draft (§3 state 3) — passed to
    agent.compose_outbound so the tenant-voice confirmation carries the DEPOSIT LINK. Pure: the
    caller mints the link (assets_deposits.create_deposit_intent) and hands the URL in. When no
    link is minted the facts still confirm the hire; the drafter never invents a URL."""
    name = (unit or {}).get("name") or booking.get("summary") or "your hire"
    when = str(booking.get("booking_date") or "")[:10]
    parts = [f"Confirming the hire of {name}" + (f" from {when}" if when else "") + "."]
    if deposit_amount is not None:
        parts.append(f"A deposit of £{float(deposit_amount):g} secures the booking.")
    if deposit_url:
        parts.append(f"Pay the deposit here to confirm: {deposit_url}")
    return " ".join(parts)


def deposit_chip(deposit: dict | None) -> dict | None:
    """The board's deposit chip state (§2/§6) from a unit_deposits row. Maps the lifecycle
    status to a short label + tone the template renders: held/paid → '✓ held', pending/failed →
    '⏳ unpaid', refunded/released → 'refunded', deducted → '£X deducted'. None when there's no
    deposit row (nothing to show). Pure — reads get_deposit_for_booking's shape."""
    if not deposit:
        return None
    status = deposit.get("status")
    if status in ("held", "paid"):
        return {"label": "✓ held", "tone": "good", "status": status}
    if status in ("pending", "failed"):
        return {"label": "⏳ unpaid", "tone": "warn", "status": status}
    if status in ("refunded", "released"):
        return {"label": "refunded", "tone": "muted", "status": status}
    if status == "deducted":
        amt = deposit.get("deducted_amount") or 0
        return {"label": f"£{float(amt):g} deducted", "tone": "muted", "status": status}
    return {"label": status or "—", "tone": "muted", "status": status}


def board(bookings: list[dict], units: list[dict], now: datetime,
          deposits: dict | None = None) -> dict:
    """The §2 daily surface, two lanes for Phase A (the damage/maintenance lane is Phase C/D):

    - HANDOVERS TODAY: units going OUT today (arrival = today) and coming BACK today
      (return = today), each with the specific unit + hirer + span.
    - UPCOMING: the next 14 days of hires (arrival within the window), unit + deposit chip.

    Pure view-model — composes bookings (their grid spans) + units, à la pod.board. Only
    hires (unit_id set OR hire_state not null) are rows; a plain office booking is ignored.
    """
    today = now.astimezone(DIARY_TZ).date()
    horizon = today + timedelta(days=UPCOMING_DAYS)
    by_unit = {u["id"]: u for u in units}
    deposits = deposits or {}

    handovers, upcoming = [], []
    for b in bookings:
        if b.get("unit_id") is None and not b.get("hire_state"):
            continue
        arrival, ret = _stay_dates(b)
        if arrival is None:
            continue
        unit = by_unit.get(b.get("unit_id"))
        row = {
            "id": b.get("id"),
            "ref": b.get("booking_ref") or (b.get("id") or "")[:8],
            "unit_id": b.get("unit_id"),
            "unit": (unit or {}).get("name") or "Unassigned (pool)",
            "contact": b.get("contact") or "—",
            "arrival": arrival.isoformat(),
            "return": ret.isoformat() if ret else None,
            "hire_state": b.get("hire_state"),
            "status": b.get("status"),
            "deposit": b.get("deposit_amount"),
            "deposit_chip": deposit_chip(deposits.get(b.get("id"))),
        }
        if arrival == today:
            handovers.append({**row, "direction": "out"})
        if ret and ret == today:
            handovers.append({**row, "direction": "back"})
        if today < arrival <= horizon:
            upcoming.append(row)

    handovers.sort(key=lambda r: (r["direction"] != "out", r["unit"]))
    upcoming.sort(key=lambda r: (r["arrival"], r["unit"]))
    return {
        "today": today.isoformat(),
        "today_label": today.strftime("%A %d %B"),
        "handovers": handovers,
        "upcoming": upcoming,
        "handover_count": len(handovers),
        "upcoming_count": len(upcoming),
    }


def maintenance_row(unit: dict) -> dict:
    """One DAMAGE & MAINTENANCE lane row for a service-due / below-floor unit (§2/§11).
    Pure — reads the flags asset_units_due_service stamps (_due_soon / _below_floor) plus
    the unit's condition + next-service date, into the shape the board template renders."""
    rating = unit.get("condition_rating")
    floor = unit.get("condition_floor")
    reasons = []
    if unit.get("_due_soon") and unit.get("next_service_due"):
        reasons.append(f"service due {str(unit['next_service_due'])[:10]}")
    if unit.get("_below_floor"):
        reasons.append(f"condition {float(rating):g} below floor {float(floor):g}")
    return {
        "id": unit.get("id"),
        "unit": unit.get("name") or "Unit",
        "category": unit.get("category"),
        "condition": (float(rating) if rating is not None else None),
        "next_service_due": (str(unit["next_service_due"])[:10]
                             if unit.get("next_service_due") else None),
        "reason": " · ".join(reasons) or "needs attention",
        "due_soon": bool(unit.get("_due_soon")),
        "below_floor": bool(unit.get("_below_floor")),
    }


def maintenance_lane(tenant_id: str, *, within_days: int = 14, client=None) -> list[dict]:
    """The board's maintenance rows (§2 lane 3): units whose next_service_due falls inside
    the lead window OR whose condition dropped below floor. Composes db.asset_units_due_service
    (the FSM list_jobs_due_service twin) into render-ready rows. Best-effort — a read failure
    returns [] so the board still renders (à la the open-damage read in view)."""
    try:
        due = db.asset_units_due_service(tenant_id, within_days=within_days, client=client)
    except Exception:
        return []
    return [maintenance_row(u) for u in due]


def capture_phase(tenant_id: str, booking_id: str) -> str:
    """Which capture this is: 'out' (handover) if no out-report exists yet for the
    booking, else 'in' (return). The diff pipeline triggers on the 'in' report."""
    return "in" if db.latest_out_report(tenant_id, booking_id) else "out"


def evidence_bundle(tenant_id: str, case: dict, *, client=None) -> dict:
    """Assemble the /app/assets/damage/{id} evidence bundle (§12b): the case, its unit +
    hire, the out/in reports, and the diff findings each with a SIGNED URL pair (out vs
    in) for display. Signed URLs are short-TTL (never public). Pure of routing — the
    route renders this. A missing signed URL (storage down) degrades to None (the page
    shows 'photo unavailable') rather than crashing."""
    from app import storage
    unit = db.asset_get_unit(case.get("unit_id"), tenant_id, client=client) \
        if case.get("unit_id") else None
    booking = db.get_booking(case.get("booking_id"), tenant_id, client=client) \
        if case.get("booking_id") else None
    deposit = db.get_deposit_for_booking(tenant_id, case.get("booking_id"), client=client) \
        if case.get("booking_id") else None
    findings = []
    for f in (case.get("diff_findings") or []):
        findings.append({
            **f,
            "out_signed": storage.signed_url(f.get("out_url"), client=client),
            "in_signed": storage.signed_url(f.get("in_url"), client=client),
        })
    out_report = db.get_condition_report(case.get("out_report_id"), tenant_id, client=client) \
        if case.get("out_report_id") else None
    in_report = db.get_condition_report(case.get("in_report_id"), tenant_id, client=client) \
        if case.get("in_report_id") else None
    # A deduction needs an out-report baseline (§14 — no baseline, no claim). Hard-block
    # the deduct action on the page when there's none.
    can_deduct = bool(out_report) and bool(deposit)
    return {
        "case": case, "unit": unit, "booking": booking, "deposit": deposit,
        "findings": findings, "out_report": out_report, "in_report": in_report,
        "deposit_chip": deposit_chip(deposit), "can_deduct": can_deduct,
        "has_baseline": bool(out_report),
    }


def view(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    """Fetch + assemble for /app/assets. Reads the tenant's active hires from today
    forward and the unit registry, then composes the board."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    # From LOOKBACK so a hire that started earlier but RETURNS today still lands on the
    # handovers lane; list_bookings gives proposed+confirmed from a date forward.
    start = (now.date() - timedelta(days=UPCOMING_DAYS)).isoformat()
    bookings = db.list_bookings(tenant_id, start, client=client)
    units = db.asset_list_units(tenant_id, client=client)
    # Deposit chips (Phase B): one query for the whole working set, keyed by booking id.
    deposits = db.deposits_for_bookings(
        tenant_id, [b.get("id") for b in bookings], client=client)
    b = board(bookings, units, now, deposits=deposits)
    # Attach the per-unit rate line for the registry preview on the board footer.
    b["units"] = [{**u, "rate_line": _rate_line(u)} for u in units]
    # Damage & maintenance lane (§2): open damage cases (Phase C) + service-due / below-floor
    # units (Phase D — the FSM maintenance cadence surfaced on the board).
    try:
        b["open_damage"] = db.list_damage_cases(tenant_id, status="open", client=client)
    except Exception:  # the board must render even if the damage read fails
        b["open_damage"] = []
    b["open_damage_count"] = len(b["open_damage"])
    b["maintenance"] = maintenance_lane(tenant_id, client=client)
    b["maintenance_count"] = len(b["maintenance"])
    return b
