"""When it's full, the business chooses (docs/plan-when-its-full.md, Kris 2026-09-23).

K11: Daniel asked for 6–8 Nov, the last space was Nadia's, and the agent
offered AND held 9–11 Nov. He might only be free on his own dates. Now:

  * `other_dates` — hold (the default, exactly as before) / offer (name the
    alternative, reserve nothing) / off (no alternatives);
  * the waiting list takes a STAY — the exact span asked for — not just a class;
  * a hold that lapses frees its place to whoever is waiting, like a cancel.

Run: python -m pytest tests/test_when_its_full.py -q
"""
from tests.booking_fixtures import carried_config
import asyncio
from datetime import date, datetime, timezone

from app import agent, courses, scheduling, slotgrid
from tests.test_stay_executor import CFG, NOW, _stay_row

FULL = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
ASK = {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00"}


def _exec(monkeypatch, existing, other="hold", waitlist=False, joined=None):
    cfg = {**CFG, "courses": {"other_dates": other, "waitlist": waitlist}}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing)
    monkeypatch.setattr(scheduling.db, "insert_stay_booking",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may be reserved")))
    if joined is not None:
        def fake_join(tenant_id, contact, contact_id, key, **kw):
            joined.append(key)
            return {"joined": True, "position": 1, "row": {"series_key": key}}
        monkeypatch.setattr(courses, "join", fake_join)
    execute, _ = scheduling.make_executor("t1", contact="daniel@x.co", stay=True, now=NOW)
    return execute


# ── the setting ───────────────────────────────────────────────────────────────
def test_the_default_is_todays_behaviour_and_says_nothing_new():
    assert courses.clean({})["other_dates"] == "hold"
    assert courses.clean({"other_dates": "nonsense"})["other_dates"] == "hold"
    assert courses.render_for_prompt({}) == ""                       # no tenant's prompt changes
    assert courses.face_value(courses.clean({})) == "Off"


def test_with_both_on_the_list_leads_and_the_dates_follow():
    txt = courses.render_for_prompt({"waitlist": True})
    assert "exactly what they asked for FIRST" in txt and "nearest free alternative too" in txt
    assert "arrival and departure" in txt


# ── the full answer follows the setting ──────────────────────────────────────
def test_hold_is_unchanged(monkeypatch):
    out = _exec(monkeypatch, FULL)("check_availability", dict(ASK))
    assert out["available"] is False and "reserve it with offer_stay" in out["message"]
    assert out["nearest_arrival"] == "2026-07-05T08:00"


def test_offer_names_the_alternative_and_reserves_nothing(monkeypatch):
    execute = _exec(monkeypatch, FULL, other="offer")
    out = execute("check_availability", dict(ASK))
    assert out["nearest_arrival"] == "2026-07-05T08:00"
    assert "do NOT reserve it" in out["message"]
    refused = execute("offer_stay", {"stays": [{"arrival": out["nearest_arrival"],
                                                "departure": out["nearest_departure"]}]})
    assert refused["error"] == "offers_not_held"                    # insert would have raised


def test_off_offers_no_alternative(monkeypatch):
    execute = _exec(monkeypatch, FULL, other="off")
    out = execute("check_availability", dict(ASK))
    assert out["available"] is False and "nearest_arrival" not in out
    assert "does not offer other dates" in out["message"]
    held = execute("hold_booking", {**ASK, "summary": "Boarding for Mabel"})
    assert held["error"] == "full" and "nearest_arrival" not in held
    assert execute("offer_stay", {"stays": [ASK]})["error"] == "no_other_dates"


# ── a stay waits for its exact span ──────────────────────────────────────────
def test_stay_keys_label_and_dates():
    key = courses.stay_key("", "2026-11-06T08:00", "2026-11-08T17:00")
    assert key == "stay||2026-11-06T08:00|2026-11-08T17:00"
    assert courses.stay_dates(key) == (date(2026, 11, 6), date(2026, 11, 8))
    assert courses.label(key) == "6th Nov 26 to 8th Nov 26"
    assert courses.next_dates(key, date(2026, 11, 1)) == [date(2026, 11, 6)]
    assert courses.next_dates(key, date(2026, 11, 7)) == []          # arrival passed: moot


def test_joining_a_full_stay_lists_the_span_asked_for(monkeypatch):
    joined = []
    execute = _exec(monkeypatch, FULL, waitlist=True, joined=joined)
    monkeypatch.setattr(scheduling.db, "list_requirement_groups", lambda *a, **k: [], raising=False)
    out = execute("join_waitlist", dict(ASK))
    assert out.get("joined") is True, out
    assert joined == ["stay||2026-06-30T12:00|2026-07-02T16:00"]


def test_a_stay_with_room_is_not_a_list(monkeypatch):
    execute = _exec(monkeypatch, [], waitlist=True, joined=[])
    assert execute("join_waitlist", dict(ASK))["error"] == "not_full"


def test_the_full_check_tells_the_agent_to_lead_with_the_list(monkeypatch):
    out = _exec(monkeypatch, FULL, waitlist=True)("check_availability", dict(ASK))
    assert "exactly these dates FIRST" in out["waiting_list"]
    assert "join_waitlist with this arrival and departure" in out["waiting_list"]


def test_room_for_a_stay_is_every_night_of_it(monkeypatch):
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: FULL)
    arr, dep = datetime(2026, 6, 30, 12, 0), datetime(2026, 7, 2, 16, 0)
    assert scheduling.stay_has_room("t1", arr, dep, cfg_row=CFG) is False
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: FULL[:9])
    assert scheduling.stay_has_room("t1", arr, dep, cfg_row=CFG) is True


