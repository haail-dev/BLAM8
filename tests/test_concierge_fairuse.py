"""Concierge daily fair-use cap (docs/modules/module-concierge-cost-controls.md Part B).

Pins:
- cap resolution by plan KEY (⚠ keys don't match display names): email=Starter
  10 · starter=Pro 30 · professional=Core 60 · enterprise=unlimited · unknown/
  trial fall back to the Pro allowance; a 0 in config means that tier unlimited;
- consume() is count-then-check via the atomic daily RPC, sends the
  Europe/London date, skips the write entirely for unlimited plans, and FAILS
  OPEN on a metering error (an answer is never lost to a counter);
- the refusal copy names today's cap; only Starter gets the Pro upgrade line;
- the business route refuses with the trial-cap NDJSON shape BEFORE the model
  is invoked, and counts exactly one question per allowed turn.

No network: db.* monkeypatched; the autouse conftest guard fails loudly on any
un-stubbed live Supabase call.

Run: python -m pytest tests/test_concierge_fairuse.py -q
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import concierge, db, fairuse
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.main import app
from app.security import require_csrf_header


# ── cap resolution ───────────────────────────────────────────────────────────
def test_daily_cap_by_plan_key():
    assert fairuse.daily_cap({"plan": "email"}) == 10
    assert fairuse.daily_cap({"plan": "starter"}) == 30
    assert fairuse.daily_cap({"plan": "professional"}) == 60
    assert fairuse.daily_cap({"plan": "enterprise"}) is None
    # unknown / trial / comped → the Pro allowance (trial seeds Pro modules)
    assert fairuse.daily_cap({"plan": "something-new"}) == 30
    assert fairuse.daily_cap({}) == 30
    assert fairuse.daily_cap(None) == 30


def test_an_exempt_account_has_no_daily_cap_and_comp_alone_does_not(monkeypatch):
    # Kris 2026-09-23 hit his own cap on the rig. The switch is per account and
    # separate from comp: LittleDogs, a real customer, is comped too.
    assert fairuse.daily_cap({"plan": "pilot", "fair_use_exempt": True}) is None
    assert fairuse.daily_cap({"plan": "email", "fair_use_exempt": True}) is None
    assert fairuse.daily_cap({"plan": "pilot", "comp_access": "indefinite"}) == 30

    def _never(*a, **k):
        raise AssertionError("an exempt account must not meter")
    monkeypatch.setattr(db, "increment_concierge_daily", _never)
    assert fairuse.consume("t1", {"fair_use_exempt": True}) == (True, None)


def test_the_switch_is_operator_only_and_read_with_the_tenant():
    import inspect
    from app import admin
    src = inspect.getsource(admin.set_fair_use)
    assert "require_admin" in src
    assert "fair_use_exempt" in inspect.getsource(db.get_tenant)


def test_zero_config_means_that_tier_unlimited(monkeypatch):
    monkeypatch.setattr(settings, "concierge_daily_cap_starter", 0)
    assert fairuse.daily_cap({"plan": "email"}) is None


# ── consume() ────────────────────────────────────────────────────────────────
def test_consume_skips_rpc_for_unlimited(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("unlimited plans must not meter")
    monkeypatch.setattr(db, "increment_concierge_daily", _never)
    assert fairuse.consume("t1", {"plan": "enterprise"}) == (True, None)


def test_consume_counts_then_checks_with_london_date(monkeypatch):
    calls, state = [], {"n": 0}

    def _inc(tid, today):
        calls.append((tid, today))
        state["n"] += 1
        return state["n"]
    monkeypatch.setattr(db, "increment_concierge_daily", _inc)
    monkeypatch.setattr(settings, "concierge_daily_cap_pro", 2)
    tenant = {"plan": "starter"}
    assert fairuse.consume("t1", tenant) == (True, 2)
    assert fairuse.consume("t1", tenant) == (True, 2)
    assert fairuse.consume("t1", tenant) == (False, 2)   # third of the day
    today = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    assert calls == [("t1", today)] * 3


def test_consume_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "increment_concierge_daily", _boom)
    assert fairuse.consume("t1", {"plan": "email"}) == (True, 10)


# ── the refusal copy ─────────────────────────────────────────────────────────
def test_capped_reply_names_cap_and_upgrades_starter_only():
    r = fairuse.capped_reply({"plan": "email"}, 10)
    assert "10" in r and "midnight" in r and "Pro plan" in r and "30" in r
    r2 = fairuse.capped_reply({"plan": "starter"}, 30)
    assert "30" in r2 and "Pro plan" not in r2


# ── the business route ───────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_business_route_refuses_before_the_model_runs(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda *a, **k: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda *a, **k: {"plan": "email"})
    monkeypatch.setattr(concierge_routes.fairuse, "consume",
                        lambda tid, tenant: (False, 10))

    async def _never(*a, **k):
        raise AssertionError("the model must not be invoked when capped")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", _never)

    res = client.post("/concierge/business", json={"question": "hi"})
    assert res.status_code == 200
    events = [json.loads(l) for l in res.text.strip().splitlines()]
    assert events[0]["type"] == "done"
    assert "today's 10 assistant questions" in events[0]["reply"]
    assert events[0]["links"] == [] and events[0]["trail"] == []


def test_business_route_counts_exactly_one_per_turn(client, monkeypatch):
    from app import concierge_routes
    seen = []
    monkeypatch.setattr(concierge_routes.db, "visible_modules",
                        lambda *a, **k: set())
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda *a, **k: {"plan": "starter", "name": "K"})
    monkeypatch.setattr(concierge_routes.fairuse, "consume",
                        lambda tid, tenant: (seen.append((tid, tenant.get("plan")))
                                             or (True, 30)))

    async def _boom(*a, **k):   # the degrade path still counts the question
        raise RuntimeError("model down")
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", _boom)

    res = client.post("/concierge/business", json={"question": "hello?"})
    assert res.status_code == 200
    assert seen == [("t1", "starter")]
    events = [json.loads(l) for l in res.text.strip().splitlines()]
    assert any(e["type"] == "done" for e in events)
