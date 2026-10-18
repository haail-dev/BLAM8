"""The Concierge's task list — several jobs at once, worked in the background
(docs/plan-concierge-task-list.md; Kris 2026-09-25: "the same agent you talk to
does the work so you wait to chat").

When one message asks for two or more separate things, the Concierge calls the
`start_tasks` tool instead of doing them in the turn. She answers at once ("On
it"), and this module works each task on its own: a fresh answer_business run
per task, in order, with nothing but the task's instruction. What it lands on:

  waiting    — a change for the owner's yes: the card is PARKED in the waiting
               list (never replacing the card on their screen), and a tap
               brings it up fresh (the existing resurface path);
  done       — nothing to confirm (a question answered, already in place);
  needs_you  — she couldn't finish without one detail: her question;
  failed     — it couldn't be prepared, and says why.

Each change is published on the live stream ({type: concierge_task}) so the
task list on screen fills in as it goes; the batch ends with one message in
the conversation (her memory of it) and a push. A deploy mid-batch is survived:
start-up picks up anything left queued or working (resume_unfinished).
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app import concierge_intake, db, events, task_priority, concierge_delegation as delegation

log = logging.getLogger(__name__)

TOOL_NAME = "start_tasks"
MAX_TASKS = 8

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Start a task list: use when the owner asks for TWO OR MORE SEPARATE things in one "
        "message (different changes, or different people/records). Call it ONCE, before any "
        "lookup, with one task per thing — each is then worked in the background while the "
        "owner carries on, and lands in their waiting list for a yes. Not for one request, "
        "not for a question, not for the SAME change across several records (that is one "
        "bulk card). Account for EVERY part of the message in this one call: each part is "
        "either a task here, or answered/declined in `now` and listed in `others`. Never drop a part."),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string",
                                  "description": "Six words at most, e.g. 'Block Sun 11 Oct'."},
                        "instruction": {"type": "string",
                                        "description": "The whole request for this one thing, "
                                                       "standing on its own: every name, date and "
                                                       "detail the owner gave — it is worked "
                                                       "without this conversation."},
                    },
                    "required": ["title", "instruction"],
                },
            },
            "others": concierge_intake.OTHERS_SCHEMA,
            "now": {"type": "string",
                    "description": "ONE or two short sentences said in the SAME reply, before the task "
                                   "line: the parts you can answer from lookups you have ALREADY made "
                                   "('No invoice is overdue — the two drafts stay unsent until you say'; "
                                   "'Your inbox has one reply waiting, from X') and any part you cannot "
                                   "do, with the reason. Facts only; never claim a change."},
        },
        "required": ["tasks"],
    },
}

CONTROL_NAME = "control_task"
CONTROL_TOOL = {
    "name": CONTROL_NAME,
    "description": "Revise or cancel a delegated request using its exact task_id and revision from the current task list. A revision replaces the WHOLE instruction, including target and dates. Ask which task if ambiguous. Interrupting speech does not cancel a task. Execution already claimed may be too late; report the returned truth.",
    "input_schema": {"type": "object", "properties": {
        "task_id": {"type": "string"}, "revision": {"type": "integer"},
        "operation": {"type": "string", "enum": ["revise", "cancel"]},
        "instruction": {"type": "string"}}, "required": ["task_id", "revision", "operation"]},
}


def foreground_tools():
    if not delegation.enabled():
        return [TOOL]
    return [{**TOOL, "description": "Delegate one slow lookup or preparation, or several separate requests, before looking up data. The foreground acknowledges immediately while each task is prepared sequentially. Approval is still required for changes. Supply each complete instruction including target, names and dates. Account for EVERY part of the message in this one call: each part is either a task here, or answered/declined in `now` and listed in `others`. Never drop a part."}, CONTROL_TOOL]


# What each background run is told, on top of the ordinary Concierge prompt.
TASK_NOTE = (
    "[This is ONE task from the owner's task list, worked in the background — the owner "
    "is not waiting on this reply and can't answer it now. Look up what you need, then put "
    "the change in `action`; they confirm it later from their waiting list. If it is "
    "already done or needs no change, say so in one sentence and propose nothing. If an "
    "essential detail is genuinely missing, reply with the ONE question you need answered "
    "and propose nothing. Reply in one or two short sentences.]")

LABEL = "Started your tasks"
HINT = "Starting your tasks…"

_RUNNING: set = set()
_BATCH_ACTIVE: set = set()
_BATCH_DIRTY: set = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight")


def _count_words(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def _clean_tasks(raw, *, limit: int | None = MAX_TASKS) -> list[dict]:
    out = []
    if not isinstance(raw, list):
        return out
    for t in (raw or [])[:limit]:
        if not isinstance(t, dict):
            continue
        title = " ".join(str(t.get("title") or "").split())[:60]
        instruction = " ".join(str(t.get("instruction") or "").split())[:1000]
        if title and instruction:
            out.append({"title": title, "instruction": instruction})
    return out


def _publish(tenant_id: str, batch_id: str, task: dict) -> None:
    events.publish(tenant_id, {"type": "concierge_task", "batch_id": batch_id,
                               "task": {k: task.get(k) for k in
                                        ("id", "revision", "event_id")}})


class Starter:
    """The start_tasks tool for one turn: creates the batch once and launches
    its worker. `batch` is what the turn's done payload carries."""

    def __init__(self, ctx, *, surface: str, source_turn=None, voice_session=None, question=""):
        self.ctx = ctx
        self.surface = surface
        self.batch: dict | None = None
        self.final: str | None = None   # the line she says once the list has started
        self.source_turn = source_turn or str(uuid.uuid4())
        self.voice_session = voice_session
        self.question = question
        self.intake: dict | None = None   # what the model said became of the parts that are not tasks

    async def start(self, args: dict) -> dict:
        if not delegation.enabled():
            return await self._start(args)
        try:
            result = await self._start(args)
        except Exception:
            log.warning("delegation creation unavailable", exc_info=True)
            result = {"ok": False, "message": "I couldn't verify that the request was saved. Check your task list before trying again."}
        if not result.get("ok"):
            self.final = result.get("message") or "I couldn't start that request."
            result["_final_reply"] = self.final
        return result

    async def _start(self, args: dict) -> dict:
        if self.batch:
            return {"ok": False, "message": "The task list has already started — just "
                                            "tell the owner you're on it."}
        if getattr(self.ctx, "viewing_as", None):
            return {"ok": False, "message": "View as sets nothing up — say what would "
                                            "happen instead."}
        every = _clean_tasks((args or {}).get("tasks"), limit=None)
        tasks, over = every[:MAX_TASKS], every[MAX_TASKS:]
        if len(tasks) < (1 if delegation.enabled() else 2):
            return {"ok": False, "message": "That's one thing, not a list — do it in this "
                                            "turn as usual."}
        batch_id = str(uuid.uuid4())
        if delegation.enabled():
            from app.concierge_routes import _live_asker
            _live_asker(self.ctx)
            rows = delegation.create(self.ctx, tasks, surface=self.surface,
                                     source_turn=self.source_turn, voice_session=self.voice_session)
            batch_id = rows[0]["batch_id"]
        else:
            rows = db.create_concierge_tasks(self.ctx.tenant_id, self.ctx.user_id, batch_id,
                                             tasks, surface=self.surface)
        self.batch = {"batch_id": batch_id,
                      "tasks": [{"id": r["id"], "title": r["title"], "status": "queued",
                                 "outcome": None, "proposal_id": None} for r in rows]}
        if delegation.enabled():
            self.batch["tasks"] = [delegation.public(row) for row in rows]
        # The parts that are not tasks, as declared (kept with the reply as the intake
        # receipt; src/app/concierge_intake.py). Only what the model said: the route adds
        # the tools that actually ran.
        self.intake = concierge_intake.declared(args)
        launch(self.ctx.tenant_id, self.ctx.user_id, batch_id)
        message = ("They're being worked on in the background now. Tell the owner "
                   "in ONE short sentence that you're on them and each will land in "
                   "their waiting list as it's ready. Don't look anything up for "
                   "them yourself, and propose nothing.")
        if over:
            # Stress test 2026-09-25: eight asked, six started, "all eight are being
            # worked on". Never promise what isn't on the list — name what's left.
            message += (f" Only the first {len(rows)} fit on one list: say so, and name "
                        "the ones NOT started so they can ask again after — "
                        + "; ".join(t["title"] for t in over) + ".")
        # Live 2026-10-14: a message with a question and a task in it lost the question,
        # because this line is the whole reply. Whatever the model already knew, or
        # had to decline, rides in front of it (one or two sentences, facts only).
        now = " ".join(str((args or {}).get("now") or "").split())[:300]
        if now:
            message += " The parts you put in `now` are already in the reply."
        # Her answer is said at once, with no second model round (Kris
        # 2026-09-25: "a VERY long time to speak her last paragraph").
        final = (f"On it — {_count_words(len(rows))} things. Each will land in your "
                 "waiting list as it's ready.")
        if over:
            final += (f" The list holds {len(rows)}, so ask me again after for: "
                      + "; ".join(t["title"] for t in over) + ".")
        if delegation.enabled() and len(rows) == 1:
            final = "On it. I'll prepare that in the background and let you know when it's ready."
        if now:
            final = f"{now} {final}"
        self.final = final
        return {"ok": True, "started": len(rows), "titles": [r["title"] for r in rows],
                "not_started": [t["title"] for t in over], "message": message,
                "_final_reply": final}


    async def control(self, args):
        try:
            result = await self._control(args)
        except Exception:
            log.warning("delegation control unavailable", exc_info=True)
            result = {"ok": False, "state": "unavailable"}
        if result.get("ok"):
            reply = ("Task cancelled before execution." if args.get("operation") == "cancel"
                     else "I've updated the request. Any change still needs your approval.")
        else:
            reply = {"too_late": "That action has already been claimed for execution. Check the original result; a further change needs a new request.",
                     "stale": "This task has changed. Check the current task before trying again.",
                     "needs_input": result.get("summary") or "Give the complete replacement request, including the target."}.get(
                         result.get("state"), "I couldn't identify an available task. Choose the task in your task list.")
        self.final = reply
        return {**result, "_final_reply": reply}

    async def _control(self, args):
        if not delegation.enabled() or getattr(self.ctx, "viewing_as", None):
            return {"ok": False, "state": "unavailable"}
        if args.get("operation") not in {"revise", "cancel"}:
            return {"ok": False, "state": "needs_input"}
        if self.surface in {"voice", "typed"}:
            current = delegation.scoped_task(self.ctx, args.get("task_id"))
            if not current or (self.surface == "voice" and not self.voice_session) or current.get("voice_session") != self.voice_session:
                return {"ok": False, "state": "unavailable"}
            candidates = db.list_session_concierge_tasks(self.ctx.tenant_id, self.ctx.user_id,
                                                        self.voice_session, client=self.ctx.client)
            candidates = [row for row in candidates if row.get("status") in {"queued", "working", "waiting", "needs_you"}
                          and delegation.scoped_task(self.ctx, row["id"])]
            if len(candidates) > 1 and current["id"] not in self.question:
                stop = {"move", "check", "look", "up", "price", "cancel", "revise", "email", "send", "booking", "the", "a", "for", "to", "invoice", "on"}
                stop.update({"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow", "instead", "actually", "task", "request"})
                question_words = set(re.findall(r"[\w'-]+", self.question.lower()))
                matches = [row for row in candidates if
                    (set(re.findall(r"[\w'-]+", row.get("title", "").lower())) - stop) & question_words]
                if len(matches) != 1 or matches[0]["id"] != current["id"]:
                    return {"ok": False, "state": "needs_input", "summary": "Which task do you want to change?"}
        result = delegation.control(self.ctx, args.get("task_id"), args.get("revision"),
                                    args["operation"], args.get("instruction") or "")
        if result.get("ok") and args["operation"] == "revise":
            launch(self.ctx.tenant_id, self.ctx.user_id, result["task"]["batch_id"])
        return result


