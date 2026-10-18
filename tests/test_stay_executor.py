"""The office agent's STAY path (scheduling.make_executor(stay=True)) — the tool executor
a kennel/hotel/hire tenant gets: check_availability + hold_booking over an arrival→departure
span, capacity = concurrent occupancy. DB is mocked; the math is slotgrid (tested separately)."""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import scheduling
from app.scheduling import DIARY_TZ

CFG = carried_config({"capacity_per_window": 10, "open_time": "08:00", "close_time": "18:00",
       "working_days": "mon,tue,wed,thu,fri,sat,sun"})
NOW = datetime(2026, 6, 25, 9, 0, tzinfo=DIARY_TZ)


def _stay_row(drop: datetime, collect: datetime):
    """An existing stay as a booking row (grid columns set), as list_active_bookings returns."""
    from app import slotgrid as sg
    sb = sg.block_at(drop.time())
    length = sg.datetime_to_abs(collect) - sg.datetime_to_abs(drop)
    return {"booking_date": drop.date().isoformat(), "start_block": sb, "length_blocks": length,
            "status": "proposed", "spaces": None, "engineer_id": None}


def _exec(monkeypatch, existing, inserted, intake_fields=None):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing)
    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": "stay-1", "booking_ref": "BK-9001", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="sarah@x.co", stay=True,
                                    intake_fields=intake_fields, now=NOW)


