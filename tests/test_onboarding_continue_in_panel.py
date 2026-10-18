"""The wizard's Continue lives inside the assistant panel too (Kris 2026-09-04).

The onboarding co-pilot fills the step's form and then says "hit Continue" — but
on three of the steps the panel sits directly over the page's own Continue, so
the instruction pointed at a button the operator could not see. The panel now
renders the step's advance action itself and clicks the real button, which keeps
each form's own validation and submit behaviour.

What this pins:
  - every step that advances marks its button with data-onb-continue (the panel
    finds it by that attribute, never by "first submit in the form" — the voice
    step has four submits and only one of them advances)
  - the voice step marks BOTH of its advance buttons (review vs adjust); the
    panel picks whichever is visible
  - the widget renders a real button in the footer slot, and falls back to the
    old hint line when a step has no button on screen

Run: python -m pytest tests/test_onboarding_continue_in_panel.py -q
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src/app" / "templates" / "onboarding"


def _html(name: str) -> str:
    return (TPL / f"{name}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("step", ["interview", "industry", "voice", "knowledge", "data"])
def test_every_advancing_step_marks_its_continue(step):
    assert "data-onb-continue" in _html(step), step


def test_voice_marks_both_of_its_advance_buttons():
    # The learned-profile review and the Adjust form each advance the wizard, and
    # exactly one is ever on screen — the panel mirrors the visible one.
    assert _html("voice").count("data-onb-continue") == 2


def test_the_marker_sits_on_the_button_that_actually_advances():
    # knowledge's Finish submits the note form (formnovalidate) rather than being
    # a bare link — the marker must be on THAT button, not the "Add note" one.
    line = next(l for l in _html("knowledge").splitlines() if "data-onb-continue" in l)
    assert 'form="kb-note"' in line and 'value="continue"' in line


def test_panel_renders_the_action_not_just_a_hint():
    js = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    assert "pageContinueBtn" in js
    assert "[data-onb-continue]" in js
    assert "offsetParent !== null" in js          # the visible one wins
    # presses the real button — looked up at CLICK time: the knowledge step's
    # notes save refreshes <main> in place and replaces the Finish button, so
    # a reference taken when the panel rendered pointed at a detached node
    # (run #9, 2026-09-05: "Finish setup →" did nothing after a save)
    assert "var live = pageContinueBtn() || btn;" in js and "live.click()" in js
    # …and still degrades to words when no step button is on screen
    assert "Looks good? Review the form and hit Continue." in js


def test_the_mailbox_step_s_continue_is_a_panel_action_too():
    # Live 2026-09-04: after the connect landed, the panel offered nothing and the
    # page's Continue link sat under it.
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "src/app" / "templates" / "onboarding" / "mailbox.html").read_text(encoding="utf-8")
    assert 'href="/onboarding/voice" data-onb-continue' in tpl
