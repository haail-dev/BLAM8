"""Website → knowledge base (the onboarding "paste your URL" flow).

Paste the business's website address on the onboarding Knowledge step and a
model reads a handful of its pages and DRAFTS the knowledge base: short,
labelled fact notes (hours, prices, services, policies, contact) the owner
reviews — edit, untick — before anything is saved. Nothing lands in
knowledge_chunks from here: websites carry stale prices and marketing copy,
and the KB is the agent's fact-check source, so the owner's sign-off is the
ingest gate (the reviewed atomic admission in src/app/knowledge_review.py is the writer).

Two stages:
  crawl()   — the pasted page + up to MAX_PAGES-1 same-site links found on it,
              scored towards the pages that hold facts (about/services/pricing/
              FAQ/contact…). Every fetch is SSRF-guarded via app.knowledge and
              redirect hops are re-validated, same as the single-URL import.
  distill() — one schema-constrained LLM call (operation='kb_website') turns
              the page text into proposed notes. Facts only, never invented —
              the prompt forbids guessing and the review step catches the rest.
"""
import asyncio
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from app import agent, knowledge as kb, llm, usage
from app.config import settings

log = logging.getLogger(__name__)

MAX_PAGES = 8            # pasted page + 7 discovered — enough for an SMB site
PAGE_CHARS = 12_000      # per-page text budget
TOTAL_CHARS = 60_000     # whole-site budget handed to the model
FETCH_TIMEOUT = 12       # per page; pages fetch concurrently so the crawl stays quick
MAX_ENTRIES = 30         # sanity cap on what one distill call may propose

_LINK_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)

# Path keywords that mark the pages facts live on; a match front-runs the queue.
_PRIORITY = ("about", "service", "price", "pricing", "rate", "cost", "faq",
             "contact", "hour", "opening", "polic", "term", "deliver", "book",
             "hire", "menu", "treatment", "area")

# Assets and non-page files a nav can link to — never worth a fetch slot.
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css",
             ".js", ".json", ".xml", ".zip", ".mp3", ".mp4", ".pdf", ".doc",
             ".docx", ".xls", ".xlsx", ".woff", ".woff2", ".ttf")

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "content": {"type": "string"},
                    "topics": {"type":"array", "items":{"type":"string","enum":["prices","rules","process","cadence","payment","safety","coverage","hours"]}},
                    "sources": {"type":"array","items":{"type":"object","properties":{"url":{"type":"string"},"excerpt":{"type":"string"}},"required":["url","excerpt"],"additionalProperties":False}},
                },
                "required": ["label", "content", "topics", "sources"],
                "additionalProperties": False,
            },
        },
        "questions":{"type":"array","items":{"type":"object","properties":{"topic":{"type":"string","enum":["prices","rules","process","cadence","payment","safety","coverage","hours"]},"question":{"type":"string"}},"required":["topic","question"],"additionalProperties":False}},
    },
    "required": ["entries", "questions"],
    "additionalProperties": False,
}

SYSTEM = (
    (
        # The owner's own words, not a report about them (Kris 2026-09-13): the
        # notes are shown to the owner as THEIR knowledge and quoted by the agent
        # to customers, so "We open at 7am" is right and "The business opens at
        # 7am" is not.
        # Found on the first live read (kennels run #10): the site said "book via
        # Revelation Pets" — true of the site, wrong the moment the assistant is
        # the way customers book.
        "You read the text of a small business's website and distill it into a knowledge base for the "
        "AI assistant that answers this business's customer emails. Produce one note per topic customers ask about, as many notes as the pages hold (there is no "
        "target count), and keep EVERY distinct rule, price and policy the pages state — never drop one "
        "to shorten the list; each note covers "
        "ONE topic: what the business does, the services or products offered, "
        "prices, opening hours, service area, booking / cancellation / deposit policies, delivery and "
        "collection, contact details, frequently asked questions. Rules: every fact must come from the "
        "pages — NEVER invent or guess a price, time or policy that is not stated; keep the business's "
        "own numbers, names and terms exactly as written; drop marketing copy, testimonials, cookie "
        "and navigation boilerplate; write plainly in British English. label is a short title of 2-4 "
        "words. If the pages hold no usable facts, return an empty list.\n"
        "VOICE: write every note as the business writing about itself, in the first person plural — "
        "use 'We' or 'Our' with the site's exact facts — never 'the business', 'they', 'their' or "
        "'owners must'. Refer to the business by the name you are given below; never combine it with a "
        "different name found on the site and never write 'trading as'.\n"
        "HOW TO BOOK: the AI assistant IS now how customers enquire and book, so never carry over "
        "instructions to book through another website, app or booking system named on the pages; keep "
        "the policy behind it (a prerequisite, a deposit, notice) and drop the tool."
    )
)
SYSTEM += ("\nFor every note, identify relevant topic keys and cite page URLs with exact supporting excerpts. "
           "Preserve scope, uncertainty and source caveats, including fictional/test facts. Do not generalise a "
           "missing integration into a missing service. Return unanswered follow-up questions under questions, "
           "tagged by topic; absent facts stay unknown. Never infer 'no variation' or 'not applicable'. "
           "Do not remove apparent duplicate proposals: the owner reviews overlap explicitly.")