def launch(tenant_id: str, user_id: str | None, batch_id: str) -> None:
    """Run the batch as an in-process task (kept referenced until it ends)."""
    key = (tenant_id, user_id, batch_id)
    if key in _BATCH_ACTIVE:
        _BATCH_DIRTY.add(key)
        return
    _BATCH_ACTIVE.add(key)

    async def runner():
        try:
            while True:
                _BATCH_DIRTY.discard(key)
                await run_batch(tenant_id, user_id, batch_id)
                if key not in _BATCH_DIRTY:
                    break
        finally:
            _BATCH_ACTIVE.discard(key)
            _BATCH_DIRTY.discard(key)

    task = asyncio.get_running_loop().create_task(runner())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def run_batch(tenant_id: str, user_id: str | None, batch_id: str) -> None:
    from app import people
    try:
        tenant = db.get_tenant(tenant_id) or {}
        row = db.get_member(tenant_id, user_id) if user_id else None
        member = people.member(row) if row else None
        mods = db.visible_modules(tenant_id)
        full_qa, scheduling_on = "crm" in mods, "scheduling" in mods
        tasks = db.list_concierge_tasks(tenant_id, [batch_id])
    except Exception:
        log.exception("task batch set-up failed (tenant=%s batch=%s)", tenant_id, batch_id)
        return
    # Quick reads first, then actions, then drafts; the gate also orders this
    # batch against the owner's other batches.
    tasks = sorted(tasks, key=lambda t: task_priority.classify(t.get("title"), t.get("instruction")))
    for t in tasks:
        tier = task_priority.classify(t.get("title"), t.get("instruction"))
        if t.get("revision", 0) > 0:
            if delegation.enabled() and t.get("status") == "queued":
                async with task_priority.gate(tenant_id, user_id, tier):
                    await _run_revision(t, tenant=tenant, tenant_id=tenant_id, user_id=user_id)
            continue
        if t.get("status") not in db.TASK_OPEN:
            continue
        async with task_priority.gate(tenant_id, user_id, tier):
            await _run_one(t, tenant=tenant, tenant_id=tenant_id, user_id=user_id,
                           member=member, full_qa=full_qa, scheduling_on=scheduling_on)
    _finish_batch(tenant_id, user_id, batch_id)


