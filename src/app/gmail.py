"""Google (Gmail) OAuth + Gmail API — the second mailbox provider.

Mirrors src/app/graph.py for Microsoft: OAuth (authorize / exchange / refresh) plus
the read & send calls the agent needs, talking to the Gmail REST API directly via
httpx. Tokens are stored in email_connections exactly like the Microsoft ones; the
tenant's `provider` column selects which backend src/app/mailbox.py dispatches to. The
read/send functions return the SAME shapes as the Microsoft path, so the pipeline
and dashboard are provider-agnostic.
"""
import asyncio
import base64
import html
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from urllib.parse import urlencode

import httpx

from app.config import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# gmail.readonly → read the inbox + getProfile (the address); gmail.send → send replies;
# calendar.events (Calendar Connect v1.1) → push confirmed bookings + read busy events.
# include_granted_scopes=true on the authorize URL means a reconnect MERGES this
# with the existing grant (Google incremental consent) rather than replacing it.
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/gmail.send "
          "https://www.googleapis.com/auth/calendar.events")

# What ONE PERSON's own calendar connect asks for (People slice 5): events, and
# enough of an identity to say which Google account they picked. No mail scope
# at all — so the token cannot read their inbox even by accident, and the grant
# is SENSITIVE rather than restricted, which is why it never spends one of the
# 100 pre-CASA slots src/app/slots.py counts.
CALENDAR_SCOPES = ("openid email "
                   "https://www.googleapis.com/auth/calendar.events")

CAL_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary"

_LABELS = {"inbox": "INBOX", "junkemail": "SPAM", "sentitems": "SENT"}


# ── OAuth ────────────────────────────────────────────────────────────────────
def build_authorize_url(state: str, scope: str | None = None,
                        incremental: bool = True) -> str:
    """`scope` defaults to the mailbox grant. `incremental=False` asks Google
    NOT to merge scopes already granted to this account — the calendar-only
    connect uses it so an owner who connected the business mailbox doesn't
    quietly get a mail-capable token back for their personal calendar."""
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": scope or SCOPES,
        "state": state,
        "access_type": "offline",          # issue a refresh token for the background agent
        "prompt": "consent",                # force refresh-token issuance, incl. on reconnect
    }
    if incremental:
        params["include_granted_scopes"] = "true"
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.google_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()


async def refresh_tokens(refresh_token: str) -> dict:
    """Google reuses the same refresh token, so the response usually omits it —
    callers keep the stored one."""
    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()


def expiry_from_now(expires_in: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)


# ── Calendar (Calendar Connect v1.1 — Google side) ──────────────────────────
def normalize_gcal_event(item: dict) -> dict | None:
    """Google Calendar event → the provider-agnostic shape the sync planner eats
    ({'id','subject','start','end','all_day'}), or None for events that shouldn't
    block the diary (cancelled, or marked Free/transparent). All-day events carry
    'date' (end exclusive) — pinned to diary time; timed events carry RFC3339
    'dateTime' with an offset."""
    from zoneinfo import ZoneInfo
    if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    all_day = "date" in start
    if all_day:
        tz = ZoneInfo("Europe/London")
        s = datetime.fromisoformat(start["date"]).replace(tzinfo=tz)
        e = datetime.fromisoformat(end.get("date") or start["date"]).replace(tzinfo=tz)
    else:
        if not start.get("dateTime") or not end.get("dateTime"):
            return None
        s = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
    return {"id": item["id"], "subject": item.get("summary") or "Busy",
            "all_day": all_day, "start": s, "end": e}


