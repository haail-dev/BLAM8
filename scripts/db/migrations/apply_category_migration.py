"""One-shot: apply the auto-categorisation migration (db/schema.sql
'Auto-categorisation — smart inbox labels' section) to prod via the Supabase
session pooler, then verify. Adds email_log.category / category_reason /
category_source + the (tenant_id, category) index. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Auto-categorisation — smart inbox labels (2026-07-26)"


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
print(f"auto-categorisation migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'email_log' and column_name like 'category%'
  order by column_name
""")
cols = [r[0] for r in cur.fetchall()]
print("email_log category columns:", cols)
assert {"category", "category_reason", "category_source"} <= set(cols)
cur.execute("select indexname from pg_indexes where tablename = 'email_log' "
            "and indexname = 'email_log_category_idx'")
assert cur.fetchone(), "email_log_category_idx missing"
print("index: email_log_category_idx")

conn.close()
print("DONE")
