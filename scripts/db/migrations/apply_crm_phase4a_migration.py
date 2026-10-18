# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply Phase 4a CRM schema — crm_tasks table.
Safe to re-run (CREATE TABLE IF NOT EXISTS)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Phase 4a §19.2: CRM task management
CREATE TABLE IF NOT EXISTS crm_tasks (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  title        text NOT NULL,
  description  text,
  due_date     date,
  priority     text NOT NULL DEFAULT 'normal',
  status       text NOT NULL DEFAULT 'open',
  assignee_id  uuid,
  contact_id   uuid REFERENCES crm_contacts(id) ON DELETE SET NULL,
  company_id   uuid REFERENCES crm_companies(id) ON DELETE SET NULL,
  deal_id      uuid REFERENCES crm_deals(id) ON DELETE SET NULL,
  promise_id   uuid REFERENCES crm_promises(id) ON DELETE SET NULL,
  source       text NOT NULL DEFAULT 'manual',
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crm_tasks_tenant_status
  ON crm_tasks (tenant_id, status, due_date);

ALTER TABLE crm_tasks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_tasks" ON crm_tasks;
CREATE POLICY "tenant isolation - crm_tasks" ON crm_tasks
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

print(f"CRM Phase 4a migration: {len(MIGRATION_SQL)} chars")

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
  SELECT table_name FROM information_schema.tables
  WHERE table_name = 'crm_tasks';
""")
assert cur.fetchone(), "crm_tasks table missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_tasks' AND column_name = 'promise_id';
""")
assert cur.fetchone(), "promise_id column missing on crm_tasks"

print("Phase 4a schema verified OK")
cur.close()
conn.close()
