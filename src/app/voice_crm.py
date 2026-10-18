"""Voice CRM — Kris's cold-call debrief tool (voice-crm-build-spec.md).

The flow behind /admin/voice-crm (routes in src/app/admin.py): paste a Google Maps
share link → resolve + parse it here (no Places API, just the redirect target's
URL), record a spoken debrief → Whisper transcript → one Claude extraction via
llm.create() → an editable review form → explicit Approve writes vcrm_* rows.
The audio is transcribed in memory and never stored.

Founder tooling: no tenant_id anywhere, service-role reads/writes only, and the
whole feature is designed to rip out (spec §6).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app import llm
from app.agent import client
from app.config import settings

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

CALL_STATUSES = ("NEW", "CALLED", "CALLBACK", "INTERESTED", "WON", "LOST", "JUNK")
# Partner pipeline (2026-10-01): bookkeepers/accountants reached by email, forum, event or
# LinkedIn rather than a cold call. The call debrief's extraction only ever picks CALL_STATUSES.
PARTNER_STATUSES = ("IDENTIFIED", "EMAILED", "REPLIED", "CALL_BOOKED", "SIGNED_UP", "NOT_NOW")
STATUSES = CALL_STATUSES + PARTNER_STATUSES
KINDS = ("customer", "partner")
CHANNELS = ("email", "forum", "event", "linkedin", "phone", "other")
OUTCOMES = ("no_answer", "gatekeeper", "decision_maker", "callback_arranged",
            "not_interested", "interested")

# Only known maps hosts are ever fetched — the resolver follows redirects, so
# the input must not be a lever to make the server GET arbitrary URLs.
_MAPS_HOSTS = {"maps.app.goo.gl", "goo.gl", "www.google.com", "google.com",
               "maps.google.com", "www.google.co.uk", "google.co.uk"}
# Apple Maps (2026-08-03, Kris's first live test): the share sheet on iPhone
# hands out maps.apple.com links — and some businesses only exist there, not on
# Google. "maps.apple" is the iOS 18+ short-link domain (maps.apple/p/…),
# which redirects to maps.apple.com.
_APPLE_HOSTS = {"maps.apple.com", "maps.apple"}


class VoiceCrmError(Exception):
    """A user-facing failure (bad link, missing key, transcription refusal) —
    the route turns it into a 4xx with the message, never a traceback."""


# ── Google Maps link parsing ─────────────────────────────────────────────────
def parse_maps_url(resolved_url: str) -> dict:
    """Pull business name / coordinates / address out of a RESOLVED Google Maps
    URL (pure — no network). Formats seen from share-link redirects:

      /maps/place/<name>/@lat,lng,zoom/data=…!3d<lat>!4d<lng>…
      /maps/place/<name+with+address>/data=…
      /maps/search/<query>/…  and  /maps?q=<query>

    The !3d/!4d pair is the place marker itself, so it beats the @-viewport
    centre when both are present. A place segment that reads "Name, Street,
    Town" keeps the first comma-part as the name and the whole string as the
    address — both editable on the review form anyway."""
    parsed = urllib.parse.urlparse(resolved_url)
    # EU consent interstitial wraps the real URL in ?continue=…
    if parsed.hostname and parsed.hostname.endswith("consent.google.com"):
        cont = urllib.parse.parse_qs(parsed.query).get("continue", [""])[0]
        if cont:
            return parse_maps_url(cont)

    out: dict = {"business_name": None, "address": None, "lat": None, "lng": None,
                 "maps_url": resolved_url}
    path = urllib.parse.unquote(parsed.path)

    m = re.search(r"/maps/(?:place|search)/([^/@]+)", path)
    raw_name = m.group(1) if m else urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
    raw_name = (raw_name or "").replace("+", " ").strip()
    if raw_name:
        if ", " in raw_name:
            out["business_name"] = raw_name.split(",", 1)[0].strip()
            out["address"] = raw_name
        else:
            out["business_name"] = raw_name

    marker = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", resolved_url)
    viewport = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", resolved_url)
    hit = marker or viewport
    if hit:
        out["lat"], out["lng"] = float(hit.group(1)), float(hit.group(2))
    return out


def parse_apple_maps_url(resolved_url: str) -> dict:
    """Apple Maps carries the place in query params (pure — no network):

      /place?name=<name>&address=<addr>&coordinate=<lat>,<lng>&place-id=…
      /?ll=<lat>,<lng>&q=<name>            (older share form)

    parse_qs already URL-decodes, so names and addresses come out readable."""
    parsed = urllib.parse.urlparse(resolved_url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    out: dict = {"business_name": None, "address": None, "lat": None, "lng": None,
                 "maps_url": resolved_url}
    name = (q.get("name") or q.get("q") or "").strip()
    if name:
        out["business_name"] = name
    address = (q.get("address") or "").strip()
    if address:
        out["address"] = address
    for key in ("coordinate", "ll", "sll"):
        m = re.match(r"^(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)$", (q.get(key) or "").strip())
        if m:
            out["lat"], out["lng"] = float(m.group(1)), float(m.group(2))
            break
    return out


async def resolve_maps_link(url: str) -> dict:
    """Follow a pasted share link (maps.app.goo.gl / maps.apple) to its full-URL
    form and parse it — Google and Apple Maps both accepted, dispatched on where
    the redirect LANDS. Validates the host BEFORE fetching (see _MAPS_HOSTS)."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in _MAPS_HOSTS | _APPLE_HOSTS:
        raise VoiceCrmError("That doesn't look like a Google or Apple Maps link.")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cl:
            r = await cl.get(url, headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError as exc:
        raise VoiceCrmError(f"Couldn't resolve the Maps link ({exc.__class__.__name__}).") from exc
    final_host = (urllib.parse.urlparse(str(r.url)).hostname or "").lower()
    if final_host in _APPLE_HOSTS:
        return parse_apple_maps_url(str(r.url))
    return parse_maps_url(str(r.url))


# ── Whisper transcription ────────────────────────────────────────────────────
async def transcribe(data: bytes, filename: str = "debrief.webm",
                     content_type: str = "audio/webm") -> str:
    """Audio bytes → text via the selected local or hosted Whisper provider.
    Every text LLM call stays on Anthropic.
    The bytes live only in this request; nothing is written anywhere."""
    from app import speech
    if speech.local():
        try:
            text = await speech.transcribe(data, content_type)
        except (httpx.HTTPError, ValueError) as exc:
            raise VoiceCrmError("Local transcription is unavailable — try again shortly.") from exc
        if not text:
            raise VoiceCrmError("Whisper heard nothing — try recording again.")
        return text
    if not settings.openai_api_key:
        raise VoiceCrmError("OPENAI_API_KEY is not set — add it in Render / .env first.")
    try:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": "whisper-1", "language": "en"},
                files={"file": (filename or "debrief.webm", data,
                                content_type or "audio/webm")},
            )
    except httpx.HTTPError as exc:
        raise VoiceCrmError(f"Transcription failed ({exc.__class__.__name__}).") from exc
    if r.status_code != 200:
        log.warning("voice_crm: whisper returned %s: %s", r.status_code, r.text[:500])
        raise VoiceCrmError(f"Transcription failed (Whisper {r.status_code}).")
    text = (r.json().get("text") or "").strip()
    if not text:
        raise VoiceCrmError("Whisper heard nothing — try recording again.")
    return text


