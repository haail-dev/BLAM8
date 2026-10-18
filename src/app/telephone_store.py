"""Service-role-only durable telephone pilot commands.

Identity must originate in the authenticated route, never user JSON. The RPC
rechecks active ownership and serializes claims in PostgreSQL. No provider I/O.
"""
from __future__ import annotations

SAFE_CODES = frozenset({
    "invalid_command", "invalid_payload", "owner_required", "not_found",
    "disabled", "ineligible", "suppressed", "idempotency_conflict",
    "invalid_state", "approval_expired", "budget_exhausted", "capacity_exhausted",
    "attempt_mismatch", "provider_mismatch", "event_conflict", "already_dispatched", "store_unavailable",
})


class StoreError(Exception):
    def __init__(self, code: str):
        self.code = code if code in SAFE_CODES else "store_unavailable"
        super().__init__(self.code)


class TelephoneStore:
    """A service-role Supabase client; never use with browser credentials."""
    def __init__(self, client=None):
        if client is None:
            from app import db
            client = db.db()
        self.client = client

    def command(self, tenant_id: str, user_id: str | None, action: str,
                payload: dict | None = None) -> dict:
        try:
            response = self.client.rpc("telephone_command", {
                "p_tenant": tenant_id, "p_user": user_id,
                "p_action": action, "p_payload": payload or {},
            }).execute()
        except Exception as exc:
            # Database errors may include SQL context, identifiers or payloads.
            # Only the explicitly defined short safe code crosses this boundary.
            code = getattr(exc, "message", None)
            if not isinstance(code, str) or code not in SAFE_CODES:
                args = getattr(exc, "args", ())
                first = args[0] if args else None
                code = first.get("message") if isinstance(first, dict) else first
            raise StoreError(code if isinstance(code, str) else "store_unavailable") from None
        result = getattr(response, "data", None)
        if not isinstance(result, dict):
            raise StoreError("store_unavailable")
        return result


# Short alias for local fixtures.
Store = TelephoneStore