async def _run_one(t: dict, *, tenant: dict, tenant_id: str, user_id: str | None,
                   member, full_qa: bool, scheduling_on: bool) -> None:
    from app import concierge, concierge_actions, concierge_data, fairuse, llm, trace, usage
    batch_id = t["batch_id"]
    t = {**t, "status": "working"}
    db.update_concierge_task(t["id"], {"status": "working", "started_at": _now()})
    _publish(tenant_id, batch_id, t)
    fields: dict = {}
    loop_detail: dict = {}
    try:
        allowed, _cap = fairuse.consume(tenant_id, tenant)
        if not allowed:
            fields = {"status": "failed",
                      "outcome": "Today's Concierge allowance ran out before this one."}
        elif settings.concierge_task_loop == "split":
            fields, loop_detail = await _run_split(t, tenant=tenant, tenant_id=tenant_id,
                                                   user_id=user_id, member=member,
                                                   full_qa=full_qa, scheduling_on=scheduling_on)
        else:
            async def _executor(name: str, args: dict) -> dict:
                if name == TOOL_NAME:
                    return {"error": "not_here", "message": "You are working one task — do it."}
                return await concierge_data.execute(tenant_id, None, name, args,
                                                    full_qa=full_qa, member=member)
            today = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)")
            with llm.request_scope():
                result = await concierge.answer_business(
                    t["instruction"], [],
                    tools=concierge_data.tools_for(full_qa, member),
                    tool_executor=_executor,
                    business_name=tenant.get("name") or tenant.get("company"),
                    today=today, full_qa=full_qa, tenant_id=tenant_id,
                    actions=True, question_note=TASK_NOTE)
            usage.record_anthropic(tenant_id, result.model, result.usage,
                                   operation="concierge_task", module="concierge")
            if result.action:
                try:
                    card = concierge_actions.stage(
                        tenant_id, None, result.action, full_qa=full_qa,
                        scheduling_on=scheduling_on, user_id=user_id, member=member,
                        park=True)
                    fields = {"status": "waiting", "outcome": card["summary"],
                              "proposal_id": card["proposal_id"]}
                except concierge_actions.ActionError as e:
                    fields = {"status": "failed", "outcome": str(e)}
            else:
                reply = (result.reply or "").strip()
                fields = {"status": "needs_you" if reply.endswith("?") else "done",
                          "outcome": reply[:600] or "Done."}
        trace.emit(tenant_id, "concierge_task", user_id=user_id,
                   outcome="ok" if fields.get("status") != "failed" else "error",
                   detail={"title": t["title"], "status": fields.get("status"),
                           "outcome": (fields.get("outcome") or "")[:200], **loop_detail})
    except Exception:
        log.exception("concierge task failed (tenant=%s task=%s)", tenant_id, t["id"])
        fields = {"status": "failed", "outcome": "I couldn't do that one just now — nothing changed."}
    fields["finished_at"] = _now()
    try:
        db.update_concierge_task(t["id"], fields)
    except Exception:
        log.exception("task row update failed (task=%s)", t["id"])
    _publish(tenant_id, batch_id, {**t, **fields})


