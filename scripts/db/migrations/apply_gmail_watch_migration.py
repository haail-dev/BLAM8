"""One-shot: apply the Gmail push watch migration (db/schema.sql 'Gmail push
watch' section) to prod via the Supabase session pooler, then verify. Adds
email_connections.gmail_watch_expiry (timestamptz, NULL = no watch armed) —
the renewal sweep stamps it when users.watch is armed/renewed. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Gmail push watch"


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
print(f"gmail-watch migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

# ── verify ────────────────────────────────────────────────────────────────────
cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'email_connections' and column_name = 'gmail_watch_expiry'
""")
print("new column:", cur.fetchall())

conn.close()
print("DONE")
