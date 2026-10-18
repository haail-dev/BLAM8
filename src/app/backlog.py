"""Owner-chosen historical import — filed, never drafted, no contacts.

Kris, stays run #10 (2026-09-13): "does the user have to wait for new
inbound to be able to see anything in the inbox?" They did — a fresh
connection started from "now" (the Gmail watermark, the Graph
'created' subscription), so the inbox read "All clear" and the connect
step's own advice ("have a look at what arrives, tell the agent which
senders to ignore, then switch drafting on") could not happen on the day.
"That's what we need, because then it really gives them a chance to
ignore certain addresses."

After an explicit choice, bring in the DAYS before connection (at most LIMIT
messages), each filed the way held mail is filed — an email_log row with
status skipped, category fyi, and a reason that says it arrived before
BLAM8 — so the conversation list fills at once and every sender can be
ignored straight away. Nothing is drafted from it, no CRM contact is
minted from it, and the pipeline's (tenant, source_message_id) uniqueness
means push/poll can never draft one of these later by mistake (they are
older than the watermark anyway). A failure never disconnects the mailbox.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone

from app import actor, db, gmail, graph, mailbox
from fastapi import HTTPException

log = logging.getLogger("blautom8.backlog")

DAYS = 14
LIMIT = 100
REASON = ("Before BLAM8 — arrived before the mailbox was connected, so nothing is "
          "drafted to it. Ignore the sender if it's a robot or a newsletter.")

_TASKS: set = set()


async def choose_import(request, ctx):
    """An authenticated explicit choice; OAuth and GET requests never call this."""
    from app import people
    if not ctx.user_id or getattr(ctx, "viewing_as", None):
        raise HTTPException(403, "Choose import from your own account.")
    try:
        member = db.get_member(ctx.tenant_id, ctx.user_id)
        if not people.can_do(people.member(member) if member else None, "settings", "change"):
            raise HTTPException(403, "You cannot change mailbox import settings.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Your permission could not be verified. Nothing started.") from None
    form = await request.form()
    connection, decision = str(form.get("connection_id", "")), str(form.get("decision", ""))
    if decision not in {"import", "skip", "later"} or not connection:
        raise HTTPException(400, "Choose a mailbox and an import option.")
    if not people.may_read_mailbox(people.member(member) if member else None, connection):
        raise HTTPException(403, "You cannot import this mailbox.")
    try:
        # Tenant-scoped metadata read before using the privileged claim RPC.
        db.mailbox_import_access(ctx.tenant_id, connection, client=ctx.client)
        chosen = db.choose_mailbox_import(ctx.tenant_id, connection, ctx.user_id,
                                         decision, str(form.get("expected_run", "")))
    except Exception:
        raise HTTPException(503, "The import choice could not be confirmed. Refresh before retrying.") from None
    if not chosen:
        raise HTTPException(409, "The import changed or this choice was already used. Refresh to check its status.")
    if decision == "import" and not start_import(ctx.tenant_id, connection):
        raise HTTPException(503, "The choice was recorded but a start could not be confirmed. Refresh to check its status.")


def verified_run(tenant_id, conn_id, run):
    """No provider access from a fabricated, stale, unapproved or expired run."""
    access = db.mailbox_import_access(tenant_id, conn_id)
    current = access.get("backlog_import") or {}
    if (not run or current.get("run_id") != run.get("run_id") or current.get("state") != "running"
            or current.get("approval", {}).get("decision") != "import"
            or current.get("approval", {}).get("version") != 1
            or datetime.fromisoformat(current["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
        raise PermissionError("Historical import has no current approved run")
    return current


def start_import(tenant_id: str, conn_id: str) -> bool:
    """All entry points claim before redirecting; concurrent retries do no work.

    Fail closed if durable state is unavailable. The connection itself stands.
    A 10-minute worker deadline precedes the 15-minute lease recovery window.
    """
    try:
        loop = asyncio.get_running_loop()
        run = db.claim_mailbox_import(tenant_id, conn_id)
        if not run:
            return False
        task = loop.create_task(import_recent(tenant_id, conn_id, run=run))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return True
    except Exception:
        log.warning("backlog: could not start import (tenant=%s)", tenant_id, exc_info=True)
        return False


async def recent_inbox_ids(conn: dict, provider: str, access: str,
                           days: int = DAYS, limit: int = LIMIT, before=None) -> list[str]:
    """Provider message ids for the last `days` of INBOX, newest first, at most
    `limit`."""
    if provider == "google":
        return await gmail.list_recent_inbox_ids(access, days=days, limit=limit, before=before)
    return await graph.list_recent_inbox_ids(access, days=days, limit=limit, before=before)


def _shape(msg: dict) -> dict:
    return {
        "from_address": ((msg.get("from") or {}).get("address") or "").strip().lower() or None,
        "from_name": (msg.get("from") or {}).get("name"),
        "subject": msg.get("subject"),
        "body": msg.get("body"),
        "thread_id": msg.get("thread_id"),
        "headers": msg.get("headers") or {},
    }


@actor.agent_work
async def import_recent(tenant_id: str, conn_id: str, *, days: int = DAYS,
                        limit: int = LIMIT, run: dict | None = None) -> int:
    """File the recent backlog of one connection. Returns how many rows were
    written. Never raises — the connect must stand whatever happens here."""
    counts = dict(selected=0, review=0, automated=0, already_known=0, excluded=0,
                  fetch_failed=0, filing_failed=0)
    state = "failed"
    try:
        run = run or db.claim_mailbox_import(tenant_id, conn_id)
        if not run:
            return 0
        run = verified_run(tenant_id, conn_id, run)
        days, limit = run["days"], run["limit"]
        if days != DAYS or limit != LIMIT:
            raise PermissionError("Historical import scope changed")
        async with asyncio.timeout(600):
            await _import_messages(tenant_id, conn_id, days, limit, counts, run=run)
        state = "partial" if counts["fetch_failed"] or counts["filing_failed"] else "complete"
    except asyncio.CancelledError:
        state = "interrupted"
        raise
    except Exception:
        state = "failed"
        log.warning("backlog: import failed (tenant=%s conn=%s)", tenant_id, conn_id, exc_info=True)
    finally:
        if run:
            counts["unprocessed"] = counts["selected"] - sum(
                value for key, value in counts.items() if key != "selected")
            result = {**run, "state": state, "counts": counts, "days": days, "limit": limit,
                      "finished_at": datetime.now(timezone.utc).isoformat()}
            try:
                db.finish_mailbox_import(tenant_id, conn_id, result)
            except Exception:
                log.warning("backlog: outcome write failed (tenant=%s)", tenant_id, exc_info=True)
            _event(tenant_id, conn_id, outcome_summary(result))
    return counts["review"] + counts["automated"]


async def _import_messages(tenant_id, conn_id, days, limit, counts, *, run):
    # Classification and filing policy are unchanged; outcome counters are separate.
    try:
        conn = db.get_connection_by_id(conn_id, tenant_id)
        if not conn:
            raise LookupError("Mailbox not found")
        provider = mailbox.connection_provider(conn, tenant_id)
        access = await mailbox.valid_access_token(conn, provider)
        own = (conn.get("mailbox") or conn.get("address") or "").strip().lower()
        before = datetime.fromisoformat(run["window_end"].replace("Z", "+00:00"))
        ids = await recent_inbox_ids(conn, provider, access, days=days, limit=limit, before=before)
    except Exception:
        log.warning("backlog: listing failed (tenant=%s conn=%s)", tenant_id, conn_id, exc_info=True)
        raise
    counts["selected"] = len(ids[:limit])
    # Robots file under Automated, not the main list (src/app/inbox_rules.py, Kris
    # 2026-09-13) — the owner's first pass through the backlog is people only.
    from app import inbox_rules
    try:
        rules = inbox_rules.settings_of(db.get_tenant_config(tenant_id) or {})
    except Exception:
        rules = dict(inbox_rules.DEFAULTS)
    for mid in ids[:limit]:
        try:
            if db.get_email_log_by_message(tenant_id, mid):
                counts["already_known"] += 1
                continue                                   # already known — push/poll got there
        except Exception:
            counts["filing_failed"] += 1
            continue
        try:
            msg = await mailbox.get_message(tenant_id, mid, mailbox_id=conn_id)
            if not msg:
                counts["fetch_failed"] += 1
                continue
        except Exception:
            counts["fetch_failed"] += 1
            log.warning("backlog: message fetch failed (tenant=%s)", tenant_id, exc_info=True)
            continue
        try:
            email = _shape(msg)
            if not email["from_address"] or email["from_address"] == own:
                counts["excluded"] += 1
                continue                                   # no sender, or the mailbox's own mail
            auto_reason = inbox_rules.automated_reason(email, rules)
            _, created = db.insert_email_log(
                tenant_id, email["from_address"], email.get("subject") or "(no subject)",
                email.get("body") or "", "",
                status="skipped", source_message_id=mid, thread_id=email.get("thread_id"),
                mailbox_id=conn_id, from_name=email.get("from_name"), channel="email",
                category="automated" if auto_reason else "fyi",
                category_reason=(f"Before BLAM8 · {auto_reason}" if auto_reason else REASON))
            counts[("automated" if auto_reason else "review") if created else "already_known"] += 1
        except Exception:
            counts["filing_failed"] += 1
            log.warning("backlog: message %s skipped (tenant=%s)", mid, tenant_id, exc_info=True)
    log.info("backlog: filed %d of %d recent messages (tenant=%s conn=%s)",
             counts["review"] + counts["automated"], counts["selected"], tenant_id, conn_id)


def outcome_summary(run: dict) -> str:
    c = run["counts"]
    return (f"Recent-mail import {run['state']}: {c['selected']} messages selected from the "
            f"last {run['days']} days of Inbox (up to {run['limit']}); "
            f"{c['review']} filed for review; {c['automated']} filed under Automated; "
            f"{c['already_known']} already imported; {c['excluded']} excluded (own mail or no sender); "
            f"{c['fetch_failed']} couldn't be fetched; {c['filing_failed']} couldn't be filed; "
            f"{c['unprocessed']} not processed. "
            "No replies were sent.")


def _event(tenant_id: str, conn_id: str, summary: str) -> None:
    """The outcome in the Agent log — the owner (and the operator) can see
    what the import did without a server log. Best-effort."""
    try:
        db.log_agent_event(tenant_id, "mailbox_backlog", summary, mailbox_id=conn_id)
    except Exception:
        log.warning("backlog: event write failed (tenant=%s)", tenant_id, exc_info=True)


def default_status(tenant_id: str, client=None) -> dict:
    try:
        conns = db.list_mailboxes(tenant_id, client=client)
        if conns:
            return status(tenant_id, conns[0]["id"], client=client)
        return {"state": "not_connected", "message": "Connect a mailbox to import recent messages.", "retry": False}
    except Exception:
        return {"state": "unavailable", "message": "Recent-message import status is unavailable. Please refresh shortly.", "retry": False}


def status(tenant_id: str, conn_id: str, client=None) -> dict:
    """UI state is mailbox-scoped; a failed read never means an empty inbox."""
    try:
        run = db.get_mailbox_import(tenant_id, conn_id, client=client)
        access = db.mailbox_import_access(tenant_id, conn_id, client=client)
        choice = access.get("backlog_import_choice") or {}
        if run["state"] == "running" and datetime.fromisoformat(
                run["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            run = {**run, "state": "interrupted"}
        ready = count_rows(tenant_id, client=client, mailbox_id=conn_id, strict=True)
    except Exception:
        return {"state": "unavailable", "message": "Recent-message import status is unavailable. Please refresh shortly.",
                "retry": False}
    state = run["state"]
    c = run.get("counts", {})
    if state == "running":
        message = "Getting your recent messages ready. We’ll file them without replying."
    elif state == "not_started":
        message = ({"skip": "Historical import skipped. You can choose it later in Settings.",
                    "later": "Historical import deferred. No historical import has started."}
                   .get(choice.get("decision"), "Historical mail has not been imported. Choose whether to import it."))
    elif state in {"failed", "interrupted"}:
        message = "The recent-message import did not finish. You can try importing again."
    elif ready:
        message = f"{ready} recent message{' is' if ready == 1 else 's are'} ready to review."
    elif c.get("selected") == 0:
        message = f"No messages were found in your Inbox from the last {run.get('days', DAYS)} days."
    else:
        message = "No messages are ready to review yet." if state == "partial" else "No recent messages need review."
    details = []
    if ready and state in {"running", "failed", "interrupted", "not_started"}:
        details.append(f"{ready} recent message{' is' if ready == 1 else 's are'} currently ready to review.")
    for key, label in [("automated", "filed under Automated"), ("already_known", "already imported"),
                       ("excluded", "excluded (own mail or no sender)"),
                       ("fetch_failed", "couldn’t be fetched"), ("filing_failed", "couldn’t be filed"),
                       ("unprocessed", "not processed")]:
        if c.get(key):
            details.append(f"{c[key]} message{'s' if c[key] != 1 else ''} {label}.")
    if state in {"complete", "partial", "failed", "interrupted"}:
        details.append("No replies were sent by this import.")
    if c.get("selected", 0) >= run.get("limit", LIMIT):
        details.append(f"Checked up to {run.get('limit', LIMIT)} recent Inbox messages.")
    return {"state": state, "message": message, "details": details, "ready": ready,
            "retry": state in {"not_started", "failed", "partial", "interrupted"},
            "connection_id": conn_id, "address": access.get("address", ""),
            "window_end": access.get("created_at", ""), "expected_run": run.get("run_id", ""),
            "choice": choice.get("decision")}


def count_rows(tenant_id: str, client=None, *, mailbox_id=None, strict=False) -> int:
    """How many "before BLAM8" rows the inbox still shows - the connected
    card's line, Today's nudge, and the retry's reason to exist. A sender the
    owner has since ignored no longer counts: ignoring senders is exactly what
    the nudge asks for, so its number has to fall as they do it (rig, 2026-09-20:
    four senders ignored a week earlier and Today still said 66). 0 on any
    failure."""
    try:
        cl = client or db.db()
        query = (cl.table("email_log").select("from_address,category")
                 .eq("tenant_id", tenant_id).like("category_reason", "Before BLAM8%"))
        if mailbox_id:
            query = query.eq("mailbox_id", mailbox_id)
        # Paginate so a long-lived mailbox's count is not truncated at 200 rows.
        rows = []
        offset = 0
        while True:
            batch = query.order("id").range(offset, offset + 199).execute().data or []
            rows.extend(batch)
            if len(batch) < 200:
                break
            offset += 200
        # First-walk finding, 1 Oct 2026: the provider's own security alerts from
        # the connect were filed automated and counted as mail to skim.
        rows = [r for r in rows if r.get("category") != "automated"]
        if not rows:
            return 0
        ignored = {a.lower() for a in db.ignored_addresses(tenant_id, client=cl)}
        return sum(1 for r in rows if (r.get("from_address") or "").lower() not in ignored)
    except Exception:
        if strict:
            raise
        return 0
