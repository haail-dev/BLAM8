"""One-shot: apply the unified slot-grid Phase 2 columns (db/schema.sql slot-grid
section) to prod via the Supabase session pooler, then verify the columns exist. Purely
ADDITIVE (add column if not exists) — safe to re-run, no existing row touched. After this,
review a backfill of start_block/length_blocks from `window`. The historical
backfill helper is not present in this baseline; do not assume this DDL alone
converts existing rows.

Needs DB_PASSWORD in .env (the session-pooler password). Mirrors
scripts/db/migrations/apply_fsm_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Unified slot-grid (docs/architecture/unified-slot-grid.md) — Phase 2 columns"


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
print(f"slot-grid migration block: {len(sql)} chars")

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
  where table_name = 'bookings' and column_name in ('start_block','length_blocks')
  order by column_name
""")
print("bookings cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'scheduling_config' and column_name = 'block_minutes'
""")
print("scheduling_config col:", [r[0] for r in cur.fetchall()])
conn.close()
print("DDL complete — existing window-based rows still need a reviewed slot-block backfill.")
