"""Concierge pass-1 fix batch (2026-09-06, Brightwell run; docs/concierge-pass1-
findings-2026-09-06.md): the answer is a TOOL (no output schema on tool
rounds), the Assistant widget is questions-only, the voice block is scoped to
customer email, the diary's shape reaches the prompt, runaway replies never
render, and the system block carries a cache breakpoint."""
import asyncio
from proposal_receipts import execution_result
import json
from types import SimpleNamespace

import pytest

from app import concierge, concierge_routes, llm
from app.config import settings


from fastapi.testclient import TestClient
from app.auth import require_onboarded, TenantCtx
from app.main import app
from app.security import require_csrf_header


@pytest.fixture
def client(monkeypatch):
    # The account's own owner (no People row): routes that read the asker
    # refuse a failed read (security review M1/M34).
    from app import db as _db
    monkeypatch.setattr(_db, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


# ── fakes ────────────────────────────────────────────────────────────────────
class _Usage:
    input_tokens = 3
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _text(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, usage=_Usage(), model="test")


def _tool(name, inp, tid="tu1", stop="tool_use"):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name,
                                                    input=inp, id=tid)],
                           stop_reason=stop, usage=_Usage(), model="test")


class _Messages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class _Client:
    def __init__(self, replies):
        self.messages = _Messages(replies)


@pytest.fixture
def quiet_llm(monkeypatch):
    monkeypatch.setattr(llm.db, "insert_llm_call", lambda row: None)
    monkeypatch.setattr(settings, "llm_prompt_cache", False)


def _run(question, client, *, actions=True, history=None, tools=None):
    async def executor(name, args):
        return {"bookings": [{"booking_id": "b1"}], "count": 1}
    return asyncio.run(concierge.answer_business(
        question, history or [], tools=tools or [{"name": "list_bookings",
                                                  "input_schema": {"type": "object"}}],
        tool_executor=executor, tenant_id="t1", actions=actions))


# ── the answer tool ──────────────────────────────────────────────────────────
def test_answer_tool_shape_matches_the_structured_output_subset():
    """Tool input schemas ride the same keyword restrictions the 07-25 outage
    taught us; and the action is OPTIONAL with only `kind` required — the
    22-empty-field skeleton is gone."""
    banned = {"maxItems", "minItems", "minLength", "maxLength", "pattern",
              "minimum", "maximum", "format"}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in banned
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    t = concierge.answer_tool(actions=True)
    walk(t["input_schema"])
    assert t["name"] == "answer"
    assert t["input_schema"]["required"] == ["reply"]
    assert t["input_schema"]["properties"]["action"]["required"] == ["kind"]
    # questions-only surface: no action property at all
    assert "action" not in concierge.answer_tool(actions=False)["input_schema"]["properties"]


def test_tool_rounds_carry_no_output_schema_and_end_with_the_answer_tool(quiet_llm, monkeypatch):
    client = _Client([
        _tool("list_bookings", {"date_from": "2026-09-10"}),
        _tool("answer", {"reply": "Two cleans on Thursday.",
                         "links": [{"kind": "diary", "id": "2026-09-10", "label": "Thu"}]}),
    ])
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("what's on Thursday?", client)
    assert out.reply == "Two cleans on Thursday."
    assert out.links == [{"kind": "diary", "id": "2026-09-10", "label": "Thu"}]
    assert out.action is None
    for call in client.messages.calls:
        assert "output_config" not in call
        assert any(t["name"] == "answer" for t in call["tools"])
    # the first round lets the model choose (and batch); from round 2 parallel
    # calls are off — the live double `answer` call of pass 2
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}
    assert client.messages.calls[1]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    fed = client.messages.calls[1]["messages"][-1]["content"][0]
    assert fed["type"] == "tool_result" and "b1" in fed["content"]


def test_answer_tool_action_is_parsed_and_dropped_on_the_questions_only_surface(quiet_llm, monkeypatch):
    reply = _tool("answer", {"reply": "Ready.", "action": {"kind": "move_booking",
                                                            "booking_id": "b1",
                                                            "new_date": "2026-09-09",
                                                            "new_window": "pm"}})
    client = _Client([reply])
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("move it", client, actions=True)
    assert out.action["kind"] == "move_booking" and out.action["new_date"] == "2026-09-09"

    client2 = _Client([_tool("answer", {"reply": "Ready.", "action": {"kind": "move_booking"}})])
    monkeypatch.setattr(concierge, "client", lambda: client2)
    out2 = _run("move it", client2, actions=False)
    assert out2.action is None
    # and the questions-only surface never even offers the action property
    assert "action" not in [t for t in client2.messages.calls[0]["tools"]
                            if t["name"] == "answer"][0]["input_schema"]["properties"]


def test_last_round_forces_the_answer_tool(quiet_llm, monkeypatch):
    n = concierge.BUSINESS_TOOL_LOOP_MAX
    replies = [_tool("list_bookings", {}, tid=f"t{i}") for i in range(n - 1)]
    replies.append(_tool("answer", {"reply": "Done looking."}))
    client = _Client(replies)
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("loop", client)
    assert out.reply == "Done looking."
    assert client.messages.calls[-1]["tool_choice"] == {"type": "tool", "name": "answer"}


def test_max_tokens_stop_degrades_to_an_honest_sentence(quiet_llm, monkeypatch):
    client = _Client([_text("Let me look those up now.}]}" * 300, stop="max_tokens")])
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("yes do both", client)
    assert out.reply == concierge.RUNAWAY_FALLBACK
    assert "}]}" not in out.reply


def test_runaway_detector_catches_repeats_and_json_debris():
    assert concierge._runaway_reply("Let me look those up now. " * 4)
    assert concierge._runaway_reply("He re's where things stand:}]")
    assert not concierge._runaway_reply("Two cleans tomorrow. Both confirmed. Margaret first.")
    assert not concierge._runaway_reply("")


def test_plain_text_json_reply_still_parses(quiet_llm, monkeypatch):
    client = _Client([_text('{"reply": "Two bookings.", "links": [], "action": {"kind": ""}}')])
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("what's on?", client)
    assert out.reply == "Two bookings."


def test_empty_text_gets_one_forced_answer_retry(quiet_llm, monkeypatch):
    client = _Client([_text(""), _tool("answer", {"reply": "Here it is."})])
    monkeypatch.setattr(concierge, "client", lambda: client)
    out = _run("where are we?", client)
    assert out.reply == "Here it is."
    assert client.messages.calls[1]["tool_choice"] == {"type": "tool", "name": "answer"}


