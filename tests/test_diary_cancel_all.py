"""POST /app/diary/cancel-all — every live booking cancelled in one call, each
calendar event removed first (Kris 2026-09-04: the e2e rig reset was one POST
per row). Route-level via TestClient, db monkeypatched, no network."""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


LIVE = [
    {"id": "b-1", "booking_ref": "BK-1001", "status": "confirmed", "booking_date": "2026-09-07",
     "calendar_event_id": "ev1", "contact": "kris@x.co", "email_log_id": "log-1"},
    {"id": "b-2", "booking_ref": "BK-1004", "status": "offered", "booking_date": "2026-09-08",
     "calendar_event_id": None, "contact": None, "email_log_id": None},
]


def _env(monkeypatch, rows, modules=("scheduling",)):
    removed, statuses, events, published = [], [], [], []

    async def remove(tenant_id, booking_id):
        removed.append(booking_id)

    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "list_live_bookings", lambda *a, **k: list(rows))
    monkeypatch.setattr(dashboard.db, "set_booking_status",
                        lambda bid, status, tid, client=None: statuses.append((bid, status)))
    monkeypatch.setattr(dashboard.db, "log_agent_event",
                        lambda tid, kind, summary, **k: events.append((kind, summary)))
    monkeypatch.setattr(dashboard.calendar_sync, "remove_pushed_event", remove)
    monkeypatch.setattr(dashboard.events, "publish", lambda tid, payload: published.append(payload))
    return removed, statuses, events, published


def test_every_live_row_is_cancelled_after_its_event_goes(client, monkeypatch):
    removed, statuses, events, published = _env(monkeypatch, LIVE)
    r = client.post("/app/diary/cancel-all")
    assert r.status_code == 200
    assert r.json() == {"cancelled": 2, "refs": ["BK-1001", "BK-1004"]}
    # the calendar event is removed BEFORE the status flips (the remover reads
    # the row's event id, and a cancelled row is never re-synced)
    assert removed == ["b-1", "b-2"]
    assert statuses == [("b-1", "cancelled"), ("b-2", "cancelled")]
    assert events == [("booking_manual",
                       "Cancelled every live booking by hand in the diary (2): BK-1001, BK-1004")]
    # only the row tied to a contact repaints a thread
    assert [p["booking_id"] for p in published] == ["b-1"]


def test_an_empty_diary_is_a_quiet_no_op(client, monkeypatch):
    removed, statuses, events, _ = _env(monkeypatch, [])
    r = client.post("/app/diary/cancel-all")
    assert r.status_code == 200
    assert r.json() == {"cancelled": 0, "refs": []}
    assert removed == [] and statuses == [] and events == []


def test_the_diary_control_is_sent_back_with_a_flash(client, monkeypatch):
    """The page's own "Cancel every live booking" form posts return_to and
    lands back on the diary with the count in the flash (run #7 fix 12b)."""
    removed, statuses, events, _ = _env(monkeypatch, LIVE)
    r = client.post("/app/diary/cancel-all", data={"return_to": "/app/diary"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/diary?msg=cancelled_all&n=2"
    assert statuses == [("b-1", "cancelled"), ("b-2", "cancelled")]


def test_a_foreign_return_to_falls_back_to_json(client, monkeypatch):
    _env(monkeypatch, LIVE)
    r = client.post("/app/diary/cancel-all", data={"return_to": "https://evil.example/"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["cancelled"] == 2


def test_without_scheduling_the_door_is_shut(client, monkeypatch):
    _env(monkeypatch, LIVE, modules=("crm",))
    assert client.post("/app/diary/cancel-all").status_code == 404
