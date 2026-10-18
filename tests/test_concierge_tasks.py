"""The Concierge's task list (docs/plan-concierge-task-list.md, 2026-09-25).

Kris: "the same agent you talk to does the work so you wait to chat … what
happens if you list 3-4 tasks for the Concierge to do in 1 go? And the same
via VC." Several separate things become a task list worked in the background;
each lands in the waiting list (a parked card), done, or as her one question.

Run: python -m pytest tests/test_concierge_tasks.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import concierge, concierge_actions, concierge_tasks, db


def _ctx(**kw):
    return SimpleNamespace(tenant_id="t1", user_id="u1", client=None, viewing_as=None, **kw)


# ── start_tasks ─────────────────────────────────────────────────────────────
def test_a_list_needs_two_things_and_is_capped():
    raw = [{"title": f"T{i}", "instruction": f"do {i}"} for i in range(9)]
    assert len(concierge_tasks._clean_tasks(raw)) == concierge_tasks.MAX_TASKS
    assert concierge_tasks._clean_tasks([{"title": "", "instruction": "x"}, "junk"]) == []


def test_starting_writes_the_rows_and_launches_the_worker(monkeypatch):
    made, launched = {}, []
    monkeypatch.setattr(db, "create_concierge_tasks", lambda tid, uid, batch, tasks, surface, client=None:
                        made.update(batch=batch, tasks=tasks, surface=surface)
                        or [{"id": f"k{i}", "title": t["title"]} for i, t in enumerate(tasks)])
    monkeypatch.setattr(concierge_tasks, "launch", lambda tid, uid, batch: launched.append(batch))
    s = concierge_tasks.Starter(_ctx(), surface="voice")
    out = asyncio.run(s.start({"tasks": [{"title": "Block Sun", "instruction": "Block Sunday 11 Oct"},
                                         {"title": "Chase Meg", "instruction": "Chase Meg's invoice"}]}))
    assert out["ok"] and out["started"] == 2 and "propose nothing" in out["message"]
    assert made["surface"] == "voice" and launched == [made["batch"]]
    assert s.batch["batch_id"] == made["batch"]
    assert [t["status"] for t in s.batch["tasks"]] == ["queued", "queued"]
    again = asyncio.run(s.start({"tasks": [{"title": "a", "instruction": "b"}] * 2}))
    assert not again["ok"]                                   # once per turn


def test_one_thing_or_view_as_starts_nothing(monkeypatch):
    monkeypatch.setattr(db, "create_concierge_tasks", lambda *a, **k: pytest.fail("no rows"))
    one = asyncio.run(concierge_tasks.Starter(_ctx(), surface="typed").start(
        {"tasks": [{"title": "Chase Meg", "instruction": "Chase Meg"}]}))
    assert not one["ok"] and "not a list" in one["message"]
    ctx = _ctx(); ctx.viewing_as = "u2"
    two = asyncio.run(concierge_tasks.Starter(ctx, surface="typed").start(
        {"tasks": [{"title": "a", "instruction": "b"}, {"title": "c", "instruction": "d"}]}))
    assert not two["ok"] and "View as" in two["message"]


# ── the worker ──────────────────────────────────────────────────────────────
@pytest.fixture
def worker(monkeypatch):
    seen = {"updates": [], "published": [], "staged": []}
    monkeypatch.setattr(db, "update_concierge_task", lambda tid, f, client=None: seen["updates"].append(f))
    monkeypatch.setattr(concierge_tasks, "_publish", lambda tenant, batch, t: seen["published"].append(dict(t)))
    from app import fairuse, usage, trace
    monkeypatch.setattr(fairuse, "consume", lambda *a, **k: (True, None))
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(trace, "emit", lambda *a, **k: None)

    def _answer(reply="", action=None):
        async def fake(*a, **k):
            seen["note"] = k.get("question_note")
            seen["tools"] = [t["name"] for t in k.get("tools", [])]
            return concierge.BusinessReply(reply=reply, links=[], action=action, usage={}, model="m")
        monkeypatch.setattr(concierge, "answer_business", fake)
    seen["answer"] = _answer
    return seen


def _task():
    return {"id": "k1", "batch_id": "b1", "title": "Block Sun", "instruction": "Block Sunday 11 Oct"}


def _run(**kw):
    asyncio.run(concierge_tasks._run_one(_task(), tenant={"name": "Little Paws"}, tenant_id="t1",
                                         user_id="u1", member=None, full_qa=True,
                                         scheduling_on=True, **kw))


def test_a_change_is_parked_in_the_waiting_list(worker, monkeypatch):
    worker["answer"]("Blocked it — confirm when ready.", {"kind": "block_date", "new_date": "2026-10-11"})
    monkeypatch.setattr(concierge_actions, "stage", lambda *a, **k: worker["staged"].append(k)
                        or {"proposal_id": "p1", "summary": "Close Sun 11 Oct"})
    _run()
    assert worker["staged"][0]["park"] is True                   # never replaces the live card
    last = worker["updates"][-1]
    assert last["status"] == "waiting" and last["proposal_id"] == "p1"
    assert [p["status"] for p in worker["published"]] == ["working", "waiting"]
    assert worker["note"] == concierge_tasks.TASK_NOTE
    assert "start_tasks" not in worker["tools"]                  # a task never starts a list


def test_her_question_is_needs_you_and_an_answer_is_done(worker):
    worker["answer"]("Which Rufus — the springer or the lab?")
    _run()
    assert worker["updates"][-1]["status"] == "needs_you"
    worker["answer"]("Em is already on Rufus's meet and greet.")
    _run()
    assert worker["updates"][-1]["status"] == "done"


def test_a_card_that_cant_be_prepared_fails_and_says_why(worker, monkeypatch):
    worker["answer"]("", {"kind": "block_date"})
    def refuse(*a, **k):
        raise concierge_actions.ActionError("That day is already closed.")
    monkeypatch.setattr(concierge_actions, "stage", refuse)
    _run()
    assert worker["updates"][-1] == {**worker["updates"][-1], "status": "failed",
                                     "outcome": "That day is already closed."}


def test_the_daily_allowance_is_honoured(worker, monkeypatch):
    from app import fairuse
    monkeypatch.setattr(fairuse, "consume", lambda *a, **k: (False, 40))
    worker["answer"]("never", None)
    _run()
    assert worker["updates"][-1]["status"] == "failed"
    assert "allowance" in worker["updates"][-1]["outcome"]


def test_the_summary_reads_like_her():
    text = concierge_tasks.summary_text([
        {"title": "Block Sun", "status": "waiting"},
        {"title": "Em on Rufus", "status": "done", "outcome": "Already on it."},
        {"title": "Friday hours", "status": "needs_you", "outcome": "6pm or 6:30?"}])
    assert text.startswith("Your task list is finished:")
    assert "⏳ Block Sun — waiting for your yes" in text and "? Friday hours — 6pm or 6:30?" in text


# ── parking, the turn, the prompt, the panel ────────────────────────────────
class _Q:
    def __init__(self, log): self.log = log
    def __getattr__(self, name):
        def call(*a, **k):
            self.log.append((name, a))
            return self
        return call
    def execute(self):
        return SimpleNamespace(data=[{"id": "p9"}])


class _C:
    def __init__(self): self.log = []
    def table(self, name): return _Q(self.log)


def test_a_parked_card_goes_straight_to_waiting_and_supersedes_nothing():
    c = _C()
    db.create_concierge_proposal("t1", "block_date", "Close Sun", {}, action={"kind": "block_date"},
                                 client=c, park=True)
    names = [n for n, _ in c.log]
    assert "update" not in names
    inserted = [a[0] for n, a in c.log if n == "insert"][0]
    assert inserted["status"] == "superseded" and inserted["payload"]["_action"]
    c2 = _C()
    db.create_concierge_proposal("t1", "block_date", "Close Sun", {}, client=c2)
    assert "update" in [n for n, _ in c2.log]                    # a live card still replaces the last


def test_the_rule_is_only_there_when_the_tool_is():
    on = concierge.build_business_prompt(actions=True, tasks_on=True)
    off = concierge.build_business_prompt(actions=True)
    assert "start_tasks ONCE, FIRST" in on and "start_tasks" not in off
    assert "start_tasks ONCE, FIRST" in concierge.build_business_prompt(actions=True, voice_mode=True,
                                                                        tasks_on=True)


def test_a_turn_that_started_a_list_carries_it_and_stages_no_card(monkeypatch):
    from app import concierge_routes as cr
    saved = []
    monkeypatch.setattr(cr.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(cr.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(cr.db, "add_concierge_message", lambda tid, role, text, **k: saved.append(text))
    monkeypatch.setattr(cr.concierge_actions, "stage", lambda *a, **k: pytest.fail("no card"))
    result = concierge.BusinessReply(reply="On it.", links=[], action={"kind": "block_date"},
                                     usage={}, model="m")
    batch = {"batch_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "tasks": []}
    done = cr.finish_business_turn(_ctx(), result, question="four things", trail=[],
                                   tools_called=[], t0=0.0, full_qa=True, scheduling_on=True,
                                   tasks=batch)
    assert done["tasks"] == batch and done["proposal"] is None
    assert saved[-1].endswith(f"[Tasks: {batch['batch_id']}]")


def test_the_panel_draws_a_saved_list_under_its_reply(monkeypatch):
    import app.dashboard as dashboard
    bid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    history = [{"role": "assistant", "content": f"On it.\n\n[Tasks: {bid}]"}]
    monkeypatch.setattr(dashboard.db, "list_concierge_tasks", lambda tid, ids, client=None: [
        {"id": "k1", "user_id": "u1", "batch_id": bid, "title": "Block Sun", "status": "waiting",
         "outcome": "Close Sun", "proposal_id": "p1"}])
    dashboard._attach_task_lists(_ctx(), history)
    assert history[0]["content"] == "On it."
    assert '"status": "waiting"' in history[0]["tasks_json"]
    html = open("src/app/templates/app/_concierge_history.html", encoding="utf-8").read()
    assert 'data-cz-tasks-json="{{ m.tasks_json }}"' in html


def test_the_screens_fill_in_live():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    live = open("src/app/static/live.js", encoding="utf-8").read()
    assert "es.addEventListener('concierge_task'" in live and "'blam8:task'" in live
    assert "window.addEventListener('blam8:task'" in js
    # Typed task lists retain their turn owner inside the continuous backdrop.
    assert "var taskNode = taskListNode(done.tasks);" in js
    assert "(taskNode._czStub || taskNode).setAttribute('data-cz-owner-turn', displayTurnId)" in js
    assert "addCard(taskNode," in js
    assert "voiceStage.cardNode = taskListNode(payload.tasks);" in js        # voice
    assert "resurface({ proposal_id: pid })" in js                          # tap a waiting row


def test_start_up_picks_unfinished_lists_back_up():
    src = open("src/app/main.py", encoding="utf-8").read()
    assert "concierge_tasks.resume_unfinished()" in src


def test_more_than_fit_are_named_back_never_promised(monkeypatch):
    """Stress test 2026-09-25: eight asked, six started, "all eight are being
    worked on". The ones not started are named so she can say so."""
    monkeypatch.setattr(db, "create_concierge_tasks", lambda tid, uid, batch, tasks, surface, client=None:
                        [{"id": f"k{i}", "title": t["title"]} for i, t in enumerate(tasks)])
    monkeypatch.setattr(concierge_tasks, "launch", lambda *a: None)
    ten = [{"title": f"Task {i}", "instruction": f"do {i}"} for i in range(10)]
    out = asyncio.run(concierge_tasks.Starter(_ctx(), surface="typed").start({"tasks": ten}))
    assert out["started"] == concierge_tasks.MAX_TASKS == 8
    assert out["not_started"] == ["Task 8", "Task 9"]
    assert "Task 8; Task 9" in out["message"] and "NOT started" in out["message"]


