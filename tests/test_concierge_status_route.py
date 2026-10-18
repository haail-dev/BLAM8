"""The full-screen page's status column reads: the work ledger and the two allowances (kept apart),
and the owner's wider day. Run: python -m pytest tests/test_concierge_status_route.py -q"""
from types import SimpleNamespace

from app import concierge_ledger, concierge_routes, debrief_walk


class _Tenants:
    def __init__(self, row):
        self.row = row

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=[self.row])


def _ctx(row=None):
    client = SimpleNamespace(table=lambda name: _Tenants(row or {}))
    return SimpleNamespace(tenant_id="t1", user_id="u1", client=client, viewing_as=None)


def _patch(monkeypatch, tenant, *, voice=(True, 600, {"used_seconds": 120, "remaining_seconds": 480})):
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda *a, **k: tenant)
    monkeypatch.setattr(concierge_ledger, "ledger", lambda t, c, args, m, u: {"items": [], "counts": {}, "args": args})
    monkeypatch.setattr(concierge_routes, "_live_allowance", lambda ctx: voice)


def test_status_returns_open_and_today_ledgers_and_both_allowances_apart(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "concierge_daily_cap_pro", 40)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    _patch(monkeypatch, {"plan": "starter"})
    out = concierge_routes.business_column_status(_ctx({"concierge_daily_used": 7, "concierge_daily_date": today}))
    assert out["open"]["args"] == {"status": "open"}
    assert out["today"]["args"] == {"status": "recent", "since_days": 1}
    assert out["limits"]["text"] == {"unlimited": False, "cap": 40, "used": 7, "left": 33}
    assert out["limits"]["voice"] == {"enabled": True, "unlimited": False, "limit_seconds": 600,
                                      "used_seconds": 120, "remaining_seconds": 480}


def test_exempt_tenants_have_no_text_allowance_and_a_new_day_starts_at_zero(monkeypatch):
    _patch(monkeypatch, {"plan": "starter", "fair_use_exempt": True})
    out = concierge_routes.business_column_status(_ctx())
    assert out["limits"]["text"] == {"unlimited": True}
    from app.config import settings
    monkeypatch.setattr(settings, "concierge_daily_cap_pro", 40)
    _patch(monkeypatch, {"plan": "starter"})
    old = concierge_routes.business_column_status(_ctx({"concierge_daily_used": 39, "concierge_daily_date": "2020-01-01"}))
    assert old["limits"]["text"]["used"] == 0 and old["limits"]["text"]["left"] == 40


def test_voice_off_or_unreadable_is_said_not_invented(monkeypatch):
    _patch(monkeypatch, {"plan": "starter"}, voice=(False, 0, {}))
    assert concierge_routes.business_column_status(_ctx())["limits"]["voice"] == {"enabled": False}

    def boom(ctx):
        raise RuntimeError("down")
    monkeypatch.setattr(concierge_routes, "_live_allowance", boom)
    assert concierge_routes.business_column_status(_ctx())["limits"]["voice"] == {"unavailable": True}


def test_around_is_for_owners_and_managers_and_gives_each_group_its_items_without_the_agendas_extra_fields(monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)
    agenda = {"groups": [{"key": "k", "label": "Follow-ups", "count": 5, "more": 2,
                          "items": [{"who": "A", "chip": "due", "text": "t", "email": "a@x.com", "rank": 1}] * 4}],
              "unread": ["Diary"]}
    monkeypatch.setattr(debrief_walk, "gather_agenda", lambda *a, **k: agenda)
    out = concierge_routes.business_around(_ctx())
    # every item the agenda holds for the group (it keeps six at most), so the column can list each one (Kris 2026-10-16)
    assert out["groups"][0]["count"] == 5 and len(out["groups"][0]["items"]) == 4 and out["groups"][0]["more"] == 2
    assert "email" not in out["groups"][0]["items"][0] and out["unread"] == ["Diary"]
    monkeypatch.setattr(debrief_walk, "refusal", lambda ctx, m: "not yours")
    assert concierge_routes.business_around(_ctx()) == {"groups": []}
