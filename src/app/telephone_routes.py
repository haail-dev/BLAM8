"""Owner-only simulated telephone pilot; disconnected from existing SMS routes."""
from datetime import datetime, timezone, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import guards
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.security import rate_limit, require_csrf_header
from app.telephone import TelephoneService
from app.telephone_provider import DefiniteFailure, get_provider, parse_event, verify_simulation_signature
from app.telephone_store import TelephoneStore, StoreError

router = APIRouter(tags=["telephone-pilot"])
PREFIX = "/api/telephone-pilot"


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(StrictBody):
    enabled: bool = False
    daily_budget_minor: int = Field(ge=1, le=100_000)
    max_concurrent: int = Field(ge=1, le=5)
    max_call_cost_minor: int = Field(ge=1, le=10_000)
    max_duration_seconds: int = Field(ge=10, le=600)


class Eligibility(StrictBody):
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    eligible: bool
    callback_requested: bool
    expires_at: AwareDatetime
    basis: Literal["owner_attested_callback"] = "owner_attested_callback"


class Suppression(StrictBody):
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    suppressed: bool = True


class Callback(StrictBody):
    idempotency_key: UUID
    to_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    from_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    objective: str = Field(min_length=1, max_length=1000)
    script: str = Field(min_length=1, max_length=4000)
    max_duration_seconds: int = Field(ge=10, le=600)
    scenario: Literal["completed", "failed", "busy", "no_answer", "timeout", "ambiguous"] = "completed"


def get_store():
    return TelephoneStore()


def _owner(ctx: TenantCtx = Depends(require_onboarded)):
    if not ctx.user_id or ctx.viewing_as:
        raise HTTPException(403, "The telephone pilot requires the signed-in owner.")
    guards.require_owner(ctx)  # Fresh membership; SQL also requires an active owner row.
    return ctx


def _enabled():
    if not settings.telephone_pilot_enabled or settings.telephone_pilot_provider != "simulated":
        raise HTTPException(404, "The telephone pilot is not enabled.")


def _service():
    # Always fail closed for a future configuration name with no vetted adapter.
    try:
        return TelephoneService(get_store(), get_provider(settings.telephone_pilot_provider))
    except Exception:
        raise HTTPException(503, "telephone_state_unavailable") from None


def _run(fn, *args):
    try:
        result = fn(*args)
        return {"simulated": True, "result": result}
    except StoreError as exc:
        code = exc.code
        status = {"owner_required": 403, "not_found": 404, "store_unavailable": 503}.get(code, 409)
        raise HTTPException(status, code) from None
    except Exception:
        raise HTTPException(503, "telephone_state_unavailable") from None


WRITE = [Depends(require_csrf_header), Depends(rate_limit("telephone_pilot", 30, 60))]


@router.get(PREFIX + "/config")
def config(ctx: TenantCtx = Depends(_owner)):
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "config")


@router.post(PREFIX + "/config", dependencies=WRITE)
def configure(body: Policy, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    if body.max_call_cost_minor > body.daily_budget_minor:
        raise HTTPException(422, "A call reservation must fit the daily budget.")
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "configure", body.model_dump())


@router.post(PREFIX + "/eligibility", dependencies=WRITE)
def eligibility(body: Eligibility, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    now = datetime.now(timezone.utc)
    if body.expires_at.tzinfo is None or not now < body.expires_at <= now + timedelta(days=7):
        raise HTTPException(422, "Eligibility needs an explicit expiry within seven days.")
    if body.eligible and not body.callback_requested:
        raise HTTPException(422, "The pilot accepts requested callbacks only.")
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "eligibility", body.model_dump(mode="json"))


@router.post(PREFIX + "/suppress", dependencies=WRITE)
def suppress(body: Suppression, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "suppress", body.model_dump())


@router.post(PREFIX + "/calls", dependencies=WRITE)
def create(body: Callback, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    if not body.objective.strip() or not body.script.strip():
        raise HTTPException(422, "An objective and script are required.")
    payload = body.model_dump(mode="json")
    payload["provider"] = "simulated"
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "create", payload)


@router.get(PREFIX + "/calls")
def calls(ctx: TenantCtx = Depends(_owner)):
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "list")


@router.get(PREFIX + "/calls/{call_id}")
def read(call_id: UUID, ctx: TenantCtx = Depends(_owner)):
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "read", {"call_id": str(call_id)})


@router.post(PREFIX + "/calls/{call_id}/approve", dependencies=WRITE)
def approve(call_id: UUID, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    return _run(_service().command, ctx.tenant_id, ctx.user_id, "approve", {"call_id": str(call_id)})


@router.post(PREFIX + "/calls/{call_id}/dispatch", dependencies=WRITE)
def dispatch(call_id: UUID, ctx: TenantCtx = Depends(_owner)):
    _enabled()
    return _run(_service().dispatch, ctx.tenant_id, ctx.user_id, str(call_id))


@router.post(PREFIX + "/calls/{call_id}/cancel", dependencies=WRITE)
def cancel(call_id: UUID, ctx: TenantCtx = Depends(_owner)):
    # Recovery stays available after disabling new calls.
    return _run(_service().cancel, ctx.tenant_id, ctx.user_id, str(call_id))


@router.post("/webhook/telephone-pilot", dependencies=[Depends(rate_limit("telephone_pilot_event", 120, 60))])
async def event(request: Request):
    # No request identity or Host header supplies authority. This endpoint is for
    # the simulator only, not Twilio/OpenAI's different signature protocols.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, "event_too_large")
    raw = bytes(raw)
    if not verify_simulation_signature(raw, request.headers.get("X-Telephone-Signature", ""),
                                       settings.telephone_pilot_webhook_secret):
        raise HTTPException(403, "invalid_signature")
    try:
        parsed = parse_event(raw)
        UUID(parsed.tenant_id)
        UUID(parsed.call_id)
    except (DefiniteFailure, ValueError):
        raise HTTPException(422, "invalid_event") from None
    # Reconcile already-dispatched calls even while new-call admission is off.
    return await run_in_threadpool(_run, _service().event, parsed)
