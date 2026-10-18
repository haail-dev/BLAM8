"""Callee DTMF consent, rather than voicemail/network connection, proves handover."""
import uuid

import pytest
from tests.test_booking_capacity_database import cluster
from tests.test_telephone_database import telephone_db, command as pilot_command
from tests.test_telephone_inbound_database import (
    inbound_db, setup, admit, command, TRANSFER_NUMBER,
)
from tests.test_telephone_objectives_database import start, template, facts, finish


def request(setup, row):
    return command(setup[0], setup[1], None, "transfer_request", {"call_id": row["id"]})["transfer"]


def evidence(row, transfer):
    return {"call_id": row["id"], "transfer_id": transfer["id"],
            "provider_leg_id": "CA"+uuid.uuid4().hex,
            "parent_call_id": row["provider_call_id"], "target_number": TRANSFER_NUMBER}


def test_network_answer_or_voicemail_cannot_complete_human_handover(setup):
    row, _ = start(setup, template(["handover_answered"]))
    transfer = request(setup, row)
    payload = evidence(row, transfer)
    network = command(setup[0], setup[1], None, "transfer_update", dict(payload, status="answered", sequence=0))
    assert network["transfer"]["network_answered"] is True
    assert network["transfer"]["human_confirmed"] is False
    assert network["transfer"]["status"] == "accepted"
    state = facts(setup, row, {"caller_name": "Caller", "details": {"confirmed": True}})
    incomplete = finish(setup, row, valid=True, revision=state["revision"])
    assert incomplete["status"] == "incomplete" and incomplete["reason"] == "external_action_unconfirmed"
    ended = command(setup[0], setup[1], None, "finish_handover", payload)
    assert ended["transfer"]["status"] == "failed" and not ended["transfer"]["human_confirmed"]


def test_dtmf_acceptance_can_precede_network_callback_and_is_idempotent(setup):
    row = admit(setup)
    transfer = request(setup, row)
    payload = evidence(row, transfer)
    accepted = command(setup[0], setup[1], None, "human_accept", payload)
    assert accepted["transfer"]["status"] == "answered"
    assert accepted["transfer"]["network_answered"] and accepted["transfer"]["human_confirmed"]
    replay = command(setup[0], setup[1], None, "human_accept", payload)
    assert replay["transfer"] == accepted["transfer"]
    later = command(setup[0], setup[1], None, "transfer_update", dict(payload, status="answered", sequence=0))
    assert later["transfer"]["status"] == "answered" and later["transfer"]["human_confirmed"]


def test_dtmf_acceptance_rejects_forged_leg_parent_target_and_cancelled_call(setup):
    row = admit(setup)
    transfer = request(setup, row)
    payload = evidence(row, transfer)
    command(setup[0], setup[1], None, "transfer_update", dict(payload, status="accepted", sequence=0))
    for field, forged in (("parent_call_id", "CAwrong"), ("target_number", "+447700900099"),
                          ("provider_leg_id", "CAdifferent")):
        with pytest.raises(Exception, match="provider_mismatch"):
            command(setup[0], setup[1], None, "human_accept", dict(payload, **{field: forged}))
    current = command(setup[0], setup[1], setup[2], "read", {"call_id": row["id"]})
    assert not current["transfer"]["human_confirmed"]
    command(setup[0], setup[1], setup[2], "cancel", {"call_id": row["id"]})
    with pytest.raises(Exception, match="invalid_state"):
        command(setup[0], setup[1], None, "human_accept", payload)


def test_late_callbacks_and_coarse_end_preserve_confirmed_human_acceptance(setup):
    row = admit(setup)
    transfer = request(setup, row)
    payload = evidence(row, transfer)
    command(setup[0], setup[1], None, "human_accept", payload)
    for status in ("accepted", "failed", "timed_out", "answered"):
        state = command(setup[0], setup[1], None, "transfer_update", dict(payload, status=status, sequence=99))
        assert state["transfer"]["status"] == "answered" and state["transfer"]["human_confirmed"]
    ended = command(setup[0], setup[1], None, "finish_handover", payload)
    assert ended["transfer"]["status"] == "answered" and ended["transfer"]["human_confirmed"]
    with pytest.raises(Exception, match="handover_already_requested"):
        command(setup[0], setup[1], None, "transfer_request", {"call_id": row["id"]})


def test_simulation_rpc_cannot_read_or_mutate_actual_provider_calls(setup):
    row = admit(setup)
    assert pilot_command(*setup[:3], "list")["calls"] == []
    for action in ("read", "approve", "cancel", "event"):
        with pytest.raises(Exception, match="not_found|invalid_state"):
            pilot_command(*setup[:3], action, {"call_id": row["id"]})
    current = command(*setup[:3], "read", {"call_id": row["id"]})
    assert current["status"] == "initiated" and current["cancel_requested"] is False
    assert current["reserved_minor"] == row["reserved_minor"]
