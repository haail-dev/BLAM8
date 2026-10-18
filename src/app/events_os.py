"""Events OS — Phase A (docs/os/events.md §6.1): the self-filling capture hook.

An `event` IS a crm_deals row (1:1) enriched. This module owns the ONE piece of
net-new intelligence Phase A adds: when the CRM intel pass (src/app/crm_intel.py) reads
an inbound as a dated occasion (a wedding, an away-day, a function — a date plus
headcount/venue context), `on_deal_is_event` upserts the crm_events row for that
deal and populates event_date / event_type / headcount / timings from the email,
routes any guest + dietary info to crm_event_guests (NOT crm_contacts — §14), and
routes named suppliers/staff to crm_event_stakeholders (the Phase-B chase substrate).

It reuses the SHIPPED confidence gate (crm_fields.gate): high-confidence → applied,
below the floor → dropped — exactly "the CRM that fills itself in", event-flavoured.
It rides the SAME Haiku call deal/promise extraction already runs (no second LLM
round-trip); crm_intel only asks for the event branch, and only calls this hook, when
the tenant's `events` module is active — so non-events tenants are byte-identical and
wholly unaffected. Everything is best-effort behind the pipeline's try/except: the
event capture must never cost a tenant their draft.

Phase B (the multi-stakeholder chase engine), C (run-of-show assembly) and D
(integrations) are DEFERRED — this module only captures.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app import crm_fields, crm_intel, db, disclosure

log = logging.getLogger(__name__)

# The gate floor: below crm_fields.SUGGEST_AT an event/guest/stakeholder read is
# noise — skip it. At/above it we apply (Phase A applies rather than queues a chip:
# the event row IS the deal's shape, and the board simply reflects the latest read;
# the human edits it on the card, the same contract as a self-filled CRM field).
APPLY_AT = crm_fields.SUGGEST_AT

_EVENT_TYPES = {"wedding", "corporate", "conference", "private", "other"}
_RSVP_VALUES = {"unknown", "yes", "no", "maybe"}
_STAKEHOLDER_KINDS = {"supplier", "staff"}


# The event branch that rides crm_intel's Haiku call (docs/os/events.md §6.1),
# registered onto crm_intel's extension seam below — the intel schema + prompt gain
# this object ONLY for events-module tenants, so every other tenant's analyze() is
# byte-identical and wholly unaffected.
EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_event": {"type": "boolean"},
        "confidence": {"type": "number"},
        "event_date": {"type": "string"},
        "event_type": {"type": "string",
                       "enum": ["wedding", "corporate", "conference", "private", "other"]},
        "guest_count": {"type": "number"},
        "drink_licence": {"type": "string",
                          "enum": ["held", "applied", "not_required", ""]},
        "timings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "time": {"type": "string"}},
                "required": ["label", "time"], "additionalProperties": False,
            },
        },
        "guests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "party_size": {"type": "number"},
                    "rsvp": {"type": "string", "enum": ["unknown", "yes", "no", "maybe"]},
                    "dietary": {"type": "string"},
                    "dietary_tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "party_size", "rsvp", "dietary",
                             "dietary_tags", "confidence"],
                "additionalProperties": False,
            },
        },
        "stakeholders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["supplier", "staff"]},
                    "role": {"type": "string"},
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "outstanding": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "role", "name", "email", "outstanding", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_event", "confidence"],
    "additionalProperties": False,
}

EVENT_SYSTEM = (
    "\n4) event: this tenant runs one-off dated occasions (weddings, functions, "
    "corporate days). If THIS email is about such an event (it states or implies a "
    "dated occasion — a date plus headcount/venue context), set is_event=true and "
    "fill what's stated: event_date (YYYY-MM-DD if derivable), event_type, "
    "guest_count, drink_licence, and timings (arrival/speeches/dinner/carriages as "
    "{label,time}). Route any GUESTS mentioned (with party_size, rsvp, dietary + "
    "normalised dietary_tags like 'vegan'/'gluten_free'/'nut_allergy') into guests, "
    "and any named SUPPLIERS or assigned STAFF (caterer, bar, florist, AV, waiting "
    "staff — kind supplier|staff, a role, plus name/email and the outstanding item we "
    "await) into stakeholders. Guests are NOT contacts. confidence is your certainty "
    "it's an event. If it's plainly not an event, set is_event=false and leave the "
    "rest empty. Invent nothing — only what the email states."
)


def is_events_tenant(tenant_id: str, client=None) -> bool:
    """True only when the tenant's `events` module is active — the one gate that
    keeps every non-events tenant on the byte-identical, untouched intel path."""
    try:
        return db.module_active(tenant_id, "events", client=client)
    except Exception:
        return False


def _confident(value) -> bool:
    try:
        return float(value) >= APPLY_AT
    except (TypeError, ValueError):
        return False


def _clean_int(raw):
    ok, val = crm_fields.coerce_value("number", raw)
    if not ok or val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _clean_date(raw):
    ok, val = crm_fields.coerce_value("date", raw)
    return val if ok else None


def _clean_timings(raw) -> list[dict] | None:
    """Coerce the model's timings into the [{"label","time"}] run-of-show spine.
    Anything malformed is dropped — a run sheet with a wrong carriage time is a real
    liability (§14), so we never fabricate: only well-formed {label,time} pairs pass."""
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        time = str(item.get("time") or "").strip()
        if label and time:
            out.append({"label": label[:60], "time": time[:20]})
    return out or None


def on_deal_is_event(tenant_id: str, deal_id: str, event: dict, *,
                     email_log_id: str | None = None, is_events: bool | None = None,
                     client=None) -> dict | None:
    """The Phase-A magic: when an inbound's deal reads as an EVENT, create/update the
    crm_events row and route guests/stakeholders — the CRM's self-fill, event-flavoured.

    `event` is the intel call's event branch (see EVENT_SCHEMA above). Returns
    the crm_events row it upserted, or None when the read wasn't an event / not
    confident / the tenant isn't an events tenant. `is_events` lets the caller pass the
    already-computed module gate (crm_intel does — the check per email is otherwise a
    second module_active DB read); None → we check. Best-effort: never raises on the
    drafting hot path."""
    if not deal_id or not isinstance(event, dict):
        return None
    if not _confident(event.get("confidence")):
        return None
    if is_events is None:
        is_events = is_events_tenant(tenant_id, client=client)
    if not is_events:
        return None   # non-events tenant → do nothing (the guardrail)

    try:
        row = db.crm_event_for_deal(deal_id, tenant_id, client=client)
        event_date = _clean_date(event.get("event_date"))
        event_type = (event.get("event_type") or "").strip().lower() or None
        if event_type not in _EVENT_TYPES:
            event_type = None
        guest_count = _clean_int(event.get("guest_count"))

        # Build the full field dict ONCE — only the values the email actually supplied
        # (so the board query stays cheap and we never overwrite with blanks). The same
        # dict creates the row on the None branch and updates it on the existing branch
        # (no redundant create-then-restate-the-same-fields write).
        fields: dict = {}
        if event_date:
            fields["event_date"] = event_date
        if event_type:
            fields["event_type"] = event_type
        if guest_count is not None:
            fields["guest_count"] = guest_count
        timings = _clean_timings(event.get("timings"))
        if timings:
            fields["timings"] = timings
        licence = (event.get("drink_licence") or "").strip().lower() or None
        if licence in ("held", "applied", "not_required"):
            fields["drink_licence"] = licence

        if row is None:
            row = db.crm_create_event(
                tenant_id, deal_id=deal_id, event_date=fields.get("event_date"),
                event_type=fields.get("event_type"), guest_count=fields.get("guest_count"),
                client=client)
            # A race-lost create returns the EXISTING row (get-or-create): if it came
            # back already carrying our fields (e.g. the winner wrote them) the update
            # below is a harmless no-op; otherwise it merges the extras (timings/licence
            # the bare create args don't cover) onto whichever row we hold.
        if fields:
            row = db.crm_update_event(row["id"], tenant_id, fields, client=client) or row

        _apply_guests(tenant_id, row["id"], event.get("guests"),
                      email_log_id=email_log_id, client=client)
        _apply_stakeholders(tenant_id, row["id"], event.get("stakeholders"),
                            deal_id=deal_id, client=client)
        return row
    except Exception:
        log.warning("Events OS capture failed for deal %s", deal_id, exc_info=True)
        return None


def _apply_guests(tenant_id: str, event_id: str, guests, *, email_log_id, client):
    """Route guest + dietary info to crm_event_guests (NOT crm_contacts — §14),
    confidence-gated + deduped by name via db.crm_upsert_event_guest."""
    if not isinstance(guests, list):
        return
    for g in guests:
        if not isinstance(g, dict) or not _confident(g.get("confidence")):
            continue
        name = (g.get("name") or "").strip() or None
        dietary = (g.get("dietary") or "").strip() or None
        tags = g.get("dietary_tags")
        tags = [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
        # Skip an empty read (no name AND no dietary signal → nothing to record).
        if not name and not dietary and not tags:
            continue
        rsvp = (g.get("rsvp") or "unknown").strip().lower()
        if rsvp not in _RSVP_VALUES:
            rsvp = "unknown"
        # A legitimate 0 (a declined/placeholder party) stays 0 — `or 1` would
        # rewrite it to a phantom cover; only a missing/uncoercible value defaults to 1.
        party = _clean_int(g.get("party_size"))
        party = party if party is not None else 1
        try:
            db.crm_upsert_event_guest(
                tenant_id, event_id=event_id, name=name, party_size=party,
                rsvp=rsvp, dietary=dietary, dietary_tags=tags,
                source_email=email_log_id, client=client)
        except Exception:
            log.warning("Events OS guest upsert failed for event %s", event_id,
                        exc_info=True)


def _apply_stakeholders(tenant_id: str, event_id: str, stakeholders, *, deal_id, client):
    """Route named suppliers/staff to crm_event_stakeholders (the chase substrate) and,
    when the email states an OUTSTANDING item, link it to a `they_owe` crm_promise so the
    Phase-B chase engine can track + auto-resolve it (§6.2/§12.1). Phase A/B store + link;
    the actual CHASE email is a daily job — nothing is emailed here."""
    if not isinstance(stakeholders, list):
        return
    for s in stakeholders:
        if not isinstance(s, dict) or not _confident(s.get("confidence")):
            continue
        kind = (s.get("kind") or "").strip().lower()
        role = (s.get("role") or "").strip()
        if kind not in _STAKEHOLDER_KINDS or not role:
            continue
        outstanding = (s.get("outstanding") or "").strip() or None
        try:
            row = db.crm_upsert_event_stakeholder(
                tenant_id, event_id=event_id, kind=kind, role=role[:60],
                name=(s.get("name") or "").strip() or None,
                email=(s.get("email") or "").strip() or None,
                outstanding=outstanding,
                client=client)
        except Exception:
            log.warning("Events OS stakeholder upsert failed for event %s", event_id,
                        exc_info=True)
            continue
        # Link the outstanding item to a they_owe promise (get-or-create, deduped) so the
        # chase engine tracks ONE promise per item and auto-resolution flips it confirmed.
        if outstanding and deal_id and (row or {}).get("id"):
            link_stakeholder_outstanding(tenant_id, stakeholder=row, deal_id=deal_id,
                                         outstanding=outstanding, client=client)


def link_stakeholder_outstanding(tenant_id: str, *, stakeholder: dict, deal_id: str,
                                 outstanding: str, client=None) -> str | None:
    """Ensure a `they_owe` promise exists for this stakeholder's outstanding item and
    link it (crm_event_stakeholders.outstanding_promise_id). A short, house-voiced
    description ("the bar owes us the drink-licence confirmation") so the chase + the
    board read naturally. Deduped by db.crm_link_stakeholder_promise — a restated item
    stays ONE promise. Best-effort: a link failure never breaks capture."""
    outstanding = (outstanding or "").strip()
    if not outstanding or not deal_id or not (stakeholder or {}).get("id"):
        return None
    who = (stakeholder.get("role") or stakeholder.get("kind") or "supplier").replace("_", " ")
    desc = f"the {who} owes us {outstanding}"
    try:
        pid = db.crm_link_stakeholder_promise(
            tenant_id, stakeholder_id=stakeholder["id"], deal_id=deal_id,
            description=desc, client=client)
        return pid
    except Exception:
        log.warning("Events OS promise-link failed for stakeholder %s",
                    stakeholder.get("id"), exc_info=True)
        return None


# ── The crm_intel extension seam (docs/crm-vs-os-architecture.md §2.3) ─────────
# R2: an OS depends on the CRM engine, never the reverse — so crm_intel never
# imports this module; we register the event branch onto its intel call here, at
# import time (src/app/main.py reaches this via the events router → events_routes).
# The adapters resolve is_events_tenant / on_deal_is_event / on_promise_resolved
# through the module namespace at CALL time, so tests monkeypatching events_os.*
# flow through the seam.

def _intel_wants(tenant_id: str) -> bool:
    """The seam's per-tenant gate: ride the intel call only for events tenants."""
    return is_events_tenant(tenant_id)


