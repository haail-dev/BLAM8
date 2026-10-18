"""One-shot: apply the email top-up credit block of db/schema.sql to prod.

Adds tenants.email_credits, the email_credit_ledger audit table, and the two
functions that write them (grant_email_credit / spend_email_credit). Idempotent
-- add column if not exists / create if not exists / create or replace function
-- so re-running is safe. Mirrors the other apply_*_migration scripts.
"""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Email top-up credit (Kris 2026-09-01)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(START):]
print(f"email-credit migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("select count(*) from email_credit_ledger")
print("ledger rows:", cur.fetchone()[0])
cur.execute("select count(*) from tenants where email_credits is not null")
print("tenants with a credit balance column:", cur.fetchone()[0])
cur.close()
conn.close()
