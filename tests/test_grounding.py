"""Grounded booking — the model may only act on a date it can point to
(src/app/grounding.py, src/app/booking_extract.py, scheduling.make_executor,
docs/plan-grounded-booking.md).

The case this pins (K7 shoot, 2026-09-22): the customer wrote `8am is fine
thanks`, the diary's open hold for him was 10–13 November (BK-1040), and the
draft held 3–6 November — dates in no email, no reply and no booking. With the
gate on, that hold is refused before any capacity is touched, and the refusal
names BK-1040.

Also pinned: the parser on the exact strings from the shoot (the ones
dateparser read as the year 2113), a time is never a date, provenance per
date, a hold needs a check first, the citation rule in the extractor, and —
the one that protects every other tenant — the flag OFF is byte-identical.
And (second live draft, same day): checking a date the customer never
named marks it checked, never bookable — the diary echoing the model's own
span is not a source.

Run: python -m pytest tests/test_grounding.py -q
"""
from __future__ import annotations
from tests.booking_fixtures import carried_config

from datetime import date, datetime

import pytest

from app import booking_extract, grounding as G, intake, pipeline, scheduling
from app.scheduling import DIARY_TZ

TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=DIARY_TZ)
GRAHAM = "business.blam8+graham.pryce@gmail.com"


# ── the parser, on the real strings ─────────────────────────────────────────
def _iso(ms):
    return sorted({d.isoformat() for m in ms for d in m.dates})


@pytest.mark.parametrize("text,want", [
    ("any space for bramble 3rd-6th nov?", ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]),
    ("any space for bramble 10th-13th nov?", ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]),
    ("Boarding for Bramble (collie) 10–13 November", ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]),
    ("Could we book Bramble in from Tuesday 3rd to Friday 6th November?",
     ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]),
    ("Friday 23 October to Friday 30 October", [f"2026-10-{d}" for d in range(23, 31)]),
    ("Meet and greet at 1pm on Thursday 8 October", ["2026-10-08"]),
    ("2026-11-10T08:00 to 2026-11-13T17:00", ["2026-11-10", "2026-11-13"]),
    ("Thursday or Friday would suit", ["2026-09-24", "2026-09-25"]),
])
def test_the_shapes_people_write(text, want):
    assert _iso(G.mentions(text, TODAY)) == want


def test_a_time_is_never_a_date():
    ms = G.mentions("8am is fine thanks", TODAY)
    assert ms == []
    assert G.times_in("8am is fine thanks") == ["08:00"]
    assert G.times_in("collection at 5pm, or 17:30") == ["17:00", "17:30"]


def test_the_quote_is_the_exact_span():
    (m,) = G.mentions("any space for bramble 10th-13th nov?", TODAY)
    assert m.quote == "10th-13th nov"


def test_a_range_is_one_mention_not_a_stray_day():
    ms = G.mentions("3rd-6th nov please", TODAY)
    assert len(ms) == 1 and len(ms[0].dates) == 4


def test_year_rolls_forward_not_back():
    # Written in September: "3 January" is NEXT year, "20 September" (two days
    # ago) is this month, not next year.
    assert _iso(G.mentions("3 January", TODAY)) == ["2027-01-03"]
    assert _iso(G.mentions("20 September", TODAY)) == ["2026-09-20"]
    assert _iso(G.mentions("6 nov 2027", TODAY)) == ["2027-11-06"]


def test_a_bare_day_of_month_is_left_to_the_extractor():
    assert G.mentions("see you on the 12th", TODAY) == []


# ── bookings → dates ────────────────────────────────────────────────────────
def test_a_stay_row_covers_its_whole_span():
    # 10 Nov 08:00 → 13 Nov 17:00 on the grid: start block 32, 3 days + 9h
    row = {"booking_date": "2026-11-10", "start_block": 32, "length_blocks": 324,
           "status": "proposed", "booking_ref": "BK-1040"}
    assert G.booking_dates(row) == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def test_a_row_without_grid_columns_is_its_booking_date():
    assert G.booking_dates({"booking_date": "2026-10-23", "status": "confirmed"}) == ["2026-10-23"]


# ── the allowed set, with provenance ────────────────────────────────────────
BK1040 = {"id": "b-1040", "booking_ref": "BK-1040", "booking_date": "2026-11-10",
          "start_block": 32, "length_blocks": 324, "status": "proposed",
          "summary": "Boarding for Bramble (collie) 10–13 November", "contact": GRAHAM}


