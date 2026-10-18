"""The Diary row's gathered details fold behind a "details" link.

Kris, 2026-09-20: "I want the bulk of the info on the Diary booking hidden.
All of 'Dogs: Biscuit · Food details: … · Medical or behavioural notes: …'
shouldn't be shown until Details is clicked. Details needs adding to the row
of links (view conversation · job page · details)."

The paragraph ships rendered and hidden; the link on the row opens it (and
closes it again). A row with nothing gathered has no link and no paragraph;
the calendar's own bookkeeping key never counts as a detail.

Run: python -m pytest tests/test_diary_details_fold.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf
from tests.test_diary_day_chart import DAY, SLOTS, _FrozenDatetime, _booking


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, bookings):
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


DETAILS = {"Dogs": "Biscuit", "Food details": "Harringtons dry, one scoop morning and evening",
           "Medical or behavioural notes": "No medication"}


def _row(html, bid):
    start = html.index(f'id="bk-{bid}"')
    return html[start:html.index("</li>", start)]


def test_the_details_fold_behind_a_link_on_the_row(client, monkeypatch):
    _env(monkeypatch, [dict(_booking("b-9am", "09:00", status="confirmed"),
                            details=DETAILS, source="email", email_log_id="e1")])
    row = _row(client.get("/app/diary").text, "b-9am")
    # The row of links: view conversation · job page · details
    assert "view conversation</a>" in row and "job page</a>" in row
    assert 'data-details-toggle aria-expanded="false" aria-controls="details-b-9am">details</button>' in row
    # …and the paragraph is there, hidden, until the link opens it.
    assert 'id="details-b-9am" data-booking-details hidden>' in row
    assert "Dogs: Biscuit · Food details: Harringtons dry, one scoop morning and evening" in row
    assert row.index("job page</a>") < row.index("details</button>") < row.index("Dogs: Biscuit")


def test_a_row_with_nothing_gathered_has_no_link_and_no_paragraph(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    row = _row(client.get("/app/diary").text, "b-9am")
    assert "data-details-toggle" not in row and "data-booking-details" not in row


def test_the_calendars_own_bookkeeping_is_not_a_detail(client, monkeypatch):
    """calendar_event_missing_at drives the amber chip, never the paragraph —
    a row whose only 'detail' is that key gets no link."""
    _env(monkeypatch, [dict(_booking("b-9am", "09:00", status="confirmed"),
                            details={"calendar_event_missing_at": "2026-09-05T10:00:00+00:00"})])
    row = _row(client.get("/app/diary").text, "b-9am")
    assert "data-details-toggle" not in row and "calendar_event_missing_at" not in row
    assert "Calendar event missing" in row


def test_the_page_carries_the_toggle():
    src = open("src/app/templates/app/diary.html", encoding="utf-8").read()
    assert "e.target.closest('[data-details-toggle]')" in src
    assert "t.textContent = open ? 'hide details' : 'details';" in src