def _intel_apply_deal(tenant_id: str, deal_id: str, branch: dict, *,
                      email_log_id: str | None = None) -> dict | None:
    # is_events=True: the seam fires this hook only when wants() said yes, so the
    # per-email module_active re-check is skipped (the same one-read optimisation
    # crm_intel made when it called on_deal_is_event directly).
    return on_deal_is_event(tenant_id, deal_id, branch,
                            email_log_id=email_log_id, is_events=True)


def _intel_promise_resolved(tenant_id: str, promise_id: str) -> None:
    on_promise_resolved(tenant_id, promise_id)


crm_intel.register_intel_extension(
    "event", EVENT_SCHEMA, EVENT_SYSTEM, _intel_wants,
    apply_deal_hook=_intel_apply_deal,
    promise_resolved_hook=_intel_promise_resolved)


# ── Phase B: auto-resolve → stakeholder confirmed (§12.4, the zero-touch magic) ─
def on_promise_resolved(tenant_id: str, promise_id: str, *, is_events: bool | None = None,
                        client=None) -> int:
    """When a `they_owe` promise AUTO-RESOLVES (a supplier replies "confirmed, we've got
    the 118 incl. 6 vegan"), flip every stakeholder whose outstanding item IS that
    promise to confirmation='confirmed' — no click (§12.4). The promise engine already
    did the resolving; this only mirrors it onto the stakeholder's board light.

    Called from crm_intel's resolution path (best-effort, gated). Returns the number of
    stakeholders flipped. A NEVER-raises contract: it must not break promise resolution.
    Gated to events tenants — for everyone else there are no stakeholder rows, so even
    without the gate it would no-op, but the gate spares the query."""
    if not promise_id:
        return 0
    try:
        if is_events is None:
            is_events = is_events_tenant(tenant_id, client=client)
        if not is_events:
            return 0
        rows = db.crm_stakeholders_for_promise(promise_id, tenant_id, client=client)
        flipped = 0
        for s in rows:
            # Don't override a manual decline; a muted stakeholder still confirms (the
            # supplier followed through — mute only silenced the chase, not the truth).
            if s.get("confirmation") == "declined":
                continue
            db.crm_update_event_stakeholder(
                s["id"], tenant_id, {"confirmation": "confirmed"}, client=client)
            flipped += 1
        return flipped
    except Exception:
        log.warning("Events OS auto-confirm on promise %s failed", promise_id,
                    exc_info=True)
        return 0


