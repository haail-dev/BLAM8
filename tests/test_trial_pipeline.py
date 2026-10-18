"""Trial enforcement in the drafting pipeline (app/pipeline.draft_for_email).

Pins the wiring around the trial gate:
  * gate OFF (the launch default) → byte-identical to before: no trial read, no
    counter write, the draft always proceeds
  * gate ON + live trial → draft proceeds AND exactly one email is counted per
    NEW draft (a duplicate webhook delivery counts zero — no double-charge)
  * gate ON + expired trial → an automatic caller stops (returns None, no model
    call), a human-initiated draft raises TrialLimitReached (→ billing CTA)
  * a converted (subscribed) tenant is never counted
  * two fresh emails ⇒ two atomic increments (no undercount)

Run: python -m pytest tests/test_trial_pipeline.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import agent, db, pipeline, trial, usage
from app import knowledge as kb
from app.config import settings

EMAIL = {"from_address": "ops@acme.co.uk", "subject": "Quote", "body": "Friday?"}


def _tenant(status="none", days_ago=1, used=0):
    return {"subscription_status": status, "trial_emails_used": used, "trial_status": "active",
            "trial_start_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
            "industry": "logistics"}


class Env:
    """Patch every collaborator of draft_for_email; record draft calls + increments."""
    def __init__(self, monkeypatch, tenant, created=True, gate=True):
        self.drafted = 0
        self.increments = []
        monkeypatch.setattr(settings, "require_subscription_gate", gate)

        async def fake_draft(cfg, email, **kw):
            self.drafted += 1
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_retrieve(*a, **k):
            return []

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
        monkeypatch.setattr(kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(kb, "render_for_prompt", lambda *a, **k: None)
        monkeypatch.setattr(db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "own_mailbox_addresses", lambda *a, **k: set())
        monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(db, "get_tenant", lambda *a, **k: tenant)
        monkeypatch.setattr(db, "effective_config", lambda *a, **k: {})
        monkeypatch.setattr(db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("log-1", created))
        monkeypatch.setattr(db, "increment_trial_email",
                            lambda tid, cap=1000: self.increments.append((tid, cap)) or 1)
        monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)


def test_gate_off_does_not_touch_the_trial(monkeypatch):
    env = Env(monkeypatch, _tenant(), gate=False)
    log_id = asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert log_id == "log-1" and env.drafted == 1
    assert env.increments == []          # no counting while the gate is down


def test_active_trial_drafts_and_counts_once(monkeypatch):
    env = Env(monkeypatch, _tenant(days_ago=2), gate=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.drafted == 1
    assert env.increments == [("t1", trial.TRIAL_EMAIL_CAP)]


def test_duplicate_delivery_counts_zero(monkeypatch):
    env = Env(monkeypatch, _tenant(days_ago=2), created=False, gate=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.increments == []          # created=False → already counted on the first delivery


def test_converted_tenant_is_not_counted(monkeypatch):
    env = Env(monkeypatch, _tenant(status="active", days_ago=99), gate=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.drafted == 1 and env.increments == []


def test_expired_auto_path_stops_without_drafting(monkeypatch):
    env = Env(monkeypatch, _tenant(days_ago=99), gate=True)
    out = asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert out is None
    assert env.drafted == 0 and env.increments == []   # no model spend, no count


def test_expired_manual_path_raises_to_billing(monkeypatch):
    env = Env(monkeypatch, _tenant(days_ago=99), gate=True)
    with pytest.raises(trial.TrialLimitReached):
        asyncio.run(pipeline.draft_for_email("t1", EMAIL))   # human-initiated (auto_send_ok=False)
    assert env.drafted == 0


def test_two_fresh_emails_increment_twice(monkeypatch):
    env = Env(monkeypatch, _tenant(days_ago=2), gate=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m2"))
    assert env.increments == [("t1", trial.TRIAL_EMAIL_CAP), ("t1", trial.TRIAL_EMAIL_CAP)]
