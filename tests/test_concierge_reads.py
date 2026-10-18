"""The reads batch (2026-09-06): get_availability, get_contact_record,
list_dormant_contacts — the three owner questions the July catalogue could
not answer (docs/archive/2026-09/concierge-pass1-findings-2026-09-06.md, F3/F4b/F13b)."""
import asyncio
from datetime import date, timedelta

import pytest

from app import concierge, concierge_data, concierge_reads, concierge_routes

_SERVICES_CFG = {"windows": "day", "slot_minutes": 60, "open_time": "08:00:00",
                 "close_time": "17:00:00", "capacity_per_window": 3, "buffer_minutes": 30,
                 "working_days": "mon,tue,wed,thu,fri", "min_notice_hours": 72,
                 "horizon_days": 60, "blackout_dates": ["2026-09-11"],
                 "service_types": [{"name": "Regular clean", "minutes": 120, "timing": "half"},
                                   {"name": "End of tenancy / deep clean", "minutes": 240, "timing": "half"}]}


# ── catalogue ────────────────────────────────────────────────────────────────
def test_reads_are_in_the_catalogue_with_the_right_gates():
    names = {t["name"] for t in concierge_data.TOOLS}
    assert {"get_availability", "get_contact_record", "list_dormant_contacts"} <= names
    starter = {t["name"] for t in concierge_data.tools_for(False)}
    assert "get_availability" in starter                 # the diary is every tier's
    assert "get_contact_record" not in starter and "list_dormant_contacts" not in starter
    pro = {t["name"] for t in concierge_data.tools_for(True)}
    assert {"get_contact_record", "list_dormant_contacts"} <= pro
    for name in ("get_availability", "get_contact_record", "list_dormant_contacts"):
        assert name in concierge_routes._BUSINESS_TOOL_LABELS
        assert name in concierge_routes._BUSINESS_TOOL_HINTS


def test_prompt_routes_the_three_questions():
    p = concierge.build_business_prompt(diary_shape="a timed diary")
    assert "call get_availability" in p
    assert "11. WHICH TOOL FIRST" in p
    assert "get_contact_record (the record itself)" in p
    assert "list_dormant_contacts" in p


def test_an_unknown_availability_is_said_in_the_tool_and_in_the_prompt():
    # Wave 3a (N9): the result says availability_unknown; the words that tell the brain
    # what to do with it ride the description and rule 11 (DIARY SHAPE is absent from voice).
    tool = next(t for t in concierge_data.TOOLS if t["name"] == "get_availability")
    assert "availability_unknown" in tool["description"] and "never 'full' or 'booked'" in tool["description"]
    p = concierge.build_business_prompt()
    assert "an `availability_unknown` result means you cannot check it from here" in p
    assert "never 'full')" in p


# ── get_availability ─────────────────────────────────────────────────────────
def test_availability_reports_fit_per_window_and_closed_days(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SERVICES_CFG))
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings",
                        lambda tid, a, b, client=None: [
                            {"id": "x", "booking_date": "2026-09-10", "start_block": 32, "length_blocks": 12},
                            {"id": "y", "booking_date": "2026-09-10", "start_block": 46, "length_blocks": 2}])
    # the engine: customers may book Thursday pm only; the owner can still place am
    monkeypatch.setattr(concierge_reads.scheduling, "service_availability",
                        lambda *a, **k: [{"date": "2026-09-10", "window": "pm", "label": "Afternoon", "remaining": 2}])
    monkeypatch.setattr(concierge_reads.scheduling, "first_fit_start",
                        lambda cfg, booked, length, day, window, now, **k: 34 if window == "am" else 48)
    out = concierge_reads.get_availability("t1", None, {"date_from": "2026-09-10", "date_to": "2026-09-12",
                                                        "service": "End of tenancy / deep clean"})
    assert out["service"] == "End of tenancy / deep clean" and out["length_minutes"] == 240
    assert out["capacity_at_once"] == 3 and out["gap_between_bookings_minutes"] == 30
    thu, fri, sat = out["days"]
    assert thu["open"] and thu["bookings"] == 2
    am, pm = thu["slots"]
    assert am["window"] == "am" and am["fits"] is True and am["earliest_start"] == "08:30"
    assert am["remaining_for_customers"] == 0 and "notice" in am["note"]
    assert pm["fits"] is True and pm["remaining_for_customers"] == 2
    assert fri["open"] is False and fri["why"] == "blackout date"
    assert sat["open"] is False and sat["why"] == "not a working day"


