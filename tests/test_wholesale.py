"""Wholesale OS — Phase W1/W2 (docs/os/wholesale.md §13): the order primitive,
the order book, and the deterministic credit gate.

Pins: order creation totals (qty×price over priced lines), the §3 status ladder
(side states resume at 'order'; complete is terminal), the §12b credit gate
arithmetic (no limit → never blocks; projection over limit → over=True; the
context block never invents figures), the order-book composition, and the
module gate (wholesale off → 404).

No network: db.wholesale_* and the CRM helpers are monkeypatched; the autouse
conftest guard fails loudly on any un-stubbed live call.

Run: python -m pytest tests/test_wholesale.py -q
"""
import pytest

from app import db, wholesale


# ── order creation totals ─────────────────────────────────────────────────────

def test_create_order_sums_priced_lines(monkeypatch):
    created = {}

    def fake_create_deal(tenant_id, fields):
        created.update(fields)
        return {"id": "deal-1", **fields}

    inserted = []
    monkeypatch.setattr(db, "crm_create_deal", fake_create_deal)
    monkeypatch.setattr(db, "wholesale_insert_line",
                        lambda tid, did, **kw: inserted.append(kw) or {"id": f"ln-{len(inserted)}", **kw})

    deal = db.wholesale_create_order(
        "t1", company_id="c1", contact_id=None, title="Tue bread",
        required_by="2026-07-14",
        lines=[
            {"description": "Sourdough", "quantity": 12, "unit_price": "2.10"},
            {"description": "Rye", "quantity": 6, "unit_price": 2.80},
            {"description": "Unpriced sample", "quantity": 2},          # no price → £0
            {"description": "   ", "quantity": 5, "unit_price": 9.99},  # blank desc → dropped
        ])

    assert created["is_order"] is True
    assert created["wholesale_status"] == "quote"
    assert created["value_estimate"] == pytest.approx(12 * 2.10 + 6 * 2.80)
    assert len(inserted) == 3            # blank-description line never lands
    assert deal["lines"][0]["line_order"] == 0


def test_order_total_ignores_garbage_lines():
    assert wholesale.order_total([
        {"quantity": 10, "unit_price": 1.5},
        {"quantity": "not-a-number", "unit_price": 3},
        {"quantity": 4, "unit_price": None},
    ]) == pytest.approx(15.0)


# ── the §3 status ladder ──────────────────────────────────────────────────────

@pytest.mark.parametrize("current,expected", [
    (None, "order"),                 # unset behaves as quote
    ("quote", "order"),
    ("order", "allocation"),
    ("fulfilment", "invoice"),
    ("payment", "complete"),
    ("complete", None),              # terminal
    ("held", "order"),               # side states resume at order
    ("on_hold_short", "order"),
    ("skipped", None),
    ("nonsense", None),
])
def test_status_ladder(current, expected):
    assert wholesale.next_status(current) == expected


# ── the §12b credit gate (arithmetic, never LLM judgment) ─────────────────────

def _exposure(limit, open_orders=400.0, unpaid=300.0):
    exposure = round(open_orders + unpaid, 2)
    return {"open_orders": open_orders, "unpaid_invoices": unpaid,
            "exposure": exposure, "limit": limit,
            "headroom": (round(limit - exposure, 2) if limit is not None else None),
            "over": (limit is not None and exposure > limit)}


def test_gate_no_limit_never_blocks(monkeypatch):
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(None))
    gate = wholesale.check_credit("t1", "c1", 10_000.0)
    assert gate["over"] is False and gate["headroom"] is None


def test_gate_projection_over_limit(monkeypatch):
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(1000.0))   # exposure 700
    gate = wholesale.check_credit("t1", "c1", 400.0)          # → 1100 > 1000
    assert gate["over"] is True
    assert gate["projected"] == pytest.approx(1100.0)
    assert gate["headroom"] == pytest.approx(300.0)


def test_gate_projection_within_limit(monkeypatch):
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(1000.0))
    gate = wholesale.check_credit("t1", "c1", 200.0)          # → 900 <= 1000
    assert gate["over"] is False


def test_credit_block_carries_only_gate_figures(monkeypatch):
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(1000.0))
    gate = wholesale.check_credit("t1", "c1", 400.0)
    block = wholesale.credit_gate_block(gate, 400.0)
    assert "HARD RULE" in block
    assert "£1000.00 limit" in block and "£1100.00" in block
    assert "OVER their limit by £100.00" in block
    assert "Never invent figures" in block


# ── order-book composition ────────────────────────────────────────────────────

