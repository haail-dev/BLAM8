"""Courses, the waiting list and re-enrolment — slice 3 of
docs/plan-progress-packages.md (2026-09-05).

A COURSE is a fixed-start block with a set number of places — eight
Tuesdays, a Saturday 09:30 Stage 1 programme, or a six-week beginners'
course. BLAM8 already books it as a series (a recurrence id over N confirmed
bookings) against the slot's capacity, so this slice adds only what the
research (docs/research-adult-classes.md §2.2, §4) says is missing and loudest:

- the WAITING LIST: a full class or slot offers a place on the list instead
  of "no space"; a participant may sit on several (iClassPro's complaint);
  a place that frees up is offered to the first waiting, who has a set time
  to take it before it goes to the next;
- RE-ENROLMENT: current students keep their place for the next block until
  the current one's last session; the business is prompted to tell them N
  days before; after the end the list is offered places.

Every knob is a setting on Diary settings → Recurring → Courses
(`scheduling_config.courses`): courses on/off, waiting list on/off, how a
freed place is offered (for approval / automatically), how long an offer is
held, the re-enrolment lead and how that note goes out. Nothing shows for a
tenant that has not switched it on.

A list is keyed by the CLASS, not the booking: a standing weekly slot is
"slot|<service>|<weekday>|<HH:MM>" (the 09:30 Saturday class, whichever term),
a one-off date is "date|<service>|<YYYY-MM-DD>|<HH:MM>". A freed place means
the customer LEFT the series (their remaining occurrences cancelled), not a
one-week gap — the sweep checks the next four occurrences have room.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from app import db
from app.dates import pretty_date

log = logging.getLogger(__name__)

NOTIFY = ("approve", "auto")
# When the dates asked for are full (docs/plan-when-its-full.md, Kris 2026-09-23):
# "hold" names the nearest opening AND reserves it (the behaviour before this
# setting existed, so the default); "offer" names it and reserves nothing until
# the customer says yes; "off" offers no other dates at all.
OTHER_DATES = ("hold", "offer", "off")
STATUSES = ("waiting", "offered", "booked", "lapsed", "left")
KIND_OFFER = "waitlist_offer"
KIND_REENROL = "reenrol_window"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LOOKAHEAD_OCCURRENCES = 4          # a freed place shows room every week, a holiday once
MAX_LISTS_PER_CONTACT = 12

OFFER_PURPOSE = ("A short, warm note in the business's voice telling the customer a place has "
                 "come up in the class, slot or dates they were waiting for, stating the facts exactly "
                 "as given: which class, which day and time, who it is for if a participant is "
                 "named, and by when they must reply to take it — after which it goes to the "
                 "next person waiting. Ask them to reply to take it (or to book through the "
                 "portal if the facts mention one). Nothing about prices or dates you were not "
                 "given. Never guess a pronoun from a name.")
REENROL_PURPOSE = ("A short, warm note in the business's voice telling the customer their current "
                   "block of classes is coming to an end, stating the facts exactly as given: which "
                   "class, who it is for if a participant is named, the date of the last session, "
                   "and that their place in the next block is held for them until then — after "
                   "which it is offered to the waiting list. Ask them to reply to keep the place. "
                   "Do NOT invent the next block's dates or price; if the facts do not give them, "
                   "say the business will confirm them. Never guess a pronoun from a name.")


# ── the settings ─────────────────────────────────────────────────────────────
def _i(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(float(str(v).strip()))))
    except (TypeError, ValueError):
        return default


def _b(d: dict, key: str, default: bool) -> bool:
    if key not in d:
        return default
    v = d.get(key)
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def clean(d) -> dict:
    d = d or {}
    wn = str(d.get("waitlist_notify") or "approve").strip().lower()
    rn = str(d.get("reenrol_notify") or "approve").strip().lower()
    od = str(d.get("other_dates") or "hold").strip().lower()
    return {"courses": _b(d, "courses", False),
            "other_dates": od if od in OTHER_DATES else "hold",
            # While a customer's details come in, hold what they asked for
            # (reserve-while-gathering). On by default: the behaviour since it shipped.
            "hold_while_gathering": _b(d, "hold_while_gathering", True),
            "waitlist": _b(d, "waitlist", False),
            "waitlist_notify": wn if wn in NOTIFY else "approve",
            "offer_hours": _i(d.get("offer_hours"), 1, 336, 48),
            "reenrol_days": _i(d.get("reenrol_days"), 0, 120, 21),
            "reenrol_notify": rn if rn in NOTIFY else "approve",
            # The CRM link (Kris 2026-09-05): the pipeline stage a contact's open
            # deal moves to when they join the list, and when they book from it —
            # the tenant's OWN stage names; blank = leave the pipeline alone.
            "waitlist_stage": " ".join(str(d.get("waitlist_stage") or "").split())[:60],
            "booked_stage": " ".join(str(d.get("booked_stage") or "").split())[:60]}


def settings_of(sched_cfg: dict | None) -> dict:
    return clean((sched_cfg or {}).get("courses"))


def waitlist_on(sched_cfg: dict | None) -> bool:
    return settings_of(sched_cfg)["waitlist"]


def face_value(c: dict) -> str:
    bits = []
    if c["other_dates"] != "hold":
        bits.append("other dates " + ("offered, not held" if c["other_dates"] == "offer" else "not offered"))
    if not (c["courses"] or c["waitlist"]):
        return " · ".join(bits) or "Off"
    if c["courses"]:
        bits.append("courses")
    if c["waitlist"]:
        bits.append(f"waiting list · offers held {c['offer_hours']} h")
    if c["courses"] and c["reenrol_days"]:
        bits.append(f"re-enrol {c['reenrol_days']} days before the end")
    return " · ".join(bits)


def full_face(c: dict) -> str:
    """The one-line face of the "When it's full" card."""
    bits = ["held while details come in" if c["hold_while_gathering"] else "nothing held until details are in",
            {"hold": "other dates offered and held", "offer": "other dates offered, not held",
             "off": "no other dates"}[c["other_dates"]],
            f"waiting list (offers held {c['offer_hours']} h)" if c["waitlist"] else "no waiting list"]
    return " · ".join(bits)


def render_for_prompt(d, nouns: dict | None = None) -> str:
    """The agent's rule block — only what is switched on, in the business's words."""
    c = clean(d)
    n = nouns or {"booking": "booking", "participant": "customer"}
    b = n["booking"]
    parts = []
    if c["courses"]:
        parts.append("Courses — this business runs FIXED-START BLOCKS with a set number of places: a "
                     f"{n['participant']} normally books the whole block (hold it as a recurring series with "
                     f"`repeat` and `occurrences`), and a {b} is full when its places are taken."
                     + (f" Current students keep their place in the next block until the current "
                        f"block's last session; after that, places go to the waiting list."
                        if c["reenrol_days"] else ""))
    if c["waitlist"]:
        alt = ("then name the nearest free alternative too, for a customer who can move"
               if c["other_dates"] != "off" else "and offer no other dates")
        parts.append(f"Waiting list — when the {b}, time or DATES the customer wants are FULL (check_availability "
                     "does not list it, or the diary shows it taken), do not stop at 'no space' and NEVER "
                     "say a colleague will add them or be in touch about it: YOU keep the list. Offer them "
                     f"a place on the waiting list for exactly what they asked for FIRST, {alt} — unless "
                     "they said their dates are flexible, when the alternative alone is enough. For a "
                     "stay, pass join_waitlist the arrival and departure they asked for; "
                     "the moment they say yes — or if their email already asks to be added — call "
                     "join_waitlist in THIS reply with the date, start time, service and who it is for, "
                     "and tell them the position it returns. When a place frees up it is offered to the "
                     f"first person waiting, who has {c['offer_hours']} hours to take it before it moves "
                     "on. A customer who was OFFERED a place (see their record) and replies to take it: "
                     "book it with hold_booking as normal.")
    # other_dates is not spoken here: it CHOOSES the diary rules the agent
    # reads (agent._diary_rules) and its tools (scheduling.tools_for_other_dates).
    return "\n".join(parts)


# ── keys and labels ──────────────────────────────────────────────────────────
def _hhmm(window) -> str:
    w = str(window or "").strip()
    return w[:5] if ":" in w else w


def slot_key(service: str | None, day: date, window: str) -> str:
    return f"slot|{(service or '').strip()}|{day.weekday()}|{_hhmm(window)}"


def date_key(service: str | None, day: date, window: str) -> str:
    return f"date|{(service or '').strip()}|{day.isoformat()}|{_hhmm(window)}"


def stay_key(resource: str | None, arrival: str, departure: str) -> str:
    """A stay's list is the exact span asked for ("stay|<resource>|<arrival>|<departure>",
    times as YYYY-MM-DDTHH:MM) — Daniel waits for 6–8 Nov, not for "November"."""
    return f"stay|{(resource or '').strip()}|{str(arrival)[:16]}|{str(departure)[:16]}"


def stay_dates(key: str) -> tuple[date, date] | None:
    k = parse_key(key)
    if k["kind"] != "stay":
        return None
    try:
        return date.fromisoformat(k["when"][:10]), date.fromisoformat(k["window"][:10])
    except ValueError:
        return None


def parse_key(key: str) -> dict:
    kind, service, when, window = (str(key or "").split("|") + ["", "", ""])[:4]
    return {"kind": kind, "service": service, "when": when, "window": window}


def label(key: str) -> str:
    k = parse_key(key)
    svc = (k["service"] + " · ") if k["service"] else ""        # a windows diary has no service (3b)
    if k["kind"] == "slot":
        try:
            dayname = WEEKDAYS[int(k["when"])] + "s"
        except (ValueError, IndexError):
            dayname = "weekly"
        return f"{svc}{dayname} {k['window']}".strip()
    if k["kind"] == "stay":
        span = stay_dates(key)
        if span:
            return f"{svc}{pretty_date(span[0])} to {pretty_date(span[1])}".strip()
    return f"{svc}{pretty_date(k['when'])} {k['window']}".strip()


def keys_for_booking(b: dict, *, standing: bool) -> list[str]:
    """The lists a booking can satisfy or free: its date's list, and — when it
    belongs to a standing series (or the tenant runs courses) — the slot's."""
    try:
        day = date.fromisoformat(str(b.get("booking_date"))[:10])
    except (TypeError, ValueError):
        return []
    svc = ((b.get("details") or {}).get("Service")) or ""
    w = b.get("window") or ""
    out = [date_key(svc, day, w)]
    if standing or b.get("recurrence_id"):
        out.append(slot_key(svc, day, w))
    return out


