"""The front desk rule (Kris 2026-09-13): "a Front desk rule option, so a
user can decide if they want overlapping — for instance they have an
employee/partner that can deal with one thing while you deal with the other."

Off (the default): a stays diary and its appointments never see each other.
On: a drop-off or collection takes N minutes of the desk during which no
appointment is offered, and a stay's arrival or departure is refused inside
an appointment — by the agent and the portal alike (both run the same
executor); a human adding by hand is not bound.

Run: python -m pytest tests/test_front_desk.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import diary_config, scheduling, slotgrid
from app.auth import TenantCtx, require_onboarded
from app.intake import STAY_FIELD
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 6, 0, tzinfo=scheduling.DIARY_TZ)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cfg(on=True, minutes=15, **over):
    cfg = carried_config({"working_days": "mon,tue,wed,thu,fri,sat", "windows": "slots", "slot_minutes": 60,
           "capacity_per_window": 30, "horizon_days": 7, "blackout_dates": "", "min_notice_hours": 0,
           "open_time": "07:00", "close_time": "18:45",
           "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
           "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
           "resource_types": [{"name": "Meet & greet", "count": 2}],
           "nouns": {"booking": "booking", "participant": "dog"},
           "front_desk": {"enabled": on, "minutes": minutes}})
    cfg.update(over)
    return cfg


def _stay(day_iso, start_block=40, length=96 * 2, status="confirmed", bid="s1"):
    # arrives 10:00 (block 40), leaves two days later at 10:00
    return {"id": bid, "booking_date": day_iso, "start_block": start_block, "length_blocks": length,
            "status": status, "summary": "Bella", "window": "day", "details": {}}


def _appt(day_iso, start_block=40, bid="a1"):
    return {"id": bid, "booking_date": day_iso, "start_block": start_block, "length_blocks": 2,
            "status": "confirmed", "summary": "Meet & greet — Sam", "window": slotgrid.hhmm(start_block),
            "resource_type": "Meet & greet", "details": {"Service": "Meet & greet"}}


def test_the_setting_cleans_and_defaults_off():
    assert diary_config.clean_front_desk(None) == {"enabled": False, "minutes": 15, "hands": 1}
    assert diary_config.clean_front_desk({"enabled": "1", "minutes": "20"}) == {"enabled": True, "minutes": 20, "hands": 1}
    assert diary_config.clean_front_desk({"enabled": True, "minutes": 900})["minutes"] == 120
    assert diary_config.front_desk_of({}) == {"enabled": False, "minutes": 15, "hands": 1}
    assert scheduling._front_desk_on(_cfg(on=False)) is False
    assert scheduling._front_desk_on(_cfg()) is True
    assert scheduling._front_desk_on(_cfg(service_types=[])) is False          # no appointments: nothing to keep apart
    assert scheduling._front_desk_on(_cfg(intake_fields=[])) is False          # not a stays diary


def test_a_stays_ends_are_desk_time_for_the_appointments():
    cfg = _cfg()
    stay = _stay("2026-09-14")
    spans = scheduling.front_desk_spans(cfg, [stay], 2)
    arr = slotgrid.abs_block(datetime(2026, 9, 14).date(), 40)
    assert spans == [(arr, 1, 2), (arr + 96 * 2, 1, 2)]                       # 15 min = 1 block, the whole pool
    assert scheduling.front_desk_spans(_cfg(on=False), [stay], 2) == []
    assert scheduling.front_desk_spans(cfg, [_appt("2026-09-14")], 2) == []    # appointments are not desk time
    assert scheduling.front_desk_spans(cfg, [_stay("2026-09-14", status="cancelled")], 2) == []
    assert scheduling.front_desk_spans(_cfg(minutes=30), [stay], 1)[0][1] == 2


def test_no_appointment_is_offered_while_a_drop_off_is_handled():
    stay = _stay("2026-09-14")                                                  # arrives 10:00
    day = datetime(2026, 9, 14).date()
    def starts(cfg):
        return [s["window"] for s in scheduling.service_availability(
            cfg, [stay], 2, day, day, NOW, capacity=2, resource="Meet & greet")]
    off, on = starts(_cfg(on=False)), starts(_cfg())
    assert "10:00" in off and "09:00" in off
    assert "10:00" not in on and "09:00" in on and "11:00" in on              # only the desk's quarter hour
    # A 09:45 start runs into the 10:00 drop-off too (the grid steps by 15 here).
    fine = _cfg(slot_minutes=15)
    fine_on = [s["window"] for s in scheduling.service_availability(
        fine, [stay], 2, day, day, NOW, capacity=2, resource="Meet & greet")]
    assert "09:45" not in fine_on and "10:15" in fine_on and "09:30" in fine_on
    # first_fit_start (the hold's placement) agrees.
    assert scheduling.first_fit_start(_cfg(), [stay], 2, day, "10:00", None, capacity=2, resource="Meet & greet") is None
    assert scheduling.first_fit_start(_cfg(on=False), [stay], 2, day, "10:00", None, capacity=2, resource="Meet & greet") == 40
    assert scheduling.front_desk_hit(_cfg(), [stay], day, 40, 2) == "10:00"
    assert scheduling.front_desk_hit(_cfg(), [stay], day, 44, 2) is None


def test_a_drop_off_is_refused_inside_an_appointment():
    cfg = _cfg()
    appt = _appt("2026-09-14", 40)                                              # 10:00–10:30
    arr = datetime(2026, 9, 14, 10, 15, tzinfo=scheduling.DIARY_TZ)
    dep = datetime(2026, 9, 16, 9, 0, tzinfo=scheduling.DIARY_TZ)
    clash = scheduling.front_desk_clash(cfg, [appt], arr, dep)
    assert clash == {"when": "arrival", "at": "10:15", "service": "Meet & greet", "from": "10:00", "until": "10:30"}
    assert scheduling.front_desk_clash(_cfg(on=False), [appt], arr, dep) is None
    ok = datetime(2026, 9, 14, 10, 30, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.front_desk_clash(cfg, [appt], ok, dep) is None            # from the appointment's end
    late = datetime(2026, 9, 16, 10, 20, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.front_desk_clash(cfg, [_appt("2026-09-16", 40)], arr.replace(hour=8), late)["when"] == "departure"
    # The agent's stay check, hold and OFFER all apply it (and so the portal,
    # which runs the same executor). The third is offer_stay, added 2026-09-22:
    # an offer is an exclusive reservation, so a span it could never honour must
    # not be offered either.
    sched = _read("src/app/scheduling.py")
    assert sched.count("rule = _stay_rules(arr, dep, cfg_row) or _front_desk_rule(arr, dep, cfg_row)") == 3
    assert '"error": "front_desk",' in sched and "the front desk handles one thing" in sched
    assert "hit = front_desk_hit(cfg_row, _booked_on(day), day, probe, length)" in sched
    assert "manual_stay_add" in sched and "front_desk" not in sched.split("def manual_stay_add")[1].split("def gate_date")[0]


def test_the_month_cell_counts_desk_time_against_the_appointments():
    stay = _stay("2026-09-14")
    on = next(d for d in scheduling.diary_days(_cfg(), [stay], NOW) if d["date"] == "2026-09-14")
    off = next(d for d in scheduling.diary_days(_cfg(on=False), [stay], NOW) if d["date"] == "2026-09-14")
    assert on["types"][1]["pct"] > off["types"][1]["pct"] == 0                 # one start of twelve gone


def test_the_agent_is_told_the_rule():
    cfg = _cfg()
    line = scheduling.stay_diary_line(cfg, cfg["service_types"])
    assert "FRONT DESK RULE" in line and "within 15 minutes after a stay's arrival or departure" in line
    assert "error front_desk" in line
    assert "FRONT DESK" not in scheduling.stay_diary_line(_cfg(on=False), cfg["service_types"])


@pytest.fixture
def saved(monkeypatch):
    calls = []
    cur = {"intake_fields": [dict(STAY_FIELD)], "front_desk": {"enabled": True, "minutes": 20}}
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(dict(cur)))
    monkeypatch.setattr(dashboard.db, "upsert_scheduling_config", lambda tid, cfg, **k: calls.append(cfg))
    monkeypatch.setattr(dashboard.db, "apply_booking_settings",
                        lambda tid, cfg, revision, **k: calls.append(cfg))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), calls
    app.dependency_overrides.clear()


def test_the_setting_saves_from_the_appointments_face_and_only_from_there(saved):
    client, calls = saved
    base = carried_config({"windows": "slots", "working_days": ["mon"], "service_name": ["Meet & greet"],
            "service_minutes": ["30"], "service_price": [""], "service_timing": ["time"],
            "service_resource": [""], "service_buffer": [""], "service_recall_months": [""], "service_at_once": ["2"]})
    client.post("/app/settings/scheduling", data={**base, "front_desk_present": "1", "front_desk_enabled": "1",
                                                   "front_desk_minutes": "10"}, follow_redirects=False)
    depth = next(c for c in calls if "resource_types" in c)
    assert depth["front_desk"] == {"enabled": True, "minutes": 10, "hands": 1}
    calls.clear()
    client.post("/app/settings/scheduling", data={**base, "front_desk_present": "1"}, follow_redirects=False)
    assert next(c for c in calls if "resource_types" in c)["front_desk"] == {"enabled": False, "minutes": 15, "hands": 1}
    calls.clear()
    client.post("/app/settings/scheduling", data=base, follow_redirects=False)      # a form without the face
    assert "front_desk" not in next(c for c in calls if "resource_types" in c)
    face = _read("src/app/templates/app/_scheduling_settings.html")
    assert 'name="front_desk_present" value="1"' in face and 'name="front_desk_enabled"' in face
    assert 'name="front_desk_minutes"' in face and "Front desk rule" in face
    assert "' · front desk rule' if front_desk.enabled" in face
    assert face.index("{% if diary_stay_mode %}\n      {# The front desk rule") < face.index('<template id="svc-template">')
    dash = _read("src/app/dashboard.py")
    assert '"front_desk": diary_config.front_desk_of(cfg),' in dash
    assert "alter table scheduling_config add column if not exists front_desk jsonb" in _read("db/schema.sql")


def test_the_diary_settings_page_renders_the_rule(monkeypatch):
    from tests.test_settings_fold import _env
    _env(monkeypatch, modules=("scheduling",))
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config({
        "intake_fields": [dict(STAY_FIELD)], "windows": "slots",
        "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
        "resource_types": [{"name": "Meet & greet", "count": 2}],
        "front_desk": {"enabled": True, "minutes": 20}}))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        html = TestClient(app).get("/app/diary/settings").text
    finally:
        app.dependency_overrides.clear()
    assert "1 appointment · front desk rule" in html
    row = html.split("data-front-desk")[1].split("</div>\n      </div>")[0]
    assert 'name="front_desk_enabled" value="1" checked' in row and 'value="20"' in row
    assert "one person handles drop-offs, collections and appointments" in row
