"""Calendar Connect (v1.1, Outlook first) — push + continuous busy-sync.

Two one-way flows, no two-way conflict resolution:
  PUSH  — a booking that reaches 'confirmed' becomes a real event in the
          tenant's calendar; cancelling deletes it. BLAM8 is the source of
          truth for bookings.
  PULL  — the tenant's busy calendar events become source='calendar' blocks
          that consume diary capacity (scheduling.plan_calendar_sync decides;
          this module just applies the plan). The tenant removes a block by
          cancelling it in the Diary — the cancelled row is a tombstone the
          sync never resurrects.

Everything here is BEST-EFFORT: a calendar outage must never break drafting,
approving, or the Diary. Errors are logged and swallowed.
"""
import logging
from datetime import datetime, time, timedelta

import httpx

from app import db, gmail, graph, health, mailbox, member_calendar, people, scheduling, sync_backoff
from app import slotgrid
from app.scheduling import DIARY_TZ

log = logging.getLogger(__name__)


def _backend(provider: str):
    """graph for Microsoft, gmail for Google. Both expose the same
    list/create/delete calendar functions, so everything below is
    provider-agnostic."""
    return gmail if provider == "google" else graph


async def _calendar_client(tenant_id: str) -> tuple[str, str, object] | None:
    """(live token, provider, backend module) for the tenant's calendar — the
    oldest calendar-connected mailbox (the diary stays tenant-level in
    enterprise v1, so one calendar represents the tenant). None when no mailbox
    has calendar scope. Provider comes from the CONNECTION (enterprise D1)."""
    conn = db.get_calendar_connection(tenant_id)
    if not conn:
        return None
    provider = mailbox.connection_provider(conn, tenant_id)
    token = await mailbox.valid_access_token(conn, provider)
    return token, provider, _backend(provider)


def _event_times(cfg_row: dict | None, booking: dict) -> tuple[str, str, bool]:
    """(start_iso, end_iso, all_day) London wall-times for a booking's window.
    Slots use their real start + slot length; am/pm map to opening-hours halves;
    'day' becomes an all-day event."""
    c = scheduling._norm(cfg_row)
    merged = scheduling._merged(cfg_row)
    day = scheduling._as_date(booking["booking_date"])
    window = booking["window"]
    if ":" in window:
        start = scheduling._window_start(day, window)
        # A service/stay row knows its own length (length_blocks on the 15-min
        # grid); only a plain slot falls back to the diary's slot size. Live
        # 2026-09-03: a 45-minute consultation pushed as a 30-minute event.
        blocks = booking.get("length_blocks")
        minutes = (int(blocks) * slotgrid.BLOCK_MIN if blocks else c["slot_minutes"])
        end = start + timedelta(minutes=minutes)
    elif window in ("am", "pm"):
        open_t = scheduling._as_time(merged["open_time"])
        close_t = scheduling._as_time(merged["close_time"])
        noon = time(12, 0)
        start_t, end_t = (open_t, noon) if window == "am" else (noon, close_t)
        start = datetime.combine(day, start_t, tzinfo=DIARY_TZ)
        end = datetime.combine(day, end_t, tzinfo=DIARY_TZ)
    else:  # whole day → all-day event (end exclusive midnight)
        start = datetime.combine(day, time(0, 0), tzinfo=DIARY_TZ)
        end = start + timedelta(days=1)
        return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), True
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), False


def _summary(b: dict) -> str:
    # Fleet bookings carry their size — the event summary gains the load
    # (D6: "6 plt · 6 load units, M17 -> LS9").
    if b.get("spaces"):
        return f"{scheduling.fmt_spaces(b['spaces'])} plt · {b['summary']}"
    return b["summary"]


async def push_bookings(tenant_id: str, email_log_id: str | None = None,
                        booking_id: str | None = None) -> None:
    """Put confirmed bookings in the calendars that should hold them: the
    business's own, and the calendar of each person the work is delegated to.
    Scoped by draft (approve path) or single booking (Diary paths).

    The two are independent on purpose — a business with no calendar connected
    still fills its people's, and one person's dead grant never stops the rest.
    """
    await _push_business(tenant_id, email_log_id, booking_id)
    await push_to_people(tenant_id, email_log_id=email_log_id, booking_id=booking_id)


