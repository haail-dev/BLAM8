"""Owner-facing accounts rendered from recorded inputs and executor results.

Unknown results stay unconfirmed. Customer details, rule refusals, verification
failures and unsupported requests remain distinct. Technical evidence is stored
beside the prose, never mixed into the owner's working panel.
"""
from __future__ import annotations

import json

from app.dates import pretty_date

# Per tool: the line while it runs, the line if it was refused, the line if it
# worked, and whether the dates read straight after the verb ("Held 12th Oct")
# or need a preposition ("Checking the diary FOR 12th Oct"). Separate try/did
# verbs matter: "Tried to hold 12th Oct — held as BK-1036" is how a machine
# talks, and the success case is the one an owner reads most.
_VERBS = {
    "check_availability":             ("Checking the diary", "Checked the diary", "Checked the diary", "for"),
    "check_appointment_availability": ("Checking the diary", "Checked the diary", "Checked the diary", "for"),
    "hold_booking":                   ("Holding", "Tried to hold", "Held", ""),
    "hold_appointment":               ("Holding an appointment", "Tried to hold an appointment", "Held an appointment", "for"),
    "offer_slots":                    ("Trying to reserve times", "Tried to offer slots", "Offered slots", "for"),
    "offer_stay":                     ("Trying to reserve dates", "Tried to offer dates", "Offered dates", ""),
    "join_waitlist":                  ("Adding them to the waiting list", "Tried the waiting list", "Added them to the waiting list", "for"),
    "cancel_booking":                 ("Cancelling", "Tried to cancel", "Cancelled", ""),
    "move_booking":                   ("Moving", "Tried to move", "Moved", ""),
    "check_stock":                    ("Checking stock", "Checked stock", "Checked stock", "for"),
    "reserve_stock":                  ("Reserving stock", "Tried to reserve stock", "Reserved stock", "for"),
    "ask_transport":                  ("Asking a specialist", "Asked a specialist", "Asked a specialist", "about"),
    "ask_field_service":              ("Asking a specialist", "Asked a specialist", "Asked a specialist", "about"),
}


def _fallback(name: str) -> str:
    """A tool nobody taught this module about still reads as English."""
    return "Checking this request"


def _when(args) -> str:
    """The dates a call is about, as the operator reads them ('12th Oct 26 to
    26th Oct 26'). Empty when the call carries none — a bare verb is better
    than an invented date."""
    if not isinstance(args, dict):
        return ""
    # offer_stay carries its spans in a list, so read the first one — the
    # panel names the dates offered, not the shape of the argument.
    spans = args.get("stays")
    if isinstance(spans, list) and spans and isinstance(spans[0], dict):
        args = {**args, "arrival": spans[0].get("arrival"),
                "departure": spans[0].get("departure")}
    arrive = args.get("arrival") or args.get("from") or args.get("start") or args.get("date_from")
    depart = args.get("departure") or args.get("to") or args.get("end") or args.get("date_to")
    if arrive and depart:
        a, d = pretty_date(arrive), pretty_date(depart)
        return a if a == d else f"{a} to {d}"
    one = (arrive or depart or args.get("date") or args.get("booking_date")
           or args.get("new_date"))
    if one:
        day = pretty_date(one)
        window = {"am": "morning", "pm": "afternoon", "eve": "evening", "day": ""}.get(
            str(args.get("window") or ""), str(args.get("window") or ""))
        return f"{day} ({window})" if window and window != "day" else day
    return ""


def _ref(result) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("booking_ref") or result.get("ref") or "").strip()


def _nights(result) -> str:
    n = (result or {}).get("nights") if isinstance(result, dict) else None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n} night{'' if n == 1 else 's'}"


def _parts(name: str):
    v = _VERBS.get(name)
    if v:
        return v
    word = _fallback(name)
    return (word, word, word, "for")


def _head(verb: str, joiner: str, when: str) -> str:
    if not when:
        return verb
    return f"{verb} {joiner} {when}".replace("  ", " ") if joiner else f"{verb} {when}"


def intent(name: str, args) -> str:
    """The line that goes up the moment the call starts."""
    doing, _, _, joiner = _parts(name)
    return _head(doing, joiner, _when(args)) + "…"


