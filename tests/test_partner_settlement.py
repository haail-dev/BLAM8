"""Partner settlement maths — credit first, then a withdrawal.

The rules being pinned here are the ones that were argued out rather than guessed:
a credit can never take a bill below zero, the retained buffer exists for clawbacks
(not to secure the discount), and it grows only gently with run-rate because ongoing
commission is itself collateral.

Run: python -m pytest tests/test_partner_settlement.py -q
"""
import pytest

from app import partner_settlement as ps

STARTER = 39.0
PRO_COMMISSION = 19.80          # 20% of a £99 Pro subscription


# ── mode handling ─────────────────────────────────────────────────────────────
def test_unknown_or_missing_mode_falls_back_to_cash():
    # Existing partners signed up to bank payouts; never silently switch them.
    for bad in (None, "", "  ", "nonsense", "CREDIT!"):
        assert ps.normalize_mode(bad) == "cash"


def test_mode_is_case_insensitive():
    assert ps.normalize_mode("Credit") == "credit" and ps.normalize_mode(" CASH ") == "cash"


# ── the credit cannot exceed the bill ─────────────────────────────────────────
def test_two_pro_referrals_make_a_starter_subscription_free():
    assert ps.credit_applied(PRO_COMMISSION * 2, STARTER) == STARTER


def test_one_referral_only_part_pays_the_bill():
    assert ps.credit_applied(PRO_COMMISSION, STARTER) == 19.80


def test_credit_never_exceeds_the_bill_however_big_the_balance():
    # Stripe cannot take an invoice below zero; this is that limit as a rule.
    assert ps.credit_applied(500.0, STARTER) == STARTER


def test_credit_is_zero_for_a_partner_with_no_subscription():
    # A pure referrer has no bill to reduce.
    assert ps.credit_applied(200.0, 0) == 0.0


def test_negative_or_junk_inputs_never_produce_negative_credit():
    assert ps.credit_applied(-50, STARTER) == 0.0
    assert ps.credit_applied("abc", STARTER) == 0.0


# ── the clawback buffer ───────────────────────────────────────────────────────
def test_buffer_at_two_referrals_is_about_one_month_plus_a_slice():
    b = ps.clawback_buffer(STARTER, PRO_COMMISSION * 2)
    assert b == pytest.approx(46.92, abs=0.01)


def test_buffer_grows_with_run_rate_but_stays_bounded():
    small = ps.clawback_buffer(STARTER, PRO_COMMISSION * 2)     # 2 referrals
    big = ps.clawback_buffer(STARTER, PRO_COMMISSION * 15)      # 15 referrals
    assert big > small
    # Capped at 3x the subscription: sizing it to "everyone refunds at once" would
    # trap hundreds against a near-zero risk.
    assert big <= STARTER * ps.BUFFER_CAP_MULTIPLE


def test_buffer_is_capped_even_at_an_absurd_run_rate():
    assert ps.clawback_buffer(STARTER, 10_000.0) == STARTER * ps.BUFFER_CAP_MULTIPLE


def test_buffer_grows_far_slower_than_the_commission_that_drives_it():
    # 7.5x the referrals must not mean 7.5x the money held back.
    small = ps.clawback_buffer(STARTER, PRO_COMMISSION * 2)
    big = ps.clawback_buffer(STARTER, PRO_COMMISSION * 15)
    assert big / small < 3.0


def test_a_pure_referrer_with_no_subscription_still_holds_a_slice_back():
    # No service to protect, but the clawback risk is unchanged.
    assert ps.clawback_buffer(0, 100.0) == pytest.approx(20.0, abs=0.01)


# ── what is actually withdrawable ─────────────────────────────────────────────
def test_nothing_is_withdrawable_until_the_buffer_is_covered():
    assert ps.available_to_withdraw(30.0, STARTER, PRO_COMMISSION * 2, "cash") == 0.0


