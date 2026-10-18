"""Parsing files strangers send us, without letting one file stop the service
(security review 2026-09, H4/H5).

H4 — images: a ~500 KB PNG can declare 13000×13000 pixels and decompress to
gigabytes; Pillow had no pixel cap, so one emailed picture could run the single
web process out of memory for every business. `open_image` reads the size from
the header and refuses anything over MAX_PIXELS before a single pixel decodes.

H5 — PDFs: a 10 KB crafted PDF kept pypdf busy for 12 s and 228 MB on the
shared event loop. `pdf_text` parses in a throwaway child process with a wall-
clock timeout and (on Linux, where we run) a memory ceiling; a hostile file
costs that one child, never the platform.

M31/M32 — Office files (.xlsx/.docx) are zip archives: an 80 KB .docx reached a
673 MB working set, and a 4.8 KB .xlsx declared a cell at row 2000, column XFD
(2000×16384 empty cells). `office_zip_ok` reads the zip's own directory before
any parser opens it; the xlsx readers also stop at XLSX_MAX_ROWS/COLS.
"""
from __future__ import annotations

import io
import logging
import multiprocessing as mp
import warnings

log = logging.getLogger(__name__)

MAX_PIXELS = 30_000_000          # 30 MP — far above any phone photo (12–48 MP phones downscale)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
PDF_TIMEOUT_S = 8.0
PDF_MEMORY_MB = 384            # address-space cap for the child; Python + pypdf need ~60-150 MB of it
ZIP_MAX_MEMBERS = 2000
ZIP_MAX_TOTAL = 60 * 1024 * 1024     # uncompressed, whole archive
ZIP_MAX_MEMBER = 40 * 1024 * 1024    # uncompressed, any one part
ZIP_MAX_RATIO = 150                  # uncompressed ÷ compressed, per part of any size
XLSX_MAX_ROWS = 20_000
XLSX_MAX_COLS = 200

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
except Exception:  # noqa: BLE001 — no Pillow: open_image returns None
    Image = None


def open_image(data: bytes):
    """A PIL image, or None when it isn't a safe image to decode. Only the
    header has been read when the size is checked — nothing is decompressed."""
    if Image is None or not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
            log.warning("image refused: %sx%s pixels (cap %s)", w, h, MAX_PIXELS)
            return None
        return im
    except Exception:  # noqa: BLE001 — incl. DecompressionBombWarning/Error
        log.info("image not openable", exc_info=True)
        return None


def office_zip_ok(data: bytes) -> bool:
    """True when an .xlsx/.docx archive is safe to hand to openpyxl/python-docx.
    Python's zipfile never inflates a part past its DECLARED size (it stops,
    then fails the CRC), so the declared sizes are a real upper bound."""
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except Exception:  # noqa: BLE001 — not a zip: the parser will refuse it itself
        return False
    if len(infos) > ZIP_MAX_MEMBERS:
        log.warning("office file refused: %s parts", len(infos))
        return False
    total = 0
    for i in infos:
        total += i.file_size
        if (i.file_size > ZIP_MAX_MEMBER
                or (i.file_size > 1024 * 1024 and i.file_size > ZIP_MAX_RATIO * max(i.compress_size, 1))):
            log.warning("office file refused: part %s %s→%s bytes", i.filename, i.compress_size, i.file_size)
            return False
    if total > ZIP_MAX_TOTAL:
        log.warning("office file refused: %s bytes uncompressed", total)
        return False
    return True


def xlsx_rows(ws, *, max_rows: int = XLSX_MAX_ROWS, max_cols: int = XLSX_MAX_COLS):
    """ws.iter_rows(values_only=True), clamped: a sheet can DECLARE row
    1,048,576 or column XFD with a few bytes, and openpyxl fills every empty
    cell up to it."""
    return ws.iter_rows(values_only=True, max_row=max_rows, max_col=max_cols)


def _pdf_child(data: bytes, max_pages: int, max_chars: int, mem_mb: int, out) -> None:
    try:
        try:
            import resource   # Linux only: a hard ceiling on this child's memory
            cap = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        except Exception:  # noqa: BLE001 — Windows dev: timeout still applies
            pass
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages[:max_pages]]
        out.put("\n".join(pages).strip()[:max_chars])
    except BaseException:  # noqa: BLE001 — MemoryError included
        out.put("")


def pdf_text(data: bytes, *, max_pages: int, max_chars: int,
             timeout_s: float = PDF_TIMEOUT_S, mem_mb: int = PDF_MEMORY_MB) -> str:
    """The text layer of a PDF, read in a child process. '' for anything too
    big, too slow, too hungry or unreadable. Never raises. Blocking — call it
    via asyncio.to_thread from async code."""
    if not data or len(data) > MAX_PDF_BYTES:
        return ""
    ctx = mp.get_context("spawn")
    q = ctx.Queue(maxsize=1)
    p = ctx.Process(target=_pdf_child, args=(data, max_pages, max_chars, mem_mb, q), daemon=True)
    try:
        p.start()
        p.join(timeout_s)
        if p.is_alive():
            log.warning("pdf parse timed out after %.0fs — skipped", timeout_s)
            p.kill()
            p.join(1)
            return ""
        try:
            return q.get_nowait() or ""
        except Exception:  # noqa: BLE001 — the child died (memory cap) without answering
            # Loud, so a cap that is too tight for real files shows in the logs
            # instead of silently reading nothing from every certificate.
            log.warning("pdf parse child ended without a result (exit code %s)", p.exitcode)
            return ""
    except Exception:  # noqa: BLE001
        log.warning("pdf parse failed", exc_info=True)
        return ""
    finally:
        try:
            q.close()
        except Exception:  # noqa: BLE001
            pass
