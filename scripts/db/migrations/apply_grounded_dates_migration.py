"""One-shot: scheduling_config.grounded_dates (docs/plan-grounded-booking.md,
2026-09-22) — the per-tenant switch for the grounded-booking gate. Purely
ADDITIVE (add column if not exists, default false) — safe to re-run. Reads the
block from db/schema.sql by its marker so the two can never drift. Needs
DB_PASSWORD in .env (a commented-out line still counts). Mirrors
scripts/db/migrations/apply_offer_nudge_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Grounded booking (docs/plan-grounded-booking.md"


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
print(f"grounded-dates migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("""
  SELECT column_name, data_type, column_default FROM information_schema.columns
  WHERE table_name = 'scheduling_config' AND column_name = 'grounded_dates';
""")
print("scheduling_config.grounded_dates:", cur.fetchall() or "MISSING")
cur.execute("SELECT count(*) FROM scheduling_config WHERE grounded_dates;")
print("tenants with it ON:", cur.fetchone()[0])
cur.close()
conn.close()
