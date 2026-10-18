# BLAM8 Storefront OS — Design & Build Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, P1–P4 (src/app/storefront.py, src/app/shop_ocr.py, src/app/shop_routes.py; tests/test_storefront.py), deployed. Live proof: none.

**Status:** 🔨 Not built. Build-ready spec. Second OS after Field (`docs/BLAM8_operating_systems.md` §8 Phase B).
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-05
**Companion docs:** `docs/BLAM8_operating_systems.md` (§2 layers, §4.2 this OS, §5 onboarding, §6 migration); `docs/architecture/CRM_design.md` (format + platform patterns this spec reuses wholesale).

---

## 0. Positioning

**Shape:** the customer visits the shop for goods (`docs/BLAM8_operating_systems.md` §4.2). Unlike Field OS (you go to the customer) and Practice OS (the customer books your time), Storefront is about **goods moving off a shelf** — stock comes in, gets caught by the inbox/phone/counter, gets reserved, gets collected, and the till rings. The daily reality is inventory that decays (best-before) and a stream of small orders through channels a corner shop actually uses: a WhatsApp "save me two sourdoughs", an email standing order, a walk-in.

**Wedge:** farm shops and independent grocers with **no POS today**. They run stock in their heads and orders on scraps of paper. Every existing answer (Square, Shopify, Vend) asks them to *first* buy a till system and *then* type every product in by hand. BLAM8's wedge is the inversion the CRM already proved: **photograph the delivery note → stock is live → replies now know what's in.** Zero typing, no hardware. The first time a farm-shop owner sees "I photographed the veg delivery and the shop now knows it has 12 crates of Maris Pipers," they switch.

**Killer feature (two halves, §12):**
1. **OCR-inferred inventory** — a photo of a delivery note / supplier invoice / shelf / chalkboard → LLM extraction → stock update. The self-filling principle (`docs/architecture/CRM_design.md` §1) applied to a catalogue instead of a contact book.
2. **Best-before drafter** — when a batch is N days from its sell-by, BLAM8 drafts a personalised "£1 off, going today" to the **regulars who have actually bought that product before** (derived from transaction history), in the shop's own voice. Nobody else pushes personalised specials off decaying stock off email content alone.

**Target industries (~200K UK SMBs):** farm shops, delis, butchers, bakeries, cafes, small restaurants, small groceries, wine merchants, artisan retailers, plant nurseries, garden centres. All share the `transaction` primitive; vocabulary differs per industry and rides on a config seed (§9), not code.

**Current build state:** none of it exists. New primitives (`products`, `transactions`), the OCR extraction pipeline, and the best-before drafter job are all net-new. Everything else — the drafter, CRM contacts as "customers", the sequences engine, the scheduler, RLS, module registry, onboarding — is reused (§15).

---

## 1. The primitive

**`transaction`** — a customer + line items (product × qty × unit price) + payment method + collection status. This is the thing the daily surface counts and the thing that "completes". It is genuinely new: no existing BLAM8 entity carries *line items against inventory that decrements on hand*. `bookings` is a capacity reservation (one window on one date), not a basket of goods; a CRM `deal` is an open sales opportunity, not a till receipt.

But Storefront leans hard on two things that already shape like existing entities:

- **The customer IS a CRM contact.** A Storefront transaction's customer is a `crm_contacts` row (reused unchanged), and their shop is a `crm_companies` row where applicable (a cafe buying wholesale, a restaurant with an account). This is the whole reason the best-before drafter can be personalised for free: purchase history hangs off a contact that BLAM8 already auto-populates from the inbox. **We do not build a second customer table.**
- **A reservation IS a capacity-free booking-shaped hold, but its shape differs enough to justify its own table.** A Storefront reservation ("hold two sourdoughs for Dave, collecting Saturday") is a *promise against stock*, not against a diary window. It needs `product_id + qty + ready_date + collection status`, and it must atomically decrement available stock the way `hold_booking_if_capacity` atomically decrements a window (§4, §5). So the reservation lives inside the `transaction` lifecycle (status `reserved`), enforced by a stock-aware atomic RPC modelled on `db.hold_booking_if_capacity` — **the pattern is reused, the table is new** because the unit of capacity is a product's on-hand quantity, not a date's window count.

New tables, justified: `products` (the catalogue — no analogue), `transactions` + `transaction_items` (the basket — no analogue), `stock_movements` (the append-only ledger that makes OCR auditable — no analogue). Reused as-is: `crm_contacts`, `crm_companies`, `crm_sequences`/enrollments, `email_log`, `bookings` (untouched — a veg-box delivery run is Field OS, see §14 multi-OS).

**Field library seed (per §4.2):** product SKU, product name, quantity on hand, unit price, best-before date, supplier, sell-by alert lead time (days before expiry to notify), reorder threshold. These are first-class columns on `products` (below), not CRM custom fields — the catalogue is core to the OS, so it earns real columns. Tenant-specific extras (e.g. a wine merchant's `vintage`, a butcher's `cut`) ride on `products.custom_data` JSONB, filled by the same LLM extraction path CRM custom fields use (`src/app/crm_fields.py`).

---

## 2. Daily-rhythm surface

Route `/app/shop` — the Storefront home, the equivalent of Field OS's dispatch board and the CRM's Today cards. Three stacked panels, computed server-side from the tables in §4, no new data collection:

**Panel 1 — Today's collections.** Every `transaction` with `status='reserved'` and `ready_date <= today` (plus overdue ones), oldest first: customer name, the line items, the total, ready/overdue flag. This is the "who's picking up today" the owner works down at the counter. One-tap **Complete** flips it to `status='completed'` (and drafts the receipt, §3).

