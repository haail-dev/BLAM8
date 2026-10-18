"""The bottom strip's tapped door: its icon becomes the spinner.

Kris, 2026-09-20: "I want the buttons at the bottom row to turn into a
spinning loading logo when clicked. Only the clicked icon."

The generic tap spinner (src/app/static/nav.js, 2026-09-01) washes the whole
tapped control with a tint and puts a ring in the middle of it. On a door of
the bottom strip that is the wrong shape: the ring lands between icon and
label and the tint covers both. So a door marks its icon's holder
(data-nav-icon), nav.js puts the spinner inside THAT, and the CSS steps the
icon aside — the label stays, the other doors stay, nothing shifts.

Pins:
  * every door on the strip wraps its icon in a data-nav-icon holder, and
    only the icon (the CRM lock badge is not an icon, it is a badge);
  * the page carries the icon-spinner rules and nav.js knows the holder;
  * a strip door swaps as the finger LANDS and leaves two frames after the
    tap. A change made inside a link's click shares one task with the
    navigation that click starts, and iOS paints nothing of the old page
    once the navigation has begun — so a swap made in the click was real and
    never seen (Kris 2026-09-20, live: "the icon doesn't turn into the
    loading symbol"); every other control keeps its delay.

Run: python -m pytest tests/test_strip_tap_spinner.py -q
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import TenantCtx, require_onboarded
from app.main import app
from tests.test_home_dashboard import Env

NAV_JS = Path("src/app/static/nav.js").read_text(encoding="utf-8")


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _strip(html: str) -> str:
    start = html.index('aria-label="Primary"')
    return html[start:html.index("</nav>", start)]


def test_every_door_wraps_its_icon_in_a_holder(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling", "crm"})
    strip = _strip(client.get("/app/").text)
    doors = re.findall(r'<a href="[^"]+"', strip)
    holders = re.findall(r'<span class="relative inline-flex h-6 w-6" data-nav-icon><svg class="h-6 w-6', strip)
    assert len(doors) == 5 and len(holders) == 5, (doors, len(holders))
    # …and the holder closes right after the icon, so the label is outside it.
    assert strip.count("</svg></span>") == 5
    for label in ("Today", "Inbox", "Money", "CRM", "Diary"):
        assert re.search(r"</svg></span>\s*(<span[^>]*>.*?</span>\s*)?" + label, strip, re.S), label


def test_the_lock_badge_is_not_an_icon(client, monkeypatch):
    """A locked CRM door carries a small lock beside its icon. That is a
    badge, and it must not be swapped for a spinner."""
    Env(monkeypatch, modules={"scheduling"})          # no CRM → locked door
    strip = _strip(client.get("/app/").text)
    assert "data-crm-locked" in strip
    lock = strip[strip.index('<svg class="absolute top-1.5'):]
    assert not lock.startswith("<span"), "the badge must sit outside any holder"
    assert strip.count("data-nav-icon") == 5


def test_the_page_carries_the_icon_spinner_rules(client, monkeypatch):
    Env(monkeypatch)
    html = client.get("/app/").text
    assert "[data-nav-icon].nav-icon-busy > svg { visibility: hidden; }" in html
    assert "[data-nav-icon] > .nav-spin::before { display: none; }" in html
    assert "[data-nav-icon] > .nav-spin::after" in html
    assert "/static/nav.js" in html


def test_nav_js_lands_the_spinner_in_the_holder_when_there_is_one():
    assert "el.querySelector(':scope > [data-nav-icon]') || el" in NAV_JS
    assert "host.appendChild(spin)" in NAV_JS
    assert "host.classList.add('nav-icon-busy')" in NAV_JS
    # …and takes it away again on the way back in (pageshow → clear).
    assert "host.classList.remove('nav-icon-busy')" in NAV_JS
    assert "window.addEventListener('pageshow', clear)" in NAV_JS


def test_a_strip_door_swaps_as_the_finger_lands_and_leaves_two_frames_later():
    """The finger landing (pointerdown) swaps the icon, with no pointer-events
    lock yet — a locked door would hand the click to the bar under it. The
    click then holds the navigation back for two frames, so the swap is on
    screen before the browser starts leaving; a frame that never comes must
    not hold the door."""
    assert "document.addEventListener('pointerdown', function (e) {" in NAV_JS
    assert "if (a && navigates(a) && busy !== a) start(a, false);" in NAV_JS
    assert "if (lock) el.classList.add('nav-busy');" in NAV_JS
    assert "e.preventDefault();\n      disarm();" in NAV_JS
    assert "if (busy !== a) start(a, true);" in NAV_JS            # a keyboard tap has no finger
    assert "requestAnimationFrame(function () { requestAnimationFrame(go); });" in NAV_JS
    assert "setTimeout(go, 100);" in NAV_JS
    assert "location.assign(href);" in NAV_JS


def test_a_press_that_never_becomes_a_tap_puts_the_icon_back():
    """A finger that slides off, a scroll that steals the press, a long-press
    menu: the icon returns instead of spinning at a page that is not leaving."""
    assert "document.addEventListener('pointerup', function () {" in NAV_JS
    assert "armed = setTimeout(function () { armed = null; if (pressed()) clear(); }, 400);" in NAV_JS
    assert "['pointercancel', 'contextmenu'].forEach(" in NAV_JS
    assert "disarm();\n    if (busy) {" in NAV_JS                   # clear() drops the put-back too


def test_everything_off_the_strip_keeps_its_shape():
    """The delay that stops an instant navigation flashing a spinner, and the
    give-up that stops a cancelled one spinning forever, are untouched — and
    a link off the strip still navigates by itself, never through leave()."""
    assert "var DELAY = 120;" in NAV_JS
    assert "var GIVE_UP = 20000;" in NAV_JS
    assert "timer = setTimeout(function () { timer = null; show(el, el, true); }, DELAY);" in NAV_JS
    # The spinner still clears on a back-forward return. (Not "the file ends
    # with it": cc7d263 appended the send acknowledgement after it.)
    assert "window.addEventListener('pageshow', clear);" in NAV_JS
