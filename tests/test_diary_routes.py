"""Diary manual-add route — window validation against the tenant's live mode.

Regression pinned: the old handler clamped any window outside ('day','am','pm')
to 'day', so a slots-mode tenant manually adding a '14:00' booking silently got
a WHOLE-DAY block — which (by the cross-mode overlap rule) blocked every slot
that date. The fix validates the posted window against the same set the form
renders (scheduling._norm()['windows']) and rejects anything else with
msg=badwindow instead of booking something the tenant didn't ask for.

Route-level via TestClient: auth + CSRF dependencies overridden, db monkeypatched
— in-process only, no network. The TestClient is used WITHOUT a context manager
on purpose: startup events (secret assert, pollers) never run.

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


class Env:
    def __init__(self, monkeypatch, cfg=None, modules=("scheduling",)):
        self.holds = []
        self.fleet_holds = []

        def hold(tenant_id, date_iso, window, summary, **kw):
            self.holds.append({"date": date_iso, "window": window, "summary": summary, **kw})
            return {"id": "b-1"}

        def fleet_hold(tenant_id, date_iso, summary, spaces, **kw):
            self.fleet_holds.append({"date": date_iso, "summary": summary, "spaces": spaces, **kw})
            return {"id": "b-2"}

        async def push(*a, **k):
            return None

        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
        monkeypatch.setattr(dashboard.db, "hold_booking_if_capacity", hold)
        monkeypatch.setattr(dashboard.db, "hold_fleet_booking", fleet_hold)
        monkeypatch.setattr(dashboard.db, "list_pallet_types", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", push)


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def add(client, **form):
    return client.post("/app/diary/add",
                       data={"booking_date": "2026-06-18", "summary": "job", **form},
                       follow_redirects=False)


SLOTS = carried_config({"windows": "slots", "open_time": "09:00", "close_time": "17:00", "slot_minutes": 60})


def test_slots_mode_time_window_books_that_slot(client, monkeypatch):
    env = Env(monkeypatch, cfg=SLOTS)
    r = add(client, window="14:00")
    assert r.status_code == 303 and "msg=added" in r.headers["location"]
    assert env.holds[0]["window"] == "14:00"   # THE regression: this used to become 'day'


def test_slots_mode_rejects_a_whole_day_window(client, monkeypatch):
    env = Env(monkeypatch, cfg=SLOTS)
    r = add(client, window="day")
    assert "msg=badwindow" in r.headers["location"]
    assert env.holds == []                     # nothing booked — never a silent day-block


def test_day_and_ampm_modes_unchanged(client, monkeypatch):
    env = Env(monkeypatch, cfg=None)           # no config row = default 'day' mode
    add(client, window="day")
    assert env.holds[0]["window"] == "day"

    env = Env(monkeypatch, cfg={"windows": "ampm"})
    add(client, window="pm")
    assert env.holds[-1]["window"] == "pm"
    r = add(client, window="evening")
    assert "msg=badwindow" in r.headers["location"]


def test_fleet_mode_add_ignores_windows_entirely(client, monkeypatch):
    env = Env(monkeypatch, cfg=None, modules=("tms",))
    r = add(client, qty="3", pallet_type="full")
    assert "msg=added" in r.headers["location"]
    assert env.fleet_holds[0]["spaces"] == 3.0 and env.holds == []


def test_stay_tenant_add_books_the_posted_span(client, monkeypatch):
    """Manual add on a STAY diary (Julius 2026-09-03: the form offered only
    'Whole day' against an arrival→departure diary): the route routes the
    posted span through scheduling.manual_stay_add — never the windows hold —
    and a bad span bounces as baddate."""
    stay_cfg = {"intake_fields": [
        {"label": "Departure / collection date & time", "required": True, "hint": ""}]}
    env = Env(monkeypatch, cfg=stay_cfg)
    calls = []

    def fake_manual(tid, arr, dep, summary, details=None, contact=None, client=None):
        calls.append({"arr": arr.isoformat(), "dep": dep.isoformat(),
                      "summary": summary, "details": details})
        return {"id": "b-9", "booking_date": arr.date().isoformat()}, None
    monkeypatch.setattr(dashboard.scheduling, "manual_stay_add", fake_manual)
    monkeypatch.setattr(dashboard.crm, "note_booking_activity", lambda *a, **k: None)
    r = add(client, arrival="2026-09-09T13:00", departure="2026-09-09T17:00",
            summary="Meet & greet",
            req_label=["Pet name(s) & breed"], req_value=["Bella and Coco"])
    assert "msg=added" in r.headers["location"]
    assert calls[0]["arr"] == "2026-09-09T13:00:00"
    assert calls[0]["dep"] == "2026-09-09T17:00:00"
    assert calls[0]["details"] == {"Pet name(s) & breed": "Bella and Coco"}
    assert env.holds == []                    # the windows path never ran
    # unparseable span → baddate, nothing booked
    r2 = add(client, arrival="not-a-date", departure="2026-09-09T17:00", summary="x")
    assert "msg=baddate" in r2.headers["location"]
    assert len(calls) == 1


def test_requirements_fill_lands_in_details_and_blanks_drop(client, monkeypatch):
    # 2026-07-04: the manual add form carries the tenant's booking requirements
    # (expandable) — filled values land in bookings.details keyed by label, the
    # same shape the agent's gather flow writes; blanks are never stored and the
    # human is never forced through the intake gate (empty details → None).
    env = Env(monkeypatch, cfg=None)
    add(client, window="day",
        req_label=["Customer name", "Job address", "Access notes"],
        req_value=["Dave", "12 High St", ""])
    assert env.holds[0]["details"] == {"Customer name": "Dave", "Job address": "12 High St"}

    add(client, window="day", req_label=["Customer name"], req_value=[""])
    assert env.holds[-1]["details"] is None


# ── manual status changes tell the Agent Log — but only the agent's ──────────
# (Kris 2026-08-31, twice). First: the release e2e showed a by-hand release of
# an AGENT-held slot leaving no trace — the feed read "Held BK-1042" and then
# nothing. Then: a booking added by hand and removed by hand logged too, and
# Kris asked why a purely human action would sit in the AGENT Log. The rule
# that satisfies both: a manual decision logs only when the booking is tied to
# an email thread (email_log_id) — it closes a story the agent started; pure
# diary housekeeping stays silent. Wording still says what the row WAS
# (releasing an offered slot ≠ cancelling a confirmed one).
def _status_env(monkeypatch, booking):
    events = []
    Env(monkeypatch)  # modules + calendar push
    monkeypatch.setattr(dashboard.db, "get_booking", lambda *a, **k: booking)
    monkeypatch.setattr(dashboard.db, "set_booking_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "log_agent_event",
                        lambda tid, kind, summary, **k:
                        events.append({"kind": kind, "summary": summary, **k}))
    monkeypatch.setattr(dashboard.crm, "note_booking_activity", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(dashboard.calendar_sync, "remove_pushed_event", _noop)
    return events


BOOKING = {"id": "b-9", "booking_ref": "BK-1042", "booking_date": "2026-09-09",
           "window": "09:00", "status": "offered", "contact": "kris@x.co",
           "email_log_id": "log-1", "source": "email"}


def test_releasing_an_offered_slot_logs_released(client, monkeypatch):
    events = _status_env(monkeypatch, dict(BOOKING))
    r = client.post("/app/diary/b-9/status", data={"status": "cancelled"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "booking_manual"
    # The date READS as a date in the summary; event_date stays ISO for filtering.
    assert e["summary"] == "Released BK-1042 (9th Sep 26 — 09:00) by hand in the diary"
    assert e["contact"] == "kris@x.co" and e["booking_ref"] == "BK-1042"
    assert e["event_date"] == "2026-09-09"


def test_cancelling_a_confirmed_booking_logs_cancelled(client, monkeypatch):
    events = _status_env(monkeypatch, dict(BOOKING, status="confirmed"))
    client.post("/app/diary/b-9/status", data={"status": "cancelled"},
                follow_redirects=False)
    assert events[0]["summary"].startswith("Cancelled BK-1042")


def test_confirming_a_hold_logs_confirmed(client, monkeypatch):
    events = _status_env(monkeypatch, dict(BOOKING, status="proposed"))
    client.post("/app/diary/b-9/status", data={"status": "confirmed"},
                follow_redirects=False)
    assert events[0]["summary"].startswith("Confirmed BK-1042")


def test_a_vanished_booking_stays_silent_but_the_route_survives(client, monkeypatch):
    events = _status_env(monkeypatch, None)
    r = client.post("/app/diary/b-9/status", data={"status": "cancelled"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert events == []


def test_manual_housekeeping_never_reaches_the_agent_log(client, monkeypatch):
    # THE case Kris watched: added by hand, removed by hand — the agent did
    # nothing, so the Agent Log says nothing. No email_log_id = not its story.
    events = _status_env(monkeypatch,
                         dict(BOOKING, status="confirmed",
                              email_log_id=None, source="manual"))
    r = client.post("/app/diary/b-9/status", data={"status": "cancelled"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert events == []


def test_services_tenant_add_routes_through_manual_service_add(client, monkeypatch):
    """Manual add on a SERVICES diary (Kris 2026-09-03: the form offered only
    'Whole day' against a per-service diary): the posted service + window go
    through scheduling.manual_service_add — never the windows hold."""
    svc_cfg = {"windows": "slots", "service_types": [
        {"name": "Site survey", "minutes": 60, "timing": "half"}]}
    env = Env(monkeypatch, cfg=svc_cfg)
    calls = []

    def fake_manual(tid, day, service, window, summary, details=None, contact=None, client=None):
        calls.append({"day": day.isoformat(), "service": service, "window": window,
                      "summary": summary, "details": details})
        return {"id": "b-7", "booking_date": day.isoformat()}, None
    monkeypatch.setattr(dashboard.scheduling, "manual_service_add", fake_manual)
    monkeypatch.setattr(dashboard.crm, "note_booking_activity", lambda *a, **k: None)
    r = add(client, service="Site survey", window="am", summary="Survey",
            req_label=["Full name"], req_value=["Kris"])
    assert "msg=added" in r.headers["location"]
    assert calls == [{"day": "2026-06-18", "service": "Site survey", "window": "am",
                      "summary": "Survey", "details": {"Full name": "Kris"}}]
    assert env.holds == []
    # a window the service can't take bounces as badwindow
    monkeypatch.setattr(dashboard.scheduling, "manual_service_add",
                        lambda *a, **k: (None, "bad_window"))
    r2 = add(client, service="Site survey", window="whenever", summary="x")
    assert "msg=badwindow" in r2.headers["location"]


def test_window_label_filter_speaks_halves():
    from app.templating import window_label
    assert window_label("pm") == "Afternoon" and window_label("am") == "Morning"
    assert window_label("day") == "Whole day" and window_label("14:00") == "14:00"
    assert window_label(None) == ""


def test_intake_editor_saves_the_service_scope(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.setup_forms, "save",
                        lambda ctx,page,expected,ops: saved.update(fields=ops[-1]['fields']['intake_fields']))
    r = client.post("/app/diary/intake", follow_redirects=False,
                    data={"settings_snapshot": __import__("json").dumps({"page":"booking_details","config":{},"definitions":[]}), "label": ["Full name", "Permit"], "required": ["1", "1"],
                          "hint": ["", "the granted document"], "doc": ["0", "1"],
                          "services": ["", "Installation day|Site survey"]})
    assert r.status_code == 303
    assert "services" not in saved["fields"][0]
    assert saved["fields"][1]["services"] == ["Installation day", "Site survey"]
    assert saved["fields"][1]["doc"] is True


def test_a_stay_series_on_named_weekdays_books_one_run_per_day_and_clears_the_promise(client, monkeypatch):
    """Kris 2026-09-21: Mabel's regular days, added by hand from the promise's
    door — Mon/Wed/Thu, weekly, three times each, and the promise ticks off
    against the first booking."""
    stay_cfg = {"intake_fields": [{"label": "Departure / collection date & time", "required": True, "hint": ""}],
                "recurring": {"enabled": True, "frequencies": ["weekly"], "max_occurrences": 12}}
    Env(monkeypatch, cfg=stay_cfg)
    calls, cleared = [], []

    def fake_manual(tid, arr, dep, summary, details=None, contact=None, client=None, recurrence_id=None):
        calls.append((arr.isoformat(), dep.isoformat(), recurrence_id))
        return {"id": f"b-{len(calls)}", "booking_date": arr.date().isoformat(), "booking_ref": f"BK-{1000 + len(calls)}"}, None
    monkeypatch.setattr(dashboard.scheduling, "manual_stay_add", fake_manual)
    monkeypatch.setattr(dashboard.crm, "note_booking_activity", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "crm_resolve_promise_auto", lambda pid, tid, **k: cleared.append((pid, k)))
    r = add(client, arrival="2026-09-28T07:45", departure="2026-09-28T17:30", summary="Daycare for Mabel",
            contact="d@x.co", repeat="weekly", occurrences="3", weekdays=["mon", "wed", "thu"], promise_id="p1")
    assert "msg=added" in r.headers["location"] and "date=2026-09-28" in r.headers["location"]
    days = [c[0][:10] for c in calls]
    assert days == ["2026-09-28", "2026-10-05", "2026-10-12",          # Mondays
                    "2026-09-30", "2026-10-07", "2026-10-14",          # Wednesdays
                    "2026-10-01", "2026-10-08", "2026-10-15"]          # Thursdays
    assert all(c[1].endswith("T17:30:00") for c in calls)             # the same span each day
    assert len({c[2] for c in calls}) == 3                             # one series (reference) per weekday
    assert cleared and cleared[0][0] == "p1" and cleared[0][1]["by_label"] == "BK-1001"


LOCKIN_CFG = carried_config({"intake_fields": [{"label": "Departure / collection date & time", "required": True, "hint": ""}],
              "recurring": {"enabled": True, "frequencies": ["weekly"], "max_occurrences": 12},
              "day_hours": {"checkin_after": "07:30", "checkout_before": "18:00"},
              "working_days": "mon,tue,wed,thu,fri,sat,sun", "horizon_days": 20, "blackout_dates": ["2026-10-01"],
              "capacity_per_window": 4, "intake_configured_at": "2026-08-01T00:00:00+00:00"})


def _lockin_env(monkeypatch, cfg=LOCKIN_CFG):
    from tests.test_diary_details_fold import _env
    from tests.test_diary_day_chart import _FrozenDatetime
    _env(monkeypatch, [])
    monkeypatch.setattr(dashboard, "datetime", _FrozenDatetime)   # today = 1 Sep 2026 → horizon 20 ends 21 Sep
    for mod in (dashboard.db, dashboard.scheduling.db):
        monkeypatch.setattr(mod, "get_scheduling_config", lambda *a, **k: carried_config(dict(cfg)))
    monkeypatch.setattr(dashboard.scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [
        {"id": "g1", "label": "Dog", "label_plural": "Dogs", "fields": [{"key": "name", "label": "Name", "type": "text"}]}])
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})


def test_a_standing_series_lands_as_dates_to_confirm(client, monkeypatch):
    """Kris 2026-09-21: "once we land in the Diary it shows an area with a list
    of days/dates to confirm as booked, for as long as the settings allow
    prebookings" — every Mon/Wed/Thu to the horizon, the closed one greyed."""
    _lockin_env(monkeypatch)
    html = client.get("/app/diary?date=2026-09-02&add=1&contact=d%40x.co&summary=Daycare+for+Mabel&subjects=Mabel"
                      "&repeat=weekly&occurrences=12&weekdays=mon%2Cwed%2Cthu&promise=p1").text
    assert 'id="lockin"' in html and 'name="promise_id" value="p1"' in html
    assert 'name="summary" value="Daycare for Mabel"' in html and 'name="subjects_text" value="Mabel"' in html
    assert 'name="series_from" type="time" value="07:30"' in html and 'name="series_to" type="time" value="18:00"' in html
    assert "Mon, Wed &amp; Thu every week" in html and "as far ahead as the diary takes bookings" in html
    # Wed 2 Sep is the first; the horizon is 20 days from the frozen clock (21 Sep).
    assert 'name="dates" value="2026-09-02" class="accent-emerald-600" checked' in html
    assert 'name="dates" value="2026-09-21" class="accent-emerald-600" checked' in html
    assert 'value="2026-09-23"' not in html                                          # past the horizon
    assert "September 2026" in html and 'data-series-day="2026-09-03"' in html       # the Thursday
    assert "Book these <span data-series-count>9</span> days" in html               # 3 a week × 3 weeks
    # The by-hand form is there, prefilled, but folded until "Adjust by hand".
    assert 'id="addform" class="hidden bg-white' in html and "data-prefilled" in html
    assert 'data-series-adjust' in html


