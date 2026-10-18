"""S2a: one typed compiler through real setup and authoritative dispatch."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from app import booking_policy as policy, db, scheduling
from tests.test_no_defaults import configured
from tests.test_agnostic_outcomes import ARGS, office


def test_profile_is_immutable_typed_and_preserves_review_metadata():
    cfg = configured(booking_revision=8, booking_setting_review={
        "capacity": {"state": "set", "review_status": "carried_over_review", "source": "saved"}})
    before = deepcopy(cfg)
    profile = policy.compile_profile(cfg, tenant_id="a", mailbox_id="mail-a")
    assert profile.capacity.values == (4,)
    assert isinstance(profile.capacity.values[0], int)
    assert profile.opening_hours.values == ("09:00:00", "17:00:00")
    assert profile.capacity.review_status == "carried_over_review"
    assert profile.identity.booking_revision == 8
    assert profile.identity.compiler == policy.COMPILER_VERSION
    assert profile.as_policy() == policy.compile_policy(cfg)
    assert cfg == before
    with pytest.raises(FrozenInstanceError):
        profile.capacity = None


@pytest.mark.parametrize("kind", ["unset", "off", "invalid", "failed"])
def test_unresolved_and_failed_reads_are_distinct(kind):
    cfg = configured()
    if kind == "unset":
        cfg["capacity_per_window"] = None
    elif kind == "off":
        cfg["booking_setting_review"] = {"capacity": {"state": "off"}}
    elif kind == "invalid":
        cfg["capacity_per_window"] = "bad"
    profile = policy.compile_profile(cfg, read_failed=kind == "failed")
    assert not profile.ready
    refusal = policy.refusal(profile)
    assert refusal["error"] == ("could_not_verify" if kind == "failed" else "unsupported")
    assert not refusal["held"]
    assert profile.prompt_sections == (("booking_readiness", refusal["message"]),)
    if kind == "failed":
        assert profile.read_state == "failed"
        assert profile.identity.booking_revision is None
        assert profile.identity.source_revision is None
        assert profile.capacity.values == ()
    elif kind == "invalid":
        assert profile.capacity.state == "set" and not profile.capacity.valid
    else:
        assert profile.capacity.state == kind


def test_source_revision_is_order_independent_and_tenant_mailbox_bound():
    cfg = configured()
    a = policy.compile_profile(cfg, tenant_id="a", mailbox_id="one")
    assert a == policy.compile_profile(dict(reversed(list(cfg.items()))), tenant_id="a", mailbox_id="one")
    assert a.identity != policy.compile_profile(cfg, tenant_id="b", mailbox_id="one").identity
    assert a.identity != policy.compile_profile(cfg, tenant_id="a", mailbox_id="two").identity
    assert policy.compile_profile({**cfg, "tenant_id": "b"}, tenant_id="a").read_state == "failed"


@pytest.mark.parametrize("mode,has_services,expected", [
    ("appointment", False, (False, False, False)),
    ("appointment", True, (False, True, False)),
    ("stay", False, (True, False, False)),
    ("stay", True, (True, False, True)),
])
def test_booking_shape_uses_existing_interpreters(mode, has_services, expected):
    from app import intake, services
    cfg = configured(booking_mode=mode, service_types=[{"name": "Service A", "duration_minutes": 30}] if has_services else [])
    profile = policy.compile_profile(cfg)
    assert profile.stay == intake.stay_mode(cfg)
    assert (profile.services or profile.appointments) == services.configured(cfg["service_types"])
    assert (profile.stay, profile.services, profile.appointments) == expected


@pytest.mark.parametrize("change", [
    {"booking_revision": 8}, {"capacity_per_window": 5},
    {"booking_mode": "stay"}, {"service_types": []},
    {"courses": {"other_dates": "off"}},
])
def test_second_worker_refuses_changed_ready_policy_without_writes(monkeypatch, change):
    world, tools, execute, state, flags = office(monkeypatch)
    original = deepcopy(world.spec["cfg"])
    current = {**original, **change}
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: deepcopy(current))
    out = execute("hold_booking", deepcopy(ARGS))
    assert out["error"] == "could_not_verify"
    assert "changed" in out["message"]
    assert not state["bookings"] and not world.writes
    assert state["verification_unavailable"] == out["message"]
    # A different business keeps its own unchanged policy and outcome.
    with monkeypatch.context() as other:
        _, _, second, second_state, _ = office(other, tenant="tenant-b")
        assert second("hold_booking", deepcopy(ARGS))["held"]
        assert second_state["bookings"][0]["tenant_id"] == "tenant-b"


@pytest.mark.parametrize("identity_change", [
    {"tenant_id": "foreign"}, {"mailbox_id": "other"}, {"compiler": "retired-compiler"},
])
def test_wrong_identity_and_old_compiler_disable_capability(monkeypatch, identity_change):
    cfg = configured()
    profile = policy.compile_profile(cfg, tenant_id="a", mailbox_id="one")
    stale = replace(profile, identity=replace(profile.identity, **identity_change))
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: cfg)
    execute, state = scheduling.make_executor("a", mailbox_id="one", booking_profile=stale)
    assert execute("hold_booking", {})["error"] == "could_not_verify"
    assert not state["bookings"]


def test_executor_records_failed_read_and_recovers_only_same_revision(monkeypatch):
    cfg = configured()
    profile = policy.compile_profile(cfg, tenant_id="a")
    execute, state = scheduling.make_executor("a", booking_profile=profile)
    def fail(*a, **k):
        raise RuntimeError("offline fixture")
    monkeypatch.setattr(db, "get_scheduling_config", fail)
    assert execute("hold_booking", {})["error"] == "could_not_verify"
    assert state["booking_profile"].read_state == "failed"
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: cfg)
    assert execute("booking_readiness", {})["ready"]
    assert state["booking_profile"] == profile


def test_recompile_after_applied_change_restores_coherence(monkeypatch):
    world, _, old_execute, state, _ = office(monkeypatch)
    cfg = deepcopy(world.spec["cfg"])
    cfg.update(policy.apply_settings(cfg, {"capacity_per_window": "5"}, {}, [], "owner", 7))
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: deepcopy(cfg))
    assert old_execute("hold_booking", ARGS)["error"] == "could_not_verify"
    from app import pipeline
    _, execute, fresh_state, _, flags = pipeline._scheduling_setup("tenant-a", "a@example.test", "thread-a", "mailbox-a")
    assert flags["booking_policy"]["revision"] == 8
    assert execute("hold_booking", deepcopy(ARGS))["held"]
    assert fresh_state["bookings"] and not state["bookings"]
