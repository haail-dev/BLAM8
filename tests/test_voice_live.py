"""Live voice conversation (docs/voice-live-seed.md) — the spoken filter, the
streaming brain variant, and the three /concierge/voice endpoints.

The spoken-filter cases follow the donor's contract (OurTab
scripts/test-voice-live.mjs): every case is fed at chunk strides 1/3/7 as well
as whole, and must produce identical output — tags/JSON split across arbitrary
chunk boundaries is the entire problem.

Run: python -m pytest tests/test_voice_live.py -q
"""
from types import SimpleNamespace
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import concierge, llm
from app.auth import require_onboarded, TenantCtx
from app.main import app
from app.security import require_csrf_header
from app.voice_spoken import ReplyExtractor


# ── the spoken filter ────────────────────────────────────────────────────────
def run_extractor(chunks):
    out = []
    ex = ReplyExtractor(out.append)
    for chunk in chunks:
        ex.push(chunk)
    ex.end()
    return "".join(out)


def chunkings(text):
    variants = [[text]]
    for size in (1, 3, 7):
        variants.append([text[i:i + size] for i in range(0, len(text), size)])
    return variants


FILTER_CASES = [
    ("plain reply passes through",
     '{"reply": "You have three bookings tomorrow.", "links": [], '
     '"action": {"kind": ""}}',
     "You have three bookings tomorrow."),
    ("links and action are silenced",
     '{"reply": "One prospect.", "links": [{"kind": "pipeline", "id": "", '
     '"label": "Open pipeline"}], "action": {"kind": "draft_reply", '
     '"draft_text": "Dear Sarah, the price is £40."}}',
     "One prospect."),
    ("reply is not the first key",
     '{"links": [], "action": {"kind": ""}, "reply": "Quiet day."}',
     "Quiet day."),
    ("escaped quotes and newlines survive",
     '{"reply": "He said \\"yes\\".\\nThat settles it.", "links": []}',
     'He said "yes".\nThat settles it.'),
    ("unicode escapes decode",
     '{"reply": "Caf\\u00e9 at 3.", "links": []}',
     "Café at 3."),
    ("a nested reply key never speaks",
     '{"action": {"kind": "add_kb_entry", "content": "the word \\"reply\\" '
     'and even {\\"reply\\": \\"fake\\"} stay silent"}, "reply": "Done bits.", '
     '"links": []}',
     "Done bits."),
    ("an unterminated reply flushes what it has",
     '{"reply": "The booking is at te',
     "The booking is at te"),
    ("pretty-printed json still works",
     '{\n  "reply" : "Fine." ,\n  "links" : [ ]\n}',
     "Fine."),
    ("empty reply speaks nothing",
     '{"reply": "", "links": [], "action": {"kind": ""}}',
     ""),
]


@pytest.mark.parametrize("name,raw,expected", FILTER_CASES,
                         ids=[c[0] for c in FILTER_CASES])
def test_reply_extractor_all_chunkings(name, raw, expected):
    for i, chunks in enumerate(chunkings(raw)):
        got = run_extractor(chunks)
        assert got == expected, f"{name} (chunking {i}): {got!r} != {expected!r}"


def test_reply_extractor_stops_after_reply_closes():
    """Nothing after the reply value — not even a second bogus reply key —
    ever reaches the speaker."""
    out = []
    ex = ReplyExtractor(out.append)
    ex.push('{"reply": "Hi.", "reply": "again"}')
    ex.end()
    assert "".join(out) == "Hi."


def test_reply_extractor_surrogate_pair():
    got = run_extractor(['{"reply": "OK \\ud83d\\ude00 done", "links": []}'])
    assert got == "OK 😀 done"


# ── llm.stream: the streaming choke point ────────────────────────────────────
class FakeStream:
    def __init__(self, deltas, final):
        self._deltas = deltas
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d
        return gen()

    # The raw events llm.stream's hedge reads (2026-09-24): a block starts,
    # then its text deltas.
    def __aiter__(self):
        async def gen():
            yield SimpleNamespace(type="content_block_start")
            for d in self._deltas:
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="text_delta", text=d))
        return gen()

    async def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, deltas, final):
        self._deltas, self._final = deltas, final
        self.stream_kwargs = None
        self.create_called = False

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return FakeStream(self._deltas, self._final)

    async def create(self, **kwargs):
        self.create_called = True
        return self._final


class FakeClient:
    def __init__(self, deltas, final):
        self.messages = FakeMessages(deltas, final)


