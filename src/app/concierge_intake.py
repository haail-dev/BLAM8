"""The intake receipt (docs/review-agentic-concierge-2026-10-16.md, R6).

When one message asks for several things, `start_tasks` takes the separate jobs and
the model accounts for every other part: answered from a lookup it had already made,
could not be done (with the reason), or a question it put back. That accounting used
to live only in the spoken sentence, so "what happened to my fourth thing?" had
nothing to read once the chat history had moved on.

The receipt is a hidden entry on the turn's assistant message (`concierge_messages.trail`,
beside `message_origin`; no new table or column). It holds only:

  * what the owner said (the message, clipped) and what was said back up front;
  * the tasks that were started (their ids: their LIVE state is always read from the
    task and card rows, never copied here);
  * the other parts as the model declared them, each with a how and a reason;
  * the read tools that actually ran in that turn (recorded by the route, not declared).

The ledger (src/app/concierge_ledger.py) turns it into `asked_together`. Nothing here
decides that anything happened: a part the model called "answered" is only credited
when a lookup is on record for that turn.
"""
from __future__ import annotations

NAME = "intake_receipt"
HOWS = ("answered", "cannot", "asking")
MAX_OTHERS = 8
SAID_CHARS = 100
BECAUSE_CHARS = 140
ASKED_CHARS = 300
NOW_CHARS = 300

# The `others` property of start_tasks. One entry per part of the owner's message that
# did NOT become a task, in their own words.
OTHERS_SCHEMA = {
    "type": "array",
    "description": ("Every part of the message that is NOT a task, so none is lost: `said` = the "
                    "owner's own words for it, `how` = answered (you already have the facts from a "
                    "lookup you ran), cannot (nothing fits, or nothing to do) or asking (you need "
                    "one detail first), `because` = the reason or the question, a short sentence."),
    "items": {
        "type": "object",
        "properties": {
            "said": {"type": "string"},
            "how": {"type": "string", "enum": list(HOWS)},
            "because": {"type": "string"},
        },
        "required": ["said", "how"],
    },
}


def _clip(value, n: int) -> str:
    return " ".join(str(value or "").split())[:n]


def clean_others(raw) -> list[dict]:
    """The model's declared non-task parts, made safe to keep: a known `how`, short text."""
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_OTHERS]:
        if not isinstance(item, dict):
            continue
        said, how = _clip(item.get("said"), SAID_CHARS), str(item.get("how") or "").strip().lower()
        if said and how in HOWS:
            out.append({"said": said, "how": how, "because": _clip(item.get("because"), BECAUSE_CHARS)})
    return out


def declared(args: dict) -> dict:
    """What start_tasks was told about the parts it did not take on."""
    args = args if isinstance(args, dict) else {}
    return {"others": clean_others(args.get("others")), "now": _clip(args.get("now"), NOW_CHARS)}


def receipt(batch: dict | None, intake: dict | None, *, question: str, tools_called: list) -> dict | None:
    """The trail entry to keep with this turn's reply, or None when no task list started.

    `batch` is what the starter created (ids and titles from the rows); `intake` is
    what the model declared; `tools_called` is the route's record of every tool that
    ran, from which only the read tools of the catalogue are kept."""
    if not batch or not batch.get("batch_id") or not intake:
        return None
    from app import concierge_data
    lookups = []
    for name in tools_called or []:
        if name in concierge_data.TOOL_NAMES and name not in lookups:
            lookups.append(name)
    tasks = [{"id": t.get("id"), "title": _clip(t.get("title"), 60)} for t in batch.get("tasks") or [] if t.get("id")]
    others = intake.get("others") or []
    if len(tasks) + len(others) < 2:
        return None          # one thing asked: there is nothing to account for beyond its own task
    return {"name": NAME, "v": 1, "batch_id": batch["batch_id"],
            "asked": _clip(question, ASKED_CHARS), "now": intake.get("now") or "",
            "tasks": tasks, "others": others, "lookups": lookups}


def from_trail(trail) -> dict | None:
    """The receipt on a stored message's trail, if it has one."""
    for entry in trail or []:
        if isinstance(entry, dict) and entry.get("name") == NAME:
            return entry
    return None