def next_dates(key: str, today: date, n: int = LOOKAHEAD_OCCURRENCES) -> list[date]:
    k = parse_key(key)
    if k["kind"] in ("date", "stay"):
        try:
            d = date.fromisoformat(k["when"][:10])
        except ValueError:
            return []
        return [d] if d >= today else []
    try:
        wd = int(k["when"])
    except ValueError:
        return []
    first = today + timedelta(days=(wd - today.weekday()) % 7)
    return [first + timedelta(days=7 * i) for i in range(n)]


# ── pure planning ────────────────────────────────────────────────────────────
def position(rows: list[dict], row: dict) -> int:
    """1-based place in the queue for this row's key (waiting + offered ahead of it)."""
    ahead = [r for r in rows if r.get("series_key") == row.get("series_key")
             and r.get("status") in ("waiting", "offered")
             and str(r.get("created_at") or "") < str(row.get("created_at") or "")]
    return len(ahead) + 1


def next_waiting(rows: list[dict], key: str) -> dict | None:
    """The row to offer next for a key — none while an offer is still live."""
    mine = [r for r in rows if r.get("series_key") == key]
    if any(r.get("status") == "offered" for r in mine):
        return None
    waiting = sorted((r for r in mine if r.get("status") == "waiting"),
                     key=lambda r: str(r.get("created_at") or ""))
    return waiting[0] if waiting else None


