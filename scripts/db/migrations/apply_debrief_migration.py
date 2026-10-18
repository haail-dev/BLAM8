"""One-shot: apply the morning-debrief migration (db/schema.sql 'Morning
debrief' section) to prod via the Supabase session pooler, then verify. Adds
the four debrief columns to tenant_config (docs/modules/module-morning-debrief.md).
Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Morning debrief"


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
print(f"morning-debrief migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name, data_type, column_default
  from information_schema.columns
  where table_name = 'tenant_config' and column_name like 'debrief%'
  order by column_name
""")
cols = cur.fetchall()
for name, dtype, default in cols:
    print(f"  {name}: {dtype} default {default}")
assert {c[0] for c in cols} == {
    "debrief_enabled", "debrief_time", "debrief_on_closed_days",
    "debrief_last_sent_on",
}, f"unexpected column set: {[c[0] for c in cols]}"
print("verified — all four debrief columns present")
