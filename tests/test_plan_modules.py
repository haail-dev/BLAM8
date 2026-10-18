"""Plan-bundled module entitlement (src/app/plans.py + app/db.apply_plan_modules +
billing.sync_subscription). A paid subscription auto-activates the plan's modules,
so upgrading on the billing page is self-serve — no operator step.

Run: python -m pytest tests/test_plan_modules.py -q
"""
from app import plans
from app.admin import TOGGLABLE_MODULES


def test_managed_modules_match_operator_switchboard():
    # the plan map and the operator switchboard must agree on what's toggleable
    assert set(plans.MANAGED_MODULES) == TOGGLABLE_MODULES


def test_plan_module_bundles_are_industry_aware():
    # base module by tier, plus the tenant's OWN industry module on +industry tiers
    assert plans.modules_for("email", "logistics") == {"scheduling"}   # Starter £39 = replies + booking
    assert plans.modules_for("starter", "logistics") == {"scheduling", "crm"}
    assert plans.modules_for("professional", "logistics") == {"scheduling", "crm", "tms"}
    assert plans.modules_for("scale", "logistics") == {"scheduling", "crm", "tms"}
    assert plans.modules_for("enterprise", "logistics") == {"scheduling", "crm", "tms"}
    # trades is a built vertical now — paid tiers add its OWN module (fsm), never tms
    assert plans.modules_for("professional", "trades") == {"scheduling", "crm", "fsm"}
    assert plans.modules_for("scale", "trades") == {"scheduling", "crm", "fsm"}
    # a vertical with no built module gets base only — never another vertical's module
    assert plans.modules_for("professional", "other") == {"scheduling", "crm"}
    assert plans.modules_for("professional", None) == {"scheduling", "crm"}
    # construction is a core-only Field OS sector (no dispatch module): base only,
    # and it must onboard/entitle without KeyError on the missing industry module
    assert plans.modules_for("professional", "construction") == {"scheduling", "crm"}
    assert plans.modules_for("starter", "construction") == {"scheduling", "crm"}
    # free / unknown tiers: nothing
    assert plans.modules_for("pilot", "logistics") == set()
    assert plans.modules_for(None, "logistics") == set()


def test_apply_plan_modules_logistics_activates_and_seeds_tms(monkeypatch):
    from app import db
    calls = []
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "logistics"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: calls.append(("set", m, a)))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: calls.append(("seed_fleet",)))
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: calls.append(("seed_tms",)))
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "tms"}
    assert ("set", "scheduling", True) in calls and ("set", "tms", True) in calls
    assert ("seed_fleet",) in calls and ("seed_tms",) in calls   # no empty tables


def test_apply_plan_modules_non_logistics_never_gets_tms(monkeypatch):
    # the bug this fixes: a paid NON-logistics tenant must never receive TMS
    from app import db
    states, seeded = {}, []
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "other"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: seeded.append(1))
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm"}
    # every vertical module stays OFF for an industry with no built module
    assert states == {"scheduling": True, "crm": True, "concierge": False, "tms": False, "fsm": False,
                      "practice": False, "storefront": False, "assets": False,
                      "events": False, "wholesale": False}
    assert seeded == []


def test_apply_plan_modules_trades_activates_and_seeds_fsm(monkeypatch):
    from app import db
    states, seeded = {}, []
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "trades"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: seeded.append("fleet"))
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: seeded.append("tms"))
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: seeded.append("fsm"))
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: seeded.append("assets"))
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "fsm"}
    assert states == {"scheduling": True, "crm": True, "concierge": False, "tms": False, "fsm": True,   # OWN module only
                      "practice": False, "storefront": False, "assets": False,
                      "events": False, "wholesale": False}
    assert seeded == ["fsm"]                                          # never tms's seeding


def test_apply_plan_modules_downgrade_pauses_industry_module(monkeypatch):
    from app import db
    states, seeded = {}, []
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "logistics"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: seeded.append(1))
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: seeded.append(1))
    db.apply_plan_modules("T", "starter")     # Core = scheduling only
    assert states == {"scheduling": True, "crm": True, "concierge": False, "tms": False, "fsm": False,  # paused on downgrade
                      "practice": False, "storefront": False, "assets": False,
                      "events": False, "wholesale": False}
    assert seeded == []


def test_billing_plans_sells_exactly_the_loop_tiers():
    # Two-tier collapse (2026-07-24): Starter (£39, key 'email') and Pro (£99,
    # key 'starter'); since 2026-09-25 Pro + Concierge (£249, key 'concierge')
    # sits above them — for EVERY industry, never flagged in_development.
    for industry in ("trades", "logistics", "other", None):
        rows = plans.billing_plans(industry)
        assert [r["key"] for r in rows] == ["email", "starter", "concierge"], industry
        assert [r["name"] for r in rows] == ["Starter", "Pro", "Pro + Concierge"], industry
        assert not any(r.get("in_development") for r in rows)
        assert rows[2]["price"] == "£249" and any("Concierge" in f for f in rows[2]["features"])
        assert not any("Concierge" in f for f in rows[0]["features"] + rows[1]["features"])
        starter, pro = rows[0], rows[1]
        assert starter.get("highlight")          # "Most popular to start"
        # Tier ladder (2026-07-25): Starter = replies + booking; the CRM is
        # Pro's headline unlock — it must never be advertised on Starter.
        assert any("Scheduling" in f for f in starter["features"])
        assert not any("CRM" in f for f in starter["features"])
        assert any("CRM" in f for f in pro["features"])


