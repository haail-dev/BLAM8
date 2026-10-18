"""Relay's CPU speech models, with no microphone or speaker device dependency."""
import io
import re

import numpy as np


VOICES = {"af_heart": "Heart", "bf_emma": "Emma", "bm_george": "George"}


def speech_chunks(text):
    for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip()):
        words = sentence.split()
        while words:
            # Short first audio, then natural sentence boundaries where possible.
            part, words = words[:25], words[25:]
            yield " ".join(part)


class Models:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        import onnxruntime as ort
        from faster_whisper import WhisperModel
        from faster_whisper.vad import get_vad_model
        from kokoro_onnx import Kokoro

        cfg = self.settings
        self.whisper = WhisperModel(
            cfg.whisper_model, device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type, cpu_threads=cfg.whisper_threads,
            download_root=str(cfg.model_dir), local_files_only=True,
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = cfg.kokoro_threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(cfg.model_dir / "kokoro" / "kokoro-v1.0.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"],
        )
        self.kokoro = Kokoro.from_session(session, str(cfg.model_dir / "kokoro" / "voices-v1.0.bin"))
        self.vad = get_vad_model()
        # Warm both engines before the health endpoint can report readiness.
        pcm = self.speak("Ready to listen.", "af_heart")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        self.transcribe(np.interp(np.arange(len(samples) * 2 // 3) * 1.5,
                                  np.arange(len(samples)), samples).astype(np.float32))

    def transcribe(self, samples, hint=""):
        segments, _ = self.whisper.transcribe(
            samples, language="en", beam_size=1, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=hint or None,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe_file(self, data):
        # Decode incrementally: a small compressed file must not expand without limit.
        import av
        samples = []
        length = 0
        with av.open(io.BytesIO(data)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                for output in resampler.resample(frame):
                    part = output.to_ndarray().reshape(-1)
                    length += len(part)
                    if length > 16000 * 300:
                        raise ValueError("Recordings are limited to five minutes.")
                    samples.append(part)
            for output in resampler.resample(None):
                samples.append(output.to_ndarray().reshape(-1))
        if not samples:
            return ""
        audio = np.concatenate(samples).astype(np.float32) / 32768
        if len(audio) > 16000 * 300:
            raise ValueError("Recordings are limited to five minutes.")
        return self.transcribe(audio)

    def speak(self, text, voice):
        samples, rate = self.kokoro.create(
            text, voice=voice, speed=0.94,
            lang="en-gb" if voice.startswith("b") else "en-us",
        )
        if rate != 24000:
            raise RuntimeError("Speech model returned an unexpected sample rate")
        return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()

    def probability(self, samples):
        # Bounded sliding context, not a fresh state for every 32ms frame.
        audio = np.pad(samples, (0, (-len(samples)) % 512))
        return float(np.asarray(self.vad(audio)).reshape(-1)[-1])
