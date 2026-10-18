import asyncio
import base64
import io
import threading
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from speech_service.config import Settings
from speech_service.inference import Busy, Worker
from speech_service.realtime import Segmenter
from speech_service.server import create_app


KEY = "test-only-" + "x" * 32
AUTH = {"Authorization": "Bearer " + KEY}
ORIGIN = "https://blam8.example"


class FakeModels:
    def load(self):
        pass

    def speak(self, text, voice):
        return b"\x01\x00" * 2400

    def transcribe_file(self, data):
        return "Recorded debrief"

    def transcribe(self, samples, hint):
        return "Check the diary"

    def probability(self, samples):
        return 0.9 if np.max(np.abs(samples[-512:])) > 0.01 else 0


@pytest.fixture
def client():
    app = create_app(Settings(api_key=KEY, allowed_origins=ORIGIN), FakeModels())
    with TestClient(app) as client:
        yield client


def ticket(client):
    result = client.post("/v1/realtime/client_secrets", headers=AUTH, json={})
    assert result.status_code == 200
    return ["realtime", "blam8-ticket." + result.json()["value"]]


def test_service_requires_auth_and_has_no_business_data(client):
    for path, body in (("/v1/realtime/client_secrets", {}), ("/v1/audio/speech", {"input": "Hello"})):
        assert client.post(path, json=body).status_code == 401
    assert client.post("/v1/audio/transcriptions", content=b"x").status_code == 401
    assert client.get("/health").json()["tts"] == "kokoro"


def test_speech_pcm_and_browser_sample(client):
    pcm = client.post("/v1/audio/speech", headers=AUTH, json={"input": "Hello"})
    assert pcm.status_code == 200 and len(pcm.content) == 4800
    sample = client.post("/v1/audio/speech", headers=AUTH,
                         json={"input": "Hello", "voice": "bf_emma", "response_format": "wav"})
    assert sample.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(sample.content)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
    assert client.post("/v1/audio/speech", headers=AUTH,
                       json={"input": "Hello", "voice": "marin"}).status_code == 422
    assert client.post("/v1/audio/speech", headers=AUTH, json={"input": "  "}).status_code == 422


def test_transcription_body_bounds(client):
    assert client.post("/v1/audio/transcriptions", headers=AUTH, content=b"audio").json() == {"text": "Recorded debrief"}
    assert client.post("/v1/audio/transcriptions", headers=AUTH, content=b"").status_code == 400
    assert client.post("/v1/audio/transcriptions", headers=AUTH, content=b"a" * (12 * 1024 * 1024 + 1)).status_code == 413


def test_ticket_is_single_use_and_origin_checked(client):
    protocols = ticket(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/realtime", subprotocols=protocols, headers={"origin": "https://other.example"}):
            pass
    with client.websocket_connect("/v1/realtime", subprotocols=protocols, headers={"origin": ORIGIN}):
        pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/realtime", subprotocols=protocols, headers={"origin": ORIGIN}):
            pass


def test_expired_ticket_is_rejected(client, monkeypatch):
    from types import SimpleNamespace
    import time
    from speech_service import server
    protocols = ticket(client)
    later = time.monotonic() + 61
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: later, time=time.time))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/realtime", subprotocols=protocols, headers={"origin": ORIGIN}):
            pass


def test_capacity_is_enforced_at_connect_and_ticket_mint():
    app = create_app(Settings(api_key=KEY, allowed_origins=ORIGIN, max_sessions=1), FakeModels())
    with TestClient(app) as client:
        first, second = ticket(client), ticket(client)
        with client.websocket_connect("/v1/realtime", subprotocols=first, headers={"origin": ORIGIN}):
            assert client.post("/v1/realtime/client_secrets", headers=AUTH, json={}).status_code == 503
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/v1/realtime", subprotocols=second, headers={"origin": ORIGIN}):
                    pass


def test_malformed_json_closes_connection(client):
    with client.websocket_connect("/v1/realtime", subprotocols=ticket(client), headers={"origin": ORIGIN}) as ws:
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_live_audio_event_order_and_final_transcript(client):
    with client.websocket_connect("/v1/realtime", subprotocols=ticket(client), headers={"origin": ORIGIN}) as ws:
        pcm = (np.ones(768 * 12) * 12000).astype("<i2").tobytes() + b"\0" * (1536 * 18)
        ws.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
        events = [ws.receive_json() for _ in range(4)]
        assert [e["type"].split(".")[-1] for e in events] == ["speech_started", "speech_stopped", "committed", "completed"]
        assert len({e["item_id"] for e in events}) == 1
        assert events[-1]["transcript"] == "Check the diary"


def test_clear_suppresses_inflight_transcript_without_crossing_into_next_turn():
    entered, release = threading.Event(), threading.Event()

    class BlockingModels(FakeModels):
        calls = 0

        def transcribe(self, samples, hint):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                release.wait(5)
                return "Discarded words"
            return "Next question"

    app = create_app(Settings(api_key=KEY, allowed_origins=ORIGIN), BlockingModels())
    with TestClient(app) as client:
        with client.websocket_connect("/v1/realtime", subprotocols=ticket(client), headers={"origin": ORIGIN}) as ws:
            pcm = (np.ones(768 * 12) * 12000).astype("<i2").tobytes() + b"\0" * (1536 * 18)
            audio = {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
            ws.send_json(audio)
            first = [ws.receive_json() for _ in range(3)]
            assert entered.wait(5)
            ws.send_json({"type": "input_audio_buffer.clear"})
            ws.send_json(audio)
            second = [ws.receive_json() for _ in range(3)]
            assert second[0]["type"].endswith("speech_started")
            assert first[0]["item_id"] != second[0]["item_id"]
            release.set()
            final = ws.receive_json()
            assert final["item_id"] == second[0]["item_id"]
            assert final["transcript"] == "Next question"


@pytest.mark.parametrize("audio", ["???", base64.b64encode(b"x").decode(), "a" * 131073],
                         ids=["invalid-base64", "odd-pcm-byte", "oversized-frame"])
def test_malformed_audio_closes_connection(client, audio):
    with client.websocket_connect("/v1/realtime", subprotocols=ticket(client), headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "input_audio_buffer.append", "audio": audio})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_segmenter_preserves_onset_and_bounds_long_utterances():
    segmenter = Segmenter()
    frame = b"\0" * 1536
    for _ in range(30):
        assert not segmenter.feed_frame(frame, 0)
    assert segmenter.feed_frame(frame, 0.9)[0][0] == "start"
    assert len(segmenter.audio) == 19
    stop = []
    for _ in range(606):
        stop += segmenter.feed_frame(frame, 0.9)
    assert stop[0][0] == "stop" and len(stop[0][2]) == 625 * 1536
    segmenter.reset()
    assert not segmenter.audio and not segmenter.prefix and segmenter.item_id is None


def test_worker_cancellation_keeps_running_inference_serial_and_queue_bounded():
    async def run():
        worker = Worker(1)
        worker.start()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocking():
            entered.set()
            release.wait(5)
            calls.append("first")

        first = asyncio.create_task(worker.call(blocking))
        await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(worker.call(lambda: calls.append("second")))
        await asyncio.sleep(0)
        with pytest.raises(Busy):
            await worker.call(lambda: None)
        assert calls == []
        release.set()
        await second
        assert calls == ["first", "second"]
        await worker.close()
    asyncio.run(run())
