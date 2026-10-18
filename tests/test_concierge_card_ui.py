"""The proposal card, from Kris's L1 and L2 runs (16 Oct 2026): sleek and expandable, the real reason when it did
not run, a replaced card goes away, and the column holds it straight in the box.
The layout needs a browser (checked by hand in a 1280px preview); this pins the rules that make it so.
Run: python -m pytest tests/test_concierge_card_ui.py -q
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")


def test_the_card_shows_one_line_and_opens_on_a_tap_anywhere_but_the_buttons():
    assert "card.setAttribute('data-collapsible', '');" in JS and "card.setAttribute('data-open', 'false');" in JS
    assert "if (e.target.closest && e.target.closest('button, a, textarea, input, label, select')) return;" in JS
    assert "body.setAttribute('aria-expanded', open ? 'true' : 'false');" in JS            # keyboard and screen readers too
    assert "if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDetail(); }" in JS
    # the details stay shut until opened; a card with no details is not made tappable
    assert '.cz-proposal[data-collapsible][data-open="false"] .cz-proposal-detail { display: none; }' in PANEL
    assert "if ((p.details && p.details.length) || p.type === 'recovery') {" in JS
    assert '.cz-proposal[data-collapsible][data-open="true"] .cz-proposal-title::after { transform: rotate(90deg); }' in PANEL


def test_a_card_that_did_not_run_says_why():
    assert "expired: 'expired. A card must be confirmed within 5 minutes.'," in JS
    assert "failed: 'the change did not go through.'" in JS
    assert "NOT_RUN_WHY[why] ? 'Not run — ' + NOT_RUN_WHY[why]" in JS
    # the reason is the server's own state (expired, failed, ...), passed on from the outcome
    assert "finish(data.state === 'cancelled' ? DECLINED_LINE : data.summary, data.ok, data.link, data.results, data.state);" in JS
    assert "function finish(text, okOutcome, link, results, why) {" in JS


def test_a_card_replaced_by_a_newer_one_goes_away_instead_of_sitting_beside_it():
    assert "var SUPERSEDED_LINE = 'Replaced by a newer card — nothing changed.';" in JS
    assert "if (!okOutcome && (text === DECLINED_LINE || why === 'superseded')) {" in JS
    assert "card.textContent = why === 'superseded' ? SUPERSEDED_LINE : text;" in JS
    # a new card asks the others where they stand (the server keeps one live card per business)
    assert "function recheckOthers(fresh) {" in JS and "checkProposal(otherId, false).then(" in JS
    assert JS.index("sideHost.show(node, stub, note);") < JS.index("recheckOthers(node);\n      return node;")
    assert JS.count("function recheckOthers(") == 1


def test_the_column_holds_the_card_straight_in_the_box():
    assert ".cz-side-item>.cz-draft{border:0;border-radius:0;background:none;box-shadow:none;padding:0}" in SIDE


def test_the_thread_keeps_no_note_about_a_card_open_on_the_right():
    assert ".cz-side-stub{display:none}" in SIDE
