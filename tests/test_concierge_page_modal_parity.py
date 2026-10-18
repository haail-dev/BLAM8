"""The Concierge page (/app/concierge) and the modal (/app/concierge/panel) must stay ONE panel.

Same template, same context builder, one JS mount(). Only the frame differs (app shell vs the #cz-modal
overlay, keyboard fitting). Keyboard fitting and real layout need a device and are not covered here.
Run: python -m pytest tests/test_concierge_page_modal_parity.py -q
"""
import pathlib
import re
from html.parser import HTMLParser

from tests.test_home_dashboard import Env, client  # noqa: F401  (the fixture)

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
BASE = (ROOT / "src/app/templates/app/base.html").read_text(encoding="utf-8")
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "path"}


class _Root(HTMLParser):
    """Collects the markup of the element with id=cz-root, tags and text, depth-counted."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.done = False
        self.out = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth == 0 and dict(attrs).get("id") == "cz-root":
            self.depth = 1
        elif self.depth:
            if tag not in VOID:
                self.depth += 1
        else:
            return
        self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.depth and not self.done:
            self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.depth or self.done:
            return
        self.out.append("</%s>" % tag)
        self.depth -= 1
        if self.depth == 0:
            self.done = True

    def handle_data(self, data):
        if self.depth and not self.done:
            self.out.append(data)


def _root(html):
    p = _Root()
    p.feed(html)
    assert p.done, "no #cz-root in the markup"
    return re.sub(r"\s+", " ", "".join(p.out)).strip()


def test_page_and_modal_render_the_same_panel(client, monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling", "crm", "concierge"})
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [
        {"role": "user", "content": "pipeline?", "links": []},
        {"role": "assistant", "content": "1 prospect.", "links": []},
    ])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling", "crm", "concierge"})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth Logistics"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: True)
    page = client.get("/app/concierge")
    panel = client.get("/app/concierge/panel")
    assert page.status_code == 200 and panel.status_code == 200
    a, b = _root(page.text), _root(panel.text)
    assert "1 prospect." in a and "1 prospect." in b
    assert len(a) > 5000 and "cz-form" in a and "cz-discovery" in a   # not an empty match
    assert a == b, "the page and the modal render different panel content for one context"
    # Documented allowance: none today. The #cz-root subtree is identical; the frames differ only
    # outside it (app shell vs the #cz-modal overlay that wraps the fetched fragment).


def test_both_frames_use_one_mount_and_one_copy_of_send_and_scroll():
    assert JS.count("function mount(root)") == 1
    assert "mount(pageRoot)" in JS                       # the page frame
    assert "mount(root)" in JS                           # the modal frame loads the fetched panel through it
    assert JS.count("async function ask(") == 1          # one send
    assert JS.count("function scrollDown()") == 1        # one scroll-to-bottom
    assert JS.count("function pinLongReply(") == 1       # one long-reply pin


def test_layout_rules_cover_page_and_modal_under_cz_root():
    # the bottom spacing: a rule for the page's #cz-root and one for the modal's, both on #cz-root
    assert "\n    #cz-root { padding-bottom: 0; }" in BASE
    assert "#cz-modal #cz-root { padding-bottom:" in BASE
    # the links' side padding lives with the shared panel CSS, not a frame-specific rule
    css = (ROOT / "src/app/static/concierge_discovery.css").read_text(encoding="utf-8")
    assert ".cz-conversation-actions {" in css and "#cz-modal" not in css
    # pinLongReply is inside mount(): it acts on the mounted root's own #cz-messages for both frames
    assert JS.index("function mount(root)") < JS.index("function pinLongReply(") < JS.index("var btn = document.getElementById('concierge-btn');")
