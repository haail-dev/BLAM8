"""Mailbox access — provider-aware (Microsoft Graph or Gmail).

A mailbox is an email_connections row (enterprise D1); its `provider` column
selects the backend. These functions present a single shape to the pipeline and
dashboard regardless of provider: keep a live access token (auto-refresh via
the stored refresh token) and read/send messages. Every function takes an
optional `mailbox_id` — the specific connection to use; None means the tenant's
DEFAULT (oldest) mailbox, which is the unchanged SaaS single-mailbox path.
Microsoft logic is unchanged; Gmail dispatches to app.gmail, which returns the
identical shapes.
"""
import base64
import logging
from datetime import datetime, timezone

import httpx

from app import db, gmail, graph

log = logging.getLogger(__name__)


class MailNotSentBeforeRequest(RuntimeError):
    """Mailbox access failed before a message-send request was made."""


def _provider(tenant_id: str) -> str:
    """Legacy fallback only — the tenants.provider mirror, for a connection row
    that predates per-connection providers."""
    return (db.get_tenant(tenant_id) or {}).get("provider") or "microsoft"


def connection_provider(conn: dict, tenant_id: str | None = None) -> str:
    """The provider for one connection row (enterprise: per-mailbox)."""
    return conn.get("provider") or (_provider(tenant_id) if tenant_id else "microsoft")


