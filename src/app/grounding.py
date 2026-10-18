"""Grounded booking — the model may only act on a date it can point to.

docs/plan-grounded-booking.md. Pure: text and rows in, a set of allowed dates
and a refusal out. No DB, no clock of its own.

The failure this exists for (K7 shoot, 2026-09-22): the customer wrote
`8am is fine thanks` — no dates — and the draft held 3–6 November, dates that
appeared in no email, no reply and no booking. The diary's open hold for him
was 10–13. He was confirming it. The model invented instead of asking.

Two rules, both enforced in the executor (the prompt only explains them):

  grounding  every date a booking-placing call asks for must be in the
             allowed set — this email, earlier emails from the customer,
             our own SENT replies, the dates each live booking covers, and
             whatever check_availability returned free this draft;
  ordering   a hold's dates must have been checked free THIS draft.

Why a purpose-built parser and not dateparser: on the real strings it read
"10th-13th nov" as the year 2113 and "8am" as the 8th of the month. The shapes
people write are few; the cost of one wrong year is a booking.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import slotgrid

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2,
    "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DOW = r"(?:mon(?:day)?|tue(?:s|sday)?|wed(?:s|nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_D = r"(\d{1,2})(?:st|nd|rd|th)?"
_DASH = r"\s*(?:-|–|—|to|until|till|through|thru)\s*"
_YR = r"(?:,?\s+(\d{4}))?"

# Order matters: ranges first, so "3rd-6th nov" is one mention, not "6th nov"
# plus a stray "3rd". Each pattern's groups are documented on its handler.
# No trailing \b: "2026-11-10T08:00" — the shape every tool result and sent
# reply carries — has a word character after the day, so \b would miss it.
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
# "3rd-6th nov", "10–13 November", "3 to 6 nov 2026"
_RANGE_ONE_MONTH = re.compile(rf"\b{_D}{_DASH}{_D}\s+(?:of\s+)?({_MON})\b{_YR}", re.I)
# "Tuesday 3rd to Friday 6th November" / "23 Oct - 30 Oct" / "Fri 23 October to Fri 30 October"
_RANGE_TWO = re.compile(
    rf"\b(?:{_DOW},?\s+)?{_D}(?:\s+(?:of\s+)?({_MON}))?{_DASH}(?:{_DOW},?\s+)?{_D}\s+(?:of\s+)?({_MON})\b{_YR}",
    re.I)
# "Friday 6th November", "23 October", "6 nov 2026"
_DAY_MONTH = re.compile(rf"\b(?:({_DOW}),?\s+)?{_D}\s+(?:of\s+)?({_MON})\b{_YR}", re.I)
# "November 6th", "Oct 23"
_MONTH_DAY = re.compile(rf"\b({_MON})\s+{_D}\b{_YR}", re.I)
# a bare weekday — "Thursday", "next tuesday"
_WEEKDAY = re.compile(rf"\b(next\s+|this\s+)?({_DOW})\b", re.I)
# times are recorded so a refusal can mention them; they NEVER become dates
_TIME = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.I)


@dataclass
class Mention:
    quote: str
    dates: list[date]


@dataclass
class Grounding:
    """The allowed set and where each date came from."""
    dates: set[str] = field(default_factory=set)            # ISO
    why: dict[str, list[str]] = field(default_factory=dict) # ISO -> sources
    times: list[str] = field(default_factory=list)          # "HH:MM" seen, for the steer
    # Every time the CUSTOMER has given — this email, their earlier ones — and
    # every time our own SENT replies stated (a human approved those). A stay's
    # drop-off / collection must be one of these (K11, Kris 2026-09-23: the
    # agent wrote "drop-off 8am" when the customer had given no time at all).
    customer_times: set[str] = field(default_factory=set)
    open_holds: list[dict] = field(default_factory=list)    # proposed/offered rows
    checked: set[str] = field(default_factory=set)          # returned free THIS draft
    full: set[str] = field(default_factory=set)            # returned FULL this draft
    on: bool = True

    def allow(self, d, why: str) -> None:
        iso = d.isoformat() if isinstance(d, date) else str(d)[:10]
        self.dates.add(iso)
        lst = self.why.setdefault(iso, [])
        if why not in lst:
            lst.append(why)


# ── the mention parser ───────────────────────────────────────────────────────
def _month(s: str) -> int | None:
    return MONTHS.get((s or "").lower())


def _year_for(month: int, day: int, today: date, year: str | None) -> date | None:
    """A month/day with no year means the NEXT one — a customer writing in
    September about "3rd November" means this year; writing in December about
    "3rd January" means next. Explicit years are taken as given."""
    if year:
        try:
            return date(int(year), month, day)
        except ValueError:
            return None
    for y in (today.year, today.year + 1):
        try:
            cand = date(y, month, day)
        except ValueError:
            continue
        # allow a few days' grace: "the 20th" written on the 22nd is this month's
        if cand >= today - timedelta(days=7):
            return cand
    return None


def _span(a: date, b: date) -> list[date]:
    if b < a:
        return [a]
    if (b - a).days > 62:          # nobody books a stay by a range longer than that
        return [a]
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def mentions(text: str, today: date) -> list[Mention]:
    """Every date mention in `text`, with the exact quote it came from. Ranges
    expand to every day they cover. Times are not dates and are not here."""
    out: list[Mention] = []
    taken: list[tuple[int, int]] = []
    t = text or ""

    def _free(m) -> bool:
        return not any(s < m.end() and m.start() < e for s, e in taken)

    def _take(m, dates: list[date]) -> None:
        dates = [d for d in dates if d]
        if not dates:
            return
        taken.append((m.start(), m.end()))
        out.append(Mention(quote=m.group(0).strip(), dates=dates))

    for m in _ISO.finditer(t):
        try:
            _take(m, [date(int(m.group(1)), int(m.group(2)), int(m.group(3)))])
        except ValueError:
            pass
    for m in _RANGE_TWO.finditer(t):
        if not _free(m):
            continue
        d1, m1, d2, m2, yr = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        mo2 = _month(m2)
        mo1 = _month(m1) if m1 else mo2
        if not mo1 or not mo2:
            continue
        a = _year_for(mo1, int(d1), today, yr)
        b = _year_for(mo2, int(d2), today, yr) if a is None else _year_for(mo2, int(d2), a, yr)
        if a and b:
            _take(m, _span(a, b))
    for m in _RANGE_ONE_MONTH.finditer(t):
        if not _free(m):
            continue
        mo = _month(m.group(3))
        if not mo:
            continue
        a = _year_for(mo, int(m.group(1)), today, m.group(4))
        b = _year_for(mo, int(m.group(2)), a or today, m.group(4))
        if a and b:
            _take(m, _span(a, b))
    for m in _DAY_MONTH.finditer(t):
        if not _free(m):
            continue
        mo = _month(m.group(3))
        if mo:
            _take(m, [_year_for(mo, int(m.group(2)), today, m.group(4))])
    for m in _MONTH_DAY.finditer(t):
        if not _free(m):
            continue
        mo = _month(m.group(1))
        if mo:
            _take(m, [_year_for(mo, int(m.group(2)), today, m.group(3))])
    for m in _WEEKDAY.finditer(t):
        if not _free(m):
            continue
        dow = WEEKDAYS.get(m.group(2).lower())
        if dow is None:
            continue
        ahead = (dow - today.weekday()) % 7 or 7          # strictly after today
        if (m.group(1) or "").strip().lower() == "next" and ahead < 7:
            ahead += 7 if today.weekday() >= dow else 0   # "next Tuesday" on a Wednesday
        _take(m, [today + timedelta(days=ahead)])
    return out


def times_in(text: str) -> list[str]:
    out = []
    for m in _TIME.finditer(text or ""):
        if m.group(3):
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            out.append(f"{h:02d}:{int(m.group(2) or 0):02d}")
        else:
            out.append(f"{int(m.group(4)):02d}:{m.group(5)}")
    return out


# ── bookings → the dates they cover ──────────────────────────────────────────
def booking_dates(row: dict) -> list[str]:
    """Every ISO date a booking row covers: a stay's whole grid span, or the
    booking_date for a row without grid columns."""
    span = slotgrid.stay_span(row)
    if span:
        start, length = span
        first = slotgrid.abs_to_datetime(start).date()
        last = slotgrid.abs_to_datetime(start + max(1, int(length)) - 1).date()
        return [d.isoformat() for d in _span(first, last)]
    bd = str(row.get("booking_date") or "")[:10]
    return [bd] if bd else []


def _ref(row: dict) -> str:
    return str(row.get("booking_ref") or row.get("reference") or row.get("id") or "?")


# ── building the allowed set ────────────────────────────────────────────────
def build(*, subject: str = "", body: str = "", history: list[dict] | None = None,
          existing_bookings: list[dict] | None = None, today: date | None = None) -> Grounding:
    """The allowed set for one draft. Sources, in the order they are cited:
    this email; earlier emails from the customer; our own SENT replies (a
    human approved them); every date each live booking covers."""
    today = today or date.today()
    g = Grounding()
    for mn in mentions(f"{subject or ''}\n{body or ''}", today):
        for d in mn.dates:
            g.allow(d, f"this email: '{mn.quote}'")
    g.times = times_in(f"{subject or ''}\n{body or ''}")
    g.customer_times.update(g.times)
    for r in history or []:
        for mn in mentions(str(r.get("body_in") or ""), today):
            for d in mn.dates:
                g.allow(d, f"an earlier email: '{mn.quote}'")
        g.customer_times.update(times_in(str(r.get("body_in") or "")))
        if (r.get("status") or "") == "sent":
            for mn in mentions(str(r.get("draft_reply") or ""), today):
                for d in mn.dates:
                    g.allow(d, f"our sent reply: '{mn.quote}'")
            g.customer_times.update(times_in(str(r.get("draft_reply") or "")))
    for b in existing_bookings or []:
        st = str(b.get("status") or "")
        if st not in ("proposed", "confirmed", "offered"):
            continue
        for d in booking_dates(b):
            g.allow(d, f"{_ref(b)} ({st})")
        if st in ("proposed", "offered"):
            g.open_holds.append(b)
    return g


def add_checked(g: Grounding, name: str, out: dict) -> None:
    """What the diary returned this draft.

    Two shapes, treated differently on purpose (live 2026-09-22: the model
    checked 8 Oct — a date the customer never wrote — the diary echoed the
    span back as free, and the first build let that echo ground the hold):

    * a SPAN check (`available: true, arrival, departure`) only echoes the
      model's own arguments. It marks those dates CHECKED — a later hold on a
      date that IS on record passes check_first — but it never makes a date
      bookable. Checking a date is not the customer naming it.
    * a SLOT LIST (`available: [{date…}]`) is the diary enumerating what is
      free for a period the customer asked about ("next week?"). The diary
      chose those dates, not the model, so they are both checked and allowed.
    """
    if not isinstance(out, dict) or out.get("error"):
        return
    if out.get("available") is True and out.get("arrival") and out.get("departure"):
        for d in _span_of(out):
            g.checked.add(d)
            g.full.discard(d)
        return
    # The diary's OWN counter-offer. When a span is full the check returns the
    # nearest opening, and that date was chosen by the business, not by the
    # model — the same case as a slot list, and bookable for the same reason.
    # Without this the agent was refused a hold on the alternative it had just
    # been handed, and hedged ("would different dates work for you?") instead
    # of offering it (live 2026-09-22, the K4 take).
    na, nd = out.get("nearest_arrival"), out.get("nearest_departure")
    if na and nd:
        a, b = _parse_dt(na), _parse_dt(nd)
        if a and b:
            for d in _span(a.date(), b.date()):
                g.checked.add(d.isoformat())
                g.full.discard(d.isoformat())
                g.allow(d, OURS)
    # A span the diary said is FULL. Remembering this is what stops a circle:
    # without it the dates are simply "not checked", so a hold on them is
    # refused with "check availability first" — which the model had just done,
    # so it checked again, hit the repeat guard, lost the tool and never
    # offered the alternative it had already been handed (live 2026-09-22).
    if out.get("available") is False and out.get("arrival") and out.get("departure"):
        for d in _span_of(out):
            if d not in g.checked:
                g.full.add(d)
        return
    slots = out.get("available")
    if isinstance(slots, list):
        for s in slots:
            d = str((s or {}).get("date") or "")[:10]
            if d:
                g.checked.add(d)
                g.allow(d, "the diary listed it free")


def _span_of(out: dict) -> list[str]:
    a, b = _parse_dt(out.get("arrival")), _parse_dt(out.get("departure"))
    if not (a and b):
        return []
    return [d.isoformat() for d in _span(a.date(), b.date())]


def check_note(g: Grounding | None, name: str, out) -> str | None:
    """Rides on a span check's result when the span is not on record: the
    model may mention it as an alternative, and is told now — not at the
    hold — that it cannot book it."""
    if g is None or not g.on or name not in ("check_availability", "check_appointment_availability"):
        return None
    if not isinstance(out, dict) or out.get("error"):
        return None
    if not (out.get("available") is True and out.get("arrival") and out.get("departure")):
        return None
    stray = [d for d in _span_of(out) if d not in g.dates]
    if not stray:
        return None
    one = len(stray) == 1
    return (f"{_pretty_runs(stray)} is not {'a date' if one else 'among the dates'} this "
            f"customer gave, and checking {'it' if one else 'them'} does not make "
            f"{'it' if one else 'them'} bookable. You may mention "
            f"{'it' if one else 'them'} as an alternative; you may not hold "
            f"{'it' if one else 'them'}. "
            + (f"Dates that ARE on record: {_pretty_runs(sorted(g.dates))}."
               if g.dates else "This customer has named no dates."))


def _parse_dt(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).strip().replace(" ", "T")[:16])
    except (TypeError, ValueError):
        return None


def dates_in_args(name: str, args: dict) -> list[str]:
    """The dates a booking-placing call is asking for. Unparseable input
    returns [] — the tool's own validation reports the bad date; this gate
    only judges dates it can read."""
    if not isinstance(args, dict):
        return []
    if name == "offer_slots":
        return sorted({str((s or {}).get("date") or "")[:10]
                       for s in (args.get("slots") or []) if (s or {}).get("date")})
    if name == "offer_stay":
        out: set[str] = set()
        for span in (args.get("stays") or []):
            a, b = _parse_dt((span or {}).get("arrival")), _parse_dt((span or {}).get("departure"))
            if a and b and b > a:
                out.update(d.isoformat() for d in _span(a.date(), b.date()))
        return sorted(out)
    if args.get("arrival") and args.get("departure"):
        a, b = _parse_dt(args["arrival"]), _parse_dt(args["departure"])
        if a and b and b > a:
            return [d.isoformat() for d in _span(a.date(), b.date())]
        return []
    for key in ("date", "new_date", "to_date"):
        v = args.get(key)
        if v:
            return [str(v)[:10]]
    return []


def _pretty_runs(isos, limit: int = 4) -> str:
    """Consecutive days read as a span, the way a person would write them.

    A model may check a whole month in one call (live 2026-09-22: "a few nights
    in November" → a 29-night check), and enumerating thirty dates made both
    the refusal and the ⓘ unreadable. `['2026-11-01'…'2026-11-30']` is
    "1 Nov to 30 Nov"; gaps stay separate."""
    try:
        ds = sorted({date.fromisoformat(str(d)[:10]) for d in isos})
    except ValueError:
        return ", ".join(_pretty(str(d)) for d in list(isos)[:limit])
    if not ds:
        return ""
    runs: list[list[date]] = [[ds[0], ds[0]]]
    for d in ds[1:]:
        if (d - runs[-1][1]).days == 1:
            runs[-1][1] = d
        else:
            runs.append([d, d])
    parts = [_pretty(a.isoformat()) if a == b else f"{_pretty(a.isoformat())} to {_pretty(b.isoformat())}"
             for a, b in runs[:limit]]
    if len(runs) > limit:
        parts.append(f"+{len(runs) - limit} more")
    return ", ".join(parts)


def _pretty(iso: str) -> str:
    # No %-d: Windows strftime has no such directive (the video tools note the
    # same trap). Day as an int, month abbreviated.
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b')}"
    except ValueError:
        return iso


def _open_holds_line(g: Grounding) -> str:
    if not g.open_holds:
        return ""
    parts = []
    for b in g.open_holds:
        ds = booking_dates(b)
        when = f"{ds[0]} to {ds[-1]}" if len(ds) > 1 else (ds[0] if ds else "?")
        parts.append(f"{_ref(b)} ({when}, {b.get('status')})")
    return (" The only booking(s) already in play for this customer: " + "; ".join(parts)
            + ". If the customer is confirming or answering about one of those, say so — "
              "it needs NO new hold.")


def check(g: Grounding | None, name: str, args: dict) -> dict | None:
    """The refusal for a booking-placing call, or None. `missing_info` so the
    loop's existing gate machinery ('STOP CALLING THIS TOOL… ask them')
    applies unchanged — that is A5: it doesn't know, so it asks."""
    if g is None or not g.on:
        return None
    if name not in ("hold_booking", "hold_appointment", "offer_slots", "offer_stay",
                    "move_booking"):
        return None
    wanted = dates_in_args(name, args)
    if not wanted:
        return None
    stray = [d for d in wanted if d not in g.dates]
    if stray:
        known = sorted(g.dates)
        return {
            "error": "missing_info", "reason": "ungrounded_date",
            "missing": ["a date the customer actually gave"],
            "ungrounded": stray,
            "grounded_dates": known,
            "message": (
                f"REFUSED: {_pretty_runs(stray)} appears in no email from "
                "this customer, no sent reply and no booking. Checking a date does not "
                "make it bookable. You may only book a date the customer can be shown "
                "to have named. "
                + (f"Dates that ARE on record: {_pretty_runs(known)}."
                   if known else "This email names no dates at all.")
                + _open_holds_line(g)
                + " Do not guess a date. Ask the customer which dates they mean."),
        }
    if name in ("hold_booking", "hold_appointment", "move_booking"):
        # A date WE suggested because what they asked for was full. Holding it
        # is not a reservation — approving the reply CONFIRMS a proposed hold
        # (db.resolve_holds_for_log), so the customer would be booked into
        # dates they have never seen, let alone agreed to. Live 2026-09-22:
        # Jess asked for 17-19 October, was told it was full, and was confirmed
        # into 19-21 October one second after the owner hit send.
        ours = [d for d in wanted if d in g.dates and chose_it_ourselves(g, d)]
        if ours:
            return {
                "error": "not_agreed", "reason": "not_agreed",
                "suggested": ours,
                "message": (f"REFUSED: {_pretty_runs(ours)} is not a date this customer "
                            "asked for — the diary suggested it because the dates they "
                            "DID ask for are full. Approving this reply would confirm a "
                            "booking they have not agreed to. Do not hold it — reserve it "
                            "with offer_stay (or offer_slots), which keeps the space for "
                            "them against a deadline without booking it, then say the dates "
                            "they wanted are full and ask whether this suits. Once they say "
                            "yes, it is theirs and you may hold it."),
            }
    if name in ("hold_booking", "hold_appointment"):
        # The customer ACCEPTING what we already reserved for them is not a new
        # booking, and must not be judged like one. Live 2026-09-22: Jess said
        # "Monday to Wednesday is perfect" against her own offered BK-1049, and
        # this gate demanded an availability check first — so the model checked,
        # the diary answered "full" because BK-1049's own capacity was in the
        # way, and the reply told her the slot had been lost. It had not: it was
        # hers. The executor's acceptance path flips that row in place, and it
        # never got the chance to run.
        held_dates: set[str] = set()
        for b in g.open_holds:
            held_dates.update(booking_dates(b))
        if wanted and set(wanted) <= held_dates:
            return None
        shut = [d for d in wanted if d in g.full]
        if shut:
            return {
                "error": "full", "reason": "already_full",
                "full": shut,
                "message": (f"REFUSED: the diary has already told you {_pretty_runs(shut)} "
                            "is full, in this draft. Asking again will not change it and "
                            "holding it is not possible. Tell the customer those dates are "
                            "full and offer the nearest opening you were given."),
            }
        unchecked = [d for d in wanted if d not in g.checked]
        if unchecked:
            return {
                "error": "check_first", "reason": "check_first",
                "unchecked": unchecked,
                "message": (
                    f"REFUSED: call check_availability for {_pretty_runs(unchecked)} "
                    "BEFORE holding — a hold is placed only for dates the diary has just "
                    "confirmed free, in this draft." + _open_holds_line(g)),
            }
    return None


# The provenance string for a date the DIARY proposed, not the customer. Named
# once because `check` reads it back: a date only we put in play may be offered,
# never held (see `check`'s not_agreed refusal).
OURS = "the diary offered it as the nearest opening"


def time_gaps(g: Grounding | None, args: dict) -> list[str]:
    """A stay's drop-off / collection TIME the customer never gave — the labels
    to ask for. The dates are grounded elsewhere; this is the same rule for the
    clock: a time is the customer's to choose, never ours to fill in from the
    opening hours and then state back to them as theirs (K11 rehearsal,
    2026-09-23: "drop-off 8am, collection 5pm" to a customer who named no time).
    Evidence is a time the customer wrote, or one a sent reply of ours stated
    and they have had the chance to answer. Empty when grounding is off."""
    if g is None or not g.on:
        return []
    out = []
    for key, label in (("arrival", "Drop-off time"), ("departure", "Collection time")):
        dt = _parse_dt(args.get(key))
        if dt is None or "T" not in str(args.get(key) or ""):
            continue
        if dt.strftime("%H:%M") not in g.customer_times:
            out.append(label)
    return out


def chose_it_ourselves(g: Grounding, iso: str) -> bool:
    """Did the customer put this date in play, or did we? A date carrying only
    our counter-offer as its source is one nobody has agreed to."""
    why = g.why.get(iso) or []
    return bool(why) and all(w == OURS for w in why)


def no_dates(g: Grounding | None) -> bool:
    """Is there nothing in this conversation the agent could act on?

    Note what this rules out: a live booking grounds its own dates, so an empty
    set also means the customer has nothing to cancel or move. There is no
    diary work of any kind to do — which is why the caller hands over no diary
    tools at all rather than a reduced set. A reduced set was tried first and
    the model simply reached for whatever was left, inventing a booking
    reference to cancel (live 2026-09-22)."""
    return g is not None and g.on and not g.dates


def note(g: Grounding | None) -> str:
    """One line for the ⓘ: what the draft was allowed to book, and from where."""
    if g is None or not g.on:
        return ""
    if not g.dates:
        return ("Grounded dates: none — this email and the thread name no dates, so "
                "no hold or offer can be placed without asking." + _checked_off_record(g))
    # Consecutive days from the same source are one fact, not four lines:
    # "17 Oct to 19 Oct ← BK-1016 (confirmed)".
    groups: list[tuple[str, list[str]]] = []
    for iso in sorted(g.dates):
        why = "; ".join(g.why.get(iso, [])[:2])
        if groups and groups[-1][0] == why and _next_day(groups[-1][1][-1], iso):
            groups[-1][1].append(iso)
        else:
            groups.append((why, [iso]))
    parts = [f"{_pretty_runs(isos)} ← {why}" for why, isos in groups[:8]]
    more = "" if len(groups) <= 8 else f" (+{len(groups) - 8} more)"
    return "Grounded dates: " + " | ".join(parts) + more + _checked_off_record(g)


def _next_day(a: str, b: str) -> bool:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days == 1
    except ValueError:
        return False


def _checked_off_record(g: Grounding) -> str:
    """The dates the model asked the diary about that nobody named — shown so
    a reader can see the model wandering even when the gate held."""
    stray = sorted(g.checked - g.dates)
    if not stray:
        return ""
    return " · checked but not on record (never bookable): " + _pretty_runs(stray)
