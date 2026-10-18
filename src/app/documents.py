"""Documents on the CRM record (Kris 2026-09-04: "there's still no Documents
section in the CRM").

Every e2e run put a document on a booking as a FILENAME — the passport, the
vet's report, the lab certificate — because the mail readers only ever
recorded the attachment names. This module keeps the files: an inbound
email's attachments are fetched from the provider and stored in the PRIVATE
`documents` bucket, one `documents` row each, on the customer's record (by
email) and on the thread's row; the operator can upload one by hand, label it
as the checklist document it satisfies, and pin it to a dependant or a
booking. Files are served only through short-TTL signed URLs behind the
operator's login.

Best-effort throughout: a Storage outage or an oversized file never fails the
draft — the email still lands, the marker in the body still says what came.
"""
from __future__ import annotations

import logging
import re
import uuid

from app import db as _db
from app.db import db

log = logging.getLogger(__name__)

BUCKET = "documents"
MAX_BYTES = 10 * 1024 * 1024        # per file
MAX_PER_EMAIL = 6                   # attachments kept from one email
SIGNED_URL_TTL = 600                # seconds

# Inline images (signatures, tracking pixels) are not documents.
_SKIP_TYPES = ("image/gif",)
_SKIP_NAMES = re.compile(r"^(image\d{3}\.(png|jpg|jpeg|gif)|.*signature.*|.*logo.*)$", re.I)

_bucket_ready = False


class DocumentError(RuntimeError):
    """Storage unavailable — the caller degrades (the file is not kept)."""


def _ensure_bucket(client) -> None:
    global _bucket_ready
    if _bucket_ready:
        return
    try:
        client.storage.create_bucket(BUCKET, options={"public": False})
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "exist" not in msg and "duplicate" not in msg:
            try:
                client.storage.get_bucket(BUCKET)
            except Exception as exc2:  # noqa: BLE001
                raise DocumentError(f"documents bucket unavailable: {exc2}") from exc2
    _bucket_ready = True


def safe_name(filename: str) -> str:
    """A filename fit for an object key: the basename, odd characters folded
    to '-', capped."""
    name = (filename or "file").replace("\\", "/").split("/")[-1].strip() or "file"
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", name)
    return name[:120]


def object_path(tenant_id: str, contact_email: str | None, filename: str) -> str:
    """tenant/contact/uuid-name — the uuid keeps two 'certificate.pdf's apart."""
    who = re.sub(r"[^a-z0-9@._-]+", "-", (contact_email or "misc").lower())[:80]
    return f"{tenant_id}/{who}/{uuid.uuid4().hex[:12]}-{safe_name(filename)}"


INLINE_KEEP_BYTES = 30 * 1024        # an inline image this big is a document, not a signature


def worth_keeping(name: str, content_type: str | None, size: int | None, inline: bool = False) -> bool:
    """Skip inline signature art and tracking pixels, and anything over the cap.
    Inline only loses when it is small — a certificate pasted into the body
    is still the certificate (live 2026-09-04)."""
    if inline and (size or 0) < INLINE_KEEP_BYTES:
        return False
    if (content_type or "").lower() in _SKIP_TYPES:
        return False
    if _SKIP_NAMES.match(name or ""):
        return False
    if size is not None and (size <= 0 or size > MAX_BYTES):
        return False
    return True


def store(tenant_id: str, *, filename: str, data: bytes, content_type: str | None,
          source: str, contact_email: str | None = None, contact_id: str | None = None,
          email_log_id: str | None = None, booking_id: str | None = None,
          instance_id: str | None = None, label: str | None = None, client=None) -> dict | None:
    """Upload the bytes and write the row. Returns the row, or None when the
    file could not be kept (logged, never raised past here)."""
    if not data or len(data) > MAX_BYTES:
        return None
    c = client or db()
    try:
        _ensure_bucket(c)
        path = object_path(tenant_id, contact_email, filename)
        c.storage.from_(BUCKET).upload(
            path, data, {"content-type": content_type or "application/octet-stream", "upsert": "false"})
    except Exception:  # noqa: BLE001
        log.warning("document upload failed (tenant %s, %s)", tenant_id, filename, exc_info=True)
        return None
    row = _db.insert_document(
        tenant_id, filename=safe_name(filename), content_type=content_type or "application/octet-stream",
        size_bytes=len(data), storage_path=path, source=source,
        contact_email=(contact_email or "").strip().lower() or None, contact_id=contact_id,
        email_log_id=email_log_id, booking_id=booking_id, instance_id=instance_id,
        label=(label or "").strip() or None, client=client)
    return row


