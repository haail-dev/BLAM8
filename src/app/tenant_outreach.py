"""Tenant list policy around crm_sequences; no second cadence runner.

Drafts share the normal inbox. Every approval (including voice) and automatic
send comes back through the same database reservation and compliance checks.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app import agent, db, disclosure, events, mailbox, usage

log = logging.getLogger(__name__)

STOP_LINE = 'To stop these emails, reply "stop". We will not contact you again.'
DEFAULTS = {"mode": "draft", "timezone": "Europe/London", "working_days": [0, 1, 2, 3, 4],
            "holidays": [], "start_time": "09:00", "end_time": "17:00",
            "daily_cap": 50, "min_gap_seconds": 150}
STOP = re.compile(r"\b(stop|unsubscribe|remove me|remove us|do not contact|don't contact|no more emails|not interested)\b", re.I)


def now():
    return datetime.now(timezone.utc)


def email(value):
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise ValueError("Use one valid email address.")
    return value


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Unknown campaign settings.")
    cfg = json.loads(json.dumps(DEFAULTS)) | raw
    try:
        ZoneInfo(cfg["timezone"])
        if cfg["mode"] not in ("draft", "auto"):
            raise ValueError()
        for key in ("start_time", "end_time"):
            if not re.fullmatch(r"\d{2}:\d{2}", cfg[key]):
                raise ValueError()
            datetime.strptime(cfg[key], "%H:%M")
        if cfg["start_time"] >= cfg["end_time"]:
            raise ValueError()
        if (not isinstance(cfg["working_days"], list) or not cfg["working_days"]
                or any(type(d) is not int or d not in range(7) for d in cfg["working_days"])):
            raise ValueError()
        if not isinstance(cfg["holidays"], list):
            raise ValueError()
        for day in cfg["holidays"]:
            datetime.strptime(day, "%Y-%m-%d")
        for key, low, high in (("daily_cap", 1, 1000), ("min_gap_seconds", 60, 86400)):
            if type(cfg[key]) is not int or not low <= cfg[key] <= high:
                raise ValueError()
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Check the mode, timezone, days, working window, cap and minimum gap.") from exc
    return cfg


def in_window(cfg, at=None):
    local = (at or now()).astimezone(ZoneInfo(cfg["timezone"]))
    return (local.weekday() in cfg["working_days"] and local.date().isoformat() not in cfg["holidays"]
            and cfg["start_time"] <= local.strftime("%H:%M") < cfg["end_time"])


def table(name, tenant_id, client=None):
    return (client or db.db()).table(name).select("*").eq("tenant_id", tenant_id)


def rows(name, tenant_id, client=None, *, order="id", **filters):
    out = []
    for offset in range(0, 100000, 500):
        q = table(name, tenant_id, client)
        for key, value in filters.items():
            q = q.eq(key, value)
        page = q.order(order).range(offset, offset+499).execute().data or []
        out.extend(page)
        if len(page) < 500:
            return out
    raise RuntimeError("Outreach scan incomplete; sending held")


def get(name, tenant_id, id, client=None):
    found = table(name, tenant_id, client).eq("id", id).limit(1).execute().data or []
    if not found:
        raise ValueError("Record not found in this business.")
    return found[0]


def update(name, tenant_id, id, fields):
    return db.db().table(name).update(fields).eq("tenant_id", tenant_id).eq("id", id).execute().data


def settings(tenant_id, client=None):
    found = table("outreach_settings", tenant_id, client).limit(1).execute().data or []
    return found[0] if found else {"enabled": False, "daily_cap": 100}


def identities(tenant_id, client=None):
    # Never load OAuth tokens into a page context.
    return ((client or db.db()).table("email_connections")
            .select("id,address,send_as_address,provider,sync_failing_since")
            .eq("tenant_id", tenant_id).order("created_at").execute().data or [])


def identity(tenant_id, id):
    found = next((r for r in identities(tenant_id) if r["id"] == id), None)
    if not found or found.get("sync_failing_since"):
        raise ValueError("Connect a healthy sending mailbox first.")
    found["sender"] = email(found.get("send_as_address") or found.get("address"))
    found["reply"] = email(found.get("address"))
    return found


def validate_target(row):
    result = {k: str(row.get(k) or "").strip() for k in
              ("name", "email", "basis", "evidence", "email_source", "company_number")}
    result["email"] = email(result["email"])
    if result["basis"] not in ("permission", "corporate"):
        raise ValueError("Record permission to contact, or a verified corporate recipient.")
    if not result["email_source"] or not result["evidence"]:
        raise ValueError("Record the email source and permission or company evidence.")
    if result["basis"] == "corporate" and not re.fullmatch(r"[A-Za-z0-9 -]{4,30}", result["company_number"]):
        raise ValueError("Corporate recipients need their company registration number.")
    for key in ("email_verified", "soft_match"):
        val = str(row.get(key, "false" if key == "email_verified" else "true")).lower()
        if val not in ("true", "false"):
            raise ValueError("Verification fields must be true or false.")
        result[key] = val == "true"
    if any(len(result[k]) > 2000 for k in ("name", "email_source", "evidence")):
        raise ValueError("Recipient evidence is too long.")
    return result


def import_targets(tenant_id, list_id, records):
    get("outreach_lists", tenant_id, list_id)
    if not records or len(records) > 500:
        raise ValueError("Import between 1 and 500 contacts at once.")
    records = [validate_target(r) for r in records]  # validate WHOLE input before writes
    existing = {r["email"] for r in rows("outreach_targets", tenant_id, list_id=list_id)}
    added = 0
    for row in records:
        if row["email"] in existing:
            continue  # reimport never resets a suppression or existing evidence
        contact = db.crm_get_or_create_contact(tenant_id, row["email"], name=row.pop("name") or None)
        db.db().table("outreach_targets").upsert({**row, "tenant_id": tenant_id,
            "list_id": list_id, "contact_id": contact["id"]}, on_conflict="tenant_id,list_id,email", ignore_duplicates=True).execute()
        existing.add(row["email"])
        added += 1
    return added


def import_csv(tenant_id, list_id, text):
    if len(text) > 1_000_000:
        raise ValueError("CSV must be smaller than 1 MB.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"email", "basis", "evidence", "email_source", "email_verified", "soft_match"}
    if not required <= set(reader.fieldnames or []):
        raise ValueError("CSV needs: " + ", ".join(sorted(required)))
    records = list(reader)
    if any(None in r or any(v is None for v in r.values()) for r in records):
        raise ValueError("CSV contains a row with the wrong number of columns.")
    return import_targets(tenant_id, list_id, records)


def suppress(tenant_id, address, reason):
    address = email(address)
    db.db().table("outreach_suppressions").upsert({"tenant_id": tenant_id, "email": address,
        "reason": reason}, on_conflict="tenant_id,email").execute()
    for c in rows("crm_contacts", tenant_id):
        if (c.get("email") or "").strip().lower() == address:
            db.crm_update_contact(c["id"], tenant_id, {"do_not_sequence": True})
    for m in rows("outreach_messages", tenant_id, email=address):
        if m["status"] in ("preparing", "draft"):
            update("outreach_messages", tenant_id, m["id"], {"status": "cancelled", "error": reason})
            if m.get("email_log_id"):
                db.set_email_log_status(m["email_log_id"], "skipped", tenant_id)


def sync(tenant_id):
    """Read the normal inbox, including skipped automated delivery reports.

    No domain guessing: stops match exact senders; DSNs require an explicit
    Final-Recipient field from a delivery-report sender. Unknown outcomes hold.
    """
    known = {r["email"] for r in rows("outreach_targets", tenant_id)}
    blocked = {r["email"] for r in rows("outreach_suppressions", tenant_id, order="email")}
    for m in rows("email_log", tenant_id, kind="inbound"):
        sender = parseaddr(m.get("from_address") or "")[1].lower()
        body = m.get("body_in") or ""
        fresh = re.split(r"(?im)^\s*(?:>|On .{5,200}wrote:|-----Original|From:)", body)[0]
        if sender in known and sender not in blocked and STOP.search(fresh[:4000]):
            suppress(tenant_id, sender, "Stop requested")
            blocked.add(sender)
        if re.match(r"^(mailer-daemon|postmaster)@", sender) and re.search(r"(?im)^(?:Action:\s*failed|Status:\s*5\.)", body):
            for recipient in re.findall(r"(?im)^Final-Recipient:\s*rfc822;\s*([^\s<>]+)", body):
                recipient = recipient.strip().lower()
                if recipient in known and recipient not in blocked:
                    suppress(tenant_id, recipient, "Delivery report: bounced")
                    blocked.add(recipient)
        if re.search(r"(?im)^Feedback-Type:\s*abuse\b", body):
            for recipient in re.findall(r"(?im)^Original-Rcpt-To:\s*(?:rfc822;\s*)?<?([^\s<>]+)>?", body):
                recipient = recipient.strip().lower()
                if recipient in known and recipient not in blocked:
                    suppress(tenant_id, recipient, "Provider complaint")
                    blocked.add(recipient)
    db.db().table("outreach_settings").update({"last_sync_at": now().isoformat()}).eq("tenant_id", tenant_id).execute()


def ready_target(tenant_id, target, contact):
    return (target["email_verified"] and not target["soft_match"] and target["evidence"]
            and target["email_source"] and not contact.get("do_not_sequence")
            and target["email"] == (contact.get("email") or "").strip().lower()
            and not table("outreach_suppressions", tenant_id).eq("email", target["email"]).execute().data
            and not db.is_ignored(tenant_id, target["email"]))


def create_campaign(tenant_id, name, list_id, sequence_id, mailbox_id, raw):
    config = validate_config(raw)
    get("outreach_lists", tenant_id, list_id)
    identity(tenant_id, mailbox_id)
    seq = db.crm_get_sequence(sequence_id, tenant_id)
    steps = db.crm_list_steps(sequence_id, tenant_id)
    if not seq or seq.get("outreach_managed") or seq.get("trigger_type") != "manual":
        raise ValueError("Choose an unused manual sequence.")
    if not 1 <= len(steps) <= 7 or any(s["action_type"] != "send_email" or not s.get("exit_on_reply")
            or not (s.get("template_subject") or "").strip() or not (s.get("template_body") or "").strip() for s in steps):
        raise ValueError("Use 1–7 email steps, each with a subject, brief and stop-on-reply.")
    if db.crm_list_enrollments(tenant_id, sequence_id=sequence_id):
        raise ValueError("This sequence already has recipients. Create a fresh sequence for this campaign.")
    if not name.strip() or len(name) > 100:
        raise ValueError("Give the campaign a name of up to 100 characters.")
    # Bind the sequence and campaign atomically against concurrent enrollment.
    created = db.db().rpc("outreach_create_campaign", {"p_tenant": tenant_id, "p_name": name.strip(),
        "p_list": list_id, "p_sequence": sequence_id, "p_box": mailbox_id, "p_config": config}).execute().data
    if not created:
        raise ValueError("This sequence changed or is already in use. Choose an unused manual sequence.")
    return created[0]


def enroll_list(tenant_id, campaign_id):
    c = get("outreach_campaigns", tenant_id, campaign_id)
    existing = {e["contact_id"] for e in db.crm_list_enrollments(tenant_id, sequence_id=c["sequence_id"])}
    added = 0
    for target in rows("outreach_targets", tenant_id, list_id=c["list_id"]):
        contact = db.crm_get_contact(target["contact_id"], tenant_id)
        if contact and contact["id"] not in existing and ready_target(tenant_id, target, contact):
            try:
                enrolled = db.crm_enroll(tenant_id, c["sequence_id"], contact["id"])
            except Exception as exc:
                if getattr(exc, "code", None) != "23505":
                    raise
                enrolled = None  # concurrent add won the unique enrollment
            if enrolled:
                existing.add(contact["id"])
                added += 1
    return added


def outcome(tenant_id, campaign_id, text):
    update("outreach_campaigns", tenant_id, campaign_id, {"last_result": text[:300]})


async def process_step(enr, seq, step, ctx, contact):
    """Called by crm_sequences. True means this step really sent, so advance it.
    A pending draft never advances the cadence or schedules the next message.
    """
    tid = enr["tenant_id"]
    campaigns = rows("outreach_campaigns", tid, sequence_id=seq["id"])
    if not campaigns:
        return False
    c = campaigns[0]
    held = lambda reason: outcome(tid, c["id"], reason)
    if not settings(tid)["enabled"] or not c["enabled"]:
        held("Paused")
        return False
    cfg = validate_config(c["config"])
    sync(tid)
    targets = rows("outreach_targets", tid, list_id=c["list_id"], contact_id=contact["id"])
    if not targets or not ready_target(tid, targets[0], contact):
        held("Recipient blocked or needs verification")
        return False
    if step["action_type"] != "send_email" or not step.get("exit_on_reply"):
        held("Outreach needs email steps with stop-on-reply")
        return False
    target = targets[0]
    box = identity(tid, c["mailbox_id"])
    token = str(uuid.uuid4())
    found = db.db().rpc("outreach_begin_step", {"p_tenant": tid, "p_campaign": c["id"],
        "p_enrollment": enr["id"], "p_target": target["id"], "p_step": int(enr.get("current_step") or 0),
        "p_snapshot": step, "p_config": cfg, "p_email": target["email"], "p_box": box["id"],
        "p_sender": box["sender"], "p_token": token}).execute().data
    m = found[0]
    if m["status"] == "sent":
        return True
    if m["status"] in ("sending", "unknown", "cancelled"):
        held("Send needs review: " + m["status"])
        return False
    effective = db.effective_config(tid, box["id"])
    if m["status"] == "preparing":
        if m["work_token"] != token:
            return False
        from app.crm_sequences import render_template
        subject = render_template(step.get("template_subject") or "", ctx).strip()
        brief = render_template(step.get("template_body") or "", ctx).strip()
        if not subject or len(subject) > 200 or re.search(r"[\r\n]", subject) or not brief:
            held("Sequence subject or brief is missing or invalid")
            return False
        result = await agent.compose_outbound(effective,
            "Write a short outreach email from this business using only the brief and its knowledge. "
            "Do not invent claims, prices, dates or a relationship with the recipient.", brief,
            tenant_id=tid, log_operation="sequence")
        body = result.text.strip()
        if not body:
            raise ValueError("Composer returned an empty draft")
        body += "\n\n" + STOP_LINE
        log_id = db.db().rpc("outreach_finish_draft", {"p_tenant": tid, "p_id": m["id"],
            "p_token": token, "p_subject": subject, "p_body": body}).execute().data
        if not log_id:
            return False
        usage.record_anthropic(tid, result.model, result.usage, operation="sequence", email_log_id=log_id)
        m = get("outreach_messages", tid, m["id"])
    if cfg["mode"] == "auto" and effective.get("auto_send"):
        entry = db.get_email_log_entry(m["email_log_id"], tid)
        try:
            await send_draft(tid, entry, entry["draft_reply"], auto=True)
            return True
        except HTTPException as exc:
            held(str(exc.detail))
            return False
    held("Awaiting draft approval")
    return False


async def send_draft(tenant_id, entry, body, *, auto=False):
    """The only provider boundary for tenant outreach, including inbox approval."""
    messages = rows("outreach_messages", tenant_id, email_log_id=entry["id"])
    if not messages:
        raise HTTPException(409, "This outreach draft has no send record.")
    m = messages[0]
    if not body.strip() or len(body) > 20000:
        raise HTTPException(400, "Write a message of 1–20,000 characters.")
    # Tenant edits can never remove the mandatory stop instruction.
    body = body.strip()
    if STOP_LINE not in body:
        body += "\n\n" + STOP_LINE
    try:
        sync(tenant_id)
        box = identity(tenant_id, m["mailbox_id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Outreach reply sync is unavailable. Nothing was sent.") from exc
    effective = db.effective_config(tenant_id, box["id"])
    if auto and not effective.get("auto_send"):
        raise HTTPException(409, "This mailbox requires draft approval.")
    claim = db.db().rpc("outreach_claim", {"p_tenant": tenant_id, "p_id": m["id"], "p_auto": auto}).execute().data
    if claim != "claimed":
        outcome(tenant_id, m["campaign_id"], claim)
        db.db().table("outreach_messages").update({"error": claim}).eq("tenant_id", tenant_id).eq("id", m["id"]).eq("status", "draft").execute()
        raise HTTPException(409, claim)
    try:
        # Keep the exact approved content for reconciliation before the provider
        # boundary. Only the worker holding the send reservation reaches here.
        db.update_email_log_draft(entry["id"], body, tenant_id)
        await mailbox.send_mail(tenant_id, m["email"], entry["subject"],
            disclosure.wrap(effective, body) if auto else body, mailbox_id=m["mailbox_id"],
            outreach_headers={"Reply-To": box["reply"],
                "List-Unsubscribe": f'<mailto:{box["reply"]}?subject=stop>'}, expected_sender=m["sender_address"])
        db.db().rpc("outreach_complete", {"p_tenant": tenant_id, "p_id": m["id"],
            "p_body": body, "p_auto": auto}).execute()
    except Exception as exc:
        # May have reached the provider. Do not repeat even on a timeout or restart.
        db.db().table("outreach_messages").update({"status": "unknown", "error": "Check the sent folder; outcome uncertain"})\
            .eq("tenant_id", tenant_id).eq("id", m["id"]).eq("status", "sending").execute()
        raise HTTPException(409, "Sending outcome is uncertain. Pause and reconcile it in Outreach.") from exc
    try:
        outcome(tenant_id, m["campaign_id"], "Sent")
        events.publish(tenant_id, {"type": "status_changed", "log_id": entry["id"],
                                   "contact": m["email"], "status": "sent"})
    except Exception:
        log.warning("Outreach send completed; notification failed", exc_info=True)
    return "sent"


def review_message(tenant_id, message_id, action):
    """Pause before refresh/reconciliation. A cancelled outcome is never retried."""
    m = get("outreach_messages", tenant_id, message_id)
    c = get("outreach_campaigns", tenant_id, m["campaign_id"])
    if settings(tenant_id)["enabled"] or c["enabled"]:
        raise ValueError("Switch outreach off and pause the campaign before reviewing a send.")
    if m["status"] == "sending" and (now()-datetime.fromisoformat(m["attempted_at"])).total_seconds() < 300:
        raise ValueError("Wait five minutes for the current send to finish.")
    if action == "cancel" and m["status"] != "sent":
        update("outreach_messages", tenant_id, m["id"], {"status": "cancelled", "error": "Cancelled by operator; never retry"})
        if m.get("email_log_id"):
            db.set_email_log_status(m["email_log_id"], "skipped", tenant_id)
        db.crm_exit_enrollment(m["enrollment_id"], tenant_id, reason="manual")
    elif action == "confirm_sent" and m["status"] in ("unknown", "sending"):
        update("outreach_messages", tenant_id, m["id"], {"status": "sending"})
        entry = db.get_email_log_entry(m["email_log_id"], tenant_id)
        db.db().rpc("outreach_complete", {"p_tenant": tenant_id, "p_id": m["id"],
            "p_body": entry["draft_reply"], "p_auto": False}).execute()
    elif action == "refresh" and m["status"] in ("draft", "preparing"):
        if m.get("lease_until") and datetime.fromisoformat(m["lease_until"]) > now():
            raise ValueError("Wait for draft preparation to finish.")
        update("outreach_messages", tenant_id, m["id"], {"status": "preparing", "lease_until": "2000-01-01T00:00:00Z", "work_token": None})
    else:
        raise ValueError("This action does not apply to this send.")
