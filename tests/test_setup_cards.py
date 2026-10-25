from copy import deepcopy
import pytest
from app import setup_cards as cards


def append(row_name="service", **fields):
    return {"op": "append", "name": row_name, "fields": fields,
            "sources": {key: {"kind": "owner_said", "reference": "Owner turn 1"} for key in fields}}


def service(**kw):
    return append("service", **({"name": "Review", "minutes": 45, "timing": "time"} | kw))


def start(*changes, page="diary_settings"):
    return cards.change(page, None, 0, "turn-1", list(changes))


def edit(card, index=0, **fields):
    patch = append(**fields)
    patch.pop("name")
    return patch | {"op": "set", "id": card["lines"][index]["id"]}


def test_corrections_carry_required_flags_sources_and_unmentioned_lines():
    original = start(append("field", label="Evidence", required=False, kind="document", hint="Original"),
                     append("field", label="Reference", required=True), page="booking_details")
    next_card = cards.change("booking_details", original, 1, "turn-2", [edit(original, hint="Revised")])
    assert next_card["lines"][0]["fields"] == {"label": "Evidence", "required": False, "kind": "document", "hint": "Revised"}
    assert next_card["lines"][1] == original["lines"][1]
    assert original["lines"][0]["fields"]["hint"] == "Original"
    assert next_card["revision"] == 2
    assert next_card["lines"][0]["sources"]["required"] == original["lines"][0]["sources"]["required"]


def test_missing_source_rejected_and_unknown_fields_never_disappear():
    patch = service()
    patch["sources"].pop("minutes")
    with pytest.raises(cards.Invalid, match="source"):
        start(patch)
    with pytest.raises(cards.Invalid, match="named fields"):
        start(service(unsupported=1))


def test_remove_is_leave_and_terminal_work_stays_in_old_revision():
    original = start(service())
    left = cards.change("diary_settings", original, 1, "turn-2", [{"op": "remove", "id": original["lines"][0]["id"]}])
    assert left["status"] == "left"
    assert left["lines"][0]["fields"] == original["lines"][0]["fields"]
    new = cards.change("diary_settings", left, 2, "turn-3", [service(minutes=90)])
    assert new["id"] != left["id"] and new["revision"] == 3
    assert len(new["lines"]) == 1
    with pytest.raises(cards.Stale):
        cards.change("diary_settings", new, 2, "turn-4", [service()])


def test_accept_preserves_unmentioned_optional_and_unknown_saved_attributes():
    original = start(service(minutes=90))
    cfg = {"service_types": [{"name": "Review", "minutes": 45, "price": 25, "timing": "half",
                              "days": [1], "buffer": 15, "future_rule": {"on": False}},
                             {"name": "Other", "minutes": 60}], "resource_types": [], "timezone": None}
    copy = deepcopy(cfg)
    accepted, patch = cards.accept(original, 1, "accept-1", [original["lines"][0]["id"]], cfg)
    assert set(patch) == {"service_types"}
    expected = deepcopy(cfg["service_types"])
    expected[0]["minutes"] = 90
    expected[0].pop("timing")  # explicit time clears the saved arrival window
    assert patch["service_types"] == expected
    assert cfg == copy
    assert accepted["status"] == "applied"
    assert accepted["last_action"]["changes"][0]["before"] == cfg["service_types"][0]


def test_explicit_optional_clears_and_numeric_days():
    card = start(service(price=None, days=["sat"]))
    cfg = {"service_types": [{"name": "Review", "minutes": 30, "price": 60, "days": [0]}]}
    patch, _ = cards.diary_patch(card, [card["lines"][0]["id"]], cfg)
    assert patch["service_types"][0]["price"] is None
    assert patch["service_types"][0]["days"] == [5]


def test_partial_saved_row_change_carries_its_existing_timing_and_duration():
    card = start(append("service", name="Review", price=80))
    cfg = {"service_types": [{"name": "Review", "minutes": 120, "timing": "day", "price": 50}]}
    patch, _ = cards.diary_patch(card, [card["lines"][0]["id"]], cfg)
    assert patch["service_types"] == [{"name": "Review", "minutes": 120, "timing": "day", "price": 80}]
    accepted, _ = cards.accept(card, 1, "accept-1", [card["lines"][0]["id"]], cfg)
    assert accepted["lines"][0]["problems"] == []
    assert "120 min" in accepted["lines"][0]["display"]
    with pytest.raises(cards.Invalid):
        cards.diary_patch(card, [card["lines"][0]["id"]], {})


def test_partial_accept_cannot_reference_an_unaccepted_resource():
    card = start(service(resource="Space"), append("resource", name="Space", count=2))
    ids = [v["id"] for v in card["lines"]]
    with pytest.raises(cards.Invalid, match="Space"):
        cards.diary_patch(card, ids[:1], {})
    patch, _ = cards.diary_patch(card, ids, {})
    assert patch["service_types"][0]["resource"] == "Space"
    assert patch["resource_types"][0]["count"] == 2


def test_existing_combined_service_checked_when_a_component_changes():
    card = start(service(name="A", minutes=90))
    cfg = {"service_types": [{"name": "A", "minutes": 30}, {"name": "B", "minutes": 30},
                             {"name": "A + B", "minutes": 60}]}
    with pytest.raises(cards.Invalid, match="combined"):
        cards.diary_patch(card, [card["lines"][0]["id"]], cfg)


def test_duplicate_and_invalid_rows_cannot_be_silently_combined():
    with pytest.raises(cards.Invalid, match="more than once"):
        start(service(), service(minutes=90))
    bad = start(service(minutes=1))
    assert bad["lines"][0]["problems"]
    with pytest.raises(cards.Invalid):
        cards.diary_patch(bad, [bad["lines"][0]["id"]], {})


def test_no_implicit_write_adapter_for_other_pages():
    card = start(append("field", label="Detail", required=True), page="booking_details")
    with pytest.raises(cards.Invalid, match="not available"):
        cards.diary_patch(card, [card["lines"][0]["id"]], {})


def test_inferred_sources_carry_bounded_evidence_and_other_kinds_stay_plain():
    patch = append("field", label="Repair", required=True)
    patch["sources"]["required"] = {"kind": "inferred", "reference": "Your site asks first",
                                    "evidence": ["source_text:abc"], "check": True}
    card = start(patch, page="booking_details")
    assert card["lines"][0]["sources"]["required"]["evidence"] == ["source_text:abc"]
    for bad in ({"kind": "owner_said", "reference": "x", "evidence": []},
                {"kind": "inferred", "reference": "x", "evidence": ["a", "b", "c", "d"]},
                {"kind": "inferred", "reference": "x", "check": False},
                {"kind": "guessed", "reference": "x"}):
        broken = append("field", label="Repair", required=True)
        broken["sources"]["required"] = bad
        with pytest.raises(cards.Invalid):
            start(broken, page="booking_details")
