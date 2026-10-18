"""Today's deck is fed by the notification feed.

Kris, 2026-09-04: "the current Today page, it's technically a glorified
CRM/Inbox. We should use it more like a mobile notification system."

The deck was pure STATE — drafts waiting, gone quiet, overdue — recomputed on
every render, with no time on anything and no record of what had HAPPENED. The
feed had all of that and only the concierge bubble ever showed it.

Pins the four things that make the join safe:

  * a notification only joins a card when it says WHO it is about and its kind
    has a topic — the morning brief and the operator's own platform pushes fail
    both tests and stay off the deck, because they are facts about the business
    and the business is not a person;
  * ranking is topics, not kinds, and the tenant's placement moves a whole
    topic — including past facts that were previously hardcoded ints;
  * the feed is a source like any other: a read that fails costs Today its
    event facts, never the page;
  * dismissing is not the same as seeing — seen kills the glow, dismissed takes
    the row off Today, and the two live in different columns.

Run: python -m pytest tests/test_deck_notifications.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db, deck
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.scheduling import DIARY_TZ
from tests.test_home_dashboard import Env

NOW = datetime(2026, 9, 4, 10, 30, tzinfo=DIARY_TZ)


def note(kind="quote_accepted", *, contact="sarah@acme.co.uk", name="Sarah Vance",
         minutes_ago=48, seen=False, body="QUO-1012 · £2,480", nid="n1", now=None):
    at = ((now or NOW) - timedelta(minutes=minutes_ago)).astimezone(timezone.utc)
    return {"id": nid, "kind": kind, "title": "Quote QUO-1012 accepted",
            "body": body, "url": "/app/quotes/q1",
            "created_at": at.isoformat(),
            "seen_at": None if seen else None if not seen else at.isoformat(),
            "contact": contact, "contact_name": name}


# ── Who a notification is about ────────────────────────────────────────────
def test_a_notification_with_nobody_attached_joins_no_card():
    """The morning brief is about the business. There is no card for that, and
    inventing one would put "your company" in a deck of your customers."""
    rows = [note("morning_debrief", contact=None, name=None),
            note("quote_accepted")]
    got = deck.event_facts(rows, {}, NOW)
    assert [c for c, _, _ in got] == ["sarah@acme.co.uk"]


def test_a_kind_with_no_topic_joins_no_card():
    """Platform plumbing (a support ticket, a sign-up) reaches the operator's
    own tenant. It is not that tenant's customer relationship."""
    assert deck.event_facts([note("support_ticket")], {}, NOW) == []
    assert deck.topic_of("morning_debrief") is None
    assert deck.topic_of("operator_signup") is None


def test_every_kind_a_family_governs_has_a_deck_topic():
    """The guard: a new notification kind that reaches a person must be able to
    land on that person's card, or Today silently stops mentioning it.

    The Problems family is exempt, and is the only exemption: a dead mailbox is
    about the product, not about a customer, so there is no card for it. It has
    its own surface — the rose strip above the deck (src/app/health.py)."""
    from app import notify_prefs
    governed = {k for col, fam in notify_prefs.FAMILIES.items()
                for k in fam["kinds"] if col != "notify_problems"}
    homeless = {k for k in governed if deck.topic_of(k) is None}
    assert not homeless, "governed kinds with no deck topic: %s" % sorted(homeless)
    # …and the exemption is real, not an oversight waiting to be "fixed".
    assert all(deck.topic_of(k) is None
               for k in notify_prefs.FAMILIES["notify_problems"]["kinds"])


