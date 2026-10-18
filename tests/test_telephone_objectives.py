"""Generic call forms validate statements without granting action authority."""
from copy import deepcopy
import pytest
from app.telephone_objectives import ObjectiveError, validate_template, validate_facts


def form():
    return {"name": "Collect requested information", "objective": "Gather the configured details.",
        "provided_facts": {"business_reference": "known fact"},
        "fields_schema": {"type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1}, "allowed": {"type": "boolean"},
            "date": {"type": "string", "format": "date"}, "choice": {"enum": ["a", "b"]},
            "items": {"type": "array", "items": {"type": "number"}},
            "details": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            "required": ["count", "allowed", "details"], "additionalProperties": False},
        "success_schema": {"properties": {"allowed": {"const": True}}},
        "permitted_actions": ["record_fields", "end_call"], "required_receipts": []}


def test_generic_nested_types_and_completion_criteria_are_preserved():
    spec = validate_template(form())
    facts = {"count": 2, "allowed": True, "date": "2026-10-16", "choice": "a", "items": [1.2, 3], "details": {"text": "caller statement"}}
    assert validate_facts(spec, facts) == facts
    assert spec == form()


def test_partial_answers_do_not_force_caller_to_finish_the_form():
    spec = form()
    assert validate_facts(spec, {"details": {}}, partial=True) == {"details": {}}
    with pytest.raises(ObjectiveError):
        validate_facts(spec, {"details": {}})
    assert spec["fields_schema"]["properties"]["details"]["required"] == ["text"]


@pytest.mark.parametrize("value", ["2", 2.5, False, None])
def test_invalid_integer_answers_are_not_silently_coerced(value):
    with pytest.raises(ObjectiveError):
        validate_facts(form(), {"count": value}, partial=True)


def test_complete_fields_with_failed_criteria_remain_unfulfilled():
    with pytest.raises(ObjectiveError, match="completion_criteria_unmet"):
        validate_facts(form(), {"count": 1, "allowed": False, "details": {"text": "value"}})


@pytest.mark.parametrize("key", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize("reference", ["https://invalid.example/never-fetch", "#/$defs/self"])
def test_form_cannot_fetch_network_schemas_or_resolve_reference_cycles(key, reference):
    spec = form()
    spec["fields_schema"]["properties"]["external"] = {key: reference}
    with pytest.raises(ObjectiveError):
        validate_template(spec)


@pytest.mark.parametrize("schema", [{"pattern": "(a+)+$"}, {"patternProperties": {".*": {}}}])
def test_untrusted_regex_execution_is_unavailable(schema):
    spec = form()
    spec["fields_schema"]["properties"]["unsafe"] = schema
    with pytest.raises(ObjectiveError):
        validate_template(spec)


@pytest.mark.parametrize("action", ["send_email", "make_payment", "book_anything", {"owner": True}])
def test_declared_actions_do_not_invent_unsupported_business_tools(action):
    spec = form()
    spec["permitted_actions"] = [action]
    with pytest.raises(ObjectiveError):
        validate_template(spec)


def test_invalid_dates_and_unconfigured_extra_fields_are_rejected():
    with pytest.raises(ObjectiveError):
        validate_facts(form(), {"date": "not a date"}, partial=True)
    with pytest.raises(ObjectiveError):
        validate_facts(form(), {"external_payment_succeeded": True}, partial=True)


def test_form_and_answer_sizes_are_bounded():
    spec = form()
    spec["provided_facts"]["large"] = "x" * 32768
    with pytest.raises(ObjectiveError):
        validate_template(spec)
    with pytest.raises(ObjectiveError):
        validate_facts(form(), {"details": {"text": "x" * 16384}}, partial=True)
