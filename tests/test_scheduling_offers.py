"""Smart booking — offered-slot holds (docs/modules/module-smart-booking-offers.md).

What this pins:
  - offer_slots reserves each listed slot as a REAL 'offered' row with a TTL
    (exclusive offers — they consume capacity via ACTIVE_STATUSES), max 3, and
    returns a code-written deadline sentence covering every successful offer
  - offer_expiry: tenant TTL, capped at real opening − notice and an applied
    last call; new businesses have no last-call default
  - one booking action per email: a hold OR one offer set, never both
  - acceptance: hold_booking on a slot already offered to this contact FLIPS
    the offered row (no fresh capacity check — it already holds the slot) and
    releases the siblings; a fresh hold releases any dangling offer set
  - resolve asymmetry lives in test_scheduling_pipeline (send ≠ confirm offers)
  - expired offers vanish from every db read the moment the deadline passes
  - the diary prompt rules carry the three ask types + clarify-first vague flow
  - period summaries (2026-08-02): a multi-day check_availability result carries
    day_summary (per-day counts, informational — no holds); the prompt rules
    tell the agent to answer a period ask with those counts, then day → offers

Run: python -m pytest tests/test_scheduling_offers.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import date, datetime, timedelta, timezone

import pytest

from app import agent, db, scheduling
from app.scheduling import DIARY_TZ, MAX_OFFER_SLOTS, make_executor, offer_expiry

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday


@pytest.fixture
def no_db(monkeypatch):
    """Default diary config (day windows, 24h notice), no bookings, no dangling
    offers; the capacity RPC always succeeds and records its kwargs."""
    calls = {"holds": [], "released": [], "accepted": []}
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_blocks", lambda *a, **k: None)

    def fake_release(tid, contact, exclude_ids=None, **k):
        calls["released"].append((contact, tuple(exclude_ids or [])))
        return 0
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", fake_release)

    def fake_hold(tid, d, w, s, **kw):
        row = {"id": f"b-{len(calls['holds']) + 1}", "booking_ref": f"BK-{len(calls['holds']) + 1}",
               "booking_date": d, "window": w,
               "status": kw.pop("status", None) or "proposed", **kw}
        calls["holds"].append(row)
        return row
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", fake_hold)
    return calls


# ── offer_expiry (pure) ───────────────────────────────────────────────────────
def test_offer_expiry_uses_tenant_ttl():
    cfg = carried_config({"offer_ttl_hours": 2, "min_notice_hours": 0})
    exp = offer_expiry(cfg, date(2026, 6, 30), "day", NOW)
    assert exp == NOW + timedelta(hours=2)


def test_offer_expiry_default_24h():
    exp = offer_expiry(carried_config({"min_notice_hours": 0}), date(2026, 6, 30), "day", NOW)
    assert exp == NOW + timedelta(hours=24)


def test_offer_expiry_capped_at_notice_before_start():
    # Slot starts Thu 14:00, notice 24h → the offer releases Wed 14:00 even
    # though the TTL alone would run to Wed 10:00+24h = Wed 10:00... TTL=48h
    # to force the cap to win.
    cfg = carried_config({"offer_ttl_hours": 48, "min_notice_hours": 24})
    exp = offer_expiry(cfg, date(2026, 6, 18), "14:00", NOW)
    assert exp == datetime(2026, 6, 17, 14, 0, tzinfo=DIARY_TZ)


def test_offer_expiry_near_boundary_keeps_notice_cap():
    # A notice cap an hour away is real: do not extend it to the slot start.
    cfg = carried_config({"offer_ttl_hours": 48, "min_notice_hours": 24})
    exp = offer_expiry(cfg, date(2026, 6, 17), "11:00", NOW)
    assert exp == datetime(2026, 6, 16, 11, 0, tzinfo=DIARY_TZ)


# ── offer_slots via the executor (windows mode) ──────────────────────────────
def test_offer_slots_places_offered_rows_with_deadline(no_db):
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    out = execute("offer_slots", {
        "slots": [{"date": "2026-06-18", "window": "day"},
                  {"date": "2026-06-19", "window": "day"}],
        "summary": "Boiler service — Mrs Hart",
    })
    assert [o["date"] for o in out["offered"]] == ["2026-06-18", "2026-06-19"]
    assert out["status"] == "offered"
    assert out["hold_until_phrase"] in out["message"]
    assert len(state["bookings"]) == 2
    for row in no_db["holds"]:
        assert row["status"] == "offered"
        assert row["expires_at"]        # every offered row carries its TTL
        assert row["contact"] == "ops@acme.co.uk"
    # The legacy hold_until field remains the earliest expiry for consumers
    # that need a trigger. Customer text uses deadline_line, which names each
    # stored expiry. The new notice cap uses the real 09:00 opening.
    assert out["hold_until"] == datetime(2026, 6, 17, 9, 0, tzinfo=DIARY_TZ).isoformat()


def test_offer_slots_supersedes_the_contacts_dangling_offers(no_db):
    # "None of those work — do Friday instead" (live e2e catch, 2026-08-31):
    # a NEW offer set releases the contact's old offered trio, excluding the
    # rows just placed — the same rule place_hold already applied.
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    out = execute("offer_slots", {
        "slots": [{"date": "2026-06-19", "window": "day"}],
        "summary": "Boiler service options"})
    assert out.get("offered") or not out.get("error")
    assert no_db["released"] == [("ops@acme.co.uk", ("b-1",))]


def test_offer_slots_all_rejected_releases_nothing(no_db):
    # An all-rejected call must not strip the customer's existing options.
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    execute("offer_slots", {"slots": [{"date": "bad-date", "window": "day"}]})
    assert no_db["released"] == []


def test_offer_slots_caps_at_three(no_db):
    execute, state = make_executor("t1", now=NOW)
    out = execute("offer_slots", {
        "slots": [{"date": "2026-06-18", "window": "day"},
                  {"date": "2026-06-19", "window": "day"},
                  {"date": "2026-06-22", "window": "day"},
                  {"date": "2026-06-23", "window": "day"}],
        "summary": "x",
    })
    assert len(out["offered"]) == MAX_OFFER_SLOTS == 3
    assert len(state["bookings"]) == 3


def test_offer_slots_rejects_bad_slots_but_places_good_ones(no_db):
    execute, state = make_executor("t1", now=NOW)
    out = execute("offer_slots", {
        "slots": [{"date": "2026-06-20", "window": "day"},    # Saturday — closed
                  {"date": "2026-06-18", "window": "day"}],
        "summary": "x",
    })
    assert [o["date"] for o in out["offered"]] == ["2026-06-18"]
    assert out["rejected"][0]["error"] == "closed"
    assert "rejected" in out["message"]
    assert len(state["bookings"]) == 1


def test_offer_slots_nothing_offered(no_db):
    execute, state = make_executor("t1", now=NOW)
    out = execute("offer_slots", {
        "slots": [{"date": "2026-06-20", "window": "day"}], "summary": "x"})
    assert out["error"] == "nothing_offered" and state["bookings"] == []


def test_one_booking_action_per_email(no_db):
    # Offer set first → no hold, no second offer set.
    execute, _ = make_executor("t1", now=NOW)
    assert execute("offer_slots", {"slots": [{"date": "2026-06-18", "window": "day"}],
                                   "summary": "x"})["offered"]
    assert execute("hold_booking", {"date": "2026-06-19", "window": "day",
                                    "summary": "y"})["error"] == "already_offered"
    assert execute("offer_slots", {"slots": [{"date": "2026-06-19", "window": "day"}],
                                   "summary": "y"})["error"] == "already_offered"
    # Hold first → no offer set.
    execute2, _ = make_executor("t1", now=NOW)
    assert execute2("hold_booking", {"date": "2026-06-18", "window": "day",
                                     "summary": "x"})["held"]
    assert execute2("offer_slots", {"slots": [{"date": "2026-06-19", "window": "day"}],
                                    "summary": "y"})["error"] == "already_held"


# ── acceptance: the customer picks an offered slot ───────────────────────────
def test_hold_on_offered_slot_converts_and_releases_siblings(no_db, monkeypatch):
    offered = [{"id": "off-1", "booking_ref": "BK-9", "booking_date": "2026-06-18",
                "window": "day", "status": "offered"},
               {"id": "off-2", "booking_ref": "BK-10", "booking_date": "2026-06-19",
                "window": "day", "status": "offered"}]
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact",
                        lambda *a, **k: offered)

    def fake_accept(bid, tid, **k):
        no_db["accepted"].append(bid)
        return {**offered[0], "status": "proposed", "expires_at": None}
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", fake_accept)

    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x"})
    assert out["held"] is True and out["accepted_offer"] is True
    assert out["booking_ref"] == "BK-9" and out["status"] == "proposed"
    assert no_db["accepted"] == ["off-1"]
    assert no_db["released"] == [("ops@acme.co.uk", ("off-1",))]   # siblings only
    assert no_db["holds"] == []          # no fresh capacity-checked hold placed
    assert state["bookings"][0]["id"] == "off-1"


def test_hold_falls_back_when_offer_expired_under_us(no_db, monkeypatch):
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact",
                        lambda *a, **k: [{"id": "off-1", "booking_date": "2026-06-18",
                                          "window": "day", "status": "offered"}])
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", lambda *a, **k: None)
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x"})
    assert out["held"] is True and "accepted_offer" not in out
    assert len(no_db["holds"]) == 1      # the sweep won the race — fresh hold placed


def test_fresh_hold_releases_dangling_offer_set(no_db):
    # "None of those work — do Friday instead": the new hold supersedes the offers.
    execute, _ = make_executor("t1", contact="ops@acme.co.uk", now=NOW)
    out = execute("hold_booking", {"date": "2026-06-19", "window": "day", "summary": "x"})
    assert out["held"] is True
    assert no_db["released"] == [("ops@acme.co.uk", ())]


# ── expired offers vanish from db reads ──────────────────────────────────────
def test_drop_expired_offers_filters_only_dead_offers():
    past = (datetime.now(DIARY_TZ) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    rows = [{"status": "offered", "expires_at": past},
            {"status": "offered", "expires_at": future},
            {"status": "offered", "expires_at": None},
            {"status": "proposed", "expires_at": past},   # never filtered by expiry
            {"status": "confirmed"}]
    kept = db._drop_expired_offers(rows)
    assert len(kept) == 4 and rows[0] not in kept


def test_expire_offered_bookings_query_shape():
    class Fake:
        def __init__(self):
            self.updates, self.filters = [], []

        def table(self, name):
            self.filters.append(("table", name)); return self

        def update(self, payload):
            self.updates.append(payload); return self

        def eq(self, col, val):
            self.filters.append((col, val)); return self

        def lt(self, col, val):
            self.filters.append(("lt:" + col, val)); return self

        def execute(self):
            return type("Res", (), {"data": [{"id": "x"}]})()

    fake = Fake()
    assert db.expire_offered_bookings(client=fake) == 1
    assert fake.updates[0]["status"] == "cancelled"
    assert ("status", "offered") in fake.filters
    assert any(f[0] == "lt:expires_at" for f in fake.filters)


# ── period availability summaries (2026-08-02) ───────────────────────────────
def test_day_summary_groups_counts_by_day():
    slots = [
        {"date": "2026-06-18", "window": "10:00", "remaining": 1},
        {"date": "2026-06-18", "window": "14:00", "remaining": 2},
        {"date": "2026-06-19", "window": "09:00", "remaining": 1},
    ]
    assert scheduling.day_summary(slots) == [
        {"date": "2026-06-18", "day": "Thursday 18 June", "free": 2},
        {"date": "2026-06-19", "day": "Friday 19 June", "free": 1},
    ]


def test_multi_day_check_carries_day_summary(no_db):
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability",
                  {"date_from": "2026-06-17", "date_to": "2026-06-23"})
    # The summary is DERIVED from the same slots list — one entry per free day,
    # `free` = that day's bookable windows (day-mode default: one per day).
    assert {d["date"] for d in out["day_summary"]} == {s["date"] for s in out["available"]}
    assert all(d["free"] == 1 for d in out["day_summary"])
    assert all(set(d) == {"date", "day", "free"} for d in out["day_summary"])


def test_single_day_check_has_no_day_summary(no_db):
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability",
                  {"date_from": "2026-06-18", "date_to": "2026-06-18"})
    assert out["available"] and "day_summary" not in out


def test_empty_period_has_no_day_summary(no_db):
    # Sat+Sun (closed on the default mon-fri diary): nothing to count — the
    # no-availability message stands alone, no empty summary alongside it.
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability",
                  {"date_from": "2026-06-20", "date_to": "2026-06-21"})
    assert out["available"] == [] and "day_summary" not in out
    assert "message" in out


# ── prompt rules + offered-slots context ─────────────────────────────────────
def test_diary_rules_carry_the_three_ask_types():
    rules = agent._WINDOWS_DIARY_RULES
    assert "offer_slots" in rules
    assert "TWO" in rules and "THREE" in rules
    assert "morning, midday or afternoon" in rules
    assert "deadline_line" in rules
    assert "3 or fewer" in rules          # the scarcity exception to clarify-first
    assert "check and get back to you" in rules


def test_diary_rules_carry_the_period_counts_flow():
    rules = agent._WINDOWS_DIARY_RULES
    assert "day_summary" in rules                  # answer period asks from the tool's counts
    assert "reserve nothing" in rules              # counts are informational — no holds
    assert "EXACTLY as returned" in rules          # say-vs-do: never an invented count


def test_prompt_renders_offered_slots_separately():
    cfg = {"tone": "", "business_name": "Acme"}
    prompt = agent.build_system_prompt(
        cfg, scheduling=True,
        existing_bookings=[
            {"booking_ref": "BK-1", "summary": "Cut", "booking_date": "2026-06-18",
             "window": "10:00", "status": "proposed"},
            {"booking_ref": "BK-2", "summary": "Cut", "booking_date": "2026-06-19",
             "window": "11:00", "status": "offered",
             "expires_at": "2026-06-17T16:00:00+00:00"},
        ])
    assert "<existing_bookings>" in prompt and "BK-1" in prompt
    assert "<offered_slots>" in prompt and "BK-2" in prompt
    # The offered row must NOT sit under the never-book-again rule…
    booked_section = prompt.split("<existing_bookings>")[1].split("</existing_bookings>")[0]
    assert "BK-2" not in booked_section
    # …and the accept instruction + deadline are stated.
    offered_section = prompt.split("<offered_slots>")[1].split("</offered_slots>")[0]
    assert "hold_booking" in offered_section and "held until" in offered_section


# ── Phase 2: the visible diary ───────────────────────────────────────────────
def test_diary_days_stamps_offered_labels_and_counts():
    booked = [
        {"id": "o1", "status": "offered", "booking_date": "2026-06-18", "window": "day",
         "expires_at": (NOW + timedelta(hours=6)).isoformat(),
         "spaces": None, "engineer_id": None},
        {"id": "p1", "status": "proposed", "booking_date": "2026-06-18", "window": "day",
         "spaces": None, "engineer_id": None},
    ]
    days = scheduling.diary_days(None, booked, NOW)
    d = next(x for x in days if x["date"] == "2026-06-18")
    assert d["offered_count"] == 1
    o = next(b for b in d["bookings"] if b["id"] == "o1")
    assert o["offer_countdown"] == "releases in 6h"
    assert o["offer_until_label"]                 # absolute deadline for the tooltip


def test_calendar_weeks_splits_booked_from_offered():
    # "2 booked · 1 offered" honesty: the month cell never counts an option as a booking.
    booked = [
        {"id": "o1", "status": "offered", "booking_date": "2026-06-18", "window": "day",
         "spaces": None, "engineer_id": None},
        {"id": "c1", "status": "confirmed", "booking_date": "2026-06-18", "window": "day",
         "spaces": None, "engineer_id": None},
    ]
    weeks = scheduling.calendar_weeks(None, booked, NOW)
    cell = next(c for w in weeks for c in w["days"] if c["date"] == "2026-06-18")
    assert cell["booked_count"] == 1 and cell["offered_count"] == 1


def test_diary_page_renders_offered_state_and_deep_link(monkeypatch):
    """Full-page render: grey Offered pill + countdown, the day chip, the Release
    button, and the view-conversation deep-link carrying #log-<email_log_id>."""
    from datetime import date as _date
    from fastapi.testclient import TestClient

    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app as fastapi_app
    from app.security import require_csrf

    day = (_date.today() + timedelta(days=2)).isoformat()
    exp = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    cfg = carried_config({"windows": "day", "working_days": "mon,tue,wed,thu,fri,sat,sun",
           "intake_configured_at": "2026-01-01T00:00:00Z"})
    rows = [{"id": "off1", "status": "offered", "booking_date": day, "window": "day",
             "summary": "Boiler service", "contact": "mrs.hart@example.com",
             "source": "email", "email_log_id": "LOG1", "expires_at": exp,
             "spaces": None, "engineer_id": None, "details": None}]

    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m == "scheduling")
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "microsoft"})
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: rows)

    fastapi_app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    fastapi_app.dependency_overrides[require_csrf] = lambda: None
    try:
        r = TestClient(fastapi_app).get("/app/diary")
    finally:
        fastapi_app.dependency_overrides.clear()
    assert r.status_code == 200
    assert "Offered ·" in r.text and "releases in 6h" in r.text
    assert "1 offered" in r.text                   # the day-level grey chip
    assert "#log-LOG1" in r.text                   # exact-conversation deep-link
    assert "Release" in r.text                     # manual release stays available


