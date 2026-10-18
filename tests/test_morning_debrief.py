"""Morning debrief (docs/modules/module-morning-debrief.md) — Phase 1.

Pins:
- compose() is pure and zero-LLM: empty data → None (a quiet day sends
  NOTHING), salience order waiting > money > diary > relationships, body caps
  at three lines while the agent-log summary carries everything;
- gather() is per-section best-effort — one failing read drops its section,
  never the brief;
- open_today() (D6) follows the tenant's declared schedule: no scheduling
  config → every day open; working_days + blackout_dates honoured; a read
  error fails OPEN;
- the sweep stamps debrief_last_sent_on BEFORE composing (a crash costs one
  brief, never a double-push), skip-and-stamps closed days unless
  debrief_on_closed_days, and one bad tenant never stops the run;
- db.list_debrief_due_tenants drops already-sent-today rows but keeps
  never-sent (NULL) ones;
- the settings POST saves enabled + validated time, degrades junk time to
  07:30, and can only flip the closed-days override when a diary rendered it;
- the onboarding interview POST persists the debrief ask (D1).

No network: db.* monkeypatched; the autouse conftest guard fails loudly on any
un-stubbed live Supabase call.

Run: python -m pytest tests/test_morning_debrief.py -q
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.onboarding as onboarding
from app import db, debrief, push
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.jobs import morning_debrief
from app.main import app
from app.security import require_csrf

TODAY = date(2026, 8, 3)          # a Monday
MORNING = datetime(2026, 8, 3, 7, 35)   # when the brief actually goes out
SATURDAY = date(2026, 8, 1)


# ── compose(): pure, zero-LLM ────────────────────────────────────────────────
def test_compose_empty_day_is_none():
    assert debrief.compose({}, TODAY) is None


def test_a_morning_with_only_old_news_still_stays_silent():
    """She retells the night's events, but they are not a REASON to speak: each
    one already buzzed at the time, and nothing is pending. Silence on a day
    with nothing waiting is what keeps the loud days credible — so events alone
    compose to None, and no push, feed row or chat turn is written."""
    assert debrief.compose(
        {"since_last": [{"kind": "quote_accepted", "who": "Sarah", "title": ""}]},
        TODAY, at=MORNING) is None


def test_compose_full_day_salience_order_and_caps():
    data = {
        "drafts": {"count": 3, "oldest_days": 2},
        "overdue": {"count": 2, "total": 540.0},
        "quotes": {"count": 1, "expiring_today": 1},
        "diary": {"total": 6, "confirmed": 4, "first": "08:30"},
        "crm": {"quiet": 2, "quiet_top": "Alison", "hot": 1},
    }
    brief = debrief.compose(data, TODAY)
    assert brief["title"] == "Morning brief — Mon 3 Aug"
    # body = top three lines, salience order: drafts, invoices, quotes
    assert brief["body"] == ("3 drafts waiting (oldest 2d) · "
                             "2 invoices overdue (£540) · "
                             "1 quote awaiting a decision, 1 expiring today")
    # the diary + crm lines still reach the agent-log summary
    assert "6 bookings today, first at 08:30 (2 to confirm)" in brief["summary"]
    assert "2 gone quiet" in brief["summary"] and "1 running hot" in brief["summary"]
    assert len(brief["body"]) <= 500


def test_compose_singulars_and_fresh_drafts():
    brief = debrief.compose({"drafts": {"count": 1, "oldest_days": 0}}, TODAY)
    # one draft, landed today: singular noun, no "(oldest 0d)" noise
    assert brief["body"] == "1 draft waiting"


def test_compose_single_quiet_contact_is_named():
    brief = debrief.compose({"crm": {"quiet": 1, "quiet_top": "Alison", "hot": 0}}, TODAY)
    assert brief["body"] == "1 gone quiet (Alison)"


# ── Phase 2 (D5): the chat rendering + section links ─────────────────────────
def test_compose_chat_is_spoken_and_the_push_is_not():
    """Two voices out of one set of facts (Kris 2026-09-04). The push body is a
    lock screen and stays a terse list; the chat turn is somebody telling you."""
    data = {
        "drafts": {"count": 3, "oldest_days": 2},
        "overdue": {"count": 2, "total": 540.0},
        "diary": {"total": 6, "confirmed": 6, "first": "08:30"},
    }
    brief = debrief.compose(data, TODAY, at=MORNING)
    assert brief["body"] == "3 drafts waiting (oldest 2d) · 2 invoices overdue (£540) · 6 bookings today, first at 08:30"
    assert brief["chat"] == (
        "Morning.\n\n"
        "You've six bookings in today, the first at 08:30.\n\n"
        "Three drafts are waiting on you — the oldest's been there two days. "
        "Two invoices are overdue, £540 between them.")
    # links only for present sections, in salience order
    assert brief["links"] == [
        {"label": "Review drafts", "href": "/app/"},
        {"label": "Overdue invoices", "href": "/app/invoices"},
        {"label": "Today's diary", "href": "/app/diary"},
    ]


def test_the_page_recognises_her_greeting():
    """The chip detection used to key on the chat turn starting with the push
    title. She does not announce herself with one any more — a receptionist says
    "Morning", not "Morning brief, Monday the 3rd of August"."""
    brief = debrief.compose({"drafts": {"count": 1, "oldest_days": 0}}, TODAY,
                            at=MORNING)
    assert debrief.is_brief(brief["chat"])
    assert brief["title"].startswith(debrief.CHAT_PREFIX)   # the PUSH still does


def test_an_ordinary_answer_is_not_mistaken_for_a_brief():
    assert not debrief.is_brief("You've 3 quotes open right now.")
    assert not debrief.is_brief("")
    assert not debrief.is_brief(None)


def test_she_says_afternoon_when_the_brief_goes_out_after_noon():
    data = {"drafts": {"count": 1, "oldest_days": 0}}
    late = debrief.compose(data, TODAY, at=datetime(2026, 8, 3, 12, 30))
    assert late["chat"].startswith("Afternoon.")
    assert debrief.is_brief(late["chat"])


# ── speak(): the receptionist ────────────────────────────────────────────────
# Kris 2026-09-04: "You arrive at the office, walking past your receptionist,
# she calls out to you — a conversational brief of what you missed and what's
# coming up for the day." The bullets said every fact and told you none of them.
def _ev(kind, who):
    return {"kind": kind, "who": who, "title": ""}


def test_she_leads_with_what_you_missed():
    said = debrief.speak({"since_last": [
        _ev("quote_accepted", "Sarah Vance"),
        _ev("invoice_paid", "June Okafor")]}, at=MORNING)
    assert said.startswith("Morning.\n\nWhile you were out, ")
    assert ("Sarah Vance accepted their quote and June Okafor paid their "
            "invoice.") in said


def test_the_same_news_about_several_people_is_one_clause():
    """Three separate acceptances read as one clause with three names, not as
    the same sentence three times."""
    said = debrief.speak({"since_last": [
        _ev("portal_booking_created", "Sarah"),
        _ev("portal_booking_created", "Tom"),
        _ev("portal_booking_created", "June")]}, at=MORNING)
    assert "Sarah, Tom and June booked in." in said


def test_beyond_three_names_she_counts_the_rest():
    said = debrief.speak({"since_last": [
        _ev("invoice_paid", n) for n in ("Sarah", "Tom", "June", "Ali")]},
        at=MORNING)
    assert "Sarah, Tom and two others paid their invoices." in said


def test_she_stops_listing_and_points_at_today():
    """A receptionist does not read out sixteen things."""
    said = debrief.speak({"since_last": [
        _ev(k, "Someone") for k in
        ("quote_accepted", "invoice_paid", "portal_escalation", "quote_question",
         "portal_booking_created", "quote_declined")]}, at=MORNING)
    assert "a few other bits — they're all on Today." in said
    assert "quote" in said                       # the top ones still got said


def test_a_draft_landing_is_not_something_that_happened_to_a_customer():
    """draft_ready has no clause: "a reply is waiting" is not news about them,
    and the drafts sentence says it better a paragraph later."""
    said = debrief.speak({"since_last": [_ev("draft_ready", "Sarah")],
                          "drafts": {"count": 1, "oldest_days": 0}}, at=MORNING)
    assert "While you were out" not in said
    assert "There's a draft waiting on you." in said


def test_an_unknown_kind_is_left_out_rather_than_mangled():
    said = debrief.speak({"since_last": [_ev("some_new_thing_2027", "Sarah")],
                          "drafts": {"count": 1, "oldest_days": 0}}, at=MORNING)
    assert "While you were out" not in said


def test_a_nameless_event_still_gets_said():
    said = debrief.speak({"since_last": [_ev("invoice_paid", "")]}, at=MORNING)
    assert "Someone paid their invoice." in said


def test_two_nameless_events_are_counted_not_someone_twice():
    """Live, rig 2026-09-25: "Someone have quotes drafted"."""
    said = debrief.speak({"since_last": [_ev("quote_drafted", ""),
                                         _ev("quote_drafted", "")]}, at=MORNING)
    assert "Two people have quotes drafted." in said
    assert "Someone" not in said


def test_one_person_twice_is_one_name():
    said = debrief.speak({"since_last": [_ev("quote_drafted", "Ellie"),
                                         _ev("quote_drafted", "Ellie")]}, at=MORNING)
    assert "Ellie has a quote drafted." in said


def test_names_and_a_nameless_one():
    said = debrief.speak({"since_last": [_ev("invoice_paid", "Sarah"),
                                         _ev("invoice_paid", "")]}, at=MORNING)
    assert "Sarah and one other paid their invoices." in said
    said = debrief.speak({"since_last": [
        _ev("invoice_paid", n) for n in ("Sarah", "Tom", "June", "")]}, at=MORNING)
    assert "Sarah, Tom and two others paid their invoices." in said


def test_the_unbriefed_read_carries_the_name(monkeypatch):
    """The brief names people from contact_name; the read must fetch it (it
    didn't, 2026-09-04 → 09-25, so every clause read "Someone")."""
    seen = {}

    class Q:
        def __getattr__(self, name):
            return lambda *a, **k: self
        def select(self, cols, *a, **k):
            seen["cols"] = cols
            return self
        def execute(self):
            return type("R", (), {"data": []})()

    class C:
        def table(self, name):
            return Q()

    db.list_unbriefed_notifications("t1", not_before="2026-09-01", client=C())
    assert "contact_name" in seen["cols"].split(",")


def test_a_balance_raised_overnight_is_said():
    """Bill the balance on draft raises it the day before collection; the
    brief says whose it is (Kris 2026-09-25)."""
    said = debrief.speak({"since_last": [_ev("invoice_balance_ready", "Jess")]},
                         at=MORNING)
    assert "Jess has a balance ready to send." in said


def test_todays_diary_is_spoken_forwards():
    said = debrief.speak({"diary": {"total": 6, "confirmed": 4, "first": "09:00"}},
                         at=MORNING)
    assert "You've six bookings in today, the first at 09:00." in said
    assert "Two still need confirming." in said


def test_one_booking_all_confirmed_says_neither_plural_nor_chore():
    said = debrief.speak({"diary": {"total": 1, "confirmed": 1, "first": "09:00"}},
                         at=MORNING)
    assert "You've one booking in today, the first at 09:00." in said
    assert "confirming" not in said


def test_the_oldest_draft_is_dated_the_way_you_would_say_it():
    fresh = debrief.speak({"drafts": {"count": 2, "oldest_days": 0}}, at=MORNING)
    assert "Two drafts are waiting on you." in fresh
    yesterday = debrief.speak({"drafts": {"count": 2, "oldest_days": 1}}, at=MORNING)
    assert "the oldest since yesterday." in yesterday
    older = debrief.speak({"drafts": {"count": 2, "oldest_days": 4}}, at=MORNING)
    assert "the oldest's been there four days." in older


def test_a_lone_overdue_invoice_is_named():
    said = debrief.speak({"overdue": {"count": 1, "total": 900.0,
                                      "who": "Ashworth & Son", "ref": "INV-1044"}},
                         at=MORNING)
    assert "One invoice is overdue from Ashworth & Son — £900." in said


def test_several_overdue_name_the_oldest():
    said = debrief.speak({"overdue": {"count": 3, "total": 2400.0,
                                      "who": "Ashworth & Son", "ref": "INV-1044"}},
                         at=MORNING)
    assert ("Three invoices are overdue, £2,400 between them. "
            "The oldest is Ashworth & Son's.") in said


def test_an_overdue_invoice_with_no_customer_still_reads():
    said = debrief.speak({"overdue": {"count": 1, "total": 900.0, "who": None}},
                         at=MORNING)
    assert "One invoice is overdue — £900." in said


def test_a_quote_expiring_today_is_flagged_in_the_same_breath():
    one = debrief.speak({"quotes": {"count": 1, "expiring_today": 1}}, at=MORNING)
    assert "One quote is still waiting on a decision — and it expires today." in one
    many = debrief.speak({"quotes": {"count": 4, "expiring_today": 2}}, at=MORNING)
    assert "Four quotes are still waiting on a decision — two of them expire today." in many


def test_a_single_quiet_contact_is_named_and_a_crowd_is_counted():
    one = debrief.speak({"crm": {"quiet": 1, "quiet_top": "Alison", "hot": 0}},
                        at=MORNING)
    assert "Alison has gone quiet." in one
    many = debrief.speak({"crm": {"quiet": 3, "quiet_top": "Alison", "hot": 2}},
                         at=MORNING)
    assert "Three contacts have gone quiet and two are reading unhappy." in many


def test_she_never_ends_on_an_offer():
    """Kris 2026-09-25: "your receptionist wouldn't offer to do whatever she
    normally does, because she does it anyway." She says what needs you, and
    stops."""
    for data in ({"drafts": {"count": 1, "oldest_days": 0}, "overdue": {"count": 1, "total": 10.0}},
                 {"overdue": {"count": 1, "total": 10.0}},
                 {"since_last": [_ev("invoice_paid", "June")]}):
        said = debrief.speak(data, at=MORNING)
        assert "?" not in said, said
    assert not hasattr(debrief, "brief_ask") and not hasattr(debrief, "_ASKS")


def test_the_default_topics_leave_out_her_own_work_and_small_signals():
    on = debrief.topics_on({})
    assert on == debrief.DEFAULT_TOPICS
    assert {"approvals", "diary", "money", "customers", "wins", "quotes"} <= on
    assert "prepared" not in on and "signals" not in on


def test_topics_filter_what_she_says():
    data = {"drafts": {"count": 2, "oldest_days": 0},
            "diary": {"total": 1, "confirmed": 1, "first": None},
            "overdue": {"count": 1, "total": 30.0, "who": "Meg"},
            "since_last": [_ev("quote_drafted", "Jess"), _ev("invoice_paid", "June"),
                           _ev("quote_viewed", "Ali"), _ev("invoice_balance_ready", "Jess")],
            "held": {"count": 1, "titles": ["Quote QUO-1 accepted"]}}
    kept = debrief.apply_topics(data, debrief.DEFAULT_TOPICS)
    assert [e["kind"] for e in kept["since_last"]] == ["invoice_paid"]   # her work + signals gone
    assert "held" in kept                                                 # "In the brief" is a promise
    said = debrief.speak(kept, at=MORNING)
    assert "June paid their invoice." in said and "Jess" not in said and "Ali" not in said
    only_money = debrief.apply_topics(data, {"money"})
    assert set(only_money) == {"overdue", "held"}
    assert debrief.apply_topics(data, {"prepared"})["since_last"][0]["kind"] == "quote_drafted"


def test_a_saved_empty_list_means_everything_off_and_junk_means_defaults():
    assert debrief.topics_on({"debrief_topics": []}) == frozenset()
    assert debrief.topics_on({"debrief_topics": ["money", "bogus"]}) == {"money"}
    assert debrief.topics_on({"debrief_topics": "money"}) == debrief.DEFAULT_TOPICS


def test_nothing_chosen_to_say_sends_nothing():
    kept = debrief.apply_topics({"drafts": {"count": 3, "oldest_days": 1}}, {"diary"})
    assert debrief.compose(kept, TODAY, at=MORNING) is None


def test_numbers_are_spoken_but_money_and_clocks_are_not():
    said = debrief.speak({"drafts": {"count": 3, "oldest_days": 0},
                          "overdue": {"count": 1, "total": 1250.0, "who": None},
                          "diary": {"total": 2, "confirmed": 2, "first": "08:15"}},
                         at=MORNING)
    assert "Three drafts" in said and "3 drafts" not in said
    assert "£1,250" in said                       # nobody says one thousand two hundred
    assert "08:15" in said                        # a clock is read, not spoken


def test_a_big_count_falls_back_to_figures():
    said = debrief.speak({"drafts": {"count": 40, "oldest_days": 0}}, at=MORNING)
    assert "40 drafts are waiting on you." in said


# ── The grammar, which is most of whether she sounds like a person ──────────
# Every one of these was found by reading a rendered brief, not by a test —
# they are here so the next edit to the wording cannot quietly undo them.
def test_a_clause_containing_and_takes_a_serial_comma():
    """"…move their booking and Ali and Dee opened their quotes" makes you stop
    and re-parse; the comma is what stops that."""
    said = debrief.speak({"since_last": [
        _ev("portal_reschedule_request", "Tom Beale"),
        _ev("quote_viewed", "Ali Chowdhury"),
        _ev("quote_viewed", "Dee Marsh")]}, at=MORNING)
    assert ("Tom Beale asked to move their booking, and Ali Chowdhury and "
            "Dee Marsh opened their quotes.") in said
    # …and stays out of the way when no clause needs it.
    plain = debrief.speak({"since_last": [_ev("invoice_paid", "June"),
                                          _ev("quote_accepted", "Sarah")]},
                          at=MORNING)
    assert "Sarah accepted their quote and June paid their invoice." in plain


def test_a_surname_is_not_lower_cased_by_starting_a_sentence():
    """str.capitalize() lowercases the rest of the string, which turned
    "Alison Reed has gone quiet" into "Alison reed has gone quiet"."""
    said = debrief.speak({"crm": {"quiet": 1, "quiet_top": "Alison Reed", "hot": 0}},
                         at=MORNING)
    assert "Alison Reed has gone quiet." in said


def test_a_name_ending_in_s_takes_a_bare_apostrophe():
    said = debrief.speak({"overdue": {"count": 2, "total": 100.0,
                                      "who": "Beale Homes"}}, at=MORNING)
    assert "The oldest is Beale Homes'." in said
    other = debrief.speak({"overdue": {"count": 2, "total": 100.0,
                                       "who": "Ashworth & Son"}}, at=MORNING)
    assert "The oldest is Ashworth & Son's." in other


def test_one_of_several_quotes_expires_rather_than_expire():
    said = debrief.speak({"quotes": {"count": 3, "expiring_today": 1}}, at=MORNING)
    assert "one of them expires today." in said


# ── gather(): best-effort sections ───────────────────────────────────────────
NOW = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)   # 09:00 on TODAY


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_gather_all_sections(monkeypatch):
    # with_recency defaults its clock to real datetime.now(); pin it to NOW so
    # the _iso() ages above hold on any run date (real logic still exercised).
    from app import crm
    real_with_recency = crm.with_recency
    monkeypatch.setattr(debrief.crm_mod, "with_recency",
                        lambda rows, now=None, **k:
                        real_with_recency(rows, now=NOW, **k))
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [
        {"created_at": _iso(0)}, {"created_at": _iso(3)}])   # newest first
    monkeypatch.setattr(db, "list_bookings", lambda *a, **k: [
        {"booking_date": "2026-08-03", "status": "confirmed", "window": "09:00"},
        {"booking_date": "2026-08-03", "status": "proposed", "window": "11:00"},
        {"booking_date": "2026-08-04", "status": "confirmed", "window": "08:00"},
    ])
    monkeypatch.setattr(db, "list_invoices", lambda *a, **k: [
        {"status": "overdue", "due_date": "2026-07-20", "total": 100},
        {"status": "sent", "due_date": "2026-08-01", "total": 50},
        {"status": "paid", "due_date": "2026-07-01", "total": 999},   # settled — out
        {"status": "sent", "due_date": "2026-08-09", "total": 999},   # not yet due — out
    ])
    monkeypatch.setattr(db, "list_quotes", lambda *a, **k: [
        {"status": "sent", "valid_until": "2026-08-03"},
        {"status": "accepted", "valid_until": "2026-08-03"},          # decided — out
    ])
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"crm_quiet_days": 14})
    monkeypatch.setattr(db, "crm_list_contacts", lambda *a, **k: [
        {"name": "Alison", "last_activity_at": _iso(20)},             # stale
        {"name": "Bob", "last_activity_at": _iso(1), "sentiment": "frustrated"},
    ])
    data = debrief.gather("t1", TODAY)
    assert data["drafts"] == {"count": 2, "oldest_days": 3}
    assert data["diary"] == {"total": 2, "confirmed": 1, "first": "09:00"}
    # The oldest overdue is named, so the spoken brief can say whose it is.
    assert data["overdue"] == {"count": 2, "total": 150.0, "who": None,
                               "ref": None}
    assert data["quotes"] == {"count": 1, "expiring_today": 1}
    assert data["crm"]["quiet"] == 1 and data["crm"]["quiet_top"] == "Alison"
    assert data["crm"]["hot"] == 1


