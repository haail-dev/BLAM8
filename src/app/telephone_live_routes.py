"""Owner Phone surface and authenticated carrier/model callbacks."""
import asyncio
from datetime import datetime
from typing import Literal
from uuid import UUID
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.dashboard import _ctx
from app.templating import templates
from app.security import require_csrf_header, rate_limit
from app.telephone_routes import StrictBody, _owner, Eligibility, Suppression, Policy
from app.telephone_live import TelephoneLiveService, readiness, public_url, instructions, tools, run_call
from app.telephone_inbound_store import TelephoneInboundStore
from app.telephone_store import TelephoneStore, StoreError
from app.telephone_provider import DefiniteFailure, AmbiguousSubmission
from app.telephone_twilio import verify_twilio_signature, capture_message_twiml, confirm_message_twiml, handover_screen_twiml
from app.telephone_realtime import verify_incoming_webhook
from app.telephone_objectives import ObjectiveStore, ObjectiveError, validate_template

router = APIRouter(tags=["telephone"])
P = "/api/telephone"
WRITES = [Depends(require_csrf_header), Depends(rate_limit("telephone_owner", 30, 60))]
_RUNNERS = set()


def get_service():
    return TelephoneLiveService(TelephoneInboundStore())


def gate():
    state = readiness()
    if not state["enabled"]:
        raise HTTPException(409, "Live telephone use is disabled.")
    if not state["configured"]:
        raise HTTPException(409, "Telephone configuration is incomplete.")


def call(fn, *args):
    try:
        return fn(*args)
    except StoreError as exc:
        raise HTTPException({"owner_required": 403, "not_found": 404,
                            "store_unavailable": 503}.get(exc.code, 409), exc.code) from None
    except ObjectiveError as exc:
        raise HTTPException(422, str(exc)) from None
    except (DefiniteFailure, AmbiguousSubmission):
        raise HTTPException(503, "provider_control_unverified") from None
    except Exception:
        raise HTTPException(503, "telephone_state_unavailable") from None


class RouteConfig(StrictBody):
    account_id: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    phone_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    enabled: bool = False
    original_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    transfer_verified: bool = False
    forwarding_state: Literal["unknown", "active", "restoration_required", "restored"] = "unknown"
    transfer_timeout_seconds: int = Field(ge=5, le=60)
    greeting: str = Field(min_length=1, max_length=300)
    instructions: str = Field(min_length=1, max_length=2000)


class TemplateBody(StrictBody):
    id: UUID | None = None
    spec: dict


class Outbound(StrictBody):
    idempotency_key: UUID
    to_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    from_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    script: str = Field(min_length=1, max_length=4000)
    max_duration_seconds: int = Field(ge=10, le=600)
    template_id: UUID
    template_version: int = Field(ge=1)


class Offboard(StrictBody):
    restored: bool = False


@router.get("/app/phone")
def phone(request: Request, ctx=Depends(_owner)):
    return templates.TemplateResponse(request, "app/phone.html", _ctx("settings", ctx,
        readiness=readiness()))


@router.get(P + "/readiness")
def ready(ctx=Depends(_owner)):
    return readiness()  # Presence/format only; never claims successful account access.


@router.get(P + "/route")
def route(ctx=Depends(_owner)):
    return call(get_service().command, ctx.tenant_id, ctx.user_id, "config_route")


@router.post(P + "/route", dependencies=WRITES)
def configure_route(body: RouteConfig, ctx=Depends(_owner)):
    payload = body.model_dump()
    payload["provider_verified"] = False
    if body.enabled:
        gate()
        if body.account_id != settings.twilio_account_sid:
            raise HTTPException(409, "Provider account does not match this platform connection.")
        adapter = call(get_service().carrier_factory, {"snapshot": {
            "account_id": body.account_id, "from_number": body.phone_number}})
        payload["provider_verified"] = call(adapter.verify_incoming_number, body.phone_number)
        if not payload["provider_verified"]:
            raise HTTPException(409, "Inbound number ownership could not be verified.")
    return call(get_service().command, ctx.tenant_id, ctx.user_id, "configure_route", payload)