class FakeUsage:
    input_tokens = 3
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeFinal:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.usage = FakeUsage()
        self.model = "test"


def test_llm_stream_forwards_deltas_and_returns_final(monkeypatch):
    monkeypatch.setattr(llm.db, "insert_llm_call", lambda row: None)
    final = FakeFinal('{"reply": "Hi."}')
    fake = FakeClient(['{"reply"', ': "Hi."}'], final)
    seen = []

    async def on_delta(text):
        seen.append(text)

    msg = asyncio.run(llm.stream(client=fake, tenant_id="t1", operation="x",
                                 on_delta=on_delta, model="m", max_tokens=10,
                                 messages=[{"role": "user", "content": "q"}]))
    assert msg is final
    assert seen == ['{"reply"', ': "Hi."}']
    assert fake.messages.create_called is False


# ── answer_business: streaming variant ───────────────────────────────────────
def test_answer_business_streams_and_typed_path_untouched(monkeypatch):
    monkeypatch.setattr(llm.db, "insert_llm_call", lambda row: None)
    final = FakeFinal(json.dumps({"reply": "Two bookings.", "links": [],
                                  "action": {"kind": ""}}))
    fake = FakeClient(['{"reply": "Two bo', 'okings.", "links": [], '
                       '"action": {"kind": ""}}'], final)
    monkeypatch.setattr(concierge, "client", lambda: fake)

    async def executor(name, args):
        return {}

    deltas = []

    async def on_delta(text, round_no):
        deltas.append((round_no, text))

    result = asyncio.run(concierge.answer_business(
        "what's on?", [], tools=[{"name": "t", "input_schema": {}}],
        tool_executor=executor, tenant_id="t1",
        on_delta=on_delta, voice_mode=True))
    assert result.reply == "Two bookings."
    assert [t for _, t in deltas] == ['{"reply": "Two bo',
                                      'okings.", "links": [], '
                                      '"action": {"kind": ""}}']
    assert all(r == 0 for r, _ in deltas)
    # voice_mode reached the prompt
    system = fake.messages.stream_kwargs["system"]
    system_text = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "VOICE STYLE" in system_text

    # Without on_delta the typed path runs — messages.create, not stream, and
    # (2026-09-06) with the `answer` TOOL instead of an output schema; a
    # text-JSON reply still parses.
    fake2 = FakeClient([], final)
    monkeypatch.setattr(concierge, "client", lambda: fake2)
    result2 = asyncio.run(concierge.answer_business(
        "what's on?", [], tools=[{"name": "t", "input_schema": {}}],
        tool_executor=executor, tenant_id="t1"))
    assert result2.reply == "Two bookings."
    assert fake2.messages.create_called is True
    assert fake2.messages.stream_kwargs is None


def test_business_prompt_voice_mode_clauses():
    p = concierge.build_business_prompt(voice_mode=True)
    assert "VOICE STYLE" in p
    assert "spoken aloud" in p
    # speak-before-lookup: the anti-dead-air clause
    assert "SAME reply as the tool call" in p
    # And the typed prompt carries none of it.
    assert "VOICE STYLE" not in concierge.build_business_prompt()


def test_voice_prompt_treats_a_stated_fact_about_a_customer_as_a_note():
    # N14: "Right on, Tom. Who wants the invoice by email" was answered as a question
    # nobody asked; speech-to-text had turned "write on Tom" into a question.
    p = concierge.build_business_prompt(voice_mode=True)
    assert "HEARD AS A QUESTION, MEANT AS A NOTE" in p
    assert "Do you want that on X's record?" in p
    assert "Never answer with what the records say about it" in p
    typed = concierge.build_business_prompt()
    assert "HEARD AS A QUESTION" not in typed
    # update_contact is a Pro kind: a Starter voice prompt neither names it nor asks for the note.
    starter = concierge.build_business_prompt(voice_mode=True, full_qa=False)
    assert "VOICE STYLE" in starter and "The owner also has a SCREEN" in starter
    assert "HEARD AS A QUESTION" not in starter and "update_contact" not in starter


