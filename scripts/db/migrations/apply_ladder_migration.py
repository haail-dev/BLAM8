"""One-shot: progress ladders (src/app/ladders.py, 2026-09-05) — the `ladder` jsonb
column on crm_field_definitions (a contact-level ladder is a dropdown
definition carrying {notify, gates}). Applied via the Supabase session pooler
like scripts/db/migrations/apply_agent_events_migration.py; safe to re-run."""
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
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute("alter table crm_field_definitions add column if not exists ladder jsonb;")
cur.execute("select column_name, data_type from information_schema.columns "
            "where table_name = 'crm_field_definitions' and column_name = 'ladder'")
print("crm_field_definitions.ladder:", cur.fetchone())
cur.close(); conn.close()
