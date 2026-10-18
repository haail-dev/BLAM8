"""Web Push delivery — the server half of the PWA notification layer.

One notification type for now (the one that matters): "Draft ready", fired from
src/app/pipeline.py when a new draft lands in the approval queue. Auto-sent replies
never notify — there is nothing for the owner to do.

Contract (same as events.publish / support._notify): a push is a best-effort
side-effect that must NEVER cost a tenant their draft. Delivery runs on a
daemon thread, every failure is swallowed to a warning, and with no VAPID keys
configured the whole module quietly no-ops.
"""
import json
import logging
import threading
from urllib.parse import quote

from app.config import settings
from app import db, notify_prefs, events, trace

log = logging.getLogger(__name__)

# Kinds that are operator/platform infrastructure, not the tenant's own business —
# they push to the platform mailbox tenant and must NOT enter that tenant's
# in-app feed (the concierge digest would show them support tickets).
_FEED_EXCLUDED_KINDS = {"support_ticket", "operator_signup"}


def push_enabled() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


def _record_notification(tenant_id: str, *, title: str, body: str, url: str,
                         kind: str, contact: str | None = None,
                         contact_name: str | None = None) -> None:
    """The in-app half of every notification: a concierge_notifications row
    (push is delivery; the row is the record — written even with push off) plus
    a live 'notification' event so an open tab's bubble starts glowing without
    a reload. Best-effort by the same contract as push itself.

    `contact` names who the notification is about, so Today's deck can put it on
    that person's card (2026-09-04). Optional everywhere: a notification with
    nobody attached is still a feed row, it just has no card to join."""
    if kind in _FEED_EXCLUDED_KINDS:
        return
    try:
        db.add_concierge_notification(tenant_id, kind=kind, title=title,
                                      body=body, url=url, contact=contact,
                                      contact_name=contact_name)
        events.publish(tenant_id, {"type": "notification", "kind": kind})
    except Exception:
        log.warning("notification feed write failed (tenant=%s kind=%s)",
                    tenant_id, kind, exc_info=True)


def notify_draft_ready(tenant_id: str, *, contact_name: str | None,
                       contact_address: str, subject: str | None,
                       log_id: str, reason: str = "") -> None:
    """Fire-and-forget "Draft ready" push to every subscribed device of a tenant.

    Tag is draft-{log_id}: a redelivery for the same draft replaces the shown
    notification instead of stacking. The click-through deep-links to the
    conversation (threads are keyed by contact address, not id)."""
    who = (contact_name or "").strip() or contact_address
    payload = {
        "title": f"Draft ready — {who}",
        # Why it waits, when there's a reason beyond "it's a draft" (full-auto
        # test 2026-09-26: an escalation, an angry customer and "check with
        # Dan" all read the same).
        "body": (f"Held — {reason}. Tap to review." if reason else
                 f"{(subject or '').strip() or 'New message'} — tap to review and approve"),
        "url": "/app/conversations/view?contact=" + quote(contact_address),
        "tag": f"draft-{log_id}",
    }
    _record_notification(tenant_id, title=payload["title"], body=payload["body"],
                         url=payload["url"], kind="draft_ready",
                         contact=contact_address, contact_name=contact_name)
    # Its own send path, so it needs its own gate — the Drafts family would
    # otherwise be a switch wired to nothing, which is worse than no switch.
    cfg = _prefs(tenant_id)
    if not notify_prefs.should_push(cfg, "draft_ready"):
        _trace_send(tenant_id, "draft_ready", payload, outcome="held",
                    note="notification settings")
        return
    _deliver(tenant_id, payload, "draft_ready", cfg)


def _prefs(tenant_id: str) -> dict:
    """The tenant's notification settings, FAILING OPEN. A config read that
    errors must not silence a business's notifications — an empty dict means
    every family is 'now', which is exactly what happened before there were
    settings at all."""
    try:
        return db.get_tenant_config(tenant_id) or {}
    except Exception:
        log.warning("notify prefs read failed (tenant=%s) — delivering",
                    tenant_id, exc_info=True)
        return {}


