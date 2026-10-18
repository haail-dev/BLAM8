"""A long typed reply is shown from its top (concierge_panel.js pinLongReply). The real function is
cut out of the source and run in node with stubbed layout. Run: python -m pytest tests/test_long_reply_top.py -q"""
import json
import pathlib
import shutil
import subprocess

import pytest

JS = (pathlib.Path(__file__).parent.parent / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
FN = JS[JS.index("    function pinLongReply(node) {"):]
FN = FN[:FN.index("\n    }\n") + 7]


def _run(node_h, view_h, scrolled=False, scroll_top=900, node_top=500, pad=16):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    code = f"""
var readerScrolled = {json.dumps(scrolled)};
var messages = {{scrollTop: {scroll_top}, clientHeight: {view_h}, getBoundingClientRect: () => ({{top: 100}})}};
global.getComputedStyle = (el) => el === messages ? {{paddingTop: '{pad}px', scrollPaddingTop: '0px'}} : {{scrollMarginTop: '0px'}};
var el = {{offsetHeight: {node_h}, getBoundingClientRect: () => ({{top: 100 + {node_top} - {scroll_top}}})}};
{FN}
pinLongReply(el);
console.log(messages.scrollTop);
"""
    return int(subprocess.run([node, "-e", code], capture_output=True, text=True, check=True).stdout)


def test_tall_reply_pins_its_top_under_the_normal_padding():
    assert _run(node_h=1500, view_h=600) == 500 - 16


def test_short_reply_keeps_scroll_to_bottom():
    assert _run(node_h=300, view_h=600) == 900          # untouched: scrollDown already ran


def test_a_long_reply_lands_at_its_top_wherever_the_reader_had_scrolled():
    # Kris 2026-10-16: "regardless". The old guard that left a reader who had scrolled by hand where they were is gone.
    assert "readerScrolled" not in JS
    assert _run(node_h=1500, view_h=600, scrolled=True) == 500 - 16


def test_keyboard_shrinks_the_area_so_a_medium_reply_counts_as_tall():
    assert _run(node_h=400, view_h=300) == 500 - 16      # clientHeight is the visible area, keyboard included


def test_wiring_typed_delivery_and_the_close_of_the_live_voice_screen_only():
    assert JS.count("pinLongReply(") == 3                # definition, the typed delivery, the voice close
    assert "pinLongReply(el);\n        loadWaiting();" in JS
    body = JS[JS.index("function refreshHistory(opts)"):JS.index("function refreshHistory(opts)") + 900]
    assert "if (opts && opts.pinNewest) {" in body and "if (now.length > answers) pinLongReply(now[now.length - 1]);" in body
    # history loaded on open (no option) never pins; the voice close asks for it on every refresh it makes
    assert JS.count("refreshHistory({pinNewest:true})") == 2


def test_conversation_links_clear_the_corners_and_the_foot_is_tight():
    root = pathlib.Path(__file__).parent.parent / "src/app"
    css = (root / "static/concierge_discovery.css").read_text(encoding="utf-8")
    base = (root / "templates/app/base.html").read_text(encoding="utf-8")
    assert "padding-inline:max(12px,env(safe-area-inset-left,0px)) max(12px,env(safe-area-inset-right,0px))" in css
    # the docked panel keeps the home-indicator inset (less its old 0.5rem trim), the page leans on the navy strip
    assert "#cz-modal #cz-root { padding-bottom: max(0.25rem, calc(env(safe-area-inset-bottom, 0px) - 1rem)); }" in base
    assert "#cz-modal.cz-kb #cz-root { padding-bottom: 0; }" in base
    assert "\n    #cz-root { padding-bottom: 0; }" in base
    assert "height: env(safe-area-inset-bottom, 0px);" in base      # the strip that owns the inset on the page