def test_every_call_site_that_can_name_a_person_does():
    """The other half of the guard, on the WRITE side: a notify() whose kind has
    a deck topic must pass contact=, or the row reaches the feed with nobody
    attached and quietly never appears on Today. Static scan, because the bug
    is invisible at runtime — the notification still arrives, it just stops
    showing up in the one place the operator looks each morning."""
    import glob
    import re

    missing = []
    for path in glob.glob("src/app/**/*.py", recursive=True):
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"push\.notify\s*\(", src):
            seg, depth, end = src[m.end():m.end() + 1200], 1, 0
            for i, ch in enumerate(seg):
                depth += (ch == "(") - (ch == ")")
                if depth == 0:
                    end = i
                    break
            call = seg[:end]
            k = re.search(r'kind=["\']([a-z_]+)["\']', call)
            if k and deck.topic_of(k.group(1)) and "contact=" not in call:
                missing.append("%s: %s" % (path, k.group(1)))
    assert not missing, "notifications that name nobody: %s" % missing


# ── What the tenant asked not to hear about ────────────────────────────────
# The other half of moving the two `viewed` opt-ins off their call sites (Kris
# 2026-09-04). Those now always write a feed row — so without this, a "viewed"
# would arrive on Today for exactly the people who switched it off, which is
# worse than the bug it replaced.
def test_a_view_they_switched_off_never_reaches_the_deck():
    off = {"notify_quote_viewed": False}
    assert deck.event_facts([note("quote_viewed")], off, NOW) == []
    # …and is still there for everyone who did not switch it off.
    assert len(deck.event_facts([note("quote_viewed")], {}, NOW)) == 1


def test_a_family_set_to_never_never_reaches_the_deck():
    """"Never" means never interrupt me and never mention it — and putting the
    event on the page they open every morning is mentioning it."""
    off = {"notify_quotes": "never"}
    assert deck.event_facts([note("quote_accepted")], off, NOW) == []
    assert len(deck.event_facts([note("invoice_paid", contact="a@x.co")],
                                off, NOW)) == 1, "only its own family is silenced"


def test_in_the_brief_is_not_a_request_to_be_spared():
    """Held for the morning means "tell me later", and Today is where later
    happens — so a brief-tier family still stacks on its person's card."""
    held = {"notify_quotes": "brief"}
    assert len(deck.event_facts([note("quote_accepted")], held, NOW)) == 1


def test_the_feed_row_still_exists_even_when_the_deck_will_not_show_it():
    """The record and the surface are different things: db.list_deck_notifications
    hands over everything live, and the filtering happens here."""
    rows = [note("quote_viewed", nid="hidden"), note("quote_accepted", nid="shown")]
    assert [f["nid"] for _, _, f in
            deck.event_facts(rows, {"notify_quote_viewed": False}, NOW)] == ["shown"]
    assert len(rows) == 2, "nothing was removed from the feed itself"


def test_the_contact_key_is_lower_cased_the_way_the_deck_keys_people():
    got = deck.event_facts([note(contact="Sarah@Acme.co.uk")], {}, NOW)
    assert got[0][0] == "sarah@acme.co.uk"


# ── The event window ───────────────────────────────────────────────────────
def test_last_weeks_news_is_history_not_today():
    fresh = note(minutes_ago=60, nid="fresh")
    stale = note(minutes_ago=60 * 24 * 8, nid="stale")
    got = deck.event_facts([fresh, stale], {}, NOW)
    assert [f["nid"] for _, _, f in got] == ["fresh"]


def test_an_unreadable_timestamp_is_dropped_rather_than_guessed():
    bad = dict(note(), created_at="whenever")
    assert deck.event_facts([bad], {}, NOW) == []


# ── How it reads on the card ───────────────────────────────────────────────
def test_the_chip_says_the_event_not_the_push_title():
    """The push title is written for a lock screen and repeats the customer's
    name; on their own card that is the one word already at the top."""
    _, _, fact = deck.event_facts([note("quote_accepted")], {}, NOW)[0]
    assert fact["chip"] == "Quote accepted"
    assert fact["text"] == "QUO-1012 · £2,480"
    assert fact["href"] == "/app/quotes/q1"
    assert fact["nid"] == "n1"          # the handle the swipe needs


