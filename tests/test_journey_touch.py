"""journey.touch writes at most once a minute per tenant, including after a restart."""
from datetime import datetime, timedelta, timezone

from app import journey


def _calls(monkeypatch, last_seen):
    calls = []
    monkeypatch.setattr(journey.db, "latest_visit",
                        lambda tenant: calls.append("latest") or {"id": "v1", "last_seen_at": last_seen.isoformat()})
    monkeypatch.setattr(journey.db, "bump_visit", lambda *a: calls.append("bump"))
    monkeypatch.setattr(journey.db, "insert_visit", lambda *a: calls.append("insert") or {"id": "v2"})
    return calls


def test_an_adopted_visit_is_remembered_so_later_requests_cost_nothing(monkeypatch):
    journey._live.clear()
    calls = _calls(monkeypatch, datetime.now(timezone.utc) - timedelta(minutes=2))
    for _ in range(5):
        journey.touch("tenant-adopt")
    journey.wait_for_writes()
    # One lookup and one write on the first request after a restart, then nothing.
    assert calls == ["latest", "bump"]


def test_a_fresh_visit_is_inserted_once(monkeypatch):
    journey._live.clear()
    calls = _calls(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=3))
    for _ in range(5):
        journey.touch("tenant-new")
    assert calls == ["latest", "insert"]


def test_the_heartbeat_write_never_holds_up_the_request(monkeypatch):
    import threading
    import time
    journey._live.clear()
    release = threading.Event()
    monkeypatch.setattr(journey.db, "latest_visit",
                        lambda tenant: {"id": "v1", "last_seen_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()})
    monkeypatch.setattr(journey.db, "bump_visit", lambda *a: release.wait(5))
    started = time.monotonic()
    journey.touch("tenant-slow")
    assert time.monotonic() - started < 1      # the slow write is on the background worker
    release.set()
    journey.wait_for_writes()
