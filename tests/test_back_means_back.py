"""The app bar's back link goes back when there is somewhere in the app to go.

Kris, 2026-09-20: "if I move from CRM into a booking via a link, the top
left Back button doesn't actually take me back to where I came from. It's
just back to whatever main page hosts it."

The link is "up" by default — each detail page names its parent (a job page
says ← Diary) — and stays so when there is no in-app history: a push, a
fresh tab, the installed app starting cold, a redirect back to the same
page. When you came from another page in the app, a tap goes back there,
and the label says so: the heading that page stored as it loaded (← Hannah),
or its section (← Inbox), or "Back". Decided in one place, the app bar, so
every page that sets a back link picks it up.

Run: python -m pytest tests/test_back_means_back.py -q
"""
from pathlib import Path

from tests.test_crm_routes import Env, client  # noqa: F401  (the fixture)

BASE = Path("src/app/templates/app/base.html").read_text(encoding="utf-8")
NAV_JS = Path("src/app/static/nav.js").read_text(encoding="utf-8")


def test_a_detail_page_still_names_its_parent_as_the_fallback(client, monkeypatch):
    Env(monkeypatch)
    html = client.get("/app/crm/contacts/ct1").text
    assert ('<a href="/app/crm" data-back-up="/app/crm" data-back-label="All contacts"' in html)
    assert "&larr; All contacts</a>" in html or "← All contacts</a>" in html
    assert '<h1 id="app-heading"' in html                    # what a page is called, for the next one's label


def test_the_bar_decides_from_the_referrer_and_only_inside_the_app():
    script = BASE[BASE.index("// Back means back"):BASE.index("var btn = document.getElementById('menu-btn')")]
    assert "new URL(document.referrer)" in script
    assert "ref.origin !== location.origin" in script         # never another site's page
    assert "/^\\/app(\\/|$)/.test(ref.pathname)" in script     # never login, marketing, admin
    assert "if (ref.pathname === location.pathname) {" in script   # a save redirecting to itself (?msg=saved)…
    assert "sessionStorage.getItem(BACKTO)" in script and "link.setAttribute('href', kept);" in script   # …goes where the page was opened from, in one tap
    assert "if (history.length < 2) return;" in script   # nowhere to go
    assert "history.back();" in script


def test_the_label_is_the_previous_pages_heading_then_its_section_then_back():
    script = BASE[BASE.index("// Back means back"):BASE.index("var btn = document.getElementById('menu-btn')")]
    assert "sessionStorage.setItem('blam8:crumb:' + here" in script
    assert "sessionStorage.getItem('blam8:crumb:' + from)" in script
    assert "label = label || section(ref.pathname) || 'Back';" in script
    for path, name in (("'/app': 'Today'", None), ("'/app/conversations': 'Inbox'", None),
                       ("'/app/crm': 'CRM'", None), ("'/app/diary': 'Diary'", None)):
        assert path in script


def test_the_tap_wears_the_wait_like_any_link():
    assert "window.blam8Wait = function (el) { start(el, true); };" in NAV_JS
    assert "if (window.blam8Wait) window.blam8Wait(link);" in BASE


def test_a_modified_click_is_left_to_the_browser():
    """Cmd-click opens the destination in a new tab; the href is set to it
    for exactly that reason."""
    script = BASE[BASE.index("// Back means back"):BASE.index("var btn = document.getElementById('menu-btn')")]
    assert "link.setAttribute('href', from);" in script
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in script