def test_a_closed_day_in_the_series_is_listed_but_not_ticked(client, monkeypatch):
    cfg = dict(LOCKIN_CFG, blackout_dates=["2026-09-09"])
    _lockin_env(monkeypatch, cfg)
    html = client.get("/app/diary?date=2026-09-02&add=1&repeat=weekly&weekdays=wed&summary=Daycare").text
    assert 'data-series-day="2026-09-09" data-closed' in html
    assert 'name="dates" value="2026-09-09" class="accent-emerald-600" disabled' in html and "· closed" in html
    assert "Book these <span data-series-count>2</span> days" in html and "1 closed." in html


def test_a_one_off_promise_opens_the_form_itself(client, monkeypatch):
    _lockin_env(monkeypatch)
    html = client.get("/app/diary?date=2026-09-28&add=1&contact=d%40x.co&summary=Boarding+for+Mabel&subjects=Mabel&promise=p1").text
    assert 'id="lockin"' not in html
    assert 'id="addform" class="bg-white' in html and "data-prefilled" in html       # open, not hidden
    assert 'name="promise_id" value="p1"' in html
    assert 'value="2026-09-28T07:30"' in html and 'value="2026-09-28T18:00"' in html
    assert 'value="Boarding for Mabel"' in html and 'value="d@x.co"' in html and 'value="Mabel"' in html


