"""Packages and the wallet — slice 2 (src/app/wallet.py, docs/plan-progress-packages.md).

Every decision is a setting (Kris 2026-09-05): what is sold lives on Invoice
settings, how a credit is spent on Diary settings. One thing is not: credits
belong to the customer, a line may be "for <participant>". Pins: the settings'
cleaning, package matching, the credit a booking spends from (participant
first, earliest expiry, never expired, service covered), the amount (sessions,
or hours by minutes / whole hour), mint on issue (by key or by description,
never twice), the register's four outcomes against the rules, the sweep's
planning, the balance wording, the catalogue.

Run: python -m pytest tests/test_wallet.py -q
"""
from datetime import date

import pytest

from app import settings_recommend, wallet

TODAY = date(2026, 9, 5)
PKGS = wallet.clean_packages([
    {"name": "Block of 10 lessons", "quantity": 10, "unit": "session", "price": 90, "valid_days": 365,
     "services": ["Group lesson"]},
    {"name": "Driving hours ×10", "quantity": "10", "unit": "hours", "price": "320", "valid_days": "180"},
    {"name": "", "quantity": 5},                       # no name — dropped
    {"name": "Zero", "quantity": 0},                   # no quantity — dropped
])


class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]


# ── settings ─────────────────────────────────────────────────────────────────
def test_clean_packages_and_prefs_and_credits():
    assert [p["key"] for p in PKGS] == ["block_of_10_lessons", "driving_hours_10"]
    assert PKGS[1]["unit"] == "hour" and PKGS[1]["price"] == 320.0 and PKGS[1]["valid_days"] == 180
    assert PKGS[0]["services"] == ["Group lesson"] and PKGS[1]["services"] == []
    assert wallet.clean_prefs({}) == {"paid_only": False, "low_at": 1, "expiry_days": 14, "portal": True}
    assert wallet.clean_credits({}) == {"spend": "auto", "late_hours": 24, "no_show_spends": True,
                                        "excused_refunds": True, "late_move_charged": True, "hours": "minutes"}
    assert wallet.clean_credits({"late_move_charged": False})["late_move_charged"] is False
    assert wallet.clean_credits({"spend": "register", "late_hours": "48", "no_show_spends": False,
                                 "hours": "hour"})["spend"] == "register"
    assert wallet.clean_credits({"spend": "weird"})["spend"] == "auto"


def test_the_packages_face_parses_its_rows():
    form = Form({"pkg_name": ["Block of 10", "Bad"], "pkg_quantity": ["10", "0"], "pkg_unit": ["session", "hour"],
                 "pkg_price": ["90", "1"], "pkg_valid_days": ["365", "1"], "pkg_services": ["Group lesson; Ducklings", ""],
                 "pkg_required": ["0", "1"], "pkg_key": ["", ""]})
    out = wallet.parse_packages_form(form)
    assert len(out) == 1 and out[0]["services"] == ["Group lesson", "Ducklings"] and not out[0]["required"]


def test_find_package_and_covers():
    assert wallet.find_package(PKGS, "driving_hours_10")["name"] == "Driving hours ×10"
    assert wallet.find_package(PKGS, "block of 10 lessons")["key"] == "block_of_10_lessons"
    assert wallet.find_package(PKGS, "Autumn term — Block of 10 lessons for Isla")["key"] == "block_of_10_lessons"
    assert wallet.find_package(PKGS, "Assessment swim") is None
    assert wallet.covers(PKGS[0], "Group lesson") and wallet.covers(PKGS[0], "group lesson — Isla")
    assert not wallet.covers(PKGS[0], "Private 1:1") and wallet.covers(PKGS[1], "anything")


# ── picking and spending ──────────────────────────────────────────────────────
def _row(**k):
    base = {"id": "c1", "tenant_id": "t1", "package_key": "block_of_10_lessons", "package_name": "Block of 10 lessons",
            "unit": "session", "quantity": 10, "used": 0, "expires_at": None, "paid": True,
            "instance_id": None, "created_at": "2026-09-01T00:00:00Z"}
    base.update(k)
    return base


