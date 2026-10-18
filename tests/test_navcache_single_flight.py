"""Concurrent misses on one key share a single fill; failures and vetoes fall back to own reads."""
import threading
import time

import pytest

from app import navcache


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    navcache._inflight.clear()
    yield
    navcache._store.clear()
    navcache._inflight.clear()


def run_together(n, fn):
    out, threads = [None] * n, []
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        try:
            out[i] = fn()
        except Exception as e:
            out[i] = e
    for i in range(n):
        threads.append(threading.Thread(target=go, args=(i,)))
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    return out


def test_eight_simultaneous_misses_fill_once():
    calls = []

    def fill():
        calls.append(1)
        time.sleep(0.2)
        return {"v": 1}
    started = time.monotonic()
    out = run_together(8, lambda: navcache.get_or_fill("k", 60, fill))
    assert len(calls) == 1
    assert all(o == {"v": 1} for o in out)
    assert time.monotonic() - started < 0.6
    assert not navcache._inflight


def test_a_failed_leader_does_not_poison_followers():
    calls = []

    def fill():
        calls.append(threading.get_ident())
        time.sleep(0.1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return "ok"
    out = run_together(4, lambda: navcache.get_or_fill("k", 60, fill))
    assert sum(isinstance(o, RuntimeError) for o in out) == 1      # only the leader saw the failure
    assert out.count("ok") == 3
    assert not navcache._inflight


def test_a_vetoed_value_is_not_shared_and_not_cached():
    calls = []

    def fill():
        calls.append(1)
        time.sleep(0.1)
        return None
    out = run_together(3, lambda: navcache.get_or_fill("k", 60, fill, cache_if=bool))
    assert out == [None, None, None]
    assert len(calls) == 3                                           # each asked for itself: never cached a miss
    assert navcache.peek("k") == (False, None)


def test_hits_after_the_fill_never_call_fill_again():
    navcache.get_or_fill("k", 60, lambda: 7)
    assert navcache.get_or_fill("k", 60, lambda: (_ for _ in ()).throw(AssertionError("refilled"))) == 7


def test_disabled_cache_still_fills_every_time(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", True)
    calls = []
    for _ in range(3):
        navcache.get_or_fill("k", 60, lambda: calls.append(1) or 1)
    assert len(calls) == 3


def test_a_follower_gives_up_waiting_on_a_stuck_leader(monkeypatch):
    monkeypatch.setattr(navcache, "_FLIGHT_WAIT", 0.2)
    release = threading.Event()
    leader = threading.Thread(target=lambda: navcache.get_or_fill("k", 60, lambda: release.wait(5) or "late"))
    leader.start()
    time.sleep(0.05)
    started = time.monotonic()
    assert navcache.get_or_fill("k", 60, lambda: "mine") == "mine"
    assert time.monotonic() - started < 1
    release.set()
    leader.join(5)