def test_round_five_names_no_dates_and_the_open_hold_is_the_only_thing_in_play():
    g = G.build(subject="Re: Bramble again", body="8am is fine thanks",
                history=[], existing_bookings=[BK1040], today=TODAY)
    assert g.dates == {"2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"}
    assert g.why["2026-11-10"] == ["BK-1040 (proposed)"]
    assert [b["booking_ref"] for b in g.open_holds] == ["BK-1040"]
    assert g.times == ["08:00"]


def test_every_source_is_cited():
    hist = [{"body_in": "any space for bramble 10th-13th nov?", "status": "sent",
             "draft_reply": "Bramble is now booked in for 10–13 November"},
            {"body_in": "", "status": "pending", "draft_reply": "unsent draft says 20 December"}]
    g = G.build(body="8am is fine thanks", history=hist, existing_bookings=[BK1040], today=TODAY)
    assert "an earlier email: '10th-13th nov'" in g.why["2026-11-10"]
    assert "our sent reply: '10–13 November'" in g.why["2026-11-10"]
    assert "BK-1040 (proposed)" in g.why["2026-11-10"]
    # an UNSENT draft is not a source — nobody approved it
    assert "2026-12-20" not in g.dates


def test_a_cancelled_booking_grounds_nothing():
    g = G.build(body="hi", existing_bookings=[{**BK1040, "status": "cancelled"}], today=TODAY)
    assert g.dates == set() and g.open_holds == []


# ── the gate ────────────────────────────────────────────────────────────────
def _g(**kw):
    return G.build(today=TODAY, **kw)


def test_the_invented_hold_is_refused_and_the_refusal_names_the_open_hold():
    g = _g(body="8am is fine thanks", existing_bookings=[BK1040])
    out = G.check(g, "hold_booking", {"arrival": "2026-11-03T08:00", "departure": "2026-11-06T17:00"})
    assert out["error"] == "missing_info" and out["reason"] == "ungrounded_date"
    assert out["ungrounded"] == ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    assert "BK-1040" in out["message"] and "needs NO new hold" in out["message"]
    assert "Ask the customer" in out["message"]


def test_a_grounded_hold_still_needs_a_check_this_draft():
    g = _g(body="any space for bramble 10th-13th nov?")
    out = G.check(g, "hold_booking", {"arrival": "2026-11-10T08:00", "departure": "2026-11-13T17:00"})
    assert out["error"] == "check_first" and out["unchecked"][0] == "2026-11-10"
    G.add_checked(g, "check_availability", {"available": True, "nights": 3,
                                            "arrival": "2026-11-10T08:00", "departure": "2026-11-13T17:00"})
    assert G.check(g, "hold_booking", {"arrival": "2026-11-10T08:00", "departure": "2026-11-13T17:00"}) is None


def test_what_the_diary_returned_free_is_grounded_even_if_the_customer_never_wrote_it():
    # a period ask: "next week?" → check_availability names days → those are bookable
    g = _g(body="what have you got next week?")
    G.add_checked(g, "check_availability", {"available": [{"date": "2026-09-29", "window": "am"},
                                                          {"date": "2026-09-30", "window": "pm"}]})
    assert G.check(g, "offer_slots", {"slots": [{"date": "2026-09-29", "window": "am"}]}) is None
    out = G.check(g, "offer_slots", {"slots": [{"date": "2026-10-02", "window": "am"}]})
    assert out["reason"] == "ungrounded_date" and out["ungrounded"] == ["2026-10-02"]


NADIA = "business.blam8+nadia.kowalski@gmail.com"
# BK-1016: Bruno's confirmed 17-19 October stay on the rig (block 32 = 08:00,
# 2 nights + the day = 3 days x 4 blocks/hr... the grid says 17th to 19th).
BK1016 = {"id": "b-1016", "booking_ref": "BK-1016", "booking_date": "2026-10-17",
          "start_block": 32, "length_blocks": 228, "status": "confirmed",
          "summary": "Boarding for Bruno (boxer) 17-19 October", "contact": NADIA}