def test_gather_one_failing_read_drops_only_its_section(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "list_email_log", _boom)
    monkeypatch.setattr(db, "list_bookings", lambda *a, **k: [
        {"booking_date": "2026-08-03", "status": "confirmed", "window": "am"}])
    monkeypatch.setattr(db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: set())   # no CRM
    data = debrief.gather("t1", TODAY)
    assert "drafts" not in data
    # am/pm windows carry no clock time — first stays None, count survives
    assert data["diary"] == {"total": 1, "confirmed": 1, "first": None}


def test_gather_no_crm_module_skips_crm_reads(monkeypatch):
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: set())
    def _never(*a, **k):
        raise AssertionError("crm read must not run without the module")
    monkeypatch.setattr(db, "crm_list_contacts", _never)
    assert debrief.gather("t1", TODAY) == {}


# ── open_today(): the D6 closed-day gate ─────────────────────────────────────
def test_open_today_no_config_is_always_open(monkeypatch):
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: None)
    assert debrief.open_today("t1", SATURDAY) is True


def test_open_today_follows_working_days(monkeypatch):
    monkeypatch.setattr(db, "get_scheduling_config",
                        lambda *a, **k: {"booking_setting_review": {"working_days": {"state": "set", "review_status": "confirmed"}},
                                         "working_days": "mon,tue,wed,thu,fri"})
    assert debrief.open_today("t1", TODAY) is True          # Monday
    assert debrief.open_today("t1", SATURDAY) is False      # Saturday


