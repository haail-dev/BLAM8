"""Live-inbox stream — one SSE connection per open dashboard tab.

GET /app/stream holds a text/event-stream open and relays this tenant's
src/app/events.py bus traffic (drafting / draft_ready / draft_sent / status_changed)
to the browser, which refreshes the affected fragment (src/app/static/live.js). The
connection is authenticated exactly like every dashboard page (require_tenant
cookies); an expired session just fails the reconnect and EventSource retries
after the browser re-authenticates.

A comment heartbeat goes out every 25s of silence so Render's proxy never
reaps the connection as idle — EventSource treats comments as keep-alives.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app import events
from app.auth import require_tenant, TenantCtx
from app.security import rate_limit

log = logging.getLogger(__name__)
router = APIRouter(tags=["sse"])

_HEARTBEAT_SECONDS = 25


def _frame(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"


async def _stream(tenant_id: str):
    q = events.attach(tenant_id)
    try:
        # Hello carries the drafts in flight RIGHT NOW, so a tab that connects
        # (or reconnects) mid-draft shows the typing bubble immediately.
        yield _frame({"type": "hello", "drafting": events.drafting_now(tenant_id)})
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _frame(event)
    finally:
        # Runs on client disconnect too (Starlette closes the generator).
        events.detach(tenant_id, q)


@router.get("/app/stream", dependencies=[Depends(rate_limit("sse", 30, 60))])
async def sse_events(ctx: TenantCtx = Depends(require_tenant)):
    return StreamingResponse(
        _stream(ctx.tenant_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