async def valid_access_token(conn: dict, provider: str = "microsoft") -> str:
    """Return a live access token for a connection, refreshing via the stored
    refresh token if the current one has expired. Works for both providers."""
    expires = datetime.fromisoformat(conn["token_expires"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < expires:
        return conn["access_token"]

    backend = gmail if provider == "google" else graph
    tokens = await backend.refresh_tokens(conn["refresh_token"])
    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", conn["refresh_token"])  # google reuses the old one
    new_expires = backend.expiry_from_now(tokens["expires_in"])
    db.update_connection_tokens(conn["id"], new_access, new_refresh, new_expires)
    return new_access


async def _access(
    tenant_id: str, mailbox_id: str | None = None
) -> tuple[str | None, str, dict | None]:
    """(live access token, provider, connection row) for one mailbox — the given
    connection, or the tenant's default when mailbox_id is None.
    (None, provider, None) if no mailbox."""
    conn = (
        db.get_connection_by_id(mailbox_id, tenant_id) if mailbox_id
        else db.get_email_connection(tenant_id)
    )
    if not conn:
        return None, _provider(tenant_id), None
    provider = connection_provider(conn, tenant_id)
    return await valid_access_token(conn, provider), provider, conn


def _send_as(conn: dict | None) -> str | None:
    """The mailbox's configured outbound identity (send_as_address), if any.
    Gmail applies it only when it is a verified send-as alias on the account;
    Microsoft requires SendAs rights — so this stays null unless the operator
    has set the alias up end-to-end (e.g. hello@blam8.com on the BLAM8 tenant)."""
    return ((conn or {}).get("send_as_address") or "").strip() or None


def _ms_addr(party: dict | None) -> dict:
    ea = (party or {}).get("emailAddress") or {}
    return {"address": ea.get("address"), "name": ea.get("name")}


async def fetch_latest_email(tenant_id: str, mailbox_id: str | None = None) -> dict | None:
    """Read the most recent inbox message as plain text. None if no mailbox/empty."""
    access, provider, _conn = await _access(tenant_id, mailbox_id)
    if not access:
        return None
    if provider == "google":
        return await gmail.fetch_latest_email(access)
    data = await graph.graph_get(
        access, "/me/mailFolders/inbox/messages",  # inbox only — /me/messages includes our own sent mail
        params={"$top": "1", "$orderby": "receivedDateTime desc",
                "$select": "id,conversationId,subject,from,body,bodyPreview,receivedDateTime,hasAttachments,internetMessageHeaders"},
        extra_headers={"Prefer": 'outlook.body-content-type="text"'},
    )
    msgs = data.get("value", [])
    if not msgs:
        return None
    m = msgs[0]
    body = (m.get("body") or {}).get("content") or m.get("bodyPreview") or ""
    sender = _ms_addr(m.get("from"))
    attachments = await _graph_attachment_meta(access, m.get("id")) if m.get("hasAttachments") else []
    if attachments:
        body = body.strip() + "\n\n[Attachments: " + ", ".join(a["name"] for a in attachments) + "]"
    return {
        "id": m.get("id"),
        "thread_id": m.get("conversationId"),
        "from_address": sender.get("address"),
        "from_name": sender.get("name"),
        "subject": m.get("subject") or "(no subject)",
        "body": body.strip(),
        "received": m.get("receivedDateTime"),
        "attachments": attachments,
        "headers": _rule_headers(m),
    }


def _rule_headers(m: dict) -> dict:
    """The headers the inbox rules read (src/app/inbox_rules.py) from a Graph
    message's internetMessageHeaders, lowercase keys."""
    from app import inbox_rules
    return inbox_rules.pick_headers(m.get("internetMessageHeaders") or [])


async def _graph_attachment_meta(access: str, message_id: str) -> list[dict]:
    """Graph's attachment list as the reader's metadata shape (documents phase 1)."""
    out: list[dict] = []
    try:
        att = await graph.graph_get(access, f"/me/messages/{message_id}/attachments",
                                    params={"$select": "id,name,contentType,size,isInline"})
        for a in (att.get("value") or []):
            name = (a.get("name") or "").strip()
            if name:
                out.append({"name": name, "content_type": a.get("contentType") or "application/octet-stream",
                            "size": int(a.get("size") or 0), "attachment_id": a.get("id"),
                            "inline": bool(a.get("isInline"))})
    except Exception:
        log.warning("attachment lookup failed for %s", message_id, exc_info=True)
    return out[:10]


async def list_messages(
    tenant_id: str, folder: str = "inbox", top: int = 25, mailbox_id: str | None = None
) -> list[dict]:
    """List messages from a mailbox folder ('inbox' / 'junkemail')."""
    access, provider, _conn = await _access(tenant_id, mailbox_id)
    if not access:
        return []
    if provider == "google":
        return await gmail.list_messages(access, folder, top)
    try:
        data = await graph.graph_get(
            access, f"/me/mailFolders/{folder}/messages",
            params={
                "$top": str(top),
                "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead",
                "$orderby": "receivedDateTime desc",
            },
        )
    except httpx.HTTPStatusError:
        return []
    out = []
    for m in data.get("value", []):
        tos = m.get("toRecipients") or []
        out.append({
            "id": m.get("id"),
            "from": _ms_addr(m.get("from")),
            "to": _ms_addr(tos[0]) if tos else {"address": None, "name": None},
            "subject": m.get("subject") or "(no subject)",
            "received": m.get("receivedDateTime"),
            "preview": m.get("bodyPreview") or "",
            "is_read": m.get("isRead", True),
        })
    return out


async def get_message(tenant_id: str, message_id: str, mailbox_id: str | None = None) -> dict | None:
    access, provider, _conn = await _access(tenant_id, mailbox_id)
    if not access:
        return None
    if provider == "google":
        return await gmail.get_message(access, message_id)
    m = await graph.graph_get(
        access, f"/me/messages/{message_id}",
        params={"$select": "id,conversationId,subject,from,toRecipients,"
                           "receivedDateTime,body,hasAttachments,internetMessageHeaders"},
        extra_headers={"Prefer": 'outlook.body-content-type="text"'},
    )
    body = ((m.get("body") or {}).get("content") or "").strip()
    attachments: list[dict] = []
    if m.get("hasAttachments"):
        # Attachment names for the drafter (document-type booking requirements,
        # 2026-09-02) — the Gmail reader gets them from the MIME tree for free;
        # Graph needs one extra names-only call. Best-effort: a failed lookup
        # still marks that files came. The same call carries what a later
        # fetch of the bytes needs (documents phase 1, 2026-09-04).
        names: list[str] = []
        try:
            att = await graph.graph_get(access, f"/me/messages/{message_id}/attachments",
                                        params={"$select": "id,name,contentType,size,isInline"})
            for a in (att.get("value") or []):
                name = (a.get("name") or "").strip()
                if not name:
                    continue
                names.append(name)
                attachments.append({"name": name, "content_type": a.get("contentType") or "application/octet-stream",
                                    "size": int(a.get("size") or 0), "attachment_id": a.get("id"),
                                    "inline": bool(a.get("isInline"))})
            names = names[:10]
        except Exception:
            log.warning("attachment name lookup failed for %s", message_id, exc_info=True)
        body += "\n\n[Attachments: " + (", ".join(names) if names else "yes — files came with this email") + "]"
    return {
        "id": m.get("id"),
        "thread_id": m.get("conversationId"),
        "from": _ms_addr(m.get("from")),
        "subject": m.get("subject") or "(no subject)",
        "received": m.get("receivedDateTime"),
        "body": body,
        "attachments": attachments[:10],
        "headers": _rule_headers(m),
    }


async def fetch_attachment(tenant_id: str, message_id: str, att: dict,
                           mailbox_id: str | None = None) -> bytes:
    """One attachment's bytes from whichever provider holds the mailbox —
    documents phase 1. `att` is an entry from get_message()['attachments']."""
    access, provider, _conn = await _access(tenant_id, mailbox_id)
    if not access or not att.get("attachment_id"):
        return b""
    if provider == "google":
        return await gmail.get_attachment(access, message_id, att["attachment_id"])
    a = await graph.graph_get(access, f"/me/messages/{message_id}/attachments/{att['attachment_id']}")
    content = a.get("contentBytes") or ""
    return base64.b64decode(content) if content else b""


def reply_subject(subject: str | None) -> str:
    """The subject to reply under: the original with a single 'Re:' prefix."""
    s = (subject or "").strip()
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}" if s else "Re:"


async def send_reply(
    tenant_id: str, to: str, subject: str | None, body: str,
    source_message_id: str | None = None, mailbox_id: str | None = None,
    attachments: list | None = None,
) -> None:
    """Send a reply. Shared by the human-approve path (dashboard) and the
    full-auto-send path (pipeline) so both behave identically.

    `mailbox_id` is the connection that RECEIVED the inbound (enterprise D5) —
    the reply must leave from the same address, in that mailbox's provider
    thread; None falls back to the tenant's default mailbox (SaaS).

    When we know which provider message is being answered (`source_message_id`),
    reply PROVIDER-NATIVELY — Graph's /reply action, or a Gmail send carrying
    threadId + In-Reply-To/References. That keeps our reply inside the original
    conversation in both mailboxes, so the customer's next message arrives with
    the SAME provider thread id and the agent's thread-scoped memory (Phase 5)
    still finds the history. Without it — or if the original message has since
    been deleted (404) — fall back to a fresh send under a 'Re:' subject."""
    if source_message_id:
        access, provider, conn = await _access(tenant_id, mailbox_id)
        if not access:
            raise RuntimeError("No mailbox connected for this tenant")
        send_as = _send_as(conn)
        try:
            if provider == "google":
                await gmail.send_reply(access, to, source_message_id, body, send_as=send_as,
                                       **({"attachments": attachments} if attachments else {}))
            else:
                # Graph /reply: addresses the original sender and keeps the
                # conversationId + threading headers; body replaces the default.
                message: dict = {"body": {"contentType": "Text", "content": body}}
                if send_as:
                    message["from"] = {"emailAddress": {"address": send_as}}
                if attachments:
                    # Files the owner picked to go with the reply (photos both
                    # ways, 2026-09-26) — inline fileAttachments, same as send_mail.
                    message["attachments"] = _graph_files(attachments)
                await graph.graph_post(
                    access, f"/me/messages/{source_message_id}/reply",
                    json={"message": message},
                )
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            # original message gone (deleted / moved) — plain send below
    await send_mail(tenant_id, to, reply_subject(subject), body, mailbox_id=mailbox_id,
                    **({"attachments": attachments} if attachments else {}))


def _graph_files(attachments: list) -> list[dict]:
    return [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": filename,
         "contentType": mimetype, "contentBytes": base64.b64encode(data).decode()}
        for filename, data, mimetype in attachments
    ]


