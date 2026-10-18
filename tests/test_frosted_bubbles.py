"""Kris, 16 Oct 2026: the Concierge's own chat bubbles are the original glass, a little cloudier (less see-through, so the ripples behind do
not fight the text), in every view type, with the view's own colour kept. No texture or pattern: ice crystals and faceted shards were both
tried and scrapped. The look needs a browser (checked by hand in a preview); this pins the rules. Run: python -m pytest tests/test_frosted_bubbles.py -q
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/app/static/concierge_frost.css").read_text(encoding="utf-8")
BASE = (ROOT / "src/app/templates/app/base.html").read_text(encoding="utf-8")


def test_it_is_linked_with_the_other_concierge_assets():
    discovery = '<link rel="stylesheet" href="/static/concierge_discovery.css?v={{ asset_rev }}">'
    frost = '<link rel="stylesheet" href="/static/concierge_frost.css?v={{ asset_rev }}">'
    assert BASE.index(discovery) < BASE.index(frost) < BASE.index('<script src="/static/concierge_panel.js')


def test_there_is_no_texture_or_pattern_and_the_sheet_is_tiny():
    assert "url(" not in CSS and "::before" not in CSS and "nth-" not in CSS and "svg" not in CSS.lower()
    assert len(CSS) < 3 * 1024
    assert not (ROOT / "scripts/gen_frost_crystals.py").exists()              # the generator of the scrapped patterns is gone


def test_the_agents_bubble_is_a_little_cloudy_and_keeps_each_views_colour():
    rule = CSS.split(".cz-message-assistant.cz-message-bubble {")[1].split("}")[0]
    assert "backdrop-filter: blur(6px)" in rule                                 # what is behind it (the ripples) is softened
    assert "color-mix(in srgb, var(--cz-source) 12%, rgb(var(--c-bg-white) / 0.66))" in rule   # tinted by the view's own colour, mostly the card colour
    assert "rgb(var(--c-bg-white) / 0.52) 88%" in rule                          # but not see-through: at least half opaque all the way across
    # the veil is the THEME's card colour, never a hard-coded white (Kris's page is dark: white would be a bright bubble with light text)
    assert "255, 255, 255" not in rule and "#fff" not in rule.lower()
    # the colour coding is the panel's own: the 3px edge and the per-view tokens are not set here at all
    assert "border" not in CSS.split("@media")[0] and "--cz-source:" not in CSS
    # (1,3,0) against the panel's (1,2,0), whatever the load order
    assert "#cz-root .cz-mode-section .cz-message-assistant.cz-message-bubble {" in CSS


def test_the_users_bubble_is_not_touched_it_only_shares_the_drop_shadow():
    rule = CSS.split("#cz-root .cz-mode-section .cz-message-user.cz-message-bubble {")[1].split("}")[0].strip()
    assert rule == "box-shadow: 0 2px 5px rgba(15, 23, 42, 0.06);"            # nothing else: its blue, text and edge are the panel's own
    panel = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")
    assert "#cz-root .cz-mode-section .cz-message-assistant:not([data-cz-pending]) {box-shadow:0 2px 5px rgba(15,23,42,.06);}" in panel   # the agent's, which it matches


def test_less_transparency_is_respected():
    assert "@media (prefers-reduced-transparency: reduce)" in CSS and "background: rgb(var(--c-bg-white) / 0.94)" in CSS
