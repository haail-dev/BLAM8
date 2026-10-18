"""Booking confirm → CRM promise auto-close (2026-07-26 e2e finding).

What this pins:
  - is_booking_confirmation_promise: narrow text gate — confirm-stem AND a
    booking noun ("A colleague will confirm the booking shortly" matches;
    "please confirm your address" / "we'll send the quote" never do)
  - crm_close_promises_for_confirmed_booking: closes ONLY open we_owe promises
    sourced from the same thread (the confirming log or a sibling log on the
    booking's thread_id) whose text passes the gate; no thread and no log → 0
    with zero queries; resolution goes through crm_resolve_promise_auto
  - set_booking_status hooks the close on 'confirmed' only (diary/portal path),
    and a close failure never breaks the status write (best-effort)
  - resolve_holds_for_log hooks the close when a confirmed hold actually moved
    (approve-&-send / auto-send path), passing the thread + booking ref through

Run: python -m pytest tests/ -q
"""
import pytest

from app import db


# ── fake supabase client (routed per table+kind, records every query) ────────
class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, sink, table, results):
        self.sink, self.table, self.results = sink, table, results
        self.kind = None
        self.payload = None
        self.filters = []

    def update(self, payload):
        self.kind, self.payload = "update", payload
        return self

    def select(self, cols):
        self.kind = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, tuple(vals)))
        return self

    def limit(self, n):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        self.sink.append(self)
        return _Res(self.results.get((self.table, self.kind), []))


class FakeSb:
    def __init__(self, results=None):
        self.queries = []
        self.results = results or {}

    def table(self, name):
        return _Query(self.queries, name, self.results)

    def sent(self, table, kind):
        return [q for q in self.queries if q.table == table and q.kind == kind]


# ── the text gate ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "A colleague will confirm the booking shortly",
    "We'll confirm your appointment tomorrow",
    "Will send confirmation of the reservation",
    "Confirm the slot once the driver is assigned",
])
def test_gate_matches_booking_confirmation_promises(text):
    assert db.is_booking_confirmation_promise(text)


@pytest.mark.parametrize("text", [
    None,
    "",
    "We'll send the quote over today",          # no confirm at all
    "Please confirm your delivery address",     # confirm, but not a booking
    "Booking BK-9 is on Friday",                # booking, but no confirm
    "We'll get back to you on pricing",
])
def test_gate_rejects_everything_else(text):
    assert not db.is_booking_confirmation_promise(text)


# ── crm_close_promises_for_confirmed_booking ─────────────────────────────────
def test_close_resolves_only_matching_promises_from_the_thread(monkeypatch):
    fake = FakeSb({
        ("email_log", "select"): [{"id": "log-1"}, {"id": "log-2"}],
        ("crm_promises", "select"): [
            {"id": "p-confirm", "description": "A colleague will confirm the booking shortly"},
            {"id": "p-quote", "description": "We'll send the quote over today"},
        ],
    })
    resolved = []
    monkeypatch.setattr(db, "crm_resolve_promise_auto",
                        lambda pid, tid, evidence=None: resolved.append((pid, tid, evidence)))

    n = db.crm_close_promises_for_confirmed_booking(
        "t1", thread_id="th-1", email_log_id="log-1", booking_ref="BK-7", client=fake)

    assert n == 1
    assert resolved == [("p-confirm", "t1", "Booking BK-7 confirmed")]
    # thread expansion is tenant-scoped
    log_q = fake.sent("email_log", "select")[0]
    assert ("tenant_id", "t1") in log_q.filters and ("thread_id", "th-1") in log_q.filters
    # the promise query is the conservative shape: open + we_owe + same-thread logs
    prom_q = fake.sent("crm_promises", "select")[0]
    assert ("tenant_id", "t1") in prom_q.filters
    assert ("status", "open") in prom_q.filters
    assert ("direction", "we_owe") in prom_q.filters
    assert ("email_log_id", ("log-1", "log-2")) in prom_q.filters


def test_close_without_thread_uses_the_log_alone(monkeypatch):
    fake = FakeSb({
        ("crm_promises", "select"): [
            {"id": "p-1", "description": "We will confirm your booking"},
        ],
    })
    resolved = []
    monkeypatch.setattr(db, "crm_resolve_promise_auto",
                        lambda pid, tid, evidence=None: resolved.append((pid, evidence)))

    n = db.crm_close_promises_for_confirmed_booking(
        "t1", thread_id=None, email_log_id="log-9", client=fake)

    assert n == 1
    assert resolved == [("p-1", "Booking confirmed")]   # no ref → generic evidence
    assert not fake.sent("email_log", "select")         # no thread — no expansion query
    prom_q = fake.sent("crm_promises", "select")[0]
    assert ("email_log_id", ("log-9",)) in prom_q.filters


