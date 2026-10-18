"""Practice OS — P0 foundations + P1 recall engine (docs/os/practice.md §13).

Pins the recall date math + stamp (the FSM twin), the recall work-set filter, the daily
recall job's compose→queue→dedupe→auto-send loop, the onboarding/plan wiring for the new
Practice industries, and that the module hub renders "Practice OS". No network: the db is
stubbed (a fake Supabase client where the real db fn is exercised, monkeypatched fns
elsewhere) — the autouse conftest guard fails loudly on any un-stubbed live call.

Run: python -m pytest tests/test_practice.py -q
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import crypto, db, onboarding, operating_systems, plans, practice_config, practice_noshow
from app import crm_intel
from app.jobs import send_recall as sr
from app.jobs import score_no_shows as sn
from app.jobs import send_appointment_reminders as ar


# ── a tiny fake Supabase query chain (only what the fns under test touch) ─────
class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table):
        self._table = table

    # every builder step returns self; the terminal execute() returns canned data
    def select(self, *a, **k): return self
    def update(self, payload): self._table.updates.append(payload); return self
    def insert(self, payload): self._table.inserts.append(payload); return self
    def eq(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self): return _FakeResult(self._table.rows)


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list = []
        self.inserts: list = []


class _FakeClient:
    def __init__(self, rows):
        self._t = _FakeTable(rows)

    def table(self, name): return _FakeQuery(self._t)

    @property
    def updates(self): return self._t.updates


# ── recall date math + stamp (the FSM complete_job_and_schedule_service twin) ──
def test_complete_appointment_stamps_recall_due_date(monkeypatch):
    booking = {"booking_date": "2026-01-15", "summary": "Check-up"}
    fake = _FakeClient([booking])
    statuses = []
    monkeypatch.setattr(db, "set_booking_status",
                        lambda bid, st, tid, **k: statuses.append(st))
    monkeypatch.setattr(db, "get_scheduling_config",
                        lambda tid, **k: {"service_types": [{"name": "Check-up", "recall_months": 6}]})

    due = db.complete_appointment_and_schedule_recall("b1", "t1", client=fake)

    # 6-month recall from Jan 15 → Jul 15, via _add_months (reused verbatim)
    assert due == "2026-07-15"
    assert statuses == ["completed"]
    # the recall stamp was written with the interval copied
    assert fake.updates[-1]["recall_due_date"] == "2026-07-15"
    assert fake.updates[-1]["recall_interval_months"] == 6


def test_complete_appointment_no_cadence_stamps_nothing(monkeypatch):
    fake = _FakeClient([{"booking_date": "2026-01-15", "summary": "Emergency"}])
    monkeypatch.setattr(db, "set_booking_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_scheduling_config",
                        lambda tid, **k: {"service_types": [{"name": "Emergency"}]})  # no recall_months
    assert db.complete_appointment_and_schedule_recall("b1", "t1", client=fake) is None
    assert fake.updates == []                             # nothing stamped


def test_complete_appointment_no_show_sets_status_never_stamps(monkeypatch):
    fake = _FakeClient([{"booking_date": "2026-01-15", "summary": "Check-up"}])
    statuses = []
    monkeypatch.setattr(db, "set_booking_status",
                        lambda bid, st, tid, **k: statuses.append(st))
    monkeypatch.setattr(db, "get_scheduling_config",
                        lambda tid, **k: {"service_types": [{"name": "Check-up", "recall_months": 6}]})
    assert db.complete_appointment_and_schedule_recall("b1", "t1", outcome="no_show", client=fake) is None
    assert statuses == ["no_show"]                        # status set…
    assert fake.updates == []                             # …but no recall for a no-show


def test_add_months_reused_for_recall():
    # the recall stamp reuses the FSM _add_months (day-clamp + year roll)
    assert db._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert db._add_months(date(2026, 12, 15), 6) == date(2027, 6, 15)


# ── the recall work-set filter (the list_jobs_due_service sibling) ────────────
def test_list_appointments_due_recall_filters_to_emailed(monkeypatch):
    rows = [
        {"id": "b1", "contact": "pat@x.co", "recall_due_date": "2026-07-01"},   # kept
        {"id": "b2", "contact": "", "recall_due_date": "2026-07-01"},           # no email → dropped
        {"id": "b3", "contact": "no-at-sign", "recall_due_date": "2026-07-02"}, # not an email → dropped
    ]
    got = db.list_appointments_due_recall("t1", "2026-07-14", client=_FakeClient(rows))
    assert [b["id"] for b in got] == ["b1"]


# ── the daily recall job: compose → queue → dedupe → auto-send ────────────────
class _Result(SimpleNamespace):
    pass


def _stub_compose(monkeypatch):
    async def fake_compose(cfg, purpose, facts, channel="email", **kwargs):
        return _Result(text="Hi — you're due your check-up, shall we rebook?",
                       usage={"input_tokens": 5}, model="fake-model")
    monkeypatch.setattr(sr.agent, "compose_outbound", fake_compose)


def _appt():
    return {"id": "b1", "contact": "pat@x.co", "summary": "Check-up",
            "booking_date": "2026-01-15", "recall_due_date": "2026-07-15",
            "practitioner": "Dr Amara", "booking_ref": "BK-1", "thread_id": None}


def test_remind_one_drafts_and_dedupes(monkeypatch):
    _stub_compose(monkeypatch)
    calls = {"insert": [], "marked": [], "events": [], "sent": []}
    monkeypatch.setattr(sr.db, "insert_email_log",
                        lambda *a, **k: (calls["insert"].append((a, k)) or ("log-1", True)))
    monkeypatch.setattr(sr.db, "mark_recall_reminded",
                        lambda bid, tid, **k: calls["marked"].append(bid))
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: calls["events"].append((a, k)))
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(sr.mailbox, "send_mail",
                        lambda *a, **k: calls["sent"].append(a))

    outcome = asyncio.run(sr._remind_one("t1", {"auto_send": False}, False, _appt()))

    assert outcome == "drafted"
    assert calls["marked"] == ["b1"]                       # deduped
    _, kw = calls["insert"][0]
    assert kw["kind"] == "recall" and kw["status"] == "pending"   # a fresh 'recall' outbound
    assert calls["sent"] == []                             # draft-only tenant never auto-sends


def test_remind_one_auto_sends_for_full_auto(monkeypatch):
    _stub_compose(monkeypatch)
    sent = []
    monkeypatch.setattr(sr.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(sr.db, "mark_recall_reminded", lambda *a, **k: None)
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sr.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_send(*a, **k): sent.append(a)
    monkeypatch.setattr(sr.mailbox, "send_mail", fake_send)

    outcome = asyncio.run(sr._remind_one("t1", {"auto_send": True}, True, _appt()))
    assert outcome == "sent"
    assert len(sent) == 1                                   # auto-sent


def test_run_picks_due_appointments_per_practice_tenant(monkeypatch):
    _stub_compose(monkeypatch)
    monkeypatch.setattr(sr.db, "tenants_with_modules", lambda mods, **k: ["t1"])
    monkeypatch.setattr(sr.db, "effective_config", lambda *a, **k: {"auto_send": False})
    seen = {}

    def _due(tid, cutoff, **k):
        seen["cutoff"] = cutoff
        return [_appt()]
    monkeypatch.setattr(sr.db, "list_appointments_due_recall", _due)
    monkeypatch.setattr(sr.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(sr.db, "mark_recall_reminded", lambda *a, **k: None)
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(sr.run())
    assert out["drafted"] == 1 and out["sent"] == 0
    assert seen["cutoff"]                                   # a cutoff (today + lead) was passed


def test_run_gate_keys_off_the_practice_module(monkeypatch):
    # the sweep only touches practice-module tenants (db.tenants_with_modules(("practice",)))
    seen = {}

    def _gate(mods, **k):
        seen["mods"] = mods
        return []
    monkeypatch.setattr(sr.db, "tenants_with_modules", _gate)
    out = asyncio.run(sr.run())
    assert seen["mods"] == ("practice",)
    assert out["drafted"] == 0 and out["sent"] == 0


# ── practice_config (the services.py twin) ───────────────────────────────────
def test_recall_months_lookup_matches_service_case_insensitively():
    svcs = [{"name": "Check-up", "recall_months": 6}, {"name": "Hygiene", "recall_months": 3}]
    assert practice_config.recall_months_for_service(svcs, "check-up") == 6
    assert practice_config.recall_months_for_service(svcs, "HYGIENE") == 3
    assert practice_config.recall_months_for_service(svcs, "Filling") is None   # no such service


def test_practitioners_clean_dedupes_and_defaults_resource():
    got = practice_config.clean_practitioners([
        {"name": "Dr Amara"}, {"name": "dr amara"},           # dupe dropped
        {"name": "Priya", "resource": "Hygiene chair", "capacity_per_day": 8},
        {"name": "  "},                                       # blank dropped
    ])
    assert [p["name"] for p in got] == ["Dr Amara", "Priya"]
    assert got[0]["resource"] == "Dr Amara"                   # resource defaults to the name
    assert got[1]["resource"] == "Hygiene chair" and got[1]["capacity_per_day"] == 8


# ── onboarding + OS wiring ───────────────────────────────────────────────────
def test_practice_os_is_live_and_industries_mapped():
    assert operating_systems.is_live("practice")             # Shape shows in the picker
    assert operating_systems.os_for_industry("dentist") == "practice"
    assert operating_systems.os_for_industry("salon") == "practice"
    assert operating_systems.os_for_industry("physio") == "practice"


def test_practice_industries_present_with_os():
    by_id = {i["id"]: i for i in onboarding.INDUSTRIES}
    for iid in ("dentist", "salon", "physio"):
        assert by_id[iid]["os"] == "practice" and by_id[iid]["live"] is True
    for iid in ("chiropractor", "vet", "tutor", "coach", "personal_trainer", "therapist"):
        assert by_id[iid]["os"] == "practice"
        assert by_id[iid]["live"] is False and by_id[iid].get("hidden") is True
    # data sources seeded for the launch industries
    assert "dentist" in onboarding.DATA_SOURCES and "physio" in onboarding.DATA_SOURCES
    assert "salon" in onboarding.DATA_SOURCES


# ── plan / module entitlement ────────────────────────────────────────────────
def test_practice_is_a_managed_module_via_industry_meta():
    assert "practice" in plans.MANAGED_MODULES
    assert plans.INDUSTRY_MODULE["dentist"] == "practice"
    # a dental tenant on the +industry tier gets scheduling + practice (its OWN module)
    assert plans.modules_for("professional", "dentist") == {"scheduling", "crm", "practice"}
    assert plans.modules_for("starter", "dentist") == {"scheduling", "crm"}     # Core = base only


def test_managed_modules_match_operator_switchboard_with_practice():
    from app.admin import TOGGLABLE_MODULES
    assert set(plans.MANAGED_MODULES) == TOGGLABLE_MODULES   # the invariant P0 must preserve


def test_apply_plan_modules_dentist_activates_practice_no_keyerror(monkeypatch):
    states = {}
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "dentist"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: None)
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "practice"}
    assert states["practice"] is True and states["scheduling"] is True
    assert states["tms"] is False and states["fsm"] is False   # never another vertical's module


def test_practice_tier_parked_but_module_meta_survives():
    # Two-tier collapse (2026-07-24): the OS tier is off the public catalog;
    # module metadata stays for founding partners / future un-parking.
    assert not any(r["key"] == "professional" for r in plans.billing_plans("dentist"))
    meta = plans.industry_module_meta("dentist")
    assert meta["name"] == "Practice OS"
    assert "Automatic recall in your voice" in meta["features"]


# ── the module hub renders Practice OS ───────────────────────────────────────
@pytest.fixture
def hub_client(monkeypatch):
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"practice"})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_module_hub_renders_practice_os(hub_client):
    html = hub_client.get("/app/modules").text
    assert "Practice OS" in html
    assert "/app/practice" in html                           # its card links to the surface


# ═══════════════════════════════════════════════════════════════════════════════
#  P3 — no-show prediction (docs/os/practice.md §12.1, §14)
# ═══════════════════════════════════════════════════════════════════════════════

# ── heuristic prior (deterministic) + the cold-start guardrail (§14) ──────────
def test_heuristic_prior_high_history_scores_high():
    # a patient who's missed half their appointments, silent on reminders → HIGH prior
    prior = practice_noshow.heuristic_prior({
        "total_appts": 6, "no_shows": 3, "has_prior_no_show": True,
        "reminders_ignored": True, "lead_days": 30, "first_visit": False})
    assert prior >= 70                                       # >= the default high threshold


def test_heuristic_prior_zero_history_is_never_high():
    # THE cold-start guard (§14): a brand-new patient with every worst-case pattern signal
    # must still NOT band high on zero attendance history.
    prior = practice_noshow.heuristic_prior({
        "total_appts": 0, "no_shows": 0, "first_visit": True, "reminders_ignored": True,
        "lead_days": 60, "hour": 8, "weekday": 4, "service_paid": False, "rescheduled": 3})
    assert prior < 70                                        # capped below high — no crying wolf
    assert practice_config.band_for_score(prior) in ("low", "medium")


def test_heuristic_prior_confirm_click_lowers_score():
    base = {"total_appts": 4, "no_shows": 2, "has_prior_no_show": True}
    high = practice_noshow.heuristic_prior(base)
    lowered = practice_noshow.heuristic_prior({**base, "confirmed_click": True})
    assert lowered < high                                    # a confirm click sharply lowers risk


def test_band_thresholds_default_and_override():
    assert practice_config.band_for_score(20) == "low"
    assert practice_config.band_for_score(50) == "medium"
    assert practice_config.band_for_score(80) == "high"
    # a packed-book practice raises the bar → the same 80 is only medium
    strict = {"no_show_thresholds": {"low": 60, "high": 90}}
    assert practice_config.band_for_score(80, strict) == "medium"


# ── the LLM refine: parses/validates the structured output + meters ───────────
class _FakeMsg:
    def __init__(self, payload, model="fake-haiku"):
        self.content = [SimpleNamespace(type="text", text=payload)]
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                                     cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.model = model


def test_score_appointment_parses_and_meters(monkeypatch):
    from app import agent
    async def fake_create(**kwargs):
        return _FakeMsg('{"score": 82, "band": "low", "reason": "2 prior no-shows"}')
    monkeypatch.setattr(agent, "client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    metered = []
    monkeypatch.setattr(practice_noshow.usage, "record_anthropic",
                        lambda *a, **k: metered.append((a, k)))

    out = asyncio.run(practice_noshow.score_appointment(
        {"scheduling_config": {}}, {"total_appts": 5, "no_shows": 2}, prior=80, tenant_id="t1"))

    assert out["score"] == 82
    # the model SAID "low" but 82 >= the high threshold — the tenant thresholds win
    assert out["band"] == "high"
    assert out["reason"] == "2 prior no-shows"
    assert metered and metered[0][1]["operation"] == "no_show_score"


def test_score_appointment_falls_back_to_prior_on_bad_output(monkeypatch):
    from app import agent
    async def fake_create(**kwargs):
        return _FakeMsg("not json at all")
    monkeypatch.setattr(agent, "client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(practice_noshow.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(practice_noshow.score_appointment(
        {}, {"total_appts": 0, "no_shows": 0}, prior=25, tenant_id="t1"))
    assert out["score"] == 25 and out["band"] == "low"       # prior is always a usable answer
    assert out["reason"]                                     # a deterministic reason is filled


# ── contact_no_show_history aggregate (no new table) ─────────────────────────
class _MultiTableClient:
    """A fake Supabase client that returns per-table canned rows and applies the
    filters contact_no_show_history / list_appointments_to_score actually use. insert/delete
    mutate the shared backing list so an add→list round-trip works on one client."""
    def __init__(self, tables):
        self._tables = tables            # {"bookings": [...], "email_log": [...]}
        self.updates = []

    def table(self, name):
        return _MultiQuery(name, self._tables.setdefault(name, []), self)


class _MultiQuery:
    def __init__(self, name, backing, parent):
        self._name = name
        self._backing = backing          # the shared list (insert/delete mutate this)
        self._rows = list(backing)       # a working copy for read filters
        self._parent = parent
        self._mode = "select"
        self._inserted = None

    def select(self, *a, **k): self._mode = "select"; return self
    def order(self, *a, **k): return self
    def limit(self, n): self._rows = self._rows[:n]; return self

    def update(self, payload):
        self._parent.updates.append((self._name, payload))
        # apply the update to the matched rows so a read-back sees it (jsonb merge tests)
        for r in self._rows:
            r.update(payload)
        return self

    def insert(self, payload):
        self._mode = "insert"
        row = dict(payload)
        row.setdefault("id", f"row-{len(self._backing) + 1}")
        self._backing.append(row)
        self._inserted = [dict(row)]      # a copy — callers may pop() the ciphertext off it
        return self

    def delete(self): self._mode = "delete"; return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self
    def gte(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col) or "") >= str(val)]
        return self
    def lte(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col) or "") <= str(val)]
        return self
    def is_(self, col, _null):
        self._rows = [r for r in self._rows if r.get(col) is None]
        return self

    def execute(self):
        if self._mode == "insert":
            return SimpleNamespace(data=self._inserted)
        if self._mode == "delete":
            for r in list(self._rows):
                if r in self._backing:
                    self._backing.remove(r)
            return SimpleNamespace(data=self._rows)
        return SimpleNamespace(data=self._rows)


def test_contact_no_show_history_aggregates_counts_and_engagement():
    now = datetime.now(timezone.utc)
    bookings = [
        {"tenant_id": "t1", "contact": "p@x.co", "status": "completed", "booking_date": "2026-01-10"},
        {"tenant_id": "t1", "contact": "p@x.co", "status": "no_show", "booking_date": "2026-03-05"},
        {"tenant_id": "t1", "contact": "p@x.co", "status": "confirmed", "booking_date": "2026-08-01"},  # future, ignored
    ]
    email_log = [
        {"tenant_id": "t1", "from_address": "p@x.co", "kind": "reminder",
         "created_at": (now - timedelta(hours=10)).isoformat()},
    ]
    client = _MultiTableClient({"bookings": bookings, "email_log": email_log})
    hist = db.contact_no_show_history("t1", "p@x.co", client=client)
    assert hist["total_appts"] == 2                          # settled only (completed + no_show)
    assert hist["no_shows"] == 1
    assert hist["last_no_show_at"] == "2026-03-05"
    assert hist["reminders_ignored"] is True                 # we chased, no inbound reply on record


def test_contact_no_show_history_empty_for_new_contact():
    client = _MultiTableClient({"bookings": [], "email_log": []})
    hist = db.contact_no_show_history("t1", "new@x.co", client=client)
    assert hist == {"total_appts": 0, "no_shows": 0, "last_no_show_at": None,
                    "avg_reply_latency_hours": None, "reminders_ignored": False}


# ── the scorer sweep: scores, saves, queues exactly one extra reminder ────────
def _confirmed_appt(start: datetime, **over):
    b = {"id": "b1", "contact": "p@x.co", "status": "confirmed", "summary": "Check-up",
         "booking_date": start.date().isoformat(), "window": start.strftime("%H:%M"),
         "no_show_scored_at": None, "extra_reminder_queued_at": None,
         "practitioner": "Dr Amara", "booking_ref": "BK-1", "thread_id": None}
    b.update(over)
    return b


def _wire_sweep(monkeypatch, appts, band="high"):
    monkeypatch.setattr(sn.db, "tenants_with_modules", lambda mods, **k: ["t1"])
    monkeypatch.setattr(sn.db, "list_appointments_to_score", lambda *a, **k: appts)
    monkeypatch.setattr(sn.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(sn.db, "contact_no_show_history",
                        lambda *a, **k: {"total_appts": 6, "no_shows": 4, "reminders_ignored": True,
                                         "avg_reply_latency_hours": None, "last_no_show_at": None})

    async def fake_score(cfg, features, prior, **k):
        return {"score": 85, "band": band, "reason": "4 prior no-shows"}
    monkeypatch.setattr(sn.practice_noshow, "score_appointment", fake_score)

    saved, marked, inserts, sent = [], [], [], []
    monkeypatch.setattr(sn.db, "save_no_show_score",
                        lambda bid, tid, s, band_, reason, **k: saved.append((bid, s, band_)))
    monkeypatch.setattr(sn.db, "mark_extra_reminder_queued", lambda bid, tid, **k: marked.append(bid))
    monkeypatch.setattr(sn.db, "insert_email_log",
                        lambda *a, **k: (inserts.append((a, k)) or ("log-1", True)))
    monkeypatch.setattr(sn.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sn.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(sn.mailbox, "send_mail", lambda *a, **k: sent.append(a))

    async def fake_compose(cfg, purpose, facts, channel="email", **kwargs):
        return SimpleNamespace(text="Just confirming your appointment — see you soon!",
                               usage={"input_tokens": 5}, model="fake")
    monkeypatch.setattr(sn.agent, "compose_outbound", fake_compose)
    return {"saved": saved, "marked": marked, "inserts": inserts, "sent": sent}


def test_sweep_scores_saves_and_queues_one_extra_for_high_in_window(monkeypatch):
    # start 4h ahead → inside the 3–6h extra-reminder window
    start = datetime.now(sn.DIARY_TZ) + timedelta(hours=4)
    appt = _confirmed_appt(start)
    calls = _wire_sweep(monkeypatch, [appt], band="high")

    out = asyncio.run(sn.run())

    assert out == {"scored": 1, "extra_reminders": 1}
    assert calls["saved"] == [("b1", 85, "high")]            # score persisted
    assert calls["marked"] == ["b1"]                         # extra reminder deduped
    _, kw = calls["inserts"][0]
    assert kw["kind"] == "reminder" and kw["status"] == "pending"


def test_sweep_dedupes_extra_reminder_on_rerun(monkeypatch):
    # the SAME appointment already has its extra reminder stamped → no second nudge
    start = datetime.now(sn.DIARY_TZ) + timedelta(hours=4)
    appt = _confirmed_appt(start, extra_reminder_queued_at="2026-07-05T09:00:00Z")
    calls = _wire_sweep(monkeypatch, [appt], band="high")

    out = asyncio.run(sn.run())
    assert out == {"scored": 1, "extra_reminders": 0}        # scored, but NOT re-nudged
    assert calls["marked"] == [] and calls["inserts"] == []


def test_sweep_no_extra_when_out_of_window(monkeypatch):
    # high band but the appointment is 24h out — outside the 3–6h window → score only
    start = datetime.now(sn.DIARY_TZ) + timedelta(hours=24)
    calls = _wire_sweep(monkeypatch, [_confirmed_appt(start)], band="high")
    out = asyncio.run(sn.run())
    assert out == {"scored": 1, "extra_reminders": 0}
    assert calls["marked"] == []


def test_sweep_no_extra_for_low_band(monkeypatch):
    start = datetime.now(sn.DIARY_TZ) + timedelta(hours=4)
    calls = _wire_sweep(monkeypatch, [_confirmed_appt(start)], band="low")
    out = asyncio.run(sn.run())
    assert out == {"scored": 1, "extra_reminders": 0}
    assert calls["marked"] == []


def test_sweep_isolates_a_failing_appointment(monkeypatch):
    start = datetime.now(sn.DIARY_TZ) + timedelta(hours=4)
    good = _confirmed_appt(start)
    bad = _confirmed_appt(start, id="b2", contact="q@x.co")
    calls = _wire_sweep(monkeypatch, [bad, good], band="high")

    # make the FIRST appointment blow up in save; the sweep must still score the second
    orig_save = calls["saved"]
    def flaky_save(bid, tid, s, band_, reason, **k):
        if bid == "b2":
            raise RuntimeError("boom")
        orig_save.append((bid, s, band_))
    monkeypatch.setattr(sn.db, "save_no_show_score", flaky_save)

    out = asyncio.run(sn.run())
    assert out["scored"] == 1                                # only the good one counted
    assert orig_save == [("b1", 85, "high")]


def test_sweep_gate_keys_off_the_practice_module(monkeypatch):
    seen = {}
    def _gate(mods, **k):
        seen["mods"] = mods
        return []
    monkeypatch.setattr(sn.db, "tenants_with_modules", _gate)
    out = asyncio.run(sn.run())
    assert seen["mods"] == ("practice",)
    assert out == {"scored": 0, "extra_reminders": 0}


# ── the risk panel renders chips + reasons; the manual /remind route ─────────
@pytest.fixture
def practice_client(monkeypatch):
    import app.dashboard as dashboard
    import app.practice_routes as pr
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"practice"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(pr.db, "module_active", lambda *a, **k: True)
    monkeypatch.setattr(pr.db, "list_appointments_due_recall", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), pr
    app.dependency_overrides.clear()


def test_risk_panel_renders_chips_and_reasons(practice_client, monkeypatch):
    client, pr = practice_client
    monkeypatch.setattr(pr.db, "list_confirmed_appointments_between", lambda *a, **k: [
        {"id": "b1", "contact": "p@x.co", "summary": "Check-up", "practitioner": "Dr Amara",
         "booking_date": "2026-07-05", "window": "09:00", "no_show_score": 82,
         "no_show_band": "high", "no_show_reason": "2 prior no-shows, booked 6 weeks out",
         "extra_reminder_queued_at": None},
        {"id": "b2", "contact": "q@x.co", "summary": "Hygiene", "practitioner": None,
         "booking_date": "2026-07-05", "window": "10:00", "no_show_score": 15,
         "no_show_band": "low", "no_show_reason": "reliable attender",
         "extra_reminder_queued_at": None},
    ])
    r = client.get("/app/practice")
    assert r.status_code == 200
    assert "No-show risk" in r.text
    assert "2 prior no-shows, booked 6 weeks out" in r.text   # the top reason
    assert "high" in r.text and "bg-red-100" in r.text        # the red chip
    # highest score first: b1 (82) appears before b2 (15)
    assert r.text.index("Dr Amara") < r.text.index("Hygiene")


def test_manual_remind_queues_and_dedupes(practice_client, monkeypatch):
    client, pr = practice_client
    appt = {"id": "b1", "contact": "p@x.co", "summary": "Check-up",
            "booking_date": "2026-07-05", "window": "09:00", "extra_reminder_queued_at": None}
    monkeypatch.setattr(pr.db, "get_booking", lambda *a, **k: appt)
    monkeypatch.setattr(pr.db, "effective_config", lambda *a, **k: {"auto_send": False})
    fired = []
    async def fake_queue(tid, cfg, auto, b):
        fired.append(b["id"]); return "drafted"
    monkeypatch.setattr(pr.score_no_shows, "queue_extra_reminder", fake_queue)

    r = client.post("/app/practice/appointments/b1/remind", follow_redirects=False)
    assert r.status_code == 303 and "msg=reminded" in r.headers["location"]
    assert fired == ["b1"]

    # a SECOND click on an already-nudged appointment is a no-op (dedupe)
    appt["extra_reminder_queued_at"] = "2026-07-05T09:00:00Z"
    fired.clear()
    r2 = client.post("/app/practice/appointments/b1/remind", follow_redirects=False)
    assert r2.status_code == 303 and "msg=already" in r2.headers["location"]
    assert fired == []                                        # never queued twice


# ═══════════════════════════════════════════════════════════════════════════════
#  P2 — the 48/24/2h reminder cycle (docs/os/practice.md §5, §6.2)
# ═══════════════════════════════════════════════════════════════════════════════

# ── the reminder_cycle config reader (default + override) ─────────────────────
def test_reminder_cycle_defaults_to_48_24_2():
    cyc = practice_config.reminder_cycle(None)
    assert cyc == {"leads_hours": [48, 24, 2], "ask_confirm": True}


def test_reminder_cycle_override_cleans_and_sorts_desc():
    cyc = practice_config.reminder_cycle(
        {"reminder_cycle": {"leads_hours": ["2", 24, "", "bad", 24, 72], "ask_confirm": False}})
    assert cyc["leads_hours"] == [72, 24, 2]       # deduped, sorted desc, blanks/invalid dropped
    assert cyc["ask_confirm"] is False


def test_reminder_cycle_empty_leads_falls_back_to_default():
    cyc = practice_config.reminder_cycle({"reminder_cycle": {"leads_hours": []}})
    assert cyc["leads_hours"] == [48, 24, 2]        # never silently no reminders


# ── the per-stage window query + per-stage jsonb dedupe ──────────────────────
def test_list_appointments_for_reminder_stage_dedupes_by_stage():
    rows = [
        {"id": "b1", "tenant_id": "t1", "contact": "p@x.co", "status": "confirmed",
         "booking_date": "2026-07-06", "reminder_stages_sent": {}},        # kept — no stage sent
        {"id": "b2", "tenant_id": "t1", "contact": "q@x.co", "status": "confirmed",
         "booking_date": "2026-07-06", "reminder_stages_sent": {"24h": "x"}},  # dropped — 24h sent
        {"id": "b3", "tenant_id": "t1", "contact": "r@x.co", "status": "confirmed",
         "booking_date": "2026-07-06", "reminder_stages_sent": {"48h": "x"}},  # kept — other stage
        {"id": "b4", "tenant_id": "t1", "contact": "", "status": "confirmed",
         "booking_date": "2026-07-06", "reminder_stages_sent": {}},        # dropped — no email
    ]
    client = _MultiTableClient({"bookings": rows})
    got = db.list_appointments_for_reminder_stage(
        "t1", "24h", "2026-07-06T00:00:00", "2026-07-06T23:59:59", client=client)
    assert [b["id"] for b in got] == ["b1", "b3"]


def test_mark_reminder_stage_sent_merges_into_jsonb():
    rows = [{"id": "b1", "tenant_id": "t1", "reminder_stages_sent": {"48h": "earlier"}}]
    client = _MultiTableClient({"bookings": rows})
    db.mark_reminder_stage_sent("b1", "24h", "t1", client=client)
    # the update merged 24h in WITHOUT dropping the existing 48h stamp
    _, payload = client.updates[-1]
    merged = payload["reminder_stages_sent"]
    assert merged["48h"] == "earlier" and "24h" in merged


# ── the job fires each stage once + confirm-ask on 24h/2h + auto-send ────────
def _confirmed_reminder_appt(booking_date, window, **over):
    b = {"id": "b1", "contact": "p@x.co", "status": "confirmed", "summary": "Check-up",
         "booking_date": booking_date, "window": window, "practitioner": "Dr Amara",
         "booking_ref": "BK-1", "thread_id": None, "reminder_stages_sent": {}}
    b.update(over)
    return b


def _stub_ar_compose(monkeypatch, sink):
    async def fake_compose(cfg, purpose, facts, channel="email", **kwargs):
        sink.append(purpose)
        return SimpleNamespace(text="See you soon!", usage={"input_tokens": 5}, model="fake")
    monkeypatch.setattr(ar.agent, "compose_outbound", fake_compose)
    # default: no SMS routing unless a test overrides it (email stays the route)
    monkeypatch.setattr(ar.sms, "preferred_sms_destination", lambda t, c: None)


def test_appointment_reminder_confirm_ask_on_24h_and_2h_not_48h():
    # the "reply to confirm" tail is appended on the closer touches, not the 48h heads-up
    assert "reply to confirm" not in ar._purpose_for("48h", ask_confirm=True).lower()
    assert "reply to confirm" in ar._purpose_for("24h", ask_confirm=True).lower()
    assert "reply to confirm" in ar._purpose_for("2h", ask_confirm=True).lower()
    # and never when the tenant turned ask_confirm off
    assert "reply to confirm" not in ar._purpose_for("2h", ask_confirm=False).lower()


def test_appointment_reminder_run_fires_each_stage_once(monkeypatch):
    # one appointment 48h ahead: the 48h stage fires, the 24h/2h windows don't match yet
    now = datetime.now(ar.DIARY_TZ)
    start = now + timedelta(hours=48, minutes=10)
    appt = _confirmed_reminder_appt(start.date().isoformat(), start.strftime("%H:%M"))
    purposes = []
    _stub_ar_compose(monkeypatch, purposes)

    monkeypatch.setattr(ar.db, "tenants_with_modules", lambda mods, **k: ["t1"])
    monkeypatch.setattr(ar.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(ar.db, "get_scheduling_config", lambda *a, **k: {})   # default cadence
    stage_seen = {}

    def _stage_query(tid, stage, ws, we, **k):
        # return the appt only for the stage whose window actually contains it
        stage_seen.setdefault(stage, 0)
        return [appt]
    monkeypatch.setattr(ar.db, "list_appointments_for_reminder_stage", _stage_query)
    marked = []
    monkeypatch.setattr(ar.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(ar.db, "mark_reminder_stage_sent",
                        lambda bid, stage, tid, **k: marked.append(stage))
    monkeypatch.setattr(ar.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ar.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(ar.run())
    # exactly one reminder went out — the 48h stage (the only window the appt falls in)
    assert out == {"drafted": 1, "sent": 0}
    assert marked == ["48h"]
    assert "reply to confirm" not in purposes[0].lower()     # 48h has no confirm ask


def test_appointment_reminder_auto_sends_for_full_auto(monkeypatch):
    now = datetime.now(ar.DIARY_TZ)
    start = now + timedelta(hours=2, minutes=5)               # inside the 2h window
    appt = _confirmed_reminder_appt(start.date().isoformat(), start.strftime("%H:%M"))
    purposes = []
    _stub_ar_compose(monkeypatch, purposes)
    monkeypatch.setattr(ar.db, "tenants_with_modules", lambda mods, **k: ["t1"])
    monkeypatch.setattr(ar.db, "effective_config", lambda *a, **k: {"auto_send": True})
    monkeypatch.setattr(ar.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(ar.db, "list_appointments_for_reminder_stage", lambda *a, **k: [appt])
    monkeypatch.setattr(ar.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(ar.db, "mark_reminder_stage_sent", lambda *a, **k: None)
    monkeypatch.setattr(ar.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(ar.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ar.usage, "record_anthropic", lambda *a, **k: None)
    sent = []

    async def fake_send(*a, **k):
        sent.append(a)
    monkeypatch.setattr(ar.mailbox, "send_mail", fake_send)

    out = asyncio.run(ar.run())
    # 3 stages configured, but only the 2h window contains the appt → one auto-send
    assert out["sent"] == 1 and out["drafted"] == 0
    assert len(sent) == 1
    assert "confirm" in purposes[-1].lower()                 # the 2h touch asks to confirm


def test_appointment_reminder_gate_keys_off_practice_module(monkeypatch):
    seen = {}

    def _gate(mods, **k):
        seen["mods"] = mods
        return []
    monkeypatch.setattr(ar.db, "tenants_with_modules", _gate)
    out = asyncio.run(ar.run())
    assert seen["mods"] == ("practice",)
    assert out == {"drafted": 0, "sent": 0}


# ── SMS channel routing (docs/modules/module-sms-channel.md §6) ──────────────────────
# Mirrors the send_reminders routing tests (tests/test_sms_channel.py): an
# SMS-preferring contact gets the reminder texted (SMS register + reply-C confirm
# ask on the confirm-asking touches), email stays the default.
_AR_PHONE = "+447700900123"


def _stub_ar_io(monkeypatch, state):
    """Capture compose/log/send for one _remind_one call, mirroring the
    send_reminders SMS-routing harness."""
    async def _compose(cfg, purpose, facts, channel="email", **kwargs):
        state["composed"].append({"facts": facts, "channel": channel})
        return SimpleNamespace(text="See you soon!", usage={}, model="m")
    monkeypatch.setattr(ar.agent, "compose_outbound", _compose)
    monkeypatch.setattr(ar.db, "insert_email_log",
                        lambda *a, **k: (state["logs"].append({"args": a, **k}) or ("log-9", True)))
    monkeypatch.setattr(ar.db, "mark_reminder_stage_sent", lambda *a, **k: None)
    monkeypatch.setattr(ar.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(ar.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ar.usage, "record_anthropic", lambda *a, **k: None)

    async def _send_sms(tenant_id, to, body, **k):
        state["sms"].append(to)
        return "SM_x"
    monkeypatch.setattr(ar.sms, "send_sms", _send_sms)

    async def _send_mail(tenant_id, to, subject, body, **k):
        state["mail"].append(to)
    monkeypatch.setattr(ar.mailbox, "send_mail", _send_mail)


def test_appointment_reminder_routes_to_sms_for_sms_preferring_contact(monkeypatch):
    state = {"composed": [], "logs": [], "sms": [], "mail": []}
    _stub_ar_io(monkeypatch, state)
    monkeypatch.setattr(ar.sms, "preferred_sms_destination", lambda t, c: _AR_PHONE)
    appt = _confirmed_reminder_appt("2026-07-06", "14:00", thread_id="th-1")
    outcome = asyncio.run(
        ar._remind_one("t1", {"auto_send": True}, True, "2h", True, appt))
    assert outcome == "sent"
    (c,) = state["composed"]
    assert c["channel"] == "sms" and "reply C" in c["facts"]     # SMS register + confirm ask
    (row,) = state["logs"]
    assert row["channel"] == "sms" and row["thread_id"] == f"sms:{_AR_PHONE}"
    assert row["args"][1] == _AR_PHONE                           # from_address = the phone
    assert state["sms"] == [_AR_PHONE] and not state["mail"]     # texted, not emailed


def test_appointment_reminder_stays_on_email_by_default(monkeypatch):
    state = {"composed": [], "logs": [], "sms": [], "mail": []}
    _stub_ar_io(monkeypatch, state)
    monkeypatch.setattr(ar.sms, "preferred_sms_destination", lambda t, c: None)
    appt = _confirmed_reminder_appt("2026-07-06", "14:00", thread_id="th-1")
    asyncio.run(ar._remind_one("t1", {"auto_send": True}, True, "2h", True, appt))
    (c,) = state["composed"]
    assert c["channel"] == "email" and "reply C" not in c["facts"]
    (row,) = state["logs"]
    assert row["channel"] == "email" and row["thread_id"] == "th-1"
    assert state["mail"] == ["p@x.co"] and not state["sms"]


def test_appointment_reminder_sms_48h_heads_up_has_no_confirm_ask(monkeypatch):
    # the reply-C ask rides only the confirm-asking touches — the 48h SMS omits it,
    # matching the email side where _purpose_for('48h') carries no confirm tail
    state = {"composed": [], "logs": [], "sms": [], "mail": []}
    _stub_ar_io(monkeypatch, state)
    monkeypatch.setattr(ar.sms, "preferred_sms_destination", lambda t, c: _AR_PHONE)
    appt = _confirmed_reminder_appt("2026-07-06", "14:00")
    asyncio.run(ar._remind_one("t1", {"auto_send": False}, False, "48h", True, appt))
    (c,) = state["composed"]
    assert c["channel"] == "sms" and "reply C" not in c["facts"]


# ═══════════════════════════════════════════════════════════════════════════════
#  P4 — encrypted medical notes + patient summary + Today board (§4.3, §5, §12.2)
# ═══════════════════════════════════════════════════════════════════════════════

# ── the encrypt(str) ↔ bytea round-trip (the crux) ───────────────────────────
def test_medical_note_encrypt_store_decrypt_round_trips_exact_plaintext():
    plaintext = "Allergic to penicillin; on warfarin. 🩺 café notes"
    rows: list = []
    client = _MultiTableClient({"practice_medical_notes": rows})

    # add: encrypt → bytea-encode → insert; the returned row carries NO ciphertext
    added = db.add_medical_note("t1", "c1", plaintext, kind="allergy", client=client)
    assert "note_ciphertext" not in added
    # the row that hit the db IS ciphertext (a bytea \x-hex literal), not the plaintext
    stored = rows[0]["note_ciphertext"]
    assert stored.startswith("\\x") and plaintext not in stored

    # list: read back the SAME row → decrypt to the exact plaintext
    rows[0].setdefault("id", "n1")
    rows[0].setdefault("created_at", "2026-07-05")
    rows[0].setdefault("note_kind", "allergy")
    got = db.list_medical_notes("t1", "c1", client=client)
    assert len(got) == 1
    assert got[0]["text"] == plaintext                       # exact round-trip
    assert got[0]["decrypt_error"] is False
    assert "note_ciphertext" not in got[0]                   # never handed back


def test_list_medical_notes_surfaces_decrypt_error_not_crash(monkeypatch):
    # simulate the known local/prod key mismatch: decrypt RAISES on a v1: token
    rows = [{"id": "n1", "tenant_id": "t1", "contact_id": "c1",
             "note_ciphertext": db._bytea_encode("v1:garbage"),
             "note_kind": "note", "created_at": "2026-07-05"}]
    client = _MultiTableClient({"practice_medical_notes": rows})

    def boom(_stored):
        raise RuntimeError("TOKEN_ENCRYPTION_KEY mismatch")
    monkeypatch.setattr(db.crypto, "decrypt", boom)

    got = db.list_medical_notes("t1", "c1", client=client)   # must NOT raise
    assert len(got) == 1
    assert got[0]["decrypt_error"] is True
    assert got[0]["text"] == ""                              # no ciphertext leaked, page survives


# ── patient_history_summary: CRM fields + redacted line, NEVER raw notes ─────
def _summary_client(custom_data, notes, defs, appts=None):
    return _MultiTableClient({
        "crm_contacts": [{"id": "c1", "tenant_id": "t1", "email": "p@x.co",
                          "name": "Pat", "custom_data": custom_data,
                          "crm_companies": {}}],
        "crm_field_definitions": [{"tenant_id": "t1", **d} for d in defs],
        "practice_medical_notes": [{"tenant_id": "t1", "contact_id": "c1", **n} for n in notes],
        "bookings": [{"tenant_id": "t1", "contact": "p@x.co", **a} for a in (appts or [])],
    })


def test_patient_history_summary_includes_crm_fields_and_redacted_line(monkeypatch):
    defs = [{"entity": "contact", "key": "insurer", "label": "Insurer"},
            {"entity": "contact", "key": "recall", "label": "Recall cadence"}]
    custom = {"insurer": "Denplan", "recall": "6 months"}
    notes = [{"note_kind": "allergy"}]
    appts = [{"booking_date": "2026-01-10", "status": "completed"}]
    client = _summary_client(custom, notes, defs, appts)

    summary = db.patient_history_summary("t1", "c1", client=client)
    assert "Insurer: Denplan" in summary
    assert "Recall cadence: 6 months" in summary
    assert "last visit 2026-01-10" in summary
    # the redacted clinical line — presence only, NEVER the note content
    assert "has recorded allergies/medical notes on file" in summary


def test_patient_history_summary_never_leaks_raw_note_text(monkeypatch):
    # even though the contact HAS notes, the summary carries none of their (would-be) text.
    # patient_history_summary only ever reads note_kind, never the ciphertext/plaintext.
    defs = []
    client = _summary_client({}, [{"note_kind": "condition"}], defs)
    summary = db.patient_history_summary("t1", "c1", client=client)
    assert "medical notes on file" in summary
    # no way for raw text to appear — we never called decrypt in this path
    assert "penicillin" not in summary and "warfarin" not in summary


# ── build_crm_context: unchanged for non-practice, redacted-only for practice ─
def test_build_crm_context_unchanged_for_non_practice_contact():
    # a non-practice caller passes NO patient_summary → byte-identical to before
    contact = {"name": "Jo", "email": "jo@x.co", "custom_data": {}, "crm_companies": {}}
    baseline = crm_intel.build_crm_context(contact, [], [], [])
    with_default = crm_intel.build_crm_context(contact, [], [], [], patient_summary="")
    assert baseline == with_default


def test_build_crm_context_appends_only_the_redacted_summary_for_practice():
    contact = {"name": "Pat", "email": "p@x.co", "custom_data": {}, "crm_companies": {}}
    redacted = "Insurer: Denplan; last visit 2026-01-10; has recorded medical notes on file"
    ctx = crm_intel.build_crm_context(contact, [], [], [], patient_summary=redacted)
    assert "Patient history: " + redacted in ctx
    # the raw clinical text is NOT present (only the redacted line ever rides in)
    assert "penicillin" not in ctx


# ── the Today board renders the three cards + outcome buttons ─────────────────
def test_today_board_renders_three_cards_and_outcome_buttons(practice_client, monkeypatch):
    client, pr = practice_client
    monkeypatch.setattr(pr.db, "list_confirmed_appointments_between", lambda tid, f, t, **k: (
        [{"id": "b1", "contact": "p@x.co", "summary": "Check-up", "practitioner": "Dr Amara",
          "booking_date": f, "window": "09:00", "no_show_score": 30, "no_show_band": "low",
          "no_show_reason": "reliable", "reminder_stages_sent": {"24h": "x"},
          "extra_reminder_queued_at": None}] if f == t else
        # today+tomorrow risk-panel call (f != t) returns the same row
        [{"id": "b1", "contact": "p@x.co", "summary": "Check-up", "practitioner": "Dr Amara",
          "booking_date": f, "window": "09:00", "no_show_score": 30, "no_show_band": "low",
          "no_show_reason": "reliable", "extra_reminder_queued_at": None}]
    ))
    r = client.get("/app/practice")
    assert r.status_code == 200
    assert "Today's appointments" in r.text                  # card 1
    assert "No-show risk" in r.text                          # card 2
    assert "Recall queue" in r.text                          # card 3
    # outcome buttons on the day view
    assert 'value="completed"' in r.text and 'value="no_show"' in r.text
    assert 'value="cancelled"' in r.text
    assert "confirmed" in r.text                             # confirmed? state from 24h stage


# ── the notes routes: add / list / delete with CSRF + practice gate ──────────
def test_notes_routes_add_list_delete(practice_client, monkeypatch):
    client, pr = practice_client
    added, deleted = [], []
    monkeypatch.setattr(pr.db, "add_medical_note",
                        lambda tid, cid, text, **k: added.append((cid, text, k.get("kind"))) or {"id": "n1"})
    monkeypatch.setattr(pr.db, "list_medical_notes",
                        lambda tid, cid, **k: [{"id": "n1", "note_kind": "allergy",
                                                "text": "peanut allergy", "decrypt_error": False,
                                                "created_at": "2026-07-05"}])
    monkeypatch.setattr(pr.db, "delete_medical_note",
                        lambda tid, cid, nid, **k: deleted.append((cid, nid)))

    # add (POST + CSRF, practice gate) → redirect back to the appointment
    r = client.post("/app/practice/patients/c1/notes",
                    data={"text": "peanut allergy", "kind": "allergy", "booking_id": "b1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/app/practice/appointments/b1" in r.headers["location"]
    assert added == [("c1", "peanut allergy", "allergy")]

    # list (JSON) → the decrypted note
    j = client.get("/app/practice/patients/c1/notes").json()
    assert j["notes"][0]["text"] == "peanut allergy"

    # delete (POST-with-delete + CSRF) → gone
    r = client.post("/app/practice/patients/c1/notes/n1/delete",
                    data={"booking_id": "b1"}, follow_redirects=False)
    assert r.status_code == 303
    assert deleted == [("c1", "n1")]


def test_add_medical_note_ignores_blank_text(practice_client, monkeypatch):
    client, pr = practice_client
    added = []
    monkeypatch.setattr(pr.db, "add_medical_note", lambda *a, **k: added.append(a) or {})
    r = client.post("/app/practice/patients/c1/notes",
                    data={"text": "   ", "kind": "note", "booking_id": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert added == []                                       # blank note never stored
