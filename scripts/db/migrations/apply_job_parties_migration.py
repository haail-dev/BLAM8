# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-02
"""One-shot: the job_parties table (docs/modules/module-job-parties.md Phase 1) — many
contacts on one job, each with a free-text label and four routing duties
(pays / approves / on_site / informed). Closes the researched leak: "a
tenanted property could have 2 tenants, a landlord, an agent and a managing
factor; some apps only allow 1-2 contacts per job."
Safe to re-run (CREATE TABLE IF NOT EXISTS throughout)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Labels are not routing (module-job-parties.md D-principle 1): `label` is
-- what the operator calls them; the four booleans are what the system does.
CREATE TABLE IF NOT EXISTS job_parties (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  booking_id  uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  contact_id  uuid REFERENCES crm_contacts(id) ON DELETE SET NULL,
  name        text,
  email       text,
  phone       text,
  label       text,                          -- 'landlord', 'tenant', 'agent'…
  pays        boolean NOT NULL DEFAULT false,
  approves    boolean NOT NULL DEFAULT false,
  on_site     boolean NOT NULL DEFAULT false,
  informed    boolean NOT NULL DEFAULT false,
  source      text NOT NULL DEFAULT 'owner', -- owner | extracted | confirmed
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_parties_unique_email
  ON job_parties (tenant_id, booking_id, email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_parties_booking
  ON job_parties (tenant_id, booking_id);

ALTER TABLE job_parties ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - job_parties" ON job_parties;
CREATE POLICY "tenant isolation - job_parties" ON job_parties
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

print(f"job_parties migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

for column in ("booking_id", "label", "pays", "approves", "on_site",
               "informed", "source"):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'job_parties' AND column_name = %s;", (column,))
    assert cur.fetchone(), f"{column} column missing on job_parties"

print("job_parties schema verified OK")
cur.close()
conn.close()
