"""An offer's dates gate a dependant's expiry the way a hold's date does
(live 2026-09-04: BK-1006 offered for a tub whose certificate had lapsed)."""
from app import scheduling


def test_gate_date_reads_hold_arrival_and_offer_slots():
    assert scheduling.gate_date({"date": "2026-10-07", "window": "am"}) == "2026-10-07"
    assert scheduling.gate_date({"arrival": "2026-10-07T09:00"}) == "2026-10-07T09:00"
    assert scheduling.gate_date({"slots": [{"date": "2026-10-09", "window": "am"},
                                           {"date": "2026-10-07", "window": "pm"}]}) == "2026-10-07"
    assert scheduling.gate_date({"slots": []}) is None
    assert scheduling.gate_date(None) is None


def test_a_lapsed_expiry_blocks_an_offer_date():
    from app import requirement_groups as rg
    group = {"label": "Hot tub", "fields": [{"key": "cert", "label": "Certificate expiry", "type": "date",
                                             "durability": "expires", "gate": True, "required": False}]}
    gaps = rg.missing_for(group, {"cert": "2026-09-01"},
                          scheduling.gate_date({"slots": [{"date": "2026-10-07", "window": "am"}]}))
    assert gaps and "expired 2026-09-01" in gaps[0]


# A stay has to be covered to the END (Kris 2026-09-22): jabs that lapse on the
# Sunday of a Friday-to-Monday stay used to pass, because only the arrival was read.
JABS = {"label": "Dog", "fields": [{"key": "vaccinations", "label": "Vaccinations", "type": "date",
                                    "durability": "expires", "gate": True, "required": True}]}
STAY = {"arrival": "2026-10-23T08:00", "departure": "2026-10-26T17:00"}


def test_gate_until_is_a_stays_departure_and_nothing_else():
    assert scheduling.gate_until(STAY) == "2026-10-26T17:00"
    assert scheduling.gate_until({"date": "2026-10-07", "window": "am"}) is None
    assert scheduling.gate_until({"arrival": "2026-10-07T09:00"}) is None
    assert scheduling.gate_until({"slots": [{"date": "2026-10-09"}]}) is None
    assert scheduling.gate_until(None) is None


def test_jabs_lapsing_mid_stay_block_it_and_say_so():
    from app import requirement_groups as rg
    gaps = rg.missing_for(JABS, {"vaccinations": "2026-10-25"},
                          scheduling.gate_date(STAY), until=scheduling.gate_until(STAY))
    assert len(gaps) == 1
    assert "lapses 2026-10-25" in gaps[0] and "2026-10-26" in gaps[0]
    assert "before the booking" not in gaps[0]


def test_lasting_to_the_departure_day_is_enough():
    from app import requirement_groups as rg
    for ok in ("2026-10-26", "2027-06-01"):
        assert rg.missing_for(JABS, {"vaccinations": ok}, scheduling.gate_date(STAY),
                              until=scheduling.gate_until(STAY)) == []


def test_lapsed_before_arrival_still_reads_as_before_the_booking():
    from app import requirement_groups as rg
    gaps = rg.missing_for(JABS, {"vaccinations": "2026-10-20"}, scheduling.gate_date(STAY),
                          until=scheduling.gate_until(STAY))
    assert gaps == ["Vaccinations (expired 2026-10-20, before the booking)"]
