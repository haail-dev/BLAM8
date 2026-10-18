# BLAM8 Wholesale OS — Build-Ready Implementation Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, W1–W5 (app/wholesale*.py; tests/test_wholesale.py), deployed and marked live. Live proof: none.

**Status:** Not built. Vocabulary seed exists (`src/app/industry_lingo.py`, hidden in the onboarding picker). This doc is the build plan.
**Author:** Julius Deane, w/ Claude (Dispatch synthesis)
**Last updated:** 2026-07-05
**Reads on:** `docs/BLAM8_operating_systems.md` §2/§4.4/§5/§6, `docs/architecture/CRM_design.md` (format + platform patterns, esp. §22 proposals+invoicing).

> **Reuse-first.** Wholesale OS is the OS that overlaps the CRM most: a **buyer is a `crm_companies` row**, an **order is a `crm_deals` row in disguise**, and the money loop is the CRM's own Phase 5.1 (§22) proposals/invoicing/payments direction. This spec builds the *thinnest possible* new layer on top of that platform — one primitive table, one credit-terms extension, standing-order automation on the existing scheduler, and a credit-limit gate spliced into the existing drafter. Everything else is config + reuse. §15 is the line-by-line reuse map; read it first if you only read one section.

---

## §0 Positioning

**Shape.** B2B distribution. The tenant supplies *other businesses* on repeat: the same buyers, the same lines, on regular cycles, on account terms. Unlike Field OS (you go to the customer) or Storefront OS (the customer comes to you), Wholesale OS is defined by the **standing relationship** — a bakery delivering to 30 cafés every Tuesday and Friday, a brewery to 40 pubs weekly, a packaging supplier restocking accounts monthly. The daily job is not *winning* an order, it's *fulfilling the recurring book* without running someone over their credit limit or double-committing stock.

**Primitive.** `order` — buyer + items + terms + fulfilment status. Modelled as a `crm_deals` extension (§1), not a fresh entity tree.

**Daily-rhythm surface.** Standing orders due to generate + credit-limit risk (who's near/over) + this-week fulfilment plan (what ships when, where the conflicts are).

**Killer feature.** Standing-order automation + a **credit-limit-aware drafter that refuses orders exceeding credit** + fulfilment planning that surfaces conflicts. See §12.

**Wedge.** Artisan producers running a wholesale side off a spreadsheet — bakers, brewers, cheesemongers, roasters, small-batch drinks. They have retail EPOS but nothing for the wholesale book; they run it in a spreadsheet + WhatsApp + memory. BLAM8 gives them proper wholesale operations without hiring an office manager. First "photo/forward the order email → order drafted, credit checked, allocation planned" moment converts them.

**Target industries (~80K UK SMBs).** Wholesalers, distributors, artisan producers with a wholesale side (bakeries, breweries, cheesemongers, roasters, distilleries), food-service suppliers, drinks wholesalers, packaging suppliers, cash-and-carry account trade.

**Build state.** 🌱 Vocabulary seed exists in `industry_lingo.LINGO["wholesale"]` and `CRM_SEEDS["wholesale"]` (credit limit, payment terms, delivery area already seeded); the OS is registered in `operating_systems.OPERATING_SYSTEMS` with `live: False`; `INDUSTRY_TO_OS["wholesale"] = "wholesale"`. Onboarding entry exists but `hidden: True`. Module not built.

---

## §1 The primitive — `order` vs CRM `deal`

An `order` is **a `crm_deals` row with a wholesale head + line items**. A wholesale order is a deal whose *subject is a fulfilment*, not a sales opportunity: it has a buyer (company), a value, a stage-like status, an activity feed — everything a deal already has — plus items, terms, a required-by date, and a fulfilment status a deal doesn't carry.

**Decision: reuse `crm_deals` as the order head; add `wholesale_order_lines` + `wholesale_standing_orders` as the only new tables.**

| Concept | Where it lives | Reuse or new |
| --- | --- | --- |
| Buyer | `crm_companies` (+ `custom_data` credit_limit / payment_terms) | **REUSE** |
| Buyer contact | `crm_contacts` | **REUSE** |
| Order head (buyer, value, status, feed) | `crm_deals` + additive wholesale columns | **REUSE + extend** |
| Order line items (sku, qty, unit_cost, unit_price) | `wholesale_order_lines` (new) | **NEW** — deals have no line-item table |
| Credit terms (limit, terms, allocation priority, MOV) | `crm_companies.custom_data` + tenant defaults | **REUSE** (custom fields) |
| Standing-order schedule | `wholesale_standing_orders` (new) | **NEW** — a recurrence rule + a line template |
| Allocation / fulfilment status | `crm_deals.wholesale_status` (new column) | **extend** |
| Invoice / payment | `invoices` / `payments` (CRM §22) | **REUSE** (§22 direction) |

**Why not a standalone `orders` table?** Because 90% of an order *is* a deal — the derivation-from-email engine (`src/app/crm.py ingest_email_log_row`), the activity feed, the pipeline board, the drafter's stage-awareness, promise tracking, reporting, and the §22 invoice loop all key off `crm_deals`. Forking a parallel `orders` tree would duplicate all of that and lose the self-filling property. Instead, an order **is** a deal, flagged `is_order = true`, carrying line items and a fulfilment status. This is the same design move the CRM doc makes for §22 (proposals/invoices hang off `deals`, they don't fork them).

