"""Server-owned founder outreach over Voice CRM. No local files or tenant cadence.

One bounded tick: sync -> prepare drafts -> reserve at most one send -> digest.
The database arbitrates workers; ambiguous sends require operator reconciliation.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app import db

DEFAULTS = {
    "name": "Admin outreach", "kind": "partner", "mode": "draft",
    "timezone": "Europe/London", "working_days": [0, 1, 2, 3, 4], "holidays": [],
    "start_time": "09:00", "end_time": "17:00", "min_gap_seconds": 150,
    "daily_cap": 100, "plan": {}, "sender": "", "reply_to": [],
    "unsubscribe_email": "", "stop_line": 'To stop these emails, reply "stop".',
    "inbox_tenant_id": "", "legacy_subjects": [], "fallback_line": "",
    # Same ordered step vocabulary as crm_sequences; at most one follow-up.
    "steps": [
        {"step_order": 0, "action_type": "send_email", "delay_days": 0, "exit_on_reply": True,
         "template_subject": "", "template_body": ""},
        {"step_order": 1, "action_type": "send_email", "delay_days": 7, "exit_on_reply": True,
         "template_subject": "", "template_body": ""},
    ],
}
STOP_RX = re.compile(r"\b(stop|unsubscribe|remove me|remove us|no more emails|do not contact|don't contact|not interested)\b", re.I)
AUTO_RX = re.compile(r"\b(auto.?reply|automatic reply|out of (the )?office|on annual leave)\b", re.I)


def utcnow():
    return datetime.now(timezone.utc)


def address(value):
    return parseaddr(str(value or ""))[1].strip().lower()


def valid_email(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value))


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Unknown outreach settings.")
    cfg = json.loads(json.dumps(DEFAULTS)) | raw
    for key in ("name", "timezone", "start_time", "end_time", "sender", "unsubscribe_email", "stop_line", "inbox_tenant_id", "fallback_line"):
        if not isinstance(cfg[key], str):
            raise ValueError(f"{key} must be text.")
    for key in ("working_days", "holidays", "reply_to", "legacy_subjects"):
        if not isinstance(cfg[key], list):
            raise ValueError(f"{key} must be a list.")
    if any(not isinstance(s, str) or not s.strip() for s in cfg["reply_to"] + cfg["legacy_subjects"]):
        raise ValueError("Reply-To and previous subjects cannot contain blank entries.")
    if cfg["mode"] not in ("draft", "auto") or cfg["kind"] not in ("partner", "customer"):
        raise ValueError("Choose draft or auto, and partner or customer.")
    try:
        ZoneInfo(cfg["timezone"])
        for key in ("start_time", "end_time"):
            if not re.fullmatch(r"\d{2}:\d{2}", cfg[key]):
                raise ValueError()
            datetime.strptime(cfg[key], "%H:%M")
        if cfg["start_time"] >= cfg["end_time"]:
            raise ValueError()
        if not cfg["working_days"] or any(type(d) is not int or d not in range(7) for d in cfg["working_days"]):
            raise ValueError()
        for day in cfg["holidays"]:
            date.fromisoformat(day)
        for key, low, high in (("daily_cap", 1, 10000), ("min_gap_seconds", 60, 86400)):
            if type(cfg[key]) is not int or not low <= cfg[key] <= high:
                raise ValueError()
        if not isinstance(cfg["plan"], dict):
            raise ValueError()
        for day, count in cfg["plan"].items():
            date.fromisoformat(day)
            if type(count) is not int or not 0 <= count <= 10000:
                raise ValueError()
        if cfg["inbox_tenant_id"]:
            uuid.UUID(cfg["inbox_tenant_id"])
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Invalid timezone, window, days, plan, cap, gap or inbox tenant ID.") from exc
    if not isinstance(cfg["reply_to"], list) or not isinstance(cfg["legacy_subjects"], list):
        raise ValueError("Reply-To and legacy subjects must be lists.")
    for value in [cfg["sender"], cfg["unsubscribe_email"], *cfg["reply_to"]]:
        if value and ("\r" in value or "\n" in value or not valid_email(address(value))):
            raise ValueError("Invalid sender, Reply-To or unsubscribe address.")
    if not cfg["stop_line"].strip() or len(cfg["stop_line"]) > 500:
        raise ValueError("A stop line is required (maximum 500 characters).")
    if not isinstance(cfg["steps"], list) or len(cfg["steps"]) != 2:
        raise ValueError("Provide a first email and exactly one follow-up step.")
    for i, step in enumerate(cfg["steps"]):
        if not isinstance(step, dict):
            raise ValueError("Invalid sequence step.")
        if step.get("exit_on_reply", True) is not True:
            raise ValueError("Stop-on-reply cannot be disabled.")
        if (step.get("step_order") != i or step.get("action_type") != "send_email"
                or type(step.get("delay_days")) is not int or not 0 <= step["delay_days"] <= 365
                or (i == 0 and step["delay_days"] != 0)
                or (i == 1 and step["delay_days"] < 1)):
            raise ValueError("Invalid sequence step or follow-up delay.")
        if any(not isinstance(step.get(k), str) for k in ("template_subject", "template_body")):
            raise ValueError("Each step needs a subject and body.")
        if len(step["template_subject"]) > 200 or len(step["template_body"]) > 20000 or re.search(r"[\r\n]", step["template_subject"]):
            raise ValueError("Subject or body is too long, or subject contains a newline.")
    if len(json.dumps(cfg)) > 60000:
        raise ValueError("Outreach settings are too large.")
    return cfg


def readiness(cfg):
    missing = [k for k in ("sender", "reply_to", "unsubscribe_email", "inbox_tenant_id", "fallback_line") if not cfg[k]]
    if any(not s["template_subject"].strip() or not s["template_body"].strip() for s in cfg["steps"]):
        missing.append("both email subjects and bodies")
    if not os.getenv("RESEND_API_KEY") or not os.getenv("RESEND_FULL_KEY"):
        missing.append("server RESEND_API_KEY and RESEND_FULL_KEY")
    return missing


def rows(table, configure=lambda q: q, order="id"):
    """Do not silently truncate CRM or inbox history at PostgREST's row limit."""
    result = []
    for offset in range(0, 1000000, 500):
        page = configure(db.db().table(table).select("*")).order(order).range(offset, offset + 499).execute().data or []
        result.extend(page)
        if len(page) < 500:
            return result
    raise RuntimeError("Outreach data exceeds the scan limit; sending blocked.")