def test_availability_unknown_service_lists_the_menu(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SERVICES_CFG))
    out = concierge_reads.get_availability("t1", None, {"date_from": "2026-09-10", "service": "Window clean"})
    assert out["error"] == "unknown_service" and "Regular clean" in out["message"]


def test_availability_caps_the_range_and_narrows_to_a_window(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: carried_config(_SERVICES_CFG))
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.scheduling, "service_availability", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.scheduling, "first_fit_start", lambda *a, **k: 32)
    out = concierge_reads.get_availability("t1", None, {"date_from": "2026-09-07", "date_to": "2026-12-01",
                                                        "window": "afternoon"})
    assert out["count"] == concierge_reads.MAX_DAYS
    monday = out["days"][0]
    assert [s["window"] for s in monday["slots"]] == ["pm"]


def test_availability_legacy_windows_diary(monkeypatch):
    cfg = {"windows": "ampm", "capacity_per_window": 2, "working_days": "mon,tue,wed,thu,fri",
           "min_notice_hours": 24, "horizon_days": 30, "blackout_dates": []}
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: carried_config(cfg))
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.scheduling, "availability",
                        lambda *a, **k: [{"date": "2026-09-08", "window": "am", "remaining": 1}])
    out = concierge_reads.get_availability("t1", None, {"date_from": "2026-09-08"})
    slots = out["days"][0]["slots"]
    assert slots == [{"window": "am", "label": "am", "fits": True, "remaining_for_customers": 1}]


def _upcoming(weekday, at_least_days=3):
    d = date.today() + timedelta(days=at_least_days)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


# Capacity held by resource pools, none on the office grid: the engine has nothing to
# count against, so it offers nothing however empty the diary is (live 2026-10-14).
_NO_GRID_CAPACITY = {"windows": "slots", "slot_minutes": 30, "capacity_per_window": None,
                     "min_notice_hours": 24, "horizon_days": 60,
                     "resource_types": [{"name": "Room A", "count": 2}],
                     "booking_setting_review": {"capacity": {"state": "not_applicable"}},
                     "service_types": [{"name": "Service A", "minutes": 45}]}


def test_availability_says_unknown_when_the_engine_has_no_capacity_to_count_against(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_NO_GRID_CAPACITY))
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: [])
    monday = _upcoming(0)
    out = concierge_reads.get_availability("t1", None, {"date_from": monday.isoformat(),
                                                        "date_to": (monday + timedelta(days=2)).isoformat()})
    assert out["capacity_at_once"] == 0 and out["availability_unknown"] is True
    assert "NOT a full diary" in out["how_to_read"] and "NEVER say the days are full" in out["how_to_read"]
    slots = [s for day in out["days"] for s in day["slots"]]
    assert len(slots) == 3 and all(s["fits"] is False for s in slots)       # the values are kept
    assert all("unknown" in s["note"] and "full" not in s["note"] for s in slots)
    assert [day["bookings"] for day in out["days"]] == [0, 0, 0]            # the counts stay real


