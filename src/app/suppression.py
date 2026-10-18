"""Draft suppression — stop drafting for contacts whose drafts the owner ignores.

docs/modules/module-draft-suppression.md. Every function here is BEST-EFFORT: a stats
failure must never cost a tenant their draft (mirror of db.log_agent_event's
stance), so check_inbound fails open to "draft" and the writers swallow errors.

The state machine (contact_draft_stats.status, per tenant+sender):

  active ──(3rd consecutive unsent draft)──▶ suppressed
  suppressed ──(2 more inbounds, owner silent)──▶ permanently_ignored  [auto]
  suppressed ──(owner: Ignore permanently)──▶ permanently_ignored       [owner]
  suppressed ──(owner: Resume / a draft is sent)──▶ active
  permanently_ignored ──(owner: Un-ignore / Resume)──▶ active

While suppressed or permanently ignored the pipeline skips the draft-generation
LLM call entirely — the inbound still lands in email_log under FYI. The safety
valve: an email that looks unusual for that sender (urgency keywords, or 3x
their average length) still gets a ONE-OFF draft without touching the streak.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import db

log = logging.getLogger("blam8.suppression")

# 1-2 unsent drafts could just be a busy week; 3 is a pattern worth acting on.
THRESHOLD = 3
# Inbounds tolerated while 'suppressed' before the banner auto-resolves to
# permanently ignored (3 drafts + 2 more emails = 5 total, owner silent throughout).
AUTO_SUPPRESS_AFTER = 2
# Re-engagement heuristic — deliberately dumb (docs §7): subject keywords…
URGENCY_KEYWORDS = ("urgent", "complaint", "cancel", "legal", "final notice",
                    "final demand", "overdue", "deadline", "asap")
# …or a body 3x the sender's average length.
LENGTH_BREAK_FACTOR = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(address: str | None) -> str | None:
    address = (address or "").strip().lower()
    return address or None


def looks_reengaging(subject: str | None, body: str | None, prior_lengths: list[int]) -> bool:
    """Does this email look materially different from the sender's usual ones?"""
    text = f"{subject or ''}".lower()
    if any(k in text for k in URGENCY_KEYWORDS):
        return True
    if prior_lengths:
        avg = sum(prior_lengths) / len(prior_lengths)
        if avg > 0 and len(body or "") >= LENGTH_BREAK_FACTOR * avg:
            return True
    return False


def check_inbound(tenant_id: str, address: str | None, subject: str | None,
                  body: str | None, *, mailbox_id: str | None = None,
                  client=None) -> dict:
    """The pipeline's pre-LLM gate. Returns {"action", "stats"} where action is
      'draft'    — not suppressed (or no record): draft as normal
      'suppress' — skip the LLM call; log the inbound under FYI
      'reengage' — suppressed, but this email broke the pattern: draft ONE-OFF
    Also advances the auto-escalation fuse: an inbound arriving while
    'suppressed' counts toward the silent flip to 'permanently_ignored'."""
    addr = _norm(address)
    if not addr:
        return {"action": "draft", "stats": None}
    try:
        stats = db.get_contact_draft_stats(tenant_id, addr, client=client)
        if not stats or stats.get("status") == "active":
            return {"action": "draft", "stats": stats}
        if stats.get("status") == "suppressed":
            # The fuse burns on every inbound while the banner sits unactioned —
            # advance it FIRST so a re-engagement draft doesn't freeze the flip.
            n = int(stats.get("emails_since_suppressed") or 0) + 1
            if n >= AUTO_SUPPRESS_AFTER:
                db.upsert_contact_draft_stats(tenant_id, addr, {
                    "status": "permanently_ignored", "suppressed_at": _now(),
                    "suppressed_by": "auto", "suppression_reason": "threshold_reached",
                    "emails_since_suppressed": n,
                }, client=client)
                stats = {**stats, "status": "permanently_ignored",
                         "emails_since_suppressed": n}
                total = int(stats.get("total_unsent_drafts") or 0) + n
                db.log_agent_event(
                    tenant_id, "suppression",
                    f"Auto-suppressed drafts for {addr} ({total} emails without a "
                    "response) — un-ignore any time from Ignored contacts",
                    contact=addr, subject=subject, mailbox_id=mailbox_id,
                    detail={"by": "auto", "emails_since_suppressed": n}, client=client)
            else:
                db.upsert_contact_draft_stats(
                    tenant_id, addr, {"emails_since_suppressed": n}, client=client)
                stats = {**stats, "emails_since_suppressed": n}
        # Safety valve — months-ignored senders can still send something that matters.
        try:
            prior = db.recent_inbound_body_lengths(tenant_id, addr, client=client)
        except Exception:
            prior = []
        if looks_reengaging(subject, body, prior):
            db.log_agent_event(
                tenant_id, "suppression",
                f"{addr} sent an email that looks different from their usual ones "
                "— drafted a reply just in case",
                contact=addr, subject=subject, mailbox_id=mailbox_id,
                detail={"reengagement": True}, client=client)
            return {"action": "reengage", "stats": stats}
        return {"action": "suppress", "stats": stats}
    except Exception:
        log.warning("suppression check failed for %s — drafting anyway", addr,
                    exc_info=True)
        return {"action": "draft", "stats": None}


