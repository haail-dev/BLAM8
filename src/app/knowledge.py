"""Knowledge base — the tenant's fact-check sources.

Ingest: a file / URL / pasted note → extract text → chunk → Voyage embeddings →
store in knowledge_chunks under one `source` (re-importing the same source
replaces it). Retrieve: embed the incoming email, match the tenant's chunks
(pgvector), and hand the closest back for the draft prompt's <knowledge> block.

Retrieval is best-effort: if Voyage is unconfigured or errors, it returns [] and
drafting proceeds without facts rather than failing. Ingestion does NOT swallow
errors — the dashboard surfaces them so the tenant knows an import failed.
"""
import asyncio
import csv
import io
import ipaddress
import logging
import re
import socket
from html import unescape
from urllib.parse import urlparse

import httpx

from app import db, embeddings, usage
from app.config import settings

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB per file / URL
MAX_CHUNKS_PER_SOURCE = 300         # cost guardrail on a single import
_MAX_QUERY_CHARS = 8000             # cap the email text we embed as a query


def is_configured() -> bool:
    return embeddings.is_configured()


# ── text extraction ──────────────────────────────────────────────────────────
def extract_text(filename: str, data: bytes) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext in ("txt", "md", "markdown", "text"):
        return data.decode("utf-8", errors="replace")
    if ext in ("csv", "tsv"):
        return _csv_to_text(data)
    if ext == "pdf":
        return _pdf_to_text(data)
    if ext in ("xlsx", "xlsm"):
        return _xlsx_to_text(data)
    if ext == "docx":
        return _docx_to_text(data)
    return data.decode("utf-8", errors="replace")  # best-effort fallback


def _csv_to_text(data: bytes) -> str:
    rows = csv.reader(io.StringIO(data.decode("utf-8", errors="replace")))
    return "\n".join(", ".join(cell.strip() for cell in row) for row in rows if any(row))


def _pdf_to_text(data: bytes) -> str:
    # A child process with a timeout + memory cap (security review 2026-09,
    # H5) — an uploaded PDF can't stall or starve the service. Generous limits:
    # an owner's price list may run to many pages.
    from app import safe_parse
    return safe_parse.pdf_text(data, max_pages=300, max_chars=600_000, timeout_s=45.0)


def _xlsx_to_text(data: bytes) -> str:
    from openpyxl import load_workbook
    from app import safe_parse

    if not safe_parse.office_zip_ok(data):   # M31: zip bomb / not a real workbook
        raise ValueError("That spreadsheet is too large or damaged to read — save it as CSV and try again.")
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets[:20]:
        parts.append(f"# {ws.title}")
        for row in safe_parse.xlsx_rows(ws):
            cells = [str(c).strip() for c in row if c is not None]
            if cells:
                parts.append(", ".join(cells))
    return "\n".join(parts)


def _docx_to_text(data: bytes) -> str:
    from docx import Document
    from app import safe_parse

    if not safe_parse.office_zip_ok(data):   # M32: an 80 KB .docx reached 673 MB
        raise ValueError("That Word file is too large or damaged to read — save it as PDF or text and try again.")
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


# ── URL fetch ────────────────────────────────────────────────────────────────
_MAX_REDIRECTS = 4


