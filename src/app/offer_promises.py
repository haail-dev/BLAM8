"""An offer's follow-up closes with the offer (Kris 2026-09-23).

When the agent offers a customer alternative dates, the reply holds them
(an `offered` booking, released at a deadline) and the same reply leaves a
follow-up: "Daniel confirms whether the alternative 9–11 November slot works".
The follow-up is pulled from the email's words and stored against the EMAIL
(crm_promises.email_log_id) — nothing tied it to the booking, so when the
hold was released, expired or cancelled the follow-up stayed open and the
Concierge read it back as if the offer still stood (live: BK-1066, released
13:12, "due to confirm by 10am tomorrow" at 20:32).

Both rows share the email, so the link is already there: when an offered
booking closes, the open "does the customer take the offer?" follow-ups from
its email close too, with a line saying why. Other promises from the same
email ("Daniel will send the vaccination card") are untouched — only a
follow-up that is plainly about choosing dates counts.
"""
from __future__ import annotations

import logging
import re

from app import db

log = logging.getLogger(__name__)

# "confirms whether the alternative … slot works", "let us know which dates
# suit", "choose one of the options", "get back to us about the 9–11 Nov slot".
_DECISION = re.compile(
    r"\b(whether|which|if (?:the|those|these|that|any)|let (?:us|you) know|choose|pick|"
    r"decide|get back|confirm)\b", re.I)
_OFFER = re.compile(r"\b(slot|slots|dates?|options?|alternative|offer(?:ed)?|times?|"
                    r"availability|works? for)\b", re.I)


def is_offer_decision(description: str) -> bool:
    """A follow-up that is the customer's yes/no to offered dates."""
    d = str(description or "")
    return bool(_DECISION.search(d) and _OFFER.search(d))


def _when(row: dict) -> str:
    from app import dates as dates_mod, slotgrid
    try:
        span = slotgrid.stay_span(row)
        if span:
            a = slotgrid.abs_to_datetime(span[0]).date()
            b = slotgrid.abs_to_datetime(span[0] + span[1]).date()
            if b > a:
                return f"{dates_mod.pretty_date(a.isoformat())} to {dates_mod.pretty_date(b.isoformat())}"
        return dates_mod.pretty_date(str(row.get("booking_date") or "")[:10])
    except Exception:
        return str(row.get("booking_date") or "")[:10]


def close_for_bookings(tenant_id: str, rows: list[dict], outcome: str, *,
                       client=None) -> int:
    """Close the offer-decision follow-ups behind these just-closed bookings.
    `outcome` reads in the tick's label: released / expired / cancelled /
    accepted. Best-effort — never raises into the booking path. Returns how
    many follow-ups closed."""
    closed = 0
    by_log: dict[str, dict] = {}
    for r in rows or []:
        lid = r.get("email_log_id")
        if lid and lid not in by_log:
            by_log[lid] = r
    if not by_log:
        return 0
    try:
        res = ((client or db.db()).table("crm_promises")
               .select("id,description,direction,contact_id,email_log_id")
               .eq("tenant_id", tenant_id).eq("status", "open")
               .in_("email_log_id", list(by_log)).execute())
        promises = res.data or []
    except Exception:
        log.warning("offer promises: read failed (tenant=%s)", tenant_id, exc_info=True)
        return 0
    for p in promises:
        if p.get("direction") != "they_owe" or not is_offer_decision(p.get("description")):
            continue
        row = by_log[p["email_log_id"]]
        label = f"offer of {_when(row)} {outcome}" + (f" ({row.get('booking_ref')})" if row.get("booking_ref") else "")
        try:
            db.crm_resolve_promise_auto(p["id"], tenant_id,
                                        evidence=f"The {label} — nothing left for them to decide.",
                                        by_kind="booking", by_id=row.get("id"), by_label=label)
            closed += 1
        except Exception:
            log.warning("offer promises: close failed (promise=%s)", p.get("id"), exc_info=True)
    return closed


def note_closed(tenant_id: str, rows: list[dict], outcome: str, *, client=None) -> None:
    """One agent event per offer that closed, so a released hold is never a
    mystery in the log (BK-1066 left no trace), then the follow-ups."""
    for r in rows or []:
        try:
            db.log_agent_event(
                tenant_id, "offer_closed",
                f"Offered dates {_when(r)} ({r.get('booking_ref') or 'no ref'}) {outcome}",
                contact=r.get("contact"), booking_id=r.get("id"), booking_ref=r.get("booking_ref"),
                client=client)
        except Exception:
            log.warning("offer promises: event failed", exc_info=True)
    close_for_bookings(tenant_id, rows, outcome, client=client)
