"""Supabase Storage helpers: condition photos + portal branding.

Condition photos (docs/os/assets.md §11):

The ONE piece of new infra in Assets OS Phase C: a PRIVATE bucket `condition-photos`
holding the handover (out) and return (in) photos the diff pipeline compares. Photos
are NEVER public — they are served only via short-TTL signed URLs on the tenant-user
evidence page, and downloaded (service-role) for the vision call.

GRACEFUL by design (§11): if Storage is unavailable or unconfigured, upload_photo
raises StorageError and the capture route records the report with a `storage_error`
flag anyway — the caretaker never loses their submission to a bucket outage. Signed
URLs and downloads degrade the same way (None / raise), so the evidence page shows
"photo storage unavailable" rather than 500ing.

Portal branding (2026-08-02): the banner a business uploads for its public
portal page /c/{slug} lives in the PUBLIC `portal-branding` bucket — public by
design, since the banner IS the public page's artwork; a plain public URL
(CDN-cached by Supabase) beats proxying every portal hit through the app. Same
graceful degradation: uploads raise StorageError for the settings page to show
a message, and a missing/unreadable banner renders as no banner, never a 500.

PROD NOTE: this needs Supabase Storage enabled on the project + (idempotently, on
first use) the buckets. That is a Julius infra item — the code creates buckets
best-effort but a project without Storage enabled will surface storage_error
cleanly.
"""
from __future__ import annotations

import logging
import secrets

from app.db import db

log = logging.getLogger(__name__)

BUCKET = "condition-photos"

# The evidence page shows photos via signed URLs; keep the TTL short (the page is
# behind auth and re-signs on each render, so a leaked URL expires quickly).
SIGNED_URL_TTL = 600  # seconds (10 min)

_bucket_ready = False


class StorageError(RuntimeError):
    """Storage is unavailable/unconfigured — the caller degrades gracefully (records
    the report with storage_error, shows "photo storage unavailable")."""


def _ensure_bucket(client) -> None:
    """Create the PRIVATE bucket on first use, idempotently. A project without Storage
    enabled raises here → StorageError up the stack (never a silent public bucket)."""
    global _bucket_ready
    if _bucket_ready:
        return
    try:
        client.storage.create_bucket(BUCKET, options={"public": False})
    except Exception as exc:  # already-exists is the happy path; anything else we tolerate
        msg = str(exc).lower()
        if "exist" not in msg and "duplicate" not in msg:
            # Bucket genuinely couldn't be ensured (Storage off / creds). Try once to
            # list it — if that works the bucket is there and we proceed; else surface.
            try:
                client.storage.get_bucket(BUCKET)
            except Exception as exc2:  # noqa: BLE001
                raise StorageError(f"condition-photos bucket unavailable: {exc2}") from exc2
    _bucket_ready = True


def _path(tenant_id: str, booking_id: str, phase: str, idx: int, content_type: str) -> str:
    """A stable per-photo object key: tenant/booking/phase/ordinal.ext. The ordinal is
    the pairing key the diff pipeline uses (out[i] ↔ in[i])."""
    ext = "jpg"
    if content_type and "/" in content_type:
        ext = content_type.split("/", 1)[1].split(";")[0].strip() or "jpg"
        ext = {"jpeg": "jpg"}.get(ext, ext)
    return f"{tenant_id}/{booking_id}/{phase}/{int(idx)}.{ext}"


def upload_photo(tenant_id: str, booking_id: str, phase: str, idx: int,
                 data: bytes, content_type: str = "image/jpeg", *, client=None) -> str:
    """Upload one condition photo → the stored PATH (persisted in
    condition_reports.photo_urls). Service-role (the bucket is private). Raises
    StorageError on any Storage failure so the route can record storage_error and
    keep the report."""
    c = client or db()
    try:
        _ensure_bucket(c)
        path = _path(tenant_id, booking_id, phase, idx, content_type)
        c.storage.from_(BUCKET).upload(
            path, data,
            {"content-type": content_type or "image/jpeg", "upsert": "true"})
        return path
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("condition photo upload failed (tenant %s, booking %s)",
                    tenant_id, booking_id, exc_info=True)
        raise StorageError(str(exc)) from exc


def signed_url(path: str, ttl: int = SIGNED_URL_TTL, *, client=None) -> str | None:
    """A short-TTL signed URL for display on the evidence page. Returns None on any
    failure (the page shows a "photo unavailable" placeholder rather than crashing)."""
    if not path:
        return None
    c = client or db()
    try:
        res = c.storage.from_(BUCKET).create_signed_url(path, ttl)
        if isinstance(res, dict):
            return res.get("signedURL") or res.get("signed_url") or res.get("signedUrl")
        return res
    except Exception:  # noqa: BLE001
        log.warning("signed_url failed for %s", path, exc_info=True)
        return None


