"""Read-only rich Concierge history projection shared by page and catch-up."""
import json
import logging
import re
from app import db
from app.startup_timing import instrument, span
from app.auth import TenantCtx
log = logging.getLogger(__name__)
_TASKS_MARK = re.compile(r"\s*\[Tasks: ([0-9a-f-]{36})\]\s*$")

@instrument("task_hydration")
def attach_task_lists(ctx: TenantCtx, history: list[dict]) -> None:
    """A reply that started a task list (docs/plan-concierge-task-list.md)
    ends in a [Tasks: <batch>] marker: take it off the words and hang the
    list's current state on the message, for the panel to draw under it."""
    marked = {}
    for m in history:
        if m.get("role") != "assistant":
            continue
        hit = _TASKS_MARK.search(m.get("content") or "")
        if hit:
            m["content"] = (m.get("content") or "")[:hit.start()]
            marked[hit.group(1)] = m
    if not marked:
        return
    try:
        rows = db.list_concierge_tasks(ctx.tenant_id, list(marked), client=ctx.client)
    except Exception:
        log.warning("task lists read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return
    from app import concierge_delegation
    wanted = [row for row in rows
              if row.get("user_id") == ctx.user_id and row.get("batch_id") in marked and row.get("revision", 0) > 0]
    scoped_by_id = {}
    if wanted:
        with span("task_scope"):
            scoped_by_id = {row["id"]: row for row in concierge_delegation.scoped_public_tasks(ctx, wanted)}
    for batch_id, m in marked.items():
        tasks = []
        for row in rows:
            if row.get("batch_id") != batch_id or row.get("user_id") != ctx.user_id:
                continue
            if row.get("revision", 0) > 0:
                if scoped_by_id.get(row["id"]):
                    tasks.append(scoped_by_id[row["id"]])
            else:
                tasks.append({key: row.get(key) for key in ("id", "title", "status", "outcome", "proposal_id")})
        if tasks:
            # A list that is finished (nothing queued or working) starts folded.
            done = all(t["status"] not in db.TASK_OPEN for t in tasks)
            m["tasks_json"] = json.dumps({"batch_id": batch_id, "tasks": tasks, "collapsed": done})
