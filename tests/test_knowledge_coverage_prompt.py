"""The knowledge interview never re-proposes a saved note (hot tub run
2026-09-04: every turn re-listed all saved notes until the reply overflowed)
and asks for one note per kind, not one per price line."""
from pathlib import Path

from app import knowledge_coverage as kc


def test_saved_notes_are_named_and_forbidden():
    cov = kc.coverage([{"source": "Prices", "text": "Routine service £95"},
                       {"source": "Where we work", "text": "We cover the Peak District"}])
    block = kc.prompt_block(cov)
    assert "ALREADY SAVED" in block
    assert "Prices; Where we work" in block
    assert "Propose ONLY the new notes" in block
    assert "ONE note per kind" in block


def test_no_saved_notes_means_no_saved_line():
    block = kc.prompt_block(kc.coverage([]))
    assert "ALREADY SAVED" not in block
    assert "NOT YET" in block


def test_the_chat_box_takes_a_real_answer():
    html = Path("src/app/templates/_concierge.html").read_text(encoding="utf-8")
    assert 'id="bc-input" rows="1" maxlength="4000"' in html