async def _run_revision(t, *, tenant, tenant_id, user_id):
    """Read/preparation only. Every publication follows an owned DB transition."""
    from app import concierge, concierge_actions, concierge_data, fairuse, llm, usage
    attempt = str(uuid.uuid4())
    claim = delegation.transition("claim", tenant_id, user_id, task_id=t["id"],
                                  revision=t["revision"], attempt_id=attempt)
    if not claim.get("ok"):
        return
    t = claim["task"]
    _publish(tenant_id, t["batch_id"], t)
    evidence = []

    def checkpoint():
        current = db.get_concierge_task(tenant_id, user_id, t["id"])
        if not current or (current.get("revision"), current.get("attempt_id"), current.get("status")) != (t["revision"], attempt, "working"):
            raise delegation.Superseded()
        return delegation.access_scope(tenant_id, user_id)

    try:
        scope, member, modules = checkpoint()
        if (t.get("result") or {}).get("scope") != scope:
            raise PermissionError("Access changed since delegation")
        allowed, _ = fairuse.consume(tenant_id, tenant)
        if not allowed:
            raise RuntimeError("Allowance unavailable")

        async def executor(name, args):
            current_scope, current_member, current_modules = checkpoint()
            if current_scope != scope:
                raise PermissionError("Access changed")
            if name not in {tool["name"] for tool in concierge_data.tools_for("crm" in current_modules, current_member)}:
                return {"error": "not_allowed"}
            value = await concierge_data.execute(tenant_id, None, name, args,
                                                 full_qa="crm" in current_modules, member=current_member)
            checkpoint()
            if isinstance(value, dict) and not value.get("error"):
                evidence.append({"tool": name, "result": value})
            return value

        with llm.request_scope():
            answer = await concierge.answer_business(
                t["instruction"], [], tools=concierge_data.tools_for("crm" in modules, member),
                tool_executor=executor, business_name=tenant.get("name") or tenant.get("company"),
                today=datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)"),
                full_qa="crm" in modules, tenant_id=tenant_id, actions=True, question_note=TASK_NOTE)
        usage.record_anthropic(tenant_id, answer.model, answer.usage, operation="concierge_task", module="concierge")
        current_scope, member, modules = checkpoint()
        if current_scope != scope:
            raise PermissionError("Access changed")
        if answer.action:
            concierge_actions.stage(tenant_id, None, answer.action, full_qa="crm" in modules,
                                    scheduling_on="scheduling" in modules, user_id=user_id,
                                    member=member, task_binding={"task_id": t["id"],
                                        "revision": t["revision"], "attempt_id": attempt, "scope": scope})
            # stage atomically binds a pending card and waiting task. Do not
            # overwrite it with a synthetic finish or replay after a lost reply.
            current = db.get_concierge_task(tenant_id, user_id, t["id"])
            if current:
                _publish(tenant_id, t["batch_id"], current)
            return
        reply = (answer.reply or "").strip()[:600]
        kind = "needs_input" if reply.endswith("?") else "lookup_answer" if evidence else "unknown"
        if not reply:
            kind = "unknown"
        summary = (f"Your lookup for {t['title']} is ready in your task list. No change has been executed."
                   if kind == "lookup_answer" else f"I need more information for {t['title']}. Check the question in your task list." if kind == "needs_input"
                   else "I couldn't verify a result. Please check this task.")
        result = {"kind": kind, "summary": summary, "scope": scope,
                  "tools": sorted({item["tool"] for item in evidence}), "reads": evidence}
        if kind == "needs_input":
            result["question"] = reply
        data = {"status": "done" if kind == "lookup_answer" else "needs_you", "summary": summary, "result": result}
    except delegation.Superseded:
        return
    except Exception:
        log.warning("delegated preparation unavailable (task=%s)", t["id"], exc_info=True)
        data = {"status": "failed", "summary": "Preparation wasn't verified. Check this task.",
                "result": {"kind": "failed", "summary": "Preparation wasn't verified. Check this task."}}
    # If stage committed but its response was lost, this CAS refuses working
    # -> failed because the durable row is already waiting.
    done = delegation.transition("finish", tenant_id, user_id, task_id=t["id"],
                                 revision=t["revision"], attempt_id=attempt, data=data)
    if done.get("ok"):
        _publish(tenant_id, t["batch_id"], done["task"])


