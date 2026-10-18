"""Somebody is told when the product stops working.

Kris 2026-09-04: "do the health notifications — mailbox disconnect is the big
one." Before this a dead OAuth grant was invisible: the Gmail poller and the
calendar sync backed off to six-hourly behind ONE log line, and the Graph
subscription renewer swallowed its exceptions outright — so a Microsoft
mailbox's webhook expired and auto-draft stopped inside three days, silently.

Pins the five properties that make an alert about the product trustworthy:

  * a blip is not an outage — nothing is said until a connection has been
    failing continuously for GRACE, so the alert never cries wolf;
  * the clock is in the DATABASE, not sync_backoff's process memory: a Render
    redeploy must not reset an hour-old outage back to zero, and "we already
    told them" must survive a restart or every deploy re-buzzes;
  * told once — a mailbox failing for a week is one notification, not a sweep's
    worth every six hours;
  * recovery is news, and it clears the record, so the NEXT failure alerts;
  * every bookkeeping failure is swallowed — health runs inside the loops that
    exist to stop one bad connection killing the others.

Run: python -m pytest tests/test_health_notifications.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import db, health, notify_prefs, push

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def rig(monkeypatch):
    """One connection in a dict, plus a recorder for what reached the owner."""
    state = {
        "row": {"id": "c1", "tenant_id": "t1", "address": "hello@acme.co.uk",
                "provider": "google", "sync_failing_since": None,
                "sync_error": None, "health_alerted_at": None},
        "pushes": [],
        "now": NOW,
    }
    monkeypatch.setattr(db, "get_connection_health",
                        lambda cid, **k: dict(state["row"]) if cid == "c1" else None)

    def _set(cid, *, failing_since, error, alerted_at, **k):
        state["row"].update(sync_failing_since=failing_since, sync_error=error,
                            health_alerted_at=alerted_at)
    monkeypatch.setattr(db, "set_connection_health", _set)
    monkeypatch.setattr(push, "notify",
                        lambda tid, **kw: state["pushes"].append((tid, kw)))
    monkeypatch.setattr(health, "_now", lambda: state["now"])
    return state


def _advance(rig, **kw):
    rig["now"] = rig["now"] + timedelta(**kw)


# ── A blip is not an outage ────────────────────────────────────────────────
def test_the_first_failed_sweep_says_nothing(rig):
    health.record_failure("c1", "mailbox", "401 from gmail")
    assert rig["pushes"] == []
    # …but the clock has started, and the reason is on the row for the page.
    assert rig["row"]["sync_failing_since"] == NOW.isoformat()
    assert rig["row"]["sync_error"] == "401 from gmail"
    assert rig["row"]["health_alerted_at"] is None


def test_a_failure_that_resolves_inside_the_grace_period_is_never_mentioned(rig):
    health.record_failure("c1", "mailbox", "500 from gmail")
    _advance(rig, minutes=20)
    health.record_success("c1", "mailbox")
    assert rig["pushes"] == [], "a provider having a moment is not news"
    assert rig["row"]["sync_failing_since"] is None


def test_an_hour_of_continuous_failure_reaches_the_owner(rig):
    health.record_failure("c1", "mailbox", "401 from gmail")
    _advance(rig, minutes=61)
    health.record_failure("c1", "mailbox", "401 from gmail")
    assert len(rig["pushes"]) == 1
    tenant, kw = rig["pushes"][0]
    assert tenant == "t1"
    assert kw["kind"] == "mailbox_disconnected"
    assert "hello@acme.co.uk" in kw["body"]
    assert "an hour" in kw["body"]
    assert kw["url"] == "/app/settings"


# ── Told once ──────────────────────────────────────────────────────────────
def test_a_mailbox_dead_for_a_week_is_one_notification(rig):
    health.record_failure("c1", "mailbox", "401")
    for _ in range(30):
        _advance(rig, hours=6)
        health.record_failure("c1", "mailbox", "401")
    assert len(rig["pushes"]) == 1


def test_the_streak_survives_a_restart_because_it_is_in_the_database(rig):
    """sync_backoff's state is per-process and Render redeploys often enough to
    hide an hour-long outage forever. The streak start is read back off the row
    every time, so the clock measures the outage and not this process."""
    health.record_failure("c1", "mailbox", "401")
    started = rig["row"]["sync_failing_since"]
    _advance(rig, minutes=90)
    health.record_failure("c1", "mailbox", "401")            # a "fresh process"
    assert rig["row"]["sync_failing_since"] == started
    assert len(rig["pushes"]) == 1


def test_the_alert_is_stamped_before_it_is_sent(rig, monkeypatch):
    """The mark_booking_reminded ordering: a crash here costs one alert, where
    the other order buzzes the same dead mailbox on every sweep forever."""
    seen = {}

    def _explode(tid, **kw):
        seen["alerted_at"] = rig["row"]["health_alerted_at"]
        raise RuntimeError("push service down")
    monkeypatch.setattr(push, "notify", _explode)
    health.record_failure("c1", "mailbox", "401")
    _advance(rig, minutes=61)
    health.record_failure("c1", "mailbox", "401")
    assert seen["alerted_at"], "stamped before the send was attempted"


# ── Recovery ───────────────────────────────────────────────────────────────
def test_a_reconnect_says_so_and_clears_the_record(rig):
    health.record_failure("c1", "mailbox", "401")
    _advance(rig, minutes=61)
    health.record_failure("c1", "mailbox", "401")
    health.record_success("c1", "mailbox")
    assert [kw["kind"] for _, kw in rig["pushes"]] == \
        ["mailbox_disconnected", "mailbox_reconnected"]
    assert rig["row"]["sync_failing_since"] is None
    assert rig["row"]["health_alerted_at"] is None
    assert rig["row"]["sync_error"] is None


def test_the_next_failure_alerts_again_from_a_clean_slate(rig):
    for _ in range(2):
        health.record_failure("c1", "mailbox", "401")
        _advance(rig, minutes=61)
        health.record_failure("c1", "mailbox", "401")
        health.record_success("c1", "mailbox")
    assert [kw["kind"] for _, kw in rig["pushes"]] == [
        "mailbox_disconnected", "mailbox_reconnected",
        "mailbox_disconnected", "mailbox_reconnected"]


def test_a_healthy_sweep_writes_nothing_at_all(rig, monkeypatch):
    """The common case, run every couple of minutes by the poller: one narrow
    read and no write."""
    writes = []
    monkeypatch.setattr(db, "set_connection_health",
                        lambda *a, **k: writes.append(a))
    health.record_success("c1", "mailbox")
    assert writes == [] and rig["pushes"] == []


def test_a_recovery_nobody_was_warned_about_is_not_announced(rig):
    """Failing for 20 minutes then recovering is not a story with two halves."""
    health.record_failure("c1", "mailbox", "500")
    _advance(rig, minutes=20)
    health.record_success("c1", "mailbox")
    assert rig["pushes"] == []


# ── Never at the cost of the sweep ─────────────────────────────────────────
@pytest.mark.parametrize("call", [
    lambda: health.record_failure("c1", "mailbox", "401"),
    lambda: health.record_success("c1", "mailbox")])
def test_a_health_write_that_fails_never_reaches_the_caller(rig, monkeypatch, call):
    """These run inside the loops that exist to stop one bad connection killing
    the others. Raising here would defeat the thing it is bolted to."""
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(db, "get_connection_health", _boom)
    call()                                                   # must not raise


def test_a_connection_that_no_longer_exists_is_a_no_op(rig):
    health.record_failure("gone", "mailbox", "401")
    assert rig["pushes"] == []


# ── How long it has been broken, in words ──────────────────────────────────
@pytest.mark.parametrize("delta, reads", [
    ({"hours": 1}, "an hour"), ({"hours": 5}, "5 hours"),
    ({"hours": 30}, "a day"), ({"days": 3}, "3 days")])
def test_the_outage_is_described_the_way_a_person_would(delta, reads):
    assert health._for_how_long(NOW, NOW + timedelta(**delta)) == reads


# ── The calendar is a degradation, not a stoppage ──────────────────────────
def test_the_calendar_says_something_different_from_the_mailbox(rig):
    health.record_failure("c1", "calendar", "403")
    _advance(rig, minutes=61)
    health.record_failure("c1", "calendar", "403")
    _, kw = rig["pushes"][0]
    assert kw["kind"] == "calendar_sync_failing"
    assert "diary" in kw["body"]
    assert "no email is being answered" not in kw["body"].lower()


# ── The page strip ─────────────────────────────────────────────────────────
def test_the_strip_waits_for_the_same_grace_as_the_notification():
    """Two thresholds would put a scary red strip on Today for a blip the owner
    is never told about — which teaches them to ignore the one that matters."""
    fresh = {"address": "a@x.co",
             "sync_failing_since": (NOW - timedelta(minutes=20)).isoformat()}
    old = {"address": "b@x.co",
           "sync_failing_since": (NOW - timedelta(hours=3)).isoformat()}
    got = health.visible_problems([fresh, old], NOW)
    assert [p["address"] for p in got] == ["b@x.co"]
    assert got[0]["how_long"] == "3 hours"


def test_a_row_with_no_streak_is_not_a_problem():
    assert health.visible_problems([{"address": "a@x.co",
                                     "sync_failing_since": None}], NOW) == []


def test_an_unreadable_streak_stamp_is_ignored_rather_than_guessed():
    assert health.visible_problems([{"address": "a@x.co",
                                     "sync_failing_since": "whenever"}], NOW) == []


# ── The family ─────────────────────────────────────────────────────────────
def test_problems_cannot_be_switched_off():
    """Every other family takes never. This one buys a week of silent downtime
    the operator finds out about from a customer, so it does not offer it."""
    assert notify_prefs.modes_for("notify_problems") == (notify_prefs.NOW,
                                                         notify_prefs.BRIEF)
    forged = {"notify_problems": "never"}
    assert notify_prefs.mode(forged, "mailbox_disconnected") == notify_prefs.NOW
    assert notify_prefs.should_push(forged, "mailbox_disconnected") is True


def test_problems_can_still_be_held_for_the_morning():
    """A real preference, not an off switch: a mailbox that dies at 3am is
    mentioned at 07:30 rather than buzzing the bedside table."""
    held = {"notify_problems": "brief"}
    assert notify_prefs.should_push(held, "mailbox_disconnected") is False
    assert notify_prefs.belongs_in_brief(held, "mailbox_disconnected") is True


def test_every_health_kind_the_app_sends_is_in_the_family():
    kinds = notify_prefs.FAMILIES["notify_problems"]["kinds"]
    sent = {spec["kind"] for spec in health.SURFACES.values()}
    sent.add("mailbox_reconnected")
    sent.add("email_cap_reached")
    assert sent <= kinds, "a health kind nobody can find in Settings: %s" % (
        sorted(sent - kinds))


def test_every_family_still_offers_at_least_two_settings():
    for col in notify_prefs.FAMILIES:
        assert len(notify_prefs.modes_for(col)) >= 2, col
        assert notify_prefs.NOW in notify_prefs.modes_for(col), col


def test_every_family_has_a_column_to_be_saved_into():
    """Caught for real on the way to main: notify_problems existed in FAMILIES
    and nowhere in db/schema.sql. Delivery was fine (an unset family reads
    "now"), but the settings handler writes every family the form rendered, so
    saving ANY notification setting would have 500'd for every tenant.

    A family key IS a tenant_config column by construction, so the schema is
    where that claim gets checked."""
    schema = open("db/schema.sql", encoding="utf-8").read()
    missing = [col for col in notify_prefs.FAMILIES
               if "add column if not exists %s " % col not in schema]
    assert not missing, "families with no column in db/schema.sql: %s" % missing


# ── The three paths that go dark, each wired ───────────────────────────────
# Every one of these was silent before. The tests are here rather than in each
# sweep's own file because what is being pinned is the same thing three times:
# the sweep tells health, so somebody eventually hears about it.
@pytest.fixture
def calls(monkeypatch):
    seen = {"fail": [], "ok": []}
    monkeypatch.setattr(health, "record_failure",
                        lambda cid, s, r: seen["fail"].append((cid, s)))
    monkeypatch.setattr(health, "record_success",
                        lambda cid, s: seen["ok"].append((cid, s)))
    return seen


def test_the_gmail_poller_reports_a_dead_grant(monkeypatch, calls):
    import asyncio

    import httpx

    from app import sync_backoff
    from app.jobs import poll_gmail

    async def _refuse(conn):
        req = httpx.Request("GET", "https://gmail.googleapis.com/x")
        raise httpx.HTTPStatusError("401", request=req,
                                    response=httpx.Response(401, request=req))
    monkeypatch.setattr(poll_gmail.db, "list_google_connections",
                        lambda: [{"id": "c1", "tenant_id": "t1"}])
    monkeypatch.setattr(poll_gmail, "poll_connection", _refuse)
    sync_backoff._state.clear()
    asyncio.run(poll_gmail.poll_once())
    assert calls["fail"] == [("c1", "mailbox")]


def test_the_gmail_poller_reports_a_grant_that_came_back(monkeypatch, calls):
    import asyncio

    from app import sync_backoff
    from app.jobs import poll_gmail

    async def _fine(conn):
        return 0
    monkeypatch.setattr(poll_gmail.db, "list_google_connections",
                        lambda: [{"id": "c1", "tenant_id": "t1"}])
    monkeypatch.setattr(poll_gmail, "poll_connection", _fine)
    sync_backoff._state.clear()
    asyncio.run(poll_gmail.poll_once())
    assert calls["ok"] == [("c1", "mailbox")]


def test_the_calendar_sync_reports_its_own_surface(monkeypatch, calls):
    import asyncio

    import httpx

    from app import calendar_sync, sync_backoff

    async def _refuse(conn):
        req = httpx.Request("GET", "https://www.googleapis.com/calendar/v3/x")
        raise httpx.HTTPStatusError("403", request=req,
                                    response=httpx.Response(403, request=req))
    monkeypatch.setattr(calendar_sync.db, "list_calendar_connections",
                        lambda: [{"id": "c1", "tenant_id": "t1", "address": "a@x.co"}])
    monkeypatch.setattr(calendar_sync, "sync_connection", _refuse)
    sync_backoff._state.clear()
    asyncio.run(calendar_sync.sync_all())
    assert calls["fail"] == [("c1", "calendar")], \
        "a calendar failure must not read as the mailbox being dead"


def test_the_graph_renewer_probes_the_grant_on_every_sweep(monkeypatch, calls):
    """The worst of the three. A Microsoft mailbox has no poller — its webhook
    keeps looking fine while the grant behind it is dead, and renewal is only
    attempted within 24h of expiry. So the token call moved ahead of the
    not-due skip: it is the only regular liveness probe the mailbox gets."""
    import asyncio
    from datetime import datetime as _dt

    from app.jobs import renew_subscriptions as rs

    far_future = (_dt.now(timezone.utc) + timedelta(days=3)).isoformat()
    conn = {"id": "c1", "tenant_id": "t1", "refresh_token": "r",
            "subscription_id": "s1", "sub_expires": far_future}
    monkeypatch.setattr(rs, "list_all_connections", lambda: [dict(conn)])
    monkeypatch.setattr(rs.mailbox, "connection_provider",
                        lambda *a, **k: "microsoft")

    async def _dead(*a, **k):
        raise RuntimeError("invalid_grant")
    monkeypatch.setattr(rs.mailbox, "valid_access_token", _dead)
    asyncio.run(rs._run())
    assert calls["fail"] == [("c1", "mailbox")], \
        "a healthy-looking subscription must not hide a dead grant"


def test_a_healthy_microsoft_mailbox_is_recorded_well_and_still_skipped(
        monkeypatch, calls):
    import asyncio
    from datetime import datetime as _dt

    from app.jobs import renew_subscriptions as rs

    far_future = (_dt.now(timezone.utc) + timedelta(days=3)).isoformat()
    monkeypatch.setattr(rs, "list_all_connections", lambda: [{
        "id": "c1", "tenant_id": "t1", "refresh_token": "r",
        "subscription_id": "s1", "sub_expires": far_future}])
    monkeypatch.setattr(rs.mailbox, "connection_provider",
                        lambda *a, **k: "microsoft")

    async def _fine(*a, **k):
        return "token"
    monkeypatch.setattr(rs.mailbox, "valid_access_token", _fine)

    def _never(*a, **k):
        raise AssertionError("a subscription days from expiry must not be renewed")
    monkeypatch.setattr(rs.graph, "renew_subscription", _never)
    result = asyncio.run(rs._run())
    assert calls["ok"] == [("c1", "mailbox")]
    assert result["skipped"] == 1


# ── The strip on Today ─────────────────────────────────────────────────────
# The push is half the answer. The other half is that the page they open says
# it: a deck full of people waiting for replies is a lie while the mailbox
# those replies come from is dead.
@pytest.fixture
def today(monkeypatch):
    from fastapi.testclient import TestClient

    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from tests.test_home_dashboard import Env

    Env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])

    def _make(rows):
        monkeypatch.setattr(dashboard.db, "list_unhealthy_connections",
                            lambda *a, **k: list(rows))
        app.dependency_overrides[require_onboarded] = \
            lambda: TenantCtx(tenant_id="t1", client=None)
        return TestClient(app).get("/app/").text
    yield _make
    app.dependency_overrides.clear()


def _broken(hours=3, address="hello@acme.co.uk"):
    return {"id": "c1", "address": address, "provider": "google",
            "sync_failing_since": (datetime.now(timezone.utc)
                                   - timedelta(hours=hours)).isoformat(),
            "sync_error": "401 from gmail.googleapis.com"}


def test_today_says_the_mailbox_has_stopped(today):
    html = today([_broken()])
    assert "hello@acme.co.uk hasn&#39;t been readable for 3 hours" in html
    assert "No email is being answered" in html
    assert "Reconnect" in html


def test_a_healthy_tenant_sees_no_strip(today):
    assert "No email is being answered" not in today([])


def test_the_strip_is_not_shown_for_a_blip(today):
    assert "No email is being answered" not in today([_broken(hours=0)])


def test_a_health_read_that_fails_costs_the_strip_never_the_page(today, monkeypatch):
    import app.dashboard as dashboard

    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(dashboard.db, "list_unhealthy_connections", _boom)
    html = today([])
    assert "Who needs you next" in html


# ── The trial cap ──────────────────────────────────────────────────────────
def test_using_up_the_trial_says_so_exactly_once(monkeypatch):
    """The same increment flips trial_status to expired_cap: from the next
    email on, nothing is drafted. Finding that out from a customer asking why
    nobody replied is the failure this closes."""
    from app import trial

    sent = []
    monkeypatch.setattr(push, "notify", lambda tid, **kw: sent.append((tid, kw)))
    counter = {"n": trial.TRIAL_EMAIL_CAP - 1}

    def _inc(tenant_id, cap=0):
        counter["n"] += 1
        return counter["n"]
    monkeypatch.setattr(db, "increment_trial_email", _inc)

    trial.consume("t1")                       # lands exactly on the cap
    trial.consume("t1")                       # past it — already said
    trial.consume("t1")
    assert [kw["kind"] for _, kw in sent] == ["email_cap_reached"]
    assert sent[0][1]["url"] == "/app/usage"


def test_a_metering_failure_never_breaks_a_draft(monkeypatch):
    from app import trial

    def _boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(db, "increment_trial_email", _boom)
    trial.consume("t1")                                      # must not raise