# ── Phase B: the escalating chase (docs/os/events.md §6.2, §12.3) ─────────────
# The gone-quiet cadence generalised to N parties per event. chase_count indexes the
# escalation level: 0 chases sent → a gentle first nudge; after 1 (≈`step_days` later)
# → the "we go to print Friday" push; at MAX_CHASES the item stops + surfaces to a human.
MAX_CHASES = 3
# Days of quiet before the NEXT chase, indexed by chase_count (0→first at 4d quiet since
# link/last-touch, 1→second at 8d). The last value is reused if chase_count runs past it.
CHASE_STEP_DAYS = (4, 8)

_CHASE_PURPOSES = (
    # level 0 — the gentle first nudge
    "A short, warm first nudge to a supplier or staff member helping run an upcoming "
    "event, gently asking them to confirm the ONE outstanding item we're waiting on. "
    "Friendly, no pressure, one clear ask. Do NOT invent details or a deadline that "
    "wasn't given.",
    # level 1 — the deadline push ("we go to print Friday")
    "A polite but firmer follow-up to a supplier/staff member who hasn't yet confirmed "
    "the outstanding item — make clear we're assembling the run sheet and need their "
    "confirmation soon, so nothing is missed on the day. One clear ask, still courteous. "
    "Only reference a deadline if one is supplied in the facts; never invent a date.",
    # level 2 — final attempt before flagging to the manager
    "A final, courteous chase for the outstanding item before we flag it for the venue "
    "manager to handle personally. Make the ask unmistakable and explain we need to lock "
    "the run sheet. Stay professional and brief. Never invent details or dates.",
)


def chase_purpose(chase_count: int) -> str:
    """The escalation brief for the NEXT chase, indexed by how many have already gone
    (clamped to the last level). chase_count 0 → gentle, 1 → deadline push, 2 → final."""
    cc = chase_count if isinstance(chase_count, int) and chase_count >= 0 else 0
    return _CHASE_PURPOSES[min(cc, len(_CHASE_PURPOSES) - 1)]


def chase_facts(stakeholder: dict, event: dict, deal: dict) -> str:
    """The facts block the drafter composes the chase from — event name + date, the
    outstanding item, and a per-event reference the reply can quote so it resolves the
    RIGHT event's promise (§14: a supplier works across events; the terse "confirmed"
    must land on one). Stated facts only — the drafter is told to invent nothing."""
    title = (deal.get("title") or "the event").strip()
    ed = str(event.get("event_date") or "").strip()
    who = (stakeholder.get("role") or stakeholder.get("kind") or "supplier").replace("_", " ")
    outstanding = (stakeholder.get("outstanding") or "the outstanding item").strip()
    # A stable, human-quotable per-event reference (like booking_ref) so a reply threads
    # back to this event — the first 8 chars of the event id, upper-cased.
    ref = f"EVT-{str(event.get('id') or '')[:8].upper()}"
    lines = [
        f"Event: {title}" + (f" on {ed}" if ed else ""),
        f"Reference (quote this in any reply): {ref}",
        f"Who we're chasing: the {who}"
        + (f" ({stakeholder.get('name')})" if stakeholder.get("name") else ""),
        f"Outstanding item we need confirmed: {outstanding}",
    ]
    gc = event.get("guest_count")
    if gc is not None:
        lines.append(f"Current headcount: {gc}")
    return "\n".join(lines)


