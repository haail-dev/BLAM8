"""Today, tighter (Kris 2026-10-16): less room above and below the name once the tagline has gone, cards that hug their text,
and an Emails-this-month row whose text fills its box. The layout needs a browser (checked by hand in a phone-width preview:
6px above the name, 36px from the name to the first card, cards 72px tall instead of 84); this pins the rules that make it so.
Run: python -m pytest tests/test_today_tighten.py -q
"""
import pathlib

HOME = (pathlib.Path(__file__).resolve().parents[1] / "src/app/templates/app/home.html").read_text(encoding="utf-8")


def test_less_room_above_the_name_and_between_it_and_the_first_card():
    # Today's wrapper only (it holds the deck): the shared page wrapper keeps its padding on every other page
    assert "main.app-sky > div:has(> .today-regions) { padding-top: .375rem; gap: .25rem; }" in HOME
    # the deck's own header row sits closer to the cards
    assert '<div class="flex items-center justify-between gap-2 pb-1 relative">' in HOME


def test_the_tagline_hands_its_row_back_as_it_fades_not_when_it_is_removed():
    assert 'el.style.margin = "0"; el.classList.add("today-tagline-gone");' in HOME
    assert (".deck-stage.space-y-4 > .today-tagline-gone ~ .deck, .deck-stage.space-y-4 > .today-tagline-gone ~ [data-my-notes] "
            "{ margin-top: 0; transition: margin .6s ease; }") in HOME


def test_cards_hug_their_text_top_and_bottom_and_keep_their_sides():
    assert ".deck-face.p-4 { padding-top: .625rem; padding-bottom: .625rem; }" in HOME
    assert HOME.count('<div class="deck-face p-4">') >= 1      # the markup still carries p-4: the sides stay 1rem


def test_the_emails_this_month_text_fills_its_box_which_is_still_as_wide_as_the_cards():
    assert '<a href="/app/usage" class="block px-2.5 pt-1 pb-2 hover:bg-slate-50 transition" data-tour="emails">' in HOME
    # the box itself is untouched (it is as wide as the cards)
    assert '<div class="today-drawer-sheet rounded-xl border border-slate-200 bg-white divide-y divide-slate-200">' in HOME