def notify(tenant_id: str, *, title: str, body: str, url: str, tag: str,
           kind: str = "generic", contact: str | None = None,
           contact_name: str | None = None) -> None:
    """Fire-and-forget generic push to every subscribed device of a tenant —
    the portal's booking/reschedule/enquiry notifications ride this. Same
    contract as notify_draft_ready: best-effort, off-thread, silently off
    without VAPID keys. `kind` names the notification type in the trace AND
    in the tenant's in-app feed (recorded regardless of push being on).

    The tenant's settings decide the PUSH only (src/app/notify_prefs.py): the feed
    row is written either way, because the feed is the record and a customer
    accepting a quote is a fact about the business, not a message you can
    decline. A family set to "in the brief", or an event landing inside quiet
    hours, means no buzz now — the morning brief mentions it instead.

    `contact`/`contact_name` say who it is ABOUT. They reach the feed row only —
    the push payload is unchanged — so Today's deck can file the event on that
    person's card rather than knowing only aggregate state (2026-09-04)."""
    _record_notification(tenant_id, title=title, body=body, url=url, kind=kind,
                         contact=contact, contact_name=contact_name)
    cfg = _prefs(tenant_id)
    if not notify_prefs.should_push(cfg, kind):
        _trace_send(tenant_id, kind, {"title": title, "tag": tag},
                    outcome="held", note="notification settings")
        return
    _deliver(tenant_id, {"title": title, "body": body, "url": url, "tag": tag},
             kind, cfg)


def notify_operator(*, title: str, body: str, url: str, tag: str, kind: str) -> None:
    """Push the OPERATOR's devices: routed to the tenant that owns
    PLATFORM_NOTIFY_MAILBOX (the same lookup support's ticket push uses),
    because subscriptions are per-tenant and that tenant is the operator's own.
    Kinds routed here belong in _FEED_EXCLUDED_KINDS — platform infrastructure must
    not surface in that tenant's in-app feed. Best-effort throughout."""
    if not push_enabled() or not settings.platform_notify_mailbox:
        return
    try:
        conn = db.get_connection_by_address(settings.platform_notify_mailbox)
    except Exception:
        log.warning("operator push: mailbox lookup failed (kind=%s)", kind, exc_info=True)
        return
    if not conn:
        return
    notify(conn["tenant_id"], title=title, body=body, url=url, tag=tag, kind=kind)


def notify_signup(tenant_id: str, company_name: str | None, email: str | None) -> None:
    """"Someone just registered" (Kris, 2026-08-03) — fired from
    db.ensure_tenant_for_user's create branch, so every door that mints a
    tenant (SSO callback, a partner's own set-up) pings the operator's phone.
    Tag signup-{tenant_id}: a re-fire replaces, never stacks."""
    who = (company_name or "").strip() or "New business"
    notify_operator(
        title="New BLAM8 sign-up 🎉",
        body=f"{who} — {email}" if email else who,
        url=f"/admin/tenants/{tenant_id}",
        tag=f"signup-{tenant_id}", kind="operator_signup")


def _trace_send(tenant_id: str, kind: str, payload: dict, *, outcome: str,
                devices: list[dict] | None = None, note: str | None = None) -> None:
    """One trace row per notification (not per device): the per-device results
    ride in detail. delivery semantics are honest — a 2xx from the push service
    means ACCEPTED for delivery, not shown on the device (Web Push gives no
    receipt), so per-device status is accepted/failed/expired, never
    'delivered'; the row says so explicitly."""
    trace.emit(tenant_id, "push_send", outcome=outcome,
               detail={"kind": kind, "tag": payload.get("tag"),
                       "title": payload.get("title"),
                       "delivery_receipts": "unavailable",   # Web Push has none
                       **({"held_because": note} if note else {}),
                       "devices": devices or []})


