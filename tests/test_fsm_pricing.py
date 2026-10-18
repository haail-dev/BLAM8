"""Field Service pricing — the pure quote math (docs/modules/module-field-service.md D3).

The trades sibling of the TMS pricing tests: rate card + a job in, a line-item quote
out, no DB. Pins call-out + labour, fixed-price jobs, the min-charge floor and VAT.

Run: python -m pytest tests/test_fsm_pricing.py -q
"""
from app import fsm_pricing

RATE = {"callout_charge": 60.0, "hourly_rate": 55.0, "min_charge": 75.0, "vat_pct": 20.0}


def test_callout_plus_labour_one_hour():
    q = fsm_pricing.quote(RATE, 60)
    assert q["callout"] == 60.0 and q["hours"] == 1 and q["labour"] == 55.0
    assert q["parts"] == 0.0
    assert q["net"] == 115.0            # 60 + 55
    assert q["vat"] == 23.0            # 20% of 115
    assert q["total"] == 138.0
    assert q["min_applied"] is False


def test_labour_rounds_part_hours_up():
    q = fsm_pricing.quote(RATE, 90)     # 1.5h → 2h
    assert q["hours"] == 2 and q["labour"] == 110.0
    assert q["net"] == 170.0 and q["total"] == 204.0


def test_fixed_price_job_skips_labour():
    q = fsm_pricing.quote(RATE, 60, job_price=90)
    assert q["labour"] == 0.0 and q["hours"] == 0
    assert q["parts"] == 90.0
    assert q["net"] == 150.0            # callout 60 + fixed 90
    assert q["total"] == 180.0          # + 20% VAT


def test_min_charge_floor_applies_before_vat():
    q = fsm_pricing.quote({**RATE, "min_charge": 200.0}, 30)   # net_raw 115 < 200
    assert q["min_applied"] is True
    assert q["net"] == 200.0
    assert q["vat"] == 40.0 and q["total"] == 240.0


def test_defaults_when_no_rate_card():
    q = fsm_pricing.quote(None, 60)
    d = fsm_pricing.DEFAULT_FSM_RATE
    assert q["callout"] == d["callout_charge"]
    assert q["currency"] == "GBP" and q["vat_pct"] == 20.0
    assert q["total"] > 0