def test_a_state_fact_has_no_time_and_no_swipe():
    """Nothing HAPPENED — an overdue invoice simply is, and recomputes itself.
    A dismiss on it would be a lie the next render undid."""
    Env  # (state facts are built in dashboard.py; asserted at the page below)
    _, _, fact = deck.event_facts([note()], {}, NOW)[0]
    assert fact["at"] and fact["nid"], "an event carries both"


@pytest.mark.parametrize("minutes, reads", [
    (30, "10:00"), (60 * 20, "Yst"), (60 * 24 * 3, "Tue"), (60 * 24 * 9, "26 Aug")])
def test_the_clock_reads_like_a_phone(minutes, reads):
    assert deck.when_label((NOW - timedelta(minutes=minutes)), NOW) == reads


def test_an_unparseable_stamp_reads_blank_rather_than_lying():
    assert deck.when_label("half past something") == ""
    assert deck.when_label(None) == ""


# ── Ranking: topics, and the tenant's order ────────────────────────────────
def test_the_default_order_puts_outstanding_promises_last_but_one():
    """Corrected 2026-09-04. The move to topics had collapsed "promise with a
    due date" (originally rank 1) and "promise with none" (originally rank 6)
    into one topic at the top, so "They owe: <description>" led every card it
    appeared on. Undated promises sit low again; a DATED one floats instead."""
    assert [t for t, _ in deck.ordered_topics({})] == [
        "unhappy", "questions", "drafts", "bookings",
        "money", "quotes", "records", "promises", "quiet"]


def test_a_topic_sent_first_outranks_every_normal_topic():
    cfg = {"deck_priority": {"money": "first"}}
    assert [t for t, _ in deck.ordered_topics(cfg)][0] == "money"
    assert deck.rank_of("money", cfg) < deck.rank_of("promises", cfg)


def test_a_topic_sent_last_falls_below_every_normal_topic():
    cfg = {"deck_priority": {"promises": "last"}}
    assert [t for t, _ in deck.ordered_topics(cfg)][-1] == "promises"
    assert deck.rank_of("promises", cfg) > deck.rank_of("quiet", cfg)


def test_two_topics_sent_first_keep_the_default_order_between_them():
    cfg = {"deck_priority": {"quiet": "first", "money": "first"}}
    assert [t for t, _ in deck.ordered_topics(cfg)][:2] == ["money", "quiet"]


@pytest.mark.parametrize("stored", [None, {}, "nonsense", {"money": "urgent"},
                                    {"nope": "first"}, []])
def test_a_broken_setting_leaves_the_deck_in_its_default_order(stored):
    """A deck that empties or inverts itself over a bad config value is worse
    than one that ignores it."""
    cfg = {"deck_priority": stored}
    assert [t for t, _ in deck.ordered_topics(cfg)] == \
        [t for t, _ in deck.ordered_topics({})]
    assert deck.placements(cfg)["money"] == deck.NORMAL


def test_urgency_lifts_a_fact_inside_its_topic_never_across_it():
    """A promise with a date beats one without. It does not beat a topic the
    operator deliberately put above promises."""
    cfg = {"deck_priority": {"money": "first"}}
    assert deck.rank_of("promises", cfg, urgent=True) < deck.rank_of("promises", cfg)
    assert deck.rank_of("promises", cfg, urgent=True) > deck.rank_of("money", cfg)


def test_an_unread_event_sorts_above_a_read_one_in_the_same_topic():
    unread = note(nid="new", minutes_ago=200)
    read = dict(note(nid="old", minutes_ago=10),
                seen_at=NOW.astimezone(timezone.utc).isoformat())
    facts = sorted((f for _, _, f in deck.event_facts([read, unread], {}, NOW)),
                   key=lambda f: f["rank"])
    assert [f["nid"] for f in facts] == ["new", "old"]