**Why the two new tables are justified.**
- `wholesale_order_lines` — a deal has a single `value_estimate`; a wholesale order has *n* priced lines (12 sourdough @ £2.10, 6 rye @ £2.80). Line items are load-bearing for allocation, margin, and invoicing; they cannot live in one numeric column. No existing table holds per-order line items.
- `wholesale_standing_orders` — a recurrence rule ("every Tue+Fri", a line template, an auto-send policy) that *spawns* order deals on a schedule. This is genuinely new behaviour (no existing table recurs); it reuses the `crm_sequences` *scheduling pattern* (an engine on the 15-min/daily timer) but not its schema (a standing order emits an order + a draft, not a step in an outreach ladder).

Everything else — buyer identity, terms, status transitions, the money loop — is reuse.

---

## §2 Daily-rhythm surface

Home page `/app/wholesale` (the OS home; mirrors `/app/tms` / `/app/fsm`). Three stacked cards, each a live query — the wholesale analogue of the transport board.

**Card 1 — Standing orders due.** Standing orders whose next cycle falls in the next 7 days, grouped by ship date. "Tuesday: 14 cafés · 312 units · 4 need approval." One click generates the cycle's order deals (or they auto-generate overnight — §6). Surfaces the *skip* control (a café on holiday) and the *amend* control (this week's qty differs).

**Card 2 — Credit-limit risk.** Buyers whose `open order value + unpaid invoice value` is at or over their `credit_limit`, sorted worst-first. "Cornerstone Café — £1,240 outstanding on a £1,000 limit · 2 invoices overdue." Each row links to the buyer and shows the drafter's stance (new orders will be **held/refused** — §12). This is the card that stops a producer shipping to a buyer who's stopped paying.

**Card 3 — This-week fulfilment plan.** Every order due to ship this week laid out by day, with **conflict flags**: a line whose ordered qty across all buyers on a day exceeds what's allocatable (production capacity or on-hand stock), an allocation-priority clash (two priority-1 buyers, not enough to cover both), a delivery-area clash. "Thu: 280 loaves ordered, 240 baked → 40 short. Priority: Wholesale Bakes first, The Deli waitlisted." This is the fulfilment-conflict surfacing from the killer-feature brief.

All three read off `crm_deals` (order heads) + `wholesale_order_lines` + `wholesale_standing_orders` + `invoices` (for the credit maths). No new data collection — the same "reports are accurate because the data is already complete" property the CRM leans on (`CRM_design.md` §19.1).

---

## §3 Workflow state machine

`quote → order → allocation → fulfilment → invoice → payment → next-cycle order`

An order deal's `wholesale_status` walks this ladder (distinct from the CRM `stage_id`, which stays for the *relationship* pipeline — a new buyer is still Prospect→Won; once won, their orders cycle through the wholesale states). The drafter has a touchpoint at every transition — every one of these already routes through `compose_outbound` / `draft_reply`.

| State | Trigger | Drafter touchpoint |
| --- | --- | --- |
| `quote` | Buyer emails an enquiry / RFQ; or a new line is priced | `draft_reply` quotes trade price from the rate card / custom-field pricing, in the tenant's voice. Same path as a Field OS quote. |
| `order` | Buyer confirms, or a standing order fires | **Credit gate runs here (§12).** If within credit → order confirmed, `draft_reply`/`compose_outbound` sends the confirmation. If over → the drafter **refuses/holds** and drafts the credit conversation instead. |
| `allocation` | Order confirmed, stock/production assigned | If short (fulfilment conflict), drafter drafts the shortfall note ("we can do 40 of the 50 rye this week — hold the balance to Friday?"). |
| `fulfilment` | Picked/dispatched | `compose_outbound` sends the dispatch note / delivery confirmation. Reuses the Field OS dispatch pattern where a delivery is physical. |
| `invoice` | Fulfilled → invoice generated | **REUSE CRM §22** — invoice drafted from order lines (line items copy straight across), due date from the buyer's payment terms (net-30/60), sent via the drafter. |
| `payment` | Payment detected in inbox / webhook | **REUSE CRM §22** — LLM payment detection matches remittance to invoice; `payment` recorded; credit headroom frees up automatically. Overdue → drafter drafts the chaser using order context. |
| `next-cycle order` | Standing-order schedule ticks | The standing-order engine spawns the next cycle's order deal → back to `order`. The loop closes without the tenant touching it. |

Terminal / side states: `held` (credit gate blocked it — awaits owner release), `skipped` (this cycle cancelled), `on_hold_short` (partially allocated, balance carried).

---

## §4 Schema

Additive only. RLS on every new table via the house `auth_tenant_ids()` predicate. Indexes on the hot read paths (tenant + status/date). Idempotent psycopg2 migration (§13, mirrors `scripts/db/migrations/apply_crm_phase4b_migration.py`).

