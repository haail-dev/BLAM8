"""The card, after Kris's L8 run (16 Oct 2026): the follow-up card says where it will be kept; a card open on the right is not also
"waiting"; a card that has been dealt with is tight (the state under the title, the arrow at the end of the title line, the link
inside the details) and fades away, moving to Done today. The layout needs a browser (checked by hand in a 1280px preview through
the real panel code); this pins the rules that make it so.
Run: python -m pytest tests/test_concierge_card_polish.py -q
"""
import pathlib

from app import concierge_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")
SIDE = (ROOT / "src/app/static/concierge_sidebar.js").read_text(encoding="utf-8")
STATUS = (ROOT / "src/app/static/concierge_status.js").read_text(encoding="utf-8")


def test_a_date_reads_as_words_and_a_non_date_is_left_alone():
    assert concierge_actions._nice_date("2026-10-19") == "Monday 19 October 2026"
    assert concierge_actions._nice_date("2026-01-05") == "Monday 5 January 2026"
    assert concierge_actions._nice_date("soon") == "soon" and concierge_actions._nice_date("") == ""


def test_the_follow_up_card_says_the_task_the_day_and_where_it_will_be_kept(monkeypatch):
    contact = {"id": "11111111-1111-1111-1111-111111111111", "name": "Sam", "email": "sam@example.com"}
    monkeypatch.setattr(concierge_actions.db, "crm_get_contact", lambda cid, tid, client=None: contact)
    summary, payload, details, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact_id": contact["id"], "due_date": "2026-10-19", "title": "Call Sam"})
    assert summary == "Follow-up for Sam — due 2026-10-19"           # the headline and the stored date are as before
    assert payload["due_date"] == "2026-10-19"
    assert details == ["Task: Call Sam", "Due: Monday 19 October 2026", "Saved to: Sam's record in the CRM, as a task"]


def test_a_card_open_on_the_right_is_not_also_listed_as_waiting():
    assert "var waiting = (api.waiting() || []).filter(function (w) { return !openIds[w.proposal_id || w.log_id]; });" in SIDE
    assert SIDE.count("status.extras(waiting, openIds)") == 2
    assert "function extras(waiting, skip) {" in STATUS and "Object.keys(skip || {}).forEach(" in STATUS


def test_a_card_that_has_been_dealt_with_is_tight_and_the_link_waits_inside_the_details():
    assert "if (card.insertBefore) card.insertBefore(foot, top.nextSibling);" in JS                                   # the state sits under the title
    assert "if (card.hasAttribute && card.hasAttribute('data-collapsible')) linkNode.classList.add('cz-proposal-links');" in JS
    assert '.cz-proposal[data-collapsible][data-open="false"] .cz-proposal-links { display: none; }' in PANEL
    assert ".cz-proposal[data-state] .cz-proposal-foot { justify-content: flex-start; margin-top: 0.1rem; }" in PANEL
    # the arrow moves from the end of the title text to the end of the title line, so it cannot wrap onto a line of its own
    assert ".cz-proposal[data-state][data-collapsible] .cz-proposal-title::after { content: none; }" in PANEL
    assert '.cz-proposal[data-state][data-collapsible] .cz-draft-head::after { content: "\\203A";' in PANEL
    assert '.cz-proposal[data-state][data-collapsible][data-open="true"] .cz-draft-head::after { transform: rotate(90deg); }' in PANEL


def test_a_done_card_on_the_right_fades_away_and_done_today_shows_it():
    assert "if (card.closest && card.closest('.cz-side-item')) { agentNow.cardDone = true; sideChanged(); fadeAway(card, sideChanged, 5000); }" in JS
    assert "agentNow.cardDone = false; agentStep('Looking at your question');" in JS            # a new question starts clean
    assert "cardDone: agentNow.cardDone }; }," in JS
    assert "var cardDone = !!(c.api.activity && c.api.activity().cardDone);" in STATUS
    assert "if (!cardDone && !ran.some(function (l) { return LEDGER_LOOKUP.test(String(l)); })) { fill(doneBox, []); return; }" in STATUS


def test_the_state_of_a_card_just_dealt_with_shows_beside_open_now_not_inside_the_card():
    # Kris 2026-10-16: "Follow-up · done ✓" belongs on the box's header, next to the name
    assert "var note = el('span', 'cz-side-note');" in SIDE
    assert "head.appendChild(el('span', 'cz-side-title', title)); head.appendChild(note); head.appendChild(count);" in SIDE
    assert ".cz-side-title{flex:0 1 auto;" in SIDE and ".cz-side-note{flex:1;min-width:0;margin-left:.5rem;" in SIDE
    assert "return { node: b, body: body, count: count, note: note };" in SIDE
    assert "openBox.note.textContent = said(open);" in SIDE and "waitBox.note.textContent = said(focused);" in SIDE   # the Waiting box has its own
    assert "it.node.getAttribute('data-state') !== 'sent'" in SIDE
    # and inside the column the card's own state line is not shown for a done card (a card that did not run keeps its reason)
    assert '.cz-side-item .cz-proposal[data-state=\"sent\"] .cz-proposal-foot{display:none}' in SIDE