def expired_offers(rows: list[dict], now: datetime) -> list[dict]:
    out = []
    for r in rows:
        if r.get("status") != "offered" or not r.get("expires_at"):
            continue
        try:
            exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            out.append(r)
    return out


def place_freed_by_cancel(booking: dict, remaining_in_series: list[dict]) -> bool:
    """A cancelled occurrence frees the customer's PLACE only when nothing of
    theirs remains in the series (a holiday week is not a free place)."""
    if not booking.get("recurrence_id"):
        return True
    return not remaining_in_series


def reenrol_due(series: list[dict], today: date, lead_days: int) -> list[dict]:
    """Pure: (recurrence_id, contact) groups whose LAST confirmed occurrence is
    within `lead_days` of today and still ahead — the re-enrolment window."""
    if not lead_days:
        return []
    groups: dict[tuple, dict] = {}
    for b in series:
        if b.get("status") != "confirmed" or not b.get("recurrence_id") or not b.get("contact"):
            continue
        g = groups.setdefault((b["recurrence_id"], b["contact"]), {"bookings": []})
        g["bookings"].append(b)
    out = []
    for (rid, contact), g in groups.items():
        last = max(str(b.get("booking_date"))[:10] for b in g["bookings"])
        try:
            last_d = date.fromisoformat(last)
        except ValueError:
            continue
        if today <= last_d <= today + timedelta(days=lead_days):
            first = min(g["bookings"], key=lambda b: str(b.get("booking_date")))
            out.append({"recurrence_id": rid, "contact": contact, "last": last_d,
                        "booking": first, "count": len(g["bookings"])})
    return out


# ── I/O ──────────────────────────────────────────────────────────────────────
def _c(client=None):
    return client or db.db()