async def _run_split(t: dict, *, tenant: dict, tenant_id: str, user_id: str | None,
                     member, full_qa: bool, scheduling_on: bool) -> tuple[dict, dict]:
    """settings.concierge_task_loop == "split": planner -> worker -> reviewer for one
    task (docs/plan-planner-worker-reviewer.md §4). Returns the task's fields and the
    detail the trace gains. Every seat's usage is recorded under one request id."""
    from app import concierge, concierge_data, llm, task_loop, usage
    instruction = t["instruction"]
    business = tenant.get("name") or tenant.get("company")
    today = datetime.now(ZoneInfo("Europe/London")).strftime("%A %d %B %Y (%Y-%m-%d)")
    tools = concierge_data.tools_for(full_qa, member)
    detail: dict = {"loop": "split", "verdict": None, "retries": 0}

    def worker_executor(trail: task_loop.Trail):
        async def _executor(name: str, args: dict) -> dict:
            if name == TOOL_NAME:
                return {"error": "not_here", "message": "You are working one task — do it."}
            try:
                out = await concierge_data.execute(tenant_id, None, name, args,
                                                   full_qa=full_qa, member=member)
            except Exception:
                trail.add(name, args, {"error": "lookup_failed"})
                raise
            trail.add(name, args, out)
            return out
        return _executor

    with llm.request_scope():
        try:
            planned = await task_loop.plan(instruction, business_name=business, today=today,
                                           tools_available=[x["name"] for x in tools],
                                           tenant_id=tenant_id)
        except Exception:
            log.exception("task planner failed (tenant=%s task=%s)", tenant_id, t["id"])
            return {"status": "failed",
                    "outcome": "I couldn't plan that one — nothing changed."}, detail
        usage.record_anthropic(tenant_id, planned.model, planned.usage,
                               operation=task_loop.PLAN_OPERATION, module="concierge")
        plan = planned.value

        async def attempt(corrections: tuple):
            """One worker run and its review: (the worker's result, the review or None)."""
            trail = task_loop.Trail()
            note = TASK_NOTE + "\n\n" + task_loop.render_plan(plan, corrections)
            result = await concierge.answer_business(
                instruction, [], tools=tools, tool_executor=worker_executor(trail),
                business_name=business, today=today, full_qa=full_qa, tenant_id=tenant_id,
                actions=True, question_note=note, model=settings.anthropic_worker_model)
            usage.record_anthropic(tenant_id, result.model, result.usage,
                                   operation="concierge_task", module="concierge")
            try:
                checked = await task_loop.review(instruction, plan=plan, trail=trail.entries,
                                                 reply=result.reply, action=result.action,
                                                 tenant_id=tenant_id)
            except Exception:
                log.exception("task reviewer failed (tenant=%s task=%s)", tenant_id, t["id"])
                return result, None
            usage.record_anthropic(tenant_id, checked.model, checked.usage,
                                   operation=task_loop.REVIEW_OPERATION, module="concierge")
            return result, checked.value

        result, review = await attempt(())
        if review is not None and review["verdict"] == "fix":
            detail["retries"] = 1          # one retry, with the reviewer's corrections
            result, review = await attempt(tuple(review["corrections"]))
        if review is None:
            return {"status": "failed",
                    "outcome": "I couldn't check that one — nothing changed."}, detail
        detail["verdict"] = review["verdict"]
        if review["verdict"] == "pass":
            return _landing(result, tenant_id=tenant_id, user_id=user_id, full_qa=full_qa,
                            scheduling_on=scheduling_on, member=member), detail
        # escalate, or a second non-pass: the owner decides; nothing is staged.
        return {"status": "needs_you", "outcome": review["reason"][:600]}, detail


