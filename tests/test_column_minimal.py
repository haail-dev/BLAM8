"""Kris, 16 Oct 2026, on the merged column: too noisy. A waiting row is its title; the tap opens the card; Back puts the card away
(still waiting), not into the thread; the plain "prepared again" line is not said in the chat when the card opens in the box; the
Around box shows each group by name and count, then its items by name, then more about one. The layout needs a browser (checked by
hand in a preview); this pins the rules. Run: python -m pytest tests/test_column_minimal.py -q
"""
import pathlib
import re
import subprocess

from app import concierge_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")
STATUS = (ROOT / "src/app/static/concierge_status.js").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "src/app/concierge_routes.py").read_text(encoding="utf-8")


def test_a_waiting_row_is_only_its_title_and_what_cannot_be_opened_opens_its_reason_under_it():
    assert "waitList.appendChild(row('\u23f3', w.action_summary || w.summary || 'A change', '', function () { api.review(w, true); }, null, {}));" in SIDE
    assert "function expandRow(icon, line, lines) {" in SIDE and "under.hidden = !under.hidden;" in SIDE
    assert "waitList.appendChild(expandRow('?', w.action_summary || w.summary || 'A change'," in SIDE
    assert "waitList.appendChild(expandRow('?', l.line, [l.meta, l.detail]));" in SIDE
    assert "{ detail: known && known.detail }" not in SIDE                        # the ledger's long line is not on the tappable row
    assert ".cz-side-xmore[hidden]{display:none}" in SIDE


def test_a_card_opened_from_the_waiting_box_is_never_put_in_the_thread():
    add = PANEL[PANEL.index("    function addCard(node, note) {"):PANEL.index("      recheckOthers(node);")]
    assert "if (sideHost && node._czColumn && sideHost.focusable()) {" in add
    assert "sideHost.show(node, null, '', { focus: true, hint: node._czHint || '' });" in add      # no stub in the thread, so nothing to go back to
    park = PANEL[PANEL.index("    function parkVoiceCard() {"):PANEL.index("    // Take the card (and the last reply's links)")]
    assert "c._czColumn) return;" in park                                       # on the live screen too
    # Back removes the card from the box and tells the panel; it stays waiting and the list returns
    assert "toChat(it); paintOpen();\n    if (api.released) api.released(it.node);" in SIDE


def test_the_plain_prepared_again_line_is_not_said_in_the_chat_when_the_card_opens_in_the_box():
    assert "if (p.notice && !inColumn && !(voiceLiveInstance && voiceLiveInstance.isOpen())) bubble('assistant', p.notice);" in PANEL
    # a revised card still says why, in the box (a hint line under Back)
    assert "card._czHint = p._czColumn && p.notice && !SILENT_NOTICE.test(p.notice) ? p.notice : '';" in PANEL
    assert "if (opts && opts.hint) wrap.appendChild(el('p', 'cz-side-hint', opts.hint));" in SIDE
    # the client's pattern is the server's plain line, and not the revised one
    js_pattern = re.search(r"var SILENT_NOTICE = /(.*)/;", PANEL).group(1)
    assert re.search(js_pattern, concierge_actions.REPREPARED_SILENT)
    assert not re.search(js_pattern, concierge_actions.REPREPARED_REVISE)


def test_the_around_box_goes_group_then_items_then_more_about_one():
    assert "rows.push(c.row('', g.label + ' (' + g.count + ')', '', function () { S.aroundKey = g.key; paintAround(); }));" in STATUS
    assert "rows.push(c.expand('', title, [i.chip, i.who && i.text ? i.text : '']));" in STATUS
    assert "back.addEventListener('click', function () { S.aroundKey = null; paintAround(); });" in STATUS
    assert "expand: expandRow" in SIDE
    # the route gives every item of a group (the agenda holds six), not the first three
    assert '"items": [{"who": i["who"], "chip": i["chip"], "text": i["text"]} for i in g["items"]]}' in ROUTES


def test_the_column_replay_passes():
    out = subprocess.run(["node", str(ROOT / "tests/replays/concierge_sidebar_replay.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
