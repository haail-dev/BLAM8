"""Concierge reads added after the owner-phrase test (docs/concierge-pass1-
findings-2026-09-06.md): the three questions every service business asks
that the July catalogue could not answer.

- get_availability: "Is Thursday morning free?" — the ENGINE's answer
  (capacity, the gap between jobs, working days, blackouts), not a count of
  booking rows. Live 2026-09-06: two AM rows on a capacity-3 diary read as
  "Thursday isn't free" and the owner would have turned a deep clean away.
- get_contact_record: "Who's Kris's cleaner / what's the alarm code / is
  Biscuit's jab in date" — the record's own fields, dependants, documents,
  open promises, notes, credits and level. The owner's data, shown to the
  owner; fields whose hint says never to email them are flagged, not hidden.
- list_dormant_contacts: "Who haven't I seen since July?" — contacts with no
  booking on or after a date (or none at all), oldest activity first.

Every executor is read-only, tenant-scoped through the caller's client, and
degrades section by section (a failed store never voids the answer).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from app import db, scheduling, slotgrid
from app import services as services_mod

log = logging.getLogger(__name__)

MAX_DAYS = 14
_NEVER_EMAIL = re.compile(r"never|not\b.*\b(email|write|put)|kept on the record|do not (send|email)", re.I)
_WINDOW_WORDS = {"am": "am", "morning": "am", "pm": "pm", "afternoon": "pm",
                 "day": "day", "all day": "day", "anytime": "day"}
_PART = {"am": "morning", "pm": "afternoon", "day": "day"}


def _as_date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _day_status(cfg: dict | None, day: date) -> str | None:
    """'closed' (not a working day), 'blackout', or None when the diary opens."""
    n = scheduling._norm(cfg)
    if day.weekday() not in n["working_days"]:
        return "closed"
    blackouts = {str(b)[:10] for b in ((cfg or {}).get("blackout_dates") or [])}
    if day.isoformat() in blackouts:
        return "blackout"
    return None


# ── get_availability ────────────────────────────────────────────────────────
def get_availability(tenant_id: str, client, args: dict) -> dict:
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    from app.booking_policy import compile_policy, refusal
    blocked = refusal(compile_policy(cfg))
    if blocked:
        return blocked
    start = _as_date(args.get("date_from")) or date.today()
    end = _as_date(args.get("date_to")) or start
    if end < start:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        end = start + timedelta(days=MAX_DAYS - 1)
    want = _WINDOW_WORDS.get(str(args.get("window") or "").strip().lower(),
                             str(args.get("window") or "").strip().lower()) or None
    merged = scheduling._merged(cfg)
    capacity = int(merged.get("capacity_per_window") or 0)
    buf = int(merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    svc_types = cfg.get("service_types") or []
    svc = services_mod.find(svc_types, args.get("service")) if args.get("service") else None
    if args.get("service") and svc_types and not svc:
        return {"error": "unknown_service",
                "message": "No service by that name — the menu is: "
                           + ", ".join(s.get("name", "") for s in svc_types if s.get("name"))}
    if svc:
        length = slotgrid.blocks_for_minutes(int(svc.get("minutes") or merged.get("slot_minutes") or 60))
        if svc.get("buffer"):
            buffer_blocks = -(-int(svc["buffer"]) // slotgrid.BLOCK_MIN)
    else:
        length = slotgrid.blocks_for_minutes(int(merged.get("slot_minutes") or 60))
    resource = None
    if svc and svc.get("resource"):
        try:
            from app import diary_config
            r = diary_config.find_resource(cfg.get("resource_types"), svc["resource"])
            if r:
                capacity, resource = int(r["count"]), r["name"]
        except Exception:
            pass
    timing = services_mod.timing(svc) if svc else ("half" if svc_types else "time")
    windows = ("day",) if timing == "day" else (("am", "pm") if timing == "half" else None)
    now = datetime.now(scheduling.DIARY_TZ)
    booked = db.list_active_bookings(tenant_id, (start - timedelta(days=1)).isoformat(),
                                     end.isoformat(), client=client)
    services_diary = str(cfg.get("windows") or "day") == "day" and bool(svc_types)
    customer_view: dict[tuple[str, str], int] = {}
    try:
        if services_diary:
            rows = scheduling.service_availability(
                cfg, booked, length, start, end, now, capacity=capacity, qty=1,
                buffer_blocks=buffer_blocks, resource=resource, timing=timing)
        else:
            rows = scheduling.availability(cfg, booked, start, end, now)
        for r in rows:
            customer_view[(str(r.get("date")), str(r.get("window")))] = int(r.get("remaining") or 0)
    except Exception:
        log.warning("get_availability: customer view failed", exc_info=True)
    days = []
    d = start
    while d <= end:
        status = _day_status(cfg, d)
        on_day = [b for b in booked if str(b.get("booking_date")) == d.isoformat()]
        entry = {"date": d.isoformat(), "weekday": d.strftime("%A"),
                 "bookings": len(on_day)}
        if status:
            entry["open"] = False
            entry["why"] = "not a working day" if status == "closed" else "blackout date"
        else:
            entry["open"] = True
            slots = []
            if services_diary and windows:
                for w in windows:
                    if want and w != want and want not in ("day",):
                        continue
                    sb = scheduling.first_fit_start(cfg, booked, length, d, w, None,
                                                    capacity=capacity, qty=1,
                                                    buffer_blocks=buffer_blocks, resource=resource)
                    slot = {"window": w, "label": _PART[w],
                            "fits": sb is not None,
                            "earliest_start": slotgrid.hhmm(int(sb)) if sb is not None else None}
                    cv = [v for (dd, ww), v in customer_view.items()
                          if dd == d.isoformat() and (ww == w or (w == "day"))]
                    if cv:
                        slot["remaining_for_customers"] = max(cv)
                    elif sb is not None:
                        slot["remaining_for_customers"] = 0
                        slot["note"] = "inside the notice window — you can place it, customers can't book it online"
                    slots.append(slot)
            elif services_diary:
                # exact-time service: list the customer-bookable starts
                starts = sorted(ww for (dd, ww) in customer_view if dd == d.isoformat())
                slots.append({"window": "time", "label": "start times",
                              "fits": bool(starts), "starts": starts[:12]})
            else:
                for (dd, ww), v in sorted(customer_view.items()):
                    if dd == d.isoformat():
                        slots.append({"window": ww, "label": ww, "fits": v > 0,
                                      "remaining_for_customers": v})
                if not slots:
                    slots.append({"window": "any", "fits": False,
                                  "note": "nothing customer-bookable that day (full, notice or horizon)"})
            entry["slots"] = slots
        days.append(entry)
        d += timedelta(days=1)
    result = {
        "service": svc.get("name") if svc else None,
        "length_minutes": length * slotgrid.BLOCK_MIN,
        "capacity_at_once": capacity,
        "gap_between_bookings_minutes": buf,
        "minimum_notice_hours": int(merged.get("min_notice_hours") or 0),
        "days": days,
        "count": len(days),
        "how_to_read": ("fits = the visit fits the diary's capacity and gaps (you can place it); "
                        "remaining_for_customers = what the online/email booking would offer "
                        "after the notice rule. Say what is booked AND whether it fits."),
    }
    # Live 2026-10-14: a diary whose configuration gives the engine no capacity to
    # count against came back with every open day fits:false and the "full, notice
    # or horizon" note, and the owner was told the diary was full. The engine lists
    # only windows that still have room, so an empty list is ALSO what a genuinely
    # full diary returns; the capacity check is what tells them apart. With none,
    # nothing can be offered however empty the diary is: that is "unknown", not full.
    if (capacity <= 0 and not customer_view and any(e.get("open") for e in days)
            and not any(s.get("fits") for e in days if e.get("open") for s in e.get("slots") or [])):
        result["availability_unknown"] = True
        result["how_to_read"] = (
            "The engine could not compute any bookable window for ANY day in this range: this is a "
            "diary configuration gap, NOT a full diary (bookings counts above are real; fits is "
            "unknown). Say you can't check availability from here and that the Diary page shows what "
            "fits; NEVER say the days are full, booked up or unavailable, and never guess at notice "
            "rules. For a move or a new visit, say you can't confirm the day and offer to email the "
            "customer or leave the choice to the owner.")
        for e in days:
            for s in e.get("slots") or []:
                if s.get("fits") is False and (s.get("note") or "").startswith("nothing customer-bookable"):
                    s["note"] = "availability unknown for this day (not computed)"
    return result


# ── get_contact_record ──────────────────────────────────────────────────────
def _resolve_contact(tenant_id: str, client, raw: str) -> tuple[dict | None, list[dict]]:
    """One contact by email or name; (None, candidates) when several match."""
    q = (raw or "").strip()
    if not q:
        return None, []
    rows = db.crm_list_contacts(tenant_id, q=q, limit=6, client=client) or []
    if "@" in q:
        exact = [r for r in rows if (r.get("email") or "").lower() == q.lower()]
        if exact:
            return exact[0], []
    if len(rows) == 1:
        return rows[0], []
    return None, rows


def get_contact_record(tenant_id: str, client, args: dict) -> dict:
    contact, candidates = _resolve_contact(tenant_id, client, str(args.get("contact") or ""))
    if contact is None:
        if candidates:
            return {"found": False, "which": [{"name": c.get("name"), "email": c.get("email"),
                                               "contact_id": c.get("id")} for c in candidates],
                    "message": "Several contacts match — ask which one."}
        return {"found": False, "message": "No contact matches that name or email."}
    today = date.today()
    out: dict = {"found": True, "contact_id": contact.get("id"), "name": contact.get("name"),
                 "email": contact.get("email"), "phone": contact.get("phone"),
                 "company": (contact.get("crm_companies") or {}).get("name") or contact.get("company"),
                 "sentiment": contact.get("sentiment"),
                 "last_activity": contact.get("last_activity_at")}
    if contact.get("notes"):
        out["notes"] = str(contact["notes"])[:600]
    # fields — the owner's own record; a never-email hint becomes a flag, not a refusal
    try:
        from app import crm_fields
        defs = [d for d in (db.crm_list_field_definitions(tenant_id, client=client) or [])
                if d.get("entity") == "contact"]
        custom = contact.get("custom_data") or {}
        fields = []
        for d in sorted(defs, key=lambda d: d.get("display_order") or 0):
            v = custom.get(d.get("key"))
            if v in (None, ""):
                continue
            if crm_fields.is_special_category(d):
                # Health details stay out of every AI prompt (security review
                # M39): say one is on record, never what it says.
                fields.append({"label": d.get("label"), "on_record": True,
                               "value": "(health detail — shown on the customer page only)"})
                continue
            f = {"label": d.get("label"), "value": crm_fields.display_value(d, v)}
            if _NEVER_EMAIL.search(str(d.get("extraction_hint") or d.get("hint") or "")):
                f["never_in_customer_email"] = True
            if isinstance(d.get("ladder"), dict):
                f["is_level"] = True
            fields.append(f)
        out["fields"] = fields
        expiry = crm_fields.expiry_lines(defs, contact, today)
        if expiry:
            out["expiring"] = expiry
    except Exception:
        log.warning("get_contact_record: fields failed", exc_info=True)
    # dependants (pets, cars, children, properties…)
    try:
        inst = db.list_requirement_instances(tenant_id, contact.get("id"), client=client) or []
        out["dependants"] = [{"kind": r.get("group_label"), "name": r.get("name"),
                              "facts": {k: v for k, v in (r.get("data") or {}).items()
                                        if v not in (None, "")}} for r in inst][:20]
    except Exception:
        log.warning("get_contact_record: dependants failed", exc_info=True)
    # documents on the record
    try:
        docs = db.list_documents(tenant_id, contact_email=contact.get("email"),
                                 contact_id=contact.get("id"), client=client) or []
        out["documents"] = [{"name": r.get("label") or r.get("filename"),
                             "type": r.get("content_type"), "date": str(r.get("created_at") or "")[:10]}
                            for r in docs][:20]
    except Exception:
        log.warning("get_contact_record: documents failed", exc_info=True)
    # open promises — what the business owes them / they owe the business
    try:
        proms = db.crm_open_promises(tenant_id, contact_id=contact.get("id"), client=client) or []
        out["open_promises"] = [{"who": ("we owe" if r.get("direction") == "we_owe" else "they owe"),
                                 "what": r.get("description") or r.get("text") or r.get("summary"),
                                 "due": str(r.get("due_date") or r.get("due_at") or "")[:10] or None}
                                for r in proms][:12]
    except Exception:
        log.warning("get_contact_record: promises failed", exc_info=True)
    # customer log (booking notes) — newest first
    try:
        from app import customer_log
        notes = customer_log.list_for_contact(tenant_id, contact.get("email"), limit=8, client=client)
        out["log"] = [{"date": str(n.get("created_at") or "")[:10], "by": n.get("author"),
                       "note": str(n.get("body") or "")[:200]} for n in notes]
    except Exception:
        log.warning("get_contact_record: log failed", exc_info=True)
    # credits / packages
    try:
        from app import wallet
        live = wallet.live(wallet.list_credits(tenant_id, contact.get("id"), client=client), today)
        out["credits"] = [{"package": r.get("package_name") or r.get("package_key"),
                           "remaining": wallet.remaining(r), "unit": r.get("unit"),
                           "expires": str(r.get("expires_at") or "")[:10] or None} for r in live]
    except Exception:
        log.warning("get_contact_record: credits failed", exc_info=True)
    # bookings: next and last
    try:
        email = contact.get("email") or ""
        rows = db.search_bookings(tenant_id, contact=email, limit=200, client=client) if email else []
        t = today.isoformat()
        future = [r for r in rows if str(r.get("booking_date")) >= t]
        past = [r for r in rows if str(r.get("booking_date")) < t]
        if future:
            n = min(future, key=lambda r: str(r.get("booking_date")))
            out["next_booking"] = {"date": n.get("booking_date"), "window": n.get("window"),
                                   "what": n.get("summary"), "reference": n.get("booking_ref"),
                                   "booking_id": n.get("id")}
        if past:
            p = max(past, key=lambda r: str(r.get("booking_date")))
            out["last_booking"] = {"date": p.get("booking_date"), "what": p.get("summary"),
                                   "reference": p.get("booking_ref")}
        out["live_bookings"] = len(future)
    except Exception:
        log.warning("get_contact_record: bookings failed", exc_info=True)
    return out


# ── list_dormant_contacts ───────────────────────────────────────────────────
def list_dormant_contacts(tenant_id: str, client, args: dict) -> dict:
    since = _as_date(args.get("since")) or (date.today() - timedelta(days=90))
    limit = max(1, min(int(args.get("limit") or 25), 50))
    contacts = db.crm_list_contacts(tenant_id, limit=500, client=client) or []
    # search_bookings clamps at 50 rows (a chat-sized read) — dormancy needs the
    # whole range, so it gets its own uncapped date-range read.
    recent = db.booking_contacts_between(tenant_id, since.isoformat(),
                                         (since + timedelta(days=400)).isoformat(), client=client)
    earlier = db.booking_contacts_between(tenant_id, (since - timedelta(days=730)).isoformat(),
                                          (since - timedelta(days=1)).isoformat(), client=client)
    active = {(r.get("contact") or "").lower() for r in recent if r.get("contact")}
    last_seen: dict[str, str] = {}
    for r in earlier:
        e = (r.get("contact") or "").lower()
        if e and str(r.get("booking_date")) > last_seen.get(e, ""):
            last_seen[e] = str(r.get("booking_date"))
    dormant = []
    for c in contacts:
        e = (c.get("email") or "").lower()
        if not e or e in active:
            continue
        dormant.append({"name": c.get("name"), "email": c.get("email"), "contact_id": c.get("id"),
                        "last_booking": last_seen.get(e), "last_activity": c.get("last_activity_at"),
                        "sentiment": c.get("sentiment")})
    dormant.sort(key=lambda d: (d["last_booking"] or "", str(d["last_activity"] or "")))
    return {"since": since.isoformat(), "contacts_checked": len(contacts),
            "active_since": len(active & {(c.get('email') or '').lower() for c in contacts}),
            "dormant": dormant[:limit], "count": len(dormant),
            "note": ("dormant = no booking on or after the date (last_booking = their most "
                     "recent one before it, null = never booked). Contacts who only ever "
                     "enquired appear here too.")}


# ── list_expiring ────────────────────────────────────────────────────────────
def list_expiring(tenant_id: str, client, args: dict) -> dict:
    """"Whose vaccinations run out this month?" (Kris 2026-09-23: "a way to
    monitor/check/remind for expiring documents"). Every expiring date across
    the whole book — a pet's jab, a car's MOT, a customer's own certificate —
    that has lapsed or lapses within `within_days`, soonest first. Each one
    names the owner and the next booking, and says whether it BLOCKS that
    booking: a gated date must cover the whole visit, so a jab that runs out
    during a stay stops it (the booking gate's own rule). The daily sweep
    (dependants_sweep) raises the Today cards; this is the owner asking."""
    from app import crm_fields, dates as dates_mod, requirement_groups as rg
    try:
        within = max(0, min(int(args.get("within_days") or 30), 400))
    except (TypeError, ValueError):
        within = 30
    want = " ".join(str(args.get("detail") or "").lower().split())
    today = datetime.now(scheduling.DIARY_TZ).date()
    horizon = today + timedelta(days=within)
    groups = {g.get("id"): g for g in rg.clean_groups(db.list_requirement_groups(tenant_id, client=client))}
    instances = db.list_gated_instances(tenant_id, client=client) if groups else []
    links = db.upcoming_bookings_by_instance(tenant_id, [i["id"] for i in instances],
                                             since=today.isoformat(), client=client) if instances else {}
    owners: dict[str, dict] = {}

    def owner(cid):
        if cid not in owners:
            owners[cid] = db.crm_get_contact(cid, tenant_id, client=client) or {}
        return owners[cid]

    def matches(label: str) -> bool:
        return not want or want in label.lower() or label.lower() in want

    out = []
    for inst in instances:
        g = groups.get(inst.get("group_id"))
        if not g:
            continue
        for f in g["fields"]:
            if f.get("durability") != "expires" or not matches(f.get("label") or ""):
                continue
            d = rg.parse_date((inst.get("data") or {}).get(f["key"]))
            if d is None or d > horizon:
                continue
            nxt = sorted((b for b in links.get(inst["id"], []) if b.get("booking_date")),
                         key=lambda b: b["booking_date"])
            row = {"what": g.get("label"), "name": inst.get("name"), "detail": f.get("label"),
                   "date": d.isoformat(), "date_words": dates_mod.pretty_date(d.isoformat()),
                   "lapsed": d < today, "days": (d - today).days}
            o = owner(inst.get("contact_id"))
            row["owner"] = o.get("name") or o.get("email")
            row["owner_email"] = o.get("email")
            if nxt:
                b = nxt[0]
                span = slotgrid.stay_span(b)
                ends = (slotgrid.abs_to_datetime(span[0] + span[1]).date() if span
                        else rg.parse_date(b["booking_date"]))
                row["next_booking"] = {"reference": b.get("booking_ref"), "date": b["booking_date"],
                                       "ends": ends.isoformat() if ends else None}
                row["blocks_next_booking"] = bool(f.get("gate")) and ends is not None and d < ends
            out.append(row)
    # A customer's own certificates (a first-aid cert, an NPLQ) — fields on the record.
    try:
        defs = crm_fields.expiring(db.crm_list_field_definitions(tenant_id, entity="contact", client=client) or [])
        if defs:
            for c in db.crm_contacts_with_data(tenant_id, client=client):
                data = c.get("custom_data") or {}
                for fd in defs:
                    if not matches(fd.get("label") or ""):
                        continue
                    d = crm_fields.expiry_date(fd, data.get(fd.get("key")))
                    if d is None or d > horizon:
                        continue
                    out.append({"what": "Customer", "name": c.get("name") or c.get("email"),
                                "detail": fd.get("label"), "date": d.isoformat(),
                                "date_words": dates_mod.pretty_date(d.isoformat()),
                                "lapsed": d < today, "days": (d - today).days,
                                "owner": c.get("name") or c.get("email"), "owner_email": c.get("email")})
    except Exception:
        log.warning("list_expiring: contact certificates failed", exc_info=True)
    out.sort(key=lambda r: r["date"])
    limit = max(1, min(int(args.get("limit") or 25), 50))
    return {"expiring": out[:limit], "count": len(out), "within_days": within,
            "today": today.isoformat(),
            "note": ("lapsed = already run out; days = days from today (negative = ago); "
                     "blocks_next_booking = the date runs out before that booking ENDS, so "
                     "the booking gate will hold it back until it's renewed.")}
