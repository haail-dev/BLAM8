from copy import deepcopy
import pytest
from app import setup_cards as cards
from tests.test_setup_cards import start, append


def apply(card, snapshot):
    return cards.accept(card, card["revision"], "accept", [r["id"] for r in card["lines"]], snapshot)


def subject(**kw):
    return append("field", **({"kind": "Asset", "label": "Code", "type": "text",
                              "required": False, "durability": "once", "gate": False} | kw))


def test_booking_correction_preserves_documents_scopes_and_stay_field():
    from app.intake import STAY_FIELD
    old = {"label": "Evidence", "required": False, "doc": True, "services": ["Review"], "hint": "Old", "future": 7}
    cfg = {"intake_fields": [old, STAY_FIELD], "service_types": [{"name": "Review"}]}
    snapshot = {"config": cfg, "definitions": []}
    card, patch = apply(start(append("field", label="Evidence", hint="New"), page="booking_details"), snapshot)
    assert patch["intake_fields"] == [old | {"hint": "New"}, STAY_FIELD]
    assert patch["definitions"] == [] and card["status"] == "applied"
    assert snapshot["config"] == cfg and old["hint"] == "Old"


def test_remembered_link_preserved_and_new_definition_planned_without_io():
    old = {"label": "Reference", "required": True, "remember": "once", "crm_key": "stable_key"}
    definition = {"id": "d", "key": "stable_key", "field_type": "text", "durability": "once"}
    snapshot = {"config": {"intake_fields": [old]}, "definitions": [definition]}
    _, patch = apply(start(append("field", label="Reference", remember="confirm"),
                            append("field", label="Account code", required=False, remember="once"), page="booking_details"), snapshot)
    assert patch["intake_fields"][0]["crm_key"] == "stable_key"
    assert patch["definitions"][0]["key"] == patch["intake_fields"][1]["crm_key"] == "account_code"
    assert snapshot["definitions"] == [definition]
    assert len(patch["definitions"]) == 1
    _, stop = apply(start(append("field", label="Reference", remember="ask"), page="booking_details"), snapshot)
    assert "crm_key" not in stop["intake_fields"][0] and not stop["definitions"]


def test_new_customer_expiry_definition_is_visible_in_review():
    accepted, patch = apply(start(append("field", label="Due date", required=False, remember="expires"), page="booking_details"),
                            {"config": {}, "definitions": []})
    receipt = accepted["last_action"]["changes"][0]
    assert patch["definitions"][0]["field_type"] == "date"
    assert "30 days before" in receipt["notes"][1]
    assert receipt["review_before"] == {}
    assert receipt["review_after"]["remember"] == "expires"
    assert "crm_key" not in receipt["review_after"]


@pytest.mark.parametrize("fields,config,definitions", [
    ({"services": ["Missing"]}, {}, []),
    ({"kind": "document", "remember": "once"}, {}, []),
    ({"remember": "expires"}, {}, [{"key": "code", "field_type": "text"}]),
    ({"remember": "once"}, {}, [{"key": "code", "ladder": {"notify": "owner"}}]),
    ({"remember": "once"}, {}, [{"key": str(i)} for i in range(10)]),
])
def test_invalid_booking_links_never_produce_partial_plan(fields, config, definitions):
    proposal = start(append("field", **({"label": "Code", "required": False} | fields)), page="booking_details")
    with pytest.raises(cards.Invalid):
        apply(proposal, {"config": config, "definitions": definitions})
    assert proposal["status"] == "proposed"


@pytest.mark.parametrize("plural,saved", [(None, "Properties"), ("", "Properties"), ("Children", "Children")])
def test_new_kind_derives_an_unstated_plural_like_the_native_editor(plural, saved):
    group = {"label": "Property" if plural != "Children" else "Child", "cardinality": "many"}
    if plural is not None:
        group["plural"] = plural
    card = start(append("field", kind=group["label"], label="Address", type="text", required=False,
                        durability="once", gate=False), append("group", **group), page="dependants")
    _, patch = apply(card, {"config": {}, "groups": []})
    assert patch["groups"][0]["label_plural"] == saved


def test_new_kind_and_field_are_order_independent_with_stable_preview_id():
    card = start(subject(), append("group", label="Asset", plural="Assets", cardinality="many"), page="dependants")
    snapshot = {"config": {}, "groups": []}
    accepted, patch = apply(card, snapshot)
    assert apply(card, snapshot)[1] == patch
    assert patch["groups"][0]["fields"][0]["key"] == "code"
    assert accepted["status"] == "applied"
    group_review = next(c for c in accepted["last_action"]["changes"] if c["name"] == "group")
    assert set(group_review["review_after"]) == {"label", "plural", "cardinality"}
    with pytest.raises(cards.Invalid, match="together|one to ten"):
        cards.accept(card, 1, "accept", [card["lines"][1]["id"]], snapshot)


def test_subject_correction_preserves_keys_ladders_proof_and_unmentioned_fields():
    field = {"key": "original_key", "label": "Code", "type": "date", "required": True,
             "durability": "expires", "recall_days": 40, "gate": True, "proof": True, "set_by": "owner"}
    ladder = {"key": "progress", "label": "Progress", "type": "dropdown", "options": ["A", "B"],
              "durability": "confirm", "gate": False, "ladder": {"notify": "owner", "gates": []}}
    group = {"id": "saved-id", "label": "Asset", "label_plural": "Assets", "cardinality": "many", "fields": [field, ladder], "future": 9}
    snapshot = {"config": {}, "groups": [group]}
    original = deepcopy(snapshot)
    card = start(append("field", kind="Asset", label="Code", hint="Updated"), page="dependants")
    _, patch = apply(card, snapshot)
    assert patch["groups"] == [group | {"fields": [field | {"hint": "Updated"}, ladder]}]
    assert snapshot == original
    for correction in ({"gate": False}, {"type": "text"}):
        with pytest.raises(cards.Invalid):
            apply(start(append("field", kind="Asset", label="Code", **correction), page="dependants"), snapshot)
    with pytest.raises(cards.Invalid, match="Progress"):
        apply(start(append("field", kind="Asset", label="Progress", options=["A", "B", "C"]), page="dependants"), snapshot)


def test_new_field_cannot_alias_existing_stored_key():
    group = {"id": "g", "label": "Asset", "fields": [{"label": "Original", "key": "code"}]}
    with pytest.raises(cards.Invalid, match="stored key"):
        apply(start(subject(), page="dependants"), {"config": {}, "groups": [group]})