**Panel 2 — Best-before nudges.** Every product with `best_before_date` within its `sell_before_lead_days` window and `qty_on_hand > 0`, sorted by urgency (soonest expiry first). Each row shows the product, qty at risk, days left, and — the differentiator — **how many past buyers** BLAM8 can nudge (a count from `transaction_items` history). A **Draft specials** button fires the best-before drafter (§12.2) for that batch. This panel is the money-saver: stock that would be binned becomes a personalised offer.

**Panel 3 — Stock levels + reorder.** Live on-hand quantities, with any product at/below `reorder_threshold` flagged amber ("Reorder — 2 left, threshold 5"). A **Photograph delivery** primary action opens the OCR intake (§12.1). This is where "zero typing" lives on the daily surface: the owner photographs a delivery note and the numbers here go up.

Why these three and nothing else: they mirror the three verbs of a shop's day — *serve collections, save decaying stock, keep shelves full*. Anything else (full catalogue editing, transaction history, supplier admin) is a secondary page reached from here, not the daily surface.

A **Today** card also lands on the main `/app` home (like the CRM cards in `src/app/dashboard.py:app_home`): "3 collections today · 4 lines going off · 2 to reorder", best-effort, hidden on read failure.

---

## 3. Workflow state machine

The `transaction.status` lifecycle. Every transition is a drafter touchpoint — BLAM8 writes the outbound in the tenant's learned voice via `agent.compose_outbound` (proactive) or `agent.draft_reply` (in-thread reply), queued to `email_log(status='pending')` for approval or auto-sent for full-auto tenants — exactly the pattern in `src/app/jobs/send_service_reminders.py` and `src/app/crm_sequences.py`.

```
                (stock arrives — OCR/manual, no transaction yet)
                                  │  updates products.qty_on_hand
                                  ▼
   enquiry ──▶ reserved ──▶ ready ──▶ completed
      │           │           │          │
   (draft:     (draft:     (draft:    (draft:
   availability confirmation "ready to  receipt +
   + price)    + hold        collect")  best-before
                confirmation)            follow-up seed)
      │
      └──▶ declined / cancelled  (draft: polite decline, stock released)
```

| Transition | Trigger | What BLAM8 drafts | Mechanics |
|---|---|---|---|
| — → **stock in** | OCR photo or manual add | (internal — no customer email) an audit note; optional "low stock cleared" | `products.qty_on_hand += extracted`, `stock_movements` row |
| inbound → **enquiry** | Customer emails/WhatsApps "do you have X?" | Availability + price reply, off live `products` | `draft_reply` with the Storefront tool (`check_stock`) injected, like the scheduling tools |
| enquiry → **reserved** | Customer says "hold N for me, collecting Sat" | Confirmation: "Held 2 sourdoughs for you, ready Saturday" | `hold_stock_if_available` RPC (§5) atomically decrements; `draft_reply` with `reserve_stock` tool |
| reserved → **ready** | Batch made/arrived, or owner marks ready | "Your order's ready to collect" | `compose_outbound`, proactive; scheduler job (§6) can auto-fire on `ready_date` |
| ready → **completed** | Owner taps Complete at the counter | Receipt / thank-you; seeds a best-before-style follow-up | `compose_outbound`; `stock_movements` records the sale |
| enquiry/reserved → **declined/cancelled** | Out of stock, or customer pulls out | Polite decline; stock released back | `compose_outbound`; reserved qty returned to `qty_on_hand` |
| (any product, batch expiring) → **best-before push** | Scheduler finds a batch inside its lead window | Personalised "going today, £X off" to past buyers | best-before drafter (§12.2) + optionally a `crm_sequences` enrollment |

The drafter is **stock-aware** the same way it's already deal-stage-aware (`docs/architecture/CRM_design.md` §5 Phase 3): a `storefront_context` string (current basket, on-hand for the products mentioned, the customer's past purchases) is passed into `draft_reply` via the existing `crm_context`-style channel (§6), so a reply never promises stock the shop doesn't have.

---

## 4. Schema

Additive migration only; RLS on every table with the house predicate `for all using (tenant_id in (select auth_tenant_ids()))` (mirrors every `crm_*` table in `db/schema.sql` and the migration in `scripts/db/migrations/apply_crm_phase4b_migration.py`). All ids `uuid default gen_random_uuid()`; all `tenant_id uuid not null references tenants(id) on delete cascade`.

