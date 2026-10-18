"""The Phone link in the app menu (founder, 16 Oct 2026).

The telephone page (/app/phone) is owner-only and switched off by default; this
only makes it reachable from the Menu dropdown. The link sits next to the other
admin-only link (Scheduled plan) and is shown to the same people.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = (ROOT / "src/app" / "templates" / "app" / "base.html").read_text(encoding="utf-8")


def test_phone_link_is_in_the_menu_for_admins_only():
    line = next((l for l in BASE.splitlines() if 'href="/app/phone"' in l), "")
    assert line, "the Menu has no Phone link"
    assert "{% if people_admin %}" in line and "{% endif %}" in line
    assert ">Phone<" in line


def test_phone_link_is_inside_the_menu_dropdown():
    menu_start = BASE.index('<nav id="menu"')
    menu_end = BASE.index("</nav>", menu_start)
    assert menu_start < BASE.index('href="/app/phone"') < menu_end


def test_phone_link_is_one_link_only():
    assert BASE.count('href="/app/phone"') == 1


def test_the_phone_route_is_still_owner_guarded():
    src = (ROOT / "src/app" / "telephone_live_routes.py").read_text(encoding="utf-8")
    assert '@router.get("/app/phone")' in src
    assert "def phone(request: Request, ctx=Depends(_owner))" in src