def _assert_public_url(url: str) -> None:
    """SSRF guard: reject non-http(s) URLs and any host that resolves to a
    private / loopback / link-local / reserved address. Stops a tenant-supplied
    knowledge URL from reaching cloud metadata (169.254.169.254), localhost, or
    internal RFC-1918 services. (Residual: DNS-rebind between this check and the
    socket connect is possible but low-risk given the one-shot 30s fetch.)"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are supported.")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host.")
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host: {host}") from e
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError as e:
            raise ValueError(f"Unresolvable address for host: {host}") from e
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise ValueError("URLs that resolve to internal/private addresses are not allowed.")


async def fetch_url(url: str) -> str:
    """Fetch a URL's text. Handles HTML (tags stripped), CSV (e.g. a Google Sheet
    'publish to web' link), and plain text. One-time import on submit.

    Redirects are followed manually so each hop is SSRF-revalidated — an attacker
    can't 302 from a public host to an internal one."""
    _assert_public_url(url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        for _ in range(_MAX_REDIRECTS + 1):
            r = await c.get(url, headers={"User-Agent": "BLAM8-knowledge/1.0"})
            if r.is_redirect and r.next_request is not None:
                url = str(r.next_request.url)
                _assert_public_url(url)  # re-validate the redirect target
                continue
            break
        else:
            raise ValueError("Too many redirects.")
        r.raise_for_status()
        content = r.content[:MAX_UPLOAD_BYTES]
    ctype = r.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        return _html_to_text(content.decode("utf-8", errors="replace"))
    if "csv" in ctype or url.lower().endswith(".csv"):
        return _csv_to_text(content)
    return content.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    # Page chrome goes as whole ELEMENTS, not just tags: site builders (Wix,
    # Squarespace) repeat the full menu + footer on every page, so a bare
    # tag-strip front-loads every source's first chunk with the same nav
    # boilerplate — and <head> leaks the <title> text. Non-greedy is safe:
    # these semantic elements don't nest within themselves; one that's never
    # closed simply falls through to the tag-strip below.
    html = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe)\b[^>]*>.*?</\1\s*>", " ", html)
    html = re.sub(r"(?is)<(head|nav|header|footer|aside)\b[^>]*>.*?</\1\s*>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # Entities after tag-strip (&nbsp; &amp; &quot; read as junk in chunks);
    # &nbsp; becomes \xa0, which must fold into the whitespace collapse.
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\s*\n\s*", "\n", text)).strip()


# ── chunking ─────────────────────────────────────────────────────────────────
def chunk_text(text: str, size: int = 1500, overlap: int = 200) -> list[str]:
    """Pack paragraphs into ~`size`-char chunks; hard-split any oversized one."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    buf = ""
    for para in (p.strip() for p in text.split("\n\n") if p.strip()):
        if buf and len(buf) + len(para) + 2 > size:
            chunks.append(buf)
            buf = ""
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), size - overlap):
                chunks.append(para[i : i + size])
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks[:MAX_CHUNKS_PER_SOURCE]


EDITABLE_MAX_CHUNKS = 6   # beyond this it is an imported document, not a note


def join_chunks(chunks: list[str]) -> str:
    """Chunks back into the text they were made from. chunk_text packs whole
    paragraphs, so joining on a blank line is exact — except for a single
    oversized paragraph, which is hard-split with `overlap` characters repeated
    at the seam. Those repeats are dropped here, or an edit-and-save would
    duplicate a sentence every time it went round."""
    out = ""
    for ch in chunks:
        ch = ch or ""
        if not out:
            out = ch
            continue
        seam = 0
        for n in range(min(len(out), len(ch), 400), 20, -1):
            if out[-n:] == ch[:n]:
                seam = n
                break
        out = out + ch[seam:] if seam else out + "\n\n" + ch
    return out


# ── ingest & retrieve ────────────────────────────────────────────────────────
def _vec_literal(vec: list[float]) -> str:
    """pgvector's text input form, '[0.1,0.2,...]' — PostgREST casts it to vector
    on insert and in the match RPC."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def ingest(tenant_id: str, source: str, text: str, client=None,
                 mailbox_id: str | None = None, *, expected_version: str | None = None,
                 original_source: str | None = None) -> int:
    """`mailbox_id` scopes the source to one mailbox (enterprise D4 — layered
    knowledge); None = company-wide, which is every pre-enterprise import."""
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("No readable text found to import.")
    vectors, embed_tokens = await embeddings.embed_documents(chunks)
    usage.record_voyage(tenant_id, settings.voyage_model, embed_tokens, operation="embed_ingest")
    rows = [
        {"tenant_id": tenant_id, "source": source, "content": ch,
         "embedding": _vec_literal(v), "mailbox_id": mailbox_id}
        for ch, v in zip(chunks, vectors)
    ]
    if expected_version is None:
        db.replace_knowledge_source(tenant_id, source, rows, client=client, mailbox_id=mailbox_id)
    else:
        from app import knowledge_forms
        knowledge_forms.save(tenant_id, original_source or source, source, mailbox_id,
                             expected_version, rows, client=client)
    return len(rows)