def outcome(name: str, args, result) -> str:
    """The line that replaces it when the call comes back — the half that was
    missing, and the half that carries the meaning."""
    _, tried, _, joiner = _parts(name)
    when = _when(args)
    head = _head(tried, joiner, when)

    if not isinstance(result, dict):
        return f"{head} — the result could not be confirmed."

    err = result.get("error")
    if err:
        if err == "missing_info" and result.get("reserved") is True:
            return "Reserved time while waiting for more customer details."
        if err == "missing_info":
            missing = result.get("missing") if isinstance(result.get("missing"), list) else []
            # Configured labels are useful; executor field names are not prose.
            labels = [str(m) for m in missing if isinstance(m, str)
                      and "_" not in m and m not in {"subjects", "arrival", "departure", "date", "window"}]
            tail = ": " + "; ".join(labels) if labels else ""
            return f"{head} — more customer details needed{tail}."
        if err in {"already_booked", "already_held", "already_offered", "already_reserved"}:
            word = {"already_booked": "booked", "already_held": "held",
                    "already_offered": "reserved", "already_reserved": "reserved"}[err]
            ref = _ref(result)
            return f"{head} — already {word}{f' ({ref})' if ref else ''}."
        if err in {"unsupported", "not_configured", "front_desk", "unknown_tool"}:
            return f"{head} — this needs to be arranged by you."
        if err in _RULE_REASONS:
            return f"{head} — could not proceed: {_RULE_REASONS[err]}."
        # Unknown errors and failed checks must never be described as a rule refusal.
        return f"{head} — the result could not be confirmed."

    if result.get("status") == "offered" and isinstance(result.get("offered"), list):
        offered = [o for o in result["offered"] if isinstance(o, dict)]
        if not offered:
            return "No times were reserved."
        refs = [str(o["booking_ref"]) for o in offered if o.get("booking_ref")]
        until = str(result.get("hold_until_phrase") or "").strip()
        tail = f" ({', '.join(refs)})" if refs else ""
        return (f"Reserved {len(offered)} option{'' if len(offered) == 1 else 's'}{tail}"
                + (f" until {until}" if until else "") + ". Nothing booked yet.")
    # Use returned dates when present: an accepted alternative may differ from the ask.
    actual_when = _when(result) or when
    if any(result.get(k) is True for k in ("held", "booked", "cancelled", "moved", "joined", "reserved")):
        verb = ("Cancelled" if result.get("cancelled") is True else
                "Moved" if result.get("moved") is True else
                "Booked" if result.get("booked") is True else
                "Reserved" if result.get("reserved") is True else
                "Added them to the waiting list" if result.get("joined") is True else "Held")
        ref = _ref(result)
        return _head(verb, "for" if verb.startswith("Added") else "", actual_when) + (f" — {ref}." if ref else ".")
    if name in {"check_stock", "reserve_stock"}:
        if result.get("found") is False:
            return "The item was not found in your catalogue."
        if result.get("held") is False:
            return "There was not enough stock to reserve the requested quantity."
        if result.get("found") is True and isinstance(result.get("available"), (int, float)):
            return f"Checked stock — {result['available']:g} available."
    if "available" in result:
        available = result["available"]
        checked = _head("Checked the diary", "for", when)
        if isinstance(available, list):
            n = len(available)
            return (f"{checked} — {n} available option{'' if n == 1 else 's'}." if n else
                    f"{checked} — {_why_none(result)}.")
        if available is True:
            nights = _nights(result)
            return f"{checked} — free{f' for all {nights}' if nights else ''}."
        if available is False:
            return f"{checked} — {_why_none(result)}."
    if result.get("status") == "held":
        ref = _ref(result)
        return f"The specialist held a booking{f' ({ref})' if ref else ''}."
    if result.get("status") == "needs_info":
        return "The specialist needs more customer details."
    if result.get("status") == "no_fit":
        return "The specialist could not arrange this request."
    return f"{head} — the result could not be confirmed."


_RULE_REASONS = {
    "outside_horizon": "the date is outside the permitted booking range",
    "beyond_horizon": "the date is too far ahead to book",
    "hold_limit": "the limit on reservations has been reached",
    "no_other_dates": "offering other dates is switched off",
    "no_waitlist": "the waiting list is switched off",
    "not_full": "the requested dates still have space",
    "offers_not_held": "these options are not reserved",
    "not_agreed": "the customer has not agreed to these dates",
    "full": "no space", "closed": "closed that day", "past": "the date has passed",
    "notice": "too soon to book", "ahead": "too far ahead to book",
    "service_day": "the service is unavailable that day",
    "service_start": "the start time is outside the permitted times",
    "service_hours": "the time is outside the permitted hours",
    "service_day_full": "the daily limit has been reached",
    "min_nights": "the requested length is below the minimum",
    "checkin_time": "the arrival time is outside the permitted times",
    "checkout_time": "the departure time is outside the permitted times",
    "nothing_offered": "none of the requested options could be reserved",
    "not_theirs": "the booking belongs to someone else",
    "not_booked": "there is no confirmed booking to change",
    "already_happened": "the booking has already happened",
    "not_movable": "the booking cannot be moved",
    "ooh_needs_owner": "your approval is needed for that time",
}


