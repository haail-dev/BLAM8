"""Packages and the wallet — slice 2 of docs/plan-progress-packages.md (2026-09-05).

A PACKAGE is a product the business sells ahead of use — ten service hours,
a block of eight sessions, a ten-class pass, a six-week class block — and
the WALLET is what the customer has left of it: bought N, used M, valid until
D, paid or not. The research (docs/research-adult-classes.md §2, §4) put this
first among the things the adult side adds and loudest among the complaints:
credits that expire by a rule nobody saw, prepaid hours not refunded, a
membership that cannot be renewed, "which sessions are prepaid".

Every decision is a setting with a research-backed default (plan §Slice 2):
the packages themselves live on Invoice settings (`invoice_settings.extras`),
HOW a credit is spent lives on Diary settings (`scheduling_config.credits`):
automatically when the class happens (the incumbents' way) or only when the
register is marked; the late-cancel window; whether a no-show spends; whether
an excused absence gives the unit back; whether hours spend actual minutes.
One thing is not a setting (Kris): credits belong to the CUSTOMER, and a line
may be "for <participant>" so a spend picks that participant's credits first.

Objects: `wallet_credits` (one row per package sold — or per hand adjustment),
`wallet_spend` (one row per booking that spent), `booking_attendance` (the
register: attended / no_show / late_cancel / excused). Credits mint when the
invoice is ISSUED and read "unpaid" until it is paid. A credit never spends
twice for one booking; expired credits never spend; the register's exception
overrides the automatic spend for that booking either way.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from app import db
from app.dates import pretty_date

log = logging.getLogger(__name__)

UNITS = ("session", "hour")
SPEND_MODES = ("auto", "register")
HOURS_MODES = ("minutes", "hour")
ATTENDANCE = ("attended", "no_show", "late_cancel", "excused")
KIND = "package_balance"               # feed / push / agent_events / email_log kind
MAX_PACKAGES = 12
AUTO_SPEND_LOOKBACK_DAYS = 60          # the sweep never reaches further back than this

PURPOSE = ("A short, warm note about the customer's package balance, in the business's "
           "voice, stating the facts exactly as given: how many sessions or hours are left "
           "of which package, and when they run out if a date is given. One line inviting "
           "them to top up or book if the facts say the business sells more; otherwise "
           "nothing else — no prices you were not given, no questions. Never guess a "
           "pronoun from a name.")


# ── the settings ─────────────────────────────────────────────────────────────
def _norm(s) -> str:
    return " ".join(re.sub(r"[_\W]+", " ", str(s or "").lower()).split())


def key_for(name: str) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    return k[:40] or "package"


def _f(v, default=0.0) -> float:
    try:
        return float(str(v).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "required", "y")


def clean_packages(raw) -> list[dict]:
    """The package list as stored: [{key, name, quantity, unit, price, valid_days,
    services, required}]. A row needs a name and a positive quantity."""
    out, seen = [], set()
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        name = " ".join(str(p.get("name") or "").split())[:80]
        qty = _f(p.get("quantity"), 0)
        if not name or qty <= 0 or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        unit = str(p.get("unit") or "session").strip().lower()
        unit = unit if unit in UNITS else ("hour" if unit.startswith("h") else "session")
        services = p.get("services") or []
        if isinstance(services, str):
            services = re.split(r"[;,\n]+", services)
        services = [" ".join(str(s).split())[:60] for s in services if str(s).strip()][:20]
        out.append({"key": str(p.get("key") or "").strip() or key_for(name), "name": name,
                    "quantity": round(qty, 2), "unit": unit,
                    "price": round(max(0.0, _f(p.get("price"), 0)), 2),
                    "valid_days": max(0, min(3650, _i(p.get("valid_days"), 0))),
                    "services": services, "required": _truthy(p.get("required"))})
        if len(out) >= MAX_PACKAGES:
            break
    return out


def parse_packages_form(form) -> list[dict]:
    """The Packages face's positional arrays → clean packages."""
    names = form.getlist("pkg_name") if hasattr(form, "getlist") else []
    def col(k):
        return form.getlist(k) if hasattr(form, "getlist") else []
    rows = []
    for i, name in enumerate(names):
        def at(xs):
            return xs[i] if i < len(xs) else ""
        rows.append({"name": name, "quantity": at(col("pkg_quantity")), "unit": at(col("pkg_unit")),
                     "price": at(col("pkg_price")), "valid_days": at(col("pkg_valid_days")),
                     "services": at(col("pkg_services")), "required": at(col("pkg_required")),
                     "key": at(col("pkg_key"))})
    return clean_packages(rows)


