"""Run one worker behind TLS: uvicorn speech_service.server:app --port 8090."""
import asyncio
import io
import secrets
import time
import wave
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .inference import Busy, Worker
from .models import Models, VOICES, speech_chunks
from .realtime import conversation


class TokenRequest(BaseModel):
    silence_ms: int = Field(500, ge=200, le=1200)
    hint: str = Field("", max_length=1200)


class SpeechRequest(BaseModel):
    input: str = Field(..., min_length=1, max_length=4000)
    voice: Literal["af_heart", "bf_emma", "bm_george"] = "af_heart"
    response_format: Literal["pcm", "wav"] = "pcm"


def create_app(settings=None, models=None):
    cfg = settings or Settings()
    engines = models or Models(cfg)
    tickets = {}
    sessions = set()

    @asynccontextmanager
    async def lifespan(app):
        if len(cfg.api_key) < 32 or not cfg.origins or "*" in cfg.origins:
            raise RuntimeError("Set SPEECH_API_KEY (32+ characters) and explicit SPEECH_ALLOWED_ORIGINS")
        await asyncio.to_thread(engines.load)
        app.state.stt = Worker(cfg.queue_size)
        app.state.tts = Worker(cfg.queue_size)
        app.state.stt.start()
        app.state.tts.start()
        try:
            yield
        finally:
            await app.state.stt.close()
            await app.state.tts.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str = Header("")):
        if not cfg.api_key or not secrets.compare_digest(
                authorization.encode("utf-8"), ("Bearer " + cfg.api_key).encode("utf-8")):
            raise HTTPException(401, "Invalid speech service credentials")

    @app.get("/health")
    async def health():
        return {"ok": True, "stt": "faster-whisper", "tts": "kokoro", "voices": VOICES}

    @app.post("/v1/realtime/client_secrets", dependencies=[Depends(authorize)])
    async def token(body: TokenRequest):
        now = time.monotonic()
        for value in list(tickets):
            if tickets[value]["expires"] <= now:
                del tickets[value]
        if len(tickets) >= cfg.max_sessions * 4 or len(sessions) >= cfg.max_sessions:
            raise HTTPException(503, "Speech service is at capacity")
        value = secrets.token_urlsafe(32)
        tickets[value] = {"expires": now + 60, "silence_ms": body.silence_ms, "hint": body.hint}
        return {"value": value, "expires_at": int(time.time()) + 60}

    @app.websocket("/v1/realtime")
    async def realtime(ws: WebSocket):
        if ws.headers.get("origin") not in cfg.origins:
            await ws.close(code=1008)
            return
        protocols = ws.scope.get("subprotocols", [])
        value = next((p.removeprefix("blam8-ticket.") for p in protocols if p.startswith("blam8-ticket.")), "")
        ticket = tickets.pop(value, None)  # single-use; reconnect obtains another ticket
        if ("realtime" not in protocols or not ticket or ticket["expires"] <= time.monotonic()
                or len(sessions) >= cfg.max_sessions):
            await ws.close(code=1008)
            return
        sessions.add(value)
        try:
            await ws.accept(subprotocol="realtime")
            await conversation(ws, engines, app.state.stt, ticket, cfg.session_seconds)
        finally:
            sessions.discard(value)

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(authorize)])
    async def transcription(request: Request):
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > 12 * 1024 * 1024:
                raise HTTPException(413, "Recording is too large")
        if not audio:
            raise HTTPException(400, "Recording is empty")
        try:
            text = await app.state.stt.call(engines.transcribe_file, bytes(audio))
        except Busy:
            raise HTTPException(503, "Listening is busy; try again") from None
        except Exception:
            raise HTTPException(422, "Could not transcribe this recording (maximum five minutes)") from None
        return {"text": text}

    @app.post("/v1/audio/speech", dependencies=[Depends(authorize)])
    async def speech(body: SpeechRequest):
        chunks = iter(speech_chunks(body.input))
        first = next(chunks, None)
        if first is None:
            raise HTTPException(422, "Speech text is empty")
        try:
            audio = await app.state.tts.call(engines.speak, first, body.voice)
        except Busy:
            raise HTTPException(503, "Speaking is busy; try again") from None
        except Exception:
            raise HTTPException(503, "Speech model unavailable") from None

        if body.response_format == "wav":
            # Browser voice samples need a container; conversation playback uses PCM.
            try:
                for part in chunks:
                    audio += await app.state.tts.call(engines.speak, part, body.voice)
            except Exception:
                raise HTTPException(503, "Speech model unavailable") from None
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(audio)
            return Response(buffer.getvalue(), media_type="audio/wav", headers={"Cache-Control": "no-store"})

        async def stream():
            yield audio
            for part in chunks:
                # No background pre-generation: disconnect cancels queued future work.
                yield await app.state.tts.call(engines.speak, part, body.voice)

        return StreamingResponse(stream(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store"})

    return app


app = create_app()
