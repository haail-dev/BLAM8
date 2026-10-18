"""A typed yes with a card waiting, one card per request, and no false "added" (Kris 2026-10-16, L1 and L2).

- A bare yes typed while a card is waiting says where the card is: no model turn, no second card.
- The same request twice reuses the waiting card instead of staging an identical one beside it.
- A reply written alongside a staged card never says the change was made.
- "What have I signed off?" is answered from three groups: signed off, approved by voice, went through (no tap).
Run: python -m pytest tests/test_concierge_typed_yes.py -q
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import concierge, concierge_routes, db
from tests.test_concierge_business import client  # noqa: F401  (the fixture)
from tests.test_concierge_ledger import U2, by_id, card, receipt, world  # noqa: F401  (the fixtures)

import pytest


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """The business route allows 10 questions a minute per caller; in one test run other files have used some."""
    from app import security
    security._BUCKETS.clear()
    yield
    security._BUCKETS.clear()


_USAGE = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_bare_yes_words_are_matched_like_the_panels_own_list():
    yes = ["Yes", "yes please", "OK", "okay, do it", "Yeah sure", "confirm", "yes.", "That's right, go ahead", "Y"]
    no = ["", "yes but move it to Friday", "Add a note to Leah", "what's waiting", "maybe", "yes yes yes yes yes yes yes", "no"]
    assert all(concierge_routes._is_bare_affirmation(t) for t in yes)
    assert not any(concierge_routes._is_bare_affirmation(t) for t in no)


def test_a_reply_that_talks_as_if_it_was_done_is_caught_but_a_ready_card_line_is_not():
    said = ["Note added to Leah's record — \"L-TEST wants the car back by Friday.\"", "Done. I've saved it.",
            "Invoice created for Alex.", "I've sent the reminder."]
    fine = ["Got it — here's the note ready to add to Leah's record.",
            "Note on Leah's record ready — wants her car back by Friday. Tap Confirm to save it.",
            "That's prepared for review: add a note to Pat. Use the card to confirm.", "You have 1 prospect."]
    assert all(concierge_routes._claims_done(t) for t in said)
    assert not any(concierge_routes._claims_done(t) for t in fine)


def _settle(monkeypatch, saved):
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda tenant_id, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tenant_id, role, content, **kw: saved.append((role, content)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)


def _done(res):
    return [json.loads(line) for line in res.text.strip().splitlines() if json.loads(line)["type"] == "done"][0]


def test_a_typed_yes_with_a_card_waiting_says_where_the_card_is_and_runs_no_turn(client, monkeypatch):
    saved = []
    _settle(monkeypatch, saved)
    monkeypatch.setattr(concierge_routes.db, "get_live_pending_proposal",
                        lambda tenant_id, user_id=None, client=None: {"id": "p1", "summary": "Add a note to Leah's record"})

    async def boom(*a, **k):
        raise AssertionError("a bare yes with a card waiting must not start a model turn")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", boom)
    staged = []
    monkeypatch.setattr(concierge_routes.concierge_actions, "stage", lambda *a, **k: staged.append(1))

    done = _done(client.post("/concierge/business", json={"question": "Yes", "surface": "page"}))
    assert done["reply"] == concierge_routes.BARE_YES_REPLY and "tap the thumbs-up" in done["reply"]
    assert done["proposal"] is None and not staged
    assert saved == [("user", "Yes"), ("assistant", concierge_routes.BARE_YES_REPLY)]


def test_a_typed_yes_with_no_card_waiting_is_an_ordinary_message(client, monkeypatch):
    saved = []
    _settle(monkeypatch, saved)
    monkeypatch.setattr(concierge_routes.db, "get_live_pending_proposal", lambda *a, **k: None)
    asked = []

    async def fake_answer(question, history, **kwargs):
        asked.append(question)
        return concierge.BusinessReply(reply="Yes to what? Tell me what you'd like done.", links=[], action=None,
                                       usage=_USAGE, model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    done = _done(client.post("/concierge/business", json={"question": "Yes", "surface": "page"}))
    assert asked == ["Yes"] and done["reply"].startswith("Yes to what")


def test_a_typed_yes_on_the_questions_only_widget_is_left_alone(client, monkeypatch):
    saved = []
    _settle(monkeypatch, saved)
    monkeypatch.setattr(concierge_routes.db, "get_live_pending_proposal",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the widget has no cards")))

    async def fake_answer(question, history, **kwargs):
        return concierge.BusinessReply(reply="Nothing is waiting here.", links=[], action=None, usage=_USAGE, model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    assert _done(client.post("/concierge/business", json={"question": "Yes"}))["reply"] == "Nothing is waiting here."


def test_a_turn_that_only_staged_a_card_never_says_the_note_was_added(client, monkeypatch):
    saved = []
    _settle(monkeypatch, saved)
    monkeypatch.setattr(concierge_routes.db, "get_live_pending_proposal", lambda *a, **k: None)

    async def fake_answer(question, history, **kwargs):
        return concierge.BusinessReply(
            reply="Note added to Leah's record — \"L-TEST wants the car back by Friday.\"", links=[],
            action={"kind": "update_contact", "name": "Leah", "mode": "note", "content": "L-TEST"},
            usage=_USAGE, model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    monkeypatch.setattr(concierge_routes.concierge_actions, "stage",
                        lambda tid, cl, action, **kw: {"proposal_id": "p9", "kind": "update_contact",
                                                       "summary": "Add a note to Leah's record", "details": [],
                                                       "draft_text": None, "expires_s": 300})
    done = _done(client.post("/concierge/business", json={"question": "Add a note to Leah: L-TEST", "surface": "page"}))
    assert "Note added" not in done["reply"] and "Tap the thumbs-up" in done["reply"]
    assert "Add a note to Leah's record" in done["reply"]
    assert any("Tap the thumbs-up" in text for role, text in saved if role == "assistant")


def test_a_ready_card_line_from_the_model_is_left_as_written(client, monkeypatch):
    saved = []
    _settle(monkeypatch, saved)
    monkeypatch.setattr(concierge_routes.db, "get_live_pending_proposal", lambda *a, **k: None)

    async def fake_answer(question, history, **kwargs):
        return concierge.BusinessReply(reply="Got it — here's the note ready to add to Leah's record.", links=[],
                                       action={"kind": "update_contact", "name": "Leah", "mode": "note"},
                                       usage=_USAGE, model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    monkeypatch.setattr(concierge_routes.concierge_actions, "stage",
                        lambda tid, cl, action, **kw: {"proposal_id": "p9", "kind": "update_contact", "summary": "Add a note",
                                                       "details": [], "draft_text": None, "expires_s": 300})
    done = _done(client.post("/concierge/business", json={"question": "Add a note to Leah", "surface": "page"}))
    assert done["reply"] == "Got it — here's the note ready to add to Leah's record."


class _Sb:
    """Just enough of the database client for create_concierge_proposal and its read of the waiting cards."""
    def __init__(self, pending):
        self.pending, self.inserted, self.updated, self._mode = pending, [], [], None

    def table(self, name):
        return self

    def select(self, *a, **k):
        self._mode = "select"
        return self

    def update(self, patch):
        self._mode = "update"
        self.updated.append(patch)
        return self

    def insert(self, row):
        self._mode = "insert"
        self.inserted.append(row)
        return self

    def __getattr__(self, name):          # eq, is_, order, limit
        return lambda *a, **k: self

    def execute(self):
        if self._mode == "select":
            return SimpleNamespace(data=list(self.pending))
        return SimpleNamespace(data=[{"id": "new-id"}])


def _waiting(payload, summary="Add a note to Leah's record", age_s=20, user="u1", kind="update_contact"):
    made = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"id": "old-id", "kind": kind, "summary": summary, "payload": payload, "user_id": user, "created_at": made.isoformat()}


def test_the_same_request_again_reuses_the_waiting_card():
    action = {"kind": "update_contact", "name": "Leah", "content": "L-TEST wants the car back by Friday."}
    payload = {"mode": "note", "_action": action}
    sb = _Sb([_waiting(payload)])
    got = db.create_concierge_proposal("t1", "update_contact", "Add a note to Leah's record", {"mode": "note"},
                                       user_id="u1", action=action, client=sb)
    assert got == "old-id" and sb.inserted == [] and sb.updated == []      # nothing new, nothing replaced


def test_a_different_request_still_replaces_the_waiting_card():
    action = {"kind": "update_contact", "name": "Leah", "content": "something else"}
    sb = _Sb([_waiting({"mode": "note", "_action": {"kind": "update_contact", "name": "Leah", "content": "L-TEST"}})])
    got = db.create_concierge_proposal("t1", "update_contact", "Add a note to Leah's record", {"mode": "note"},
                                       user_id="u1", action=action, client=sb)
    assert got == "new-id" and len(sb.inserted) == 1 and sb.updated == [{"status": "superseded"}]


def test_an_old_card_past_the_confirm_window_is_not_reused():
    action = {"kind": "update_contact", "name": "Leah", "content": "L-TEST"}
    sb = _Sb([_waiting({"mode": "note", "_action": action}, age_s=900)])
    got = db.create_concierge_proposal("t1", "update_contact", "Add a note to Leah's record", {"mode": "note"},
                                       user_id="u1", action=action, client=sb)
    assert got == "new-id"


def test_whats_signed_off_is_answered_from_three_groups(world):
    world.members = {U2: {"user_id": U2, "role": "manager", "display_name": "Dana Reeve", "active": True}}
    world.cards = [card("tap", status="executed", result=receipt("tap", via="tap"), summary="Note on Pat's record"),
                   card("words", status="executed", result=receipt("words", via="words"), summary="Follow-up for Sam"),
                   card("legacy", status="executed", result=receipt("legacy", via=None), summary="Diary blocked")]
    out = world.run({"status": "recent"})
    groups = out["by_sign_off"]
    assert [g for g in groups["signed_off"]] and "Note on Pat's record" in groups["signed_off"][0]
    assert "approved by you, by tap" in groups["signed_off"][0]
    assert "by voice (not a sign-off)" in groups["approved_by_voice"][0] and "Follow-up for Sam" in groups["approved_by_voice"][0]
    assert "Diary blocked" in groups["went_through_no_tap_recorded"][0]
    # and the guidance puts the groups first, so a "what have I signed off?" answer is built from them
    assert out["how_to_read"].startswith("If they ask what they have SIGNED OFF") and "`by_sign_off`" in out["how_to_read"]
