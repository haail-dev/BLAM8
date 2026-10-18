"""One-shot: apply the agent trace migration (db/schema.sql 'Agent trace'
section) to prod via the Supabase session pooler, then verify. Creates
agent_trace (execution-narrative observability, service-role only) with RLS
enabled and NO tenant policy. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Agent trace — execution-narrative observability"


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
print(f"agent-trace migration block: {len(block)} chars")

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
  where table_name = 'agent_trace' order by ordinal_position
""")
cols = [r[0] for r in cur.fetchall()]
print("agent_trace columns:", cols)
assert {"id", "created_at", "tenant_id", "trace_type", "outcome", "request_id",
        "session_id", "user_id", "contact", "email_log_id", "booking_id",
        "proposal_id", "latency_ms", "detail"} <= set(cols)
cur.execute("select relrowsecurity from pg_class where relname = 'agent_trace'")
rls = cur.fetchone()
print("RLS enabled:", rls)
assert rls and rls[0]
cur.execute("select policyname from pg_policies where tablename = 'agent_trace'")
policies = [r[0] for r in cur.fetchall()]
print("policies:", policies)
assert policies == []   # service-role only: RLS on, no tenant policy
cur.execute("""
  select indexname from pg_indexes where tablename = 'agent_trace'
""")
print("indexes:", sorted(r[0] for r in cur.fetchall()))

conn.close()
print("DONE")