def test_the_ticked_dates_book_at_the_given_times_and_clear_the_promise(client, monkeypatch):
    """The landing panel's submit: an explicit list, one series per weekday,
    at the drop-off / collection typed on the panel."""
    Env(monkeypatch, cfg=LOCKIN_CFG)
    calls, cleared = [], []

    def fake_manual(tid, arr, dep, summary, details=None, contact=None, client=None, recurrence_id=None):
        calls.append((arr.isoformat(), dep.isoformat(), recurrence_id))
        return {"id": f"b-{len(calls)}", "booking_date": arr.date().isoformat(), "booking_ref": f"BK-{1000 + len(calls)}"}, None
    monkeypatch.setattr(dashboard.scheduling, "manual_stay_add", fake_manual)
    monkeypatch.setattr(dashboard.crm, "note_booking_activity", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "crm_resolve_promise_auto", lambda pid, tid, **k: cleared.append((pid, k)))
    r = add(client, booking_date="", summary="Daycare for Mabel", contact="d@x.co", subjects_text="Mabel",
            dates=["2026-09-28", "2026-09-30", "2026-10-05"], series_from="07:45", series_to="17:30", promise_id="p1")
    assert "msg=added" in r.headers["location"] and "date=2026-09-28" in r.headers["location"]
    assert [c[0] for c in calls] == ["2026-09-28T07:45:00", "2026-10-05T07:45:00", "2026-09-30T07:45:00"]   # Mondays, then the Wednesday
    assert all(c[1].endswith("T17:30:00") for c in calls)
    assert calls[0][2] == calls[1][2] and calls[0][2] and calls[2][2] is None       # a series id for the pair only
    assert cleared and cleared[0][0] == "p1" and cleared[0][1]["by_label"] == "BK-1001"


def test_series_to_horizon_lists_every_occurrence_and_flags_the_closed_ones():
    from datetime import date as _date
    from app import scheduling
    items = scheduling.series_to_horizon(LOCKIN_CFG, _date(2026, 9, 23), "weekly", ["mon", "wed", "thu"], today=_date(2026, 9, 21))
    assert [(i["date"].isoformat(), i["closed"]) for i in items] == [
        ("2026-09-23", False), ("2026-09-24", False), ("2026-09-28", False), ("2026-09-30", False),
        ("2026-10-01", True), ("2026-10-05", False), ("2026-10-07", False), ("2026-10-08", False)]   # to 11 Oct
    assert scheduling.series_to_horizon(LOCKIN_CFG, _date(2026, 9, 23), "", ["mon"]) == []
    assert [i["date"].day for i in scheduling.series_to_horizon(LOCKIN_CFG, _date(2026, 9, 23), "weekly", [], today=_date(2026, 9, 21))] == [23, 30, 7]