@router.post(P + "/connect-number", dependencies=WRITES)
def connect_number(ctx=Depends(_owner)):
    gate()
    service = get_service()
    config = call(service.command, ctx.tenant_id, ctx.user_id, "config_route")
    if not config.get("enabled") or not config.get("provider_verified"):
        raise HTTPException(409, "Configure and verify the incoming route first.")
    # Explicit owner action: replaces voice routing only; never changes SMS.
    call(service.carrier_factory({"snapshot": {"account_id": config["account_id"],
        "phone_number": config["phone_number"]}}).configure_number, config["phone_number"])
    return {"voice_webhook_configured": True, "carrier_forwarding_changed": False, "live_tested": False}


@router.get(P + "/policy")
def read_policy(ctx=Depends(_owner)):
    return call(TelephoneStore().command, ctx.tenant_id, ctx.user_id, "config")


@router.post(P + "/policy", dependencies=WRITES)
def policy(body: Policy, ctx=Depends(_owner)):
    if body.max_call_cost_minor > body.daily_budget_minor:
        raise HTTPException(422, "Per-call reservation exceeds the daily budget.")
    return call(TelephoneStore().command, ctx.tenant_id, ctx.user_id, "configure", body.model_dump())


@router.post(P + "/eligibility", dependencies=WRITES)
def eligibility(body: Eligibility, ctx=Depends(_owner)):
    from datetime import timezone, timedelta
    now = datetime.now(timezone.utc)
    if body.expires_at.tzinfo is None or not now < body.expires_at <= now + timedelta(days=7):
        raise HTTPException(422, "An explicit callback eligibility expiry is required.")
    return call(TelephoneStore().command, ctx.tenant_id, ctx.user_id, "eligibility", body.model_dump(mode="json"))


@router.post(P + "/suppress", dependencies=WRITES)
def suppress(body: Suppression, ctx=Depends(_owner)):
    return call(TelephoneStore().command, ctx.tenant_id, ctx.user_id, "suppress", body.model_dump())


@router.get(P + "/templates")
def template_list(ctx=Depends(_owner)):
    return call(ObjectiveStore().command, ctx.tenant_id, ctx.user_id, "list")


@router.post(P + "/templates", dependencies=WRITES)
def template_save(body: TemplateBody, ctx=Depends(_owner)):
    spec = call(validate_template, body.spec)
    return call(ObjectiveStore().command, ctx.tenant_id, ctx.user_id, "save", {
        "id": str(body.id) if body.id else None, "spec": spec})


@router.post(P + "/outbound", dependencies=WRITES)
def prepare(body: Outbound, ctx=Depends(_owner)):
    service = get_service()
    selected = call(service.objectives.command, ctx.tenant_id, ctx.user_id, "read", {
        "id": str(body.template_id), "version": body.template_version})
    spec = selected["spec"]
    payload = body.model_dump(mode="json")
    payload.update(objective=spec["objective"], provider="twilio")
    row = call(service.command, ctx.tenant_id, ctx.user_id, "create_outbound", payload)
    call(service.objectives.command, ctx.tenant_id, ctx.user_id, "bind", {
        "call_id": row["id"], "id": str(body.template_id), "version": body.template_version})
    return row


@router.post(P + "/calls/{call_id}/approve", dependencies=WRITES)
def approve(call_id: UUID, ctx=Depends(_owner)):
    service = get_service()
    state = call(service.objectives.command, ctx.tenant_id, None, "state", {"call_id": str(call_id)})
    if not state.get("bound"):
        raise HTTPException(409, "Select the exact call form before approving.")
    return call(service.command, ctx.tenant_id, ctx.user_id, "approve_outbound", {"call_id": str(call_id)})


@router.post(P + "/calls/{call_id}/dispatch", dependencies=WRITES)
def dispatch(call_id: UUID, ctx=Depends(_owner)):
    gate()
    return call(get_service().dispatch, ctx.tenant_id, ctx.user_id, str(call_id))


