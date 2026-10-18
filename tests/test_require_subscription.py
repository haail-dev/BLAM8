"""require_subscription — the access gate wired onto the /app dashboard router.

Behaviour, now that it sits behind REQUIRE_SUBSCRIPTION_GATE and grants the free
trial access too:
  * gate OFF (default)            → pass-through (no row read), every status passes
  * gate ON + 'active'/'trialing' → passes (paid / grandfathered)
  * gate ON + live free trial     → passes even with subscription_status 'none'
  * gate ON + lapsed (no sub, no live trial) → raises SubscriptionRequired
  * gate ON + the tenant row is GONE → raises TenantGone (end the session; a
    deleted account has nothing to bill)

Run: python -m pytest tests/test_require_subscription.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.auth import SubscriptionRequired, TenantCtx, TenantGone, require_subscription
from app.config import settings


def _ctx_for_row(row: dict | None) -> TenantCtx:
    """A fake TenantCtx whose client.table(...).select(...).eq(...).limit(...)
    chain returns `row` — the shape db.get_tenant reads."""
    chain = MagicMock()
    chain.execute.return_value = SimpleNamespace(data=[row] if row is not None else [])
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.limit.return_value = chain
    return TenantCtx(tenant_id="tenant-1", client=client)


def _recent_trial(status="none"):
    return {"subscription_status": status, "trial_emails_used": 0, "trial_status": "active",
            "trial_start_at": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", True)


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(settings, "require_subscription_gate", False)


# ── gate OFF → pass-through ───────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["none", "past_due", "canceled", "active"])
async def test_gate_off_passes_every_status(gate_off, status):
    ctx = _ctx_for_row({"subscription_status": status})
    assert (await require_subscription(ctx)) is ctx
    ctx.client.table.assert_not_called()    # pass-through never reads the row


# ── gate ON ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing"])
async def test_active_subscription_passes(gate_on, status):
    ctx = _ctx_for_row({"subscription_status": status})
    assert (await require_subscription(ctx)) is ctx


@pytest.mark.asyncio
async def test_live_free_trial_passes_without_subscription(gate_on):
    ctx = _ctx_for_row(_recent_trial("none"))
    assert (await require_subscription(ctx)) is ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["none", "past_due", "canceled", "incomplete"])
async def test_lapsed_redirects(gate_on, status):
    """No active subscription AND no live trial (no trial anchor → expired) →
    SubscriptionRequired, so main.py can 303 to /billing/plans."""
    ctx = _ctx_for_row({"subscription_status": status})
    with pytest.raises(SubscriptionRequired):
        await require_subscription(ctx)


@pytest.mark.asyncio
async def test_a_vanished_tenant_ends_the_session_rather_than_billing_for_it(gate_on, monkeypatch):
    """Live 2026-09-04: a deleted test tenant was shown the plans page — "your
    pilot has ended" for an account that does not exist. No row means the session
    points at a ghost, so it ends; the cached user→tenant hop that keeps pointing
    there goes with it."""
    from app import navcache
    dropped = []
    monkeypatch.setattr(navcache, "invalidate", lambda prefix: dropped.append(prefix))
    ctx = TenantCtx(tenant_id="tenant-1", client=_ctx_for_row(None).client, user_id="user-9")
    with pytest.raises(TenantGone):
        await require_subscription(ctx)
    assert dropped == ["ut:user-9"]


@pytest.mark.asyncio
async def test_a_vanished_tenant_without_a_user_id_drops_nothing(gate_on, monkeypatch):
    """"ut:" on its own is every user's hop — never invalidate a bare prefix."""
    from app import navcache
    dropped = []
    monkeypatch.setattr(navcache, "invalidate", lambda prefix: dropped.append(prefix))
    with pytest.raises(TenantGone):
        await require_subscription(_ctx_for_row(None))
    assert dropped == []


@pytest.mark.asyncio
async def test_expired_trial_redirects(gate_on):
    old = {"subscription_status": "none", "trial_emails_used": 0, "trial_status": "active",
           "trial_start_at": (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()}
    ctx = _ctx_for_row(old)
    with pytest.raises(SubscriptionRequired):
        await require_subscription(ctx)
