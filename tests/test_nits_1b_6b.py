"""The per-slice nits 1b–6b (docs/plan-progress-packages.md, 2026-09-05).

1b: a near-miss level normalises; a contact-ladder field write fires the
    change; an award is named in the facts.
2b: a package name inside a description matches only when unique; the
    portal balance switch is honoured; a line's "for (name)" resolves.
3b: a windows-diary list label; released holds reopen the waiter; the day
    panel's waiting rows.
4b: a customer's certificate has its own feed kind; the catalogue page.
5b: a withdrawn note; the register's hint; the portal tool.
6b: a missed make-up feeds back; the notice rule when the time is known.

Run: python -m pytest tests/test_nits_1b_6b.py -q
"""
import asyncio
from datetime import date

from app import courses, customer_log as clog, ladders, makeups, settings_recommend, wallet


# ── 1b ───────────────────────────────────────────────────────────────────────
def test_near_miss_levels_normalise():
    levels = ["Stage 1", "Stage 2", "Stage 3"]
    assert ladders.normalise_level("1", levels) == "Stage 1"
    assert ladders.normalise_level("stage 2", levels) == "Stage 2"
    assert ladders.normalise_level("Stage3", levels) == "Stage 3"
    assert ladders.normalise_level("Stage 2 ", levels) == "Stage 2"
    assert ladders.normalise_level("9", levels) is None and ladders.normalise_level("", levels) is None
    belts = ["White belt", "Yellow belt", "Orange belt"]
    assert ladders.normalise_level("yellow belt", belts) == "Yellow belt" and ladders.normalise_level("1", belts) is None


def test_the_award_is_named_and_a_customer_reads_naturally():
    f = {"key": "badge", "label": "Swim England badge", "options": ["Stage 4", "Stage 5"], "ladder": {"notify": "approve"}}
    facts = ladders.facts_for("Finn", "Child", f, "Stage 4", "Stage 5", "Kris")
    assert 'earned "Stage 5"' in facts and "congratulate" in facts
    plain = ladders.facts_for("Kris", "Customer", {"key": "k", "label": "CEFR level", "options": ["A1", "A2"]}, "A1", "A2", "Kris")
    # Run #8: the customer's own move speaks to them — never "Kris has moved" to Kris.
    assert plain.startswith("The reader of this message — Kris, the customer themselves — has moved to A2")
    assert "the customer's customer" not in plain and "second person" in plain


def test_a_contact_ladder_write_fires_the_change(monkeypatch):
    from app import db
    fired = []
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda t, entity=None, client=None: [
        {"key": "cefr", "label": "CEFR level", "options": ["A1", "A2", "B1"], "ladder": {"notify": "approve", "gates": []}, "entity": "contact"}])

    async def fake(*a, **k):
        fired.append((a[4]["key"], a[5], a[6]))
        return {"ok": True}
    monkeypatch.setattr(ladders, "on_level_change", fake)
    c = {"id": "c1", "name": "Kris", "email": "kris@x.co"}
    assert ladders.contact_field_changed("t1", c, "cefr", "A1", "a2")
    assert fired == [("cefr", "A1", "A2")]
    assert not ladders.contact_field_changed("t1", c, "cefr", "A2", "A2")       # no change
    assert not ladders.contact_field_changed("t1", c, "notes", None, "x")       # not a ladder
    assert not ladders.contact_field_changed("t1", c, "cefr", None, "Z9")       # not a level


# ── 2b ───────────────────────────────────────────────────────────────────────
def test_package_by_name_inside_a_description_only_when_unique():
    pk = wallet.clean_packages([{"name": "Block of 10", "quantity": 10}, {"name": "Block of 10 private", "quantity": 10}])
    assert wallet.find_package(pk, "Block of 10")["key"] == "block_of_10"
    assert wallet.find_package(pk, "Autumn — Block of 10 private for Isla")["key"] == "block_of_10_private"
    pk2 = wallet.clean_packages([{"name": "Block of 10", "quantity": 10}, {"name": "Private hours", "quantity": 5}])
    assert wallet.find_package(pk2, "Block of 10 and Private hours for Isla") is None            # two unrelated fit
    assert wallet.clean_prefs({})["portal"] is True and wallet.clean_prefs({"portal": False})["portal"] is False