def test_availability_keeps_a_genuinely_full_day_full(monkeypatch):
    # The engine lists only windows with room left, so a full day returns NOTHING, exactly
    # like the case above; the diary's own capacity is what says this one really is full.
    cfg = carried_config({"windows": "day", "capacity_per_window": 1, "min_notice_hours": 0, "horizon_days": 60})
    day = _upcoming(0)
    booked = [{"id": "b1", "booking_date": day.isoformat(), "window": "day", "status": "confirmed"}]
    now = concierge_reads.datetime.now(concierge_reads.scheduling.DIARY_TZ)
    assert concierge_reads.scheduling.availability(cfg, [], day, day, now) != []
    assert concierge_reads.scheduling.availability(cfg, booked, day, day, now) == []
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: cfg)
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: booked)
    out = concierge_reads.get_availability("t1", None, {"date_from": day.isoformat(), "date_to": day.isoformat()})
    assert out["capacity_at_once"] == 1 and "availability_unknown" not in out
    assert out["how_to_read"].startswith("fits = the visit fits the diary's capacity")
    (slot,) = out["days"][0]["slots"]
    assert slot["fits"] is False and "full" in slot["note"] and out["days"][0]["bookings"] == 1


def test_an_all_closed_range_is_not_flagged_unknown(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config",
                        lambda tid, client=None: carried_config(_NO_GRID_CAPACITY))
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: [])
    sunday = _upcoming(6)
    out = concierge_reads.get_availability("t1", None, {"date_from": sunday.isoformat(), "date_to": sunday.isoformat()})
    assert out["days"][0]["open"] is False and out["days"][0]["why"] == "not a working day"
    assert "availability_unknown" not in out and out["how_to_read"].startswith("fits = ")


def test_a_fit_found_by_the_owners_own_view_is_not_flagged_unknown(monkeypatch):
    cfg = carried_config({**_SERVICES_CFG, "capacity_per_window": None,
                          "resource_types": [{"name": "Room A", "count": 2}],
                          "booking_setting_review": {"capacity": {"state": "not_applicable"}}})
    monkeypatch.setattr(concierge_reads.db, "get_scheduling_config", lambda tid, client=None: cfg)
    monkeypatch.setattr(concierge_reads.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.scheduling, "service_availability", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.scheduling, "first_fit_start", lambda *a, **k: 32)
    monday = _upcoming(0)
    out = concierge_reads.get_availability("t1", None, {"date_from": monday.isoformat(), "date_to": monday.isoformat()})
    assert out["capacity_at_once"] == 0 and "availability_unknown" not in out
    assert out["days"][0]["slots"][0]["fits"] is True


# ── get_contact_record ───────────────────────────────────────────────────────
def _contact():
    return {"id": "c1", "name": "Kris Deane", "email": "kris@x.com", "phone": "07495",
            "sentiment": "positive", "last_activity_at": "2026-09-05T23:03:00+00:00",
            "custom_data": {"regular_cleaner": "Sam", "alarm_code": "4471 then tick",
                            "first_aid": "2026-10-01", "pets": ""}}


def _record_fakes(monkeypatch, contacts):
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None: contacts)
    monkeypatch.setattr(concierge_reads.db, "crm_list_field_definitions",
                        lambda tid, client=None: [
                            {"entity": "contact", "key": "regular_cleaner", "label": "Regular cleaner",
                             "field_type": "dropdown", "display_order": 0, "hint": "Which cleaner holds the slot"},
                            {"entity": "contact", "key": "alarm_code", "label": "Alarm code", "field_type": "text",
                             "display_order": 10, "extraction_hint": "Kept on the record only — NEVER written into an email"},
                            {"entity": "contact", "key": "first_aid", "label": "First aid certificate",
                             "field_type": "date", "display_order": 20, "durability": "expires",
                             "recall_days": 90},
                            {"entity": "deal", "key": "x", "label": "Deal thing", "field_type": "text"}])
    monkeypatch.setattr(concierge_reads.db, "list_requirement_instances",
                        lambda tid, cid, client=None: [{"group_label": "Pet", "name": "Biscuit",
                                                        "data": {"breed": "Spaniel", "meds": ""}}])
    monkeypatch.setattr(concierge_reads.db, "list_documents",
                        lambda tid, **k: [{"label": "Vaccination card", "content_type": "image/jpeg",
                                           "created_at": "2026-09-01T10:00:00+00:00"}])
    monkeypatch.setattr(concierge_reads.db, "crm_open_promises",
                        lambda tid, contact_id=None, client=None: [
                            {"direction": "we_owe", "description": "Photograph the meter readings", "due_date": "2026-09-30"}])
    monkeypatch.setattr(concierge_reads.db, "search_bookings",
                        lambda tid, **k: [{"booking_date": "2026-08-25", "summary": "Regular clean", "booking_ref": "BK-1003"},
                                          {"booking_date": "2099-01-05", "summary": "Regular clean", "booking_ref": "BK-1004",
                                           "window": "am", "id": "b4"}])
    from app import customer_log, wallet
    monkeypatch.setattr(customer_log, "list_for_contact",
                        lambda tid, email, limit=50, client=None: [{"created_at": "2026-09-02", "author": "customer",
                                                                   "body": "Please knock twice"}])
    monkeypatch.setattr(wallet, "list_credits", lambda tid, cid, client=None: [
        {"package_name": "10-hour block", "quantity": 10, "used": 6, "unit": "hours", "expires_at": "2099-01-01"}])