def test_checking_a_date_nobody_named_does_not_make_it_bookable():
    # live 2026-09-22 11:51: "8am drop off and 5pm pick up thanks" answered an
    # email asking for 24th-26th nov; the model checked 8 Oct out of nowhere,
    # the diary said free, and the first build let that echo ground the hold.
    hist = [{"body_in": "any space for bruno 24th-26th nov?", "status": "pending", "draft_reply": "unsent"}]
    g = _g(body="8am drop off and 5pm pick up thanks", history=hist, existing_bookings=[BK1016])
    assert "2026-11-24" in g.dates and "2026-11-26" in g.dates and "2026-10-08" not in g.dates
    free = {"available": True, "nights": 0, "arrival": "2026-10-08T08:00", "departure": "2026-10-08T17:00"}
    assert G.check(g, "check_availability", {"arrival": "2026-10-08T08:00", "departure": "2026-10-08T17:00"}) is None
    G.add_checked(g, "check_availability", free)
    assert "2026-10-08" in g.checked and "2026-10-08" not in g.dates
    out = G.check(g, "hold_booking", {"arrival": "2026-10-08T08:00", "departure": "2026-10-08T17:00"})
    assert out["reason"] == "ungrounded_date" and out["ungrounded"] == ["2026-10-08"]
    assert "Checking a date does not make it bookable" in out["message"]
    assert "24 Nov" in out["message"]
    # the check's own result says so, a call earlier than the refusal
    note = G.check_note(g, "check_availability", free)
    assert note.startswith("8 Oct is not a date this customer gave") and "24 Nov" in note
    # and it reads as a span, not as an enumeration (the ⓘ has to be legible)
    assert "17 Oct to 19 Oct" in note
    # ...and a grounded span carries no such line
    ok = {"available": True, "nights": 2, "arrival": "2026-11-24T08:00", "departure": "2026-11-26T17:00"}
    assert G.check_note(g, "check_availability", ok) is None
    G.add_checked(g, "check_availability", ok)
    assert G.check(g, "hold_booking", {"arrival": "2026-11-24T08:00", "departure": "2026-11-26T17:00"}) is None
    # the panel shows the wandering even though the gate held
    assert "checked but not on record (never bookable): 8 Oct" in G.note(g)


def test_a_whole_month_checked_reads_as_one_span_not_thirty_days():
    # live 2026-09-22: "a few nights in November" → the model checked 1–30 Nov
    # in one call, and the ⓘ printed thirty dates. Unreadable on screen.
    g = _g(body="do you have space for ziggy for a few nights in november?")
    assert g.dates == set()
    G.add_checked(g, "check_availability", {"available": True, "nights": 29,
                                            "arrival": "2026-11-01T08:00", "departure": "2026-11-30T17:00"})
    line = G.note(g)
    assert "checked but not on record (never bookable): 1 Nov to 30 Nov" in line
    assert "2 Nov" not in line and len(line) < 260
    out = G.check(g, "hold_booking", {"arrival": "2026-11-01T08:00", "departure": "2026-11-30T17:00"})
    assert "REFUSED: 1 Nov to 30 Nov appears in no email" in out["message"]
    assert len(out["ungrounded"]) == 30           # the data stays exact
    # a gap is still two spans
    g2 = _g(body="hi")
    G.add_checked(g2, "check_availability", {"available": [{"date": "2026-11-01"}, {"date": "2026-11-02"},
                                                           {"date": "2026-11-09"}]})
    assert "1 Nov to 2 Nov" in G.note(g2) and "9 Nov" in G.note(g2)


def test_with_no_date_on_record_there_is_no_diary_work_at_all():
    """Kris 2026-09-22, watching a take: "It should be able to do everything in
    the right order, the first time over." Refusing an ungrounded hold AFTER
    the model reaches for it teaches it nothing — it checked the same span
    twice, lost the tool, then invented BK-UNKNOWN to cancel. With nothing to
    act on it gets no diary tools and the prompt stops ordering it to act."""
    assert G.no_dates(_g(body="8am drop off and 5pm pick up thanks")) is True
    assert G.no_dates(_g(body="do you have space in november?")) is True

    # one date anywhere — their own email, an earlier one, or a live booking —
    # and the diary is back in play. A live booking grounds its own dates, so
    # an empty set also means there is nothing to cancel or move.
    for kw in ({"body": "any space 10th-13th nov?"},
               {"body": "8am is fine", "existing_bookings": [BK1040]},
               {"body": "8am", "history": [{"body_in": "10th-13th nov?", "status": "sent",
                                            "draft_reply": ""}]}):
        assert G.no_dates(_g(**kw)) is False

    # and the flag off changes nothing for anybody else
    off = _g(body="8am drop off and 5pm pick up thanks")
    off.on = False
    assert G.no_dates(off) is False
    assert G.no_dates(None) is False


