"""An ungated expiry is a RECALL (run #6, Foxholes Garage, 2026-09-04).

A vehicle's MOT date must not gate a booking — a lapsed MOT is the reason you
book — so the garage set the field `expires` with the gate off. The sweep then
had nothing to say: it skipped every ungated field and only spoke about
instances with an upcoming booking; the two recall jobs that exist are gated
to the Practice and Trades OS. Now an ungated expiry due within RECALL_DAYS
(or lapsed) with nothing in the diary for that dependant raises one feed row
in the owner's words; once a visit is booked, silence. Gated fields behave as
before.

Run: python -m pytest tests/test_dependant_recall.py -q
"""
from datetime import date

from app import dependants_sweep as sweep

GROUP = {"id": "g-veh", "label": "Vehicle", "label_plural": "vehicles", "cardinality": "many",
         "fields": [
             {"key": "registration", "label": "Registration", "type": "text", "required": True,
              "durability": "once", "gate": False, "hint": ""},
             {"key": "mot_expiry_date", "label": "MOT expiry date", "type": "date", "required": False,
              "durability": "expires", "gate": False, "hint": ""},
         ]}
TODAY = date(2026, 9, 4)


def _inst(name, expiry):
    return {"id": f"i-{name.lower()}", "group_id": "g-veh", "contact_id": "c1", "name": name,
            "data": {"registration": "X", "mot_expiry_date": expiry}}


def test_a_due_ungated_expiry_with_nothing_booked_is_a_recall():
    items = sweep.due([GROUP], [_inst("Golf", "2026-09-28")], {}, TODAY)
    assert len(items) == 1
    it = items[0]
    assert it["recall"] is True and it["booking"] is None and it["lapsed"] is False
    title, body = sweep.phrase(it)
    assert title == "Golf's MOT expiry date is due 28th Sep 26"
    assert "Nothing booked for Golf yet" in body


def test_a_lapsed_ungated_expiry_reads_as_lapsed():
    items = sweep.due([GROUP], [_inst("Fiesta", "2026-09-01")], {}, TODAY)
    assert len(items) == 1 and items[0]["lapsed"] is True
    title, body = sweep.phrase(items[0])
    assert title == "Fiesta's MOT expiry date lapsed 1st Sep 26"
    assert "worth offering a slot" in body


def test_a_booked_dependant_is_left_alone():
    links = {"i-golf": [{"booking_date": "2026-09-15", "booking_ref": "BK-1002", "id": "b1"}]}
    assert sweep.due([GROUP], [_inst("Golf", "2026-09-28")], links, TODAY) == []


def test_a_far_off_expiry_is_silent():
    assert sweep.due([GROUP], [_inst("Golf", "2027-03-01")], {}, TODAY) == []


def test_gated_fields_still_need_a_booking_and_still_speak_of_it():
    gated = {**GROUP, "fields": [dict(GROUP["fields"][1], gate=True, key="cert", label="Certificate expiry")]}
    inst = {"id": "i-tub", "group_id": "g-veh", "contact_id": "c1", "name": "Byre",
            "data": {"cert": "2026-09-10"}}
    assert sweep.due([gated], [inst], {}, TODAY) == []          # no booking → nothing (as before)
    links = {"i-tub": [{"booking_date": "2026-09-15", "booking_ref": "BK-9", "id": "b9"}]}
    items = sweep.due([gated], [inst], links, TODAY)
    assert len(items) == 1 and not items[0].get("recall") and items[0]["before_booking"] is True
