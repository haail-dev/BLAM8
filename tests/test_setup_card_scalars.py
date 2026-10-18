from copy import deepcopy
import pytest
from app import setup_cards as cards, setup_card_scalars as scalars, booking_policy
from tests.test_setup_cards import append, start, service


def setting(key, value):
    return append("setting", key=key, value=value)


def test_stage_links_resolve_saved_spelling_preserve_rules_and_clear_explicitly():
    cfg = {"_setup_stages": [{"id": "a", "name": "Waiting List"}],
           "courses": {"future": 7, "booked_stage": "Enrolled"}}
    card, patch = apply(setting("waitlist_stage", "waiting list"), cfg=cfg)
    assert patch == {"courses": {"future": 7, "booked_stage": "Enrolled", "waitlist_stage": "Waiting List"}}
    assert card["last_action"]["changes"][0]["fields"]["value"] == "Waiting List"
    card, patch = apply(setting("booked_stage", None), cfg=cfg)
    assert patch["courses"]["booked_stage"] == ""
    assert card["last_action"]["changes"][0]["fields"]["value"] is None


@pytest.mark.parametrize("stages", [None, [], [{"id": "a", "name": "Waiting"}, {"id": "b", "name": "waiting"}]])
def test_stage_links_reject_unverified_missing_or_ambiguous_stages(stages):
    with pytest.raises(cards.Invalid, match="verified|unambiguous"):
        apply(setting("waitlist_stage", "Waiting"), cfg={"_setup_stages": stages})


def apply(*rows, cfg=None, actor="owner"):
    proposal = start(*rows)
    return cards.accept(proposal, 1, "accept", [v["id"] for v in proposal["lines"]], cfg or {}, actor=actor)


def test_explicit_zero_false_and_omission_remain_distinct():
    cfg = {"buffer_minutes": 30, "recurring": {"enabled": True, "max_occurrences": 8, "frequencies": ["weekly"], "future": 7},
           "front_desk": {"enabled": True, "minutes": 15}}
    before = deepcopy(cfg)
    card, patch = apply(setting("buffer_minutes", 0), setting("recurring_enabled", False), cfg=cfg)
    assert patch == {"buffer_minutes": 0, "recurring": cfg["recurring"] | {"enabled": False}}
    assert card["status"] == "applied" and cfg == before


def test_opening_hours_pair_and_owner_confirmation_are_atomic():
    with pytest.raises(cards.Invalid, match="together"):
        apply(setting("open_time", "08:00"))
    card, patch = apply(setting("open_time", "08:00"), setting("close_time", "17:30"))
    assert patch["open_time"] == "08:00:00" and patch["close_time"] == "17:30:00"
    review = patch["booking_setting_review"]["opening_hours"]
    assert review["confirmed_by"] == "owner" and review["source"] == "owner" and review["revision"] == 1
    assert "booking_revision" not in patch
    assert card["last_action"]["changes"][0]["review_after"]["close_time"] == "17:30:00"
    with pytest.raises(cards.Invalid, match="Closing"):
        apply(setting("close_time", "07:00"), cfg={"open_time": "08:00"})


def test_clear_hours_shows_both_cleared_values_and_only_confirms_that_setting():
    cfg = {"open_time": "08:00", "close_time": "17:00", "booking_revision": 4,
           "booking_setting_review": {"capacity": {"state": "set", "review_status": "please_confirm"}}}
    card, patch = apply(setting("open_time", None), cfg=cfg)
    assert patch["open_time"] is None and patch["close_time"] is None
    assert patch["booking_setting_review"]["capacity"] == cfg["booking_setting_review"]["capacity"]
    assert patch["booking_setting_review"]["opening_hours"]["state"] == "unset"
    assert card["last_action"]["changes"][0]["review_after"] == {"open_time": None, "close_time": None, "state": "unset"}


def test_empty_working_days_is_off_not_omitted_or_all_week():
    card, patch = apply(setting("working_days", []), cfg={"working_days": "mon,tue"})
    assert patch["working_days"] is None
    assert patch["booking_setting_review"]["working_days"]["state"] == "off"
    assert card["last_action"]["changes"][0]["fields"]["value"] == []
    assert not booking_policy.compile_profile(patch).working_days.valid


def test_equal_carried_value_is_explicitly_confirmed_without_changing_other_choices():
    cfg = {"capacity_per_window": 4, "booking_revision": 2, "booking_setting_review": {"capacity": {"review_status": "please_confirm", "source": "legacy"}}}
    _, patch = apply(setting("capacity_per_window", 4), cfg=cfg)
    assert patch["booking_setting_review"]["capacity"]["review_status"] == "confirmed"
    assert patch["booking_setting_review"]["capacity"]["carried_over_source"] == "legacy"
    assert patch["booking_setting_review"]["capacity"]["revision"] == 3