async def list_calendar_events(access_token, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Busy events in [start, end) from the primary calendar, normalised.
    singleEvents=true expands recurring series into instances."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"timeMin": start_dt.isoformat(), "timeMax": end_dt.isoformat(),
              "singleEvents": "true", "maxResults": "250", "orderBy": "startTime"}
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(6):
            r = await c.get(f"{CAL_BASE}/events", headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            for item in data.get("items", []):
                ev = normalize_gcal_event(item)
                if ev:
                    out.append(ev)
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    return out


async def create_calendar_event(
    access_token: str, subject: str, start_iso: str, end_iso: str,
    all_day: bool = False, body_text: str = "",
) -> str:
    """Create an event in the primary calendar; returns the event id. start/end
    are naive London wall-times ('2026-06-11T14:00:00'); all-day events use the
    date parts (end exclusive, as supplied)."""
    payload: dict = {"summary": subject, "description": body_text}
    if all_day:
        payload["start"] = {"date": start_iso[:10]}
        payload["end"] = {"date": end_iso[:10]}
    else:
        payload["start"] = {"dateTime": start_iso, "timeZone": "Europe/London"}
        payload["end"] = {"dateTime": end_iso, "timeZone": "Europe/London"}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{CAL_BASE}/events", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["id"]


async def delete_calendar_event(access_token: str, event_id: str) -> None:
    """Remove an event BLAM8 created (booking cancelled). 404/410 = already gone."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{CAL_BASE}/events/{event_id}", headers=headers)
        if r.status_code not in (204, 404, 410):
            r.raise_for_status()


async def _get(access_token: str, path: str, params: dict | None = None,
               client: httpx.AsyncClient | None = None) -> dict:
    """GET a Gmail API path. Pass `client` to reuse one connection across a batch
    of concurrent calls (see list_messages); otherwise a one-shot client is used."""
    if client is None:
        async with httpx.AsyncClient(timeout=30) as c:
            return await _get(access_token, path, params, client=c)
    r = await client.get(
        f"{API_BASE}{path}", headers={"Authorization": f"Bearer {access_token}"}, params=params
    )
    r.raise_for_status()
    return r.json()


async def get_profile_email(access_token: str) -> str | None:
    return (await _get(access_token, "/profile")).get("emailAddress")


# ── parsing helpers ──────────────────────────────────────────────────────────
def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload", {}).get("headers") or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _addr(value: str) -> dict:
    name, email = parseaddr(value or "")
    return {"address": email or None, "name": name or None}


def _rule_headers(msg: dict) -> dict:
    """The headers the inbox rules read (src/app/inbox_rules.py), lowercase keys."""
    from app import inbox_rules
    return inbox_rules.pick_headers(
        (h.get("name"), h.get("value")) for h in (msg.get("payload", {}).get("headers") or []))


def _received_iso(msg: dict) -> str | None:
    date = _header(msg, "Date")
    if date:
        try:
            return parsedate_to_datetime(date).astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    ms = msg.get("internalDate")
    if ms:
        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
        except Exception:
            pass
    return None


def _b64url(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    """Decode HTML entities, drop non-breaking spaces, and collapse runaway
    whitespace — applied to both plain-text and stripped-HTML bodies."""
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?:[ \t]*\n){3,}", "\n\n", text)   # 3+ blank lines -> one
    return text.strip()


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)            # keep line breaks
    raw = re.sub(r"(?i)</p\s*>", "\n\n", raw)            # keep paragraph breaks
    return _clean_text(re.sub(r"(?s)<[^>]+>", " ", raw))


def _collect_bodies(payload: dict, acc: dict) -> None:
    """Recursively gather every text/plain and (stripped) text/html body."""
    data = (payload.get("body") or {}).get("data")
    if data:
        if payload.get("mimeType") in ("text/plain", "message/delivery-status", "message/feedback-report"):
            acc["plain"].append(_clean_text(_b64url(data)))
        elif payload.get("mimeType") == "text/html":
            acc["html"].append(_strip_html(_b64url(data)))
    for part in payload.get("parts") or []:
        _collect_bodies(part, acc)


def _attachment_names(payload: dict) -> list[str]:
    """Filenames of the real attachments in the MIME tree — the pipeline can't
    read the files (bytes are never fetched), but the DRAFTER must know they
    came: a document-type booking requirement is satisfied by an attachment
    (docs: intake `doc` fields, 2026-09-02), and 'I've attached the form'
    answered with 'please attach the form' reads as not listening."""
    names: list[str] = []

    def walk(p: dict) -> None:
        name = (p.get("filename") or "").strip()
        if name:
            names.append(name)
        for part in p.get("parts") or []:
            walk(part)

    walk(payload or {})
    return names[:10]


def _attachment_meta(payload: dict) -> list[dict]:
    """The real attachments with what a fetch needs: {name, content_type,
    size, attachment_id, inline}. Documents phase 1 (2026-09-04): the bytes
    are fetched per attachment through get_attachment and kept on the
    customer's record — the drafter still only sees the names."""
    out: list[dict] = []

    def walk(p: dict) -> None:
        name = (p.get("filename") or "").strip()
        body = p.get("body") or {}
        if name and body.get("attachmentId"):
            headers = {(h.get("name") or "").lower(): h.get("value") or ""
                       for h in (p.get("headers") or [])}
            disp = headers.get("content-disposition", "")
            out.append({"name": name, "content_type": p.get("mimeType") or "application/octet-stream",
                        "size": int(body.get("size") or 0), "attachment_id": body["attachmentId"],
                        # A Content-ID alone is not inline: Gmail's compose stamps one
                        # on real attachments too (live 2026-09-04, a 50 KB certificate).
                        "inline": disp.lower().startswith("inline")})
        for part in p.get("parts") or []:
            walk(part)

    walk(payload or {})
    return out[:10]


async def get_attachment(access_token: str, message_id: str, attachment_id: str) -> bytes:
    """One attachment's bytes (Gmail returns base64url in `data`)."""
    res = await _get(access_token, f"/messages/{message_id}/attachments/{attachment_id}")
    data = res.get("data") or ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)) if data else b""


