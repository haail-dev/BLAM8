"""Additional-depot add-on (2026-07-11): £149/depot/mo beyond the included
first depot. Billing rules mirror the mailbox add-on; these tests pin the
counting logic and the no-op guards (the Stripe reconcile itself is exercised
by _reconcile_addon_item, shared with the proven mailbox path)."""
from app import billing


def _branches(monkeypatch, n):
    monkeypatch.setattr(billing.db, "list_branches",
                        lambda *a, **k: [{"id": f"b{i}"} for i in range(n)])


def test_billable_depots_first_included(monkeypatch):
    _branches(monkeypatch, 3)
    t = {"plan": "professional", "subscription_status": "active"}
    assert billing.billable_depots(t, "t1") == 2
    _branches(monkeypatch, 1)
    assert billing.billable_depots(t, "t1") == 0


def test_billable_depots_enterprise_and_unpaid_bill_nothing(monkeypatch):
    _branches(monkeypatch, 5)
    assert billing.billable_depots(
        {"plan": "enterprise", "subscription_status": "active"}, "t1") == 0
    assert billing.billable_depots(
        {"plan": "professional", "subscription_status": None}, "t1") == 0


def test_sync_depot_addon_noop_without_price_id(monkeypatch):
    # Unconfigured price id must short-circuit BEFORE any tenant/Stripe access —
    # the launch state until STRIPE_EXTRA_DEPOT_PRICE_ID lands on Render.
    monkeypatch.setattr(billing.plans, "extra_depot_price_id", lambda: None)
    monkeypatch.setattr(billing.db, "get_tenant",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("touched db")))
    billing.sync_depot_addon("t1")  # must not raise


def test_sync_depot_addon_reconciles_overage(monkeypatch):
    monkeypatch.setattr(billing.plans, "extra_depot_price_id", lambda: "price_depot")
    monkeypatch.setattr(billing.db, "get_tenant", lambda *a, **k: {
        "plan": "professional", "subscription_status": "active",
        "stripe_subscription_id": "sub_1"})
    _branches(monkeypatch, 4)
    seen = {}
    monkeypatch.setattr(billing, "_reconcile_addon_item",
                        lambda sub_id, price_id, qty: seen.update(
                            sub=sub_id, price=price_id, qty=qty))
    billing.sync_depot_addon("t1")
    assert seen == {"sub": "sub_1", "price": "price_depot", "qty": 3}