@router.get(P + "/calls")
def calls(ctx=Depends(_owner)):
    service = get_service()
    result = call(service.command, ctx.tenant_id, ctx.user_id, "list")
    rows = result.get("calls", [])
    if rows:
        states = call(service.objectives.command, ctx.tenant_id, ctx.user_id, "states", {"call_ids": [row["id"] for row in rows]})["states"]
        for row in rows:
            row["objective_state"] = states.get(row["id"], {"bound": False})
    return result


@router.get(P + "/calls/{call_id}")
def read(call_id: UUID, ctx=Depends(_owner)):
    service = get_service()
    row = call(service.command, ctx.tenant_id, ctx.user_id, "read", {"call_id": str(call_id)})
    row["objective_state"] = call(service.objectives.command, ctx.tenant_id, None, "state", {"call_id": str(call_id)})
    return row


@router.post(P + "/calls/{call_id}/cancel", dependencies=WRITES)
def cancel(call_id: UUID, ctx=Depends(_owner)):
    service = get_service()
    row = call(service.command, ctx.tenant_id, ctx.user_id, "cancel", {"call_id": str(call_id)})
    try:
        service.carrier_factory(row).cancel(row["provider_call_id"])
    except Exception:
        pass  # Persisted intent remains visible; never report a verified hangup.
    return row


@router.post(P + "/calls/{call_id}/deliver-message", dependencies=WRITES)
def deliver(call_id: UUID, ctx=Depends(_owner)):
    service = get_service()
    call(service.command, ctx.tenant_id, ctx.user_id, "read", {"call_id": str(call_id)})
    return call(service.command, ctx.tenant_id, None, "outbox_deliver", {"call_id": str(call_id)})


@router.delete(P + "/calls/{call_id}/message", dependencies=WRITES)
def erase(call_id: UUID, ctx=Depends(_owner)):
    return call(get_service().command, ctx.tenant_id, ctx.user_id, "erase_message", {"call_id": str(call_id)})


@router.delete(P + "/calls/{call_id}/answers", dependencies=WRITES)
def erase_answers(call_id: UUID, ctx=Depends(_owner)):
    return call(ObjectiveStore().command, ctx.tenant_id, ctx.user_id, "erase", {"call_id": str(call_id)})


@router.post(P + "/offboard", dependencies=WRITES)
def offboard(body: Offboard, ctx=Depends(_owner)):
    return call(get_service().command, ctx.tenant_id, ctx.user_id, "offboard", body.model_dump())


async def signed_twilio(request):
    raw = await bounded_body(request, 32768)
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError()
        form = {key: values[0] for key, values in parsed.items()}
        query = request.scope.get("query_string", b"").decode("ascii")
        url = public_url(request.url.path) + ("?" + query if query else "")
    except (ValueError, UnicodeError):
        raise HTTPException(422, "invalid_carrier_payload") from None
    if not verify_twilio_signature(url, form, request.headers.get("X-Twilio-Signature", ""), settings.twilio_auth_token):
        raise HTTPException(403, "invalid_signature")
    if form.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(403, "provider_account_mismatch")
    return form


async def bounded_body(request, maximum):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise HTTPException(413, "payload_too_large")
    return bytes(raw)


@router.post("/webhook/telephone/twilio/voice")
async def voice(request: Request):
    form = await signed_twilio(request)
    if not settings.telephone_live_enabled:
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    gate()
    service = get_service()
    try:
        row, xml = await run_in_threadpool(service.voice, form, dict(request.query_params))
    except (StoreError, DefiniteFailure):
        # Busy/budget/unconfigured routing declines without starting a model call.
        return Response('<Response><Reject reason="busy"/></Response>', media_type="application/xml")
    return Response(xml, media_type="application/xml")


@router.post("/webhook/telephone/twilio/status")
async def status(request: Request):
    form = await signed_twilio(request)
    return await run_in_threadpool(call, get_service().status, form, dict(request.query_params))


@router.post("/webhook/telephone/twilio/transfer")
async def transfer(request: Request):
    form = await signed_twilio(request)
    return await run_in_threadpool(call, get_service().transfer_event, form, dict(request.query_params))


