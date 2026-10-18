"""Admin console burger menu — the old header link row became a dropdown.

Pins: all five console links live in the menu, the button carries the COMBINED
pending badge (per-page counts are invisible until the menu opens), and the
template still renders with the counts absent (a defended-against regression:
`+` on two Jinja Undefineds raises).

Run: python -m pytest tests/test_admin_nav_menu.py -q
"""
from app.templating import templates


def _render(**ctx):
    return templates.env.get_template("admin/base.html").render(**ctx)


def test_menu_holds_all_console_links_and_combined_badge():
    html = _render(ctx={"email": "op@blam8.com"}, pending_os_requests=2, unseen_requests=3)
    for href in ["/app", "/admin", "/admin/invites", "/admin/os-access", "/admin/partners",
                 "/admin/requests", "/admin/logout"]:
        assert f'href="{href}"' in html
    assert 'id="admin-menu-btn"' in html and 'id="admin-menu"' in html
    assert ">5</span>" in html          # combined 2+3 on the burger button
    assert "op@blam8.com" in html


def test_renders_without_counts_or_ctx():
    html = _render()
    assert 'id="admin-menu"' in html
    assert ">0</span>" not in html      # zero badge is suppressed, not shown