class DistilledEntries(list):
    questions: list


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def _host_key(host: str | None) -> str:
    h = (host or "").lower()
    return h[4:] if h.startswith("www.") else h


async def _fetch_raw(client: httpx.AsyncClient, url: str) -> tuple[str, bytes, str]:
    """One SSRF-guarded page fetch → (final_url, body, content_type). Redirects
    are followed manually so every hop is re-validated, exactly like the
    single-URL import in app.knowledge."""
    kb._assert_public_url(url)
    for _ in range(kb._MAX_REDIRECTS + 1):
        r = await client.get(url, headers={"User-Agent": "BLAM8-knowledge/1.0"})
        if r.is_redirect and r.next_request is not None:
            url = str(r.next_request.url)
            kb._assert_public_url(url)
            continue
        break
    else:
        raise ValueError("Too many redirects.")
    r.raise_for_status()
    return url, r.content[: kb.MAX_UPLOAD_BYTES], r.headers.get("content-type", "").lower()


_INDEX_NAMES = ("/index.html", "/index.htm", "/default.html")


def _canonical(scheme: str, netloc: str, path: str) -> tuple[str, str]:
    """(url to fetch, comparison key). First-walk finding, 1 Oct 2026: a nav link
    to "index.html" got the home page fetched twice and the eighth page fell off
    the cap. Index names fold into their directory, "" and "/" match, a trailing
    slash and the host's case are ignored; path case is kept (/About != /about)."""
    path = path or "/"
    if path.lower().endswith(_INDEX_NAMES):
        path = path[: path.rfind("/") + 1]
    key = f"{scheme}://{netloc.lower()}{path.rstrip('/')}"
    return f"{scheme}://{netloc}{path}", key


def discover_links(base_url: str, html: str) -> list[str]:
    """Same-site page links found in `html`, priority-scored, deduped, in
    fetch order. The base page itself is not included."""
    base_host = _host_key(urlparse(base_url).hostname)
    base = urlparse(base_url)
    base_key = _canonical(base.scheme, base.netloc, base.path)[1]
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for i, href in enumerate(_LINK_RE.findall(html)):
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if _host_key(parsed.hostname) != base_host or not base_host:
            continue
        path = parsed.path or "/"
        if path.lower().endswith(_SKIP_EXT):
            continue
        clean, key = _canonical(parsed.scheme, parsed.netloc, path)
        if key in seen or key == base_key:
            continue
        seen.add(key)
        lower = path.lower()
        priority = 0 if any(w in lower for w in _PRIORITY) else 1
        scored.append((priority, i, clean))
    return [u for _, _, u in sorted(scored)]