@router.post("/webhook/telephone/twilio/transfer-ended")
async def transfer_ended(request: Request):
    form = await signed_twilio(request)
    service = get_service()
    result = await run_in_threadpool(call, lambda: service.transfer_event(form, dict(request.query_params), ended=True))
    bound = result.get("transfer") or {}
    result = await run_in_threadpool(call, service.command, result["tenant_id"], None, "finish_handover", {
        "call_id": result["id"], "transfer_id": bound.get("id"), "provider_leg_id": form.get("DialCallSid"),
        "parent_call_id": result["provider_call_id"], "target_number": bound.get("target")})
    # No blind redial to a possibly forwarded number. A failed transfer gets a
    # bounded voice-message fallback, not an invented successful human handover.
    transfer_row = result.get("transfer") or {}
    if transfer_row.get("status") in {"failed", "timed_out"}:
        return Response(capture_message_twiml(public_url("/webhook/telephone/twilio/fallback-message") +
                                              "?expected_revision=" + str(result.get("message_revision", 0)),
                                              result.get("remaining_seconds", 0)), media_type="application/xml")
    return Response("<Response><Hangup/></Response>", media_type="application/xml")


@router.post("/webhook/telephone/twilio/sip-ended")
async def sip_ended(request: Request):
    await signed_twilio(request)
    return Response("<Response><Hangup/></Response>", media_type="application/xml")


@router.post("/webhook/telephone/openai")
async def incoming_model(request: Request):
    raw = await bounded_body(request, 65536)
    try:
        event = verify_incoming_webhook(raw, dict(request.headers), settings.telephone_openai_webhook_secret)
    except DefiniteFailure:
        raise HTTPException(403, "invalid_signature") from None
    headers = event["data"]["sip_headers"]
    matches = [item.get("value") for item in headers if str(item.get("name", "")).lower() == "x-blam8-correlation"]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise HTTPException(403, "unbound_sip_call")
    service = get_service()
    model_id = event["data"]["call_id"]
    try:
        row = await run_in_threadpool(service.command, None, None, "claim_sip", {"token": matches[0], "model_call_id": model_id})
    except StoreError as exc:
        # Identical retried webhook is already claimed; never accept twice.
        if exc.code not in {"correlation_consumed", "correlation_invalid", "not_found"}:
            raise HTTPException(503, "telephone_state_unavailable") from None
        return {"accepted": False, "state": "already_claimed_or_unbound"}
    client = service.realtime_factory()
    if not settings.telephone_live_enabled or row.get("cancel_requested"):
        await run_in_threadpool(call, client.reject, model_id, 486)
        await run_in_threadpool(service.command, None, None, "bind_model", {"model_call_id": model_id, "state": "failed"})
        return {"accepted": False}
    try:
        await run_in_threadpool(client.accept, model_id, instructions(row), tools(row["snapshot"].get("template")))
    except Exception:
        await run_in_threadpool(service.command, None, None, "bind_model", {"model_call_id": model_id, "state": "unknown"})
        return {"accepted": False, "state": "acceptance_unknown"}
    await run_in_threadpool(service.command, None, None, "bind_model", {"model_call_id": model_id, "state": "accepted"})
    runner = asyncio.create_task(run_call(service, row, model_id))
    _RUNNERS.add(runner)
    runner.add_done_callback(_RUNNERS.discard)
    return {"accepted": True}


async def handover_binding(request: Request):
    form = await signed_twilio(request)
    service = get_service()
    row = await run_in_threadpool(call, service.resolve_carrier, form, {})
    transfer = row.get("transfer") or {}
    if (str(transfer.get("id")) != request.query_params.get("transfer_id")
            or form.get("ParentCallSid") != row.get("provider_call_id")
            or form.get("To") != transfer.get("target")
            or not form.get("CallSid")
            or (transfer.get("provider_leg_id") and transfer["provider_leg_id"] != form["CallSid"])):
        raise HTTPException(409, "provider_mismatch")
    return service, row, transfer, form


@router.post("/webhook/telephone/twilio/handover-screen")
async def handover_screen(request: Request):
    _, row, transfer, _ = await handover_binding(request)
    if row.get("cancel_requested") or transfer.get("status") in {"failed", "timed_out"}:
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    action = public_url("/webhook/telephone/twilio/handover-accept") + "?transfer_id=" + str(transfer["id"])
    return Response(handover_screen_twiml(action, row.get("remaining_seconds", 0)), media_type="application/xml")


