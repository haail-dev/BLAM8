"""Exact customer promises and signed, race-safe selection of existing offers."""
from tests.booking_fixtures import carried_config
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app import offer_deadlines as deadlines, portal, portal_data, scheduling
from tests.test_scheduling_offers import no_db  # noqa: F401
from tests.test_agent_tools import run_draft, tool_response, text_response

NOW = datetime(2026, 9, 27, 16, 59, tzinfo=scheduling.DIARY_TZ)


def row(day="2026-09-28", window="am", exp=None, id="b1"):
    return {"id": id, "booking_date": day, "window": window, "status": "offered",
            "expires_at": (exp or NOW.replace(hour=20, minute=0)).isoformat(),
            "contact": "c@example.test", "summary": "Visit", "booking_ref": "BK-1"}


@pytest.mark.parametrize("value", [None, ""])
def test_unset_and_blank_last_call_are_off(value):
    cfg = carried_config({"min_notice_hours": 4, "open_time": "08:00", "offer_last_call": value})
    assert scheduling.offer_expiry(cfg, "2026-09-28", "am", NOW) == NOW.replace(
        day=28, hour=4, minute=0)
    assert "offer_last_call" not in scheduling.DEFAULT_CONFIG


def test_rig_mixed_deadlines_and_real_opening():
    cfg = carried_config({"min_notice_hours": 4, "open_time": "08:00", "offer_last_call": "20:00"})
    rows = [row(), row("2026-10-01", "pm", NOW + timedelta(days=1)),
            row("2026-10-02", "am", NOW + timedelta(days=1))]
    for r in rows:
        assert scheduling.offer_expiry(cfg, r["booking_date"], r["window"], NOW) == deadlines.expiry(r)
    line = deadlines.deadline_line(rows, NOW)
    assert line == "Monday's slot is held until 8pm tonight, the other two until tomorrow at 4:59pm."
    assert "8pm tonight" in line and "4:59pm" in line and "5pm" not in line


def test_full_and_partial_sets_only_name_successful_offers(no_db, monkeypatch):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config({
        "min_notice_hours": 4, "open_time": "08:00", "windows": "ampm", "offer_last_call": "20:00"}))
    execute, _ = scheduling.make_executor("t", now=NOW)
    out = execute("offer_slots", {"slots": [
        {"date": "2026-09-28", "window": "am"},
        {"date": "2026-10-01", "window": "pm"},
        {"date": "2026-10-02", "window": "am"}]})
    assert len(out["offered"]) == 3
    assert all(r["expires_at"] for r in out["offered"])
    assert "4:59pm" in out["deadline_line"]
    execute, _ = scheduling.make_executor("t", now=NOW)
    out = execute("offer_slots", {"slots": [
        {"date": "2026-09-28", "window": "am"}, {"date": "bad", "window": "am"}]})
    assert len(out["offered"]) == 1 and out["rejected"]
    assert out["deadline_line"] == "I'm holding this for you until 8pm tonight."


def test_same_deadline_is_one_sentence_and_owner_uses_same_instant():
    rows = [row(), row("2026-09-29")]
    assert deadlines.deadline_line(rows, NOW) == "I'm holding these for you until 8pm tonight."
    assert deadlines.display(rows[0], NOW)["until_iso"] == rows[0]["expires_at"]


def test_rig_shared_deadline_at_1915_and_enforce():
    tomorrow = NOW.replace(day=28, hour=19, minute=15)
    line = deadlines.deadline_line([row(), row("2026-10-01", "pm", tomorrow),
                                    row("2026-10-02", "am", tomorrow)], NOW)
    assert line == "Monday's slot is held until 8pm tonight, the other two until tomorrow at 7:15pm."
    offered = "Available slots: Monday 08:00–12:00, Thursday 12:00–18:00 and Friday 08:00–12:00."
    reply = offered + "\n\nI'm holding these until 5pm tomorrow.\n\nKind regards,\nAlex"
    expected = offered + "\n\n" + line + "\n\nKind regards,\nAlex"
    assert deadlines.enforce(reply, [line]) == expected
    assert deadlines.enforce(expected, [line]) == expected


