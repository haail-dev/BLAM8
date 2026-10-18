# Shared local speech for Blam8

Blam8 can use a self-hosted faster-whisper and Kokoro service for all users.
The conversation brain and business tools stay in the web app. Customers only
need their browser and microphone; they install no models. Select the provider
with `SPEECH_PROVIDER=local`. The existing `openai` provider remains the default
until a reachable worker is configured. There is no automatic paid fallback.

## What changes

The worker accepts 24 kHz mono PCM16 over a WebSocket, detects speech with Silero,
and returns speech boundaries and final Whisper transcripts. Interim captions
are revisable snapshots on that same connection and never submit a business
action. Optional caption jobs yield to queued final transcriptions. The browser
retains its existing echo gate, barge-in, mute, discard, reconnection and turn
assembly. Local transcription may wait up to 12 seconds including queueing;
the browser allows 15 seconds before abandoning a missing final transcript.

Kokoro produces PCM at the existing player's 24 kHz sample rate. Speech is split
at sentence boundaries and at 25 words, so longer replies can start playing
before the entire reply has been synthesized. Heart, Emma and George have
separate voice samples and caches. Existing hosted voice preferences fall back
to Heart when switching to local; these are different voices, not imitations
of the hosted provider's voices. Voice CRM recorded debriefs use the same worker.

## Install and start

Run the worker on a host separate from the lightweight Blam8 web process.
Size that host using the measurements below and the expected concurrency;
do not assume the current Render starter instance can carry the models.
The service is CPU-first. Whisper device and compute type can be configured
for a separately provisioned compatible GPU runtime; the supplied image uses CPU.

From the repository root, in a dedicated Python 3.12 environment:

```sh
pip install -r deploy/speech/requirements.txt
pip install --no-deps -e .
python -m speech_service.download_models --model-dir /srv/blam8-models
```

Provisioning downloads the selected Whisper model and the fixed Kokoro model
assets. The two Kokoro SHA256 checksums match the assets tested in Relay.
Existing assets with different checksums are preserved and reported as errors.
Runtime loads local files only and warms the models before accepting requests.

Set these worker environment variables through the host's secret/config store:

```text
SPEECH_API_KEY=<random secret of at least 32 characters>
SPEECH_ALLOWED_ORIGINS=https://blam8.haail.dev
SPEECH_MODEL_DIR=/srv/blam8-models
SPEECH_WHISPER_MODEL=base.en
SPEECH_WHISPER_THREADS=4
SPEECH_KOKORO_THREADS=2
SPEECH_MAX_SESSIONS=8
SPEECH_QUEUE_SIZE=8
```

Origins are an exact comma-separated allowlist: include the actual Blam8
origin, including port for local development. Do not include paths or `*`.
Generate the service secret locally and store it only in the two server
environments, never in browser code or a committed file.

```sh
uvicorn speech_service.server:app --host 127.0.0.1 --port 8090 --workers 1 --ws-max-size 196608 --ws-max-queue 4
```

Put the worker behind HTTPS/WSS, with WebSocket upgrade support, a request-body
limit of 12 MiB and an idle timeout exceeding 30 minutes. Forward the `Origin`
and `Sec-WebSocket-Protocol` headers. Do not log authorization or WebSocket
protocol headers: the latter carries a short-lived ticket. HTTP and WS are
allowed without TLS only on loopback during development.

Use **one worker and one replica** for this initial deployment. Session tickets
and capacity limits are held in that process. Multiple workers/replicas need a
shared ticket store, shared limits and routing before they can be enabled.

Alternatively, build the supplied image with the repository root as context:

```sh
docker build -f deploy/speech/Dockerfile -t blam8-speech .
```

Mount the provisioned model directory at `/models` and provide the worker
environment variables securely. The container's unprivileged user needs read
access to the model files and directories. The image does not contain models.
It listens on port 8090; expose it only through the configured TLS proxy.
Container construction and deployment are separate from the tested Python path.

## Connect the web app

Configure and restart Blam8 after the worker health check succeeds:

```text
SPEECH_PROVIDER=local
SPEECH_SERVICE_URL=https://speech.your-domain.example
SPEECH_SERVICE_WS_URL=wss://speech.your-domain.example/v1/realtime
SPEECH_SERVICE_KEY=<same secret as worker SPEECH_API_KEY>
```

The HTTP base URL has no `/v1` suffix. The WebSocket URL includes its full path
and must be reachable from customer browsers. The configured WebSocket origin
is added to Blam8's content security policy. `VOICE_LIVE_ENABLED` remains the
UI off switch. No OpenAI key is needed for local speech.

The existing tenant authentication, CSRF checks, request limits and
assistant-reply-only TTS authorization remain in the app. The worker has no
database credentials, tenant data store or business tools. Blam8 obtains a
random single-use ticket valid for 60 seconds; a browser connection can last
30 minutes before reconnecting. The long-lived worker key stays on the server.
Context hints supplied by the app remain scoped to the requesting user's
existing access permissions.

Audio stays in memory in the worker. Recorded uploads are capped at 12 MiB and
five decoded minutes; live segments at 20 seconds. Model execution uses separate
bounded queues for STT and TTS. Disconnecting cancels queued work; an inference
already executing finishes in its thread without starting overlapping calls.
An overloaded worker returns an error instead of creating an unlimited backlog.

## Validation and rollout

```sh
python -m pytest tests/speech -q
python tests/run_offline.py tests/test_local_speech.py tests/test_voice_live.py tests/test_voice_live_client.py tests/test_voice_crm.py tests/test_voice_mishearing.py tests/test_voice_discard.py tests/test_security_concierge_scopes.py -q
python -m speech_service.smoke --model-dir /srv/blam8-models
```

The smoke command also needs `httpx` for FastAPI's TestClient. It synthesizes a
fixed diary question, decodes/transcribes its WAV, verifies alternate voices,
and sends the PCM at real-time speed through the WebSocket. It does not open a
microphone, play speakers, send messages or call a paid speech API.

On 11 October 2026, a local CPU test using Relay's installed speech libraries
and existing model assets produced the correct 12-word diary question through
both recorded and realtime routes. Realtime emitted speech start, two interim
captions, speech stop, commit and final transcript in order. That run measured
5.15 seconds startup, 2.53 seconds complete sentence synthesis and 0.82 seconds
recorded transcription. These are single-run synthetic measurements, not a
concurrency benchmark, accuracy evaluation or proof of improved browser latency.

Before switching a hosted deployment, verify actual browser microphone capture,
quiet speech, names, numbers, silence/background noise, short yes/no answers,
interruptions, discarded turns, reconnects, and multiple simultaneous users.
Measure end-of-speech to final transcript, first spoken reply, p50/p95 latency,
memory and CPU on the intended host. Listen to the generated voices. Local
inference removes per-request hosted STT/TTS charges, but compute and hosting
still cost money. LLM and search costs are unchanged.

Rollback: set `SPEECH_PROVIDER=openai`, ensure `OPENAI_API_KEY` is present,
restart the web app and reconnect active browser conversations. There are no
database changes or migrations. The self-hosted service can then be stopped.

## Sources

The implementation follows Relay's `relay/audio.py` and `relay/neural_voice.py`
and the installed versions of [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and [Kokoro ONNX](https://github.com/thewh1teagle/kokoro-onnx).
Kokoro assets come from the project's
[model release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0).
