"""The Inbox's month deck (Kris 2026-09-24): "I would like the Card style
added to the Inbox too. Make the Emails stack inside each month, the months
can scroll like they do now."

Each open month's emails are the Today page's deck (home.html) inside the
month's own capped scroller: sticky piles with home.html's pins, the same
three-state z-order, words frosted until 90% shows. The page still scrolls
from month to month.

Run: python -m pytest tests/test_inbox_month_deck.py -q
"""
PART = open("src/app/templates/app/_conversation_list.html", encoding="utf-8").read()
PAGE = open("src/app/templates/app/conversations.html", encoding="utf-8").read()


def test_each_month_is_its_own_deck_with_the_today_pins():
    assert '<div class="convo-deck space-y-1.5">' in PART
    assert "{% set ti = [loop.index0, 3] | min %}{% set ri = [loop.length - 1 - loop.index0, 3] | min %}" in PART
    assert 'style="top: {{ 6 + ti * 12 }}px; bottom: {{ ri * 10 }}px; z-index: {{ loop.index }};"' in PART


def test_the_month_scroller_is_capped_and_has_no_top_or_bottom_padding():
    # Padding on the scroller shifts where the rows stick, and the restack's
    # pile tests (measured from the scroller's edge) then miss them — the
    # bottom pile showed its LAST email on the face in the first try.
    rule = PAGE[PAGE.index(".convo-deck { max-height: 62vh;"):][:260]
    assert "overflow-y: auto;" in rule and "isolation: isolate;" in rule
    assert "margin: 6px -12px 0; padding-left: 12px; padding-right: 12px; }" in rule
    assert "padding-top" not in rule and "padding-bottom" not in rule
    assert ".convo-deck > .convo-swipe { position: sticky; }" in PAGE
    assert ".convo-swipe.is-buried > .convo-row > * { filter: blur(3px); }" in PAGE


def test_the_restack_is_home_htmls_run_per_month_on_its_own_scroller():
    js = PAGE[PAGE.index("function restackDeck(stage) {"):][:4600]
    assert "if (r.bottom >= sr.bottom - botPin - 2) z = 300 - i;" in js
    assert "else if (r.top - sr.top <= topPin + 2) z = i + 1;" in js
    assert "else z = 600 + i;" in js
    assert "c.classList.toggle('is-buried', hidden / h > 0.1);" in js
    assert "list.querySelectorAll('.convo-deck').forEach(restackDeck);" in js
    # live.js swaps the list's innerHTML: each month keeps where it was scrolled
    assert "if (deck && scrolled[d.dataset.month]) deck.scrollTop = scrolled[d.dataset.month];" in js
    assert "list.addEventListener('toggle', schedule, true);" in js


def test_the_bottom_pile_sits_on_the_bottom_of_the_screen():
    # Kris 2026-09-24: "the bottom of the stack isn't at the bottom of the
    # page". <main> scrolls with the bottom bar under it: a deck on the first
    # screen runs to main's bottom edge; a later month gets a full screen.
    js = PAGE[PAGE.index("function sizeDecks() {"):][:900]
    assert "var h = top + 200 < mh ? mh - top - 10 : full;" in js
    assert "deck.style.maxHeight = Math.max(200, Math.round(h)) + 'px';" in js
    assert "function restackAll() { sizeDecks(); list.querySelectorAll('.convo-deck').forEach(restackDeck); }" in PAGE
