"""One-shot: apply the Field Service recurring-service-reminders migration (db/schema.sql
FSM reminders section — job_types.service_months + bookings.service_reminder_queued_at) to
prod via the Supabase session pooler, then verify. Safe to re-run (guarded ADD COLUMN)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Field Service recurring service reminders"


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
print(f"FSM reminders migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("""
  select table_name, column_name from information_schema.columns
  where (table_name = 'job_types' and column_name = 'service_months')
     or (table_name = 'bookings' and column_name = 'service_reminder_queued_at')
  order by table_name, column_name
""")
print("cols:", cur.fetchall())

conn.close()
print("DONE")
