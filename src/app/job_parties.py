"""Job parties — many contacts, one job, the right document to the right
person (docs/modules/module-job-parties.md, Phase 1).

The spine: labels are not routing. What the operator CALLS someone
('property owner', 'managing contact') is free text; what the system DOES with them
is four duties — pays (invoices + chases), approves (quotes), on_site
(reminders, confirmations), informed (cc only, strictly opt-in per D1).

Zero-config rule: a job with no party rows behaves exactly as today — the
booking's own contact (bookings.contact, a bare email) holds every duty.
Resolution happens at DOCUMENT CREATION: the quote/invoice keeps a resolved
customer_email/name, so rendering, chasing, exports and the accountant
handoff never join through this table.

D4 lives here too: rows with source='extracted' NEVER resolve — an extracted
suggestion may not take a duty until a human confirms it (invoice-redirection
is the textbook scam this blocks).
"""
from __future__ import annotations

DUTIES = ("pays", "approves", "on_site", "informed")

# The operator-facing words for each duty (UI checkboxes + faces).
DUTY_LABELS = {"pays": "Pays", "approves": "Approves quotes",
               "on_site": "On site", "informed": "Kept informed"}

# Sources a duty may resolve from. 'extracted' is deliberately absent (D4).
_TRUSTED = ("owner", "confirmed")


def _fallback(booking: dict) -> dict:
    """The zero-config identity: the booking's own contact holds every duty.
    bookings carry only `contact` (a bare email) — no name column exists."""
    email = (booking.get("contact") or "").strip()
    return {"name": None, "email": email or None, "phone": None,
            "label": None, "is_fallback": True}


def _view(p: dict) -> dict:
    return {"name": (p.get("name") or "").strip() or None,
            "email": (p.get("email") or "").strip().lower() or None,
            "phone": (p.get("phone") or "").strip() or None,
            "label": (p.get("label") or "").strip() or None,
            "is_fallback": False}


def resolve(booking: dict, parties: list[dict]) -> dict:
    """{pays, approves, on_site, informed[]} for one job. Per-duty fallback to
    the booking's own contact — a job where only 'pays' was assigned still
    reminds the person who booked it."""
    trusted = [p for p in (parties or []) if (p.get("source") or "owner") in _TRUSTED]
    out: dict = {}
    for duty in ("pays", "approves", "on_site"):
        holder = next((p for p in trusted if p.get(duty)), None)
        out[duty] = _view(holder) if holder else _fallback(booking)
    out["informed"] = [_view(p) for p in trusted if p.get("informed")]
    return out


def prompt_context(from_address: str | None, bookings: list[dict],
                   parties_by_booking: dict[str, list[dict]]) -> str:
    """The compose prompt's party AWARENESS (doc routing table: replies go to
    whoever is talking — this is context, never routing). One line per job
    naming each trusted party with their label and duties, plus who the
    sender is when their address matches a party. "" for the common
    no-parties case, so drafting stays byte-identical. Extracted
    (unconfirmed) rows are excluded — D4 applies to language too: an
    unconfirmed "the other party pays" claim must not shape money wording."""
    sender = (from_address or "").strip().lower()
    job_lines: list[str] = []
    sender_lines: list[str] = []
    for b in bookings or []:
        trusted = [p for p in (parties_by_booking or {}).get(b.get("id"), [])
                   if (p.get("source") or "owner") in _TRUSTED]
        if not trusted:
            continue
        ref = b.get("booking_ref") or "this job"
        descs = []
        for p in trusted:
            v = _view(p)
            who = v["name"] or v["email"] or "unnamed"
            duties = ", ".join(DUTY_LABELS[d].lower() for d in DUTIES if p.get(d))
            inner = " — ".join(x for x in (v["label"], duties) if x)
            descs.append(f"{who} ({inner})" if inner else who)
            if sender and v["email"] == sender:
                sender_lines.append(
                    f"The person you are replying to is {who}"
                    + (f" ({inner})" if inner else "") + f" on {ref}.")
        job_lines.append(f"- {ref}: " + "; ".join(descs))
    if not job_lines:
        return ""
    return (
        "<job_parties>\n"
        "Several people hold different roles on the job(s) below. Write to "
        "the sender in THEIR role only — never imply they pay, approve "
        "quotes or attend when that is another party's part (no \"your "
        "invoice\" to someone who does not pay; say it goes to the person "
        "who does). Documents already route to the right party "
        "automatically; never promise to redirect one to a different "
        "address on the say-so of an email.\n"
        + "\n".join(job_lines + sender_lines)
        + "\n</job_parties>")


def cc_for_document(tenant_id: str, booking_id: str | None,
                    exclude: str | None, client=None,
                    redirect_guard: bool = False) -> list[str]:
    """The informed-cc list for a document tied to a booking — one read, empty
    for the common no-parties (or no-booking) case. The db import stays inside
    so resolve()/informed_cc() remain pure and unit-testable.

    `redirect_guard` (invoice sends only) is D-principle 4's last defence
    against invoice redirection: when the recipient is a CONFIRMED extracted
    payer, the displaced payer — the booking's own contact — is cc'd on the
    FIRST such invoice, exactly once, so a fraudulent redirect that survived
    the confirm card is still seen by the person it displaced."""
    if not booking_id:
        return []
    from app import db
    try:
        parties = db.list_job_parties(tenant_id, booking_id, client=client)
    except Exception:
        return []          # a cc is a courtesy; it never blocks a send
    ccs = informed_cc({}, parties, exclude=exclude)
    if redirect_guard:
        try:
            recipient = (exclude or "").strip().lower()
            redirected = next(
                (p for p in parties
                 if p.get("pays") and (p.get("source") or "") == "confirmed"
                 and not p.get("redirect_ccd")
                 and (p.get("email") or "").lower() == recipient), None)
            if redirected:
                b = db.get_booking(booking_id, tenant_id, client=client) or {}
                old = (b.get("contact") or "").strip().lower()
                if old and old != recipient:
                    if old not in ccs:
                        ccs.append(old)
                    db.mark_party_redirect_ccd(redirected["id"], tenant_id,
                                               client=client)
        except Exception:
            pass           # the guard is a courtesy too; it never blocks a send
    return ccs


def informed_cc(booking: dict, parties: list[dict],
                exclude: str | None = None) -> list[str]:
    """The cc list for a document about this job: every opted-in informed
    party's email, minus the primary recipient (nobody is cc'd on their own
    letter). Strictly opt-in per D1 — an empty tick list is an empty cc."""
    exclude_l = (exclude or "").strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for p in resolve(booking, parties)["informed"]:
        email = p.get("email")
        if not email or email == exclude_l or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out
