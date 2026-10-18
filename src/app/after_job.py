"""After the job — tasks a service leaves for the business once the job is done
(docs/plan-after-job-tasks.md, Kris 2026-09-26).

A service can carry up to five "after the job" tasks, each with a deadline in
calendar or working days counted from the moment the job is done. When a job
is done its tasks land in crm_tasks (source 'after_job', one per booking and
task), reach Today as they come due, and the drafting agent can tell a
customer where one has got to.

AGNOSTIC BY RULE (Kris 2026-09-26, a hard rule): nothing in this module knows a
trade. The task titles are the business's own words from its own settings; the
wording here — "after the job", "job done", "task", "deadline" — reads the same
for any service business. Trade examples live only in
src/app/setups.py presets.

"Job done" is a STAMP (bookings.done_at), not a status: capacity, reminders,
balances, recalls and every other reader of bookings.status are untouched.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

MAX_TASKS = 5
MAX_DAYS = 400
UNITS = ("calendar", "working")
SOURCE = "after_job"
DUE_SOON_DAYS = 3
KIND = "after_job_due"
DONE_WHEN = ("marked", "day_end")      # business-wide: who says a job is done

_LINE = re.compile(r"^(?P<title>.+?)[\s,:\-–—]+(?:within\s+|in\s+)?(?P<days>\d{1,3})\s*"
                   r"(?P<working>working\s+)?days?\s*(?:after)?\s*$", re.IGNORECASE)


# ── settings: a service's tasks ─────────────────────────────────────────────
def clean_tasks(raw) -> list[dict]:
    """[{title, days, unit}] — from the saved list OR the settings text
    ("Send the report — 2 days; Register the warranty — 30 working days")."""
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[;\n]", raw) if p.strip()]
    out: list[dict] = []
    for r in raw or []:
        if isinstance(r, str):
            m = _LINE.match(" ".join(r.split()))
            if not m:
                continue
            r = {"title": m.group("title"), "days": m.group("days"),
                 "unit": "working" if m.group("working") else "calendar"}
        if not isinstance(r, dict):
            continue
        title = " ".join(str(r.get("title") or "").split())[:80].strip(" -–—,:")
        try:
            days = int(float(r.get("days")))
        except (TypeError, ValueError):
            continue
        if not title or not 1 <= days <= MAX_DAYS:
            continue
        unit = r.get("unit") if r.get("unit") in UNITS else "calendar"
        if any(t["title"].lower() == title.lower() for t in out):
            continue
        out.append({"title": title, "days": days, "unit": unit})
        if len(out) >= MAX_TASKS:
            break
    return out


def as_text(tasks) -> str:
    """The settings field's value for a service's tasks."""
    return "; ".join(f"{t['title']} — {t['days']} {'working ' if t['unit'] == 'working' else ''}"
                     f"day{'s' if t['days'] != 1 else ''}" for t in clean_tasks(tasks))


def clean_done_when(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in DONE_WHEN else "marked"


def tenant_uses(service_types) -> bool:
    """Does any service carry after-job tasks? (Nothing else changes when not.)"""
    return any((s or {}).get("after_job") for s in service_types or [])


# ── deadlines ───────────────────────────────────────────────────────────────
def due_date(done_on: date, days: int, unit: str, nation: str | None = None) -> date:
    """Calendar days, or working days (weekends and the business's own bank
    holidays skipped — src/app/bank_holidays.py, the same list the diary closes on)."""
    if unit != "working":
        return done_on + timedelta(days=days)
    try:
        from app import bank_holidays
        closed = set(bank_holidays.days_for(nation) or {})
    except Exception:  # noqa: BLE001 — no list: weekends only
        closed = set()
    d, left = done_on, days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in closed:
            left -= 1
    return d


def _pretty(d: date) -> str:
    from app.dates import pretty_date
    return pretty_date(d.isoformat())


def local_date(ts) -> str:
    """A stored UTC timestamp as the business's own date (ISO) — a job marked
    done at 00:45 BST is the 27th, not the 26th (rig 2026-09-27)."""
    from app.scheduling import DIARY_TZ
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(DIARY_TZ).date().isoformat()
    except (TypeError, ValueError):
        return str(ts or "")[:10]


# ── db (thin, so tests can swap them) ───────────────────────────────────────
def _db():
    from app import db
    return db.db()


def _claim_done(tenant_id: str, booking_id: str, by: str, at: str) -> dict | None:
    """Stamp done_at once — the first call wins, a repeat changes nothing."""
    rows = (_db().table("bookings").update({"done_at": at, "done_by": by[:80]})
            .eq("tenant_id", tenant_id).eq("id", booking_id).is_("done_at", "null")
            .execute().data or [])
    return rows[0] if rows else None


def _insert_task(row: dict) -> dict | None:
    try:
        res = _db().table("crm_tasks").insert(row).execute()
        return res.data[0] if res.data else None
    except Exception as e:  # noqa: BLE001 — the unique (booking, task) index: already there
        if "duplicate" in str(e).lower() or "23505" in str(e):
            return None
        raise


def _tasks_for_bookings(tenant_id: str, booking_ids: list[str]) -> list[dict]:
    if not booking_ids:
        return []
    return (_db().table("crm_tasks").select("*").eq("tenant_id", tenant_id)
            .eq("source", SOURCE).in_("booking_id", booking_ids)
            .order("due_date").execute().data or [])


# ── the moment a job is done ────────────────────────────────────────────────
def service_of(booking: dict, service_types) -> dict | None:
    from app import scheduling, services
    name = scheduling.booked_service({"details": booking.get("details") or {}})
    key = services._norm(name) if name else ""
    for s in services.clean_service_types(service_types):
        if key and services._norm(s["name"]) == key:
            return s
    return None


def mark_done(tenant_id: str, booking: dict, *, by: str = "owner",
              sched_cfg: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Stamp the job done (once) and create its service's after-job tasks.
    Returns the tasks created ([] when it was already done or the service has
    none). Best-effort callers wrap it; the stamp itself never blocks a page."""
    from app import db
    cfg = sched_cfg if sched_cfg is not None else (db.get_scheduling_config(tenant_id) or {})
    now = now or datetime.now(timezone.utc)
    if not _claim_done(tenant_id, booking["id"], by, now.isoformat()):
        return []
    svc = service_of(booking, cfg.get("service_types"))
    tasks = clean_tasks((svc or {}).get("after_job"))
    if not tasks:
        return []
    from app.scheduling import DIARY_TZ
    done_on = now.astimezone(DIARY_TZ).date()
    contact_id = None
    try:
        c = db.crm_contact_by_email(tenant_id, booking.get("contact")) if booking.get("contact") else None
        contact_id = (c or {}).get("id")
    except Exception:  # noqa: BLE001
        pass
    made = []
    for i, t in enumerate(tasks):
        due = due_date(done_on, t["days"], t["unit"], cfg.get("bank_holiday_nation"))
        row = _insert_task({
            "tenant_id": tenant_id, "title": t["title"], "due_date": due.isoformat(),
            "description": f"After the job — {booking.get('booking_ref') or 'booking'} "
                           f"({svc['name']}), done {_pretty(done_on)}",
            "priority": "normal", "source": SOURCE, "contact_id": contact_id,
            "booking_id": booking["id"], "task_key": f"{svc['name'].lower()}#{i}"})
        if row:
            made.append(row)
    return made


