"""The Diary filter (docs/plan-diary-multiview.md stage 3, Kris 2026-09-23: "we need to
add a Multiview UI to the Diary … the view mode becomes more of a filter").

A diary that holds more than one kind — stays AND appointments — gets chips: All,
Stays, each appointment. The pick changes the VIEW only: each day's bookings, its bar,
its chart lanes and the cell's words. A single-kind diary shows no chips at all.

Run: python -m pytest tests/test_diary_filter.py -q
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import scheduling, slotgrid
from app.auth import TenantCtx, require_onboarded
from app.intake import STAY_FIELD
from app.main import app
from app.scheduling import DIARY_TZ
from app.security import require_csrf

DAY = date(2026, 9, 10)                                   # a Thursday
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=DIARY_TZ)
MEET = {"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet",
        "starts": ["11:00", "13:00", "15:00"], "max_per_day": 2}
MIXED = {"windows": "slots", "open_time": "07:00", "close_time": "18:45", "slot_minutes": 60,
         "capacity_per_window": 6, "working_days": "mon,tue,wed,thu,fri,sat,sun", "horizon_days": 28,
         "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
         "service_types": [MEET], "resource_types": [{"name": "Meet & greet", "count": 1}],
         "intake_configured_at": "2026-08-01T00:00:00+00:00"}


def _stay(bid, arr, dep):
    a, d = datetime.fromisoformat(arr), datetime.fromisoformat(dep)
    return {"id": bid, "booking_ref": bid.upper(), "booking_date": a.date().isoformat(), "window": "day",
            "start_block": slotgrid.block_at(a.time()), "status": "confirmed", "summary": f"Stay {bid}",
            "length_blocks": slotgrid.datetime_to_abs(d) - slotgrid.datetime_to_abs(a), "details": {}}


def _appt(bid, hhmm, status="confirmed"):
    return {"id": bid, "booking_ref": bid.upper(), "booking_date": DAY.isoformat(), "window": hhmm,
            "start_block": slotgrid.block_at(hhmm), "length_blocks": 2, "status": status,
            "summary": f"Meet & greet {bid}", "resource_type": "Meet & greet",
            "details": {"Service": "Meet & greet"}}


BOOKINGS = [_stay("s1", "2026-09-10T08:00", "2026-09-12T17:00"),     # arrives today
            _stay("s2", "2026-09-08T08:00", "2026-09-10T17:00"),     # goes home today
            _appt("a1", "11:00"), _appt("a2", "13:00")]


# ── the kinds and one kind's day ─────────────────────────────────────────────
def test_only_a_diary_of_more_than_one_kind_gets_chips():
    kinds = scheduling.diary_kinds(MIXED)
    assert [(k["key"], k["label"]) for k in kinds] == [("all", "All"), ("stays", "Stays"),
                                                       ("meet-greet", "Meet & greet")]
    assert scheduling.diary_kinds({**MIXED, "service_types": []}) == []          # stays only
    assert scheduling.diary_kinds({**MIXED, "intake_fields": []}) == []          # a services diary


def _today(cfg=MIXED):
    days = scheduling.diary_days(cfg, [dict(b) for b in BOOKINGS], NOW)
    return next(d for d in days if d["date"] == DAY.isoformat())


def test_the_stays_view_says_who_comes_and_goes():
    d = scheduling.filter_day(_today(), scheduling.diary_kinds(MIXED)[1], scheduling.diary_kinds(MIXED))
    assert (d["stay_in"], d["stay_out"]) == (1, 1)
    assert {b["id"] for b in d["bookings"]} == {"s1", "s2"}
    assert [t["key"] for t in d["types"]] == ["spaces"]
    if d.get("chart"):
        assert {b["booking_id"] for b in d["chart"]["bars"]} <= {"s1", "s2"}


def test_an_appointment_view_counts_its_day_against_its_max():
    kinds = scheduling.diary_kinds(MIXED)
    d = scheduling.filter_day(_today(), kinds[2], kinds)
    assert {b["id"] for b in d["bookings"]} == {"a1", "a2"}
    t = d["kind_type"]
    assert (t["used"], t["max"], t["full"]) == (2, 2, True)                     # 2 of 2: full
    if d.get("chart"):
        assert {b["booking_id"] for b in d["chart"]["bars"]} == {"a1", "a2"}
        assert min(b["lane"] for b in d["chart"]["bars"]) == 0                  # its own lanes, from 0


def test_the_appointment_bar_follows_the_service_rules():
    """With only 11:00, 13:00, 15:00 as starts and one at a time, 11:00 and 13:00
    booked leaves 15:00; a day not ticked for the service is OFF, not free."""
    t = next(x for x in _today({**MIXED, "service_types": [{**MEET, "max_per_day": None}]})["types"]
             if x["key"] == "Meet & greet")
    assert t["full"] is False and t["off"] is False
    off = {**MEET, "days": [0], "max_per_day": None}                            # Mondays only; DAY is a Thursday
    t2 = next(x for x in _today({**MIXED, "service_types": [off]})["types"] if x["key"] == "Meet & greet")
    assert t2["off"] is True and t2["full"] is False


def test_the_filter_never_changes_the_day_itself():
    day = _today()
    before = (len(day["bookings"]), len(day["types"]))
    kinds = scheduling.diary_kinds(MIXED)
    scheduling.filter_day(day, kinds[2], kinds)
    assert (len(day["bookings"]), len(day["types"])) == before
    assert scheduling.filter_day(day, kinds[0], kinds) is day                    # All = as it was


# ── the page ─────────────────────────────────────────────────────────────────
class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, cfg):
    for mod in (dashboard.db, scheduling.db):
        monkeypatch.setattr(mod, "get_scheduling_config", lambda *a, **k: dict(cfg))
        monkeypatch.setattr(mod, "list_bookings", lambda *a, **k: [dict(b) for b in BOOKINGS])
    for name, fn in (("active_modules", lambda *a, **k: {"scheduling"}),
                     ("module_active", lambda tid, m, **k: m == "scheduling"),
                     ("list_email_log", lambda *a, **k: []), ("is_enterprise", lambda *a, **k: False),
                     ("get_tenant", lambda *a, **k: {"provider": "google"}),
                     ("calendar_connected", lambda *a, **k: False), ("list_branches", lambda *a, **k: []),
                     ("list_pallet_types", lambda *a, **k: []), ("list_requirement_groups", lambda *a, **k: [])):
        monkeypatch.setattr(dashboard.db, name, fn, raising=False)
    monkeypatch.setattr(scheduling, "datetime", _Frozen)


def test_the_page_shows_the_chips_and_one_kinds_words(client, monkeypatch):
    _env(monkeypatch, MIXED)
    html = client.get("/app/diary?show=meet-greet").text
    assert "data-diary-filter" in html
    for key in ("all", "stays", "meet-greet"):
        assert f'data-show="{key}"' in html
    assert 'data-show="meet-greet" aria-current="true"' in html
    assert "full · 2 of 2" in html                                               # the Thursday cell
    assert 'value="appointment" checked' in html                                 # + Add picks the kind
    stays = client.get("/app/diary?show=stays").text
    assert "1 in · 1 out" in stays and 'value="stay" checked' in stays


def test_a_single_kind_diary_has_no_chips(client, monkeypatch):
    _env(monkeypatch, {**MIXED, "service_types": [], "resource_types": []})
    assert "data-diary-filter" not in client.get("/app/diary").text