def test_contact_record_shows_the_owner_everything_and_flags_never_email(monkeypatch):
    _record_fakes(monkeypatch, [_contact()])
    out = concierge_reads.get_contact_record("t1", None, {"contact": "Kris"})
    assert out["found"] and out["name"] == "Kris Deane"
    labels = {f["label"]: f for f in out["fields"]}
    assert labels["Regular cleaner"]["value"] == "Sam"
    assert labels["Alarm code"]["value"] == "4471 then tick"          # shown, not withheld
    assert labels["Alarm code"]["never_in_customer_email"] is True
    assert "never_in_customer_email" not in labels["Regular cleaner"]
    assert "pets" not in {f["label"].lower() for f in out["fields"]}   # empty values skipped
    assert "Deal thing" not in labels                                  # other entities skipped
    assert out["expiring"] and "First aid certificate" in out["expiring"][0]
    assert out["dependants"] == [{"kind": "Pet", "name": "Biscuit", "facts": {"breed": "Spaniel"}}]
    assert out["documents"][0]["name"] == "Vaccination card"
    assert out["open_promises"] == [{"who": "we owe", "what": "Photograph the meter readings", "due": "2026-09-30"}]
    assert out["log"][0]["note"] == "Please knock twice"
    assert out["credits"] == [{"package": "10-hour block", "remaining": 4.0, "unit": "hours", "expires": "2099-01-01"}]
    assert out["next_booking"]["reference"] == "BK-1004" and out["next_booking"]["booking_id"] == "b4"
    assert out["last_booking"]["reference"] == "BK-1003" and out["live_bookings"] == 1


def test_contact_record_asks_which_one_and_survives_a_dead_store(monkeypatch):
    two = [_contact(), {**_contact(), "id": "c2", "name": "Kris Patel", "email": "kp@x.com"}]
    _record_fakes(monkeypatch, two)
    out = concierge_reads.get_contact_record("t1", None, {"contact": "Kris"})
    assert out["found"] is False and len(out["which"]) == 2
    # an exact email picks one of the two
    out2 = concierge_reads.get_contact_record("t1", None, {"contact": "kp@x.com"})
    assert out2["found"] and out2["name"] == "Kris Patel"
    # a store that blows up costs its section, not the answer
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(concierge_reads.db, "list_documents", boom)
    out3 = concierge_reads.get_contact_record("t1", None, {"contact": "kp@x.com"})
    assert out3["found"] and "documents" not in out3 and out3["fields"]
    assert concierge_reads.get_contact_record("t1", None, {"contact": ""})["found"] is False