def test_past_bookings_leave_the_existing_block(monkeypatch):
    # Live 2026-08-31 (Kris: "there is NO booking in the diary, how is it
    # saying there is bookings?"): July's confirmed call was fed to a
    # September draft as an existing booking and the agent asked whether the
    # new request was "separate" from it. Past dates are history — only live
    # bookings ride the never-book-again block. No `today` = no filter.
    rows = [{"booking_ref": "BK-OLD", "summary": "Old call",
             "booking_date": "2026-07-28", "window": "10:00", "status": "confirmed"},
            {"booking_ref": "BK-NEW", "summary": "New call",
             "booking_date": "2026-09-20", "window": "10:00", "status": "confirmed"}]
    p = agent.build_system_prompt({"tone": "", "business_name": "A"}, scheduling=True,
                                  today=date(2026, 9, 1), existing_bookings=rows)
    assert "BK-NEW" in p and "BK-OLD" not in p
    p_unscoped = agent.build_system_prompt({"tone": "", "business_name": "A"},
                                           existing_bookings=rows)
    assert "BK-OLD" in p_unscoped                    # no diary today → unfiltered


def test_prompt_offers_only_skips_existing_bookings_block():
    prompt = agent.build_system_prompt(
        {"tone": "", "business_name": "Acme"}, scheduling=True,
        existing_bookings=[{"booking_ref": "BK-2", "summary": "Cut",
                            "booking_date": "2026-06-19", "window": "11:00",
                            "status": "offered"}])
    assert "<existing_bookings>" not in prompt and "<offered_slots>" in prompt