def test_stay_check_available(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability",
                  {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00"})
    assert out["available"] is True and out["nights"] == 2


def test_stay_check_full_offers_nearest(monkeypatch):
    # 10 dogs fill every space 30 Jun -> 5 Jul.
    full = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    execute, _ = _exec(monkeypatch, existing=full, inserted=[])
    out = execute("check_availability",
                  {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00"})
    assert out["available"] is False
    # 08:00, not 00:00. Until 2026-09-22 the nearest opening was whatever block
    # the capacity maths first allowed, so this asserted a MIDNIGHT drop-off at a
    # business open 08:00–18:00. An offer the tenant could not honour is not an
    # offer; `_stay_offer`'s `legal` predicate now keeps both ends inside that
    # day's hours.
    assert out["nearest_arrival"] == "2026-07-05T08:00"
    assert out["nearest_departure"] == "2026-07-07T12:00"
    for when in (out["nearest_arrival"], out["nearest_departure"]):
        assert "08:00" <= when[11:] <= "18:00", when


def test_stay_hold_inserts_multiday_booking(monkeypatch):
    inserted = []
    execute, state = _exec(monkeypatch, existing=[], inserted=inserted)
    out = execute("hold_booking", {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00",
                                    "summary": "Boarding for Rex"})
    assert out["held"] is True and out["booking_ref"] == "BK-9001" and out["nights"] == 2
    # the persisted row spans 208 blocks (2½ days) — a genuine multi-day hold
    assert inserted and inserted[0]["length_blocks"] == 208
    assert state["bookings"] == inserted


def test_stay_hold_refused_when_full(monkeypatch):
    full = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    inserted = []
    execute, _ = _exec(monkeypatch, existing=full, inserted=inserted)
    out = execute("hold_booking", {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00",
                                   "summary": "Boarding for Rex"})
    # ...and the refusal offers the same honourable alternative as the check
    assert out["error"] == "full" and out["nearest_arrival"] == "2026-07-05T08:00"
    assert inserted == []


def test_stay_hold_gated_on_required_intake(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[],
                       intake_fields=[{"label": "Pet name", "required": True, "hint": ""}])
    out = execute("hold_booking", {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00",
                                   "summary": "Boarding", "details": {}})
    assert out["error"] == "missing_info" and "Pet name" in out["missing"]


def test_same_day_visit_books_like_any_stay(monkeypatch):
    """Kennel daycare (live e2e 2026-09-02): drop-off and collection on ONE
    day is a legal booking. The overnight rules — checkout_before, min_nights
    — must not touch it: a checkout_before of 09:30 (a morning drop-off
    window mis-mapped by a recommendation) plus min_nights 1 made daycare
    unbookable at a real kennels."""
    inserted = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config({
        **CFG,
        "day_hours": {"checkin_after": "07:00", "checkout_before": "09:30"},
        "stay_pricing": {"rate": 38.0, "rate_unit": "night", "min_nights": 1}}))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": "stay-2", "booking_ref": "BK-9002", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    execute, _ = scheduling.make_executor("t1", contact="sarah@x.co", stay=True, now=NOW)
    check = execute("check_availability",
                    {"arrival": "2026-06-30T08:00", "departure": "2026-06-30T17:00"})
    assert check["available"] is True and check["nights"] == 0
    # No £38-per-night quote for a day visit — the day rate lives on the
    # service list / knowledge, and "1 night" for 0 nights billed daycare at
    # the boarding rate.
    assert not check.get("quote")
    out = execute("hold_booking", {"arrival": "2026-06-30T08:00",
                                   "departure": "2026-06-30T17:00",
                                   "summary": "Daycare for Bella and Coco"})
    assert out["held"] is True and inserted
    # An OVERNIGHT stay still meets the rules exactly as before.
    execute2, _ = scheduling.make_executor("t1", contact="s@x.co", stay=True, now=NOW)
    over = execute2("check_availability",
                    {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T10:00"})
    assert over["error"] == "checkout_time"          # 10:00 > checkout_before 09:30


def test_manual_stay_add_books_confirmed_and_checks_capacity(monkeypatch):
    """The dashboard's manual Add on a stay diary (Julius 2026-09-03): the
    posted arrival→departure span inserts CONFIRMED with the real grid span;
    notice/check-in rules are skipped (human's diary) but capacity is not;
    departure before arrival is refused."""
    inserted = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": "m-1", "booking_ref": "BK-9100", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    row, err = scheduling.manual_stay_add(
        "t1", datetime(2026, 9, 9, 13, 0), datetime(2026, 9, 9, 17, 0),
        "Meet & greet", contact="k@x.com")
    assert err is None and row["booking_ref"] == "BK-9100"
    assert inserted[0]["status"] == "confirmed" and inserted[0]["source"] == "manual"
    assert inserted[0]["start_block"] == 52 and inserted[0]["length_blocks"] == 16
    # full diary → refused, nothing inserted
    full = [_stay_row(datetime(2026, 9, 9, 0, 0), datetime(2026, 9, 10, 0, 0))
            for _ in range(10)]
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: full)
    row2, err2 = scheduling.manual_stay_add(
        "t1", datetime(2026, 9, 9, 13, 0), datetime(2026, 9, 9, 17, 0), "x")
    assert row2 is None and err2 == "full" and len(inserted) == 1
    # inverted span → bad_span
    row3, err3 = scheduling.manual_stay_add(
        "t1", datetime(2026, 9, 9, 17, 0), datetime(2026, 9, 9, 13, 0), "x")
    assert row3 is None and err3 == "bad_span"


def test_stay_hold_tool_teaches_free_visits_are_bookings():
    """Live e2e 2026-09-03 (Julius): the agent 'confirmed' a meet & greet in
    prose and never held it — the business had a customer arriving Wednesday
    1pm with nothing in the diary. The tool now says a FREE visit holds like
    a paid one."""
    hold = next(t for t in scheduling.STAY_TOOLS if t["name"] == "hold_booking")
    assert "configured introductory appointment" in hold["description"]
    assert "FREE visit" in hold["description"]


def test_stay_email_holds_each_distinct_visit(monkeypatch):
    """One email, several visits (live e2e 2026-09-02): a daycare day AND a
    boarding weekend must BOTH hold — the old one-hold gate forced the model
    to drop one and claim it anyway. Same span twice is still refused, and
    the cap is STAY_HOLDS_MAX."""
    execute, state = _exec(monkeypatch, existing=[], inserted=[])
    first = execute("hold_booking", {"arrival": "2026-06-30T08:00",
                                     "departure": "2026-06-30T17:00", "summary": "Daycare"})
    second = execute("hold_booking", {"arrival": "2026-07-10T12:00",
                                      "departure": "2026-07-12T16:00", "summary": "Boarding"})
    assert first["held"] is True and second["held"] is True
    assert len(state["bookings"]) == 2
    # An overlapping span = the same visit twice → refused with the held ref.
    dup = execute("hold_booking", {"arrival": "2026-07-11T09:00",
                                   "departure": "2026-07-13T10:00", "summary": "Dup"})
    assert dup["error"] == "already_held"
    third = execute("hold_booking", {"arrival": "2026-07-20T12:00",
                                     "departure": "2026-07-21T10:00", "summary": "C"})
    assert third["held"] is True
    fourth = execute("hold_booking", {"arrival": "2026-08-01T12:00",
                                      "departure": "2026-08-02T10:00", "summary": "D"})
    assert fourth["error"] == "hold_limit"


def test_non_stay_diary_keeps_one_hold_per_email(monkeypatch):
    """The multi-hold carve-out is stay-only — a slot diary still refuses a
    second hold."""
    inserted = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": f"b-{len(inserted)}", "booking_ref": "BK-1", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_booking", fake_insert, raising=False)
    execute, state = scheduling.make_executor("t1", contact="s@x.co", now=NOW)
    state["bookings"].append({"id": "b-0", "status": "proposed"})
    out = execute("hold_booking", {"date": "2026-06-30", "window": "10:00", "summary": "B"})
    assert out["error"] == "already_held"


# ── offer_stay (docs/plan-alternatives-and-offers.md, 2026-09-22) ────────────
# A stay diary had only hold_booking, so when a customer's dates were full the
# agent either booked them into an alternative they had never seen — approving
# a reply CONFIRMS a proposed hold — or reserved nothing and hoped the space
# survived the exchange. Live that day: Jess asked for 17-19 October, was told
# it was full, and was confirmed into 19-21 October a second after send.
def _offer_exec(monkeypatch, inserted, existing=None, cfg=None):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg or CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing or [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": f"o-{len(inserted) + 1}", "booking_ref": f"BK-90{len(inserted) + 1}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="sarah@x.co", stay=True, now=NOW)


SPAN = {"arrival": "2026-06-30T09:00", "departure": "2026-07-02T16:00"}
SPAN2 = {"arrival": "2026-07-06T09:00", "departure": "2026-07-08T16:00"}


def test_offer_stay_reserves_without_booking(monkeypatch):
    inserted = []
    execute, state = _offer_exec(monkeypatch, inserted)
    out = execute("offer_stay", {"stays": [SPAN, SPAN2], "summary": "Boarding for Rex"})

    # reserved, with a deadline the reply is required to quote
    assert out["status"] == "offered" and len(out["offered"]) == 2
    assert out["hold_until"] and out["hold_until_phrase"]
    assert "nothing is booked until they say yes" in out["message"].lower()

    # the ROWS are offered, not proposed — this is the bit that stops approval
    # confirming them (db.resolve_holds_for_log only moves 'proposed')
    assert [r["status"] for r in inserted] == ["offered", "offered"]
    assert all(r["expires_at"] for r in inserted)
    assert state["bookings"] == inserted


def test_an_offer_obeys_every_rule_a_hold_obeys(monkeypatch):
    """An offer is an exclusive reservation, so a span the business could never
    honour must not be offered either — check-out windows, minimum stay, the
    front desk, and capacity."""
    inserted = []
    strict = {**CFG, "day_hours": {"checkout_before": "09:30"}}
    execute, _ = _offer_exec(monkeypatch, inserted, cfg=strict)
    out = execute("offer_stay", {"stays": [SPAN], "summary": "Boarding for Rex"})
    assert out["error"] == "nothing_offered"          # 16:00 > checkout_before
    assert out["rejected"][0]["error"] == "checkout_time"
    assert inserted == []

    # and a full span is rejected on capacity, with the free one still offered
    full = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    execute2, _ = _offer_exec(monkeypatch, inserted, existing=full)
    out2 = execute2("offer_stay", {"stays": [SPAN, SPAN2], "summary": "Boarding for Rex"})
    assert [o["arrival"] for o in out2["offered"]] == ["2026-07-06T09:00"]
    assert out2["rejected"][0]["error"] == "full"


def test_accepting_an_offered_stay_flips_it_rather_than_booking_a_second(monkeypatch):
    """The offered row already holds the capacity. A fresh hold would collide
    with it, so the acceptance converts it in place and releases the sibling."""
    inserted, released, accepted_ids = [], [], []
    execute, state = _offer_exec(monkeypatch, inserted)
    offered_row = {"id": "o-1", "booking_ref": "BK-901", "booking_date": "2026-06-30",
                   "start_block": scheduling.slotgrid.block_at(datetime(2026, 6, 30, 9, 0).time()),
                   "length_blocks": (scheduling.slotgrid.datetime_to_abs(datetime(2026, 7, 2, 16, 0))
                                     - scheduling.slotgrid.datetime_to_abs(datetime(2026, 6, 30, 9, 0))),
                   "status": "offered", "details": {"Pet name": "Rex"}}
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact",
                        lambda *a, **k: [offered_row])
    monkeypatch.setattr(scheduling.db, "accept_offered_booking",
                        lambda bid, tid, details=None: (accepted_ids.append(bid),
                                                        {**offered_row, "status": "proposed",
                                                         "details": details})[1])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact",
                        lambda tid, c, exclude_ids=None: released.append(exclude_ids))

    # NOTE the times: the offer was 09:00→16:00, and this accepts with the
    # customer's ORIGINAL 08:00→17:00. Live 2026-09-22 that mismatch made the
    # exact-block match miss, the flip never ran, and the diary called her own
    # reservation full. A customer agreeing to "Monday to Wednesday" is
    # agreeing to the DAYS; the times are the ones we told her.
    out = execute("hold_booking", {"arrival": "2026-06-30T08:00",
                                   "departure": "2026-07-02T17:00",
                                   "summary": "Boarding for Rex",
                                   "details": {"Phone": "07700 900123"}})
    assert out["held"] is True and out["was_offered"] is True
    # and it reports the OFFER's times back, because those are the promise
    assert out["arrival"].endswith("T09:00") and out["departure"].endswith("T16:00")
    assert out["booking_ref"] == "BK-901"
    assert accepted_ids == ["o-1"] and released == [["o-1"]]
    assert inserted == [], "accepting an offer must not insert a second row"
    # what the customer added since the offer lands on the row, and the offer's
    # own details survive (the 2026-09-03 lesson)
    assert state["bookings"][0]["details"] == {"Pet name": "Rex", "Phone": "07700 900123"}