def chase_subject(stakeholder: dict, event: dict, deal: dict) -> str:
    """A stable subject carrying the per-event reference so replies thread + resolve the
    right event (§14)."""
    title = (deal.get("title") or "your event").strip()
    ref = f"EVT-{str(event.get('id') or '')[:8].upper()}"
    return f"{title} — quick confirmation needed [{ref}]"


async def compose_chase(tenant_id: str, cfg: dict, stakeholder: dict, event: dict,
                        deal: dict, *, now, auto: bool = False) -> str:
    """Compose + queue (and maybe send) ONE stakeholder chase — the shared path the
    daily job and the chase-now route both call, modelled verbatim on
    send_service_reminders._remind_one:

        compose_outbound(cfg, CHASE_PURPOSE[level], facts) → insert_email_log(
        kind="event_chase") → stamp last_chased_at+chase_count → log_agent_event →
        usage.record_anthropic → auto-send-or-leave-pending.

    Returns 'sent' | 'drafted'. GUARDRAIL (§14): DRAFT-FIRST by default even for
    cfg.auto_send tenants — over-chasing a supplier you have a relationship with is worse
    than under-chasing. `auto` is decided by the caller from the dedicated opt-in flag
    (cfg.auto_chase_events), NOT cfg.auto_send, and defaults False."""
    # Local imports keep events_os light and avoid an import cycle with agent/mailbox.
    from app import agent, mailbox, usage

    to = (stakeholder.get("email") or "").strip()
    cc = stakeholder.get("chase_count")
    cc = cc if cc is not None else 0
    facts = chase_facts(stakeholder, event, deal)
    subject = chase_subject(stakeholder, event, deal)
    result = await agent.compose_outbound(cfg, chase_purpose(cc), facts,
                                          tenant_id=tenant_id, log_operation="event_chase")
    log_id, _ = db.insert_email_log(
        tenant_id, to or None, subject, f"[event chase] {facts}", result.text,
        status="pending", kind="event_chase",
        thread_id=f"event:{event.get('id')}")
    # Stamp BEFORE the send (dedupe + back off even if the send fails below) — the
    # send_service_reminders discipline: the mark lands only after the row is created.
    db.crm_stamp_stakeholder_chased(stakeholder["id"], tenant_id, now=now)
    db.log_agent_event(
        tenant_id, "event_chase",
        f"Chased the {(stakeholder.get('role') or stakeholder.get('kind') or 'supplier')}"
        f" on {(deal.get('title') or 'the event')} for {stakeholder.get('outstanding') or 'the outstanding item'}"
        f" (chase {cc + 1})",
        direction="outbound", contact=to or None, subject=subject, email_log_id=log_id,
        detail={"event_id": event.get("id"), "stakeholder_id": stakeholder["id"],
                "chase_level": cc})
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="event_chase", email_log_id=log_id)
    if auto and to:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the event chase to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


# ── The daily-rhythm board (docs/os/events.md §2) ─────────────────────────────
# THIS WEEK's events as live rollup cards + a NEXT 4 WEEKS summary. Composed
# server-side from crm_events + children (no data entry). The three status lights
# per supplier/staff (✅ confirmed / ⚠ needs-something / ⏳ chasing) are the chase
# engine's work made visible; Phase A renders them from the stored confirmation
# state (Phase B drives the chasing that flips them).

_CONFIRM_TONE = {"confirmed": "good", "needs_info": "warn", "pending": "chase",
                 "declined": "muted", "muted": "muted"}


def _card(tenant_id: str, event: dict, *, client=None) -> dict:
    """One board card: the event's date/timings/value/headcount + its child rollups
    (dietary matrix, supplier/staff status lights). Pure-ish (one child read each)."""
    deal = event.get("crm_deals") or {}
    guests = db.crm_list_event_guests(tenant_id, event["id"], client=client)
    stakeholders = db.crm_list_event_stakeholders(tenant_id, event["id"], client=client)
    matrix = db.crm_event_dietary_matrix(tenant_id, event["id"], client=client)
    lights = [_stakeholder_light(s) for s in stakeholders]
    # The run-sheet summary line (§2): "run sheet ● Draft ready — N gaps". Reads straight off
    # the STORED assembled sheet (the daily job / a manual assemble writes it) — no re-assembly
    # on the board render (that would fan out a query per card). None until first assembly.
    ros = event.get("run_of_show")
    ros = ros if isinstance(ros, dict) else {}
    ros_assembled = bool(ros.get("assembled_at"))
    ros_gaps = len(ros.get("gaps") or [])
    return {
        "id": event["id"],
        "deal_id": event.get("deal_id"),
        "title": deal.get("title") or "Event",
        "company": (deal.get("crm_companies") or {}).get("name"),
        "event_date": event.get("event_date"),
        "event_type": event.get("event_type"),
        "guest_count": event.get("guest_count"),
        "guest_count_final": event.get("guest_count_final"),
        "value": deal.get("value_estimate"),
        "stage": (deal.get("crm_stages") or {}).get("name"),
        "timings": event.get("timings") or [],
        "dietary_matrix": matrix,
        "lights": lights,
        "suppliers_confirmed": sum(1 for s in stakeholders
                                   if s.get("kind") == "supplier"
                                   and s.get("confirmation") == "confirmed"),
        "suppliers_total": sum(1 for s in stakeholders if s.get("kind") == "supplier"),
        "staff_confirmed": sum(1 for s in stakeholders
                               if s.get("kind") == "staff"
                               and s.get("confirmation") == "confirmed"),
        "staff_total": sum(1 for s in stakeholders if s.get("kind") == "staff"),
        "guests_recorded": len(guests),
        # The chase engine's live work, made visible on the card (§2):
        "chasing": sum(1 for s in stakeholders if _is_chasing(s)),
        "needs_human": sum(1 for s in stakeholders if _needs_human(s)),
        # The run-sheet line (§2): assembled?, and how many gaps still flagged.
        "run_sheet_assembled": ros_assembled,
        "run_sheet_gaps": ros_gaps,
    }


