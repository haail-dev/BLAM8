"""Pull down to refresh, on every page.

Kris, 2026-09-20: "Add the current refresh (Pull down) application wide
please. Any page that scrolls needs to scroll all the way to the top before
the refresh pull works, or the user can pull the top bar down to refresh (in
a message thread for instance). Use the refresh feature we built for the
landing page."

The landing page's pull (templates/landing.html, 2026-09-05) is inline and
moves only its video stage. The shared one (src/app/static/pull_refresh.js) is
the same gesture — same numbers, same words, a reload at the end — with the
whole shell moving instead, and it rides every shell that is not the landing.

Pins:
  * every shell loads the script; the landing, which draws its own, empties
    the block instead of loading it twice;
  * the numbers and the words are the landing's, read from the landing;
  * the pull only starts when every scroller under the finger is at its top,
    and the top bar is a handle that works from anywhere;
  * the things that own their own vertical drag, or are not the page, opt out:
    the thread's stage (pulling down there opens history), the concierge
    widget, the menu dropdown, the bottom strip and the dock rail;
  * the app shell says what moves and what the handle is;
  * the offline page, served from the service worker's cache, gets the script
    precached under the bare name it asks for.

Run: python -m pytest tests/test_pull_refresh.py -q
"""
import io
import re
import glob

SCRIPT = "src/app/static/pull_refresh.js"
LANDING = "src/app/templates/landing.html"
APP_BASE = "src/app/templates/app/base.html"
TAG = '<script src="/static/pull_refresh.js?v={{ asset_rev }}" defer></script>'


def _read(p):
    return io.open(p, encoding="utf-8").read()


def _shells():
    """Every template that is a whole document (owns <html>), bar the shared
    head fragment and the offline page (which has its own pin below)."""
    out = []
    for p in sorted(glob.glob("src/app/templates/**/*.html", recursive=True)):
        s = _read(p)
        if "<html" in s and "_theme_head" not in p and not p.endswith("offline.html"):
            out.append(p.replace("\\", "/"))
    return out


def test_every_shell_loads_the_pull():
    shells = _shells()
    assert len(shells) >= 13, shells
    missing = [p for p in shells if TAG not in _read(p)]
    assert not missing, "shells without the pull: " + ", ".join(missing)


def test_the_landing_keeps_its_own_pull_and_stands_the_shared_one_down():
    landing = _read(LANDING)
    assert "{% block pull_refresh %}{% endblock %}" in landing
    assert "function onPullRefresh()" in landing, "the landing's own pull is gone"
    base = _read("src/app/templates/base.html")
    assert "{% block pull_refresh %}" + TAG + "{% endblock %}" in base
    js = _read(SCRIPT)
    assert "data-pull-refresh-own" in js


def test_the_numbers_and_the_words_are_the_landings():
    landing, js = _read(LANDING), _read(SCRIPT)
    m = re.search(r"var PULL_MAX = (\d+), PULL_FIRE = (\d+);", landing)
    assert m, "the landing's thresholds moved"
    assert f"var PULL_MAX = {m.group(1)}, PULL_FIRE = {m.group(2)}, RESIST = 0.5;" in js
    assert "dy * 0.5" in landing, "the landing's resistance changed"
    for words in ("'Pull to refresh'", "'Release to refresh'", "'Refreshing'"):
        assert words in landing and words in js, words
    assert "window.location.reload()" in landing and "window.location.reload()" in js


def test_the_pull_stops_where_the_words_change():
    """Kris, 2026-09-20: "lock the pull down distance to just as the text
    changes. It doesn't need to drag any further down." Pulling past the
    point of no return showed nothing new, so the shell stops there — on
    the landing too, since it is the same pull."""
    for src in (LANDING, SCRIPT):
        m = re.search(r"var PULL_MAX = (\d+), PULL_FIRE = (\d+)", _read(src))
        assert m and m.group(1) == m.group(2), src


