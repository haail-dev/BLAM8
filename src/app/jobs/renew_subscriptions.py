"""Renew mailbox push watches before they expire.

Microsoft: Graph message subscriptions max out near 4230 minutes, so without
this the mailbox webhook silently dies in ~3 days. Gmail: users.watch (the
Pub/Sub push feed, /webhook/gmail) lapses after ~7 days — the same sweep
re-arms it, and also arms mailboxes connected before push existed (no watch
stamp yet). Both are self-healing: a missed renewal is recovered on the next
sweep from the saved token, no user re-consent.

Runs in-process from app.main startup (renew_forever) — so no separate Render
cron is required. `python -m app.jobs.renew_subscriptions` still does one
manual sweep.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import gmail, graph, health, mailbox
from app.config import settings
from app.db import db, list_all_connections, set_gmail_watch_expiry, set_subscription

log = logging.getLogger("blautom8.sub_renew")


def _expires(iso: str | None):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _run() -> dict:
    """Keep every mailbox watch alive. Renews subscriptions expiring within 24h and —
    crucially — RECREATES one that is missing or already expired, from the saved
    token, so a single missed renewal can't permanently kill auto-draft (the gap
    that silently broke it before)."""
    now = datetime.now(timezone.utc)
    renew_before = now + timedelta(hours=24)
    conns = list_all_connections()
    renewed = created = skipped = 0
    for conn in conns:
        if not conn.get("refresh_token") and not conn.get("access_token"):
            continue
        if mailbox.connection_provider(conn, conn["tenant_id"]) == "google":
            # Gmail push watch (users.watch → Pub/Sub, /webhook/gmail) — the Graph
            # subscription's counterpart: ~7-day life, re-armed inside the same 24h
            # window. A missing stamp (pre-push mailbox, or a failed arm) is armed
            # here too. No topic configured = push is off; the poller carries Gmail.
            if not settings.gmail_pubsub_topic:
                continue
            wexp = _expires(conn.get("gmail_watch_expiry"))
            if wexp is not None and wexp > renew_before:
                skipped += 1
                continue
            try:
                access = await mailbox.valid_access_token(conn, "google")
                res = await gmail.watch_mailbox(access, settings.gmail_pubsub_topic)
                exp_ms = int(res.get("expiration") or 0)
                if exp_ms:
                    set_gmail_watch_expiry(
                        conn["id"],
                        datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).isoformat())
                renewed += 1
            except Exception:
                pass  # one bad connection shouldn't stop the rest
            continue
        # The token FIRST, before the not-due skip — this call is the only
        # regular liveness probe a Microsoft mailbox gets (Kris 2026-09-04).
        # Gmail has a poller failing loudly every couple of minutes; Microsoft
        # has a webhook that keeps looking fine while the grant behind it is
        # dead, and a renewal attempt that only happens within 24h of expiry.
        # Asking for an access token every sweep costs one refresh per six
        # hours per mailbox and is what makes "your mailbox has stopped"
        # possible at all. Failing here is the grant, not the subscription.
        try:
            access = await mailbox.valid_access_token(conn, "microsoft")  # refresh + re-encrypt via db
            health.record_success(conn["id"], "mailbox")
        except Exception as exc:                              # noqa: BLE001
            health.record_failure(conn["id"], "mailbox", repr(exc)[:200])
            continue
        exp = _expires(conn.get("sub_expires"))
        active = bool(conn.get("subscription_id")) and exp is not None and exp > now
        if active and exp > renew_before:
            skipped += 1
            continue  # healthy, not due yet
        try:
            if active:  # still alive but expiring soon → renew in place
                try:
                    res = await graph.renew_subscription(access, conn["subscription_id"])
                    db().table("email_connections").update(
                        {"sub_expires": res["expirationDateTime"]}
                    ).eq("id", conn["id"]).execute()
                    renewed += 1
                    continue
                except Exception:
                    pass  # renew failed (e.g. already deleted) → recreate below
            # missing / expired / renew-failed → build a fresh subscription
            if conn.get("subscription_id"):
                try:
                    await graph.delete_subscription(access, conn["subscription_id"])
                except Exception:
                    pass
            sub = await graph.create_subscription(
                access, f"{settings.app_base_url}/webhook", settings.graph_webhook_secret
            )
            set_subscription(conn["id"], sub["id"], sub["expirationDateTime"])
            created += 1
        except Exception:
            pass  # one bad connection shouldn't stop the rest
    return {"checked": len(conns), "renewed": renewed, "created": created, "skipped": skipped}


async def renew_forever(interval_seconds: int) -> None:
    """In-process loop started by app.main — the Graph counterpart of the Gmail
    poller. `_run()` already swallows per-connection errors and recreates missing
    or expired subscriptions, so a deploy/restart self-heals any lapsed mailbox on
    the first sweep; this guards the sweep itself so the task never dies."""
    log.info("graph subscription renewer started (every %ss)", interval_seconds)
    while True:
        try:
            res = await _run()
            if res.get("renewed") or res.get("created"):
                log.info("graph subscription renew: %s", res)
        except Exception:
            log.exception("graph subscription renew sweep failed; continuing")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    print(asyncio.run(_run()))
