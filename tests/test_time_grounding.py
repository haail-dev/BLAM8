"""Times are the customer's to give (K11 rehearsal, Kris 2026-09-23).

Nadia asked for "Friday 6 to Sunday 8 November" and named no time. The agent
filled drop-off 8am and collection 5pm from the opening hours, booked it, and
wrote back "drop-off 8am, collection 5pm" as though she had said so. Dates were
grounded; the clock was not. Now, on a grounded tenant:

  * a stay's drop-off / collection time must be one the customer wrote (or one
    a sent reply of ours stated) — otherwise it is a MISSING DETAIL;
  * being a missing detail, the stay is RESERVED while the agent asks;
  * when their times arrive, confirming the reservation takes THEIR times,
    not the placeholders it was reserved with.

Run: python -m pytest tests/test_time_grounding.py -q
"""
from datetime import date, datetime

from app import grounding as gr
from app import scheduling, slotgrid
from app.scheduling import DIARY_TZ

TODAY = date(2026, 6, 25)
ASK = "Could Bruno come and stay from Tuesday 30 June to Thursday 2 July?"
STAY = {"arrival": "2026-06-30T08:00", "departure": "2026-07-02T17:00",
        "summary": "Boarding for Bruno", "details": {}}


def test_the_customers_times_are_collected_from_every_email_and_our_sent_replies():
    g = gr.build(subject="Bruno", body="Drop off at 9.30am please", today=TODAY, history=[
        {"body_in": "We'll collect about 5pm", "status": "skipped"},
        {"body_in": "", "status": "sent", "draft_reply": "Collection is from 4pm."}])
    assert {"09:30", "17:00", "16:00"} <= g.customer_times


def test_a_time_nobody_gave_is_a_gap_and_a_given_one_is_not():
    g = gr.build(subject="", body=ASK, today=TODAY)
    assert gr.time_gaps(g, STAY) == ["Drop-off time", "Collection time"]
    g2 = gr.build(subject="", body=ASK + " Drop-off 8am, pick-up 5pm.", today=TODAY)
    assert gr.time_gaps(g2, STAY) == []
    assert gr.time_gaps(None, STAY) == []                      # grounding off: nothing changes
    assert gr.time_gaps(g, {"date": "2026-06-30", "window": "am"}) == []   # not a stay


def _executor(monkeypatch, body, offered=None, moved=None):
    from tests.test_stay_executor import _exec
    inserted = []
    offered = offered if offered is not None else []
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: list(offered))
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def accept(bid, tid, details=None, **k):
        for o in offered:
            if o["id"] == bid:
                return {**o, "status": "proposed", "details": details}
        return None
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", accept)
    if moved is not None:
        monkeypatch.setattr(scheduling.db, "set_booking_blocks",
                            lambda bid, sb, ln, tid, client=None: moved.append((bid, sb, ln)))
    execute, state = _exec(monkeypatch, existing=[], inserted=inserted)
    g = gr.build(subject="Bruno", body=body, today=TODAY)
    # rebuild the executor with grounding on, the rest wired as _exec left it
    execute, state = scheduling.make_executor("t1", contact="sarah@x.co", stay=True,
                                              now=datetime(2026, 6, 25, 9, 0, tzinfo=DIARY_TZ),
                                              grounding=g)
    return execute, state, inserted


def test_no_time_given_means_ask_and_reserve_not_book(monkeypatch):
    execute, _, inserted = _executor(monkeypatch, ASK)
    execute("check_availability", {"arrival": STAY["arrival"], "departure": STAY["departure"]})
    out = execute("hold_booking", dict(STAY))
    assert out["error"] == "missing_info" and out["missing"] == ["Drop-off time", "Collection time"]
    assert "Never pick a time for them" in out["message"]
    assert out["reserved"] is True                             # held while she's asked
    assert [r.get("status") for r in inserted] == ["offered"]


def test_times_she_gave_book_as_before(monkeypatch):
    execute, _, inserted = _executor(monkeypatch, ASK + " I'll drop him at 8am and collect at 5pm.")
    execute("check_availability", {"arrival": STAY["arrival"], "departure": STAY["departure"]})
    out = execute("hold_booking", dict(STAY))
    assert out.get("held") is True and inserted[0].get("status") in (None, "proposed")


def test_confirming_the_reservation_takes_her_times_not_the_placeholders(monkeypatch):
    placeholder = {"id": "o1", "booking_ref": "BK-1062", "status": "offered",
                   "booking_date": "2026-06-30", "start_block": slotgrid.block_at("08:00"),
                   "length_blocks": (slotgrid.datetime_to_abs(datetime(2026, 7, 2, 17, 0))
                                     - slotgrid.datetime_to_abs(datetime(2026, 6, 30, 8, 0))),
                   "details": {}}
    moved = []
    execute, state, _ = _executor(monkeypatch, ASK + " Drop-off 9.30am on the Tuesday, collect him 4pm Thursday.",
                                  offered=[placeholder], moved=moved)
    execute("check_availability", {"arrival": "2026-06-30T09:30", "departure": "2026-07-02T16:00"})
    out = execute("hold_booking", {**STAY, "arrival": "2026-06-30T09:30", "departure": "2026-07-02T16:00"})
    assert out.get("held") is True and out.get("was_offered") is True
    want_len = (slotgrid.datetime_to_abs(datetime(2026, 7, 2, 16, 0))
                - slotgrid.datetime_to_abs(datetime(2026, 6, 30, 9, 30)))
    assert moved == [("o1", slotgrid.block_at("09:30"), want_len)]
    assert out["arrival"] == "2026-06-30T09:30" and out["departure"] == "2026-07-02T16:00"