async def send_mail(
    tenant_id: str, to: str, subject: str, body: str, mailbox_id: str | None = None,
    attachments: list | None = None, cc: list[str] | None = None,
    outreach_headers: dict | None = None, expected_sender: str | None = None,
) -> None:
    """Send a plain-text email from the connected mailbox. The From address is
    the mailbox itself unless its send_as_address is set (see _send_as).
    `attachments` = [(filename, bytes, mimetype)] — e.g. the invoice PDF
    (docs/modules/module-invoicing.md). `cc` = 'kept informed' job parties
    (docs/modules/module-job-parties.md) — one send, one thread, everyone reading the
    same document."""
    try:
        access, provider, conn = await _access(tenant_id, mailbox_id)
    except Exception as exc:
        raise MailNotSentBeforeRequest(str(exc)) from exc
    if not access:
        raise MailNotSentBeforeRequest("No mailbox connected for this tenant")
    send_as = _send_as(conn)
    if expected_sender and (send_as or (conn or {}).get("address") or "").strip().lower() != expected_sender:
        raise MailNotSentBeforeRequest("Sending identity changed; review the outreach draft")
    if outreach_headers:
        # MIME preserves the standard List-Unsubscribe header on both providers.
        # Graph JSON custom headers only support x-*; its MIME sendMail endpoint
        # accepts standard message headers (Microsoft Graph v1.0 sendMail docs).
        from email.message import EmailMessage
        from email.policy import SMTP
        if set(outreach_headers) != {"Reply-To", "List-Unsubscribe"}:
            raise MailNotSentBeforeRequest("Invalid outreach headers")
        msg = EmailMessage(policy=SMTP)
        msg["From"] = send_as or conn["address"]
        msg["To"] = to
        msg["Subject"] = subject
        for key, value in outreach_headers.items():
            msg[key] = value
        msg.set_content(body)
        if provider == "google":
            return await gmail._send_raw(access, msg)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(graph.GRAPH_BASE + "/me/sendMail",
                headers={"Authorization": f"Bearer {access}", "Content-Type": "text/plain"},
                content=base64.b64encode(msg.as_bytes()))
            r.raise_for_status()
        return
    if provider == "google":
        return await gmail.send_mail(access, to, subject, body, attachments=attachments,
                                     send_as=send_as, cc=cc)
    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to}}],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    if send_as:
        message["from"] = {"emailAddress": {"address": send_as}}
    if attachments:
        message["attachments"] = _graph_files(attachments)
    await graph.graph_post(access, "/me/sendMail",
                           json={"message": message, "saveToSentItems": True})
