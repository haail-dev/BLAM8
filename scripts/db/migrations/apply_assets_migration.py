# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply the Assets OS schema (docs/os/assets.md §4) — the FULL set now,
so the later phases (deposits, condition photo-diffs, damage cases) need no new
migration. Only Phase A (asset_units + bookings.unit_id/hire_state) has an engine
behind it today; the deposit/condition/damage tables are created additively and
their engines ship in Phases B/C.

Safe to re-run (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS /
CREATE UNIQUE INDEX IF NOT EXISTS — never a table-level unique(lower(...))
constraint, which Postgres rejects; the Storefront migration was bitten by that)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- ── Assets OS §4: the durable supply object (the `vehicles` of Assets OS) ──────
CREATE TABLE IF NOT EXISTS asset_units (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  asset_ref         text,                        -- tenant's own asset ID / reg / plate
  name              text NOT NULL,               -- "Rose Cottage", "Digger JCB-3"
  category          text,                        -- cottage|digger|marquee|car|kayak|tool|…
  resource_type     text,                        -- capacity pool key; NULL = its own named unit
  -- rate card (independent of the diary; a per-unit override of the tenant stay_pricing default)
  rate_daily        numeric,
  rate_weekly       numeric,
  rate_monthly      numeric,
  deposit_default   numeric,                     -- deposit taken to hold this unit
  -- condition + service
  condition_rating  numeric(2,1) DEFAULT 5.0,    -- 0.0–5.0, the CURRENT running condition
  condition_floor   numeric(2,1) DEFAULT 3.0,    -- below this → maintenance queue
  next_service_due  date,
  service_interval_days integer,                 -- for auto-advancing next_service_due
  active            boolean NOT NULL DEFAULT true,
  custom_data       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- seam, like companies.custom_data
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_asset_units_tenant       ON asset_units (tenant_id, active);
CREATE INDEX IF NOT EXISTS idx_asset_units_tenant_type  ON asset_units (tenant_id, resource_type);
CREATE INDEX IF NOT EXISTS idx_asset_units_service_due  ON asset_units (tenant_id, next_service_due)
  WHERE next_service_due IS NOT NULL;

-- Link a booking to the specific unit it hires. Mirrors bookings.vehicle_id exactly.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS unit_id    uuid REFERENCES asset_units(id) ON DELETE SET NULL;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS hire_state text;   -- held|reserved|out|returned (NULL = not a hire)
CREATE INDEX IF NOT EXISTS idx_bookings_unit ON bookings (tenant_id, unit_id) WHERE unit_id IS NOT NULL;

-- ── Phase C (deferred engine): one condition snapshot, at handover (out) or return (in). ──
-- Photos live in Supabase Storage; we store their URLs (as shipment_events.photo_url does).
CREATE TABLE IF NOT EXISTS condition_reports (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  unit_id       uuid NOT NULL REFERENCES asset_units(id) ON DELETE CASCADE,
  booking_id    uuid REFERENCES bookings(id) ON DELETE SET NULL,
  phase         text NOT NULL,                   -- 'out' (handover) | 'in' (return)
  rating        numeric(2,1),                    -- 0.0–5.0 assessed at this snapshot
  photo_urls    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- ordered list of Storage URLs
  notes         text,
  captured_by   text,                            -- caretaker/staff name (free text)
  captured_at   timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_condition_reports_unit    ON condition_reports (tenant_id, unit_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_condition_reports_booking ON condition_reports (tenant_id, booking_id);

-- ── Phase C (deferred engine): a damage finding for one hire — the evidence bundle. ──
CREATE TABLE IF NOT EXISTS damage_cases (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  unit_id            uuid NOT NULL REFERENCES asset_units(id) ON DELETE CASCADE,
  booking_id         uuid REFERENCES bookings(id) ON DELETE SET NULL,
  out_report_id      uuid REFERENCES condition_reports(id) ON DELETE SET NULL,
  in_report_id       uuid REFERENCES condition_reports(id) ON DELETE SET NULL,
  description        text,                        -- LLM or human summary of the damage
  diff_findings      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{area, change, severity, confidence, out_url, in_url}]
  diff_confidence    numeric,                     -- overall model confidence in the comparison
  severity           text,                        -- none|minor|moderate|major
  proposed_deduction numeric,
  status             text NOT NULL DEFAULT 'open',   -- open|disputed|resolved|dismissed
  resolution         text,                        -- deducted|refunded|waived|goodwill
  resolved_amount    numeric,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_damage_cases_open ON damage_cases (tenant_id, status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_damage_cases_unit ON damage_cases (tenant_id, unit_id);

-- ── Phase B (deferred engine): deposit lifecycle over the existing Stripe rail. ──
CREATE TABLE IF NOT EXISTS unit_deposits (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  booking_id      uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  unit_id         uuid REFERENCES asset_units(id) ON DELETE SET NULL,
  amount          numeric NOT NULL,
  currency        text NOT NULL DEFAULT 'GBP',
  status          text NOT NULL DEFAULT 'pending', -- pending|held|deducted|refunded|failed|released
  method          text,                            -- stripe_hold | stripe_charge | manual
  external_ref    text,                            -- Stripe PaymentIntent / SetupIntent id
  deducted_amount numeric DEFAULT 0,
  damage_case_id  uuid REFERENCES damage_cases(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
-- Functional/plain unique keys are a CREATE UNIQUE INDEX, never a table-level
-- unique(...) constraint (Postgres rejects unique(lower(...)); Storefront was bitten).
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_deposits_booking ON unit_deposits (booking_id);
CREATE INDEX IF NOT EXISTS idx_unit_deposits_status ON unit_deposits (tenant_id, status);

-- ── RLS on every new table (idempotent), verbatim from the Phase 4b pattern ────
ALTER TABLE asset_units ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - asset_units" ON asset_units;
CREATE POLICY "tenant isolation - asset_units" ON asset_units
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE condition_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - condition_reports" ON condition_reports;
CREATE POLICY "tenant isolation - condition_reports" ON condition_reports
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE damage_cases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - damage_cases" ON damage_cases;
CREATE POLICY "tenant isolation - damage_cases" ON damage_cases
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE unit_deposits ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - unit_deposits" ON unit_deposits;
CREATE POLICY "tenant isolation - unit_deposits" ON unit_deposits
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"Assets OS migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify — every new table + the bookings.unit_id column.
for table in ("asset_units", "condition_reports", "damage_cases", "unit_deposits"):
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s;", (table,))
    assert cur.fetchone(), f"{table} table missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'bookings' AND column_name = 'unit_id';
""")
assert cur.fetchone(), "unit_id column missing on bookings"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'bookings' AND column_name = 'hire_state';
""")
assert cur.fetchone(), "hire_state column missing on bookings"

print("Assets OS schema verified OK")
cur.close()
conn.close()