def test_two_groups_of_two_in_slot_order_not_expiry_order():
    later = NOW.replace(day=28, hour=19, minute=15)
    rows = [row("2026-10-01", exp=later), row("2026-10-02", exp=later),
            row("2026-10-03"), row("2026-10-04")]
    assert deadlines.deadline_line(rows, NOW) == (
        "Thursday's and Friday's slots are held until tomorrow at 7:15pm, "
        "the other two until 8pm tonight.")


@pytest.mark.parametrize("second_day,first_label", [
    ("2026-09-28", "Monday morning's"),
    ("2026-10-05", "Monday 28th September's"),
])
def test_same_weekday_uses_only_needed_detail(second_day, first_label):
    rows = [row(), row(second_day, "pm", NOW + timedelta(days=1))]
    assert deadlines.deadline_line(rows, NOW) == (
        f"{first_label} slot is held until 8pm tonight, the other one until tomorrow at 4:59pm.")


def test_stay_and_appointment_keep_span():
    stay = row(window="day")
    stay.update(start_block=36, length_blocks=96)
    assert deadlines.deadline_line([stay, row(window="13:00", exp=NOW + timedelta(days=1))], NOW) == (
        "Monday 28th September at 09:00 to Tuesday 29th September at 09:00 is held until "
        "8pm tonight, the other one until tomorrow at 4:59pm.")


def test_three_interleaved_groups_name_slots_then_remaining_count():
    later = NOW + timedelta(days=1)
    last = later + timedelta(minutes=1)
    rows = [row(), row("2026-09-29", exp=later), row("2026-09-30", exp=last),
            row("2026-10-01", exp=later), row("2026-10-02", exp=last),
            row("2026-10-03", exp=last)]
    assert deadlines.deadline_line(rows, NOW) == (
        "Monday's slot is held until 8pm tonight, Tuesday's and Thursday's slots are held "
        "until tomorrow at 4:59pm, the other 3 until tomorrow at 5pm.")


def test_notice_uses_per_day_opening_and_service_notice():
    cfg = {"min_notice_hours": 4, "day_hours": {"mon": {"open": "10:00", "close": "18:00"}}}
    exp = scheduling.offer_expiry(cfg, "2026-09-28", "am", NOW)
    assert exp.hour == 6 and exp.day == 28


def test_closed_last_call_creates_no_offer(no_db, monkeypatch):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config({
        "min_notice_hours": 0, "offer_last_call": "16:00"}))
    execute, state = scheduling.make_executor("t", now=NOW)
    out = execute("offer_slots", {"slots": [{"date": "2026-09-28", "window": "day"}]})
    assert out["error"] == "nothing_offered"
    assert out["rejected"][0]["error"] == "offer_closed"
    assert not state["bookings"]


def test_model_is_reasked_once_then_exact_line_inserted(monkeypatch):
    line = deadlines.deadline_line([row()], NOW)
    wrong = {"reply": "I'm holding the slot until 5pm tomorrow. Which suits?", "deferred": False, "missing": []}
    fake, result = run_draft(monkeypatch, [tool_response("offer_slots", {}),
        text_response(wrong), text_response(wrong)],
        lambda *a: {"offered": [{"date": "2026-09-28", "window": "am"}], "deadline_line": line})
    assert len(fake.calls) == 3
    assert line in result.text and "5pm tomorrow" not in result.text


def test_exact_model_sentence_needs_no_retry(monkeypatch):
    line = deadlines.deadline_line([row()], NOW)
    fake, result = run_draft(monkeypatch, [tool_response("offer_slots", {}),
        text_response({"reply": line, "deferred": False, "missing": []})],
        lambda *a: {"offered": [{"date": "2026-09-28", "window": "am"}], "deadline_line": line})
    assert len(fake.calls) == 2 and result.text.count(line) == 1


@pytest.fixture
def choice(monkeypatch):
    offered = row(exp=datetime.now(scheduling.DIARY_TZ) + timedelta(hours=1))
    session = portal_data.PortalSession(tenant_id="t", business_name="Business",
        contact={"email": "c@example.test", "name": "Customer"}, scheduling_on=True)
    monkeypatch.setattr(portal_data.db, "offered_bookings_for_contact", lambda *a: [offered])
    monkeypatch.setattr(portal_data, "_flavour", lambda s: ("windows", [], [], []))
    finalise = AsyncMock(return_value={"booked": True})
    monkeypatch.setattr(portal_data, "_finalise_portal_booking", finalise)
    return session, offered, finalise


