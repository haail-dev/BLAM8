"""Push subscription endpoints — the browser registers/unregisters its device.

POST /api/push/subscribe    body = PushSubscription.toJSON() from the browser
POST /api/push/unsubscribe  body = {endpoint}

Auth mirrors the other JSON APIs (concierge_routes): onboarded tenant context +
double-submit CSRF header + a per-IP rate limit. The writes themselves go
through the service role (db.upsert_push_subscription) because a device
changing hands between users is an UPDATE across user_ids that per-user RLS
would (correctly, for direct access) refuse.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.auth import TenantCtx, require_onboarded
from app.security import rate_limit, require_csrf_header

router = APIRouter(prefix="/api/push")


class _Keys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=512)
    auth: str = Field(..., min_length=1, max_length=512)


class SubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)
    keys: _Keys


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)


def _require_push_endpoint(endpoint: str) -> None:
    # Push service endpoints are always https URLs; anything else is junk we
    # would later POST notification ciphertext at.
    if not endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="not a push endpoint")


@router.post("/subscribe", dependencies=[
    Depends(rate_limit("push_subscribe", limit=10, window_s=60)),
    Depends(require_csrf_header),
])
def subscribe(body: SubscribeRequest,
                    ctx: TenantCtx = Depends(require_onboarded)) -> dict:
    _require_push_endpoint(body.endpoint)
    db.upsert_push_subscription(ctx.tenant_id, ctx.user_id, body.endpoint,
                                body.keys.p256dh, body.keys.auth)
    return {"ok": True}


@router.post("/unsubscribe", dependencies=[
    Depends(rate_limit("push_unsubscribe", limit=10, window_s=60)),
    Depends(require_csrf_header),
])
def unsubscribe(body: UnsubscribeRequest,
                      ctx: TenantCtx = Depends(require_onboarded)) -> dict:
    db.delete_push_subscription(ctx.user_id, body.endpoint)
    return {"ok": True}