# A knowledge base this small is read WHOLE by a draft (Kris 2026-09-26,
# services rig: "do you cover Swindon?" retrieved one note, not the business's
# own "Coverage — Bath and within 15 miles", and the reply answered from the
# model's own geography). Nine short notes are ~2K tokens; missing the one rule
# that decides the answer costs far more. Rows past the similarity floor ride
# along as `_context` — in the prompt, never in the "Grounded on" pills.
WHOLE_KB_MAX_CHUNKS = 25
WHOLE_KB_MAX_CHARS = 9000


class RetrievalRows(list):
    """List-compatible reporting metadata; never changes the facts supplied."""
    def __init__(self, rows=(), *, outcome: str, query: str = ""):
        super().__init__(rows)
        self.outcome = outcome
        self.query = query


async def retrieve(tenant_id: str, query: str, k: int | None = None,
                   mailbox_id: str | None = None, whole_if_small: bool = False) -> list[dict]:
    """With a mailbox_id, retrieval UNIONS company-wide facts with that mailbox's
    own (D4); without one it matches company-wide only — everything, for a
    standard tenant. Best-effort with a bounded retry: a transient embeddings or
    RPC failure used to silently strip ALL knowledge from the draft (the agent
    then hedges or invents — caught by the 2026-07-12 red-team), so we retry
    twice with a short backoff and log every failure before giving up."""
    if not is_configured() or not query.strip():
        return RetrievalRows(outcome="not_run")
    last_exc: Exception | None = None
    for attempt in range(3):
        if attempt:
            # Drafting is a background pipeline, so a few seconds of backoff is
            # cheap insurance against Voyage 429 bursts (observed 2026-07-12).
            await asyncio.sleep(2 * attempt + (attempt - 1))  # 2s, then 5s
        try:
            vec, embed_tokens = await embeddings.embed_query(query[:_MAX_QUERY_CHARS])
            usage.record_voyage(tenant_id, settings.voyage_model, embed_tokens, operation="embed_query")
            k_top = k or settings.knowledge_match_count
            rows_all = db.match_knowledge_chunks(
                tenant_id, _vec_literal(vec),
                max(k_top, WHOLE_KB_MAX_CHUNKS) if whole_if_small else k_top,
                mailbox_id=mailbox_id,
            ) or []
            rows = rows_all[:k_top]
            # Relevance floor (Kris 2026-08-31): the K nearest neighbours come
            # back however distant — a booking email "matched" six product
            # pages at 0.19-0.38. Below the floor is noise: it never reaches
            # the prompt, the pills, or the recall viewer's count, so "0
            # matches" is now a sentence retrieval can honestly say.
            floor = settings.knowledge_min_similarity
            kept = [r for r in rows if (r.get("similarity") or 0) >= floor]
            out = kept or _keyword_rescue(query, rows)
            if (whole_if_small and len(rows_all) < WHOLE_KB_MAX_CHUNKS
                    and sum(len(r.get("content") or "") for r in rows_all) <= WHOLE_KB_MAX_CHARS):
                have = {id(r) for r in out}
                out = out + [{**r, "_context": True} for r in rows_all if id(r) not in have]
            return RetrievalRows(out, outcome="ok", query=query[:_MAX_QUERY_CHARS])
        except Exception as e:
            last_exc = e
            logger.warning("knowledge retrieval failed (attempt %d/3, tenant %s): %r",
                           attempt + 1, tenant_id, e)
    logger.error("knowledge retrieval gave up after 3 attempts (tenant %s): %r — "
                 "drafting proceeds WITHOUT retrieved facts", tenant_id, last_exc)
    return RetrievalRows(outcome="failed", query=query[:_MAX_QUERY_CHARS])