def panel(calls) -> str:
    """The ⓘ's prose: every call this draft made, in order, one line each.
    `calls` is the (name, args, result) triples the loop saw. Empty string when
    the draft called nothing — a reply that needed no tools has no working to
    show, and an empty panel is honest where a heading over nothing is not."""
    out = []
    for c in calls or []:
        try:
            name, args, result = c
        except (TypeError, ValueError):
            continue
        line = outcome(name, args, result)
        if line:
            out.append(line)
    return "\n".join(out)


# Why an empty diary answer was empty (app/scheduling.why_none). "No space"
# is only the truth when every day asked about was open, in time, and full
# (Kris 2026-09-25: a same-day emergency at 17:58 read "no space" when it
# was the notice period).
_WHY_WORDS = {"closed": "closed that day", "over": "that day's hours are over",
              "notice": "too soon — inside the notice period", "full": "no space",
              "ahead": "too far ahead to book yet"}


def _why_none(result: dict) -> str:
    days = result.get("why_none_days")
    if isinstance(days, dict) and days:
        seen = []
        for d in sorted(days):
            w = _WHY_WORDS.get(str(days[d]), "no available times returned")
            if w not in seen:
                seen.append(w)
        return "; ".join(seen)
    return _WHY_WORDS.get(str(result.get("why_none") or ""), "no available times returned")


RECORD_FORMAT = "working-v1"


def retrieval_record(rows, query: str, enabled: bool) -> dict:
    state = getattr(rows, "outcome", "ok" if enabled and query.strip() else "not_run")
    return {"state": state, "query": getattr(rows, "query", query) if state != "not_run" else "",
            "matches": sum(not r.get("_context") for r in rows or []),
            "context": sum(bool(r.get("_context")) for r in rows or [])}


def knowledge_line(record: dict) -> str:
    state = record.get("state")
    if state == "failed":
        return "The knowledge search could not be completed."
    if state != "ok":
        return ""
    n, extra = record.get("matches", 0), record.get("context", 0)
    line = f"Knowledge search: {n} match{'' if n == 1 else 'es'} returned."
    if extra:
        line += f" Also retrieved {extra} other knowledge note{'' if extra == 1 else 's'}."
    return line


def record(calls, result, retrieval=None, *, draft=None, needs_owner=None, grounding_note="") -> str | None:
    """Persist owner prose and evidence together, without a schema change."""
    raw = (getattr(result, "reasoning", "") or "").strip()
    calls = calls or []
    lookup = retrieval or {}
    if not grounding_note and not calls and not raw and not retrieval and not getattr(result, "text", ""):
        return None
    looked = []
    if getattr(result, "model", ""):
        looked.append("The customer's message and your settings.")
    if lookup.get("state") in {"ok", "failed"}:
        looked.append("Searched your knowledge using the customer's subject and message.")
    if any(c[0].startswith("check_") and "availability" in c[0] for c in calls if len(c) == 3):
        looked.append("Checked your diary.")
    if grounding_note:
        looked.append("Date references in the conversation and booking record.")
    found = [knowledge_line(lookup)] if knowledge_line(lookup) else []
    checks = [c for c in calls if isinstance(c, (tuple, list)) and len(c) == 3
              and c[0] in {"check_availability", "check_appointment_availability", "check_stock"}]
    actions = [c for c in calls if c not in checks]
    found.extend(panel(checks).splitlines())
    reply = getattr(result, "text", "") if draft is None else draft
    decided = ("Prepared a reply." if reply.strip() else "No reply was prepared.")
    needs = []
    if getattr(result, "deferred", False) and not reply.strip():
        decided = "Left the reply unwritten because a business fact is missing."
        needs.append("Add the missing business information shown beside the message.")
    elif needs_owner or (needs_owner is None and getattr(result, "thread_state", "") == "needs_attention"):
        needs.append("Review this request before it goes any further.")
    prose = "Looked at\n" + ("\n".join(looked) or "No further lookups were recorded.")
    prose += "\n\nFound\n" + ("\n".join(found) or "No lookup results were recorded.")
    prose += "\n\nDecided\n" + "\n".join(filter(None, [panel(actions), decided]))
    if needs:
        prose += "\n\nNeeds you\n" + "\n".join(needs)
    return json.dumps({"format": RECORD_FORMAT, "owner": prose, "retrieval": lookup,
                       "technical": {"trail": raw, "calls": calls, "grounding": grounding_note}}, ensure_ascii=False, default=str)


def unpack(value) -> dict:
    try:
        data = json.loads(value or "")
        if isinstance(data, dict) and data.get("format") == RECORD_FORMAT:
            return data
    except (TypeError, ValueError):
        pass
    return {}


def owner_panel(value) -> str:
    data = unpack(value)
    if data:
        return data.get("owner") or ""
    return ("A plain-English account was not recorded for this earlier reply."
            if value else "")
