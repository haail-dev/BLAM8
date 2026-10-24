"""The assistant's onboarding Continue stands alone: no hint line under it."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / 'src/app/static/concierge.js').read_text(encoding='utf-8')


def test_ready_continue_has_no_hint_line_under_it():
    assert 'Review the visible form before saving your choices.' not in JS
    assert 'Your Knowledge is saved. Continue when you’re ready.' not in JS


def test_disabled_voice_continue_has_no_reason_line_under_it():
    branch = JS.split("if(state.journey && state.journey.current==='voice'){", 1)[1].split('cta.classList.remove("hidden");', 1)[0]
    assert "aria-describedby','voice-completion-status'" in branch
    assert 'else cta.appendChild(notice(state.reason' in branch
