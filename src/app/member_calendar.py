"""A person's own calendar (People slice 5 — docs/plan-people.md).

Kris, 2026-09-13, after watching his wife accept an invitation and seeing
Google name Calendar on the consent screen: "she can see the business Calendar
in BLAM8 while having her own Calendar to have events synced to… another
calendar for the business, mostly used to delegate work from the main calendar."

The diary already pushes every confirmed booking into ONE business calendar.
This is the second half: each person may connect their own, and the work
delegated to them by kind lands there too — so a change made in BLAM8 reaches
the calendar already on their phone without them opening BLAM8 at all.

Three deliberate lines:

  * The grant is CALENDAR ONLY. No mail scope is asked for, so the token cannot
    read anyone's inbox even by accident, and (on Google) it is a *sensitive*
    rather than a *restricted* scope — it never spends one of the 100 pre-CASA
    grants src/app/slots.py counts, because those are Gmail MAILBOX connects and
    nothing here writes email_connections.
  * Google's incremental consent is switched OFF for this flow. An owner who
    connected the business mailbox must not get a mail-capable token back when
    they connect their personal calendar.
  * Nothing is written to a calendar that has not been delegated by kind
    (people.does_booking). Someone's calendar is their own property; filling it
    by default is worse than filling it by omission.

Everything is best-effort, like the business calendar: a dead grant records its
reason on the person's page and is skipped, never raised at the diary.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timezone

from app import db, gmail, graph

log = logging.getLogger(__name__)

# The intent carried through the consent round-trip, in oauth_states.mode.
MODE = "mycal"

PROVIDERS = ("google", "microsoft")


def backend(provider: str):
    """gmail for Google, graph for Microsoft — both expose the same
    create/delete calendar calls, so the push loop is provider-agnostic."""
    return gmail if provider == "google" else graph


def normalise_provider(provider: str | None) -> str:
    p = (provider or "").strip().lower()
    return p if p in PROVIDERS else "google"


def scopes(provider: str) -> str:
    return backend(normalise_provider(provider)).CALENDAR_SCOPES


def authorize_url(provider: str, state: str) -> str:
    """Where the person is sent to consent. Google takes the extra
    `incremental=False` so the token comes back holding only what this screen
    asked for."""
    provider = normalise_provider(provider)
    if provider == "google":
        return gmail.build_authorize_url(state, scope=gmail.CALENDAR_SCOPES,
                                         incremental=False)
    return graph.build_authorize_url(state, scope=graph.CALENDAR_SCOPES)


async def exchange(provider: str, code: str) -> dict:
    provider = normalise_provider(provider)
    if provider == "google":
        return await gmail.exchange_code_for_tokens(code)
    return await graph.exchange_code_for_tokens(code, scope=graph.CALENDAR_SCOPES)


async def refresh(provider: str, refresh_token: str) -> dict:
    provider = normalise_provider(provider)
    if provider == "google":
        return await gmail.refresh_tokens(refresh_token)
    return await graph.refresh_tokens(refresh_token, scope=graph.CALENDAR_SCOPES)


def account_email(tokens: dict) -> str | None:
    """Which account they actually consented with, read from the id_token both
    providers return alongside the access token when `openid email` is asked
    for.

    The signature is NOT verified, and does not need to be: this JWT came
    straight back from the provider's own token endpoint over TLS in a
    server-to-server request, which is the one case the OpenID spec itself says
    may skip verification. It is used only to LABEL the connection on the
    person's page — nothing is authorised by it.

    Reading the calendar's own address through the API is not an option here:
    calendars.get needs a broader scope than calendar.events, and widening the
    grant to print an address would be a bad trade."""
    raw = (tokens or {}).get("id_token")
    if not raw or raw.count(".") != 2:
        return None
    payload = raw.split(".")[1]
    try:
        pad = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    for key in ("email", "preferred_username", "upn"):
        value = str(claims.get(key) or "").strip()
        if "@" in value:
            return value.lower()
    return None


def expired(cal: dict, now: datetime | None = None) -> bool:
    raw = (cal or {}).get("token_expires")
    if not raw:
        return True
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= when


async def live_token(cal: dict) -> str:
    """A usable access token for one person's calendar, refreshed and saved if
    the stored one has run out. Raises if the grant is dead — the caller
    records the reason against the person and moves on to the next."""
    provider = normalise_provider(cal.get("provider"))
    if not expired(cal):
        return cal["access_token"]
    if not cal.get("refresh_token"):
        raise RuntimeError("no refresh token")
    tokens = await refresh(provider, cal["refresh_token"])
    access = tokens["access_token"]
    # Google reuses the refresh token and usually omits it from the response.
    keep = tokens.get("refresh_token") or cal["refresh_token"]
    expires = backend(provider).expiry_from_now(tokens["expires_in"])
    db.update_member_calendar_tokens(cal["tenant_id"], cal["user_id"], access, keep, expires)
    cal["access_token"], cal["refresh_token"] = access, keep
    cal["token_expires"] = expires.isoformat()
    return access


def why_it_stopped(exc: BaseException) -> str:
    """The reason a person's own page gives them. Their calendar going quiet
    with no explanation is the failure mode worth spending a column on — and
    the words have to mean something to someone who has never heard of OAuth."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (400, 401):
            return "Your calendar needs connecting again — the permission ran out."
        if code == 403:
            return ("Google or Microsoft refused the change. Reconnect, and say yes "
                    "to the calendar permission.")
        if code == 404:
            return "We couldn't find that calendar any more."
        return f"Your calendar provider returned an error ({code})."
    if isinstance(exc, RuntimeError) and "refresh token" in str(exc):
        return "Your calendar needs connecting again — the permission ran out."
    return "We couldn't reach your calendar. It will try again on its own."


def face(cal: dict | None) -> dict:
    """What the person's page shows about their calendar, in one shape."""
    if not cal:
        return {"connected": False, "email": "", "provider": "", "problem": ""}
    return {"connected": True,
            "email": (cal.get("calendar_email") or "").strip(),
            "provider": normalise_provider(cal.get("provider")),
            "problem": (cal.get("last_error") or "").strip()}