def test_choose_uses_executor_and_finalises_once(choice, monkeypatch):
    session, offered, finalise = choice
    def executor(s, **kwargs):
        assert kwargs == {"expected_offer_id": "b1"}
        return lambda name, args: {"held": True}, {"bookings": [offered]}
    monkeypatch.setattr(portal_data, "_diary_executor", executor)
    assert asyncio.run(portal_data.choose_held_time(session, "b1"))["booked"]
    finalise.assert_awaited_once()


@pytest.mark.parametrize("kind", ["expired", "race", "capacity", "foreign", "missing_info", "failed_rule", "full"])
def test_failed_choose_never_finalises_or_rebooks(choice, monkeypatch, kind):
    session, offered, finalise = choice
    if kind == "expired":
        offered["expires_at"] = NOW.replace(year=2000).isoformat()
    def execute(*a):
        if kind == "capacity":
            raise RuntimeError("BLAM8_CAPACITY_CONFLICT")
        return {"error": kind if kind in ("missing_info", "failed_rule", "full") else "offer_gone"}
    monkeypatch.setattr(portal_data, "_diary_executor", lambda *a, **k: (execute, {"bookings": []}))
    out = asyncio.run(portal_data.choose_held_time(session, "other" if kind == "foreign" else "b1"))
    assert out.get("error") and not out.get("booked")
    if kind == "full":
        assert out["error"] == "offer_gone" and "has just gone" in out["message"]
    finalise.assert_not_awaited()


@pytest.mark.parametrize("token", ["CODE", "CODE.wrong", ""])
def test_choose_rejects_unsigned_links_before_customer_lookup(monkeypatch, token):
    monkeypatch.setattr(portal, "_portal_tenant", lambda slug: ({"id": "t"}, {}))
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda *a: pytest.fail("unverified lookup"))
    out = asyncio.run(portal.choose_offer("business", portal.ChooseOfferRequest(token=token, booking_id="b1")))
    assert out["error"] == "not_identified"


def test_portal_real_executor_never_falls_back_after_lost_claim(no_db, monkeypatch):
    offered = row(exp=NOW.replace(year=2099))
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [offered])
    monkeypatch.setattr(scheduling.db, "accept_offered_booking", lambda *a, **k: None)
    execute, state = scheduling.make_executor("t", contact=offered["contact"], now=NOW, expected_offer_id="b1")
    out = execute("hold_booking", {"date": offered["booking_date"], "window": "am", "summary": "Visit"})
    assert out["error"] == "offer_gone"
    assert not state["bookings"] and not no_db["holds"]


def test_offer_footer_uses_the_same_signed_url(monkeypatch):
    from tests.test_portal import _footer_env
    _footer_env(monkeypatch)
    monkeypatch.setattr(portal.db, "offered_bookings_for_contact", lambda *a: [row()])
    footer = portal.email_footer("t1", {}, "c@example.test")
    manage = footer.split("Manage your booking: ")[1].splitlines()[0]
    assert "Or pick online: " + manage in footer
    assert portal.code_from_link("t1", manage.split("?t=")[1]) == "ACME-7K3M"


@pytest.mark.parametrize("signed", [True, False])
def test_only_signed_landing_shows_held_times(monkeypatch, signed):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.test_portal import _page_env
    _page_env(monkeypatch, tenant={"id": "t1", "name": "Business", "portal_slug": "business"}, cfg={})
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda *a: {
        "email": "c@example.test", "name": "Customer", "reference_code": "CODE"})
    monkeypatch.setattr(portal.db, "bookings_for_contact", lambda *a: [])
    monkeypatch.setattr(portal.db, "job_party_memberships", lambda *a: [])
    calls = []
    monkeypatch.setattr(portal.db, "offered_bookings_for_contact", lambda *a: calls.append(a) or [row()])
    token = portal.link_token("t1", "CODE")
    response = TestClient(app).get("/c/business?" + ("t=" + token if signed else "ref=CODE"))
    assert response.status_code == 200
    assert ("Your held times" in response.text) == signed
    assert bool(calls) == signed
    if signed:
        assert 'data-choose-offer="b1"' in response.text
        assert row()["expires_at"] in response.text