# ── a task follows its card (Kris 2026-09-25: "Not all the tasks ended with a tick") ──
@pytest.fixture
def cards(monkeypatch):
    rows = [{"id": "k1", "batch_id": "b1", "title": "Block Sun", "status": "waiting",
             "outcome": "Close Sun", "proposal_id": "p1"}]
    seen = {"updates": [], "published": []}
    monkeypatch.setattr(db, "concierge_tasks_for_proposal",
                        lambda tid, pid, client=None: [r for r in rows if r["proposal_id"] == pid])
    def upd(tid, f, client=None):
        seen["updates"].append(f); rows[0].update(f)
    monkeypatch.setattr(db, "update_concierge_task", upd)
    monkeypatch.setattr(concierge_tasks, "_publish", lambda tenant, batch, t: seen["published"].append(dict(t)))
    return seen


def test_confirming_a_task_card_ticks_its_row(cards):
    concierge_tasks.settle_card("t1", "p1", "done", "Closed Sunday 11 October.")
    assert cards["updates"][-1]["status"] == "done"
    assert cards["published"][-1]["outcome"] == "Closed Sunday 11 October."


def test_a_brought_back_card_is_followed_then_settled(cards):
    concierge_tasks.follow_card("t1", "p1", "p2")
    assert cards["updates"][-1] == {"proposal_id": "p2"}
    concierge_tasks.settle_card("t1", "p2", "declined", "You said no — nothing changed.")
    assert cards["updates"][-1]["status"] == "declined"
    concierge_tasks.settle_card("t1", "p2", "done", "late")          # only a waiting task settles
    assert cards["updates"][-1]["status"] == "declined"


