"""Today's deck: what happened, on the card of whoever it happened to.

Kris, 2026-09-04: "the current Today page, it's technically a glorified
CRM/Inbox. We should use it more like a mobile notification system."

The deck was built entirely out of STATE — three drafts waiting, gone quiet
40 days, an invoice overdue. All true, all recomputed on every render, and
none of it able to say what actually *happened*. Meanwhile the notification
feed had exactly that, twenty kinds of it, visible only inside the concierge
bubble where one glance marked the lot seen.

So the feed comes to the deck. Not as a second list next to it — as facts on
the same person-keyed glass cards, which is what a phone does when it stacks
a morning's notifications under the app they came from. A card is now a
person's stack: what they did, when, alongside what they still need from you.

Two ideas hold it up.

**Topics, not kinds.** Cards rank by the loudest thing on them, and a rank
table with twenty-six entries is a table nobody can hold in their head or
sensibly offer as a setting. Everything the deck can say — state and event
alike — falls into eight topics, and it is topics that carry the order.

**The order is the operator's.** A business chasing money and another business
chasing no-shows do not agree about what belongs at the top of the morning,
and neither of them is wrong. Each topic takes first / normal / last, the
same three-way shape the notification families use one screen away, over a
default that reproduces the order the deck already shipped with.

Pure module: no I/O, no db import. dashboard.py does the reads and hands the
rows in, which is what makes the ranking testable without a Supabase in the
room.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from app import notify_prefs
from app.scheduling import DIARY_TZ

# ── The eight topics ────────────────────────────────────────────────────────
# `order` is the default rank. It reproduces what the deck shipped with —
# running hot · draft · booked · overdue · quiet — with the two genuinely new
# event topics slotted where they belong: a customer's question sits with the
# drafts it will become, and a quote's progress sits under the money already
# owed.
#
# PROMISES sit low (Kris 2026-09-04: "there's an appointment booked for less
# than 24hrs away that could be shown instead of what's owed each way"). The
# original deck had this right and the move to topics broke it: it ranked a
# promise WITH a due date first and one without sixth, and collapsing the two
# into one topic put "They owe: <description>" above everything on the card.
# The distinction is back as a general rule rather than a special case —
# anything carrying a time floats once that time is close (IMMINENT_WINDOW), so
# a promise due today leads and one with no date waits its turn.
#
# `kinds` are the notification kinds that land in the topic. Facts the deck
# derives from state carry their topic directly.
TOPICS: dict[str, dict] = {
    "promises":  {"label": "Promises", "order": 7,
                  "help": "Something you said you'd do, or they did", "kinds": set()},
    "unhappy":   {"label": "Unhappy customers", "order": 1,
                  "help": "The thread has turned — look before it hardens",
                  "kinds": set()},
    "drafts":    {"label": "Drafts waiting", "order": 3,
                  "help": "A reply is written and waiting on your yes",
                  "kinds": {"draft_ready"}},
    "questions": {"label": "Questions", "order": 2,
                  "help": "Somebody asked you something directly",
                  "kinds": {"portal_escalation", "quote_question", "owner_rule"}},
    "bookings":  {"label": "Bookings", "order": 4,
                  "help": "Arriving, collecting, booked, moved, or a reschedule asked for",
                  "kinds": {"dependant_expiry", "portal_booking_created", "portal_booking_moved",
                            "portal_reschedule_request", "level_change",
                            "waitlist_offer", "reenrol_window", "customer_note",
                            "certificate_expiry", "hand_cancel",
                            "team_on_way", "team_job_done", "team_photos", "after_job_due",
                            "vulnerable_household", "out_of_hours_ask", "alert_words"}},
    "money":     {"label": "Money", "order": 5,
                  "help": "Invoices: paid, opened, overdue, failed, refunded or disputed",
                  "kinds": {"invoice_paid", "invoice_viewed", "invoice_overdue",
                            "invoice_payment_seen", "invoice_payment_failed", "package_balance",
                            # card payments panel (2026-09-18): a refund, a dispute, a bank
                            # payment in flight all land on the customer's card
                            "invoice_refunded", "invoice_disputed", "invoice_payment_pending",
                            "invoice_balance_ready"}},
    "quotes":    {"label": "Quotes", "order": 6,
                  "help": "Drafted, opened, accepted, declined or expired",
                  "kinds": {"quote_drafted", "quote_viewed", "quote_accepted",
                            "quote_declined", "quote_expired",
                            "quote_keeps_looking"}},
    # Records (Kris 2026-09-21, Today as a notification centre): the book
    # itself needing a hand — a detail BLAM8 read but wants confirmed, a
    # document nobody has labelled. Between quotes and promises: worth a tap
    # this morning, never ahead of money.
    "records":   {"label": "Records", "order": 6.5,
                  "help": "A detail to confirm, a document to label", "kinds": set()},
    "quiet":     {"label": "Gone quiet", "order": 8,
                  "help": "No word either way for a while", "kinds": set()},
}

_TOPIC_BY_KIND = {k: t for t, spec in TOPICS.items() for k in spec["kinds"]}

FIRST, NORMAL, LAST = "first", "normal", "last"
PLACEMENTS = (FIRST, NORMAL, LAST)

# How far a placement moves a topic. Bigger than any default order, so "first"
# always outranks every normal topic and "last" always falls below them —
# while ties inside each band keep the default order, which is why a tenant who
# floats three topics still gets a sensible sequence among those three.
_SHIFT = {FIRST: -100, NORMAL: 0, LAST: 100}

# How long an event stays on Today. A notification is news; past a week it is
# history, and history belongs in the feed and the CRM timeline, not on the
# card of somebody you are deciding about this morning.
EVENT_WINDOW = timedelta(days=7)

# How soon "soon" is. A booking inside this window is the thing you most need
# to know about that person, whatever else is outstanding with them.
IMMINENT_WINDOW = timedelta(hours=24)

# How far imminence lifts a fact: past every NORMAL topic (orders 1-8), but not
# past a topic the operator explicitly placed first (_SHIFT, 100). The clock
# reorders the middle; it does not overrule a stated preference.
_IMMINENT_SHIFT = 50

# How far ahead the deck looks at the diary at all. list_bookings returns
# everything from today forward — the whole diary is not Today.
BOOKING_HORIZON = timedelta(days=7)

# How each notification kind reads on a card. The push title is written for a
# lock screen ("Quote QUO-1012 accepted ✓") and would repeat the person's name
# back at them on their own card, so the chip says the event in two or three
# words and the body carries the detail.
_CHIPS: dict[str, tuple[str, str]] = {
    "draft_ready":              ("Draft ready", "brand"),
    "dependant_expiry":         ("Lapsing", "amber"),
    "level_change":             ("Moved up", "emerald"),
    "hand_cancel":              ("Cancelled by you", "amber"),
    "team_on_way":              ("On the way", "brand"),
    "team_job_done":            ("Job done", "emerald"),
    "team_photos":              ("Photos from the job", "ink"),
    "after_job_due":            ("After the job", "amber"),
    "vulnerable_household":     ("May need to come first", "amber"),
    "out_of_hours_ask":         ("Out of hours?", "amber"),
    "alert_words":              ("Alert word", "rose"),
    "owner_rule":               ("Your rule applied", "ink"),
    "waitlist_offer":           ("Place offered", "amber"),
    "reenrol_window":           ("Re-enrol", "amber"),
    "customer_note":            ("Their note", "violet"),
    "certificate_expiry":       ("Certificate", "amber"),
    "package_balance":          ("Running low", "amber"),
    "portal_booking_created":   ("Booked", "emerald"),
    "portal_booking_moved":     ("Moved", "brand"),
    "portal_reschedule_request": ("Wants to move", "amber"),
    "portal_escalation":        ("Asked you", "amber"),
    "quote_question":           ("Asked about a quote", "amber"),
    "quote_drafted":            ("Quote drafted", "brand"),
    "quote_viewed":             ("Opened your quote", "ink"),
    "quote_keeps_looking":      ("Keeps looking", "amber"),
    "quote_accepted":           ("Quote accepted", "emerald"),
    "quote_declined":           ("Quote declined", "rose"),
    "quote_expired":            ("Quote expired", "rose"),
    "invoice_paid":             ("Paid", "emerald"),
    "invoice_viewed":           ("Opened your invoice", "ink"),
    "invoice_overdue":          ("Overdue", "rose"),
    "invoice_payment_seen":     ("Says they've paid", "amber"),
    "invoice_payment_failed":   ("Payment failed", "rose"),
    "invoice_refunded":         ("Refunded", "ink"),
    "invoice_disputed":         ("Disputed", "rose"),
    "invoice_payment_pending":  ("Bank payment on its way", "amber"),
    "invoice_balance_ready":    ("Balance ready to take", "brand"),
}


def silenced(cfg: dict | None, kind: str) -> bool:
    """Has this tenant asked not to hear about this kind at all?

    True for a family set to "never", and for one of the two `viewed` opt-ins
    they have switched off. NOT true for "in the brief" — that is a request to
    be told later, not to be spared, and Today is where later happens."""
    return (notify_prefs.opted_out(cfg, kind)
            or notify_prefs.mode(cfg, kind) == notify_prefs.NEVER)


def topic_of(kind: str) -> str | None:
    """The topic a notification kind belongs to, or None when it is not a deck
    event at all — the morning brief and the operator's own platform pushes are
    about the business, not about a person, and have no card to land on."""
    return _TOPIC_BY_KIND.get(kind)


def placements(cfg: dict | None) -> dict[str, str]:
    """Every topic's first/normal/last, defaulted and validated. A junk value
    reads as `normal`: a broken setting must leave the deck in its default
    order, never empty it or invert it."""
    stored = (cfg or {}).get("deck_priority") or {}
    if not isinstance(stored, dict):
        stored = {}
    return {t: (stored.get(t) if stored.get(t) in PLACEMENTS else NORMAL)
            for t in TOPICS}


ORDER_KEY = "order"
# Topics added after tenants began saving orders. A saved list may lack these
# and still count — they slot in at their default place; lacking any OTHER
# topic is junk, as it always was.
_ADDED_LATER = frozenset({"records"})
_ORDER_STEP = 30   # rungs apart, so an imminent lift (50) climbs about two rungs, never past the top


def clean_order(values) -> list[str] | None:
    """An order of the topics — each once, no strangers — else None. Junk (a
    repeat, a stranger, nothing at all) leaves the deck in its default order
    rather than half-reordering it. A topic added since the order was saved
    (records, 2026-09-21) slots in at its default place, so the tenant's
    ordering of the others survives the addition."""
    if not isinstance(values, (list, tuple)):
        return None
    seen: list[str] = []
    for v in values:
        t = str(v or "").strip()
        if t not in TOPICS or t in seen:
            return None
        seen.append(t)
    if not seen or any(t not in seen for t in TOPICS if t not in _ADDED_LATER):
        return None
    default = [t for t, _ in sorted(TOPICS.items(), key=lambda kv: kv[1]["order"])]
    for i, t in enumerate(default):
        if t in seen:
            continue
        prev = next((p for p in reversed(default[:i]) if p in seen), None)
        seen.insert(seen.index(prev) + 1 if prev else 0, t)
    return seen


def explicit_order(cfg: dict | None) -> list[str] | None:
    """The order the tenant moved the topics into (the cog on Today, or
    Settings › Today's order), validated — or None when they have only ever
    used the older first/normal/last placements, or nothing at all."""
    stored = (cfg or {}).get("deck_priority") or {}
    if not isinstance(stored, dict):
        return None
    return clean_order(stored.get(ORDER_KEY))


def rank_of(topic: str, cfg: dict | None = None, *, urgent: bool = False,
            imminent: bool = False) -> int:
    """A fact's sort key. Topic order decides it, the operator's placement
    shifts it, `urgent` lifts a fact above its own topic-mates, and `imminent`
    lifts one that is about to HAPPEN above the topics entirely.

    Imminence is its own axis because it is a different question. Topic order
    answers "what kind of thing matters to me"; imminence answers "what happens
    next", and an appointment in two hours outranks anything outstanding
    (Kris 2026-09-04). It is deliberately a smaller shift than a placement, so
    a tenant who put a topic FIRST still meets that topic first — they said
    that is what they want at the top, and the clock does not overrule them."""
    spec = TOPICS.get(topic)
    if spec is None:
        return 999
    order = explicit_order(cfg)
    if order is not None:
        # The tenant's own list (Kris 2026-09-13): its rung IS the rank; the
        # clock still lifts an imminent fact a couple of rungs, never past
        # what they put first.
        base = order.index(topic) * _ORDER_STEP
    else:
        base = spec["order"] + _SHIFT[placements(cfg)[topic]]
    if imminent:
        base -= _IMMINENT_SHIFT
    return base * 10 - (1 if urgent else 0)


def ordered_topics(cfg: dict | None = None) -> list[tuple[str, dict]]:
    """Topics in the order this tenant will actually see them — what the
    settings row lists, so the page shows the consequence of the choice."""
    return sorted(TOPICS.items(), key=lambda kv: rank_of(kv[0], cfg))


def summary(cfg: dict | None = None) -> str:
    """One line for the settings face: what Today actually leads with.

    Names the top topic rather than counting choices — "3 moved" tells you
    nothing you can picture, and the whole point of the setting is which card
    you meet first in the morning."""
    order = ordered_topics(cfg)
    places = placements(cfg)
    head = "Leads with " + order[0][1]["label"].lower()
    hidden = [spec["label"].lower() for t, spec in order if places[t] == LAST]
    if hidden:
        head += " · " + ", ".join(hidden) + " last"
    if all(p == NORMAL for p in places.values()):
        return head + " (default order)"
    return head


def when_label(stamp, now: datetime | None = None) -> str:
    """A short clock for a card row: "09:42" today, "Yst" yesterday, the weekday
    inside a week, "11 Sep" beyond it. Diary time, because the operator reads a
    wall clock, not UTC. An unparseable stamp reads blank rather than guessing —
    a wrong time on a card is worse than no time."""
    dt = parse_stamp(stamp)
    if dt is None:
        return ""
    now = now or datetime.now(DIARY_TZ)
    days = (now.date() - dt.date()).days
    if days <= 0:
        return dt.strftime("%H:%M")
    if days == 1:
        return "Yst"
    if days < 7:
        return dt.strftime("%a")
    # Built by hand, not %-d: that flag is glibc-only and Windows rejects it.
    return "%d %s" % (dt.day, dt.strftime("%b"))


def parse_stamp(stamp) -> datetime | None:
    """An ISO timestamp from the feed into diary-local time, or None."""
    if not stamp:
        return None
    if isinstance(stamp, datetime):
        dt = stamp
    else:
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DIARY_TZ)


def _booking_start(row: dict, now: datetime) -> tuple[datetime | None, bool]:
    """(when it starts, is the start a real clock time). A diary window is
    "09:00" on a slots diary but "am", "pm" or a named round on others, so half
    the bookings in the platform have a date and no time — those get the day
    without a clock rather than a guessed one."""
    day = None
    try:
        day = datetime.fromisoformat(str(row.get("booking_date"))[:10]).date()
    except (TypeError, ValueError):
        return None, False
    window = str(row.get("window") or "")
    if ":" in window:
        try:
            hh, mm = window.split(":")[:2]
            return datetime(day.year, day.month, day.day, int(hh), int(mm),
                            tzinfo=now.tzinfo), True
        except ValueError:
            pass
    # Date only — placed at the end of the day so "today, some time" never reads
    # as already finished, and never claims to be imminent on the hour.
    return datetime(day.year, day.month, day.day, 23, 59, tzinfo=now.tzinfo), False


def is_imminent(at: datetime, now: datetime) -> bool:
    """Is this booking time close enough to lead its card? Inside
    IMMINENT_WINDOW, OR on today's or tomorrow's date in the diary's calendar —
    the same days when_booking calls "Today" and "Tomorrow".

    The window alone was a rolling 24 hours, so between midnight and 09:00 a
    booking at 09:00 tomorrow was more than a day away, the card still said
    "Tomorrow 09:00", and a draft led the deck over it. The chip and the rank
    now go by the same calendar."""
    return at - now <= IMMINENT_WINDOW or (at.date() - now.date()).days <= 1


def when_booking(start: datetime, clocked: bool, now: datetime) -> str:
    """"In 40 minutes" · "Today 14:00" · "Tomorrow 09:00" · "Fri 09:00".

    The old card said "Booked today" for every booking the query returned,
    including next month's, which is how an appointment tomorrow morning ended
    up looking like background (Kris 2026-09-04)."""
    days = (start.date() - now.date()).days
    clock = start.strftime("%H:%M") if clocked else ""
    if days == 0:
        if clocked:
            minutes = int((start - now).total_seconds() // 60)
            if 0 <= minutes < 90:
                return "In %d minutes" % minutes if minutes > 1 else "Now"
            return "Today " + clock
        return "Today"
    if days == 1:
        return ("Tomorrow " + clock) if clocked else "Tomorrow"
    return (start.strftime("%a") + " " + clock) if clocked else start.strftime("%a")


def booking_facts(rows: list[dict], cfg: dict | None = None,
                  now: datetime | None = None, *, notes: dict | None = None,
                  stay_span=None) -> list[tuple[str, dict]]:
    """Diary rows → (contact, fact), forward-looking.

    Two more things since 2026-09-21 (Kris: Today as a notification centre):
    `stay_span(row)` → (start, end) — when given, a booking whose end is still ahead also owes
    a COLLECTION fact ("Collect · Today 17:30"), and a stay that began
    yesterday still says so today; `notes` — {contact: [what to bring or do]}
    from the customer's on-the-day promises, written onto the arrival once it
    is within a day, so "bring the food portioned out" is on the card the
    morning it matters and nowhere else.

    Three things the old inline version got wrong, all of which pushed the one
    time-critical thing on the page out of sight:

      * every booking was chipped "Booked today", because list_bookings returns
        everything from today FORWARD and nobody filtered it;
      * a booking that had already happened this morning still sat on the card
        as though it were coming;
      * nothing about it floated, so an appointment in two hours ranked below
        an undated promise.

    Pure, like the rest of this module — dashboard.py does the read.
    """
    now = now or datetime.now(DIARY_TZ)
    out: list[tuple[str, dict]] = []
    for row in rows:
        contact = (row.get("contact") or "").strip().lower()
        if not contact:
            continue
        start, clocked = _booking_start(row, now)
        # The collection, on a diary that has one: the stay's end, still ahead.
        # The span also gives a 'whole day' booking its real start — a date-only
        # row is otherwise placed at 23:59, after its own collection.
        end = None
        if stay_span is not None:
            try:
                span = stay_span(row)
            except Exception:                                 # noqa: BLE001
                span = None
            if span:
                s0, end = span
                if s0 is not None:
                    start = s0 if s0.tzinfo else s0.replace(tzinfo=now.tzinfo)
                    clocked = True
                if end is not None and end.tzinfo is None:
                    end = end.replace(tzinfo=now.tzinfo)
        if start is None:
            continue
        proposed = (row.get("status") or "") != "confirmed"
        summary = (row.get("summary") or "").strip() or "Booked"
        coll = None
        items = (notes or {}).get(contact) or []
        if end is not None and end > now and end - now <= BOOKING_HORIZON and end > start:
            day_items = items if (end.date() - now.date()).days <= 1 else []
            coll = (contact, {
                "kind": "collection",
                "topic": "bookings",
                "rank": rank_of("bookings", cfg, imminent=is_imminent(end, now)),
                "chip": "Collect · " + when_booking(end, True, now),
                "tone": "amber" if proposed else "emerald",
                "text": summary + " · collection" + (" · not confirmed" if proposed else "")
                        + (" · " + " · ".join(day_items) if day_items else ""),
                "href": "/app/diary?date=" + end.date().isoformat(),
                "at_ts": end.timestamp(),
            })
        if start < now or start - now > BOOKING_HORIZON:
            if coll:
                out.append(coll)              # begun: only the collection is still ahead
            continue                          # begun, or not Today's problem
        soon = is_imminent(start, now)
        if items and (start.date() - now.date()).days <= 1:
            summary += " · " + " · ".join(items)
        out.append((contact, {
            "kind": "booking",
            "topic": "bookings",
            "rank": rank_of("bookings", cfg, imminent=soon),
            "chip": when_booking(start, clocked, now),
            # Amber for a hold nobody has confirmed: it is not in the diary yet
            # in the way the operator thinks it is.
            "tone": "amber" if proposed else "emerald",
            "text": summary + (" · not confirmed" if proposed else ""),
            "href": "/app/diary?date=" + start.date().isoformat(),
            # Sorts imminent facts among themselves, soonest first.
            "at_ts": start.timestamp(),
        }))
        if coll:
            out.append(coll)
    return out


def event_facts(rows: list[dict], cfg: dict | None = None,
                now: datetime | None = None) -> list[tuple[str, str, dict]]:
    """Notification rows → (contact, contact_name, fact) ready for the deck.

    A row joins the deck only when it says who it is about AND its kind belongs
    to a topic — the morning brief and the operator's platform pushes fail both
    tests, correctly: they are facts about the business, and the business is not
    a card. Rows outside EVENT_WINDOW are dropped here rather than in the query,
    so the window is one decision in one place.

    A kind the tenant has SILENCED is dropped too (Kris 2026-09-04). "Never"
    means never interrupt me and never mention it, and putting the event on the
    front page is mentioning it — the feed row is still written, because the
    feed is the record, but the record is not the same surface as the morning's
    work. This matters more since the toggles moved: a "viewed" that used to
    write no row at all now writes one, and without this it would arrive on
    Today for exactly the people who asked not to hear about it.
    """
    now = now or datetime.now(DIARY_TZ)
    cutoff = now - EVENT_WINDOW
    out: list[tuple[str, str, dict]] = []
    for row in rows:
        contact = (row.get("contact") or "").strip().lower()
        kind = row.get("kind") or ""
        topic = topic_of(kind)
        if not contact or topic is None or silenced(cfg, kind):
            continue
        at = parse_stamp(row.get("created_at"))
        if at is None or at < cutoff:
            continue
        chip, tone = _CHIPS.get(row["kind"], (TOPICS[topic]["label"], "brand"))
        out.append((contact, (row.get("contact_name") or "").strip(), {
            "kind": "event",
            "topic": topic,
            # Unseen events sort above seen ones inside their topic: the point
            # of the deck reading the feed is that you see the new thing first.
            "rank": rank_of(topic, cfg, urgent=not row.get("seen_at")),
            "chip": chip,
            "tone": tone,
            "text": (row.get("body") or row.get("title") or "").strip(),
            "href": row.get("url") or "",
            "at": when_label(at, now),
            "unseen": not row.get("seen_at"),
            # The handle the swipe needs. Only event facts carry one — state
            # facts have nothing to dismiss, they recompute themselves.
            "nid": row.get("id"),
        }))
    return out


# ── Namesakes (Kris 2026-09-20) ────────────────────────────────────────────
# Two cards both headed "Hannah" and no way to know which is which. When two
# people on the deck share a name, each wears the handle of their address —
# the part before the @ — beside it. A name nobody else on the deck has stays
# as it was: "Sarah Vance · sarah.vance" on every card would be noise, and the
# handle is there to tell people apart, not to introduce them. Namesakes whose
# handles ALSO match (hannah@gmail.com, hannah@work.com) show the whole address.

def _name_key(card: dict) -> str:
    return str(card.get("name") or "").strip().lower()


def _handle(card: dict, name_key: str) -> str:
    email = str(card.get("email") or "").strip()
    # A card headed by its own address has nothing to add; a card with no
    # address (an overdue invoice names its customer and nothing else) cannot
    # wear one, and the namesake beside it still does.
    if "@" not in email or email.lower() == name_key:
        return ""
    return email.split("@", 1)[0]


def namesakes(cards: list[dict]) -> list[dict]:
    """Stamp `handle` on every card whose name another card on the deck
    shares. In place, and returned for chaining. Pure."""
    groups: dict[str, list[dict]] = {}
    for c in cards:
        groups.setdefault(_name_key(c), []).append(c)
    for name_key, group in groups.items():
        if not name_key or len(group) < 2:
            continue
        handles = [_handle(c, name_key) for c in group]
        taken = Counter(h.lower() for h in handles if h)
        for c, h in zip(group, handles):
            if not h:
                continue
            c["handle"] = str(c["email"]).strip() if taken[h.lower()] > 1 else h
    return cards
