"""GPT-Live native media, trusted sideband delegation, existing Concierge authority.

One process owns a bounded, non-recoverable transport session. Durable business
work stays in the existing C2/C3 stores; losing this registry never replays it.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import traceback
from pathlib import Path
import time
import uuid
from typing import Literal
from urllib.parse import quote
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.auth import TenantCtx, require_concierge, check_concierge_access
from app.config import settings
from app.security import rate_limit, require_csrf_header
from app import concierge_delegation, task_priority, trace, usage, voice_turn_state
from app import debrief_walk

router = APIRouter(prefix="/concierge/voice/gptlive", tags=["concierge"])
log = logging.getLogger(__name__)
MAX_SESSIONS = 16
LIFETIME = 1800
UTTERANCE_GAP_MS = 2500   # a silence this long between heard fragments is counted as a new utterance (a diagnostic), and is the longest gap across which a stood-down request is carried into the next (snapshot)
CAPTURE_FLAGS = ("echoCancellation", "noiseSuppression", "autoGainControl")   # microphone settings the client may report
CAPTURE_COUNTS = {"channelCount": 32, "sampleRate": 384000}                   # ...and their ceilings
LEVEL_BUCKETS = 9   # eight RMS buckets, then a count of samples the browser could not read
TALK_MODES = ("hands_free", "hold")   # how the person talked: hold to talk is a per-device setting
TALK_COUNTS = {"presses": 65535, "latches": 65535, "watchdog_stops": 65535, "talk_ms": 86400000}   # counts and milliseconds the client may report, with their ceilings
LINE_COUNTS = {"closed_ms": 86400000, "opens": 65535, "near_misses": 65535}                       # the owner's volume line: counts and milliseconds, with their ceilings
LINE_FAIL_OPEN = ("no_clone", "track", "context", "analyser", "hidden", "gap", "press", "visible", "warmup", "zero")   # why the line let everything through
CONTEXT_WAIT = 3
TRAILING_WORDS_HEADROOM = 2.4   # seconds the words of a request may arrive after its delegation, whatever the settle setting (see context_wait_seconds)
MAINTENANCE_INTERVAL = 2
AUTHORITY_GRACE = 15   # seconds a conversation may continue on its last successful permission check while checks cannot run
ALLOWANCE_WARN_SECONDS = 60   # the day's live-voice allowance is warned about once, this long before the cut
PROMPT = """You are Blam8's spoken Concierge. Speak concise British English.
Delegate EVERY business question, correction, task instruction or requested change
to the client backend. It uses the existing business agent and permissions. That
includes questions about earlier conversations: when you last spoke, what you
discussed, or what the owner previously asked. The backend has the available saved
history; do not answer those from your native session or claim sessions start fresh.
That also
includes: any customer, record or name lookup (a half-remembered name too: "Whitaker,
I think" is a lookup, not a chat; a rambling message that names a customer and a
promise or a date is a lookup and maybe a reminder, not a chat about it); what someone
wrote or said; what is waiting, outstanding or awaiting approval; what you have done,
what they have signed off, or where a task or request stands ("where are we with that
one?", "what happened to the text to Pat?"); whether a prepared change happened; how to do something in BLAM8 or where a
setting lives; and accounts, VAT or tax questions, which the backend declines honestly.
Never improvise any of those. You answer yourself only greetings, requests to repeat
or clarify unclear words, the interface feedback described below, and the fixed summary below.
Do not invent records, prices, task progress, approval, execution or success.
Development, testing and feedback about BLAM8's live voice or UI are not business
instructions: discuss or clarify without delegating a record lookup. Never volunteer
invoice/customer lists while clarifying unrelated talk. A contextual drafts request
needs a recent explicitly requested business result, not an unsolicited suggestion.
A proposal or prepared draft is NOT an executed change. Only relay business facts
from current backend updates. Unknown means unconfirmed; failed means failed.
If asked what you are or what you can do, say only this: you look things up in their
BLAM8 records and get changes ready that happen only when they tap Confirm on the card;
there is no undo, and a sent email cannot be unsent; you can edit or delete a draft
invoice or quote, but only once they tap Confirm on its card; you do not change or delete
a sent one, a customer or a booking, do accounts, VAT or tax, take payments, send texts or
change the app's settings (prices, hours, plan). For what their own account can see or do,
delegate so the backend answers from their setup.
Say money amounts in full as the backend wrote them — £252.50 is 'two hundred and
fifty-two pounds fifty'; never shorten them. Do not read reference codes aloud — they
are on the screen; name the customer and the amount.
Ask a clarification when words or the intended task are unclear. A spoken yes/no
is never sufficient authorization here: ask the user to use the visible Confirm
or Cancel card. Never claim a message was delivered to its recipient from a send
acknowledgment. You may converse while the backend works. Audio interruption is
not task cancellation. Do not repeat old results after a new instruction.
Do not treat a cough, music or nearby conversation as a new request; keep
listening while the user pauses.
"""
SERVER_EVENTS = ["session.started", "session.closed", "error", "session.input_transcript.delta",
                 "session.output_transcript.delta", "session.usage.updated"]


def available():
    return bool(settings.voice_live_enabled and settings.gptlive_enabled and settings.openai_api_key)


def scope(ctx):
    # access_scope refuses a switched-off member itself (PermissionError, the verified 403 below).
    # The _live_asker read it used to carry duplicated that, cost a fourth read every poll, and
    # turned a failed lookup into a false "Permissions changed" (14 Oct 18:30Z): _asker reads a
    # failed get_member as an inactive member by design, which is right for a page and wrong here.
    return concierge_delegation.access_scope(ctx.tenant_id, ctx.user_id)[0]


@dataclass
class Session:
    id: str
    provider_id: str
    tenant: str
    user: str
    voice_session: str
    permission: str
    socket: object
    allowance_enabled: bool = False
    allowance_limit_seconds: int = 0
    allowance_connected: bool = False
    allowance_heartbeat_at: float = 0
    allowance_warned: bool = False
    created: float = field(default_factory=time.monotonic)
    last_input: float = 0
    last_poll: float = field(default_factory=time.monotonic)
    verified_at: float = field(default_factory=time.monotonic)   # last permission check that could run and passed
    closed: bool = False
    finalized: bool = False
    usage_seconds: float = 0
    fragments: list = field(default_factory=list)
    output: list = field(default_factory=list)
    output_chars: int = 0
    seen: set = field(default_factory=set)
    delegations: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    events: list = field(default_factory=list)
    notices: set = field(default_factory=set)
    approval: dict = field(default_factory=dict)
    consumed_ms: int = -1
    # A business turn that newer speech stood down before commit, kept so the next admission can carry it into
    # the question when the two were one breath: {"text", "start_ms", "end_ms", "event_ids", "turn"}, or {}.
    carry: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)
    sequence: int = 0
    generation: int = 0
    lock: object = field(default_factory=asyncio.Lock)
    ended: object = field(default_factory=asyncio.Event)
    reader: object = None
    jobs: set = field(default_factory=set)
    maintenance: object = None
    speech_context: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    close_reason: str = "local_close"
    client_close_reported: bool = False
    reached_ready: bool | None = None  # Client observation; absent on older clients.
    close_trigger: dict = field(default_factory=dict)
    diagnostic_queue: list = field(default_factory=list)
    diagnostic_job: object = None
    diagnostic_rows: int = 0
    terminal: dict = field(default_factory=dict)
    transcript_segments: dict = field(default_factory=dict)
    transcript_dirty: set = field(default_factory=set)
    transcript_saved: set = field(default_factory=set)
    transcript_lock: object = field(default_factory=asyncio.Lock)
    transcript_job: object = None
    transcript_chars: int = 0
    transcript_fragments: int = 0
    transcript_started: float = field(default_factory=time.time)

    def publish(self, kind, **payload):
        self.sequence += 1
        self.events.append({"seq": self.sequence, "kind": kind, **payload})
        self.events = self.events[-100:]
        return self.sequence

    async def send(self, kind, content=None, delegation_id=None):
        message = {"type": kind, "event_id": uuid.uuid4().hex}
        if content is not None:
            message.update(content=content[:1400], delegation_id=delegation_id)
            if kind == "session.commentary.append":
                self.speech_context = (self.speech_context + [content[:1400]])[-16:]
        if kind.endswith(".append"):
            if len(self.commands) >= 256:
                raise ValueError("Command capacity reached")
            self.commands[message["event_id"]] = {"sent": time.monotonic(), "state": "pending"}
        await asyncio.wait_for(self.socket.send(json.dumps(message)), 5)


_sessions: dict[str, Session] = {}
_activity_sink = ContextVar("gptlive_activity_sink", default=None)
_history_events = ContextVar("gptlive_history_events", default=None)
_creating: set[tuple] = set()
_cancelled: dict[tuple, float] = {}
_diagnostic_jobs: set = set()


def idle_close_seconds():
    """How long a live conversation may sit idle before the browser ends it; 0 = never."""
    return max(0, min(int(settings.gptlive_idle_close_seconds), LIFETIME))


def settle_seconds():
    """How long the delegation wait lets the owner be silent before it takes the words heard as the request
    (GPTLIVE_SETTLE_MS, 300-2500 ms, default 1000): a pause inside one request is not its end. The whole wait
    (context_wait_seconds) is derived from it, so it can never outlast it."""
    return max(0.0, settings.gptlive_settle_ms / 1000)


def context_wait_seconds():
    """The whole wait for a delegation's words: CONTEXT_WAIT, or the settle wait plus TRAILING_WORDS_HEADROOM when
    that is longer. The words of a request often reach the server after the delegation that asks for them
    (transcript lag, about 0.8 s at the median) and the settle runs from the last word's arrival, so the last
    word must arrive within (this wait minus the settle) of the delegation: at least 2.4 s at any setting."""
    return max(CONTEXT_WAIT, settle_seconds() + TRAILING_WORDS_HEADROOM)


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sdp: str = Field(..., min_length=1, max_length=65536)
    voice_session: uuid.UUID
    context_from: AwareDatetime | None = None


class PlaybackTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(..., ge=1, le=65535, strict=True)
    observed_at: AwareDatetime
    action: Literal["silence", "resume", "recover_connection", "playback"]
    reason: Literal["user", "guard", "connection", "unknown"] = "unknown"
    silence_reasons: list[Literal["user", "guard", "connection"]] = Field(..., max_length=3)
    playback_state: Literal["unknown", "waiting", "playing", "blocked", "muted"]
    guard_seq: int | None = Field(None, ge=1, le=1000000, strict=True)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(..., min_length=1, max_length=128)
    operation: str = Field(..., pattern="^(close|interrupt|discard|debrief)$")
    # The Debrief button (operation "debrief" only): start the walk, or move it on.
    step: Literal["start", "next"] = "start"
    delegation: str = Field("", max_length=128)
    turn_id: str = Field("", max_length=40)
    close_reason: Literal["unknown", "end", "mode_switch", "panel_close", "reconnect", "context_clear", "transport_terminal", "start_failure", "provider_closed", "hold_stop", "idle"] = "unknown"
    # Whole seconds the conversation had been idle ("idle" closes only); a count, never speech.
    idle_seconds: int | None = Field(None, ge=0, le=86400, strict=True)
    startup_stage: Literal["unknown", "capture", "offer", "ice", "create", "answer", "awaiting_ready", "ready"] = "unknown"
    peer_state: Literal["unknown", "new", "connecting", "connected", "disconnected", "failed", "closed"] = "unknown"
    channel_state: Literal["unknown", "connecting", "open", "closing", "closed"] = "unknown"
    reached_ready: bool | None = Field(None, strict=True)
    playback_state: Literal["unknown", "waiting", "playing", "blocked", "muted"] = "unknown"
    silence_reasons: list[Literal["user", "guard", "connection"]] | None = Field(None, max_length=3)
    playback_events: list[PlaybackTransition] | None = Field(None, max_length=32)
    playback_events_omitted: int | None = Field(None, ge=0, le=65535, strict=True)
    control_id: str = Field("", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    # What the microphone was actually given (track.getSettings) and the loudness shape of the
    # room: eight RMS buckets plus a ninth count of samples that could not be read. Diagnostics
    # that must never cost a close, so anything off the allow-list is dropped, not rejected.
    capture: dict | None = None
    levels: list | None = None
    # How the person talked when hold to talk was on: presses, taps that latched the mic, milliseconds the
    # mic was open and presses that were ended as stuck. Counts only, dropped rather than rejected as well.
    talk: dict | None = None
    # The owner's volume line (a per-device setting, off by default): where it was, how long it shut sound out,
    # how often it opened, near misses, and every reason it let everything through. Allow-listed like the rest.
    line: dict | None = None

    @field_validator("capture", mode="before")
    @classmethod
    def _capture_allow_list(cls, value):
        if not isinstance(value, dict):
            return None
        kept = {key: value[key] for key in CAPTURE_FLAGS if type(value.get(key)) is bool}
        kept.update({key: value[key] for key, limit in CAPTURE_COUNTS.items()
                     if type(value.get(key)) is int and 0 <= value[key] <= limit})
        return kept or None

    @field_validator("levels", mode="before")
    @classmethod
    def _levels_are_nine_counts(cls, value):
        if isinstance(value, list) and len(value) == LEVEL_BUCKETS and all(type(n) is int and 0 <= n <= 65535 for n in value):
            return list(value)
        return None

    @field_validator("talk", mode="before")
    @classmethod
    def _talk_allow_list(cls, value):
        if not isinstance(value, dict) or value.get("mode") not in TALK_MODES:
            return None
        kept = {"mode": value["mode"]}
        kept.update({key: value[key] for key, limit in TALK_COUNTS.items()
                     if type(value.get(key)) is int and 0 <= value[key] <= limit})
        return kept

    @field_validator("line", mode="before")
    @classmethod
    def _line_allow_list(cls, value):
        # No line set means nothing to report; a line outside 0..1 is not one the browser can have set.
        if not isinstance(value, dict) or type(value.get("line")) is not float or not 0 < value["line"] < 1:
            return None
        kept = {"line": value["line"]}
        kept.update({key: value[key] for key, limit in LINE_COUNTS.items()
                     if type(value.get(key)) is int and 0 <= value[key] <= limit})
        if type(value.get("agc_requested_off")) is bool:
            kept["agc_requested_off"] = value["agc_requested_off"]
        reasons = value.get("fail_open")
        if isinstance(reasons, dict):
            kept["fail_open"] = {name: reasons[name] for name in LINE_FAIL_OPEN
                                 if type(reasons.get(name)) is int and 0 <= reasons[name] <= 65535}
        return kept


def idle_detail(body):
    """The seconds-idle count for a close the browser made because nothing was happening."""
    return {"idle_seconds": body.idle_seconds} if body.close_reason == "idle" and body.idle_seconds is not None else {}


def own(session_id, ctx):
    session = _sessions.get(session_id)
    if not session or (session.tenant, session.user) != (ctx.tenant_id, ctx.user_id):
        raise HTTPException(404, "Conversation unavailable.")
    return session


async def _authorized(session, ctx):
    if session.closed:
        raise HTTPException(410, "Conversation ended. Start a new conversation.")
    if not available():
        raise HTTPException(503, "GPT-Live is unavailable here.")
    # These async route helpers currently contain synchronous database reads.
    await asyncio.to_thread(check_concierge_access, ctx)
    try:
        valid = not getattr(ctx, "viewing_as", None) and await asyncio.to_thread(scope, ctx) == session.permission
    except PermissionError:
        valid = False
    if not valid:
        raise HTTPException(403, "Permissions changed. Start a new conversation.")
    if session.closed or time.monotonic() - session.created > LIFETIME:
        raise HTTPException(410, "Conversation ended. Start a new conversation.")


class AuthorizationFailure(HTTPException):
    """Distinguish an authority check from an unavailable task projection."""


class TaskNoticeFailure(RuntimeError):
    pass


async def authorized(session, ctx):
    started = time.monotonic()
    try:
        result = await _authorized(session, ctx)
    except HTTPException as exc:
        failure = AuthorizationFailure(exc.status_code, exc.detail)
        failure.since_verified = max(0.0, started - session.verified_at)
        raise failure from None
    except Exception as exc:
        failure = AuthorizationFailure(503, "Permissions could not be verified.")
        failure.since_verified = max(0.0, started - session.verified_at)
        raise failure from exc
    session.verified_at = time.monotonic()
    return result


def error_metadata(exc, phase):
    # Unwrap our category wrappers, never a WebSocket exception's OS cause.
    cause = exc.__cause__ if isinstance(exc, (TaskNoticeFailure, AuthorizationFailure)) and exc.__cause__ else exc
    name = type(cause).__name__
    module = type(cause).__module__.split(".")[0]
    allowed = {"TimeoutError", "ConnectionError", "ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK",
               "JSONDecodeError", "ValueError", "TypeError", "RuntimeError", "EOFError", "PermissionError", "HTTPException", "AuthorizationFailure", "OSError"}
    known_module = module in {"builtins", "httpx", "httpcore", "websockets", "postgrest", "psycopg", "psycopg2", "asyncio", "json", "app"}
    safe_name = name if known_module and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "unclassified"
    data = {"phase": phase, "error_category": name if name in allowed else "other", "error_class": safe_name}
    if isinstance(exc, HTTPException):
        data["http_status"] = exc.status_code if 100 <= exc.status_code <= 599 else 0
    if module == "postgrest" and name == "APIError":
        # PostgREST's code (PGRST301 expired token, 57014 statement timeout, an int HTTP status when
        # the body was not JSON) and its fixed message; never hint/details, which can carry row data.
        code = str(getattr(cause, "code", "") or "")
        data["api_code"] = code if re.fullmatch(r"[A-Za-z0-9]{1,12}", code) else "unclassified"
        message = str(getattr(cause, "message", "") or "")
        data["api_message"] = message if re.fullmatch(r"[A-Za-z ,.'-]{1,80}", message) else "redacted"
    if isinstance(exc, AuthorizationFailure) and getattr(exc, "since_verified", None) is not None:
        data["since_verified_s"] = round(min(exc.since_verified, 86400.0), 1)
    if isinstance(cause, ConnectionClosed):
        for key, frame in (("received_close_code", cause.rcvd), ("sent_close_code", cause.sent)):
            code = getattr(frame, "code", None)
            data[key] = code if type(code) is int and 1000 <= code <= 4999 else None
        data["close_code"] = data["received_close_code"] or data["sent_close_code"] or 1006
    # No raw traceback text, source lines or locals: only bounded code locations.
    files = {"gptlive.py", "auth.py", "concierge_routes.py", "db.py", "connection.py", "protocol.py", "tasks.py"}
    data["frames"] = [{"file": Path(f.filename).name if Path(f.filename).name in files else "library",
        "function": f.name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", f.name) else "unknown",
        "line": min(1000000, max(0, f.lineno))} for f in traceback.extract_tb(cause.__traceback__)[-6:]]
    return data


def reserve_terminal(session, reason, phase, exc=None, **metadata):
    # The first authoritative boundary wins; cleanup's 410/EOF cannot replace it.
    if session.closed or session.terminal:
        return False
    session.close_reason = reason
    session.terminal = {"reason": reason, "phase": phase,
        "reserved_at": datetime.now(timezone.utc).isoformat(), **metadata}
    if exc is not None:
        session.terminal.update(error_metadata(exc, phase))
    return True


def authority_reason(session, exc):
    if session.closed:
        return None
    if exc.status_code in {401, 403}:
        return "permission_denied"
    if exc.status_code == 410:
        return "lifetime" if time.monotonic() - session.created > LIFETIME else "conversation_ended"
    return "feature_unavailable" if not available() else "permission_check_unavailable"


def unverifiable(session, exc):
    """A check that could not run, inside the grace since the last one that passed. A verified
    denial (401/403), an ended conversation (410) and a switched-off feature never qualify."""
    return (exc.status_code == 503 and available() and not session.closed
            and getattr(exc, "since_verified", AUTHORITY_GRACE + 1) <= AUTHORITY_GRACE)


async def record_error(session, exc, phase, terminal=False):
    key = "diagnostic_" + phase
    session.counters[key] = min(65535, session.counters.get(key, 0) + 1)
    if session.counters[key] <= 3:
        await safe_trace(session.tenant, "gptlive_error", user_id=session.user,
            session_id=trace.session_ref(session.voice_session), outcome="terminal" if terminal else "secondary" if session.closed or session.terminal else "recoverable",
            detail={**error_metadata(exc, phase), "first_terminal": dict(session.terminal)})


def config():
    return {"model": "gpt-live-1", "store": False, "instructions": PROMPT,
            "audio": {"output": {"voice": "marin"}}, "delegation": {"type": "client"},
            "client": {"data_channel": {"allowed_client_events": [],
                "allowed_server_events": [{"type": name} for name in SERVER_EVENTS]}}}


async def meter_voice(tenant_id, seconds, confirmed):
    """Put the session's voice time on the cost ledger; never lets metering break a close."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(usage.record_voice_session, tenant_id, "gpt-live-1", seconds,
                                confirmed=bool(confirmed))


