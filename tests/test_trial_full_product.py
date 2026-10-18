"""Full-product trial (2026-07-31, Julius): the trial was email-only — every
aha-moment the site sells (diary, self-filling CRM, real portal bookings) was
invisible until payment. Now sign-up seeds the Pro module set, and each LLM
surface carries its own trial pot: TRIAL_EMAIL_CAP for drafts (existing) and
TRIAL_CONCIERGE_CAP for owner-assistant questions (new).

What this pins:
  - a brand-new tenant gets the Pro modules seeded via apply_plan_modules
    ("starter" is the Pro tier's plan KEY — legacy naming), and a seeding
    failure never blocks sign-in
  - concierge_blocked(): off while the enforcement flag is off (launch state),
    off for converted tenants, on only when a trial tenant has spent the pot
  - the counter increment is best-effort (a metering failure never breaks an
    answer)

Run: python -m pytest tests/test_trial_full_product.py -q
"""
from types import SimpleNamespace

import pytest

from app import db, trial
from app.config import settings


# ── sign-up seeds the Pro module set ─────────────────────────────────────────
class _FakeClient:
    """Just enough supabase for ensure_tenant_for_user's two inserts."""

    def __init__(self):
        self.inserted = []

    def table(self, name):
        self._t = name
        return self

    def insert(self, row):
        self.inserted.append((self._t, row))
        return self

    def execute(self):
        return SimpleNamespace(data=[{"id": "t-new"}])


def test_new_tenant_gets_pro_modules(monkeypatch):
    fake = _FakeClient()
    seeded = []
    monkeypatch.setattr(db, "db", lambda: fake)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(db, "apply_plan_modules",
                        lambda tid, plan: seeded.append((tid, plan)) or set())
    tid = db.ensure_tenant_for_user("user-1", "Acme Ltd")
    assert tid == "t-new"
    assert seeded == [("t-new", "concierge")]    # the trial is Pro + Concierge since 2026-09-25
    assert ("tenant_members", {"tenant_id": "t-new", "user_id": "user-1",
                               "role": "owner"}) in fake.inserted


def test_seeding_failure_never_blocks_signin(monkeypatch):
    fake = _FakeClient()

    def boom(tid, plan):
        raise RuntimeError("supabase hiccup")
    monkeypatch.setattr(db, "db", lambda: fake)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(db, "apply_plan_modules", boom)
    assert db.ensure_tenant_for_user("user-1", None) == "t-new"


def test_existing_tenant_is_never_reseeded(monkeypatch):
    called = []
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t-old")
    monkeypatch.setattr(db, "apply_plan_modules",
                        lambda *a: called.append(a))
    assert db.ensure_tenant_for_user("user-1", None) == "t-old"
    assert called == []


# ── the trial concierge pot ──────────────────────────────────────────────────
def _trial_tenant(used):
    return {"subscription_status": None, "trial_concierge_used": used}


def test_concierge_blocked_only_when_gate_on_and_pot_spent(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", True)
    cap = trial.TRIAL_CONCIERGE_CAP
    assert trial.concierge_blocked(_trial_tenant(cap)) is True
    assert trial.concierge_blocked(_trial_tenant(cap - 1)) is False
    assert trial.concierge_blocked(_trial_tenant(None)) is False


def test_concierge_never_blocked_with_gate_off(monkeypatch):
    # Launch state: evaluate/report only, nothing blocks (mirrors the email gate).
    monkeypatch.setattr(settings, "require_subscription_gate", False)
    assert trial.concierge_blocked(_trial_tenant(10_000)) is False


def test_converted_tenant_is_never_limited(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", True)
    assert trial.concierge_blocked(
        {"subscription_status": "active", "trial_concierge_used": 10_000}) is False


def test_consume_concierge_is_best_effort(monkeypatch):
    def boom(tid):
        raise RuntimeError("db down")
    monkeypatch.setattr(trial.db, "increment_trial_concierge", boom)
    trial.consume_concierge("t1")      # must not raise