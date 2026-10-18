"""Hand edits on a customer's record (src/app/record_edits.py, 2026-09-06): the
contact page's pencil, field editor and note box, and the Concierge's
update_contact card, all through one code path."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import concierge_actions, dashboard, record_edits
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

_DEFS = [{"entity": "contact", "key": "access_arrangements", "label": "Access arrangements", "field_type": "text"},
         {"entity": "contact", "key": "regular_cleaner", "label": "Regular cleaner", "field_type": "dropdown",
          "options": ["Sam", "Priya", "Lou"]},
         {"entity": "contact", "key": "hours", "label": "Hours per visit", "field_type": "number"},
         {"entity": "contact", "key": "dvsa", "label": "DVSA record", "field_type": "dropdown",
          "options": ["Introduced", "Helped"], "ladder": {"notify": "approve", "gates": []}},
         {"entity": "company", "key": "sector", "label": "Sector", "field_type": "text"}]


def _contact():
    return {"id": "c7", "name": "Sophie Lindqvist", "email": "sophie@x.com", "phone": "",
            "custom_data": {"access_arrangements": "knock", "regular_cleaner": "Sam", "hours": 2.5}}


@pytest.fixture
def stores(monkeypatch):
    merged, acts, events, updated = [], [], [], []
    monkeypatch.setattr(record_edits.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, client=None, overwrite=True:
                        merged.append((rid, values, overwrite)) or {})
    monkeypatch.setattr(record_edits.db, "crm_insert_activity",
                        lambda tid, **k: acts.append(k) or True)
    monkeypatch.setattr(record_edits.db, "log_agent_event", lambda *a, **k: events.append(a[2]))
    monkeypatch.setattr(record_edits.db, "crm_update_contact",
                        lambda cid, tid, fields: updated.append((cid, fields)))
    monkeypatch.setattr(record_edits.db, "crm_contact_by_email", lambda tid, e, client=None: None)
    return {"merged": merged, "acts": acts, "events": events, "updated": updated}


# ── the module ───────────────────────────────────────────────────────────────
def test_set_field_coerces_writes_and_stamps_the_timeline(stores):
    old, new = record_edits.set_contact_field("t1", _contact(), _DEFS[1], "Priya")
    assert (old, new) == ("Sam", "Priya")
    assert stores["merged"] == [("c7", {"regular_cleaner": "Priya"}, True)]
    assert stores["acts"][0]["activity_type"] == "note" and stores["acts"][0]["subject"] == "Record updated"
    assert stores["acts"][0]["summary"] == "Regular cleaner set to 'Priya' (was 'Sam') — by you, from the record page"
    assert stores["acts"][0]["source_id"].startswith("edit:")
    assert "Sophie Lindqvist's Regular cleaner to 'Priya'" in stores["events"][0]


def test_set_field_refuses_bad_values_no_ops_and_clears(stores):
    with pytest.raises(record_edits.EditError) as e:
        record_edits.set_contact_field("t1", _contact(), _DEFS[1], "Dave")
    assert "choose one of: Sam, Priya, Lou" in str(e.value)
    with pytest.raises(record_edits.EditError) as e2:
        record_edits.set_contact_field("t1", _contact(), _DEFS[1], "Sam")
    assert "already 'Sam'" in str(e2.value)
    with pytest.raises(record_edits.EditError) as e3:
        record_edits.set_contact_field("t1", _contact(), _DEFS[2], "lots")
    assert "takes a number" in str(e3.value)
    old, new = record_edits.set_contact_field("t1", _contact(), _DEFS[0], "")
    assert (old, new) == ("knock", "—") and stores["merged"][-1] == ("c7", {"access_arrangements": ""}, True)
    with pytest.raises(record_edits.EditError):
        record_edits.set_contact_field("t1", {"id": "c7", "custom_data": {}}, _DEFS[0], "")


def test_set_field_on_a_ladder_fires_the_level_hook(stores, monkeypatch):
    from app import ladders
    fired = []
    monkeypatch.setattr(ladders, "contact_field_changed",
                        lambda tid, contact, key, old, new, client=None: fired.append((key, old, new)) or True)
    record_edits.set_contact_field("t1", _contact(), _DEFS[3], "Helped")
    assert fired == [("dvsa", None, "Helped")]


def test_note_and_basics(stores):
    body = record_edits.add_contact_note("t1", _contact(), "  Knocks   twice ")
    assert body == "Knocks twice"
    assert stores["acts"][-1]["subject"] == "Note" and stores["acts"][-1]["summary"] == "Knocks twice"
    with pytest.raises(record_edits.EditError):
        record_edits.add_contact_note("t1", _contact(), "   ")
    changes = record_edits.edit_contact_basics("t1", _contact(), name="Sophie L", email="sophie@x.com",
                                               phone="07700 900 123")
    assert changes == {"name": ("Sophie Lindqvist", "Sophie L"), "phone": ("", "07700 900 123")}
    assert stores["updated"] == [("c7", {"name": "Sophie L", "phone": "07700 900 123"})]
    assert "name 'Sophie Lindqvist' → 'Sophie L'" in stores["acts"][-1]["summary"]
    with pytest.raises(record_edits.EditError):
        record_edits.edit_contact_basics("t1", _contact(), name="Sophie Lindqvist", email="sophie@x.com", phone="")
    with pytest.raises(record_edits.EditError) as e:
        record_edits.edit_contact_basics("t1", _contact(), email="not-an-email")
    assert "doesn't look like an email" in str(e.value)


def test_basics_refuses_an_email_that_is_someone_elses(stores, monkeypatch):
    monkeypatch.setattr(record_edits.db, "crm_contact_by_email",
                        lambda tid, e, client=None: {"id": "c9", "name": "Sam Deane"})
    with pytest.raises(record_edits.EditError) as e:
        record_edits.edit_contact_basics("t1", _contact(), email="sam@x.com")
    assert "already belongs to Sam Deane" in str(e.value)


def test_find_field_by_key_label_or_loose_match():
    assert record_edits.find_field(_DEFS, "regular cleaner")["key"] == "regular_cleaner"
    assert record_edits.find_field(_DEFS, "ACCESS_ARRANGEMENTS")["key"] == "access_arrangements"
    assert record_edits.find_field(_DEFS, "hours")["key"] == "hours"
    assert record_edits.find_field(_DEFS, "sector") is None       # a company field
    assert record_edits.find_field(_DEFS, "") is None


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def page(monkeypatch, stores):
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    monkeypatch.setattr(dashboard.db, "crm_get_contact", lambda cid, tid, client=None: _contact() if cid == "c7" else None)
    monkeypatch.setattr(dashboard.db, "crm_list_field_definitions", lambda tid, entity=None, client=None: _DEFS)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_fields_route_writes_only_what_changed(page, stores):
    r = page.post("/app/crm/contacts/c7/fields",
                  data={"f_access_arrangements": "knock", "f_regular_cleaner": "Lou", "f_hours": "2.5",
                        "f_dvsa": "Helped"},            # a ladder key is ignored here (its own control)
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/crm/contacts/c7?msg=fields"
    assert stores["merged"] == [("c7", {"regular_cleaner": "Lou"}, True)]
    r2 = page.post("/app/crm/contacts/c7/fields", data={"f_regular_cleaner": "Sam"}, follow_redirects=False)
    assert r2.headers["location"].endswith("?msg=nochange")
    r3 = page.post("/app/crm/contacts/c7/fields", data={"f_regular_cleaner": "Dave"}, follow_redirects=False)
    assert "msg=fielderr" in r3.headers["location"] and "choose%20one%20of" in r3.headers["location"]
    assert page.post("/app/crm/contacts/zz/fields", data={}, follow_redirects=False).status_code == 404


def test_note_and_edit_routes(page, stores):
    r = page.post("/app/crm/contacts/c7/note", data={"note": "Keys in the safe"}, follow_redirects=False)
    assert r.headers["location"] == "/app/crm/contacts/c7?msg=noted"
    assert stores["acts"][-1]["summary"] == "Keys in the safe"
    r2 = page.post("/app/crm/contacts/c7/note", data={"note": " "}, follow_redirects=False)
    assert "msg=editerr" in r2.headers["location"]
    r3 = page.post("/app/crm/contacts/c7/edit", data={"name": "Sophie L", "email": "sophie@x.com", "phone": ""},
                   follow_redirects=False)
    assert r3.headers["location"] == "/app/crm/contacts/c7?msg=edited"
    assert stores["updated"] == [("c7", {"name": "Sophie L"})]
    r4 = page.post("/app/crm/contacts/c7/edit", data={"name": "Sophie Lindqvist", "email": "bad", "phone": ""},
                   follow_redirects=False)
    assert "msg=editerr" in r4.headers["location"]


# ── the Concierge card runs through the same module ─────────────────────────
def test_concierge_update_contact_uses_record_edits(monkeypatch, stores):
    monkeypatch.setattr(concierge_actions.db, "crm_get_contact", lambda cid, tid, client=None: _contact())
    monkeypatch.setattr(concierge_actions.db, "crm_list_field_definitions",
                        lambda tid, entity=None, client=None: _DEFS)
    out = asyncio.run(concierge_actions._exec_update_contact(
        "t1", None, {"contact_id": "c7", "contact": "sophie@x.com", "name": "Sophie Lindqvist",
                     "mode": "field", "key": "regular_cleaner", "label": "Regular cleaner",
                     "old": "Sam", "value": "Priya", "shown": "Priya"}, ""))
    assert out["summary"] == "Done — Sophie Lindqvist's Regular cleaner is now 'Priya'."
    assert stores["merged"] == [("c7", {"regular_cleaner": "Priya"}, True)]
    assert stores["acts"][-1]["summary"].endswith("— by you, from the Concierge")
    out2 = asyncio.run(concierge_actions._exec_update_contact(
        "t1", None, {"contact_id": "c7", "name": "Sophie Lindqvist", "mode": "note",
                     "content": "Wants us to knock twice"}, ""))
    assert out2["summary"] == "Done — the note is on Sophie Lindqvist's record."
    with pytest.raises(concierge_actions.ActionError) as e:
        asyncio.run(concierge_actions._exec_update_contact(
            "t1", None, {"contact_id": "c7", "name": "Sophie", "mode": "field", "key": "regular_cleaner",
                         "value": "Sam"}, ""))
    assert "already 'Sam'" in str(e.value)


def test_the_page_carries_the_pencil_the_editor_and_the_note_box():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "src/app" / "templates" / "app" / "crm_contact.html").read_text(encoding="utf-8")
    for needle in ("/edit\"", "/fields\"", "/note\"", "Edit values", "editable_fields", "name=\"f_{{ d.key }}\""):
        assert needle in page
    assert "or edit them here" in page


# ── bookings by hand: plan a move, apply it, rename (item 2, 2026-09-06) ─────
_SVC_CFG = {"windows": "day", "slot_minutes": 60, "capacity_per_window": 3, "buffer_minutes": 30,
            "working_days": "mon,tue,wed,thu,fri", "min_notice_hours": 72,
            "service_types": [{"name": "Regular clean", "minutes": 120, "timing": "half"}]}


def _booking(**over):
    b = {"id": "b1", "booking_ref": "BK-1078", "booking_date": "2099-09-07", "window": "am",
         "start_block": 36, "length_blocks": 10, "status": "confirmed", "contact": "m@x.com",
         "summary": "Regular clean — Margaret", "details": {"Hours": "2.5"}}
    b.update(over)
    return b


def test_plan_move_on_a_services_diary_uses_the_engine_without_the_notice_cutoff(monkeypatch):
    monkeypatch.setattr(record_edits.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SVC_CFG))
    monkeypatch.setattr(record_edits.db, "list_active_bookings",
                        lambda tid, a, b, client=None: [{"id": "b1"}, {"id": "other"}])
    seen = {}
    from app import scheduling

    def fit(cfg, booked, length, day, window, now, **k):
        seen.update(window=window, now=now, booked=[x["id"] for x in booked], **k)
        return 48 if window == "pm" else None
    monkeypatch.setattr(scheduling, "first_fit_start", fit)
    plan = record_edits.plan_booking_move("t1", _booking(), "2099-09-09", "afternoon")
    assert plan == {"timed": True, "new_date": "2099-09-09", "new_window": "pm", "new_start_block": 48,
                    "length_blocks": 10, "when": "9th Sep 99 at 12:00", "at": "12:00"}
    assert seen["now"] is None and seen["booked"] == ["other"] and seen["buffer_blocks"] == 2
    with pytest.raises(record_edits.EditError) as e:
        record_edits.plan_booking_move("t1", _booking(), "2099-09-09", "morning")
    assert "no room in the morning for a 2.5-hour visit" in str(e.value)
    # a clock time keeps the block but is stored in its half (live 2026-09-06)
    monkeypatch.setattr(scheduling, "first_fit_start", lambda cfg, booked, length, day, window, now, **k: 36)
    plan2 = record_edits.plan_booking_move("t1", _booking(booking_date="2099-09-08"), "2099-09-09", "09:00")
    assert plan2["new_window"] == "am" and plan2["new_start_block"] == 36 and plan2["at"] == "09:00"
    with pytest.raises(record_edits.EditError):
        record_edits.plan_booking_move("t1", _booking(), "2099-09-09", "teatime")
    with pytest.raises(record_edits.EditError):
        record_edits.plan_booking_move("t1", _booking(), "2020-01-01", "pm")
    with pytest.raises(record_edits.EditError):
        record_edits.plan_booking_move("t1", _booking(status="cancelled"), "2099-09-09", "pm")


def test_plan_move_on_a_legacy_diary_uses_windows(monkeypatch):
    cfg = {"windows": "ampm", "capacity_per_window": 2, "working_days": "mon,tue,wed,thu,fri",
           "min_notice_hours": 24, "horizon_days": 30000, "blackout_dates": []}
    monkeypatch.setattr(record_edits.db, "get_scheduling_config", lambda tid, client=None: carried_config(cfg))
    from app import scheduling
    monkeypatch.setattr(scheduling, "hold_error", lambda cfg, d, w, now: None)
    monkeypatch.setattr(scheduling, "check_availability",
                        lambda tid, a, b, client=None: [{"date": "2099-09-09", "window": "pm", "remaining": 1}])
    plan = record_edits.plan_booking_move("t1", _booking(start_block=None), "2099-09-09", "pm")
    assert plan == {"timed": False, "new_date": "2099-09-09", "new_window": "pm", "when": "9th Sep 99 (pm)"}
    with pytest.raises(record_edits.EditError) as e:
        record_edits.plan_booking_move("t1", _booking(start_block=None), "2099-09-09", "am")
    assert "already taken" in str(e.value)


def test_apply_move_writes_in_place_refreshes_the_calendar_and_logs(monkeypatch):
    calls, events = {}, []
    monkeypatch.setattr(record_edits.db, "move_booking_in_place",
                        lambda bid, tid, d, sb, ln, w, client=None: calls.update(bid=bid, d=d, sb=sb, ln=ln, w=w)
                        or {"id": bid, "status": "confirmed"})
    monkeypatch.setattr(record_edits.db, "log_agent_event", lambda *a, **k: events.append(a[2]))
    from app import calendar_sync
    pushed = []

    async def _rm(tid, bid):
        pushed.append(("rm", bid))

    async def _push(tid, booking_id=None):
        pushed.append(("push", booking_id))
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _rm)
    monkeypatch.setattr(calendar_sync, "push_bookings", _push)
    plan = {"timed": True, "new_date": "2099-09-09", "new_window": "pm", "new_start_block": 48,
            "length_blocks": 10, "when": "9th Sep 99 at 12:00"}
    out = asyncio.run(record_edits.apply_booking_move("t1", _booking(), plan))
    assert out == "Done — Regular clean — Margaret moved to 9th Sep 99 at 12:00."
    assert calls == {"bid": "b1", "d": "2099-09-09", "sb": 48, "ln": 10, "w": "pm"}
    assert pushed == [("rm", "b1"), ("push", "b1")]
    assert events[0] == "You moved BK-1078 from 7th Sep 99 at 09:00 to 9th Sep 99 at 12:00 (the diary)"
    monkeypatch.setattr(record_edits.db, "move_booking_in_place", lambda *a, **k: None)
    with pytest.raises(record_edits.EditError) as e:
        asyncio.run(record_edits.apply_booking_move("t1", _booking(), plan))
    assert "filled up" in str(e.value)


def test_rename_a_booking(monkeypatch):
    saved, events = {}, []
    monkeypatch.setattr(record_edits.db, "rename_booking",
                        lambda bid, tid, summary, client=None: saved.update(bid=bid, summary=summary))
    monkeypatch.setattr(record_edits.db, "log_agent_event", lambda *a, **k: events.append(a[2]))
    assert record_edits.edit_booking_summary("t1", _booking(), "  Deep clean —  Margaret ") == "Deep clean — Margaret"
    assert saved == {"bid": "b1", "summary": "Deep clean — Margaret"}
    assert "'Regular clean — Margaret' → 'Deep clean — Margaret'" in events[0]
    with pytest.raises(record_edits.EditError):
        record_edits.edit_booking_summary("t1", _booking(), "Regular clean — Margaret")
    with pytest.raises(record_edits.EditError):
        record_edits.edit_booking_summary("t1", _booking(), "  ")


def test_diary_edit_route_moves_and_renames(monkeypatch):
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        monkeypatch.setattr(dashboard.db, "get_booking", lambda bid, tid, client=None: _booking() if bid == "b1" else None)
        planned, applied, renamed = [], [], []
        monkeypatch.setattr(record_edits, "plan_booking_move",
                            lambda tid, b, d, w, client=None: planned.append((d, w)) or {"timed": True, "when": "x"})

        async def _apply(tid, b, plan, via="the diary", client=None):
            applied.append(plan)
            return "Done."
        monkeypatch.setattr(record_edits, "apply_booking_move", _apply)
        monkeypatch.setattr(record_edits, "edit_booking_summary",
                            lambda tid, b, s, via="the diary", client=None: renamed.append(s) or s)
        c = TestClient(app)
        r = c.post("/app/diary/b1/edit", data={"summary": "Regular clean — Margaret", "new_date": "2099-09-09",
                                               "when": "afternoon"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/diary/b1?msg=moved"
        assert planned == [("2099-09-09", "afternoon")] and len(applied) == 1 and renamed == []
        r2 = c.post("/app/diary/b1/edit", data={"summary": "Deep clean", "new_date": "", "when": ""},
                    follow_redirects=False)
        assert r2.headers["location"] == "/app/diary/b1?msg=renamed" and renamed == ["Deep clean"]
        # the form always posts the prefilled day + time: unchanged = no move
        # (live 2026-09-06: a rename-only save re-placed the booking at 08:00)
        r2b = c.post("/app/diary/b1/edit", data={"summary": "Deeper clean", "new_date": "2099-09-07",
                                                 "when": "09:00", "when_was": "09:00"}, follow_redirects=False)
        assert r2b.headers["location"] == "/app/diary/b1?msg=renamed" and len(applied) == 1
        r3 = c.post("/app/diary/b1/edit", data={"summary": "Regular clean — Margaret", "new_date": "", "when": ""},
                    follow_redirects=False)
        assert r3.headers["location"].endswith("?msg=nochange")
        monkeypatch.setattr(record_edits, "plan_booking_move",
                            lambda *a, **k: (_ for _ in ()).throw(record_edits.EditError("no room in the morning")))
        r4 = c.post("/app/diary/b1/edit", data={"new_date": "2099-09-09", "when": "morning"}, follow_redirects=False)
        assert "msg=editerr" in r4.headers["location"] and "no%20room" in r4.headers["location"]
        assert c.post("/app/diary/zz/edit", data={}, follow_redirects=False).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_the_job_page_carries_the_move_form():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    page = (root / "src/app" / "templates" / "app" / "diary_job.html").read_text(encoding="utf-8")
    assert "/edit\"" in page and "Move or rename" in page and 'name="when"' in page and 'name="new_date"' in page


def test_concierge_move_stager_uses_the_shared_plan(monkeypatch):
    monkeypatch.setattr(concierge_actions.db, "get_booking", lambda bid, tid, client=None: _booking())
    monkeypatch.setattr(concierge_actions.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SVC_CFG))
    monkeypatch.setattr(record_edits, "plan_booking_move",
                        lambda tid, b, d, w, client=None: {"timed": True, "new_date": d, "new_window": "pm",
                                                          "new_start_block": 48, "length_blocks": 10,
                                                          "when": "9th Sep 99 at 12:00", "at": "12:00"})
    summary, payload, details, _ = concierge_actions._stage_move_booking(
        "t1", None, {"booking_id": "0f6f74c1-7810-4225-9fd7-0381e1c99413", "new_date": "2099-09-09",
                     "new_window": "afternoon"})
    assert summary == "Move Regular clean — Margaret: 7th Sep 99 (am) → 9th Sep 99 at 12:00"
    assert payload["timed"] and payload["new_start_block"] == 48 and payload["booking_id"] == "b1"
    assert details[1] == "New: 9th Sep 99 — arriving 12:00 (2.5 hours)"

from tests.booking_fixtures import carried_config


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