def test_signed_choose_binds_tenant_and_contact(monkeypatch):
    monkeypatch.setattr(portal, "_portal_tenant", lambda slug: ({"id": "t", "name": "Business"}, {}))
    monkeypatch.setattr(portal.db, "crm_contact_by_reference", lambda tid, code: {"email": "c@example.test"})
    monkeypatch.setattr(portal.db, "visible_modules", lambda tid: {"scheduling"})
    choose = AsyncMock(return_value={"booked": True})
    monkeypatch.setattr(portal_data, "choose_held_time", choose)
    body = portal.ChooseOfferRequest(token=portal.link_token("t", "CODE"), booking_id="b1")
    assert asyncio.run(portal.choose_offer("business", body))["booked"]
    session, bid = choose.call_args.args
    assert session.tenant_id == "t" and session.contact["email"] == "c@example.test" and bid == "b1"
    body.token = portal.link_token("another-business", "CODE")
    assert asyncio.run(portal.choose_offer("business", body))["error"] == "not_identified"
    assert choose.await_count == 1


def test_choose_confirms_existing_row_releases_sibling_and_notifies_once(no_db, monkeypatch):
    from unittest.mock import Mock
    from app import calendar_sync, crm_intel
    chosen = row(exp=NOW.replace(year=2099))
    sibling = row("2026-10-01", exp=NOW.replace(year=2099), id="b2")
    rows = [chosen, sibling]
    monkeypatch.setattr(portal_data.db, "offered_bookings_for_contact",
        lambda *a, **k: [b for b in rows if b["status"] == "offered"])
    def accept(bid, tid, **kwargs):
        assert kwargs["atomic"] is True and bid == "b1"
        chosen.update(status="confirmed", expires_at=None)
        sibling["status"] = "cancelled"
        return chosen
    monkeypatch.setattr(portal_data.db, "accept_offered_booking", accept)
    monkeypatch.setattr(portal_data, "_flavour", lambda s: ("windows", [], [], []))
    monkeypatch.setattr(portal_data, "_dep_groups", lambda s: [])
    monkeypatch.setattr(portal_data, "_waitlist_on", lambda s: False)
    monkeypatch.setattr(portal_data, "_trace", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.crm, "note_contact_activity", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(calendar_sync, "push_bookings", AsyncMock())
    monkeypatch.setattr(crm_intel, "resolve_from_action", AsyncMock())
    monkeypatch.setattr(portal_data, "_send_booking_confirmation", AsyncMock())
    monkeypatch.setattr(portal_data, "_contact_view", lambda s: {})
    notify = Mock()
    monkeypatch.setattr(portal_data.push, "notify", notify)
    session = portal_data.PortalSession(tenant_id="t", business_name="Business",
        contact={"email": "c@example.test", "name": "Customer"}, scheduling_on=True)
    assert asyncio.run(portal_data.choose_held_time(session, "b1"))["booked"]
    assert chosen["status"] == "confirmed" and sibling["status"] == "cancelled"
    assert not no_db["holds"]
    assert asyncio.run(portal_data.choose_held_time(session, "b1"))["error"] == "offer_gone"
    notify.assert_called_once()


def test_exact_ttl_is_24_elapsed_hours_over_clock_change():
    from datetime import timezone
    now = datetime(2026, 10, 24, 16, 59, tzinfo=scheduling.DIARY_TZ)
    expiry = scheduling.offer_expiry({"min_notice_hours": 0}, "2026-11-02", "am", now)
    assert expiry.astimezone(timezone.utc) - now.astimezone(timezone.utc) == timedelta(hours=24)
    assert expiry.hour == 15 and expiry.minute == 59


def test_portal_label_names_both_ends_of_a_stored_span():
    r = row(window="day")
    r.update(start_block=36, length_blocks=96)
    label = deadlines.slot_label(r)
    assert "Monday" in label and "Tuesday" in label and label.count("09:00") == 2
