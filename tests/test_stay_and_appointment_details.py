"""Two fixes from the Ellen test (2026-09-23, Kris: "Go, fix both").

1. A stay's details never gate an appointment. On a stays diary that also takes
   appointments, "Stays" is a scope beside the appointment names: "Collection date and
   time" ticked for Stays binds the trial night, not the meet & greet. Each tool is told
   only its own detail keys.
2. The same request again in one email updates the reservation made while details were
   gathered — the retry naming Pepper was refused "already reserved offered slots".

Run: python -m pytest tests/test_stay_and_appointment_details.py -q
"""
from app import intake, scheduling
from app.intake import STAY_FIELD
from tests.test_stay_executor import CFG, NOW

MEET = {"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}
FIELDS = [dict(STAY_FIELD),
          {"label": "Collection date and time", "required": True, "services": ["Stays"]},
          {"label": "Number of dogs", "required": True},
          {"label": "Owner's phone", "required": True, "services": ["Meet & greet"]}]
APPT = {"service": "Meet & greet", "date": "2026-06-30", "window": "13:00", "summary": "Meet & greet"}
STAY = {"arrival": "2026-06-30T13:30", "departure": "2026-07-01T09:30", "summary": "Trial night"}


def _exec(monkeypatch, inserted):
    cfg = {**CFG, "intake_fields": FIELDS, "service_types": [MEET],
           "resource_types": [{"name": "Meet & greet", "count": 1}]}
    for name, fn in (("get_scheduling_config", lambda *a, **k: cfg), ("list_active_bookings", lambda *a, **k: []),
                     ("bookings_for_contact", lambda *a, **k: []), ("offered_bookings_for_contact", lambda *a, **k: []),
                     ("count_service_bookings_by_day", lambda *a, **k: {}),
                     ("release_offered_for_contact", lambda *a, **k: 0)):
        monkeypatch.setattr(scheduling.db, name, fn)

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": f"b{len(inserted) + 1}", "booking_ref": f"BK-92{len(inserted) + 1:02d}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    execute, _ = scheduling.make_executor("t1", contact="ellen@x.co", stay=True, now=NOW,
                                          intake_fields=FIELDS, service_types=[MEET])
    return execute


# ── 1. each kind gathers its own details ─────────────────────────────────────
def test_stays_is_a_scope_like_any_service():
    stay_only, appt_only, both = FIELDS[1], FIELDS[3], FIELDS[2]
    assert intake.applies(stay_only, intake.STAYS_SCOPE) and not intake.applies(stay_only, "Meet & greet")
    assert intake.applies(appt_only, "Meet & greet") and not intake.applies(appt_only, intake.STAYS_SCOPE)
    assert intake.applies(both, intake.STAYS_SCOPE) and intake.applies(both, "Meet & greet")


def test_an_appointment_is_never_asked_for_a_stays_detail(monkeypatch):
    out = _exec(monkeypatch, [])("hold_appointment", dict(APPT))
    assert out["error"] == "missing_info"
    assert set(out["missing"]) == {"Number of dogs", "Owner's phone"}           # no collection time


def test_a_stay_is_never_asked_for_an_appointments_detail(monkeypatch):
    out = _exec(monkeypatch, [])("hold_booking", dict(STAY))
    assert set(out["missing"]) == {"Collection date and time", "Number of dogs"}   # no phone


def test_each_tool_is_told_only_its_own_keys():
    tools = [{"name": "hold_appointment", "input_schema": {"properties": {"details": {"description": "d"}}}},
             {"name": "hold_booking", "input_schema": {"properties": {"details": {"description": "d"}}}}]
    appt, stay = intake.with_detail_keys(tools, FIELDS, ["Meet & greet"])
    a = appt["input_schema"]["properties"]["details"]["description"]
    s = stay["input_schema"]["properties"]["details"]["description"]
    assert "'Owner's phone'" in a and "'Collection date and time'" not in a
    assert "'Collection date and time'" in s and "'Owner's phone'" not in s
    plain = intake.with_detail_keys(tools, FIELDS)                             # no appointments: unchanged
    assert "'Owner's phone'" in plain[1]["input_schema"]["properties"]["details"]["description"]


def test_the_requirements_page_offers_stays_beside_the_appointments():
    from pathlib import Path
    dash = (Path(__file__).resolve().parents[1] / "src/app/dashboard.py").read_text(encoding="utf-8")
    assert "service_names = [intake.STAYS_SCOPE] + services.names(cfg.get(\"service_types\"))" in dash


# ── 2. the same request again updates its reservation ────────────────────────
def test_the_same_appointment_again_reuses_the_reservation(monkeypatch):
    inserted = []
    execute = _exec(monkeypatch, inserted)
    first = execute("hold_appointment", dict(APPT))
    assert first.get("reserved") is True and len(inserted) == 1
    again = execute("hold_appointment", {**APPT, "details": {"Number of dogs": "1"}})
    assert again["error"] == "missing_info" and again["missing"] == ["Owner's phone"]
    assert again.get("reserved") is True and again["booking_ref"] == first["booking_ref"]
    assert "SAME request" in again["message"] and len(inserted) == 1            # nothing new placed


def test_the_same_stay_again_reuses_the_reservation(monkeypatch):
    inserted = []
    execute = _exec(monkeypatch, inserted)
    first = execute("hold_booking", dict(STAY))
    assert first.get("reserved") is True
    again = execute("hold_booking", {**STAY, "details": {"Number of dogs": "1"}})
    assert again.get("reserved") is True and again["booking_ref"] == first["booking_ref"]
    assert len(inserted) == 1


def test_a_different_appointment_is_still_one_too_many(monkeypatch):
    inserted = []
    execute = _exec(monkeypatch, inserted)
    assert execute("hold_appointment", dict(APPT)).get("reserved") is True
    other = execute("hold_appointment", {**APPT, "window": "15:00"})
    assert other["error"] == "already_offered"