def test_the_prompt_stops_telling_it_to_act_when_it_cannot():
    from app import agent
    cfg = {"business_name": "Little Paws", "auto_send": False}
    pushy = agent.build_system_prompt(cfg, scheduling=True, today=TODAY)
    assert "Do any checking NOW with the tools" in pushy

    quiet = agent.build_system_prompt(cfg, scheduling=True, today=TODAY, no_dates=True)
    assert "Do any checking NOW with the tools" not in quiet
    assert "NO diary tools on this reply" in quiet
    assert "Ask which dates they mean" in quiet
    assert "Never invent a booking reference" in quiet
    # nothing was held, so nothing is confirmed by approving
    assert "confirms the held booking" not in quiet


def test_the_panel_groups_consecutive_days_that_share_a_source():
    g = _g(body="8am", existing_bookings=[BK1016])
    line = G.note(g)
    assert "17 Oct to 19 Oct ← BK-1016 (confirmed)" in line
    assert "18 Oct" not in line


def test_a_span_the_diary_called_full_is_refused_as_full_not_as_unchecked():
    """Live 2026-09-22, staging K4: the customer asked for a weekend that was
    genuinely full. The model checked (full), checked the alternative, then
    tried to hold the original — and was told "call check_availability BEFORE
    holding", which it had just done. So it checked again, hit the repeat
    guard, lost the tool, and never offered the alternative it was holding."""
    g = _g(body="could we book poppy in from 17th-19th October?")
    assert {"2026-10-17", "2026-10-18", "2026-10-19"} <= g.dates

    G.add_checked(g, "check_availability", {"available": False,
                                            "arrival": "2026-10-17T08:00",
                                            "departure": "2026-10-19T17:00"})
    out = G.check(g, "hold_booking", {"arrival": "2026-10-17T08:00",
                                      "departure": "2026-10-19T17:00"})
    assert out["error"] == "full" and out["reason"] == "already_full"
    assert "17 Oct to 19 Oct is full" in out["message"]
    assert "offer the nearest opening" in out["message"]
    assert "check_availability" not in out["message"]      # never send it round again

    # the alternative it was given is checkable and holdable as normal, and
    # checking it does not un-ground the full dates
    G.add_checked(g, "check_availability", {"available": True, "nights": 2,
                                            "arrival": "2026-10-19T07:00",
                                            "departure": "2026-10-21T16:00"})
    assert G.check(g, "hold_booking", {"arrival": "2026-10-17T08:00",
                                       "departure": "2026-10-19T17:00"})["reason"] == "already_full"

    # ...and a span that later comes back FREE is bookable again — a cancellation
    # mid-draft must not leave the dates permanently shut
    g2 = _g(body="17th-19th October please")
    G.add_checked(g2, "check_availability", {"available": False, "arrival": "2026-10-17T08:00",
                                             "departure": "2026-10-19T17:00"})
    G.add_checked(g2, "check_availability", {"available": True, "nights": 2,
                                             "arrival": "2026-10-17T08:00",
                                             "departure": "2026-10-19T17:00"})
    assert G.check(g2, "hold_booking", {"arrival": "2026-10-17T08:00",
                                        "departure": "2026-10-19T17:00"}) is None


