import asyncio
import json

import httpx
import pytest

from app import concierge_routes as routes, speech, voice_crm


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(speech.settings, "speech_provider", "local")
    monkeypatch.setattr(speech.settings, "speech_service_url", "https://speech.example")
    monkeypatch.setattr(speech.settings, "speech_service_ws_url", "wss://speech.example/v1/realtime")
    monkeypatch.setattr(speech.settings, "speech_service_key", "test-service-secret")
    monkeypatch.setattr(speech.settings, "openai_api_key", "")


def test_readiness_and_voice_choices_do_not_require_paid_key(local):
    assert speech.ready()
    assert speech.resolve_voice("marin") == "af_heart"
    assert speech.resolve_voice("bf_emma") == "bf_emma"


@pytest.mark.parametrize("url", ["http://speech.example", "https://user:secret@speech.example", "https://speech.example?key=x", ""])
def test_service_rejects_insecure_or_credential_urls(local, monkeypatch, url):
    monkeypatch.setattr(speech.settings, "speech_service_url", url)
    assert not speech.ready()


def test_token_and_crm_use_only_self_hosted_service(local, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "speech.example"
        assert request.headers["authorization"] == "Bearer test-service-secret"
        if request.url.path.endswith("client_secrets"):
            assert json.loads(request.content)["hint"] == "Names: Sam"
            return httpx.Response(200, json={"value": "one-use-ticket", "expires_at": 100})
        assert request.content == b"audio"
        return httpx.Response(200, json={"text": "  Call Sam  "})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async def run():
        response = await routes._mint_realtime_token(False, 500, "Names: Sam")
        result = json.loads(response.body)
        assert result["provider"] == "local" and result["websocket_url"].startswith("wss://")
        assert "test-service-secret" not in response.body.decode()
        captions = await routes._mint_realtime_token(True, 500)
        assert json.loads(captions.body)["captions"] == "same_session"
        assert await voice_crm.transcribe(b"audio") == "Call Sam"
    asyncio.run(run())
    assert len(requests) == 2


def test_local_failure_does_not_fall_back_to_paid_service(local, monkeypatch):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(503)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async def run():
        assert (await routes._mint_realtime_token(False, 500)).status_code == 503
        with pytest.raises(voice_crm.VoiceCrmError, match="Local transcription"):
            await voice_crm.transcribe(b"audio")
    asyncio.run(run())
    assert len(seen) == 2 and all(url.startswith("https://speech.example/") for url in seen)


def test_local_tts_preserves_pcm_and_closes_upstream(local, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "speech.example"
        assert json.loads(request.content) == {"voice": "af_heart", "input": "Hello", "response_format": "pcm"}
        return httpx.Response(200, content=b"\x01\x00" * 100)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(routes, "_tts_http", lambda: client)
            response = await routes._tts_stream_response("Hello", "marin")
            assert response.status_code == 200
            assert b"".join([chunk async for chunk in response.body_iterator]) == b"\x01\x00" * 100
    asyncio.run(run())
    assert len(requests) == 1


def test_local_csp_allows_only_configured_speech_origin(local):
    from fastapi import Response
    from app import security
    response = Response()
    security._apply_security_headers(response)
    policy = response.headers.get("Content-Security-Policy-Report-Only") or response.headers["Content-Security-Policy"]
    connect = next(part for part in policy.split(";") if "connect-src" in part)
    assert "wss://speech.example" in connect and "openai.com" not in connect
    assert "test-service-secret" not in policy


def test_local_voice_sample_uses_wav_and_provider_scoped_cache(local, monkeypatch):
    monkeypatch.setattr(routes, "_VOICE_SAMPLES", {"openai:bf_emma": b"wrong-provider"})
    monkeypatch.setattr(routes, "_VOICE_SAMPLE_LOCK", None)
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        assert payload["voice"] == "bf_emma" and payload["response_format"] == "wav"
        calls.append(request)
        return httpx.Response(200, content=b"RIFF-local-sample")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(routes, "_tts_http", lambda: client)
            for _ in range(2):
                response = await routes.voice_sample(routes.VoiceSampleRequest(voice="bf_emma"), None)
                assert response.headers["content-type"] == "audio/wav"
                assert response.body == b"RIFF-local-sample"
    asyncio.run(run())
    assert len(calls) == 1


def test_local_browser_connection_and_slow_transcript_contract():
    import subprocess
    from pathlib import Path
    script = Path(__file__).parent.joinpath("replays", "local_speech_replay.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_local_tts_keeps_requester_server_said_boundary(local, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(routes, "_SPOKEN", {})
    calls = []
    async def proxy(text, voice):
        calls.append((text, voice))
        return "local-audio"
    monkeypatch.setattr(routes, "_tts_stream_response", proxy)
    owner = SimpleNamespace(tenant_id="local-tenant", user_id="one")
    other = SimpleNamespace(tenant_id="local-tenant", user_id="two")
    text = "Your diary lookup found three appointments. No change was executed."
    routes._remember_speech(owner, text)
    async def run():
        body = routes.VoiceTtsRequest(text=text, voice="bf_emma")
        assert (await routes.voice_tts(body, other)).status_code == 403
        assert await routes.voice_tts(body, owner) == "local-audio"
        assert (await routes.voice_tts(routes.VoiceTtsRequest(text="Invented business success."), owner)).status_code == 403
    asyncio.run(run())
    assert calls == [(text, "bf_emma")]


def test_local_provider_uses_existing_deferred_lifecycle_replay():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "tests/replays/voice_lifecycle_replay.mjs", "--local"], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