def packages_of(settings_row: dict | None) -> list[dict]:
    return clean_packages(((settings_row or {}).get("extras") or {}).get("packages"))


def clean_prefs(d) -> dict:
    d = d or {}
    return {"paid_only": bool(d.get("paid_only")),
            "low_at": max(0, min(99, _i(d.get("low_at"), 1))),
            "expiry_days": max(0, min(365, _i(d.get("expiry_days"), 14))),
            # Show the balance on the portal (2b nit): stored since slice 2, now honoured; default on.
            "portal": bool(d.get("portal", True)) if "portal" in d else True}


def prefs_of(settings_row: dict | None) -> dict:
    return clean_prefs(((settings_row or {}).get("extras") or {}).get("package_prefs"))


def clean_credits(d) -> dict:
    """Diary settings → Cancellations & credits: how a credit is spent."""
    d = d or {}
    spend = str(d.get("spend") or "auto").strip().lower()
    hours = str(d.get("hours") or "minutes").strip().lower()
    return {"spend": spend if spend in SPEND_MODES else "auto",
            "late_hours": max(0, min(336, _i(d.get("late_hours"), 24))),
            "no_show_spends": bool(d.get("no_show_spends", True)) if "no_show_spends" in d else True,
            "excused_refunds": bool(d.get("excused_refunds", True)) if "excused_refunds" in d else True,
            # A move inside the late-cancel window: charged as a late cancel plus a
            # fresh booking (the norm, Kris 2026-09-06), or free — the business's call.
            "late_move_charged": bool(d.get("late_move_charged", True)) if "late_move_charged" in d else True,
            "hours": hours if hours in HOURS_MODES else "minutes"}


def credits_of(sched_cfg: dict | None) -> dict:
    return clean_credits((sched_cfg or {}).get("credits"))


def find_package(packages: list[dict], key_or_name) -> dict | None:
    k = str(key_or_name or "").strip()
    if not k:
        return None
    for p in packages:
        if p["key"] == k:
            return p
    n = _norm(k)
    for p in packages:
        if _norm(p["name"]) == n:
            return p
    # A package named INSIDE a longer description ("Autumn block — Block of 10
    # sessions for Isla") — only when exactly one package fits (2b nit).
    hits = sorted((p for p in packages if _norm(p["name"]) and _norm(p["name"]) in n),
                  key=lambda p: -len(_norm(p["name"])))
    if hits:
        # "Block of 10 private" also contains "Block of 10": the longest name wins
        # when every other hit is a part of it; two unrelated fits is no match.
        top = _norm(hits[0]["name"])
        return hits[0] if all(_norm(p["name"]) in top for p in hits[1:]) else None
    # The name's words all present but spread out ("10-hour session
    # block" for "10-hour block" — the concierge's model restates a package
    # in its own words, run #8 2026-09-05). Only when exactly ONE package's
    # every word is in the description; a single-word name is too loose.
    words = set(n.split())
    loose = [p for p in packages
             if len(_norm(p["name"]).split()) >= 2 and set(_norm(p["name"]).split()) <= words]
    return loose[0] if len(loose) == 1 else None


def covers(package: dict, service: str | None) -> bool:
    if not package.get("services"):
        return True
    s = _norm(service)
    return any(_norm(x) == s or (_norm(x) and _norm(x) in s) for x in package["services"])