def state():
    data = db.db().table("admin_outreach_config").select("*").eq("id", 1).execute().data
    if not data:
        raise RuntimeError("Apply the admin outreach migration first.")
    return data[0]


def update_state(fields, token=None):
    query = db.db().table("admin_outreach_config").update(fields).eq("id", 1)
    if token:
        query = query.eq("lease_token", token)
    query.execute()


def in_window(cfg, now):
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    return (local.weekday() in cfg["working_days"] and local.date().isoformat() not in cfg["holidays"]
            and cfg["start_time"] <= local.strftime("%H:%M") < cfg["end_time"])


def target(cfg, now):
    today = now.astimezone(ZoneInfo(cfg["timezone"])).date().isoformat()
    return sum(n for d, n in cfg["plan"].items() if d <= today)


def in_plan(message, cfg):
    if not cfg["plan"] or message["step"] != 0:
        return False
    value = message.get("sent_at") or message.get("attempted_at") or message.get("created_at")
    return bool(value) and datetime.fromisoformat(value).astimezone(ZoneInfo(cfg["timezone"])).date().isoformat() >= min(cfg["plan"])


def tailored_line(prospect, cfg):
    """Extractive tailoring: quote a record fact; never generate an unsupported claim."""
    description = re.sub(r"\s+", " ", prospect.get("description") or "").strip()
    if 8 <= len(description) <= 200 and not re.search(r"[<>]", description):
        return f'You describe your work as “{description}”. {cfg["fallback_line"]}'
    return cfg["fallback_line"]


def compose(prospect, cfg, step):
    from app.crm_sequences import render_template
    ctx = {"contact": {"name": prospect.get("contact_name") or "there", "email": prospect.get("email") or ""},
           "company": {"name": prospect.get("business_name") or ""}, "tailored_line": tailored_line(prospect, cfg)}
    s = cfg["steps"][step]
    subject = render_template(s["template_subject"], ctx)
    body = render_template(s["template_body"], ctx)
    if "\n" in subject or "\r" in subject:
        raise ValueError("Rendered subject contains a newline.")
    return {"from": cfg["sender"], "to": [address(prospect["email"])], "reply_to": cfg["reply_to"],
            "subject": subject, "text": body + "\n\n" + cfg["stop_line"],
            "headers": {"List-Unsubscribe": f'<mailto:{address(cfg["unsubscribe_email"])}?subject=stop>'}}