```sql
-- ── The catalogue (the field library, §1) ───────────────────────────────────
create table if not exists shop_products (
  id                   uuid primary key default gen_random_uuid(),
  tenant_id            uuid not null references tenants(id) on delete cascade,
  sku                  text,                       -- tenant's own code; nullable (many shops have none)
  name                 text not null,
  unit                 text default 'each',        -- each | kg | crate | bottle | loaf …
  unit_price           numeric,                    -- sell price per unit, GBP
  qty_on_hand          numeric not null default 0, -- current stock, decremented on reserve/sale
  qty_reserved         numeric not null default 0, -- held against open reservations (available = on_hand - reserved)
  best_before_date     date,                       -- current batch's sell-by (nullable; non-perishables have none)
  supplier             text,
  sell_before_lead_days integer default 2,         -- days before best_before to start nudging
  reorder_threshold    numeric,                    -- amber flag on the daily surface when qty_on_hand <= this
  active               boolean not null default true,
  custom_data          jsonb not null default '{}'::jsonb,  -- tenant extras (vintage, cut…), LLM-filled
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (tenant_id, lower(name))                  -- one row per named product per tenant (dedupes OCR)
);
create index if not exists shop_products_tenant_idx        on shop_products (tenant_id);
create index if not exists shop_products_bestbefore_idx    on shop_products (tenant_id, best_before_date)
  where best_before_date is not null and qty_on_hand > 0;
create index if not exists shop_products_reorder_idx       on shop_products (tenant_id, active);

-- ── The primitive: transactions + their line items ──────────────────────────
create table if not exists shop_transactions (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  contact_id     uuid references crm_contacts(id) on delete set null,  -- the customer IS a CRM contact (§1)
  company_id     uuid references crm_companies(id) on delete set null, -- account customers (cafe/restaurant)
  status         text not null default 'enquiry',   -- enquiry | reserved | ready | completed | declined | cancelled
  channel        text default 'email',              -- email | whatsapp | walk_in | phone
  payment_method text,                              -- cash | card | account | unpaid
  total_amount   numeric,                           -- Σ line items; denormalised for the daily surface
  currency       text not null default 'GBP',
  ready_date     date,                              -- when a reservation is collectable
  thread_id      text,                              -- links back to the email_log thread (drafter context)
  notes          text,
  custom_data    jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  completed_at   timestamptz,
  updated_at     timestamptz not null default now()
);
create index if not exists shop_tx_tenant_status_idx on shop_transactions (tenant_id, status, ready_date);
create index if not exists shop_tx_contact_idx       on shop_transactions (tenant_id, contact_id);

create table if not exists shop_transaction_items (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  transaction_id uuid not null references shop_transactions(id) on delete cascade,
  product_id     uuid references shop_products(id) on delete set null,  -- null = free-text line not yet matched
  product_name   text not null,           -- captured verbatim (survives a product delete / pre-match)
  qty            numeric not null default 1,
  unit_price     numeric,                 -- snapshot at reservation time (price may change later)
  line_total     numeric,
  created_at     timestamptz not null default now()
);
create index if not exists shop_tx_items_tx_idx      on shop_transaction_items (tenant_id, transaction_id);
create index if not exists shop_tx_items_product_idx on shop_transaction_items (tenant_id, product_id);

-- ── The stock ledger: every change to on-hand, append-only (audit + OCR trust) ─
create table if not exists shop_stock_movements (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  product_id     uuid not null references shop_products(id) on delete cascade,
  delta          numeric not null,        -- +12 (delivery), -2 (reservation), -1 (sale), +2 (cancel)
  reason         text not null,           -- stock_in | reserve | release | sale | adjust | wastage
  source         text,                    -- ocr | manual | agent | scheduler
  source_ref     text,                    -- email_log id, transaction id, or OCR job id
  best_before_date date,                  -- the batch's sell-by, when a delivery states one
  note           text,
  created_at     timestamptz not null default now()
);
create index if not exists shop_stock_mov_product_idx on shop_stock_movements (tenant_id, product_id, created_at desc);

-- ── OCR extraction jobs (the killer feature's audit + confidence gate, §12.1) ─
create table if not exists shop_ocr_jobs (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  kind          text not null default 'delivery_note', -- delivery_note | invoice | shelf | chalkboard
  image_path    text,                    -- storage ref for the uploaded photo
  status        text not null default 'pending',  -- pending | extracted | applied | rejected
  extracted     jsonb,                   -- [{name, qty, unit, best_before, unit_price, confidence}]
  applied_at    timestamptz,
  email_log_id  text,                    -- when the photo arrived as an email attachment
  created_at    timestamptz not null default now()
);
create index if not exists shop_ocr_jobs_tenant_idx on shop_ocr_jobs (tenant_id, status, created_at desc);
```

RLS block for every table (one shown; repeat per table, exactly as `scripts/db/migrations/apply_crm_phase4b_migration.py` does):

```sql
alter table shop_products enable row level security;
drop policy if exists "tenant isolation - shop_products" on shop_products;
create policy "tenant isolation - shop_products" on shop_products
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
```

**Migration script:** `scripts/db/migrations/apply_storefront_migration.py`, byte-for-byte the shape of `scripts/db/migrations/apply_crm_phase4b_migration.py` — psycopg2, `env_value("DB_PASSWORD")`, direct pooler host, `autocommit`, `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, then a verify loop asserting each table + the RLS policies exist. Idempotent, re-runnable.

---

## 5. DB layer

New `shop_*` functions in `src/app/db.py`, following the established `crm_*` conventions exactly: `tenant_id: str` first-class arg on every function; `client: Client | None = None` (None → service-role `db()` for schedulers/OCR ingest, a `TenantCtx.client` for RLS-scoped reads from routes); list functions take `limit`/`offset`; JSONB merged read-merge-write; `updated_at` stamped on writes; get-or-create is race-safe (insert in try/except, re-fetch on conflict) like `crm_get_or_create_contact`.

```python
# ── catalogue ────────────────────────────────────────────────────────────────
def shop_list_products(tenant_id, *, q=None, low_stock=False, expiring=False,
                       limit=200, offset=0, client=None) -> list[dict]: ...
def shop_get_product(product_id, tenant_id, client=None) -> dict | None: ...
def shop_get_or_create_product(tenant_id, name, *, unit="each",
                               best_before_date=None, supplier=None) -> dict: ...   # dedupes on lower(name)
def shop_update_product(product_id, tenant_id, fields, client=None) -> None: ...     # stamps updated_at
def shop_merge_product_custom_data(product_id, tenant_id, values, client=None) -> None: ...  # reuse crm_merge_custom_data shape
def shop_products_expiring(tenant_id, within_days, client=None) -> list[dict]: ...   # best-before drafter feed (service-role)
def shop_products_low_stock(tenant_id, client=None) -> list[dict]: ...               # reorder panel

# ── stock ledger (atomic on-hand mutation goes through the RPC, not here) ─────
def shop_record_movement(tenant_id, product_id, delta, reason, *,
                         source=None, source_ref=None, best_before_date=None,
                         note=None, client=None) -> dict: ...   # append + qty_on_hand += delta in the RPC below
def shop_apply_stock_in(tenant_id, lines, *, source="ocr", source_ref=None) -> dict:
    """Bulk stock-in: for each {name, qty, unit, best_before, unit_price} get-or-create
    the product, then call the atomic increment RPC + write a stock_movements row.
    Returns {created, updated, movements}. Service-role (OCR + scheduler use it)."""

