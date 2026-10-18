from copy import deepcopy
import pytest
from app import setup_cards as cards, setup_rows, intake, scheduling, services
from tests.test_setup_cards import start, append, service


def plan(card, cfg, indices=None):
    ids = [v["id"] for i, v in enumerate(card["lines"]) if indices is None or i in indices]
    return cards.accept(card, card["revision"], "accept", ids, cfg)


def configured():
    return {"booking_mode": "stay", "capacity_per_window": 20,
            "service_types": [{"name": "Review", "minutes": 45, "resource": "Review", "price": 40,
                               "days": [5], "buffer": 15, "future": {"enabled": True}}],
            "resource_types": [{"name": "Space", "count": 8, "rate": 15, "rate_unit": "night", "future": 7},
                               {"name": "Review", "count": 2, "skills": ["Verification"], "future": 3}],
            "stay_pricing": {"rate": 50, "rate_unit": "night"}, "front_desk": {"enabled": True, "minutes": 15}}


@pytest.mark.parametrize("cfg", [{"booking_mode": "stay"}, {"intake_fields": [intake.STAY_FIELD]}])
def test_new_appointment_creates_explicit_pool_and_readable_atomic_receipt(cfg):
    card, patch = plan(start(service(at_once=3)), cfg)
    assert patch["service_types"][0]["resource"] == "Review"
    assert "at_once" not in patch["service_types"][0]
    assert patch["resource_types"] == [{"name": "Review", "count": 3}]
    assert "3 at once" in card["lines"][0]["display"]
    receipt = card["last_action"]["changes"][0]
    assert receipt["resource_before"] is None and receipt["resource_after"]["count"] == 3
    assert receipt["review_after"]["at_once"] == 3
    assert scheduling.stay_resource_types(cfg | patch) == []
    assert "capacity_per_window" not in patch


def test_new_appointment_never_assumes_one():
    card = start(service())
    with pytest.raises(cards.Invalid, match="at once"):
        plan(card, {"booking_mode": "stay"})
    assert card["status"] == "proposed"


@pytest.mark.parametrize("value", [None, 0, -1, 10000, 1.5, True, "unknown"])
def test_invalid_concurrency_never_produces_a_plan(value):
    card = start(service(at_once=value))
    assert card["lines"][0]["problems"]
    with pytest.raises(cards.Invalid):
        plan(card, {"booking_mode": "stay"})


def test_corrections_preserve_stay_spaces_service_rules_and_resource_attributes():
    cfg = configured()
    before = deepcopy(cfg)
    card, patch = plan(start(append("service", name="review", price=60, at_once=4)), cfg)
    assert patch["service_types"] == [cfg["service_types"][0] | {"price": 60}]
    assert patch["resource_types"] == [cfg["resource_types"][0], cfg["resource_types"][1] | {"count": 4}]
    assert cfg == before
    assert scheduling.stay_resource_types(cfg | patch) == scheduling.stay_resource_types(cfg)
    assert card["last_action"]["changes"][0]["review_before"]["at_once"] == 2
    assert card["last_action"]["changes"][0]["review_after"]["at_once"] == 4
    assert set(patch) == {"service_types", "resource_types"}


def test_duration_only_carries_saved_capacity_without_rewriting_resource_catalogue():
    cfg = configured()
    card, patch = plan(start(append("service", name="Review", minutes=60)), cfg)
    assert set(patch) == {"service_types"}
    assert card["last_action"]["changes"][0]["fields"]["at_once"] == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_separate_service_and_resource_proposals_require_joint_acceptance(reverse):
    changes = [service(), append("resource", name="Review", count=2)]
    if reverse:
        changes.reverse()
    card = start(*changes)
    for i in range(2):
        with pytest.raises(cards.Invalid, match="together|at once"):
            plan(card, {"booking_mode": "stay"}, [i])
    _, patch = plan(card, {"booking_mode": "stay"})
    assert patch["service_types"][0]["resource"] == "Review"
    assert patch["resource_types"][0]["count"] == 2


