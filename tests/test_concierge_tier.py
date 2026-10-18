"""Pro + Concierge (£249) — the business Concierge is its own tier.

Kris, 2026-09-25: "No person icon / concierge access for lower tiers at all.
They have the App help in the Search." Pro + Concierge = Pro plus the
Concierge ("for now" the same allowance); the trial is Pro + Concierge;
existing tenants were given the module so nothing they had went away.

Every other test runs with the Concierge on (tests/conftest.py
_concierge_on_by_default); these turn it off.

Run: python -m pytest tests/test_concierge_tier.py -q
"""
import inspect

import pytest
from fastapi import HTTPException

import app.auth as auth
import app.concierge_routes as cr
import app.dashboard as dashboard
from app import plans, usage
from tests.test_home_dashboard import Env, client  # noqa: F401  (the fixture)

_REAL_HAS_CONCIERGE = auth.has_concierge   # taken at import, before conftest's default


@pytest.fixture
def no_concierge(monkeypatch):
    monkeypatch.setattr(auth, "has_concierge", lambda *a, **k: False)
    monkeypatch.setattr(dashboard, "has_concierge", lambda *a, **k: False)


def test_the_tier_is_pro_plus_the_concierge():
    assert plans.modules_for("concierge") == {"scheduling", "crm", "concierge"}
    assert "concierge" not in plans.modules_for("starter")        # Pro
    assert "concierge" not in plans.modules_for("email")          # Starter
    assert plans.plan_name("concierge") == "Pro + Concierge"
    assert plans.plan_price_gbp("concierge") == 249
    assert usage.PLANS["concierge"]["monthly_emails"] == usage.PLANS["starter"]["monthly_emails"]
    assert "concierge" in plans.MANAGED_MODULES                   # a Pro purchase pauses it


def test_every_business_and_voice_route_needs_the_concierge():
    gated, open_ = [], []
    for r in cr.router.routes:
        deps = [d.call for d in r.dependant.dependencies]
        (gated if auth.require_concierge in deps else open_).append(r.path)
    for path in gated:
        assert path.startswith(("/concierge/business", "/concierge/voice/")), path
    assert "/concierge/business" in gated and "/concierge/voice/chat" in gated \
        and "/concierge/voice/token" in gated and "/concierge/business/execute" in gated
    # App help, onboarding, the website assistant, and the feed stay open
    for path in ("/concierge/help", "/concierge/ask", "/concierge/onboard", "/concierge/fix",
                 "/concierge/notifications/{notification_id}/dismiss"):
        assert path in open_, path


def test_the_check_refuses_without_the_module(no_concierge):
    import asyncio
    ctx = auth.TenantCtx(tenant_id="t1", client=None, user_id="u1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.require_concierge(ctx))
    assert e.value.status_code == 403


def test_the_check_reads_the_module(monkeypatch):
    monkeypatch.setattr(auth.db, "visible_modules", lambda *a, **k: {"crm", "concierge"})
    assert _REAL_HAS_CONCIERGE("t1") is True
    monkeypatch.setattr(auth.db, "visible_modules", lambda *a, **k: {"crm"})
    assert _REAL_HAS_CONCIERGE("t1") is False


def test_without_the_module_there_is_no_person_and_no_concierge_script(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling", "crm"})               # Pro
    html = client.get("/app/").text
    assert 'id="concierge-btn"' not in html and 'id="cz-modal"' not in html
    assert "/static/concierge_panel.js" not in html and "voice-live.js" not in html
    assert 'class="cz-menu-pc' not in html and 'href="/app/concierge" data-concierge-open' not in html
    assert 'id="search-help-line"' in html                         # App help stays, in the search


def test_with_the_module_the_person_is_back(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling", "crm", "concierge"})
    html = client.get("/app/").text
    assert 'id="concierge-btn"' in html and "/static/concierge_panel.js" in html


def test_the_concierge_pages_send_you_to_the_plans(client, monkeypatch, no_concierge):
    Env(monkeypatch, modules={"scheduling", "crm"})
    r = client.get("/app/concierge", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/billing/plans?concierge=1"
    assert client.get("/app/concierge/panel").status_code == 403


def test_the_trial_opens_with_the_concierge():
    src = inspect.getsource(auth.db.ensure_tenant_for_user)
    assert 'apply_plan_modules(tenant_id, "concierge")' in src


def _counts(monkeypatch, *, waiting=2, done_today=1):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(dashboard.db, "list_waiting_proposals", lambda *a, **k: [{}] * waiting)
    monkeypatch.setattr(dashboard.db, "list_waiting_drafts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_waiting_invoices", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_done_proposals", lambda *a, **k:
                        [{"done_at": now}] * done_today + [{"done_at": "2026-01-01T09:00:00+00:00"}])
    monkeypatch.setattr(dashboard.db, "list_sent_drafts", lambda *a, **k: [])


def test_today_has_a_concierge_row_with_her_counts(client, monkeypatch):
    """Kris 2026-09-25: a Concierge row in Today's numbers sheet."""
    Env(monkeypatch, modules={"scheduling", "crm", "concierge"})
    _counts(monkeypatch)
    html = client.get("/app/").text
    row = html[html.index("data-concierge-row"):]
    row = row[:row.index("</a>")]
    assert "2<span" in row and "waiting" in row and "1 done for you today" in row
    assert 'href="/app/concierge" data-concierge-open' in html


def test_without_the_tier_the_row_is_locked_and_goes_to_the_plans(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling", "crm"})
    html = client.get("/app/").text
    assert 'href="/billing/plans?concierge=1" class="block px-4 py-2.5 hover:bg-slate-50 transition" data-concierge-row data-concierge-locked' in html