async def create_provider(sdp):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://api.openai.com/v1/live/sessions",
            headers={"Authorization": "Bearer " + settings.openai_api_key},
            json={"session": config(), "transport": {"type": "webrtc", "sdp": sdp}})
        response.raise_for_status()
        data = response.json()
    provider = data.get("session", {}).get("id")
    answer = data.get("transport", {}).get("sdp")
    if not isinstance(provider, str) or not provider or len(provider) > 1024:
        raise ValueError("Invalid provider session")
    return provider, answer


async def attach(provider):
    return await connect("wss://api.openai.com/v1/live/sessions/" + quote(provider, safe="") + "/attach",
        additional_headers={"Authorization": "Bearer " + settings.openai_api_key},
        open_timeout=10, close_timeout=2, max_size=262144, max_queue=16)


def archive_fragment(session, kind, item):
    # Ten-second transport segments, NOT provider turns or proof of heard audio.
    role = "user" if kind == "session.input_transcript.delta" else "assistant"
    key = (role, item["start_ms"] // 10000)
    if session.transcript_fragments >= 4096 or session.transcript_chars + len(item["delta"]) > 240000:
        session.counters["transcript_capacity"] = session.counters.get("transcript_capacity", 0) + 1
        if session.counters["transcript_capacity"] == 1:
            session.publish("status", text="Spoken history reached its limit; the saved transcript may be partial.")
        return
    pieces = session.transcript_segments.setdefault(key, {})
    if len(pieces) >= 256 or sum(len(p["delta"]) for p in pieces.values()) + len(item["delta"]) > 12000:
        session.counters["transcript_capacity"] = session.counters.get("transcript_capacity", 0) + 1
        if session.counters["transcript_capacity"] == 1:
            session.publish("status", text="Spoken history reached its limit; the saved transcript may be partial.")
        return
    session.transcript_fragments += 1
    session.transcript_chars += len(item["delta"])
    pieces[item["event_id"]] = {**item, "ordinal": session.transcript_fragments}
    session.transcript_dirty.add(key)


async def flush_transcript(session):
    # Cancellation of maintenance/close must not release a writer while its
    # thread is still running and allow an older snapshot to overwrite a new one.
    if session.transcript_job is None or session.transcript_job.done():
        session.transcript_job = asyncio.create_task(_flush_transcript(session))
    await asyncio.shield(session.transcript_job)


async def _flush_transcript(session):
    from app import db
    async with session.transcript_lock:
        while session.transcript_dirty:
            keys = list(session.transcript_dirty)
            if not keys:
                return
            rows = []
            for role, bucket in keys:
                pieces = sorted(session.transcript_segments[(role, bucket)].values(),
                                key=lambda item: (item["start_ms"], item["end_ms"], item["event_id"]))
                for piece in pieces:
                    if piece["event_id"] in session.transcript_saved:
                        continue
                    identity = f"gptlive-observation:{session.tenant}:{session.user}:{session.id}:{session.voice_session}:{piece['event_id']}"
                    rows.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                                 "tenant_id": session.tenant, "user_id": session.user, "role": role,
                                 "created_at": datetime.fromtimestamp(session.transcript_started + piece["start_ms"] / 1000, timezone.utc).isoformat(),
                                 "content": "[Live observed speech — provisional; playback unverified]\n" + piece["delta"],
                                 "links": [], "trail": [{"name":"message_origin", "origin":"live", "session":session.id,
                                     "event_id":piece["event_id"], "start_ms":piece["start_ms"], "end_ms":piece["end_ms"],
                                     "ordinal":piece.get("ordinal",0), "provisional":True}]})
            # One serialized off-loop batch; retrying a lost reply overwrites the
            # same IDs. New fragments arriving during the write remain dirty.
            session.transcript_dirty.difference_update(keys)
            try:
                await asyncio.to_thread(db.save_native_transcript, rows)
                session.transcript_saved.update(row["trail"][0]["event_id"] for row in rows)
            except Exception:
                session.transcript_dirty.update(keys)
                session.counters["transcript_save_error"] = session.counters.get("transcript_save_error", 0) + 1
                if session.counters["transcript_save_error"] == 1:
                    session.publish("status", text="Spoken history could not be saved; some transcript segments may be missing.")
                return


async def finish(session):
    # Invalidate ownership before any network await. Committed/durable work is
    # not cancelled by a transport close; only pre-commit voice turns stand down.
    if session.closed:
        return
    reserve_terminal(session, session.close_reason, "local_close")
    session.closed = True
    session.generation += 1
    session.carry = {}
    if not session.close_trigger:
        session.close_trigger = {"trigger_type": "server_close", "control_id": uuid.uuid4().hex}
    for job in session.delegations.values():
        if job.get("turn"):
            with contextlib.suppress(RuntimeError):
                cancel_turn(session, job["turn"], "close", **session.close_trigger)
    try:
        await session.send("session.close")
        if session.reader is asyncio.current_task() or session.reader is None:
            # This reader owns the close drain: waiting on its own event would
            # prevent session.closed/final usage from ever being consumed.
            async def drain():
                while not session.ended.is_set():
                    event = json.loads(await session.socket.recv())
                    if event.get("type") == "session.closed":
                        await handle_event(session, event, None)
            await asyncio.wait_for(drain(), 2)
        else:
            await asyncio.wait_for(session.ended.wait(), 2)
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.socket.close(), 3)
        if session.reader and session.reader is not asyncio.current_task():
            session.reader.cancel()
        if session.maintenance and session.maintenance is not asyncio.current_task():
            session.maintenance.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(flush_transcript(session), 3)
            if session.transcript_dirty:
                await asyncio.wait_for(flush_transcript(session), 3)
        if session.allowance_enabled:
            with contextlib.suppress(Exception):
                from app import db
                await asyncio.to_thread(db.close_concierge_live_session,
                    session.voice_session, session.tenant, session.user)
        _sessions.pop(session.id, None)
        await meter_voice(session.tenant, session.usage_seconds, session.finalized)
        await safe_trace(session.tenant, "gptlive_session", user_id=session.user,
            outcome="closed" if session.finalized else "unconfirmed",
            detail={"voice_session": session.voice_session, "seconds": session.usage_seconds,
                    "final_usage_confirmed": session.finalized, "reason": session.close_reason,
                    "reached_ready": session.reached_ready,
                    "stages": dict(session.counters), "first_terminal": dict(session.terminal)})


