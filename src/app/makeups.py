"""Make-ups — slice 6 of docs/plan-progress-packages.md (2026-09-05).

The classes module's loudest money leak (docs/benchmark-class-software.md:
"make-ups lost money", parents booking a make-up "at 10 p.m. on a Sunday")
and the rule documented by the researched classes businesses: a class
missed WITH NOTICE may be made up, once a term, within so many weeks, subject
to space. Run #7's portal line 7 ("Finn can't make Saturday 3 October — can
he have a make-up session?") had the rule stated honestly from the knowledge
base and no way to book against it. This is the way.

A make-up is a ONE-OFF booking bound to a MISSED OCCURRENCE — the register's
own word: the missed class is a booking_attendance row marked `excused` (told
in time; `late_cancel` / `no_show` only when the business allows it). The
agent and the portal book it through the ordinary hold with `makeup_for` =
the missed booking's reference; the executor checks the rule, stamps the
booking "Make-up for BK-1006", prices it per the setting and records the pair
in `makeups`, so the second request for the same miss — or a second make-up
in the same block — is refused with the reason.

Every knob is a setting on Diary settings → Cancellations & credits →
Make-ups (`scheduling_config.makeups`): on/off; how many per participant per
block (1; 0 = no limit); the notice the miss needed (24 h — stated to the
customer; the register's `excused` is the business's own judgement of it);
within how many days of the miss (42); whether a no-show or late cancel
also earns one (no); whether a make-up is free (yes).
"""
from __future__ import annotations

import copy
import logging
from datetime import date, datetime, timedelta

from app import db
from app.dates import pretty_date

log = logging.getLogger(__name__)

ELIGIBLE_ALWAYS = ("excused",)
ELIGIBLE_IF_ALLOWED = ("late_cancel", "no_show")


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
    return {"on": _b(d, "on", False),
            "per_term": _i(d.get("per_term"), 0, 12, 1),
            "notice_hours": _i(d.get("notice_hours"), 0, 336, 24),
            "within_days": _i(d.get("within_days"), 0, 365, 42),
            "no_show_ok": _b(d, "no_show_ok", False),
            "free": _b(d, "free", True)}


def settings_of(sched_cfg: dict | None) -> dict:
    return clean((sched_cfg or {}).get("makeups"))


def on(sched_cfg: dict | None) -> bool:
    return settings_of(sched_cfg)["on"]


def face_value(c: dict) -> str:
    if not c["on"]:
        return "no make-ups"
    per = "unlimited" if not c["per_term"] else f"{c['per_term']} per block"
    within = f"within {c['within_days']} days" if c["within_days"] else "any time"
    return f"make-ups {per} · {c['notice_hours']} h notice · {within}" + (" · free" if c["free"] else "")


def eligible_statuses(c: dict) -> tuple:
    return ELIGIBLE_ALWAYS + (ELIGIBLE_IF_ALLOWED if c["no_show_ok"] else ())


def _n(nouns: dict | None) -> dict:
    return nouns or {"booking": "booking", "participant": "customer"}


def render_for_prompt(d, nouns: dict | None = None) -> str:
    c = clean(d)
    if not c["on"]:
        return ""
    b = _n(nouns)["booking"]
    per = ("as often as needed" if not c["per_term"]
           else f"{'once' if c['per_term'] == 1 else str(c['per_term']) + ' times'} per block/term per participant")
    within = f"within {c['within_days']} days of the missed {b}" if c["within_days"] else "while the block runs"
    return (f"Make-ups — a {b} missed with at least " + str(c["notice_hours"]) + " hours' notice may be "
            "made up " + per + ", " + within + ", subject to space"
            + (", at no charge" if c["free"] else "") + f". The customer's record lists the missed {b}s "
            "that qualify (\"Make-ups available\"); ONLY those qualify. To book one: check_availability "
            "for the same service, then hold_booking for ONE date with `makeup_for` set to the missed "
            "booking's reference (no repeat). A miss not on the list is not a make-up: say what the rule "
            "is and, if they told the business in time, that the register decides.")


def with_makeups(tools) -> list[dict]:
    """Deep-copy a tool list and give the booking-placing tools a `makeup_for`
    argument. Module constants stay byte-stable."""
    out = []
    for t in tools or []:
        if t.get("name") in ("hold_booking", "create_booking") and (t.get("input_schema") or {}).get("properties") is not None:
            t = copy.deepcopy(t)
            t["input_schema"]["properties"]["makeup_for"] = {
                "type": "string",
                "description": ("ONLY for a MAKE-UP of a missed class: the missed booking's reference "
                                "(e.g. BK-1006) from the customer's 'Make-ups available' list. Books one "
                                "date, priced per the make-up rule.")}
        out.append(t)
    return out


