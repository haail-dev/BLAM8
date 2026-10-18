"""The customer-writable log — slice 5 (src/app/customer_log.py,
docs/plan-progress-packages.md). Pins: the switch's default-on contract; a
customer's note lands on THEIR OWN booking and is refused on a dependant's
(the safeguarding rule); the business writes anywhere; the wording of the
log for the drafter and the portal; the portal tool (gated on identity, the
switch, and the customer's own bookings); the register page renders both
authors; the feed kind.

Run: python -m pytest tests/test_customer_log.py -q
"""
import asyncio

from app import customer_log as clog, portal_data


def test_the_switch_defaults_on_and_notes_are_cleaned():
    assert clog.on({}) and clog.on({"portal_customer_notes": None}) and clog.on(None)
    assert not clog.on({"portal_customer_notes": False})
    assert clog.clean_note("  felt   confident\non roundabouts ") == "felt confident on roundabouts"
    assert len(clog.clean_note("x" * 1000)) == clog.MAX_NOTE_CHARS


def test_a_dependants_booking_refuses_the_customer_but_not_the_business():
    b = {"id": "b1", "booking_ref": "BK-1028", "contact": "kris@x.co"}
    ref = clog.refusal_for_customer(b, [{"id": "i-finn", "name": "Finn"}])
    assert ref["error"] == "dependant_record" and "Finn" in ref["message"]
    assert clog.refusal_for_customer(b, []) is None


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._filters, self._payload = store, name, [], None
    def select(self, *a, **k): return self
    def eq(self, k, v): self._filters.append((k, v)); return self
    def order(self, *a, **k): return self
    def limit(self, n): return self
    def is_(self, *a): return self
    def insert(self, p): self._payload = p; return self
    def execute(self):
        rows = self.store.setdefault(self.name, [])
        class R: pass
        r = R()
        if self._payload is not None:
            p = dict(self._payload); p.setdefault("id", f"n{len(rows) + 1}")
            p.setdefault("created_at", f"2026-09-{5 + len(rows):02d}T10:00:00+00:00")
            rows.append(p); r.data = [p]; return r
        r.data = [x for x in rows if all(x.get(k) == v for k, v in self._filters)]; return r


def _fake(monkeypatch, store, subjects):
    from app import db, push
    monkeypatch.setattr(clog, "_c", lambda client=None: type("C", (), {"table": lambda self, n: FakeTable(store, n)})())
    monkeypatch.setattr(db, "subjects_for_bookings", lambda t, ids, client=None: {i: subjects for i in ids})
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    pushed = []
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushed.append(k))
    return store, pushed


def test_add_note_writes_the_customers_line_and_raises_the_card(monkeypatch):
    store, pushed = _fake(monkeypatch, {}, [])
    b = {"id": "b1", "booking_ref": "BK-1028", "contact": "kris@x.co", "booking_date": "2026-09-20"}
    out = clog.add_note("t1", b, "felt confident on roundabouts", contact_email="kris@x.co", contact_name="Kris")
    assert out["added"] and store["booking_notes"][0]["author"] == "customer"
    assert pushed and pushed[0]["kind"] == "customer_note" and "Kris added a note" in pushed[0]["title"]
    out = clog.add_note("t1", b, "watch the mirror check", author="business")
    assert out["added"] and len(pushed) == 1                      # the business's line raises no card
    assert clog.add_note("t1", b, "   ")["error"] == "empty"
    ls = clog.lines(list(reversed(clog.list_for_booking("t1", "b1"))))
    assert ls[0] == "6th Sep 26 (BK-1028, the business): watch the mirror check"
    assert ls[1] == "5th Sep 26 (BK-1028, their note): felt confident on roundabouts"
    assert "never contradict" in clog.context_block("t1", "kris@x.co")


def test_add_note_refuses_a_dependants_booking(monkeypatch):
    store, pushed = _fake(monkeypatch, {}, [{"id": "i-finn", "name": "Finn"}])
    b = {"id": "b1", "booking_ref": "BK-1028", "contact": "kris@x.co"}
    out = clog.add_note("t1", b, "he loved it", contact_email="kris@x.co")
    assert out["error"] == "dependant_record" and not store.get("booking_notes") and not pushed
    assert clog.add_note("t1", b, "teacher's note", author="business")["added"]


def test_the_portal_tool_is_gated_and_writes_through_the_module(monkeypatch):
    from app import db, portal_chat
    assert "add_note_to_booking" in portal_data.TOOL_NAMES and "add_note_to_booking" in portal_chat.BUSINESS_TOOLS
    assert "add_note_to_booking" in portal_data._DIARY_TOOLS
    s = portal_data.PortalSession(tenant_id="t1", business_name="Acme", scheduling_on=True)
    assert "add_note_to_booking" not in {t["name"] for t in portal_data.tools_for(False)}
    names = {t["name"] for t in portal_data.tools_for(True, s)}
    assert "add_note_to_booking" in names
    s.notes_on = False
    assert "add_note_to_booking" not in {t["name"] for t in portal_data.tools_for(True, s)}
    # anonymous → identify first; off → say so; a ref that is not theirs → not_found
    s2 = portal_data.PortalSession(tenant_id="t1", business_name="Acme", scheduling_on=True)
    out = asyncio.run(portal_data.execute(s2, "add_note_to_booking", {"booking_ref": "BK-1", "note": "x"}))
    assert out.get("error") in ("not_identified", "identify_first") or "identif" in str(out).lower()
    s2.contact = {"email": "kris@x.co", "name": "Kris"}
    s2.notes_on = False
    assert asyncio.run(portal_data.execute(s2, "add_note_to_booking", {"booking_ref": "BK-1", "note": "x"}))["error"] == "notes_off"
    s2.notes_on = True
    monkeypatch.setattr(db, "bookings_for_contact", lambda t, e, client=None: [
        {"id": "b1", "booking_ref": "BK-1028", "contact": e, "status": "confirmed", "booking_date": "2026-09-20"}])
    monkeypatch.setattr(portal_data, "_trace", lambda *a, **k: None)
    assert asyncio.run(portal_data.execute(s2, "add_note_to_booking", {"booking_ref": "BK-9", "note": "x"}))["error"] == "not_found"
    store, pushed = _fake(monkeypatch, {}, [])
    out = asyncio.run(portal_data.execute(s2, "add_note_to_booking", {"booking_ref": "bk-1028", "note": "felt confident"}))
    assert out["added"] and store["booking_notes"][0]["contact_email"] == "kris@x.co"


def test_the_feed_knows_the_kind():
    from app import deck, notify_prefs
    assert deck.topic_of("customer_note") == "bookings"
    assert notify_prefs._BY_KIND["customer_note"] == "notify_bookings"
