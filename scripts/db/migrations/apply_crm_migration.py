"""One-shot: apply the CRM module migration (db/schema.sql CRM section — the six
crm_* tables + RLS) to prod via the Supabase session pooler, then verify. Safe to
re-run (guarded). Mirrors scripts/db/migrations/apply_invoicing_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = '-- CRM module — "the CRM that fills itself in"'

TABLES = ("crm_companies", "crm_contacts", "crm_stages",
          "crm_deals", "crm_activities", "crm_artifacts")


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
print(f"CRM migration block: {len(sql)} chars")

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
  where tablename = 'crm_activities' and indexname = 'crm_activities_source_uniq'
""")
print("idempotency index:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
