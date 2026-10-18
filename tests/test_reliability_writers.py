"""Each owner/customer writer loses an atomic reservation cleanly, before effects."""
from tests.booking_fixtures import carried_config
from types import SimpleNamespace

import pytest

from app import db, dashboard, record_edits, concierge_actions as ca, scheduling, portal_data
from app.booking_errors import CAPACITY_MESSAGE, CapacityConflict
from tests.test_diary_settings_save import make_saved, saved  # noqa: F401


def conflict(*args, **kwargs):
    raise RuntimeError("BLAM8_CAPACITY_CONFLICT: private SQL diagnostics")


@pytest.mark.parametrize("return_to,expected", [
    ("/app/diary", "/app/diary?msg=full"),
    ("/app/diary?date=2026-10-01", "/app/diary?date=2026-10-01&msg=full"),
    ("/app/diary?date=2026-10-01&msg=old#day", "/app/diary?date=2026-10-01&msg=full#day"),
    ("https://example.invalid/", "/app/conversations?msg=full"),
])
def test_confirm_button_returns_plain_conflict(saved, monkeypatch, return_to, expected):
    client, _ = saved
    monkeypatch.setattr(db, "get_booking", lambda *a, **k: {"id": "b1", "status": "offered"})
    monkeypatch.setattr(db, "set_booking_status", conflict)
    from app import job_sheets, calendar_sync
    def unexpected(*a, **k):
        pytest.fail("A refused confirmation must have no follow-on effects")
    async def unexpected_async(*a, **k):
        unexpected()
    monkeypatch.setattr(job_sheets, "soon", unexpected)
    monkeypatch.setattr(db, "log_agent_event", unexpected)
    monkeypatch.setattr(dashboard.events, "publish", unexpected)
    monkeypatch.setattr(calendar_sync, "push_bookings", unexpected_async)
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", unexpected_async)
    out = client.post("/app/diary/b1/status", data={"status": "confirmed", "return_to": return_to},
                      follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == expected


@pytest.mark.asyncio
async def test_diary_move_conflict_stops_before_calendar_or_log(monkeypatch):
    monkeypatch.setattr(db, "move_booking_in_place", conflict)
    with pytest.raises(record_edits.EditError, match="Nothing changed"):
        await record_edits.apply_booking_move("t1", {"id": "b1"}, {
            "timed": True, "new_date": "2026-10-01", "new_start_block": 40,
            "length_blocks": 4, "new_window": "am"})


@pytest.mark.asyncio
async def test_stay_date_conflict_stops_before_prices_invoices_or_calendar(monkeypatch):
    monkeypatch.setattr(db, "move_booking_in_place", conflict)
    with pytest.raises(record_edits.EditError, match="Nothing changed"):
        await record_edits.apply_stay_change("t1", {"id": "b1"}, {
            "new_date": "2026-10-01", "new_start_block": 40, "length_blocks": 100})


@pytest.mark.asyncio
async def test_concierge_change_stay_translates_conflict(monkeypatch):
    monkeypatch.setattr(db, "get_booking", lambda *a, **k: {"id": "b1"})
    monkeypatch.setattr(record_edits, "plan_stay_change", lambda *a, **k: {
        "new_date": "2026-10-01", "new_start_block": 40, "length_blocks": 100})
    monkeypatch.setattr(db, "move_booking_in_place", conflict)
    with pytest.raises(ca.ActionError, match="Nothing changed"):
        await ca.execute("t1", None, "change_stay", {"booking_id": "b1", "arrival": "", "departure": ""})


@pytest.mark.asyncio
async def test_concierge_dependant_action_translates_conflict(monkeypatch):
    monkeypatch.setattr(db, "get_requirement_instance", lambda *a, **k: {"id": "i1", "name": "Alex"})
    monkeypatch.setattr(db, "crm_get_contact", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(db, "list_requirement_groups", lambda *a, **k: [{"id": "g1", "label": "Participant", "fields": [{"key": "level", "label": "Level"}]}])
    async def failing_save(*a, **k):
        conflict()
    monkeypatch.setattr(record_edits, "save_dependant", failing_save)
    with pytest.raises(ca.ActionError, match="Nothing changed"):
        await ca.execute("t1", None, "update_dependant", {
            "instance_id": "i1", "contact_id": "c1", "group_id": "g1", "key": "level"})


@pytest.mark.asyncio
async def test_portal_reschedule_translates_database_conflict(monkeypatch):
    from tests.test_portal import _session, _B_ID
    session = _session(contact={"id": "c1", "email": "owner@example.invalid"}, diary=("windows", [], [], []))
    monkeypatch.setattr(db, "get_booking", lambda *a, **k: {
        "id": _B_ID, "contact": "owner@example.invalid", "status": "confirmed"})
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config({}))
    monkeypatch.setattr(scheduling, "hold_error", lambda *a, **k: None)
    monkeypatch.setattr(db, "move_booking_if_capacity", conflict)
    out = await portal_data.execute(session, "move_booking", {
        "booking_id": _B_ID, "new_date": "2026-10-01", "new_window": "day"})
    assert out["error"] == "slot_full"
    assert out["message"] == "That slot was just taken — offer the other available times."
    assert not session.moves_made


def test_accept_offered_booking_returns_a_plain_typed_conflict():
    class FailingClient:
        def __getattr__(self, name):
            return conflict if name == "execute" else lambda *a, **k: self
    with pytest.raises(CapacityConflict) as error:
        db.accept_offered_booking("b1", "t1", client=FailingClient())
    assert str(error.value) == CAPACITY_MESSAGE


def test_unrelated_failure_is_not_a_capacity_refusal(monkeypatch):
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config({}))
    monkeypatch.setattr(scheduling, "hold_error", lambda *a, **k: None)
    def unavailable(*a, **k):
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(db, "move_booking_if_capacity", unavailable)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        scheduling.move_booking("t1", "b1", "2026-10-01", "day")