def test_a_hold_and_an_offer_are_never_both_placed_for_one_email(monkeypatch):
    inserted = []
    execute, _ = _offer_exec(monkeypatch, inserted)
    assert execute("hold_booking", {**SPAN, "summary": "Boarding for Rex"})["held"] is True
    out = execute("offer_stay", {"stays": [SPAN2], "summary": "Boarding for Rex"})
    assert out["error"] == "already_held"
    assert len(inserted) == 1


def test_every_intake_gated_tool_can_carry_what_the_gate_demands():
    """The trap that ate a live draft on 2026-09-22.

    `offer_stay` shipped routed through `_intake_gated` but missing from
    `requirement_groups.with_subjects`'s hard-coded tool list, so the gate
    refused it for missing `subjects` while the schema gave the model no way to
    send any. It retried four times and the loop ran out — the customer's email
    ended with no draft at all. A tool missing from that list is not
    "unsupported", it is UNWINNABLE, and nothing in the types says so.

    There are two lists a new booking tool must join — this one, and the
    combined-executor whitelist in pipeline.py. This test guards the first; the
    second is guarded in test_appointments_on_stays.
    """
    from app import requirement_groups as rgroups
    groups = [{"id": "g1", "label": "Dog", "label_plural": "Dogs",
               "fields": [{"label": "Breed"}]}]
    gated = {"hold_booking", "offer_stay"}          # the stay tools _intake_gated wraps
    granted = {t["name"] for t in rgroups.with_subjects(scheduling.STAY_TOOLS, groups)
               if "subjects" in (t["input_schema"].get("properties") or {})}
    assert gated <= granted, f"gated but cannot carry subjects: {gated - granted}"

    # and the same for the service side, whose offer tool is offer_slots
    granted_svc = {t["name"] for t in rgroups.with_subjects(scheduling.SERVICE_TOOLS, groups)
                   if "subjects" in (t["input_schema"].get("properties") or {})}
    assert {"hold_booking", "offer_slots"} <= granted_svc

    # a read-only tool must NOT grow one — it books nothing
    assert "check_availability" not in granted


