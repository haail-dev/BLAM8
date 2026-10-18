"""The memory read-out (src/app/memstat.py): numbers only, never tenant data, never fatal."""
import asyncio
import builtins
import io
import json
import logging
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import concierge_routes, db, gptlive as live, memstat, navcache
from app import main as app_main
from app.admin_auth import AdminCtx, require_admin
from app.config import background_jobs_on, settings

TENANT = "11111111-2222-3333-4444-555555555555"
KEY = "eyJhbGciOiJIUzI1NiJ9.e30.abc"


@pytest.fixture(autouse=True)
def _own_stores(monkeypatch):
    """The stores are process-global; give every test empty ones so counts are exact."""
    monkeypatch.setattr(live, "_sessions", {})
    monkeypatch.setattr(live, "_cancelled", {})
    monkeypatch.setattr(concierge_routes, "_SPOKEN", {})
    monkeypatch.setattr(concierge_routes, "_VOICE_SAMPLES", {})
    monkeypatch.setattr(db, "_client", None)
    monkeypatch.delattr(db, "_retry_counts", raising=False)   # present only once the retry layer exists
    navcache._store.clear()


class UnreadableStore(dict):
    def items(self):
        raise ValueError("this store cannot be read")


def a_session():
    return live.Session("owned", "provider", TENANT, "u1", "voice", "permissions", object())


def test_snapshot_is_numbers_only_and_names_no_tenant_or_key(monkeypatch):
    navcache._store[f"t:{TENANT}:data:conversations"] = (time.monotonic() + 60, [{"id": "secret-row"}])
    concierge_routes._SPOKEN[f"{TENANT}:u1"] = [[time.time(), "private spoken text"]]
    s = a_session()
    s.fragments.append({"type": "session.input_transcript.delta", "delta": "private words"})
    live._sessions["owned"] = s

    data = memstat.snapshot(tasks=3, pool=(1, 40))

    assert all(value is None or type(value) in (int, float) for value in data.values())
    assert all(re.fullmatch(r"[a-z0-9_]+", key) for key in data)
    text = json.dumps(data)
    for private in (TENANT, "secret-row", "private", "conversations"):
        assert private not in text
    assert data["cache_keys"] == 1
    assert data["cache_rows"] == 1
    assert data["cache_data_keys"] == 1
    assert data["live_sessions"] == 1
    assert data["live_fragments"] == 1
    assert data["spoken_people"] == 1
    assert data["spoken_entries"] == 1


def test_the_store_gauges_count_what_each_store_holds():
    s = a_session()
    s.transcript_chars, s.transcript_fragments, s.output_chars = 120, 4, 30
    s.events.extend([{}, {}]); s.history.append({}); s.seen.update({"a", "b", "c"})
    s.delegations["d1"] = {}; s.commands["c1"] = {}
    live._sessions["owned"] = s
    live._cancelled[("t", "u", "v")] = 1.0
    concierge_routes._VOICE_SAMPLES["x:marin"] = b"12345"
    concierge_routes._VOICE_SAMPLES["x:cedar"] = b"123"

    data = memstat.snapshot()

    assert (data["live_transcript_chars"], data["live_transcript_fragments"], data["live_output_chars"]) == (120, 4, 30)
    assert (data["live_events"], data["live_history"], data["live_seen"]) == (2, 1, 3)
    assert (data["live_delegations"], data["live_commands"], data["live_cancelled"]) == (1, 1, 1)
    assert (data["voice_samples"], data["voice_sample_bytes"]) == (2, 8)
    assert data["threads"] >= 1
    assert data["gc_gen0"] is not None and data["alloc_blocks"] > 0


def test_expired_cache_entries_are_counted_separately():
    navcache._store["t:x:one"] = (time.monotonic() - 5, [1, 2, 3])
    navcache._store["t:x:two"] = (time.monotonic() + 50, "text")

    data = memstat.snapshot()

    assert (data["cache_keys"], data["cache_expired"], data["cache_rows"]) == (2, 1, 3)


def test_rss_is_read_from_proc_or_absent(monkeypatch):
    rss = memstat.resident_mb()
    assert rss is None or rss > 0
    real_open = builtins.open
    with monkeypatch.context() as patched:
        def fake_open(path, *args, **kwargs):
            if path == "/proc/self/status":
                return io.StringIO("Name:\tpython\nVmPeak:\t  200000 kB\nVmRSS:\t  123456 kB\nThreads:\t4\n")
            return real_open(path, *args, **kwargs)
        patched.setattr(builtins, "open", fake_open)
        assert memstat.resident_mb() == 120.6
    with monkeypatch.context() as patched:
        def no_status(path, *args, **kwargs):
            if path == "/proc/self/status":
                return io.StringIO("Name:\tpython\n")
            return real_open(path, *args, **kwargs)
        patched.setattr(builtins, "open", no_status)
        assert memstat.resident_mb() is None


def test_one_unreadable_store_does_not_break_the_read_out(monkeypatch):
    monkeypatch.setattr(navcache, "_store", UnreadableStore())

    data = memstat.snapshot()

    assert data["cache_keys"] is None and data["cache_rows"] is None
    assert data["live_sessions"] == 0
    assert data["threads"] >= 1


