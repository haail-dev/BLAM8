"""Expiry chases for dependants (docs/modules/module-requirement-groups.md Phase 2).

A field marked `expires` with the gate on is a date that must cover the
booking: a certificate or an insurance renewal. The booking gate already
refuses a hold when it has lapsed; this sweep is the OTHER half — the
warning before it bites. Once a day, for every tenant with a group: each
live instance whose gated date lapses within the window (or has lapsed)
and that has an upcoming booking raises one feed row per (instance, field,
booking) — "Murphy's Certificate expiry lapses 12th Oct, before his visit on
the 15th" — so Today's deck and the push carry it, and the owner (or the
customer, by reply) sorts it before the day. A new date read from the reply
(dependants_extract) is the stand-down: the next sweep finds nothing to say.

Pure planning (`due`) is unit-tested; the sweep is the thin I/O around it."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app import db, push, requirement_groups as rg
from app.dates import pretty_date

log = logging.getLogger(__name__)

WINDOW_DAYS = 14        # warn this far ahead of the lapse
RECALL_DAYS = 30        # an ungated expiry is a RECALL: nudge a month out (the DVSA's reminder lands at one month)
KIND = "dependant_expiry"
KIND_CONTACT = "certificate_expiry"      # 4b nit: a customer's own certificate, its own chip


def due(groups: list[dict], instances: list[dict], bookings_by_instance: dict[str, list[dict]],
        today: date) -> list[dict]:
    """Pure: which (instance, field, booking) triples need a word today.
    `bookings_by_instance` maps instance id → its upcoming live bookings
    (booking_date ISO, booking_ref). A field counts when it has a date and
    that date is before the booking OR within WINDOW_DAYS of today; the
    earliest booking is the one named."""
    by_gid = {g.get("id"): g for g in rg.clean_groups(groups)}
    out = []
    for inst in instances or []:
        g = by_gid.get(inst.get("group_id"))
        if not g:
            continue
        upcoming = sorted((b for b in bookings_by_instance.get(inst.get("id"), [])
                           if b.get("booking_date") and b["booking_date"] >= today.isoformat()),
                          key=lambda b: b["booking_date"])
        for f in g["fields"]:
            if f.get("durability") != "expires":
                continue
            d = rg.parse_date((inst.get("data") or {}).get(f["key"]))
            if d is None:
                continue
            if not f.get("gate"):
                # An UNGATED expiry is the reason to book, not a reason to refuse
                # (an asset's inspection, an asset's service): the recall. Nudge when it is
                # due within RECALL_DAYS or has lapsed AND nothing is in the diary
                # for this dependant yet — once a visit is booked there is
                # nothing to say (run #6, 2026-09-04: the services rig's bread-and-
                # butter message had no path; both recall jobs are OS-gated).
                if upcoming or d > today + timedelta(days=int(f.get("recall_days") or RECALL_DAYS)):
                    continue
                out.append({"instance": inst, "group": g, "field": f, "date": d,
                            "booking": None, "lapsed": d < today,
                            "before_booking": False, "recall": True})
                continue
            if not upcoming:
                continue
            nxt = upcoming[0]
            bd = rg.parse_date(nxt["booking_date"])
            lapsed_before_booking = bd is not None and d < bd
            soon = d <= today + timedelta(days=WINDOW_DAYS)
            if not (lapsed_before_booking or soon):
                continue
            out.append({"instance": inst, "group": g, "field": f, "date": d,
                        "booking": nxt, "lapsed": d < today,
                        "before_booking": lapsed_before_booking})
    return out


def phrase(item: dict) -> tuple[str, str]:
    """(title, body) in the operator's words."""
    name = item["instance"].get("name")
    # The label as the owner wrote it: "HSG282 certificate expiry", not
    # "hsg282 certificate expiry" (live 2026-09-04) — only a plain Capitalised
    # label folds to lower case mid-sentence.
    raw = item["field"]["label"]
    label = raw.lower() if raw[:1].isupper() and raw[1:] == raw[1:].lower() else raw
    when = pretty_date(item["date"].isoformat())
    if item.get("recall"):
        if item["lapsed"]:
            title = f"{name}'s {label} lapsed {when}"
            body = f"Nothing in the diary for {name} — worth offering a slot now."
        else:
            title = f"{name}'s {label} is due {when}"
            body = f"Nothing booked for {name} yet — a good moment to offer a slot."
        return title, body
    ref = item["booking"].get("booking_ref") or "the booking"
    on = pretty_date(item["booking"].get("booking_date"))
    if item["lapsed"]:
        title = f"{name}'s {label} has lapsed"
        body = f"Lapsed {when} — before {ref} on {on}. The booking will be held back until it's renewed."
    elif item["before_booking"]:
        title = f"{name}'s {label} lapses before {ref}"
        body = f"Lapses {when}; {ref} is on {on}. Ask for the new date before then."
    else:
        title = f"{name}'s {label} lapses {when}"
        body = f"Next booking {ref} on {on} — worth asking for the renewal now."
    return title, body


