"""Fix mode carries the owner's words (round three, 1 Oct 2026, F25).

Seen live: a three-part correction became a never-rule; the rule was saved and
the reply redrafted with NO instruction, so two of the three parts were lost;
fix mode then ended and the next correction went to the help assistant, which
cannot redraft and answered with an issue report it called a "draft".

- /concierge/fix/apply takes an optional `direction` (the owner's own thread
  messages) and passes it to the redraft for EVERY kind.
- The widget sends it with every apply and keeps fix mode for the draft.
- The help prompt says plainly it cannot change a customer's reply.

Over-cap `direction` is REJECTED by the request model (HTTP 422); the widget
trims to the cap before sending, so the owner never meets that.
"""
from pathlib import Path

import pytest

from app.concierge import build_help_prompt
from tests.test_concierge_fix import Env, _apply, client  # noqa: F401  (fixtures/fakes)

JS = Path(__file__).resolve().parents[1].joinpath("src", "app", "static", "concierge.js").read_text(encoding="utf-8")
DIRECTION = "Say we cannot do Sundays.\nOffer Monday instead.\nDrop the price line."


def test_never_rule_saved_and_redraft_carries_direction(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="never", content="Never quote Sunday", direction=DIRECTION)
    assert r.status_code == 200
    assert env.never == ["Never quote Sunday"]
    assert env.instructions == [DIRECTION]


def test_knowledge_saved_and_redraft_carries_direction(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="knowledge", content="We are closed Sundays.", direction=DIRECTION)
    assert r.status_code == 200
    assert len(env.ingested) == 1
    assert env.instructions == [DIRECTION]


def test_requirement_saved_and_redraft_carries_direction(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="requirement", content="Vehicle registration", direction=DIRECTION)
    assert r.status_code == 200
    assert len(env.saved_fields) == 1
    assert env.instructions == [DIRECTION]


def test_redraft_content_wins_over_direction(client, monkeypatch):
    env = Env(monkeypatch)
    _apply(client, kind="redraft", content="Offer Monday", direction=DIRECTION)
    assert env.instructions == ["Offer Monday"]


def test_redraft_with_empty_content_uses_direction(client, monkeypatch):
    env = Env(monkeypatch)
    _apply(client, kind="redraft", content="", direction=DIRECTION)
    assert env.instructions == [DIRECTION]


@pytest.mark.parametrize("kind,content", [("never", "Never X"), ("knowledge", "A fact."),
                                          ("requirement", "Phone"), ("redraft", "")])
def test_no_direction_behaves_as_before(client, monkeypatch, kind, content):
    env = Env(monkeypatch)
    assert _apply(client, kind=kind, content=content).status_code == 200
    assert env.instructions == [""]          # old clients: rules/notes redraft bare, as today
    env2 = Env(monkeypatch)
    assert _apply(client, kind=kind, content=content, direction="   ").status_code == 200
    assert env2.instructions == [""]


def test_a_redraft_card_content_is_still_its_own_instruction(client, monkeypatch):
    env = Env(monkeypatch)
    _apply(client, kind="redraft", content="Offer him Sunday morning out of hours at £140")
    assert env.instructions == ["Offer him Sunday morning out of hours at £140"]


def test_direction_over_the_cap_is_rejected(client, monkeypatch):
    env = Env(monkeypatch)
    assert _apply(client, kind="never", content="Never X", direction="x" * 2000).status_code == 200
    r = _apply(client, kind="never", content="Never X", direction="x" * 2001)
    assert r.status_code == 422
    assert env.never == ["Never X"]          # the rejected call saved nothing more
    assert len(env.instructions) == 1


# ── the widget ────────────────────────────────────────────────────────────────
def test_widget_sends_the_owners_words_with_every_apply():
    assert "direction: fixDirection()" in JS
    assert 'm.role === "user"' in JS and "FIX_DIRECTION_MAX = 2000" in JS
    node = JS.index("function fixDirection")
    assert 'join("\\n")' in JS[node:node + 600]


def test_widget_keeps_fix_mode_after_an_apply():
    start = JS.index('fetch("/concierge/fix/apply"')
    end = JS.index("no.addEventListener", start)
    applied = JS[start:end]
    assert "clearThread" not in applied and "FIX_LOG_ID = null" not in applied
    assert "setFixLogId(null)" not in applied
    # the next message still goes to /concierge/fix with the same draft
    assert "if (FIX_LOG_ID) { await fixTurn(text, history); }" in JS
    # and fix mode survives a page load, ending only with the thread / the panel
    assert 'sessionStorage.getItem(KEY_FIX)' in JS
    # Review: closing the panel must NOT end fix mode (on a phone the panel covers
    # the draft, so the owner closes it to read the redraft and opens it again);
    # it is tied to the draft's page instead.
    assert "if (!open && FIX_LOG_ID) clearThread();" not in JS
    assert "saved.page === FIX_PAGE" in JS and "page: FIX_PAGE" in JS
    assert '"BLAM8 assistant — Fix a draft"' in JS


# ── the help assistant ────────────────────────────────────────────────────────
def test_help_prompt_never_describes_a_report_as_a_reply():
    p = build_help_prompt()
    assert ("You cannot change, redraft or send a customer's reply from here: when the owner asks for "
            "a drafted reply to be changed, say so plainly and tell them to press the thumbs-down on "
            "that draft, which opens the place where it can be redrafted; never describe a report card "
            "as a reply, a draft or a correction.") in p