def test_voice_mode_keeps_the_structured_output_path(quiet_llm, monkeypatch):
    """The live-voice surface streams JSON text to TTS — untouched by design."""
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return _text('{"reply": "Two.", "links": [], "action": {"kind": ""}}')
    monkeypatch.setattr(concierge.llm, "create", fake_create)
    monkeypatch.setattr(concierge, "client", lambda: object())

    async def executor(name, args):
        return {}
    out = asyncio.run(concierge.answer_business(
        "what's on?", [], tools=[{"name": "t", "input_schema": {}}],
        tool_executor=executor, tenant_id="t1", voice_mode=True))
    assert out.reply == "Two."
    assert "output_config" in calls[0]


# ── the prompt ───────────────────────────────────────────────────────────────
def test_prompt_questions_only_surface_says_where_changes_happen():
    p = concierge.build_business_prompt(actions=False)
    assert "QUESTIONS ONLY" in p
    assert "kind 'concierge'" in p
    assert "define_dependants" not in p
    assert "COMPLETE REQUESTS GET A PROPOSAL" not in p
    q = concierge.build_business_prompt(actions=True)
    assert "QUESTIONS ONLY" not in q
    assert "COMPLETE REQUESTS GET A PROPOSAL" in q
    assert "never end with 'shall I?'" in q


def test_prompt_answers_via_the_tool_and_bans_narration():
    p = concierge.build_business_prompt()
    assert "`answer` tool" in p
    assert "no 'let me check…' text" in p
    assert "Batch independent lookups" in p
    assert "search_contacts first" in p
    # voice mode keeps its spoken preamble rule instead
    v = concierge.build_business_prompt(voice_mode=True)
    assert "8. HOW TO ANSWER" not in v


def test_prompt_voice_block_is_scoped_to_customer_email():
    p = concierge.build_business_prompt(voice="Never include an alarm code in a reply.")
    assert "TO CUSTOMERS" in p
    assert "Withholding an owner's own data is wrong" in p
    # and the standalone rule (live 2026-09-06 pass 2: the scoping sentence
    # alone did not beat the voice note's own word "reply")
    assert "10. THE OWNER'S OWN DATA IS THEIRS" in p
    assert "GIVE IT TO THEM in this chat" in p


def test_prompt_booked_group_is_one_line_in_inbox_status_answers():
    p = concierge.build_business_prompt()
    assert "The Booked group (upcoming bookings) is ONE line" in p


def test_prompt_carries_the_diary_shape_when_given():
    p = concierge.build_business_prompt(diary_shape="a timed diary: 60-minute slots, up to 3 bookings at the same time")
    assert "DIARY SHAPE" in p and "up to 3 bookings" in p
    assert "never declare a slot taken" in p
    assert "DIARY SHAPE" not in concierge.build_business_prompt()


def test_diary_shape_line_reads_the_brightwell_config():
    cfg = {"windows": "day", "slot_minutes": 60, "open_time": "08:00:00",
           "close_time": "17:00:00", "capacity_per_window": 3, "buffer_minutes": 30,
           "working_days": "mon,tue,wed,thu,fri", "min_notice_hours": 72,
           "blackout_dates": ["2026-12-25"],
           "service_types": [{"name": "Regular clean", "minutes": 120, "timing": "half"},
                             {"name": "End of tenancy", "minutes": 240, "timing": "half"}]}
    line = concierge_routes.diary_shape_line(carried_config(cfg))
    assert "60-minute slots between 08:00 and 17:00, up to 3 bookings at the same time" in line
    assert "Regular clean (120 min, booked by morning/afternoon window)" in line
    assert "30-minute gap" in line and "minimum notice 72 hours" in line
    assert "1 blackout dates set" in line
    assert "Booking is unavailable" in concierge_routes.diary_shape_line(None)
    assert "Booking is unavailable" in concierge_routes.diary_shape_line({"capacity_per_window": "x"})


