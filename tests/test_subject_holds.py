"""One hold per VISIT: a second dependant on the same email is its own hold
(run #6, Foxholes Garage, 2026-09-04).

Live: "Tuesday 15 September morning for both please" — the Golf's MOT and
service held (BK-1001), the Fiesta's MOT was refused `already_held`, and the
reply told the customer "the diary only allows one hold per email, so I wasn't
able to lock in Sam's MOT… could you send a separate email". Two cars on one
morning is the garage's normal case and each car is its own booking. A hold
that names a DIFFERENT dependant in `subjects` now goes through (cap
SUBJECT_HOLDS_MAX); the same dependant twice, or a hold naming no subject
after one that did, is still refused; an offered set still blocks.

Run: python -m pytest tests/test_subject_holds.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import scheduling
from app.scheduling import DIARY_TZ

CFG = carried_config({"windows": "slots", "open_time": "08:00", "close_time": "17:30", "slot_minutes": 45,
       "capacity_per_window": 3, "min_notice_hours": 0,
       "working_days": "mon,tue,wed,thu,fri,sat"})
SERVICES = [{"name": "MOT", "minutes": 45, "price": 45.0},
            {"name": "Interim Service", "minutes": 120, "price": 120.0, "timing": "day"}]
GROUPS = [{"id": "g-veh", "label": "Vehicle", "label_plural": "vehicles", "cardinality": "many",
           "fields": [{"key": "registration", "label": "Registration", "type": "text",
                       "required": True, "durability": "once", "gate": False, "hint": ""}]}]
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=DIARY_TZ)


def _exec(monkeypatch, inserted):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-kris"})
    monkeypatch.setattr(scheduling.db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "upsert_requirement_instance",
                        lambda t, g, c, name, data, meta: {"id": "inst-" + name.lower()})
    monkeypatch.setattr(scheduling.db, "link_booking_subjects", lambda *a, **k: None)

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": f"svc-{len(inserted) + 1}", "booking_ref": f"BK-700{len(inserted) + 1}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "window": window, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="kris@x.co", service_types=SERVICES,
                                    groups=GROUPS, now=NOW)


def _golf(**over):
    args = {"date": "2026-09-15", "window": "day", "service": "Interim Service",
            "summary": "Interim Service — Golf",
            "subjects": [{"name": "Golf", "group": "Vehicle", "fields": {"Registration": "YK19 HDF"}}]}
    args.update(over)
    return args


def _fiesta(**over):
    args = {"date": "2026-09-15", "window": "08:00", "service": "MOT", "summary": "MOT — Fiesta",
            "subjects": [{"name": "Fiesta", "group": "Vehicle", "fields": {"Registration": "MK17 LPQ"}}]}
    args.update(over)
    return args


def test_a_second_vehicle_is_its_own_hold(monkeypatch):
    inserted = []
    execute, state = _exec(monkeypatch, inserted)
    first = execute("hold_booking", _golf())
    second = execute("hold_booking", _fiesta())
    assert first.get("held") is True and second.get("held") is True, (first, second)
    assert [b["booking_ref"] for b in state["bookings"]] == ["BK-7001", "BK-7002"]
    assert state["bookings"][0]["_subject_names"] == {"golf"}
    assert state["bookings"][1]["_subject_names"] == {"fiesta"}


def test_the_same_vehicle_twice_is_still_refused(monkeypatch):
    execute, state = _exec(monkeypatch, [])
    assert execute("hold_booking", _golf()).get("held") is True
    again = execute("hold_booking", _golf(window="day", service="MOT"))
    assert again["error"] == "already_held" and again["booking_id"] == state["bookings"][0]["id"]
    assert "DIFFERENT dependant" in again["message"]


def test_a_hold_naming_no_subject_after_one_that_did_is_refused(monkeypatch):
    execute, _ = _exec(monkeypatch, [])
    assert execute("hold_booking", _golf()).get("held") is True
    # a tenant WITH groups refuses a subject-less hold at the gate anyway; the
    # one-hold rule must not let it through first
    out = execute("hold_booking", _fiesta(subjects=[]))
    assert out["error"] in ("already_held", "missing_info")


def test_the_cap_holds(monkeypatch):
    execute, state = _exec(monkeypatch, [])
    for name in ("Golf", "Fiesta", "Polo"):
        out = execute("hold_booking", _golf(subjects=[{"name": name, "group": "Vehicle",
                                                       "fields": {"Registration": "X"}}]))
        assert out.get("held") is True, out
    fourth = execute("hold_booking", _golf(subjects=[{"name": "Astra", "group": "Vehicle",
                                                      "fields": {"Registration": "Y"}}]))
    assert fourth["error"] == "hold_limit"
    assert len(state["bookings"]) == scheduling.SUBJECT_HOLDS_MAX


def test_tool_description_teaches_one_hold_per_visit():
    hold = next(t for t in scheduling.SERVICE_TOOLS if t["name"] == "hold_booking")
    assert "One hold per visit" in hold["description"]
    assert "At most one hold per email" not in hold["description"]


# ── run #7 (swim school, 2026-09-05): a customer's "none" is an answer ────────
NONE_GROUPS = [{"id": "g-child", "label": "Child", "label_plural": "children", "cardinality": "many",
                "fields": [{"key": "medical", "label": "Medical conditions, allergies or medication",
                            "type": "text", "required": True, "durability": "once", "gate": False,
                            "hint": ""}]}]


def test_a_bare_none_on_a_required_fact_teaches_the_rephrase(monkeypatch):
    """"Finn has no medical conditions" -> the model keyed "None" -> the gate
    refused it as a placeholder and the parent was asked to repeat himself.
    Still refused (a bare placeholder cannot be told from filler), but the
    message now says how to record a genuine none, so the model rephrases
    instead of re-asking."""
    from app import requirement_groups as rg
    subs, err = rg.subjects_from_args(
        {"subjects": [{"name": "Finn", "group": "Child",
                       "fields": {"Medical conditions, allergies or medication": "None"}}]}, NONE_GROUPS)
    assert err is None and subs[0]["data"] == {}
    assert subs[0]["placeholders"] == {"Medical conditions, allergies or medication": "None"}
    # The rephrase the message asks for passes the gate.
    subs2, _ = rg.subjects_from_args(
        {"subjects": [{"name": "Finn", "group": "Child",
                       "fields": {"Medical conditions, allergies or medication":
                                  "No medical conditions, allergies or medication — per the parent"}}]},
        NONE_GROUPS)
    assert subs2[0]["data"] == {"medical": "No medical conditions, allergies or medication — per the parent"}
    assert subs2[0]["placeholders"] == {}


def test_the_refusal_message_carries_the_teaching(monkeypatch):
    inserted: list = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-kris"})
    monkeypatch.setattr(scheduling.db, "list_requirement_instances", lambda *a, **k: [])
    execute, _state = scheduling.make_executor("t1", contact="kris@x.co", service_types=SERVICES,
                                               groups=NONE_GROUPS, now=NOW)
    out = execute("hold_booking", {"date": "2026-09-15", "window": "08:00", "service": "MOT",
                              "summary": "Assessment — Finn",
                              "subjects": [{"name": "Finn", "group": "Child",
                                            "fields": {"Medical conditions, allergies or medication": "None"}}]})
    assert out["error"] == "missing_info"
    assert "'None' was given for Finn's Medical conditions" in out["message"]
    assert "write their answer in words" in out["message"]
    assert not inserted


def test_subject_description_teaches_a_genuine_none():
    from app import requirement_groups as rg
    assert "never a bare 'none'" in rg.render_for_prompt(NONE_GROUPS)


def _cp12(address: str, window: str = "am"):
    return {"date": "2026-09-15", "window": window, "service": "MOT",
            "summary": f"Gas safety record — {address}", "details": {"Job address": address}}


def test_a_different_job_address_is_its_own_hold(monkeypatch):
    """Kris 2026-09-26, plumber rig: a letting agent's three certificates at
    three houses got ONE hold, and the reply said the diary allows one per
    email. Three addresses are three visits; the same address twice is not."""
    inserted = []
    _exec(monkeypatch, inserted)          # the diary fakes; a plumber has no vehicle kind
    execute, state = scheduling.make_executor("t1", contact="rachel@x.co", service_types=SERVICES,
                                              groups=[], now=NOW)
    a = execute("hold_booking", _cp12("12 Oldfield Road, Bath", "08:00"))
    b = execute("hold_booking", _cp12("5 Moorland Road, Bath", "08:45"))
    c = execute("hold_booking", _cp12("41 Wellsway, Bath", "09:30"))
    assert all(x.get("held") for x in (a, b, c)), (a, b, c)
    again = execute("hold_booking", _cp12("12 Oldfield Road Bath", "10:15"))
    assert again["error"] in ("already_held", "hold_limit")
    assert len(inserted) == 3