# ── list_dormant_contacts ────────────────────────────────────────────────────
def test_dormant_contacts_are_those_without_a_booking_since(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts",
                        lambda tid, q=None, limit=100, client=None: [
                            {"id": "a", "name": "Active Anna", "email": "anna@x.com", "last_activity_at": "2026-09-01"},
                            {"id": "b", "name": "Lapsed Len", "email": "len@x.com", "last_activity_at": "2026-05-01"},
                            {"id": "c", "name": "Enquirer Eve", "email": "eve@x.com", "last_activity_at": "2026-08-20"},
                            {"id": "d", "name": "No Email", "email": None}])

    def between(tid, date_from, date_to, client=None):
        if date_from == "2026-07-01":
            return [{"contact": "anna@x.com", "booking_date": "2026-08-01"}]
        return [{"contact": "len@x.com", "booking_date": "2026-04-12"},
                {"contact": "len@x.com", "booking_date": "2026-03-01"}]
    monkeypatch.setattr(concierge_reads.db, "booking_contacts_between", between)
    out = concierge_reads.list_dormant_contacts("t1", None, {"since": "2026-07-01"})
    assert out["since"] == "2026-07-01" and out["contacts_checked"] == 4
    assert [d["name"] for d in out["dormant"]] == ["Enquirer Eve", "Lapsed Len"] or \
           [d["name"] for d in out["dormant"]] == ["Lapsed Len", "Enquirer Eve"]
    by = {d["name"]: d for d in out["dormant"]}
    assert by["Lapsed Len"]["last_booking"] == "2026-04-12"
    assert by["Enquirer Eve"]["last_booking"] is None
    assert out["count"] == 2 and out["active_since"] == 1


