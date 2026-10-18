"""A promise on the CRM contact page is a stacked block, not a squeezed row.

Kris, 2026-09-20: "The promises aren't displayed well in the contacts CRM."
On a phone the one-line row — label, words, buttons — squeezed the words
into a column a few words wide, and a cleared row's "cleared by BK-1003 ·
20th Sep 26" into a word per line.

Now: the label and the buttons (or the cleared-by line) share the first
line; the words get the whole width beneath; the amber "looks cleared" and
a cleared row's evidence sit under the words. Everything the page said
before, it still says.

Run: python -m pytest tests/test_crm_promise_rows.py -q
"""
import re

import app.dashboard as dashboard
from tests.test_crm_routes import Env, client  # noqa: F401  (the fixture)

OPEN = {"id": "p1", "direction": "we_owe", "status": "open",
        # An obligation, not a booking the diary holds (promise_kinds hides those from the fold).
        "description": "Weekend boarding quote to be sent for Friday 9 October to Monday 12 October.",
        "due_hint": "by Friday 2 October"}
SUGGESTED = {"id": "p3", "direction": "they_owe", "status": "open", "resolution_type": "suggested",
             "description": "Biscuit's owner to send the signed boarding form.",
             "resolution_evidence": "food mentioned in their reply",
             "resolved_by_kind": "email", "resolved_by_id": "e9", "resolved_by_label": "their email"}
CLEARED = {"id": "p4", "direction": "they_owe", "status": "resolved",
           "resolved_at": "2026-09-20T13:55:00+00:00",
           "description": "Bring Biscuit for a meet and greet at 1pm on Wednesday 7 October.",
           "resolution_evidence": "BK-1003 confirmed.",
           "resolved_by_kind": "booking", "resolved_by_id": "bk1", "resolved_by_label": "BK-1003"}


def _page(client, monkeypatch, open_rows, cleared_rows):
    Env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "crm_open_promises",
                        lambda tid, contact_id=None, client=None: [dict(p) for p in open_rows])
    monkeypatch.setattr(dashboard.db, "crm_recent_cleared_promises",
                        lambda *a, **k: [dict(p) for p in cleared_rows])
    return client.get("/app/crm/contacts/ct1").text


def _block(html, attr):
    """One promise's block: from its div to the footnote that always follows
    the last row (a lone row has nothing else after it)."""
    start = html.index(attr)
    start = html.rfind("<div ", 0, start)
    return html[start:html.index("What you owe them", start)]


def test_an_open_promise_is_label_and_buttons_then_the_words(client, monkeypatch):
    block = _block(_page(client, monkeypatch, [OPEN], []), 'data-promise="p1"')
    first = block[:block.index("</form>")]
    assert "you owe" in first and 'value="done"' in first and 'value="dropped"' in first
    words = block[block.index("</form>"):]
    assert '<p class="mt-1 text-slate-700">Weekend boarding quote' in words
    assert "· by Friday 2 October" in words
    assert "flex-1 min-w-0" not in block                     # the squeeze is gone


def test_a_promise_that_looks_cleared_keeps_its_amber_line_under_the_words(client, monkeypatch):
    html = _page(client, monkeypatch, [SUGGESTED], [])
    block = html[html.index('data-promise="p3"'):]
    block = block[:block.index("What you owe them")]
    assert 'value="confirm"' in block and 'value="keep"' in block
    assert re.search(r'<p class="mt-1 text-slate-700">Biscuit&#39;s owner.*?</p>\s*<p class="mt-0\.5 text-xs text-amber-700">looks cleared', block, re.S)


def test_a_cleared_promise_is_tick_and_cleared_by_then_the_words(client, monkeypatch):
    block = _block(_page(client, monkeypatch, [], [CLEARED]), 'data-cleared-promise="p4"')
    first = block[:block.index('<p class="mt-1')]
    assert "they owed" in first and 'href="/app/diary/bk1"' in first and "cleared by BK-1003" in first
    assert "20th Sep 26" in first
    assert "whitespace-nowrap" not in first.split("</span>", 1)[1]   # the cleared-by line may wrap
    words = block[block.index('<p class="mt-1'):]
    assert "Bring Biscuit for a meet and greet" in words
    assert "BK-1003 confirmed." in _page(client, monkeypatch, [], [CLEARED])


def test_nothing_open_and_nothing_cleared_still_reads(client, monkeypatch):
    html = _page(client, monkeypatch, [], [])
    assert "Nothing open — commitments BLAM8 reads in the mail flow land here by themselves." in html
    assert "data-promise=" not in html and "data-cleared-promise" not in html
