"""One-shot (2026-09-05, the per-slice nits 1b-6b): booking_attendance.notified_at
(when the customer told the business — the make-up notice rule) and
booking_notes.deleted_at (a customer may withdraw their own note). Applied via
the Supabase session pooler; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table booking_attendance add column if not exists notified_at timestamptz;
alter table booking_notes add column if not exists deleted_at timestamptz;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(DDL)
cur.execute("select table_name, column_name from information_schema.columns where (table_name, column_name) in (('booking_attendance','notified_at'),('booking_notes','deleted_at'))")
print(cur.fetchall())
cur.close(); conn.close()