def test_the_diary_s_own_counter_offer_is_bookable():
    """Live 2026-09-22, the K4 take. The weekend was full, the check returned
    the nearest opening, the model tried to hold THAT — and the gate refused
    it: "20 Oct to 21 Oct appears in no email from this customer." True, and
    beside the point: the business proposed those dates, not the model. The
    agent hedged ("would different dates work for you?") instead of offering
    what it had been handed."""
    g = _g(body="could we book dottie in from Saturday 17th to Monday 19th October?")
    G.add_checked(g, "check_availability", {"available": False,
                                            "arrival": "2026-10-17T08:00",
                                            "departure": "2026-10-19T17:00",
                                            "nearest_arrival": "2026-10-19T07:00",
                                            "nearest_departure": "2026-10-21T16:00"})
    # the weekend they asked for is still shut, and still shut for the right reason
    assert G.check(g, "hold_booking", {"arrival": "2026-10-17T08:00",
                                       "departure": "2026-10-19T17:00"})["reason"] == "already_full"
    # ...and the alternative the DIARY named is GROUNDED — the agent may write
    # it into the reply, which is what it could not do before. Whether it may
    # BOOK it is a separate question, answered later the same day and the other
    # way: see test_a_date_we_suggested_may_be_offered_but_never_held. The
    # refusal here is "they have not agreed", never "you cannot point to it".
    assert "2026-10-20" in g.dates and "2026-10-21" in g.dates
    assert G.check(g, "hold_booking", {"arrival": "2026-10-19T07:00",
                                       "departure": "2026-10-21T16:00"})["reason"] == "not_agreed"
    assert G.check(g, "offer_slots", {"slots": [{"date": "2026-10-20"}]}) is None
    assert "the diary offered it as the nearest opening" in G.note(g)

    # the model still cannot launder a date of its OWN choosing through a check
    g2 = _g(body="a few nights in november?")
    G.add_checked(g2, "check_availability", {"available": True, "nights": 2,
                                             "arrival": "2026-11-03T08:00",
                                             "departure": "2026-11-05T17:00"})
    assert G.check(g2, "hold_booking", {"arrival": "2026-11-03T08:00",
                                        "departure": "2026-11-05T17:00"})["reason"] == "ungrounded_date"


def test_a_date_we_suggested_may_be_offered_but_never_held():
    """Kris, 2026-09-22, watching the K4 take: "the Agent already held the
    dates... once i clicked send the booking was confirmed. Before the customer
    even knows the original dates aren't doable."

    He is right and the damage is real: approving a reply CONFIRMS a proposed
    hold (db.resolve_holds_for_log), so Jess — who asked for 17-19 October and
    was told it was full — was confirmed into 19-21 October one second after
    send. A date the customer chose may be held. A date WE chose may only be
    offered."""
    g = _g(body="could we book dottie in from Saturday 17th to Monday 19th October?")
    G.add_checked(g, "check_availability", {"available": False,
                                            "arrival": "2026-10-17T08:00",
                                            "departure": "2026-10-19T17:00",
                                            "nearest_arrival": "2026-10-19T07:00",
                                            "nearest_departure": "2026-10-21T16:00"})
    assert G.chose_it_ourselves(g, "2026-10-20") is True
    # 19 Oct is in BOTH — they named it, so it is theirs
    assert G.chose_it_ourselves(g, "2026-10-19") is False
    assert G.chose_it_ourselves(g, "2026-10-17") is False

    out = G.check(g, "hold_booking", {"arrival": "2026-10-19T07:00",
                                      "departure": "2026-10-21T16:00"})
    assert out["error"] == "not_agreed" and out["suggested"] == ["2026-10-20", "2026-10-21"]
    assert "not a date this customer asked for" in out["message"]
    assert "offer_stay" in out["message"] and "ask whether this suits" in out["message"]

    # moving a booking onto our suggestion is the same wrong
    assert G.check(g, "move_booking", {"booking_ref": "BK-1009",
                                       "new_date": "2026-10-20"})["reason"] == "not_agreed"

    # ...but OFFERING it is precisely what we want, and stays allowed: an
    # offered row survives the send un-confirmed and expires on its own
    assert G.check(g, "offer_slots", {"slots": [{"date": "2026-10-20"}]}) is None

    # and once the customer says yes, it is theirs and holdable
    g2 = _g(body="19th to 21st October works, thanks")
    G.add_checked(g2, "check_availability", {"available": True, "nights": 2,
                                             "arrival": "2026-10-19T07:00",
                                             "departure": "2026-10-21T16:00"})
    assert G.check(g2, "hold_booking", {"arrival": "2026-10-19T07:00",
                                        "departure": "2026-10-21T16:00"}) is None


