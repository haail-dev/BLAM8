"""SMS channel transport (docs/modules/module-sms-channel.md).

The one file that knows Twilio exists. Everything else treats SMS as "an
email_log row whose channel is 'sms'": inbound texts are normalised into the
provider-neutral dict the pipeline already eats (sms_webhook.py), and approved
drafts leave through send_sms() below instead of the mailbox. Swap providers by
rewriting this file — no other module imports anything Twilio-shaped.

No SDK: the REST call is one httpx POST with basic auth, and the webhook
signature is HMAC-SHA1 per https://www.twilio.com/docs/usage/security — both
small enough that a dependency would cost more than it saves.
"""
import base64
import hashlib
import hmac
import logging
import math
import re

import httpx

from app import db
from app.config import settings

log = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"

# Outbound target the drafter is told to hit (spec D3): 2 GSM-7 segments. The
# send never truncates — an over-long human edit still goes out (and is metered
# for what it is); the inbox warns instead (spec §11).
MAX_DRAFT_SEGMENTS = 2


def configured() -> bool:
    """Platform-level Twilio credentials present? Blank = channel off everywhere."""
    return bool(settings.twilio_account_sid and settings.twilio_auth_token)


# ── Phone-number shapes ───────────────────────────────────────────────────────
_E164 = re.compile(r"\+[1-9]\d{6,14}$")


def is_phone_address(address: str | None) -> bool:
    """Does this email_log from_address hold a phone number (the SMS identity)
    rather than an email? The two shapes can't collide: E.164 has no '@'."""
    return bool(address) and bool(_E164.fullmatch(str(address).strip()))


def normalize_e164(number: str | None, default_cc: str = "+44") -> str | None:
    """Best-effort E.164 for UK-centric input: '07700 900123' -> '+447700900123',
    '00447...' -> '+447...', '+44 7700-900123' -> '+447700900123'. Twilio already
    sends E.164 in webhooks; this is for tenant-entered numbers and matching.
    None when the input can't plausibly be a phone number."""
    if not number:
        return None
    n = re.sub(r"[\s\-().]", "", str(number))
    if n.startswith("00"):
        n = "+" + n[2:]
    if n.startswith("0") and default_cc:
        n = default_cc + n[1:]
    elif not n.startswith("+") and n.isdigit() and len(n) >= 10:
        n = "+" + n  # already carries a country code, just unprefixed
    return n if _E164.fullmatch(n) else None


# ── Segment arithmetic (GSM 03.38) ────────────────────────────────────────────
# The basic GSM-7 set; ESC-extension chars cost two septets. Anything outside
# both forces UCS-2 (70 / 67-per-segment). Close enough for cost metering and
# the inbox over-length warning — carriers do the authoritative split.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = set("^{}\\[~]|€")


def segments(text: str | None) -> int:
    """How many SMS segments this text costs to send."""
    if not text:
        return 0
    septets = 0
    for ch in text:
        if ch in _GSM7:
            septets += 1
        elif ch in _GSM7_EXT:
            septets += 2
        else:  # UCS-2 fallback: count code units, 70 single / 67 concatenated
            n = len(text)
            return 1 if n <= 70 else math.ceil(n / 67)
    return 1 if septets <= 160 else math.ceil(septets / 153)


