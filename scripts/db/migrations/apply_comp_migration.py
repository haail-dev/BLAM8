"""One-shot: apply the admin comp-access migration (db/schema.sql — the
tenants.comp_access + comp_until columns) to prod via the Supabase session
pooler, then verify. Safe to re-run (ALTER ... if not exists). No backfill —
NULL comp_access = 'off' = normal billing, which is correct for every existing
tenant. Mirrors scripts/db/migrations/apply_mailbox_addon_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Admin comp access — free/comped tenants"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(MARKER):]
print(f"Comp-access migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied (tenants.comp_access, tenants.comp_until)")

cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'tenants' and column_name in ('comp_access','comp_until')
  order by column_name
""")
print("columns:", cur.fetchall())

conn.close()
print("DONE")