def _is_chasing(s: dict) -> bool:
    """A stakeholder actively being chased: pending, not yet exhausted, at least one
    chase gone (or awaiting the first)."""
    if s.get("confirmation") != "pending":
        return False
    cc = s.get("chase_count")
    return (cc if cc is not None else 0) < MAX_CHASES


def _needs_human(s: dict) -> bool:
    """The exhaustion contract (§6.2): a pending item chased MAX_CHASES times with no
    reply stops chasing and surfaces to the manager as 'needs a human'."""
    if s.get("confirmation") != "pending":
        return False
    cc = s.get("chase_count")
    return (cc if cc is not None else 0) >= MAX_CHASES


def _stakeholder_light(s: dict) -> dict:
    """One board status light for a stakeholder, reflecting the CHASE state — not just
    the raw confirmation. A confirmed item is green; a muted one is muted; an exhausted
    pending item is 'needs a human'; anything else pending shows ⏳ chasing (N)."""
    conf = s.get("confirmation")
    cc = s.get("chase_count")
    cc = cc if cc is not None else 0
    if conf == "pending" and _needs_human(s):
        tone = "needs_human"
    else:
        tone = _CONFIRM_TONE.get(conf, "chase")
    return {"id": s.get("id"), "kind": s.get("kind"), "role": s.get("role"),
            "name": s.get("name"), "confirmation": conf, "tone": tone,
            "outstanding": s.get("outstanding"), "chase_count": cc,
            "last_chased_at": s.get("last_chased_at")}


def board_view(tenant_id: str, *, today: date | None = None, client=None) -> dict:
    """The §2 board view-model: THIS WEEK's event cards + a NEXT 4 WEEKS summary.
    Reads crm_events_between over a 5-week window, then splits at the week boundary."""
    # Derive "today" in the app's consistent tz (the diary's DIARY_TZ), NOT naive
    # local — writes stamp UTC, so a naive now() would slide the "THIS WEEK" split
    # by one near midnight.
    if today is None:
        from app.scheduling import DIARY_TZ   # local import — keep the module light
        today = datetime.now(DIARY_TZ).date()
    week_end = today + timedelta(days=6)
    horizon = today + timedelta(days=34)   # this week (7d) + next 4 weeks (28d)
    events = db.crm_events_between(tenant_id, today.isoformat(), horizon.isoformat(),
                                   client=client)
    this_week, later = [], []
    for e in events:
        ed = e.get("event_date")
        if not ed:
            continue
        (this_week if ed <= week_end.isoformat() else later).append(e)
    cards = [_card(tenant_id, e, client=client) for e in this_week]
    later_value = sum(float((l.get("crm_deals") or {}).get("value_estimate") or 0)
                      for l in later)
    return {
        "today": today.isoformat(),
        "week_label": "Week of " + today.strftime("%d %b").lstrip("0"),
        "this_week": cards,
        "this_week_count": len(cards),
        "next_weeks_count": len(later),
        "next_weeks_value": later_value,
        "next_weeks": [{"id": l["id"], "title": (l.get("crm_deals") or {}).get("title") or "Event",
                        "event_date": l.get("event_date"),
                        "guest_count": l.get("guest_count"),
                        "value": (l.get("crm_deals") or {}).get("value_estimate")}
                       for l in later],
    }


def detail_view(tenant_id: str, event_id: str, *, client=None) -> dict | None:
    """The §2 event-detail view-model: the deal + event fields + guest list + dietary
    matrix + stakeholder list. None when the event isn't this tenant's (IDOR-safe)."""
    event = db.crm_get_event(event_id, tenant_id, client=client)
    if event is None:
        return None
    deal = db.crm_get_deal(event["deal_id"], tenant_id, client=client) or {}
    stakeholders = db.crm_list_event_stakeholders(tenant_id, event_id, client=client)
    # Enrich each stakeholder row with the derived CHASE state so the detail page renders
    # the ✅/⚠/⏳ lights, the chase count, and the "needs a human" flag (§7/§8).
    for s in stakeholders:
        light = _stakeholder_light(s)
        s["tone"] = light["tone"]
        s["is_chasing"] = _is_chasing(s)
        s["needs_human"] = _needs_human(s)
    ros = event.get("run_of_show") or {}
    return {
        "event": event,
        "deal": deal,
        "guests": db.crm_list_event_guests(tenant_id, event_id, client=client),
        "dietary_matrix": db.crm_event_dietary_matrix(tenant_id, event_id, client=client),
        "stakeholders": stakeholders,
        "lights": _CONFIRM_TONE,
        "max_chases": MAX_CHASES,
        # The assembled sheet + gaps (Phase C). An empty {} until first assembly; the
        # template renders the [Assemble] prompt in that case. Never trust it to have keys.
        "run_of_show": ros if isinstance(ros, dict) else {},
    }


# ══ Phase C — run-of-show assembly (docs/os/events.md §6.3, §14) ═══════════════
# The run-of-show is the venue's actual daily deliverable: the assembled, minute-by-
# minute schedule with GAPS FLAGGED LOUDLY. Assembly is DETERMINISTIC — NO LLM — because
# a run sheet carries real liability (§14: a wrong carriage time = missed taxis). It MERGES
# stated facts (the timings array, each annotated with its responsible stakeholder; the
# dietary matrix for the caterer line; staff call-times; contingency notes) and FLAGS gaps
# by RULE. It NEVER fabricates a timing, a name or a number: a gap stays a gap. The only
# LLM surfaces in Phase C are the sign-off draft and the blast wording — both facts-only.

# Gap kinds — a stable, machine-readable tag per gap so the board card + tests key off it.
GAP_UNASSIGNED_TIMING = "unassigned_timing"
GAP_UNCONFIRMED_SUPPLIER = "unconfirmed_supplier"
GAP_MISSING_HEADCOUNT = "missing_headcount"
GAP_OPEN_PROMISE = "open_promise"

