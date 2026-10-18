"""The contacts logbook (Kris 2026-09-21: "I want to make the CRM page look
better, more like a contact logbook").

A name-and-address ledger said nothing about the people on it. crm.logbook
shapes the contacts page from reads handed in: grouped by when you last spoke,
each entry a person with initials tinted by mood, the dependants their
bookings are about, the last thing that happened, and the facts that need an
eye — the next stay, what they owe, promises open. Pure and offline.

Run: python -m pytest tests/test_crm_logbook.py -q
"""
from datetime import date

from app import crm

TODAY = date(2026, 9, 21)


def _contact(cid, name, email, days, **kw):
    return {"id": cid, "name": name, "email": email, "days_since_last": days,
            "stale": days is not None and days > 14, **kw}


def test_groups_by_when_you_last_spoke_in_logbook_order():
    book = crm.logbook([
        _contact("a", "Hannah Price", "h@x.co", 0),
        _contact("b", "Meg Ashworth", "m@x.co", 1),
        _contact("c", "Priya", "p@x.co", 4),
        _contact("d", "Rob", "r@x.co", 20),
        _contact("e", "Old Timer", "o@x.co", 90),
        _contact("f", None, "quiet@x.co", None),
    ], today=TODAY)
    assert [g["label"] for g in book] == ["Today", "Yesterday", "This week", "This month", "Earlier"]
    assert [e["title"] for e in book[-1]["entries"]] == ["Old Timer", "quiet@x.co"]
    assert [e["when"] for g in book for e in g["entries"]] == \
        ["today", "yesterday", "4d ago", "20d ago", "90d ago", "—"]


def test_initials_and_mood_tint():
    book = crm.logbook([
        _contact("a", "Hannah Price", "h@x.co", 0, sentiment="positive"),
        _contact("b", None, "meg.ashworth@x.co", 0, sentiment="frustrated"),
        _contact("c", "Priya", "p@x.co", 0, sentiment=None),
    ], today=TODAY)
    entries = book[0]["entries"]
    assert [(e["initials"], e["tone"]) for e in entries] == \
        [("HP", "emerald"), ("M", "rose"), ("P", "slate")]


def test_the_dogs_the_last_line_and_the_facts():
    book = crm.logbook(
        [_contact("a", "Hannah Price", "H@x.co", 0)],
        dependants=[{"contact_id": "a", "name": "Biscuit", "data": {"breed": "Cockapoo"}},
                    {"contact_id": "a", "name": "Crumb", "data": {}},
                    {"contact_id": "zz", "name": "Someone else's", "data": {}}],
        bookings=[{"contact": "h@x.co", "booking_date": "2026-11-20", "status": "confirmed"},
                  {"contact": "h@x.co", "booking_date": "2026-10-07", "status": "confirmed"},
                  {"contact": "h@x.co", "booking_date": "2026-09-01", "status": "confirmed"},   # past
                  {"contact": "h@x.co", "booking_date": "2026-09-25", "status": "cancelled"}],  # dead
        invoices=[{"customer_email": "h@x.co", "status": "sent", "total": 30.51, "amount_paid": 0,
                   "due_date": "2026-10-05"},
                  {"customer_email": "h@x.co", "status": "sent", "total": 15.25, "amount_paid": 0,
                   "due_date": "2026-10-05"},
                  {"customer_email": "h@x.co", "status": "paid", "total": 99, "amount_paid": 99,
                   "due_date": "2026-09-01"}],
        promises=[{"contact_id": "a", "direction": "we_owe"},
                  {"contact_id": "a", "direction": "they_owe"},
                  {"contact_id": "a", "direction": "they_owe"}],
        activities=[{"contact_id": "a", "direction": "inbound", "summary": "Wants a November weekend too"},
                    {"contact_id": "a", "direction": "outbound", "summary": "older, must not win"}],
        today=TODAY)
    e = book[0]["entries"][0]
    assert e["pets"] == ["Biscuit · Cockapoo", "Crumb"]
    assert e["last"] == {"arrow": "←", "text": "Wants a November weekend too"}
    assert [f["text"] for f in e["facts"]] == ["Next 7th Oct 26", "Owes £45.76", "you owe 1 · they owe 2"]
    assert [f["tone"] for f in e["facts"]] == ["emerald", "amber", "slate"]


def test_an_overdue_invoice_turns_the_owed_fact_rose_and_says_how_late():
    book = crm.logbook(
        [_contact("m", "Meg Ashworth", "m@x.co", 1)],
        invoices=[{"customer_email": "m@x.co", "status": "sent", "total": 30.5, "amount_paid": 0,
                   "due_date": "2026-09-10"}],
        today=TODAY)
    (f,) = book[0]["entries"][0]["facts"]
    assert f == {"tone": "rose", "text": "Owes £30.50 · 11d late"}


def test_a_bare_contact_has_no_lines_but_still_a_title():
    book = crm.logbook([_contact("q", None, "quiet@x.co", None)], today=TODAY)
    e = book[0]["entries"][0]
    assert e["title"] == "quiet@x.co" and e["pets"] == [] and e["last"] is None and e["facts"] == []


def test_a_long_last_line_is_clamped():
    book = crm.logbook([_contact("a", "A", "a@x.co", 0)],
                       activities=[{"contact_id": "a", "direction": "outbound", "summary": "x" * 200}],
                       today=TODAY)
    text = book[0]["entries"][0]["last"]["text"]
    assert len(text) == 90 and text.endswith("…")


def test_the_last_line_is_what_was_last_said_not_the_bookkeeping_after_it():
    book = crm.logbook([_contact("a", "Meg", "m@x.co", 0), _contact("b", "Quiet", "q@x.co", 0)],
                       activities=[{"contact_id": "a", "activity_type": "invoice_paid", "direction": "inbound",
                                    "summary": "£30.51 received (marked paid by you (other))"},
                                   {"contact_id": "a", "activity_type": "promise_cleared",
                                    "summary": "cleared by your reply — …"},
                                   {"contact_id": "a", "activity_type": "email", "direction": "inbound",
                                    "summary": "Wants to extend boarding to the Tuesday"},
                                   {"contact_id": "b", "activity_type": "invoice", "direction": "outbound",
                                    "summary": "£15.25, due 5th Oct 26"}],
                       today=TODAY)
    a, b = book[0]["entries"]
    assert a["last"] == {"arrow": "←", "text": "Wants to extend boarding to the Tuesday"}
    assert b["last"] == {"arrow": "→", "text": "£15.25, due 5th Oct 26"}     # nothing said: the bookkeeping stands in


def test_a_to_z_groups_by_initial_with_the_unlettered_last():
    book = crm.logbook([
        _contact("a", "Hannah Price", "h@x.co", 0),
        _contact("b", "meg ashworth", "m@x.co", 1),          # case-blind
        _contact("c", None, "07700 900123", 4),               # a number, not a letter
        _contact("d", "Anjali", "an@x.co", 20),
        _contact("e", None, "zed@x.co", 90),
    ], order="name", today=TODAY)
    assert [g["label"] for g in book] == ["A", "H", "M", "Z", "#"]
    assert [e["title"] for g in book for e in g["entries"]] == \
        ["Anjali", "Hannah Price", "meg ashworth", "zed@x.co", "07700 900123"]
