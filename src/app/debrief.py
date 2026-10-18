"""Morning debrief — the "what does today need from me?" brief.

Composition is deliberately ZERO-LLM (docs/modules/module-morning-debrief.md D3, the
events run-sheet reasoning: a wrong fact at 7am burns trust) — every line is
deterministic arithmetic over reads the dashboard already does. The composer
is split pure-function-style: gather() does the I/O (each section best-effort,
the dashboard-card contract — a read failure skips the section, never the
brief), compose() turns the data dict into title/body/summary with no I/O.

The non-duplication contract with the concierge bubble: the brief never
re-announces individual events (each fired its own push at event time) — only
state AGGREGATES ("3 drafts waiting") and forward-looking facts ("6 bookings
today"). An empty day composes to None and nothing is sent: silence is what
keeps the loud days credible.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app import db, notify_prefs, scheduling
from app import crm as crm_mod

log = logging.getLogger(__name__)

# How far back a held notification may still be mentioned. Long enough to cross
# a Christmas week of closed days, short enough that turning the brief back on
# after a quiet month opens with today rather than a history lesson.
HELD_FLOOR = timedelta(days=14)


def open_today(tenant_id: str, today: date, client=None) -> bool:
    """The closed-day gate (D6): follow the schedule the tenant already
    declared — scheduling_config working_days + blackout_dates, the same
    normalised read the diary uses. No confirmed working days → no schedule →
    every day counts as open (self-silencing still protects quiet days).
    Fail-open: a read error must not silently kill briefs."""
    try:
        cfg = db.get_scheduling_config(tenant_id, client=client)
        if cfg is None:
            return True
        # Days in use count, whether confirmed or carried over awaiting review;
        # only undeclared days (unset, or none saved) leave every day open.
        review = (cfg.get("booking_setting_review") or {}).get("working_days") or {}
        if not cfg.get("working_days") or review.get("state", "set") != "set":
            return True
        c = scheduling._norm(cfg)
        return today.weekday() in c["working_days"] and today not in c["blackouts"]
    except Exception:
        log.warning("debrief open_today read failed (tenant=%s)", tenant_id,
                    exc_info=True)
        return True


def _age_days(created_at: str | None, today: date) -> int:
    try:
        return max(0, (today - datetime.fromisoformat(
            str(created_at).replace("Z", "+00:00")).date()).days)
    except (TypeError, ValueError):
        return 0



def _parsed(stamp) -> datetime | None:
    """An ISO timestamp from the feed into an aware datetime in diary time —
    quiet hours are a wall-clock question, so the comparison has to happen in
    the clock the operator reads. Unparseable means "no idea when", which
    belongs_in_brief treats as "not in quiet hours"."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(scheduling.DIARY_TZ)


