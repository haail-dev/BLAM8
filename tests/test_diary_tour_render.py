"""The Diary page rendered whole on a stays diary that also books
appointments (Kris 2026-09-13): the header gone, the cells reading
"1 in · 29 free · 2 meet & greets", the walk's anchors on the page.

Run: python -m pytest tests/test_diary_tour_render.py -q
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling
from app.auth import TenantCtx, require_onboarded
from app.intake import STAY_FIELD
from app.main import app
from app.scheduling import DIARY_TZ
from app.security import require_csrf

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=DIARY_TZ)        # a Monday
CFG = {"windows": "slots", "open_time": "07:00", "close_time": "18:45", "slot_minutes": 60,
       "capacity_per_window": 30, "working_days": "mon,tue,wed,thu,fri,sat", "horizon_days": 14,
       "min_notice_hours": 10, "blackout_dates": "",
       "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
       "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
       "resource_types": [{"name": "Meet & greet", "count": 2}],
       "nouns": {"booking": "booking", "participant": "dog"},
       "intake_configured_at": "2026-08-01T00:00:00+00:00"}


def _rows():
    day = date(2026, 9, 16).isoformat()                    # Wednesday, past the notice
    stay = {"id": "s1", "booking_ref": "BK-1", "booking_date": day, "start_block": 28, "length_blocks": 96 * 2,
            "status": "confirmed", "summary": "Bella", "window": "day", "details": {}, "spaces": None}
    appt = {"id": "a1", "booking_ref": "BK-2", "booking_date": day, "start_block": 40, "length_blocks": 2,
            "status": "confirmed", "summary": "Meet & greet — Sam", "window": "10:00", "spaces": None,
            "resource_type": "Meet & greet", "details": {"Service": "Meet & greet"}}
    appt2 = {**appt, "id": "a2", "booking_ref": "BK-3", "start_block": 44, "summary": "Meet & greet — Jo"}
    return [stay, appt, appt2]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
def client(monkeypatch):
    rows = _rows()
    for mod in (dashboard.db, scheduling.db):
        monkeypatch.setattr(mod, "get_scheduling_config", lambda *a, **k: dict(CFG))
        monkeypatch.setattr(mod, "list_bookings", lambda *a, **k: [dict(b) for b in rows])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m == "scheduling")
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "google"})
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_pallet_types", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [])
    monkeypatch.setattr(dashboard, "_tour_for", lambda ctx, t, page, force=False, after=None: ("diary", "dogs") if force else (None, ""))
    monkeypatch.setattr(scheduling, "datetime", _FrozenDatetime)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_page_reads_smart_and_carries_the_walk(client):
    html = client.get("/app/diary?tour=1").text
    assert "Up to 30" not in html and "Cancel every live booking" not in html and "+ Add booking" not in html
    assert 'data-tour-diary="stays"' in html
    assert 'data-tour="today"' in html and 'data-tour="colour"' in html
    assert '<div id="tour-root" data-tour-page="diary"' in html
    assert "1 in · 29 free · 2 meet &amp; greets" in html                     # Wednesday's cell
    assert html.count("1 in · 29 free") >= 2                                  # the stay runs into Thursday
    assert ">free<" in html                                                   # a plain day still just says free
    assert "needs 10h notice" in html                                         # today, inside the notice
    assert "Add by hand" in html and "data-addform-close" in html
    # The colour anchor is the booked Wednesday, not a plain free day — and its
    # two bars (Kris 2026-09-13): the spaces at 3%, the meet & greets a sliver.
    cell = html.split('data-tour="colour"')[1].split("</button>")[0]
    assert "1 in · 29 free" in cell
    assert 'data-bar-type="spaces" title="dogs: 3%"' in cell and 'style="height:3%"' in cell
    assert 'data-bar-type="Meet &amp; greet" title="meet &amp; greets: 4%"' in cell and 'style="height:4%"' in cell
    assert "bg-emerald-500/40" in cell and "bg-blue-500/40" in cell
    assert 'data-tour-types="2"' in html
    # The day panel: the two visits in blue on their own lanes, the stay in emerald, a legend.
    panel = html.split('id="panel-2026-09-16"')[1].split("</section>")[0]
    assert panel.count("stay-bar") == 3 and "bg-blue-500 hover:bg-blue-600" in panel and "bg-emerald-500 hover:bg-emerald-600" in panel
    assert "data-chart-legend" in panel and "1 of 30 spaces in use" in panel and "1 of 2 meet &amp; greets" in panel
    assert 'rounded-full mr-1.5 align-middle bg-blue-500' in panel and 'rounded-full mr-1.5 align-middle bg-emerald-500' in panel
    plain = client.get("/app/diary").text
    assert 'id="tour-root"' not in plain                                      # walked, or not yet owed: no walk