async def _push_business(tenant_id: str, email_log_id: str | None = None,
                         booking_id: str | None = None) -> None:
    """The one tenant calendar — bookings that don't have an event yet."""
    try:
        client = await _calendar_client(tenant_id)
        if not client:
            return
        token, _provider, backend = client
        cfg_row = db.get_scheduling_config(tenant_id)
        for b in db.list_bookings_to_push(tenant_id, email_log_id=email_log_id,
                                          booking_id=booking_id):
            start_iso, end_iso, all_day = _event_times(cfg_row, b)
            body = "Booked via BLAM8" + (f" — {b['contact']}" if b.get("contact") else "")
            event_id = await backend.create_calendar_event(
                token, _summary(b), start_iso, end_iso, all_day=all_day, body_text=body)
            db.set_booking_calendar_event(b["id"], event_id, tenant_id)
    except Exception:
        log.warning("calendar push failed for tenant %s", tenant_id, exc_info=True)


# ── a person's own calendar (People slice 5) ───────────────────────────────
async def push_to_people(tenant_id: str, email_log_id: str | None = None,
                         booking_id: str | None = None, user_id: str | None = None) -> None:
    """Put the work delegated to each person into the calendar they connected.

    Only kinds actually ticked for them travel (people.does_booking) — an empty
    delegation writes nothing, because a calendar is somebody's own property.
    Only today onwards travels: nobody wants last year's subjects appearing in
    their phone. `user_id` fills one newly connected calendar with what is
    already on the books."""
    try:
        cals = ([db.get_member_calendar(tenant_id, user_id)] if user_id
                else db.list_member_calendars(tenant_id))
        cals = [c for c in cals if c]
        if not cals:
            return
        who = {m.get("user_id"): m
               for m in (people.member(r) for r in db.list_members(tenant_id))}
        cfg_row = db.get_scheduling_config(tenant_id)
        today = datetime.now(DIARY_TZ).date().isoformat()
        rows = db.list_confirmed_bookings(tenant_id, email_log_id=email_log_id,
                                          booking_id=booking_id, from_date=today)
        if not rows:
            return
        from app import job_people
        job_people.annotate(tenant_id, rows)     # a job they're ON is theirs too
        for cal in cals:
            await _push_one_person(tenant_id, cal, who.get(cal["user_id"]), rows, cfg_row)
    except Exception:
        log.warning("people calendar push failed for tenant %s", tenant_id, exc_info=True)


async def _push_one_person(tenant_id: str, cal: dict, member: dict | None,
                           rows: list[dict], cfg_row: dict | None) -> None:
    """One person's share. A failure is written against THEM — their own page
    says why their calendar went quiet — and never raised at the diary."""
    mine = [b for b in rows if people.does_booking(member, b, cfg_row)]
    if not mine:
        return
    already = {e["booking_id"]
               for e in db.list_member_booking_events(tenant_id, user_id=cal["user_id"])}
    mine = [b for b in mine if b["id"] not in already]
    if not mine:
        return
    backend = member_calendar.backend(cal.get("provider"))
    try:
        token = await member_calendar.live_token(cal)
        for b in mine:
            start_iso, end_iso, all_day = _event_times(cfg_row, b)
            body = "Your work, from BLAM8" + (f" — {b['contact']}" if b.get("contact") else "")
            event_id = await backend.create_calendar_event(
                token, _summary(b), start_iso, end_iso, all_day=all_day, body_text=body)
            db.set_member_booking_event(tenant_id, cal["user_id"], b["id"], event_id)
        db.set_member_calendar_error(tenant_id, cal["user_id"], None)
    except Exception as exc:
        try:
            db.set_member_calendar_error(tenant_id, cal["user_id"],
                                         member_calendar.why_it_stopped(exc))
        except Exception:
            pass
        log.warning("member calendar push failed (tenant=%s user=%s)",
                    tenant_id, cal.get("user_id"), exc_info=True)


async def remove_pushed_event(tenant_id: str, booking_id: str) -> None:
    """Booking cancelled/declined in the Diary → take its event back out of
    every calendar BLAM8 put it in: the business's, and each person's."""
    try:
        b = db.get_booking(booking_id, tenant_id)
        if not b or b.get("source") == "calendar":
            return
        if b.get("calendar_event_id"):
            client = await _calendar_client(tenant_id)
            if client:
                token, _provider, backend = client
                await backend.delete_calendar_event(token, b["calendar_event_id"])
                db.set_booking_calendar_event(booking_id, None, tenant_id)
    except Exception:
        log.warning("calendar event removal failed for booking %s", booking_id, exc_info=True)
    await remove_from_people(tenant_id, booking_id)


