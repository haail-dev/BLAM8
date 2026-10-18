"""Text-messaging channel settings (docs/modules/module-sms-channel.md §9) — /app/channels.

One page: activate (provisions the tenant's UK mobile number and wires its
webhooks), the missed-call divert dial string with a test step, the text-back
toggle, and the opt-out list (view + manual add — removal stays with the
customer's own START, so a tenant can never quietly re-subscribe a STOP).

Every route gates on _require_sms (db.module_active(tenant_id, 'sms') → 404 if
off), the twin of practice_routes._require_practice. Mutations carry CSRF.
"""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db, sms
from app.auth import require_onboarded, TenantCtx
from app.dashboard import _ctx
from app.security import require_csrf
from app.templating import templates
from app import guards as _guards

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/channels", tags=["channels"])


def _require_sms(ctx: TenantCtx) -> None:
    if not db.module_active(ctx.tenant_id, "sms", client=ctx.client):
        raise HTTPException(status_code=404,
                            detail="Text messaging is not active for this account")


@router.get("")
def channels_view(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    _require_sms(ctx)
    conn = db.sms_connection(ctx.tenant_id, client=ctx.client)
    number = (conn or {}).get("phone_number")
    return templates.TemplateResponse(request, "app/channels.html", _ctx(
        "channels", ctx,
        conn=conn, number=number,
        # The GSM conditional-divert code (§5): forward only UNANSWERED calls —
        # the tenant keeps their number, BLAM8 only ever sees the missed ones.
        divert_code=f"*61*{number}#" if number else None,
        text_back_on=bool(((conn or {}).get("config") or {}).get("text_back_enabled", True)),
        opt_outs=db.sms_opt_out_list(ctx.tenant_id, client=ctx.client) if conn else [],
        twilio_ready=sms.configured()))


@router.post("/activate", dependencies=[Depends(require_csrf)])
async def activate(ctx: TenantCtx = Depends(require_onboarded)):
    _guards.require_owner(ctx)   # security review 2026-09 (H1/H2)
    _require_sms(ctx)
    try:
        await sms.provision_number(ctx.tenant_id)
    except sms.SmsNotConfigured:
        return RedirectResponse("/app/channels?msg=nocreds", status_code=303)
    except Exception:
        log.warning("number provisioning failed for tenant %s", ctx.tenant_id, exc_info=True)
        return RedirectResponse("/app/channels?msg=err", status_code=303)
    return RedirectResponse("/app/channels?msg=live", status_code=303)


@router.post("/textback", dependencies=[Depends(require_csrf)])
def toggle_text_back(enabled: str = Form("off"),
                     ctx: TenantCtx = Depends(require_onboarded)):
    _require_sms(ctx)
    conn = db.sms_connection(ctx.tenant_id, client=ctx.client)
    if not conn:
        return RedirectResponse("/app/channels?msg=err", status_code=303)
    config = dict(conn.get("config") or {})
    config["text_back_enabled"] = enabled == "on"
    db.update_channel_config(conn["id"], ctx.tenant_id, config)
    return RedirectResponse("/app/channels?msg=saved", status_code=303)


@router.post("/optout", dependencies=[Depends(require_csrf)])
def add_opt_out(phone: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    """Manual opt-out — a customer who asked in person/by phone. There is no
    manual opt-IN: only the customer's own START clears a STOP (PECR posture)."""
    _require_sms(ctx)
    e164 = sms.normalize_e164(phone)
    if not e164:
        return RedirectResponse("/app/channels?msg=badnumber", status_code=303)
    db.sms_opt_out(ctx.tenant_id, e164)
    return RedirectResponse("/app/channels?msg=optedout", status_code=303)