def list_rows(tenant_id: str, *, contact_email: str | None = None, keys: list[str] | None = None,
              live_only: bool = True, client=None) -> list[dict]:
    try:
        q = _c(client).table("waitlist").select("*").eq("tenant_id", tenant_id)
        if contact_email:
            q = q.eq("contact_email", contact_email)
        if keys:
            q = q.in_("series_key", keys)
        if live_only:
            q = q.in_("status", ["waiting", "offered"])
        return q.order("created_at").execute().data or []
    except Exception:
        log.warning("waitlist read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def _set(row_id: str, tenant_id: str, fields: dict, client=None) -> None:
    try:
        _c(client).table("waitlist").update(fields).eq("id", row_id).eq("tenant_id", tenant_id).execute()
    except Exception:
        log.warning("waitlist update failed (%s)", row_id, exc_info=True)


def _crm(tenant_id: str, contact_email: str | None, activity: str, summary: str, *,
         stage_name: str = "", source_id: str | None = None, client=None) -> None:
    """The CRM side of a list event (Kris 2026-09-05: "connect the waiting list
    to the CRM"): a timeline row on the contact, and — when the Courses face
    names a stage — the contact's open deal moved there (created if they have
    none). Best-effort; the stage is the tenant's own name, matched case-
    insensitively, so nothing here knows a vertical."""
    if not contact_email:
        return
    try:
        from app import crm
        contact = db.crm_contact_by_email(tenant_id, contact_email, client=client)
        if not contact:
            return
        crm.note_contact_activity(tenant_id, contact, "waitlist", subject=summary, source_id=source_id)
        if not stage_name:
            return
        stages = db.crm_list_stages(tenant_id, client=client)
        stage = next((s for s in stages if (s.get("name") or "").strip().lower() == stage_name.strip().lower()), None)
        if not stage:
            return
        deals = [d for d in db.crm_list_deals(tenant_id, contact_id=contact["id"], client=client)
                 if d.get("status", "open") == "open"]
        if deals:
            if deals[0].get("stage_id") != stage["id"]:
                db.crm_update_deal(deals[0]["id"], tenant_id, {"stage_id": stage["id"]}, client=client)
        else:
            db.crm_create_deal(tenant_id, {"title": summary[:120], "stage_id": stage["id"],
                                           "primary_contact_id": contact["id"], "status": "open"})
    except Exception:
        log.warning("waitlist CRM hook failed (tenant=%s)", tenant_id, exc_info=True)


def join(tenant_id: str, contact_email: str, contact_id: str | None, key: str, *,
         participant: str | None = None, instance_id: str | None = None,
         note: str | None = None, source: str = "email", client=None) -> dict:
    """Add one waiter to one list. Same contact + same participant + same key
    joins once; several lists per participant are fine (the complaint)."""
    rows = list_rows(tenant_id, contact_email=contact_email, client=client)
    for r in rows:
        if r.get("series_key") == key and (r.get("instance_id") or None) == (instance_id or None) \
                and (not participant or (r.get("participant") or "").lower() == participant.lower()):
            all_rows = list_rows(tenant_id, keys=[key], client=client)
            return {"joined": False, "already": True, "row": r, "position": position(all_rows, r)}
    if len(rows) >= MAX_LISTS_PER_CONTACT:
        return {"error": "too_many", "message": "This customer is already on the maximum number of waiting lists."}
    row = {"tenant_id": tenant_id, "contact_id": contact_id, "contact_email": contact_email,
           "instance_id": instance_id, "participant": (participant or "").strip()[:80] or None,
           "series_key": key, "label": label(key), "status": "waiting",
           "note": (note or "").strip()[:300] or None, "source": source}
    try:
        res = _c(client).table("waitlist").insert(row).execute()
        row = res.data[0] if res.data else row
    except Exception:
        log.warning("waitlist insert failed (tenant=%s)", tenant_id, exc_info=True)
        return {"error": "write_failed", "message": "The waiting list could not be written just now."}
    all_rows = list_rows(tenant_id, keys=[key], client=client)
    pos = position(all_rows, row)
    who = f"{participant} ({contact_email})" if participant else contact_email
    db.log_agent_event(tenant_id, "waitlist_join",
                       f"{who} joined the waiting list for {label(key)} — number {pos}",
                       contact=contact_email, detail={"key": key, "position": pos, "source": source})
    try:
        cfg = settings_of(db.get_scheduling_config(tenant_id, client=client))
    except Exception:
        cfg = clean({})
    _crm(tenant_id, contact_email, "joined",
         f"Joined the waiting list for {label(key)}" + (f" ({participant})" if participant else "") + f" — number {pos}",
         stage_name=cfg["waitlist_stage"], source_id=f"waitlist-join-{row.get('id')}", client=client)
    return {"joined": True, "row": row, "position": pos, "label": label(key)}


def leave(tenant_id: str, row_id: str, *, why: str = "left", client=None) -> None:
    _set(row_id, tenant_id, {"status": why if why in STATUSES else "left",
                             "closed_at": datetime.now(timezone.utc).isoformat()}, client=client)


def on_booked(tenant_id: str, contact_email: str | None, bookings: list[dict], *,
              standing: bool = True, client=None) -> int:
    """A booking placed for a waiter closes their list row(s) for that class."""
    if not contact_email or not bookings:
        return 0
    keys = sorted({k for b in bookings for k in keys_for_booking(b, standing=standing)})
    if not keys:
        return 0
    n = 0
    for r in list_rows(tenant_id, contact_email=contact_email, keys=keys, client=client):
        _set(r["id"], tenant_id, {"status": "booked", "booking_id": bookings[0].get("id"),
                                  "closed_at": datetime.now(timezone.utc).isoformat()}, client=client)
        n += 1
    if n:
        db.log_agent_event(tenant_id, "waitlist_join",
                           f"{contact_email} booked from the waiting list — {label(keys[-1])}",
                           contact=contact_email, booking_id=bookings[0].get("id"),
                           booking_ref=bookings[0].get("booking_ref"))
        try:
            cfg = settings_of(db.get_scheduling_config(tenant_id, client=client))
        except Exception:
            cfg = clean({})
        _crm(tenant_id, contact_email, "booked",
             f"Booked from the waiting list — {label(keys[-1])} ({bookings[0].get('booking_ref') or 'booking'})",
             stage_name=cfg["booked_stage"], source_id=f"waitlist-booked-{bookings[0].get('id')}", client=client)
    return n


def _confirmed_series(tenant_id: str, since: date, client=None) -> list[dict]:
    try:
        return (_c(client).table("bookings")
                .select("id,booking_ref,booking_date,window,status,contact,recurrence_id,details,summary")
                .eq("tenant_id", tenant_id).eq("status", "confirmed")
                .gte("booking_date", since.isoformat()).not_.is_("recurrence_id", "null")
                .execute().data or [])
    except Exception:
        log.warning("series read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def has_room(tenant_id: str, key: str, today: date, *, cfg_row: dict | None = None,
             now: datetime | None = None, client=None) -> bool:
    """Does the class behind `key` have a place on every one of its next
    occurrences (four for a standing slot, the one date otherwise)?"""
    from app import scheduling, services, slotgrid
    cfg_row = cfg_row if cfg_row is not None else (db.get_scheduling_config(tenant_id, client=client) or {})
    k = parse_key(key)
    if k["kind"] == "stay":
        if not next_dates(key, today):
            return False
        try:
            arr, dep = datetime.fromisoformat(k["when"]), datetime.fromisoformat(k["window"])
        except ValueError:
            return False
        return scheduling.stay_has_room(tenant_id, arr, dep, k["service"] or None,
                                        cfg_row=cfg_row, client=client)
    dates = next_dates(key, today)
    if not dates:
        return False
    svc = services.find(cfg_row.get("service_types"), k["service"]) if k["service"] else None
    length = (slotgrid.blocks_for_minutes(int(svc["minutes"])) if svc and svc.get("minutes")
              else slotgrid.blocks_for_minutes(int((cfg_row.get("slot_minutes") or 60))))
    when = now or datetime.now(scheduling.DIARY_TZ)
    # A service bound to a pool (an appointment beside stays, a resource, a bay)
    # has room only in ITS pool, and only inside its own rules — the same
    # question the agent's availability check asks (2026-09-23).
    pool = None
    if svc and svc.get("resource"):
        from app import diary_config
        pool = diary_config.find_resource(cfg_row.get("resource_types"), svc["resource"])
    for d in dates:
        booked = db.list_active_bookings(tenant_id, (d - timedelta(days=1)).isoformat(), d.isoformat(),
                                         client=client)
        counts = (db.count_service_bookings_by_day(tenant_id, svc["name"], d.isoformat(), d.isoformat(),
                                                   client=client)
                  if svc and services.rules(svc)["max_per_day"] else None)
        slots = scheduling.service_availability(
            cfg_row, booked, length, d, d, when,
            capacity=int(pool["count"]) if pool else None, resource=pool["name"] if pool else None,
            svc=svc, day_counts=counts)
        if not any(s["window"] == k["window"] for s in slots):
            return False
    return True


async def offer_place(tenant_id: str, row: dict, cfg: dict, *, now: datetime | None = None,
                      client=None) -> dict:
    """Offer this waiter the place: the row turns 'offered' with a deadline,
    and the note goes per `waitlist_notify` (approve → the approval queue and
    a card; auto → sent)."""
    from app import agent, disclosure, mailbox, push, usage
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(hours=cfg["offer_hours"])
    _set(row["id"], tenant_id, {"status": "offered", "offered_at": now.isoformat(),
                                "expires_at": expires.isoformat()}, client=client)
    to = row.get("contact_email")
    contact = db.crm_contact_by_email(tenant_id, to, client=client) or {} if to else {}
    who = row.get("participant")
    lbl = row.get("label") or label(row.get("series_key") or "")
    deadline = pretty_date(expires.date()) + expires.strftime(" %H:%M")
    db.log_agent_event(tenant_id, KIND_OFFER,
                       f"A place in {lbl} offered to {who or contact.get('name') or to} — "
                       f"held until {deadline}", contact=to,
                       detail={"key": row.get("series_key"), "row": row.get("id"), "notify": cfg["waitlist_notify"]})
    _crm(tenant_id, to, "offered", f"Offered a place in {lbl} — held until {deadline}",
         source_id=f"waitlist-offer-{row.get('id')}", client=client)
    if not to:
        return {"ok": True, "sent": False}
    is_stay = parse_key(row.get("series_key") or "")["kind"] == "stay"
    facts = ((f"A space has come up for the dates they were waiting for: {lbl}." if is_stay
              else f"A place has come up in {lbl}.") + (f" It is for {who}." if who else "")
             + f" The customer must reply by {deadline} to take it; after that it is offered to the "
               f"next person waiting." + (f" The message is to {contact['name']}." if contact.get("name") else ""))
    tenant_cfg = db.effective_config(tenant_id, None, client=client)
    try:
        result = await agent.compose_outbound(tenant_cfg, OFFER_PURPOSE, facts, channel="email",
                                              tenant_id=tenant_id, log_operation=KIND_OFFER)
    except Exception:
        log.warning("waitlist offer compose failed (tenant=%s)", tenant_id, exc_info=True)
        return {"ok": False, "error": "compose_failed"}
    subject = f"A space has come up — {lbl}" if is_stay else f"A place has come up — {lbl}"
    log_id, _ = db.insert_email_log(tenant_id, to, subject, f"[waiting list] {facts}", result.text,
                                    status="pending", kind=KIND_OFFER, channel="email")
    usage.record_anthropic(tenant_id, result.model, result.usage, operation=KIND_OFFER, email_log_id=log_id)
    url = f"/app/conversations/view?contact={to}"
    title = f"Place offered: {lbl}"
    if cfg["waitlist_notify"] == "auto":
        try:
            await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(tenant_cfg, result.text))
            db.mark_email_log_sent(log_id, tenant_id, via="human", edited=False)
        except Exception:
            log.warning("waitlist offer send failed (tenant=%s)", tenant_id, exc_info=True)
            return {"ok": False, "error": "send_failed", "log_id": log_id}
        push.notify(tenant_id, title=title, body=f"Told {contact.get('name') or to} — held until {deadline}.",
                    url=url, tag=f"waitlist-{row['id']}", kind=KIND_OFFER, contact=to,
                    contact_name=contact.get("name"))
        return {"ok": True, "sent": True, "log_id": log_id}
    push.notify(tenant_id, title=title,
                body=f"A message to {contact.get('name') or to} is waiting for your approval.",
                url=url, tag=f"waitlist-{row['id']}", kind=KIND_OFFER, contact=to,
                contact_name=contact.get("name"))
    return {"ok": True, "sent": False, "log_id": log_id}


