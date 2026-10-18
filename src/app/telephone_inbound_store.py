"""Service-only actual telephone admission, messages and handover storage."""
from __future__ import annotations
from app.telephone_store import StoreError

CODES = frozenset({"invalid_command", "invalid_payload", "owner_required", "not_found",
    "disabled", "provider_unverified", "provider_mismatch", "ineligible", "suppressed",
    "idempotency_conflict", "invalid_state", "approval_expired", "already_dispatched",
    "attempt_mismatch", "budget_exhausted", "capacity_exhausted", "event_conflict",
    "revision_conflict", "handover_unavailable", "handover_already_requested",
    "correlation_invalid", "correlation_consumed", "tool_conflict", "message_erased",
    "store_unavailable"})


class InboundStoreError(StoreError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in CODES else "store_unavailable"
        Exception.__init__(self, self.code)


class TelephoneInboundStore:
    def __init__(self, client=None):
        if client is None:
            from app import db
            client = db.db()
        self.client = client

    def command(self, tenant_id, user_id, action, payload=None):
        try:
            response = self.client.rpc("telephone_inbound_command", {
                "p_tenant": tenant_id, "p_user": user_id,
                "p_action": action, "p_payload": payload or {},
            }).execute()
        except Exception as exc:
            code = getattr(exc, "message", None)
            if not isinstance(code, str) or code not in CODES:
                args = getattr(exc, "args", ())
                first = args[0] if args else None
                code = first.get("message") if isinstance(first, dict) else first
            raise InboundStoreError(code) from None
        result = getattr(response, "data", None)
        if not isinstance(result, dict):
            raise InboundStoreError("store_unavailable")
        return result
