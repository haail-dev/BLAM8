"""Promise kinds (Kris 2026-09-21: "the three 'You owe' promises are all bookings
waiting for the day — showing them under 'You owe' makes it feel like pending
work"). The examples are the kennels rig's own open promises that morning.

Run: python -m pytest tests/test_promise_kinds.py -q
"""
import pytest

from app import promise_kinds as pk


def _p(desc, direction="we_owe", due=""):
    return {"description": desc, "direction": direction, "due_hint": due}


@pytest.mark.parametrize("desc,expected", [
    # the diary holds these
    ("Host Biscuit for November boarding (BK-1024) with drop-off 20 November at 8.30am and collection 22 November at 5pm for £61.", "booking"),
    ("Host Biscuit for a meet & greet and trial night (BK-1020) with drop-off 7 October at 1pm and collection 8 October by 9.30am for £30.50.", "booking"),
    ("Mabel's trial day is booked for Monday 21 September, drop-off 07:45 and collection 17:30 (reference BK-1014).", "booking"),
    # …but work still owed on a booking is an obligation
    ("Cancel the original wedding weekend booking BK-1012 in the background.", "obligation"),
    # Money holds this
    ("Hannah will pay the deposit by bank transfer today.", "payment"),
    # on the day: the customer's logistics, never a debt
    ("Bring Biscuit's food portioned out or with a scoop for his stay.", "on_the_day"),
    ("Collect Dottie after the trial night.", "on_the_day"),
    ("Lucy will include Peanut's Thyforon tablet in her food bag for each daycare session.", "on_the_day"),
    ("Daniel must send Mabel's Metacam in labelled with her name, dose and time it is due on daycare days.", "on_the_day"),
    ("Anjali will drop Milo off at 8:30am on Saturdays for every-other-weekend boarding starting 10 October.", "on_the_day"),
    # a statement of something already done
    ("Daniel will send Mabel's vaccination card (attached as mabel-vaccination-card.pdf).", "done"),
    ("Hannah has provided her mobile, home address, husband Tom's mobile and vet details for the host's records.", "done"),
    # the real ones
    ("We will lock in Mondays, Wednesdays and Thursdays as Mabel's regular slots once the trial goes well.", "obligation"),
    ("Lucy to confirm whether Peanut has previously visited Little Paws.", "obligation"),
    ("Post daily photos on Facebook so Priya can keep an eye on Lola and Bean during their stay.", "on_the_day"),
    ("Owen will provide any required documentation or information we request to proceed with the booking.", "obligation"),
    ("A colleague will follow up about whether the team can administer Metacam.", "obligation"),
    ("", "obligation"),
])
def test_each_promise_lands_in_its_kind(desc, expected):
    assert pk.kind(_p(desc)) == expected


def test_obligations_is_the_you_owe_they_owe_list_and_annotate_writes_the_kind():
    rows = [_p("Host Biscuit for BK-1024."), _p("Bring the food."), _p("Confirm the dates with Lucy.", "they_owe")]
    assert [r["description"] for r in pk.obligations(rows)] == ["Confirm the dates with Lucy."]
    assert [r["description"] for r in pk.on_the_day(rows)] == ["Bring the food."]
    assert [r["kind"] for r in pk.annotate(rows)] == ["booking", "on_the_day", "obligation"]


from datetime import date as _date

TODAY = _date(2026, 9, 21)


