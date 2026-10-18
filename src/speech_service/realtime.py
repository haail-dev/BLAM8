"""24kHz PCM turn detection and the event contract used by voice-turns.js."""
import asyncio
import base64
import binascii
import time
import uuid
from collections import deque
from contextlib import suppress

import numpy as np
from fastapi import WebSocketDisconnect

from .inference import Busy


class Segmenter:
    """32ms frames, 608ms pre-roll, hysteresis and a hard 20s segment limit."""
    def __init__(self, silence_ms=500):
        self.silence_frames = max(7, min(38, (silence_ms + 31) // 32))
        self.reset()

    def reset(self):
        self.pending = bytearray()
        self.prefix = deque(maxlen=19)
        self.context = deque(maxlen=16)
        self.audio = []
        self.item_id = None
        self.quiet = 0

    def feed_frame(self, frame, probability):
        events = []
        self.prefix.append(frame)
        if self.item_id is None:
            if probability < 0.5:
                return events
            self.item_id = uuid.uuid4().hex
            self.audio = list(self.prefix)
            events.append(("start", self.item_id, None))
        else:
            self.audio.append(frame)
        self.quiet = self.quiet + 1 if probability < 0.35 else 0
        if self.quiet >= self.silence_frames or len(self.audio) >= 625:
            events.append(("stop", self.item_id, b"".join(self.audio)))
            self.item_id = None
            self.audio = []
            self.prefix.clear()
            self.quiet = 0
        return events


def samples16(pcm):
    # Each frame has exactly 768 samples, so the 24→16kHz ratio has no drift.
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    return np.interp(np.arange(len(audio) * 2 // 3) * 1.5,
                     np.arange(len(audio)), audio).astype(np.float32)


async def conversation(ws, models, worker, ticket, session_seconds):
    segmenter = Segmenter(ticket["silence_ms"])
    send_lock = asyncio.Lock()
    jobs = set()
    generation = 0
    last_partial = 0
    partial_job = None
    received_bytes = 0
    started = time.monotonic()

    async def send(payload):
        async with send_lock:
            await ws.send_json(payload)

    async def transcribe(item_id, pcm, version, partial=False):
        try:
            text = await asyncio.wait_for(
                worker.call(models.transcribe, samples16(pcm), ticket["hint"], optional=partial),
                timeout=3 if partial else 12)
            if version != generation or (partial and item_id != segmenter.item_id):
                return
            await send({"type": "conversation.item.input_audio_transcription." +
                        ("partial" if partial else "completed"),
                        "item_id": item_id, "transcript": text})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not partial and version == generation:
                await send({"type": "conversation.item.input_audio_transcription.failed",
                            "item_id": item_id, "error": {
                                "code": "busy" if isinstance(exc, Busy) else "transcription_failed",
                                "message": "Listening is busy. Please try again."}})

    def launch(item_id, pcm, partial=False):
        job = asyncio.create_task(transcribe(item_id, pcm, generation, partial))
        jobs.add(job)
        job.add_done_callback(jobs.discard)
        return job

    try:
        async with asyncio.timeout(session_seconds):
            while True:
                message = await ws.receive_json()
                if not isinstance(message, dict):
                    await ws.close(code=1008)
                    return
                if message.get("type") == "input_audio_buffer.clear":
                    generation += 1
                    segmenter.reset()
                    for job in list(jobs):
                        job.cancel()
                    continue
                if message.get("type") != "input_audio_buffer.append":
                    continue
                encoded = message.get("audio", "")
                if not isinstance(encoded, str) or len(encoded) > 131072:
                    await ws.close(code=1009)
                    return
                try:
                    pcm = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    await ws.close(code=1008)
                    return
                if len(pcm) % 2:
                    await ws.close(code=1008)
                    return
                received_bytes += len(pcm)
                # Allow 3s of browser scheduling burst, not unbounded accelerated audio.
                if received_bytes > (time.monotonic() - started + 3) * 48000:
                    await ws.close(code=1008)
                    return
                segmenter.pending.extend(pcm)
                while len(segmenter.pending) >= 1536:
                    frame = bytes(segmenter.pending[:1536])
                    del segmenter.pending[:1536]
                    segmenter.context.append(samples16(frame))
                    probability = await asyncio.to_thread(
                        models.probability, np.concatenate(list(segmenter.context)))
                    for kind, item_id, audio in segmenter.feed_frame(frame, probability):
                        if kind == "start":
                            last_partial = time.monotonic()
                            await send({"type": "input_audio_buffer.speech_started", "item_id": item_id})
                        else:
                            if partial_job and not partial_job.done():
                                partial_job.cancel()
                            await send({"type": "input_audio_buffer.speech_stopped", "item_id": item_id})
                            await send({"type": "input_audio_buffer.committed", "item_id": item_id})
                            if len(jobs) >= 3:
                                await ws.close(code=1013)
                                return
                            launch(item_id, audio)
                    now = time.monotonic()
                    if (segmenter.item_id and now - last_partial >= 1.5
                            and (partial_job is None or partial_job.done())):
                        last_partial = now
                        partial_job = launch(segmenter.item_id, b"".join(segmenter.audio), partial=True)
    except TimeoutError:
        await ws.close(code=1001, reason="Session expired; reconnect to continue.")
    except WebSocketDisconnect:
        pass
    except ValueError:
        await ws.close(code=1008)
    finally:
        generation += 1
        for job in list(jobs):
            job.cancel()
        for job in list(jobs):
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await job
