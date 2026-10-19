"""Kris, 16 Oct 2026: on a phone the right-hand column is the right half of a two-page strip: swipe (or tap a task or Waiting) and it
snaps to one half, never resting between. The layout needs a browser (checked by hand at phone width on the real page code);
this pins the rules. Run: python -m pytest tests/test_column_on_phones.py -q
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
PAGE = (ROOT / "src/app/templates/app/concierge.html").read_text(encoding="utf-8")
SHARED = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")
BASE = (ROOT / "src/app/templates/app/base.html").read_text(encoding="utf-8")


def test_the_page_wraps_the_panel_in_a_strip_that_takes_no_part_in_layout_until_a_phone():
    assert '<div id="cz-pager" class="cz-pager">' in PAGE and ".cz-pager { display: contents; }" in BASE
    assert PAGE.index('id="cz-pager"') < PAGE.index('{% include "app/_concierge_panel.html" %}')
    # the pop-up uses the shared panel template alone: no strip there
    assert "cz-pager" not in SHARED


def test_a_phone_gets_a_snapping_two_page_strip_and_the_column_is_its_second_half():
    assert "export var PAGER_PX = WIDE_PX - 1;" in SIDE                     # any window too narrow for the side-by-side column
    assert ".cz-pager.cz-pager-on{display:flex;width:100%;height:100%;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;" in SIDE
    assert ".cz-pager.cz-pager-on>#cz-root{flex:0 0 100%;width:100%;max-width:none;margin:0;min-width:0;scroll-snap-align:start;scroll-snap-stop:always}" in SIDE
    assert ".cz-pager.cz-pager-on>.cz-side{display:flex;position:static;flex:0 0 100%;" in SIDE and "scroll-snap-stop:always}" in SIDE
    assert "(on ? pager : root).appendChild(col);" in SIDE
    assert "view.matchMedia('(max-width:' + PAGER_PX + 'px)')" in SIDE


def test_two_ways_across_and_the_strip_slides_when_something_opens_in_the_column():
    assert "cz-pager-tab" not in SIDE and "cz-voice-tab" not in SIDE
    assert "sideHost.revealWaiting();" in PANEL
    assert "var home = el('button', 'cz-side-home', '\\u2039 Concierge');" in SIDE
    assert "slideTo(Math.max(0, pager.scrollWidth - pager.clientWidth));" in SIDE
    assert "if (pager.scrollLeft === from && Math.abs(from - left) > 2) pager.scrollLeft = left;" in SIDE   # a dropped smooth scroll still goes there
    assert "      reveal();\n" in SIDE                                       # host.show slides across
    assert "focusable: function () { return pagerOn || !mq || mq.matches; }," in SIDE
    assert "active: function () { return pagerOn || !mq || mq.matches; }," in SIDE  # offered cards use the right-hand page on phones too


def test_the_columns_reads_wait_until_it_is_on_screen():
    assert "function visible() { return pagerOn ? revealed : (!mq || mq.matches); }" in SIDE
    assert "function ensureLoaded() { if (loaded || !visible()) return; loaded = true; status.load(); }" in SIDE
    assert "api.onChange(function () { paint(); if (visible()) status.schedule(); });" in SIDE
    assert "\n  status.load();\n" not in SIDE


def test_the_page_mounts_the_column_for_touch_and_phone_widths_too():
    assert "window.matchMedia('(hover: hover) and (pointer: fine), (pointer: coarse), (max-width: 767px)').matches" in PANEL


def test_the_column_replay_passes():
    out = subprocess.run(["node", str(ROOT / "tests/replays/concierge_sidebar_replay.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_pop_up_a_phone_opens_gets_the_strip_and_the_column_too():
    modal = PANEL[PANEL.index("  // ── the modal frame (every other app page)"):]
    assert "strip.id = 'cz-pager'; strip.className = 'cz-pager';" in modal and "while (slot.firstChild) strip.appendChild(slot.firstChild);" in modal
    assert "if (window.matchMedia('(max-width: 767px)').matches) {" in modal
    assert "import('/static/concierge_sidebar.js').then(function (m) { m.mountSidebar(root, api.sidebar); })" in modal
    assert "if (strip) strip.scrollLeft = 0;" in modal                       # it opens on the Concierge
    # the page frame still mounts it for a computer or a touch device; the pop-up on a computer still has none
    assert PANEL.count("import('/static/concierge_sidebar.js')") == 2


def test_over_the_live_voice_screen_the_column_is_a_panel_on_top_with_a_swipe():
    assert "body.voice-live-open .cz-pager.cz-pager-on>.cz-side{position:fixed;" in SIDE
    assert "transform:translateX(100%);visibility:hidden" in SIDE and "body.voice-live-open.cz-voice-side .cz-pager.cz-pager-on>.cz-side{transform:none;visibility:visible" in SIDE
    assert "if (voiceUp()) { floatSide(true); sideOverVoice(true); return; }" in SIDE   # reveal() brings the panel in, it does not slide the strip under the voice screen
    assert "if (Math.abs(dx) < Math.abs(dy) * 1.2) { drag = null; return; }" in SIDE   # mostly vertical is a scroll, never a pan
    assert "target = dx < 0 ? 1 : 0;" in SIDE and "if (target === 1) reveal(); else sideOverVoice(false);" in SIDE
    assert "if (voiceUp()) sideOverVoice(false); else slideTo(0);" in SIDE
    assert "body.classList.toggle('cz-pager-body', on)" in SIDE
    # a card opened from the Waiting box shows in the column wherever one exists (a phone included), not at the foot of the voice screen
    assert "if (held._czColumn && sideHost && sideHost.focusable()) {" in PANEL


def test_over_the_voice_screen_the_column_is_moved_onto_the_body_so_the_popup_cannot_stack_it_underneath():
    # Kris 2026-10-17: in the pop-up (#cz-modal, fixed, z-index 80) the swipe opened the column UNDER the voice screen (z-index 90).
    assert "body>.cz-side.cz-side-over{display:flex;position:fixed;" in SIDE and "z-index:93" in SIDE
    assert "body.cz-voice-side>.cz-side.cz-side-over,body.cz-panning>.cz-side.cz-side-over{visibility:visible}" in SIDE
    assert "function floatSide(on) {" in SIDE
    assert "if (voiceUp()) floatSide(true); else { sideOverVoice(false, true); floatSide(false); }" in SIDE   # it goes home with the voice screen
    assert "if (on && voiceUp()) floatSide(true);" in SIDE


def test_on_a_phone_the_voice_screen_and_the_column_are_one_canvas_with_the_water_running_under_both():
    # Kris 2026-10-17: not a panel dragged over the voice screen, and not a flat backdrop either: one canvas twice the phone's width, the voice screen the
    # left half and the column the right half, with the water running all the way across. --cz-pan (0..1) is how far across it is; everything that moves
    # is driven by the same number, with the individual `translate` property so the orb's own transform and transitions are untouched.
    assert "translate:calc((1 - var(--cz-pan,0)) * var(--cz-w,100vw)) 0" in SIDE                  # the column: the right half
    assert "{translate:calc(var(--cz-pan,0) * -1 * var(--cz-w,100vw)) 0}" in SIDE                  # the voice screen: the left half
    assert "var VOICE_ROOTS = ['#voice-live-root.is-open', '#voice-gptlive-root.is-open'];" in SIDE  # both voice screens
    # GPT-Live's water (canvas.vl-pond, and the glow .vl-ambient) is NOT excluded from the pan: it is made two screens wide and pans with the canvas.
    # Only the first screen's water (.vl-ripples) stays put behind both halves.
    assert "r + ' .vl-stage>:not(.vl-ripples):not(.vl-live)'" in SIDE and ":not(.vl-ambient)" not in SIDE
    assert "var VOICE_WATER = ['#voice-gptlive-root.is-open .vl-stage>.vl-pond', '#voice-gptlive-root.is-open .vl-stage>.vl-ambient'];" in SIDE
    assert "{width:calc(2 * var(--cz-w,100vw)) !important;max-width:none !important}" in SIDE        # !important: the canvas carries inline sizes
    assert "z-index:93;background:none;pointer-events:none;touch-action:pan-y;" in SIDE             # empty overlay space lets the water receive taps
    assert ".cz-side-over>*{pointer-events:auto}" in SIDE                                         # the column controls remain interactive
    assert "radial-gradient" not in SIDE and "#0b1220" not in SIDE                                 # no backdrop anywhere: the water shows through
    assert "doc.addEventListener('touchmove', function (e) {" in SIDE and "doc.addEventListener('touchcancel', function (e) { settle(e, true); }, { passive: true });" in SIDE
    assert "(d.p > 0.3 || v < -0.45)" in SIDE and "(d.p < 0.7 || v > 0.45)" in SIDE                # settles on the nearer half, or the way it was flicked
    assert "function glide(to) {" in SIDE and "paintPan(from + (to - from) * e);" in SIDE         # one clock for everything that moves
    assert "sideOverVoice(false, true)" in SIDE                                                    # a reset (the voice screen ended) is instant


def test_the_pond_lays_its_lake_out_from_its_canvas_so_it_can_be_wider_than_the_stage_on_a_phone():
    WATER = (ROOT / "src/app/static/water-core.js").read_text(encoding="utf-8")
    POND = (ROOT / "src/app/static/voice-pond.js").read_text(encoding="utf-8")
    assert "lakeFromCanvas: true," in POND                                                         # opt-in: nothing else that uses the water engine changes
    assert "const lakeBox = () => (options.lakeFromCanvas && canvas.clientWidth > 0 && canvas.clientHeight > 0 ? canvas : host);" in WATER
    assert "const box = lakeBox();" in WATER and "const r = box.getBoundingClientRect(); left = r.left; top = r.top;" in WATER
    assert "const hr = lakeBox().getBoundingClientRect(); left = hr.left; top = hr.top;" in WATER   # what it keeps clear is measured from the same place
    assert "if (options.lakeFromCanvas) ro?.observe(canvas);" in WATER                             # a canvas that changes size re-lays the lake out
    assert "function syncOrigin() {" in WATER and "function local(e) { syncOrigin();" in WATER      # a press measures the lake again: it can have been swiped across
    assert "host.clientWidth" not in WATER and "host.getBoundingClientRect" not in WATER.replace("lakeBox", "")