def download_bytes(path: str, *, client=None) -> bytes:
    """Fetch a photo's bytes for the vision call (service-role). Raises StorageError on
    any failure so the diff pipeline degrades to a "needs manual review" case."""
    if not path:
        raise StorageError("no photo path")
    c = client or db()
    try:
        return c.storage.from_(BUCKET).download(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("condition photo download failed for %s", path, exc_info=True)
        raise StorageError(str(exc)) from exc


# ── Portal branding (banner on /c/{slug}) ────────────────────────────────────
BRANDING_BUCKET = "portal-branding"

# Banners are decoration, not documents — 4 MB is plenty for a header image and
# keeps the public page fast (kb.MAX_UPLOAD_BYTES is 5 MB for content files).
MAX_BANNER_BYTES = 4 * 1024 * 1024

_branding_ready = False

# Magic-byte signatures: the browser's declared content-type is untrusted, the
# first bytes decide. Anything else is rejected before Storage is touched.
_BANNER_SIGS = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)


def sniff_banner(data: bytes) -> tuple[str, str] | None:
    """(content_type, ext) from the file's magic bytes — None when it isn't an
    image format the banner accepts (png/jpeg/gif/webp)."""
    for sig, ctype, ext in _BANNER_SIGS:
        if data.startswith(sig):
            return ctype, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _ensure_branding_bucket(client) -> None:
    """Create the PUBLIC bucket on first use, idempotently (see module docstring
    for why public). Same tolerate-already-exists dance as the photos bucket."""
    global _branding_ready
    if _branding_ready:
        return
    try:
        client.storage.create_bucket(BRANDING_BUCKET, options={"public": True})
    except Exception as exc:  # already-exists is the happy path
        msg = str(exc).lower()
        if "exist" not in msg and "duplicate" not in msg:
            try:
                client.storage.get_bucket(BRANDING_BUCKET)
            except Exception as exc2:  # noqa: BLE001
                raise StorageError(f"portal-branding bucket unavailable: {exc2}") from exc2
    _branding_ready = True


def upload_banner(tenant_id: str, data: bytes, content_type: str, ext: str,
                  *, client=None) -> str:
    """Upload a portal banner → the stored PATH (tenant_config.portal_banner_path).
    The path carries a fresh token per upload so a replaced banner gets a new
    public URL — CDN/browser caches of the old one can't go stale-but-current.
    Raises StorageError so the settings/onboarding page shows a message."""
    c = client or db()
    try:
        _ensure_branding_bucket(c)
        path = f"{tenant_id}/banner-{secrets.token_hex(4)}.{ext}"
        c.storage.from_(BRANDING_BUCKET).upload(
            path, data,
            {"content-type": content_type, "upsert": "true",
             "cache-control": "86400"})
        return path
    except StorageError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("portal banner upload failed (tenant %s)", tenant_id, exc_info=True)
        raise StorageError(str(exc)) from exc


def delete_banner(path: str, *, client=None) -> None:
    """Best-effort removal of a replaced/removed banner object. Failure only
    leaves an orphaned file in the bucket — never surfaces to the tenant."""
    if not path:
        return
    try:
        (client or db()).storage.from_(BRANDING_BUCKET).remove([path])
    except Exception:  # noqa: BLE001
        log.warning("portal banner delete failed for %s", path, exc_info=True)


def banner_public_url(path: str | None, *, client=None) -> str | None:
    """The stable public URL for a stored banner path — None when there is no
    banner or the URL can't be composed (the page just renders without one)."""
    if not path:
        return None
    try:
        c = client or db()
        url = c.storage.from_(BRANDING_BUCKET).get_public_url(path)
        return url or None
    except Exception:  # noqa: BLE001
        log.warning("banner_public_url failed for %s", path, exc_info=True)
        return None


# ── Account closure: a business's files go with it (security review M15) ────
# Every bucket keys objects under "{tenant_id}/…" (documents, condition photos,
# the public portal banner). Closing an account used to delete the rows only —
# passports, certificates and photos outlived the business, and a closed
# business's banner stayed public. Called BEFORE the tenant cascade.
TENANT_BUCKETS = ("documents", BUCKET, BRANDING_BUCKET)


def _walk(bucket, prefix: str, depth: int = 0) -> list[str]:
    """Every object path under `prefix` (folders recursed, pages of 1000)."""
    out: list[str] = []
    if depth > 6:
        return out
    offset = 0
    while True:
        page = bucket.list(prefix, {"limit": 1000, "offset": offset}) or []
        for e in page:
            name = e.get("name")
            if not name:
                continue
            full = f"{prefix}/{name}"
            if e.get("id") is None:          # a folder
                out += _walk(bucket, full, depth + 1)
            else:
                out.append(full)
        if len(page) < 1000:
            return out
        offset += 1000


def remove_tenant_files(tenant_id: str, *, client=None, dry_run: bool = False) -> dict:
    """Delete (or, dry_run, count) every stored object under the tenant's
    prefix in every bucket. Best-effort per bucket; returns {bucket: n}."""
    if not tenant_id or "/" in tenant_id:
        raise ValueError("tenant id")
    c = client or db()
    done: dict = {}
    for name in TENANT_BUCKETS:
        try:
            b = c.storage.from_(name)
            paths = _walk(b, tenant_id)
            if paths and not dry_run:
                for i in range(0, len(paths), 100):
                    b.remove(paths[i:i + 100])
            done[name] = len(paths)
        except Exception:  # noqa: BLE001 — one bucket failing must not stop the rest
            log.warning("tenant file removal failed in %s for %s", name, tenant_id, exc_info=True)
            done[name] = "error"
    return done
