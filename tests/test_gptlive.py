import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from app import gptlive as live, voice_turn_state
from tests.test_voice_live import client
from tests.test_voice_discard import client as voice_client
from tests.test_proposal_recovery import Store
from proposal_receipts import execution_result


class Socket:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.close":
            await self.queue.put(json.dumps({"type": "session.closed", "usage": {"seconds": 12}}))

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def unit(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "list_concierge_messages", lambda *a, **k: [])
    monkeypatch.setattr(db, "save_native_transcript", lambda *a, **k: None)
    monkeypatch.setattr(db, "concierge_live_rollout", lambda: False)
    live._sessions.clear(); live._creating.clear(); live._cancelled.clear()
    voice_turn_state._states.clear()
    original_scope = live.scope
    monkeypatch.setattr(live, "scope", lambda ctx: "permissions")
    async def auth(ctx):
        return ctx
    monkeypatch.setattr(live, "require_concierge", auth)
    monkeypatch.setattr(live, "check_concierge_access", lambda ctx: ctx)
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(live.settings, "gptlive_enabled", True)
    monkeypatch.setattr(live.settings, "voice_live_enabled", True)
    monkeypatch.setattr(live.settings, "openai_api_key", "secret-do-not-disclose")
    monkeypatch.setattr(live.settings, "gptlive_settle_ms", 0)
    monkeypatch.setattr(live, "CONTEXT_WAIT", .15)
    monkeypatch.setattr(live, "TRAILING_WORDS_HEADROOM", 0)     # so the whole wait stays .15 s; the fake-clock tests restore both
    yield SimpleNamespace(tenant_id="t1", user_id="u1", client=None, viewing_as=None, scope=original_scope)
    if live._diagnostic_jobs:
        await asyncio.gather(*list(live._diagnostic_jobs))
    live._sessions.clear(); live._creating.clear(); live._cancelled.clear()
    voice_turn_state._states.clear()


@pytest.mark.asyncio
async def test_native_session_reserves_live_allowance_and_settles_on_close(unit, monkeypatch):
    from app import db
    voice = uuid.uuid4(); sock = Socket(); reservations = []; closes = []
    monkeypatch.setattr(db, "concierge_live_rollout", lambda: True)
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"plan": "pro"})
    monkeypatch.setattr(db, "concierge_live_tier_limits", lambda: {"pro": 10})
    monkeypatch.setattr(db, "concierge_live_status", lambda *a, **k: {"used_seconds": 20, "remaining_seconds": 580})
    monkeypatch.setattr(db, "reserve_concierge_live_session", lambda *a, **k: (reservations.append(a) or {"ok": True}))
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: (closes.append(a) or {"ok": True}))
    monkeypatch.setattr(live, "create_provider", lambda sdp: asyncio.sleep(0, result=("live_metered", "v=answer")))
    monkeypatch.setattr(live, "attach", lambda provider: asyncio.sleep(0, result=sock))
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    result = await live.create(live.Create(sdp="v=offer", voice_session=voice), unit)
    assert result["allowance"] == {"enabled": True, "unlimited": False, "remaining_seconds": 580, "limit_seconds": 600}
    assert reservations and reservations[0][0] == str(voice)
    session = live._sessions[result["session"]]
    assert not session.allowance_connected
    heartbeats = []
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", lambda *a, **k: (heartbeats.append(a) or {"ok": True}))
    await live.handle_event(session, {"type": "session.started"}, unit)
    assert session.allowance_connected
    assert heartbeats == [(str(voice), "t1", "u1", 600)]
    await live.finish(session)
    assert closes == [(str(voice), "t1", "u1")]


@pytest.mark.asyncio
async def test_native_session_denies_creation_when_daily_allowance_is_exhausted(unit, monkeypatch):
    from app import db
    called = []
    monkeypatch.setattr(db, "concierge_live_rollout", lambda: True)
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"plan": "pro"})
    monkeypatch.setattr(db, "concierge_live_tier_limits", lambda: {"pro": 10})
    monkeypatch.setattr(db, "concierge_live_status", lambda *a, **k: {"used_seconds": 600, "remaining_seconds": 0})
    monkeypatch.setattr(db, "reserve_concierge_live_session", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(live, "create_provider", lambda *a, **k: (called.append(True)))
    with pytest.raises(HTTPException) as exc:
        await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert exc.value.status_code == 429 and not called


@pytest.mark.asyncio
async def test_native_server_heartbeat_enforces_limit_without_browser_heartbeat(unit, monkeypatch):
    from app import db
    s = session()
    s.allowance_enabled = True
    s.allowance_connected = True
    s.allowance_limit_seconds = 600
    s.allowance_heartbeat_at = asyncio.get_running_loop().time() - 6
    heartbeats, closes = [], []
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", lambda *a, **k:
                        (heartbeats.append(a) or {"ok": False, "ended": True, "reason": "limit_reached"}))
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k:
                        (closes.append(a) or {"ok": True}))
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    await live.maintain(s, unit)
    assert heartbeats == [(s.voice_session, s.tenant, s.user, 600)]
    assert closes == [(s.voice_session, s.tenant, s.user)]
    assert s.closed and s.close_reason == "limit_reached"


@pytest.mark.asyncio
async def test_native_context_cutoff_is_applied_before_server_session_creation(unit, monkeypatch):
    from app import db
    from datetime import datetime, timezone
    voice = uuid.uuid4(); sock = Socket()
    rows = [
        {"role": "user", "content": "Old question", "created_at": "2026-10-14T11:59:59+00:00"},
        {"role": "assistant", "content": "Old answer", "created_at": "2026-10-14T11:59:59+00:00"},
        {"role": "user", "content": "Current question", "created_at": "2026-10-14T12:00:01+00:00"},
        {"role": "assistant", "content": "Current answer", "created_at": "2026-10-14T12:00:01+00:00"},
    ]
    monkeypatch.setattr(db, "list_concierge_messages", lambda *a, **k: rows)
    monkeypatch.setattr(live, "create_provider", lambda sdp: asyncio.sleep(0, result=("live_context", "v=answer")))
    monkeypatch.setattr(live, "attach", lambda provider: asyncio.sleep(0, result=sock))
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    result = await live.create(live.Create(sdp="v=offer", voice_session=voice,
        context_from=datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)), unit)
    assert result["context"] == [
        {"role": "user", "content": "[Conversation on Wednesday 14 October 2026, from 13:00]\nCurrent question"},
        {"role": "assistant", "content": "[Saved turn on Wednesday 14 October 2026, at 13:00]\nCurrent answer"},
    ]
    restored = live._sessions[result["session"]]
    saved_context = [dict(t) for t in result["context"]]
    calls = []
    async def backend(ctx, question, history, turn, voice):
        from app.concierge_routes import Turn
        from app import concierge
        history = concierge.sanitize_history([Turn(**t).model_dump() for t in history], 16, 4000)
        calls.append((question, history))
        return {"reply": "The available saved conversation was on Wednesday."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(restored, fragment("When did we last speak?"), unit)
    await live.handle_event(restored, delegation(), unit)
    await asyncio.gather(*restored.jobs)
    assert calls == [("When did we last speak?", saved_context)]
    assert "earlier conversations" in live.config()["instructions"]
    await live.finish(restored)


def session():
    return live.Session("owned", "live_provider", "t1", "u1", str(uuid.uuid4()), "permissions", Socket())


def fragment(text="Check the diary.", start=0, end=100, event_id="words"):
    return {"type": "session.input_transcript.delta", "event_id": event_id,
            "delta": text, "start_ms": start, "end_ms": end}


def delegation(event_id="delegation-event", name="d1", offset=100):
    return {"type": "session.delegation.created", "event_id": event_id, "offset_ms": offset,
            "delegation": {"id": name, "type": "delegation", "target": "client"}}


def working_turn(s, turn="working-turn", **extra):
    """A business turn that is working (past admission) for the interrupt-rule tests."""
    s.delegations[turn] = {"turn": turn, "state": "working", **extra}
    return voice_turn_state.begin(voice_turn_state.key(s.tenant, s.user, turn))


@pytest.mark.asyncio
async def test_three_authorized_replies_and_paraphrase_do_not_cross_guard(unit):
    from app.concierge_routes import _remember_speech, _SPOKEN
    _SPOKEN.clear(); s = session()
    lines = ["The email was sent.", "The booking was cancelled.", "The record was updated."]
    for i, text in enumerate(lines):
        _remember_speech(unit, text)
        await s.send("session.commentary.append", text)
        await live.handle_event(s, {**fragment(text, i*100, (i+1)*100, str(i)), "type":"session.output_transcript.delta"}, unit)
    await live.monitor_output(s, "I've sent your email.", unit)
    for text in ['The email was not sent.', 'Was the email sent?', 'The status says "sent".', "The status says 'sent'."]:
        await live.monitor_output(s, text, unit)
    assert not [e for e in s.events if e["kind"] == "guard"]
    await live.monitor_output(s, "The invoice was charged.", unit)
    assert len([e for e in s.events if e["kind"] == "guard"]) == 1
    await s.send("session.commentary.append", "Alice charged Bob.")
    await live.monitor_output(s, "Bob charged Alice.", unit)
    await live.monitor_output(s, "I have not checked, but the invoice was charged.", unit)
    assert len([e for e in s.events if e["kind"] == "guard"]) == 3
    _SPOKEN.clear()


def test_native_prompt_frames_capabilities_and_delegates_account_specifics():
    # Wave 3a: the native voice has a fixed, safe frame for "what can you do" and
    # hands everything specific to the account (lookups, help, tax) to the backend.
    prompt = " ".join(live.PROMPT.split())
    for phrase in ("tap Confirm on the card", "no undo", "cannot be unsent", "VAT or tax",
                   "delegate so the backend answers", "half-remembered name", "awaiting approval",
                   "whether a prepared change happened", "accounts, VAT or tax",
                   "change the app's settings (prices, hours, plan)",
                   "the interface feedback described below, and the fixed summary below",
                   "Say money amounts in full as the backend wrote them",
                   "£252.50 is 'two hundred and fifty-two pounds fifty'",
                   "Do not read reference codes aloud",
                   "Do not treat a cough, music or nearby conversation as a new request; keep listening while the user pauses."):
        assert phrase in prompt, phrase
    # The earlier wording is gone: it listed prices as something the app never changes.
    assert "change prices or settings" not in prompt and "exactly as the backend gave them" not in prompt
    # The interface-feedback carve-out is kept verbatim.
    assert "Development, testing and feedback about BLAM8's live voice or UI" in prompt
    assert live.config()["instructions"] == live.PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("words", [
    "Neither's been sent.", "Neither has been sent yet.", "Neither’s been sent.",
    "None were sent.", "Nothing has been executed.", "Nor was the message sent.",
    "Neither INV-1001 nor INV-1002 was sent.", "I haven't sent it.", "It hasn’t been sent.",
    "The message was not sent.", "I never sent it.", "No message was sent.",
    'The example is "Nothing happened. INV-1002 was sent."',
])
async def test_fragmented_neither_negation(unit, words):
    s = session()
    await s.send("session.commentary.append", "Neither has been sent yet.")
    # Word, contraction and punctuation boundaries all arrive independently.
    for i, char in enumerate(words):
        await live.handle_event(s, {**fragment(char, i, i+1, str(i)),
                                   "type": "session.output_transcript.delta"}, unit)
    assert not [e for e in s.events if e["kind"] == "guard"]
    assert not s.counters.get("output_guard")
    assert not [e for e in s.socket.sent if e["type"] == "session.instructions.append"]


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [" but ", " and ", "; ", ", however "])
@pytest.mark.parametrize("negative", ["INV-1001 was not sent", "Neither's been sent",
                                      "None were sent", "Nothing was sent"])
async def test_fragmented_mixed_negation_keeps_affirmative_guard(unit, separator, negative):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear()
    s = session()
    await s.send("session.commentary.append", "INV-1001 was sent.")
    for i, words in enumerate([negative, separator, "INV-1002 was ", "sent", "."]):
        await live.handle_event(s, {**fragment(words, i*10, (i+1)*10, str(i)),
                                   "type": "session.output_transcript.delta"}, unit)
    assert len([e for e in s.events if e["kind"] == "guard"]) == 1
    assert s.counters["output_guard"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["direct", "fragmented"])
@pytest.mark.parametrize("words,supported,guards", [
    ("I sent it.", False, 1), ("I sent it.", True, 0),
    ("Neither invoice was sent, but I cancelled the booking.", False, 1),
    ("Neither was sent, but I charged the account.", False, 1),
    ("Neither was sent I charged the account.", False, 1),
    ("Neither was sent nor was it charged.", False, 0),
    ("I sent it nothing else happened.", False, 1),
    ("I sent nothing.", False, 0),
    ("I sent none.", False, 0),
    ("The invoice was sent nothing else changed.", False, 1),
    ("I sent nothing less than the full invoice.", False, 1),
    ("I sent none other than the full invoice.", False, 1),
    ("I sent no fewer than three invoices.", False, 1),
    ("The invoice was sent unconfirmed details followed.", False, 1),
    ("Neither invoice was overdue so I sent both.", False, 1),
    ("Nothing prevented it: I sent both invoices.", False, 1),
    ("None needed changes before I sent both.", False, 1),
    ("I never hesitated then sent both.", False, 1),
    ("I haven't checked whether it was sent.", False, 1),
    ("Neither was overdue therefore both were sent.", False, 1),
])
async def test_affirmative_guard_regression(unit, words, supported, guards, delivery):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session()
    if supported:
        await s.send("session.commentary.append", words)
    if delivery == "direct":
        await live.monitor_output(s, words, unit)
    else:
        for i, char in enumerate(words):
            await live.handle_event(s, {**fragment(char, i, i+1, str(i)),
                                       "type": "session.output_transcript.delta"}, unit)
    assert len([e for e in s.events if e["kind"] == "guard"]) == guards
    assert len([e for e in s.socket.sent if e["type"] == "session.instructions.append"]) == guards


@pytest.mark.asyncio
async def test_guard_fragment_correlation_and_clause_boundaries(unit, monkeypatch):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    s.delegations["d"] = {"turn": "context-turn", "state": "complete"}
    prefix = "Neither invoice was sent, but"
    chunks = [prefix, " I cancelled ", "the book", "ing."]
    for i, words in enumerate(chunks):
        await live.handle_event(s, {**fragment(words, i*100, (i+1)*100, f"out-{i}"),
                                   "type": "session.output_transcript.delta"}, unit)
    await s.diagnostic_job
    detail = next(t["detail"] for t in traces if t["outcome"] == "output_guard")
    assert detail["transcript_ids"] == ["out-1", "out-2", "out-3"]
    assert detail["fragment_bounds_ms"] == [[100, 200], [200, 300], [300, 400]]
    assert detail["clause_start_char"] == len(prefix)
    assert detail["clause_end_char"] == len("".join(chunks))
    assert detail["published_seq"] == s.events[-1]["seq"]
    assert detail["application_session_id"] == s.id
    assert detail["context_turn_ids"] == ["context-turn"] and detail["turn_id"] is None
    assert detail["turn_linkage"] == "session_context_only"
    assert s.output == []


@pytest.mark.asyncio
async def test_guard_fragment_buffer_and_diagnostics_are_bounded(unit, monkeypatch):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    await live.handle_event(s, {**fragment("x"*1800, 0, 1, "long"), "type": "session.output_transcript.delta"}, unit)
    for i, char in enumerate(" I sent it" + " "*40 + "."):
        await live.handle_event(s, {**fragment(char, i+1, i+2, f"char-{i}"), "type": "session.output_transcript.delta"}, unit)
        assert sum(len(p["delta"]) for p in s.output) <= 1600
    await s.diagnostic_job
    detail = traces[-1]["detail"]
    assert len(detail["transcript_ids"]) == 32 and detail["transcript_ids_omitted"] > 0
    assert detail["clause_start_char"] > 0 and s.output == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["new_input", "discard", "close", "provider_close"])
@pytest.mark.parametrize("committed", [False, True])
async def test_cancellation_source_correlation(unit, monkeypatch, source, committed):
    s = session(); live._sessions[s.id] = s; s.consumed_ms = 100; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    s.delegations["d"] = {"turn": "source-turn", "state": "working"}
    state = voice_turn_state.begin(voice_turn_state.key(s.tenant, s.user, "source-turn"))
    if committed:
        voice_turn_state.commit(state, "entered_write")
    if source == "new_input":
        await live.handle_event(s, fragment("Wait, stop.", 110, 200, "trigger-fragment"), unit)
    elif source == "provider_close":
        await live.handle_event(s, {"type": "session.closed", "event_id": "provider-end"}, unit)
        await live.finish(s)
    else:
        await live.control(live.Control(session=s.id, operation=source, turn_id="source-turn", control_id="control-1"), unit)
    await s.diagnostic_job
    detail = next(t["detail"] for t in traces if t["outcome"] in {"input_cancellation", "turn_cancellation"})
    assert detail["source"] == ("close" if source == "provider_close" else source)
    assert detail["application_session_id"] == s.id and detail["turn_id"] == "source-turn"
    assert detail["request_id"] and detail["consumed_ms"] == 100
    assert datetime.fromisoformat(detail["requested_at"]) <= datetime.fromisoformat(detail["returned_at"])
    assert detail["cancel_status"] == ("too_late" if committed else "cancelled")
    assert state.status == ("committing" if committed else "cancelled")
    if source == "new_input":
        assert detail["event_id"] == "trigger-fragment" and detail["trigger_type"] == "transcript"
    elif source == "provider_close":
        assert detail["event_id"] == "provider-end" and detail["trigger_type"] == "provider_event"
    else:
        assert detail["control_id"] == "control-1" and detail["trigger_type"] == "control"


@pytest.mark.asyncio
async def test_cancellation_source_correlation_hedge_loser_is_not_turn_cancellation(unit):
    from app import llm
    from tests.test_llm_hedge import _client
    s = session(); receipts = []; state = voice_turn_state.begin(voice_turn_state.key(s.tenant, s.user, "hedged-turn"))
    def report(*args, **kw):
        receipts.append((kw["ordinal"], args[6]))
    result = await llm._stream_hedged(_client([1, .001], []), {}, None, .001, report)
    assert result.tag == 1 and (0, "cancelled") in receipts and (1, "winner") in receipts
    assert state.status == "running" and s.diagnostic_rows == 0


@pytest.mark.asyncio
async def test_blocked_diagnostics_do_not_delay_protection_and_have_capacity(unit, monkeypatch):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session(); entered = asyncio.Event(); release = asyncio.Event()
    async def blocked(*args, **kw):
        entered.set(); await release.wait()
    monkeypatch.setattr(live, "safe_trace", blocked)
    try:
        await asyncio.wait_for(live.monitor_output(s, "I sent it.", unit), .1)
        await entered.wait()
        assert s.events[-1]["kind"] == "guard" and s.socket.sent[-1]["type"] == "session.instructions.append"
        for i in range(100):
            s.delegations[str(i)] = {"turn": str(i), "state": "working"}
        await asyncio.wait_for(live.handle_event(s, fragment("stop", event_id="noise"), unit), .1)
        assert all(voice_turn_state._states[voice_turn_state.key(s.tenant, s.user, str(i))].status == "cancelled" for i in range(100))
        assert s.fragments[-1]["event_id"] == "noise"
        # 100 cancellations + the one-off first-input field-name row = 101 rows into a queue of 64
        assert len(s.diagnostic_queue) == 64 and s.counters["diagnostic_dropped"] == 37
    finally:
        release.set()
        if s.diagnostic_job:
            await s.diagnostic_job


@pytest.mark.asyncio
async def test_output_guard_timestamped_hashes_even_when_stop_send_fails(unit, monkeypatch):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear()
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    s.speech_context = ["INV-1001 was sent."]
    async def failed_send(*args):
        assert s.events[-1]["kind"] == "guard"
        raise RuntimeError("unavailable")
    monkeypatch.setattr(s, "send", failed_send)
    with pytest.raises(RuntimeError):
        await live.monitor_output(s, "INV-1002 was sent.", unit)
    await s.diagnostic_job
    args, row = traces[0]; detail = row["detail"]
    assert args[1] == "gptlive_stage" and row["outcome"] == "output_guard"
    assert datetime.fromisoformat(detail["observed_at"]).tzinfo is not None
    expected = hashlib.sha256(json.dumps(live.claim_tokens("INV-1002 was sent."),
                                        separators=(",", ":")).encode()).hexdigest()
    assert detail["claim_hash"] == expected
    assert detail["claim_hash"] not in detail["support_candidate_hashes"]
    assert detail["speech_context_size"] == 1 and detail["support_candidates_omitted"] == 0
    assert "INV-100" not in json.dumps(traces)
    assert s.counters["output_guard"] == 1


