# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply the Storefront OS schema (docs/os/storefront.md §4) — the
catalogue, the transaction primitive + line items, the append-only stock ledger,
the OCR-jobs table, and the two ATOMIC stock RPCs (the diary's
hold_booking_if_capacity siblings). The full §4 set ships now so later phases
(2 drafter / 3 OCR / 4 best-before) need no new migration.

Safe to re-run (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE FUNCTION)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- ── The catalogue (the field library, §1) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS shop_products (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  sku                   text,
  name                  text NOT NULL,
  unit                  text DEFAULT 'each',
  unit_price            numeric,
  qty_on_hand           numeric NOT NULL DEFAULT 0,
  qty_reserved          numeric NOT NULL DEFAULT 0,
  best_before_date      date,
  supplier              text,
  sell_before_lead_days integer DEFAULT 2,
  reorder_threshold     numeric,
  active                boolean NOT NULL DEFAULT true,
  custom_data           jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
-- One row per named product per tenant (dedupes OCR). A functional unique key must be a
-- unique INDEX, not a table-level UNIQUE constraint (Postgres rejects lower(name) there).
CREATE UNIQUE INDEX IF NOT EXISTS shop_products_name_uniq ON shop_products (tenant_id, lower(name));
CREATE INDEX IF NOT EXISTS shop_products_tenant_idx     ON shop_products (tenant_id);
CREATE INDEX IF NOT EXISTS shop_products_bestbefore_idx ON shop_products (tenant_id, best_before_date)
  WHERE best_before_date IS NOT NULL AND qty_on_hand > 0;
CREATE INDEX IF NOT EXISTS shop_products_reorder_idx    ON shop_products (tenant_id, active);

-- ── The primitive: transactions + their line items ──────────────────────────
CREATE TABLE IF NOT EXISTS shop_transactions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact_id     uuid REFERENCES crm_contacts(id) ON DELETE SET NULL,
  company_id     uuid REFERENCES crm_companies(id) ON DELETE SET NULL,
  status         text NOT NULL DEFAULT 'enquiry',
  channel        text DEFAULT 'email',
  payment_method text,
  total_amount   numeric,
  currency       text NOT NULL DEFAULT 'GBP',
  ready_date     date,
  thread_id      text,
  notes          text,
  custom_data    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shop_tx_tenant_status_idx ON shop_transactions (tenant_id, status, ready_date);
CREATE INDEX IF NOT EXISTS shop_tx_contact_idx       ON shop_transactions (tenant_id, contact_id);

CREATE TABLE IF NOT EXISTS shop_transaction_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  transaction_id uuid NOT NULL REFERENCES shop_transactions(id) ON DELETE CASCADE,
  product_id     uuid REFERENCES shop_products(id) ON DELETE SET NULL,
  product_name   text NOT NULL,
  qty            numeric NOT NULL DEFAULT 1,
  unit_price     numeric,
  line_total     numeric,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shop_tx_items_tx_idx      ON shop_transaction_items (tenant_id, transaction_id);
CREATE INDEX IF NOT EXISTS shop_tx_items_product_idx ON shop_transaction_items (tenant_id, product_id);

-- ── The stock ledger: every change to on-hand, append-only (audit + OCR trust) ─
CREATE TABLE IF NOT EXISTS shop_stock_movements (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  product_id       uuid NOT NULL REFERENCES shop_products(id) ON DELETE CASCADE,
  delta            numeric NOT NULL,
  reason           text NOT NULL,
  source           text,
  source_ref       text,
  best_before_date date,
  note             text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shop_stock_mov_product_idx ON shop_stock_movements (tenant_id, product_id, created_at DESC);

-- ── OCR extraction jobs (Phase 3+4 — table ships now so no later migration) ───
CREATE TABLE IF NOT EXISTS shop_ocr_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind          text NOT NULL DEFAULT 'delivery_note',
  image_path    text,
  status        text NOT NULL DEFAULT 'pending',
  extracted     jsonb,
  applied_at    timestamptz,
  email_log_id  text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shop_ocr_jobs_tenant_idx ON shop_ocr_jobs (tenant_id, status, created_at DESC);

-- ── RLS: the house predicate on every table (mirrors every crm_* table) ───────
ALTER TABLE shop_products ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - shop_products" ON shop_products;
CREATE POLICY "tenant isolation - shop_products" ON shop_products
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE shop_transactions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - shop_transactions" ON shop_transactions;
CREATE POLICY "tenant isolation - shop_transactions" ON shop_transactions
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE shop_transaction_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - shop_transaction_items" ON shop_transaction_items;
CREATE POLICY "tenant isolation - shop_transaction_items" ON shop_transaction_items
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE shop_stock_movements ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - shop_stock_movements" ON shop_stock_movements;
CREATE POLICY "tenant isolation - shop_stock_movements" ON shop_stock_movements
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE shop_ocr_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - shop_ocr_jobs" ON shop_ocr_jobs;
CREATE POLICY "tenant isolation - shop_ocr_jobs" ON shop_ocr_jobs
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

-- ── The ONE atomic reservation decision (the shop sibling of ────────────────
-- hold_booking_if_capacity / hold_fleet_booking, §5). A product row-lock
-- (FOR UPDATE) serialises racing holds so two concurrent emails can never
-- over-reserve. Reserves p_qty IFF (qty_on_hand - qty_reserved) >= p_qty:
-- bumps qty_reserved, writes a 'reserve' stock_movement, creates-or-updates the
-- transaction + its line item. Returns the transaction row; an EMPTY result
-- means out_of_stock (a clean signal — no exception for PostgREST to wrap).
CREATE OR REPLACE FUNCTION shop_hold_stock_if_available (
  p_tenant_id      uuid,
  p_product_id     uuid,
  p_qty            numeric,
  p_transaction_id uuid    DEFAULT NULL,
  p_contact_id     uuid    DEFAULT NULL,
  p_company_id     uuid    DEFAULT NULL,
  p_ready_date     date    DEFAULT NULL,
  p_channel        text    DEFAULT 'email',
  p_thread_id      text    DEFAULT NULL,
  p_source         text    DEFAULT 'agent'
)
RETURNS SETOF shop_transactions
LANGUAGE plpgsql VOLATILE
SET search_path = public
AS $$
DECLARE
  v_prod    shop_products%ROWTYPE;
  v_avail   numeric;
  v_tx      shop_transactions%ROWTYPE;
  v_line    numeric;
BEGIN
  IF p_qty IS NULL OR p_qty <= 0 THEN
    RAISE EXCEPTION 'invalid qty: %', p_qty;
  END IF;

  -- Row-lock the product so concurrent reservations serialise on THIS row
  -- (the shop's unit of capacity is a product's available quantity, not a date).
  SELECT * INTO v_prod
  FROM shop_products
  WHERE id = p_product_id AND tenant_id = p_tenant_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN;   -- unknown product → out_of_stock (empty result)
  END IF;

  v_avail := coalesce(v_prod.qty_on_hand, 0) - coalesce(v_prod.qty_reserved, 0);
  IF v_avail < p_qty THEN
    RETURN;   -- not enough available — empty result set = out_of_stock
  END IF;

  -- Reserve: hold against on-hand (available = on_hand - reserved).
  UPDATE shop_products
    SET qty_reserved = coalesce(qty_reserved, 0) + p_qty,
        updated_at   = now()
    WHERE id = p_product_id AND tenant_id = p_tenant_id;

  v_line := round(coalesce(v_prod.unit_price, 0) * p_qty, 2);

  -- Create or update the transaction (the reservation lives in the tx lifecycle
  -- as status='reserved', §1/§3).
  IF p_transaction_id IS NULL THEN
    INSERT INTO shop_transactions
      (tenant_id, contact_id, company_id, status, channel, ready_date,
       thread_id, total_amount)
    VALUES
      (p_tenant_id, p_contact_id, p_company_id, 'reserved', p_channel, p_ready_date,
       p_thread_id, v_line)
    RETURNING * INTO v_tx;
  ELSE
    SELECT * INTO v_tx
    FROM shop_transactions
    WHERE id = p_transaction_id AND tenant_id = p_tenant_id
    FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'unknown transaction: %', p_transaction_id;
    END IF;
    UPDATE shop_transactions
      SET status       = 'reserved',
          ready_date   = coalesce(p_ready_date, ready_date),
          total_amount = coalesce(total_amount, 0) + v_line,
          updated_at   = now()
      WHERE id = p_transaction_id AND tenant_id = p_tenant_id
      RETURNING * INTO v_tx;
  END IF;

  INSERT INTO shop_transaction_items
    (tenant_id, transaction_id, product_id, product_name, qty, unit_price, line_total)
  VALUES
    (p_tenant_id, v_tx.id, p_product_id, v_prod.name, p_qty, v_prod.unit_price, v_line);

  INSERT INTO shop_stock_movements
    (tenant_id, product_id, delta, reason, source, source_ref)
  VALUES
    (p_tenant_id, p_product_id, -p_qty, 'reserve', p_source, v_tx.id::text);

  RETURN NEXT v_tx;
END $$;

-- ── Atomic stock-in increment (the OCR/manual delivery path, §5) ─────────────
-- Adds p_delta to qty_on_hand under a row-lock and appends a 'stock_in'
-- movement, so a delivery top-up is as atomic as a reservation. Called per
-- product line by db.shop_apply_stock_in.
CREATE OR REPLACE FUNCTION shop_apply_stock_in_one (
  p_tenant_id       uuid,
  p_product_id      uuid,
  p_delta           numeric,
  p_best_before     date    DEFAULT NULL,
  p_source          text    DEFAULT 'manual',
  p_source_ref      text    DEFAULT NULL,
  p_note            text    DEFAULT NULL
)
RETURNS SETOF shop_products
LANGUAGE plpgsql VOLATILE
SET search_path = public
AS $$
DECLARE
  v_prod shop_products%ROWTYPE;
BEGIN
  IF p_delta IS NULL OR p_delta = 0 THEN
    RAISE EXCEPTION 'invalid stock-in delta: %', p_delta;
  END IF;

  SELECT * INTO v_prod
  FROM shop_products
  WHERE id = p_product_id AND tenant_id = p_tenant_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown product: %', p_product_id;
  END IF;

  UPDATE shop_products
    SET qty_on_hand      = coalesce(qty_on_hand, 0) + p_delta,
        best_before_date = coalesce(p_best_before, best_before_date),
        updated_at       = now()
    WHERE id = p_product_id AND tenant_id = p_tenant_id
    RETURNING * INTO v_prod;

  INSERT INTO shop_stock_movements
    (tenant_id, product_id, delta, reason, source, source_ref, best_before_date, note)
  VALUES
    (p_tenant_id, p_product_id, p_delta, 'stock_in', p_source, p_source_ref, p_best_before, p_note);

  RETURN NEXT v_prod;
END $$;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"Storefront OS migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify — every §4 table exists…
for table in ("shop_products", "shop_transactions", "shop_transaction_items",
              "shop_stock_movements", "shop_ocr_jobs"):
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s;", (table,))
    assert cur.fetchone(), f"{table} table missing"

# …the unique (tenant_id, lower(name)) index on products…
cur.execute("""
  SELECT indexname FROM pg_indexes
  WHERE tablename = 'shop_products' AND indexdef ILIKE '%lower(name)%';
""")
assert cur.fetchone(), "unique (tenant_id, lower(name)) index missing on shop_products"

# …and both atomic RPCs.
for fn in ("shop_hold_stock_if_available", "shop_apply_stock_in_one"):
    cur.execute("SELECT proname FROM pg_proc WHERE proname = %s;", (fn,))
    assert cur.fetchone(), f"{fn} function missing"

print("Storefront schema verified OK")
cur.close()
conn.close()