def download(path: str | None, *, client=None) -> bytes | None:
    """A stored file's bytes (service-role), or None — the drafting model reads
    kept photos back through this on a redraft (src/app/photos.py)."""
    if not path:
        return None
    try:
        c = client or db()
        return c.storage.from_(BUCKET).download(path)
    except Exception:  # noqa: BLE001
        log.warning("document download failed (%s)", path, exc_info=True)
        return None


def signed_url(path: str, ttl: int = SIGNED_URL_TTL, *, client=None) -> str | None:
    if not path:
        return None
    c = client or db()
    try:
        res = c.storage.from_(BUCKET).create_signed_url(path, ttl)
        if isinstance(res, dict):
            return res.get("signedURL") or res.get("signed_url") or res.get("signedUrl")
        return res
    except Exception:  # noqa: BLE001
        log.warning("document signed_url failed for %s", path, exc_info=True)
        return None


def remove_object(path: str, *, client=None) -> None:
    if not path:
        return
    c = client or db()
    try:
        c.storage.from_(BUCKET).remove([path])
    except Exception:  # noqa: BLE001
        log.warning("document remove failed for %s", path, exc_info=True)


def instance_for(instances: list[dict] | None, filename: str, hint: str = "") -> str | None:
    """Which dependant an incoming file belongs to, or None when it is not
    obvious (Kris 2026-09-21: Bella's vaccination card filed against Hannah,
    not against Bella, so neither the proof gate nor the confirm chip could
    see it).

    One dependant on the record is the answer. Otherwise the file has to NAME
    one — in its own filename or in the words around it — and name exactly one.
    Two dogs mentioned in one email is a guess, and a certificate filed against
    the wrong animal is worse than one filed against nobody.

    Pure, so the guessing is testable without a mailbox."""
    live = [i for i in (instances or []) if i.get("id") and not i.get("archived_at")]
    if not live:
        return None
    if len(live) == 1:
        return live[0]["id"]
    hay = f"{filename or ''} {hint or ''}".lower()
    hits = {i["id"] for i in live
            if (i.get("name") or "").strip()
            and re.search(r"\b" + re.escape(str(i["name"]).strip().lower()) + r"\b", hay)}
    return hits.pop() if len(hits) == 1 else None


MAX_TEXT_PAGES = 3
MAX_TEXT_CHARS = 6000


def document_text(name: str, content_type: str | None, data: bytes) -> str:
    """The text layer of a kept PDF — what a vet card, certificate or MOT
    actually says — for `dependants_extract.read_documents`. Empty for
    anything else, a scanned PDF with no text layer, or a file that will not
    parse: a photographed card needs OCR, which is out of scope. Never raises."""
    is_pdf = (content_type or "").lower() == "application/pdf" or name.lower().endswith(".pdf")
    if not is_pdf or not data:
        return ""
    try:
        # In a throwaway child with a timeout + memory cap (H5): a crafted PDF
        # costs that child, never the shared event loop.
        from app import safe_parse
        return safe_parse.pdf_text(data, max_pages=MAX_TEXT_PAGES, max_chars=MAX_TEXT_CHARS)
    except Exception:  # noqa: BLE001
        log.info("no text read from %s", name, exc_info=True)
        return ""