async def offer_next(tenant_id: str, key: str, cfg: dict, *, now: datetime | None = None,
                     client=None) -> dict | None:
    rows = list_rows(tenant_id, keys=[key], client=client)
    nxt = next_waiting(rows, key)
    if not nxt:
        return None
    return await offer_place(tenant_id, nxt, cfg, now=now, client=client)


async def on_place_freed(tenant_id: str, booking: dict, *, today: date | None = None,
                         client=None) -> list[dict]:
    """A cancel in the diary: if it freed a place (not a one-week gap) and the
    list has someone for that class, offer it now."""
    sched = db.get_scheduling_config(tenant_id, client=client) or {}
    cfg = settings_of(sched)
    if not cfg["waitlist"]:
        return []
    today = today or date.today()
    remaining = []
    if booking.get("recurrence_id"):
        try:
            remaining = [b for b in _confirmed_series(tenant_id, today, client=client)
                         if b.get("recurrence_id") == booking["recurrence_id"]
                         and b.get("contact") == booking.get("contact") and b.get("id") != booking.get("id")]
        except Exception:
            remaining = []
    if not place_freed_by_cancel(booking, remaining):
        return []
    keys = list(keys_for_booking(booking, standing=cfg["courses"]))
    for k in stay_keys_overlapping(tenant_id, booking, client=client):
        if k not in keys:
            keys.append(k)
    if booking.get("recurrence_id"):
        # The series is now empty for this customer: EVERY future date it held
        # is a freed place, not just the row cancelled last (run #8: nine
        # Saturdays cancelled one by one, the waiting row keyed on the first).
        for b in _series_rows(tenant_id, booking["recurrence_id"], booking.get("contact"), today,
                              client=client):
            for k in keys_for_booking(b, standing=cfg["courses"]):
                if k not in keys:
                    keys.append(k)
    out = []
    for key in keys:
        if not list_rows(tenant_id, keys=[key], client=client):
            continue
        if has_room(tenant_id, key, today, cfg_row=sched, client=client):
            res = await offer_next(tenant_id, key, cfg, client=client)
            if res:
                out.append(res)
    return out