# ── Webhook authenticity ──────────────────────────────────────────────────────
def validate_signature(url: str, params: dict, signature: str | None) -> bool:
    """Twilio request signing: HMAC-SHA1(auth_token, url + k1v1k2v2... sorted by
    key), base64 — constant-time compared, like the Graph clientState check."""
    if not settings.twilio_auth_token:
        return False
    payload = url + "".join(k + str(v) for k, v in sorted(params.items()))
    digest = base64.b64encode(
        hmac.new(settings.twilio_auth_token.encode(), payload.encode("utf-8"),
                 hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(digest, signature or "")


def webhook_url(path: str) -> str:
    """The public URL Twilio signed — always the configured base, never the Host
    header (a proxy rewrite would break every signature otherwise)."""
    return settings.app_base_url.rstrip("/") + path


# ── Send ──────────────────────────────────────────────────────────────────────
class SmsNotConfigured(RuntimeError):
    """No active SMS connection for this tenant (or platform creds missing)."""


class SmsOptedOut(RuntimeError):
    """The recipient has texted STOP — sending is a compliance breach, not an error
    to retry. Callers surface it as a visible skip, never a silent drop."""


async def send_sms(tenant_id: str, to: str, body: str, *,
                   email_log_id: str | None = None, client=None) -> str:
    """Send one SMS from the tenant's provisioned number. Returns the provider
    message sid. Raises SmsOptedOut / SmsNotConfigured — callers decide how loud
    to be (the dashboard shows the error; jobs log and move on)."""
    to_e164 = normalize_e164(to)
    if not to_e164:
        raise ValueError(f"not a sendable phone number: {to!r}")
    if not configured():
        raise SmsNotConfigured("Twilio credentials are not set")
    conn = db.sms_connection(tenant_id, client=client)
    if not conn or not conn.get("phone_number"):
        raise SmsNotConfigured(f"tenant {tenant_id} has no active SMS number")
    if db.sms_opted_out(tenant_id, to_e164, client=client):
        raise SmsOptedOut(f"{to_e164} has opted out")
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            f"{TWILIO_API}/Accounts/{settings.twilio_account_sid}/Messages.json",
            data={"From": conn["phone_number"], "To": to_e164, "Body": body},
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        )
        resp.raise_for_status()
    sid = (resp.json() or {}).get("sid") or ""
    _record_send_cost(tenant_id, body, email_log_id)
    return sid


def _record_send_cost(tenant_id: str, body: str, email_log_id: str | None) -> None:
    """Pass-through metering (spec §8): one usage_events row per outbound, cost =
    segments × the configured per-segment rate. Best-effort like all metering."""
    try:
        n = segments(body)
        db.insert_usage_event({
            "tenant_id": tenant_id, "provider": "twilio", "model": "sms",
            "operation": "sms_send", "module": "sms",
            "input_tokens": 0, "output_tokens": n,  # segments ride the output column
            "cache_read_tokens": 0, "cache_write_tokens": 0, "embed_tokens": 0,
            "cost_usd": round(n * settings.twilio_sms_segment_usd, 6),
            "email_log_id": email_log_id,
        })
    except Exception:
        log.exception("sms: failed to meter send for tenant %s", tenant_id)


# ── Number provisioning (spec §2) ─────────────────────────────────────────────
async def provision_number(tenant_id: str) -> dict:
    """Buy the tenant one SMS+voice-capable UK **mobile** number (07 — UK local
    numbers are voice-only, spec §2) and point its webhooks at us. Returns the
    upserted channel_connections row. Idempotent by intent: an existing active
    connection is returned untouched, so a double-click can't buy two numbers."""
    if not configured():
        raise SmsNotConfigured("Twilio credentials are not set")
    existing = db.sms_connection(tenant_id)
    if existing and existing.get("phone_number"):
        return existing
    base = settings.app_base_url.rstrip("/")
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    acct = f"{TWILIO_API}/Accounts/{settings.twilio_account_sid}"
    async with httpx.AsyncClient(timeout=30) as http:
        avail = await http.get(
            f"{acct}/AvailablePhoneNumbers/GB/Mobile.json",
            params={"SmsEnabled": "true", "VoiceEnabled": "true", "PageSize": 1},
            auth=auth)
        avail.raise_for_status()
        numbers = (avail.json() or {}).get("available_phone_numbers") or []
        if not numbers:
            raise RuntimeError("Twilio has no UK mobile numbers available right now")
        buy = await http.post(
            f"{acct}/IncomingPhoneNumbers.json",
            data={"PhoneNumber": numbers[0]["phone_number"],
                  "SmsUrl": f"{base}/webhook/sms", "SmsMethod": "POST",
                  "VoiceUrl": f"{base}/webhook/voice", "VoiceMethod": "POST"},
            auth=auth)
        buy.raise_for_status()
        payload = buy.json() or {}
    return db.upsert_channel_connection(
        tenant_id, "sms",
        phone_number=payload.get("phone_number"),
        provider_sids={"number_sid": payload.get("sid")},
        config={"text_back_enabled": True})


# ── Preferred-channel resolution (spec §6) ────────────────────────────────────
def preferred_sms_destination(tenant_id: str, contact: str | None) -> str | None:
    """Where a reminder/recall for this booking contact should go by SMS — or
    None, meaning email stays the route. SMS wins only when it clearly should:

    - the contact IS a phone number (an SMS-booked customer — there is no email);
    - or the CRM contact for that email has a phone AND their 'Preferred contact'
      field (the industry-seeded CRM custom field, key `preferred_contact`) says
      SMS — and they haven't texted STOP, and the tenant's SMS channel is live.

    Best-effort: any lookup failure falls back to email — a reminder must never
    be lost to channel resolution."""
    c = (contact or "").strip()
    if not c:
        return None
    if is_phone_address(c):
        return c                      # SMS-booked: the phone is the only identity
    if "@" not in c:
        return None
    try:
        row = db.crm_contact_by_email(tenant_id, c) or {}
        phone = normalize_e164(row.get("phone"))
        if not phone:
            return None
        pref = str((row.get("custom_data") or {}).get("preferred_contact") or "")
        if pref.strip().lower() != "sms":
            return None
        if db.sms_opted_out(tenant_id, phone):
            return None               # STOP means STOP — email carries the reminder
        if not db.sms_connection(tenant_id):
            return None               # channel not active for this tenant
        return phone
    except Exception:
        log.warning("sms: preferred-channel lookup failed for tenant %s", tenant_id,
                    exc_info=True)
        return None


# ── Channel style (spec D3) ───────────────────────────────────────────────────
# Injected into the drafting/outbound prompts when the message travels as SMS.
# The tenant's whole voice config still applies; this narrows the register.
SMS_STYLE_HINT = (
    "This conversation happens over SMS TEXT MESSAGE, not email. Adjust the form, "
    "keep the voice:\n"
    "- Be brief: aim for one or two sentences, under ~300 characters total.\n"
    "- No subject line, no 'Dear ...' greeting ceremony, no multi-line sign-off "
    "block. Sign with the business or agent name once, briefly, only if the "
    "thread is new.\n"
    "- Plain text only — no markdown, no bullet lists, no links unless the "
    "customer asked for one.\n"
    "- Ask for at most one thing per message."
)


def style_hint(cfg: dict) -> str:
    """The SMS register block, plus any tenant override from channel_config.sms
    (e.g. {'sign_as': 'Sarah @ Example Services'})."""
    hint = SMS_STYLE_HINT
    sms_cfg = (cfg.get("channel_config") or {}).get("sms") or {}
    if sms_cfg.get("sign_as"):
        hint += f"\n- Sign as: {sms_cfg['sign_as']}"
    return hint