@pytest.mark.asyncio
async def test_guard_counter_and_context_are_captured_before_stop_await(unit, monkeypatch):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    async def slow_send(*args):
        entered.set(); await release.wait()
    monkeypatch.setattr(s, "send", slow_send)
    pending = asyncio.create_task(live.monitor_output(s, "I sent it.", unit))
    await entered.wait()
    assert s.counters["output_guard"] == 1 and s.events[-1]["kind"] == "guard"
    s.speech_context.append("Later result.")
    s.delegations["later"] = {"turn": "later-turn", "state": "working"}
    release.set(); await pending; await s.diagnostic_job
    assert traces[0]["detail"]["speech_context_size"] == 0
    assert traces[0]["detail"]["context_turn_ids"] == []
    assert s.counters["output_guard"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("words,nonverbal,cancels", [
    ("[chuckle", True, False), ("[clear throat", True, False), (" [chuckle] ", True, False),
    ("Please check again.", False, False), ("[chuckle] Check again", False, False),
    ("[chuckle] No, check again", False, True), ("Wait, stop.", False, True)])
@pytest.mark.parametrize("committed", [False, True])
async def test_new_input_cancellation_diagnostic_keeps_actual_outcome(unit, monkeypatch, words, nonverbal, cancels, committed):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    s.delegations = {"d": {"turn": "working-turn", "state": "working"},
                     "done": {"turn": "done-turn", "state": "complete"}}
    state = voice_turn_state.begin(voice_turn_state.key(s.tenant, s.user, "working-turn"))
    if committed:
        voice_turn_state.commit(state, "entered_write")
    await live.handle_event(s, fragment(words, 0, 100, "old"), unit)
    assert not traces
    incoming = fragment(words, 110, 200, "new-input")
    await live.handle_event(s, incoming, unit)
    await live.handle_event(s, incoming, unit)
    await s.diagnostic_job
    # The first input of a session also logs its field names (input_event_shape); the
    # duplicate delivery above must still be judged exactly once.
    assert len(traces) == 2
    assert words not in json.dumps(traces)
    assert s.fragments[-1]["event_id"] == "new-input"
    if not cancels:
        # Heard, but not a cue: the turn keeps running and a content-free input_held row says so.
        assert not [t for t in traces if t["outcome"] == "input_cancellation"]
        held = [t["detail"] for t in traces if t["outcome"] == "input_held"]
        assert len(held) == 1 and held[0]["turn_id"] == "working-turn" and held[0]["event_id"] == "new-input"
        assert (held[0]["start_ms"], held[0]["end_ms"]) == (110, 200) and held[0]["pending_fragments"] == 1
        assert state.status == ("committing" if committed else "running") and s.counters["input_held"] == 1
        return
    cancelled = [t for t in traces if t["outcome"] == "input_cancellation"]
    assert len(cancelled) == 1
    detail = cancelled[0]["detail"]
    assert detail["stage"] == "input_cancellation" and detail["turn_id"] == "working-turn"
    assert detail["event_id"] == "new-input" and (detail["start_ms"], detail["end_ms"]) == (110, 200)
    assert detail["bracketed_nonverbal"] is nonverbal and detail["cue"] is True
    assert detail["cancel_status"] == ("too_late" if committed else "cancelled")
    assert state.status == ("committing" if committed else "cancelled")
    assert datetime.fromisoformat(detail["observed_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_input_cancellation_rows_carry_pending_counts_and_no_words(unit, monkeypatch):
    import time
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    s.delegations["d"] = {"turn": "working-turn", "state": "working", "started": time.monotonic() - 2}
    voice_turn_state.begin(voice_turn_state.key(s.tenant, s.user, "working-turn"))
    # a session that never had a turn age still reports one (>= 0), so older jobs are not special
    s.delegations["bare"] = {"turn": "bare-turn", "state": "working"}
    # three fragments, four words (one fragment holds two): the last one is what ends the turn under either
    # interrupt rule, and a count of fragments must not pass for a count of words
    for words, start, end, name in (("Pelican", 110, 200, "a"), (" marmalade ferry [cough]", 210, 300, "b"), (" actually", 310, 400, "c")):
        await live.handle_event(s, fragment(words, start, end, name), unit)
    await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_cancellation" and t["detail"]["turn_id"] == "working-turn"]
    last = rows[-1]
    assert last["event_id"] == "c" and last["trigger_type"] == "transcript"
    assert last["pending_words"] == 4 and last["pending_fragments"] == 3        # "[cough]" is not a word
    assert last["pending_span_ms"] == 290 and last["since_question_ms"] == 300  # 110..400 ; 400 - consumed_ms
    assert 2000 <= last["turn_age_ms"] < 20000
    bare = [t["detail"] for t in traces if t["outcome"] == "input_cancellation" and t["detail"]["turn_id"] == "bare-turn"]
    assert bare and all(type(row["turn_age_ms"]) is int and row["turn_age_ms"] >= 0 for row in bare)
    dumped = json.dumps(traces).lower()
    assert "pelican" not in dumped and "marmalade" not in dumped and "ferry" not in dumped and "actually" not in dumped
    assert all(type(last[key]) is int for key in ("pending_words", "pending_fragments", "pending_span_ms", "since_question_ms", "turn_age_ms"))


@pytest.mark.asyncio
async def test_delegation_seen_counts_working_turns(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    s.delegations["earlier"] = {"turn": "earlier-turn", "state": "working", "offset": 0, "received": 0}
    s.delegations["finished"] = {"turn": "finished-turn", "state": "complete", "offset": 0, "received": 0}
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    seen = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "delegation_seen"]
    assert len(seen) == 1 and seen[0]["working_turns"] == 1
    s, traces = session(), []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    seen = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "delegation_seen"]
    assert seen[0]["working_turns"] == 0


@pytest.mark.asyncio
async def test_the_seen_event_cap_is_pinned_and_how_close_a_session_got_is_counted(unit, monkeypatch):
    s = session(); traces = []; live._sessions[s.id] = s
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    def usage(i):
        return {"type": "session.usage.updated", "event_id": f"usage-{i}", "usage": {"seconds": 1}}
    for i in range(live_cap := 4096):
        await live.handle_event(s, usage(i), unit)
    assert len(s.seen) == live_cap and s.counters["events_seen"] == live_cap
    await live.handle_event(s, usage(7), unit)                      # a repeat is ignored before the cap is looked at
    assert s.counters["events_seen"] == live_cap
    with pytest.raises(ValueError, match="Event capacity reached"):  # the next new event ends the sideband
        await live.handle_event(s, usage(live_cap), unit)
    assert len(s.seen) == live_cap and s.counters["events_seen"] == live_cap
    # content-free, in the close row
    await live.finish(s)
    summary = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_session")
    assert summary["stages"]["events_seen"] == live_cap


@pytest.mark.asyncio
async def test_a_discard_keeps_its_reason_even_with_a_later_request_queued(unit, monkeypatch):
    # The discard control bumps the generation; a delegation anchored on later speech may be queued at
    # that moment, and it is not what stood this turn down.
    import time
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        raise live.BackendCancelled("Business turn interrupted")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    s.fragments.append({"event_id": "later", "start_ms": 110, "end_ms": 240, "delta": " and the invoices", "received": time.monotonic()})
    s.delegations["d2"] = {"offset": 250, "received": time.monotonic(), "state": "queued", "ambiguous": False}
    assert live.superseded(s, s.delegations["d1"], s.consumed_ms) is True       # it is anchored and queued...
    s.generation += 1                                                              # ...but a discard landed
    release.set(); await asyncio.gather(*list(s.jobs))
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_cancelled"]
    assert len(stages) == 1 and stages[0]["reason"] == "discard"
    assert not [e for e in s.events if e["kind"] == "status"]


def test_snapshot_meta_reports_largest_gap_and_utterances(unit):
    s = _tagged_window(fragment("Check", 0, 200, "a"), fragment(" the", 300, 500, "b"), fragment(" diary", 3200, 3400, "c"))
    job = {"offset": 100}
    assert live.snapshot(s, job) == ("Check the diary", 3400)
    assert job["context_meta"]["largest_gap_ms"] == 2700 and job["context_meta"]["utterances"] == 2
    # the boundary is a gap of 2500 ms or more
    for gap, utterances in ((2499, 1), (2500, 2)):
        s = _tagged_window(fragment("Check", 0, 200, "a"), fragment(" the diary", 200 + gap, 400 + gap, "b"))
        job = {"offset": 100}
        live.snapshot(s, job)
        assert job["context_meta"]["largest_gap_ms"] == gap and job["context_meta"]["utterances"] == utterances
    # one fragment has no gap; no fragments is no utterance
    s = _tagged_window(fragment("Check the diary", 0, 200, "a"))
    job = {"offset": 100}
    live.snapshot(s, job)
    assert job["context_meta"]["largest_gap_ms"] == -1 and job["context_meta"]["utterances"] == 1
    s = _tagged_window()
    job = {"offset": 100}
    assert live.snapshot(s, job) is None
    assert job["context_meta"]["largest_gap_ms"] == -1 and job["context_meta"]["utterances"] == 0


@pytest.mark.asyncio
async def test_first_input_event_shape_is_logged_once_without_values(unit, monkeypatch):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    first = {**fragment("Pelican marmalade", 0, 200, "one"), "confidence": 0.93, "speaker": "voice-zebra-7",
             "nested": {"inner": "kumquat"}, "bad key!": "x", 7: "numeric key"}
    await live.handle_event(s, first, unit)
    await live.handle_event(s, {**fragment(" ferry", 210, 300, "two"), "extra_field": 1}, unit)
    # output transcripts are not input and never log a shape
    await live.handle_event(s, {**fragment("Reply", 0, 50, "out"), "type": "session.output_transcript.delta"}, unit)
    await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_event_shape"]
    assert len(rows) == 1 and s.counters["input_event_shape"] == 1
    assert rows[0]["fields"] == ["confidence", "delta", "end_ms", "event_id", "nested", "speaker", "start_ms", "type"]
    dumped = json.dumps(traces)
    for value in ("Pelican", "marmalade", "voice-zebra-7", "0.93", "kumquat", "numeric key", "extra_field"):
        assert value not in dumped


def test_control_capture_and_levels_are_allow_listed_and_never_reject_a_close():
    body = live.Control(session="s", operation="close", levels=[0, 1, 2, 3, 4, 5, 6, 7, 65535],
        capture={"echoCancellation": True, "noiseSuppression": False, "autoGainControl": None, "channelCount": 1,
                 "sampleRate": 48000, "deviceId": "private-device", "label": "Private microphone"})
    assert body.capture == {"echoCancellation": True, "noiseSuppression": False, "channelCount": 1, "sampleRate": 48000}
    assert body.levels == [0, 1, 2, 3, 4, 5, 6, 7, 65535]
    # anything off the list is dropped, not rejected: a diagnostic must not cost a close
    for capture, levels in ((["x"], [1, 2, 3]), ({"autoGainControl": "yes", "channelCount": 999, "sampleRate": -1}, [1] * 8 + [65536]),
                            ("private", [True] * 9), ({}, [1.5] * 9)):
        bad = live.Control(session="s", operation="close", capture=capture, levels=levels)
        assert bad.capture is None and bad.levels is None
    old = live.Control(session="s", operation="close")
    assert old.capture is None and old.levels is None


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_client_close_reports_capture_and_levels(unit, monkeypatch, registered):
    s = session(); traces = []
    if registered:
        live._sessions[s.id] = s
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    body = live.Control(session=s.id if registered else s.voice_session, operation="close", close_reason="end",
        capture={"echoCancellation": True, "noiseSuppression": True, "autoGainControl": True, "channelCount": 1, "sampleRate": 48000},
        levels=[10, 20, 30, 40, 5, 0, 0, 0, 7])
    await live.control(body, unit)
    report = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_client_close")
    assert report["capture"]["autoGainControl"] is True and report["capture"]["sampleRate"] == 48000
    assert report["levels"] == [10, 20, 30, 40, 5, 0, 0, 0, 7]


# ── What may stop a working turn: a correction or stop cue, or a new request; not any word ──────────

@pytest.mark.parametrize("words,cue", [
    ("Stop.", True), ("stop", True), ("Wait", True), ("Wait, wait.", True), ("Hold on a moment", True), ("hang on", True),
    ("One sec", True), ("one second", True), ("One moment please", True), ("Actually", True), ("Sorry", True),
    ("No,", True), ("No.", True), ("no!", True), ("Thursday, not Friday.", True), ("instead", True),
    ("I meant Tom", True), ("make that Friday", True), ("rather than Leah", True),
    ("the other one", True), ("Never mind", True), ("nevermind", True), ("Forget it", True), ("forget that", True),
    ("ignore that", True), ("scrap that", True), ("Don’t worry about it", True), ("don't worry", True),
    ("Cancel that", True), ("cancel it", True), ("[cough] stop", True), ("[inhale", False),
    # added after review: a repeated "no", and the natural ways of taking a request back or starting over
    ("no no", True), ("No no no", True), ("No, no", True), ("no   no", True),
    ("scratch that", True), ("start again", True), ("Try again", True), ("that's wrong", True), ("That’s wrong", True), ("thats wrong", True),
    ("wrong one", True), ("the wrong one", True),
    ("make it Friday", False), ("make it", False), ("change", False), ("swap", False), ("scratch", False),
    ("redo", False), ("start", False), ("again", False), ("try", False), ("wrong", False), ("no one", False),
    ("no nothing", False), ("no nobody", False), ("nono", False), ("I know no", False), ("they swapped it", False),
    ("restart again", False), ("changed it", False), ("scratching", False), ("starting again", False),
    ("Yeah", False), ("Hello", False), ("Please", False), ("[cough", False), ("Mm-hmm", False), ("Okay", False),
    ("Thanks", False), ("Right", False), ("Uh-huh", False), ("[laughter]", False), ("the weather tonight", False),
    ("no", False), ("No problem", False), ("nobody", False), ("Nothing", False), ("piano, then", False),
    ("unstoppable", False), ("waiting for it", False), ("forget items", False), ("cancelled", False),
    ("the stopover", False), ("knot", False), ("another one", False),
    # rig N20 (15 Oct): "change it", "swap it" and "redo it" (and "that") are the ordinary words of a request, not a
    # cue. The owner's own second half ("Or change it to two hundred quid") cancelled his request when they were.
    ("change it", False), ("Change that", False), ("Change it to Friday", False), ("swap that", False),
    ("Swap it", False), ("redo it", False), ("Redo that", False),
    ("Or change it to two hundred quid", False), ("Can you just bin it? Or change it to two hundred quid.", False),
    ("I think I put the wrong amount on one of Tom's invoices, a draft one. Can you just bin it? "
     "Or change it to two hundred quid.", False),
    ("could you swap it for a different one", False), ("can you redo it", False), ("can you change that to Thursday", False),
    ("change it to two hundred, no, wait", True),    # a real cue beside the words is still a cue
])
def test_the_interrupt_cue_table(words, cue):
    assert live.heard_cue([{"delta": words}]) is cue


@pytest.mark.asyncio
async def test_plain_words_during_a_working_turn_hold_and_do_not_cancel(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    for i, words in enumerate(("the weather", " tonight will be", " mostly dry")):
        await live.handle_event(s, fragment(words, 110 + i * 200, 300 + i * 200, f"tv-{i}"), unit)
    await s.diagnostic_job
    assert state.status == "running"
    held = [t["detail"] for t in traces if t["outcome"] == "input_held"]
    assert [h["pending_words"] for h in held] == [2, 5, 7]
    assert [h["pending_fragments"] for h in held] == [1, 2, 3]
    assert all(h["turn_id"] == "working-turn" and h["turn_age_ms"] >= 0 for h in held)
    assert not [t for t in traces if t["outcome"] == "input_cancellation"] and "input_cancellation" not in s.counters
    assert s.counters["input_held"] == 3
    dumped = json.dumps(traces)
    assert not any(word in dumped for word in ("weather", "tonight", "mostly"))
    assert [p["event_id"] for p in s.fragments] == ["tv-0", "tv-1", "tv-2"]   # held, not dropped


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("No", ","), ("Hold", " on"), ("One", " second"), ("Make", " that Friday"),
                                          ("I", " meant Leah"), ("Rather", " than Friday"), ("The other", " one"),
                                          ("No", " no"), ("Scratch", " that"), ("Cancel", " it"),
                                          ("Start", " again"), ("Try", " again"),
                                          ("That’s", " wrong"), ("Wrong", " one")])
async def test_a_correction_cue_cancels_even_when_split_across_fragments(unit, monkeypatch, first, second):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    await live.handle_event(s, fragment(first, 110, 200, "first"), unit)
    assert state.status == "running"
    await live.handle_event(s, fragment(second, 210, 300, "second"), unit)
    await s.diagnostic_job
    assert state.status == "cancelled"
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["cue"] is True and row["event_id"] == "second" and row["pending_fragments"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("Swap", " that"), ("Swap", " it for a different one"), ("Redo", " it"),
                                          ("Change", " it to Friday"), ("Change", " that"),
                                          ("Or change", " it to two hundred quid")])
async def test_the_ordinary_words_of_a_request_do_not_cancel_a_working_turn_even_split(unit, monkeypatch, first, second):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    await live.handle_event(s, fragment(first, 110, 200, "first"), unit)
    await live.handle_event(s, fragment(second, 210, 300, "second"), unit)
    await s.diagnostic_job
    assert state.status == "running" and "cancel_requested" not in s.delegations["working-turn"]
    assert not [t for t in traces if t["outcome"] == "input_cancellation"] and "input_cancellation" not in s.counters
    assert s.counters["input_held"] == 2


@pytest.mark.asyncio
async def test_the_second_half_of_a_request_is_held_not_taken_as_a_cancel_cue(unit, monkeypatch):
    # Rig N20, 15 Oct: "... Can you just bin it? [pause] Or change it to two hundred quid." The second half arrived
    # while the first half's turn was working, "change it" was a cue, and the owner's own request was cancelled.
    s = session(); traces = []; seen = []; histories = []; entered = asyncio.Event(); release = asyncio.Event()
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))

    async def backend(ctx, question, history, turn, voice):
        seen.append(question); histories.append([dict(message) for message in history])
        if len(seen) == 1:
            entered.set(); await release.wait()
            raise live.BackendCancelled("Business turn cancelled")
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Can you just bin it", 0, 700, "first-half"), unit)
    await live.handle_event(s, delegation(offset=700), unit)
    await entered.wait()
    await live.handle_event(s, fragment(" Or change it to two hundred quid", 1700, 2600, "second-half"), unit)
    await s.diagnostic_job
    assert s.delegations["d1"]["state"] == "working" and "cancel_requested" not in s.delegations["d1"]
    assert "input_cancellation" not in s.counters and s.counters["input_held"] == 1
    # only the model's own judgement that this is a new request may replace the turn
    await live.handle_event(s, delegation("second-delegation", "d2", offset=2600), unit)
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_cancellation"]
    assert len(rows) == 1 and rows[0]["trigger_type"] == "delegation" and rows[0]["cue"] is False
    assert s.delegations["d1"]["state"] == "cancelled" and s.delegations["d2"]["state"] == "complete"
    # ... and it runs the whole request: the first half is carried into it, so "change what?" never reaches the backend
    assert seen == ["Can you just bin it", WHOLE_REQUEST]
    # the history the backend gets holds no unanswered first half (it would be told to ignore it), and nothing is left over
    assert histories[1] == [] and s.carry == {}
    assert s.history == [{"role": "user", "content": WHOLE_REQUEST}, {"role": "assistant", "content": "Fixture read only"}]


WHOLE_REQUEST = "Can you just bin it Or change it to two hundred quid"


async def _a_request_split_by_a_pause(s, unit, monkeypatch, continuation, first="Can you just bin it", first_end=700,
                                      start=1700, end=None, redelegate=True):
    """The first half of a request is delegated and working (its backend waits); the second half is heard; the model
    then raises a request on it (unless `redelegate` is off). Returns the backend's (question, history) calls."""
    calls = []; entered = asyncio.Event(); release = asyncio.Event()
    end = start + 900 if end is None else end

    async def backend(ctx, question, history, turn, voice):
        calls.append((question, [dict(message) for message in history]))
        if len(calls) == 1:
            entered.set(); await release.wait()
            raise live.BackendCancelled("Business turn cancelled")
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(first, 0, first_end, "first-half"), unit)
    await live.handle_event(s, delegation(offset=first_end), unit)
    await entered.wait()
    await live.handle_event(s, fragment(continuation, start, end, "second-half"), unit)
    if redelegate:
        await live.handle_event(s, delegation("second-delegation", "d2", offset=end), unit)
    release.set(); await asyncio.gather(*list(s.jobs))
    if s.diagnostic_job:
        await s.diagnostic_job
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation,cue", [
    (" Or change it to two hundred quid", False),          # no cue: held, then the model's own delegation stands the turn down
    (" no, change it to two hundred quid", True),          # a cue: stood down at once, and the same question follows
    (" actually change it to two hundred", True),
    (" sorry, I mean change it to two hundred", True),
    (" or not, just change it to two hundred", True),
])
async def test_a_request_stood_down_by_its_own_second_half_is_carried_into_the_next(unit, monkeypatch, continuation, cue):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, continuation)
    whole = "Can you just bin it " + continuation.strip()
    assert [question for question, history in calls] == ["Can you just bin it", whole]
    assert calls[1][1] == []                                  # clean history: no unanswered turn, so no "do not answer it" note
    assert s.history == [{"role": "user", "content": whole}, {"role": "assistant", "content": "Fixture read only"}]
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["cue"] is cue and row["trigger_type"] == ("transcript" if cue else "delegation")
    requested = [t["detail"] for t in traces if t["outcome"] == "backend_requested"]
    assert [r.get("carried") for r in requested] == [None, True]              # a flag, never the words
    assert "bin it" not in json.dumps(traces) and "two hundred" not in json.dumps(traces)
    assert s.carry == {} and s.delegations["d1"]["state"] == "cancelled" and s.delegations["d2"]["state"] == "complete"
    assert s.delegations["d2"]["context_event_ids"] == ["first-half", "second-half"]     # the saved turn links both halves
    status = [e["text"] for e in s.events if e["kind"] == "status"]
    assert status == ["Stopped before anything ran — your newer words took over. Nothing was changed."]   # a re-run is on its way


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [" check the diary tomorrow", " actually, check the diary tomorrow"])
async def test_a_request_more_than_the_utterance_gap_later_is_not_carried(unit, monkeypatch, continuation):
    # 5.2 s of audio after the first half ended: another request, not the second half of that one
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, continuation, start=5900, end=6500)
    assert [question for question, history in calls] == ["Can you just bin it", continuation.strip()]
    assert calls[1][1] == [] and s.carry == {}
    requested = [t["detail"] for t in traces if t["outcome"] == "backend_requested"]
    assert [r.get("carried") for r in requested] == [None, None]


@pytest.mark.asyncio
async def test_the_carry_gap_is_the_utterance_gap_on_the_audio_clock(unit, monkeypatch):
    for gap, carried in ((live.UTTERANCE_GAP_MS, True), (live.UTTERANCE_GAP_MS + 1, False)):
        s = session()
        calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, " check the diary tomorrow",
                                                  start=700 + gap, end=700 + gap + 600)
        assert (calls[1][0] == "Can you just bin it check the diary tomorrow") is carried, gap
        assert calls[1][0] in ("Can you just bin it check the diary tomorrow", "check the diary tomorrow")


def test_a_word_that_began_before_the_carried_request_ended_is_not_joined_to_it(unit):
    # the carry's gap is measured on the audio clock and never negative: a late tail word crossing the end is its own
    s = session(); s.consumed_ms = 700
    s.carry = {"text": "Can you just bin it", "start_ms": 0, "end_ms": 700, "event_ids": ["first-half"], "turn": "t"}
    s.fragments = [{"event_id": "late", "start_ms": 650, "end_ms": 1300, "delta": " change it to two hundred quid"}]
    assert live.snapshot(s, {"offset": 1200, "received": 0}) == ("change it to two hundred quid", 1300)
    s.fragments = [{"event_id": "next", "start_ms": 700, "end_ms": 1300, "delta": " change it to two hundred quid"}]
    assert live.snapshot(s, {"offset": 1200, "received": 0}) == ("Can you just bin it change it to two hundred quid", 1300)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation,reason", [(" never mind", "retraction"), (" yes", "approval_requires_card")])
async def test_a_retraction_or_a_bare_yes_after_a_split_is_still_what_it_is(unit, monkeypatch, continuation, reason):
    # the carry is joined after those checks: "never mind" must not become "Can you just bin it never mind"
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, continuation)
    assert [question for question, history in calls] == ["Can you just bin it"]      # nothing was run on the second half
    assert s.delegations["d2"]["state"] == "clarification"
    row = next(t["detail"] for t in traces if t["outcome"] == "context_unconfirmed")
    assert row["reason"] == reason and "carried" not in row
    assert s.carry == {} and [e["text"] for e in s.events if e["kind"] == "question"] == ["Can you just bin it"]


@pytest.mark.asyncio
async def test_under_any_words_nothing_is_carried(unit, monkeypatch):
    # any_words stays the exact earlier behaviour: the second half is a question of its own and the unanswered
    # first half stays in the history
    monkeypatch.setattr(live.settings, "gptlive_interrupt_rule", "any_words")
    s = session()
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, " Or change it to two hundred quid")
    assert [question for question, history in calls] == ["Can you just bin it", "Or change it to two hundred quid"]
    assert calls[1][1] == [{"role": "user", "content": "Can you just bin it"}]
    assert s.carry == {}
    status = [e["text"] for e in s.events if e["kind"] == "status"]
    assert status == ["Stopped before anything ran — your newer words took over. Nothing was changed; say it again if you still want it."]


@pytest.mark.asyncio
async def test_without_a_re_delegation_the_screen_still_says_to_ask_again_and_the_history_is_clean(unit, monkeypatch):
    s = session()
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, " actually, just bin it", redelegate=False)
    assert [question for question, history in calls] == ["Can you just bin it"]
    status = [e["text"] for e in s.events if e["kind"] == "status"]
    assert status == ["Stopped before anything ran — your newer words took over. Nothing was changed; say it again if you still want it."]
    assert s.history == [] and s.carry["text"] == "Can you just bin it" and s.carry["end_ms"] == 700
    assert s.carry["event_ids"] == ["first-half"] and s.carry["start_ms"] == 0


@pytest.mark.asyncio
async def test_a_discard_or_a_close_drops_the_carry(unit, monkeypatch):
    for how in ("discard", "close"):
        s = session(); live._sessions[s.id] = s
        await _a_request_split_by_a_pause(s, unit, monkeypatch, " actually, just bin it", redelegate=False)
        assert s.carry
        if how == "discard":
            await live.control(live.Control(session=s.id, operation="discard", turn_id=s.delegations["d1"]["turn"]), unit)
        else:
            await live.finish(s)
        assert s.carry == {}, how


@pytest.mark.asyncio
async def test_the_carried_request_is_judged_as_the_whole_it_now_is(unit, monkeypatch):
    # each half is inside the limits; the two together are not
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    first = ("word " * 300).strip()                          # 1,499 characters
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, " " + ("more " * 120).strip(), first=first)
    assert [question for question, history in calls] == [first]                      # 2,099 together: refused, not run
    assert s.delegations["d2"]["state"] == "clarification" and s.carry == {}
    row = next(t["detail"] for t in traces if t["outcome"] == "context_unconfirmed")
    assert row["reason"] == "context_limit" and "carried" not in row
    # and the 30 s window counts from the first half's start
    s = session(); traces.clear(); live._sessions.clear()
    calls = await _a_request_split_by_a_pause(s, unit, monkeypatch, " change it to two hundred quid", first_end=20000,
                                              start=21000, end=33000)
    assert [question for question, history in calls] == ["Can you just bin it"]
    assert s.delegations["d2"]["state"] == "clarification"
    assert next(t["detail"] for t in traces if t["outcome"] == "context_unconfirmed")["reason"] == "context_limit"


