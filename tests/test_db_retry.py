"""The shared Supabase client resends a timed-out read once, on a fresh connection, with a shorter
read timeout (live 2026-10-14: single stalled reads ended voice sessions). Writes and RPCs are
never resent after a read that may already have been processed.

These tests run a real postgrest RequestConfig over an httpx MockTransport with the same event
hooks the production client carries, so nothing opens a socket."""
import logging

import httpx
import pytest
from postgrest.base_request_builder import RequestConfig
from yarl import URL

from app import db

TABLE = "https://x.supabase.co/rest/v1/t"
RPC = "https://x.supabase.co/rest/v1/rpc/f"


@pytest.fixture(autouse=True)
def clean_state():
    db._retry_counts.clear()
    db._retry_read.seconds = None
    yield
    db._retry_counts.clear()
    db._retry_read.seconds = None


def make(steps, method="GET", path=TABLE):
    """A request config whose transport plays `steps` in order: an exception instance is raised, a
    Response is returned. `seen` counts calls and `timeouts` records the read timeout of each."""
    state = {"seen": 0, "timeouts": []}

    def handler(request):
        step = steps[state["seen"]]
        state["seen"] += 1
        state["timeouts"].append(request.extensions["timeout"]["read"])
        if isinstance(step, Exception):
            step.request = request
            raise step
        return step

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(20.0),
                          event_hooks={"request": [db._stamp_request], "response": [db._count_response]})
    req = RequestConfig(client, URL(path), method, httpx.Headers(), httpx.QueryParams({"select": "email"}),
                        None, None if method in ("GET", "HEAD") else {"a": 1})
    return req, state


def ok():
    return httpx.Response(200, json=[])


def test_a_timed_out_read_is_resent_once_on_a_shorter_read_timeout():
    req, state = make([httpx.ReadTimeout("read timed out"), ok()])
    response = db._send_retrying_stale_connections(req)
    assert response.status_code == 200
    assert state["seen"] == 2
    assert state["timeouts"] == [20.0, 8.0]
    assert db._retry_counts == {"ReadTimeout:recovered": 1}
    assert getattr(db._retry_read, "seconds", None) is None
    # the shortened limit belongs to the resend only: the next plain request is back on the default
    after = make([ok()])
    db._send_retrying_stale_connections(after[0])
    assert after[1]["timeouts"] == [20.0]


@pytest.mark.parametrize("method,path", [("POST", TABLE), ("PATCH", TABLE), ("DELETE", TABLE), ("POST", RPC)])
def test_a_timed_out_write_or_rpc_is_never_resent(method, path):
    req, state = make([httpx.ReadTimeout("read timed out"), ok()], method=method, path=path)
    with pytest.raises(httpx.ReadTimeout):
        db._send_retrying_stale_connections(req)
    assert state["seen"] == 1
    assert db._retry_counts == {}
    assert getattr(db._retry_read, "seconds", None) is None


def test_a_second_timeout_propagates_and_is_counted_failed():
    req, state = make([httpx.ReadTimeout("read timed out"), httpx.ReadTimeout("read timed out again")])
    with pytest.raises(httpx.ReadTimeout):
        db._send_retrying_stale_connections(req)
    assert state["seen"] == 2
    assert state["timeouts"] == [20.0, 8.0]
    assert db._retry_counts == {"ReadTimeout:failed": 1}
    assert getattr(db._retry_read, "seconds", None) is None


def test_stale_connection_errors_still_resend_any_method():
    # today's behaviour, pinned: the request never completed at the server, so a write is safe to resend
    req, state = make([httpx.ConnectError("connection refused"), ok()], method="POST")
    response = db._send_retrying_stale_connections(req)
    assert response.status_code == 200
    assert state["seen"] == 2
    # a connection failure resends on the default read timeout, not the shortened one
    assert state["timeouts"] == [20.0, 20.0]
    assert db._retry_counts == {"ConnectError:recovered": 1}


def test_a_read_error_on_a_write_still_propagates():
    req, state = make([httpx.ReadError("connection reset"), ok()], method="POST")
    with pytest.raises(httpx.ReadError):
        db._send_retrying_stale_connections(req)
    assert state["seen"] == 1
    assert db._retry_counts == {}


def test_the_retry_log_names_the_class_not_the_request(caplog):
    req, _ = make([httpx.ReadTimeout("read timed out"), ok()])
    with caplog.at_level(logging.WARNING, logger=db.log.name):
        db._send_retrying_stale_connections(req)
    lines = [r.getMessage() for r in caplog.records if r.name == db.log.name]
    assert len(lines) == 1
    assert "ReadTimeout" in lines[0]
    assert "supabase.co" not in lines[0]
    assert "select" not in lines[0]
