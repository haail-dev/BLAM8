"""Business-configured call forms. Gathered statements never prove external actions."""
import json
from copy import deepcopy
from jsonschema import Draft202012Validator, FormatChecker

from app.telephone_store import StoreError


class ObjectiveError(ValueError):
    pass


def _bounded(value, limit=32768):
    try:
        if len(json.dumps(value, allow_nan=False)) > limit:
            raise ValueError()
        nodes = [value]
        count = 0
        while nodes:
            node = nodes.pop()
            count += 1
            if count > 2000:
                raise ValueError()
            if isinstance(node, dict):
                if any(k in node for k in ("pattern", "patternProperties")):
                    raise ValueError()  # Untrusted regular expressions may monopolise a worker.
                if any(k in node for k in ("$ref", "$dynamicRef", "$recursiveRef")):
                    raise ValueError()  # Inline schemas only: no network resolution or reference cycles.
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)
    except (ValueError, TypeError, RecursionError):
        raise ObjectiveError("invalid_or_unsupported_form") from None


def validate_template(spec):
    _bounded(spec)
    if not isinstance(spec, dict) or set(spec) - {
            "name", "objective", "provided_facts", "fields_schema", "success_schema",
            "permitted_actions", "required_receipts"}:
        raise ObjectiveError("invalid_template")
    if any(not isinstance(spec.get(k), str) or not 1 <= len(spec[k].strip()) <= limit
           for k, limit in (("name", 100), ("objective", 1000))):
        raise ObjectiveError("invalid_template")
    if not isinstance(spec.get("provided_facts", {}), dict):
        raise ObjectiveError("invalid_template")
    fields = spec.get("fields_schema")
    success = spec.get("success_schema", {})
    if not isinstance(fields, dict) or fields.get("type") != "object" or not isinstance(success, dict):
        raise ObjectiveError("invalid_template")
    allowed = {"save_message", "request_handover", "record_fields", "end_call"}
    actions = spec.get("permitted_actions", [])
    receipts = spec.get("required_receipts", [])
    if (not isinstance(actions, list) or not all(isinstance(x, str) for x in actions)
            or len(actions) != len(set(actions)) or not set(actions) <= allowed
            or not isinstance(receipts, list) or not all(isinstance(x, str) for x in receipts)
            or not set(receipts) <= {"message_delivered", "handover_answered"}):
        raise ObjectiveError("unsupported_action")
    try:
        Draft202012Validator.check_schema(fields)
        Draft202012Validator.check_schema(success)
    except Exception:
        raise ObjectiveError("invalid_form_schema") from None
    return deepcopy(spec)


def validate_facts(spec, values, *, partial=False):
    _bounded(values, 16384)
    if not isinstance(values, dict):
        raise ObjectiveError("invalid_gathered_fields")
    schema = deepcopy(spec["fields_schema"])
    if partial:
        # Required criteria constrain completion, not a caller's ability to stop.
        def optional(node):
            if isinstance(node, dict):
                node.pop("required", None)
                for value in node.values():
                    optional(value)
            elif isinstance(node, list):
                for value in node:
                    optional(value)
        optional(schema)
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = list(validator.iter_errors(values))
        if errors:
            raise ObjectiveError("gathered_fields_do_not_match_form")
        if not partial and not Draft202012Validator(spec.get("success_schema", {}),
                format_checker=FormatChecker()).is_valid(values):
            raise ObjectiveError("completion_criteria_unmet")
    except ObjectiveError:
        raise
    except Exception:
        raise ObjectiveError("invalid_gathered_fields") from None
    return deepcopy(values)


class ObjectiveStore:
    def __init__(self, client=None):
        if client is None:
            from app import db
            client = db.db()
        self.client = client

    def command(self, tenant, user, action, payload=None):
        try:
            data = self.client.rpc("telephone_objective_command", {
                "p_tenant": tenant, "p_user": user, "p_action": action,
                "p_payload": payload or {},
            }).execute().data
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except Exception:
            raise StoreError("store_unavailable") from None

    def record(self, tenant, call_id, values, source_id):
        state = self.command(tenant, None, "state", {"call_id": call_id})
        spec = state["template"]
        merged = {**state.get("facts", {}), **values}
        validate_facts(spec, merged, partial=True)
        return self.command(tenant, None, "facts", {"call_id": call_id,
            "expected_revision": state["revision"], "facts": merged,
            "source_id": source_id, "provided_keys": list(values), "delta": values})

    def finish(self, tenant, call_id, reason):
        state = self.command(tenant, None, "state", {"call_id": call_id})
        valid = True
        try:
            validate_facts(state["template"], state.get("facts", {}))
        except ObjectiveError:
            valid = False
        return self.command(tenant, None, "finish", {"call_id": call_id,
            "expected_revision": state["revision"], "criteria_valid": valid, "reason": reason})
