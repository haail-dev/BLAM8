"""One-shot: apply the HRT review-time migration (db/schema.sql 'HRT
review-time' section) to prod via the Supabase session pooler, then verify.
Adds email_log.review_seconds — the visible dwell a human spent on a draft
before sending (Kris 2026-08-31). Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- HRT review-time (2026-08-31) — email_log.review_seconds"


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
print(f"review-time migration block: {len(block)} chars")

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
  where table_name = 'email_log' and column_name = 'review_seconds'
""")
row = cur.fetchone()
print("email_log.review_seconds:", row)
assert row and row[1] == "integer"

conn.close()
print("DONE")
