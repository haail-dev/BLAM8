"""What kind of thing a promise is — and so which surface owns it.

Kris (2026-09-21, filming the CRM card): "the three 'You owe' promises are all
bookings, waiting for the day to arrive. Showing this under 'You owe' makes it
feel like pending work. I expect there's other types mislabelled too." On the
kennels rig 24 of 61 open promises were bookings the diary already held, 12
were the customer's on-the-day logistics, one a deposit the invoice tracks, one
a statement of something already done — and only 23 were obligations anyone
still owed anyone.

The extractor (src/app/crm_intel.py) reads "concrete commitments in this email", and
a confirmation is nothing but commitments — we've booked him in, bring his food,
pay the deposit — with "we owe" / "they owe" as the only labels. This module
sorts a promise into the kind it is, from its words, so each surface can show
only what it should:

    obligation   the real ones, both ways — "You owe" / "They owe" everywhere
    booking      the diary holds it ("Host Biscuit for BK-1024…") — hidden here
    payment      Money holds it ("pay the deposit by bank transfer") — hidden here
    on_the_day   bring the food, arrive at 1pm, meds labelled — "On the day" on
                 the card, never a debt
    done         a statement of something already done ("(attached)") — hidden

Read-time and deterministic, so it covers every row already on the book and
needs no schema change; a stored kind decided at extraction can replace it.
The drafter, the portal and the concierge keep the unfiltered list — knowing
the deposit is owed or the food is coming is worth having when writing.
"""
from __future__ import annotations

import re

KINDS = ("obligation", "booking", "payment", "on_the_day", "done")

_BK_REF = re.compile(r"\bBK-\d+\b", re.I)
_CANCEL = re.compile(r"\b(cancel|cancell(ed|ing)|refund|move|reschedul)", re.I)
_DONE = re.compile(r"\(attached\b|\battached as\b|\bhas (already )?(sent|provided|paid|confirmed)\b|"
                   r"\bhave (already )?(sent|provided|paid)\b|\balready (sent|provided|paid|attached)\b", re.I)
_BOOKING = re.compile(r"^\s*host\b|\bhost(ing)? [A-Z]|\bbooked (in|for)\b|\bis booked\b|\bbook (him|her|them|it) in\b|"
                      r"\bconfirmed for\b|\breserve[ds]? (a|the) (space|slot|kennel|place)\b", re.I)
_PAYMENT = re.compile(r"\b(deposit|invoice|pay(ment|ing)?|bank transfer|balance due|settle)\b", re.I)
_ON_THE_DAY = re.compile(r"\b(bring|brought|collect(ion|ed|s)?|drop[- ]?off|drop (him|her|them|[A-Z]\w+) off|"
                         r"arrive|arriving|pick(ing)? (him|her|them|[A-Z]\w+) up|pick-up|"
                         r"food bag|own food|bedding|blanket|lead|coat|harness|"
                         r"tablet|medication|meds\b|dose|labelled|labeled|portioned|"
                         # care notes for whoever is on site — ours to do on the day, not a debt
                         r"feeding|food|lunch|breakfast|dinner|mixes? with|"
                         r"nervous|sensitiv\w*|keep (him|her|them) (away|apart|separate)|"
                         # what we do DURING the visit — photos, updates — is on the day too
                         r"during (their|the|his|her|its) (stay|visit|day)|while (he|she|they|it)('s| is| are)? (here|with us))\b", re.I)
# A commitment that waits on something else ("once the trial goes well", "as
# soon as the card arrives") is not a thing to do today.
_CONDITIONAL = re.compile(r"\b(once|as soon as|when|if|provided|subject to|pending|after (the|their|his|her)\b)", re.I)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_DUE_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\b", re.I)


def kind(promise: dict) -> str:
    """The kind of a promise row, from its description (and due hint)."""
    text = f"{promise.get('description') or ''} {promise.get('due_hint') or ''}".strip()
    if not text:
        return "obligation"
    if _DONE.search(text):
        return "done"
    # A booking reference with cancel/move on it is work someone still owes;
    # a booking reference otherwise is the diary's, not a promise.
    if _BK_REF.search(text) or _BOOKING.search(text):
        return "obligation" if _CANCEL.search(text) else "booking"
    if _PAYMENT.search(text):
        return "payment"
    if _ON_THE_DAY.search(text):
        return "on_the_day"
    return "obligation"


def annotate(rows: list[dict]) -> list[dict]:
    """The same rows, each with its `kind` written on."""
    for r in rows or []:
        r["kind"] = kind(r)
    return rows or []


def obligations(rows: list[dict]) -> list[dict]:
    """Only what someone still owes someone — what 'You owe' / 'They owe' mean."""
    return [r for r in (rows or []) if kind(r) == "obligation"]


def on_the_day(rows: list[dict]) -> list[dict]:
    return [r for r in (rows or []) if kind(r) == "on_the_day"]