def _landing(result, *, tenant_id: str, user_id: str | None, full_qa: bool,
             scheduling_on: bool, member) -> dict:
    """Where a reviewed-pass answer lands: the same three outcomes as the single path."""
    from app import concierge_actions
    if result.action:
        try:
            card = concierge_actions.stage(tenant_id, None, result.action, full_qa=full_qa,
                                           scheduling_on=scheduling_on, user_id=user_id,
                                           member=member, park=True)
        except concierge_actions.ActionError as e:
            return {"status": "failed", "outcome": str(e)}
        return {"status": "waiting", "outcome": card["summary"],
                "proposal_id": card["proposal_id"]}
    reply = (result.reply or "").strip()
    return {"status": "needs_you" if reply.endswith("?") else "done",
            "outcome": reply[:600] or "Done."}


_MARKS = {"waiting": "⏳", "done": "✓", "needs_you": "?", "failed": "✗"}


def summary_text(tasks: list[dict]) -> str:
    """Her one message when the list is finished — what the conversation (and
    her memory) keeps of it."""
    lines = []
    for t in tasks:
        st = t.get("status")
        what = {"waiting": "waiting for your yes", "done": t.get("outcome") or "done",
                "needs_you": t.get("outcome") or "I need one detail",
                "failed": t.get("outcome") or "couldn't be done"}.get(st, st or "")
        lines.append(f"{_MARKS.get(st, '·')} {t['title']} — {what}")
    return "Your task list is finished:\n" + "\n".join(lines)