def test_order_book_groups_by_status(monkeypatch):
    orders = [
        {"id": "d1", "wholesale_status": "order", "company_id": "c1",
         "value_estimate": 25, "title": "A"},
        {"id": "d2", "wholesale_status": "held", "company_id": "c1",
         "value_estimate": 60, "title": "B"},
        {"id": "d3", "wholesale_status": None, "company_id": "missing",
         "value_estimate": 0, "title": "C"},
    ]
    monkeypatch.setattr(db, "wholesale_list_orders", lambda *a, **k: [dict(o) for o in orders])
    monkeypatch.setattr(db, "wholesale_lines_for_deals",
                        lambda *a, **k: {"d1": [{"quantity": 10, "unit_price": 2.5}]})
    monkeypatch.setattr(db, "wholesale_order_companies",
                        lambda *a, **k: [{"id": "c1", "name": "Cornerstone", "custom_data": {}}])

    book = wholesale.order_book("t1")
    by_status = {c["status"]: c for c in book["columns"]}
    assert [o["id"] for o in by_status["order"]["orders"]] == ["d1"]
    assert by_status["order"]["orders"][0]["total"] == pytest.approx(25.0)  # lines win
    assert by_status["order"]["orders"][0]["buyer"] == "Cornerstone"
    assert [o["id"] for o in by_status["held"]["orders"]] == ["d2"]
    assert [o["id"] for o in by_status["quote"]["orders"]] == ["d3"]        # None → quote
    assert by_status["quote"]["orders"][0]["buyer"] == "—"                  # unknown company
    assert book["count"] == 3


def test_credit_risk_rows_worst_first(monkeypatch):
    comps = [
        {"id": "c1", "name": "Fine Foods", "custom_data": {"credit_limit": "2000"}},
        {"id": "c2", "name": "Overdrawn Deli", "custom_data": {"credit_limit": "£500"}},
        {"id": "c3", "name": "No Limit Ltd", "custom_data": {}},
    ]
    exposures = {
        "c1": _exposure(2000.0, open_orders=100, unpaid=0),   # headroom 1900
        "c2": _exposure(500.0, open_orders=400, unpaid=300),  # exposure 700 → over
    }
    monkeypatch.setattr(db, "wholesale_order_companies", lambda *a, **k: comps)
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda tid, cid, client=None: exposures[cid])

    rows = wholesale.credit_risk_rows("t1")
    assert [r["name"] for r in rows] == ["Overdrawn Deli", "Fine Foods"]  # worst headroom first
    assert rows[0]["over"] is True and rows[0]["stance"] == "will hold new orders"
    assert len(rows) == 2                                     # no-limit buyer excluded


# ── the module gate ───────────────────────────────────────────────────────────

def test_routes_404_when_module_off(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import require_onboarded, TenantCtx

    monkeypatch.setattr(db, "module_active", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/wholesale/orders")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(require_onboarded, None)


# ── W2: the inbound credit hook (docs/os/wholesale.md §6c) ────────────────────

def test_inbound_credit_context_over_buyer(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email",
                        lambda tid, addr: {"id": "ct1", "company_id": "c2"})
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(500.0, open_orders=400, unpaid=300))
    block = wholesale.inbound_credit_context("t1", "buyer@overdrawn.co.uk")
    assert "HARD RULE" in block
    assert "ALREADY OVER their limit by £200.00" in block
    assert "do NOT confirm or accept it" in block
    assert "part-order" not in block          # no headroom → no part-order offer


def test_inbound_credit_context_clean_buyer_is_empty(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email",
                        lambda tid, addr: {"id": "ct1", "company_id": "c1"})
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(2000.0, open_orders=100, unpaid=0))
    assert wholesale.inbound_credit_context("t1", "buyer@fine.co.uk") == ""