def gather(tenant_id: str, today: date, client=None, *,
           replay_since: str | None = None) -> dict:
    """All four sections' raw numbers. Every section is independently
    best-effort; a failed read leaves its key absent and compose() skips it.

    `replay_since` (ISO time, /rig/brief only): "while you were out" retells
    everything since then, including what an earlier brief already said."""
    data: dict = {}
    today_iso = today.isoformat()

    # 1 · Waiting on you — the pending approval queue (the dashboard read).
    try:
        pending = db.list_email_log(tenant_id, status="pending", client=client)
        if pending:
            data["drafts"] = {"count": len(pending),
                              # newest-first list → the tail is the oldest
                              "oldest_days": _age_days(pending[-1].get("created_at"), today)}
    except Exception:
        log.warning("debrief drafts read failed (tenant=%s)", tenant_id, exc_info=True)

    # 1b · After the job (src/app/after_job.py) — deadlines this week / missed.
    # Absent for a business that uses no after-job tasks.
    try:
        from app import after_job
        aj = after_job.brief_counts(tenant_id, today)
        if aj:
            data["after_job"] = aj
    except Exception:
        log.warning("debrief after-job read failed (tenant=%s)", tenant_id, exc_info=True)

    # 2 · Today's diary — bookings are the shared unit across every board.
    try:
        rows = [b for b in db.list_bookings(tenant_id, today_iso, client=client)
                if b.get("booking_date") == today_iso]
        if rows:
            starts = sorted(w for w in (str(b.get("window") or "") for b in rows)
                            if ":" in w)
            data["diary"] = {
                "total": len(rows),
                "confirmed": sum(1 for b in rows if b.get("status") == "confirmed"),
                "first": starts[0] if starts else None,
            }
    except Exception:
        log.warning("debrief diary read failed (tenant=%s)", tenant_id, exc_info=True)

    # 3 · Money — overdue invoices + open quotes, aggregates only (the 08:45
    # overdue job owns the per-invoice milestone pushes; we never repeat them).
    try:
        overdue = [i for i in db.list_invoices(tenant_id, client=client)
                   if i.get("status") in ("sent", "overdue")
                   and str(i.get("due_date") or "")[:10] < today_iso
                   and str(i.get("due_date") or "")]
        if overdue:
            # Sorted by how late: the one she names is the one that has been
            # waiting longest, which is the one you would ask about.
            overdue.sort(key=lambda i: str(i.get("due_date") or ""))
            worst = overdue[0]
            data["overdue"] = {
                "count": len(overdue),
                "total": sum(float(i.get("total") or 0) for i in overdue),
                # A number is a number; a name is a person you can picture
                # owing you money (2026-09-04, the receptionist's brief).
                "who": (worst.get("customer_name")
                        or worst.get("customer_email") or "").strip() or None,
                "ref": (worst.get("invoice_ref") or "").strip() or None}
    except Exception:
        log.warning("debrief invoices read failed (tenant=%s)", tenant_id, exc_info=True)
    try:
        open_quotes = [q for q in db.list_quotes(tenant_id, client=client)
                       if q.get("status") == "sent"]
        if open_quotes:
            data["quotes"] = {
                "count": len(open_quotes),
                "expiring_today": sum(
                    1 for q in open_quotes
                    if str(q.get("valid_until") or "")[:10] == today_iso),
            }
    except Exception:
        log.warning("debrief quotes read failed (tenant=%s)", tenant_id, exc_info=True)

    # 4 · Relationships — CRM-gated, honouring the tenant's own quiet threshold.
    try:
        if "crm" in db.visible_modules(tenant_id, client=client):
            cfg = db.get_tenant_config(tenant_id, client=client) or {}
            quiet_days = int(cfg.get("crm_quiet_days") or crm_mod.STALE_AFTER_DAYS)
            contacts = crm_mod.with_recency(
                db.crm_list_contacts(tenant_id, client=client),
                # Recency judged from the brief's own date, not the wall clock —
                # gather() takes `today` for a reason (and the fixed-date test
                # rotted 14 days after its fixture without this).
                now=datetime(today.year, today.month, today.day, 9,
                             tzinfo=timezone.utc),
                stale_after=quiet_days)
            quiet = [c for c in contacts if c.get("stale")]
            hot = [c for c in contacts
                   if c.get("sentiment") in ("frustrated", "angry")]
            if quiet or hot:
                data["crm"] = {"quiet": len(quiet),
                               "quiet_top": (quiet[0].get("name") or "").strip() or None,
                               "hot": len(hot)}
    except Exception:
        log.warning("debrief crm read failed (tenant=%s)", tenant_id, exc_info=True)

    # 5 · Held for the morning (Kris 2026-09-02) — notifications whose family
    # is set to "in the brief", plus any that landed inside quiet hours. The
    # feed row exists either way; this is the mention they were promised.
    #
    # Read by STAMP, not by clock (Kris 2026-09-04). A rolling 24h window meant
    # a Friday-evening held item died over a closed weekend: the sweep skips
    # closed days before it gathers, and by Monday the window had passed it. A
    # held row now waits for the next brief that actually goes out. HELD_FLOOR
    # is only a guard against resurrection — a brief switched off in August
    # must not come back in September and read the summer out.
    try:
        cfg = db.get_tenant_config(tenant_id, client=client) or {}
        floor = (datetime.now(timezone.utc) - HELD_FLOOR).isoformat()
        if replay_since:
            rows = db.list_unbriefed_notifications(
                tenant_id, not_before=replay_since, include_briefed=True, client=client)
        else:
            rows = db.list_unbriefed_notifications(tenant_id, not_before=floor,
                                                   client=client)
        kept = [r for r in rows
                if notify_prefs.belongs_in_brief(cfg, r.get("kind") or "",
                                                 _parsed(r.get("created_at")))]
        if kept:
            data["held"] = {"count": len(kept),
                            "titles": [r["title"] for r in kept if r.get("title")]}
        # The same rows, unfiltered, for the spoken brief (Kris 2026-09-04).
        # The non-duplication contract is about the PUSH — a lock screen should
        # not repeat what it already buzzed you for an hour ago. Being briefed
        # is the opposite: a receptionist tells you what happened while you were
        # out whether or not your phone mentioned it first, and leaving half of
        # it unsaid is what made the old brief read as a status bar.
        if rows:
            data["since_last"] = [
                {"kind": r.get("kind") or "",
                 "who": (r.get("contact_name") or r.get("contact") or "").strip(),
                 "title": r.get("title") or ""}
                for r in rows]
    except Exception:
        log.warning("debrief: held-notification read failed", exc_info=True)

    # 6 · Only what the owner wants to be told (2026-09-25).
    try:
        on = topics_on(db.get_tenant_config(tenant_id, client=client))
    except Exception:
        on = DEFAULT_TOPICS
    return apply_topics(data, on)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


