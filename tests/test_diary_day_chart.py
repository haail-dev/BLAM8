"""Diary day view — the occupancy chart draws EVERY booking on the day.

Regression pinned (live e2e 2026-09-01): the chart iterates lanes and the
template pulled `bars|selectattr('lane')|first` — ONE bar per lane. A lane is
one space across the whole day, so two bookings that don't overlap in TIME share
lane 0; on a capacity-1 diary that meant exactly one bar could ever render. A day
holding an offered 09:00 and an offered 11:00 drew a single bar, captioned itself
"2 bookings · click a bar for details", and the 11:00 hold's details card stayed
unreachable — the only way to reveal it was clicking the bar that was missing.

Run: python -m pytest tests/ -q
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.scheduling import DIARY_TZ
from app.security import require_csrf

DAY = date(2026, 9, 10)                                  # a Thursday
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=DIARY_TZ)        # the Tuesday before
SLOTS = {"windows": "slots", "open_time": "09:00", "close_time": "17:00",
         "slot_minutes": 60, "capacity_per_window": 1,
         "working_days": "mon,tue,wed,thu,fri",
         "intake_configured_at": "2026-08-01T00:00:00+00:00"}


def _booking(bid: str, window: str, status: str = "offered") -> dict:
    return {"id": bid, "booking_ref": bid.upper(), "booking_date": DAY.isoformat(),
            "window": window, "status": status, "summary": f"Catch-up call {window}",
            "spaces": None, "contact": "kris@example.com"}


# ── the chart data: sequential bookings share a lane, and BOTH are bars ───────
def test_day_chart_keeps_every_booking_when_they_share_a_lane():
    todays = [_booking("b-9am", "09:00"), _booking("b-11am", "11:00")]
    chart = scheduling._day_chart(scheduling._merged(SLOTS), todays, DAY, capacity=1)
    assert [b["booking_id"] for b in chart["bars"]] == ["b-9am", "b-11am"]
    # They don't overlap, so the packer reuses the lane — which is exactly the
    # case the template used to collapse.
    assert {b["lane"] for b in chart["bars"]} == {0}
    assert chart["lanes"] == 1


def test_day_chart_splits_overlapping_bookings_into_lanes():
    """The lane packing itself is unchanged: a genuine overlap still gets its
    own lane (2 spaces in use), so the fix cannot mask a double-booking."""
    todays = [_booking("b-1", "09:00"), _booking("b-2", "09:00")]
    chart = scheduling._day_chart(scheduling._merged(SLOTS), todays, DAY, capacity=2)
    assert {b["lane"] for b in chart["bars"]} == {0, 1}
    assert chart["used"] == 2


# ── the rendered page: one clickable bar per booking ─────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


@pytest.fixture
def diary_env(monkeypatch):
    bookings = [_booking("b-9am", "09:00"), _booking("b-11am", "11:00")]
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


class _FrozenDatetime(datetime):
    """`scheduling.diary` reads now() to pick the weeks it renders."""
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def test_diary_page_draws_a_bar_for_every_booking_on_the_day(client, diary_env):
    html = client.get("/app/diary").text
    for b in diary_env:
        assert f'data-bar="{b["id"]}"' in html, f'{b["id"]} has no clickable bar'
        # Its details card is rendered too — the bar is what reveals it.
        assert f'data-detail="{b["id"]}"' in html
    assert html.count('class="stay-bar') >= 2
