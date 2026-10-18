"""Assets OS — Phase A (docs/os/assets.md §13): units + reused booking, the spine visible.

Pins the unit CRUD (the vehicles-CRUD twin), the unit-scoped atomic hold
(hold_unit_booking reuses the SHIPPED stay engine — reserves against a free named
unit, refuses an overlapping span, and for a fungible pool leaves unit_id unassigned),
the two-lane board (handovers today + upcoming), and the onboarding/plan/module wiring
for the new Assets industries.

No network: the shipped engine is monkeypatched to simulate free/overlapping capacity
(no new capacity math to exercise here — only the scoping); the autouse conftest guard
fails loudly on any un-stubbed live call.

Run: python -m pytest tests/test_assets.py -q
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import assets, db, onboarding, operating_systems, plans, slotgrid


# ── a tiny fake Supabase query chain (only what the fns under test touch) ─────
class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table):
        self._table = table
        self._rows = list(table.rows)

    def select(self, *a, **k): return self
    def insert(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        made = []
        for p in rows:
            row = dict(p)
            row.setdefault("id", f"u-{len(self._table.rows) + 1}")
            self._table.rows.append(row)
            self._table.inserts.append(row)
            made.append(dict(row))
        self._table.last_insert = made
        return _FakeInsert(self._table)
    def update(self, payload):
        self._table.updates.append(payload)
        self._table.last_update = [{**(self._rows[0] if self._rows else {}), **payload}]
        return self
    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def is_(self, col, _null):
        self._rows = [r for r in self._rows if r.get(col) is None]
        return self
    @property
    def not_(self):
        # minimal negating wrapper: only .is_(col, "null") is used (rows WHERE col IS NOT NULL)
        outer = self
        class _Not:
            def is_(self, col, _null):
                outer._rows = [r for r in outer._rows if r.get(col) is not None]
                return outer
        return _Not()
    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        if self._table.updates and self._table.last_update is not None:
            up, self._table.last_update = self._table.last_update, None
            return _FakeResult(up)
        return _FakeResult(self._rows)


class _FakeInsert:
    def __init__(self, table): self._table = table
    def execute(self): return _FakeResult(self._table.last_insert)


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.inserts: list = []
        self.updates: list = []
        self.last_insert: list = []
        self.last_update = None


class _FakeClient:
    def __init__(self, rows=None):
        self._t = _FakeTable(list(rows or []))

    def table(self, name): return _FakeQuery(self._t)

    @property
    def inserts(self): return self._t.inserts

    @property
    def updates(self): return self._t.updates


# ── unit CRUD (the vehicles-CRUD twin) ────────────────────────────────────────
def test_asset_list_units_filters_active():
    rows = [{"id": "u1", "tenant_id": "t1", "name": "Rose", "active": True},
            {"id": "u2", "tenant_id": "t1", "name": "Old barn", "active": False}]
    got = db.asset_list_units("t1", active=True, client=_FakeClient(rows))
    assert [u["id"] for u in got] == ["u1"]                    # inactive dropped


def test_asset_create_unit_inserts_row():
    client = _FakeClient([])
    got = db.asset_create_unit("t1", {"name": "Digger JCB-3", "category": "digger"},
                               client=client)
    assert got["name"] == "Digger JCB-3"
    assert client.inserts[0]["tenant_id"] == "t1"             # tenant-stamped


def test_asset_update_unit_scoped_and_stamps_updated_at():
    client = _FakeClient([{"id": "u1", "tenant_id": "t1", "name": "Rose"}])
    db.asset_update_unit("u1", "t1", {"rate_daily": 120}, client=client)
    payload = client.updates[-1]
    assert payload["rate_daily"] == 120
    assert payload.get("updated_at")                          # touched


def test_asset_get_unit_returns_none_when_absent():
    assert db.asset_get_unit("nope", "t1", client=_FakeClient([])) is None


# ── hold_unit_booking — NAMED unit: reserve when free, refuse when overlapping ─
def _patch_stay_engine(monkeypatch, *, fits):
    """Stub the SHIPPED primitives hold_unit_booking composes: the unit's existing
    hires read + slotgrid.stay_fits (the concurrent-occupancy decision, capacity 1)."""
    monkeypatch.setattr(db, "asset_list_unit_bookings", lambda *a, **k: [{"stub": 1}])
    monkeypatch.setattr(slotgrid, "stay_fits", lambda *a, **k: fits)
    inserted = {}

    def fake_insert(tid, date_iso, sb, lb, summary, **k):
        inserted.update({"tenant": tid, "date": date_iso, "sb": sb, "lb": lb,
                         "resource_type": k.get("resource_type"),
                         "deposit": k.get("deposit_amount")})
        return {"id": "b1", "booking_ref": "AB-1"}
    monkeypatch.setattr(db, "insert_stay_booking", fake_insert)
    return inserted


def test_hold_unit_booking_named_reserves_against_free_unit(monkeypatch):
    inserted = _patch_stay_engine(monkeypatch, fits=True)
    client = _FakeClient([{"id": "b1"}])
    got = db.hold_unit_booking("t1", "u1", 0, slotgrid.BLOCKS_PER_DAY, "Hire · Rose",
                               deposit_amount=200, contact="a@b.co",
                               booking_date_iso="2026-08-05", client=client)
    assert got is not None
    assert got["unit_id"] == "u1" and got["hire_state"] == "held"
    # the SHIPPED stay insert carried the unit as the resource_type SCOPING KEY + the deposit
    assert inserted["resource_type"] == "u1" and inserted["deposit"] == 200


def test_hold_unit_booking_named_refuses_overlap(monkeypatch):
    _patch_stay_engine(monkeypatch, fits=False)             # the unit's span is taken
    called = {"inserted": False}
    monkeypatch.setattr(db, "insert_stay_booking",
                        lambda *a, **k: called.__setitem__("inserted", True) or {"id": "x"})
    got = db.hold_unit_booking("t1", "u1", 0, slotgrid.BLOCKS_PER_DAY, "Hire · Rose",
                               booking_date_iso="2026-08-05", client=_FakeClient([]))
    assert got is None                                       # never a double-book
    assert called["inserted"] is False                      # refused BEFORE any insert


# ── hold_unit_booking — POOL unit: no availability pre-check, no unit pinned ───
def test_hold_unit_booking_pool_holds_without_unit(monkeypatch):
    """A fungible-pool hire (unit_id None) consumes 1 of N; the unit is assigned best-fit at
    handover (Phase C), so the hold carries hire_state='held' and NO unit_id — mirroring how
    hold_fleet_booking assigns a vehicle later. stay_fits is NOT called (the pool's capacity
    is scoped by the caller's resource_type, not this named-unit pre-check)."""
    fits_calls = []
    monkeypatch.setattr(slotgrid, "stay_fits",
                        lambda *a, **k: fits_calls.append(1) or True)
    monkeypatch.setattr(db, "insert_stay_booking",
                        lambda *a, **k: {"id": "b1", "booking_ref": "AB-2"})
    got = db.hold_unit_booking("t1", None, 0, slotgrid.BLOCKS_PER_DAY, "Hire · drill",
                               booking_date_iso="2026-08-05",
                               client=_FakeClient([{"id": "b1"}]))
    assert got["hire_state"] == "held" and got.get("unit_id") is None
    assert fits_calls == []                                  # no named-unit pre-check for a pool


# ── assets.board composes the handover + upcoming lanes ───────────────────────
def _hire(unit_id, arrival_ord, nights, **extra):
    from datetime import date
    return {"id": f"b-{arrival_ord}", "unit_id": unit_id, "hire_state": "held",
            "status": "confirmed", "contact": "a@b.co", "booking_ref": "R1",
            "booking_date": date.fromordinal(arrival_ord).isoformat(),
            "start_block": 0, "length_blocks": nights * slotgrid.BLOCKS_PER_DAY, **extra}


def test_board_splits_handovers_today_and_upcoming():
    from datetime import date
    now = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)
    today_ord = date(2026, 7, 8).toordinal()
    units = [{"id": "u1", "name": "Rose Cottage"}, {"id": "u2", "name": "Digger"}]
    bookings = [
        _hire("u1", today_ord, 4, deposit_amount=200),        # arrives today → OUT
        _hire("u2", today_ord - 3, 3),                        # returns today (start-3 + 3n) → BACK
        _hire("u1", today_ord + 5, 7),                        # within 14 days → UPCOMING
        _hire("u1", today_ord + 40, 2),                       # beyond horizon → dropped
        {"id": "office", "start_block": today_ord * slotgrid.BLOCKS_PER_DAY,
         "length_blocks": slotgrid.BLOCKS_PER_DAY},           # not a hire → ignored
    ]
    b = assets.board(bookings, units, now)
    dirs = sorted((h["direction"], h["unit"]) for h in b["handovers"])
    assert dirs == [("back", "Digger"), ("out", "Rose Cottage")]
    assert b["upcoming_count"] == 1 and b["upcoming"][0]["unit"] == "Rose Cottage"
    # the OUT row carries the deposit chip
    out = next(h for h in b["handovers"] if h["direction"] == "out")
    assert out["deposit"] == 200