def _email_to_tenant(tenant_id: str, payload: dict, address: str,
                     kind: str) -> None:
    """The same notification as mail (runs off-thread).

    Sent FROM the tenant's connected mailbox, because the product has no
    transactional sender — so it lands in their Sent folder, and it must never
    be addressed to the mailbox BLAM8 monitors (notify_prefs.email_target
    refuses that, and this asserts nothing about it: it just sends where it is
    told).

    Best-effort by the same contract as push: a send that fails costs one
    notification and is traced, never an exception into the caller's thread.
    """
    import asyncio

    from app import mailbox

    body = payload.get("body") or ""
    url = payload.get("url") or ""
    text = body + (("\n\n" + settings.app_base_url.rstrip("/") + url) if url else "")
    try:
        asyncio.run(mailbox.send_mail(tenant_id, address,
                                      payload.get("title") or "BLAM8", text))
        _trace_send(tenant_id, kind, payload, outcome="ok",
                    devices=[{"channel": "email", "to": address, "status": "sent"}])
    except Exception as exc:                                  # noqa: BLE001
        log.warning("notification email failed (tenant=%s kind=%s)",
                    tenant_id, kind, exc_info=True)
        _trace_send(tenant_id, kind, payload, outcome="failed",
                    devices=[{"channel": "email", "to": address,
                              "status": "failed", "error": str(exc)[:120]}])


def _deliver(tenant_id: str, payload: dict, kind: str, cfg: dict) -> None:
    """Both channels, each optional, neither blocking the caller.

    Push reaches devices that installed the app and said yes; email reaches an
    inbox. An operator who never installs the PWA had nothing before this but a
    bubble they had to go looking for — so email is not a fallback for push, it
    is a channel in its own right, and both answer to the same family setting.
    """
    if push_enabled():
        threading.Thread(target=_send_to_tenant, args=(tenant_id, payload, kind),
                         daemon=True, name="push-" + str(payload.get("tag"))).start()
    try:
        tenant = db.get_tenant(tenant_id) or {}
        address = notify_prefs.email_target(cfg, tenant.get("mailbox"))
    except Exception:                                         # noqa: BLE001
        # Failing CLOSED here, unlike the settings read: without the monitored
        # address we cannot prove the mail isn't addressed at the agent's own
        # inbox, and a notification loop is worse than a missed notification.
        log.warning("notify email target lookup failed (tenant=%s)", tenant_id,
                    exc_info=True)
        address = None
    if address:
        threading.Thread(target=_email_to_tenant,
                         args=(tenant_id, payload, address, kind),
                         daemon=True, name="mail-" + str(payload.get("tag"))).start()


def _send_to_tenant(tenant_id: str, payload: dict, kind: str = "generic") -> None:
    """Deliver one payload to every subscription of a tenant (runs off-thread)."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:                       # dep missing locally — degrade
        log.warning("pywebpush not installed — push notification dropped")
        _trace_send(tenant_id, kind, payload, outcome="unknown")
        return
    try:
        subs = db.list_push_subscriptions(tenant_id)
    except Exception:
        log.warning("push: listing subscriptions failed (tenant=%s)",
                    tenant_id, exc_info=True)
        _trace_send(tenant_id, kind, payload, outcome="error")
        return
    if not subs:
        _trace_send(tenant_id, kind, payload, outcome="no_devices")
        return
    data = json.dumps(payload)
    devices: list[dict] = []
    for sub in subs:
        device = {"subscription_id": sub.get("id"), "user_id": sub.get("user_id")}
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["keys_p256dh"], "auth": sub["keys_auth"]},
                },
                data=data,
                vapid_private_key=settings.vapid_private_key,
                # Fresh dict per call: pywebpush stamps aud/exp into the claims,
                # and aud differs per push service (FCM vs Mozilla vs WNS).
                vapid_claims={"sub": settings.vapid_subject},
            )
            db.touch_push_subscription(sub["id"])
            device["status"] = "accepted"
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                # The push service says this device unsubscribed — drop the row.
                # The trace row below is now the only record the device existed.
                db.remove_push_subscription_by_id(sub["id"])
                device["status"] = "expired_removed"
            else:
                log.warning("push send failed (tenant=%s tag=%s status=%s)",
                            tenant_id, payload.get("tag"), status)
                device["status"] = "failed"
                device["http_status"] = status
        except Exception as exc:
            log.warning("push send failed (tenant=%s tag=%s)", tenant_id,
                        payload.get("tag"), exc_info=True)
            device["status"] = "failed"
            device["error"] = repr(exc)[:200]
        devices.append(device)
    accepted = sum(1 for d in devices if d["status"] == "accepted")
    outcome = ("ok" if accepted == len(devices)
               else "partial" if accepted else "failed")
    _trace_send(tenant_id, kind, payload, outcome=outcome, devices=devices)