# ── transactions ─────────────────────────────────────────────────────────────
def shop_list_transactions(tenant_id, *, status=None, ready_by=None,
                           contact_id=None, limit=100, offset=0, client=None) -> list[dict]: ...
def shop_today_collections(tenant_id, today_iso, client=None) -> list[dict]: ...     # daily surface panel 1
def shop_get_transaction(tx_id, tenant_id, client=None) -> dict | None: ...          # with items embedded
def shop_create_transaction(tenant_id, *, contact_id=None, company_id=None,
                            channel="email", items, status="enquiry",
                            ready_date=None, thread_id=None) -> dict: ...
def shop_set_transaction_status(tx_id, tenant_id, status, *, client=None) -> dict: ... # stamps completed_at on 'completed'
def shop_past_buyers_of(tenant_id, product_id, *, limit=200, client=None) -> list[dict]:
    """Distinct contacts who have a completed transaction_item for this product —
    the best-before drafter's personalised audience (§12.2). Service-role."""

# ── atomic reservation (the hold_booking analogue) ───────────────────────────
def shop_hold_stock_if_available(tenant_id, product_id, qty, *,
                                 transaction_id=None, contact_id=None,
                                 ready_date=None, thread_id=None) -> dict | None:
    """Atomically reserve `qty` iff (qty_on_hand - qty_reserved) >= qty:
    bumps qty_reserved, writes a 'reserve' stock_movement, creates/updates the
    transaction+item. Returns the row or None ('out_of_stock') — mirrors
    db.hold_booking_if_capacity: two racing emails can never over-reserve."""

# ── OCR jobs ─────────────────────────────────────────────────────────────────
def shop_create_ocr_job(tenant_id, kind, image_path, *, email_log_id=None) -> dict: ...
def shop_set_ocr_extracted(job_id, tenant_id, extracted, client=None) -> None: ...
def shop_mark_ocr_applied(job_id, tenant_id, client=None) -> None: ...
```

The atomic decrement/increment (`shop_hold_stock_if_available`, `shop_apply_stock_in`) is a **Postgres RPC** (`create or replace function shop_hold_stock_if_available(...) returns setof shop_transactions ... security definer`) shipped in the migration, exactly as the diary's `hold_booking_if_capacity` / `hold_fleet_booking` RPCs are — the capacity decision must be atomic in the DB, never in Python (`src/app/scheduling.py` docstring: "Capacity is enforced in the DB … Two racing emails cannot overbook; this layer can only be too cautious").

---

## 6. Engine / automation

**Scheduler jobs** (registered in `src/app/main.py` alongside the existing loops, using `src/app/scheduler.py` `run_daily` / `run_every` — no external crons, per the house convention):

- **`src/app/jobs/send_best_before.py`** — `run_daily(hour=7, minute=0, …)`. For each tenant with the `storefront` module active (`db.tenants_with_modules(("storefront",))`, the pattern `send_service_reminders.py` uses), find products inside their best-before lead window (`db.shop_products_expiring`), and for each fire the best-before drafter (§12.2). This is the exact shape of `send_service_reminders.run()`: iterate tenants, per-item try/except so one bad draft never stops the run, `compose_outbound` → `insert_email_log(status='pending')` → optional auto-send for `cfg['auto_send']` tenants → `log_agent_event` + `usage.record_anthropic`.

- **`src/app/jobs/ready_reminders.py`** — `run_daily`. Reservations whose `ready_date == today` and still `reserved` → draft the "ready to collect" nudge (transition reserved→ready, §3). Same persist/send pattern.

- **OCR ingest is event-driven, not scheduled** — a photo arriving as an email attachment is caught in the inbound pipeline (§12.1); a photo uploaded in the UI hits the OCR route directly. Both land a `shop_ocr_jobs` row and run the extraction inline.

**Drafter integration.** Two paths, both already built:
- *Reactive* (customer emailed in): the inbound pipeline passes Storefront tools + a `storefront_context` string into `agent.draft_reply` (the same seam `crm_context` uses today, `src/app/agent.py:draft_reply`). Tools: `check_stock` (read `shop_products` for named items) and `reserve_stock` (call `shop_hold_stock_if_available`) — defined and injected only when the module is active, identical to how `SCHEDULING_TOOLS`/`FLEET_TOOLS` are conditionally injected in `src/app/scheduling.py`. The tool result feeds the reply; the drafter states real availability, never invents it.
- *Proactive* (BLAM8 reaches out): best-before and ready nudges use `agent.compose_outbound(cfg, purpose, facts)` — the shop's voice, facts-only, no invention (see `build_outbound_prompt` in `src/app/agent.py`).

**LLM extraction** reuses the whole `src/app/crm_fields.py` + `src/app/crm_extract.py` confidence-gated pipeline for two jobs: (a) tenant custom product fields (`shop_products.custom_data`) filled from supplier emails / OCR text, gated at 0.75 autofill / 0.5 suggest / else skip (`crm_fields.gate`); (b) the OCR line extraction itself (§12.1) with a vision call. Model: Haiku 4.5 for text (`settings.anthropic_model`), Sonnet (`settings.anthropic_booking_model`) only for the reserve-capable reply loop that must obey the "hold before you reply" mandate — same split as the diary.

**Sequences / promise reuse.** The best-before "3-touch clearance" and "collection nudge" are seeded `crm_sequences` (§9) — the engine (`src/app/crm_sequences.py:process_sequence_queue`, already on the 15-min `run_every` loop) enrolls the past-buyer contacts and fires each step through `compose_outbound` with `exit_on_reply`. No new execution loop. The CRM **promise** tracker also reuses cleanly: "I'll hold those two loaves till Saturday" extracted from an outbound reply is a promise against the reservation, auto-resolved when the transaction completes (`docs/architecture/CRM_design.md` §18.4 mechanics, unchanged).

---

## 7. API + routes

Two routers, styled on `src/app/crm_routes.py` (JSON API under `/api`, `Depends(require_onboarded)` → `TenantCtx`, `Depends(require_csrf)` on writes, `ctx.tenant_id` + `client=ctx.client` on every `db.shop_*` call) and the page routes under `/app` returning `templates.TemplateResponse(..., _ctx("shop", ctx, ...))`.

```
# ── pages (src/app/dashboard.py-style, server-rendered) ──────────────────────────
GET   /app/shop                      — the daily-rhythm surface (§2)
GET   /app/shop/products             — full catalogue (search, low-stock, expiring filters)
GET   /app/shop/products/{id}        — product detail: stock ledger, past buyers, custom fields
GET   /app/shop/transactions         — order/reservation list (filter by status)
GET   /app/shop/transactions/{id}    — one order: line items, customer, timeline
GET   /app/shop/stock-in             — the OCR intake page (camera / upload)

