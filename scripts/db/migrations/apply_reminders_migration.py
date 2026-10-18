"""One-shot: apply the Phase 2B reminders migration (db/schema.sql 'Phase 2B' section) to
prod via the Supabase session pooler, then verify the new columns. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Phase 2B"


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
print(f"reminders migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select table_name, column_name from information_schema.columns
  where (table_name = 'email_log' and column_name = 'kind')
     or (table_name = 'bookings' and column_name = 'reminder_queued_at')
  order by table_name
""")
print("new columns:", cur.fetchall())
cur.execute("select count(*) from email_log where kind = 'inbound'")
print("existing email_log rows defaulted to kind='inbound':", cur.fetchone()[0])

conn.close()
print("DONE")