def test_dormant_defaults_to_ninety_days(monkeypatch):
    monkeypatch.setattr(concierge_reads.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(concierge_reads.db, "booking_contacts_between", lambda *a, **k: [])
    out = concierge_reads.list_dormant_contacts("t1", None, {})
    assert out["since"] == (date.today() - timedelta(days=90)).isoformat()


# ── the executor wiring + the 'completed' mapping ───────────────────────────
def test_execute_reaches_the_new_reads_and_gates_them(monkeypatch):
    monkeypatch.setattr(concierge_reads, "list_dormant_contacts", lambda t, c, a: {"dormant": [], "count": 0})
    out = asyncio.run(concierge_data.execute("t1", None, "list_dormant_contacts", {}, full_qa=True))
    assert out == {"dormant": [], "count": 0}
    locked = asyncio.run(concierge_data.execute("t1", None, "list_dormant_contacts", {}, full_qa=False))
    assert locked.get("locked") is True


def test_list_bookings_completed_means_confirmed_before_today(monkeypatch):
    seen = {}
    monkeypatch.setattr(concierge_data.db, "search_bookings",
                        lambda tid, **k: seen.update(k) or [])
    concierge_data._list_bookings("t1", None, {"status": "completed", "date_from": "2026-07-01"})
    assert seen["status"] == "confirmed"
    assert seen["date_to"] == (date.today() - timedelta(days=1)).isoformat()
    concierge_data._list_bookings("t1", None, {"status": "completed", "date_to": "2026-01-31"})
    assert seen["date_to"] == "2026-01-31"


# ── list_bookings for a named contact: cancelled fallback (wave 3b, 14 Oct rig N7) ──────────────
GONE = [{"id": "b1", "booking_date": "2026-10-13", "window": "am", "contact": "tom@x.com",
         "summary": "First job", "status": "cancelled", "booking_ref": "BK-1001", "quoted_amount": 45},
        {"id": "b2", "booking_date": "2026-10-13", "window": "pm", "contact": "tom@x.com",
         "summary": "Second job", "status": "cancelled", "booking_ref": "BK-1002", "quoted_amount": 120}]
LIVE = [{"id": "b3", "booking_date": "2026-10-20", "window": "am", "contact": "tom@x.com",
         "summary": "Third job", "status": "confirmed", "booking_ref": "BK-1003", "quoted_amount": 80}]


def fake_bookings(monkeypatch, live=(), cancelled=()):
    calls = []

    def search(tid, **k):
        calls.append(k)
        return list(cancelled if k.get("status") == "cancelled" else live)
    monkeypatch.setattr(concierge_data.db, "search_bookings", search)
    monkeypatch.setattr(concierge_data.db, "stays_running_into", lambda *a, **k: [])
    return calls


def test_list_bookings_for_a_named_contact_with_no_live_rows_returns_their_cancelled_ones(monkeypatch):
    calls = fake_bookings(monkeypatch, cancelled=GONE)
    out = concierge_data._list_bookings("t1", None, {"contact": "tom@x.com", "date_from": "2026-10-01"})
    assert out["bookings"] == [] and out["count"] == 0
    assert len(out["cancelled"]) == 2
    assert all(r["status"] == "cancelled" and r["booking_id"] for r in out["cancelled"])
    assert [r["reference"] for r in out["cancelled"]] == ["BK-1001", "BK-1002"]
    assert "cancelled bookings are listed under cancelled" in out["note"]
    # exactly two reads: the live diary first, then the cancelled ones for the same contact and range
    assert len(calls) == 2
    assert calls[0].get("status") is None and calls[0]["contact"] == "tom@x.com"
    assert calls[1]["status"] == "cancelled" and calls[1]["contact"] == "tom@x.com"
    assert calls[1]["date_from"] == "2026-10-01" and calls[1]["limit"] == calls[0]["limit"]


def test_list_bookings_with_live_rows_never_queries_cancelled(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE, cancelled=GONE)
    out = concierge_data._list_bookings("t1", None, {"contact": "tom@x.com"})
    assert len(calls) == 1 and "cancelled" not in out
    assert out["count"] == 1 and out["bookings"][0]["reference"] == "BK-1003"


def test_list_bookings_by_date_only_is_unchanged(monkeypatch):
    calls = fake_bookings(monkeypatch, cancelled=GONE)
    out = concierge_data._list_bookings("t1", None, {"date_from": "2026-10-13", "date_to": "2026-10-13"})
    assert len(calls) == 1 and out == {"bookings": [], "count": 0}


def test_list_bookings_with_a_status_given_never_falls_back(monkeypatch):
    calls = fake_bookings(monkeypatch, cancelled=GONE)
    out = concierge_data._list_bookings("t1", None, {"contact": "tom@x.com", "status": "confirmed"})
    assert len(calls) == 1 and out == {"bookings": [], "count": 0}


def test_list_bookings_with_nothing_cancelled_either_keeps_the_plain_empty_answer(monkeypatch):
    calls = fake_bookings(monkeypatch)
    out = concierge_data._list_bookings("t1", None, {"contact": "tom@x.com"})
    assert len(calls) == 2 and out == {"bookings": [], "count": 0}


def test_list_bookings_unknown_person_note_still_comes_first_on_the_fallback(monkeypatch):
    from app import job_people
    fake_bookings(monkeypatch, cancelled=GONE)
    monkeypatch.setattr(job_people, "team", lambda tid: [])
    out = concierge_data._list_bookings("t1", None, {"contact": "tom@x.com", "person": "Zed"})
    assert out["note"].startswith("Nobody called") and "cancelled" not in out


# ── list_bookings resolves a contact name (wave 3b, 14 Oct rig N8) ───────────────────────────────
TOM = {"id": "c1", "name": "Tom Hale", "email": "thale@mail.example"}


def fake_resolver(monkeypatch, result):
    asked = []

    def resolve(tenant_id, client, raw):
        asked.append(raw)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(concierge_reads, "_resolve_contact", resolve)
    return asked


def test_list_bookings_resolves_a_name_to_the_contacts_email(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE)
    stays = []
    monkeypatch.setattr(concierge_data.db, "stays_running_into",
                        lambda *a, **k: stays.append(k) or [])
    asked = fake_resolver(monkeypatch, (TOM, []))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom", "date_from": "2026-10-20"})
    assert asked == ["Tom"]
    assert calls[0]["contact"] == "thale@mail.example" and stays[0]["contact"] == "thale@mail.example"
    assert out["count"] == 1 and out["bookings"][0]["reference"] == "BK-1003"


def test_list_bookings_by_name_uses_the_resolved_email_for_the_cancelled_read_too(monkeypatch):
    calls = fake_bookings(monkeypatch, cancelled=GONE)
    fake_resolver(monkeypatch, (TOM, []))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"})
    assert [c["contact"] for c in calls] == ["thale@mail.example"] * 2
    assert calls[1]["status"] == "cancelled" and len(out["cancelled"]) == 2


def test_list_bookings_asks_which_when_several_contacts_match(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE)
    other = {"id": "c2", "name": "Tom Reed", "email": "treed@mail.example"}
    fake_resolver(monkeypatch, (None, [TOM, other]))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"})
    assert calls == []
    assert out == {"bookings": [], "count": 0, "message": "Several contacts match — ask which one.",
                   "which": [{"name": "Tom Hale", "email": "thale@mail.example", "contact_id": "c1"},
                             {"name": "Tom Reed", "email": "treed@mail.example", "contact_id": "c2"}]}


def test_list_bookings_falls_back_to_the_raw_string_when_nobody_matches(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE)
    fake_resolver(monkeypatch, (None, []))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"})
    assert calls[0]["contact"] == "Tom" and out["count"] == 1
    # a contact row with no email keeps the raw string too
    fake_resolver(monkeypatch, ({"id": "c3", "name": "Tom", "email": None}, []))
    concierge_data._list_bookings("t1", None, {"contact": "Tom"})
    assert calls[-1]["contact"] == "Tom"


def test_list_bookings_survives_a_failed_contact_lookup(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE)
    fake_resolver(monkeypatch, RuntimeError("lookup failed"))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"})
    assert calls[0]["contact"] == "Tom" and out["count"] == 1


def test_list_bookings_never_resolves_an_email_or_an_empty_contact(monkeypatch):
    calls = fake_bookings(monkeypatch, live=LIVE)
    asked = fake_resolver(monkeypatch, AssertionError("must not resolve"))
    concierge_data._list_bookings("t1", None, {"contact": "tom@x.com"})
    concierge_data._list_bookings("t1", None, {"date_from": "2026-10-20"})
    concierge_data._list_bookings("t1", None, {"contact": "   "})
    assert asked == [] and [c.get("contact") for c in calls] == ["tom@x.com", None, None]


def test_list_bookings_strips_filter_characters_before_the_contact_lookup(monkeypatch):
    fake_bookings(monkeypatch, live=LIVE)
    asked = fake_resolver(monkeypatch, (None, []))
    concierge_data._list_bookings("t1", None, {"contact": "Tom (work), %Hale*"})
    assert asked == ["Tom work Hale"]


def a_member(role, pages, does=()):
    from app import people
    return people.member({"user_id": "u1", "role": role, "active": True, "display_name": "Sam",
                          "scopes": {"pages": list(pages), "does": list(does), "can": []}})


OTHER = {"id": "c2", "name": "Tom Reed", "email": "treed@mail.example"}


@pytest.mark.parametrize("member", [None, a_member("owner", []), a_member("manager", []),
                                    a_member("staff", ["today", "diary", "crm"])])
def test_list_bookings_which_list_goes_to_whoever_may_open_the_crm_whole_book(monkeypatch, member):
    calls = fake_bookings(monkeypatch, live=LIVE)
    fake_resolver(monkeypatch, (None, [TOM, OTHER]))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"}, member)
    assert calls == [] and [c["contact_id"] for c in out["which"]] == ["c1", "c2"]


@pytest.mark.parametrize("member", [
    a_member("staff", ["today", "diary"]),                              # the CRM door is shut
    a_member("staff", ["today", "diary", "crm"], does=["desk"]),        # open, but only their own customers
])
def test_list_bookings_which_list_never_shows_the_crm_to_someone_it_is_shut_to(monkeypatch, member):
    calls = fake_bookings(monkeypatch, live=LIVE)
    fake_resolver(monkeypatch, (None, [TOM, OTHER]))
    out = concierge_data._list_bookings("t1", None, {"contact": "Tom"}, member)
    # no names or addresses from the CRM: the plain substring match on the raw string runs instead
    assert "which" not in out and "message" not in out
    assert calls[0]["contact"] == "Tom" and out["count"] == 1
    assert "treed@mail.example" not in str(out) and "thale@mail.example" not in str(out)

from tests.booking_fixtures import carried_config