def test_open_today_honours_blackouts(monkeypatch):
    monkeypatch.setattr(db, "get_scheduling_config",
                        lambda *a, **k: {"booking_setting_review": {"working_days": {"state": "set", "review_status": "confirmed"}},
                                         "working_days": "mon,tue,wed,thu,fri",
                                         "blackout_dates": ["2026-08-03"]})
    assert debrief.open_today("t1", TODAY) is False


def test_open_today_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "get_scheduling_config", _boom)
    assert debrief.open_today("t1", SATURDAY) is True


# ── the sweep job ────────────────────────────────────────────────────────────
@pytest.fixture
def sweep(monkeypatch):
    """Recorders around the job: stamps, pushes, agent-log rows, chat turns."""
    state = {"stamps": [], "pushes": [], "events": [], "chats": [],
             "briefed": [], "order": []}
    monkeypatch.setattr(db, "stamp_debrief_sent",
                        lambda tid, day, **k: (state["stamps"].append((tid, day)),
                                               state["order"].append(("stamp", tid)))[0])
    monkeypatch.setattr(db, "log_agent_event",
                        lambda tid, kind, summary, **k: state["events"].append((tid, kind)))
    monkeypatch.setattr(push, "notify",
                        lambda tid, **kw: (state["pushes"].append((tid, kw)),
                                           state["order"].append(("push", tid)))[0])
    monkeypatch.setattr(db, "add_concierge_message",
                        lambda tid, role, content, **k: state["chats"].append(
                            (tid, role, content, k.get("links"))))
    monkeypatch.setattr(db, "mark_notifications_briefed",
                        lambda tid, **k: (state["briefed"].append(tid),
                                          state["order"].append(("briefed", tid)))[0])
    return state