def test_voice_prompt_asks_for_a_tap_not_a_spoken_yes_and_spells_out_money():
    # N10, N11: "Want me to send both of them now?" beside a card; "two fifty-two fifty".
    p = concierge.build_business_prompt(voice_mode=True)
    assert "Every card today is approve-first" in p
    assert "in GPT-Live ONLY a tap on the card counts" in p
    assert "say 'it's ready — tap Confirm'" in p
    assert "Never ask 'Shall I?' or 'Want me to?' about a card as if a spoken yes could run it" in p
    assert "MONEY AND REFERENCES" in p and "'£252.50'" in p
    # The draft path keeps its own mode-aware question: Standard voice sends a waiting draft on
    # a yes only after "Shall I send it?" was asked, so the card rule must not forbid it.
    assert "ask 'Shall I send it?' when it is waiting" in p
    assert ("The one spoken question that still belongs is the WRITING EMAILS one below — "
            "'Shall I send it?' for a draft that is waiting — and in GPT-Live the note on the "
            "message overrides that too.") in p
    assert "or 'Shall I send it?' about a card" not in p            # the clause that contradicted it
    assert "nothing does today except a draft reply" not in p


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    # No membership row = the account's own owner (lookups fail CLOSED).
    from app import db as _db
    monkeypatch.setattr(_db, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_voice_token_503_without_key(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "")
    res = client.post("/concierge/voice/token", json={})
    assert res.status_code == 503
    assert "OPENAI_API_KEY" in res.json()["error"]


class FakeHttpxResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHttpxClient:
    """Stands in for httpx.AsyncClient in the token route."""
    last_json = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        FakeHttpxClient.last_json = json
        return FakeHttpxResponse(200, {"value": "ek_test", "expires_at": 123})


def test_voice_token_mints_turn_session(client, monkeypatch):
    import httpx
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpxClient)
    res = client.post("/concierge/voice/token", json={"silence_ms": 500})
    assert res.status_code == 200
    assert res.json()["value"] == "ek_test"
    sent = FakeHttpxClient.last_json["session"]["audio"]["input"]
    assert sent["transcription"]["model"] == "gpt-4o-transcribe"
    vad = sent["turn_detection"]
    # The hard-won numbers: 0.5 threshold (0.6 ate quiet first syllables) and
    # 600ms prefix padding. Do not "fix noise" by raising these.
    assert vad == {"type": "server_vad", "threshold": 0.5,
                   "prefix_padding_ms": 600, "silence_duration_ms": 500}


def test_voice_token_captions_session_has_no_vad(client, monkeypatch):
    import httpx
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpxClient)
    res = client.post("/concierge/voice/token", json={"purpose": "captions"})
    assert res.status_code == 200
    sent = FakeHttpxClient.last_json["session"]["audio"]["input"]
    # gpt-live-transcribe REJECTS turn_detection (probed live 2026-08-23) —
    # display-only captions, never the turn engine.
    assert sent["transcription"]["model"] == "gpt-live-transcribe"
    assert "turn_detection" not in sent


