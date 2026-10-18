"""Plans-page trial banner — the page must tell the truth about the pilot.

When REQUIRE_SUBSCRIPTION_GATE is on and a tenant's free pilot is over, the
/app router 303s them to /billing/plans; the page must then say the pilot has
ENDED (amber banner) instead of the emerald "Continue free →" box whose link
just bounces straight back here. Checkout CTAs switch copy too ("billing
starts today" — an expired-window tenant gets no Stripe trial_end). With the
gate off the pilot is open-ended, so nothing may claim it ended. Comped
tenants see their comp, not trial copy.

Run: python -m pytest tests/test_plans_trial_banner.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.billing_routes as billing_routes
import app.dashboard as dashboard
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.main import app
from app.security import require_csrf

CORE_PRICE = "price_core_99"


@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def _tenant(**over) -> dict:
    """A free-pilot tenant whose 14-day window lapsed long ago (the default);
    override trial_start_at/comp_access per test. industry None keeps the
    plans page off the OS-preview db path."""
    base = {"id": "t1", "name": "Koth", "industry": None, "mailbox": None,
            "plan": "pilot", "subscription_status": "none",
            "trial_start_at": (datetime.now(timezone.utc) - timedelta(days=99)).isoformat(),
            "trial_emails_used": 0, "trial_status": "active",
            "comp_access": None, "comp_until": None, "os_access_approved": False}
    base.update(over)
    return base


def _env(monkeypatch, tenant: dict, gate: bool) -> None:
    monkeypatch.setattr(settings, "require_subscription_gate", gate)
    monkeypatch.setattr(settings, "stripe_starter_price_id", CORE_PRICE)
    monkeypatch.setattr(billing_routes.billing, "is_configured", lambda: True)
    monkeypatch.setattr(billing_routes.db, "get_tenant",
                        lambda tid, client=None: dict(tenant))
    monkeypatch.setattr(billing_routes.db, "tenant_pending_os_request", lambda tid: None)
    # The app-shell context (dashboard._ctx) behind the plans page.
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)


def test_gate_on_expired_pilot_shows_ended_banner(client, monkeypatch):
    _env(monkeypatch, _tenant(), gate=True)
    html = client.get("/billing/plans").text
    assert "Your free pilot has ended" in html          # sub-heading
    assert "Your 14-day free pilot ended" in html       # amber banner (time expiry)
    assert "Continue free" not in html                  # the bouncing link is gone
    assert "Start free trial" not in html               # no second free trial exists
    assert "Choose this plan" in html
    assert "billing starts today" in html


def test_gate_on_cap_exhausted_names_the_cap(client, monkeypatch):
    _env(monkeypatch, _tenant(trial_start_at=datetime.now(timezone.utc).isoformat(),
                              trial_emails_used=150), gate=True)
    html = client.get("/billing/plans").text
    assert "pilot emails" in html                       # "used all N pilot emails"
    assert "Continue free" not in html


def test_gate_on_live_pilot_keeps_continue_free(client, monkeypatch):
    _env(monkeypatch, _tenant(trial_start_at=datetime.now(timezone.utc).isoformat()),
         gate=True)
    html = client.get("/billing/plans").text
    assert "Continue free" in html
    assert "Your free pilot has ended" not in html
    assert "Start free trial" in html


def test_gate_off_never_claims_the_pilot_ended(client, monkeypatch):
    """Gate off = pilot open-ended (the launch state): even a lapsed window
    must not read as ended, because nothing actually blocks."""
    _env(monkeypatch, _tenant(), gate=False)
    html = client.get("/billing/plans").text
    assert "Continue free" in html
    assert "Your free pilot has ended" not in html


def test_comped_tenant_sees_comp_not_trial_copy(client, monkeypatch):
    _env(monkeypatch, _tenant(comp_access="indefinite"), gate=True)
    html = client.get("/billing/plans").text
    assert "complimentary access" in html
    assert "Open BLAM8" in html
    assert "Your free pilot has ended" not in html
    assert "Continue free" not in html


def test_sent_from_the_concierge_says_which_tier_she_is_on(client, monkeypatch):
    """/app/concierge on a plan without her redirects to ?concierge=1 — the page
    must say she's part of Pro + Concierge, and only then."""
    _env(monkeypatch, _tenant(plan="starter", subscription_status="active"), gate=False)
    html = client.get("/billing/plans?concierge=1").text
    assert "The Concierge is part of Pro + Concierge." in html
    assert "data-concierge-upsell" not in client.get("/billing/plans").text


def test_a_pro_plus_concierge_subscriber_is_on_a_paid_plan(client, monkeypatch):
    """'concierge' (£249) is a paid tier: its subscriber sees their status and
    the Customer Portal, never the free-pilot box."""
    _env(monkeypatch, _tenant(plan="concierge", subscription_status="active"), gate=True)
    html = client.get("/billing/plans").text
    assert "Current status: <strong>active</strong>" in html
    assert "Continue free" not in html
    assert "Your free pilot has ended" not in html
