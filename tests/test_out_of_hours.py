"""Out of hours and bank holidays (docs/plan-service-urgency.md, D1 stages 2-3).

Sarah, Fri 17:58, "no hot water, two small kids, could someone come today?":
Hartwell goes out in the evening at £140 the first hour, but the diary had no
way to say so. A service now carries out-of-hours cover — evenings until a
time, extra days, bank holidays — at its own price, and who decides: ask me
first (the default: the reply never sees it, Today asks), automatically, or
automatically for existing customers. Bank holidays come from gov.uk for the
business's nation: closed for normal work, open for out-of-hours cover.

Run: python -m pytest tests/test_out_of_hours.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

import pytest

from app import bank_holidays, scheduling, services
from app.scheduling import DIARY_TZ

CFG = carried_config({"windows": "ampm", "open_time": "08:00", "close_time": "18:00", "capacity_per_window": 2,
       "min_notice_hours": 4, "working_days": "mon,tue,wed,thu,fri,sat"})
OOH = {"until": "22:00", "days": [6], "bank_holidays": True, "price": 140.0,
       "price_after": 65.0, "offer": "ask", "price_basis": "first_hour"}
CALLOUT = {"name": "Emergency callout", "minutes": 120, "price": 85.0, "timing": "half",
           "same_day_until": "14:00", "notice_hours": 1, "out_of_hours": dict(OOH)}
SERVICE = {"name": "Boiler service", "minutes": 60, "price": 95.0, "timing": "half"}
FRI_1758 = datetime(2026, 9, 25, 17, 58, tzinfo=DIARY_TZ)


def slots(svc, now, d0, d1=None, cfg=CFG):
    length = scheduling.slotgrid.blocks_for_minutes(svc["minutes"])
    return scheduling.service_availability(cfg, [], length, d0, d1 or d0, now,
                                           timing=services.timing(svc), svc=svc)


def test_the_setting_is_cleaned_and_needs_a_price():
    got = services.clean_service_types([{**CALLOUT, "out_of_hours": {**OOH, "price": ""}}])
    assert "out_of_hours" not in got[0]                         # never offered without a price
    got = services.clean_service_types([CALLOUT, SERVICE])
    assert got[0]["out_of_hours"]["offer"] == "ask" and "out_of_hours" not in got[1]
    assert services.ooh_label(got[0]) == ("evenings until 22:00, Sun and bank holidays, "
                                          "£140 for the first hour then £65 an hour (ask me first)")
    assert "out of hours: evenings until 22:00" in services.render_for_prompt(got)


def test_friday_evening_opens_at_the_out_of_hours_price():
    got = slots(CALLOUT, FRI_1758, date(2026, 9, 25))
    assert [(s["window"], s["price"]) for s in got] == [("eve", 140.0)]
    assert got[0]["out_of_hours"] and got[0]["price_after"] == 65.0
    assert slots(SERVICE, FRI_1758, date(2026, 9, 25)) == []    # a normal service: nothing tonight


def test_sunday_opens_as_morning_and_afternoon_out_of_hours():
    sun = date(2026, 9, 27)
    got = slots(CALLOUT, FRI_1758, sun)
    assert {s["window"] for s in got} == {"am", "pm"} and all(s["out_of_hours"] for s in got)
    assert slots(SERVICE, FRI_1758, sun) == []


def test_bank_holidays_close_normal_work_and_open_the_callout(monkeypatch):
    xmas = date(2026, 12, 25)
    monkeypatch.setattr(bank_holidays, "days_for",
                        lambda n: {xmas: "Christmas Day"} if n == "england-and-wales" else {})
    cfg = {**CFG, "bank_holiday_nation": "england-and-wales"}
    now = datetime(2026, 12, 20, 9, 0, tzinfo=DIARY_TZ)
    assert slots(SERVICE, now, xmas, cfg=cfg) == []             # a Friday, but a bank holiday
    got = slots(CALLOUT, now, xmas, cfg=cfg)
    assert got and all(s["out_of_hours"] and s["price"] == 140.0 for s in got)
    # …unless they work it: back to normal.
    worked = {**cfg, "bank_holidays_open": ["2026-12-25"]}
    assert slots(SERVICE, now, xmas, cfg=worked)
    # No nation chosen: nothing changes (every business before this).
    assert slots(SERVICE, now, xmas)


def test_the_feed_is_read_per_nation():
    rows = bank_holidays.parse_feed({
        "england-and-wales": {"events": [{"title": "Boxing Day", "date": "2026-12-28"}]},
        "scotland": {"events": [{"title": "World Cup bank holiday", "date": "2026-06-15"}]},
        "northern-ireland": {"events": [{"title": "St Patrick's Day", "date": "2026-03-17"}]}})
    assert {(r["nation"], r["day"]) for r in rows} == {
        ("england-and-wales", "2026-12-28"), ("scotland", "2026-06-15"),
        ("northern-ireland", "2026-03-17")}
    assert bank_holidays.clean_nation("England") == "england-and-wales"


@pytest.fixture
def execu(monkeypatch):
    inserted, quotes = [], {}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config",
                        lambda *a, **k: carried_config({**CFG, "service_types": [CALLOUT, SERVICE]}))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda bid, tid, p: quotes.__setitem__(bid, p))

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": f"b{len(inserted) + 1}", "booking_ref": f"BK-90{len(inserted) + 1}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "window": window, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)

    def make(offer="ask", **kw):
        svc = {**CALLOUT, "out_of_hours": {**OOH, "offer": offer}}
        monkeypatch.setattr(scheduling.db, "get_scheduling_config",
                            lambda *a, **k: carried_config({**CFG, "service_types": [svc, SERVICE]}))
        return scheduling.make_executor("t1", contact="sarah@x.co", service_types=[svc, SERVICE],
                                        now=FRI_1758, **kw)
    return make, inserted, quotes


def test_ask_me_first_hides_the_evening_and_refuses_to_hold_it(execu):
    make, inserted, _ = execu
    ex, state = make("ask")
    got = ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25",
                                    "service": "Emergency callout"})
    assert got["available"] == [] and state["ooh_withheld"][0]["price"] == 140.0
    held = ex("hold_booking", {"date": "2026-09-25", "window": "evening",
                               "service": "Emergency callout", "summary": "No hot water",
                               "details": {"Job address": "14 Sydney Buildings"}})
    assert held["error"] == "ooh_needs_owner" and inserted == []


def test_automatic_offers_it_priced_and_the_hold_carries_the_price(execu):
    make, inserted, quotes = execu
    ex, state = make("auto")
    got = ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25",
                                    "service": "Emergency callout"})
    assert got["available"][0]["window"] == "eve" and "£140 for the first hour" in got["out_of_hours_note"]
    held = ex("hold_booking", {"date": "2026-09-25", "window": "eve",
                               "service": "Emergency callout", "summary": "No hot water",
                               "details": {"Job address": "14 Sydney Buildings"}})
    assert held.get("held") is True, held
    assert quotes[inserted[0]["id"]] == 140.0 and inserted[0]["window"] == "eve"


def test_the_owners_own_words_let_ask_me_first_offer_it(execu):
    make, _, _ = execu
    ex, _ = make("ask", owner_directed=True)
    got = ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25",
                                    "service": "Emergency callout"})
    assert got["available"] and got["available"][0]["out_of_hours"]


def test_existing_customers_only(execu):
    make, _, _ = execu
    ex, _ = make("existing", existing_bookings=[{"status": "completed"}])
    assert ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25",
                                     "service": "Emergency callout"})["available"]
    ex, _ = make("existing", existing_bookings=[])
    assert ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25",
                                     "service": "Emergency callout"})["available"] == []


def test_today_asks_the_owner(monkeypatch):
    from app import push, urgency
    sent = []
    monkeypatch.setattr(push, "notify", lambda tid, **k: sent.append(k))
    assert urgency.ask_owner_ooh("t1", contact="sarah@x.co", name="Sarah Bennett", withheld=[
        {"service": "Emergency callout", "price": 140.0,
         "slots": [{"date": "2026-09-25", "label": "Evening (arrival between 18:00 and 22:00) — out of hours"}]}])
    assert sent[0]["kind"] == "out_of_hours_ask" and "£140" in sent[0]["title"]


def test_the_diary_card_can_carry_them_from_the_owners_notes():
    """Stage 4: "same day if reported before 2pm" and "out-of-hours … £140 the
    first hour, then £65" reach the Diary card's service rows."""
    from app import settings_recommend
    fields = settings_recommend.PAGES["diary_settings"]["fields"]
    assert "prices the owner states" in fields["service_out_of_hours"]
    vals = settings_recommend.clean("diary_settings", [
        {"name": "service_urgent", "value": "Emergency callout | 14:00 | 1"},
        {"name": "service_out_of_hours", "value": "Emergency callout | 22:00 | Sun | yes | 140 | 65"}])
    assert [v["name"] for v in vals] == ["service_urgent", "service_out_of_hours"]
    js = open("src/app/static/concierge.js", encoding="utf-8").read()
    assert 'pfFillServiceRules(pluck(values, "service_urgent"), pluck(values, "service_out_of_hours"),' in js
    assert 'put(row, "service_ooh_price"' in js


