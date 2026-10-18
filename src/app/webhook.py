"""Inbound-mail push receivers → automatic drafting.

/webhook — Microsoft Graph change notifications (Days 10-11). Graph validates
this URL when a subscription is created, then POSTs here the moment new mail
arrives. We verify clientState, find the mailbox by its subscription id, and
draft a reply straight into the queue — no button needed.

/webhook/gmail — the Gmail counterpart: users.watch publishes to a Pub/Sub
topic whose push subscription POSTs here (endpoint URL carries ?token=… as the
shared secret). The notification only says "this mailbox changed", so we run
the same poll_connection the poller uses — watermark, dedupe and re-baseline
all come free, and the poller stays on as the safety net.

Both are public endpoints (called by the provider, not a logged-in user).
"""
import asyncio
import base64
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app import db, pipeline, sync_backoff
from app.config import settings
from app.jobs import poll_gmail
from app.security import rate_limit

log = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])


@router.api_route("/webhook", methods=["GET", "POST"], dependencies=[Depends(rate_limit("webhook", 120, 60))])
async def webhook(request: Request):
    # 1) Subscription validation handshake — echo the token back as plain text.
    token = request.query_params.get("validationToken")
    if token:
        return PlainTextResponse(token, status_code=200)

    # 2) Change notification(s).
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("", status_code=202)

    for note in body.get("value", []):
        if not hmac.compare_digest(note.get("clientState") or "", settings.graph_webhook_secret):
            continue  # not from our subscription (constant-time compare)
        sub_id = note.get("subscriptionId")
        msg_id = (note.get("resourceData") or {}).get("id")
        if not sub_id or not msg_id:
            continue
        conn = db.get_connection_by_subscription(sub_id)
        if not conn:
            continue
        try:
            # The webhook is the automatic inbound path, so it opts into full-auto send
            # (gated again per-tenant by tenant_config.auto_send inside the pipeline).
            # The subscription already identifies WHICH mailbox received the mail —
            # its connection id is the mailbox_id the whole pipeline keys off
            # (enterprise D5: per-mailbox brain, reply from the receiving address).
            await pipeline.draft_for_message(
                conn["tenant_id"], msg_id, auto_send_ok=True, mailbox_id=conn["id"]
            )
        except Exception:
            pass  # never fail the webhook — Graph would back off / disable it

    return PlainTextResponse("", status_code=202)


# In-flight push-triggered polls — referenced so the event loop can't GC a
# running task; each removes itself when done.
_push_tasks: set = set()


async def _poll_pushed_connection(conn: dict) -> None:
    """One push-triggered sweep of one Gmail connection. Shares the poller's
    backoff key so a broken grant being hammered by Pub/Sub redeliveries can't
    traceback-spam the log (the OOM lesson — src/app/sync_backoff.py)."""
    key = ("gmail", conn.get("id"))
    if sync_backoff.should_skip(key):
        return
    try:
        n = await poll_gmail.poll_connection(conn)
        sync_backoff.record_success(key)
        if n:
            log.info("gmail push: drafted %d new email(s) for connection %s", n, conn.get("id"))
    except Exception:
        if sync_backoff.record_failure(key):
            log.warning("gmail push: poll failed for connection %s; skipping with backoff",
                        conn.get("id"), exc_info=True)


@router.post("/webhook/gmail", dependencies=[Depends(rate_limit("webhook_gmail", 120, 60))])
async def webhook_gmail(request: Request):
    # Shared-secret gate — the Gmail mirror of the Graph clientState check. A bad
    # token gets a 403 (Pub/Sub will retry, which is right: if it's OUR config
    # that's wrong, the retries surface it; an attacker's retries change nothing).
    token = request.query_params.get("token") or ""
    if not settings.gmail_pubsub_token or not hmac.compare_digest(token, settings.gmail_pubsub_token):
        return PlainTextResponse("", status_code=403)

    # Pub/Sub envelope: message.data is base64 JSON {"emailAddress", "historyId"}.
    # Anything malformed is ACKed (204) — retrying a bad message forever helps no one.
    try:
        body = await request.json()
        payload = json.loads(base64.b64decode((body.get("message") or {}).get("data") or "").decode())
        address = payload.get("emailAddress") or ""
    except Exception:
        return PlainTextResponse("", status_code=204)

    conn = db.get_google_connection_by_address(address)
    if conn:
        _schedule_poll(conn)
    return PlainTextResponse("", status_code=204)


def _schedule_poll(conn: dict) -> None:
    """ACK now, draft in the background: drafting outlives Pub/Sub's ack
    deadline, and a redelivery would only re-run the same idempotent poll."""
    task = asyncio.create_task(_poll_pushed_connection(conn))
    _push_tasks.add(task)
    task.add_done_callback(_push_tasks.discard)
