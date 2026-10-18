"""Field Service Phase 3 — recurring service reminders (docs/modules/module-field-service.md D5).

Pins the date math, the specialist's rebook framing, and the daily job's compose →
queue → dedupe loop (no network: the client + db are stubbed).

Run: python -m pytest tests/test_fsm_reminders.py -q
"""
import asyncio
from datetime import date
from types import SimpleNamespace

from app import db
from app.agents import field_service as fs
from app.jobs import send_service_reminders as ssr


# ── date math ─────────────────────────────────────────────────────────────────
def test_add_months_basic_and_day_clamp():
    assert db._add_months(date(2026, 7, 2), 12) == date(2027, 7, 2)
    assert db._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)   # clamp to Feb
    assert db._add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)  # year roll


# ── the specialist's rebook framing ───────────────────────────────────────────
def test_outbound_reminder_frames_rebook_never_engineer_never_a_date():
    facts = fs.outbound_reminder({
        "job_type": "Boiler service", "service_due_date": "2027-07-02",
        "booking_date": "2026-07-02", "booking_ref": "BK-1001"})
    assert "Boiler service" in facts
    assert "2027-07-02" in facts and "2026-07-02" in facts
    assert "rebook" in facts.lower()
    # never names the engineer, never promises a slot
    assert "engineer" not in facts.lower()
    assert "do not promise" in facts.lower() or "do NOT promise".lower() in facts.lower()


# ── the daily job: compose → queue → dedupe ────────────────────────────────────
class _Result(SimpleNamespace):
    pass


def _stub_compose(monkeypatch):
    async def fake_compose(cfg, purpose, facts, channel="email", **kwargs):
        return _Result(text="Hi — you're due your annual service, shall we rebook?",
                       usage={"input_tokens": 5}, model="fake-model")
    monkeypatch.setattr(ssr.agent, "compose_outbound", fake_compose)


def test_remind_one_drafts_and_dedupes(monkeypatch):
    _stub_compose(monkeypatch)
    calls = {"insert": [], "marked": [], "events": [], "sent": []}
    monkeypatch.setattr(ssr.db, "insert_email_log",
                        lambda *a, **k: (calls["insert"].append((a, k)) or ("log-1", True)))
    monkeypatch.setattr(ssr.db, "mark_service_reminded",
                        lambda bid, tid, **k: calls["marked"].append(bid))
    monkeypatch.setattr(ssr.db, "log_agent_event", lambda *a, **k: calls["events"].append((a, k)))
    monkeypatch.setattr(ssr.usage, "record_anthropic", lambda *a, **k: None)

    job = {"id": "b1", "contact": "cust@x.co", "job_type": "Boiler service",
           "service_due_date": "2027-07-02", "booking_date": "2026-07-02",
           "booking_ref": "BK-1001", "thread_id": None}
    outcome = asyncio.run(ssr._remind_one("t1", {"auto_send": False}, False, job))

    assert outcome == "drafted"
    assert calls["marked"] == ["b1"]                       # deduped
    # queued as a fresh outbound 'reminder' (so the approve-send uses send_mail, not reply)
    _, kw = calls["insert"][0]
    assert kw["kind"] == "reminder" and kw["status"] == "pending"
    assert calls["sent"] == []                             # draft-only tenant never auto-sends


def test_run_picks_due_jobs_per_fsm_tenant(monkeypatch):
    _stub_compose(monkeypatch)
    monkeypatch.setattr(ssr.db, "tenants_with_modules", lambda mods, **k: ["t1"])
    monkeypatch.setattr(ssr.db, "effective_config", lambda *a, **k: {"auto_send": False})
    due = [{"id": "b1", "contact": "a@x.co", "job_type": "Service",
            "service_due_date": "2026-07-10", "booking_date": "2025-07-10",
            "booking_ref": "BK-9", "thread_id": None}]
    seen = {}

    def _due(tid, cutoff, **k):
        seen["cutoff"] = cutoff
        return due
    monkeypatch.setattr(ssr.db, "list_jobs_due_service", _due)
    monkeypatch.setattr(ssr.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(ssr.db, "mark_service_reminded", lambda *a, **k: None)
    monkeypatch.setattr(ssr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ssr.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(ssr.run())
    assert out["drafted"] == 1 and out["sent"] == 0
    assert seen["cutoff"]   # a cutoff (today + lead) was computed and passed to the query
