"""One-shot: apply the concierge transparency & proactivity migration
(db/schema.sql 'Concierge transparency & proactivity' section) to prod via the
Supabase session pooler, then verify. Adds concierge_messages.trail,
email_log.draft_sources, and the concierge_notifications table with RLS.
Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Concierge transparency & proactivity"


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
print(f"concierge-transparency migration block: {len(block)} chars")

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
  where table_name = 'concierge_messages' and column_name = 'trail'
""")
assert cur.fetchone(), "concierge_messages.trail missing"
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'email_log' and column_name = 'draft_sources'
""")
assert cur.fetchone(), "email_log.draft_sources missing"
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'concierge_notifications' order by ordinal_position
""")
cols = [r[0] for r in cur.fetchall()]
print("concierge_notifications columns:", cols)
assert {"id", "tenant_id", "kind", "title", "body", "url",
        "created_at", "seen_at"} <= set(cols)
cur.execute("select relrowsecurity from pg_class where relname = 'concierge_notifications'")
rls = cur.fetchone()
print("RLS enabled:", rls)
assert rls and rls[0]
cur.execute("select policyname from pg_policies where tablename = 'concierge_notifications'")
print("policies:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
