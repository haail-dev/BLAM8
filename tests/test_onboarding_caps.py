"""Per-tenant daily ceilings on the onboarding cost surfaces (2026-09-02 review).

/concierge/onboard, /onboarding/knowledge/website and /onboarding/voice/learn
sit outside the business-chat fair-use meter and previously had no cost guard.

Pins:
- fairuse.onboarding_allowed counts today's usage_events rows for the operation
  (check-before-spend) against the config cap, measures from London midnight,
  treats a 0 cap as uncapped (no DB call), and FAILS OPEN on a metering error;
- each route refuses BEFORE any LLM/crawl work: the co-pilot answers in-chat
  with the polite reset line, the website import returns a 429 JSON error, and
  the voice-learn button redirects without spawning a run.

No network: db.* monkeypatched; the autouse conftest guard fails loudly on any
un-stubbed live Supabase call.

Run: python -m pytest tests/test_onboarding_caps.py -q
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db, fairuse
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.main import app
from app.security import require_csrf, require_csrf_header


# ── the gate ─────────────────────────────────────────────────────────────────
def test_allowed_under_cap_counts_from_london_midnight(monkeypatch):
    seen = {}

    def _count(tid, op, since, client=None):
        seen.update(tid=tid, op=op, since=since)
        return 9
    monkeypatch.setattr(db, "count_usage_events", _count)
    assert fairuse.onboarding_allowed("t1", "kb_website") is True
    assert (seen["tid"], seen["op"]) == ("t1", "kb_website")
    midnight = datetime.fromisoformat(seen["since"])
    assert midnight.tzinfo is not None
    now_london = datetime.now(ZoneInfo("Europe/London"))
    assert midnight.astimezone(ZoneInfo("Europe/London")).date() == now_london.date()
    assert (midnight.hour, midnight.minute, midnight.second) == (0, 0, 0)


def test_refused_at_cap(monkeypatch):
    monkeypatch.setattr(db, "count_usage_events", lambda *a, **k: 10)
    assert fairuse.onboarding_allowed("t1", "kb_website") is False


def test_zero_cap_means_uncapped_and_skips_the_count(monkeypatch):
    monkeypatch.setattr(settings, "concierge_onboard_daily_cap", 0)

    def _never(*a, **k):
        raise AssertionError("uncapped surfaces must not query")
    monkeypatch.setattr(db, "count_usage_events", _never)
    assert fairuse.onboarding_allowed("t1", "concierge_onboard") is True


def test_fails_open_on_metering_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "count_usage_events", _boom)
    assert fairuse.onboarding_allowed("t1", "voice_learning") is True


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf] = lambda: None
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_onboard_route_refuses_in_chat_before_the_model(client, monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.fairuse, "onboarding_allowed",
                        lambda tid, op, client=None: False)

    async def _never(*a, **k):
        raise AssertionError("the model must not be invoked when capped")
    monkeypatch.setattr(concierge_routes.concierge, "answer_onboarding", _never)

    res = client.post("/concierge/onboard",
                      json={"step": "interview", "question": "hi"})
    assert res.status_code == 200
    body = res.json()
    assert "midnight" in body["reply"]
    assert body["form_fill"] == {} and body["ready_to_advance"] is False


def test_website_route_refuses_before_the_crawl(client, monkeypatch):
    from app import onboarding
    monkeypatch.setattr(onboarding.kb, "is_configured", lambda: True)
    monkeypatch.setattr(onboarding.fairuse, "onboarding_allowed",
                        lambda tid, op, client=None: False)

    async def _never(*a, **k):
        raise AssertionError("no crawl/distill when capped")
    monkeypatch.setattr(onboarding.kb_website, "build", _never)

    res = client.post("/onboarding/knowledge/website",
                      data={"url": "https://example.com"})
    assert res.status_code == 429
    assert "allowance" in res.json()["error"]


def test_voice_learn_capped_click_skips_the_spawn(client, monkeypatch):
    from app import onboarding
    monkeypatch.setattr(onboarding.db, "get_connection_status",
                        lambda *a, **k: True)
    monkeypatch.setattr(onboarding.fairuse, "onboarding_allowed",
                        lambda tid, op, client=None: False)

    def _never(*a, **k):
        raise AssertionError("no learn run when capped")
    monkeypatch.setattr(onboarding.voice_learning, "spawn", _never)

    res = client.post("/onboarding/voice/learn", follow_redirects=False)
    assert res.status_code == 303   # back to the voice screen, run not started


def test_voice_learn_allowed_click_still_spawns(client, monkeypatch):
    from app import onboarding
    spawned = []
    monkeypatch.setattr(onboarding.db, "get_connection_status",
                        lambda *a, **k: True)
    monkeypatch.setattr(onboarding.fairuse, "onboarding_allowed",
                        lambda tid, op, client=None: True)
    monkeypatch.setattr(onboarding.voice_learning, "spawn",
                        lambda tid: spawned.append(tid))

    res = client.post("/onboarding/voice/learn", follow_redirects=False)
    assert res.status_code == 303
    assert spawned == ["t1"]