@pytest.mark.asyncio
async def test_a_leading_no_with_punctuation_is_a_cue_and_the_rest_of_a_correction_follows(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    await live.handle_event(s, fragment("No,", 110, 200, "no"), unit)
    assert state.status == "cancelled"
    await live.handle_event(s, fragment(" Tom not Leah", 350, 500, "rest"), unit)
    await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_cancellation"]
    # asked once: the rest of the correction is counted, not written as a second row
    assert [r["event_id"] for r in rows] == ["no"] and rows[0]["cue"] is True
    assert s.counters["input_cancel_repeat"] == 1 and "input_held" not in s.counters


@pytest.mark.asyncio
async def test_one_cue_in_the_pool_writes_one_cancellation_row_not_one_per_later_fragment(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    voice_turn_state.commit(state, "entered_write")          # a committed turn: every cancel answers too_late
    await live.handle_event(s, fragment("That's not it", 110, 200, "cue"), unit)
    for i in range(40):
        await live.handle_event(s, fragment(" and so on", 210 + i * 10, 219 + i * 10, f"plain-{i}"), unit)
    await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_cancellation"]
    assert len(rows) == 1 and rows[0]["event_id"] == "cue" and rows[0]["cancel_status"] == "too_late"
    assert s.counters["input_cancel_repeat"] == 40 and "input_held" not in s.counters
    assert s.diagnostic_rows == 2                              # the first-input field names + the one cancellation
    assert s.delegations["working-turn"]["cancel_requested"] is True


@pytest.mark.asyncio
async def test_a_second_anchored_delegation_does_not_cancel_a_stood_down_turn_again(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    working_turn(s)
    await live.handle_event(s, fragment("Wait", 110, 200, "cue"), unit)
    await live.handle_event(s, delegation("first-event", "d2", offset=250), unit)
    await live.handle_event(s, delegation("second-event", "d3", offset=260), unit)
    await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    assert len([t for t in traces if t["outcome"] == "input_cancellation"]) == 1
    assert s.counters["input_cancel_repeat"] == 2


@pytest.mark.asyncio
async def test_a_split_retraction_cancels(unit, monkeypatch):
    s = session(); s.consumed_ms = 100
    state = working_turn(s)
    await live.handle_event(s, fragment("never", 110, 200, "a"), unit)
    assert state.status == "running"
    await live.handle_event(s, fragment(" mind", 210, 300, "b"), unit)
    assert state.status == "cancelled"


@pytest.mark.asyncio
async def test_stop_alone_cancels(unit):
    s = session(); s.consumed_ms = 100
    state = working_turn(s)
    await live.handle_event(s, fragment("Stop.", 110, 200, "stop"), unit)
    assert state.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("blip", ["Yeah", "Hello", "Please", "[cough", "Mm-hmm", "Okay", "Thanks", "[laughter]"])
async def test_blips_never_cancel(unit, monkeypatch, blip):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    await live.handle_event(s, fragment(blip, 110, 200, "blip"), unit)
    await s.diagnostic_job
    assert state.status == "running" and "input_cancellation" not in s.counters
    assert [t["detail"]["event_id"] for t in traces if t["outcome"] == "input_held"] == ["blip"]


@pytest.mark.asyncio
async def test_held_rows_are_capped_but_still_counted(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    total = live.INPUT_HELD_ROWS + 8
    for i in range(total):
        await live.handle_event(s, fragment("hmm", 110 + i * 10, 119 + i * 10, f"h{i}"), unit)
    await s.diagnostic_job
    assert state.status == "running" and s.counters["input_held"] == total
    assert len([t for t in traces if t["outcome"] == "input_held"]) == live.INPUT_HELD_ROWS
    assert s.diagnostic_rows <= live.INPUT_HELD_ROWS + 1   # + the one-off first-input field names


@pytest.mark.asyncio
async def test_the_jobs_own_window_cannot_cancel_it(unit, monkeypatch):
    # A delegation is admitted: snapshot freezes its window and the job turns "working", but
    # consumed_ms only moves after two more awaits. The owner's own tail words landing in that gap
    # (end_ms inside the frozen window) used to stand the turn down before it began.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); calls = []; seen = []
    async def gate(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            entered.set(); await release.wait()
    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "authorized", gate)
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    job = s.delegations["d1"]
    assert job["state"] == "working" and job["context_end"] == 100 and s.consumed_ms == -1
    await live.handle_event(s, fragment(" actually please", 95, 100, "tail"), unit)    # a cue, but inside the window
    release.set(); await asyncio.gather(*list(s.jobs))
    assert seen == ["Check the diary."] and job["state"] == "complete"
    assert not voice_turn_state._states and "input_cancellation" not in s.counters and "input_held" not in s.counters
    assert any(e["kind"] == "result" for e in s.events)


def _slow_first_turn(monkeypatch, entered, release, then="cancel", questions=None):
    """The first backend call waits on `release` and then either stands down (`cancel`, as voice_chat does
    for a turn cancelled before commit) or answers; later calls answer at once."""
    calls = []
    async def backend(ctx, question, history, turn, voice):
        calls.append(question)
        if questions is not None:
            questions.append(question)
        if len(calls) == 1:
            entered.set(); await release.wait()
            if then == "cancel":
                raise live.BackendCancelled("Business turn interrupted")
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    return calls


@pytest.mark.asyncio
async def test_a_new_delegation_past_the_question_cancels_the_working_turn(unit, monkeypatch):
    # The model raised a request anchored on speech the working turn never consumed: real new words
    # (not a cue) heard behind it, then the delegation.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    turn = s.delegations["d1"]["turn"]
    await live.handle_event(s, fragment(" and the invoices", 110, 240, "new-words"), unit)
    assert voice_turn_state._states.get(voice_turn_state.key(s.tenant, s.user, turn)) is None   # held: no cue, no request yet
    await live.handle_event(s, delegation("second-event", "d2", offset=250), unit)
    assert voice_turn_state._states[voice_turn_state.key(s.tenant, s.user, turn)].status == "cancelled"
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["trigger_type"] == "delegation" and row["event_id"] == "second-event" and row["offset_ms"] == 250
    assert row["cue"] is False and row["deferred"] is False and row["turn_id"] == turn and row["turn_age_ms"] >= 0
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_cancelled"]
    assert len(stages) == 1 and stages[0]["reason"] == "new_input"
    assert s.delegations["d1"]["state"] == "cancelled"
    assert any(e["kind"] == "status" and e["text"].startswith("Stopped before anything ran") for e in s.events)
    # the stood-down request is carried into the next: the two were one breath (see the carry tests below)
    assert questions == ["Check the diary.", "Check the diary. and the invoices"] and s.delegations["d2"]["state"] == "complete"


@pytest.mark.asyncio
async def test_a_delegation_that_beats_its_words_cancels_the_working_turn_when_they_arrive(unit, monkeypatch):
    # The transcript trails the delegation by about a second: the cancel waits for the words it anchored on.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    turn = s.delegations["d1"]["turn"]
    await live.handle_event(s, delegation("second-event", "d2", offset=250), unit)           # no words behind it yet
    assert voice_turn_state._states.get(voice_turn_state.key(s.tenant, s.user, turn)) is None
    await live.handle_event(s, fragment(" and the invoices", 110, 240, "late-words"), unit)   # now there are
    assert voice_turn_state._states[voice_turn_state.key(s.tenant, s.user, turn)].status == "cancelled"
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    rows = [t["detail"] for t in traces if t["outcome"] == "input_cancellation"]
    assert len(rows) == 1 and rows[0]["trigger_type"] == "delegation" and rows[0]["deferred"] is True
    assert rows[0]["event_id"] == "late-words" and rows[0]["offset_ms"] == 250 and rows[0]["cue"] is False
    assert s.delegations["d1"]["state"] == "cancelled" and s.delegations["d2"]["state"] == "complete"
    assert questions == ["Check the diary.", "Check the diary. and the invoices"]
    assert "input_held" not in s.counters      # judged as a request, not held


@pytest.mark.asyncio
async def test_a_re_delegation_with_no_new_speech_is_a_retry_and_the_turn_completes(unit, monkeypatch):
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, then="answer", questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)     # nothing was said behind it
    assert not voice_turn_state._states and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    assert s.delegations["d1"]["state"] == "complete" and questions == ["Check the diary."]
    assert len([e for e in s.events if e["kind"] == "result"]) == 1                      # the answer is shown, not withheld
    said = [m for m in s.socket.sent if m["type"] == "session.commentary.append"]
    assert [m["delegation_id"] for m in said] == ["d1"] and said[0]["content"] == "Fixture read only"
    assert not any("Stopped before anything ran" in e.get("text", "") or "did not receive" in e.get("text", "") for e in s.events)
    assert not any("newer words take priority" in e.get("text", "") for e in s.events)
    # the retry itself is answered with nothing: the question it repeats was already running
    assert s.delegations["d2"]["state"] == "clarification"
    row = next(t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed")
    assert row["reason"] == "missing_context" and row["retry"] is True and row["pending_fragments"] == 0


def _texts_the_owner_could_hear(s):
    """Every status line and every commentary the voice was given."""
    return [e.get("text", "") for e in s.events if e["kind"] == "status"] + \
           [m.get("content", "") for m in s.socket.sent if m["type"] == "session.commentary.append"]


@pytest.mark.asyncio
async def test_two_retries_queued_behind_a_working_turn_do_not_refuse_each_other(unit, monkeypatch):
    # P1: the model re-delegates twice while the backend works; a retry is a duplicate, not a request.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, then="answer", questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-one", "d2", offset=250), unit)
    await live.handle_event(s, delegation("retry-two", "d3", offset=260), unit)
    assert [s.delegations[k]["ambiguous"] for k in ("d2", "d3")] == [False, False]
    assert [s.delegations[k]["retry"] for k in ("d2", "d3")] == [True, True]
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "complete" and questions == ["Check the diary."]
    heard = _texts_the_owner_could_hear(s)
    assert heard == ["Fixture read only"]                       # only the answer: no "one task at a time", no "please repeat"
    rows = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed"]
    assert [(r["reason"], r["retry"]) for r in rows] == [("missing_context", True)] * 2
    assert s.delegations["d2"]["state"] == s.delegations["d3"]["state"] == "clarification"


@pytest.mark.asyncio
async def test_a_real_request_after_a_retry_runs_and_the_retry_is_silent(unit, monkeypatch):
    # P3: a retry is queued, then the owner says something new and the model delegates it.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    turn = s.delegations["d1"]["turn"]
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, fragment(" check the invoices", 300, 800, "real"), unit)      # after the retry's offset: held
    assert s.counters["input_held"] == 1
    assert voice_turn_state._states.get(voice_turn_state.key(s.tenant, s.user, turn)) is None
    await live.handle_event(s, delegation("real-event", "d3", offset=850), unit)
    assert s.delegations["d3"]["ambiguous"] is False and s.delegations["d3"]["retry"] is False
    assert s.delegations["d2"]["ambiguous"] is False
    assert voice_turn_state._states[voice_turn_state.key(s.tenant, s.user, turn)].status == "cancelled"
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "cancelled" and s.delegations["d3"]["state"] == "complete"
    assert questions == ["Check the diary.", "check the invoices"]          # the real request RAN
    assert s.delegations["d2"]["state"] == "clarification"
    heard = _texts_the_owner_could_hear(s)
    assert not any("one task instruction" in text or "did not receive" in text for text in heard)
    assert any(text.startswith("Stopped before anything ran") for text in heard)
    row = next(t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed")
    # the owner's words are waiting in the pool, but the real request queued behind the retry takes them,
    # so the retry has nothing to ask about
    assert row["retry"] is True and row["reason"] == "missing_context" and row["pending_fragments"] == 0


@pytest.mark.asyncio
async def test_a_cue_a_retry_and_a_real_request_do_not_refuse_each_other(unit, monkeypatch):
    # P10: a correction cue stands the turn down, a retry is raised, then the model delegates the correction.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, fragment("Wait, check the invoices", 300, 400, "cue"), unit)
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, delegation("real-event", "d3", offset=450), unit)
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    assert len([t for t in traces if t["outcome"] == "input_cancellation"]) == 1 and s.counters["input_cancel_repeat"] == 1
    assert questions == ["Check the diary.", "Wait, check the invoices"]
    heard = _texts_the_owner_could_hear(s)
    assert not any("one task instruction" in text or "did not receive" in text for text in heard)
    assert s.delegations["d2"]["state"] == "clarification" and s.delegations["d3"]["state"] == "complete"


@pytest.mark.asyncio
async def test_a_retry_raised_while_a_real_request_waits_does_not_make_it_ambiguous(unit, monkeypatch):
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); questions = []
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, fragment(" and the invoices", 110, 240, "new-words"), unit)
    await live.handle_event(s, delegation("real-event", "d2", offset=250), unit)          # anchored: stands d1 down
    await live.handle_event(s, delegation("duplicate-event", "d3", offset=100), unit)     # a duplicate inside the old window
    assert s.delegations["d2"]["ambiguous"] is False and s.delegations["d3"]["retry"] is True
    release.set(); await asyncio.gather(*list(s.jobs))
    assert questions == ["Check the diary.", "Check the diary. and the invoices"] and s.delegations["d2"]["state"] == "complete"
    assert not any("one task instruction" in text for text in _texts_the_owner_could_hear(s))


@pytest.mark.asyncio
async def test_under_any_words_a_re_delegation_is_answered_as_it_always_was(unit, monkeypatch):
    monkeypatch.setattr(live.settings, "gptlive_interrupt_rule", "any_words")
    s = session(); entered = asyncio.Event(); release = asyncio.Event()
    _slow_first_turn(monkeypatch, entered, release, then="answer")
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    assert s.delegations["d2"]["retry"] is False                # the retry marking is off with the rest of A5
    release.set(); await asyncio.gather(*list(s.jobs))
    heard = _texts_the_owner_could_hear(s)
    assert "I did not receive a complete instruction. Please repeat it." in heard


@pytest.mark.asyncio
async def test_a_retry_that_finds_unheard_speech_in_the_pool_is_not_silent(unit, monkeypatch):
    # P7: the retry was raised with nothing behind it, but the owner then said something that began
    # after the retry's offset: it anchors neither the deferral nor the retry. "Please repeat" is right.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, then="answer", questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, fragment(" and the invoices", 260, 400, "after-the-offset"), unit)
    assert voice_turn_state._states == {} and s.counters["input_held"] == 1
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "complete" and questions == ["Check the diary."]
    heard = _texts_the_owner_could_hear(s)
    assert heard.count("I did not receive a complete instruction. Please repeat it.") == 2       # status and commentary
    row = next(t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed")
    assert row["retry"] is True and row["reason"] == "missing_context" and row["pending_fragments"] == 1
    assert "invoices" not in json.dumps(traces)


@pytest.mark.asyncio
async def test_the_old_turns_own_late_tail_word_cannot_anchor_a_retry(unit, monkeypatch):
    # P2: a reordered tail word of the SAME request ("please", 90-150) lands after the turn's window
    # (0-100) closed. It crosses the window's end, so it is not a new request: nothing is cancelled, the
    # answer is spoken, and the retry neither runs it as a question nor speaks.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, then="answer", questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, fragment(" please", 90, 150, "late-own-word"), unit)
    assert voice_turn_state._states == {} and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs))
    assert questions == ["Check the diary."] and s.delegations["d1"]["state"] == "complete"
    assert _texts_the_owner_could_hear(s) == ["Fixture read only"]
    row = next(t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed")
    assert row["retry"] is True and row["pending_fragments"] == 0


@pytest.mark.asyncio
async def test_speech_starting_exactly_where_the_window_ends_does_anchor_a_retry(unit, monkeypatch):
    # contiguous fragments: a word that starts at the window's end is new speech, not a crossing tail
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); questions = []
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, fragment(" and the invoices", 100, 240, "contiguous"), unit)
    assert s.delegations["d1"]["cancel_requested"] is True
    release.set(); await asyncio.gather(*list(s.jobs))
    assert questions == ["Check the diary.", "Check the diary. and the invoices"] and s.delegations["d2"]["state"] == "complete"


@pytest.mark.asyncio
async def test_television_that_starts_before_the_offset_can_still_anchor_a_retry(unit, monkeypatch):
    # P4, accepted by the plan: the model raised a request during background speech whose audio began before
    # the delegation's offset but whose words arrived after it. The turn is stood down (deferred) and the
    # words are the retry's question. Pinned so that changing it is a decision, not an accident.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []; questions = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    _slow_first_turn(monkeypatch, entered, release, questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)
    await live.handle_event(s, fragment(" the weather tonight", 200, 700, "tv-before-offset"), unit)
    release.set(); await asyncio.gather(*list(s.jobs)); await s.diagnostic_job
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["deferred"] is True and row["event_id"] == "tv-before-offset"
    assert questions == ["Check the diary.", "Check the diary. the weather tonight"]      # carried: it was within the same breath
    # while speech that starts after the offset is only held
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); questions = []
    _slow_first_turn(monkeypatch, entered, release, then="answer", questions=questions)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words2"), unit)
    await live.handle_event(s, delegation("first-2", "e1", offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("retry-2", "e2", offset=250), unit)
    await live.handle_event(s, fragment(" the weather tonight", 300, 700, "tv-after-offset"), unit)
    assert s.counters["input_held"] == 1 and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs))
    assert questions == ["Check the diary."]


@pytest.mark.asyncio
@pytest.mark.parametrize("late,withheld", [((" and the invoices", 150, 300), True), ((" please", 90, 150), False)])
async def test_a_queued_delegation_anchored_after_the_turn_finished_still_withholds_its_commentary(unit, monkeypatch, late, withheld):
    # The queued half of superseded(): the turn has published its answer and is no longer "working", so
    # neither the anchored delegation (A5) nor the deferral can stand it down; this is what still holds
    # the commentary back. Speech that began inside the turn's window (the second case) does not count.
    s = session(); calls = []; original = live.authorized
    async def backend(ctx, question, history, turn, voice):
        return {"reply": "Three appointments."}
    async def authorized(session_, ctx):
        calls.append(1)
        await original(session_, ctx)
        if len(calls) == 4:      # settle loop, after admission, after the backend, then before the commentary
            assert s.delegations["d1"]["state"] == "complete"
            await live.handle_event(s, fragment(late[0], late[1], late[2], "late"), unit)
            await live.handle_event(s, delegation("newer", "d2", offset=350), unit)
    monkeypatch.setattr(live, "authorized", authorized)
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    said = [m for m in s.socket.sent if m["type"] == "session.commentary.append" and m["content"] == "Three appointments."]
    assert (not said) is withheld
    assert "cancel_requested" not in s.delegations["d1"]


def test_the_tail_cut_never_starts_mid_word():
    # "nonstop" cut after its "non" would read "stop" at the start of the tail
    text = "non" + "stop " + "b " * 1997 + "b"                      # 4003 characters: the cut would fall at "stop"
    assert len(text) == live.PENDING_CHARS + 3
    assert live.heard_cue([{"delta": text}]) is False
    assert live.tail_text([{"delta": text}]).startswith("b b")
    assert live.heard_cue([{"delta": "a " * 3000 + "stop"}]) is True       # a cue inside the tail is still seen
    assert live.heard_cue([{"delta": "x" * 4500}]) is False                # no whitespace to cut at: one long word
    assert live.tail_text([{"delta": "short text"}]) == "short text"


@pytest.mark.asyncio
async def test_a_delegation_with_nothing_running_still_asks_for_the_words(unit, monkeypatch):
    s = session()
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "clarification" and not s.delegations["d1"]["retry"]
    assert any("did not receive a complete instruction" in e.get("text", "") for e in s.events)


@pytest.mark.asyncio
async def test_a_delegation_inside_the_old_window_does_not(unit, monkeypatch):
    s = session(); entered = asyncio.Event(); release = asyncio.Event()
    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.handle_event(s, delegation("same-moment", "d2", offset=100), unit)      # not past the question
    assert not voice_turn_state._states and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "complete"


@pytest.mark.asyncio
async def test_a_delegation_inside_the_frozen_window_does_not_either(unit, monkeypatch):
    # Before consumed_ms has caught up with a freshly frozen window, "past consumed_ms" is not
    # enough: the delegation must also be past the working turn's own window.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); calls = []
    async def gate(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            entered.set(); await release.wait()
    async def backend(ctx, question, history, turn, voice):
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "authorized", gate)
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    assert s.delegations["d1"]["state"] == "working" and s.consumed_ms == -1 and s.delegations["d1"]["context_end"] == 100
    await live.handle_event(s, delegation("inside", "d2", offset=90), unit)
    assert not voice_turn_state._states and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "complete"


@pytest.mark.asyncio
async def test_any_words_rule_restores_the_previous_behaviour(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    state = working_turn(s)
    job = s.delegations["working-turn"]
    await live.handle_event(s, fragment("the weather", 110, 200, "tv"), unit)
    assert state.status == "running"                                  # the default rule holds it
    assert live.superseded(s, job, 100) is False                      # ...and does not hide a result
    monkeypatch.setattr(live.settings, "gptlive_interrupt_rule", "any_words")
    assert live.superseded(s, job, 100) is True                       # the old rule: any later word hides it
    await live.handle_event(s, fragment(" tonight", 210, 300, "tv2"), unit)
    await s.diagnostic_job
    assert state.status == "cancelled"
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["cue"] is False and row["trigger_type"] == "transcript" and row["event_id"] == "tv2"
    # the old rule had no delegation cancel: the words themselves did it
    s = session(); s.consumed_ms = 100
    state = working_turn(s, "second-turn")
    await live.handle_event(s, delegation(offset=250), unit)
    assert state.status == "running"
    await asyncio.gather(*list(s.jobs))


def test_the_interrupt_rule_setting_has_two_values_and_defaults_to_cue_words(monkeypatch, caplog):
    # It is the rollback switch, so a typo must never stop the app starting: it is trimmed and
    # lower-cased, and anything else logs a warning and uses cue_words.
    import logging
    from app.config import Settings
    monkeypatch.delenv("GPTLIVE_INTERRUPT_RULE", raising=False)
    assert Settings(_env_file=None).gptlive_interrupt_rule == "cue_words"
    for given in ("any_words", "ANY_WORDS", " any_words", "Any_Words\n"):
        assert Settings(_env_file=None, gptlive_interrupt_rule=given).gptlive_interrupt_rule == "any_words", given
    for given in ("cue_words", "CUE_WORDS", " cue_words ", "", None):
        assert Settings(_env_file=None, gptlive_interrupt_rule=given).gptlive_interrupt_rule == "cue_words", given
    assert "GPTLIVE_INTERRUPT_RULE" not in caplog.text                  # valid values and blanks are silent
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert Settings(_env_file=None, gptlive_interrupt_rule="some_words").gptlive_interrupt_rule == "cue_words"
    assert "some_words" in caplog.text and "using cue_words" in caplog.text
    monkeypatch.setenv("GPTLIVE_INTERRUPT_RULE", " ANY_WORDS ")
    assert Settings(_env_file=None).gptlive_interrupt_rule == "any_words"
    monkeypatch.setenv("GPTLIVE_INTERRUPT_RULE", "typo")
    assert Settings(_env_file=None).gptlive_interrupt_rule == "cue_words"


# ── The settle wait: how long the owner may be silent inside one request before it is taken as complete ──────

def test_the_settle_setting_defaults_to_a_second_and_is_set_from_the_environment(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("GPTLIVE_SETTLE_MS", raising=False)
    assert Settings(_env_file=None).gptlive_settle_ms == 1000
    monkeypatch.setenv("GPTLIVE_SETTLE_MS", "1500")
    assert Settings(_env_file=None).gptlive_settle_ms == 1500


@pytest.mark.parametrize("raw", ["abc", "", "  ", "1s", "1.5", "2e3", "one second", "None"])   # 2e3 is not parsed: 1000 is the default
def test_a_malformed_settle_setting_does_not_stop_the_app(monkeypatch, caplog, raw):
    """An operator editing the environment at midnight must not be able to stop the app starting."""
    from app.config import Settings
    monkeypatch.setenv("GPTLIVE_SETTLE_MS", raw)
    with caplog.at_level("WARNING"):
        assert Settings(_env_file=None).gptlive_settle_ms == 1000
    assert any("GPTLIVE_SETTLE_MS" in record.getMessage() and "using 1000" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("raw,expected", [("100", 300), ("0", 300), ("-5", 300), ("299", 300), ("2501", 2500), ("99999", 2500)])
def test_an_out_of_range_settle_setting_is_clamped_with_a_warning(monkeypatch, caplog, raw, expected):
    from app.config import Settings
    monkeypatch.setenv("GPTLIVE_SETTLE_MS", raw)
    with caplog.at_level("WARNING"):
        assert Settings(_env_file=None).gptlive_settle_ms == expected
    assert any("GPTLIVE_SETTLE_MS" in record.getMessage() and f"using {expected}" in record.getMessage()
               for record in caplog.records)


@pytest.mark.parametrize("raw,expected", [("1000", 1000), (" 800 ", 800), ("300", 300), ("2500", 2500), ("600", 600)])
def test_a_sensible_settle_setting_is_read_as_given_and_not_complained_about(monkeypatch, caplog, raw, expected):
    from app.config import Settings
    monkeypatch.setenv("GPTLIVE_SETTLE_MS", raw)
    with caplog.at_level("WARNING"):
        assert Settings(_env_file=None).gptlive_settle_ms == expected
    assert not [r for r in caplog.records if "GPTLIVE_SETTLE_MS" in r.getMessage()]


@pytest.mark.parametrize("configured,expected", [(1000, 1.0), (600, .6), (2500, 2.5), (300, .3), (0, 0.0), (-5, 0.0),
                                                  (4000, 4.0)])     # not capped at the 3 s wait: the whole wait grows with it
def test_the_settle_wait_follows_the_setting(monkeypatch, configured, expected):
    monkeypatch.setattr(live.settings, "gptlive_settle_ms", configured)
    assert live.settle_seconds() == expected


@pytest.mark.parametrize("configured,expected", [(0, 3), (300, 3), (600, 3), (1000, 3.4), (2000, 4.4), (2500, 4.9)])
def test_the_whole_wait_keeps_its_headroom_for_trailing_words_at_any_setting(monkeypatch, configured, expected):
    # The words of a request often reach the server after the delegation that asks for them, and the settle runs
    # from the last word's arrival: the whole wait is the settle plus the headroom when that is more than 3 s.
    monkeypatch.setattr(live.settings, "gptlive_settle_ms", configured)
    assert live.CONTEXT_WAIT == 3 and live.TRAILING_WORDS_HEADROOM == 2.4          # no unit fixture here: the real constants
    assert live.context_wait_seconds() == pytest.approx(expected)
    assert live.context_wait_seconds() - live.settle_seconds() >= live.TRAILING_WORDS_HEADROOM - 1e-9
    assert live.context_wait_seconds() >= live.settle_seconds()                     # the settle can never outlast the wait


def _the_real_waits(monkeypatch):
    """The unit fixture shortens the waits so a refusal is quick; the fake-clock tests need the real ones."""
    monkeypatch.setattr(live, "CONTEXT_WAIT", 3)
    monkeypatch.setattr(live, "TRAILING_WORDS_HEADROOM", 2.4)


class _FakeClock:
    """Time for app.gptlive only, moved by the test. The delegation wait reads this clock and its sleeps only yield,
    so a silence of an exact length can sit between the two halves of a request without any real waiting."""

    def __init__(self, monkeypatch):
        self.ticks = 0
        clock, real_time, real_asyncio = self, live.time, live.asyncio

        class Time:
            def __getattr__(self, name):
                return getattr(real_time, name)

            def monotonic(self):
                return 1000.0 + clock.ticks / 10

        class Asyncio:
            def __getattr__(self, name):
                return getattr(real_asyncio, name)

            async def sleep(self, delay, *args):
                await real_asyncio.sleep(0)
        monkeypatch.setattr(live, "time", Time())
        monkeypatch.setattr(live, "asyncio", Asyncio())

    async def advance(self, seconds):
        for _ in range(round(seconds * 10)):
            self.ticks += 1
            for _ in range(5):
                await asyncio.sleep(0)

    async def advance_to(self, seconds):
        await self.advance(round(seconds * 10) / 10 - self.ticks / 10)


JOINED = "Can you just bin it Or change it to two hundred quid"
# What the owner does, as (seconds on the fake clock, "fragment", text, start_ms, end_ms, event id) or
# (seconds, "delegation", offset_ms): the first half, the model raising the request, then the rest.
_FIRST_HALF = (0, "fragment", "Can you just bin it", 0, 700, "first-half")
_RAISED = (0, "delegation", 700)


def _second_half(at):
    return (at, "fragment", " Or change it to two hundred quid", 1700, 2500, "second-half")


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_ms,events,question,consumed", [
    # rig N20 (15 Oct): a 0.9 s pause inside one request is absorbed by the default ...
    (None, [_FIRST_HALF, _RAISED, _second_half(.9)], JOINED, 2500),
    # ... and was not by the old fixed 600 ms: the request went in as two halves
    (600, [_FIRST_HALF, _RAISED, _second_half(.9)], "Can you just bin it", 700),
    # a pause longer than the setting still splits it (the stated limit) ...
    (None, [_FIRST_HALF, _RAISED, _second_half(1.2)], "Can you just bin it", 700),
    # ... which a longer setting absorbs
    (1500, [_FIRST_HALF, _RAISED, _second_half(1.2)], JOINED, 2500),
    # every fragment restarts the settle clock (review MINOR-2): the delegation, then fragments at +0.2, +1.0 and
    # +1.8 s, 0.8 s apart. Measured from the delegation the wait would be over at 1.0 s with only the first.
    (1000, [(0, "delegation", 600), (.2, "fragment", "Check the diary for Tom.", 0, 600, "one"),
            (1.0, "fragment", " and Leah", 700, 1100, "two"), (1.8, "fragment", " on Friday", 1200, 1700, "three")],
     "Check the diary for Tom. and Leah on Friday", 1700),
], ids=["default-absorbs-a-0.9s-pause", "600-does-not", "default-splits-a-1.2s-pause", "1500-absorbs-a-1.2s-pause",
        "every-fragment-restarts-the-settle"])
async def test_a_pause_inside_one_request_is_absorbed_only_if_the_settle_wait_outlasts_it(
        unit, monkeypatch, settle_ms, events, question, consumed):
    from app.config import Settings
    monkeypatch.delenv("GPTLIVE_SETTLE_MS", raising=False)
    monkeypatch.setattr(live.settings, "gptlive_settle_ms",
                        Settings(_env_file=None).gptlive_settle_ms if settle_ms is None else settle_ms)
    _the_real_waits(monkeypatch)
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    clock = _FakeClock(monkeypatch)
    s = session(); seen = []

    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    for at, kind, *what in events:
        await clock.advance_to(at)                            # the owner draws breath, then speaks or the model delegates
        if kind == "fragment":
            text, start, end, event_id = what
            await live.handle_event(s, fragment(text, start, end, event_id), unit)
        else:
            await live.handle_event(s, delegation(offset=what[0]), unit)
    for _ in range(60):
        if not s.jobs:
            break
        await clock.advance(.1)
    await asyncio.wait_for(asyncio.gather(*list(s.jobs)), 10)
    assert seen == [question] and s.consumed_ms == consumed and s.delegations["d1"]["state"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_ms,lag,admitted", [
    (600, .8, True), (1000, .8, True), (1000, 1.9, True), (1000, 2.2, True), (1000, 2.6, False),
    (2000, .8, True), (2000, 1.1, True), (2000, 2.2, True), (2500, .4, True), (2500, .8, True), (2500, 2.2, True),
    (2500, 2.6, False),
])
async def test_the_words_may_trail_their_delegation_by_the_wait_minus_the_settle(unit, monkeypatch, settle_ms, lag, admitted):
    # The review's probe P3. The words of a request often reach the server after the delegation that asks for them
    # (transcript lag, about 0.8 s at the median) and the settle runs from the last word's arrival, so the words must
    # arrive within (the whole wait minus the settle) of the delegation. That is 2.4 s at any setting, because the
    # whole wait grows with the settle; with a fixed 3 s a setting of 2000 failed at 1.1 s and 2500 at 0.8 s.
    _the_real_waits(monkeypatch)
    monkeypatch.setattr(live.settings, "gptlive_settle_ms", settle_ms)
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    clock = _FakeClock(monkeypatch)
    s = session(); seen = []

    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, delegation(offset=700), unit)               # the model raises the request first ...
    await clock.advance(lag)
    await live.handle_event(s, fragment("Check the diary for Tom.", 0, 700, "trailing"), unit)   # ... its words trail it
    for _ in range(80):
        if not s.jobs:
            break
        await clock.advance(.1)
    await asyncio.wait_for(asyncio.gather(*list(s.jobs)), 10)
    if admitted:
        assert seen == ["Check the diary for Tom."] and s.delegations["d1"]["state"] == "complete"
    else:
        assert seen == [] and s.delegations["d1"]["state"] == "clarification"
        assert "I did not receive a complete instruction. Please repeat it." in [e.get("text") for e in s.events]


