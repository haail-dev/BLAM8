"""Every page that shows a browser-tab icon shows the glass B.

The landing page's assistant moved to the coloured glass B, but tabs still
showed the old navy B: base.html linked the old SVG favicon and every other
layout linked blam8-logo.png as its icon. This file holds the tab icon in one
place, so a new layout can't quietly bring the old mark back:

  - every template that links a tab icon links both glass sizes, cache-busted;
  - no template uses the old favicon SVG or the navy logo as a tab icon;
  - the icon files exist as square, transparent PNGs at the sizes linked.

The home-screen (apple-touch-icon) and installed-app (manifest) icons are
deliberately out of scope here, as are the customer-facing pages (quote, job
sheet, portal, tracking) that have never carried a BLAM8 tab icon.

Run: python tests/run_offline.py tests/test_tab_icon.py -q
"""
import re
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "app" / "templates"
IMG = ROOT / "src" / "app" / "static" / "img"
SIZES = (32, 64)
ICON_LINK = re.compile(r'<link\s+rel="(?:shortcut )?icon"[^>]*>', re.I)


def _with_icon():
    return sorted(p for p in TEMPLATES.rglob("*.html")
                  if ICON_LINK.search(p.read_text(encoding="utf-8")))


def test_there_are_layouts_to_check():
    assert len(_with_icon()) >= 9


@pytest.mark.parametrize("path", _with_icon(), ids=lambda p: p.relative_to(TEMPLATES).as_posix())
def test_every_tab_icon_is_the_glass_b(path):
    html = path.read_text(encoding="utf-8")
    links = ICON_LINK.findall(html)
    for n in SIZES:
        want = (f'sizes="{n}x{n}" href="/static/img/blam8-tab-icon-{n}.png'
                '?v={{ asset_rev }}"')
        assert any(want in link for link in links), f"{n}px tab icon missing"
    assert not any("blam8-favicon.svg" in link or "blam8-logo.png" in link
                   for link in links), "old navy B used as a tab icon"


@pytest.mark.parametrize("n", SIZES)
def test_tab_icon_files_are_square_transparent_pngs(n):
    with Image.open(IMG / f"blam8-tab-icon-{n}.png") as im:
        assert im.format == "PNG"
        assert im.size == (n, n)
        assert im.mode == "RGBA"
        assert im.getpixel((0, 0))[3] == 0, "corner should be transparent"
