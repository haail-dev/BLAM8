"""Hand edits on a customer's record — ONE code path for both doors.

Kris, 2026-09-06: "this software is only automated — there's no discreet way
to manually add/update/change most stuff." The principle adopted: every
entity the automation writes must also be writable by hand, through the same
functions. The contact card's pencil (dashboard) and the Concierge's
update_contact kind both call the functions here, so there is one record and
one timeline line whichever door was used.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from app import crm_fields, db
from app.booking_errors import translate_capacity

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EditError(Exception):
    """A hand edit that can't be made — .args[0] is the sentence to show."""


def _stamp(tenant_id: str, contact_id: str, subject: str, line: str) -> None:
    db.crm_insert_activity(tenant_id, contact_id=contact_id, company_id=None,
                           activity_type="note", occurred_at=datetime.now(timezone.utc).isoformat(),
                           source_id=f"edit:{uuid.uuid4()}", subject=subject, summary=line)


def find_field(defs: list[dict], key_or_label: str) -> dict | None:
    """A contact field definition by key, exact label, or a loose label match."""
    want = " ".join((key_or_label or "").lower().split())
    if not want:
        return None
    for d in defs:
        if d.get("entity") != "contact":
            continue
        if str(d.get("key") or "").lower() == want or \
                " ".join(str(d.get("label") or "").lower().split()) == want:
            return d
    for d in defs:
        if d.get("entity") == "contact" and want in str(d.get("label") or "").lower():
            return d
    return None


def set_contact_field(tenant_id: str, contact: dict, field: dict, raw, *, via: str = "the record page",
                      client=None) -> tuple[str, str]:
    """Set one custom field on a contact (the human's confirmed edit: overwrite)
    and leave a timeline line saying what changed. Returns (old, new) as shown.
    An empty value clears the field. Raises EditError on a value the field
    can't take, or when nothing would change."""
    key = field.get("key")
    old = (contact.get("custom_data") or {}).get(key)
    old_shown = crm_fields.display_value(field, old)
    raw_s = "" if raw is None else str(raw).strip()
    if raw_s == "":
        if old in (None, ""):
            raise EditError(f"{field.get('label')} is already empty.")
        value, new_shown = "", "—"
    else:
        ok, value = crm_fields.coerce_value(field.get("field_type") or "text", raw_s,
                                            field.get("options") or None)
        if not ok:
            opts = field.get("options") or []
            raise EditError(f"'{raw_s}' isn't a valid {field.get('label')}"
                            + (f" — choose one of: {', '.join(map(str, opts))}" if opts
                               else f" (it takes a {field.get('field_type')})") + ".")
        if old not in (None, "") and str(old).strip() == str(value).strip():
            raise EditError(f"{field.get('label')} is already '{old_shown}'.")
        new_shown = crm_fields.display_value(field, value)
    db.crm_merge_custom_data("contact", contact["id"], tenant_id, {key: value},
                             client=client, overwrite=True)
    _stamp(tenant_id, contact["id"], "Record updated",
           f"{field.get('label')} set to '{new_shown}'"
           + (f" (was '{old_shown}')" if old not in (None, "") else "")
           + f" — by you, from {via}")
    db.log_agent_event(tenant_id, "record_updated",
                       f"You set {contact.get('name') or contact.get('email')}'s "
                       f"{field.get('label')} to '{new_shown}' ({via})",
                       contact=contact.get("email"), client=client)
    # A customer ladder moved by hand fires the same hook as an accepted chip
    # (src/app/ladders.py): trail + the level message.
    if isinstance(field.get("ladder"), dict):
        try:
            from app import ladders
            ladders.contact_field_changed(tenant_id, contact, key, old, value, client=client)
        except Exception:
            log.warning("ladder hook failed after a hand edit (contact=%s)", contact.get("id"), exc_info=True)
    return old_shown, new_shown


def add_contact_note(tenant_id: str, contact: dict, text: str, *, via: str = "the record page",
                     client=None) -> str:
    """A dated note on the customer's timeline. Returns the cleaned text."""
    body = " ".join(str(text or "").split())[:600]
    if not body:
        raise EditError("The note is empty.")
    _stamp(tenant_id, contact["id"], "Note", body)
    db.log_agent_event(tenant_id, "record_updated",
                       f"You added a note to {contact.get('name') or contact.get('email')}'s record ({via})",
                       contact=contact.get("email"), client=client)
    return body