def test_on_credit_the_bill_is_settled_before_anything_is_withdrawable():
    # 5 Pro referrals = £99 cleared. £39 settles the bill and the buffer holds £58.80,
    # leaving a £1.20 residue — nowhere near the minimum, so still nothing to take.
    v = ps.view(cleared_gbp=99.0, subscription_gbp=STARTER,
                monthly_commission_gbp=99.0, mode="credit")
    assert v["credit_applied"] == STARTER and v["is_free_this_month"] is True
    assert v["available"] == 1.20 and v["can_withdraw"] is False


def test_on_cash_the_bill_is_not_deducted_but_the_buffer_still_is():
    cash = ps.available_to_withdraw(200.0, STARTER, 99.0, "cash")
    credit = ps.available_to_withdraw(200.0, STARTER, 99.0, "credit")
    assert cash == round(credit + STARTER, 2)


def test_surplus_above_bill_and_buffer_is_withdrawable():
    v = ps.view(cleared_gbp=300.0, subscription_gbp=STARTER,
                monthly_commission_gbp=99.0, mode="credit")
    assert v["available"] > 0 and v["can_withdraw"] is True


def test_available_never_goes_negative():
    assert ps.available_to_withdraw(0, STARTER, 99.0, "credit") == 0.0
    assert ps.available_to_withdraw(-100, STARTER, 99.0, "cash") == 0.0


# ── the minimum ───────────────────────────────────────────────────────────────
def test_below_the_minimum_you_cannot_withdraw():
    v = ps.view(cleared_gbp=STARTER + 46.92 + 10, subscription_gbp=STARTER,
                monthly_commission_gbp=PRO_COMMISSION * 2, mode="credit")
    assert 0 < v["available"] < ps.MIN_WITHDRAWAL_GBP
    assert v["can_withdraw"] is False and "minimum" in v["blocked_reason"]


def test_a_request_below_the_minimum_settles_at_zero():
    assert ps.clamp_request(20.0, 500.0) == 0.0


def test_a_request_is_clamped_to_what_is_available():
    # The UI is a slider, so an out-of-range value means a hand-built request.
    assert ps.clamp_request(10_000.0, 120.0) == 120.0


def test_a_valid_request_passes_through_untouched():
    assert ps.clamp_request(75.0, 120.0) == 75.0


def test_junk_and_negative_requests_settle_at_zero():
    assert ps.clamp_request("lots", 500.0) == 0.0
    assert ps.clamp_request(-75.0, 500.0) == 0.0


# ── the reasons a partner is shown ────────────────────────────────────────────
def test_nothing_cleared_says_so_rather_than_showing_a_dead_button():
    v = ps.view(cleared_gbp=0, subscription_gbp=STARTER, monthly_commission_gbp=0)
    assert "Nothing cleared yet" in v["blocked_reason"]


def test_all_going_to_the_subscription_is_explained_as_such():
    v = ps.view(cleared_gbp=STARTER, subscription_gbp=STARTER,
                monthly_commission_gbp=STARTER, mode="credit")
    assert v["available"] == 0.0
    assert "going to your subscription" in v["blocked_reason"]


def test_a_withdrawable_partner_has_no_blocked_reason():
    v = ps.view(cleared_gbp=400.0, subscription_gbp=STARTER,
                monthly_commission_gbp=99.0, mode="credit")
    assert v["blocked_reason"] is None and v["can_withdraw"] is True


# ── the view a partner sees ───────────────────────────────────────────────────
def test_view_reports_the_bill_after_credit_and_the_free_month():
    v = ps.view(cleared_gbp=PRO_COMMISSION * 2, subscription_gbp=STARTER,
                monthly_commission_gbp=PRO_COMMISSION * 2, mode="credit")
    assert v["bill_after_credit"] == 0.0 and v["is_free_this_month"] is True


def test_one_referral_is_not_yet_a_free_month():
    v = ps.view(cleared_gbp=PRO_COMMISSION, subscription_gbp=STARTER,
                monthly_commission_gbp=PRO_COMMISSION, mode="credit")
    assert v["bill_after_credit"] == 19.20 and v["is_free_this_month"] is False


def test_cash_mode_never_reports_a_free_month_or_a_credit():
    v = ps.view(cleared_gbp=500.0, subscription_gbp=STARTER,
                monthly_commission_gbp=99.0, mode="cash")
    assert v["credit_applied"] == 0.0 and v["is_free_this_month"] is False
    assert v["bill_after_credit"] == STARTER