# Deterministic role → timing-label keyword hints, so a "Dinner" line annotates the caterer
# and a "Carriages" line the transport/bar as appropriate. Pure lookup — a label with no
# keyword match simply has no responsible party (which is then flagged as a gap, never
# guessed). Matched on the stakeholder's ROLE containing one of these tokens.
_TIMING_ROLE_HINTS = {
    "arrival": ("front of house", "foh", "host", "coordinator", "waiting"),
    "reception": ("front of house", "foh", "host", "coordinator", "waiting"),
    "ceremony": ("celebrant", "registrar", "officiant", "coordinator"),
    "speeches": ("av", "sound", "coordinator", "mc"),
    "dinner": ("caterer", "catering", "waiting", "kitchen", "chef"),
    "meal": ("caterer", "catering", "waiting", "kitchen", "chef"),
    "service": ("caterer", "catering", "waiting", "kitchen", "chef"),
    "dance": ("dj", "band", "av", "sound", "entertainment"),
    "band": ("band", "dj", "av", "sound", "entertainment"),
    "bar": ("bar", "bartender"),
    "carriages": ("transport", "taxi", "coach", "coordinator"),
}


def _sort_key_time(t) -> tuple:
    """A stable sort key for a HH:MM (or H:MM / free) time string. Well-formed times sort
    chronologically; anything unparseable sorts LAST (never dropped, never reordered into a
    fabricated slot) but keeps its stated text. NEVER invents a time."""
    raw = str((t or {}).get("time") or "").strip()
    parts = raw.replace(".", ":").split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hh <= 47 and 0 <= mm <= 59:
            return (0, hh * 60 + mm)
    except (ValueError, IndexError):
        pass
    return (1, raw)   # unparseable → sort after the timed lines, order preserved by raw


def _responsible_for(label: str, stakeholders: list[dict]) -> dict | None:
    """The stakeholder responsible for a timing line, chosen DETERMINISTICALLY: (1) the
    label token-matches a stakeholder ROLE directly (a "Bar" line → the 'bar' role); else
    (2) a keyword hint maps the label to a role family (a "Dinner" line → the caterer). No
    match → None (the caller flags an unassigned-timing gap). NEVER guesses a name."""
    lab = str(label or "").strip().lower()
    if not lab:
        return None
    # (1) direct: the label mentions the role (or vice versa) — e.g. "Bar service" ↔ 'bar'.
    for s in stakeholders:
        role = str(s.get("role") or "").strip().lower().replace("_", " ")
        if role and (role in lab or lab in role):
            return s
    # (2) keyword hint: the label's leading word maps to a role family.
    for key, roles in _TIMING_ROLE_HINTS.items():
        if key in lab:
            for s in stakeholders:
                role = str(s.get("role") or "").strip().lower().replace("_", " ")
                if any(tok in role for tok in roles):
                    return s
    return None


def assemble_run_of_show(event: dict, *, stakeholders: list[dict],
                         guests: list[dict] | None = None,
                         dietary_matrix: dict | None = None,
                         open_promises: list[dict] | None = None,
                         guest_count_final: bool | None = None,
                         now=None) -> dict:
    """DETERMINISTIC run-of-show assembly (§6.3, §14) — NO LLM. Merge the STATED facts into
    an ordered run_of_show dict + an explicit gap list. Same input → same output.

    Returns {"lines", "staff_call_times", "dietary_summary", "contingency_notes", "gaps",
    "assembled_at"}. Each `line` is {label, time, responsible?}. `gaps` is a list of
    {kind, detail} — one per rule below. The assembly NEVER fabricates a timing, a name or
    a number: a line with no responsible party keeps `responsible: None` and yields a gap;
    an empty `timings` yields an empty `lines` + a headcount/promise gap set, not phantom
    rows.

    Gap rules (§6.3):
      (a) GAP_UNASSIGNED_TIMING   — a timing line with no responsible stakeholder;
      (b) GAP_UNCONFIRMED_SUPPLIER — a supplier whose confirmation != 'confirmed';
      (c) GAP_MISSING_HEADCOUNT   — no CONFIRMED final headcount (guest_count_final falsy);
      (d) GAP_OPEN_PROMISE        — an open they_owe promise on the event's deal.
    """
    stakeholders = stakeholders or []
    gaps: list[dict] = []

    # ── ordered timing lines, each annotated with its responsible stakeholder ────────
    raw_timings = event.get("timings")
    raw_timings = raw_timings if isinstance(raw_timings, list) else []
    # Only well-formed {label,time} pairs pass — a malformed entry is dropped, NEVER
    # coerced into a fabricated slot (the same discipline as the capture hook's cleaner).
    clean: list[dict] = []
    for t in raw_timings:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()
        time = str(t.get("time") or "").strip()
        if label and time:
            clean.append({"label": label, "time": time})
    clean.sort(key=_sort_key_time)

    lines: list[dict] = []
    for t in clean:
        resp = _responsible_for(t["label"], stakeholders)
        if resp is None:
            line = {"label": t["label"], "time": t["time"], "responsible": None}
            gaps.append({"kind": GAP_UNASSIGNED_TIMING,
                         "detail": f"{t['label']} ({t['time']}) has no assigned party"})
        else:
            line = {"label": t["label"], "time": t["time"],
                    "responsible": {"role": resp.get("role"), "name": resp.get("name"),
                                    "kind": resp.get("kind"),
                                    "confirmation": resp.get("confirmation")}}
        lines.append(line)

    # ── staff call-times: assigned staff, with any stated outstanding call-time item ─
    staff_call_times = [
        {"role": s.get("role"), "name": s.get("name"),
         "confirmation": s.get("confirmation"),
         # We surface the stated outstanding item verbatim (e.g. "call time") — we never
         # INVENT a call time; if it's unstated it stays unstated and the item is chased.
         "outstanding": s.get("outstanding")}
        for s in stakeholders if s.get("kind") == "staff"
    ]

    # ── the caterer line's dietary summary (from the matrix — never re-counted here) ─
    dietary_summary = dict(dietary_matrix or {})

    # ── gap (b): every unconfirmed SUPPLIER (a declined one is a hard gap too) ───────
    for s in stakeholders:
        if s.get("kind") != "supplier":
            continue
        if s.get("confirmation") != "confirmed":
            who = str(s.get("role") or "supplier").replace("_", " ")
            name = f" — {s.get('name')}" if s.get("name") else ""
            gaps.append({"kind": GAP_UNCONFIRMED_SUPPLIER,
                         "detail": f"{who}{name} not confirmed "
                                   f"({s.get('confirmation') or 'pending'})"})

    # ── gap (c): missing CONFIRMED final headcount ──────────────────────────────────
    # A real 0 is a legitimate (if odd) count — the gap is the absence of the FINAL flag,
    # not a falsy count. guest_count_final defaults to the event row's own flag.
    final = event.get("guest_count_final") if guest_count_final is None else guest_count_final
    if not final:
        gc = event.get("guest_count")
        detail = ("final headcount not confirmed"
                  + (f" (working number: {gc})" if gc is not None else " (no headcount yet)"))
        gaps.append({"kind": GAP_MISSING_HEADCOUNT, "detail": detail})

    # ── gap (d): open they_owe promises on the event's deal ─────────────────────────
    for p in (open_promises or []):
        if not isinstance(p, dict):
            continue
        if (p.get("direction") or "they_owe") != "they_owe":
            continue   # we only flag what SUPPLIERS/parties owe US, not our own promises
        desc = str(p.get("description") or "an outstanding item").strip()
        gaps.append({"kind": GAP_OPEN_PROMISE, "detail": f"open item: {desc}"})

    assembled_at = None
    if now is not None:
        try:
            assembled_at = now.astimezone(timezone.utc).isoformat()
        except (AttributeError, ValueError):
            assembled_at = str(now)
    else:
        assembled_at = datetime.now(timezone.utc).isoformat()

    return {
        "lines": lines,
        "staff_call_times": staff_call_times,
        "dietary_summary": dietary_summary,
        "contingency_notes": (event.get("contingency_notes") or "").strip() or None,
        "gaps": gaps,
        "assembled_at": assembled_at,
    }


