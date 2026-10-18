"""The deck's pile rests on the diary rail — no buffer under the stack.

Kris, 2026-09-20: "Now that we have more screen space, you can move the
bottom of the card stack lower, basically leave no buffer between the stack
and the bottom bar."

The page's frame keeps its side and top padding but none at the bottom, and
the deepest waiting card pins to the stage's own bottom edge (bottom: 0)
with the rest of the pile peeking 10px per card above it. The z-order script
measures the pile with the same offsets, so a card is still "waiting" at the
moment it lands.

Run: python -m pytest tests/test_today_deck_bottom.py -q
"""
import re

from tests.test_deck_namesakes import HANNAH_B, HANNAH_W, SARAH, _crm, _promise, client  # noqa: F401  (the fixture)

HOME = open("src/app/templates/app/home.html", encoding="utf-8").read()


def test_the_frame_has_side_and_top_padding_but_none_at_the_bottom(client, monkeypatch):
    _crm(monkeypatch, [SARAH], [_promise("p1", "c3", "Send the slab measurements")])
    html = client.get("/app/").text
    assert '<div class="max-w-5xl mx-auto w-full px-4 pt-4 sm:px-6 sm:pt-6 pb-0 h-full flex flex-col min-h-0 gap-3">' in html
    assert "p-4 sm:p-6 h-full" not in html


def test_the_deepest_card_pins_to_the_stages_bottom_edge(client, monkeypatch):
    _crm(monkeypatch, [HANNAH_B, HANNAH_W, SARAH],
         [_promise("p1", "c1", "Send Biscuit's vaccination dates"), _promise("p2", "c2", "Ring back about Biscuit"),
          _promise("p3", "c3", "Send the slab measurements")])
    html = client.get("/app/").text
    pins = re.findall(r'<article class="deck-card glass-card" style="top: (\d+)px; bottom: (\d+)px;', html)
    assert [b for _, b in pins] == ["20", "10", "0"]           # the pile: 10px a card, the last one on the rail
    assert [t for t, _ in pins] == ["6", "18", "30"]           # the top pile is as it was


def test_the_z_order_script_measures_the_pile_with_the_same_offsets():
    script = HOME[HOME.index("function restack()"):HOME.index("var queued = false;")]
    assert "var topPin = 6 + Math.min(i, 3) * 12;" in script
    assert "var botPin = Math.min(n - 1 - i, 3) * 10;" in script
    assert "6 + Math.min(n - 1 - i, 3)" not in HOME
