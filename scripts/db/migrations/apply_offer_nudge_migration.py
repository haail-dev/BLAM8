"""One-shot: apply the Phase 3 offer-nudge columns (db/schema.sql, smart-booking
offers) to prod via the Supabase session pooler, then verify. Purely ADDITIVE
(add column if not exists) — safe to re-run. Needs DB_PASSWORD in .env.
Mirrors scripts/db/migrations/apply_offered_slots_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Smart booking Phase 3: the pre-expiry offer nudge"


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
print(f"offer-nudge migration block: {len(sql)} chars")

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
  where table_name = 'bookings' and column_name = 'offer_nudged_at'
""")
print("bookings col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'scheduling_config' and column_name = 'offer_nudge_enabled'
""")
print("scheduling_config col:", [r[0] for r in cur.fetchall()])
conn.close()
print("DONE")
