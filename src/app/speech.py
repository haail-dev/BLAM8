"""Provider configuration shared by live speech, samples and recorded debriefs."""
import re
from urllib.parse import urlsplit

import httpx

from app.config import settings


LOCAL_VOICES = {"af_heart": "Heart", "bf_emma": "Emma", "bm_george": "George"}
HOSTED_VOICES = {name: name.capitalize() for name in ("marin", "cedar", "alloy", "sage", "coral")}


def local():
    return settings.speech_provider == "local"


def ready():
    if not local():
        return bool(settings.openai_api_key)
    try:
        endpoint("/health")
        websocket_url()
    except ValueError:
        return False
    return bool(settings.speech_service_key)


def _valid_url(value, websocket=False):
    parsed = urlsplit(value)
    secure, insecure = ("wss", "ws") if websocket else ("https", "http")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc)
            or any(c.isspace() for c in value)
            or parsed.scheme not in (secure, insecure)
            or (parsed.scheme == insecure and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("Use TLS for the speech service, or loopback for development")
    return value.rstrip("/")


def websocket_origin():
    parsed = urlsplit(websocket_url())
    return parsed.scheme + "://" + parsed.netloc


def endpoint(path):
    return _valid_url(settings.speech_service_url) + path


def websocket_url():
    return _valid_url(settings.speech_service_ws_url, websocket=True)


def headers():
    if not settings.speech_service_key:
        raise ValueError("SPEECH_SERVICE_KEY is missing")
    return {"Authorization": "Bearer " + settings.speech_service_key}


def voice_choices():
    return LOCAL_VOICES if local() else HOSTED_VOICES


def resolve_voice(voice):
    choices = voice_choices()
    return voice if voice in choices else next(iter(choices))


async def mint_token(silence_ms, hint):
    url = endpoint("/v1/realtime/client_secrets")
    ws_url = websocket_url()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers(), json={
            "silence_ms": min(1200, max(200, silence_ms or 500)), "hint": hint or ""})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("value"), str) or not data["value"]:
        raise ValueError("Speech service returned no session ticket")
    return {"ok": True, "provider": "local", "value": data["value"],
            "expires_at": data.get("expires_at"), "websocket_url": ws_url,
            "captions": "same_session", "voice": resolve_voice("")}


async def transcribe(data, content_type):
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(endpoint("/v1/audio/transcriptions"),
                                     headers={**headers(), "Content-Type": content_type}, content=data)
        response.raise_for_status()
        data = response.json()
        text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise ValueError("Speech service returned an invalid transcript")
    return text.strip()
