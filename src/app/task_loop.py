"""Planner -> worker -> reviewer for one Concierge task (docs/plan-planner-worker-reviewer.md).

Phase 1, behind settings.concierge_task_loop == "split" (the default "single"
never reaches this module). The planner writes a plan with checkable success
criteria before the worker runs any lookup. The reviewer then checks the
worker's tool trail and its proposed change against those criteria. Neither
seat can change what lands: a passed change is still parked for the owner's yes.
"""
from __future__ import annotations

import json
import logging
from typing import NamedTuple

from app import llm, metering
from app.config import settings

log = logging.getLogger(__name__)

PLAN_OPERATION = "concierge_task_plan"
REVIEW_OPERATION = "concierge_task_review"
TRAIL_CAP = 12_000     # characters of trail the reviewer sees, as agent.py caps its draft trail
RESULT_CAP = 1_500     # characters kept of one lookup's result
RISKS = ("low", "medium", "high")
VERDICTS = ("pass", "fix", "escalate")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "description": "What the owner wants, in one sentence."},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "Short steps, in order."},
        "lookups": {"type": "array", "items": {"type": "string"},
                    "description": "The lookups that settle the task."},
        "success_criteria": {"type": "array", "items": {"type": "string"},
                             "description": "Two to five facts the evidence must show."},
        "risk": {"type": "string", "enum": list(RISKS)},
    },
    "required": ["intent", "steps", "lookups", "success_criteria", "risk"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "failed_criteria": {"type": "array", "items": {"type": "string"}},
        "corrections": {"type": "array", "items": {"type": "string"},
                        "description": "For fix only: one short instruction each."},
        "reason": {"type": "string",
                   "description": "Plain words for the owner, at most two sentences."},
    },
    "required": ["verdict", "failed_criteria", "corrections", "reason"],
    "additionalProperties": False,
}

PLANNER_SYSTEM = (
    "You plan one task for a small business's assistant. Another assistant works the task "
    "afterwards. It can look things up, but it cannot change this plan.\n"
    "Write the plan before any lookup: the intent in one sentence, the steps in order, the "
    "lookups that settle it, and two to five success criteria. Each criterion must be a fact "
    "the evidence can show, such as 'the lookups show nothing booked on Sunday 11 October and "
    "the proposed change blocks that date'. Never write 'the task is done properly'.\n"
    "A change is only proposed here; the owner confirms it later. So each criterion must be "
    "checkable from the lookups and the proposed change, before the owner's yes.\n"
    "Risk is low for a question or a note, medium for a change to one record, and high for a "
    "change that touches money, many people or messages to customers.\n"
    "Plain words. No markup. Reply with the JSON only.")

REVIEWER_SYSTEM = (
    "You check one piece of work from a small business's assistant before the owner sees it. "
    "You get the owner's instruction, the plan with its success criteria, the tool trail "
    "(every lookup that ran, its arguments and what came back), and the assistant's reply and "
    "proposed change.\n"
    "Judge the evidence, not the assistant's summary. For each success criterion, find the "
    "trail entry or the proposed change that shows it holds. If a lookup came back empty or "
    "failed, or the reply says something the trail does not show, say so.\n"
    "The proposed change is a proposal. The owner confirms it later, from their waiting list. "
    "It has not been applied, and the trail cannot show it applied. So judge whether the "
    "proposal is the right change given the lookups: the right record, the right date, and "
    "nothing in the way. Do not judge whether it has taken effect.\n"
    "Verdicts. pass: the instruction is met and every criterion is shown. fix: something is "
    "wrong or unshown and one more try can correct it. Give each correction as one short "
    "instruction and add no new goals. escalate: the owner must decide or check, or the "
    "evidence cannot settle it.\n"
    "The reason is for the owner, in plain words, at most two sentences. Reply with the JSON "
    "only.")


class Seat(NamedTuple):
    """One seat's parsed answer, the model that answered, and its usage."""
    value: dict
    model: str
    usage: object


def planner_model() -> str:
    return settings.anthropic_planner_model or settings.anthropic_model


def reviewer_model() -> str:
    return settings.anthropic_reviewer_model or settings.anthropic_model


def _text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _json_object(msg) -> dict:
    data = json.loads(_text(msg))
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    return data


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return [s for s in (str(x).strip() for x in value) if s]


def _clean_plan(data: dict) -> dict:
    intent = str(data.get("intent") or "").strip()
    criteria = _strings(data.get("success_criteria"))
    if not intent or not 2 <= len(criteria) <= 5 or data.get("risk") not in RISKS:
        raise ValueError("the plan has no intent, not two to five criteria, or no known risk")
    return {"intent": intent, "steps": _strings(data.get("steps")),
            "lookups": _strings(data.get("lookups")), "success_criteria": criteria,
            "risk": data["risk"]}


