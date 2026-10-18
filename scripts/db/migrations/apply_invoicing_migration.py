"""One-shot: apply the Invoicing module migration (db/schema.sql invoicing section —
invoice_settings + invoices.sent_at/paid_at) to prod via the Supabase session pooler,
then verify. Safe to re-run (guarded). Mirrors scripts/db/migrations/apply_fsm_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Invoicing module — modular invoice settings"


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
print(f"Invoicing migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("select to_regclass('public.invoice_settings')")
print("table:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'invoices' and column_name in ('sent_at','paid_at') order by column_name
""")
print("invoices cols:", [r[0] for r in cur.fetchall()])
cur.execute("select policyname from pg_policies where tablename = 'invoice_settings'")
print("policies:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
