"""One-shot: apply the concierge fair-use migration (db/schema.sql 'Concierge
cost controls' section) to prod via the Supabase session pooler, then verify.
Adds tenants.concierge_daily_used / concierge_daily_date and the
increment_concierge_daily RPC (docs/modules/module-concierge-cost-controls.md Part B).
Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Concierge cost controls"


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
print(f"concierge-fairuse migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'tenants' and column_name like 'concierge_daily%'
  order by column_name
""")
cols = cur.fetchall()
for name, dtype in cols:
    print(f"  tenants.{name}: {dtype}")
assert {c[0] for c in cols} == {"concierge_daily_used", "concierge_daily_date"}, cols

cur.execute("""
  select proname from pg_proc where proname = 'increment_concierge_daily'
""")
assert cur.fetchone(), "increment_concierge_daily RPC missing"
print("verified — columns + RPC present")
