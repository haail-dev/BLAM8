"""One-shot: apply the Voice CRM migration (db/schema.sql 'Voice CRM (founder
tool)' section) to prod via the Supabase session pooler, then verify. Creates
vcrm_prospects + vcrm_calls (Kris's cold-call debrief tool, service-role only)
with RLS enabled and NO policies. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Voice CRM (founder tool) — vcrm_*"


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
print(f"vcrm migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

for table, expect in (
    ("vcrm_prospects", {"id", "business_name", "description", "location", "address",
                        "phone", "website", "maps_url", "lat", "lng", "contact_name",
                        "status", "next_action", "next_action_date", "notes",
                        "created_at", "updated_at"}),
    ("vcrm_calls", {"id", "prospect_id", "called_at", "outcome", "summary",
                    "transcript", "extraction_json", "created_at"}),
):
    cur.execute("""
      select column_name from information_schema.columns
      where table_name = %s order by ordinal_position
    """, (table,))
    cols = [r[0] for r in cur.fetchall()]
    print(f"{table} columns:", cols)
    assert expect <= set(cols)
    cur.execute("select relrowsecurity from pg_class where relname = %s", (table,))
    rls = cur.fetchone()
    print(f"{table} RLS enabled:", rls)
    assert rls and rls[0]
    cur.execute("select policyname from pg_policies where tablename = %s", (table,))
    policies = [r[0] for r in cur.fetchall()]
    print(f"{table} policies:", policies)
    assert policies == []   # service-role only: RLS on, no policies

cur.execute("select indexname from pg_indexes where tablename in ('vcrm_prospects','vcrm_calls')")
print("indexes:", sorted(r[0] for r in cur.fetchall()))

conn.close()
print("DONE")
