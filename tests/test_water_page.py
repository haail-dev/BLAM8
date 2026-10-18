"""Ripples behind the page: the wiring that can be read from the source. The behaviour (the switch, the look in each theme, taps, the
trail, sleeping, reduced motion, the voice screen holding it still) is proven in a real browser by tests/water_page_browser.py."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/app" / "static"
TEMPLATES = ROOT / "src/app" / "templates"
PAGE = (STATIC / "water-page.js").read_text(encoding="utf-8")
CORE = (STATIC / "water-core.js").read_text(encoding="utf-8")
HEAD = (TEMPLATES / "_theme_head.html").read_text(encoding="utf-8")
SETTINGS = (TEMPLATES / "app" / "settings.html").read_text(encoding="utf-8")


def test_the_page_module_uses_the_engine_with_the_revision_and_owns_no_loop():
    assert "await import('./water-core.js' + revision)" in PAGE
    assert not re.search(r"^\s*import\b", PAGE, re.M)
    assert "requestAnimationFrame" not in PAGE and "setInterval" not in PAGE and "getContext" not in PAGE


def test_a_failure_in_the_page_water_cannot_touch_the_page():
    assert "catch (_) { stop(); }" in PAGE
    assert "water?.destroy()" in PAGE


def test_it_follows_the_two_attributes_the_theme_script_keeps():
    assert "attributeFilter: ['data-water', 'data-theme']" in PAGE
    assert "voice-live-open" in PAGE and "water?.hold(held)" in PAGE
    assert "saveData" in PAGE


def test_the_engine_can_be_held_still():
    assert "function hold(on)" in CORE and "!held" in CORE


def test_the_theme_script_stamps_the_switch_before_paint_and_costs_nothing_when_off():
    assert "localStorage.getItem('blam8-water') === 'on'" in HEAD
    assert "setAttribute('data-water', water)" in HEAD
    assert "window.blam8ApplyWater" in HEAD
    # the module is fetched only on pages that opt in, only when the switch is on, and after the page is idle
    block = HEAD[HEAD.index("{% if water_page %}"):HEAD.index("{% endif %}", HEAD.index("{% if water_page %}"))]
    assert "water !== 'on'" in block and "requestIdleCallback" in block and "/static/water-page.js?v={{ asset_rev }}" in block


def test_the_app_and_onboarding_shells_opt_in_and_the_others_do_not_yet():
    for shell in ("app/base.html", "onboarding/base.html"):
        text = (TEMPLATES / shell).read_text(encoding="utf-8")
        assert text.index("{% set water_page = true %}") < text.index('{% include "_theme_head.html" %}'), shell
    for shell in ("admin/base.html", "portal.html"):   # phase 3
        assert "water_page" not in (TEMPLATES / shell).read_text(encoding="utf-8"), shell


def test_settings_has_the_switch_beside_the_appearance_picker():
    assert 'id="water-switch"' in SETTINGS and 'role="switch"' in SETTINGS
    assert SETTINGS.index('id="theme-picker"') < SETTINGS.index('id="water-switch"')
    assert "localStorage.setItem('blam8-water'" in SETTINGS and "window.blam8ApplyWater(" in SETTINGS


AMBIENT = (STATIC / "water-ambient.js").read_text(encoding="utf-8")
LANDING = (TEMPLATES / "landing.html").read_text(encoding="utf-8")


def test_the_page_water_can_sit_inside_an_element_the_page_marks():
    assert "[data-water-host]" in PAGE and "place().append(host)" in PAGE
    assert 'html[data-water="on"] body,html[data-water="on"] [data-water-host],html[data-water="on"] #cz-card{isolation:isolate}' in PAGE


def test_the_engine_can_be_fainter_or_stronger_and_can_ignore_the_pointer():
    assert "opacity: 1," in CORE and "* CFG.opacity" in CORE
    assert "options.pointer !== false" in CORE


def test_the_card_ripple_is_a_plain_module_that_moves_only_while_the_card_shows():
    assert "await import('./water-core.js' + revision)" in AMBIENT and not re.search(r"^\s*import\b", AMBIENT, re.M)
    assert "pointer: false" in AMBIENT and "IntersectionObserver" in AMBIENT
    assert "checkVisibility" in AMBIENT and "document.hidden" in AMBIENT
    assert "requestAnimationFrame" not in AMBIENT and "setInterval" not in AMBIENT and "getContext" not in AMBIENT
    assert "saveData" in AMBIENT and "catch (_)" in AMBIENT


def test_the_landing_turns_the_water_on_for_visitors_and_marks_its_deck_and_card():
    assert "document.documentElement.setAttribute('data-water', 'on')" in LANDING
    assert "/static/water-page.js?v={{ asset_rev }}" in LANDING and "/static/water-ambient.js?v={{ asset_rev }}" in LANDING
    assert "requestIdleCallback" in LANDING
    assert '<div class="deck-sky" data-water-host>' in LANDING
    card = LANDING[LANDING.index("<!-- Pro + Concierge -->"):]
    assert "data-water-card" in card.split("\n")[1]
    assert LANDING.count("data-water-card") == 2   # the comment-free attribute once, and the mention in the block comment


def test_the_concierge_popup_gets_its_own_water_while_it_is_open():
    # the pop-up covers the page with an opaque card, so the page's water cannot show there (Kris 2026-10-16)
    assert "const modalCard = () => document.getElementById('cz-card');" in PAGE
    assert "const modalOpen = () => { const m = document.getElementById('cz-modal'); return !!m && !m.classList.contains('hidden'); };" in PAGE
    assert "if (modalWater || !card || !wanted() || !modalOpen()) return;" in PAGE      # same switch, same data-saver rule
    assert "modalHost.style.cssText = 'position:absolute;inset:0;z-index:-1;pointer-events:none;';" in PAGE
    assert "surface: card," in PAGE                                                         # taps and the pointer on the card move it
    assert "catch (_) { stopModal(); }" in PAGE and "modalWater?.destroy()" in PAGE          # a failure never touches the pop-up
    assert "new MutationObserver(syncModal).observe(modal, {attributes: true, attributeFilter: ['class']});" in PAGE
    assert "water?.hold(held); modalWater?.hold(held);" in PAGE                             # still while the live voice screen is open
    assert "function sync() { stop(); start(); stopModal(); startModal(); }" in PAGE         # theme or switch change rebuilds both
    assert "window.blam8ModalWaterState = " in PAGE
