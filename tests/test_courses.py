"""Courses, the waiting list and re-enrolment — slice 3 (src/app/courses.py,
docs/plan-progress-packages.md). Pins: the settings' cleaning and the prompt
block; keys and labels (a standing slot vs a one-off date); the queue order
and one live offer per class; a freed PLACE vs a one-week gap; the
re-enrolment window against a series' last date; expired offers; the join /
already-waiting / several-lists rule; the executor's full-slot hint and the
join_waitlist tool; the portal tool gate; the record lines.

Run: python -m pytest tests/test_courses.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime, timedelta, timezone

from app import courses, diary_config

TODAY = date(2026, 9, 5)          # a Saturday


# ── settings ─────────────────────────────────────────────────────────────────
def test_clean_and_face_and_prompt():
    assert courses.clean({}) == {"courses": False, "other_dates": "hold", "hold_while_gathering": True, "waitlist": False, "waitlist_notify": "approve",
                                 "offer_hours": 48, "reenrol_days": 21, "reenrol_notify": "approve",
                                 "waitlist_stage": "", "booked_stage": ""}
    c = courses.clean({"courses": "1", "waitlist": True, "waitlist_notify": "auto", "offer_hours": "24",
                       "reenrol_days": "14", "reenrol_notify": "bogus"})
    assert c["waitlist_notify"] == "auto" and c["offer_hours"] == 24 and c["reenrol_notify"] == "approve"
    assert courses.face_value(courses.clean({})) == "Off"
    assert courses.face_value(c) == "courses · waiting list · offers held 24 h · re-enrol 14 days before the end"
    assert courses.render_for_prompt({}) == ""
    txt = courses.render_for_prompt(c)
    assert "join_waitlist" in txt and "24 hours" in txt and "FIXED-START" in txt
    assert "join_waitlist" in diary_config.render_all_for_prompt({"courses": {"waitlist": True}})


# ── keys ─────────────────────────────────────────────────────────────────────
def test_keys_and_labels_and_next_dates():
    assert courses.slot_key("Group lesson", date(2026, 9, 12), "09:30") == "slot|Group lesson|5|09:30"
    assert courses.label("slot|Group lesson|5|09:30") == "Group lesson · Saturdays 09:30"
    assert courses.label("date|Assessment swim|2026-09-12|08:30") == "Assessment swim · 12th Sep 26 08:30"
    b = {"booking_date": "2026-09-12", "window": "09:30", "details": {"Service": "Group lesson"}}
    assert courses.keys_for_booking(b, standing=False) == ["date|Group lesson|2026-09-12|09:30"]
    assert courses.keys_for_booking({**b, "recurrence_id": "r1"}, standing=False)[1] == "slot|Group lesson|5|09:30"
    assert courses.keys_for_booking(b, standing=True)[1] == "slot|Group lesson|5|09:30"
    assert courses.next_dates("slot|x|5|09:30", TODAY) == [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19), date(2026, 9, 26)]
    assert courses.next_dates("date|x|2026-09-12|09:30", TODAY) == [date(2026, 9, 12)]
    assert courses.next_dates("date|x|2026-09-01|09:30", TODAY) == []


# ── the queue ────────────────────────────────────────────────────────────────
def _row(i, **k):
    base = {"id": f"w{i}", "series_key": "slot|Group lesson|5|09:30", "status": "waiting",
            "created_at": f"2026-09-0{i}T10:00:00+00:00", "contact_email": f"p{i}@x.co"}
    base.update(k)
    return base


def test_queue_order_one_live_offer_and_expiry():
    rows = [_row(3), _row(1), _row(2, series_key="slot|Ducklings|5|10:00")]
    assert courses.position(rows, rows[0]) == 2 and courses.position(rows, rows[1]) == 1
    assert courses.next_waiting(rows, "slot|Group lesson|5|09:30")["id"] == "w1"
    rows[1]["status"] = "offered"; rows[1]["expires_at"] = "2026-09-06T10:00:00+00:00"
    assert courses.next_waiting(rows, "slot|Group lesson|5|09:30") is None          # an offer is live
    assert courses.position(rows, rows[0]) == 2                                     # still queued behind it
    now = datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)
    assert [r["id"] for r in courses.expired_offers(rows, now)] == ["w1"]
    assert courses.expired_offers(rows, now - timedelta(hours=2)) == []


def test_a_freed_place_is_not_a_holiday_week():
    b = {"id": "b5", "recurrence_id": "r1", "contact": "kris@x.co"}
    assert not courses.place_freed_by_cancel(b, [{"id": "b6"}])
    assert courses.place_freed_by_cancel(b, [])
    assert courses.place_freed_by_cancel({"id": "b9"}, [])


def test_reenrol_due_uses_the_series_last_date():
    series = [{"status": "confirmed", "recurrence_id": "r1", "contact": "kris@x.co", "booking_date": d,
               "window": "09:30", "details": {"Service": "Group lesson"}}
              for d in ("2026-09-12", "2026-09-19", "2026-09-26")]
    series.append({"status": "confirmed", "recurrence_id": "r2", "contact": "kris@x.co", "booking_date": "2026-12-12"})
    series.append({"status": "cancelled", "recurrence_id": "r3", "contact": "kris@x.co", "booking_date": "2026-09-10"})
    due = courses.reenrol_due(series, TODAY, 21)
    assert [(d["recurrence_id"], d["last"].isoformat(), d["count"]) for d in due] == [("r1", "2026-09-26", 3)]
    assert courses.reenrol_due(series, TODAY, 0) == []
    assert courses.reenrol_due(series, date(2026, 10, 1), 21) == []                 # ended — no note


# ── I/O with a fake table ─────────────────────────────────────────────────────
class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._filters, self._payload = store, name, [], None
    def select(self, *a, **k): return self
    def eq(self, k, v): self._filters.append((k, v)); return self
    def gte(self, k, v): return self
    def in_(self, k, v): self._filters.append((k, tuple(v))); return self
    def order(self, *a, **k): return self
    def limit(self, n): return self
    @property
    def not_(self): return self
    def is_(self, *a): return self
    def insert(self, p): self._payload = ("insert", p); return self
    def update(self, p): self._payload = ("update", p); return self
    def execute(self):
        rows = self.store.setdefault(self.name, [])
        class R: pass
        r = R()
        if self._payload and self._payload[0] == "insert":
            p = dict(self._payload[1]); p.setdefault("id", f"{self.name}-{len(rows) + 1}")
            p.setdefault("created_at", f"2026-09-05T10:00:{len(rows):02d}+00:00")
            rows.append(p); r.data = [p]; return r
        matched = [x for x in rows if all((x.get(k) in v) if isinstance(v, tuple) else x.get(k) == v
                                          for k, v in self._filters)]
        if self._payload and self._payload[0] == "update":
            for x in matched: x.update(self._payload[1])
        r.data = matched; return r


def _fake(monkeypatch, store):
    from app import db
    monkeypatch.setattr(courses, "_c", lambda client=None: type("C", (), {"table": lambda self, n: FakeTable(store, n)})())
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    return store


def test_join_once_per_participant_but_several_lists(monkeypatch):
    store = _fake(monkeypatch, {})
    key = "slot|Group lesson|5|09:30"
    a = courses.join("t1", "kris@x.co", "c1", key, participant="Isla", instance_id="i-isla")
    assert a["joined"] and a["position"] == 1
    again = courses.join("t1", "kris@x.co", "c1", key, participant="Isla", instance_id="i-isla")
    assert again["already"] and again["position"] == 1
    finn = courses.join("t1", "kris@x.co", "c1", key, participant="Finn", instance_id="i-finn")
    assert finn["joined"] and finn["position"] == 2
    other = courses.join("t1", "kris@x.co", "c1", "slot|Ducklings|5|10:00", participant="Isla", instance_id="i-isla")
    assert other["joined"] and other["position"] == 1                            # several lists per participant
    assert len(store["waitlist"]) == 3
    lines = courses.lines_for_contact("t1", "kris@x.co")
    assert lines[0] == "Waiting list: Group lesson · Saturdays 09:30 for Isla — number 1"
    assert lines[1] == "Waiting list: Group lesson · Saturdays 09:30 for Finn — number 2"
    assert "hold_booking" in courses.context_block("t1", "kris@x.co")
    # a booking of that class closes the rows
    n = courses.on_booked("t1", "kris@x.co", [{"id": "b1", "booking_date": "2026-09-12", "window": "09:30",
                                                "details": {"Service": "Group lesson"}, "recurrence_id": "r1"}])
    assert n == 2 and {r["status"] for r in store["waitlist"]} == {"booked", "waiting"}


# ── the executor and the portal ──────────────────────────────────────────────
def test_executor_hints_the_list_when_full_and_joins(monkeypatch):
    from app import db, scheduling
    store = _fake(monkeypatch, {})
    cfg = carried_config({"service_types": [{"name": "Group lesson", "minutes": 30}],
           "courses": {"waitlist": True, "courses": True}, "capacity_per_window": 1,
           "working_days": "sat", "open_time": "09:00", "close_time": "12:00", "slot_minutes": 30,
           "min_notice_hours": 0, "horizon_days": 60})
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(db, "list_active_bookings", lambda *a, **k: [
        {"booking_date": "2026-09-12", "window": "09:30", "status": "confirmed", "start_block": 38, "length_blocks": 2}])
    monkeypatch.setattr(db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "email": "kris@x.co"})
    monkeypatch.setattr(db, "release_offered_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "offered_bookings_for_contact", lambda *a, **k: [])
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", service_types=cfg["service_types"],
                                              now=datetime(2026, 9, 5, 9, 0, tzinfo=scheduling.DIARY_TZ))
    out = execute("hold_booking", {"date": "2026-09-12", "window": "09:30", "service": "Group lesson", "summary": "x"})
    assert out["error"] == "full" and "join_waitlist" in out["message"]
    out = execute("join_waitlist", {"date": "2026-09-12", "window": "09:30", "service": "Group lesson",
                                    "subjects": [{"name": "Isla"}]})
    assert out["joined"] and out["position"] == 1 and out["label"] == "Group lesson · Saturdays 09:30"
    assert store["waitlist"][0]["participant"] == "Isla"
    # a date the diary has not opened (60-day horizon from 5 Sep) is not full — no list
    far = execute("join_waitlist", {"date": "2026-12-12", "window": "09:30", "service": "Group lesson"})
    assert far["error"] == "beyond_horizon" and far["opens_on"] == "2026-10-13" and "not full" in far["message"]
    # off → the tool says so instead of writing
    cfg["courses"] = {}
    execute, _ = scheduling.make_executor("t1", contact="kris@x.co", service_types=cfg["service_types"])
    assert execute("join_waitlist", {"date": "2026-09-12", "window": "09:30", "service": "Group lesson"})["error"] == "no_waitlist"


def test_the_drafter_spots_a_waiting_list_claim():
    from app.agent import _WAITLIST_CLAIM as rx
    for s in ("I have put Isla on the waiting list for that time.",
              "I've added Finn to our waiting list.",
              "so I'll note that preference and pass it to the waiting list",
              "waitlisting Isla as requested", "joined waitlist for Isla",
              "Isla is now on the waiting list"):
        assert rx.search(s), s
    for s in ("If the 10:00 class is full, would you like a place on the waiting list?",
              "We keep a waiting list for full classes.", "The place is confirmed."):
        assert not rx.search(s), s


def test_the_tool_is_offered_only_when_the_list_is_on():
    from app import scheduling, portal_data, portal_chat
    assert scheduling.JOIN_WAITLIST_TOOL["name"] == "join_waitlist"
    assert "join_waitlist" in portal_data.TOOL_NAMES and "join_waitlist" in portal_chat.BUSINESS_TOOLS
    assert "join_waitlist" in portal_data._DIARY_TOOLS
    names = {t["name"] for t in portal_data.tools_for(False)}
    assert "join_waitlist" not in names


def test_a_collapsed_series_frees_every_date_it_held(monkeypatch):
    """Run #8: Kris's ten Saturdays cancelled one by one; Sam's row waited on
    the FIRST (26 Sep). The last cancel (21 Nov) empties the series — the hook
    now offers against every date the series held, not the last row's."""
    import asyncio
    from datetime import date as _date
    cfg_row = {"courses": {"waitlist": True, "courses": False, "offer_hours": 48}}
    monkeypatch.setattr(courses.db, "get_scheduling_config", lambda t, client=None: carried_config(cfg_row))
    monkeypatch.setattr(courses, "_confirmed_series", lambda t, since, client=None: [])   # nothing of Kris's left
    series = [{"id": f"b{i}", "booking_ref": f"BK-{i}", "booking_date": d, "window": "10:00", "status": "cancelled",
               "contact": "kris@x.co", "recurrence_id": "r1", "details": {"Service": "1-hour lesson"}}
              for i, d in enumerate(["2026-09-26", "2026-10-03", "2026-10-10"])]
    monkeypatch.setattr(courses, "_series_rows", lambda t, rid, contact, since, client=None: list(series))
    waiting_key = courses.date_key("1-hour lesson", _date(2026, 9, 26), "10:00")
    monkeypatch.setattr(courses, "list_rows", lambda t, keys=None, client=None, **k: [{"key": keys[0]}] if keys and keys[0] == waiting_key else [])
    monkeypatch.setattr(courses, "has_room", lambda t, key, today, **k: True)
    offered = []

    async def offer_next(t, key, cfg, client=None, **k):
        offered.append(key); return {"key": key}
    monkeypatch.setattr(courses, "offer_next", offer_next)
    last = {"id": "b9", "booking_ref": "BK-9", "booking_date": "2026-11-21", "window": "10:00", "status": "confirmed",
            "contact": "kris@x.co", "recurrence_id": "r1", "details": {"Service": "1-hour lesson"}}
    out = asyncio.run(courses.on_place_freed("t1", last, today=_date(2026, 9, 5)))
    assert offered == [waiting_key] and out == [{"key": waiting_key}]
    # one occurrence cancelled with the rest standing is a gap, not a freed place
    monkeypatch.setattr(courses, "_confirmed_series",
                        lambda t, since, client=None: [{**last, "id": "b8", "booking_date": "2026-11-14"}])
    offered.clear()
    assert asyncio.run(courses.on_place_freed("t1", last, today=_date(2026, 9, 5))) == [] and offered == []