# Section deep-links for the concierge chat rendering (Phase 2, D5) — the
# follow-up affordances under the injected brief. Keys mirror gather()'s
# section keys; hrefs are the mounted /app routes (dashboard.py).
_SECTION_LINKS = (
    ("drafts", "Review drafts", "/app/"),
    ("overdue", "Overdue invoices", "/app/invoices"),
    ("quotes", "Open quotes", "/app/quotes"),
    ("diary", "Today's diary", "/app/diary"),
    ("crm", "Your contacts", "/app/crm"),
)

# The concierge page detects an injected brief by this prefix (chat_history →
# contextual follow-up chip). The PUSH title starts with it by construction.
CHAT_PREFIX = "Morning brief — "


# ── The receptionist ────────────────────────────────────────────────────────
# Kris 2026-09-04: "You arrive at the office, walking past your receptionist,
# she calls out to you — she would give you a conversational brief of what you
# missed and what's coming up for the day."
#
# The chat turn was the push's bullet list with the same words and a dot in
# front of each one. Every fact was there and none of it was told to you: a
# status bar, not a greeting. It says the same things now, in the order and the
# grammar somebody would actually use out loud — what you missed, what today
# holds, what is waiting on you. (No question at the end since 2026-09-25: she does
# her own work; she tells you what needs you.)
#
# Still ZERO-LLM (D3: a wrong fact at 7am burns trust, and that reasoning does
# not soften because the sentence got warmer). Everything below is arithmetic
# and grammar over the same gathered data.

# She says "Morning" because the brief fires between 05:00 and 12:45, and the
# page recognises its own brief by these — text the composer controls, rather
# than a title it no longer prints.
GREETINGS = ("Morning", "Afternoon", "Evening")   # Evening: a brief sent on demand late (rig 2026-09-26: "Afternoon." at 23:25)

