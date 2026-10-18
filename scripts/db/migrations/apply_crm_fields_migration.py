"""One-shot: apply the CRM custom-fields migration (db/schema.sql phase-2 section
— crm_field_definitions + crm_field_suggestions + RLS) to prod via the Supabase
session pooler, then verify. Safe to re-run (guarded). Mirrors
scripts/db/migrations/apply_crm_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- CRM custom fields (phase 2"

TABLES = ("crm_field_definitions", "crm_field_suggestions")


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
print(f"CRM custom-fields migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

for t in TABLES:
    cur.execute(f"select to_regclass('public.{t}')")
    exists = cur.fetchone()[0]
    cur.execute("select policyname from pg_policies where tablename = %s", (t,))
    policies = [r[0] for r in cur.fetchall()]
    print(f"{t}: table={exists} policies={policies}")

cur.execute("""
  select indexname from pg_indexes
  where tablename = 'crm_field_suggestions' and indexname = 'crm_field_suggestions_source_uniq'
""")
print("suggestion idempotency index:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
