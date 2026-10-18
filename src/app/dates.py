"""How a date is written for a person to read.

ISO dates are for databases. Anywhere one reaches the operator — the thread's
hold trail, the Agent Log's diary link, the summaries the agents write into
agent_events — it should read as a date (Kris 2026-09-01: "instead of
2026-09-11 it should be 11th Sep 26").

Deliberately a leaf module: stdlib only, imported by db/pipeline/dashboard and
registered as the `pretty_date` Jinja filter in src/app/templating.py, so the same
wording appears whoever renders it.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = ["pretty_date", "pretty_day", "sent_when"]

_LONDON = ZoneInfo("Europe/London")


def _ordinal(n: int) -> str:
    # 1st/2nd/3rd/4th… — the teens are all 'th' (11th, 12th, 13th).
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _as_date(value):
    """The shared front door: a date/datetime through, an ISO-ish string parsed,
    anything else None — callers pipe values straight through without first
    proving they hold a date."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def pretty_date(value) -> str:
    """'11th Sep 26'. Accepts a date/datetime or any ISO-ish string, and returns
    anything it cannot parse untouched — callers pipe values straight through
    without first proving they hold a date, and a summary line must never blow
    up (or lose its date) over an odd value."""
    if value is None or value == "":
        return ""
    d = _as_date(value)
    if d is None:
        return str(value)
    return f"{_ordinal(d.day)} {d.strftime('%b')} {d.strftime('%y')}"


def pretty_day(value) -> str:
    """'Monday 7th September' — the same date said to a CUSTOMER rather than
    filed for the operator: the weekday is what someone checks a promise
    against, and the year is noise inside a booking horizon. Same
    parse-anything, return-anything-odd-untouched contract as pretty_date."""
    if value is None or value == "":
        return ""
    d = _as_date(value)
    if d is None:
        return str(value)
    return f"{d.strftime('%A')} {_ordinal(d.day)} {d.strftime('%B')}"


def _clock(dt: datetime) -> str:
    """'8pm', '12:08pm', 'noon' — how a deadline is said out loud."""
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    h = dt.hour % 12 or 12
    return f"{h}{'' if dt.minute == 0 else f':{dt.minute:02d}'}{'am' if dt.hour < 12 else 'pm'}"


def deadline_phrase(deadline: datetime, now: datetime | None = None) -> str:
    """When a held slot lets go, said to the customer the way a person would
    (run log F14, services take 2026-09-25: "held until Friday 25 September at
    20:00" was written ON the Friday): '8pm tonight', '3pm today', 'tomorrow
    at 8am', 'Sunday at 12:08pm' inside the week, 'Monday 5th October at 10am'
    beyond it. Both in diary (London) time."""
    from app.scheduling import DIARY_TZ
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    d = deadline.astimezone(DIARY_TZ)
    if d.hour == 0 and d.minute == 0:
        # Midnight ends the day before (live 2026-09-26: "until tomorrow at
        # 12am" for a hold that ended at the end of today).
        d = d - timedelta(minutes=1)
        days = (d.date() - now.date()).days
        return ("midnight tonight" if days <= 0 else "midnight tomorrow" if days == 1
                else f"midnight on {d.strftime('%A')}" if days < 7
                else f"midnight on {pretty_day(d.date())}")
    days = (d.date() - now.date()).days
    clock = _clock(d)
    if days <= 0:
        return f"{clock} tonight" if d.hour >= 17 else f"{clock} today"
    if days == 1:
        return f"tomorrow at {clock}"
    if days < 7:
        return f"{d.strftime('%A')} at {clock}"
    return f"{pretty_day(d.date())} at {clock}"


def sent_when(value, now: datetime | None = None) -> str:
    """When a message went or came in, for the thread's bubbles (sent and
    inbound alike): '14:32' while it is
    still that (London) day, '22nd Sep 26 · 14:32' from the next day on (Kris
    2026-09-23). A timestamp; a naive one is taken as UTC (how the database
    hands them over). Anything unparseable or empty → '' so the bubble just
    shows no time rather than a raw string."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(_LONDON)
    today = (now or datetime.now(timezone.utc)).astimezone(_LONDON).date()
    clock = local.strftime("%H:%M")
    return clock if local.date() == today else f"{pretty_date(local)} · {clock}"