# What each notification kind sounds like when a person says it. One phrasing
# for a single customer, one for several, so a busy night reads "Sarah, Tom and
# June all booked in" rather than the same clause three times.
_SAID: dict[str, tuple[str, str]] = {
    "quote_accepted":            ("accepted their quote", "accepted their quotes"),
    "invoice_paid":              ("paid their invoice", "paid their invoices"),
    "portal_escalation":         ("got in touch through the portal",
                                  "got in touch through the portal"),
    "quote_question":            ("asked about their quote", "asked about their quotes"),
    "portal_booking_created":    ("booked in", "booked in"),
    "portal_reschedule_request": ("asked to move their booking",
                                  "asked to move their bookings"),
    "portal_booking_moved":      ("moved their booking", "moved their bookings"),
    "quote_declined":            ("turned their quote down", "turned their quotes down"),
    "quote_expired":             ("let their quote expire", "let their quotes expire"),
    "invoice_payment_failed":    ("had a payment fail", "had payments fail"),
    "invoice_payment_seen":      ("says they've paid", "say they've paid"),
    "invoice_overdue":           ("went overdue", "went overdue"),
    # Bill the balance on draft (src/app/jobs/raise_balances.py): raised, not sent.
    "invoice_balance_ready":     ("has a balance ready to send",
                                  "have balances ready to send"),
    "quote_keeps_looking":       ("keeps coming back to their quote",
                                  "keep coming back to their quotes"),
    "quote_viewed":              ("opened their quote", "opened their quotes"),
    "invoice_viewed":            ("opened their invoice", "opened their invoices"),
    "quote_drafted":             ("has a quote drafted", "have quotes drafted"),
}

# The order she says them in: the wins, then the people who need you, then what
# moved, then the bad news, then the soft signals. `draft_ready` is deliberately
# absent — "a reply is waiting" is not something that HAPPENED to a customer,
# and the drafts sentence further down already says it, better.
_SAID_ORDER = (
    "quote_accepted", "invoice_paid",
    "portal_escalation", "quote_question",
    "portal_booking_created", "portal_reschedule_request", "portal_booking_moved",
    "quote_declined", "quote_expired", "invoice_payment_failed", "invoice_overdue",
    "invoice_balance_ready", "invoice_payment_seen", "quote_keeps_looking", "quote_viewed",
    "invoice_viewed", "quote_drafted",
)

# Beyond this many kinds of news, she stops listing and points at the page.
_MAX_CLAUSES = 4

_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight",
          "nine", "ten", "eleven", "twelve")


def _count(n: int) -> str:
    """Small numbers as words, because that is how they are said. Money and
    clock times stay in figures — nobody says "nine hundred pounds" to mean
    £900, and "the first at 09:00" is read, not spoken."""
    return _WORDS[n] if 0 <= n < len(_WORDS) else f"{n:,}"


def _join(parts: list[str]) -> str:
    """"A", "A and B", "A, B and C" — the way a list is spoken.

    A serial comma appears only when an item already contains "and", which is
    the one case it earns its keep: "…asked to move their booking and Ali and
    Dee opened their quotes" makes the reader stop and re-parse, and "…booking,
    and Ali and Dee opened their quotes" does not."""
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    tail = ", and " if any(" and " in p for p in parts) else " and "
    return ", ".join(parts[:-1]) + tail + parts[-1]


def _sentence(text: str) -> str:
    """Capitalise the first letter and NOTHING else. str.capitalize() lowercases
    the rest, which turned "Alison Reed has gone quiet" into "Alison reed"."""
    return text[:1].upper() + text[1:] if text else text


def _owns(name: str) -> str:
    """"Ashworth & Son's", but "Beale Homes'" — a name already ending in s
    takes the bare apostrophe."""
    return name + ("'" if name.endswith(("s", "S")) else "'s")


def _people(who: list[str]) -> tuple[list[str], int]:
    """The distinct names, in order, and how many events had no name. Two
    invoices from Sarah are one person, not "Sarah and Sarah"."""
    named: list[str] = []
    for w in who:
        if w and w not in named:
            named.append(w)
    return named, sum(1 for w in who if not w)


def _names(who: list[str]) -> str:
    """Up to three names, then a count. Four names in one clause is a list;
    "Sarah, Tom and two others" is a sentence. Nameless events are counted,
    never "Someone and Someone"."""
    named, nameless = _people(who)
    if not named:
        return "Someone" if nameless <= 1 else f"{_sentence(_count(nameless))} people"
    if len(named) + (1 if nameless else 0) <= 3:
        others = [f"{_count(nameless)} other{'' if nameless == 1 else 's'}"] if nameless else []
        return _join(named + others)
    rest = len(named) - 2 + nameless
    return _join(named[:2] + [f"{_count(rest)} others"])


