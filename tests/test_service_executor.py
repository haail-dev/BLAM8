"""The office agent's SERVICE path (scheduling.make_executor(service_types=...)) — the tool
executor a tenant with per-service durations gets: check_availability + hold_booking where
the booked length is the service's duration. DB is mocked; the math is slotgrid/services
(tested separately in tests/test_services.py)."""
from tests.booking_fixtures import carried_config

from datetime import datetime

from app import scheduling, slotgrid
from app.scheduling import DIARY_TZ

CFG = carried_config({"windows": "slots", "open_time": "09:00", "close_time": "17:00", "slot_minutes": 30,
       "capacity_per_window": 1, "min_notice_hours": 0,
       "working_days": "mon,tue,wed,thu,fri,sat,sun"})
SERVICES = [{"name": "Cut", "minutes": 60, "price": 25.0},
            {"name": "Massage", "minutes": 90, "price": None}]
NOW = datetime(2026, 6, 16, 8, 0, tzinfo=DIARY_TZ)  # Tuesday, before opening


def _row(day, start_block, length_blocks, status="confirmed"):
    return {"booking_date": day, "start_block": start_block, "length_blocks": length_blocks,
            "status": status, "spaces": None, "engineer_id": None}


def _exec(monkeypatch, existing, inserted, intake_fields=None):
    monkeypatch.setattr(scheduling.db, "crm_list_field_definitions", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing)
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": "svc-1", "booking_ref": "BK-7001", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "window": window,
               "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="sarah@x.co", service_types=SERVICES,
                                    intake_fields=intake_fields, now=NOW)


def test_check_returns_slots_with_duration_and_price(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-17", "service": "Cut"})
    assert out["service"] == "Cut" and out["duration_minutes"] == 60 and out["price"] == 25.0
    windows = [s["window"] for s in out["available"]]
    assert "09:00" in windows and windows[-1] == "16:00"   # last 60-min start before 17:00


def test_check_unknown_service(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-17", "service": "perm"})
    assert out["error"] == "unknown_service" and "Cut" in out["services"]


def test_check_multi_day_carries_day_summary(monkeypatch):
    # Period ask (2026-08-02): a multi-day range also returns per-day counts of
    # free start times, derived from the same slots list.
    from collections import Counter
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-18", "service": "Cut"})
    counts = {d["date"]: d["free"] for d in out["day_summary"]}
    assert counts == dict(Counter(s["date"] for s in out["available"]))
    assert out["day_summary"][0]["day"] == "Wednesday 17 June"
    # A single-day check stays summary-free.
    one = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-17", "service": "Cut"})
    assert "day_summary" not in one


def test_check_without_service_uses_default_slot(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability", {"date_from": "2026-06-17", "date_to": "2026-06-17"})
    assert "service" not in out and out["available"]       # general availability, 30-min slots
    assert [s["window"] for s in out["available"]][-1] == "16:30"


def test_hold_books_service_length(monkeypatch):
    inserted = []
    execute, state = _exec(monkeypatch, existing=[], inserted=inserted)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00",
                                   "service": "Massage", "summary": "Massage — Sarah"})
    assert out["held"] is True and out["booking_ref"] == "BK-7001"
    assert out["service"] == "Massage" and out["duration_minutes"] == 90
    # 90 min = 6 blocks, starting at 10:00 (start_block 40), window carries the time.
    assert inserted[0]["length_blocks"] == 6 and inserted[0]["start_block"] == 40
    assert inserted[0]["window"] == "10:00"
    assert inserted[0]["details"]["Service"] == "Massage"
    assert state["bookings"] == inserted


def test_hold_stamps_price_quote(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, existing=[], inserted=inserted)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "09:00",
                                   "service": "Cut", "summary": "Cut"})
    assert out["price"] == 25.0


def test_hold_unknown_service(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    out = execute("hold_booking", {"date": "2026-06-17", "window": "09:00",
                                   "service": "perm", "summary": "x"})
    assert out["error"] == "unknown_service"


def test_hold_rejects_slot_outside_opening_hours(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, existing=[], inserted=inserted)
    # 16:30 + 60 min would run to 17:30, past close → not offered.
    out = execute("hold_booking", {"date": "2026-06-17", "window": "16:30",
                                   "service": "Cut", "summary": "Cut"})
    assert out["error"] == "invalid_window" and inserted == []


def test_hold_full_when_capacity_taken(monkeypatch):
    # 10:00-11:00 already booked, capacity 1 → a 60-min Cut at 10:00 can't be held.
    existing = [_row("2026-06-17", 40, 4)]
    inserted = []
    execute, _ = _exec(monkeypatch, existing=existing, inserted=inserted)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00",
                                   "service": "Cut", "summary": "Cut"})
    assert out["error"] == "full" and inserted == []


def test_hold_gated_on_required_intake(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[],
                       intake_fields=[{"label": "Pet name", "required": True, "hint": ""}])
    out = execute("hold_booking", {"date": "2026-06-17", "window": "09:00", "service": "Cut",
                                   "summary": "Cut", "details": {}})
    assert out["error"] == "missing_info" and "Pet name" in out["missing"]


