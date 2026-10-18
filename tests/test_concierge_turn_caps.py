"""The in-app assistant's question cap matches its own box (run #6, 2026-09-04).

The knowledge interview asks three trade-shaped questions a turn and the chat
box takes 4,000 characters (_concierge.html) — but the in-app routes kept the
public /ask cap of 1,000, so a full answer came back 422 and the widget said
"Sorry — I couldn't help just now" with no trace anywhere. The in-app turns
(onboard, help, fix, business) take what the box takes; the anonymous public
/ask and the voice chat keep the tight cap.

Run: python -m pytest tests/test_concierge_turn_caps.py -q
"""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import concierge_routes as cr

LONG = "x" * 3_900          # a real three-question answer
TOO_LONG = "x" * 4_001


def test_in_app_turns_take_what_the_box_takes():
    for model in (cr.OnboardRequest, cr.HelpRequest, cr.FixRequest, cr.BusinessRequest):
        kwargs = {"question": LONG}
        if model is cr.OnboardRequest:
            kwargs["step"] = "knowledge"
        if model is cr.FixRequest:
            kwargs["log_id"] = "abc123"
        model(**kwargs)                     # must not raise
        with pytest.raises(ValidationError):
            kwargs["question"] = TOO_LONG
            model(**kwargs)


def test_public_ask_keeps_the_tight_cap():
    cr.AskRequest(question="x" * 1000)
    with pytest.raises(ValidationError):
        cr.AskRequest(question="x" * 1001)


def test_public_ask_bounds_history_before_it_reaches_the_handler():
    turn = {"role": "user", "content": "x" * cr.MAX_TURN_CHARS}
    cr.AskRequest(question="x", history=[turn] * cr.MAX_HISTORY_TURNS)
    with pytest.raises(ValidationError):
        cr.AskRequest(question="x", history=[turn] * (cr.MAX_HISTORY_TURNS + 1))
    with pytest.raises(ValidationError):
        cr.AskRequest(question="x", history=[{"role": "user",
                                              "content": "x" * (cr.MAX_TURN_CHARS + 1)}])


def test_public_ask_rejects_oversized_json_before_model_work(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    response = TestClient(app).post(
        "/concierge/ask", content=b"{" + b" " * cr.MAX_PUBLIC_ASK_BODY_BYTES)
    assert response.status_code == 413


def test_public_ask_enforces_the_browser_session_allowance(monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    async def fake_answer(*_):
        return SimpleNamespace(reply="Answer", offer_signup=False, suggested_industry="",
                               ticket=None, lookup=None, model="test", usage={})

    monkeypatch.setattr(cr.concierge, "answer", fake_answer)
    monkeypatch.setattr(cr.usage, "record_anthropic", lambda *_, **__: None)
    cr._public_ask_sessions.clear()
    cr._public_ask_locks.clear()
    try:
        client = TestClient(app)
        status = client.get("/concierge/ask-limit")
        assert status.json() == {"limit": 20, "remaining": 20}
        for remaining in range(19, -1, -1):
            response = client.post("/concierge/ask", json={"question": "Hello"})
            assert response.status_code == 200
            assert response.json()["questions_remaining"] == remaining
        exhausted = client.post("/concierge/ask", json={"question": "One more"})
        assert exhausted.status_code == 429
        assert exhausted.json()["detail"]["code"] == "question_allowance_exhausted"
    finally:
        cr._public_ask_sessions.clear()
        cr._public_ask_locks.clear()


def test_box_and_route_agree():
    tpl = Path("src/app/templates/_concierge.html").read_text(encoding="utf-8")
    m = re.search(r'id="bc-input"[^>]*maxlength="(\d+)"', tpl)
    assert m, "the assistant box has lost its maxlength"
    assert int(m.group(1)) == cr.MAX_INAPP_QUESTION_CHARS


def test_in_app_turns_are_not_sliced_back_to_the_public_cap():
    """Run #7 (2026-09-05): the models took 4,000 but every handler then did
    `body.question.strip()[:MAX_QUESTION_CHARS]` — the knowledge answer was
    silently clipped at 1,000 mid-sentence and the assistant saved a note ending
    in "[incomplete — please complete the sentence]". Only the public /ask keeps
    the tight slice."""
    src = Path("src/app/concierge_routes.py").read_text(encoding="utf-8")
    assert src.count("[:MAX_QUESTION_CHARS]") == 2, "an in-app handler slices to the public cap"  # public /ask + the voice chat
    assert src.count("[:MAX_INAPP_QUESTION_CHARS]") == 4