def test_accepting_your_own_offer_is_not_a_full_diary(monkeypatch):
    """Live 2026-09-22, the acceptance run. Jess said "Monday to Wednesday is
    perfect" against the BK-1049 we had offered her. The gate demanded an
    availability check first; the check counted her OWN offer against her and
    answered full; and the reply apologised that the slot "didn't hold". It
    had held — for her.

    The capacity math cannot tell whose reservation is in the way, so the check
    has to. Only an EXACT span match counts: a different stay of theirs that
    merely overlaps is a real clash.
    """
    inserted = []
    arr, dep = datetime(2026, 6, 30, 9, 0), datetime(2026, 7, 2, 16, 0)
    theirs = {"id": "o-1", "booking_ref": "BK-901", "status": "offered",
              "booking_date": "2026-06-30",
              "start_block": scheduling.slotgrid.block_at(arr.time()),
              "length_blocks": (scheduling.slotgrid.datetime_to_abs(dep)
                                - scheduling.slotgrid.datetime_to_abs(arr)),
              "expires_at": None, "spaces": None, "engineer_id": None}
    # the diary is full BECAUSE of their own row plus nine others
    full = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: full)
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [theirs])
    monkeypatch.setattr(scheduling.db, "insert_stay_booking",
                        lambda *a, **k: inserted.append(1))
    execute, _ = scheduling.make_executor("t1", contact="sarah@x.co", stay=True, now=NOW)

    out = execute("check_availability", {"arrival": "2026-06-30T09:00",
                                         "departure": "2026-07-02T16:00"})
    assert out["available"] is True, "a customer's own reservation is not 'full'"
    assert out["already_theirs"] == "BK-901"
    assert "never tell them the space was lost" in out["message"]

    # a span they do NOT hold is still honestly full
    other = execute("check_availability", {"arrival": "2026-07-01T09:00",
                                           "departure": "2026-07-03T16:00"})
    assert other["available"] is False