def test_has_room_sends_a_stay_key_to_the_stay_check(monkeypatch):
    seen = []
    monkeypatch.setattr(scheduling, "stay_has_room",
                        lambda t, a, d, r, cfg_row=None, client=None: seen.append((a, d, r)) or True)
    key = courses.stay_key("Kennel", "2026-11-06T08:00", "2026-11-08T17:00")
    assert courses.has_room("t1", key, date(2026, 11, 1), cfg_row=CFG) is True
    assert seen == [(datetime(2026, 11, 6, 8, 0), datetime(2026, 11, 8, 17, 0), "Kennel")]


def test_a_freed_stay_opens_only_the_lists_it_overlaps(monkeypatch):
    rows = [{"series_key": courses.stay_key("", "2026-11-06T08:00", "2026-11-08T17:00")},
            {"series_key": courses.stay_key("", "2026-11-20T08:00", "2026-11-22T17:00")},
            {"series_key": "date||2026-11-07|am"}]
    monkeypatch.setattr(courses, "list_rows", lambda *a, **k: rows)
    freed = _stay_row(datetime(2026, 11, 5, 8, 0), datetime(2026, 11, 9, 17, 0))
    assert courses.stay_keys_overlapping("t1", freed) == [rows[0]["series_key"]]
    before = _stay_row(datetime(2026, 11, 1, 8, 0), datetime(2026, 11, 5, 17, 0))
    assert courses.stay_keys_overlapping("t1", before) == []


def test_the_diary_shows_a_stay_waiter_on_every_day_of_the_span(monkeypatch):
    row = {"series_key": courses.stay_key("", "2026-11-06T08:00", "2026-11-08T17:00"),
           "status": "waiting", "created_at": "2026-09-23T10:00:00"}
    monkeypatch.setattr(courses, "list_rows", lambda *a, **k: [row])
    got = courses.waiting_by_day("t1", ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"])
    assert sorted(got) == ["2026-11-06", "2026-11-07", "2026-11-08"]


# ── a lapsed hold frees its place ────────────────────────────────────────────
class _Q:
    """Just enough of the Supabase query chain for the release update."""

    def __init__(self, log):
        self.log = log

    def table(self, name):
        self.log.append(("table", name))
        return self

    def update(self, fields):
        self.log.append(("update", fields["status"]))
        return self

    def eq(self, *a):
        return self

    def in_(self, col, ids):
        self.log.append(("ids", list(ids)))
        return self

    def execute(self):
        return type("R", (), {"data": []})()


def test_a_lapsed_hold_is_released_and_offered_to_the_list(monkeypatch):
    lapsed = {**_stay_row(datetime(2026, 11, 6, 8, 0), datetime(2026, 11, 8, 17, 0)),
              "id": "b1", "booking_ref": "BK-1065", "status": "offered", "contact": "nadia@x.co"}
    log, freed = [], []
    monkeypatch.setattr(courses, "_lapsed_holds", lambda *a, **k: [lapsed])
    monkeypatch.setattr(courses, "_c", lambda client=None: _Q(log))
    monkeypatch.setattr(courses.db, "log_agent_event", lambda *a, **k: None)

    async def fake_freed(tenant_id, booking, **k):
        freed.append(booking)
        return [{"ok": True}]
    monkeypatch.setattr(courses, "on_place_freed", fake_freed)
    out = asyncio.run(courses.release_lapsed_holds("t1", now=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)))
    assert out == [{"ok": True}]
    assert ("update", "cancelled") in log and ("ids", ["b1"]) in log
    assert freed[0]["booking_ref"] == "BK-1065"
    # the freed booking is treated as a place given back, not a series gap
    assert courses.place_freed_by_cancel(freed[0], []) is True


def test_nothing_lapsed_touches_nothing(monkeypatch):
    monkeypatch.setattr(courses, "_lapsed_holds", lambda *a, **k: [])
    monkeypatch.setattr(courses, "_c", lambda client=None: (_ for _ in ()).throw(AssertionError("no write")))
    assert asyncio.run(courses.release_lapsed_holds("t1")) == []


def test_the_half_hourly_sweep_only_visits_tenants_with_a_list(monkeypatch):
    seen = []
    monkeypatch.setattr(courses, "tenants_on", lambda client=None: [
        ("with", {"courses": {"waitlist": True}}), ("courses-only", {"courses": {"courses": True, "reenrol_days": 21}})])

    async def fake_release(tenant_id, **k):
        seen.append(tenant_id)
        return []

    async def fake_pass(tenant_id, *a, **k):
        return None
    monkeypatch.setattr(courses, "release_lapsed_holds", fake_release)
    monkeypatch.setattr(courses, "_list_pass", fake_pass)
    asyncio.run(courses.run_frequent())
    assert seen == ["with"]


