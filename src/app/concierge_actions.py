"""Concierge business actions (Phase 2) — staging and execution.

The confirm-first contract (docs/modules/module-concierge-business.md):
  * The model only ever PROPOSES: its `action` output lands in stage(), which
    re-validates every field against the tenant's REAL data (ids must exist and
    be owned, stages must be real stages, windows must fit the diary mode) and
    builds the human card. Nothing the model wrote is trusted as-is.
  * stage() writes a concierge_proposals row (pending, single-use, 5-minute
    expiry) — the audit trail starts before anything can change.
  * execute() runs ONLY after the operator's Confirm click atomically claims
    that row. It composes the platform's existing write paths — the approval
    queue, the capacity-gated booking RPCs, the CRM helpers, kb.ingest — and
    never invents a new one.

Deliberately absent: deleting anything but a DRAFT invoice or quote that nothing
has ever happened to (2026-10-15: invoice_delete_draft / quote_delete_draft, and
the edit twins invoice_edit_draft / quote_edit_draft — always on a card, never
automatic, never for a sent one). A drafted reply goes to the inbox
approval queue; the existing approve-to-send flow still governs actual reply
sending. The one sanctioned exception (2026-07-26): invoice_send and
invoice_reminder_draft email on Confirm — an owner-initiated commercial send
where the confirm card IS the approval, mirroring the Send button on
/app/invoices/{id} (both compose src/app/invoice_send.py).
"""
import asyncio
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from app import db, events, scheduling
from app import knowledge as kb

log = logging.getLogger(__name__)

PROPOSAL_TTL_S = 300   # Confirm must land within 5 minutes of the proposal

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ActionError(Exception):
    """A proposal (or execution) that can't proceed — .args[0] is the honest,
    customer-facing sentence the chat shows."""


# Changes to, or removal of, a customer's document. Whatever per-feature auto-trigger a
# business sets up later, these never qualify (Kris 2026-10-15): the owner taps a card
# every time, and approval_mode() answers 'confirm' for them before it looks at anything
# else.
ALWAYS_CONFIRM_KINDS = frozenset((
    "invoice_edit_draft", "invoice_delete_draft", "quote_edit_draft", "quote_delete_draft"))

# A delete is confirmed by a deliberate tap on its card and by nothing else (Kris
# 2026-10-16): never a spoken or typed "yes", "go ahead" or "do it", on any surface. The
# older Standard voice client reads a spoken yes as the confirm for whatever card is
# waiting; GPT-Live and the typed panel already send words back to the card. The rule is
# the server's, at the one confirm route every surface uses (concierge_routes.
# business_execute): for these kinds the request must carry the marker only the card's own
# button sends (confirm_via = "tap"), and a missing marker is not a tap. Edits are not
# here: a spoken yes may still confirm an edit, as for the other kinds.
TAP_ONLY_KINDS = frozenset(("invoice_delete_draft", "quote_delete_draft"))
TAP_ONLY_SENTENCE = "That is a delete: tap Confirm on the card."


# ── parked work: how long it lives, and what may be prepared again ─────────────────────
# A card nobody tapped waits on the owner's list for the owner's own task-expiry setting
# (default 24 h, between 60 minutes and 7 days: concierge_settings). Tapping it AFTER its
# five-minute confirm window never revives it: it prepares a NEW card against today's
# records, for a fresh tap (business_resurface). Which kinds, per docs/concierge-confirm-
# audit-2026-10-16.md section 5:
#   silent  everything the executor needs is re-read from records; the owner is told it was
#           prepared again and still taps the new card;
#   revise  the owner is told it may have changed and reads the current version;
#   never   sends, reminders, renewal requests, document edits and deletes, and bulk cards
#           as a whole: they are asked for again from scratch.
REPREPARE_SILENT = frozenset(("confirm_booking", "assign_person", "block_date", "unblock_date",
                              "create_contact", "quote_convert_invoice"))
REPREPARE_NEVER = frozenset(("invoice_send", "quote_send", "invoice_reminder_draft",
                             "quote_reminder_draft", "request_renewal", "invoice_edit_draft",
                             "quote_edit_draft", "invoice_delete_draft", "quote_delete_draft", "bulk"))
REPREPARED_SILENT = "Prepared again for you to confirm."
REPREPARED_REVISE = ("This may have changed since I first prepared it. Here is the current "
                     "version; confirm again if it is right.")
NEVER_SENTENCE = ("I don't keep that kind of change on the list: it would send something or change "
                  "or delete a document. Ask me to prepare it again and I'll show you the current version.")


def reprepare_class(kind, payload=None) -> str:
    """'silent', 'revise' or 'never' for a parked card of this kind."""
    payload = payload if isinstance(payload, dict) else {}
    if kind in REPREPARE_NEVER:
        return "never"
    if kind == "confirm_booking" and (payload.get("then") or payload.get("draft_text")):
        return "revise"           # a drafted email rides along: the owner reads it again
    if kind == "update_contact":
        return "silent" if payload.get("mode") == "note" else "revise"
    return "silent" if kind in REPREPARE_SILENT else "revise"


def parked_lifetime_s(tenant_id: str, client=None) -> int:
    """How long an unanswered card stays on the owner's list: their task-expiry setting.
    Enforced in business_resurface, the waiting list, and the ledger's Parked/Expired."""
    from app import concierge_settings
    try:
        tenant = db.get_tenant(tenant_id, client=client)
    except Exception:
        tenant = None
    return int(concierge_settings.resolved(tenant)["task_expire_minutes"]) * 60


def needs_tap(kind, items=None) -> bool:
    """Does confirming a card of this kind need the owner's tap? A tap-only kind does, and
    so does a bulk card holding one that can actually run (an item that could not be
    prepared stays unticked and never runs, so it does not count)."""
    if kind in TAP_ONLY_KINDS:
        return True
    return kind == "bulk" and isinstance(items, (list, tuple)) and any(
        isinstance(i, dict) and i.get("ok") and i.get("kind") in TAP_ONLY_KINDS for i in items)


def row_needs_tap(row) -> bool:
    """needs_tap for a stored proposal row (its kind, and a bulk's prepared items)."""
    row = row or {}
    return needs_tap(row.get("kind"), (row.get("payload") or {}).get("items"))


def tap_flag(kind, items=None) -> dict:
    """{"tap_only": True} to spread into a card the client shows, or nothing. The client
    uses it only to say 'tap Confirm' instead of 'say yes'; the refusal is the server's."""
    return {"tap_only": True} if needs_tap(kind, items) else {}


def approval_mode(kind: str) -> str:
    """How a staged change of this kind is authorised. Today every Concierge
    card is 'confirm': only the owner's tap on the card runs it, a spoken yes
    never does (Kris 2026-10-14: that is right while everything is approve-
    first; when per-feature auto-triggers arrive the business will choose, per
    kind, what goes out on its own, and this is the one place that answer
    lives). The drafts path has its own mode already (mailbox auto_send,
    src/app/concierge_draft.py)."""
    if kind in ALWAYS_CONFIRM_KINDS:
        return "confirm"
    # A business's own per-kind choice will be read BELOW this line, so it can never
    # reach the kinds above.
    return "confirm"


def allowed_kinds(full_qa: bool, scheduling_on: bool) -> frozenset:
    """Which action kinds this tenant's tier + modules unlock. Mirrors the spec:
    email drafting + KB on every tier, booking moves wherever the office diary
    exists, CRM writes on Pro (the 'crm' module — same boundary as the pages)."""
    kinds = {"draft_reply", "add_kb_entry", "define_dependants", "update_dependant",
             "request_renewal", "bulk",
             # Invoicing is a standalone core module (2026-07-26) — every tier.
             "invoice_create", "invoice_send", "invoice_mark_paid",
             "invoice_reminder_draft",
             # Edit or delete a DRAFT invoice (2026-10-15), on a card, every time.
             "invoice_edit_draft", "invoice_delete_draft",
             # Quotes ride the same core-module boundary (docs/modules/module-quotes.md
             # design rule 3: nothing quote-shaped is paywalled).
             "quote_create", "quote_send", "quote_mark_accepted",
             "quote_mark_declined", "quote_convert_invoice", "quote_cancel",
             "quote_reminder_draft",
             # Edit or delete a DRAFT quote (2026-10-15), on a card, every time.
             "quote_edit_draft", "quote_delete_draft"}
    if scheduling_on:
        kinds.add("move_booking")
        kinds.add("cancel_booking")
        kinds.add("confirm_booking")
        kinds.add("block_date")
        kinds.add("unblock_date")
        kinds.add("change_stay")
        kinds.add("assign_person")
    if full_qa:
        kinds |= {"update_deal_stage", "update_deal_value",
                  "create_followup", "create_contact", "update_contact"}
    return frozenset(kinds)


def _clean(action: dict, key: str, max_len: int = 300) -> str:
    return str((action or {}).get(key) or "").strip()[:max_len]


def _require_date(value: str, what: str) -> str:
    if not _DATE_RE.match(value):
        raise ActionError(f"I need a real date for {what} (got '{value or 'nothing'}').")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ActionError(f"'{value}' isn't a valid date.")
    return value


def _alternatives(tenant_id: str, date_iso: str, client=None) -> str:
    """Up to three free slots on/near the requested date, for a friendly
    'that slot is taken' message. Best-effort."""
    try:
        day = datetime.strptime(date_iso, "%Y-%m-%d").date()
        options = scheduling.check_availability(tenant_id, day, day + timedelta(days=2),
                                                client=client)[:3]
        if options:
            return " Free instead: " + "; ".join(
                f"{o['date']} ({o['window']})" for o in options) + "."
    except Exception:
        pass
    return ""


# ── staging (validate the model's proposal against real data) ────────────────
# What each action needs the asker to be allowed to do (docs/plan-people.md
# slice 4). The assistant must not be a way round the ticks on the People
# page: if a staff member cannot press Cancel on the diary, they cannot ask
# the assistant to cancel either. An unmapped kind falls to settings:change —
# the narrowest ability — so an action added later is closed to staff until
# somebody decides where it belongs.
ACTION_ABILITIES: dict[str, tuple[str, str]] = {
    "cancel_booking": ("diary", "cancel"),
    "confirm_booking": ("diary", "add"),      # the Diary's Confirm button ability
    "move_booking": ("diary", "add"),
    "block_date": ("diary", "add"),
    "unblock_date": ("diary", "add"),
    "change_stay": ("diary", "add"),
    "assign_person": ("diary", "add"),
    "draft_reply": ("inbox", "approve"),
    "update_contact": ("crm", "edit"),
    "update_dependant": ("crm", "edit"),
    "request_renewal": ("crm", "edit"),
    "create_contact": ("crm", "edit"),
    "update_deal_stage": ("crm", "edit"),
    "update_deal_value": ("crm", "edit"),
    "create_followup": ("crm", "edit"),
    "add_kb_entry": ("settings", "change"),
    "define_dependants": ("settings", "change"),
    "invoice_create": ("money", "send"),
    "invoice_send": ("money", "send"),
    "invoice_mark_paid": ("money", "send"),
    "invoice_reminder_draft": ("money", "chase"),
    # Putting a bill right before it goes out is the same door as making one (2026-10-15).
    "invoice_edit_draft": ("money", "send"),
    "invoice_delete_draft": ("money", "send"),
    "quote_create": ("money", "send"),
    "quote_send": ("money", "send"),
    "quote_mark_accepted": ("money", "send"),
    "quote_mark_declined": ("money", "send"),
    "quote_convert_invoice": ("money", "send"),
    "quote_cancel": ("money", "send"),
    "quote_edit_draft": ("money", "send"),
    "quote_delete_draft": ("money", "send"),
    "quote_reminder_draft": ("money", "chase"),
}


def ability_for(kind: str) -> tuple[str, str]:
    return ACTION_ABILITIES.get(kind, ("settings", "change"))


# Stagers whose card depends on WHO is asking, not only whether they may: the
# customer and field lenses (M36) and the own-mailbox lens (M35). They take
# `member` as a keyword; the rest keep their three-argument shape.
_MEMBER_STAGERS = frozenset(("draft_reply", "update_contact", "update_dependant",
                             "request_renewal"))


def _run_stager(kind: str, tenant_id, client, action, member):
    """Run the stager for `kind`, handing it the person when it needs one. None
    is the account's own owner — nothing to narrow, so the call is unchanged."""
    stager = _STAGERS[kind]
    if member is not None and kind in _MEMBER_STAGERS:
        return stager(tenant_id, client, action, member=member)
    return stager(tenant_id, client, action)


NOT_THEIR_CUSTOMER = ("That customer isn't one of the ones you work with — "
                      "ask whoever runs the account.")
HEALTH_FIELD = ("That's a health detail — I keep those out of what I read and change. "
                "Open the customer's page to update it by hand.")
PRIVATE_FIELD = ("That detail is private on this account, so I can't show or "
                 "change it for you — ask whoever runs the account.")
NOT_THEIR_MAILBOX = ("That conversation is in a mailbox that isn't yours — "
                     "nothing was drafted.")


def _require_their_customer(tenant_id, client, member, contact: dict | None) -> None:
    """Refuse a customer outside this person's customer lens. Fails closed:
    a lens that can't be read refuses."""
    from app import crm_visibility
    if not crm_visibility.narrows_contacts(member):
        return
    try:
        from app import concierge_data
        allowed = concierge_data.allowed_customers(tenant_id, client, member)
    except Exception:
        log.warning("stager crm lens failed (tenant=%s)", tenant_id, exc_info=True)
        raise ActionError(NOT_THEIR_CUSTOMER)
    if not crm_visibility.may_open(contact, member, allowed):
        raise ActionError(NOT_THEIR_CUSTOMER)


def _require_their_mailbox(tenant_id, client, member, row: dict | None) -> None:
    """Refuse mail outside this person's own mailboxes (M35)."""
    from app import concierge_data, people
    if not people.inbox_is_theirs_only(member):
        return
    if not concierge_data._their_mailbox_only(tenant_id, client, member, [row or {}]):
        raise ActionError(NOT_THEIR_MAILBOX)


def mailbox_refusal(tenant_id, client, kind: str, payload: dict,
                    member: dict | None) -> str | None:
    """At Confirm: may THIS person write the draft this card carries? A
    draft_reply lands in a real thread; the mailbox is re-read here, not
    trusted from staging. Returns the refusal sentence, or None."""
    from app import people
    if not people.inbox_is_theirs_only(member):
        return None
    if kind == "draft_reply":
        items = [payload or {}]
    elif kind == "bulk":
        items = [it.get("payload") or {} for it in (payload or {}).get("items") or []
                 if it.get("kind") == "draft_reply" and it.get("ok")]
    else:
        items = []
    for p in items:
        try:
            row = db.get_email_log_entry(p.get("log_id"), tenant_id, client=client)
            if not row:
                return NOT_THEIR_MAILBOX
            _require_their_mailbox(tenant_id, client, member, row)
        except ActionError as e:
            return str(e)
        except Exception:
            return NOT_THEIR_MAILBOX
    return None


def may_do(member: dict | None, kind: str) -> bool:
    """May this person have the assistant do that? None is an account that
    predates People — its single user owns it."""
    if member is None:
        return True
    from app import people
    page, ability = ability_for(kind)
    return people.can_do(member, page, ability)


def asker_note(member: dict | None, *, full_qa: bool, scheduling_on: bool) -> str | None:
    """Who the Concierge is talking to, and what it may prepare for them — a
    per-turn note, so the cached prompt stays shared. Kris 2026-09-24, View as
    Em: it talked ABOUT Em ("they'd get a card") instead of TO her, and tried
    to block a date before finding out she can't. The owner (or an account
    from before People) gets no note: everything is theirs."""
    if member is None:
        return None
    from app import people
    if people.is_admin(member):
        return None
    name = people.name_of(member).split(" ")[0].split("@")[0] or "this person"
    labels = {(page, key): label.lower() for page in people.PAGES
              for key, label, _h in people.abilities_for(page)}
    yes, no = set(), set()
    for kind in allowed_kinds(full_qa, scheduling_on):
        pair = ability_for(kind)
        (yes if may_do(member, kind) else no).add(labels.get(pair, pair[1]))
    no -= yes
    parts = [f"[You are talking to {name} ({member.get('role_label') or member.get('role')}), "
             f"not the owner. Speak to {name} directly as \"you\" — never about them in the "
             "third person."]
    parts.append(("What they may have you prepare: " + "; ".join(sorted(yes)) + ".")
                 if yes else "They can't have you change anything — questions only.")
    if no:
        parts.append("NOT theirs to do: " + "; ".join(sorted(no)) + ". If they ask for one of "
                     "these, don't look anything up and don't propose it — say in one "
                     "sentence that it needs the owner or a manager.")
    parts.append("Doors they can open: " + ", ".join(people.PAGE_LABELS[p] for p in
                                                        people.pages_of(member)) + ".]")
    return " ".join(parts)


def team_note(tenant_id: str) -> str | None:
    """The team, for the model (plan step 3): who works here and what they do,
    so "delegate the next drop-off to Em" isn't a customer search for Em.
    Nothing for a one-person business."""
    from app import job_people, people
    roster = job_people.team(tenant_id)
    if len(roster) < 2:
        return None
    try:
        labels = {k["key"]: k["label"].lower()
                  for k in people.kinds_for(db.get_scheduling_config(tenant_id) or {})}
    except Exception:
        labels = {}

    def line(t):
        bits = [t["role_label"]] + ([] if t["has_login"] else ["freelancer or helper, gets job sheets"])
        does = [labels.get(k, k) for k in t["does"]]
        return f"{t['name']} ({', '.join(bits)}" + (f"; does {', '.join(does)}" if does else "") + ")"
    return ("[The team — people who work here, NOT customers: "
            + "; ".join(line(t) for t in roster) + ".]")


def stage_check(tenant_id: str, action: dict, *, member: dict | None, full_qa: bool,
                scheduling_on: bool, client=None) -> str:
    """Everything stage() checks — may this person do it, can it be prepared
    against today's records — without writing the proposal. View as uses it
    (src/app/viewas.py): the owner sees whether their person WOULD get the card,
    and no card is left waiting on that person's account. Returns the summary;
    raises ActionError with the sentence stage() would have said."""
    kind = _clean(action, "kind", 40)
    allowed = allowed_kinds(full_qa, scheduling_on)
    if kind in allowed and not may_do(member, kind):
        raise ActionError("that isn't something you can do on this account — it needs the owner or a manager")
    if kind not in allowed:
        raise ActionError("that can't be prepared on this account")
    if kind == "bulk":
        return "several changes"
    summary, _payload, _details, _draft = _run_stager(kind, tenant_id, client, action, member)
    return summary


def stage(tenant_id: str, client, action: dict, *, full_qa: bool, scheduling_on: bool,
          user_id: str | None = None, member: dict | None = None,
          spoken_ms: int = 0, park: bool = False, task_binding: dict | None = None) -> dict:
    """Validate one model proposal, write the pending concierge_proposals row,
    and return the confirmation card for the UI. Raises ActionError with the
    exact sentence to show when the proposal can't be prepared. spoken_ms (the
    owner's speaking time that led here, voice only) rides the payload as
    _spoken_ms for src/app/time_saved.py. park=True (a background task, 2026-09-25)
    files the card straight into the waiting list without replacing the card
    the owner may be looking at."""
    kind = _clean(action, "kind", 40)
    allowed = allowed_kinds(full_qa, scheduling_on)
    if kind in allowed and not may_do(member, kind):
        raise ActionError("You don't have permission to do that on this account. "
                          "Ask whoever runs it.")
    if kind not in allowed:
        if kind in ("update_deal_stage", "update_deal_value",
                    "create_followup", "create_contact") and not full_qa:
            raise ActionError("CRM actions are part of the Pro plan — you can upgrade "
                              "on the Plans & billing page.")
        raise ActionError("I can't prepare that action on this account.")

    if kind == "bulk":
        return _stage_bulk(tenant_id, client, action, allowed=allowed, user_id=user_id,
                           member=member, spoken_ms=spoken_ms,
                           **({"task_binding": task_binding} if task_binding else {}),
                           **({"park": True} if park else {}))
    summary, payload, details, draft_text = _run_stager(kind, tenant_id, client, action, member)
    if spoken_ms:
        payload = {**payload, "_spoken_ms": int(spoken_ms)}
    if task_binding:
        payload = {**payload, "_prepared_card": {"details": details, "draft_text": draft_text}}
    proposal_id = db.create_concierge_proposal(
        tenant_id, kind, summary, payload, user_id=user_id, action=dict(action), client=client,
        **({"task_binding": task_binding} if task_binding else {}),
        **({"park": True} if park else {}))
    return {
        "proposal_id": proposal_id,
        "kind": kind,
        "summary": summary,
        "details": details,
        "draft_text": draft_text,      # editable in the card (draft_reply only)
        "expires_s": PROPOSAL_TTL_S,
        "approval": approval_mode(kind),
        **tap_flag(kind),
    }