```sql
-- ── Order head: extend crm_deals in place (an order IS a deal) ───────────────
ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS is_order boolean NOT NULL DEFAULT false;
ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS wholesale_status text;
  -- quote | order | allocation | fulfilment | invoice | payment | held |
  -- skipped | on_hold_short | complete   (NULL for non-order deals)
ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS required_by date;         -- ship-by date
ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS standing_order_id uuid;   -- provenance
ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS allocation_priority integer; -- override buyer default
CREATE INDEX IF NOT EXISTS idx_crm_deals_wholesale
  ON crm_deals (tenant_id, wholesale_status, required_by) WHERE is_order;

-- ── Order line items (new — deals carry no line table) ──────────────────────
CREATE TABLE IF NOT EXISTS wholesale_order_lines (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  deal_id      uuid NOT NULL REFERENCES crm_deals(id) ON DELETE CASCADE,
  sku          text,                    -- free-text ok; producers rarely have a SKU scheme
  description  text NOT NULL,
  quantity     numeric NOT NULL DEFAULT 0,
  unit         text,                    -- ea | case | kg | dozen …
  unit_price   numeric,                 -- trade price charged
  unit_cost    numeric,                 -- for margin (optional)
  allocated_qty numeric,                -- filled at allocation; NULL = not yet allocated
  line_order   integer NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wholesale_lines_deal
  ON wholesale_order_lines (tenant_id, deal_id, line_order);

-- ── Standing orders (new — a recurrence rule + a line template) ─────────────
CREATE TABLE IF NOT EXISTS wholesale_standing_orders (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  company_id    uuid NOT NULL REFERENCES crm_companies(id) ON DELETE CASCADE,
  contact_id    uuid REFERENCES crm_contacts(id) ON DELETE SET NULL,
  name          text,                   -- "Cornerstone — Tue/Fri bread"
  cadence       text NOT NULL,          -- weekly | fortnightly | monthly | custom
  days_of_week  integer[] DEFAULT '{}', -- 0=Mon … 6=Sun for weekly/fortnightly
  day_of_month  integer,                -- for monthly
  lead_days     integer NOT NULL DEFAULT 1,  -- generate the order this many days ahead
  line_template jsonb NOT NULL DEFAULT '[]', -- [{description,sku,quantity,unit,unit_price}]
  auto_confirm  boolean NOT NULL DEFAULT false, -- skip the review queue (trusted account)
  status        text NOT NULL DEFAULT 'active',  -- active | paused | ended
  next_run_on   date,                   -- the engine's cursor
  last_run_on   date,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_standing_orders_due
  ON wholesale_standing_orders (tenant_id, status, next_run_on) WHERE status = 'active';

-- ── RLS (every table) ───────────────────────────────────────────────────────
ALTER TABLE wholesale_order_lines ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - wholesale_order_lines" ON wholesale_order_lines;
CREATE POLICY "tenant isolation - wholesale_order_lines" ON wholesale_order_lines
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE wholesale_standing_orders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - wholesale_standing_orders" ON wholesale_standing_orders;
CREATE POLICY "tenant isolation - wholesale_standing_orders" ON wholesale_standing_orders
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
```

**Credit terms** are NOT new columns — they ride the CRM's own custom-fields mechanism on `crm_companies.custom_data`, already seeded for wholesale (`credit_limit`, `payment_terms`, `delivery_area` in `CRM_SEEDS["wholesale"]`). The wholesale module adds two more seeds (`allocation_priority`, `minimum_order_value`) and reads them via `crm_fields`/`crm_merge_custom_data`. This keeps them self-filling: the LLM lands `credit_limit=1000` when a buyer emails "our limit's a grand", exactly as it lands `fleet_size`.

---

## §5 DB layer — `src/app/db.py` function signatures

New `wholesale_*` functions, same style as the `crm_*` block (service-role writes, `client=` honoured on reads, RLS does the isolation). Reuse `crm_create_deal`, `crm_update_deal`, `crm_get_or_create_company`, `crm_merge_custom_data`, `crm_list_deals`, `crm_insert_activity`, `insert_email_log`, `effective_config` verbatim — do not re-implement.