# ── The diary on a card ────────────────────────────────────────────────────
# Kris 2026-09-04: "there's an appointment booked for less than 24hrs away that
# could be shown instead of what's owed each way". Three faults, all of which
# hid the one time-critical thing on the page.
BNOW = datetime(2026, 9, 4, 11, 0, tzinfo=DIARY_TZ)


def bk(day=0, window="09:00", status="confirmed", summary="Patio survey",
       contact="sarah@acme.co.uk"):
    return {"id": "b1", "contact": contact, "window": window, "status": status,
            "summary": summary,
            "booking_date": (BNOW + timedelta(days=day)).date().isoformat()}


def test_tomorrow_morning_reaches_the_card_and_says_so():
    """It never used to: dashboard.py narrowed the diary to TODAY for the board
    count and the deck read the same narrowed list, so an appointment tomorrow
    was not on the page at all — and everything that was got chipped
    "Booked today", including next month's."""
    got = deck.booking_facts([bk(day=1)], {}, BNOW)
    assert [f["chip"] for _, f in got] == ["Tomorrow 09:00"]
    assert got[0][1]["text"] == "Patio survey"
    assert got[0][1]["href"] == "/app/diary?date=2026-09-05"


@pytest.mark.parametrize("window, at, reads", [
    ("11:40", BNOW, "In 40 minutes"),
    ("11:01", BNOW, "Now"),
    ("16:00", BNOW, "Today 16:00"),
    ("am", BNOW, "Today"),
])
def test_todays_bookings_are_read_off_the_clock(window, at, reads):
    got = deck.booking_facts([bk(window=window)], {}, at)
    assert got[0][1]["chip"] == reads


def test_a_booking_that_has_already_happened_is_not_whats_next():
    """The deck answers "who needs you next". This morning's finished job does
    not, and the dockbar strip still shows the whole day slot by slot."""
    assert deck.booking_facts([bk(window="09:00")], {}, BNOW) == []


def test_a_window_with_no_clock_survives_the_whole_day():
    """Half the diaries in the platform book "am"/"pm" rather than 09:00, and
    those must not read as finished at one minute past midnight."""
    assert len(deck.booking_facts([bk(window="pm")], {}, BNOW)) == 1


def test_the_far_diary_is_not_todays_problem():
    assert deck.booking_facts([bk(day=2)], {}, BNOW)          # inside the horizon
    assert deck.booking_facts([bk(day=30)], {}, BNOW) == []


def test_a_hold_nobody_confirmed_says_so():
    _, fact = deck.booking_facts([bk(day=1, status="proposed")], {}, BNOW)[0]
    assert fact["tone"] == "amber" and fact["text"].endswith("· not confirmed")


def test_a_booking_with_nobody_attached_joins_no_card():
    assert deck.booking_facts([bk(day=1, contact="")], {}, BNOW) == []


def test_an_unreadable_booking_date_is_dropped_rather_than_guessed():
    assert deck.booking_facts([dict(bk(), booking_date="soon")], {}, BNOW) == []


# ── Imminence: what happens next beats what is outstanding ─────────────────
def test_an_appointment_soon_outranks_everything_outstanding():
    """The actual complaint. An undated promise sat at the top of the card
    while an appointment inside 24 hours waited below it."""
    soon = deck.rank_of("bookings", {}, imminent=True)
    for topic in ("promises", "money", "quotes", "quiet", "drafts", "questions"):
        assert soon < deck.rank_of(topic, {}), topic


def test_a_booking_further_out_stays_in_its_topic():
    assert deck.rank_of("bookings", {}) > deck.rank_of("unhappy", {})
    assert deck.rank_of("bookings", {}, imminent=True) < deck.rank_of("unhappy", {})


def test_the_clock_does_not_overrule_a_stated_preference():
    """A tenant who put money FIRST meets money first, appointment or no
    appointment — imminence reorders the middle, it does not win an argument
    the operator has already settled."""
    cfg = {"deck_priority": {"money": "first"}}
    assert deck.rank_of("money", cfg) < deck.rank_of("bookings", cfg, imminent=True)