def stay_keys_overlapping(tenant_id: str, booking: dict, *, client=None) -> list[str]:
    """The stay lists a freed booking could open: any waited-for span sharing a
    date with it. A stay freeing 5–9 Nov opens the 6–8 Nov list; one that ended
    on the 5th does not. Cheap for a tenant with no stay lists (one read)."""
    from app import slotgrid
    span = slotgrid.stay_span(booking)
    if not span:
        return []
    first = slotgrid.abs_to_datetime(span[0]).date()
    last = slotgrid.abs_to_datetime(span[0] + span[1]).date()
    out = []
    for r in list_rows(tenant_id, client=client):
        key = r.get("series_key") or ""
        sd = stay_dates(key)
        if sd and sd[0] <= last and first <= sd[1] and key not in out:
            out.append(key)
    return out


def _lapsed_holds(tenant_id: str, now: datetime, client=None) -> list[dict]:
    """Offered rows past their deadline and not yet released — an offer nobody
    took, or a space reserved while details were gathered that never came."""
    try:
        return (_c(client).table("bookings")
                .select("id,booking_ref,booking_date,window,status,contact,recurrence_id,details,"
                        "summary,start_block,length_blocks,expires_at")
                .eq("tenant_id", tenant_id).eq("status", "offered")
                .lt("expires_at", now.isoformat()).execute().data or [])
    except Exception:
        log.warning("lapsed holds read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


async def release_lapsed_holds(tenant_id: str, *, now: datetime | None = None,
                               today: date | None = None, client=None) -> list[dict]:
    """A lapsed hold frees a place exactly as a cancel does (the K11 lesson: if
    Nadia never sends her times, the last space goes to whoever is waiting for
    it). Releases the rows the nightly cron would, sooner, and offers each
    freed place to its list. Returns the offers made."""
    now = now or datetime.now(timezone.utc)
    rows = _lapsed_holds(tenant_id, now, client=client)
    if not rows:
        return []
    try:
        (_c(client).table("bookings").update({"status": "cancelled", "updated_at": now.isoformat()})
         .eq("tenant_id", tenant_id).eq("status", "offered")
         .in_("id", [r["id"] for r in rows]).execute())
    except Exception:
        log.warning("lapsed holds release failed (tenant=%s)", tenant_id, exc_info=True)
        return []
    out = []
    for r in rows:
        db.log_agent_event(tenant_id, "offer_lapsed",
                           f"{r.get('booking_ref') or 'A held place'} lapsed unanswered — released",
                           contact=r.get("contact"), booking_id=r.get("id"), booking_ref=r.get("booking_ref"))
        out += await on_place_freed(tenant_id, {**r, "status": "confirmed"}, today=today, client=client)
    return out


def _series_rows(tenant_id: str, recurrence_id: str, contact: str | None, since: date,
                 client=None) -> list[dict]:
    """The customer's occurrences of one series from `since` on, cancelled ones
    included — the dates a collapsed series gives back."""
    try:
        q = (_c(client).table("bookings")
             .select("id,booking_ref,booking_date,window,status,contact,recurrence_id,details,summary")
             .eq("tenant_id", tenant_id).eq("recurrence_id", recurrence_id)
             .gte("booking_date", since.isoformat())
             .in_("status", ["cancelled", "confirmed"]))
        if contact:
            q = q.eq("contact", contact)
        return q.execute().data or []
    except Exception:
        log.warning("series rows read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


async def send_reenrol(tenant_id: str, item: dict, cfg: dict, *, client=None) -> dict:
    from app import agent, disclosure, mailbox, push, usage
    to = item["contact"]
    b = item["booking"]
    contact = db.crm_contact_by_email(tenant_id, to, client=client) or {}
    svc = ((b.get("details") or {}).get("Service")) or b.get("summary") or "the class"
    try:
        d0 = date.fromisoformat(str(b.get("booking_date"))[:10])
        when = f"{WEEKDAYS[d0.weekday()]}s at {_hhmm(b.get('window'))}"
    except (TypeError, ValueError):
        when = ""
    who = ""
    try:
        subs = db.subjects_for_bookings(tenant_id, [b["id"]], client=client).get(b["id"], [])
        who = ", ".join(s.get("name") for s in subs if s.get("name"))
    except Exception:
        pass
    lbl = f"{svc}" + (f", {when}" if when else "")
    last = pretty_date(item["last"])
    db.log_agent_event(tenant_id, KIND_REENROL,
                       f"Re-enrolment window: {who or contact.get('name') or to}'s {lbl} block ends {last}",
                       contact=to, booking_id=b.get("id"), booking_ref=b.get("booking_ref"),
                       detail={"recurrence_id": item["recurrence_id"], "last": item["last"].isoformat()})
    facts = (f"The current block of {lbl}" + (f" for {who}" if who else "") + f" ends on {last}. "
             f"The customer's place in the next block is held for them until then; after that it is "
             f"offered to the waiting list. Reply to keep the place."
             + (f" The message is to {contact['name']}." if contact.get("name") else ""))
    tenant_cfg = db.effective_config(tenant_id, None, client=client)
    try:
        result = await agent.compose_outbound(tenant_cfg, REENROL_PURPOSE, facts, channel="email",
                                              tenant_id=tenant_id, log_operation=KIND_REENROL)
    except Exception:
        log.warning("re-enrol compose failed (tenant=%s)", tenant_id, exc_info=True)
        return {"ok": False, "error": "compose_failed"}
    subject = f"Keep {who + chr(39) + 's ' if who else 'your '}place — {svc} next block"
    log_id, _ = db.insert_email_log(tenant_id, to, subject, f"[re-enrolment] {facts}", result.text,
                                    status="pending", kind=KIND_REENROL, channel="email")
    usage.record_anthropic(tenant_id, result.model, result.usage, operation=KIND_REENROL, email_log_id=log_id)
    try:
        _c(client).table("course_notices").insert({"tenant_id": tenant_id, "recurrence_id": item["recurrence_id"],
                                                   "contact_email": to, "kind": "reenrol",
                                                   "email_log_id": log_id}).execute()
    except Exception:
        log.warning("course_notices insert failed (tenant=%s)", tenant_id, exc_info=True)
    url = f"/app/conversations/view?contact={to}"
    title = f"Re-enrol: {who or contact.get('name') or to} — {svc}"
    if cfg["reenrol_notify"] == "auto":
        try:
            await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(tenant_cfg, result.text))
            db.mark_email_log_sent(log_id, tenant_id, via="human", edited=False)
        except Exception:
            log.warning("re-enrol send failed (tenant=%s)", tenant_id, exc_info=True)
            return {"ok": False, "error": "send_failed", "log_id": log_id}
        push.notify(tenant_id, title=title, body=f"Told them the block ends {last}.", url=url,
                    tag=f"reenrol-{item['recurrence_id']}-{to}", kind=KIND_REENROL, contact=to,
                    contact_name=contact.get("name"))
        return {"ok": True, "sent": True, "log_id": log_id}
    push.notify(tenant_id, title=title, body="A message is waiting for your approval.", url=url,
                tag=f"reenrol-{item['recurrence_id']}-{to}", kind=KIND_REENROL, contact=to,
                contact_name=contact.get("name"))
    return {"ok": True, "sent": False, "log_id": log_id}


def _notified(tenant_id: str, client=None) -> set[tuple[str, str]]:
    try:
        rows = _c(client).table("course_notices").select("recurrence_id,contact_email") \
            .eq("tenant_id", tenant_id).eq("kind", "reenrol").execute().data or []
        return {(r["recurrence_id"], r["contact_email"]) for r in rows}
    except Exception:
        return set()


def tenants_on(client=None) -> list[tuple[str, dict]]:
    try:
        rows = _c(client).table("scheduling_config").select("*").execute().data or []
    except Exception:
        return []
    out = []
    for r in rows:
        c = settings_of(r)
        if c["waitlist"] or (c["courses"] and c["reenrol_days"]):
            out.append((r["tenant_id"], r))
    return out


def reopen_released(tenant_id: str, *, client=None) -> int:
    """3b nit: a waiter's row closed when the HOLD was placed; a hold released
    since (skipped draft, cancelled by hand) puts them back in the queue at
    their original place (created_at is untouched)."""
    try:
        rows = (_c(client).table("waitlist").select("id,booking_id").eq("tenant_id", tenant_id)
                .eq("status", "booked").not_.is_("booking_id", "null").execute().data or [])
    except Exception:
        return 0
    if not rows:
        return 0
    ids = [r["booking_id"] for r in rows]
    try:
        dead = {b["id"] for b in (_c(client).table("bookings").select("id,status").eq("tenant_id", tenant_id)
                                  .in_("id", ids).in_("status", ["cancelled", "declined"]).execute().data or [])}
    except Exception:
        return 0
    n = 0
    for r in rows:
        if r["booking_id"] in dead:
            _set(r["id"], tenant_id, {"status": "waiting", "booking_id": None, "closed_at": None}, client=client)
            n += 1
    return n


def waiting_by_day(tenant_id: str, dates: list[str], *, client=None) -> dict[str, list[dict]]:
    """3b nit: the diary's day panels — who is waiting for a class on that day
    (a standing slot's weekday, or the exact date)."""
    rows = list_rows(tenant_id, client=client)
    if not rows:
        return {}
    out: dict[str, list[dict]] = {}
    for iso in dates:
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        for r in rows:
            k = parse_key(r.get("series_key") or "")
            sd = stay_dates(r.get("series_key") or "")
            hit = ((k["kind"] == "slot" and k["when"] == str(d.weekday())) or (k["kind"] == "date" and k["when"] == iso)
                   or (sd is not None and sd[0] <= d <= sd[1]))
            if hit:
                out.setdefault(iso, []).append({**r, "position": position(rows, r)})
    for iso in out:
        out[iso].sort(key=lambda r: (parse_key(r["series_key"])["window"], r["position"]))
    return out


async def _list_pass(tenant_id: str, sched: dict, cfg: dict, today: date, now: datetime,
                     stats: dict) -> None:
    """Lapse waiters' expired offers (passing the place on), then offer every
    place that has opened to the first waiting."""
    stats["reopened"] = stats.get("reopened", 0) + reopen_released(tenant_id)
    rows = list_rows(tenant_id)
    for r in expired_offers(rows, now):
        leave(tenant_id, r["id"], why="lapsed")
        db.log_agent_event(tenant_id, KIND_OFFER,
                           f"{r.get('participant') or r.get('contact_email')} did not take the place in "
                           f"{r.get('label')} in time — offered to the next", contact=r.get("contact_email"))
        _crm(tenant_id, r.get("contact_email"), "lapsed",
             f"Did not take the offered place in {r.get('label')} in time",
             source_id=f"waitlist-lapsed-{r.get('id')}")
        stats["lapsed"] += 1
    rows = list_rows(tenant_id)
    for key in sorted({r["series_key"] for r in rows}):
        if next_waiting(rows, key) is None:
            continue
        if parse_key(key)["kind"] in ("date", "stay") and not next_dates(key, today):
            for r in rows:                       # the date has passed — the list is moot
                if r["series_key"] == key and r["status"] == "waiting":
                    leave(tenant_id, r["id"], why="lapsed")
            continue
        if has_room(tenant_id, key, today, cfg_row=sched):
            if await offer_next(tenant_id, key, cfg):
                stats["offered"] += 1


async def run_sweep(today: date | None = None) -> dict:
    """Daily: lapse expired offers and pass the place on; offer places that
    have opened; raise the re-enrolment notes."""
    today = today or datetime.now().date()
    now = datetime.now(timezone.utc)
    stats = {"lapsed": 0, "offered": 0, "reenrol": 0}
    for tenant_id, sched in tenants_on():
        cfg = settings_of(sched)
        try:
            if cfg["waitlist"]:
                await _list_pass(tenant_id, sched, cfg, today, now, stats)
            if cfg["courses"] and cfg["reenrol_days"]:
                done = _notified(tenant_id)
                for item in reenrol_due(_confirmed_series(tenant_id, today), today, cfg["reenrol_days"]):
                    if (item["recurrence_id"], item["contact"]) in done:
                        continue
                    res = await send_reenrol(tenant_id, item, cfg)
                    if res.get("ok"):
                        stats["reenrol"] += 1
        except Exception:
            log.warning("courses sweep failed (tenant=%s)", tenant_id, exc_info=True)
    return stats


async def run_frequent(today: date | None = None) -> dict:
    """Every half hour, for tenants that keep a list: holds that lapsed free
    their place and it goes to whoever is waiting, and a waiter's offer that
    ran out passes on — within the half hour, not the next morning."""
    today = today or datetime.now().date()
    now = datetime.now(timezone.utc)
    stats = {"lapsed": 0, "offered": 0}
    for tenant_id, sched in tenants_on():
        cfg = settings_of(sched)
        if not cfg["waitlist"]:
            continue
        try:
            stats["offered_from_lapsed"] = stats.get("offered_from_lapsed", 0) + len(await release_lapsed_holds(tenant_id, now=now, today=today))
            await _list_pass(tenant_id, sched, cfg, today, now, stats)
        except Exception:
            log.warning("waiting-list sweep failed (tenant=%s)", tenant_id, exc_info=True)
    return stats


# ── what the drafter, the portal and the pages read ──────────────────────────
def lines_for_contact(tenant_id: str, contact_email: str | None, *, client=None) -> list[str]:
    if not contact_email:
        return []
    rows = list_rows(tenant_id, contact_email=contact_email, client=client)
    if not rows:
        return []
    by_key: dict[str, list[dict]] = {}
    for r in list_rows(tenant_id, keys=sorted({r["series_key"] for r in rows}), client=client):
        by_key.setdefault(r["series_key"], []).append(r)
    out = []
    for r in rows:
        who = f" for {r['participant']}" if r.get("participant") else ""
        if r.get("status") == "offered":
            try:
                exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
                until = f" — reply by {pretty_date(exp.date())} {exp.strftime('%H:%M')} to take it"
            except (TypeError, ValueError):
                until = ""
            out.append(f"OFFERED a place in {r.get('label')}{who}{until}")
        else:
            out.append(f"Waiting list: {r.get('label')}{who} — number {position(by_key.get(r['series_key'], []), r)}")
    return out


def context_block(tenant_id: str, contact_email: str | None, *, client=None) -> str:
    lines = lines_for_contact(tenant_id, contact_email, client=client)
    if not lines:
        return ""
    return ("Waiting lists (from the business's records — a customer replying to take an offered "
            "place books it with hold_booking; never re-add them):\n" + "\n".join("- " + ln for ln in lines))


def job_context(tenant_id: str, booking: dict, *, client=None) -> dict:
    """The job page's Waiting list block — best-effort, empty when the list is off."""
    try:
        cfg = settings_of(db.get_scheduling_config(tenant_id, client=client))
    except Exception:
        return {"waitlist_on": False, "waiting": []}
    if not cfg["waitlist"]:
        return {"waitlist_on": False, "waiting": []}
    return {"waitlist_on": True, "waiting": rows_for_booking(tenant_id, booking, standing=cfg["courses"], client=client)}


def card_context(tenant_id: str, contact: dict | None, *, client=None) -> dict:
    """The contact card's Waiting lists section — best-effort."""
    try:
        sched = db.get_scheduling_config(tenant_id, client=client) or {}
        cfg = settings_of(sched)
    except Exception:
        return {"wait_on": False, "wait_rows": [], "wait_services": []}
    if not cfg["waitlist"]:
        return {"wait_on": False, "wait_rows": [], "wait_services": []}
    from app import services
    rows = list_rows(tenant_id, contact_email=(contact or {}).get("email"), client=client)
    if rows:
        allk = list_rows(tenant_id, keys=sorted({r["series_key"] for r in rows}), client=client)
        for r in rows:
            r["position"] = position(allk, r)
    return {"wait_on": True, "wait_rows": rows,
            "wait_services": services.names(sched.get("service_types"))}


def rows_for_booking(tenant_id: str, booking: dict, *, standing: bool = True, client=None) -> list[dict]:
    """The job page: who is waiting for this occurrence's class."""
    keys = keys_for_booking(booking, standing=standing)
    if not keys:
        return []
    rows = list_rows(tenant_id, keys=keys, client=client)
    for r in rows:
        r["position"] = position(rows, r)
    return rows