# ── the pure rule ────────────────────────────────────────────────────────────
def term_key(booking: dict) -> str:
    if booking.get("recurrence_id"):
        return str(booking["recurrence_id"])
    try:
        d = date.fromisoformat(str(booking.get("booking_date"))[:10])
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    except (TypeError, ValueError):
        return "unknown"


def told_in_time(b: dict, c: dict) -> bool:
    """6b nit: the notice rule, enforced when the business recorded WHEN the
    customer told them (`notified_at`); unknown = the register's own judgement
    stands (excused means excused)."""
    told = b.get("notified_at")
    if not told or not c.get("notice_hours"):
        return True
    try:
        start = datetime.fromisoformat(f"{str(b.get('booking_date'))[:10]}T{(str(b.get('window') or '00:00')[:5] if ':' in str(b.get('window') or '') else '00:00')}:00")
        told_dt = datetime.fromisoformat(str(told).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return True
    return told_dt <= start - timedelta(hours=int(c["notice_hours"]))


def eligible(misses: list[dict], used: list[dict], c: dict, today: date) -> list[dict]:
    """Pure: the missed occurrences a customer may make up today.
    `misses` = bookings with an `attendance` status attached (and optional
    `instance_id`); `used` = makeups rows already recorded. Returns
    [{booking, until, term}], oldest miss first."""
    if not c["on"]:
        return []
    ok = eligible_statuses(c)
    used_for = {u.get("missed_booking_id") for u in used if u.get("status", "booked") == "booked"}
    per_term_used: dict[tuple, int] = {}
    for u in used:
        if u.get("status", "booked") == "booked":
            k = (u.get("instance_id") or "", u.get("term_key") or "")
            per_term_used[k] = per_term_used.get(k, 0) + 1
    out = []
    for b in sorted(misses, key=lambda x: str(x.get("booking_date") or "")):
        if b.get("attendance") not in ok or b.get("id") in used_for:
            continue
        if b.get("attendance") == "excused" and not c["no_show_ok"] and not told_in_time(b, c):
            continue
        try:
            missed_on = date.fromisoformat(str(b.get("booking_date"))[:10])
        except (TypeError, ValueError):
            continue
        until = missed_on + timedelta(days=c["within_days"]) if c["within_days"] else None
        if until and until < today:
            continue
        tk = term_key(b)
        k = (b.get("instance_id") or "", tk)
        if c["per_term"] and per_term_used.get(k, 0) >= c["per_term"]:
            continue
        out.append({"booking": b, "until": until, "term": tk})
    return out


def refusal(ref: str, misses: list[dict], used: list[dict], c: dict, today: date,
            nouns: dict | None = None) -> str:
    """Why a given reference does not qualify — in words the agent can pass on."""
    word = _n(nouns)["booking"]
    b = next((m for m in misses if str(m.get("booking_ref") or "").upper() == ref.upper()), None)
    if b is None:
        return f"{ref} is not one of this customer's {word}s."
    if b.get("attendance") == "excused" and not c["no_show_ok"] and not told_in_time(b, c):
        return (f"{ref} was excused, but the business was told less than {c['notice_hours']} hours before "
                f"the {word} — under the rule that earns no make-up.")
    if b.get("attendance") not in eligible_statuses(c):
        return (f"{ref} is not marked as missed with notice on the register"
                + (f" (it is marked {b['attendance'].replace('_', ' ')})" if b.get("attendance") else "")
                + f" — only a {word} the business marked excused"
                + (", late cancel or no-show" if c["no_show_ok"] else "") + " earns a make-up.")
    if b.get("id") in {u.get("missed_booking_id") for u in used if u.get("status", "booked") == "booked"}:
        u = next(x for x in used if x.get("missed_booking_id") == b.get("id"))
        return f"{ref} has already been made up" + (f" ({u.get('makeup_ref')})" if u.get("makeup_ref") else "") + "."
    try:
        missed_on = date.fromisoformat(str(b.get("booking_date"))[:10])
        if c["within_days"] and missed_on + timedelta(days=c["within_days"]) < today:
            return f"{ref} was missed on {pretty_date(missed_on.isoformat())} — the {c['within_days']}-day window has passed."
    except (TypeError, ValueError):
        pass
    if c["per_term"]:
        return f"The make-up allowance for this block ({c['per_term']} per participant) has been used."
    return f"{ref} does not qualify for a make-up."


# ── I/O ──────────────────────────────────────────────────────────────────────
def _c(client=None):
    return client or db.db()


def misses_for(tenant_id: str, contact_email: str | None, *, client=None) -> list[dict]:
    """The customer's bookings with the register's word attached."""
    if not contact_email:
        return []
    rows = [b for b in db.bookings_for_contact(tenant_id, contact_email, client=client)
            if b.get("status") == "confirmed"]
    if not rows:
        return []
    ids = [b["id"] for b in rows]
    try:
        att = {a["booking_id"]: a for a in (_c(client).table("booking_attendance").select("booking_id,status,notified_at")
                                             .eq("tenant_id", tenant_id).in_("booking_id", ids).execute().data or [])}
    except Exception:
        att = {}
    try:
        subs = db.subjects_for_bookings(tenant_id, ids, client=client)
    except Exception:
        subs = {}
    try:
        recs = {r["id"]: r.get("recurrence_id") for r in (_c(client).table("bookings").select("id,recurrence_id")
                                                             .eq("tenant_id", tenant_id).in_("id", ids)
                                                             .not_.is_("recurrence_id", "null").execute().data or [])}
    except Exception:
        recs = {}
    out = []
    for b in rows:
        s = (subs.get(b["id"]) or [{}])[0]
        out.append({**b, "attendance": (att.get(b["id"]) or {}).get("status"),
                    "notified_at": (att.get(b["id"]) or {}).get("notified_at"),
                    "instance_id": s.get("id"), "participant": s.get("name"),
                    "recurrence_id": recs.get(b["id"])})
    return out


def used_for(tenant_id: str, contact_email: str | None, *, client=None) -> list[dict]:
    if not contact_email:
        return []
    try:
        return (_c(client).table("makeups").select("*").eq("tenant_id", tenant_id)
                .eq("contact_email", contact_email).execute().data or [])
    except Exception:
        log.warning("makeups read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def available(tenant_id: str, contact_email: str | None, c: dict, *, today: date | None = None,
              client=None) -> list[dict]:
    today = today or date.today()
    return eligible(misses_for(tenant_id, contact_email, client=client),
                    used_for(tenant_id, contact_email, client=client), c, today)


def check(tenant_id: str, contact_email: str | None, ref: str, c: dict, *, today: date | None = None,
          client=None, nouns: dict | None = None) -> tuple[dict | None, dict | None]:
    """(the missed booking, None) when `ref` may be made up now, else (None, error)."""
    if not c["on"]:
        return None, {"error": "no_makeups", "message": "This business does not offer make-up classes."}
    if not contact_email:
        return None, {"error": "no_contact", "message": "The customer's email is needed to check their make-ups."}
    ref = " ".join(str(ref or "").split()).upper()
    today = today or date.today()
    misses = misses_for(tenant_id, contact_email, client=client)
    used = used_for(tenant_id, contact_email, client=client)
    for item in eligible(misses, used, c, today):
        if str(item["booking"].get("booking_ref") or "").upper() == ref:
            return item["booking"], None
    return None, {"error": "not_a_makeup", "message": refusal(ref, misses, used, c, today, nouns)
                  + " Do not book it as a make-up; explain the rule."}


def record(tenant_id: str, missed: dict, makeup_row: dict, c: dict, *, contact_email: str | None,
           client=None) -> dict | None:
    row = {"tenant_id": tenant_id, "contact_email": contact_email or missed.get("contact"),
           "instance_id": missed.get("instance_id"), "missed_booking_id": missed["id"],
           "missed_ref": missed.get("booking_ref"), "makeup_booking_id": makeup_row["id"],
           "makeup_ref": makeup_row.get("booking_ref"), "term_key": term_key(missed), "status": "booked"}
    try:
        res = _c(client).table("makeups").insert(row).execute()
        row = res.data[0] if res.data else row
    except Exception:
        log.warning("makeups insert failed (tenant=%s)", tenant_id, exc_info=True)
        return None
    if c["free"]:
        try:
            db.set_booking_quote(makeup_row["id"], tenant_id, 0.0, client=client)
        except Exception:
            pass
    db.log_agent_event(tenant_id, "makeup_booked",
                       f"{makeup_row.get('booking_ref') or 'A booking'} is a make-up for "
                       f"{missed.get('booking_ref')} ({pretty_date(missed.get('booking_date'))})"
                       + (f" — {missed.get('participant')}" if missed.get("participant") else ""),
                       contact=contact_email or missed.get("contact"), booking_id=makeup_row.get("id"),
                       booking_ref=makeup_row.get("booking_ref"), event_date=makeup_row.get("booking_date"))
    return row


def on_attendance(tenant_id: str, booking: dict, status: str, *, client=None) -> dict | None:
    """6b nit: the make-up's own register. A make-up marked excused / missed
    is released — its `makeups` row turns 'missed', so the original miss may be
    made up again under the rule; attended (or any other status) re-books it."""
    try:
        rows = (_c(client).table("makeups").select("*").eq("tenant_id", tenant_id)
                .eq("makeup_booking_id", booking["id"]).execute().data or [])
    except Exception:
        return None
    if not rows:
        return None
    new_status = "missed" if status in ("excused", "no_show", "late_cancel") else "booked"
    if rows[0].get("status") == new_status:
        return rows[0]
    try:
        _c(client).table("makeups").update({"status": new_status}).eq("id", rows[0]["id"]).execute()
    except Exception:
        log.warning("makeups feedback failed (tenant=%s)", tenant_id, exc_info=True)
        return None
    if new_status == "missed":
        db.log_agent_event(tenant_id, "makeup_booked",
                           f"{booking.get('booking_ref')} (the make-up for {rows[0].get('missed_ref')}) was missed — "
                           f"{rows[0].get('missed_ref')} may be made up again under the rule",
                           contact=booking.get("contact"), booking_id=booking.get("id"), booking_ref=booking.get("booking_ref"))
    return {**rows[0], "status": new_status}


def lines(items: list[dict]) -> list[str]:
    out = []
    for it in items:
        b = it["booking"]
        svc = ((b.get("details") or {}).get("Service")) or b.get("summary") or "booking"
        who = f", {b['participant']}" if b.get("participant") else ""
        out.append(f"{b.get('booking_ref')} ({svc}, missed {pretty_date(b.get('booking_date'))}{who})"
                   + (f" — book by {pretty_date(it['until'].isoformat())}" if it.get("until") else ""))
    return out


def context_block(tenant_id: str, contact_email: str | None, sched_cfg: dict | None, *,
                  today: date | None = None, client=None) -> str:
    c = settings_of(sched_cfg)
    if not c["on"]:
        return ""
    from app import diary_config
    word = diary_config.nouns_of(sched_cfg)["booking"]
    ls = lines(available(tenant_id, contact_email, c, today=today, client=client))
    if not ls:
        return (f"Make-ups available: none — no missed {word} on this customer's record qualifies yet "
                "(a miss counts once the business marks it excused on the register).")
    return "Make-ups available (book with hold_booking + makeup_for):\n" + "\n".join("- " + ln for ln in ls)


def job_context(tenant_id: str, booking: dict, sched_cfg: dict | None, *, today: date | None = None,
                client=None) -> dict:
    """The job page: is THIS occurrence a miss that earns a make-up, has it been
    used, or is this booking itself a make-up?"""
    if sched_cfg is None:
        try:
            sched_cfg = db.get_scheduling_config(tenant_id, client=client)
        except Exception:
            sched_cfg = {}
    c = settings_of(sched_cfg)
    out = {"makeups_on": c["on"], "makeup": None}
    if not c["on"]:
        return out
    try:
        rows = (_c(client).table("makeups").select("*").eq("tenant_id", tenant_id)
                .or_(f"missed_booking_id.eq.{booking['id']},makeup_booking_id.eq.{booking['id']}")
                .execute().data or [])
    except Exception:
        rows = []
    is_makeup = next((r for r in rows if r.get("makeup_booking_id") == booking["id"]), None)
    used = next((r for r in rows if r.get("missed_booking_id") == booking["id"]), None)
    if is_makeup:
        out["makeup"] = {"kind": "is_makeup", "for": is_makeup.get("missed_ref")}
        return out
    if used:
        out["makeup"] = {"kind": "used", "by": used.get("makeup_ref")}
        return out
    if booking.get("contact"):
        for it in available(tenant_id, booking["contact"], c, today=today, client=client):
            if it["booking"].get("id") == booking["id"]:
                out["makeup"] = {"kind": "available", "until": it["until"]}
                break
    return out
