"""One-shot: add llm_calls.request_id (LLM exchange correlation, 2026-07-26)
to prod via the Supabase session pooler, then verify. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute("alter table llm_calls add column if not exists request_id uuid")
print("DDL applied")

cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'llm_calls' and column_name = 'request_id'
""")
row = cur.fetchone()
print("llm_calls.request_id:", row)
assert row == ("request_id", "uuid")

conn.close()
print("DONE")
