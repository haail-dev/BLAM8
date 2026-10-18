"""Proof of delivery — the post-dispatch status board (TMS baseline).

Pure view-model (à la src/app/tms.py): bookings + their shipment_events in, a POD
board out. A booking's state is the LATEST event on its timeline. The
mark-delivered WRITE path lives in db.mark_delivered (shared with the driver
app); this module only shapes the read. The event spine (shipment_events) is the
same source the tracking map and customer portal read.
"""
from datetime import datetime, timedelta

from app import db
from app.scheduling import DIARY_TZ, _as_date, fmt_spaces

LOOKBACK_DAYS = 7
AHEAD_DAYS = 3

# event kind → (label, Tailwind colour token used by the template)
_STATE = {
    "delivered": ("Delivered", "emerald"),
    "failed": ("Failed", "rose"),
    "out_for_delivery": ("Out for delivery", "blue"),
    "loaded": ("Loaded", "amber"),
    "booked": ("Booked", "slate"),
}


def latest_state(events_for_booking: list[dict]) -> dict:
    """The current POD state from a booking's event timeline (db returns these
    oldest-first, so the last is newest). The signature/photo come from the
    delivered/failed event specifically."""
    if not events_for_booking:
        return {"kind": "pending", "label": "Awaiting", "colour": "slate",
                "signed_by": None, "photo_url": None, "note": None, "at": None}
    last = events_for_booking[-1]
    label, colour = _STATE.get(last.get("kind"), (str(last.get("kind")).title(), "slate"))
    pod = next((e for e in reversed(events_for_booking)
                if e.get("kind") in ("delivered", "failed")), None)
    return {
        "kind": last.get("kind"), "label": label, "colour": colour,
        "signed_by": (pod or {}).get("signed_by"),
        "photo_url": (pod or {}).get("photo_url"),
        "note": (pod or {}).get("note"),
        "at": (last.get("created_at") or "")[:16].replace("T", " "),
    }


def board(bookings: list[dict], events: list[dict], now: datetime,
          vehicle_names: dict | None = None) -> dict:
    """POD rows for the window around `now`. Transport rows only; undelivered
    first (what the office chases), delivered last."""
    today = now.astimezone(DIARY_TZ).date()
    lo, hi = today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=AHEAD_DAYS)
    by_booking: dict = {}
    for e in events:
        by_booking.setdefault(e.get("booking_id"), []).append(e)
    vnames = vehicle_names or {}
    rows, delivered = [], 0
    seen = set()
    for b in bookings:
        if b.get("spaces") is None or b.get("id") in seen:
            continue
        d = _as_date(b["booking_date"])
        if not (lo <= d <= hi):
            continue
        seen.add(b["id"])
        st = latest_state(by_booking.get(b["id"], []))
        if st["kind"] == "delivered":
            delivered += 1
        rows.append({
            "id": b["id"],
            "ref": b.get("booking_ref") or (b.get("id") or "")[:8],
            "date": str(b.get("booking_date"))[:10],
            "summary": (b.get("summary") or "")[:60],
            "spaces": fmt_spaces(b.get("spaces") or 0),
            "delivery": b.get("delivery_postcode") or "—",
            "vehicle": vnames.get(b.get("vehicle_id")) or "—",
            "state": st,
            "closed": st["kind"] == "delivered",
        })
    rows.sort(key=lambda r: (r["closed"], r["date"]))
    return {"rows": rows, "delivered": delivered, "total": len(rows),
            "today": today.isoformat()}


def view(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    """Fetch + assemble for /app/tms/pod. Merges active (proposed/confirmed) and
    delivered (status='completed') rows so the board shows the whole window."""
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    today = now.date()
    start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = (today + timedelta(days=AHEAD_DAYS)).isoformat()
    active = db.list_bookings(tenant_id, start, client=client)
    completed = db.list_completed_bookings(tenant_id, start, end, client=client)
    events = db.list_shipment_events(tenant_id, client=client)
    vnames = {v["id"]: v["name"] for v in db.list_vehicles(tenant_id, client=client)}
    return board(active + completed, events, now, vehicle_names=vnames)
