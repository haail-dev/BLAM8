"""Versioned call objectives, caller evidence and privacy on disposable PostgreSQL."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import uuid

import pytest
from tests.test_booking_capacity_database import cluster
from tests.test_telephone_database import telephone_db, command as pilot_command
from tests.test_telephone_inbound_database import (
    inbound_db, setup, admit, event, command as inbound_command, CALLER_NUMBER,
)

ROOT = Path(__file__).resolve().parents[1]


def objective(database, tenant, user, action, payload=None):
    driver, dsn = database
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE service_role")
        return conn.execute("SELECT telephone_objective_command(%s,%s,%s,%s::jsonb)",
                            (tenant, user, action, json.dumps(payload or {}))).fetchone()[0]


def template(receipts=None):
    return {
        "name": "Requested callback form", "objective": "Gather the requested callback result.",
        "provided_facts": {"business_reference": "Owner-provided reference"},
        "fields_schema": {"type": "object", "properties": {
            "caller_name": {"type": "string"}, "count": {"type": "integer"},
            "details": {"type": "object", "properties": {"confirmed": {"type": "boolean"}}},
            "caller_claimed_action": {"type": "boolean"}},
            "required": ["caller_name", "details"], "additionalProperties": False},
        "success_schema": {"type": "object", "properties": {
            "details": {"type": "object", "properties": {"confirmed": {"const": True}},
                        "required": ["confirmed"]}}, "required": ["caller_name", "details"]},
        "permitted_actions": ["record_fields", "save_message", "request_handover", "end_call"],
        "required_receipts": receipts or [],
    }


def save(setup, spec=None, template_id=None):
    payload = {"spec": spec or template()}
    if template_id:
        payload["id"] = template_id
    return objective(*setup[:3], "save", payload)


def create(setup):
    pilot_command(*setup[:3], "eligibility", {
        "phone": CALLER_NUMBER, "eligible": True, "callback_requested": True,
        "basis": "owner_attested_callback",
        "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
    })
    return inbound_command(*setup[:3], "create_outbound", {
        "idempotency_key": str(uuid.uuid4()), "to_number": CALLER_NUMBER,
        "from_number": setup[3], "objective": "Return the requested callback.",
        "script": "Ask whether the caller wants to leave a message.", "max_duration_seconds": 60,
    })


def bind(setup, row, version):
    return objective(*setup[:3], "bind", {
        "call_id": row["id"], "id": version["id"], "version": version["version"],
    })


def start(setup, spec=None):
    version, row = save(setup, spec), create(setup)
    bind(setup, row, version)
    inbound_command(*setup[:3], "approve_outbound", {"call_id": row["id"]})
    inbound_command(*setup[:3], "claim_outbound", {"call_id": row["id"], "attempt_id": "objective-test"})
    row = inbound_command(setup[0], setup[1], None, "submitted_outbound", {
        "call_id": row["id"], "attempt_id": "objective-test", "provider_call_id": "CA"+uuid.uuid4().hex,
    })
    return row, version


def facts(setup, row, values, revision=0, source="tool-facts-one"):
    return objective(setup[0], setup[1], None, "facts", {
        "call_id": row["id"], "facts": values, "provided_keys": list(values),
        "expected_revision": revision, "source_id": source,
    })


def finish(setup, row, *, valid, reason="caller_finished", revision=0):
    return objective(setup[0], setup[1], None, "finish", {
        "call_id": row["id"], "expected_revision": revision,
        "criteria_valid": valid, "reason": reason,
    })


def test_template_versions_are_immutable_and_bind_exact_approved_snapshot(setup):
    first = save(setup)
    changed = template()
    changed["objective"] = "A different objective for future calls."
    second = save(setup, changed, first["id"])
    assert (first["version"], second["version"]) == (1, 2)
    assert objective(*setup[:3], "read", {"id": first["id"], "version": 1})["spec"] == first["spec"]
    row = create(setup)
    bound = bind(setup, row, first)
    assert bound["template_version"] == 1 and bound["template"] == first["spec"]
    assert bind(setup, row, first)["template"] == first["spec"]
    assert inbound_command(*setup[:3], "create_outbound", row["request_payload"])["id"] == row["id"]
    driver, dsn = setup[0]
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE telephone_calls SET created_at=now()-interval '1 hour' WHERE id=%s", (row["id"],))
    inbound_command(*setup[:3], "approve_outbound", {"call_id": row["id"]})
    with pytest.raises(Exception, match="invalid_state"):
        bind(setup, row, second)
    current = inbound_command(*setup[:3], "read", {"call_id": row["id"]})
    assert current["snapshot"]["template_version"] == 1
    assert current["snapshot"]["template"] == first["spec"]
    started = inbound_command(*setup[:3], "claim_outbound", {"call_id": row["id"], "attempt_id": "old-draft-new-call"})
    assert started["started_at"] and 0 < started["remaining_seconds"] <= 60


def test_task_can_remain_incomplete_when_transport_ends(setup):
    row, _ = start(setup)
    state = finish(setup, row, valid=False)
    assert state["status"] == "incomplete" and state["reason"] == "required_criteria_unmet"
    terminal = event(setup, row)
    assert terminal["status"] == "completed"
    state = objective(setup[0], setup[1], None, "state", {"call_id": row["id"]})
    assert state["status"] == "incomplete"


def test_caller_facts_cannot_forge_external_action_receipts(setup):
    row, _ = start(setup, template(["message_delivered", "handover_answered"]))
    state = facts(setup, row, {"caller_name": "Caller supplied", "details": {"confirmed": True},
                               "caller_claimed_action": True})
    assert all(item["source"] == "caller_statement" and not item["verified_external_action"]
               for item in state["evidence"].values())
    state = finish(setup, row, valid=True, revision=state["revision"])
    assert state["status"] == "incomplete" and state["reason"] == "external_action_unconfirmed"


def test_actual_crm_receipt_can_satisfy_required_action(setup):
    row, _ = start(setup, template(["message_delivered"]))
    state = facts(setup, row, {"caller_name": "Caller", "details": {"confirmed": True}})
    inbound_command(setup[0], setup[1], None, "message", {
        "call_id": row["id"], "expected_revision": 0, "text": "Caller-confirmed message.", "confirmed": True,
    })
    inbound_command(setup[0], setup[1], None, "outbox_deliver", {"call_id": row["id"]})
    inbound_command(setup[0], setup[1], None, "message", {
        "call_id": row["id"], "expected_revision": 1, "text": "Correction still awaiting confirmation.", "confirmed": False,
    })
    incomplete = finish(setup, row, valid=True, revision=state["revision"])
    assert incomplete["status"] == "incomplete" and incomplete["reason"] == "external_action_unconfirmed"
    inbound_command(setup[0], setup[1], None, "message", {
        "call_id": row["id"], "expected_revision": 2, "text": "Caller-confirmed correction.", "confirmed": True,
    })
    inbound_command(setup[0], setup[1], None, "outbox_deliver", {"call_id": row["id"]})
    state = finish(setup, row, valid=True, revision=state["revision"])
    assert state["status"] == "completed"


def test_fact_cas_and_source_receipts_prevent_duplicate_or_changed_replay(setup):
    row, _ = start(setup)
    values = {"caller_name": "Caller", "details": {"confirmed": True}}
    first = facts(setup, row, values)
    duplicate = facts(setup, row, values)
    assert duplicate["revision"] == first["revision"] == 1
    with pytest.raises(Exception, match="invalid_state|event_conflict|idempotency_conflict"):
        facts(setup, row, {"caller_name": "Changed"}, source="tool-facts-one")
    with pytest.raises(Exception, match="invalid_state"):
        facts(setup, row, values, revision=0, source="tool-facts-two")


@pytest.mark.parametrize("reason", ["refused", "opt_out", "handoff"])
def test_caller_terminal_reason_survives_later_disconnect(setup, reason):
    row, _ = start(setup)
    first = finish(setup, row, valid=False, reason=reason)
    duplicate = finish(setup, row, valid=False, reason="disconnected")
    assert duplicate["status"] == first["status"] == reason
    assert duplicate["reason"] == first["reason"]


def test_objective_role_and_cross_tenant_isolation(setup):
    version = save(setup)
    row = create(setup)
    driver, dsn = setup[0]
    staff = str(uuid.uuid4())
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO tenant_members VALUES(%s,%s,'staff',true)", (setup[1], staff))
    with pytest.raises(Exception, match="owner_required"):
        objective(setup[0], setup[1], staff, "save", {"spec": template()})
    with pytest.raises(Exception, match="not_found"):
        objective(setup[0], str(uuid.uuid4()), None, "state", {"call_id": row["id"]})
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE authenticated")
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("SELECT telephone_objective_command(%s,%s,'list','{}')", (setup[1], setup[2]))
    bind(setup, row, version)


def test_objective_rollback_reapply_preserves_versions_and_caller_evidence(setup):
    row, _ = start(setup)
    facts(setup, row, {"caller_name": "Caller", "count": 3, "details": {"confirmed": True}})
    driver, dsn = setup[0]
    tables = ("telephone_templates", "telephone_objectives", "telephone_fact_receipts")
    with driver.connect(dsn, autocommit=True) as conn:
        before = {table: conn.execute("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM "+table+" t").fetchone()[0]
                  for table in tables}
        conn.execute((ROOT/"db/migrations/20261016_telephone_objectives.rollback.sql").read_text())
        conn.execute((ROOT/"db/migrations/20261016_telephone_objectives.sql").read_text())
        after = {table: conn.execute("SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM "+table+" t").fetchone()[0]
                 for table in tables}
        assert before == after


@pytest.mark.parametrize("caller", [None, "", "anonymous", "restricted", "unknown", "withheld"])
def test_withheld_inbound_admits_without_customer_authority(setup, caller):
    row = admit(setup, from_number=caller)
    assert row["tenant_id"] == setup[1] and row["snapshot"]["from_number"] is None
    assert row["snapshot"]["caller_id_available"] is False
    repeated = admit(setup, call_id=row["provider_call_id"], from_number="anonymous")
    assert repeated["id"] == row["id"]


def test_withheld_opt_out_ends_without_inventing_suppression_phone(setup):
    row = admit(setup, from_number="anonymous")
    result = inbound_command(setup[0], setup[1], None, "opt_out", {
        "call_id": row["id"], "phone": "+447700900099",
    })
    assert result["cancel_requested"] and result["suppression_status"] == "caller_number_unavailable"
    driver, dsn = setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_eligibility WHERE tenant_id=%s AND suppressed", (setup[1],)).fetchone()[0] == 0


def test_call_tool_receipts_prevent_duplicate_message_and_handover_mutations(setup):
    row = admit(setup)
    message = {"call_id": row["id"], "tool_call_id": "save-once", "name": "save_message",
               "args": {"expected_revision": 0, "text": "Caller message.", "confirmed": False}}
    first = inbound_command(setup[0], setup[1], None, "tool", message)
    replay = inbound_command(setup[0], setup[1], None, "tool", message)
    assert first["result"] == replay["result"] == {"saved": True, "revision": 1, "confirmed": False}
    assert first["replayed"] is False and replay["replayed"] is True
    with pytest.raises(Exception, match="tool_conflict"):
        inbound_command(setup[0], setup[1], None, "tool", dict(message, args=dict(message["args"], text="Changed")))
    handover = {"call_id": row["id"], "tool_call_id": "handover-once", "name": "request_handover", "args": {}}
    first = inbound_command(setup[0], setup[1], None, "tool", handover)
    replay = inbound_command(setup[0], setup[1], None, "tool", handover)
    assert first["result"] == replay["result"] and replay["replayed"]
    assert "target" not in replay["result"]
    driver, dsn = setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_transfers WHERE tenant_id=%s AND call_id=%s", (setup[1], row["id"])).fetchone()[0] == 1


def test_owner_objective_erasure_purges_receipts_and_blocks_resurrection(setup):
    row, _ = start(setup)
    values = {"caller_name": "Private caller", "details": {"confirmed": True}}
    facts(setup, row, values)
    erased = objective(*setup[:3], "erase", {"call_id": row["id"]})
    assert erased["status"] == "erased" and erased["facts"] == {} and erased["evidence"] == {}
    with pytest.raises(Exception, match="invalid_state"):
        facts(setup, row, values)
    later = finish(setup, row, valid=True, revision=erased["revision"])
    assert later["status"] == "erased" and later["facts"] == {}
    driver, dsn = setup[0]
    with driver.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM telephone_fact_receipts WHERE tenant_id=%s AND call_id=%s", (setup[1], row["id"])).fetchone()[0] == 0