def test_month_grid_day_states_wear_their_borders(monkeypatch):
    """Kris 2026-08-31: the month cell's BORDER tells the story — offered days
    pulse amber, days with bookings pulse emerald, both at once spins a duo
    ring, and a solid emerald fill is reserved for fully booked."""
    from datetime import date as _date
    from fastapi.testclient import TestClient

    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app as fastapi_app
    from app.security import require_csrf

    base = _date.today() + timedelta(days=14)
    while base.weekday() != 0:                        # a Monday, always working
        base += timedelta(days=1)
    d_off, d_duo, d_full = (base.isoformat(), (base + timedelta(days=1)).isoformat(),
                            (base + timedelta(days=2)).isoformat())
    exp = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    cfg = carried_config({"windows": "slots", "open_time": "09:00", "close_time": "17:00",
           "slot_minutes": 60, "working_days": "mon,tue,wed,thu,fri", "capacity_per_window": 1,
           "intake_configured_at": "2026-01-01T00:00:00Z"})   # capacity 1: eight hourly bookings ARE a full day
    common = {"summary": "s", "contact": "c@x.co", "source": "email",
              "email_log_id": None, "spaces": None, "engineer_id": None,
              "details": None}
    rows = ([{"id": "o1", "status": "offered", "booking_date": d_off,
              "window": "10:00", "expires_at": exp, **common}]
            + [{"id": "d1", "status": "confirmed", "booking_date": d_duo,
                "window": "09:00", **common},
               {"id": "d2", "status": "offered", "booking_date": d_duo,
                "window": "10:00", "expires_at": exp, **common}]
            + [{"id": f"f{i}", "status": "confirmed", "booking_date": d_full,
                "window": f"{9 + i:02d}:00", **common} for i in range(8)])

    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m == "scheduling")
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "microsoft"})
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: rows)

    fastapi_app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    fastapi_app.dependency_overrides[require_csrf] = lambda: None
    try:
        html = TestClient(fastapi_app).get("/app/diary").text
    finally:
        fastapi_app.dependency_overrides.clear()
    assert "cal-day-offered" in html                  # amber pulse, border only
    # Booked and full read off the cell's bar now (Kris 2026-09-13): the duo
    # ring and the solid emerald fill are gone.
    assert "cal-day-duo" not in html and "bg-emerald-600 border-emerald-600" not in html
    assert 'data-bar-type="spaces"' in html and 'style="height:100%"' in html   # the full day's bar at the top
    assert "bg-amber-400/70" in html                  # the offered share of a bar
    assert "bg-rose-50 border-rose-200" not in html   # the old rose Full is gone
    assert "@keyframes cal-pulse-amber" in html       # the styles ride the page