def test_quote_unit_uses_per_unit_rate():
    q = assets.quote_unit({"rate_daily": 120, "deposit_default": 200}, nights=3)
    assert q == {"nights": 3, "rate_daily": 120.0, "total": 360.0, "deposit": 200.0}
    assert assets.quote_unit({}, 3) is None                   # no rate → no quote


# ── onboarding + OS wiring ────────────────────────────────────────────────────
def test_assets_os_is_live_and_industries_mapped():
    assert operating_systems.is_live("assets")                # Shape shows in the picker
    for iid in ("lettings", "hire", "car_hire", "tool_hire"):
        assert operating_systems.os_for_industry(iid) == "assets"


def test_assets_industries_present_live_with_data_sources():
    by_id = {i["id"]: i for i in onboarding.INDUSTRIES}
    for iid in ("lettings", "hire"):
        assert by_id[iid]["os"] == "assets" and by_id[iid]["live"] is True
        assert not by_id[iid].get("hidden")
        assert iid in onboarding.DATA_SOURCES
    # the lettings channel-manager signal is captured
    vendors = [v for src in onboarding.DATA_SOURCES["lettings"] for v in src.get("vendors", [])]
    assert "Airbnb" in vendors


# ── plan / module entitlement ────────────────────────────────────────────────
def test_assets_is_a_managed_module_via_industry_meta():
    assert "assets" in plans.MANAGED_MODULES
    assert plans.INDUSTRY_MODULE["lettings"] == "assets"
    # a lettings tenant on the +industry tier gets scheduling + assets (its OWN module)
    assert plans.modules_for("professional", "lettings") == {"scheduling", "crm", "assets"}
    assert plans.modules_for("starter", "lettings") == {"scheduling", "crm"}   # Core = base only


def test_managed_modules_match_operator_switchboard_with_assets():
    from app.admin import TOGGLABLE_MODULES
    assert set(plans.MANAGED_MODULES) == TOGGLABLE_MODULES     # the MANAGED==TOGGLABLE invariant


def test_apply_plan_modules_lettings_activates_assets_no_keyerror(monkeypatch):
    states = {}
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "lettings"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: None)
    seeded = {}
    monkeypatch.setattr(db, "seed_assets_defaults", lambda t: seeded.setdefault("t", t))
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "assets"}
    assert states["assets"] is True and states["scheduling"] is True
    assert states["tms"] is False and states["storefront"] is False   # never another vertical's
    assert seeded["t"] == "T"                                 # activation seeded example units


def test_seed_assets_defaults_inserts_when_empty(monkeypatch):
    client = _FakeClient([])
    monkeypatch.setattr(db, "db", lambda: client)
    db.seed_assets_defaults("t1")
    assert len(client.inserts) == len(db.DEFAULT_ASSET_UNITS)  # example units seeded


def test_seed_assets_defaults_idempotent_when_present(monkeypatch):
    client = _FakeClient([{"id": "u1", "tenant_id": "t1", "name": "Rose"}])
    monkeypatch.setattr(db, "db", lambda: client)
    db.seed_assets_defaults("t1")
    assert client.inserts == []                               # never re-seeds


def test_assets_tier_parked_but_module_meta_survives():
    # Two-tier collapse (2026-07-24): the OS tier is off the public catalog for
    # every industry; the module metadata stays intact for the founding-partner
    # path and any future un-parking.
    assert not any(r["key"] == "professional" for r in plans.billing_plans("lettings"))
    meta = plans.industry_module_meta("lettings")
    assert meta["name"] == "Assets OS · Lettings"
    assert "Handover & return board" in meta["features"]


# ── the module hub renders Assets OS ──────────────────────────────────────────
@pytest.fixture
def hub_client(monkeypatch):
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"assets"})
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_module_hub_renders_assets_os(hub_client):
    html = hub_client.get("/app/modules").text
    assert "Assets OS" in html
    assert "/app/assets" in html                              # its card links to the surface


# ── the /app/assets board + unit registry render, gate 404s ───────────────────
@pytest.fixture
def assets_client(monkeypatch):
    import app.dashboard as dashboard
    import app.assets_routes as ar
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"assets"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(ar.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), ar
    app.dependency_overrides.clear()


def test_assets_board_renders_two_lanes(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.assets, "view", lambda *a, **k: {
        "today": "2026-07-08", "today_label": "Wednesday 08 July",
        "handovers": [{"id": "b1", "ref": "R1", "unit": "Rose Cottage", "contact": "a@b.co",
                       "arrival": "2026-07-08", "return": "2026-07-12", "direction": "out",
                       "deposit": 200, "hire_state": "held", "status": "confirmed",
                       "unit_id": "u1"}],
        "upcoming": [], "handover_count": 1, "upcoming_count": 0, "units": []})
    r = client.get("/app/assets")
    assert r.status_code == 200
    assert "Handovers today" in r.text and "Upcoming" in r.text
    assert "Rose Cottage" in r.text


def test_assets_units_registry_renders(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "asset_list_units", lambda *a, **k: [
        {"id": "u1", "name": "Rose Cottage", "category": "cottage", "active": True,
         "rate_daily": 120, "rate_weekly": None, "deposit_default": 200,
         "resource_type": None, "asset_ref": None, "next_service_due": None}])
    r = client.get("/app/assets/units")
    assert r.status_code == 200
    assert "Rose Cottage" in r.text and "Hold hire" in r.text  # the take-a-booking flow


def test_assets_gate_404s_when_module_off(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "module_active", lambda *a, **k: False)
    assert client.get("/app/assets").status_code == 404       # _require_assets gate