# ── What is a thing to do TODAY (Kris 2026-09-21: Today is a notification ────
# centre that lists things to do; work through it and only the day is left).
# An obligation reaches Today's deck only when it is the owner's to do now:
# ours (not the customer's — waiting on them is the card's business and the
# chase's), not conditional on something that has not happened, and not dated
# further off than a couple of days.
def due_date(promise: dict, today):
    """The date a promise's due hint names, or None. Understands 'today',
    'tomorrow' and a day-and-month ('Tuesday 29 September', 'from 29 Sept',
    'after 29 September'); a date already past this year is next year's."""
    from datetime import date, timedelta
    hint = (promise.get("due_hint") or "").strip().lower()
    if not hint:
        return None
    if "tomorrow" in hint:
        return today + timedelta(days=1)
    if "today" in hint or "straight away" in hint or "asap" in hint:
        return today
    m = _DUE_DATE.search(hint)
    if not m:
        return None
    day = int(m.group(1))
    mon = next((i + 1 for i, name in enumerate(_MONTHS) if name.startswith(m.group(2).lower()[:3])), None)
    if not mon:
        return None
    try:
        d = date(today.year, mon, day)
    except ValueError:
        return None
    if d < today - timedelta(days=60):          # long past: they meant next year
        d = date(today.year + 1, mon, day)
    return d


def actionable_today(promise: dict, today, *, soon_days: int = 2) -> bool:
    """Should this promise sit on Today as a thing to do?"""
    if kind(promise) != "obligation" or promise.get("direction") != "we_owe":
        return False
    if _CONDITIONAL.search(promise.get("description") or ""):
        return False
    d = due_date(promise, today)
    return d is None or (d - today).days <= soon_days


# ── The route that fulfils a promise (Kris 2026-09-21: "because this task is ──
# for the Diary, we should have a route highlighted to complete the task").
# A promise we owe names the surface that does it: a booking to lock in is the
# Diary's (the Add form, prefilled and ready), money is the invoice's, a
# reply is the thread's, a policy is Knowledge's. Pure: the pop-up and the
# card render whatever comes back; None means no door beyond the email.
_WEEKDAY_WORDS = (("mon", r"\bmon(day)?s?\b"), ("tue", r"\btue(s|sday)?s?\b"), ("wed", r"\bwed(nesday)?s?\b"),
                  ("thu", r"\bthu(rs|rsday)?s?\b"), ("fri", r"\bfri(day)?s?\b"), ("sat", r"\bsat(urday)?s?\b"),
                  ("sun", r"\bsun(day)?s?\b"))
_DIARY_WORDS = re.compile(r"\b(lock in|book(ed)? (him|her|them|it|in)|book in|reserve|hold|slots?|regular|series|standing|"
                          r"daycare|boarding|stay|session|lesson|class|appointment|visit)\b", re.I)
_MONEY_WORDS = re.compile(r"\b(invoice|deposit|refund|balance|bill|charge|receipt)\b", re.I)
_REPLY_WORDS = re.compile(r"\b(send|let (them|him|her|you) know|confirm|reply|photos?|update|tell|get back|follow up|ring|call|email)\b", re.I)
_KNOWLEDGE_WORDS = re.compile(r"\b(policy|rule|price list|pricing|website|knowledge)\b", re.I)
_EVERY_N_WEEKS = re.compile(r"\b(\d{1,2})[- ]week\b", re.I)


def weekdays_in(text: str) -> list[str]:
    """The weekdays a sentence names, in week order: 'Mondays, Wednesdays and
    Thursdays' → ['mon', 'wed', 'thu']."""
    t = text or ""
    return [key for key, pat in _WEEKDAY_WORDS if re.search(pat, t, re.I)]


def route(promise: dict, *, contact: dict | None = None, pets=(), today=None) -> dict | None:
    """{kind, label, short, href} for a promise WE owe, or None."""
    from datetime import date, timedelta
    from urllib.parse import urlencode
    if promise.get("direction") != "we_owe" or kind(promise) != "obligation":
        return None
    text = promise.get("description") or ""
    contact = contact or {}
    email = (contact.get("email") or "").strip()
    who = (contact.get("name") or email or "them").split(" ")[0]
    today = today or date.today()
    names = [p for p in (pets or []) if p]
    pet = next((p for p in names if re.search(r"\b" + re.escape(p) + r"\b", text, re.I)), names[0] if len(names) == 1 else "")
    if _DIARY_WORDS.search(text):
        days = weekdays_in(text)
        repeat = "weekly" if (days or re.search(r"\b(weekly|every week|regular)\b", text, re.I)) else ""
        m = _EVERY_N_WEEKS.search(text)
        occurrences = int(m.group(1)) if m else 12
        # The first date: the earliest named weekday from tomorrow, else tomorrow.
        first = today + timedelta(days=1)
        if days:
            order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            first = min((first + timedelta(days=(order.index(d) - first.weekday()) % 7)) for d in days)
        q = {"date": first.isoformat(), "add": "1", "contact": email,
             "summary": (f"Daycare for {pet}" if pet and repeat else f"Booking for {pet}" if pet else ""),
             "subjects": pet, "repeat": repeat, "occurrences": str(occurrences) if repeat else "",
             "weekdays": ",".join(days), "promise": promise.get("id") or ""}
        href = "/app/diary?" + urlencode({k: v for k, v in q.items() if v})
        what = f"{pet}'s regular days" if pet and repeat else (f"{pet}'s booking" if pet else "the booking")
        return {"kind": "diary", "label": f"Add {what} in the Diary", "short": "Diary", "href": href}
    if _MONEY_WORDS.search(text):
        return {"kind": "money", "label": "Open Money", "short": "Money", "href": "/app/money"}
    if _REPLY_WORDS.search(text) and email:
        from urllib.parse import quote
        return {"kind": "reply", "label": f"Reply to {who}", "short": "Reply",
                "href": f"/app/conversations/view?contact={quote(email)}"}
    if _KNOWLEDGE_WORDS.search(text):
        return {"kind": "knowledge", "label": "Add it to Knowledge", "short": "Knowledge", "href": "/app/knowledge"}
    return None
