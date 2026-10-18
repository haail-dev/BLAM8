"""One-shot: apply the portal banner branding column (db/schema.sql,
2026-08-02) to prod via the Supabase session pooler, then verify. Purely
additive (add column if not exists) — safe to re-run. Needs DB_PASSWORD in
.env; falls back to the main checkout's .env when run from a worktree.
Mirrors scripts/db/migrations/apply_trial_concierge_migration.py."""
import os
import re

import psycopg2

ENV_CANDIDATES = (".env", os.path.join("..", "..", "..", ".env"))
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Customer Portal banner branding (2026-08-02, Julius)"


def env_value(key: str) -> str:
    for env_path in ENV_CANDIDATES:
        if not os.path.exists(env_path):
            continue
        for line in open(env_path, encoding="utf-8"):
            line = line.strip().lstrip("#").strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in any of {ENV_CANDIDATES}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(MARKER):]
print(f"portal-banner migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenant_config' and column_name = 'portal_banner_path'
""")
print("tenant_config col:", [r[0] for r in cur.fetchall()])
conn.close()
print("DONE")
