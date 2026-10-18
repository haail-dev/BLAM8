"""The Concierge's diary answer knows a stay is in on every day it covers.

Live 2026-09-23, kennels rig (Little Paws): "Which dogs are with us on the
13th of October?" → list_bookings(13th..13th) → [] → "Nothing is booked",
while Rufus (Fri 9th 08:00 → Tue 13th 17:00) and Bella (Mon 12th → Mon 26th)
were both in. list_bookings keyed on the day a booking STARTED.
"""
from app import concierge_data, slotgrid

RUFUS = {"id": "b-rufus", "booking_date": "2026-10-09", "window": "day",
         "contact": "meg@x.com", "summary": "Boarding for Rufus", "status": "confirmed",
         "booking_ref": "BK-1017", "start_block": 32, "length_blocks": 4 * 96 + 36}  # 08:00 → Tue 17:00
BELLA = {"id": "b-bella", "booking_date": "2026-10-12", "window": "day",
         "contact": "hannah@x.com", "summary": "Boarding for Bella", "status": "confirmed",
         "booking_ref": "BK-1038", "start_block": 34, "length_blocks": 14 * 96 + 34}
GONE = {"id": "b-gone", "booking_date": "2026-10-05", "window": "day",
        "contact": "x@x.com", "summary": "Boarding for Pip", "status": "confirmed",
        "booking_ref": "BK-1001", "start_block": 32, "length_blocks": 2 * 96}      # left Wed 7th
DAYCARE = {"id": "b-day", "booking_date": "2026-10-13", "window": "am",
           "contact": "d@x.com", "summary": "Daycare for Otis", "status": "confirmed",
           "booking_ref": "BK-1050", "start_block": 32, "length_blocks": 36}      # same day


def _stub(monkeypatch, in_range, before, subjects=None):
    seen = {}

    def search(tid, **k):
        seen["search"] = k
        return list(in_range)

    def running(tid, day, contact=None, client=None):
        seen["into"] = day
        return list(before)
    monkeypatch.setattr(concierge_data.db, "search_bookings", search)
    monkeypatch.setattr(concierge_data.db, "stays_running_into", running)
    monkeypatch.setattr(concierge_data.db, "subjects_for_bookings",
                        lambda tid, ids, client=None: subjects or {})
    return seen


def test_a_stay_that_began_earlier_is_in_on_the_day(monkeypatch):
    seen = _stub(monkeypatch, [DAYCARE], [BELLA, RUFUS, GONE],
                 {"b-bella": [{"name": "Bella"}]})
    out = concierge_data._list_bookings("t1", None, {"date_from": "2026-10-13", "date_to": "2026-10-13"})
    refs = [b["reference"] for b in out["bookings"]]
    # Rufus leaves on the 13th — still in that day; Pip left on the 7th — not.
    assert refs == ["BK-1017", "BK-1038", "BK-1050"]
    assert out["count"] == 3
    assert seen["into"] == "2026-10-13"
    rufus, bella, day = out["bookings"]
    assert rufus["started_before_range"] is True and bella["started_before_range"] is True
    assert rufus["arrives"] == "Fri 09 Oct 2026 08:00" and rufus["leaves"] == "Tue 13 Oct 2026 17:00"
    assert rufus["nights"] == 4 and bella["nights"] == 14
    assert bella["for"] == ["Bella"] and "for" not in rufus
    # A same-day visit is not a stay: no arrives/leaves, and it began in range.
    assert "nights" not in day and "started_before_range" not in day


def test_a_stay_ending_the_moment_the_day_starts_is_not_in(monkeypatch):
    midnight = dict(GONE, length_blocks=(13 - 5) * 96 - 32)   # 5th 08:00 → 13th 00:00
    _stub(monkeypatch, [], [midnight])
    out = concierge_data._list_bookings("t1", None, {"date_from": "2026-10-13"})
    assert out["bookings"] == []
    assert slotgrid.stay_span(midnight)[0] + slotgrid.stay_span(midnight)[1] == slotgrid.abs_block("2026-10-13", 0)


def test_running_stays_only_join_the_live_diary(monkeypatch):
    # cancelled / completed lists are about those bookings, not who's in
    seen = _stub(monkeypatch, [], [RUFUS])
    out = concierge_data._list_bookings("t1", None, {"date_from": "2026-10-13", "status": "cancelled"})
    assert out["bookings"] == [] and "into" not in seen
    # no date: nothing to run into
    seen = _stub(monkeypatch, [], [RUFUS])
    concierge_data._list_bookings("t1", None, {})
    assert "into" not in seen


def test_a_failed_running_read_still_answers_the_range(monkeypatch):
    _stub(monkeypatch, [DAYCARE], [])

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(concierge_data.db, "stays_running_into", boom)
    out = concierge_data._list_bookings("t1", None, {"date_from": "2026-10-13"})
    assert [b["reference"] for b in out["bookings"]] == ["BK-1050"]


def test_the_tool_tells_the_model_how_to_read_a_stay():
    src = open(concierge_data.__file__, encoding="utf-8").read()
    desc = src[src.index('"name": "list_bookings"'):][:900]
    assert "is in on every day it covers" in desc
    assert "started_before_range" in desc and "arrives / leaves / nights" in desc