def edit_contact_basics(tenant_id: str, contact: dict, *, name: str | None = None,
                        email: str | None = None, phone: str | None = None,
                        via: str = "the record page", client=None) -> dict:
    """Name / email / phone by hand. Only the changed columns are written;
    an email must look like one and not belong to another contact. Returns
    the changes as {column: (old, new)}."""
    changes: dict[str, tuple] = {}
    if name is not None:
        n = " ".join(str(name).split())[:120]
        if n and n != (contact.get("name") or ""):
            changes["name"] = (contact.get("name") or "", n)
    if email is not None:
        e = str(email).strip().lower()[:254]
        if e and e != (contact.get("email") or "").lower():
            if not _EMAIL_RE.match(e):
                raise EditError(f"'{e}' doesn't look like an email address.")
            other = db.crm_contact_by_email(tenant_id, e, client=client)
            if other and other.get("id") != contact.get("id"):
                raise EditError(f"{e} already belongs to {other.get('name') or 'another contact'}.")
            changes["email"] = (contact.get("email") or "", e)
    if phone is not None:
        p = " ".join(str(phone).split())[:40]
        if p != (contact.get("phone") or ""):
            changes["phone"] = (contact.get("phone") or "", p)
    if not changes:
        raise EditError("Nothing changed.")
    db.crm_update_contact(contact["id"], tenant_id, {k: v[1] for k, v in changes.items()})
    what = "; ".join(f"{k} '{v[0] or '—'}' → '{v[1] or '—'}'" for k, v in changes.items())
    _stamp(tenant_id, contact["id"], "Record updated", f"{what} — by you, from {via}")
    db.log_agent_event(tenant_id, "record_updated",
                       f"You edited {contact.get('name') or contact.get('email')}'s record: {what} ({via})",
                       contact=changes.get("email", (None, contact.get("email")))[1], client=client)
    return changes


# ── dependants: a subject or asset on the customer's record ────────────────
def _dep_shown(value) -> str:
    return "—" if value in (None, "") else str(value)


def plan_dependant_field(group: dict, instance: dict, label: str, raw) -> tuple[dict, object]:
    """Which of the dependant's fields `label` means and the value it would
    take, checked the way the form checks it. Returns (field, value); raises
    EditError with the sentence to show. Writes nothing."""
    from app import requirement_groups as rgroups
    fields = group.get("fields") or []
    f = rgroups.field_for(group, label)
    if not f:
        # "care" → Care instructions: every word said is in exactly one label.
        words = set(re.findall(r"[a-z0-9]+", str(label or "").lower()))
        hits = [x for x in fields
                if words and words <= set(re.findall(r"[a-z0-9]+", str(x.get("label") or "").lower()))]
        f = hits[0] if len(hits) == 1 else None
    if not f:
        names = ", ".join(str(x.get("label")) for x in fields)
        raise EditError(f"A {str(group.get('label') or 'record').lower()} has no '{label}' — "
                        f"the details are: {names or 'none yet'}.")
    raw_s = "" if raw is None else str(raw).strip()
    t, opts = f.get("type"), [str(o) for o in f.get("options") or []]
    if raw_s == "":
        value = ""
    elif opts:
        # A fixed list (a ladder's levels, a dropdown) takes one of its own
        # words — "Expert" is not a Stay level.
        value = next((o for o in opts if o.lower() == raw_s.lower()), None)
        if value is None:
            raise EditError(f"'{raw_s}' isn't a {f.get('label')} — choose one of: {', '.join(opts)}.")
    elif t == "date":
        # A gated date blocks bookings when it runs out: it has to be a date,
        # never "next week" kept as text.
        d = rgroups.parse_date(raw_s)
        if d is None:
            raise EditError(f"{f.get('label')} needs a date, like 21 September 2027 (got '{raw_s}').")
        value = d.isoformat()
    else:
        value = rgroups.coerce_value(f, raw_s)
        if t == "number" and not isinstance(value, (int, float)):
            raise EditError(f"{f.get('label')} takes a number (got '{raw_s}').")
    old = (instance.get("data") or {}).get(f["key"])
    if str(old or "").strip() == str(value or "").strip():
        raise EditError(f"{instance.get('name')}'s {f.get('label')} is already '{_dep_shown(old)}'.")
    return f, value


