"""Server-owned routing for bounded horizontal operations, disabled by default.

Authority describes the complete application path. Tenant text and business
labels never participate. Candidate CRM results remain suggestions even when
the model reports high confidence. No mutation-capable tool loop uses this API.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import logging
import time
import hashlib
import json

from app import llm, metering
from app.config import settings

log = logging.getLogger(__name__)
POLICY_VERSION = "horizontal-v1"


class Operation(StrEnum):
    CRM_FIELDS = "crm_ordinary_fields"
    ADMIN_MESSAGE = "routine_admin_message"
    TONE_PREVIEW = "tone_preview"


class Authority(StrEnum):
    PREVIEW = "preview"
    SUGGESTION = "internal_suggestion"
    CUSTOMER_DRAFT = "customer_draft"
    RECORD_WRITE = "record_write"
    BOOKING_MUTATION = "booking_mutation"
    ACTION = "broader_action"


class Reason(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "eligible_candidate"
    NOT_ENABLED = "operation_not_enabled"
    PROTECTED = "protected_authority"


_AUTHORITIES = {
    Operation.CRM_FIELDS: Authority.SUGGESTION,
    Operation.ADMIN_MESSAGE: Authority.CUSTOMER_DRAFT,
    Operation.TONE_PREVIEW: Authority.PREVIEW,
}


@dataclass(frozen=True)
class ModelDecision:
    model: str
    baseline_model: str
    route_id: str
    reason: Reason
    policy_version: str = POLICY_VERSION

    @property
    def candidate(self) -> bool:
        return self.reason == Reason.CANDIDATE

    def metadata(self) -> dict:
        return {"route_id": self.route_id, "policy_version": self.policy_version,
                "selected_model": self.model, "selection_reason": self.reason.value}


def resolve_model(operation, *, authority, fallback_model: str) -> ModelDecision:
    """Apply an explicit server allowlist after the caller resolves its baseline."""
    try:
        op, auth = Operation(operation), Authority(authority)
    except (ValueError, TypeError):
        return ModelDecision(fallback_model, fallback_model, "baseline", Reason.BASELINE)
    route = op.value
    if _AUTHORITIES[op] != auth:
        return ModelDecision(fallback_model, fallback_model, route, Reason.PROTECTED)
    enabled = {s.strip() for s in settings.model_candidate_operations.split(",")}
    if not settings.model_candidate_enabled or op.value not in enabled:
        return ModelDecision(fallback_model, fallback_model, route, Reason.NOT_ENABLED)
    return ModelDecision(settings.model_candidate_model, fallback_model, route, Reason.CANDIDATE)


def crm_field_fingerprint(definition: dict) -> str:
    """Bind review to field meaning, so editing a tenant label/hint invalidates it."""
    semantic = {key: definition.get(key) for key in (
        "entity", "key", "label", "field_type", "extraction_hint", "options",
        "durability", "ladder", "required", "probe_behaviour")}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def ordinary_crm_fields(definitions: list[dict]) -> bool:
    """Require server review of field meanings; type alone cannot establish safety.

    All fields in this bounded call must qualify. Dates, built-ins, expiring
    fields and fields driving workflow changes retain the baseline path.
    """
    allowed = {s.strip() for s in settings.model_candidate_crm_fields.split(",") if s.strip()}
    return bool(definitions) and all(
        crm_field_fingerprint(d) in allowed
        and not str(d.get("key", "")).startswith("_")
        and d.get("field_type") in {"text", "number", "boolean", "dropdown"}
        and d.get("durability", "once") == "once"
        and not d.get("ladder")
        for d in definitions)


class InvalidOutput(ValueError):
    """Candidate output failed the caller's deterministic validator."""


def _abandon(task, metadata, tenant_id, module):
    """Ignore late output while retaining any eventual charge exactly once."""
    metadata["abandoned"] = True
    task.cancel()

    def finished(done):
        try:
            msg = done.result()
        except BaseException:
            # llm already records failed/cancelled provider attempts.
            return
        if tenant_id:
            from app import usage
            usage.record_anthropic(tenant_id, msg.model, msg.usage,
                                   operation="llm_failure", module=module)
    task.add_done_callback(finished)