def test_every_card_outcome_reaches_its_task():
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert '"done" if outcome.ok else "needs_you", outcome.summary' in src        # confirm
    assert 'outcome = _save_proposal_outcome(ctx, row, current, result)' in src
    assert 'concierge_tasks.follow_card(ctx.tenant_id, body.proposal_id, proposal["proposal_id"])' in src
    assert src.count('"declined"') >= 2                                                         # no + bin


def test_the_list_folds_and_counts():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "function paintTaskHead(box)" in js and "head.setAttribute('aria-expanded'" in js
    assert "if (batch.collapsed) head.click();" in js
    assert "declined: '–'" in js


# ── Kris's second run (2026-09-25) ──────────────────────────────────────────
def test_the_list_starting_ends_the_turn_on_her_line(monkeypatch):
    """"She took a VERY long time to speak her last paragraph": once the list
    has started, her line is set straight away — no second model round."""
    monkeypatch.setattr(db, "create_concierge_tasks", lambda tid, uid, batch, tasks, surface, client=None:
                        [{"id": f"k{i}", "title": t["title"]} for i, t in enumerate(tasks)])
    monkeypatch.setattr(concierge_tasks, "launch", lambda *a: None)
    s = concierge_tasks.Starter(_ctx(), surface="voice")
    out = asyncio.run(s.start({"tasks": [{"title": "a", "instruction": "b"},
                                         {"title": "c", "instruction": "d"},
                                         {"title": "e", "instruction": "f"}]}))
    assert out[concierge.FINAL_REPLY_KEY] == s.final
    assert s.final.startswith("On it — three things.")
    assert concierge._final_from([{"x": 1}, out]) == s.final
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert "if starter.final and result.reply == starter.final:" in src and "_speak(starter.final)" in src