def test_voice_chat_streams_speak_tool_and_done(client, monkeypatch):
    from app import concierge_routes

    saved = []
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tenant_id, client=None: {"name": "Kennel Co"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config",
                        lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tenant_id, role, content, **kw: saved.append((role, content)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic",
                        lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.fairuse, "consume",
                        lambda tenant_id, tenant: (True, None))

    async def fake_answer(question, history, **kwargs):
        assert kwargs["voice_mode"] is True
        on_delta = kwargs["on_delta"]
        on_tool = kwargs["on_tool"]
        # Round 0: the speak-before-lookup sentence, then a tool call.
        await on_delta('{"reply": "Let me check the di', 0)
        await on_delta('ary.", "links": [], "action": {"kind": ""}}', 0)
        await on_tool("list_bookings", {"date_from": "2026-08-23"})
        # Round 1: the real answer.
        await on_delta('{"reply": "Two bookings tomorrow.", ', 1)
        await on_delta('"links": [], "action": {"kind": ""}}', 1)
        return concierge.BusinessReply(
            reply="Two bookings tomorrow.", links=[], action=None,
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)

    res = client.post("/concierge/voice/chat", json={"question": "what's on tomorrow?"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(f[6:]) for f in res.text.split("\n\n")
              if f.startswith("data: ")]
    spoken = "".join(f["text"] for f in frames if f["t"] == "speak")
    # Only the reply fields — never a brace, bracket or key name — get spoken.
    assert spoken == "Let me check the diary.Two bookings tomorrow."
    tools = [f for f in frames if f["t"] == "tool"]
    assert tools and tools[0]["name"] == "list_bookings"
    assert tools[0]["hint"] == "Checking the diary…"
    done = [f for f in frames if f["t"] == "done"][0]
    assert done["reply"] == "Two bookings tomorrow."
    # The exchange persisted exactly like a typed turn.
    assert ("user", "what's on tomorrow?") in saved
    assert ("assistant", "Two bookings tomorrow.") in saved


def test_voice_chat_tells_a_gptlive_turn_that_only_a_tap_confirms(client, monkeypatch):
    # Per turn, from the registered session: the cached prompt cannot say it because
    # Standard voice accepts a spoken yes on the same text.
    from app import concierge_routes, gptlive
    live_id = "5b0a1a2e-0000-4000-8000-000000000001"
    monkeypatch.setitem(gptlive._sessions, "s-live",
                        gptlive.Session("s-live", "prov", "t1", "u1", live_id, "permissions", None))
    notes = []
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda tenant_id, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda tenant_id, client=None: {"name": "Biz"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.fairuse, "consume", lambda tenant_id, tenant: (True, None))

    async def fake_answer(question, history, **kwargs):
        notes.append(kwargs.get("question_note") or "")
        await kwargs["on_delta"]('{"reply": "Ready.", "links": [], "action": {"kind": ""}}', 0)
        return concierge.BusinessReply(
            reply="Ready.", links=[], action=None,
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)

    assert client.post("/concierge/voice/chat", json={"question": "block Friday", "voice_session": live_id}).status_code == 200
    assert client.post("/concierge/voice/chat", json={"question": "block Friday",
                       "voice_session": "5b0a1a2e-0000-4000-8000-000000000002"}).status_code == 200
    assert client.post("/concierge/voice/chat", json={"question": "block Friday"}).status_code == 200
    live_note, standard_note, no_session_note = notes
    assert "only a tap on the Confirm card" in live_note and "never ask 'Shall I?'" in live_note
    assert "only a tap on the Confirm card" not in standard_note
    assert "only a tap on the Confirm card" not in no_session_note


def test_voice_chat_daily_cap_is_a_402(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tenant_id, client=None: {"plan": "email"})
    monkeypatch.setattr(concierge_routes.fairuse, "consume",
                        lambda tenant_id, tenant: (False, 10))
    res = client.post("/concierge/voice/chat", json={"question": "hi"})
    assert res.status_code == 402
    assert "allowance" in res.json()["error"]


def test_voice_chat_error_frame_on_engine_failure(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: set())
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config",
                        lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.fairuse, "consume",
                        lambda tenant_id, tenant: (True, None))

    async def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", boom)

    res = client.post("/concierge/voice/chat", json={"question": "hi"})
    assert res.status_code == 200
    frames = [json.loads(f[6:]) for f in res.text.split("\n\n")
              if f.startswith("data: ")]
    assert any(f["t"] == "error" for f in frames)


def test_voice_tts_503_without_key(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "")
    # TTS speaks only what the server said to this person (security review M37)
    concierge_routes._remember_speech(TenantCtx(tenant_id="t1", client=None, user_id="u1"), "Hello.")
    res = client.post("/concierge/voice/tts", json={"text": "Hello."})
    assert res.status_code == 503


class FakeTtsUpstream:
    status_code = 200

    async def aiter_bytes(self):
        yield b"\x00\x01"
        yield b"\x02\x03"

    async def aread(self):
        return b""

    async def aclose(self):
        pass


class FakeTtsClient:
    last_json = None

    def __init__(self, *a, **k):
        pass

    def build_request(self, method, url, headers=None, json=None):
        FakeTtsClient.last_json = json
        return "req"

    async def send(self, req, stream=False):
        return FakeTtsUpstream()

    async def aclose(self):
        pass


def test_voice_tts_streams_pcm_with_stream_format(client, monkeypatch):
    import httpx
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeTtsClient)
    monkeypatch.setattr(concierge_routes, "_TTS_HTTP", None)   # the pooled client is made fresh
    # TTS speaks only what the server said to this person (security review M37)
    concierge_routes._remember_speech(TenantCtx(tenant_id="t1", client=None, user_id="u1"),
                                      "Two bookings. x")
    res = client.post("/concierge/voice/tts", json={"text": "Two bookings."})
    assert res.status_code == 200
    assert res.content == b"\x00\x01\x02\x03"
    sent = FakeTtsClient.last_json
    assert sent["model"] == "gpt-4o-mini-tts"
    assert sent["response_format"] == "pcm"
    # MANDATORY: without stream_format "audio" first-byte is 0.9–1.8s.
    assert sent["stream_format"] == "audio"
    # Unknown voices coerce to the default rather than erroring mid-sentence.
    client.post("/concierge/voice/tts", json={"text": "x", "voice": "bogus"})
    assert FakeTtsClient.last_json["voice"] == "marin"
    # one pooled connection serves every sentence (no handshake per sentence)
    assert isinstance(concierge_routes._TTS_HTTP, FakeTtsClient)