# ── the setting CHOOSES what the agent is told (Kris 2026-09-23) ─────────────
# "The switch changes the agent's context, so the agent knows different,
# instead of knowing there are options." Each option's instructions and tools
# carry nothing of the others', and the default is byte-identical to before.
def _names(tools):
    return [t["name"] for t in tools]


def _all_text(tools, other):
    return agent._diary_rules(other) + " ".join(t.get("description", "") for t in tools)


def test_hold_is_byte_identical_to_before_the_setting():
    for tools in (scheduling.STAY_TOOLS, scheduling.SCHEDULING_TOOLS, scheduling.SERVICE_TOOLS):
        assert scheduling.tools_for_other_dates(tools, "hold") is tools
    assert agent._diary_rules("hold") is agent._WINDOWS_DIARY_RULES
    cfg = {"business_brief": "Kennels"}
    assert agent.build_system_prompt(cfg, scheduling=True) ==         agent.build_system_prompt(cfg, scheduling=True, other_dates="hold")


def test_offer_is_told_to_name_and_never_to_reserve():
    stay = scheduling.tools_for_other_dates(scheduling.STAY_TOOLS, "offer")
    assert "offer_stay" not in _names(stay)                      # the tool isn't handed over
    text = _all_text(stay, "offer")
    assert "does NOT reserve alternatives" in text
    for hold_line in ("reserve it with offer_stay", "reserved via offer_slots BEFORE you write the reply",
                      "NEVER write specific times as options", "requested time is taken"):
        assert hold_line not in text, hold_line


def test_off_is_told_there_are_no_alternatives():
    stay = scheduling.tools_for_other_dates(scheduling.STAY_TOOLS, "off")
    assert "offer_stay" not in _names(stay)
    text = _all_text(stay, "off")
    assert "does NOT offer other times or dates" in text and "does not offer other dates" in text
    for other_line in ("offer the nearest alternatives", "NAME the nearest alternatives",
                       "reserve it with offer_stay", "requested time is taken"):
        assert other_line not in text, other_line


def test_a_slots_diary_keeps_its_open_ask_offers_but_not_for_full():
    """offer_slots also serves "you said Tuesday — three times": that stays.
    Only its full-request clause goes."""
    for tools in (scheduling.SCHEDULING_TOOLS, scheduling.SERVICE_TOOLS):
        for other in ("offer", "off"):
            slots = next(t for t in scheduling.tools_for_other_dates(tools, other) if t["name"] == "offer_slots")
            assert "requested time is taken" not in slots["description"]
            assert "they gave a day / time of day" in slots["description"]
            assert "never call this for those" in slots["description"]


def test_each_option_says_nothing_of_the_others():
    lines = {"hold": "reserved via offer_slots BEFORE you write the reply",
             "offer": "does NOT reserve alternatives",
             "off": "does NOT offer other times or dates"}
    for mine in lines:
        rules = agent._diary_rules(mine)
        for theirs, line in lines.items():
            assert (line in rules) == (mine == theirs), (mine, theirs)


def test_the_guard_lets_an_offer_business_name_times_after_full():
    from types import SimpleNamespace
    import json
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
        {"reply": "We're full then, but Monday 9th at 08:00 to Wednesday 11th at 16:00 is free — shall I book it?",
         "action_summary": ""}))])
    assert agent._unreserved_booking_claim(msg) is True                # hold: must be reserved
    assert agent._unreserved_booking_claim(msg, times_named_ok=True) is False
    booked = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
        {"reply": "Done", "thread_state": "booked"}))])
    assert agent._unreserved_booking_claim(booked, times_named_ok=True) is True   # a claim is still a claim


def test_an_offer_draft_names_the_alternative_without_being_forced_to_reserve(monkeypatch):
    from tests.test_agent_tools import run_draft, text_response, tool_response
    full = {"available": False, "nearest_arrival": "2026-11-09T08:00", "nearest_departure": "2026-11-11T16:00",
            "message": "Full for those exact dates."}
    reply = {"reply": "Sorry, 6th–8th is full. Monday 9th 08:00 to Wednesday 11th 16:00 is free — shall I book it?",
             "deferred": False, "missing": "", "blocked": ""}
    tools = [{"name": "check_availability", "description": "x", "input_schema": {"type": "object"}}]
    fake, result = run_draft(monkeypatch, [
        tool_response("check_availability", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(reply),
    ], lambda name, args: dict(full), tools=tools, scheduling=True, other_dates="offer")
    assert result.text == reply["reply"] and len(fake.calls) == 2      # no forced offer turn
    fake, _ = run_draft(monkeypatch, [
        tool_response("check_availability", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(reply), text_response(reply),
    ], lambda name, args: dict(full), tools=tools, scheduling=True)
    assert len(fake.calls) > 2                                          # hold: the guard still insists
