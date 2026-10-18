"""Storefront OS — Phase 1 (docs/os/storefront.md §13).

Pins the catalogue dedupe (lower(name)), the atomic stock-in (get-or-create +
increment RPC + a movement row), the atomic reserve (reserves when available,
returns None = out_of_stock when not — the hold_booking_if_capacity discipline),
the transaction-status stamp (completed_at), the /app/shop daily surface render,
and the onboarding/plan/module wiring for the new Storefront industries.

No network: the db is stubbed (a fake Supabase client where the real db fn is
exercised, monkeypatched fns elsewhere) — the autouse conftest guard fails
loudly on any un-stubbed live call.

Run: python -m pytest tests/test_storefront.py -q
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, onboarding, operating_systems, plans, industry_lingo


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
        row = dict(payload)
        row.setdefault("id", f"prod-{len(self._table.rows) + 1}")
        self._table.rows.append(row)
        self._table.inserts.append(row)
        self._table.last_insert = [dict(row)]
        return _FakeInsert(self._table)
    def update(self, payload): self._table.updates.append(payload); return self
    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def in_(self, col, vals):
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self
    def ilike(self, col, pat):
        needle = pat.strip("%").lower()
        self._rows = [r for r in self._rows if needle == str(r.get(col) or "").lower()]
        return self
    def or_(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def offset(self, *a, **k): return self
    def execute(self): return _FakeResult(self._rows)


class _FakeInsert:
    def __init__(self, table): self._table = table
    def execute(self): return _FakeResult(self._table.last_insert)


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.inserts: list = []
        self.updates: list = []
        self.last_insert: list = []


class _FakeClient:
    def __init__(self, rows=None):
        self._t = _FakeTable(list(rows or []))

    def table(self, name): return _FakeQuery(self._t)

    @property
    def inserts(self): return self._t.inserts

    @property
    def updates(self): return self._t.updates


# ── shop_get_or_create_product dedupes on lower(name) ─────────────────────────
def test_get_or_create_product_dedupes_on_lower_name():
    existing = {"id": "p1", "tenant_id": "t1", "name": "Sourdough"}
    client = _FakeClient([existing])
    # a second get-or-create with different case returns the SAME row, no insert
    got = db.shop_get_or_create_product("t1", "sourdough", client=client)
    assert got["id"] == "p1"
    assert client.inserts == []                                # never forked


def test_get_or_create_product_inserts_when_absent():
    client = _FakeClient([])
    got = db.shop_get_or_create_product("t1", "Rye loaf", client=client)
    assert got["name"] == "Rye loaf"
    assert len(client.inserts) == 1                            # created once


# ── shop_apply_stock_in: get-or-create + increment RPC + a movement per line ──
def test_apply_stock_in_creates_increments_and_writes_movements(monkeypatch):
    made = {}

    def fake_get_or_create(tid, name, **k):
        made.setdefault(name, {"id": f"p-{name}", "name": name})
        return made[name]
    monkeypatch.setattr(db, "shop_get_or_create_product", fake_get_or_create)
    monkeypatch.setattr(db, "shop_update_product", lambda *a, **k: None)

    rpc_calls = []

    class _RpcClient:
        def table(self, name):
            # the pre-existing lookup for created/updated accounting → nothing on record
            return _FakeQuery(_FakeTable([]))
        def rpc(self, fn, params):
            rpc_calls.append((fn, params))
            return SimpleNamespace(execute=lambda: _FakeResult([{}]))
    monkeypatch.setattr(db, "db", lambda: _RpcClient())

    out = db.shop_apply_stock_in("t1", [
        {"name": "Maris Piper", "qty": 12, "unit": "crate", "best_before": "2026-07-20"},
        {"name": "Eggs", "qty": 6},
        {"name": "  ", "qty": 5},          # blank name → skipped
        {"name": "Milk", "qty": 0},        # zero qty → skipped
    ], source="manual")

    assert out == {"created": 2, "updated": 0, "movements": 2}
    # one atomic increment RPC per real line, carrying the best-before through
    assert [c[0] for c in rpc_calls] == ["shop_apply_stock_in_one", "shop_apply_stock_in_one"]
    assert rpc_calls[0][1]["p_delta"] == 12.0
    assert rpc_calls[0][1]["p_best_before"] == "2026-07-20"
    assert rpc_calls[0][1]["p_source"] == "manual"


# ── shop_hold_stock_if_available: reserves when available, None when not ───────
def test_hold_stock_reserves_when_available(monkeypatch):
    tx_row = {"id": "tx1", "status": "reserved"}

    class _RpcClient:
        def rpc(self, fn, params):
            assert fn == "shop_hold_stock_if_available"
            assert params["p_qty"] == 2.0
            return SimpleNamespace(execute=lambda: _FakeResult([tx_row]))
    monkeypatch.setattr(db, "db", lambda: _RpcClient())

    got = db.shop_hold_stock_if_available("t1", "p1", 2, contact_id="c1")
    assert got == tx_row                                       # the reserved transaction


def test_hold_stock_returns_none_when_out_of_stock(monkeypatch):
    class _RpcClient:
        # the RPC returns an EMPTY set when not enough is available (the clean signal)
        def rpc(self, fn, params):
            return SimpleNamespace(execute=lambda: _FakeResult([]))
    monkeypatch.setattr(db, "db", lambda: _RpcClient())

    got = db.shop_hold_stock_if_available("t1", "p1", 99)
    assert got is None                                         # out_of_stock


# ── shop_set_transaction_status stamps completed_at on 'completed' ────────────
def test_set_transaction_status_stamps_completed_at():
    client = _FakeClient([{"id": "tx1", "tenant_id": "t1", "status": "reserved"}])
    db.shop_set_transaction_status("tx1", "t1", "completed", client=client)
    payload = client.updates[-1]
    assert payload["status"] == "completed"
    assert payload.get("completed_at")                        # stamped


def test_set_transaction_status_ready_does_not_stamp_completed():
    client = _FakeClient([{"id": "tx1", "tenant_id": "t1", "status": "reserved"}])
    db.shop_set_transaction_status("tx1", "t1", "ready", client=client)
    payload = client.updates[-1]
    assert payload["status"] == "ready"
    assert "completed_at" not in payload                       # only 'completed' stamps


# ── shop_today_collections filters to due/overdue reservations ────────────────
def test_today_collections_keeps_due_and_overdue_drops_future():
    rows = [
        {"id": "a", "tenant_id": "t1", "status": "reserved", "ready_date": "2026-07-01"},  # overdue → kept
        {"id": "b", "tenant_id": "t1", "status": "ready", "ready_date": "2026-07-05"},     # today → kept
        {"id": "c", "tenant_id": "t1", "status": "reserved", "ready_date": "2026-07-20"},  # future → dropped
        {"id": "d", "tenant_id": "t1", "status": "reserved", "ready_date": None},          # undated → kept
    ]
    got = db.shop_today_collections("t1", "2026-07-05", client=_FakeClient(rows))
    assert sorted(r["id"] for r in got) == ["a", "b", "d"]


# ── onboarding + OS wiring ────────────────────────────────────────────────────
def test_storefront_os_is_live_and_industries_mapped():
    assert operating_systems.is_live("storefront")            # Shape shows in the picker
    for iid in ("farm_shop", "deli", "bakery", "wine_merchant", "nursery"):
        assert operating_systems.os_for_industry(iid) == "storefront"


def test_storefront_industries_present_live_with_data_sources():
    by_id = {i["id"]: i for i in onboarding.INDUSTRIES}
    for iid in ("farm_shop", "deli", "bakery", "wine_merchant", "nursery"):
        assert by_id[iid]["os"] == "storefront" and by_id[iid]["live"] is True
        assert iid in onboarding.DATA_SOURCES
    # the wedge signal — "None yet" POS is an option for a no-till shop
    vendors = [v for src in onboarding.DATA_SOURCES["farm_shop"] for v in src.get("vendors", [])]
    assert "None yet" in vendors


def test_storefront_lingo_and_crm_seeds_present():
    assert "shop" in (industry_lingo.seed_for("farm_shop") or "").lower()
    seeds = industry_lingo.crm_seeds_for("farm_shop")
    assert seeds["deal"] == ()                                 # Storefront tracks transactions, not deals
    assert seeds["contact"]                                    # contact fields seeded
    assert industry_lingo.shop_seed_for("bakery")["lead"] == 1  # bakery clears fast


# ── plan / module entitlement ────────────────────────────────────────────────
def test_storefront_is_a_managed_module_via_industry_meta():
    assert "storefront" in plans.MANAGED_MODULES
    assert plans.INDUSTRY_MODULE["farm_shop"] == "storefront"
    # a farm-shop tenant on the +industry tier gets scheduling + storefront (its OWN module)
    assert plans.modules_for("professional", "farm_shop") == {"scheduling", "crm", "storefront"}
    assert plans.modules_for("starter", "farm_shop") == {"scheduling", "crm"}   # Core = base only


def test_managed_modules_match_operator_switchboard_with_storefront():
    from app.admin import TOGGLABLE_MODULES
    assert set(plans.MANAGED_MODULES) == TOGGLABLE_MODULES     # the invariant Phase 1 must preserve


def test_apply_plan_modules_farm_shop_activates_storefront_no_keyerror(monkeypatch):
    states = {}
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"industry": "farm_shop"})
    monkeypatch.setattr(db, "set_module_active", lambda t, m, a: states.__setitem__(m, a))
    monkeypatch.setattr(db, "seed_fleet_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_tms_defaults", lambda t: None)
    monkeypatch.setattr(db, "seed_fsm_defaults", lambda t: None)
    got = db.apply_plan_modules("T", "professional")
    assert got == {"scheduling", "crm", "storefront"}
    assert states["storefront"] is True and states["scheduling"] is True
    assert states["tms"] is False and states["fsm"] is False   # never another vertical's module


def test_storefront_tier_parked_but_module_meta_survives():
    # Two-tier collapse (2026-07-24): the OS tier is off the public catalog;
    # module metadata stays for founding partners / future un-parking.
    assert not any(r["key"] == "professional" for r in plans.billing_plans("farm_shop"))
    meta = plans.industry_module_meta("farm_shop")
    assert meta["name"] == "Storefront OS"
    assert "Reservations & collections" in meta["features"]


# ── the module hub renders Storefront OS ──────────────────────────────────────
@pytest.fixture
def hub_client(monkeypatch):
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"storefront"})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_module_hub_renders_storefront_os(hub_client):
    html = hub_client.get("/app/modules").text
    assert "Storefront OS" in html
    assert "/app/shop" in html                                # its card links to the surface


# ── the /app/shop daily surface renders the panels ───────────────────────────
@pytest.fixture
def shop_client(monkeypatch):
    import app.dashboard as dashboard
    import app.shop_routes as sr
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"storefront"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(sr.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), sr
    app.dependency_overrides.clear()


def test_shop_surface_renders_three_panels(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "shop_today_collections", lambda *a, **k: [
        {"id": "tx1", "status": "reserved", "ready_date": "2026-07-05", "total_amount": 7.0,
         "shop_transaction_items": [{"qty": 2, "product_name": "Sourdough"}]},
    ])
    monkeypatch.setattr(sr.db, "shop_list_products", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "unit": "each", "unit_price": 3.5,
         "qty_on_hand": 2, "qty_reserved": 0, "reorder_threshold": 5, "best_before_date": None},
    ])
    monkeypatch.setattr(sr.db, "shop_products_expiring", lambda *a, **k: [])   # panel 2 feed
    r = client.get("/app/shop")
    assert r.status_code == 200
    assert "Today's collections" in r.text                    # panel 1
    assert "Best-before specials" in r.text                   # panel 2 (placeholder)
    assert "Stock levels" in r.text                           # panel 3
    assert "Sourdough" in r.text
    assert "Reorder" in r.text                                # 2 on hand <= threshold 5 → amber flag


def test_shop_reserve_out_of_stock_shows_message(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: {"id": "p1", "name": "Sourdough"})
    monkeypatch.setattr(sr.db, "crm_get_or_create_contact", lambda *a, **k: {"id": "c1"})
    # the atomic RPC says out_of_stock (None) → the route surfaces msg=oos
    monkeypatch.setattr(sr.db, "shop_hold_stock_if_available", lambda *a, **k: None)
    r = client.post("/app/shop/transactions",
                    data={"product_id": "p1", "qty": "99", "reserve": "1",
                          "customer_email": "dave@x.co"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=oos" in r.headers["location"]


def test_shop_reserve_success_redirects_reserved(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: {"id": "p1", "name": "Sourdough"})
    monkeypatch.setattr(sr.db, "crm_get_or_create_contact", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(sr.db, "shop_hold_stock_if_available", lambda *a, **k: {"id": "tx1"})
    r = client.post("/app/shop/transactions",
                    data={"product_id": "p1", "qty": "2", "reserve": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=reserved" in r.headers["location"]


def test_shop_gate_404s_when_module_off(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "module_active", lambda *a, **k: False)
    assert client.get("/app/shop").status_code == 404          # _require_storefront gate


# ═══ Phase 2 — drafter integration (docs/os/storefront.md §3, §6, §7, §13) ═══
import asyncio

from app import agent, pipeline, storefront
from app.jobs import ready_reminders, release_stale_reservations


# ── check_stock returns real availability (available = on_hand - reserved) ────
def test_check_stock_returns_real_availability(monkeypatch):
    monkeypatch.setattr(db, "shop_get_product_by_name", lambda t, n, **k: {
        "id": "p1", "name": "Sourdough", "unit": "loaf",
        "qty_on_hand": 10, "qty_reserved": 3, "unit_price": 3.5})
    execute, state = storefront.make_storefront_executor("t1", contact_id="c1")
    out = execute("check_stock", {"product_name": "sourdough", "qty": 4})
    assert out["found"] is True
    assert out["available"] == 7.0          # 10 on hand - 3 reserved
    assert out["unit_price"] == 3.5
    assert out["enough"] is True            # 7 available >= 4 requested


def test_check_stock_not_in_catalogue_is_found_false(monkeypatch):
    monkeypatch.setattr(db, "shop_get_product_by_name", lambda *a, **k: None)
    monkeypatch.setattr(db, "shop_list_products", lambda *a, **k: [])
    execute, _ = storefront.make_storefront_executor("t1")
    out = execute("check_stock", {"product_name": "quinoa"})
    assert out["found"] is False and "colleague" in out["message"]  # never invents a price


# ── reserve_stock calls the atomic RPC + surfaces out-of-stock as a no-hold ───
def test_reserve_stock_holds_via_rpc(monkeypatch):
    monkeypatch.setattr(db, "shop_get_product_by_name", lambda t, n, **k: {
        "id": "p1", "name": "Sourdough", "qty_on_hand": 5, "qty_reserved": 0})
    calls = {}

    def fake_hold(tid, pid, qty, **k):
        calls["args"] = (tid, pid, qty, k)
        return {"id": "tx1", "status": "reserved"}
    monkeypatch.setattr(db, "shop_hold_stock_if_available", fake_hold)

    execute, state = storefront.make_storefront_executor("t1", contact_id="c1",
                                                         thread_id="th-1")
    out = execute("reserve_stock", {"product_name": "Sourdough", "qty": 2,
                                    "ready_date": "2026-07-10"})
    assert out["held"] is True and out["transaction_id"] == "tx1"
    # the ATOMIC RPC was called with the resolved product + contact + ready date
    tid, pid, qty, k = calls["args"]
    assert (tid, pid, qty) == ("t1", "p1", 2.0)
    assert k["contact_id"] == "c1" and k["ready_date"] == "2026-07-10"
    assert state["reservations"] == [{"id": "tx1", "status": "reserved"}]  # linked for the pipeline


def test_reserve_stock_out_of_stock_is_no_hold(monkeypatch):
    monkeypatch.setattr(db, "shop_get_product_by_name", lambda t, n, **k: {
        "id": "p1", "name": "Sourdough", "qty_on_hand": 1, "qty_reserved": 1})
    # the atomic RPC returns None when not enough is available (the clean signal)
    monkeypatch.setattr(db, "shop_hold_stock_if_available", lambda *a, **k: None)
    execute, state = storefront.make_storefront_executor("t1")
    out = execute("reserve_stock", {"product_name": "Sourdough", "qty": 99})
    assert out["held"] is False                 # NEVER a fabricated hold
    assert not state["reservations"]            # nothing linked
    assert "reserved" in out["message"].lower() # tells the model not to claim a hold


def test_reserve_stock_one_per_email(monkeypatch):
    monkeypatch.setattr(db, "shop_get_product_by_name", lambda t, n, **k: {
        "id": "p1", "name": "Sourdough", "qty_on_hand": 9, "qty_reserved": 0})
    monkeypatch.setattr(db, "shop_hold_stock_if_available",
                        lambda *a, **k: {"id": "tx1", "status": "reserved"})
    execute, state = storefront.make_storefront_executor("t1")
    execute("reserve_stock", {"product_name": "Sourdough", "qty": 1})
    second = execute("reserve_stock", {"product_name": "Sourdough", "qty": 1})
    assert second["error"] == "already_reserved"   # enforced in code, not just the prompt
    assert len(state["reservations"]) == 1


# ── the tools + context are injected for a storefront tenant, NOT otherwise ───
class _PipelineEnv:
    """Drives pipeline.draft_for_email with everything stubbed, capturing the
    draft_reply kwargs so we can assert the storefront injection (and its absence)."""

    def __init__(self, monkeypatch, storefront_on):
        self.draft_kwargs = {}
        mods = {"storefront"} if storefront_on else set()

        async def fake_draft(cfg, email, **kw):
            self.draft_kwargs = kw
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_retrieve(*a, **k):
            return []
        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"mailbox": "me@biz.co"})
        monkeypatch.setattr(pipeline.db, "effective_config", lambda *a, **k: {})
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {})
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set(mods))
        monkeypatch.setattr(pipeline.db, "module_active",
                            lambda t, m, **k: m in mods)
        monkeypatch.setattr(pipeline.db, "crm_get_or_create_contact",
                            lambda *a, **k: {"id": "c1"})
        monkeypatch.setattr(pipeline.db, "shop_list_products", lambda *a, **k: [
            {"id": "p1", "name": "Sourdough"}])
        monkeypatch.setattr(pipeline.db, "shop_storefront_context",
                            lambda *a, **k: "In stock now: Sourdough — 5 available")
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                            lambda *a, **k: {"contact_id": None, "company_id": None})
        monkeypatch.setattr(pipeline.db, "insert_email_log", lambda *a, **k: ("log-1", True))
        monkeypatch.setattr(pipeline.db, "log_agent_event", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.trial, "gate_allows", lambda *a, **k: True)


_SHOP_EMAIL = {"from_address": "dave@x.co", "subject": "Sourdough?",
               "body": "Do you have any Sourdough left?", "thread_id": "th-1"}


def test_storefront_tenant_gets_tools_and_context(monkeypatch):
    env = _PipelineEnv(monkeypatch, storefront_on=True)
    asyncio.run(pipeline.draft_for_email("t1", _SHOP_EMAIL, source_message_id="m1"))
    assert env.draft_kwargs["tools"] == list(storefront.STOREFRONT_TOOLS)
    assert env.draft_kwargs["storefront"] is True
    assert "Sourdough" in env.draft_kwargs["storefront_context"]
    assert env.draft_kwargs["tool_executor"] is not None


def test_non_storefront_tenant_has_no_tools_or_context(monkeypatch):
    env = _PipelineEnv(monkeypatch, storefront_on=False)
    asyncio.run(pipeline.draft_for_email("t1", _SHOP_EMAIL, source_message_id="m2"))
    assert env.draft_kwargs["tools"] is None            # no tools injected
    assert env.draft_kwargs["storefront"] is False
    assert env.draft_kwargs["storefront_context"] == ""  # byte-identical: no context


# ── the <storefront> prompt section (and it suppresses the no-booking block) ──
def test_build_system_prompt_storefront_section_and_suppresses_no_booking():
    cfg = {"business_brief": "A farm shop."}
    p = agent.build_system_prompt(cfg, storefront=True,
                                  storefront_context="In stock now: Sourdough — 5 available")
    assert "<storefront>" in p and "reserve_stock" in p and "check_stock" in p
    assert "<shop_context>" in p and "Sourdough" in p
    # the "no booking diary" block must NOT appear — the <storefront> block owns reserve/collect
    assert "<booking>" not in p


def test_build_system_prompt_no_storefront_is_unchanged():
    """A non-storefront tenant's prompt carries neither the section nor the context —
    byte-identical to before (the injection is fully conditional)."""
    cfg = {"business_brief": "A shop."}
    p = agent.build_system_prompt(cfg)                      # storefront defaults False
    assert "<storefront>" not in p and "<shop_context>" not in p
    assert "reserve_stock" not in p


# ── build_crm_context is byte-identical whether or not storefront is on ───────
def test_build_crm_context_untouched_by_storefront():
    from app import crm_intel
    contact = {"id": "c1", "name": "Dave", "email": "dave@x.co", "custom_data": {}}
    # build_crm_context never reads storefront state — the shop context rides a SEPARATE
    # seam (storefront_context), so the CRM block is identical for a shop and a non-shop.
    ctx = crm_intel.build_crm_context(contact, [], [], [])
    assert "stock" not in ctx.lower() and "reserv" not in ctx.lower()


# ── ready_reminders.run() drafts, flips reserved→ready, dedupes ───────────────
def test_ready_reminders_drafts_and_flips(monkeypatch):
    monkeypatch.setattr(ready_reminders.db, "tenants_with_modules", lambda *a, **k: ["t1"])
    monkeypatch.setattr(ready_reminders.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(ready_reminders.db, "shop_reservations_ready_today", lambda *a, **k: [
        {"id": "tx1", "thread_id": "th-1", "ready_date": "2026-07-05",
         "crm_contacts": {"email": "dave@x.co"},
         "shop_transaction_items": [{"qty": 2, "product_name": "Sourdough"}]},
    ])
    flips, logs = [], []
    monkeypatch.setattr(ready_reminders.db, "shop_set_transaction_status",
                        lambda tx, t, s, **k: flips.append((tx, s)))
    monkeypatch.setattr(ready_reminders.db, "insert_email_log",
                        lambda *a, **k: logs.append(k) or ("log-1", True))
    monkeypatch.setattr(ready_reminders.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ready_reminders.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(cfg, purpose, facts, **k):
        return agent.DraftResult(text="Ready to collect!", usage={}, model="m")
    monkeypatch.setattr(ready_reminders.agent, "compose_outbound", fake_compose)

    out = asyncio.run(ready_reminders.run())
    assert out["drafted"] == 1 and out["sent"] == 0
    assert ("tx1", "ready") in flips             # reserved→ready (the §3 transition + dedupe)
    assert logs[0]["kind"] == "ready"            # queued as a pending 'ready' draft


def test_ready_reminders_skips_when_no_contact(monkeypatch):
    monkeypatch.setattr(ready_reminders.db, "tenants_with_modules", lambda *a, **k: ["t1"])
    monkeypatch.setattr(ready_reminders.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(ready_reminders.db, "shop_reservations_ready_today", lambda *a, **k: [
        {"id": "tx1", "crm_contacts": {}, "shop_transaction_items": []},
    ])
    flips = []
    monkeypatch.setattr(ready_reminders.db, "shop_set_transaction_status",
                        lambda tx, t, s, **k: flips.append((tx, s)))
    out = asyncio.run(ready_reminders.run())
    # no email to send, but the order still advances to 'ready' so the surface is right
    assert out == {"date": out["date"], "drafted": 0, "sent": 0}
    assert ("tx1", "ready") in flips


# ── release_stale_reservations.run() releases over-age holds + drafts nudge ────
def test_release_stale_reservations_releases_and_drafts(monkeypatch):
    monkeypatch.setattr(release_stale_reservations.db, "tenants_with_modules",
                        lambda *a, **k: ["t1"])
    monkeypatch.setattr(release_stale_reservations.db, "effective_config",
                        lambda *a, **k: {"reservation_hold_days": 3, "auto_send": False})
    monkeypatch.setattr(release_stale_reservations.db, "shop_reservations_stale",
                        lambda *a, **k: [
        {"id": "tx1", "thread_id": "th-1", "crm_contacts": {"email": "dave@x.co"},
         "shop_transaction_items": [{"qty": 2, "product_name": "Sourdough"}]},
    ])
    released, logs = [], []
    monkeypatch.setattr(release_stale_reservations.db, "shop_release_reservation",
                        lambda tx, t, **k: released.append(tx))
    monkeypatch.setattr(release_stale_reservations.db, "insert_email_log",
                        lambda *a, **k: logs.append(k) or ("log-1", True))
    monkeypatch.setattr(release_stale_reservations.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(release_stale_reservations.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(cfg, purpose, facts, **k):
        return agent.DraftResult(text="Still want these?", usage={}, model="m")
    monkeypatch.setattr(release_stale_reservations.agent, "compose_outbound", fake_compose)

    out = asyncio.run(release_stale_reservations.run())
    assert out["released"] == 1 and out["drafted"] == 1
    assert released == ["tx1"]                     # the held stock went back to the shelf
    assert logs[0]["kind"] == "reminder"


def test_shop_release_reservation_decrements_and_moves_stock(monkeypatch):
    """The release helper: qty_reserved decremented, a 'release' movement written, status
    cancelled — the pure db mechanics the sweep and the cancel route both use."""
    tx = {"id": "tx1", "shop_transaction_items": [{"product_id": "p1", "qty": 2}]}
    monkeypatch.setattr(db, "shop_get_transaction", lambda *a, **k: tx)
    monkeypatch.setattr(db, "shop_get_product",
                        lambda *a, **k: {"id": "p1", "qty_reserved": 5})
    moved, statuses = [], []
    monkeypatch.setattr(db, "shop_record_movement",
                        lambda t, p, d, r, **k: moved.append((p, d, r)))
    monkeypatch.setattr(db, "shop_set_transaction_status",
                        lambda tx_, t, s, **k: statuses.append(s) or {"id": tx_, "status": s})
    upd = _FakeClient([{"id": "p1", "tenant_id": "t1", "qty_reserved": 5}])
    db.shop_release_reservation("tx1", "t1", client=upd)
    # qty_reserved 5 - 2 = 3 back on the shelf
    assert upd.updates[-1]["qty_reserved"] == 3.0
    assert moved == [("p1", 2.0, "release")]       # a +2 'release' movement per line
    assert statuses == ["cancelled"]


# ── the reserve / complete / cancel JSON routes (§7) ──────────────────────────
@pytest.fixture
def shop_api_client(monkeypatch):
    import app.dashboard as dashboard
    import app.shop_routes as sr
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(sr.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), sr
    app.dependency_overrides.clear()


def test_api_reserve_uses_atomic_rpc(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_transaction", lambda *a, **k: {"id": "tx1", "contact_id": "c1"})
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: {"id": "p1", "name": "Sourdough"})
    seen = {}
    monkeypatch.setattr(sr.db, "shop_hold_stock_if_available",
                        lambda t, p, q, **k: seen.update({"tx": k.get("transaction_id")}) or {"id": "tx1"})
    r = client.post("/api/shop/transactions/tx1/reserve",
                    data={"product_id": "p1", "qty": "2"})
    assert r.status_code == 200 and r.json()["status"] == "reserved"
    assert seen["tx"] == "tx1"                     # reserved against the existing transaction


def test_api_reserve_out_of_stock_409(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_transaction", lambda *a, **k: {"id": "tx1"})
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: {"id": "p1"})
    monkeypatch.setattr(sr.db, "shop_hold_stock_if_available", lambda *a, **k: None)
    r = client.post("/api/shop/transactions/tx1/reserve", data={"product_id": "p1", "qty": "99"})
    assert r.status_code == 409                    # out_of_stock, never a fabricated hold


def test_api_complete_drafts_receipt(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    tx = {"id": "tx1", "contact_id": "c1", "thread_id": "th-1",
          "crm_contacts": {"email": "dave@x.co"},
          "shop_transaction_items": [{"qty": 1, "product_name": "Sourdough"}]}
    monkeypatch.setattr(sr.db, "shop_get_transaction", lambda *a, **k: tx)
    statuses, logs = [], []
    monkeypatch.setattr(sr.db, "shop_set_transaction_status",
                        lambda tx_, t, s, **k: statuses.append(s))
    monkeypatch.setattr(sr.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(sr.db, "insert_email_log", lambda *a, **k: logs.append(k) or ("log-1", True))
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(cfg, purpose, facts, **k):
        return agent.DraftResult(text="Thanks!", usage={}, model="m")
    monkeypatch.setattr(sr.agent, "compose_outbound", fake_compose)

    r = client.post("/api/shop/transactions/tx1/complete")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert statuses == ["completed"]               # stamps completed
    assert r.json()["draft_id"] == "log-1"         # receipt drafted


def test_api_cancel_releases_and_drafts_decline(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    tx = {"id": "tx1", "contact_id": "c1", "crm_contacts": {"email": "dave@x.co"},
          "shop_transaction_items": [{"qty": 1, "product_name": "Sourdough"}]}
    monkeypatch.setattr(sr.db, "shop_get_transaction", lambda *a, **k: tx)
    released = []
    monkeypatch.setattr(sr.db, "shop_release_reservation",
                        lambda tx_, t, **k: released.append(tx_))
    monkeypatch.setattr(sr.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(sr.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(cfg, purpose, facts, **k):
        return agent.DraftResult(text="Sorry!", usage={}, model="m")
    monkeypatch.setattr(sr.agent, "compose_outbound", fake_compose)

    r = client.post("/api/shop/transactions/tx1/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert released == ["tx1"]                      # stock returned via the release helper


# ═══ Phase 3 — OCR-inferred inventory (docs/os/storefront.md §12.1, §13) ══════
import base64 as _b64
import json

from app import shop_ocr


class _FakeVisionMsg:
    """A stub Anthropic vision response — .content[0].text + .usage + .model."""
    def __init__(self, text, model="claude-sonnet-vision"):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(input_tokens=100, output_tokens=50,
                                     cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.model = model


def _stub_vision(monkeypatch, text=None, raises=False):
    """Patch shop_ocr.client() so extract_lines never touches the real API."""
    async def fake_create(**kwargs):
        if raises:
            raise RuntimeError("model down")
        _stub_vision.kwargs = kwargs
        return _FakeVisionMsg(text)
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(shop_ocr, "client", lambda: fake_client)


# ── band_lines bands at 0.75 / 0.5 (reusing crm_fields.gate) ──────────────────
def test_band_lines_bands_at_gate_thresholds():
    banded = shop_ocr.band_lines([
        {"name": "Maris Piper", "qty": 12, "confidence": 0.9},   # ≥0.75 → apply
        {"name": "Eggs",        "qty": 6,  "confidence": 0.6},   # 0.5–0.75 → review
        {"name": "Blur",        "qty": 1,  "confidence": 0.3},   # <0.5 → skip
        {"name": "Edge",        "qty": 1,  "confidence": 0.75},  # exactly 0.75 → apply
        {"name": "Edge2",       "qty": 1,  "confidence": 0.5},   # exactly 0.5 → review
    ])
    assert [b["band"] for b in banded] == ["apply", "review", "skip", "apply", "review"]
    # only apply-band lines are pre-checked (the trust bridge — a low line is never auto-applied)
    assert [b["checked"] for b in banded] == [True, False, False, True, False]


# ── extract_lines parses the structured vision output + meters ────────────────
def test_extract_lines_parses_and_meters(monkeypatch):
    payload = {"lines": [
        {"name": "Maris Piper", "qty": 12, "unit": "crate", "unit_price": 8.5,
         "best_before": "2026-07-20", "confidence": 0.92},
        {"name": "Eggs", "qty": 6, "unit": "box", "unit_price": None,
         "best_before": None, "confidence": 0.6},
        {"name": "  ", "qty": 3, "confidence": 0.9},        # blank name → dropped
        {"name": "Bad qty", "qty": "abc", "confidence": 0.9},  # non-numeric qty → dropped
    ], "document_note": "Fresh Farms delivery"}
    _stub_vision(monkeypatch, text=json.dumps(payload))
    metered = {}
    monkeypatch.setattr(shop_ocr.usage, "record_anthropic",
                        lambda t, m, u, **k: metered.update({"tenant": t, "op": k.get("operation")}))

    out = asyncio.run(shop_ocr.extract_lines("aW1n", "delivery_note", tenant_id="t1"))

    assert [l["name"] for l in out["lines"]] == ["Maris Piper", "Eggs"]  # malformed dropped
    assert out["lines"][0]["band"] == "apply" and out["lines"][0]["checked"] is True
    assert out["lines"][1]["band"] == "review" and out["lines"][1]["checked"] is False
    assert out["document_note"] == "Fresh Farms delivery"
    # metered as ocr_stock_in
    assert metered == {"tenant": "t1", "op": "ocr_stock_in"}
    # the call carried an image content block + the Sonnet booking model
    from app.config import settings as _s
    assert _stub_vision.kwargs["model"] == _s.anthropic_booking_model
    blocks = _stub_vision.kwargs["messages"][0]["content"]
    img = next(b for b in blocks if b["type"] == "image")
    assert img["source"]["type"] == "base64" and img["source"]["data"] == "aW1n"


def test_extract_lines_uses_the_verbatim_prompt(monkeypatch):
    _stub_vision(monkeypatch, text='{"lines": []}')
    asyncio.run(shop_ocr.extract_lines("aW1n", "invoice", tenant_id="t1"))
    system = _stub_vision.kwargs["system"]
    system = system if isinstance(system, str) else "".join(b["text"] for b in system)
    assert "Read ONLY what is on the document" in system      # the never-invent rule
    assert "LOW confidence" in system                         # blurry → low, not a guess
    assert "invoice" in system                                # the kind is interpolated


# ── extract_lines never raises on a malformed / failed call ───────────────────
def test_extract_lines_returns_empty_on_api_error(monkeypatch):
    _stub_vision(monkeypatch, raises=True)
    out = asyncio.run(shop_ocr.extract_lines("aW1n", tenant_id="t1"))
    assert out == {"lines": [], "document_note": ""}          # graceful — no crash


def test_extract_lines_returns_empty_on_non_json(monkeypatch):
    _stub_vision(monkeypatch, text="sorry, I can't read this")
    out = asyncio.run(shop_ocr.extract_lines("aW1n", tenant_id="t1"))
    assert out == {"lines": [], "document_note": ""}          # bad JSON → empty, never raises


# ── the OCR db layer (job create / set-extracted / mark-applied) ──────────────
def test_shop_create_ocr_job_inserts_pending():
    client = _FakeClient([])
    job = db.shop_create_ocr_job("t1", "delivery_note", None,
                                 email_log_id="log-9", client=client)
    assert job["status"] == "pending" and job["kind"] == "delivery_note"
    assert client.inserts[0]["email_log_id"] == "log-9"       # attachment provenance carried


def test_shop_set_ocr_extracted_flips_status():
    client = _FakeClient([{"id": "job1", "tenant_id": "t1", "status": "pending"}])
    db.shop_set_ocr_extracted("job1", "t1", [{"name": "Eggs"}], client=client)
    assert client.updates[-1]["status"] == "extracted"
    assert client.updates[-1]["extracted"] == [{"name": "Eggs"}]


def test_shop_mark_ocr_applied_stamps():
    client = _FakeClient([{"id": "job1", "tenant_id": "t1", "status": "extracted"}])
    db.shop_mark_ocr_applied("job1", "t1", client=client)
    assert client.updates[-1]["status"] == "applied"
    assert client.updates[-1].get("applied_at")               # stamped


# ── POST /api/shop/ocr: creates a job, extracts, returns banded lines ─────────
def test_api_ocr_creates_job_and_returns_banded_lines(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_create_ocr_job",
                        lambda t, k, image_path=None, **kw: {"id": "job1"})
    set_calls = {}
    monkeypatch.setattr(sr.db, "shop_set_ocr_extracted",
                        lambda j, t, ex, **k: set_calls.update({"job": j, "lines": ex}))

    async def fake_extract(image_b64, kind="delivery_note", *, tenant_id=None):
        fake_extract.b64 = image_b64
        return {"lines": [
            {"name": "Maris Piper", "qty": 12, "unit": "crate", "unit_price": None,
             "best_before": None, "confidence": 0.9, "band": "apply", "checked": True},
            {"name": "Eggs", "qty": 6, "unit": "box", "unit_price": None,
             "best_before": None, "confidence": 0.6, "band": "review", "checked": False},
        ], "document_note": "note"}
    monkeypatch.setattr(sr.shop_ocr, "extract_lines", fake_extract)

    r = client.post("/api/shop/ocr",
                    files={"image": ("note.jpg", b"\xff\xd8fakejpeg", "image/jpeg")},
                    data={"kind": "delivery_note"})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "job1"
    assert [l["name"] for l in body["lines"]] == ["Maris Piper", "Eggs"]
    # a low-confidence line is NOT pre-checked
    assert body["lines"][0]["checked"] is True and body["lines"][1]["checked"] is False
    # the banded lines were persisted to the job
    assert set_calls["job"] == "job1" and len(set_calls["lines"]) == 2
    # the uploaded bytes were base64'd into the extraction
    assert fake_extract.b64 == _b64.b64encode(b"\xff\xd8fakejpeg").decode()


def test_api_ocr_rejects_empty_image(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    r = client.post("/api/shop/ocr",
                    files={"image": ("empty.jpg", b"", "image/jpeg")},
                    data={"kind": "delivery_note"})
    assert r.status_code == 400


# ── POST /api/shop/ocr/{id}/apply: checked lines → stock, marks applied ───────
def test_api_ocr_apply_stocks_checked_lines_only_and_marks_applied(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_ocr_job",
                        lambda j, t, **k: {"id": j, "status": "extracted"})
    applied = {}
    monkeypatch.setattr(sr.db, "shop_apply_stock_in",
                        lambda t, lines, **k: applied.update({"lines": lines, "kw": k})
                        or {"created": 1, "updated": 1, "movements": 2})
    marked = []
    monkeypatch.setattr(sr.db, "shop_mark_ocr_applied", lambda j, t, **k: marked.append(j))

    # two rows submitted; only row 0 is checked → only it becomes stock
    r = client.post("/api/shop/ocr/job1/apply", data={
        "line_checked_0": "on",
        "line_name_0": "Maris Piper", "line_qty_0": "12", "line_unit_0": "crate",
        "line_best_before_0": "2026-07-20", "line_unit_price_0": "8.5",
        # row 1 present but UNCHECKED (no line_checked_1) → skipped
        "line_name_1": "Eggs", "line_qty_1": "6", "line_unit_1": "box",
        "line_best_before_1": "", "line_unit_price_1": "",
    })
    assert r.status_code == 200 and r.json()["status"] == "applied"
    # only the checked line went to stock, sourced 'ocr' with the job as source_ref
    assert [l["name"] for l in applied["lines"]] == ["Maris Piper"]
    assert applied["kw"]["source"] == "ocr" and applied["kw"]["source_ref"] == "job1"
    assert applied["lines"][0]["best_before"] == "2026-07-20"
    assert marked == ["job1"]                                 # job flipped to applied


def test_api_ocr_apply_is_idempotent_on_already_applied(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_ocr_job",
                        lambda j, t, **k: {"id": j, "status": "applied"})
    stocked = []
    monkeypatch.setattr(sr.db, "shop_apply_stock_in",
                        lambda *a, **k: stocked.append(1) or {})
    monkeypatch.setattr(sr.db, "shop_mark_ocr_applied", lambda *a, **k: None)
    r = client.post("/api/shop/ocr/job1/apply", data={
        "line_checked_0": "on", "line_name_0": "Maris Piper", "line_qty_0": "12"})
    assert r.status_code == 200 and r.json().get("deduped") is True
    assert stocked == []                                      # no double stock-in on re-apply


def test_api_ocr_apply_404_on_unknown_job(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_ocr_job", lambda *a, **k: None)
    r = client.post("/api/shop/ocr/nope/apply", data={})
    assert r.status_code == 404


# ── the /app/shop/stock-in page renders the review scaffold ───────────────────
def test_stock_in_page_renders_review_table(shop_client):
    client, sr = shop_client
    r = client.get("/app/shop/stock-in")
    assert r.status_code == 200
    assert "Photograph delivery" in r.text                    # the wedge CTA
    assert "Review the lines" in r.text                       # the confidence-banded table
    assert "/api/shop/ocr" in r.text                          # posts to the extraction route


def test_stock_in_page_404s_when_module_off(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "module_active", lambda *a, **k: False)
    assert client.get("/app/shop/stock-in").status_code == 404


# ═══ Phase 4 — best-before drafter (docs/os/storefront.md §12.2, §13) ═════════
from datetime import date, timedelta

from app.jobs import send_best_before as bb


# ── a fake client covering the filter chain the Phase-4 db fns use ────────────
class _P4Query:
    """A tiny query chain supporting the operators shop_products_expiring /
    shop_past_buyers_of / shop_had_special_today touch (not_.is_, gt, gte, nested
    select via pre-baked rows). Filters are applied on execute()."""
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *a, **k): return self
    @property
    def not_(self): return self          # .not_.is_(col, "null") → drop null col
    def is_(self, col, _null):
        self._rows = [r for r in self._rows if r.get(col) is not None]
        return self
    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self
    def gt(self, col, val):
        self._rows = [r for r in self._rows if float(r.get(col) or 0) > float(val)]
        return self
    def gte(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col) or "") >= str(val)]
        return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self): return _FakeResult(self._rows)


class _P4Client:
    def __init__(self, tables):
        self._tables = tables            # {table_name: [rows]}

    def table(self, name):
        return _P4Query(self._tables.get(name, []))


# ── shop_products_expiring filters on the best-before window + qty>0 ──────────
def test_products_expiring_filters_on_window_and_qty():
    today = date.today()
    soon = (today + timedelta(days=1)).isoformat()      # inside a lead of 2
    far = (today + timedelta(days=30)).isoformat()      # outside a lead of 2
    rows = [
        {"id": "p1", "tenant_id": "t1", "name": "Sourdough", "qty_on_hand": 4, "best_before_date": soon,
         "sell_before_lead_days": 2},                    # in window → kept
        {"id": "p2", "tenant_id": "t1", "name": "Milk", "qty_on_hand": 0, "best_before_date": soon,
         "sell_before_lead_days": 2},                    # qty 0 → dropped by gt(qty,0)
        {"id": "p3", "tenant_id": "t1", "name": "Honey", "qty_on_hand": 9, "best_before_date": far,
         "sell_before_lead_days": 2},                    # far off → outside window
        {"id": "p4", "tenant_id": "t1", "name": "Salt", "qty_on_hand": 9, "best_before_date": None,
         "sell_before_lead_days": 2},                    # undated → never fires
    ]
    client = _P4Client({"shop_products": rows})
    got = db.shop_products_expiring("t1", client=client)
    assert [p["id"] for p in got] == ["p1"]              # only the in-window dated stock


def test_products_expiring_within_days_override():
    today = date.today()
    far = (today + timedelta(days=20)).isoformat()
    rows = [{"id": "p3", "tenant_id": "t1", "name": "Honey", "qty_on_hand": 9,
             "best_before_date": far, "sell_before_lead_days": 2}]
    client = _P4Client({"shop_products": rows})
    # its own 2-day lead excludes it; a 30-day override pulls it into the window
    assert db.shop_products_expiring("t1", client=_P4Client({"shop_products": rows})) == []
    got = db.shop_products_expiring("t1", within_days=30, client=client)
    assert [p["id"] for p in got] == ["p3"]


# ── shop_past_buyers_of: completed-only, excludes opt-out, requires email ─────
def _buyer_items(rows):
    """Wrap contact/tx facts into the shop_transaction_items → shop_transactions →
    crm_contacts embedded shape shop_past_buyers_of reads."""
    return [{"transaction_id": f"tx{i}", "tenant_id": "t1", "product_id": "p1",
             "shop_transactions": {"id": f"tx{i}", "status": r["status"],
                                   "contact_id": r["contact"].get("id"),
                                   "crm_contacts": r["contact"]}}
            for i, r in enumerate(rows)]


def test_past_buyers_completed_only_optout_and_email_gated():
    items = _buyer_items([
        {"status": "completed", "contact": {"id": "c1", "email": "dave@x.co", "do_not_sequence": False}},
        {"status": "completed", "contact": {"id": "c1", "email": "dave@x.co", "do_not_sequence": False}},  # dup → once
        {"status": "reserved",  "contact": {"id": "c2", "email": "sam@x.co", "do_not_sequence": False}},   # not completed
        {"status": "completed", "contact": {"id": "c3", "email": "opt@x.co", "do_not_sequence": True}},    # opted out
        {"status": "completed", "contact": {"id": "c4", "email": "",         "do_not_sequence": False}},   # no email
        {"status": "completed", "contact": {"id": "c5", "email": "jo@x.co",  "do_not_sequence": False}},
    ])
    client = _P4Client({"shop_transaction_items": items})
    got = db.shop_past_buyers_of("t1", "p1", client=client)
    ids = [b["id"] for b in got]
    assert ids == ["c1", "c5"]                          # completed + email + not opted-out, deduped
    assert "c2" not in ids and "c3" not in ids and "c4" not in ids   # the targeting can't leak a non-buyer


# ── shop_had_special_today: the per-contact/day cap ──────────────────────────
def test_had_special_today_true_when_special_logged_today():
    client = _P4Client({
        "crm_contacts": [{"id": "c1", "tenant_id": "t1", "email": "dave@x.co"}],
        "email_log": [{"id": "e1", "tenant_id": "t1", "kind": "special",
                       "from_address": "dave@x.co", "created_at": f"{date.today().isoformat()}T09:00:00+00:00"}],
    })
    assert db.shop_had_special_today("t1", "c1", date.today().isoformat(), client=client) is True


def test_had_special_today_false_when_none_and_no_email():
    # no special row today → False
    client = _P4Client({
        "crm_contacts": [{"id": "c1", "tenant_id": "t1", "email": "dave@x.co"}],
        "email_log": [],
    })
    assert db.shop_had_special_today("t1", "c1", date.today().isoformat(), client=client) is False
    # a contact with no email is trivially uncapped (nobody to email)
    client2 = _P4Client({"crm_contacts": [{"id": "c9", "tenant_id": "t1", "email": ""}]})
    assert db.shop_had_special_today("t1", "c9", date.today().isoformat(), client=client2) is False


# ── send_best_before.run(): one special per past buyer, cap + isolation ───────
def _bb_common(monkeypatch, *, auto=False):
    monkeypatch.setattr(bb.db, "tenants_with_modules", lambda *a, **k: ["t1"])
    monkeypatch.setattr(bb.db, "effective_config", lambda *a, **k: {"auto_send": auto})
    monkeypatch.setattr(bb.db, "shop_list_transactions", lambda *a, **k: [])   # no history noise
    monkeypatch.setattr(bb.db, "shop_had_special_today", lambda *a, **k: False)

    async def fake_compose(cfg, purpose, facts, **k):
        fake_compose.last = (purpose, facts)
        return agent.DraftResult(text="Going today!", usage={}, model="m")
    monkeypatch.setattr(bb.agent, "compose_outbound", fake_compose)
    return fake_compose


def test_best_before_drafts_one_special_per_past_buyer(monkeypatch):
    fake_compose = _bb_common(monkeypatch)
    monkeypatch.setattr(bb.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "unit": "loaf", "qty_on_hand": 6,
         "best_before_date": "2026-07-06", "sell_before_lead_days": 2}])
    monkeypatch.setattr(bb.db, "shop_past_buyers_of", lambda *a, **k: [
        {"id": "c1", "email": "dave@x.co"}, {"id": "c2", "email": "sam@x.co"}])
    logs = []
    monkeypatch.setattr(bb.db, "insert_email_log", lambda *a, **k: logs.append(k) or ("log-1", True))
    monkeypatch.setattr(bb.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(bb.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(bb.run())
    assert out["drafted"] == 2 and out["sent"] == 0     # one special per past buyer
    assert all(l["kind"] == "special" for l in logs)    # queued as 'special' (the day cap keys on it)
    # the verbatim §12.2 purpose + facts-only (the discount default folded in)
    purpose, facts = fake_compose.last
    assert "do NOT invent stock, prices or dates" in purpose.lower() or "do not invent" in purpose.lower()
    assert "20% off going-today stock" in facts


def test_best_before_per_contact_day_cap_across_two_products(monkeypatch):
    """A buyer of TWO expiring products gets ONE special this run (the in-run seen-set)."""
    _bb_common(monkeypatch)
    monkeypatch.setattr(bb.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "qty_on_hand": 6, "best_before_date": "2026-07-06"},
        {"id": "p2", "name": "Rye", "qty_on_hand": 4, "best_before_date": "2026-07-06"}])
    # the SAME contact bought both products
    monkeypatch.setattr(bb.db, "shop_past_buyers_of",
                        lambda t, pid, **k: [{"id": "c1", "email": "dave@x.co"}])
    n = {"logs": 0}
    monkeypatch.setattr(bb.db, "insert_email_log",
                        lambda *a, **k: (n.__setitem__("logs", n["logs"] + 1), ("log-1", True))[1])
    monkeypatch.setattr(bb.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(bb.usage, "record_anthropic", lambda *a, **k: None)

    out = asyncio.run(bb.run())
    assert out["drafted"] == 1 and n["logs"] == 1       # capped: one special, not two


def test_best_before_respects_had_special_today_cap(monkeypatch):
    """A buyer already nudged on an EARLIER run today is skipped (cross-run cap)."""
    _bb_common(monkeypatch)
    monkeypatch.setattr(bb.db, "shop_had_special_today", lambda *a, **k: True)   # already capped
    monkeypatch.setattr(bb.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "qty_on_hand": 6, "best_before_date": "2026-07-06"}])
    monkeypatch.setattr(bb.db, "shop_past_buyers_of",
                        lambda *a, **k: [{"id": "c1", "email": "dave@x.co"}])
    monkeypatch.setattr(bb.db, "insert_email_log",
                        lambda *a, **k: pytest.fail("must not draft when already capped today"))
    out = asyncio.run(bb.run())
    assert out == {"date": out["date"], "drafted": 0, "sent": 0}


def test_best_before_auto_sends_per_config(monkeypatch):
    _bb_common(monkeypatch, auto=True)
    monkeypatch.setattr(bb.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "qty_on_hand": 6, "best_before_date": "2026-07-06"}])
    monkeypatch.setattr(bb.db, "shop_past_buyers_of",
                        lambda *a, **k: [{"id": "c1", "email": "dave@x.co"}])
    monkeypatch.setattr(bb.db, "insert_email_log", lambda *a, **k: ("log-1", True))
    monkeypatch.setattr(bb.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(bb.usage, "record_anthropic", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(bb.db, "mark_email_log_sent", lambda *a, **k: None)

    async def fake_send(tid, to, subject, text, **k):
        sent.append(to)
    monkeypatch.setattr(bb.mailbox, "send_mail", fake_send)

    out = asyncio.run(bb.run())
    assert out["sent"] == 1 and out["drafted"] == 0     # auto-sent for a full-auto tenant
    assert sent == ["dave@x.co"]


def test_best_before_isolates_a_bad_draft(monkeypatch):
    """One bad compose must not stop the run — the second buyer still drafts."""
    _bb_common(monkeypatch)
    monkeypatch.setattr(bb.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "qty_on_hand": 6, "best_before_date": "2026-07-06"}])
    monkeypatch.setattr(bb.db, "shop_past_buyers_of", lambda *a, **k: [
        {"id": "c1", "email": "boom@x.co"}, {"id": "c2", "email": "ok@x.co"}])
    monkeypatch.setattr(bb.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(bb.usage, "record_anthropic", lambda *a, **k: None)

    def flaky_log(tid, frm, *a, **k):
        if frm == "boom@x.co":
            raise RuntimeError("compose blew up")
        return ("log-2", True)
    monkeypatch.setattr(bb.db, "insert_email_log", flaky_log)

    out = asyncio.run(bb.run())
    assert out["drafted"] == 1                          # c1 failed, c2 still drafted


# ── POST /api/shop/products/{id}/draft-specials (the daily-surface button) ────
def test_api_draft_specials_fires_for_one_product(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: {
        "id": "p1", "name": "Sourdough", "unit": "loaf", "qty_on_hand": 6,
        "best_before_date": "2026-07-06"})
    monkeypatch.setattr(sr.db, "effective_config", lambda *a, **k: {"auto_send": False})
    monkeypatch.setattr(sr.db, "shop_past_buyers_of", lambda *a, **k: [
        {"id": "c1", "email": "dave@x.co"}, {"id": "c2", "email": "sam@x.co"}])
    monkeypatch.setattr(sr.db, "shop_had_special_today", lambda *a, **k: False)
    monkeypatch.setattr(sr.db, "shop_list_transactions", lambda *a, **k: [])
    logs = []
    monkeypatch.setattr(sr.db, "insert_email_log", lambda *a, **k: logs.append(k) or ("log-1", True))
    monkeypatch.setattr(sr.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(sr.usage, "record_anthropic", lambda *a, **k: None)

    async def fake_compose(cfg, purpose, facts, **k):
        return agent.DraftResult(text="Going today!", usage={}, model="m")
    monkeypatch.setattr(sr.agent, "compose_outbound", fake_compose)

    r = client.post("/api/shop/products/p1/draft-specials")
    assert r.status_code == 200
    body = r.json()
    assert body["drafted"] == 2 and body["past_buyers"] == 2
    assert all(l["kind"] == "special" for l in logs)


def test_api_draft_specials_404_on_unknown_product(shop_api_client, monkeypatch):
    client, sr = shop_api_client
    monkeypatch.setattr(sr.db, "shop_get_product", lambda *a, **k: None)
    r = client.post("/api/shop/products/nope/draft-specials")
    assert r.status_code == 404


# ── the daily-surface panel 2 renders expiring products + past-buyer counts ───
def test_shop_surface_best_before_panel_lists_expiring_with_counts(shop_client, monkeypatch):
    client, sr = shop_client
    monkeypatch.setattr(sr.db, "shop_today_collections", lambda *a, **k: [])
    monkeypatch.setattr(sr.db, "shop_list_products", lambda *a, **k: [])
    monkeypatch.setattr(sr.db, "shop_products_expiring", lambda *a, **k: [
        {"id": "p1", "name": "Sourdough", "unit": "loaf", "qty_on_hand": 6,
         "best_before_date": "2026-07-06"}])
    monkeypatch.setattr(sr.db, "shop_past_buyers_of", lambda *a, **k: [
        {"id": "c1"}, {"id": "c2"}, {"id": "c3"}])
    r = client.get("/app/shop")
    assert r.status_code == 200
    assert "Best-before specials" in r.text
    assert "Sourdough" in r.text                        # the expiring product
    assert "3 past buyers" in r.text                    # the differentiator count
    assert "/api/shop/products/p1/draft-specials" in r.text   # the [Draft specials] action


# ── the storefront clearance sequence is seeded (§12.2) ──────────────────────
def test_storefront_clearance_sequence_template():
    from app import crm_sequences as seqmod
    seq = seqmod.default_sequences("storefront")[0]
    assert seq["name"] == "Gone today — clearance"
    assert len(seq["steps"]) == 3
    assert all(s["exit_on_reply"] for s in seq["steps"])   # engine exits on a reply


def test_crm_seed_storefront_sequences_delegates(monkeypatch):
    from app import crm_sequences as seqmod
    calls = {}
    monkeypatch.setattr(seqmod, "crm_seed_default_sequences",
                        lambda tid, vertical: calls.update({"tid": tid, "v": vertical}) or [])
    seqmod.crm_seed_storefront_sequences("t1")
    assert calls == {"tid": "t1", "v": "storefront"}       # seeds the clearance template