def test_release_logs_the_agent_event(monkeypatch):
    # The supersession is visible in the Agent Log, not only the diary (Kris
    # 2026-08-31: "did the agent release the correct slots? You should be able
    # to find this in the Agent Logs" — until now the release was silent).
    events = []

    class _Q:
        def __getattr__(self, name): return lambda *a, **k: self
        def execute(self):
            return type("R", (), {"data": [
                {"booking_ref": "BK-1039"}, {"booking_ref": "BK-1040"}]})()

    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda s, t: _Q()})())
    monkeypatch.setattr(db, "log_agent_event",
                        lambda tid, kind, summary, **k:
                        events.append((tid, kind, summary, k.get("contact"))))
    n = db.release_offered_for_contact("t1", "kris@x.co")
    assert n == 2
    assert len(events) == 1
    tid, kind, summary, contact = events[0]
    assert (tid, kind, contact) == ("t1", "offers_released", "kris@x.co")
    assert "BK-1039, BK-1040" in summary and "superseded" in summary


def test_release_with_nothing_dangling_stays_silent(monkeypatch):
    events = []

    class _Q:
        def __getattr__(self, name): return lambda *a, **k: self
        def execute(self): return type("R", (), {"data": []})()

    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda s, t: _Q()})())
    monkeypatch.setattr(db, "log_agent_event",
                        lambda *a, **k: events.append(a))
    assert db.release_offered_for_contact("t1", "kris@x.co") == 0
    assert events == []


