"""Reserve while gathering (Kris 2026-09-22, K2 rehearsal).

"Agent should have held that slot while gathering this info" — and then,
deciding the edge: "reserve on missing details only".

The customer agreed a date that is free and grounded; the only thing stopping
the hold was detail they had not sent yet (vaccination date, neutered, feeding
notes). The agent asked for it and held nothing, so the slot stayed open to
anyone. Now:

  * details missing, nothing else wrong → the slot is RESERVED (an `offered`
    row: exclusive, with its release time) and the refusal says so;
  * the details arrive → the same hold confirms that reservation in place;
  * a RULE failing (a lapsed certificate, a level not reached) → nothing is
    reserved: not fixed by a quick reply, and a held slot blocks someone else.

Run: python -m pytest tests/test_reserve_while_gathering.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

import pytest

from app import requirement_groups as rg
from app import scheduling
from app.scheduling import DIARY_TZ, make_executor

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)   # Tuesday
DAY = "2026-06-24"                                     # a free Wednesday, well past notice
INTAKE = [{"label": "Dog name", "required": True}, {"label": "Neutered", "required": True}]


@pytest.fixture
def diary(monkeypatch):
    calls = {"holds": [], "offers": [], "accepted": []}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_blocks", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-1"})

    def fake_hold(tid, d, w, s, **kw):
        status = kw.pop("status", None) or "proposed"
        row = {"id": f"b-{len(calls['holds']) + 1}", "booking_ref": f"BK-{len(calls['holds']) + 1}",
               "booking_date": d, "window": w, "status": status, **kw}
        calls["holds"].append(row)
        (calls["offers"] if status == "offered" else calls["accepted"]).append(row)
        return row
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", fake_hold)
    # the offer the reservation made, as the next email's executor would see it
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact",
                        lambda *a, **k: [dict(o) for o in calls["offers"] if o["status"] == "offered"])

    def fake_accept(bid, tid, details=None, **k):
        for o in calls["offers"]:
            if o["id"] == bid:
                o["status"] = "proposed"
                o["details"] = details
                return dict(o)
        return None
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", fake_accept)
    return calls


def test_missing_details_reserve_the_slot_and_say_until_when(diary):
    execute, state = make_executor("t1", now=NOW, contact="laura@x.co", intake_fields=INTAKE)
    out = execute("hold_booking", {"date": DAY, "window": "day", "summary": "Meet & greet",
                                   "details": {"Dog name": "Otis"}})
    assert out["error"] == "missing_info" and out["missing"] == ["Neutered"]
    assert out["reserved"] is True and out["hold_until_phrase"]
    assert "reserved for this customer" in out["message"] and "NOT booked" in out["message"]
    assert [(o["booking_date"], o["status"]) for o in diary["offers"]] == [(DAY, "offered")]
    assert diary["offers"][0]["expires_at"]                      # it lets go on its own


def test_the_details_arriving_confirm_the_reservation_in_place(diary):
    execute, _ = make_executor("t1", now=NOW, contact="laura@x.co", intake_fields=INTAKE)
    execute("hold_booking", {"date": DAY, "window": "day", "summary": "Meet & greet",
                             "details": {"Dog name": "Otis"}})
    # the customer replies with the rest — a fresh email, a fresh executor
    execute2, _ = make_executor("t1", now=NOW, contact="laura@x.co", intake_fields=INTAKE)
    out = execute2("hold_booking", {"date": DAY, "window": "day", "summary": "Meet & greet",
                                    "details": {"Dog name": "Otis", "Neutered": "Yes"}})
    assert out.get("held") is True and out.get("accepted_offer") is True
    assert diary["offers"][0]["status"] == "proposed"            # the SAME row, flipped
    assert len(diary["holds"]) == 1                               # no second booking made


def test_everything_given_books_as_before(diary):
    execute, _ = make_executor("t1", now=NOW, contact="laura@x.co", intake_fields=INTAKE)
    out = execute("hold_booking", {"date": DAY, "window": "day", "summary": "x",
                                   "details": {"Dog name": "Otis", "Neutered": "Yes"}})
    assert out.get("held") is True and diary["offers"] == []


JABS = rg.clean_group({"id": "g", "label": "Dog", "label_plural": "Dogs", "fields": [
    {"label": "Vaccinations", "type": "date", "durability": "expires", "gate": True, "required": True}]})


def _record(monkeypatch, data):
    monkeypatch.setattr(scheduling.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i-otis", "group_id": "g", "contact_id": "c-1", "name": "Otis", "data": data,
         "field_meta": {}}])
    monkeypatch.setattr(scheduling.db, "upsert_requirement_instance",
                        lambda *a, **k: {"id": "i-otis", "name": "Otis"})
    monkeypatch.setattr(scheduling.db, "link_booking_subjects", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "list_documents", lambda *a, **k: [])


def test_a_rule_that_fails_reserves_nothing(diary, monkeypatch):
    """A lapsed certificate is a rule, not a missing detail (K1's case)."""
    _record(monkeypatch, {"vaccinations": "2026-06-01"})
    execute, _ = make_executor("t1", now=NOW, contact="laura@x.co", groups=[JABS])
    out = execute("hold_booking", {"date": DAY, "window": "day", "summary": "x", "details": {},
                                   "subjects": ["Otis"]})
    assert out["error"] == "missing_info" and "expired" in out["missing"][0]
    assert "reserved" not in out and diary["holds"] == []


def test_a_dependant_detail_simply_not_given_does_reserve(diary, monkeypatch):
    _record(monkeypatch, {})
    execute, _ = make_executor("t1", now=NOW, contact="laura@x.co", groups=[JABS])
    out = execute("hold_booking", {"date": DAY, "window": "day", "summary": "x", "details": {},
                                   "subjects": ["Otis"]})
    assert out["missing"] == ["Otis: Vaccinations"] and out["reserved"] is True
    assert [o["status"] for o in diary["offers"]] == ["offered"]


def test_a_full_day_reserves_nothing_and_refuses_as_before(diary, monkeypatch):
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", lambda *a, **k: None)
    execute, _ = make_executor("t1", now=NOW, contact="laura@x.co", intake_fields=INTAKE)
    out = execute("hold_booking", {"date": DAY, "window": "day", "summary": "x",
                                   "details": {"Dog name": "Otis"}})
    assert out["error"] == "missing_info" and "reserved" not in out


def test_a_stay_with_details_missing_is_reserved_as_an_offer(monkeypatch):
    """The rig's case: a kennel stay, dates free and given, a required detail
    not yet sent — the span is reserved as an `offered` stay."""
    from tests.test_stay_executor import _exec
    inserted = []
    execute, state = _exec(monkeypatch, existing=[], inserted=inserted,
                           intake_fields=[{"label": "Neutered", "required": True}])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    out = execute("hold_booking", {"arrival": "2026-06-30T13:00", "departure": "2026-07-01T09:30",
                                   "summary": "Trial night for Otis", "details": {}})
    assert out["error"] == "missing_info" and out["reserved"] is True
    assert [r.get("status") for r in inserted] == ["offered"] and inserted[0].get("expires_at")
