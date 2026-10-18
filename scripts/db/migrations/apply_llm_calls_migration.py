"""One-shot: apply the llm_calls migration (db/schema.sql 'LLM call log'
section) to prod via the Supabase session pooler, then verify. Creates the
full-transcript LLM observability table written by src/app/llm.py — service-role
only, purged daily past settings.llm_log_retention_days. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- LLM call log"


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
print(f"llm_calls migration block: {len(block)} chars")

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
    where table_name = 'llm_calls' order by ordinal_position
""")
cols = cur.fetchall()
print(f"llm_calls columns: {[c[0] for c in cols]}")
assert {"tenant_id", "operation", "model", "request", "response"} <= {c[0] for c in cols}
cur.execute("select relrowsecurity from pg_class where relname = 'llm_calls'")
assert cur.fetchone()[0] is True, "RLS not enabled on llm_calls"
cur.execute("select count(*) from pg_policies where tablename = 'llm_calls'")
print(f"RLS on, tenant policies: {cur.fetchone()[0]} (0 = service-role only, as designed)")
conn.close()
print("OK")