def test_assets_book_holds_via_unit_scoped_engine(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "asset_get_unit",
                        lambda *a, **k: {"id": "u1", "name": "Rose", "deposit_default": 200})
    seen = {}
    monkeypatch.setattr(ar.db, "hold_unit_booking",
                        lambda t, u, sb, lb, s, **k: seen.update({"unit": u, "lb": lb}) or {"id": "b1"})
    r = client.post("/app/assets/units/u1/book",
                    data={"arrival": "2026-08-05", "nights": "7", "contact": "a@b.co"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=held" in r.headers["location"]
    assert seen["unit"] == "u1" and seen["lb"] == 7 * slotgrid.BLOCKS_PER_DAY


def test_assets_book_full_surfaces_message(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda *a, **k: {"id": "u1", "name": "Rose"})
    monkeypatch.setattr(ar.db, "hold_unit_booking", lambda *a, **k: None)   # overlap → refused
    r = client.post("/app/assets/units/u1/book",
                    data={"arrival": "2026-08-05", "nights": "2"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=full" in r.headers["location"]     # never a double-book


def test_onboarding_shows_assets_as_live_shape():
    """Assets is a live Shape on the onboarding menu (is_live + at least one live industry)."""
    assert operating_systems.is_live("assets")
    live_assets = [i for i in onboarding.INDUSTRIES
                   if i.get("os") == "assets" and i.get("live") and not i.get("hidden")]
    assert {"lettings", "hire"} <= {i["id"] for i in live_assets}


# ══════════════════════════════════════════════════════════════════════════════
# Phase B — deposits over the reused Stripe rail (docs/os/assets.md §5/§11/§14).
# Stripe is monkeypatched ENTIRELY (PaymentIntent/Refund/Checkout) — never a real call.
# ══════════════════════════════════════════════════════════════════════════════
import types

from app import assets_deposits, billing


class _StripeSpy:
    """A stand-in for the `stripe` module surface the deposit rail touches. Records every call
    so tests can assert the exact args (capture_method, amount_to_capture, metadata.kind…)."""
    def __init__(self):
        self.calls = []
        self.checkout = types.SimpleNamespace(Session=types.SimpleNamespace(create=self._sess))
        self.PaymentIntent = types.SimpleNamespace(
            create=self._rec("PaymentIntent.create", {"id": "pi_1"}),
            capture=self._rec("PaymentIntent.capture", {"id": "pi_1", "status": "succeeded"}),
            cancel=self._rec("PaymentIntent.cancel", {"id": "pi_1", "status": "canceled"}))
        self.Refund = types.SimpleNamespace(
            create=self._rec("Refund.create", {"id": "re_1"}))

    def _rec(self, name, ret):
        def fn(*a, **k):
            self.calls.append((name, a, k))
            return ret
        return fn

    def _sess(self, *a, **k):
        self.calls.append(("checkout.Session.create", a, k))
        return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1",
                "payment_intent": "pi_1"}

    def find(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def stripe_spy(monkeypatch):
    """Swap the `stripe` module INSIDE assets_deposits for the spy, and force billing.is_configured
    True + a no-op ensure_stripe_customer (no network, no real Stripe)."""
    spy = _StripeSpy()
    monkeypatch.setattr(assets_deposits, "stripe", spy)
    monkeypatch.setattr(assets_deposits.billing, "is_configured", lambda: True)
    monkeypatch.setattr(assets_deposits.billing, "ensure_stripe_customer",
                        lambda tid, email: "cus_test")
    return spy


def _booking(**extra):
    return {"id": "b1", "contact": "hirer@x.co", "summary": "Rose Cottage",
            "booking_date": "2026-08-05", "unit_id": "u1", **extra}


# ── create_deposit_intent: hold for a short hire, charge for a long one (§14) ──
def test_create_deposit_intent_uses_hold_for_short_hire(stripe_spy):
    res = assets_deposits.create_deposit_intent(
        "t1", _booking(), 200, hire_days=3, deposit_id="d1")
    assert res.ok and res.url == "https://checkout.stripe.test/cs_1"
    assert res.method == assets_deposits.METHOD_HOLD
    _, _, kw = stripe_spy.find("checkout.Session.create")[0]
    pi = kw["payment_intent_data"]
    assert pi["capture_method"] == "manual"                     # an authorised HOLD
    assert pi["metadata"]["kind"] == "deposit"                  # webhook routing marker
    assert pi["metadata"]["booking_id"] == "b1" and pi["metadata"]["deposit_id"] == "d1"
    assert kw["line_items"][0]["price_data"]["unit_amount"] == 20000   # £200 in pence


def test_create_deposit_intent_uses_charge_for_long_hire(stripe_spy):
    res = assets_deposits.create_deposit_intent(
        "t1", _booking(), 250, hire_days=10, deposit_id="d2")
    assert res.ok and res.method == assets_deposits.METHOD_CHARGE
    _, _, kw = stripe_spy.find("checkout.Session.create")[0]
    # A CHARGE = immediate capture: no manual capture_method on the intent.
    assert "capture_method" not in kw["payment_intent_data"]
    assert kw["payment_intent_data"]["metadata"]["kind"] == "deposit"


def test_create_deposit_intent_graceful_when_stripe_unconfigured(monkeypatch):
    monkeypatch.setattr(assets_deposits.billing, "is_configured", lambda: False)
    res = assets_deposits.create_deposit_intent("t1", _booking(), 200, hire_days=3)
    assert res.ok is False and "not configured" in (res.reason or "")   # never crashes


# ── refund_or_release: void a hold vs refund a charge (§11) ───────────────────
def test_refund_or_release_cancels_a_held_auth(stripe_spy):
    dep = {"external_ref": "pi_1", "method": assets_deposits.METHOD_HOLD, "amount": 200}
    res = assets_deposits.refund_or_release(dep)
    assert res.ok and res.reason == "released"
    assert stripe_spy.find("PaymentIntent.cancel")                # the hold is VOIDED
    assert not stripe_spy.find("Refund.create")


def test_refund_or_release_refunds_a_charge(stripe_spy):
    dep = {"external_ref": "pi_1", "method": assets_deposits.METHOD_CHARGE, "amount": 250}
    res = assets_deposits.refund_or_release(dep)
    assert res.ok and res.reason == "refunded"
    _, _, kw = stripe_spy.find("Refund.create")[0]
    assert kw["payment_intent"] == "pi_1"                         # money given back
    assert not stripe_spy.find("PaymentIntent.cancel")


# ── deduct: capture N of a hold / partial-refund a charge (§11) ───────────────
def test_deduct_captures_n_of_a_hold(stripe_spy):
    dep = {"external_ref": "pi_1", "method": assets_deposits.METHOD_HOLD, "amount": 200}
    res = assets_deposits.deduct(dep, 50)
    assert res.ok and res.reason == "deducted"
    _, a, kw = stripe_spy.find("PaymentIntent.capture")[0]
    assert kw["amount_to_capture"] == 5000                        # capture £50, rest auto-voids
    assert not stripe_spy.find("Refund.create")


def test_deduct_partial_refunds_a_charge(stripe_spy):
    dep = {"external_ref": "pi_1", "method": assets_deposits.METHOD_CHARGE, "amount": 200}
    res = assets_deposits.deduct(dep, 50)
    assert res.ok and res.reason == "deducted"
    _, _, kw = stripe_spy.find("Refund.create")[0]
    assert kw["amount"] == 15000                                  # refund the £150 remainder
    assert not stripe_spy.find("PaymentIntent.capture")


# ── DB layer: upsert / set_status / get, tenant-scoped ────────────────────────
def test_upsert_deposit_inserts_pending_row():
    client = _FakeClient([])
    got = db.upsert_deposit("t1", "b1", "u1", 200, client=client)
    assert got["status"] == "pending" and got["amount"] == 200
    assert client.inserts[0]["tenant_id"] == "t1" and client.inserts[0]["booking_id"] == "b1"


def test_set_deposit_status_writes_only_passed_fields():
    client = _FakeClient([{"id": "d1", "tenant_id": "t1", "status": "pending"}])
    db.set_deposit_status("d1", "t1", "held", external_ref="pi_1", client=client)
    payload = client.updates[-1]
    assert payload["status"] == "held" and payload["external_ref"] == "pi_1"
    assert "deducted_amount" not in payload                       # untouched optional not written


def test_set_deposit_status_rejects_unknown_status():
    with pytest.raises(ValueError):
        db.set_deposit_status("d1", "t1", "bogus", client=_FakeClient([]))


def test_get_deposit_for_booking_scoped():
    rows = [{"id": "d1", "tenant_id": "t1", "booking_id": "b1", "status": "held"}]
    got = db.get_deposit_for_booking("t1", "b1", client=_FakeClient(rows))
    assert got["id"] == "d1"
    assert db.get_deposit_for_booking("t1", "nope", client=_FakeClient(rows)) is None


# ── webhook routing: a deposit event → set_deposit_status; subs untouched ──────
def _webhook_client(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_xxx")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_xxx")
    from app.main import app
    return TestClient(app, follow_redirects=False)


def test_webhook_routes_deposit_event_to_set_status(monkeypatch):
    from unittest.mock import patch
    client = _webhook_client(monkeypatch)
    evt = {"id": "evt_dep", "type": "payment_intent.amount_capturable_updated",
           "data": {"object": {"id": "pi_1",
                               "metadata": {"kind": "deposit", "tenant_id": "t1",
                                            "deposit_id": "d1"}}}}
    with patch("app.stripe_webhook.stripe.Webhook.construct_event", return_value=evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.db.set_deposit_status") as set_status, \
         patch("app.stripe_webhook.billing.sync_subscription") as sync:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    set_status.assert_called_once()                              # routed to the deposit handler
    args, kw = set_status.call_args
    assert args[:3] == ("d1", "t1", "held") and kw["external_ref"] == "pi_1"
    sync.assert_not_called()                                     # never touches the sub path


def test_webhook_subscription_event_still_flows_no_regression(monkeypatch):
    """A subscription event (no metadata.kind) must STILL reach billing.sync_subscription — the
    new deposit branch is additive and never intercepts it."""
    from unittest.mock import patch
    client = _webhook_client(monkeypatch)
    evt = {"id": "evt_sub", "type": "customer.subscription.updated",
           "data": {"object": {"id": "sub_1", "customer": "cus_1"}}}
    with patch("app.stripe_webhook.stripe.Webhook.construct_event", return_value=evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.db.set_deposit_status") as set_status, \
         patch("app.stripe_webhook.billing.sync_subscription") as sync:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    sync.assert_called_once()                                   # subscription path intact
    set_status.assert_not_called()                             # deposit handler not triggered


def test_webhook_untagged_charge_refunded_still_ignored(monkeypatch):
    """charge.refunded WITHOUT metadata.kind stays a no-op (the pre-existing contract) even
    though charge.refunded is now a handled type for tagged deposit refunds."""
    from unittest.mock import patch
    client = _webhook_client(monkeypatch)
    evt = {"id": "evt_ch", "type": "charge.refunded", "data": {"object": {}}}
    with patch("app.stripe_webhook.stripe.Webhook.construct_event", return_value=evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.db.set_deposit_status") as set_status, \
         patch("app.stripe_webhook.db.set_deposit_status_by_ref") as by_ref:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    set_status.assert_not_called() and by_ref.assert_not_called()


# ── deposit routes (§7) ───────────────────────────────────────────────────────
def test_deposit_link_route_mints_and_returns_url(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_booking",
                        lambda *a, **k: _booking(deposit_amount=200, start_block=0,
                                                 length_blocks=3 * slotgrid.BLOCKS_PER_DAY))
    monkeypatch.setattr(ar.db, "upsert_deposit", lambda *a, **k: {"id": "d1"})
    monkeypatch.setattr(ar.db, "set_deposit_status", lambda *a, **k: {"id": "d1"})
    monkeypatch.setattr(ar.assets_deposits, "create_deposit_intent",
                        lambda *a, **k: ar.assets_deposits.DepositResult(
                            True, url="https://pay.test/x", method="stripe_hold",
                            external_ref="pi_1"))
    r = client.post("/api/assets/bookings/b1/deposit/link",
                    data={"amount": "200"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["url"] == "https://pay.test/x"


def test_deposit_refund_route(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_booking", lambda *a, **k: _booking())
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda *a, **k: {"id": "d1", "method": "stripe_hold", "external_ref": "pi_1"})
    monkeypatch.setattr(ar.db, "set_deposit_status", lambda *a, **k: {"id": "d1"})
    monkeypatch.setattr(ar.assets_deposits, "refund_or_release",
                        lambda dep: ar.assets_deposits.DepositResult(True, reason="released"))
    r = client.post("/api/assets/bookings/b1/deposit/refund", follow_redirects=False)
    assert r.status_code == 200 and r.json()["status"] == "released"


def test_deposit_deduct_route_sets_amount(assets_client, monkeypatch):
    client, ar = assets_client
    seen = {}
    monkeypatch.setattr(ar.db, "get_booking", lambda *a, **k: _booking())
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda *a, **k: {"id": "d1", "method": "stripe_hold",
                                         "external_ref": "pi_1", "amount": 200})
    monkeypatch.setattr(ar.db, "set_deposit_status",
                        lambda did, tid, status, **k: seen.update({"status": status, **k}))
    monkeypatch.setattr(ar.assets_deposits, "deduct",
                        lambda dep, amt, **k: ar.assets_deposits.DepositResult(True, reason="deducted"))
    r = client.post("/api/assets/bookings/b1/deposit/deduct",
                    data={"amount": "50"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["deducted_amount"] == 50.0
    assert seen["status"] == "deducted" and seen["deducted_amount"] == 50.0


# ── deposit chip renders the right state ──────────────────────────────────────
def test_deposit_chip_states():
    assert assets.deposit_chip({"status": "held"})["label"] == "✓ held"
    assert assets.deposit_chip({"status": "pending"})["label"] == "⏳ unpaid"
    assert assets.deposit_chip({"status": "refunded"})["tone"] == "muted"
    assert assets.deposit_chip({"status": "deducted", "deducted_amount": 50})["label"] == "£50 deducted"
    assert assets.deposit_chip(None) is None


def test_board_attaches_deposit_chip():
    from datetime import date
    now = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)
    today_ord = date(2026, 7, 8).toordinal()
    units = [{"id": "u1", "name": "Rose"}]
    bookings = [_hire("u1", today_ord, 4)]
    deposits = {"b-{}".format(today_ord): {"status": "held"}}
    b = assets.board(bookings, units, now, deposits=deposits)
    out = next(h for h in b["handovers"] if h["direction"] == "out")
    assert out["deposit_chip"]["label"] == "✓ held"


# ── the lifecycle sweep releases a stale unpaid hold ──────────────────────────
def test_deposit_lifecycle_releases_stale_unpaid_hold(monkeypatch):
    import asyncio
    from app.jobs import deposit_lifecycle as job
    released = {}
    monkeypatch.setattr(job.db, "tenants_with_modules", lambda mods: ["t1"])
    monkeypatch.setattr(job.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(job.db, "deposits_stale_unpaid",
                        lambda t, cutoff: [{"id": "d1", "booking_id": "b1", "status": "pending"}])
    monkeypatch.setattr(job.db, "get_booking",
                        lambda bid, tid: {"id": "b1", "contact": "x@y.co", "summary": "Rose"})
    monkeypatch.setattr(job.db, "release_hire",
                        lambda bid, tid: released.setdefault("hire", bid))
    monkeypatch.setattr(job.db, "set_deposit_status",
                        lambda did, tid, status, **k: released.setdefault("dep_status", status))
    monkeypatch.setattr(job.db, "reachable_contact", lambda c: True)
    monkeypatch.setattr(job.db, "insert_email_log", lambda *a, **k: ("log1", True))
    monkeypatch.setattr(job.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(*a, **k):
        return SimpleNamespace(text="Still going ahead?", model="m", usage={})
    monkeypatch.setattr(job.agent, "compose_outbound", fake_compose)

    out = asyncio.run(job.run())
    assert released["hire"] == "b1"                              # the hold is freed
    assert released["dep_status"] == "released"                 # the deposit is released
    assert out["released"] == 1 and out["drafted"] == 1         # a "still going ahead?" note drafted


# ── the handover sweep chases an unpaid deposit ───────────────────────────────
def test_handover_reminder_chases_unpaid_deposit(monkeypatch):
    import asyncio
    from app.jobs import send_handover_reminders as job
    logged = {}
    monkeypatch.setattr(job.db, "tenants_with_modules", lambda mods: ["t1"])
    monkeypatch.setattr(job.db, "hires_arriving_on",
                        lambda t, d: [{"id": "b1", "contact": "x@y.co", "summary": "Rose",
                                       "details": None}])
    monkeypatch.setattr(job.db, "reachable_contact", lambda c: True)
    monkeypatch.setattr(job.db, "effective_config", lambda *a, **k: {"auto_send": False})
    # UNPAID deposit → the chase branch (paid=False).
    monkeypatch.setattr(job.db, "get_deposit_for_booking",
                        lambda t, b: {"id": "d1", "status": "pending"})
    monkeypatch.setattr(job.db, "insert_email_log",
                        lambda tid, to, subj, *a, **k: logged.update({"subject": subj}) or ("log1", True))
    monkeypatch.setattr(job.db, "stamp_handover_reminded", lambda *a, **k: None)
    monkeypatch.setattr(job.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(*a, **k):
        return SimpleNamespace(text="Please pay your deposit.", model="m", usage={})
    monkeypatch.setattr(job.agent, "compose_outbound", fake_compose)

    out = asyncio.run(job.run())
    assert out["drafted"] == 1
    assert logged["subject"] == "One step to secure your hire"   # the deposit chase, not the paid note


# ══════════════════════════════════════════════════════════════════════════════
# Phase C — condition capture + photo-diffs (docs/os/assets.md §6.3/§7/§12b/§14).
# The Anthropic VISION call AND Supabase Storage are mocked ENTIRELY — never a real
# call (the loud-guard conftest also blocks live Supabase). The novel feature: a
# before/after LLM comparison producing damage-claim evidence, gated + human-approved.
# ══════════════════════════════════════════════════════════════════════════════
import asyncio
import json

from app import assets_condition, storage


# ── a fake Anthropic vision client (records the two image blocks per call) ─────
class _FakeVision:
    """Stands in for agent.client(): one messages.create returning the §12b findings
    JSON. Records the content blocks so a test can assert TWO images went per pair."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, *a, **k):
        self.calls.append(k)
        text = json.dumps(self._payload)
        return SimpleNamespace(
            model="claude-sonnet-test",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            content=[SimpleNamespace(type="text", text=text)])


def _patch_storage(monkeypatch):
    """Mock app.storage so no bucket is ever touched: download returns dummy bytes,
    signed_url a fake URL, upload a fake path."""
    monkeypatch.setattr(storage, "download_bytes", lambda path, **k: b"\xff\xd8dummy")
    monkeypatch.setattr(storage, "signed_url", lambda path, *a, **k: f"https://signed/{path}")
    monkeypatch.setattr(storage, "upload_photo",
                        lambda t, b, phase, idx, data, **k: f"{t}/{b}/{phase}/{idx}.jpg")


def _patch_vision(monkeypatch, payload):
    vision = _FakeVision(payload)
    monkeypatch.setattr(assets_condition, "client", lambda: vision)
    monkeypatch.setattr(assets_condition.usage, "record_anthropic", lambda *a, **k: None)
    return vision


# ── db: condition reports + baseline lookup ───────────────────────────────────
def test_insert_condition_report_persists_phase_and_paths():
    client = _FakeClient([])
    got = db.insert_condition_report("t1", "u1", "b1", "out",
                                     rating=5, photo_urls=["p0", "p1"],
                                     captured_by="Sam", client=client)
    assert got["phase"] == "out" and got["photo_urls"] == ["p0", "p1"]
    assert client.inserts[0]["tenant_id"] == "t1" and client.inserts[0]["booking_id"] == "b1"


def test_insert_condition_report_rejects_unknown_phase():
    with pytest.raises(ValueError):
        db.insert_condition_report("t1", "u1", "b1", "sideways", client=_FakeClient([]))


def test_latest_out_report_returns_baseline_or_none():
    rows = [{"id": "r1", "tenant_id": "t1", "booking_id": "b1", "phase": "out"}]
    got = db.latest_out_report("t1", "b1", client=_FakeClient(rows))
    assert got["id"] == "r1"
    assert db.latest_out_report("t1", "b1", client=_FakeClient([])) is None   # no baseline


def test_insert_and_resolve_damage_case():
    client = _FakeClient([{"id": "c1", "tenant_id": "t1"}])
    db.resolve_damage_case("c1", "t1", resolution="deducted", resolved_amount=50, client=client)
    payload = client.updates[-1]
    assert payload["status"] == "resolved" and payload["resolution"] == "deducted"
    assert payload["resolved_amount"] == 50


def test_capture_token_roundtrip():
    rows = [{"id": "b1", "tenant_id": "t1", "capture_token": "tok123"}]
    got = db.get_booking_by_capture_token("tok123", client=_FakeClient(rows))
    assert got["id"] == "b1"
    assert db.get_booking_by_capture_token("nope", client=_FakeClient(rows)) is None


# ── the diff pipeline: pairs by ordinal, runs the vision call, GATES (§12b) ────
def _in_report(paths=("in0", "in1")):
    return {"id": "in1", "tenant_id": "t1", "booking_id": "b1", "unit_id": "u1",
            "phase": "in", "photo_urls": list(paths)}


def _out_baseline(paths=("out0", "out1")):
    return {"id": "out1", "tenant_id": "t1", "booking_id": "b1", "phase": "out",
            "photo_urls": list(paths)}


def test_diff_pairs_by_ordinal_and_sends_two_images(monkeypatch):
    _patch_storage(monkeypatch)
    vision = _patch_vision(monkeypatch, {"findings": [], "clean_return_note": "all good"})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline())
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report(), unit={"category": "car"}))
    assert verdict["outcome"] == "clean"                       # empty findings → clean
    # TWO image blocks per pair, TWO pairs (out0↔in0, out1↔in1) → 2 calls.
    assert len(vision.calls) == 2
    blocks = vision.calls[0]["messages"][0]["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2                                     # a BEFORE and an AFTER


def test_diff_high_confidence_moderate_opens_case(monkeypatch):
    _patch_storage(monkeypatch)
    _patch_vision(monkeypatch, {"findings": [
        {"area": "bumper", "change": "new scratch", "severity": "moderate", "confidence": 0.9}]})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline(("out0",)))
    opened = {}
    monkeypatch.setattr(assets_condition.db, "insert_damage_case",
                        lambda *a, **k: opened.update(k) or {"id": "c1", **k})
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report(("in0",))))
    assert verdict["outcome"] == "damage_claim"                # ≥0.80 + moderate → a claim
    assert verdict["severity"] == "moderate"
    assert opened["status"] == "open"
    # each finding carries its out/in path (the evidence pairing).
    assert verdict["findings"][0]["out_url"] == "out0" and verdict["findings"][0]["in_url"] == "in0"


def test_diff_mid_confidence_review_only(monkeypatch):
    _patch_storage(monkeypatch)
    _patch_vision(monkeypatch, {"findings": [
        {"area": "seat", "change": "possible stain", "severity": "moderate", "confidence": 0.65}]})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline(("out0",)))
    monkeypatch.setattr(assets_condition.db, "insert_damage_case",
                        lambda *a, **k: {"id": "c1", **k})
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report(("in0",))))
    assert verdict["outcome"] == "review"                      # 0.55–0.80 → review, no draft


def test_diff_low_confidence_is_clean(monkeypatch):
    _patch_storage(monkeypatch)
    _patch_vision(monkeypatch, {"findings": [
        {"area": "door", "change": "maybe a mark", "severity": "minor", "confidence": 0.3}]})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline(("out0",)))
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report(("in0",))))
    assert verdict["outcome"] == "clean"                       # <0.55 → clean return


def test_diff_no_baseline_cannot_claim(monkeypatch):
    _patch_storage(monkeypatch)
    _patch_vision(monkeypatch, {"findings": [
        {"area": "x", "change": "y", "severity": "major", "confidence": 0.99}]})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: None)   # NO baseline
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report()))
    assert verdict["outcome"] == "no_baseline"                 # no baseline, no claim (§14)
    assert verdict["damage_case"] is None


def test_diff_storage_failure_is_manual_review(monkeypatch):
    """A storage/vision failure never crashes and never passes as clean — a case is opened
    flagged for manual review."""
    monkeypatch.setattr(storage, "download_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(storage.StorageError("down")))
    _patch_vision(monkeypatch, {"findings": []})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline(("out0",)))
    opened = {}
    monkeypatch.setattr(assets_condition.db, "insert_damage_case",
                        lambda *a, **k: opened.update(k) or {"id": "c1"})
    verdict = asyncio.run(assets_condition.run_diff("t1", _in_report(("in0",))))
    assert verdict["outcome"] == "manual_review"
    assert "review" in (opened["description"] or "").lower()


# ── the capture magic-link page (GET/POST, no auth) ───────────────────────────
@pytest.fixture
def capture_client(monkeypatch):
    from app.main import app
    from app import assets_routes as ar
    return TestClient(app), ar


def test_capture_get_shows_out_shot_list(capture_client, monkeypatch):
    client, ar = capture_client
    monkeypatch.setattr(ar.db, "get_booking_by_capture_token",
                        lambda tok: {"id": "b1", "tenant_id": "t1", "unit_id": "u1",
                                     "booking_ref": "R1"})
    monkeypatch.setattr(ar.db, "asset_get_unit",
                        lambda uid, tid, **k: {"id": "u1", "name": "Rose", "category": "cottage"})
    monkeypatch.setattr(ar.db, "latest_out_report", lambda t, b: None)   # → phase 'out'
    r = client.get("/capture/tok123")
    assert r.status_code == 200
    assert "Handover (out)" in r.text and "Rose" in r.text
    assert "Living area" in r.text                             # the cottage shot list


def test_capture_get_unknown_token_404(capture_client, monkeypatch):
    client, ar = capture_client
    monkeypatch.setattr(ar.db, "get_booking_by_capture_token", lambda tok: None)
    assert client.get("/capture/nope").status_code == 404


def test_capture_post_out_persists_report_no_diff(capture_client, monkeypatch):
    client, ar = capture_client
    _patch_storage(monkeypatch)
    monkeypatch.setattr(ar.db, "get_booking_by_capture_token",
                        lambda tok: {"id": "b1", "tenant_id": "t1", "unit_id": "u1"})
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, tid, **k: {"id": "u1", "name": "Rose"})
    monkeypatch.setattr(ar.db, "latest_out_report", lambda t, b: None)   # first capture → 'out'
    saved = {}
    monkeypatch.setattr(ar.db, "insert_condition_report",
                        lambda t, u, b, phase, **k: saved.update({"phase": phase, **k}) or {"id": "r1", "phase": phase})
    diffed = {"ran": False}
    monkeypatch.setattr(ar, "_run_condition_diff",
                        lambda *a, **k: diffed.__setitem__("ran", True))
    r = client.post("/capture/tok123",
                    files={"photo_00": ("front.jpg", b"\xff\xd8x", "image/jpeg")},
                    data={"rating": "5", "captured_by": "Sam"})
    assert r.status_code == 200 and "Condition captured" in r.text
    assert saved["phase"] == "out" and saved["photo_urls"] == ["t1/b1/out/0.jpg"]
    assert diffed["ran"] is False                              # no diff on the OUT report


def test_capture_post_in_triggers_diff(capture_client, monkeypatch):
    client, ar = capture_client
    _patch_storage(monkeypatch)
    monkeypatch.setattr(ar.db, "get_booking_by_capture_token",
                        lambda tok: {"id": "b1", "tenant_id": "t1", "unit_id": "u1"})
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, tid, **k: {"id": "u1", "name": "Rose"})
    monkeypatch.setattr(ar.db, "latest_out_report", lambda t, b: _out_baseline())   # baseline exists → 'in'
    monkeypatch.setattr(ar.db, "insert_condition_report",
                        lambda t, u, b, phase, **k: {"id": "r2", "phase": phase, "booking_id": b,
                                                     "unit_id": u, "photo_urls": k.get("photo_urls")})
    ran = {}
    async def fake_diff(tid, report, unit):
        ran.update({"phase": report["phase"]})
    monkeypatch.setattr(ar, "_run_condition_diff", fake_diff)
    r = client.post("/capture/tok123",
                    files={"photo_00": ("front.jpg", b"\xff\xd8x", "image/jpeg")},
                    data={"rating": "3"})
    assert r.status_code == 200
    assert ran["phase"] == "in"                                # the return report triggers the diff


def test_capture_post_storage_unavailable_is_graceful(capture_client, monkeypatch):
    """A Storage outage records the report with storage_error and shows a clear message —
    never crashes the capture."""
    client, ar = capture_client
    monkeypatch.setattr(storage, "upload_photo",
                        lambda *a, **k: (_ for _ in ()).throw(storage.StorageError("no bucket")))
    monkeypatch.setattr(ar.db, "get_booking_by_capture_token",
                        lambda tok: {"id": "b1", "tenant_id": "t1", "unit_id": "u1"})
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, tid, **k: {"id": "u1", "name": "Rose"})
    monkeypatch.setattr(ar.db, "latest_out_report", lambda t, b: None)
    saved = {}
    monkeypatch.setattr(ar.db, "insert_condition_report",
                        lambda t, u, b, phase, **k: saved.update(k) or {"id": "r1", "phase": phase})
    r = client.post("/capture/tok123",
                    files={"photo_00": ("front.jpg", b"\xff\xd8x", "image/jpeg")},
                    data={"captured_by": "Sam"})
    assert r.status_code == 200
    assert "photo storage was unavailable" in r.text.lower()   # graceful message
    assert saved["storage_error"] is True and saved["photo_urls"] == []   # report still recorded


# ── the drafter touchpoint: damage claim FORCED pending; clean auto-sends ──────
def test_run_condition_diff_damage_claim_is_pending_never_autosent(monkeypatch):
    from app import assets_routes as ar
    logged = {}
    async def fake_run_diff(t, r, unit=None, cfg=None):
        return {"outcome": "damage_claim", "findings": [
            {"area": "wall", "change": "burn mark", "severity": "major"}],
            "diff_confidence": 0.9, "severity": "major", "proposed_deduction": None}
    monkeypatch.setattr(ar.assets_condition, "run_diff", fake_run_diff)
    monkeypatch.setattr(ar.db, "get_booking",
                        lambda b, t: {"id": "b1", "contact": "x@y.co", "thread_id": None})
    monkeypatch.setattr(ar.db, "reachable_contact", lambda c: True)
    # auto_send TRUE — the claim must STILL be pending (deposits are dispute-prone, §12b/§14).
    monkeypatch.setattr(ar.db, "effective_config", lambda *a, **k: {"auto_send": True})
    monkeypatch.setattr(ar.db, "insert_email_log",
                        lambda t, to, subj, *a, **k: logged.update({"status": k.get("status"), "subj": subj}) or ("log1", True))
    monkeypatch.setattr(ar.usage, "record_anthropic", lambda *a, **k: None)
    sent = {"count": 0}
    async def fake_send(*a, **k):
        sent["count"] += 1
    monkeypatch.setattr(ar.mailbox, "send_mail", fake_send)
    async def fake_compose(*a, **k):
        return SimpleNamespace(text="We found a burn mark.", model="m", usage={})
    monkeypatch.setattr("app.agent.compose_outbound", fake_compose)

    asyncio.run(ar._run_condition_diff("t1", {"booking_id": "b1"}, {"name": "Rose"}))
    assert logged["status"] == "pending"                       # forced pending
    assert sent["count"] == 0                                  # NEVER auto-sent, even with auto_send


def test_run_condition_diff_clean_drafts_refund(monkeypatch):
    from app import assets_routes as ar
    logged = {}
    async def fake_run_diff(t, r, unit=None, cfg=None):
        return {"outcome": "clean", "findings": [], "diff_confidence": 0.1, "severity": "none",
                "proposed_deduction": None}
    monkeypatch.setattr(ar.assets_condition, "run_diff", fake_run_diff)
    monkeypatch.setattr(ar.db, "get_booking",
                        lambda b, t: {"id": "b1", "contact": "x@y.co", "thread_id": None})
    monkeypatch.setattr(ar.db, "reachable_contact", lambda c: True)
    monkeypatch.setattr(ar.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(ar.db, "insert_email_log",
                        lambda t, to, subj, *a, **k: logged.update({"subj": subj}) or ("log1", True))
    monkeypatch.setattr(ar.usage, "record_anthropic", lambda *a, **k: None)
    async def fake_compose(*a, **k):
        return SimpleNamespace(text="Deposit on its way back.", model="m", usage={})
    monkeypatch.setattr("app.agent.compose_outbound", fake_compose)
    asyncio.run(ar._run_condition_diff("t1", {"booking_id": "b1"}, {"name": "Rose"}))
    assert "deposit" in logged["subj"].lower()                 # a clean-return refund note drafted


# ── the damage queue + evidence bundle render; resolve wires to the deposit ────
def test_damage_queue_renders(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "list_damage_cases",
                        lambda t, status=None, **k: [
                            {"id": "c1", "severity": "moderate", "description": "bumper scratch",
                             "diff_confidence": 0.9, "resolution": None}])
    r = client.get("/app/assets/damage")
    assert r.status_code == 200
    assert "bumper scratch" in r.text and "Evidence bundle" in r.text


def test_evidence_bundle_page_renders(assets_client, monkeypatch):
    client, ar = assets_client
    _patch_storage(monkeypatch)
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "unit_id": "u1", "booking_id": "b1",
                                             "out_report_id": "o1", "in_report_id": "i1",
                                             "severity": "moderate", "status": "open",
                                             "diff_confidence": 0.9,
                                             "diff_findings": [{"area": "wall", "change": "scuff",
                                                                "severity": "moderate", "confidence": 0.9,
                                                                "out_url": "out0", "in_url": "in0"}]})
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, t, **k: {"id": "u1", "name": "Rose"})
    monkeypatch.setattr(ar.db, "get_booking", lambda b, t, **k: {"id": "b1", "contact": "x@y.co"})
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda t, b, **k: {"id": "d1", "amount": 200, "status": "held"})
    monkeypatch.setattr(ar.db, "get_condition_report",
                        lambda rid, t, **k: {"id": rid, "captured_at": "2026-07-08T10:00:00",
                                             "captured_by": "Sam"})
    r = client.get("/app/assets/damage/c1")
    assert r.status_code == 200
    assert "scuff" in r.text
    assert "https://signed/out0" in r.text and "https://signed/in0" in r.text  # side-by-side


def test_resolve_deducted_calls_deposit_deduct(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "booking_id": "b1",
                                             "out_report_id": "o1"})   # has baseline
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda t, b, **k: {"id": "d1", "method": "stripe_hold",
                                           "external_ref": "pi_1", "amount": 200})
    seen = {}
    monkeypatch.setattr(ar.assets_deposits, "deduct",
                        lambda dep, amt, **k: seen.update({"amt": amt, "case": k.get("damage_case_id")})
                        or ar.assets_deposits.DepositResult(True, reason="deducted"))
    monkeypatch.setattr(ar.db, "set_deposit_status", lambda *a, **k: {"id": "d1"})
    resolved = {}
    monkeypatch.setattr(ar.db, "resolve_damage_case",
                        lambda cid, t, **k: resolved.update(k) or {"id": cid})
    r = client.post("/api/assets/damage/c1/resolve",
                    data={"resolution": "deducted", "amount": "50"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["amount"] == 50.0
    assert seen["amt"] == 50.0 and seen["case"] == "c1"        # Phase B deduct wired w/ case id
    assert resolved["resolution"] == "deducted"


def test_resolve_deducted_hard_blocked_without_baseline(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "booking_id": "b1",
                                             "out_report_id": None})   # NO baseline
    blocked = {"deduct": False}
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda t, b, **k: {"id": "d1", "amount": 200})
    monkeypatch.setattr(ar.assets_deposits, "deduct",
                        lambda *a, **k: blocked.__setitem__("deduct", True))
    r = client.post("/api/assets/damage/c1/resolve",
                    data={"resolution": "deducted", "amount": "50"}, follow_redirects=False)
    assert r.status_code == 400 and "baseline" in r.json()["reason"].lower()
    assert blocked["deduct"] is False                          # no baseline, no claim (§14)


def test_resolve_refunded_calls_refund_or_release(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "booking_id": "b1", "out_report_id": "o1"})
    monkeypatch.setattr(ar.db, "get_deposit_for_booking",
                        lambda t, b, **k: {"id": "d1", "method": "stripe_hold", "external_ref": "pi_1"})
    called = {"refund": False}
    monkeypatch.setattr(ar.assets_deposits, "refund_or_release",
                        lambda dep: called.__setitem__("refund", True)
                        or ar.assets_deposits.DepositResult(True, reason="released"))
    monkeypatch.setattr(ar.db, "set_deposit_status", lambda *a, **k: {"id": "d1"})
    monkeypatch.setattr(ar.db, "resolve_damage_case", lambda cid, t, **k: {"id": cid})
    r = client.post("/api/assets/damage/c1/resolve",
                    data={"resolution": "refunded"}, follow_redirects=False)
    assert r.status_code == 200 and r.json()["resolution"] == "refunded"
    assert called["refund"] is True


def test_damage_rerun_reruns_diff(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "unit_id": "u1", "in_report_id": "i1"})
    monkeypatch.setattr(ar.db, "get_condition_report",
                        lambda rid, t, **k: {"id": rid, "booking_id": "b1", "phase": "in",
                                             "photo_urls": ["in0"]})
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, t, **k: {"id": "u1", "name": "Rose"})
    async def fake_run_diff(t, r, unit=None):
        return {"outcome": "clean", "diff_confidence": 0.1, "severity": "none"}
    monkeypatch.setattr(ar.assets_condition, "run_diff", fake_run_diff)
    r = client.post("/api/assets/damage/c1/rerun", follow_redirects=False)
    assert r.status_code == 200 and r.json()["outcome"] == "clean"


# ── storage helper: graceful when the bucket path errors ──────────────────────
def test_storage_signed_url_none_on_failure(monkeypatch):
    class _Boom:
        def storage(self): raise RuntimeError
    def boom_client():
        c = SimpleNamespace()
        c.storage = SimpleNamespace(from_=lambda b: (_ for _ in ()).throw(RuntimeError("down")))
        return c
    monkeypatch.setattr(storage, "db", boom_client)
    assert storage.signed_url("some/path") is None             # never crashes the page
    assert storage.signed_url("") is None


# ══════════════════════════════════════════════════════════════════════════════
# Phase D — maintenance + polish (docs/os/assets.md §2/§6/§11/§12b/§13). COMPLETES
# Assets OS. Maintenance = the FSM recurring-service cadence, by DAYS; the auto
# proposed_deduction is a SUGGESTION only; the PDF reuses invoice_pdf.py; pool
# assignment reuses the best_fit selection. Everything monkeypatched — no live call.
# ══════════════════════════════════════════════════════════════════════════════
from datetime import date, timedelta

from app import assets_condition, industry_lingo


# ── asset_units_due_service: by service window AND by condition<floor (§6/§11) ──
def test_units_due_service_surfaces_by_window_and_by_floor(monkeypatch):
    soon = (date.today() + timedelta(days=5)).isoformat()
    far = (date.today() + timedelta(days=90)).isoformat()
    rows = [
        {"id": "u1", "tenant_id": "t1", "name": "Digger", "active": True, "next_service_due": soon,
         "condition_rating": 5.0, "condition_floor": 3.0},        # due soon → in
        {"id": "u2", "tenant_id": "t1", "name": "Marquee", "active": True, "next_service_due": far,
         "condition_rating": 2.5, "condition_floor": 3.5},        # below floor → in
        {"id": "u3", "tenant_id": "t1", "name": "Kayak", "active": True, "next_service_due": far,
         "condition_rating": 5.0, "condition_floor": 3.0},        # neither → out
    ]
    got = db.asset_units_due_service("t1", within_days=14, client=_FakeClient(rows))
    ids = {u["id"] for u in got}
    assert ids == {"u1", "u2"}                                    # u3 (healthy, far) excluded
    u1 = next(u for u in got if u["id"] == "u1")
    u2 = next(u for u in got if u["id"] == "u2")
    assert u1["_due_soon"] and not u1["_below_floor"]
    assert u2["_below_floor"] and not u2["_due_soon"]


# ── complete_unit_service: advance next_service_due by service_interval_days ────
def test_complete_unit_service_advances_by_interval_days(monkeypatch):
    serviced_on = "2026-07-08"
    monkeypatch.setattr(db, "asset_get_unit",
                        lambda uid, tid, client=None: {
                            "id": "u1", "service_interval_days": 90,
                            "condition_rating": 2.0, "custom_data": {"_shot_list": ["Front"]}})
    seen = {}
    client = _FakeClient([{"id": "u1", "tenant_id": "t1"}])

    def capture_update(self, payload):          # grab the update payload off the fake query
        seen.update(payload)
        self._table.updates.append(payload)
        self._table.last_update = [{"id": "u1", **payload}]
        return self
    monkeypatch.setattr(_FakeQuery, "update", capture_update)

    db.complete_unit_service("u1", "t1", serviced_on=serviced_on, client=client)
    # advanced by the INTERVAL in DAYS (the FSM cadence twin, by days not months)
    assert seen["next_service_due"] == (date(2026, 7, 8) + timedelta(days=90)).isoformat()
    assert seen["condition_rating"] == 5.0                       # lifted back toward pristine
    assert seen["custom_data"]["_last_serviced_on"] == serviced_on
    assert seen["custom_data"]["_shot_list"] == ["Front"]        # never clobbers other keys


def test_complete_unit_service_clears_due_when_no_interval(monkeypatch):
    monkeypatch.setattr(db, "asset_get_unit",
                        lambda uid, tid, client=None: {"id": "u1", "service_interval_days": None,
                                                       "condition_rating": 5.0, "custom_data": {}})
    seen = {}

    def capture_update(self, payload):
        seen.update(payload)
        self._table.updates.append(payload)
        self._table.last_update = [{"id": "u1", **payload}]
        return self
    monkeypatch.setattr(_FakeQuery, "update", capture_update)
    db.complete_unit_service("u1", "t1", serviced_on="2026-07-08",
                             client=_FakeClient([{"id": "u1"}]))
    assert seen["next_service_due"] is None                      # a one-off service clears the date


# ── the board's maintenance lane renders (§2) ─────────────────────────────────
def test_maintenance_lane_composes_rows(monkeypatch):
    soon = (date.today() + timedelta(days=3)).isoformat()
    monkeypatch.setattr(db, "asset_units_due_service", lambda t, within_days=14, client=None: [
        {"id": "u1", "name": "Reg AB12 CDE", "next_service_due": soon,
         "condition_rating": 5.0, "condition_floor": 3.0, "_due_soon": True, "_below_floor": False},
        {"id": "u2", "name": "Marquee", "next_service_due": None,
         "condition_rating": 2.0, "condition_floor": 3.5, "_due_soon": False, "_below_floor": True}])
    rows = assets.maintenance_lane("t1")
    assert rows[0]["unit"] == "Reg AB12 CDE" and rows[0]["due_soon"]
    assert "service due" in rows[0]["reason"]
    assert rows[1]["below_floor"] and "below floor" in rows[1]["reason"]


def test_view_attaches_maintenance_lane(monkeypatch):
    now = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "list_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "asset_list_units", lambda *a, **k: [])
    monkeypatch.setattr(db, "deposits_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_damage_cases", lambda *a, **k: [])
    monkeypatch.setattr(db, "asset_units_due_service", lambda t, within_days=14, client=None: [
        {"id": "u1", "name": "Digger", "next_service_due": "2026-07-14",
         "condition_rating": 5.0, "condition_floor": 3.0, "_due_soon": True, "_below_floor": False}])
    b = assets.view("t1", now=now)
    assert b["maintenance_count"] == 1 and b["maintenance"][0]["unit"] == "Digger"


# ── the /serviced route marks a unit serviced ─────────────────────────────────
def test_serviced_route_completes_service(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "asset_get_unit", lambda uid, tid, **k: {"id": "u1", "name": "Digger"})
    seen = {}
    monkeypatch.setattr(ar.db, "complete_unit_service",
                        lambda uid, tid, **k: seen.update({"unit": uid, **k}) or {"id": uid})
    r = client.post("/app/assets/units/u1/serviced", data={}, follow_redirects=False)
    assert r.status_code == 303 and "msg=serviced" in r.headers["location"]
    assert seen["unit"] == "u1"


# ── insurer seed appears for an Assets industry (§11) ─────────────────────────
def test_insurer_seed_present_for_assets_industries():
    for industry in ("lettings", "hire"):
        seeds = industry_lingo.crm_seeds_for(industry)
        labels = [f["label"] for f in seeds.get("company", ())]
        assert "Insurer" in labels                               # the LLM-autofilled insurer field


# ── auto proposed_deduction from repair-cost config + severity (§12b) ─────────
def test_repair_cost_sums_by_severity_from_config():
    cfg = {"assets_repair_costs": {"cottage": {"minor": 25, "moderate": 75, "major": 200}}}
    findings = [{"severity": "moderate"}, {"severity": "minor"}]
    assert assets_condition.repair_cost_for(cfg, "cottage", findings) == 100.0   # 75 + 25


def test_repair_cost_none_without_config():
    findings = [{"severity": "major"}]
    assert assets_condition.repair_cost_for(None, "cottage", findings) is None
    assert assets_condition.repair_cost_for({}, "cottage", findings) is None      # stays None (today's behaviour)


def test_repair_cost_falls_back_to_default_category():
    cfg = {"assets_repair_costs": {"_default": {"major": 150}}}
    assert assets_condition.repair_cost_for(cfg, "unknown", [{"severity": "major"}]) == 150.0


def test_run_diff_seeds_proposed_deduction_from_config(monkeypatch):
    _patch_storage(monkeypatch)
    _patch_vision(monkeypatch, {"findings": [
        {"area": "wall", "change": "burn mark", "severity": "major", "confidence": 0.95}]})
    monkeypatch.setattr(assets_condition.db, "latest_out_report", lambda t, b: _out_baseline(("out0",)))
    opened = {}
    monkeypatch.setattr(assets_condition.db, "insert_damage_case",
                        lambda *a, **k: opened.update(k) or {"id": "c1", **k})
    cfg = {"assets_repair_costs": {"cottage": {"major": 300}}}
    verdict = asyncio.run(assets_condition.run_diff(
        "t1", _in_report(("in0",)), unit={"category": "cottage"}, cfg=cfg))
    assert verdict["outcome"] == "damage_claim"
    assert verdict["proposed_deduction"] == 300.0               # a SUGGESTION seeded onto the case
    assert opened["proposed_deduction"] == 300.0                # persisted on insert
    # ...but WITHOUT config it stays None (the owner types it — today's behaviour).
    opened.clear()
    verdict2 = asyncio.run(assets_condition.run_diff(
        "t1", _in_report(("in0",)), unit={"category": "cottage"}))
    assert verdict2["proposed_deduction"] is None


# ── the PDF evidence-bundle export (§12b) ─────────────────────────────────────
def test_damage_export_pdf_route_returns_pdf(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_damage_case",
                        lambda cid, t, **k: {"id": "c1", "unit_id": "u1", "booking_id": "b1234567",
                                             "severity": "moderate", "status": "open"})
    monkeypatch.setattr(ar.assets, "evidence_bundle",
                        lambda t, case, **k: {"case": case, "unit": {"name": "Rose"},
                                              "booking": {}, "deposit": {}, "findings": [],
                                              "out_report": {}, "in_report": {}, "has_baseline": True})
    monkeypatch.setattr(ar.assets_pdf, "render", lambda bundle, **k: b"%PDF-1.4 fake")
    r = client.get("/app/assets/damage/c1/export.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert "attachment" in r.headers["content-disposition"]


def test_assets_pdf_render_produces_pdf_bytes(monkeypatch):
    """The generator itself (reusing invoice_pdf's fpdf2 spine) yields a real PDF, and a
    photo download failure degrades to a placeholder rather than crashing."""
    from app import assets_pdf
    monkeypatch.setattr(assets_pdf.storage, "download_bytes",
                        lambda p, **k: (_ for _ in ()).throw(RuntimeError("no bucket")))
    bundle = {
        "case": {"id": "c1", "severity": "major", "status": "open",
                 "description": "wall: burn mark (major)", "diff_confidence": 0.9,
                 "proposed_deduction": 200},
        "unit": {"name": "Rose Cottage"}, "booking": {"contact": "x@y.co", "booking_ref": "R1"},
        "deposit": {"amount": 200, "status": "held"},
        "findings": [{"area": "wall", "change": "burn mark", "severity": "major",
                      "confidence": 0.9, "out_url": "out0", "in_url": "in0"}],
        "out_report": {"captured_by": "Sam", "captured_at": "2026-07-08T10:00:00"},
        "in_report": {"captured_by": "Sam", "captured_at": "2026-07-12T10:00:00"},
        "has_baseline": True}
    out = assets_pdf.render(bundle)
    assert isinstance(out, (bytes, bytearray)) and out[:4] == b"%PDF"


# ── assign_pool_unit: picks a free unit + sets unit_id; handles a full pool ────
def test_assign_pool_unit_picks_free_unit(monkeypatch):
    monkeypatch.setattr(db, "get_booking",
                        lambda bid, tid, client=None: {"id": "b1", "unit_id": None,
                                                       "resource_type": "drill"})
    monkeypatch.setattr(db, "asset_units_for_pool",
                        lambda t, pool, client=None: [{"id": "u1"}, {"id": "u2"}])
    # u1 is already tied up by another active hire → u2 is the free pick.
    active = _FakeClient([{"tenant_id": "t1", "unit_id": "u1", "status": "confirmed"}])
    monkeypatch.setattr(db, "db", lambda: active)
    seen = {}

    def capture_update(self, payload):
        seen.update(payload)
        self._table.updates.append(payload)
        self._table.last_update = [{"id": "b1", **payload}]
        return self
    monkeypatch.setattr(_FakeQuery, "update", capture_update)
    got = db.assign_pool_unit("b1", "t1", client=active)
    assert got["id"] == "u2"                                     # the free one, best-fit deterministic
    assert seen["unit_id"] == "u2"                               # pinned onto the booking


def test_assign_pool_unit_full_pool_returns_none(monkeypatch):
    monkeypatch.setattr(db, "get_booking",
                        lambda bid, tid, client=None: {"id": "b1", "unit_id": None,
                                                       "resource_type": "drill"})
    monkeypatch.setattr(db, "asset_units_for_pool",
                        lambda t, pool, client=None: [{"id": "u1"}])
    # u1 is taken → no free unit.
    taken = _FakeClient([{"tenant_id": "t1", "unit_id": "u1", "status": "confirmed"}])
    got = db.assign_pool_unit("b1", "t1", client=taken)
    assert got is None                                           # full pool → no assignment


def test_assign_pool_unit_idempotent_when_already_pinned(monkeypatch):
    monkeypatch.setattr(db, "get_booking",
                        lambda bid, tid, client=None: {"id": "b1", "unit_id": "u9"})
    monkeypatch.setattr(db, "asset_get_unit",
                        lambda uid, tid, client=None: {"id": uid, "name": "Drill 9"})
    got = db.assign_pool_unit("b1", "t1", client=_FakeClient([]))
    assert got["id"] == "u9"                                     # already assigned → no-op


def test_assign_unit_route(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_booking", lambda bid, t, **k: {"id": "b1", "unit_id": None})
    monkeypatch.setattr(ar.db, "assign_pool_unit",
                        lambda bid, tid, **k: {"id": "u2", "name": "Drill 2"})
    r = client.post("/app/assets/bookings/b1/assign-unit", follow_redirects=False)
    assert r.status_code == 200 and r.json()["unit_id"] == "u2"


def test_assign_unit_route_full_pool(assets_client, monkeypatch):
    client, ar = assets_client
    monkeypatch.setattr(ar.db, "get_booking", lambda bid, t, **k: {"id": "b1", "unit_id": None})
    monkeypatch.setattr(ar.db, "assign_pool_unit", lambda bid, tid, **k: None)
    r = client.post("/app/assets/bookings/b1/assign-unit", follow_redirects=False)
    assert r.status_code == 200 and r.json()["ok"] is False and "free unit" in r.json()["reason"]


# ── the owner maintenance nudge reuses the FSM cadence, deduped per unit ───────
def test_owner_service_nudge_drafts_once_per_unit(monkeypatch):
    from app.jobs import send_handover_reminders as job
    monkeypatch.setattr(job.db, "tenants_with_modules", lambda mods: ["t1"])
    monkeypatch.setattr(job.db, "effective_config", lambda *a, **k: {"auto_send": True})
    monkeypatch.setattr(job.db, "asset_units_due_service",
                        lambda t, within_days=14: [
                            {"id": "u1", "name": "Digger", "next_service_due": "2026-07-14",
                             "condition_rating": 5.0, "condition_floor": 3.0,
                             "_due_soon": True, "_below_floor": False, "custom_data": {}},
                            {"id": "u2", "name": "Marquee", "next_service_due": None,
                             "condition_rating": 2.0, "condition_floor": 3.5,
                             "_due_soon": False, "_below_floor": True,
                             "custom_data": {"_service_nudged_at": "2026-07-01"}}])  # already nudged
    monkeypatch.setattr(job.db, "get_tenant", lambda t: {"mailbox": "owner@x.co"})
    monkeypatch.setattr(job.db, "reachable_contact", lambda c: bool(c))
    monkeypatch.setattr(job.db, "hires_arriving_on", lambda t, d: [])
    logged = {}
    monkeypatch.setattr(job.db, "insert_email_log",
                        lambda t, to, subj, *a, **k: logged.update({"to": to, "subj": subj, "status": k.get("status")}) or ("log1", True))
    stamped = []
    monkeypatch.setattr(job.db, "stamp_unit_service_nudged", lambda uid, tid: stamped.append(uid))
    monkeypatch.setattr(job.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(*a, **k):
        return SimpleNamespace(text="Digger is due a service.", model="m", usage={})
    monkeypatch.setattr(job.agent, "compose_outbound", fake_compose)

    out = asyncio.run(job.run())
    assert out["service_nudges"] == 1                            # one digest drafted
    assert logged["to"] == "owner@x.co" and logged["status"] == "pending"   # internal → pending
    assert stamped == ["u1"]                                     # only the un-nudged unit stamped (u2 skipped)


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    from tests.booking_fixtures import carried_config
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