```python
# ── Order lines ─────────────────────────────────────────────────────────────
def wholesale_insert_line(tenant_id: str, deal_id: str, *, description: str,
                          quantity: float, unit: str | None = None, sku: str | None = None,
                          unit_price: float | None = None, unit_cost: float | None = None,
                          line_order: int = 0) -> dict: ...
def wholesale_list_lines(tenant_id: str, deal_id: str, client=None) -> list[dict]: ...
def wholesale_set_allocation(tenant_id: str, line_id: str, allocated_qty: float) -> None: ...
def wholesale_delete_line(tenant_id: str, line_id: str) -> None: ...

# ── Order heads (thin wrappers over crm_deals) ──────────────────────────────
def wholesale_create_order(tenant_id: str, *, company_id: str, contact_id: str | None,
                           title: str, required_by: str | None,
                           lines: list[dict], standing_order_id: str | None = None,
                           status: str = "quote") -> dict:
    """Create an order = crm_create_deal(is_order=True, wholesale_status=status) +
    N wholesale_insert_line. value_estimate = sum(qty*unit_price). Returns the deal row."""
def wholesale_list_orders(tenant_id: str, *, status: str | None = None,
                          due_before: str | None = None, company_id: str | None = None,
                          client=None) -> list[dict]:
    """Order deals (is_order=true), optionally filtered by wholesale_status / required_by."""
def wholesale_set_status(tenant_id: str, deal_id: str, status: str) -> None:
    """Advance wholesale_status (delegates to crm_update_deal)."""

# ── Standing orders ─────────────────────────────────────────────────────────
def wholesale_create_standing_order(tenant_id: str, fields: dict) -> dict: ...
def wholesale_list_standing_orders(tenant_id: str, *, status: str | None = None,
                                   client=None) -> list[dict]: ...
def wholesale_due_standing_orders(on_or_before: str, limit: int = 500) -> list[dict]:
    """Service-role sweep read: active standing orders with next_run_on <= date,
    ACROSS all tenants (the engine scopes per row.tenant_id) — mirrors
    crm_due_enrollments."""
def wholesale_advance_standing_order(tenant_id: str, so_id: str, *, last_run_on: str,
                                     next_run_on: str) -> None: ...

# ── Credit maths (read-side; the gate in §6/§12 calls this) ─────────────────
def wholesale_credit_exposure(tenant_id: str, company_id: str, client=None) -> dict:
    """{'open_orders': £, 'unpaid_invoices': £, 'exposure': £, 'limit': £|None,
    'headroom': £|None, 'over': bool}. open_orders = SUM(value_estimate) over
    crm_deals where is_order and wholesale_status in ('order','allocation','fulfilment');
    unpaid_invoices = SUM(amount) over invoices where status in ('sent','overdue');
    limit read from crm_companies.custom_data->>'credit_limit'."""
```

---

## §6 Engine / automation

Two automation loops, both on the **existing in-process scheduler** (`src/app/scheduler.py` `run_daily`/`run_every`) wired in `src/app/main.py` alongside the gmail poller and the CRM sequence sweep — no new Render cron service. Both are service-role sweeps that scope per-tenant off each row's `tenant_id`, with per-item try/except isolation, exactly like `crm_sequences.process_sequence_queue` and `send_service_reminders.run`.

### 6a. Standing-order automation — `src/app/wholesale_standing.py`

`run_daily(6, 0, generate_due_standing_orders, "wholesale-standing")`.

```python
async def generate_due_standing_orders() -> dict:
    """For every active standing order whose next_run_on <= today: spawn the cycle's
    order deal from line_template, run the credit gate, draft the confirmation, advance
    the cursor. Service-role; per-standing-order isolation."""
    today = date.today().isoformat()
    due = db.wholesale_due_standing_orders(today, limit=500)
    counts = {}
    for so in due:
        try:
            outcome = await _spawn_cycle(so)   # 'confirmed' | 'held' | 'drafted' | 'skipped'
        except Exception:
            log.warning("standing order %s failed (tenant %s)", so["id"], so["tenant_id"],
                        exc_info=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"due": len(due), **counts}

async def _spawn_cycle(so: dict) -> str:
    tenant_id = so["tenant_id"]
    cfg = db.effective_config(tenant_id, None)
    lines = so["line_template"]                       # jsonb → [{description, qty, unit_price…}]
    order = db.wholesale_create_order(
        tenant_id, company_id=so["company_id"], contact_id=so.get("contact_id"),
        title=f"{so.get('name') or 'Standing order'} — {so['next_run_on']}",
        required_by=so["next_run_on"], lines=lines,
        standing_order_id=so["id"], status="order")
    # Credit gate BEFORE any confirmation goes out (§12).
    gate = wholesale.check_credit(tenant_id, so["company_id"], order["value_estimate"])
    to = _buyer_email(so)
    if gate["over"]:
        db.wholesale_set_status(tenant_id, order["id"], "held")
        # Draft the credit conversation to the OWNER's queue, not an auto-send to the buyer.
        await _draft_credit_hold(tenant_id, cfg, order, gate)
        return "held"
    # Within credit → confirm. compose_outbound → insert_email_log → optional auto-send,
    # the exact send_service_reminders pattern.
    await _draft_order_confirmation(tenant_id, cfg, to, order, auto=so.get("auto_confirm"))
    db.wholesale_advance_standing_order(
        tenant_id, so["id"], last_run_on=so["next_run_on"],
        next_run_on=_next_cycle_date(so))
    return "confirmed" if (so.get("auto_confirm") and cfg.get("auto_send")) else "drafted"
```

`_draft_order_confirmation` is the reminders pattern verbatim:

```python
async def _draft_order_confirmation(tenant_id, cfg, to, order, auto):
    facts = wholesale.confirmation_facts(order)     # pure: buyer, ship date, lines, total
    result = await agent.compose_outbound(cfg, ORDER_PURPOSE, facts)
    log_id, _ = db.insert_email_log(
        tenant_id, to, f"Your order for {order['required_by']}",
        f"[wholesale order] {facts}", result.text,
        status="pending", kind="wholesale_order", thread_id=None)
    usage.record_anthropic(tenant_id, result.model, result.usage,
                           operation="wholesale_order", email_log_id=log_id)
    db.log_agent_event(tenant_id, "wholesale", f"Order confirmation queued for {to}",
                       direction="outbound", contact=to, email_log_id=log_id)
    if auto and cfg.get("auto_send"):
        await mailbox.send_mail(tenant_id, to, f"Your order for {order['required_by']}", result.text)
        db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
```