async def save_dependant(tenant_id: str, contact: dict, group: dict, name: str, raw: dict, *,
                         via: str = "the record page", client=None) -> dict | None:
    """Add or edit one dependant (a subject or asset) on a customer — the
    owner's entry, which overrides whatever was read (source 'owner'). ONE path
    for both doors (the by-hand principle, 2026-09-06): the contact card's form
    and the Concierge's update_dependant card. `raw` is {field key: value}; an
    empty value clears that field. Leaves a timeline line saying what changed,
    answers any chip still offering to create it, and fires a ladder move
    (src/app/ladders.py) exactly as the form always has."""
    from app import dependants_extract as _dx, ladders, requirement_groups as rgroups
    cid = contact["id"]
    existing = next((i for i in db.list_requirement_instances(tenant_id, cid, client=client)
                     if i.get("group_id") == group["id"]
                     and str(i.get("name") or "").lower() == name.lower()), None)
    before = dict((existing or {}).get("data") or {})
    data, meta, _c = rgroups.merge(before, (existing or {}).get("field_meta"),
                                   rgroups.normalise_data(group, raw), group, source="owner")
    for k, v in raw.items():                       # an empty box clears the field
        if str(v or "").strip() == "" and k in data:
            data.pop(k)
            meta.pop(k, None)
    row = db.upsert_requirement_instance(tenant_id, group["id"], cid, name, data, meta, client=client)
    if row:
        # A chip still offering to create this one is answered by the row.
        _dx.absorb_new_chips(tenant_id, cid, group, row, client=client)
    changed = [f for f in group.get("fields") or [] if before.get(f["key"]) != data.get(f["key"])]
    if existing and changed:
        what = "; ".join(f"{f.get('label')} set to '{_dep_shown(data.get(f['key']))}'"
                         + (f" (was '{before.get(f['key'])}')" if before.get(f["key"]) not in (None, "") else "")
                         for f in changed)
        _stamp(tenant_id, cid, "Record updated", f"{name}: {what} — by you, from {via}")
        db.log_agent_event(tenant_id, "record_updated",
                           f"You updated {name} ({contact.get('name') or contact.get('email')}): {what} ({via})",
                           contact=contact.get("email"), client=client)
    # A ladder moved by hand is a level change (src/app/ladders.py).
    for f, old, new in ladders.changed_ladder(group, before, data):
        await ladders.on_level_change(tenant_id, contact, row or {**(existing or {}), "name": name},
                                      group, f, old, new, client=client)
    return row


# ── stays: extend, shorten or move (docs/plan-stay-date-changes.md) ─────────
def stay_times(booking: dict) -> tuple[datetime, datetime] | None:
    """(drop-off, collection) for a STAY row — a grid span that runs past a
    day. None for a same-day visit or an unmapped row."""
    from app import slotgrid
    span = slotgrid.stay_span(booking)
    if not span:
        return None
    arr = slotgrid.abs_to_datetime(span[0])
    dep = slotgrid.abs_to_datetime(span[0] + span[1])
    if dep.date() <= arr.date():
        return None
    return arr, dep


def _parse_when(raw, fallback: datetime) -> datetime:
    """'2026-10-05' keeps the fallback's clock time; '2026-10-05T17:00' or
    '2026-10-05 17:00' sets both. Raises EditError on nonsense."""
    s = str(raw or "").strip().replace("T", " ")
    if not s:
        return fallback
    try:
        if len(s) <= 10:
            d = datetime.fromisoformat(s[:10]).date()
            return datetime.combine(d, fallback.time())
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        raise EditError(f"'{raw}' isn't a date I can place — say the day, or the day and time like 5 Oct 17:00.")


def _words_at(dt: datetime) -> str:
    """'Monday 5 October 2026, 17:00' — the intake's own wording for a date."""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')}, {dt.strftime('%H:%M')}"


def _stay_words(arr: datetime, dep: datetime) -> str:
    from app import dates as dates_mod
    return (f"{dates_mod.pretty_date(arr.date().isoformat())} {arr.strftime('%H:%M')} → "
            f"{dates_mod.pretty_date(dep.date().isoformat())} {dep.strftime('%H:%M')}")


