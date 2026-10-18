"""Regression inputs from the setup walks; no model or network required."""
from copy import deepcopy
import pytest
from app import setup_rows as rows


@pytest.mark.parametrize("raw", [
    "Check | 45 | 45 | time | pool: Bay | days: Monday to Saturday",
    "Check | pool: Bay | 45 | days: mon-sat | time | 45",
    "Check | minutes: 45 | price: £45 | timing: time | resource: Bay | days: mon-sat",
])
def test_service_keyed_parts_can_move_without_shifting(raw):
    result = rows.parse("diary_settings", "service", raw)
    assert result["fields"] == {"name": "Check", "minutes": 45, "price": "45", "timing": "time",
                                "resource": "Bay", "days": list(rows.DAYS[:6])}
    assert result["problems"] == []


@pytest.mark.parametrize("raw", ["Check | 45 | time", "Check | 45 | | time"])
def test_optional_price_never_absorbs_timing(raw):
    result = rows.parse("diary_settings", "service", raw)
    assert not result["problems"]
    assert result["fields"]["price"] is None
    assert result["fields"]["timing"] == "time"


@pytest.mark.parametrize("raw", [
    "Check | 45 | 45 | time | days: someday",
    "Check | 45 | 45 | time | days: money",
    "Check | 45 | 45 | time | days: mon-fri | days: sat",
    "Check | 45 | 45 | time | unrecognised",
    "Check | 45 | 45 | time | pool: " + "a" * 61,
    "Check | 45 | 45", "Check | NaN | 45 | time", "Check | 99999 | 45 | time",
    "Check | 45 | 0 | time", "Check | 45 | 1.234 | time",
])
def test_invalid_service_has_no_partially_applicable_fields(raw):
    result = rows.parse("diary_settings", "service", raw)
    assert result["fields"] is None
    assert result["problems"]


def test_multiple_services_and_remember_survive_as_names():
    result = rows.parse("booking_details", "field",
                        "Authorisation | optional | Ask who approved | only for: A; B; C | remember: once")
    assert result["fields"] == {"label": "Authorisation", "required": False, "hint": "Ask who approved",
                                "services": ["A", "B", "C"], "remember": "once", "kind": "answer"}


def test_document_is_explicit_and_cannot_be_remembered():
    ok = rows.parse("booking_details", "field", "Evidence | optional | document | hint: Attach it")
    assert ok["fields"]["kind"] == "document"
    assert rows.parse("booking_details", "field", "Evidence | optional | document | remember: once")["problems"]


def test_dropdown_does_not_move_required_durability_gate_or_hint():
    result = rows.parse("dependants", "field",
                        "Item | Category | dropdown: A, B | optional | once | no gate | Which category?")
    assert result["fields"] == {"kind": "Item", "label": "Category", "type": "dropdown",
                                "options": ["A", "B"], "required": False, "durability": "once",
                                "gate": False, "hint": "Which category?"}


def test_unknown_type_is_reported_instead_of_discarded():
    result = rows.parse("dependants", "field", "Item | Detail | unknown | required | once | no gate | Hint")
    assert result["fields"] is None
    assert result["problems"]


def test_expiry_never_gains_an_invented_reminder_interval():
    missing = rows.parse("dependants", "field", "Item | Valid until | date | optional | expires | no gate")
    assert missing["problems"]
    given = rows.parse("dependants", "field", "Item | Valid until | date | optional | expires 60 | no gate")
    assert given["fields"]["recall_days"] == 60
    assert given["fields"]["gate"] is False


def test_named_fields_round_trip_and_do_not_mutate_input():
    first = rows.parse("dependants", "field", "Item | Category | dropdown: A, B | optional | once | no gate")
    before = deepcopy(first)
    assert rows.parse("dependants", "field", first["fields"])["fields"] == first["fields"]
    assert first == before


def test_references_use_whole_card_not_row_order_and_saved_names():
    card = [{"name": "service", "value": "Check | 45 | | time | pool: Bay"},
            {"name": "resource", "value": "Bay | 1"}]
    assert not any(r["problems"] for r in rows.prepare("diary_settings", card))
    assert rows.prepare("diary_settings", card[:1])[0]["problems"]
    assert not rows.prepare("diary_settings", card[:1], saved={"resource": ["Bay"]})[0]["problems"]


def test_missing_kind_is_not_redirected_to_first_subject():
    card = [{"name": "group", "value": "Item | Items | several"},
            {"name": "field", "value": "Other | Detail | text | optional | once | no gate"}]
    assert rows.prepare("dependants", card, saved={})[1]["problems"]


def test_combined_service_cannot_be_shorter_than_longest_part():
    card = [{"name": "service", "value": v} for v in
            ("A | 45 | | time", "B | 120 | | time", "A + B | 90 | | time")]
    assert rows.prepare("diary_settings", card)[2]["problems"]
    card[2]["value"] = "A + B | 120 | | time"
    assert not rows.prepare("diary_settings", card)[2]["problems"]


def test_duplicate_rows_never_silently_choose_the_last_value():
    card = [{"name": "service", "value": v} for v in
            ("Check | 45 | | time", "Check | 120 | | time")]
    assert all(r["problems"] for r in rows.prepare("diary_settings", card))


def test_new_rows_cannot_overflow_a_saved_list_and_disappear_on_save():
    card = [{"name": "field", "value": "Extra | optional"}]
    result = rows.prepare("booking_details", card, saved={"intake": [f"Detail {i}" for i in range(20)]})
    assert result[0]["problems"]


def test_subject_kind_cannot_disappear_because_it_has_no_details():
    card = [{"name": "group", "value": "Item | Items | several"}]
    assert rows.prepare("dependants", card, saved={})[0]["problems"]


def test_omitted_optional_parts_are_distinct_from_explicit_clears():
    absent = rows.parse("diary_settings", "service", "Check | 45 | time")
    clear = rows.parse("diary_settings", "service", "Check | 45 | | time")
    assert "price" not in absent["provided"]
    assert "price" in clear["provided"]
    field = rows.parse("booking_details", "field", "Evidence | optional")
    assert field["provided"] == ["label", "required"]
    assert "remember" not in rows.describe("booking_details", "field", field["fields"], field["provided"])
