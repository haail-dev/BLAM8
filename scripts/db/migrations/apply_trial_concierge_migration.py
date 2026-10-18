"""One-shot: apply the full-product-trial concierge counter (db/schema.sql,
2026-07-31) to prod via the Supabase session pooler, then verify. Purely
additive (add column if not exists + create or replace function) — safe to
re-run. Needs DB_PASSWORD in .env. Mirrors scripts/db/migrations/apply_offer_nudge_migration.py.

Note: existing trial tenants do NOT get modules backfilled by this script —
module seeding happens at tenant creation (app/db.ensure_tenant_for_user).
There are no real trial tenants yet (pre-launch); if any appear before this
deploys, flip their modules in the admin switchboard."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Full-product trial (2026-07-31, Julius)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(MARKER):]
print(f"trial-concierge migration block: {len(sql)} chars")

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
  where table_name = 'tenants' and column_name = 'trial_concierge_used'
""")
print("tenants col:", [r[0] for r in cur.fetchall()])
cur.execute("select count(*) from pg_proc where proname = 'increment_trial_concierge'")
print("increment_trial_concierge fns:", cur.fetchone()[0])
conn.close()
print("DONE")