@router.post("/session", dependencies=[Depends(require_csrf_header),
    Depends(rate_limit("gptlive_create", limit=4, window_s=60))])
async def create(body: Create, ctx: TenantCtx = Depends(require_concierge)):
    if not available():
        raise HTTPException(503, "GPT-Live is unavailable here. Choose Standard or try later.")
    if getattr(ctx, "viewing_as", None):
        raise HTTPException(403, "Cannot start this conversation while viewing as someone else.")
    timings = {}
    checkpoint = time.monotonic()
    permission = await asyncio.to_thread(scope, ctx)
    timings["permissions_ms"] = round((time.monotonic() - checkpoint) * 1000)
    # New native transport sessions restore only this requester's saved backend
    # conversation. This is context, never a replay of tools or old announcements.
    from app import db, concierge
    checkpoint = time.monotonic()
    history_rows = await asyncio.to_thread(db.list_concierge_messages, ctx.tenant_id,
        user_id=ctx.user_id, include_shared=False, include_native=False, client=ctx.client)
    if body.context_from:
        cutoff = body.context_from.timestamp()
        def at_or_after_cutoff(row):
            try:
                stamp = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00")).timestamp()
                return stamp >= cutoff
            except (TypeError, ValueError, OverflowError):
                return False
        history_rows = [row for row in history_rows if at_or_after_cutoff(row)]
    history = concierge.sanitize_history(history_rows, 24, 3000)
    timings["history_ms"] = round((time.monotonic() - checkpoint) * 1000)
    owner = (ctx.tenant_id, ctx.user_id)
    if owner in _creating or any((s.tenant, s.user) == owner and not s.closed for s in _sessions.values()):
        raise HTTPException(409, "End the current GPT-Live conversation first.")
    if len(_sessions) + len(_creating) >= MAX_SESSIONS:
        raise HTTPException(503, "Voice capacity is busy. Try later.")
    _creating.add(owner)
    session = None
    provider = None
    allowance_reserved = False
    allowance_limit = 0
    allowance = {"enabled": False, "unlimited": False, "remaining_seconds": None, "limit_seconds": 0}
    try:
        try:
            from app import fairuse
            from app.concierge_routes import _live_allowance
            enabled, limit, status = await asyncio.to_thread(_live_allowance, ctx)
            if enabled:
                allowance_limit = limit
                reserved = await asyncio.to_thread(db.reserve_concierge_live_session,
                    str(body.voice_session), ctx.tenant_id, ctx.user_id, fairuse.london_day(), limit)
                if not reserved.get("ok"):
                    raise HTTPException(429, "Today's live voice allowance has been used.")
                allowance_reserved = True
                _enabled, limit, status = await asyncio.to_thread(_live_allowance, ctx)
                allowance_limit = limit
                allowance = {"enabled": True, "unlimited": limit == 0,
                    "remaining_seconds": status.get("remaining_seconds"), "limit_seconds": limit}
        except HTTPException:
            raise
        except Exception:
            log.exception("native live allowance unavailable before session creation (tenant=%s)", ctx.tenant_id)
            raise HTTPException(503, "Live voice allowance is temporarily unavailable.") from None
        startup_phase = "provider"
        checkpoint = time.monotonic()
        provider, answer = await create_provider(body.sdp)
        timings["provider_ms"] = round((time.monotonic() - checkpoint) * 1000)
        if not isinstance(answer, str) or not answer.startswith("v=") or len(answer) > 65536:
            raise ValueError("Invalid SDP answer")
        startup_phase = "sideband"
        checkpoint = time.monotonic()
        socket = await attach(provider)
        timings["sideband_ms"] = round((time.monotonic() - checkpoint) * 1000)
        session = Session(uuid.uuid4().hex, provider, ctx.tenant_id, ctx.user_id,
                          str(body.voice_session), permission, socket, allowance_enabled=allowance_reserved,
                          allowance_limit_seconds=allowance_limit)
        session.history = history
        startup_phase = "authorization"
        checkpoint = time.monotonic()
        await authorized(session, ctx)
        timings["authorization_ms"] = round((time.monotonic() - checkpoint) * 1000)
        if (ctx.tenant_id, ctx.user_id, str(body.voice_session)) in _cancelled:
            raise HTTPException(409, "Conversation was closed during connection.")
        _sessions[session.id] = session
        session.reader = asyncio.create_task(read_sideband(session, ctx))
        return {"session": session.id, "transport": {"type": "webrtc", "sdp": answer},
                "startup_timings": timings, "allowance": allowance, "context": session.history,
                "idle_close_seconds": idle_close_seconds()}
    except BaseException as exc:
        failed_stage_ms = round((time.monotonic() - checkpoint) * 1000)
        if session:
            await asyncio.shield(finish(session))
        elif provider:
            # Created at the provider but never attached on our side: it still billed the start charge.
            await meter_voice(ctx.tenant_id, 0, False)
            with contextlib.suppress(Exception):
                cleanup = await attach(provider)
                try:
                    await asyncio.wait_for(cleanup.send(json.dumps({"type": "session.close", "event_id": uuid.uuid4().hex})), 3)
                finally:
                    await asyncio.wait_for(cleanup.close(), 3)
        if allowance_reserved and not session:
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.to_thread(db.close_concierge_live_session,
                    str(body.voice_session), ctx.tenant_id, ctx.user_id))
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, HTTPException):
            raise
        # Never log upstream bodies/SDP/transcripts/credentials.
        detail = {**error_metadata(exc, startup_phase), "timings": timings,
                  "failed_stage_ms": failed_stage_ms,
                  "final_usage_confirmed": False}
        if isinstance(exc, httpx.HTTPStatusError):
            detail["upstream_status"] = exc.response.status_code
        await safe_trace(ctx.tenant_id, "gptlive_start", user_id=ctx.user_id, outcome="error", detail=detail)
        raise HTTPException(502, "GPT-Live could not connect. No fallback was started.") from None
    finally:
        _creating.discard(owner)