def test_a_different_stay_that_merely_overlaps_is_still_a_clash(monkeypatch):
    """The date-span match must not become a wildcard. A reservation of theirs
    on DIFFERENT days that happens to overlap is a real clash and stays one —
    otherwise a customer with one booking could walk through a full diary."""
    arr, dep = datetime(2026, 6, 30, 9, 0), datetime(2026, 7, 2, 16, 0)
    theirs = {"id": "o-1", "booking_ref": "BK-901", "status": "offered",
              "booking_date": "2026-06-30",
              "start_block": scheduling.slotgrid.block_at(arr.time()),
              "length_blocks": (scheduling.slotgrid.datetime_to_abs(dep)
                                - scheduling.slotgrid.datetime_to_abs(arr)),
              "expires_at": None, "spaces": None, "engineer_id": None}
    full = [_stay_row(datetime(2026, 6, 30, 0, 0), datetime(2026, 7, 5, 0, 0)) for _ in range(10)]
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: full)
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [theirs])
    execute, _ = scheduling.make_executor("t1", contact="sarah@x.co", stay=True, now=NOW)

    # the SAME days, different clock — theirs
    same = execute("check_availability", {"arrival": "2026-06-30T08:00",
                                          "departure": "2026-07-02T17:00"})
    assert same["available"] is True and same["already_theirs"] == "BK-901"

    # one day longer — a different stay, and the diary is genuinely full
    longer = execute("check_availability", {"arrival": "2026-06-30T09:00",
                                            "departure": "2026-07-03T16:00"})
    assert longer["available"] is False


