"""The website reader folds link aliases before de-duplicating (first-walk
finding, 1 Oct 2026: nav "index.html" fetched the home page twice and the
eighth page fell off the cap).

Rule pinned here: fragment dropped; /index.html, /index.htm, /default.html
(any case) = their directory; "" and "/" are one path; trailing slash and host
case ignored; path case is KEPT, so "/About/" and "/about" are two pages.

Run: python -m pytest tests/test_kb_website_canonical.py -q
"""
from app import kb_website as kw

BASE = "https://example.com/"


def _html(*hrefs):
    return "".join(f'<a href="{h}">x</a>' for h in hrefs)


def test_index_alias_does_not_duplicate_home_and_cap_keeps_seven_distinct():
    pages = [f"{c}.html" for c in "abcdefg"]
    links = kw.discover_links(BASE, _html("index.html", *pages))
    assert len(links) == 7
    assert links == [f"https://example.com/{p}" for p in pages]
    capped = links[: kw.MAX_PAGES - 1]
    assert len(set(capped)) == 7 and BASE.rstrip("/") not in [u.rstrip("/") for u in capped]


def test_root_aliases_and_fragments_are_one_page():
    assert kw.discover_links(BASE, _html("/#top", "/", "", "/index.htm", "/DEFAULT.HTML",
                                         "https://EXAMPLE.com/")) == []
    # from an inner page the same rule applies to its own aliases
    inner = "https://example.com/team/"
    assert kw.discover_links(inner, _html("/team/index.html", "/team", "/team/#x")) == []


def test_trailing_slash_and_host_case_fold_but_path_case_does_not():
    out = kw.discover_links(BASE, _html("/about/", "/about", "https://EXAMPLE.com/about",
                                        "/About/", "/About"))
    # /about (three spellings) is one page; /About is another by the stated rule
    assert len(out) == 2
    assert {u.rstrip("/").rsplit("/", 1)[1] for u in out} == {"about", "About"}


def test_nested_index_folds_to_its_directory():
    assert kw.discover_links(BASE, _html("/services/index.html", "/services/")) == [
        "https://example.com/services/"]
