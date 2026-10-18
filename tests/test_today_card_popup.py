"""Tap a person's card on Today and it opens — every line in full.

Kris, 2026-09-21: "Currently clicking the blank space/Name on a chip does
nothing. I would like it to open a pop-up (like the Promises links), but the
pop-up just shows the full sentence for the lines that are being listed with
ellipses. So it's a small expansion before clicking a link for FULL detail.
Also make both pop-ups appear centre screen."

The name is a button and blank space on the card's face does the same; the
pop-up is the card's own <template> (nothing fetched) with every line in
full — a link row the same link, a promise row a hand-over to the row's own
promise pop-up. Both shells sit centre screen.

Run: python -m pytest tests/test_today_card_popup.py -q
"""
from pathlib import Path

from tests.test_home_dashboard import Env, client  # noqa: F401  (the fixture)
from tests.test_today_promise_popup import PROMISE, _crm

ROOT = Path(__file__).resolve().parents[1]
CARD_SRC = (ROOT / "src/app/templates/app/_card_popup.html").read_text(encoding="utf-8")


def _card(html, name):
    return next(c for c in html.split("<article") if name in c)


def _tpl(card):
    start = card.index("<template data-card-popup")
    return card[start:card.index("</template>", start)]


def test_the_name_is_the_button_that_opens_the_card(client, monkeypatch):
    Env(monkeypatch)
    card = _card(client.get("/app/").text, "Sarah")
    assert '<button type="button" data-card-open aria-haspopup="dialog"' in card
    assert 'title="a@x.com"' in card                                    # the address stays its tooltip
    assert "Sarah <span" in card[card.index("data-card-open"):]


def test_the_pop_up_holds_every_line_in_full_with_its_link(client, monkeypatch):
    Env(monkeypatch)
    card = _card(client.get("/app/").text, "Sarah")
    tpl = _tpl(card)
    assert 'data-card-title="Sarah · Acme Ltd"' in tpl
    assert 'href="/app/conversations/view?contact=a%40x.com"' in tpl    # the same link, for FULL detail
    assert "Draft ready" in tpl and "Quote please" in tpl
    assert "truncate" not in tpl and "break-words" in tpl                # unfolded here…
    rows = card[card.index("data-facts"):card.index("<template data-card-popup")]
    assert "truncate" in rows                                            # …still clipped on the card


def test_a_promise_line_hands_over_to_the_promise_pop_up(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    tpl = _tpl(_card(client.get("/app/").text, "Sarah Vance"))
    assert '<button type="button" data-card-promise="p1"' in tpl
    assert "You owe" in tpl and "Send the slab measurements · by Friday" in tpl
    assert "data-promise-open" not in tpl and "data-promise-card" not in tpl   # the row's own button opens it


def test_the_dot_sits_on_the_first_line_of_a_wrapped_row(client, monkeypatch):
    """Kris, 2026-09-21: "the word isn't inline with the bullet point".

    A line wraps in the pop-up (the card's own rows are clipped to one), and a
    centred dot floated to the middle of the paragraph, level with nothing. It
    sits on the first line's baseline here — beside the chip that opens the row.
    """
    _crm(monkeypatch, promises=[PROMISE])
    tpl = _tpl(_card(client.get("/app/").text, "Sarah Vance"))
    dots = [d for d in tpl.split("<span ") if "rounded-full" in d[:120]]
    assert dots, "every row still opens with its dot"
    for d in dots:
        assert "self-baseline" in d and "self-center" not in d


def test_both_pop_ups_sit_centre_screen(client, monkeypatch):
    Env(monkeypatch)
    html = client.get("/app/").text
    for mark in ("data-promise-modal", "data-card-modal"):
        assert html.count("<div " + mark) == 1
        shell = html[html.index("<div " + mark):]
        assert "fixed inset-0 z-50 flex items-center justify-center" in shell[:shell.index(">")]
        assert 'class="relative w-full max-w-md max-h-[80vh]' in shell[:600]
        assert "top-[10vh]" not in shell[:600]
    shell = html[html.index("<div data-card-modal"):]
    assert 'role="dialog"' in shell and 'aria-modal="true"' in shell
    assert "data-card-close" in shell and 'aria-label="Close"' in shell and "&times;" in shell


def test_the_script_opens_on_the_name_or_blank_space_never_a_link():
    assert 'e.target.closest("[data-card-open]")' in CARD_SRC
    assert ('e.target.closest("a, button, form, input, label, select, textarea, template, '
            '[data-card-modal], [data-promise-modal]")') in CARD_SRC
    assert 'face.classList.contains("deck-face-back")' in CARD_SRC       # the diary side stays a flip
    assert "String(window.getSelection())" in CARD_SRC                   # a swipe that selected text is not a tap
    assert "'[data-facts] [data-promise-open=' + JSON.stringify(id) + ']'" in CARD_SRC and "row.click()" in CARD_SRC
    home = (ROOT / "src/app/templates/app/home.html").read_text(encoding="utf-8")
    assert '{% include "app/_card_popup.html" %}' in home and "{{ card_popup(p) }}" in home
