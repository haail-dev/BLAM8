"""Smart booking Phase 3 — the pre-expiry offer nudge (app/jobs/nudge_offered_slots).

What this pins:
  - one nudge per CUSTOMER covering their whole offer set (not one per slot),
    quoting each stored release deadline in the shared code-written sentence
  - the three-beat outbound contract: pending email_log row (kind='offer_nudge')
    → dedupe stamp at draft time → auto-send ONLY for full-auto tenants, wrapped
    in the AI disclosure
  - the tenant kill-switch (scheduling_config.offer_nudge_enabled, default ON)
  - the stop condition: a customer who replied since the offers went out is
    stamped but never chased (the reply drafter owns that thread)
  - db.list_offers_to_nudge query shape: live offered rows entering the lead
    window, never nudged, old enough that the offer email isn't still fresh

Run: python -m pytest tests/test_offer_nudge.py -q
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.jobs import nudge_offered_slots as job
from app.scheduling import DIARY_TZ

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)


def _offer(i, contact="ops@acme.co.uk", hours_left=3):
    return {"id": f"off-{i}", "contact": contact, "booking_date": "2026-06-18",
            "window": "10:00" if i == 1 else "14:00", "booking_ref": f"BK-{i}",
            "summary": "Boiler service", "thread_id": "th-9",
            "created_at": (NOW - timedelta(hours=20)).isoformat(),
            "expires_at": (NOW + timedelta(hours=hours_left)).isoformat()}


@pytest.fixture
def env(monkeypatch):
    state = {"tenants": ["t1"], "sched_cfg": {"offer_nudge_enabled": True},
             "offers": [_offer(1), _offer(2)], "cfg": {"auto_send": False},
             "inbound_since": False,
             "logged": [], "nudged": [], "sent_mail": [], "marked_sent": [], "events": []}
    monkeypatch.setattr(job.db, "tenants_with_modules", lambda mods: state["tenants"])
    monkeypatch.setattr(job.db, "get_scheduling_config", lambda tid, **k: state["sched_cfg"])
    monkeypatch.setattr(job.db, "list_offers_to_nudge", lambda tid, end, age, **k: state["offers"])
    monkeypatch.setattr(job.db, "offered_bookings_for_contact",
                        lambda tid, contact: [b for b in state["offers"] if b["contact"] == contact])
    monkeypatch.setattr(job.db, "effective_config", lambda tid, mid, **k: state["cfg"])
    monkeypatch.setattr(job.db, "has_inbound_from_since",
                        lambda tid, to, since: state["inbound_since"])

    def insert_log(tid, addr, subject, body_in, draft, **kw):
        state["logged"].append({"to": addr, "subject": subject, "body_in": body_in,
                                "draft": draft, **kw})
        return "log-1", True
    monkeypatch.setattr(job.db, "insert_email_log", insert_log)
    monkeypatch.setattr(job.db, "mark_offers_nudged",
                        lambda ids, tid, **k: state["nudged"].append(tuple(ids)))
    monkeypatch.setattr(job.db, "log_agent_event",
                        lambda *a, **k: state["events"].append((a, k)))
    monkeypatch.setattr(job.db, "mark_email_log_sent",
                        lambda log_id, tid, **k: state["marked_sent"].append(log_id))
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)

    async def compose(cfg, purpose, facts, **kw):
        state["composed"] = {"purpose": purpose, "facts": facts, **kw}
        return SimpleNamespace(text="Still want one of these?", model="m", usage=None)
    monkeypatch.setattr(job.agent, "compose_outbound", compose)

    async def send_mail(tid, to, subject, body, **kw):
        state["sent_mail"].append({"to": to, "subject": subject, "body": body})
    monkeypatch.setattr(job.mailbox, "send_mail", send_mail)
    monkeypatch.setattr(job.disclosure, "wrap", lambda cfg, text, **k: text + "\n--AI")
    return state


def test_one_nudge_covers_the_whole_offer_set(env):
    out = asyncio.run(job.run())
    assert out == {"drafted": 1, "sent": 0}
    assert len(env["logged"]) == 1                      # one email, two slots
    row = env["logged"][0]
    assert row["kind"] == "offer_nudge" and row["status"] == "pending"
    assert row["thread_id"] == "th-9"
    facts = env["composed"]["facts"]
    assert "BK-1" in facts and "BK-2" in facts
    assert "10:00" in facts and "14:00" in facts
    # Equal deadlines share a single sentence, in diary time.
    assert "1pm" in facts
    assert "I'm holding these for you until" in row["draft"]
    # Dedupe stamped for both rows at draft time, nothing auto-sent.
    assert env["nudged"] == [("off-1", "off-2")]
    assert env["sent_mail"] == [] and env["marked_sent"] == []


def test_auto_tenant_sends_with_disclosure(env):
    env["cfg"] = {"auto_send": True}
    out = asyncio.run(job.run())
    assert out == {"drafted": 0, "sent": 1}
    assert env["sent_mail"][0]["body"].endswith("--AI")   # disclosure-wrapped
    assert env["marked_sent"] == ["log-1"]


def test_toggle_off_skips_the_tenant_entirely(env):
    env["sched_cfg"] = {"offer_nudge_enabled": False}
    out = asyncio.run(job.run())
    assert out == {"drafted": 0, "sent": 0}
    assert env["logged"] == [] and "composed" not in env


def test_missing_toggle_defaults_on(env):
    env["sched_cfg"] = {}                                 # pre-migration row
    assert asyncio.run(job.run()) == {"drafted": 1, "sent": 0}


def test_replied_customer_is_stamped_but_never_chased(env):
    env["inbound_since"] = True
    out = asyncio.run(job.run())
    assert out == {"drafted": 0, "sent": 0}
    assert env["logged"] == []
    assert env["nudged"] == [("off-1", "off-2")]          # won't re-check next tick


def test_two_contacts_get_two_nudges(env):
    env["offers"] = [_offer(1), _offer(2, contact="jo@other.co.uk")]
    out = asyncio.run(job.run())
    assert out == {"drafted": 2, "sent": 0}
    assert {r["to"] for r in env["logged"]} == {"ops@acme.co.uk", "jo@other.co.uk"}


def test_list_offers_to_nudge_query_shape():
    class Fake:
        def __init__(self):
            self.filters = []

        def table(self, name):
            self.filters.append(("table", name)); return self

        def select(self, cols):
            return self

        def eq(self, col, val):
            self.filters.append((col, val)); return self

        def is_(self, col, val):
            self.filters.append(("is:" + col, val)); return self

        def gt(self, col, val):
            self.filters.append(("gt:" + col, val)); return self

        def lte(self, col, val):
            self.filters.append(("lte:" + col, val)); return self

        def order(self, col):
            return self

        def execute(self):
            return type("Res", (), {"data": [
                {"id": "a", "contact": "x@y.co"},        # reachable → kept
                {"id": "b", "contact": "front desk"},    # unreachable → dropped
            ]})()

    fake = Fake()
    rows = db.list_offers_to_nudge("t1", "END", "AGE", client=fake)
    assert [r["id"] for r in rows] == ["a"]
    assert ("status", "offered") in fake.filters
    assert ("is:offer_nudged_at", "null") in fake.filters
    assert ("lte:expires_at", "END") in fake.filters      # inside the lead window
    assert any(f[0] == "gt:expires_at" for f in fake.filters)   # still live
    assert ("lte:created_at", "AGE") in fake.filters      # offer old enough to chase


def test_mixed_deadline_chase_includes_later_siblings_once(env, monkeypatch):
    env["offers"] = [_offer(1, hours_left=2), _offer(2, hours_left=12)]
    monkeypatch.setattr(job.db, "list_offers_to_nudge", lambda *a, **k: env["offers"][:1])
    out = asyncio.run(job.run())
    assert out == {"drafted": 1, "sent": 0}
    expected = job.offer_deadlines.deadline_line(env["offers"])
    assert expected in env["logged"][0]["draft"]
    assert "BK-1" in env["composed"]["facts"] and "BK-2" in env["composed"]["facts"]
    assert env["nudged"] == [("off-1", "off-2")]
