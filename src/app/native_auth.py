"""Sign-in for the BLAM8 iOS apps (docs/architecture/native-sign-in.md).

Google refuses OAuth inside an app's embedded web view, so the iOS apps sign in
through the system sign-in sheet instead. The sheet proves who the person is;
the BLAM8 session itself starts back in the app's web view, where its invite,
referral and attribution cookies live. This module carries the result between
the two: a short-lived, single-use code bound (PKCE-style) to a verifier that
only the app holds, so a code intercepted on its way back is useless.

Pending codes live in this process's memory, like the public-ask sessions in
concierge_routes: the service runs a single uvicorn process (render.yaml). A
restart inside a code's two-minute life just means tapping sign-in again.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

NATIVE_COOKIE = "bl_native"
# Callback schemes registered by the two apps' Info.plists.
NATIVE_SCHEMES = frozenset({"blam8", "blam8-concierge"})
# Where each app may land once signed in.
NATIVE_NEXT_PATHS = frozenset({"/app", "/concierge/live"})
CODE_TTL_S = 120
MAX_PENDING = 1000
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")   # S256 of a 32-byte verifier


@dataclass(frozen=True)
class Pending:
    """A native sign-in in flight: which app, its challenge, where it lands."""
    scheme: str
    challenge: str
    next: str


def valid_challenge(challenge: str | None) -> bool:
    return bool(challenge) and _CHALLENGE_RE.fullmatch(challenge) is not None


def challenge_for(verifier: str) -> str:
    """The unpadded base64url S256 challenge for a verifier (RFC 7636)."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")


def set_cookie(response, pending: Pending) -> None:
    """Mark this browser (the app's sign-in sheet) as a native sign-in so the
    callback hands back a code instead of starting a web session."""
    value = base64.urlsafe_b64encode(json.dumps(
        {"s": pending.scheme, "c": pending.challenge, "n": pending.next}
    ).encode()).decode()
    response.set_cookie(NATIVE_COOKIE, value, max_age=600, httponly=True,
                        secure=True, samesite="lax", path="/auth/")


def clear_cookie(response) -> None:
    response.delete_cookie(NATIVE_COOKIE, path="/auth/")


def parse_pending(raw: str | None) -> Pending | None:
    """The native sign-in this cookie describes, or None when it is absent or
    fails any allow-list (a tampered cookie must never pick the scheme)."""
    if not raw:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()))
        pending = Pending(str(data["s"]), str(data["c"]), str(data["n"]))
    except Exception:
        return None
    if (pending.scheme in NATIVE_SCHEMES and pending.next in NATIVE_NEXT_PATHS
            and valid_challenge(pending.challenge)):
        return pending
    return None


def callback_url(scheme: str, **params: str) -> str:
    """`<scheme>://auth/complete?...` — where the sign-in sheet hands back."""
    return f"{scheme}://auth/complete?{urlencode(params)}"


_pending: dict[str, tuple[dict, str, str, float]] = {}
_lock = threading.Lock()


def _key(code: str) -> str:
    # Only a hash of each code is kept, never the code itself.
    return hashlib.sha256(code.encode()).hexdigest()


def issue(session: dict, challenge: str, next_path: str, now: float | None = None) -> str:
    """Park a GoTrue session under a new single-use code and return the code."""
    now = time.monotonic() if now is None else now
    code = secrets.token_urlsafe(32)
    with _lock:
        for key in [k for k, entry in _pending.items() if entry[3] <= now]:
            del _pending[key]
        while len(_pending) >= MAX_PENDING:
            del _pending[next(iter(_pending))]          # oldest first
        _pending[_key(code)] = (session, challenge, next_path, now + CODE_TTL_S)
    return code


def redeem(code: str, verifier: str, now: float | None = None) -> tuple[dict, str] | None:
    """(session, landing path) for a live code and its matching verifier.

    The code is consumed before the verifier is checked, so a wrong guess
    burns it rather than leaving it open to more attempts."""
    if not code or not verifier:
        return None
    now = time.monotonic() if now is None else now
    with _lock:
        entry = _pending.pop(_key(code), None)
    if entry is None:
        return None
    session, challenge, next_path, expires = entry
    if now >= expires or not hmac.compare_digest(challenge_for(verifier), challenge):
        return None
    return session, next_path
