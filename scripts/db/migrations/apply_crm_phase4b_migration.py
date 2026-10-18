# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply Phase 4b CRM schema — multi-step follow-up sequences.
Safe to re-run (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Phase 4b §19.3: multi-step follow-up sequences
CREATE TABLE IF NOT EXISTS crm_sequences (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name          text NOT NULL,
  description   text,
  trigger_type  text NOT NULL DEFAULT 'manual',
  -- manual | deal_stage_enter | gone_quiet | new_contact | tag_added
  trigger_config jsonb NOT NULL DEFAULT '{}',
  status        text NOT NULL DEFAULT 'active',   -- active | paused | archived
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crm_sequence_steps (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  sequence_id      uuid NOT NULL REFERENCES crm_sequences(id) ON DELETE CASCADE,
  step_order       integer NOT NULL,
  delay_days       integer NOT NULL DEFAULT 3,
  action_type      text NOT NULL DEFAULT 'send_email',
  -- send_email | update_stage | create_task | add_tag
  template_subject text,
  template_body    text,
  exit_on_reply    boolean NOT NULL DEFAULT true,
  side_effects     jsonb NOT NULL DEFAULT '[]',
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_seq_steps_seq
  ON crm_sequence_steps (tenant_id, sequence_id, step_order);

CREATE TABLE IF NOT EXISTS crm_sequence_enrollments (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  sequence_id    uuid NOT NULL REFERENCES crm_sequences(id) ON DELETE CASCADE,
  contact_id     uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  deal_id        uuid REFERENCES crm_deals(id) ON DELETE SET NULL,
  current_step   integer NOT NULL DEFAULT 0,
  status         text NOT NULL DEFAULT 'active',  -- active | completed | exited | paused
  next_action_at timestamptz,
  enrolled_at    timestamptz NOT NULL DEFAULT now(),
  exited_at      timestamptz,
  exit_reason    text  -- replied | stage_changed | manual | completed | bounced
);
CREATE INDEX IF NOT EXISTS idx_crm_seq_enroll_due
  ON crm_sequence_enrollments (tenant_id, status, next_action_at)
  WHERE status = 'active';

-- Global per-contact opt-out from all sequences.
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS do_not_sequence boolean NOT NULL DEFAULT false;

ALTER TABLE crm_sequences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_sequences" ON crm_sequences;
CREATE POLICY "tenant isolation - crm_sequences" ON crm_sequences
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE crm_sequence_steps ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_sequence_steps" ON crm_sequence_steps;
CREATE POLICY "tenant isolation - crm_sequence_steps" ON crm_sequence_steps
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE crm_sequence_enrollments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_sequence_enrollments" ON crm_sequence_enrollments;
CREATE POLICY "tenant isolation - crm_sequence_enrollments" ON crm_sequence_enrollments
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

print(f"CRM Phase 4b migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify
for table in ("crm_sequences", "crm_sequence_steps", "crm_sequence_enrollments"):
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s;", (table,))
    assert cur.fetchone(), f"{table} table missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_contacts' AND column_name = 'do_not_sequence';
""")
assert cur.fetchone(), "do_not_sequence column missing on crm_contacts"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_sequence_enrollments' AND column_name = 'next_action_at';
""")
assert cur.fetchone(), "next_action_at column missing on crm_sequence_enrollments"

print("Phase 4b schema verified OK")
cur.close()
conn.close()