def test_a_promise_with_a_date_floats_and_one_without_waits():
    dated = deck.rank_of("promises", {}, urgent=True, imminent=True)
    undated = deck.rank_of("promises", {})
    assert dated < deck.rank_of("bookings", {}) < undated, \
        "the original deck's rank 1 vs rank 6 split, restored"


# ── On the page ────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def live(**kw):
    """A feed row stamped against the REAL clock. The pure-model tests below
    hand event_facts an explicit `now`, so a fixed date is honest there — but
    the page renders on today's date and deck.EVENT_WINDOW is 7 days, so a
    fixture pinned to a date in the past silently stops being a card at all.
    Every test that goes through the client uses this."""
    return note(now=datetime.now(DIARY_TZ), **kw)


def _feed(monkeypatch, rows):
    monkeypatch.setattr(dashboard.db, "list_deck_notifications",
                        lambda *a, **k: list(rows))
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})


def test_an_event_lands_on_the_persons_card(client, monkeypatch):
    Env(monkeypatch)
    _feed(monkeypatch, [live()])
    html = client.get("/app/").text
    assert "Sarah Vance" in html
    assert "Quote accepted" in html
    assert "QUO-1012 · £2,480" in html
    assert 'data-dismiss="n1"' in html          # the swipe is on the row


def test_an_event_joins_the_card_the_inbox_already_built(client, monkeypatch):
    """CONVOS puts a@x.com in the deck with a draft waiting. A notification for
    the same address must land on that card, not mint a second one."""
    Env(monkeypatch)
    _feed(monkeypatch, [live(contact="A@x.com", name="Sarah")])
    html = client.get("/app/").text
    # CONVOS holds two pending contacts, so two cards — not three.
    assert html.count("<article") == 2
    assert html.count('data-dismiss="n1"') == 1
    # …and the one card that carries the event carries the draft too.
    card = next(c for c in html.split("<article") if 'data-dismiss="n1"' in c)
    assert "Draft ready" in card and "Quote accepted" in card


@pytest.mark.parametrize("day", ["2026-09-28", "2026-09-27", "2026-09-30"])
@pytest.mark.parametrize("hour", [8, 9, 11])
def test_the_appointment_leads_the_card_not_what_is_owed(client, monkeypatch, day, hour):
    """End to end, as reported: an appointment inside 24 hours must be the first
    thing on the person's card, above an open promise with no date on it."""
    from app.scheduling import DIARY_TZ as _TZ

    now = datetime.fromisoformat(day).replace(hour=hour, tzinfo=_TZ)

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(dashboard, "datetime", ClockDatetime)
    monkeypatch.setattr(deck, "datetime", ClockDatetime)
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    Env(monkeypatch, modules={"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [
        {"id": "b1", "contact": "a@x.com", "booking_date": tomorrow,
         "window": "09:00", "status": "confirmed", "summary": "Patio survey"}])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [
        {"id": "c1", "name": "Sarah", "email": "a@x.com"}])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [
        {"contact_id": "c1", "direction": "we_owe",
         "description": "Send the slab measurements"}])
    _feed(monkeypatch, [])

    cards = client.get("/app/").text.split("<article")[1:]
    card = next(c for c in cards if "Tomorrow 09:00" in c)
    # Tomorrow's appointment leads at any hour: imminence counts today's and
    # tomorrow's diary dates, not only a rolling 24 hours (is_imminent).
    assert card == cards[0]
    assert "Tomorrow 09:00" in card and "You owe" in card
    assert card.index("Tomorrow 09:00") < card.index("You owe")