def complete(tenant_id: str, task_id: str, *, doc_id: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    fields = {"status": "done", "completed_at": now, "updated_at": now}
    if doc_id:
        fields["evidence_doc_id"] = doc_id
    (_db().table("crm_tasks").update(fields).eq("tenant_id", tenant_id)
     .eq("id", task_id).eq("source", SOURCE).execute())


# ── what the agent is told (setting-chosen context) ─────────────────────────
AGENT_RULE = (
    "If the customer asks about something on this list, answer from it: done (and when), "
    "or not yet (and the date it is due by). Never say a task is done unless the list says "
    "done. Never promise it sooner than its due date. Something that is not on this list is "
    "not something you can confirm — say the business will check.")


def context_lines(tenant_id: str, contact: str | None, service_types=None,
                  today: date | None = None) -> str:
    """The customer's after-job tasks, for the draft — '' (nothing at all
    added) unless the business uses after-job tasks AND this customer has
    some. The titles are the business's own words."""
    from app import db
    from app.dates import pretty_date
    if not contact or not tenant_uses(service_types):
        return ""
    try:
        bookings = db.bookings_for_contact(tenant_id, contact) or []
        by_id = {b["id"]: b for b in bookings}
        tasks = _tasks_for_bookings(tenant_id, list(by_id))
    except Exception:  # noqa: BLE001
        log.warning("after-job context failed (tenant=%s)", tenant_id, exc_info=True)
        return ""
    tasks = [t for t in tasks if t.get("status") in ("open", "done")]
    if not tasks:
        return ""
    lines = []
    for t in tasks[:10]:
        b = by_id.get(t.get("booking_id")) or {}
        state = (f"done {pretty_date(local_date(t['completed_at']))}" if t.get("status") == "done"
                 else f"NOT done yet — due by {pretty_date(str(t.get('due_date'))[:10])}")
        lines.append(f"- {b.get('booking_ref') or 'a job'}: “{t['title']}” — {state}")
    return ("After-job tasks for this customer's jobs (the business's own list):\n"
            + "\n".join(lines) + "\n" + AGENT_RULE)


# ── the daily sweep: day-end done + Today when due ──────────────────────────
def _days_left(t: dict, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(t.get("due_date"))[:10]) - today).days
    except (TypeError, ValueError):
        return None