def _finish_batch(tenant_id: str, user_id: str | None, batch_id: str) -> None:
    from app import push
    try:
        tasks = db.list_concierge_tasks(tenant_id, [batch_id])
        if any(t.get("revision", 0) > 0 for t in tasks):
            for task in tasks:
                if not task.get("event_id"):
                    continue
                notice = delegation.transition("notice_claim", tenant_id, user_id,
                    task_id=task["id"], revision=task["revision"], data={"event_id": task["event_id"]})
                if not notice.get("ok"):
                    continue
                # A notification is an invitation to read current scoped truth,
                # never a permanent copy of private lookup/action material.
                db.add_concierge_message(tenant_id, "assistant", "A task update is ready. Open your task list to check it.", user_id=user_id)
                push.notify(tenant_id, title="Task update", body="A task update is ready — open Concierge.",
                            url="/app/#concierge", tag=f"task-{task['id']}-{task['event_id']}", kind="concierge_tasks_done")
            return
        if any(t.get("status") in db.TASK_OPEN for t in tasks):
            return
        db.add_concierge_message(tenant_id, "assistant", summary_text(tasks), user_id=user_id)
        events.publish(tenant_id, {"type": "concierge_tasks_done", "batch_id": batch_id})
        waiting = sum(1 for t in tasks if t.get("status") == "waiting")
        asks = sum(1 for t in tasks if t.get("status") == "needs_you")
        bits = []
        if waiting:
            bits.append(f"{waiting} waiting for your yes")
        if asks:
            bits.append(f"{asks} need{'s' if asks == 1 else ''} a detail")
        done = sum(1 for t in tasks if t.get("status") == "done")
        failed = sum(1 for t in tasks if t.get("status") == "failed")
        if done:
            bits.append(f"{done} done")
        if failed:
            bits.append(f"{failed} couldn't be prepared")
        cancelled = sum(1 for t in tasks if t.get("status") in {"cancelled", "declined"})
        if cancelled:
            bits.append(f"{cancelled} not run")
        push.notify(tenant_id, title="Your tasks are ready",
                    body=" · ".join(bits) or "Check your task list", url="/app/#concierge",
                    tag=f"tasks-{batch_id}", kind="concierge_tasks_done")
    except Exception:
        log.exception("task batch finish failed (tenant=%s batch=%s)", tenant_id, batch_id)