class ContextUnconfirmed(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class BackendCancelled(RuntimeError):
    """The business turn stood down before any commit (voice_chat's cancelled frame):
    nothing ran, so it is reported as cancelled, never as an unconfirmed result."""



def business_instruction(session, text):
    """Narrow meta guard, not a classifier that guesses a business target."""
    words = re.sub(r"[.!?,;:]", " ", text.lower())
    words = re.sub(r"\bblam eight\b", "blam8", words)
    words = " ".join(words.split())
    target = r"(?:invoices?|reminders?|chasers?|quotes?|bookings?|diary|calendar|appointments?|customers?|contacts?|messages?|emails?|payments?|tasks?|crm|settings|documents?|staff|products?|stock|phone number|address|availability)"
    explicit = bool(re.search(r"\b(?:check|list|show|find|read|look up|send|draft|prepare|write|book|move|update|create|cancel)\b.*\b" + target + r"\b", words))
    product = bool(re.search(r"\b(?:blam8|live conversation|live features|live voice|ui|interface|mist)\b", words))
    development = bool(re.search(r"\b(?:continue (?:working|work) on|(?:i am|i'm|we are|we're|i|we) (?:just )?(?:testing|test|watching|debugging)|(?:develop|debug)(?:ing)? (?:the )?(?:blam8|live|ui))\b|^(?:please )?test (?:the |my )?(?:blam8|live|ui)\b", words))
    visual_feedback = bool(re.search(r"\b(?:ui|interface|mist)\b.*\b(?:expand|release|contract|speed|respond|doesn['’]t|does not)\b", words))
    if product and (development or visual_feedback):
        reason = "mixed_business_meta" if explicit else "non_business_meta"
        raise ContextUnconfirmed(reason, "That sounds like feedback about live voice or the UI. I haven't looked up business records. Please state the separate business task if you want one.")
    return explicit


# A spoken answer is never authority: the visible Confirm or Cancel card is. A bare yes/no is always
# sent back to the card. The longer polite forms ("go on then, do it, thanks") are too, but only while
# a card is live; with no card they are an instruction to carry on with the last discussed thing,
# which only the backend can act on. A phrase that names a scope ("send both") is never matched.
_BARE_APPROVAL = r"(?:yes|yeah|yep|okay|ok|no|nope|confirm|send it|do it)[.!? ]*"
_POLITE_GO_AHEAD = (r"(?:(?:right|yeah|yes|yep|okay|ok|sure|fine|cool|um|uh|erm|no|nope|please)[,.!\s]*){0,4}"
                    r"(?:(?:yes|yeah|yep|okay|ok|no|nope|confirm|send it|do it|go ahead|go on(?: then)?|go for it|"
                    r"carry on|crack on|that['’]?s (?:fine|right|correct)|fine by me|sounds good|not (?:now|yet)|"
                    r"don['’]?t(?: send it| do it)?|leave it)[,.!\s]*){1,3}"
                    r"(?:(?:then|please|thanks|thank you|cheers|mate|now)[,.!\s]*)*")


def _card_live(session):
    """A proposal or prepared draft is waiting on the screen (memory only, no read)."""
    return bool(session.approval.get("proposal") or session.approval.get("draft"))


# Words that take back or replace a request ("never mind", "forget it", "cancel that"). A transcript
# that is only these starts no business turn (snapshot), and they are also a cue (below).
_RETRACTION = (r"(?:never ?mind|forget (?:it|that)|ignore (?:that|it)|scrap that|"
               r"don['’]?t worry(?: about it)?|cancel (?:that|it))")
# What speech has to contain to stop a business turn that is already working (rule "cue_words"): a
# retraction, a stop or hold-on, or a correction. Deliberately NOT cues on their own: yes, yeah, okay,
# please, hello, thanks and bracketed annotations, so a cough, a laugh or a murmur never stops work.
# "make it" is deliberately NOT a cue ("make it Friday" is also everyday television dialogue); "make that" is.
# "change", "swap" and "redo" with "it" or "that" are deliberately NOT cues either: they are the ordinary words
# of a request ("can you change it to two hundred", "swap it for the other one"), and as a cue they cancelled
# the owner's own request when they were its second half (rig N20, 15 Oct). "scratch that" and "cancel it" are.
# Matched on the joined, tag-stripped text of everything heard since the turn's own question, so a cue
# split across fragments ("never" + " mind", "No," + " not Leah") is still one cue.
_INTERRUPT_CUE = re.compile(
    r"\b(?:" + _RETRACTION + r"|stop|wait|hold on|hang on|one (?:sec|second|moment)|actually|sorry|not|instead"
    r"|i meant|make that|rather than|other one"
    r"|(?:scratch|cancel)\s+(?:it|that)|start again|try again|that['’]?s wrong|wrong one)\b"
    r"|\bno\b(?:[,.!]|\s+no\b)", re.I)
INPUT_HELD_ROWS = 32   # traced rows for held fragments per session; the stage counter keeps counting past it


_WHITESPACE = re.compile(r"\s")
PENDING_FRAGMENTS = 32   # a cue is judged on the most recent fragments only (it spans at most two) ...
PENDING_CHARS = 4000     # ... and the most recent characters, so the cost per event has a ceiling


def tail_text(parts):
    """The joined, tag-stripped text of the most recent PENDING_FRAGMENTS fragments, cut to at most the
    last PENDING_CHARS characters before stripping. A provider fragment may legally be 2,000 characters
    with no spaces and the pool holds 256 of them; an older cue was acted on when it arrived. The cut moves
    on to the next whitespace so it cannot start mid-word ("nonstop" must not become "stop"); a tail with
    no whitespace in it is one long word and has nothing to judge."""
    joined = "".join(p["delta"] for p in parts[-PENDING_FRAGMENTS:])
    if len(joined) > PENDING_CHARS:
        gap = _WHITESPACE.search(joined, len(joined) - PENDING_CHARS)
        joined = joined[gap.end():] if gap else ""
    return strip_tags(joined)


def heard_cue(parts):
    """Does the speech in these fragments (in timestamp order) contain a correction or stop cue?"""
    return bool(_INTERRUPT_CUE.search(tail_text(parts)))


def turn_boundary(session, job):
    """Speech ending at or before this is the turn's own question (or already consumed), never new.
    The job's own frozen window counts even before consumed_ms has caught up with it."""
    return max(session.consumed_ms, job.get("context_end", -1))


def interrupts(session, job, item, memo=None):
    """Why this fragment should stop `job`'s working turn: "cue" (a correction or stop cue among the
    speech heard since its question), "new_input" (rule any_words: any speech), or None. `memo` shares
    the pending text between the jobs of one event (they differ only in the F9 window)."""
    boundary = turn_boundary(session, job)
    if item["end_ms"] <= boundary:
        return None   # the turn's own words, arriving late: they can never cancel it
    if settings.gptlive_interrupt_rule == "any_words":
        return "new_input"
    return "cue" if _INTERRUPT_CUE.search(pending_input(session, boundary, item, memo)["text"]) else None


def anchored_request(session, job, item=None, candidates=None):
    """A delegation the model raised past this working turn's window and anchored on speech the turn
    never consumed: a fragment ending after the window that starts at or before the delegation's
    offset (`item` is a fragment arriving right now). A delegation with no such speech behind it is a
    retry or a duplicate, not a new request, and must not stand the working turn down. `candidates`
    stands in for the queued delegations (default: the session's), so a delegation can be judged
    before it is registered."""
    boundary = turn_boundary(session, job)
    # The anchoring speech must START at or after the window's end: a late word of the turn's own request
    # that crosses it (it began inside the window) is the turn's tail, not a new request.
    heard = [p for p in session.fragments if p["start_ms"] >= boundary and p["end_ms"] > boundary]
    if item is not None and item["start_ms"] >= boundary and item["end_ms"] > boundary:
        heard.append(item)
    return next((other for other in (session.delegations.values() if candidates is None else candidates)
                 if other is not job and other.get("state") == "queued" and other.get("offset", -1) > boundary
                 and any(p["start_ms"] <= other["offset"] for p in heard)), None)


def superseded(session, job, end_ms):
    """Has the owner moved on since this turn's question ended at `end_ms`? Only a newer request
    (another delegation working past it, or queued and anchored on speech after it) or, for words, a
    correction or stop cue among them (any words at all under rule any_words). Background talk and a
    re-delegation with nothing new behind it do not hide a finished answer."""
    # A cue (or any word under any_words) that already stood this turn down was judged when it arrived and
    # may since have scrolled out of the bounded tail, so the request itself is remembered.
    if job.get("cancel_requested"):
        return True
    later = [p for p in session.fragments if p["end_ms"] > end_ms]
    # The queued half is belt and braces for one window: while the turn is working, a queued delegation
    # anchored on later speech has already set cancel_requested above (A5 or the deferral), but once the
    # turn has finished and only its commentary is left to send, it is no longer "working", nothing stands
    # it down, and this is what still withholds the commentary. Anchoring speech must start at or after
    # end_ms (the turn's own late tail word is not a new request). The working half covers another turn
    # (a debrief beside a delegation) that is running past end_ms.
    if any(other is not job and other.get("offset", -1) > end_ms
           and (other.get("state") == "working"
                or (other.get("state") == "queued"
                    and any(end_ms <= p["start_ms"] <= other["offset"] for p in later)))
           for other in session.delegations.values()):
        return True
    if not later:
        return False
    return settings.gptlive_interrupt_rule == "any_words" or bool(_INTERRUPT_CUE.search(pending_input(session, end_ms)["text"]))


def strip_tags(text):
    """Provider annotations such as "[inhale] [cough]" arrive split across fragments, so they
    are stripped from the JOINED text (a dangling opener or closer included); the archived
    fragments keep them. The result is whitespace-normalised."""
    return " ".join(re.sub(r"\[[^\[\]\r\n]{0,40}\]|\[[^\[\]\s]{0,40}|[^\[\]\s]{0,40}\]", " ", text).split())


def pending_input(session, boundary, item=None, memo=None):
    """What has been heard past `boundary` (plus the fragment arriving now): the count and span of all
    of it, and the bounded text to judge (tail_text of the most recent fragments, in timestamp order).
    Computed once per boundary when a `memo` dict is passed, so one event costs one join however many
    turns are working."""
    if memo is not None and boundary in memo:
        return memo[boundary]
    parts = [p for p in session.fragments if p["end_ms"] > boundary]
    if item is not None:
        parts.append(item)
    parts.sort(key=lambda p: (p["start_ms"], p["end_ms"], p["event_id"]))
    view = {"count": len(parts), "text": tail_text(parts),
            "span_ms": (max(p["end_ms"] for p in parts) - min(p["start_ms"] for p in parts)) if parts else 0}
    if memo is not None:
        memo[boundary] = view
    return view


def input_shape(view):
    """What is waiting, as counts and milliseconds only. The words never enter a trace. The word count
    is of the judged tail (see tail_text); fragments and span cover everything pending."""
    return {"pending_words": len(view["text"].split()), "pending_fragments": view["count"],
            "pending_span_ms": view["span_ms"]}


def snapshot(session, job):
    # Provider offset anchors a delegation; it is not the end of the words.
    # Freeze the complete, settled server-owned window before admission.
    parts = sorted((p for p in session.fragments if p["end_ms"] > session.consumed_ms),
                   key=lambda p: (p["start_ms"], p["end_ms"], p["event_id"]))
    # A retry has a floor, the end of the turn it repeats: only speech that STARTS at or after it can anchor
    # the retry (the old turn's own late tail word, crossing that end, is not a new request). -1 for any other job.
    floor = job.get("retry_floor", -1)
    anchored = bool(parts and any(floor <= p["start_ms"] <= job["offset"] for p in parts))
    job["context_end"] = max((p["end_ms"] for p in parts), default=session.consumed_ms) if anchored else session.consumed_ms
    job["context_count"] = len(parts)
    gaps = [b["start_ms"] - a["end_ms"] for a, b in zip(parts, parts[1:])]
    job["context_meta"] = {"context_count": min(256, len(parts)), "context_start_ms": parts[0]["start_ms"] if parts else -1,
        "context_end_ms": max((p["end_ms"] for p in parts), default=-1), "delegation_offset_ms": job["offset"],
        "fragment_bounds_ms": [[p["start_ms"], p["end_ms"]] for p in parts[:16]],
        "fragment_arrival_delta_ms": [max(-30000, min(3000, round((p.get("received", job.get("received", 0)) - job.get("received", 0)) * 1000))) for p in parts[:16]],
        "fragment_bounds_omitted": max(0, len(parts) - 16),
        # How scattered the window is: the longest silence between its fragments, and how many
        # separate stretches of speech (silences of UTTERANCE_GAP_MS or more) it holds. Diagnostic only.
        "largest_gap_ms": max(gaps) if gaps else -1,
        "utterances": (1 + sum(gap >= UTTERANCE_GAP_MS for gap in gaps)) if parts else 0}
    # A retry (a re-delegation of a request already running) is not a second request.
    if job.get("ambiguous") or sum(j["state"] == "queued" and not j.get("retry") for j in session.delegations.values()) > 1:
        raise ContextUnconfirmed("multiple_delegations", "Please give one task instruction at a time.")
    offset = job["offset"]
    if not parts or not any(floor <= p["start_ms"] <= offset for p in parts):
        return None
    if job["context_end"] - parts[0]["start_ms"] > 30000:
        raise ContextUnconfirmed("context_limit", "Please repeat one shorter complete instruction.")
    if any(a["end_ms"] > b["start_ms"] for a, b in zip(parts, parts[1:])):
        raise ContextUnconfirmed("overlap_conflict", "The words overlapped. Please repeat one complete instruction.")
    text = "".join(p["delta"] for p in parts).strip()
    # Provider annotations are stripped from the joined text (strip_tags). Nothing left
    # fails closed below as incomplete_text.
    text = strip_tags(text)
    if len(text) > 2000:
        raise ContextUnconfirmed("context_limit", "Please repeat one shorter complete instruction.")
    # A bare retraction ("never mind", "forget it", "cancel that") starts no business turn: the working
    # turn, if any, was already stood down by these very words. Inside a longer sentence it is an instruction.
    if re.fullmatch(r"(?:(?:actually|oh|okay|ok|no|sorry|right|um|uh|erm)[,.!\s]*){0,3}"
                    + _RETRACTION +
                    r"(?:[,.!\s]*(?:then|please|thanks|thank you|cheers))?[.!?\s]*", text, re.I):
        raise ContextUnconfirmed("retraction", "Okay, leaving that — that message changed nothing. If you mean the card on screen, tap Cancel on it.")
    if re.fullmatch(_BARE_APPROVAL, text, re.I) or (_card_live(session) and re.fullmatch(_POLITE_GO_AHEAD, text, re.I)):
        raise ContextUnconfirmed("approval_requires_card", "Use the visible Confirm or Cancel card. No change was executed by this reply.")
    if not text or re.search(r"\b(and|or|to|the|with|for|but)\W*$", text, re.I):
        raise ContextUnconfirmed("incomplete_text", "Please repeat the complete instruction.")
    # A request stood down by its own second half is carried into this one (see delegate's BackendCancelled
    # handler): the same breath on the provider's audio clock, however long the model took to re-delegate. It is
    # joined only after the checks above, so "never mind" or "yes" after a split stay what they are, and it is
    # judged as the whole it now is (size, window, product talk). Nothing here changes session state; the carry
    # is cleared where a request is admitted or refused.
    carry = session.carry
    carried = bool(carry) and 0 <= parts[0]["start_ms"] - carry["end_ms"] <= UTTERANCE_GAP_MS
    if carried:
        text = carry["text"] + " " + text
        if len(text) > 2000 or job["context_end"] - carry["start_ms"] > 30000:
            raise ContextUnconfirmed("context_limit", "Please repeat one shorter complete instruction.")
    business_instruction(session, text)
    job["context_event_ids"] = (list(carry["event_ids"]) if carried else []) + [piece["event_id"] for piece in parts]
    job["context_start"] = carry["start_ms"] if carried else parts[0]["start_ms"]
    if carried:
        job["context_meta"]["carried"] = True   # a bool: the words never enter a trace
    return text, job["context_end"]


async def run_backend(ctx, question, history, turn, voice_session, debrief=""):
    from app.concierge_routes import VoiceChatRequest, voice_chat
    response = await voice_chat(VoiceChatRequest(question=question, history=history,
                              turn_id=turn, voice_session=voice_session, debrief=debrief), ctx)
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Business admission refused.")
    await safe_trace(ctx.tenant_id, "gptlive_stage", user_id=ctx.user_id,
        session_id=trace.session_ref(voice_session), outcome="backend_admitted",
        detail={"stage":"backend_admitted", "turn_id":turn})
    buffer = ""
    async for chunk in response.body_iterator:
        buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            if frame.startswith("data: "):
                item = json.loads(frame[6:])
                if item.get("t") == "tool" and _activity_sink.get():
                    await _activity_sink.get()()
                if item.get("t") == "done":
                    return item
                if item.get("t") == "cancelled":
                    raise BackendCancelled("Business turn interrupted")
                if item.get("t") == "error":
                    raise RuntimeError("Business turn interrupted")
    raise RuntimeError("Business result unconfirmed")


async def delegate(session, delegation_id, ctx):
    job = session.delegations[delegation_id]
    generation = session.generation
    try:
        async with session.lock:
            deadline = time.monotonic() + context_wait_seconds()
            while time.monotonic() < deadline:
                await authorized(session, ctx)
                if session.generation != generation:
                    return
                if time.monotonic() - max(session.last_input, job["received"]) >= settle_seconds():
                    value = snapshot(session, job)
                    if value:
                        break
                await asyncio.sleep(.1)
            else:
                raise ContextUnconfirmed("missing_context", "I did not receive a complete instruction. Please repeat it.")
            question, end_ms = value
            session.carry = {}   # an admission uses a carried request or leaves it behind: it is never kept for a later one
            job.update(state="working", turn=uuid.uuid4().hex, started=time.monotonic())
            await stage(session, "backend_requested", turn=job["turn"], **job.get("context_meta", {}))
            await authorized(session, ctx)
            if session.generation != generation:
                return
            session.consumed_ms = end_ms
            session.publish("question", text=question, turn_id=job["turn"])
            history = session.history[-24:]
            session.history.append({"role": "user", "content": question})
            session.history = session.history[-24:]
            session.publish("context", turns=session.history)
            async def tool_activity():
                await authorized(session, ctx)
                if not session.closed and session.generation == generation:
                    session.publish("activity", turn_id=job["turn"], state="running", text="Checking business data")
            token = _activity_sink.set(tool_activity)
            history_token = _history_events.set({"session":session.id,"event_ids":job.get("context_event_ids",[])})
            session.publish("activity", turn_id=job["turn"], state="running", text="Request received")
            try:
                result = await asyncio.wait_for(run_backend(ctx, question, history, job["turn"], session.voice_session), 60)
            finally:
                _activity_sink.reset(token)
                _history_events.reset(history_token)
                if not session.closed and session.generation == generation:
                    session.publish("activity", turn_id=job["turn"], state="finished", text="")
            await stage(session, "backend_result", turn=job["turn"])
            await authorized(session, ctx)
            if session.generation != generation:
                return
            # A newer request or a correction may supersede this foreground reply (background
            # talk does not; see superseded). Durable work remains visible in the existing
            # task/receipt paths.
            if superseded(session, job, end_ms):
                session.publish("status", text="A result is available in chat; your newer words take priority.")
                return
            session.history.append({"role": "assistant", "content": result.get("reply", "")[:3000]})
            session.history = session.history[-24:]
            session.publish("context", turns=session.history)
            session.approval = {"proposal": (result.get("proposal") or {}).get("proposal_id"),
                                "draft": (result.get("draft") or {}).get("log_id")}
            session.publish("result", turn_id=job["turn"], payload=result)
            await stage(session, "result_published", turn=job["turn"])
            job["state"] = "complete"
            text = result.get("reply", "")
            if result.get("proposal") or result.get("draft"):
                text = "Prepared for review, not executed. Use the visible card to confirm. " + text
            elif result.get("tasks"):
                text = "The task was accepted for background work, not completed. " + text
            if text:
                await authorized(session, ctx)
                if session.closed or session.generation != generation or superseded(session, job, end_ms):
                    return
                await session.send("session.commentary.append", text, delegation_id)
    except ContextUnconfirmed as exc:
        job["state"] = "clarification"
        # Quarantine only this frozen rejected window, not later input. Any request stood down just before
        # is dropped with it: a refusal (a retraction included) says the owner is not carrying on.
        session.consumed_ms = max(session.consumed_ms, job.get("context_end", session.consumed_ms))
        session.carry = {}
        marked = bool(job.get("retry"))
        # Silence for a retry is decided now, not when it was raised: speech that began after the turn it
        # repeats and was never consumed means the owner said something this delegation did not get, so
        # it is asked about as before, unless a real request queued behind it is about to take those words.
        # The row says how much was waiting (a count, never the words).
        def claimed(p):
            return any(o is not job and o.get("state") == "queued" and not o.get("retry") and p["start_ms"] <= o.get("offset", -1)
                       for o in session.delegations.values())
        unheard = sum(1 for p in session.fragments if p["end_ms"] > session.consumed_ms
                      and p["start_ms"] >= job.get("retry_floor", -1) and not claimed(p)) if marked else 0
        retry = exc.reason == "missing_context" and marked and not unheard
        await stage(session, "context_unconfirmed", reason=exc.reason, **job.get("context_meta", {}),
                    **({"retry": True, "pending_fragments": unheard} if marked else {}))
        if retry:
            return   # a re-delegation of a request that was already running: nothing new was said, so nothing to ask
        if not session.closed and session.generation == generation:
            session.publish("status", text=str(exc))
            with contextlib.suppress(Exception):
                await session.send("session.commentary.append", str(exc), delegation_id)
    except BackendCancelled:
        job["state"] = "cancelled"
        # A discard or a close bumps the generation (and a close shuts the session), so whatever else
        # is queued at that moment cannot turn their reason into new_input.
        newer = session.generation == generation and not session.closed and superseded(session, job, session.consumed_ms)
        # Carry the stood-down request into the next admission (snapshot) when the two were one breath. This runs
        # before the first await: the lock was released on the way here, so a delegation queued behind this one
        # takes it the moment this task yields, and it must find the carry. cue_words only: any_words stays the
        # exact earlier behaviour. The unanswered question leaves the history so the backend is not told to ignore
        # it; without a re-delegation nothing was lost, the carry is bounded by the audio clock (UTTERANCE_GAP_MS).
        carried = newer and settings.gptlive_interrupt_rule == "cue_words"
        rerun = carried and anchored_request(session, job) is not None   # judged now: a queued one is taken by the next yield
        if carried:
            session.carry = {"text": question, "start_ms": job.get("context_start", job["context_end"]), "end_ms": job["context_end"],
                             "event_ids": list(job.get("context_event_ids", [])), "turn": job["turn"]}
            if session.history and session.history[-1] == {"role": "user", "content": question}:
                session.history = session.history[:-1]
                session.publish("context", turns=session.history)
        await stage(session, "backend_cancelled", turn=job.get("turn"),
                    reason="new_input" if newer else ("close" if session.closed else "discard"))
        # In an open conversation whatever stood the turn down (a cue, a new request) is shown on the screen, and
        # the voice is told so it does not claim the work. It is told NOT to announce the stop: the owner who spoke
        # again usually meant their newer words as the request (rig N20, 15 Oct), so an announced "I paused that"
        # was noise at best. A discard or a close has already told the user what it did.
        if not session.closed and session.generation == generation:
            # "Say it again" is only true when nothing is on its way: with a newer request already raised on those
            # words, the stood-down one is carried into it, so the screen does not ask for a repeat.
            status = "Stopped before anything ran — your newer words took over. Nothing was changed"
            status += "." if rerun else "; say it again if you still want it."
            session.publish("status", text=status, reason="backend_cancelled")
            with contextlib.suppress(Exception):
                await session.send("session.commentary.append", "The earlier request was stopped before anything ran because the user spoke again, and nothing was changed. Do not announce this and do not say it was done; treat their newer words as the current request and carry on. Only if they told you to stop, acknowledge in a few words.", delegation_id)
    except Exception as exc:
        job["state"] = "unconfirmed"
        await stage(session, "backend_unconfirmed", reason=f"http_{exc.status_code}" if isinstance(exc, HTTPException) else type(exc).__name__, turn=job.get("turn"))
        if not session.closed:
            session.publish("status", text="The result is unconfirmed. Check chat before trying another action.")
    finally:
        if job["state"] in {"queued", "working"}:
            job["state"] = "unconfirmed"


DEBRIEF_STOPPED = ("The debrief was stopped before anything ran — your newer words took over. "
                   "Nothing was changed; press Debrief again if you still want it.")


async def debrief_refusal(ctx):
    """Why this person may not run the debrief (owner and managers only), or None."""
    from app.concierge_routes import _live_asker
    return debrief_walk.refusal(ctx, await asyncio.to_thread(_live_asker, ctx))


async def run_debrief(session, ctx, step):
    """The Debrief button, native mode (docs/gptlive.md). The data channel
    allows no client commands, so the button asks THIS server, which runs the
    backend turn a delegation would — "Run me through my debrief." as the
    owner's request, the walk's note on the Concierge's side — and hands the
    verified reply to the native voice as commentary, as it does a delegated
    result. Deliberately parallel to delegate(): same lock, same authority
    re-checks, same supersession by newer words, and the reply joins
    speech_context, so the output guard treats it as supported."""
    generation = session.generation
    turn = uuid.uuid4().hex
    question = debrief_walk.NEXT_LINE if step == "next" else debrief_walk.START_LINE
    # The window is fixed at the button, before the lock: a debrief queued behind a working turn is
    # "working" from here, and a delegation or word arriving meanwhile must not be taken as new
    # speech past it. Inside the lock it moves up to everything said by then.
    job = {"offset": 0, "received": time.monotonic(), "state": "working", "turn": turn, "ambiguous": False,
           "started": time.monotonic(),
           "context_end": max([p["end_ms"] for p in session.fragments], default=session.consumed_ms)}
    session.delegations["debrief-" + turn[:16]] = job
    try:
        async with session.lock:
            await authorized(session, ctx)
            if session.generation != generation:
                return
            if job.get("cancel_requested"):
                # Newer words stood it down while it waited for the lock: it never begins, and it must
                # not take those words as its own (the request that stood it down needs them).
                job["state"] = "cancelled"
                await stage(session, "backend_cancelled", turn=turn, reason="new_input")
                if not session.closed:
                    session.publish("status", text=DEBRIEF_STOPPED, reason="backend_cancelled")
                return
            # Anything said before the button is not part of this request.
            end_ms = max([p["end_ms"] for p in session.fragments], default=session.consumed_ms)
            session.consumed_ms = max(session.consumed_ms, end_ms)
            session.carry = {}   # an admission: a request stood down earlier is not carried past the button
            job["context_end"] = end_ms
            await stage(session, "backend_requested", turn=turn, debrief=step)
            session.publish("question", text=question, turn_id=turn)
            history = session.history[-16:]
            session.history.append({"role": "user", "content": question})

            async def tool_activity():
                await authorized(session, ctx)
                if not session.closed and session.generation == generation:
                    session.publish("activity", turn_id=turn, state="running", text="Checking business data")
            token = _activity_sink.set(tool_activity)
            history_token = _history_events.set({"session": session.id, "event_ids": []})
            session.publish("activity", turn_id=turn, state="running", text="Request received")
            try:
                result = await asyncio.wait_for(
                    run_backend(ctx, question, history, turn, session.voice_session, debrief=step), 60)
            finally:
                _activity_sink.reset(token)
                _history_events.reset(history_token)
                if not session.closed and session.generation == generation:
                    session.publish("activity", turn_id=turn, state="finished", text="")
            await stage(session, "backend_result", turn=turn)
            await authorized(session, ctx)
            if session.generation != generation:
                return
            if superseded(session, job, end_ms):
                session.publish("status", text="A result is available in chat; your newer words take priority.")
                return
            session.history.append({"role": "assistant", "content": result.get("reply", "")[:3000]})
            session.history = session.history[-16:]
            session.approval = {"proposal": (result.get("proposal") or {}).get("proposal_id"),
                                "draft": (result.get("draft") or {}).get("log_id")}
            session.publish("result", turn_id=turn, payload=result)
            await stage(session, "result_published", turn=turn)
            job["state"] = "complete"
            text = result.get("reply", "")
            if result.get("proposal") or result.get("draft"):
                text = "Prepared for review, not executed. Use the visible card to confirm. " + text
            elif result.get("tasks"):
                text = "The task was accepted for background work, not completed. " + text
            if text:
                await authorized(session, ctx)
                if (session.closed or session.generation != generation
                        or superseded(session, job, end_ms)):
                    return
                # No delegation id: nothing the provider created asked for this.
                await session.send("session.commentary.append", text)
    except BackendCancelled:
        # voice_chat's cancelled frame: the turn stood down before any commit, so nothing ran.
        job["state"] = "cancelled"
        newer = session.generation == generation and not session.closed
        await stage(session, "backend_cancelled", turn=turn,
                    reason="new_input" if newer else ("close" if session.closed else "discard"))
        if newer:
            session.publish("status", text=DEBRIEF_STOPPED, reason="backend_cancelled")
    except Exception as exc:
        job["state"] = "unconfirmed"
        await stage(session, "backend_unconfirmed", reason=f"http_{exc.status_code}" if isinstance(exc, HTTPException) else type(exc).__name__, turn=turn)
        if not session.closed:
            session.publish("status", text="The debrief is unconfirmed. Check chat before trying again.")
    finally:
        if job["state"] in {"queued", "working"}:
            job["state"] = "unconfirmed"


async def safe_trace(*args, **kwargs):
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.to_thread(trace.emit, *args, **kwargs), .25)


