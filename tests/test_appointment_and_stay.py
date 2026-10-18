"""A new dog's meet & greet AND trial night, in one email (docs/plan-diary-multiview.md
stage 2, Kris 2026-09-23).

On a stays diary an appointment and a stay are different KINDS of visit. The in-email
"one hold per visit" rule counted every hold against every other, so the second of the
two was refused as `already_held`. Now each kind is counted on its own.

Also found on the way: with an appointment pool configured, the STAY check picked that
pool as the kennels (a lone resource type auto-selects) — boarding capped at "2 at
once" and filed as a meet & greet — and the agent was told the pool was a kind of space.

Run: python -m pytest tests/test_appointment_and_stay.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import diary_config, scheduling
from app.intake import STAY_FIELD
from tests.test_stay_executor import CFG, NOW

MEET = {"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}
APPT_CFG = {**CFG, "intake_fields": [dict(STAY_FIELD)], "service_types": [MEET],
            "resource_types": [{"name": "Meet & greet", "count": 2}]}
TRIAL = {"arrival": "2026-06-30T13:30", "departure": "2026-07-01T09:30", "summary": "Trial night for Otis",
         "subjects": [{"name": "Otis"}]}
M_AND_G = {"service": "Meet & greet", "date": "2026-06-30", "window": "13:00", "summary": "Meet & greet for Otis",
           "subjects": [{"name": "Otis"}]}


def _exec(monkeypatch, inserted, released=None, offered=None):
    released = released if released is not None else []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(APPT_CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: list(offered or []))
    monkeypatch.setattr(scheduling.db, "count_service_bookings_by_day", lambda *a, **k: {})
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact",
                        lambda *a, **k: released.append(k.get("exclude_ids")) or 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": f"b{len(inserted) + 1}", "booking_ref": f"BK-90{len(inserted) + 1:02d}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, "status": "proposed", **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    execute, state = scheduling.make_executor("t1", contact="sam@x.co", stay=True, now=NOW,
                                              service_types=[MEET])
    return execute, state


# ── the space pool is the kennels, never the appointment's ───────────────────
def test_a_stay_never_lands_in_the_appointment_pool(monkeypatch):
    execute, _ = _exec(monkeypatch, [])
    out = execute("check_availability", {"arrival": TRIAL["arrival"], "departure": TRIAL["departure"]})
    assert out["available"] is True and "resource" not in out
    assert scheduling.stay_resource_types(APPT_CFG) == []


def test_real_space_types_survive_beside_an_appointment_pool():
    cfg = {**APPT_CFG, "resource_types": [{"name": "Meet & greet", "count": 2}, {"name": "Large kennel", "count": 4}]}
    assert [r["name"] for r in scheduling.stay_resource_types(cfg)] == ["Large kennel"]


def test_the_agent_is_not_told_the_appointment_pool_is_a_kind_of_space():
    assert "Resource types" not in diary_config.render_all_for_prompt(APPT_CFG)
    windows = {**APPT_CFG, "intake_fields": []}                       # not a stays diary: unchanged
    assert "Meet & greet (2 available" in diary_config.render_all_for_prompt(windows)


# ── one email, both visits ───────────────────────────────────────────────────
def test_meet_and_greet_then_trial_night(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, inserted)
    a = execute("hold_appointment", dict(M_AND_G))
    assert a.get("held") is True, a
    s = execute("hold_booking", dict(TRIAL))
    assert s.get("held") is True, s
    assert [r.get("window") or "day" for r in inserted] == ["13:00", "day"]


def test_trial_night_then_meet_and_greet(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, inserted)
    assert execute("hold_booking", dict(TRIAL)).get("held") is True
    a = execute("hold_appointment", dict(M_AND_G))
    assert a.get("held") is True, a


def test_the_same_kind_twice_is_still_refused(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, inserted)
    assert execute("hold_appointment", dict(M_AND_G)).get("held") is True
    again = execute("hold_appointment", {**M_AND_G, "window": "14:00"})
    assert again["error"] == "already_held"
    assert execute("hold_booking", dict(TRIAL)).get("held") is True
    overlap = execute("hold_booking", {**TRIAL, "arrival": "2026-06-30T15:00"})
    assert overlap["error"] == "already_held"


# ── booking one kind never releases the other ────────────────────────────────
def test_the_meet_and_greet_never_releases_the_reserved_trial_night(monkeypatch):
    """A trial night reserved (offered) while the owner's details were gathered —
    by an earlier email — survives the meet & greet being booked now."""
    reserved_stay = {"id": "s-old", "status": "offered", "window": "day", "booking_date": "2026-06-30"}
    old_option = {"id": "a-old", "status": "offered", "window": "15:00", "booking_date": "2026-06-30"}
    released = []
    execute, _ = _exec(monkeypatch, [], released=released, offered=[reserved_stay, old_option])
    assert execute("hold_appointment", dict(M_AND_G)).get("held") is True
    assert released and "s-old" in released[-1] and "a-old" not in released[-1]


# ── a blocked day takes no drop-off or collection (live 2026-09-24) ──────────
def test_a_blocked_day_refuses_a_daycare_day_and_a_stays_ends_but_not_its_middle(monkeypatch):
    # Claire's daycare day went into the owner's 1-6 Oct block; the block
    # card promises "nothing can be booked on those days".
    global APPT_CFG
    was = APPT_CFG
    APPT_CFG = {**was, "blackout_dates": ["2026-06-30"]}
    try:
        inserted = []
        execute, _ = _exec(monkeypatch, inserted)
        day = execute("hold_booking", {"arrival": "2026-06-30T08:00", "departure": "2026-06-30T17:00",
                                       "summary": "Daycare for Alfie"})
        assert day["error"] == "closed" and day["closed_day"] == "2026-06-30"
        assert "CLOSED" in day["message"] and "never full" in day["message"]
        arrive = execute("check_availability", {"arrival": "2026-06-30T13:30", "departure": "2026-07-01T09:30"})
        assert arrive["error"] == "closed" and "drop-off" in arrive["message"]
        leave = execute("check_availability", {"arrival": "2026-06-29T13:30", "departure": "2026-06-30T09:30"})
        assert leave["error"] == "closed" and "collection" in leave["message"]
        through = execute("check_availability", {"arrival": "2026-06-29T13:30", "departure": "2026-07-01T09:30"})
        assert through.get("available") is True          # running THROUGH a closed day is fine
        assert inserted == []
    finally:
        APPT_CFG = was
