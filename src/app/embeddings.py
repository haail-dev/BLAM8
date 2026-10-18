"""Voyage AI embeddings — the knowledge base's retrieval backbone.

Anthropic doesn't offer embeddings, so the knowledge base pairs with Voyage
(decision: voyage-3, 1024-dim — see README). Called server-side only, via httpx
(no SDK dependency). Degrades cleanly: is_configured() is False when no key is
set, and callers treat the knowledge base as simply absent.
"""
import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_URL = "https://api.voyageai.com/v1/embeddings"
_MAX_BATCH = 100   # Voyage accepts up to 128 inputs/request; stay comfortably under
_RETRIES_429 = 3   # Voyage rate-limits low tiers hard (429 storms observed 2026-07-12);
_MAX_WAIT = 25.0   # honour Retry-After, capped, so a burst degrades to slow not silent


def is_configured() -> bool:
    return bool(settings.voyage_api_key)


async def _embed(texts: list[str], input_type: str) -> tuple[list[list[float]], int]:
    """Embed a list of texts. `input_type` is 'document' (for stored chunks) or
    'query' (for the incoming email) — Voyage tunes the embedding for each side.
    Returns (embeddings, total_tokens) so callers can meter usage."""
    if not is_configured():
        raise RuntimeError("VOYAGE_API_KEY is not set — the knowledge base is disabled.")
    headers = {
        "Authorization": f"Bearer {settings.voyage_api_key}",
        "Content-Type": "application/json",
    }
    out: list[list[float]] = []
    total_tokens = 0
    async with httpx.AsyncClient(timeout=60) as c:
        for i in range(0, len(texts), _MAX_BATCH):
            batch = texts[i : i + _MAX_BATCH]
            for attempt in range(_RETRIES_429 + 1):
                r = await c.post(
                    _URL,
                    headers=headers,
                    json={
                        "input": batch,
                        "model": settings.voyage_model,
                        "input_type": input_type,
                        "output_dimension": settings.voyage_output_dimension,
                    },
                )
                if r.status_code != 429 or attempt == _RETRIES_429:
                    break
                wait = min(float(r.headers.get("retry-after") or 21), _MAX_WAIT)
                logger.warning("Voyage 429 (attempt %d/%d) — waiting %.0fs before retry",
                               attempt + 1, _RETRIES_429, wait)
                await asyncio.sleep(wait)
            r.raise_for_status()
            payload = r.json()
            total_tokens += (payload.get("usage") or {}).get("total_tokens") or 0
            data = sorted(payload["data"], key=lambda d: d["index"])
            out.extend(item["embedding"] for item in data)
    return out, total_tokens


async def embed_documents(texts: list[str]) -> tuple[list[list[float]], int]:
    return await _embed(texts, "document")


async def embed_query(text: str) -> tuple[list[float], int]:
    vecs, tokens = await _embed([text], "query")
    return vecs[0], tokens