@pytest.mark.parametrize("desc,direction,due,expected", [
    # yours, unconditional, undated → do it today
    ("Cancel the original wedding weekend booking BK-1012 in the background.", "we_owe", "", True),
    ("Check vaccination card expiry dates against the November stay and alert the owner.", "we_owe", "", True),
    ("Flag Bramble's hat sensitivity to all staff at Little Paws.", "we_owe", "", False),   # a care note: on the day
    # waiting on the customer is not your to-do
    ("Ben to provide Scout's vaccination expiry date.", "they_owe", "", False),
    # conditional on something that hasn't happened
    ("Book Poppy in for regular Tuesdays once the meet & greet is completed.", "we_owe", "", False),
    ("Hold both spaces for Coco as soon as vaccination is confirmed.", "we_owe", "", False),
    ("We will lock in Mondays as Mabel's regular slots once the trial goes well.", "we_owe", "from there", False),
    # dated: soon shows, far off waits
    ("We will confirm the meet & greet booking for Tuesday 29 September.", "we_owe", "Tuesday 29 September", False),
    ("Ring the vet back.", "we_owe", "tomorrow", True),
    ("Lock in the trial night and the stay.", "we_owe", "straight away", True),
    # care notes are on-the-day, not obligations at all
    ("We will administer Mabel's Metacam with her breakfast on daycare days.", "we_owe", "", False),
    ("Ensure Poppy receives a lunch (Lily's Kitchen puppy food) on her daycare days.", "we_owe", "from Tuesday 29 September", False),
])
def test_what_counts_as_a_thing_to_do_today(desc, direction, due, expected):
    assert pk.actionable_today({"description": desc, "direction": direction, "due_hint": due}, TODAY) is expected


def test_due_dates_read_from_the_hint():
    assert pk.due_date({"due_hint": "Tuesday 29 September"}, TODAY) == _date(2026, 9, 29)
    assert pk.due_date({"due_hint": "after 29 September meet & greet"}, TODAY) == _date(2026, 9, 29)
    assert pk.due_date({"due_hint": "Friday 20 November at 8.30am"}, TODAY) == _date(2026, 11, 20)
    assert pk.due_date({"due_hint": "3 January"}, TODAY) == _date(2027, 1, 3)        # long past → next year
    assert pk.due_date({"due_hint": "tomorrow"}, TODAY) == _date(2026, 9, 22)
    assert pk.due_date({"due_hint": "from there"}, TODAY) is None


def test_what_we_do_during_the_visit_is_on_the_day():
    assert pk.kind(_p("Post daily photos on Facebook so Priya can keep an eye on Lola and Bean during their stay.")) == "on_the_day"
    assert pk.kind(_p("Send Hannah a photo or two while he's with us.")) == "on_the_day"


def test_a_diary_promise_opens_the_add_form_prefilled():
    """Kris 2026-09-21: "because this task is for the Diary, we should have a
    route highlighted to complete the task" — click, and the Add is ready."""
    daniel = {"id": "c1", "name": "Daniel Okafor", "email": "d@x.co"}
    p = {"id": "p1", "direction": "we_owe",
         "description": "We will lock in Mondays, Wednesdays and Thursdays as Mabel's regular slots once the trial goes well.",
         "due_hint": "from there"}
    from datetime import date as _date
    r = pk.route(p, contact=daniel, pets=["Mabel"], today=_date(2026, 9, 21))
    assert r["kind"] == "diary" and r["short"] == "Diary"
    assert r["label"] == "Add Mabel's regular days in the Diary"
    assert r["href"].startswith("/app/diary?date=2026-09-23&add=1")      # the first named day after today (a Monday)
    for bit in ("contact=d%40x.co", "subjects=Mabel", "repeat=weekly", "occurrences=12",
                "weekdays=mon%2Cwed%2Cthu", "promise=p1", "summary=Daycare+for+Mabel"):
        assert bit in r["href"], bit
    assert pk.weekdays_in("Tuesdays and Fridays from the 29th") == ["tue", "fri"]


def test_money_reply_and_customer_promises_route_or_not():
    c = {"id": "c1", "name": "Meg Ashworth", "email": "m@x.co"}
    assert pk.route({"direction": "we_owe", "description": "Refund Meg for the cancelled weekend."}, contact=c)["kind"] == "money"
    r = pk.route({"direction": "we_owe", "description": "Send Meg the photos from the weekend."}, contact=c)
    assert r["kind"] == "reply" and r["label"] == "Reply to Meg" and "contact=m%40x.co" in r["href"]
    assert pk.route({"direction": "they_owe", "description": "Meg to send the vaccination card."}, contact=c) is None
    assert pk.route({"direction": "we_owe", "description": "Host Biscuit for BK-1024."}, contact=c) is None    # a booking, not a promise
