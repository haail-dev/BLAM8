"""No implicit booking permission; explicit values and provenance are separate."""
import pytest
from app import booking_policy as policy, scheduling


def configured(**kw):
    return {"capacity_per_window": 4, "working_days": "mon,tue,wed,thu,fri",
            "open_time": "09:00", "close_time": "17:00", "timezone": "Europe/London", **kw}


@pytest.mark.parametrize("missing", list(policy.FIELDS))
def test_unset_setting_hands_off_before_any_read_or_reservation(monkeypatch, missing):
    cfg = configured(**{k: None for k in policy.FIELDS[missing]})
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: cfg)
    execute, state = scheduling.make_executor("business")
    for tool in ("check_availability", "hold_booking", "offer_slots", "hold_appointment", "move_booking"):
        result = execute(tool, {})
        assert result["error"] == "unsupported"
        assert result["held"] is False
        assert result["unresolved_settings"] == [missing]
    assert state["bookings"] == []


def test_failure_to_read_settings_is_could_not_verify(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", broken)
    execute, state = scheduling.make_executor("business")
    assert execute("hold_booking", {})["error"] == "could_not_verify"
    assert state["bookings"] == []


def test_clearing_settings_invalidates_the_same_executors_cached_read(monkeypatch):
    cfg = configured()
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: cfg)
    execute, _ = scheduling.make_executor("business")
    assert execute("booking_readiness", {})["ready"]
    cfg["capacity_per_window"] = None
    result = execute("check_availability", {})
    assert result["error"] == "unsupported"
    assert result["unresolved_settings"] == ["capacity"]


def test_omitted_cleared_and_zero_are_distinct():
    cfg = configured()
    assert policy.apply_settings(cfg, {}, {}, [], "owner", 0) == {}
    cleared = policy.apply_settings(cfg, {"capacity_per_window": ""}, {}, [], "owner", 0)
    assert cleared["capacity_per_window"] is None
    assert cleared["booking_setting_review"]["capacity"]["state"] == "unset"
    zero = policy.apply_settings(cfg, {"capacity_per_window": "0"}, {}, [], "owner", 0)
    assert zero["capacity_per_window"] == 0
    assert zero["booking_setting_review"]["capacity"]["review_status"] == "confirmed"
    assert policy.refusal(policy.compile_policy({**cfg, **zero}))["held"] is False


def test_posting_an_unchanged_unset_form_confirms_nothing():
    changes = policy.apply_settings({}, {"capacity_per_window": "", "working_days": "",
                                          "open_time": "", "close_time": "", "timezone": ""},
                                    {k: "unset" for k in policy.FIELDS}, [], "owner", 0)
    assert changes == {}


def test_confirmation_changes_only_provenance():
    cfg = configured(booking_revision=1, booking_setting_review={
        "capacity": {"state": "set", "review_status": "carried_over_review", "source": "runtime_fallback"}})
    assert policy.apply_settings(cfg, {"capacity_per_window": "4"}, {}, [], "owner", 1) == {}
    changed = policy.apply_settings(cfg, {}, {}, ["capacity"], "owner", 1)
    assert changed["capacity_per_window"] == cfg["capacity_per_window"]
    assert changed["booking_revision"] == 2
    assert changed["booking_setting_review"]["capacity"]["confirmed_by"] == "owner"
    assert changed["booking_setting_review"]["capacity"]["confirmed_at"]


@pytest.mark.parametrize("setting,values", [
    ("opening_hours", ("17:00", "09:00")), ("opening_hours", ("nonsense", "17:00")),
    ("timezone", ("America/New_York",)), ("capacity", ("-1",)), ("working_days", ("whatever",)),
])
def test_invalid_values_are_refused(setting, values):
    with pytest.raises(ValueError):
        policy.validate(setting, values)


def test_not_applicable_needs_a_real_resource_pool():
    cfg = configured(capacity_per_window=None, booking_setting_review={"capacity": {"state": "not_applicable"}})
    assert not policy.compile_policy(cfg)["ready"]
    cfg["resource_types"] = [{"name": "Room", "count": 2}]
    assert policy.compile_policy(cfg)["ready"]


def test_turning_bookings_off_preserves_the_business_timezone():
    cfg = configured(booking_revision=2)
    changed = policy.apply_settings(
        cfg, {"timezone": "Europe/London"}, {"timezone": "off"}, [], "owner", 2)

    assert changed["timezone"] == "Europe/London"
    assert changed["booking_setting_review"]["timezone"]["state"] == "off"
    assert policy.compile_policy({**cfg, **changed})["ready"] is False


def test_off_timezone_without_a_new_value_keeps_the_existing_timezone():
    cfg = configured(booking_revision=2)
    changed = policy.apply_settings(
        cfg, {"timezone": ""}, {"timezone": "off"}, [], "owner", 2)

    assert changed["timezone"] == "Europe/London"
    assert policy.compile_policy({**cfg, **changed})["ready"] is False


def test_stale_confirmation_is_refused():
    with pytest.raises(ValueError, match="Reload"):
        policy.apply_settings(configured(booking_revision=2), {}, {}, ["capacity"], "owner", 1)


def test_no_substitution_in_pure_readers():
    from app import slotgrid
    assert scheduling._merged(None)["capacity_per_window"] is None
    assert scheduling._merged(None)["working_days"] is None
    assert slotgrid.hours_for_day({}) == (None, None)
    assert slotgrid.open_close_blocks({}) == (0, 0)


