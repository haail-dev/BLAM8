"""Microsoft Graph OAuth (authorization-code flow) + minimal Graph API calls.

We talk to the Microsoft identity platform v2.0 endpoints directly with httpx
rather than via MSAL, because the spec stores the raw access/refresh tokens in
`email_connections` and the background agent refreshes them itself.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.config import settings

AUTHORITY = f"https://login.microsoftonline.com/{settings.azure_tenant_id}"
AUTHORIZE_URL = f"{AUTHORITY}/oauth2/v2.0/authorize"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# offline_access => refresh token for the background agent (spec §4).
# Calendars.ReadWrite (Calendar Connect v1.1): push confirmed bookings into the
# tenant's calendar + read busy events into the diary. Existing tenants gain it
# at their next re-consent (the Diary's connect banner sends them through).
SCOPES = "offline_access Mail.Read Mail.Send User.Read Calendars.ReadWrite"

# What ONE PERSON's own calendar connect asks for (People slice 5) — their
# calendar and their name, never their mail. See app/gmail.CALENDAR_SCOPES.
CALENDAR_SCOPES = "offline_access openid email Calendars.ReadWrite"


def build_authorize_url(state: str, scope: str | None = None) -> str:
    params = {
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": settings.azure_redirect_uri,
        "response_mode": "query",
        "scope": scope or SCOPES,
        "state": state,
        # Always let the user choose which mailbox to connect, rather than
        # silently reusing whatever Microsoft account is already signed in.
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str, scope: str | None = None) -> dict:
    data = {
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.azure_redirect_uri,
        "scope": scope or SCOPES,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()


async def refresh_tokens(refresh_token: str, scope: str | None = None) -> dict:
    """`scope` narrows the refresh to what the grant actually holds — a
    person's calendar-only token must never ask for mail scopes back."""
    data = {
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope or SCOPES,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()


def expiry_from_now(expires_in: int) -> datetime:
    """When the access token should be treated as expired (60s safety margin)."""
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)


async def graph_get(
    access_token: str,
    path: str,
    params: dict | None = None,
    extra_headers: dict | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{GRAPH_BASE}{path}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()


async def graph_post(
    access_token: str, path: str, json: dict | None = None, extra_headers: dict | None = None
) -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{GRAPH_BASE}{path}", headers=headers, json=json)
        r.raise_for_status()
        return r.json() if r.content else {}


async def list_recent_inbox_ids(access_token: str, days: int = 14, limit: int = 100) -> list[str]:
    """Ids of Inbox messages received in the last `days`, newest first, at
    most `limit` — the backlog a fresh connection brings in (src/app/backlog.py).
    Ids only; the pipeline's own fetch reads each message."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = await graph_get(
        access_token, "/me/mailFolders/inbox/messages",
        params={"$filter": f"receivedDateTime ge {since}", "$top": str(min(int(limit), 100)),
                "$select": "id", "$orderby": "receivedDateTime desc"})
    return [m["id"] for m in (data.get("value") or []) if m.get("id")]


async def list_sent_messages(access_token: str, top: int = 300) -> list[dict]:
    """Up to `top` most-recent Sent Items as plain text, following
    @odata.nextLink pages (Graph caps a page well below 300). Shape:
    {subject, body, from_address, to_addresses, sent}."""
    headers = {"Authorization": f"Bearer {access_token}",
               "Prefer": 'outlook.body-content-type="text"'}
    url = f"{GRAPH_BASE}/me/mailFolders/sentitems/messages"
    params: dict | None = {
        "$top": "100",
        "$select": "subject,body,from,toRecipients,sentDateTime",
        "$orderby": "sentDateTime desc",
    }
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=60) as c:
        for _ in range(10):                     # paging cap — 300 needs 3 pages
            r = await c.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            for m in data.get("value", []):
                sender = ((m.get("from") or {}).get("emailAddress") or {})
                out.append({
                    "subject": m.get("subject") or "",
                    "body": ((m.get("body") or {}).get("content") or "").strip(),
                    "from_address": sender.get("address"),
                    "to_addresses": [((t or {}).get("emailAddress") or {}).get("address")
                                     for t in (m.get("toRecipients") or [])],
                    "sent": m.get("sentDateTime"),
                })
            if len(out) >= top:
                return out[:top]
            url, params = data.get("@odata.nextLink"), None
            if not url:
                break
    return out


async def graph_patch(access_token: str, path: str, json: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(f"{GRAPH_BASE}{path}", headers=headers, json=json)
        r.raise_for_status()
        return r.json() if r.content else {}


# ── Calendar (Calendar Connect v1.1 — Outlook first) ────────────────────────
_LONDON = "Europe/London"


def _graph_dt(value: dict) -> datetime:
    """Graph returns {'dateTime': '2026-06-17T14:00:00.0000000', 'timeZone': ...}.
    We request London times via the Prefer header; trim the 7-digit fraction
    (fromisoformat rejects it) and attach the zone."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(value["dateTime"][:19]).replace(
        tzinfo=ZoneInfo(value.get("timeZone") or _LONDON))