@pytest.mark.asyncio
async def test_sweep_sends_and_stamps_first(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    monkeypatch.setattr(debrief, "gather",
                        lambda *a, **k: {"drafts": {"count": 2, "oldest_days": 1}})
    result = await morning_debrief.run()
    assert result["sent"] == 1 and result["errors"] == 0
    assert sweep["stamps"] and sweep["pushes"]
    # stamp-first ordering: the stamp precedes the push
    assert sweep["order"][0][0] == "stamp"
    tid, kw = sweep["pushes"][0]
    assert kw["kind"] == "morning_debrief" and kw["url"] == "/app/"
    assert kw["tag"].startswith("debrief-")
    assert sweep["events"] == [("t1", "morning_debrief")]
    # Phase 2 (D5): the brief also lands as the concierge's assistant turn
    tid, role, content, links = sweep["chats"][0]
    assert (tid, role) == ("t1", "assistant")
    assert debrief.is_brief(content)
    assert {"label": "Review drafts", "href": "/app/"} in links


@pytest.mark.asyncio
async def test_sweep_closed_day_stamps_but_never_reads(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: False)
    def _never(*a, **k):
        raise AssertionError("closed day must not gather")
    monkeypatch.setattr(debrief, "gather", _never)
    result = await morning_debrief.run()
    assert result["closed_day"] == 1 and result["sent"] == 0
    assert sweep["stamps"] == [("t1", datetime.now(morning_debrief.DIARY_TZ)
                                .date().isoformat())]
    assert not sweep["pushes"]


@pytest.mark.asyncio
async def test_sweep_closed_day_override_still_sends(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": True}])
    def _never(*a, **k):
        raise AssertionError("override must not consult the schedule")
    monkeypatch.setattr(debrief, "open_today", _never)
    monkeypatch.setattr(debrief, "gather",
                        lambda *a, **k: {"drafts": {"count": 1, "oldest_days": 0}})
    result = await morning_debrief.run()
    assert result["sent"] == 1


@pytest.mark.asyncio
async def test_sweep_quiet_day_sends_nothing(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    monkeypatch.setattr(debrief, "gather", lambda *a, **k: {})
    result = await morning_debrief.run()
    assert result["quiet"] == 1 and result["sent"] == 0
    assert sweep["stamps"] and not sweep["pushes"] and not sweep["events"]
    assert not sweep["chats"]   # a quiet day injects no chat turn either


@pytest.mark.asyncio
async def test_sweep_one_bad_tenant_never_stops_the_run(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "bad", "debrief_on_closed_days": False},
        {"tenant_id": "good", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    def _gather(tid, *a, **k):
        if tid == "bad":
            raise RuntimeError("boom")
        return {"drafts": {"count": 1, "oldest_days": 0}}
    monkeypatch.setattr(debrief, "gather", _gather)
    result = await morning_debrief.run()
    assert result["errors"] == 1 and result["sent"] == 1
    assert [t for t, _ in sweep["pushes"]] == ["good"]


# ── The held queue: "in the brief" has to actually reach a brief ─────────────
# Kris 2026-09-04. gather() used to pick held notifications with a rolling
# `now - 24h` window, and the sweep skips closed days BEFORE it gathers — so a
# Friday-evening held item was already out of range by Monday's brief and was
# never mentioned by anything. The window is gone: briefed_at stamps what a
# brief said, the queue is what is unstamped, and it closes only on a send.
@pytest.fixture
def held(monkeypatch):
    """gather() with only its held section live — every other read empty."""
    for name in ("list_email_log", "list_bookings", "list_invoices", "list_quotes"):
        monkeypatch.setattr(db, name, lambda *a, **k: [])
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: set())
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"notify_quotes": "brief"})
    calls: list[dict] = []

    def _unbriefed(tenant_id, *, not_before, **k):
        calls.append({"tenant_id": tenant_id, "not_before": not_before})
        return [{"kind": "quote_accepted", "title": "Quote QUO-1012 accepted",
                 "created_at": _iso(3)}]
    monkeypatch.setattr(db, "list_unbriefed_notifications", _unbriefed)
    return calls


def test_held_reads_the_unbriefed_queue_not_a_time_window(held):
    data = debrief.gather("t1", TODAY)
    assert data["held"] == {"count": 1, "titles": ["Quote QUO-1012 accepted"]}
    assert held[0]["tenant_id"] == "t1"
    # The floor is a resurrection guard, not a window: it must be far enough
    # back to clear a closed weekend several times over.
    floor = datetime.fromisoformat(held[0]["not_before"])
    assert timedelta(days=13) < datetime.now(timezone.utc) - floor < timedelta(days=15)


def test_a_three_day_old_held_item_still_gets_its_mention(held):
    """The exact case the 24h window dropped: held Friday, brief on Monday."""
    brief = debrief.compose(debrief.gather("t1", TODAY), TODAY)
    assert "While you were away: Quote QUO-1012 accepted" in brief["summary"]


def test_a_family_set_to_now_is_not_repeated_in_the_brief(held, monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"notify_quotes": "now"})
    assert "held" not in debrief.gather("t1", TODAY)