def _started(monkeypatch, args):
    monkeypatch.setattr(db, "create_concierge_tasks", lambda tid, uid, batch, tasks, surface, client=None:
                        [{"id": f"k{i}", "title": t["title"]} for i, t in enumerate(tasks)])
    monkeypatch.setattr(concierge_tasks, "launch", lambda *a: None)
    s = concierge_tasks.Starter(_ctx(), surface="voice")
    return s, asyncio.run(s.start(args))


TWO = [{"title": "a", "instruction": "b"}, {"title": "c", "instruction": "d"}]


def test_start_tasks_says_the_now_sentence_before_the_task_line(monkeypatch):
    # N16: the parts already answered, or that cannot be done, had nowhere to go: the task
    # line is the whole reply.
    s, out = _started(monkeypatch, {"tasks": TWO,
                                    "now": "No invoice is overdue — the two drafts stay unsent until you say.  "})
    expected = ("No invoice is overdue — the two drafts stay unsent until you say. "
                "On it — two things. Each will land in your waiting list as it's ready.")
    assert s.final == expected and out[concierge.FINAL_REPLY_KEY] == expected
    assert "`now` are already in the reply" in out["message"]
    # Whitespace is collapsed and a long one is cut to 300 characters.
    cut = " ".join(("word " * 120).split())[:300]
    s, _out = _started(monkeypatch, {"tasks": TWO, "now": ("word " * 120)})
    assert len(cut) == 300 and s.final == cut + " On it — two things. Each will land in your waiting list as it's ready."
    # Without it: the old line, exactly, and the old message.
    s, out = _started(monkeypatch, {"tasks": TWO})
    assert s.final == "On it — two things. Each will land in your waiting list as it's ready."
    assert "`now`" not in out["message"]
    # Over-long lists keep their "only the first N fit" sentence after it.
    many = [{"title": f"t{i}", "instruction": f"i{i}"} for i in range(concierge_tasks.MAX_TASKS + 1)]
    s, _out = _started(monkeypatch, {"tasks": many, "now": "Nothing is overdue."})
    assert s.final.startswith("Nothing is overdue. On it —") and "The list holds" in s.final