def record_draft_generated(tenant_id: str, address: str | None, *,
                           subject: str | None = None,
                           mailbox_id: str | None = None, client=None) -> None:
    """A draft just entered the approval queue for this contact. Extends the
    unsent streak; the streak reaching THRESHOLD pauses future drafting. A
    one-off draft for a non-active contact (re-engagement / Draft now) never
    advances the streak — the owner asked for that one, not for a reset."""
    addr = _norm(address)
    if not addr:
        return
    try:
        stats = db.get_contact_draft_stats(tenant_id, addr, client=client) or {}
        if stats.get("status") not in (None, "active"):
            return
        streak = int(stats.get("consecutive_unsent_drafts") or 0) + 1
        fields = {
            "consecutive_unsent_drafts": streak,
            "total_unsent_drafts": int(stats.get("total_unsent_drafts") or 0) + 1,
            "last_unsent_draft_at": _now(),
            "first_unsent_draft_at": (stats.get("first_unsent_draft_at")
                                      if streak > 1 else None) or _now(),
        }
        if streak >= THRESHOLD:
            fields.update(status="suppressed", suppressed_at=_now(),
                          suppression_reason="threshold_reached",
                          emails_since_suppressed=0)
        db.upsert_contact_draft_stats(tenant_id, addr, fields, client=client)
        if streak >= THRESHOLD:
            db.log_agent_event(
                tenant_id, "suppression",
                f"Paused drafting for {addr} — {streak} drafts went unanswered. "
                "Open their thread to resume, ignore, or reply.",
                contact=addr, subject=subject, mailbox_id=mailbox_id,
                detail={"consecutive_unsent_drafts": streak}, client=client)
    except Exception:
        log.warning("failed to record unsent draft for %s", addr, exc_info=True)


def record_sent(tenant_id: str, address: str | None, client=None) -> None:
    """A reply to this contact went out (human approve or full-auto). Engagement
    resets the streak and reactivates a threshold pause; a PERMANENT ignore
    stays — the owner opted out explicitly and only Resume/Un-ignore lifts it."""
    addr = _norm(address)
    if not addr:
        return
    try:
        stats = db.get_contact_draft_stats(tenant_id, addr, client=client)
        if not stats:
            return
        fields = {
            "consecutive_unsent_drafts": 0, "emails_since_suppressed": 0,
            "total_sent_drafts": int(stats.get("total_sent_drafts") or 0) + 1,
            "first_unsent_draft_at": None, "last_unsent_draft_at": None,
        }
        if stats.get("status") == "suppressed":
            fields.update(status="active", suppressed_at=None,
                          suppressed_by=None, suppression_reason=None)
        db.upsert_contact_draft_stats(tenant_id, addr, fields, client=client)
    except Exception:
        log.warning("failed to reset draft streak for %s", addr, exc_info=True)


def resume(tenant_id: str, address: str | None, client=None) -> None:
    """Owner: 'Resume drafting' (banner) / 'Un-ignore' (Ignored page). Back to a
    clean slate — future emails draft again as normal."""
    addr = _norm(address)
    if not addr:
        return
    try:
        db.upsert_contact_draft_stats(tenant_id, addr, {
            "status": "active", "consecutive_unsent_drafts": 0,
            "emails_since_suppressed": 0, "suppressed_at": None,
            "suppressed_by": None, "suppression_reason": None,
            "first_unsent_draft_at": None, "last_unsent_draft_at": None,
        }, client=client)
    except Exception:
        log.warning("failed to resume drafting for %s", addr, exc_info=True)


def ignore_permanently(tenant_id: str, address: str | None, *,
                       by: str = "owner", client=None) -> None:
    """Owner: 'Ignore permanently' — no future drafts, no future banners. Mail
    still arrives and files under FYI (unlike the hard ignore, which hides it)."""
    addr = _norm(address)
    if not addr:
        return
    try:
        db.upsert_contact_draft_stats(tenant_id, addr, {
            "status": "permanently_ignored", "suppressed_at": _now(),
            "suppressed_by": by, "suppression_reason": "permanently_ignored",
        }, client=client)
    except Exception:
        log.warning("failed to permanently ignore %s", addr, exc_info=True)