def due_contacts(definitions: list[dict], contacts: list[dict],
                 bookings_by_email: dict[str, list[dict]], today: date) -> list[dict]:
    """Pure (slice 4, certificates on contacts): each contact whose expiring
    field (a first-aid certificate, a lifeguard NPLQ, a dive medical) lapses
    within the field's own recall lead — or has lapsed — and who has nothing
    upcoming in the diary. A booked course is the stand-down: the renewal is
    in hand."""
    from app import crm_fields
    defs = crm_fields.expiring(definitions)
    if not defs:
        return []
    out = []
    for c in contacts or []:
        email = (c.get("email") or "").lower()
        upcoming = [b for b in bookings_by_email.get(email, [])
                    if b.get("booking_date") and b["booking_date"] >= today.isoformat()]
        if upcoming:
            continue
        data = c.get("custom_data") or {}
        for d in defs:
            due = crm_fields.expiry_date(d, data.get(d.get("key")))   # + the field's validity
            if due is None:
                continue
            lead = int(d.get("recall_days") or RECALL_DAYS)
            if due > today + timedelta(days=lead):
                continue
            out.append({"contact": c, "field": d, "date": due, "lapsed": due < today})
    return out


def phrase_contact(item: dict) -> tuple[str, str]:
    name = item["contact"].get("name") or item["contact"].get("email") or "The customer"
    raw = item["field"]["label"]
    label = raw.lower() if raw[:1].isupper() and raw[1:] == raw[1:].lower() else raw
    when = pretty_date(item["date"].isoformat())
    # The copy takes the field's own label (run #8: "offer a renewal" on a
    # theory test pass — a theory test is resat, not renewed).
    if item["lapsed"]:
        return (f"{name}'s {label} lapsed {when}",
                f"Nothing in the diary for {name} — worth getting in touch about their {label} now.")
    return (f"{name}'s {label} is due {when}",
            f"Nothing booked for {name} yet — a good moment to get in touch about their {label}.")


async def _contact_certificates(today: date) -> int:
    """The contact side of the daily job (slice 4)."""
    raised = 0
    try:
        tenants = db.tenants_with_expiring_contact_fields()
    except Exception:
        log.warning("certificate sweep: tenant list failed", exc_info=True)
        return 0
    for tenant_id in tenants:
        try:
            defs = db.crm_list_field_definitions(tenant_id, entity="contact")
            contacts = db.crm_contacts_with_data(tenant_id)
            links = db.upcoming_bookings_by_email(tenant_id, since=today.isoformat())
            for item in due_contacts(defs, contacts, links, today):
                tag = f"contact:{item['contact']['id']}:{item['field']['key']}"
                if db.notification_exists_today(tenant_id, KIND_CONTACT, tag=tag):
                    continue
                title, body = phrase_contact(item)
                push.notify(tenant_id, title=title, body=body,
                            url=f"/app/crm/contacts/{item['contact']['id']}#{tag}",
                            tag=f"cert-{item['contact']['id']}-{item['field']['key']}",
                            kind=KIND_CONTACT, contact=item["contact"].get("email"),
                            contact_name=item["contact"].get("name"))
                raised += 1
        except Exception:
            log.warning("certificate sweep failed (tenant=%s)", tenant_id, exc_info=True)
    return raised


async def run_expiry_chases(today: date | None = None) -> int:
    """The daily job. Returns how many rows it raised."""
    today = today or datetime.now().date()
    raised = 0
    try:
        tenants = db.tenants_with_requirement_groups()
    except Exception:
        log.warning("dependant expiry sweep: tenant list failed", exc_info=True)
        return 0
    for tenant_id in tenants:
        try:
            groups = db.list_requirement_groups(tenant_id)
            instances = db.list_gated_instances(tenant_id)
            if not instances:
                continue
            links = db.upcoming_bookings_by_instance(tenant_id, [i["id"] for i in instances],
                                                     since=today.isoformat())
            for item in due(groups, instances, links, today):
                booking_id = (item["booking"] or {}).get("id") or "recall"
                tag = f"{item['instance']['id']}:{item['field']['key']}:{booking_id}"
                if db.notification_exists_today(tenant_id, KIND, tag=tag):
                    continue
                title, body = phrase(item)
                contact = db.crm_get_contact(item["instance"].get("contact_id"), tenant_id) or {}
                push.notify(tenant_id, title=title, body=body,
                            url=f"/app/crm/contacts/{item['instance'].get('contact_id')}#{tag}",
                            tag=f"dep-{item['instance']['id']}-{item['field']['key']}",
                            kind=KIND, contact=contact.get("email"),
                            contact_name=contact.get("name"))
                raised += 1
        except Exception:
            log.warning("dependant expiry sweep failed (tenant=%s)", tenant_id, exc_info=True)
    # Certificates on contacts (slice 4): the same recall for the customer's
    # own expiring dates.
    raised += await _contact_certificates(today)
    return raised