def _while_you_were_out(events: list[dict]) -> str:
    """What happened since the last brief, grouped by what it was.

    Grouping is what keeps this a sentence: three separate acceptances read as
    one clause with three names, not as three clauses with the same verb."""
    by_kind: dict[str, list[str]] = {}
    for e in events:
        if e.get("kind") in _SAID:
            by_kind.setdefault(e["kind"], []).append(e.get("who") or "")
    if not by_kind:
        return ""
    clauses = []
    for kind in _SAID_ORDER:
        who = by_kind.get(kind)
        if not who:
            continue
        singular, plural = _SAID[kind]
        named, nameless = _people(who)
        one = len(named) + nameless == 1
        clauses.append(f"{_names(who)} {singular if one else plural}")
    tail = ""
    if len(clauses) > _MAX_CLAUSES:
        # She would not read out sixteen things. The deck has them all.
        tail = ", and a few other bits — they're all on Today"
        clauses = clauses[:_MAX_CLAUSES]
    return f"While you were out, {_join(clauses)}{tail}."


def _today_holds(data: dict) -> str:
    dy = data.get("diary")
    if not dy:
        return ""
    bits = f"You've {_count(dy['total'])} booking{'' if dy['total'] == 1 else 's'} in today"
    if dy.get("first"):
        bits += f", the first at {dy['first']}"
    bits += "."
    to_confirm = dy["total"] - dy["confirmed"]
    if to_confirm:
        bits += (f" {_sentence(_count(to_confirm))} still "
                 f"need{'s' if to_confirm == 1 else ''} confirming.")
    return bits


def _waiting_on_you(data: dict) -> str:
    """The sentences that end in something for you to do."""
    out = []
    d = data.get("drafts")
    if d:
        n = d["count"]
        line = ("There's a draft waiting on you" if n == 1
                else f"{_sentence(_count(n))} drafts are waiting on you")
        age = d.get("oldest_days") or 0
        if age == 1:
            line += " — the oldest since yesterday"
        elif age > 1:
            line += f" — the oldest's been there {_count(age)} days"
        out.append(line + ".")

    a = data.get("after_job")
    if a:
        bits = []
        if a.get("overdue"):
            bits.append(f"{_count(a['overdue'])} past its deadline" if a["overdue"] == 1
                        else f"{_count(a['overdue'])} past their deadlines")
        if a.get("week"):
            bits.append(f"{_count(a['week'])} due this week")
        n = a.get("overdue", 0) + a.get("week", 0)
        out.append(f"{'One after-job task' if n == 1 else _sentence(_count(n)) + ' after-job tasks'}: "
                   + ", ".join(bits) + ".")

    o = data.get("overdue")
    if o:
        n = o["count"]
        if n == 1:
            who = f" from {o['who']}" if o.get("who") else ""
            out.append(f"One invoice is overdue{who} — £{o['total']:,.0f}.")
        else:
            first = f" The oldest is {_owns(o['who'])}." if o.get("who") else ""
            out.append(f"{_sentence(_count(n))} invoices are overdue, "
                       f"£{o['total']:,.0f} between them.{first}")

    q = data.get("quotes")
    if q:
        n = q["count"]
        line = ("One quote is still waiting on a decision" if n == 1
                else f"{_sentence(_count(n))} quotes are still waiting on a decision")
        if q.get("expiring_today"):
            k = q["expiring_today"]
            line += (" — and it expires today" if k == 1 and n == 1
                     else f" — {_count(k)} of them "
                          f"{'expires' if k == 1 else 'expire'} today")
        out.append(line + ".")

    c = data.get("crm")
    if c:
        bits = []
        if c.get("quiet"):
            bits.append(f"{c['quiet_top']} has gone quiet"
                        if c["quiet"] == 1 and c.get("quiet_top")
                        else f"{_count(c['quiet'])} contacts have gone quiet")
        if c.get("hot"):
            bits.append("one's reading unhappy" if c["hot"] == 1
                        else f"{_count(c['hot'])} are reading unhappy")
        out.append(_sentence(_join(bits)) + ".")
    return " ".join(out)