@pytest.mark.asyncio
async def test_a_closed_day_leaves_the_held_queue_standing(monkeypatch, sweep):
    """The regression. Saturday skips; nothing is marked briefed, so Monday's
    brief still has Friday's held notification to mention."""
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: False)
    result = await morning_debrief.run()
    assert result["closed_day"] == 1
    assert sweep["briefed"] == [], "a skipped day must not claim to have briefed"


@pytest.mark.asyncio
async def test_a_quiet_day_leaves_the_held_queue_standing(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    monkeypatch.setattr(debrief, "gather", lambda *a, **k: {})
    await morning_debrief.run()
    assert sweep["briefed"] == []


@pytest.mark.asyncio
async def test_the_queue_closes_only_after_the_brief_has_gone_out(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    monkeypatch.setattr(debrief, "gather", lambda *a, **k: {
        "held": {"count": 1, "titles": ["Quote QUO-1012 accepted"]}})
    await morning_debrief.run()
    assert sweep["briefed"] == ["t1"]
    # Stamped AFTER the push: the other order loses the mention on a crash,
    # this one repeats it tomorrow.
    kinds = [k for k, _ in sweep["order"]]
    assert kinds.index("push") < kinds.index("briefed")


@pytest.mark.asyncio
async def test_a_stamp_failure_costs_a_repeat_never_the_brief(monkeypatch, sweep):
    monkeypatch.setattr(db, "list_debrief_due_tenants", lambda *a, **k: [
        {"tenant_id": "t1", "debrief_on_closed_days": False}])
    monkeypatch.setattr(debrief, "open_today", lambda *a, **k: True)
    monkeypatch.setattr(debrief, "gather",
                        lambda *a, **k: {"drafts": {"count": 1, "oldest_days": 0}})
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "mark_notifications_briefed", _boom)
    result = await morning_debrief.run()
    assert result["sent"] == 1 and result["errors"] == 0
    assert sweep["pushes"], "the brief went out regardless"


# ── db.list_debrief_due_tenants: the not-sent-today filter ───────────────────
def test_due_list_keeps_null_drops_sent_today(monkeypatch):
    rows = [
        {"tenant_id": "never", "debrief_last_sent_on": None},
        {"tenant_id": "old", "debrief_last_sent_on": "2026-08-02"},
        {"tenant_id": "done", "debrief_last_sent_on": "2026-08-03"},
    ]
    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def lte(self, *a, **k): return self
        def execute(self):
            class R:
                data = rows
            return R()
    class _C:
        def table(self, name): return _Q()
    got = db.list_debrief_due_tenants("2026-08-03", "07:30", client=_C())
    assert [r["tenant_id"] for r in got] == ["never", "old"]


# ── the settings POST ────────────────────────────────────────────────────────
@pytest.fixture
def settings_client(monkeypatch):
    def _make(mods=("scheduling",)):
        calls = []
        monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set(mods))
        monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                            lambda tid, cfg, **k: calls.append(cfg))
        app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
        app.dependency_overrides[require_csrf] = lambda: None
        return TestClient(app), calls
    yield _make
    app.dependency_overrides.clear()


