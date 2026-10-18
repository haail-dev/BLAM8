"""One-shot: scheduling_config.front_desk — the front desk rule (Kris
2026-09-13: may an appointment overlap a drop-off or a collection? {"enabled":
bool, "minutes": int}; app/diary_config.clean_front_desk). Applied via the
Supabase session pooler like scripts/db/migrations/apply_inbox_ignore_migration.py; safe to
re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table scheduling_config add column if not exists front_desk jsonb not null default '{}'::jsonb;
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
cur.execute("select column_name, data_type from information_schema.columns where table_name='scheduling_config' and column_name='front_desk'")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
