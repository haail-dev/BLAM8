"""One-shot: apply the scheduling-intake migration (db/schema.sql 'Scheduling intake'
section) to prod via the Supabase session pooler, verify, and confirm the grandfather
backfill marked existing scheduling tenants configured. Safe to re-run (DDL guarded;
the backfill only fills NULLs)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Scheduling intake"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
intake_sql = schema[schema.index(MARKER):]
print(f"scheduling-intake migration block: {len(intake_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(intake_sql)
print("DDL + grandfather backfill applied")

# ── verify columns ───────────────────────────────────────────────────────────
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'scheduling_config'
    and column_name in ('intake_fields', 'intake_configured_at')
  order by column_name
""")
print("new columns:", [r[0] for r in cur.fetchall()])

# ── confirm active scheduling tenants were grandfathered configured ───────────
cur.execute("""
  select count(*) from subscriptions s
   where s.module = 'scheduling' and s.status = 'active'
     and not exists (
       select 1 from scheduling_config c
        where c.tenant_id = s.tenant_id and c.intake_configured_at is not null)
""")
unconfigured = cur.fetchone()[0]
print("active scheduling tenants still unconfigured (want 0):", unconfigured)

cur.execute("select count(*) from scheduling_config where intake_configured_at is not null")
print("scheduling_config rows marked configured:", cur.fetchone()[0])

conn.close()
print("DONE")