async def remove_from_people(tenant_id: str, booking_id: str | None = None,
                             user_id: str | None = None) -> None:
    """Take BLAM8's events back out of people's own calendars — one booking
    that was cancelled, or everything belonging to one person who has
    disconnected. A delete that fails KEEPS its row, so a repaired grant still
    clears it and nothing is ever written twice."""
    try:
        rows = db.list_member_booking_events(tenant_id, booking_id=booking_id, user_id=user_id)
    except Exception:
        log.warning("member calendar events read failed (tenant=%s)", tenant_id, exc_info=True)
        return
    cache: dict[str, dict | None] = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in cache:
            try:
                cache[uid] = db.get_member_calendar(tenant_id, uid)
            except Exception:
                cache[uid] = None
        cal = cache[uid]
        try:
            if cal:
                token = await member_calendar.live_token(cal)
                await member_calendar.backend(cal.get("provider")).delete_calendar_event(
                    token, r["event_id"])
            db.delete_member_booking_event(tenant_id, uid, r["booking_id"])
        except Exception:
            log.warning("member calendar removal failed (tenant=%s user=%s)",
                        tenant_id, uid, exc_info=True)


async def sync_connection(conn: dict) -> dict | None:
    """One busy-sync sweep for one connection: list busy events over the booking
    horizon, reconcile against linked bookings, apply the plan."""
    tenant_id = conn["tenant_id"]
    mods = db.active_modules(tenant_id)
    if "scheduling" not in mods and "tms" not in mods:
        return None
    cfg_row = db.get_scheduling_config(tenant_id)
    now = datetime.now(DIARY_TZ)
    horizon = scheduling._norm(cfg_row)["horizon_days"]
    provider = mailbox.connection_provider(conn, tenant_id)
    backend = _backend(provider)
    token = await mailbox.valid_access_token(conn, provider)
    start = datetime.combine(now.date(), time(0, 0), tzinfo=DIARY_TZ)
    events = await backend.list_calendar_events(
        token, start, start + timedelta(days=horizon + 1))
    rows = db.list_calendar_linked_bookings(tenant_id)
    plan = scheduling.plan_calendar_sync(cfg_row, rows, events, now)
    for item in plan["add"]:
        db.insert_calendar_block(tenant_id, item["event_id"], item["summary"],
                                 item["date"], item["window"])
    for bid in plan["cancel"]:
        db.set_booking_status(bid, "cancelled", tenant_id)
    # Hand-deleted events behind PUSHED bookings: chip on, chip off (the
    # booking itself is never touched — see plan_calendar_sync).
    for bid in plan.get("flag_missing", []):
        db.set_booking_calendar_missing(bid, tenant_id, True)
    for bid in plan.get("unflag_missing", []):
        db.set_booking_calendar_missing(bid, tenant_id, False)
    return {"added": len(plan["add"]), "cancelled": len(plan["cancel"]),
            "flagged": len(plan.get("flag_missing", []))}


async def sync_all() -> None:
    """One sweep over every calendar-connected mailbox (the poller body).

    A connection that keeps failing — a dead/expired refresh token (400 on the
    token endpoint), a missing calendar grant or a disabled Calendar API (401/403)
    — is logged ONCE and then skipped with exponential backoff. It is never
    retried-and-tracebacked every sweep, which is what OOM-looped the box (see
    src/app/sync_backoff.py). A recovered grant resumes on the first sweep after the
    backoff window, no restart needed."""
    for conn in db.list_calendar_connections():
        cid = conn.get("id")
        key = ("calendar", cid)
        if sync_backoff.should_skip(key):
            continue
        try:
            result = await sync_connection(conn)
            sync_backoff.record_success(key)   # a working grant resumes silently
            health.record_success(cid, "calendar")
            if result and (result["added"] or result["cancelled"]):
                log.info("calendar sync tenant %s: +%s blocks, -%s",
                         conn["tenant_id"], result["added"], result["cancelled"])
        except httpx.HTTPStatusError as e:
            health.record_failure(
                cid, "calendar", "%s from %s" % (e.response.status_code,
                                                 e.request.url.host))
            if sync_backoff.record_failure(key):
                log.warning(
                    "calendar sync: connection %s (%s) failing — %s from %s. The calendar "
                    "grant is missing/expired or the provider's Calendar API is disabled. "
                    "Skipping with backoff; reconnect the mailbox to re-consent, or enable "
                    "the Calendar API in the provider console. Resumes automatically once fixed.",
                    cid, conn.get("address") or "?", e.response.status_code, e.request.url.host)
        except Exception as exc:
            health.record_failure(cid, "calendar", repr(exc)[:200])
            if sync_backoff.record_failure(key):
                log.warning("calendar sync failed for connection %s; skipping with backoff",
                            cid, exc_info=True)
