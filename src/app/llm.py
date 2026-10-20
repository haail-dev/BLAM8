"""The single choke point for every Anthropic messages.create call.

Every LLM request in the app goes through create() so the FULL exchange — the
exact system prompt, messages (including every tool-loop round), tool
definitions, and the raw response — is written to llm_calls, where the operator
can read every word that crossed the API. Three design rules:

1. Logging is BEST-EFFORT (same contract as usage.record): a logging failure
   can never break a draft — it logs the exception and the call proceeds.
2. Callers keep their own client factory and pass it in, so the per-module
   `_client` test seams (FakeClient) keep working and this module stays
   stateless. Do not call the SDK directly anywhere else — a call that skips
   create() is a call the operator cannot see.
3. One redaction: base64 image/document payloads (vision calls) become a size
   stub. Every WORD is kept; the megabytes of pixels are not.

Modes (settings.llm_log_mode): 'full' stores request + response payloads,
'meta' stores only the envelope (model/tokens/duration), 'off' writes nothing.
Rows are purged after settings.llm_log_retention_days by the daily sweep
(src/app/main.py) — transcripts contain customer email bodies, so they age out.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import time
import uuid

from app import db, metering
from app.config import settings

log = logging.getLogger(__name__)

# Content-block types whose base64 `source.data` is bulk, not words.
_REDACT_TYPES = ("image", "document")

# Block types the API accepts a cache_control marker on.
_CACHEABLE_TYPES = ("text", "image", "tool_use", "tool_result", "document")


def _cached_kwargs(kwargs: dict, system_ttl: str | None = None) -> dict:
    """Stamp ONE ephemeral cache breakpoint on the last cacheable content block
    of the final message (docs/modules/module-concierge-cost-controls.md Part A).

    Prompt caching is a prefix match, so a single tail breakpoint covers
    tools → system → all messages in one entry; on the next tool-loop round
    (or the next chat turn inside the 5-min TTL) that entry is a prefix of the
    new prompt and re-bills at ~0.1x instead of full price. Conservative by
    construction: anything unexpected (SDK objects, empty content, an existing
    marker) passes through untouched, and caller structures are NEVER mutated —
    tool loops reuse their messages list across rounds."""
    if not settings.llm_prompt_cache:
        return kwargs
    # Second breakpoint on the system block (2026-09-06): once a chat's history
    # hits its turn cap the oldest turn drops every turn, the prefix changes,
    # and a tail-only breakpoint means NOTHING before the history is cached —
    # live Brightwell pass: round 1 of every late turn re-wrote ~6.7k tokens.
    # tools → system is the stable prefix; stamping it keeps that hot.
    # system_ttl="1h" keeps tools → system hot for an hour instead of five
    # minutes (the live voice Concierge: an owner's questions are often more
    # than five minutes apart, and each cold start re-wrote ~10k tokens).
    # A 1-hour entry must come before any 5-minute one — it does: system
    # precedes the messages tail.
    system = kwargs.get("system")
    if isinstance(system, str) and system.strip():
        cc = {"type": "ephemeral", **({"ttl": system_ttl} if system_ttl else {})}
        kwargs = {**kwargs, "system": [{"type": "text", "text": system, "cache_control": cc}]}
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        return kwargs
    last = messages[-1]
    if not isinstance(last, dict):
        return kwargs
    content = last.get("content")
    if isinstance(content, str):
        if not content.strip():
            return kwargs
        new_last = {**last, "content": [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"}}]}
    elif (isinstance(content, list) and content
          and isinstance(content[-1], dict)
          and content[-1].get("type") in _CACHEABLE_TYPES
          and "cache_control" not in content[-1]):
        new_block = {**content[-1], "cache_control": {"type": "ephemeral"}}
        new_last = {**last, "content": [*content[:-1], new_block]}
    else:
        return kwargs
    return {**kwargs, "messages": [*messages[:-1], new_last]}

# ── Request correlation ───────────────────────────────────────────────────────
# One user-facing exchange (a draft, a concierge question, a portal turn) can
# span MANY API calls — every tool-loop round is its own llm_calls row. The
# request_id groups them: every create() inside one request_scope() shares one
# id, so "this one question cost N tokens across M calls" is a GROUP BY away.
# A contextvar (not a parameter) so NESTED calls inherit the scope — the TMS/
# field-service specialist invoked from inside draft_reply's tool loop lands
# under the draft's id without threading anything through the executors.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_request_id", default=None)
_pending_costs: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "llm_pending_costs", default=None)


def mark_metered(attempts):
    """A nested caller already persisted these receipts; never charge them twice."""
    pending = _pending_costs.get()
    if pending is not None:
        ids = {a.get("attempt_id") for a in attempts}
        pending[:] = [item for item in pending if item[3].get("attempt_id") not in ids]


@contextlib.contextmanager
def request_scope():
    """Group every create() in this (async) context under one request_id.
    Re-entrant no-op: inside an existing scope the OUTER id is kept, so a
    specialist called from a draft loop logs under the draft's exchange."""
    if _request_id.get() is not None:
        yield
        return
    token = _request_id.set(str(uuid.uuid4()))
    pending_token = _pending_costs.set([])
    try:
        yield
    except BaseException:
        # A later tool round can fail after earlier paid calls succeeded. The
        # caller never receives its aggregate in that case, so retain it here.
        from app import usage
        for tenant, model, module, receipt in list(_pending_costs.get() or []):
            if tenant:
                totals = metering.Totals()
                totals.attempts = [receipt]
                usage.record_anthropic(tenant, model, totals, operation="llm_failure", module=module)
        raise
    finally:
        _pending_costs.reset(pending_token)
        _request_id.reset(token)


