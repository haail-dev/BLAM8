"""Google grant-slot availability (Gmail mailboxes, and people's calendars).

Google's OAuth app is published (In production — refresh tokens do NOT expire
weekly) but unverified until the paid CASA assessment clears, which caps at 100
the number of Google users who may grant an UNAPPROVED SENSITIVE OR RESTRICTED
scope, behind an "unverified app" warning. Two things here ask for one:
connecting a Gmail mailbox (gmail.readonly + gmail.send — restricted) and a
person connecting their own calendar (calendar.events — sensitive, People slice
5). The cap counts users over the project's entire lifetime and cannot be
reset, so both are metered here. SSO sign-in only asks
for email+profile — basic scopes, uncapped — so signing in with Google is open
to everyone; it's CONNECTING a Gmail mailbox that consumes a slot. We surface
honest availability on the connect surfaces BEFORE a tenant clicks Gmail and
hits that wall — and nudge them to Microsoft (no cap) as the remaining slots
run low. GOOGLE_OAUTH_VERIFIED=true hides the indicator entirely (no cap once
verified).

The slot count is cached in-process for ~60s so a busy page never pays a DB
round-trip per visit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app import db
from app.config import settings

GOOGLE_TESTING_CAP = 100
INVITE_COOKIE = "blam8_ginvite"
INVITE_EMAIL_COOKIE = "blam8_ginvite_email"
INVITE_EMAIL_COOKIE_TTL_SECONDS = 10 * 60
_CACHE_TTL_SECONDS = 60.0
_cache: dict = {"at": -1e9, "used": 0}


def _cached_used() -> int:
    """Google grants in use — connected Gmail mailboxes PLUS people's own
    calendars — cached for _CACHE_TTL_SECONDS. time.monotonic() so it can't be
    skewed by a wall-clock change."""
    now = time.monotonic()
    if now - _cache["at"] > _CACHE_TTL_SECONDS:
        _cache["used"] = db.count_gmail_connections() + db.count_google_member_calendars()
        _cache["at"] = now
    return _cache["used"]


def google_slots() -> dict:
    """{used, cap, remaining, verified} for the Gmail-connect indicator.

    Once verified, the cap no longer applies — we skip the DB hit and report
    `verified: true` so callers hide the indicator. `low` (<= 20 remaining) and
    `full` (0 remaining) are convenience flags the template styles on."""
    if settings.google_oauth_verified:
        return {"used": 0, "cap": GOOGLE_TESTING_CAP, "remaining": GOOGLE_TESTING_CAP,
                "verified": True, "low": False, "full": False}
    used = _cached_used()
    remaining = max(0, GOOGLE_TESTING_CAP - used)
    return {"used": used, "cap": GOOGLE_TESTING_CAP, "remaining": remaining,
            "verified": False, "low": remaining <= 20, "full": remaining == 0}


# ── Gmail-connect invite gate ─────────────────────────────────────────────────
# While slots are scarce, connecting a GMAIL mailbox additionally requires an
# invite code (GOOGLE_INVITE_CODES, comma-separated, or single-use /admin codes)
# so public advertising can't burn the 100 unverified-app grants. Sign-in is
# never gated (either provider); neither is connecting Microsoft. A correct code
# sets a signed cookie, so returning users on the same browser aren't re-asked.
# The whole gate lifts once GOOGLE_OAUTH_VERIFIED=true.

def invite_gate_active() -> bool:
    """Armed by GOOGLE_INVITE_GATE=true (single-use codes minted in /admin/invites)
    or by any GOOGLE_INVITE_CODES being set; verification lifts it entirely."""
    if settings.google_oauth_verified:
        return False
    return settings.google_invite_gate or bool(settings.google_invite_codes.strip())


def invite_code_ok(code: str) -> bool:
    """Reusable 'channel' codes from env (never consumed)."""
    codes = {c.strip().lower() for c in settings.google_invite_codes.split(",") if c.strip()}
    return code.strip().lower() in codes


def redeem_invite(code: str) -> bool:
    """Accept a gate code: an env channel code (reusable), else a single-use
    operator invite from the DB (consumed atomically — see db.use_google_invite)."""
    if not code.strip():
        return False
    if invite_code_ok(code):
        return True
    try:
        return db.use_google_invite(code)
    except Exception:
        return False    # DB hiccup must fail closed, not 500 the login page


def _invite_sig() -> str:
    return hmac.new(settings.token_encryption_key.encode(),
                    b"blam8-google-invite-ok", hashlib.sha256).hexdigest()


def invite_cookie_value() -> str:
    """The value a passed invite sets — an HMAC, so it can't be forged client-side."""
    return _invite_sig()


def invite_cookie_valid(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, _invite_sig())


def normalize_google_email(email: str | None) -> str:
    return (email or "").strip().casefold()


def google_invite_email_available(email: str | None) -> bool:
    """Whether BLAM8 has an unused invite registered to this email."""
    try:
        return db.google_invite_email_available(normalize_google_email(email))
    except Exception:
        return False


def google_invite_email_cookie_value(email: str) -> str:
    """Short-lived signed proof of the invite email selected for OAuth."""
    normalized = normalize_google_email(email)
    payload = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")
    expires = str(int(time.time()) + INVITE_EMAIL_COOKIE_TTL_SECONDS)
    message = f"{payload}.{expires}".encode("ascii")
    sig = hmac.new(settings.token_encryption_key.encode(),
                   b"blam8-google-invite-email:" + message, hashlib.sha256).hexdigest()
    return f"{payload}.{expires}.{sig}"


def google_invite_email_from_cookie(value: str | None) -> str | None:
    """Verify the signed invite-email proof and return its normalized email."""
    if not value:
        return None
    try:
        payload, expires, sig = value.split(".", 2)
        if int(expires) < int(time.time()):
            return None
        message = f"{payload}.{expires}".encode("ascii")
        expected = hmac.new(settings.token_encryption_key.encode(),
                            b"blam8-google-invite-email:" + message,
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        email = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        return email or None
    except (ValueError, UnicodeDecodeError):
        return None
