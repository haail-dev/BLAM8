"""View as — an owner sees BLAM8 exactly as one of their people does.

Kris 2026-09-24: "build View as Em for me to access via the menu on my account
until it's built, then we pull it into its own." The staff side of People had
never been seen on screen: nobody can sign in as Em but Em.

How it works: a signed cookie names the person being viewed. app.auth
.require_tenant — the one place a request learns who it is — checks the cookie
(the real signed-in user must be an OWNER of this tenant, the person must be an
active member of the same tenant, and it expires) and then answers every
question as that person: doors, kinds, the CRM they see, Today's notes, what
the Concierge offers. Nothing downstream has to know.

It is READ-ONLY. While viewing, any request that could change something is
refused before it reaches a handler (see may_write), except leaving View as
and asking the Concierge a question — and even then nothing is saved under the
person's name and no card is set up (concierge_routes checks ctx.viewing_as).
"""
from __future__ import annotations

import hashlib
import hmac
import time

from app.config import settings

COOKIE = "bl_viewas"
MAX_AGE_S = 4 * 3600

# The only writes allowed while viewing: leaving, and asking the Concierge
# (answered as the person, never saved, never staged — see concierge_routes).
_WRITE_OK = (
    "/app/view-as/",
    "/concierge/business",           # a typed question (exact path below)
    "/concierge/voice/token",
    "/concierge/voice/chat",
    "/concierge/voice/tts",
    "/concierge/voice/heard",
)
_WRITE_EXACT = {"/concierge/business"}


def _sig(payload: str) -> str:
    return hmac.new(settings.token_encryption_key.encode(), f"viewas:{payload}".encode(),
                    hashlib.sha256).hexdigest()


def sign(real_id: str, target_id: str, tenant_id: str) -> str:
    payload = f"{real_id}.{target_id}.{tenant_id}.{int(time.time())}"
    return f"{payload}.{_sig(payload)}"


def read(cookie: str | None) -> tuple[str, str, str] | None:
    """(real_id, target_id, tenant_id) from a valid, unexpired cookie, else None."""
    parts = (cookie or "").split(".")
    if len(parts) != 5:
        return None
    real_id, target_id, tenant_id, ts, sig = parts
    payload = f"{real_id}.{target_id}.{tenant_id}.{ts}"
    if not hmac.compare_digest(sig, _sig(payload)):
        return None
    try:
        if time.time() - int(ts) > MAX_AGE_S:
            return None
    except ValueError:
        return None
    return real_id, target_id, tenant_id


def may_write(method: str, path: str) -> bool:
    """Can this request run while viewing as someone? Reads always; writes only
    to leave View as or to ask the Concierge."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if path in _WRITE_EXACT:
        return True
    return any(path.startswith(p) for p in _WRITE_OK if p not in _WRITE_EXACT)
