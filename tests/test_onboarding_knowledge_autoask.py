"""The onboarding knowledge step opens with the interview (hot tub run
2026-09-04: the static greeting waited for a nudge)."""
from pathlib import Path

from app import knowledge_coverage as kc


def test_the_page_carries_the_ask_and_the_panel_sends_it():
    html = Path("src/app/templates/onboarding/knowledge.html").read_text(encoding="utf-8")
    assert 'data-onb-autoask="{{ coverage_ask }}"' in html
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    assert "function maybeOnbAutoAsk" in js
    assert 'ONB_STEP !== "knowledge"' in js
    assert "onboardTurn(text, log.slice(-MAX_TURNS))" in js
    src = Path("src/app/onboarding.py").read_text(encoding="utf-8")
    # the website route is offered by chat only while no site is on file — one
    # given at step 1 is read on arrival instead (2026-09-13)
    assert "coverage_ask = ''" in src  # Optional gaps no longer start a generic interview.


def test_the_ask_covers_the_missing_kinds_three_at_a_time():
    cov = kc.coverage([])
    text = kc.ask_text(cov["missing"])
    assert "Interview me" in text
    assert "What do you charge" in text            # prices first
    assert "Ask about these first" in text


def test_the_ask_is_stamped_only_once_the_assistant_spoke_and_the_turn_retries():
    """Run #9 (2026-09-05): a deploy restart killed the knowledge step's opening
    turn — the owner saw the apology, and because the once-per-session stamp
    had landed BEFORE the turn, the interview could never start again. The
    stamp now follows a successful turn, and the POST retries once."""
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    block = js.split("function maybeOnbAutoAsk")[1].split("// ── proactive notifications")[0]
    assert "if (ok) { try { sessionStorage.setItem(KEY_ONB_AUTOASK, ONB_STEP); }" in block
    # nothing stamps before the turn resolves
    before_turn = block.split("onboardTurn(text")[0]
    assert "sessionStorage.setItem(KEY_ONB_AUTOASK" not in before_turn
    turn = js.split("async function onboardTurn")[1].split("function navLinkNode")[0]
    assert 'postJsonWithRetry("/concierge/onboard"' in turn
    assert "return spoke;" in turn
    assert "Send that again in a moment" in turn        # the apology offers a way back
    retry = js.split("async function postJsonWithRetry")[1].split("async function onboardTurn")[0]
    assert "res.status >= 500 && attempt < 2" in retry   # 5xx and network errors retry; 4xx do not
    help_turn = js.split("async function helpTurn")[1].split("async function fixTurn")[0]
    # Durable help persists proposals: a lost response must not replay a write.
    # Legacy read-only help and onboarding keep their existing retry behaviour.
    assert 'setupCards ? window.Blam8SetupCards.request : postJsonWithRetry' in help_turn
    assert 'sendHelp("/concierge/help"' in help_turn
