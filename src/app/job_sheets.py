"""Job sheets — the job goes to the person; they don't come to the app.

Kris, 2026-09-25, rehearsing the plumber film: "if they never touch the app,
how do they get the info to do the job? See the flaw." A person on the team
who doesn't sign in could be put on a job (src/app/job_people.py) and then heard
nothing — the owner still had to ring them. Now anyone on a job WITHOUT a
login but WITH a mobile or an email is sent the job sheet:

  new        "Job for you" — when, what, where, who's there, and a private
             link to the job page (/j/{token}, src/app/job_sheet_public.py) with
             a map, tap-to-call, On my way and Done.
  changed    the time, the place or the job moved — the new details.
  off        they were taken off it — no need to go.
  cancelled  the booking was cancelled — no need to go.

By text when the business's SMS number is live (the more use the SMS channel
gets, the better — Kris), else by email from the business's own mailbox.
WhatsApp is the next channel; `deliver` is the one place that chooses.

The words are written here, not by a model: a job sheet is facts — a time, an
address, a phone number — and a fact the model rephrased is a fact at risk.

`reconcile(booking)` is the whole contract: it compares who is on the job and
what the job says with the sheets already sent, and sends the difference.
Every path that puts someone on a job, moves it or cancels it calls it (the
job page, the Concierge's card, the cancel) and the five-minute sweep catches
anything that didn't — so a new path is covered without knowing this exists.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from app import db, job_people, people
from app.config import settings

log = logging.getLogger(__name__)

NEW, CHANGED, OFF, CANCELLED = "new", "changed", "off", "cancelled"

# A sheet goes out for a booking the customer has agreed to. An offer the
# customer hasn't taken up yet is not a job to turn up to; the sheet follows
# the moment it is confirmed (the sweep sees the confirmation).
LIVE = ("confirmed",)

# The sweep's net for assignments made anywhere, and the day it started: a
# person already on an upcoming job before this shipped is not suddenly
# messaged by a deploy.
NET_DAYS = 30
STARTED = "2026-09-25T18:00:00+00:00"

_WHERE = re.compile(r"\b(address|postcode|post code|location|site)\b", re.IGNORECASE)
_SKIP_NOTE = re.compile(r"\b(service|name|email|phone|mobile|contact)\b", re.IGNORECASE)


# ── The facts ────────────────────────────────────────────────────────────────
def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def when_text(b: dict, cfg: dict | None = None) -> str:
    """'Mon 28 Sep, 08:00–10:00' / 'Mon 28 Sep, morning (arrive 08:00–12:00)'
    / a stay's two ends. With the diary's config, an arrival-window job reads
    as its window — the customer was told "morning", so the person going
    must not read the first two hours of it (Kris 2026-09-26: BK-1003's sheet
    said 08:00–10:00)."""
    from app import record_edits, slotgrid
    try:
        st = record_edits.stay_times(b)
    except Exception:
        st = None
    if st:
        return f"{_day(st[0].date())} {st[0]:%H:%M} → {_day(st[1].date())} {st[1]:%H:%M}"
    try:
        d = date.fromisoformat(str(b.get("booking_date"))[:10])
    except (TypeError, ValueError):
        return "date to be confirmed"
    out = _day(d)
    w = str(b.get("window") or "").strip()
    if w == "eve":
        return f"{out}, evening (out of hours)"
    if cfg is not None and w in ("am", "pm", "day"):
        try:
            from app import scheduling
            lo, hi = scheduling.window_bounds(cfg, d, w)
            word = {"am": "morning", "pm": "afternoon", "day": "all day"}[w]
            return f"{out}, {word} (arrive {slotgrid.hhmm(lo)}–{slotgrid.hhmm(hi)})"
        except Exception:  # noqa: BLE001 — fall back to the booked block
            pass
    try:
        if b.get("start_block") is not None:
            start = int(b["start_block"])
            out += f", {slotgrid.hhmm(start)}"
            if b.get("length_blocks"):
                end = start + int(b["length_blocks"])
                if end <= 96:
                    out += f"–{slotgrid.hhmm(end)}"
            return out
    except (TypeError, ValueError):
        pass
    w = str(b.get("window") or "").strip()
    words = {"am": "morning", "pm": "afternoon", "day": "all day"}
    return out + (f", {words.get(w, w)}" if w else "")


def _details(b: dict) -> dict:
    d = b.get("details")
    return d if isinstance(d, dict) else {}


def where_text(b: dict) -> str:
    """The address the job is at — the booking's own address-shaped details,
    else its postcodes. '' when the customer comes to the business."""
    parts = [str(v).strip() for k, v in _details(b).items()
             if _WHERE.search(str(k)) and str(v or "").strip()]
    if not parts:
        parts = [str(b.get(k)).strip() for k in ("delivery_postcode", "collection_postcode")
                 if b.get(k)]
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def what_text(b: dict) -> str:
    return (str(_details(b).get("Service") or "").strip()
            or " ".join(str(b.get("summary") or "").split())[:120] or "Job")


def notes(b: dict) -> list[str]:
    """The rest of what was asked when booking — the boiler's make, the dog's
    name — minus what the sheet already says and the customer's contact
    details (those sit with who's there)."""
    out = []
    for k, v in _details(b).items():
        v = " ".join(str(v or "").split())
        if not v or _WHERE.search(str(k)) or _SKIP_NOTE.search(str(k)):
            continue
        k = str(k).strip()
        out.append(f"{k} {v[:160]}" if k.endswith("?") else f"{k}: {v[:160]}")
    return out[:8]


def who_there(tenant_id: str, b: dict) -> dict:
    """Who they'll meet: the on-site party if the owner named one (the
    tenant, the housekeeper), else the customer — name and phone."""
    from app import job_parties
    try:
        parties = db.list_job_parties(tenant_id, b["id"])
    except Exception:
        parties = []
    who = dict(job_parties.resolve(b, parties)["on_site"])
    if who.get("is_fallback") and b.get("contact"):
        try:
            c = db.crm_contact_by_email(tenant_id, b["contact"]) or {}
        except Exception:
            c = {}
        who["name"] = who.get("name") or (c.get("name") or "").strip() or None
        who["phone"] = (who.get("phone") or (c.get("phone") or "").strip()
                        or str((c.get("custom_data") or {}).get("phone") or "").strip() or None)
    return {"name": who.get("name"), "phone": who.get("phone"), "label": who.get("label")}


def facts(tenant_id: str, b: dict) -> dict:
    try:
        cfg = db.get_scheduling_config(tenant_id) or {}
    except Exception:
        cfg = None
    # `when_key` is what a change is judged by: the booking's own slot, so
    # how the time is WORDED can improve without telling anyone it moved.
    return {"ref": b.get("booking_ref") or "", "what": what_text(b), "when": when_text(b, cfg),
            "when_key": when_text(b),
            "where": where_text(b), "who": who_there(tenant_id, b), "notes": notes(b)}


def fingerprint(f: dict) -> str:
    """What makes a sheet out of date: the time, the place, the job."""
    raw = "|".join((f.get("when_key") or f.get("when") or "", f.get("where") or "", f.get("what") or ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def ended(b: dict, today: date | None = None) -> bool:
    """Past the job's last day — its sheet is history, not news."""
    from app import record_edits
    from app.scheduling import DIARY_TZ
    today = today or datetime.now(DIARY_TZ).date()
    try:
        st = record_edits.stay_times(b)
    except Exception:
        st = None
    try:
        last = st[1].date() if st else date.fromisoformat(str(b.get("booking_date"))[:10])
    except (TypeError, ValueError):
        return False
    return last < today


def link(token: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/j/{token}"


# ── The words ────────────────────────────────────────────────────────────────
def first_name(name: str) -> str:
    return (name or "").split(" ")[0] or "there"


def compose(kind: str, biz: str, person: dict, f: dict, url: str) -> dict:
    """{subject, sms, email} — plain words, facts exactly as the diary has
    them. The text fits two SMS segments for a normal job."""
    ref = f" ({f['ref']})" if f.get("ref") else ""
    where = f.get("where") or ""
    who = f.get("who") or {}
    hi = first_name(person.get("name", ""))
    if kind == NEW:
        subject = f"New job: {f['when']} — {f['what']}{ref}"
        sms = (f"{biz}: job for you, {hi}. {f['when']} — {f['what']}"
               + (f", {where}" if where else "") + f". Details + On my way: {url}")
        head = f"Hi {hi}, you're on this job for {biz}."
    elif kind == CHANGED:
        subject = f"Changed: {f['when']} — {f['what']}{ref}"
        sms = (f"{biz}: CHANGED{ref} — now {f['when']}, {f['what']}"
               + (f", {where}" if where else "") + f". {url}")
        head = f"Hi {hi}, this job has changed — the details below are the new ones."
    elif kind == OFF:
        subject = f"You're off: {f['when']} — {f['what']}{ref}"
        sms = f"{biz}: you're off{ref} {f['when']}, {f['what']} — no need to go."
        head = f"Hi {hi}, you've been taken off this job — no need to go."
    else:
        subject = f"Cancelled: {f['when']} — {f['what']}{ref}"
        sms = f"{biz}: CANCELLED{ref} {f['when']}, {f['what']} — no need to go."
        head = f"Hi {hi}, this job has been cancelled — no need to go."
    # One space after each label: a phone's mail app reads proportional type,
    # so padded columns come out ragged (the first render, 2026-09-25).
    lines = [head, "", f"When: {f['when']}", f"Job: {f['what']}"]
    if where:
        lines.append(f"Where: {where}")
    if kind in (NEW, CHANGED):
        if who.get("name") or who.get("phone"):
            lines.append("Who: " + " · ".join(x for x in (
                who.get("name"), (f"({who['label']})" if who.get("label") else None),
                who.get("phone")) if x))
        if f.get("ref"):
            lines.append(f"Ref: {f['ref']}")
        if f.get("notes"):
            lines += ["", "Notes:"] + [f"- {n}" for n in f["notes"]]
        lines += ["", "Open the job — map, call, On my way and Done:", url]
    lines += ["", f"— {biz}"]
    return {"subject": subject, "sms": sms, "email": "\n".join(lines)}


# ── Sending ──────────────────────────────────────────────────────────────────
def reachable(person: dict) -> bool:
    """Someone the sheet can reach: no login (a login sees the job in the
    app), still on the team, and a mobile or an email to send to."""
    return bool(person and person.get("active") and not person.get("has_login")
                and (person.get("phone") or person.get("email")))


def _sms_live(tenant_id: str) -> bool:
    from app import sms
    try:
        return sms.configured() and bool(db.sms_connection(tenant_id))
    except Exception:
        return False


async def deliver(tenant_id: str, person: dict, msg: dict,
                  mailbox_id: str | None = None) -> tuple[str, str] | None:
    """Send one message the best way the person can get it: a text when the
    business's SMS number is live, else an email. (channel, to) or None."""
    from app import mailbox, sms
    phone = sms.normalize_e164(person.get("phone"))
    if phone and _sms_live(tenant_id):
        try:
            await sms.send_sms(tenant_id, phone, msg["sms"])
            return ("sms", phone)
        except Exception:
            log.warning("job sheet text failed (tenant=%s) — trying email", tenant_id, exc_info=True)
    email = (person.get("email") or "").strip()
    if email:
        await mailbox.send_mail(tenant_id, email, msg["subject"], msg["email"], mailbox_id=mailbox_id)
        return ("email", email)
    return None


def _biz_name(tenant_id: str) -> str:
    try:
        inv = db.get_invoice_settings(tenant_id) or {}
        if (inv.get("business_name") or "").strip():
            return inv["business_name"].strip()
    except Exception:
        pass
    try:
        return ((db.get_tenant(tenant_id) or {}).get("name") or "").strip() or "The office"
    except Exception:
        return "The office"


_WORD = {NEW: "the job sheet", CHANGED: "the new details", OFF: "that they're off",
         CANCELLED: "that it's cancelled"}
_BY = {"sms": "by text", "email": "by email"}


async def _send(tenant_id: str, b: dict, person: dict, kind: str, f: dict, token: str,
                biz: str) -> tuple[str, str] | None:
    msg = compose(kind, biz, person, f, link(token))
    sent = await deliver(tenant_id, person, msg, mailbox_id=b.get("mailbox_id"))
    if sent:
        db.log_agent_event(
            tenant_id, "job_sheet",
            f"Sent {person['name']} {_WORD[kind]} for {f.get('ref') or f.get('what')} {_BY[sent[0]]}",
            direction="outbound", contact=sent[1], subject=msg["subject"],
            booking_id=b.get("id"), booking_ref=b.get("booking_ref"),
            event_date=str(b.get("booking_date") or "")[:10] or None,
            detail={"kind": kind, "channel": sent[0], "person_id": person["id"]})
    return sent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def reconcile(tenant_id: str, booking_id: str) -> list[str]:
    """Bring the sheets for one booking in line with the booking. Returns what
    was sent ('new:Jake', 'changed:Jake'…) — for tests and the log."""
    b = db.get_booking(booking_id, tenant_id)
    roster = {t["id"]: t for t in job_people.team(tenant_id)}
    # A person who has left the team is still told a job they had is off.
    try:
        everyone = {t["id"]: t for t in (people.team_person(r) for r in db.list_team_people(tenant_id))}
    except Exception:
        everyone = dict(roster)
    sheets = {str(s["person_id"]): s for s in db.list_job_sheets(tenant_id, [booking_id])}
    on = {p["person_id"] for p in job_people.by_booking(tenant_id, [booking_id], list(everyone.values()))
          .get(str(booking_id), [])} if b else set()
    done: list[str] = []
    if not b:
        for s in sheets.values():
            if not s.get("closed_at"):
                db.update_job_sheet(s["id"], {"closed_at": _now()})
        return done
    over = ended(b)
    biz = None
    f = None
    for pid, s in sheets.items():
        if s.get("closed_at") or not s.get("sent_at"):
            continue
        if over:
            db.update_job_sheet(s["id"], {"closed_at": _now()})
            continue
        person = everyone.get(pid)
        f = f or facts(tenant_id, b)
        biz = biz or _biz_name(tenant_id)
        kind = None
        if b.get("status") == "cancelled":
            kind = CANCELLED
        elif pid not in on or not person or not person.get("active"):
            kind = OFF
        elif fingerprint(f) != s.get("facts_hash"):
            kind = CHANGED
        if not kind:
            continue
        sent = await _send(tenant_id, b, person or {"id": pid, "name": "there"}, kind, f,
                           s["token"], biz) if person and reachable({**person, "has_login": False}) else None
        fields = {"last_notice": kind, "last_notice_at": _now()}
        if kind in (OFF, CANCELLED):
            fields["closed_at"] = _now()
        else:
            fields["facts_hash"] = fingerprint(f)
        db.update_job_sheet(s["id"], fields)
        done.append(f"{kind}:{(person or {}).get('name', pid)}")
    if over or b.get("status") not in LIVE:
        return done
    for pid in on:
        person = roster.get(pid)
        s = sheets.get(pid)
        if not reachable(person) or (s and s.get("sent_at") and not s.get("closed_at")):
            continue
        f = f or facts(tenant_id, b)
        biz = biz or _biz_name(tenant_id)
        token = secrets.token_urlsafe(24)
        try:
            sent = await _send(tenant_id, b, person, NEW, f, token, biz)
        except Exception:
            log.warning("job sheet send failed (tenant=%s, booking=%s, person=%s)",
                        tenant_id, booking_id, pid, exc_info=True)
            continue
        if not sent:
            continue
        db.insert_job_sheet({"tenant_id": tenant_id, "booking_id": booking_id, "person_id": pid,
                             "token": token, "channel": sent[0], "sent_to": sent[1],
                             "sent_at": _now(), "facts_hash": fingerprint(f),
                             "last_notice": NEW, "last_notice_at": _now(),
                             "opened_at": None, "on_my_way_at": None, "done_at": None,
                             "closed_at": None})
        done.append(f"new:{person['name']}")
    return done


_TASKS: set = set()


def soon(tenant_id: str, booking_id: str) -> None:
    """Reconcile in the background — the route that changed the job answers
    now; the text or email follows a moment later. Never raises."""
    async def _run():
        try:
            await reconcile(tenant_id, booking_id)
        except Exception:
            log.warning("job sheet reconcile failed (tenant=%s, booking=%s)",
                        tenant_id, booking_id, exc_info=True)
    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def sweep() -> int:
    """Every open sheet, and every job someone was put on lately: reconcile
    each. The net under every path that didn't call soon()."""
    since = max(STARTED, (datetime.now(timezone.utc) - timedelta(days=NET_DAYS)).isoformat())
    pairs = set(db.open_job_sheet_booking_ids()) | set(db.recent_booking_people(since))
    n = 0
    for tenant_id, booking_id in sorted(pairs):
        try:
            n += len(await reconcile(tenant_id, booking_id))
        except Exception:
            log.warning("job sheet sweep failed (tenant=%s, booking=%s)", tenant_id, booking_id,
                        exc_info=True)
    return n


# ── What the owner sees on the job page ──────────────────────────────────────
def _hhmm(ts) -> str:
    from app.scheduling import DIARY_TZ
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(DIARY_TZ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def status_lines(tenant_id: str, b: dict, on_it: list[dict],
                 roster: list[dict] | None = None) -> dict[str, str]:
    """{person_id: 'sheet sent by text 08:02 · opened · on the way 09:40'} for
    the people on the job who don't sign in — or why nothing was sent."""
    try:
        sheets = {str(s["person_id"]): s for s in db.list_job_sheets(tenant_id, [b["id"]])}
    except Exception:
        sheets = {}
    who = {t["id"]: t for t in (roster if roster is not None else job_people.team(tenant_id))}
    out = {}
    for p in on_it:
        if p.get("has_login"):
            continue
        s = sheets.get(p["person_id"])
        if s and s.get("sent_at") and not s.get("closed_at"):
            bits = [f"sheet sent {_BY.get(s.get('channel'), '')} {_hhmm(s['sent_at'])}".replace("  ", " ")]
            if s.get("last_notice") == CHANGED:
                bits.append(f"changes sent {_hhmm(s.get('last_notice_at'))}")
            if s.get("opened_at"):
                bits.append("opened")
            if s.get("on_my_way_at"):
                bits.append(f"on the way {_hhmm(s['on_my_way_at'])}")
            if s.get("done_at"):
                bits.append(f"done {_hhmm(s['done_at'])}")
            out[p["person_id"]] = " · ".join(bits)
        elif not reachable(who.get(p["person_id"])):
            out[p["person_id"]] = "no mobile or email — add one in People and they're sent each job"
        elif b.get("status") in LIVE:
            out[p["person_id"]] = "sending the job sheet…"
        else:
            out[p["person_id"]] = "the job sheet goes out once the booking is confirmed"
    return out