# ── JSON API (src/app/crm_routes.py-style, prefix /api/shop) ─────────────────────
GET   /api/shop/products                    — list (q, low_stock, expiring, limit, offset)
POST  /api/shop/products                    — manual add
PATCH /api/shop/products/{id}               — edit price / threshold / best-before
POST  /api/shop/stock-in                    — apply a confirmed stock-in (manual or reviewed OCR)
POST  /api/shop/ocr                          — upload a photo → creates job, runs extraction, returns lines
POST  /api/shop/ocr/{job_id}/apply          — commit reviewed OCR lines to stock
GET   /api/shop/transactions                — list (status, ready_by, contact_id)
POST  /api/shop/transactions                — create (walk-in counter entry)
POST  /api/shop/transactions/{id}/reserve   — reserve stock (atomic RPC)
POST  /api/shop/transactions/{id}/complete  — mark collected → drafts receipt
POST  /api/shop/transactions/{id}/cancel    — release stock + draft decline
POST  /api/shop/products/{id}/draft-specials — fire the best-before drafter for this batch now
GET   /api/shop/collections/today           — the Today card feed
```

Every route is IDOR-safe by construction: the `db.shop_*` functions filter on `.eq("tenant_id", ctx.tenant_id)` and run under the tenant's RLS client, exactly as the `crm_*` functions do.

---

## 8. UI

Server-rendered Jinja2, matching the existing app design system (`src/app/templates/app/*.html`, `_ctx()` helper injecting `active` + module flags). New templates:

- `templates/app/shop_home.html` — the three-panel daily surface (§2). Panel 3's **Photograph delivery** is the hero CTA.
- `templates/app/shop_products.html` + `shop_product_detail.html` — catalogue list + detail (stock ledger, past-buyer count, custom fields rendered via `crm_fields.display_value`).
- `templates/app/shop_transactions.html` + `shop_transaction_detail.html` — orders.
- `templates/app/shop_stock_in.html` — camera/upload + a review table of extracted lines with per-line confidence badges (green ≥0.75 auto-checked, amber 0.5–0.75 needs a glance, greyed <0.5), a one-tap **Apply to stock**. This is the "self-fills" moment on screen.

**Nav / module hub.** One new entry in `src/app/dashboard.py` `MODULES` (§10) lights the `/app/shop` card. The Today card on `/app` home reuses the CRM-card slot pattern in `app_home`. Charts (stock-value-at-risk, weekly sales) reuse the Chart.js wrapper Phase 4a added (`static/js/crm_charts.js`).

---

## 9. Onboarding + industry seeds

**INDUSTRIES entries** (`src/app/onboarding.py`) — unhide/add the Storefront sectors, each tagged `"os": "storefront"`, `"live": True` once this ships (until then `"live": False, "hidden": True`, per the release policy comment in that file):

```python
{"id": "farm_shop",  "label": "Farm shop & grocer", "os": "storefront",
 "blurb": "Stock, orders, collections — photograph the delivery, the shop knows.", "live": True},
{"id": "deli",       "label": "Deli & butcher",     "os": "storefront",
 "blurb": "Counter orders, reservations, best-before specials to your regulars.", "live": True},
{"id": "bakery",     "label": "Bakery & cafe",      "os": "storefront",
 "blurb": "Pre-orders, collections and end-of-day clearance to past buyers.", "live": True},
{"id": "wine_merchant", "label": "Wine & artisan retail", "os": "storefront",
 "blurb": "Stock by vintage/line, account customers, personalised offers.", "live": True},
{"id": "nursery",    "label": "Nursery & garden centre", "os": "storefront",
 "blurb": "Seasonal stock, reservations, collection nudges.", "live": True},
```

`src/app/operating_systems.py`: flip the `storefront` entry's `"live": False` → `True` (line ~21), so `is_live("storefront")` returns true and the Shape grid + `/onboarding/industry?shape=storefront` start routing new tenants here. Add these ids to `INDUSTRY_TO_OS` so a stored industry derives the OS (and the §6 backfill maps them).

**`industry_lingo.py`** — a `CRM_SEEDS`-shaped block per Storefront industry (the customer is a CRM contact, so these seed the *contact/company* custom fields), plus a `LINGO` vocabulary seed so the drafter speaks shop, not sales. Example (farm shop):

```python
"farm_shop": {
    "company":  ({"label": "Account type", "field_type": "dropdown",
                  "options": ["Retail", "Trade/wholesale", "Restaurant"],
                  "extraction_hint": "whether they buy retail or on a trade account"},),
    "contact":  ({"label": "Regular for", "field_type": "text",
                  "extraction_hint": "products they buy repeatedly — bread, veg box, cheese"},
                 {"label": "Collection day", "field_type": "text",
                  "extraction_hint": "the day they usually collect"}),
    "deal":     (),   # Storefront tracks transactions, not deals — deal seeds stay empty
}
```

A parallel **`SHOP_SEEDS`** dict seeds default *products* per industry (a starter catalogue the OCR then grows) and the best-before lead defaults (a bakery's is 0–1 days; a wine merchant's effectively none). Loaded in the onboarding "confirm your catalogue" sub-step (extends the existing custom-field wizard step).

**DATA_SOURCES** (`src/app/onboarding.py`) — a Storefront block:

```python
"farm_shop": [
    {"kind": "pos", "label": "Existing till / POS",
     "hint": "Square, Shopify, Vend — we sync stock both ways.",
     "vendors": ["Square", "Shopify", "Vend", "None yet"]},
    {"kind": "spreadsheet", "label": "Stock spreadsheet",
     "hint": "A sheet you track stock on today.", "vendors": []},
    {"kind": "whatsapp", "label": "WhatsApp orders",
     "hint": "Where customers message their orders.", "vendors": []},
],
```

"None yet" is the wedge signal — a shop with no POS is the target, and it onboards straight into OCR-first stock.

---

## 10. Module registration + plan wiring

**`src/app/dashboard.py` `MODULES`** — one new entry (the `tms`/`fsm` add-on shape):

```python
{"id": "storefront", "name": "Storefront OS", "group": "addon", "badge": "Shops",
 "href": "/app/shop",
 "blurb": "The whole shop off your inbox: stock that fills itself in from a photo of the "
          "delivery note, reservations and collections, and best-before specials pushed to "
          "the regulars who buy that product — no POS, no typing."},
```

It lights when a `subscriptions` row `(module='storefront', status='active')` exists — same switchboard as every add-on, no code path change to flip it on (`db.active_modules`).

**`src/app/plans.py`.** Storefront is a paid **industry module**, not Core (Core = email + scheduling + CRM). Add it to the entitlement map like `tms`/`fsm`:

```python
INDUSTRY_MODULE_META = {
  ...,
  "farm_shop": {"key": "storefront", "label": "Storefront OS", "name": "Storefront OS",
                "features": ["OCR-inferred stock (photo → inventory)", "Reservations & collections",
                             "Best-before specials to past buyers", "Stock levels & reorder alerts",
                             "POS sync (Square/Shopify)"]},
  # deli / bakery / wine_merchant / nursery all map to the same "storefront" key
}
```

so `INDUSTRY_MODULE` derives `farm_shop → storefront` (etc.) and `MANAGED_MODULES` gains `storefront`. The `+industry` tier (`professional`) becomes "Core + Storefront OS" for these tenants via the existing `billing_plans(industry=...)` machinery — no billing code changes.

**Pricing tier suggestion.** Slot Storefront OS at the **£299 `professional`** tier ("Core + Storefront OS"), consistent with Field OS · Transport and the doc's §9 recommendation (Core covers CRM/drafter/calendar; each OS's module is a £150–300 add-on). Rationale: the OCR pipeline + best-before drafter carry real per-tenant LLM cost (still <£5/mo/tenant, §12) and the target shops trade daily volume — £299 sits comfortably under a part-time counter hire, the real alternative.

---

## 11. OS-native integrations

Per §4.2, three integration families. What wires in for v1 vs later:

| Integration | Role | v1 or later | How it wires in |
|---|---|---|---|
| **OCR pipeline** (Claude vision) | photo → stock (the killer feature) | **v1** | `app/jobs`/route calls a vision `messages.create` (§12.1); no third party — Anthropic only. Reuses `usage.record_anthropic` metering. |
| **WhatsApp Business** | customer orders in the channel shops actually use | **v1-lite / later-full** | The drafter is transport-agnostic (`docs/architecture/CRM_design.md` §23). v1: capture WhatsApp orders as `channel='whatsapp'` transactions via the same `compose_outbound`/extraction path once the WhatsApp channel module (CRM Phase 5.2) lands; until then, walk-in + email cover the flow. Storefront is a *reason to prioritise* the WhatsApp channel, not a blocker. |
| **Square / Shopify / Vend POS sync** | shops that already have a till | **later** | Read-only stock pull first (their catalogue → `shop_products`, marked `source='pos'`), write-back second. Deferred because the wedge is explicitly the **no-POS** shop — POS sync is the *expansion* motion, not the wedge. Design the `shop_products.custom_data._pos` namespace now (like CRM's `_enrichment` block, `docs/architecture/CRM_design.md` §26) so sync has a home. |

v1 ships OCR + email/walk-in transactions + best-before drafter. WhatsApp rides in with the shared channel module. POS sync is a fast-follow once a design-partner shop with an existing till asks for it.

---

## 12. Killer feature deep-dive

### 12.1 OCR-inferred inventory (photo → stock, zero typing)

**Two entry points, one pipeline.** (a) A photo arrives as an **email attachment** — the inbound pipeline detects an image part, and if the tenant has `storefront` active, spawns a `shop_ocr_jobs` row + runs extraction (the attachment handling seam already exists for PODs). (b) The owner **photographs at the counter** via `/app/shop/stock-in` → `POST /api/shop/ocr`.

**The extraction call.** A single Claude **vision** `messages.create` (Sonnet — `settings.anthropic_booking_model` — for reliable table reading; fall back to Haiku for cost if accuracy holds in testing), image as a base64 `source`, structured JSON output (`output_config` json_schema, the same mechanism `draft_reply`/`compose_outbound` use). Prompt:

```
You are reading a UK shop's {kind} (a delivery note, supplier invoice, shelf, or
chalkboard). Extract every product line you can see. For each line return:
  name          — the product as written (normalise obvious abbreviations)
  qty           — the quantity delivered (a number)
  unit          — each | kg | crate | box | bottle | loaf … (best guess from context)
  unit_price    — price per unit if shown, else null
  best_before   — a date in YYYY-MM-DD if the line or document states one, else null
  confidence    — 0.0-1.0, YOUR certainty this line is correct

Rules:
- Read ONLY what is on the document. Never invent a product, quantity, price or date.
- A blurred or ambiguous line gets LOW confidence, not a guess.
- Totals, VAT lines, addresses and headers are NOT products — skip them.
Return JSON: {"lines": [ … ], "document_note": "…"}.
```

**Confidence gating** reuses `src/app/crm_fields.py:gate` thresholds exactly (0.75 / 0.5): lines ≥0.75 are pre-checked in the review table and applied on one tap; 0.5–0.75 are shown amber, unchecked, for a glance; <0.5 are listed but not applied. **Nothing writes to stock without the owner's tap in v1** — the review step is the trust bridge (a mis-read "12" vs "72" on a Maris Piper crate matters). A tenant can later opt into auto-apply-above-0.9 once they trust it (config flag, mirrors `auto_send`).

**Applying.** On apply, `db.shop_apply_stock_in(tenant_id, lines, source="ocr", source_ref=job_id)`: each line is get-or-created against `lower(name)` (dedupe — a second delivery of "Sourdough" tops up the existing row, never forks it), `qty_on_hand` incremented via the atomic RPC, a `shop_stock_movements` row written (with `best_before_date` when present), and `shop_ocr_jobs.status='applied'`. The stock ledger makes every OCR change reversible and auditable.

**Failure modes + mitigations:**
- *Blurry / partial photo* → low-confidence lines stay unchecked; owner retakes. Never silently wrong.
- *Product name variance* ("King Edward potatoes" vs "K.Edward pot.") → dedupe is `lower(name)` exact in v1; a fuzzy-match pass (Levenshtein ≤2 / token-set overlap, the exact tactic in `docs/architecture/CRM_design.md` §18.1 freemail company matching) is a fast-follow to stop near-duplicate products.
- *Best-before absent on the note* → `best_before_date` null; the owner sets it once for the batch (or it's a non-perishable). The nudge simply doesn't fire for null-dated stock.
- *Hallucinated line* → the "read ONLY what's on the document" rule + the human review tap; a hallucinated line reads as low-value and gets unchecked.
- *Cost* → one vision call per delivery, ~£0.01–0.03 at Sonnet vision pricing, metered via `usage.record_anthropic`. A shop takes a handful of deliveries a week — negligible.

### 12.2 Best-before drafter (personalised specials to past buyers)

**The mechanic.** Daily at 07:00 (`send_best_before.py`), for each product inside its `sell_before_lead_days` window with `qty_on_hand > 0`:

1. `db.shop_past_buyers_of(tenant_id, product_id)` → the distinct CRM contacts with a **completed** `transaction_item` for this product. This is the personalisation, and it's free: it comes straight from the transaction history the daily counter flow already writes. No "email everyone" blast — only people who've bought *this* thing.
2. For each past buyer, build `facts` — the product, the discount (a tenant default like "20% off going-today stock", editable), the collection window, and the buyer's own history ("you usually grab a sourdough on Saturdays"). Compose via `agent.compose_outbound(cfg, purpose, facts)` in the shop's learned voice.

```
purpose = ("A warm, brief heads-up that a product this customer has bought before is "
           "in today and needs to go — invite them to grab it at the stated discount "
           "before close. Personal, not a mailshot. Do NOT invent stock, prices or "
           "dates beyond the facts.")
```

3. Queue to `email_log(status='pending', kind='special')` for approval, or auto-send for full-auto tenants — the `send_service_reminders.py` persist/send/`log_agent_event`/`usage.record_anthropic` pattern, unchanged.

**Scale / dedupe guardrails** (reuse the sequences guardrails, `docs/architecture/CRM_design.md` §19.3): max one special per contact per day even across multiple expiring products (queue collisions to the next slot); a contact who replies "stop"/"unsubscribe" gets `crm_contacts.do_not_sequence=true` (the column already exists) and is excluded; hard bounces exclude the address. For a **multi-product clearance**, enroll the audience in the seeded "3-touch clearance" `crm_sequence` instead of a one-shot, so the engine handles cadence + exit-on-reply.

**Confidence / correctness:** the drafter only ever states facts passed in; the past-buyer set is a deterministic SQL query, not an LLM guess, so the *targeting* can't hallucinate. The only LLM surface is the wording, which the approval queue gates for non-auto tenants.

**Failure modes:** a product with no past buyers → nothing drafts (the daily surface still flags it for a manual chalkboard special); a tiny shop where "past buyers" is the owner's whole mailing list → the per-contact/day cap + do-not-sequence keep it from feeling spammy; a mis-dated batch (best-before wrong) → the owner sees the nudge on the daily surface before it sends and can fix the date.

---

## 13. Build order

Each phase independently shippable; single dev + LLM assistance. Rough total 4–6 weeks (matches `docs/BLAM8_operating_systems.md` §8 Phase B estimate).

**Phase 1 — Primitive + catalogue + daily surface (~1.5 weeks).** Migration (`shop_products`, `shop_transactions`, `shop_transaction_items`, `shop_stock_movements` + RLS + atomic RPC), `db.shop_*` layer, `/app/shop` daily surface, manual product add/edit, manual transaction + reserve/complete via the atomic RPC. Module registration + plan wiring (§10). Onboarding INDUSTRIES/seeds + `is_live` flip (§9). **Ships a working shop-inventory app with the diary-grade atomic capacity guarantee — usable before any AI.**

**Phase 2 — Drafter integration (~1 week).** `check_stock` + `reserve_stock` tools into `draft_reply`; `storefront_context` injection; reactive enquiry→reserved flow off real inbound email; proactive ready-to-collect nudges (`ready_reminders.py`). **Ships "the shop that answers its own inbox."**

**Phase 3 — OCR-inferred inventory (~1.5 weeks).** The vision extraction pipeline, `shop_ocr_jobs`, the `/app/shop/stock-in` review UI with confidence gating, email-attachment ingest, `shop_apply_stock_in`. **Ships the wedge — "photo → stock, zero typing."**

**Phase 4 — Best-before drafter (~1 week).** `send_best_before.py` daily job, `shop_past_buyers_of`, the seeded clearance sequences, the daily-surface Panel 2 **Draft specials** action. **Ships the retention/margin killer feature.**

**Later (demand-gated):** WhatsApp channel (rides CRM Phase 5.2), POS sync (Square/Shopify), OCR auto-apply-above-0.9, fuzzy product dedupe.

---

## 14. Open questions + risks

- **Multi-OS tenant (Storefront + Field).** A farm shop that also runs veg-box **delivery rounds** is Storefront + Field OS. Recommendation (per doc §9): primary OS at onboarding, optional secondary later; the veg-box run is a `bookings` row on the diary (Field), the shop stock is `shop_products` (Storefront) — the two primitives don't collide, so this composes cleanly. Detailed UX deferred.
- **Stock accuracy without a till.** BLAM8 only knows about stock it saw come in (OCR/manual) and go out (reservations/completed transactions). A walk-in cash sale the owner doesn't record drifts the count. Mitigation: the daily surface makes recording a walk-in one tap, and a periodic "shelf photo → reconcile" (an OCR `kind='shelf'` job) resets counts. Accept that v1 is *directional* stock, not audited stock — still a step-change for a shop running on memory.
- **Best-before targeting for tiny shops.** Where "past buyers of X" is basically the whole list, personalisation blurs into a mailshot. The per-contact/day cap + do-not-sequence + owner approval keep it safe; watch open/complaint signals in the pilot.
- **OCR on chalkboards / handwriting** is materially harder than printed delivery notes. Ship printed-document confidence first; treat `kind='chalkboard'` as best-effort with a lower auto-check bar.
- **Weight/variable-unit products** (a butcher's "£12.50/kg", a 1.3kg joint) — `unit` + `unit_price` handle the common case; genuinely variable-weight per-transaction pricing may need a `qty × measured_weight` line-item extension. Defer until a butcher pilot needs it.
- **Reservation expiry.** A `reserved` transaction never collected holds stock forever. Add a config `reservation_hold_days` (default 3) → a daily sweep releases stale reservations back to `qty_on_hand` + drafts a "still want these?" nudge. Ship in Phase 2.

---

## 15. Reuse map

The staged-implementation driver: what's genuinely new vs. what BLAM8 already does.

| Part | REUSE (name it) or BUILD NEW |
|---|---|
| Customer identity | **REUSE** — `crm_contacts` / `crm_companies`, auto-populated from the inbox (`src/app/crm.py`) |
| Outbound in tenant voice (proactive) | **REUSE** — `agent.compose_outbound` (`src/app/agent.py`) |
| Reply in tenant voice (reactive, with tools) | **REUSE** — `agent.draft_reply` + conditional tool injection (`src/app/agent.py`, the `SCHEDULING_TOOLS`/`FLEET_TOOLS` pattern) |
| LLM field extraction + confidence gate | **REUSE** — `src/app/crm_fields.py` (`gate`, `coerce_value`, `apply_extraction`) + `crm_extract.py` |
| Follow-up cadence (clearance / collection nudges) | **REUSE** — `crm_sequences` engine, already on `run_every(900, …)` |
| Promise tracking (holds, "till Saturday") | **REUSE** — CRM promise tracker + auto-resolve (`docs/architecture/CRM_design.md` §18.4) |
| Atomic capacity guarantee | **REUSE the pattern** — model `shop_hold_stock_if_available` on `db.hold_booking_if_capacity` (new RPC, same discipline) |
| In-process scheduling | **REUSE** — `src/app/scheduler.py` `run_daily` / `run_every`; new jobs `send_best_before.py`, `ready_reminders.py` mirror `send_service_reminders.py` |
| Multi-tenant isolation | **REUSE** — RLS `for all using (tenant_id in (select auth_tenant_ids()))` on every new table |
| Module registry + gating | **REUSE** — `MODULES` entry + `subscriptions` switchboard (`src/app/dashboard.py`, `db.active_modules`) |
| Plan / billing wiring | **REUSE** — `INDUSTRY_MODULE_META` + `billing_plans(industry=…)` (`src/app/plans.py`) |
| OS layer + `is_live` flip | **REUSE** — `src/app/operating_systems.py` (flip `storefront.live`, extend `INDUSTRY_TO_OS`) |
| Onboarding (Shape→Industry, seeds, data sources) | **REUSE** — `src/app/onboarding.py` INDUSTRIES/DATA_SOURCES + `src/app/industry_lingo.py` CRM_SEEDS/LINGO |
| Migration convention | **REUSE** — idempotent psycopg2 script, shape of `scripts/db/migrations/apply_crm_phase4b_migration.py` |
| Usage metering | **REUSE** — `usage.record_anthropic` on every LLM call |
| **The `transaction` primitive** (`shop_transactions` + items) | **BUILD NEW** — no analogue; a basket of goods against decrementing inventory |
| **The `products` catalogue** | **BUILD NEW** — the field library as real columns |
| **`shop_stock_movements` ledger** | **BUILD NEW** — append-only stock audit (makes OCR trustworthy/reversible) |
| **OCR vision pipeline** (`shop_ocr_jobs` + vision call + review UI) | **BUILD NEW** — the wedge; a new vision `messages.create` + confidence-gated review |
| **Best-before drafter job** | **BUILD NEW** — new `send_best_before.py`, but assembled entirely from reused parts (past-buyer SQL + `compose_outbound` + `insert_email_log`) |
| **The `/app/shop` daily surface + templates** | **BUILD NEW** — three-panel view-model + Jinja2 pages |

The reuse map is the plan: ~70% of Storefront OS is configuration and glue over existing platform capabilities; the genuinely new engineering is the two primitives, the stock ledger, and the OCR pipeline — which is exactly the "one primitive + a home screen + a few workflows + OS-native integrations" bounded-build the OS architecture promises (`docs/BLAM8_operating_systems.md` §2).
