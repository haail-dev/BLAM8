"""Tenant journey: visit tracking + the operator console's funnel read.

Answers the operator questions the database previously answered only by hand
(2026-08-14, the Muneeb signup): did a new tenant finish onboarding, where did
they stall, when were they last here, how often do they come back, and are the
trial entitlements actually in place?

Two halves:

* touch() — called from auth.require_tenant on every authenticated request.
  Maintains tenant_visits: a request >30 min after the tenant's previous one
  starts a new visit row, anything sooner extends the current one. Writes are
  throttled to one per minute per tenant via an in-process cache, so the cost
  on the request path is ~zero and a crashed write is only a coarser number.
  Strictly best-effort: it must never take down a request.

* tenant_journey() — the /admin/tenants/{id} read: signup + auth identity,
  the wizard funnel with the first incomplete step marked (mirrors
  src/app/onboarding.py STEPS — interview, mailbox, voice, knowledge), visit
  history rollup, and the activity counts that say whether the trial is being
  *used* (contacts, bookings, knowledge) rather than merely provisioned.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import db

log = logging.getLogger("blam8.journey")

VISIT_GAP = timedelta(minutes=30)      # silence longer than this = a new visit
WRITE_EVERY = timedelta(seconds=60)    # DB write throttle per tenant


@dataclass
class _Live:
    """Per-tenant in-process visit state between throttled writes."""
    visit_id: str
    last_seen: datetime          # newest request seen (may be ahead of the DB)
    last_write: datetime         # when the DB row last heard from us
    pending: int = 0             # requests since last_write


_live: dict[str, _Live] = {}
_touch_lock = threading.Lock()
# The once-a-minute heartbeat write is a Supabase round trip (~0.5 s for its read
# and update). Nobody's page should wait for it, so one background worker does it.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journey-write")


def _bump(visit_id, seen_iso, pending):
    try:
        db.bump_visit(visit_id, seen_iso, pending)
    except Exception:
        log.warning("visit bump failed for visit %s", visit_id, exc_info=True)


def wait_for_writes():
    """Block until queued heartbeat writes have run (tests and shutdown)."""
    _writer.submit(lambda: None).result(timeout=10)


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def touch(tenant_id: str, user_id: str | None = None) -> None:
    """Record 'this tenant is here right now'. Never raises."""
    # Auth awaits this on a worker thread. Preserve the previous serialization
    # of cold adoption/insert, pending counts and throttled writes.
    with _touch_lock:
        _touch(tenant_id, user_id)


def _touch(tenant_id: str, user_id: str | None) -> None:
    try:
        now = datetime.now(timezone.utc)
        st = _live.get(tenant_id)
        if st is None:
            # Cold start (new worker): adopt a still-warm visit row rather than
            # splitting one browsing session across a deploy.
            row = db.latest_visit(tenant_id)
            if row and now - _parse(row["last_seen_at"]) < VISIT_GAP:
                st = _Live(row["id"], _parse(row["last_seen_at"]), now - WRITE_EVERY)
                # Remember it. Without this every request found no state, re-read the
                # latest visit and re-wrote it (3 Supabase calls, ~150 ms, under the
                # global lock) instead of the one throttled write a minute.
                _live[tenant_id] = st
        if st is None or now - st.last_seen >= VISIT_GAP:
            row = db.insert_visit(tenant_id, user_id)
            _live[tenant_id] = _Live(row["id"], now, now)
            return
        st.last_seen = now
        st.pending += 1
        if now - st.last_write >= WRITE_EVERY:
            _writer.submit(_bump, st.visit_id, now.isoformat(), st.pending)
            st.last_write, st.pending = now, 0
    except Exception:
        log.warning("visit touch failed for tenant %s", tenant_id, exc_info=True)


# ── operator read ────────────────────────────────────────────────────────────
def _visit_rollup(tenant_id: str) -> dict:
    """Totals over recent visits (newest-first rows from db.list_recent_visits)."""
    visits = db.list_recent_visits(tenant_id)
    minutes = 0.0
    days = set()
    for v in visits:
        start, end = _parse(v["started_at"]), _parse(v["last_seen_at"])
        # A one-request visit spans 0 minutes; call it 1 so the total is honest
        # about the visit having happened at all.
        minutes += max((end - start).total_seconds() / 60, 1.0)
        days.add(start.date())
    return {
        "count": len(visits),
        "minutes": round(minutes),
        "active_days": len(days),
        "first_seen": _parse(visits[-1]["started_at"]) if visits else None,
        "last_seen": _parse(visits[0]["last_seen_at"]) if visits else None,
    }


def funnel(tenant: dict, mailbox_count: int, cfg: dict, kb_chunks: int) -> dict:
    """The wizard funnel as data: every step done/not, plus where they stopped.
    Step order mirrors src/app/onboarding.py STEPS. Voice and knowledge are optional
    (skippable) steps — they show as done/skipped but never mark a tenant
    'stalled': only the steps that gate the dashboard can stall someone."""
    steps = [
        {"id": "interview", "label": "Interview / industry",
         "done": bool(tenant.get("industry")), "optional": False,
         "detail": tenant.get("industry") or "not chosen"},
        {"id": "mailbox", "label": "Mailbox connected",
         "done": mailbox_count > 0, "optional": False,
         "detail": f"{mailbox_count} connected" if mailbox_count else "none"},
        {"id": "voice", "label": "Voice / tone",
         "done": bool(cfg.get("voice_profile") or cfg.get("tone")), "optional": True,
         "detail": cfg.get("voice_profile_source") or ("set" if cfg.get("tone") else "not set")},
        {"id": "knowledge", "label": "Knowledge added",
         "done": kb_chunks > 0, "optional": True,
         "detail": f"{kb_chunks} chunks" if kb_chunks else "empty"},
    ]
    onboarded = bool(tenant.get("onboarded_at"))
    stalled_at = None
    if not onboarded:
        required = [s for s in steps if not s["optional"] and not s["done"]]
        stalled_at = required[0]["id"] if required else "finish"
    return {"steps": steps, "onboarded": onboarded, "stalled_at": stalled_at}


def stalled_step(tenant: dict) -> str | None:
    """Overview-list flag: where an un-onboarded tenant is stuck (cheap — only
    queries when onboarded_at is empty, which is the rare case)."""
    if tenant.get("onboarded_at"):
        return None
    if not tenant.get("industry"):
        return "interview"
    if not db.list_mailboxes(tenant["id"]):
        return "mailbox"
    return "finish"


def tenant_journey(tenant_id: str) -> dict:
    """Everything the operator tenant page's Journey card shows. Each block is
    best-effort — an auth-API hiccup blanks the owner line, not the page."""
    tenant = db.get_tenant(tenant_id) or {}
    cfg = {}
    try:
        cfg = db.get_tenant_config(tenant_id) or {}
    except Exception:
        log.warning("journey: tenant_config read failed", exc_info=True)
    mailboxes = []
    try:
        mailboxes = db.list_mailboxes(tenant_id)
    except Exception:
        log.warning("journey: mailbox read failed", exc_info=True)

    counts = {}
    for key, table in [("knowledge_chunks", "knowledge_chunks"),
                       ("crm_contacts", "crm_contacts"),
                       ("bookings", "bookings")]:
        try:
            counts[key] = db.count_tenant_rows(table, tenant_id)
        except Exception:
            counts[key] = None

    owner = None
    owner_id = db.tenant_owner_user_id(tenant_id)
    if owner_id:
        owner = db.get_auth_user(owner_id)

    signed_up = _parse(tenant["created_at"]) if tenant.get("created_at") else None
    return {
        "signed_up_at": signed_up,
        "days_on_platform": (datetime.now(timezone.utc) - signed_up).days if signed_up else None,
        "owner": owner,
        "last_sign_in_at": _parse(owner["last_sign_in_at"]) if owner and owner.get("last_sign_in_at") else None,
        "funnel": funnel(tenant, len(mailboxes), cfg, counts.get("knowledge_chunks") or 0),
        "onboarded_at": _parse(tenant["onboarded_at"]) if tenant.get("onboarded_at") else None,
        "mailboxes": [{"address": m.get("address"), "provider": m.get("provider")} for m in mailboxes],
        "visits": _visit_rollup(tenant_id),
        "counts": counts,
    }
