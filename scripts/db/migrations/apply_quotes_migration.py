"""One-shot: apply the Quotes module migration (db/schema.sql — quotes,
quote_lines, tenant_counters.quote_seq + next_quote_ref, RLS, invoice_settings
quote defaults, tenant_config.notify_quote_viewed) to prod via the Supabase
session pooler, then verify. Safe to re-run (guarded).
Mirrors scripts/db/migrations/apply_invoice_payments_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Quotes module (docs/modules/module-quotes.md, 2026-07-30)"


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
print(f"Quotes migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'quotes' order by ordinal_position
""")
print("quotes cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'quote_lines' order by ordinal_position
""")
print("quote_lines cols:", [r[0] for r in cur.fetchall()])
cur.execute("select column_name from information_schema.columns "
            "where table_name = 'tenant_counters' and column_name = 'quote_seq'")
print("tenant_counters col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'invoice_settings' and column_name in
    ('default_quote_valid_days','default_deposit_percent')
""")
print("invoice_settings cols:", [r[0] for r in cur.fetchall()])
cur.execute("select column_name, column_default from information_schema.columns "
            "where table_name = 'tenant_config' and column_name = 'notify_quote_viewed'")
print("tenant_config col:", cur.fetchall())
cur.execute("select proname from pg_proc where proname = 'next_quote_ref'")
print("function:", [r[0] for r in cur.fetchall()])
cur.execute("select tablename, rowsecurity from pg_tables "
            "where tablename in ('quotes','quote_lines')")
print("RLS:", cur.fetchall())
conn.close()
print("done")