def test_view_of_a_brand_new_partner_is_all_zeros_and_safe():
    v = ps.view(cleared_gbp=0, subscription_gbp=0, monthly_commission_gbp=0)
    assert v["available"] == 0.0 and v["can_withdraw"] is False
    assert v["is_free_this_month"] is False


# ── choosing which commission rows a withdrawal consumes ──────────────────────
def _c(cid, amount, earned="2026-06-01T00:00:00+00:00"):
    return {"id": cid, "amount": amount, "earned_at": earned}


def test_selection_takes_whole_rows_up_to_the_target():
    rows = [_c("a", 19.80), _c("b", 19.80), _c("c", 19.80)]
    ids, total = ps.select_for_amount(rows, 45.0)
    assert ids == ["a", "b"] and total == 39.60


def test_selection_never_exceeds_the_target():
    rows = [_c("a", 30.0), _c("b", 30.0)]
    _, total = ps.select_for_amount(rows, 50.0)
    assert total == 30.0


def test_selection_takes_the_oldest_rows_first():
    rows = [_c("new", 19.80, "2026-07-01T00:00:00+00:00"),
            _c("old", 19.80, "2026-05-01T00:00:00+00:00")]
    ids, _ = ps.select_for_amount(rows, 19.80)
    assert ids == ["old"]


def test_a_row_that_does_not_fit_is_skipped_not_split():
    # Splitting would fork the ledger's one-row-per-collected-invoice invariant.
    rows = [_c("big", 100.0), _c("small", 10.0)]
    ids, total = ps.select_for_amount(rows, 50.0)
    assert ids == ["small"] and total == 10.0


def test_clawbacks_are_always_included_even_when_over_target():
    # Clause 4.7: a refund deducts from the very next statement.
    rows = [_c("claw", -25.0), _c("a", 50.0)]
    ids, total = ps.select_for_amount(rows, 30.0)
    assert "claw" in ids and total == 25.0


def test_including_a_clawback_lets_more_positive_rows_fit():
    rows = [_c("claw", -10.0), _c("a", 30.0), _c("b", 30.0)]
    _, total = ps.select_for_amount(rows, 50.0)
    assert total == 50.0            # -10 + 30 + 30


def test_a_net_negative_balance_settles_nothing_and_leaves_clawbacks_open():
    rows = [_c("claw", -80.0), _c("a", 20.0)]
    ids, total = ps.select_for_amount(rows, 500.0)
    assert ids == [] and total == 0.0


def test_a_zero_target_settles_nothing():
    assert ps.select_for_amount([_c("a", 19.80)], 0) == ([], 0.0)


def test_selection_of_an_empty_ledger_is_safe():
    assert ps.select_for_amount([], 100.0) == ([], 0.0)


def test_zero_amount_rows_are_ignored():
    ids, total = ps.select_for_amount([_c("zero", 0.0), _c("a", 19.80)], 100.0)
    assert ids == ["a"] and total == 19.80


def test_credit_selection_overshoots_so_the_bill_actually_reaches_zero():
    """£19.80 commission rows against a £39 bill: stopping under the target would credit
    only £19.80 and leave 60p outstanding, so "refer two, yours is free" would be a lie.
    A Stripe credit larger than the invoice carries the remainder forward."""
    rows = [_c("a", 19.80), _c("b", 19.80)]
    ids, total = ps.select_for_amount(rows, 39.0, allow_overshoot=True)
    assert ids == ["a", "b"] and total == 39.60


def test_overshoot_stops_at_the_row_that_crosses_the_target():
    rows = [_c("a", 19.80), _c("b", 19.80), _c("c", 19.80)]
    ids, total = ps.select_for_amount(rows, 39.0, allow_overshoot=True)
    assert ids == ["a", "b"] and total == 39.60      # 'c' is not swept in too


def test_cash_withdrawals_never_overshoot():
    # Money out of the door must never exceed what was asked for.
    rows = [_c("a", 19.80), _c("b", 19.80)]
    _, total = ps.select_for_amount(rows, 39.0)
    assert total == 19.80