def test_accepting_your_own_offer_needs_no_fresh_check():
    """Live 2026-09-22: the customer wrote "Monday to Wednesday is perfect"
    against her own offered BK-1049, and this gate demanded she be checked for
    first. So the model checked, the diary counted her own offer against her,
    said full, and the reply told her the slot had been lost.

    Accepting what we reserved is not a new booking. The executor's acceptance
    path flips that row in place — it just never got to run."""
    OFFER = {"id": "b-1049", "booking_ref": "BK-1049", "booking_date": "2026-10-19",
             "start_block": 28, "length_blocks": 228, "status": "offered",
             "summary": "Boarding for Dottie"}
    g = _g(body="Monday to Wednesday is perfect, thanks.", existing_bookings=[OFFER])
    assert [b["booking_ref"] for b in g.open_holds] == ["BK-1049"]
    assert G.check(g, "hold_booking", {"arrival": "2026-10-19T07:00",
                                       "departure": "2026-10-21T16:00"}) is None

    # a span they hold NOTHING for is gated exactly as before
    assert G.check(g, "hold_booking", {"arrival": "2026-11-02T07:00",
                                       "departure": "2026-11-04T16:00"})["reason"] == "ungrounded_date"
    # A span that merely OVERLAPS their hold is not their hold, so the bypass
    # must not apply. Here the extra days are not named anywhere either, so it
    # is refused harder than check_first — which is the right answer; what
    # matters is that it is refused at all.
    g2 = _g(body="the 19th to the 23rd October then", existing_bookings=[OFFER])
    out = G.check(g2, "hold_booking", {"arrival": "2026-10-19T07:00",
                                       "departure": "2026-10-23T16:00"})
    assert out is not None, "only an exact cover is an acceptance"
    assert out["reason"] in ("check_first", "ungrounded_date")


def test_a_checked_but_unnamed_date_is_not_a_bookable_one_for_move_either():
    g = _g(body="8am is fine thanks", existing_bookings=[BK1040])
    G.add_checked(g, "check_availability", {"available": True, "nights": 3,
                                            "arrival": "2026-11-17T08:00", "departure": "2026-11-20T17:00"})
    out = G.check(g, "move_booking", {"booking_ref": "BK-1040", "new_date": "2026-11-17", "new_window": "day"})
    assert out["reason"] == "ungrounded_date"


def test_move_booking_is_gated_on_its_new_date():
    g = _g(body="can we move it to 12th November?")
    assert G.check(g, "move_booking", {"booking_ref": "BK-1040", "new_date": "2026-11-12", "new_window": "day"}) is None
    out = G.check(g, "move_booking", {"booking_ref": "BK-1040", "new_date": "2026-11-19", "new_window": "day"})
    assert out["reason"] == "ungrounded_date"


def test_reads_and_cancels_are_never_gated():
    g = _g(body="8am is fine thanks")
    assert G.check(g, "check_availability", {"arrival": "2026-11-03T08:00", "departure": "2026-11-06T17:00"}) is None
    assert G.check(g, "cancel_booking", {"booking_ref": "BK-1040"}) is None


def test_unparseable_dates_are_the_tools_problem_not_the_gates():
    g = _g(body="8am is fine thanks")
    assert G.check(g, "hold_booking", {"arrival": "soon", "departure": "later"}) is None


def test_off_means_off():
    g = _g(body="8am is fine thanks")
    g.on = False
    assert G.check(g, "hold_booking", {"arrival": "2026-11-03T08:00", "departure": "2026-11-06T17:00"}) is None
    assert G.check(None, "hold_booking", {"date": "2026-11-03"}) is None


def test_the_panel_line_says_where_each_date_came_from():
    g = _g(body="any space for bramble 10th-13th nov?", existing_bookings=[BK1040])
    line = G.note(g)
    assert line.startswith("Grounded dates: ")
    assert "10 Nov" in line and "this email: '10th-13th nov'" in line and "BK-1040" in line
    assert "name no dates" in G.note(_g(body="8am is fine thanks"))


# ── through the real executor ───────────────────────────────────────────────
STAY_CFG = carried_config({"capacity_per_window": 10, "open_time": "07:00", "close_time": "18:45",
            "working_days": "mon,tue,wed,thu,fri,sat,sun",
            "intake_configured_at": "2026-09-13T12:25:14+00:00",
            "intake_fields": [dict(intake.STAY_FIELD)], "service_types": []})


def _stay_exec(monkeypatch, inserted, existing=None, grounding=None):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(STAY_CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "subjects_for_bookings", lambda *a, **k: {})

    def fake_insert(tenant_id, date_iso, sb, length, summary, **k):
        row = {"id": f"new-{len(inserted) + 1}", "booking_ref": f"BK-104{1 + len(inserted)}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, "status": "proposed", **k}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact=GRAHAM, stay=True,
                                    intake_fields=STAY_CFG["intake_fields"],
                                    existing_bookings=existing, now=NOW, grounding=grounding)


