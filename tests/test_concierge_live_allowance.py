"""Connection-minute quota: independent from the Concierge question counter."""
import asyncio
import pathlib
from types import SimpleNamespace

from fastapi.responses import JSONResponse

from app import db, fairuse
from app.auth import TenantCtx


def test_tier_minutes_are_per_user_and_independent_of_question_exemption(monkeypatch):
    monkeypatch.setattr(db, "concierge_live_tier_limits", lambda: {
        "starter": 4, "pro": 9, "core": 12, "enterprise": 0,
    })
    assert fairuse.live_limit_seconds({"plan": "email", "fair_use_exempt": True}) == 240
    assert fairuse.live_limit_seconds({"plan": "starter"}) == 540
    assert fairuse.live_limit_seconds({"plan": "professional"}) == 720
    assert fairuse.live_limit_seconds({"plan": "enterprise"}) == 0
    assert fairuse.live_limit_seconds({"plan": "new-plan"}) == 540


def test_tier_defaults_to_ten_minutes(monkeypatch):
    monkeypatch.setattr(db, "concierge_live_tier_limits", lambda: {})
    assert fairuse.live_limit_seconds({"plan": "email"}) == 600
    assert fairuse.live_limit_seconds({"plan": "enterprise"}) == 600


def test_status_and_connection_dates_use_london_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert ZoneInfo("Europe/London")
    assert fairuse.london_day() == datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def test_database_rpc_calls_include_authenticated_scope_and_server_date(monkeypatch):
    calls = []

    class RPC:
        def __init__(self, name, args):
            self.name, self.args = name, args

        def execute(self):
            calls.append((self.name, self.args))
            return SimpleNamespace(data={"ok": True})

    class FakeDB:
        def rpc(self, name, args):
            return RPC(name, args)

    monkeypatch.setattr(db, "db", lambda: FakeDB())
    db.reserve_concierge_live_session("s", "t", "u", "2026-10-14", 600)
    db.heartbeat_concierge_live_session("s", "t", "u", 600)
    db.close_concierge_live_session("s", "t", "u")
    db.concierge_live_status("t", "u", "2026-10-14", 600)
    assert [c[0] for c in calls] == [
        "concierge_live_reserve", "concierge_live_heartbeat",
        "concierge_live_close", "concierge_live_status",
    ]
    assert calls[0][1]["p_tenant_id"] == "t" and calls[0][1]["p_user_id"] == "u"
    assert calls[0][1]["p_day"] == "2026-10-14"
    assert calls[1][1]["p_limit_seconds"] == 600


def test_clients_can_read_allowance_but_cannot_mutate_native_session_lease():
    from app import concierge_routes
    paths = {route.path for route in concierge_routes.router.routes}
    assert "/concierge/voice/allowance" in paths
    assert "/concierge/voice/allowance/heartbeat" not in paths
    assert "/concierge/voice/allowance/close" not in paths


def test_standard_voice_captions_remain_available_when_rollout_enabled(monkeypatch):
    from app import concierge_routes
    ctx = TenantCtx(tenant_id="tenant", client=None, user_id="user")
    monkeypatch.setattr(concierge_routes.db, "concierge_live_rollout", lambda: True)
    monkeypatch.setattr(concierge_routes, "_live_asker", lambda _ctx: None)
    calls = []

    async def mint(*args, **kwargs):
        calls.append((args, kwargs))
        return JSONResponse({"value": "provider-token"})

    monkeypatch.setattr(concierge_routes, "_mint_realtime_token", mint)
    response = asyncio.run(concierge_routes.voice_token(
        concierge_routes.VoiceTokenRequest(purpose="captions"), ctx))
    assert response.status_code == 200
    assert calls and calls[0][0][0] is True


def test_migration_has_lease_settlement_rls_and_rollback():
    root = pathlib.Path(__file__).parent.parent
    migration = (root / "db/migrations/20261014_concierge_live_allowance.sql").read_text()
    rollback = (root / "db/migrations/20261014_concierge_live_allowance.rollback.sql").read_text()
    assert "pg_advisory_xact_lock" in migration
    assert "reserved_until" in migration and "charged_seconds" in migration
    assert "Europe/London" in migration
    assert "lease_expired" in migration and "day_rollover" in migration
    assert "settled_at := least(day_end,r.reserved_until,now())" in migration
    assert "stamp := least(stamp,r.reserved_until)" in migration
    assert "enable row level security" in migration
    assert "revoke all on function concierge_live_reserve" in migration
    assert "drop table if exists concierge_live_sessions" in rollback
