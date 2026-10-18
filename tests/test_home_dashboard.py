"""Home = the Today dashboard (2026-07-02): profile strip + approval queue by
mailbox + today's board + usage meter. The module hub moved to /app/modules.

Route-level via TestClient (auth overridden, db/usage/trial monkeypatched):
- / renders the strip, the per-mailbox queue (legacy null mailbox_id rows land
  under the default box), the Today card and the usage meter;
- /modules still renders the hub cards;
- the Mailboxes nav renders from the multimailbox entitlement (enterprise OR an
  active paid plan), not from `enterprise` — the discoverability fix.

Run: python -m pytest tests/test_home_dashboard.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


BOXES = [
    {"id": "m1", "address": "office@koth.co.uk", "label": None, "department": "Office"},
    {"id": "m2", "address": "dispatch@koth.co.uk", "label": "Dispatch", "department": "Depot North"},
]

CONVOS = [
    {"contact": "a@x.com", "state": "pending", "subject": "Quote please", "mailbox_id": "m2",
     "last_at": "2026-07-02T09:00:00+00:00",
     "display_name": "Sarah", "contact_company": "Acme Ltd"},
    {"contact": "b@x.com", "state": "pending", "subject": "Booking?", "mailbox_id": None,
     "last_at": "2026-07-02T08:00:00+00:00"},        # legacy row → default box (m1)
    {"contact": "c@x.com", "state": "sent", "subject": "Done", "mailbox_id": "m1",
     "last_at": "2026-07-01T08:00:00+00:00"},        # decided — never in the queue
]


SLOTS_CFG = {  # an office slots diary with intake done — the strip's happy path
    "windows": "slots", "open_time": "09:00", "close_time": "17:00",
    "slot_minutes": 60, "capacity_per_window": 1,
    "working_days": "mon,tue,wed,thu,fri", "intake_configured_at": "2026-07-03",
}


class Env:
    def __init__(self, monkeypatch, *, modules={"scheduling"}, boxes=BOXES, convos=CONVOS,
                 enterprise=False, subscribed=True, bookings=(), cfg=SLOTS_CFG):
        monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: dict(cfg))
        monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})   # the deck order + tour stamps (2026-09-13)
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
            "name": "Koth Logistics", "industry": "logistics", "mailbox": "office@koth.co.uk"})
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: enterprise)
        monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: subscribed)
        monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: list(boxes))
        monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: list(convos))
        monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: list(bookings))
        monkeypatch.setattr(dashboard.usage, "summarize", lambda *a, **k: {
            "plan": {"label": "Pilot"}, "emails": 29, "allowance": 200, "pct": 14.5,
            "over": False, "warn": False})
        monkeypatch.setattr(dashboard.trial, "evaluate", lambda *a, **k: {
            "status": "active", "active": True, "days_remaining": 11,
            "emails_used": 29, "cap": 100, "emails_remaining": 71})
        monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: {"ok": True})


def test_a_booking_on_the_day_reaches_the_strip_without_tripping_the_lens(client, monkeypatch):
    # Live 2026-09-21 (Little Paws): Today 500'd with UnboundLocalError on the
    # People lens the first day the rig had a booking ON the day — the strip's
    # delegation filter ran twenty lines above where the lens was read, and only
    # a non-empty day evaluates the filter, so every empty day had passed.
    from datetime import datetime
    from app import scheduling
    today = datetime.now(scheduling.DIARY_TZ).date().isoformat()
    Env(monkeypatch, bookings=[{"id": "b1", "booking_ref": "BK-1014", "booking_date": today,
                                "window": "09:00", "start_block": 0, "status": "confirmed",
                                "summary": "Trial day for Mabel", "contact": "a@x.com"}])
    r = client.get("/app/")
    assert r.status_code == 200
    assert "Today's diary" in r.text


def test_home_is_the_today_dashboard(client, monkeypatch):
    # Flight Deck (2026-08-31): Today is a deck of person cards — one card per
    # person, whoever needs you next on top; the panels-by-feature layout died.
    Env(monkeypatch)
    r = client.get("/app/")
    assert r.status_code == 200
    assert "Koth Logistics" in r.text                 # profile strip
    assert "Who needs you next" in r.text
    # the deck's material is the shared .glass-card (base.html); .deck-card
    # keeps only the sticky-pile layout (unified-style pass, 2026-08-31)
    assert 'class="deck-card glass-card"' in r.text
    # identity display: named contact's card carries name + company, address as
    # the tooltip; the unnamed one gets a card under the bare address
    assert "Draft ready" in r.text
    assert "Sarah" in r.text and "Acme Ltd" in r.text and 'title="a@x.com"' in r.text
    assert "b@x.com" in r.text
    assert "c@x.com" not in r.text                    # decided conversations stay out
    assert "2 drafts waiting" not in r.text           # the brief says it now (tests/test_today_header_row.py)
    assert "trial emails used · 11 days left" in r.text   # the countdown sits on the usage tile now (c0d215f: the plan line gave way to the tagline)
    assert "29" in r.text and "200" in r.text         # usage meter
    assert "Diary" in r.text                          # scheduling-only tenant's board card


def test_every_waiting_draft_gets_a_card_including_legacy_rows(client, monkeypatch):
    # Mailbox grouping dissolved into the deck: what matters is that EVERY
    # waiting draft surfaces as a person card — including legacy rows with a
    # null mailbox_id, which used to need the default-box fallback to appear.
    Env(monkeypatch)
    html = client.get("/app/").text
    assert "Sarah" in html                                   # named contact's card
    assert "b@x.com" in html                                 # legacy null-mailbox draft
    assert html.count("Draft ready") >= 2                    # both surface as facts


def test_home_all_clear_when_nothing_pending(client, monkeypatch):
    Env(monkeypatch, convos=[{"contact": "c@x.com", "state": "sent", "mailbox_id": "m1"}])
    html = client.get("/app/").text
    assert "All clear" in html and "Draft ready" not in html


def test_todays_diary_strip_owns_the_day_for_office_tenants(client, monkeypatch):
    # scheduling-only + intake done → the full-width strip carries the day and
    # replaces the count card ("Today's diary" header renders open OR closed)
    Env(monkeypatch)
    html = client.get("/app/").text
    assert "Today's diary" in html
    assert ">Today</h2>" not in html                  # count card stays hidden
    # a vertical tenant's day lives on its own board — count card, no strip
    Env(monkeypatch, modules={"scheduling", "tms"})
    html = client.get("/app/").text
    assert "Today's diary" not in html and "Transport board" in html
    # a diary still blocked on intake set-up keeps the count card too
    Env(monkeypatch, cfg={"windows": "slots", "intake_configured_at": None})
    html = client.get("/app/").text
    assert "Today's diary" not in html and "Diary" in html


def test_board_card_prefers_the_verticals_surface(client, monkeypatch):
    Env(monkeypatch, modules={"scheduling", "tms"},
        bookings=[{"booking_date": "2000-01-01", "status": "confirmed"}])  # not today → 0
    html = client.get("/app/").text
    assert "Transport board" in html
    Env(monkeypatch, modules={"scheduling", "fsm"})
    assert "Field OS · Trades" in client.get("/app/").text


def test_modules_hub_lives_at_its_own_page(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/modules")
    assert r.status_code == 200
    assert "Email Automation" in r.text and "Field OS · Trades" in r.text
    # and the dashboard no longer renders the hub cards
    assert "Email Automation" not in client.get("/app/").text


def test_mailboxes_nav_follows_the_entitlement_not_enterprise(client, monkeypatch):
    """The door lives on Settings since the home plan line gave way to the
    tagline (c0d215f, Kris 2026-09-13); the entitlement rule is unchanged."""
    from tests.test_settings_fold import _env as settings_env
    def _at(subscribed, enterprise):
        settings_env(monkeypatch)
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: enterprise)
        monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: subscribed)
        return client.get("/app/settings").text
    assert 'href="/app/mailboxes"' in _at(True, False)          # paid plan → visible
    assert 'href="/app/mailboxes"' not in _at(False, False)     # pilot → hidden
    assert 'href="/app/mailboxes"' in _at(False, True)          # enterprise → visible

def test_today_never_drags_sideways():
    """Kris 2026-09-20: "I am able to drag the screen left & right for no reason."
    Two culprits on a phone: the deck stage scrolled sideways for a link a few
    pixels past its edge, and the diary rail scrolled for one closed-day
    sentence wider than the screen. Chips on the rail may still scroll."""
    from pathlib import Path
    home = (Path(__file__).resolve().parents[1] / "src/app/templates/app/home.html").read_text(encoding="utf-8")
    assert ".deck-stage { isolation: isolate; overflow-x: hidden; }" in home
    assert '<span class="min-w-0 truncate text-xs text-slate-500">Closed today' in home
    assert '<span class="flex-none text-xs text-slate-500">Closed today' not in home
