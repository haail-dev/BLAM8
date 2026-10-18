"""Versioned receipt fixtures for tests of completed proposal consumers."""
from datetime import datetime, timezone


def execution_result(pid, execution_input=None, state="executing", **result):
    return {**result, "execution": {"version": 2, "id": "test-execution", "operation_id": pid,
        "state": state, "started_at": datetime.now(timezone.utc).isoformat(), "input": execution_input or {}}}