# ── What she tells you (Kris 2026-09-25) ────────────────────────────────────
# "In a real scenario your receptionist wouldn't offer to do whatever she
# normally does, because she does it anyway. She'd tell you the important
# things she can't do alone, or things that are generally important." So the
# brief no longer ends on an offer, and what it covers is the owner's choice:
# a topic list, set from the cog on the brief's bubble or in Settings ›
# Notifications (tenant_config.debrief_topics; NULL = these defaults).
# (key, label, what it covers, on by default)
TOPICS = (
    ("approvals", "Waiting on you", "Replies I've drafted that need your yes", True),
    ("diary", "Today's diary", "Who's booked in today, and what still needs confirming", True),
    ("money", "Money", "Overdue invoices and payments that failed", True),
    ("customers", "Customers who need you",
     "Portal messages, questions about quotes, requests to move, and anyone unhappy or gone quiet", True),
    ("wins", "Good news", "Paid, accepted and booked in", True),
    ("quotes", "Quotes", "Still waiting on a decision, turned down or expired", True),
    ("prepared", "What I've prepared", "Quotes I drafted and balances I raised — I do these anyway", False),
    ("signals", "Small signals", "Someone opened a quote or invoice, or keeps looking", False),
)
TOPIC_KEYS = tuple(k for k, *_ in TOPICS)
DEFAULT_TOPICS = frozenset(k for k, _l, _h, on in TOPICS if on)

# Which topic each overnight event belongs to. A kind not listed is never said.
_KIND_TOPIC = {
    "quote_accepted": "wins", "invoice_paid": "wins", "portal_booking_created": "wins",
    "portal_booking_moved": "wins", "invoice_payment_seen": "wins",
    "portal_escalation": "customers", "quote_question": "customers",
    "portal_reschedule_request": "customers",
    "invoice_overdue": "money", "invoice_payment_failed": "money",
    "quote_declined": "quotes", "quote_expired": "quotes",
    "quote_drafted": "prepared", "invoice_balance_ready": "prepared",
    "quote_keeps_looking": "signals", "quote_viewed": "signals", "invoice_viewed": "signals",
}
# Which topic each gathered section belongs to. 'held' (notifications the owner
# chose to hear about in the brief) is always kept: that choice was made per
# family in Settings, and dropping it here would make "In the brief" a lie.
_SECTION_TOPIC = {"drafts": "approvals", "diary": "diary", "overdue": "money",
                  "quotes": "quotes", "crm": "customers"}


def topics_on(cfg: dict | None) -> frozenset:
    """The topics this tenant's brief covers. A saved list is used as it is
    (an empty list means they turned everything off); anything unreadable
    falls back to the defaults."""
    stored = (cfg or {}).get("debrief_topics")
    if isinstance(stored, list):
        return frozenset(k for k in stored if k in TOPIC_KEYS)
    return DEFAULT_TOPICS


def apply_topics(data: dict, on) -> dict:
    """The gathered data with only the chosen topics left. Pure."""
    out = {k: v for k, v in data.items()
           if k not in _SECTION_TOPIC or _SECTION_TOPIC[k] in on}
    if "since_last" in out:
        kept = [e for e in out["since_last"] if _KIND_TOPIC.get(e.get("kind")) in on]
        if kept:
            out["since_last"] = kept
        else:
            out.pop("since_last")
    return out


def speak(data: dict, at: datetime | None = None) -> str:
    """The whole greeting, as paragraphs. Pure, and zero-LLM like everything
    else here: this is grammar over the same numbers the bullets used."""
    hour = (at or datetime.now(scheduling.DIARY_TZ)).hour
    greeting = GREETINGS[2] if hour >= 18 else GREETINGS[1] if hour >= 12 else GREETINGS[0]
    paragraphs = [
        greeting + ".",
        _while_you_were_out(data.get("since_last") or []),
        _today_holds(data),
        _waiting_on_you(data),
    ]
    return "\n\n".join(p for p in paragraphs if p)


