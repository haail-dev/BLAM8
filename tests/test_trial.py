"""Free-trial state machine + gate (src/app/trial.py).

Pins the evaluate() decisions (active / grace / converted / expired by time /
expired by cap), on_trial(), the flag-gated gate_allows(), and that the email
counter is incremented via a SINGLE atomic RPC (never a Python read-modify-write,
which would lose concurrent increments).

Run: python -m pytest tests/test_trial.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import db, trial
from app.config import settings

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def _t(**kw) -> dict:
    base = {"subscription_status": "none", "trial_emails_used": 0, "trial_status": "active"}
    return {**base, **kw}


# ── evaluate(): the state machine ────────────────────────────────────────────
def test_active_within_time_and_cap():
    t = _t(trial_start_at=(NOW - timedelta(days=3)).isoformat(), trial_emails_used=10)
    s = trial.evaluate(t, now=NOW)
    assert s["status"] == "active" and s["active"] is True
    assert s["days_remaining"] == settings.trial_days - 3
    assert s["cap"] == trial.TRIAL_EMAIL_CAP                      # tier-agnostic (env-driven)
    assert s["emails_used"] == 10 and s["emails_remaining"] == trial.TRIAL_EMAIL_CAP - 10


def test_expired_by_time():
    t = _t(trial_start_at=(NOW - timedelta(days=20)).isoformat())
    s = trial.evaluate(t, now=NOW)
    assert s["status"] == "expired_time" and s["active"] is False


def test_expired_by_cap_even_while_in_time():
    t = _t(trial_start_at=(NOW - timedelta(days=1)).isoformat(), trial_emails_used=trial.TRIAL_EMAIL_CAP)
    s = trial.evaluate(t, now=NOW)
    assert s["status"] == "expired_cap" and s["active"] is False
    assert s["emails_remaining"] == 0


def test_converted_subscription_is_never_trial_limited():
    # active/trialing Stripe (incl. grandfathered 'active') → access regardless of age/usage
    for status in ("active", "trialing"):
        t = _t(subscription_status=status,
                trial_start_at=(NOW - timedelta(days=999)).isoformat(),
                trial_emails_used=50_000)
        s = trial.evaluate(t, now=NOW)
        assert s["status"] == "converted" and s["active"] is True


def test_grace_window_keeps_access_then_lapses():
    base = dict(trial_status="grace_active",
                trial_start_at=(NOW - timedelta(days=30)).isoformat())
    inside = trial.evaluate(_t(**base, trial_grace_until=(NOW + timedelta(days=3)).isoformat()), now=NOW)
    assert inside["status"] == "grace_active" and inside["active"] is True
    lapsed = trial.evaluate(_t(**base, trial_grace_until=(NOW - timedelta(days=1)).isoformat()), now=NOW)
    assert lapsed["status"] == "expired_time" and lapsed["active"] is False


def test_falls_back_to_created_at_when_no_trial_start():
    t = {"subscription_status": "none", "created_at": (NOW - timedelta(days=2)).isoformat()}
    assert trial.evaluate(t, now=NOW)["active"] is True


def test_no_anchor_at_all_reads_as_expired():
    assert trial.evaluate({"subscription_status": "none"}, now=NOW)["status"] == "expired_time"


# ── on_trial() ───────────────────────────────────────────────────────────────
def test_on_trial_tracks_subscription_status():
    assert trial.on_trial({"subscription_status": "none"}) is True
    assert trial.on_trial({"subscription_status": "past_due"}) is True
    assert trial.on_trial({"subscription_status": "active"}) is False
    assert trial.on_trial({"subscription_status": "trialing"}) is False


# ── gate_allows(): flag-gated enforcement ────────────────────────────────────
def test_gate_off_always_allows(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", False)
    expired = _t(trial_start_at=(NOW - timedelta(days=99)).isoformat())
    assert trial.gate_allows(expired) is True       # open-ended while the gate is down


def test_gate_on_blocks_expired_allows_active(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", True)
    assert trial.gate_allows(_t(trial_start_at=(datetime.now(timezone.utc)).isoformat())) is True
    assert trial.gate_allows(_t(trial_start_at=(datetime.now(timezone.utc) - timedelta(days=99)).isoformat())) is False


# ── consume() / increment_trial_email(): atomic, best-effort ─────────────────
def test_increment_uses_a_single_atomic_rpc(monkeypatch):
    """The counter must move via the DB's atomic UPDATE (col = col + 1), not a
    Python read-then-write — so concurrent webhook deliveries can't lose a count."""
    rpc = MagicMock()
    rpc.execute.return_value = SimpleNamespace(data=7)
    fake = MagicMock()
    fake.rpc.return_value = rpc
    monkeypatch.setattr(db, "db", lambda: fake)
    out = db.increment_trial_email("t1", cap=1000)
    assert out == 7
    fake.rpc.assert_called_once_with("increment_trial_email", {"p_tenant_id": "t1", "p_cap": 1000})


def test_consume_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(trial.db, "increment_trial_email", boom)
    trial.consume("t1")     # best-effort: must swallow (metering never breaks a draft)