def plan_stay_change(tenant_id: str, booking: dict, *, new_arrival=None, new_departure=None,
                     client=None) -> dict:
    """Where a stay would run after the owner changes its drop-off and/or
    collection — extend, shorten and move are one change. Checks, every one
    an existing rule: the row is live and the new collection is still ahead;
    room on every night of the new span in the stay's own resource size, with
    its own row excluded; check-in / check-out times and the minimum stay; a
    gated dependant date covering the NEW collection; and the new price with
    what it does to money. Returns the plan; raises EditError with the
    sentence to show. Writes nothing."""
    from app import dates as dates_mod, diary_config, requirement_groups as rgroups, scheduling, slotgrid
    if booking.get("status") not in ("proposed", "confirmed"):
        raise EditError("That booking isn't in the live diary any more.")
    cur = stay_times(booking)
    if not cur:
        raise EditError("That isn't a stay — it's a single-day booking. Move it instead.")
    old_arr, old_dep = cur
    arr = _parse_when(new_arrival, old_arr)
    dep = _parse_when(new_departure, old_dep)
    now = datetime.now(scheduling.DIARY_TZ).replace(tzinfo=None)
    if dep <= now:
        raise EditError(f"{dates_mod.pretty_date(dep.date().isoformat())} has already passed.")
    if dep <= arr:
        raise EditError("The collection has to come after the drop-off.")
    if arr != old_arr and arr < now and old_arr >= now:
        raise EditError("The new drop-off has already passed.")
    if arr == old_arr and dep == old_dep:
        raise EditError("That's how the stay already is.")
    nights = dep.date().toordinal() - arr.date().toordinal()
    old_nights = old_dep.date().toordinal() - old_arr.date().toordinal()
    if nights <= 0:
        raise EditError("A stay needs at least one night — for a same-day visit, use the same-day booking flow.")
    cfg_row = db.get_scheduling_config(tenant_id, client=client) or {}
    merged = scheduling._merged(cfg_row)
    # The house rules on an overnight stay: check-in / check-out times, the minimum.
    dh = diary_config.clean_day_hours(cfg_row.get("day_hours"))
    ci, co = dh.get("checkin_after"), dh.get("checkout_before")
    if ci and arr != old_arr and arr.strftime("%H:%M") < ci:
        raise EditError(f"Check-in is from {ci} — the drop-off can't be {arr.strftime('%H:%M')}.")
    if co and dep != old_dep and dep.strftime("%H:%M") > co:
        raise EditError(f"Check-out is by {co} — the collection can't be {dep.strftime('%H:%M')}.")
    sp = diary_config.clean_stay_pricing(cfg_row.get("stay_pricing"))
    if sp["min_nights"] and nights < sp["min_nights"]:
        raise EditError(f"The minimum stay is {sp['min_nights']} night{'s' if sp['min_nights'] != 1 else ''} "
                        f"— that would leave {nights}.")
    # Room on every night, in its own resource size, with its own row out of the count.
    resource = booking.get("resource_type") or None
    rts = scheduling.stay_resource_types(cfg_row)
    rate, rate_unit = None, None
    if rts and resource:
        r = diary_config.find_resource(rts, resource)
        if r:
            capacity, rate, rate_unit = int(r["count"]), r.get("rate"), r.get("rate_unit")
        else:
            capacity = int(merged["capacity_per_window"])
    elif rts and len(rts) == 1:
        resource, capacity = rts[0]["name"], int(rts[0]["count"])
        rate, rate_unit = rts[0].get("rate"), rts[0].get("rate_unit")
    else:
        capacity = int(merged["capacity_per_window"])
    qty = int(booking.get("quantity") or 1)
    booked = [b for b in (db.list_active_bookings(
        tenant_id, (arr.date() - timedelta(days=365)).isoformat(), dep.date().isoformat(), client=client) or [])
        if b.get("id") != booking.get("id")]
    spans = scheduling._pool_spans(booked, cfg_row, resource, exclude_typed=resource is None)
    buf = int(merged.get("buffer_minutes") or 0)
    buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
    offer = slotgrid.stay_offer(spans, arr, dep, capacity, qty=qty, buffer_blocks=buffer_blocks)
    if not offer["fits"]:
        # Which night is the problem: the first one the new span adds that
        # has no room on its own (the owner then knows how far they CAN go).
        full = None
        day = arr.date()
        while day < dep.date():
            a_ = max(arr, datetime.combine(day, datetime.min.time()))
            b_ = min(dep, datetime.combine(day + timedelta(days=1), datetime.min.time()))
            if b_ > a_ and not slotgrid.stay_offer(spans, a_, b_, capacity, qty=qty,
                                                   buffer_blocks=buffer_blocks)["fits"]:
                full = day
                break
            day += timedelta(days=1)
        where = f" in {resource}" if resource else ""
        raise EditError(f"There's no room{where} for every night of {_stay_words(arr, dep)}"
                        + (f" — the night of {dates_mod.pretty_date(full.isoformat())} is full." if full else "."))
    # A gated dependant date has to cover the whole NEW stay (the booking gate's own rule).
    try:
        subjects = db.subjects_for_bookings(tenant_id, [booking["id"]], client=client).get(booking["id"], [])
        groups = {g.get("id"): g for g in rgroups.clean_groups(db.list_requirement_groups(tenant_id, client=client))}
        for inst in subjects:
            g = groups.get(inst.get("group_id"))
            if not g:
                continue
            gated = {str(f.get("label")) for f in g.get("fields") or [] if f.get("gate")}
            gaps = [x for x in rgroups.missing_for(g, inst.get("data") or {}, arr.date().isoformat(), None,
                                                   until=dep.date().isoformat())
                    if any(x == lab or x.startswith(lab + " (") for lab in gated)]
            if gaps:
                raise EditError(f"{inst.get('name')} can't be booked to {dates_mod.pretty_date(dep.date().isoformat())}"
                                f" — {', '.join(gaps)} doesn't cover it. Ask for the up-to-date record first.")
    except EditError:
        raise
    except Exception:
        log.warning("stay change: dependant gate read failed (booking=%s)", booking.get("id"), exc_info=True)
    # The price, and what the change does to money.
    length = slotgrid.datetime_to_abs(dep) - slotgrid.datetime_to_abs(arr)
    days = max(1, -(-length // slotgrid.BLOCKS_PER_DAY))
    if rate is not None:
        quote = diary_config.quote_stay({"rate": rate, "rate_unit": rate_unit or "night"}, nights, days, qty)
    else:
        quote = diary_config.quote_stay(cfg_row.get("stay_pricing"), nights, days, qty)
    price_new = float(quote["amount"]) if quote else None
    price_old = float(booking["quoted_amount"]) if booking.get("quoted_amount") is not None else None
    invoiced = booking["id"] in db.invoiced_booking_ids(tenant_id, [booking["id"]], client=client)
    kind = "extend" if (arr == old_arr and dep > old_dep) else ("shorten" if (arr == old_arr and dep < old_dep) else "move")
    return {"stay": True, "kind": kind, "new_date": arr.date().isoformat(),
            "new_start_block": slotgrid.block_at(arr.time()), "length_blocks": int(length),
            "arrival": arr.isoformat(timespec="minutes"), "departure": dep.isoformat(timespec="minutes"),
            "old_arrival": old_arr.isoformat(timespec="minutes"), "old_departure": old_dep.isoformat(timespec="minutes"),
            "when": _stay_words(arr, dep), "old_when": _stay_words(old_arr, old_dep),
            "nights": nights, "old_nights": old_nights, "delta_nights": nights - old_nights,
            "resource": resource, "price_old": price_old, "price_new": price_new, "invoiced": invoiced,
            "extra_amount": (round(price_new - price_old, 2)
                             if invoiced and price_new is not None and price_old is not None and price_new > price_old
                             else None)}


async def apply_stay_change(tenant_id: str, booking: dict, plan: dict, *, via: str = "the diary",
                            client=None) -> str:
    """Write the plan: the span in place (same reference, details, deposit,
    calendar link), the details' own drop-off/collection words, the price —
    a stay not yet invoiced takes the new price; an invoiced one that grew
    gets a DRAFT invoice for the extra nights and the original is never
    touched; one that shrank is flagged, no automatic refund — then the
    calendar, the log and the customer's timeline. Returns the Done sentence."""
    from app import calendar_sync, dates as dates_mod
    from app import scheduling
    readiness, _ = scheduling.make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise EditError(blocked["message"])
    bid = booking["id"]
    with translate_capacity(EditError):
        row = db.move_booking_in_place(bid, tenant_id, plan["new_date"], int(plan["new_start_block"]),
                                       int(plan["length_blocks"]), "day", client=client)
    if not row:
        raise EditError("The stay is unchanged — it's no longer in the diary.")
    arr = datetime.fromisoformat(plan["arrival"])
    dep = datetime.fromisoformat(plan["departure"])
    # The details carry the dates in words wherever the intake put them.
    extra = {}
    for k in (booking.get("details") or {}):
        kl = k.lower()
        if "drop" in kl or "arriv" in kl or "check-in" in kl or "check in" in kl:
            extra[k] = _words_at(arr)
        elif "collect" in kl or "depart" in kl or "check-out" in kl or "check out" in kl:
            extra[k] = _words_at(dep)
    money = ""
    # The stay's own price always follows its nights (test run 3.1, Kris
    # 2026-09-24: "did the cost change on the extension?" — it hadn't: an
    # invoiced stay kept its old £427 and only the draft invoice knew). What
    # has already been INVOICED is never rewritten; the invoices below settle
    # the difference.
    if plan.get("price_new") is not None:
        db.set_booking_quote(bid, tenant_id, float(plan["price_new"]), client=client)
    if plan.get("invoiced"):
        if plan.get("extra_amount"):
            ref_line = f"Extra nights — {booking.get('summary') or 'stay'} now {plan['when']}"
            inv = db.create_invoice(
                tenant_id, booking.get("contact"), None, None, None,
                [{"booking_id": None, "description": ref_line, "amount": float(plan["extra_amount"]),
                  "quantity": 1, "unit_price": float(plan["extra_amount"])}],
                client=client, booking_id=bid,
                notes=f"The stay ({booking.get('booking_ref')}) was extended by {plan['delta_nights']} night(s); "
                      f"{db.EXTRA_NIGHTS_NOTE}.", created_by="owner")
            money = ((f" The stay is now £{float(plan['price_new']):,.2f}."
                      if plan.get("price_new") is not None else "")
                     + f" A draft invoice for the extra £{float(plan['extra_amount']):,.2f} is ready"
                     + (f" ({inv.get('invoice_ref')})" if inv and inv.get("invoice_ref") else "") + ".")
        elif plan.get("delta_nights", 0) < 0:
            # Shorter again: the extra-night DRAFTS an extension raised are
            # cancelled first, newest first, as far as the nights removed
            # cover them (3.2: back to the 26th left INV-1019 billing a night
            # nobody was staying). Only what's left needs a refund by hand.
            removed = -int(plan["delta_nights"])
            cancelled = []
            for d in db.extra_night_drafts(tenant_id, bid, client=client):
                m = re.search(r"extended by (\d+) night", d.get("notes") or "")
                n = int(m.group(1)) if m else 0
                if not n or n > removed:
                    continue
                db.set_invoice_status(d["id"], tenant_id, "cancelled", client=client)
                cancelled.append(d.get("invoice_ref") or "the draft")
                removed -= n
                if not removed:
                    break
            if cancelled:
                money = (f" The extra-night draft invoice{'s' if len(cancelled) > 1 else ''} "
                         f"{', '.join(cancelled)} {'are' if len(cancelled) > 1 else 'is'} cancelled.")
            if removed:
                money += " The stay was already invoiced — adjust that invoice by hand if you're refunding."
    elif (plan.get("price_old") is not None and plan.get("price_new") is not None
          and plan["price_new"] != plan["price_old"]):
        money = f" Price £{plan['price_old']:,.2f} → £{plan['price_new']:,.2f}."
    if extra:
        try:
            db.update_booking_details(bid, tenant_id, extra, client=client)
        except Exception:
            log.warning("stay change: details words not updated (booking=%s)", bid, exc_info=True)
    try:
        await calendar_sync.remove_pushed_event(tenant_id, bid)
        if (row or {}).get("status") == "confirmed" or booking.get("status") == "confirmed":
            await calendar_sync.push_bookings(tenant_id, booking_id=bid)
    except Exception:
        log.warning("stay change: calendar refresh failed (booking=%s)", bid, exc_info=True)
    what = booking.get("summary") or f"{booking.get('contact') or 'The'} stay"
    verb = {"extend": "extended", "shorten": "shortened", "move": "moved"}.get(plan.get("kind"), "changed")
    db.log_agent_event(tenant_id, "stay_changed",
                       f"You {verb} {booking.get('booking_ref') or what}: {plan['old_when']} → {plan['when']} "
                       f"({plan['nights']} night{'s' if plan['nights'] != 1 else ''}) ({via})",
                       contact=booking.get("contact"), booking_id=bid, booking_ref=booking.get("booking_ref"),
                       event_date=plan["new_date"], client=client)
    try:
        c = db.crm_contact_by_email(tenant_id, booking.get("contact") or "", client=client) if booking.get("contact") else None
        if c:
            _stamp(tenant_id, c["id"], f"Stay {verb}",
                   f"{booking.get('booking_ref') or what}: {plan['old_when']} → {plan['when']} — by you, from {via}")
    except Exception:
        log.warning("stay change: timeline line failed (booking=%s)", bid, exc_info=True)
    return f"Done — {what} now runs {plan['when']} ({plan['nights']} night{'s' if plan['nights'] != 1 else ''}).{money}"


# ── bookings: move by hand, edit the summary ────────────────────────────────
_WINDOW_WORDS = {"am": "am", "morning": "am", "pm": "pm", "afternoon": "pm",
                 "day": "day", "all day": "day", "anytime": "day", "any": "day"}
_PART = {"am": "morning", "pm": "afternoon", "day": "day"}


def is_services_diary(cfg: dict | None) -> bool:
    """A diary whose bookings sit on the slot grid (windows='day' + a service
    menu): 'afternoon' means a start time, not a half-day bucket."""
    return bool(cfg) and str(cfg.get("windows") or "day") == "day" \
        and bool(cfg.get("service_types"))


def plan_booking_move(tenant_id: str, booking: dict, new_date: str, when: str, *,
                      client=None) -> dict:
    """Where a booking would land if moved by the OWNER to `new_date` at
    `when` (morning / afternoon / day / HH:MM / a legacy window). On a
    services diary the earliest start that fits (capacity + the gap between
    jobs; the customer notice rule does not bind the owner's own diary);
    on a legacy windows diary the window itself. Returns the plan; raises
    EditError with the sentence to show when it can't be placed."""
    from datetime import date as _date, timedelta as _td
    from app import dates as dates_mod, scheduling, slotgrid
    if booking.get("status") not in ("proposed", "confirmed"):
        raise EditError("That booking isn't in the live diary any more.")
    if stay_times(booking):
        # A move only knows a start: on a stay it would shift the drop-off and
        # lose the collection (docs/plan-stay-date-changes.md).
        raise EditError("That's a stay — change its drop-off or collection dates instead of moving it.")
    try:
        day = _date.fromisoformat(str(new_date)[:10])
    except (TypeError, ValueError):
        raise EditError("Give the new day as a date.")
    if day < _date.today():
        raise EditError(f"{dates_mod.pretty_date(day)} has already passed.")
    cfg = db.get_scheduling_config(tenant_id, client=client) or {}
    w = str(when or "").strip().lower()
    w = _WINDOW_WORDS.get(w, w)
    if is_services_diary(cfg):
        if ":" not in w and w not in ("am", "pm", "day"):
            raise EditError(f"'{when}' isn't a time I can place — say morning, afternoon, "
                            "or a time like 14:00.")
        merged = scheduling._merged(cfg)
        length = int(booking.get("length_blocks") or 0) or slotgrid.blocks_for_minutes(
            int(merged.get("slot_minutes") or 60))
        buf = int(merged.get("buffer_minutes") or 0)
        buffer_blocks = -(-buf // slotgrid.BLOCK_MIN) if buf > 0 else 0
        booked = [b for b in db.list_active_bookings(
            tenant_id, (day - _td(days=1)).isoformat(), day.isoformat(), client=client)
            if b.get("id") != booking["id"]]            # its own old slot never blocks it
        sb = scheduling.first_fit_start(cfg, booked, length, day, w, None,
                                        capacity=int(merged.get("capacity_per_window") or 0),
                                        buffer_blocks=buffer_blocks)
        if sb is None:
            part = _PART.get(w, f"slot at {w}")
            hours = length * slotgrid.BLOCK_MIN / 60
            raise EditError(f"{dates_mod.pretty_date(day)} has no room in the {part} for a "
                            f"{hours:g}-hour visit (capacity or the gap between jobs).")
        at = slotgrid.hhmm(int(sb))
        if (day.isoformat() == str(booking.get("booking_date"))[:10]
                and int(sb) == int(booking.get("start_block") or -1)):
            raise EditError("That's where it already is.")
        # The stored window keeps the diary's own vocabulary: a clock time lands
        # in its half (the seeded rows read am/pm; the engine reads the block).
        stored = w if w in ("am", "pm", "day") else ("am" if int(sb) < slotgrid.NOON_BLOCK else "pm")
        return {"timed": True, "new_date": day.isoformat(), "new_window": stored,
                "new_start_block": int(sb), "length_blocks": int(length),
                "when": f"{dates_mod.pretty_date(day)} at {at}", "at": at}
    from datetime import datetime as _dt
    err = scheduling.hold_error(cfg, day, w, _dt.now(scheduling.DIARY_TZ))
    if err == "invalid_window":
        valid = scheduling._norm(cfg)["windows"]
        raise EditError(f"'{when}' isn't a bookable time here — this diary takes "
                        f"{', '.join(valid[:8])}.")
    if err:
        raise EditError(f"{dates_mod.pretty_date(day)} isn't bookable ({err.replace('_', ' ')}).")
    if not any(o["date"] == day.isoformat() and o["window"] == w
               for o in scheduling.check_availability(tenant_id, day, day, client=client)):
        raise EditError(f"{dates_mod.pretty_date(day)} {w} is already taken.")
    if day.isoformat() == str(booking.get("booking_date"))[:10] and w == booking.get("window"):
        raise EditError("That's where it already is.")
    return {"timed": False, "new_date": day.isoformat(), "new_window": w,
            "when": f"{dates_mod.pretty_date(day)} ({w})"}


async def apply_booking_move(tenant_id: str, booking: dict, plan: dict, *,
                             via: str = "the diary", client=None) -> str:
    """Move the booking per the plan — in place (same reference, details,
    linkage), the calendar event re-pushed, an agent event logged. Returns
    the Done sentence. Raises EditError when the slot filled up meanwhile."""
    from app import calendar_sync, dates as dates_mod, scheduling
    from app import scheduling
    readiness, _ = scheduling.make_executor(tenant_id, source="manual")
    blocked = readiness("booking_readiness", {})
    if blocked.get("error"):
        raise EditError(blocked["message"])
    bid = booking["id"]
    if plan.get("timed"):
        with translate_capacity(EditError):
            row = db.move_booking_in_place(bid, tenant_id, plan["new_date"], int(plan["new_start_block"]),
                                           int(plan["length_blocks"]), plan["new_window"], client=client)
        if not row:
            raise EditError("The booking is unchanged — that slot filled up in the meantime.")
    else:
        row, err = scheduling.move_booking(tenant_id, bid, plan["new_date"], plan["new_window"])
        if err:
            raise EditError("The booking is unchanged — that slot "
                            + ("filled up in the meantime." if err == "full"
                               else f"isn't bookable ({err.replace('_', ' ')})."))
    try:
        await calendar_sync.remove_pushed_event(tenant_id, bid)
        if (row or {}).get("status") == "confirmed" or booking.get("status") == "confirmed":
            await calendar_sync.push_bookings(tenant_id, booking_id=bid)
    except Exception:
        log.warning("move by hand: calendar refresh failed (booking=%s)", bid, exc_info=True)
    what = booking.get("summary") or f"{booking.get('contact') or 'The'} booking"
    old = dates_mod.pretty_date(booking.get("booking_date"))
    if booking.get("start_block") is not None:
        from app import slotgrid
        old += f" at {slotgrid.hhmm(int(booking['start_block']))}"
    elif booking.get("window"):
        old += f" ({booking.get('window')})"
    db.log_agent_event(tenant_id, "booking_moved",
                       f"You moved {booking.get('booking_ref') or what} from {old} to {plan['when']} ({via})",
                       contact=booking.get("contact"), booking_id=bid,
                       booking_ref=booking.get("booking_ref"), event_date=plan["new_date"], client=client)
    return f"Done — {what} moved to {plan['when']}."


def edit_booking_summary(tenant_id: str, booking: dict, summary: str, *, via: str = "the diary",
                         client=None) -> str:
    text = " ".join(str(summary or "").split())[:200]
    if not text:
        raise EditError("The booking needs a name.")
    if text == (booking.get("summary") or ""):
        raise EditError("Nothing changed.")
    db.rename_booking(booking["id"], tenant_id, text, client=client)
    db.log_agent_event(tenant_id, "booking_edited",
                       f"You renamed {booking.get('booking_ref') or 'a booking'}: "
                       f"'{booking.get('summary') or ''}' → '{text}' ({via})",
                       contact=booking.get("contact"), booking_id=booking["id"],
                       booking_ref=booking.get("booking_ref"),
                       event_date=str(booking.get("booking_date") or "")[:10] or None, client=client)
    return text