# ── the route: surface split ─────────────────────────────────────────────────
def _route_fakes(monkeypatch, action):
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda tenant_id, client=None: {"crm", "scheduling"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tenant_id, client=None: {"name": "K"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config",
                        lambda tenant_id, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "get_scheduling_config",
                        lambda tenant_id, client=None: carried_config({"windows": "day", "capacity_per_window": 3}))
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    seen = {}

    async def fake_answer(question, history, **kwargs):
        seen.update(kwargs)
        return concierge.BusinessReply(
            reply="Ready.", links=[{"kind": "concierge", "id": "", "label": "Concierge"}],
            action=action,
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            model="test")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    staged = []
    monkeypatch.setattr(
        concierge_routes.concierge_actions, "stage",
        lambda tid, cl, act, **kw: staged.append(act) or {
            "proposal_id": "p1", "kind": act["kind"], "summary": "s",
            "details": [], "draft_text": "", "expires_s": 300})
    return seen, staged


def test_widget_surface_is_questions_only(client, monkeypatch):
    seen, staged = _route_fakes(monkeypatch, {"kind": "move_booking", "booking_id": "b1"})
    res = client.post("/concierge/business", json={"question": "move it"})   # default surface
    done = [json.loads(l) for l in res.text.strip().splitlines()
            if json.loads(l)["type"] == "done"][0]
    assert seen["actions"] is False
    assert "up to 3 bookings" in seen["diary_shape"]
    assert staged == []                       # nothing staged, whatever the model said
    assert done["proposal"] is None
    assert done["links"] == [{"label": "Concierge", "href": "/app/concierge"}]


def test_page_surface_stages_proposals(client, monkeypatch):
    seen, staged = _route_fakes(monkeypatch, {"kind": "move_booking", "booking_id": "b1"})
    res = client.post("/concierge/business", json={"question": "move it", "surface": "page"})
    done = [json.loads(l) for l in res.text.strip().splitlines()
            if json.loads(l)["type"] == "done"][0]
    assert seen["actions"] is True
    assert staged and staged[0]["kind"] == "move_booking"
    assert done["proposal"]["proposal_id"] == "p1"


def test_unknown_surface_is_rejected(client, monkeypatch):
    _route_fakes(monkeypatch, None)
    res = client.post("/concierge/business", json={"question": "hi", "surface": "voice"})
    assert res.status_code == 422


def test_the_page_sends_surface_page_and_the_widget_does_not():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "src/app" / "static" / "concierge_panel.js").read_text(encoding="utf-8")
    js = (root / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    assert "surface: 'page'" in page
    # The widget's questions-only business tab is gone (2026-09-23): it never
    # calls the business route at all now.
    assert "/concierge/business" not in js


# ── the cache breakpoint on the system block ─────────────────────────────────
def test_system_block_gets_its_own_cache_breakpoint(monkeypatch):
    monkeypatch.setattr(settings, "llm_prompt_cache", True)
    out = llm._cached_kwargs({"system": "SYS", "messages": [{"role": "user", "content": "q"}]})
    assert out["system"] == [{"type": "text", "text": "SYS",
                              "cache_control": {"type": "ephemeral"}}]
    assert out["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # an already-blocked system passes through untouched; caller dict never mutated
    src = {"system": [{"type": "text", "text": "S"}], "messages": [{"role": "user", "content": "q"}]}
    out2 = llm._cached_kwargs(src)
    assert out2["system"] == [{"type": "text", "text": "S"}]
    assert src["messages"][0]["content"] == "q"


# ── move_booking on a services diary (pass 2: "'pm' isn't a bookable time") ──
from datetime import date as _date
from app import concierge_actions


_SERVICES_CFG = {"windows": "day", "slot_minutes": 60, "open_time": "08:00:00",
                 "close_time": "17:00:00", "capacity_per_window": 3, "buffer_minutes": 30,
                 "working_days": "mon,tue,wed,thu,fri", "min_notice_hours": 72,
                 "service_types": [{"name": "Regular clean", "minutes": 120, "timing": "half"}]}


def test_services_diary_is_detected_only_with_a_menu():
    assert concierge_actions._is_services_diary(_SERVICES_CFG)
    assert not concierge_actions._is_services_diary({"windows": "am,pm", "service_types": [{"name": "x"}]})
    assert not concierge_actions._is_services_diary({"windows": "day"})
    assert not concierge_actions._is_services_diary(None)


def _booking():
    return {"id": "b1", "booking_date": "2026-09-07", "window": "am", "start_block": 36,
            "length_blocks": 10, "contact": "m@x.com", "booking_ref": "BK-1078",
            "summary": "Regular clean — Margaret (2.5 hrs, Priya)", "status": "confirmed"}


def test_timed_move_resolves_a_window_word_to_a_start_block(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "list_active_bookings",
                        lambda tid, a, b, client=None: [{"id": "other", "booking_date": "2026-09-09",
                                                         "start_block": 34, "length_blocks": 8}])
    seen = {}

    def fake_fit(cfg, booked, length, day, window, now, **kw):
        seen.update(window=window, length=length, now=now, booked=booked, **kw)
        return 48 if window == "pm" else None
    monkeypatch.setattr(concierge_actions.scheduling, "first_fit_start", fake_fit)
    summary, payload, details, _ = concierge_actions._stage_timed_move(
        "t1", None, _SERVICES_CFG, _booking(), "2026-09-09", "afternoon")
    assert seen["window"] == "pm" and seen["length"] == 10
    assert seen["now"] is None                       # the owner's move: no notice cutoff
    assert seen["buffer_blocks"] == 2 and seen["capacity"] == 3
    assert all(b["id"] != "b1" for b in seen["booked"])   # its own slot never blocks it
    assert payload["timed"] is True and payload["new_start_block"] == 48
    assert payload["new_window"] == "pm" and payload["length_blocks"] == 10
    assert "Margaret" in summary and "at 12:00" in summary and "7th Sep 26" in summary
    assert details[1].endswith("arriving 12:00 (2.5 hours)")


def test_timed_move_refuses_a_full_window_and_a_nonsense_word(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_actions.scheduling, "first_fit_start", lambda *a, **k: None)
    monkeypatch.setattr(concierge_actions, "_alternatives", lambda *a, **k: " Free instead: Fri.")
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_timed_move("t1", None, _SERVICES_CFG, _booking(), "2026-09-09", "morning")
    assert "no room in the morning for a 2.5-hour visit" in str(e.value)
    assert "Free instead" in str(e.value)
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_timed_move("t1", None, _SERVICES_CFG, _booking(), "2026-09-09", "teatime")
    assert "say morning, afternoon, or a time like 14:00" in str(e2.value)


def test_stage_move_booking_routes_a_services_diary_to_the_timed_path(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking", lambda bid, tid, client=None: _booking())
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SERVICES_CFG))
    monkeypatch.setattr(concierge_actions.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_actions.scheduling, "first_fit_start", lambda *a, **k: 56)
    # A day that is always ahead of the clock: plan_booking_move refuses a
    # past date, and the hardcoded 9 Sep went stale the day after it was written.
    from datetime import date as _date, timedelta as _td
    soon = (_date.today() + _td(days=2)).isoformat()
    summary, payload, details, _ = concierge_actions._stage_move_booking(
        "t1", None, {"booking_id": "0f6f74c1-7810-4225-9fd7-0381e1c99413",
                     "new_date": soon, "new_window": "pm"})
    assert payload["timed"] and payload["new_start_block"] == 56
    assert "at 14:00" in summary


def test_exec_move_booking_timed_moves_in_place(monkeypatch):
    """The timed branch runs through app/record_edits.apply_booking_move — the
    same function the job page's Move form calls (2026-09-06)."""
    from app import record_edits
    monkeypatch.setattr(concierge_actions.db, "get_booking",
                        lambda bid, tid, client=None: {"id": bid, "status": "confirmed", "summary": "Regular clean"})
    seen = {}

    async def _apply(tid, booking, plan, via="the diary", client=None):
        seen.update(bid=booking["id"], plan=plan, via=via)
        return "Done — Regular clean moved to 9th Sep 26 at 12:00."
    monkeypatch.setattr(record_edits, "apply_booking_move", _apply)
    out = asyncio.run(concierge_actions._exec_move_booking(
        "t1", None, {"booking_id": "b1", "timed": True, "new_date": "2026-09-09",
                     "new_start_block": 48, "length_blocks": 10, "new_window": "pm",
                     "old_date": "2026-09-07", "old_window": "am", "contact": "m@x.com",
                     "booking_ref": "BK-1078", "what": "Regular clean"}, ""))
    assert seen["bid"] == "b1" and seen["via"] == "the Concierge" and seen["plan"]["new_start_block"] == 48
    assert out["summary"] == "Done — Regular clean moved to 9th Sep 26 at 12:00."


def test_exec_move_booking_timed_reports_a_lost_slot(monkeypatch):
    from app import record_edits
    monkeypatch.setattr(concierge_actions.db, "get_booking",
                        lambda bid, tid, client=None: {"id": bid, "status": "confirmed"})
    monkeypatch.setattr(record_edits.db, "move_booking_in_place", lambda *a, **k: None)
    monkeypatch.setattr(concierge_actions, "_alternatives", lambda *a, **k: "")
    with pytest.raises(concierge_actions.ActionError) as e:
        asyncio.run(concierge_actions._exec_move_booking(
            "t1", None, {"booking_id": "b1", "timed": True, "new_date": "2026-09-09",
                         "new_start_block": 48, "length_blocks": 10, "new_window": "pm",
                         "old_date": "2026-09-07", "old_window": "am"}, ""))
    assert "filled up in the meantime" in str(e.value)


# ── get_conversation rescues a guessed address (pass 2) ──────────────────────
from app import concierge_data


def test_get_conversation_resolves_a_guessed_address_by_name(monkeypatch):
    convos = {"dan@hallamlettings.example.com": [{"created_at": "2026-09-04", "subject": "End of tenancy",
                                                 "body_in": "Can it be 1 Oct?", "draft_reply": "", "status": "pending"}]}
    monkeypatch.setattr(concierge_data.db, "get_conversation",
                        lambda tid, contact, client=None: convos.get(contact, []))
    monkeypatch.setattr(concierge_data.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None:
                        [{"name": "Dan Hallam", "email": "dan@hallamlettings.example.com"}]
                        if "hallam" in (q or "").lower() else [])
    out = concierge_data._get_conversation("t1", None, {"contact": "dan.hallam@hallam-co.com"})
    assert out["count"] == 1 and out["contact"] == "dan@hallamlettings.example.com"
    assert out["resolved_from_name"] is True and "dan.hallam@hallam-co.com" in out["note"]
    # a bare name works the same way
    out2 = concierge_data._get_conversation("t1", None, {"contact": "Dan Hallam"})
    assert out2["count"] == 1
    # and an unknown, unmatched address stays an honest empty with a hint
    out3 = concierge_data._get_conversation("t1", None, {"contact": "zed@nowhere.example"})
    assert out3["count"] == 0 and "search_contacts" in out3["message"]


def test_get_conversation_rescue_refuses_ambiguity(monkeypatch):
    monkeypatch.setattr(concierge_data.db, "get_conversation", lambda tid, contact, client=None: [])
    monkeypatch.setattr(concierge_data.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None:
                        [{"name": "Sam Deane", "email": "sam@x.com"}, {"name": "Sam Patel", "email": "sam@y.com"}])
    out = concierge_data._get_conversation("t1", None, {"contact": "sam@guess.example"})
    assert out["count"] == 0 and "resolved_from_name" not in out


# ── cancel_booking: skip-and-keep, series_from, the customer's note ──────────
def _series_booking(**over):
    b = {"id": "b1", "booking_ref": "BK-1004", "booking_date": "2099-09-08", "window": "am",
         "start_block": 36, "length_blocks": 12, "status": "confirmed",
         "contact": "kris@x.com", "summary": "Regular clean — Kris (3 hrs, Sam)",
         "recurrence_id": "rid-1"}
    b.update(over)
    return b


def _later():
    return [_series_booking(id="b2", booking_ref="BK-1005", booking_date="2099-09-22"),
            _series_booking(id="b3", booking_ref="BK-1006", booking_date="2099-10-06")]


def test_cancel_kind_is_gated_on_the_diary():
    assert "cancel_booking" in concierge_actions.allowed_kinds(True, True)
    assert "cancel_booking" in concierge_actions.allowed_kinds(False, True)
    assert "cancel_booking" not in concierge_actions.allowed_kinds(True, False)
    assert "cancel_booking" in concierge.BUSINESS_ACTION_KINDS
    assert "scope" in concierge._ACTION_FIELDS and "notify" in concierge._ACTION_FIELDS
    assert "cancel_booking (cancel ONE future booking" in concierge.build_business_prompt()
    assert "skip this one and keep the" in concierge.build_business_prompt(full_qa=False)


def test_stage_cancel_one_is_skip_and_keep(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking_by_ref",
                        lambda tid, ref, client=None: _series_booking() if ref.upper() == "BK-1004" else None)
    monkeypatch.setattr(concierge_actions, "_series_after", lambda tid, c, b: _later())
    summary, payload, details, _ = concierge_actions._stage_cancel_booking(
        "t1", None, {"booking_id": "bk-1004", "scope": "one", "reason": "away"})
    assert summary == "Cancel BK-1004 — Regular clean — Kris (3 hrs, Sam), 8th Sep 99 at 09:00"
    assert any("series keeps going — next visit 22nd Sep 99 (BK-1005), 2 more booked" in d for d in details)
    assert any("note goes to your approval queue" in d for d in details)
    assert any("No charge" in d for d in details)
    assert payload["scope"] == "one" and payload["later_ids"] == [] and payload["notify"] is True
    assert payload["series_next"] == "2099-09-22" and payload["reason"] == "away"


def test_stage_cancel_series_from_ends_the_run_and_can_stay_silent(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking",
                        lambda bid, tid, client=None: _series_booking())
    monkeypatch.setattr(concierge_actions, "_series_after", lambda tid, c, b: _later())
    summary, payload, details, _ = concierge_actions._stage_cancel_booking(
        "t1", None, {"booking_id": "0f6f74c1-7810-4225-9fd7-0381e1c99413",
                     "scope": "series_from", "notify": "no"})
    assert summary.endswith("and the 2 later visits in the series")
    assert any(d.startswith("The series ENDS: 22nd Sep 99, 6th Oct 99") for d in details)
    assert any(d == "Customer: no message" for d in details)
    assert payload["later_ids"] == ["b2", "b3"] and payload["notify"] is False


def test_stage_cancel_refuses_past_missing_and_dead_bookings(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking_by_ref", lambda *a, **k: None)
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_cancel_booking("t1", None, {"booking_id": "BK-9"})
    assert "which booking" in str(e.value)
    monkeypatch.setattr(concierge_actions.db, "get_booking_by_ref",
                        lambda *a, **k: _series_booking(booking_date="2020-01-01"))
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_cancel_booking("t1", None, {"booking_id": "BK-1004"})
    assert "already passed" in str(e2.value)
    monkeypatch.setattr(concierge_actions.db, "get_booking_by_ref",
                        lambda *a, **k: _series_booking(status="cancelled"))
    with pytest.raises(concierge_actions.ActionError) as e3:
        concierge_actions._stage_cancel_booking("t1", None, {"booking_id": "BK-1004"})
    assert "nothing to cancel" in str(e3.value)


def test_exec_cancel_mirrors_the_diary_and_queues_one_note(monkeypatch):
    store = {"b1": _series_booking(), "b2": _series_booking(id="b2", booking_ref="BK-1005",
                                                           booking_date="2099-09-22")}
    statuses, register, freed, notes, events = [], [], [], [], []
    monkeypatch.setattr(concierge_actions.db, "get_booking", lambda bid, tid, client=None: store.get(bid))
    monkeypatch.setattr(concierge_actions.db, "set_booking_status",
                        lambda bid, st, tid, client=None: statuses.append((bid, st)))
    monkeypatch.setattr(concierge_actions.db, "log_agent_event", lambda *a, **k: events.append(a[2]))
    from app import calendar_sync, courses, hand_cancel, wallet

    async def _remove(tid, bid):
        return None
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _remove)
    monkeypatch.setattr(wallet, "apply_attendance",
                        lambda tid, row, status, **k: register.append((row["id"], status, k.get("logged_by"))) or {})

    async def _freed(tid, row, client=None):
        freed.append(row["id"])
    monkeypatch.setattr(courses, "on_place_freed", _freed)

    async def _note(tid, row, reason=None, client=None):
        notes.append((row["id"], reason))
        return {"ok": True, "queued": True}
    monkeypatch.setattr(hand_cancel, "on_hand_cancel", _note)

    out = asyncio.run(concierge_actions._exec_cancel_booking(
        "t1", None, {"booking_id": "b1", "later_ids": ["b2"], "scope": "series_from",
                     "notify": True, "reason": "moving away", "what": "Regular clean — Kris",
                     "when": "8th Sep 99 at 09:00", "date": "2099-09-08", "contact": "kris@x.com"}, ""))
    assert statuses == [("b1", "cancelled"), ("b2", "cancelled")]
    assert register == [("b1", "excused", "business"), ("b2", "excused", "business")]
    assert freed == ["b1", "b2"]
    assert len(notes) == 1 and notes[0][0] == "b1"
    assert notes[0][1] == "this also cancels the 1 later visit(s) in the series: 22nd Sep 99"
    assert "moving away" not in notes[0][1]      # the owner's reason never reaches the customer
    assert out["summary"] == ("Done — Regular clean — Kris on 8th Sep 99 at 09:00 is cancelled along "
                              "with 1 later visit. A note to the customer is waiting for your approval.")
    assert out["link"]["href"] == "/app/diary?date=2099-09-08"
    assert len(events) == 2


def test_exec_cancel_one_keeps_the_series_and_can_be_silent(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking", lambda bid, tid, client=None: _series_booking())
    monkeypatch.setattr(concierge_actions.db, "set_booking_status", lambda *a, **k: None)
    monkeypatch.setattr(concierge_actions.db, "log_agent_event", lambda *a, **k: None)
    from app import calendar_sync, courses, hand_cancel, wallet

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _noop)
    monkeypatch.setattr(courses, "on_place_freed", _noop)
    monkeypatch.setattr(wallet, "apply_attendance", lambda *a, **k: {})
    called = []

    async def _note(*a, **k):
        called.append(1)
    monkeypatch.setattr(hand_cancel, "on_hand_cancel", _note)
    out = asyncio.run(concierge_actions._exec_cancel_booking(
        "t1", None, {"booking_id": "b1", "later_ids": [], "scope": "one", "notify": False,
                     "what": "Regular clean — Kris", "when": "8th Sep 99 at 09:00",
                     "date": "2099-09-08", "series_next": "2099-09-22"}, ""))
    assert called == []
    assert out["summary"] == ("Done — Regular clean — Kris on 8th Sep 99 at 09:00 is cancelled; "
                              "the series carries on from 22nd Sep 99.")


def test_exec_cancel_reports_nothing_changed_when_already_gone(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking",
                        lambda bid, tid, client=None: _series_booking(status="cancelled"))
    with pytest.raises(concierge_actions.ActionError) as e:
        asyncio.run(concierge_actions._exec_cancel_booking(
            "t1", None, {"booking_id": "b1", "later_ids": [], "notify": True}, ""))
    assert "Nothing changed" in str(e.value)


def test_move_stager_resolves_a_reference_exactly(monkeypatch):
    seen = {}
    monkeypatch.setattr(concierge_actions.db, "get_booking_by_ref",
                        lambda tid, ref, client=None: seen.update(ref=ref) or None)
    with pytest.raises(concierge_actions.ActionError):
        concierge_actions._stage_move_booking("t1", None, {"booking_id": "BK-1078", "new_date": "2099-01-05",
                                                           "new_window": "pm"})
    assert seen["ref"] == "BK-1078"


# ── block_date: close a day, say what it strands ─────────────────────────────
def _cfg_with_blackouts(days):
    return {"windows": "day", "capacity_per_window": 3, "blackout_dates": list(days),
            "working_days": "mon,tue,wed,thu,fri"}


def test_block_kind_is_gated_on_the_diary_and_in_the_prompt():
    assert "block_date" in concierge_actions.allowed_kinds(False, True)
    assert "block_date" not in concierge_actions.allowed_kinds(True, False)
    assert "block_date" in concierge.BUSINESS_ACTION_KINDS
    assert "end_date" in concierge._ACTION_FIELDS
    assert "block_date (close the diary for a day" in concierge.build_business_prompt()


def test_stage_block_lists_the_bookings_it_strands(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_cfg_with_blackouts(["2099-12-25"])))
    monkeypatch.setattr(concierge_actions.db, "stays_running_into", lambda *a, **k: [])
    monkeypatch.setattr(concierge_actions.db, "list_active_bookings",
                        lambda tid, a, b, client=None: [{"id": "x", "status": "confirmed", "booking_date": "2099-09-18"},
                                                        {"id": "y", "status": "offered", "booking_date": "2099-09-18"}])
    monkeypatch.setattr(concierge_actions.db, "get_booking",
                        lambda bid, tid, client=None: {"id": bid, "booking_ref": "BK-1150",
                                                       "summary": "Regular clean — Anita", "booking_date": "2099-09-18"})
    summary, payload, details, _ = concierge_actions._stage_block_date(
        "t1", None, {"new_date": "2099-09-18", "reason": "first-aid course"})
    assert summary == "Block the diary on 18th Sep 99"
    assert details[0] == "Closed: 18th Sep 99 — nothing can be booked"
    assert details[1].startswith("Already booked on those days — 1 booking stay as they are") or \
           details[1].startswith("Already booked on those days — 1 booking")
    assert "BK-1150 — Regular clean — Anita (18th Sep 99)" in details[1]
    assert details[2] == "Reason: first-aid course"
    assert payload == {"days": ["2099-09-18"], "first": "2099-09-18", "last": "2099-09-18",
                       "when": "18th Sep 99", "stranded": 1, "reason": "first-aid course"}


