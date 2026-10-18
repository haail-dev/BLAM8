"""Agent Log — the transparency feed (the burger-menu activity page).

What this pins:
  - the pipeline emits a CURATED event set per drafted email: the 'drafted' anchor
    (with the human-notice badges), the 'transport' hand-off (when the base asked the
    TMS desk), and an 'office_booking' for any appointment hold — transport rows are
    NOT double-logged as office bookings
  - _transport_summary turns each hand-off outcome into one honest line + the calendar
    date (set only for a placed hold — that's what lands on the diary)
  - the route renders the feed grouped by day, coerces a bad kind filter, clamps the
    page, and passes the date/kind/search filters through to the query

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import agent, pipeline
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


def _result(deferred=False, missing="", scheduling_requested=False):
    return agent.DraftResult(text="Reply.", usage={}, model="m", deferred=deferred,
                             missing=missing, scheduling_requested=scheduling_requested)


def _recorder(monkeypatch):
    events = []
    monkeypatch.setattr(pipeline.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **kw: events.append(
                            {"tenant_id": tenant_id, "kind": kind, "summary": summary, **kw}))
    return events


EMAIL = {"from_address": "sam@haulage.co", "subject": "Pallet collection"}


# ── the pipeline emission ─────────────────────────────────────────────────────
def test_log_activity_emits_drafted_transport_and_office(monkeypatch):
    events = _recorder(monkeypatch)
    sched_state = {
        "bookings": [
            {"id": "bf-1", "spaces": 6.0, "booking_date": "2026-07-02", "booking_ref": "BK-1024"},
            {"id": "bw-1", "spaces": None, "booking_date": "2026-07-03", "window": "am",
             "booking_ref": "BK-2002"},
        ],
        "transport_result": {"status": "held", "date": "2026-07-02", "spaces": 6,
                             "booking_ref": "BK-1024", "alternatives": [], "ask": "",
                             "branch": "", "assumed": [], "weight_kg": 0, "message": ""},
    }
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(deferred=True, missing="delivery time"),
                                 sched_state, "mb-1", escalated="urgent")

    assert [e["kind"] for e in events] == ["drafted", "transport", "office_booking"]
    drafted, transport, office = events
    # anchor + badges
    assert "Drafted a reply to sam@haulage.co" in drafted["summary"]
    assert drafted["email_log_id"] == "log-1" and drafted["mailbox_id"] == "mb-1"
    assert drafted["detail"]["knowledge_gap"] == "delivery time"
    assert drafted["detail"]["escalated"] == "urgent"
    # transport hand-off — held: carries the ref, the calendar date, the transport booking
    assert "held BK-1024" in transport["summary"]
    assert transport["event_date"] == "2026-07-02"
    assert transport["booking_ref"] == "BK-1024" and transport["booking_id"] == "bf-1"
    # office booking — the people row only, with its own date/ref
    assert "appointment" in office["summary"].lower()
    assert office["event_date"] == "2026-07-03" and office["booking_ref"] == "BK-2002"


def test_log_activity_no_modules_is_just_the_drafted_anchor(monkeypatch):
    events = _recorder(monkeypatch)
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(scheduling_requested=True),
                                 {"bookings": []}, None)
    assert [e["kind"] for e in events] == ["drafted"]
    assert events[0]["detail"]["scheduling_requested"] is True


def test_redraft_anchor_says_re_drafted(monkeypatch):
    events = _recorder(monkeypatch)
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(), {"bookings": []}, None, redraft=True)
    assert events[0]["summary"].startswith("Re-drafted")


@pytest.mark.parametrize("tr,expect_text,expect_date", [
    ({"status": "held", "booking_ref": "BK-9", "date": "2026-07-02", "spaces": 6},
     "held BK-9", "2026-07-02"),
    ({"status": "offered", "alternatives": ["2026-07-03", "2026-07-04"]},
     "offered 2026-07-03, 2026-07-04", None),
    ({"status": "needs_info", "ask": "Which branch — Leeds or Manchester?"},
     "needs info: Which branch", None),
    ({"status": "no_fit"}, "won't fit", None),
    ({"status": "error"}, "temporarily unavailable", None),
])
def test_transport_summary_per_status(tr, expect_text, expect_date):
    summary, date = pipeline._transport_summary(tr)
    assert expect_text in summary
    assert date == expect_date


# ── the route ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ctx_env(monkeypatch, rows, members=None):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling", "tms"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    captured = {}

    def fake_list(tenant_id, *, on_date=None, kind=None, q=None, who=None,
                  limit=60, offset=0, client=None):
        captured.update(on_date=on_date, kind=kind, q=q, who=who, limit=limit, offset=offset)
        return list(rows)
    monkeypatch.setattr(dashboard.db, "list_agent_events", fake_list)
    # Who did it (People slice 4): the page offers the filter once a business
    # has more than one pair of hands, so the list has to be stubbed.
    monkeypatch.setattr(dashboard.db, "list_members", lambda *a, **k: list(members or []))
    return captured


ROWS = [
    {"id": "e1", "kind": "transport", "direction": "inbound", "contact": "sam@haulage.co",
     "subject": "Pallet collection", "summary": "Asked the transport desk — held BK-1024 for 2026-07-02",
     "event_date": "2026-07-02", "booking_ref": "BK-1024", "email_log_id": "log-1",
     "booking_id": "bf-1", "detail": {"status": "held"}, "created_at": "2026-06-29T10:15:00+00:00",
     "mailbox_id": None},
    {"id": "e2", "kind": "drafted", "direction": "inbound", "contact": "sam@haulage.co",
     "subject": "Pallet collection", "summary": "Drafted a reply to sam@haulage.co re 'Pallet collection'",
     "event_date": None, "booking_ref": None, "email_log_id": "log-1", "booking_id": None,
     "detail": {"knowledge_gap": "delivery time"}, "created_at": "2026-06-29T10:14:00+00:00",
     "mailbox_id": None},
]


def test_route_renders_feed_grouped(client, monkeypatch):
    _ctx_env(monkeypatch, ROWS)
    r = client.get("/app/agent-log")
    assert r.status_code == 200
    assert "held BK-1024" in r.text and "Drafted a reply" in r.text
    assert "29th Jun 26" in r.text                     # the day-group header, read as a date
    assert "2026-06-29" not in r.text                  # never the raw ISO (Kris 2026-09-01)
    assert "Knowledge gap" in r.text                   # the badge from detail
    assert "/app/conversations/view?contact=" in r.text  # the conversation link
    assert "/app/board?day=2026-07-02" in r.text       # the calendar trace link


def test_route_empty_state(client, monkeypatch):
    _ctx_env(monkeypatch, [])
    r = client.get("/app/agent-log")
    assert r.status_code == 200
    assert "No agent activity yet" in r.text


def test_route_passes_filters_and_sanitizes(client, monkeypatch):
    captured = _ctx_env(monkeypatch, [])
    r = client.get("/app/agent-log?date=2026-07-02&kind=bogus&q=leeds&page=-3")
    assert r.status_code == 200
    assert captured["on_date"] == "2026-07-02"
    assert captured["kind"] is None                    # bad kind coerced to "all"
    assert captured["q"] == "leeds"
    assert captured["offset"] == 0                     # page clamped to >= 0


def test_route_valid_kind_and_paging(client, monkeypatch):
    captured = _ctx_env(monkeypatch, [])
    client.get("/app/agent-log?kind=transport&page=2")
    assert captured["kind"] == "transport"
    assert captured["offset"] == 2 * dashboard.AGENT_LOG_PAGE


def test_agent_log_page_renders_with_its_heading(client, monkeypatch):
    # Its nav door moved to the Settings Workspace hub (2026-08-30 menu diet)
    # — test_money.test_refer_a_business_entry_is_on_the_settings_hub covers
    # the hub itself; this pins the page.
    _ctx_env(monkeypatch, [])
    r = client.get("/app/agent-log")
    assert r.status_code == 200 and "Agent log" in r.text


# ── "no sign-off set" (src/app/sign_off.py: the sign-off is appended by code) ─────
def test_drafted_event_flags_a_missing_sign_off(monkeypatch):
    """No sign-off applied to a non-empty email draft = a badge on the anchor event.
    It is information only: nothing here can hold or block a draft."""
    events = _recorder(monkeypatch)
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(), {"bookings": []}, None)
    assert events[0]["detail"]["sign_off_missing"] is True
    events.clear()
    # A sign-off was applied: no badge.
    signed = _result()._replace(sign_off="Thanks")
    pipeline._log_agent_activity("t1", "log-1", EMAIL, signed, {"bookings": []}, None)
    assert "sign_off_missing" not in events[0]["detail"]
    events.clear()
    # Nothing was drafted (a hold): nothing to announce.
    held = agent.DraftResult(text="", usage={}, model="m")
    pipeline._log_agent_activity("t1", "log-1", EMAIL, held, {"bookings": []}, None)
    assert "sign_off_missing" not in events[0]["detail"]
    events.clear()
    # SMS never carries an email sign-off.
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(), {"bookings": []}, None,
                                 channel="sms")
    assert "sign_off_missing" not in events[0]["detail"]
    events.clear()
    # A redraft is announced the same way.
    pipeline._log_agent_activity("t1", "log-1", EMAIL, _result(), {"bookings": []}, None,
                                 redraft=True)
    assert events[0]["detail"]["sign_off_missing"] is True


def test_agent_log_page_shows_the_no_sign_off_badge(client, monkeypatch):
    row = {"id": "e3", "kind": "drafted", "direction": "inbound", "contact": "sam@haulage.co",
           "subject": "Pallet collection", "summary": "Drafted a reply to sam@haulage.co",
           "event_date": None, "booking_ref": None, "email_log_id": "log-3", "booking_id": None,
           "detail": {"sign_off_missing": True}, "created_at": "2026-06-29T10:16:00+00:00",
           "mailbox_id": None}
    _ctx_env(monkeypatch, [row])
    html = client.get("/app/agent-log").text
    assert "No sign-off set" in html
    assert 'href="/app/settings"' in html
    # An ordinary drafted row carries no such badge.
    _ctx_env(monkeypatch, ROWS)
    assert "No sign-off set" not in client.get("/app/agent-log").text
