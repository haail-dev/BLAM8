"""Retention sweep (Render cron): redact email content older than the retention
window, drop expired oauth_states, and release stale diary holds. Runs under
the service key.

    python -m app.jobs.purge_email_log
"""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import db

# Scheduling (D4 backstop): a proposed hold whose draft was never approved or
# skipped must not occupy diary capacity forever.
HOLD_EXPIRY_DAYS = 7

# email_log columns written FROM the customer's email — cleared with the body.
EMAIL_DERIVED_COLUMNS = ("knowledge_gap", "request_summary", "action_summary",
                         "booking_details", "category_reason", "draft_sources",
                         "searched_for")


def run() -> dict:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=settings.email_retention_days)).isoformat()
    # Redact the free-text content but KEEP the audit row (from_address, subject,
    # status, timestamps) — email_log is the approval-history / audit trail.
    # The columns DERIVED from the email go too (security review 2026-09, M14:
    # summaries, booking details, gap notes and search text kept the content
    # the "only the audit record remains" promise said was gone).
    red = (
        db().table("email_log")
        .update({"body_in": "[redacted]", "draft_reply": "[redacted]",
                 **{c: None for c in EMAIL_DERIVED_COLUMNS}})
        .lt("created_at", cutoff)
        .execute()
    )
    # Agent Log (agent_events): keep the audit summary/contact/subject — the action
    # trail — but clear the `detail` bag (the only free-text: a customer question, a
    # knowledge-gap description) past the same window, mirroring email_log's redaction.
    ev = (
        db().table("agent_events")
        .update({"detail": {}})
        .lt("created_at", cutoff)
        .execute()
    )
    # LLM transcript log (llm_calls, src/app/llm.py): the full-prompt observability
    # rows carry customer email bodies, so past their window the WHOLE row goes —
    # usage_events keeps the billing audit, nothing else references these rows.
    llm_cutoff = (now - timedelta(days=settings.llm_log_retention_days)).isoformat()
    llm = db().table("llm_calls").delete().lt("created_at", llm_cutoff).execute()
    # Agent trace (agent_trace, src/app/trace.py): detail bags can carry message
    # snippets, so whole rows age out on the same window as llm_calls.
    try:
        tr = db().table("agent_trace").delete().lt("created_at", llm_cutoff).execute()
        traces_swept = len(tr.data or [])
    except Exception:
        traces_swept = 0    # pre-migration schema — nothing to sweep yet
    # Sweep CSRF/state rows that were never consumed.
    st = db().table("oauth_states").delete().lt("expires_at", now.isoformat()).execute()
    # Support tickets (UK GDPR minimisation): resolved issue tickets past the
    # retention window lose their contact + device details (email, error text,
    # page, user agent); the de-identified body stays as the bug-history record.
    from app import db as dbmod
    try:
        tickets_redacted = dbmod.redact_expired_tickets(settings.ticket_retention_days)
    except Exception:
        tickets_redacted = 0    # pre-migration schema — nothing to redact yet
    # Concierge notification feed: 30-day rolling window — the digest only ever
    # shows recent items, and titles can carry contact names/subjects.
    notif_cutoff = (now - timedelta(days=30)).isoformat()
    try:
        nt = db().table("concierge_notifications").delete() \
            .lt("created_at", notif_cutoff).execute()
        notifications_swept = len(nt.data or [])
    except Exception:
        notifications_swept = 0    # pre-migration schema — nothing to sweep yet
    # Release stale agent holds (proposed → cancelled). Confirmed bookings are
    # never touched — only forgotten provisional capacity comes back.
    hold_cutoff = (now - timedelta(days=HOLD_EXPIRY_DAYS)).isoformat()
    exp = (
        db().table("bookings")
        .update({"status": "cancelled", "updated_at": now.isoformat()})
        .eq("status", "proposed")
        .lt("created_at", hold_cutoff)
        .execute()
    )
    # Release offered slots past their deadline (docs/modules/module-smart-booking-offers.md).
    # Capacity math already ignores an expired offer the moment expires_at passes —
    # this sweep just makes the diary rows agree. The 7-day proposed rule above is
    # also the backstop for any offered row that somehow has no deadline.
    try:
        offers_released = dbmod.expire_offered_bookings()
        offers_released += len((
            db().table("bookings")
            .update({"status": "cancelled", "updated_at": now.isoformat()})
            .eq("status", "offered").is_("expires_at", "null")
            .lt("created_at", hold_cutoff)
            .execute()
        ).data or [])
    except Exception:
        offers_released = 0    # pre-migration schema — nothing to sweep yet
    return {"redacted": len(red.data or []), "states_swept": len(st.data or []),
            "holds_expired": len(exp.data or []), "events_redacted": len(ev.data or []),
            "llm_calls_swept": len(llm.data or []), "tickets_redacted": tickets_redacted,
            "traces_swept": traces_swept, "notifications_swept": notifications_swept,
            "offers_released": offers_released}


if __name__ == "__main__":
    print(run())