def with_request_scope(fn):
    """Decorator form of request_scope() for async entry points (tool loops)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with request_scope():
            return await fn(*args, **kwargs)
    return wrapper


def current_request_id() -> str | None:
    """The ambient exchange id, or None outside any scope — what src/app/trace.py
    stamps on trace rows so they join this exchange's llm_calls rows."""
    return _request_id.get()


# Per-call wall-clock bound for every Anthropic client (Kris 2026-09-04). Four
# calls since 25 Aug ran 85-168 s emitting a lone "{" until max_tokens ended
# them -- a degenerate generation, and the SDK's default 600 s timeout was the
# only other bound. 90 s is 3x the slowest healthy turn seen; the SDK retries a
# timed-out request (default max_retries=2, same as overloads), so a stalled
# turn is re-asked instead of failing the draft.
CALL_TIMEOUT_S = 90.0


async def create(*, client, tenant_id: str | None = None, operation: str = "",
                 module: str = "email", log_mode: str | None = None,
                 cache: bool = True, hedge_after: float | None = None,
                 route_metadata: dict | None = None, route_validate=None, **kwargs):
    """Call `client.messages.create(**kwargs)` and log the full exchange.

    Drop-in: returns the SDK response unchanged and re-raises exactly what the
    SDK raises (a failed request is still logged — the words went to the API).

    `log_mode` caps THIS call's logging below the global setting (e.g. 'meta'
    for voice learning, whose request carries the tenant's raw sent mail — a
    corpus we promise never to store; only the envelope may persist).

    `cache=False` skips the breakpoint stamp for ONE-SHOT BULK calls whose
    payload is unique and never re-sent (website distill, voice-learning
    batches): a breakpoint there bills the whole payload as a cache WRITE at
    1.25x with no read ever following — a pure surcharge (measured 2026-09-02:
    every such prod row had cache_read=0). Tool loops and chat turns keep the
    default.

    `hedge_after` (seconds) races a second identical request when the first
    has produced nothing by then — the stall guard the voice path uses
    (_stream_hedged), for a background call the owner is waiting on: drafting
    (run log F16, services take 2026-09-25: a 16 s round expected ~3 s, a 31 s
    write expected ~14 s — ~30 of Sarah's 56 s draft was stall)."""
    if cache:
        kwargs = _cached_kwargs(kwargs)
    return await _run_metered(client, kwargs, tenant_id, operation, module, log_mode,
                              hedge_after=hedge_after, route_metadata=route_metadata,
                              route_validate=route_validate)


# The hedge (2026-09-24, Kris: "It took a very long time Thinking"): about 3%
# of Concierge calls sat 9-27 s before their first word, far past what their
# length explains (llm_calls, 17-24 Sep) — a stall at the API, not the model
# thinking. A healthy call starts within ~1.5 s. If nothing has started after
# HEDGE_AFTER_S, an identical second request races the first; whichever
# produces content first is used and the other is closed. Costs one extra
# call on the rare stall; saves the owner 10-20 s of silence.
HEDGE_AFTER_S = 5.0


