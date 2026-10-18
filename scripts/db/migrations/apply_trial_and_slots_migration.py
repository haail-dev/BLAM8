"""One-shot: apply the trial-lock + Google-slots section of db/schema.sql to prod.

Adds the tenants.trial_* columns (+ backfill, default, grace runway), the
increment_trial_email RPC, and the count_provider_signups RPC (reads
auth.identities for the login-page slot indicator). Idempotent — add column if
not exists / create or replace function. Mirrors the other apply_*_migration
scripts (psycopg2 + DB_PASSWORD from .env, direct pooler host)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Trial lock + email cap (src/app/trial.py"   # apply from here to EOF


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
migration_sql = schema[schema.index(START):]
print(f"trial+slots migration block: {len(migration_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(migration_sql)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenants'
    and column_name in ('trial_start_at','trial_emails_used','trial_status','trial_grace_until')
  order by column_name
""")
print("tenants trial cols:", [r[0] for r in cur.fetchall()])
cur.execute("select trial_status, count(*) from tenants group by 1 order by 1")
print("tenants by trial_status:", cur.fetchall())
cur.execute("select count_provider_signups('google'), count_provider_signups('azure')")
print("signups (google, azure):", cur.fetchone())

conn.close()
print("DONE")