def test_settings_save_roundtrip(settings_client):
    client, calls = settings_client()
    r = client.post("/app/settings/debrief",
                    data={"debrief_enabled": "on", "debrief_time": "06:45",
                          "debrief_on_closed_days": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calls == [{"debrief_enabled": True, "debrief_time": "06:45",
                      "debrief_on_closed_days": True}]


def test_settings_junk_time_degrades_to_default(settings_client):
    client, calls = settings_client()
    client.post("/app/settings/debrief",
                data={"debrief_time": "9am sharp"}, follow_redirects=False)
    assert calls[0]["debrief_time"] == "07:30"
    assert calls[0]["debrief_enabled"] is False   # unticked checkbox = off


def test_settings_no_diary_cannot_flip_closed_days(settings_client):
    # The form never rendered the toggle — the save must not smuggle it in.
    client, calls = settings_client(mods=())
    client.post("/app/settings/debrief",
                data={"debrief_enabled": "on", "debrief_time": "07:30",
                      "debrief_on_closed_days": "on"},
                follow_redirects=False)
    assert "debrief_on_closed_days" not in calls[0]


# ── Phase 2 (D5): the concierge page's follow-up chip ────────────────────────
@pytest.fixture
def concierge_page(monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"provider": "microsoft"})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _brief_turn(content=None):
    return {"role": "assistant", "links": [], "trail": [],
            "content": content or ("Morning.\n\nThere's a draft waiting on you."
                                   "\n\nWant me to start with the drafts?")}


def test_the_newest_brief_carries_the_cog_and_no_chip(concierge_page, monkeypatch):
    """The cog on the brief's bubble (Kris 2026-09-25): what the brief tells
    you, changed where you read it. The "Yes please" chip went with the offer."""
    monkeypatch.setattr(dashboard.db, "list_concierge_messages",
                        lambda *a, **k: [_brief_turn("Morning.\n\nThere's a draft waiting on you.")])
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {"debrief_topics": ["money"]})
    html = concierge_page.get("/app/concierge").text
    assert "data-cz-brief-cog" in html and "Yes please" not in html
    assert 'value="money" checked' in html
    assert 'value="approvals" checked' not in html and 'value="approvals"' in html