@pytest.mark.asyncio
async def test_the_n20_sentence_runs_as_one_question_with_clean_history(unit, monkeypatch):
    # The PR review's probe P1: a pause past the settle splits the request, the first half runs alone, and the model
    # re-delegates the second half. The backend must get the whole sentence, and no history telling it to ignore the
    # first half (the carry); it used to get "Or change it to two hundred quid" alone.
    from app.config import Settings
    monkeypatch.delenv("GPTLIVE_SETTLE_MS", raising=False)
    monkeypatch.setattr(live.settings, "gptlive_settle_ms", Settings(_env_file=None).gptlive_settle_ms)
    _the_real_waits(monkeypatch)
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    clock = _FakeClock(monkeypatch)
    s = session(); calls = []; traces = []; entered = asyncio.Event(); release = asyncio.Event()
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))

    async def backend(ctx, question, history, turn, voice):
        calls.append((question, [dict(message) for message in history]))
        if len(calls) == 1:
            entered.set(); await release.wait()
            raise live.BackendCancelled("Business turn cancelled")
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    first = "I think I put the wrong amount on one of Tom's invoices, a draft one. Can you just bin it"
    await live.handle_event(s, fragment(first, 0, 5000, "first-half"), unit)
    await live.handle_event(s, delegation(offset=5000), unit)
    for _ in range(40):                        # the first half goes out alone: the owner was silent for the settle
        if entered.is_set():
            break
        await clock.advance(.1)
    await asyncio.wait_for(entered.wait(), 5)
    await clock.advance(.2)                    # a pause of 1.2 s in all, then the second half
    await live.handle_event(s, fragment(" Or change it to two hundred quid", 6200, 7500, "second-half"), unit)
    await live.handle_event(s, delegation("second-delegation", "d2", offset=7500), unit)
    release.set()
    for _ in range(60):
        if not s.jobs:
            break
        await clock.advance(.1)
    await asyncio.wait_for(asyncio.gather(*list(s.jobs)), 10)
    whole = first + " Or change it to two hundred quid"
    assert [question for question, history in calls] == [first, whole]
    assert calls[1][1] == [] and s.carry == {}
    assert s.history == [{"role": "user", "content": whole}, {"role": "assistant", "content": "Fixture read only"}]
    assert s.delegations["d1"]["state"] == "cancelled" and s.delegations["d2"]["state"] == "complete"


@pytest.mark.asyncio
async def test_the_cue_check_has_a_cost_ceiling_per_event(unit):
    # The provider may send 2,000 characters with no spaces and the pool keeps 256 fragments: the
    # check must not join and strip all of it, once per working turn, for every fragment.
    import time
    best = None
    for run in range(3):
        s = session(); s.consumed_ms = 100
        for turn in range(4):
            working_turn(s, f"fence-{run}-{turn}")
        started = time.perf_counter(); done = 0
        for i in range(256):
            await live.handle_event(s, fragment("a" * 2000, 110 + i * 10, 119 + i * 10, f"f{run}-{i}"), unit)
            done += 1
            if time.perf_counter() - started > 8.0:        # far over budget already: stop early
                break
        per_event = (time.perf_counter() - started) / done
        best = per_event if best is None else min(best, per_event)
    assert best < 0.05, f"{best * 1000:.1f} ms per event"


@pytest.mark.asyncio
async def test_one_event_joins_the_pending_speech_once_however_many_turns_are_working(unit, monkeypatch):
    s = session(); s.consumed_ms = 100; joins = []
    real = live.tail_text
    monkeypatch.setattr(live, "tail_text", lambda parts: (joins.append(len(parts)), real(parts))[1])
    for turn in range(5):
        working_turn(s, f"memo-{turn}")
    await live.handle_event(s, fragment("the weather", 110, 200, "tv"), unit)
    assert joins == [1]
    assert s.counters["input_held"] == 5


def test_the_cue_is_judged_on_the_recent_tail_only():
    plain = [{"delta": f" w{i}"} for i in range(40)]
    assert live.heard_cue([{"delta": "stop"}] + plain) is False                  # scrolled out: it was acted on when it arrived
    assert live.heard_cue(plain[:20] + [{"delta": " stop"}] + plain[20:30]) is True
    assert live.heard_cue([{"delta": "x" * 5000 + " stop"}]) is True
    assert live.heard_cue([{"delta": "stop " + "x" * 5000}]) is False


@pytest.mark.asyncio
async def test_pending_words_count_the_judged_tail_and_fragments_count_everything(unit, monkeypatch):
    s = session(); traces = []; s.consumed_ms = 100
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    working_turn(s)
    for i in range(40):
        await live.handle_event(s, fragment(f" w{i}", 110 + i * 10, 119 + i * 10, f"w{i}"), unit)
    await live.handle_event(s, fragment(" stop", 510, 519, "cue"), unit)
    await s.diagnostic_job
    row = next(t["detail"] for t in traces if t["outcome"] == "input_cancellation")
    assert row["pending_fragments"] == 41 and row["pending_words"] == live.PENDING_FRAGMENTS
    assert row["pending_span_ms"] == 519 - 110 and row["since_question_ms"] == 519 - 100


@pytest.mark.asyncio
async def test_a_cue_that_scrolled_out_of_the_tail_still_withholds_the_finished_answer(unit, monkeypatch):
    s = session()
    async def backend(ctx, question, history, turn, voice):
        await live.handle_event(s, fragment("Wait, stop.", 110, 200, "cue"), unit)
        for i in range(live.PENDING_FRAGMENTS + 8):
            await live.handle_event(s, fragment(f" w{i}", 210 + i * 10, 219 + i * 10, f"w{i}"), unit)
        return {"reply": "Three appointments."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["cancel_requested"] is True
    assert not [e for e in s.events if e["kind"] == "result"]
    assert any("newer words take priority" in e.get("text", "") for e in s.events)


@pytest.mark.asyncio
async def test_a_finished_result_is_spoken_despite_background_words(unit, monkeypatch):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    async def backend(ctx, question, history, turn, voice):
        # talk in the room while the backend works: heard, but not a correction and not a new request
        await live.handle_event(s, fragment("the weather tonight will be mostly dry", 110, 900, "tv"), unit)
        return {"reply": "Three appointments."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "complete" and s.counters["input_held"] == 1
    assert [e for e in s.events if e["kind"] == "result"]
    assert not any("newer words take priority" in e.get("text", "") for e in s.events)
    said = [m for m in s.socket.sent if m["type"] == "session.commentary.append"]
    assert len(said) == 1 and said[0]["content"] == "Three appointments." and said[0]["delegation_id"] == "d1"


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["cue", "delegation"])
async def test_a_finished_result_is_withheld_after_a_cue_or_a_newer_delegation(unit, monkeypatch, newer):
    s = session()
    async def backend(ctx, question, history, turn, voice):
        if newer == "cue":
            await live.handle_event(s, fragment("Actually, Thursday", 110, 900, "correction"), unit)
        else:
            # a new request: words the model anchored on, then the delegation (without words it is a retry)
            await live.handle_event(s, fragment("and the invoices", 110, 900, "new-words"), unit)
            await live.handle_event(s, delegation("newer-event", "d2", offset=1200), unit)
        return {"reply": "Three appointments."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert not [e for e in s.events if e["kind"] == "result"]
    assert any("newer words take priority" in e.get("text", "") for e in s.events)
    assert not [m for m in s.socket.sent if m["type"] == "session.commentary.append" and m["content"] == "Three appointments."]


@pytest.mark.asyncio
async def test_the_commentary_is_not_sent_if_a_cue_arrives_after_the_result_was_published(unit, monkeypatch):
    # the second hold-back check, between publishing the result and sending the commentary
    s = session(); calls = []
    original = live.authorized
    async def backend(ctx, question, history, turn, voice):
        return {"reply": "Three appointments."}
    async def authorized(session_, ctx):
        calls.append(1)
        await original(session_, ctx)
        if len(calls) == 4:      # settle loop, after admission, after the backend, then before the commentary
            assert [e for e in s.events if e["kind"] == "result"]
            await live.handle_event(s, fragment("Wait, stop.", 110, 900, "late"), unit)
    monkeypatch.setattr(live, "authorized", authorized)
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert len(calls) == 4 and s.delegations["d1"]["state"] == "complete"
    assert not [m for m in s.socket.sent if m["type"] == "session.commentary.append"]


@pytest.mark.asyncio
async def test_hot_sideband_slow_database_does_not_starve_poll_or_close_on_task_error(unit, monkeypatch):
    import time
    from app import concierge_routes as routes
    s=session();live._sessions[s.id]=s;counts={"scope":0,"tasks":0};ticks=[]
    def slow_scope(ctx):
        counts["scope"]+=1;time.sleep(.02);return "permissions"
    def slow_tasks(*args):
        counts["tasks"]+=1;time.sleep(.06)
        if counts["tasks"]==1:raise OSError("fixture database outage")
        return {"tasks":[]}
    monkeypatch.setattr(live,"scope",slow_scope)
    monkeypatch.setattr(routes,"session_task_snapshot",slow_tasks)
    monkeypatch.setattr(live,"MAINTENANCE_INTERVAL",.1)
    for _ in range(500):s.socket.queue.put_nowait(json.dumps({"type":"session.input_audio.append"}))
    async def tick():
        for _ in range(40):ticks.append(1);await asyncio.sleep(.005)
    s.reader=asyncio.create_task(live.read_sideband(s,unit));ticker=asyncio.create_task(tick())
    await asyncio.sleep(.08)
    response=await asyncio.wait_for(live.events(s.id,0,unit),.3)
    await ticker
    assert not s.closed and response["delivery"]=="native_playback_unconfirmed"
    assert len(ticks)==40 and counts["scope"]<15 and counts["tasks"]<5
    for _ in range(100):
        if s.counters.get("task_update_error"):break
        await asyncio.sleep(.01)
    assert s.counters["task_update_error"]==1
    await live.finish(s)
    assert s.finalized and s.socket.closed and s.id not in live._sessions


@pytest.mark.asyncio
async def test_native_three_turn_actual_bridge_crm_tool_proposal_confirm_and_receipt(unit, voice_client, monkeypatch):
    import time
    from app import concierge, concierge_routes as routes, concierge_data
    _,saved,metered=voice_client;store=Store();unit.client=store
    crm={"notes":"before"};lookups=[];effects=[];ticks=[]
    monkeypatch.setattr(routes,"_asker",lambda ctx:None)
    monkeypatch.setattr(routes,"_people_note",lambda *a:"")
    monkeypatch.setattr(routes,"_voice_note",lambda *a,**k:"")
    monkeypatch.setattr(routes.concierge_delegation,"context_note",lambda *a:"")
    monkeypatch.setattr(routes.concierge_tasks,"settle_card",lambda *a,**k:None)
    monkeypatch.setattr(routes.concierge_actions,"mailbox_refusal",lambda *a,**k:None)
    def lookup(*args):
        time.sleep(.06);lookups.append(crm["notes"]);return {"notes":crm["notes"]}
    monkeypatch.setitem(concierge_data._EXECUTORS,"search_contacts",lookup)
    def prepare(*args,**kwargs):
        store.row.update(kind="update_contact_field",payload={"field":"notes","value":"after"},status="pending")
        return {"proposal_id":store.row["id"],"kind":store.row["kind"],"summary":"Update fixture notes","expires_s":300}
    monkeypatch.setattr(routes.concierge_actions,"stage",prepare)
    async def execute(*args,**kwargs):
        effects.append(1);crm["notes"]="after";return {"summary":"Fixture record updated."}
    monkeypatch.setattr(routes.concierge_actions,"execute",execute)
    async def model(question,history,**kw):
        await kw["on_tool"]("search_contacts",{"query":"fixture"})
        value=await kw["tool_executor"]("search_contacts",{"query":"fixture"})
        action={"kind":"update_contact_field"} if question.startswith("Prepare") else None
        return concierge.BusinessReply(reply="Prepared for review." if action else "Fixture notes: "+value["notes"]+".",links=[],action=action,model="fixture",usage={})
    monkeypatch.setattr(routes.concierge,"answer_business",model)
    s=session();live._sessions[s.id]=s
    async def ticker():
        for _ in range(60):ticks.append(1);await asyncio.sleep(.005)
    ticker_task=asyncio.create_task(ticker())
    for i,question in enumerate(["Read fixture notes", "Prepare fixture notes change", "Read verified fixture notes"]):
        await live.handle_event(s,fragment(question,i*200,i*200+100,"input"+str(i)),unit)
        await live.handle_event(s,delegation("event"+str(i),"d"+str(i),i*200+100),unit)
        await asyncio.gather(*list(s.jobs))
        result=[e for e in s.events if e["kind"]=="result"][-1]["payload"]
        assert result["t"]=="done" and result["trail"][0]["name"]=="search_contacts"
        if i==1:
            assert result["proposal"]["proposal_id"]==store.row["id"]
            assert store.row["status"]=="pending" and crm["notes"]=="before" and not effects
            # Explicit visible-card endpoint, not a spoken affirmative or native event.
            outcome=await routes.business_execute(routes.BusinessExecuteRequest(proposal_id=store.row["id"]),unit)
            assert outcome.ok and store.row["status"]=="executed" and effects==[1]
            replay=await routes.business_execute(routes.BusinessExecuteRequest(proposal_id=store.row["id"]),unit)
            assert replay.ok and effects==[1]
    await ticker_task
    assert lookups==["before","before","after"] and len(saved)>=6 and len(metered)==3
    assert len(ticks)==60 and s.counters["backend_requested"]==3 and s.counters["result_published"]==3
    assert len([e for e in s.socket.sent if e["type"]=="session.commentary.append"])==3
    assert "after" in result["reply"] and store.row["result"]["execution"]["confirmed_by"]=="u1"


@pytest.mark.asyncio
async def test_exact_native_session_payload_and_private_server_key(unit, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url == "https://api.openai.com/v1/live/sessions"
        assert request.headers["authorization"] == "Bearer secret-do-not-disclose"
        payload = json.loads(request.content)
        assert payload["transport"] == {"type": "webrtc", "sdp": "v=offer"}
        assert payload["session"]["model"] == "gpt-live-1"
        assert payload["session"]["delegation"] == {"type": "client"}
        assert payload["session"]["store"] is False
        assert payload["session"]["client"]["data_channel"] == {
            "allowed_client_events": [], "allowed_server_events": [{"type": x} for x in live.SERVER_EVENTS]}
        return httpx.Response(201, json={"session": {"id": "live_exact_prefix"},
                                        "transport": {"type": "webrtc", "sdp": "v=answer"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await live.create_provider("v=offer") == ("live_exact_prefix", "v=answer")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_sideband_exact_url_auth_and_limits(unit, monkeypatch):
    async def connection(url, **kw):
        assert url == "wss://api.openai.com/v1/live/sessions/live_exact_prefix/attach"
        assert kw["additional_headers"] == {"Authorization": "Bearer secret-do-not-disclose"}
        assert kw["max_size"] == 262144 and kw["max_queue"] == 16
        return "socket"
    monkeypatch.setattr(live, "connect", connection)
    assert await live.attach("live_exact_prefix") == "socket"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_sdp", "attach", "cancelled"])
async def test_failed_creation_retains_id_and_closes_provider(unit, monkeypatch, failure):
    sock = Socket(); attempts = []
    async def create(sdp):
        return "live_known", "bad" if failure == "invalid_sdp" else "v=answer"
    async def attach(provider):
        attempts.append(provider)
        if len(attempts) == 1 and failure == "attach":
            raise OSError("unavailable")
        if len(attempts) == 1 and failure == "cancelled":
            raise asyncio.CancelledError()
        return sock
    monkeypatch.setattr(live, "create_provider", create)
    monkeypatch.setattr(live, "attach", attach)
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else HTTPException):
        await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert sock.closed and sock.sent[-1]["type"] == "session.close"
    assert not live._sessions and not live._creating


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_failed_creation_closes_cleanup_socket_when_send_fails(unit, monkeypatch, failure):
    sock = Socket(); creates = []; attaches = []
    async def create(sdp):
        creates.append(sdp)
        return "live_known", "invalid-answer"
    async def attach(provider):
        attaches.append(provider)
        return sock
    async def send(raw):
        if failure == "error":
            raise OSError("send failed")
        await asyncio.Event().wait()
    original = asyncio.wait_for
    async def short(awaitable, timeout):
        return await original(awaitable, .02)
    sock.send = send
    monkeypatch.setattr(live, "create_provider", create)
    monkeypatch.setattr(live, "attach", attach)
    monkeypatch.setattr(live.asyncio, "wait_for", short)
    with pytest.raises(HTTPException) as exc:
        await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert exc.value.status_code == 502
    assert sock.closed and creates == ["v=offer"] and attaches == ["live_known"]
    assert not live._sessions and not live._creating


@pytest.mark.asyncio
async def test_close_marker_covers_late_created_session(unit, monkeypatch):
    voice = uuid.uuid4(); entered = asyncio.Event(); release = asyncio.Event(); sock = Socket()
    async def create(sdp):
        entered.set(); await release.wait(); return "live_late", "v=answer"
    async def attach(provider):
        return sock
    monkeypatch.setattr(live, "create_provider", create)
    monkeypatch.setattr(live, "attach", attach)
    start = asyncio.create_task(live.create(live.Create(sdp="v=offer", voice_session=voice), unit))
    await entered.wait()
    result = await live.control(live.Control(session=str(voice), operation="close"), unit)
    assert result["ok"] and not result["final_usage_confirmed"]
    assert result["retry_allowed"] is False
    with pytest.raises(HTTPException) as busy:
        await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert busy.value.status_code == 409
    release.set()
    with pytest.raises(HTTPException) as exc:
        await start
    assert exc.value.status_code == 409 and sock.closed and not live._sessions
    settled = await live.control(live.Control(session=str(voice), operation="close"), unit)
    assert settled["retry_allowed"] is True


@pytest.mark.asyncio
async def test_startup_permission_read_does_not_block_event_loop(unit, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    def slow_scope(ctx):
        entered.set()
        assert release.wait(2), "Permission DB read blocked the event loop"
        return "permissions"
    async def provider(sdp):
        raise ValueError("offline fixture ends before provider connection")
    monkeypatch.setattr(live, "scope", slow_scope)
    monkeypatch.setattr(live, "create_provider", provider)
    pending = asyncio.create_task(live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # The loop must run this release while the synchronous scope read waits.
        release.set()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 502
    finally:
        release.set()


@pytest.mark.asyncio
async def test_startup_upstream_failure_records_only_safe_metadata(unit, monkeypatch):
    records = []
    async def provider(sdp):
        request = httpx.Request("POST", "https://provider.invalid/private?credential=SECRET", headers={"Authorization": "Bearer SECRET"})
        response = httpx.Response(503, request=request, text="PRIVATE BODY SECRET")
        response.raise_for_status()
    async def capture(*args, **kwargs):
        records.append(kwargs["detail"])
    monkeypatch.setattr(live, "create_provider", provider)
    monkeypatch.setattr(live, "safe_trace", capture)
    with pytest.raises(HTTPException) as error:
        await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert error.value.status_code == 502
    detail = records[-1]
    assert detail["phase"] == "provider" and detail["upstream_status"] == 503
    assert isinstance(detail["failed_stage_ms"], int) and detail["failed_stage_ms"] >= 0
    assert all(isinstance(v, int) and v >= 0 for v in detail["timings"].values())
    encoded = json.dumps(detail)
    assert not any(private in encoded for private in ("SECRET", "PRIVATE BODY", "provider.invalid", "Authorization", "v=offer"))


@pytest.mark.asyncio
async def test_owned_reader_close_drains_final_usage(unit):
    s = session(); live._sessions[s.id] = s; s.reader = asyncio.current_task()
    await live.finish(s)
    assert s.finalized and s.usage_seconds == 12 and s.socket.closed
    assert not live._sessions


@pytest.mark.asyncio
async def test_close_without_ack_is_bounded_and_unconfirmed(unit, monkeypatch):
    s = session(); s.reader = asyncio.current_task()
    async def silent(raw):
        s.socket.sent.append(json.loads(raw))
    s.socket.send = silent
    original = asyncio.wait_for
    async def short(awaitable, timeout):
        return await original(awaitable, .02)
    monkeypatch.setattr(live.asyncio, "wait_for", short)
    await live.finish(s)
    assert s.closed and s.socket.closed and not s.finalized


@pytest.mark.asyncio
async def test_duplicate_delegation_and_transcript_executes_once_after_reorder(unit, monkeypatch):
    s = session(); calls = []
    async def backend(ctx, question, history, turn, voice):
        calls.append((question, turn, voice))
        return {"t": "done", "reply": "Three appointments.", "links": []}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, delegation(), unit)
    await live.handle_event(s, delegation("duplicate-provider-event"), unit)
    await asyncio.sleep(.01)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, fragment(), unit)
    await asyncio.gather(*list(s.jobs))
    assert len(calls) == 1 and calls[0][0] == "Check the diary."
    assert len(s.fragments) == 1
    assert len([e for e in s.events if e["kind"] == "result"]) == 1
    result_event = [e for e in s.events if e["kind"] == "result"][0]
    assert result_event["turn_id"] == calls[0][1]
    assert s.socket.sent[-1]["delegation_id"] == "d1"
    assert "secret-do-not-disclose" not in json.dumps(s.events)


@pytest.mark.parametrize("words,offset", [
    ([fragment("Thursday and", 0, 101)], 100),
    ([fragment("Friday", 0, 50), fragment(" not Thursday and", 51, 110, "correction")], 100),
    ([fragment("Book", 0, 60), fragment(" tomorrow", 50, 90, "overlap")], 100),
    ([fragment("Book it and", 0, 90)], 100),
    ([fragment("yes", 0, 90)], 100),
])
def test_ambiguous_partial_correction_or_approval_never_becomes_instruction(unit, words, offset):
    s = session(); s.fragments = [{k: v for k, v in f.items() if k != "type"} for f in words]
    s.delegations["d1"] = {"state": "queued"}
    with pytest.raises(ValueError):
        live.snapshot(s, {"offset": offset})


def _tagged_window(*words):
    s = session()
    s.fragments = [{k: v for k, v in f.items() if k != "type"} for f in words]
    s.delegations["d1"] = {"state": "queued"}
    return s


@pytest.mark.parametrize("text,reason", [
    ("[cough] Yeah", "approval_requires_card"),
    ("[cough Yeah", "approval_requires_card"),
    ("Check the diary and [cough]", "incomplete_text"),
    ("[inhale] [cough]", "incomplete_text"),
    ("[chuckle", "incomplete_text"),
])
def test_non_speech_tags_are_stripped_before_the_approval_and_incomplete_checks(unit, text, reason):
    s = _tagged_window(fragment(text, 0, 90))
    with pytest.raises(live.ContextUnconfirmed) as e:
        live.snapshot(s, {"offset": 100})
    assert e.value.reason == reason


@pytest.mark.parametrize("text,expected", [
    ("Check the diary [coug", "Check the diary"),
    ("ough] Check the diary", "Check the diary"),
    ("[cough Yeah, send both", "Yeah, send both"),
    ("[inhale] [cough] Check the diary", "Check the diary"),
    ("Can you check my outstanding invoices please", "Can you check my outstanding invoices please"),
])
def test_dangling_tag_pieces_are_stripped_and_plain_text_is_unchanged(unit, text, expected):
    s = _tagged_window(fragment(text, 0, 90))
    assert live.snapshot(s, {"offset": 100}) == (expected, 90)


@pytest.mark.asyncio
async def test_split_non_speech_tags_do_not_reach_the_backend_or_the_chat(unit, monkeypatch):
    # the provider splits a tag across fragments (" [inhale" | "] [c" | "ough] ")
    s = session(); seen = []
    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    for i, (text, start, end) in enumerate([("[inhale", 0, 20), ("] [c", 21, 40),
                                            ("ough] ", 41, 60), ("Check the diary", 61, 110)]):
        await live.handle_event(s, fragment(text, start, end, f"tag-{i}"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*s.jobs)
    assert seen == ["Check the diary"]
    assert s.consumed_ms == 110 and s.delegations["d1"]["state"] == "complete"
    assert [e["text"] for e in s.events if e["kind"] == "question"] == ["Check the diary"]
    assert [m["content"] for m in s.history if m["role"] == "user"] == ["Check the diary"]
    assert s.fragments[0]["delta"] == "[inhale"        # the archive keeps what the provider sent


@pytest.mark.asyncio
async def test_missing_context_and_two_requests_ask_clarification_without_backend(unit, monkeypatch):
    s = session()
    async def forbidden(*a):
        raise AssertionError("No instruction should execute")
    monkeypatch.setattr(live, "run_backend", forbidden)
    await live.handle_event(s, delegation(), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "clarification"
    assert not any(e["kind"] == "result" for e in s.events)
    s = session(); await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await live.handle_event(s, delegation("two", "d2"), unit)
    await asyncio.gather(*list(s.jobs))
    assert all(j["state"] == "clarification" for j in s.delegations.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_new_trusted_input_uses_c1_precommit_cancel_boundary(unit, monkeypatch, committed):
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); effects = []
    async def backend(ctx, question, history, turn, voice):
        current = voice_turn_state.begin(voice_turn_state.key(ctx.tenant_id, ctx.user_id, turn))
        if committed:
            voice_turn_state.commit(current, "entered_write")
        entered.set(); await release.wait()
        voice_turn_state.checkpoint(current)
        effects.append("existing-authorized-effect")
        return {"t": "done", "reply": "Backend result."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await entered.wait()
    await live.handle_event(s, fragment("Thursday, not Friday.", 110, 200, "correction"), unit)
    release.set(); await asyncio.gather(*list(s.jobs))
    assert effects == (["existing-authorized-effect"] if committed else [])
    assert not any(e["kind"] == "result" for e in s.events)
    assert s.history[0]["content"] == "Check the diary."


@pytest.mark.asyncio
async def test_permissions_changed_during_backend_prevents_disclosure(unit, monkeypatch):
    s = session(); permission = ["permissions"]
    monkeypatch.setattr(live, "scope", lambda ctx: permission[0])
    async def backend(*a):
        permission[0] = "revoked"
        return {"reply": "Private customer data"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await asyncio.gather(*list(s.jobs))
    assert not any(e["kind"] == "result" for e in s.events)
    assert all("Private customer" not in json.dumps(e) for e in s.socket.sent)


@pytest.mark.asyncio
async def test_append_ack_and_error_never_mark_delivery_or_retry_business(unit):
    s = session(); await s.send("session.commentary.append", "Verified result.", "d1")
    command = s.socket.sent[-1]["event_id"]
    await live.handle_event(s, {"type": "session.commentary.appended", "event_id": "ack", "client_event_id": command}, unit)
    assert s.commands[command]["state"] == "accepted_context_not_playback"
    await live.handle_event(s, {"type": "error", "event_id": "error", "error": {"client_event_id": command}}, unit)
    assert s.commands[command]["state"] == "unconfirmed"
    assert len(s.socket.sent) == 1 and not s.delegations


@pytest.mark.asyncio
async def test_native_task_update_fresh_revision_once_and_no_delivery_ack(unit, monkeypatch):
    from app import concierge_routes as routes
    s = session()
    row = {"voice_session": s.voice_session, "id": "task", "revision": 1, "event_id": "event1", "speakable": True,
           "outcome": "Prepared for approval. No change executed."}
    def tasks(voice, ctx):
        return {"tasks": [dict(row)]}
    monkeypatch.setattr(routes, "session_task_snapshot", tasks)
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda ctx, id: dict(row))
    monkeypatch.setattr(live.concierge_delegation, "public", lambda row: row)
    def forbidden(*a, **kw):
        raise AssertionError("Native output is not a C3 delivery ACK")
    monkeypatch.setattr(live.concierge_delegation, "transition", forbidden)
    await live.task_updates(s, unit); await live.task_updates(s, unit)
    assert len(s.socket.sent) == 1 and s.socket.sent[0]["type"] == "session.commentary.append"
    assert s.events[0]["delivery"] == "native_playback_unconfirmed"
    row.update(revision=2, event_id="event2", outcome="Lookup found one appointment. No change executed.")
    await live.task_updates(s, unit); assert len(s.socket.sent) == 2
    row.update(event_id="event3")
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda ctx, id: {**row, "event_id": "newer"})
    await live.task_updates(s, unit); assert len(s.socket.sent) == 2


@pytest.mark.asyncio
async def test_stream_bridge_reuses_existing_voice_chat_and_ignores_speak_chunks(unit, monkeypatch):
    from app import concierge_routes as routes
    from fastapi.responses import StreamingResponse
    calls = []
    async def chat(body, ctx):
        calls.append((body, ctx))
        async def frames():
            yield 'data: {"t":"speak","text":"Unfinished."}\n\n'
            yield 'data: {"t":"done","reply":"Final scoped reply","proposal":{"proposal_id":"current"}}\n\n'
        return StreamingResponse(frames())
    monkeypatch.setattr(routes, "voice_chat", chat)
    result = await live.run_backend(unit, "Check diary", [], "turn", str(uuid.uuid4()))
    assert result["reply"] == "Final scoped reply"
    assert calls[0][0].turn_id == "turn" and calls[0][1] is unit


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_discard_reports_actual_c1_outcome_for_captured_turn(unit, committed):
    s = session(); live._sessions[s.id] = s
    s.delegations["old"] = {"turn": "old", "state": "working"}
    s.delegations["current"] = {"turn": "current", "state": "working"}
    state = voice_turn_state.begin(voice_turn_state.key("t1", "u1", "old"))
    if committed:
        voice_turn_state.commit(state, "business_effect")
    result = await live.control(live.Control(session=s.id, operation="discard", turn_id="old"), unit)
    assert result["turns"][0]["status"] == ("too_late" if committed else "cancelled")
    assert len(result["turns"]) == 1
    assert voice_turn_state.key("t1", "u1", "current") not in voice_turn_state._states


def test_create_requires_auth_csrf_server_owned_config_and_feature_gate(client, unit, monkeypatch):
    from app.main import app
    from app.security import require_csrf_header
    monkeypatch.setattr(live.settings, "gptlive_enabled", False)
    body = {"sdp": "v=offer", "voice_session": str(uuid.uuid4())}
    assert client.post('/concierge/voice/gptlive/session', json=body).status_code == 503
    assert client.post('/concierge/voice/gptlive/session', json={**body, "model": "other"}).status_code == 422
    del app.dependency_overrides[require_csrf_header]
    assert client.post('/concierge/voice/gptlive/session', json=body).status_code == 403
    app.dependency_overrides.clear()
    assert client.post('/concierge/voice/gptlive/session', json=body, follow_redirects=False).status_code in (303, 307, 401, 403)


def test_wrong_owner_cannot_read_or_control_provider_session(client, unit):
    s = session(); s.user = "someone-else"; live._sessions[s.id] = s
    assert client.get('/concierge/voice/gptlive/events/owned').status_code == 404
    assert client.post('/concierge/voice/gptlive/control', json={"session": "owned", "operation": "close"}).status_code == 404


def test_actual_native_browser_deferred_races_and_mode_selector():
    import pathlib, subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "tests/replays/gptlive_replay.mjs"], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_opaque_provider_id_preserved_and_encoded_only_in_attach_path(unit, monkeypatch):
    provider = "live:opaque/prefix+suffix?x=" + "x" * 180
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={"session": {"id": provider}, "transport": {"sdp": "v=answer"}})), **kw))
    result, answer = await live.create_provider("v=offer")
    assert result == provider
    async def connect(url, **kw):
        assert "live%3Aopaque%2Fprefix%2Bsuffix%3Fx%3D" in url and url.endswith("/attach")
        return "socket"
    monkeypatch.setattr(live, "connect", connect)
    assert await live.attach(result) == "socket"


@pytest.mark.asyncio
async def test_visible_card_settlement_releases_native_task_updates(unit, monkeypatch):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"proposal": "proposal"}; state = ["pending"]
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: {"status": state[0]})
    row = {"voice_session": s.voice_session, "id": "task", "revision": 1, "event_id": "completed", "speakable": True, "outcome": "Lookup complete. No change executed."}
    def tasks(*a):
        return {"tasks": [row]}
    monkeypatch.setattr(routes, "session_task_snapshot", tasks)
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda *a: row)
    monkeypatch.setattr(live.concierge_delegation, "public", lambda row: row)
    await live.task_updates(s, unit); assert not s.socket.sent
    state[0] = "completed"
    await live.task_updates(s, unit)
    assert s.socket.sent[-1]["type"] == "session.commentary.append" and not s.approval