async def resume_unfinished() -> int:
    """Start-up: pick up every batch a deploy or restart stopped mid-way."""
    await asyncio.sleep(5)   # let the app finish starting
    try:
        rows = db.list_unfinished_concierge_tasks()
    except Exception:
        log.exception("unfinished tasks read failed")
        return 0
    seen = {}
    for r in rows:
        if r.get("revision", 0) > 0:
            if not delegation.enabled():
                continue
            recovered = delegation.transition("recover", r["tenant_id"], r.get("user_id"),
                                               task_id=r["id"], revision=r["revision"])
            if not recovered.get("ok"):
                continue
        seen.setdefault(r["batch_id"], r)
    for batch_id, r in seen.items():
        launch(r["tenant_id"], r.get("user_id"), batch_id)
    if seen:
        log.info("resumed %d concierge task batch(es)", len(seen))
    return len(seen)


# ── Following a task's card after it lands (Kris 2026-09-25: "Not all the tasks
# ended with a tick. They're all in pending as far as I see.") ───────────────
# A waiting task is settled by what the owner does with its card: confirmed →
# done (✓, with what ran), a "no" or the bin → declined. Bringing the card back
# makes a NEW card, so the task follows it to the new id first.

def follow_card(tenant_id: str, old_proposal_id: str, new_proposal_id: str) -> None:
    try:
        for t in db.concierge_tasks_for_proposal(tenant_id, old_proposal_id):
            if t.get("revision", 0) > 0:
                continue
            db.update_concierge_task(t["id"], {"proposal_id": new_proposal_id})
            _publish(tenant_id, t["batch_id"], {**t, "proposal_id": new_proposal_id})
    except Exception:
        log.warning("task follow failed (proposal=%s)", old_proposal_id, exc_info=True)


def settle_card(tenant_id: str, proposal_id: str, status: str, outcome: str) -> None:
    try:
        for t in db.concierge_tasks_for_proposal(tenant_id, proposal_id):
            if t.get("revision", 0) > 0:
                continue
            if t.get("status") not in {"waiting", "needs_you"}:
                continue
            fields = {"status": status, "outcome": (outcome or "")[:600], "finished_at": _now()}
            db.update_concierge_task(t["id"], fields)
            _publish(tenant_id, t["batch_id"], {**t, **fields})
    except Exception:
        log.warning("task settle failed (proposal=%s)", proposal_id, exc_info=True)