def test_close_noops_with_no_email_trail(monkeypatch):
    fake = FakeSb()
    monkeypatch.setattr(db, "crm_resolve_promise_auto",
                        lambda *a, **k: pytest.fail("must not resolve anything"))
    assert db.crm_close_promises_for_confirmed_booking("t1", client=fake) == 0
    assert fake.queries == []   # manual booking, no thread/log — zero queries


# ── set_booking_status hook (diary / portal confirm) ─────────────────────────
def test_set_booking_status_confirmed_triggers_close(monkeypatch):
    fake = FakeSb()
    booking = {"id": "b-1", "thread_id": "th-1", "email_log_id": "log-1",
               "booking_ref": "BK-7"}
    monkeypatch.setattr(db, "get_booking", lambda bid, tid, client=None: dict(booking))
    calls = []
    monkeypatch.setattr(db, "crm_close_promises_for_confirmed_booking",
                        lambda tid, **kw: calls.append((tid, kw)))

    db.set_booking_status("b-1", "confirmed", "t1", client=fake)

    assert fake.sent("bookings", "update")[0].payload["status"] == "confirmed"
    (tid, kw), = calls
    assert tid == "t1"
    assert kw["thread_id"] == "th-1" and kw["email_log_id"] == "log-1"
    assert kw["booking_ref"] == "BK-7"


@pytest.mark.parametrize("status", ["cancelled", "declined", "completed"])
def test_set_booking_status_other_statuses_never_close(monkeypatch, status):
    fake = FakeSb()
    monkeypatch.setattr(db, "get_booking",
                        lambda *a, **k: pytest.fail("must not fetch the booking"))
    monkeypatch.setattr(db, "crm_close_promises_for_confirmed_booking",
                        lambda *a, **k: pytest.fail("must not close promises"))
    db.set_booking_status("b-1", status, "t1", client=fake)
    assert fake.sent("bookings", "update")[0].payload["status"] == status


def test_set_booking_status_close_failure_never_breaks_confirm(monkeypatch):
    fake = FakeSb()
    monkeypatch.setattr(db, "get_booking",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    db.set_booking_status("b-1", "confirmed", "t1", client=fake)   # must not raise
    assert fake.sent("bookings", "update")[0].payload["status"] == "confirmed"


# ── resolve_holds_for_log hook (approve-&-send / auto-send confirm) ──────────
def test_resolve_holds_confirmed_closes_promises_with_thread_and_ref(monkeypatch):
    fake = FakeSb({
        ("bookings", "update"): [{"id": "b-1", "booking_ref": "BK-9"}],
        ("email_log", "select"): [{"thread_id": "th-2"}],
    })
    calls = []
    monkeypatch.setattr(db, "crm_close_promises_for_confirmed_booking",
                        lambda tid, **kw: calls.append((tid, kw)))

    moved = db.resolve_holds_for_log("log-1", "confirmed", "t1", client=fake)

    assert moved == 1
    (tid, kw), = calls
    assert tid == "t1"
    assert kw["thread_id"] == "th-2" and kw["email_log_id"] == "log-1"
    assert kw["booking_ref"] == "BK-9"


def test_resolve_holds_no_move_or_cancel_never_closes(monkeypatch):
    monkeypatch.setattr(db, "crm_close_promises_for_confirmed_booking",
                        lambda *a, **k: pytest.fail("must not close promises"))
    # confirmed, but no proposed hold actually moved
    db.resolve_holds_for_log("log-1", "confirmed", "t1", client=FakeSb())
    # a hold moved, but to cancelled (skip / re-draft)
    fake = FakeSb({("bookings", "update"): [{"id": "b-1"}]})
    db.resolve_holds_for_log("log-1", "cancelled", "t1", client=fake)


def test_the_matcher_reads_live_promises_right():
    """2026-09-27, the open promises in live data: the genuine ones close, the
    lookalikes ("confirmed bookings", "details are confirmed") never do."""
    from app.db import is_booking_confirmation_promise as is_c
    assert is_c("A colleague will confirm the booking shortly.")
    assert is_c("A colleague will send a formal confirmation of the Wednesday 16 September 10:00 booking shortly.")
    assert is_c("Confirm the appointment straight away once Lucy chooses a slot.")
    assert not is_c("Investigate and resolve the Outlook calendar sync issue for the user's confirmed bookings not appearing in Outlook.")
    assert not is_c("Provide a provisional diary slot for a walkthrough once email platform details are confirmed.")
    assert not is_c("Confirm your address and send the quote.")
