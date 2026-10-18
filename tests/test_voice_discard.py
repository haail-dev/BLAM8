"""Tap the heard words on the voice screen to cancel them (Kris 2026-09-23: "it heard stuff in the
background that I didn't mean to say, it would be good if I could click a
cross to remove it before it affects the chat").

Before it's sent, the words never go. After, the turn is cancelled, leaves the
history the next turn sends, and is not saved to the chat — usage still counts.
"""
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import concierge
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header

STATIC = pathlib.Path(__file__).parent.parent / "src/app" / "static"
VOICE_JS = (STATIC / "voice-live.js").read_text(encoding="utf-8")
PANEL_JS = (STATIC / "concierge_panel.js").read_text(encoding="utf-8")


@pytest.fixture
def client(monkeypatch):
    from app import concierge_routes
    concierge_routes._DISCARDED.clear()
    saved, recorded = [], []
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda tid, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda tid, client=None: {"name": "K"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tid, role, content, **kw: saved.append((role, content)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(concierge_routes.fairuse, "consume", lambda tid, tenant: (True, None))

    async def fake_answer(question, history, **kwargs):
        return concierge.BusinessReply(reply="Rufus is a spaniel.", links=[], action=None,
                                       model="t", usage={"input_tokens": 1, "output_tokens": 1,
                                                         "cache_read_input_tokens": 0,
                                                         "cache_creation_input_tokens": 0})
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    try:
        yield TestClient(app), saved, recorded
    finally:
        app.dependency_overrides.clear()
        concierge_routes._DISCARDED.clear()


def test_discard_before_registration_skips_model_and_persistence(client):
    c, saved, recorded = client
    assert c.post("/concierge/voice/discard", json={"turn_id": "abc123"}).json() == {"ok": True, "status": "cancelled"}
    res = c.post("/concierge/voice/chat", json={"question": "Zebra zebra. Rufus", "turn_id": "abc123"})
    done = [json.loads(f[6:]) for f in res.text.split("\n\n") if f.startswith("data: ")][-1]
    assert done["t"] == "cancelled"
    assert saved == []                 # not in the chat
    assert recorded == []             # cancellation arrived before any model call


def test_an_unstruck_turn_is_saved(client):
    c, saved, _ = client
    c.post("/concierge/voice/discard", json={"turn_id": "other"})
    c.post("/concierge/voice/chat", json={"question": "What breed is Rufus?", "turn_id": "mine"})
    assert ("user", "What breed is Rufus?") in saved


def test_a_strike_is_scoped_to_its_tenant(client):
    from app import concierge_routes
    c, _, _ = client
    c.post("/concierge/voice/discard", json={"turn_id": "abc"})
    assert concierge_routes._discarded("t1", "abc", "u1")
    assert not concierge_routes._discarded("t2", "abc", "u1")
    assert not concierge_routes._discarded("t1", "")


def test_the_client_wires_tap_to_cancel():
    # the heard words ARE the cancel (Kris: "make the text clickable to
    # cancel on click") — no separate cross — and a tap is never an interrupt
    assert "class=\"vl-discard\"" in VOICE_JS
    assert "root.querySelector('.vl-discard').addEventListener('click',()=>discardHeard());" in VOICE_JS
    assert 'els.transcript.classList.toggle("is-strikable", on);' in VOICE_JS
    assert "event.stopPropagation();   // a strike is not a tap-to-interrupt" in VOICE_JS
    # a sent turn carries an id a tap can strike; the server is told
    assert "body.turn_id = turnId;" in VOICE_JS
    assert 'deps.apiFetch("discard", { turn_id: turn.id })' in VOICE_JS
    # struck speech still transcribing is ignored by its item id (voice-turns.js),
    # so it can never swallow the next real sentence (replayed in voice_replay.mjs)
    assert "turns.discard();" in VOICE_JS
    # once answered it's past striking
    assert "settle();   // answered: it's in the chat now, past striking" in VOICE_JS
    # the panel drops the user turn from history and its bubble
    assert "onDiscardTurn: voiceDiscardTurn," in PANEL_JS
    assert "history.splice(i, 1)" in PANEL_JS


def test_the_tour_explains_the_screen():
    # Started from the Concierge's settings cog since 2026-09-23 (Kris), not a
    # chip on the voice screen; steps are data (more to come)
    assert "startTour: () => { if (root && open) openTour(); }," in VOICE_JS
    for title in ("Just talk", "Tap the mic to mute", "Tap the orb to interrupt",
                  "Discard an unintended request"):
        assert f'title: "{title}"' in VOICE_JS
    # a tap on the tour is never a tap-to-interrupt
    assert 'if (event.target.closest(".vl-tour")) return;' in VOICE_JS


@pytest.mark.parametrize("tool", ["draft_email", "start_tasks"])
def test_accepted_discard_during_generation_prevents_tools_and_action(client, monkeypatch, tool):
    import asyncio
    from app import concierge_routes as routes, concierge_draft, concierge_tasks

    calls = []
    monkeypatch.setattr(routes.concierge_actions, "stage", lambda *a, **k: calls.append("stage"))

    async def forbidden(*a, **k):
        calls.append("write")
        raise AssertionError("Cancelled turn entered a write helper")

    monkeypatch.setattr(concierge_draft, "draft", forbidden)
    monkeypatch.setattr(concierge_tasks.Starter, "start", forbidden)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(*a, **kw):
            entered.set()
            await release.wait()
            await kw["tool_executor"](tool, {})
            return concierge.BusinessReply(reply="Confirm?", links=[], action={"kind": "fake"}, model="t", usage={})

        monkeypatch.setattr(routes.concierge, "answer_business", answer)
        ctx = TenantCtx(tenant_id="t1", user_id="u1", client=None)
        response = await routes.voice_chat(routes.VoiceChatRequest(question="Do it", turn_id="race"), ctx)

        async def collect():
            return [frame async for frame in response.body_iterator]

        pending = asyncio.create_task(collect())
        await entered.wait()
        cancelled = routes.voice_discard(routes.VoiceDiscardRequest(turn_id="race"), ctx)
        assert json.loads(cancelled.body)["status"] == "cancelled"
        release.set()
        frames = await pending
        assert any('"t": "cancelled"' in frame for frame in frames)

    asyncio.run(scenario())
    assert calls == []
    assert client[1] == []


def test_discard_after_model_before_finalization_blocks_nonempty_action(client, monkeypatch):
    from app import concierge_routes as routes, voice_turn_state
    calls = []
    monkeypatch.setattr(routes.concierge_actions, "stage", lambda *a, **k: calls.append("stage"))

    async def answer(*a, **kw):
        voice_turn_state.cancel(voice_turn_state.key("t1", "u1", "last-moment"))
        return concierge.BusinessReply(reply="Confirm?", links=[], action={"kind": "fake"}, model="t", usage={})

    monkeypatch.setattr(routes.concierge, "answer_business", answer)
    response = client[0].post("/concierge/voice/chat", json={"question": "Do it", "turn_id": "last-moment"})
    assert '"t": "cancelled"' in response.text
    assert calls == [] and client[1] == []
    assert len(client[2]) == 1  # completed model work remains metered


@pytest.mark.parametrize("tool", ["draft_email", "start_tasks", "show_draft"])
def test_discard_after_write_helper_entry_is_too_late_and_result_survives(client, monkeypatch, tool):
    import asyncio
    from app import concierge_routes as routes, concierge_draft, concierge_tasks

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        created = []

        async def draft(*a, **kw):
            entered.set()
            await release.wait()
            created.append("draft-or-policy-send")
            return {"ok": True}, {"log_id": "surviving", "status": "pending"}

        async def tasks(self, *a, **kw):
            entered.set()
            await release.wait()
            created.append("task-launch")
            self.batch = {"batch_id": "surviving", "tasks": []}
            return {"ok": True}

        monkeypatch.setattr(concierge_draft, "draft", draft)
        monkeypatch.setattr(concierge_draft, "show", draft)
        monkeypatch.setattr(concierge_tasks.Starter, "start", tasks)

        async def answer(*a, **kw):
            await kw["tool_executor"](tool, {})
            return concierge.BusinessReply(reply="Ready", links=[], action=None, model="t", usage={})

        monkeypatch.setattr(routes.concierge, "answer_business", answer)
        ctx = TenantCtx(tenant_id="t1", user_id="u1", client=None)
        response = await routes.voice_chat(routes.VoiceChatRequest(question="Do it", turn_id="committed"), ctx)

        async def collect():
            return [frame async for frame in response.body_iterator]

        pending = asyncio.create_task(collect())
        await entered.wait()
        cancelled = routes.voice_discard(routes.VoiceDiscardRequest(turn_id="committed"), ctx)
        assert json.loads(cancelled.body)["status"] == "too_late"
        release.set()
        frames = await pending
        done = json.loads(next(frame[6:] for frame in frames if '"t": "done"' in frame))
        assert (done["tasks"] or done["draft"])["batch_id" if tool == "start_tasks" else "log_id"] == "surviving"
        assert len(created) == 1
        again = routes.voice_discard(routes.VoiceDiscardRequest(turn_id="committed"), ctx)
        assert json.loads(again.body)["status"] == "too_late"

    asyncio.run(scenario())
    assert ("user", "Do it") in client[1]


def test_registry_scopes_users_preserves_early_discard_and_bounds_capacity():
    from app import voice_turn_state as state
    state._states.clear()
    try:
        identity = state.key("tenant", "u1", "same")
        assert state.cancel(identity)["status"] == "cancelled"
        with pytest.raises(state.Cancelled):
            state.commit(state.begin(identity), "draft_email")
        other = state.begin(state.key("tenant", "u2", "same"))
        state.commit(other, "draft_email")
        assert state.cancel(state.key("tenant", "u2", "same"))["status"] == "too_late"
        assert state.cancel(state.key("other-tenant", "u2", "same"))["status"] == "cancelled"
        with pytest.raises(RuntimeError, match="Duplicate"):
            state.begin(identity)
        for n in range(state.MAX_TURNS - len(state._states)):
            state.register(state.key("t", "u", str(n)))
        with pytest.raises(RuntimeError, match="capacity"):
            state.register(state.key("t", "u", "overflow"))
    finally:
        state._states.clear()


@pytest.mark.parametrize("committing", [False, True])
def test_asgi_disconnect_cancels_only_before_write_boundary(client, monkeypatch, committing):
    """Drive the app's actual ASGI disconnect listener, not just its iterator."""
    import asyncio
    from app import concierge_routes as routes, concierge_draft, voice_turn_state

    async def scenario():
        entered, release, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writes = []

        async def draft(*a, **kw):
            entered.set()
            await release.wait()
            writes.append("draft")
            return {"ok": True}, {"log_id": "survived", "status": "pending"}

        async def answer(*a, **kw):
            if committing:
                await kw["tool_executor"]("draft_email", {})
            else:
                entered.set()
                await release.wait()
            return concierge.BusinessReply(reply="Ready", links=[], action=None, model="t", usage={})

        monkeypatch.setattr(routes.concierge, "answer_business", answer)
        monkeypatch.setattr(concierge_draft, "draft", draft)
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": json.dumps({"question": "Do it", "turn_id": "disconnect"}).encode(), "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/concierge/voice/chat",
                 "raw_path": b"/concierge/voice/chat", "query_string": b"", "root_path": "",
                 "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
                 "server": ("testserver", 80), "client": ("127.0.0.1", 1234)}
        request = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        disconnect.set()
        await asyncio.wait_for(request, 2)
        state = voice_turn_state._states[voice_turn_state.key("t1", "u1", "disconnect")]
        assert state.status == ("committing" if committing else "cancelled")
        runners = list(routes._VOICE_RUNNERS)
        assert runners  # disconnected browser must not destroy accepted work
        release.set()
        await asyncio.wait_for(asyncio.gather(*runners), 2)
        assert writes == (["draft"] if committing else [])
        assert bool(client[1]) is committing
        assert len(client[2]) == 1  # completed model work still gets metered

    asyncio.run(scenario())
