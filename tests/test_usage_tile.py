"""The assistant and the customer portal on the usage tile (Kris 2026-09-13,
on the Today tour: "I plan to cap Concierge usage… besides the customer
portal it's the only potentially expensive feature — we need to track that
and portal use").

Run: python -m pytest tests/test_usage_tile.py -q
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import usage

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ev(op, created, cost=0.01, module="email"):
    return {"operation": op, "module": module, "created_at": created, "cost_usd": cost,
            "input_tokens": 10, "output_tokens": 5, "embed_tokens": 0, "email_log_id": None}


def test_summary_counts_assistant_turns_today_and_portal_use(monkeypatch):
    now = datetime.now(timezone.utc)
    today = usage._london_today_iso() + "T09:00:00+00:00"
    last_week = (now - timedelta(days=7)).isoformat()
    events = [
        _ev("draft", last_week), _ev("draft", today),
        _ev("concierge_help", today, module="concierge"), _ev("concierge_business", today, module="concierge"),
        _ev("concierge_onboard", last_week, module="concierge"),
        _ev("portal_chat", last_week, module="portal"), _ev("portal_chat", today, module="portal"),
        _ev("reminder", today),
    ]
    monkeypatch.setattr(usage.db, "get_tenant", lambda tid, client=None: {"plan": "starter",
                        "billing_started_at": (now - timedelta(days=10)).isoformat()})
    monkeypatch.setattr(usage.db, "list_usage_events", lambda *a, **k: list(events))
    monkeypatch.setattr(usage.db, "count_bookings_since",
                        lambda tid, since, source=None, client=None: 3 if source == "portal" else 99)
    monkeypatch.setattr(usage, "extra_mailbox_allowance", lambda *a, **k: 0)
    monkeypatch.setattr(usage, "extra_depot_allowance", lambda *a, **k: 0)
    from app import fairuse
    monkeypatch.setattr(fairuse, "daily_cap", lambda tenant: 30)
    s = usage.summarize("t1")
    assert s["assistant_turns"] == 3 and s["assistant_today"] == 2 and s["assistant_day_cap"] == 30
    assert s["portal_turns"] == 2 and s["portal_bookings"] == 3
    assert round(s["assistant_cost_usd"], 2) == 0.03 and round(s["portal_cost_usd"], 2) == 0.02


def test_a_failed_booking_count_never_breaks_the_summary(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(usage.db, "get_tenant", lambda tid, client=None: {"plan": "enterprise",
                        "billing_started_at": now.isoformat()})
    monkeypatch.setattr(usage.db, "list_usage_events", lambda *a, **k: [])

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(usage.db, "count_bookings_since", boom)
    monkeypatch.setattr(usage, "extra_mailbox_allowance", lambda *a, **k: 0)
    monkeypatch.setattr(usage, "extra_depot_allowance", lambda *a, **k: 0)
    s = usage.summarize("t1")
    assert s["portal_bookings"] == 0 and s["assistant_turns"] == 0 and s["assistant_day_cap"] is None


def test_the_tile_the_page_and_the_tour_show_them():
    home = _read("src/app/templates/app/home.html")
    assert "data-usage-more" in home and "u.assistant_turns" in home and "u.portal_bookings" in home
    assert "of {{ u.assistant_day_cap }} today" in home
    page = _read("src/app/templates/app/usage.html")
    assert "data-usage-more" in page and "summary.assistant_turns" in page and "summary.portal_bookings" in page
    assert "Chats before a customer verifies are on us, not you." in page
    js = _read("src/app/static/tour.js")
    assert "emails drafted, chats " in js and "what customers do on your portal" in js
    assert 'def count_bookings_since(tenant_id: str, since_iso: str, source: str | None = None,' in _read("src/app/db.py")
