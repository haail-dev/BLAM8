"""Customer photos, for the drafting model (Kris 2026-09-26: "OCR should be
included for the agent… a helpful feature for people that need evidence").

Photos attached to an email were already KEPT on the customer's record
(src/app/documents.py) — but only after the reply was written, so the model never
saw them, and on the full-auto test a photographed error code was answered "I
can see the appliance display in the image" with nothing seen. Now the photos are
fetched before drafting (or read back from the record for a redraft), shrunk
to what the model needs, and handed to it as images.

Everything here is best-effort: a photo that can't be fetched or opened is
skipped, never the draft.
"""
from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger(__name__)

MAX_PHOTOS = 4
MAX_SIDE = 1568            # the model's own sweet spot; bigger is wasted tokens
IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif")
_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")

try:  # iPhone photos (HEIC) open like any other once registered
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # noqa: BLE001 — no HEIC support: those photos are skipped
    pass


def is_photo(name: str | None, content_type: str | None) -> bool:
    ct = (content_type or "").lower().split(";")[0].strip()
    return ct in IMAGE_TYPES or (name or "").lower().endswith(_EXT)


def for_model(data: bytes, name: str | None = None) -> dict | None:
    """Bytes → {name, media_type, data(base64)} as a JPEG no bigger than
    MAX_SIDE, or None when it isn't an image we can open."""
    try:
        from PIL import ImageOps
        from app import safe_parse
        im = safe_parse.open_image(data)           # pixel cap before any decode (H4)
        if im is None:
            return None
        im = ImageOps.exif_transpose(im)           # a phone photo taken sideways reads upright
        im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return {"name": name or "photo", "media_type": "image/jpeg",
                "data": base64.b64encode(out.getvalue()).decode("ascii")}
    except Exception:  # noqa: BLE001
        log.info("photo not readable (%s)", name, exc_info=True)
        return None


async def fetch_for_draft(tenant_id: str, message_id: str, attachments: list | None,
                          mailbox_id: str | None = None) -> list[dict]:
    """The photos on an inbound email, fetched from the mailbox before the
    reply is drafted — the same keep-rules as the record (no logos, no tiny
    inline images)."""
    from app import documents, mailbox
    out: list[dict] = []
    for att in attachments or []:
        name = (att.get("name") or "").strip()
        if len(out) >= MAX_PHOTOS:
            break
        if not is_photo(name, att.get("content_type")):
            continue
        if not documents.worth_keeping(name, att.get("content_type"), att.get("size"),
                                       inline=bool(att.get("inline"))):
            continue
        try:
            data = await mailbox.fetch_attachment(tenant_id, message_id, att, mailbox_id=mailbox_id)
        except Exception:  # noqa: BLE001
            log.warning("photo fetch failed (%s / %s)", message_id, name, exc_info=True)
            continue
        im = for_model(data, name)
        if im:
            out.append(im)
    return out


def stored_for_log(tenant_id: str, email_log_id: str, contact_email: str | None) -> list[dict]:
    """The photos kept from one email (a redraft reads them back from the record)."""
    from app import db, documents
    out: list[dict] = []
    try:
        docs = [d for d in db.list_documents(tenant_id, contact_email=contact_email)
                if str(d.get("email_log_id") or "") == str(email_log_id)
                and is_photo(d.get("filename"), d.get("content_type"))]
    except Exception:  # noqa: BLE001
        return out
    for d in docs[:MAX_PHOTOS]:
        data = documents.download(d.get("url_or_path"))
        im = for_model(data, d.get("filename")) if data else None
        if im:
            out.append(im)
    return out


WEB_SIDE = 2048            # big enough to zoom into a fault, small enough to email
_SHRINK_OVER = 1_500_000   # bytes — a phone camera's 4–8 MB original


def web_ready(data: bytes, name: str | None, content_type: str | None) -> tuple[str, bytes, str]:
    """A photo made fit to email and to show in a browser (photos both ways,
    2026-09-26): an iPhone HEIC (which Chrome and most mail clients can't show)
    or a camera original over 1.5 MB becomes a JPEG no bigger than WEB_SIDE.
    Anything else — a PDF, a small JPEG, an image we can't open — comes back
    untouched. Returns (filename, bytes, content_type)."""
    name = name or "photo"
    ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    heic = ct in ("image/heic", "image/heif") or name.lower().endswith((".heic", ".heif"))
    if not is_photo(name, ct) or not (heic or len(data or b"") > _SHRINK_OVER):
        return name, data, ct
    try:
        from PIL import ImageOps
        from app import safe_parse
        opened = safe_parse.open_image(data)       # pixel cap before any decode (H4)
        if opened is None:
            return name, data, ct
        im = ImageOps.exif_transpose(opened).convert("RGB")
        im.thumbnail((WEB_SIDE, WEB_SIDE))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        stem = name.rsplit(".", 1)[0] if "." in name else name
        return stem + ".jpg", out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        log.info("photo not shrinkable (%s)", name, exc_info=True)
        return name, data, ct