async def _stream_hedged(client, kwargs: dict, on_delta, hedge_after: float, report=None):
    winner = None
    started = asyncio.Event()

    async def attempt(tag: int):
        nonlocal winner
        t0 = time.monotonic()
        msg = exc = None
        observed = None
        provider_id = None
        transport = None
        complete = False
        try:
            async with client.messages.stream(**kwargs) as s:
                transport = getattr(s, "response", None)
                async for ev in s:
                    if ev.type == "message_start":
                        observed = metering.raw_usage(getattr(ev.message, "usage", None))
                        provider_id = getattr(ev.message, "id", None)
                    elif ev.type == "message_delta" and getattr(ev, "usage", None) is not None:
                        update = metering.raw_usage(ev.usage) or {}
                        observed = {**(observed or {}), **{k: v for k, v in update.items() if v is not None}}
                    if ev.type in ("content_block_start", "content_block_delta"):
                        if winner is None:
                            winner = tag
                            started.set()
                        if winner != tag:
                            return None
                        if (ev.type == "content_block_delta" and on_delta is not None
                                and getattr(ev.delta, "type", "") == "text_delta"):
                            result = on_delta(ev.delta.text)
                            if hasattr(result, "__await__"):
                                await result
                msg = await s.get_final_message()
                complete = True
                return msg
        except BaseException as error:
            exc = error
            raise
        finally:
            if report:
                report(msg, exc, t0, observed, provider_id, complete,
                       "winner" if complete else "cancelled" if isinstance(exc, asyncio.CancelledError)
                       else "failed" if exc else "loser", ordinal=tag, transport=transport)

    tasks = [asyncio.create_task(attempt(0))]
    waiter = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait({tasks[0], waiter}, timeout=hedge_after,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            log.warning("llm: no first token after %.0fs — hedging with a second request", hedge_after)
            tasks.append(asyncio.create_task(attempt(1)))
        error = None
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is not None:
                    error = error or t.exception()
                elif t.result() is not None:
                    return t.result()
        raise error or RuntimeError("no stream attempt finished")
    finally:
        waiter.cancel()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(waiter, *tasks, return_exceptions=True)


async def stream(*, client, tenant_id: str | None = None, operation: str = "",
                 module: str = "email", log_mode: str | None = None,
                 on_delta=None, system_ttl: str | None = None,
                 hedge_after: float | None = HEDGE_AFTER_S, **kwargs):
    """Streaming twin of create(): `client.messages.stream(**kwargs)`, forwarding
    every text delta to `on_delta` (sync or async, one str argument) as it
    arrives, then returning the accumulated final Message — so callers see the
    exact same return shape as create() and the llm_calls row is identical.
    The live voice surface is why this exists: the first spoken sentence must
    reach TTS while the rest of the reply is still generating."""
    kwargs = _cached_kwargs(kwargs, system_ttl=system_ttl)
    return await _run_metered(client, kwargs, tenant_id, operation, module, log_mode,
                              hedge_after=hedge_after, on_delta=on_delta, streaming=True)


async def _run_metered(client, kwargs, tenant_id, operation, module, log_mode,
                       *, hedge_after=None, on_delta=None, streaming=False,
                       route_metadata=None, route_validate=None):
    # Sonnet 5 otherwise enables adaptive thinking. Keep small output budgets
    # and forced tool choices on the explicit no-thinking path, including voice
    # streams and both hedge attempts. Log exactly the request we send.
    if kwargs.get("model") == "claude-sonnet-5":
        kwargs = {**kwargs, "thinking": {"type": "disabled"}}
    attempts = []
    request_id = _request_id.get() or str(uuid.uuid4())
    call_id = str(uuid.uuid4())

    def report(msg, exc, t0, observed=None, provider_id=None, complete=False, outcome="failed", ordinal=0,
               transport=None):
        raw = metering.raw_usage(getattr(msg, "usage", None)) or observed
        if raw is None and exc is not None:
            raw = metering.raw_usage(getattr(exc, "usage", None))
            body = getattr(exc, "body", None)
            if raw is None and isinstance(body, dict):
                raw = metering.raw_usage(body.get("usage"))
        receipt = {"attempt_id": str(uuid.uuid4()), "request_id": request_id,
                   "call_id": call_id, "ordinal": ordinal, "operation": operation,
                   "provider_message_id": getattr(msg, "id", None) or provider_id,
                   "provider_request_id": getattr(msg, "_request_id", None),
                   "model": getattr(msg, "model", None) or kwargs.get("model"),
                   "usage": raw, "complete": complete, "outcome": outcome}
        if route_metadata is not None:
            receipt["route"] = dict(route_metadata)
            if complete and route_validate is not None:
                try:
                    route_validate(msg)
                except (ValueError, TypeError, KeyError, AttributeError):
                    receipt["route"]["schema_failure"] = True
        receipt["selected_model"] = kwargs.get("model")
        receipt["duration_ms"] = int((time.monotonic() - t0) * 1000)
        transport = transport if transport is not None else getattr(exc, "response", None)
        request = getattr(transport, "request", None) or getattr(exc, "request", None)
        headers = getattr(request, "headers", {})
        retry_count = headers.get("x-stainless-retry-count")
        retry_count = int(retry_count) if str(retry_count).isdigit() else None
        receipt["sdk_retry_count"] = retry_count
        if transport is not None:
            receipt["provider_request_id"] = transport.headers.get("request-id")
        elif exc is not None:
            receipt["provider_request_id"] = getattr(exc, "request_id", None)
        if retry_count is None and getattr(client, "max_retries", 0):
            # The SDK did not expose whether earlier transport attempts ran.
            receipt["complete"] = False
        for retry_index in range(retry_count or 0):
            # The final request header proves these attempts existed, but does
            # not expose their usage. Do not fabricate counts or provider IDs.
            prior = {**receipt, "attempt_id": str(uuid.uuid4()), "usage": None,
                     "complete": False, "outcome": "sdk_retry_unobserved",
                     "provider_message_id": None, "provider_request_id": None,
                     "sdk_retry_index": retry_index, "cost_usd": None}
            attempts.append(prior)
            _log_call(tenant_id, operation, module, kwargs, None, None, t0, "meta", prior)
        receipt["cost_usd"] = metering.price(receipt["model"], receipt)
        # Non-standard pricing must never silently use standard global rates.
        if kwargs.get("speed") == "fast" or kwargs.get("inference_geo") not in (None, "global"):
            receipt["cost_usd"] = None
            receipt["pricing_unknown"] = True
        attempts.append(receipt)
        _log_call(tenant_id, operation, module, kwargs, msg, exc, t0, log_mode, receipt)

    try:
        if (streaming or hedge_after) and hasattr(client.messages, "stream"):
            # None disables the second attempt while retaining event-level usage capture.
            msg = await _stream_hedged(client, kwargs, on_delta, hedge_after, report)
        else:
            t0 = time.monotonic()
            transport = None
            try:
                if hasattr(client.messages, "with_raw_response"):
                    response = await client.messages.with_raw_response.create(**kwargs)
                    transport = response.http_response
                    msg = response.parse()
                    if hasattr(msg, "__await__"):
                        msg = await msg
                else:
                    msg = await client.messages.create(**kwargs)
            except BaseException as exc:
                report(None, exc, t0, transport=transport)
                raise
            report(msg, None, t0, complete=True, outcome="winner", transport=transport)
    except BaseException:
        # No caller receives usage on failure. Record this separately without
        # counting a handled email or a completed Concierge turn.
        if tenant_id and attempts:
            from app import usage
            failed = metering.Totals()
            failed.attempts = attempts
            usage.record_anthropic(tenant_id, kwargs.get("model") or "", failed,
                                   operation="llm_failure", module=module)
        raise
    value = getattr(msg, "usage", None)
    if isinstance(value, dict):
        value["_blam8_attempts"] = attempts
    elif value is not None:
        object.__setattr__(value, "_blam8_attempts", attempts)
    pending = _pending_costs.get()
    if pending is not None:
        pending.extend((tenant_id, kwargs.get("model") or "", module, a) for a in attempts)
    return msg


def _log_call(tenant_id, operation, module, kwargs, msg, exc, t0, log_mode=None, receipt=None) -> None:
    mode = "off" if settings.llm_log_mode == "off" else log_mode or settings.llm_log_mode
    if mode == "off":
        return
    try:
        usage_obj = (receipt or {}).get("usage") or metering.raw_usage(getattr(msg, "usage", None)) or {}
        row = {
            "tenant_id": tenant_id,
            # Outside any scope a call is its own one-call exchange — a fresh id
            # per row keeps request_id NOT NULL-ish and GROUP BY-safe throughout.
            "request_id": receipt["request_id"] if receipt else _request_id.get() or str(uuid.uuid4()),
            "raw_usage": receipt,
            "cost_version": metering.COST_VERSION,
            "operation": operation or "",
            "module": module,
            "model": str((receipt or {}).get("model") or kwargs.get("model") or ""),
            "stop_reason": ("api_error" if exc is not None
                            else getattr(msg, "stop_reason", None)),
            "input_tokens": int(usage_obj.get("input_tokens") or 0),
            "output_tokens": int(usage_obj.get("output_tokens") or 0),
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }
        if mode == "full" and settings.llm_log_mode == "full":
            row["request"] = _redact(_jsonable(kwargs))
            row["response"] = ({"error": repr(exc)} if exc is not None
                               else _redact(_jsonable(msg)))
        db.insert_llm_call(row)
    except Exception:  # logging must never break the pipeline
        log.exception("llm: failed to log %s call (tenant %s)", operation, tenant_id)


def _coerce(obj):
    """json.dumps default: SDK content blocks / Message are pydantic."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    return repr(obj)


def _jsonable(obj):
    """Anything the SDK sends or returns → plain JSON-safe dicts/lists."""
    return json.loads(json.dumps(obj, default=_coerce))


def _redact(node):
    """Replace base64 image/document data with a size stub, in place. Operates on
    the _jsonable output (pure dicts/lists), so mutation never touches SDK state."""
    if isinstance(node, dict):
        source = node.get("source")
        if (node.get("type") in _REDACT_TYPES and isinstance(source, dict)
                and isinstance(source.get("data"), str)):
            source["data"] = f"<{len(source['data'])} base64 chars redacted>"
        else:
            for v in node.values():
                _redact(v)
    elif isinstance(node, list):
        for v in node:
            _redact(v)
    return node
