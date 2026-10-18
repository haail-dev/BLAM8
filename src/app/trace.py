"""Agent trace — the execution-narrative observability layer (agent_trace).

Every AI action or decision the other two logs don't capture lands here as one
row: the concierge proposal lifecycle (propose → confirm/execute or
expire/deny), portal chat turns and identity-verification attempts, KB
retrievals, smart-label transitions, push delivery, voice-learning runs. Three
sibling systems, three jobs:

  * llm_calls (src/app/llm.py)      — every word that crossed the API
  * agent_events (db.py)        — the curated, tenant-visible activity page
  * agent_trace (this module)   — the operator's debug trail: what ran, with
                                  what inputs, what came back, and why

Contract (same as log_agent_event / insert_llm_call): emit() is BEST-EFFORT —
a trace failure is swallowed to a warning and never breaks the action being
traced. request_id defaults to the ambient llm request scope, so a trace row
written inside a tool loop joins its llm_calls rows for free. Rows are purged
with llm_calls on the retention sweep (detail may carry message snippets).
"""
import hashlib
import logging

from app import db, llm

log = logging.getLogger(__name__)


def session_ref(token: str | None) -> str | None:
    """An opaque, stable per-visitor handle from a portal session token — a
    sha256 prefix, so the trace can group one visitor's turns without ever
    storing the capability token itself."""
    token = (token or "").strip()
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def emit(tenant_id: str | None, trace_type: str, *, outcome: str = "ok",
         request_id: str | None = None, session_id: str | None = None,
         user_id: str | None = None, contact: str | None = None,
         email_log_id: str | None = None, booking_id: str | None = None,
         proposal_id: str | None = None, latency_ms: int | float | None = None,
         detail: dict | None = None) -> None:
    """Append one trace row. request_id falls back to the ambient llm request
    scope (llm.with_request_scope / request_scope), which is what joins this
    row to the llm_calls rows of the same exchange."""
    try:
        db.insert_agent_trace({
            "tenant_id": tenant_id,
            "trace_type": trace_type,
            "outcome": outcome,
            "request_id": request_id or llm.current_request_id(),
            "session_id": session_id,
            "user_id": user_id,
            "contact": contact,
            "email_log_id": email_log_id,
            "booking_id": booking_id,
            "proposal_id": proposal_id,
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
            "detail": detail or {},
        })
    except Exception:   # observability must never break the action it observes
        log.warning("agent_trace insert failed (tenant=%s type=%s)",
                    tenant_id, trace_type, exc_info=True)
