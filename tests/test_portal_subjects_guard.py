"""Portal, hot tub run 2026-09-04: a booking about a DIFFERENT dependant on the
same morning is not a duplicate; a refusal names what the existing booking is
for; additions to an existing visit are handed to the owner; and a reply
may not claim a change when every tool
refused.

Run: python -m pytest tests/test_portal_subjects_guard.py -q
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import portal_chat, portal_data


# ── the claim guard ──────────────────────────────────────────────────────────
def test_claims_a_change_matches_the_live_sentences_and_not_answers():
    assert portal_chat.claims_a_change("I've added The Byre's HSG282 water test to the same visit as The Dairy routine service.")
    assert portal_chat.claims_a_change("BK-1003 already covers the Granary — it's in the diary alongside the Dairy.")
    assert portal_chat.claims_a_change("Done — that's booked. BK-1003, routine service on The Dairy.")
    assert not portal_chat.claims_a_change("BK-1003 on Tuesday 6 October is for The Dairy only. Shall I add the Byre to that visit?")
    assert not portal_chat.claims_a_change("A trim is £35, or £30 each for three or more.")


def _text(reply):
    return SimpleNamespace(type="text", text=json.dumps({"reply": reply}))


def _tool(name, args, id_="tu1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def _msg(content, stop):
    return SimpleNamespace(content=content, stop_reason=stop, model="m",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def _run(monkeypatch, scripted, executor):
    calls = []

    async def fake_create(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})
        return scripted.pop(0)

    monkeypatch.setattr(portal_chat.llm, "create", fake_create)
    monkeypatch.setattr(portal_chat, "client", lambda: None)
    reply, _t, _m = asyncio.run(portal_chat.answer_portal(
        "can you do the Byre's test on the same visit?", [], system="s",
        tools=[{"name": "create_booking"}], tool_executor=executor, tenant_id="t1"))
    return reply, calls


def test_a_claimed_change_over_a_refusal_is_sent_back_once(monkeypatch):
    async def executor(name, args):
        return {"error": "already_booked", "booking_ref": "BK-1003", "about": ["The Dairy"],
                "message": "already"}

    reply, calls = _run(monkeypatch, [
        _msg([_tool("create_booking", {"booking_date": "2026-10-06", "window": "am"})], "tool_use"),
        _msg([_text("I've added The Byre's test to the same visit. Confirmation on its way.")], "end_turn"),
        _msg([_text("BK-1003 on Tuesday 6 October is for The Dairy only. Adding to that visit is not supported here. I can pass the request to the business.")], "end_turn"),
    ], executor)
    assert reply.startswith("BK-1003 on Tuesday 6 October is for The Dairy only")
    nudge = calls[2]["messages"][-1]
    assert nudge["role"] == "user"
    assert "REFUSED" in nudge["content"] and "BK-1003 is for The Dairy" in nudge["content"]


def test_a_claimed_change_after_a_success_stands(monkeypatch):
    async def executor(name, args):
        return {"added": True, "booking_ref": "BK-1003", "subjects": ["The Byre"]}

    reply, calls = _run(monkeypatch, [
        _msg([_tool("create_booking", {"booking_date": "2026-10-06", "window": "am"})], "tool_use"),
        _msg([_text("I've added The Byre's test to BK-1003 — one visit, Tuesday morning.")], "end_turn"),
    ], executor)
    assert reply.startswith("I've added The Byre's test")
    assert len(calls) == 2


# ── the subjects-aware duplicate guard ───────────────────────────────────────
class _Session(SimpleNamespace):
    pass


def _session():
    return _Session(tenant_id="t1", contact={"id": "c1", "email": "kris@x.co", "name": "Kris"},
                    max_bookings=0, trace_session="s1")


ROWS = [{"id": "b3", "booking_ref": "BK-1003", "status": "confirmed", "booking_date": "2026-10-06",
         "window": "am", "details": {"Service": "Routine service", "Hot tubs": "The Dairy"},
         "summary": "Routine service — The Dairy"}]


def _guard_env(monkeypatch):
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda t, e: list(ROWS))
    monkeypatch.setattr(portal_data.db, "subjects_for_bookings",
                        lambda t, ids: {"b3": [{"id": "i-dairy", "name": "The Dairy"}]})
    monkeypatch.setattr(portal_data, "date", SimpleNamespace(today=lambda: __import__("datetime").date(2026, 9, 4)))


def test_a_different_dependant_on_the_same_morning_is_not_a_duplicate(monkeypatch):
    _guard_env(monkeypatch)
    out = portal_data._booking_block(_session(), {"booking_date": "2026-10-06", "window": "am",
                                                  "subjects": [{"name": "The Granary", "group": "Hot tub"}]},
                                     "service")
    assert out is None


def test_the_same_dependant_is_refused_and_the_refusal_says_what_the_visit_is_for(monkeypatch):
    _guard_env(monkeypatch)
    out = portal_data._booking_block(_session(), {"booking_date": "2026-10-06", "window": "am",
                                                  "subjects": [{"name": "the dairy", "group": "Hot tub"}]},
                                     "service")
    assert out["error"] == "already_booked" and out["booking_ref"] == "BK-1003"
    assert out["about"] == ["The Dairy"]
    assert "It is for The Dairy" in out["message"] and "escalate_to_business" in out["message"]
    assert "add_to_booking" not in out["message"]


def test_no_subjects_named_keeps_the_old_date_window_rule(monkeypatch):
    _guard_env(monkeypatch)
    out = portal_data._booking_block(_session(), {"booking_date": "2026-10-06", "window": "am"}, "service")
    assert out["error"] == "already_booked"


@pytest.mark.parametrize("identified", [False, True])
@pytest.mark.parametrize("diary", [False, True])
@pytest.mark.parametrize("args", [
    {"booking_ref": "BK-1003", "subjects": [{"name": "The Byre", "group": "Hot tub"}]},
    {"booking_ref": "BK-9999", "service": "Water test"},
    {"booking_ref": "BK-1003", "subjects": [{"name": "Known subject", "fields": {}}]},
])
def test_add_request_is_unsupported_through_real_dispatch_without_writes(monkeypatch, identified, diary, args):
    def forbidden(*a, **k):
        pytest.fail("unsupported addition must not read or mutate booking/customer data")
    for name in ("bookings_for_contact", "list_requirement_groups", "upsert_requirement_instance",
                 "link_booking_subjects", "update_booking_details", "insert_stay_booking"):
        monkeypatch.setattr(portal_data.db, name, forbidden)
    session = portal_data.PortalSession(tenant_id="t1", business_name="Archive",
        scheduling_on=diary, contact={"id": "c1", "email": "a@example.invalid"} if identified else None)
    out = asyncio.run(portal_data.execute(session, "add_to_booking", args))
    assert out["error"] == "unsupported"
    assert "Nothing changed" in out["message"] and "escalate_to_business" in out["message"]
    assert "add_to_booking" not in portal_data.TOOL_NAMES
    assert "add_to_booking" not in portal_data._ASYNC


@pytest.mark.parametrize("shape", ["slots", "services", "stays_appointments", "email_only"])
def test_add_tool_is_absent_from_every_real_catalogue(monkeypatch, shape):
    from tests.agnostic_harness import World, fixtures
    world = World(monkeypatch, fixtures()[shape])
    session = portal_data.PortalSession(tenant_id="t1", business_name="Archive",
        scheduling_on=shape != "email_only", contact={"id": "c1", "email": "a@example.invalid"})
    tools = portal_data.tools_for(session.scheduling_on, session)
    assert "add_to_booking" not in str(tools)
    assert "escalate_to_business" in {t["name"] for t in tools}
    prompt = portal_chat.build_portal_prompt(business_name="Archive", scheduling_on=session.scheduling_on)
    assert "not supported here" in prompt and "Do not create a second booking" in prompt
    assert not world.writes


def test_portal_loop_relays_unsupported_add_and_corrects_false_success(monkeypatch):
    session = portal_data.PortalSession(tenant_id="t1", business_name="Archive", scheduling_on=True,
        contact={"id": "c1", "email": "a@example.invalid"})
    async def execute(name, args):
        return await portal_data.execute(session, name, args)
    reply, calls = _run(monkeypatch, [
        _msg([_tool("add_to_booking", {"booking_ref": "BK-1003"})], "tool_use"),
        _msg([_text("I've added the extra service to BK-1003.")], "end_turn"),
        _msg([_text("Adding to that booking is not supported here. Nothing changed. I can pass your request to the business.")], "end_turn"),
    ], execute)
    assert "not supported" in reply and "Nothing changed" in reply
    assert "unsupported" in str(calls[1]["messages"])
    nudge = calls[2]["messages"][-1]["content"]
    assert "escalate_to_business" in nudge and "call add_to_booking" not in nudge