async def stage(session, name, reason=None, turn=None, **metadata):
    session.counters[name] = session.counters.get(name, 0) + 1
    await safe_trace(session.tenant, "gptlive_stage", user_id=session.user,
        session_id=trace.session_ref(session.voice_session), outcome=name,
        detail={"stage": name, "reason": reason, "turn_id": turn,
                "observed_at": datetime.now(timezone.utc).isoformat(), **metadata})


def diagnostic(session, name, *, count=True, **metadata):
    """Bounded best-effort IO, never awaited by guard/cancellation paths."""
    if count:
        session.counters[name] = session.counters.get(name, 0) + 1
    needs_worker = session.diagnostic_job is None or session.diagnostic_job.done()
    if session.diagnostic_rows >= 256 or len(session.diagnostic_queue) >= 64 or (needs_worker and len(_diagnostic_jobs) >= 16):
        session.counters["diagnostic_dropped"] = session.counters.get("diagnostic_dropped", 0) + 1
        return
    session.diagnostic_rows += 1
    session.diagnostic_queue.append({"stage": name, "reason": None, "turn_id": None,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "application_session_id": session.id, **metadata})
    if needs_worker:
        session.diagnostic_job = asyncio.create_task(drain_diagnostics(session))
        _diagnostic_jobs.add(session.diagnostic_job)
        session.diagnostic_job.add_done_callback(_diagnostic_jobs.discard)


async def drain_diagnostics(session):
    while session.diagnostic_queue:
        detail = session.diagnostic_queue.pop(0)
        await safe_trace(session.tenant, "gptlive_stage", user_id=session.user,
            session_id=trace.session_ref(session.voice_session), outcome=detail["stage"], detail=detail)


def cancel_turn(session, turn, source, **trigger):
    requested_at = datetime.now(timezone.utc).isoformat()
    request_id = uuid.uuid4().hex
    outcome = None
    try:
        outcome = voice_turn_state.cancel(voice_turn_state.key(session.tenant, session.user, turn))
        return outcome
    finally:
        diagnostic(session, "input_cancellation" if source == "new_input" else "turn_cancellation",
            source=source, turn_id=turn, request_id=request_id, requested_at=requested_at,
            returned_at=datetime.now(timezone.utc).isoformat(), consumed_ms=session.consumed_ms,
            cancel_status=outcome["status"] if outcome else "error",
            close_reason=session.close_reason if source == "close" else None, **trigger)


def mask_quotes(text):
    # Preserve positions for fragmented transcript buffering; punctuation inside
    # quoted examples must never become a separate affirmative claim.
    return re.sub(r'"[^"\n]*(?:"|$)|“[^”\n]*(?:”|$)|(?<!\w)\x27[^\x27\n]*(?:\x27|$)', lambda m: " " * len(m[0]), text)


_ACTIONS = {"sent", "booked", "cancelled", "charged", "updated", "completed", "executed"}
_PREDICATE = r"\b(sent|booked|cancelled|canceled|charged|updated|completed|executed)\b"
# A denial is matched where it ends at the predicate, never a negative somewhere in
# its history. Only bounded auxiliary/adverb bridges are accepted. Subject
# negatives require passive syntax; arbitrary intervening verbs/adjectives
# must remain claim candidates (e.g. "Nothing prevented it: I sent ...").
_MODIFIERS = r"(?:(?:yet|ever|actually|successfully|already)\s+){0,2}"
_AUXILIARIES = r"(?:(?:have|has|had|be|been|being)\s+){0,2}"
_PASSIVE = r"(?:is|are|was|were|(?:has|have|had)\s+been)\s+"
_NOUN = r"[\w-]+"
_DENIAL = (
    rf"(?:\b(?:not|never)\s+|\b\w+n['’]t\s+){_AUXILIARIES}{_MODIFIERS}$"
    rf"|\b(?:neither|none|nothing)['’]s\s+(?:been\s+)?{_MODIFIERS}$"
    rf"|\b(?:none|nothing)\s+{_PASSIVE}{_MODIFIERS}$"
    rf"|\b(?:none|nothing|anything)\s+{_MODIFIERS}$"
    rf"|\b(?:neither|no)(?:\s+{_NOUN}(?:\s+nor\s+{_NOUN})?)?\s+{_PASSIVE}{_MODIFIERS}$"
    rf"|\bnor\s+{_PASSIVE}(?:(?:the\s+)?{_NOUN}\s+)?{_MODIFIERS}$"
)
# What may stand directly before a completion word that is used as the verb of a claim.
_SUBJECT_BRIDGE = (r"(?:i|we|you|it|that|this|they|he|she|all|both|everything|done|is|are|was|were|be|been|being"
                   r"|has|have|had|got|now|just|already|successfully|yes|not|never|\w+n['’]t)(?:['’](?:ve|s|re|ll|d|m))?")