# ── ladder gates on a STAY diary (2026-09-22) ───────────────────────────────
LADDER_GROUP = {"id": "g-dog", "label": "Dog", "label_plural": "Dogs",
                "fields": [{"label": "Stay level", "key": "stay_level", "type": "dropdown",
                            "options": ["Not assessed", "Meet & greet done", "Trial night done"],
                            "ladder": {"gates": [{"service": "Boarding",
                                                  "min": "Trial night done"}]}}]}


def test_a_stay_tells_the_ladder_gate_what_it_is_booking():
    """Kris 2026-09-22, on making the trial-night requirement real rather than a
    line in never_do: "Making the trial night a real gate should be configurable
    in BLAM8?" It already is — progress ladders — but it could not work on a
    stay diary, because a stay's hold_booking carries no `service` argument and
    a blank service matches EVERY gate. A "Boarding needs a trial night" rule
    would have blocked the daycare day the same dog already attends.

    The service is in the details, under the tenant's own label, and the model
    fills it on every hold.
    """
    from app import ladders
    mabel = {"stay_level": "Meet & greet done"}

    def verdict(args):
        svc = scheduling.booked_service(args)
        return svc, ladders.prerequisite_refusal(LADDER_GROUP, mabel, "Mabel", svc)

    svc, ref = verdict({"details": {"Service type": "Boarding", "Dog name(s)": "Mabel"}})
    assert svc == "Boarding" and ref and ref["error"] == "prerequisite"
    assert "Trial night done" in ref["message"]

    # the daycare she already attends is NOT gated by a boarding rule
    svc, ref = verdict({"details": {"Service type": "Daycare"}})
    assert svc == "Daycare" and ref is None

    # the model writes the key however it likes (the company_name lesson)
    assert scheduling.booked_service({"details": {"service_type": "Boarding"}}) == "Boarding"
    assert scheduling.booked_service({"details": {"SERVICE": "Boarding"}}) == "Boarding"

    # a service diary passes `service` outright and is untouched
    assert scheduling.booked_service({"service": "Boarding"}) == "Boarding"
    assert scheduling.booked_service({"service": "Boarding",
                                      "details": {"Service type": "Daycare"}}) == "Boarding"

    # and once she has done her trial night, boarding is hers
    assert ladders.prerequisite_refusal(LADDER_GROUP, {"stay_level": "Trial night done"},
                                        "Mabel", "Boarding") is None


def test_a_hold_that_names_no_service_is_still_gated():
    """Fail CLOSED. If we cannot tell what is being booked we do not know
    whether a prerequisite applies, and for a safety gate the recoverable
    mistake is the one where the agent explains and asks."""
    from app import ladders
    assert scheduling.booked_service({"details": {"Dog name(s)": "Mabel"}}) == ""
    ref = ladders.prerequisite_refusal(LADDER_GROUP, {"stay_level": "Not assessed"}, "Mabel", "")
    assert ref and ref["error"] == "prerequisite"


def test_the_onboarding_agent_knows_a_ladder_can_be_a_prerequisite():
    """Kris 2026-09-22: "make sure the Onboarding Agent knows the feature."

    The card could always express a service gate, but the catalogue described
    a ladder purely as PROGRESSION — swim stages, belts, grades — and said
    "ONLY when the business genuinely moves people through levels". So it never
    occurred to the agent that "a meet & greet, then a trial night, before any
    boarding" is the same mechanism, and a kennels' most important rule stayed
    a line in never_do that the model mentioned in three drafts out of four.
    """
    from app import settings_recommend
    ladder = settings_recommend.PAGES["dependants"]["fields"]["ladder"]
    # it is a gate, not a reminder
    assert "HARD GATE" in ladder and "diary refuses that service below the required level" in ladder
    assert "Service = minimum level" in ladder
    assert "only their named steps, services and required levels" in ladder
    # the old framing no longer forbids the prerequisite case. Scoped to the
    # ladder: "ONLY when the business genuinely…" still guards PACKAGES, where
    # prepaid blocks really are the narrow case it describes.
    assert "ONLY when the business genuinely" not in ladder
    assert "prerequisite step before another service can be booked" in ladder

    # and it reaches the agent — the catalogue is what goes in the prompt
    assert ladder in settings_recommend.catalogue_block()
