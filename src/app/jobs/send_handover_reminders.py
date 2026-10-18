"""Assets OS — handover reminders (in-process daily) — docs/os/assets.md §6.

For each assets tenant, units going OUT tomorrow:
  * PAID deposit → a "you collect tomorrow" note (handover instructions).
  * UNPAID deposit → chase the deposit link so the hold is secured before collection.
A hire with no deposit row at all is treated like an unpaid one (chase the deposit).

Phase D also folds in a LIGHT maintenance nudge (§6/§11): units whose next_service_due
falls inside the lead window (or that have dropped below their condition floor) get ONE
internal "service due" note to the OWNER — not customer-facing. It reuses the FSM
service-reminder cadence (REMINDER_LEAD_DAYS) and dedupes per unit via
custom_data._service_nudged_at (stamp_unit_service_nudged), cleared on the next service.

The send_service_reminders shape: compose_outbound → insert_email_log(pending|auto) → optional
mailbox.send_mail. Deduped per hire via details.handover_reminded_at (no new column in Phase B),
so a re-run the same day never double-sends. Per-item try/except. Service role.

    python -m app.jobs.send_handover_reminders
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app import agent, assets, db, disclosure, mailbox, usage
from app.config import settings
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# Surface a unit for the owner nudge this many days before its service falls due — the FSM
# recurring-service lead window (send_service_reminders.REMINDER_LEAD_DAYS), reused verbatim.
SERVICE_LEAD_DAYS = 14

OWNER_SERVICE_PURPOSE = (
    "An INTERNAL heads-up to the business owner (not the customer) that one of their hire "
    "units is due a service soon or has dropped below its condition floor and wants a look. "
    "List the unit and why. Keep it short and practical — it's a to-do nudge for the owner.")

PAID_PURPOSE = ("A friendly heads-up that the customer collects their hire TOMORROW. Confirm it's "
                "all set and remind them to bring what they need for collection. Do NOT invent a "
                "time or address — keep it short and warm.")

CHASE_PURPOSE = ("A polite nudge: the customer collects their hire TOMORROW but the deposit that "
                 "secures it hasn't been paid yet. Ask them to pay the deposit to confirm the "
                 "booking before collection. Do NOT invent an amount or a link — keep it short "
                 "and friendly (the deposit link is added separately).")


def _capture_link(tenant_id: str, booking: dict) -> str | None:
    """Mint (once) a tokenised condition-capture link for this hire and return its URL —
    dropped into the handover note so the caretaker photographs the unit at collection
    (the OUT baseline the return diff needs, §12b/§14: no baseline, no claim). Reuses the
    track-token convention on a `capture_token`. Best-effort: a mint failure just omits it."""
    try:
        token = booking.get("capture_token") or assets.mint_capture_token()
        if not booking.get("capture_token"):
            db.set_booking_capture_token(booking["id"], tenant_id, token)
        base = (settings.app_base_url or "").rstrip("/")
        return f"{base}/capture/{token}"
    except Exception:
        log.warning("could not mint capture link for booking %s", booking.get("id"),
                    exc_info=True)
        return None


def _facts(booking: dict, paid: bool, capture_url: str | None = None) -> str:
    summary = booking.get("summary") or "your hire"
    if paid:
        base = f"The customer collects {summary} tomorrow and the deposit is paid — all set."
    else:
        base = (f"The customer collects {summary} tomorrow but the deposit hasn't been paid yet, "
                "so the booking isn't fully secured.")
    if capture_url:
        base += (f" At handover, capture the unit's condition here (photos + a rating): "
                 f"{capture_url}")
    return base


async def _remind_one(tenant_id: str, cfg: dict, auto: bool, booking: dict, paid: bool) -> str:
    """Compose + queue (and maybe send) one handover reminder. Returns 'sent' | 'drafted'."""
    to = booking["contact"]
    subject = "You collect tomorrow" if paid else "One step to secure your hire"
    capture_url = _capture_link(tenant_id, booking)
    facts = _facts(booking, paid, capture_url)
    purpose = PAID_PURPOSE if paid else CHASE_PURPOSE
    result = await agent.compose_outbound(cfg, purpose, facts,
                                          tenant_id=tenant_id, log_operation="reminder")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[handover reminder] {facts}", result.text,
        status="pending", kind="reminder", thread_id=booking.get("thread_id"))
    db.stamp_handover_reminded(booking["id"], tenant_id)   # dedupe even if the send fails
    db.log_agent_event(
        tenant_id, "handover_reminder",
        f"{'Reminded' if paid else 'Chased the deposit from'} {to} — collection tomorrow",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id,
        booking_id=booking["id"], booking_ref=booking.get("booking_ref"))
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id, module="email")
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the handover reminder to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def _nudge_owner_service(tenant_id: str, cfg: dict) -> int:
    """Queue at most ONE internal "service due" nudge to the OWNER per un-nudged unit (§6/§11).
    Reuses the FSM service-reminder cadence (asset_units_due_service, the lead window) and the
    compose_outbound → insert_email_log shape. Kept LIGHT: it's internal, so it's always a
    PENDING draft (never auto-sent) to the tenant's own mailbox, deduped per unit via
    stamp_unit_service_nudged. Returns how many were drafted. A missing owner address → 0."""
    units = db.asset_units_due_service(tenant_id, within_days=SERVICE_LEAD_DAYS)
    units = [u for u in units
             if not (u.get("custom_data") or {}).get("_service_nudged_at")]
    if not units:
        return 0
    owner = (db.get_tenant(tenant_id) or {}).get("mailbox")
    if not db.reachable_contact(owner):
        return 0
    rows = [assets.maintenance_row(u) for u in units]
    facts = "Units due attention:\n" + "\n".join(
        f"- {r['unit']}: {r['reason']}" for r in rows)
    result = await agent.compose_outbound(cfg, OWNER_SERVICE_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="reminder",
                                          log_module="assets")
    log_id, _ = db.insert_email_log(
        tenant_id, owner, "Units due a service", f"[service due] {facts}", result.text,
        status="pending", kind="reminder")
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="reminder", email_log_id=log_id, module="assets")
    for u in units:
        db.stamp_unit_service_nudged(u["id"], tenant_id)   # dedupe (cleared on next service)
    db.log_agent_event(
        tenant_id, "service_due",
        f"Nudged the owner: {len(units)} unit(s) due a service / below floor",
        direction="outbound", contact=owner, subject="Units due a service", email_log_id=log_id)
    return 1


async def run() -> dict:
    now = datetime.now(DIARY_TZ)
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    drafted = sent = service_nudges = 0
    for tenant_id in db.tenants_with_modules(("assets",)):
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        # Owner maintenance nudge (§6/§11) — light, internal, once per unit per cycle.
        try:
            service_nudges += await _nudge_owner_service(tenant_id, cfg)
        except Exception:
            log.warning("owner service nudge failed for tenant %s", tenant_id, exc_info=True)
        hires = db.hires_arriving_on(tenant_id, tomorrow)
        # Skip already-reminded hires (dedupe) and hires with no reachable contact.
        hires = [h for h in hires
                 if not (h.get("details") or {}).get("handover_reminded_at")
                 and db.reachable_contact(h.get("contact"))]
        if not hires:
            continue
        for booking in hires:
            try:
                deposit = db.get_deposit_for_booking(tenant_id, booking["id"])
                paid = bool(deposit) and deposit.get("status") in ("held", "paid")
                outcome = await _remind_one(tenant_id, cfg, auto, booking, paid)
            except Exception:
                # One bad hire (compose/send failure) must not stop the run; it retries next
                # day (the dedupe stamp lands only after the row is created).
                log.warning("handover reminder failed for booking %s (tenant %s)",
                            booking.get("id"), tenant_id, exc_info=True)
                continue
            sent += outcome == "sent"
            drafted += outcome == "drafted"
    return {"date": tomorrow, "drafted": drafted, "sent": sent,
            "service_nudges": service_nudges}


if __name__ == "__main__":
    print(asyncio.run(run()))
