"""A service's own booking rules — hours, days, max a day (docs/plan-diary-multiview.md
stage 1, Kris 2026-09-23: "only take meet & greets between 1pm-4pm", on set days, up to
N a day). For ANY service: a kennels' meet & greet, a garage's MOT.

The setting chooses what the agent is told (docs/plan-when-its-full.md): the rules are
named in its diary line only when set, and the availability tools only ever return times
inside them. The hold refusing is the logged backstop.

Also the pool bug found on the way: list_active_bookings never read resource_type or
quantity, so an appointment's "at once" counted nothing.

Run: python -m pytest tests/test_service_rules.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

from app import db, scheduling, services, slotgrid
from app.intake import STAY_FIELD

MEET = {"name": "Meet & greet", "minutes": 30}
RULED = {**MEET, "hours_from": "13:00", "hours_until": "16:00", "days": "tue,thu", "max_per_day": "4"}
TUE, WED = date(2026, 11, 3), date(2026, 11, 4)
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=scheduling.DIARY_TZ)
CFG = carried_config({"working_days": "mon,tue,wed,thu,fri,sat,sun", "windows": "day", "slot_minutes": 60,
       "capacity_per_window": 6, "horizon_days": 60, "blackout_dates": "",
       "open_time": "07:00", "close_time": "18:45"})


# ── the rules as stored ──────────────────────────────────────────────────────
def test_rules_are_stored_only_when_set():
    assert services.clean_service_types([MEET]) == [{"name": "Meet & greet", "minutes": 30, "price": None,
                                                     "resource": None, "buffer": None}]
    svc = services.clean_service_types([RULED])[0]
    assert (svc["hours_from"], svc["hours_until"], svc["days"], svc["max_per_day"]) == ("13:00", "16:00", [1, 3], 4)


def test_nonsense_rules_fall_away():
    svc = services.clean_service_types([{**MEET, "hours_from": "16:00", "hours_until": "13:00",
                                         "days": "mon,tue,wed,thu,fri,sat,sun", "max_per_day": "0"}])[0]
    assert not {"hours_from", "hours_until", "days", "max_per_day"} & set(svc)      # backwards/all/zero = none
    assert services.clean_service_types([{**MEET, "hours_from": "1300", "days": ["2", 4]}])[0]["days"] == [2, 4]
    assert services.clean_service_types([{**MEET, "hours_from": "1300"}])[0]["hours_from"] == "13:00"


def test_the_label_names_only_what_is_set():
    assert services.rules_label(services.clean_service_types([RULED])[0]) == "Tue and Thu, 13:00–16:00, up to 4 a day"
    assert services.rules_label({"hours_from": "13:00"}) == "from 13:00"
    assert services.rules_label(MEET) == ""


def test_the_form_carries_the_rules_positionally():
    got = services.parse_form(["Meet & greet", "Assessment"], ["30", "60"], ["", ""],
                              from_list=["13:00", ""], until_list=["16:00", ""],
                              days_list=["1,3", ""], max_list=["4", ""])
    assert got[0]["days"] == [1, 3] and got[0]["max_per_day"] == 4
    assert "hours_from" not in got[1] and "days" not in got[1]


# ── what the agent is told ───────────────────────────────────────────────────
def test_the_prompt_names_rules_only_when_set():
    plain = services.render_for_prompt([MEET])
    assert "(30 min)" in plain and services.RULES_PROMPT_LINE not in plain
    ruled = services.render_for_prompt([RULED])
    assert "Meet & greet (30 min, booked Tue and Thu, 13:00–16:00, up to 4 a day)" in ruled
    assert services.RULES_PROMPT_LINE in ruled


def test_the_stay_diary_line_names_an_appointments_rules():
    cfg = {**CFG, "resource_types": [{"name": "Meet & greet", "count": 2}]}
    plain = scheduling.stay_diary_line(cfg, [{**MEET, "resource": "Meet & greet"}])
    assert "Meet & greet — 30 min, 2 at a time." in plain and services.RULES_PROMPT_LINE not in plain
    ruled = scheduling.stay_diary_line(cfg, [{**services.clean_service_types([RULED])[0], "resource": "Meet & greet"}])
    assert "Meet & greet — 30 min, 2 at a time, booked Tue and Thu, 13:00–16:00, up to 4 a day" in ruled
    assert services.RULES_PROMPT_LINE in ruled


# ── the diary only offers what the rules allow ───────────────────────────────
def _starts(svc, day, counts=None):
    length = slotgrid.blocks_for_minutes(30)
    return [s["window"] for s in scheduling.service_availability(
        CFG, [], length, day, day, NOW, svc=svc, day_counts=counts)]


def test_no_rules_is_unchanged():
    assert _starts(None, TUE) == _starts(services.clean_service_types([MEET])[0], TUE)
    assert _starts(None, TUE)[0] == "07:00"


def test_hours_keep_the_whole_appointment_inside_them():
    got = _starts(services.clean_service_types([RULED])[0], TUE)
    assert got[0] == "13:00" and got[-1] == "15:00"                # hourly starts; 15:00 + 30 min fits
    assert all("13:00" <= g <= "15:30" for g in got)


def test_a_day_not_ticked_has_no_times():
    assert _starts(services.clean_service_types([RULED])[0], WED) == []


def test_a_day_at_its_max_has_no_times():
    svc = services.clean_service_types([RULED])[0]
    assert _starts(svc, TUE, {TUE.isoformat(): 3})                  # 3 of 4 — still open
    assert _starts(svc, TUE, {TUE.isoformat(): 4}) == []


# ── the executor: check, and the hold as the backstop ────────────────────────
def _exec(monkeypatch, counts=None):
    svc = {**services.clean_service_types([RULED])[0], "resource": "Meet & greet"}
    cfg = {**CFG, "intake_fields": [dict(STAY_FIELD)], "service_types": [svc],
           "resource_types": [{"name": "Meet & greet", "count": 2}]}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "count_service_bookings_by_day",
                        lambda *a, **k: dict(counts or {}))
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "crm_list_field_definitions", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(scheduling.db, "insert_stay_booking",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not book")))
    execute, _ = scheduling.make_executor("t1", contact="sam@x.co", stay=True, now=NOW,
                                          service_types=[svc])
    return execute


def test_the_check_returns_only_times_inside_the_rules(monkeypatch):
    out = _exec(monkeypatch)("check_appointment_availability",
                             {"service": "Meet & greet", "date_from": TUE.isoformat(), "date_to": WED.isoformat()})
    days = {s["date"] for s in out["available"]}
    assert days == {TUE.isoformat()}
    assert min(s["window"] for s in out["available"]) == "13:00"


def test_the_hold_refuses_outside_the_rules_with_the_reason(monkeypatch):
    wed = _exec(monkeypatch)("hold_appointment", {"service": "Meet & greet", "date": WED.isoformat(), "window": "13:00"})
    assert wed["error"] == "service_day" and "Tue and Thu" in wed["message"]
    early = _exec(monkeypatch)("hold_appointment", {"service": "Meet & greet", "date": TUE.isoformat(), "window": "10:00"})
    assert early["error"] == "service_hours" and "13:00–16:00" in early["message"]
    full = _exec(monkeypatch, counts={TUE.isoformat(): 4})(
        "hold_appointment", {"service": "Meet & greet", "date": TUE.isoformat(), "window": "13:00"})
    assert full["error"] == "service_day_full" and "up to 4 a day" in full["message"]


# ── the pool bug ─────────────────────────────────────────────────────────────
class _Capture:
    def __init__(self):
        self.selected = ""

    def table(self, name):
        return self

    def select(self, cols):
        self.selected = cols
        return self

    def eq(self, *a):
        return self

    def in_(self, *a):
        return self

    def gte(self, *a):
        return self

    def lte(self, *a):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


def test_active_bookings_carry_the_pool_and_quantity():
    """Every pool check reads these two columns; without them "at once" was never
    enforced for an appointment and a stays diary counted appointments as dogs."""
    cap = _Capture()
    db.list_active_bookings("t1", "2026-11-01", "2026-11-02", client=cap)
    cols = set(cap.selected.split(","))
    assert {"resource_type", "quantity"} <= cols


def test_a_booked_appointment_now_counts_against_at_once():
    row = {"booking_date": TUE.isoformat(), "start_block": slotgrid.block_at("13:00"), "length_blocks": 2,
           "status": "confirmed", "window": "13:00", "resource_type": "Meet & greet", "quantity": None}
    cfg = {**CFG, "resource_types": [{"name": "Meet & greet", "count": 1}]}
    free = scheduling.service_availability(cfg, [row], 2, TUE, TUE, NOW, capacity=1, resource="Meet & greet")
    assert "13:00" not in {s["window"] for s in free}               # the one place is taken
    assert "14:00" in {s["window"] for s in free}


def test_the_settings_rows_carry_the_rules():
    """The form compiles, every service row (and the template for a new one) posts all
    four rule fields, and a row shows its own values."""
    from pathlib import Path
    from jinja2 import ChainableUndefined, Environment, FileSystemLoader
    root = Path(__file__).resolve().parents[1] / "src/app" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)), undefined=ChainableUndefined)
    html = env.get_template("app/_scheduling_settings.html").render(
        scheduling_cfg={}, day_hours={}, service_types=services.clean_service_types([RULED]),
        csrf_token=lambda _: "offline")
    row = html.split('id="svc-rows"', 1)[1].split('id="svc-add"', 1)[0]
    blank = html.split('<template id="svc-template">', 1)[1].split('</template>', 1)[0]
    for card, starts_open in ((row, False), (blank, True)):
        opening = card.split('<details ', 1)[1].split('>', 1)[0]
        assert 'svc-row' in opening and (' open' in opening) == starts_open
        assert card.index('data-service-title') < card.index('name="service_name"')
        assert card.index('After-job tasks') < card.index('class="svc-remove')
    for name in ("service_from", "service_until", "service_days", "service_max", "service_starts"):
        assert f'name="{name}"' in row and f'name="{name}"' in blank
    assert 'value="13:00"' in row and 'value="16:00"' in row and 'value="1,3"' in row and 'value="4"' in row
    assert row.count('aria-pressed="true"') == 2                        # Tue + Thu
    assert 'aria-pressed="true"' not in blank and 'name="service_days" value=""' in blank


# ── fixed start times (the rig's knowledge: "at 11am or 3pm", the 1pm one) ────
STARTS = {**MEET, "starts": "15:00, 11:00, 13:00, nonsense"}


def test_fixed_starts_are_cleaned_and_named():
    svc = services.clean_service_types([STARTS])[0]
    assert svc["starts"] == ["11:00", "13:00", "15:00"]
    assert services.rules_label(svc) == "at 11:00, 13:00 or 15:00"
    assert "Meet & greet (30 min, booked at 11:00, 13:00 or 15:00)" in services.render_for_prompt([svc])


def test_only_the_fixed_starts_are_offered():
    assert _starts(services.clean_service_types([STARTS])[0], TUE) == ["11:00", "13:00", "15:00"]


def test_a_hold_at_another_time_names_the_starts(monkeypatch):
    monkeypatch.setattr(scheduling.db, "crm_list_field_definitions", lambda *a, **k: [])
    svc = {**services.clean_service_types([STARTS])[0], "resource": "Meet & greet"}
    cfg = {**CFG, "intake_fields": [dict(STAY_FIELD)], "service_types": [svc],
           "resource_types": [{"name": "Meet & greet", "count": 1}]}
    for name, fn in (("get_scheduling_config", lambda *a, **k: cfg), ("list_active_bookings", lambda *a, **k: []),
                     ("count_service_bookings_by_day", lambda *a, **k: {}), ("bookings_for_contact", lambda *a, **k: []),
                     ("offered_bookings_for_contact", lambda *a, **k: [])):
        monkeypatch.setattr(scheduling.db, name, fn)
    monkeypatch.setattr(scheduling.db, "insert_stay_booking",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not book")))
    execute, _ = scheduling.make_executor("t1", contact="sam@x.co", stay=True, now=NOW, service_types=[svc])
    out = execute("hold_appointment", {"service": "Meet & greet", "date": TUE.isoformat(), "window": "12:00"})
    assert out["error"] == "service_start" and "11:00, 13:00 or 15:00" in out["message"]


def test_the_form_carries_fixed_starts():
    got = services.parse_form(["Meet & greet"], ["30"], [""], starts_list=["11:00, 13:00, 15:00"])
    assert got[0]["starts"] == ["11:00", "13:00", "15:00"]


def test_a_closed_day_is_said_to_be_closed_not_full(monkeypatch):
    # Live 2026-09-24 (Sophie/Pepper): the diary was closed 1-6 Oct and the
    # reply said "we're fully booked for meet & greets on Tuesday 6th October".
    global CFG
    was = CFG
    CFG = {**was, "blackout_dates": [TUE.isoformat()]}
    try:
        out = _exec(monkeypatch)("check_appointment_availability",
                                 {"service": "Meet & greet", "date_from": TUE.isoformat(), "date_to": TUE.isoformat()})
    finally:
        CFG = was
    assert out["available"] == []
    assert out["closed_days"] == [TUE.isoformat()]
    assert "never call a closed day full or fully booked" in out["closed_note"]
