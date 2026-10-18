"""The recall viewer closes with a discreet cross, not a sentence.

Kris, 2026-09-20: "when I click the knowledge to show what it found, we have
Back to the draft as a link to get back — I want that changed to a discreet
x."

Same close as the promise pop-up: a × that is quiet to look at and thumb-
sized to hit, its words kept for a screen reader and a hover. The handle the
script listens for (data-matches-close) is unchanged.

Run: python -m pytest tests/test_matches_close.py -q
"""
from pathlib import Path

from tests.test_thread_decision import ROW, _env, client  # noqa: F401  (the fixture)

THREAD = Path("src/app/templates/app/_thread.html").read_text(encoding="utf-8")
PAGE = Path("src/app/templates/app/conversation.html").read_text(encoding="utf-8")


def test_the_viewer_closes_with_a_cross(client, monkeypatch):
    _env(monkeypatch, [dict(ROW, draft_sources=[{"source": "Opening hours", "similarity": 0.9}])],
         trace=[{"trace_type": "kb_retrieval", "detail": {"query": "hours", "results_returned": 1}}])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    panel = html[html.index('id="matches-panel"'):html.index('id="matches-list"')]
    assert "Back to the draft</button>" not in panel
    assert 'data-matches-close aria-label="Back to the draft" title="Back to the draft"' in panel
    assert "&times;</button>" in panel or "×</button>" in panel
    assert "text-xl leading-none text-slate-400" in panel      # quiet, and the same size as the pop-up's cross


def test_the_script_still_finds_it():
    assert "e.target.closest('[data-matches-close]')" in PAGE
    assert THREAD.count("data-matches-close") == 1
