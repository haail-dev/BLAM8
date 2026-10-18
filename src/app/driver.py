"""Driver app — a mobile run sheet over the day's manifest (TMS baseline).

Pure view-model: one fleet resource's bookings for a day + their shipment events in, a
tap-through stop list out. Marking a stop delivered uses the SAME write path as
the office POD board (db.mark_delivered → a shipment_events row + booking
'completed'). Authenticated for the demo; a tokenised per-driver link is the
production follow-on.
"""
from datetime import datetime

from app import db
from app.pod import latest_state
from app.scheduling import DIARY_TZ, _as_date, fmt_spaces


def run(vehicle: dict | None, bookings: list[dict], events: list[dict], day) -> dict:
    """One fleet resource's stop list for one day, undelivered first."""
    by_booking: dict = {}
    for e in events:
        by_booking.setdefault(e.get("booking_id"), []).append(e)
    vid = (vehicle or {}).get("id")
    stops, delivered, seen = [], 0, set()
    for b in bookings:
        if b.get("spaces") is None or b.get("id") in seen:
            continue
        if b.get("vehicle_id") != vid or _as_date(b["booking_date"]) != day:
            continue
        seen.add(b["id"])
        st = latest_state(by_booking.get(b["id"], []))
        if st["kind"] == "delivered":
            delivered += 1
        stops.append({
            "id": b["id"],
            "ref": b.get("booking_ref") or (b.get("id") or "")[:8],
            "summary": b.get("summary") or "",
            "spaces": fmt_spaces(b.get("spaces") or 0),
            "collection": b.get("collection_postcode") or "",
            "delivery": b.get("delivery_postcode") or "",
            "state": st,
            "done": st["kind"] == "delivered",
        })
    stops.sort(key=lambda s: s["done"])
    return {"vehicle": vehicle, "stops": stops, "delivered": delivered,
            "total": len(stops), "day": day.isoformat()}


def view(tenant_id: str, vehicle_id: str | None = None, client=None,
         now: datetime | None = None) -> dict:
    """Fetch + assemble for /app/driver. Defaults to the first active fleet resource."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    day = now.date()
    vehicles = db.list_vehicles(tenant_id, client=client, active_only=True)
    vehicle = (next((v for v in vehicles if v["id"] == vehicle_id), None)
               or (vehicles[0] if vehicles else None))
    bookings: list[dict] = []
    if vehicle:
        active = db.list_vehicle_day_bookings(tenant_id, vehicle["id"], day.isoformat(), client=client)
        completed = [b for b in db.list_completed_bookings(
                        tenant_id, day.isoformat(), day.isoformat(), client=client)
                     if b.get("vehicle_id") == vehicle["id"]]
        bookings = active + completed
    events = db.list_shipment_events(tenant_id, client=client)
    return {**run(vehicle, bookings, events, day), "vehicles": vehicles,
            "selected": vehicle["id"] if vehicle else None}