_LEAD_MODIFIERS = r"(?:(?:yet|ever|actually|successfully|already|now|just)\s+){0,2}"
_OBJECT_START = r"(?:it|that|this|them|both|her|him|you|everything|all|the|your|my|our|a|an|one|two|three|every)"
# No two adjacent optional whitespace runs, so it cannot backtrack on a long run of spaces
# (a quoted span is masked to spaces): linear however long the clause is.
_BARE_LEAD_IN = r"\s*(?:(?:ok(?:ay)?|right|yes|yeah|done|sorted|all)\b[,.!\s—–:-]*){0,2}(?:[.!]\s*)?"
# What may follow a noun plus 's standing for "is" ("The booking's cancelled."): the end of the clause,
# an adverb or a preposition. A possessive before a noun ("Tom's booked jobs") is not a claim.
_AFTER_CONTRACTED_IS = r"\s*(?:[.!?,;:—–]|$|(?:now|just|already|yet|too|also|again|for|on|in|at|to|by|since|until|up|out|off|down)\b)"


def _claim_position(text, match):
    """A completion word is only a claim candidate when it is the verb of a claim:
    after a subject or auxiliary ("I've sent", "it's been booked", "was cancelled",
    "all sent"), before an object ("booked her in", "sent both"), or standing as
    the whole clause ("Sent.", "Done, cancelled."). A word that describes a record
    ("unpaid sent bills", "two jobs booked for Tuesday", "fully booked", "nothing
    gets booked in") is not a claim."""
    after = text[match.end():]
    # Whitespace runs count as one, and only the last few words decide the position: every pattern
    # below is bounded however long the clause is (a quoted span is masked to a long run of spaces).
    before = re.sub(r"\s+", " ", text[:match.start()])
    tail = before[-80:].split(" ", 1)[-1] if len(before) > 80 else before
    if re.search(rf"\b{_SUBJECT_BRIDGE}\s+{_LEAD_MODIFIERS}$", tail, re.I):
        return True
    if re.match(rf"\s+{_OBJECT_START}\b", after, re.I):
        return True
    # A capitalised word as the subject ("Bob charged Alice", "Invoice sent."), when the clause ends or an
    # adverb, preposition or another name follows. A determiner or adjective that merely starts the
    # sentence ("The sent invoices are listed", "Two cancelled bookings show") is not a subject.
    if re.search(rf"\b[A-Z][a-z]+\s+{_LEAD_MODIFIERS}$", tail) and (
            re.match(_AFTER_CONTRACTED_IS, after, re.I) or re.match(r"\s+[A-Z]", after)):
        return True
    # A name or noun plus 's standing for "is" ("Leah's booked in", "The booking's cancelled").
    if re.search(rf"\w['’]s\s+{_LEAD_MODIFIERS}$", tail) and re.match(_AFTER_CONTRACTED_IS, after, re.I):
        return True
    # A capitalised word that opens the clause, with its complement ("Booked for Friday.", "Sent to Tom."):
    # the capital tells it from a lower-case description after a comma (", booked for Tuesday,").
    if (match.group()[:1].isupper() and re.fullmatch(_BARE_LEAD_IN, before, re.I)
            and re.match(r"\s+(?:to|for|in|on)\s+\S", after, re.I)):
        return True
    return bool(re.fullmatch(_BARE_LEAD_IN, before, re.I) and re.fullmatch(r"[\s.!]*", after))


def claim_tokens(text):
    """Lexical support for simple paraphrases, not a semantic/playback gate."""
    text = mask_quotes(text)
    if "?" in text or re.match(r"\s*(is|was|were|has|have|did|can|could|would)\b", text, re.I):
        return None
    predicates = [m for m in re.finditer(_PREDICATE, text, re.I) if _claim_position(text, m)]
    if not predicates:
        return None
    previous = 0
    denied = []
    for predicate in predicates:
        denied.append(bool(re.search(_DENIAL, text[previous:predicate.start()], re.I)))
        previous = predicate.end()
    # Only a complete negative object is recognised after the predicate.
    # Additional words may introduce an affirmative idiom or another clause;
    # ambiguous trailing status words likewise remain claim candidates.
    if len(predicates) == 1 and re.fullmatch(r"\s+(?:none|nothing)\s*[.!]?\s*", text[previous:], re.I):
        denied[0] = True
    if all(denied):
        return None
    ignored = {"i", "ve", "we", "you", "your", "the", "a", "an", "is", "was", "were", "has", "have", "been", "now", "successfully"}
    words = ["cancelled" if x == "canceled" else x for x in re.findall(r"[a-z0-9]+", text.lower()) if x not in ignored]
    actions = [x for x in words if x in _ACTIONS]
    if len(actions) != 1:
        return tuple(words)  # multiple predicates must not be reordered into support
    return (actions[0], tuple(x for x in words if x != actions[0]))


_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
             "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60,
             "seventieth": 70, "eightieth": 80, "ninetieth": 90}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | set(_ORDINALS)
_MONTHS = {name: name[:3] for name in ("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december")}
_MONTHS.update({"jan": "jan", "feb": "feb", "mar": "mar", "apr": "apr", "jun": "jun", "jul": "jul", "aug": "aug",
                "sep": "sep", "sept": "sep", "oct": "oct", "nov": "nov", "dec": "dec"})
_INTERJECTIONS = {"okay", "ok", "right", "so", "well", "alright", "yes", "yeah", "ah", "oh", "um", "uh", "hmm",
                  "honestly", "actually", "just", "now", "then", "also", "too", "still", "yet"}
# Words that carry no reference: pronouns, articles, auxiliaries, prepositions, contraction tails, units.
_STOPWORDS = {"i", "ve", "s", "re", "ll", "d", "m", "t", "we", "us", "you", "your", "he", "she", "her", "him", "his",
              "they", "them", "me", "my", "our", "the", "a", "an", "is", "are", "was", "were", "has", "have", "had",
              "been", "be", "it", "its", "that", "this", "there", "here", "on", "in", "at", "for", "to", "of", "by",
              "with", "from", "and", "or", "as", "out", "up", "off", "no", "any", "some", "all", "pounds", "pound",
              "quid"} | _INTERJECTIONS


def _read_number(tokens, i):
    """A spelled number at tokens[i], cardinal or ordinal, 0 to 999: (value, next index) or None."""
    n, j, total, hundreds = len(tokens), i, 0, False
    word = tokens[i]
    if i + 1 < n and tokens[i + 1] == "hundred" and (word == "a" or 1 <= _UNITS.get(word, 0) <= 9):
        total, j, hundreds = (100 if word == "a" else _UNITS[word] * 100), i + 2, True
        if j + 1 < n and tokens[j] == "and" and tokens[j + 1] in _NUMBER_WORDS:
            j += 1
        elif j >= n or tokens[j] not in _NUMBER_WORDS:
            return total, j
    word = tokens[j] if j < n else ""
    if word in _TENS:
        total += _TENS[word]
        j += 1
        follower = tokens[j] if j < n else ""
        if 1 <= _UNITS.get(follower, 0) <= 9:
            return total + _UNITS[follower], j + 1
        if 1 <= _ORDINALS.get(follower, 0) <= 9:
            return total + _ORDINALS[follower], j + 1
        return total, j
    if word in _UNITS:
        return total + _UNITS[word], j + 1
    if word in _ORDINALS:
        return total + _ORDINALS[word], j + 1
    return (total, j) if hundreds else None


def _normalise_words(text):
    """Lower-case content words with the spelling of numbers normalised: number and
    ordinal words become digits ("thirteenth" -> "13", "a hundred and forty" -> "140",
    "forty-five" -> "45"), digit ordinals lose their suffix ("13th" -> "13"), month
    names shorten ("October" -> "oct"), currency marks and thousands commas go
    ("£1,200" -> "1200"). Pronouns, articles, prepositions, contraction tails and
    interjections are dropped. Numbers are bounded to 0-999; a digit is never dropped."""
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text.lower().replace("£", " "))
    tokens = re.findall(r"\d+(?:\.\d+)?[a-z]*|[a-z]+", text)
    words, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token[0].isdigit():
            suffixed = re.fullmatch(r"(\d+(?:\.\d+)?)(?:st|nd|rd|th)", token)
            words.append(suffixed.group(1) if suffixed else token)
            i += 1
            continue
        number = _read_number(tokens, i)
        if number:
            words.append(str(number[0]))
            i = number[1]
            continue
        token = "cancelled" if token == "canceled" else _MONTHS.get(token, token)
        if token not in _STOPWORDS:
            words.append(token)
        i += 1
    return words


def _ordered_subsequence(wanted, words):
    position = iter(words)
    return all(word in position for word in wanted)


def _denied_at(text, match):
    """The denial grammar, for one completion word inside a backend sentence."""
    return bool(re.search(_DENIAL, text[:match.start()], re.I)
                or re.fullmatch(r"\s+(?:none|nothing)\s*[.!]?\s*", text[match.end():], re.I))


# Wording that makes a backend completion word a promise, a condition or a request rather than a
# record of something done: "will be sent once you confirm", "ready to be sent", "waiting to send".
_NOT_YET = (r"\b(?:will|would|shall|should|could|can|may|might|must|going|about|ready|once|when|whenever|if|"
            r"until|unless|before|after|pending|awaiting|waiting|whether|want|wants|need|needs)\b"
            r"|['’](?:ll|d)\b|\bto\s+be\b")
# The same, after the word: "is cancelled only once you confirm", "it's sent when you tap Confirm".
_CONDITION_AFTER = r"\b(?:only\s+)?(?:once|if|unless|until|when|whenever|after|before|as\s+soon\s+as)\s+(?:you|we|i)\b"
_CLAUSE_BREAK = r"\b(?:but|however|and)\b|[,;—–]"


def _supported_by_server(session, ctx, clause, candidate):
    """The clause's normalised words appear, in order, inside one clause of a recent
    backend sentence that uses the SAME completion word as a plain record: never a
    question, never denied, never wrapped in future, condition or request wording
    on either side of the word. Number and
    ordinal spelling, contractions, interjections and dropped articles are
    forgiven; word order and the completion word's own form are not (a backend
    "ready to send" never supports a spoken "I've sent it"). A clause that names
    nothing beyond the completion word ("I've sent it.") is never supported this
    way, and neither is a multi-word candidate: both keep the exact-words rule."""
    from app.concierge_routes import _spoken_texts
    if len(candidate) != 2 or not isinstance(candidate[1], tuple) or candidate[0] not in _ACTIONS:
        return False
    predicate = candidate[0]
    wanted = _normalise_words(clause)
    if not [word for word in wanted if word != predicate]:
        return False
    pattern = r"\bcancell?ed\b" if predicate == "cancelled" else rf"\b{predicate}\b"
    for source in list(session.speech_context) + _spoken_texts(ctx):
        for sentence in re.split(r"(?<=[.!?])\s+", mask_quotes(source)):
            if "?" in sentence:
                continue   # a question records nothing (claim_tokens skips them the same way)
            hits = list(re.finditer(pattern, sentence, re.I))
            if not hits or any(_denied_at(sentence, hit) or re.search(_NOT_YET, sentence[:hit.start()], re.I)
                               or re.search(_CONDITION_AFTER, sentence[hit.end():], re.I) for hit in hits):
                continue
            for part in re.split(_CLAUSE_BREAK, sentence, flags=re.I):
                if re.search(pattern, part, re.I) and _ordered_subsequence(wanted, _normalise_words(part)):
                    return True
    return False


def output_slice(parts, start, end):
    """Retain source identity and absolute character positions when buffering."""
    result = []
    for part in parts:
        left = max(start, part["char_start"])
        right = min(end, part["char_start"] + len(part["delta"]))
        if left < right:
            result.append({**part, "char_start": left,
                "delta": part["delta"][left-part["char_start"]:right-part["char_start"]]})
    return result


