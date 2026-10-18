"""One-shot: apply the sent-time migration (db/schema.sql 'Sent time' section)
to prod via the Supabase session pooler, then verify. Adds email_log.sent_at —
when a reply went, shown on the thread's sent bubble (Kris 2026-09-23). Safe
to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Sent time (2026-09-23) — email_log.sent_at"


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
print(f"sent-time migration block: {len(block)} chars")

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
  where table_name = 'email_log' and column_name = 'sent_at'
""")
row = cur.fetchone()
print("email_log.sent_at:", row)
assert row and row[1] == "timestamp with time zone"

conn.close()
print("DONE")