def test_one_hold_per_email(monkeypatch):
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])
    execute("hold_booking", {"date": "2026-06-17", "window": "09:00", "service": "Cut", "summary": "A"})
    second = execute("hold_booking", {"date": "2026-06-17", "window": "11:00",
                                      "service": "Cut", "summary": "B"})
    assert second["error"] == "already_held"


# ── arrival-window services (Kris 2026-09-03) ───────────────────────────────
ARRIVAL = [{"name": "Site survey", "minutes": 60, "timing": "half"},
           {"name": "Installation day", "minutes": 480, "timing": "day"},
           {"name": "Consultation", "minutes": 45}]


def _exec_arrival(monkeypatch, existing, inserted):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing)
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": f"svc-{len(inserted) + 1}", "booking_ref": f"BK-{len(inserted) + 1}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "window": window, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="kris@x.co", service_types=ARRIVAL, now=NOW)


def test_arrival_window_check_offers_halves_and_coaches(monkeypatch):
    execute, _ = _exec_arrival(monkeypatch, existing=[], inserted=[])
    out = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-17", "service": "Site survey"})
    assert [s["window"] for s in out["available"]] == ["am", "pm"]
    assert out["arrival_window"] is True and "never a clock time" in out["message_timing"]
    # an exact-time service on the same tenant is untouched
    out2 = execute("check_availability",
                   {"date_from": "2026-06-17", "date_to": "2026-06-17", "service": "Consultation"})
    assert out2["available"][0]["window"] == "09:00" and "arrival_window" not in out2


def test_arrival_window_hold_lands_at_the_earliest_fit_and_keeps_the_half(monkeypatch):
    inserted = []
    execute, state = _exec_arrival(monkeypatch, existing=[], inserted=inserted)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "pm", "service": "Site survey",
                                   "summary": "Survey — Kris"})
    assert out["held"] is True and out["window"] == "pm" and out["arrival_window"] is True
    # The hold hands the drafter a READY SENTENCE, not ingredients (2026-09-04:
    # given "in the afternoon" the model wrote "a this afternoon callout", twice).
    # NOW is the 16th, the hold is the 17th — so it says tomorrow, not a date.
    assert 'we\'ll be with you tomorrow afternoon' in out["message"]
    assert "do not quote a clock time" in out["message"]
    assert inserted[0]["window"] == "pm" and inserted[0]["start_block"] == 48     # 12:00, 4 blocks
    assert inserted[0]["length_blocks"] == 4
    # a clock time passed for an arrival-window service maps to its half
    execute2, _ = _exec_arrival(monkeypatch, existing=[], inserted=[])
    out2 = execute2("hold_booking", {"date": "2026-06-17", "window": "10:00",
                                     "service": "Site survey", "summary": "x"})
    assert out2["window"] == "am"
    # nonsense → the timing-specific hint, nothing placed
    execute3, _ = _exec_arrival(monkeypatch, existing=[], inserted=[])
    bad = execute3("hold_booking", {"date": "2026-06-17", "window": "whenever",
                                    "service": "Site survey", "summary": "x"})
    assert bad["error"] == "invalid_window" and "'am' (morning) or 'pm'" in bad["message"]
    # whole-day service: window resolves to 'day' regardless of what was passed
    ins4 = []
    execute4, _ = _exec_arrival(monkeypatch, existing=[], inserted=ins4)
    out4 = execute4("hold_booking", {"date": "2026-06-17", "window": "09:00",
                                     "service": "Installation day", "summary": "Fit"})
    assert out4["held"] is True and out4["window"] == "day" and ins4[0]["start_block"] == 36


def test_arrival_window_hold_refuses_a_full_half(monkeypatch):
    morning = [_row("2026-06-17", 36, 12)]          # 09:00–12:00 solid
    execute, _ = _exec_arrival(monkeypatch, existing=morning, inserted=[])
    out = execute("hold_booking", {"date": "2026-06-17", "window": "am",
                                   "service": "Site survey", "summary": "x"})
    assert out["error"] == "full"


def test_arrival_window_offer_reserves_the_half(monkeypatch):
    inserted = []
    execute, _ = _exec_arrival(monkeypatch, existing=[], inserted=inserted)
    out = execute("offer_slots", {"service": "Site survey", "summary": "Survey",
                                  "slots": [{"date": "2026-06-17", "window": "am"},
                                            {"date": "2026-06-18", "window": "pm"}]})
    assert [o["window"] for o in out["offered"]] == ["am", "pm"]
    assert out["arrival_window"] is True
    assert [r["window"] for r in inserted] == ["am", "pm"] and inserted[1]["start_block"] == 48
    # Each offered slot arrives as customer words — tomorrow by name, the day
    # after by date — so the reply never has to invent the phrasing.
    assert '"tomorrow morning"' in out["message_timing"]
    assert '"on Thursday 18th June, in the afternoon"' in out["message_timing"]


