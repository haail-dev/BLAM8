"""Multi-step follow-up sequences — the engine (docs/architecture/CRM_design.md §19.3).

Sequence → ordered steps → per-contact enrollments, processed on the 15-min
timer (src/app/jobs/process_sequences.py). This extends the single-touch gone-quiet
nudge into multi-step outreach; it's in Core.

A step's template_body is a BRIEF, not a literal email — send_email steps run it
through the outbound composer (app/agent.compose_outbound) so the message comes out
in the tenant's voice, exactly like the service-reminder job. There is no inbound to
reply to, so we use the composer, not draft_reply.

The sweep reads due enrollments across all tenants via the service role (db.db()),
then does per-tenant scoped work off each row's tenant_id. One failing enrollment must
never break the run — every enrollment is processed in its own try/except.

    python -m app.crm_sequences   # runs one pass
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import agent, db, disclosure, mailbox, usage

log = logging.getLogger(__name__)

MAX_STEPS = 7            # guardrail: keeps outreach human-length (§19.3)
MAX_ACTIVE_SEQUENCES = 5  # guardrail: max active sequences per tenant (Core tier)

TRIGGER_TYPES = ("manual", "deal_stage_enter", "gone_quiet", "new_contact", "tag_added")
ACTION_TYPES = ("send_email", "update_stage", "create_task", "add_tag")

# The brief handed to the composer for a send_email step — the rendered template
# body carries the actual intent, this frames it as proactive outreach.
_SEND_PURPOSE = ("A follow-up in an automated re-engagement sequence. Use the brief "
                 "below as the message's intent; keep it short, warm and human — a nudge, "
                 "not an essay. Do NOT invent dates, prices or references.")


def render_template(body: str, ctx: dict) -> str:
    """Substitute {{contact.name}} etc. in a step body. Missing keys → empty string.
    Dotted keys resolve against the nested ctx (contact/company/deal/tenant); flat keys
    (days_since_last_contact, promise_summary) resolve at the top level."""
    if not body:
        return ""
    import re

    def resolve(key: str) -> str:
        key = key.strip()
        node = ctx
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return ""
        return "" if node is None else str(node)

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: resolve(m.group(1)), body)


def _build_ctx(tenant: dict, cfg: dict, contact: dict, deal: dict | None,
               promise_summary: str = "") -> dict:
    """The template context for render_template — flattened CRM facts for one step."""
    from app import crm
    company = (contact or {}).get("crm_companies") or {}
    days_since = crm.days_since((contact or {}).get("last_activity_at"))
    return {
        "contact": {"name": (contact or {}).get("name") or "",
                    "email": (contact or {}).get("email") or ""},
        "company": {"name": company.get("name") or ""},
        "deal": {"title": (deal or {}).get("title") or "",
                 "stage": (((deal or {}).get("crm_stages") or {}).get("name")) or "",
                 "value": (deal or {}).get("value_estimate") or ""},
        "tenant": {"business_name": (tenant or {}).get("name")
                   or (cfg or {}).get("agent_name") or "",
                   "sender_name": (cfg or {}).get("agent_name")
                   or (cfg or {}).get("sign_off") or ""},
        "days_since_last_contact": "" if days_since is None else days_since,
        "promise_summary": promise_summary,
    }


async def _do_send_email(tenant_id: str, cfg: dict, to: str, step: dict, ctx: dict) -> None:
    """Compose + queue (and maybe auto-send) one sequence email. Mirrors
    app/jobs/send_service_reminders._remind_one — outbound composer, pending draft
    unless the tenant is full-auto."""
    subject = render_template(step.get("template_subject") or "", ctx).strip() or "Following up"
    brief = render_template(step.get("template_body") or "", ctx).strip()
    result = await agent.compose_outbound(cfg, _SEND_PURPOSE, brief,
                                          tenant_id=tenant_id, log_operation="sequence")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[sequence] {brief}", result.text,
        status="pending", kind="sequence", thread_id=None)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="sequence", email_log_id=log_id)
    db.log_agent_event(
        tenant_id, "sequence", f"Sequence nudge queued for {to}",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    if cfg.get("auto_send"):
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent", f"Sent the sequence nudge to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})


def _side_effect_stage(step: dict) -> str | None:
    """The stage_id an update_stage step moves the deal to (from side_effects or
    trigger-style config)."""
    for eff in step.get("side_effects") or []:
        if isinstance(eff, dict) and eff.get("stage_id"):
            return eff["stage_id"]
    return None


async def _process_one(enr: dict) -> str:
    """Execute the due step for one enrollment and advance/exit/complete it.
    Returns an outcome tag for the run summary. Raises on unexpected failure —
    the caller isolates it so one bad enrollment never breaks the sweep."""
    tenant_id = enr["tenant_id"]
    # CRM tier gate (docs/crm-vs-os-architecture.md §1, Julius 2026-07-24): a
    # tenant who leaves the CRM tier (downgrade to £39, lapse) must stop paying —
    # and stop SENDING — sequence steps. Exit rather than skip: a skipped-but-due
    # enrollment would re-scan every sweep forever; on re-upgrade the tenant
    # re-enrols from a reviewed sequence, never from a forgotten one.
    if not db.module_active(tenant_id, "crm"):
        db.crm_exit_enrollment(enr["id"], tenant_id, reason="module_off")
        return "exited"
    seq = db.crm_get_sequence(enr["sequence_id"], tenant_id)
    if not seq or seq.get("status") != "active":
        if seq and seq.get("outreach_managed") and seq.get("status") == "paused":
            db.crm_reschedule_enrollment(enr["id"], tenant_id, datetime.now(timezone.utc).isoformat())
            return "outreach_paused"
        db.crm_exit_enrollment(enr["id"], tenant_id, reason="stage_changed")
        return "exited"

    steps = db.crm_list_steps(enr["sequence_id"], tenant_id)[:MAX_STEPS]
    idx = int(enr.get("current_step") or 0)   # step to run now = current_step + 1 (1-based)
    if idx >= len(steps):
        db.crm_complete_enrollment(enr["id"], tenant_id)
        return "completed"
    step = steps[idx]

    contact = db.crm_get_contact(enr["contact_id"], tenant_id)
    if not contact:
        db.crm_exit_enrollment(enr["id"], tenant_id, reason="manual")
        return "exited"
    to = contact.get("email") or ""
    if contact.get("do_not_sequence"):
        db.crm_exit_enrollment(enr["id"], tenant_id, reason="manual")
        return "exited"

    # Exit-on-reply: any inbound from this contact since they enrolled.
    since = enr.get("enrolled_at") or enr.get("next_action_at") \
        or datetime.now(timezone.utc).isoformat()
    if (seq.get("outreach_managed") or step.get("exit_on_reply")) and to and db.crm_contact_replied_since(tenant_id, to, since):
        db.crm_exit_enrollment(enr["id"], tenant_id, reason="replied")
        return "replied"

    # Rate limit: max 1 sequence email/contact/day — reschedule +1 day, don't advance.
    if step.get("action_type") == "send_email" and db.crm_sequenced_today(tenant_id, enr["contact_id"]):
        nxt = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.crm_reschedule_enrollment(enr["id"], tenant_id, nxt)
        return "rate_limited"

    deal = db.crm_get_deal(enr["deal_id"], tenant_id) if enr.get("deal_id") else None
    cfg = db.effective_config(tenant_id, None)
    tenant = db.get_tenant(tenant_id) or {}
    ctx = _build_ctx(tenant, cfg, contact, deal)

    action = step.get("action_type") or "send_email"
    if seq.get("outreach_managed"):
        from app import tenant_outreach
        if not await tenant_outreach.process_step(enr, seq, step, ctx, contact):
            # Rotate held recipients behind other due work without advancing
            # their step or making a human approval wait for another timer.
            db.crm_reschedule_enrollment(enr["id"], tenant_id, datetime.now(timezone.utc).isoformat())
            return "outreach_held"
    elif action == "send_email":
        if to:
            await _do_send_email(tenant_id, cfg, to, step, ctx)
    elif action == "update_stage":
        stage_id = _side_effect_stage(step)
        if enr.get("deal_id") and stage_id:
            db.crm_update_deal(enr["deal_id"], tenant_id, {"stage_id": stage_id})
    elif action == "create_task":
        title = render_template(step.get("template_subject") or "", ctx).strip() \
            or f"Sequence follow-up: {seq.get('name') or ''}".strip()
        db.crm_create_task(tenant_id, title=title,
                           description=render_template(step.get("template_body") or "", ctx) or None,
                           contact_id=enr["contact_id"], deal_id=enr.get("deal_id"),
                           source="auto_sequence")
    elif action == "add_tag":
        # No tag system exists yet — best-effort: log + skip (§19.3 add_tag stub).
        log.info("sequence add_tag step skipped (no tag system): seq=%s enr=%s",
                 enr["sequence_id"], enr["id"])

    # Advance: next step re-arms the timer; none → complete.
    next_idx = idx + 1
    if next_idx < len(steps):
        delay = int(steps[next_idx].get("delay_days") or 0)
        nxt = (datetime.now(timezone.utc) + timedelta(days=delay)).isoformat()
        db.crm_advance_enrollment(enr["id"], tenant_id, current_step=next_idx, next_action_at=nxt)
        return "advanced"
    db.crm_advance_enrollment(enr["id"], tenant_id, current_step=next_idx,
                              next_action_at=datetime.now(timezone.utc).isoformat())
    db.crm_complete_enrollment(enr["id"], tenant_id)
    return "completed"


async def process_sequence_queue() -> dict:
    """Process every due enrollment (next_action_at <= now, status active) across all
    tenants. Service-role read; per-enrollment isolation."""
    now_iso = datetime.now(timezone.utc).isoformat()
    due = db.crm_due_enrollments(now_iso, limit=200)
    counts: dict = {}
    for enr in due:
        try:
            outcome = await _process_one(enr)
        except Exception:
            log.warning("sequence enrollment %s failed (tenant %s)",
                        enr.get("id"), enr.get("tenant_id"), exc_info=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"due": len(due), **counts}


# ── Seed templates (§19.3 table) ────────────────────────────────────────────
# Vertical → one default sequence (name, description, ordered steps). The bodies
# are BRIEFS — the composer rewrites them in the tenant's voice.

def default_sequences(vertical: str | None) -> list[dict]:
    """The built-in sequence template(s) for a vertical (falls back to Generic)."""
    generic = {
        "name": "Gone quiet — 3-touch re-engage",
        "description": "Re-engage a contact who has gone quiet, over three light touches.",
        "trigger_type": "gone_quiet",
        "steps": [
            {"delay_days": 3, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Checking in",
             "template_body": "A casual check-in with {{contact.name}} — no pressure, "
                              "just making sure nothing's slipped through."},
            {"delay_days": 5, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Anything I can help with?",
             "template_body": "A value-add nudge: offer to help {{contact.name}} with "
                              "anything outstanding on {{deal.title}}."},
            {"delay_days": 6, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Closing the loop",
             "template_body": "A warm break-up note — we'll assume the timing isn't right "
                              "and leave the door open for {{contact.name}} to come back."},
        ],
    }
    trades = {
        "name": "Quote follow-up",
        "description": "Chase a quote you've sent, then offer to adjust, then close.",
        "trigger_type": "deal_stage_enter",
        "steps": [
            {"delay_days": 2, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Your quote",
             "template_body": "Did {{contact.name}} get a chance to review the quote for "
                              "{{deal.title}}?"},
            {"delay_days": 5, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Happy to adjust",
             "template_body": "Offer to adjust the {{deal.title}} quote if the scope or "
                              "budget needs tweaking."},
            {"delay_days": 7, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Still keen?",
             "template_body": "Final follow-up on {{deal.title}} — offer to close it out or "
                              "re-quote if things have changed."},
        ],
    }
    logistics = {
        "name": "RFQ response",
        "description": "Confirm an RFQ, chase pricing questions, then a final availability window.",
        "trigger_type": "new_contact",
        "steps": [
            {"delay_days": 1, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "We've got your enquiry",
             "template_body": "Confirm to {{contact.name}} that we've received their RFQ and "
                              "are pulling pricing together."},
            {"delay_days": 4, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Any questions on pricing?",
             "template_body": "Check whether {{contact.name}} has any questions on the "
                              "pricing we sent for {{deal.title}}."},
            {"delay_days": 5, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Final availability",
             "template_body": "Give {{contact.name}} a final availability window before we "
                              "release the capacity."},
        ],
    }
    # Storefront OS (docs/os/storefront.md §12.2) — a multi-product clearance enrolls the
    # past-buyer audience here (3 light touches, exit-on-reply) instead of a one-shot
    # best-before draft, so the engine handles cadence + exit_on_reply. Same brief-not-
    # literal bodies; the composer rewrites them in the shop's voice.
    storefront = {
        "name": "Gone today — clearance",
        "description": "Clear a batch that's going off — a warm nudge to past buyers, "
                       "then a last-chance, then a soft close.",
        "trigger_type": "manual",
        "steps": [
            {"delay_days": 0, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Going today",
             "template_body": "A warm heads-up to {{contact.name}} that something they buy "
                              "is in and needs to go today — invite them to grab it at the "
                              "clearance price before close. Personal, not a mailshot."},
            {"delay_days": 1, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Last chance",
             "template_body": "A brief last-chance nudge — there's still a little left and "
                              "it won't keep; invite {{contact.name}} to pop in before it's gone."},
            {"delay_days": 2, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "All gone — thanks",
             "template_body": "A soft, no-pressure close: thank {{contact.name}} and let them "
                              "know we'll flag the next batch when it's in."},
        ],
    }
    # Events OS (docs/os/events.md §13 Phase C) — the post-event follow-up. After the day,
    # a warm thank-you (day+1), then a light feedback ask (day+3), exit-on-reply. Seeded for
    # events tenants; the SHIPPED sequence engine runs it (branching, exit_on_reply, rate-
    # limit, voice-rewrite) — no new execution loop. Enroll the booker at stage-enter
    # Post-event (or by hand); guests are never enrolled (§14 — they aren't CRM contacts).
    events = {
        "name": "Post-event thank-you + feedback",
        "description": "After the event: a warm thank-you, then a light feedback ask — "
                       "two touches, exit on reply.",
        "trigger_type": "deal_stage_enter",
        "steps": [
            {"delay_days": 1, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "Thank you",
             "template_body": "A warm thank-you to {{contact.name}} for choosing us for "
                              "{{deal.title}} — say it was a pleasure to host, and that we "
                              "hope the day went beautifully. No ask, just genuine thanks."},
            {"delay_days": 3, "action_type": "send_email", "exit_on_reply": True,
             "template_subject": "How did we do?",
             "template_body": "A light, no-pressure feedback ask: invite {{contact.name}} to "
                              "share how {{deal.title}} went and whether there's anything we "
                              "could do better — and mention we'd love a review or referral if "
                              "they enjoyed it. Warm, brief, easy to ignore."},
        ],
    }
    return {"trades": [trades], "logistics": [logistics],
            "storefront": [storefront], "events": [events]}.get((vertical or "").lower(),
                                                                 [generic])


def crm_seed_storefront_sequences(tenant_id: str) -> list[dict]:
    """Seed the storefront 'Gone today — clearance' sequence for a tenant (a thin alias
    over crm_seed_default_sequences for the storefront vertical). Called from onboarding /
    the storefront seed step; NOT auto-run on all tenants.

    ENROLLMENT HOOK (docs/os/storefront.md §12.2): to run a MULTI-PRODUCT clearance rather
    than the one-shot best-before drafter, enroll the past-buyer audience of each expiring
    product in this sequence — the existing engine (app/crm_sequences.process_sequence_queue,
    already on the 15-min run_every loop) then handles the 3-touch cadence + exit_on_reply.
    Wire that from a future "clear the shelf" action:

        seq = next(s for s in db.crm_list_sequences(tenant_id)
                   if s["name"] == "Gone today — clearance")
        for buyer in db.shop_past_buyers_of(tenant_id, product_id):   # deterministic, opt-out-safe
            db.crm_enroll(tenant_id, seq["id"], buyer["id"])          # do_not_sequence honoured in crm_enroll

    The one-shot best-before drafter (app/jobs/send_best_before) is the shipped core; this
    seeds the cadence template + the hook so a clearance run is a small future addition."""
    return crm_seed_default_sequences(tenant_id, "storefront")


def crm_seed_default_sequences(tenant_id: str, vertical: str | None) -> list[dict]:
    """Insert the vertical's default sequence(s) + steps for a tenant. Provided for
    onboarding — NOT auto-run on all tenants."""
    created = []
    for tmpl in default_sequences(vertical):
        seq = db.crm_create_sequence(
            tenant_id, name=tmpl["name"], description=tmpl.get("description"),
            trigger_type=tmpl.get("trigger_type", "manual"))
        for order, step in enumerate(tmpl["steps"][:MAX_STEPS]):
            db.crm_add_step(tenant_id, seq["id"], step_order=order,
                            delay_days=step.get("delay_days", 3),
                            action_type=step.get("action_type", "send_email"),
                            template_subject=step.get("template_subject"),
                            template_body=step.get("template_body"),
                            exit_on_reply=step.get("exit_on_reply", True),
                            side_effects=step.get("side_effects"))
        created.append(seq)
    return created


if __name__ == "__main__":
    print(asyncio.run(process_sequence_queue()))
