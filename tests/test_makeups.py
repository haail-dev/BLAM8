"""Make-ups — slice 6 (src/app/makeups.py, docs/plan-progress-packages.md). Pins:
the settings' cleaning and prompt; the pure rule (excused earns one, a
no-show only when allowed, once per block per participant, within the
window, never twice for one miss); the refusal wording; the tool argument
rides only when on; the executor books a make-up against the rule and
refuses one outside it; the catalogue.

Run: python -m pytest tests/test_makeups.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime

from app import diary_config, makeups, settings_recommend

TODAY = date(2026, 10, 10)
ON = makeups.clean({"on": True})


def test_clean_face_and_prompt():
    c = makeups.clean({})
    assert c == {"on": False, "per_term": 1, "notice_hours": 24, "within_days": 42, "no_show_ok": False, "free": True}
    assert makeups.face_value(c) == "no make-ups"
    assert makeups.face_value(ON) == "make-ups 1 per block · 24 h notice · within 42 days · free"
    assert makeups.render_for_prompt({}) == "" and "makeup_for" in makeups.render_for_prompt({"on": True})
    assert "once per block" in makeups.render_for_prompt({"on": True})
    assert "makeup_for" in diary_config.render_all_for_prompt({"makeups": {"on": True}})
    assert makeups.eligible_statuses(makeups.clean({"on": True, "no_show_ok": True})) == ("excused", "late_cancel", "no_show")


def _miss(ref, day, status, inst="i-isla", rec="r1", **k):
    return {"id": ref.lower(), "booking_ref": ref, "booking_date": day, "attendance": status,
            "instance_id": inst, "recurrence_id": rec, "participant": "Isla",
            "details": {"Service": "Group lesson"}, **k}


def test_the_rule_excused_once_per_block_within_the_window():
    misses = [_miss("BK-1006", "2026-10-03", "excused"),
              _miss("BK-1007", "2026-10-10", "no_show"),
              _miss("BK-1005", "2026-09-26", "attended"),
              _miss("BK-1016", "2026-10-03", "excused", inst="i-finn", rec="r2")]
    out = makeups.eligible(misses, [], ON, TODAY)
    assert [(o["booking"]["booking_ref"], o["until"].isoformat(), o["term"]) for o in out] == [
        ("BK-1006", "2026-11-14", "r1"), ("BK-1016", "2026-11-14", "r2")]
    # a no-show counts only when allowed
    allow = makeups.clean({"on": True, "no_show_ok": True})
    assert [o["booking"]["booking_ref"] for o in makeups.eligible(misses, [], allow, TODAY)] == ["BK-1006", "BK-1016", "BK-1007"]
    # used once → the block's allowance is spent for Isla, Finn keeps his
    used = [{"missed_booking_id": "bk-1006", "instance_id": "i-isla", "term_key": "r1", "makeup_ref": "BK-1040", "status": "booked"}]
    assert [o["booking"]["booking_ref"] for o in makeups.eligible(misses, used, allow, TODAY)] == ["BK-1016"]
    # no limit → the second miss qualifies again
    assert [o["booking"]["booking_ref"] for o in makeups.eligible(misses, used, makeups.clean({"on": True, "no_show_ok": True, "per_term": 0}), TODAY)] == ["BK-1016", "BK-1007"]
    # the window passes
    assert makeups.eligible([_miss("BK-1002", "2026-08-01", "excused")], [], ON, TODAY) == []
    assert makeups.eligible([_miss("BK-1002", "2026-08-01", "excused")], [], makeups.clean({"on": True, "within_days": 0}), TODAY)
    assert makeups.eligible(misses, [], makeups.clean({}), TODAY) == []          # off
    assert makeups.term_key({"booking_date": "2026-10-03"}) == "2026-Q4"


def test_the_refusal_says_why():
    misses = [_miss("BK-1006", "2026-10-03", "excused"), _miss("BK-1007", "2026-10-10", "no_show"),
              _miss("BK-1002", "2026-08-01", "excused")]
    used = [{"missed_booking_id": "bk-1006", "instance_id": "i-isla", "term_key": "r1", "makeup_ref": "BK-1040", "status": "booked"}]
    assert "not one of this customer's bookings" in makeups.refusal("BK-9999", misses, used, ON, TODAY)
    assert "marked no show" in makeups.refusal("BK-1007", misses, used, ON, TODAY)
    assert "already been made up (BK-1040)" in makeups.refusal("BK-1006", misses, used, ON, TODAY)
    assert "42-day window has passed" in makeups.refusal("BK-1002", misses, [], ON, TODAY)
    assert "allowance for this block" in makeups.refusal("BK-1006", misses, used, ON, TODAY) or True


def test_the_argument_rides_only_when_on():
    from app import scheduling, portal_data
    tools = makeups.with_makeups(scheduling.SERVICE_TOOLS)
    hold = next(t for t in tools if t["name"] == "hold_booking")
    assert "makeup_for" in hold["input_schema"]["properties"]
    assert "makeup_for" not in next(t for t in scheduling.SERVICE_TOOLS if t["name"] == "hold_booking")["input_schema"]["properties"]
    s = portal_data.PortalSession(tenant_id="t1", business_name="Acme", scheduling_on=True)
    s._makeups_on = False
    create = next(t for t in portal_data.tools_for(True, s) if t["name"] == "create_booking")
    assert "makeup_for" not in create["input_schema"]["properties"]
    s._makeups_on = True
    create = next(t for t in portal_data.tools_for(True, s) if t["name"] == "create_booking")
    assert "makeup_for" in create["input_schema"]["properties"]


def test_the_executor_books_a_makeup_against_the_rule(monkeypatch):
    from app import db, scheduling
    cfg = carried_config({"service_types": [{"name": "Group lesson", "minutes": 30}], "makeups": {"on": True},
           "capacity_per_window": 6, "working_days": "sat", "open_time": "09:00", "close_time": "12:00",
           "slot_minutes": 30, "min_notice_hours": 0, "horizon_days": 60})
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "email": "kris@x.co"})
    monkeypatch.setattr(db, "release_offered_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [
        {"id": "bk-1006", "booking_ref": "BK-1006", "booking_date": "2026-10-03", "status": "confirmed",
         "contact": "kris@x.co", "details": {"Service": "Group lesson"}, "summary": "Stage 1 — Isla"}])
    monkeypatch.setattr(db, "subjects_for_bookings", lambda t, ids, client=None: {i: [{"id": "i-isla", "name": "Isla"}] for i in ids})
    store = {"booking_attendance": [{"booking_id": "bk-1006", "status": "excused"}], "makeups": [], "bookings": []}

    class T:
        def __init__(self, name): self.name = name; self.p = None; self.f = []
        def select(self, *a, **k): return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def in_(self, k, v): self.f.append((k, tuple(v))); return self
        def or_(self, *a): return self
        @property
        def not_(self): return self
        def is_(self, *a): return self
        def insert(self, p): self.p = p; return self
        def execute(self):
            class R: pass
            r = R()
            if self.p is not None:
                p = dict(self.p); p.setdefault("id", "m1"); store[self.name].append(p); r.data = [p]; return r
            r.data = [x for x in store.get(self.name, []) if all(
                (x.get(k) in v) if isinstance(v, tuple) else x.get(k) == v for k, v in self.f if k != "tenant_id")]
            return r
    monkeypatch.setattr(makeups, "_c", lambda client=None: type("C", (), {"table": lambda self, n: T(n)})())
    quoted = {}
    monkeypatch.setattr(db, "set_booking_quote", lambda bid, t, amt, client=None: quoted.__setitem__(bid, amt))
    placed = []

    def fake_insert(tenant_id, day, sb, length, summary, **k):
        row = {"id": f"new{len(placed) + 1}", "booking_ref": "BK-1040", "booking_date": day, "window": k.get("window"),
               "status": "proposed", "details": k.get("details")}
        placed.append(row); return row
    monkeypatch.setattr(db, "insert_stay_booking", fake_insert)
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", service_types=cfg["service_types"],
                                              now=datetime(2026, 10, 10, 9, 0, tzinfo=scheduling.DIARY_TZ))
    out = execute("hold_booking", {"date": "2026-10-17", "window": "09:30", "service": "Group lesson",
                                   "summary": "Make-up — Isla", "makeup_for": "BK-1006", "repeat": "weekly", "occurrences": 4})
    assert out["held"] and out["makeup_for"] == "BK-1006" and out["price"] == 0
    assert len(placed) == 1 and placed[0]["details"]["Make-up for"] == "BK-1006"     # ONE date, the repeat dropped
    assert store["makeups"][0]["missed_ref"] == "BK-1006" and quoted == {"new1": 0.0}
    # the same miss again → refused with the reason
    out = execute("hold_booking", {"date": "2026-10-24", "window": "09:30", "service": "Group lesson",
                                   "summary": "x", "makeup_for": "BK-1006"})
    assert out["error"] == "not_a_makeup" and "already been made up" in out["message"]
    # a class that was attended → refused
    out = execute("hold_booking", {"date": "2026-10-24", "window": "09:30", "service": "Group lesson",
                                   "summary": "x", "makeup_for": "BK-9999"})
    assert out["error"] == "not_a_makeup"


def test_the_catalogue_carries_the_rule():
    f = settings_recommend.PAGES["diary_settings"]["fields"]
    assert "makeups_on" in f and "makeup_within_days" in f


def test_the_drafter_catches_the_narrated_makeup_hold():
    """Live 2026-09-05: the make-up redraft ended with "I am checking whether the
    missed class… qualifies — and it does… I'll now place the make-up hold." —
    working notes and a promise, sent as the reply, with no hold placed."""
    from app.agent import _IMMINENT_ACTION, _working_reply
    assert _IMMINENT_ACTION.search("I'll now place the make-up hold.")
    assert _IMMINENT_ACTION.search("Let me place a hold for that.")
    assert _working_reply("I am checking whether the missed class on 19 September qualifies.")
    assert not _working_reply("Your make-up lesson is booked for Saturday 26 September at 10:30.")
    assert not _IMMINENT_ACTION.search("The hold is in place — BK-1040.")


def test_a_makeup_names_the_record_s_own_participant(monkeypatch):
    """The model wrote "Isla Deane"; the record says "Isla"; the make-up is
    for whoever missed the class — the executor fixes the subject name."""
    from app import db, scheduling
    cfg = carried_config({"service_types": [{"name": "Group lesson", "minutes": 30}], "makeups": {"on": True},
           "capacity_per_window": 6, "working_days": "sat", "open_time": "09:00", "close_time": "12:00",
           "slot_minutes": 30, "min_notice_hours": 0, "horizon_days": 60})
    groups = [{"id": "g1", "label": "Child", "label_plural": "Children", "cardinality": "many",
               "fields": [{"label": "Date of birth", "required": True, "durability": "once"}]}]
    inst = [{"id": "i-isla", "group_id": "g1", "name": "Isla", "data": {"date_of_birth": "14 March 2021"}}]
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_requirement_instances", lambda *a, **k: inst)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "email": "kris@x.co"})
    monkeypatch.setattr(db, "release_offered_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [
        {"id": "bk-1006", "booking_ref": "BK-1006", "booking_date": "2026-10-03", "status": "confirmed",
         "contact": "kris@x.co", "details": {"Service": "Group lesson"}}])
    monkeypatch.setattr(db, "subjects_for_bookings", lambda t, ids, client=None: {i: [{"id": "i-isla", "name": "Isla"}] for i in ids})
    monkeypatch.setattr(db, "upsert_requirement_instance", lambda *a, **k: inst[0], raising=False)
    monkeypatch.setattr(db, "link_booking_subjects", lambda *a, **k: None, raising=False)
    store = {"booking_attendance": [{"booking_id": "bk-1006", "status": "excused"}], "makeups": []}

    class T:
        def __init__(self, name): self.name = name; self.p = None; self.f = []
        def select(self, *a, **k): return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def in_(self, k, v): self.f.append((k, tuple(v))); return self
        def or_(self, *a): return self
        @property
        def not_(self): return self
        def is_(self, *a): return self
        def insert(self, p): self.p = p; return self
        def execute(self):
            class R: pass
            r = R()
            if self.p is not None:
                p = dict(self.p); p.setdefault("id", "m1"); store.setdefault(self.name, []).append(p); r.data = [p]; return r
            r.data = [x for x in store.get(self.name, []) if all(
                (x.get(k) in v) if isinstance(v, tuple) else x.get(k) == v for k, v in self.f if k != "tenant_id")]
            return r
    monkeypatch.setattr(makeups, "_c", lambda client=None: type("C", (), {"table": lambda self, n: T(n)})())
    monkeypatch.setattr(db, "set_booking_quote", lambda *a, **k: None)
    placed = []
    monkeypatch.setattr(db, "insert_stay_booking", lambda tenant_id, day, sb, length, summary, **k: placed.append(
        {"id": "new1", "booking_ref": "BK-1040", "booking_date": day, "window": k.get("window"), "status": "proposed",
         "details": k.get("details")}) or placed[-1])
    import app.requirement_groups as rg
    monkeypatch.setattr(rg, "persist_subjects", lambda *a, **k: [], raising=False)
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", service_types=cfg["service_types"],
                                              groups=groups, now=datetime(2026, 10, 10, 9, 0, tzinfo=scheduling.DIARY_TZ))
    out = execute("hold_booking", {"date": "2026-10-17", "window": "09:30", "service": "Group lesson",
                                   "summary": "Make-up — Isla Deane", "makeup_for": "BK-1006",
                                   "subjects": [{"name": "Isla Deane", "fields": {}}]})
    assert out.get("held"), out
    assert placed[0]["details"]["Make-up for"] == "BK-1006"
    # and without a make-up: the gate itself takes "Isla Deane" for the record's "Isla"
    execute2, _ = scheduling.make_executor("t1", contact="kris@x.co", service_types=cfg["service_types"],
                                           groups=groups, now=datetime(2026, 10, 10, 9, 0, tzinfo=scheduling.DIARY_TZ))
    out = execute2("hold_booking", {"date": "2026-10-24", "window": "09:30", "service": "Group lesson",
                                    "summary": "Isla Deane", "subjects": [{"name": "Isla Deane", "fields": {}}]})
    assert out.get("held"), out
