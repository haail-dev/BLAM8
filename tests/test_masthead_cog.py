"""A section's settings live beside you, not two taps down a dropdown.

Kris, 2026-09-02: "add a setting cog left side of the Menu for the pages that
have dedicated settings, like CRM. Then remove the nav from Menu."

Each module's settings page was a row inside the masthead dropdown, so
"configure this" was a hunt on the very page you were configuring. The cog is
in the bar on every page of a section that has one; the settings rows left the
dropdown; the page-to-page navigation stayed, because taking that out would
leave pages with no route to them at all.
"""
import io
import glob
import os
import re

import pytest

from tests.test_settings_fold import client, _env      # noqa: F401  (fixture)
from tests.test_crm_routes import Env as CrmEnv


BASE = "src/app/templates/app/base.html"
COG = 'aria-label="Settings for this section"'


def _read(p):
    return io.open(p, encoding="utf-8").read()


def _map():
    """The one place a page is mapped to its settings page."""
    block = re.search(r"\{% set SETTINGS_FOR = \{(.*?)\}\s*%\}", _read(BASE), re.S)
    assert block, "the cog's map is gone"
    return dict(re.findall(r"'([a-z_]+)': '([^']+)'", block.group(1)))


# ── Where it appears ───────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/app/crm", "/app/crm/pipeline",
                                  "/app/crm/companies", "/app/crm/fields"])
def test_every_page_of_a_section_carries_its_settings_cog(client, monkeypatch, path):
    from app import dashboard
    monkeypatch.setattr(dashboard.setup_forms,'load',lambda ctx,page: {'page':page,'definitions':[]})
    CrmEnv(monkeypatch)
    html = client.get(path).text
    assert COG in html, f"{path} has no cog"
    href = re.search(r'href="([^"]+)"[^>]*' + re.escape(COG), html)
    assert href and href.group(1) == "/app/crm/settings", \
        f"{path} cog points at {href and href.group(1)}"


def test_a_page_with_no_settings_of_its_own_shows_no_cog(client, monkeypatch):
    """Absent, not pointed at the general Settings page — a cog that goes
    somewhere unrelated is a decoration that lies about where it goes."""
    _env(monkeypatch)
    assert COG not in client.get("/app/settings").text


@pytest.mark.parametrize("page", ["home", "conversations", "inbox", "drafts",
                                  "settings", "knowledge", "concierge",
                                  "agent_log", "mailboxes", "modules"])
def test_the_app_wide_pages_are_not_in_the_map(page):
    """Everything that is not one module's own area. Adding one of these later
    would put a cog on a page whose settings are the whole Settings page."""
    assert page not in _map()


def test_the_cog_sits_to_the_left_of_the_menu_button(client, monkeypatch):
    CrmEnv(monkeypatch)
    html = client.get("/app/crm").text
    assert html.index(COG) < html.index('id="menu-btn"')


def test_the_cog_is_a_real_target_not_a_bare_icon(client, monkeypatch):
    """It is the only control in the bar without a word beside it, so it has to
    be reachable by thumb and by screen reader."""
    CrmEnv(monkeypatch)
    tag = re.search(r"<a[^>]*" + re.escape(COG) + r"[^>]*>", client.get("/app/crm").text)
    assert tag, "the cog is not a link"
    assert "p-1" in tag.group(0), "no padding — the tap target is icon-sized"
    assert "title=" in tag.group(0), "no hover title"


def test_the_icon_uses_a_size_that_exists_in_the_built_css():
    """It shipped once as h-[18px] — an arbitrary value absent from tw.css,
    which rendered a 0x0 svg: a link you could neither see nor press."""
    tag = re.search(r"<a[^>]*" + re.escape(COG) + r".*?</a>", _read(BASE), re.S).group(0)
    svg = re.search(r'<svg class="([^"]+)"', tag).group(1)
    assert "[" not in svg, f"arbitrary size {svg} needs a Tailwind rebuild to exist"


def test_every_cog_target_is_a_page_the_app_serves():
    """A cog aimed at a 404 is worse than no cog."""
    from app.main import app as fastapi_app

    routes = {getattr(r, "path", None) for r in fastapi_app.routes}
    for page, target in _map().items():
        assert target in routes, \
            f"{page} cog points at {target}, which is not a route"


# ── What left the dropdown, and what stayed ────────────────────────────────
NAVS = sorted(glob.glob("src/app/templates/app/_*_nav.html"))
GONE = ["/app/crm/settings", "/app/crm/fields", "/app/diary/settings",
        "/app/diary/intake", "/app/invoices/settings", "/app/tms/pricing",
        "/app/fsm/pricing"]


def test_the_navs_exist_to_be_checked():
    assert len(NAVS) >= 5, NAVS


@pytest.mark.parametrize("target", GONE)
def test_the_settings_rows_left_the_dropdown(target):
    for p in NAVS:
        assert 'href="%s"' % target not in _read(p), \
            "%s still lists %s" % (os.path.basename(p), target)


def test_navigation_stayed(client, monkeypatch):
    """The rows that move people between pages are not settings, and several of
    them have no link anywhere else in the app."""
    CrmEnv(monkeypatch)
    html = client.get("/app/crm").text
    for target in ("/app/crm/companies", "/app/crm/pipeline", "/app/crm/tasks",
                   "/app/crm/sequences", "/app/crm/reports"):
        assert 'href="%s"' % target in html, "%s lost its only route" % target


@pytest.mark.parametrize("target", GONE)
def test_nothing_that_left_the_menu_became_unreachable(target):
    """The real risk of trimming a menu. Every destination taken out of the
    dropdown must still be linked from a page somebody can reach — the cog's own
    target counts, the nav partials do not."""
    homes = [p for p in glob.glob("src/app/templates/app/*.html")
             if not p.endswith("_nav.html") and 'href="%s"' % target in _read(p)]
    assert homes, "%s is now linked from nowhere" % target
