"""Approved callback orchestration. This pilot has no telephone-network adapter."""
from dataclasses import asdict
from uuid import uuid4

from app.telephone_provider import (
    CallSpec, DefiniteFailure, get_provider,
)
from app.telephone_store import StoreError


class TelephoneService:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider if provider is not None else get_provider("simulated")

    def command(self, tenant_id, user_id, action, payload=None):
        return self.store.command(tenant_id, user_id, action, payload or {})

    def dispatch(self, tenant_id, user_id, call_id):
        before = self.command(tenant_id, user_id, "read", {"call_id": call_id})
        if before.get("snapshot", {}).get("provider", "simulated") != "simulated":
            raise StoreError("invalid_state")
        attempt_id = str(uuid4())
        # The transaction rechecks approval, eligibility, suppression, limits and
        # current ownership, and claims ONCE before any external effect could start.
        row = self.command(tenant_id, user_id, "claim", {
            "call_id": call_id, "attempt_id": attempt_id,
        })
        snapshot = row["snapshot"]
        spec = CallSpec(call_id=call_id, attempt_id=attempt_id,
                        destination=snapshot["to_number"], objective=snapshot["objective"],
                        script=snapshot.get("script", ""),
                        max_duration_seconds=snapshot["max_duration_seconds"],
                        scenario=snapshot.get("scenario", "completed"))
        try:
            submission = self.provider.submit(spec)
        except DefiniteFailure:
            return self.command(tenant_id, None, "failed", {
                "call_id": call_id, "attempt_id": attempt_id,
                "error_code": "provider_rejected",
            })
        except Exception:
            # No provider errors, destination or script enter logs/receipts.
            # A crash before this write leaves the claimed reservation held too.
            return self.command(tenant_id, None, "unknown", {
                "call_id": call_id, "attempt_id": attempt_id,
                "error_code": "submission_unknown",
            })
        try:
            row = self.command(tenant_id, None, "submitted", {
                "call_id": call_id, "attempt_id": attempt_id,
                "provider_call_id": submission.provider_call_id,
            })
        except Exception:
            # The provider may have accepted; repeating dispatch is never safe.
            return self.command(tenant_id, None, "unknown", {
                "call_id": call_id, "attempt_id": attempt_id,
                "error_code": "submission_receipt_unknown",
            })
        if row.get("cancel_requested"):
            # Cancellation may arrive while submit is in flight, before the
            # provider ID exists. Deliver that durable intent once it is known.
            try:
                self.provider.cancel(submission.provider_call_id)
            except Exception:
                pass
            return row  # An acknowledgement cannot substitute for a terminal event.
        # Explicitly synthetic events, not assertions about a real call or CRM.
        for event in self.provider.events(spec, submission, tenant_id):
            row = self.event(event)
        return row

    def event(self, event):
        payload = asdict(event)
        tenant_id = payload.pop("tenant_id")
        # The signed adapter supplies tenant identity; SQL additionally binds the
        # request to its provider call ID before applying a monotonic transition.
        return self.command(tenant_id, None, "event", payload)

    def cancel(self, tenant_id, user_id, call_id):
        row = self.command(tenant_id, user_id, "cancel", {"call_id": call_id})
        if row.get("cancel_requested") and row.get("status") not in {
                "completed", "failed", "busy", "no_answer", "cancelled"} and row.get("provider_call_id"):
            try:
                self.provider.cancel(row["provider_call_id"])
            except Exception:
                # Intent remains durable and capacity stays reserved. A transport
                # acknowledgement would not prove the call has stopped either.
                pass
        return row
