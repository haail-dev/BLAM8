"""Single-process cooperative voice cancellation; never undoes accepted work.

The boundary is conservative: entering an internally-writing helper commits
the turn even while that helper is awaiting generation. Keep one web worker
until these states and the speech allowlist have shared storage.
"""
from dataclasses import dataclass
from threading import RLock
import time


class Cancelled(Exception):
    pass


@dataclass
class Turn:
    touched: float
    status: str = "running"
    boundary: str = ""
    registered: bool = False


TTL = 900
MAX_TURNS = 2048
_states: dict[tuple[str, str, str], Turn] = {}
_lock = RLock()


def key(tenant_id, user_id, turn_id):
    return (tenant_id, user_id or "", turn_id)


def register(identity):
    with _lock:
        now = time.monotonic()
        for k, state in list(_states.items()):
            if state.status in {"completed", "cancelled"} and now - state.touched > TTL:
                del _states[k]
        if identity in _states:
            return _states[identity]
        if len(_states) >= MAX_TURNS:
            raise RuntimeError("Voice turn capacity reached; try again shortly")
        state = Turn(now)
        _states[identity] = state
        return state


def begin(identity):
    with _lock:
        state = register(identity)
        if state.registered:
            raise RuntimeError("Duplicate voice turn")
        state.registered = True
        return state


def checkpoint(state):
    with _lock:
        if state.status == "cancelled":
            raise Cancelled()


def commit(state, boundary):
    with _lock:
        checkpoint(state)
        if state.status == "completed":
            raise Cancelled()
        state.status = "committing"
        state.boundary = state.boundary or boundary
        state.touched = time.monotonic()


def cancel(identity):
    with _lock:
        state = register(identity)  # preserve a discard arriving before chat
        if state.status in {"committing", "completed"}:
            return {"ok": True, "status": "too_late", "boundary": state.boundary}
        state.status = "cancelled"
        state.touched = time.monotonic()
        return {"ok": True, "status": "cancelled"}


def complete(state):
    with _lock:
        if state.status != "cancelled":
            state.status = "completed"
        state.touched = time.monotonic()
