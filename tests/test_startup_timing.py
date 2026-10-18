import asyncio
import json

import pytest
from starlette.requests import Request
from starlette.responses import Response
from app import startup_timing as timing


def capture(monkeypatch):
    rows = []
    monkeypatch.setattr(timing.log, "info", lambda template, payload: rows.append(json.loads(payload)))
    return rows


def test_disabled_and_bounded_content_free_instrumentation(monkeypatch):
    rows = capture(monkeypatch)
    @timing.instrument("history")
    def read(value):
        return value
    assert read("PRIVATE CONTENT") == "PRIVATE CONTENT" and rows == []
    state, token = timing.begin("panel")
    for _ in range(100):
        assert read("PRIVATE CONTENT") == "PRIVATE CONTENT"
        timing.mark("identity_cache_hit", True)
        timing.mark("PRIVATE CONTENT", "PRIVATE CONTENT")
    timing.finish(state, token, 200)
    assert len(rows) <= 64 and rows[-1]["stage"] == "complete"
    assert "PRIVATE CONTENT" not in json.dumps(rows)
    assert timing._current.get() is None


@pytest.mark.asyncio
async def test_context_isolation_and_thread_propagation(monkeypatch):
    rows = capture(monkeypatch)
    async def request():
        state, token = timing.begin("panel")
        def worker():
            with timing.span("history_rich"):
                return timing._current.get()["id"]
        try:
            assert await asyncio.to_thread(worker) == state["id"]
            return state["id"]
        finally:
            timing.finish(state, token, 200)
    ids = await asyncio.gather(request(), request())
    assert ids[0] != ids[1]
    assert all(sum(r["stage"] == "history_rich" for r in rows if r["request"] == key) == 2 for key in ids)
    assert timing._current.get() is None


