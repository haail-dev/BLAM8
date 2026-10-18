"""One-shot: apply the OS founding-partner access migration (db/schema.sql
'-- OS founding-partner access gate' section) to prod via the Supabase session
pooler, then verify. Creates os_access_requests and adds
tenants.os_access_approved — the flag /billing/checkout enforces for the
OS-tier price ids. Safe to re-run ('create table if not exists' /
'add column if not exists')."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- OS founding-partner access gate"

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
print(f"os-access migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'os_access_requests' order by ordinal_position
""")
print("os_access_requests columns:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenants' and column_name = 'os_access_approved'
""")
print("tenants.os_access_approved:", bool(cur.fetchall()))

conn.close()
print("DONE")
