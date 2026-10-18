"""Field Service (Trades) specialist agent + engineer capacity (docs/modules/module-field-service.md).

The trades twin of tests/test_tms_agent.py + the fleet capacity tests. What this pins:
  - the engineer capacity math: per-day load, skill-gated best-fit (tightest pack), and
    availability that only returns dates a qualifying engineer can actually fit
  - the field-service desk's prompt: role, trades tool-discipline, structured-result
    contract (no customer prose, never names the engineer)
  - answer() runs the SAME tool loop the base agent does — check_engineer_availability →
    book_job → a structured result — over scheduling.FSM_TOOLS, summing usage; non-JSON
    degrades to a clean error result
  - make_handoff: ONE hand-off per email, the hold bubbles into state['bookings'] and
    tokens into state['usage']
  - the base draft loop awaits the async hand-off executor and carries the
    <field_service> section (not trades domain rules)

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

from app import scheduling
from app.agents import field_service as fs_agent
from app.scheduling import DIARY_TZ


# ── pure engineer capacity math ───────────────────────────────────────────────
def _eng(id, skills=(), minutes=480, active=True, branch=None):
    return {"id": id, "skills": list(skills), "daily_minutes": minutes,
            "active": active, "branch_id": branch}


def _job(eng_id, minutes, day="2026-07-02", status="proposed"):
    return {"engineer_id": eng_id, "duration_minutes": minutes,
            "booking_date": day, "status": status}


def test_engineer_has_skill():
    e = _eng("e1", ["Gas", "plumbing"])
    assert scheduling.engineer_has_skill(e, "gas")        # case-insensitive
    assert scheduling.engineer_has_skill(e, None)         # no requirement → anyone
    assert not scheduling.engineer_has_skill(e, "electrical")


def test_engineer_day_loads_sums_active_only():
    engs = [_eng("e1", minutes=480), _eng("e2", minutes=240)]
    booked = [_job("e1", 120), _job("e1", 60),
              _job("e2", 100, status="cancelled"),   # not active → ignored
              _job("e1", 999, day="2026-07-03")]      # other day → ignored
    loads = scheduling.engineer_day_loads(engs, booked, date(2026, 7, 2))
    by = {l["engineer"]["id"]: l for l in loads}
    assert by["e1"]["used_min"] == 180 and by["e1"]["free_min"] == 300
    assert by["e2"]["used_min"] == 0 and by["e2"]["free_min"] == 240


def test_engineer_best_fit_tightest_pack_and_skill_gate():
    # Two plumbers; Alex has the smaller free day → best-fit packs the tighter one,
    # keeping Sam's bigger gap open for a bigger job.
    loads = [
        {"engineer": _eng("sam", ["gas", "plumbing"], 480), "used_min": 0, "free_min": 480},
        {"engineer": _eng("alex", ["plumbing"], 240), "used_min": 0, "free_min": 240},
    ]
    assert scheduling.engineer_best_fit(loads, 120, "plumbing")["engineer"]["id"] == "alex"
    # A gas job: only Sam qualifies.
    assert scheduling.engineer_best_fit(loads, 120, "gas")["engineer"]["id"] == "sam"
    # A skill nobody holds → nobody fits.
    assert scheduling.engineer_best_fit(loads, 60, "electrical") is None
    # Too big for either free day → nobody fits.
    assert scheduling.engineer_best_fit(loads, 600, "plumbing") is None


def test_engineer_availability_only_returns_fitting_dates():
    engs = [_eng("e1", ["gas"], 480)]
    now = datetime(2026, 6, 29, 9, 0, tzinfo=DIARY_TZ)        # a Monday
    # A 120-min gas job over a week: weekdays within the horizon come back, marked fits.
    slots = scheduling.engineer_availability(
        carried_config(None), engs, [], "2026-07-01", "2026-07-03", now, duration=120, skill="gas")
    days = {s["date"] for s in slots}
    assert "2026-07-02" in days and all(s["fits"] for s in slots)
    # A skill no engineer has → no fitting dates at all.
    none_fit = scheduling.engineer_availability(
        carried_config(None), engs, [], "2026-07-01", "2026-07-03", now, duration=120, skill="electrical")
    assert none_fit == []


def test_fsm_diary_line_lists_jobs_never_counts_engineers_as_capacity():
    line = scheduling.fsm_diary_line(
        None,
        [_eng("e1", ["gas"]), _eng("e2", ["plumbing"])],
        [{"id": "b1", "name": "Main"}],
        [{"name": "Boiler service", "default_minutes": 60, "required_skill": "gas"},
         {"name": "Emergency callout", "default_minutes": 60, "emergency": True}])
    assert "2 engineers" in line
    assert "Boiler service (60 min, needs gas)" in line
    assert "[emergency]" in line


# ── the engineer executor quotes + stamps the hold ───────────────────────────
def test_make_engineer_executor_quotes_and_stamps_the_hold(monkeypatch):
    from datetime import datetime
    eng = _eng("e1", ["gas"], 480)
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "list_branches", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "list_engineers", lambda *a, **k: [eng])
    monkeypatch.setattr(scheduling.db, "list_job_types", lambda *a, **k: [
        {"name": "Boiler service", "default_minutes": 60, "required_skill": "gas",
         "default_price": None, "emergency": False}])
    monkeypatch.setattr(scheduling.db, "get_fsm_rate_card", lambda *a, **k:
                        {"callout_charge": 60, "hourly_rate": 55, "min_charge": 75, "vat_pct": 20})
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "hold_engineer_booking", lambda *a, **k:
                        {"id": "b1", "booking_ref": "BK-1", "booking_date": "2026-07-02"})
    stamped = []
    monkeypatch.setattr(scheduling.db, "set_booking_quote",
                        lambda bid, tid, amt, **k: stamped.append((bid, amt)))

    now = datetime(2026, 6, 29, 9, 0, tzinfo=DIARY_TZ)
    execute, state = scheduling.make_engineer_executor("t1", contact="c@x.co", now=now)

    chk = execute("check_engineer_availability",
                  {"date_from": "2026-07-02", "date_to": "2026-07-02", "job_type": "Boiler service"})
    assert chk["quote"]["total"] == 138.0       # 60 callout + 55 labour + 20% VAT

    out = execute("book_job", {"date": "2026-07-02", "job_type": "Boiler service",
                               "summary": "Boiler not firing"})
    assert out["held"] is True and out["quote"]["total"] == 138.0   # inc-VAT, for the reply
    assert stamped == [("b1", 115.0)]                               # pre-VAT net, for invoicing


# ── the hand-off tool the base agent sees ────────────────────────────────────
def test_ask_field_service_tool_shape():
    t = fs_agent.ASK_FIELD_SERVICE_TOOL
    assert t["name"] == "ask_field_service"
    assert list(t["input_schema"]["properties"]) == ["request"]
    assert t["input_schema"]["required"] == ["request"]
    # The base never sees engineer/capacity internals — only the customer's words.
    assert "engineer" not in str(t["input_schema"]).lower()


# ── the desk's prompt ─────────────────────────────────────────────────────────
def test_prompt_has_trades_rules_and_result_contract():
    p = fs_agent.build_system_prompt(diary_shape="Engineer diary: 2 engineers",
                                     today=date(2026, 6, 16))
    assert "field-service desk" in p
    assert "STRUCTURED result" in p
    assert "Tuesday 16 June 2026" in p and "2026-06-16" in p
    assert "check_engineer_availability" in p and "book_job" in p
    assert "Engineer diary: 2 engineers" in p
    for token in ("status", "booking_ref", "needs_info", "emergency", "job_type"):
        assert token in p
    assert "you do NOT write to the customer" in p
    # never names the individual engineer
    assert "NEVER name the individual engineer" in p


# ── answer(): the tool loop + structured output ───────────────────────────────
def _usage(n):
    return SimpleNamespace(input_tokens=n, output_tokens=n * 2,
                           cache_read_input_tokens=n * 3, cache_creation_input_tokens=0)


def _text(payload, n=10):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                           stop_reason="end_turn", usage=_usage(n), model="fake-model")


def _tool(name, args, tid="tu-1", n=10):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=tid, name=name, input=args)],
                           stop_reason="tool_use", usage=_usage(n), model="fake-model")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


HELD = {"status": "held", "date": "2026-07-02", "job_type": "Boiler service",
        "duration_minutes": 60, "required_skill": "gas", "priority": "emergency",
        "booking_ref": "BK-1042", "alternatives": [], "assumed": [], "ask": "", "message": ""}


def test_answer_runs_tool_loop_holds_and_returns_structured(monkeypatch):
    fake = FakeClient([
        _tool("check_engineer_availability",
              {"date_from": "2026-07-02", "date_to": "2026-07-02", "job_type": "Boiler service"}),
        _tool("book_job", {"date": "2026-07-02", "job_type": "Boiler service",
                           "priority": "emergency", "summary": "Boiler not firing"},
              tid="tu-2", n=5),
        _text(HELD, n=3),
    ])
    monkeypatch.setattr(fs_agent, "_client", fake)

    executed = []

    def executor(name, args):
        executed.append(name)
        if name == "check_engineer_availability":
            return {"available": [{"date": "2026-07-02", "free_minutes": 480, "fits": True}],
                    "duration_minutes": 60}
        return {"held": True, "booking_id": "b-1", "booking_ref": "BK-1042",
                "date": "2026-07-02", "job_type": "Boiler service",
                "duration_minutes": 60, "priority": "emergency", "status": "proposed"}

    result, totals = asyncio.run(fs_agent.answer(
        "Boiler not firing, can someone come Thursday?", executor=executor,
        diary_shape="Engineer diary", today=date(2026, 6, 29)))

    assert executed == ["check_engineer_availability", "book_job"]
    assert result["status"] == "held" and result["booking_ref"] == "BK-1042"
    assert result["priority"] == "emergency"
    assert totals["input_tokens"] == 18 and totals["output_tokens"] == 36
    first = fake.calls[0]
    assert [t["name"] for t in first["tools"]] == ["check_engineer_availability", "book_job"]
    assert first["output_config"]["format"]["schema"] is fs_agent.FSM_SCHEMA
    last_tool_result = fake.calls[-1]["messages"][-1]["content"][0]
    assert last_tool_result["type"] == "tool_result"
    assert json.loads(last_tool_result["content"])["held"] is True


def test_answer_asks_which_service_without_holding(monkeypatch):
    needs = {**HELD, "status": "needs_info", "booking_ref": "", "date": "",
             "ask": "Which service do you need — a boiler service or a repair?"}
    fake = FakeClient([
        _tool("check_engineer_availability", {"date_from": "2026-07-02", "date_to": "2026-07-02",
                                              "job_type": "thingamajig"}),
        _text(needs, n=2),
    ])
    monkeypatch.setattr(fs_agent, "_client", fake)
    result, _ = asyncio.run(fs_agent.answer(
        "can you sort my thingamajig", today=date(2026, 6, 29),
        executor=lambda *a: {"error": "unknown_job_type", "job_types": ["Boiler service", "Repair"]}))
    assert result["status"] == "needs_info" and "service" in result["ask"].lower()


def test_answer_falls_back_to_error_on_bad_output(monkeypatch):
    fake = FakeClient([_text("nonsense, not an object")])
    monkeypatch.setattr(fs_agent, "_client", fake)
    result, _ = asyncio.run(fs_agent.answer("x", executor=lambda *a: {}, today=date(2026, 6, 29)))
    assert result["status"] == "error"


# ── make_handoff: one per email, bubbles holds + usage ────────────────────────
def test_make_handoff_one_per_email_bubbles_hold_and_usage(monkeypatch):
    monkeypatch.setattr(fs_agent.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(fs_agent.db, "list_engineers", lambda *a, **k: [])
    monkeypatch.setattr(fs_agent.db, "list_branches", lambda *a, **k: [])
    monkeypatch.setattr(fs_agent.db, "list_job_types", lambda *a, **k: [])

    def fake_make_engineer_executor(tenant_id, **kw):
        state = {"bookings": []}

        def execute(name, args):
            state["bookings"].append({"id": f"b-{len(state['bookings']) + 1}"})
            return {"held": True}
        return execute, state
    monkeypatch.setattr(fs_agent.scheduling, "make_engineer_executor", fake_make_engineer_executor)

    seen = []

    async def fake_answer(request, *, executor, diary_shape=None, today=None, **kw):
        seen.append(request)
        executor("book_job", {})                       # place the real hold via the engineer exec
        return ({**HELD, "booking_ref": "BK-9"}, {"input_tokens": 7})
    monkeypatch.setattr(fs_agent, "answer", fake_answer)

    execute, state = fs_agent.make_handoff("t1", contact="c@x.co", thread_id="th-1", mailbox_id="mb-1")
    out1 = asyncio.run(execute("ask_field_service", {"request": "boiler Thursday"}))
    assert out1["booking_ref"] == "BK-9"
    assert state["bookings"] == [{"id": "b-1"}]
    assert state["usage"]["input_tokens"] == 7

    out2 = asyncio.run(execute("ask_field_service", {"request": "again, same email"}))
    assert out2 == out1                                # cached — the desk ran once
    assert seen == ["boiler Thursday"]
    assert state["bookings"] == [{"id": "b-1"}]        # no second hold placed


# ── the base draft loop awaits the async hand-off executor ────────────────────
def test_base_draft_awaits_async_field_service_executor(monkeypatch):
    from app import agent
    fake = FakeClient([
        _tool("ask_field_service", {"request": "boiler not firing Thursday"}),
        _text({"reply": "Pencilled in for Thursday, ref BK-1042.",
               "deferred": False, "missing": "", "scheduling_requested": False}),
    ])
    monkeypatch.setattr(agent, "_client", fake)

    seen = []

    async def executor(name, args):                    # async, like the real hand-off
        seen.append((name, args))
        return {"status": "held", "booking_ref": "BK-1042", "date": "2026-07-02",
                "job_type": "Boiler service"}

    result = asyncio.run(agent.draft_reply(
        {}, {"subject": "No heating", "body": "Boiler not firing, can someone come Thursday?"},
        tools=[fs_agent.ASK_FIELD_SERVICE_TOOL], tool_executor=executor, field_service=True))

    assert seen == [("ask_field_service", {"request": "boiler not firing Thursday"})]
    assert result.text == "Pencilled in for Thursday, ref BK-1042."
    fed = json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])
    assert fed["booking_ref"] == "BK-1042" and fed["status"] == "held"
    # the base prompt for an FSM tenant carries the hand-off section, not trades rules
    system = fake.calls[0]["system"]
    system = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "<field_service>" in system
    assert "ask_field_service" in system