def test_portal_balance_honours_the_switch(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {"extras": {"package_prefs": {"portal": False}}})
    assert wallet.portal_block("t1", "c1") == ""
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {"extras": {}})
    monkeypatch.setattr(wallet, "list_credits", lambda *a, **k: [{"package_name": "Block", "unit": "session", "quantity": 10, "used": 2, "paid": True}])
    assert "8 sessions of 10 left on Block" in wallet.portal_block("t1", "c1", today=date(2026, 9, 5))


# ── 3b ───────────────────────────────────────────────────────────────────────
def test_windows_diary_label_and_reopen_and_day_panel(monkeypatch):
    assert courses.label("slot||5|09:30") == "Saturdays 09:30"
    assert courses.label("date||2026-09-12|09:30") == "12th Sep 26 09:30"
    store = {"waitlist": [
        {"id": "w1", "tenant_id": "t1", "status": "booked", "booking_id": "b-dead", "series_key": "slot|Group lesson|5|09:30",
         "created_at": "2026-09-01T10:00:00+00:00", "contact_email": "a@x.co", "participant": "Finn", "label": "Group lesson · Saturdays 09:30"},
        {"id": "w2", "tenant_id": "t1", "status": "booked", "booking_id": "b-live", "series_key": "slot|Group lesson|5|09:30",
         "created_at": "2026-09-02T10:00:00+00:00", "contact_email": "b@x.co"},
        {"id": "w3", "tenant_id": "t1", "status": "waiting", "series_key": "date|Private 1:1|2026-09-20|09:00",
         "created_at": "2026-09-03T10:00:00+00:00", "contact_email": "c@x.co", "label": "Private 1:1 · 20th Sep 26 09:00"}],
        "bookings": [{"id": "b-dead", "tenant_id": "t1", "status": "cancelled"}, {"id": "b-live", "tenant_id": "t1", "status": "confirmed"}]}

    class T:
        def __init__(self, n): self.n = n; self.f = []; self.p = None
        def select(self, *a, **k): return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def in_(self, k, v): self.f.append((k, tuple(v))); return self
        def order(self, *a, **k): return self
        @property
        def not_(self): return self
        def is_(self, *a): return self
        def update(self, p): self.p = p; return self
        def execute(self):
            class R: pass
            r = R()
            rows = [x for x in store[self.n] if all((x.get(k) in v) if isinstance(v, tuple) else x.get(k) == v for k, v in self.f)]
            if self.p is not None:
                for x in rows: x.update(self.p)
            r.data = rows; return r
    monkeypatch.setattr(courses, "_c", lambda client=None: type("C", (), {"table": lambda self, n: T(n)})())
    assert courses.reopen_released("t1") == 1
    assert store["waitlist"][0]["status"] == "waiting" and store["waitlist"][1]["status"] == "booked"
    byday = courses.waiting_by_day("t1", ["2026-09-19", "2026-09-20", "2026-09-21"])
    assert [w["id"] for w in byday["2026-09-19"]] == ["w1"] and [w["id"] for w in byday["2026-09-20"]] == ["w3"]
    assert "2026-09-21" not in byday


# ── 4b ───────────────────────────────────────────────────────────────────────
def test_certificate_kind_and_catalogue():
    from app import deck, dependants_sweep, notify_prefs
    assert dependants_sweep.KIND_CONTACT == "certificate_expiry"
    assert deck.topic_of("certificate_expiry") == "bookings" and notify_prefs._BY_KIND["certificate_expiry"] == "notify_bookings"
    out = settings_recommend.clean("crm_fields", [
        {"name": "contact_field", "value": "First aid certificate expiry | date | expires 90 | the date on the certificate"},
        {"name": "contact_field", "value": "Membership number | text"}])
    assert len(out) == 2 and "crm_fields" in settings_recommend.catalogue_block()


