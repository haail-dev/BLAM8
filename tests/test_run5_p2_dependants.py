"""Run #5 (hot tubs, 2026-09-04) P2 fixes on the dependants side."""
from datetime import date

from app import dependants_extract as dx, dependants_sweep as ds, intake


def test_a_placeholder_word_at_the_front_is_a_placeholder():
    assert intake.is_placeholder("Unknown — drain and refill required due to guest rash")
    assert intake.is_placeholder("Not known, will check")
    assert intake.is_placeholder("TBC - after the test")
    assert not intake.is_placeholder("Unknown Road, Ashbourne")      # a real word follows without a separator
    assert not intake.is_placeholder("No — neither horse is lame")
    assert not intake.is_placeholder("2026-09-19")


def test_the_feed_title_keeps_the_label_as_written():
    item = {"instance": {"name": "The Granary"}, "field": {"label": "HSG282 certificate expiry"},
            "date": date(2026, 9, 1), "booking": {"booking_ref": "BK-1002", "booking_date": "2026-09-08"},
            "lapsed": True, "before_booking": True}
    title, body = ds.phrase(item)
    assert title == "The Granary's HSG282 certificate expiry has lapsed"
    plain = ds.phrase({**item, "field": {"label": "Passport expiry"}})[0]
    assert plain == "The Granary's passport expiry has lapsed"


GROUP = {"id": "g1", "label": "Hot tub", "label_plural": "Hot tubs",
         "fields": [{"key": "make_and_model", "label": "Make and model", "type": "text", "durability": "once"},
                    {"key": "water_last_changed", "label": "Water last changed", "type": "date", "durability": "confirm"}]}
BYRE = {"id": "i1", "group_id": "g1", "name": "The Byre", "data": {"make_and_model": "Hot Spring Envoy"}}


def test_a_new_chip_for_a_dependant_that_exists_becomes_that_dependant():
    steps = dx.plan([GROUP], [BYRE], [{"group": "Hot tub", "name": "The Byre hot tub", "confidence": 0.95,
                                       "fields": [{"key": "make_and_model", "value": "Hot Spring Envoy", "confidence": 0.95}]}])
    assert all(s.get("existing") is BYRE for s in steps if s["action"] != "skip")
    assert not any(s["action"] == "suggest" and s.get("existing") is None for s in steps)


def test_a_date_the_email_never_says_is_dropped():
    text = "Thanks both. The Dairy's certificate runs to 14 December. Nothing about water changes here."
    steps = dx.plan([GROUP], [BYRE], [{"group": "Hot tub", "name": "The Byre", "confidence": 0.6,
                                       "fields": [{"key": "water_last_changed", "value": "2025-07-08", "confidence": 0.6}]}],
                    text=text)
    assert steps == []
    kept = dx.plan([GROUP], [BYRE], [{"group": "Hot tub", "name": "The Byre", "confidence": 0.6,
                                      "fields": [{"key": "water_last_changed", "value": "2026-09-19", "confidence": 0.6}]}],
                   text="The Byre's water was changed on 19 September 2026.")
    assert kept and kept[0]["data"] == {"water_last_changed": "2026-09-19"}


def test_word_values_are_left_to_the_confidence_gate():
    assert dx._echoes("Holiday let", dx._echo_norm("two holiday cottages"))
    assert not dx._echoes("2025-07-08", dx._echo_norm("no dates at all"))
