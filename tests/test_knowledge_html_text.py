"""URL-import text extraction — the nav-boilerplate + entity fix.

Real imports (littledogs.co.uk, a Wix site, 2026-09-02) showed every page's
first chunk front-loaded with the same site menu, the <title> text, and raw
entities (&nbsp; &quot;) — noise that every URL-importing tenant inherited.
_html_to_text now drops page chrome as whole elements (head/nav/header/footer/
aside plus script-like tags) and unescapes entities after the tag-strip.

Run: python -m pytest tests/test_knowledge_html_text.py -q
"""
from app.knowledge import _html_to_text

PAGE = """<!doctype html>
<html><head><title>FAQ | LittleDogs &quot;OrNotSoLittleDogs&quot;</title>
<style>body { color: red }</style><script>var x = "<b>not text</b>";</script></head>
<body>
<header id="SITE_HEADER"><nav>Home About &amp; Our Facility DayCare Book Now FAQ Contact</nav></header>
<main><h1>FAQ</h1>
<p>All male dogs must be neutered by age two.&nbsp; In-season females stay home.</p>
<p>Rest &amp; naps: minimum 1 hour at lunchtime.</p></main>
<aside>Follow us on Facebook</aside>
<footer>&copy; LittleDogs — licensed by Mendip Council</footer>
</body></html>"""


def test_page_chrome_is_dropped_whole():
    text = _html_to_text(PAGE)
    assert "Home About" not in text          # nav (inside header)
    assert "OrNotSoLittleDogs" not in text   # <title> text via <head>
    assert "Follow us" not in text           # aside
    assert "Mendip Council" not in text      # footer
    assert "color: red" not in text and "not text" not in text


def test_content_survives_with_entities_unescaped():
    text = _html_to_text(PAGE)
    assert "neutered by age two. In-season females stay home." in text  # &nbsp; folded
    assert "Rest & naps" in text
    assert "&amp;" not in text and "&nbsp;" not in text


def test_header_not_eaten_by_head_prefix():
    # <head> must not pair with </header> (or swallow to a later </head>):
    # 'head' is a prefix of 'header', so the element-strip needs the \b.
    html = "<header>menu</header><p>Body copy stays.</p>"
    assert _html_to_text(html) == "Body copy stays."


def test_unclosed_chrome_falls_back_to_tag_strip():
    # A never-closed <header> can't match as an element; its tags still strip,
    # so the text leaks (acceptable) but nothing after it is swallowed.
    html = "<header><p>menu</p><p>Body copy stays.</p>"
    assert "Body copy stays." in _html_to_text(html)