def test_pick_credit_prefers_the_participant_then_the_earliest_expiry_and_skips_expired():
    rows = [_row(id="shared", expires_at="2026-12-31"),
            _row(id="isla", instance_id="i-isla", expires_at="2027-06-01"),
            _row(id="finn", instance_id="i-finn"),
            _row(id="old", expires_at="2026-01-01"),
            _row(id="spent", used=10)]
    assert wallet.pick_credit(rows, TODAY, "Group lesson", PKGS, {"i-isla"})["id"] == "isla"
    assert wallet.pick_credit(rows, TODAY, "Group lesson", PKGS, set())["id"] == "shared"
    assert wallet.pick_credit(rows, TODAY, "Private 1:1", PKGS, set()) is None       # not covered
    assert wallet.pick_credit([_row(id="unpaid", paid=False)], TODAY, "Group lesson", PKGS, set(), paid_only=True) is None
    assert wallet.pick_credit([_row(id="adj", package_key=None)], TODAY, "Private 1:1", PKGS, set())["id"] == "adj"


def test_spend_amount_sessions_and_hours():
    cfg = wallet.clean_credits({})
    assert wallet.spend_amount(_row(), {"length_blocks": 3}, cfg) == 1.0
    assert wallet.spend_amount(_row(unit="hour"), {"length_blocks": 3}, cfg) == 0.75       # 45 min
    assert wallet.spend_amount(_row(unit="hour"), {"length_blocks": 8}, cfg) == 2.0
    assert wallet.spend_amount(_row(unit="hour"), {"length_blocks": 3}, wallet.clean_credits({"hours": "hour"})) == 1.0


def test_balance_lines_speak_plainly():
    rows = [_row(used=4, expires_at="2026-12-12", paid=False), _row(id="h", package_name="Driving hours", unit="hour",
                                                                    quantity=10, used=8.5)]
    lines = wallet.balance_lines(rows, TODAY)
    assert lines[0] == "6 sessions of 10 left on Block of 10 lessons, valid until 12th Dec 26 (unpaid)"
    assert lines[1] == "1.5 hours of 10 left on Driving hours"
    assert wallet.balance_lines([_row(used=10)], TODAY) == []


# ── mint on issue ─────────────────────────────────────────────────────────────
class FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._filters, self._payload = store, name, [], None
    def select(self, *a, **k): return self
    def eq(self, k, v): self._filters.append((k, v)); return self
    def limit(self, n): return self
    def order(self, *a, **k): return self
    def in_(self, k, v): self._filters.append((k, tuple(v))); return self
    def insert(self, payload): self._payload = ("insert", payload); return self
    def update(self, payload): self._payload = ("update", payload); return self
    def upsert(self, payload): self._payload = ("upsert", payload); return self
    def delete(self): self._payload = ("delete", None); return self
    def execute(self):
        rows = self.store.setdefault(self.name, [])
        class R: pass
        r = R()
        if self._payload and self._payload[0] in ("insert", "upsert"):
            p = dict(self._payload[1]); p.setdefault("id", f"{self.name}-{len(rows) + 1}")
            rows.append(p); r.data = [p]; return r
        matched = [x for x in rows if all(
            (x.get(k) in v) if isinstance(v, tuple) else x.get(k) == v for k, v in self._filters)]
        if self._payload and self._payload[0] == "update":
            for x in matched: x.update(self._payload[1])
            r.data = matched; return r
        if self._payload and self._payload[0] == "delete":
            for x in matched: rows.remove(x)
            r.data = matched; return r
        r.data = matched; return r


def _fake_db(monkeypatch, store, *, packages=PKGS, credits_cfg=None):
    from app import db
    monkeypatch.setattr(wallet, "_c", lambda client=None: type("C", (), {"table": lambda self, n: FakeTable(store, n)})())
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {"extras": {"packages": packages, "package_prefs": {}}})
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: {"credits": credits_cfg or {}})
    monkeypatch.setattr(db, "crm_contact_by_email", lambda t, e, client=None: {"id": "c-kris", "email": e} if e else None)
    monkeypatch.setattr(db, "subjects_for_bookings", lambda t, ids, client=None: {i: [{"id": "i-isla"}] for i in ids})
    monkeypatch.setattr(db, "list_invoice_lines", lambda *a, **k: [])
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    return store