def test_saving_stay_toggle_persists_explicit_mode():
    from app import intake
    class Client:
        def table(self, *a): return self
        def upsert(self, row): self.row = row; return self
        def execute(self): return SimpleNamespace(data=[])
    client = Client()
    for enabled in (True, False):
        db.upsert_scheduling_config("t1", {"intake_fields": intake.apply_stay([], enabled)}, client=client)
        assert client.row["booking_mode"] == ("stay" if enabled else "appointment")
    assert intake.stay_mode({"booking_mode": "stay", "intake_fields": [{"label": "End"}]})


def test_carried_over_terms_wait_for_explicit_review(saved, monkeypatch):
    client, calls = saved
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config({"service_types": [{
        "name": "Visit", "minutes": 60, "out_of_hours": {"until": "22:00", "price": 90,
        "price_basis": "first_hour", "terms": "Existing wording.",
        "carried_over_review": ["price_basis", "terms"]}}]}))
    form = {"service_name": ["Visit"], "service_minutes": ["60"],
            "service_ooh_until": ["22:00"], "service_ooh_price": ["90"],
            "service_ooh_basis": ["first_hour"], "service_ooh_terms": ["Existing wording."]}
    for reviewed in (False, True):
        if reviewed: form["service_ooh_reviewed"] = ["Visit"]
        result = client.post("/app/settings/scheduling", data=form, follow_redirects=False)
        assert result.status_code == 303
        policy = calls[-1]["service_types"][0]["out_of_hours"]
        assert bool(policy.get("carried_over_review")) is not reviewed
        assert policy["terms"] == "Existing wording."
    assert len(calls) == 2  # one write per save



def test_renamed_pool_does_not_block_the_owner_stay_plan(monkeypatch):
    from tests.test_stay_date_changes import _stub, BELLA, CFG
    _stub(monkeypatch)
    monkeypatch.setattr(db,"get_scheduling_config",lambda *a,**k: carried_config({
        **CFG,"resource_types":[{"name":"Renamed","count":2}]}))
    plan = record_edits.plan_stay_change("t1",{**BELLA,"resource_type":"Old"},new_departure="2026-10-28")
    assert plan["resource"] == "Old" and plan["nights"] == 16



def _reject_acceptance(monkeypatch, offered):
    """Exercise the real DB error translation, then the email executor boundary."""
    original = db.accept_offered_booking
    calls = []
    class FailingClient:
        def __getattr__(self, name):
            return conflict if name == "execute" else lambda *a, **k: self
    def accept(booking_id, tenant_id, details=None):
        calls.append(booking_id)
        return original(booking_id, tenant_id, details=details, client=FailingClient())
    def unexpected(*a, **k):
        pytest.fail("Acceptance conflict must not insert, release or reprice bookings")
    monkeypatch.setattr(db, "offered_bookings_for_contact", lambda *a, **k: [offered])
    monkeypatch.setattr(db, "accept_offered_booking", accept)
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **k: [])
    for name in ("insert_stay_booking", "release_offered_for_contact", "set_booking_quote"):
        monkeypatch.setattr(db, name, unexpected)
    return calls


def test_appointment_offer_acceptance_conflict_reaches_email_executor(monkeypatch):
    from tests.test_service_executor import _exec
    execute, state = _exec(monkeypatch, [], [])
    calls = _reject_acceptance(monkeypatch, {
        "id": "offer-1", "status": "offered", "booking_date": "2026-06-17",
        "window": "10:00", "details": {"Service": "Cut"}})
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00", "service": "Cut"})
    assert calls == ["offer-1"] and state["bookings"] == []
    assert out["error"] == "full" and out["held"] is False
    assert out["message"].startswith(CAPACITY_MESSAGE)
    assert "BLAM8_CAPACITY_CONFLICT" not in str(out) and "private SQL" not in str(out)


def test_stay_offer_acceptance_conflict_reaches_email_executor(monkeypatch):
    from tests.test_stay_executor import _exec
    execute, state = _exec(monkeypatch, [], [])
    calls = _reject_acceptance(monkeypatch, {
        "id": "offer-1", "status": "offered", "booking_date": "2026-06-30",
        "start_block": 48, "length_blocks": 208, "details": {}})
    out = execute("hold_booking", {"arrival": "2026-06-30T12:00", "departure": "2026-07-02T16:00"})
    assert calls == ["offer-1"] and state["bookings"] == []
    assert out["error"] == "full" and out["held"] is False
    assert out["message"].startswith(CAPACITY_MESSAGE)
    assert "BLAM8_CAPACITY_CONFLICT" not in str(out) and "private SQL" not in str(out)


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