def is_brief(content: str | None) -> bool:
    """Is this assistant turn one of ours? Asked by the concierge page (the
    cog on the brief's bubble) and by Today's brief strip.

    Recognised by the greeting rather than by a title, because she no longer
    announces herself with one — a receptionist says "Morning", not "Morning
    brief, Friday the 4th of September"."""
    text = (content or "").lstrip()
    return any(text.startswith(g + ".") for g in GREETINGS)


def compose(data: dict, today: date, at: datetime | None = None) -> dict | None:
    """The pure half: data dict → {title, body, summary, chat, links}, or None
    when there is nothing to say (→ no push, no feed row, no chat turn).

    Two voices out of one set of facts. `body` and `summary` stay a terse list
    in salience order (waiting-on-you > money > diary > relationships) — that is
    a lock screen and a log line, and neither wants sentences. `chat` is the
    spoken greeting (speak(), 2026-09-04). `at` is when the brief is actually
    going out, which is all the greeting needs it for."""
    lines: list[str] = []

    d = data.get("drafts")
    if d:
        oldest = f" (oldest {d['oldest_days']}d)" if d.get("oldest_days") else ""
        lines.append(f"{_plural(d['count'], 'draft')} waiting{oldest}")

    a = data.get("after_job")
    if a:
        lines.append(" · ".join(x for x in (
            f"{a['overdue']} after-job overdue" if a.get("overdue") else "",
            f"{a['week']} after-job due this week" if a.get("week") else "") if x))
    o = data.get("overdue")
    if o:
        lines.append(f"{_plural(o['count'], 'invoice')} overdue (£{o['total']:,.0f})")
    q = data.get("quotes")
    if q:
        expiring = f", {q['expiring_today']} expiring today" if q.get("expiring_today") else ""
        lines.append(f"{_plural(q['count'], 'quote')} awaiting a decision{expiring}")

    dy = data.get("diary")
    if dy:
        to_confirm = dy["total"] - dy["confirmed"]
        bits = f"{_plural(dy['total'], 'booking')} today"
        if dy.get("first"):
            bits += f", first at {dy['first']}"
        if to_confirm:
            bits += f" ({to_confirm} to confirm)"
        lines.append(bits)

    c = data.get("crm")
    if c:
        bits = []
        if c.get("quiet"):
            top = f" ({c['quiet_top']})" if c.get("quiet_top") and c["quiet"] == 1 else ""
            bits.append(f"{c['quiet']} gone quiet{top}")
        if c.get("hot"):
            bits.append(f"{c['hot']} running hot")
        lines.append(" · ".join(bits))

    h = data.get("held")
    if h:
        # What you were not interrupted for. Named, because "3 notifications"
        # tells you nothing you can act on — the point of holding them was that
        # they could wait, not that they did not matter.
        shown = " · ".join(h["titles"][:3])
        more = f" (+{h['count'] - len(h['titles'][:3])} more)" if h["count"] > 3 else ""
        lines.append(f"While you were away: {shown}{more}")

    if not lines:
        return None

    title = f"{CHAT_PREFIX}{today.strftime('%a %d %b').replace(' 0', ' ')}"
    return {
        "title": title,
        # add_concierge_notification caps body at 500; three lines stay well
        # under it and read cleanly on a lock screen.
        "body": " · ".join(lines[:3]),
        "summary": f"{title}: " + "; ".join(lines),
        # The concierge chat turn (D5) — spoken, since 2026-09-04. The push
        # above stays terse and non-duplicating because it competes with a lock
        # screen; this is somebody telling you what you missed as you walk in,
        # so it retells the night's events whether or not your phone did.
        "chat": speak(data, at=at),
        "links": [{"label": label, "href": href}
                  for key, label, href in _SECTION_LINKS if key in data],
    }