def eligible(prospect, suppressed):
    return (valid_email(address(prospect.get("email"))) and address(prospect["email"]) not in suppressed
            and prospect.get("email_verified") is True and not prospect.get("soft_match", True)
            and bool(prospect.get("company_number")) and bool(prospect.get("email_source"))
            and not re.search(r"STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact", prospect.get("next_action") or "", re.I))


class Provider:
    def __init__(self):
        self.read_key = os.getenv("RESEND_FULL_KEY", "")
        self.send_key = os.getenv("RESEND_API_KEY", "")
        self.last_request = 0.0

    def get(self, path, **params):
        # Account-wide API rate limits: leave space between paginated reads.
        time.sleep(max(0, 0.6 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        r = httpx.get("https://api.resend.com" + path, params=params,
                      headers={"Authorization": f"Bearer {self.read_key}"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def list(self, path):
        result, after = [], None
        for _ in range(100):
            page = self.get(path, limit=100, **({"after": after} if after else {}))
            data = page.get("data") or []
            result.extend(data)
            if not page.get("has_more"):
                return result
            if not data or data[-1]["id"] == after:
                break
            after = data[-1]["id"]
        raise RuntimeError("Provider pagination incomplete; sending blocked.")

    def send(self, message):
        time.sleep(max(0, 0.6 - (time.monotonic() - self.last_request)))
        r = httpx.post("https://api.resend.com/emails", json=message["payload"], timeout=25,
                       headers={"Authorization": f"Bearer {self.send_key}", "Idempotency-Key": f'admin-outreach/{message["id"]}'})
        r.raise_for_status()
        return r.json()["id"]


def suppress(email, reason, cfg):
    email = address(email)
    if not valid_email(email):
        raise ValueError("Enter a valid email address.")
    db.db().table("admin_outreach_suppressions").upsert({"email": email, "reason": reason}).execute()
    # Match exact mailboxes, never guess which firm owns a domain.
    for p in rows("vcrm_prospects"):
        if address(p.get("email")) == email:
            db.vcrm_update_prospect(p["id"], {"status": "NOT_NOW", "next_action": reason, "next_action_date": None})
    db.db().table("admin_outreach_messages").update({"status": "cancelled", "error": reason}).eq("email", email).in_("status", ["draft", "approved"]).execute()
    if cfg.get("inbox_tenant_id"):
        db.db().table("email_log").update({"status": "skipped"}).eq("tenant_id", cfg["inbox_tenant_id"]).eq("from_address", email).in_("status", ["pending", "draft_ready"]).execute()
        db.db().table("ignored_contacts").upsert({"tenant_id": cfg["inbox_tenant_id"], "address": email, "reason": reason}, on_conflict="tenant_id,address").execute()


def fresh_text(body):
    return re.split(r"(?im)^\s*(?:>|On .{5,200}wrote:|-----Original|From:)", body or "")[0][:4000].strip()


def sync(cfg, provider, now):
    mailboxes = db.db().table("email_connections").select("id,sync_failing_since").eq("tenant_id", cfg["inbox_tenant_id"]).execute().data or []
    if not mailboxes or any(m.get("sync_failing_since") for m in mailboxes):
        raise RuntimeError("Connected reply inbox is missing or unhealthy")
    prospects = rows("vcrm_prospects")
    suppressed = {s["email"] for s in rows("admin_outreach_suppressions", order="email")}
    for p in prospects:
        email = address(p.get("email"))
        if email and email not in suppressed and (p["status"] == "NOT_NOW" or re.search(r"STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact", p.get("next_action") or "", re.I)):
            suppress(email, p.get("next_action") or "Never contact again", cfg)
            suppressed.add(email)
    known = {address(p.get("email")) for p in prospects if p.get("email")}
    seen = {e["id"] for e in rows("admin_outreach_events")}
    sent = provider.list("/emails")
    incoming = rows("email_log", lambda q: q.eq("tenant_id", cfg["inbox_tenant_id"]).gte("created_at", (now - timedelta(days=45)).isoformat()))
    messages = [("inbox:" + m["id"], address(m.get("from_address")), m.get("subject") or "", m.get("body_in") or "")
                for m in incoming if m.get("kind") in (None, "inbound")]
    for m in provider.list("/emails/receiving"):
        key, email = "resend:" + m["id"], address(m.get("from"))
        if key not in seen and email in known:
            detail = provider.get("/emails/receiving/" + m["id"])
            body = detail.get("text") or re.sub(r"<[^>]+>", " ", detail.get("html") or "")
            messages.append((key, email, m.get("subject") or "", body))
    for m in sent:
        if m.get("last_event") in ("bounced", "complained", "suppressed"):
            for recipient in m.get("to") or []:
                email = address(recipient)
                key = f'provider:{m["id"]}:{m["last_event"]}:{email}'
                if email in known and key not in seen:
                    suppress(email, m["last_event"], cfg)
                    db.db().table("admin_outreach_events").upsert({"id": key, "email": email, "kind": m["last_event"]}).execute()
    subjects = [s["template_subject"].lower() for s in cfg["steps"]] + [s.lower() for s in cfg["legacy_subjects"]]
    subjects += [m["payload"].get("subject", "").lower() for m in rows("admin_outreach_messages")]
    for key, email, subject, body in messages:
        if key in seen or email not in known:
            continue
        fresh = fresh_text(body)
        # Stop has precedence over auto-replies and existing REPLIED/WON statuses.
        if STOP_RX.search(fresh):
            kind = "stopped"
            suppress(email, "STOP REQUEST: never contact again", cfg)
        elif not any(s and s in subject.lower() for s in subjects) and not any(
                address(p.get("email")) == email and p["status"] in ("EMAILED", "REPLIED", "CALL_BOOKED", "SIGNED_UP")
                for p in prospects):
            continue
        elif AUTO_RX.search(subject + " " + fresh[:200]):
            kind = "auto"
            for p in prospects:
                if address(p.get("email")) == email and p["status"] == "EMAILED" and p.get("next_action_date"):
                    due = (now.astimezone(ZoneInfo(cfg["timezone"])).date() + timedelta(days=7)).isoformat()
                    db.vcrm_update_prospect(p["id"], {"next_action_date": max(due, p["next_action_date"])})
        else:
            kind = "replied"
            for p in prospects:
                if address(p.get("email")) == email and p["status"] in ("IDENTIFIED", "EMAILED"):
                    db.vcrm_update_prospect(p["id"], {"status": "REPLIED", "next_action": "Read and answer the reply in the normal inbox", "next_action_date": None})
            db.db().table("admin_outreach_messages").update({"status": "cancelled", "error": "Recipient replied"}).eq("email", email).in_("status", ["draft", "approved"]).execute()
        db.db().table("admin_outreach_events").upsert({"id": key, "email": email, "kind": kind}).execute()
    return sent


def prepare(cfg, now):
    suppressed = {r["email"] for r in rows("admin_outreach_suppressions", order="email")}
    existing = {(m["email"], m["step"]): m for m in rows("admin_outreach_messages")}
    # Replies survive duplicate/imported prospect rows too.
    replied = {e["email"] for e in rows("admin_outreach_events") if e["kind"] == "replied"}
    today = now.astimezone(ZoneInfo(cfg["timezone"])).date().isoformat()
    remaining = max(0, target(cfg, now) - sum(in_plan(m, cfg) and m["status"] in ("sent", "sending", "unknown", "draft", "approved") for m in existing.values()))
    prospects = rows("vcrm_prospects")
    replied |= {address(p.get("email")) for p in prospects if p["status"] in ("REPLIED", "CALL_BOOKED", "SIGNED_UP", "NOT_NOW")}
    count = 0
    for p in prospects:
        if p.get("kind") != cfg["kind"]:
            continue
        email = address(p.get("email"))
        if not eligible(p, suppressed | replied):
            continue
        step = 0 if p["status"] == "IDENTIFIED" else 1
        if step == 1 and (p["status"] != "EMAILED" or not p.get("next_action_date") or str(p["next_action_date"]) > today):
            continue
        if step == 1 and (email, 0) not in existing:
            continue  # historical first touch must be reconciled, not guessed
        if (email, step) in existing:
            old = existing[(email, step)]
            if old["status"] in ("draft", "approved") and old.get("config_snapshot") != cfg:
                db.db().table("admin_outreach_messages").update({"payload": compose(p, cfg, step), "config_snapshot": cfg,
                    "status": "draft"}).eq("id", old["id"]).in_("status", ["draft", "approved"]).execute()
            continue
        if step == 0 and remaining <= 0:
            continue
        message = {"prospect_id": p["id"], "email": email, "step": step, "status": "draft",
                   "payload": compose(p, cfg, step), "config_snapshot": cfg}
        db.db().table("admin_outreach_messages").upsert(message, on_conflict="email,step", ignore_duplicates=True).execute()
        existing[(email, step)] = message
        remaining -= step == 0
        count += 1
        if count >= 100:
            break
    return count


def digest(cfg, now, result):
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    day = local.date().isoformat()
    messages = rows("admin_outreach_messages")
    events = rows("admin_outreach_events")
    def local_day(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo(cfg["timezone"])).date().isoformat() if value else ""
    def save(on_day, closed, outcome):
        delivered = [m for m in messages if m["status"] == "sent" and local_day(m.get("sent_at")) <= on_day]
        shortfall = max(0, sum(n for d, n in cfg["plan"].items() if d <= on_day) - sum(in_plan(m, cfg) for m in delivered))
        counts = {"first_sent": sum(m["step"] == 0 and local_day(m.get("sent_at")) == on_day for m in delivered),
                  "followups_sent": sum(m["step"] == 1 and local_day(m.get("sent_at")) == on_day for m in delivered),
                  "shortfall": shortfall, "failed_day": shortfall > 0 and closed, "closed": closed,
                  "unknown": sum(m["status"] in ("sending", "unknown") for m in messages), "result": outcome}
        for kind in ("replied", "stopped", "bounced", "complained"):
            counts[kind] = len({e["email"] for e in events if e["kind"] == kind and local_day(e["created_at"]) == on_day})
        db.db().table("admin_outreach_digests").upsert({"day": on_day, "summary": counts, "updated_at": now.isoformat()}).execute()
        return counts
    previous = {d["day"]: d for d in rows("admin_outreach_digests", order="day")}
    for planned_day in sorted(cfg["plan"]):
        if planned_day < day and not previous.get(planned_day, {}).get("summary", {}).get("closed"):
            save(planned_day, True, {"note": "Day closed from durable history after restart"})
    return save(day, local.strftime("%H:%M") >= cfg["end_time"], result)


def run(*, preview=False):
    token = str(uuid.uuid4())
    if not db.db().rpc("admin_outreach_lock", {"p_token": token}).execute().data:
        return {"blocked": "Another outreach job is running"}
    result = {}
    cfg = None
    try:
        current = state()
        cfg = validate_config(current["config"])
        if readiness(cfg):
            result = {"blocked": "Configure " + ", ".join(readiness(cfg))}
            return result
        provider = Provider()
        observed = sync(cfg, provider, utcnow())  # any failure aborts before preparing/sending
        update_state({"last_sync_at": utcnow().isoformat()}, token)
        result = {"drafts_prepared": prepare(cfg, utcnow())}
        if preview or not current["enabled"]:
            result["blocked"] = "Preview only" if preview else "Paused"
            return result
        if not in_window(cfg, utcnow()):
            result["blocked"] = "Outside working window"
            return result
        if current.get("last_attempt_at") and (utcnow() - datetime.fromisoformat(current["last_attempt_at"])).total_seconds() < cfg["min_gap_seconds"]:
            result["blocked"] = "Minimum gap between emails"
            return result
        messages = rows("admin_outreach_messages")
        if any(m["status"] in ("sending", "unknown") for m in messages):
            result["blocked"] = "Reconcile an uncertain send before resuming"
            return result
        candidates = [m for m in messages if m["status"] == "approved" or (cfg["mode"] == "auto" and m["status"] == "draft")]
        candidates.sort(key=lambda m: (-m["step"], m["created_at"]))
        cutoff = utcnow() - timedelta(hours=24)
        recent = [m for m in observed if datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")) > cutoff]
        if len(recent) >= cfg["daily_cap"]:
            result["blocked"] = "Provider cap reached; shortfall carries forward"
            return result
        for message in candidates:
            claimed = db.db().rpc("admin_outreach_claim", {"p_id": message["id"], "p_token": token, "p_config": cfg,
                                 "p_observed": len(recent), "p_provider_ids": [m["id"] for m in recent]}).execute().data
            if not claimed:
                continue
            provider_id = None
            try:
                provider_id = provider.send(message)
                due = utcnow().astimezone(ZoneInfo(cfg["timezone"])).date() + timedelta(days=cfg["steps"][1]["delay_days"])
                while due.weekday() not in cfg["working_days"] or due.isoformat() in cfg["holidays"]:
                    due += timedelta(days=1)
                db.db().rpc("admin_outreach_complete", {"p_id": message["id"], "p_provider_id": provider_id, "p_due": due.isoformat()}).execute()
                result["sent"] = message["id"]
            except Exception:
                # No blind retry: the remote side may have accepted the request.
                db.db().table("admin_outreach_messages").update({"status": "unknown", "provider_id": provider_id,
                    "error": "Provider outcome uncertain; reconcile before resuming"}).eq("id", message["id"]).execute()
                raise
            break
        if "sent" not in result:
            result["blocked"] = "Awaiting approval, capacity, minimum gap, or current eligible drafts"
        return result
    except Exception as exc:
        # Never put provider payloads, credentials or inbox content into errors.
        result = {"blocked": f"Job failed ({type(exc).__name__}); no further sends attempted"}
        return result
    finally:
        try:
            if cfg is not None:
                digest(cfg, utcnow(), result)
            update_state({"last_result": result}, token)
        finally:
            update_state({"lease_token": None, "lease_until": None}, token)


def reconcile(message_id, provider_id=""):
    """Operator review after a timeout: verify acceptance, or permanently cancel."""
    current = state()
    if current["enabled"] or (current.get("lease_until") and datetime.fromisoformat(current["lease_until"]) > utcnow()):
        raise ValueError("Pause sending and wait for the current job to finish first.")
    found = db.db().table("admin_outreach_messages").select("*").eq("id", message_id).execute().data or []
    if not found or found[0]["status"] not in ("sending", "unknown"):
        raise ValueError("This message does not need reconciliation.")
    message = found[0]
    if not provider_id:
        db.db().table("admin_outreach_messages").update({"status": "cancelled", "error": "Operator cancelled after uncertain outcome; never retry"}).eq("id", message_id).in_("status", ["sending", "unknown"]).execute()
        return
    try:
        uuid.UUID(provider_id)
    except ValueError as exc:
        raise ValueError("Invalid provider email ID.") from exc
    remote = Provider().get("/emails/" + provider_id)
    payload = message["payload"]
    accepted = datetime.fromisoformat(remote["created_at"].replace("Z", "+00:00"))
    attempted = datetime.fromisoformat(message["attempted_at"])
    if (remote.get("to") != payload["to"] or remote.get("subject") != payload["subject"]
            or address(remote.get("from")) != address(payload["from"])
            or abs((accepted - attempted).total_seconds()) > 600):
        raise ValueError("Provider email does not match this send attempt.")
    cfg = validate_config(message["config_snapshot"])
    due = accepted.astimezone(ZoneInfo(cfg["timezone"])).date() + timedelta(days=cfg["steps"][1]["delay_days"])
    db.db().rpc("admin_outreach_complete", {"p_id": message_id, "p_provider_id": provider_id, "p_due": due.isoformat()}).execute()


def import_csv(text, kind="partner"):
    if kind not in ("partner", "customer"):
        raise ValueError("Invalid prospect list.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"business_name", "email", "company_number", "email_source", "email_verified", "soft_match"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("CSV requires " + ", ".join(sorted(required)))
    records = list(reader)
    if len(records) > 2000:
        raise ValueError("Import at most 2,000 prospects at once.")
    fields = []
    for n, row in enumerate(records, 2):
        if any(row.get(k) is None for k in required) or None in row:
            raise ValueError(f"CSV row {n}: wrong number of columns.")
        if not row["business_name"].strip() or not valid_email(address(row["email"])):
            raise ValueError(f"CSV row {n}: business name and email required.")
        if any(row[k].strip().lower() not in ("true", "false") for k in ("email_verified", "soft_match")):
            raise ValueError(f"CSV row {n}: verification flags must be true or false.")
        fields.append({"business_name": row["business_name"].strip(), "email": address(row["email"]),
                       "company_number": row["company_number"].strip(), "email_source": row["email_source"].strip(),
                       "email_verified": row["email_verified"].strip().lower() == "true", "soft_match": row["soft_match"].strip().lower() == "true",
                       "description": row.get("description", ""), "contact_name": row.get("contact_name", ""),
                       "source": row.get("source", ""), "kind": kind, "channel": "email", "status": "IDENTIFIED"})
    existing = {address(p.get("email")) for p in rows("vcrm_prospects")}
    count = 0
    for row in fields:
        if row["email"] in existing:
            continue  # no import can reset a replied/stopped prospect
        db.vcrm_create_prospect(row)
        existing.add(row["email"])
        count += 1
    return count
