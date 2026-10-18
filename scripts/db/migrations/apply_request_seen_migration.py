"""One-shot: apply the feature-request seen-stamp migration (db/schema.sql
'Feature-request seen stamp' section) to prod via the Supabase session pooler,
then verify. Adds feature_requests.seen_at (timestamptz, NULL = unseen) — the
operator nav's Requests badge counts NULLs; rows predating the badge are
backfilled seen so it starts at zero. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Feature-request seen stamp"


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
print(f"request-seen migration block: {len(block)} chars")

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
  where table_name = 'feature_requests' and column_name = 'seen_at'
""")
print("new column:", cur.fetchall())
cur.execute("select count(*) from feature_requests where seen_at is null")
print("unseen rows:", cur.fetchone())

conn.close()
print("DONE")