# ── 5b ───────────────────────────────────────────────────────────────────────
def test_withdraw_my_note_and_the_register_hint(monkeypatch):
    from app import db, portal_chat, portal_data
    store = {"booking_notes": [
        {"id": "n1", "tenant_id": "t1", "booking_id": "b1", "author": "customer", "contact_email": "kris@x.co", "body": "first", "created_at": "2026-09-05T10:00:00+00:00"},
        {"id": "n2", "tenant_id": "t1", "booking_id": "b1", "author": "business", "contact_email": "kris@x.co", "body": "coach", "created_at": "2026-09-05T11:00:00+00:00"},
        {"id": "n3", "tenant_id": "t1", "booking_id": "b1", "author": "customer", "contact_email": "kris@x.co", "body": "second", "created_at": "2026-09-05T12:00:00+00:00"}]}

    class T:
        def __init__(self, n): self.n = n; self.f = []; self.p = None
        def select(self, *a, **k): return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def is_(self, k, v): self.f.append((k, None)); return self
        def order(self, *a, **k): return self
        def limit(self, n): return self
        def update(self, p): self.p = p; return self
        def execute(self):
            class R: pass
            r = R()
            rows = [x for x in store[self.n] if all(x.get(k) == v for k, v in self.f)]
            if self.p is not None:
                for x in rows: x.update(self.p)
            r.data = rows; return r
    monkeypatch.setattr(clog, "_c", lambda client=None: type("C", (), {"table": lambda self, n: T(n)})())
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    assert clog.latest_customer_note(clog.list_for_booking("t1", "b1")) == "second"
    out = clog.withdraw_last("t1", {"id": "b1", "booking_ref": "BK-1"}, "kris@x.co")
    assert out["withdrawn"] and out["note"] == "second" and store["booking_notes"][2]["deleted_at"]
    assert [n["id"] for n in clog.list_for_booking("t1", "b1")] == ["n1", "n2"]
    assert clog.job_context("t1", {"id": "b1"})["customer_note_hint"] == "first"
    assert "withdraw_my_note" in portal_data.TOOL_NAMES and "withdraw_my_note" in portal_chat.BUSINESS_TOOLS
    s = portal_data.PortalSession(tenant_id="t1", business_name="Acme", scheduling_on=True)
    assert "withdraw_my_note" in {t["name"] for t in portal_data.tools_for(True, s)}
    s.notes_on = False
    assert "withdraw_my_note" not in {t["name"] for t in portal_data.tools_for(True, s)}


# ── 6b ───────────────────────────────────────────────────────────────────────
def test_notice_rule_when_the_time_is_known_and_a_missed_makeup_feeds_back(monkeypatch):
    c = makeups.clean({"on": True})
    b = {"id": "bk-1006", "booking_ref": "BK-1006", "booking_date": "2026-10-03", "window": "09:30",
         "attendance": "excused", "instance_id": "i", "recurrence_id": "r1"}
    assert makeups.told_in_time(b, c)                                              # unknown = the register's word
    assert makeups.told_in_time({**b, "notified_at": "2026-10-02T09:00:00+00:00"}, c)
    assert not makeups.told_in_time({**b, "notified_at": "2026-10-03T08:00:00+00:00"}, c)
    late = {**b, "notified_at": "2026-10-03T08:00:00+00:00"}
    assert makeups.eligible([late], [], c, date(2026, 10, 10)) == []
    assert "less than 24 hours" in makeups.refusal("BK-1006", [late], [], c, date(2026, 10, 10))
    assert makeups.eligible([late], [], makeups.clean({"on": True, "no_show_ok": True}), date(2026, 10, 10))
    store = {"makeups": [{"id": "m1", "tenant_id": "t1", "makeup_booking_id": "new1", "missed_ref": "BK-1006", "status": "booked"}]}

    class T:
        def __init__(self, n): self.n = n; self.f = []; self.p = None
        def select(self, *a, **k): return self
        def eq(self, k, v): self.f.append((k, v)); return self
        def update(self, p): self.p = p; return self
        def execute(self):
            class R: pass
            r = R()
            rows = [x for x in store[self.n] if all(x.get(k) == v for k, v in self.f)]
            if self.p is not None:
                for x in rows: x.update(self.p)
            r.data = rows; return r
    from app import db
    monkeypatch.setattr(makeups, "_c", lambda client=None: type("C", (), {"table": lambda self, n: T(n)})())
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    out = makeups.on_attendance("t1", {"id": "new1", "booking_ref": "BK-1040"}, "excused")
    assert out["status"] == "missed" and store["makeups"][0]["status"] == "missed"
    assert makeups.on_attendance("t1", {"id": "new1", "booking_ref": "BK-1040"}, "attended")["status"] == "booked"
    assert makeups.on_attendance("t1", {"id": "other", "booking_ref": "BK-1"}, "excused") is None
    # a 'missed' make-up row no longer counts as used → the miss qualifies again
    used = [{"missed_booking_id": "bk-1006", "instance_id": "i", "term_key": "r1", "status": "missed"}]
    assert makeups.eligible([b], used, c, date(2026, 10, 10))
