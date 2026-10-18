"""One-shot (run #8, 2026-09-05): `dependants_configured_at` on scheduling_config
— the Dependants settings page was answered (a kind, a ladder-only save, or
"nothing of the sort"), so the setup hand-off can count the step done for a
business whose customers are the participants. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table scheduling_config add column if not exists dependants_configured_at timestamptz;
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
cur.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name in ('dependants_configured_at')")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