def test_accepting_an_offer_merges_the_acceptance_details_onto_the_row(no_db, monkeypatch):
    """Live 2026-09-03: BK-1002 was accepted with a richer `details` bag (the
    gate's email fill + a field the customer added since the offer) and the flip
    dropped it — the stored row kept only the offer-time details. The flip now
    merges offer-time + acceptance-time (acceptance wins, blanks never overwrite)."""
    offered = [{"id": "off-1", "booking_ref": "BK-9", "booking_date": "2026-06-18",
                "window": "day", "status": "offered",
                "details": {"Full name": "Kris", "Nature of the work": "Headstone"}}]
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: offered)
    seen = {}

    def fake_accept(bid, tid, **k):
        seen.update(k)
        return {**offered[0], "status": "proposed", "expires_at": None,
                "details": k.get("details")}
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", fake_accept)
    fields = [{"label": "Email address", "required": True, "hint": ""},
              {"label": "Full name", "required": True, "hint": ""},
              {"label": "Nature of the work", "required": False, "hint": ""}]
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Full name": "Kris Deane", "Type of visit": "Consultation",
                                               "Nature of the work": ""}})
    assert out["held"] is True and out["accepted_offer"] is True
    assert seen["details"] == {"Full name": "Kris Deane",            # acceptance wins
                               "Nature of the work": "Headstone",    # blank never overwrites
                               "Type of visit": "Consultation",      # new since the offer
                               "Email address": "ops@acme.co.uk"}    # the gate's own fill
    # and the result tells the agent the email is on file
    assert out["prefilled_from_sender"] == ["Email address"]
    assert "do NOT ask" in out["message"] and "already offered" in out["message"]


def test_offers_run_the_intake_gate_like_holds(no_db):
    """Live 2026-09-03: three slots were OFFERED with no visit type, email or
    permit on them — the gate only bit at acceptance. Offers are exclusive
    reservations, so they gather the same required details a hold does, and a
    sender-filled email is announced the same way."""
    fields = [{"label": "Email address", "required": True, "hint": ""},
              {"label": "Full name", "required": True, "hint": ""}]
    execute, state = make_executor("t1", contact="ops@acme.co.uk", now=NOW, intake_fields=fields)
    out = execute("offer_slots", {"slots": [{"date": "2026-06-18", "window": "day"}],
                                  "summary": "x", "details": {}})
    assert out["error"] == "missing_info" and out["missing"] == ["Full name"]
    assert no_db["holds"] == [] and state["bookings"] == []
    out2 = execute("offer_slots", {"slots": [{"date": "2026-06-18", "window": "day"}],
                                   "summary": "x", "details": {"Full name": "Sam"}})
    assert out2["status"] == "offered" and len(out2["offered"]) == 1
    assert out2["prefilled_from_sender"] == ["Email address"] and "do NOT ask" in out2["message"]
    assert no_db["holds"][0]["details"]["Email address"] == "ops@acme.co.uk"