def test_stage_block_handles_a_run_and_already_blocked_days(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_cfg_with_blackouts(["2099-09-21"])))
    monkeypatch.setattr(concierge_actions.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_actions.db, "stays_running_into", lambda *a, **k: [])
    summary, payload, details, _ = concierge_actions._stage_block_date(
        "t1", None, {"new_date": "2099-09-23", "end_date": "2099-09-21"})   # reversed on purpose
    assert summary == "Block the diary on 21st Sep 99 to 23rd Sep 99 (3 days)"
    assert payload["days"] == ["2099-09-22", "2099-09-23"]          # the 21st already was
    assert "(1 of those days already were)" in details[0]
    assert details[1] == "Nothing is booked on those days"
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_block_date("t1", None, {"new_date": "2099-09-21"})
    assert "already blocked" in str(e.value)
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_block_date("t1", None, {"new_date": "2020-01-01"})
    assert "already passed" in str(e2.value)
    with pytest.raises(concierge_actions.ActionError) as e3:
        concierge_actions._stage_block_date("t1", None, {"new_date": "2099-01-01", "end_date": "2099-03-01"})
    assert "up to 31" in str(e3.value)


def test_exec_block_appends_to_the_diary_blackouts(monkeypatch):
    saved, events = {}, []
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_cfg_with_blackouts(["2099-12-25"])))
    monkeypatch.setattr(concierge_actions.db, "upsert_scheduling_config",
                        lambda tid, cfg, client=None: saved.update(cfg))
    monkeypatch.setattr(concierge_actions.db, "log_agent_event", lambda *a, **k: events.append(a[2]))
    out = asyncio.run(concierge_actions._exec_block_date(
        "t1", None, {"days": ["2099-09-18"], "first": "2099-09-18", "last": "2099-09-18",
                     "when": "18th Sep 99", "stranded": 2, "reason": "first-aid course"}, ""))
    assert saved == {"blackout_dates": ["2099-09-18", "2099-12-25"]}
    assert out["summary"] == ("Done — the diary is blocked on 18th Sep 99. 2 bookings on those days "
                              "still need moving or cancelling.")
    assert out["link"]["href"] == "/app/diary?date=2099-09-18"
    assert "first-aid course" in events[0] and "2 booking(s)" in events[0]
    # a second Confirm of the same days changes nothing
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_cfg_with_blackouts(["2099-09-18", "2099-12-25"])))
    with pytest.raises(concierge_actions.ActionError):
        asyncio.run(concierge_actions._exec_block_date(
            "t1", None, {"days": ["2099-09-18"], "first": "2099-09-18", "when": "18th Sep 99"}, ""))