def remind(tenant_id: str, today: date) -> int:
    """One Today card when a task is due within DUE_SOON_DAYS, one more when
    it's overdue (crm_tasks.reminded remembers which went)."""
    from app import push
    rows = (_db().table("crm_tasks").select("*").eq("tenant_id", tenant_id)
            .eq("source", SOURCE).eq("status", "open").execute().data or [])
    sent = 0
    who = _customers(tenant_id, {t.get("booking_id") for t in rows if t.get("booking_id")})
    for t in rows:
        left = _days_left(t, today)
        if left is None:
            continue
        stage = "overdue" if left < 0 else "soon" if left <= DUE_SOON_DAYS else None
        if not stage or t.get("reminded") == stage or (stage == "soon" and t.get("reminded")):
            continue
        when = ("overdue" if left < 0 else "due today" if left == 0
                else f"due in {left} day{'s' if left != 1 else ''}")
        contact, name = who.get(t.get("booking_id")) or (None, None)
        push.notify(tenant_id, title=f"{t['title']} — {when}",
                    # Today shows the BODY (not the title), so it names the task
                    # and its deadline first (rig 2026-09-27: the card read only
                    # "After the job — BK-1035 …").
                    body=f"{t['title']} — {when}. " + (t.get("description") or "").strip(),
                    url=f"/app/diary/{t['booking_id']}#after-job" if t.get("booking_id") else "/app/crm/tasks",
                    tag=f"{KIND}-{t['id']}", kind=KIND, contact=contact, contact_name=name)
        _db().table("crm_tasks").update({"reminded": stage}).eq("id", t["id"]).execute()
        sent += 1
    return sent