def test_the_appointment_still_leads_just_after_midnight(client, monkeypatch):
    """The test above failed every night from 00:00 to 09:00 BST: tomorrow's
    09:00 was more than 24 hours off, so it stopped counting as imminent while
    the chip still read "Tomorrow 09:00" — and another person's draft took the
    top of the deck. The clock is frozen at 00:30 BST so the rule is proved
    whatever time the suite runs."""
    frozen_at = datetime(2026, 9, 27, 0, 30, tzinfo=DIARY_TZ)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_at.astimezone(tz) if tz else frozen_at.replace(tzinfo=None)

    monkeypatch.setattr(deck, "datetime", _Frozen)
    monkeypatch.setattr(dashboard, "datetime", _Frozen)
    Env(monkeypatch, modules={"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [
        {"id": "b1", "contact": "a@x.com", "booking_date": "2026-09-28",
         "window": "09:00", "status": "confirmed", "summary": "Patio survey"}])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [
        {"id": "c1", "name": "Sarah", "email": "a@x.com"}])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [
        {"contact_id": "c1", "direction": "we_owe",
         "description": "Send the slab measurements"}])
    _feed(monkeypatch, [])

    card = client.get("/app/").text.split("<article")[1]
    assert "Tomorrow 09:00" in card and "You owe" in card
    assert card.index("Tomorrow 09:00") < card.index("You owe")


def test_imminent_goes_by_the_diary_calendar_not_a_rolling_day():
    """Today and tomorrow in the diary's calendar are imminent at any hour;
    the day after is not, even late at night."""
    small_hours = datetime(2026, 9, 27, 0, 30, tzinfo=DIARY_TZ)
    late = datetime(2026, 9, 27, 23, 30, tzinfo=DIARY_TZ)
    tomorrow_9 = datetime(2026, 9, 28, 9, 0, tzinfo=DIARY_TZ)
    day_after_9 = datetime(2026, 9, 29, 9, 0, tzinfo=DIARY_TZ)
    assert deck.is_imminent(tomorrow_9, small_hours)
    assert not deck.is_imminent(day_after_9, small_hours)
    assert not deck.is_imminent(day_after_9, late)       # 33.5h: not tomorrow either
    got = deck.booking_facts([{"contact": "a@x.com", "booking_date": "2026-09-28",
                               "window": "09:00", "status": "confirmed"}], {}, small_hours)
    assert got[0][1]["rank"] == deck.rank_of("bookings", {}, imminent=True)


def test_the_more_button_can_actually_be_opened(client, monkeypatch):
    """"+6 of something i can't expand" — the count was a dead label. The extras
    ship RENDERED and hidden, so the worst a broken script can do is leave a
    longer card, never an unreachable one."""
    Env(monkeypatch)
    _feed(monkeypatch, [live(kind=k, contact="a@x.com", name="Sarah", nid=k,
                             minutes_ago=60 + i)
                        for i, k in enumerate(
                            ("quote_accepted", "invoice_paid", "quote_declined",
                             "invoice_overdue", "portal_booking_moved"))])
    html = client.get("/app/").text
    assert "data-more" in html and "+2 more" in html
    # every fact is on the page, not just the visible four — counted on the
    # list items, since the click handler mentions [data-extra] as well
    import re as _re
    assert len(_re.findall(r"<li[^>]*data-extra", html)) == 2
    assert "Quote declined" in html and "Overdue" in html


def test_a_feed_that_will_not_read_costs_the_events_never_the_page(client, monkeypatch):
    Env(monkeypatch)
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", _boom)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    r = client.get("/app/")
    assert r.status_code == 200
    assert "Who needs you next" in r.text


def test_no_unread_dot_beside_the_name(client, monkeypatch):
    """Kris 2026-09-27: the blue dot beside the customer's name is gone; the
    unread event still reaches the card as its own line."""
    Env(monkeypatch)
    _feed(monkeypatch, [live(nid="n1")])
    html = client.get("/app/").text
    assert "Quote accepted" in html
    assert "New since you last looked" not in html