INVENTED = {"arrival": "2026-11-03T08:00", "departure": "2026-11-06T17:00",
            "summary": "Boarding for Bramble (collie) 3–6 November",
            "details": {intake.STAY_FIELD["label"]: "2026-11-06 17:00"}}


def test_the_same_read_twice_in_one_draft_is_answered_from_the_first(monkeypatch):
    """Live 2026-09-22 (K10 rehearsal): with no dates to work from, the model
    asked the diary about 1 Nov, was told it could not hold it, and asked the
    identical question again. The diary cannot change inside one draft, so the
    repeat is the model circling — it costs tokens and prints the same line
    three times in the ⓘ."""
    calls = []
    execute, _state = _stay_exec(monkeypatch, [])
    inner = scheduling.db.list_active_bookings
    monkeypatch.setattr(scheduling.db, "list_active_bookings",
                        lambda *a, **k: (calls.append(1), inner(*a, **k))[1])
    args = {"arrival": "2026-11-01T08:00", "departure": "2026-11-01T17:00"}
    first = execute("check_availability", dict(args))
    assert first.get("available") is True and not first.get("repeated")
    spent = len(calls)

    again = execute("check_availability", dict(args))
    assert again.get("repeated") is True
    assert again.get("available") is first.get("available")
    assert "You already asked this exact question" in again["message"]
    assert len(calls) == spent, "the repeat must not touch the diary again"

    # a DIFFERENT span is a real question and goes through
    other = execute("check_availability", {"arrival": "2026-11-02T08:00",
                                           "departure": "2026-11-02T17:00"})
    assert not other.get("repeated")
    assert len(calls) > spent


def test_round_five_through_the_executor_touches_no_capacity(monkeypatch):
    inserted = []
    g = G.build(body="8am is fine thanks", existing_bookings=[BK1040], today=TODAY)
    execute, state = _stay_exec(monkeypatch, inserted, existing=[BK1040], grounding=g)
    out = execute("hold_booking", dict(INVENTED))
    assert out["error"] == "missing_info" and out["reason"] == "ungrounded_date"
    assert "BK-1040" in out["message"]
    assert inserted == [] and state["bookings"] == []
    assert state["grounding"] is g


def test_a_grounded_and_checked_hold_goes_through(monkeypatch):
    inserted = []
    # Times given too: since 2026-09-23 a stay's clock is grounded like its
    # dates (tests/test_time_grounding.py) — with none given, this would be a
    # reservation while the agent asks, not a booking.
    g = G.build(body="any space for bramble 3rd-6th nov? drop off 8am, collect 5pm", today=TODAY)
    execute, state = _stay_exec(monkeypatch, inserted, existing=[], grounding=g)
    first = execute("hold_booking", dict(INVENTED))
    assert first["error"] == "check_first"                      # no check yet this draft
    chk = execute("check_availability", {"arrival": INVENTED["arrival"], "departure": INVENTED["departure"]})
    assert chk["available"] is True
    assert "2026-11-03" in g.checked                            # the executor recorded it
    held = execute("hold_booking", dict(INVENTED))
    assert held["held"] is True and held["booking_ref"] == "BK-1041"
    assert len(inserted) == 1


def test_the_flag_off_is_byte_identical(monkeypatch):
    # grounding=None: the invented hold is NOT stopped here (that is the point
    # of the flag — every other tenant behaves exactly as before this change).
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[], grounding=None)
    out = execute("hold_booking", dict(INVENTED))
    assert out.get("held") is True and len(inserted) == 1


def test_the_panel_leads_with_the_grounding_line():
    class R:
        reasoning = "→ hold_booking({...})\n← {\"error\": \"missing_info\"}"
    g = G.build(body="8am is fine thanks", existing_bookings=[BK1040], today=TODAY)
    panel = pipeline._working_panel({"bookings": [], "grounding": g, "calls": []}, R())
    from app import working
    saved = working.unpack(panel)
    assert saved["technical"]["grounding"].startswith("Grounded dates: 10 Nov")
    assert "Grounded dates" not in saved["owner"]
    # and nothing changes for a state without one
    assert working.unpack(pipeline._working_panel({"bookings": [], "calls": []}, R()))["technical"]["trail"] == R.reasoning


