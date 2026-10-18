"""Offline adapter contract, including non-success and authenticated event boundaries."""
from dataclasses import asdict, replace
import json

import pytest

from app.telephone_provider import (AmbiguousSubmission, CallSpec, DefiniteFailure,
    ProviderEvent, get_provider, parse_event, sign_simulation_event, verify_simulation_signature)


def spec(**changes):
    return replace(CallSpec("call-1", "attempt-1", "+442079460123", "Return requested callback", 60),
                   **changes)


def event_body(**changes):
    event = replace(ProviderEvent("call-1", "sim_abc", "tenant-1", "event-1", 0, "completed"),
                    **changes)
    return json.dumps(asdict(event)).encode()


def test_submission_is_explicit_simulation_idempotent_and_revision_bound():
    provider = get_provider("simulated")
    first = provider.submit(spec())
    assert first.simulated is True
    assert first == provider.submit(spec()) == get_provider("simulated").submit(spec())
    assert first != provider.submit(spec(attempt_id="attempt-2"))
    with pytest.raises(DefiniteFailure, match="attempt_changed"):
        provider.submit(spec(objective="Different approved objective"))


@pytest.mark.parametrize("name", ["twilio", "bland", "live", "", None])
def test_real_providers_fail_closed(name):
    with pytest.raises(DefiniteFailure, match="provider_unavailable"):
        get_provider(name)


def test_ambiguous_submission_is_not_a_definite_failure():
    with pytest.raises(AmbiguousSubmission, match="submission_unknown"):
        get_provider("simulated").submit(spec(scenario="ambiguous"))


@pytest.mark.parametrize("scenario,terminal", [("completed", "completed"), ("busy", "busy"),
    ("failed", "failed"), ("no_answer", "no_answer"), ("timeout", "no_answer")])
def test_scenarios_provide_stable_ordered_events_without_objective_success(scenario, terminal):
    provider = get_provider("simulated")
    request = spec(scenario=scenario)
    submission = provider.submit(request)
    events = provider.events(request, submission, "tenant-1")
    assert events == provider.events(request, submission, "tenant-1")
    assert [e.sequence for e in events] == list(range(len(events)))
    assert len({e.event_id for e in events}) == len(events)
    assert events[-1].status == terminal
    assert all(e.outcome == "none" and e.tenant_id == "tenant-1" for e in events)
    assert all(e.provider_call_id == submission.provider_call_id for e in events)
    assert provider.cancel(submission.provider_call_id) is True
    assert events[-1].status == terminal  # cancel acknowledgment does not invent terminal events


def test_signature_binds_exact_bytes_and_rejects_missing_or_wrong_keys():
    raw = event_body()
    key = "offline-test-key-" * 3
    signature = sign_simulation_event(raw, key)
    assert verify_simulation_signature(raw, signature, key)
    assert not verify_simulation_signature(raw + b" ", signature, key)
    assert not verify_simulation_signature(raw, signature, "another-secret-" * 3)
    assert not verify_simulation_signature(raw, "", key)
    assert not verify_simulation_signature(raw, signature, "")
    assert parse_event(raw).outcome == "none"


@pytest.mark.parametrize("changes", [{"sequence": True}, {"sequence": -1},
    {"duration_seconds": 7201}, {"cost_minor": -1}, {"status": "success"},
    {"outcome": "paid"}, {"message_summary": "x" * 1001}, {"tenant_id": ""}])
def test_event_validation_rejects_unbounded_or_confused_values(changes):
    with pytest.raises(DefiniteFailure, match="invalid_event"):
        parse_event(event_body(**changes))


@pytest.mark.parametrize("raw", [b"[]", b"not-json", b"{}", b"x" * 8193,
    b'{"call_id":"first","call_id":"second"}', b'{"secret":"not-permitted"}'])
def test_invalid_envelopes_fail_with_generic_error(raw):
    with pytest.raises(DefiniteFailure, match="^invalid_event$"):
        parse_event(raw)


@pytest.mark.parametrize("changes", [{"destination": "local-number"},
    {"max_duration_seconds": True}, {"max_duration_seconds": 0}, {"scenario": "real"}])
def test_invalid_specs_cannot_submit(changes):
    with pytest.raises(DefiniteFailure, match="invalid_call_spec"):
        get_provider("simulated").submit(spec(**changes))
