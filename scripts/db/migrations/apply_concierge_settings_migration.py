"""One-shot: tenants.concierge_settings jsonb (per-business Concierge task timeouts,
2026-10-14). Applied via the Supabase session pooler like
scripts/db/migrations/apply_waitlist_migration.py; safe to re-run. Run from the folder holding .env."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table tenants add column if not exists concierge_settings jsonb not null default '{}'::jsonb;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(DDL)
cur.execute("select data_type, column_default, is_nullable from information_schema.columns "
            "where table_name='tenants' and column_name='concierge_settings'")
print("column:", cur.fetchone())
cur.execute("select count(*), count(*) filter (where concierge_settings = '{}'::jsonb) from tenants")
print("tenants / with empty settings:", cur.fetchone())
conn.close()