def test_capacity_not_applicable_needs_named_pools_and_conflicting_state_is_rejected():
    with pytest.raises(cards.Invalid, match="pools"):
        apply(setting("capacity_state", "not_applicable"))
    with pytest.raises(cards.Invalid, match="conflicts"):
        apply(setting("capacity_state", "off"), setting("capacity_per_window", 4))
    _, patch = apply(append("resource", name="Space", count=4), setting("capacity_state", "not_applicable"))
    assert patch["capacity_per_window"] is None
    assert booking_policy.compile_profile(patch).capacity.valid


def test_weekday_hours_require_both_sides_and_preserve_other_days():
    cfg = {"day_hours": {"mon": {"open": "08:00", "close": "17:00"}, "future": 9}}
    _, patch = apply(setting("dayhours_mon_close", "16:00"), cfg=cfg)
    assert patch["day_hours"] == {"mon": {"open": "08:00", "close": "16:00"}, "future": 9}
    with pytest.raises(cards.Invalid, match="both"):
        apply(setting("dayhours_tue_open", "08:00"), cfg=cfg)
    _, patch = apply(setting("dayhours_mon_open", None), setting("dayhours_mon_close", None), cfg=cfg)
    assert patch["day_hours"] == {"future": 9}


def test_enabled_recurring_requires_explicit_choices_and_carries_unknown_keys():
    with pytest.raises(cards.Invalid, match="missing"):
        apply(setting("recurring_enabled", True))
    with pytest.raises(cards.Invalid, match="frequencies"):
        apply(setting("recurring_enabled", True), cfg={"recurring": {"frequencies": [], "max_occurrences": 8}})
    _, patch = apply(setting("recurring_enabled", True), setting("recurring_max", 8), setting("recurring_freq", ["weekly"]), cfg={"recurring": {"future": 1}})
    assert patch["recurring"] == {"enabled": True, "max_occurrences": 8, "frequencies": ["weekly"], "future": 1}


def test_stay_price_needs_unit_and_preserves_other_pricing_fields():
    with pytest.raises(cards.Invalid, match="unit"):
        apply(setting("stay_rate", 40))
    _, patch = apply(setting("stay_rate", 40), setting("stay_rate_unit", "night"), cfg={"stay_pricing": {"deposit": 10, "min_nights": 2}})
    assert patch["stay_pricing"] == {"rate": 40, "rate_unit": "night", "deposit": 10, "min_nights": 2}


@pytest.mark.parametrize("key,value", [("buffer_minutes", -1), ("min_notice_hours", True), ("slot_minutes", 13),
    ("open_time", "25:00"), ("timezone", "America/New_York"), ("blackout_dates", ["2026-02-30"]),
    ("stay_rate", 0), ("stay_rate", 12.345), ("waitlist_on", "false"), ("horizon_days", None),
    ("unknown_column", 1), ("noun_booking", "x" * 25)])
def test_bad_values_are_visible_and_never_become_a_patch(key, value):
    proposal = start(setting(key, value))
    assert proposal["lines"][0]["problems"]
    with pytest.raises(cards.Invalid):
        cards.accept(proposal, 1, "accept", [proposal["lines"][0]["id"]], {})


def test_correction_preserves_identity_sources_and_other_settings():
    one = start(setting("buffer_minutes", 15), setting("horizon_days", 60))
    line = one["lines"][0]
    two = cards.change("diary_settings", one, 1, "correction", [{"op": "set", "id": line["id"], "fields": {"value": 0},
        "sources": {"value": {"kind": "owner_said", "reference": "No gap"}}}])
    assert two["lines"][0]["fields"] == {"key": "buffer_minutes", "value": 0}
    assert two["lines"][1] == one["lines"][1]
    assert two["lines"][0]["sources"]["key"] == line["sources"]["key"]
    with pytest.raises(cards.Invalid, match="original key"):
        cards.change("diary_settings", two, 2, "bad", [{"op": "set", "id": line["id"], "fields": {"key": "horizon_days"},
            "sources": {"key": {"kind": "owner_said", "reference": "different"}}}])


def test_mixed_service_and_scalar_acceptance_keeps_one_aggregate_patch():
    card, patch = apply(service(), setting("buffer_minutes", 0), setting("horizon_days", 60))
    assert set(patch) == {"service_types", "buffer_minutes", "horizon_days"}
    assert len(card["last_action"]["changes"]) == 3
