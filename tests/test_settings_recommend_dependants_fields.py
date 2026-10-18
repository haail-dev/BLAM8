"""The Dependants recommend card keeps every field of a kind (live 2026-09-04,
hot tub run: the model wrote four fields, the card showed one — the repeat
dedupe keyed on the first pipe part, which for a field is the KIND)."""
from app import settings_recommend


def test_every_field_of_one_kind_survives():
    vals = [
        {"name": "group", "value": "Hot tub | Hot tubs | several"},
        {"name": "field", "value": "Hot tub | Make and model | required | once | no gate | data plate"},
        {"name": "field", "value": "Hot tub | Water last changed | optional | confirm | no gate | date"},
        {"name": "field", "value": "Hot tub | Micro test certificate valid until | optional | expires | gate | lets"},
    ]
    out = settings_recommend.clean("dependants", vals)
    assert [v["value"].split("|")[1].strip() for v in out if v["name"] == "field"] == [
        "Make and model", "Water last changed", "Micro test certificate valid until"]


def test_the_same_field_twice_still_dedupes():
    vals = [
        {"name": "field", "value": "Hot tub | Make and model | required | once | no gate | a"},
        {"name": "field", "value": "hot tub | make and model | optional | once | no gate | b"},
    ]
    out = settings_recommend.clean("dependants", vals)
    assert len(out) == 1 and out[0]["value"].endswith("| a")


def test_other_pages_keep_the_first_part_key():
    vals = [
        {"name": "field", "value": "Site address | required | where"},
        {"name": "field", "value": "site address | optional | again"},
    ]
    out = settings_recommend.clean("booking_details", vals)
    assert len(out) == 1