async def list_calendar_events(access_token: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Busy events in [start, end) from the default calendar, normalised to
    {'id','subject','start','end','all_day'} with aware datetimes. Free-marked
    events are dropped — they shouldn't block the diary."""
    from zoneinfo import ZoneInfo
    fmt = "%Y-%m-%dT%H:%M:%S"
    london = ZoneInfo(_LONDON)
    headers = {"Authorization": f"Bearer {access_token}",
               "Prefer": f'outlook.timezone="{_LONDON}"'}
    url = f"{GRAPH_BASE}/me/calendarView"
    params = {"startDateTime": start_dt.astimezone(london).strftime(fmt),
              "endDateTime": end_dt.astimezone(london).strftime(fmt),
              "$select": "id,subject,start,end,isAllDay,showAs", "$top": "50"}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(6):                      # paging cap — plenty for a horizon
            r = await c.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            for e in data.get("value", []):
                if e.get("showAs") in ("free", "workingElsewhere"):
                    continue
                out.append({
                    "id": e["id"],
                    "subject": e.get("subject") or "Busy",
                    "all_day": bool(e.get("isAllDay")),
                    "start": _graph_dt(e["start"]),
                    "end": _graph_dt(e["end"]),
                })
            url, params = data.get("@odata.nextLink"), None
            if not url:
                break
    return out


async def create_calendar_event(
    access_token: str, subject: str, start_iso: str, end_iso: str,
    all_day: bool = False, body_text: str = "",
) -> str:
    """Create an event in the tenant's default calendar; returns the event id.
    start/end are naive London wall-times ('2026-06-11T14:00:00')."""
    payload = {
        "subject": subject,
        "isAllDay": all_day,
        "start": {"dateTime": start_iso, "timeZone": _LONDON},
        "end": {"dateTime": end_iso, "timeZone": _LONDON},
        "body": {"contentType": "text", "content": body_text},
        "showAs": "busy",
    }
    data = await graph_post(access_token, "/me/events", json=payload)
    return data["id"]


async def delete_calendar_event(access_token: str, event_id: str) -> None:
    """Remove an event BLAM8 created (booking cancelled). 404 = already gone."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{GRAPH_BASE}/me/events/{event_id}", headers=headers)
        if r.status_code not in (204, 404):
            r.raise_for_status()


def _subscription_expiry() -> str:
    """Messages subscriptions max out near 4230 minutes; renew well before that."""
    return (datetime.now(timezone.utc) + timedelta(minutes=4000)).strftime(
        "%Y-%m-%dT%H:%M:%S.0000000Z"
    )


async def create_subscription(access_token: str, notification_url: str, client_state: str) -> dict:
    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": "/me/mailFolders('inbox')/messages",
        "expirationDateTime": _subscription_expiry(),
        "clientState": client_state,
    }
    return await graph_post(access_token, "/subscriptions", json=payload)


async def renew_subscription(access_token: str, subscription_id: str) -> dict:
    return await graph_patch(
        access_token,
        f"/subscriptions/{subscription_id}",
        json={"expirationDateTime": _subscription_expiry()},
    )


async def delete_subscription(access_token: str, subscription_id: str) -> None:
    """Best-effort delete of a Graph subscription. A 404 (already gone/expired) is fine."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{GRAPH_BASE}/subscriptions/{subscription_id}", headers=headers)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()
