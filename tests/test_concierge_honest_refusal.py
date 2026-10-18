"""A change that can't be set up is said plainly — and a quick "yes" is trusted.

Live 2026-09-23, kennels rig, iOS voice: "Bella's on 350g a day now, could you
update the record?" → the model proposed update_contact with a field the
customer record doesn't have → staging refused, but the reply still said
"I'll update that to 350g now. Want me to go ahead?" with the refusal tacked
on. The owner said "Yes." (flagged as clipped because it came the instant the
question ended) → "did you say yes? … the record's ready to go" — untrue.
"""
import time
from types import SimpleNamespace

from app import concierge, concierge_actions, concierge_routes


def _finish(monkeypatch, action, stage, *, persist=True):
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes, "resolve_business_links", lambda links: [])
    monkeypatch.setattr(concierge_actions, "stage", stage)
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: None)
    ctx = SimpleNamespace(tenant_id="t1", client=None, user_id="u1")
    result = SimpleNamespace(model="m", usage=None, links=[], action=action,
                             reply="I can see Bella's feeding says 300g — I'll update it to 350g now. Want me to go ahead?")
    return concierge_routes.finish_business_turn(
        ctx, result, question="Bella's on 350g now", trail=[], tools_called=[],
        t0=time.monotonic(), full_qa=True, scheduling_on=True, persist=persist)


def test_nonpersisted_turn_never_stages_a_proposal(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("discarded/nonpersisted turn must not stage")
    done = _finish(monkeypatch, {"kind": "update_contact"}, forbidden, persist=False)
    assert done["proposal"] is None and done["correction"] is None


def test_a_refused_change_replaces_the_promise(monkeypatch):
    def refuse(*a, **k):
        raise concierge_actions.ActionError("There's no 'feeding' field on the customer record.")
    done = _finish(monkeypatch, {"kind": "update_contact"}, refuse)
    assert done["proposal"] is None
    assert done["reply"] == done["correction"]
    assert done["correction"].startswith("Sorry — I can't do that: There's no 'feeding' field")
    assert done["correction"].endswith("Nothing has changed.")
    assert "go ahead" not in done["reply"]


def test_an_unexpected_failure_is_honest_too(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    done = _finish(monkeypatch, {"kind": "update_contact"}, boom)
    assert done["correction"] == "Sorry — I couldn't prepare that just now. Nothing has changed."
    assert done["reply"] == done["correction"]


def test_no_action_means_no_correction(monkeypatch):
    done = _finish(monkeypatch, None, lambda *a, **k: None)
    assert done["correction"] is None and "Want me to go ahead?" in done["reply"]


def test_the_voice_screen_says_the_correction():
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    block = js[js.index("if (finalPayload.correction) {"):][:200]
    assert "speak(finalPayload.correction);" in block


def test_a_bare_answer_is_never_treated_as_cut_off():
    bare = concierge_routes._bare_answer
    for said in ("Yes.", "yeah go ahead", "No thanks", "okay do it", "yep"):
        assert bare(said), said
    for said in ("", "yes but make it 400g", "Bella's on 350g a day",
                 "actually what about Rufus", "yes yes yes yes yes"):
        assert not bare(said), said
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    # (M34 2026-09: the note now also takes the asker, for the Inbox door.)
    # (The voice route builds it off the event loop: asyncio.to_thread(_voice_note, ...).)
    assert "_voice_note, ctx, body.clipped and not _bare_answer(question)," in src


def test_the_model_is_told_dependant_details_are_not_customer_fields():
    # Since update_dependant (same day): they have their own kind.
    src = concierge.build_business_prompt()
    assert "dependant's details are NOT customer fields — use update_dependant" in src
