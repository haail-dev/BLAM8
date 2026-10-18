"""A booking or hold line in the thread is a link to its slot in the Diary.

Kris, 2026-09-20: "When showing a booking or hold, I want the sentence to
be linked to the Diary booking slot. Quick navigation."

The thread ghost's "Booked slot · 7th Oct 26 · 13:00" lines are links now:
/app/diary?day=<date>&booking=<id>. The office Diary learned what the fleet
board already knew — open a day from the query — plus a booking to light:
its row scrolls into view with a ring on it. Today's own booking rows link
with ?date=, so the Diary takes that spelling too.

Run: python -m pytest tests/test_thread_booking_links.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf
from tests.test_diary_day_chart import DAY, SLOTS, _FrozenDatetime, _booking
from tests.test_thread_decision import ROW, _env as thread_env


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── The rows (src/app/dashboard.py — pure) ─────────────────────────────────────
def test_a_live_row_links_to_its_day_and_its_booking():
    rows = dashboard._held_rows([{"id": "b1", "status": "confirmed",
                                  "booking_date": "2026-10-07", "window": "13:00"}])
    assert rows[0]["id"] == "b1"
    assert rows[0]["href"] == "/app/diary?day=2026-10-07&booking=b1"
    assert rows[0]["label"] == "Booked" and rows[0]["when"] == "13:00"


def test_a_row_with_no_day_links_to_the_booking_itself():
    rows = dashboard._held_rows([{"id": "b1", "status": "hold", "window": "10:00"}])
    assert rows[0]["href"] == "/app/diary/b1"


def test_a_row_with_neither_has_no_link_and_still_reads():
    rows = dashboard._held_rows([{"status": "offered", "window": "10:00"}])
    assert rows[0]["href"] == "" and rows[0]["id"] is None and rows[0]["label"] == "Offered"


def test_a_released_row_still_drops_out():
    assert dashboard._held_rows([{"id": "b1", "status": "cancelled",
                                  "booking_date": "2026-10-07"}]) == []


# ── The thread ─────────────────────────────────────────────────────────────
def test_the_thread_line_is_a_link_to_the_slot(client, monkeypatch):
    thread_env(monkeypatch, [ROW],
               bookings=[{"id": "b1", "status": "confirmed", "booking_ref": "BK-1003",
                          "booking_date": "2026-10-07", "window": "13:00"}])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert '<a href="/app/diary?day=2026-10-07&amp;booking=b1" class="hover:underline">' in html
    line = html[html.index('href="/app/diary?day=2026-10-07'):]
    assert "Booked slot · 7th Oct 26 · 13:00</a>" in line[:400]


# ── The Diary ──────────────────────────────────────────────────────────────
@pytest.fixture
def diary_env(monkeypatch):
    bookings = [_booking("b-9am", "09:00"), _booking("b-11am", "11:00", status="confirmed")]
    for mod in (dashboard.db, scheduling.db):
        monkeypatch.setattr(mod, "get_scheduling_config", lambda *a, **k: dict(SLOTS))
        monkeypatch.setattr(mod, "list_bookings", lambda *a, **k: [dict(b) for b in bookings])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m == "scheduling")
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "google"})
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_pallet_types", lambda *a, **k: [])
    monkeypatch.setattr(scheduling, "datetime", _FrozenDatetime)
    return bookings


def test_the_diary_opens_the_day_and_lights_the_booking(client, diary_env):
    html = client.get(f"/app/diary?day={DAY.isoformat()}&booking=b-11am").text
    assert 'id="bk-b-11am"' in html and 'id="bk-b-9am"' in html     # every row is a target
    assert 'var openDay = "2026-09-10", focus = "b-11am";' in html


def test_todays_own_spelling_of_the_day_is_taken_too(client, diary_env):
    html = client.get(f"/app/diary?date={DAY.isoformat()}").text
    assert 'var openDay = "2026-09-10", focus = "";' in html


def test_a_day_nobody_asked_for_opens_nothing(client, diary_env):
    html = client.get("/app/diary").text
    assert 'var openDay = "", focus = "";' in html
