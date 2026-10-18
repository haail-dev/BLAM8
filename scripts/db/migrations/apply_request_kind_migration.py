"""One-shot: apply the feature-request kind migration (db/schema.sql
'Feature-request kind' section) to prod via the Supabase session pooler, then
verify the column exists. Adds feature_requests.kind ('feature' | 'bug', default
'feature') — the concierge chatbot files issue reports into the same pipeline.
Safe to re-run (DDL guarded by 'add column if not exists')."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Feature-request kind"


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
print(f"request-kind migration block: {len(block)} chars")

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
  select column_name, data_type, column_default from information_schema.columns
  where table_name = 'feature_requests' and column_name = 'kind'
""")
print("new column:", cur.fetchall())

conn.close()
print("DONE")
