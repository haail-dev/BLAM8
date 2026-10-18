"""Confirming a booking: ONE function for the Diary button and the Concierge.

The Diary's Confirm (dashboard.diary_set_status) and the Concierge's
confirm_booking card must do exactly the same things, so they share this: the
status write (capacity-gated), the job sheet notice, the agent-log line for a
booking tied to an email thread, the live thread repaint, the calendar push and
the CRM timeline/promise tick. A capacity conflict is reported, never raised.
"""
import logging

from app import calendar_sync, crm, crm_intel, db, events
from app.dates import pretty_date

log = logging.getLogger(__name__)


async def confirm(tenant_id: str, booking_id: str, *, client=None,
                  via: str = "by hand in the diary") -> dict:
    """Confirm one booking. Returns {"ok": True, "booking": <row as it was>} or
    {"ok": False, "reason": "missing" | "full"}. `via` ends the agent-log line
    ("Confirmed BK-1015 (…) <via>")."""
    from app.booking_errors import capacity_conflict
    # Read BEFORE the write: the event should say what the row WAS.
    booking = db.get_booking(booking_id, tenant_id, client=client)
    if not booking:
        return {"ok": False, "reason": "missing"}
    try:
        db.set_booking_status(booking_id, "confirmed", tenant_id, client=client)
    except Exception as exc:
        if not capacity_conflict(exc):
            raise
        return {"ok": False, "reason": "full", "booking": booking}
    from app import job_sheets   # the people on it get it (src/app/job_sheets.py)
    job_sheets.soon(tenant_id, booking_id)
    # Agent Log: a human decision earns a line only when the booking is tied to
    # an email thread (pure manual housekeeping stays out of the agent's story).
    if booking.get("email_log_id"):
        ref = booking.get("booking_ref") or str(booking_id)[:8]
        when = " — ".join(x for x in [pretty_date(booking.get("booking_date")),
                                      str(booking.get("window") or "")] if x)
        db.log_agent_event(
            tenant_id, "booking_manual",
            f"Confirmed {ref}{f' ({when})' if when else ''} {via}",
            contact=booking.get("contact"), booking_id=booking_id,
            booking_ref=booking.get("booking_ref"),
            event_date=str(booking.get("booking_date") or "") or None)
    if booking.get("contact"):
        events.publish(tenant_id, {"type": "status_changed",
                                   "log_id": booking.get("email_log_id"),
                                   "contact": booking.get("contact"),
                                   "booking_id": booking_id, "status": "confirmed"})
    await calendar_sync.push_bookings(tenant_id, booking_id=booking_id)
    try:   # CRM: a confirmed hold joins the contact's timeline, and any promise
           # it fulfils ticks off (2026-09-03).
        crm.note_booking_activity(tenant_id, db.get_booking(booking_id, tenant_id))
        await crm_intel.resolve_from_action(
            tenant_id, booking.get("contact"),
            f"Confirmed booking {booking.get('booking_ref') or ''} for "
            f"{booking.get('booking_date')} — {booking.get('summary') or ''}",
            client=client, ref=("booking", booking_id, booking.get("booking_ref")))
    except Exception:
        log.warning("CRM booking activity failed for booking %s", booking_id, exc_info=True)
    return {"ok": True, "booking": booking}