def _settled(status, **result):
    row = {"id": "p1", "status": status, "kind": "create_followup", "payload": {}}
    if result:
        row["result"] = execution_result("p1", state="completed" if status == "executed" else status, **result)
    return row


def _proposing_history():
    return [{"role": "user", "content": "Block Friday"},
            {"role": "assistant", "content": "Ready — tap Confirm.\n\n[Proposed: Block the diary on 16th Oct]"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("row,spoken", [
    (_settled("executed", summary="Follow-up created for Sam, due Friday 16 Oct."),
     "The card was confirmed and it went through: Follow-up created for Sam, due Friday 16 Oct."),
    (_settled("failed", summary="That slot filled up in the meantime."),
     "The card was confirmed but the change did not go through: That slot filled up in the meantime."),
    ({"id": "p1", "status": "cancelled"}, "The card was cancelled; nothing changed."),
    ({"id": "p1", "status": "superseded"}, "That card was replaced by a newer one; nothing from it happened."),
    ({"id": "p1", "status": "expired"}, "That card expired unanswered; nothing happened. It is in the waiting list."),
])
async def test_a_settled_card_is_told_to_the_voice_once_and_lands_in_history(unit, monkeypatch, row, spoken):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"proposal": "p1"}; s.history = _proposing_history()
    traced = []
    async def capture(*a, **k):
        traced.append(k)
    monkeypatch.setattr(live, "safe_trace", capture)
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: row)
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    await live.task_updates(s, unit)
    sent = [e for e in s.socket.sent if e["type"] == "session.commentary.append"]
    assert [e["content"] for e in sent] == [spoken]
    assert not s.approval
    # The next delegated turn reads the outcome beside the reply that proposed it.
    assert s.history[-1]["content"].startswith("Ready — tap Confirm.\n\n[Proposed: Block the diary on 16th Oct]")
    assert s.history[-1]["content"].endswith("\n\n[Card outcome: " + spoken + "]")
    assert s.history[0] == {"role": "user", "content": "Block Friday"}
    # The trace says that it happened and which kind of thing settled, never the words.
    stages = [k["detail"] for k in traced if k.get("outcome") == "card_outcome_sent"]
    assert len(stages) == 1 and stages[0]["kind"] == "proposal"
    assert "Follow-up" not in json.dumps(stages) and "diary" not in json.dumps(stages)
    # Once only: the approval was cleared before the send.
    await live.task_updates(s, unit)
    assert len([e for e in s.socket.sent if e["type"] == "session.commentary.append"]) == 1
    assert len([k for k in traced if k.get("outcome") == "card_outcome_sent"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("socket down"), "closed"])
async def test_a_receipt_whose_append_failed_is_not_traced_as_sent(unit, monkeypatch, failure):
    from app import db, concierge_routes as routes
    from websockets.exceptions import ConnectionClosed
    s = session(); s.approval = {"proposal": "p1"}; s.history = _proposing_history()
    boom = ConnectionClosed(None, None) if failure == "closed" else failure
    async def refuse(raw):
        raise boom
    s.socket.send = refuse
    traced = []
    async def capture(*a, **k):
        traced.append(k)
    monkeypatch.setattr(live, "safe_trace", capture)
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: {"id": "p1", "status": "cancelled"})
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    expected = ConnectionClosed if failure == "closed" else live.TaskNoticeFailure
    with pytest.raises(expected):
        await live.task_updates(s, unit)
    # Nothing was heard, so nothing says it was: the approval is still released before the
    # send (as before), but the stage is recorded only after the append went out.
    assert not [k for k in traced if k.get("outcome") == "card_outcome_sent"]
    assert not s.approval


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "executing", "unknown"])
async def test_a_card_still_in_flight_says_nothing_and_stays_tracked(unit, monkeypatch, status):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"proposal": "p1"}; s.history = _proposing_history()
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: {"id": "p1", "status": status})
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    await live.task_updates(s, unit)
    assert not s.socket.sent and s.approval == {"proposal": "p1"}
    assert s.history == _proposing_history()


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    {"id": "p1", "status": "completed"},   # not a stored status: reads as missing
    {"id": "p1", "status": "executed", "result": {"summary": "Done — something."}},   # legacy receipt: unproven
])
async def test_an_unproven_or_missing_receipt_says_nothing_and_releases_the_latch(unit, monkeypatch, row):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"proposal": "p1"}; s.history = _proposing_history()
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: row)
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    await live.task_updates(s, unit)
    assert not s.socket.sent and not s.approval
    assert s.history == _proposing_history()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,spoken", [
    ("sent", "The email was approved and has gone."),
    ("skipped", "The email was left unsent; nothing went out."),
])
async def test_a_draft_that_was_sent_or_left_is_told_once(unit, monkeypatch, status, spoken):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"draft": "d1"}; s.history = _proposing_history()
    traced = []
    async def capture(*a, **k):
        traced.append(k)
    monkeypatch.setattr(live, "safe_trace", capture)
    monkeypatch.setattr(db, "get_email_log_entry", lambda *a, **k: {"id": "d1", "status": status})
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    await live.task_updates(s, unit)
    assert [e["content"] for e in s.socket.sent if e["type"] == "session.commentary.append"] == [spoken]
    assert not s.approval and s.history[-1]["content"].endswith("[Card outcome: " + spoken + "]")
    assert [k["detail"]["kind"] for k in traced if k.get("outcome") == "card_outcome_sent"] == ["draft"]
    await live.task_updates(s, unit)
    assert len([e for e in s.socket.sent if e["type"] == "session.commentary.append"]) == 1


@pytest.mark.asyncio
async def test_a_draft_still_waiting_says_nothing_and_stays_tracked(unit, monkeypatch):
    from app import db, concierge_routes as routes
    s = session(); s.approval = {"draft": "d1"}
    monkeypatch.setattr(db, "get_email_log_entry", lambda *a, **k: {"id": "d1", "status": "pending"})
    monkeypatch.setattr(routes, "session_task_snapshot", lambda *a: {"tasks": []})
    await live.task_updates(s, unit)
    assert not s.socket.sent and s.approval == {"draft": "d1"}


def test_card_receipt_covers_each_settled_state_and_cuts_a_long_summary():
    assert live.card_receipt({"state": "cancelled"}) == "The card was cancelled; nothing changed."
    assert live.card_receipt({"state": "superseded"}).startswith("That card was replaced")
    assert live.card_receipt({"state": "expired"}).startswith("That card expired unanswered")
    assert live.card_receipt({"state": "failed", "summary": ""}) == "The card was confirmed but the change did not go through."
    for unsettled in ("unknown", "executing", "pending", "missing"):
        assert live.card_receipt({"state": unsettled, "summary": "Whatever."}) is None
    long_summary = "Created the follow-up for Sam due Friday 16 October. " + "More detail here. " * 40
    line = live.card_receipt({"state": "completed", "summary": long_summary})
    prefix = "The card was confirmed and it went through: "
    assert line.startswith(prefix + "Created the follow-up for Sam due Friday 16 October.")
    assert len(line) <= len(prefix) + 240 and line.endswith(".")
    assert line == prefix + live._short_outcome(long_summary)
    assert live.draft_receipt({"status": "pending"}) is None and live.draft_receipt({}) is None


@pytest.mark.asyncio
async def test_registered_session_exposes_only_application_id_and_closes_cleanly(unit, monkeypatch):
    from app import db
    sock = Socket()
    reads = []
    def history(tenant, **kw):
        reads.append((tenant, kw))
        return [{"role": "user", "content": "Previous complete instruction"}]
    monkeypatch.setattr(db, "list_concierge_messages", history)
    async def provider(sdp):
        return "live_private_provider_id", "v=answer"
    async def attach(provider):
        return sock
    monkeypatch.setattr(live, "create_provider", provider)
    monkeypatch.setattr(live, "attach", attach)
    async def no_tasks(*a):
        pass
    monkeypatch.setattr(live, "task_updates", no_tasks)
    result = await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert "secret-do-not-disclose" not in json.dumps(result)
    assert "live_private_provider_id" not in json.dumps(result)
    await asyncio.sleep(0)
    s = live._sessions[result["session"]]
    assert s.history[0]["content"].startswith("Previous complete instruction")
    assert "Do not answer it or run lookups" in s.history[0]["content"]
    assert reads[0][1]["user_id"] == unit.user_id and reads[0][1]["include_shared"] is False
    await live.finish(s)
    assert s.finalized and not live._sessions and sock.closed


@pytest.mark.asyncio
async def test_native_task_update_lost_append_is_not_replayed(unit, monkeypatch):
    from app import concierge_routes as routes
    s = session(); count = []
    row = {"voice_session": s.voice_session, "id": "task", "revision": 1, "event_id": "result", "speakable": True, "outcome": "Prepared, not executed."}
    def tasks(*a):
        return {"tasks": [row]}
    async def lost(raw):
        count.append(raw); raise OSError("ACK lost")
    s.socket.send = lost
    monkeypatch.setattr(routes, "session_task_snapshot", tasks)
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda *a: row)
    monkeypatch.setattr(live.concierge_delegation, "public", lambda row: row)
    with pytest.raises(live.TaskNoticeFailure) as failure:
        await live.task_updates(s, unit)
    assert isinstance(failure.value.__cause__, OSError)
    await live.task_updates(s, unit)
    assert len(count) == 1


@pytest.mark.asyncio
async def test_real_voice_chat_bridge_supersession_preserves_usage_without_staging(unit, voice_client, monkeypatch):
    from app import concierge, concierge_routes as routes
    _, saved, metered = voice_client
    entered = asyncio.Event(); release = asyncio.Event()
    async def model(*a, **k):
        entered.set(); await release.wait()
        return concierge.BusinessReply(reply="Old answer.", links=[], action=None, model="test",
            usage={"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    monkeypatch.setattr(routes.concierge, "answer_business", model)
    s = session(); s.consumed_ms = 100
    s.delegations["d"] = {"turn": "actual-bridge-turn", "state": "working"}
    run = asyncio.create_task(live.run_backend(unit, "Check Friday", [], "actual-bridge-turn", s.voice_session))
    await entered.wait()
    await live.handle_event(s, fragment("Thursday, not Friday.", 101, 200, "new-words"), unit)
    release.set()
    with pytest.raises(RuntimeError, match="interrupted"):
        await run
    assert not saved and len(metered) == 1


def test_both_concierge_render_paths_include_user_scoped_mode_identity(client, monkeypatch):
    from app import dashboard
    monkeypatch.setattr(dashboard, "has_concierge", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Test"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: True)
    for path in ('/app/concierge', '/app/concierge/panel'):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-voice-user="u1"' in response.text
        assert response.text.count('id="cz-mode-heading"') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["receipt", "revision", "session", "not_speakable"])
async def test_native_task_speaks_fresh_receipt_or_suppresses_stale_binding(unit, monkeypatch, change):
    from app import concierge_routes as routes
    s = session()
    stale = {"voice_session": s.voice_session, "id": "task", "revision": 1,
             "event_id": "same-event", "speakable": True, "outcome": "Old receipt"}
    current = dict(stale, outcome="Current receipt: result remains unconfirmed.")
    if change == "revision":
        current["revision"] = 2
    if change == "session":
        current["voice_session"] = str(uuid.uuid4())
    if change == "not_speakable":
        current["speakable"] = False
    def tasks(*a):
        return {"tasks": [stale]}
    monkeypatch.setattr(routes, "session_task_snapshot", tasks)
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda *a: current)
    monkeypatch.setattr(live.concierge_delegation, "public", lambda row: row)
    await live.task_updates(s, unit)
    if change == "receipt":
        assert s.socket.sent[0]["content"] == current["outcome"]
        assert s.events[0]["text"] == current["outcome"]
    else:
        assert not s.socket.sent and not s.notices


@pytest.mark.asyncio
async def test_audio_interrupt_does_not_cancel_queued_business_work(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; calls = []
    async def backend(*a):
        calls.append("backend")
        return {"reply": "Verified lookup result."}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    result = await live.control(live.Control(session=s.id, operation="interrupt"), unit)
    assert result["status"] == "audio_interrupted"
    await asyncio.gather(*list(s.jobs))
    assert calls == ["backend"] and any(e["kind"] == "result" for e in s.events)

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['close', 'revision', 'denied'])
async def test_native_task_offload_rechecks_late_ownership_and_current_result(unit, monkeypatch, change):
    import threading
    from app import concierge_routes as routes
    s=session();entered=threading.Event();release=threading.Event()
    row={'id':'task','voice_session':s.voice_session,'revision':1,'event_id':'event','speakable':True,'outcome':'Verified fixture result.'}
    monkeypatch.setattr(routes,'session_task_snapshot',lambda *a:{'tasks':[row]})
    def current(*a):
        entered.set();release.wait(2)
        if change=='denied':return None
        return {**row,'revision':2 if change=='revision' else 1}
    monkeypatch.setattr(live.concierge_delegation,'scoped_task',current)
    monkeypatch.setattr(live.concierge_delegation,'public',lambda row:row)
    pending=asyncio.create_task(live.task_updates(s,unit))
    while not entered.is_set():await asyncio.sleep(.005)
    if change=='close':s.closed=True;s.generation+=1
    release.set();await pending
    assert not s.notices and not s.events and not s.socket.sent


@pytest.mark.asyncio
async def test_permission_refresh_denied_closes_and_slow_trace_releases_registry(unit, monkeypatch):
    import time
    s=session();live._sessions[s.id]=s
    def denied(ctx):raise PermissionError('private scope detail')
    monkeypatch.setattr(live,'scope',denied)
    monkeypatch.setattr(live.trace,'emit',lambda *a,**k:time.sleep(.4))
    await asyncio.wait_for(live.maintain(s,unit),1)
    assert s.closed and s.id not in live._sessions and s.close_reason=='permission_denied'
    assert s.socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['missing_context','admission','backend_error','permission'])