def test_the_held_back_evening_reaches_the_pipelines_state():
    """Live 2026-09-26 (Ann Fowler, Sat 18:34): the diary held tonight and
    Sunday back, but the pipeline reads a MERGED state and never saw them, so
    Today didn't ask. _sync now carries ooh_withheld across."""
    src = open("src/app/pipeline.py", encoding="utf-8").read()
    sync = src.split("    def _sync():")[1].split("\n    def ")[0]
    assert 'state["ooh_withheld"] = office_state.get("ooh_withheld") or []' in sync


def test_today_says_tonight_and_the_reply_never_says_closed(execu):
    from datetime import date as _d
    assert scheduling.arrival_words("2026-09-26", "eve", today=_d(2026, 9, 26)) == "tonight"
    assert scheduling.arrival_words("2026-09-27", "am", today=_d(2026, 9, 26)) == "tomorrow morning"
    make, _, _ = execu
    ex, _ = make("ask")
    got = ex("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-27",
                                    "service": "Emergency callout"})
    assert "never say the business is closed" in got["out_of_hours_held_back"]


def test_an_accepted_out_of_hours_offer_is_priced_out_of_hours(execu, monkeypatch):
    """Live 2026-09-26 (Dev, Sunday offered in automatic mode): an offer held no
    price, so accepting the Sunday slot would have left it unpriced."""
    make, inserted, quotes = execu
    ex, state = make("auto")
    offer = {"id": "o1", "booking_ref": "BK-1020", "booking_date": "2026-09-27", "window": "am",
             "status": "offered", "details": {"Service": "Emergency callout",
                                              "Job address": "18 Widcombe Parade"}}
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [dict(offer)])
    monkeypatch.setattr(scheduling.db, "accept_offered_booking",
                        lambda oid, tid, details=None: {**offer, "status": "proposed"})
    got = ex("hold_booking", {"date": "2026-09-27", "window": "am", "service": "Emergency callout",
                              "summary": "Leak", "details": {"Job address": "18 Widcombe Parade"}})
    assert got.get("accepted_offer") and quotes["o1"] == 140.0