def test_the_loop_numbers_are_passed_in_and_absent_without_them():
    assert memstat.loop_state() == {}   # no running loop on this thread
    given = memstat.snapshot(tasks=7, pool=(2, 40))
    assert (given["tasks"], given["pool_borrowed"], given["pool_total"]) == (7, 2, 40)
    absent = memstat.snapshot()
    assert absent["tasks"] is None and absent["pool_borrowed"] is None and absent["pool_total"] is None


@pytest.mark.asyncio
async def test_the_loop_thread_reads_the_task_count_and_the_worker_limiter():
    state = memstat.loop_state()
    assert state["tasks"] >= 1
    borrowed, total = state["pool"]
    assert borrowed >= 0 and total > 0


def test_the_database_pool_is_read_from_the_shared_client(monkeypatch):
    from supabase import create_client
    options = db._http1_options()
    client = create_client("https://x.supabase.co", KEY, options=options)
    monkeypatch.setattr(db, "_client", client)

    assert memstat.snapshot()["db_connections"] == 0
    # the path the read-out takes is the very client the queries go through
    assert client.postgrest.session is options.httpx_client

    monkeypatch.setattr(db, "_client", None)
    assert memstat.snapshot()["db_connections"] is None


def test_retry_counters_are_copied_only_when_the_database_layer_has_them(monkeypatch):
    monkeypatch.delattr(db, "_retry_counts", raising=False)
    assert not [key for key in memstat.snapshot() if key.startswith("db_retry_")]

    monkeypatch.setattr(db, "_retry_counts", {"ReadTimeout:recovered": 2, "ReadTimeout:failed": 1,
                                              "https://x/y?email=a@b.c:failed": 9, "other": 4}, raising=False)
    data = memstat.snapshot()

    assert data["db_retry_ReadTimeout:recovered"] == 2
    assert data["db_retry_ReadTimeout:failed"] == 1
    assert sorted(key for key in data if key.startswith("db_retry_")) == [
        "db_retry_ReadTimeout:failed", "db_retry_ReadTimeout:recovered"]


def test_collect_is_only_on_request():
    assert "collected" not in memstat.snapshot()
    data = memstat.snapshot(collect=True)
    assert isinstance(data["collected"], int)
    assert "rss_mb_after" in data


def test_the_log_line_is_bounded_and_parses():
    text = memstat.line()
    assert len(text) < 2000
    assert text.startswith("memstat ")
    assert json.loads(text[len("memstat "):])["threads"] >= 1
    assert json.loads(memstat.line(tasks=5, pool=(1, 40))[len("memstat "):])["tasks"] == 5


@pytest.mark.asyncio
async def test_the_loop_enforces_the_floor_and_survives_a_failing_snapshot(monkeypatch, caplog):
    readings, sleeps = [], []

    class Stop(Exception):
        pass

    def snapshot(tasks=None, pool=None, collect=False):
        readings.append(tasks)
        if len(readings) == 1:
            raise RuntimeError("the first reading fails")
        return {"rss_mb": 1.5}

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    monkeypatch.setattr(memstat, "snapshot", snapshot)
    monkeypatch.setattr(memstat.asyncio, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger="app.memstat"), pytest.raises(Stop):
        await memstat.run_forever(0.01)

    assert sleeps == [memstat.MIN_SECONDS] * 3
    assert len(readings) == 3 and all(tasks >= 1 for tasks in readings)
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("memstat failed") == 1
    assert [m for m in messages if m.startswith("memstat {")] == ['memstat {"rss_mb":1.5}'] * 2


# -- wiring: the setting, the startup hook and the operator route ----------------------------------

@pytest.fixture
def admin_client():
    app_main.app.dependency_overrides[require_admin] = lambda: AdminCtx(
        user_id="admin", email="admin@example.com", role="owner")
    yield TestClient(app_main.app)
    app_main.app.dependency_overrides.pop(require_admin, None)


def test_admin_memstat_requires_admin_and_returns_numbers(admin_client):
    response = admin_client.get("/admin/memstat")

    assert response.status_code == 200
    data = response.json()
    assert data and all(value is None or isinstance(value, (int, float)) for value in data.values())
    assert isinstance(data["tasks"], int) and data["tasks"] >= 1    # read on the loop thread
    assert "collected" not in data
    assert "collected" in admin_client.get("/admin/memstat?collect=1").json()

    del app_main.app.dependency_overrides[require_admin]
    refused = admin_client.get("/admin/memstat", follow_redirects=False)
    assert refused.status_code in (302, 303, 307, 401, 403)


def test_memstat_is_off_by_default():
    assert settings.memstat_seconds == 0


@pytest.mark.asyncio
async def test_the_startup_hook_starts_the_loop_only_when_the_setting_is_on(monkeypatch):
    started = []

    async def run_forever(seconds):
        started.append(seconds)

    monkeypatch.setattr(memstat, "run_forever", run_forever)
    monkeypatch.setattr(app_main.app.state, "memstat", None, raising=False)
    assert not background_jobs_on()    # the read-out is of this process, so it must not wait for the jobs flag

    monkeypatch.setattr(settings, "memstat_seconds", 0)
    await app_main._start_memstat()
    assert app_main.app.state.memstat is None and started == []

    monkeypatch.setattr(settings, "memstat_seconds", 60)
    await app_main._start_memstat()
    task = app_main.app.state.memstat
    assert isinstance(task, asyncio.Task)
    await task
    assert started == [60]