async def test_native_admission_failure_metadata_and_no_false_completion(unit, voice_client, monkeypatch, failure):
    from app import concierge_routes as routes
    _,saved,_=voice_client;s=session();traces=[]
    monkeypatch.setattr(live.trace,'emit',lambda *a,**k:traces.append(k))
    if failure=='admission':monkeypatch.setattr(routes.fairuse,'consume',lambda *a:(False,1))
    if failure=='backend_error':
        async def failed(*a,**k):raise OSError('private CRM failure')
        monkeypatch.setattr(routes.concierge,'answer_business',failed)
    if failure=='permission':monkeypatch.setattr(live,'scope',lambda ctx:'revoked')
    if failure!='missing_context':await live.handle_event(s,fragment('Read fixture CRM'),unit)
    await live.handle_event(s,delegation(),unit);await asyncio.gather(*list(s.jobs))
    assert not saved and not [e for e in s.events if e['kind']=='result']
    assert s.counters['delegation_seen']==1
    expected='context_unconfirmed' if failure=='missing_context' else 'backend_unconfirmed'
    assert s.counters[expected]==1
    native=[t for t in traces if (t.get('detail') or {}).get('stage')]
    assert native and all('question' not in t['detail'] and 'reply' not in t['detail'] for t in native)
    assert all('private CRM' not in str(t['detail']) for t in native)

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['preflight','context'])
async def test_shared_voice_worker_read_cancellation_before_model(unit, voice_client, monkeypatch, phase):
    import threading
    from app import concierge_routes as routes
    _,saved,metered=voice_client;entered=threading.Event();release=threading.Event();calls=[]
    def slow(*a,**k):entered.set();release.wait(2);return None if phase=='preflight' else ''
    if phase=='preflight':monkeypatch.setattr(routes,'_asker',slow)
    else:
        monkeypatch.setattr(routes,'_asker',lambda ctx:None)
        monkeypatch.setattr(routes.concierge_delegation,'context_note',slow)
        monkeypatch.setattr(routes,'_people_note',lambda *a:'')
        monkeypatch.setattr(routes,'_voice_note',lambda *a,**k:'')
    async def forbidden(*a,**k):calls.append(1);raise AssertionError('cancelled worker entered model')
    monkeypatch.setattr(routes.concierge,'answer_business',forbidden)
    task=asyncio.create_task(live.run_backend(unit,'Fixture read',[],'worker-'+phase,str(uuid.uuid4())))
    while not entered.is_set():await asyncio.sleep(.005)
    voice_turn_state.cancel(voice_turn_state.key('t1','u1','worker-'+phase))
    release.set()
    with pytest.raises(RuntimeError,match='interrupted'):await task
    assert not calls and not saved and not metered