def test_an_issued_invoice_mints_by_key_or_description_and_never_twice(monkeypatch):
    store = _fake_db(monkeypatch, {})
    inv = {"id": "inv-1", "invoice_ref": "INV-1001", "customer_email": "kris@x.co", "contact_id": None, "status": "sent"}
    lines = [{"description": "Autumn block for Isla", "quantity": 1, "package_key": "block_of_10_lessons", "instance_id": "i-isla"},
             {"description": "Driving hours ×10", "quantity": 2},        # the concierge's line, matched by name
             {"description": "Goggles", "quantity": 1}]
    assert wallet.on_invoice_issued("t1", inv, lines=lines) == 2
    rows = store["wallet_credits"]
    assert rows[0]["quantity"] == 10 and rows[0]["instance_id"] == "i-isla" and rows[0]["expires_at"]
    assert rows[1]["quantity"] == 20 and rows[1]["unit"] == "hour" and rows[1]["paid"] is False
    assert wallet.on_invoice_issued("t1", inv, lines=lines) == 0          # issued twice — nothing minted
    wallet.on_invoice_paid("t1", inv)
    assert all(r["paid"] for r in rows)


def test_the_register_settles_the_wallet_per_the_rules(monkeypatch):
    store = _fake_db(monkeypatch, {"wallet_credits": [
        {"id": "c1", "tenant_id": "t1", "contact_id": "c-kris", "package_key": "block_of_10_lessons",
         "package_name": "Block of 10 lessons", "unit": "session", "quantity": 10, "used": 0,
         "expires_at": None, "paid": True, "instance_id": None, "created_at": "2026-09-01"}]})
    booking = {"id": "b1", "booking_ref": "BK-1004", "contact": "kris@x.co", "length_blocks": 2,
               "details": {"Service": "Group lesson"}, "summary": "Stage 1 — Isla"}
    out = wallet.apply_attendance("t1", booking, "attended", today=TODAY)
    assert out["wallet"]["spent"] == 1.0 and store["wallet_credits"][0]["used"] == 1.0
    assert store["booking_attendance"][0]["status"] == "attended"
    # marking it again does not spend twice
    assert wallet.apply_attendance("t1", booking, "attended", today=TODAY)["wallet"]["spent"] == 1.0
    assert store["wallet_credits"][0]["used"] == 1.0
    # excused gives it back (the default)
    out = wallet.apply_attendance("t1", booking, "excused", today=TODAY)
    assert out["wallet"]["refunded"] == 1.0 and store["wallet_credits"][0]["used"] == 0.0
    # a no-show spends by default…
    assert wallet.apply_attendance("t1", booking, "no_show", today=TODAY)["wallet"]["spent"] == 1.0
    # …and is free when the business says so
    _fake_db(monkeypatch, store, credits_cfg={"no_show_spends": False})
    out = wallet.apply_attendance("t1", booking, "late_cancel", today=TODAY)
    assert out["wallet"] == {"refunded": 1.0} and store["wallet_credits"][0]["used"] == 0.0
    assert wallet.apply_attendance("t1", booking, "nonsense") == {"error": "bad_status"}


# ── the sweep's planning ─────────────────────────────────────────────────────
def test_due_auto_spend_is_past_confirmed_unmarked_and_unspent():
    bookings = [{"id": "past", "status": "confirmed", "booking_date": "2026-09-04"},
                {"id": "today", "status": "confirmed", "booking_date": "2026-09-05"},
                {"id": "future", "status": "confirmed", "booking_date": "2026-09-12"},
                {"id": "marked", "status": "confirmed", "booking_date": "2026-09-03"},
                {"id": "spent", "status": "confirmed", "booking_date": "2026-09-03"},
                {"id": "cancelled", "status": "cancelled", "booking_date": "2026-09-03"},
                {"id": "ancient", "status": "confirmed", "booking_date": "2026-01-03"}]
    due = wallet.due_auto_spend(bookings, {"marked"}, {"spent"}, TODAY)
    assert [b["id"] for b in due] == ["past"]


def test_due_messages_once_each():
    prefs = wallet.clean_prefs({"low_at": 2, "expiry_days": 14})
    rows = [_row(id="low", used=9), _row(id="soon", expires_at="2026-09-15"),
            _row(id="both", used=9, expires_at="2026-09-10"), _row(id="sent", used=9, low_sent_at="x"),
            _row(id="fine", expires_at="2027-01-01")]
    out = [(r["id"], why) for r, why in wallet.due_messages(rows, TODAY, prefs)]
    assert out == [("low", "low"), ("soon", "expiry"), ("both", "low"), ("both", "expiry")]


