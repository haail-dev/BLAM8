"""A whole address (Kris 2026-09-26: "taking address should mean WHOLE
address" — BK-1003 was booked at "14 Sydney Buildings, Bath", no postcode).

A required address field counts as gathered only once a UK postcode is in the
booking's details — in the address itself or in a postcode field of its own.

Run: python -m pytest tests/test_whole_address.py -q
"""
from app import intake, scheduling

FIELDS = [{"label": "Job address", "required": True},
          {"label": "What needs doing", "required": True},
          {"label": "Email address", "required": False}]


def test_an_address_without_a_postcode_is_still_missing():
    d = {"Job address": "14 Sydney Buildings, Bath", "What needs doing": "No hot water"}
    assert intake.missing_required(FIELDS, d) == ["Job address"]
    d["Job address"] = "14 Sydney Buildings, Bath BA2 6BJ"
    assert intake.missing_required(FIELDS, d) == []
    d["Job address"] = "14 sydney buildings, bath, ba26bj"
    assert intake.missing_required(FIELDS, d) == []


def test_a_postcode_field_of_its_own_counts():
    fields = FIELDS + [{"label": "Postcode", "required": True}]
    d = {"Job address": "14 Sydney Buildings, Bath", "Postcode": "BA2 6BJ",
         "What needs doing": "Leak"}
    assert intake.missing_required(fields, d) == []


def test_only_places_need_one():
    assert intake.is_address_field("Collection address")
    assert not intake.is_address_field("Email address")
    assert not intake.is_address_field("What needs doing")
    fields = [{"label": "Email address", "required": True}]
    assert intake.missing_required(fields, {"Email address": "sam@x.co"}) == []
    # an address not given at all is missing the ordinary way (listed once)
    assert intake.missing_required(FIELDS, {"What needs doing": "Leak"}) == ["Job address"]


def test_the_refusal_tells_the_agent_to_ask_for_the_postcode():
    missing = intake.missing_required(FIELDS, {"Job address": "18 Widcombe Parade, Bath",
                                               "What needs doing": "Leak"})
    short = intake.needs_postcode(FIELDS, {"Job address": "18 Widcombe Parade, Bath",
                                           "What needs doing": "Leak"})
    assert missing == short == ["Job address"]
    src = open("src/app/scheduling.py", encoding="utf-8").read()
    assert "INCLUDING the postcode" in src and "never guess or look one up" in src
    assert "missing_info" in scheduling._missing_info(missing)["error"]