@pytest.mark.asyncio
async def test_committed_finalize_worker_survives_lost_stream_without_duplicate_save(unit, voice_client, monkeypatch):
    import threading
    from app import concierge_routes as routes
    _,saved,metered=voice_client;entered=threading.Event();release=threading.Event();finished=threading.Event()
    original=routes.finish_business_turn
    def slow(*a,**k):
        entered.set();release.wait(2)
        try:return original(*a,**k)
        finally:finished.set()
    monkeypatch.setattr(routes,'finish_business_turn',slow)
    monkeypatch.setattr(routes,'_asker',lambda ctx:None)
    monkeypatch.setattr(routes.concierge_delegation,'context_note',lambda *a:'')
    monkeypatch.setattr(routes,'_people_note',lambda *a:'')
    monkeypatch.setattr(routes,'_voice_note',lambda *a,**k:'')
    turn='lost-finalize-stream';task=asyncio.create_task(live.run_backend(unit,'Fixture read',[],turn,str(uuid.uuid4())))
    while not entered.is_set():await asyncio.sleep(.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert voice_turn_state.cancel(voice_turn_state.key('t1','u1',turn))['status']=='too_late'
    release.set()
    while not finished.is_set():await asyncio.sleep(.005)
    for _ in range(50):
        if not routes._VOICE_RUNNERS:break
        await asyncio.sleep(.005)
    assert len(saved)==2 and len(metered)==1
    with pytest.raises(HTTPException) as denied:await live.run_backend(unit,'Fixture read',[],turn,str(uuid.uuid4()))
    assert denied.value.status_code==503
    assert len(saved)==2 and len(metered)==1

@pytest.mark.asyncio
async def test_quoted_fragmented_multi_sentence_status_never_becomes_affirmative_guard(unit):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear();s=session()
    for i,text in enumerate(["The customer said 'the email was sent, ","but no invoice arrived. The booking was cancelled'.",' The example was "the record was updated. It was completed.".']):
        await live.handle_event(s,{**fragment(text,i*100,i*100+100,'quote'+str(i)),'type':'session.output_transcript.delta'},unit)
    assert not s.events
    await live.monitor_output(s,"He said 'sent', but I charged the invoice.",unit)
    assert len([e for e in s.events if e['kind']=='guard'])==1


@pytest.mark.asyncio
async def test_native_archive_scoped_dedup_order_and_non_authoritative_history(unit, monkeypatch):
    from app import db, concierge
    saved = {}
    monkeypatch.setattr(db, "save_native_transcript", lambda rows: saved.update({row["id"]: row for row in rows}))
    s = session()
    await live.handle_event(s, fragment("Hello ", 0, 20, "a"), unit)
    await live.handle_event(s, fragment("Hello ", 0, 20, "a"), unit)
    await live.handle_event(s, fragment("there", 21, 40, "b"), unit)
    await live.handle_event(s, {**fragment("Hi!", 60, 80, "c"), "type":"session.output_transcript.delta"}, unit)
    await live.flush_transcript(s); await live.flush_transcript(s)
    rows=sorted(saved.values(),key=lambda row:row["created_at"])
    assert len(rows)==3 and rows[0]["content"].endswith("Hello ")
    assert rows[1]["content"].endswith("there") and rows[2]["content"].endswith("Hi!")
    assert all(row["tenant_id"]=="t1" and row["user_id"]=="u1" for row in rows)
    assert concierge.sanitize_history(rows,16,3000)==[]
    other=session();other.voice_session=s.voice_session;other.id="new-owned-session"
    await live.handle_event(other,fragment("New conversation",0,20,"a"),unit)
    await live.flush_transcript(other)
    assert len(saved)==4 and not s.transcript_dirty


@pytest.mark.asyncio
async def test_native_archive_cancelled_waiter_does_not_overlap_or_lose_new_words(unit,monkeypatch):
    import threading
    from app import db
    entered=threading.Event();release=threading.Event();saved={};calls=[]
    def write(rows):
        calls.append(rows)
        if len(calls)==1:entered.set();assert release.wait(3)
        saved.update({row["id"]:row for row in rows})
    monkeypatch.setattr(db,"save_native_transcript",write)
    s=session();await live.handle_event(s,fragment("A",0,20,"a"),unit)
    waiter=asyncio.create_task(live.flush_transcript(s))
    while not entered.is_set():await asyncio.sleep(.001)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    await live.handle_event(s,fragment("B",21,40,"b"),unit)
    join=asyncio.create_task(live.flush_transcript(s));await asyncio.sleep(.01)
    assert len(calls)==1
    release.set();await join
    assert len(saved)==2 and "".join(row["content"].split("\n",1)[1] for row in sorted(saved.values(),key=lambda row:row["created_at"]))=="AB"
    assert len(calls)==2 and not s.transcript_dirty


@pytest.mark.asyncio
async def test_native_archive_failure_retry_is_idempotent_and_globally_bounded(unit,monkeypatch):
    from app import db
    rows={};attempts=[]
    def lost_reply(batch):
        rows.update({row["id"]:row for row in batch});attempts.append(batch)
        if len(attempts)==1:raise RuntimeError("lost database reply")
    monkeypatch.setattr(db,"save_native_transcript",lost_reply)
    s=session();await live.handle_event(s,fragment("Ordinary conversation",0,20,"a"),unit)
    await live.flush_transcript(s);assert s.transcript_dirty
    await live.flush_transcript(s);assert len(rows)==1 and not s.transcript_dirty
    s.transcript_chars=240000
    live.archive_fragment(s,"session.input_transcript.delta",{"event_id":"extra","start_ms":10000,"end_ms":10001,"delta":"X"})
    assert len(s.transcript_segments)==1 and s.counters["transcript_capacity"]==1


@pytest.mark.asyncio
async def test_actual_tool_sse_only_sanitized_activity_callback(unit,monkeypatch):
    from app import concierge_routes as routes
    from fastapi.responses import StreamingResponse
    calls=[]
    async def callback():calls.append("Checking business data")
    async def chat(*args):
        async def frames():
            yield 'data: {"t":"tool","name":"private","detail":"SECRET RECORD","hint":"MODEL CLAIM"}\n\n'
            yield 'data: {"t":"done","reply":"Read ready"}\n\n'
        return StreamingResponse(frames())
    monkeypatch.setattr(routes,"voice_chat",chat)
    token=live._activity_sink.set(callback)
    try:result=await live.run_backend(unit,"Check",[],"turn",str(uuid.uuid4()))
    finally:live._activity_sink.reset(token)
    assert result["reply"]=="Read ready" and calls==["Checking business data"]


@pytest.mark.asyncio
async def test_close_releases_session_capacity_while_archive_writer_drains(unit,monkeypatch):
    import threading
    from app import db
    entered=threading.Event();release=threading.Event();saved={}
    def write(rows):
        entered.set();assert release.wait(3)
        saved.update({row["id"]:row for row in rows})
    monkeypatch.setattr(db,"save_native_transcript",write)
    original=asyncio.wait_for
    async def brief(awaitable,timeout):return await original(awaitable,.02 if timeout==3 else timeout)
    monkeypatch.setattr(live.asyncio,"wait_for",brief)
    s=session();live._sessions[s.id]=s
    await live.handle_event(s,fragment("A",0,20,"a"),unit)
    waiter=asyncio.create_task(live.flush_transcript(s))
    while not entered.is_set():await asyncio.sleep(.001)
    await live.handle_event(s,fragment("B",21,40,"b"),unit)
    await live.finish(s)
    assert s.closed and s.id not in live._sessions and not s.transcript_job.done()
    release.set();await waiter
    assert not s.transcript_dirty and len(saved)==2
    assert "".join(row["content"].split("\n",1)[1] for row in sorted(saved.values(),key=lambda row:row["created_at"]))=="AB"


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_complete_invoice_instruction_crosses_delegation_anchor_once(unit, monkeypatch, late):
    s = session(); seen = []
    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    if late:
        monkeypatch.setattr(live.settings, "gptlive_settle_ms", 20)
        monkeypatch.setattr(live, "CONTEXT_WAIT", .4)
        await live.handle_event(s, fragment("Can you check my outstanding", 0, 70), unit)
        await live.handle_event(s, delegation(offset=80), unit)
        await asyncio.sleep(.01)
        await live.handle_event(s, fragment(" invoices please", 71, 110, "late-words"), unit)
    else:
        await live.handle_event(s, fragment("Can you check my outstanding invoices please", 0, 110), unit)
        await live.handle_event(s, delegation(offset=80), unit)
    await asyncio.gather(*s.jobs)
    assert seen == ["Can you check my outstanding invoices please"]
    assert s.consumed_ms == 110 and s.delegations["d1"]["state"] == "complete"


@pytest.mark.asyncio
async def test_rejected_window_does_not_poison_next_clear_request_and_trace_has_no_words(unit, monkeypatch):
    s = session(); seen = []; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    async def backend(ctx, question, *args):
        seen.append(question)
        return {"reply": "Fixture read"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Book it and", 0, 90), unit)
    await live.handle_event(s, delegation(offset=80), unit)
    await asyncio.gather(*s.jobs)
    assert s.consumed_ms == 90 and not seen
    rejected = [t for t in traces if t.get("outcome") == "context_unconfirmed"]
    assert rejected[0]["detail"]["reason"] == "incomplete_text"
    assert rejected[0]["detail"]["fragment_bounds_ms"] == [[0,90]]
    assert rejected[0]["detail"]["delegation_offset_ms"] == 80
    assert len(rejected[0]["detail"]["fragment_arrival_delta_ms"]) == 1
    assert "Book it" not in json.dumps(rejected)
    await live.handle_event(s, fragment("Check the diary tomorrow", 100, 200, "fresh"), unit)
    await live.handle_event(s, delegation("fresh-delegation", "d2", 180), unit)
    await asyncio.gather(*s.jobs)
    assert seen == ["Check the diary tomorrow"]


def test_missing_anchor_does_not_quarantine_unrelated_later_words(unit):
    s = session(); s.fragments = [{k: v for k, v in fragment("Check the diary", 150, 200).items() if k != "type"}]
    job = {"offset": 100}; assert live.snapshot(s, job) is None
    assert job["context_end"] == -1


@pytest.mark.asyncio
async def test_client_close_reason_is_first_scoped_allowlisted_diagnostic(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    body = live.Control(session=s.id, operation="close", close_reason="end", startup_stage="ready", peer_state="connected", channel_state="open")
    await live.control(body, unit)
    reported = [t for t in traces if t.get("outcome") == "end"]
    assert len(reported) == 1 and reported[0]["detail"]["peer_state"] == "connected"
    assert s.close_reason == "local_close"
    with pytest.raises(ValueError):
        live.Control(session="owned", operation="close", close_reason="raw-secret-or-stack")


@pytest.mark.asyncio
async def test_hold_stop_closes_the_provider_session(unit, monkeypatch):
    """Hold on the orb ends the connection (Kris 2026-10-14): the client's close carries
    reason hold_stop, and the server must accept it and finish the session - a rejected
    reason would leave the provider session, and its hearing, open."""
    s = session(); live._sessions[s.id] = s; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    body = live.Control(session=s.id, operation="close", close_reason="hold_stop", startup_stage="ready", peer_state="connected", channel_state="open")
    result = await live.control(body, unit)
    assert result["ok"] is True and s.closed
    assert [t for t in traces if t.get("outcome") == "hold_stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [None, False, True])
@pytest.mark.parametrize("registered", [False, True])
async def test_historical_readiness(unit, monkeypatch, ready, registered):
    s = session(); traces = []
    if registered:
        live._sessions[s.id] = s
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    extra = {} if ready is None else {"reached_ready": ready, "playback_state": "muted"}
    body = live.Control(session=s.id if registered else s.voice_session, operation="close",
                        startup_stage="awaiting_ready", peer_state="disconnected", **extra)
    await live.control(body, unit)
    report = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_client_close")
    assert report["reached_ready"] is ready
    assert report["stage"] == "awaiting_ready" and report["peer_state"] == "disconnected"
    assert report["playback_state"] == ("unknown" if ready is None else "muted")
    assert report["silence_reasons"] is None and report["playback_events"] is None
    assert report["playback_events_omitted"] is None
    if registered:
        summary = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_session")
        assert summary["reached_ready"] is ready
    else:
        await live.control(body, unit)
        assert len(traces) == 1
    with pytest.raises(ValueError):
        live.Control(session="owned", operation="close", reached_ready="yes")
    with pytest.raises(ValueError):
        live.Control(session="owned", operation="close", playback_state="raw-private-text")


@pytest.mark.asyncio
async def test_client_playback_transition_schema_and_trace(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    transition = {"sequence": 3, "observed_at": "2026-10-13T13:00:00.000Z", "action": "resume",
        "reason": "user", "silence_reasons": ["connection"], "playback_state": "muted", "guard_seq": 9}
    body = live.Control(session=s.id, operation="close", control_id="client-close", reached_ready=True,
        silence_reasons=["connection"], playback_state="muted", playback_events=[transition], playback_events_omitted=2)
    await live.control(body, unit)
    report = next(t["detail"] for t in traces if "playback_events" in t["detail"])
    assert report["application_session_id"] == s.id and report["control_id"] == "client-close"
    assert report["playback_events"][0]["action"] == "resume"
    assert report["playback_events"][0]["guard_seq"] == 9
    assert report["silence_reasons"] == ["connection"] and report["playback_events_omitted"] == 2
    for invalid in ({"playback_events": [transition]*33}, {"silence_reasons": ["secret"]},
                    {"playback_events": [{**transition, "observed_at": "2026-10-13T13:00:00"}]},
                    {"playback_events": [{**transition, "transcript": "private"}]}, {"control_id": "unsafe\ntext"}):
        with pytest.raises(ValueError):
            live.Control(session=s.id, operation="close", **invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["send it", "do it", "List invoices", "Check diary"])
async def test_short_card_approval_is_distinct_from_complete_short_request(unit, monkeypatch, text):
    s = session(); seen = []
    async def backend(ctx, question, *args):
        seen.append(question)
        if "drafts" in question and antecedent=="business":
            assert "00000000-0000-0000-0000-000000000001" in args[0][-1]["content"]; return {"reply": "Fixture"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(text), unit);await live.handle_event(s, delegation(), unit)
    await asyncio.gather(*s.jobs)
    assert seen == ([] if text in {"send it", "do it"} else [text])


@pytest.mark.asyncio
async def test_missing_anchor_timeout_preserves_future_window_for_fresh_delegation(unit, monkeypatch):
    s = session(); seen = []
    async def backend(ctx, question, *args):
        seen.append(question)
        if "drafts" in question and antecedent=="business":
            assert "00000000-0000-0000-0000-000000000001" in args[0][-1]["content"]; return {"reply": "Fixture"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("List invoices", 150, 200), unit)
    await live.handle_event(s, delegation(offset=100), unit);await asyncio.gather(*s.jobs)
    assert s.consumed_ms == -1 and not seen
    await live.handle_event(s, delegation("later", "d2", 200), unit);await asyncio.gather(*s.jobs)
    assert seen == ["List invoices"]


@pytest.mark.asyncio
async def test_backend_value_error_is_unconfirmed_result_not_repeat_instruction(unit, monkeypatch):
    s = session(); traces=[]
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    async def backend(*args):raise ValueError("private backend body")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit);await live.handle_event(s, delegation(), unit)
    await asyncio.gather(*s.jobs)
    assert s.delegations["d1"]["state"] == "unconfirmed"
    assert any(t.get("outcome")=="backend_unconfirmed" for t in traces)
    assert not any(t.get("outcome")=="context_unconfirmed" for t in traces)
    assert "private backend body" not in json.dumps(s.events)


@pytest.mark.asyncio
async def test_close_marker_reserved_before_slow_diagnostic_trace(unit, monkeypatch):
    entered=asyncio.Event(); release=asyncio.Event(); voice=str(uuid.uuid4())
    async def slow_trace(*a, **kw):entered.set(); await release.wait()
    monkeypatch.setattr(live, "safe_trace", slow_trace)
    pending=asyncio.create_task(live.control(live.Control(session=voice, operation="close", close_reason="end"), unit))
    await entered.wait()
    assert (unit.tenant_id, unit.user_id, voice) in live._cancelled
    release.set(); await pending


@pytest.mark.asyncio
async def test_owned_close_cancels_and_removes_session_before_slow_trace(unit, monkeypatch):
    s=session();live._sessions[s.id]=s;entered=asyncio.Event();release=asyncio.Event()
    async def slow_trace(*a, **kw):entered.set();await release.wait()
    monkeypatch.setattr(live, "safe_trace", slow_trace)
    pending=asyncio.create_task(live.control(live.Control(session=s.id, operation="close", close_reason="end"), unit))
    await entered.wait()
    assert s.closed and s.generation==1 and s.id not in live._sessions and s.socket.closed
    release.set();await pending


@pytest.mark.asyncio
async def test_clarification_trace_does_not_consume_words_arriving_after_snapshot(unit, monkeypatch):
    s=session();entered=asyncio.Event();release=asyncio.Event();seen=[]
    async def tracer(*a, **kw):
        if kw.get("outcome")=="context_unconfirmed":entered.set();await release.wait()
    async def backend(ctx, question, *args):seen.append(question);return {"reply":"Fixture"}
    monkeypatch.setattr(live, "safe_trace", tracer);monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Book it and",0,90),unit);await live.handle_event(s,delegation(offset=80),unit)
    await entered.wait();assert s.consumed_ms==90
    await live.handle_event(s,fragment("List invoices",100,200,"new-words"),unit)
    release.set();await asyncio.gather(*s.jobs)
    assert s.consumed_ms==90
    await live.handle_event(s,delegation("next-job","d2",180),unit);await asyncio.gather(*s.jobs)
    assert seen==["List invoices"]


@pytest.mark.asyncio
async def test_close_during_admission_trace_never_starts_backend(unit, monkeypatch):
    s=session();entered=asyncio.Event();release=asyncio.Event();seen=[]
    async def tracer(*a, **kw):
        if kw.get("outcome")=="backend_requested":entered.set();await release.wait()
    async def backend(*args):seen.append(True);return {"reply":"Fixture"}
    monkeypatch.setattr(live, "safe_trace", tracer);monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s,fragment(),unit);await live.handle_event(s,delegation(),unit)
    await entered.wait();await live.finish(s);release.set();await asyncio.gather(*s.jobs)
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize("text,allowed", [
    ("Continue working on the Blam eight live conversation mode", False),
    ("I'm watching the UI when I talk the UI should expand out of the B but it doesn't", False),
    ("Check overdue invoices in the BLAM8 UI", True),
    ("Find a test invoice in BLAM8", True),
    ("Continue working on BLAM8 live conversation and send overdue invoices", False),
])
async def test_meta_feedback_does_not_lookup_business_records(unit, monkeypatch, text, allowed):
    s=session();seen=[]
    async def backend(ctx, question, *args):seen.append(question);return {"reply":"Fixture read only"}
    monkeypatch.setattr(live,"run_backend",backend)
    await live.handle_event(s,fragment(text),unit);await live.handle_event(s,delegation(),unit)
    await asyncio.gather(*s.jobs)
    assert seen==([text] if allowed else [])
    assert s.delegations["d1"]["state"]==("complete" if allowed else "clarification")


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["Could you write up the drafts please", "Could you write up the drafts for me please", "Write the drafts now", "Please prepare those drafts thanks", "Write all the drafts please", "Could you draft it for me", "Prepare drafts for those invoices please", "Write up all of these drafts please"])
@pytest.mark.parametrize("antecedent", ["business", "expired", "meta", "restored", "different_topic", "settings", "diary", "failed", "empty"])
async def test_draft_followups_remain_in_shared_concierge_pipeline(unit, monkeypatch, antecedent, phrase):
    s=session();seen=[]
    async def backend(ctx, question, *args):
        seen.append(question)
        return {"reply":"Lookup unconfirmed" if antecedent=="failed" else "Fixture result", "trail": [] if antecedent in {"failed", "empty"} else [{"name":"list_invoices", "invoice_ids":["00000000-0000-0000-0000-000000000001"]}]}
    monkeypatch.setattr(live,"run_backend",backend)
    if antecedent=="restored":s.history=[{"role":"assistant","content":"I can see overdue invoices. Want drafts?"}]
    else:
        text={"meta":"Continue working on BLAM8 live conversation mode", "settings":"Show settings", "diary":"Check diary"}.get(antecedent, "Check overdue invoices")
        await live.handle_event(s,fragment(text),unit);await live.handle_event(s,delegation(),unit);await asyncio.gather(*s.jobs)
        if antecedent=="different_topic":
            await live.handle_event(s,fragment("What is the weather",110,150,"topic"),unit)
            await live.handle_event(s,delegation("topic-job","d2",150),unit);await asyncio.gather(*s.jobs)
    before=len(seen)
    await live.handle_event(s,fragment(phrase,200,300,"followup"),unit)
    await live.handle_event(s,delegation("followup-job","d3",300),unit);await asyncio.gather(*s.jobs)
    assert len(seen)==before+1


@pytest.mark.asyncio
async def test_reader_error_invalidates_before_blocked_diagnostics(unit, monkeypatch):
    s=session();live._sessions[s.id]=s;entered=asyncio.Event();release=asyncio.Event();records=[]
    async def tracing(*a, **kw):
        records.append((a,kw))
        if len(a)>1 and a[1]=="gptlive_error":entered.set();await release.wait()
    monkeypatch.setattr(live,"safe_trace",tracing)
    s.socket.queue.put_nowait("private-invalid-json")
    pending=asyncio.create_task(live.read_sideband(s,unit));await entered.wait()
    assert s.closed and s.generation==1 and s.id not in live._sessions and s.socket.closed
    assert s.terminal["error_category"]=="JSONDecodeError"
    release.set();await pending
    assert "private-invalid-json" not in json.dumps(records,default=str)


def test_websocket_diagnostic_preserves_outer_class_codes_and_hides_os_body(unit):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    exc=ConnectionClosedError(Close(1001,"private close reason"),Close(1000,"private sent reason"),True)
    exc.__cause__=OSError("private token")
    data=live.error_metadata(exc,"receive")
    assert data["error_category"]=="ConnectionClosedError"
    assert data["received_close_code"]==1001 and data["sent_close_code"]==1000
    assert "private" not in json.dumps(data)


@pytest.mark.asyncio
async def test_recoverable_task_failure_keeps_reader_alive_and_records_category(unit,monkeypatch):
    s=session();seen=asyncio.Event();records=[];calls=0
    async def broken(*args):
        nonlocal calls
        calls+=1
        if calls==1:raise RuntimeError("private task data")
        seen.set()
    async def tracing(*a,**kw):records.append((a,kw))
    monkeypatch.setattr(live,"task_updates",broken);monkeypatch.setattr(live,"safe_trace",tracing)
    monkeypatch.setattr(live,"MAINTENANCE_INTERVAL",.01)
    s.reader=asyncio.create_task(live.read_sideband(s,unit));await asyncio.wait_for(seen.wait(),1)
    assert not s.closed and s.counters["task_update_error"]==1
    assert any(k.get("outcome")=="recoverable" and k["detail"]["phase"]=="task_refresh" for _,k in records)
    assert "private task data" not in json.dumps(records,default=str)
    await live.finish(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [(403,"permission_denied"),(503,"permission_check_unavailable")])
async def test_authority_failure_class_is_not_generic_permission_denial(unit,monkeypatch,status,reason):
    s=session()
    # a check that could not run is tolerated inside the grace; this pins the terminal path
    monkeypatch.setattr(live,"AUTHORITY_GRACE",0)
    s.verified_at-=100   # past the grace even on a coarse clock
    async def failed(*args):raise HTTPException(status,"private check detail")
    monkeypatch.setattr(live,"_authorized",failed)
    await live.maintain(s,unit)
    assert s.closed and s.close_reason==reason and s.terminal["http_status"]==status
    assert "private check detail" not in json.dumps(s.terminal)


@pytest.mark.asyncio
async def test_closed_during_authorization_retains_first_local_reason(unit,monkeypatch):
    s=session();entered=asyncio.Event();release=asyncio.Event()
    async def checked(*args):
        entered.set();await release.wait();raise HTTPException(410,"ended")
    monkeypatch.setattr(live,"_authorized",checked)
    pending=asyncio.create_task(live.maintain(s,unit));await entered.wait()
    live.reserve_terminal(s,"local_close","client_control",client_reason="end")
    await live.finish(s);release.set();await pending
    assert s.close_reason=="local_close" and s.terminal["client_reason"]=="end"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["receive", "task_refresh"])
async def test_real_socket_close_remains_terminal(unit, monkeypatch, phase):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    s=session()
    async def closed(*args):
        raise ConnectionClosedError(Close(1011,"private provider detail"),None)
    if phase=="receive":
        s.socket.recv=closed
        await live.read_sideband(s,unit)
    else:
        monkeypatch.setattr(live,"task_updates",closed)
        await live.maintain(s,unit)
    assert s.closed and s.close_reason=="sideband_error"
    assert s.terminal["phase"]==phase and s.terminal["received_close_code"]==1011
    assert "private" not in json.dumps(s.terminal)


@pytest.mark.asyncio
async def test_notice_timeout_keeps_reader_alive(unit, monkeypatch):
    s=session();seen=asyncio.Event();calls=0
    async def notices(*args):
        nonlocal calls
        calls+=1
        if calls==1:
            try:raise TimeoutError("private transport body")
            except TimeoutError as exc:raise live.TaskNoticeFailure() from exc
        seen.set()
    monkeypatch.setattr(live,"task_updates",notices)
    monkeypatch.setattr(live,"MAINTENANCE_INTERVAL",.01)
    s.reader=asyncio.create_task(live.read_sideband(s,unit))
    await asyncio.wait_for(seen.wait(),1)
    assert not s.closed and s.counters["task_update_error"]==1
    await live.finish(s)


@pytest.mark.asyncio
async def test_actual_shared_bridge_invoice_followup_prepares_for_approval_without_execute(unit, voice_client, monkeypatch):
    from app import concierge, concierge_routes as routes, concierge_data
    voice_client
    monkeypatch.setattr(routes,"_asker",lambda ctx:None)
    monkeypatch.setattr(routes,"_people_note",lambda *a:"")
    monkeypatch.setattr(routes,"_voice_note",lambda *a,**k:"")
    monkeypatch.setattr(routes.concierge_delegation,"context_note",lambda *a:"")
    monkeypatch.setattr(routes.concierge_actions,"mailbox_refusal",lambda *a,**k:None)
    records=[{"invoice_id":str(i),"status":"overdue"} for i in range(4)]
    monkeypatch.setitem(concierge_data._EXECUTORS,"list_invoices",lambda *args:{"invoices":records})
    staged=[];executed=[]
    def stage(*args,**kw):
        staged.append(args[2]);return {"proposal_id":"fixture-four", "kind":"fixture_drafts", "summary":"Review four fixture drafts", "expires_s":300}
    async def execute(*args,**kw):executed.append(1);raise AssertionError("No execution authorized")
    monkeypatch.setattr(routes.concierge_actions,"stage",stage)
    monkeypatch.setattr(routes.concierge_actions,"execute",execute)
    async def model(question,history,**kw):
        if question=="Check overdue invoices":
            await kw["on_tool"]("list_invoices",{})
            await kw["tool_executor"]("list_invoices",{})
            return concierge.BusinessReply(reply="Four fixture overdue invoices.",links=[],action=None,model="fixture",usage={})
        assert question=="Could you write up the drafts please"
        assert any("Four fixture overdue invoices" in item.get("content","") for item in history)
        return concierge.BusinessReply(reply="Four fixture drafts prepared for review.",links=[],action={"kind":"fixture_drafts","invoice_ids":[r["invoice_id"] for r in records]},model="fixture",usage={})
    monkeypatch.setattr(routes.concierge,"answer_business",model)
    s=session()
    for i,text in enumerate(["Check overdue invoices","Could you write up the drafts please"]):
        await live.handle_event(s,fragment(text,i*200,i*200+100,"i"+str(i)),unit)
        await live.handle_event(s,delegation("e"+str(i),"d"+str(i),i*200+100),unit)
        await asyncio.gather(*s.jobs)
    assert len(staged)==1 and len(staged[0]["invoice_ids"])==4 and executed==[]
    assert [e for e in s.events if e["kind"]=="result"][-1]["payload"]["proposal"]["proposal_id"]=="fixture-four"


@pytest.mark.asyncio
async def test_several_finished_tasks_are_one_short_spoken_line(unit, monkeypatch):
    from app import concierge_routes as routes
    s = session()
    base = {"voice_session": s.voice_session, "revision": 1, "speakable": True}
    rows = [{**base, "id": "draft", "event_id": "e1", "title": "Draft reply to Sam", "priority": 3, "outcome": "Draft is ready."},
            {**base, "id": "look", "event_id": "e2", "title": "Check Friday", "priority": 1, "outcome": "Friday is free."},
            {**base, "id": "need", "event_id": "e3", "title": "Block Monday", "priority": 0, "outcome": "Needs your approval."}]
    monkeypatch.setattr(routes, "session_task_snapshot", lambda voice, ctx: {"tasks": [dict(r) for r in rows]})
    monkeypatch.setattr(live.concierge_delegation, "scoped_task", lambda ctx, id: next(dict(r) for r in rows if r["id"] == id))
    monkeypatch.setattr(live.concierge_delegation, "public", lambda row: row)
    await live.task_updates(s, unit)
    sent = [e for e in s.socket.sent if e["type"] == "session.commentary.append"]
    assert len(sent) == 1 and len(s.events) == 3
    line = sent[0]["content"]
    assert line.startswith("3 requests are ready: Block Monday; Check Friday; Draft reply to Sam.")
    await live.task_updates(s, unit)
    assert len([e for e in s.socket.sent if e["type"] == "session.commentary.append"]) == 1


def test_short_outcome_keeps_whole_sentences():
    assert live._short_outcome("Short.") == "Short."
    long = "Friday has two free slots at ten and two. " + "Extra detail here. " * 30
    out = live._short_outcome(long)
    assert out.startswith("Friday has two free slots at ten and two.") and out.endswith(".")
    assert len(out) <= 240


def test_api_error_diagnostics_carry_the_code_and_a_bounded_message_only():
    from postgrest.exceptions import APIError
    data = live.error_metadata(APIError({"message": "JWT expired", "code": "PGRST301",
                                         "hint": "h", "details": "private row"}), "task_refresh")
    assert data["api_code"] == "PGRST301" and data["api_message"] == "JWT expired"
    assert "private" not in json.dumps(data)
    # a message that can embed row data (a filter echoing a contact name) is redacted, not trimmed
    data = live.error_metadata(APIError({"message": "failed to parse logic tree ((x))", "code": "PGRST100"}),
                               "task_refresh")
    assert data["api_code"] == "PGRST100" and data["api_message"] == "redacted"
    # a non-JSON body: the int HTTP status is the code, the raw body (details) is never read
    data = live.error_metadata(APIError({"message": "JSON could not be generated", "code": 502,
                                         "hint": "", "details": "<html>"}), "task_refresh")
    assert data["api_code"] == "502" and "<html>" not in json.dumps(data)
    data = live.error_metadata(APIError({"message": "x", "code": "not a code!"}), "task_refresh")
    assert data["api_code"] == "unclassified"
    # the same keys through the authority wrapper, whose cause is the read that failed
    failure = live.AuthorizationFailure(503, "Permissions could not be verified.")
    failure.__cause__ = APIError({"message": "JWT expired", "code": "PGRST301", "details": "private row"})
    data = live.error_metadata(failure, "authorization")
    assert data["api_code"] == "PGRST301" and data["api_message"] == "JWT expired"
    assert data["http_status"] == 503 and data["error_class"] == "APIError"
    assert "private" not in json.dumps(data)
    # any other exception carries no api_* keys
    data = live.error_metadata(RuntimeError("private"), "task_refresh")
    assert not [key for key in data if key.startswith("api_")]


def arm(monkeypatch, unit, position=0, exc_factory=None, fail_times=1):
    """Run the real permission scope over fake database reads. The Nth read inside one check raises
    (1 get_member, 2 active_modules, 3 preview_modules) for the first `fail_times` checks; position 0
    never fails. `state` counts checks ("auth") and every read ("reads")."""
    from app import db
    state = {"auth": 0, "read": 0, "reads": []}

    def read(name, value):
        def fake(*args, **kwargs):
            state["read"] += 1
            state["reads"].append(name)
            if state["read"] == position and state["auth"] <= fail_times:
                raise exc_factory()
            return value
        return fake

    member = {"user_id": "u1", "role": "owner", "active": True, "scopes": {}}
    monkeypatch.setattr(db, "get_member", read("get_member", member))
    monkeypatch.setattr(db, "active_modules", read("active_modules", {"concierge"}))
    monkeypatch.setattr(db, "preview_modules", read("preview_modules", set()))

    def counted_scope(ctx):
        state["auth"] += 1
        state["read"] = 0
        return unit.scope(ctx)
    monkeypatch.setattr(live, "scope", counted_scope)
    return state


@pytest.mark.asyncio
async def test_a_failed_member_read_is_unverifiable_not_a_permission_change(unit, monkeypatch):
    s = session()
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    arm(monkeypatch, unit, 1, lambda: httpx.ReadTimeout("read timed out"))
    with pytest.raises(live.AuthorizationFailure) as caught:
        await live.authorized(s, unit)
    # a read that could not run is the "could not be verified" class, never "Permissions changed"
    assert caught.value.status_code == 503
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    assert live.error_metadata(caught.value, "authorization")["error_class"] == "ReadTimeout"
    assert live.authority_reason(s, caught.value) == "permission_check_unavailable"


@pytest.mark.asyncio
async def test_an_inactive_member_is_still_refused_at_once(unit, monkeypatch):
    from app import db
    s = session()
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    reads = []

    def switched_off(*args, **kwargs):
        reads.append(1)
        return {"user_id": "u1", "role": "owner", "active": False, "scopes": {}}
    monkeypatch.setattr(db, "get_member", switched_off)
    await asyncio.wait_for(live.maintain(s, unit), 3)
    assert s.closed and s.close_reason == "permission_denied"
    assert s.terminal["http_status"] == 403 and s.terminal["phase"] == "authorization"
    assert reads == [1]


@pytest.mark.asyncio
async def test_scope_digest_matches_access_scope(unit, monkeypatch):
    state = arm(monkeypatch, unit)
    digest = await asyncio.to_thread(unit.scope, unit)
    # one check reads the member once and the two module lists once each (it used to read the member twice)
    assert state["reads"] == ["get_member", "active_modules", "preview_modules"]
    assert digest == live.concierge_delegation.access_scope("t1", "u1")[0]


def recorded(monkeypatch):
    """Replace the bounded trace writer with a list of (args, kwargs), and keep the task snapshot off
    the database so maintain() runs its real task_updates over an empty list."""
    from app import concierge_routes as routes
    records = []

    async def tracing(*args, **kwargs):
        records.append((args, kwargs))
    monkeypatch.setattr(live, "safe_trace", tracing)
    monkeypatch.setattr(routes, "session_task_snapshot", lambda voice, ctx: {"tasks": []})
    return records


def rows(records, phase):
    return [kwargs for _args, kwargs in records if (kwargs.get("detail") or {}).get("phase") == phase]


def read_timed_out():
    return httpx.ReadTimeout("read timed out")


def token_expired():
    from postgrest.exceptions import APIError
    return APIError({"message": "JWT expired", "code": "PGRST301", "hint": "h", "details": "private row"})


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("failure,error_class,api_code", [(read_timed_out, "ReadTimeout", None),
                                                          (token_expired, "APIError", "PGRST301")])
async def test_one_unverifiable_check_inside_the_grace_keeps_the_conversation(
        unit, monkeypatch, position, failure, error_class, api_code):
    import time
    s = session()
    records = recorded(monkeypatch)
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    state = arm(monkeypatch, unit, position, failure)
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    s.verified_at = time.monotonic()
    s.reader = asyncio.create_task(live.read_sideband(s, unit))
    await asyncio.sleep(.3)
    assert not s.closed and not s.terminal
    assert s.counters["diagnostic_authority_grace"] == 1
    grace = rows(records, "authority_grace")
    assert len(grace) == 1 and grace[0]["outcome"] == "recoverable"
    detail = grace[0]["detail"]
    assert detail["error_class"] == error_class and detail.get("api_code") == api_code
    assert 0 <= detail["since_verified_s"] < 5
    assert state["auth"] >= 3   # the checks carried on after the one that could not run
    assert "read timed out" not in json.dumps(records, default=str)
    assert "private row" not in json.dumps(records, default=str)
    await live.finish(s)


@pytest.mark.asyncio
async def test_unverifiable_checks_beyond_the_grace_end_the_conversation(unit, monkeypatch):
    import time
    s = session()
    records = recorded(monkeypatch)
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    arm(monkeypatch, unit, 1, read_timed_out, fail_times=10 ** 6)
    monkeypatch.setattr(live, "AUTHORITY_GRACE", .05)
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    s.verified_at = time.monotonic()
    await asyncio.wait_for(live.maintain(s, unit), 5)
    assert s.closed and s.close_reason == "permission_check_unavailable"
    assert s.terminal["http_status"] == 503 and s.terminal["error_class"] == "ReadTimeout"
    assert s.terminal["since_verified_s"] >= .05
    order = [(kwargs["outcome"], kwargs["detail"]["phase"]) for _args, kwargs in records
             if "phase" in (kwargs.get("detail") or {})]
    assert ("recoverable", "authority_grace") in order and order[-1] == ("terminal", "authorization")
    assert order.index(("recoverable", "authority_grace")) < order.index(("terminal", "authorization"))


@pytest.mark.asyncio
async def test_a_verified_denial_is_immediate_inside_the_grace(unit, monkeypatch):
    import time
    s = session()
    records = recorded(monkeypatch)
    monkeypatch.setattr(live, "scope", lambda ctx: "a different digest")
    s.verified_at = time.monotonic()
    await asyncio.wait_for(live.maintain(s, unit), 3)
    assert s.closed and s.close_reason == "permission_denied"
    assert s.terminal["http_status"] == 403 and s.terminal["phase"] == "authorization"
    assert "diagnostic_authority_grace" not in s.counters and not rows(records, "authority_grace")


@pytest.mark.asyncio
async def test_a_slow_but_successful_check_does_not_end_the_conversation(unit, monkeypatch):
    import time
    s = session()
    records = recorded(monkeypatch)

    def slow_scope(ctx):
        time.sleep(.3)
        return "permissions"
    monkeypatch.setattr(live, "scope", slow_scope)
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    before = s.verified_at
    s.reader = asyncio.create_task(live.read_sideband(s, unit))
    await asyncio.sleep(.8)
    assert not s.closed and not s.terminal
    assert s.verified_at > before
    assert "diagnostic_authority_grace" not in s.counters and not rows(records, "authority_grace")
    await live.finish(s)


@pytest.mark.asyncio
async def test_feature_off_and_an_ended_conversation_are_not_tolerated(unit, monkeypatch):
    import time
    s = session()
    recorded(monkeypatch)
    monkeypatch.setattr(live.settings, "gptlive_enabled", False)
    s.verified_at = time.monotonic()
    await asyncio.wait_for(live.maintain(s, unit), 3)
    assert s.closed and s.close_reason == "feature_unavailable"
    assert "diagnostic_authority_grace" not in s.counters
    # the helper itself: only a 503 on an open conversation with the feature on qualifies
    monkeypatch.setattr(live.settings, "gptlive_enabled", True)
    lapse = live.AuthorizationFailure(503, "Permissions could not be verified.")
    lapse.since_verified = 1.0
    assert live.unverifiable(session(), lapse)
    lapse.since_verified = live.AUTHORITY_GRACE + 1
    assert not live.unverifiable(session(), lapse)
    ended = session()
    ended.closed = True
    lapse.since_verified = 1.0
    assert not live.unverifiable(ended, lapse)
    # an ended conversation is refused before any database read
    reads = []
    monkeypatch.setattr(live, "scope", lambda ctx: reads.append(1) or "permissions")
    with pytest.raises(live.AuthorizationFailure) as caught:
        await live.authorized(ended, unit)
    assert caught.value.status_code == 410 and reads == []
    assert not live.unverifiable(session(), caught.value)


@pytest.mark.asyncio
async def test_the_events_poll_tolerates_a_lapse_and_serves_events_from_memory(unit, monkeypatch):
    import time
    s = session()
    live._sessions[s.id] = s
    records = recorded(monkeypatch)
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    arm(monkeypatch, unit, 2, read_timed_out, fail_times=1)
    s.publish("status", text="held in memory")
    s.verified_at = time.monotonic()
    s.last_poll = time.monotonic() - 100
    out = await live.events(s.id, 0, unit)
    assert out["sequence"] == 1 and [e["text"] for e in out["events"]] == ["held in memory"]
    assert not s.closed and not s.terminal
    grace = rows(records, "authority_grace")
    assert len(grace) == 1 and grace[0]["outcome"] == "recoverable"
    assert time.monotonic() - s.last_poll < 5
    await live.finish(s)


@pytest.mark.asyncio
async def test_the_events_poll_beyond_the_grace_is_terminal_as_before(unit, monkeypatch):
    import time
    s = session()
    live._sessions[s.id] = s
    recorded(monkeypatch)
    arm(monkeypatch, unit)
    s.permission = await asyncio.to_thread(live.scope, unit)
    arm(monkeypatch, unit, 1, read_timed_out, fail_times=10 ** 6)
    monkeypatch.setattr(live, "AUTHORITY_GRACE", 0)
    s.verified_at = time.monotonic() - 100   # past the grace even on a coarse clock
    with pytest.raises(live.AuthorizationFailure):
        await live.events(s.id, 0, unit)
    assert s.closed and s.close_reason == "permission_check_unavailable"
    assert s.terminal["phase"] == "event_poll" and s.terminal["http_status"] == 503


@pytest.mark.asyncio
async def test_the_poll_watchdog_counts_the_browser_not_the_database(unit, monkeypatch):
    import threading
    import time
    s = session()
    live._sessions[s.id] = s
    recorded(monkeypatch)
    seen = {}

    def blocked_scope(ctx):
        seen["age"] = time.monotonic() - s.last_poll   # read_sideband closes the session past 30 s
        threading.Event().wait(.2)
        return "permissions"
    monkeypatch.setattr(live, "scope", blocked_scope)
    s.last_poll = time.monotonic() - 100
    await live.events(s.id, 0, unit)
    assert seen["age"] < 5
    await live.finish(s)


@pytest.mark.asyncio
async def test_a_lapse_inside_the_task_refresh_is_tolerated_too(unit, monkeypatch):
    s = session()
    recorded(monkeypatch)
    calls = {"n": 0}

    def lapse_on_the_refresh_check(ctx):
        calls["n"] += 1
        if calls["n"] == 2:   # the first check is maintain's own, the second is task_updates'
            raise httpx.ReadTimeout("read timed out")
        return "permissions"
    monkeypatch.setattr(live, "scope", lapse_on_the_refresh_check)
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    s.reader = asyncio.create_task(live.read_sideband(s, unit))
    await asyncio.sleep(.3)
    assert not s.closed and not s.terminal
    assert s.counters["diagnostic_authority_grace"] == 1 and "task_update_error" not in s.counters
    assert calls["n"] >= 4
    await live.finish(s)


@pytest.mark.asyncio
async def test_a_lapse_during_a_delegation_fails_that_turn_closed_without_ending_the_conversation(unit, monkeypatch):
    s = session()
    traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    calls = {"n": 0}

    def lapse_once(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("read timed out")
        return "permissions"
    monkeypatch.setattr(live, "scope", lapse_once)
    await live.handle_event(s, fragment("Read fixture CRM"), unit)
    await live.handle_event(s, delegation(), unit)
    await asyncio.gather(*list(s.jobs))
    assert not s.closed and not s.terminal
    assert s.delegations["d1"]["state"] == "unconfirmed" and s.counters["backend_unconfirmed"] == 1
    assert not [e for e in s.events if e["kind"] == "result"]
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_unconfirmed"]
    assert stages and stages[0]["reason"] == "http_503"
    await live.finish(s)


async def speak(s, unit, words, delivery):
    """Deliver output text the way the provider does: sentence by sentence straight to the guard
    ("direct"), or one character at a time through the transcript reader ("fragmented")."""
    if delivery == "direct":
        for sentence in re.split(r"(?<=[.!?])\s+", words):
            await live.monitor_output(s, sentence, unit)
        return
    for i, char in enumerate(words):
        await live.handle_event(s, {**fragment(char, i, i + 1, f"c{i}"),
                                   "type": "session.output_transcript.delta"}, unit)


def guards_of(s):
    return len([e for e in s.events if e["kind"] == "guard"])


# A backend sentence the voice may paraphrase: the rig's "cancelled jobs" answer, in neutral nouns.
CANCELLED_ANSWER = ("So Tom had two jobs booked for last Tuesday the 13th — a first check at 8am, quoted at "
                    "forty-five pounds, and a second job at 11am, quoted at a hundred and twenty. "
                    "Both were cancelled. There are no invoices for either.")

# Spoken as a claim of the assistant's own action: each one must keep the guard.
STILL_GUARDED = [
    ("I've sent it.", None), ("I sent it.", None), ("Done, I've booked her in.", None),
    ("Booked her in for Friday.", None), ("It's been sent.", None), ("That's sent now.", None),
    ("The invoice was sent.", None), ("Both invoices are sent.", None), ("All sent.", None),
    ("Everything's updated.", None), ("I've cancelled the booking.", None), ("Cancelled.", None),
    ("No problem, cancelled.", None), ("I have now sent both.", None),
    ("Leah is booked in for Friday.", None), ("Alright, I've booked it.", None),
    ("The note was updated.", None), ("I charged the invoice.", None),
    # a noun or name plus 's for "is", and a dash or colon after the lead-in
    ("The booking's cancelled.", None), ("The invoice’s sent now.", None),
    ("Tom's booked in for Tuesday.", None), ("Done — booked.", None), ("Okay: cancelled.", None),
    # a capitalised word that opens the clause, with its complement
    ("Booked for Friday.", None), ("Sent to Tom.", None), ("Cancelled for tomorrow.", None),
    ("Okay. Booked in for Friday.", None),
    # a capitalised subject followed by a name, an adverb, a preposition or the end
    ("Priya cancelled her booking.", None), ("Email sent just now.", None), ("Bob charged Alice.", None),
    ("Invoice sent.", None), ("Tom booked in.", None),
    # a backend sentence that does not support the same claim
    ("Bob charged Alice.", "Alice charged Bob."),
    ("I've sent it.", "I haven't sent it."),
    ("I've sent it.", "The draft is ready to send."),
    ("INV-1002 was sent.", "INV-1001 was sent."),
    # a paraphrase of the right shape, but the backend denied it, named another record or never said it
    ("INV-1001 was sent on the 5th.", "INV-1001 was not sent."),
    ("INV-1001 was sent on the 5th.", "Neither INV-1001 nor INV-1002 was sent."),
    ("INV-1002 was sent on the 5th of October.", "INV-1001 sent 5 Oct"),
    ("INV-1001 was sent on the 5th.", "INV-1001 is ready to send on the 5th."),
    ("INV-1002 was sent.", "INV-1001 was sent, INV-1002 was not sent."),
    ("I've sent it.", "INV-1001 sent 5 Oct"),
    ("I've booked both.", "Both were cancelled."),
    ("Both were booked.", CANCELLED_ANSWER),
    # future, conditional or waiting wording in the backend is not a record of anything done
    ("INV-1001 was sent.", "INV-1001 will be sent once you confirm."),
    ("INV-1001 was sent.", "Once you confirm, INV-1001 is sent."),
    ("INV-1001 was sent.", "INV-1001 is ready to be sent."),
    ("INV-1001 was sent on the 5th.", "Waiting for you to confirm before INV-1001 is sent on the 5th."),
    # a question records nothing; a condition after the word is not a record either
    ("INV-1001 was sent.", "INV-1001 sent? No."),
    ("Leah's booked.", "Leah booked? No, nothing's booked."),
    ("The booking's cancelled.", "The booking is cancelled only once you confirm."),
    ("The email's sent.", "The email is sent when you tap Confirm."),
    ("The email's sent.", "The email is sent, but only if you confirm."),
    ("The email's sent.", "The email is sent as soon as you tap Confirm."),
    ("It's sent.", "It's sent when you tap Confirm."),
]


def test_a_bare_status_word_is_still_a_claim_candidate():
    # "Sent." is one of the phone's own fixed lines, so the guard never fires on that exact text;
    # the candidate test itself must still see it as a claim.
    assert live.claim_tokens("Sent.") == ("sent", ())
    assert live.claim_tokens("Done, cancelled.") is not None
    assert live.claim_tokens("Sent 5 Oct.") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["direct", "fragmented"])
@pytest.mark.parametrize("words,backend", STILL_GUARDED)
async def test_action_claims_still_guard_without_support(unit, words, backend, delivery):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session()
    if backend:
        await s.send("session.commentary.append", backend)
    await speak(s, unit, words, delivery)
    assert guards_of(s) == 1
    assert len([e for e in s.socket.sent if e["type"] == "session.instructions.append"]) == 1
    _SPOKEN.clear()


# A status word describing a record, not claiming an action: none of these may stop the voice.
DESCRIPTIVE = [
    ("I'll check your unpaid sent bills.", None),
    ("Okay. Tom had two jobs booked for last Tuesday the thirteenth.", CANCELLED_ANSWER),
    ("Tom had two jobs booked for last Tuesday the thirteenth.", None),
    ("The diary looks fully booked for the next two weeks, so there aren't any customer slots to rebook her into.", None),
    ("I can block that off so nothing gets booked in.", None),
    ("That's a sent invoice for a hundred and forty pounds.", None),
    ("There's nothing booked for the rest of this week.", None),
    ("Nothing's booked in for Thursday.", None),
    ("Tom's booked jobs are listed below.", None),
    ("What's sent is listed in chat.", None),
    ("Two jobs, booked for Tuesday, quoted at forty-five pounds.", None),
    ("The sent invoices are listed in chat.", None),
    ("Two cancelled bookings show for Tom.", None),
    ("Your booked jobs are in chat.", None),
    ("Invoices sent 5 Oct.", None),
    ("Tom has nothing booked this week.", None),
    ("Tom has nothing booked the whole week.", None),
    ("Invoice INV-1001, sent 5 Oct, due 19th.", None),
    ("Both were cancelled.", CANCELLED_ANSWER),
    # claim position, but the backend said the same thing in other words
    ("INV-1001 was sent on the 5th of October.", "INV-1001 sent 5 Oct"),
    ("Yes, sent the 5th, viewed, not paid.", "INV-1001: sent 5 Oct, viewed, not paid."),
    ("Both of them were cancelled.", CANCELLED_ANSWER),
    ("Alright, the first check and the second job were both cancelled.",
     "The first check and the second job were both cancelled."),
    ("The quote for forty-five pounds was sent.", "The quote for £45 was sent."),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["direct", "fragmented"])
@pytest.mark.parametrize("words,backend", DESCRIPTIVE)
async def test_descriptive_status_words_do_not_guard(unit, words, backend, delivery):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session()
    if backend:
        await s.send("session.commentary.append", backend)
    await speak(s, unit, words, delivery)
    assert guards_of(s) == 0 and not s.counters.get("output_guard")
    assert not [e for e in s.socket.sent if e["type"] == "session.instructions.append"]
    _SPOKEN.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["direct", "fragmented"])