def assemble_and_store(tenant_id: str, event: dict, *, now=None, client=None) -> dict:
    """Assemble the run-of-show for an event from its CURRENT child rows and PERSIST it onto
    crm_events.run_of_show via crm_update_event. Returns {"run_of_show", "gaps"} — the board
    card's "N gaps flagged" reads off len(gaps). Deterministic: it only reads + assembles;
    the ONLY writes are the assembled sheet (no LLM anywhere on this path).

    A prior sign-off stamp on the stored sheet is PRESERVED across re-assembly so the daily
    job's dedupe survives a manual re-assemble (the stamp lives inside run_of_show — no
    migration; §6.3)."""
    event_id = event["id"]
    stakeholders = db.crm_list_event_stakeholders(tenant_id, event_id, client=client)
    matrix = db.crm_event_dietary_matrix(tenant_id, event_id, client=client)
    guests = db.crm_list_event_guests(tenant_id, event_id, client=client)
    # Open they_owe promises on the event's DEAL (a gap per open item, §6.3(d)).
    open_promises = _open_they_owe(tenant_id, event.get("deal_id"), client=client)
    sheet = assemble_run_of_show(
        event, stakeholders=stakeholders, guests=guests, dietary_matrix=matrix,
        open_promises=open_promises, now=now)
    # Carry forward the sign-off stamp (dedupe across re-assembly) — never lose it.
    prior = event.get("run_of_show") or {}
    if isinstance(prior, dict) and prior.get("signoff_requested_at"):
        sheet["signoff_requested_at"] = prior["signoff_requested_at"]
    db.crm_update_event(event_id, tenant_id, {"run_of_show": sheet}, client=client)
    return {"run_of_show": sheet, "gaps": sheet["gaps"]}


def _open_they_owe(tenant_id: str, deal_id, *, client=None) -> list[dict]:
    """The event deal's OPEN they_owe promises — the run-sheet's open-item gaps (§6.3(d)).
    Best-effort: a read failure yields no promise-gaps rather than breaking assembly."""
    if not deal_id:
        return []
    try:
        rows = db.crm_open_promises(tenant_id, client=client)
    except Exception:
        log.warning("run-of-show: open-promise read failed for deal %s", deal_id,
                    exc_info=True)
        return []
    return [p for p in rows
            if p.get("deal_id") == deal_id and (p.get("direction") == "they_owe")]


# ── the sign-off request draft (§6.3) — the ONE LLM surface, facts-only ─────────
SIGNOFF_PURPOSE = (
    "A short, warm note to the CLIENT asking them to review and sign off the run sheet for "
    "their upcoming event. If there are still open items to resolve, list them plainly and "
    "ask the client to help close them; if the sheet is complete, simply ask them to confirm "
    "it reads right. One clear ask, courteous. Do NOT invent any timing, name, number or "
    "deadline — reference only the facts supplied below."
)


def signoff_facts(deal: dict, event: dict, sheet: dict) -> str:
    """The facts block the sign-off draft composes from — the assembled-sheet summary + the
    open gaps to resolve. STATED FACTS ONLY (§14): the assembled lines and the gap list, both
    deterministic; the drafter is told to invent nothing."""
    title = (deal.get("title") or "your event").strip()
    ed = str(event.get("event_date") or "").strip()
    lines = [f"Event: {title}" + (f" on {ed}" if ed else "")]
    gc = event.get("guest_count")
    if gc is not None:
        final = " (confirmed)" if event.get("guest_count_final") else " (not yet final)"
        lines.append(f"Headcount: {gc}{final}")
    ros_lines = sheet.get("lines") or []
    if ros_lines:
        lines.append("Run sheet as it stands:")
        for ln in ros_lines:
            who = (ln.get("responsible") or {}).get("role")
            who = f" — {str(who).replace('_', ' ')}" if who else ""
            lines.append(f"  {ln.get('time')} {ln.get('label')}{who}")
    else:
        lines.append("Run sheet: no timings pinned yet.")
    gaps = sheet.get("gaps") or []
    if gaps:
        lines.append("Still to resolve before we can lock it:")
        for g in gaps:
            lines.append(f"  - {g.get('detail')}")
    return "\n".join(lines)


def signoff_subject(deal: dict, event: dict) -> str:
    title = (deal.get("title") or "your event").strip()
    ref = f"EVT-{str(event.get('id') or '')[:8].upper()}"
    return f"{title} — please review your run sheet [{ref}]"


