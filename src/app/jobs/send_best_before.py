"""Storefront best-before drafter (in-process daily) — docs/os/storefront.md §12.2.

The retention/margin killer feature. Daily at 07:00, for each storefront tenant, find
products inside their best-before lead window with stock still on hand
(db.shop_products_expiring); for each, pull the DISTINCT past buyers of THAT product from
completed-transaction history (db.shop_past_buyers_of — deterministic SQL, so the
targeting can never hallucinate a non-buyer); and draft a warm, personal "going today,
£X off" to each in the shop's learned voice. Queued as a pending 'special' draft for
approval — or sent immediately for full-auto tenants.

The Storefront twin of src/app/jobs/send_service_reminders.py: iterate tenants, per-item
try/except so one bad draft never stops the run, compose_outbound → insert_email_log →
optional auto-send → log_agent_event + usage.record_anthropic. Service role.

Guardrails (§12.2, the sequences guardrails reused):
  - max ONE special per contact per day even across multiple expiring products — an
    in-run `seen` set covers same-run collisions (a buyer of two expiring products gets
    one special), and db.shop_had_special_today covers cross-run collisions;
  - opt-outs (crm_contacts.do_not_sequence) are excluded IN the past-buyer query, so a
    "stop"/"unsubscribe" contact is never targeted;
  - the drafter states ONLY the facts passed in (no invented stock, prices or dates), and
    the audience is past-buyers-of-THIS-product only, never a blast.

    python -m app.jobs.send_best_before
"""
import asyncio
import logging
from datetime import datetime

from app import agent, db, disclosure, mailbox, usage
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)

# The shop's default clearance discount — read from tenant config (editable later), a
# sensible default when unset. The facts state it verbatim; the drafter never invents one.
DEFAULT_DISCOUNT = "20% off going-today stock"

# The §12.2 purpose string, verbatim.
BEST_BEFORE_PURPOSE = ("A warm, brief heads-up that a product this customer has bought "
                       "before is in today and needs to go — invite them to grab it at "
                       "the stated discount before close. Personal, not a mailshot. Do "
                       "NOT invent stock, prices or dates beyond the facts.")


def _discount(cfg: dict) -> str:
    """The tenant's clearance discount line (config-editable; a sensible default)."""
    return str(cfg.get("best_before_discount") or DEFAULT_DISCOUNT).strip() or DEFAULT_DISCOUNT


def _buyer_history(tenant_id: str, contact_id: str) -> str:
    """A short, personal note from the buyer's OWN completed transactions ("you usually
    grab a sourdough on Saturdays"). Best-effort — no history just yields ''; the facts
    stay factual either way."""
    try:
        txns = db.shop_list_transactions(tenant_id, status="completed",
                                         contact_id=contact_id, limit=20)
    except Exception:
        return ""
    names: list[str] = []
    for tx in txns or []:
        for it in (tx.get("shop_transaction_items") or []):
            n = (it.get("product_name") or "").strip()
            if n and n not in names:
                names.append(n)
    if not names:
        return ""
    return "They usually buy: " + ", ".join(names[:5]) + "."


def _facts(product: dict, discount: str, history: str) -> str:
    """The facts string for one special — the product, its best-before, the discount,
    and (when known) the buyer's own history. Nothing invented."""
    name = (product.get("name") or "the product").strip()
    unit = (product.get("unit") or "").strip()
    qty = product.get("qty_on_hand")
    bb = str(product.get("best_before_date") or "")[:10]
    parts = [f"'{name}' is in the shop today and needs to go before close."]
    if bb:
        parts.append(f"It's best before {bb}.")
    if qty not in (None, ""):
        try:
            parts.append(f"There's about {float(qty):g}{(' ' + unit) if unit else ''} left.")
        except (TypeError, ValueError):
            pass
    parts.append(f"The offer: {discount}.")
    if history:
        parts.append(history)
    return " ".join(parts)


async def _draft_one(tenant_id: str, cfg: dict, auto: bool, product: dict,
                     discount: str, buyer: dict) -> str:
    """Compose + queue (and maybe send) one personalised best-before special. Returns
    'sent' | 'drafted'. The audience is a past buyer of THIS product (deterministic)."""
    to = (buyer.get("email") or "").strip()
    name = (product.get("name") or "your favourite").strip()
    subject = f"{name} — going today"
    history = _buyer_history(tenant_id, buyer.get("id")) if buyer.get("id") else ""
    facts = _facts(product, discount, history)
    result = await agent.compose_outbound(cfg, BEST_BEFORE_PURPOSE, facts,
                                          tenant_id=tenant_id, log_operation="special")
    log_id, _ = db.insert_email_log(
        tenant_id, to, subject, f"[best-before] {facts}", result.text,
        status="pending", kind="special", thread_id=None)
    db.log_agent_event(
        tenant_id, "special",
        f"Drafted a best-before special on {name} for {to}",
        direction="outbound", contact=to, subject=subject, email_log_id=log_id)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="special", email_log_id=log_id)
    if auto:
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
        db.log_agent_event(tenant_id, "sent",
                           f"Sent the best-before special to {to} (auto)",
                           direction="outbound", contact=to, subject=subject,
                           email_log_id=log_id, detail={"via": "auto"})
        return "sent"
    return "drafted"


async def run() -> dict:
    today = datetime.now(DIARY_TZ).date().isoformat()
    drafted = sent = 0
    for tenant_id in db.tenants_with_modules(("storefront",)):
        products = db.shop_products_expiring(tenant_id)
        if not products:
            continue
        cfg = db.effective_config(tenant_id, None)
        auto = bool(cfg.get("auto_send"))
        discount = _discount(cfg)
        # Per-contact/day cap ACROSS products: an in-run `seen` set means a buyer of two
        # expiring products gets ONE special this run; shop_had_special_today covers a
        # buyer already nudged on an earlier run today. Collisions skip to the next run.
        seen: set = set()
        for product in products:
            try:
                buyers = db.shop_past_buyers_of(tenant_id, product["id"])
            except Exception:
                log.warning("past-buyers lookup failed for product %s (tenant %s)",
                            product.get("id"), tenant_id, exc_info=True)
                continue
            for buyer in buyers:
                cid = buyer.get("id")
                if not cid or cid in seen:
                    continue
                try:
                    if db.shop_had_special_today(tenant_id, cid, today):
                        seen.add(cid)   # already capped today — don't re-check per product
                        continue
                    outcome = await _draft_one(tenant_id, cfg, auto, product, discount, buyer)
                except Exception:
                    # One bad draft (compose error, send failure) must not stop the run.
                    log.warning("best-before special failed for contact %s (tenant %s)",
                                cid, tenant_id, exc_info=True)
                    continue
                seen.add(cid)
                sent += outcome == "sent"
                drafted += outcome == "drafted"
    return {"date": today, "drafted": drafted, "sent": sent}


if __name__ == "__main__":
    print(asyncio.run(run()))