def _clean_review(data: dict) -> dict:
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    corrections = _strings(data.get("corrections"))
    if verdict == "fix" and not corrections:
        verdict = "escalate"   # a fix nobody can name is the owner's to decide
    reason = str(data.get("reason") or "").strip()
    if verdict != "pass" and not reason:
        reason = "I couldn't confirm this one is right, so nothing changed."
    return {"verdict": verdict, "failed_criteria": _strings(data.get("failed_criteria")),
            "corrections": corrections if verdict == "fix" else [], "reason": reason}


def render_plan(plan: dict, corrections=()) -> str:
    """The plan as the worker reads it. Corrections from a fix, if any, follow it."""
    lines = ["THE PLAN. It was written before any lookup. Follow it; you cannot change it.",
             f"Intent: {plan['intent']}", "Steps:"]
    lines += [f"{i}. {step}" for i, step in enumerate(plan["steps"], 1)]
    lines.append("Lookups: " + ("; ".join(plan["lookups"]) or "none"))
    lines.append("Success criteria (the reviewer checks each one against your lookups and "
                 "proposed change):")
    lines += [f"- {c}" for c in plan["success_criteria"]]
    lines.append(f"Risk: {plan['risk']}")
    if corrections:
        lines += ["", "THE REVIEWER'S CORRECTIONS to your last try. Apply them: look up again "
                      "and answer afresh."]
        lines += [f"- {c}" for c in corrections]
    return "\n".join(lines)


class Trail:
    """The lookups one worker run made, in order: {tool, args, result_summary}.
    Capped like agent.py's draft trail, with a note where the cap cut it short."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._size = 0
        self._cut = False

    def add(self, tool: str, args: dict, result) -> None:
        if self._cut:
            return
        entry = {"tool": tool, "args": args,
                 "result_summary": json.dumps(result, ensure_ascii=False, default=str)[:RESULT_CAP]}
        size = len(json.dumps(entry, ensure_ascii=False, default=str))
        if self._size + size > TRAIL_CAP:
            self._cut = True
            self.entries.append({"note": f"More lookups ran; the trail is capped at "
                                         f"{TRAIL_CAP} characters."})
            return
        self.entries.append(entry)
        self._size += size


async def plan(instruction: str, *, business_name: str | None, today: str,
               tools_available: list[str], tenant_id: str | None) -> Seat:
    """The planner seat: one call, no tools, a JSON plan. Raises on any failure."""
    from app import concierge
    model = planner_model()
    msg = await llm.create(
        client=concierge.client(), tenant_id=tenant_id, operation=PLAN_OPERATION,
        module="concierge", model=model, max_tokens=1500, system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": (
            f"BUSINESS: {business_name or 'this business'}\n"
            f"TODAY: {today}\n"
            f"TOOLS THE ASSISTANT HAS: {', '.join(tools_available) or 'none'}\n\n"
            f"TASK:\n{instruction}")}],
        output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
    )
    totals = metering.Totals()
    metering.add(totals, msg.usage)
    return Seat(_clean_plan(_json_object(msg)), getattr(msg, "model", None) or model, totals)


async def review(instruction: str, *, plan: dict, trail: list[dict], reply: str,
                 action: dict | None, tenant_id: str | None) -> Seat:
    """The reviewer seat: one call, no tools. It judges the evidence against the
    plan's criteria, never the worker's summary alone. Raises on any failure."""
    from app import concierge
    model = reviewer_model()
    shown = json.dumps(trail, ensure_ascii=False, default=str) if trail else "(no lookups ran)"
    proposed = json.dumps(action, ensure_ascii=False, default=str) if action else "(none)"
    brief = (f"THE OWNER'S INSTRUCTION:\n{instruction}\n\n{render_plan(plan)}\n\n"
             f"TOOL TRAIL (every lookup, its arguments, and what came back):\n{shown}\n\n"
             f"THE ASSISTANT'S REPLY:\n{reply or '(none)'}\n\n"
             f"THE ASSISTANT'S PROPOSED CHANGE:\n{proposed}")
    msg = await llm.create(
        client=concierge.client(), tenant_id=tenant_id, operation=REVIEW_OPERATION,
        module="concierge", model=model, max_tokens=1500, system=REVIEWER_SYSTEM,
        messages=[{"role": "user", "content": brief}],
        output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
    )
    totals = metering.Totals()
    metering.add(totals, msg.usage)
    return Seat(_clean_review(_json_object(msg)), getattr(msg, "model", None) or model, totals)