# ── update_contact: a field by its label, or a dated note ────────────────────
# (the executor's writes are covered in tests/test_record_edits.py — one code
#  path with the contact page's pencil, 2026-09-06)
_DEFS = [{"entity": "contact", "key": "access_arrangements", "label": "Access arrangements",
          "field_type": "text"},
         {"entity": "contact", "key": "regular_cleaner", "label": "Regular cleaner",
          "field_type": "dropdown", "options": ["Sam", "Priya", "Lou"]},
         {"entity": "deal", "key": "x", "label": "Deal thing", "field_type": "text"}]


def _sophie():
    return {"id": "c7", "name": "Sophie Lindqvist", "email": "sophie@x.com",
            "custom_data": {"access_arrangements": "Sophie works from home — knock", "regular_cleaner": "Sam"}}


def _update_fakes(monkeypatch, contacts):
    from app import concierge_reads
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None: contacts)
    monkeypatch.setattr(concierge_actions.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: _DEFS)


def test_update_kind_is_pro_and_in_the_prompt():
    assert "update_contact" in concierge_actions.allowed_kinds(True, False)
    assert "update_contact" not in concierge_actions.allowed_kinds(False, True)
    assert "update_contact" in concierge.BUSINESS_ACTION_KINDS
    assert "update_contact (write to ONE customer's record" in concierge.build_business_prompt()