def test_prompt_and_tool_account_for_every_part(monkeypatch):
    from app import concierge_delegation
    for enabled in (False, True):
        monkeypatch.setattr(concierge_delegation, "enabled", lambda enabled=enabled: enabled)
        p = concierge.build_business_prompt(actions=True, tasks_on=True)
        assert "ACCOUNT FOR EVERY PART" in p and "Never drop a part" in p, enabled
        assert "start_tasks" in p
    now = concierge_tasks.TOOL["input_schema"]["properties"]["now"]
    assert now["type"] == "string" and "Facts only; never claim a change" in now["description"]
    assert "Never drop a part" in concierge_tasks.TOOL["description"]
    assert "now" not in concierge_tasks.TOOL["input_schema"]["required"]
    for enabled in (False, True):
        monkeypatch.setattr(concierge_delegation, "enabled", lambda enabled=enabled: enabled)
        start = concierge_tasks.foreground_tools()[0]
        assert "now" in start["input_schema"]["properties"] and "Never drop a part" in start["description"]


def test_a_tool_can_end_the_typed_turn_without_another_round(monkeypatch):
    calls = []

    class Msg:
        def __init__(self, content):
            self.content, self.usage, self.model = content, SimpleNamespace(), "m"
            self.stop_reason = "tool_use"

    async def fake_create(**kw):
        calls.append(kw)
        tu = SimpleNamespace(type="tool_use", id="t1", name="start_tasks",
                             input={"tasks": [{"title": "a", "instruction": "b"}] * 2})
        return Msg([tu])

    from app import llm
    monkeypatch.setattr(llm, "create", fake_create)

    async def ex(name, args):
        return {"ok": True, concierge.FINAL_REPLY_KEY: "On it — two things."}
    r = asyncio.run(concierge.answer_business("two things", [], tools=[concierge_tasks.TOOL],
                                              tool_executor=ex, tenant_id="t1", tasks_on=True))
    assert r.reply == "On it — two things." and r.action is None
    assert len(calls) == 1                                      # one model round, not two


def test_the_list_keeps_up_even_without_the_stream():
    """"Some stay on the timer, others get a tick": the list asks while rows
    are unfinished; ticks and "not done" fade; the bin is one line that fades."""
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "fetch('/concierge/business/tasks?batch='" in js and "setInterval(function () { if (document.visibilityState === 'visible' && anyTaskRunning()) pollTasks(); }, 3000);" in js
    assert "fadeAway(row, null, 3000, true);" in js
    assert "if (!okOutcome && (text === DECLINED_LINE || why === 'superseded'))" in js and "fadeAway(card, function" in js
    assert "px-3 py-1.5 text-left" in js
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert '@router.get("/business/tasks")' in src



def test_the_fold_really_hides_the_rows():
    """Kris 2026-09-25: "isn't collapsable even with the arrow showing and
    moving". A row is display:flex, which beats the hidden attribute."""
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    start = js.index("function taskListNode(batch)")
    body = js[start:js.index("return box;", start)]
    assert "row.hidden = " not in body and "r.hidden = " not in body
    assert "r.style.display = (!open || r.hasAttribute('data-gone')) ? 'none' : '';" in js
    assert "if (keep) el.style.display = 'none';" in js