def test_concierge_page_no_chip_once_conversation_moved_on(concierge_page, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [
        _brief_turn(),
        {"role": "user", "content": "thanks", "links": [], "trail": []},
        {"role": "assistant", "content": "Any time.", "links": [], "trail": []},
    ])
    r = concierge_page.get("/app/concierge")
    assert "Anything need chasing?" not in r.text


def test_concierge_page_no_chip_on_empty_history(concierge_page, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [])
    r = concierge_page.get("/app/concierge")
    assert r.status_code == 200
    assert "Anything need chasing?" not in r.text


# ── the onboarding interview POST (D1) ───────────────────────────────────────
@pytest.fixture
def interview_client(monkeypatch):
    calls = []
    monkeypatch.setattr(onboarding.db, "set_tenant_industry", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config",
                        lambda tid, cfg, **k: calls.append(cfg))
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), calls
    app.dependency_overrides.clear()


# The debrief moved OFF the interview (Julius 2026-08-02): it's a Settings
# concern, and the on-at-07:30 default seeds at tenant creation instead —
# because the sweep selects debrief_enabled = True, a NULL row gets no brief.
def test_interview_page_has_no_debrief_card(interview_client, monkeypatch):
    client, _ = interview_client
    monkeypatch.setattr(onboarding.db, "get_tenant", lambda *a, **k: {"industry": None})
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(onboarding.db, "get_tenant_referral", lambda *a, **k: None)
    r = client.get("/onboarding/interview")
    assert r.status_code == 200
    assert 'name="debrief_enabled"' not in r.text
    assert 'name="debrief_time"' not in r.text