BULK_MAX_ITEMS = 10


def _stage_bulk(tenant_id, client, action, *, allowed, user_id, member=None, spoken_ms=0,
                park=False, task_binding=None):
    """One card, several changes (Kris, 2026-09-06 — option 1): every item is
    staged against real data on its own; the ones that can't be prepared
    stay on the card unticked with their reason, the rest are ticked, and one
    Confirm runs the ticked ones. A list of one is just that one action."""
    raw_items = [it for it in (action.get("items") or []) if isinstance(it, dict)][:BULK_MAX_ITEMS]
    if not raw_items:
        raise ActionError("There's nothing in that list to do.")
    items = []
    for it in raw_items:
        k = _clean(it, "kind", 40)
        entry = {"kind": k, "ok": False, "error": None, "summary": "", "details": [],
                 "payload": {}, "draft_text": None}
        if k not in allowed or k == "bulk" or k not in _STAGERS:
            entry["error"] = "I can't prepare that kind of change on this account."
            items.append(entry)
            continue
        try:
            if not may_do(member, k):
                raise ActionError("You don't have permission to do that on this account. "
                                  "Ask whoever runs it.")
            summary, payload, details, draft_text = _run_stager(k, tenant_id, client, it, member)
            entry.update(ok=True, summary=summary, details=details or [],
                         payload=payload, draft_text=draft_text)
        except ActionError as e:
            entry["error"] = str(e)
            entry["summary"] = _bulk_item_label(it)
        except Exception:
            log.warning("bulk item staging failed (kind=%s)", k, exc_info=True)
            entry["error"] = "I couldn't prepare that one just now."
            entry["summary"] = _bulk_item_label(it)
        items.append(entry)
    ok = [i for i in items if i["ok"]]
    if not ok:
        raise ActionError("None of those could be prepared — "
                          + " ".join(f"{i['summary'] or i['kind']}: {i['error']}" for i in items[:3]))
    if len(items) == 1:
        # a bulk of one is that one action: same card, same claim, no list
        only = items[0]
        if spoken_ms:
            only["payload"] = {**only["payload"], "_spoken_ms": int(spoken_ms)}
        if task_binding:
            only["payload"] = {**only["payload"], "_prepared_card": {"details": only["details"], "draft_text": only["draft_text"]}}
        proposal_id = db.create_concierge_proposal(
            tenant_id, only["kind"], only["summary"], only["payload"], user_id=user_id,
            action=dict(action), client=client,
            **({"task_binding": task_binding} if task_binding else {}),
            **({"park": True} if park else {}))
        return {"proposal_id": proposal_id, "kind": only["kind"], "summary": only["summary"],
                "details": only["details"], "draft_text": only["draft_text"],
                "expires_s": PROPOSAL_TTL_S, "approval": approval_mode(only["kind"]),
                **tap_flag(only["kind"])}
    kinds = {i["kind"] for i in ok}
    summary = (f"{len(ok)} change{'s' if len(ok) != 1 else ''} ready"
               + (f" — {len(items) - len(ok)} can't be done" if len(ok) != len(items) else ""))
    payload = {"items": [{"kind": i["kind"], "ok": i["ok"], "payload": i["payload"],
                          "summary": i["summary"], "error": i["error"]} for i in items]}
    if spoken_ms:
        payload["_spoken_ms"] = int(spoken_ms)
    if task_binding:
        payload["_prepared_card"] = {"details": ["Untick anything you don't want; one Confirm runs the rest"],
            "draft_text": None, "items": [{key: item.get(key) for key in
                ("kind", "ok", "summary", "details", "error", "draft_text")} for item in items],
            "bulk_kinds": sorted(kinds)}
    proposal_id = db.create_concierge_proposal(
        tenant_id, "bulk", summary, payload, user_id=user_id, action=dict(action), client=client,
        **({"task_binding": task_binding} if task_binding else {}),
        **({"park": True} if park else {}))
    return {"proposal_id": proposal_id, "kind": "bulk", "summary": summary,
            "details": ["Untick anything you don't want; one Confirm runs the rest"],
            "draft_text": None, "expires_s": PROPOSAL_TTL_S, "approval": approval_mode("bulk"),
            **tap_flag("bulk", items),
            "items": [{"kind": i["kind"], "ok": i["ok"], "summary": i["summary"],
                       "details": i["details"], "error": i["error"],
                       "draft_text": i["draft_text"]} for i in items],
            "bulk_kinds": sorted(kinds)}


def _bulk_item_label(it: dict) -> str:
    for f in ("booking_id", "invoice_id", "quote_id", "contact", "name", "title", "new_date"):
        v = _clean(it, f, 80)
        if v:
            return f"{_clean(it, 'kind', 40)} — {v}"
    return _clean(it, "kind", 40)


async def _exec_bulk(tenant_id, client, payload, edited, selected=None):
    """Run the ticked items in order; each success or failure is reported on
    its own line. The bulk proposal is single-use as a whole (claimed once)."""
    items = list(payload.get("items") or [])
    chosen = [i for i, it in enumerate(items)
              if it.get("ok") and (selected is None or i in set(selected))]
    if not chosen:
        raise ActionError("Nothing was ticked — nothing changed.")
    results, first_link = [], None
    for i in chosen:
        it = items[i]
        executor = _EXECUTORS.get(it.get("kind"))
        if not executor:
            results.append({"index": i, "ok": False, "state": "failed", "summary": "That kind isn't supported; it was not dispatched."})
            continue
        try:
            out = await executor(tenant_id, client, it.get("payload") or {}, None)
            if (out or {}).get("state") == "failed":
                # a draft edit or delete that found the record changed under its card:
                # it wrote nothing, and its sentence says so (see _refused)
                results.append({"index": i, "ok": False, "state": "failed",
                                "summary": out.get("summary") or "That one was refused; nothing was changed."})
                continue
            results.append({"index": i, "ok": True, "state": "completed", "summary": (out or {}).get("summary") or "Done."})
            if first_link is None and (out or {}).get("link"):
                first_link = out["link"]
        except ActionError as e:
            results.append({"index": i, "ok": False, "state": "unknown",
                            "summary": "Outcome unconfirmed — check this item before taking another action."})
        except Exception:
            log.exception("bulk item failed (kind=%s)", it.get("kind"))
            results.append({"index": i, "ok": False, "state": "unknown",
                            "summary": "Outcome unconfirmed — check this item before taking another action."})
    done = sum(1 for r in results if r["ok"])
    uncertain = any(r.get("state") == "unknown" for r in results)
    summary = f"{done} of {len(results)} items completed."
    if uncertain:
        summary += " Some outcomes aren't confirmed — check the items before taking another action."
    return {"summary": summary, "link": first_link, "results": results,
            "state": "unknown" if uncertain else "completed" if done else "failed"}


def _signed(tenant_id, client, text):
    """The configured sign-off on a card's email text (src/app/sign_off.py). Best-effort:
    a config read failure leaves the text as the model wrote it, logged, never a
    bounced card."""
    from app import sign_off
    try:
        cfg = db.effective_config(tenant_id, None, client=client)
    except Exception:
        log.warning("sign-off config read failed for tenant %s", tenant_id)
        return text
    return sign_off.apply(text, cfg, "email")[0]


def _stage_draft_reply(tenant_id, client, action, member=None):
    contact = _clean(action, "contact", 254).lower()
    draft_text = str((action or {}).get("draft_text") or "").strip()[:8000]
    if "@" not in contact:
        raise ActionError("I need the contact's email address to draft a reply — "
                          "which conversation is this for?")
    if not draft_text:
        raise ActionError("I didn't produce any draft text — try asking again.")
    rows = db.get_conversation(tenant_id, contact, client=client) or []
    if not rows:
        raise ActionError(f"I couldn't find a conversation with {contact}.")
    latest = rows[-1]
    _require_their_mailbox(tenant_id, client, member, latest)
    draft_text = _signed(tenant_id, client, draft_text)
    payload = {"log_id": latest["id"], "contact": contact,
               "subject": latest.get("subject") or "", "draft_text": draft_text}
    summary = f"Draft a reply to {contact}" + (
        f" — “{payload['subject']}”" if payload["subject"] else "")
    details = ["The draft goes to your inbox approval queue — nothing sends "
               "until you approve it there."]
    from app.concierge_booking_claims import check
    reason, _ = check(tenant_id, contact, draft_text, client)
    if reason:
        payload["booking_hold"] = reason
        summary = f"Held for review — {contact}"
        details = [reason, "Edit the draft to correct this, or ask for a new draft. Nothing has been queued."]
    return summary, payload, details, draft_text


def _stage_move_booking(tenant_id, client, action):
    booking_id = _clean(action, "booking_id", 64)
    new_date = _require_date(_clean(action, "new_date", 10), "the new day")
    new_window = _clean(action, "new_window", 10)
    booking = None
    if _UUID_RE.match(booking_id):
        booking = db.get_booking(booking_id, tenant_id, client=client)
    elif booking_id:
        # The model sometimes hands over the human reference (BK-1001) instead
        # of the row id — resolve it rather than bouncing the whole proposal.
        booking = db.get_booking_by_ref(tenant_id, booking_id, client=client)
    if not booking:
        raise ActionError("I couldn't pin down which booking to move — "
                          "could you say which one (name or day)?")
    booking_id = booking["id"]
    if booking.get("status") not in ("proposed", "confirmed"):
        raise ActionError("That booking isn't in the live diary any more.")
    cfg = db.get_scheduling_config(tenant_id, client=client)
    if _is_services_diary(cfg):
        # One placement rule for both doors (src/app/record_edits.py, 2026-09-06).
        from app import record_edits
        try:
            plan = record_edits.plan_booking_move(tenant_id, booking, new_date, new_window, client=client)
        except record_edits.EditError as e:
            raise ActionError(str(e) + _alternatives(tenant_id, new_date, client)
                              if "no room" in str(e) else str(e))
        from app import dates as dates_mod
        what = booking.get("summary") or "booking"
        who = what if what != "booking" else f"{booking.get('contact')}'s booking"
        old_pretty = dates_mod.pretty_date(str(booking.get("booking_date")))
        payload = {"booking_id": booking["id"], "old_date": str(booking.get("booking_date")),
                   "old_window": booking.get("window"), **plan,
                   "contact": booking.get("contact") or "",
                   "booking_ref": booking.get("booking_ref") or "", "what": what}
        summary = f"Move {who}: {old_pretty} ({payload['old_window']}) → {plan['when']}"
        details = [f"Current: {old_pretty} — {payload['old_window']}",
                   f"New: {plan['when'].replace(' at ', ' — arriving ')} ({hours_label(plan['length_blocks'])})"]
        return summary, payload, details, None
    err = scheduling.hold_error(cfg, new_date, new_window,
                                datetime.now(scheduling.DIARY_TZ))
    if err == "invalid_window":
        valid = scheduling._norm(cfg)["windows"]
        raise ActionError(f"'{new_window}' isn't a bookable time here — this diary "
                          f"takes {', '.join(valid[:8])}.")
    if err:
        raise ActionError(f"{new_date} isn't bookable ({err.replace('_', ' ')})."
                          + _alternatives(tenant_id, new_date, client))
    # Advisory availability read now; the atomic RPC re-decides at Confirm.
    if not any(o["date"] == new_date and o["window"] == new_window
               for o in scheduling.check_availability(
                   tenant_id, new_date, new_date, client=client)):
        raise ActionError(f"{new_date} {new_window} is already taken."
                          + _alternatives(tenant_id, new_date, client))
    payload = {"booking_id": booking_id,
               "old_date": str(booking.get("booking_date")),
               "old_window": booking.get("window"),
               "new_date": new_date, "new_window": new_window,
               "contact": booking.get("contact") or "",
               "booking_ref": booking.get("booking_ref") or "",
               "what": booking.get("summary") or "booking"}
    who = payload["contact"] or payload["what"]
    summary = (f"Move {who}'s booking: {payload['old_date']} "
               f"({payload['old_window']}) → {new_date} ({new_window})")
    details = [f"Current: {payload['old_date']} — {payload['old_window']}",
               f"New: {new_date} — {new_window}"]
    return summary, payload, details, None


_WINDOW_WORDS = {"am": "am", "morning": "am", "pm": "pm", "afternoon": "pm",
                 "day": "day", "all day": "day", "anytime": "day", "any": "day"}


def _is_services_diary(cfg: dict | None) -> bool:
    """A diary whose bookings sit on the slot grid (windows='day' + a service
    menu): 'afternoon' means a start time, not a half-day bucket."""
    return bool(cfg) and str(cfg.get("windows") or "day") == "day"         and bool(cfg.get("service_types"))