def unit_words(unit: str, n: float) -> str:
    n = round(float(n), 2)
    label = "hour" if unit == "hour" else "session"
    shown = ("%g" % n)
    return f"{shown} {label}{'' if n == 1 else 's'}"


# ── the wallet (I/O) ─────────────────────────────────────────────────────────
def _c(client=None):
    return client or db.db()


def list_credits(tenant_id: str, contact_id: str | None, client=None) -> list[dict]:
    if not contact_id:
        return []
    try:
        res = (_c(client).table("wallet_credits").select("*")
               .eq("tenant_id", tenant_id).eq("contact_id", contact_id)
               .order("created_at").execute())
        return res.data or []
    except Exception:
        log.warning("wallet_credits read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def remaining(row: dict) -> float:
    return round(_f(row.get("quantity"), 0) - _f(row.get("used"), 0), 2)


def expired(row: dict, today: date) -> bool:
    e = row.get("expires_at")
    return bool(e) and str(e)[:10] < today.isoformat()


def live(rows: list[dict], today: date, *, paid_only: bool = False) -> list[dict]:
    return [r for r in rows if remaining(r) > 0 and not expired(r, today)
            and (r.get("paid") or not paid_only)]


def balance_lines(rows: list[dict], today: date, *, paid_only: bool = False) -> list[str]:
    """The balance in the customer's own words, one line per live package row."""
    out = []
    for r in live(rows, today, paid_only=paid_only):
        left = remaining(r)
        total = _f(r.get("quantity"), 0)
        name = r.get("package_name") or "credits"
        line = f"{unit_words(r.get('unit') or 'session', left)}"
        line += f" of {('%g' % total)} left on {name}" if total > left else f" on {name}"
        if r.get("expires_at"):
            line += f", valid until {pretty_date(str(r['expires_at'])[:10])}"
        if not r.get("paid"):
            line += " (unpaid)"
        out.append(line)
    return out


def context_block(tenant_id: str, contact_id: str | None, *, today: date | None = None,
                  client=None, nouns: dict | None = None) -> str:
    """The wallet as the drafter and the portal read it: quote the balance,
    never guess. Empty when the customer has nothing on record."""
    today = today or date.today()
    rows = list_credits(tenant_id, contact_id, client=client)
    lines = balance_lines(rows, today)
    if not lines:
        return ""
    word = (nouns or {}).get("booking") or "booking"
    return ("Packages on record (credits the customer has bought — quote the balance from "
            f"here, never guess; a spent unit comes off after each {word}):\n"
            + "\n".join("- " + ln for ln in lines))


def card_context(tenant_id: str, contact_id: str | None, *, today: date | None = None,
                 client=None) -> dict:
    """The contact card's Packages & credits section — best-effort, so a
    tenant that never sells packages pays one settings read and nothing else."""
    try:
        packages = packages_of(db.get_invoice_settings(tenant_id, client=client))
    except Exception:
        packages = []
    rows = list_credits(tenant_id, contact_id, client=client) if (packages or contact_id) else []
    return {"wallet_rows": rows, "wallet_packages": packages,
            "wallet_lines": balance_lines(rows, today or date.today())}


def job_context(tenant_id: str, booking: dict, *, register_on: bool = False,
                today: date | None = None, client=None) -> dict:
    """The job page's Register block — shown when the business sells packages
    or keeps dependants (a class register is useful without credits too).
    Best-effort: a tenant with neither pays one settings read."""
    try:
        packages = packages_of(db.get_invoice_settings(tenant_id, client=client))
    except Exception:
        packages = []
    on = bool(packages) or register_on
    lines: list[str] = []
    if packages and booking.get("contact"):
        try:
            c = db.crm_contact_by_email(tenant_id, booking["contact"], client=client)
            if c:
                lines = balance_lines(list_credits(tenant_id, c.get("id"), client=client), today or date.today())
        except Exception:
            pass
    return {"register_on": on, "attendance_options": ATTENDANCE, "balance_lines": lines,
            "attendance": attendance_of(tenant_id, booking["id"], client=client) if on else None}


def portal_block(tenant_id: str, contact_id: str | None, *, today: date | None = None,
                 client=None) -> str:
    """The balance for the portal — only when the Packages face says so."""
    try:
        if not prefs_of(db.get_invoice_settings(tenant_id, client=client))["portal"]:
            return ""
    except Exception:
        return ""
    return context_block(tenant_id, contact_id, today=today, client=client)


def mint(tenant_id: str, contact_id: str, contact_email: str | None, package: dict | None, *,
         quantity: float | None = None, unit: str | None = None, invoice_id: str | None = None,
         instance_id: str | None = None, paid: bool = False, note: str | None = None,
         today: date | None = None, client=None) -> dict | None:
    """One wallet row: a package sold (quantity × the package's units, expiry from
    valid_days), or a hand adjustment (no package; quantity may be negative)."""
    today = today or date.today()
    qty = round(_f(quantity, 0) if quantity is not None else _f((package or {}).get("quantity"), 0), 2)
    if qty == 0:
        return None
    unit = unit or (package or {}).get("unit") or "session"
    expires = None
    if package and package.get("valid_days"):
        expires = (today + timedelta(days=int(package["valid_days"]))).isoformat()
    row = {"tenant_id": tenant_id, "contact_id": contact_id, "contact_email": contact_email,
           "package_key": (package or {}).get("key"), "package_name": (package or {}).get("name") or (note or "Adjustment"),
           "unit": unit if unit in UNITS else "session", "quantity": qty, "used": 0,
           "price": (package or {}).get("price"), "expires_at": expires, "paid": bool(paid),
           "invoice_id": invoice_id, "instance_id": instance_id, "note": note}
    try:
        res = _c(client).table("wallet_credits").insert(row).execute()
        return res.data[0] if res.data else None
    except Exception:
        log.warning("wallet mint failed (tenant=%s)", tenant_id, exc_info=True)
        return None


def on_invoice_issued(tenant_id: str, inv: dict, *, lines: list[dict] | None = None,
                      client=None) -> int:
    """An issued invoice mints the credits its package lines sold. A line names
    its package by key (the invoice page's picker) or by description (the
    concierge's line, matched to a package name). Returns how many rows minted."""
    settings = db.get_invoice_settings(tenant_id, client=client)
    packages = packages_of(settings)
    if not packages:
        return 0
    contact_id = inv.get("contact_id")
    if not contact_id and inv.get("customer_email"):
        c = db.crm_contact_by_email(tenant_id, inv["customer_email"], client=client)
        contact_id = (c or {}).get("id")
    if not contact_id:
        log.info("wallet: invoice %s has no contact — nothing minted", inv.get("invoice_ref"))
        return 0
    lines = lines if lines is not None else db.list_invoice_lines(inv["id"], tenant_id, client=client)
    already = _c(client).table("wallet_credits").select("id").eq("tenant_id", tenant_id) \
        .eq("invoice_id", inv["id"]).limit(1).execute().data
    if already:
        return 0                                    # issued twice — never mint twice
    n = 0
    for ln in lines or []:
        pkg = find_package(packages, ln.get("package_key")) or find_package(packages, ln.get("description"))
        if not pkg:
            continue
        mult = _f(ln.get("quantity"), 1) or 1
        row = mint(tenant_id, contact_id, inv.get("customer_email"), pkg,
                   quantity=round(pkg["quantity"] * mult, 2), invoice_id=inv["id"],
                   instance_id=ln.get("instance_id"), paid=(inv.get("status") == "paid"), client=client)
        if row:
            n += 1
            db.log_agent_event(tenant_id, KIND,
                               f"{inv.get('invoice_ref') or 'Invoice'} sold {pkg['name']} — "
                               f"{unit_words(pkg['unit'], row['quantity'])} added to the customer's wallet",
                               direction="outbound", contact=inv.get("customer_email"))
    return n


def on_invoice_paid(tenant_id: str, inv: dict, *, client=None) -> None:
    try:
        _c(client).table("wallet_credits").update({"paid": True}) \
            .eq("tenant_id", tenant_id).eq("invoice_id", inv["id"]).execute()
    except Exception:
        log.warning("wallet paid flag failed (tenant=%s)", tenant_id, exc_info=True)


# ── spending ─────────────────────────────────────────────────────────────────
def pick_credit(rows: list[dict], today: date, service: str | None, packages: list[dict],
                subject_ids: set[str] | None = None, *, paid_only: bool = False) -> dict | None:
    """The credit a booking spends from: live, covering the service (by the
    package's services list; an adjustment row covers everything), the
    booking's own participant first, shared rows next, earliest expiry first."""
    by_key = {p["key"]: p for p in packages}
    cands = []
    for r in live(rows, today, paid_only=paid_only):
        pkg = by_key.get(r.get("package_key"))
        if pkg and not covers(pkg, service):
            continue
        if r.get("instance_id") and r["instance_id"] not in (subject_ids or set()):
            continue
        cands.append(r)
    cands.sort(key=lambda r: (0 if r.get("instance_id") else 1,
                              str(r.get("expires_at") or "9999-12-31"), str(r.get("created_at") or "")))
    return cands[0] if cands else None


def spend_amount(row: dict, booking: dict, credits_cfg: dict) -> float:
    if (row.get("unit") or "session") != "hour":
        return 1.0
    if credits_cfg.get("hours") == "hour":
        return 1.0
    blocks = _i(booking.get("length_blocks"), 0)
    minutes = blocks * 15 if blocks else 60
    return round(minutes / 60.0, 2)


def spend_for_booking(tenant_id: str, booking: dict, reason: str, *, today: date | None = None,
                      client=None) -> dict:
    """Spend one booking against the customer's wallet. Idempotent per booking."""
    today = today or date.today()
    c = _c(client)
    try:
        prior = c.table("wallet_spend").select("id").eq("tenant_id", tenant_id) \
            .eq("booking_id", booking["id"]).limit(1).execute().data
    except Exception:
        prior = []
    if prior:
        return {"skipped": "already_spent"}
    contact = db.crm_contact_by_email(tenant_id, booking.get("contact"), client=client) if booking.get("contact") else None
    if not contact:
        return {"skipped": "no_contact"}
    settings = db.get_invoice_settings(tenant_id, client=client)
    packages, prefs = packages_of(settings), prefs_of(settings)
    credits_cfg = credits_of(db.get_scheduling_config(tenant_id, client=client))
    rows = list_credits(tenant_id, contact["id"], client=client)
    try:
        subs = db.subjects_for_bookings(tenant_id, [booking["id"]], client=client).get(booking["id"], [])
        subject_ids = {s["id"] for s in subs}
    except Exception:
        subject_ids = set()
    service = ((booking.get("details") or {}).get("Service")) or booking.get("summary")
    row = pick_credit(rows, today, service, packages, subject_ids, paid_only=prefs["paid_only"])
    if not row:
        return {"skipped": "no_credit"}
    amount = min(spend_amount(row, booking, credits_cfg), remaining(row))
    try:
        c.table("wallet_spend").insert({"tenant_id": tenant_id, "credit_id": row["id"],
                                        "booking_id": booking["id"], "amount": amount,
                                        "reason": reason}).execute()
        c.table("wallet_credits").update({"used": round(_f(row.get("used"), 0) + amount, 2)}) \
            .eq("id", row["id"]).execute()
    except Exception:
        log.warning("wallet spend failed (tenant=%s, booking=%s)", tenant_id, booking.get("id"), exc_info=True)
        return {"skipped": "write_failed"}
    left = round(remaining(row) - amount, 2)
    db.log_agent_event(tenant_id, KIND,
                       f"{booking.get('booking_ref') or 'A booking'} spent {unit_words(row.get('unit') or 'session', amount)} "
                       f"of {row.get('package_name')} ({reason.replace('_', ' ')}) — {unit_words(row.get('unit') or 'session', left)} left",
                       contact=booking.get("contact"), booking_id=booking.get("id"),
                       booking_ref=booking.get("booking_ref"))
    return {"spent": amount, "credit": row, "left": left}


def refund_booking(tenant_id: str, booking_id: str, *, client=None) -> float:
    """Give a booking's spend back (an excused absence, a cancelled class)."""
    c = _c(client)
    try:
        spends = c.table("wallet_spend").select("*").eq("tenant_id", tenant_id) \
            .eq("booking_id", booking_id).execute().data or []
        total = 0.0
        for s in spends:
            cr = c.table("wallet_credits").select("used").eq("id", s["credit_id"]).limit(1).execute().data
            if cr:
                c.table("wallet_credits").update(
                    {"used": max(0.0, round(_f(cr[0].get("used"), 0) - _f(s.get("amount"), 0), 2))}
                ).eq("id", s["credit_id"]).execute()
            c.table("wallet_spend").delete().eq("id", s["id"]).execute()
            total += _f(s.get("amount"), 0)
        return round(total, 2)
    except Exception:
        log.warning("wallet refund failed (tenant=%s, booking=%s)", tenant_id, booking_id, exc_info=True)
        return 0.0


def apply_attendance(tenant_id: str, booking: dict, status: str, *, note: str | None = None,
                     logged_by: str = "business", today: date | None = None, client=None,
                     notified_at: str | None = None) -> dict:
    """The register: record the outcome for this occurrence and settle the
    wallet per the Diary's rules. The exception always wins over the sweep."""
    status = str(status or "").strip().lower()
    if status not in ATTENDANCE:
        return {"error": "bad_status"}
    c = _c(client)
    try:
        row = {"booking_id": booking["id"], "tenant_id": tenant_id, "status": status,
               "note": (note or "").strip()[:300] or None, "logged_by": logged_by,
               "at": datetime.now(timezone.utc).isoformat()}
        if notified_at:
            row["notified_at"] = notified_at         # 6b nit: when the customer told the business
        c.table("booking_attendance").upsert(row).execute()
    except Exception:
        log.warning("attendance write failed (tenant=%s)", tenant_id, exc_info=True)
        return {"error": "write_failed"}
    cfg = credits_of(db.get_scheduling_config(tenant_id, client=client))
    out: dict = {"status": status}
    if status == "attended":
        refund_booking(tenant_id, booking["id"], client=client)
        out["wallet"] = spend_for_booking(tenant_id, booking, "attended", today=today, client=client)
    elif status in ("no_show", "late_cancel"):
        if cfg["no_show_spends"]:
            refund_booking(tenant_id, booking["id"], client=client)
            out["wallet"] = spend_for_booking(tenant_id, booking, status, today=today, client=client)
        else:
            out["wallet"] = {"refunded": refund_booking(tenant_id, booking["id"], client=client)}
    elif status == "excused":
        if cfg["excused_refunds"]:
            out["wallet"] = {"refunded": refund_booking(tenant_id, booking["id"], client=client)}
        else:
            out["wallet"] = {"skipped": "excused_spends"}
    try:   # 6b nit: a make-up that is itself missed feeds back (src/app/makeups.py)
        from app import makeups as _makeups
        _makeups.on_attendance(tenant_id, booking, status, client=client)
    except Exception:
        log.warning("make-up feedback failed (tenant=%s)", tenant_id, exc_info=True)
    db.log_agent_event(tenant_id, "attendance",
                       f"{booking.get('booking_ref') or 'A booking'} marked {status.replace('_', ' ')}"
                       + (f" — {note.strip()[:80]}" if note and note.strip() else ""),
                       contact=booking.get("contact"), booking_id=booking.get("id"),
                       booking_ref=booking.get("booking_ref"))
    return out


def attendance_of(tenant_id: str, booking_id: str, client=None) -> dict | None:
    try:
        res = _c(client).table("booking_attendance").select("*").eq("tenant_id", tenant_id) \
            .eq("booking_id", booking_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


# ── the daily sweep ───────────────────────────────────────────────────────────
def due_auto_spend(bookings: list[dict], attendance_ids: set[str], spent_ids: set[str],
                   today: date) -> list[dict]:
    """Pure: confirmed bookings whose day has passed, with no register row and
    no spend yet — the class happened, the credit comes off."""
    floor = (today - timedelta(days=AUTO_SPEND_LOOKBACK_DAYS)).isoformat()
    out = []
    for b in bookings:
        if b.get("status") != "confirmed" or not b.get("booking_date"):
            continue
        d = str(b["booking_date"])[:10]
        if d >= today.isoformat() or d < floor:
            continue
        if b["id"] in attendance_ids or b["id"] in spent_ids:
            continue
        out.append(b)
    return out


def due_messages(rows: list[dict], today: date, prefs: dict) -> list[tuple[dict, str]]:
    """Pure: (credit row, 'low' | 'expiry') for the automatic notes, once each."""
    out = []
    for r in live(rows, today):
        left = remaining(r)
        if prefs["low_at"] and left <= prefs["low_at"] and not r.get("low_sent_at"):
            out.append((r, "low"))
        if prefs["expiry_days"] and r.get("expires_at") and not r.get("expiry_sent_at"):
            if str(r["expires_at"])[:10] <= (today + timedelta(days=prefs["expiry_days"])).isoformat():
                out.append((r, "expiry"))
    return out


def facts_for(row: dict, why: str, contact_name: str | None, packages: list[dict]) -> str:
    left = remaining(row)
    parts = [f"The customer has {unit_words(row.get('unit') or 'session', left)} left of "
             f"{row.get('package_name')}."]
    if row.get("expires_at"):
        parts.append(f"They run out on {pretty_date(str(row['expires_at'])[:10])}.")
    if why == "low":
        parts.append("That is the last of them, or nearly.")
    pkg = next((p for p in packages if p["key"] == row.get("package_key")), None)
    if pkg and pkg.get("price"):
        parts.append(f"The business sells the same again: {pkg['name']} — {unit_words(pkg['unit'], pkg['quantity'])} for £{pkg['price']:.2f}.")
    if contact_name:
        parts.append(f"The message is to {contact_name}.")
    return " ".join(parts)


async def send_balance_message(tenant_id: str, row: dict, why: str, packages: list[dict], *,
                               client=None) -> bool:
    from app import agent, disclosure, mailbox, push, usage
    to = row.get("contact_email")
    contact = db.crm_get_contact(row.get("contact_id"), tenant_id, client=client) or {} if row.get("contact_id") else {}
    to = to or contact.get("email")
    if not to:
        return False
    cfg = db.effective_config(tenant_id, None, client=client)
    facts = facts_for(row, why, contact.get("name"), packages)
    filed = False   # the note is composed and in the queue: that is a message (run #8's counter)
    try:
        result = await agent.compose_outbound(cfg, PURPOSE, facts, channel="email",
                                              tenant_id=tenant_id, log_operation=KIND)
        subject = f"Your {row.get('package_name')} — " + ("nearly used up" if why == "low" else "running out soon")
        log_id, _ = db.insert_email_log(tenant_id, to, subject, f"[package balance] {facts}", result.text,
                                        status="pending", kind=KIND, channel="email")
        filed = True
        usage.record_anthropic(tenant_id, result.model, result.usage, operation=KIND, email_log_id=log_id)
        await mailbox.send_mail(tenant_id, to, subject, disclosure.wrap(cfg, result.text))
        db.mark_email_log_sent(log_id, tenant_id, via="human", edited=False)
    except Exception:
        log.warning("package balance message %s (tenant=%s)",
                    "filed for approval, not sent" if filed else "failed", tenant_id, exc_info=True)
        return filed
    stamp = {"low_sent_at" if why == "low" else "expiry_sent_at": datetime.now(timezone.utc).isoformat()}
    try:
        _c(client).table("wallet_credits").update(stamp).eq("id", row["id"]).execute()
    except Exception:
        pass
    push.notify(tenant_id, title=f"{contact.get('name') or to}: {row.get('package_name')} "
                + ("nearly used up" if why == "low" else "expires soon"),
                body=f"Told them — {unit_words(row.get('unit') or 'session', remaining(row))} left.",
                url=f"/app/crm/contacts/{row.get('contact_id')}", tag=f"wallet-{row['id']}-{why}",
                kind=KIND, contact=to, contact_name=contact.get("name"))
    return True


def tenants_with_packages(client=None) -> list[tuple[str, dict]]:
    try:
        rows = _c(client).table("invoice_settings").select("tenant_id,extras").execute().data or []
    except Exception:
        return []
    return [(r["tenant_id"], r) for r in rows if packages_of(r)]


async def run_sweep(today: date | None = None) -> dict:
    """Daily: the automatic spend (when the Diary says so) and the balance notes."""
    today = today or datetime.now().date()
    stats = {"spent": 0, "messages": 0}
    for tenant_id, settings in tenants_with_packages():
        try:
            prefs = prefs_of(settings)
            packages = packages_of(settings)
            cfg = credits_of(db.get_scheduling_config(tenant_id))
            if cfg["spend"] == "auto":
                since = (today - timedelta(days=AUTO_SPEND_LOOKBACK_DAYS)).isoformat()
                bookings = db.list_active_bookings(tenant_id, since, (today - timedelta(days=1)).isoformat())
                ids = [b["id"] for b in bookings]
                att = {r["booking_id"] for r in (_c().table("booking_attendance").select("booking_id")
                                                  .eq("tenant_id", tenant_id).in_("booking_id", ids).execute().data or [])} if ids else set()
                spent = {r["booking_id"] for r in (_c().table("wallet_spend").select("booking_id")
                                                    .eq("tenant_id", tenant_id).in_("booking_id", ids).execute().data or [])} if ids else set()
                for b in due_auto_spend(bookings, att, spent, today):
                    # The availability select is lean (no contact, details, summary,
                    # ref); the spend needs the whole row (run #8, 2026-09-05).
                    full = db.get_booking(b["id"], tenant_id) or b
                    out = spend_for_booking(tenant_id, full, "auto", today=today)
                    if out.get("spent"):
                        stats["spent"] += 1
            rows = _c().table("wallet_credits").select("*").eq("tenant_id", tenant_id).execute().data or []
            for row, why in due_messages(rows, today, prefs):
                if await send_balance_message(tenant_id, row, why, packages):
                    stats["messages"] += 1
        except Exception:
            log.warning("wallet sweep failed (tenant=%s)", tenant_id, exc_info=True)
    return stats


# ── Money ─────────────────────────────────────────────────────────────────────
def summary(tenant_id: str, *, today: date | None = None, client=None) -> dict:
    """Packages sold and the unspent balance (the deferred liability)."""
    today = today or date.today()
    try:
        rows = _c(client).table("wallet_credits").select("*").eq("tenant_id", tenant_id).execute().data or []
    except Exception:
        rows = []
    sold = [r for r in rows if r.get("package_key")]
    sold_total = round(sum(_f(r.get("price"), 0) for r in sold), 2)
    unspent = live(rows, today)
    units = round(sum(remaining(r) for r in unspent), 2)
    value = 0.0
    for r in unspent:
        q = _f(r.get("quantity"), 0)
        if q > 0 and r.get("price"):
            value += _f(r["price"], 0) * remaining(r) / q
    return {"sold_count": len(sold), "sold_total": sold_total,
            "unspent_units": units, "unspent_value": round(value, 2),
            "customers": len({r.get("contact_id") for r in unspent})}