def test_interview_post_ignores_debrief_fields(interview_client):
    client, calls = interview_client
    r = client.post("/onboarding/interview",
                    data={"industry": "trades", "business_brief": "Plumbing",
                          "goals": "book jobs",
                          "debrief_enabled": "on", "debrief_time": "08:00"},
                    follow_redirects=False)
    assert r.status_code == 303
    cfg = calls[0]
    assert "debrief_enabled" not in cfg and "debrief_time" not in cfg


def test_new_tenant_seeds_debrief_default(monkeypatch):
    from app import db as dbmod
    seeded = {}

    class FakeTable:
        def insert(self, row):
            return self

        def execute(self):
            class R:
                data = [{"id": "t-new"}]
            return R()

    class FakeClient:
        def table(self, name):
            return FakeTable()

    monkeypatch.setattr(dbmod, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(dbmod, "db", lambda: FakeClient())
    monkeypatch.setattr(dbmod, "apply_plan_modules", lambda tid, plan: None)
    monkeypatch.setattr(dbmod, "upsert_tenant_config",
                        lambda tid, cfg, **k: seeded.update({tid: cfg}))
    assert dbmod.ensure_tenant_for_user("u1", "Acme") == "t-new"
    # draft_hold rides the same seed since 2026-09-02 (born held — see
    # test_draft_hold.py); the debrief defaults are unchanged.
    assert seeded["t-new"] == {"debrief_enabled": True, "debrief_time": "07:30",
                               "draft_hold": True}


def test_the_cog_saves_only_known_topics_in_order(monkeypatch):
    saved = {}
    monkeypatch.setattr(dashboard.db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: saved.update(cfg))
    ctx = TenantCtx(tenant_id="t1", client=None)
    assert dashboard._save_brief_topics(ctx, ["signals", "bogus", "money"]) == ["money", "signals"]
    assert saved == {"debrief_topics": ["money", "signals"]}


def test_settings_saves_the_brief_topics_only_when_the_list_was_on_screen():
    import inspect
    src = inspect.getsource(dashboard.save_debrief_settings)
    assert 'if form.get("debrief_topics_form"):' in src
    assert 'form.get("debrief_topic_" + k) is not None' in src
    html = open("src/app/templates/app/settings.html", encoding="utf-8").read()
    assert 'name="debrief_topics_form"' in html and 'name="debrief_topic_{{ t.key }}"' in html


def test_an_open_concierge_picks_up_a_newer_brief_and_buttons_sit_tight():
    """Kris 2026-09-25: a brief sent while the Concierge was open never showed,
    and the link buttons had blank lines under them."""
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "checkForNewBrief();" in js and "if (isOpen() && !voiceOverlayUp()) reloadPanel();" in js
    # …and while it is already open: the live stream says a brief landed
    assert "window.addEventListener('blam8:brief'" in js
    live = open("src/app/static/live.js", encoding="utf-8").read()
    assert "data.kind === 'morning_debrief'" in live and "new CustomEvent('blam8:brief')" in live
    html = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "data-cz-newest=" in html
    # The message rows (and their link chips) live in the history partial the panel includes.
    history = open("src/app/templates/app/_concierge_history.html", encoding="utf-8").read()
    assert '{% if m.links %}<div class="mt-2 -mb-1 flex flex-wrap gap-2 whitespace-normal">' in history
    assert "{% endfor %}</div>{% endif %}</div>" in history


@pytest.mark.parametrize("days,state,status", [
    (None, "unset", "confirmed"), ("", "unset", "confirmed"),
    ("mon,tue,wed,thu,fri", "unset", "confirmed"),
])
def test_open_today_undeclared_days_are_open(monkeypatch, days, state, status):
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: {
        "working_days": days, "booking_setting_review": {
            "working_days": {"state": state, "review_status": status}}})
    assert debrief.open_today("t1", SATURDAY) is True


@pytest.mark.parametrize("review", [
    {"working_days": {"state": "set", "review_status": "carried_over_review"}},
    {"working_days": {"state": "set", "review_status": "confirmed"}},
    {},   # a row saved before the review record existed
])
def test_open_today_days_in_use_close_the_weekend(monkeypatch, review):
    # Carried-over days are in use for bookings, so the debrief honours them too.
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: {
        "working_days": "mon,tue,wed,thu,fri", "booking_setting_review": review})
    assert debrief.open_today("t1", SATURDAY) is False
    assert debrief.open_today("t1", TODAY) is True