def test_professional_and_enterprise_parked_but_keys_still_valid():
    # Parked 2026-07-24 (the Scale pattern): off the public catalog, but a
    # legacy subscription's plan key must keep resolving — modules, mailbox
    # allowance, meter row.
    rows = plans.billing_plans("logistics")
    assert not any(r["key"] in ("professional", "enterprise") for r in rows)
    assert plans.modules_for("professional", "logistics") == {"scheduling", "crm", "tms"}
    assert plans.included_mailboxes("professional") == 2
    assert plans.included_mailboxes("enterprise") is None
    from app.usage import PLANS
    assert PLANS["professional"]["price_gbp"] == 248


def test_scale_retired_from_catalog_but_key_still_valid():
    # Scale (£799) came off the menu 2026-07-11 — the depot ladder replaced it.
    # The public catalog no longer sells it, but a legacy subscription's plan
    # key must keep resolving: modules, mailbox allowance, meter row.
    assert not any(r["key"] == "scale" for r in plans.billing_plans("logistics"))
    assert plans.modules_for("scale", "logistics") == {"scheduling", "crm", "tms"}
    assert plans.included_mailboxes("scale") == 5     # legacy allowance kept
    from app.usage import PLANS
    assert PLANS["scale"]["price_gbp"] == 799


def test_depot_pricing_never_advertised_on_public_tiers():
    # The additional-depot line item (£149) is a Field OS concept. With the OS
    # tiers parked (2026-07-24) it must not surface on any public row — the
    # add-on BILLING stays live for legacy OS tenants (test_depot_addon.py).
    for industry in ("logistics", "trades", "dentist", "wholesale"):
        for row in plans.billing_plans(industry):
            assert not any("depot" in f.lower() for f in row["features"]), (industry, row["key"])


def test_included_depots_first_free_enterprise_unlimited():
    assert plans.included_depots("professional") == 1
    assert plans.included_depots("starter") == 1
    assert plans.included_depots("enterprise") is None


def test_paid_trial_pins_to_signup_window_not_a_fresh_period():
    # the money leak this fixes: a paid trial must end at the ORIGINAL 14-day
    # window from sign-up, never grant a fresh 14 days on top of the pilot.
    from datetime import datetime, timezone, timedelta
    from app import trial
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    signup = now - timedelta(days=11)                     # 3 days left in the window
    window_end = signup + timedelta(days=trial.settings.trial_days)
    te = trial.paid_trial_end({"trial_start_at": signup.isoformat()}, now=now)
    assert te == int(window_end.timestamp())              # pinned to sign-up + 14d
    assert te < int((now + timedelta(days=trial.settings.trial_days)).timestamp())  # NOT a fresh 14


def test_paid_trial_none_when_window_used_up_or_nearly_gone(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from app import trial
    now = datetime(2026, 1, 20, tzinfo=timezone.utc)
    # window fully gone → no trial (subscription bills now)
    assert trial.paid_trial_end({"trial_start_at": (now - timedelta(days=30)).isoformat()}, now=now) is None
    # under Stripe's ~48h floor → no trial
    assert trial.paid_trial_end({"trial_start_at": (now - timedelta(days=13)).isoformat()}, now=now) is None
    # comfortably inside the window → a trial end is returned
    assert trial.paid_trial_end({"trial_start_at": (now - timedelta(days=5)).isoformat()}, now=now) is not None


def test_sync_subscription_activates_modules_when_active(monkeypatch):
    from app import billing, db
    monkeypatch.setattr(db, "stripe_get_tenant_id_by_customer", lambda c: "T")
    monkeypatch.setattr(db, "stripe_set_subscription", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_tenant_plan", lambda *a, **k: None)
    applied = []
    monkeypatch.setattr(db, "apply_plan_modules", lambda t, p: applied.append((t, p)))
    monkeypatch.setattr(billing, "price_to_plan", lambda: {"price_pro": "professional"})
    sub = {"customer": "cus_1", "id": "sub_1", "status": "active",
           "items": {"data": [{"price": {"id": "price_pro"}}]}}
    billing.sync_subscription(sub)
    assert applied == [("T", "professional")]


def test_sync_subscription_skips_modules_when_inactive(monkeypatch):
    from app import billing, db
    monkeypatch.setattr(db, "stripe_get_tenant_id_by_customer", lambda c: "T")
    monkeypatch.setattr(db, "stripe_set_subscription", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_tenant_plan", lambda *a, **k: None)
    applied = []
    monkeypatch.setattr(db, "apply_plan_modules", lambda t, p: applied.append(1))
    monkeypatch.setattr(billing, "price_to_plan", lambda: {"price_pro": "professional"})
    sub = {"customer": "cus_1", "id": "sub_1", "status": "past_due",
           "items": {"data": [{"price": {"id": "price_pro"}}]}}
    billing.sync_subscription(sub)
    assert applied == []   # not active/trialing → modules untouched
