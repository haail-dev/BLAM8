"""C3: durable task/revision authority. Events are hints, speech is a receipt read."""
from __future__ import annotations

import uuid
import hashlib
import json
import re
from dataclasses import dataclass
from app import concierge_settings, db, task_expiry, task_priority
from app.config import settings


class Superseded(RuntimeError):
    pass


def enabled():
    return settings.concierge_delegation_enabled


def transition(operation, tenant_id, user_id, *, task_id=None, revision=None,
               attempt_id=None, data=None, client=None):
    """No unsafe fallback when the prerequisite migration is missing."""
    # Only the backend service role can invoke authority transitions. The
    # caller performs scoped/current-permission checks before reaching here.
    result = db.db().rpc("concierge_task_transition", {
        "p_operation": operation, "p_tenant": tenant_id, "p_user": user_id,
        "p_task": task_id, "p_revision": revision, "p_attempt": attempt_id,
        "p_data": data or {},
    }).execute().data
    if not isinstance(result, dict):
        raise RuntimeError("Task transition returned no durable receipt")
    return result


def binding(row):
    value = (row.get("payload") or {}).get("_delegation")
    return value if isinstance(value, dict) else None


def access_scope(tenant_id, user_id):
    """Conservative snapshot: any membership/module change requires a new read.

    This includes visibility scopes, not only tool doors. An answer obtained
    before a narrower CRM/mailbox grant must not be disclosed afterwards.
    """
    from app import people
    raw = db.get_member(tenant_id, user_id) if user_id else None
    member = people.member(raw) if raw else None
    if member is not None and not member.get("active", True):
        raise PermissionError("Membership is inactive")
    modules = db.visible_modules(tenant_id)
    permissions = {key: member.get(key) for key in ("user_id", "role", "active", "scopes")} if member else None
    digest = hashlib.sha256(json.dumps({"member": permissions, "modules": sorted(modules)},
                                      sort_keys=True, default=str).encode()).hexdigest()
    return digest, member, modules


@dataclass(frozen=True)
class TaskReadPermissions:
    tenant_id: str
    user_id: str
    client: object
    asker: object
    scope: str


def task_read_permissions(ctx):
    """Fresh permissions for one read-only history hydration; never cached.

    Action/transition callers continue to use scoped_task's fresh default.
    Each task/revision/proposal/mailbox is still checked individually.
    """
    from app.concierge_routes import _live_asker
    asker = _live_asker(ctx)
    scope, _, _ = access_scope(ctx.tenant_id, ctx.user_id)
    return TaskReadPermissions(ctx.tenant_id, ctx.user_id, ctx.client, asker, scope)


def scoped_task(ctx, task_id, *, permissions=None):
    if permissions is not None and (not isinstance(permissions, TaskReadPermissions)
            or permissions.tenant_id != ctx.tenant_id or permissions.user_id != ctx.user_id
            or permissions.client is not ctx.client):
        raise PermissionError("Task permission context does not match requester")
    row = db.get_concierge_task(ctx.tenant_id, ctx.user_id, task_id, client=ctx.client)
    if not row or not row.get("revision"):
        return None
    # Current permissions apply to the prepared action, not only the session.
    from app.concierge_routes import _live_asker, _proposal_refusal
    if permissions is None:
        _live_asker(ctx)
        scope, _, _ = access_scope(ctx.tenant_id, ctx.user_id)
    else:
        scope = permissions.scope
    result = row.get("result") or {}
    if result.get("scope") and result["scope"] != scope:
        return None
    if result.get("kind") in {"lookup_answer", "needs_input"} and result.get("scope") != scope:
        return None
    if row.get("proposal_id"):
        proposal = db.get_concierge_proposal(row["proposal_id"], ctx.tenant_id, client=ctx.client)
        refused = (not proposal or (_proposal_refusal(ctx, proposal) if permissions is None
                                   else _proposal_refusal(ctx, proposal, asker=permissions.asker)))
        if refused:
            return None
    return row


def _tenant_or_none(ctx):
    """Only the timeouts come from this read: failing it must not cost the list."""
    try:
        return db.get_tenant(ctx.tenant_id, client=ctx.client)
    except Exception:
        return None


def scoped_public_tasks(ctx, rows):
    """`public()` rows for rows the caller already fetched, in input order.

    Same checks as `scoped_task` per row, but the requester's permissions are
    read once and every task's proposal comes from one query, instead of
    re-reading the task, the member, the modules and the proposal for each.
    """
    from app.concierge_routes import _proposal_refusal
    from app import parallel_reads
    rows = [row for row in rows if row.get("revision") and row.get("tenant_id") == ctx.tenant_id
            and row.get("user_id") == ctx.user_id]
    if not rows:
        return []
    permissions = task_read_permissions(ctx)
    # The proposals and the business's own timeouts are independent reads.
    proposals, tenant = parallel_reads.run_all([
        lambda: db.get_concierge_proposals([row.get("proposal_id") for row in rows], ctx.tenant_id, client=ctx.client),
        lambda: _tenant_or_none(ctx)])
    limits = concierge_settings.resolved(tenant)

    def visible(row):
        result = row.get("result") or {}
        if result.get("scope") and result["scope"] != permissions.scope:
            return False
        if result.get("kind") in {"lookup_answer", "needs_input"} and result.get("scope") != permissions.scope:
            return False
        if row.get("proposal_id"):
            proposal = proposals.get(row["proposal_id"])
            return bool(proposal) and not _proposal_refusal(ctx, proposal, asker=permissions.asker)
        return True
    # Mailbox checks can read the database, so the per-row checks overlap.
    flags = parallel_reads.run_all([lambda row=row: visible(row) for row in rows])
    return [public(row, proposals.get(row.get("proposal_id")), limits) for row, ok in zip(rows, flags) if ok]