def test_the_pull_waits_for_every_scroller_to_be_at_its_top():
    js = _read(SCRIPT)
    walk = re.search(r"function atTop\(el\) \{(.*?)\n  \}", js, re.S)
    assert walk, "no atTop walk"
    assert "el.parentElement" in walk.group(1) and "scrollTop > 1" in walk.group(1)
    assert "window.scrollY" in walk.group(1)
    # …checked both when the finger lands and again as it moves.
    assert js.count("!atTop(") >= 2


def test_the_top_bar_is_a_handle_that_works_from_anywhere():
    js = _read(SCRIPT)
    assert "closest('[data-pull-bar]')" in js
    assert "if (!fromBar && !atTop(t)) return;" in js


def test_a_sideways_drag_is_not_a_pull():
    """The inbox rows swipe, the dock rail scrolls sideways."""
    js = _read(SCRIPT)
    assert "Math.abs(dx) > SLOP && Math.abs(dx) > Math.abs(dy)" in js


def test_the_browsers_own_pull_is_off_so_there_is_one_pull():
    js = _read(SCRIPT)
    assert "html, body { overscroll-behavior-y: none; }" in js
    assert "[data-pull-root] > main { overscroll-behavior-y: none; }" in js


def test_the_app_shell_says_what_moves_and_what_the_handle_is():
    base = _read(APP_BASE)
    assert '<div class="h-full flex flex-col" data-pull-root>' in base
    assert '<header id="app-bar"' in base
    bar = base[base.index('<header id="app-bar"'):]
    assert "data-pull-bar" in bar[:bar.index(">")]
    # The gap the pull opens is the body's canvas (slate-100, dark in dark mode),
    # not the html's navy: the label keeps the landing's slate ink.
    assert "--pull-refresh-ink" not in base
    assert TAG in base


def test_what_owns_its_own_drag_or_is_not_the_page_opts_out():
    js = _read(SCRIPT)
    assert """closest('[data-pull-refresh="off"]')""" in js
    off = 'data-pull-refresh="off"'
    base = _read(APP_BASE)
    for marker in ('<nav id="menu"', 'aria-label="Diary rail"', 'aria-label="Primary"'):
        tag = base[base.index(marker):]
        assert off in tag[:tag.index(">")], marker
    stage = _read("src/app/templates/app/_thread.html")
    tag = stage[stage.index('<div id="thread-stage"'):]
    assert off in tag[:tag.index(">")], "the thread's stage pulls history, not the page"
    cx = _read("src/app/templates/_concierge.html")
    tag = cx[cx.index('<div id="blam8-concierge"'):]
    assert off in tag[:tag.index(">")]


def test_the_offline_page_has_the_pull_from_the_cache():
    offline = _read("src/app/templates/offline.html")
    assert '<script src="/static/pull_refresh.js" defer></script>' in offline
    sw = _read("src/app/static/sw.js")
    pre = sw[sw.index("const PRECACHE = ["):sw.index("];", sw.index("const PRECACHE = ["))]
    assert "'/static/pull_refresh.js'" in pre


def test_a_reload_that_never_comes_gives_the_page_back():
    js = _read(SCRIPT)
    assert "var STUCK = 10000;" in js
    assert "e.persisted" in js, "a page back from the bfcache must not be mid-pull"


def test_the_concierge_bar_pulls_its_card_and_reopens_after_the_reload():
    """Kris 2026-09-23: the Concierge's bar refreshes too. The modal opts out
    of the page's pull, but its own bar is a handle inside it: the card comes
    down, and the reload lands back in the Concierge."""
    js = _read(SCRIPT)
    assert "if (off && !(bar && off.contains(bar))) return;" in js
    assert "handle.getAttribute('data-pull-moves')" in js
    assert "handle.getAttribute('data-pull-reopen')" in js
    base = _read(APP_BASE)
    tag = base[base.index('<header id="cz-head"'):]
    tag = tag[:tag.index(">")]
    assert 'data-pull-bar data-pull-moves="#cz-card" data-pull-reopen="concierge"' in tag
    modal = base[base.index('<div id="cz-modal"'):]
    assert 'data-pull-refresh="off"' in modal[:modal.index(">")]   # the page's pull stays out
    panel = _read("src/app/static/concierge_panel.js")
    assert "location.hash === '#concierge'" in panel                 # ...and reopens it
