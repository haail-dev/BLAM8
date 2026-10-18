"""One-shot: apply the booking-details migration (db/schema.sql 'Booking-request details'
section) to prod via the Supabase session pooler, then verify the column exists. Adds
email_log.booking_details (jsonb) — the cumulative {label, value} list the pinned booking
card renders. Safe to re-run (DDL guarded by 'add column if not exists')."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Booking-request details"


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
print(f"booking-details migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

# ── verify column ─────────────────────────────────────────────────────────────
cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'email_log' and column_name = 'booking_details'
""")
print("new column:", cur.fetchall())

conn.close()
print("DONE")
