"""Add tenant_config.debrief_topics (2026-09-25): what the morning brief tells
you, as a list of topic keys (app/debrief.TOPICS). NULL = the defaults, so no
existing tenant changes until they choose. Idempotent.

    python scripts/db/migrations/apply_debrief_topics_migration.py
"""
import re

import psycopg2


def env_value(key: str) -> str:
    for line in open(".env", encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in .env")


ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432, dbname="postgres",
                        user=f"postgres.{ref}", password=env_value("DB_PASSWORD"), sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute("alter table public.tenant_config add column if not exists debrief_topics jsonb")
cur.execute("notify pgrst, 'reload schema'")
cur.execute("select data_type from information_schema.columns "
            "where table_name='tenant_config' and column_name='debrief_topics'")
print("tenant_config.debrief_topics:", cur.fetchone())