def test_skipped_tools_cannot_promise_a_booking(monkeypatch):
    import asyncio
    from app import agent
    from tests.test_agent_tools import FakeClient, text_response
    response = text_response({"reply": "Your booking is confirmed.", "thread_state": "booked",
                              "scheduling_requested": True, "action_summary": "Booked the visit"})
    fake = FakeClient([response] * 20)
    monkeypatch.setattr(agent, "client", lambda: fake)
    result = asyncio.run(agent.draft_reply({}, {"body": "Please book a visit"},
                                         booking_policy=policy.compile_policy({})))
    assert result.text == ""
    assert result.blocked
    assert result.thread_state == "needs_attention"


def test_settings_apply_reload_and_confirmation(monkeypatch):
    import copy
    from fastapi.testclient import TestClient
    from app import dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.security import require_csrf
    from app.main import app
    stored, writes = {}, []
    def apply(tid, patch, revision, **kw):
        assert revision == stored.get("booking_revision", 0)
        stored.update(copy.deepcopy(patch))
        writes.append(copy.deepcopy(patch))
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: copy.deepcopy(stored))
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "apply_booking_settings", apply)
    monkeypatch.setattr(dashboard.db, "upsert_scheduling_config", lambda tid, patch, **kw: stored.update(patch))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="business", user_id="owner", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        client = TestClient(app)
        data = {**configured(), "booking_revision": 0}
        assert client.post("/app/settings/scheduling", data=data, follow_redirects=False).status_code == 303
        assert policy.compile_policy(dashboard.db.get_scheduling_config("business"))["ready"]
        before = copy.deepcopy(stored)
        assert client.post("/app/settings/scheduling", data={"booking_revision": 1, "confirm_booking_setting": ["capacity"]}, follow_redirects=False).status_code == 303
        assert {k: v for k, v in stored.items() if k not in ("booking_setting_review", "booking_revision")} == {
            k: v for k, v in before.items() if k not in ("booking_setting_review", "booking_revision")}
        assert stored["booking_setting_review"]["capacity"]["confirmed_by"] == "owner"
        assert client.post("/app/settings/scheduling", data={"booking_revision": 2, "open_time": "17:00", "close_time": "09:00"}, follow_redirects=False).status_code == 422
        assert stored["open_time"] == "09:00:00"
        assert len(writes) == 2
        assert client.post("/app/settings/scheduling", data={"booking_revision": 2, "capacity_per_window": "", "capacity_state": "set"}, follow_redirects=False).status_code == 303
        assert stored["capacity_per_window"] is None
        assert stored["booking_setting_review"]["capacity"]["state"] == "unset"
        assert client.post("/app/settings/scheduling", data={"booking_revision": 3, "capacity_per_window": "0"}, follow_redirects=False).status_code == 303
        assert stored["capacity_per_window"] == 0
        assert client.post("/app/settings/scheduling", data={
            "booking_revision": 4, "timezone": "Europe/London", "timezone_state": "off",
        }, follow_redirects=False).status_code == 303
        assert stored["timezone"] == "Europe/London"
        assert stored["booking_setting_review"]["timezone"]["state"] == "off"
        assert not policy.compile_policy(stored)["ready"]
    finally:
        app.dependency_overrides.clear()


def test_new_settings_page_has_no_capacity_or_hours_fallback(monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from app import dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from tests.test_settings_fold import _env
    _env(monkeypatch, modules=("scheduling",))
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="business", client=None)
    try:
        html = TestClient(app).get("/app/diary/settings").text
        assert "Not set yet: booking unavailable" in html
        for name in ("capacity_per_window", "open_time", "close_time"):
            control = re.search(r'<input[^>]+name="' + name + r'"[^>]*>', html).group()
            assert 'value=""' in control
        assert "In use: carried over, please confirm" not in html
    finally:
        app.dependency_overrides.clear()


def test_compiled_unready_policy_exposes_only_handoff(monkeypatch):
    from app import pipeline
    cfg = {"intake_configured_at": "2026-09-27"}
    monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: cfg)
    monkeypatch.setattr(pipeline.db, "list_requirement_groups", lambda *a, **k: [])
    tools, execute, state, diary, flags = pipeline._scheduling_setup("business", None, None, None)
    assert [t["name"] for t in tools] == ["handoff_booking_to_owner"]
    assert flags["scheduling"] is False
    assert diary == flags["booking_policy"]["message"]
    result = execute("handoff_booking_to_owner", {})
    assert result["error"] == "unsupported" and result["held"] is False
    assert state["bookings"] == []


def test_ready_business_keeps_reply_after_reserve_nudges_run_out(monkeypatch):
    from tests.test_agent_tools import run_draft, text_response
    reply = "Your booking is confirmed."
    fake, result = run_draft(monkeypatch, [text_response({"reply": reply, "thread_state": "booked"})],
                             executor=lambda *a: {}, scheduling=True,
                             booking_policy=policy.compile_policy(configured()))
    assert len(fake.calls) > 1
    assert result.text == reply
    assert not result.blocked


@pytest.mark.parametrize("tool", ["hold_appointment", "move_booking"])
def test_successful_appointment_or_move_counts_as_reserved(monkeypatch, tool):
    from tests.test_agent_tools import run_draft, text_response, tool_response
    reply = "Your booking is confirmed."
    executed = []
    def execute(name, args):
        executed.append(name)
        return {"id": "booking", "held": True}
    fake, result = run_draft(monkeypatch, [tool_response(tool, {}), text_response({"reply": reply, "thread_state": "booked"})],
                             executor=execute, scheduling=True,
                             booking_policy=policy.compile_policy(configured()))
    assert executed == [tool]
    assert len(fake.calls) == 2
    assert result.text == reply
    assert not result.blocked
