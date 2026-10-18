# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply Phase 3.5 CRM schema additions — probe_behaviour column,
crm_probe_log table, and promise reinforcement/resolution columns.
Safe to re-run (DDL is guarded with IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Phase 3.5 §18.2: probe_behaviour on field definitions
ALTER TABLE crm_field_definitions
  ADD COLUMN IF NOT EXISTS probe_behaviour text NOT NULL DEFAULT 'passive';

-- Phase 3.5 §18.2: probe attempt tracking
CREATE TABLE IF NOT EXISTS crm_probe_log (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact_id          uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  field_definition_id uuid NOT NULL REFERENCES crm_field_definitions(id) ON DELETE CASCADE,
  attempt_number      integer NOT NULL DEFAULT 1,
  draft_id            uuid,
  resolved            boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS crm_probe_log_contact_idx
  ON crm_probe_log (tenant_id, contact_id, field_definition_id);

ALTER TABLE crm_probe_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_probe_log" ON crm_probe_log;
CREATE POLICY "tenant isolation - crm_probe_log" ON crm_probe_log
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

-- Phase 3.5 §18.3: promise reinforcement columns
ALTER TABLE crm_promises
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz DEFAULT now(),
  ADD COLUMN IF NOT EXISTS mention_count integer NOT NULL DEFAULT 1;

-- Phase 3.5 §18.4: promise resolution tracking
ALTER TABLE crm_promises
  ADD COLUMN IF NOT EXISTS resolution_type text,
  ADD COLUMN IF NOT EXISTS resolution_evidence text;

-- Phase 3.5 §18.1: name-based company lookup index for fuzzy matching
CREATE INDEX IF NOT EXISTS idx_crm_companies_name_tenant
  ON crm_companies (tenant_id, lower(name));
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"CRM Phase 3.5 migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify
cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_field_definitions' AND column_name = 'probe_behaviour';
""")
assert cur.fetchone(), "probe_behaviour column missing"

cur.execute("""
  SELECT table_name FROM information_schema.tables
  WHERE table_name = 'crm_probe_log';
""")
assert cur.fetchone(), "crm_probe_log table missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_promises' AND column_name = 'mention_count';
""")
assert cur.fetchone(), "mention_count column missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_promises' AND column_name = 'resolution_type';
""")
assert cur.fetchone(), "resolution_type column missing"

print("All Phase 3.5 schema objects verified OK")
cur.close()
conn.close()