_STOP = {"the", "and", "for", "what", "how", "much", "would", "are", "does", "your",
         "you", "with", "this", "that", "our", "can", "please", "about", "any", "all"}


def _stems(text: str) -> list[str]:
    """Content words as 4-letter stems (trim/trims/trimmed → trim, price/prices →
    pric); short and function words dropped."""
    words = re.findall(r"[a-z0-9£]+", (text or "").lower())
    return sorted({w[:4] for w in words if len(w) >= 3 and w not in _STOP})


def _keyword_rescue(query: str, rows: list[dict]) -> list[dict]:
    """When the floor leaves nothing, keep a nearest-neighbour row whose source
    or content literally carries EVERY content word of the query. Live
    2026-09-04 (services run): the portal rewrote "how much would services be for
    three subjects at the site?" to "service price", a two-word query that scored
    under the floor against the one note that says "service £35" — and the
    customer was told there was no pricing. The full sentence retrieved it;
    the summary didn't. Words are matched by 4-letter stem so services ~ service and
    price ~ Prices (the note's own label counts). An empty stem list (a query
    of stop words) rescues nothing, so the floor still says "0 matches" when
    that is the truth."""
    stems = _stems(query)
    if not stems:
        return []
    out = []
    for r in rows:
        hay = f"{r.get('source') or ''} {r.get('content') or ''}".lower()
        if all(st in hay for st in stems):
            out.append(r)
    return out


def render_for_prompt(chunks: list[dict]) -> str | None:
    if not chunks:
        return None
    return "\n\n".join(f"[{c.get('source') or 'source'}]\n{c['content']}" for c in chunks)


# ── correction loop (Phase 3 F2) ──────────────────────────────────────────────
async def search_chunks(tenant_id: str, text: str, k: int = 5,
                        mailbox_id: str | None = None) -> list[dict]:
    """Trace a flagged piece of draft text back to the knowledge it likely came from —
    searched in the same scope the draft retrieved from (company-wide ∪ its mailbox).
    Returns [{chunk_id, source, content, similarity}]; [] when the KB is off/empty."""
    if not is_configured() or not text.strip():
        return []
    vec, embed_tokens = await embeddings.embed_query(text[:_MAX_QUERY_CHARS])
    usage.record_voyage(tenant_id, settings.voyage_model, embed_tokens, operation="embed_flag")
    rows = db.match_knowledge_chunks(tenant_id, _vec_literal(vec), k, mailbox_id=mailbox_id)
    return [
        {"chunk_id": r["id"], "source": r.get("source"), "content": r["content"],
         "similarity": r.get("similarity")}
        for r in rows
    ]


async def update_chunk(tenant_id: str, chunk_id: str, content: str, client=None) -> dict:
    """Correct one matched chunk: re-embed the edited content (as a *document*, matching
    ingest) and save it tenant-scoped. Raises LookupError if the chunk isn't this
    tenant's, ValueError if the content is empty."""
    content = (content or "").strip()
    if not content:
        raise ValueError("Knowledge cannot be empty.")
    existing = db.get_knowledge_chunk(chunk_id, tenant_id, client=client)
    if not existing:
        raise LookupError("Chunk not found")
    vectors, embed_tokens = await embeddings.embed_documents([content])
    usage.record_voyage(tenant_id, settings.voyage_model, embed_tokens, operation="embed_update")
    db.update_knowledge_chunk(chunk_id, content, _vec_literal(vectors[0]), tenant_id, client=client)
    return {"chunk_id": chunk_id, "source": existing.get("source")}