def test_arrival_words_say_near_dates_by_name(monkeypatch):
    from datetime import date
    today = date(2026, 6, 16)
    assert scheduling.arrival_words(date(2026, 6, 16), "pm", today) == "this afternoon"
    assert scheduling.arrival_words(date(2026, 6, 17), "am", today) == "tomorrow morning"
    assert scheduling.arrival_words(date(2026, 6, 16), "day", today) == "today"
    assert scheduling.arrival_words(date(2026, 6, 22), "pm", today) == \
        "on Monday 22nd June, in the afternoon"
    # A ready sentence needs a window; without one there is nothing to say.
    assert scheduling.arrival_sentence(date(2026, 6, 22), "pm", today) == \
        "we'll be with you on Monday 22nd June, in the afternoon"
    assert scheduling.arrival_sentence(date(2026, 6, 22), "", today) == ""


def test_manual_service_add_places_by_timing(monkeypatch):
    """The diary's Add form on a services tenant: an exact-time service lands at its
    start; an arrival-window one at the earliest fit in its half; rules a human may
    override (notice, working days) are skipped, capacity is not."""
    inserted = []
    cfg = carried_config({**CFG, "service_types": ARRIVAL, "min_notice_hours": 240})
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings",
                        lambda *a, **k: [_row("2026-06-17", 36, 12)])   # morning solid

    def fake_insert(tenant_id, date_iso, sb, length, summary, **kw):
        row = {"id": "m-1", "booking_date": date_iso, "start_block": sb,
               "length_blocks": length, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    from datetime import date
    row, err = scheduling.manual_service_add("t1", date(2026, 6, 17), "Site survey", "pm",
                                             "Survey — Kris", details={"Full name": "Kris"},
                                             contact="kris@x.co")
    assert err is None and row["start_block"] == 48 and row["window"] == "pm"
    assert row["status"] == "confirmed" and row["source"] == "manual"
    assert row["details"] == {"Full name": "Kris", "Service": "Site survey"}
    assert scheduling.manual_service_add("t1", date(2026, 6, 17), "Site survey", "am", "x")[1] == "full"
    assert scheduling.manual_service_add("t1", date(2026, 6, 17), "Nope", "am", "x")[1] == "unknown_service"
    assert scheduling.manual_service_add("t1", date(2026, 6, 17), "Consultation", "am", "x")[1] == "bad_window"
    row2, err2 = scheduling.manual_service_add("t1", date(2026, 6, 17), "Consultation", "14:00", "x")
    assert err2 is None and row2["start_block"] == 56 and row2["window"] == "14:00"


def test_gate_reads_the_bookings_service_for_scoped_fields(monkeypatch):
    """The permit (scoped to Installation day) no longer gates a consultation;
    an installation without a permit FILE is still refused."""
    fields = [{"label": "Full name", "required": True, "hint": ""},
              {"label": "Cemetery permit or church faculty", "required": True, "hint": "",
               "doc": True, "services": ["Installation day"]}]
    svcs = [{"name": "Design consultation", "minutes": 45},
            {"name": "Installation day", "minutes": 480, "timing": "day"}]
    inserted = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": f"s-{len(inserted)+1}", "booking_ref": "BK-1", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "window": window, **kw}
        inserted.append(row); return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)

    def run(service, details):
        ex, _ = scheduling.make_executor("t1", contact="kris@x.co", service_types=svcs,
                                         intake_fields=fields, now=NOW)
        return ex("hold_booking", {"date": "2026-06-17", "window": "10:00", "service": service,
                                   "summary": "x", "details": details})
    assert run("Design consultation", {"Full name": "Kris"})["held"] is True
    out = run("Installation day", {"Full name": "Kris"})
    assert out["error"] == "missing_info" and out["missing"] == ["Cemetery permit or church faculty"]
    out2 = run("Installation day", {"Full name": "Kris",
                                    "Cemetery permit or church faculty": "not yet — we will apply"})
    assert out2["error"] == "missing_info"
    assert run("Installation day", {"Full name": "Kris",
                                    "Cemetery permit or church faculty": "faculty.pdf"})["held"] is True


def test_a_range_beyond_the_horizon_says_so_not_fully_booked(monkeypatch):
    """Run #8: an empty Monday 100 days out came back exactly like a full day
    and the agent told the customer "fully booked". The tool now names the
    horizon and when the diary opens for that date; a range whose tail runs
    past the horizon carries a note."""
    execute, _ = _exec(monkeypatch, existing=[], inserted=[])          # CFG: default 28-day horizon
    out = execute("check_availability", {"date_from": "2026-09-14", "date_to": "2026-09-14", "service": "Cut"})
    assert out["available"] == [] and out["beyond_horizon"] is True and out["horizon_days"] == 28
    assert out["opens_on"] == "2026-08-17" and "NOT 'fully booked'" in out["message"]
    assert "do not offer the waiting list" in out["message"].lower()
    tail = execute("check_availability", {"date_from": "2026-06-17", "date_to": "2026-09-14", "service": "Cut"})
    assert tail["available"] and "beyond_horizon" not in tail and "not open yet" in tail["horizon_note"]
    inside = execute("check_availability", {"date_from": "2026-06-17", "date_to": "2026-06-18", "service": "Cut"})
    assert "beyond_horizon" not in inside and "horizon_note" not in inside
