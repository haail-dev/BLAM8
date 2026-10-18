"""The customer-writable log — slice 5 of docs/plan-progress-packages.md
(2026-09-05).

The adult side's most-praised feature (docs/research-adult-classes.md §4):
Total Drive's reflective log the participant "reviews and adds to at home", a session
check-in, a participant's own logbook. A customer who IS the participant may add
a note to one of their own bookings from the portal — "felt confident with
the first stage, still rushing the final check" — and the business reads it on
the job page's register, in the drafter's context and on Today's deck.

Two rules, both from the research: it is ON by default and a SETTING
(Settings → Portal: "Customers can add notes to their own record"); and it
is OFF for dependants — a guardian editing a dependant's record is a safeguarding
question we do not open, so a booking that is ABOUT a dependant refuses the
customer's note and says the business keeps that record.

Notes are append-only rows in `booking_notes` (author 'customer' or
'business'); the register (booking_attendance) is untouched.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from app import db
from app.dates import pretty_date

log = logging.getLogger(__name__)

MAX_NOTE_CHARS = 600
MAX_NOTES_PER_BOOKING = 20
CONTEXT_NOTES = 6
KIND = "customer_note"
AUTHORS = ("customer", "business")


def on(cfg: dict | None) -> bool:
    """Absent column / NULL = default ON, the portal's own contract."""
    return (cfg or {}).get("portal_customer_notes") is not False


def clean_note(body) -> str:
    return " ".join(str(body or "").split())[:MAX_NOTE_CHARS]


def _c(client=None):
    return client or db.db()