def test_summary_counts_sold_and_unspent():
    rows = [_row(id="a", price=90, used=4), _row(id="b", price=90, used=10), _row(id="adj", package_key=None, quantity=1)]
    class C:
        def table(self, n):
            return FakeTable({"wallet_credits": rows}, n)
    s = wallet.summary("t1", today=TODAY, client=C())
    assert s["sold_count"] == 2 and s["sold_total"] == 180.0
    assert s["unspent_units"] == 7.0 and s["unspent_value"] == 54.0 and s["customers"] == 1


# ── the card ─────────────────────────────────────────────────────────────────
def test_the_packages_card_and_the_diary_credits_fields():
    out = settings_recommend.clean("packages", [
        {"name": "package", "value": "Block of 10 lessons | 10 | session | 90 | 365 | Group lesson"},
        {"name": "package", "value": "block of 10 lessons | 10 | session | 90 | 365"},   # a dupe by name
        {"name": "packages_paid_only", "value": "0"}, {"name": "package_low_at", "value": "1"},
    ])
    assert [v["name"] for v in out] == ["package", "packages_paid_only", "package_low_at"]
    out = settings_recommend.clean("diary_settings", [{"name": "credits_spend", "value": "auto"},
                                                      {"name": "late_cancel_hours", "value": "24"}])
    assert len(out) == 2 and "credits_spend" in settings_recommend.catalogue_block()


def test_find_package_by_the_names_words_spread_through_the_line():
    """The concierge restates a package in its own words: every word of ONE
    package's name inside the description still finds it (run #8: "10-hour
    driving lesson block" sold the "10-hour block" and minted nothing)."""
    pk = [{"key": "10_hour_block", "name": "10-hour block", "unit": "hour", "quantity": 10, "price": 320,
           "valid_days": 180, "services": [], "required": False},
          {"key": "intensive_20_hour_course", "name": "Intensive 20-hour course", "unit": "hour",
           "quantity": 20, "price": 620, "valid_days": 90, "services": [], "required": False}]
    assert wallet.find_package(pk, "10-hour driving lesson block")["key"] == "10_hour_block"
    assert wallet.find_package(pk, "Intensive 20-hour driving course")["key"] == "intensive_20_hour_course"
    # two packages fit → no match; a line naming nothing → no match
    assert wallet.find_package(pk, "10-hour block and the intensive 20-hour course") is None
    assert wallet.find_package(pk, "Mock test") is None


def test_the_sweeps_booking_query_carries_the_id():
    """run_sweep keys attendance and spend look-ups on b["id"]; the active-
    bookings select must include it (run #8: it did not, and the per-tenant
    try swallowed the KeyError — auto spend never ran on prod)."""
    from app import db as _db
    seen = {}

    class Chain:
        def __init__(self, cols=None):
            self.cols = cols
        def table(self, n): return self
        def select(self, cols): seen["cols"] = cols; return self
        def eq(self, *a): return self
        def in_(self, *a): return self
        def gte(self, *a): return self
        def lte(self, *a): return self
        def execute(self):
            class R: data = []
            return R()

    _db.list_active_bookings("t1", "2026-09-01", "2026-09-30", client=Chain())
    assert "id" in [c.strip() for c in seen["cols"].split(",")]