def test_inbound_credit_context_unknown_sender_is_empty(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email", lambda tid, addr: None)
    assert wholesale.inbound_credit_context("t1", "who@nowhere.com") == ""
    assert wholesale.inbound_credit_context("t1", "+447700900000") == ""  # sms identity
    assert wholesale.inbound_credit_context("t1", None) == ""


def test_known_order_block_offers_headroom_part_order(monkeypatch):
    monkeypatch.setattr(db, "wholesale_credit_exposure",
                        lambda *a, **k: _exposure(1000.0))   # exposure 700, headroom 300
    gate = wholesale.check_credit("t1", "c1", 400.0)
    block = wholesale.credit_gate_block(gate, 400.0)
    assert "part-order within the £300.00 of headroom" in block


# ── W3: standing-order cycle dates + the spawn (docs/os/wholesale.md §6a) ─────

from datetime import date

from app import wholesale_standing


@pytest.mark.parametrize("so,after,expected", [
    # weekly Tue+Fri, after Mon 2026-07-13 → Tue 2026-07-14
    ({"cadence": "weekly", "days_of_week": [1, 4]}, date(2026, 7, 13), date(2026, 7, 14)),
    # weekly Tue+Fri, after Tue → Fri same week (strictly after)
    ({"cadence": "weekly", "days_of_week": [1, 4]}, date(2026, 7, 14), date(2026, 7, 17)),
    # weekly Fri only, after Fri → next Fri
    ({"cadence": "weekly", "days_of_week": [4]}, date(2026, 7, 17), date(2026, 7, 24)),
    # fortnightly Tue, after Tue 14th → Tue 28th (skips a week)
    ({"cadence": "fortnightly", "days_of_week": [1]}, date(2026, 7, 14), date(2026, 7, 28)),
    # monthly day 31 clamps to short months (Aug after Jul → 31 Aug; Sep clamps to 30)
    ({"cadence": "monthly", "day_of_month": 31}, date(2026, 7, 15), date(2026, 8, 31)),
    ({"cadence": "monthly", "day_of_month": 31}, date(2026, 8, 31), date(2026, 9, 30)),
    # december rolls the year
    ({"cadence": "monthly", "day_of_month": 5}, date(2026, 12, 10), date(2027, 1, 5)),
    # weekly with no days configured → None (engine skips, never crashes)
    ({"cadence": "weekly", "days_of_week": []}, date(2026, 7, 13), None),
    ({"cadence": "someday"}, date(2026, 7, 13), None),
])
def test_next_cycle_date(so, after, expected):
    assert wholesale_standing.next_cycle_date(so, after) == expected


def _so(**over):
    base = {"id": "so1", "tenant_id": "t1", "company_id": "c1", "contact_id": "ct1",
            "name": "Tue book", "cadence": "weekly", "days_of_week": [1],
            "next_run_on": "2026-07-14", "auto_confirm": False,
            "line_template": [{"description": "Sourdough", "quantity": 12, "unit_price": 2.1}]}
    base.update(over)
    return base


class _Result:
    text = "Dear buyer, your order is confirmed."
    model = "haiku"
    usage = {}


def _patch_spawn(monkeypatch, *, gate_over):
    calls = {"created": [], "status": [], "advanced": [], "logged": [],
             "sent": [], "drafts": []}
    monkeypatch.setattr(db, "effective_config", lambda *a, **k: {"auto_send": True})
    monkeypatch.setattr(db, "wholesale_create_order",
                        lambda tid, **kw: calls["created"].append(kw) or
                        {"id": "d9", "value_estimate": 25.2, "required_by": kw.get("required_by"),
                         "title": kw.get("title"),
                         "lines": [{"description": "Sourdough", "quantity": 12, "unit_price": 2.1}]})
    monkeypatch.setattr(db, "wholesale_set_status",
                        lambda tid, did, st: calls["status"].append(st))
    monkeypatch.setattr(db, "wholesale_advance_standing_order",
                        lambda tid, soid, **kw: calls["advanced"].append(kw))
    monkeypatch.setattr(db, "wholesale_order_companies",
                        lambda tid, client=None: [{"id": "c1", "name": "Cornerstone",
                                                   "custom_data": {}}])
    monkeypatch.setattr(db, "crm_get_contact",
                        lambda cid, tid, client=None: {"email": "buyer@cornerstone.uk"})
    monkeypatch.setattr(wholesale, "check_credit",
                        lambda *a, **k: {"over": gate_over, "exposure": 700.0, "limit": 500.0,
                                         "unpaid_invoices": 300.0, "open_orders": 400.0,
                                         "headroom": -200.0, "projected": 700.0})
    async def fake_compose(cfg, purpose, facts, **kw):
        calls["drafts"].append(purpose)
        return _Result()
    monkeypatch.setattr(wholesale_standing.agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("log1", True))
    monkeypatch.setattr(wholesale_standing.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: calls["logged"].append(a))
    async def fake_send(tid, to, subject, body):
        calls["sent"].append(body)
    monkeypatch.setattr(wholesale_standing.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    return calls


@pytest.mark.asyncio
async def test_spawn_within_credit_drafts_by_default(monkeypatch):
    calls = _patch_spawn(monkeypatch, gate_over=False)
    outcome = await wholesale_standing._spawn_cycle(_so())
    assert outcome == "drafted"                       # auto_confirm off → owner queue
    assert calls["created"][0]["status"] == "order"
    assert calls["sent"] == []                        # nothing auto-sent
    assert calls["advanced"][0]["next_run_on"] == "2026-07-21"  # next Tuesday


@pytest.mark.asyncio
async def test_spawn_auto_confirm_sends_with_disclosure(monkeypatch):
    calls = _patch_spawn(monkeypatch, gate_over=False)
    outcome = await wholesale_standing._spawn_cycle(_so(auto_confirm=True))
    assert outcome == "confirmed"
    assert len(calls["sent"]) == 1
    assert "AI assistant" in calls["sent"][0]         # Art 50 footer rides the auto-send


@pytest.mark.asyncio
async def test_spawn_over_credit_holds_and_never_sends(monkeypatch):
    calls = _patch_spawn(monkeypatch, gate_over=True)
    outcome = await wholesale_standing._spawn_cycle(_so(auto_confirm=True))
    assert outcome == "held"
    assert calls["status"] == ["held"]                # the order deal is held
    assert calls["sent"] == []                        # a refusal NEVER auto-sends
    assert any("cannot release" in p or "credit" in p.lower() for p in calls["drafts"])
    assert calls["advanced"], "cursor still advances so the sweep never jams"


@pytest.mark.asyncio
async def test_spawn_empty_template_skips(monkeypatch):
    calls = _patch_spawn(monkeypatch, gate_over=False)
    outcome = await wholesale_standing._spawn_cycle(_so(line_template=[]))
    assert outcome == "skipped"
    assert calls["created"] == []                     # no empty order deals
    assert calls["advanced"], "cursor advances past the empty cycle"


# ── W4: the this-week plan + shortage resolution (§6b/§12c) ───────────────────

from app import wholesale_plan


def _plan_orders():
    return [
        {"id": "o1", "wholesale_status": "order", "required_by": "2026-07-16",
         "company_id": "c1", "title": "Wholesale Bakes Thu", "allocation_priority": None},
        {"id": "o2", "wholesale_status": "order", "required_by": "2026-07-16",
         "company_id": "c2", "title": "The Deli Thu", "allocation_priority": None},
        {"id": "o3", "wholesale_status": "held", "required_by": "2026-07-16",
         "company_id": "c1", "title": "Held order", "allocation_priority": None},
        {"id": "o4", "wholesale_status": "order", "required_by": "2026-07-17",
         "company_id": "c2", "title": "Fri order", "allocation_priority": None},
    ]


def _plan_lines():
    return {
        "o1": [{"description": "Loaves", "quantity": 180}],
        "o2": [{"description": "loaves", "quantity": 100}],     # case-insensitive merge
        "o3": [{"description": "Loaves", "quantity": 500}],     # held → excluded
        "o4": [{"description": "Loaves", "quantity": 50}],
    }


def test_week_plan_shortage_resolved_by_priority():
    plan = wholesale_plan.build_week_plan(
        _plan_orders(), _plan_lines(), capacity={"Loaves": 240},
        priority_by_company={"c1": 1, "c2": 2})
    thu = next(d for d in plan if d["date"] == "2026-07-16")
    assert len(thu["orders"]) == 2                      # held order excluded
    assert thu["shortages"][0]["ordered"] == pytest.approx(280)   # 180+100 merged
    assert thu["shortages"][0]["short_by"] == pytest.approx(40)
    # priority-1 buyer fills; the deli waits with the remainder
    assert thu["waitlist"] == [{"title": "The Deli Thu", "deal_id": "o2",
                                "ordered": 100.0, "allocatable": 60.0}]
    fri = next(d for d in plan if d["date"] == "2026-07-17")
    assert fri["shortages"] == []                       # 50 <= 240


def test_week_plan_no_capacity_no_flags():
    plan = wholesale_plan.build_week_plan(_plan_orders(), _plan_lines(), capacity=None)
    assert all(not d["shortages"] and not d["waitlist"] for d in plan)


def test_week_plan_order_priority_overrides_buyer_default():
    orders = _plan_orders()
    orders[1]["allocation_priority"] = 1                # the Deli overrides to prio 1
    plan = wholesale_plan.build_week_plan(
        orders, _plan_lines(), capacity={"loaves": 240},
        priority_by_company={"c1": 1, "c2": 2})
    thu = next(d for d in plan if d["date"] == "2026-07-16")
    # both prio 1 → stable fill order by input; first entry fills, second waits
    assert len(thu["waitlist"]) == 1
