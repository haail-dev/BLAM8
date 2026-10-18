"""One-shot: apply the Customer Portal migration (db/schema.sql 'Customer
Portal' section) to prod via the Supabase session pooler, then verify.
Adds tenants.portal_slug, crm_contacts.reference_code (both with unique
partial indexes) and the tenant_config portal_* settings columns.
Safe to re-run (add column if not exists / create index if not exists)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Customer Portal — public self-service page per business"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
block = schema[schema.index(MARKER):]
print(f"portal migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select table_name, column_name from information_schema.columns
  where (table_name, column_name) in (
    ('tenants', 'portal_slug'),
    ('crm_contacts', 'reference_code'),
    ('tenant_config', 'portal_enabled'),
    ('tenant_config', 'portal_description'),
    ('tenant_config', 'portal_service_area'),
    ('tenant_config', 'portal_welcome'),
    ('tenant_config', 'portal_show_prices'))
""")
cols = {(r[0], r[1]) for r in cur.fetchall()}
print("columns:", sorted(cols))
assert len(cols) == 7, cols
cur.execute("""
  select indexname from pg_indexes
  where indexname in ('tenants_portal_slug_idx', 'crm_contacts_reference_code_idx')
""")
idx = [r[0] for r in cur.fetchall()]
print("indexes:", idx)
assert len(idx) == 2, idx

conn.close()
print("DONE")