async def create_candidate(*, decision: ModelDecision, client, validate, **kwargs):
    """At most two provider invocations in one existing 90-second call budget.

    SDK retries and hedges are disabled for BOTH attempts. The first receives
    half the remaining deadline, reserving time for a single baseline fallback.
    Calls contain no tools and results are validated before publication. Failed
    transport receipts are already metered by llm; successful malformed receipts
    are returned with the final totals, or metered here if the operation fails.
    """
    if kwargs.get("tools") or kwargs.get("tool_choice"):
        raise ValueError("routed candidate calls cannot carry tools")
    if not decision.candidate:
        raise ValueError("candidate execution requires an eligible decision")

    def validated(msg):
        if getattr(msg, "stop_reason", None) != "end_turn":
            raise ValueError("incomplete routed response")
        return validate(msg)

    totals = metering.Totals()
    deadline = time.monotonic() + llm.CALL_TIMEOUT_S
    fallback_reason = None
    with llm.request_scope():
        try:
            for index, model in enumerate((decision.model, decision.baseline_model)):
                attempt_started = time.monotonic()
                remaining = deadline - attempt_started
                if remaining <= 0:
                    raise TimeoutError("routing deadline exhausted")
                budget = remaining / 2 if index == 0 else remaining
                bounded = client.with_options(max_retries=0, timeout=budget)
                metadata = {**decision.metadata(), "fallback_reason": fallback_reason,
                            "routing_attempt": index, "schema_failure": False}
                try:
                    task = asyncio.create_task(llm.create(
                        client=bounded, model=model, hedge_after=None,
                        route_metadata=metadata, route_validate=validated, **kwargs))
                    try:
                        done, _ = await asyncio.wait({task}, timeout=budget)
                    except BaseException:
                        _abandon(task, metadata, kwargs.get("tenant_id"), kwargs.get("module", "email"))
                        raise
                    if not done:
                        _abandon(task, metadata, kwargs.get("tenant_id"), kwargs.get("module", "email"))
                        raise TimeoutError("routing attempt deadline exhausted")
                    msg = task.result()
                    metering.add(totals, msg.usage)
                    # A transport that suppresses cancellation cannot publish late data.
                    if task.cancelling() or time.monotonic() >= deadline or (index == 0 and
                            time.monotonic() - attempt_started >= budget):
                        raise TimeoutError("late routing response")
                    try:
                        value = validated(msg)
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        metadata["schema_failure"] = True
                        for receipt in metering.receipts(msg.usage) or []:
                            receipt.setdefault("route", {})["schema_failure"] = True
                        raise InvalidOutput("invalid routed output") from exc
                    for receipt in totals.attempts:
                        receipt.setdefault("route", {}).update({
                            "usable_result_ms": int((time.monotonic() - (deadline - llm.CALL_TIMEOUT_S)) * 1000),
                            "result_model": msg.model})
                    return msg, value, totals, index == 0
                except (TimeoutError, InvalidOutput) as exc:
                    fallback_reason = "schema_failure" if isinstance(exc, InvalidOutput) else "timeout"
                    if index:
                        raise
                except Exception as exc:
                    # Only provider availability errors warrant another invocation.
                    import anthropic
                    if not isinstance(exc, (anthropic.APIConnectionError, anthropic.APIStatusError)):
                        raise
                    fallback_reason = "provider_error"
                    if index:
                        raise
                log.info("model routing fallback", extra={**metadata, "fallback_reason": fallback_reason})
        except BaseException:
            if totals.attempts and kwargs.get("tenant_id"):
                from app import usage
                usage.record_anthropic(kwargs["tenant_id"], decision.model, totals,
                                       operation="llm_failure", module=kwargs.get("module", "email"))
            raise