def _body_with_attachments(payload: dict) -> str:
    body = _extract_body(payload).strip()
    names = _attachment_names(payload)
    if names:
        body += "\n\n[Attachments: " + ", ".join(names) + "]"
    return body


def _extract_body(payload: dict) -> str:
    """Best readable body from the MIME tree. Many HTML emails ship a junk
    plain-text alternative (a stray word + a wall of &nbsp;), so prefer the plain
    part only when it's substantive — otherwise use the (richer) HTML, which is
    what Gmail itself renders."""
    acc: dict[str, list[str]] = {"plain": [], "html": []}
    _collect_bodies(payload, acc)
    plain = max(acc["plain"], key=len, default="")
    html_text = max(acc["html"], key=len, default="")
    if len(plain) >= 40 and len(plain) >= 0.5 * len(html_text):
        return plain
    return html_text or plain


# ── shaped read/send (match the Microsoft mailbox shapes) ────────────────────
async def list_messages(access_token: str, folder: str = "inbox", top: int = 25) -> list[dict]:
    """List a folder. Gmail has no bulk-metadata endpoint, so each message's
    headers are fetched individually — but CONCURRENTLY over one shared
    connection, not one-after-another (the sequential version made the inbox take
    seconds to load). gather() preserves order, so newest-first is kept."""
    label = _LABELS.get(folder, "INBOX")
    async with httpx.AsyncClient(timeout=30) as c:
        data = await _get(access_token, "/messages", {"labelIds": label, "maxResults": str(top)}, client=c)
        refs = data.get("messages") or []

        async def _meta(ref: dict) -> dict | None:
            try:
                return await _get(
                    access_token, f"/messages/{ref['id']}",
                    {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date", "To"]},
                    client=c,
                )
            except httpx.HTTPStatusError:
                return None

        msgs = await asyncio.gather(*(_meta(r) for r in refs))

    out: list[dict] = []
    for m in msgs:
        if not m:
            continue
        out.append({
            "id": m.get("id"),
            "from": _addr(_header(m, "From")),
            "to": _addr(_header(m, "To")),
            "subject": _header(m, "Subject") or "(no subject)",
            "received": _received_iso(m),
            "preview": m.get("snippet") or "",
            "is_read": "UNREAD" not in (m.get("labelIds") or []),
        })
    return out


async def list_sent_messages(access_token: str, top: int = 300) -> list[dict]:
    """Up to `top` most-recent SENT messages with full plain-text bodies —
    same shape as graph.list_sent_messages. Gmail has no bulk-body endpoint, so
    bodies are fetched individually in bounded concurrent chunks (a 300-wide
    gather would trip per-user rate limits)."""
    async with httpx.AsyncClient(timeout=60) as c:
        data = await _get(access_token, "/messages",
                          {"labelIds": "SENT", "maxResults": str(min(top, 500))}, client=c)
        refs = (data.get("messages") or [])[:top]

        async def _full(ref: dict) -> dict | None:
            try:
                return await _get(access_token, f"/messages/{ref['id']}",
                                  {"format": "full"}, client=c)
            except httpx.HTTPStatusError:
                return None

        msgs: list = []
        for i in range(0, len(refs), 20):
            msgs.extend(await asyncio.gather(*(_full(r) for r in refs[i:i + 20])))

    out: list[dict] = []
    for m in msgs:
        if not m:
            continue
        out.append({
            "subject": _header(m, "Subject") or "",
            "body": _extract_body(m.get("payload", {})).strip(),
            "from_address": _addr(_header(m, "From")).get("address"),
            "to_addresses": [a for _n, a in getaddresses([_header(m, "To")]) if a],
            "sent": _received_iso(m),
        })
    return out


async def list_recent_inbox_ids(access_token: str, days: int = 14, limit: int = 100,
                                before: datetime | None = None) -> list[str]:
    """Ids of INBOX messages from the last `days`, newest first, at most
    `limit` — the backlog a fresh connection brings in (src/app/backlog.py).
    Gmail's search syntax carries the window (`newer_than:14d`); one list call,
    no bodies."""
    query = (f"after:{int((before-timedelta(days=int(days))).timestamp())} before:{int(before.timestamp())}"
             if before else f"newer_than:{int(days)}d")
    data = await _get(access_token, "/messages",
                      {"labelIds": "INBOX", "q": query,
                       "maxResults": str(min(int(limit), 500))})
    return [m["id"] for m in (data.get("messages") or []) if m.get("id")]


async def get_message(access_token: str, message_id: str) -> dict:
    m = await _get(access_token, f"/messages/{message_id}", {"format": "full"})
    return {
        "id": m.get("id"),
        "thread_id": m.get("threadId"),
        "from": _addr(_header(m, "From")),
        "subject": _header(m, "Subject") or "(no subject)",
        "received": _received_iso(m),
        "body": _body_with_attachments(m.get("payload", {})),
        "attachments": _attachment_meta(m.get("payload", {})),
        "headers": _rule_headers(m),
    }


async def fetch_latest_email(access_token: str) -> dict | None:
    data = await _get(access_token, "/messages", {"labelIds": "INBOX", "maxResults": "1"})
    msgs = data.get("messages") or []
    if not msgs:
        return None
    m = await _get(access_token, f"/messages/{msgs[0]['id']}", {"format": "full"})
    sender = _addr(_header(m, "From"))
    return {
        "id": m.get("id"),
        "thread_id": m.get("threadId"),
        "from_address": sender.get("address"),
        "from_name": sender.get("name"),
        "subject": _header(m, "Subject") or "(no subject)",
        "body": _body_with_attachments(m.get("payload", {})),
        "received": _received_iso(m),
        "attachments": _attachment_meta(m.get("payload", {})),
        "headers": _rule_headers(m),
    }


async def send_mail(access_token: str, to: str, subject: str, body: str,
                    attachments: list | None = None, send_as: str | None = None,
                    cc: list[str] | None = None) -> None:
    """Send a plain-text email. From defaults to the authenticated user; `send_as`
    sets an explicit From, which Gmail honours ONLY when it is a verified
    send-as alias on the account (anything else silently falls back to the
    primary address — a safe failure mode). `attachments` =
    [(filename, bytes, mimetype)] — used to attach the invoice PDF. `cc` rides
    the raw MIME, so it delivers (docs/modules/module-job-parties.md)."""
    msg = EmailMessage()
    if send_as:
        msg["From"] = send_as
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, data, mimetype in (attachments or []):
        maintype, _, subtype = (mimetype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=filename)
    await _send_raw(access_token, msg)


async def _send_raw(access_token: str, msg: EmailMessage, thread_id: str | None = None) -> None:
    payload: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{API_BASE}/messages/send",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()


def _reply_subject(subject: str) -> str:
    """The original subject under a single 'Re:' prefix. Gmail only files a sent
    message into the requested thread when the subjects match (Re:-prefixed is
    fine), so this must come from the FETCHED original, not our stored copy.
    (Same logic as mailbox.reply_subject — duplicated because mailbox imports us.)"""
    s = (subject or "").strip()
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}" if s else "Re:"


