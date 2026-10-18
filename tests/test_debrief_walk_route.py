"""The Debrief button's server half: POST /concierge/voice/chat with
debrief="start" | "next", and the walk riding on the turns after it.

The brain is faked (as in tests/test_voice_live.py); what is under test is what
the route hands it — the note, the tool — and what comes back on the done frame.
The agenda itself is covered in tests/test_debrief_walk.py.

Run: python tests/run_offline.py tests/test_debrief_walk_route.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import concierge, concierge_routes, db, debrief_walk as dw, gptlive, security
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header

USAGE = {"input_tokens": 1, "output_tokens": 1,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def canned_agenda(*labels):
    """Two (or more) groups without touching a database."""
    groups = []
    for key in ("drafts", "money", "quotes")[:len(labels)]:
        groups.append(dw._group(key, [dw._item(key, f"Person {key}", f"{key}@x.com", "Waiting",
                                                f"something about {key}", rank=10)]))
    return {"date": "2026-10-14", "weekday": "Wednesday 14 October", "greeting": "Morning",
            "groups": groups, "unread": [], "total": len(groups)}


@pytest.fixture
def api(monkeypatch):
    """A signed-in owner, a fake brain that records what it was handed."""
    seen = {"member": None, "calls": [], "tool": None, "agenda": canned_agenda("a", "b"), "saved": []}
    monkeypatch.setattr(db, "get_member", lambda *a, **k: seen["member"])
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda *a, **k: {"name": "Biz"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tenant_id, role, content, **kw: seen["saved"].append((role, content)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.fairuse, "consume", lambda *a, **k: (True, None))

    async def fake_answer(question, history, **kwargs):
        seen["calls"].append({"question": question, **kwargs})
        if seen["tool"] is not None:
            await kwargs["on_tool"](dw.TOOL_NAME, seen["tool"])
            seen["calls"][-1]["tool_out"] = await kwargs["tool_executor"](dw.TOOL_NAME, seen["tool"])
        await kwargs["on_delta"]('{"reply": "Morning.", "links": [], "action": {"kind": ""}}', 0)
        return concierge.BusinessReply(reply="Morning.", links=[], action=None, usage=USAGE, model="test")

    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    monkeypatch.setattr(dw, "gather_agenda", lambda *a, **k: seen["agenda"])
    from app import voice_turn_state
    monkeypatch.setattr(voice_turn_state, "_states", {})
    dw._WALKS.clear()
    gptlive._sessions.clear()
    security._BUCKETS.clear()      # the route is rate limited per minute, process-wide
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    seen["client"] = TestClient(app)
    yield seen
    app.dependency_overrides.clear()
    dw._WALKS.clear()
    gptlive._sessions.clear()
    security._BUCKETS.clear()


def chat(api, question="Run me through my debrief.", **body):
    res = api["client"].post("/concierge/voice/chat", json={"question": question, **body})
    frames = [json.loads(f[6:]) for f in res.text.split("\n\n") if f.startswith("data: ")] \
        if res.headers["content-type"].startswith("text/event-stream") else []
    return res, frames


def done_of(frames):
    return next(f for f in frames if f["t"] == "done")


def tool_names(call):
    return [t["name"] for t in call["tools"]]


def test_start_hands_the_brain_the_note_and_the_tool_and_reports_progress(api):
    res, frames = chat(api, debrief="start", voice_session="vs-1")
    assert res.status_code == 200
    call = api["calls"][0]
    assert call["question"] == "Run me through my debrief."
    assert call["question_note"].startswith("[DEBRIEF") or "[DEBRIEF" in call["question_note"]
    assert "Open with" in call["question_note"] and "Drafts waiting" in call["question_note"]
    assert dw.TOOL_NAME in tool_names(call)
    assert done_of(frames)["debrief"] == {"step": 1, "of": 2, "label": "Drafts waiting", "finished": False}
    assert dw.current("t1", "u1", "vs-1").opened is True
    # the owner's request is saved with her answer, like any turn
    assert ("user", "Run me through my debrief.") in api["saved"]


def test_a_started_walk_leaves_a_trace_of_counts_and_keys_only(api, monkeypatch):
    rows = []
    monkeypatch.setattr(concierge_routes.trace, "emit",
                        lambda tenant_id, kind, **kw: rows.append((tenant_id, kind, kw)))
    api["agenda"] = {**canned_agenda("a", "b"), "unread": ["Quotes"]}
    chat(api, debrief="start", voice_session="vs-1")
    started = [r for r in rows if r[1] == "debrief_walk"]
    assert len(started) == 1 and started[0][0] == "t1"
    kw = started[0][2]
    assert kw["outcome"] == "started" and kw["user_id"] == "u1"
    assert kw["detail"] == {"groups": ["drafts", "money"], "counts": [1, 1],
                            "unread": ["Quotes"], "trigger": "start"}
    # nothing customer-written travels in a trace
    assert "Person" not in str(kw) and "@" not in str(kw)
    # a later ordinary turn writes no walk trace of its own
    rows.clear()
    chat(api, "ok", voice_session="vs-1")
    assert not [r for r in rows if r[1] == "debrief_walk"]


def test_an_empty_day_is_traced_as_empty(api, monkeypatch):
    rows = []
    monkeypatch.setattr(concierge_routes.trace, "emit",
                        lambda tenant_id, kind, **kw: rows.append((kind, kw)))
    api["agenda"] = {**canned_agenda(), "groups": [], "total": 0}
    chat(api, debrief="start", voice_session="vs-1")
    assert ("debrief_walk", "empty") in [(k, kw.get("outcome")) for k, kw in rows]


def test_the_walk_rides_on_later_turns_of_the_same_conversation_only(api):
    chat(api, debrief="start", voice_session="vs-1")
    api["calls"].clear()
    chat(api, "what about Meg?", voice_session="vs-1")
    note = api["calls"][0]["question_note"]
    assert "DEBRIEF IN PROGRESS — group 1 of 2" in note and "Open with" not in note
    assert dw.TOOL_NAME in tool_names(api["calls"][0])
    # another conversation, even the same person, is not on the walk
    api["calls"].clear()
    _, frames = chat(api, "what about Meg?", voice_session="vs-2")
    assert "DEBRIEF" not in (api["calls"][0].get("question_note") or "")
    assert dw.TOOL_NAME not in tool_names(api["calls"][0])
    assert "debrief" not in done_of(frames)        # an ordinary turn's payload is unchanged


def test_the_model_moves_on_with_the_tool_and_the_trail_says_so(api):
    chat(api, debrief="start", voice_session="vs-1")
    api["tool"] = {"outcome": "done"}
    _, frames = chat(api, "ok next", voice_session="vs-1")
    out = api["calls"][-1]["tool_out"]
    assert out["moved_on"] and out["group"]["label"] == "Money"
    done = done_of(frames)
    assert done["debrief"]["step"] == 2 and done["debrief"]["label"] == "Money"
    tool_frame = next(f for f in frames if f["t"] == "tool")
    assert tool_frame["hint"] == "Moving on…" and tool_frame["label"] == "Moved on in the debrief"
    assert [t["label"] for t in done["trail"]] == ["Moved on in the debrief"]


def test_the_button_can_move_it_on_without_the_model_being_asked_in_words(api):
    chat(api, debrief="start", voice_session="vs-1")
    api["calls"].clear()
    _, frames = chat(api, "Next.", debrief="next", voice_session="vs-1")
    assert "group 2 of 2: Money" in api["calls"][0]["question_note"]
    assert done_of(frames)["debrief"]["step"] == 2
    # "Next" means finished with it — only the owner's words ("leave it") put one in "left for later"
    walk = dw.current("t1", "u1", "vs-1")
    assert walk.dealt == ["Drafts waiting"] and walk.left == []
    assert [ (r, c) for r, c in api["saved"] if r == "user"][-1] == ("user", "Next.")


def test_finishing_the_last_group_returns_the_conversation_to_normal(api):
    chat(api, debrief="start", voice_session="vs-1")
    api["tool"] = {"outcome": "done"}
    chat(api, "next", voice_session="vs-1")                       # to group 2
    _, frames = chat(api, "next", voice_session="vs-1")            # off the end
    assert api["calls"][-1]["tool_out"]["finished"] is True
    assert done_of(frames)["debrief"]["finished"] is True
    api["tool"] = None
    api["calls"].clear()
    chat(api, "thanks", voice_session="vs-1")
    assert dw.TOOL_NAME not in tool_names(api["calls"][0])
    assert not api["calls"][0].get("question_note") or "DEBRIEF" not in api["calls"][0]["question_note"]


def test_next_with_nothing_to_move_on_is_a_fresh_start(api):
    """A restart lost the walk (or it had finished): the button still works."""
    _, frames = chat(api, "Next.", debrief="next", voice_session="vs-9")
    assert "Open with" in api["calls"][0]["question_note"]
    assert done_of(frames)["debrief"]["step"] == 1


def test_start_again_replaces_a_walk_in_progress(api):
    chat(api, debrief="start", voice_session="vs-1")
    api["tool"] = {"outcome": "done"}
    chat(api, "next", voice_session="vs-1")
    api["tool"] = None
    _, frames = chat(api, debrief="start", voice_session="vs-1")
    assert done_of(frames)["debrief"]["step"] == 1


def test_a_quiet_day_gets_one_honest_sentence_and_no_tool(api):
    api["agenda"] = {**canned_agenda(), "groups": [], "total": 0}
    _, frames = chat(api, debrief="start", voice_session="vs-1")
    call = api["calls"][0]
    assert "Nothing needs the owner" in call["question_note"]
    assert dw.TOOL_NAME not in tool_names(call)
    assert done_of(frames)["debrief"]["finished"] is True
    api["calls"].clear()
    chat(api, "ok", voice_session="vs-1")
    assert not api["calls"][0].get("question_note")


def test_a_failed_agenda_says_so_and_starts_nothing(api, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(dw, "gather_agenda", boom)
    res, _ = chat(api, debrief="start", voice_session="vs-1")
    assert res.status_code == 503 and "couldn't put the debrief together" in res.json()["error"]
    assert api["calls"] == [] and dw.current("t1", "u1", "vs-1") is None


@pytest.mark.parametrize("role,active", [("staff", True), ("manager", False)])
def test_staff_and_switched_off_managers_are_refused_and_nothing_starts(api, role, active):
    from app import people
    api["member"] = {"role": role, "active": active, "scopes": {}}
    res, _ = chat(api, debrief="start", voice_session="vs-1")
    assert res.status_code == 403
    assert api["calls"] == [] and dw.current("t1", "u1", "vs-1") is None
    assert people.member(api["member"]) is not None


def test_a_manager_may(api):
    api["member"] = {"role": "manager", "active": True, "scopes": {}}
    res, _ = chat(api, debrief="start", voice_session="vs-1")
    assert res.status_code == 200 and api["calls"]


def test_viewing_as_someone_else_is_refused(api):
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1", viewing_as="owner-id")
    res, _ = chat(api, debrief="start", voice_session="vs-1")
    assert res.status_code == 403 and api["calls"] == []


def test_in_live_mode_the_note_says_to_tap_the_card_not_to_ask_for_a_yes(api):
    gptlive._sessions["s1"] = gptlive.Session("s1", "prov", "t1", "u1", "vs-live", "permissions", None)
    chat(api, debrief="start", voice_session="vs-live")
    assert "TAPPING" in api["calls"][0]["question_note"]
    api["calls"].clear()
    chat(api, debrief="start", voice_session="vs-std")
    assert "TAPPING" not in api["calls"][0]["question_note"]


def test_the_trail_labels_and_spoken_hints_know_the_tool():
    assert concierge_routes._BUSINESS_TOOL_HINTS[dw.TOOL_NAME] == "Moving on…"
    assert concierge_routes._BUSINESS_TOOL_LABELS[dw.TOOL_NAME] == "Moved on in the debrief"
    # the phone may say the chip aloud only if the server could have said it
    assert concierge_routes._squash("Moving on.") in concierge_routes._fixed_lines()


@pytest.mark.parametrize("restriction", ["staff", "inactive", "view_as"])
def test_existing_walk_is_withheld_when_access_changes(api, restriction):
    chat(api, debrief="start", voice_session="revoked-walk")
    walk = dw.current("t1", "u1", "revoked-walk")
    if restriction == "view_as":
        app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
            tenant_id="t1", client=None, user_id="u1", viewing_as="owner-id")
    else:
        api["member"] = {"role": "staff" if restriction == "staff" else "manager",
                         "active": restriction != "inactive", "scopes": {}}
    api["tool"] = {"outcome": "done"}  # An unsolicited tool call is refused too.
    res, frames = chat(api, "What is next?", voice_session="revoked-walk")
    assert res.status_code == 200
    call = api["calls"][-1]
    assert dw.TOOL_NAME not in tool_names(call)
    assert "DEBRIEF" not in (call.get("question_note") or "")
    assert "debrief" not in done_of(frames)
    assert not call["tool_out"].get("moved_on")
    assert walk.idx == 0


@pytest.mark.parametrize("command", ["start", "next"])
def test_duplicate_turn_does_not_replace_or_advance_walk(api, command):
    chat(api, debrief="start", voice_session="duplicate-walk", turn_id="duplicate-walk-turn")
    walk = dw.current("t1", "u1", "duplicate-walk")
    before = (walk.idx, list(walk.dealt), list(walk.left))
    res, _ = chat(api, debrief=command, voice_session="duplicate-walk", turn_id="duplicate-walk-turn")
    assert res.status_code == 503
    assert dw.current("t1", "u1", "duplicate-walk") is walk
    assert (walk.idx, walk.dealt, walk.left) == before
    assert len(api["calls"]) == 1


@pytest.mark.parametrize("command", ["start", "next"])
def test_cancelled_turn_does_not_mutate_walk_and_cleans_up(api, command):
    from app import voice_turn_state as turns
    chat(api, debrief="start", voice_session="cancelled-walk")
    walk = dw.current("t1", "u1", "cancelled-walk")
    identity = turns.key("t1", "u1", "cancelled-walk-" + command)
    turns.cancel(identity)
    res, frames = chat(api, debrief=command, voice_session="cancelled-walk",
                       turn_id=identity[-1])
    assert res.status_code == 200
    assert any(f["t"] == "cancelled" for f in frames)
    assert dw.current("t1", "u1", "cancelled-walk") is walk
    assert walk.idx == 0 and walk.dealt == []
    assert len(api["calls"]) == 1
    assert turns._states[identity].status == "cancelled"
