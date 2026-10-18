"""The Debrief button in GPT-Live (native) mode: src/app/gptlive.py `control` with
operation "debrief".

The data channel allows no client commands, so the button asks the server; the
server runs the same backend turn a delegation would and hands the verified
reply to the native voice as commentary. These tests pin that it does NOT
silence the voice, that it re-checks authority like a delegation does, and that
newer words from the owner still win.

Run: python tests/run_offline.py tests/test_debrief_walk_gptlive.py -q
"""
import asyncio
import time

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import db, debrief_walk as dw, gptlive as live, voice_turn_state
from tests.test_gptlive import unit, session, fragment, delegation  # noqa: F401  (fixtures and helpers)


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """No membership row = the account's own owner."""
    monkeypatch.setattr(db, "get_member", lambda *a, **k: None)


def result(reply="Morning. Five things need you.", **extra):
    return {"t": "done", "reply": reply, "links": [],
            "debrief": {"step": 1, "of": 5, "label": "Drafts waiting", "finished": False}, **extra}


@pytest.mark.asyncio
async def test_the_button_runs_the_backend_turn_and_the_voice_relays_the_reply(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; calls = []

    async def backend(ctx, question, history, turn, voice, debrief=""):
        calls.append((question, voice, debrief))
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    out = await live.control(live.Control(session=s.id, operation="debrief", step="start"), unit)
    assert out == {"ok": True, "status": "debrief_requested"}
    await asyncio.gather(*list(s.jobs))
    assert calls == [(dw.START_LINE, s.voice_session, "start")]
    kinds = [e["kind"] for e in s.events]
    assert "question" in kinds and "result" in kinds
    question = next(e for e in s.events if e["kind"] == "question")
    assert question["text"] == dw.START_LINE
    published = next(e for e in s.events if e["kind"] == "result")
    assert published["payload"]["debrief"]["label"] == "Drafts waiting"
    # the verified reply goes to the voice as commentary, with no provider delegation id…
    last = s.socket.sent[-1]
    assert last["type"] == "session.commentary.append" and last["content"] == "Morning. Five things need you."
    assert last["delegation_id"] is None
    # …and starting the debrief never tells the voice to stop talking
    assert not any(m["type"] == "session.instructions.append" for m in s.socket.sent)
    assert [m["role"] for m in s.history] == ["user", "assistant"]
    assert "Morning. Five things need you." in s.speech_context


@pytest.mark.asyncio
async def test_the_button_is_an_admission_and_drops_a_request_carried_from_before(unit, monkeypatch):
    # a request stood down by newer speech is carried only into the next delegation, never past the Debrief button
    s = session(); live._sessions[s.id] = s
    s.carry = {"text": "Can you just bin it", "start_ms": 0, "end_ms": 700, "event_ids": ["first-half"], "turn": "t"}

    async def backend(ctx, question, history, turn, voice, debrief=""):
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief", step="start"), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.carry == {} and next(e for e in s.events if e["kind"] == "question")["text"] == dw.START_LINE


@pytest.mark.asyncio
async def test_next_sends_the_next_line(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; calls = []

    async def backend(ctx, question, history, turn, voice, debrief=""):
        calls.append((question, debrief))
        return result("Next up, money.")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief", step="next"), unit)
    await asyncio.gather(*list(s.jobs))
    assert calls == [(dw.NEXT_LINE, "next")]


@pytest.mark.asyncio
async def test_a_prepared_card_is_never_spoken_as_done(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s

    async def backend(ctx, question, history, turn, voice, debrief=""):
        return result("I've drafted it.", proposal={"proposal_id": "p1"})
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    spoken = s.socket.sent[-1]["content"]
    assert spoken.startswith("Prepared for review, not executed. Use the visible card to confirm.")
    assert s.approval["proposal"] == "p1"


@pytest.mark.asyncio
async def test_staff_are_refused_and_nothing_runs(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s
    monkeypatch.setattr(db, "get_member", lambda *a, **k: {"role": "staff", "active": True, "scopes": {}})

    async def forbidden(*a, **k):
        raise AssertionError("a refused debrief must not reach the backend")
    monkeypatch.setattr(live, "run_backend", forbidden)
    with pytest.raises(HTTPException) as refused:
        await live.control(live.Control(session=s.id, operation="debrief"), unit)
    assert refused.value.status_code == 403
    assert not s.jobs and not s.delegations


@pytest.mark.asyncio
async def test_someone_elses_conversation_is_not_reachable(unit):
    s = session(); s.user = "someone-else"; live._sessions[s.id] = s
    with pytest.raises(HTTPException) as missing:
        await live.control(live.Control(session=s.id, operation="debrief"), unit)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_newer_words_from_the_owner_take_priority_over_the_result(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s

    async def backend(ctx, question, history, turn, voice, debrief=""):
        # the owner starts talking while the backend works
        s.fragments.append({"event_id": "late", "start_ms": 500, "end_ms": 600,
                            "delta": "hold on a moment", "received": time.monotonic()})
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    assert not any(e["kind"] == "result" for e in s.events)
    assert any("newer words take priority" in e.get("text", "") for e in s.events)
    assert not any(m["type"] == "session.commentary.append" for m in s.socket.sent)


@pytest.mark.asyncio
async def test_background_words_do_not_hide_the_debrief_result(unit, monkeypatch):
    # The same rule as a delegated turn: talk in the room is held, only a correction or stop cue
    # (above) or a newer request takes priority over the result.
    s = session(); live._sessions[s.id] = s

    async def backend(ctx, question, history, turn, voice, debrief=""):
        await live.handle_event(s, {"type": "session.input_transcript.delta", "event_id": "tv",
                                    "delta": "the weather tonight will be mostly dry", "start_ms": 500, "end_ms": 900}, unit)
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    assert s.counters["input_held"] == 1 and "input_cancellation" not in s.counters
    assert any(e["kind"] == "result" for e in s.events)
    assert not any("newer words take priority" in e.get("text", "") for e in s.events)
    assert s.socket.sent[-1]["type"] == "session.commentary.append" and s.socket.sent[-1]["content"] == "Morning. Five things need you."


@pytest.mark.asyncio
async def test_the_debrief_turn_owns_the_window_up_to_the_button(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s
    s.fragments.append({"event_id": "before", "start_ms": 0, "end_ms": 400, "delta": "actually, no",
                        "received": time.monotonic()})

    async def backend(ctx, question, history, turn, voice, debrief=""):
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    job = next(iter(s.delegations.values()))
    assert job["context_end"] == 400 and s.consumed_ms == 400 and job["started"] > 0
    # words said before the button are not newer than the request, cue or not
    assert any(e["kind"] == "result" for e in s.events)


async def _debrief_waiting_behind_a_delegated_turn(s, unit, monkeypatch):
    """A delegated turn is working; the Debrief button is pressed and the debrief waits on the lock.
    Returns (release, calls): the first backend call is the delegated turn, the second the debrief."""
    entered = asyncio.Event(); release = asyncio.Event(); calls = []

    async def backend(ctx, question, history, turn, voice, debrief=""):
        calls.append(debrief or question)
        if len(calls) == 1:
            entered.set(); await release.wait()
            return {"t": "done", "reply": "Three appointments."}
        state = voice_turn_state._states.get(voice_turn_state.key(ctx.tenant_id, ctx.user_id, turn))
        if state and state.status == "cancelled":      # what voice_chat does for a turn cancelled before it began
            raise live.BackendCancelled("Business turn interrupted")
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.handle_event(s, fragment("Check the diary.", 0, 100, "words"), unit)
    await live.handle_event(s, delegation(offset=100), unit)
    await entered.wait()
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.sleep(.02)                            # the debrief task is now waiting for the lock
    job = next(j for key, j in s.delegations.items() if key.startswith("debrief-"))
    assert job["state"] == "working" and job["context_end"] == 100
    return release, calls, job


@pytest.mark.asyncio
async def test_a_retry_delegation_does_not_cancel_a_debrief_waiting_on_the_lock(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s
    release, calls, job = await _debrief_waiting_behind_a_delegated_turn(s, unit, monkeypatch)
    await live.handle_event(s, delegation("retry-event", "d2", offset=250), unit)     # no new speech behind it
    assert not voice_turn_state._states and "input_cancellation" not in s.counters
    release.set(); await asyncio.gather(*list(s.jobs))
    assert len(calls) == 2 and job["state"] == "complete" and s.delegations["d1"]["state"] == "complete"
    assert [e["payload"].get("reply") for e in s.events if e["kind"] == "result"] == ["Three appointments.", "Morning. Five things need you."]
    assert not any("unconfirmed" in e.get("text", "") or "Stopped" in e.get("text", "") for e in s.events)


@pytest.mark.asyncio
async def test_new_speech_and_a_delegation_stop_a_waiting_debrief_before_it_begins_without_eating_the_words(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; seen = []
    release, calls, job = await _debrief_waiting_behind_a_delegated_turn(s, unit, monkeypatch)
    await live.handle_event(s, fragment(" and the invoices", 110, 240, "new-words"), unit)
    await live.handle_event(s, delegation("new-event", "d2", offset=250), unit)
    assert job["cancel_requested"] is True
    release.set(); await asyncio.gather(*list(s.jobs))
    # the debrief never reached the backend, was not reported as a failure, and left the words for d2
    assert len(calls) == 2 and calls[1] != dw.START_LINE
    assert job["state"] == "cancelled" and s.delegations["d2"]["state"] != "clarification"
    status = [e["text"] for e in s.events if e["kind"] == "status"]
    assert any(text.startswith("The debrief was stopped before anything ran") for text in status)
    assert not any("unconfirmed" in text or "did not receive" in text for text in status)
    assert [e["text"] for e in s.events if e["kind"] == "question"][-1] == "and the invoices"


@pytest.mark.asyncio
@pytest.mark.parametrize("how,reason", [("newer", "new_input"), ("discard", "discard")])
async def test_a_debrief_turn_the_backend_stood_down_is_cancelled_not_unconfirmed(unit, monkeypatch, how, reason):
    s = session(); live._sessions[s.id] = s

    async def backend(ctx, question, history, turn, voice, debrief=""):
        if how == "discard":
            s.generation += 1          # what the discard control does before it cancels the captured turn
        raise live.BackendCancelled("Business turn interrupted")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    job = next(iter(s.delegations.values()))
    assert job["state"] == "cancelled" and s.counters["backend_cancelled"] == 1 and "backend_unconfirmed" not in s.counters
    status = [e["text"] for e in s.events if e["kind"] == "status"]
    if how == "newer":
        assert len(status) == 1 and status[0].startswith("The debrief was stopped before anything ran")
    else:
        assert not status          # a discard has already told the user what it did
    assert not any(e["kind"] == "result" for e in s.events) and not any(m["type"] == "session.commentary.append" for m in s.socket.sent)


@pytest.mark.asyncio
async def test_permissions_lost_while_it_works_prevents_disclosure(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; permission = ["permissions"]
    monkeypatch.setattr(live, "scope", lambda ctx: permission[0])

    async def backend(*a, **k):
        permission[0] = "revoked"
        return result("Private customer data")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    assert not any(e["kind"] == "result" for e in s.events)
    assert all("Private customer" not in str(m) for m in s.socket.sent)


@pytest.mark.asyncio
async def test_a_backend_failure_is_unconfirmed_not_replayed(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; attempts = []

    async def backend(*a, **k):
        attempts.append(1)
        raise RuntimeError("model down")
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await asyncio.gather(*list(s.jobs))
    assert attempts == [1]
    job = next(iter(s.delegations.values()))
    assert job["state"] == "unconfirmed"
    assert any("unconfirmed" in e.get("text", "") for e in s.events)
    assert not any(m["type"] == "session.commentary.append" for m in s.socket.sent)


@pytest.mark.asyncio
async def test_new_trusted_input_cancels_a_working_debrief_turn_like_any_other(unit, monkeypatch):
    from app import voice_turn_state
    s = session(); live._sessions[s.id] = s; entered = asyncio.Event(); release = asyncio.Event()
    state = {}

    async def backend(ctx, question, history, turn, voice, debrief=""):
        state["turn"] = voice_turn_state.begin(voice_turn_state.key(ctx.tenant_id, ctx.user_id, turn))
        entered.set(); await release.wait()
        return result()
    monkeypatch.setattr(live, "run_backend", backend)
    await live.control(live.Control(session=s.id, operation="debrief"), unit)
    await entered.wait()
    await live.handle_event(s, {"type": "session.input_transcript.delta", "event_id": "new-words",
                                "delta": "Actually, no.", "start_ms": 700, "end_ms": 900}, unit)
    assert state["turn"].status == "cancelled"
    release.set(); await asyncio.gather(*list(s.jobs))


def test_the_control_model_only_takes_known_operations_and_steps():
    live.Control(session="s", operation="debrief", step="next")
    with pytest.raises(ValidationError):
        live.Control(session="s", operation="debrief", step="rewind")
    with pytest.raises(ValidationError):
        live.Control(session="s", operation="delete")
    with pytest.raises(ValidationError):
        live.Control(session="s", operation="debrief", unexpected="x")


@pytest.mark.asyncio
async def test_run_backend_hands_the_flag_to_the_voice_route(unit, monkeypatch):
    from app import concierge_routes
    seen = []

    async def frames():
        yield b'data: {"t": "done", "reply": "Morning."}\n\n'

    class Response:
        status_code = 200

        def __init__(self):
            self.body_iterator = frames()      # a fresh stream for every call

    async def route(body, ctx):
        seen.append(body)
        return Response()
    monkeypatch.setattr(concierge_routes, "voice_chat", route)
    out = await live.run_backend(unit, dw.START_LINE, [], "turn-1", "11111111-1111-1111-1111-111111111111", debrief="start")
    assert out["reply"] == "Morning."
    assert seen[0].debrief == "start" and seen[0].question == dw.START_LINE
    # a delegated spoken request carries no flag at all
    await live.run_backend(unit, "Check the diary.", [], "turn-2", "11111111-1111-1111-1111-111111111111")
    assert seen[1].debrief == ""
