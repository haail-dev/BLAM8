"""Phase 1 of the planner -> worker -> reviewer loop (docs/plan-planner-worker-reviewer.md §7).

Offline. The only fake is the Anthropic SDK's messages.create, which answers
from a script per seat and routes each request by its shape: a request with
tools is the worker, a plan schema is the planner, any other request is the
reviewer. Everything above the SDK runs for real: llm.create, metering,
usage.record_anthropic, concierge.answer_business and the task's landing.

Run: python tests/run_offline.py tests/test_task_loop.py -q
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app import (concierge, concierge_actions, concierge_data, concierge_tasks, db, fairuse,
                 metering, task_loop, trace, usage)
from app.config import settings
from tests.agnostic_guard import RX

PLAN = {"intent": "Chase Meg's invoice",
        "steps": ["Find Meg", "Check INV-7", "Propose a reminder"],
        "lookups": ["find_customer"],
        "success_criteria": ["Meg's record is found", "INV-7 is shown as unpaid"],
        "risk": "low"}
ACTION = {"kind": "block_date", "new_date": "2026-10-11"}
FIND = {"name": "find_customer", "description": "Find a customer by name.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"]}}
LOOKUP_RESULT = {"id": "c1", "name": "Meg", "invoices": [{"no": "INV-7", "status": "unpaid"}]}


def _usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=1000, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, cache_creation=None)


def _msg(content, stop):
    return SimpleNamespace(id="msg_test", model="", stop_reason=stop, usage=_usage(), content=content)


def text(body):
    return _msg([SimpleNamespace(type="text", text=json.dumps(body))], "end_turn")


def verdict(kind, reason="", corrections=()):
    return text({"verdict": kind, "failed_criteria": [], "corrections": list(corrections),
                 "reason": reason})


def lookup():
    return _msg([SimpleNamespace(type="tool_use", id="tu_lookup", name="find_customer",
                                 input={"name": "Meg"})], "tool_use")


def answer(reply, action=None):
    payload = {"reply": reply, "links": []}
    if action:
        payload["action"] = action
    return _msg([SimpleNamespace(type="tool_use", id="tu_answer", name="answer", input=payload)],
                "tool_use")


def _seat(kw):
    if kw.get("tools"):
        return "worker"
    props = kw["output_config"]["format"]["schema"]["properties"]
    return "plan" if "success_criteria" in props else "review"


class FakeAnthropic:
    """The SDK client: messages.create answers from the script for that seat."""

    def __init__(self, world):
        self.world = world
        self.messages = self

    async def create(self, **kw):
        seat = _seat(kw)
        self.world["calls"].append((seat, kw))
        step = self.world["script"][seat].pop(0)
        if isinstance(step, BaseException):
            raise step
        step.model = kw["model"]
        return step


@pytest.fixture
def world(monkeypatch):
    w = {"calls": [], "script": {"plan": [], "worker": [], "review": []}, "updates": [],
         "published": [], "staged": [], "events": [], "trace": [], "lookups": []}
    fake = FakeAnthropic(w)
    monkeypatch.setattr(concierge, "client", lambda: fake)
    monkeypatch.setattr(db, "insert_llm_call", lambda row: None)
    monkeypatch.setattr(db, "insert_usage_event", lambda row: w["events"].append(row))
    monkeypatch.setattr(db, "update_concierge_task", lambda tid, f, client=None: w["updates"].append(f))
    monkeypatch.setattr(concierge_tasks, "_publish",
                        lambda tenant, batch, t: w["published"].append(dict(t)))
    monkeypatch.setattr(fairuse, "consume", lambda *a, **k: (True, None))
    monkeypatch.setattr(trace, "emit", lambda tenant, kind, **k: w["trace"].append(k))
    monkeypatch.setattr(concierge_data, "tools_for", lambda full_qa, member=None: [FIND])

    async def execute(tenant_id, client, name, args, *, full_qa, member=None):
        w["lookups"].append((name, args))
        return LOOKUP_RESULT
    monkeypatch.setattr(concierge_data, "execute", execute)
    monkeypatch.setattr(concierge_actions, "stage",
                        lambda *a, **k: w["staged"].append(k) or {"proposal_id": "p1",
                                                                  "summary": "Close Sun 11 Oct"})
    monkeypatch.setattr(settings, "llm_log_mode", "meta")
    monkeypatch.setattr(settings, "concierge_task_loop", "split")
    return w


def _run():
    task = {"id": "k1", "batch_id": "b1", "title": "Chase Meg",
            "instruction": "Chase Meg's invoice"}
    asyncio.run(concierge_tasks._run_one(task, tenant={"name": "Little Paws"}, tenant_id="t1",
                                         user_id="u1", member=None, full_qa=True,
                                         scheduling_on=True))


def _seats(world):
    return [seat for seat, _ in world["calls"]]


def _flat(kw):
    return json.dumps(kw["messages"], ensure_ascii=False)


# ── the switch and the models ────────────────────────────────────────────────
def test_the_switch_defaults_to_single_and_the_worker_is_haiku_55():
    assert settings.concierge_task_loop == "single"
    assert settings.anthropic_worker_model == "claude-haiku-5-5"
    assert settings.anthropic_planner_model == "" and settings.anthropic_reviewer_model == ""
    assert task_loop.planner_model() == settings.anthropic_model
    assert task_loop.reviewer_model() == settings.anthropic_model


def test_planner_and_reviewer_follow_their_own_setting_when_it_is_set(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_planner_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "anthropic_reviewer_model", "claude-opus-5-5")
    assert task_loop.planner_model() == "claude-sonnet-5-5"
    assert task_loop.reviewer_model() == "claude-opus-5-5"


# ── S2: single is today's path ───────────────────────────────────────────────
def test_single_mode_makes_no_planner_or_reviewer_call(world, monkeypatch):
    monkeypatch.setattr(settings, "concierge_task_loop", "single")
    world["script"]["worker"] = [answer("Meg owes INV-7.")]
    _run()
    assert _seats(world) == ["worker"]
    assert world["calls"][0][1]["model"] == settings.anthropic_model
    assert world["updates"][-1]["status"] == "done"
    assert world["trace"][-1]["detail"] == {"title": "Chase Meg", "status": "done",
                                            "outcome": "Meg owes INV-7."}


# ── S3: planner -> worker -> reviewer, once each on a pass ───────────────────
def test_split_runs_planner_then_worker_then_reviewer_once_each_on_a_pass(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.")]
    world["script"]["review"] = [verdict("pass")]
    _run()
    assert _seats(world) == ["plan", "worker", "worker", "review"]
    models = {seat: kw["model"] for seat, kw in world["calls"]}
    assert models == {"plan": settings.anthropic_model,
                      "worker": settings.anthropic_worker_model,
                      "review": settings.anthropic_model}
    assert world["updates"][-1]["status"] == "done" and world["staged"] == []
    assert world["trace"][-1]["detail"]["verdict"] == "pass"
    assert world["trace"][-1]["detail"]["retries"] == 0
    assert world["trace"][-1]["detail"]["loop"] == "split"


# ── S4: the reviewer gets the frozen plan, the trail, the reply and the action
def test_the_reviewer_gets_the_same_plan_object_the_trail_the_reply_and_the_action(world, monkeypatch):
    seen = {}
    real_plan, real_review = task_loop.plan, task_loop.review

    async def plan_spy(*a, **k):
        seat = await real_plan(*a, **k)
        seen["plan"], seen["snapshot"] = seat.value, copy.deepcopy(seat.value)
        return seat

    async def review_spy(instruction, **k):
        seen["review"] = {"instruction": instruction, "plan": k["plan"],
                          "trail": copy.deepcopy(k["trail"]), "reply": k["reply"],
                          "action": k["action"]}
        return await real_review(instruction, **k)

    monkeypatch.setattr(task_loop, "plan", plan_spy)
    monkeypatch.setattr(task_loop, "review", review_spy)
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Blocked it, confirm when ready.", ACTION)]
    world["script"]["review"] = [verdict("pass")]
    _run()
    review = seen["review"]
    assert review["plan"] is seen["plan"]                 # the same object the planner returned
    assert review["plan"] == seen["snapshot"]             # and the worker did not change it
    assert review["instruction"] == "Chase Meg's invoice"
    assert len(review["trail"]) == 1
    entry = review["trail"][0]
    assert entry["tool"] == "find_customer" and entry["args"] == {"name": "Meg"}
    assert "INV-7" in entry["result_summary"]
    assert review["reply"] == "Blocked it, confirm when ready."
    assert review["action"]["kind"] == "block_date" and review["action"]["new_date"] == "2026-10-11"
    assert world["updates"][-1]["status"] == "waiting"


def test_a_reviewer_pass_on_an_action_reply_lands_waiting_with_one_stage_call(world, monkeypatch):
    # The B path: a passed change is parked for the owner's yes, staged exactly once.
    staged_actions = []
    fixture_stage = concierge_actions.stage

    def stage(tenant_id, user, action, **k):
        staged_actions.append(action)
        return fixture_stage(tenant_id, user, action, **k)

    monkeypatch.setattr(concierge_actions, "stage", stage)
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Blocked it, confirm when ready.", ACTION)]
    world["script"]["review"] = [verdict("pass")]
    _run()
    assert _seats(world) == ["plan", "worker", "worker", "review"]
    assert len(staged_actions) == 1 and len(world["staged"]) == 1
    assert staged_actions[0]["kind"] == "block_date" and staged_actions[0]["new_date"] == "2026-10-11"
    assert world["updates"][-1]["status"] == "waiting"
    assert world["updates"][-1]["proposal_id"] == "p1"


def test_the_reviewer_request_carries_the_instruction_criteria_trail_and_reply(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.")]
    world["script"]["review"] = [verdict("pass")]
    _run()
    kw = world["calls"][-1][1]
    assert _seat(kw) == "review"
    brief = _flat(kw)
    assert "Chase Meg's invoice" in brief
    for criterion in PLAN["success_criteria"]:
        assert criterion in brief
    assert "find_customer" in brief and "INV-7" in brief and "Meg owes INV-7." in brief
    assert "not the assistant's summary" in task_loop.REVIEWER_SYSTEM


# ── S5: fix retries once with corrections; a second non-pass lands needs_you ─
def test_a_fix_retries_once_with_the_corrections_in_the_note_then_lands(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7."),
                                 lookup(), answer("Blocked it, confirm when ready.", ACTION)]
    world["script"]["review"] = [verdict("fix", corrections=["Look up INV-7 before you answer."]),
                                 verdict("pass")]
    _run()
    assert _seats(world) == ["plan", "worker", "worker", "review", "worker", "worker", "review"]
    workers = [kw for seat, kw in world["calls"] if seat == "worker"]
    assert "Look up INV-7 before you answer." not in _flat(workers[0])
    assert "Look up INV-7 before you answer." in _flat(workers[2])
    assert concierge_tasks.TASK_NOTE[:40] in _flat(workers[2])
    assert len(world["staged"]) == 1 and world["updates"][-1]["status"] == "waiting"
    detail = world["trace"][-1]["detail"]
    assert detail["retries"] == 1 and detail["verdict"] == "pass" and detail["loop"] == "split"


@pytest.mark.parametrize("second", ["escalate", "fix"])
def test_a_second_non_pass_lands_needs_you_with_the_reason_and_stages_nothing(world, second):
    reason = "Only you can say whether Meg has paid."
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7."),
                                 lookup(), answer("Meg owes INV-7 still.")]
    second_corrections = ["Look up INV-7 again."] if second == "fix" else []
    world["script"]["review"] = [verdict("fix", corrections=["Look up INV-7 again."]),
                                 verdict(second, reason=reason, corrections=second_corrections)]
    _run()
    assert _seats(world).count("worker") == 4 and _seats(world).count("review") == 2
    assert world["updates"][-1]["status"] == "needs_you"
    assert world["updates"][-1]["outcome"] == reason
    assert world["staged"] == []
    assert world["trace"][-1]["detail"]["retries"] == 1
    assert world["trace"][-1]["detail"]["verdict"] == second


# ── S6: escalate, and fail-closed on a planner or reviewer exception ─────────
def test_escalate_lands_needs_you_straight_away_and_stages_nothing(world):
    reason = "Only you can say whether Meg has paid."
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.", ACTION)]
    world["script"]["review"] = [verdict("escalate", reason=reason)]
    _run()
    assert _seats(world) == ["plan", "worker", "worker", "review"]
    assert world["updates"][-1]["status"] == "needs_you"
    assert world["updates"][-1]["outcome"] == reason
    assert world["staged"] == []
    assert world["trace"][-1]["detail"]["retries"] == 0


def test_a_planner_failure_fails_the_task_and_nothing_runs_or_stages(world):
    world["script"]["plan"] = [RuntimeError("planner down")]
    _run()
    assert _seats(world) == ["plan"]
    assert world["updates"][-1]["status"] == "failed"
    assert world["updates"][-1]["outcome"] == "I couldn't plan that one — nothing changed."
    assert world["staged"] == []


def test_a_reviewer_failure_fails_the_task_and_stages_nothing(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.", ACTION)]
    world["script"]["review"] = [RuntimeError("reviewer down")]
    _run()
    assert _seats(world) == ["plan", "worker", "worker", "review"]
    assert world["updates"][-1]["status"] == "failed"
    assert world["updates"][-1]["outcome"] == "I couldn't check that one — nothing changed."
    assert world["staged"] == []


def test_a_plan_needs_two_to_five_criteria_and_a_known_risk():
    with pytest.raises(ValueError):
        task_loop._clean_plan({**PLAN, "success_criteria": ["only one"]})
    with pytest.raises(ValueError):
        task_loop._clean_plan({**PLAN, "success_criteria": [str(i) for i in range(6)]})
    with pytest.raises(ValueError):
        task_loop._clean_plan({**PLAN, "risk": "extreme"})
    assert task_loop._clean_plan(PLAN) == PLAN


def test_a_fix_with_nothing_to_correct_is_escalated():
    review = {"verdict": "fix", "failed_criteria": [], "corrections": [], "reason": ""}
    out = task_loop._clean_review(review)
    assert out["verdict"] == "escalate" and out["reason"]
    with pytest.raises(ValueError):
        task_loop._clean_review({**review, "verdict": "maybe"})


# ── S7: the worker's request shape ──────────────────────────────────────────
def test_the_worker_request_has_no_sampling_or_thinking_and_ends_on_a_user_turn(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.")]
    world["script"]["review"] = [verdict("pass")]
    _run()
    workers = [kw for seat, kw in world["calls"] if seat == "worker"]
    assert workers
    for kw in workers:
        for key in ("temperature", "top_p", "top_k", "thinking"):
            assert key not in kw
        assert kw["messages"][-1]["role"] == "user"
        assert kw["model"] == "claude-haiku-5-5"
    assert "THE PLAN" in _flat(workers[0])


# ── S8: pricing ──────────────────────────────────────────────────────────────
def test_haiku_55_is_priced_and_unknown_above_a_100k_prompt():
    cost = usage.cost_usd("anthropic", "claude-haiku-5-5", input_tokens=1000, output_tokens=1000)
    assert cost == pytest.approx(0.0006, abs=1e-9)
    dated = usage.cost_usd("anthropic", "claude-haiku-5-5-20261001", input_tokens=1000,
                           output_tokens=1000)
    assert dated == pytest.approx(0.0006, abs=1e-9)
    assert usage.cost_usd("anthropic", "claude-haiku-5-5", input_tokens=100_000) == pytest.approx(0.01)
    assert usage.cost_usd("anthropic", "claude-haiku-5-5", input_tokens=100_001) is None
    assert usage.cost_usd("anthropic", "claude-haiku-5-5", input_tokens=40_000,
                          cache_read=60_001) is None


def test_sonnet_55_and_opus_55_are_priced():
    assert usage.cost_usd("anthropic", "claude-sonnet-5-5", input_tokens=1000,
                          output_tokens=1000) == pytest.approx(0.012, abs=1e-9)
    assert usage.cost_usd("anthropic", "claude-opus-5-5", input_tokens=1000,
                          output_tokens=1000) == pytest.approx(0.024, abs=1e-9)


def test_cache_reads_are_0_05x_on_sonnet_55_and_opus_55_and_0_1x_on_haiku_55():
    # Pricing page, read 14 Oct 2026: Sonnet 5.5 cache hit $0.10/MTok, Opus 5.5 $0.20/MTok,
    # Haiku 5.5 $0.01/MTok. Haiku's 100K read sits exactly at its prompt cap, so it is priced.
    assert usage.cost_usd("anthropic", "claude-sonnet-5-5",
                          cache_read=1_000_000) == pytest.approx(0.10, abs=1e-9)
    assert usage.cost_usd("anthropic", "claude-opus-5-5",
                          cache_read=1_000_000) == pytest.approx(0.20, abs=1e-9)
    assert usage.cost_usd("anthropic", "claude-haiku-5-5",
                          cache_read=100_000) == pytest.approx(0.001, abs=1e-9)


def test_the_cost_version_moved_with_the_new_rows():
    assert metering.COST_VERSION != "anthropic-standard-global-2026-09-28"
    assert metering.COST_VERSION == "anthropic-standard-global-2026-10-14"


# ── S9: three usage events per passed task, one request id ───────────────────
def test_three_usage_events_per_passed_task_under_one_request_id(world):
    world["script"]["plan"] = [text(PLAN)]
    world["script"]["worker"] = [lookup(), answer("Meg owes INV-7.")]
    world["script"]["review"] = [verdict("pass")]
    _run()
    ops = [e["operation"] for e in world["events"]]
    assert ops == [task_loop.PLAN_OPERATION, "concierge_task", task_loop.REVIEW_OPERATION]
    ids = {a.get("request_id") for e in world["events"] for a in e["raw_usage"]["attempts"]}
    assert len(ids) == 1 and None not in ids


# ── the trail, the brief, the house voice ────────────────────────────────────
def test_the_trail_is_capped_and_says_so():
    trail = task_loop.Trail()
    for i in range(40):
        trail.add("find_customer", {"name": f"n{i}"}, {"blob": "x" * 900})
    assert "note" in trail.entries[-1] and len(trail.entries) < 40
    total = sum(len(json.dumps(e)) for e in trail.entries)
    assert total <= task_loop.TRAIL_CAP + 400
    for entry in trail.entries:
        if "result_summary" in entry:
            assert len(entry["result_summary"]) <= task_loop.RESULT_CAP


def test_the_worker_is_told_the_plan_and_the_corrections_and_cannot_change_it():
    out = task_loop.render_plan(PLAN, corrections=["Check INV-7."])
    assert "cannot change it" in out
    assert PLAN["success_criteria"][0] in out
    assert "- Check INV-7." in out


def test_the_new_prompts_carry_no_trade_words():
    src = open("src/app/task_loop.py", encoding="utf-8").read()
    assert RX.search(src) is None