# ── Claude field extraction ──────────────────────────────────────────────────
# Plain-string schema on purpose: structured output has rejected validation
# keywords before (the maxItems outage, 0cec322) — the allowed-value clamping
# happens server-side in clean_extraction instead.
_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string"},
        "summary": {"type": "string"},
        "status": {"type": "string"},
        "contact_name": {"type": ["string", "null"]},
        "next_action": {"type": ["string", "null"]},
        "next_action_date": {"type": ["string", "null"]},
        "business_name": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "website": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["outcome", "summary", "status"],
    "additionalProperties": False,
}

_PROMPT = """You are structuring the spoken debrief a founder recorded right after a cold
call to a prospective customer. Today is {weekday} {today}.

Return JSON with:
  outcome          — one of: no_answer | gatekeeper | decision_maker | callback_arranged | not_interested | interested
  summary          — 1-2 sentences: what happened on the call
  status           — the prospect's pipeline status after this call, one of:
                     NEW | CALLED | CALLBACK | INTERESTED | WON | LOST | JUNK
                     (spoke but nothing arranged → CALLED; call-back arranged → CALLBACK;
                      real interest → INTERESTED; told to never call again / dead → LOST or JUNK)
  contact_name     — the person spoken to / the decision maker, if named
  next_action      — the concrete next step in the founder's words, if any
  next_action_date — YYYY-MM-DD, resolving relative dates ("Thursday", "next week",
                     "in a fortnight") against today's date above; null if none
  business_name    — ONLY if the founder corrects or states the business name
  description      — what the business does, if mentioned
  location         — town/area, if mentioned
  phone / website  — if dictated
  notes            — anything else worth keeping that fits nowhere above

Rules:
- Use ONLY what was said. Never invent a name, date, or fact. Unsaid → null.
- The debrief is casual speech — ums, tangents, self-corrections. Keep the
  meaning, drop the noise.{prospect_context}"""