def test_prompt_makes_free_text_a_note_and_names_the_followup_fields():
    # Wave 3a (N12, wave 1 re-test A2): a field label the model had never seen was
    # refused by the stager, and a follow-up arrived with no contact to resolve.
    p = concierge.build_business_prompt(full_qa=True)
    for phrase in ("leave title EMPTY", "you do not know its fields, so it is a note",
                   "Never invent a field label", "create_followup (contact = their email or name",
                   "never without the contact"):
        assert phrase in p, phrase
    assert "update_contact (write to ONE customer's record" in p      # the old pin, verbatim
    starter = concierge.build_business_prompt(full_qa=False)
    assert "create_followup (contact" not in starter and "update_contact" not in starter


def test_stage_update_sets_a_field_by_its_label(monkeypatch):
    _update_fakes(monkeypatch, [_sophie()])
    summary, payload, details, _ = concierge_actions._stage_update_contact(
        "t1", None, {"contact": "Sophie", "title": "access arrangements",
                     "content": "Knock twice — Sophie works from home"})
    assert summary == ("Update Sophie Lindqvist — Access arrangements: Sophie works from home — knock "
                       "→ Knock twice — Sophie works from home")
    assert payload["mode"] == "field" and payload["key"] == "access_arrangements"
    assert payload["value"] == "Knock twice — Sophie works from home"
    assert any("old value is replaced" in d for d in details)


def test_stage_update_validates_dropdowns_unknown_fields_and_no_ops(monkeypatch):
    _update_fakes(monkeypatch, [_sophie()])
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_update_contact(
            "t1", None, {"contact": "sophie@x.com", "title": "Regular cleaner", "content": "Dave"})
    assert "choose one of: Sam, Priya, Lou" in str(e.value)
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_update_contact(
            "t1", None, {"contact": "Sophie", "title": "Shoe size", "content": "7"})
    assert "no 'Shoe size' field" in str(e2.value) and "Access arrangements" in str(e2.value)
    with pytest.raises(concierge_actions.ActionError) as e3:
        concierge_actions._stage_update_contact(
            "t1", None, {"contact": "Sophie", "title": "Regular cleaner", "content": "Sam"})
    assert "already 'Sam'" in str(e3.value)
    with pytest.raises(concierge_actions.ActionError) as e4:
        concierge_actions._stage_update_contact("t1", None, {"contact": "Sophie", "content": ""})
    assert "What should the record say" in str(e4.value)


def test_stage_update_note_hints_at_a_field(monkeypatch):
    _update_fakes(monkeypatch, [_sophie()])
    summary, payload, details, _ = concierge_actions._stage_update_contact(
        "t1", None, {"contact": "Sophie", "content": "She wants us to knock twice — access via the side gate"})
    assert summary == "Add a note to Sophie Lindqvist's record"
    assert payload["mode"] == "note" and payload["contact_id"] == "c7"
    assert any("'Access arrangements' field" in d for d in details)


def test_stage_update_asks_which_contact(monkeypatch):
    _update_fakes(monkeypatch, [_sophie(), {**_sophie(), "id": "c8", "name": "Sophie Khan", "email": "sk@x.com"}])
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_update_contact("t1", None, {"contact": "Sophie", "content": "x"})
    assert "which one" in str(e.value) and "sk@x.com" in str(e.value)


# ── create_followup names the contact the way update_contact does (live 2026-10-14) ──
_SOPHIE_UUID = "6ad58130-1d01-4ce6-9cef-77f7db120018"


def test_stage_followup_takes_the_contact_by_email(monkeypatch):
    # live 2026-10-14: the brain sent contact = the email and no contact_id, and
    # every follow-up was refused with "couldn't pin down which contact"
    _update_fakes(monkeypatch, [_sophie()])
    summary, payload, details, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact": "sophie@x.com", "title": "Call Sophie", "due_date": "2026-10-16"})
    assert summary == "Follow-up for Sophie Lindqvist — due 2026-10-16"
    assert payload == {"contact_id": "c7", "contact_name": "Sophie Lindqvist",
                       "title": "Call Sophie", "due_date": "2026-10-16"}
    assert details[:2] == ["Task: Call Sophie", "Due: Friday 16 October 2026"]
    assert details[2].startswith("Saved to: ") and details[2].endswith("record in the CRM, as a task")


