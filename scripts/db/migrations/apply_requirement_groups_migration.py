# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-04
"""One-shot: Dependants (docs/modules/module-requirement-groups.md) — the booking is
about something that belongs to the customer. Three tables: the group
DEFINITION in the tenant's own word ("Horse", "Dog", "Vehicle") with its
fields and their durability; an INSTANCE per dependant on the customer's
record ("Murphy", with what is known about him); and the link from a booking
to the instances it is ABOUT (its subjects). Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- The definition: what a "Horse" is, for THIS tenant. Labels are the
-- operator's words; the mechanics (ask, remember, prefill, gate) are fixed.
CREATE TABLE IF NOT EXISTS requirement_groups (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  label          text NOT NULL,                      -- 'Horse'
  label_plural   text NOT NULL DEFAULT '',            -- 'Horses'
  cardinality    text NOT NULL DEFAULT 'many',        -- 'one' | 'many' per contact
  fields         jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{key,label,type,options,required,hint,durability,gate}]
  display_order  int NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_requirement_groups_tenant
  ON requirement_groups (tenant_id, display_order);

-- A filled copy: "Murphy", on Kris's record.
CREATE TABLE IF NOT EXISTS requirement_instances (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  group_id    uuid NOT NULL REFERENCES requirement_groups(id) ON DELETE CASCADE,
  contact_id  uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  name        text NOT NULL,
  data        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {key: value}
  field_meta  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {key: {source, at, email_log_id}}
  archived_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_instances_unique_name
  ON requirement_instances (tenant_id, group_id, contact_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_requirement_instances_contact
  ON requirement_instances (tenant_id, contact_id);

-- Which instance(s) a booking is about.
CREATE TABLE IF NOT EXISTS booking_subjects (
  booking_id   uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  instance_id  uuid NOT NULL REFERENCES requirement_instances(id) ON DELETE CASCADE,
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (booking_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_booking_subjects_instance
  ON booking_subjects (tenant_id, instance_id);

ALTER TABLE requirement_groups ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - requirement_groups" ON requirement_groups;
CREATE POLICY "tenant isolation - requirement_groups" ON requirement_groups
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE requirement_instances ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - requirement_instances" ON requirement_instances;
CREATE POLICY "tenant isolation - requirement_instances" ON requirement_instances
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE booking_subjects ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - booking_subjects" ON booking_subjects;
CREATE POLICY "tenant isolation - booking_subjects" ON booking_subjects
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


if __name__ == "__main__":
    password = env_value("DB_PASSWORD")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
    print(f"requirement_groups migration: {len(MIGRATION_SQL)} chars")
    conn = psycopg2.connect(
        host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(MIGRATION_SQL)
    print("DDL applied")
    for table, column in (("requirement_groups", "fields"), ("requirement_instances", "field_meta"),
                          ("booking_subjects", "instance_id")):
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s;", (table, column))
        assert cur.fetchone(), f"{column} missing on {table}"
    print("requirement groups schema verified OK")
    cur.close()
    conn.close()