def _customers(tenant_id: str, booking_ids: set) -> dict:
    """{booking_id: (contact email, name)} — Today files a card under its customer."""
    if not booking_ids:
        return {}
    try:
        from app import db
        rows = (_db().table("bookings").select("id,contact").eq("tenant_id", tenant_id)
                .in_("id", list(booking_ids)).execute().data or [])
        out = {}
        for b in rows:
            c = (b.get("contact") or "").strip().lower() or None
            name = None
            if c:
                try:
                    name = (db.crm_contact_by_email(tenant_id, c) or {}).get("name")
                except Exception:  # noqa: BLE001
                    pass
            out[b["id"]] = (c, name)
        return out
    except Exception:  # noqa: BLE001
        return {}


def day_end_done(tenant_id: str, cfg: dict, today: date) -> int:
    """'day_end' businesses: a confirmed job whose last day has ended counts as
    done. Only the last fortnight — switching the setting on never back-fills
    years of history into tasks."""
    from app import db, deposits
    rows = (_db().table("bookings").select("*").eq("tenant_id", tenant_id)
            .eq("status", "confirmed").is_("done_at", "null")
            .gte("booking_date", (today - timedelta(days=45)).isoformat())
            .lt("booking_date", today.isoformat()).execute().data or [])
    n = 0
    for b in rows:
        days = deposits.booking_days(b)
        if not days or not (today - timedelta(days=14) <= days[1] < today):
            continue
        from app.scheduling import DIARY_TZ
        end = datetime.combine(days[1], datetime.min.time().replace(hour=23), tzinfo=DIARY_TZ)
        mark_done(tenant_id, b, by="day_end", sched_cfg=cfg, now=end)
        n += 1
    return n


async def run() -> None:
    """Daily: for every business using after-job tasks."""
    from app.scheduling import DIARY_TZ
    today = datetime.now(DIARY_TZ).date()
    rows = _db().table("scheduling_config").select("*").execute().data or []
    for cfg in rows:
        if not tenant_uses(cfg.get("service_types")):
            continue
        tid = cfg["tenant_id"]
        try:
            if clean_done_when(cfg.get("job_done_when")) == "day_end":
                day_end_done(tid, cfg, today)
            remind(tid, today)
        except Exception:  # noqa: BLE001 — one business never stops the rest
            log.warning("after-job sweep failed (tenant=%s)", tid, exc_info=True)


# ── the job page ────────────────────────────────────────────────────────────
def job_context(tenant_id: str, booking: dict, sched_cfg: dict | None) -> dict:
    """{after_job: {...}} for the job page — {} for a business that uses no
    after-job tasks, so its page is unchanged."""
    cfg = sched_cfg or {}
    if not tenant_uses(cfg.get("service_types")):
        return {"after_job": None}
    svc = service_of(booking, cfg.get("service_types"))
    try:
        tasks = _tasks_for_bookings(tenant_id, [booking["id"]])
    except Exception:  # noqa: BLE001
        tasks = []
    return {"after_job": {
        "done_at": local_date(booking.get("done_at")) if booking.get("done_at") else None,
        "done_by": booking.get("done_by"),
        "planned": clean_tasks((svc or {}).get("after_job")),
        "tasks": [{**t, "completed_on": local_date(t.get("completed_at")) if t.get("completed_at") else ""}
                  for t in tasks if t.get("status") in ("open", "done")],
        "manual": clean_done_when(cfg.get("job_done_when")) == "marked",
        "today": datetime.now(__import__("app.scheduling", fromlist=["DIARY_TZ"]).DIARY_TZ).date().isoformat(),
    }}


def brief_counts(tenant_id: str, today: date) -> dict | None:
    """{overdue, week} for the morning brief — None when there are none (or
    the business uses no after-job tasks: then it has no such rows at all)."""
    rows = (_db().table("crm_tasks").select("due_date").eq("tenant_id", tenant_id)
            .eq("source", SOURCE).eq("status", "open").execute().data or [])
    overdue = week = 0
    for t in rows:
        left = _days_left(t, today)
        if left is None:
            continue
        if left < 0:
            overdue += 1
        elif left <= 7:
            week += 1
    return {"overdue": overdue, "week": week} if overdue or week else None