def test_stage_followup_takes_the_contact_by_name_and_names_candidates(monkeypatch):
    _update_fakes(monkeypatch, [_sophie()])
    _, payload, _, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact": "Sophie", "due_date": "2026-10-16"})
    assert payload["contact_id"] == "c7" and payload["title"] == "Follow up with Sophie Lindqvist"
    _update_fakes(monkeypatch, [_sophie(), {**_sophie(), "id": "c8", "name": "Sophie Khan", "email": "sk@x.com"}])
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_create_followup("t1", None, {"contact": "Sophie", "due_date": "2026-10-16"})
    assert "which one" in str(e.value) and "sk@x.com" in str(e.value)
    _update_fakes(monkeypatch, [])
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_create_followup("t1", None, {"contact": "Nobody", "due_date": "2026-10-16"})
    assert "couldn't find that contact" in str(e2.value)


def test_stage_followup_still_takes_a_contact_id_and_checks_the_date_first(monkeypatch):
    from app import concierge_reads
    looked_up = []
    monkeypatch.setattr(concierge_actions.db, "crm_get_contact",
                        lambda cid, tid, client=None: _sophie() if cid == _SOPHIE_UUID else None)
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None: looked_up.append(q) or [])
    _, payload, _, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact_id": _SOPHIE_UUID, "title": "Call", "due_date": "2026-10-16"})
    assert payload["contact_id"] == _SOPHIE_UUID and not looked_up
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_create_followup(
            "t1", None, {"contact_id": "6ad58130-1d01-4ce6-9cef-77f7db120099", "due_date": "2026-10-16"})
    assert "couldn't find that contact" in str(e.value)
    with pytest.raises(concierge_actions.ActionError) as e2:
        concierge_actions._stage_create_followup("t1", None, {"contact": "Sophie", "due_date": "Friday"})
    assert "I need a real date" in str(e2.value)
    assert not looked_up


def test_stage_followup_strips_characters_that_break_the_contact_lookup(monkeypatch):
    from app import concierge_reads
    seen = []
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None: seen.append(q) or [_sophie()])
    _, payload, _, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact": "Sophie, the (customer)", "due_date": "2026-10-16"})
    assert payload["contact_id"] == "c7"
    assert len(seen) == 1 and not any(ch in seen[0] for ch in "%,()*\\")


def test_exec_followup_creates_the_task_for_the_resolved_contact(monkeypatch):
    _update_fakes(monkeypatch, [_sophie()])
    _, payload, _, _ = concierge_actions._stage_create_followup(
        "t1", None, {"contact": "sophie@x.com", "title": "Call Sophie", "due_date": "2026-10-16"})
    made = {}
    monkeypatch.setattr(concierge_actions.db, "crm_create_task",
                        lambda tid, **kw: made.update(kw) or {"id": "k1"})
    monkeypatch.setattr(concierge_actions.db, "log_agent_event", lambda *a, **k: None)
    out = asyncio.run(concierge_actions._exec_create_followup("t1", None, payload, ""))
    assert made == {"title": "Call Sophie", "due_date": "2026-10-16",
                    "contact_id": "c7", "source": "manual"}
    assert out["summary"] == "Done — follow-up for Sophie Lindqvist on 2026-10-16."


# ── bulk: several changes on one card, one Confirm (option 1, 2026-09-06) ────
def test_bulk_is_a_kind_everywhere_it_needs_to_be():
    assert "bulk" in concierge.BUSINESS_ACTION_KINDS
    assert "bulk" in concierge_actions.allowed_kinds(False, False)
    schema = concierge.answer_tool(actions=True)["input_schema"]["properties"]["action"]
    assert "items" in schema["properties"]
    assert "bulk" not in schema["properties"]["items"]["items"]["properties"]["kind"]["enum"]
    assert "bulk (the SAME change across several records" in concierge.build_business_prompt()
    # live 2026-09-06: rule 1 still said "bulk changes — you cannot do" and the
    # model queued one reminder "at a time" with the bulk kind sitting right there
    p = concierge.build_business_prompt()
    assert "bulk changes — you" not in p
    assert "never say you can only do one at a time" in p
    assert "never a table" in p
    assert "bulk (the SAME change" in concierge.build_business_prompt(full_qa=False)


def test_parse_business_carries_bulk_items_and_drops_junk():
    text = json.dumps({"reply": "Three reminders ready.", "links": [], "action": {
        "kind": "bulk", "items": [
            {"kind": "invoice_reminder_draft", "invoice_id": "INV-1", "draft_text": "Hi"},
            {"kind": "bulk", "items": []},                       # nested bulk dropped
            {"kind": "nonsense"},                                # unknown dropped
            "not a dict",
            {"kind": "cancel_booking", "booking_id": "BK-2", "scope": "one"}]}})
    reply, links, action = concierge._parse_business(text)
    assert action["kind"] == "bulk"
    assert [i["kind"] for i in action["items"]] == ["invoice_reminder_draft", "cancel_booking"]
    assert action["items"][0]["invoice_id"] == "INV-1" and action["items"][1]["scope"] == "one"


def _bulk_stagers(monkeypatch):
    def ok_stager(tid, client, it):
        return (f"Chase {it['invoice_id']}", {"id": it["invoice_id"]}, ["detail"], "Hi there")

    def bad_stager(tid, client, it):
        raise concierge_actions.ActionError("That invoice is already paid.")
    monkeypatch.setitem(concierge_actions._STAGERS, "invoice_reminder_draft", ok_stager)
    monkeypatch.setitem(concierge_actions._STAGERS, "invoice_send", bad_stager)
    created = {}
    monkeypatch.setattr(concierge_actions.db, "create_concierge_proposal",
                        lambda tid, kind, summary, payload, user_id=None, action=None, client=None:
                        created.update(kind=kind, summary=summary, payload=payload) or "prop-b")
    return created


def test_stage_bulk_keeps_failed_items_on_the_card_unticked(monkeypatch):
    created = _bulk_stagers(monkeypatch)
    card = concierge_actions.stage("t1", None, {"kind": "bulk", "items": [
        {"kind": "invoice_reminder_draft", "invoice_id": "INV-1"},
        {"kind": "invoice_send", "invoice_id": "INV-2"},
        {"kind": "invoice_reminder_draft", "invoice_id": "INV-3"},
        {"kind": "update_deal_stage", "deal_id": "d1"}]}, full_qa=False, scheduling_on=False)
    assert card["kind"] == "bulk" and card["proposal_id"] == "prop-b"
    assert card["summary"] == "2 changes ready — 2 can't be done"
    oks = [i["ok"] for i in card["items"]]
    assert oks == [True, False, True, False]
    assert card["items"][1]["error"] == "That invoice is already paid."
    assert card["items"][1]["summary"] == "invoice_send — INV-2"
    assert "Pro plan" in card["items"][3]["error"] or "can't prepare" in card["items"][3]["error"]
    assert card["items"][0]["draft_text"] == "Hi there"
    assert created["kind"] == "bulk" and [i["ok"] for i in created["payload"]["items"]] == oks
    assert card["bulk_kinds"] == ["invoice_reminder_draft"]