def list_for_booking(tenant_id: str, booking_id: str, *, client=None) -> list[dict]:
    try:
        return (_c(client).table("booking_notes").select("*").eq("tenant_id", tenant_id)
                .eq("booking_id", booking_id).is_("deleted_at", "null").order("created_at").execute().data or [])
    except Exception:
        log.warning("booking_notes read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def list_for_contact(tenant_id: str, contact_email: str | None, *, limit: int = 50,
                     client=None) -> list[dict]:
    if not contact_email:
        return []
    try:
        return (_c(client).table("booking_notes").select("*").eq("tenant_id", tenant_id)
                .eq("contact_email", contact_email).is_("deleted_at", "null").order("created_at", desc=True)
                .limit(limit).execute().data or [])
    except Exception:
        log.warning("booking_notes read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def refusal_for_customer(booking: dict, subjects: list[dict]) -> dict | None:
    """Pure: the customer may write only to a booking that is THEIR OWN (no
    dependant subject on it) and that is theirs (contact matches)."""
    if subjects:
        names = ", ".join(s.get("name") for s in subjects if s.get("name")) or "a dependant"
        return {"error": "dependant_record",
                "message": (f"That booking is about {names} — the business keeps a dependant's "
                            "record, so a customer cannot add notes to it. Pass the note to the "
                            "business with escalate_to_business (unless an earlier turn already "
                            "did — then say so and move on).")}
    return None


def add_note(tenant_id: str, booking: dict, body: str, *, author: str = "customer",
             contact_email: str | None = None, contact_name: str | None = None,
             client=None) -> dict:
    """Append one note. A customer's note is refused on a dependant's booking
    (the rule above); the business may write to any booking."""
    text = clean_note(body)
    if not text:
        return {"error": "empty", "message": "The note is empty."}
    author = author if author in AUTHORS else "customer"
    if author == "customer":
        try:
            subs = db.subjects_for_bookings(tenant_id, [booking["id"]], client=client).get(booking["id"], [])
        except Exception:
            subs = []
        ref = refusal_for_customer(booking, subs)
        if ref:
            return ref
    existing = list_for_booking(tenant_id, booking["id"], client=client)
    if len(existing) >= MAX_NOTES_PER_BOOKING:
        return {"error": "too_many", "message": "That booking already has the maximum number of notes."}
    row = {"tenant_id": tenant_id, "booking_id": booking["id"], "booking_ref": booking.get("booking_ref"),
           "contact_email": contact_email or booking.get("contact"), "author": author, "body": text}
    try:
        res = _c(client).table("booking_notes").insert(row).execute()
        row = res.data[0] if res.data else row
    except Exception:
        log.warning("booking_notes insert failed (tenant=%s)", tenant_id, exc_info=True)
        return {"error": "write_failed", "message": "The note could not be saved just now."}
    ref = booking.get("booking_ref") or "the booking"
    who = contact_name or contact_email or booking.get("contact") or "The customer"
    db.log_agent_event(tenant_id, KIND if author == "customer" else "booking_note",
                       (f"{who} added a note to {ref}: “{text[:120]}”" if author == "customer"
                        else f"Note on {ref}: {text[:120]}"),
                       contact=row.get("contact_email"), booking_id=booking.get("id"),
                       booking_ref=booking.get("booking_ref"), event_date=booking.get("booking_date"))
    if author == "customer":
        try:
            from app import push
            push.notify(tenant_id, title=f"{who} added a note — {ref}",
                        body=text[:140], url=f"/app/diary/{booking['id']}#notes",
                        tag=f"note-{row.get('id') or booking['id']}", kind=KIND,
                        contact=row.get("contact_email"), contact_name=contact_name)
        except Exception:
            log.warning("customer note push failed (tenant=%s)", tenant_id, exc_info=True)
    return {"added": True, "note": row, "booking_ref": booking.get("booking_ref")}


def withdraw_last(tenant_id: str, booking: dict, contact_email: str, *, client=None) -> dict:
    """5b nit: a customer may withdraw their OWN latest note on a booking (a
    soft delete — the business's lines are never touched)."""
    mine = [n for n in list_for_booking(tenant_id, booking["id"], client=client)
            if n.get("author") == "customer" and (n.get("contact_email") or "").lower() == (contact_email or "").lower()]
    if not mine:
        return {"error": "no_note", "message": "There is no note of theirs on that booking to withdraw."}
    last = mine[-1]
    try:
        _c(client).table("booking_notes").update({"deleted_at": datetime.now(timezone.utc).isoformat()}) \
            .eq("id", last["id"]).eq("tenant_id", tenant_id).execute()
    except Exception:
        log.warning("booking_notes withdraw failed (tenant=%s)", tenant_id, exc_info=True)
        return {"error": "write_failed", "message": "The note could not be withdrawn just now."}
    db.log_agent_event(tenant_id, KIND, f"{contact_email} withdrew their note on {booking.get('booking_ref') or 'a booking'}",
                       contact=contact_email, booking_id=booking.get("id"), booking_ref=booking.get("booking_ref"))
    return {"withdrawn": True, "note": last.get("body"), "booking_ref": booking.get("booking_ref")}


def latest_customer_note(notes: list[dict]) -> str:
    """The register's hint (5b nit): what the customer last wrote on this booking."""
    mine = [n for n in notes if n.get("author") == "customer"]
    return (mine[-1].get("body") or "") if mine else ""


def lines(notes: list[dict], *, limit: int = CONTEXT_NOTES) -> list[str]:
    """The log in words, newest first: "12th Sep 26 (BK-1028, their note): felt
    confident on roundabouts"."""
    out = []
    for n in notes[:limit]:
        when = pretty_date(str(n.get("created_at") or "")[:10])
        ref = n.get("booking_ref") or ""
        who = "their note" if n.get("author") == "customer" else "the business"
        out.append(f"{when}{f' ({ref}, {who})' if ref else f' ({who})'}: {n.get('body')}")
    return out


def context_block(tenant_id: str, contact_email: str | None, *, client=None,
                  nouns: dict | None = None) -> str:
    """For the drafter and the portal: the customer's own log, newest first."""
    rows = list_for_contact(tenant_id, contact_email, limit=CONTEXT_NOTES, client=client)
    ls = lines(rows)
    if not ls:
        return ""
    from app import diary_config
    what = diary_config.plural((nouns or {}).get("booking") or "booking")
    return (f"The customer's own notes on their {what} (they wrote these on the portal — refer "
            "to them when they ask how they are getting on; never contradict a fact they recorded):\n"
            + "\n".join("- " + ln for ln in ls))


def job_context(tenant_id: str, booking: dict, *, client=None) -> dict:
    """The job page: this booking's notes, both authors, oldest first."""
    notes = list_for_booking(tenant_id, booking["id"], client=client)
    return {"booking_notes": notes, "customer_note_hint": latest_customer_note(notes)}
