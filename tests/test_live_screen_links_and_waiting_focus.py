"""Kris's L6 run (16 Oct 2026): on the live voice screen the page links of the newest reply stand one above the other under the
mic line (not left-aligned at the bottom), the mic line sits just under the B, and a waiting item tapped in the column opens in
the Waiting box itself, without a Review button. The layout needs a browser (checked by hand in a preview); this pins the rules.
Run: python -m pytest tests/test_live_screen_links_and_waiting_focus.py -q
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE = (ROOT / "src/app/static/voice-gptlive.js").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")


def test_the_mic_line_sits_just_under_the_b_and_the_links_stand_stacked_below_it():
    assert "#voice-gptlive-root .vl-mic-copy { order:1; margin-top:-1.75rem; }" in LIVE                      # about an inch up
    assert "#voice-gptlive-root .vl-status-slot, #voice-gptlive-root .vl-transcript, #voice-gptlive-root .vl-reply { order:2; }" in LIVE
    assert "#voice-gptlive-root .vl-links { order:3;" in LIVE and "flex-direction:column; align-items:center;" in LIVE
    assert "#voice-gptlive-root .vl-links > div { display:flex; flex-direction:column; flex-wrap:nowrap; align-items:center;" in LIVE
    assert "#voice-gptlive-root .vl-controls { order:4; }" in LIVE
    # the container is in the orb column, after the mic line and before the controls
    assert '<p class="vl-mic-mode" hidden></p></div><div class="vl-links"></div><div class="vl-controls">' in LIVE
    assert "links:root.querySelector('.vl-links')" in LIVE
    # a tap target for each link, written after the shared face rules (no font-size in that block: the 14 Oct test)
    assert "#voice-gptlive-root .vl-links a::before { content:" in LIVE


def test_the_live_screen_lifts_the_links_out_of_the_cards_into_that_place():
    body = LIVE[LIVE.index("  function refreshStage() {"):LIVE.index("  function paintTasks() {")]
    assert "els.links?.replaceChildren?.();" in body
    assert "stage?.node?.querySelector?.('[data-cz-links]')" in body and "els.links.appendChild(links)" in body
    assert "row.setAttribute('data-cz-links', '');" in PANEL


def test_a_waiting_row_has_no_review_button_and_opens_the_card_in_the_waiting_box():
    assert "'Review'" not in SIDE and ".cz-side-review" not in SIDE
    assert "row('⏳', w.action_summary || w.summary || 'A change', '', function () { api.review(w, true); }, null, {})" in SIDE
    assert "var waitList = el('div', 'cz-side-list'), waitFocus = el('div', 'cz-side-focus');" in SIDE
    assert '.cz-side-box[data-focus="true"] .cz-side-list{display:none}' in SIDE
    assert '.cz-side-box[data-focus="true"] .cz-side-body{max-height:none;overflow:visible}' in SIDE
    assert "show: function (node, stub, note, opts) {" in SIDE and "(focus ? waitFocus : openBox.body).appendChild(wrap);" in SIDE
    assert "'\u2039 Back to the list'" in SIDE and "api.released(it.node)" in SIDE
    # the panel: the card made for a tap in the column is flagged, and goes to the box (typed) or the box and voice together
    assert "sideHost.show(node, null, '', { focus: true, hint: node._czHint || '' });" in PANEL and "sideHost.show(node, stub, note);" in PANEL
    assert "var inColumn = !!(w._czColumn && sideHost && sideHost.focusable());" in PANEL
    assert "p._czColumn = inColumn;" in PANEL and "card._czColumn = !!p._czColumn;" in PANEL and "voiceStage.cardNode._czColumn = inColumn;" in PANEL
    assert "if (held._czColumn && sideHost && sideHost.focusable()) {" in PANEL                    # a column exists: wide window or a phone's panel over the voice screen
    assert "if (!(held._czShown && !held.isConnected)) { held._czShown = true; sideHost.show(held, null, '', { focus: true }); }" in PANEL
    assert "released: function (node) {" in PANEL and "if (voiceStage.cardNode === node) clearVoiceCard(); else loadWaiting();" in PANEL


def test_the_column_replay_passes():
    out = subprocess.run(["node", str(ROOT / "tests/replays/concierge_sidebar_replay.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
