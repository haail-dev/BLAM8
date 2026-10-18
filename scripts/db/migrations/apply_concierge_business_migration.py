"""One-shot: apply the concierge business-Q&A migration (db/schema.sql
'Concierge business Q&A' section) to prod via the Supabase session pooler, then
verify. Creates concierge_messages (tenant-scoped chat transcript for the
/app/concierge page) with RLS. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Concierge business Q&A — chat history"


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
print(f"concierge-business migration block: {len(block)} chars")

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
  select column_name from information_schema.columns
  where table_name = 'concierge_messages'
  order by ordinal_position
""")
cols = [r[0] for r in cur.fetchall()]
print("concierge_messages columns:", cols)
assert {"id", "tenant_id", "user_id", "role", "content", "links", "created_at"} <= set(cols)
cur.execute("select relrowsecurity from pg_class where relname = 'concierge_messages'")
rls = cur.fetchone()
print("RLS enabled:", rls)
assert rls and rls[0]
cur.execute("select policyname from pg_policies where tablename = 'concierge_messages'")
print("policies:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