def test_conflicting_counts_and_stay_pool_reuse_are_rejected():
    with pytest.raises(cards.Invalid, match="different capacities"):
        plan(start(service(at_once=3), append("resource", name="Review", count=2)), {"booking_mode": "stay"})
    with pytest.raises(cards.Invalid, match="stay space"):
        plan(start(service(name="Space", at_once=2)), configured())


@pytest.mark.parametrize("fields", [{"timing": "half"}, {"timing": "day"}, {"resource": "Space"}])
def test_stay_appointment_cannot_silently_retime_or_share_stay_capacity(fields):
    with pytest.raises(cards.Invalid, match="exact-time|own same-name"):
        plan(start(service(**fields)), configured())


def test_capacity_correction_retains_exact_saved_resource_name_for_existing_bookings():
    cfg = configured()
    card, patch = plan(start(append("resource", name="review", count=4)), cfg)
    assert patch["resource_types"][1]["name"] == "Review"
    assert set(patch) == {"resource_types"}
    assert "appointments" in card["last_action"]["changes"][0]["notes"][0]
    card, _ = plan(start(append("resource", name="Space", count=10)), cfg)
    assert card["last_action"]["changes"][0]["notes"] == ["Capacity for stay spaces."]


def test_nonstay_diaries_reject_appointment_specific_concurrency():
    with pytest.raises(cards.Invalid, match="stay diary"):
        plan(start(service(at_once=2)), {})


def test_saved_shared_or_missing_pool_needs_review_not_hidden_migration():
    cfg = configured()
    cfg["service_types"][0]["resource"] = "Space"
    with pytest.raises(cards.Invalid, match="cannot move"):
        plan(start(service(resource="Review", at_once=3)), cfg)
    cfg = configured()
    cfg["resource_types"] = cfg["resource_types"][:1]
    with pytest.raises(cards.Invalid, match="at once"):
        plan(start(append("service", name="Review", price=60)), cfg)


def test_stay_detail_scope_is_valid_only_for_stay_diaries():
    card = start(append("field", label="Evidence", required=False, kind="document", services=[intake.STAYS_SCOPE]), page="booking_details")
    _, patch = plan(card, {"config": configured(), "definitions": []})
    assert patch["intake_fields"][0]["services"] == [intake.STAYS_SCOPE]
    with pytest.raises(cards.Invalid, match="services"):
        plan(card, {"config": {}, "definitions": []})


def test_saved_bookings_keep_their_separate_capacity_pools_after_correction():
    cfg = configured() | {"slot_minutes": 60}
    _, patch = plan(start(append("service", name="review", at_once=4)), cfg)
    updated = cfg | patch
    bookings = [{"booking_date": "2026-10-05", "start_block": 36, "length_blocks": 96,
                 "quantity": 1, "status": "confirmed", "resource_type": "Space"},
                {"booking_date": "2026-10-05", "start_block": 36, "length_blocks": 3,
                 "quantity": 1, "status": "confirmed", "resource_type": "Review"}]
    for pool in ("Space", "Review"):
        old_spans = scheduling._pool_spans(bookings, cfg, pool)
        assert len(old_spans) == 1
        assert scheduling._pool_spans(bookings, updated, pool) == old_spans
    assert services.appointment_at_once(updated["service_types"], updated["resource_types"]) == {"Review": 4}
    assert scheduling.stay_resource_types(updated)[0]["count"] == 8


def test_first_named_stay_pool_cannot_repartition_existing_shared_capacity():
    cfg = {"booking_mode": "stay", "capacity_per_window": 20}
    with pytest.raises(cards.Invalid, match="existing bookings"):
        plan(start(append("resource", name="Space", count=8)), cfg)
    # A timed appointment has its own capacity and does not repartition stays.
    _, patch = plan(start(service(at_once=2)), cfg)
    assert scheduling.stay_resource_types(cfg | patch) == []
    # Named spaces on a diary with capacity still unset are an explicit choice.
    _, patch = plan(start(append("resource", name="Space", count=8)), {"booking_mode": "stay"})
    assert patch["resource_types"][0]["count"] == 8
