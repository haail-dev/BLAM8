"""Retrieval relevance floor (Kris 2026-08-31: "every search provides six
matches... most aren't even relevant").

Vector search returns the K nearest neighbours however distant, so a
booking-only email "matched" six product pages at 0.19-0.38 and the recall
viewer showed 6 matches on every draft. Below settings.knowledge_min_similarity
nothing reaches the prompt, the pills, or the match count — "0 matches" is a
sentence retrieval can now honestly say.

Run: python -m pytest tests/test_knowledge_floor.py -q
"""
import asyncio

from app import knowledge
from app.config import settings


def _wire(monkeypatch, rows):
    async def fake_embed(q):
        return [0.0], 1
    monkeypatch.setattr(knowledge, "is_configured", lambda: True)
    monkeypatch.setattr(knowledge.embeddings, "embed_query", fake_embed)
    monkeypatch.setattr(knowledge.usage, "record_voyage", lambda *a, **k: None)
    monkeypatch.setattr(knowledge.db, "match_knowledge_chunks",
                        lambda *a, **k: [dict(r) for r in rows])


def test_noise_below_the_floor_never_surfaces(monkeypatch):
    _wire(monkeypatch, [
        {"source": "Scheduling", "content": "x", "similarity": 0.56},
        {"source": "Plans", "content": "x", "similarity": 0.45},
        {"source": "Calendar connect", "content": "x", "similarity": 0.38},
        {"source": "Q&A promo", "content": "x", "similarity": 0.19},
    ])
    out = asyncio.run(knowledge.retrieve("t1", "book me in Friday"))
    assert [r["source"] for r in out] == ["Scheduling", "Plans"]  # >= floor only


def test_a_short_query_rescues_the_chunk_that_literally_matches(monkeypatch):
    # Live 2026-09-04 (farrier portal): "trim price" scored under the floor
    # against the one note that says "trim £35"; the customer was told there
    # was no pricing. The note's label ("Prices") and content carry both words.
    _wire(monkeypatch, [
        {"source": "Prices", "content": "Full set of shoes £95; trim £35 (£30 each for three or more)", "similarity": 0.41},
        {"source": "Coverage", "content": "We cover the Mendips: Wells, Shepton, Frome", "similarity": 0.33},
    ])
    out = asyncio.run(knowledge.retrieve("t1", "trim price"))
    assert [r["source"] for r in out] == ["Prices"]
    # plural/inflection matches by stem; a word the chunk lacks still refuses
    assert [r["source"] for r in asyncio.run(knowledge.retrieve("t1", "trims prices"))] == ["Prices"]
    assert asyncio.run(knowledge.retrieve("t1", "trim discount")) == []
    # a query made of stop words rescues nothing
    assert asyncio.run(knowledge.retrieve("t1", "what about that")) == []


def test_a_pure_booking_email_can_honestly_match_nothing(monkeypatch):
    _wire(monkeypatch, [
        {"source": "Scheduling", "content": "x", "similarity": 0.38},
        {"source": "About", "content": "x", "similarity": 0.21},
    ])
    assert asyncio.run(knowledge.retrieve("t1", "10:00 Friday please")) == []


def test_the_floor_is_calibrated_conservatively():
    # 0.45 splits the live data: relevant hits ran 0.46-0.78, booking noise
    # topped out at 0.45. Raising it risks starving drafts of real facts.
    assert settings.knowledge_min_similarity == 0.45
