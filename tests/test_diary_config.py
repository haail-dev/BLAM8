"""Inbound-business diary depth — the pure config layer (src/app/diary_config.py): cleaners,
the stay quote, and the prompt renderers. Engine/executor wiring is in
tests/test_inbound_depth.py. Every cleaner of an empty value must return the empty shape
(the parity invariant)."""
from app import diary_config as dc


# ── resource types ────────────────────────────────────────────────────────────
def test_clean_resource_types_dedupes_and_clamps():
    out = dc.clean_resource_types([
        {"name": "Double room", "count": 2, "rate": "85", "rate_unit": "night"},
        {"name": " double ROOM ", "count": 5},                 # dup name → dropped
        {"name": "Single", "count": 0, "rate": "0", "skills": "quiet, ground floor"},
        {"name": "", "count": 3},                              # blank → dropped
    ])
    assert [r["name"] for r in out] == ["Double room", "Single"]
    assert out[0] == {"name": "Double room", "count": 2, "rate": 85.0,
                      "rate_unit": "night", "skills": []}
    assert out[1]["count"] == 1 and out[1]["rate"] is None         # count floored to 1, £0 → None
    assert out[1]["skills"] == ["quiet", "ground floor"]
    assert dc.resource_total_count(out) == 3
    assert dc.find_resource(out, "SINGLE")["name"] == "Single"
    assert dc.resources_configured([]) is False


# ── stay pricing + quote ──────────────────────────────────────────────────────
def test_clean_stay_pricing_and_quote_per_unit():
    p = dc.clean_stay_pricing({"rate": "30", "rate_unit": "night", "deposit": "50", "min_nights": "2"})
    assert p == {"rate": 30.0, "rate_unit": "night", "deposit": 50.0, "min_nights": 2}
    # 3 nights at £30 = £90; deposit rides along.
    q = dc.quote_stay(p, nights=3, days=4)
    assert q["amount"] == 90.0 and q["deposit"] == 50.0 and q["units"] == 3
    # day-rate uses days; weekly rounds up; quantity multiplies.
    assert dc.quote_stay({"rate": 20, "rate_unit": "day"}, 3, days=4)["amount"] == 80.0
    assert dc.quote_stay({"rate": 100, "rate_unit": "week"}, 9)["units"] == 2     # 9 nights → 2 weeks
    assert dc.quote_stay({"rate": 30, "rate_unit": "night"}, 2, qty=2)["amount"] == 120.0
    assert dc.quote_stay({}, 3) is None                                          # no rate → no quote


def test_stay_pricing_configured():
    assert dc.stay_pricing_configured({"rate": 30}) is True
    assert dc.stay_pricing_configured({"min_nights": 2}) is True
    assert dc.stay_pricing_configured({}) is False


# ── per-day hours + check-in/out ──────────────────────────────────────────────
def test_clean_day_hours_overrides_and_rules():
    c = dc.clean_day_hours({"sat": {"open": "09:00", "close": "13:00"},
                            "sun": {"open": "10:00", "close": "09:00"},   # invalid (close<open) → dropped
                            "checkin_after": "14:00", "checkout_before": "11:00"})
    assert c["sat"] == {"open": "09:00", "close": "13:00"}
    assert "sun" not in c
    assert c["checkin_after"] == "14:00" and c["checkout_before"] == "11:00"
    assert dc.day_hours_configured({}) is False


# ── party size / recurring / delivery ─────────────────────────────────────────
def test_party_recurring_delivery():
    assert dc.clean_party_size({"enabled": True, "max_people": "40"}) == {"enabled": True, "max_people": 40}
    assert dc.party_size_enabled({}) is False
    r = dc.clean_recurring({"enabled": True, "max_occurrences": "8", "frequencies": ["weekly", "bogus"]})
    assert r["enabled"] and r["max_occurrences"] == 8 and r["frequencies"] == ["weekly"]
    assert dc.clean_recurring({})["frequencies"]            # defaults to all freqs when none given
    assert dc.clean_delivery({"offered": True, "fee": "15", "radius_miles": "20"}) == {
        "offered": True, "fee": 15.0, "radius_miles": 20}
    assert dc.delivery_offered({}) is False


# ── prompt rendering ──────────────────────────────────────────────────────────
def test_render_all_empty_is_blank():
    assert dc.render_all_for_prompt({}) == ""
    assert dc.render_all_for_prompt(None) == ""


def test_render_all_lists_configured_blocks():
    out = dc.render_all_for_prompt({
        "resource_types": [{"name": "Double", "count": 2, "rate": 85, "rate_unit": "night"}],
        "stay_pricing": {"rate": 30, "rate_unit": "night", "deposit": 50},
        "party_size": {"enabled": True, "max_people": 40},
        "delivery": {"offered": True, "fee": 15},
    })
    assert "Resource types" in out and "Double (2 available; £85/night)" in out
    assert "Stay pricing" in out and "£30/night" in out
    assert "Party size" in out and "DELIVER" in out


def test_six_weekly_and_quarterly_are_frequencies_with_their_own_words():
    # Kris 2026-09-04: a farrier's cycle is every six to eight weeks; neither
    # weekly/fortnightly/monthly said it.
    r = dc.clean_recurring({"enabled": True, "frequencies": ["six_weekly", "quarterly", "Every 6 weeks"]})
    assert r["frequencies"] == ["six_weekly", "quarterly"]
    assert dc.clean_recurring({})["frequencies"] == ["weekly", "fortnightly", "monthly", "six_weekly", "quarterly"]
    assert dc.freq_label("six_weekly") == "Every 6 weeks" and dc.freq_label("quarterly") == "Quarterly"
    p = dc.render_recurring_for_prompt({"enabled": True, "frequencies": ["six_weekly"]})
    assert "six_weekly = every 6 weeks" in p and "every six weeks" in p
    assert dc.FREQ_DAYS["six_weekly"] == 42 and dc.FREQ_MONTHS["quarterly"] == 3