`_next_cycle_date(so)` is pure date maths from `cadence`/`days_of_week`/`day_of_month`/`lead_days` (unit-tested in isolation).

### 6b. Fulfilment planning + credit sweep — `src/app/wholesale_plan.py`

`run_daily(5, 30, run_fulfilment_plan, "wholesale-plan")` — builds the this-week plan and flags conflicts; also refreshes the credit-risk card. Mostly pure functions the home page and the daily digest both read:

```python
def build_week_plan(orders: list[dict], lines_by_deal: dict, capacity: dict | None) -> list[dict]:
    """Pure. Orders due this week grouped by ship date; per day aggregate ordered qty per
    sku; flag SHORT lines (ordered > allocatable) and resolve them by allocation_priority
    (buyer default overridable per order). Returns [{date, orders, shortages[], waitlist[]}]."""

def credit_risk(exposures: list[dict]) -> list[dict]:
    """Pure. Buyers at/over limit, worst-first, with the drafter stance ('will hold')."""
```

`capacity` is optional — v1 sources it from a tenant-config number per sku (`weekly_capacity`) or from a warehouse integration (§11) when connected; absent capacity means no shortage flags (graceful, not blocking).

### 6c. Credit-limit check + the drafter refusal — `src/app/wholesale.py`

The gate is one pure function plus a drafter hook:

```python
def check_credit(tenant_id: str, company_id: str, order_value: float) -> dict:
    exp = db.wholesale_credit_exposure(tenant_id, company_id)
    if exp["limit"] is None:
        return {"over": False, "headroom": None, **exp}   # no limit set → never blocks
    projected = exp["exposure"] + order_value
    return {"over": projected > exp["limit"],
            "headroom": exp["limit"] - exp["exposure"], "projected": projected, **exp}
```

The **inbound** touchpoint: the pipeline already calls the CRM extraction on every email and already builds a `crm_context` string it hands to `draft_reply` (`src/app/agent.py draft_reply(..., crm_context=...)`). Wholesale appends a **credit block** to that context so the drafter *refuses in the tenant's voice* when an inbound is placing/growing an order over the limit — see §12 for the block + prompt. No new call to the model; it's one more section in the system prompt the drafter already builds.

---

## §7 API + routes — `src/app/wholesale_routes.py`

JSON API mirrors `src/app/crm_routes.py`; RLS-scoped via the tenant client. Order CRUD are thin over the deal helpers.

```
GET    /api/wholesale/orders                 — list (status, due_before, company filters)
POST   /api/wholesale/orders                 — create order (buyer + lines) → deal
GET    /api/wholesale/orders/{id}            — order detail (deal + lines + credit stance)
PATCH  /api/wholesale/orders/{id}            — edit lines / required_by / status
POST   /api/wholesale/orders/{id}/advance    — move wholesale_status one state (§3)
POST   /api/wholesale/orders/{id}/allocate   — set allocated_qty per line
POST   /api/wholesale/orders/{id}/invoice    — REUSE CRM §22 invoice-from-order

GET    /api/wholesale/standing               — list standing orders
POST   /api/wholesale/standing               — create (buyer + cadence + line template)
PATCH  /api/wholesale/standing/{id}          — edit / pause / end
POST   /api/wholesale/standing/{id}/run      — force-generate this cycle now
POST   /api/wholesale/standing/{id}/skip     — skip the next cycle

GET    /api/wholesale/plan?week=YYYY-Www      — this-week fulfilment plan + conflicts
GET    /api/wholesale/credit                  — credit-risk buyers (exposure vs limit)
GET    /api/wholesale/credit/{company_id}     — one buyer's exposure detail
```

Server-rendered pages (Jinja2, house design system) under the `/app/wholesale` prefix in the same router file, like `dashboard.py`.

---

## §8 UI — pages + nav

| Route | Page | Notes |
| --- | --- | --- |
| `/app/wholesale` | **OS home** — the three daily-rhythm cards (§2) | mirrors `/app/tms` home |
| `/app/wholesale/orders` | Order book — filterable list, status columns | reuses the pipeline/kanban CSS from CRM |
| `/app/wholesale/orders/{id}` | Order detail — lines, credit stance, status ladder, invoice button | |
| `/app/wholesale/standing` | Standing orders — list + builder (buyer, cadence, line template, auto-confirm) | builder mirrors the sequence-step editor |
| `/app/wholesale/plan` | This-week fulfilment plan — day columns, shortage/waitlist flags | |
| `/app/wholesale/credit` | Credit ledger — every buyer, exposure vs limit, overdue invoices | |

**Nav.** Add a wholesale entry to the `MODULES` registry in `src/app/dashboard.py` (§10) — its card lights on `/app/modules` when the `wholesale` subscription row activates, and the sidebar picks it up with zero further code. The Today dashboard (`app_home`) gains a best-effort wholesale card (standing-orders-due count + credit-risk count) guarded by try/except, exactly like the existing CRM Today cards.

---

## §9 Onboarding + industry seeds