# ── the extractor's citation rule (pure) ────────────────────────────────────
SRC = "Subject: Bramble\nWe'd like the weekend after next if you have it, so Sat 3rd to Mon 5th October."


def test_a_date_is_admitted_only_with_a_quote_that_is_really_there():
    items = {"dates": [{"iso": "2026-10-03", "quote": "Sat 3rd"},
                       {"iso": "2026-10-10", "quote": "the weekend after next"},   # present
                       {"iso": "2026-10-17", "quote": "the 17th"}],               # NOT in the email
             "ranges": []}
    got = booking_extract.admit(items, SRC, TODAY)
    assert [(d.isoformat(), q) for d, q in got] == [("2026-10-03", "Sat 3rd"),
                                                    ("2026-10-10", "the weekend after next")]


def test_ranges_expand_and_bad_input_is_dropped():
    items = {"dates": [{"iso": "not-a-date", "quote": "Sat 3rd"}],
             "ranges": [{"from_iso": "2026-10-03", "to_iso": "2026-10-05", "quote": "Sat 3rd to Mon 5th October"},
                        {"from_iso": "2026-10-05", "to_iso": "2026-10-03", "quote": "Sat 3rd to Mon 5th October"},  # backwards
                        {"from_iso": "2026-01-01", "to_iso": "2026-12-31", "quote": "Sat 3rd"}]}                  # absurd
    got = booking_extract.admit(items, SRC, TODAY)
    assert [d.isoformat() for d, _ in got] == ["2026-10-03", "2026-10-04", "2026-10-05"]


def test_the_quote_match_survives_case_and_whitespace_but_not_absence():
    assert booking_extract.admit({"dates": [{"iso": "2026-10-03", "quote": "  SAT   3RD "}], "ranges": []}, SRC)
    assert not booking_extract.admit({"dates": [{"iso": "2026-10-03", "quote": "Sun 4th"}], "ranges": []}, SRC)


def test_the_extractor_schema_uses_no_rejected_keywords():
    # structured output refuses `format`/`pattern` (the dependants extractor
    # learned this the hard way) — keep them out of the schema for good
    import json
    s = json.dumps(booking_extract.SCHEMA)
    assert '"format"' not in s and '"pattern"' not in s


# ── the pipeline carries the grounding out of the executor ──────────────────
# Live 2026-09-22: the first grounded draft's ⓘ panel had no "Grounded dates"
# header and the extractor never ran — the executor closure had the grounding,
# but _scheduling_setup rebuilds the state it returns and _sync() copied
# bookings/usage across and nothing else. This pins the carry, both ways.
def _setup(monkeypatch, grounded: bool):
    cfg = carried_config({"intake_configured_at": "2026-01-01T00:00:00+00:00", "grounded_dates": grounded,
           "intake_fields": [dict(intake.STAY_FIELD)], "service_types": []})
    monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(pipeline.db, "list_requirement_groups", lambda *a, **k: [])
    return pipeline._scheduling_setup(
        "t1", GRAHAM, None, None, existing_bookings=[BK1040],
        email={"subject": "Bramble again", "body": "any space for bramble 10th-13th nov?"},
        history=[])


def test_the_pipeline_state_carries_the_grounding_when_the_flag_is_on(monkeypatch):
    tools, execute, state, diary, flags = _setup(monkeypatch, grounded=True)
    assert flags["scheduling"] is True
    g = state.get("grounding")
    assert isinstance(g, G.Grounding)
    assert "2026-11-10" in g.dates and "this email: '10th-13th nov'" in g.why["2026-11-10"]
    assert [b["booking_ref"] for b in g.open_holds] == ["BK-1040"]
    # and the panel opens with it
    class R:
        reasoning = ""
    from app import working
    assert working.unpack(pipeline._working_panel(state, R()))["technical"]["grounding"].startswith("Grounded dates: 10 Nov")


def test_the_pipeline_state_has_no_grounding_when_the_flag_is_off(monkeypatch):
    tools, execute, state, diary, flags = _setup(monkeypatch, grounded=False)
    assert flags["scheduling"] is True and tools
    assert state.get("grounding") is None
    class R:
        reasoning = ""
    assert pipeline._working_panel(state, R()) is None