@pytest.mark.asyncio
async def test_middleware_opt_in_and_exception_privacy(monkeypatch):
    from app.main import _startup_diagnostics
    rows = capture(monkeypatch)
    def request(enabled, path="/app/concierge/panel"):
        headers = [(b"authorization", b"Bearer PRIVATE SECRET")]
        if enabled:
            headers.append((b"x-blam8-startup-diagnostics", b"1"))
        return Request({"type":"http", "method":"GET", "path":path, "headers":headers,
                        "query_string":b"private=PRIVATE_SECRET", "scheme":"https", "server":("test",443)})
    async def next_response(req):
        with timing.span("auth"):
            return Response("PRIVATE CONTENT")
    normal = await _startup_diagnostics(request(False), next_response)
    assert normal.body == b"PRIVATE CONTENT" and rows == []
    enabled = await _startup_diagnostics(request(True), next_response)
    assert enabled.body == normal.body
    assert len(enabled.headers["X-BLAM8-Startup-Trace"]) == 16
    assert float(enabled.headers["X-BLAM8-Startup-Log-Sum-Ms"]) >= 0
    assert float(enabled.headers["X-BLAM8-Startup-Log-Max-Ms"]) >= 0
    assert rows[-1]["stage"] == "complete" and rows[-1]["status"] == 200
    assert "PRIVATE" not in json.dumps(rows)

    rows.clear()
    async def failed(req):
        raise ValueError("PRIVATE SECRET")
    with pytest.raises(ValueError, match="PRIVATE SECRET"):
        await _startup_diagnostics(request(True), failed)
    assert rows[-1]["status"] == 0 and timing._current.get() is None
    assert "PRIVATE" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_loop_lag_probe_is_bounded(monkeypatch):
    rows = capture(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(timing.time, "monotonic", lambda: clock[0])
    async def delayed_sleep(seconds):
        clock[0] += seconds + .3
    monkeypatch.setattr(timing.asyncio, "sleep", delayed_sleep)
    state, token = timing.begin("home")
    await timing.loop_probe(state)
    timing.finish(state, token, 200)
    observed = [r for r in rows if r["stage"] == "loop_lag"]
    assert len(observed) == 4 and all(r["duration_ms"] == 300 for r in observed)


def test_log_emission_cost_is_observed_without_text_or_extra_records(monkeypatch):
    clock = [0.0]
    rows = []
    monkeypatch.setattr(timing.time,"monotonic",lambda:clock[0])
    def delayed_logger(template,payload):
        rows.append(json.loads(payload))
        clock[0] += .25
    monkeypatch.setattr(timing.log,"info",delayed_logger)
    state,token=timing.begin("panel")
    with timing.span("discretion_lookup"):
        pass
    timing.finish(state,token,200)
    assert len(rows)==4
    assert state["log_sum_ms"] == 1000 and state["log_max_ms"] == 250
    assert all(set(row).issubset({"request","route","stage","event","elapsed_ms","duration_ms","status","cpu_ms"}) for row in rows)


def test_code_sampler_never_reads_frame_values_or_private_paths():
    import os
    from types import SimpleNamespace
    class Frame:
        f_back = None
        f_lineno = 42
        f_code = SimpleNamespace(co_filename=timing._app_prefix+"auth.py",co_name="require_subscription")
        @property
        def f_locals(self):raise AssertionError("must never read locals")
        @property
        def f_globals(self):raise AssertionError("must never read globals")
    assert timing._code_locations(Frame()) == [{"file":"auth.py","function":"require_subscription","line":42}]
    frame=Frame()
    frame.f_code=SimpleNamespace(co_filename=os.path.join(os.sep,"private","SECRET.py"),co_name="SECRET")
    assert timing._code_locations(frame) == []


@pytest.mark.asyncio
async def test_single_sampler_cap_stop_and_reserved_log_budget(monkeypatch):
    rows = capture(monkeypatch)
    first, token = timing.begin("home")
    timing.start_sampler(first)
    second, other = timing.begin("panel")
    timing.start_sampler(second)
    assert second["sampler_status"] == "busy" and "sampler_thread" not in second
    await timing.stop_sampler(second)
    timing.finish(second,other,200)
    for _ in range(100):
        timing.mark("identity_cache_hit",True)
    # Eight source-only samples remain emit-able even after ordinary records cap.
    first["samples"] = [{"kind":"loop_stack","frames":[{"file":"auth.py","function":"require_subscription","line":42}],"observed_ms":1.,"duration_ms":300.} for _ in range(8)]
    await timing.stop_sampler(first)
    assert first["sampler_status"] == "stopped" and not first["sampler_thread"].is_alive()
    timing.finish(first,token,200)
    own=[r for r in rows if r["request"]==first["id"]]
    assert len(own)==64 and sum(r["stage"]=="loop_stack" for r in own)==8
    assert own[-1]["stage"] == "complete"


@pytest.mark.asyncio
async def test_subscription_span_covers_real_worker_and_preserves_gate(monkeypatch):
    from types import SimpleNamespace
    from app import auth, db
    rows=capture(monkeypatch)
    ctx=SimpleNamespace(tenant_id="t",user_id="u",client=object())
    monkeypatch.setattr(auth.settings,"require_subscription_gate",True)
    calls=[]
    def tenant(tenant_id,client):
        calls.append((tenant_id,client))
        return {"subscription_status":"active"}
    monkeypatch.setattr(db,"get_tenant",tenant)
    state,token=timing.begin("home")
    try:
        assert await auth.require_subscription(ctx) is ctx
    finally:timing.finish(state,token,200)
    stages=[(r["stage"],r["event"]) for r in rows]
    assert calls==[("t",ctx.client)]
    assert ("subscription_worker","start") in stages and ("subscription_worker","end") in stages
    assert any(r["stage"]=="subscription_gate" and r["value"] is True for r in rows)
    assert any(r["stage"]=="pool_total" and r["value"]>0 for r in rows)
    monkeypatch.setattr(auth.settings,"require_subscription_gate",False)
    assert await auth.require_subscription(ctx) is ctx and calls==[("t",ctx.client)]


@pytest.mark.asyncio
async def test_sampler_copies_only_code_locations_and_releases_owner(monkeypatch):
    import threading
    from types import SimpleNamespace
    rows=capture(monkeypatch)
    copied=threading.Event()
    class Frame:
        f_code=SimpleNamespace(co_filename=timing._app_prefix+"db.py",co_name="get_tenant")
        f_lineno=123
        f_back=None
        @property
        def f_locals(self):raise AssertionError("no local data")
    original=timing._code_locations
    def copy(frame):
        value=original(frame);copied.set();return value
    monkeypatch.setattr(timing,"_code_locations",copy)
    ident=threading.get_ident()
    monkeypatch.setattr(timing.sys,"_current_frames",lambda:{ident:Frame()})
    state,token=timing.begin("home")
    timing.start_sampler(state)
    state["last_tick"]=timing.time.monotonic()-1
    try:
        assert await asyncio.to_thread(copied.wait,1)
    finally:
        await timing.stop_sampler(state)
        timing.finish(state,token,200)
    samples=[r for r in rows if r["stage"]=="loop_stack"]
    assert samples and samples[0]["frames"]==[{"file":"db.py","function":"get_tenant","line":123}]
    assert state["sampler_status"]=="stopped" and timing._sampler_guard.acquire(blocking=False)
    timing._sampler_guard.release()


@pytest.mark.asyncio
async def test_sampler_join_has_a_hard_timeout():
    import threading
    stop,release=threading.Event(),threading.Event()
    class UnresponsiveJoin:
        ident=1
        def join(self,timeout):release.wait(2)
        def is_alive(self):return True
    state={"sampler_stop":stop,"sampler_thread":UnresponsiveJoin(),"samples":[]}
    start=timing.time.monotonic()
    try:
        await timing.stop_sampler(state)
        assert timing.time.monotonic()-start < 1
        assert stop.is_set() and state["sampler_status"]=="stopping"
    finally:release.set()


def test_database_round_trips_are_counted_by_table_name_only():
    import httpx
    from app import db, startup_timing as t
    state, token = t.begin("probe")
    try:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
                              event_hooks={"request": [db._stamp_request], "response": [db._count_response]})
        client.get("https://x.supabase.co/rest/v1/concierge_tasks?select=*&id=eq.secret-value")
        client.get("https://x.supabase.co/rest/v1/concierge_tasks")
        client.get("https://x.supabase.co/auth/v1/user")
        assert {k: v[0] for k, v in state["db"].items()} == {"concierge_tasks": 2, "auth_user": 1}
        assert "secret" not in repr(state["db"])
    finally:
        t.finish(state, token, 200)