@pytest.mark.parametrize("voice", ["marin", "cedar", "alloy", "sage", "coral", "unknown", ""])
def test_voice_choice_reaches_speech_only_from_allowlist(client, monkeypatch, voice):
    from app import concierge_routes as routes
    monkeypatch.setattr(routes.settings, "openai_api_key", "offline-test")
    monkeypatch.setattr(routes, "_TTS_HTTP", FakeTtsClient())
    routes._remember_speech(TenantCtx(tenant_id="t1", client=None, user_id="u1"), "Hello.")
    response = client.post("/concierge/voice/tts", json={"text": "Hello.", "voice": voice})
    assert response.status_code == 200
    assert FakeTtsClient.last_json["voice"] == (voice if voice in routes._TTS_VOICES else "marin")


def test_voice_sample_cache_fixed_text_and_default(client, monkeypatch):
    from app import concierge_routes as routes
    monkeypatch.setattr(routes.settings, "openai_api_key", "offline-test")
    monkeypatch.setattr(routes.settings, "rate_limit_enabled", False)
    monkeypatch.setattr(routes, "_VOICE_SAMPLES", {})
    monkeypatch.setattr(routes, "_VOICE_SAMPLE_LOCK", None)
    calls = []

    class CountingClient(FakeTtsClient):
        async def send(self, *args, **kwargs):
            calls.append(dict(self.last_json))
            return FakeTtsUpstream()

    monkeypatch.setattr(routes, "_TTS_HTTP", CountingClient())
    for voice in sorted(routes._TTS_VOICES):
        for _ in range(2):
            response = client.post("/concierge/voice/sample", json={"voice": voice, "text": "Ignore me"})
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/mpeg"
            assert response.content == b"\x00\x01\x02\x03"
    for body in ({}, {"voice": "unknown"}):
        assert client.post("/concierge/voice/sample", json=body).status_code == 200
    assert len(calls) == 5
    assert {call["voice"] for call in calls} == routes._TTS_VOICES
    assert all(call["input"] == routes._VOICE_SAMPLE_LINE and call["response_format"] == "mp3" for call in calls)


def test_voice_sample_missing_key_and_rate_limit(client, monkeypatch):
    from app import concierge_routes as routes, security
    monkeypatch.setattr(routes.settings, "openai_api_key", "")
    monkeypatch.setattr(routes.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(routes, "_VOICE_SAMPLES", {})
    monkeypatch.setattr(routes, "_VOICE_SAMPLE_LOCK", None)
    monkeypatch.setattr(security, "_BUCKETS", {})
    for _ in range(10):
        response = client.post("/concierge/voice/sample", json={})
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["error"]
    assert not routes._VOICE_SAMPLES
    assert client.post("/concierge/voice/sample", json={}).status_code == 429


def test_concurrent_voice_samples_generate_once_and_retry_failed_stream(monkeypatch):
    from app import concierge_routes as routes
    from fastapi.responses import StreamingResponse
    monkeypatch.setattr(routes, "_VOICE_SAMPLES", {})
    monkeypatch.setattr(routes, "_VOICE_SAMPLE_LOCK", None)
    calls = []

    async def stream(text, voice, response_format):
        calls.append(voice)
        async def chunks():
            await asyncio.sleep(0)
            if len(calls) == 1:
                raise RuntimeError("offline interrupted stream")
            yield b"sample"
        return StreamingResponse(chunks())

    monkeypatch.setattr(routes, "_tts_stream_response", stream)

    async def run():
        body = routes.VoiceSampleRequest(voice="cedar")
        ctx = TenantCtx(tenant_id="t1", client=None, user_id="u1")
        failed = await routes.voice_sample(body, ctx)
        assert failed.status_code == 502
        assert not routes._VOICE_SAMPLES
        results = await asyncio.gather(*(routes.voice_sample(body, ctx) for _ in range(5)))
        assert all(result.body == b"sample" for result in results)

    asyncio.run(run())
    assert calls == ["cedar", "cedar"]


def test_voice_endpoints_require_auth():
    for path in ("/concierge/voice/token", "/concierge/voice/chat",
                 "/concierge/voice/tts", "/concierge/voice/sample"):
        res = TestClient(app).post(path, json={"question": "hi", "text": "hi"},
                                   follow_redirects=False)
        assert res.status_code in (303, 307, 401, 403), path


# ── the audio rig's endpoints (secret-guarded, no tenant) ────────────────────