def _prospect_context(prospect: dict | None) -> str:
    if not prospect:
        return ""
    known = ", ".join(f"{k}={prospect[k]}" for k in
                      ("business_name", "contact_name", "status", "location")
                      if prospect.get(k))
    return (f"\n- Known prospect record: {known}. Only return a field if the "
            "debrief adds or corrects it.") if known else ""


def clean_extraction(raw: dict) -> dict:
    """Clamp the model's output to what the review form + tables accept: known
    outcome/status values, a real ISO date, stripped strings, unsaid → None."""
    def s(key):
        v = raw.get(key)
        v = str(v).strip() if v not in (None, "") else ""
        return v or None

    outcome = (s("outcome") or "").lower().replace(" ", "_")
    status = (s("status") or "").upper()
    nad = s("next_action_date")
    if nad:
        try:
            nad = date.fromisoformat(nad[:10]).isoformat()
        except ValueError:
            nad = None
    return {
        "outcome": outcome if outcome in OUTCOMES else "decision_maker",
        "summary": s("summary") or "",
        "status": status if status in CALL_STATUSES else "CALLED",
        "contact_name": s("contact_name"),
        "next_action": s("next_action"),
        "next_action_date": nad,
        "business_name": s("business_name"),
        "description": s("description"),
        "location": s("location"),
        "phone": s("phone"),
        "website": s("website"),
        "notes": s("notes"),
    }


async def extract_debrief(transcript: str, prospect: dict | None = None) -> dict:
    """One Claude call (through the llm.create choke point — operation
    'vcrm_debrief' in llm_calls) → cleaned fields + the raw output kept on the
    call row for auditability."""
    now = datetime.now(LONDON)
    prompt = _PROMPT.format(weekday=now.strftime("%A"), today=now.date().isoformat(),
                            prospect_context=_prospect_context(prospect))
    msg = await llm.create(
        client=client(), tenant_id=None, operation="vcrm_debrief", module="vcrm",
        model=settings.anthropic_model,
        max_tokens=1500,
        system=prompt,
        messages=[{"role": "user", "content": f"The debrief transcript:\n\n{transcript}"}],
        output_config={"format": {"type": "json_schema", "schema": _EXTRACTION_SCHEMA}},
    )
    text = "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text").strip()
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VoiceCrmError("The extraction came back malformed — try again.") from exc
    return {"fields": clean_extraction(raw), "raw": raw}