def test_stage_bulk_of_one_is_that_action_and_none_is_refused(monkeypatch):
    created = _bulk_stagers(monkeypatch)
    card = concierge_actions.stage("t1", None, {"kind": "bulk", "items": [
        {"kind": "invoice_reminder_draft", "invoice_id": "INV-9"}]}, full_qa=False, scheduling_on=False)
    assert card["kind"] == "invoice_reminder_draft" and "items" not in card
    assert created["kind"] == "invoice_reminder_draft"
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions.stage("t1", None, {"kind": "bulk", "items": [
            {"kind": "invoice_send", "invoice_id": "INV-2"}]}, full_qa=False, scheduling_on=False)
    assert "None of those could be prepared" in str(e.value) and "already paid" in str(e.value)
    with pytest.raises(concierge_actions.ActionError):
        concierge_actions.stage("t1", None, {"kind": "bulk", "items": []}, full_qa=False, scheduling_on=False)


def test_every_card_kind_is_approve_first_today():
    kinds = [k for k in concierge.BUSINESS_ACTION_KINDS if k]
    assert "bulk" in kinds and "update_contact" in kinds
    assert all(concierge_actions.approval_mode(k) == "confirm" for k in kinds)


def test_every_staged_card_names_its_approval_mode(monkeypatch):
    _bulk_stagers(monkeypatch)
    reminder = {"kind": "invoice_reminder_draft", "invoice_id": "INV-4"}
    plain = concierge_actions.stage("t1", None, reminder, full_qa=False, scheduling_on=False)
    one = concierge_actions.stage("t1", None, {"kind": "bulk", "items": [reminder]},
                                  full_qa=False, scheduling_on=False)
    many = concierge_actions.stage("t1", None, {"kind": "bulk", "items": [
        reminder, {"kind": "invoice_reminder_draft", "invoice_id": "INV-5"}]},
        full_qa=False, scheduling_on=False)
    assert (plain["kind"], one["kind"], many["kind"]) == ("invoice_reminder_draft", "invoice_reminder_draft", "bulk")
    assert plain["approval"] == one["approval"] == many["approval"] == "confirm"


def test_exec_bulk_runs_the_ticked_items_and_reports_each(monkeypatch):
    ran = []

    async def good(tid, client, payload, edited):
        ran.append(payload["id"])
        return {"summary": f"Sent {payload['id']}.", "link": {"label": "Invoices", "href": "/app/invoices"}}

    async def bad(tid, client, payload, edited):
        raise concierge_actions.ActionError("The slot filled up.")
    monkeypatch.setitem(concierge_actions._EXECUTORS, "invoice_reminder_draft", good)
    monkeypatch.setitem(concierge_actions._EXECUTORS, "move_booking", bad)
    payload = {"items": [
        {"kind": "invoice_reminder_draft", "ok": True, "payload": {"id": "INV-1"}},
        {"kind": "invoice_send", "ok": False, "payload": {}},
        {"kind": "invoice_reminder_draft", "ok": True, "payload": {"id": "INV-3"}},
        {"kind": "move_booking", "ok": True, "payload": {"id": "b1"}}]}
    out = asyncio.run(concierge_actions.execute("t1", None, "bulk", payload, selected=[0, 3]))
    assert ran == ["INV-1"]
    assert out["state"] == "unknown" and out["summary"].startswith("1 of 2 items completed.")
    assert out["link"] == {"label": "Invoices", "href": "/app/invoices"}
    assert [(r["index"], r["ok"]) for r in out["results"]] == [(0, True), (3, False)]
    # untouched selection = every prepared item
    ran.clear()
    out2 = asyncio.run(concierge_actions.execute("t1", None, "bulk", payload))
    assert ran == ["INV-1", "INV-3"] and out2["summary"].startswith("2 of 3 items completed.")
    with pytest.raises(concierge_actions.ActionError) as e:
        asyncio.run(concierge_actions.execute("t1", None, "bulk", payload, selected=[1]))
    assert "Nothing was ticked" in str(e.value)


def test_execute_route_passes_selected_and_returns_results(client, monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda pid, tid, max_age_s=300, client=None, **kw:
                        {"id": pid, "result": execution_result(pid, kw.get("execution_input")), "kind": "bulk", "payload": {"items": []}, "user_id": "u1"})
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal", lambda *a, **k: True)
    seen = {}

    async def fake_exec(tid, cl, kind, payload, edited_draft_text=None, selected=None):
        seen.update(kind=kind, selected=selected)
        return {"summary": "Done — 2 of 2 ran.", "link": None,
                "results": [{"index": 0, "ok": True, "state": "completed", "summary": "a"}, {"index": 2, "ok": True, "state": "completed", "summary": "b"}]}
    monkeypatch.setattr(concierge_routes.concierge_actions, "execute", fake_exec)
    res = client.post("/concierge/business/execute", json={"proposal_id": "prop-b", "selected": [0, 2]})
    assert res.status_code == 200
    data = res.json()
    assert seen == {"kind": "bulk", "selected": [0, 2]}
    assert data["ok"] is True and len(data["results"]) == 2


def test_service_failure_refunds_the_turn(client, monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda tid, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda tid, client=None: {"name": "K"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: None)
    refunded = []
    monkeypatch.setattr(concierge_routes.db, "refund_concierge_turn",
                        lambda tid, client=None: refunded.append(tid))

    class RateLimitError(Exception):
        pass

    async def boom(*a, **k):
        raise RateLimitError("credit balance is too low")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", boom)
    res = client.post("/concierge/business", json={"question": "hi"})
    done = [json.loads(l) for l in res.text.strip().splitlines() if json.loads(l)["type"] == "done"][0]
    assert "on our side" in done["reply"]
    assert refunded == ["t1"]
    # an ordinary processing error keeps the old line and costs the turn
    refunded.clear()

    async def boom2(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", boom2)
    res2 = client.post("/concierge/business", json={"question": "hi"})
    done2 = [json.loads(l) for l in res2.text.strip().splitlines() if json.loads(l)["type"] == "done"][0]
    assert "different way" in done2["reply"] and refunded == []


def test_card_captions_are_kind_aware_and_bulk_renders_on_both_surfaces():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "src/app" / "static" / "concierge_panel.js").read_text(encoding="utf-8")
    js = (root / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    # The captions under the text box are gone (Kris 2026-09-24): the card's
    # head says what Confirm does; the two never contradict because only one speaks.
    assert "Confirm sends this exact email from your mailbox" not in page
    assert "sends this exact text to your approval queue" not in page
    # The Concierge is the one surface with cards since 2026-09-23.
    assert "selectedIdx()" in page and "Can't be done: " in page and "bulk" in page
    assert "selectedIdx()" not in js

from tests.booking_fixtures import carried_config


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