@router.post("/webhook/telephone/twilio/handover-accept")
async def handover_accept(request: Request):
    service, row, transfer, form = await handover_binding(request)
    if form.get("Digits") != "1":
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    await run_in_threadpool(call, service.command, row["tenant_id"], None, "human_accept", {
        "call_id": row["id"], "transfer_id": transfer["id"], "provider_leg_id": form["CallSid"],
        "parent_call_id": form["ParentCallSid"], "target_number": form["To"]})
    return Response("<Response/>", media_type="application/xml")


@router.post("/webhook/telephone/twilio/fallback-message")
async def fallback_message(request: Request):
    form = await signed_twilio(request)
    service = get_service()
    row = await run_in_threadpool(call, service.resolve_carrier, form, {})
    if (row.get("transfer") or {}).get("status") not in {"failed", "timed_out"}:
        raise HTTPException(409, "message_fallback_not_active")
    text = form.get("SpeechResult", "").strip()
    revision = row.get("message_revision", 0)
    expected = request.query_params.get("expected_revision", "")
    if not expected.isdigit() or int(expected) > 2:
        raise HTTPException(409, "message_revision_changed")
    expected = int(expected)
    if revision != expected:
        if row.get("message_text") != text:
            raise HTTPException(409, "message_revision_changed")
        if revision == expected + 2 and row.get("message_confirmed"):
            # Retried speech cannot undo confirmation or restore erased text.
            return Response("<Response><Hangup/></Response>", media_type="application/xml")
        if revision != expected + 1 or row.get("message_confirmed"):
            raise HTTPException(409, "message_revision_changed")
        saved = row  # The exact speech was already captured by this callback.
    elif not text or len(text) > 2000 or revision >= 3 or row.get("remaining_seconds", 0) < 10:
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    else:
        saved = await run_in_threadpool(call, service.command, row["tenant_id"], None, "message", {
            "call_id": row["id"], "expected_revision": expected, "text": text, "confirmed": False})
    xml = confirm_message_twiml(text, public_url("/webhook/telephone/twilio/fallback-confirm") + "?revision=" + str(saved["message_revision"]),
                                time_limit_remaining=row["remaining_seconds"])
    return Response(xml, media_type="application/xml")


@router.post("/webhook/telephone/twilio/fallback-confirm")
async def fallback_confirm(request: Request):
    form = await signed_twilio(request)
    service = get_service()
    row = await run_in_threadpool(call, service.resolve_carrier, form, {})
    if (row.get("transfer") or {}).get("status") not in {"failed", "timed_out"}:
        raise HTTPException(409, "message_fallback_not_active")
    revision = request.query_params.get("revision", "")
    if not revision.isdigit():
        raise HTTPException(409, "message_revision_changed")
    duplicate = (form.get("Digits") == "1" and row.get("message_confirmed") is True
                 and row.get("message_revision") == int(revision) + 1)
    if int(revision) != row.get("message_revision") and not duplicate:
        raise HTTPException(409, "message_revision_changed")
    if form.get("Digits") == "2" and int(revision) < 3 and not row.get("message_confirmed"):
        return Response(capture_message_twiml(public_url("/webhook/telephone/twilio/fallback-message") +
                                              "?expected_revision=" + revision,
                                              row["remaining_seconds"]), media_type="application/xml")
    if form.get("Digits") != "1":
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    if not duplicate:
        await run_in_threadpool(call, service.command, row["tenant_id"], None, "message", {
            "call_id": row["id"], "expected_revision": int(revision), "text": row["message_text"], "confirmed": True})
    try:
        await run_in_threadpool(service.command, row["tenant_id"], None, "outbox_deliver", {"call_id": row["id"]})
        xml = "<Response><Say>Your message has been saved for the business.</Say><Hangup/></Response>"
    except StoreError:
        xml = "<Response><Say>Your message was captured, but delivery needs the business to check it.</Say><Hangup/></Response>"
    return Response(xml, media_type="application/xml")