async def compose_signoff(tenant_id: str, cfg: dict, event: dict, deal: dict, sheet: dict,
                          *, now, auto: bool = False) -> str:
    """Compose + queue (and maybe send) the CLIENT sign-off request for an assembled run
    sheet — modelled verbatim on compose_chase / send_service_reminders._remind_one:

        compose_outbound(cfg, SIGNOFF_PURPOSE, facts) → insert_email_log(kind="run_sheet")
        → stamp signoff_requested_at (dedupe) → log_agent_event → usage.record_anthropic →
        auto-send-or-leave-pending.

    Returns 'sent' | 'drafted'. DRAFT-FIRST by default (§14): `auto` comes from the caller's
    dedicated opt-in, defaulting False. The sign-off stamp lands on the STORED sheet (inside
    run_of_show — no migration) so the daily job never re-drafts the same sheet."""
    from app import agent, mailbox, usage

    to = ((deal.get("crm_contacts") or {}).get("email")
          or deal.get("contact_email") or "").strip()
    facts = signoff_facts(deal, event, sheet)
    subject = signoff_subject(deal, event)
    result = await agent.compose_outbound(cfg, SIGNOFF_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="run_sheet")
    log_id, _ = db.insert_email_log(
        tenant_id, to or None, subject, f"[run sheet sign-off] {facts}", result.text,
        status="pending", kind="run_sheet", thread_id=f"event:{event.get('id')}")
    # Stamp the STORED sheet BEFORE the send (dedupe even if the send fails) — the
    # signoff_requested_at lives inside run_of_show, re-read + merged so a concurrent
    # assemble can't clobber it.
    _stamp_signoff(tenant_id, event, now=now)
    db.log_agent_event(
        tenant_id, "run_sheet",
        f"Drafted the run-sheet sign-off for {(deal.get('title') or 'the event')}"
        f" ({len(sheet.get('gaps') or [])} gaps to resolve)",
        direction="outbound", contact=to or None, subject=subject, email_log_id=log_id,
        detail={"event_id": event.get("id"), "gaps": len(sheet.get("gaps") or [])})
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="run_sheet", email_log_id=log_id)
    if auto and to:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent", f"Sent the run-sheet sign-off to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


def _stamp_signoff(tenant_id: str, event: dict, *, now) -> None:
    """Mark the stored run_of_show as sign-off-requested (dedupe for the daily job). Re-reads
    the event so the stamp merges onto the LATEST sheet, never a stale in-hand copy — the
    stamp lives inside run_of_show (no migration). Best-effort: a stamp failure never breaks
    the send (worst case the job re-drafts once — the mark lands after the log row exists)."""
    try:
        stamp = (now.astimezone(timezone.utc).isoformat()
                 if hasattr(now, "astimezone") else datetime.now(timezone.utc).isoformat())
        fresh = db.crm_get_event(event["id"], tenant_id) or event
        ros = fresh.get("run_of_show")
        ros = dict(ros) if isinstance(ros, dict) else {}
        ros["signoff_requested_at"] = stamp
        db.crm_update_event(event["id"], tenant_id, {"run_of_show": ros})
    except Exception:
        log.warning("run-of-show: sign-off stamp failed for event %s",
                    event.get("id"), exc_info=True)


# ── the run-sheet blast to every stakeholder (§7 POST .../run-of-show/send) ─────
BLAST_PURPOSE = (
    "A clear, friendly note to a supplier or staff member with the FINAL run sheet for an "
    "upcoming event, so everyone works to the same schedule on the day. Summarise the "
    "timings and their part in plain terms. One short message. Do NOT invent any timing, "
    "name, number or deadline beyond the facts supplied."
)


def blast_facts(stakeholder: dict, event: dict, deal: dict, sheet: dict) -> str:
    """The facts block for one stakeholder's run-sheet blast — the assembled lines + this
    party's own line(s). STATED FACTS ONLY."""
    title = (deal.get("title") or "the event").strip()
    ed = str(event.get("event_date") or "").strip()
    who = str(stakeholder.get("role") or stakeholder.get("kind") or "team").replace("_", " ")
    lines = [f"Event: {title}" + (f" on {ed}" if ed else ""),
             f"Your role: {who}"]
    for ln in (sheet.get("lines") or []):
        rrole = (ln.get("responsible") or {}).get("role")
        mine = " (you)" if rrole and str(rrole) == str(stakeholder.get("role")) else ""
        lines.append(f"  {ln.get('time')} {ln.get('label')}{mine}")
    if not (sheet.get("lines") or []):
        lines.append("  (timings still being finalised)")
    return "\n".join(lines)


async def blast_run_sheet(tenant_id: str, cfg: dict, event: dict, deal: dict, sheet: dict,
                          *, stakeholders: list[dict], now, auto: bool = False) -> dict:
    """Send the FINAL run sheet to every stakeholder with an email — one compose_outbound per
    party (fan-out, §7/§12). Draft-first by default (§14). Returns {"drafted", "sent"}. Each
    party is isolated in its own try/except so one bad send never drops the rest."""
    from app import agent, mailbox, usage

    drafted = sent = 0
    subject = signoff_subject(deal, event).replace("please review your run sheet",
                                                   "final run sheet")
    for s in stakeholders:
        to = (s.get("email") or "").strip()
        if not to:
            continue   # no address → nobody to blast (a gap the manager already sees)
        try:
            facts = blast_facts(s, event, deal, sheet)
            result = await agent.compose_outbound(cfg, BLAST_PURPOSE, facts,
                                                  tenant_id=tenant_id, log_operation="run_sheet")
            log_id, _ = db.insert_email_log(
                tenant_id, to, subject, f"[run sheet] {facts}", result.text,
                status="pending", kind="run_sheet", thread_id=f"event:{event.get('id')}")
            db.log_agent_event(
                tenant_id, "run_sheet",
                f"Sent the final run sheet to the {(s.get('role') or s.get('kind') or 'team')}"
                f" on {(deal.get('title') or 'the event')}",
                direction="outbound", contact=to, subject=subject, email_log_id=log_id,
                detail={"event_id": event.get("id"), "stakeholder_id": s.get("id")})
            usage.record_anthropic(tenant_id, result.model, result.usage,
                                   operation="run_sheet", email_log_id=log_id)
            if auto:
                await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
                db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
                sent += 1
            else:
                drafted += 1
        except Exception:
            log.warning("run-sheet blast failed for stakeholder %s on event %s",
                        s.get("id"), event.get("id"), exc_info=True)
            continue
    return {"drafted": drafted, "sent": sent}