def test_the_job_pages_register_does_not_hide_behind_dependants():
    """The register / notes / waiting-list card renders for a business with
    no dependant kind (a driving pupil IS the customer); only the Dependants
    heading needs dep_groups (run #8)."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("src", "app", "templates", "app", "diary_job.html").read_text(encoding="utf-8")
    card = src.index("{% if dep_groups or register_on or booking_notes or (waitlist_on and waiting) %}")
    register = src.index("{% if register_on %}")
    dependants_only = src.index("{% if dep_groups %}")
    assert card < register < dependants_only


def test_the_sweep_spends_the_full_booking_row(monkeypatch):
    """The lean availability row has no contact; run_sweep re-reads the booking
    before spending (run #8: the sweep found the due lesson and spent nothing)."""
    import asyncio
    from app import db as _db
    got = {}

    class Chain:
        def table(self, n): return self
        def select(self, *a): return self
        def eq(self, *a): return self
        def in_(self, *a): return self
        def execute(self):
            class R: data = []
            return R()

    monkeypatch.setattr(wallet, "tenants_with_packages", lambda client=None: [("t1", {"extras": {"packages": [
        {"name": "10-hour block", "quantity": 10, "unit": "hour", "price": 320, "valid_days": 180}]}})])
    monkeypatch.setattr(_db, "get_scheduling_config", lambda tid, client=None: {"credits": {"spend": "auto"}})
    monkeypatch.setattr(_db, "list_active_bookings", lambda tid, a, b, client=None: [
        {"id": "b-1", "status": "confirmed", "booking_date": "2026-09-04"}])
    monkeypatch.setattr(_db, "get_booking", lambda bid, tid, client=None: {
        "id": bid, "status": "confirmed", "booking_date": "2026-09-04", "contact": "kris@x.co",
        "booking_ref": "BK-1002", "length_blocks": 4, "details": {"Service": "1-hour lesson"}})
    monkeypatch.setattr(wallet, "_c", lambda client=None: Chain())
    monkeypatch.setattr(wallet, "due_messages", lambda rows, today, prefs: [])

    def fake_spend(tid, booking, reason, *, today=None, client=None):
        got.update(booking); return {"spent": 1.0}
    monkeypatch.setattr(wallet, "spend_for_booking", fake_spend)

    stats = asyncio.run(wallet.run_sweep(date(2026, 9, 5)))
    assert stats["spent"] == 1
    assert got["contact"] == "kris@x.co" and got["booking_ref"] == "BK-1002"


@pytest.mark.asyncio
async def test_a_balance_note_filed_for_approval_counts_as_a_message(monkeypatch):
    """Run #8: the sweep printed {messages: 0} while queueing two notes — the
    counter read the send, and the send fails wherever the note waits for
    approval. A note filed in the queue is a message; a compose failure is not."""
    from types import SimpleNamespace
    monkeypatch.setattr(wallet.db, "crm_get_contact", lambda cid, tid, client=None: {"name": "Kris", "email": "k@x.com"})
    monkeypatch.setattr(wallet.db, "effective_config", lambda tid, mb, client=None: {})
    filed = []
    monkeypatch.setattr(wallet.db, "insert_email_log",
                        lambda tid, to, subject, body, draft, **kw: filed.append((subject, kw.get("status"), kw.get("kind"))) or ("log1", None))
    from app import agent, mailbox, usage, push
    async def compose(cfg, purpose, facts, **kw):
        return SimpleNamespace(text="You have 2 hours left.", model="m", usage={})
    async def send_mail(*a, **k):
        raise RuntimeError("no mailbox here")
    monkeypatch.setattr(agent, "compose_outbound", compose)
    monkeypatch.setattr(mailbox, "send_mail", send_mail)
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(push, "notify", lambda *a, **k: None)
    row = {"id": "w1", "contact_id": "c1", "package_name": "10-hour block", "unit": "hour",
           "quantity": 10, "used": 8}
    assert await wallet.send_balance_message("t1", row, "low", []) is True
    assert filed == [("Your 10-hour block — nearly used up", "pending", wallet.KIND)]
    async def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(agent, "compose_outbound", boom)
    assert await wallet.send_balance_message("t1", row, "low", []) is False
    assert len(filed) == 1


def test_the_thread_dock_and_the_flash_and_the_catalogue_nits():
    from pathlib import Path
    from app import settings_recommend
    root = Path(__file__).resolve().parents[1]
    thread = root.joinpath("src", "app", "templates", "app", "_thread.html").read_text(encoding="utf-8")
    assert "pending_rows[-1] if pending_rows else None" in thread           # an earlier pending note keeps its control
    flash = root.joinpath("src", "app", "static", "flash.js").read_text(encoding="utf-8")
    assert "el.dataset.flashHold === undefined" in flash
    diary = root.joinpath("src", "app", "templates", "app", "diary.html").read_text(encoding="utf-8")
    assert "data-flash data-flash-hold" in diary and "Every live booking cancelled" in diary
    p = settings_recommend.catalogue_block()
    assert "A service performed at the customer's location, or collecting a person, is not item delivery" in p
    # 2026-09-21 (app/intake_memory): a customer fact a BOOKING needs is a
    # booking row WITH a remember part — the same customer field, and asked.
    assert "write it as a booking_details row with a remember part instead" in p
    assert "remember: once, confirm or expires" in p
    from app import portal_chat
    prompt = portal_chat.build_portal_prompt(business_name="Hartley", scheduling_on=True, diary_flavour="service",
                                             verified_name="Kris", customer_profile=["Pick-up address: 14 Homend"])
    assert "is answered BY the record even when the business asks it per booking" in prompt
