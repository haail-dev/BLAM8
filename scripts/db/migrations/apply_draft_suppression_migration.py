"""One-shot: apply the draft-suppression migration (db/schema.sql
'Draft suppression — stop drafting for contacts the owner ignores' section) to
prod via the Supabase session pooler, then verify. Adds the contact_draft_stats
table + (tenant_id, status) index + RLS. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Draft suppression — stop drafting for contacts the owner ignores (2026-07-26)"


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
print(f"draft-suppression migration block: {len(block)} chars")

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
  where table_name = 'contact_draft_stats'
  order by column_name
""")
cols = {r[0] for r in cur.fetchall()}
print("contact_draft_stats columns:", sorted(cols))
assert {"status", "consecutive_unsent_drafts", "total_unsent_drafts",
        "total_sent_drafts", "emails_since_suppressed", "suppressed_by"} <= cols
cur.execute("select indexname from pg_indexes where tablename = 'contact_draft_stats' "
            "and indexname = 'contact_draft_stats_status_idx'")
assert cur.fetchone(), "contact_draft_stats_status_idx missing"
print("index: contact_draft_stats_status_idx")
cur.execute("select relrowsecurity from pg_class where relname = 'contact_draft_stats'")
assert cur.fetchone()[0], "RLS not enabled"
print("RLS: enabled")

conn.close()
print("DONE")