async def save_email_attachments(tenant_id: str, *, mailbox_id: str | None, message_id: str,
                                 email_log_id: str | None, from_address: str | None,
                                 attachments: list[dict] | None,
                                 text_hint: str | None = None) -> list[dict]:
    """Keep an inbound email's attachments on the sender's record. Fetches
    the bytes per attachment through the mailbox (Gmail or Graph), stores
    each worth keeping, links the row to the thread row. Best-effort: any
    failure is logged and the rest continue."""
    from app import mailbox, trace
    kept: list[dict] = []

    def _t(outcome: str, name: str, **extra) -> None:
        try:
            trace.emit(tenant_id, "document_keep", outcome=outcome, email_log_id=email_log_id,
                       contact=from_address, detail={"message_id": message_id, "name": name, **extra})
        except Exception:  # noqa: BLE001
            pass

    for att in (attachments or [])[:MAX_PER_EMAIL]:
        name = (att.get("name") or "").strip()
        if not name or not worth_keeping(name, att.get("content_type"), att.get("size"),
                                         inline=bool(att.get("inline"))):
            _t("skipped", name, content_type=att.get("content_type"), size=att.get("size"),
               inline=bool(att.get("inline")))
            continue
        try:
            data = await mailbox.fetch_attachment(tenant_id, message_id, att, mailbox_id=mailbox_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("attachment fetch failed (%s / %s)", message_id, name, exc_info=True)
            _t("fetch_failed", name, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            continue
        if not data:
            _t("empty_fetch", name)
            continue
        contact = None
        try:
            contact = _db.crm_contact_by_email(tenant_id, from_address) if from_address else None
        except Exception:  # noqa: BLE001
            contact = None
        # The file belongs to the DEPENDANT where we can tell which one — the
        # proof gate and the confirm chip both key on it. Best-effort: an
        # unlinked file is still kept on the contact, exactly as before.
        instance_id = None
        try:
            if (contact or {}).get("id"):
                instance_id = instance_for(
                    _db.list_requirement_instances(tenant_id, contact["id"]), name, text_hint or "")
        except Exception:  # noqa: BLE001
            instance_id = None
        row = store(tenant_id, filename=name, data=data, content_type=att.get("content_type"),
                    source="email", contact_email=from_address,
                    contact_id=(contact or {}).get("id"), email_log_id=email_log_id,
                    instance_id=instance_id)
        if row:
            # What the file SAYS, for the reader that runs next (not stored):
            # a vet card's new expiry lives inside it (slice 4, 2026-09-22).
            import asyncio as _asyncio
            text = await _asyncio.to_thread(document_text, name, att.get("content_type"), data)
            kept.append({**row, "text": text} if text else row)
            _t("kept", name, size=len(data), document_id=row.get("id"), read=bool(text))
        else:
            _t("store_failed", name, size=len(data))
    if kept:
        try:
            _db.log_agent_event(
                tenant_id, "document",
                f"Kept {len(kept)} document{'s' if len(kept) != 1 else ''} from this email: "
                + ", ".join(r.get("filename") or "" for r in kept),
                contact=from_address, email_log_id=email_log_id)
        except Exception:  # noqa: BLE001
            pass
    return kept


def with_updates(documents: list[dict], chips: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pure: each document with what it asks to change on the record (Kris
    2026-09-22: a new card should sit in Documents "asking for a confirmation
    to use it to update info"). A chip belongs to a document when the same
    email brought both and — when the document is linked to a dependant — it is
    about that dependant. `replaces` is the earlier document of the same kind
    for the same dependant: this one is its new version. Returns the documents
    and the chips no document claimed (they stay where they were shown before)."""
    claimed: set = set()
    out = []
    for i, d in enumerate(documents or []):
        d = dict(d)
        mine = [c for c in chips or []
                if c.get("kind") == "field" and d.get("email_log_id")
                and c.get("email_log_id") == d.get("email_log_id")
                and (not d.get("instance_id") or c.get("record_id") == d.get("instance_id"))]
        claimed.update(c.get("id") for c in mine)
        d["updates"] = mine
        label = (d.get("label") or "").strip().lower()
        d["replaces"] = next(
            (o for o in (documents or [])[i + 1:]           # newest first: later = older
             if label and (o.get("label") or "").strip().lower() == label
             and o.get("instance_id") == d.get("instance_id")), None)
        out.append(d)
    return out, [c for c in chips or [] if c.get("id") not in claimed]


def pretty_size(n: int | None) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def context_lines(documents: list[dict]) -> str:
    """The drafter's line: what is already on file for this customer, so a
    document-type requirement is never asked for twice."""
    if not documents:
        return ""
    parts = []
    for d in documents[:12]:
        label = (d.get("label") or "").strip()
        parts.append(f"{d.get('filename')}" + (f" ({label})" if label else ""))
    return "Documents already on file for this customer (never ask for one of these again): " + "; ".join(parts)