@pytest.mark.parametrize("words,guards", [
    ("Nothing's booked in for Thursday.", 0), ("Nothing’s booked in for Thursday.", 0),
    ("Nothing's been sent.", 0), ("Neither's booked yet.", 0),
    ("Nothing's wrong, I've sent it.", 1),
])
async def test_nothings_contraction_is_a_denial(unit, words, guards, delivery):
    from app.concierge_routes import _SPOKEN
    _SPOKEN.clear(); s = session()
    await speak(s, unit, words, delivery)
    assert guards_of(s) == guards
    _SPOKEN.clear()


@pytest.mark.parametrize("text,expected", [
    ("thirteenth", ["13"]), ("the 13th of October", ["13", "oct"]), ("the thirteenth of October", ["13", "oct"]),
    ("a hundred and forty pounds", ["140"]), ("£140", ["140"]), ("forty-five", ["45"]), ("forty five", ["45"]),
    ("twenty-first", ["21"]), ("two hundred and five", ["205"]), ("one hundred", ["100"]),
    ("£252.50", ["252.50"]), ("£1,200", ["1200"]), ("5th", ["5"]), ("8am", ["8am"]),
    ("INV-1002 was sent on the 5th", ["inv", "1002", "sent", "5"]),
    ("Nothing's booked in", ["nothing", "booked"]), ("I've cancelled it", ["cancelled"]),
    ("It's canceled, honestly", ["cancelled"]), ("Sept 5", ["sep", "5"]),
    ("a hundred and a", ["100"]), ("", []),
])
def test_normalise_words_table(text, expected):
    # Spelling of numbers and dates is normalised; no digit is ever dropped; the mapping is deterministic.
    assert live._normalise_words(text) == expected


@pytest.mark.asyncio
async def test_supported_by_server_needs_the_same_predicate_in_order_and_not_denied(unit):
    from app.concierge_routes import _SPOKEN, _remember_speech
    _SPOKEN.clear(); s = session()

    def supported(clause, *backend):
        s.speech_context = list(backend)
        candidate = live.claim_tokens(clause) or ("booked", ())
        return live._supported_by_server(s, unit, clause, candidate)

    n7 = "Tom had two jobs booked for last Tuesday the thirteenth."
    assert supported(n7, CANCELLED_ANSWER)
    assert supported("INV-1001 was sent on the 5th of October.", "INV-1001 sent 5 Oct")
    # the backend denied it, said another word, another record, the words in another order, or nothing
    assert not supported("INV-1001 was sent on the 5th.", "INV-1001 was not sent on the 5th.")
    assert not supported("INV-1001 was sent on the 5th.", "INV-1001 is ready to send on the 5th.")
    assert not supported("INV-1002 was sent on the 5th.", "INV-1001 was sent on the 5th.")
    assert not supported("Bob charged Alice.", "Alice charged Bob.")
    assert not supported(n7)
    # a clause that names nothing beyond the completion word keeps the exact-words rule
    assert not supported("I've sent it.", "INV-1001 sent 5 Oct")
    # a multi-word candidate is never widened
    assert not live._supported_by_server(s, unit, "sent and charged INV-1001", ("sent", "and", "charged"))
    # the server's own spoken text counts too, within the same trust boundary as _server_said
    s.speech_context = []
    _remember_speech(unit, "INV-1001 sent 5 Oct")
    assert live._supported_by_server(s, unit, "INV-1001 was sent on the 5th.", ("sent", ("inv", "1001", "on", "5th")))
    _SPOKEN.clear()
    assert not live._supported_by_server(s, unit, "INV-1001 was sent on the 5th.", ("sent", ("inv", "1001", "on", "5th")))


@pytest.mark.asyncio
async def test_a_turn_cancelled_before_commit_is_reported_cancelled_not_unconfirmed(unit, monkeypatch):
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        raise live.BackendCancelled("Business turn cancelled")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await entered.wait()
    await live.handle_event(s, fragment("Thursday, not Friday.", 110, 200, "correction"), unit)
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "cancelled"
    assert s.counters["backend_cancelled"] == 1 and "backend_unconfirmed" not in s.counters
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_cancelled"]
    assert len(stages) == 1 and stages[0]["reason"] == "new_input" and stages[0]["turn_id"]
    status = [e for e in s.events if e["kind"] == "status"]
    assert len(status) == 1 and status[0]["reason"] == "backend_cancelled"
    assert status[0]["text"].startswith("Stopped before anything ran") and "Nothing was changed" in status[0]["text"]
    assert not any("unconfirmed" in json.dumps(e) for e in s.events)
    said = [m for m in s.socket.sent if m["type"] == "session.commentary.append"]
    assert len(said) == 1 and said[0]["delegation_id"] == "d1"
    assert "stopped before anything ran" in said[0]["content"] and "nothing was changed" in said[0]["content"]
    assert [e["state"] for e in s.events if e["kind"] == "activity"][-1] == "finished"
    assert not any(e["kind"] == "result" for e in s.events)


@pytest.mark.asyncio
async def test_the_stop_commentary_tells_the_voice_not_to_announce_it_or_invite_a_repeat(unit, monkeypatch):
    # Rig N20 (15 Oct): the voice said "I paused that before anything ran ... just say so" to an owner whose newer words
    # WERE the request. The screen still says what happened; the voice is told to carry on and not to announce a stop.
    s = session(); entered = asyncio.Event(); release = asyncio.Event()

    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        raise live.BackendCancelled("Business turn cancelled")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await entered.wait()
    await live.handle_event(s, fragment("Thursday, not Friday.", 110, 200, "correction"), unit)
    release.set(); await asyncio.gather(*list(s.jobs))
    said = [m["content"].lower() for m in s.socket.sent if m["type"] == "session.commentary.append"]
    assert len(said) == 1
    content = said[0]
    # it no longer hands the voice a line that invites a repeat ...
    for invitation in ("say it again", "say so", "still want", "ask again", "repeat"):
        assert invitation not in content, invitation
    # ... it says not to announce the stop, not to claim the work was done, and to treat the newer words as the request
    assert "do not announce this" in content and "do not say it was done" in content
    assert "newer words as the current request" in content and "carry on" in content
    assert "nothing was changed" in content
    # the screen is unchanged: it still says what happened and how to ask again
    status = [e for e in s.events if e["kind"] == "status"]
    assert status[0]["text"] == ("Stopped before anything ran — your newer words took over. "
                                 "Nothing was changed; say it again if you still want it.")


@pytest.mark.asyncio
async def test_a_discarded_turn_is_cancelled_quietly(unit, monkeypatch):
    # The Discard control has already told the user what it did: no "newer words" status or commentary.
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        raise live.BackendCancelled("Business turn cancelled")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await entered.wait()
    s.generation += 1   # what the discard control does before it cancels the captured turn
    release.set(); await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "cancelled"
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_cancelled"]
    assert len(stages) == 1 and stages[0]["reason"] == "discard"
    assert not [e for e in s.events if e["kind"] == "status"]
    assert not [m for m in s.socket.sent if m["type"] == "session.commentary.append"]


@pytest.mark.asyncio
async def test_the_cancelled_frame_from_voice_chat_maps_to_backend_cancelled(unit, monkeypatch):
    from app import concierge_routes as routes

    def chat(*frames):
        async def voice_chat(request, ctx):
            async def body():
                for frame in frames:
                    yield ("data: " + json.dumps(frame) + "\n\n").encode()
            return SimpleNamespace(status_code=200, body_iterator=body())
        return voice_chat
    voice = str(uuid.uuid4())
    monkeypatch.setattr(routes, "voice_chat", chat({"t": "tool"}, {"t": "cancelled"}))
    with pytest.raises(live.BackendCancelled):
        await live.run_backend(unit, "Check diary", [], "turn", voice)
    # an error frame stays a plain, unconfirmed failure
    monkeypatch.setattr(routes, "voice_chat", chat({"t": "error"}))
    with pytest.raises(RuntimeError) as failed:
        await live.run_backend(unit, "Check diary", [], "turn", voice)
    assert type(failed.value) is RuntimeError
    monkeypatch.setattr(routes, "voice_chat", chat({"t": "done", "reply": "Fixture read only"}))
    assert (await live.run_backend(unit, "Check diary", [], "turn", voice))["reply"] == "Fixture read only"


@pytest.mark.asyncio
@pytest.mark.parametrize("words", ["Actually, never mind", "Never mind.", "Oh sorry, forget it", "Cancel that",
                                   "[cough] ignore that please", "No no, scrap that", "Don’t worry about it, thanks"])
async def test_a_bare_retraction_never_reaches_the_backend(unit, monkeypatch, words):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def forbidden(*a):
        raise AssertionError("A retraction must not start a business turn")
    monkeypatch.setattr(live, "run_backend", forbidden)
    await live.handle_event(s, fragment(words, 0, 90), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.delegations["d1"]["state"] == "clarification"
    rows = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed"]
    assert len(rows) == 1 and rows[0]["reason"] == "retraction"
    status = [e for e in s.events if e["kind"] == "status"]
    assert len(status) == 1 and status[0]["text"].startswith("Okay, leaving that") and "tap Cancel" in status[0]["text"]
    assert not any(e["kind"] in {"question", "result", "activity"} for e in s.events)
    assert "backend_requested" not in s.counters and s.consumed_ms == 90


@pytest.mark.asyncio
@pytest.mark.parametrize("words", ["Never mind the diary, check the invoices", "Forget it, send both",
                                   "Cancel that booking for Friday", "Cancel the one for Friday"])
async def test_a_retraction_inside_a_longer_instruction_still_reaches_the_backend(unit, monkeypatch, words):
    s = session(); seen = []

    async def backend(ctx, question, history, turn, voice):
        seen.append(question)
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(words, 0, 90), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    assert seen == [words] and s.delegations["d1"]["state"] == "complete"


POLITE_GO_AHEADS = ["Right, yeah, go on then, do it. Thanks!", "Go ahead", "Go on then", "That's fine",
                    "That’s right, thanks", "Sounds good", "Fine by me", "Not now", "Not yet, thanks",
                    "Leave it", "Don't send it", "Carry on then", "Crack on", "Yes please",
                    "Sure, go for it, cheers"]


@pytest.mark.parametrize("card", [{"proposal": "p1"}, {"draft": "log1"}])
@pytest.mark.parametrize("words", POLITE_GO_AHEADS)
def test_a_polite_go_ahead_beside_a_live_card_gets_the_card_clarification(unit, words, card):
    s = _tagged_window(fragment(words, 0, 90))
    s.approval = dict(card)
    with pytest.raises(live.ContextUnconfirmed) as e:
        live.snapshot(s, {"offset": 100})
    assert e.value.reason == "approval_requires_card"
    assert "Confirm or Cancel card" in str(e.value)


@pytest.mark.parametrize("words", POLITE_GO_AHEADS)
def test_the_same_words_with_no_card_are_an_instruction_for_the_backend(unit, words):
    s = _tagged_window(fragment(words, 0, 90))
    for approval in ({}, {"proposal": None, "draft": None}):
        s.approval = approval
        assert live.snapshot(s, {"offset": 100}) == (words, 90)


@pytest.mark.parametrize("words", ["Yeah, send both", "Go ahead and send both invoices", "Send it to Tom",
                                   "Do it for Friday", "Leave it with me and check the diary", "Yes check the diary"])
def test_words_that_name_a_scope_reach_the_backend_even_with_a_card_live(unit, words):
    s = _tagged_window(fragment(words, 0, 90))
    s.approval = {"proposal": "p1"}
    assert live.snapshot(s, {"offset": 100}) == (words, 90)


@pytest.mark.parametrize("words", ["yes", "Yeah.", "No", "Nope!", "confirm", "send it", "Do it."])
@pytest.mark.parametrize("card", [{}, {"proposal": "p1"}])
def test_a_bare_yes_or_no_is_always_sent_back_to_the_card(unit, words, card):
    s = _tagged_window(fragment(words, 0, 90))
    s.approval = dict(card)
    with pytest.raises(live.ContextUnconfirmed) as e:
        live.snapshot(s, {"offset": 100})
    assert e.value.reason == "approval_requires_card"


@pytest.mark.asyncio
async def test_a_spoken_go_ahead_beside_a_card_starts_no_backend_turn(unit, monkeypatch):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def forbidden(*a):
        raise AssertionError("A spoken go-ahead must not start a business turn")
    monkeypatch.setattr(live, "run_backend", forbidden)
    s.approval = {"proposal": "p1"}
    await live.handle_event(s, fragment("Right, yeah, go on then, do it. Thanks!", 0, 90), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    rows = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "context_unconfirmed"]
    assert len(rows) == 1 and rows[0]["reason"] == "approval_requires_card"
    assert s.approval == {"proposal": "p1"}      # the card is untouched: only a tap settles it
    assert not any(e["kind"] in {"question", "result"} for e in s.events)


def allowance_session(limit):
    s = session()
    s.allowance_enabled = True
    s.allowance_connected = True
    s.allowance_limit_seconds = limit
    return s


async def beat(s, unit, monkeypatch, used):
    """One server heartbeat answering ok with this much of the day used, through the real maintain loop."""
    import time
    from app import db
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", lambda *a, **k: {"ok": True, "used_seconds": used})
    s.allowance_heartbeat_at = time.monotonic() - 6
    await asyncio.sleep(.15)


@pytest.mark.asyncio
async def test_the_heartbeat_warns_once_when_about_a_minute_is_left(unit, monkeypatch):
    import time
    from app import db
    recorded(monkeypatch)
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    s = allowance_session(600)
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", lambda *a, **k: {"ok": True, "used_seconds": 500})
    s.allowance_heartbeat_at = time.monotonic() - 6
    s.maintenance = asyncio.create_task(live.maintain(s, unit))
    await asyncio.sleep(.15)
    assert not s.allowance_warned and not [e for e in s.events if e["kind"] == "status"]   # 100 s left: nothing yet
    await beat(s, unit, monkeypatch, 545)
    warnings = [e for e in s.events if e["kind"] == "status"]
    assert s.allowance_warned and len(warnings) == 1 and warnings[0]["reason"] == "allowance_warning"
    assert warnings[0]["text"] == "About a minute of live voice left today."
    said = [m for m in s.socket.sent if m["type"] == "session.commentary.append"]
    assert len(said) == 1 and "about one minute left" in said[0]["content"]
    assert any("about one minute left" in line for line in s.speech_context)
    await beat(s, unit, monkeypatch, 550)
    await beat(s, unit, monkeypatch, 559)
    assert len([e for e in s.events if e["kind"] == "status"]) == 1
    assert len([m for m in s.socket.sent if m["type"] == "session.commentary.append"]) == 1
    assert not s.closed
    await live.finish(s)


@pytest.mark.asyncio
async def test_delegation_seen_records_the_lag_fields(unit, monkeypatch):
    s = session(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def backend(ctx, question, history, turn, voice):
        return {"reply": "Fixture read only"}
    monkeypatch.setattr(live, "run_backend", backend)
    s.created -= 12.5   # the session began 12.5 s before the delegation arrived
    await live.handle_event(s, fragment("Check the diary.", 1600, 7600), unit)
    await live.handle_event(s, delegation(offset=8600), unit)
    await asyncio.gather(*list(s.jobs))
    seen = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "delegation_seen"]
    assert len(seen) == 1
    assert seen[0]["delegation_offset_ms"] == 8600 and seen[0]["first_input_ms"] == 1600
    assert type(seen[0]["seen_after_ms"]) is int and 12500 <= seen[0]["seen_after_ms"] < 20000
    # no words before the delegation: the first-input marker says so rather than guessing
    s, traces = session(), []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))
    await live.handle_event(s, delegation(offset=100), unit)
    await asyncio.gather(*list(s.jobs))
    seen = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "delegation_seen"]
    assert seen[0]["first_input_ms"] == -1 and seen[0]["seen_after_ms"] >= 0


@pytest.mark.asyncio
async def test_one_failed_heartbeat_inside_the_grace_keeps_the_session(unit, monkeypatch):
    import time
    from app import db
    records = recorded(monkeypatch)
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    calls = []

    def heartbeat(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out")
        return {"ok": True, "used_seconds": 10}
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", heartbeat)
    s = allowance_session(600)
    s.allowance_heartbeat_at = time.monotonic() - 6
    s.maintenance = asyncio.create_task(live.maintain(s, unit))
    await asyncio.sleep(.3)
    assert not s.closed and not s.terminal and len(calls) == 2
    assert s.counters["diagnostic_allowance_grace"] == 1
    grace = rows(records, "allowance_grace")
    assert len(grace) == 1 and grace[0]["outcome"] == "recoverable"
    assert grace[0]["detail"]["error_class"] == "ReadTimeout"
    assert "read timed out" not in json.dumps(records, default=str)
    assert time.monotonic() - s.allowance_heartbeat_at < 5      # the stamp moved on the beat that worked
    await live.finish(s)


@pytest.mark.asyncio
async def test_failed_heartbeats_beyond_the_grace_end_the_session_as_allowance_unavailable(unit, monkeypatch):
    import time
    from app import db
    records = recorded(monkeypatch)
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    monkeypatch.setattr(live, "AUTHORITY_GRACE", 6.15)

    def failing(*a, **k):
        raise httpx.ReadTimeout("read timed out")
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", failing)
    s = allowance_session(600)
    s.allowance_heartbeat_at = time.monotonic() - 5.9   # the first failure is inside the grace, later ones are not
    await asyncio.wait_for(live.maintain(s, unit), 5)
    assert s.closed and s.close_reason == "allowance_unavailable"
    assert s.terminal["phase"] == "allowance_heartbeat" and s.terminal["error_class"] == "ReadTimeout"
    order = [(kwargs["outcome"], kwargs["detail"]["phase"]) for _args, kwargs in records
             if "phase" in (kwargs.get("detail") or {})]
    assert ("recoverable", "allowance_grace") in order and order[-1] == ("terminal", "allowance_heartbeat")
    assert order.index(("recoverable", "allowance_grace")) < order.index(("terminal", "allowance_heartbeat"))
    assert len(rows(records, "allowance_grace")) <= 3       # the trace is bounded however long it fails


@pytest.mark.asyncio
async def test_a_verified_limit_still_ends_the_session_at_once_inside_the_grace(unit, monkeypatch):
    import time
    from app import db
    records = recorded(monkeypatch)
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(db, "heartbeat_concierge_live_session",
                        lambda *a, **k: {"ok": False, "ended": True, "reason": "limit_reached"})
    s = allowance_session(600)
    s.allowance_heartbeat_at = time.monotonic() - 5.5       # well inside the grace
    await asyncio.wait_for(live.maintain(s, unit), 3)
    assert s.closed and s.close_reason == "limit_reached" and s.terminal["phase"] == "allowance_heartbeat"
    assert "diagnostic_allowance_grace" not in s.counters and not rows(records, "allowance_grace")


@pytest.mark.asyncio
async def test_an_unlimited_day_is_never_warned_and_the_cut_is_unchanged(unit, monkeypatch):
    import time
    from app import db
    recorded(monkeypatch)
    monkeypatch.setattr(db, "close_concierge_live_session", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(live, "MAINTENANCE_INTERVAL", .01)
    s = allowance_session(0)
    monkeypatch.setattr(db, "heartbeat_concierge_live_session", lambda *a, **k: {"ok": True, "used_seconds": 99999})
    s.allowance_heartbeat_at = time.monotonic() - 6
    s.maintenance = asyncio.create_task(live.maintain(s, unit))
    await asyncio.sleep(.15)
    assert not s.allowance_warned and not [e for e in s.events if e["kind"] == "status"]
    await live.finish(s)
    # a verified limit still ends the conversation at once, with no warning on the way
    s = allowance_session(600)
    monkeypatch.setattr(db, "heartbeat_concierge_live_session",
                        lambda *a, **k: {"ok": False, "ended": True, "reason": "limit_reached"})
    s.allowance_heartbeat_at = time.monotonic() - 6
    await asyncio.wait_for(live.maintain(s, unit), 3)
    assert s.closed and s.close_reason == "limit_reached" and not s.allowance_warned
    assert not [e for e in s.events if e["kind"] == "status"]


@pytest.mark.parametrize("text,expected", [
    ("sent.", True), (" sent.", True), ("  sent.", True), ("Ok sent.", True), ("OK. sent.", True),
    ("Okay, sent.", True), ("Okay okay sent.", True), ("Right. sent.", True), ("Yes, sent.", True),
    ("Yeah sent.", True), ("Done, sent.", True), ("Done — sent.", True), ("Done – sent.", True),
    ("Done: sent.", True), ("Done. sent.", True), ("Sorted. sent.", True), ("All sent.", True),
    ("All done, sent.", True), ("Ok right sent.", True), ("Yes yes sent.", True),
    # not a lead-in the rule knows: unchanged
    (", sent.", False), ("... sent.", False), ("Alright, sent.", False),
    ("okay sent 5 Oct", False), ("Done, booked for Friday.", False),
])
def test_bare_lead_in_shapes_keep_their_answers(text, expected):
    assert bool(live.claim_tokens(text)) is expected


@pytest.mark.parametrize("text", [
    " " * 1600 + "x sent", "'" * 800 + "s sent", "I" + " " * 1600 + "sent", 'I said "' + "x" * 1500 + '" sent it',
    "—" * 1600 + " sent", "okay " * 320 + "sent", "not " * 400 + "sent", "nothing " * 200 + "booked",
], ids=["spaces", "quotes", "subject-then-spaces", "masked-quote", "dashes", "okays", "nots", "nothings"])
def test_claim_tokens_is_linear_on_long_runs(text):
    # A quoted span is masked to a run of spaces, so a long run must not backtrack: the old lead-in
    # pattern took 0.85 s at 800 characters and 6 s at 1,600 on the single-worker event loop.
    import time
    best = min((lambda start: (live.claim_tokens(text), time.perf_counter() - start)[1])(time.perf_counter())
               for _ in range(3))
    assert best < .05


@pytest.mark.parametrize("text,expected", [
    ("the quick brown fox " * 30 + "I've sent it.", True),
    ("the quick brown fox " * 30 + "Bob charged Alice.", True),
    ("the quick brown fox " * 30 + "the booking's cancelled.", True),
    ("the quick brown fox " * 30 + "unpaid sent bills.", False),
    ("the quick brown fox " * 30 + "it was just sent.", True),
    ("x " * 60 + "I" + " " * 300 + "sent it.", True),
    # the subject sits on one side of a long masked quote and the word on the other
    ('I "' + "x" * 200 + '" sent it.', True),
    ('Tom had two jobs "' + "x" * 200 + '" booked for Tuesday.', False),
])
def test_claim_position_only_needs_the_last_few_words(text, expected):
    assert bool(live.claim_tokens(text)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("how,reason", [("discard", "discard"), ("close", "close")])
async def test_a_cancel_by_discard_or_close_names_its_reason(unit, monkeypatch, how, reason):
    s = session(); entered = asyncio.Event(); release = asyncio.Event(); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **k: traces.append(k))

    async def backend(ctx, question, history, turn, voice):
        entered.set(); await release.wait()
        raise live.BackendCancelled("Business turn interrupted")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment(), unit)
    await live.handle_event(s, delegation(), unit)
    await entered.wait()
    s.generation += 1
    if how == "close":
        s.closed = True
    release.set(); await asyncio.gather(*list(s.jobs))
    stages = [t["detail"] for t in traces if (t.get("detail") or {}).get("stage") == "backend_cancelled"]
    assert len(stages) == 1 and stages[0]["reason"] == reason
    assert not [e for e in s.events if e["kind"] == "status"]


@pytest.mark.parametrize("words", ["Tom has nothing booked this week.", "You have nothing booked the whole week.",
                                   "There is nothing sent to either of them."])
def test_nothing_directly_before_the_word_is_a_denial(words):
    # "nothing booked" denies it; "Nothing prevented it: I sent ..." does not (the word sits further on)
    assert live.claim_tokens(words) is None
    assert live.claim_tokens("Nothing prevented it: I sent both invoices.") is not None
