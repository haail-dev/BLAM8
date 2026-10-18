"""Customer Portal tool catalogue + executor (docs/modules/module-portal.md).

The customer-facing subset of the brain. Every guard lives HERE, server-side,
not in the prompt:

  * tenant_id is closed over from the URL slug — the model never supplies it.
  * WHO the customer is lives in the PortalSession: it is set only by a
    successful lookup_reference (the code is the capability) or find_contact
    (name+email — the spec's deliberate fallback), and every booking read/write
    is filtered to that contact. A session with no identity gets a polite
    refusal from every identity-gated tool no matter what the model asks for.
  * Writes are three: create_booking / move_booking (both through the same
    capacity-safe paths the email agent uses — advisory-locked RPCs, so racing
    customers can't overbook) and escalate_to_business (an inbox row the owner
    answers through the normal approval flow). No deletes, no sends.
  * execute() never raises — failures come back as data the model can relay.

Shape mirrors src/app/concierge_data.py so the two catalogues stay reviewable
side by side.
"""
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import crm, db, events, push, scheduling, services, slotgrid, trace
from app import record_versions
from app import knowledge as kb
from app.dates import pretty_date

log = logging.getLogger(__name__)

MAX_QUERY_CHARS = 300
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def mask_code(code: str | None) -> str:
    """TEST-KRH7 → TEST-KR**. The only form of a reference code that ever
    appears in chat (identity hotfix 2026-07-26): tool results carry the
    masked form alone, so the model cannot echo a full code it never saw —
    the full code travels by email only."""
    code = (code or "").strip()
    return code[:-2] + "**" if len(code) > 2 else code

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the business's own knowledge base — services, prices, "
            "policies, opening hours, FAQs. Use for every question about what "
            "the business does or charges. Pass the customer's question in "
            "their own words (a full sentence), not a two-word summary — short "
            "queries miss. Returns the most relevant entries; answer ONLY from "
            "what comes back."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "lookup_reference",
        "description": (
            "Verify the reference code the customer TYPED (from the "
            "business's emails, e.g. ACME-7K3M). On success the customer is "
            "identified for the rest of the conversation and their upcoming "
            "bookings are returned. Never repeat the code back — refer to it "
            "only in the masked form the result carries. If not found, ask "
            "them to double-check the code in their email, or offer the "
            "name+email route (find_contact)."),
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "find_contact",
        "description": (
            "For an existing customer without their code: takes the email "
            "address the business has for them (and their name, if given). "
            "The portal is invite-only — it never creates a customer. If the "
            "email is on file, the reference code is EMAILED to it: tell the "
            "customer to check their inbox and type the code here, then "
            "verify it with lookup_reference. An email not on file returns "
            "not_invited. The code is never shown in chat."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "list_my_bookings",
        "description": (
            "The identified customer's own upcoming bookings (date, window, "
            "reference, status). Only works after lookup_reference or "
            "find_contact has succeeded."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_dependant",
        "description": (
            (
                "Update (or add) one of the identified customer's own dependants — the things the business's "
                "bookings are about, shown in <customer_record> — with what the customer states here: a "
                "changed detail, a renewed expiry date, a new one they mention. Pass the name and the fields "
                "keyed EXACTLY as the record shows them. Only works after lookup_reference or find_contact has "
                "succeeded; a document (a certificate or form) cannot be attached here — ask them to email it."
            )),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "group": {"type": "string"},
                           "fields": {"type": "object"}},
            "required": ["name", "fields"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "Live diary availability between two ISO dates (inclusive, max 31 "
            "days). Returns bookable slots as {date, window, remaining} — "
            "window is 'day', 'am', 'pm' or a start time like '10:00'. Offer "
            "the customer 2-3 of these; never offer a slot this didn't "
            "return. A multi-day range also returns day_summary — per-day "
            "counts of free slots. For a period ask ('what's your "
            "availability next week?') answer with those counts ('Tuesday "
            "has 4 slots, Wednesday 5') and ask which day suits, then offer "
            "2-3 concrete slots on the day they pick."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO date"},
                "date_to": {"type": "string", "description": "ISO date"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            (
                "Book the identified customer into a slot they chose from check_availability. `summary` is "
                "what the appointment is for (use the requested service name). Returns the booking reference "
                "on success, or slot_full — then offer the alternatives."
            )),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_date": {"type": "string", "description": "ISO date"},
                "window": {"type": "string",
                           "description": "'day', 'am', 'pm' or 'HH:MM'"},
                "summary": {"type": "string"},
            },
            "required": ["booking_date", "window", "summary"],
        },
    },
    {
        # The customer's own log (src/app/customer_log.py, slice 5): a note on one of
        # THEIR OWN bookings — refused on a dependant's. Filtered out when the
        # business switches it off.
        "name": "add_note_to_booking",
        "description": (
            (
                "Add the identified customer's OWN note to one of their bookings — how the session went, what "
                "to work on, a question for next time. Call it AS SOON AS they ask to record something — do "
                "not ask them to confirm first. booking_ref from list_my_bookings (past bookings count). Only "
                "for a booking that is about the customer themselves: a booking about a dependant (a "
                "separately tracked subject) returns dependant_record — pass that note to the business with "
                "escalate_to_business instead, ONCE (never if an earlier turn already passed it on). Returns "
                "added:true."
            )),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_ref": {"type": "string", "description": "The booking reference, e.g. BK-1042"},
                "note": {"type": "string", "description": "The customer's note, in their words (max 600 chars)"},
            },
            "required": ["booking_ref", "note"],
        },
    },
    {
        # 5b nit: the customer may withdraw their OWN latest note on a booking.
        "name": "withdraw_my_note",
        "description": ("Withdraw the identified customer's OWN most recent note on one of their bookings "
                        "(they wrote it on the portal and want it gone). booking_ref from list_my_bookings. "
                        "Only their own notes — never the business's."),
        "input_schema": {"type": "object", "properties": {"booking_ref": {"type": "string"}},
                         "required": ["booking_ref"]},
    },
    {
        # The waiting list (src/app/courses.py, slice 3): offered only when the
        # business keeps one (tools_for filters it).
        "name": "join_waitlist",
        "description": (
            "Add the identified customer (or the named dependant) to the WAITING LIST for a "
            "class or start time that check_availability showed as full — only after they "
            "said yes. Returns their position; tell them it, and that a place is offered to "
            "the first waiting when one comes up. Never use this when the slot has space."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "The date they wanted, ISO (a weekly class: any date it runs)"},
                "window": {"type": "string", "description": "The start time they wanted, e.g. '09:30'"},
                "service": {"type": "string", "description": "The service / class name"},
                "details": {"type": "object", "description": "Optional note"},
            },
            "required": ["date", "window"],
        },
    },
    {
        "name": "move_booking",
        "description": (
            "Reschedule one of the identified customer's own bookings to a "
            "new slot they chose from check_availability. booking_id is the "
            "id from list_my_bookings, or the booking reference (like "
            "BK-1042) — prefer the reference. Depending on "
            "the business's settings this either moves the booking instantly "
            "(moved:true) or passes the request to the business to confirm "
            "(requested:true) — relay whichever the result says happened."),
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string"},
                "new_date": {"type": "string", "description": "ISO date"},
                "new_window": {"type": "string"},
            },
            "required": ["booking_id", "new_date", "new_window"],
        },
    },
    {
        "name": "list_my_invoices",
        "description": (
            "The identified customer's OWN invoices — ref, amount, status "
            "(paid / awaiting payment / overdue), due date, line items, and a "
            "pay_url when an invoice can be paid online. Use for 'do I owe "
            "anything?', 'can I pay my invoice?', 'when was my last payment?', "
            "'can I see my invoice?'. Read-only; needs the customer identified "
            "first."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_my_quotes",
        "description": (
            "The identified customer's OWN quotes — ref, what it's for, amount, "
            "status (awaiting your answer / accepted / declined / expired), "
            "validity date, and a view_url where they can read the full quote "
            "and accept it online. Use for 'what did you quote me?', 'can I "
            "see my quote?', 'how do I accept?'. Read-only; needs the customer "
            "identified first."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "escalate_to_business",
        "description": (
            "Pass the customer's question or request to the business — it "
            "lands in their inbox and they reply by email. Needs the customer "
            "identified first (find_contact) so the business can answer. "
            "`message` is the customer's request in their own words plus any "
            "details gathered."),
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}
# A stays diary's appointments (Kris 2026-09-13): two more diary tools, shaped
# by the flavoured defs below when the diary has appointment services.
TOOL_NAMES |= {"check_appointment_availability", "create_appointment"}

# Tools that only make sense with a live diary (scheduling module). Without it
# the assistant gathers details and escalates instead — the catalogue shrinks
# rather than refusing at run time (same pattern as concierge PRO_TOOLS).
_DIARY_TOOLS = frozenset({"check_availability", "create_booking", "move_booking",
                          "join_waitlist", "add_note_to_booking", "withdraw_my_note",
                          "check_appointment_availability", "create_appointment"})

NOT_IDENTIFIED = {
    "error": "not_identified",
    "message": ("The customer isn't identified yet — ask for their reference "
                "code, or their name and email, and call lookup_reference or "
                "find_contact first."),
}


def tools_for(scheduling_on: bool, session: "PortalSession | None" = None) -> list[dict]:
    """The tool catalogue for one portal session. With a diary active the three
    diary tools take the shape of the TENANT'S OWN diary flavour — the same
    detection the email pipeline runs (live e2e 2026-09-03: a kennels portal
    offered day/am/pm windows against a stay diary, so a half-day daycare visit
    was unbookable there while the email agent booked it to the minute)."""
    if not scheduling_on:
        return [t for t in TOOLS if t["name"] not in _DIARY_TOOLS]
    base = [t for t in TOOLS if t["name"] not in ("join_waitlist", "add_note_to_booking", "withdraw_my_note")]
    if session is None:
        return base
    # The waiting list (src/app/courses.py) rides only for a tenant that keeps one;
    # the customer's log (src/app/customer_log.py) unless the business switched it off.
    extra = [t for t in TOOLS if t["name"] == "join_waitlist"] if _waitlist_on(session) else []
    if getattr(session, "notes_on", True):
        extra += [t for t in TOOLS if t["name"] in ("add_note_to_booking", "withdraw_my_note")]
    if _makeups_on(session):
        from app import makeups as _mk
        base = _mk.with_makeups(base)
    flavour = _flavour(session)[0]
    if flavour == "windows":
        return base + extra
    defs = _flavoured_diary_defs(session)
    if _makeups_on(session):
        from app import makeups as _mk
        defs = _mk.with_makeups(defs)
    return ([t for t in base if t["name"] not in _DIARY_TOOLS] + defs + extra)


def _makeups_on(session: "PortalSession") -> bool:
    if getattr(session, "_makeups_on", None) is None:
        try:
            from app import makeups
            session._makeups_on = makeups.on(db.get_scheduling_config(session.tenant_id))
        except Exception:
            session._makeups_on = False
    return session._makeups_on


def _waitlist_on(session: "PortalSession") -> bool:
    if getattr(session, "_waitlist_on", None) is None:
        try:
            from app import courses
            session._waitlist_on = courses.waitlist_on(db.get_scheduling_config(session.tenant_id))
        except Exception:
            session._waitlist_on = False
    return session._waitlist_on


def _flavour(session: "PortalSession") -> tuple[str, list, list, list]:
    """('stay'|'service'|'windows', gate_fields, service_types, doc_labels) —
    resolved once per session, mirroring pipeline._scheduling_setup so the
    portal books exactly what the email agent books (stays win over services).
    Document-type requirements can't be attached in chat, so they are lifted
    out of the portal's hard intake gate (`gate_fields`) and returned as
    labels the customer is asked to email in instead."""
    if session.diary is None:
        from app import intake, services
        try:
            cfg_row = db.get_scheduling_config(session.tenant_id) or {}
        except Exception:
            # Fail-open to the windows toolset — the portal keeps answering
            # (degraded to day/window granularity) rather than erroring out.
            log.warning("portal: diary flavour lookup failed for %s",
                        session.tenant_id, exc_info=True)
            cfg_row = {}
        fields = intake.clean_fields(cfg_row.get("intake_fields"))
        svc_types = cfg_row.get("service_types") or []
        if intake.stay_mode(cfg_row):
            flavour = "stay"
        elif services.configured(svc_types):
            flavour = "service"
        else:
            flavour = "windows"
        session.diary = (flavour,
                         [f for f in fields if not f.get("doc")],
                         svc_types,
                         [f for f in fields if f.get("doc")])
    return session.diary


def _doc_labels(session: "PortalSession", service=None) -> list[str]:
    """The document requirements the customer must email in for THIS booking —
    scoped fields count only for their own services (Kris 2026-09-04)."""
    from app import intake
    out = []
    for f in _flavour(session)[3]:
        if isinstance(f, dict):
            if intake.applies(f, service):
                out.append(f["label"])
        elif f:                      # a bare label (older cached shape / tests) = unscoped
            out.append(str(f))
    return out


def _diary_executor(session: "PortalSession", *, expected_offer_id: str | None = None):
    """The email agent's own flavour-matched diary executor, bound to this
    session's tenant + identified customer. Built per call — its per-instance
    gates (holds-per-email) don't apply here; the portal's own daily booking
    budget is the guard on this surface."""
    flavour, gate_fields, svc_types, _docs = _flavour(session)
    return scheduling.make_executor(
        session.tenant_id,
        contact=(session.contact or {}).get("email"),
        source="portal", intake_fields=gate_fields,
        **({"expected_offer_id": expected_offer_id} if expected_offer_id else {}),
        stay=(flavour == "stay"),
        # A stays diary's appointment services ride too (Kris 2026-09-13): the
        # executor keeps the stay tools as they are and adds the appointment path.
        service_types=(svc_types if (flavour == "service" or (flavour == "stay" and svc_types)) else None),
        groups=_dep_groups(session))


def _dep_groups(session: "PortalSession") -> list[dict]:
    """The tenant's dependant groups, cached on the session."""
    from app import requirement_groups as rgroups
    if getattr(session, "_dep_groups", None) is None:
        try:
            session._dep_groups = rgroups.clean_groups(db.list_requirement_groups(session.tenant_id))
        except Exception:
            session._dep_groups = []
    return session._dep_groups


def _sched_tool(toolset: list[dict], name: str) -> dict:
    return next(t for t in toolset if t["name"] == name)


def _flavoured_diary_defs(session: "PortalSession") -> list[dict]:
    """The portal's diary tools with the tenant's exact `details` keys on the
    create_booking argument (Kris 2026-09-04). Until now the portal model only
    learned the labels from a refusal — every first create call missed."""
    from app import intake, requirement_groups as rgroups
    _flavour_, gate_fields, _svc_, _docs_ = _flavour(session)
    return rgroups.with_subjects(
        intake.with_detail_keys(_flavoured_diary_defs_base(session), gate_fields),
        _dep_groups(session))


def _flavoured_diary_defs_base(session: "PortalSession") -> list[dict]:
    """check_availability / create_booking shaped like the tenant's diary.
    The schemas are the email agent's own (scheduling.STAY_TOOLS /
    SERVICE_TOOLS) so both surfaces book identically; the descriptions are
    rewritten for the self-serve portal (booking is immediate, documents go
    by email). move_booking is OMITTED for stays — scheduling.move_booking is
    window-shaped, so a stay reschedule goes to the business via
    escalate_to_business instead of failing with invalid_window."""
    flavour, _gate, _svc, _doc_fields = _flavour(session)
    docs = _doc_labels(session)
    doc_line = (
        " Required DOCUMENTS (" + ", ".join(docs) + ") cannot be attached in "
        "this chat — book without them and ask the customer to email them to "
        "the business." if docs else "")
    if flavour == "stay":
        check = _sched_tool(scheduling.STAY_TOOLS, "check_availability")
        hold = _sched_tool(scheduling.STAY_TOOLS, "hold_booking")
        defs = [
            {"name": "check_availability", "strict": True,
             "description": (
                 (
                     "Check whether the business has a free space for a STAY between an arrival and a departure "
                     "time. Stays run from a drop-off to a collection and MAY SPAN SEVERAL DAYS — or be a SAME-DAY "
                     "visit (arrival and departure on one date), booked "
                     "exactly the same way. Call this BEFORE offering or agreeing any dates — never guess. Returns "
                     "whether the span fits and how many nights; when full it returns the nearest later start that "
                     "fits the same length, which you should offer."
                 )),
             "input_schema": check["input_schema"]},
            {"name": "create_booking",
             "description": (
                 "Book the identified customer's STAY from arrival to "
                 "departure — only after check_availability showed the span "
                 "fits. The booking is placed immediately and the reference "
                 "returned; never say a stay is booked unless this returned "
                 "booked:true. The business requires booking details: pass "
                 "everything the customer has given in `details`, keyed "
                 "EXACTLY as the business names them — a missing_info error "
                 "lists what to collect in chat first." + doc_line),
             "input_schema": hold["input_schema"]},
        ]
        # The diary's other use (Kris 2026-09-13): appointments a customer
        # comes to — a meet & greet before the first stay — in their own pools.
        from app import services as _services
        if _services.configured(_svc):
            a_check = _sched_tool(scheduling.APPOINTMENT_TOOLS, "check_appointment_availability")
            a_hold = _sched_tool(scheduling.APPOINTMENT_TOOLS, "hold_appointment")
            names = ", ".join(s["name"] for s in _services.clean_service_types(_svc))
            defs += [
                {"name": "check_appointment_availability", "strict": True,
                 "description": (
                     "Check start times for an APPOINTMENT the customer comes to — one of: "
                     + names + (
                         ". Never for a stay or a same-day visit (those use check_availability). Pass `service` and a "
                         "date range; you get the start times where that appointment's full length still fits."
                     )),
                 "input_schema": a_check["input_schema"]},
                {"name": "create_appointment",
                 "description": (
                     "Book the identified customer's APPOINTMENT (" + names + ") at a start "
                     "time check_appointment_availability returned — placed immediately, "
                     "reference returned; never say it is booked unless this returned "
                     "booked:true. Pass the booking details in `details`, keyed EXACTLY as "
                     "the business names them." + doc_line),
                 "input_schema": a_hold["input_schema"]},
            ]
        return defs
    check = _sched_tool(scheduling.SERVICE_TOOLS, "check_availability")
    hold = _sched_tool(scheduling.SERVICE_TOOLS, "hold_booking")
    move = _sched_tool(TOOLS, "move_booking")
    return [
        {"name": "check_availability", "strict": True,
         "description": (
             "Check the business's real diary for free start times for a "
             "SERVICE. Each service has its own fixed length, so pass the "
             "`service` (one of the services listed for this business) and "
             "you get only the start times where its full length fits. Call "
             "this BEFORE offering or agreeing any time — never guess. "
             "Returns bookable slots as {date, window, remaining}; window is "
             "a start time, or 'am' / 'pm' / 'day' for a service booked as an "
             "ARRIVAL WINDOW (tell the customer the morning or the afternoon, "
             "never a clock time). Any time not returned is not bookable."),
         "input_schema": check["input_schema"]},
        {"name": "create_booking",
         "description": (
             "Book the identified customer into a start time they chose from "
             "check_availability, with the `service` so the right length is "
             "booked. The booking is placed immediately and the reference "
             "returned; never say it is booked unless this returned "
             "booked:true. Pass any booking details the customer gave in "
             "`details`, keyed exactly as the business names them — a "
             "missing_info error lists what to collect first." + doc_line),
         "input_schema": hold["input_schema"]},
        move,
    ]


@dataclass
class PortalSession:
    """Server-side state for one chat request. `contact` is the verified
    customer row — set only by lookup_reference (a code the customer
    possesses; the portal is invite-only, so find_contact never sets it);
    the callables are bound in src/app/portal.py (budgets, minting,
    the verification email)."""
    tenant_id: str
    business_name: str
    scheduling_on: bool = False
    show_prices: bool = True
    contact: dict | None = None
    booking_allowed: object = None     # () -> bool; None = allow
    move_allowed: object = None        # () -> bool; None = allow
    ensure_reference: object = None    # (tenant_id, email, name) -> contact
    verify_allowed: object = None      # (email) -> bool; None = allow
    send_verification: object = None   # async (contact) -> bool; None = can't send
    # settings.portal_reschedule_mode: 'auto' applies the customer's move on
    # the spot (the portal's original promise); 'approve' turns it into a
    # needs-attention inbox request the business confirms first.
    reschedule_mode: str = "auto"
    # settings.portal_booking_confirmation (default ON): a self-serve booking
    # emails the customer a confirmation with their reference + portal link.
    confirmation_on: bool = True
    notes_on: bool = True              # settings.portal_customer_notes (slice 5): the customer's own log
    # settings.portal_max_active_bookings (0 = no limit): most upcoming
    # bookings one customer can hold through the portal at once.
    max_bookings: int = 0
    bookings_made: list = field(default_factory=list)
    escalations: list = field(default_factory=list)
    moves_made: list = field(default_factory=list)
    move_requests: list = field(default_factory=list)
    # Observability only: the opaque per-visitor handle (trace.session_ref of
    # the chat session token) every trace row of this turn carries.
    trace_session: str | None = None
    # Phase 3 party-awareness cache: (email, trusted job_party rows). Keyed by
    # email so an identity switch mid-chat can never reuse the old person's
    # memberships.
    party_cache: tuple | None = None
    # Diary-flavour cache (see _flavour): (flavour, gate_fields, service_types,
    # doc_labels), resolved on first diary-tool touch.
    diary: tuple | None = None
    # Emails this session itself identified — created as a new customer or
    # verified by code. A later find_contact for the same email stays
    # identified instead of re-triggering the code path (run #8: the switch
    # fired in the session that had created the contact seconds earlier).
    own_emails: set = field(default_factory=set)
    # On-file profile cache (see _profile_sections): (email, record, jobs,
    # record version, built at). Rebuilt when the record's version moves
    # (src/app/record_versions.py) or after PROFILE_TTL_S. Keyed by
    # email so an identity switch mid-chat never shows the old person's data.
    profile: tuple | None = None


def _trace(session: "PortalSession", trace_type: str, *, outcome: str = "ok",
           latency_ms=None, booking_id=None, email_log_id=None,
           detail: dict | None = None) -> None:
    """One portal trace row, pre-filled with the session's identifiers —
    tenant, visitor handle, and the identified customer's email (if any)."""
    trace.emit(session.tenant_id, trace_type, outcome=outcome,
               session_id=session.trace_session,
               contact=(session.contact or {}).get("email"),
               booking_id=booking_id, email_log_id=email_log_id,
               latency_ms=latency_ms, detail=detail)


def _s(args: dict, key: str, cap: int = MAX_QUERY_CHARS) -> str:
    return str(args.get(key) or "").strip()[:cap]


def _iso(args: dict, key: str) -> date | None:
    try:
        return date.fromisoformat(_s(args, key, 10))
    except ValueError:
        return None


PROFILE_TTL_S = 120.0   # a write from outside this process still shows within two minutes


def _profile_lines(session: PortalSession) -> list[str]:
    """Every on-file line for the identified customer, record facts first. Kept
    for callers that want one list; anything that speaks to the MODEL should use
    _profile_sections instead, because the two halves carry different rules."""
    record, jobs = _profile_sections(session)
    return record + jobs


def _profile_sections(session: PortalSession) -> tuple[list[str], list[str]]:
    """(record facts, earlier-job facts) for the identified customer.

    Record facts are about the CUSTOMER — phone plus the DEFINED custom fields,
    the same fields the email drafter's <customer_context> carries (Julius
    2026-09-03: the portal asked a verified customer their dogs' breeds with
    dog_names_breeds sitting in the CRM) — plus the open-promise lines. Job
    facts are what they gave for ONE earlier booking. They are kept apart
    because "never re-ask for this" is right for the first and wrong for the
    second: a boiler at one address is not this customer's boiler (live
    2026-09-04, a landlord's CP12 detail rode into a service at another
    property). Cached per identity; ([], []) anonymous or on any failure."""
    c = session.contact or {}
    email = (c.get("email") or "").lower()
    if not email:
        return [], []
    version = record_versions.get(session.tenant_id, c.get("id"))
    if session.profile and session.profile[0] == email:
        built_version = session.profile[3] if len(session.profile) > 3 else None
        built_at = session.profile[4] if len(session.profile) > 4 else 0.0
        if built_version == version and time.monotonic() - built_at < PROFILE_TTL_S:
            return session.profile[1], session.profile[2]
        # The record changed since this was built (a level moved on the CRM, a
        # field autofilled from an email — run #8: the chat answered "I don't
        # have that information" from a record cached before the level was
        # set) or the cache aged out: re-read the contact, then rebuild.
        try:
            fresh = db.crm_get_record("contact", c.get("id"), session.tenant_id)
            if fresh and (fresh.get("email") or "").lower() == email:
                session.contact = c = fresh
        except Exception:
            log.warning("portal: contact re-read failed (tenant=%s)", session.tenant_id,
                        exc_info=True)
    lines: list[str] = []
    jobs: list[str] = []
    try:
        if c.get("phone"):
            lines.append(f"Phone: {c['phone']}")
        by_key = {d["key"]: d
                  for d in db.crm_list_field_definitions(session.tenant_id)
                  if d.get("entity") == "contact"}
        for key, value in (c.get("custom_data") or {}).items():
            d = by_key.get(key)
            from app import crm_fields as _cfs
            if d and _cfs.is_special_category(d):
                continue    # health details stay out of the portal chat's AI context (M39)
            if d and value not in (None, ""):
                line = _ladder_line(d, value) or f"{d['label']}: {value}"
                # An expiring field says when it lapses — the stored date plus
                # its validity (run #8: a theory pass date is not its expiry).
                try:
                    from datetime import date as _date
                    from app import crm_fields as _cf
                    note = _cf.expiry_note(d, value, _date.today())
                    if note:
                        line += f" ({note})"
                except Exception:
                    pass
                lines.append(line)
        # Dependants on record (docs/modules/module-requirement-groups.md): one line per
        # instance, so the chat never re-asks Murphy's shoeing and books the
        # visit as being ABOUT him.
        try:
            from app import requirement_groups as rgroups
            known = rgroups.render_known(_dep_groups(session),
                                         db.list_requirement_instances(session.tenant_id, c.get("id")))
            if known:
                lines.extend(known.split("\n"))
        except Exception:
            pass
        # Packages (src/app/wallet.py): the customer's balance in their own words.
        try:
            from app import wallet as _wallet
            bal = _wallet.portal_block(session.tenant_id, c.get("id"))
            if bal:
                lines.extend(bal.split("\n"))
        except Exception:
            pass
        # Waiting lists (src/app/courses.py): where they stand, or the offered place.
        try:
            from app import courses as _courses
            lines.extend(_courses.lines_for_contact(session.tenant_id, c.get("email")))
        except Exception:
            pass
        # Make-ups (src/app/makeups.py): the missed classes that qualify.
        try:
            from app import makeups as _makeups
            if _makeups_on(session):
                blk = _makeups.context_block(session.tenant_id, c.get("email"),
                                             db.get_scheduling_config(session.tenant_id))
                if blk:
                    lines.extend(blk.split("\n"))
        except Exception:
            pass
        # Their own log (src/app/customer_log.py): the notes they wrote on their lessons.
        try:
            from app import customer_log as _clog
            for ln in _clog.lines(_clog.list_for_contact(session.tenant_id, c.get("email"))):
                lines.append("Your note " + ln)
        except Exception:
            pass
        # Facts given on an earlier booking (Kris 2026-09-04): the inscription
        # Kris sent by EMAIL lived on BK-1005's details, not the CRM record, so
        # the portal asked for it again. These ride in their OWN section, newest
        # booking first, one line per label: they belong to the job they were
        # given for, and the prompt says so. Own try: never costs the CRM lines.
        try:
            jobs.extend(_booking_fact_lines(session, email, have_labels={
                ln.split(":", 1)[0].strip().lower() for ln in lines}))
        except Exception:
            log.warning("portal: booking facts failed (tenant=%s)",
                        session.tenant_id, exc_info=True)
        # Open promises ride along (Julius 2026-09-03): "is there anything you
        # still need from me?" answers from the ledger, both directions. Last
        # in the list so a ledger hiccup never costs the fields above.
        if c.get("id"):
            for p in (db.crm_open_promises(session.tenant_id,
                                           contact_id=c["id"]) or [])[:6]:
                side = ("The business still owes them"
                        if p.get("direction") == "we_owe"
                        else "Still needed from this customer")
                due = f" ({p['due_hint']})" if p.get("due_hint") else ""
                lines.append(f"{side}: {p.get('description')}{due}")
    except Exception:
        log.warning("portal: on-file profile failed (tenant=%s)",
                    session.tenant_id, exc_info=True)
    session.profile = (email, lines, jobs, version, time.monotonic())
    return lines, jobs


def _ladder_line(definition: dict, value) -> str | None:
    """A contact ladder as a LEVEL line — "Level on the DVSA driver's record:
    Prompted — 3 of 6, next Independent" — the way the dependant path renders
    one. "DVSA driver's record: Prompted" read as a verb to the model (run #8:
    "your DVSA driver's record has been prompted"). None for any other field."""
    if not isinstance(definition.get("ladder"), dict):
        return None
    levels = [str(o) for o in (definition.get("options") or []) if str(o).strip()]
    if definition.get("field_type") != "dropdown" or len(levels) < 2:
        return None
    from app import ladders as _ladders
    i = _ladders.index_of({"options": levels}, value)
    if i is None:
        return None
    tail = f", next {levels[i + 1]}" if i + 1 < len(levels) else ", the top level"
    return f"Level on the {definition['label']}: {levels[i]} — {i + 1} of {len(levels)}{tail}"


# Keys the booking paths stamp for themselves — plumbing, not customer facts.
_INTERNAL_DETAIL_KEYS = {"portal", "customer name", "service", "resource type",
                         "type of visit"}          # intake._norm forms
# Identity details never travel between bookings: a booking made for someone
# else (a sister's consultation, BK-1008) must not hand its name, phone or
# email to the next one — those come from the customer's own record.
_IDENTITY_KEY = re.compile(r"^(?:(?:full|customer|your|contact)\s+)?name$|e-?mail|phone|mobile|^tel(?:ephone)?\b|contact number", re.I)
_BOOKING_FACTS_MAX = 8


def _booking_fact_lines(session: PortalSession, email: str, have_labels: set) -> list[str]:
    """'From their booking BK-1005 (Design consultation, 8 Sep): Inscription wording &
    lettering style: …' — one line per detail label across the customer's live
    bookings, newest first, skipping plumbing keys, labels already on the record,
    and blanks. Capped so a long history can't crowd the prompt."""
    from app import intake
    rows = db.bookings_for_contact(session.tenant_id, email) or []
    rows = sorted(rows, key=lambda b: str(b.get("booking_date") or ""), reverse=True)
    out: list[str] = []
    seen = set(have_labels or ())
    for b in rows:
        details = b.get("details") or {}
        if not isinstance(details, dict):
            continue
        ref = b.get("booking_ref") or "an earlier booking"
        what = str(details.get("Service") or b.get("summary") or "").strip()
        when = pretty_date(b.get("booking_date")) if b.get("booking_date") else ""
        tag = ", ".join(x for x in (what, when) if x)
        for label, value in details.items():
            key = intake._norm(label)
            if key in _INTERNAL_DETAIL_KEYS or key in seen or _IDENTITY_KEY.search(key):
                continue
            text = str(value or "").strip()
            if not text:
                continue
            seen.add(key)
            out.append(f"From their booking {ref}{' (' + tag + ')' if tag else ''}: "
                       f"{label}: {text}")
            if len(out) >= _BOOKING_FACTS_MAX:
                return out
    return out


def _contact_view(session: PortalSession) -> dict:
    """What the model (and the client's done-event) may see of the identified
    customer. The reference code appears MASKED only — the full code never
    enters the chat context, so it cannot be echoed (hotfix 2026-07-26)."""
    c = session.contact or {}
    view = {"name": c.get("name") or "",
            "email": c.get("email") or "",
            "reference_masked": mask_code(c.get("reference_code"))}
    on_file, job_facts = _profile_sections(session)
    if on_file:
        # Rides the identity tools' results so a MID-TURN verification still
        # hands the model the record (the pre-verified path gets it in the
        # prompt's <customer_record>).
        view["on_file"] = on_file
    if job_facts:
        # Deliberately a SEPARATE key: these describe one earlier job, not the
        # customer, and the key name has to say so — the model reads it raw.
        view["from_earlier_jobs"] = job_facts
    return view


def _booking_view(b: dict) -> dict:
    return {"booking_id": b.get("id"), "booking_ref": b.get("booking_ref"),
            "date": b.get("booking_date"), "window": b.get("window"),
            "status": b.get("status"), "summary": b.get("summary")}


def _memberships(session: PortalSession) -> list[dict]:
    """The verified email's TRUSTED job-party rows (module-job-parties.md
    Phase 3) — one read, cached per identity; [] for the overwhelming
    non-party visitor and on any failure (party-awareness is additive, never
    a portal outage). db.job_party_memberships already excludes 'extracted'
    rows: an unconfirmed claim grants no portal capability (D4)."""
    email = ((session.contact or {}).get("email") or "").lower()
    if not email:
        return []
    if session.party_cache is None or session.party_cache[0] != email:
        try:
            session.party_cache = (email, db.job_party_memberships(
                session.tenant_id, email))
        except Exception:
            session.party_cache = (email, [])
    return session.party_cache[1]


def _onsite_booking_ids(session: PortalSession) -> set:
    return {m.get("booking_id") for m in _memberships(session)
            if m.get("on_site") and m.get("booking_id")}


def _upcoming(session: PortalSession) -> list[dict]:
    email = (session.contact or {}).get("email")
    if not email:
        return []
    today = date.today().isoformat()
    rows = db.bookings_for_contact(session.tenant_id, email)
    out = [_booking_view(b) for b in rows
           if (b.get("booking_date") or "") >= today]
    # Phase 3: jobs where they are the trusted ON-SITE party — the tenant who
    # opens the door sees the visit their letting agent booked, labelled with
    # the operator's own word for their role.
    onsite = {m["booking_id"]: m for m in _memberships(session)
              if m.get("on_site") and m.get("booking_id")}
    extra_ids = [bid for bid in onsite if bid not in {b.get("id") for b in rows}]
    if extra_ids:
        try:
            jobs = db.bookings_by_ids(session.tenant_id, extra_ids)
        except Exception:
            jobs = []
        for b in jobs:
            if (b.get("booking_date") or "") >= today \
                    and b.get("status") in ("confirmed", "proposed"):
                v = _booking_view(b)
                v["your_role"] = onsite[b["id"]].get("label") or "site contact"
                out.append(v)
    return out[:10]


# ── executors ────────────────────────────────────────────────────────────────
async def _search_knowledge_base(session: PortalSession, args: dict) -> dict:
    query = _s(args, "query")
    if not query:
        return {"error": "empty_query"}
    t0 = time.monotonic()
    rows = await kb.retrieve(session.tenant_id, query, k=5)
    # NB: retrieve() returns [] both for a genuinely empty KB match and for a
    # retrieval failure after its 3 retries — the trace can only distinguish
    # them via the knowledge.py warnings, so 'empty' here means "no entries came
    # back", not "the KB has nothing".
    _trace(session, "portal_kb_retrieval", outcome="ok" if rows else "empty",
           latency_ms=(time.monotonic() - t0) * 1000,
           detail={"query": query, "results_returned": len(rows),
                   "sources": [r.get("source") for r in rows],
                   "retrieval_method": "retrieve"})
    if not rows:
        return {"entries": [],
                "note": ("Nothing found — say you don't have that information "
                         "and offer to pass the question to the business.")}
    return {"entries": [{"source": r.get("source"),
                         "content": (r.get("content") or "")[:800]} for r in rows]}


def _lookup_reference(session: PortalSession, args: dict) -> dict:
    code = _s(args, "code", 20).upper()
    from app import portal as _portal
    contact = _portal.contact_for_code(session.tenant_id, code)   # the miss budget (H3)
    if not contact:
        # Each failed attempt is one trace row — a brute-force/enumeration run
        # shows up as a stack of not_found rows on one session/tenant. The code
        # itself is stored masked only, same rule as the chat wire.
        _trace(session, "portal_identity_verify", outcome="not_found",
               detail={"verification_method": "reference_code",
                       "code_masked": mask_code(code)})
        return {"found": False,
                "message": ("That code doesn't match any account at "
                            f"{session.business_name} — ask them to "
                            "double-check it (it's in the email), request it "
                            "again, or take their name and email instead.")}
    session.contact = contact
    session.own_emails.add((contact.get("email") or "").lower())
    _trace(session, "portal_identity_verify", outcome="verified",
           detail={"verification_method": "reference_code",
                   "code_masked": mask_code(code)})
    return {"found": True, "customer": _contact_view(session),
            "upcoming_bookings": _upcoming(session)}


NOT_INVITED = {
    "error": "not_invited",
    "message": ("This portal is invite-only and that email isn't on the "
                "business's customer list, so nothing was created. Tell them "
                "the portal is for existing customers: they can email the "
                "business directly, and every reply they get carries their "
                "reference code and portal link."),
}


async def _find_contact(session: PortalSession, args: dict) -> dict:
    """The no-code route for a customer who has lost their reference code.

    Invite-only (Kris 2026-09-24): the portal serves people the business
    already knows, so this NEVER creates a contact. An email on file (a
    contact, or any bookings under it) gets the reference code EMAILED to
    it and the customer types it back (lookup_reference verifies it) — the
    identity hotfix of 2026-07-26 still holds: name+email are discoverable,
    so they never link straight to an account. Anything else is
    NOT_INVITED."""
    name, email = _s(args, "name", 80), _s(args, "email", 254).lower()
    if not _EMAIL_RE.match(email):
        return {"error": "invalid_details",
                "message": "Need a valid email address."}
    if (email in session.own_emails and session.contact
            and (session.contact.get("email") or "").lower() == email):
        _trace(session, "portal_identity_verify", outcome="already_identified",
               detail={"verification_method": "same_session"})
        return {"found": True, "new_customer": False, "customer": _contact_view(session),
                "upcoming_bookings": _upcoming(session),
                "message": "Already identified in this conversation — carry on."}
    existing = db.crm_contact_by_email(session.tenant_id, email)
    has_bookings = bool(db.bookings_for_contact(session.tenant_id, email))
    if not (existing or has_bookings):
        _trace(session, "portal_identity_verify", outcome="not_invited",
               detail={"verification_method": "email_code", "email": email,
                       "code_issued": False})
        return dict(NOT_INVITED)
    if session.verify_allowed and not session.verify_allowed(email):
        _trace(session, "portal_identity_verify", outcome="limit",
               detail={"verification_method": "email_code",
                       "email": email, "code_issued": False})
        return {"error": "verification_limit",
                "message": ("Too many code requests — tell them to try "
                            "again in an hour or contact the business "
                            "directly.")}
    contact = existing
    if session.ensure_reference:
        # Mints the reference code if the account predates codes (and
        # creates the contact row when only bookings exist for the email).
        contact = session.ensure_reference(session.tenant_id, email,
                                           name or (existing or {}).get("name"))
    sent = False
    if session.send_verification and (contact or {}).get("reference_code"):
        sent = await session.send_verification(contact)
    if not sent:
        _trace(session, "portal_identity_verify", outcome="send_failed",
               detail={"verification_method": "email_code",
                       "email": email, "code_issued": False})
        return {"error": "verification_unavailable",
                "message": ("Couldn't email the code just now — ask them "
                            "to contact the business directly, or use the "
                            "reference code from any earlier email from "
                            "the business.")}
    _trace(session, "portal_identity_verify", outcome="code_issued",
           detail={"verification_method": "email_code",
                   "email": email, "code_issued": True})
    return {"verification_sent": True, "email": email,
            "message": ("That email is on file, so the reference code has "
                        "been EMAILED to it. Tell the customer to check "
                        "their inbox and type the code here — NEVER state "
                        "the code yourself. When they type it, verify it "
                        "with lookup_reference.")}


def _list_my_bookings(session: PortalSession, args: dict) -> dict:
    if not session.contact:
        return NOT_IDENTIFIED
    return {"customer": _contact_view(session),
            "upcoming_bookings": _upcoming(session)}


def _check_appointment_availability(session: PortalSession, args: dict) -> dict:
    """A stays diary's appointments (Kris 2026-09-13): the email agent's own
    appointment check, in the appointment's pool."""
    if _flavour(session)[0] != "stay":
        return {"error": "unsupported", "message": "This diary has no appointments — use check_availability."}
    execute, _state = _diary_executor(session)
    return execute("check_appointment_availability", dict(args or {}))


def _check_availability(session: PortalSession, args: dict) -> dict:
    # Stay / service diaries: the email agent's own flavour-matched check —
    # arrival→departure spans (stays) or service start times — so the portal
    # sees exactly the availability the email agent sees.
    if _flavour(session)[0] != "windows":
        execute, _state = _diary_executor(session)
        return execute("check_availability", dict(args or {}))
    d1, d2 = _iso(args, "date_from"), _iso(args, "date_to")
    if not d1 or not d2 or d2 < d1:
        return {"error": "bad_dates", "message": "Pass ISO dates, from <= to."}
    d2 = min(d2, d1 + timedelta(days=31))
    slots = scheduling.check_availability(session.tenant_id, d1, d2)
    if not slots:
        return {"slots": [],
                "note": ("No availability in that range — offer to check "
                         "other dates, or to pass the request to the "
                         "business.")}
    out = {"slots": slots[:40]}
    # Period ask: per-day counts over the FULL result (not the 40-slot cap),
    # so "Tuesday has 4 slots, Wednesday 5" stays honest on a busy diary.
    if d2 > d1:
        out["day_summary"] = scheduling.day_summary(slots)
    return out


def _booking_block(session: PortalSession, args: dict, flavour: str) -> dict | None:
    """The cross-session create guards the per-turn executor can't provide
    (live e2e 2026-09-03: two chats booked the same daycare day twice —
    BK-1020/BK-1021). Returns a refusal dict, or None to proceed:
      - already_booked: the identified customer holds an ACTIVE upcoming
        booking overlapping the requested span (a stay's real grid span;
        date+window for slot diaries) — quote the ref, never book it twice;
      - booking_limit: settings.portal_max_active_bookings (0/absent = off)
        caps how many upcoming bookings one customer can self-serve hold.
    Best-effort: any failure in here ALLOWS the booking — a guard outage must
    never block a customer."""
    try:
        email = (session.contact or {}).get("email")
        if not email:
            return None
        today = date.today().isoformat()
        rows = [b for b in db.bookings_for_contact(session.tenant_id, email)
                if b.get("status") in ("proposed", "confirmed")
                and str(b.get("booking_date") or "") >= today]
        if session.max_bookings and len(rows) >= session.max_bookings:
            return {"error": "booking_limit",
                    "message": (f"This business allows at most "
                                f"{session.max_bookings} upcoming bookings per "
                                f"customer and they already have {len(rows)}. "
                                "Do not book — tell them, and offer to pass "
                                "the request to the business "
                                "(escalate_to_business).")}

        # What each existing booking is ABOUT (its dependants), so a booking for
        # a DIFFERENT one on the same morning is not a duplicate (live
        # 2026-09-04, hot tubs: the Granary's service was refused because the
        # Dairy's was that morning), and a refusal says what the diary holds.
        subj_names = _subject_names_by_booking(session, rows)
        asked = _requested_subject_names(args)

        def _already(b: dict) -> dict:
            ref = b.get("booking_ref") or "a booking"
            about = subj_names.get(b.get("id")) or []
            about_txt = (" It is for " + ", ".join(about) + " — nothing else.") if about else ""
            return {"error": "already_booked", "booking_ref": b.get("booking_ref"),
                    "about": about,
                    "message": (f"This customer ALREADY has {ref} covering that "
                                f"time — never book the same visit twice.{about_txt} "
                                "Tell them exactly that (quote the reference and "
                                "what it is for), and NEVER say you added or "
                                "changed anything. To put another one of their "
                                "dependants or a second service on that visit, "
                                "explain that adding to an existing booking is not supported here, "
                                "and offer to pass the request to the business (escalate_to_business).")}

        def _different_subject(b: dict) -> bool:
            have = {n.lower() for n in (subj_names.get(b.get("id")) or [])}
            return bool(asked and have and not (asked & have))

        if flavour == "stay":
            arr = datetime.fromisoformat(str(args.get("arrival") or "").strip()[:16])
            dep = datetime.fromisoformat(str(args.get("departure") or "").strip()[:16])
            lo, hi = slotgrid.datetime_to_abs(arr), slotgrid.datetime_to_abs(dep)
            for b in rows:
                span = slotgrid.stay_span(b)
                if span and lo < span[0] + span[1] and span[0] < hi:
                    return _already(b)
        else:
            # windows AND service diaries book a date + start window.
            day = _s(args, "booking_date", 10) or _s(args, "date", 10)
            window = _s(args, "window", 5)
            for b in rows:
                if str(b.get("booking_date")) == day and (
                        b.get("window") == window
                        or "day" in (window, b.get("window"))):
                    if _different_subject(b):
                        continue   # same morning, another dependant — a second visit is fine
                    return _already(b)
    except Exception:
        log.warning("portal duplicate guard failed (tenant=%s)",
                    session.tenant_id, exc_info=True)
    return None


def _subject_names_by_booking(session: "PortalSession", rows: list[dict]) -> dict[str, list[str]]:
    """{booking_id: [dependant names]} for the customer's live bookings —
    empty when the tenant has no dependants (or the read fails)."""
    ids = [b.get("id") for b in rows if b.get("id")]
    if not ids:
        return {}
    try:
        subs = db.subjects_for_bookings(session.tenant_id, ids)
    except Exception:
        return {}
    return {bid: [str(i.get("name") or "") for i in (insts or []) if i.get("name")]
            for bid, insts in (subs or {}).items()}


def _requested_subject_names(args: dict | None) -> set[str]:
    raw = (args or {}).get("subjects")
    if isinstance(raw, (dict, str)):
        raw = [raw]
    out = set()
    for item in raw if isinstance(raw, list) else []:
        name = item if isinstance(item, str) else (item or {}).get("name") if isinstance(item, dict) else None
        if name:
            out.add(" ".join(str(name).split()).lower())
    return out


async def _create_appointment(session: PortalSession, args: dict) -> dict:
    """A stays diary's appointment, booked like any portal booking but placed
    by hold_appointment in the appointment's own pool (Kris 2026-09-13)."""
    if _flavour(session)[0] != "stay":
        return {"error": "unsupported", "message": "This diary has no appointments — use create_booking."}
    return await _create_booking(session, args, tool="hold_appointment")


async def _create_booking(session: PortalSession, args: dict, tool: str = "hold_booking") -> dict:
    if not session.contact:
        return NOT_IDENTIFIED
    if session.booking_allowed and not session.booking_allowed():
        _trace(session, "portal_booking_create", outcome="limit",
               detail={"reason": "daily booking budget"})
        return {"error": "limit_reached",
                "message": ("Booking limit reached for today from this "
                            "device — ask them to contact the business "
                            "directly.")}
    email = session.contact.get("email")
    name = session.contact.get("name") or email
    summary = _s(args, "summary", 120) or "Portal booking"
    flavour, _gate, _svc, _doc_fields = _flavour(session)
    docs = _doc_labels(session, (args or {}).get("service"))
    # An appointment on a stays diary is service-shaped (date + start time).
    appointment = tool == "hold_appointment"
    blocked = _booking_block(session, args, "service" if appointment else flavour)
    if blocked:
        _trace(session, "portal_booking_create", outcome=blocked["error"],
               detail={"summary": summary,
                       "existing_ref": blocked.get("booking_ref")})
        return blocked
    if flavour != "windows":
        # Stay / service diaries: place the hold through the email agent's own
        # flavour-matched executor (same capacity math, same intake gate minus
        # document fields), then confirm it — the customer IS the approver.
        call = dict(args or {})
        details = dict(call.get("details") or {})
        details.setdefault("portal", True)
        details.setdefault("customer_name", name)
        call["details"] = details
        # The model often writes the customer into its own summary ("Site
        # survey — Kris Deane"); appending the name again read "— Kris Deane —
        # Kris Deane" on the diary and the calendar (live 2026-09-03).
        call["summary"] = (summary if name and name.lower() in (summary or "").lower()
                           else f"{summary} — {name}")
        execute, state = _diary_executor(session)
        out = execute(tool, call)
        if not (isinstance(out, dict) and out.get("held")):
            err = (out or {}).get("error") or "failed"
            _trace(session, "portal_booking_create", outcome=err,
                   detail={"summary": summary, "flavour": flavour})
            return out if isinstance(out, dict) else {"error": "failed"}
        row = state["bookings"][0]
        when = (f"{out.get('arrival')} → {out.get('departure')}"
                if flavour == "stay" and not appointment
                else f"{row.get('booking_date')} ({row.get('window') or ''})")
        result = await _finalise_portal_booking(session, row, summary, name,
                                                email, when)
        if docs:
            result["documents_by_email"] = (
                "Ask the customer to EMAIL the business their "
                + ", ".join(docs) + " to complete the booking record.")
        if out.get("quote"):
            result["quote"] = out["quote"]
        return result
    day, window = _iso(args, "booking_date"), _s(args, "window", 5)
    if not day or not window:
        return {"error": "bad_slot"}
    # Same two-step as the email agent's hold: the advisory-locked RPC decides
    # capacity (racing customers can't overbook), then the self-serve booking
    # confirms immediately — the customer IS the approver here.
    row, err = scheduling.place_hold(
        session.tenant_id, day, window, f"{summary} — {name}",
        details={"portal": True, "customer_name": name},
        contact=email, source="portal")
    if err:
        _trace(session, "portal_booking_create", outcome=err,
               detail={"booking_date": day.isoformat(), "window": window,
                       "summary": summary})
        return {"error": "slot_full" if err == "full" else err,
                "message": ("That slot was just taken — offer the other available times." if err == "full" else
                            "That slot isn't bookable — offer slots from "
                            "check_availability.")}
    return await _finalise_portal_booking(
        session, row, summary, name, email,
        f"{row.get('booking_date')} ({row.get('window')})")


async def choose_held_time(session: PortalSession, booking_id: str) -> dict:
    try:
        return await _choose_held_time(session, booking_id)
    except Exception:
        log.warning("portal held-time lookup failed", exc_info=True)
        return {"error": "verification_unavailable",
                "message": "I couldn't verify that time. Please contact the business."}


async def _choose_held_time(session: PortalSession, booking_id: str) -> dict:
    """Choose a stored offer through the email executor; never create a replacement."""
    if not session.contact or not session.contact.get("email"):
        return NOT_IDENTIFIED
    gone = {"error": "offer_gone", "message": "That slot has just gone. Please choose another time."}
    row = next((b for b in db.offered_bookings_for_contact(
        session.tenant_id, session.contact["email"]) if str(b["id"]) == booking_id), None)
    if not row:
        return gone
    from app.offer_deadlines import expiry
    if expiry(row) <= datetime.now(scheduling.DIARY_TZ):
        return gone
    details = dict(row.get("details") or {})
    args = {"date": str(row["booking_date"]), "window": row.get("window"),
            "summary": row.get("summary") or "Booking", "details": details,
            "service": details.get("Service")}
    flavour = _flavour(session)[0]
    tool = "hold_booking"
    if flavour == "stay":
        if services.find(_flavour(session)[2], details.get("Service")):
            tool = "hold_appointment"
        else:
            span = scheduling.slotgrid.stay_span(row)
            if not span:
                return {"error": "unsupported", "message": "Please contact the business to choose this time."}
            args.update(arrival=scheduling.slotgrid.abs_to_datetime(span[0]).isoformat(),
                        departure=scheduling.slotgrid.abs_to_datetime(span[0] + span[1]).isoformat())
    execute, state = _diary_executor(session, expected_offer_id=booking_id)
    try:
        out = execute(tool, args)
    except Exception as exc:
        from app.booking_errors import capacity_conflict
        if capacity_conflict(exc):
            return gone
        if "BLAM8_BOOKING_UNSUPPORTED" in str(exc):
            return {"error": "unsupported", "held": False,
                    "message": "The business needs to handle this booking. Please contact them."}
        log.warning("portal offer acceptance failed", exc_info=True)
        return {"error": "verification_unavailable", "message": "I couldn't verify that time. Please contact the business."}
    if out.get("error") in ("offer_gone", "full", "slot_full"):
        return gone
    if not out.get("held"):
        error = out.get("error")
        if error == "missing_info":
            message = "Please send the missing booking details to the business before choosing this time."
        elif error == "unsupported":
            message = "The business needs to handle this booking. Please contact them."
        elif error in ("could_not_verify", "verification_unavailable", "tool_failed", "lookup_failed"):
            message = "I couldn't verify that time. Please contact the business."
        else:
            message = "This booking does not meet the business's booking rules. Please contact them."
        return {**out, "message": message}
    accepted = state["bookings"][0]
    return await _finalise_portal_booking(session, accepted, args["summary"],
        session.contact.get("name") or session.contact["email"], session.contact["email"],
        f"{accepted['booking_date']} ({accepted.get('window') or ''})")


async def _finalise_portal_booking(session: PortalSession, row: dict,
                                   summary: str, name: str, email: str,
                                   when: str) -> dict:
    """The self-serve confirm tail every flavour shares: flip the hold to
    confirmed, push the calendar event, and fan out the visibility rows
    (agent log, CRM timeline, trace, owner push, live inbox)."""
    if row.get("status") != "confirmed":
        db.set_booking_status(row["id"], "confirmed", session.tenant_id)
        row["status"] = "confirmed"
    session.bookings_made.append(row["id"])
    try:   # a waiter who books the class leaves the list (src/app/courses.py)
        from app import courses as _courses
        if _waitlist_on(session):
            _courses.on_booked(session.tenant_id, email, [row])
    except Exception:
        log.warning("portal booking: waitlist hook failed", exc_info=True)
    try:
        from app import calendar_sync
        await calendar_sync.push_bookings(session.tenant_id, booking_id=row["id"])
    except Exception:
        log.warning("portal booking: calendar push failed", exc_info=True)
    # Operator-facing lines read words, never ISO (live 2026-09-03: the CRM
    # timeline and Agent Log said "for 2026-09-07 (09:00)").
    when_words = _pretty_when(when)
    db.log_agent_event(
        session.tenant_id, "booked",
        f"{name} booked {summary} for {when_words} through the customer portal",
        contact=email, booking_id=row["id"], booking_ref=row.get("booking_ref"),
        event_date=row.get("booking_date"))
    try:   # CRM timeline (2026-07-31): the self-serve booking joins the feed
        crm.note_contact_activity(
            session.tenant_id, session.contact, "calendar_event",
            source_id=str(row["id"]), subject=summary,
            summary=f"Booked via the portal for {when_words}"
                    + (f" — {row.get('booking_ref')}" if row.get("booking_ref") else ""))
    except Exception:
        log.warning("portal booking: CRM activity failed", exc_info=True)
    _trace(session, "portal_booking_create", booking_id=row["id"],
           detail={"booking_ref": row.get("booking_ref"),
                   "booking_date": row.get("booking_date"),
                   "when": when, "summary": summary})
    push.notify(
        session.tenant_id,
        title=f"New booking — {name}",
        body=f"{summary} — {when_words}",
        url=f"/app/diary?date={row.get('booking_date')}",
        tag=f"portal-booking-{row['id']}", kind="portal_booking_created",
        contact=email, contact_name=name)
    events.publish(session.tenant_id, {"type": "status_changed",
                                       "contact": email, "status": "booked"})
    if session.confirmation_on:
        await _send_booking_confirmation(session, row, summary, name, email, when)
    try:   # Action-side promise resolution (2026-09-03): the self-serve
           # booking may fulfil an open promise ("Kris requests a meet and
           # greet Wednesday") — tick the ledger now, not on a future email.
        from app import crm_intel
        await crm_intel.resolve_from_action(
            session.tenant_id, email,
            f"Booked and confirmed {row.get('booking_ref') or 'a booking'} "
            f"for {when} via the customer portal — {summary}",
            ref=("booking", row.get("id"), row.get("booking_ref")))
    except Exception:
        log.warning("portal booking: promise resolution failed", exc_info=True)
    return {"booked": True, "booking": _booking_view(row),
            "customer": _contact_view(session)}


_SERVICE_WHEN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*\((\d{1,2}:\d{2}|am|pm|day)\)$")
_WINDOW_WORDS = {"am": "in the morning", "pm": "in the afternoon", "day": "during the day"}


def _pretty_when(when: str) -> str:
    """'2026-09-21T07:30 → 2026-09-21T17:30' → 'Monday 21 September, 7:30am →
    Monday 21 September, 5:30pm'; the service-diary shape '2026-09-07 (09:00)'
    → 'Monday 7 September, 9:00am' (live 2026-09-03 the confirmation email
    carried the raw ISO date — the one thing an operator-facing date must never
    do, and a customer-facing one even less). Windows rows pass through
    untouched — their when is already words. Purely cosmetic; any parse
    failure keeps the raw."""
    def _one(s: str) -> str:
        try:
            d = datetime.fromisoformat(s.strip())
            t = d.strftime("%I:%M%p").lstrip("0").lower()
            return f"{d.strftime('%A %d %B').replace(' 0', ' ')}, {t}"
        except ValueError:
            return s.strip()
    m = _SERVICE_WHEN.match((when or "").strip())
    if m:
        if m.group(2) in _WINDOW_WORDS:
            # An arrival-window booking: the customer was told the half-day.
            try:
                d = datetime.fromisoformat(m.group(1))
                return f"{d.strftime('%A %d %B').replace(' 0', ' ')}, {_WINDOW_WORDS[m.group(2)]}"
            except ValueError:
                return when
        return _one(f"{m.group(1)}T{m.group(2)}")
    parts = when.split("→")
    return " → ".join(_one(p) for p in parts) if len(parts) == 2 else when


async def _send_booking_confirmation(session: PortalSession, row: dict,
                                     summary: str, name: str, email: str,
                                     when: str) -> None:
    """The transactional 'you're booked' email for a self-serve portal booking
    (Julius 2026-09-03 — the assistant PROMISED one before it existed).
    Template-built, no LLM; the footer carries their reference code + portal
    link (the ref-delivery path for customers born on the portal). Best-effort
    end to end: a mail hiccup never costs the booking."""
    try:
        from app import mailbox
        from app import portal as portal_mod   # lazy — portal imports this module
        ref = row.get("booking_ref") or "your booking"
        first = (name or "").split(" ")[0] or "there"
        body = (
            f"Hi {first},\n\n"
            f"You're booked in with {session.business_name} — here's your "
            "confirmation:\n\n"
            f"{summary}\n"
            f"{_pretty_when(when)}\n"
            f"Booking reference: {ref}\n\n"
            "If anything changes, just reply to this email or use your "
            f"booking page below.\n\n{session.business_name}"
        ) + portal_mod.email_footer(session.tenant_id, None, email)
        await mailbox.send_mail(session.tenant_id, email,
                                f"Booking confirmed — {ref}", body)
        db.log_agent_event(
            session.tenant_id, "booking_confirmation",
            f"Emailed {name} the confirmation for {ref} ({row.get('booking_date')})",
            contact=email, booking_id=row.get("id"), booking_ref=ref,
            event_date=row.get("booking_date"))
        try:
            crm.note_contact_activity(
                session.tenant_id, session.contact, "email",
                source_id=f"confirm-{row.get('id')}", subject=f"Booking confirmed — {ref}",
                summary="Sent the portal booking confirmation email")
        except Exception:
            log.warning("portal confirmation: CRM note failed", exc_info=True)
    except Exception:
        log.warning("portal confirmation email failed (tenant=%s, booking=%s)",
                    session.tenant_id, row.get("id"), exc_info=True)


async def _move_booking(session: PortalSession, args: dict) -> dict:
    if not session.contact:
        return NOT_IDENTIFIED
    if _flavour(session)[0] == "stay":
        # Stay diaries drop this tool from the catalogue; a stray call must
        # not hit the window-shaped move path (invalid_window every time).
        return {"error": "unsupported",
                "message": ("Stay bookings are rearranged by the business — "
                            "use escalate_to_business with the new dates; "
                            "the business confirms the change by email.")}
    booking_id = _s(args, "booking_id", 64)
    day, window = _iso(args, "new_date"), _s(args, "new_window", 5)
    if not booking_id or not day or not window:
        return {"error": "bad_slot"}
    # Postgres raises 22P02 on a non-uuid id — a ref like BK-1027 must never
    # reach the uuid column (live e2e 2026-07-31, second finding).
    booking = (db.get_booking(booking_id, session.tenant_id)
               if _UUID_RE.match(booking_id) else None)
    email = (session.contact.get("email") or "").lower()
    # Phase 3: the trusted ON-SITE party may move the visit they attend — and
    # ONLY on_site grants this. A payer or informed party doesn't get to move
    # an appointment at someone else's property; an extracted row grants
    # nothing (excluded upstream, D4).
    onsite_ids = _onsite_booking_ids(session)
    if not booking:
        # Models mangle long uuids when re-typing them (live e2e 2026-07-31:
        # one flipped hex block = not_found). The human-sized booking_ref is
        # the reliable key, so accept it too — resolved against the caller's
        # OWN bookings (plus their on-site jobs), which keeps the ownership
        # guarantee implicit.
        wanted = booking_id.upper()
        candidates = list(db.bookings_for_contact(session.tenant_id, email))
        if onsite_ids:
            try:
                candidates += db.bookings_by_ids(session.tenant_id,
                                                 list(onsite_ids))
            except Exception:
                pass
        for b in candidates:
            if b.get("id") and (b.get("booking_ref") or "").upper() == wanted:
                booking_id = b["id"]
                booking = db.get_booking(booking_id, session.tenant_id)
                break
    # Ownership check — the ONLY bookings a portal session may touch are the
    # identified contact's own, or jobs they are the trusted on-site party on.
    if not booking or ((booking.get("contact") or "").lower() != email
                       and booking.get("id") not in onsite_ids):
        _trace(session, "portal_booking_move", outcome="not_found",
               booking_id=booking_id or None)
        return {"error": "not_found",
                "message": "That booking isn't one of this customer's."}
    if booking.get("status") not in ("proposed", "confirmed"):
        _trace(session, "portal_booking_move", outcome="not_movable",
               booking_id=booking_id)
        return {"error": "not_movable",
                "message": "That booking can't be rescheduled any more."}
    # Reschedule budget (2026-07-31): mirrors the create_booking daily budget —
    # before this, moves had only the generic chat rate limit.
    if session.move_allowed and not session.move_allowed():
        _trace(session, "portal_booking_move", outcome="limit",
               booking_id=booking_id,
               detail={"reason": "daily reschedule budget"})
        return {"error": "limit_reached",
                "message": ("Reschedule limit reached for today from this "
                            "device — ask them to contact the business "
                            "directly.")}
    readiness, _ = scheduling.make_executor(session.tenant_id, source="portal")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        return blocked
    if session.reschedule_mode == "approve":
        return _request_reschedule(session, booking, day, window)
    row, err = scheduling.move_booking(session.tenant_id, booking_id,
                                       day.isoformat(), window)
    if err:
        _trace(session, "portal_booking_move", outcome=err, booking_id=booking_id,
               detail={"new_date": day.isoformat(), "new_window": window})
        return {"error": "slot_full" if err == "full" else err,
                "message": ("That slot was just taken — offer the other available times." if err == "full" else
                            "That slot isn't bookable — offer slots from "
                            "check_availability.")}
    try:
        from app import calendar_sync
        await calendar_sync.remove_pushed_event(session.tenant_id, booking_id)
        if row.get("status") == "confirmed":
            await calendar_sync.push_bookings(session.tenant_id, booking_id=booking_id)
    except Exception:
        log.warning("portal move: calendar refresh failed", exc_info=True)
    name = session.contact.get("name") or email

    def _when(day, window) -> str:
        # Read by a person (docs: UI date convention): "9th Oct 26 in the morning", never ISO.
        return " ".join(x for x in (pretty_date(day), scheduling.window_words(window)) if x)

    db.log_agent_event(
        session.tenant_id, "booking_moved",
        f"{name} rescheduled {booking.get('booking_ref') or 'their booking'} "
        f"to {_when(row.get('booking_date'), row.get('window'))} through the "
        "customer portal",
        contact=email, booking_id=booking_id, booking_ref=row.get("booking_ref"),
        event_date=row.get("booking_date"))
    try:   # CRM timeline (2026-07-31): each reschedule is its own feed row —
           # the slot suffix keys it, so moving twice never dedupes away.
        crm.note_contact_activity(
            session.tenant_id, session.contact, "calendar_event",
            source_id=f"{booking_id}:{row.get('booking_date')}:{row.get('window')}",
            subject=booking.get("summary"),
            summary=f"Rescheduled via the portal: {_when(booking.get('booking_date'), booking.get('window') or 'day')}"
                    f" → {_when(row.get('booking_date'), row.get('window'))}"
                    + (f" — {row.get('booking_ref')}" if row.get("booking_ref") else ""))
    except Exception:
        log.warning("portal move: CRM activity failed", exc_info=True)
    session.moves_made.append(booking_id)
    _trace(session, "portal_booking_move", booking_id=booking_id,
           detail={"booking_ref": row.get("booking_ref"),
                   "old_date": str(booking.get("booking_date")),
                   "old_window": booking.get("window"),
                   "new_date": row.get("booking_date"),
                   "new_window": row.get("window")})
    push.notify(
        session.tenant_id,
        title=f"Booking rescheduled — {name}",
        body=f"Now {_when(row.get('booking_date'), row.get('window'))}",
        url=f"/app/diary?date={row.get('booking_date')}",
        tag=f"portal-move-{booking_id}", kind="portal_booking_moved",
        contact=email, contact_name=name)
    # Live diary/inbox tabs repaint — create_booking always published; the
    # move path silently didn't (gap found 2026-07-31).
    events.publish(session.tenant_id, {"type": "status_changed",
                                       "contact": email, "status": "booked"})
    return {"moved": True, "booking": _booking_view(row)}


def _request_reschedule(session: PortalSession, booking: dict, day, window: str) -> dict:
    """Approve-first reschedules (settings.portal_reschedule_mode='approve'):
    the customer's move becomes a needs-attention inbox request — the same
    channel escalations use — and the diary row stays put until the business
    replies and moves it themselves (diary or concierge). The new slot is NOT
    held: the business may counter-offer, and a pending request must never
    block other customers from booking."""
    email = session.contact.get("email")
    name = session.contact.get("name") or email
    ref = booking.get("booking_ref") or "their booking"
    old = f"{booking.get('booking_date')} ({booking.get('window') or 'day'})"
    new = f"{day.isoformat()} ({window})"
    log_id, _created = db.insert_email_log(
        session.tenant_id, email,
        f"Reschedule request from {name}",
        (f"[Customer portal reschedule request — reply goes to {email}]\n\n"
         f"{name} asked to move {ref} from {old} to {new}.\n"
         f"Booking: {booking.get('summary') or '—'}\n\n"
         "Reply to confirm or offer other dates. The diary hasn't been "
         "changed — move the booking in your diary (or ask the concierge) "
         "once you're happy."),
        "", status="pending",
        source_message_id=f"portal:{uuid.uuid4()}",
        thread_state="needs_attention",
        request_summary=f"Reschedule request: {ref} → {new}",
        from_name=name, contact_name=session.contact.get("name"),
        category="needs_reply",
        category_reason="Portal reschedule request awaiting your decision")
    db.log_agent_event(
        session.tenant_id, "reschedule_requested",
        f"{name} asked to move {ref} from {old} to {new} through the "
        "customer portal — awaiting your decision",
        contact=email, email_log_id=log_id, booking_id=booking.get("id"),
        booking_ref=booking.get("booking_ref"), event_date=day.isoformat())
    try:
        crm.note_contact_activity(
            session.tenant_id, session.contact, "reschedule_request",
            source_id=log_id, subject=booking.get("summary"),
            summary=f"Asked on the portal to move {ref} from {old} to {new}")
    except Exception:
        log.warning("portal reschedule request: CRM activity failed", exc_info=True)
    push.notify(
        session.tenant_id,
        title=f"Reschedule request — {name}",
        body=f"{ref}: {old} → {new}",
        url="/app/conversations/view?contact=" + (email or ""),
        tag=f"portal-reschedule-{log_id}", kind="portal_reschedule_request",
        contact=email, contact_name=name)
    events.publish(session.tenant_id, {"type": "status_changed", "log_id": log_id,
                                       "contact": email, "status": "pending"})
    session.move_requests.append(log_id)
    _trace(session, "portal_booking_move", outcome="requested",
           booking_id=booking.get("id"), email_log_id=log_id,
           detail={"old_date": str(booking.get("booking_date")),
                   "old_window": booking.get("window"),
                   "new_date": day.isoformat(), "new_window": window})
    return {"requested": True, "booking": _booking_view(booking),
            "message": (f"Request sent — {session.business_name} will confirm "
                        f"by email to {email}. The current booking stands "
                        "until they do.")}


def _escalate_to_business(session: PortalSession, args: dict) -> dict:
    if not session.contact:
        return NOT_IDENTIFIED
    message = _s(args, "message", 2000)
    if not message:
        return {"error": "empty_message"}
    email = session.contact.get("email")
    name = session.contact.get("name") or email
    # The enquiry lands as a normal needs-attention conversation: the owner
    # replies through the standard approval flow (Redraft works on it too).
    log_id, _created = db.insert_email_log(
        session.tenant_id, email,
        f"Portal enquiry from {name}",
        f"[Customer portal enquiry — reply goes to {email}]\n\n{message}",
        "", status="pending",
        source_message_id=f"portal:{uuid.uuid4()}",
        thread_state="needs_attention",
        request_summary=f"Portal enquiry from {name}",
        from_name=name, contact_name=session.contact.get("name"),
        category="needs_reply",
        category_reason="Portal enquiry the assistant couldn't answer")
    db.log_agent_event(
        session.tenant_id, "escalated",
        f"Portal enquiry from {name} passed to you — needs your reply",
        contact=email, email_log_id=log_id)
    try:   # CRM timeline (2026-07-31): the enquiry joins the feed NOW, not
           # only after the owner replies (note_outbound_sent dedupes on it).
        row_full = db.get_email_log_entry(log_id, session.tenant_id)
        if row_full:
            crm.ingest_email_log_row(session.tenant_id, row_full)
    except Exception:
        log.warning("portal escalation: CRM ingest failed", exc_info=True)
    push.notify(
        session.tenant_id,
        title=f"Portal enquiry — {name}",
        body=(message[:120] + "…") if len(message) > 120 else message,
        url="/app/conversations/view?contact=" + (email or ""),
        tag=f"portal-enquiry-{log_id}", kind="portal_escalation",
        contact=email, contact_name=name)
    events.publish(session.tenant_id, {"type": "status_changed", "log_id": log_id,
                                       "contact": email, "status": "pending"})
    session.escalations.append(log_id)
    _trace(session, "portal_escalation", email_log_id=log_id,
           detail={"message": message[:300], "escalated_to": "inbox",
                   "category": "needs_reply"})
    return {"escalated": True,
            "message": ("Passed to the business — they'll reply to "
                        f"{email}.")}


def _invoice_status_phrase(r: dict, today: str) -> str:
    st = r.get("status") or "draft"
    if st in ("sent", "partially_paid") and (r.get("due_date") or "9999") < today:
        return "overdue"
    return {"sent": "awaiting payment", "partially_paid": "partially paid"}.get(st, st)


def _list_my_invoices(session: PortalSession, args: dict) -> dict:
    """The verified customer's invoices — filtered server-side by their OWN email
    (never model-filtered), pay_url attached only when the invoice is genuinely
    payable online. Drafts are the business's private state and never shown."""
    from app import payments
    if not session.contact:
        return NOT_IDENTIFIED
    email = session.contact.get("email") or ""
    rows = db.invoices_for_contact(session.tenant_id, email)
    today = date.today().isoformat()
    can_pay = payments.tenant_can_take_payments(session.tenant_id)
    out = []
    for r in rows:
        if r.get("status") in ("draft", "cancelled", "void"):
            continue
        lines = db.list_invoice_lines(r["id"], session.tenant_id)
        payable = r.get("status") in ("sent", "overdue", "partially_paid")
        out.append({
            "ref": r.get("invoice_ref"),
            "issue_date": r.get("issue_date"),
            "due_date": r.get("due_date"),
            "total": r.get("total"),
            "status": _invoice_status_phrase(r, today),
            "paid_on": (r.get("paid_at") or "")[:10] or None,
            "lines": [{"description": l.get("description"),
                       "amount": l.get("amount")} for l in lines[:10]],
            "pay_url": payments.payment_url(r["id"]) if payable else None,
            "pay_online": bool(can_pay and payable),
        })
        if len(out) >= 10:
            break
    _trace(session, "portal_tool", detail={"tool": "list_my_invoices",
                                           "count": len(out)})
    # Phase 3 duty guard: a verified party who does NOT hold 'pays' (the
    # on-site tenant, the informed agent) has an empty list by email match —
    # this note stops the model reasoning its way around that and discussing
    # someone else's money.
    if not out:
        ms = _memberships(session)
        if ms and not any(m.get("pays") for m in ms):
            return {"invoices": [], "count": 0, "total_outstanding": 0,
                    "party_note": (
                        "This person is a party on a job (e.g. the on-site "
                        "contact) but does NOT handle payment — invoices go "
                        "to another party. Never discuss the job's prices, "
                        "invoices or balances with them; money questions go "
                        "to the business.")}
    owed = round(sum(float(i["total"] or 0) for i in out
                     if i["status"] in ("awaiting payment", "overdue",
                                        "partially paid")), 2)
    # Tell the model the TRUTH about how paying works, so it can never promise
    # a route that doesn't exist (live e2e 2026-09-01: with no Stripe and no
    # bank details it told the customer the pay page had "bank details").
    from app import invoice_config
    s = invoice_config.settings_for(db.get_invoice_settings(session.tenant_id))
    if can_pay:
        payment_note = "Invoices with a pay_url can be paid online by card."
    elif s.get("bank_sort_code") and s.get("bank_account_number"):
        payment_note = ("Payment is by bank transfer — the pay_url page shows "
                        "the bank details and reference.")
    else:
        payment_note = ("No online payment or bank details are set up. To pay, "
                        "the customer should reply to the invoice email or "
                        "contact the business — do NOT promise payment details "
                        "at any link.")
    return {"invoices": out, "count": len(out), "total_outstanding": owed,
            "payment_note": payment_note}


def _quote_status_phrase(r: dict, today: str) -> str:
    st = r.get("status") or "sent"
    if st == "sent" and (r.get("valid_until") or "9999") < today:
        return "expired"
    return {"sent": "awaiting your answer", "invoiced": "accepted"}.get(st, st)


def _list_my_quotes(session: PortalSession, args: dict) -> dict:
    """The verified customer's quotes — filtered server-side by their OWN email
    (never model-filtered). view_url is the same /q capability link their email
    carried; drafts and cancelled quotes are the business's private state."""
    from app.quote_send import quote_url
    if not session.contact:
        return NOT_IDENTIFIED
    email = session.contact.get("email") or ""
    rows = db.quotes_for_contact(session.tenant_id, email)
    today = date.today().isoformat()
    out = []
    for r in rows:
        if r.get("status") in ("draft", "cancelled"):
            continue
        status = _quote_status_phrase(r, today)
        out.append({
            "ref": r.get("quote_ref"),
            "title": r.get("title"),
            "issue_date": r.get("issue_date"),
            "valid_until": r.get("valid_until"),
            "total": r.get("total"),
            "status": status,
            "accepted_on": (r.get("accepted_at") or "")[:10] or None,
            "view_url": quote_url(r["id"]),
            "can_accept_online": status == "awaiting your answer",
        })
        if len(out) >= 10:
            break
    _trace(session, "portal_tool", detail={"tool": "list_my_quotes",
                                           "count": len(out)})
    # Phase 3 duty guard, quotes edition: no 'approves' duty → the quote is
    # another party's to answer; don't let the model discuss its contents.
    if not out:
        ms = _memberships(session)
        if ms and not any(m.get("approves") for m in ms):
            return {"quotes": [], "count": 0, "awaiting_answer": 0,
                    "party_note": (
                        "This person is a party on a job but quotes go to "
                        "whoever approves them — another party. Never discuss "
                        "the job's pricing or quote contents with them; "
                        "questions about the quote go to the business.")}
    open_quotes = [q for q in out if q["status"] == "awaiting your answer"]
    return {"quotes": out, "count": len(out),
            "awaiting_answer": len(open_quotes)}


def _update_dependant(session: PortalSession, args: dict) -> dict:
    """The customer keeps their own dependants current (Phase 3). Source
    'customer': a `once` field already on record is kept (the business set
    it or read it first), `confirm`/`expires` take the customer's word."""
    if not session.contact:
        return NOT_IDENTIFIED
    from app import requirement_groups as rgroups
    groups = _dep_groups(session)
    if not groups:
        return {"error": "not_offered", "message": "This business doesn't keep dependant records."}
    subs, err = rgroups.subjects_from_args({"subjects": [args or {}]}, groups)
    if err or not subs:
        return {"error": "invalid", "message": err or "Give the name and at least one field."}
    s = subs[0]
    g = s["group"]
    cid = session.contact.get("id")
    existing = next((i for i in db.list_requirement_instances(session.tenant_id, cid)
                     if i.get("group_id") == g.get("id")
                     and str(i.get("name") or "").lower() == s["name"].lower()), None)
    data, meta, changed = rgroups.merge((existing or {}).get("data"), (existing or {}).get("field_meta"),
                                        s["data"], g, source="customer")
    if not existing and not data:
        return {"error": "invalid", "message": "Say at least one thing about them to add a record."}
    row = db.upsert_requirement_instance(session.tenant_id, g["id"], cid, (existing or {}).get("name") or s["name"],
                                         data, meta) if (changed or not existing) else existing
    if row and not existing:
        from app import dependants_extract as _dx
        _dx.absorb_new_chips(session.tenant_id, cid, g, row)
    session.profile = None   # the record changed: re-read on the next turn
    _trace(session, "portal_tool", detail={"tool": "update_dependant", "changed": changed,
                                           "new": existing is None})
    if not row:
        return {"error": "failed", "message": "Couldn't save that just now — ask them to email the business."}
    return {"saved": True, "name": row.get("name") or s["name"], "changed": changed,
            "new": existing is None, "record": rgroups.instance_line(g, {"name": row.get("name"), "data": data}),
            "still_needed": rgroups.missing_for(g, data)}


def _add_note_to_booking(session: PortalSession, args: dict) -> dict:
    """The customer's own log (src/app/customer_log.py): a note on one of THEIR
    bookings — never a dependant's."""
    from app import customer_log
    if not session.contact or not session.contact.get("email"):
        return NOT_IDENTIFIED
    if not getattr(session, "notes_on", True):
        return {"error": "notes_off", "message": "This business does not take customer notes here — "
                                                 "offer to pass it on (escalate_to_business)."}
    ref = " ".join(str((args or {}).get("booking_ref") or "").split()).upper()
    note = customer_log.clean_note((args or {}).get("note"))
    if not ref or not note:
        return {"error": "missing_info", "missing": [k for k, v in (("booking_ref", ref), ("note", note)) if not v],
                "message": "Pass the booking reference (see list_my_bookings) and the note."}
    email = session.contact.get("email")
    booking = next((b for b in db.bookings_for_contact(session.tenant_id, email)
                    if str(b.get("booking_ref") or "").upper() == ref), None)
    if not booking:
        _trace(session, "portal_tool", outcome="not_found", detail={"tool": "add_note_to_booking", "ref": ref})
        return {"error": "not_found",
                "message": f"{ref} is not one of this customer's bookings — check list_my_bookings."}
    out = customer_log.add_note(session.tenant_id, booking, note, author="customer",
                                contact_email=email, contact_name=session.contact.get("name"))
    _trace(session, "portal_note", outcome=out.get("error") or "ok",
           detail={"ref": ref, "chars": len(note)})
    if out.get("added"):
        out["message"] = f"Recorded on {ref}. Tell the customer it is on their record."
    return out


def _withdraw_my_note(session: PortalSession, args: dict) -> dict:
    from app import customer_log
    if not session.contact or not session.contact.get("email"):
        return NOT_IDENTIFIED
    if not getattr(session, "notes_on", True):
        return {"error": "notes_off", "message": "This business does not take customer notes here."}
    ref = " ".join(str((args or {}).get("booking_ref") or "").split()).upper()
    email = session.contact.get("email")
    booking = next((b for b in db.bookings_for_contact(session.tenant_id, email)
                    if str(b.get("booking_ref") or "").upper() == ref), None) if ref else None
    if not booking:
        return {"error": "not_found", "message": f"{ref or 'That'} is not one of this customer's bookings — check list_my_bookings."}
    out = customer_log.withdraw_last(session.tenant_id, booking, email)
    _trace(session, "portal_note", outcome=out.get("error") or "withdrawn", detail={"ref": ref})
    return out


def _join_waitlist(session: PortalSession, args: dict) -> dict:
    """The waiting list, through the email agent's own executor (one code path)."""
    if not session.contact or not session.contact.get("email"):
        return {"error": "not_identified",
                "message": "Identify the customer first (find_contact) — the list needs their email."}
    execute, _state = _diary_executor(session)
    out = execute("join_waitlist", args)
    _trace(session, "portal_waitlist_join", outcome=(out.get("error") or "ok") if isinstance(out, dict) else "failed",
           detail={"date": args.get("date"), "window": args.get("window"), "service": args.get("service")})
    return out if isinstance(out, dict) else {"error": "failed"}


_SYNC = {
    "lookup_reference": _lookup_reference,
    "join_waitlist": _join_waitlist,
    "add_note_to_booking": _add_note_to_booking,
    "withdraw_my_note": _withdraw_my_note,
    "update_dependant": _update_dependant,
    "list_my_bookings": _list_my_bookings,
    "check_availability": _check_availability,
    "check_appointment_availability": _check_appointment_availability,
    "escalate_to_business": _escalate_to_business,
    "list_my_invoices": _list_my_invoices,
    "list_my_quotes": _list_my_quotes,
}
_ASYNC = {
    "search_knowledge_base": _search_knowledge_base,
    "find_contact": _find_contact,
    "create_booking": _create_booking,
    "create_appointment": _create_appointment,
    "move_booking": _move_booking,
}


async def execute(session: PortalSession, name: str, args: dict) -> dict:
    """Run one tool call for a portal session. Never raises — any failure
    comes back as {'error', 'message'} the model can relay politely."""
    try:
        if name == "add_to_booking":
            return {"error": "unsupported",
                    "message": "Adding to an existing booking is not supported here. Nothing changed. "
                               "Offer to pass the request to the business (escalate_to_business)."}
        if name not in TOOL_NAMES:
            return {"error": "unknown_tool"}
        if name in _DIARY_TOOLS and not session.scheduling_on:
            return {"error": "no_diary",
                    "message": "This business doesn't take diary bookings "
                               "here — gather details and escalate instead."}
        if name in _ASYNC:
            return await _ASYNC[name](session, args or {})
        return _SYNC[name](session, args or {})
    except Exception as e:
        log.warning("portal tool %s failed (tenant=%s)", name, session.tenant_id,
                    exc_info=True)
        _trace(session, "portal_tool", outcome="error",
               detail={"tool": name, "exception": repr(e)[:300]})
        return {"error": "lookup_failed",
                "message": ("That didn't work just now — say you couldn't "
                            "check and offer to pass the question to the "
                            "business.")}
