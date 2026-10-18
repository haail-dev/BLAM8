"""Job-party extraction (docs/modules/module-job-parties.md Phase 2 — the differentiator).

One small call per inbound email on a thread that has a live booking: does this
email NAME other people involved in the job — a property owner, a tenant, a letting
agent, a site contact — and what does it say each of them does? Claims land in
`job_parties` as source='extracted' rows, which by D4 NEVER resolve a duty:
they render as confirm cards on the diary job page (with the distinct
money-warning for pays/approves — "invoice this other address instead" is the
textbook redirection scam) and only a human's confirm makes them real.

Nobody in the researched sector ships this; every one of them makes the
operator forward documents by hand.

Cost: ~$0.0002/email at Haiku pricing, and only booking-threads pay it;
usage is metered like drafting (operation='party_extract').
"""
import json
import logging
import re

from app import agent, db, llm, usage
from app.config import settings

log = logging.getLogger(__name__)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "parties": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "label": {"type": "string"},
                    "booking_ref": {"type": "string"},
                    "pays": {"type": "boolean"},
                    "approves": {"type": "boolean"},
                    "on_site": {"type": "boolean"},
                    "informed": {"type": "boolean"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "email", "phone", "label", "booking_ref",
                             "pays", "approves", "on_site", "informed",
                             "evidence", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["parties"],
    "additionalProperties": False,
}

MAX_BODY_CHARS = 4000     # same budget as crm_extract — an SMB email fits
MIN_CONFIDENCE = 0.6      # below this a claim is speculation, not a card

# Live statuses a claim can attach to — a cancelled job takes no new people.
# An OFFERED visit counts (run #5, 2026-09-04): the housekeeper named while
# the slot was still an offer is the housekeeper of that visit.
LIVE_STATUSES = ("confirmed", "proposed", "offered")

HISTORY_CHARS = 1800      # what earlier emails in the thread contribute

SYSTEM = (
    (
        "You read one business email about booked jobs and extract OTHER PEOPLE the email says are "
        "involved in a job — for example, whoever pays, approves, attends or needs updates. Only "
        "people the email actually names with a part in the job: never the business being written to, "
        "never signature boilerplate, never staff mentioned in passing. label is the email's own word "
        "for their role. The duty booleans are ONLY what the email states: pays = the email says they "
        "pay the bill / invoices go to them; approves = they sign off quotes or estimates; on_site = "
        "they attend, let people in, or are the day-of contact; informed = the email asks for them to "
        "be copied or kept in the loop. booking_ref is the job reference (like BK-123) when the email "
        "ties the person to one, else empty — but when the email plainly means the job it is ABOUT and "
        "names no reference, give the ref of the job marked THIS EMAIL'S JOB. evidence is the exact "
        "sentence the claim comes from, verbatim. confidence is your certainty (1.0 = stated outright, "
        "0.5 = implied, lower = speculative). Unknown fields are empty strings. No people mentioned = "
        "an empty list — never invent. When EARLIER EMAILS in the thread are shown, use them only to "
        "fill in a person this email names — the address or phone given before for the same person — "
        "never to add people this email does not mention."
    )
)


_CONTACT_DETAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|(?<!\d)(?:\+44\s?\d|0\d)[\d\s]{8,12}\d")


def thread_history_text(history: list[dict] | None) -> str:
    """The customer's earlier emails in this thread, newest last, stripped of
    quotes and capped — where 'Jo from Hilltop Housekeeping (jo@…)' was said
    two emails before 'Jo just needs the times'. The whole thread is scanned:
    the last two customer emails always ride, and any older one that carries
    an address or a phone number (live 2026-09-04: Jo's address was fourteen
    emails back and a six-row window never saw it)."""
    from app.conversation_direction import direction
    inbound = [row for row in (history or []) if direction(row) == "inbound"]
    chosen: list[tuple[int, str]] = []
    used = 0
    for idx in range(len(inbound) - 1, -1, -1):
        row = inbound[idx]
        text = agent.strip_quoted(row.get("body_in") or row.get("body") or "").strip()
        if not text:
            continue
        recent = idx >= len(inbound) - 2
        if not recent and not _CONTACT_DETAIL.search(text):
            continue
        snippet = text[:700]
        if used + len(snippet) > HISTORY_CHARS and chosen:
            break
        chosen.append((idx, snippet))
        used += len(snippet)
    chosen.sort()
    return "\n---\n".join(t for _, t in chosen)[-HISTORY_CHARS:]