def _stage_timed_move(tenant_id, client, cfg, booking, new_date, new_window):
    """Live 2026-09-06 (Brightwell, pass 1 AND 2): 'move Margaret to Wednesday
    afternoon' bounced with "'pm' isn't a bookable time here — this diary
    takes day" because the stager only knew the legacy am/pm/day buckets. A
    services diary places bookings on the grid: resolve the owner's word to
    the earliest start that fits (capacity, the gap between jobs, the notice
    rule) — the same rule as the email-side move tool — and carry the block
    so Confirm moves the booking in place."""
    from app import slotgrid
    w = str(new_window or "").strip().lower()
    w = _WINDOW_WORDS.get(w, w)
    if ":" not in w and w not in ("am", "pm", "day"):
        raise ActionError(f"'{new_window}' isn't a time I can place — say morning, "
                          "afternoon, or a time like 14:00.")
    merged = scheduling._merged(cfg)
    length = int(booking.get("length_blocks") or 0) or slotgrid.blocks_for_minutes(
        int(merged.get("slot_minutes") or 60))
    buf = int(merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    day = scheduling._as_date(new_date)
    booked = [b for b in db.list_active_bookings(
        tenant_id, (day - timedelta(days=1)).isoformat(), day.isoformat(), client=client)
        if b.get("id") != booking["id"]]           # its own old slot never blocks it
    # now=None: the customer-facing notice rule does not bind the OWNER moving
    # their own diary (first_fit_start's own contract: "a human adding to their
    # own diary") — capacity and the gap between jobs still do.
    sb = scheduling.first_fit_start(
        cfg, booked, length, day, w, None,
        capacity=int(merged.get("capacity_per_window") or 0), buffer_blocks=buffer_blocks)
    if sb is None:
        part = {"am": "morning", "pm": "afternoon", "day": "day"}.get(w, f"slot at {w}")
        hours = length * slotgrid.BLOCK_MIN / 60
        raise ActionError(f"{new_date} has no room in the {part} for a {hours:g}-hour visit "
                          "(capacity or the gap between jobs)."
                          + _alternatives(tenant_id, new_date, client))
    payload = {"booking_id": booking["id"],
               "old_date": str(booking.get("booking_date")),
               "old_window": booking.get("window"),
               "new_date": new_date, "new_window": w,
               "new_start_block": int(sb), "length_blocks": int(length), "timed": True,
               "contact": booking.get("contact") or "",
               "booking_ref": booking.get("booking_ref") or "",
               "what": booking.get("summary") or "booking"}
    from app import dates as dates_mod
    who = payload["what"] if payload["what"] != "booking" else f"{payload['contact']}'s booking"
    at = slotgrid.hhmm(int(sb))
    old_pretty = dates_mod.pretty_date(payload["old_date"])
    new_pretty = dates_mod.pretty_date(new_date)
    summary = f"Move {who}: {old_pretty} ({payload['old_window']}) → {new_pretty} at {at}"
    details = [f"Current: {old_pretty} — {payload['old_window']}",
               f"New: {new_pretty} — arriving {at} ({hours_label(length)})"]
    return summary, payload, details, None


def hours_label(length_blocks: int) -> str:
    from app import slotgrid
    h = length_blocks * slotgrid.BLOCK_MIN / 60
    return f"{h:g} hour{'s' if h != 1 else ''}"


def _resolve_booking(tenant_id, client, raw: str) -> dict | None:
    """A booking by row id or by its human reference (BK-1004)."""
    raw = (raw or "").strip()
    if _UUID_RE.match(raw):
        return db.get_booking(raw, tenant_id, client=client)
    if raw:
        return db.get_booking_by_ref(tenant_id, raw, client=client)
    return None


def _series_after(tenant_id, client, booking: dict) -> list[dict]:
    """The later visits in this booking's standing series (same recurrence_id,
    later date, still live), soonest first."""
    rid = booking.get("recurrence_id")
    if not rid:
        return []
    try:
        rows = ((client or db.db()).table("bookings")
                .select("*")
                .eq("tenant_id", tenant_id).eq("recurrence_id", rid)
                .in_("status", ["proposed", "confirmed"])
                .gt("booking_date", str(booking.get("booking_date")))
                .order("booking_date").limit(200).execute())
        return res_rows(rows)
    except Exception:
        log.warning("series lookup failed (booking=%s)", booking.get("id"), exc_info=True)
        return []


def res_rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _stage_cancel_booking(tenant_id, client, action):
    """Pass 1 + 2 on Brightwell (2026-09-06): "Kris is away next Tuesday, skip
    that visit but keep the fortnightly going" had no kind — the reply could
    only send the owner to the diary. Cancel ONE future visit (skip-and-keep
    for a series) or the visit and everything after it in its series; the
    customer's note rides the hand-cancel path (approval queue), never sent
    from here."""
    from app import dates as dates_mod, slotgrid
    booking = _resolve_booking(tenant_id, client, _clean(action, "booking_id", 64))
    if not booking:
        raise ActionError("I couldn't pin down which booking to cancel — could you "
                          "say which one (name or day)?")
    if booking.get("status") not in ("proposed", "confirmed"):
        raise ActionError("That booking isn't in the live diary any more — nothing to cancel.")
    day = str(booking.get("booking_date") or "")[:10]
    if day and day < datetime.now(scheduling.DIARY_TZ).date().isoformat():
        raise ActionError(f"{booking.get('booking_ref') or 'That booking'} was on "
                          f"{dates_mod.pretty_date(day)} — it has already passed, so there "
                          "is nothing to cancel (the register decides what happened).")
    scope = _clean(action, "scope", 20).lower() or "one"
    if scope not in ("one", "series_from"):
        scope = "one"
    notify_raw = _clean(action, "notify", 10).lower()
    notify = notify_raw not in ("no", "false", "0", "silent")
    reason = _clean(action, "reason", 200)
    later = _series_after(tenant_id, client, booking)
    rows = [booking] + (later if scope == "series_from" else [])
    ref = booking.get("booking_ref") or "the booking"
    what = booking.get("summary") or "booking"
    when = dates_mod.pretty_date(day)
    sb = booking.get("start_block")
    if sb is not None:
        when += f" at {slotgrid.hhmm(int(sb))}"
    elif booking.get("window"):
        when += f" ({booking.get('window')})"
    if scope == "series_from":
        summary = (f"Cancel {ref} — {what}, {when}, and the {len(later)} later "
                   f"visit{'s' if len(later) != 1 else ''} in the series")
    else:
        summary = f"Cancel {ref} — {what}, {when}"
    details = [f"Visit: {ref} on {when}"]
    if scope == "series_from":
        details.append("The series ENDS: " + (", ".join(
            dates_mod.pretty_date(r.get("booking_date")) for r in later[:6])
            + (" …" if len(later) > 6 else "") if later else "no later visits found"))
    elif later:
        details.append(f"The series keeps going — next visit "
                       f"{dates_mod.pretty_date(later[0].get('booking_date'))} "
                       f"({later[0].get('booking_ref')}), {len(later)} more booked")
    elif booking.get("recurrence_id"):
        details.append("This is the last booked visit of its series")
    details.append("Customer: " + ("a short note goes to your approval queue — nothing sends "
                                   "until you approve it" if notify and booking.get("contact")
                                   else "no message"))
    if reason:
        details.append(f"Reason: {reason}")
    details.append("No charge to the customer (a business cancellation is excused on the register)")
    payload = {"booking_id": booking["id"], "booking_ref": ref, "what": what, "date": day,
               "when": when, "scope": scope, "notify": bool(notify and booking.get("contact")),
               "reason": reason, "contact": booking.get("contact") or "",
               "later_ids": [r["id"] for r in later] if scope == "series_from" else [],
               "series_next": (later[0].get("booking_date") if later and scope == "one" else None)}
    return summary, payload, details, None


def _stage_confirm_booking(tenant_id, client, action):
    """Confirm a HELD (proposed) booking, optionally followed by the email that
    says so (docs/plan-concierge-confirm-first.md). A held time waits on the
    business, so when an email would promise it, the owner confirms first and
    the email follows: one card, one yes. The email step rides the payload:
    `draft_text` (a typed draft, re-checked after the confirm) or
    `instruction` (a new email the Drafting agent writes after the confirm)."""
    from app import dates as dates_mod, slotgrid
    booking = _resolve_booking(tenant_id, client, _clean(action, "booking_id", 64))
    if not booking:
        raise ActionError("I couldn't pin down which booking to confirm - could you "
                          "say which one (name or day)?")
    ref = booking.get("booking_ref") or "That booking"
    status = str(booking.get("status") or "").lower()
    if status == "confirmed":
        raise ActionError(f"{ref} is already confirmed.")
    if status == "offered":
        raise ActionError(f"{ref} is only offered - the customer hasn't picked a time yet, "
                          "so there is nothing to confirm.")
    if status != "proposed":
        raise ActionError(f"{ref} isn't a held booking any more (it is {status or 'not live'}) "
                          "- nothing to confirm.")
    day = str(booking.get("booking_date") or "")[:10]
    if day and day < datetime.now(scheduling.DIARY_TZ).date().isoformat():
        raise ActionError(f"{ref} was on {dates_mod.pretty_date(day)}, which has passed - it "
                          "needs moving to a new date, not confirming.")
    when = dates_mod.pretty_date(day)
    sb = booking.get("start_block")
    if sb is not None:
        when += f" at {slotgrid.hhmm(int(sb))}"
    elif booking.get("window"):
        when += f" ({booking.get('window')})"
    what = booking.get("summary") or "booking"
    contact = _clean(action, "contact", 254).lower() or str(booking.get("contact") or "").lower()
    draft_text = str((action or {}).get("draft_text") or "").strip()[:8000]
    instruction = _clean(action, "instruction", 1500)
    then = None
    if "@" in contact and (draft_text or instruction):
        then = {"contact": contact, "instruction": instruction}
        if draft_text:
            rows = db.get_conversation(tenant_id, contact, client=client) or []
            if not rows:
                raise ActionError(f"I couldn't find a conversation with {contact}.")
            then.update(log_id=rows[-1]["id"], subject=rows[-1].get("subject") or "")
    summary = f"Confirm {ref} - {what}, {when}" + (f" and email {contact}" if then else "")
    details = [f"Booking: {ref} on {when}, now held - confirming puts it in the diary as booked"]
    if then:
        details.append("Then the email goes to your approval queue - nothing sends until you "
                       "approve it, and if the confirm fails no email is queued")
    payload = {"booking_id": booking["id"], "booking_ref": ref, "when": when, "then": then}
    if then and draft_text:
        payload["draft_text"] = draft_text
    return summary, payload, details, (draft_text or None)


def _stage_block_date(tenant_id, client, action):
    """"Block out Friday 18th, all three cleaners are on the first-aid course"
    (pass 1 + 2, 2026-09-06): the reply could only point at diary settings and
    never checked what was booked. The card closes the day(s) as blackout
    dates AND lists the bookings already on them — they stay as they are;
    the owner moves or cancels each one (its own card)."""
    from app import dates as dates_mod
    first = _require_date(_clean(action, "new_date", 10), "the day to block")
    last_raw = _clean(action, "end_date", 10)
    last = _require_date(last_raw, "the last day") if last_raw else first
    if last < first:
        first, last = last, first
    today = datetime.now(scheduling.DIARY_TZ).date().isoformat()
    if last < today:
        raise ActionError(f"{dates_mod.pretty_date(first)} has already passed — nothing to block.")
    span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days + 1
    if span > 31:
        raise ActionError("That's more than a month of days — block up to 31 at a time "
                          "(or set the working days in Diary settings instead).")
    days = [(datetime.fromisoformat(first) + timedelta(days=i)).date().isoformat()
            for i in range(span)]
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    already = {str(d)[:10] for d in (cfg.get("blackout_dates") or [])}
    new_days = [d for d in days if d not in already]
    if not new_days:
        raise ActionError(("That day is" if span == 1 else "Those days are")
                          + " already blocked in the diary — nothing to change.")
    stranded = [b for b in (db.list_active_bookings(tenant_id, first, last, client=client) or [])
                if b.get("status") in ("proposed", "confirmed")]
    # …and stays that began BEFORE the first day and are still running on it
    # (test run 5.1, Kris 2026-09-24: the Concierge said Rufus's stay runs
    # through the 11th while the card said "Nothing is booked on those days"
    # — the card only looked at bookings that START on the day).
    from app import record_edits
    seen = {b.get("id") for b in stranded}
    for b in db.stays_running_into(tenant_id, first, client=client) or []:
        times = record_edits.stay_times(b)
        if b.get("id") not in seen and times and times[1].date().isoformat() >= first:
            stranded.append(b)
            seen.add(b.get("id"))
    reason = _clean(action, "reason", 200)
    when = (dates_mod.pretty_date(first) if span == 1
            else f"{dates_mod.pretty_date(first)} to {dates_mod.pretty_date(last)} ({span} days)")
    summary = f"Block the diary on {when}"
    details = [f"Closed: {when} — nothing can be booked" + (
        f" ({len(already & set(days))} of those days already were)" if already & set(days) else "")]
    if stranded:
        refs = []
        for b in stranded[:8]:
            row = db.get_booking(b["id"], tenant_id, client=client) or b
            refs.append(f"{row.get('booking_ref') or '?'} — {row.get('summary') or 'booking'} "
                        f"({dates_mod.pretty_date(row.get('booking_date'))})")
        details.append(f"Already booked on those days — {len(stranded)} booking"
                       f"{'s' if len(stranded) != 1 else ''} stay as they are until you move "
                       "or cancel each one: " + "; ".join(refs)
                       + (" …" if len(stranded) > 8 else ""))
    else:
        details.append("Nothing is booked on those days")
    if reason:
        details.append(f"Reason: {reason}")
    payload = {"days": new_days, "first": first, "last": last, "when": when,
               "stranded": len(stranded), "reason": reason}
    return summary, payload, details, None


def _stage_unblock_date(tenant_id, client, action):
    """"Unblock the 11th" (test run 5, Kris 2026-09-24): reopen days the
    diary has blocked. The card says which of the days asked for were blocked
    — the rest were open already and stay as they are. Confirm takes them out
    of the same blackout list block_date and Diary settings write."""
    from app import dates as dates_mod
    first = _require_date(_clean(action, "new_date", 10), "the day to reopen")
    last_raw = _clean(action, "end_date", 10)
    last = _require_date(last_raw, "the last day") if last_raw else first
    if last < first:
        first, last = last, first
    span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days + 1
    if span > 62:
        raise ActionError("That's more than two months of days — reopen up to 62 at a time "
                          "(or change them in Diary settings).")
    days = [(datetime.fromisoformat(first) + timedelta(days=i)).date().isoformat()
            for i in range(span)]
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    blocked = {str(d)[:10] for d in (cfg.get("blackout_dates") or [])}
    reopen = [d for d in days if d in blocked]
    when = (dates_mod.pretty_date(first) if span == 1
            else f"{dates_mod.pretty_date(first)} to {dates_mod.pretty_date(last)} ({span} days)")
    if not reopen:
        raise ActionError(("That day isn't" if span == 1 else "None of those days are")
                          + " blocked in the diary — nothing to reopen.")
    shown = (dates_mod.pretty_date(reopen[0]) if len(reopen) == 1
             else f"{dates_mod.pretty_date(reopen[0])} to {dates_mod.pretty_date(reopen[-1])}")
    summary = f"Reopen the diary on {shown}" + (f" ({len(reopen)} days)" if len(reopen) > 1 else "")
    details = [f"Open again: {len(reopen)} day{'s' if len(reopen) != 1 else ''} — bookings can be taken"]
    if len(reopen) < span:
        details.append(f"{span - len(reopen)} of the days asked for weren't blocked — left as they are")
    payload = {"days": reopen, "first": reopen[0], "last": reopen[-1], "when": shown}
    return summary, payload, details, None


def _stage_assign_person(tenant_id, client, action):
    """"Delegate the next drop-off to Em" (Kris 2026-09-24, plan step 2): put
    one person from the team — with or without a login — on one booking. The
    card says what changes for them, and who comes off it when the business
    works one person per job."""
    from app import dates as dates_mod, job_people
    booking = _resolve_booking(tenant_id, client, _clean(action, "booking_id", 64))
    if not booking:
        raise ActionError("I couldn't pin down which booking — could you say which one "
                          "(name or day)?")
    if booking.get("status") not in ("proposed", "confirmed"):
        raise ActionError("That booking isn't in the live diary any more.")
    roster = job_people.team(tenant_id)
    # The name under whichever key the model chose (live 2026-09-24: the card
    # came back "nobody called ''" — the name was there, under another key).
    asked = next((v for v in (_clean(action, k, 80) for k in
                              ("person", "name", "person_name", "assignee", "staff",
                               "who", "to", "team_member")) if v), "")
    t = job_people.find_person(roster, asked)
    if not t:
        raise ActionError((f"There's nobody called '{asked}' on the team" if asked
                           else "I didn't catch who to put on it")
                          + " — it's " + ", ".join(p["name"] for p in roster) + ".")
    on = job_people.by_booking(tenant_id, [booking["id"]], roster).get(str(booking["id"]), [])
    if any(p["person_id"] == t["id"] for p in on):
        raise ActionError(f"{t['name']} is already on that job.")
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    part = _clean(action, "part", 60)
    day = str(booking.get("booking_date") or "")[:10]
    what = booking.get("summary") or "the booking"
    ref = booking.get("booking_ref") or ""
    summary = f"Put {t['name']} on {what}" + (f" ({ref})" if ref else "") \
        + (f", {dates_mod.pretty_date(day)}" if day else "")
    # Job sheets (src/app/job_sheets.py): a freelancer or helper is sent the job.
    details = [f"{t['name']} sees it on their Today and Diary" if t["has_login"]
               else f"{t['name']} is sent it as a job sheet, by text or email" if (t.get("phone") or t.get("email"))
               else f"{t['name']} has no mobile or email yet — add one in People to send the job sheet"]
    if part:
        details.append(f"Their part: {part}")
    if on and job_people.mode(cfg) == job_people.ONE:
        details.append("Takes " + ", ".join(p["name"] for p in on) + " off it (one person per job)")
    elif on:
        details.append("Already on it: " + ", ".join(p["name"] for p in on))
    payload = {"booking_id": booking["id"], "person_id": t["id"], "name": t["name"],
               "part": part, "ref": ref, "what": what, "day": day}
    return summary, payload, details, None


def _find_contact_field(defs: list[dict], label: str) -> dict | None:
    want = " ".join((label or "").lower().split())
    if not want:
        return None
    for d in defs:
        if d.get("entity") != "contact":
            continue
        if " ".join(str(d.get("label") or "").lower().split()) == want or str(d.get("key") or "").lower() == want:
            return d
    for d in defs:      # a loose match on words, second
        if d.get("entity") == "contact" and want in str(d.get("label") or "").lower():
            return d
    return None


def _stage_update_dependant(tenant_id, client, action, member=None):
    """"Bella's on 350g a day now, update her record" (live 2026-09-23): a
    dependant's details (a dog's food, breed, jab date) live on the dependant,
    not the owner's record — update_contact aimed them at a customer field
    that doesn't exist. name = the dependant, title = its detail, content = the
    new value, contact = the owner when the name alone is ambiguous. Confirm
    runs record_edits.save_dependant — the contact card's form's own path."""
    from app import record_edits
    name = " ".join(_clean(action, "name", 80).split())
    if not name:
        raise ActionError("Which one — give me the name (e.g. 'Bella').")
    label = _clean(action, "title", 80)
    if not label:
        raise ActionError(f"Which of {name}'s details should change?")
    inst, group, contact = _find_dependant(tenant_id, client, name, _clean(action, "contact", 254),
                                           member=member)
    try:
        field, value = record_edits.plan_dependant_field(group, inst, label, _clean(action, "content", 600))
    except record_edits.EditError as e:
        raise ActionError(str(e))
    _require_visible_field(member, field)
    old = (inst.get("data") or {}).get(field["key"])
    who_name = contact.get("name") or contact.get("email") or "their owner"
    old_s, new_s = record_edits._dep_shown(old), record_edits._dep_shown(value)
    summary = f"Update {name} ({who_name}) — {field.get('label')}: {old_s} → {new_s}"
    details = [f"{group.get('label') or 'Record'}: {name}, on {who_name}'s record",
               f"{field.get('label')} now: {old_s}", f"New: {new_s}",
               "The timeline keeps a line saying what changed"]
    if isinstance(field.get("ladder"), dict):
        details.append("This is a level — moving it sends the level message as that ladder is set up")
    payload = {"instance_id": inst.get("id"), "contact_id": inst.get("contact_id"),
               "group_id": group.get("id"), "name": name, "owner": who_name,
               "key": field["key"], "label": field.get("label"), "value": value, "shown": new_s}
    return summary, payload, details, None


def _require_visible_field(member, field: dict | None) -> None:
    """A field this person can't see is one they can't change either — and the
    card never shows its value, old or new (M36)."""
    from app import crm_visibility
    if member is not None and not crm_visibility.can_see_field(member, field):
        raise ActionError(PRIVATE_FIELD)


def _find_dependant(tenant_id, client, name, who="", member=None):
    """(instance, group, owner contact) for a dependant named `name` — scoped
    to the owner `who` when given. Two with the name ask whose; none says so.
    A person narrowed to their own customers only ever finds theirs (M36)."""
    from app import requirement_groups as rgroups
    owner = None
    if who:
        from app import concierge_reads
        owner, candidates = concierge_reads._resolve_contact(tenant_id, client, who)
        if owner is None and candidates:
            raise ActionError("Several customers match — which one: "
                              + ", ".join(f"{c.get('name')} ({c.get('email')})" for c in candidates[:5]) + "?")
    rows = (db.list_requirement_instances(tenant_id, owner["id"], client=client) if owner
            else db.list_requirement_instances_all(tenant_id, client=client)) or []
    matches = [r for r in rows if str(r.get("name") or "").strip().lower() == name.lower()]
    from app import crm_visibility
    if member is not None and crm_visibility.narrows_contacts(member):
        if owner is not None:
            _require_their_customer(tenant_id, client, member, owner)
        else:
            try:
                from app import concierge_data
                allowed = concierge_data.allowed_customers(tenant_id, client, member)
            except Exception:
                raise ActionError(NOT_THEIR_CUSTOMER)
            matches = [r for r in matches if crm_visibility.may_open(
                db.crm_get_contact(r.get("contact_id"), tenant_id, client=client) or {},
                member, allowed)]
    if not matches:
        raise ActionError(f"I can't find {name} on " + (f"{owner.get('name') or owner.get('email')}'s record"
                                                         if owner else "anyone's record") + ".")
    if len(matches) > 1:
        owners = []
        for r in matches[:5]:
            c = db.crm_get_contact(r.get("contact_id"), tenant_id, client=client) or {}
            owners.append(c.get("name") or c.get("email") or "?")
        raise ActionError(f"There's more than one {name} — whose: {', '.join(owners)}?")
    inst = matches[0]
    groups = rgroups.clean_groups(db.list_requirement_groups(tenant_id, client=client))
    group = next((g for g in groups if g.get("id") == inst.get("group_id")), None)
    if not group:
        raise ActionError(f"{name}'s kind of record no longer exists — nothing to change.")
    contact = owner or db.crm_get_contact(inst.get("contact_id"), tenant_id, client=client) or {}
    return inst, group, contact


def _stage_request_renewal(tenant_id, client, action, member=None):
    """"Ask Hannah for Bella's up-to-date vaccination card" (Kris 2026-09-23:
    "monitor/check/remind for expiring documents"): one email to the owner
    asking for the renewed record, written by the model into draft_text and
    editable on the card; Confirm sends it from the business's mailbox (the
    card is the approval, as with a payment reminder). name = the dependant,
    title = the expiring detail (optional when it has only one)."""
    from app import dates as dates_mod
    name = " ".join(_clean(action, "name", 80).split())
    if not name:
        raise ActionError("Whose record — give me the name (e.g. 'Bella').")
    inst, group, contact = _find_dependant(tenant_id, client, name, _clean(action, "contact", 254),
                                           member=member)
    expiring = [f for f in group.get("fields") or [] if f.get("durability") == "expires"]
    if member is not None:
        # a date they can't see is not one they can quote back (M36)
        from app import crm_visibility
        expiring = [f for f in expiring if crm_visibility.can_see_field(member, f)]
    label = _clean(action, "title", 80)
    field = None
    if label:
        want = label.lower()
        field = next((f for f in expiring if str(f.get("label") or "").lower() == want
                      or str(f.get("key") or "").lower() == want), None) \
            or next((f for f in expiring if want in str(f.get("label") or "").lower()
                     or str(f.get("label") or "").lower() in want), None)
    elif len(expiring) == 1:
        field = expiring[0]
    if not field:
        names = ", ".join(str(f.get("label")) for f in expiring) or "none"
        raise ActionError(f"Which date should they renew? {name}'s dates that run out: {names}.")
    to = (contact.get("email") or "").strip()
    if not to:
        raise ActionError(f"{contact.get('name') or 'The owner'} has no email address to ask.")
    on_file = (inst.get("data") or {}).get(field["key"])
    when = dates_mod.pretty_date(str(on_file)) if on_file else None
    who_name = contact.get("name") or to
    label_l = str(field.get("label") or "record")
    draft_text = str((action or {}).get("draft_text") or "").strip()[:8000]
    if not draft_text:
        from app import invoice_config, invoice_send as invsend
        cfg = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
        # The business's own sign-off when it has one, as the invoice and quote
        # emails do: a name that ends in "." or runs long is then never mistaken
        # for a second closing.
        closing = (invsend.configured_sign_off(tenant_id, client=client)
                   or f"Thank you,\n{cfg.get('business_name') or 'us'}")
        draft_text = (f"Hi {who_name.split()[0]},\n\nCould you send us {name}'s up-to-date "
                      f"{label_l.lower()} when you get a moment? "
                      + (f"The one we have on file runs out on {when}. " if when else "")
                      + "A photo of the card or a PDF is perfect — just reply to this email."
                      + f"\n\n{closing}")
    draft_text = _signed(tenant_id, client, draft_text)
    subject = f"{name}'s {label_l.lower()} — could you send the latest?"
    summary = f"Ask {who_name} for {name}'s up-to-date {label_l}" + (f" (on file: {when})" if when else "")
    # Just who it goes to (Kris 2026-09-24: the "sends from your mailbox" /
    # "when they reply" lines were clutter — the card says what it does).
    details = [f"To {to}"]
    payload = {"instance_id": inst.get("id"), "contact_id": inst.get("contact_id"), "to": to,
               "name": name, "owner": who_name, "label": label_l, "subject": subject,
               "draft_text": draft_text}
    return summary, payload, details, draft_text


def _find_stay(tenant_id, client, action):
    """The stay a change_stay action means: by booking id / BK ref, or by the
    dependant's name ("extend Max's stay") — the soonest live stay of that
    dependant, or of that owner. Raises ActionError with the question to ask."""
    from app import record_edits
    booking_id = _clean(action, "booking_id", 64)
    if booking_id:
        b = (db.get_booking(booking_id, tenant_id, client=client) if _UUID_RE.match(booking_id)
             else db.get_booking_by_ref(tenant_id, booking_id, client=client))
        if not b:
            raise ActionError("I couldn't pin down which stay — could you say whose, or its reference?")
        return b
    name = " ".join(_clean(action, "name", 80).split())
    who = _clean(action, "contact", 254)
    if not name and not who:
        raise ActionError("Whose stay — give me the dependant's name or the customer.")
    inst, contact = None, None
    if name:
        inst, _group, contact = _find_dependant(tenant_id, client, name, who)
    else:
        from app import concierge_reads
        contact, candidates = concierge_reads._resolve_contact(tenant_id, client, who)
        if contact is None:
            raise ActionError("Several customers match — which one: "
                              + ", ".join(f"{c.get('name')} ({c.get('email')})" for c in candidates[:5]) + "?"
                              if candidates else "I couldn't find that customer.")
    email = (contact or {}).get("email") or ""
    rows = [b for b in (db.bookings_for_contact(tenant_id, email, client=client) or [])
            if b.get("status") in ("proposed", "confirmed") and record_edits.stay_times(b)]
    if inst:
        linked = db.subjects_for_bookings(tenant_id, [b["id"] for b in rows], client=client)
        mine = [b for b in rows if any(i.get("id") == inst.get("id") for i in linked.get(b["id"], []))]
        if not mine:
            mine = [b for b in rows if name.lower() in str(b.get("summary") or "").lower()]
        rows = mine
    now = datetime.now(scheduling.DIARY_TZ).replace(tzinfo=None)
    rows = [b for b in rows if record_edits.stay_times(b)[1] >= now]
    if not rows:
        raise ActionError(f"{name or (contact or {}).get('name') or 'They'} " +
                          ("has" if name else "have") + " no stay in the diary from today on.")
    rows.sort(key=lambda b: record_edits.stay_times(b)[0])
    # bookings_for_contact is a trimmed read (no quoted_amount, no details):
    # the plan prices and re-words from the FULL row (live 2026-09-23: the
    # card read "Price: £488.00" with no old price, and no invoice line).
    return db.get_booking(rows[0]["id"], tenant_id, client=client) or rows[0]


def _stage_change_stay(tenant_id, client, action):
    """"Extend Max's stay by two nights" / "Bella's going home on the 20th
    instead" / "move Mabel's stay to the following week" (docs/
    plan-stay-date-changes.md). new_date = the new drop-off, end_date = the
    new collection (a day, or day + time); either may be left out. The plan
    is record_edits.plan_stay_change — the job page's own form runs the same
    checks — so the card shows the new run, the nights, the room, the price
    and what happens to an invoice; Confirm re-checks and writes."""
    from app import dates as dates_mod, record_edits
    booking = _find_stay(tenant_id, client, action)
    new_arrival = _clean(action, "new_date", 16) or None
    new_departure = _clean(action, "end_date", 16) or None
    if not new_arrival and not new_departure:
        raise ActionError("What should change — the new collection day, the new drop-off, or both?")
    try:
        plan = record_edits.plan_stay_change(tenant_id, booking, new_arrival=new_arrival,
                                             new_departure=new_departure, client=client)
    except record_edits.EditError as e:
        raise ActionError(str(e))
    what = booking.get("summary") or f"{booking.get('contact')}'s stay"
    ref = booking.get("booking_ref") or ""
    verb = {"extend": "Extend", "shorten": "Shorten", "move": "Move"}[plan["kind"]]
    delta = plan["delta_nights"]
    nights_words = (f"{'+' if delta > 0 else ''}{delta} night{'s' if abs(delta) != 1 else ''}, "
                    if delta else "") + f"{plan['nights']} in all"
    summary = f"{verb} {what} ({ref}): {plan['old_when']} → {plan['when']}"
    details = [f"Collection: {plan['old_departure'][:16].replace('T', ' ')} → {plan['departure'][:16].replace('T', ' ')}"
               if plan["kind"] != "move" else f"Now: {plan['old_when']}",
               f"Nights: {nights_words}",
               "Room: every night fits" + (f" in {plan['resource']}" if plan.get("resource") else "")]
    if plan.get("price_new") is not None:
        if plan.get("invoiced") and plan.get("extra_amount"):
            details.append(f"Price: £{plan['price_old']:,.2f} → £{plan['price_new']:,.2f} — already invoiced, so a "
                           f"draft invoice for the extra £{plan['extra_amount']:,.2f} is created")
        elif plan.get("invoiced") and delta < 0:
            details.append(f"Price: £{plan['price_old']:,.2f} → £{plan['price_new']:,.2f} — already invoiced; "
                           "adjust that invoice by hand if you're refunding")
        elif plan.get("price_old") is not None:
            details.append(f"Price: £{plan['price_old']:,.2f} → £{plan['price_new']:,.2f}")
        else:
            details.append(f"Price: £{plan['price_new']:,.2f}")
    notify = _clean(action, "notify", 5).lower() in ("yes", "true", "1")
    details.append("Customer: " + ("a short note goes to your approval queue — nothing sends until you approve it"
                                   if notify and booking.get("contact") else "no message"))
    payload = {"booking_id": booking["id"], "booking_ref": ref, "what": what, **plan,
               "contact": booking.get("contact") or "", "notify": bool(notify and booking.get("contact"))}
    return summary, payload, details, None


_BACKGROUND: set = set()


def _background(coro, what: str) -> None:
    """Run follow-up work (a customer note) after the owner's answer has gone
    back; failures are logged, never raised into a finished action."""
    async def run():
        try:
            await coro
        except Exception:
            log.warning("%s failed", what, exc_info=True)
    task = asyncio.create_task(run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _stay_note_needed(tenant_id, client, booking, plan) -> bool:
    """Retire the stay's notes still waiting, and say whether a new one is
    worth writing: not when this change puts the dates back to what the
    customer was last told (the unsent note it retires started from them -
    test run 3.2, 24 Sep: "back to the 26th" for a customer who never heard
    of the 27th). Quick (two DB calls), so "Done" can say it truthfully."""
    subject = f"Your stay — new dates ({booking.get('booking_ref') or ''})".strip()
    retired = db.retire_pending_owner_messages(tenant_id, booking.get("contact"), subject, client=client)
    return not any(f"was {plan['when']};" in (r.get("body_in") or "") for r in retired)


async def _queue_stay_note(tenant_id, client, booking, plan):
    """A short note to the customer about the new dates, into the approval
    queue (never sent from here) — the same door the hand-cancel note uses."""
    from app import agent, usage
    contact = booking.get("contact")
    subject = f"Your stay — new dates ({booking.get('booking_ref') or ''})".strip()
    # Earlier waiting notes were retired by _stay_note_needed before this ran.
    cfg = db.effective_config(tenant_id, None, client=client)
    facts = (f"The customer: {contact}. Their stay {booking.get('booking_ref') or ''} "
             f"({booking.get('summary') or 'stay'}) was {plan['old_when']}; it is now {plan['when']} "
             f"({plan['nights']} nights). Say the new dates plainly and ask them to shout if anything is wrong."
             + (f" The price is now £{plan['price_new']:,.2f}." if plan.get("price_new") is not None else ""))
    result = await agent.compose_outbound(cfg, "A short note from the business telling the customer their "
                                          "stay's dates have changed — the new drop-off and collection, "
                                          "nothing more.", facts, tenant_id=tenant_id,
                                          log_operation="concierge_email", log_module="concierge")
    text = (result.text or "").strip()
    if not text:
        return False
    log_id, _ = db.insert_email_log(tenant_id, contact, subject,
                                    f"[stay change] was {plan['old_when']}; now {plan['when']}", text, status="pending", kind="owner_message",
                                    category="draft_ready", category_reason="The Concierge changed the stay's dates")
    usage.record_anthropic(tenant_id, result.model, result.usage, operation="concierge_email",
                           email_log_id=log_id, module="concierge")
    return True


def _stage_update_contact(tenant_id, client, action, member=None):
    """"Add a note to Sophie's record that she wants us to knock twice" (pass
    1 + 2, 2026-09-06): the reply sent the owner to a contact page that has
    no hand-edit for fields and no notes box. A field change (by its label)
    merges into the record's custom_data on Confirm — the human's confirmed
    edit — and a note lands on the customer's timeline, dated. Health fields
    are refused (HEALTH_FIELD): they are changed on the customer page only."""
    from app import concierge_reads, crm_fields, crm_visibility
    contact, candidates = concierge_reads._resolve_contact(
        tenant_id, client, _clean(action, "contact", 254))
    if member is not None and crm_visibility.narrows_contacts(member):
        if contact is not None:
            _require_their_customer(tenant_id, client, member, contact)
        elif candidates:
            try:
                from app import concierge_data
                allowed = concierge_data.allowed_customers(tenant_id, client, member)
            except Exception:
                raise ActionError(NOT_THEIR_CUSTOMER)
            candidates = [c for c in candidates if crm_visibility.may_open(c, member, allowed)]
            if len(candidates) == 1:
                contact, candidates = candidates[0], []
            elif not candidates:
                raise ActionError(NOT_THEIR_CUSTOMER)
    if contact is None:
        if candidates:
            raise ActionError("Several contacts match — which one: "
                              + ", ".join(f"{c.get('name')} ({c.get('email')})" for c in candidates[:5]) + "?")
        raise ActionError("I couldn't find that customer — could you give their name or email?")
    content = _clean(action, "content", 600)
    if not content:
        raise ActionError("What should the record say? Give me the value or the note.")
    label = _clean(action, "title", 80)
    name = contact.get("name") or contact.get("email")
    if label:
        defs = db.crm_list_field_definitions(tenant_id, entity="contact", client=client) or []
        if member is not None:
            # A private field is not offered, matched or echoed (M36): asked for
            # by name it is refused without its value.
            seen = crm_visibility.visible_defs(defs, member)
            if _find_contact_field(seen, label) is None and \
                    _find_contact_field(defs, label) is not None:
                raise ActionError(PRIVATE_FIELD)
            defs = seen
        field = _find_contact_field(defs, label)
        if field and crm_fields.is_special_category(field):
            # Health details are typed on the customer page, never through the
            # assistant, so neither the old nor the new value reaches a model.
            raise ActionError(HEALTH_FIELD)
        if not field:
            names = ", ".join(str(d.get("label")) for d in defs if d.get("entity") == "contact")[:300]
            raise ActionError(f"There's no '{label}' field on the customer record — the fields are: "
                              f"{names or 'none yet'}. Say the field to set, or leave it as a note.")
        ok, value = crm_fields.coerce_value(field.get("field_type") or "text", content,
                                            field.get("options") or None)
        if not ok:
            opts = field.get("options") or []
            raise ActionError(f"'{content}' isn't a valid {field.get('label')}"
                              + (f" — choose one of: {', '.join(map(str, opts))}" if opts else
                                 f" (it takes a {field.get('field_type')})") + ".")
        old = (contact.get("custom_data") or {}).get(field.get("key"))
        old_shown = crm_fields.display_value(field, old)
        new_shown = crm_fields.display_value(field, value)
        if old not in (None, "") and str(old).strip() == str(value).strip():
            raise ActionError(f"{name}'s {field.get('label')} is already '{new_shown}' — nothing to change.")
        summary = f"Update {name} — {field.get('label')}: {old_shown} → {new_shown}"
        details = [f"Field: {field.get('label')}", f"Now: {old_shown}", f"New: {new_shown}"]
        if old not in (None, ""):
            details.append("The old value is replaced (the timeline keeps a line saying what changed)")
        payload = {"contact_id": contact.get("id"), "contact": contact.get("email") or "",
                   "name": name, "mode": "field", "key": field.get("key"),
                   "label": field.get("label"), "old": old_shown, "value": value,
                   "shown": new_shown}
        return summary, payload, details, None
    summary = f"Add a note to {name}'s record"
    details = [f"Note: {content}", "Dated today, on their timeline"]
    try:
        defs = db.crm_list_field_definitions(tenant_id, entity="contact", client=client) or []
        if member is not None:
            defs = crm_visibility.visible_defs(defs, member)
        words = set(content.lower().split())
        hint = next((d for d in defs if d.get("entity") == "contact"
                     and any(w in words for w in str(d.get("label") or "").lower().split() if len(w) > 3)), None)
        if hint:
            details.append(f"Tip: this may belong in the '{hint.get('label')}' field — say "
                           f"\"set {hint.get('label')} to …\" to change the field instead")
    except Exception:
        pass
    payload = {"contact_id": contact.get("id"), "contact": contact.get("email") or "",
               "name": name, "mode": "note", "content": content}
    return summary, payload, details, None


def _stage_update_deal_stage(tenant_id, client, action):
    deal_id = _clean(action, "deal_id", 64)
    new_stage = _clean(action, "new_stage", 60)
    if not _UUID_RE.match(deal_id):
        raise ActionError("I couldn't pin down which deal — could you name it?")
    deal = db.crm_get_deal(deal_id, tenant_id, client=client)
    if not deal:
        raise ActionError("I couldn't find that deal.")
    stages = db.crm_list_stages(tenant_id, client=client) or []
    stage_row = next((s for s in stages
                      if (s.get("name") or "").strip().lower() == new_stage.lower()), None)
    if not stage_row:
        names = ", ".join(s.get("name") or "?" for s in stages)
        raise ActionError(f"'{new_stage}' isn't one of your pipeline stages ({names}).")
    old_stage = (deal.get("crm_stages") or {}).get("name") or "?"
    if deal.get("stage_id") == stage_row["id"] or old_stage.lower() == new_stage.lower():
        raise ActionError(f"“{deal.get('title') or 'That deal'}” is already in "
                          f"{stage_row.get('name')} — nothing to change.")
    payload = {"deal_id": deal_id, "stage_id": stage_row["id"],
               "stage_name": stage_row.get("name"),
               "stage_type": stage_row.get("stage_type") or "active",
               "old_stage": old_stage, "title": deal.get("title") or "deal",
               "contact_id": (deal.get("crm_contacts") or {}).get("id") or ""}
    summary = f"{payload['title']}: {old_stage} → {payload['stage_name']}"
    return summary, payload, [f"Current stage: {old_stage}",
                              f"New stage: {payload['stage_name']}"], None


def _nice_date(iso: str) -> str:
    """2026-10-19 -> Monday 19 October 2026 (the ISO text when it is not a date)."""
    from datetime import datetime
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return iso
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def _stage_create_followup(tenant_id, client, action):
    from app import concierge_reads
    contact_id = _clean(action, "contact_id", 64)
    due_date = _require_date(_clean(action, "due_date", 10), "the follow-up")
    if _UUID_RE.match(contact_id):
        contact = db.crm_get_contact(contact_id, tenant_id, client=client)
        if not contact:
            raise ActionError("I couldn't find that contact.")
    else:
        # The brain names the person the way update_contact does (contact = their
        # email or name); an id only arrives when copied from search_contacts.
        # Live 2026-10-14: every follow-up was refused for want of a UUID.
        ref = _clean(action, "contact", 254) or _clean(action, "name", 120) or contact_id
        # crm_list_contacts builds a PostgREST filter from this text.
        ref = "".join(ch for ch in ref if ch not in "%,()*\\").strip()
        if not ref:
            raise ActionError("I couldn't pin down which contact — could you name them?")
        contact, candidates = concierge_reads._resolve_contact(tenant_id, client, ref)
        if contact is None:
            if candidates:
                raise ActionError("Several contacts match — which one: "
                                  + ", ".join(f"{c.get('name')} ({c.get('email')})" for c in candidates[:5]) + "?")
            raise ActionError("I couldn't find that contact — could you give their name or email?")
        contact_id = str(contact.get("id") or "")
    name = contact.get("name") or contact.get("email") or "them"
    title = _clean(action, "title", 120) or f"Follow up with {name}"
    payload = {"contact_id": contact_id, "contact_name": name,
               "title": title, "due_date": due_date}
    summary = f"Follow-up for {name} — due {due_date}"
    # Says where it will be kept (Kris 2026-10-16: the card only said the task and the date).
    return summary, payload, [f"Task: {title}", f"Due: {_nice_date(due_date)}",
                              f"Saved to: {name}'s record in the CRM, as a task"], None


def _stage_create_contact(tenant_id, client, action):
    name = _clean(action, "name", 120)
    email = _clean(action, "email", 254).lower()
    phone = _clean(action, "phone", 40)
    company = _clean(action, "company", 120)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ActionError("I need an email address to create a contact — "
                          "it's how BLAM8 recognises them in the inbox.")
    if db.crm_contact_by_email(tenant_id, email, client=client):
        raise ActionError(f"A contact with {email} already exists.")
    payload = {"name": name, "email": email, "phone": phone, "company": company}
    summary = f"New contact: {name or email}"
    details = [d for d in [f"Email: {email}", name and f"Name: {name}",
                           phone and f"Phone: {phone}",
                           company and f"Company: {company}"] if d]
    return summary, payload, details, None


def _stage_add_kb_entry(tenant_id, client, action):
    title = _clean(action, "title", 80)
    content = str((action or {}).get("content") or "").strip()[:6000]
    if not content:
        raise ActionError("I need the fact itself to add it to the knowledge base.")
    if not title:
        title = content[:60]
    if not kb.is_configured():
        raise ActionError("The knowledge base isn't available right now.")
    payload = {"title": title, "content": content}
    summary = f"New knowledge entry: {title}"
    return summary, payload, [content[:200] + ("…" if len(content) > 200 else "")], None


def _stage_define_dependants(tenant_id, client, action):
    """Dependants (module-requirement-groups.md Phase 3): prose → a staged kind.
    `title` = singular label, `name` = plural, `content` = one field per line
    'Label | required/optional | once/confirm/expires | gate/no gate | hint'."""
    from app import requirement_groups as rgroups
    label = _clean(action, "title", 60)
    plural = _clean(action, "name", 60)
    content = str((action or {}).get("content") or "").strip()[:4000]
    if not label:
        raise ActionError("I need the kind's name — the word you use for the subject of a booking.")
    fields = []
    for line in content.splitlines():
        parts = [p.strip() for p in line.strip(" -•\t").split("|")]
        if not parts or not parts[0]:
            continue
        req = "option" not in (parts[1].lower() if len(parts) > 1 else "required")
        dur = (parts[2].lower() if len(parts) > 2 else "once")
        dur = "confirm" if "confirm" in dur else ("expires" if "expir" in dur else "once")
        gate = len(parts) > 3 and parts[3].lower().strip() in ("gate", "gated", "yes", "true", "1")
        hint = parts[4] if len(parts) > 4 else ""
        fields.append({"label": parts[0], "required": req, "durability": dur, "gate": gate, "hint": hint,
                       "type": "date" if dur == "expires" else "text"})
    group = rgroups.clean_group({"label": label, "label_plural": plural, "cardinality": "many",
                                 "fields": fields})
    if not group:
        raise ActionError("I need at least one thing to know about each one — a field or two.")
    existing = {rgroups._norm(g["label"]): g for g in
                rgroups.clean_groups(db.list_requirement_groups(tenant_id, client=client))}
    if rgroups._norm(label) in existing:
        group["id"] = existing[rgroups._norm(label)].get("id")
    details = [f"{f['label']} — {'required' if f['required'] else 'optional'}, "
               + {"once": "ask once", "confirm": "confirm each time", "expires": "expires"}[f["durability"]]
               + (", gates the booking" if f.get("gate") else "")
               + (f" — {f['hint']}" if f.get("hint") else "") for f in group["fields"]]
    summary = (f"{'Update' if group.get('id') else 'Add'} the kind “{group['label']}” ({group['label_plural']}) "
               f"— {len(group['fields'])} thing{'s' if len(group['fields']) != 1 else ''} to know about each one")
    return summary, {"group": group}, details, None


def _stage_update_deal_value(tenant_id, client, action):
    deal_id = _clean(action, "deal_id", 64)
    if not _UUID_RE.match(deal_id):
        raise ActionError("I couldn't pin down which deal — could you name it?")
    deal = db.crm_get_deal(deal_id, tenant_id, client=client)
    if not deal:
        raise ActionError("I couldn't find that deal.")
    amount = _parse_amount(_clean(action, "amount", 20))
    old = deal.get("value_estimate")
    if old is not None and round(float(old), 2) == amount:
        raise ActionError(f"“{deal.get('title') or 'That deal'}” is already valued "
                          f"at £{amount:,.2f} — nothing to change.")
    payload = {"deal_id": deal_id, "amount": amount,
               "old_value": float(old) if old is not None else None,
               "title": deal.get("title") or "deal",
               "contact_id": (deal.get("crm_contacts") or {}).get("id") or ""}
    summary = (f"{payload['title']}: value "
               + (f"£{payload['old_value']:,.2f} → " if payload["old_value"] is not None
                  else "") + f"£{amount:,.2f}")
    return summary, payload, [f"New value: £{amount:,.2f}"], None


async def _exec_update_deal_value(tenant_id, client, payload, edited):
    db.crm_update_deal(payload["deal_id"], tenant_id,
                       {"value_estimate": payload["amount"]}, client=client)
    db.log_agent_event(tenant_id, "deal_stage",
                       f"Concierge set “{payload.get('title')}” value to "
                       f"£{payload['amount']:,.2f} — confirmed by you", client=client)
    contact_id = payload.get("contact_id")
    link = ({"label": "Open the deal", "href": f"/app/crm/contacts/{contact_id}"}
            if contact_id else {"label": "Open the pipeline", "href": "/app/crm/pipeline"})
    return {"summary": f"Done — “{payload.get('title')}” is now valued at "
                       f"£{payload['amount']:,.2f}.", "link": link}


# ── invoicing (standalone core module, 2026-07-26) ───────────────────────────
def _resolve_invoice(tenant_id, client, raw: str) -> dict:
    """An invoice from either handle the model may hold: the uuid from a
    list_invoices result, or the human ref (INV-1001) — resolve, don't bounce."""
    raw = (raw or "").strip()
    if not raw:
        raise ActionError("I couldn't pin down which invoice — could you say which "
                          "one (the INV- number or the customer)?")
    inv = None
    if _UUID_RE.match(raw):
        inv = db.get_invoice(raw, tenant_id, client=client)
    else:
        inv = db.get_invoice_by_ref(tenant_id, raw.upper(), client=client)
    if not inv:
        raise ActionError("I couldn't find that invoice.")
    return inv


def _parse_amount(raw: str) -> float:
    cleaned = (raw or "").replace("£", "").replace(",", "").strip()
    try:
        amount = round(float(cleaned), 2)
    except (TypeError, ValueError):
        raise ActionError("I need the price to invoice — what should it be?")
    if not 0 < amount <= 1_000_000:
        raise ActionError(f"£{cleaned} doesn't look like a billable amount.")
    return amount


def _stage_invoice_create(tenant_id, client, action):
    contact = _clean(action, "contact", 254).lower()
    if "@" not in contact:
        raise ActionError("I need the customer's email address to invoice them.")
    description = _clean(action, "title", 200) or "Services"
    amount = _parse_amount(_clean(action, "amount", 20))
    due_date = _clean(action, "due_date", 10)
    if due_date:
        due_date = _require_date(due_date, "the due date")
    booking_id = _clean(action, "booking_id", 64)
    booking = None
    if booking_id:
        if _UUID_RE.match(booking_id):
            booking = db.get_booking(booking_id, tenant_id, client=client)
        else:
            rows = db.search_bookings(tenant_id, limit=50, client=client) or []
            match = next((b for b in rows if (b.get("booking_ref") or "").strip().lower()
                          == booking_id.lower()), None)
            if match:
                booking = db.get_booking(match["id"], tenant_id, client=client)
        if not booking:
            raise ActionError("I couldn't find that booking to invoice against — "
                              "you can still invoice without one.")
    crm_contact = db.crm_contact_by_email(tenant_id, contact, client=client)
    name = (crm_contact or {}).get("name") or (booking or {}).get("contact") or contact
    payload = {"contact": contact, "contact_name": name,
               "contact_id": (crm_contact or {}).get("id") or "",
               "booking_id": (booking or {}).get("id") or "",
               "booking_ref": (booking or {}).get("booking_ref") or "",
               "description": description, "amount": amount, "due_date": due_date}
    summary = f"Create invoice for {name} — {description} = £{amount:,.2f}"
    try:   # the card states the real default — the tenant's own terms
        from app import invoice_config
        _n = invoice_config.due_days(invoice_config.settings_for(
            db.get_invoice_settings(tenant_id, client=client)))
    except Exception:
        _n = 14
    default_due = "Due: on receipt" if _n == 0 else f"Due: {_n} days from today (your payment terms)"
    details = [f"Line: {description} × 1 — £{amount:,.2f} (before any VAT)",
               f"Due: {due_date}" if due_date else default_due]
    if payload["booking_ref"]:
        details.append(f"Billing booking {payload['booking_ref']}")
    details.append("Created as a DRAFT — you'll send it separately.")
    return summary, payload, details, None


def _stage_invoice_send(tenant_id, client, action):
    from app import invoice_config, invoice_send as invsend, payments
    inv = _resolve_invoice(tenant_id, client, _clean(action, "invoice_id", 64))
    if inv.get("status") in ("paid", "cancelled", "void"):
        raise ActionError(f"Invoice {inv.get('invoice_ref')} is {inv.get('status')} — "
                          "nothing to send.")
    to = (inv.get("customer_email") or "").strip()
    if not to:
        raise ActionError(f"Invoice {inv.get('invoice_ref')} has no customer email — "
                          "add one on the invoice page first.")
    total = float(inv.get("total") or 0)
    pay_now = payments.tenant_can_take_payments(tenant_id, client=client)
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    pay_link = payments.payment_url(inv["id"]) if pay_now else None
    # The exact email text, shown editable in the card — Confirm sends what the
    # operator sees (the reminder card always worked this way; the invoice card
    # used to send sight-unseen).
    body = invsend.invoice_email_body(inv, s, pay_link,
                                      invsend.configured_sign_off(tenant_id, client=client))
    resend = inv.get("status") != "draft"
    payload = {"invoice_id": inv["id"], "ref": inv.get("invoice_ref"),
               "to": to, "total": total, "draft_text": body,
               "shown": _shown(inv, db.list_invoice_lines(inv["id"], tenant_id, client=client))}
    summary = (f"{'Resend' if resend else 'Send'} invoice {payload['ref']} "
               f"(£{total:,.2f}) to {to}")
    details = []
    if resend:
        sent_on = str(inv.get("sent_at") or "")[:10]
        details.append("⚠ Already sent" + (f" on {sent_on}" if sent_on else "")
                       + " — confirming emails the customer AGAIN.")
    details.append(f"Emailed as a PDF from your mailbox to {to}")
    if pay_now:
        details.append("Includes a Pay Now link — card payments mark it paid "
                       "automatically.")
    else:
        gap = invsend.payment_route_gap(s, None)
        details.append(f"⚠ {gap} Add them in Settings › Getting paid before sending."
                       if gap else
                       "No Stripe connected — the email carries the bank-transfer "
                       "details from Settings › Getting paid.")
    details.append("Edit the email text below if you like — Confirm sends this "
                   "exact text with the PDF attached.")
    return summary, payload, details, body


def _stage_invoice_mark_paid(tenant_id, client, action):
    inv = _resolve_invoice(tenant_id, client, _clean(action, "invoice_id", 64))
    if inv.get("status") not in db.INVOICE_PAYABLE_STATUSES:
        raise ActionError(f"Invoice {inv.get('invoice_ref')} is already "
                          f"{inv.get('status')}.")
    method = _clean(action, "payment_method", 20).lower() or "other"
    if method not in ("card", "bank_transfer", "cash", "other"):
        method = "other"
    total = float(inv.get("total") or 0)
    who = inv.get("customer_name") or inv.get("customer_email") or "the customer"
    payload = {"invoice_id": inv["id"], "ref": inv.get("invoice_ref"),
               "total": total, "payment_method": method, "who": who,
               "shown": _shown(inv, db.list_invoice_lines(inv["id"], tenant_id, client=client))}
    summary = (f"Mark invoice {payload['ref']} (£{total:,.2f}) as paid — "
               f"{method.replace('_', ' ')}")
    return summary, payload, [f"Customer: {who}",
                              f"Payment method: {method.replace('_', ' ')}"], None


def _stage_invoice_reminder_draft(tenant_id, client, action):
    from app import invoice_config, payments
    inv = _resolve_invoice(tenant_id, client, _clean(action, "invoice_id", 64))
    if inv.get("status") not in ("sent", "overdue", "partially_paid"):
        raise ActionError(f"Invoice {inv.get('invoice_ref')} isn't awaiting payment "
                          f"(it's {inv.get('status')}) — nothing to chase.")
    to = (inv.get("customer_email") or "").strip()
    if not to:
        raise ActionError("That invoice has no customer email to remind.")
    ref, total = inv.get("invoice_ref"), float(inv.get("total") or 0)
    draft_text = str((action or {}).get("draft_text") or "").strip()[:8000]
    if not draft_text:
        # The model normally writes the reminder itself; this fallback keeps the
        # card honest rather than bouncing the proposal.
        from app import invoice_send as invsend
        s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
        biz = s.get("business_name") or "us"
        closing = invsend.configured_sign_off(tenant_id, client=client) or f"Thank you,\n{biz}"
        draft_text = (f"Hello,\n\nA friendly reminder that invoice {ref} for "
                      f"£{total:,.2f} was due on {str(inv.get('due_date') or '')[:10]}."
                      f"\n\n{closing}")
    draft_text = _signed(tenant_id, client, draft_text)
    details = []
    if payments.tenant_can_take_payments(tenant_id, client=client):
        link = payments.payment_url(inv["id"])
        if link not in draft_text:
            draft_text += f"\n\nYou can pay online here: {link}"
    elif re.search(r"pay\s*now|pay\s+online|payment\s+link", draft_text, re.I):
        # The model promised an online-payment link that doesn't exist (no Stripe
        # connected) — surface it loudly; the textarea is right there to fix it.
        details.append("⚠ This text mentions paying online, but Stripe isn't "
                       "connected so there's no payment link — edit the wording "
                       "before confirming.")
    n = int(inv.get("reminders_sent") or 0)
    payload = {"invoice_id": inv["id"], "ref": ref, "to": to, "total": total,
               "draft_text": draft_text}
    summary = f"Send a payment reminder for {ref} (£{total:,.2f}) to {to}"
    details += [f"This will be reminder #{n + 1} for this invoice"] if n else []
    return summary, payload, details, draft_text


# ── draft invoices and quotes: edit and delete, always on a card (2026-10-15) ──
# Scenario N20 on the rig: "I put the wrong amount on one of Priya's invoices, a draft
# one. Can you just bin it, or change it to two hundred quid?" Nothing existed to do
# either, and the Concierge made up an update. Only a DRAFT is in reach: a document that
# was sent, opened, linked for payment or paid against stays a refusal that says why.
MAX_EDIT_LINES = 20
_SENT_LIKE = ("sent", "overdue", "partially_paid")


def _amount_cell(raw, what: str) -> float:
    """One line's amount as the main app's editor reads it (round(float(...), 2)),
    minus its silent fall-back to 0: a figure the Concierge cannot read is refused.
    Negative is allowed — a credit line (a deposit already paid) is one."""
    cleaned = str(raw if raw is not None else "").replace("£", "").replace(",", "").strip()
    try:
        value = round(float(cleaned), 2)
    except (TypeError, ValueError):
        raise ActionError(f"I couldn't read '{cleaned[:30]}' as an amount for {what}.")
    if not math.isfinite(value) or abs(value) > 1_000_000:
        raise ActionError(f"£{cleaned[:30]} doesn't look like a billable amount.")
    return value


def _plain_lines(rows) -> list[dict]:
    return [{"description": str(r.get("description") or "").strip(),
             "amount": round(float(r.get("amount") or 0), 2)} for r in rows or []]


def _line_sig(lines) -> list:
    """What makes two sets of lines the same set: description and amount, in any order."""
    return sorted((str(l.get("description") or "").strip(), round(float(l.get("amount") or 0), 2))
                  for l in lines or [])


def _read_edit_lines(action: dict, current: list[dict], noun: str) -> list[dict]:
    """The new lines the owner asked for, as [{description, amount}]. The model sends
    them in `lines` — text ('Parts | 120.50' per line), a JSON array, or a list — or,
    for a document with exactly one line, just the new `amount` (and maybe a new
    `title`). It replaces the lines wholesale, as the main app's editor does."""
    raw = (action or {}).get("lines")
    items = None
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()[:6000]
        if text[0] in "[{":
            try:
                parsed = json.loads(text)
            except ValueError:
                raise ActionError("I couldn't read the new lines — give each one a description "
                                  "and an amount.")
            items = parsed if isinstance(parsed, list) else [parsed]
        else:
            items = [ln for ln in text.splitlines() if ln.strip()]
    if items is None:
        if not _clean(action, "amount", 20):
            raise ActionError("Tell me the new lines — a description and an amount for each — "
                              f"or just the new price if the {noun} has a single line.")
        if len(current) != 1:
            have = "; ".join(f"{l['description']} £{l['amount']:,.2f}" for l in current[:8])
            raise ActionError(f"That {noun} has {len(current)} lines ({have}), so tell me the full "
                              "set of new lines — a description and an amount for each.")
        return [{"description": _clean(action, "title", 200) or current[0]["description"],
                 "amount": _parse_amount(_clean(action, "amount", 20))}]
    if not items:
        raise ActionError(f"There are no lines in that — a {noun} needs at least one.")
    if len(items) > MAX_EDIT_LINES:
        raise ActionError(f"I can change at most {MAX_EDIT_LINES} lines at once.")
    out = []
    for n, item in enumerate(items, 1):
        if isinstance(item, dict):
            desc = str(item.get("description") or item.get("title") or item.get("name") or "").strip()
            amount = item.get("amount")
        elif isinstance(item, str):
            desc, bar, amount = item.rpartition("|")
            if not bar:
                raise ActionError(f"Line {n} needs a description and an amount, like 'Parts | 120.00'.")
            desc = desc.strip()
        else:
            raise ActionError(f"I couldn't read line {n} — it needs a description and an amount.")
        if not desc:
            raise ActionError(f"Line {n} has no description.")
        out.append({"description": desc[:200], "amount": _amount_cell(amount, f"line {n}")})
    return out


def _align_lines(old: list[dict], new: list[dict]):
    """Which existing line each new line stands for: the same description first (any
    case), then the same place for a reworded one. Returns (pairs, removed, exact):
    pairs[i] is the index into `old` that new[i] replaces, or None for an added line;
    removed lists the old lines no new line stands for; exact is the set of new lines
    matched by description. A line's link to its job rides either match, so a reworded
    line does not free billed work to be invoiced again; the flags an owner set on a
    quote line (optional, ticked, price entry, deposit rule) ride the exact match only
    and never pass to a different line that merely landed in the same place."""
    used: set[int] = set()
    exact: set[int] = set()
    pairs: list = [None] * len(new)
    for i, n in enumerate(new):
        for j, o in enumerate(old):
            if j not in used and o["description"].strip().lower() == n["description"].strip().lower():
                pairs[i] = j
                used.add(j)
                exact.add(i)
                break
    for i in range(len(new)):
        if pairs[i] is None and i < len(old) and i not in used:
            pairs[i] = i
            used.add(i)
    return pairs, [j for j in range(len(old)) if j not in used], exact


def _line_changes(old: list[dict], new: list[dict], pairs, removed) -> list[str]:
    """The card's before-and-after, one line per change; unchanged lines are left out."""
    out = []
    for i, n in enumerate(new):
        j = pairs[i]
        if j is None:
            out.append(f"Add: {n['description']} — £{n['amount']:,.2f}")
            continue
        o = old[j]
        if o["description"].strip() == n["description"] and round(float(o["amount"]), 2) == n["amount"]:
            continue
        if o["description"].strip() == n["description"]:
            out.append(f"{n['description']}: £{float(o['amount']):,.2f} → £{n['amount']:,.2f}")
        else:
            out.append(f"{o['description']} (£{float(o['amount']):,.2f}) → "
                       f"{n['description']} (£{n['amount']:,.2f})")
    for j in removed:
        out.append(f"Remove: {old[j]['description']} — £{float(old[j]['amount']):,.2f}")
    return out


def _not_a_draft(noun: str, ref, status, did: str, sent_hint: str) -> str:
    """Why a document that is not a draft cannot be edited or deleted from here, in
    words that are true for how far it got."""
    if status == "paid":
        return f"{noun} {ref} has already been paid, so it can't be {did} from here."
    if status in _SENT_LIKE:
        return (f"{noun} {ref} was already sent, so it can't be {did} from here. {sent_hint}").strip()
    return f"{noun} {ref} is {status or 'not a draft'}, so it can't be {did} from here."


def _refused(summary: str) -> dict:
    """A Confirm that found the record changed under the card. Nothing was written, and
    the sentence says so: state 'failed' reaches the owner as that sentence, where a
    raised ActionError would only reach them as 'the outcome isn't confirmed'."""
    return {"summary": summary, "state": "failed"}


def _shown(doc: dict, lines, email=None) -> dict:
    """What the owner was shown on a send or settle card, kept in the proposal payload
    ('shown'): numbers and a hash only, no text. Confirm compares the document as it is
    then, so a draft edited after the card was prepared is refused, never sent or settled."""
    import hashlib
    sig = _line_sig(lines)
    return {"total": round(float(doc.get("total") or 0), 2), "lines_n": len(sig),
            "lines_hash": hashlib.sha1(repr(sig).encode()).hexdigest()[:16],
            "to": (email if email is not None else str(doc.get("customer_email") or "")).strip().lower()}


def _changed_since_prepared(payload: dict, doc: dict, lines, noun: str, ref, did: str):
    """None when the document is what the card showed, or when the card carries no
    snapshot (a card prepared before this check: it behaves as it always did). Otherwise
    the refusal that reaches the owner: nothing was sent or settled."""
    before = (payload or {}).get("shown")
    if not isinstance(before, dict):
        return None
    now = _shown(doc, lines)
    if all(before.get(k) == now[k] for k in ("total", "lines_n", "lines_hash", "to")):
        return None
    what = (f"it was £{before.get('total') or 0:,.2f}, it is now £{now['total']:,.2f}"
            if before.get("total") != now["total"]
            else "its lines or the customer's address are different now")
    return _refused(f"{noun} {ref} changed after I prepared this ({what}), so I have not {did} it. "
                    "Ask me to prepare it again and I'll show you the current version.")


# What each fact about a draft invoice means in words — one clause per column
# db.UNTOUCHED_INVOICE_NULLS reads, so the sentence and the delete's guard cannot drift.
_INVOICE_TOUCHED = {
    "sent_at": "was sent to the customer at some point",
    "payment_link": "has a payment link out",
    "viewed_at": "has been opened by the customer",
    "paid_at": "has a payment recorded against it",
    "payment_method": "has a payment recorded against it",
    "stripe_payment_intent_id": "has a payment recorded against it",
}


def _invoice_touched_clause(inv: dict) -> str | None:
    for col in ("sent_at", "payment_link", "viewed_at", "paid_at", "payment_method",
                "stripe_payment_intent_id"):
        if inv.get(col):
            return _INVOICE_TOUCHED[col]
    if float(inv.get("amount_paid") or 0):
        return "has a payment recorded against it"
    return None


def _stage_invoice_edit_draft(tenant_id, client, action):
    inv = _resolve_invoice(tenant_id, client, _clean(action, "invoice_id", 64))
    ref = inv.get("invoice_ref")
    if inv.get("status") != "draft":
        raise ActionError(_not_a_draft(
            "Invoice", ref, inv.get("status"), "edited",
            "Cancelling or voiding a sent invoice isn't available from here yet."))
    rows = db.list_invoice_lines(inv["id"], tenant_id, client=client) or []
    old = [{**plain, "booking_id": row.get("booking_id")}
           for plain, row in zip(_plain_lines(rows), rows)]
    new = _read_edit_lines(action, old, "invoice")
    pairs, removed, _exact = _align_lines(old, new)
    lines = [{**n, "booking_id": old[pairs[i]]["booking_id"] if pairs[i] is not None else None}
             for i, n in enumerate(new)]
    if _line_sig(lines) == _line_sig(old):
        raise ActionError(f"Invoice {ref} already says that — nothing to change.")
    _subtotal, vat, total_after = db.invoice_totals(
        tenant_id, lines, include=inv.get("prices_include_vat") is True, client=client)
    if total_after <= 0:
        raise ActionError(f"That would leave invoice {ref} at £0.00 or less — say if you'd "
                          "rather I delete the draft.")
    total_before = round(float(inv.get("total") or 0), 2)
    who = inv.get("customer_name") or inv.get("customer_email") or "the customer"
    payload = {"invoice_id": inv["id"], "ref": ref, "who": who,
               "total_before": total_before, "total_after": total_after,
               "before": [{"description": l["description"], "amount": l["amount"]} for l in old],
               "lines": lines}
    summary = (f"Change draft invoice {ref} for {who}: £{total_before:,.2f} → £{total_after:,.2f}"
               if total_before != total_after else f"Change the lines on draft invoice {ref} for {who}")
    total_line = f"Total: £{total_before:,.2f} → £{total_after:,.2f}"
    if vat:
        total_line += f" (including £{vat:,.2f} VAT)"
    details = _line_changes(old, lines, pairs, removed) + [
        total_line, "It stays a DRAFT — nothing has been sent to the customer."]
    return summary, payload, details, None


def _stage_invoice_delete_draft(tenant_id, client, action):
    inv = _resolve_invoice(tenant_id, client, _clean(action, "invoice_id", 64))
    ref = inv.get("invoice_ref")
    if inv.get("status") != "draft":
        raise ActionError(_not_a_draft(
            "Invoice", ref, inv.get("status"), "deleted",
            "Cancelling or voiding a sent invoice isn't available from here yet."))
    clause = _invoice_touched_clause(inv)
    if clause:
        raise ActionError(f"Invoice {ref} {clause}, so it can't be deleted from here.")
    quote_ref = db.invoice_linked_quote_ref(inv["id"], tenant_id, client=client)
    if quote_ref:
        raise ActionError(f"Invoice {ref} was made from quote {quote_ref}, so it can't be "
                          "deleted from here.")
    total = round(float(inv.get("total") or 0), 2)
    who = inv.get("customer_name") or inv.get("customer_email") or "the customer"
    payload = {"invoice_id": inv["id"], "ref": ref, "who": who, "total": total,
               # the audit copy: the proposal row is all that remains of it afterwards
               "lines": _plain_lines(db.list_invoice_lines(inv["id"], tenant_id, client=client))}
    summary = f"Delete draft {ref} (£{total:,.2f} for {who})"
    return summary, payload, ["Nothing has been sent to the customer.",
                              "It can't be brought back."], None


# ── quote actions (docs/modules/module-quotes.md Phase 2 — the invoice siblings) ─────
def _resolve_quote(tenant_id, client, raw: str) -> dict:
    """A quote from either handle the model may hold: the uuid from a
    list_quotes result, or the human ref (QUO-1001) — resolve, don't bounce."""
    raw = (raw or "").strip()
    if not raw:
        raise ActionError("I couldn't pin down which quote — could you say which "
                          "one (the QUO- number or the customer)?")
    quote = None
    if _UUID_RE.match(raw):
        quote = db.get_quote(raw, tenant_id, client=client)
    else:
        quote = db.get_quote_by_ref(tenant_id, raw.upper(), client=client)
    if not quote:
        raise ActionError("I couldn't find that quote.")
    return quote


def _stage_quote_create(tenant_id, client, action):
    contact = _clean(action, "contact", 254).lower()
    if "@" not in contact:
        raise ActionError("I need the customer's email address to quote them.")
    description = _clean(action, "title", 200) or "Work as discussed"
    amount = _parse_amount(_clean(action, "amount", 20))
    valid_until = _clean(action, "valid_until", 10)
    if valid_until:
        valid_until = _require_date(valid_until, "the valid-until date")
    deposit = _clean(action, "deposit_percent", 6)
    deposit_v = None
    if deposit:
        try:
            deposit_v = float(deposit)
        except ValueError:
            raise ActionError(f"'{deposit}' isn't a deposit percentage.")
        if deposit_v == 0:
            deposit_v = 0          # explicit "no deposit on this quote"
        elif not (0 < deposit_v <= 100):
            raise ActionError("The deposit must be between 1 and 100 percent.")
    booking_id = _clean(action, "booking_id", 64)
    booking = None
    if booking_id:
        if _UUID_RE.match(booking_id):
            booking = db.get_booking(booking_id, tenant_id, client=client)
        else:
            rows = db.search_bookings(tenant_id, limit=50, client=client) or []
            match = next((b for b in rows if (b.get("booking_ref") or "").strip().lower()
                          == booking_id.lower()), None)
            if match:
                booking = db.get_booking(match["id"], tenant_id, client=client)
        if not booking:
            raise ActionError("I couldn't find that booking to quote against — "
                              "you can still quote without one.")
    crm_contact = db.crm_contact_by_email(tenant_id, contact, client=client)
    name = (crm_contact or {}).get("name") or (booking or {}).get("contact") or contact
    payload = {"contact": contact, "contact_name": name,
               "contact_id": (crm_contact or {}).get("id") or "",
               "booking_id": (booking or {}).get("id") or "",
               "booking_ref": (booking or {}).get("booking_ref") or "",
               "description": description, "amount": amount,
               "valid_until": valid_until,
               "deposit_percent": "" if deposit_v is None else deposit_v}
    summary = f"Create quote for {name} — {description} = £{amount:,.2f}"
    details = [f"Line: {description} × 1 — £{amount:,.2f} (before any VAT)",
               f"Valid until: {valid_until}" if valid_until
               else "Valid until: your default validity window"]
    if deposit_v:
        details.append(f"Deposit on acceptance: {deposit_v:g}%")
    elif deposit_v == 0:
        details.append("No deposit on this quote")
    if payload["booking_ref"]:
        details.append(f"Quoting booking {payload['booking_ref']}")
    details.append("Created as a DRAFT — you'll send it separately.")
    return summary, payload, details, None


def _stage_quote_send(tenant_id, client, action):
    from app import invoice_config, quote_send as qsend
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") not in ("draft", "sent"):
        raise ActionError(f"Quote {quote.get('quote_ref')} is {quote.get('status')} — "
                          "nothing to send.")
    to = (quote.get("customer_email") or "").strip()
    if not to:
        raise ActionError(f"Quote {quote.get('quote_ref')} has no customer email — "
                          "add one on the quote page first.")
    total = float(quote.get("total") or 0)
    s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
    from app import invoice_send as invsend
    body = qsend.quote_email_body(quote, s, qsend.quote_url(quote["id"]),
                                  invsend.configured_sign_off(tenant_id, client=client))
    resend = quote.get("status") != "draft"
    payload = {"quote_id": quote["id"], "ref": quote.get("quote_ref"),
               "to": to, "total": total, "draft_text": body,
               "shown": _shown(quote, db.list_quote_lines(quote["id"], tenant_id, client=client))}
    summary = (f"{'Resend' if resend else 'Send'} quote {payload['ref']} "
               f"(£{total:,.2f}) to {to}")
    details = []
    if resend:
        sent_on = str(quote.get("sent_at") or "")[:10]
        details.append("⚠ Already sent" + (f" on {sent_on}" if sent_on else "")
                       + " — confirming emails the customer AGAIN.")
    details += [f"Emailed from your mailbox to {to} as a view-and-accept link",
                "You'll get a notification when they open it, and when they "
                "accept or decline",
                "Edit the email text below if you like — Confirm sends this "
                "exact text."]
    return summary, payload, details, body


def _stage_quote_reminder_draft(tenant_id, client, action):
    from app import quote_send as qsend
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") != "sent":
        raise ActionError(f"Quote {quote.get('quote_ref')} isn't awaiting an answer "
                          f"(it's {quote.get('status')}) — nothing to chase.")
    to = (quote.get("customer_email") or "").strip()
    if not to:
        raise ActionError("That quote has no customer email to chase.")
    ref, total = quote.get("quote_ref"), float(quote.get("total") or 0)
    draft_text = str((action or {}).get("draft_text") or "").strip()[:8000]
    if not draft_text:
        from app import invoice_config, invoice_send as invsend
        s = invoice_config.settings_for(db.get_invoice_settings(tenant_id, client=client))
        biz = s.get("business_name") or "us"
        closing = invsend.configured_sign_off(tenant_id, client=client) or f"Thank you,\n{biz}"
        draft_text = (f"Hello,\n\nJust checking you received our quote {ref} for "
                      f"£{total:,.2f} — happy to answer any questions or adjust "
                      f"anything.\n\n{closing}")
    draft_text = _signed(tenant_id, client, draft_text)
    link = qsend.quote_url(quote["id"])
    if link not in draft_text:
        draft_text += f"\n\nYou can view and accept the quote here:\n{link}"
    n = int(quote.get("reminders_sent") or 0)
    payload = {"quote_id": quote["id"], "ref": ref, "to": to, "total": total,
               "draft_text": draft_text}
    summary = f"Send a follow-up on quote {ref} (£{total:,.2f}) to {to}"
    details = [f"This will be follow-up #{n + 1} for this quote"] if n else []
    return summary, payload, details, draft_text


def _stage_quote_mark_accepted(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") != "sent":
        raise ActionError(f"Quote {quote.get('quote_ref')} is {quote.get('status')} — "
                          "only a sent quote can be marked accepted.")
    who = _clean(action, "accepted_name", 200) or "Accepted verbally"
    total = float(quote.get("total") or 0)
    payload = {"quote_id": quote["id"], "ref": quote.get("quote_ref"),
               "total": total, "accepted_name": who}
    summary = f"Mark quote {payload['ref']} (£{total:,.2f}) as accepted — {who}"
    details = ["Records the acceptance exactly like an online one: the name, "
               "the date, and a frozen copy of what was quoted"]
    if quote.get("deposit_percent"):
        _of = (f" of £{float(quote['deposit_base']):,.2f}"
               if quote.get("deposit_base") is not None else "")
        details.append(f"Mints the {float(quote['deposit_percent']):g}%{_of} deposit "
                       "invoice — you send it (or share its pay link) yourself")
    return summary, payload, details, None


def _stage_quote_mark_declined(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") != "sent":
        raise ActionError(f"Quote {quote.get('quote_ref')} is {quote.get('status')} — "
                          "only a sent quote can be marked declined.")
    reason = _clean(action, "reason", 500)
    who = quote.get("customer_name") or quote.get("customer_email") or "the customer"
    payload = {"quote_id": quote["id"], "ref": quote.get("quote_ref"),
               "reason": reason, "who": who}
    summary = f"Mark quote {payload['ref']} as declined"
    return summary, payload, ([f"Reason: {reason}"] if reason else []) + [
        f"Customer: {who}"], None


def _stage_quote_convert_invoice(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") != "accepted":
        raise ActionError(f"Quote {quote.get('quote_ref')} is {quote.get('status')} — "
                          "only an accepted quote converts to an invoice.")
    total = float(quote.get("total") or 0)
    who = quote.get("customer_name") or quote.get("customer_email") or "the customer"
    payload = {"quote_id": quote["id"], "ref": quote.get("quote_ref"),
               "total": total, "who": who}
    summary = f"Convert quote {payload['ref']} (£{total:,.2f}) into a draft invoice"
    details = ["Copies exactly the lines the customer accepted — never re-keyed"]
    if quote.get("deposit_invoice_id"):
        details.append("The deposit already invoiced is credited, so the invoice "
                       "is the outstanding balance")
    details.append("Created as a DRAFT — you'll send it separately.")
    return summary, payload, details, None


def _stage_quote_cancel(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    if quote.get("status") not in ("draft", "sent"):
        raise ActionError(f"Quote {quote.get('quote_ref')} is {quote.get('status')} — "
                          "it can't be cancelled.")
    payload = {"quote_id": quote["id"], "ref": quote.get("quote_ref")}
    summary = f"Cancel quote {payload['ref']}"
    return summary, payload, ["Its link stops working and its number is never "
                              "reused"], None


_QUOTE_SENT_HINT = ("I can cancel it instead if you like — a cancelled quote stays on record.")

# What each fact about a draft quote means in words: a clause per column
# db.UNTOUCHED_QUOTE_NULLS reads, plus the two counters db.delete_draft_quote guards.
_QUOTE_TOUCHED = {
    "sent_at": "was sent to the customer at some point",
    "viewed_at": "has been opened by the customer",
    "view_count": "has been opened by the customer",
    "accepted_at": "was accepted by the customer",
    "declined_at": "was declined by the customer",
    "invoice_id": "has an invoice built on it",
    "deposit_invoice_id": "has an invoice built on it",
    "reminders_sent": "has had a follow-up sent",
}


def _quote_touched_clause(quote: dict) -> str | None:
    for col in ("sent_at", "viewed_at", "view_count", "accepted_at", "declined_at",
                "invoice_id", "deposit_invoice_id", "reminders_sent"):
        if quote.get(col):
            return _QUOTE_TOUCHED[col]
    return None


def _stage_quote_edit_draft(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    ref = quote.get("quote_ref")
    if quote.get("status") != "draft":
        raise ActionError(_not_a_draft("Quote", ref, quote.get("status"), "edited", _QUOTE_SENT_HINT))
    rows = db.list_quote_lines(quote["id"], tenant_id, client=client) or []
    old = [{**plain, "booking_id": row.get("booking_id"), "kb_entry_id": row.get("kb_entry_id"),
            "is_optional": bool(row.get("is_optional")), "is_selected": row.get("is_selected") is not False,
            "counts_to_deposit_rule": bool(row.get("counts_to_deposit_rule"))}
           for plain, row in zip(_plain_lines(rows), rows)]
    new = _read_edit_lines(action, old, "quote")
    pairs, removed, exact = _align_lines(old, new)
    lines = []
    for i, n in enumerate(new):
        o = old[pairs[i]] if pairs[i] is not None else None
        line = {"description": n["description"], "amount": n["amount"], "line_order": i,
                "booking_id": o["booking_id"] if o else None,
                "kb_entry_id": None, "is_optional": False, "is_selected": True}
        if o is not None and i in exact:           # the flags belong to the line they were set on
            line.update(kb_entry_id=o["kb_entry_id"], is_optional=o["is_optional"],
                        is_selected=o["is_selected"])
            if o["counts_to_deposit_rule"]:
                line["counts_to_deposit_rule"] = True
        lines.append(line)
    if _line_sig(lines) == _line_sig(old):
        raise ActionError(f"Quote {ref} already says that — nothing to change.")
    _subtotal, vat, total_after = db.quote_totals(
        tenant_id, lines, include=quote.get("prices_include_vat") is True, client=client)
    if total_after <= 0:
        raise ActionError(f"That would leave quote {ref} at £0.00 or less — say if you'd "
                          "rather I delete the draft.")
    total_before = round(float(quote.get("total") or 0), 2)
    who = quote.get("customer_name") or quote.get("customer_email") or "the customer"
    payload = {"quote_id": quote["id"], "ref": ref, "who": who,
               "total_before": total_before, "total_after": total_after,
               "before": [{"description": l["description"], "amount": l["amount"]} for l in old],
               "lines": lines}
    summary = (f"Change draft quote {ref} for {who}: £{total_before:,.2f} → £{total_after:,.2f}"
               if total_before != total_after else f"Change the lines on draft quote {ref} for {who}")
    total_line = f"Total: £{total_before:,.2f} → £{total_after:,.2f}"
    if vat:
        total_line += f" (including £{vat:,.2f} VAT)"
    details = _line_changes(old, lines, pairs, removed) + [
        total_line, "It stays a DRAFT — nothing has been sent to the customer."]
    return summary, payload, details, None


def _stage_quote_delete_draft(tenant_id, client, action):
    quote = _resolve_quote(tenant_id, client, _clean(action, "quote_id", 64))
    ref = quote.get("quote_ref")
    if quote.get("status") != "draft":
        raise ActionError(_not_a_draft("Quote", ref, quote.get("status"), "deleted", _QUOTE_SENT_HINT))
    clause = _quote_touched_clause(quote)
    if clause:
        raise ActionError(f"Quote {ref} {clause}, so it can't be deleted from here.")
    total = round(float(quote.get("total") or 0), 2)
    who = quote.get("customer_name") or quote.get("customer_email") or "the customer"
    payload = {"quote_id": quote["id"], "ref": ref, "who": who, "total": total,
               "lines": _plain_lines(db.list_quote_lines(quote["id"], tenant_id, client=client))}
    summary = f"Delete draft {ref} (£{total:,.2f} for {who})"
    return summary, payload, ["Nothing has been sent to the customer.",
                              "It can't be brought back."], None


_STAGERS = {
    "cancel_booking": _stage_cancel_booking,
    "confirm_booking": _stage_confirm_booking,
    "block_date": _stage_block_date,
    "unblock_date": _stage_unblock_date,
    "assign_person": _stage_assign_person,
    "update_contact": _stage_update_contact,
    "update_dependant": _stage_update_dependant,
    "change_stay": _stage_change_stay,
    "request_renewal": _stage_request_renewal,
    "draft_reply": _stage_draft_reply,
    "move_booking": _stage_move_booking,
    "update_deal_stage": _stage_update_deal_stage,
    "update_deal_value": _stage_update_deal_value,
    "create_followup": _stage_create_followup,
    "create_contact": _stage_create_contact,
    "add_kb_entry": _stage_add_kb_entry,
    "define_dependants": _stage_define_dependants,
    "invoice_create": _stage_invoice_create,
    "invoice_send": _stage_invoice_send,
    "invoice_mark_paid": _stage_invoice_mark_paid,
    "invoice_reminder_draft": _stage_invoice_reminder_draft,
    "invoice_edit_draft": _stage_invoice_edit_draft,
    "invoice_delete_draft": _stage_invoice_delete_draft,
    "quote_create": _stage_quote_create,
    "quote_send": _stage_quote_send,
    "quote_reminder_draft": _stage_quote_reminder_draft,
    "quote_mark_accepted": _stage_quote_mark_accepted,
    "quote_mark_declined": _stage_quote_mark_declined,
    "quote_convert_invoice": _stage_quote_convert_invoice,
    "quote_cancel": _stage_quote_cancel,
    "quote_edit_draft": _stage_quote_edit_draft,
    "quote_delete_draft": _stage_quote_delete_draft,
}


# ── execution (runs ONLY on the operator's Confirm) ──────────────────────────
async def execute(tenant_id: str, client, kind: str, payload: dict, *,
                  edited_draft_text: str | None = None,
                  selected: list[int] | None = None) -> dict:
    """Run one claimed proposal. Returns {summary, link:{label, href}} for the
    chat; raises ActionError with an honest sentence when the write fails in a
    way the customer should hear about (e.g. the slot filled up meanwhile)."""
    if kind == "bulk":
        return await _exec_bulk(tenant_id, client, payload, edited_draft_text, selected=selected)
    executor = _EXECUTORS.get(kind)
    if not executor:
        raise ActionError("That action isn't supported.")
    return await executor(tenant_id, client, payload, edited_draft_text)


async def _exec_draft_reply(tenant_id, client, payload, edited):
    from urllib.parse import quote
    text = (edited or "").strip()[:8000] or payload["draft_text"]
    log_id, contact = payload["log_id"], payload["contact"]
    if not db.get_email_log_entry(log_id, tenant_id, client=client):
        raise ActionError("That conversation isn't available any more.")
    from app.concierge_booking_claims import check
    reason, _ = check(tenant_id, contact, text, client)
    if reason:
        raise ActionError(reason)
    db.update_email_log_draft(log_id, text, tenant_id, client=client)
    db.set_email_log_status(log_id, "pending", tenant_id, client=client)
    db.log_agent_event(tenant_id, "drafted",
                       f"Concierge drafted a reply to {contact} — waiting for approval",
                       contact=contact, subject=payload.get("subject"),
                       email_log_id=log_id, detail={"via": "concierge"}, client=client)
    events.publish(tenant_id, {"type": "status_changed", "log_id": log_id,
                               "contact": contact, "status": "pending"})
    return {"summary": f"Draft placed in {contact}'s conversation — approve it "
                       "there to send.",
            "link": {"label": "Review the draft",
                     "href": "/app/conversations/view?contact=" + quote(contact)}}


async def _exec_confirm_booking(tenant_id, client, payload, edited):
    from urllib.parse import quote
    from app import booking_confirm
    from app.concierge_booking_claims import check
    booking = db.get_booking(payload["booking_id"], tenant_id, client=client)
    if not booking:
        raise ActionError("That booking isn't in the diary any more - nothing changed.")
    ref = payload.get("booking_ref") or booking.get("booking_ref") or "The booking"
    status = str(booking.get("status") or "").lower()
    if status != "confirmed":
        if status != "proposed":
            raise ActionError(f"{ref} is {status or 'not live'} now - nothing was confirmed.")
        done = await booking_confirm.confirm(tenant_id, booking["id"], client=client,
                                             via="by the Concierge")
        if not done["ok"]:
            raise ActionError(
                "That time has been taken since it was held - nothing was confirmed and no "
                "email was queued. Move it or pick another time." if done["reason"] == "full"
                else "That booking isn't in the diary any more - nothing changed.")
    done_summary = f"Done - {ref} is confirmed in the diary."
    link = {"label": "Open the diary", "href": "/app/diary"}
    then = payload.get("then")
    if not then:
        return {"summary": done_summary, "link": link}
    contact = then["contact"]
    thread = {"label": "Review the email",
              "href": "/app/conversations/view?contact=" + quote(contact)}
    if payload.get("draft_text"):
        text = (edited or "").strip()[:8000] or payload["draft_text"]
        reason, _ = check(tenant_id, contact, text, client)
        if reason:
            return {"summary": f"{done_summary} The email wasn't queued: {reason}", "link": link}
        out = await _exec_draft_reply(tenant_id, client, {
            "log_id": then["log_id"], "contact": contact,
            "subject": then.get("subject") or "", "draft_text": text}, None)
        return {"summary": f"{done_summary} The email to {contact} is in your approval queue.",
                "link": out.get("link") or thread}
    # A new email the Drafting agent writes now that the booking is confirmed.
    from app import concierge_draft
    from app.auth import TenantCtx
    try:
        result, _card = await concierge_draft.draft(
            TenantCtx(tenant_id=tenant_id, client=client),
            {"contact": contact, "mode": "new", "instruction": then.get("instruction") or ""},
            member=None)
    except Exception:
        log.warning("confirm-then-email draft failed (tenant %s)", tenant_id, exc_info=True)
        result = {"ok": False}
    if result.get("ok"):
        return {"summary": f"{done_summary} The email to {contact} is drafted and waiting for "
                           "your approval.", "link": thread}
    return {"summary": f"{done_summary} I couldn't draft the email - ask me and I'll write it.",
            "link": link}


async def _exec_move_booking(tenant_id, client, payload, edited):
    from app import calendar_sync
    booking_id = payload["booking_id"]
    if payload.get("timed"):
        # Services diary: the same in-place move the job page's form makes
        # (src/app/record_edits.py) — calendar refresh and agent event included.
        from app import record_edits
        booking = db.get_booking(booking_id, tenant_id, client=client)
        if not booking:
            raise ActionError("The booking is unchanged — it's no longer in the diary.")
        try:
            done = await record_edits.apply_booking_move(tenant_id, booking, payload,
                                                         via="the Concierge", client=client)
        except record_edits.EditError as e:
            raise ActionError(str(e) + (_alternatives(tenant_id, payload["new_date"], client)
                                        if "filled up" in str(e) else ""))
        return {"summary": done,
                "link": {"label": "Open the diary", "href": f"/app/diary?date={payload['new_date']}"}}
    else:
        row, err = scheduling.move_booking(tenant_id, booking_id,
                                           payload["new_date"], payload["new_window"])
    if err:
        note = _alternatives(tenant_id, payload["new_date"], client) if err == "full" else ""
        raise ActionError("The booking is unchanged — that slot "
                          + ("filled up in the meantime." if err == "full"
                             else f"isn't bookable ({err.replace('_', ' ')}).") + note)
    # Calendar: the only pattern is delete-then-recreate (both best-effort).
    try:
        await calendar_sync.remove_pushed_event(tenant_id, booking_id)
        if row.get("status") == "confirmed":
            await calendar_sync.push_bookings(tenant_id, booking_id=booking_id)
    except Exception:
        log.warning("concierge move: calendar refresh failed (booking=%s)", booking_id,
                    exc_info=True)
    who = payload.get("contact") or payload.get("what") or "The booking"
    db.log_agent_event(tenant_id, "booking_moved",
                       f"Concierge moved {who}'s booking from {payload['old_date']} "
                       f"({payload['old_window']}) to {payload['new_date']} "
                       f"({payload['new_window']}) — confirmed by you",
                       contact=payload.get("contact"), booking_id=booking_id,
                       booking_ref=payload.get("booking_ref"),
                       event_date=payload["new_date"], client=client)
    from app import dates as dates_mod, slotgrid
    when = dates_mod.pretty_date(payload["new_date"])
    if payload.get("timed"):
        when += f" at {slotgrid.hhmm(int(payload['new_start_block']))}"
    else:
        when += f" ({payload['new_window']})"
    what = payload.get("what") if payload.get("what") not in (None, "", "booking") else f"{who}'s booking"
    return {"summary": f"Done — {what} moved to {when}.",
            "link": {"label": "Open the diary",
                     "href": f"/app/diary?date={payload['new_date']}"}}


async def _exec_cancel_booking(tenant_id, client, payload, edited):
    """Mirror of the diary's own cancel (dashboard.diary_set_status): calendar
    event off, status cancelled, a freed place offered to the waiting list, and
    the customer's note composed for approval — plus an EXCUSED register entry
    so a business cancellation never charges the customer or spends a credit."""
    from app import calendar_sync, dates as dates_mod, hand_cancel, wallet
    from app import courses as _courses
    ids = [payload["booking_id"]] + list(payload.get("later_ids") or [])
    done, notes = [], 0
    first = None
    for i, bid in enumerate(ids):
        row = db.get_booking(bid, tenant_id, client=client)
        if not row or row.get("status") not in ("proposed", "confirmed"):
            continue
        if first is None:
            first = row
        try:
            await calendar_sync.remove_pushed_event(tenant_id, bid)
        except Exception:
            log.warning("concierge cancel: calendar removal failed (%s)", bid, exc_info=True)
        db.set_booking_status(bid, "cancelled", tenant_id, client=client)
        done.append(row)
        try:
            wallet.apply_attendance(
                tenant_id, row, "excused", logged_by="business",
                note="cancelled by the business from the Concierge"
                     + (f" — {payload.get('reason')}" if payload.get("reason") else ""),
                client=client)
        except Exception:
            log.warning("concierge cancel: register write failed (%s)", bid, exc_info=True)
        if row.get("status") == "confirmed":
            try:
                await _courses.on_place_freed(tenant_id, row, client=client)
            except Exception:
                log.warning("concierge cancel: freed-place hook failed (%s)", bid, exc_info=True)
    if not done:
        raise ActionError("Nothing changed — that booking is no longer in the live diary.")
    if payload.get("notify") and first:
        # The owner's reason is for the record, never the customer's note (live
        # 2026-09-06: "Kris is away" reached Kris's own note in the third person;
        # a reason can be internal). Only the factual series suffix rides along;
        # the owner edits the draft in the queue if they want to say why.
        reason = None
        if len(done) > 1:
            more = ", ".join(dates_mod.pretty_date(r.get("booking_date")) for r in done[1:6])
            reason = f"this also cancels the {len(done) - 1} later visit(s) in the series: {more}"
        try:
            out = await hand_cancel.on_hand_cancel(tenant_id, first, reason=reason, client=client)
            notes = 1 if (out or {}).get("queued") else 0
        except Exception:
            log.warning("concierge cancel: hand-cancel note failed (%s)", first.get("id"), exc_info=True)
    who = payload.get("what") or payload.get("contact") or "The booking"
    for r in done:
        db.log_agent_event(tenant_id, "booking_cancelled",
                           f"Concierge cancelled {r.get('booking_ref')} ({dates_mod.pretty_date(r.get('booking_date'))})"
                           + (" — the series keeps going" if payload.get("scope") == "one"
                              and payload.get("series_next") else "")
                           + " — confirmed by you",
                           contact=r.get("contact"), booking_id=r["id"],
                           booking_ref=r.get("booking_ref"),
                           event_date=str(r.get("booking_date") or "")[:10] or None, client=client)
    summary = f"Done — {who} on {payload.get('when')} is cancelled"
    if len(done) > 1:
        summary += f" along with {len(done) - 1} later visit{'s' if len(done) > 2 else ''}"
    elif payload.get("series_next"):
        summary += f"; the series carries on from {dates_mod.pretty_date(payload['series_next'])}"
    summary += "." + (" A note to the customer is waiting for your approval." if notes else "")
    return {"summary": summary,
            "link": {"label": "Open the diary",
                     "href": f"/app/diary?date={payload.get('date')}"}}


async def _exec_block_date(tenant_id, client, payload, edited):
    """Append the days to the diary's blackout dates — the same column the
    Diary settings page writes (dashboard.diary_settings_save)."""
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    existing = [str(d)[:10] for d in (cfg.get("blackout_dates") or [])]
    merged = sorted(set(existing) | set(payload.get("days") or []))
    if merged == sorted(set(existing)):
        raise ActionError("Nothing changed — those days were already blocked.")
    db.upsert_scheduling_config(tenant_id, {"blackout_dates": merged}, client=client)
    db.log_agent_event(tenant_id, "diary_blocked",
                       f"Concierge blocked the diary on {payload.get('when')}"
                       + (f" — {payload['reason']}" if payload.get("reason") else "")
                       + (f"; {payload['stranded']} booking(s) on those days still need moving or cancelling"
                          if payload.get("stranded") else "")
                       + " — confirmed by you",
                       event_date=payload.get("first"), client=client)
    n = payload.get("stranded") or 0
    note = (f" {n} booking{'s' if n != 1 else ''} on those days still "
            f"need{'' if n != 1 else 's'} moving or cancelling." if n else "")
    return {"summary": f"Done — the diary is blocked on {payload.get('when')}.{note}",
            "link": {"label": "Open the diary", "href": f"/app/diary?date={payload.get('first')}"}}


async def _exec_unblock_date(tenant_id, client, payload, edited):
    """Take the days out of the diary's blackout dates — the list block_date
    and Diary settings write."""
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    existing = [str(d)[:10] for d in (cfg.get("blackout_dates") or [])]
    drop = set(payload.get("days") or [])
    kept = sorted(d for d in set(existing) if d not in drop)
    if kept == sorted(set(existing)):
        raise ActionError("Nothing changed — those days were already open.")
    db.upsert_scheduling_config(tenant_id, {"blackout_dates": kept}, client=client)
    db.log_agent_event(tenant_id, "diary_unblocked",
                       f"Concierge reopened the diary on {payload.get('when')} — confirmed by you",
                       event_date=payload.get("first"), client=client)
    return {"summary": f"Done — the diary is open again on {payload.get('when')}.",
            "link": {"label": "Open the diary", "href": f"/app/diary?date={payload.get('first')}"}}


async def _exec_assign_person(tenant_id, client, payload, edited):
    from app import job_people
    t = next((p for p in job_people.team(tenant_id) if p["id"] == payload.get("person_id")), None)
    if not t:
        raise ActionError(f"{payload.get('name')} isn't on the team any more — nothing changed.")
    b = db.get_booking(payload["booking_id"], tenant_id, client=client)
    if not b or b.get("status") not in ("proposed", "confirmed"):
        raise ActionError("That booking isn't in the live diary any more — nothing changed.")
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    replaced = job_people.put_on(tenant_id, payload["booking_id"], t["id"], cfg=cfg,
                                 part=payload.get("part") or "")
    from app import job_sheets   # no login? the job sheet goes to them
    job_sheets.soon(tenant_id, payload["booking_id"])
    db.log_agent_event(tenant_id, "job_person",
                       f"Concierge put {t['name']} on {payload.get('what')}"
                       + (f" ({payload.get('ref')})" if payload.get("ref") else "")
                       + (f", instead of {', '.join(replaced)}" if replaced else "")
                       + " — confirmed by you", event_date=payload.get("day") or None, client=client)
    return {"summary": f"Done — {t['name']} is on {payload.get('what')}"
                       + (f", instead of {', '.join(replaced)}" if replaced else "") + ".",
            "link": {"label": "Open the job", "href": f"/app/diary/{payload['booking_id']}"}}


async def _exec_update_dependant(tenant_id, client, payload, edited):
    """The card's Confirm: record_edits.save_dependant, the contact card's own
    path — same merge, same timeline line, same ladder hook."""
    from app import record_edits, requirement_groups as rgroups
    inst = db.get_requirement_instance(payload["instance_id"], tenant_id, client=client)
    contact = db.crm_get_contact(payload.get("contact_id"), tenant_id, client=client) if inst else None
    groups = rgroups.clean_groups(db.list_requirement_groups(tenant_id, client=client))
    group = next((g for g in groups if g.get("id") == payload.get("group_id")), None)
    if not inst or not contact or not group:
        raise ActionError(f"{payload.get('name')}'s record has gone — nothing changed.")
    from app.booking_errors import translate_capacity
    with translate_capacity(ActionError):
        await record_edits.save_dependant(tenant_id, contact, group, inst.get("name") or payload.get("name"),
                                          {payload["key"]: payload.get("value")},
                                          via="the Concierge", client=client)
    return {"summary": f"Done — {payload.get('name')}'s {payload.get('label')} is now '{payload.get('shown')}'.",
            "link": {"label": "Open the record", "href": f"/app/crm/contacts/{payload.get('contact_id')}"}}


async def _exec_request_renewal(tenant_id, client, payload, edited):
    """Send the request from the business's mailbox, log it as a sent email
    (its own inbox wording), and leave a line on the owner's timeline."""
    from app import mailbox, record_edits
    text = (edited or "").strip()[:8000] or payload["draft_text"]
    to, subject = payload["to"], payload["subject"]
    try:
        await mailbox.send_mail(tenant_id, to, subject, text)
    except Exception:
        log.warning("renewal request send failed (%s)", payload.get("instance_id"), exc_info=True)
        raise ActionError("That didn't send — check your mailbox connection and try again. Nothing went out.")
    try:
        log_id, _ = db.insert_email_log(tenant_id, to, subject, "[renewal_request]", text,
                                        status="pending", kind="renewal_request")
        db.mark_email_log_sent(log_id, tenant_id, via="concierge", edited=bool(edited))
    except Exception:
        log.warning("renewal request email_log write failed", exc_info=True)
    db.log_agent_event(tenant_id, "renewal_request",
                       f"Asked {payload.get('owner')} for {payload.get('name')}'s up-to-date "
                       f"{payload.get('label')} (confirmed by you)",
                       direction="outbound", contact=to, subject=subject, client=client)
    if payload.get("contact_id"):
        try:
            record_edits._stamp(tenant_id, payload["contact_id"], "Asked for an up-to-date record",
                                f"{payload.get('name')}: asked for the latest {payload.get('label')} "
                                "— by you, from the Concierge")
        except Exception:
            log.warning("renewal request timeline line failed", exc_info=True)
    return {"summary": f"Sent — asked {payload.get('owner')} for {payload.get('name')}'s "
                       f"up-to-date {payload.get('label')}.",
            "link": {"label": "Open the record", "href": f"/app/crm/contacts/{payload.get('contact_id')}"}}


async def _exec_change_stay(tenant_id, client, payload, edited):
    """Confirm: the plan is re-made against the diary as it is NOW (a night
    may have filled since the card), then written by the job page's own
    apply — same in-place span, same price rule, same calendar refresh."""
    from app import record_edits
    booking = db.get_booking(payload["booking_id"], tenant_id, client=client)
    if not booking:
        raise ActionError("The stay is unchanged — it's no longer in the diary.")
    try:
        plan = record_edits.plan_stay_change(tenant_id, booking, new_arrival=payload["arrival"],
                                             new_departure=payload["departure"], client=client)
        done = await record_edits.apply_stay_change(tenant_id, booking, plan, via="the Concierge", client=client)
    except record_edits.EditError as e:
        raise ActionError(f"The stay is unchanged — {e}")
    tail = ""
    if payload.get("notify") and booking.get("contact"):
        if _stay_note_needed(tenant_id, client, booking, plan):
            # Written in the background (test run 3.1, 2026-09-24: the owner's
            # "yes" waited 3.5 s of the 5.7 s on the model writing this note).
            # "Done" comes straight back; the note lands in approvals moments later.
            _background(_queue_stay_note(tenant_id, client, booking, plan),
                        f"change_stay note ({booking.get('id')})")
            tail = " A note to the customer is being written for your approval."
        else:
            tail = " No note needed — they still have these dates."
    link = {"label": "Open the diary", "href": f"/app/diary?date={plan['new_date']}"}
    if "draft invoice for the extra" in done:
        # The Done line names a draft invoice: the button opens it.
        drafts = db.extra_night_drafts(tenant_id, booking["id"], client=client)
        if drafts:
            link = {"label": f"Open {drafts[0].get('invoice_ref') or 'the invoice'}",
                    "href": f"/app/invoices/{drafts[0]['id']}"}
    return {"summary": done + tail,
            "link": link}


async def _exec_update_contact(tenant_id, client, payload, edited):
    """A field: merged into custom_data (overwrite — the human's confirmed edit;
    record_versions bumps so the portal's cached record re-reads) with a
    timeline line saying what changed. A note: a dated 'note' activity on the
    customer's timeline."""
    # One code path for both doors (src/app/record_edits.py, 2026-09-06): the card
    # and the contact page's pencil write the same rows the same way.
    from app import record_edits
    cid = payload["contact_id"]
    contact = db.crm_get_contact(cid, tenant_id, client=client)
    if not contact:
        raise ActionError("That customer's record has gone — nothing changed.")
    try:
        if payload.get("mode") == "field":
            defs = db.crm_list_field_definitions(tenant_id, entity="contact", client=client) or []
            field = next((d for d in defs if d.get("key") == payload.get("key")), None)
            if not field:
                raise ActionError("That field no longer exists — nothing changed.")
            _, shown = record_edits.set_contact_field(tenant_id, contact, field, payload.get("value"),
                                                      via="the Concierge", client=client)
            return {"summary": f"Done — {payload.get('name')}'s {field.get('label')} is now '{shown}'.",
                    "link": {"label": "Open the record", "href": f"/app/crm/contacts/{cid}"}}
        record_edits.add_contact_note(tenant_id, contact, payload.get("content") or "",
                                      via="the Concierge", client=client)
    except record_edits.EditError as e:
        raise ActionError(f"Nothing changed — {e}")
    return {"summary": f"Done — the note is on {payload.get('name')}'s record.",
            "link": {"label": "Open the record", "href": f"/app/crm/contacts/{cid}"}}


async def _exec_update_deal_stage(tenant_id, client, payload, edited):
    stage_type = payload.get("stage_type") or "active"
    fields = {"stage_id": payload["stage_id"],
              "status": "open" if stage_type == "active" else stage_type,
              "closed_at": None if stage_type == "active"
              else datetime.now(timezone.utc).isoformat()}
    db.crm_update_deal(payload["deal_id"], tenant_id, fields, client=client)
    db.log_agent_event(tenant_id, "deal_stage",
                       f"Concierge moved “{payload.get('title')}” from "
                       f"{payload.get('old_stage')} to {payload.get('stage_name')} "
                       "— confirmed by you", client=client)
    contact_id = payload.get("contact_id")
    link = ({"label": "Open the deal", "href": f"/app/crm/contacts/{contact_id}"}
            if contact_id else {"label": "Open the pipeline", "href": "/app/crm/pipeline"})
    return {"summary": f"Done — “{payload.get('title')}” is now "
                       f"{payload.get('stage_name')}.", "link": link}


async def _exec_create_followup(tenant_id, client, payload, edited):
    task = db.crm_create_task(tenant_id, title=payload["title"],
                              due_date=payload["due_date"],
                              contact_id=payload["contact_id"], source="manual")
    db.log_agent_event(tenant_id, "task_created",
                       f"Concierge set a follow-up for {payload.get('contact_name')} "
                       f"due {payload['due_date']} — confirmed by you", client=client)
    return {"summary": f"Done — follow-up for {payload.get('contact_name')} on "
                       f"{payload['due_date']}.",
            "link": {"label": "Open the contact",
                     "href": f"/app/crm/contacts/{payload['contact_id']}"}}


async def _exec_create_contact(tenant_id, client, payload, edited):
    from app import crm
    company_id = None
    dom = crm.domain_of(payload["email"])
    if dom and crm.is_business_domain(dom):
        company = db.crm_get_or_create_company(tenant_id, dom,
                                               name=payload.get("company") or None)
        company_id = company.get("id")
    contact = db.crm_get_or_create_contact(tenant_id, payload["email"],
                                           name=payload.get("name") or None,
                                           company_id=company_id)
    extra = {}
    if payload.get("phone"):
        extra["phone"] = payload["phone"]
    if payload.get("name") and not contact.get("name"):
        extra["name"] = payload["name"]
    if extra:
        db.crm_update_contact(contact["id"], tenant_id, extra)
    db.log_agent_event(tenant_id, "crm_autofill",
                       f"Concierge added the contact {payload.get('name') or payload['email']} "
                       "— confirmed by you", contact=payload["email"], client=client)
    return {"summary": f"Done — {payload.get('name') or payload['email']} is in your CRM.",
            "link": {"label": "Open the contact",
                     "href": f"/app/crm/contacts/{contact['id']}"}}


async def _exec_define_dependants(tenant_id, client, payload, edited):
    from app import requirement_groups as rgroups
    group = rgroups.clean_group(payload.get("group") or {})
    if not group:
        raise ActionError("That kind is no longer valid.")
    current = rgroups.clean_groups(db.list_requirement_groups(tenant_id, client=client))
    if group.get("id"):
        current = [group if g.get("id") == group["id"] else g for g in current]
    else:
        if len(current) >= rgroups.MAX_GROUPS:
            raise ActionError(f"You already have {rgroups.MAX_GROUPS} kinds — remove one on the Dependants page first.")
        current = current + [group]
    db.save_requirement_groups(tenant_id, current, client=client)
    db.log_agent_event(tenant_id, "dependants",
                       f"Concierge defined the kind “{group['label']}” — confirmed by you", client=client)
    return {"summary": f"Done — bookings can be about {group['label_plural'].lower()} now; the agent asks for "
                       "each one and remembers them on the customer's record.",
            "link": {"label": "Open Dependants", "href": "/app/diary/dependants"}}


async def _exec_add_kb_entry(tenant_id, client, payload, edited):
    from app import knowledge_review
    row = knowledge_review.stage(tenant_id,[{"label":payload["title"],"content":payload["content"]}],
                                 origin="Assistant suggestion",return_to="/app/knowledge")
    return {"summary":f"“{payload['title']}” is pending review. Nothing has been saved to Knowledge.",
            "link":{"label":"Review Knowledge","href":f"/onboarding/knowledge/review/{row['id']}"}}



async def _exec_invoice_create(tenant_id, client, payload, edited):
    inv = db.create_invoice(
        tenant_id, payload["contact"], payload.get("contact_name"),
        None, None,
        [{"description": payload["description"], "amount": payload["amount"],
          "quantity": 1, "unit_price": payload["amount"],
          "booking_id": payload.get("booking_id") or None}],
        client=client, contact_id=payload.get("contact_id") or None,
        booking_id=payload.get("booking_id") or None,
        due_date=payload.get("due_date") or None, created_by="ai")
    if not inv:
        raise ActionError("The invoice didn't save — try again.")
    ref = inv.get("invoice_ref")
    db.log_agent_event(tenant_id, "invoice_created",
                       f"Concierge created invoice {ref} for "
                       f"{payload.get('contact_name') or payload['contact']} "
                       f"(£{float(inv.get('total') or 0):,.2f}) — confirmed by you",
                       contact=payload["contact"], client=client)
    return {"summary": f"Done — invoice {ref} created as a draft "
                       f"(£{float(inv.get('total') or 0):,.2f}, due "
                       f"{str(inv.get('due_date') or '')[:10]}). Ask me to send it "
                       "when you're ready.",
            "link": {"label": "Open the invoice", "href": f"/app/invoices/{inv['id']}"}}


async def _exec_invoice_send(tenant_id, client, payload, edited):
    from app import invoice_send as invsend
    inv = db.get_invoice(payload["invoice_id"], tenant_id, client=client)
    if not inv or inv.get("status") in ("paid", "cancelled", "void"):
        raise ActionError("That invoice isn't sendable any more.")
    changed = _changed_since_prepared(
        payload, inv, db.list_invoice_lines(inv["id"], tenant_id, client=client),
        "Invoice", payload.get("ref") or inv.get("invoice_ref"), "sent")
    if changed:
        return changed
    body = (edited or "").strip()[:8000] or payload.get("draft_text") or None
    try:
        await invsend.send_invoice_email(tenant_id, inv, client=client, via="concierge",
                                         body_override=body)
    except Exception:
        log.warning("concierge invoice send failed (%s)", payload["invoice_id"],
                    exc_info=True)
        raise ActionError("The email didn't send — check your mailbox connection "
                          "and try again. The invoice is unchanged.")
    return {"summary": f"Done — invoice {payload['ref']} emailed to {payload['to']}.",
            "link": {"label": "Open the invoice",
                     "href": f"/app/invoices/{payload['invoice_id']}"}}


async def _exec_invoice_mark_paid(tenant_id, client, payload, edited):
    from app import payments
    if isinstance(payload.get("shown"), dict):
        cur = db.get_invoice(payload["invoice_id"], tenant_id, client=client)
        if cur:
            changed = _changed_since_prepared(
                payload, cur, db.list_invoice_lines(cur["id"], tenant_id, client=client),
                "Invoice", payload.get("ref") or cur.get("invoice_ref"), "settled")
            if changed:
                return changed
    row = db.mark_invoice_paid(payload["invoice_id"], tenant_id,
                               payment_method=payload["payment_method"],
                               client=client)
    if not row:
        raise ActionError(f"Invoice {payload['ref']} is already settled — "
                          "nothing changed.")
    payments.invoice_paid_followup(
        tenant_id, row, via=f"marked paid via concierge "
                            f"({payload['payment_method'].replace('_', ' ')})")
    db.log_agent_event(tenant_id, "invoice_paid",
                       f"Concierge marked invoice {payload['ref']} paid "
                       f"({payload['payment_method'].replace('_', ' ')}) — "
                       "confirmed by you",
                       contact=row.get("customer_email"), client=client)
    return {"summary": f"Done — invoice {payload['ref']} is marked paid "
                       f"(£{payload['total']:,.2f}).",
            "link": {"label": "Open the invoice",
                     "href": f"/app/invoices/{payload['invoice_id']}"}}


async def _exec_invoice_reminder_draft(tenant_id, client, payload, edited):
    from app import invoice_send as invsend
    text = (edited or "").strip()[:8000] or payload["draft_text"]
    inv = db.get_invoice(payload["invoice_id"], tenant_id, client=client)
    if not inv or inv.get("status") not in ("sent", "overdue", "partially_paid"):
        raise ActionError("That invoice isn't awaiting payment any more.")
    try:
        await invsend.send_reminder_email(tenant_id, inv, text, client=client,
                                          via="concierge")
    except Exception:
        log.warning("concierge reminder send failed (%s)", payload["invoice_id"],
                    exc_info=True)
        raise ActionError("The reminder didn't send — check your mailbox "
                          "connection and try again.")
    return {"summary": f"Done — payment reminder for {payload['ref']} sent to "
                       f"{payload['to']}.",
            "link": {"label": "Open the invoice",
                     "href": f"/app/invoices/{payload['invoice_id']}"}}


def _payload_lines(payload: dict) -> list[dict]:
    """The staged replacement lines, re-read with the same rules the stager used (the
    row is ours, but nothing about a line is trusted blind at the moment it is written)."""
    out = []
    for l in (payload or {}).get("lines") or []:
        desc = str((l or {}).get("description") or "").strip()[:200]
        if not desc:
            raise ActionError("A line on that card has no description.")
        out.append({**l, "description": desc, "amount": _amount_cell((l or {}).get("amount"), "a line")})
    if not out or len(out) > MAX_EDIT_LINES:
        raise ActionError("That card has no usable lines.")
    return out


async def _exec_invoice_edit_draft(tenant_id, client, payload, edited):
    ref = payload.get("ref") or "That invoice"
    inv = db.get_invoice(payload.get("invoice_id") or "", tenant_id, client=client)
    if not inv:
        return _refused(f"Invoice {ref} isn't there any more, so nothing was changed.")
    if inv.get("status") != "draft":
        return _refused(f"Invoice {ref} is no longer a draft, so nothing was changed.")
    current = db.list_invoice_lines(inv["id"], tenant_id, client=client) or []
    if _line_sig(current) != _line_sig(payload.get("before")):
        # edited in the main app since the card, or this very card already ran
        return _refused(f"Invoice {ref} was changed since I prepared this, so nothing was "
                        "changed. Ask me again and I'll prepare it afresh.")
    try:
        lines = _payload_lines(payload)
    except ActionError as e:
        return _refused(f"{e} Nothing was changed.")
    if not db.update_draft_invoice_lines(inv["id"], tenant_id, lines, client=client):
        return _refused(f"Invoice {ref} is no longer a draft, so nothing was changed.")
    fresh = db.get_invoice(inv["id"], tenant_id, client=client) or {}
    total = float(fresh.get("total") if fresh.get("total") is not None
                  else payload.get("total_after") or 0)
    before = float(payload.get("total_before") if payload.get("total_before") is not None
                   else inv.get("total") or 0)
    db.log_agent_event(tenant_id, "invoice_edited",
                       f"Concierge changed draft invoice {ref} for {payload.get('who') or 'the customer'}: "
                       f"£{before:,.2f} → £{total:,.2f} — confirmed by you",
                       contact=inv.get("customer_email"), client=client)
    return {"summary": f"Done — invoice {ref} now totals £{total:,.2f} (it was £{before:,.2f}). "
                       "It is still a draft.",
            "link": {"label": "Open the invoice", "href": f"/app/invoices/{inv['id']}"}}


async def _exec_invoice_delete_draft(tenant_id, client, payload, edited):
    ref = payload.get("ref") or "That invoice"
    inv = db.get_invoice(payload.get("invoice_id") or "", tenant_id, client=client)
    if not inv:
        return _refused(f"Invoice {ref} isn't there any more, so nothing was deleted.")
    if inv.get("status") != "draft":
        return _refused(f"Invoice {ref} is no longer a draft, so nothing was deleted.")
    clause = _invoice_touched_clause(inv)
    if clause:
        return _refused(f"Invoice {ref} {clause}, so nothing was deleted.")
    quote_ref = db.invoice_linked_quote_ref(inv["id"], tenant_id, client=client)
    if quote_ref:
        return _refused(f"Invoice {ref} was made from quote {quote_ref}, so nothing was deleted.")
    total = round(float(inv.get("total") or 0), 2)
    if total != round(float(payload.get("total") or 0), 2):
        return _refused(f"Invoice {ref} was changed since I prepared this, so nothing was deleted. "
                        "Ask me again and I'll prepare it afresh.")
    if payload.get("lines") and _line_sig(db.list_invoice_lines(inv["id"], tenant_id, client=client))             != _line_sig(payload["lines"]):
        return _refused(f"Invoice {ref} was changed since I prepared this, so nothing was deleted. "
                        "Ask me again and I'll prepare it afresh.")
    if not db.delete_draft_invoice(inv["id"], tenant_id, client=client):
        return _refused(f"Invoice {ref} was changed just now, so nothing was deleted.")
    who = payload.get("who") or inv.get("customer_name") or inv.get("customer_email") or "the customer"
    db.log_agent_event(tenant_id, "invoice_deleted",
                       f"Concierge deleted draft invoice {ref} (£{total:,.2f} for {who}) "
                       "— confirmed by you",
                       contact=inv.get("customer_email"), client=client)
    return {"summary": f"Done — draft invoice {ref} (£{total:,.2f} for {who}) is deleted. "
                       "It can't be brought back.",
            "link": {"label": "Open your invoices", "href": "/app/invoices"}}


async def _exec_quote_create(tenant_id, client, payload, edited):
    deposit = payload.get("deposit_percent")
    quote = db.create_quote(
        tenant_id, payload["contact"], payload.get("contact_name"),
        [{"description": payload["description"], "amount": payload["amount"],
          "quantity": 1, "unit_price": payload["amount"],
          "booking_id": payload.get("booking_id") or None}],
        client=client, contact_id=payload.get("contact_id") or None,
        booking_id=payload.get("booking_id") or None,
        title=payload["description"],
        valid_until=payload.get("valid_until") or None,
        deposit_percent=(None if deposit == "" else float(deposit)),
        created_by="ai")
    if not quote:
        raise ActionError("The quote didn't save — try again.")
    ref = quote.get("quote_ref")
    db.log_agent_event(tenant_id, "quote_created",
                       f"Concierge created quote {ref} for "
                       f"{payload.get('contact_name') or payload['contact']} "
                       f"(£{float(quote.get('total') or 0):,.2f}) — confirmed by you",
                       contact=payload["contact"], client=client)
    return {"summary": f"Done — quote {ref} created as a draft "
                       f"(£{float(quote.get('total') or 0):,.2f}, valid until "
                       f"{str(quote.get('valid_until') or '')[:10]}). Ask me to "
                       "send it when you're ready.",
            "link": {"label": "Open the quote", "href": f"/app/quotes/{quote['id']}"}}


async def _exec_quote_send(tenant_id, client, payload, edited):
    from app import quote_send as qsend
    quote = db.get_quote(payload["quote_id"], tenant_id, client=client)
    if not quote or quote.get("status") not in ("draft", "sent"):
        raise ActionError("That quote isn't sendable any more.")
    changed = _changed_since_prepared(
        payload, quote, db.list_quote_lines(quote["id"], tenant_id, client=client),
        "Quote", payload.get("ref") or quote.get("quote_ref"), "sent")
    if changed:
        return changed
    body = (edited or "").strip()[:8000] or payload.get("draft_text") or None
    try:
        await qsend.send_quote_email(tenant_id, quote, client=client,
                                     via="concierge", body_override=body)
    except Exception:
        log.warning("concierge quote send failed (%s)", payload["quote_id"],
                    exc_info=True)
        raise ActionError("The email didn't send — check your mailbox connection "
                          "and try again. The quote is unchanged.")
    return {"summary": f"Done — quote {payload['ref']} emailed to {payload['to']}. "
                       "You'll hear the moment they open it.",
            "link": {"label": "Open the quote",
                     "href": f"/app/quotes/{payload['quote_id']}"}}


async def _exec_quote_reminder_draft(tenant_id, client, payload, edited):
    from app import quote_send as qsend
    text = (edited or "").strip()[:8000] or payload["draft_text"]
    quote = db.get_quote(payload["quote_id"], tenant_id, client=client)
    if not quote or quote.get("status") != "sent":
        raise ActionError("That quote isn't awaiting an answer any more.")
    try:
        await qsend.send_quote_reminder(tenant_id, quote, text, client=client,
                                        via="concierge")
    except Exception:
        log.warning("concierge quote reminder failed (%s)", payload["quote_id"],
                    exc_info=True)
        raise ActionError("The follow-up didn't send — check your mailbox "
                          "connection and try again.")
    return {"summary": f"Done — follow-up on {payload['ref']} sent to "
                       f"{payload['to']}.",
            "link": {"label": "Open the quote",
                     "href": f"/app/quotes/{payload['quote_id']}"}}


async def _exec_quote_mark_accepted(tenant_id, client, payload, edited):
    from app import quote_public
    row = db.accept_quote(payload["quote_id"], tenant_id, payload["accepted_name"])
    if not row:
        raise ActionError(f"Quote {payload['ref']} couldn't be marked accepted — "
                          "it may have just been answered online. Nothing changed.")
    quote_public.accepted_side_effects(tenant_id, row)
    quote_public.mint_deposit_invoice(tenant_id, row)
    db.log_agent_event(tenant_id, "quote_accepted",
                       f"Concierge marked quote {payload['ref']} accepted "
                       f"({payload['accepted_name']}) — confirmed by you",
                       contact=row.get("customer_email"), client=client)
    summary = (f"Done — quote {payload['ref']} is accepted "
               f"(£{payload['total']:,.2f}).")
    if row.get("deposit_percent"):
        summary += " The deposit invoice is minted — ask me to send it."
    else:
        summary += " Ask me to convert it to an invoice when the work's done."
    return {"summary": summary,
            "link": {"label": "Open the quote",
                     "href": f"/app/quotes/{payload['quote_id']}"}}


async def _exec_quote_mark_declined(tenant_id, client, payload, edited):
    row = db.decline_quote(payload["quote_id"], tenant_id,
                           payload.get("reason") or "Recorded by you")
    if not row:
        raise ActionError(f"Quote {payload['ref']} couldn't be marked declined — "
                          "it may have just been answered online. Nothing changed.")
    db.log_agent_event(tenant_id, "quote_declined",
                       f"Concierge marked quote {payload['ref']} declined — "
                       "confirmed by you",
                       contact=row.get("customer_email"), client=client)
    return {"summary": f"Done — quote {payload['ref']} is marked declined.",
            "link": {"label": "Open the quote",
                     "href": f"/app/quotes/{payload['quote_id']}"}}


async def _exec_quote_convert_invoice(tenant_id, client, payload, edited):
    from app import quoting
    quote = db.get_quote(payload["quote_id"], tenant_id, client=client)
    if not quote or quote.get("status") != "accepted":
        raise ActionError("That quote isn't in an accepted state any more.")
    dep = (db.get_invoice(quote["deposit_invoice_id"], tenant_id, client=client)
           if quote.get("deposit_invoice_id") else None)
    lines = quoting.conversion_lines(quote, dep)
    if not lines:
        raise ActionError("There's nothing on that quote to invoice.")
    inv = db.create_invoice(
        tenant_id, quote.get("customer_email"), quote.get("customer_name"),
        None, None, lines, client=client,
        contact_id=quote.get("contact_id"), deal_id=quote.get("deal_id"),
        booking_id=quote.get("booking_id"),
        notes=f"From quote {quote.get('quote_ref') or ''}".strip(),
        customer_reference=quote.get("customer_reference"),
        prices_include_vat=quote.get("prices_include_vat") is True,   # inherits the quote's basis
        created_by="ai")
    if not inv:
        raise ActionError("The invoice didn't save — the quote is unchanged.")
    db.link_quote_invoice(payload["quote_id"], tenant_id, inv["id"], client=client)
    db.log_agent_event(tenant_id, "invoice_created",
                       f"Concierge converted quote {payload['ref']} into invoice "
                       f"{inv.get('invoice_ref')} — confirmed by you",
                       contact=quote.get("customer_email"), client=client)
    return {"summary": f"Done — quote {payload['ref']} is now draft invoice "
                       f"{inv.get('invoice_ref')} "
                       f"(£{float(inv.get('total') or 0):,.2f}). Ask me to send "
                       "it when you're ready.",
            "link": {"label": "Open the invoice",
                     "href": f"/app/invoices/{inv['id']}"}}


async def _exec_quote_cancel(tenant_id, client, payload, edited):
    quote = db.get_quote(payload["quote_id"], tenant_id, client=client)
    if not quote or quote.get("status") not in ("draft", "sent"):
        raise ActionError(f"Quote {payload['ref']} can't be cancelled any more.")
    db.set_quote_status(payload["quote_id"], tenant_id, "cancelled", client=client)
    db.log_agent_event(tenant_id, "quote_cancelled",
                       f"Concierge cancelled quote {payload['ref']} — confirmed "
                       "by you",
                       contact=quote.get("customer_email"), client=client)
    return {"summary": f"Done — quote {payload['ref']} is cancelled.",
            "link": {"label": "Open your quotes", "href": "/app/quotes"}}


async def _exec_quote_edit_draft(tenant_id, client, payload, edited):
    ref = payload.get("ref") or "That quote"
    quote = db.get_quote(payload.get("quote_id") or "", tenant_id, client=client)
    if not quote:
        return _refused(f"Quote {ref} isn't there any more, so nothing was changed.")
    if quote.get("status") != "draft":
        return _refused(f"Quote {ref} is no longer a draft, so nothing was changed.")
    current = db.list_quote_lines(quote["id"], tenant_id, client=client) or []
    if _line_sig(current) != _line_sig(payload.get("before")):
        return _refused(f"Quote {ref} was changed since I prepared this, so nothing was "
                        "changed. Ask me again and I'll prepare it afresh.")
    try:
        lines = _payload_lines(payload)
    except ActionError as e:
        return _refused(f"{e} Nothing was changed.")
    if not db.update_draft_quote_lines(quote["id"], tenant_id, lines, client=client):
        return _refused(f"Quote {ref} is no longer a draft, so nothing was changed.")
    fresh = db.get_quote(quote["id"], tenant_id, client=client) or {}
    total = float(fresh.get("total") if fresh.get("total") is not None
                  else payload.get("total_after") or 0)
    before = float(payload.get("total_before") if payload.get("total_before") is not None
                   else quote.get("total") or 0)
    db.log_agent_event(tenant_id, "quote_edited",
                       f"Concierge changed draft quote {ref} for {payload.get('who') or 'the customer'}: "
                       f"£{before:,.2f} → £{total:,.2f} — confirmed by you",
                       contact=quote.get("customer_email"), client=client)
    return {"summary": f"Done — quote {ref} now totals £{total:,.2f} (it was £{before:,.2f}). "
                       "It is still a draft.",
            "link": {"label": "Open the quote", "href": f"/app/quotes/{quote['id']}"}}


async def _exec_quote_delete_draft(tenant_id, client, payload, edited):
    ref = payload.get("ref") or "That quote"
    quote = db.get_quote(payload.get("quote_id") or "", tenant_id, client=client)
    if not quote:
        return _refused(f"Quote {ref} isn't there any more, so nothing was deleted.")
    if quote.get("status") != "draft":
        return _refused(f"Quote {ref} is no longer a draft, so nothing was deleted.")
    clause = _quote_touched_clause(quote)
    if clause:
        return _refused(f"Quote {ref} {clause}, so nothing was deleted.")
    total = round(float(quote.get("total") or 0), 2)
    if total != round(float(payload.get("total") or 0), 2):
        return _refused(f"Quote {ref} was changed since I prepared this, so nothing was deleted. "
                        "Ask me again and I'll prepare it afresh.")
    if payload.get("lines") and _line_sig(db.list_quote_lines(quote["id"], tenant_id, client=client))             != _line_sig(payload["lines"]):
        return _refused(f"Quote {ref} was changed since I prepared this, so nothing was deleted. "
                        "Ask me again and I'll prepare it afresh.")
    if not db.delete_draft_quote(quote["id"], tenant_id, client=client):
        return _refused(f"Quote {ref} was changed just now, so nothing was deleted.")
    who = payload.get("who") or quote.get("customer_name") or quote.get("customer_email") or "the customer"
    db.log_agent_event(tenant_id, "quote_deleted",
                       f"Concierge deleted draft quote {ref} (£{total:,.2f} for {who}) "
                       "— confirmed by you",
                       contact=quote.get("customer_email"), client=client)
    return {"summary": f"Done — draft quote {ref} (£{total:,.2f} for {who}) is deleted. "
                       "It can't be brought back.",
            "link": {"label": "Open your quotes", "href": "/app/quotes"}}


_EXECUTORS = {
    "draft_reply": _exec_draft_reply,
    "cancel_booking": _exec_cancel_booking,
    "confirm_booking": _exec_confirm_booking,
    "block_date": _exec_block_date,
    "unblock_date": _exec_unblock_date,
    "assign_person": _exec_assign_person,
    "update_contact": _exec_update_contact,
    "update_dependant": _exec_update_dependant,
    "change_stay": _exec_change_stay,
    "request_renewal": _exec_request_renewal,
    "move_booking": _exec_move_booking,
    "update_deal_stage": _exec_update_deal_stage,
    "update_deal_value": _exec_update_deal_value,
    "create_followup": _exec_create_followup,
    "create_contact": _exec_create_contact,
    "add_kb_entry": _exec_add_kb_entry,
    "define_dependants": _exec_define_dependants,
    "invoice_create": _exec_invoice_create,
    "invoice_send": _exec_invoice_send,
    "invoice_mark_paid": _exec_invoice_mark_paid,
    "invoice_reminder_draft": _exec_invoice_reminder_draft,
    "invoice_edit_draft": _exec_invoice_edit_draft,
    "invoice_delete_draft": _exec_invoice_delete_draft,
    "quote_create": _exec_quote_create,
    "quote_send": _exec_quote_send,
    "quote_reminder_draft": _exec_quote_reminder_draft,
    "quote_mark_accepted": _exec_quote_mark_accepted,
    "quote_mark_declined": _exec_quote_mark_declined,
    "quote_convert_invoice": _exec_quote_convert_invoice,
    "quote_cancel": _exec_quote_cancel,
    "quote_edit_draft": _exec_quote_edit_draft,
    "quote_delete_draft": _exec_quote_delete_draft,
}