- **`operating_systems.py`** — flip `wholesale` to `live: True` in `OPERATING_SYSTEMS` once the module ships (release-gates the Shape selector, per the house rule that an industry is only selectable when its module exists).
- **`onboarding.py INDUSTRIES`** — remove `hidden: True` and set `live: True` on the `wholesale` entry (id/os already correct: `os: "wholesale"`). Sub-verticals (brewery, bakery, food-service) onboard via the same `wholesale` id + the free-text catch-all; no new ids needed (matches the doc's "OS seeds minimal, industry seeds carry vocabulary" rule).
- **`industry_lingo.py`** — `LINGO["wholesale"]` exists (orders, SKUs, stock, lead times, MOQ, trade pricing, pro forma, dispatch). `CRM_SEEDS["wholesale"]` exists (credit_limit, payment_terms, delivery_area, buyer role, products, order frequency). **Add** two company seeds — `allocation_priority` (number, hint "priority tier if we're short — 1 is highest") and `minimum_order_value` (number, hint "minimum order value / carriage-paid threshold") — so the killer feature's inputs self-fill.
- **`onboarding.py DATA_SOURCES`** — add a `"wholesale"` key: Xero/Sage (accounts — where invoices + credit live), Unleashed/DEAR (warehouse — stock + allocation), spreadsheet (the wedge's current system-of-record), calendar. Each `{kind, label, hint, vendors}` like the existing logistics/trades blocks.
- **Standing-order seeding** — offer `wholesale_standing.seed_examples(tenant_id)` at onboarding finish (opt-in, like `crm_sequences.crm_seed_default_sequences`): a couple of blank standing-order templates so the "your Tuesday book" surface isn't empty on day one. Never auto-run across all tenants.

---

## §10 Module registration + plan wiring

- **`dashboard.py MODULES`** — add:
  ```python
  {"id": "wholesale", "name": "Wholesale OS", "group": "addon", "badge": "Wholesale",
   "href": "/app/wholesale",
   "blurb": "The whole wholesale book: standing orders that generate themselves, "
            "credit-limit-aware order confirmations, and a week-ahead fulfilment plan "
            "that flags every shortfall — all off the inbox the agent already reads."},
  ```
- **`plans.py INDUSTRY_MODULE_META`** — add a `wholesale` entry keyed `"wholesale"`:
  ```python
  "wholesale": {"key": "wholesale", "label": "Wholesale OS", "name": "Wholesale OS",
                "features": ["Standing-order automation", "Credit-limit-aware ordering",
                             "Allocation & fulfilment planning", "Order-to-invoice loop"]},
  ```
  `INDUSTRY_MODULE`, `MANAGED_MODULES`, `modules_for()` all derive from this automatically. So a `wholesale`-industry tenant on a `professional`/`scale`/`enterprise` tier gets the `wholesale` module unlocked, and `billing_plans("wholesale")` renders "Core + Wholesale OS" with the right feature list — **no other pricing code changes**.
- **Pricing tier.** Wholesale OS is the vertical add-on at the existing +industry tier (`professional`, £299), consistent with Field OS · Transport. Core (£99) still carries the CRM/drafter/calendar that a wholesaler also uses; the module is the standing-order/credit/fulfilment engine. The `BASE_PLAN_MODULES` map is unchanged (scheduling stays base). This matches `docs/BLAM8_operating_systems.md` §9's recommendation (Core covers CRM regardless of OS; each OS module is the £150–300 upsell).
- **`db.apply_plan_modules`** reconciles a subscription to `modules_for()` on activation — since the meta entry above feeds it, activating a wholesale subscription lights the module with no extra wiring.

---

## §11 OS-native integrations

| Integration | Purpose | v1 vs later |
| --- | --- | --- |
| **Xero** | Push invoices, pull payment/credit status → drives `wholesale_credit_exposure` | **v1** (accounts truth for the credit gate; reuses the §22 invoice→accounting seam) |
| **Sage** | Same, for Sage-run wholesalers | **later** (Xero first — bigger UK SMB share) |
| **Unleashed** | Warehouse stock + on-hand → feeds allocation & shortage flags | **v1-lite** (read stock levels into `capacity`; write-back later) |
| **DEAR (Cin7 Core)** | Same as Unleashed | **later** |
| **Salesforce** | For larger wholesalers already on SF | **later** (BLAM8's own CRM is the buyer store; SF sync is an enterprise ask) |

**v1 posture:** ship without any integration required — capacity/credit read from tenant-config numbers + the tenant's own invoices, so the wedge (spreadsheet artisans) get the whole loop with nothing to connect. Xero + Unleashed are the first two connectors because they harden the two killer inputs (credit truth, stock truth); everything else is inbound-demand-gated, exactly the doc's §4.4 integration ranking (Xero, Sage, Salesforce, Unleashed, DEAR).

---

## §12 Killer feature deep-dive

### 12a. Standing-order automation

The engine in §6a turns "every Tuesday and Friday" into order deals that draft their own confirmations. Mechanics: a `wholesale_standing_orders` row holds the recurrence + a `line_template`; the daily 06:00 sweep spawns the cycle order, runs the credit gate, and either (a) auto-confirms to a trusted account (`auto_confirm` + tenant `auto_send`) or (b) queues a pending draft for the owner's approval — the identical draft-first-or-full-auto split the whole product uses (`send_service_reminders`, `crm_sequences`). The tenant's Tuesday goes from "re-key 14 orders from memory" to "glance at 14 pre-drafted confirmations, hit approve." Skips and per-cycle amendments are one-click on the home card before generation.

### 12b. The credit-limit-aware drafter (the refusal)

This is the sharpest single line: **BLAM8 is the only inbox agent that will refuse an order because the buyer is over their credit limit — in the owner's voice, without the owner watching.**

**LLM mechanics.** The pipeline already runs CRM extraction on every inbound and hands `draft_reply` a `crm_context` string. Wholesale computes the buyer's live exposure (`wholesale_credit_exposure`) and, when the inbound is an order/RFQ that would push `exposure + order_value > credit_limit`, appends a **credit-gate block** to that context. The drafter reads it like any other business rule — no extra model call, one more section in the system prompt it already assembles.

**The block injected into `crm_context`:**

```
## Credit gate — HARD RULE
This buyer is on account. Current exposure £{exposure} against a £{limit} limit
({unpaid_invoices} unpaid invoices, {open_orders} in open orders). This new order
(~£{order_value}) would take them to £{projected} — OVER their limit by £{overage}.

Do NOT confirm or accept this order. Instead, warmly explain we can't release it
until the account is brought back within terms, name the amount outstanding, and
offer the way forward (settle the overdue balance, or a part-order within the
£{headroom} of headroom). Keep it courteous — this is a valued account, not a
debt-collection notice. Never invent figures beyond those above.
```

The order deal is set `wholesale_status = 'held'`; the draft goes to the **owner's approval queue** (never auto-sent, even for full-auto tenants — a refusal is a judgment call the owner should see once). When the buyer settles (payment detected via §22 → exposure drops → headroom returns), the hold clears and the standing-order/next reply flows normally.

**Confidence + safety.** The gate is arithmetic, not an LLM judgment — the model never *decides* whether to refuse, it only *phrases* a refusal the deterministic check already mandated. So there's no hallucinated-refusal risk: no limit set → `over=False` → the block is never added and the drafter behaves exactly as today. This keeps a wholesaler who doesn't run credit terms completely unaffected.

### 12c. Fulfilment-conflict surfacing

`build_week_plan` (§6b) aggregates ordered qty per sku per ship-day and compares to allocatable capacity. When ordered > allocatable it flags a **shortage**, then resolves it by `allocation_priority` (buyer default, overridable per order): priority-1 buyers filled first, the balance waitlisted with a drafted "we can do 40 of 50 this week — hold the balance to Friday?" note the owner can send. The plan is the home page's Card 3 and the 05:30 digest. It's pure and data-complete, so it's honest from day one — the same property that makes the CRM's reports trustworthy.

---

## §13 Build order

Each phase is independently shippable and demoable. Rough single-developer-with-LLM pace.

**Phase W1 — Order primitive + order book (≈1 wk).** Migration (deal columns + `wholesale_order_lines` + RLS); `db.wholesale_*` line/order helpers; `wholesale_create_order`; order CRUD API; order book + detail pages. Ships: a wholesaler can see/create orders as line-item deals, invoice them via the existing §22 path. Reuses everything CRM.

**Phase W2 — Credit gate + the refusal (≈1 wk).** `wholesale_credit_exposure` + `check_credit`; the credit-gate block into `crm_context`; the `held` state + owner-queue routing; credit-risk home card + `/credit` ledger. Ships the killer differentiator. Depends on §22 invoices existing for the unpaid-invoice half of exposure (open-order half works standalone if not).

**Phase W3 — Standing-order automation (≈1–1.5 wk).** `wholesale_standing_orders` table + helpers; `wholesale_standing.py` engine on `run_daily`; standing-order builder UI; onboarding seed. Ships the everyday-magic surface. Reuses the scheduler + reminders send pattern wholesale.

**Phase W4 — Fulfilment planning (≈1 wk).** `wholesale_plan.py` (`build_week_plan`, `credit_risk`); this-week plan page + home Card 3; allocation API; capacity from tenant-config. Ships the third daily card + conflict surfacing.

**Phase W5 — Integrations + go-live (≈1–1.5 wk).** Xero (credit/payment truth) + Unleashed (stock→capacity); flip `operating_systems.is_live`; unhide onboarding; `plans.py`/`dashboard.py` registration; DATA_SOURCES; docs + landing.

Total ≈ 5–6 weeks, matching the doc's §8 per-OS estimate — and lighter than Storefront/Practice because so much is CRM reuse (§15).

---

## §14 Open questions + risks

- **Credit exposure source of truth.** Before Xero is connected, unpaid-invoice exposure comes from BLAM8's own `invoices` (§22). If the wholesaler invoices *outside* BLAM8, exposure under-counts and the gate under-refuses. Mitigation: v1 makes clear the gate is only as good as the invoices BLAM8 sees; Xero connect (W5) closes it. *Recommendation: ship W2 with an "exposure from BLAM8 invoices only" caveat on the credit card.*
- **Allocation capacity model.** A baker's constraint is daily bake capacity; a distributor's is on-hand stock; a packaging supplier's is lead time. v1 uses a single per-sku `weekly_capacity` number — crude but honest. Richer models (per-day, per-line, BOM) defer until a design partner needs one.
- **Standing-order amendments.** How does a buyer's "no rye this week, double the sourdough" email amend the *next generated* cycle? v1: the LLM extraction flags the amendment as a promise/note on the buyer; the owner edits the pending order. Auto-applying amendments to the line template is a later, riskier step.
- **Order vs deal in reporting.** Order deals (`is_order`) will show in the CRM pipeline/reports unless filtered. Decide whether the relationship pipeline hides `is_order` deals (recommended — an order isn't a sales opportunity) or shows them in a wholesale lane. *Recommendation: filter `is_order` out of the CRM pipeline; they live on the wholesale board.*
- **Multi-OS tenants.** An artisan producer with a *shop* (Storefront) + a *wholesale side* is the wedge's natural end-state. Per doc §9, primary OS at onboarding + secondary later; defer detailed design.
- **Partial-allocation accounting.** When an order ships short and the balance carries (`on_hold_short`), does the invoice bill the shipped qty or the ordered qty? *Recommendation: invoice the allocated/shipped qty; the balance rides the next cycle.*

---

## §15 Reuse map

The load-bearing section: Wholesale OS is mostly reuse. **New code is one primitive table, one standing-order table + engine, a credit gate, and six pages.**

| Part | REUSE (name it) | BUILD NEW |
| --- | --- | --- |
| Buyer (company) | `crm_companies`, `crm_get_or_create_company`, freemail/stated-name inference (`src/app/crm.py`) | — |
| Buyer contact | `crm_contacts`, `crm_get_or_create_contact` | — |
| Order head | `crm_deals` (+ `crm_create_deal`/`crm_update_deal`); `is_order`/`wholesale_status`/`required_by` columns | additive columns only |
| Order line items | — | `wholesale_order_lines` table + `wholesale_*_line` helpers |
| Credit terms (limit, payment terms, allocation priority, MOV) | `crm_companies.custom_data`, `crm_fields` extraction, `crm_merge_custom_data`, `CRM_SEEDS["wholesale"]` | +2 seed fields (`allocation_priority`, `minimum_order_value`) |
| Self-filling from email | pipeline → `crm.ingest_email_log_row` → `crm_fields`/`crm_extract` LLM autofill | — |
| Standing-order recurrence | scheduler *pattern* (`scheduler.run_daily`) | `wholesale_standing_orders` table + `wholesale_standing.py` engine + date maths |
| Order confirmation / dispatch drafts | `agent.compose_outbound` → `db.insert_email_log` → `mailbox.send_mail` (the `send_service_reminders` pattern) | thin `_draft_order_confirmation` wrapper |
| Credit-refusal drafter | `agent.draft_reply(crm_context=…)` — one appended prompt block; no new model call | the credit-gate block + `check_credit` |
| Quote at each state | `draft_reply` quote path, `fsm_pricing`/`pricing` rate-card pattern | wholesale trade-price read (custom fields) |
| Invoice / payment / overdue chaser | **CRM §22** `invoices`/`payments`, LLM payment detection, invoice-from-lines, drafter chaser | invoice-from-order-lines adapter |
| Promise tracking (buyer said "I'll pay Friday") | `crm_intel` promises + open-promise dedupe/auto-resolve | — |
| Follow-up sequences (chase a lapsed buyer) | `crm_sequences` engine + enrollments, wholesale seed sequence | one seed sequence |
| Daily-rhythm home + Today cards | `dashboard.py app_home` card pattern (try/except-guarded, like the CRM cards) | 3 wholesale cards |
| Reports (order value, fulfilment rate) | CRM §19.1 reporting pattern (server-side aggregate, Chart.js) | wholesale queries |
| Tasks (chase overdue, hold released) | `crm_tasks` (§19.2), auto-generate on `held`/overdue | task-source hooks |
| RLS / migration | `auth_tenant_ids()` policy pattern, `scripts/db/migrations/apply_crm_phase4b_migration.py` psycopg2 idempotent template | one migration script |
| Scheduler wiring | `src/app/main.py` in-process loop registration (gmail poller, sequence sweep) | 2 `run_daily` registrations |
| Module registry / plan / pricing | `dashboard.MODULES`, `plans.INDUSTRY_MODULE_META`/`modules_for`/`billing_plans`, `apply_plan_modules` | 1 MODULES + 1 META entry |
| Onboarding (Shape → Industry) | `operating_systems`, `onboarding.INDUSTRIES`/`DATA_SOURCES`, `industry_lingo` | flip live/hidden flags, +1 DATA_SOURCES block |
| Voice seed | `industry_lingo.LINGO["wholesale"]` | — |

**Net new modules:** `src/app/wholesale.py` (credit gate + pure view-models), `src/app/wholesale_standing.py` (engine), `src/app/wholesale_plan.py` (fulfilment planning), `src/app/wholesale_routes.py` (API + pages), one migration script, six templates. Everything else is a flag flip, a seed, or a call into the platform.