def build_user_prompt(bookings: list[dict], subject: str | None, body: str | None,
                      from_name: str | None = None,
                      from_address: str | None = None,
                      primary_ids=None, history_text: str | None = None) -> str:
    """`primary_ids` are the bookings THIS email placed or moved. The rest are the
    customer's other live jobs — real context (a claim may name one outright) but
    not what the email is about, and the model must not drift onto them."""
    primary = set(primary_ids or ())
    lines = ["The jobs this thread is about:"]
    for b in bookings:
        mark = " — THIS EMAIL'S JOB" if b.get("id") in primary else ""
        lines.append(f"- {b.get('booking_ref') or 'job'}{mark}: "
                     f"{(b.get('summary') or 'booking').strip()} on "
                     f"{b.get('booking_date') or '?'} "
                     f"(booked by {b.get('contact') or 'unknown'})")
    lines.append("")
    if history_text:
        lines.append("Earlier emails in this thread from the same customer (for a name's "
                     "address or phone only):\n" + history_text)
        lines.append("")
    lines.append(f"Email from: {from_name or 'unknown'} <{from_address or '?'}>")
    lines.append(f"Subject: {subject or '(none)'}")
    lines.append(f"Body:\n{agent.strip_quoted(body or '')[:MAX_BODY_CHARS]}")
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    """The schema-constrained reply as claim dicts; anything unparseable is an
    empty extraction, never an error."""
    try:
        data = json.loads(text)
        return [p for p in (data.get("parties") or []) if isinstance(p, dict)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def extract(tenant_id: str, bookings: list[dict], *, subject: str | None,
                  body: str | None, from_name: str | None = None,
                  from_address: str | None = None,
                  email_log_id: str | None = None,
                  primary_ids=None, history_text: str | None = None) -> list[dict]:
    """Run one extraction call over the thread's live bookings. Returns raw
    claims for apply(); [] when there's no live job to attach anyone to."""
    live = [b for b in bookings or [] if b.get("status") in LIVE_STATUSES]
    if not live:
        return []
    msg = await llm.create(
        client=agent.client(), tenant_id=tenant_id, operation="party_extract",
        model=settings.anthropic_model,
        max_tokens=700,
        system=SYSTEM,
        messages=[{"role": "user",
                   "content": build_user_prompt(live, subject, body,
                                                from_name, from_address,
                                                primary_ids, history_text)}],
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="party_extract",
                           email_log_id=email_log_id)
    return parse_response(text)


def _digits(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def apply(tenant_id: str, bookings: list[dict], extracted: list[dict], *,
          email_log_id: str | None = None, client=None, primary_ids=None,
          sender: str | None = None) -> dict:
    """Land high-confidence claims as source='extracted' rows — the suggestion
    IS the row; D4 keeps it inert until a human confirms. Returns a tally:
    {suggested, money_claims, refs} for the agent-event line.

    Skips, in order: low confidence; no way to ever reach them (no email AND
    no phone); no unambiguous job (a stated booking_ref wins, else THIS
    email's own single live booking, else the thread's single live booking,
    else nothing — a guess about WHICH job a payer belongs to is exactly the
    wrong thing to guess); the booking's own contact (they already hold every
    duty); anyone already on the job.

    `primary_ids` = the bookings this email placed or moved. Live 2026-09-04:
    a certificate email that held BK-1003 had its on-site tenant bound to BK-1002 —
    the customer's latest OTHER confirmed job — because every live job the
    customer had looked equally like "the" job. The email's own job is the one
    it is about."""
    live = [b for b in bookings or [] if b.get("status") in LIVE_STATUSES]
    by_ref = {b["booking_ref"]: b for b in live if b.get("booking_ref")}
    mine = [b for b in live if b.get("id") in set(primary_ids or ())]
    fallback = mine[0] if len(mine) == 1 else (live[0] if len(live) == 1 else None)
    tally = {"suggested": 0, "money_claims": 0, "refs": []}
    if not live or not extracted:
        return tally
    existing = db.job_parties_for_bookings(tenant_id, [b["id"] for b in live],
                                           client=client)

    def _land(x: dict, b: dict, email: str, phone: str) -> None:
        # The booking's own contact — and the email's own sender — already
        # hold every duty; "all the invoices come to me" from the booker is
        # not a new party (live 2026-09-04: Kris bound as her own payer).
        if email and email in {(b.get("contact") or "").strip().lower(),
                               (sender or "").strip().lower()} - {""}:
            return
        rows = existing.setdefault(b["id"], [])
        if email and any((p.get("email") or "").lower() == email for p in rows):
            return
        if phone and any(_digits(p.get("phone")) == _digits(phone)
                         for p in rows if p.get("phone")):
            return
        row = db.upsert_job_party(tenant_id, b["id"], {
            "name": x.get("name"), "email": email, "phone": phone,
            "label": x.get("label"),
            "pays": bool(x.get("pays")), "approves": bool(x.get("approves")),
            "on_site": bool(x.get("on_site")),
            "informed": bool(x.get("informed")),
            "source": "extracted",
            "evidence": (x.get("evidence") or "").strip()[:500] or None,
            "email_log_id": email_log_id,
        }, client=client)
        if row:
            rows.append(row)
            tally["suggested"] += 1
            if x.get("pays") or x.get("approves"):
                tally["money_claims"] += 1
            ref = b.get("booking_ref")
            if ref and ref not in tally["refs"]:
                tally["refs"].append(ref)

    for x in extracted:
        try:
            if float(x.get("confidence") or 0) < MIN_CONFIDENCE:
                continue
            email = (x.get("email") or "").strip().lower()
            phone = (x.get("phone") or "").strip()
            if not email and not phone:
                continue
            stated = by_ref.get((x.get("booking_ref") or "").strip())
            targets = [stated] if stated else ([fallback] if fallback else
                                              jobs_for_claim(x, live))
            if not targets:
                continue
            for b in targets:
                _land(x, b, email, phone)
        except Exception:
            # One bad claim must not cost the rest — or anything upstream.
            log.warning("party claim failed to land for tenant %s", tenant_id,
                        exc_info=True)
    return tally


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def jobs_for_claim(claim: dict, live: list[dict]) -> list[dict]:
    """Which live jobs an unreferenced claim is about, on a thread with
    several. The evidence sentence names a day ("on Tuesday", "the 8th",
    "8 September") → every live job on that date. No day named: an on-site
    or informed claim goes to the earliest upcoming job (they are about
    turning up); a payer / approver claim goes nowhere — see apply()."""
    text = " ".join(str(claim.get("evidence") or "").lower().split())
    dated: list[dict] = []
    for b in live:
        d = _job_date(b)
        if d is None:
            continue
        day_word = _WEEKDAYS[d.weekday()]
        if re.search(r"\b" + day_word + r"\b", text) or \
                re.search(r"\b" + str(d.day) + r"(?:st|nd|rd|th)?\b(?!:)", text) and \
                (d.strftime("%B").lower() in text or d.strftime("%b").lower() in text
                 or not re.search(r"\b(january|february|march|april|may|june|july|august|"
                                  r"september|october|november|december)\b", text)):
            dated.append(b)
    if dated:
        # A weekday word alone ("on Tuesday") means the nearest such day, not
        # every Tuesday the customer has booked (6 Oct is a Tuesday too).
        if not re.search(r"\d{1,2}(?:st|nd|rd|th)?", text):
            first = min(_job_date(b) for b in dated)
            dated = [b for b in dated if _job_date(b) == first]
        return dated
    if claim.get("pays") or claim.get("approves"):
        return []
    upcoming = sorted((b for b in live if _job_date(b)), key=_job_date)
    return upcoming[:1]


def _job_date(b: dict):
    from datetime import date
    try:
        return date.fromisoformat(str(b.get("booking_date") or "")[:10])
    except ValueError:
        return None