def build_reply_mime(to: str, subject: str, body: str, in_reply_to: str, references: str,
                     attachments: list | None = None) -> EmailMessage:
    """Assemble the MIME for a threaded reply (pure — unit-testable). Per RFC 2822,
    References = the original's References + its Message-ID; both headers are what
    make the reply thread in the RECIPIENT's mail client too, not just in Gmail."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    for filename, data, mimetype in (attachments or []):
        maintype, _, subtype = (mimetype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=filename)
    return msg


async def send_reply(access_token: str, to: str, message_id: str, body: str,
                     send_as: str | None = None, attachments: list | None = None) -> None:
    """Send `body` as a real reply to an existing message: same Gmail thread
    (threadId + In-Reply-To/References + matching subject) so it threads correctly
    in both mailboxes — and the customer's next reply keeps the threadId the
    agent's conversation memory is scoped to. `send_as` as in send_mail (verified
    alias or silently ignored). Raises httpx.HTTPStatusError (404) when the
    original message no longer exists; callers fall back to a plain send."""
    m = await _get(
        access_token, f"/messages/{message_id}",
        {"format": "metadata", "metadataHeaders": ["Message-ID", "References", "Subject"]},
    )
    msg = build_reply_mime(
        to, _reply_subject(_header(m, "Subject")), body,
        _header(m, "Message-ID"), _header(m, "References"), attachments=attachments,
    )
    if send_as:
        msg["From"] = send_as
    await _send_raw(access_token, msg, thread_id=m.get("threadId"))


# ── history polling (Gmail auto-draft — see src/app/jobs/poll_gmail.py) ──────────
async def current_history_id(access_token: str) -> str | None:
    """The mailbox's current history watermark — 'now' for the poller's baseline."""
    hid = (await _get(access_token, "/profile")).get("historyId")
    return str(hid) if hid else None


async def watch_mailbox(access_token: str, topic: str) -> dict:
    """Arm Gmail push for this mailbox: new INBOX mail publishes to the Pub/Sub
    `topic` (projects/<project>/topics/<name>), whose push subscription POSTs to
    /webhook/gmail — the Gmail counterpart of graph.create_subscription. The
    watch expires after ~7 days; the renewal loop re-arms it (calling watch
    again just extends it — no stop needed). gmail.readonly is a sufficient
    scope, so mailboxes connected before push existed need no re-consent.
    Returns Gmail's response: {historyId, expiration (epoch ms as a string)}."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{API_BASE}/watch",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"},
        )
        r.raise_for_status()
        return r.json()


async def new_inbox_message_ids(access_token: str, start_history_id: str) -> tuple[list[str], str]:
    """Ids of messages added to INBOX since `start_history_id`, plus the new
    watermark to store. Follows pagination; de-dupes (one message can appear in
    several history records). Raises httpx.HTTPStatusError 404 when the watermark
    is too old (Gmail keeps roughly a week of history) — callers re-baseline."""
    ids: dict[str, None] = {}   # insertion-ordered set
    latest = str(start_history_id)
    page_token = None
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            params = {
                "startHistoryId": str(start_history_id),
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
                "maxResults": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            data = await _get(access_token, "/history", params, client=c)
            latest = str(data.get("historyId") or latest)
            for h in data.get("history") or []:
                for added in h.get("messagesAdded") or []:
                    mid = (added.get("message") or {}).get("id")
                    if mid:
                        ids[mid] = None
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return list(ids), latest