async def crawl(url: str) -> tuple[str, list[dict]]:
    """Fetch the pasted page plus its best same-site links → (site_url, pages)
    where pages = [{url, text}]. Raises ValueError on a bad/blocked URL; a
    discovered page that fails just drops out."""
    site = normalize_url(url)
    if not site:
        raise ValueError("Paste your website address first.")
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        final_url, body, ctype = await _fetch_raw(client, site)
        if "text/html" not in ctype:
            raise ValueError("That address isn't a web page — paste your website's home page.")
        html = body.decode("utf-8", errors="replace")
        pages = [{"url": final_url, "text": kb._html_to_text(html)[:PAGE_CHARS]}]

        sem = asyncio.Semaphore(4)

        async def grab(u: str) -> dict | None:
            async with sem:
                try:
                    fu, b, ct = await _fetch_raw(client, u)
                    if "text/html" not in ct:
                        return None
                    text = kb._html_to_text(b.decode("utf-8", errors="replace"))[:PAGE_CHARS]
                    # Under ~200 chars a page is a stub or a JS shell — no facts.
                    return {"url": fu, "text": text} if len(text) >= 200 else None
                except Exception:
                    return None  # one dead link never sinks the crawl

        links = discover_links(final_url, html)[: MAX_PAGES - 1]
        fetched = await asyncio.gather(*(grab(u) for u in links))

    seen_urls = {pages[0]["url"].rstrip("/")}
    total = len(pages[0]["text"])
    for page in fetched:
        if page is None or page["url"].rstrip("/") in seen_urls:
            continue
        if total >= TOTAL_CHARS:
            break
        page["text"] = page["text"][: TOTAL_CHARS - total]
        total += len(page["text"])
        seen_urls.add(page["url"].rstrip("/"))
        pages.append(page)
    return final_url, pages


def _build_prompt(site: str, pages: list[dict], business_name: str | None,
                  industry: str | None) -> str:
    lines = [f"The business: {business_name or 'unknown'}"
             + (f" ({industry})" if industry else ""),
             f"Their website: {site}", ""]
    for p in pages:
        lines += [f"=== PAGE: {p['url']} ===", p["text"], ""]
    return "\n".join(lines)


async def distill(tenant_id: str, site: str, pages: list[dict], *,
                  business_name: str | None = None,
                  industry: str | None = None) -> list[dict]:
    """One LLM pass over the crawled text → proposed [{label, content}]. An
    unparseable reply is an empty proposal, never an error."""
    msg = await llm.create(
        client=agent.client(), tenant_id=tenant_id, operation="kb_website",
        model=settings.anthropic_model,
        cache=False,   # one-shot unique payload — a breakpoint is a pure write surcharge
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user",
                   "content": _build_prompt(site, pages, business_name, industry)}],
        output_config={"format": {"type": "json_schema", "schema": DISTILL_SCHEMA}},
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="kb_website")
    try:
        data = json.loads(text)
        entries = data.get("entries") or []
    except (ValueError, AttributeError):
        return []
    result = DistilledEntries([
        {"label": (e.get("label") or "").strip() or "Website note",
         "content": e["content"].strip(), "topics": e.get("topics", []),
         "provenance": [{"source": p["url"], "excerpt": cite["excerpt"][:1500]}
             for cite in e.get("sources", []) if isinstance(cite,dict)
             for p in pages if cite.get("url")==p["url"] and cite.get("excerpt")
             and " ".join(cite["excerpt"].split()) in " ".join(p["text"].split())]}
        for e in entries[:MAX_ENTRIES]
        if isinstance(e, dict) and (e.get("content") or "").strip()
    ])
    from app.knowledge_review import clean_questions
    result.questions = clean_questions(data.get("questions"))
    for entry in result:
        if not entry["provenance"]:
            entry["provenance"] = [{"source":p["url"],"excerpt":"Page read; no exact supporting quotation was supplied. Check this source before approving."} for p in pages]
    return result


async def build(tenant_id: str, url: str, *, business_name: str | None = None,
                industry: str | None = None) -> dict:
    """The whole flow behind the onboarding endpoint: crawl then distill.
    Returns {site, pages: [url, …], entries: [{label, content}, …]}."""
    site, pages = await crawl(url)
    entries = await distill(tenant_id, site, pages,
                            business_name=business_name, industry=industry)
    from app.knowledge_fidelity import inventory
    return {"site": site, "pages": [p["url"] for p in pages], "entries": entries,
            "questions":getattr(entries,"questions",[]), "source_units":inventory(pages),
            **({'evidence_pages': pages} if settings.knowledge_source_review_enabled else {})}
