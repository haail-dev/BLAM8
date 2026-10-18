"""Opt-in, content-free timing for the two startup GET paths.

The caller sends X-BLAM8-Startup-Diagnostics: 1. No identity, query string,
cookie, exception text or database result is logged. This does no network I/O.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid

log = logging.getLogger(__name__)
_current = ContextVar("startup_timing", default=None)
_sampler_guard = threading.Lock()
_app_prefix = os.path.normcase(os.path.dirname(__file__)) + os.sep
_libraries = {"socket.py", "ssl.py", "selectors.py", "base_events.py", "events.py", "threading.py",
              "http11.py", "connection.py", "connection_pool.py", "request_builder.py", "_client.py", "_asyncio.py"}
_names = frozenset(("ingress", "complete", "loop_lag", "auth", "identity_http", "refresh_http",
    "tenant_lookup", "journey", "user_client", "view_as", "onboarding", "access", "entitlement",
    "home", "navigation_context", "panel", "panel_context", "history", "history_rich",
    "history_native", "history_observed", "task_hydration", "task_scope", "identity_cache_hit",
    "discretion_pool", "discretion_lookup", "discretion_body", "discretion_on",
    "security_headers", "response_available", "probe_cancel", "subscription", "subscription_worker",
    "subscription_gate", "pool_borrowed", "pool_total", "loop_stack", "pool_stack", "db_table", "render"))


def _emit(state, stage, event, **values):
    if stage not in _names:
        return
    with state["lock"]:
        # Reserve the final record even when many task scopes exhaust the cap.
        # Reserve eight stack records plus completion within the same hard cap.
        limit = 64 if stage in {"complete", "db_table"} else 63 if stage in {"loop_stack", "pool_stack"} else 55
        if state["events"] >= limit:
            return
        state["events"] += 1
    payload = {"request": state["id"], "route": state["route"], "stage": stage, "event": event,
               "elapsed_ms": round((time.monotonic() - state["start"]) * 1000, 1)}
    payload.update({k: v for k, v in values.items() if k in {"duration_ms", "status", "value", "observed_ms", "count", "cpu_ms"}
                    and type(v) in (bool, int, float)})
    if stage == "db_table" and re.fullmatch(r"[a-z0-9_]{1,48}", str(values.get("table", ""))):
        payload["table"] = values["table"]
    if stage in {"loop_stack", "pool_stack"}:
        payload["frames"] = values.get("frames", [])[:8]
    emitted = time.monotonic()
    try:
        log.info("startup_timing %s", json.dumps(payload, separators=(",", ":")))
    except Exception:
        pass  # Diagnostics must not change a request's outcome.
    finally:
        duration = max(0, (time.monotonic() - emitted) * 1000)
        with state["lock"]:
            state["log_sum_ms"] += duration
            state["log_max_ms"] = max(state["log_max_ms"], duration)


def begin(route):
    state = {"id": uuid.uuid4().hex[:16], "route": route, "start": time.monotonic(),
             "events": 0, "lock": threading.Lock(), "log_sum_ms": 0., "log_max_ms": 0., "db": {},
             "cpu0": time.process_time()}
    token = _current.set(state)
    _emit(state, "ingress", "start")
    return state, token


def db_call(request, started):
    """Count one database round trip against the opted-in request, by table name
    only (never a query string, filter or row)."""
    state = _current.get()
    if state is None:
        return
    parts = [p for p in request.url.path.split("/") if p]
    table = re.sub(r"[^a-z0-9_]", "_", (parts[-1] if parts else "root").lower())[:48] or "root"
    if "auth" in parts[:1]:
        table = "auth_" + table
    elapsed = (time.monotonic() - started) * 1000
    with state["lock"]:
        count, total = state["db"].get(table, (0, 0.))
        state["db"][table] = (count + 1, total + elapsed)


def finish(state, token, status):
    with state["lock"]:
        tables = sorted(state["db"].items(), key=lambda kv: -kv[1][1])[:12]
    for table, (count, total) in tables:
        _emit(state, "db_table", "value", table=table, count=count, duration_ms=round(total, 1))
    # cpu_ms: process CPU used while this request was in flight (all threads). Meaningful when
    # the request ran alone: it is what the 0.5 vCPU has to supply, as opposed to I/O waiting.
    _emit(state, "complete", "end", status=status,
          cpu_ms=round((time.process_time() - state["cpu0"]) * 1000, 1))
    _current.reset(token)


def mark(name, value):
    state = _current.get()
    if state is not None:
        _emit(state, name, "value", value=value)


def pool_state(log_values=False):
    state = _current.get()
    if state is None:
        return
    try:
        from anyio.to_thread import current_default_thread_limiter
        limiter = current_default_thread_limiter()
    except Exception:
        return
    state["pool"] = (limiter.borrowed_tokens, limiter.total_tokens)
    if log_values:
        mark("pool_borrowed", limiter.borrowed_tokens)
        mark("pool_total", limiter.total_tokens)


def _code_locations(frame):
    """Copy code locations only. Never access locals, globals, args or values."""
    app, library = [], []
    for _ in range(48):
        if frame is None:
            break
        code = frame.f_code
        path = os.path.normcase(code.co_filename)
        own = path.startswith(_app_prefix)
        filename = path[len(_app_prefix):].replace(os.sep, "/") if own else os.path.basename(path)
        if own or filename in _libraries:
            name = code.co_name
            location = {"file": filename, "function": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "unknown",
                        "line": min(1000000, max(0, frame.f_lineno))}
            target = app if own else library
            if len(target) < 4:
                target.append(location)
        frame = frame.f_back
    return app + library


def start_sampler(state):
    """At most one owned, daemon sampler in the process; no I/O in its thread."""
    state["sampler_status"] = "busy"
    if not _sampler_guard.acquire(blocking=False):
        return
    stop = threading.Event()
    state.update(sampler_stop=stop, samples=[], last_tick=time.monotonic())
    loop_thread = threading.get_ident()
    def sample():
        deadline = time.monotonic() + 120
        seen = set()
        try:
            while not stop.wait(.1) and time.monotonic() < deadline:
                if len(state["samples"]) >= 8:
                    continue
                at = time.monotonic()
                gap = max(0, (at - state["last_tick"]) * 1000)
                borrowed, total = state.get("pool", (0, 0))
                locations = []
                if gap >= 250:
                    kind = "loop_stack"
                    frames = sys._current_frames()
                    locations = _code_locations(frames.get(loop_thread))
                elif total and borrowed >= total:
                    kind = "pool_stack"
                    frames = sys._current_frames()
                    workers = [t.ident for t in threading.enumerate() if t.name == "AnyIO worker thread"][:4]
                    for ident in workers:
                        locations.extend(_code_locations(frames.get(ident))[:2])
                else:
                    continue
                del frames  # Retain only code-location dictionaries, never frame references.
                signature = (kind, tuple((v["file"], v["function"], v["line"]) for v in locations))
                if locations and signature not in seen:
                    seen.add(signature)
                    state["samples"].append({"kind": kind, "frames": locations[:8],
                        "observed_ms": round((at - state["start"]) * 1000, 1), "duration_ms": round(gap, 1)})
        except Exception:
            state["sampler_status"] = "failed"
        finally:
            _sampler_guard.release()
    thread = threading.Thread(target=sample, name="startup-code-sampler", daemon=True)
    state["sampler_thread"] = thread
    try:
        thread.start()
        state["sampler_status"] = "active"
    except Exception:
        _sampler_guard.release()
        state["sampler_status"] = "failed"


async def stop_sampler(state):
    stop = state.get("sampler_stop")
    thread = state.get("sampler_thread")
    if stop is None or thread is None or not thread.ident:
        return
    stop.set()
    try:
        await asyncio.wait_for(asyncio.to_thread(thread.join, .1), .25)
    except Exception:
        pass
    state["sampler_status"] = "stopping" if thread.is_alive() else "stopped"
    for value in state.get("samples", [])[:8]:
        _emit(state, value["kind"], "sample", **{k: v for k, v in value.items() if k != "kind"})


@contextmanager
def span(name):
    state = _current.get()
    if state is None:
        yield
        return
    started = time.monotonic()
    cpu0 = time.thread_time()
    _emit(state, name, "start")
    try:
        yield
    finally:
        # cpu_ms: CPU this thread spent inside the span (waiting on I/O costs none).
        _emit(state, name, "end", duration_ms=round((time.monotonic() - started) * 1000, 1),
              cpu_ms=round((time.thread_time() - cpu0) * 1000, 1))


def instrument(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(*args, **kwargs):
                with span(name):
                    return await fn(*args, **kwargs)
        else:
            @wraps(fn)
            def wrapped(*args, **kwargs):
                with span(name):
                    return fn(*args, **kwargs)
        return wrapped
    return decorate


async def loop_probe(state):
    # Bounded per opt-in request; reports stalls, not normal scheduling jitter.
    deadline = time.monotonic() + 120
    reports = 0
    while time.monotonic() < deadline:
        before = time.monotonic()
        await asyncio.sleep(.1)
        state["last_tick"] = time.monotonic()
        pool_state()
        lag = max(0, (time.monotonic() - before - .1) * 1000)
        if lag >= 200 and reports < 4:
            _emit(state, "loop_lag", "observed", duration_ms=round(lag, 1))
            reports += 1
