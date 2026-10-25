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


def test_continue_floats_over_a_fading_thread_foot():
    tpl = (Path(__file__).resolve().parents[1] / 'src/app/templates/_concierge.html').read_text(encoding='utf-8')
    block = tpl.split('{% if concierge_onboarding_step or setup_strip %}', 1)[1].split('{% endif %}', 1)[0]
    assert 'margin-top:calc(-1 * var(--bc-cta-h, 0px))' in block and 'background:transparent' in block and 'border-top:0' in block
    assert 'rgb(var(--c-bg-slate-50)/0)' in block and 'pointer-events:none' in block
    assert '#bc-messages { padding-bottom:calc(.75rem + var(--bc-cta-h, 0px)); }' in block
    assert 'panel.style.setProperty("--bc-cta-h", cta.offsetHeight + "px");' in JS


def test_voice_page_status_speaks_only_while_saving_or_after_a_failure():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'src/app/templates/onboarding/voice.html').read_text(encoding='utf-8')
    js = (root / 'src/app/static/voice_completion.js').read_text(encoding='utf-8')
    assert '<p id="voice-completion-status" role="status" aria-live="polite"></p>' in html
    assert 'if(status)status.textContent=saving?state.reason:failure;' in js
