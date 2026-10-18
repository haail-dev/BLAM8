"""Live tracking + the customer portal — both off the event spine (TMS baseline).

DEMO-GRADE & HONEST: there is no GPS feed. Each fleet resource's map position is
SIMULATED — it points at its next undelivered drop (or sits at the branch when the
run is done) and the page says so plainly. The shape is exactly what a real
tracker integration would fill. The public portal timeline reads the very same
shipment_events the POD board writes.
"""
from datetime import datetime

from app import db
from app.config import settings
from app.pod import latest_state
from app.scheduling import DIARY_TZ, _as_date

# The customer-facing journey, in order. 'booked' is implicit (the order exists).
TIMELINE = [
    ("booked", "Order received"),
    ("loaded", "Loaded for delivery"),
    ("out_for_delivery", "Out for delivery"),
    ("delivered", "Delivered"),
]


def fleet_positions(vehicles: list[dict], bookings: list[dict], events: list[dict],
                    day, depot_postcode: str | None = None) -> list[dict]:
    """One live row per active fleet resource that has drops today: its simulated
    position (the next undelivered drop's postcode, else the branch) and progress."""
    by_booking: dict = {}
    for e in events:
        by_booking.setdefault(e.get("booking_id"), []).append(e)
    rows = []
    for v in vehicles:
        if not v.get("active", True):
            continue
        stops = [b for b in bookings
                 if b.get("vehicle_id") == v["id"] and b.get("spaces") is not None
                 and _as_date(b["booking_date"]) == day]
        if not stops:
            continue
        done, pending = [], []
        for b in stops:
            (done if latest_state(by_booking.get(b["id"], []))["kind"] == "delivered"
             else pending).append(b)
        if pending:
            target = (pending[0].get("delivery_postcode") or "").upper() or depot_postcode
            status = "En route" if done else "Starting run"
            nxt = (pending[0].get("summary") or "")[:40]
        else:
            target, status, nxt = depot_postcode, "Back at depot", "All drops done"
        rows.append({
            "id": v["id"], "name": v["name"], "registration": v.get("registration"),
            "delivered": len(done), "total": len(stops),
            "pct": round(100 * len(done) / len(stops)) if stops else 0,
            "status": status, "postcode": target, "next_summary": nxt,
        })
    return rows


def view(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    """Fetch + assemble for /app/tms/tracking."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    day = now.date()
    vehicles = db.list_vehicles(tenant_id, client=client, active_only=True)
    branches = db.list_branches(tenant_id, client=client)
    depot = branches[0].get("postcode") if branches else None
    active = db.list_bookings(tenant_id, day.isoformat(), client=client)
    completed = db.list_completed_bookings(tenant_id, day.isoformat(), day.isoformat(), client=client)
    bookings = [b for b in (active + completed) if _as_date(b["booking_date"]) == day]
    events = db.list_shipment_events(tenant_id, client=client)
    rows = fleet_positions(vehicles, bookings, events, day, depot_postcode=depot)
    return {"vehicles_live": rows, "day": day.isoformat(), "depot_postcode": depot,
            "maps_key": settings.google_places_api_key, "active_count": len(rows)}


def public_timeline(booking: dict, events: list[dict]) -> dict:
    """The customer-facing status of one shipment (the public portal). Pure."""
    kinds = {e.get("kind") for e in events}
    failed = "failed" in kinds
    reached = ["booked"] + [k for k, _ in TIMELINE[1:] if k in kinds]
    current = reached[-1]
    cur_idx = [k for k, _ in TIMELINE].index(current)
    steps = [{"key": k, "label": label, "done": i <= cur_idx, "current": k == current}
             for i, (k, label) in enumerate(TIMELINE)]
    pod = next((e for e in reversed(events) if e.get("kind") in ("delivered", "failed")), None)
    return {
        "ref": booking.get("booking_ref") or (booking.get("id") or "")[:8],
        "summary": booking.get("summary"),
        "date": str(booking.get("booking_date"))[:10],
        "destination": booking.get("delivery_postcode"),
        "steps": steps, "failed": failed, "delivered": "delivered" in kinds,
        "signed_by": (pod or {}).get("signed_by") if "delivered" in kinds else None,
        "updated": (events[-1].get("created_at") or "")[:16].replace("T", " ") if events else None,
    }


def portal_view(token: str) -> dict | None:
    """Resolve a public tracking token → render-ready timeline + carrier name, or
    None if the token is unknown. Service role (the token is the capability)."""
    booking = db.get_booking_by_track_token(token)
    if not booking:
        return None
    events = db.list_events_for_booking(booking["id"])
    carrier = (db.get_tenant(booking["tenant_id"]) or {}).get("name") or "Your carrier"
    return {"carrier": carrier, **public_timeline(booking, events)}