async def monitor_output(session, text, ctx, *, fragments=(), char_start=0):
    from app.concierge_routes import _server_said
    text = mask_quotes(text)
    separators = list(re.finditer(r"\b(?:but|however|and)\b|[,;]", text, re.I))
    if separators:
        offset = 0
        for boundary in separators:
            await monitor_output(session, text[offset:boundary.start()], ctx,
                                 fragments=fragments, char_start=char_start+offset)
            offset = boundary.end()
        await monitor_output(session, text[offset:], ctx, fragments=fragments, char_start=char_start+offset)
        return
    candidate = claim_tokens(text)
    if not candidate or _server_said(ctx, text):
        return
    support = [claim_tokens(clause) for context in session.speech_context
               for clause in re.split(r"(?<=[.!?])\s*", context)]
    if candidate in support or _supported_by_server(session, ctx, text, candidate):
        return
    occurred_at = datetime.now(timezone.utc).isoformat()
    sequence = session.publish("guard", text="Speech is unverified. Check the business result in chat.", reason="unsupported_success_claim")
    session.counters["output_guard"] = session.counters.get("output_guard", 0) + 1
    turns = [job["turn"] for job in session.delegations.values() if job.get("turn")]
    context_size = len(session.speech_context)
    try:
        await session.send("session.instructions.append", "Stop speaking. Do not claim business success without a current backend receipt.")
    finally:
        # Diagnostic work follows protective publication/send, including failures.
        def fingerprint(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        sources = output_slice(fragments, char_start, char_start+len(text))
        diagnostic(session, "output_guard", count=False, reason="unsupported_success_claim", observed_at=occurred_at,
            published_seq=sequence, clause_start_char=char_start, clause_end_char=char_start+len(text),
            transcript_ids=[p["event_id"] for p in sources[:32]], transcript_ids_omitted=max(0, len(sources)-32),
            fragment_bounds_ms=[[p["start_ms"], p["end_ms"]] for p in sources[:32]],
            context_turn_ids=turns[-16:], context_turn_ids_omitted=max(0, len(turns)-16),
            turn_linkage="session_context_only", claim_hash=fingerprint(candidate), clause_hash=fingerprint(text),
            support_candidate_hashes=[fingerprint(value) for value in support[:32]],
            support_candidates_omitted=max(0, len(support)-32), speech_context_size=context_size)


async def handle_event(session, event, ctx):
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if kind in {"session.input_audio.append", "session.output_audio.delta"}:
        key = "input_audio_event" if kind == "session.input_audio.append" else "output_audio_event"
        session.counters[key] = session.counters.get(key, 0) + 1
        return  # reflected media never invokes database/business work
    if kind == "session.closed":
        if not session.close_trigger:
            event_id = event.get("event_id")
            session.close_trigger = {"trigger_type": "provider_event",
                "event_id": event_id if isinstance(event_id, str) and len(event_id) <= 128 else None}
        reserve_terminal(session, "provider_closed", "provider_event")
        usage = event.get("usage") or {}
        seconds = usage.get("seconds")
        if isinstance(seconds, (int, float)) and 0 <= seconds <= 7200:
            session.usage_seconds = seconds
        session.finalized = True
        session.ended.set()
        return
    if session.closed:
        return
    if kind == "session.started":
        if session.allowance_enabled and not session.allowance_connected:
            try:
                from app import db
                result = await asyncio.to_thread(db.heartbeat_concierge_live_session,
                    session.voice_session, session.tenant, session.user, session.allowance_limit_seconds)
            except Exception as exc:
                reserve_terminal(session, "allowance_unavailable", "allowance_start", exc)
                await finish(session)
                await record_error(session, exc, "allowance_start", True)
                return
            if not result.get("ok"):
                reserve_terminal(session, result.get("reason") or "allowance_start_failed", "allowance_start")
                await finish(session)
                return
            session.allowance_connected = True
            session.allowance_heartbeat_at = time.monotonic()
        return
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or len(event_id) > 128 or event_id in session.seen:
        return
    if len(session.seen) >= 4096:
        raise ValueError("Event capacity reached")
    session.seen.add(event_id)
    session.counters["events_seen"] = len(session.seen)   # how close the session got to the 4,096 cap above
    if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
        start, end, text = event.get("start_ms"), event.get("end_ms"), event.get("delta")
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= LIFETIME * 1000
                or not isinstance(text, str) or len(text) > 2000):
            raise ValueError("Invalid transcript event")
        item = {"event_id": event_id, "start_ms": start, "end_ms": end, "delta": text}
        archive_fragment(session, kind, item)
        if kind == "session.input_transcript.delta":
            if "input_fragment" not in session.counters:
                # Once per session: which fields the provider sends on an input fragment. Field
                # names only (a confidence or speaker field would show up here), never a value.
                diagnostic(session, "input_event_shape",
                           fields=sorted(k for k in event if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", k))[:32])
            session.counters["input_fragment"] = session.counters.get("input_fragment", 0) + 1
            session.last_input = time.monotonic()
            item["received"] = session.last_input
            if end > session.consumed_ms:
                working = [job for job in session.delegations.values() if job.get("turn") and job["state"] == "working"]
                if working:
                    # Counts and milliseconds only: what was waiting when this fragment landed.
                    memo = {}   # one join per event, shared by every working turn (they share a boundary)
                    heard = {**input_shape(pending_input(session, session.consumed_ms, item, memo)),
                             "since_question_ms": end - session.consumed_ms}
                    now = time.monotonic()
                    for job in working:
                        if job.get("cancel_requested"):
                            # Asked once, and a cue stays in the pool: the repeats are counted, not traced
                            # (a committed turn would otherwise write a row per later fragment).
                            if end > turn_boundary(session, job):
                                session.counters["input_cancel_repeat"] = session.counters.get("input_cancel_repeat", 0) + 1
                            continue
                        age = round((now - job.get("started", now)) * 1000)
                        why = interrupts(session, job, item, memo)
                        request = None
                        if not why and end > turn_boundary(session, job) and settings.gptlive_interrupt_rule == "cue_words":
                            # The model raised a request before its words reached us (the transcript
                            # trails the delegation); these are the words it anchored on, so the cancel
                            # that had to wait for them happens now.
                            request = anchored_request(session, job, item)
                        if why or request:
                            # Cooperative C1 boundary protects already-entered effects.
                            cancel_turn(session, job["turn"], "new_input", trigger_type="delegation" if request else "transcript",
                                event_id=event_id, start_ms=start, end_ms=end, cue=(why == "cue"),
                                bracketed_nonverbal=bool(re.match(r"^\s*\[[^\]\r\n]+\]?\s*$", text)),
                                turn_age_ms=age, **heard, **({"offset_ms": request["offset"], "deferred": True} if request else {}))
                            job["cancel_requested"] = True
                        elif end > turn_boundary(session, job):
                            # Heard, judged, and not a reason to stop the work: held, content-free.
                            session.counters["input_held"] = session.counters.get("input_held", 0) + 1
                            if session.counters["input_held"] <= INPUT_HELD_ROWS:
                                diagnostic(session, "input_held", count=False, turn_id=job["turn"], event_id=event_id,
                                           start_ms=start, end_ms=end, turn_age_ms=age, **heard)
            session.fragments.append(item)
            session.fragments = session.fragments[-256:]
        else:
            item["char_start"] = session.output_chars
            session.output_chars += len(text)
            parts = output_slice(session.output + [item], max(0, session.output_chars-1600), session.output_chars)
            said = "".join(p["delta"] for p in parts)
            base = parts[0]["char_start"] if parts else session.output_chars
            start = 0
            for boundary in re.finditer(r"[.!?]", mask_quotes(said)):
                await monitor_output(session, said[start:boundary.end()], ctx,
                                     fragments=parts, char_start=base+start)
                start = boundary.end()
            # Punctuation bounds a claim candidate, not a provider turn-final.
            session.output = output_slice(parts, base+start, session.output_chars)
    elif kind == "session.delegation.created":
        data, offset = event.get("delegation") or {}, event.get("offset_ms")
        delegation_id = data.get("id")
        if (data.get("target") != "client" or not isinstance(delegation_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", delegation_id)
                or type(offset) is not int or not 0 <= offset <= LIFETIME * 1000):
            raise ValueError("Invalid delegation")
        if delegation_id in session.delegations:
            return
        if len(session.delegations) >= 100:
            raise ValueError("Delegation capacity reached")
        # The model itself judging that the owner has raised a new request past everything a working
        # turn was asked is what stands that turn down (cue_words), not any heard word. It must be
        # anchored on speech the turn never consumed. A delegation with no such speech is a retry: it is
        # marked, its empty window is not answered with "please repeat", and, being a duplicate, it is not
        # a second request either, so it never makes another queued job ambiguous. If its words are only
        # late, the cancel happens when they arrive (see the input branch above). Under any_words nothing
        # here applies and a re-delegation is answered as it always was.
        running, anchored_for = [], []
        if settings.gptlive_interrupt_rule == "cue_words":
            running = [j for j in session.delegations.values() if j.get("turn") and j["state"] == "working"]
            anchored_for = [j for j in running if anchored_request(session, j, candidates=({"state": "queued", "offset": offset},))]
        retry = bool(running) and not anchored_for
        pending = [] if retry else [j for j in session.delegations.values() if j["state"] == "queued" and not j.get("retry")]
        for job in pending:
            job["ambiguous"] = True
        session.delegations[delegation_id] = {"offset": offset, "received": time.monotonic(),
                                              "state": "queued", "ambiguous": bool(pending), "retry": retry,
                                              **({"retry_floor": max(turn_boundary(session, j) for j in running)} if retry else {})}
        now = time.monotonic()
        for earlier in anchored_for:
            if earlier.get("cancel_requested"):
                session.counters["input_cancel_repeat"] = session.counters.get("input_cancel_repeat", 0) + 1
            else:
                cancel_turn(session, earlier["turn"], "new_input", trigger_type="delegation", cue=False,
                            event_id=event_id, offset_ms=offset, deferred=False,
                            turn_age_ms=round((now - earlier.get("started", now)) * 1000))
                earlier["cancel_requested"] = True
        # seen_after_ms is when we learned of it on this server's clock since the session began;
        # against delegation_offset_ms (the words' own audio clock) it shows provider-side lag (14 Oct, N5).
        # working_turns: business turns already running when the model raised this one (whether the
        # model delegates a correction while an earlier request is still outstanding).
        await stage(session, "delegation_seen", delegation_offset_ms=offset,
                    seen_after_ms=round((time.monotonic() - session.created) * 1000),
                    first_input_ms=min((p["start_ms"] for p in session.fragments), default=-1),
                    working_turns=sum(1 for j in session.delegations.values() if j["state"] == "working"))
        task = asyncio.create_task(delegate(session, delegation_id, ctx))
        session.jobs.add(task)
        task.add_done_callback(session.jobs.discard)
    elif kind == "session.usage.updated":
        value = (event.get("usage") or {}).get("seconds")
        if isinstance(value, (int, float)) and 0 <= value <= 7200:
            session.usage_seconds = max(session.usage_seconds, value)
    elif kind.endswith(".appended"):
        command = session.commands.get(event.get("client_event_id"))
        if command:
            command["state"] = "accepted_context_not_playback"
    elif kind == "error":
        command = session.commands.get((event.get("error") or {}).get("client_event_id"))
        if command:
            command["state"] = "unconfirmed"
        # Command failure never replays a backend operation.
        session.publish("status", text="A voice update was not confirmed. Check chat for the result.")


async def task_updates(session, ctx):
    """Verified native context, never a C3 delivery claim or playback ACK.

    Visual/push delivery remains authoritative. A native paraphrase may follow;
    an append attempt is not retried and cannot settle a delivery receipt.
    """
    if session.lock.locked():
        return
    generation = session.generation
    await authorized(session, ctx)
    from app import db
    from app.concierge_routes import session_task_snapshot, _remember_speech
    # A settled visible card must not latch this conversation silent forever —
    # and its outcome must reach the voice once (live 2026-10-14: "Not yet, use
    # the card" after three confirmed taps). Tracked here, not at the tap: the
    # tap route runs in a web request with no handle on the native session.
    receipt = None
    receipt_kind = None
    if session.approval.get("proposal"):
        row = await asyncio.to_thread(db.get_concierge_proposal, session.approval["proposal"], ctx.tenant_id, client=ctx.client)
        if row and row.get("status") in {"pending", "executing", "unknown"}:
            return
        if row:
            from app import proposal_outcomes
            receipt = card_receipt(proposal_outcomes.public(row))
            receipt_kind = "proposal"
    if session.approval.get("draft"):
        row = await asyncio.to_thread(db.get_email_log_entry, session.approval["draft"], ctx.tenant_id, client=ctx.client)
        if row and row.get("status") in {"pending", "sending"}:
            return
        if row and not receipt:
            receipt = draft_receipt(row)
            receipt_kind = "draft"
    session.approval.clear()
    if receipt:
        await send_receipt(session, ctx, receipt, receipt_kind)
    result = await asyncio.to_thread(session_task_snapshot, session.voice_session, ctx)
    ready = []
    for row in result.get("tasks", []):
        event = row.get("event_id")
        key = (session.voice_session, row.get("id"), row.get("revision"), event)
        if not event or key in session.notices or not row.get("speakable"):
            continue
        await authorized(session, ctx)
        if session.closed:
            return
        def current_result():
            current = concierge_delegation.scoped_task(ctx, row["id"])
            return concierge_delegation.public(current) if current else None
        fresh = await asyncio.to_thread(current_result)
        if not fresh or session.closed or session.generation != generation:
            continue
        if (fresh.get("event_id") != event or fresh.get("revision") != row.get("revision")
                or fresh.get("voice_session") != session.voice_session or not fresh.get("speakable")):
            continue
        outcome = str(fresh.get("outcome") or "")[:1200]
        if outcome:
            ready.append((key, row, fresh, outcome))
    if not ready:
        return
    # Everything that finished since the last pass is one spoken line, the
    # ones that need the owner first. Each result still lands in chat in full.
    ready.sort(key=lambda item: task_priority.rank(item[2]))
    spoken = _short_outcome(ready[0][3]) if len(ready) == 1 else _batch_line(ready)
    for key, row, _fresh, outcome in ready:
        session.notices.add(key)  # reserve once before uncertain send
        session.publish("task", text=outcome, task_id=row["id"], revision=row["revision"],
                        delivery="native_playback_unconfirmed")
    _remember_speech(ctx, spoken)
    try:
        await session.send("session.commentary.append", spoken)
    except ConnectionClosed:
        raise
    except Exception as exc:
        raise TaskNoticeFailure() from exc


def _short_outcome(text: str, limit: int = 240) -> str:
    """One short spoken sentence; the full verified result stays in chat."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 40:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "."


def card_receipt(public):
    """One spoken line for a settled card, from the durable receipt only."""
    state = public.get("state")
    if state == "completed":
        return "The card was confirmed and it went through: " + _short_outcome(public.get("summary") or "Done.")
    if state == "cancelled":
        return "The card was cancelled; nothing changed."
    if state == "superseded":
        return "That card was replaced by a newer one; nothing from it happened."
    if state == "expired":
        return "That card expired unanswered; nothing happened. It is in the waiting list."
    if state == "failed":
        summary = _short_outcome(public.get("summary") or "")
        return "The card was confirmed but the change did not go through" + (": " + summary if summary else ".")
    return None   # unknown/executing/missing: say nothing rather than guess


def draft_receipt(row):
    """One spoken line for a settled draft, from the email log status only."""
    status = str(row.get("status") or "")
    if status == "sent":
        return "The email was approved and has gone."
    if status == "skipped":
        return "The email was left unsent; nothing went out."
    return None


async def send_receipt(session, ctx, text, kind):
    """Tell the voice a tracked card or draft settled: once, content-free in the trace."""
    from app.concierge_routes import _remember_speech
    # The next delegated turn must know it too (the saved rows already do).
    if session.history and session.history[-1].get("role") == "assistant":
        note = "\n\n[Card outcome: " + text + "]"
        previous = session.history[-1]["content"]
        session.history[-1] = {**session.history[-1], "content": previous[:max(0, 3000 - len(note))] + note}
    _remember_speech(ctx, text)
    try:
        await session.send("session.commentary.append", text)
    except ConnectionClosed:
        raise
    except Exception as exc:
        raise TaskNoticeFailure() from exc
    # Traced only once the append went out: a failed send must not leave a trace
    # that says the voice was told.
    await stage(session, "card_outcome_sent", kind=kind)


def _batch_line(ready) -> str:
    titles = [str(fresh.get("title") or "A request").strip().rstrip(".") for _k, _r, fresh, _o in ready]
    shown = "; ".join(titles[:3]) + (f"; and {len(titles) - 3} more" if len(titles) > 3 else "")
    return f"{len(ready)} requests are ready: {shown}. The full results are in your task list."


async def maintain(session, ctx):
    """Independent maintenance failures cannot masquerade as lost authority."""
    while not session.closed:
        if session.allowance_enabled:
            if not session.allowance_connected:
                if time.monotonic() - session.created > 30:
                    reserve_terminal(session, "allowance_start_timeout", "allowance_start")
                    await finish(session)
                    return
            elif time.monotonic() - session.allowance_heartbeat_at >= 5:
                try:
                    from app import db
                    result = await asyncio.to_thread(db.heartbeat_concierge_live_session,
                        session.voice_session, session.tenant, session.user, session.allowance_limit_seconds)
                except Exception as exc:
                    if time.monotonic() - session.allowance_heartbeat_at <= AUTHORITY_GRACE:
                        # A beat that could not run is not a lost allowance (the lease is 90 s and a POST is
                        # never resent): carry on while the last good beat is recent. Counted
                        # (diagnostic_allowance_grace), at most three rows traced, and tried again two
                        # seconds on. A verified ok:false below is still terminal at once.
                        await record_error(session, exc, "allowance_grace")
                        await asyncio.sleep(MAINTENANCE_INTERVAL)
                        continue
                    reserve_terminal(session, "allowance_unavailable", "allowance_heartbeat", exc)
                    await finish(session)
                    await record_error(session, exc, "allowance_heartbeat", True)
                    return
                if not result.get("ok"):
                    reserve_terminal(session, result.get("reason") or "allowance_ended", "allowance_heartbeat")
                    await finish(session)
                    return
                session.allowance_heartbeat_at = time.monotonic()
                used = result.get("used_seconds")
                if (session.allowance_limit_seconds and not session.allowance_warned and isinstance(used, (int, float))
                        and session.allowance_limit_seconds - used <= ALLOWANCE_WARN_SECONDS):
                    # Once per conversation: the cut mid-sentence was the complaint (14 Oct, N4). The
                    # screen shows it and the voice is asked to say it; the cut itself is unchanged.
                    session.allowance_warned = True
                    session.publish("status", text="About a minute of live voice left today.", reason="allowance_warning")
                    with contextlib.suppress(Exception):
                        await session.send("session.commentary.append", "Live voice allowance: about one minute left for today. Mention it in one short sentence, then carry on.")
        try:
            await authorized(session, ctx)
        except AuthorizationFailure as exc:
            if unverifiable(session, exc):
                # Not a permission change: counted (diagnostic_authority_grace), at most three rows
                # traced, this tick's refresh skipped; the next check is two seconds away.
                await record_error(session, exc, "authority_grace")
                await asyncio.sleep(MAINTENANCE_INTERVAL)
                continue
            reason = authority_reason(session, exc)
            if reason:
                reserve_terminal(session, reason, "authorization", exc)
                await finish(session)
                await record_error(session, exc, "authorization", True)
            return
        for phase, operation in (("transcript_flush", flush_transcript), ("task_refresh", task_updates)):
            try:
                if phase == "transcript_flush":
                    await operation(session)
                else:
                    await operation(session, ctx)
            except AuthorizationFailure as exc:
                if unverifiable(session, exc):
                    await record_error(session, exc, "authority_grace")
                    continue
                reason = authority_reason(session, exc)
                if reason:
                    reserve_terminal(session, reason, "authorization", exc)
                    await finish(session)
                    await record_error(session, exc, "authorization", True)
                return
            except ConnectionClosed as exc:
                reserve_terminal(session, "sideband_error", phase, exc)
                await finish(session)
                await record_error(session, exc, phase, True)
                return
            except Exception as exc:
                if isinstance(exc, HTTPException) and exc.status_code in {401, 403}:
                    reserve_terminal(session, "permission_denied", phase, exc)
                    await finish(session)
                    await record_error(session, exc, phase, True)
                    return
                if session.closed:
                    return
                actual_phase = "task_notice_send" if isinstance(exc, TaskNoticeFailure) else phase
                key = "task_update_error" if phase == "task_refresh" else "transcript_flush_error"
                session.counters[key] = min(65535, session.counters.get(key, 0) + 1)
                if session.counters[key] == 1:
                    session.publish("status", text="Task or history updates are temporarily unavailable; check chat. Work will not be replayed.", reason=key)
                await record_error(session, exc, actual_phase)
            if session.closed:
                return
        await asyncio.sleep(MAINTENANCE_INTERVAL)


async def read_sideband(session, ctx):
    session.maintenance = asyncio.create_task(maintain(session, ctx))
    phase = "receive"
    try:
        while not session.ended.is_set():
            phase = "receive"
            if session.closed:
                raw = await asyncio.wait_for(session.socket.recv(), 2)
                phase = "decode"
                await handle_event(session, json.loads(raw), ctx)
                continue
            if time.monotonic() - session.created > LIFETIME:
                reserve_terminal(session, "lifetime", "lifetime")
                break
            if time.monotonic() - session.last_poll > 30:
                reserve_terminal(session, "poll_timeout", "poll_timeout")
                break
            for command in session.commands.values():
                if command["state"] == "pending" and time.monotonic() - command["sent"] > 5:
                    command["state"] = "unconfirmed"
                    session.publish("status", text="A voice update was not confirmed. Check chat; it will not be replayed.")
            try:
                raw = await asyncio.wait_for(session.socket.recv(), 5)
            except asyncio.TimeoutError:
                continue
            if len(raw) > 262144:
                reserve_terminal(session, "event_limit", "event_size")
                break
            phase = "decode"
            event = json.loads(raw)
            phase = "dispatch"
            await handle_event(session, event, ctx)
            await asyncio.sleep(0)
    except Exception as exc:
        first = reserve_terminal(session, "sideband_error", phase, exc)
        if first:
            session.counters["sideband_error"] = session.counters.get("sideband_error", 0) + 1
            session.publish("status", text="GPT-Live disconnected. Business results remain in chat.")
        await finish(session)
        await record_error(session, exc, phase, first)
    finally:
        await finish(session)


@router.get("/events/{session_id}")
async def events(session_id: str, after: int = 0, ctx: TenantCtx = Depends(require_concierge)):
    session = own(session_id, ctx)
    # The watchdog measures the browser's polling, not the database: stamp before the check.
    session.last_poll = time.monotonic()
    try:
        await authorized(session, ctx)
    except AuthorizationFailure as exc:
        if unverifiable(session, exc):
            await record_error(session, exc, "authority_grace")
        else:
            reason = authority_reason(session, exc)
            if reason:
                reserve_terminal(session, reason, "event_poll", exc)
            await finish(session)
            raise
    return {"events": [e for e in session.events if e["seq"] > after], "sequence": session.sequence,
            "usage_seconds": session.usage_seconds, "delivery": "native_playback_unconfirmed"}


@router.post("/control", dependencies=[Depends(require_csrf_header)])
async def control(body: Control, ctx: TenantCtx = Depends(require_concierge)):
    control_id = body.control_id or uuid.uuid4().hex
    client_diagnostics = {"control_id": control_id, "silence_reasons": body.silence_reasons,
        "playback_events": None if body.playback_events is None else [event.model_dump(mode="json") for event in body.playback_events],
        "playback_events_omitted": body.playback_events_omitted,
        "capture": body.capture, "levels": body.levels, "talk": body.talk, "line": body.line}
    session = _sessions.get(body.session)
    if not session:
        session = next((s for s in _sessions.values() if s.voice_session == body.session
                        and (s.tenant, s.user) == (ctx.tenant_id, ctx.user_id)), None)
    if not session and body.operation == "close":
        try:
            value = str(uuid.UUID(body.session))
        except ValueError:
            raise HTTPException(404, "Conversation unavailable.")
        now = time.monotonic()
        for key, created in list(_cancelled.items()):
            if now - created > 60:
                _cancelled.pop(key, None)
        if len(_cancelled) >= 256:
            raise HTTPException(503, "Voice cleanup capacity is busy.")
        marker = (ctx.tenant_id, ctx.user_id, value)
        first_report = marker not in _cancelled
        _cancelled[marker] = now  # Reserve cancellation before any diagnostic await.
        if first_report:
            await safe_trace(ctx.tenant_id, "gptlive_client_close", user_id=ctx.user_id,
                session_id=trace.session_ref(value), outcome=body.close_reason,
                detail={"reason": body.close_reason, "stage": body.startup_stage,
                        **client_diagnostics, **idle_detail(body),
                        "reached_ready": body.reached_ready, "playback_state": body.playback_state,
                        "peer_state": body.peer_state, "channel_state": body.channel_state})
        # A stored cancellation marker alone does not mean provider creation
        # has finished. Only allow a fresh explicit startup when this owner's
        # create and live session have both settled (single process ownership).
        retry_allowed = ((ctx.tenant_id, ctx.user_id) not in _creating
                         and not any((s.tenant, s.user) == (ctx.tenant_id, ctx.user_id)
                                     and not s.closed for s in _sessions.values()))
        return {"ok": True, "final_usage_confirmed": False, "retry_allowed": retry_allowed}
    if not session or (session.tenant, session.user) != (ctx.tenant_id, ctx.user_id):
        raise HTTPException(404, "Conversation unavailable.")
    if body.operation == "close":
        report = None
        if not session.client_close_reported:
            session.client_close_reported = True
            session.reached_ready = body.reached_ready
            report = {"reason": body.close_reason, "stage": body.startup_stage,
                      "application_session_id": session.id, **client_diagnostics, **idle_detail(body),
                      "reached_ready": body.reached_ready, "playback_state": body.playback_state,
                      "peer_state": body.peer_state, "channel_state": body.channel_state,
                      "received_at": datetime.now(timezone.utc).isoformat()}
        # Authoritative cancellation/cleanup precedes best-effort diagnostics.
        reserve_terminal(session, "local_close", "client_control", client_reason=body.close_reason, **idle_detail(body))
        if not session.closed:
            session.close_trigger = {"trigger_type": "control", "control_id": control_id}
        await finish(session)
        if report:
            await safe_trace(ctx.tenant_id, "gptlive_client_close", user_id=ctx.user_id,
                session_id=trace.session_ref(session.voice_session), outcome=body.close_reason, detail=report)
        retry_allowed = ((ctx.tenant_id, ctx.user_id) not in _creating
                         and not any((s.tenant, s.user) == (ctx.tenant_id, ctx.user_id)
                                     and not s.closed for s in _sessions.values()))
        return {"ok": True, "final_usage_confirmed": session.finalized, "retry_allowed": retry_allowed}
    await authorized(session, ctx)
    if body.operation == "debrief":
        reason = await debrief_refusal(ctx)
        if reason:
            raise HTTPException(403, reason)
        if len(session.delegations) >= 100:
            raise HTTPException(429, "Too many requests in this conversation. Start a new one.")
        task = asyncio.create_task(run_debrief(session, ctx, body.step))
        session.jobs.add(task)
        task.add_done_callback(session.jobs.discard)
        return {"ok": True, "status": "debrief_requested"}
    outcomes = []
    if body.operation == "discard":
        session.generation += 1
        session.carry = {}
        for job in session.delegations.values():
            if job.get("turn") and job["turn"] == body.turn_id:
                value = cancel_turn(session, job["turn"], "discard", trigger_type="control", control_id=control_id)
                outcomes.append({"turn_id": job["turn"], **value})
        session.consumed_ms = max([p["end_ms"] for p in session.fragments], default=session.consumed_ms)
    await session.send("session.instructions.append", "Stop speaking now. Wait for the user's next instruction.")
    return {"ok": True, "status": "audio_interrupted" if body.operation == "interrupt" else "discard_checked",
            "turns": outcomes}