def test_a_hold_ending_at_midnight_says_midnight():
    from app import dates
    now = datetime(2026, 9, 26, 19, 16, tzinfo=DIARY_TZ)
    assert dates.deadline_phrase(datetime(2026, 9, 27, 0, 0, tzinfo=DIARY_TZ), now) == "midnight tonight"
    assert dates.deadline_phrase(datetime(2026, 9, 28, 0, 0, tzinfo=DIARY_TZ), now) == "midnight tomorrow"


def test_a_confirmed_booking_settles_todays_out_of_hours_ask(monkeypatch):
    """Real take, 30 Sep 2026: Today still asked "could have call-out on Sunday
    … £140?" for a customer already booked and confirmed for tomorrow. The
    booking answers the question, so the approve-and-send that confirms the
    hold settles the card for that contact (the same swipe the draft-ready
    card gets)."""
    from types import SimpleNamespace
    from app import db as real_db
    settled = []
    monkeypatch.setattr(real_db, "resolve_concierge_notifications",
                        lambda tid, *, kind, url, client=None: settled.append((tid, kind, url)) or 1)
    assert real_db.settle_out_of_hours_ask("t1", "sarah@x.co") == 1
    assert settled == [("t1", "out_of_hours_ask", "/app/conversations/view?contact=sarah%40x.co")]
    assert real_db.settle_out_of_hours_ask("t1", None) == 0          # no contact, nothing to settle

    # And the hold resolver calls it for every booking it just confirmed.
    calls = []
    monkeypatch.setattr(real_db, "settle_out_of_hours_ask", lambda tid, c, client=None: calls.append(c))
    monkeypatch.setattr(real_db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(real_db, "crm_close_promises_for_confirmed_booking", lambda *a, **k: None)

    class _Q:
        def __init__(self, table): self.table_name = table
        def update(self, *a, **k): return self
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def is_(self, *a, **k): return self
        def execute(self):
            if self.table_name == "bookings":
                return SimpleNamespace(data=[{"id": "b1", "contact": "sarah@x.co", "booking_ref": "BK-1001",
                                              "booking_date": "2026-10-01", "window": "am"}])
            return SimpleNamespace(data=[{"thread_id": "th1"}])
    monkeypatch.setattr(real_db, "db", lambda: SimpleNamespace(table=_Q))
    assert real_db.resolve_holds_for_log("log1", "confirmed", "t1") == 1
    assert calls == ["sarah@x.co"]