TYPED_NOTE_HOURS = 24   # the typed path has no session to bound it: open tasks plus this many hours
NOTE_MAX_CHARS = 6000   # the whole note rides in every call: past this the oldest requests are left out
_NOTE_HEAD = ("Current delegated requests on THIS surface/session. Ask which one if ambiguous; never guess a task ID. "
              "This is only what was started from here recently, not the whole record: for what became of anything "
              "else, call list_awaiting_approval, and never answer 'no task for X' from this list alone. ")


def context_note(ctx, voice_session, *, surface="voice"):
    if not enabled() or (surface == "voice" and not voice_session):
        return ""
    typed = surface == "typed"
    rows = db.list_session_concierge_tasks(ctx.tenant_id, ctx.user_id, voice_session, client=ctx.client,
                                           recent_hours=TYPED_NOTE_HOURS if typed else None)
    if typed:
        # One batched, scoped read (permissions once, every card in one query) rather than
        # scoped_task's four reads per row, up to 40 rows, on every typed turn.
        kept = {task["id"] for task in scoped_public_tasks(ctx, rows)}
        scoped_rows = [row for row in rows if row["id"] in kept]
        scoped_rows.sort(key=lambda row: row.get("status") in db.TASK_SETTLED)   # unsettled first, stable
    else:
        scoped_rows = [scoped for scoped in (scoped_task(ctx, row["id"]) for row in rows) if scoped]
    current = []
    for scoped in scoped_rows:
        keys = ("id", "revision", "title", "instruction", "status", "commit_state")
        if typed and scoped.get("status") in db.TASK_SETTLED:
            # A settled task's wording is no use to the next turn, and it rides in every call.
            keys = tuple(key for key in keys if key != "instruction")
        current.append({key: scoped.get(key) for key in keys})
    shown = list(current)
    while shown and len(_NOTE_HEAD + json.dumps(shown)) > NOTE_MAX_CHARS:
        shown.pop()
    note = _NOTE_HEAD + json.dumps(shown)
    if len(shown) < len(current):
        note += f" {len(current) - len(shown)} older requests are not listed: ask which one they mean."
    return note


_FETCH = object()


def public(row, proposal=_FETCH, limits=None):
    """No instructions, original payload, attempts or raw provider exceptions."""
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    kind = result.get("kind")
    summary = result.get("summary") if isinstance(result.get("summary"), str) else None
    proposal_state = None
    if kind == "action_receipt":
        from app import proposal_outcomes
        if proposal is _FETCH:
            proposal = db.get_concierge_proposal(row.get("proposal_id"), row["tenant_id"])
        receipt = proposal_outcomes.public(proposal or {})
        summary = receipt["summary"]
        proposal_state = receipt["state"]
        kind = {"completed": "action_completed", "failed": "failed", "cancelled": "cancelled",
                "pending": "waiting"}.get(proposal_state, "unknown")
    if kind not in {"lookup_answer", "waiting", "needs_input", "failed", "cancelled", "unknown", "action_completed"}:
        summary = None
    priority = 0 if kind in {"waiting", "needs_input"} else task_priority.classify(row.get("title"), row.get("instruction"))
    return {key: row.get(key) for key in
            ("id", "batch_id", "title", "status", "proposal_id", "revision", "row_version", "event_id", "voice_session", "commit_state")} | {
        "outcome": summary or ("Working on this request." if row.get("status") in {"queued", "working"}
                              else "The result isn't verified. Check this task."),
        "priority": priority, **task_expiry.assess(row, limits=limits), "result_kind": kind, "proposal_state": proposal_state, "speakable": bool(summary),
        "reads": result.get("reads", []) if kind == "lookup_answer" else [],
        "question": result.get("question") if kind == "needs_input" else None,
        "delivery": {key: (row.get("delivery") or {}).get(key) for key in ("state",)},
    }


def create(ctx, tasks, *, surface, source_turn, voice_session):
    result = transition("create", ctx.tenant_id, ctx.user_id, client=ctx.client, data={
        "scope": access_scope(ctx.tenant_id, ctx.user_id)[0],
        "batch_id": str(uuid.uuid4()), "surface": surface, "source_turn": source_turn,
        "voice_session": voice_session, "tasks": [{**task, "position": i} for i, task in enumerate(tasks)],
    })
    if not result.get("ok") or not result.get("tasks"):
        raise RuntimeError("No durable delegated task was created")
    return result["tasks"]


def control(ctx, task_id, revision, operation, instruction=""):
    if not enabled() or getattr(ctx, "viewing_as", None):
        return {"ok": False, "state": "unavailable"}
    row = scoped_task(ctx, task_id)
    if not row:
        return {"ok": False, "state": "unavailable"}
    if operation == "revise":
        instruction = " ".join(instruction.split())
        words = re.findall(r"[\w'-]+", instruction.lower())
        generic = {"move", "check", "look", "up", "cancel", "revise", "send", "email", "the", "a", "for", "to", "on", "at", "please", "instead", "actually", "booking", "task", "request",
                   "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today", "tomorrow"}
        target_words = {word for word in words if word not in generic and not word.isdigit()}
        fragment = (not words or words[0] in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "instead", "actually", "same"}
                    or any(word in {"it", "that", "them", "those", "there"} for word in words))
        if len(instruction) < 12 or len(words) < 3 or fragment or not target_words:
            return {"ok": False, "state": "needs_input", "summary": "Give the complete replacement request, including the target."}
    result = transition(operation, ctx.tenant_id, ctx.user_id, task_id=task_id,
                        revision=revision, data={"instruction": instruction}, client=ctx.client)
    current = result.get("task")
    return {"ok": bool(result.get("ok")), "state": result.get("state") or operation,
            "task": public(current) if current else None}
