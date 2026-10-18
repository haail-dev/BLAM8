"""The Concierge page's right-hand column (computers, wide window, full page only).
Run: python -m pytest tests/test_concierge_sidebar.py -q"""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")
STATUS = (ROOT / "src/app/static/concierge_status.js").read_text(encoding="utf-8")


def test_column_behaviour_in_a_fake_dom():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    result = subprocess.run([node, "tests/replays/concierge_sidebar_replay.mjs"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "ok" in result.stdout


def test_column_is_only_built_for_the_page_frame_on_a_computer():
    page = JS[JS.index("var pageApi = mount(pageRoot);"):]
    page = page[:page.index("return;")]
    assert "(hover: hover) and (pointer: fine)" in page        # the same computer test as "Concierge opens as"
    assert "mountSidebar(pageRoot, pageApi.sidebar)" in page
    # the page frame loads it, and so does the pop-up a phone opens (and only then: tests/test_column_on_phones.py)
    assert JS.count("import('/static/concierge_sidebar.js')") == 2


def test_column_does_not_change_server_markup_and_reuses_the_panels_own_paths():
    # created by JS, appended under #cz-root: the page and the modal still render one server template
    assert "root.appendChild(col)" in SIDE and "document.write" not in SIDE
    assert not list((ROOT / "src/app/templates/app").glob("*sidebar*"))
    # Review is the chip's own path: it never POSTs a confirm itself
    assert "confirm" not in SIDE.lower().replace("confirm and cancel", "") and "confirm" not in STATUS.lower()
    block = JS[JS.index("      sidebar: {"):JS.index("        onChange: function (fn) { root.addEventListener('cz:side', fn); },")]
    assert "else resurface(w);" in block and "/confirm" not in block
    # live: repainted from the events the panel already fires, no new timer or poll
    assert "root.dispatchEvent(new CustomEvent('cz:side'))" in JS and "sideChanged();" in JS and "sideChanged()" in JS[JS.index("function paintTaskRow"):JS.index("function taskListNode")]
    assert "setInterval" not in SIDE + STATUS
    # the column only READS: GETs of the two status routes, never a POST, never a card action
    assert "POST" not in SIDE + STATUS and "method:" not in SIDE + STATUS
    assert "'/concierge/business/status'" in STATUS and "'/concierge/business/around'" in STATUS
    assert SIDE.count("fetch(") == 1
    # width gate lives in the column's own CSS (1100px), plus the in-chat waiting dot steps aside only then
    assert "@media (min-width:' + WIDE_PX + 'px) and (hover:hover) and (pointer:fine)" in SIDE
    assert ".cz-waitdot-row{display:none}}" in SIDE


def test_menu_entry_is_the_full_page_and_phones_do_not_get_it():
    base = (ROOT / "src/app/templates/app/base.html").read_text(encoding="utf-8")
    entry = base[base.index('<a href="/app/concierge" class="cz-menu-pc'):]
    entry = entry[:entry.index("</a>")]
    assert "data-concierge-open" not in entry                  # a plain link: the full page, not the pop-up
    assert "@media (max-width: 767px) { .cz-menu-pc { display: none !important; } }" in base
    # the person icon still opens the pop-up
    assert 'id="concierge-btn"' in base and 'id="cz-modal"' in base


def test_review_steps_the_explore_board_aside_so_the_card_shows():
    """17 Oct: after Clear history the Explore actions board covers the thread, so a Review click added the
    card behind it and seemed to do nothing. Review (and the chips) now hide the board first."""
    body = JS[JS.index("    function resurface(w, btn) {"):]
    head = body[:body.index("if (w.type === 'link')")]
    assert "if (discovery && !(voiceLiveInstance && voiceLiveInstance.isOpen())) discovery.hide();" in head
    block = JS[JS.index("      sidebar: {"):JS.index("        focusTask: function (id)")]
    assert "if (discovery) discovery.hide(); var rc = proposalCardNode(w); rc._czColumn = !!inColumn; addCard(rc," in block


def test_cards_open_on_the_right_and_the_thread_keeps_a_one_line_note():
    """17 Oct: on the full-screen page the big things open in the column's 'Open now' area. The panel moves
    the SAME card node it built (so Confirm/Cancel keep their own checks); without the column it is as before."""
    body = JS[JS.index("    function addCard(node, note) {"):]
    body = body[:body.index("      return node;")]
    assert "if (sideHost && node._czColumn && sideHost.focusable()) {" in body and "} else if (sideHost && sideHost.active()) {" in body and "messages.appendChild(node);" in body
    assert "shown on the right" in JS
    # proposals, resurfaced drafts, recovery cards and new task lists all go through it
    assert "addCard(card, (p.summary || 'A change') + ' is ready, shown on the right.')" in JS
    assert "addCard(node, 'The email draft is ready, shown on the right.')" in JS
    assert JS.count("addCard(proposalCardNode(") == 2 and "var rc = proposalCardNode(w); rc._czColumn = !!inColumn; addCard(rc," in JS   # the third is flagged first
    assert "addCard(taskNode," in JS
    # the stub (not the card) is what history merging re-parents under the reply
    assert "(shownCard._czStub || shownCard).setAttribute('data-cz-owner-turn'" in JS
    # no confirm path is re-implemented in the column
    assert "/confirm" not in SIDE and "postJson" not in SIDE


def test_status_sections_read_the_ledger_and_keep_the_two_allowances_apart():
    # the sections of the status map (Kris 17 Oct, 1-7): each is its own box, empty ones stay hidden
    for title in ("Doing now", "Done today", "Links", "Context", "Around the business", "Limits"):
        assert "'" + title + "'" in STATUS
    assert "Typed questions today" in STATUS and "Live voice today" in STATUS    # two lines, never one merged figure
    # status reads ride the panel's own refresh event, spaced out, not a poll of their own
    assert "LOAD_GAP_MS" in STATUS and "status.schedule()" in SIDE
    # the panel exposes what the sections read, read-only
    for name in ("activity: function", "done: function", "context: function"):
        assert name in JS


def test_review_does_not_stack_chat_bubbles_or_duplicate_open_cards():
    """17 Oct live test: four 'isn't waiting any more' bubbles, rows that stayed, the same recovery card added
    twice, and a skipped draft still listed. Refusals show on the row; an open card is focused, not re-added."""
    assert "refused[w.proposal_id] = why; sideChanged();" in JS
    assert "function focusOpenCard(id)" in JS and JS.count("focusOpenCard(") >= 5
    # the draft's skip/send refresh the waiting list so a settled draft leaves it
    assert "paint('skipped'); loadWaiting();" in JS and "paint('sent'); loadWaiting();" in JS
    assert "CARD_TTL_MS = 300000" in SIDE and "w.delegated" in SIDE
    db = (ROOT / "src/app/db.py").read_text(encoding="utf-8")
    assert '"delegated": bool((r.get("payload") or {}).get("_delegation"))' in db


def test_voice_screen_leaves_room_for_the_column_without_touching_the_voice_files():
    assert "body.cz-side-wide.voice-live-open #voice-live-root.is-open .vl-stage" in SIDE and "#voice-gptlive-root.is-open .vl-stage{padding-right:22rem}" in SIDE
    # the layer and the stage run the full width (the water is one backdrop behind the cards): nothing shortens them
    assert ".vl-stage{right:22rem}" not in SIDE
    # what is placed from the right or across the width keeps clear of the column by the same 22rem
    assert "#voice-gptlive-root.is-open .cz-debrief{right:calc(22rem + .8rem)}" in SIDE
    assert "#voice-gptlive-root.is-open .cz-badges{right:calc(22rem + .8rem)}" in SIDE
    assert "#voice-gptlive-root.has-debrief .cz-badges{right:calc(22rem + .8rem + 2.75rem + .5rem)}" in SIDE
    assert "#voice-gptlive-root.is-open .vl-stage>.vl-top-strip" in SIDE and "#voice-gptlive-root.is-open .vl-allowance{left:calc(50% - 11rem)}" in SIDE
    # the layer behind runs the full width (one backdrop, the app bar does not show); only its stage keeps clear of the column
    assert "#voice-gptlive-root.is-open{right:22rem}" not in SIDE and "Search this column" not in SIDE and "cz-side-search" not in SIDE
    assert "voice-live-open #cz-root.cz-has-side .cz-side-box" not in SIDE        # the cards keep their own colour (Kris 2026-10-16)
    assert "classList.toggle('cz-side-wide'" in SIDE
    # the voice overlay (a body-level layer, so never the modal's) is shrunk by CSS from the column's file only
    assert "cz-side" not in (ROOT / "src/app/static/voice-live.js").read_text(encoding="utf-8")
    assert "cz-side" not in (ROOT / "src/app/static/voice-gptlive.js").read_text(encoding="utf-8")
    assert "cz-side" not in (ROOT / "src/app/static/voice-overlay-css.js").read_text(encoding="utf-8")


def test_the_column_hides_tasks_done_today_and_context_for_now_and_reads_the_wider_day_up_front():
    status = (ROOT / "src/app/static/concierge_status.js").read_text(encoding="utf-8")
    # Kris 2026-10-16: hidden, not removed. Tasks only while one runs; Context stays away; Done today only after a ledger lookup.
    assert "tasksBox.node.setAttribute('data-empty', tasks.length ? 'false' : 'true');" in SIDE
    assert "Nothing running" not in SIDE
    assert "export var SHOW_CONTEXT = false;" in status and "if (!SHOW_CONTEXT) { fill(ctxBox, []); return; }" in status
    assert "var LEDGER_LOOKUP = /awaiting your approval/i;" in status
    assert "if (!cardDone && !ran.some(function (l) { return LEDGER_LOOKUP.test(String(l)); })) { fill(doneBox, []); return; }" in status
    # the wider day is read with the status, so its dots show before the box is opened
    assert "loadAround();   // read with the status" in status
    assert "if (aroundBox.node.getAttribute('data-open') === 'true') loadAround();" not in status
