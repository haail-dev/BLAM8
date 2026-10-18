"""Today as a notification centre, the arrivals half (Kris 2026-09-21: "we
should be showing more than we are as notifications").

booking_facts now says what is COMING both ways on a stays diary — the arrival
and, separately, the collection — and carries the customer's on-the-day items
("bring the food portioned out") on the day they matter. A stay that began
yesterday still owes you its collection today. Pure, like the deck.

Run: python -m pytest tests/test_deck_arrivals.py -q
"""
from datetime import datetime, timedelta

from app import deck
from app.scheduling import DIARY_TZ

NOW = datetime(2026, 9, 21, 7, 0, tzinfo=DIARY_TZ)


def _row(date, window, status="confirmed", contact="h@x.co", summary="Boarding for Biscuit", **kw):
    return {"id": kw.pop("id", date + window), "booking_date": date, "window": window, "status": status,
            "contact": contact, "summary": summary, **kw}


def _span(hours_after_start, start_clock=None):
    def span(row):
        start = datetime.fromisoformat(row["booking_date"] + "T" + (start_clock or row["window"])).replace(tzinfo=DIARY_TZ)
        return start, start + timedelta(hours=hours_after_start)
    return span


def test_a_stay_owes_you_an_arrival_and_a_collection():
    facts = deck.booking_facts([_row("2026-09-21", "08:30")], {}, NOW, stay_span=_span(56))   # → Wed 16:30
    kinds = [(f["kind"], f["chip"]) for _, f in facts]
    assert kinds == [("booking", "Today 08:30"), ("collection", "Collect · Wed 16:30")]
    assert facts[1][1]["href"] == "/app/diary?date=2026-09-23"


def test_a_stay_that_began_yesterday_still_owes_its_collection_today():
    facts = deck.booking_facts([_row("2026-09-20", "08:30")], {}, NOW, stay_span=_span(33))   # → today 17:30
    assert [(f["kind"], f["chip"]) for _, f in facts] == [("collection", "Collect · Today 17:30")]
    assert facts[0][1]["rank"] < deck.rank_of("bookings", {})                                # imminent: lifted


def test_on_the_day_items_ride_the_arrival_they_belong_to():
    notes = {"h@x.co": ["Bring Biscuit's food portioned out", "Meds labelled with the dose"]}
    today, later = deck.booking_facts(
        [_row("2026-09-21", "08:30"), _row("2026-09-25", "08:30", id="later")], {}, NOW, notes=notes)
    assert today[1]["text"].endswith("· Bring Biscuit's food portioned out · Meds labelled with the dose")
    assert "Bring" not in later[1]["text"]          # not until the day is close


def test_a_whole_day_booking_takes_its_clock_from_the_span():
    """A 'day' row has no clock, and used to sit at 23:59 — after its own
    collection. The diary's span gives it 07:45 → 17:30."""
    facts = deck.booking_facts([_row("2026-09-21", "day", summary="Trial day for Mabel")], {}, NOW,
                               stay_span=_span(9.75, "07:45"))
    assert [(f["kind"], f["chip"]) for _, f in facts] ==         [("booking", "In 45 minutes"), ("collection", "Collect · Today 17:30")]


def test_no_stay_span_means_no_collection_facts_as_before():
    facts = deck.booking_facts([_row("2026-09-21", "08:30")], {}, NOW)
    assert [f["kind"] for _, f in facts] == ["booking"]


def test_an_order_saved_before_records_existed_still_counts():
    """A tenant who ordered the eight topics keeps their order; the new topic
    slots in at its default place rather than voiding the whole setting."""
    old = ["quiet", "promises", "quotes", "money", "bookings", "drafts", "questions", "unhappy"]
    assert deck.clean_order(old) == ["quiet", "promises", "quotes", "records", "money",
                                     "bookings", "drafts", "questions", "unhappy"]
    assert deck.clean_order(["quotes", "quotes"]) is None and deck.clean_order(["nope"]) is None
