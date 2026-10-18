"""Time saved — what BLAM8 did that a person would otherwise have done by hand
(Kris 2026-09-24: "each action, if done by a real person, takes ~X amount of
time ... start tracking how much time has been saved").

Two sources, one number:

  * Emails — every sent email_log row BLAM8 wrote. Saving = the minutes a
    person would take to write that kind of email from scratch, minus the
    human time actually spent on it: its HRT stamp (review_seconds) when it
    has one, UNTRACKED_REVIEW_S when it doesn't, nothing for an auto-send —
    plus, for a voice-asked draft, how long the owner spoke asking for it.
  * Concierge actions — every executed proposal. Saving = the minutes the
    same change takes by hand in the app, minus the time the card sat on
    screen before Confirm (executed_at - created_at) and, by voice, the
    owner's speaking time since the last card (payload._spoken_ms, measured
    by voice-live.js — the ask plus any back-and-forth that led to it).

NET, never gross: a 2-minute job someone spent 90 s confirming saved 30 s.
A saving never goes below zero. Failed, expired, cancelled and superseded
proposals count nothing.

No double counting: an email the Concierge sent itself (sent_via='concierge'
— invoice, reminder, renewal request) is part of its action's minutes and is
skipped here; a Concierge action whose only output is a DRAFT (draft_reply)
counts 0 — the email earns its minutes when it's sent.

⚠ The minutes below are ESTIMATES (2026-09-24, first pass). Calibrate each by
timing it by hand on the rig and note the source beside it; until then the
number is operator-only (/admin), never shown to a tenant.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ── by-hand minutes per email kind (write it from scratch, send it) ──────────
EMAIL_MINUTES: dict[str, float] = {
    "inbound": 6,             # read the enquiry, check diary/records, write the reply
    "owner_message": 4,       # the owner knows what to say; still has to write it
    "invoice": 3,             # covering email for an invoice
    "quote": 3,
    "invoice_reminder": 3,
    "quote_reminder": 3,
    "renewal_request": 3,
    "reminder": 2,            # appointment reminder
    "run_sheet": 10,          # events sign-off / run sheet
}
EMAIL_DEFAULT_MINUTES = 4
# A human send with no HRT stamp (pre-08-31, no-JS, voice "yes"): assume this
# much review rather than none. Measured platform HRT is ~22 s (09-24), so a
# minute keeps the guess on the conservative side.
UNTRACKED_REVIEW_S = 60

# ── by-hand minutes per Concierge action (the non-email part) ────────────────
ACTION_MINUTES: dict[str, float] = {
    "draft_reply": 0,             # the draft's send counts as an email
    "move_booking": 4,            # find it, check the new slot, move it, fix the calendar
    "confirm_booking": 2,         # open the diary, find it, confirm it
    "cancel_booking": 3,          # + CANCEL_EXTRA_VISIT_MINUTES per later visit in a series
    "block_date": 1,
    "unblock_date": 1,
    "assign_person": 1,      # open the job, pick the person, save
    "change_stay": 4,             # re-dates a stay: diary, nights, price
    "update_contact": 2,
    "update_dependant": 2,
    "create_contact": 3,
    "define_dependants": 5,
    "add_kb_entry": 3,
    "request_renewal": 4,         # look up the document + write the request
    "update_deal_stage": 1,
    "update_deal_value": 1,
    "create_followup": 2,
    "invoice_create": 8,          # lines, prices, customer, PDF
    "invoice_send": 3,
    "invoice_mark_paid": 2,
    "invoice_reminder_draft": 4,  # check what's owed + write the chaser (sent by the action)
    "invoice_edit_draft": 3,      # open the draft, change the lines, check the total, save
    "invoice_delete_draft": 1,    # find the draft and bin it
    "quote_create": 10,
    "quote_send": 3,
    "quote_reminder_draft": 4,
    "quote_mark_accepted": 1,
    "quote_mark_declined": 1,
    "quote_convert_invoice": 5,
    "quote_cancel": 2,
    "quote_edit_draft": 3,
    "quote_delete_draft": 1,
}
CANCEL_EXTRA_VISIT_MINUTES = 1


def email_saving_s(row: dict) -> float:
    """Net seconds saved by one sent email_log row (0 for rows that don't count)."""
    if row.get("status") != "sent" or row.get("sent_via") == "concierge":
        return 0.0
    by_hand = EMAIL_MINUTES.get(row.get("kind") or "inbound", EMAIL_DEFAULT_MINUTES) * 60
    if row.get("sent_via") == "auto":
        spent = 0
    elif isinstance(row.get("review_seconds"), (int, float)):
        spent = row["review_seconds"]
    else:
        spent = UNTRACKED_REVIEW_S
    spent += row.get("spoken_seconds") or 0
    return max(0.0, by_hand - spent)


def _kind_minutes(kind: str, payload: dict) -> float:
    m = ACTION_MINUTES.get(kind, 0)
    if kind == "cancel_booking":
        m += CANCEL_EXTRA_VISIT_MINUTES * len(payload.get("later_ids") or [])
    return m


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def action_gross_minutes(row: dict) -> float:
    """By-hand minutes for one executed proposal. A bulk card counts the items
    that actually RAN (its result lists each ticked item's outcome)."""
    kind, payload = row.get("kind") or "", row.get("payload") or {}
    if kind != "bulk":
        return _kind_minutes(kind, payload)
    items = payload.get("items") or []
    ran = [r.get("index") for r in ((row.get("result") or {}).get("results") or []) if r.get("ok")]
    return sum(_kind_minutes(items[i].get("kind") or "", items[i].get("payload") or {})
               for i in ran if isinstance(i, int) and 0 <= i < len(items))


def action_saving_s(row: dict) -> float:
    """Net seconds saved by one concierge_proposals row (0 unless executed)."""
    from app import proposal_outcomes
    if proposal_outcomes.state(row) != "completed":
        return 0.0
    gross = action_gross_minutes(row) * 60
    a, b = _parse(row.get("created_at")), _parse(row.get("executed_at"))
    spent = max(0.0, (b - a).total_seconds()) if a and b else 0.0
    spent += ((row.get("payload") or {}).get("_spoken_ms") or 0) / 1000
    return max(0.0, gross - spent)


def tally(emails: list[dict], actions: list[dict]) -> dict:
    """{'emails': {'n','s'}, 'actions': {'n','s'}, 'total_s'} — n counts only
    rows that saved something."""
    out = {"emails": {"n": 0, "s": 0.0}, "actions": {"n": 0, "s": 0.0}}
    for key, rows, fn in (("emails", emails, email_saving_s), ("actions", actions, action_saving_s)):
        for r in rows:
            s = fn(r)
            if s > 0:
                out[key]["n"] += 1
                out[key]["s"] += s
    out["total_s"] = out["emails"]["s"] + out["actions"]["s"]
    return out


def by_tenant(emails: list[dict], actions: list[dict]) -> dict[str, dict]:
    """tally() per tenant_id."""
    groups: dict[str, tuple[list, list]] = {}
    for i, rows in ((0, emails), (1, actions)):
        for r in rows:
            if r.get("tenant_id"):
                groups.setdefault(r["tenant_id"], ([], []))[i].append(r)
    return {t: tally(e, a) for t, (e, a) in groups.items()}


def summary(tenant_id: str | None = None) -> dict[str, dict]:
    """All-time time-saved per tenant (cross-tenant when tenant_id is None).
    Best-effort: {} on a read failure and the console omits the numbers."""
    from app import db
    try:
        return by_tenant(db.time_saved_email_rows(tenant_id), db.time_saved_action_rows(tenant_id))
    except Exception:
        log.warning("time saved unavailable", exc_info=True)
        return {}