def test_a_card_of_only_read_events_does_not_claim_to_be_new(client, monkeypatch):
    Env(monkeypatch)
    # Was passing for the wrong reason once the fixture aged out — the row has
    # to actually REACH the card for "no dot" to mean anything.
    seen = dict(live(), seen_at=datetime.now(timezone.utc).isoformat())
    _feed(monkeypatch, [seen])
    html = client.get("/app/").text
    assert "Quote accepted" in html
    assert "New since you last looked" not in html


def test_the_operators_order_reaches_the_page(client, monkeypatch):
    """CONVOS gives a@x.com a draft; the feed gives them an overdue invoice.
    Drafts outrank money by default, so the card leads with the draft — until
    the tenant says money comes first."""
    Env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [
        live(kind="invoice_overdue", contact="a@x.com", name="Sarah",
             body="INV-1044 · £900 · 12d late")])

    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    card = next(c for c in client.get("/app/").text.split("<article")
                if "Overdue" in c)
    assert card.index("Draft ready") < card.index("Overdue")

    monkeypatch.setattr(dashboard.db, "get_tenant_config",
                        lambda *a, **k: {"deck_priority": {"money": "first"}})
    card = next(c for c in client.get("/app/").text.split("<article")
                if "Overdue" in c)
    assert card.index("Overdue") < card.index("Draft ready")


# ── The settings row ───────────────────────────────────────────────────────
@pytest.fixture
def settings_post(monkeypatch):
    from app.security import require_csrf
    saved = []
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: saved.append(cfg))
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), saved
    app.dependency_overrides.clear()


def test_saving_the_order_stores_only_what_was_moved(settings_post):
    """A tenant on the default order stores {} — so the default can change
    later without silently overriding people who never expressed a view."""
    c, saved = settings_post
    r = c.post("/app/settings/deck",
               data={"deck_money": "first", "deck_quiet": "last",
                     "deck_drafts": "normal", "deck_promises": "normal"},
               follow_redirects=False)
    assert r.status_code == 303
    assert saved == [{"deck_priority": {"money": "first", "quiet": "last"}}]


def test_a_junk_placement_is_dropped_not_stored(settings_post):
    c, saved = settings_post
    c.post("/app/settings/deck", data={"deck_money": "URGENT!!"},
           follow_redirects=False)
    assert saved == [{"deck_priority": {}}]


def test_the_order_form_cannot_touch_a_notification_setting(settings_post):
    """Its own form and its own handler — the trap the debrief form guards with
    notify_form, avoided here by the route never writing those columns."""
    c, saved = settings_post
    c.post("/app/settings/deck", data={"deck_money": "first"},
           follow_redirects=False)
    assert list(saved[0]) == ["deck_priority"]


def test_the_face_names_the_card_you_meet_first():
    assert deck.summary({}).startswith("Leads with unhappy customers")
    assert "(default order)" in deck.summary({})
    moved = {"deck_priority": {"money": "first", "quiet": "last"}}
    assert deck.summary(moved) == "Leads with money · gone quiet last"


# ── The swipe ──────────────────────────────────────────────────────────────
def test_dismiss_marks_the_row_and_scopes_it_to_the_tenant(client, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "dismiss_concierge_notification",
                        lambda tid, nid, **k: calls.append((tid, nid)) or True)
    c = client
    c.cookies.set("bl_csrf", "tok")
    r = c.post("/concierge/notifications/n1/dismiss", headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls == [("t1", "n1")]


def test_dismiss_needs_the_csrf_header(client):
    client.cookies.set("bl_csrf", "tok")
    assert client.post("/concierge/notifications/n1/dismiss").status_code == 403


def test_a_dismiss_that_will_not_write_is_reported_not_raised(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "dismiss_concierge_notification", _boom)
    client.cookies.set("bl_csrf", "tok")
    r = client.post("/concierge/notifications/n1/dismiss",
                    headers={"X-CSRF-Token": "tok"})
    assert r.status_code == 200 and r.json() == {"ok": False}
