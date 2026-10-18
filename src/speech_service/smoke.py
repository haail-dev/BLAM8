"""Synthetic audio only: exercise real models, HTTP and realtime without a mic."""
import argparse
import base64
import json
import secrets
import time

from fastapi.testclient import TestClient

from .config import Settings
from .server import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    args = parser.parse_args()
    key = secrets.token_urlsafe(32)
    headers = {"Authorization": "Bearer " + key}
    app = create_app(Settings(api_key=key, model_dir=args.model_dir,
                              allowed_origins="http://localhost:8000"))
    started = time.perf_counter()
    with TestClient(app) as client:
        print(json.dumps({"startup_seconds": round(time.perf_counter() - started, 3)}), flush=True)
        line = "Check the diary for tomorrow and tell me which appointments need confirming."
        started = time.perf_counter()
        response = client.post("/v1/audio/speech", headers=headers,
                               json={"input": line, "response_format": "wav"})
        response.raise_for_status()
        print(json.dumps({"tts_seconds": round(time.perf_counter() - started, 3)}), flush=True)
        started = time.perf_counter()
        transcript = client.post("/v1/audio/transcriptions", headers=headers, content=response.content)
        transcript.raise_for_status()
        assert "diary" in transcript.json()["text"].lower(), transcript.text
        print(json.dumps({"stt_seconds": round(time.perf_counter() - started, 3),
                          "transcript": transcript.json()["text"]}), flush=True)
        # Both alternate voices must exist in the installed asset bundle.
        for voice in ("bf_emma", "bm_george"):
            result = client.post("/v1/audio/speech", headers=headers, json={"input": "Hello.", "voice": voice})
            result.raise_for_status()
            assert len(result.content) > 2400
        import io
        import wave
        with wave.open(io.BytesIO(response.content)) as wav:
            pcm = wav.readframes(wav.getnframes())
        token = client.post("/v1/realtime/client_secrets", headers=headers, json={}).json()["value"]
        with client.websocket_connect("/v1/realtime", headers={"origin": "http://localhost:8000"},
                                      subprotocols=["realtime", "blam8-ticket." + token]) as ws:
            audio = b"\0" * 24000 + pcm + b"\0" * 48000
            for offset in range(0, len(audio), 3072):
                ws.send_json({"type": "input_audio_buffer.append",
                              "audio": base64.b64encode(audio[offset:offset + 3072]).decode()})
                time.sleep(0.064)
            events = []
            while True:
                event = ws.receive_json()
                events.append(event["type"].split(".")[-1])
                if events[-1] == "failed":
                    raise RuntimeError(event)
                if events[-1] == "completed":
                    assert "diary" in event["transcript"].lower(), event
                    print(json.dumps({"realtime_events": events, "transcript": event["transcript"]}), flush=True)
                    break
        print("Real-model service smoke passed.", flush=True)


if __name__ == "__main__":
    main()
