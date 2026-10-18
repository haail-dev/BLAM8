"""One-shot: add tenant_config.portal_reschedule_mode to prod (the Customer
reschedules setting, 2026-07-31 — 'auto' default | 'approve'). Found live:
saving Settings → Customer Portal 500'd until this column existed. Safe to
re-run (add column if not exists)."""
import re

import psycopg2

ENV_PATH = ".env"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute("alter table tenant_config add column if not exists "
            "portal_reschedule_mode text not null default 'auto'")
print("DDL applied")

cur.execute("""
  select column_name, column_default from information_schema.columns
  where table_name = 'tenant_config' and column_name = 'portal_reschedule_mode'
""")
row = cur.fetchone()
print("column:", row)
assert row is not None, "column missing after apply"

conn.close()
print("DONE")
